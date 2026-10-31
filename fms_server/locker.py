"""LastGate 배송 연동. 접수·계정 장부는 보관소, 로봇 예약·운행·적재 장부(cargo)는 FMS가 소유한다.

흐름: 예상 배송(DELIVERY_JOB) → 로봇 예약 → 픽업스테이션 node 이동 → ROBOT_ARRIVED
     → 최종 묶음(BUNDLE_CONFIRMED, 물품 2개·트레이 2개) → 물품 ID·묶음 저장 → 물품별 PICK
     → 세대별 물품별 DROP → 픽업 node 복귀·트레이 반납 DROP → 남은 물품 반납 → JOB_DONE.
"""
from __future__ import annotations

import copy
import json
import logging
import queue
import threading
import time
from pathlib import Path

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from fms_server.cargo import TRAY_RETURN_PREFIX, TRAYS, CargoError, CargoStore
from fms_server.task import JobError, JobStatus, resolve_steps, _parse_command

log = logging.getLogger("fms.locker")
JOB_TOPIC = "locker/robot/job"
BUNDLE_TOPIC = "locker/robot/bundle"
EVENT_TOPIC = "locker/robot/event"


class LockerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    site_id: str = Field(min_length=1)
    # 적재·복귀 지점. 픽업스테이션 ID(권장) 또는 이전 방식의 일반 목적지 이름 중 하나.
    pickup_station_id: Optional[str] = Field(default=None, min_length=1)
    pickup_target: Optional[str] = Field(default=None, min_length=1)
    locker_id: str = Field(default="LG-01", min_length=1, max_length=64)  # 접수번호와 함께 물품 ID 대응 키
    households: dict[str, str]
    pick_duration: float = Field(default=2, gt=0, le=300)
    drop_duration: float = Field(default=2, gt=0, le=300)

    @field_validator("households")
    @classmethod
    def check_households(cls, value):
        import re
        if not value or any(not re.fullmatch(r"\d+-\d+", k) or not v.strip() for k, v in value.items()):
            raise ValueError("세대는 동-호 형식과 목적지 이름으로 지정한다")
        return value

    @model_validator(mode="after")
    def _one_pickup(self):
        if (self.pickup_station_id is None) == (self.pickup_target is None):
            raise ValueError("픽업스테이션(pickup_station_id) 또는 보관소 목적지(pickup_target) 중 하나만 지정한다")
        return self


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


class LockerBridge:
    def __init__(self, registry, fleet, jobs, coordinator, mqtt, data_root, cargo=None):
        self.registry, self.fleet, self.jobs = registry, fleet, jobs
        self.coordinator, self.mqtt = coordinator, mqtt
        self.cargo = cargo or CargoStore(data_root)
        self.lock = coordinator.lock
        self.root = Path(data_root)
        self.config_path = self.root / "locker_config.json"
        self.path = self.root / "locker_deliveries.json"
        self.config = (LockerConfig.model_validate_json(self.config_path.read_text())
                       if self.config_path.exists() else None)
        self.deliveries = json.loads(self.path.read_text()) if self.path.exists() else {}
        self._messages = queue.Queue(maxsize=2000)
        self._stop = threading.Event()
        self._thread = None
        self._last_send = {}
        self._last_telemetry = 0
        # 시작 시 예약부터 복원한다. 미확인 오더는 재발행하지 않는다.
        for d in self.deliveries.values():
            if d.get("robot_id") and d["phase"] != "done":
                coordinator.reserve_robot(d["robot_id"], d["job_id"])
        mqtt.subscribe_json(JOB_TOPIC, self.enqueue)
        mqtt.subscribe_json(BUNDLE_TOPIC, self.enqueue)

    def enqueue(self, payload):
        try:
            self._messages.put_nowait(payload)
        except queue.Full:
            log.error("보관소 수신 큐 포화 — 보관소 heartbeat로 재동기화 대기")

    def _pickup_name(self, config: dict) -> str:
        """적재·복귀 node 의 목적지 이름. 픽업스테이션이면 연결이 정상인지 확인한다."""
        station_id = config.get("pickup_station_id")
        if not station_id:
            return config["pickup_target"]
        from fms_server.pickup_stations import PickupStationError, get_station
        site = config["site_id"]
        try:
            station = get_station(self.registry.map_store(site), self.registry.location_store(site), station_id)
        except PickupStationError as exc:
            raise JobError(str(exc)) from None
        if not station.ok:
            raise JobError(f"픽업스테이션 연결 확인 필요: {', '.join(station.problems)}")
        return station.name

    def configure(self, config):
        with self.lock:
            self.registry.get_site(config.site_id)
            locations = self.registry.location_store(config.site_id)
            pickup = self._pickup_name(config.model_dump())
            for name in {pickup, *config.households.values()}:
                locations.resolve_pose(name)
            _write(self.config_path, config.model_dump())
            self.config = config

    def snapshot(self):
        with self.lock:
            deliveries = copy.deepcopy(list(self.deliveries.values()))
            for d in deliveries:
                d["bundle"] = self.cargo.bundle(d["bundle_id"]) if d.get("bundle_id") else None
            return {"config": self.config.model_dump() if self.config else None,
                    "deliveries": deliveries}

    def _save(self):
        _write(self.path, self.deliveries)

    def start(self):
        self._thread = threading.Thread(target=self._run, name="locker-bridge", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join()

    def _run(self):
        while not self._stop.wait(.25):
            try:
                self.tick()
            except Exception:
                log.exception("보관소 연동 진행 실패 — 예약 유지")

    def _send(self, payload):
        self.mqtt.publish_json(EVENT_TOPIC, payload)

    def _events(self, d):
        if d["phase"] == "awaiting_bundle":
            return [{"type": "ROBOT_ARRIVED", "job_id": d["job_id"], "robot_id": d["robot_id"]}]
        events = [{"type": "DELIVERY_RESULT", "job_id": d["job_id"],
                   "receipt_no": receipt, "result": result}
                  for receipt, result in d["results"].items()]
        if d["phase"] == "done":
            events.append({"type": "JOB_DONE", "job_id": d["job_id"], "robot_id": d["robot_id"]})
        return events

    def _replay(self, d):
        if not self.mqtt.connected:
            return
        try:
            for event in self._events(d):
                self._send(event)
        except ConnectionError:
            log.warning("배송 결과 발행 보류: %s", d["job_id"])
            return
        self._last_send[d["job_id"]] = time.monotonic()

    @staticmethod
    def _items(payload):
        items = payload.get("items")
        if not isinstance(items, list) or not items or len(items) > len(TRAYS):
            raise JobError(f"배송 목록은 1~{len(TRAYS)}개 물품이어야 한다")
        seen = set()
        for it in items:
            if not isinstance(it, dict) or not isinstance(it.get("receipt_no"), str) or not it["receipt_no"]:
                raise JobError("접수번호가 없는 물품")
            if it["receipt_no"] in seen:
                raise JobError("중복 접수번호")
            seen.add(it["receipt_no"])
            if any(type(it.get(k)) is not int or it[k] <= 0 for k in ("dong", "ho")):
                raise JobError("동·호수가 잘못되었다")
        return copy.deepcopy(items)

    def _targets(self, config, items):
        locations = self.registry.location_store(config["site_id"])
        out = []
        for it in items:
            key = f'{it["dong"]}-{it["ho"]}'
            target = config["households"].get(key)
            if not target:
                raise JobError(f"세대 {key} 목적지 연결이 없다")
            map_id, pose = locations.resolve_pose(target)
            out.append({"receipt_no": it["receipt_no"], "household": key, "target": target,
                        "step": {"map_id": map_id, "pose": pose.model_dump()}})
        return out

    def receive(self, payload):
        """네트워크 콜백은 큐에만 넣고, 실제 처리는 배정 락 안에서 한다."""
        with self.lock:
            kind, key = payload.get("type"), payload.get("job_id")
            if not isinstance(key, str) or not key or len(key) > 128:
                return
            d = self.deliveries.get(key)
            if kind == "DELIVERY_JOB":
                if d:
                    self._replay(d)
                    return
                if self.config is None:
                    return  # 설정 후 보관소가 heartbeat로 다시 보낸다
                items = self._items(payload)
                config = self.config.model_dump()
                commands = [{"type": "move", "target": self._pickup_name(config)}]
                pickup = resolve_steps([_parse_command(c) for c in commands], config["site_id"], self.registry)[0]
                d = {"job_id": key, "phase": "queued", "robot_id": None, "config": config,
                     "pickup": pickup, "items": [], "stops": [], "index": 0, "results": {},
                     "proposed_items": items,
                     "child_id": None, "error": None}
                self.deliveries[key] = d
                self._save()
            elif kind == "BUNDLE_CONFIRMED" and d:
                if payload.get("robot_id") != d["robot_id"]:
                    raise JobError("적재 확정 로봇이 예약과 다르다")
                if d["phase"] != "awaiting_bundle":
                    self._replay(d)
                    return
                d["items"] = self._items(payload)
                try:
                    if len(d["items"]) != len(TRAYS):
                        raise JobError(f"최종 묶음이 물품 {len(TRAYS)}개가 아니라 적재하지 않는다 "
                                       f"({len(d['items'])}개)")
                    targets = self._targets(d["config"], d["items"])
                    # 물품 ID·묶음·트레이 배정을 먼저 영속 저장한 뒤 PICK 을 발행한다.
                    entries = [{**it, "tray": it.get("tray"), "household": t["household"],
                                "target": t["target"], "step": t["step"]}
                               for it, t in zip(d["items"], targets)]
                    bundle = self.cargo.open_bundle(
                        site_id=d["config"]["site_id"], delivery_job_id=key, robot_id=d["robot_id"],
                        locker_id=d["config"].get("locker_id", "LG-01"),
                        station_id=d["config"].get("pickup_station_id"),
                        station_name=d.get("pickup_name") or self._pickup_name(d["config"]),
                        items=entries)
                    d["bundle_id"] = bundle["bundle_id"]
                    parcel_of = {i["receipt_no"]: i["parcel_id"] for i in bundle["items"]}
                    d["parcels"] = [i["parcel_id"] for i in sorted(bundle["items"],
                                                                   key=lambda i: TRAYS.index(i["tray_id"]))]
                    # 같은 세대에 가는 물품은 이동을 공유하지만 DROP 은 물품마다 따로 한다.
                    groups = {}
                    for target in targets:
                        stop = groups.setdefault(target["household"], {**target, "receipts": [], "parcels": []})
                        stop["receipts"].append(target["receipt_no"])
                        stop["parcels"].append(parcel_of[target["receipt_no"]])
                    d["stops"] = [groups[k] for k in sorted(groups)]
                    d["phase"], d["child_id"] = "loading", None
                except (JobError, CargoError, ValueError, KeyError) as exc:
                    d["phase"], d["error"] = "blocked", str(exc)
                self._save()
            elif kind == "JOB_CANCELLED" and d and d["phase"] == "awaiting_bundle":
                # 이미 보관소에 있으므로 추가 주행 없이 예약을 해제한다.
                d["phase"] = "done"
                self._save()
                self.coordinator.release_robot(d["robot_id"], key)
                self._replay(d)

    def _child(self, d, commands, resolved):
        label = f'locker:{d["job_id"]}:{d["phase"]}:{d["index"]}'
        if not d["child_id"]:
            # 작업 생성 직후 중단된 경우에도 같은 하위 작업을 찾아 중복 생성을 막는다.
            prior = [j for j in self.jobs.list() if j.external_owner == d["job_id"] and j.preset_name == label]
            job = prior[-1] if prior else self.jobs.create(
                d["config"]["site_id"], commands, d["robot_id"], resolved_steps=resolved,
                external_owner=d["job_id"], preset_name=label)
            d["child_id"] = job.job_id
            self._save()
        job = self.jobs.get(d["child_id"])
        if job.status == JobStatus.PENDING:
            try:
                self.coordinator.dispatch(job.job_id, d["robot_id"])
                d["error"] = None
            except JobError as exc:
                # 배터리·맵 동기화·로컬라이징 대기는 다음 tick에서 다시 검사한다.
                d["error"] = str(exc)
            return False
        if job.status == JobStatus.SUCCESS:
            return True
        if job.status.terminal:
            d["phase"], d["error"] = "blocked", job.error or job.status.value
            self._save()
        return False

    def _action(self, d, kind, target, step, payloads):
        """같은 지점에서 물품마다 PICK/DROP 하나씩. payload_id = parcel_id (트레이 반납은 묶음 ID)."""
        duration = d["config"]["pick_duration" if kind == "pickup" else "drop_duration"]
        # 마지막 액션 종료와 clear_order가 같은 tick이면 FINISHED가 사라진다.
        # 뒤에 관측 대기를 둬 200 ms State 발행에서 인도 완료를 확인한다.
        commands = [{"type": kind, "target": target, "duration": duration,
                     "params": {"payload_id": payload}} for payload in payloads]
        commands.append({"type": "wait", "duration": 1})
        return self._child(d, commands, [step] * len(commands))

    @staticmethod
    def _where(target, step):
        return {"map_id": step["map_id"], **step["pose"], "target": target}

    def _observe(self, d):
        """진행 중인 하위 작업의 액션 보고를 물품 장부에 반영한다 (RUNNING/FINISHED)."""
        if not d.get("bundle_id") or not d.get("child_id") or not d.get("robot_id"):
            return
        job = self.jobs.get(d["child_id"])
        record = self.fleet.get(d["robot_id"])
        state = record.state if record else None
        if state is None or not job.order_id or state.order_id != job.order_id:
            return
        payloads = {n["action"]["action_id"]: n["action"].get("payload_id")
                    for n in (job.order_snapshot or {}).get("nodes", []) if n.get("action")}
        for action in state.action_states:
            pid = payloads.get(action.action_id)
            if not pid or pid.startswith(TRAY_RETURN_PREFIX) or action.status.value not in ("RUNNING", "FINISHED"):
                continue
            if d["phase"] == "loading":
                self.cargo.observe_action(pid, "pick", action.status.value, action_id=action.action_id)
            elif d["phase"] == "delivering":
                stop = d["stops"][d["index"]]
                self.cargo.observe_action(pid, "drop", action.status.value, action_id=action.action_id,
                                          location=self._where(stop["target"], stop["step"]))

    def _blocked(self, d, payloads):
        """하위 작업이 FINISHED 없이 끝났다. 시작이 관측된 물품은 unknown 으로 둔다."""
        if d.get("bundle_id"):
            self.cargo.mark_uncertain(payloads, f"액션 완료 보고 없이 작업 종료: {d.get('error')}")
            self.cargo.set_bundle_status(d["bundle_id"], "blocked", d.get("error"))

    def _step(self, d):
        phase = d["phase"]
        config = d["config"]
        if phase == "queued":
            # 예약 전에는 설정 수정을 반영한다. 예약한 뒤에는 좌표 스냅샷을 보존한다.
            if self.config is not None:
                config = self.config.model_dump()
            try:
                self._targets(config, d["proposed_items"])
                pickup_name = self._pickup_name(config)
                pickup = resolve_steps([_parse_command({"type": "move", "target": pickup_name})],
                                       config["site_id"], self.registry)[0]
            except (JobError, ValueError, KeyError) as exc:
                d["error"] = str(exc)
                return
            for record in sorted(self.fleet.available_robots(), key=lambda r: r.robot_id):
                a = self.registry.assignment_for(record.robot_id)
                if not a or a.site_id != config["site_id"]:
                    continue
                if any(t["state"] != "empty" for t in self.cargo.robot_cargo(record.robot_id)["trays"]):
                    continue  # 트레이가 비지 않은 로봇은 새 묶음을 받지 않는다
                try:
                    self.coordinator.reserve_robot(record.robot_id, d["job_id"])
                except JobError:
                    continue
                d["robot_id"], d["phase"] = record.robot_id, "to_pickup"
                d["config"], d["pickup"], d["pickup_name"], d["error"] = config, pickup, pickup_name, None
                self._save()
                break
            return
        pickup_name = d.get("pickup_name") or config.get("pickup_target")
        if phase == "to_pickup" or (phase == "returning" and not d.get("bundle_id")):
            # 묶음 확정 전 복귀(적재 전 차단)는 단순 이동이다. 반납할 트레이가 없다.
            if self._child(d, [{"type": "move", "target": pickup_name}], [d["pickup"]]):
                d["child_id"] = None
                if phase == "to_pickup":
                    d["phase"] = "awaiting_bundle"
                else:
                    self._finish_return(d)
                self._save()
                if d["phase"] == "done":
                    self.coordinator.release_robot(d["robot_id"], d["job_id"])
                self._replay(d)
        elif phase == "returning":
            # 픽업 node 로 돌아와 트레이를 반납한다. 반납 DROP FINISHED 가 묶음 종료 근거다.
            payload = f'{TRAY_RETURN_PREFIX}{d["bundle_id"]}'
            if self._action(d, "drop", pickup_name, d["pickup"], [payload]):
                self.cargo.return_trays(d["bundle_id"], self._where(pickup_name, d["pickup"]))
                d["child_id"] = None
                self._finish_return(d)
                self._save()
                self.coordinator.release_robot(d["robot_id"], d["job_id"])
                self._replay(d)
            elif d["phase"] == "blocked":
                self._blocked(d, [])
        elif phase == "loading":
            if not d.get("bundle_id"):
                d["phase"], d["error"] = "blocked", "물품 장부 없는 이전 형식 배송 — 적재물 확인 후 복귀"
                self._save()
                return
            if self._action(d, "pickup", pickup_name, d["pickup"], d["parcels"]):
                for pid in d["parcels"]:
                    self.cargo.observe_action(pid, "pick", "FINISHED", action_id=f'{d["child_id"]}:success')
                # 두 트레이 모두 적재 확인돼야 출발한다.
                missing = [pid for pid in d["parcels"] if self.cargo.parcel(pid)["status"] != "onboard"]
                d["child_id"] = None
                if missing:
                    d["phase"], d["error"] = "blocked", f"적재 확인되지 않은 물품: {', '.join(missing)}"
                    self.cargo.set_bundle_status(d["bundle_id"], "blocked", d["error"])
                else:
                    d["phase"] = "delivering"
                    self.cargo.set_bundle_status(d["bundle_id"], "delivering")
                self._save()
            elif d["phase"] == "blocked":
                self._blocked(d, d["parcels"])
        elif phase == "delivering":
            stop = d["stops"][d["index"]]
            parcels = stop.get("parcels") or []
            if not parcels:
                d["phase"], d["error"] = "blocked", "물품 장부 없는 이전 형식 배송 — 적재물 확인 후 복귀"
                self._save()
                return
            if self._action(d, "drop", stop["target"], stop["step"], parcels):
                where = self._where(stop["target"], stop["step"])
                for pid in parcels:
                    self.cargo.observe_action(pid, "drop", "FINISHED", action_id=f'{d["child_id"]}:success',
                                              location=where)
                for receipt in stop["receipts"]:
                    d["results"][receipt] = "DELIVERED"
                d["index"] += 1
                d["child_id"] = None
                if d["index"] == len(d["stops"]):
                    d["phase"] = "returning"
                    self.cargo.set_bundle_status(d["bundle_id"], "returning")
                self._save()
                self._replay(d)
            elif d["phase"] == "blocked":
                self._blocked(d, parcels)

    def _finish_return(self, d):
        # 남은 물품은 로봇이 보관소에 돌아와 반납한 뒤에만 재입고(FAILED) 처리한다.
        for it in d["items"]:
            d["results"].setdefault(it["receipt_no"], "FAILED")
        d["phase"] = "done"

    def recover_return(self, key, delivered, confirm_remaining_onboard):
        """운영자가 하차 불확실 물품을 대조하고 로봇 복귀를 요청한다."""
        with self.lock:
            d = self.deliveries[key]
            if d["phase"] != "blocked" or not confirm_remaining_onboard:
                raise JobError("차단된 배송의 잔여 적재물을 확인해야 복귀할 수 있다")
            child = self.jobs.get(d["child_id"]) if d["child_id"] else None
            if child and not child.status.terminal:
                raise JobError("수신 불명확 작업은 먼저 취소·종료를 확인해야 한다")
            known = {it["receipt_no"] for it in d["items"]}
            if not set(delivered) <= known:
                raise JobError("이 배송에 없는 접수번호")
            for receipt in delivered:
                d["results"][receipt] = "DELIVERED"
            if d.get("bundle_id"):
                # 장부에도 운영자 확인을 남긴다. 인도 확인 물품은 delivered, 확인 필요 물품은 로봇 적재로 본다.
                bundle = self.cargo.bundle(d["bundle_id"])
                for item in bundle["items"]:
                    status = item["status"]
                    if item["receipt_no"] in delivered and status not in ("delivered",):
                        self.cargo.resolve(item["parcel_id"], "delivered", "배송 복귀 요청 시 인도 확인")
                    elif status in ("unknown", "loading", "unloading"):
                        self.cargo.resolve(item["parcel_id"], "onboard", "배송 복귀 요청 시 잔여 적재물 확인")
                self.cargo.set_bundle_status(d["bundle_id"], "returning")
            d["phase"], d["child_id"], d["error"] = "returning", None, None
            # 이전 복귀 실패와 다른 하위 작업을 만든다.
            d["index"] += 1
            self._save()

    def tick(self):
        with self.lock:
            for _ in range(100):
                try:
                    payload = self._messages.get_nowait()
                except queue.Empty:
                    break
                try:
                    self.receive(payload)
                except Exception:
                    log.exception("보관소 메시지 처리 실패: %s", payload.get("job_id"))
            if not self.mqtt.connected:
                return
            for d in self.deliveries.values():
                if d["phase"] not in ("done", "blocked", "awaiting_bundle"):
                    try:
                        self._observe(d)
                        self._step(d)
                    except Exception as exc:
                        d["phase"], d["error"] = "blocked", str(exc)
                        self._save()
                        log.exception("배송 %s 차단", d["job_id"])
                if d["phase"] == "awaiting_bundle" and time.monotonic() - self._last_send.get(d["job_id"], 0) >= 5:
                    self._replay(d)
            if time.monotonic() - self._last_telemetry >= 10:
                for record in self.fleet.all():
                    if record.state is None or record.liveness(self.fleet.stale_after).value != "ONLINE":
                        continue
                    state = record.state
                    reservation = self.coordinator._reservations.get(record.robot_id)
                    a = self.registry.assignment_for(record.robot_id)
                    self.mqtt.publish_json(f"locker/robot/{record.robot_id}/state", {
                        "robot_id": record.robot_id, "status": "배송 중" if reservation else state.display_state,
                        "location": a.map_id if a else state.map_id, "battery": state.battery.charge,
                        "available": record.available_for_job(self.fleet.stale_after) and not reservation,
                    }, retain=True)
                self._last_telemetry = time.monotonic()

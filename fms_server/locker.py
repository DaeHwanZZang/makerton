"""LastGate 배송 연동. 접수·계정 장부는 보관소, 로봇 예약·운행·적재 장부(cargo)는 FMS가 소유한다.

흐름: 예상 배송(DELIVERY_JOB) → 로봇 예약 → 픽업 node 이동 → ROBOT_ARRIVED
     → 최종 묶음(BUNDLE_CONFIRMED, 물품 2개·트레이 2개) → 물품 ID·묶음 저장 → 칸별 트레이 적재 PICK
     → 세대별 물품별 DROP(빈 트레이는 남음) → 반납 node 이동·칸별 트레이 반납 DROP → 남은 물품 반납 → JOB_DONE.
화면은 이 하위 작업들을 배송 작업 하나로 묶어 보여준다 (delivery_progress.py).
"""
from __future__ import annotations

import copy
import json
import logging
import math
import queue
import threading
import time
from pathlib import Path

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from fms_server.cargo import TRAYS, CargoError, CargoStore, parse_tray_return, tray_return_payload
from fms_server.delivery_progress import delivery_progress
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
    # 트레이 반납 뒤 충전 스테이션 도킹. "auto"(반납 지점에서 가까운 빈 충전소) | "off" | 충전소 목적지 이름
    charger_target: str = Field(default="auto", min_length=1)
    charge_min: float = Field(default=30, ge=0, le=100)   # 도킹 후 이 충전량 이상이어야 다음 작업을 받는다

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
        return self._station_targets(config)[0]

    def _station_targets(self, config: dict) -> tuple[str, str]:
        """(픽업 node, 반납 node) 목적지 이름. 픽업스테이션이면 두 node 연결이 정상인지 확인한다.

        이전 방식(pickup_target 일반 목적지)은 같은 곳에서 반납한다.
        """
        station_id = config.get("pickup_station_id")
        if not station_id:
            return config["pickup_target"], config["pickup_target"]
        from fms_server.pickup_stations import PickupStationError, get_station
        site = config["site_id"]
        try:
            station = get_station(self.registry.map_store(site), self.registry.location_store(site), station_id)
        except PickupStationError as exc:
            raise JobError(str(exc)) from None
        if not station.ok:
            raise JobError(f"픽업스테이션 연결 확인 필요: {', '.join(station.problems)}")
        return station.name, station.return_name

    def configure(self, config):
        with self.lock:
            self.registry.get_site(config.site_id)
            locations = self.registry.location_store(config.site_id)
            pickup = self._pickup_name(config.model_dump())
            for name in {pickup, *config.households.values()}:
                locations.resolve_pose(name)
            if config.charger_target not in ("auto", "off") and config.charger_target not in {
                    c["name"] for c in self._chargers(config.site_id)}:
                raise JobError(f"충전소 {config.charger_target!r} 가 없거나 맵의 물리 충전기와 맞지 않는다")
            _write(self.config_path, config.model_dump())
            self.config = config

    def snapshot(self):
        with self.lock:
            deliveries = copy.deepcopy(list(self.deliveries.values()))
            for d in deliveries:
                d["bundle"] = self.cargo.bundle(d["bundle_id"]) if d.get("bundle_id") else None
                d["progress"] = delivery_progress(d, self.jobs, self.cargo)
            return {"config": self.config.model_dump() if self.config else None,
                    "deliveries": deliveries}

    def _save(self):
        # 단계 전환 시각을 남긴다. 진행 상세 창의 타임라인에 쓴다.
        now = time.time()
        for d in self.deliveries.values():
            mark = f'{d["phase"]}:{d.get("index", 0)}'
            timeline = d.setdefault("timeline", [])
            if not timeline or timeline[-1]["mark"] != mark:
                timeline.append({"mark": mark, "phase": d["phase"], "index": d.get("index", 0), "at": now})
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
        if d["phase"] == "canceled":
            if d.get("locker_cancel"):
                return []   # 보관소가 먼저 취소했다 (JOB_CANCELLED). 보낼 것이 없다.
            # 로봇 배정 전 운영자 취소. 보관소가 DELIVERY_JOB 을 다시 보내면 같은 취소를 다시 알린다.
            return [{"type": "JOB_REJECTED", "job_id": d["job_id"], "reason": d.get("error") or "",
                     "unroutable": d.get("unroutable") or []}]
        if d["phase"] == "awaiting_bundle":
            return [{"type": "ROBOT_ARRIVED", "job_id": d["job_id"], "robot_id": d["robot_id"]}]
        events = [{"type": "DELIVERY_RESULT", "job_id": d["job_id"],
                   "receipt_no": receipt, "result": result}
                  for receipt, result in d["results"].items()]
        if d["phase"] in ("docking", "done"):
            # 보관소 기준 배송은 트레이 반납으로 끝난다. 도킹 중에도 로봇 예약은 유지한다.
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
    def _tray_order(items):
        """트레이 순(TRAY_A → TRAY_B) = 방문 순서. 트레이가 없거나 모르는 값이면 받은 순서를 유지한다."""
        rank = {tray: i for i, tray in enumerate(TRAYS)}
        return [it for _, it in sorted(enumerate(items), key=lambda p: (rank.get(p[1].get("tray"), len(TRAYS)), p[0]))]

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
                    # 방문 순서 = 트레이 순서 (보관소가 정한다). 같은 세대에 가는 물품은 이동을 공유하지만
                    # DROP 은 물품마다 따로 한다. 세대는 처음 나온 트레이 순서로 들른다.
                    target_of = {t["receipt_no"]: t for t in targets}
                    groups = {}
                    for it in self._tray_order(d["items"]):
                        target = target_of[it["receipt_no"]]
                        stop = groups.setdefault(target["household"], {**target, "receipts": [], "parcels": []})
                        stop["receipts"].append(target["receipt_no"])
                        stop["parcels"].append(parcel_of[target["receipt_no"]])
                    d["stops"] = list(groups.values())
                    d["phase"], d["child_id"] = "loading", None
                except (JobError, CargoError, ValueError, KeyError) as exc:
                    d["phase"], d["error"] = "blocked", str(exc)
                self._save()
            elif kind == "JOB_CANCELLED" and d:
                self._locker_cancel(d, str(payload.get("reason") or ""))

    def _locker_cancel(self, d, reason):
        """보관소가 적재 전에 배송을 취소했다 (도착 때 2개를 못 채움 NOT_ENOUGH·NO_ITEMS, 도착 전 EMPTIED).

        - 로봇 배정 전: 배송을 끝낸다 (보관소에 보낼 것이 없다).
        - 픽업 지점으로 이동 중: 이동을 취소하고 충전 스테이션으로 보낸다.
        - 픽업 지점 도착 후(묶음 대기): 실은 것이 없으므로 반납 없이 충전 스테이션으로 보낸다.
        - 적재를 시작한 뒤에는 무시한다 (보관소는 적재 확정 뒤 취소하지 않는다).
        """
        phase = d["phase"]
        if phase not in ("queued", "to_pickup", "awaiting_bundle"):
            return
        d["locker_cancel"] = reason or "취소"
        if phase == "queued":
            d["phase"], d["error"] = "canceled", f"보관소 취소: {d['locker_cancel']}"
            if d.get("robot_id"):
                self.coordinator.release_robot(d["robot_id"], d["job_id"])
            self._save()
            return
        if phase == "to_pickup" and d.get("child_id"):
            child = self.jobs.get(d["child_id"])
            if not child.status.terminal and child.status != JobStatus.CANCELING:
                try:
                    self.coordinator.cancel(child.job_id)
                except JobError:
                    pass   # 그 사이 끝났다. 도킹 작업 배정은 로봇이 비면 된다.
        d["phase"], d["child_id"], d["error"] = "docking", None, None
        self._save()
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
            if not pid or action.status.value not in ("RUNNING", "FINISHED"):
                continue
            tray_return = parse_tray_return(pid)
            if tray_return:
                if d["phase"] == "returning":
                    self.cargo.observe_tray_return(*tray_return, action.status.value, action_id=action.action_id,
                                                   location=self._where(d.get("return_name"), d["return_step"]))
                continue
            if d["phase"] == "loading":
                self.cargo.observe_action(pid, "pick", action.status.value, action_id=action.action_id)
            elif d["phase"] == "delivering":
                stop = d["stops"][d["index"]]
                self.cargo.observe_action(pid, "drop", action.status.value, action_id=action.action_id,
                                          location=self._where(stop["target"], stop["step"]))

    def _blocked(self, d, payloads, *, returning=False):
        """하위 작업이 FINISHED 없이 끝났다. 시작이 관측된 물품·반납 중이던 칸은 unknown 으로 둔다."""
        if d.get("bundle_id"):
            reason = f"액션 완료 보고 없이 작업 종료: {d.get('error')}"
            self.cargo.mark_uncertain(payloads, reason)
            if returning:
                self.cargo.mark_trays_uncertain(d["bundle_id"], reason)
            self.cargo.set_bundle_status(d["bundle_id"], "blocked", d.get("error"))

    def _return_target(self, d):
        """반납 node (이름, 좌표 스냅샷). 예약 때 저장하며, 이전 레코드는 지금 설정에서 구한다."""
        if not d.get("return_step"):
            _, name = self._station_targets(d["config"])
            d["return_name"] = name
            d["return_step"] = resolve_steps([_parse_command({"type": "move", "target": name})],
                                             d["config"]["site_id"], self.registry)[0]
            self._save()
        return d["return_name"], d["return_step"]

    def _step(self, d):
        phase = d["phase"]
        config = d["config"]
        if phase == "queued":
            # 예약 전에는 설정 수정을 반영한다. 예약한 뒤에는 좌표 스냅샷을 보존한다.
            if self.config is not None:
                config = self.config.model_dump()
            try:
                self._targets(config, d["proposed_items"])
                pickup_name, return_name = self._station_targets(config)
                pickup, return_step = (resolve_steps([_parse_command({"type": "move", "target": name})],
                                                     config["site_id"], self.registry)[0]
                                       for name in (pickup_name, return_name))
            except (JobError, ValueError, KeyError) as exc:
                d["error"] = str(exc)
                return
            for record in sorted(self.fleet.available_robots(), key=lambda r: r.robot_id):
                a = self.registry.assignment_for(record.robot_id)
                if not a or a.site_id != config["site_id"]:
                    continue
                if any(t["state"] != "none" for t in self.cargo.robot_cargo(record.robot_id)["trays"]):
                    continue  # 트레이를 반납하지 않은 로봇은 새 묶음을 받지 않는다
                try:
                    self.coordinator.reserve_robot(record.robot_id, d["job_id"])
                except JobError:
                    continue
                d["robot_id"], d["phase"] = record.robot_id, "to_pickup"
                d["config"], d["pickup"], d["pickup_name"], d["error"] = config, pickup, pickup_name, None
                d["return_name"], d["return_step"] = return_name, return_step
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
                self._replay(d)
                if d["phase"] == "docking":
                    self._dock(d)
        elif phase == "returning":
            # 반납 node 로 가서 칸마다 빈 트레이(미배송 물품이 있으면 담긴 채)를 내려놓는다.
            # 칸별 반납 DROP FINISHED 가 트레이 반납·묶음 종료 근거다.
            try:
                return_name, return_step = self._return_target(d)
            except (JobError, ValueError, KeyError) as exc:
                d["phase"], d["error"] = "blocked", f"반납 지점을 찾을 수 없다: {exc}"
                self.cargo.set_bundle_status(d["bundle_id"], "blocked", d["error"])
                self._save()
                return
            if not d.get("return_trays"):
                d["return_trays"] = self.cargo.trays_to_return(d["bundle_id"])
                self._save()
            if not d["return_trays"]:
                self.cargo.finish_without_trays(d["bundle_id"])
                done = True
            else:
                payloads = [tray_return_payload(d["bundle_id"], t) for t in d["return_trays"]]
                done = self._action(d, "drop", return_name, return_step, payloads)
                if done:
                    where = self._where(return_name, return_step)
                    for tray in d["return_trays"]:
                        self.cargo.observe_tray_return(d["bundle_id"], tray, "FINISHED",
                                                       action_id=f'{d["child_id"]}:success', location=where)
            if done:
                d["child_id"] = None
                self._finish_return(d)
                self._save()
                self._replay(d)
                self._dock(d)   # 같은 tick 에 충전 스테이션 도킹 작업을 만든다
            elif d["phase"] == "blocked":
                self._blocked(d, [], returning=True)
        elif phase == "docking":
            self._dock(d)
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
        # 보관소에는 여기서 JOB_DONE 을 보내고, 로봇은 충전 스테이션 도킹까지 같은 배송으로 예약을 유지한다.
        for it in d["items"]:
            d["results"].setdefault(it["receipt_no"], "FAILED")
        d["phase"] = "docking"

    def _finish(self, d):
        # 결과·JOB_DONE 은 도킹 단계에 들어갈 때 이미 보냈다. 여기서는 로봇 예약만 푼다.
        d["phase"], d["child_id"] = "done", None
        self._save()
        self.coordinator.release_robot(d["robot_id"], d["job_id"])

    DOCK_UNDOCK_DISTANCE = 0.6   # 충전기 진입 직선 거리 (m). 로봇은 이만큼 떨어진 진입 지점까지 경로계획으로 간다

    def _chargers(self, site_id):
        """도킹 가능한 충전소: type=charger 목적지 중 맵 chargers.json 의 물리 충전기와 위치·방향이 맞는 것.

        approach_free: 진입 지점(FORWARD 면 충전기 뒤쪽, BACKWARD 면 앞쪽 DOCK_UNDOCK_DISTANCE)이 주행 가능한 셀인지.
        막혀 있으면 로봇이 NO_PATH 로 실패하므로 고르지 않는다 (보통 진입 방향 설정이 반대인 경우).
        """
        import json as _json
        from fms_server.mapkit.chargers import read_chargers
        maps, locations = self.registry.map_store(site_id), self.registry.location_store(site_id)
        physical, models = {}, {}
        out = []
        for loc in locations.list():
            if loc.type != "charger":
                continue
            if loc.map_id not in physical:
                try:
                    physical[loc.map_id] = read_chargers(_json.loads(maps.file_path(loc.map_id, "chargers.json").read_text()))
                except Exception:
                    physical[loc.map_id] = []
            for c in physical[loc.map_id]:
                delta = math.atan2(math.sin(c.theta - loc.theta), math.cos(c.theta - loc.theta))
                if math.hypot(c.x - loc.x, c.y - loc.y) <= .3 and abs(delta) <= .35:
                    entry = c.entry or "FORWARD"
                    sign = -1.0 if entry == "FORWARD" else 1.0
                    ax = loc.x + sign * self.DOCK_UNDOCK_DISTANCE * math.cos(loc.theta)
                    ay = loc.y + sign * self.DOCK_UNDOCK_DISTANCE * math.sin(loc.theta)
                    try:
                        if loc.map_id not in models:
                            models[loc.map_id] = maps.load_map_model(loc.map_id)
                        free = models[loc.map_id].in_bounds(ax, ay) and models[loc.map_id].is_free(ax, ay)
                    except Exception:
                        free = False
                    out.append({"name": loc.name, "map_id": loc.map_id, "x": loc.x, "y": loc.y,
                                "entry": entry, "approach_free": free})
                    break
        return out

    def _busy_chargers(self, d):
        """다른 작업·배송이 향하거나 다른 로봇이 서 있는 충전소 이름."""
        busy = {o.get("charger") for o in self.deliveries.values()
                if o is not d and o["phase"] == "docking" and o.get("charger")}
        for job in self.jobs.list(d["config"]["site_id"]):
            if job.robot_id != d["robot_id"] and not job.status.terminal:
                busy.update(c.target for c in job.commands if getattr(c, "kind", None) == "docking")
        return busy

    def _occupied(self, charger, robot_id):
        for record in self.fleet.all():
            a = self.registry.assignment_for(record.robot_id)
            if record.robot_id == robot_id or record.state is None or not a or a.map_id != charger["map_id"]:
                continue
            pose = record.state.pose
            if math.hypot(pose.x - charger["x"], pose.y - charger["y"]) < 0.5:
                return True
        return False

    def _choose_charger(self, d):
        mode = d["config"].get("charger_target", "auto")
        if mode == "off":
            return None, "도킹 안 함 (설정)"
        chargers = self._chargers(d["config"]["site_id"])
        blocked = [c["name"] for c in chargers if not c["approach_free"]]
        blocked_note = (f"충전소 진입 지점이 벽·장애물 안: {', '.join(blocked)} "
                        f"(충전기 진입 방향 FORWARD/BACKWARD 확인)") if blocked else None
        if mode != "auto":
            chosen = [c for c in chargers if c["name"] == mode]
            if not chosen:
                return None, f"충전소 {mode!r} 를 찾을 수 없거나 물리 충전기와 맞지 않음"
            if not chosen[0]["approach_free"]:
                return None, blocked_note
            return chosen[0], None
        busy = self._busy_chargers(d)
        free = [c for c in chargers if c["approach_free"] and c["name"] not in busy
                and not self._occupied(c, d["robot_id"])]
        if not free:
            if not chargers:
                return None, "등록된 충전 스테이션 없음"
            return None, blocked_note if len(blocked) == len(chargers) else "빈 충전 스테이션 없음"
        origin = d.get("return_step") or d.get("pickup")
        ox, oy = origin["pose"]["x"], origin["pose"]["y"]
        # 반납 지점과 같은 층을 먼저, 그 안에서는 직선거리 순.
        free.sort(key=lambda c: (c["map_id"] != origin["map_id"], math.hypot(c["x"] - ox, c["y"] - oy), c["name"]))
        return free[0], None

    def _dock(self, d):
        """트레이 반납 뒤 충전 스테이션 도킹. 실패해도 물품 결과는 이미 확정이므로 배송을 차단하지 않는다."""
        if not d.get("charger_step"):
            charger, note = self._choose_charger(d)
            if charger is None:
                d["dock_note"] = note
                self._finish(d)
                return
            try:
                step = resolve_steps([_parse_command({"type": "move", "target": charger["name"]})],
                                     d["config"]["site_id"], self.registry)[0]
            except (JobError, ValueError, KeyError) as exc:
                d["dock_note"] = f"충전소 좌표 확인 실패: {exc}"
                self._finish(d)
                return
            d["charger"], d["charger_entry"], d["charger_step"] = charger["name"], charger["entry"], step
            self._save()
        commands = [{"type": "docking", "target": d["charger"], "params": {
            "entry": d["charger_entry"], "undock_distance": self.DOCK_UNDOCK_DISTANCE, "speed": 0.2,
            "min_charge": d["config"].get("charge_min", 30)}}]
        if self._child(d, commands, [d["charger_step"]]):
            self._finish(d)
        elif d["phase"] == "blocked":
            # 도킹 실패·취소. 로봇 상태(DOCK_FAILED 등)는 로봇 화면에서 확인한다.
            d["dock_note"], d["error"] = f"충전 도킹 실패: {d['error']}", None
            self._finish(d)

    def _unroutable(self, d, config):
        """이 배송의 예상 물품 중 지금 설정으로 갈 수 없는 물품(세대 연결 없음·목적지 삭제)의 접수번호."""
        locations = self.registry.location_store(config["site_id"])
        out = []
        for it in d.get("proposed_items") or []:
            target = config["households"].get(f'{it["dong"]}-{it["ho"]}')
            try:
                if not target:
                    raise KeyError(target)
                locations.resolve_pose(target)
            except Exception:
                out.append(it["receipt_no"])
        return out

    def cancel_queued(self, key, reason=""):
        """로봇 배정 전(배정 대기) 배송을 운영자가 취소한다. 보관소에 JOB_REJECTED 를 보낸다.

        갈 수 없는 세대 물품은 보관소가 직접 수령으로 바꾸고 나머지로 새 배송을 만든다.
        로봇을 예약한 뒤의 배송은 기존 절차(현재 단계 취소 → 적재물 대조 → 복귀)를 쓴다.
        """
        with self.lock:
            d = self.deliveries[key]
            if d["phase"] != "queued" or d.get("robot_id"):
                raise JobError("로봇 배정 전(배정 대기) 배송만 취소할 수 있다")
            config = self.config.model_dump() if self.config is not None else d["config"]
            d["unroutable"] = self._unroutable(d, config)
            detail = reason.strip() or d.get("error") or "운영자 취소"
            d["phase"], d["error"] = "canceled", f"운영자 취소: {detail}"
            self._save()
            self._replay(d)
            return d["unroutable"]

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
                self.cargo.restore_trays(d["bundle_id"])
                self.cargo.set_bundle_status(d["bundle_id"], "returning")
            d["phase"], d["child_id"], d["error"] = "returning", None, None
            d.pop("return_trays", None)  # 반납할 칸을 다시 계산한다
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
                if d["phase"] not in ("done", "blocked", "awaiting_bundle", "canceled"):
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

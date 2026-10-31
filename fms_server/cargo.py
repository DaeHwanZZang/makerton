"""
FMS 물품 장부 — 물품 ID·배송 묶음·트레이·적재 상태
==================================================

LastGate 가 접수번호(receipt_no)·계정·수령 방식의 원본이라면, FMS 는 로봇에 무엇이 실렸는지의 원본이다.

    {data_root}/cargo.json
      sources : "<locker_id>|<receipt_no>" -> parcel_id     (같은 물품은 재시도·재시작에도 같은 ID)
      parcels : parcel_id -> 물품 레코드 (상태·트레이·위치·이력)
      bundles : bundle_id -> 물품 2개 묶음 (로봇·스테이션·트레이 배정·진행)

상태 (parcel.status)
    planned    이번 적재 대상. 로봇에 실렸다고 확정하지 않음
    loading    PICK RUNNING
    onboard    PICK FINISHED — 로봇 트레이에 있음
    unloading  DROP RUNNING
    delivered  세대 DROP FINISHED — 작업 위치 바닥에 내려놓음
    returned   트레이 반납(보관소 픽업 node 의 반납 DROP FINISHED) 때 로봇에 남아 있던 물품
    canceled   적재되지 않고 끝난 계획
    unknown    적재·하차 결과를 확정할 수 없음 (운영자 확정 필요)

전이 근거는 로봇이 보고한 액션 상태다. RUNNING 을 놓쳐도 일치하는 FINISHED 는 완료 증거로 쓴다.
FINISHED 없이 액션이 끝나면 unknown 이다. 통신 단절만으로 물품을 없애지 않는다.
PICK/DROP 은 C++ 시뮬레이터의 시간 기반 액션이다. 실제 적재 센서로 확인한 상태가 아니다.

트레이는 탈착식이다. TRAY_A / TRAY_B 는 로봇의 트레이 거치 칸 이름이다. 픽업 node 에서 물품이 담긴
트레이를 칸에 싣고(PICK), 세대에서 물품만 내려놓고(DROP, 빈 트레이는 남음), 반납 node 에서 빈 트레이를
칸마다 내려놓는다(반납 DROP, payload_id = TRAY_RETURN:{bundle_id}:{칸}). 물리 트레이 개체 ID 는 추적하지 않는다.

칸 상태 (bundle.trays[칸].state)
    none        트레이 없음 (반납 완료 또는 실은 적 없음)
    reserved    이번 묶음에 배정, 아직 안 실음
    loading     트레이 적재 중 (PICK RUNNING)
    loaded      물품 담긴 트레이 실림 (PICK FINISHED)
    unloading   물품 하차 중 (DROP RUNNING)
    empty_tray  빈 트레이 실림 (DROP FINISHED)
    returning   트레이 반납 중 (반납 DROP RUNNING)
    unknown     확인 필요
두 칸이 모두 none 이어야 새 묶음을 받는다. 배송을 다 마쳐도 반납 전에는 받지 않는다.
"""

from __future__ import annotations

import copy
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

log = logging.getLogger("fms.cargo")

CARGO_FILE = "cargo.json"
TRAYS = ("TRAY_A", "TRAY_B")
TRAY_RETURN_PREFIX = "TRAY_RETURN:"

TERMINAL = ("delivered", "returned", "canceled")
ACTIVE = ("planned", "loading", "onboard", "unloading", "unknown")
# 칸에 트레이가 실려 있거나 결과가 불확실한 상태 (반납 대상)
TRAY_ON_ROBOT = ("loading", "loaded", "unloading", "empty_tray", "returning", "unknown")
_TRAY_FROM_PARCEL = {"planned": "reserved", "loading": "loading", "onboard": "loaded",
                     "unloading": "unloading", "delivered": "empty_tray", "returned": "none",
                     "canceled": "none", "unknown": "unknown"}


def tray_return_payload(bundle_id: str, tray: str) -> str:
    return f"{TRAY_RETURN_PREFIX}{bundle_id}:{tray}"


def parse_tray_return(payload: str) -> Optional[tuple[str, str]]:
    if not payload.startswith(TRAY_RETURN_PREFIX):
        return None
    bundle_id, _, tray = payload[len(TRAY_RETURN_PREFIX):].rpartition(":")
    return (bundle_id, tray) if bundle_id and tray in TRAYS else None

# 허용 전이. 운영자 확정(resolve)은 unknown/onboard/planned 에서 별도 표로 검사한다.
_ALLOWED = {
    "planned": {"loading", "onboard", "unknown", "canceled"},
    "loading": {"onboard", "unknown"},
    "onboard": {"unloading", "delivered", "returned", "unknown"},
    "unloading": {"delivered", "unknown"},
    "unknown": set(),
}
RESOLVE_RESULTS = {
    # 운영자가 실물을 확인해 기록한다. 결과 -> 새 상태
    "onboard": "onboard",        # 로봇 트레이에 있다
    "delivered": "delivered",    # 세대에 인도했다
    "returned": "returned",      # 보관소에 돌려놓았다
    "not_loaded": "canceled",    # 실리지 않았다 (보관소에 그대로)
}


class CargoError(Exception):
    """요청이 잘못됨 (HTTP 422 급)."""


class CargoNotFound(CargoError):
    """물품·묶음 없음 (HTTP 404 급)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CargoStore:
    """cargo.json 하나에 물품·묶음을 저장한다. 프로세스 하나 기준 RLock 으로 보호한다."""

    def __init__(self, data_root) -> None:
        self._path = Path(data_root) / CARGO_FILE
        self._lock = threading.RLock()
        self._data = {"sources": {}, "parcels": {}, "bundles": {}}
        if self._path.is_file():
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            for key in self._data:
                self._data[key] = raw.get(key, {})

    @property
    def lock(self):
        return self._lock

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self._path)

    # -- 조회 ------------------------------------------------------------

    def parcel(self, parcel_id: str) -> dict:
        with self._lock:
            parcel = self._data["parcels"].get(parcel_id)
            if parcel is None:
                raise CargoNotFound(f"물품 {parcel_id!r} 없음")
            return copy.deepcopy(parcel)

    def bundle(self, bundle_id: str) -> dict:
        with self._lock:
            bundle = self._data["bundles"].get(bundle_id)
            if bundle is None:
                raise CargoNotFound(f"배송 묶음 {bundle_id!r} 없음")
            return self._bundle_view(bundle)

    def parcels(self, site_id: Optional[str] = None, status: Optional[str] = None) -> list[dict]:
        with self._lock:
            rows = [p for p in self._data["parcels"].values()
                    if (site_id is None or p["site_id"] == site_id)
                    and (status is None or p["status"] == status)]
            return copy.deepcopy(sorted(rows, key=lambda p: p["created_at"]))

    def bundles(self, site_id: Optional[str] = None) -> list[dict]:
        with self._lock:
            rows = [b for b in self._data["bundles"].values() if site_id is None or b["site_id"] == site_id]
            return [self._bundle_view(b) for b in sorted(rows, key=lambda b: b["created_at"])]

    def _bundle_view(self, bundle: dict) -> dict:
        view = copy.deepcopy(bundle)
        parcels = [self._data["parcels"][i["parcel_id"]] for i in bundle["items"]]
        for item, parcel in zip(view["items"], parcels):
            item["status"] = parcel["status"]
        delivered = sum(1 for p in parcels if p["status"] == "delivered")
        view["delivered"] = delivered
        view["progress"] = f"{delivered}/{len(parcels)}"
        if bundle["status"] == "closed":
            view["summary"] = ("배송 완료" if delivered == len(parcels)
                               else "일부 배송" if delivered else "미배송 반납")
        else:
            view["summary"] = {"loading": "적재 중", "delivering": "배송 중", "returning": "트레이 반납 이동",
                               "blocked": "확인 필요", "canceled": "취소"}.get(bundle["status"], bundle["status"])
        return view

    def robot_cargo(self, robot_id: str) -> dict:
        """로봇 두 칸의 트레이·물품. 열린 묶음이 없으면 두 칸 모두 트레이 없음."""
        with self._lock:
            bundle = next((b for b in self._data["bundles"].values()
                           if b["robot_id"] == robot_id and b["status"] not in ("closed", "canceled")), None)
            trays = []
            for tray in TRAYS:
                slot = (bundle or {}).get("trays", {}).get(tray) or {"state": "none", "parcel_id": None}
                parcel = self._data["parcels"].get(slot.get("parcel_id")) if slot.get("parcel_id") else None
                show = slot["state"] not in ("none", "empty_tray")
                trays.append({"tray_id": tray, "state": slot["state"],
                              "parcel": copy.deepcopy(parcel) if parcel and show else None,
                              "last_parcel_id": slot.get("parcel_id")})
            return {"robot_id": robot_id, "bundle": self._bundle_view(bundle) if bundle else None,
                    "trays": trays}

    def active_for_station(self, station_id: str) -> list[str]:
        """스테이션을 아직 쓰는 물품. 진행 중 물품과, 트레이를 반납하지 않은 묶음(빈 트레이 포함)의 물품."""
        with self._lock:
            ids = {p["parcel_id"] for p in self._data["parcels"].values()
                   if p.get("pickup_station_id") == station_id and p["status"] in ACTIVE}
            for bundle in self._data["bundles"].values():
                if (bundle.get("pickup_station_id") == station_id
                        and any(t["state"] != "none" for t in bundle.get("trays", {}).values())):
                    ids.update(i["parcel_id"] for i in bundle["items"])
            return sorted(ids)

    # -- 묶음 생성 ---------------------------------------------------------

    def open_bundle(self, *, site_id: str, delivery_job_id: str, robot_id: str, locker_id: str,
                    station_id: Optional[str], station_name: str, items: list[dict]) -> dict:
        """최종 적재 목록으로 물품 ID·묶음·트레이 배정을 저장한다. PICK 발행 전에 호출한다.

        items: [{receipt_no, dong, ho, size, tray, household, target, step}]
        같은 배송(delivery_job_id)의 열린 묶음이 이미 있으면 그대로 돌려준다 (재시작·중복 확정).
        """
        with self._lock:
            for b in self._data["bundles"].values():
                if b["delivery_job_id"] == delivery_job_id and b["status"] not in ("closed", "canceled"):
                    return self._bundle_view(b)
            if len(items) != len(TRAYS):
                raise CargoError(f"배송 묶음은 물품 {len(TRAYS)}개여야 한다 (받은 수 {len(items)})")
            if sorted(str(i.get("tray") or "") for i in items) != sorted(TRAYS):
                raise CargoError(f"트레이는 {', '.join(TRAYS)}에 하나씩 배정해야 한다")
            if len({i["receipt_no"] for i in items}) != len(items):
                raise CargoError("같은 물품이 두 트레이에 배정되었다")
            busy = [t["tray_id"] for t in self.robot_cargo(robot_id)["trays"] if t["state"] != "none"]
            if busy:
                raise CargoError(f"로봇 {robot_id} 칸 {', '.join(busy)}에 트레이가 반납되지 않았다")
            bundle_id = f"B-{uuid4().hex}"
            now = _now()
            entries = []
            for item in items:
                key = f"{locker_id}|{item['receipt_no']}"
                parcel_id = self._data["sources"].get(key)
                parcel = self._data["parcels"].get(parcel_id) if parcel_id else None
                if parcel is not None and parcel["status"] in ACTIVE:
                    raise CargoError(f"물품 {parcel_id}({item['receipt_no']})가 다른 배송에서 진행 중이다")
                if parcel is None:
                    parcel_id = f"P-{uuid4().hex}"
                    self._data["sources"][key] = parcel_id
                    parcel = {"parcel_id": parcel_id, "site_id": site_id, "created_at": now,
                              "source": {"locker_id": locker_id, "receipt_no": item["receipt_no"]},
                              "history": [], "attempts": 0}
                    self._data["parcels"][parcel_id] = parcel
                parcel.update({
                    "status": "planned", "dong": item["dong"], "ho": item["ho"], "size": item.get("size"),
                    "household": item["household"], "assigned_robot_id": robot_id, "onboard_robot_id": None,
                    "pickup_station_id": station_id, "pickup_station": station_name,
                    "delivery_job_id": delivery_job_id, "bundle_id": bundle_id, "tray_id": item["tray"],
                    "drop_target": item["target"], "drop_step": item["step"], "drop_position": None,
                    "last_confirmed_location": {"kind": "locker", "locker_id": locker_id},
                    "updated_at": now,
                })
                parcel["attempts"] += 1
                parcel["history"].append({"at": now, "from": None, "to": "planned",
                                          "reason": f"묶음 {bundle_id} {item['tray']} 배정"})
                entries.append({"parcel_id": parcel_id, "receipt_no": item["receipt_no"],
                                "tray_id": item["tray"], "household": item["household"],
                                "target": item["target"]})
            bundle = {"bundle_id": bundle_id, "site_id": site_id, "delivery_job_id": delivery_job_id,
                      "robot_id": robot_id, "pickup_station_id": station_id, "pickup_station": station_name,
                      "status": "loading", "items": entries, "created_at": now, "updated_at": now,
                      "closed_at": None, "error": None,
                      "trays": {e["tray_id"]: {"state": "reserved", "parcel_id": e["parcel_id"]} for e in entries}}
            self._data["bundles"][bundle_id] = bundle
            self._save()
            log.info("배송 묶음 %s: 로봇 %s %s", bundle_id, robot_id,
                     ", ".join(f"{e['tray_id']}={e['parcel_id']}({e['receipt_no']})" for e in entries))
            return self._bundle_view(bundle)

    def set_bundle_status(self, bundle_id: str, status: str, error: Optional[str] = None) -> None:
        with self._lock:
            bundle = self._data["bundles"].get(bundle_id)
            if bundle is None or bundle["status"] in ("closed", "canceled"):
                return
            if bundle["status"] == status and bundle.get("error") == error:
                return
            bundle["status"], bundle["error"], bundle["updated_at"] = status, error, _now()
            self._save()

    # -- 액션 보고 반영 ------------------------------------------------------

    def _move(self, parcel: dict, to: str, reason: str, *, action_id: Optional[str] = None,
              location: Optional[dict] = None) -> bool:
        current = parcel["status"]
        if current == to:
            return False
        if to not in _ALLOWED.get(current, set()):
            log.warning("물품 %s 전이 무시: %s -> %s (%s)", parcel["parcel_id"], current, to, reason)
            return False
        now = _now()
        parcel["status"], parcel["updated_at"] = to, now
        if to == "onboard":
            parcel["onboard_robot_id"] = parcel["assigned_robot_id"]
            parcel["last_confirmed_location"] = {"kind": "robot", "robot_id": parcel["assigned_robot_id"],
                                                 "tray_id": parcel["tray_id"]}
        elif to == "delivered":
            parcel["onboard_robot_id"] = None
            parcel["drop_position"] = location
            parcel.pop("floor_cleared_at", None)
            parcel["last_confirmed_location"] = {"kind": "floor", **(location or {})}
        elif to == "returned":
            parcel["onboard_robot_id"] = None
            parcel["last_confirmed_location"] = {"kind": "station", "station_id": parcel.get("pickup_station_id"),
                                                 "name": parcel.get("pickup_station"), **(location or {})}
        elif to == "unknown":
            parcel["candidates"] = {"robot_id": parcel["assigned_robot_id"], "tray_id": parcel["tray_id"]}
        parcel["history"].append({"at": now, "from": current, "to": to, "reason": reason,
                                  **({"action_id": action_id} if action_id else {})})
        log.info("물품 %s %s -> %s (%s)", parcel["parcel_id"], current, to, reason)
        return True

    def observe_action(self, parcel_id: str, kind: str, status: str, *, action_id: str,
                       location: Optional[dict] = None) -> None:
        """PICK/DROP 액션 상태 보고를 물품 상태에 반영한다. kind = pick | drop."""
        with self._lock:
            parcel = self._data["parcels"].get(parcel_id)
            if parcel is None:
                return
            if kind == "pick":
                target = {"RUNNING": "loading", "FINISHED": "onboard"}.get(status)
            else:
                target = {"RUNNING": "unloading", "FINISHED": "delivered"}.get(status)
            if target and self._move(parcel, target, f"{kind.upper()} {status}", action_id=action_id,
                                     location=location):
                self._sync_slot(parcel)
                self._save()

    def _slot(self, parcel: dict) -> Optional[dict]:
        bundle = self._data["bundles"].get(parcel.get("bundle_id"))
        return (bundle or {}).get("trays", {}).get(parcel.get("tray_id")) if bundle else None

    def _sync_slot(self, parcel: dict) -> None:
        """물품 상태에서 칸 상태를 정한다 (적재·하차 단계). 반납 단계는 observe_tray_return 이 다룬다."""
        slot = self._slot(parcel)
        if slot is not None and slot["state"] not in ("returning", "none"):
            slot["state"] = _TRAY_FROM_PARCEL[parcel["status"]]

    def mark_uncertain(self, parcel_ids: list[str], reason: str) -> list[str]:
        """FINISHED 없이 끝난 액션. 시작이 관측된 물품(loading/unloading)만 unknown 으로 둔다."""
        changed = []
        with self._lock:
            for pid in parcel_ids:
                parcel = self._data["parcels"].get(pid)
                if parcel and parcel["status"] in ("loading", "unloading"):
                    self._move(parcel, "unknown", reason)
                    self._sync_slot(parcel)
                    changed.append(pid)
            if changed:
                self._save()
        return changed

    def trays_to_return(self, bundle_id: str) -> list[str]:
        """반납 액션이 필요한 칸 (트레이가 실렸거나 불확실한 칸)."""
        with self._lock:
            bundle = self._data["bundles"].get(bundle_id)
            if bundle is None:
                raise CargoNotFound(f"배송 묶음 {bundle_id!r} 없음")
            return [t for t in TRAYS if bundle["trays"][t]["state"] in TRAY_ON_ROBOT]

    def observe_tray_return(self, bundle_id: str, tray: str, status: str, *, action_id: str,
                            location: Optional[dict] = None) -> None:
        """반납 node 의 칸별 반납 DROP. FINISHED 면 칸을 비우고 실려 있던 미배송 물품은 반납으로 둔다."""
        with self._lock:
            bundle = self._data["bundles"].get(bundle_id)
            if bundle is None or tray not in bundle.get("trays", {}):
                return
            slot = bundle["trays"][tray]
            if status == "RUNNING" and slot["state"] in TRAY_ON_ROBOT and slot["state"] != "returning":
                slot["before_return"], slot["state"] = slot["state"], "returning"
            elif status == "FINISHED" and slot["state"] != "none":
                slot["state"] = "none"
                parcel = self._data["parcels"].get(slot.get("parcel_id"))
                if parcel and parcel["status"] == "onboard":
                    self._move(parcel, "returned", f"{tray} 트레이 반납", action_id=action_id, location=location)
                elif parcel and parcel["status"] == "planned":
                    self._move(parcel, "canceled", "적재되지 않은 채 반납")
                log.info("묶음 %s %s 트레이 반납", bundle_id, tray)
            else:
                return
            bundle["updated_at"] = _now()
            self._close_if_done(bundle)
            self._save()

    def mark_trays_uncertain(self, bundle_id: str, reason: str) -> None:
        """반납 액션이 FINISHED 없이 끝났다. 반납 중이던 칸은 unknown."""
        with self._lock:
            bundle = self._data["bundles"].get(bundle_id)
            if bundle is None:
                return
            changed = False
            for slot in bundle.get("trays", {}).values():
                if slot["state"] == "returning":
                    slot["state"], changed = "unknown", True
            if changed:
                bundle["error"] = reason
                self._save()

    def restore_trays(self, bundle_id: str) -> None:
        """운영자가 잔여 적재물을 확인한 뒤: 불확실한 칸을 물품 상태 기준으로 되돌려 반납을 다시 하게 한다."""
        with self._lock:
            bundle = self._data["bundles"].get(bundle_id)
            if bundle is None:
                return
            for slot in bundle.get("trays", {}).values():
                if slot["state"] in ("unknown", "returning"):
                    parcel = self._data["parcels"].get(slot.get("parcel_id"))
                    state = _TRAY_FROM_PARCEL[parcel["status"]] if parcel else "none"
                    # 반납 중 불확실이면 트레이는 아직 로봇에 있다고 본다 (운영자 확인).
                    if slot.get("before_return"):
                        slot["state"] = slot.pop("before_return")
                    else:
                        slot["state"] = "loaded" if state == "unknown" else state
            self._save()

    def _close_if_done(self, bundle: dict) -> None:
        trays_empty = all(t["state"] == "none" for t in bundle.get("trays", {}).values())
        parcels_done = all(self._data["parcels"][i["parcel_id"]]["status"] in TERMINAL for i in bundle["items"])
        if trays_empty and parcels_done:
            bundle["status"], bundle["closed_at"], bundle["error"] = "closed", _now(), None
        elif trays_empty:
            bundle["status"], bundle["error"] = "blocked", "확인 필요한 물품이 있어 묶음을 닫지 않았다"

    def finish_without_trays(self, bundle_id: str) -> dict:
        """반납할 트레이가 하나도 없을 때 (아무것도 싣지 않음). 계획을 취소하고 묶음을 닫는다."""
        with self._lock:
            bundle = self._data["bundles"].get(bundle_id)
            if bundle is None:
                raise CargoNotFound(f"배송 묶음 {bundle_id!r} 없음")
            for tray, slot in bundle["trays"].items():
                if slot["state"] == "reserved":
                    slot["state"] = "none"
                    parcel = self._data["parcels"].get(slot.get("parcel_id"))
                    if parcel and parcel["status"] == "planned":
                        self._move(parcel, "canceled", "적재되지 않은 채 묶음 종료")
            self._close_if_done(bundle)
            bundle["updated_at"] = _now()
            self._save()
            return self._bundle_view(bundle)

    def cancel_bundle(self, bundle_id: str, reason: str) -> None:
        """적재 전에 끝난 묶음. planned 만 취소한다."""
        with self._lock:
            bundle = self._data["bundles"].get(bundle_id)
            if bundle is None:
                return
            for item in bundle["items"]:
                parcel = self._data["parcels"][item["parcel_id"]]
                if parcel["status"] == "planned":
                    self._move(parcel, "canceled", reason)
            if all(self._data["parcels"][i["parcel_id"]]["status"] in TERMINAL for i in bundle["items"]):
                bundle["status"], bundle["closed_at"] = "canceled", _now()
            bundle["updated_at"] = _now()
            self._save()

    # -- 바닥 표시 정리 ----------------------------------------------------

    def clear_floor(self, site_id: str, map_id: Optional[str] = None) -> list[str]:
        """배송 완료 물품의 바닥 하차 표시를 맵에서 지운다 (세대가 가져간 뒤·시연 전 정리).

        물품 상태(delivered)·하차 위치·이력은 그대로 두고 floor_cleared_at 만 기록한다.
        map_id 를 주면 그 맵(층)만, 없으면 현장 전체. 정리한 parcel_id 목록을 돌려준다.
        """
        with self._lock:
            now = _now()
            cleared = []
            for parcel in self._data["parcels"].values():
                position = parcel.get("drop_position") or {}
                if (parcel["site_id"] != site_id or parcel["status"] != "delivered" or not position
                        or parcel.get("floor_cleared_at") or (map_id and position.get("map_id") != map_id)):
                    continue
                parcel["floor_cleared_at"] = now
                parcel["history"].append({"at": now, "from": "delivered", "to": "delivered",
                                          "reason": "바닥 하차 표시 정리"})
                cleared.append(parcel["parcel_id"])
            if cleared:
                self._save()
                log.info("바닥 하차 표시 정리 %s%s: %d개", site_id, f"/{map_id}" if map_id else "", len(cleared))
            return cleared

    # -- 운영자 확정 -------------------------------------------------------

    def resolve(self, parcel_id: str, result: str, reason: str) -> dict:
        """운영자가 실물 위치를 확인해 기록한다. unknown 외에 onboard→returned/delivered 정정도 허용한다."""
        if result not in RESOLVE_RESULTS:
            raise CargoError(f"result 는 {', '.join(RESOLVE_RESULTS)} 중 하나여야 한다")
        if not reason.strip():
            raise CargoError("정정 사유를 입력하세요.")
        with self._lock:
            parcel = self._data["parcels"].get(parcel_id)
            if parcel is None:
                raise CargoNotFound(f"물품 {parcel_id!r} 없음")
            to = RESOLVE_RESULTS[result]
            allowed = {"unknown": {"onboard", "delivered", "returned", "canceled"},
                       "onboard": {"delivered", "returned"},
                       "planned": {"canceled", "onboard"},
                       "loading": {"onboard", "canceled"},
                       "unloading": {"delivered", "onboard"}}.get(parcel["status"], set())
            if to not in allowed:
                raise CargoError(f"{parcel['status']} 물품은 {result} 로 확정할 수 없다")
            now = _now()
            current = parcel["status"]
            parcel["status"], parcel["updated_at"] = to, now
            if to == "onboard":
                parcel["onboard_robot_id"] = parcel["assigned_robot_id"]
                parcel["last_confirmed_location"] = {"kind": "robot", "robot_id": parcel["assigned_robot_id"],
                                                     "tray_id": parcel["tray_id"]}
            elif to == "delivered":
                parcel["onboard_robot_id"] = None
                step = parcel.get("drop_step") or {}
                position = {"map_id": step.get("map_id"), **(step.get("pose") or {}),
                            "target": parcel.get("drop_target")}
                parcel["drop_position"] = position
                parcel["last_confirmed_location"] = {"kind": "floor", **position}
                parcel.pop("floor_cleared_at", None)
            elif to in ("returned", "canceled"):
                parcel["onboard_robot_id"] = None
                parcel["last_confirmed_location"] = {"kind": "station" if to == "returned" else "locker",
                                                     "station_id": parcel.get("pickup_station_id")}
            parcel.pop("candidates", None)
            parcel["history"].append({"at": now, "from": current, "to": to,
                                      "reason": f"운영자 확정: {reason.strip()}"})
            slot = self._slot(parcel)
            if slot is not None and slot["state"] != "none":
                # 반납했다/실리지 않았다고 확정하면 그 칸 트레이도 로봇에 없다.
                slot["state"] = _TRAY_FROM_PARCEL[to]
            bundle = self._data["bundles"].get(parcel.get("bundle_id"))
            if bundle and bundle["status"] == "blocked":
                self._close_if_done(bundle)
            self._save()
            log.info("물품 %s 운영자 확정 %s -> %s (%s)", parcel_id, current, to, reason)
            return copy.deepcopy(parcel)

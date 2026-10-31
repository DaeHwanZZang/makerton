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

트레이 (TRAY_A / TRAY_B) 는 로봇 안 고정 슬롯이다. (robot_id, tray_id) 에는 진행 중인 물품이 하나뿐이다.
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
TRAY_STATE = {"planned": "reserved", "loading": "loading", "onboard": "occupied",
              "unloading": "unloading", "unknown": "unknown"}

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
        """로봇 트레이 두 칸의 현재 물품. 확인된 적재물과 확인 필요한 물품을 구분한다."""
        with self._lock:
            trays = {tray: None for tray in TRAYS}
            for parcel in self._data["parcels"].values():
                if parcel["status"] in ACTIVE and parcel.get("assigned_robot_id") == robot_id \
                        and parcel.get("tray_id") in trays:
                    trays[parcel["tray_id"]] = parcel
            bundle = next((b for b in self._data["bundles"].values()
                           if b["robot_id"] == robot_id and b["status"] not in ("closed", "canceled")), None)
            return {
                "robot_id": robot_id,
                "bundle": self._bundle_view(bundle) if bundle else None,
                "trays": [{"tray_id": tray,
                           "state": TRAY_STATE[p["status"]] if p else "empty",
                           "parcel": copy.deepcopy(p) if p else None}
                          for tray, p in trays.items()],
            }

    def active_for_station(self, station_id: str) -> list[str]:
        with self._lock:
            return [p["parcel_id"] for p in self._data["parcels"].values()
                    if p.get("pickup_station_id") == station_id and p["status"] in ACTIVE]

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
            busy = [t["tray_id"] for t in self.robot_cargo(robot_id)["trays"] if t["state"] != "empty"]
            if busy:
                raise CargoError(f"로봇 {robot_id} 트레이 {', '.join(busy)}가 비어 있지 않다")
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
                      "closed_at": None, "error": None}
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
                self._save()

    def mark_uncertain(self, parcel_ids: list[str], reason: str) -> list[str]:
        """FINISHED 없이 끝난 액션. 시작이 관측된 물품(loading/unloading)만 unknown 으로 둔다."""
        changed = []
        with self._lock:
            for pid in parcel_ids:
                parcel = self._data["parcels"].get(pid)
                if parcel and parcel["status"] in ("loading", "unloading"):
                    self._move(parcel, "unknown", reason)
                    changed.append(pid)
            if changed:
                self._save()
        return changed

    def return_trays(self, bundle_id: str, location: Optional[dict] = None) -> dict:
        """보관소 픽업 node 의 트레이 반납 FINISHED. 남은 물품은 반납, 실리지 않은 계획은 취소하고 묶음을 닫는다."""
        with self._lock:
            bundle = self._data["bundles"].get(bundle_id)
            if bundle is None:
                raise CargoNotFound(f"배송 묶음 {bundle_id!r} 없음")
            for item in bundle["items"]:
                parcel = self._data["parcels"][item["parcel_id"]]
                if parcel["status"] == "onboard":
                    self._move(parcel, "returned", "트레이 반납", location=location)
                elif parcel["status"] == "planned":
                    self._move(parcel, "canceled", "적재되지 않은 채 묶음 종료")
            if any(self._data["parcels"][i["parcel_id"]]["status"] in ACTIVE for i in bundle["items"]):
                # unknown 물품이 남으면 묶음을 닫지 않는다. 운영자가 확정해야 한다.
                bundle["status"], bundle["error"] = "blocked", "확인 필요한 물품이 있어 묶음을 닫지 않았다"
            else:
                bundle["status"], bundle["closed_at"], bundle["error"] = "closed", _now(), None
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
            elif to in ("returned", "canceled"):
                parcel["onboard_robot_id"] = None
                parcel["last_confirmed_location"] = {"kind": "station" if to == "returned" else "locker",
                                                     "station_id": parcel.get("pickup_station_id")}
            parcel.pop("candidates", None)
            parcel["history"].append({"at": now, "from": current, "to": to,
                                      "reason": f"운영자 확정: {reason.strip()}"})
            bundle = self._data["bundles"].get(parcel.get("bundle_id"))
            if bundle and bundle["status"] == "blocked" and all(
                    self._data["parcels"][i["parcel_id"]]["status"] in TERMINAL for i in bundle["items"]):
                bundle["status"], bundle["closed_at"], bundle["error"] = "closed", now, None
            self._save()
            log.info("물품 %s 운영자 확정 %s -> %s (%s)", parcel_id, current, to, reason)
            return copy.deepcopy(parcel)

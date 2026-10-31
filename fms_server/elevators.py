"""
엘리베이터 (층 간 연결)
======================

맵(층)마다 `type=elevator` 구역을 그려 두면 각각은 그냥 "그 층의 엘리베이터 자리" 일 뿐이다.
**같은 엘리베이터 승강기가 여러 층에 선다**는 정보가 따로 있어야 로봇을 층 사이로 보낼 수 있다.
이 모듈이 그 연결을 가진다.

    elevator = { elevator_id, name, stops: [ {map_id, zone_id, floor?}, ... ] }

    stop.map_id   관리자 map_id (site 안에서 유일, "층" 하나)
    stop.zone_id  그 맵의 elevator 구역 id (annotations.json 의 zones[].zone_id)
    stop.floor    선택. 표시용 층 이름 ("1F", "3" ...). 없으면 map_id 로 보인다

규칙
- 한 승강기는 맵마다 최대 한 번 선다 (같은 층에 두 번 서는 승강기는 없다).
- 한 구역은 승강기 하나에만 속한다.
- stop 의 구역은 그 맵의 **활성 버전** 에 있는 elevator 유형이어야 한다.
- 구역은 맵 버전마다 새로 구워지지만 zone_id 는 편집해도 유지된다. 구역을 지우면 연결은 남고
  조회 때 `missing: true` 로 보인다 (자동으로 지우지 않는다 — 실수로 지웠다 되살리는 경우를 위해).

영속화: `{data_root}/sites/{site_id}/elevators.json` — `{"elevators": {id: {...}}}`.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from common.map_annotations import ZoneType
from fms_server.mapkit.store import MapStoreError

log = logging.getLogger("fms.elevators")

ELEVATORS_FILE = "elevators.json"
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class ElevatorError(Exception):
    """요청이 잘못됨 (HTTP 422 급)."""


class ElevatorNotFound(ElevatorError):
    """승강기를 못 찾음 (HTTP 404 급)."""


class ElevatorConflict(ElevatorError):
    """이미 있는 id / 이미 다른 승강기에 묶인 구역 (HTTP 409 급)."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Stop:
    map_id: str
    zone_id: str
    floor: str = ""
    # 진입 방향 직접 지정 (rad, 로봇이 칸 안쪽을 보는 방향). 없으면 구역 주변에서 열린 변을 문으로 본다
    entry_theta: Optional[float] = None

    def to_dict(self) -> dict:
        d = {"map_id": self.map_id, "zone_id": self.zone_id, "floor": self.floor}
        if self.entry_theta is not None:
            d["entry_theta"] = self.entry_theta
        return d


@dataclass
class Elevator:
    elevator_id: str
    name: str
    stops: list[Stop] = field(default_factory=list)
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "stops": [s.to_dict() for s in self.stops],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, elevator_id: str, data: dict) -> "Elevator":
        return cls(
            elevator_id=elevator_id,
            name=data.get("name", elevator_id),
            stops=[Stop(s["map_id"], s["zone_id"], s.get("floor", ""), s.get("entry_theta")) for s in data.get("stops", [])],
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )


class ElevatorStore:
    """모든 site 의 승강기를 메모리에 들고 있고 바뀔 때만 파일에 쓴다 (JobStore 와 같은 방식)."""

    def __init__(self, data_root: Path | str, registry) -> None:
        self._lock = threading.RLock()
        self._sites_root = Path(data_root) / "sites"
        self._registry = registry
        self._items: dict[str, dict[str, Elevator]] = {}  # site_id -> id -> Elevator
        self._load_all()

    # -- 영속화 ----------------------------------------------------------

    def _path(self, site_id: str) -> Path:
        return self._sites_root / site_id / ELEVATORS_FILE

    def _load_all(self) -> None:
        if not self._sites_root.is_dir():
            return
        for site_dir in sorted(p for p in self._sites_root.iterdir() if p.is_dir()):
            path = site_dir / ELEVATORS_FILE
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._items[site_dir.name] = {
                    eid: Elevator.from_dict(eid, e) for eid, e in (data.get("elevators") or {}).items()
                }
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                log.error("elevators.json 로드 실패 %s: %s", site_dir.name, exc)

    def _persist(self, site_id: str) -> None:
        path = self._path(site_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"elevators": {e.elevator_id: e.to_dict() for e in self._items.get(site_id, {}).values()}}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(path)

    # -- 검증 ------------------------------------------------------------

    def _elevator_zones(self, site_id: str, map_id: str) -> dict[str, dict]:
        """그 맵 활성 버전의 elevator 구역 {zone_id: {name, polygon}}. 맵이 없으면 ElevatorError."""
        store = self._registry.map_store(site_id)
        try:
            if store.active_version(map_id) is None:
                raise ElevatorError(f"맵 {map_id!r} 에 활성 버전이 없다")
            ann = store.load_annotations(map_id)
        except MapStoreError as exc:
            raise ElevatorError(f"맵 {map_id!r}: {exc}") from None
        return {z.zone_id: z for z in ann.zones if z.type == ZoneType.ELEVATOR}

    def _validate_stops(self, site_id: str, stops: list[Stop], *, self_id: Optional[str]) -> None:
        if len(stops) < 2:
            raise ElevatorError("승강기는 최소 2개 층(stop)이 있어야 한다")
        seen_maps: set[str] = set()
        by_map: dict[str, dict] = {}
        for s in stops:
            if not s.map_id or not s.zone_id:
                raise ElevatorError("stop 에 map_id 와 zone_id 가 필요하다")
            if s.map_id in seen_maps:
                raise ElevatorError(f"맵 {s.map_id!r} 에 이 승강기가 두 번 선다 (층마다 한 번)")
            seen_maps.add(s.map_id)
            if s.map_id not in by_map:
                by_map[s.map_id] = self._elevator_zones(site_id, s.map_id)
            if s.zone_id not in by_map[s.map_id]:
                raise ElevatorError(
                    f"맵 {s.map_id!r} 활성 버전에 elevator 구역 {s.zone_id!r} 가 없다 "
                    f"(있는 것: {sorted(by_map[s.map_id]) or '없음'})"
                )
        used = {
            (st.map_id, st.zone_id): e.elevator_id
            for e in self._items.get(site_id, {}).values() if e.elevator_id != self_id
            for st in e.stops
        }
        for s in stops:
            other = used.get((s.map_id, s.zone_id))
            if other:
                raise ElevatorConflict(f"구역 {s.zone_id} ({s.map_id}) 는 이미 승강기 {other!r} 에 묶여 있다")

    # -- CRUD ------------------------------------------------------------

    def create(self, site_id: str, elevator_id: str, name: str, stops: list[Stop]) -> Elevator:
        self._registry.get_site(site_id)  # SiteNotFound
        if not _ID_RE.match(elevator_id):
            raise ElevatorError("elevator_id 는 영문/숫자/-/_ 64자 이하 (예: elev-A)")
        with self._lock:
            items = self._items.setdefault(site_id, {})
            if elevator_id in items:
                raise ElevatorConflict(f"승강기 {elevator_id!r} 가 이미 있다")
            self._validate_stops(site_id, stops, self_id=None)
            e = Elevator(elevator_id, name or elevator_id, stops)
            items[elevator_id] = e
            self._persist(site_id)
        log.info("승강기 생성: %s/%s (%d개 층)", site_id, elevator_id, len(stops))
        return e

    def update(self, site_id: str, elevator_id: str, name: Optional[str], stops: Optional[list[Stop]]) -> Elevator:
        with self._lock:
            e = self.get(site_id, elevator_id)
            if stops is not None:
                self._validate_stops(site_id, stops, self_id=elevator_id)
                e.stops = stops
            if name is not None:
                e.name = name or elevator_id
            e.updated_at = _now_iso()
            self._persist(site_id)
        return e

    def delete(self, site_id: str, elevator_id: str) -> None:
        with self._lock:
            self.get(site_id, elevator_id)
            del self._items[site_id][elevator_id]
            self._persist(site_id)
        log.info("승강기 삭제: %s/%s", site_id, elevator_id)

    def get(self, site_id: str, elevator_id: str) -> Elevator:
        with self._lock:
            e = self._items.get(site_id, {}).get(elevator_id)
        if e is None:
            raise ElevatorNotFound(f"승강기 {elevator_id!r} 없음")
        return e

    def list(self, site_id: str) -> list[Elevator]:
        with self._lock:
            return sorted(self._items.get(site_id, {}).values(), key=lambda e: e.elevator_id)

    # -- 조회용 -----------------------------------------------------------

    def describe(self, site_id: str, e: Elevator) -> dict:
        """stop 마다 구역 이름/중심/현재 존재 여부를 붙인 응답용 dict."""
        out = e.to_dict() | {"elevator_id": e.elevator_id}
        stops = []
        for s in e.stops:
            info = s.to_dict() | {"missing": True, "zone_name": None, "center": None}
            try:
                z = self._elevator_zones(site_id, s.map_id).get(s.zone_id)
            except ElevatorError:
                z = None
            if z is not None:
                xs = [p.x for p in z.polygon]
                ys = [p.y for p in z.polygon]
                info.update(missing=False, zone_name=z.name,
                            center={"x": round(sum(xs) / len(xs), 4), "y": round(sum(ys) / len(ys), 4)})
            stops.append(info)
        out["stops"] = stops
        return out

    def geometry(self, site_id: str, stop: Stop, *, margin: float = 0.5) -> "StopGeometry":
        """승강기 한 층의 진입 기하. `stop_geometry` 참고."""
        z = self._elevator_zones(site_id, stop.map_id).get(stop.zone_id)
        if z is None:
            raise ElevatorError(f"맵 {stop.map_id!r} 에 elevator 구역 {stop.zone_id!r} 가 없다")
        model = self._registry.map_store(site_id).load_map_model(stop.map_id)
        return stop_geometry(z, model, stop.entry_theta, margin=margin)

    def unlinked(self, site_id: str) -> list[dict]:
        """어느 승강기에도 안 묶인 elevator 구역 (맵마다). UI 의 "연결할 구역" 후보."""
        used = {(st.map_id, st.zone_id) for e in self.list(site_id) for st in e.stops}
        out = []
        for m in self._registry.map_store(site_id).list_maps():
            try:
                zones = self._elevator_zones(site_id, m.map_id)
            except ElevatorError:
                continue
            for zid, z in zones.items():
                if (m.map_id, zid) not in used:
                    out.append({"map_id": m.map_id, "zone_id": zid, "zone_name": z.name})
        return out


@dataclass(frozen=True)
class StopGeometry:
    """로봇이 승강기에 전진으로 타고 후진으로 내리는 기하.

    inside    칸 안 정지 자세 (구역 중심, 칸 안쪽을 본다)
    outside   승강장 대기 자세 (문 밖, 같은 방향을 본다)
    distance  outside -> inside 직선 거리 (타기: +distance 전진, 내리기: -distance 후진)
    """

    inside: tuple[float, float, float]
    outside: tuple[float, float, float]
    distance: float


def stop_geometry(zone, model, entry_theta: Optional[float] = None, *, margin: float = 0.5) -> StopGeometry:
    xs = [p.x for p in zone.polygon]
    ys = [p.y for p in zone.polygon]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    # 변마다 (바깥 방향, 중심에서 변까지 반길이, 변 위 샘플 점들)
    sides = {
        "E": ((1, 0), (x1 - x0) / 2), "W": ((-1, 0), (x1 - x0) / 2),
        "S": ((0, 1), (y1 - y0) / 2), "N": ((0, -1), (y1 - y0) / 2),
    }
    if entry_theta is not None:
        dx, dy = -math.cos(entry_theta), -math.sin(entry_theta)  # 문 쪽 = 진입 방향의 반대
        half = abs(dx) * (x1 - x0) / 2 + abs(dy) * (y1 - y0) / 2
        theta = entry_theta
    else:
        best, best_frac = None, 0.0
        for name, ((dx, dy), half) in sides.items():
            # 변 바로 바깥 0.3/0.6 m 의 선을 따라 FREE 비율을 센다 (닫힌 벽 쪽은 0)
            span = (y1 - y0) if dx else (x1 - x0)
            n = max(3, int(span / 0.2))
            free = tot = 0
            for d in (0.3, 0.6):
                for i in range(n):
                    t = -span / 2 + span * (i + 0.5) / n
                    px = cx + dx * (half + d) + (0 if dx else t)
                    py = cy + dy * (half + d) + (t if dx else 0)
                    tot += 1
                    free += model.is_free(px, py)
            frac = free / tot
            if frac > best_frac:
                best, best_frac = (name, dx, dy, half), frac
        if best is None or best_frac < 0.5:
            raise ElevatorError(
                "구역 주변에서 열린(FREE) 변을 못 찾았다 — 승강기 stop 에 entry_theta 를 지정해라")
        _, dx, dy, half = best
        theta = math.atan2(-dy, -dx)
    dist = half + margin
    outside = (cx + dx * dist, cy + dy * dist, theta)
    if not model.is_free(outside[0], outside[1]):
        raise ElevatorError(f"승강장 대기 지점 ({outside[0]:.2f}, {outside[1]:.2f}) 가 주행 가능 칸이 아니다")
    return StopGeometry((cx, cy, theta), outside, dist)

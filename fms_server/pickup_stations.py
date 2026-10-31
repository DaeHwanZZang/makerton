"""
픽업스테이션 — 표시용 pickup zone 과 픽업 node 를 한 단위로 관리한다.

    맵 버전 annotations.json   zones[]: {zone_id: "PS-…", type: "pickup", name, polygon}
    site locations.json        "<이름>": {map_id, x, y, theta, type: "pickup", zone_id: "PS-…"}

- 스테이션 ID 는 zone_id 다 (`PS-<hex>`). 이름은 연결된 location 이름이며 식별자가 아니다.
- zone 은 굽지 않고 동작도 없다. node 는 zone 밖(인접)이어도 된다.
- node 는 맵 범위 안 FREE 셀이어야 한다. FREE 는 도달 가능성의 충분조건이 아니다.
- zone(맵 버전)과 node(locations.json)는 별도 파일이라 프로세스 중단 시 어긋날 수 있다.
  목록은 두 저장소를 대조해 broken 을 표시하고, broken 스테이션은 작업 대상으로 쓰지 않는다.
  broken 스테이션은 삭제로 정리한다.

락 순서: JobCoordinator.lock → _EDIT_LOCK → LocationStore._lock → presets/jobs.
(위치 이름 변경 API 가 coordinator → locations 순으로 잡으므로 그 순서를 따른다.)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional
from uuid import uuid4

from common.map_annotations import LocationType, Zone, ZoneType, normalize_theta, rectangle_polygon
from common.schemas import Point, Pose
from fms_server.locations import LocationError, LocationNotFound, LocationStore
from fms_server.mapkit.store import _EDIT_LOCK, MapStore, MapStoreError, MapVersionConflict

log = logging.getLogger("fms.pickup_stations")

STATION_PREFIX = "PS-"
ACTIVE_JOB_STATUSES = ("assigned", "working", "canceling")
OPEN_JOB_STATUSES = ("pending", *ACTIVE_JOB_STATUSES)


class PickupStationError(Exception):
    """요청이 잘못됨 (HTTP 422 급)."""


class PickupStationNotFound(PickupStationError):
    """스테이션 없음 (HTTP 404 급)."""


class PickupStationInUse(PickupStationError):
    """작업·설정이 참조 중이라 변경 불가 (HTTP 409 급)."""


@dataclass
class Rect:
    x: float
    y: float
    width: float
    height: float
    rotation: float = 0

    def polygon(self) -> list[Point]:
        return rectangle_polygon(self.x,self.y,self.width,self.height,self.rotation)


@dataclass
class PickupStation:
    station_id: str
    map_id: str
    name: Optional[str]
    zone: Optional[Zone]
    node: Optional[Pose]
    problems: list[str] = field(default_factory=list)
    return_node: Optional[Pose] = None
    return_name: Optional[str] = None

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def only_missing_return(self) -> bool:
        """반납 node 만 없는 상태 (반납 node 도입 전 스테이션). 반납 node 추가 편집을 허용한다."""
        return self.problems == [MISSING_RETURN]

    def to_dict(self) -> dict:
        return {
            "station_id": self.station_id,
            "map_id": self.map_id,
            "name": self.name,
            "status": "ok" if self.ok else "broken",
            "problems": list(self.problems),
            "zone": None if self.zone is None else {
                "name": self.zone.name,
                "polygon": [p.model_dump() for p in self.zone.polygon],
            },
            "node": None if self.node is None else self.node.model_dump(),
            "return_node": None if self.return_node is None else self.return_node.model_dump(),
            "return_name": self.return_name,
        }


MISSING_RETURN = "반납 지점(node)이 없음"


def return_name_for(name: str) -> str:
    return f"{name} 반납"


# =============================================================================
# 조회 — 두 저장소 대조
# =============================================================================

def list_stations(maps: MapStore, locations: LocationStore) -> list[PickupStation]:
    """활성 맵 버전의 pickup zone 과 픽업·반납 location 을 대조한다."""
    zones: dict[str, tuple[str, Zone]] = {}
    for summary in maps.list_maps():
        if not summary.active_version:
            continue
        try:
            annotations = maps.load_annotations(summary.map_id, summary.active_version)
        except (MapStoreError, OSError, ValueError) as exc:
            log.error("맵 %s annotations 로드 실패: %s", summary.map_id, exc)
            continue
        for zone in annotations.zones:
            if zone.type == ZoneType.PICKUP:
                zones[zone.zone_id] = (summary.map_id, zone)

    nodes: dict[str, list] = {}
    returns: dict[str, list] = {}
    for loc in locations.list():
        if loc.type == LocationType.PICKUP.value:
            nodes.setdefault(loc.zone_id or "", []).append(loc)
        elif loc.type == LocationType.TRAY_RETURN.value:
            returns.setdefault(loc.zone_id or "", []).append(loc)

    stations = []
    for station_id in sorted(set(zones) | set(nodes) | set(returns)):
        map_id, zone = zones.get(station_id, (None, None))
        linked = nodes.get(station_id, [])
        back = returns.get(station_id, [])
        problems = []
        if not station_id:
            problems.append("zone 연결 정보가 없는 픽업·반납 지점")
        elif zone is None:
            problems.append("활성 맵에 픽업 구역이 없음")
        if not linked:
            problems.append("픽업 지점(node)이 없음")
        elif len(linked) > 1:
            problems.append(f"픽업 지점이 {len(linked)}개 연결됨")
        if not back:
            problems.append(MISSING_RETURN)
        elif len(back) > 1:
            problems.append(f"반납 지점이 {len(back)}개 연결됨")
        node = linked[0] if linked else None
        ret = back[0] if back else None
        for label, loc in (("픽업", node), ("반납", ret)):
            if loc is not None and map_id is not None and loc.map_id != map_id:
                problems.append(f"{label} 지점 맵({loc.map_id})이 구역 맵({map_id})과 다름")
        stations.append(PickupStation(
            station_id=station_id,
            map_id=map_id or (node.map_id if node else ret.map_id if ret else ""),
            name=node.name if node else (zone.name if zone else None),
            zone=zone,
            node=node.pose if node else None,
            problems=problems,
            return_node=ret.pose if ret else None,
            return_name=ret.name if ret else None,
        ))
    return stations


def get_station(maps: MapStore, locations: LocationStore, station_id: str) -> PickupStation:
    for station in list_stations(maps, locations):
        if station.station_id == station_id:
            return station
    raise PickupStationNotFound(f"픽업스테이션 {station_id!r} 없음")


def resolve_station_by_name(maps: MapStore, locations: LocationStore, name: str) -> PickupStation:
    """작업 대상으로 쓸 정상 스테이션. broken 이면 거절한다."""
    for station in list_stations(maps, locations):
        if station.name == name:
            if not station.ok:
                raise PickupStationError(
                    f"픽업스테이션 {name!r} 연결이 손상됨: {', '.join(station.problems)}")
            return station
    raise PickupStationNotFound(f"픽업스테이션 {name!r} 없음")


# =============================================================================
# 검증
# =============================================================================

def _check_node(maps: MapStore, map_id: str, version: str, node: Pose, label: str = "픽업") -> None:
    model = maps.load_map_model(map_id, version)
    if not model.in_bounds(node.x, node.y):
        raise PickupStationError(f"{label} 지점은 맵 범위 안에 있어야 합니다.")
    if not model.is_free(node.x, node.y):
        cell = model.cell_at(node.x, node.y).value
        raise PickupStationError(f"{label} 지점이 주행 가능한 셀이 아닙니다 ({cell}).")


MIN_NODE_GAP = 1.0  # m. 이보다 가까우면 경고 (로봇 여러 대 운영 시 겹침)


def _check_pair(maps: MapStore, map_id: str, version: str, pickup: Pose, ret: Pose) -> list[str]:
    """픽업·반납 node 가 같은 셀이면 거절, MIN_NODE_GAP 미만이면 경고."""
    model = maps.load_map_model(map_id, version)
    if model.world_to_pixel(pickup.x, pickup.y) == model.world_to_pixel(ret.x, ret.y):
        raise PickupStationError("픽업 지점과 반납 지점이 같은 위치입니다. 서로 다른 곳에 두세요.")
    gap = ((pickup.x - ret.x) ** 2 + (pickup.y - ret.y) ** 2) ** 0.5
    if gap < MIN_NODE_GAP:
        return [f"픽업·반납 지점 간격 {gap:.2f} m — {MIN_NODE_GAP:.0f} m 미만이면 로봇이 겹칠 수 있습니다"]
    return []


def _pose(x: float, y: float, theta: float) -> Pose:
    try:
        return Pose(x=x, y=y, theta=normalize_theta(theta))
    except ValueError as exc:
        raise PickupStationError(f"픽업 지점 자세가 올바르지 않습니다: {exc}") from None


def _zone(station_id: str, name: str, rect: Rect) -> Zone:
    return Zone(zone_id=station_id, name=name, type=ZoneType.PICKUP, polygon=rect.polygon())


def _require_version(maps: MapStore, map_id: str, base_version: str) -> None:
    if maps.active_version(map_id) != base_version:
        raise MapVersionConflict("맵 버전이 변경되었습니다. 새로고침 후 다시 편집하세요.")


# =============================================================================
# 쓰기
# =============================================================================

@dataclass
class EditResult:
    station_id: str
    name: str
    version: str
    map_changed: bool
    warnings: list[str] = field(default_factory=list)


def create_station(maps: MapStore, locations: LocationStore, map_id: str, base_version: str,
                   name: str, rect: Rect, node_x: float, node_y: float, node_theta: float,
                   return_x: float, return_y: float, return_theta: float) -> EditResult:
    """zone·픽업 node·반납 node 를 함께 만든다. 새 맵 버전 준비 → node 저장 → 활성화 순서."""
    node = _pose(node_x, node_y, node_theta)
    ret = _pose(return_x, return_y, return_theta)
    ret_name = return_name_for(name)
    station_id = f"{STATION_PREFIX}{uuid4().hex}"
    with _EDIT_LOCK, locations._lock:
        _require_version(maps, map_id, base_version)
        _check_node(maps, map_id, base_version, node)
        _check_node(maps, map_id, base_version, ret, "반납")
        pair_warnings = _check_pair(maps, map_id, base_version, node, ret)
        for candidate in (name, ret_name):
            try:
                locations.get(candidate)
            except LocationNotFound:
                continue
            raise PickupStationError(f"같은 이름의 목적지가 이미 있습니다: {candidate}")
        result = maps.edit_zone(map_id, base_version, station_id, _zone(station_id, name, rect),
                                create=True, activate=False)
        created = []
        try:
            _, warnings = locations.create(name, map_id, node.x, node.y, node.theta,
                                           type=LocationType.PICKUP.value, zone_id=station_id)
            created.append(name)
            _, more = locations.create(ret_name, map_id, ret.x, ret.y, ret.theta,
                                       type=LocationType.TRAY_RETURN.value, zone_id=station_id)
            created.append(ret_name)
            maps.activate(map_id, result.version)
        except Exception:
            for done in created:
                locations.delete(done, allow_pickup=True)
            raise
    log.info("픽업스테이션 생성: %s (%s) 맵 %s 버전 %s", name, station_id, map_id, result.version)
    return EditResult(station_id, name, result.version, True,
                      list(result.warnings) + warnings + more + pair_warnings)


def update_station(maps: MapStore, locations: LocationStore, map_id: str, station_id: str,
                   base_version: str, rect: Rect, node_x: float, node_y: float, node_theta: float,
                   return_x: float, return_y: float, return_theta: float,
                   *, node_locked: Callable[[str], Optional[str]] = lambda name: None) -> EditResult:
    """zone 영역과 두 node 자세를 바꾼다. 이름 변경은 목적지 이름 변경(참조 갱신)을 쓴다.

    반납 node 가 없는 스테이션(도입 전)은 이 편집으로 반납 node 를 추가할 수 있다.
    node_locked(name) 이 사유 문자열을 돌려주면 node 위치 변경을 거절한다 (진행 중 작업 등).
    zone 이 그대로면 새 맵 버전을 만들지 않는다.
    """
    node = _pose(node_x, node_y, node_theta)
    ret = _pose(return_x, return_y, return_theta)
    with _EDIT_LOCK, locations._lock:
        _require_version(maps, map_id, base_version)
        station = get_station(maps, locations, station_id)
        if not station.ok and not station.only_missing_return:
            raise PickupStationError(
                f"연결이 손상된 픽업스테이션은 편집할 수 없습니다: {', '.join(station.problems)}. 삭제 후 다시 만드세요.")
        if station.map_id != map_id:
            raise PickupStationNotFound(f"픽업스테이션 {station_id!r} 은 맵 {map_id!r} 에 없습니다.")
        _check_node(maps, map_id, base_version, node)
        _check_node(maps, map_id, base_version, ret, "반납")
        warnings = _check_pair(maps, map_id, base_version, node, ret)
        name = station.name
        old = station.node
        node_moved = (old.x, old.y, old.theta) != (node.x, node.y, node.theta)
        old_ret = station.return_node
        ret_added = old_ret is None
        ret_moved = not ret_added and (old_ret.x, old_ret.y, old_ret.theta) != (ret.x, ret.y, ret.theta)
        if ret_added:
            try:
                locations.get(return_name_for(name))
            except LocationNotFound:
                pass
            else:
                raise PickupStationError(f"같은 이름의 목적지가 이미 있습니다: {return_name_for(name)}")
        for moved, loc_name, label in ((node_moved, name, "픽업"), (ret_moved, station.return_name, "반납")):
            if moved:
                reason = node_locked(loc_name)
                if reason:
                    raise PickupStationInUse(f"{label} 지점을 사용 중이라 위치를 바꿀 수 없습니다: {reason}")
        new_zone = _zone(station_id, station.zone.name, rect)
        zone_changed = new_zone.polygon != station.zone.polygon
        result = None
        if zone_changed:
            result = maps.edit_zone(map_id, base_version, station_id, new_zone, activate=False)
        warnings += list(result.warnings) if result else []
        undo = []
        try:
            if node_moved:
                _, more = locations.update(name, x=node.x, y=node.y, theta=node.theta)
                undo.append(lambda: locations.update(name, x=old.x, y=old.y, theta=old.theta))
                warnings += more
            if ret_added:
                _, more = locations.create(return_name_for(name), map_id, ret.x, ret.y, ret.theta,
                                           type=LocationType.TRAY_RETURN.value, zone_id=station_id)
                undo.append(lambda: locations.delete(return_name_for(name), allow_pickup=True))
                warnings += more
            elif ret_moved:
                _, more = locations.update(station.return_name, x=ret.x, y=ret.y, theta=ret.theta)
                undo.append(lambda: locations.update(station.return_name, x=old_ret.x, y=old_ret.y,
                                                     theta=old_ret.theta))
                warnings += more
            if result is not None:
                maps.activate(map_id, result.version)
        except Exception:
            for step in reversed(undo):
                step()
            raise
    log.info("픽업스테이션 수정: %s (%s) zone=%s 픽업=%s 반납=%s", name, station_id, zone_changed, node_moved,
             "추가" if ret_added else ret_moved)
    return EditResult(station_id, name, result.version if result else base_version,
                      result is not None, warnings)


def delete_station(maps: MapStore, locations: LocationStore, map_id: str, station_id: str,
                   base_version: str, *, in_use: Callable[[str], Optional[str]] = lambda name: None
                   ) -> EditResult:
    """zone 삭제(새 버전 활성화) → node 삭제. broken 스테이션도 남은 쪽을 정리한다.

    중간에 멈추면 node 만 남는다 → 목록에서 broken 으로 보이고 다시 삭제하면 정리된다.
    """
    with _EDIT_LOCK, locations._lock:
        _require_version(maps, map_id, base_version)
        station = get_station(maps, locations, station_id)
        if station.zone is not None and station.map_id != map_id:
            raise PickupStationNotFound(f"픽업스테이션 {station_id!r} 은 맵 {map_id!r} 에 없습니다.")
        linked = locations.by_zone(station_id) if station_id else []
        for loc in linked:
            reason = in_use(loc.name)
            if reason:
                raise PickupStationInUse(f"픽업스테이션 '{loc.name}'을 사용 중입니다: {reason}")
        result = None
        if station.zone is not None:
            result = maps.edit_zone(map_id, base_version, station_id)
        for loc in linked:
            locations.delete(loc.name, allow_pickup=True)
    log.info("픽업스테이션 삭제: %s (%s)", station.name, station_id)
    return EditResult(station_id, station.name or "", result.version if result else base_version,
                      result is not None, list(result.warnings) if result else [])


# =============================================================================
# 참조 검사 (작업·프리셋·보관소 설정)
# =============================================================================

def describe_references(name: str, *, presets: Iterable[dict], jobs: Iterable,
                        locker_config: Optional[dict], statuses: Iterable[str],
                        station_id: Optional[str] = None,
                        active_parcels: Iterable[str] = ()) -> Optional[str]:
    """이 이름을 참조하는 것이 있으면 사람이 읽을 사유를 돌려준다."""
    statuses = set(statuses)
    reasons = []
    preset_names = [p.get("name", "?") for p in presets
                    if any(c.get("target") == name for c in p.get("commands", []))]
    if preset_names:
        reasons.append(f"프리셋 {', '.join(preset_names)}")
    job_ids = []
    for job in jobs:
        if job.status.value not in statuses:
            continue
        targets = {getattr(c, "target", None) for c in job.commands}
        if name in targets:
            job_ids.append(job.job_id)
    if job_ids:
        reasons.append(f"작업 {', '.join(job_ids)}")
    if locker_config and (locker_config.get("pickup_target") == name
                          or (station_id and locker_config.get("pickup_station_id") == station_id)):
        reasons.append("보관소 연동 설정")
    active_parcels = list(active_parcels)
    if active_parcels:
        reasons.append(f"미반납 물품·트레이 {len(active_parcels)}개")
    return " / ".join(reasons) or None

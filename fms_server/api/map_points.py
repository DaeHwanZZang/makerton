"""실시간 맵에서 목적지/충전 도킹 스테이션 등록. 화면의 맵 버전을 검사한다."""
import math
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from fms_server.locations import LocationError, LocationNotFound
from fms_server.mapkit.chargers import Charger
from fms_server.mapkit.store import MapStoreError, MapVersionConflict, _EDIT_LOCK
from fms_server.sites import SiteError, SiteNotFound

router = APIRouter(prefix='/api/v1/sites/{site_id}/maps/{map_id}', tags=['map points'])


class MapPoseRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False, str_strip_whitespace=True)
    base_version: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=128)
    x: float
    y: float
    theta: float = Field(default=0, ge=-math.pi, le=math.pi)


class MapPointRequest(MapPoseRequest):
    type: Literal['station', 'waypoint', 'parking', 'other'] = 'station'


class DockingStationRequest(MapPoseRequest):
    entry: Literal['FORWARD', 'BACKWARD'] = 'FORWARD'


def _create(request, site, map_id, body, *, docking=False):
    registry = request.app.state.sites
    try:
        store = registry.map_store(site)
        locations = registry.location_store(site)
        # 맵 편집과 목적지 쓰기의 락 순서를 통일한다.
        with request.app.state.job_coordinator.lock, _EDIT_LOCK, locations._lock:
            if docking:
                from fms_server.api.traffic import ensure_editable
                ensure_editable(request, site, map_id)
            if store.active_version(map_id) != body.base_version:
                raise MapVersionConflict('맵 버전이 변경되었습니다. 새로고침 후 다시 추가하세요.')
            model = store.load_map_model(map_id, body.base_version)
            if not model.in_bounds(body.x, body.y):
                raise LocationError('맵 범위 안의 위치를 선택하세요.')
            try:
                locations.get(body.name)
            except LocationNotFound:
                pass
            else:
                raise LocationError('같은 이름의 목적지가 이미 있습니다.')
            result = None
            if docking:
                result = store.add_charger(map_id, body.base_version, Charger(
                    id=body.name, x=body.x, y=body.y, theta=body.theta, entry=body.entry))
            loc, warnings = locations.create(body.name, map_id, body.x, body.y, body.theta,
                                             type='charger' if docking else body.type)
            if result:
                try:
                    store.activate(map_id, result.version)
                except Exception:
                    locations.delete(loc.name)
                    raise
        notified = request.app.state.coordinator.notify_map_activated(site, map_id) if result else []
        return {'name': loc.name, 'map_id': map_id, 'type': loc.type,
                'x': loc.x, 'y': loc.y, 'theta': loc.theta,
                'version': result.version if result else body.base_version,
                'warnings': warnings, 'notified_robots': notified}
    except MapVersionConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except SiteNotFound as exc:
        raise HTTPException(404, str(exc)) from None
    except (LocationError, MapStoreError, SiteError) as exc:
        raise HTTPException(422, str(exc)) from None


@router.post('/locations')
def create_map_location(site_id: str, map_id: str, body: MapPointRequest, request: Request):
    return _create(request, site_id, map_id, body)


@router.post('/docking-stations')
def create_docking_station(site_id: str, map_id: str, body: DockingStationRequest, request: Request):
    return _create(request, site_id, map_id, body, docking=True)

"""맵 구역 편집. 저장 시 새 맵 버전을 굽고 활성화한 뒤 SET_MAP을 알린다."""
from __future__ import annotations

from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from common.map_annotations import TrafficGate, Zone, ZoneType, rectangle_polygon, rectangles_overlap
from fms_server.mapkit.store import MapStoreError, MapVersionConflict, ZoneNotFound
from fms_server.sites import SiteError

router = APIRouter(prefix='/api/v1/sites/{site_id}/maps/{map_id}/zones', tags=['zones'])


class RectangleZoneRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False, str_strip_whitespace=True)
    base_version: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=128)
    type: Literal['prohibit', 'elevator', 'occupancy']
    gates: list[TrafficGate] = Field(default_factory=list, max_length=16)
    x: float
    y: float
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    rotation: float = 0  # 도 단위. 회전 전 사각형의 중심을 고정한다

    @model_validator(mode='after')
    def validate_zone(self):
        self.zone('validation')
        return self

    def zone(self, zone_id: str) -> Zone:
        return Zone(zone_id=zone_id, name=self.name, type=self.type, gates=self.gates,
                    polygon=rectangle_polygon(self.x,self.y,self.width,self.height,self.rotation))


def _edit_locked(request, site_id, map_id, version, zone_id, zone=None, *, create=False):
    try:
        store = request.app.state.sites.map_store(site_id)
        existing = store.load_annotations(map_id).zone_by_id(zone_id)
        if (zone and zone.type == ZoneType.OCCUPANCY) or (existing and existing.type == ZoneType.OCCUPANCY):
            from fms_server.traffic import bounds, intersects
            coord = request.app.state.job_coordinator
            key = coord.traffic.key(site_id, map_id)
            if (any(v['map'] == key for v in coord.traffic.data['leases'].values())
                    or any(j.site_id == site_id and not j.status.terminal and j.status.value != 'pending'
                           for j in request.app.state.jobs.list(site_id))):
                raise HTTPException(409, '교통 예약·진행 중인 작업이 있습니다. 모두 종료한 뒤 점유 존을 편집하세요.')
            uid = store.map_uid(map_id)
            if any(r.state and r.state.map_id == uid and (r.state.order_id or abs(r.state.velocity) > .01)
                   for r in request.app.state.fleet.all()):
                raise HTTPException(409, '이동 중인 로봇이 있습니다. 정지한 뒤 점유 존을 편집하세요.')
            if zone and zone.type == ZoneType.OCCUPANCY:
                model = store.load_map_model(map_id)
                for other in store.load_annotations(map_id).zones:
                    if other.type != ZoneType.OCCUPANCY or other.zone_id == zone_id:
                        continue
                    if rectangles_overlap(zone.polygon, other.polygon):
                        raise HTTPException(422, '점유 존은 서로 겹치거나 경계를 공유할 수 없습니다.')
                for gate in zone.gates:
                    if any(not model.is_free(p.x,p.y) or intersects(bounds(zone),p,0.1) for p in (gate.wait,gate.exit)):
                        raise HTTPException(422, '대기·출구 node는 존 바깥의 FREE 위치에 지정하세요.')
                    if gate.wait == gate.exit:
                        raise HTTPException(422, '대기 node와 출구 node는 달라야 합니다.')
        if not create:
            existing = store.load_annotations(map_id).zone_by_id(zone_id)
            if existing is not None and existing.type == ZoneType.PICKUP:
                # zone 만 바꾸면 node 와의 1:1 연결이 깨진다.
                raise HTTPException(422, '픽업 구역은 픽업스테이션 편집에서 변경하세요.')
        result = store.edit_zone(map_id, version, zone_id, zone, create=create)
    except MapVersionConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except ZoneNotFound as exc:
        raise HTTPException(404, str(exc)) from None
    except (SiteError, MapStoreError) as exc:
        raise HTTPException(422, str(exc)) from None
    notified = request.app.state.coordinator.notify_map_activated(site_id, map_id)
    return {'zone_id': zone_id, 'version': result.version, 'warnings': result.warnings,
            'notified_robots': notified}


def _edit(request, *args, **kwargs):
    with request.app.state.job_coordinator.lock:
        from fms_server.api.traffic import ensure_editable
        ensure_editable(request, args[0], args[1])
        return _edit_locked(request, *args, **kwargs)


@router.post('')
def create_zone(site_id: str, map_id: str, body: RectangleZoneRequest, request: Request):
    zone_id = f'zone-{uuid4().hex[:16]}'
    return _edit(request, site_id, map_id, body.base_version, zone_id,
                 body.zone(zone_id), create=True)


@router.put('/{zone_id}')
def update_zone(site_id: str, map_id: str, zone_id: str, body: RectangleZoneRequest, request: Request):
    return _edit(request, site_id, map_id, body.base_version, zone_id, body.zone(zone_id))


@router.delete('/{zone_id}')
def delete_zone(site_id: str, map_id: str, zone_id: str, base_version: str, request: Request):
    return _edit(request, site_id, map_id, base_version, zone_id)

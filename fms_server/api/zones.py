"""맵 구역 편집. 저장 시 새 맵 버전을 굽고 활성화한 뒤 SET_MAP을 알린다."""
from __future__ import annotations

from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from common.map_annotations import Zone
from common.schemas import Point
from fms_server.mapkit.store import MapStoreError, MapVersionConflict, ZoneNotFound
from fms_server.sites import SiteError

router = APIRouter(prefix='/api/v1/sites/{site_id}/maps/{map_id}/zones', tags=['zones'])


class RectangleZoneRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False, str_strip_whitespace=True)
    base_version: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=128)
    type: Literal['prohibit', 'elevator']
    x: float
    y: float
    width: float = Field(gt=0)
    height: float = Field(gt=0)

    def zone(self, zone_id: str) -> Zone:
        return Zone(zone_id=zone_id, name=self.name, type=self.type, polygon=[
            Point(x=self.x, y=self.y), Point(x=self.x + self.width, y=self.y),
            Point(x=self.x + self.width, y=self.y + self.height),
            Point(x=self.x, y=self.y + self.height),
        ])


def _edit(request, site_id, map_id, version, zone_id, zone=None, *, create=False):
    try:
        store = request.app.state.sites.map_store(site_id)
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

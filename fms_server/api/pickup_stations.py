"""픽업스테이션 REST 라우터. zone 과 node 를 함께 생성·편집·삭제한다.

    GET    /api/v1/sites/{site_id}/pickup-stations
    POST   /api/v1/sites/{site_id}/maps/{map_id}/pickup-stations
           {base_version, name, zone:{x,y,width,height}, node:{x,y,theta}, return_node:{x,y,theta}}
    PUT    /api/v1/sites/{site_id}/maps/{map_id}/pickup-stations/{station_id}
           {base_version, zone:{...}, node:{...}, return_node:{...}}   이름 변경은 목적지 이름 변경을 쓴다
           반납 node 가 없는 스테이션(도입 전)은 PUT 으로 반납 node 를 추가한다.
    DELETE /api/v1/sites/{site_id}/maps/{map_id}/pickup-stations/{station_id}?base_version=
"""
from __future__ import annotations

import math

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from fms_server.locations import LocationError
from fms_server.mapkit.store import MapStoreError, MapVersionConflict
from fms_server.pickup_stations import (
    ACTIVE_JOB_STATUSES, OPEN_JOB_STATUSES, PickupStationError, PickupStationInUse,
    PickupStationNotFound, Rect, create_station, delete_station, describe_references,
    list_stations, update_station,
)
from fms_server.sites import SiteError, SiteNotFound

router = APIRouter(prefix='/api/v1/sites/{site_id}', tags=['pickup stations'])


class ZoneRect(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    x: float
    y: float
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    rotation: float = 0


class NodePose(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    x: float
    y: float
    theta: float = Field(default=0, ge=-math.pi, le=math.pi)


class UpdateStationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    base_version: str = Field(min_length=1)
    zone: ZoneRect
    node: NodePose          # 픽업(트레이 적재) 지점
    return_node: NodePose   # 반납(빈 트레이) 지점


class CreateStationRequest(UpdateStationRequest):
    name: str = Field(min_length=1, max_length=128)


def _stores(request, site_id):
    registry = request.app.state.sites
    return registry.map_store(site_id), registry.location_store(site_id)


def _reference_checker(request, site_id, statuses, station_id):
    presets = request.app.state.presets.list(site_id)
    jobs = request.app.state.jobs.list(site_id)
    locker = getattr(request.app.state, 'locker', None)
    config = locker.snapshot()['config'] if locker is not None else None
    cargo = getattr(request.app.state, 'cargo', None)
    parcels = cargo.active_for_station(station_id) if cargo is not None else []
    return lambda name: describe_references(
        name, presets=presets, jobs=jobs,
        locker_config=config if config and config.get('site_id') == site_id else None,
        statuses=statuses, station_id=station_id, active_parcels=parcels)


def _call(request, site_id, map_id, fn):
    try:
        with request.app.state.job_coordinator.lock:
            from fms_server.api.traffic import ensure_editable
            ensure_editable(request, site_id, map_id)
            result = fn()
    except MapVersionConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except PickupStationInUse as exc:
        raise HTTPException(409, str(exc)) from None
    except (SiteNotFound, PickupStationNotFound) as exc:
        raise HTTPException(404, str(exc)) from None
    except (PickupStationError, LocationError, MapStoreError, SiteError) as exc:
        raise HTTPException(422, str(exc)) from None
    notified = (request.app.state.coordinator.notify_map_activated(site_id, map_id)
                if result.map_changed else [])
    return {'station_id': result.station_id, 'name': result.name, 'map_id': map_id,
            'version': result.version, 'map_changed': result.map_changed,
            'warnings': result.warnings, 'notified_robots': notified}


@router.get('/pickup-stations')
def list_pickup_stations(site_id: str, request: Request):
    try:
        maps, locations = _stores(request, site_id)
        return [s.to_dict() for s in list_stations(maps, locations)]
    except SiteNotFound as exc:
        raise HTTPException(404, str(exc)) from None
    except SiteError as exc:
        raise HTTPException(422, str(exc)) from None


@router.post('/maps/{map_id}/pickup-stations')
def create_pickup_station(site_id: str, map_id: str, body: CreateStationRequest, request: Request):
    def run():
        maps, locations = _stores(request, site_id)
        return create_station(maps, locations, map_id, body.base_version, body.name,
                              Rect(**body.zone.model_dump()), body.node.x, body.node.y, body.node.theta,
                              body.return_node.x, body.return_node.y, body.return_node.theta)
    return _call(request, site_id, map_id, run)


@router.put('/maps/{map_id}/pickup-stations/{station_id}')
def update_pickup_station(site_id: str, map_id: str, station_id: str,
                          body: UpdateStationRequest, request: Request):
    def run():
        maps, locations = _stores(request, site_id)
        # 작업 생성·배정과 경합하지 않도록 코디네이터 락을 가장 바깥에서 잡는다.
        with request.app.state.job_coordinator.lock:
            # 생성된 작업은 좌표 스냅샷을 쓰지만, 그 로봇이 node 를 쓰는 동안 위치를 옮기지 않는다.
            return update_station(maps, locations, map_id, station_id, body.base_version,
                                  Rect(**body.zone.model_dump()), body.node.x, body.node.y,
                                  body.node.theta, body.return_node.x, body.return_node.y,
                                  body.return_node.theta,
                                  node_locked=_reference_checker(request, site_id, ACTIVE_JOB_STATUSES, station_id))
    return _call(request, site_id, map_id, run)


@router.delete('/maps/{map_id}/pickup-stations/{station_id}')
def delete_pickup_station(site_id: str, map_id: str, station_id: str, base_version: str,
                          request: Request):
    def run():
        maps, locations = _stores(request, site_id)
        with request.app.state.job_coordinator.lock:
            return delete_station(maps, locations, map_id, station_id, base_version,
                                  in_use=_reference_checker(request, site_id, OPEN_JOB_STATUSES, station_id))
    return _call(request, site_id, map_id, run)

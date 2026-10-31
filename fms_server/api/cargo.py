"""FMS 물품 장부 조회·정정 API.

    GET  /api/v1/sites/{site_id}/parcels?status=
    GET  /api/v1/sites/{site_id}/parcels/{parcel_id}
    POST /api/v1/sites/{site_id}/parcels/{parcel_id}/resolve   {result, reason}
    GET  /api/v1/sites/{site_id}/delivery-bundles
    GET  /api/v1/sites/{site_id}/cargo                          화면용: 물품·묶음·로봇 트레이 한 번에
    GET  /api/v1/robots/{robot_id}/cargo                        로봇 두 트레이의 현재 물품
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from fms_server.cargo import CargoError, CargoNotFound

router = APIRouter(tags=['cargo'])


class ResolveRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    result: Literal['onboard', 'delivered', 'returned', 'not_loaded']
    reason: str = Field(min_length=1, max_length=500)


def _site_parcel(request, site_id, parcel_id):
    try:
        parcel = request.app.state.cargo.parcel(parcel_id)
    except CargoNotFound as exc:
        raise HTTPException(404, str(exc)) from None
    if parcel['site_id'] != site_id:
        raise HTTPException(404, f'물품 {parcel_id!r} 없음')
    return parcel


@router.get('/api/v1/sites/{site_id}/parcels')
def list_parcels(site_id: str, request: Request, status: Optional[str] = None):
    return request.app.state.cargo.parcels(site_id, status)


@router.get('/api/v1/sites/{site_id}/parcels/{parcel_id}')
def get_parcel(site_id: str, parcel_id: str, request: Request):
    return _site_parcel(request, site_id, parcel_id)


@router.post('/api/v1/sites/{site_id}/parcels/{parcel_id}/resolve')
def resolve_parcel(site_id: str, parcel_id: str, body: ResolveRequest, request: Request):
    _site_parcel(request, site_id, parcel_id)
    # 배송 진행(보관소 연동 워커)과 같은 락에서 정정한다.
    with request.app.state.job_coordinator.lock:
        try:
            return request.app.state.cargo.resolve(parcel_id, body.result, body.reason)
        except CargoNotFound as exc:
            raise HTTPException(404, str(exc)) from None
        except CargoError as exc:
            raise HTTPException(422, str(exc)) from None


@router.get('/api/v1/sites/{site_id}/delivery-bundles')
def list_bundles(site_id: str, request: Request):
    return request.app.state.cargo.bundles(site_id)


@router.get('/api/v1/sites/{site_id}/cargo')
def site_cargo(site_id: str, request: Request):
    cargo = request.app.state.cargo
    robots = {}
    try:
        robot_ids = list(request.app.state.sites.get_site(site_id).robots)
    except Exception:
        robot_ids = []
    for robot_id in robot_ids:
        robots[robot_id] = cargo.robot_cargo(robot_id)
    return {'parcels': cargo.parcels(site_id), 'bundles': cargo.bundles(site_id), 'robots': robots}


@router.get('/api/v1/robots/{robot_id}/cargo')
def robot_cargo(robot_id: str, request: Request):
    return request.app.state.cargo.robot_cargo(robot_id)

"""맵별 동시 이동 허가 설정과 점유/대기 상태 조회."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from fms_server.mapkit.store import MapStoreError
from fms_server.sites import SiteError

router = APIRouter(prefix='/api/v1/sites/{site_id}/maps/{map_id}/traffic', tags=['traffic'])


def ensure_editable(request, site_id, map_id):
    """호출자가 coordinator.lock을 보유한 상태에서 맵 변경과 허가를 직렬화한다."""
    coord = request.app.state.job_coordinator
    key = coord.traffic.key(site_id, map_id)
    if (any(v['map'] == key for v in coord.traffic.data['leases'].values())
            or any(j.traffic.get('map') == key and not j.status.terminal
                   for j in request.app.state.jobs.list(site_id))):
        raise HTTPException(409, '교통 예약·대기 작업이 있습니다. 모두 종료한 뒤 맵을 변경하세요.')


def ensure_robot_editable(request, robot_id, site_id=None, map_id=None):
    coord = request.app.state.job_coordinator
    assignment = request.app.state.sites.assignment_for(robot_id)
    if assignment and assignment.site_id == site_id and assignment.map_id == map_id:
        return
    if (any(v['robot'] == robot_id for v in coord.traffic.data['leases'].values())
            or any(j.robot_id == robot_id and j.traffic and not j.status.terminal
                   for j in request.app.state.jobs.list())):
        raise HTTPException(409, '교통 예약·대기 중인 로봇의 맵 배정·등록을 변경할 수 없습니다.')


class TrafficSettings(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    max_moving_robots: int | None = Field(default=None, ge=1)


def coordinator(request, site_id, map_id):
    try:
        request.app.state.sites.map_store(site_id).get_map(map_id)
    except (SiteError, MapStoreError) as exc:
        raise HTTPException(404, str(exc)) from None
    return request.app.state.job_coordinator


@router.get('')
def get_traffic(site_id: str, map_id: str, request: Request):
    coord = coordinator(request, site_id, map_id)
    with coord.lock:
        return coord.traffic.snapshot(site_id, map_id)


@router.put('')
def set_traffic(site_id: str, map_id: str, body: TrafficSettings, request: Request):
    coord = coordinator(request, site_id, map_id)
    with coord.lock:
        # 제한 신설 시 기존 오더의 허가를 취소하지 않는다.
        if any(j.site_id == site_id and not j.status.terminal and j.status.value != 'pending'
               and not j.traffic for j in request.app.state.jobs.list(site_id)):
            raise HTTPException(409, '기존 작업을 종료한 뒤 이동 제한을 설정하세요.')
        coord.traffic.set_limit(site_id, map_id, body.max_moving_robots)
        return coord.traffic.snapshot(site_id, map_id)

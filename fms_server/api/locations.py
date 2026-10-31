"""Named location REST 라우터.

관리자가 `(x, y, theta)` 에 이름을 붙인다. site 스코프 — 이름은 site 전체에서
유일하고, location 마다 어느 맵 프레임인지(`map_id`)를 함께 갖는다. job 은 나중에
raw 좌표 대신 이 이름으로 목적지를 지정한다.

    GET    /api/v1/sites/{site_id}/locations
    POST   /api/v1/sites/{site_id}/locations              {name, map_id, x, y, theta?, type?}
    GET    /api/v1/sites/{site_id}/locations/{name}
    PUT    /api/v1/sites/{site_id}/locations/{name}        부분 수정 (준 필드만)
    DELETE /api/v1/sites/{site_id}/locations/{name}
"""

from __future__ import annotations

import logging
import copy

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from fms_server.locations import LocationError, LocationNotFound, NamedLocation
from fms_server.sites import SiteError, SiteNotFound

log = logging.getLogger("fms.api.locations")

router = APIRouter(prefix="/api/v1/sites/{site_id}/locations", tags=["locations"])


# =============================================================================
# 모델
# =============================================================================

class CreateLocationRequest(BaseModel):
    name: str
    map_id: str
    x: float
    y: float
    theta: float = 0.0
    type: str = "waypoint"


class UpdateLocationRequest(BaseModel):
    name: str | None = None
    map_id: str | None = None
    x: float | None = None
    y: float | None = None
    theta: float | None = None
    type: str | None = None


class LocationOut(BaseModel):
    name: str
    map_id: str
    map_uid: str
    x: float
    y: float
    theta: float
    type: str
    created_at: str
    updated_at: str
    warnings: list[str] = []


# =============================================================================
# 헬퍼
# =============================================================================

def _store(request: Request, site_id: str):
    try:
        return request.app.state.sites.location_store(site_id)
    except SiteNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except SiteError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


def _out(request: Request, site_id: str, loc: NamedLocation, warnings: list[str] | None = None) -> LocationOut:
    try:
        map_uid = request.app.state.sites.map_store(site_id).map_uid(loc.map_id)
    except Exception:
        map_uid = ""
    return LocationOut(
        name=loc.name, map_id=loc.map_id, map_uid=map_uid,
        x=loc.x, y=loc.y, theta=loc.theta, type=loc.type,
        created_at=loc.created_at, updated_at=loc.updated_at,
        warnings=warnings or [],
    )


# =============================================================================
# 라우트
# =============================================================================


def _update_with_references(request, site_id, store, old_name, body):
    """이름 키와 프리셋/미해결 수동 작업의 참조를 함께 변경한다.

    생성된 작업의 좌표/오더 스냅샷과 충전기 설비 ID는 변경하지 않는다.
    """
    fields = dict(new_name=body.name, map_id=body.map_id, x=body.x,
                  y=body.y, theta=body.theta, type=body.type)
    if body.name is None or body.name.strip() == old_name:
        return store.update(old_name, **fields)
    presets = request.app.state.presets
    jobs = request.app.state.jobs
    coordinator = request.app.state.job_coordinator
    # 배정은 coordinator→locations 순으로 락을 잡으므로 같은 순서를 지킨다.
    with coordinator._lock, store._lock, presets._lock, jobs._lock:
        paths = [store._path, presets._path(site_id), jobs._jobs_path(site_id)]
        backups = {p: p.read_bytes() if p.is_file() else None for p in paths}
        old_jobs = {j.job_id: copy.deepcopy(j) for j in jobs.list(site_id)}
        try:
            loc, warnings = store.update(old_name, **fields)
            rows = presets.list(site_id)
            changed = False
            for preset in rows:
                references = [c for c in preset['commands'] if c.get('target') == old_name]
                for command in references:
                    command['target'] = loc.name
                if references:
                    from fms_server.task import _now_iso
                    preset['updated_at'] = _now_iso()
                    changed = True
            if changed:
                presets._write(site_id, rows)
            changed_jobs = False
            for job in jobs.list(site_id):
                if job.status.value != 'pending' or job.resolved_steps:
                    continue
                for command in job.commands:
                    if command.target == old_name:
                        command.target = loc.name
                        job.touch()
                        changed_jobs = True
            if changed_jobs:
                jobs._persist_site(site_id)
            return loc, warnings
        except Exception:
            jobs._jobs.update(old_jobs)
            for path, contents in backups.items():
                if contents is None:
                    # 이 작업이 새 파일을 만들었을 때만 원래의 미존재 상태로 되돌린다.
                    if path.exists():
                        path.unlink()
                else:
                    temporary = path.with_suffix('.rollback.tmp')
                    temporary.write_bytes(contents)
                    temporary.replace(path)
            raise

@router.get("", response_model=list[LocationOut])
def list_locations(site_id: str, request: Request) -> list[LocationOut]:
    store = _store(request, site_id)
    return [_out(request, site_id, loc) for loc in store.list()]


@router.post("", response_model=LocationOut)
def create_location(site_id: str, body: CreateLocationRequest, request: Request) -> LocationOut:
    store = _store(request, site_id)
    try:
        loc, warnings = store.create(
            body.name, body.map_id, body.x, body.y, body.theta, body.type
        )
    except LocationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return _out(request, site_id, loc, warnings)


@router.get("/{name}", response_model=LocationOut)
def get_location(site_id: str, name: str, request: Request) -> LocationOut:
    store = _store(request, site_id)
    try:
        return _out(request, site_id, store.get(name))
    except LocationNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


@router.put("/{name}", response_model=LocationOut)
def update_location(
    site_id: str, name: str, body: UpdateLocationRequest, request: Request
) -> LocationOut:
    store = _store(request, site_id)
    try:
        loc, warnings = _update_with_references(request, site_id, store, name, body)
    except LocationNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except LocationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return _out(request, site_id, loc, warnings)


@router.delete("/{name}")
def delete_location(site_id: str, name: str, request: Request):
    store = _store(request, site_id)
    try:
        store.delete(name)
    except LocationNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return {"deleted": name, "site_id": site_id}

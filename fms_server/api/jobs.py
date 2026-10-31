"""job (작업) REST 라우터.

job = 이동/대기/도킹/주차 명령의 순서 묶음. site 스코프.
프리셋 게시·자동 배정은 presets API를 사용한다. 이 API의 생성은 수동 pending을 유지한다.
규약은 `../task.py` 참고.

    POST   /api/v1/sites/{site_id}/jobs                {commands:[{type:"move",target:"<loc>"}], robot_id?}
    GET    /api/v1/sites/{site_id}/jobs
    GET    /api/v1/sites/{site_id}/jobs/{job_id}
    POST   /api/v1/sites/{site_id}/jobs/{job_id}/dispatch   {robot_id?}
    POST   /api/v1/sites/{site_id}/jobs/{job_id}/cancel
    DELETE /api/v1/sites/{site_id}/jobs/{job_id}            (working 이 아닐 때만)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from fms_server.sites import SiteError, SiteNotFound
from fms_server.route import stage_label
from fms_server.task import Job, JobError, JobNotFound

log = logging.getLogger("fms.api.jobs")

router = APIRouter(prefix="/api/v1/sites/{site_id}/jobs", tags=["jobs"])


# =============================================================================
# 모델
# =============================================================================

class CommandIn(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    type: str = "move"
    target: str | None = None
    duration: float | None = None
    params: dict | None = None


class CreateJobRequest(BaseModel):
    commands: list[CommandIn]
    robot_id: str | None = None


class DispatchRequest(BaseModel):
    robot_id: str | None = None


class JobOut(BaseModel):
    job_id: str
    site_id: str
    status: str
    robot_id: str | None
    commands: list[dict]
    auto_assign: bool = False
    preset_name: str | None = None
    order_id: str | None
    order_update_id: int
    done_commands: int
    total_commands: int
    stage: int = 0              # 다층 job: 지금 단계 (0부터)
    total_stages: int = 0       # 0 이면 단일 Order job
    stage_label: str | None = None
    error: str | None
    created_at: str
    updated_at: str
    dispatched_at: str | None

    @classmethod
    def of(cls, job: Job) -> "JobOut":
        return cls(
            job_id=job.job_id,
            site_id=job.site_id,
            status=job.status.value,
            robot_id=job.robot_id,
            commands=[c.to_dict() for c in job.commands],
            auto_assign=job.auto_assign, preset_name=job.preset_name,
            order_id=job.order_id,
            order_update_id=job.order_update_id,
            done_commands=job.done_commands,
            total_commands=len(job.commands),
            stage=job.stage, total_stages=len(job.plan),
            stage_label=stage_label(job.plan[job.stage]) if job.plan and job.stage < len(job.plan) else None,
            error=job.error,
            created_at=job.created_at,
            updated_at=job.updated_at,
            dispatched_at=job.dispatched_at,
        )


# =============================================================================
# 헬퍼
# =============================================================================

def _store(request: Request):
    return request.app.state.jobs


def _coordinator(request: Request):
    return request.app.state.job_coordinator


def _check_site(request: Request, site_id: str) -> None:
    try:
        request.app.state.sites.get_site(site_id)
    except SiteNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except SiteError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


def _job_in_site(request: Request, site_id: str, job_id: str) -> Job:
    try:
        job = _store(request).get(job_id)
    except JobNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    if job.site_id != site_id:
        raise HTTPException(status_code=404, detail=f"job {job_id} 는 site {site_id} 에 없다")
    return job


# =============================================================================
# 라우트
# =============================================================================

@router.post("", response_model=JobOut)
def create_job(site_id: str, body: CreateJobRequest, request: Request) -> JobOut:
    _check_site(request, site_id)
    try:
        job = _store(request).create(
            site_id, [c.model_dump(exclude_none=True) for c in body.commands], body.robot_id
        )
    except JobError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return JobOut.of(job)


@router.get("", response_model=list[JobOut])
def list_jobs(site_id: str, request: Request) -> list[JobOut]:
    _check_site(request, site_id)
    return [JobOut.of(j) for j in _store(request).list(site_id)]


@router.get("/{job_id}", response_model=JobOut)
def get_job(site_id: str, job_id: str, request: Request) -> JobOut:
    return JobOut.of(_job_in_site(request, site_id, job_id))


@router.post("/{job_id}/dispatch", response_model=JobOut)
def dispatch_job(site_id: str, job_id: str, body: DispatchRequest, request: Request) -> JobOut:
    _job_in_site(request, site_id, job_id)
    try:
        job = _coordinator(request).dispatch(job_id, body.robot_id)
    except JobNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except JobError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return JobOut.of(job)


@router.post("/{job_id}/cancel", response_model=JobOut)
def cancel_job(site_id: str, job_id: str, request: Request) -> JobOut:
    _job_in_site(request, site_id, job_id)
    try:
        job = _coordinator(request).cancel(job_id)
    except JobError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return JobOut.of(job)


@router.delete("/{job_id}")
def delete_job(site_id: str, job_id: str, request: Request):
    _job_in_site(request, site_id, job_id)
    try:
        _coordinator(request).delete(job_id)
    except JobError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return {"deleted": job_id, "site_id": site_id}

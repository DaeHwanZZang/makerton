"""보관소 목적지 연결·배송 상태·운영자 복귀 API."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from fms_server.locker import LockerConfig
from fms_server.locations import LocationError
from fms_server.sites import SiteError
from fms_server.task import JobError

router = APIRouter(prefix="/api/v1/locker", tags=["locker"])


@router.get("")
def status(request: Request):
    return request.app.state.locker.snapshot()


@router.put("/config")
def configure(body: LockerConfig, request: Request):
    try:
        request.app.state.locker.configure(body)
    except (SiteError, LocationError, JobError) as exc:
        raise HTTPException(422, str(exc)) from None
    return {"config": body.model_dump()}


class ReturnRequest(BaseModel):
    delivered_receipts: list[str] = Field(default_factory=list)
    confirm_remaining_onboard: bool = False


@router.post("/deliveries/{job_id}/return")
def return_robot(job_id: str, body: ReturnRequest, request: Request):
    try:
        request.app.state.locker.recover_return(job_id, body.delivered_receipts, body.confirm_remaining_onboard)
    except KeyError:
        raise HTTPException(404, "배송을 찾을 수 없다") from None
    except JobError as exc:
        raise HTTPException(409, str(exc)) from None
    return {"job_id": job_id, "phase": "returning"}


class CancelRequest(BaseModel):
    reason: str = Field(default="", max_length=500)


@router.post("/deliveries/{job_id}/cancel")
def cancel_delivery(job_id: str, body: CancelRequest, request: Request):
    """로봇 배정 전 배송 취소. 갈 수 없는 세대 물품은 보관소가 직접 수령으로 바꾼다."""
    try:
        unroutable = request.app.state.locker.cancel_queued(job_id, body.reason)
    except KeyError:
        raise HTTPException(404, "배송을 찾을 수 없다") from None
    except JobError as exc:
        raise HTTPException(409, str(exc)) from None
    return {"job_id": job_id, "phase": "canceled", "unroutable": unroutable}

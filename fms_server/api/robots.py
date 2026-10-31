"""로봇 레지스트리 REST + WebSocket 라우터.

    GET  /api/v1/robots            전체 목록 + 요약
    GET  /api/v1/robots/{id}       한 대 상세
    POST /api/v1/robots/{id}/init-position   위치 힌트 (INIT_POSITION 즉시명령 발행)
    WS   /api/v1/robots/stream     스냅샷 1회 + 이후 변경 이벤트 스트림

오더 발행은 job API(api/jobs.py)가 한다.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from common.schemas import InstantAction, InstantActionType

from fms_server.api.views import FleetView, RobotView, build_robot_view

log = logging.getLogger("fms.api.robots")

router = APIRouter(prefix="/api/v1", tags=["robots"])


@router.get("/robots", response_model=FleetView)
def list_robots(request: Request) -> FleetView:
    return FleetView.from_fleet(request.app.state.fleet, request.app.state.sites)


@router.get("/robots/{robot_id}", response_model=RobotView)
def get_robot(robot_id: str, request: Request):
    fleet = request.app.state.fleet
    record = fleet.get(robot_id)
    if record is None:
        return JSONResponse(status_code=404, content={"detail": f"로봇 {robot_id} 를 모른다"})
    return build_robot_view(record, fleet, request.app.state.sites)


class InitPositionRequest(BaseModel):
    x: float
    y: float
    theta: float = Field(default=0.0, description="rad")


@router.post("/robots/{robot_id}/init-position")
def init_position(robot_id: str, body: InitPositionRequest, request: Request):
    """
    위치 힌트를 보낸다. 로봇은 그 근처에서 스캔 매칭으로 정밀 위치를 잡고 State.localization 으로
    결과를 알린다 (이 응답은 발행 여부뿐이다). 힌트는 로봇이 지금 보고한 맵(map_id) 좌표로 본다 —
    로봇이 맵을 갈아타는 중이면 로봇이 이 힌트를 무시한다.
    """
    record = request.app.state.fleet.get(robot_id)
    if record is None or record.state is None:
        return JSONResponse(status_code=404, content={"detail": f"로봇 {robot_id} 의 state 를 아직 못 받았다"})
    params: dict[str, float | str | bool] = {"x": body.x, "y": body.y, "theta": body.theta}
    if record.state.map_id:
        params["map_id"] = record.state.map_id
    action = InstantAction(
        header_id=0,
        timestamp=time.time(),
        robot_id=robot_id,
        action_id=f"INITPOS-{int(time.time() * 1000)}-{robot_id}",
        action_type=InstantActionType.INIT_POSITION,
        params=params,
    )
    request.app.state.mqtt.publish_instant(action)
    log.info("위치 힌트 -> %s (%.2f, %.2f, %.2f)", robot_id, body.x, body.y, body.theta)
    return {"robot_id": robot_id, "sent": True, "params": params}


@router.websocket("/robots/stream")
async def robots_stream(ws: WebSocket) -> None:
    broadcaster = ws.app.state.broadcaster
    await broadcaster.register(ws)
    try:
        while True:
            # 클라이언트가 보내는 건 무시한다 (순수 관측 스트림). 연결 유지용.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        broadcaster.unregister(ws)

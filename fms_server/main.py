"""FMS 서버 진입점 (FastAPI).

    .venv/bin/uvicorn fms_server.main:app --reload

수명 주기:
    시작: SiteRegistry(디스크에서 site 로드) -> Fleet -> MQTT 구독
          -> SiteCoordinator (등록 로봇을 배정 맵과 SET_MAP 동기화)
          -> WebSocket 브로드캐스터
    종료: 역순

브로커(`MQTT_broker`)가 안 떠 있어도 서버는 뜬다. paho 가 재연결을 계속 시도한다
(`FMS_MQTT_REQUIRED=1` 이면 시작 시 연결 실패를 예외로 올린다).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from fms_server.api.broadcaster import FleetBroadcaster
from fms_server.api.elevators import router as elevators_router
from fms_server.api.jobs import router as jobs_router
from fms_server.api.presets import router as presets_router
from fms_server.presets import PresetStore
from fms_server.allocator import JobAllocator
from fms_server.api.locations import router as locations_router
from fms_server.api.robots import router as robots_router
from fms_server.api.sites import router as sites_router
from fms_server.api.zones import router as zones_router
from fms_server.api.traffic import router as traffic_router
from fms_server.api.map_points import router as map_points_router
from fms_server.api.pickup_stations import router as pickup_stations_router
from fms_server.api.cargo import router as cargo_router
from fms_server.cargo import CargoStore
from fms_server.config import settings
from fms_server.elevator_ctl import ElevatorController
from fms_server.elevator_sim import BuiltinElevatorSim
from fms_server.elevators import ElevatorStore
from fms_server.coordinator import SiteCoordinator
from fms_server.fleet import Fleet
from fms_server.mqtt_client import FmsMqttClient
from fms_server.sites import SiteRegistry
from fms_server.task import JobCoordinator, JobStore
from fms_server.locker import LockerBridge
from fms_server.api.locker import router as locker_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("fms.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    registry = SiteRegistry(settings.data_root)
    fleet = Fleet(stale_after=settings.robot_stale_after)
    mqtt = FmsMqttClient(fleet, settings)
    coordinator = SiteCoordinator(fleet, registry, mqtt, settings.public_url)
    broadcaster = FleetBroadcaster(fleet, registry)
    jobs = JobStore(settings.data_root)
    elevator_ctl = ElevatorController(settings)

    def elevator_store():
        # 테스트가 registry/data_root 를 바꾸고 app.state.elevators 를 비우면 다시 만든다
        if getattr(app.state, "elevators", None) is None:
            app.state.elevators = ElevatorStore(app.state.settings.data_root, app.state.sites)
        return app.state.elevators

    job_coordinator = JobCoordinator(fleet, registry, jobs, mqtt, elevators=elevator_store,
                                     elevator_ctl=elevator_ctl, site_coordinator=coordinator)
    elevator_ctl.add_listener(job_coordinator.on_elevator_state)
    elevator_sim = (BuiltinElevatorSim(settings, elevator_store, lambda: app.state.sites)
                    if settings.elevator_sim else None)

    allocator = JobAllocator(job_coordinator)
    cargo = CargoStore(settings.data_root)
    app.state.cargo = cargo
    locker = LockerBridge(registry, fleet, jobs, job_coordinator, mqtt, settings.data_root, cargo=cargo)
    app.state.locker = locker
    app.state.presets = PresetStore(settings.data_root)
    app.state.allocator = allocator

    app.state.settings = settings
    app.state.sites = registry
    app.state.fleet = fleet
    app.state.mqtt = mqtt
    app.state.coordinator = coordinator
    app.state.broadcaster = broadcaster
    app.state.jobs = jobs
    app.state.job_coordinator = job_coordinator
    app.state.elevator_ctl = elevator_ctl
    app.state.elevator_sim = elevator_sim

    broadcaster.start()
    coordinator.start()
    job_coordinator.start()
    mqtt.start()
    elevator_ctl.start()
    if elevator_sim is not None:
        elevator_sim.start()
    allocator.start()
    locker.start()
    log.info("FMS 서버 시작 완료")
    try:
        yield
    finally:
        locker.stop()
        allocator.stop()
        if elevator_sim is not None:
            elevator_sim.stop()
        elevator_ctl.stop()
        mqtt.stop()
        job_coordinator.stop()
        coordinator.stop()
        await broadcaster.stop()
        log.info("FMS 서버 종료")


app = FastAPI(title="FMS Server", version="0.1.0", lifespan=lifespan)
app.include_router(robots_router)
app.include_router(sites_router)
app.include_router(locations_router)
app.include_router(jobs_router)
app.include_router(presets_router)
app.include_router(zones_router)
app.include_router(traffic_router)
app.include_router(map_points_router)
app.include_router(pickup_stations_router)
app.include_router(cargo_router)
app.include_router(elevators_router)
app.include_router(locker_router)

_ADMIN_HTML = Path(__file__).parent / "static" / "admin.html"
app.mount("/static", StaticFiles(directory=_ADMIN_HTML.parent), name="static")


@app.get("/", include_in_schema=False)
def admin_page() -> FileResponse:
    """맵 중심 관제 UI. 관리 기능은 왼쪽 메뉴의 개별 화면에 둔다."""
    return FileResponse(_ADMIN_HTML)


@app.get("/health")
def health(request: Request) -> dict:
    fleet = request.app.state.fleet
    mqtt = request.app.state.mqtt
    registry = request.app.state.sites
    return {
        "status": "ok",
        "mqtt_connected": mqtt.connected,
        "robots_known": len(fleet.all()),
        "sites": len(registry.list_sites()),
        "jobs": len(request.app.state.jobs.list()),
    }

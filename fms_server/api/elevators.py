"""엘리베이터(층 간 연결) REST 라우터. 각 맵의 elevator 구역을 "같은 승강기" 로 묶는다.

    GET    /api/v1/sites/{site_id}/elevators                 승강기 목록 (stop 마다 구역 이름/중심/missing)
    GET    /api/v1/sites/{site_id}/elevators/unlinked        어느 승강기에도 안 묶인 elevator 구역 (후보)
    POST   /api/v1/sites/{site_id}/elevators                 {elevator_id, name?, stops:[{map_id, zone_id, floor?}]}
    GET    /api/v1/sites/{site_id}/elevators/{elevator_id}
    PUT    /api/v1/sites/{site_id}/elevators/{elevator_id}   {name?, stops?}  (stops 는 통째로 교체)
    DELETE /api/v1/sites/{site_id}/elevators/{elevator_id}

규칙은 `fms_server/elevators.py` 참고. 호출/탑승 제어는 `elevator_ctl.py` + job 다층 이동(`route.py`).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from fms_server.elevators import ElevatorConflict, ElevatorError, ElevatorNotFound, ElevatorStore, Stop
from fms_server.sites import SiteError, SiteNotFound

router = APIRouter(prefix="/api/v1/sites/{site_id}/elevators", tags=["elevators"])


class StopIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    map_id: str = Field(min_length=1)
    zone_id: str = Field(min_length=1)
    floor: str = Field(default="", max_length=32)
    entry_theta: float | None = Field(default=None, ge=-3.1416, le=3.1416, allow_inf_nan=False)

    def stop(self) -> Stop:
        return Stop(self.map_id, self.zone_id, self.floor, self.entry_theta)


class CreateElevatorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    elevator_id: str = Field(min_length=1)
    name: str = Field(default="", max_length=128)
    stops: list[StopIn]


class UpdateElevatorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str | None = Field(default=None, max_length=128)
    stops: list[StopIn] | None = None


def _store(request: Request) -> ElevatorStore:
    # lifespan 을 건드리지 않으려고 처음 쓸 때 만든다 (site 목록은 SiteRegistry 가 가진다)
    state = request.app.state
    if getattr(state, "elevators", None) is None:
        state.elevators = ElevatorStore(state.settings.data_root, state.sites)
    return state.elevators


def _wrap(fn):
    try:
        return fn()
    except SiteNotFound as exc:
        raise HTTPException(404, str(exc)) from None
    except ElevatorNotFound as exc:
        raise HTTPException(404, str(exc)) from None
    except ElevatorConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except (ElevatorError, SiteError) as exc:
        raise HTTPException(422, str(exc)) from None


def _describe(request, store, site_id, elevator):
    info = store.describe(site_id, elevator)
    runtime = request.app.state.elevator_ctl.snapshot(elevator.elevator_id)
    floors = {stop.map_id: stop.floor or stop.map_id for stop in elevator.stops}
    runtime["current_floor"] = floors.get(runtime["at"], runtime["at"])
    runtime["target_floor"] = floors.get(runtime["target"], runtime["target"])
    info["runtime"] = runtime
    return info


@router.get("")
def list_elevators(site_id: str, request: Request):
    def run():
        request.app.state.sites.get_site(site_id)
        s = _store(request)
        return {"elevators": [_describe(request, s, site_id, e) for e in s.list(site_id)],
                "simulation_enabled": getattr(request.app.state, "elevator_sim", None) is not None}
    return _wrap(run)


@router.get("/unlinked")
def unlinked(site_id: str, request: Request):
    def run():
        request.app.state.sites.get_site(site_id)
        return {"zones": _store(request).unlinked(site_id)}
    return _wrap(run)


@router.post("")
def create_elevator(site_id: str, body: CreateElevatorRequest, request: Request):
    def run():
        s = _store(request)
        e = s.create(site_id, body.elevator_id, body.name, [x.stop() for x in body.stops])
        return _describe(request, s, site_id, e)
    return _wrap(run)


@router.get("/{elevator_id}")
def get_elevator(site_id: str, elevator_id: str, request: Request):
    def run():
        s = _store(request)
        return _describe(request, s, site_id, s.get(site_id, elevator_id))
    return _wrap(run)


@router.put("/{elevator_id}")
def update_elevator(site_id: str, elevator_id: str, body: UpdateElevatorRequest, request: Request):
    def run():
        s = _store(request)
        stops = None if body.stops is None else [x.stop() for x in body.stops]
        return _describe(request, s, site_id, s.update(site_id, elevator_id, body.name, stops))
    return _wrap(run)


@router.delete("/{elevator_id}")
def delete_elevator(site_id: str, elevator_id: str, request: Request):
    def run():
        _store(request).delete(site_id, elevator_id)
        return {"deleted": elevator_id, "site_id": site_id}
    return _wrap(run)

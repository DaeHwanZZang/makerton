"""프리셋 CRUD 및 게시판 등록/로봇 지정 실행."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from fms_server.api.jobs import CommandIn, JobOut, _check_site
from fms_server.task import Job, JobError, JobNotFound, _parse_command, resolve_steps, compile_to_order

router = APIRouter(prefix='/api/v1/sites/{site_id}/presets', tags=['presets'])


class PresetIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=128)
    commands: list[CommandIn] = Field(min_length=1)


class PublishIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    robot_id: str | None = None


def _call(request,site,fn):
    _check_site(request,site)
    try:
        return fn(request.app.state.presets)
    except JobNotFound as exc:
        raise HTTPException(404,str(exc)) from None
    except JobError as exc:
        raise HTTPException(422,str(exc)) from None


@router.get('')
def list_presets(site_id: str, request: Request):
    return _call(request,site_id,lambda store: store.list(site_id))


@router.post('')
def create_preset(site_id: str, body: PresetIn, request: Request):
    return _call(request,site_id,lambda store: store.save(site_id,body.name,
        [c.model_dump(exclude_none=True) for c in body.commands],request.app.state.sites))


@router.put('/{preset_id}')
def update_preset(site_id: str, preset_id: str, body: PresetIn, request: Request):
    return _call(request,site_id,lambda store: store.save(site_id,body.name,
        [c.model_dump(exclude_none=True) for c in body.commands],request.app.state.sites,preset_id))


@router.delete('/{preset_id}')
def delete_preset(site_id: str, preset_id: str, request: Request):
    def remove(store):
        store.delete(site_id,preset_id)
        return {'deleted':preset_id}
    return _call(request,site_id,remove)


@router.post('/{preset_id}/publish', response_model=JobOut)
def publish_preset(site_id: str, preset_id: str, body: PublishIn, request: Request):
    def publish(store):
        preset = store.get(site_id,preset_id)
        commands = [_parse_command(c) for c in preset['commands']]
        resolved = resolve_steps(commands,site_id,request.app.state.sites)
        # 충전기 연결과 맵 일치도 게시 전에 검사한다. 직접 실행은 dispatch에서 가용성도 검사한다.
        robots = request.app.state.sites.get_site(site_id).robots
        candidate = body.robot_id or next((r.robot_id for r in robots.values()
                          if r.map_id == resolved[0]['map_id']), None)
        start = request.app.state.sites.assignment_for(candidate) if candidate else None
        if start and {r['map_id'] for r in resolved} != {start.map_id}:
            # 여러 층: 승강기 연결·문 방향·충전기를 미리 확인한다
            from fms_server.route import RouteError, build_plan
            try:
                build_plan(commands,resolved,start.map_id,site_id,request.app.state.job_coordinator._elevators(),
                           registry=request.app.state.sites)
            except RouteError as exc:
                raise JobError(str(exc)) from None
        elif candidate:
            compile_to_order(Job('validate',site_id,commands,robot_id=candidate,resolved_steps=resolved),
                             request.app.state.sites)
        elif any(c.kind == 'docking' for c in commands):
            from common.schemas import Pose
            from fms_server.mapkit.chargers import require_charger
            for c, step in zip(commands,resolved):
                if c.kind=='docking':
                    require_charger(request.app.state.sites.map_store(site_id),step['map_id'],
                                    Pose(**step['pose']),c.params['entry'])
        job = request.app.state.jobs.create(site_id,preset['commands'],body.robot_id,
                    auto_assign=not bool(body.robot_id),preset_name=preset['name'],resolved_steps=resolved)
        if body.robot_id:
            try:
                request.app.state.job_coordinator.dispatch(job.job_id)
            except JobError:
                request.app.state.jobs.delete(job.job_id)
                raise
        return JobOut.of(job)
    return _call(request,site_id,publish)

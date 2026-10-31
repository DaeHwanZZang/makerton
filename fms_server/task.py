"""
작업 묶음 — 프리셋에서 명령과 좌표를 복사하고 MQTT Order로 실행한다.

단계: move(target), wait(duration, 직전 위치), docking(target, params), parking(target, params).
목적지는 같은 현장이어야 하고 여러 층이면 승강기로 이동한다(route.py). 게시판 작업(auto_assign)만 FMS 배정 대상이다.
상태: pending → assigned(로봇 예약/수신 대기) → working → success/failed.
실행 중 취소는 canceling으로 예약을 유지하고 새 로봇 상태로 취소를 확인한다.
수신 여부가 불명확한 작업은 자동 재게시하지 않는다 (중복 실행 방지).
JobStore는 sites/{site_id}/jobs.json에 스냅샷·상태를 저장한다.
"""

from __future__ import annotations

import json
import copy
import math
from functools import wraps
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from common.schemas import (
    Action, ActionType, ActionStatus, Pose, LocalizationState,
    ErrorLevel,
    InstantAction,
    InstantActionType,
    Order,
    OrderNode,
    RobotState,
    State,
)
from fms_server.fleet import Fleet, FleetEvent
from fms_server.locations import LocationError, LocationNotFound
from fms_server.mqtt_client import FmsMqttClient
from fms_server.sites import SiteRegistry

log = logging.getLogger("fms.task")

JOBS_FILE = "jobs.json"


class JobError(Exception):
    """요청이 잘못됨 (HTTP 422 급)."""


class JobNotFound(JobError):
    """job 을 못 찾음 (HTTP 404 급)."""


class JobStatus(str, Enum):
    PENDING = "pending"
    ASSIGNED = "assigned"
    WORKING = "working"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELING = "canceling"
    CANCELED = "canceled"

    @property
    def terminal(self) -> bool:
        return self in (JobStatus.SUCCESS, JobStatus.FAILED, JobStatus.CANCELED)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_seg(value: str, kind: str) -> None:
    if not value or "/" in value or "\\" in value or value in (".", "..") or value.startswith("."):
        raise JobError(f"허용되지 않는 {kind}: {value!r}")


# =============================================================================
# 명령
# =============================================================================

@dataclass
class MoveCommand:
    """named location 하나로 이동."""

    target: str  # location 이름 (site 스코프)

    kind: str = "move"

    def to_dict(self) -> dict:
        return {"type": "move", "target": self.target}


@dataclass
class ActionCommand:
    kind: str
    target: Optional[str] = None
    duration: float = 0.0
    params: dict = field(default_factory=dict)

    def to_dict(self):
        data = {"type": self.kind}
        if self.target is not None:
            data["target"] = self.target
        if self.kind in ("wait", "pickup", "drop"):
            data["duration"] = self.duration
            if self.params:
                data["params"] = dict(self.params)
        else:
            data["params"] = dict(self.params)
        return data


def _parse_command(entry: dict):
    kind = entry.get("type")
    target = entry.get("target")
    if kind not in ("move", "wait", "docking", "parking", "pickup", "drop"):
        raise JobError(f"알 수 없는 명령 type: {kind!r}")
    if kind != "wait" and (not isinstance(target, str) or not target.strip()):
        raise JobError(f"{kind} 명령에 target(location 이름)이 없다")
    if target is not None and (not isinstance(target, str) or not target.strip()):
        raise JobError("target은 비어 있지 않은 목적지 이름이어야 한다")
    if kind == "move":
        return MoveCommand(target=target)
    if kind in ("wait", "pickup", "drop"):
        duration = entry.get("duration")
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
            raise JobError("대기 시간은 0보다 큰 유한한 초 단위 숫자여야 한다")
        if kind == "wait":
            return ActionCommand(kind, target, duration=float(duration))
        params = entry.get("params", {})
        if not isinstance(params, dict) or set(params) - {"payload_id"}:
            raise JobError("적재·하차 params에는 payload_id만 지정할 수 있다")
        if params and (not isinstance(params["payload_id"], str) or not params["payload_id"].strip()):
            raise JobError("payload_id는 비어 있지 않은 문자열이어야 한다")
        return ActionCommand(kind, target, duration=float(duration), params=dict(params))
    params = entry.get("params", {})
    if not isinstance(params, dict):
        raise JobError("도킹/주차 params는 객체여야 한다")
    for key, value in params.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool) and not math.isfinite(value):
            raise JobError(f"params.{key}는 유한한 숫자여야 한다")
    try:
        Action(action_id="validate", action_type=ActionType.DOCK if kind == "docking" else ActionType.PARK, params=params)
    except ValueError as exc:
        raise JobError(str(exc)) from None
    return ActionCommand(kind, target, params=copy.deepcopy(params))


def resolve_steps(commands, site_id, registry):
    """작업 생성 시 목적지의 좌표·맵을 복사한다. wait는 직전 위치를 사용한다."""
    locations = registry.location_store(site_id)
    resolved = []
    previous = None
    for command in commands:
        if command.target:
            try:
                map_id, pose = locations.resolve_pose(command.target)
            except (LocationError, LocationNotFound) as exc:
                raise JobError(str(exc)) from None
            previous = (map_id, pose)
        elif previous is None:
            raise JobError("첫 대기 단계에는 목적지가 필요하다. 먼저 이동 단계를 추가하세요.")
        map_id, pose = previous
        resolved.append({"map_id": map_id, "pose": pose.model_dump()})
    return resolved  # 목적지가 여러 층이면 build_plan 이 승강기 단계를 끼운다


# =============================================================================
# Job
# =============================================================================

@dataclass
class Job:
    job_id: str
    site_id: str
    commands: list[MoveCommand | ActionCommand]
    status: JobStatus = JobStatus.PENDING
    robot_id: Optional[str] = None

    auto_assign: bool = False
    preset_name: Optional[str] = None
    external_owner: Optional[str] = None  # 외부 배송이 여러 하위 작업 동안 로봇을 예약한다
    resolved_steps: list[dict] = field(default_factory=list)
    order_snapshot: Optional[dict] = None

    # 다층 job: route.build_plan 결과. 비어 있으면 단일 Order(이전 방식).
    plan: list[dict] = field(default_factory=list)
    stage: int = 0
    stage_data: dict = field(default_factory=dict)  # 지금 stage 의 실행 상태 (stage 가 바뀌면 지운다)

    order_id: Optional[str] = None
    order_update_id: int = 0
    done_commands: int = 0        # 완료로 간주한 명령 수 (진행률 표시용)
    acknowledged: bool = False    # 로봇이 우리 order_id 를 한 번이라도 보고했나
    error: Optional[str] = None

    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    dispatched_at: Optional[str] = None

    def touch(self) -> None:
        self.updated_at = _now_iso()

    def to_dict(self) -> dict:
        return {
            "site_id": self.site_id,
            "commands": [c.to_dict() for c in self.commands],
            "status": self.status.value,
            "auto_assign": self.auto_assign, "preset_name": self.preset_name,
            "external_owner": self.external_owner,
            "resolved_steps": self.resolved_steps, "order_snapshot": self.order_snapshot,
            "plan": self.plan, "stage": self.stage, "stage_data": self.stage_data,
            "robot_id": self.robot_id,
            "order_id": self.order_id,
            "order_update_id": self.order_update_id,
            "done_commands": self.done_commands,
            "acknowledged": self.acknowledged,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "dispatched_at": self.dispatched_at,
        }

    @classmethod
    def from_dict(cls, job_id: str, data: dict) -> "Job":
        return cls(
            job_id=job_id,
            site_id=data["site_id"],
            commands=[_parse_command(e) for e in data.get("commands", [])],
            status=JobStatus(data.get("status", "pending")),
            auto_assign=data.get("auto_assign", False), preset_name=data.get("preset_name"),
            external_owner=data.get("external_owner"),
            resolved_steps=data.get("resolved_steps", []), order_snapshot=data.get("order_snapshot"),
            plan=data.get("plan", []), stage=data.get("stage", 0), stage_data=data.get("stage_data", {}),
            robot_id=data.get("robot_id"),
            order_id=data.get("order_id"),
            order_update_id=data.get("order_update_id", 0),
            done_commands=data.get("done_commands", 0),
            acknowledged=data.get("acknowledged", False),
            error=data.get("error"),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            dispatched_at=data.get("dispatched_at"),
        )


# =============================================================================
# JobStore — 영속화 + 메모리 인덱스
# =============================================================================

class JobStore:
    def __init__(self, data_root: Path | str) -> None:
        self._lock = threading.RLock()
        self._sites_root = Path(data_root) / "sites"
        self._jobs: dict[str, Job] = {}
        self._seq: dict[str, int] = {}   # site_id -> 마지막으로 쓴 번호
        self._load_all()

    # -- 영속화 ----------------------------------------------------------

    def _jobs_path(self, site_id: str) -> Path:
        _check_seg(site_id, "site_id")
        return self._sites_root / site_id / JOBS_FILE

    def _load_all(self) -> None:
        if not self._sites_root.is_dir():
            return
        for site_dir in sorted(p for p in self._sites_root.iterdir() if p.is_dir()):
            path = site_dir / JOBS_FILE
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                log.error("jobs.json 로드 실패 %s: %s", site_dir.name, exc)
                continue
            self._seq[site_dir.name] = int(data.get("seq", 0))
            for job_id, entry in (data.get("jobs") or {}).items():
                try:
                    self._jobs[job_id] = Job.from_dict(job_id, entry)
                except (KeyError, ValueError, JobError) as exc:
                    log.error("job %r 엔트리 무시: %s", job_id, exc)
        if self._jobs:
            log.info("job %d개 로드", len(self._jobs))

    def _persist_site(self, site_id: str) -> None:
        path = self._jobs_path(site_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        jobs = {j.job_id: j.to_dict() for j in self._jobs.values() if j.site_id == site_id}
        payload = {"seq": self._seq.get(site_id, 0), "jobs": jobs}
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        temporary.replace(path)

    def save(self, job: Job) -> None:
        with self._lock:
            self._jobs[job.job_id] = job
            self._persist_site(job.site_id)

    # -- CRUD ----------------------------------------------------------

    def create(
        self, site_id: str, commands: list[dict], robot_id: Optional[str] = None,
        *, auto_assign=False, preset_name=None, resolved_steps=None, external_owner=None
    ) -> Job:
        _check_seg(site_id, "site_id")
        if not commands:
            raise JobError("job 에 명령이 하나도 없다")
        parsed = [_parse_command(e) for e in commands]
        if robot_id is not None:
            _check_seg(robot_id, "robot_id")
        with self._lock:
            n = self._seq.get(site_id, 0) + 1
            self._seq[site_id] = n
            job_id = f"{site_id}-J{n:04d}"
            job = Job(job_id=job_id, site_id=site_id, commands=parsed, robot_id=robot_id,
                      auto_assign=auto_assign, preset_name=preset_name,
                      external_owner=external_owner,
                      resolved_steps=copy.deepcopy(resolved_steps or []))
            self._jobs[job_id] = job
            self._persist_site(site_id)
        log.info("job 생성: %s (명령 %d개, 로봇 %s)", job_id, len(parsed), robot_id or "-")
        return job

    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise JobNotFound(f"job {job_id!r} 없음")
        return job

    def list(self, site_id: Optional[str] = None) -> list[Job]:
        with self._lock:
            jobs = list(self._jobs.values())
        if site_id is not None:
            jobs = [j for j in jobs if j.site_id == site_id]
        return sorted(jobs, key=lambda j: j.job_id)

    def jobs_for_robot(self, robot_id: str, *, status: Optional[JobStatus] = None) -> list[Job]:
        with self._lock:
            jobs = [j for j in self._jobs.values() if j.robot_id == robot_id]
        if status is not None:
            jobs = [j for j in jobs if j.status == status]
        return jobs

    def delete(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise JobNotFound(f"job {job_id!r} 없음")
            if job.status in (JobStatus.ASSIGNED, JobStatus.WORKING, JobStatus.CANCELING):
                raise JobError(f"job {job_id} 는 수행 중이다 — 먼저 취소해라")
            del self._jobs[job_id]
            self._persist_site(job.site_id)
        log.info("job 삭제: %s", job_id)


# =============================================================================
# Order 컴파일
# =============================================================================

def compile_to_order(job: Job, registry: SiteRegistry) -> Order:
    """
    작업 묶음의 이동/대기/도킹/주차 단계를 `Order`로 만든다. 모든 목적지는 같은 맵이어야 하고,
    그 맵은 배정 로봇의 배정 맵과 일치해야 한다. 지금은 전부 `released=true`
    (트래픽 제어 붙기 전).
    """
    if job.robot_id is None:
        raise JobError("배정된 로봇이 없다")
    if not job.commands:
        raise JobError("명령이 없다")

    resolved = job.resolved_steps or resolve_steps(job.commands, job.site_id, registry)
    target_map = resolved[0]["map_id"]

    assignment = registry.assignment_for(job.robot_id)
    if assignment is None:
        raise JobError(f"로봇 {job.robot_id} 가 어느 site 에도 등록돼 있지 않다")
    if assignment.site_id != job.site_id:
        raise JobError(f"로봇 {job.robot_id} 는 다른 site({assignment.site_id})에 등록돼 있다")
    if assignment.map_id != target_map:
        raise JobError(
            f"로봇 {job.robot_id} 배정 맵은 {assignment.map_id!r} 인데 "
            f"job 목적지는 {target_map!r} 맵이다"
        )

    order_id = job.order_id or job.job_id
    nodes = []
    for i, (command, step) in enumerate(zip(job.commands, resolved, strict=True)):
        action = None
        if isinstance(command, ActionCommand):
            kind = {"wait": ActionType.WAIT, "docking": ActionType.DOCK, "parking": ActionType.PARK,
                    "pickup": ActionType.PICK, "drop": ActionType.DROP}[command.kind]
            action = Action(action_id=f"{order_id}-a{i}", action_type=kind,
                            payload_id=command.params.get("payload_id"),
                            duration=command.duration, params=command.params)
            if command.kind == "docking":
                from fms_server.mapkit.chargers import require_charger
                require_charger(registry.map_store(job.site_id), target_map,
                                Pose(**step["pose"]), command.params["entry"])
        nodes.append(OrderNode(node_id=f"{order_id}-n{i}", sequence_id=i,
                               released=True, position=Pose(**step["pose"]), action=action))

    return Order(
        header_id=0,
        timestamp=time.time(),
        robot_id=job.robot_id,
        order_id=order_id,
        order_update_id=job.order_update_id or 1,
        task_id=job.job_id,
        nodes=nodes,
    )


# =============================================================================
# JobCoordinator — 디스패치 + 로봇 보고로 상태 되짚기
# =============================================================================

def _serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call


class JobCoordinator:
    """
    job 을 로봇에 내려보내고(`dispatch`), 로봇 state 를 보고 job 상태를 옮긴다.
    fleet 리스너는 paho 스레드에서 불린다 — 여기서 하는 건 메모리 갱신 + 파일
    쓰기뿐이라 블로킹은 없다.
    """

    def __init__(
        self,
        fleet: Fleet,
        registry: SiteRegistry,
        store: JobStore,
        mqtt: FmsMqttClient,
        *,
        elevators=None,          # ElevatorStore 또는 그걸 돌려주는 callable (다층 job 용)
        elevator_ctl=None,       # ElevatorController (다층 job 용)
        site_coordinator=None,   # SiteCoordinator: 층을 옮긴 로봇에 바로 SET_MAP 을 보낸다 (없으면 다음 state 때)
    ) -> None:
        self._fleet = fleet
        self._registry = registry
        self._store = store
        self._mqtt = mqtt
        self._elevators_src = elevators
        self._ctl = elevator_ctl
        self._site_coordinator = site_coordinator
        self._unsubscribe = None
        self._lock = threading.RLock()
        self._reservations: dict[str, str] = {}

    @property
    def lock(self):
        """외부 배송 전이도 배정과 같은 락에서 처리한다."""
        return self._lock

    @_serialized
    def reserve_robot(self, robot_id: str, owner: str) -> None:
        if self._reservations.get(robot_id, owner) != owner:
            raise JobError(f"로봇 {robot_id}는 다른 배송이 예약했다")
        if any(j.robot_id == robot_id and not j.status.terminal
               and j.status != JobStatus.PENDING and j.external_owner != owner
               for j in self._store.list()):
            raise JobError(f"로봇 {robot_id}는 다른 작업을 수행 중이다")
        self._reservations[robot_id] = owner

    @_serialized
    def release_robot(self, robot_id: str, owner: str) -> None:
        if self._reservations.get(robot_id) == owner:
            del self._reservations[robot_id]

    # -- 수명 주기 ----------------------------------------------------

    def start(self) -> None:
        self._unsubscribe = self._fleet.add_listener(self._on_fleet_event)
        log.info("job 코디네이터 시작")

    def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    # -- 디스패치 (API 핸들러) --------------------------------------

    @_serialized
    def dispatch(self, job_id: str, robot_id: Optional[str] = None) -> Job:
        if not getattr(self._mqtt, "connected", True):
            raise JobError("MQTT 연결을 기다리는 중입니다")
        job = self._store.get(job_id)
        if job.status != JobStatus.PENDING:
            raise JobError(f"job {job_id} 는 {job.status.value} 상태다 — pending 만 디스패치 가능")

        target_robot = robot_id or job.robot_id
        if not target_robot:
            raise JobError("배정할 로봇이 없다 (robot_id 를 줘라)")

        owner = self._reservations.get(target_robot)
        if owner is not None and owner != job.external_owner:
            raise JobError(f"로봇 {target_robot}는 배송 {owner}가 예약 중이다")
        if job.external_owner and owner != job.external_owner:
            raise JobError("외부 배송 예약을 먼저 복원해야 한다")

        if any(j.robot_id == target_robot and j.status in (JobStatus.ASSIGNED, JobStatus.WORKING, JobStatus.CANCELING)
               for j in self._store.list()):
            raise JobError(f"로봇 {target_robot}는 이미 다른 작업에 배정되어 있다")

        record = self._fleet.get(target_robot)
        if record is None or record.state is None:
            raise JobError(f"로봇 {target_robot} 의 state 를 아직 못 받았다")
        if record.state.battery.charge <= 0:
            raise JobError("배터리가 소진된 로봇에는 작업을 배정할 수 없다")
        if not record.available_for_job(self._fleet.stale_after):
            raise JobError(
                f"로봇 {target_robot} 가 작업 수락 불가 상태다 ({record.state.display_state})"
            )

        assignment = self._registry.assignment_for(target_robot)
        if assignment is None or assignment.site_id != job.site_id:
            raise JobError(f"로봇 {target_robot} 가 site {job.site_id} 에 등록돼 있지 않다")

        # 맵 버전 게이팅 — 오래된 금지구역 들고 도는 로봇에 job 안 준다
        try:
            map_store = self._registry.map_store(assignment.site_id)
            active_version = map_store.active_version(assignment.map_id)
        except Exception:
            active_version = None
        if not active_version:
            raise JobError(f"배정 맵 {assignment.map_id} 에 활성 버전이 없다")
        if record.state.map_id != map_store.map_uid(assignment.map_id):
            raise JobError("로봇이 보고한 맵 ID가 배정 맵과 다르다")
        if record.state.map_version != active_version:
            raise JobError(
                f"로봇 {target_robot} 맵 버전({record.state.map_version})이 "
                f"배정 맵 활성 버전({active_version})과 다르다 — SET_MAP 동기화 대기"
            )

        resolved = job.resolved_steps or resolve_steps(job.commands, job.site_id, self._registry)
        if {r["map_id"] for r in resolved} != {assignment.map_id}:
            return self._dispatch_multi(job, target_robot, assignment.map_id, resolved)

        previous_robot = job.robot_id
        job.robot_id = target_robot
        job.order_id = job.job_id
        job.order_update_id = 1
        job.acknowledged = False
        job.done_commands = 0
        job.error = None

        try:
            order = compile_to_order(job, self._registry)
        except JobError:
            job.robot_id = previous_robot
            raise
        job.order_snapshot = order.model_dump(mode="json")
        job.status = JobStatus.ASSIGNED
        job.dispatched_at = _now_iso()
        job.touch()
        self._store.save(job)
        try:
            self._mqtt.publish_order(order)
        except Exception as exc:
            # 전송 성공 여부가 불명확하면 재배정하지 않고 예약 상태를 유지한다.
            job.error = f"오더 전송 확인 필요: {exc}"
            self._store.save(job)
        log.info("job 디스패치: %s -> %s (노드 %d개)", job_id, target_robot, len(order.nodes))
        return job

    # -- 다층 job ----------------------------------------------------

    def _elevators(self):
        src = self._elevators_src
        return src() if callable(src) else src

    def _dispatch_multi(self, job: Job, robot_id: str, start_map: str, resolved: list[dict]) -> Job:
        from fms_server.route import RouteError, build_plan
        elevators = self._elevators()
        if elevators is None or self._ctl is None:
            raise JobError("다층 이동에는 승강기 설정과 제어기가 필요하다")
        try:
            rec = self._fleet.get(robot_id)
            pose = rec.state.pose if rec and rec.state else None
            busy = frozenset(e for e in (st.get("elevator_id") for j in self._store.list() if j.plan and not j.status.terminal
                                         and j.job_id != job.job_id for st in j.plan) if e)
            plan = build_plan(job.commands, resolved, start_map, job.site_id, elevators, registry=self._registry,
                              start_xy=(pose.x, pose.y) if pose else None, avoid=busy)
        except RouteError as exc:
            raise JobError(str(exc)) from None
        job.robot_id = robot_id
        job.resolved_steps = resolved
        job.plan, job.stage, job.stage_data = plan, 0, {}
        job.order_id = job.job_id
        job.order_update_id = 1
        job.acknowledged = False
        job.done_commands = 0
        job.error = None
        job.status = JobStatus.ASSIGNED
        job.dispatched_at = _now_iso()
        job.touch()
        self._store.save(job)
        log.info("다층 job 디스패치: %s -> %s (stage %d개)", job.job_id, robot_id, len(plan))
        self._step_multi(job)
        return job

    @_serialized
    def tick_multifloor(self) -> None:
        """진행 중인 다층 job 을 한 걸음씩. 로봇/승강기 이벤트가 없어도 시간 초과를 보게 주기적으로 부른다."""
        for job in self._store.list():
            if job.plan and job.status in (JobStatus.ASSIGNED, JobStatus.WORKING):
                try:
                    self._step_multi(job)
                except Exception:
                    log.exception("다층 job %s 진행 중 오류", job.job_id)
                    self._fail_multi(job, "내부 오류 (서버 로그 확인)")

    def on_elevator_state(self, _car) -> None:
        self.tick_multifloor()

    def _fail_multi(self, job: Job, message: str) -> None:
        self._finish(job, JobStatus.FAILED, message)

    def _release_elevators(self, job: Job) -> None:
        """job 이 잡고 있는 승강기를 놓는다 (실패/취소 때)."""
        if self._ctl is None:
            return
        for st in job.plan:
            eid = st.get("elevator_id")
            if eid and self._ctl.lease_owner(eid) == job.job_id:
                self._ctl.release(eid)
                self._ctl.release_lease(eid, job.job_id)

    def _advance_stage(self, job: Job) -> bool:
        job.stage += 1
        job.stage_data = {}
        if job.stage >= len(job.plan):
            job.done_commands = len(job.commands)
            self._finish(job, JobStatus.SUCCESS, None)
            return False
        job.touch()
        self._store.save(job)
        return True

    def _step_multi(self, job: Job) -> None:
        for _ in range(12):  # 한 번에 여러 stage 가 끝날 수 있다 (release 등은 바로 끝난다)
            if job.status not in (JobStatus.ASSIGNED, JobStatus.WORKING) or job.stage >= len(job.plan):
                return
            record = self._fleet.get(job.robot_id)
            if record is None or record.state is None:
                return
            fatal = [e for e in record.state.errors if e.level == ErrorLevel.FATAL]
            if fatal:
                self._fail_multi(job, "; ".join(e.error_type for e in fatal))
                return
            if not self._run_stage(job, job.plan[job.stage], record):
                return

    def _run_stage(self, job: Job, stage: dict, record) -> bool:
        """stage 하나를 진행. 끝나서 다음으로 넘어갔으면 True."""
        now = time.time()
        sd = job.stage_data
        state = record.state
        kind = stage["t"]
        sd.setdefault("t0", now)

        if kind == "order":
            oid = f"{job.job_id}-s{job.stage}"
            if "sent_at" not in sd:
                store = self._registry.map_store(job.site_id)
                if not state.is_available or state.map_id != store.map_uid(stage["map_id"]):
                    return False
                order = self._stage_order(job, stage, oid)
                job.order_id, job.order_update_id = oid, 1
                job.order_snapshot = order.model_dump(mode="json")
                sd["sent_at"] = now
                job.touch()
                self._store.save(job)
                try:
                    self._mqtt.publish_order(order)
                except Exception as exc:
                    self._fail_multi(job, f"오더 전송 실패: {exc}")
                return False
            if state.order_id == oid:
                if not sd.get("ack"):
                    sd["ack"] = True
                    job.status = JobStatus.WORKING
                    job.acknowledged = True
                self._stage_progress(job, state)
                return False
            if sd.get("ack"):
                if state.order_id is not None:
                    self._fail_multi(job, "로봇이 다른 오더로 전환됨")
                elif state.state == RobotState.ERROR:
                    self._fail_multi(job, "로봇이 ERROR 상태")
                elif state.state in (RobotState.IDLE, RobotState.CHARGING):
                    if job.external_owner and any(n["cmd"] is not None
                            and n.get("action", {}).get("type") in ("pickup", "drop")
                            and job.done_commands <= n["cmd"] for n in stage["nodes"] if n.get("action")):
                        self._fail_multi(job, "적재·하차 FINISHED 보고 누락 — 확인 필요")
                        return False
                    for n in stage["nodes"]:
                        if n["cmd"] is not None:
                            job.done_commands = max(job.done_commands, n["cmd"] + 1)
                    return self._advance_stage(job)
                return False
            if now - sd["sent_at"] > 20:
                self._fail_multi(job, "로봇이 오더를 받지 않았다 (20초)")
            return False

        ctl = self._ctl
        if ctl is None:
            self._fail_multi(job, "엘리베이터 제어기가 없다")
            return False

        if kind == "call":
            eid, map_id = stage["elevator_id"], stage["map_id"]
            if not ctl.acquire(eid, job.job_id):
                msg = f"승강기 {eid} 를 다른 작업({ctl.lease_owner(eid)})이 쓰는 중"
                if job.error != msg:
                    job.error = msg
                    self._store.save(job)
                sd["t0"] = now
                return False
            if job.error:
                job.error = None
            car = ctl.state(eid)
            if car is None:
                if now - sd["t0"] > 60:
                    self._fail_multi(job, f"승강기 {eid} 제어기 응답이 없다")
                return False
            if car.open_at(map_id) and car.held and sd.get("called"):
                return self._advance_stage(job)
            if not sd.get("called") or (now - sd["called_at"] > 8 and car.target != map_id and car.at != map_id):
                ctl.call(eid, map_id, hold=True)
                sd["called"], sd["called_at"] = True, now
                self._store.save(job)
            elif now - sd["t0"] > 180:
                self._fail_multi(job, f"승강기 {eid} 가 {map_id} 에 오지 않았다")
            return False

        if kind == "ride":
            eid, to_map = stage["elevator_id"], stage["to_map"]
            ctl.acquire(eid, job.job_id)
            car = ctl.state(eid)
            if not sd.get("go"):
                if car is None:
                    return False
                ctl.go(eid, to_map, hold=True)
                ctl.release(eid)  # 출발 층 문 붙잡기를 푼다 (GO 가 먼저 큐에 들어갔다)
                sd["go"], sd["go_at"] = True, now
                self._store.save(job)
                return False
            if car is not None and car.open_at(to_map) and car.held:
                return self._advance_stage(job)
            if now - sd["go_at"] > 300:
                self._fail_multi(job, f"승강기 {eid} 가 {to_map} 에 도착하지 않았다")
            return False

        if kind == "switch":
            # 하차를 마쳤다 (아직 출발 층 맵). 지금 자세를 기록해 두고 배정 맵을 바꾼다 -> SET_MAP
            store = self._registry.map_store(job.site_id)
            to_map = stage["to_map"]
            if not sd.get("switched"):
                nxt = job.plan[job.stage + 1] if job.stage + 1 < len(job.plan) else None
                if nxt is not None and nxt["t"] == "localize":
                    nxt["robot_pose"] = [state.pose.x, state.pose.y, state.pose.theta]
                self._registry.register_robot(job.site_id, job.robot_id, to_map)
                if self._site_coordinator is not None:
                    self._site_coordinator.notify_robot(job.robot_id)
                sd["switched"] = True
                self._store.save(job)
            if state.map_id == store.map_uid(to_map) and state.map_version == store.active_version(to_map):
                return self._advance_stage(job)
            if now - sd["t0"] > 180:
                self._fail_multi(job, f"로봇이 맵 {to_map} 를 받지 못했다")
            return False

        if kind == "localize":
            store = self._registry.map_store(job.site_id)
            uid, version = store.map_uid(stage["map_id"]), store.active_version(stage["map_id"])
            if state.map_id != uid or state.map_version != version:
                if now - sd["t0"] > 180:
                    self._fail_multi(job, f"로봇이 맵 {stage['map_id']} 를 받지 못했다")
                return False
            pose = {"x": stage["x"], "y": stage["y"], "theta": stage["theta"]}  # 시스템이 아는 자세 = 힌트
            if "tp_at" not in sd:
                # 시뮬: 출발 층 맵 위에서 하차한 몸을 도착 층 같은 자리로 옮긴다 (하차 오차는 그대로 옮긴다)
                from common.elevator_proto import topic_sim_teleport
                from fms_server.route import transfer_pose
                body = pose
                if stage.get("robot_pose") and stage.get("ref_from") and stage.get("ref_to"):
                    x, y, th = transfer_pose(stage["robot_pose"], stage["ref_from"], stage["ref_to"])
                    body = {"x": x, "y": y, "theta": th}
                ctl.publish_json(topic_sim_teleport(job.robot_id), body | {"map_id": uid})
                sd["tp_at"] = now
                self._store.save(job)
                return False
            if "hint_at" not in sd:
                if now - sd["tp_at"] < 0.6:
                    return False
                self._mqtt.publish_instant(InstantAction(
                    header_id=0, timestamp=now, robot_id=job.robot_id,
                    action_id=f"INITPOS-{int(now * 1000)}-{job.robot_id}",
                    action_type=InstantActionType.INIT_POSITION, params=pose | {"map_id": uid}))
                sd["hint_at"] = now
                sd["tries"] = sd.get("tries", 0) + 1
                self._store.save(job)
                return False
            elapsed = now - sd["hint_at"]
            if record.last_state_at and record.last_state_at > sd["hint_at"] + 1.3:
                if state.localization == LocalizationState.LOCALIZED:
                    return self._advance_stage(job)
                if state.localization == LocalizationState.UNLOCALIZED or elapsed > 10:
                    if sd["tries"] >= 3:
                        self._fail_multi(job, "새 맵에서 로컬라이징 실패 (힌트 3회)")
                    else:
                        del sd["hint_at"]
            return False

        if kind == "release":
            eid = stage["elevator_id"]
            ctl.release(eid)
            ctl.release_lease(eid, job.job_id)
            return self._advance_stage(job)

        self._fail_multi(job, f"알 수 없는 stage {kind!r}")
        return False

    def _stage_order(self, job: Job, stage: dict, oid: str) -> Order:
        kinds = {"wait": ActionType.WAIT, "docking": ActionType.DOCK, "parking": ActionType.PARK,
                 "pickup": ActionType.PICK, "drop": ActionType.DROP,
                 "straight": ActionType.STRAIGHT}
        nodes = []
        for i, n in enumerate(stage["nodes"]):
            action = None
            if n["action"]:
                a = n["action"]
                action = Action(action_id=f"{oid}-a{i}", action_type=kinds[a["type"]],
                                payload_id=a.get("params", {}).get("payload_id"),
                                duration=a.get("duration", 0.0), params=a.get("params", {}))
            nodes.append(OrderNode(node_id=f"{oid}-n{i}", sequence_id=i, released=True,
                                   position=Pose(x=n["x"], y=n["y"], theta=n["theta"]), action=action))
        return Order(header_id=0, timestamp=time.time(), robot_id=job.robot_id, order_id=oid,
                     order_update_id=1, task_id=job.job_id, nodes=nodes)

    def _stage_progress(self, job: Job, state: State) -> None:
        status = {a.action_id: a.status for a in state.action_states}
        stage = job.plan[job.stage]
        before = job.done_commands
        for node in (job.order_snapshot or {}).get("nodes", []):
            src = stage["nodes"][node["sequence_id"]]
            if node.get("action"):
                if status.get(node["action"]["action_id"]) != ActionStatus.FINISHED:
                    break
            elif not (state.last_node_id == node["node_id"] or state.last_node_sequence_id > node["sequence_id"]):
                break
            if src["cmd"] is not None:
                job.done_commands = max(job.done_commands, src["cmd"] + 1)
        if job.done_commands != before:
            job.touch()
            self._store.save(job)

    @_serialized
    def cancel(self, job_id: str) -> Job:
        job = self._store.get(job_id)
        if job.status.terminal or job.status == JobStatus.CANCELING:
            raise JobError(f"job {job_id} 는 이미 종료됐다 ({job.status.value})")

        dispatched = job.status in (JobStatus.ASSIGNED, JobStatus.WORKING)
        if dispatched and job.robot_id:
            action = InstantAction(
                header_id=0,
                timestamp=time.time(),
                robot_id=job.robot_id,
                action_id=f"CANCEL-{int(time.time() * 1000)}-{job.robot_id}",
                action_type=InstantActionType.CANCEL_ORDER,
                params={},
            )
            self._mqtt.publish_instant(action)

        job.status = JobStatus.CANCELING if dispatched else JobStatus.CANCELED
        job.touch()
        self._store.save(job)
        log.info("job 취소: %s", job_id)
        return job

    @_serialized
    def allocate_pending(self):
        """FIFO 게시판 + ID 순서 가용 로봇. 충돌 검사는 dispatch에서 다시 한다."""
        if not getattr(self._mqtt, "connected", True):
            return
        self.tick_multifloor()
        pending = sorted((j for j in self._store.list()
                          if j.status == JobStatus.PENDING and j.auto_assign),
                         key=lambda j: (j.created_at, j.job_id))
        for job in pending:
            reason = "같은 현장·맵의 가용 로봇을 기다리는 중"
            for record in sorted(self._fleet.available_robots(), key=lambda r: r.robot_id):
                if job.robot_id and record.robot_id != job.robot_id:
                    continue
                try:
                    self.dispatch(job.job_id, record.robot_id)
                    break
                except JobError as exc:
                    reason = str(exc)
            else:
                if job.error != reason:
                    job.error = reason
                    self._store.save(job)

    @_serialized
    def delete(self, job_id):
        self._store.delete(job_id)

    # -- fleet 이벤트 (paho 스레드) --------------------------------

    @_serialized
    def _on_fleet_event(self, event: FleetEvent) -> None:
        if event.kind != "state":
            return
        working = [j for j in self._store.jobs_for_robot(event.robot_id)
                   if j.status in (JobStatus.ASSIGNED, JobStatus.WORKING, JobStatus.CANCELING)]
        for job in working:
            if job.plan and job.status != JobStatus.CANCELING:
                self._step_multi(job)
            else:
                self._advance(job, event.record.state)

    def _advance(self, job: Job, state: Optional[State]) -> None:
        if state is None:
            return

        if job.status == JobStatus.CANCELING:
            # 취소 요청 이전의 상태로 예약을 풀지 않는다. 수신 확인 전에는 재배정하지 않는다.
            if (state.order_id is None and state.timestamp >= datetime.fromisoformat(job.updated_at).timestamp()
                    and state.state in (RobotState.IDLE, RobotState.CHARGING)):
                self._finish(job, JobStatus.CANCELED, None)
            return

        fatal = [e for e in state.errors if e.level == ErrorLevel.FATAL]
        if fatal:
            self._finish(job, JobStatus.FAILED, "; ".join(e.error_type for e in fatal))
            return

        if state.order_id == job.order_id:
            # 로봇이 우리 오더를 물었다. 진행률 갱신.
            first_ack = not job.acknowledged
            job.acknowledged = True
            job.status = JobStatus.WORKING
            done = 0
            action_status = {a.action_id: a.status for a in state.action_states}
            nodes = (job.order_snapshot or {}).get("nodes", [])
            for node in nodes:
                if node.get("action"):
                    if action_status.get(node["action"]["action_id"]) != ActionStatus.FINISHED:
                        break
                elif not (state.last_node_id == node["node_id"] or
                          state.last_node_sequence_id > node["sequence_id"]):
                    break
                done += 1
            if first_ack:
                self._store.save(job)
            if done != job.done_commands:
                job.done_commands = done
                job.touch()
                self._store.save(job)
            return

        if job.acknowledged:
            # 우리 order_id 를 봤었는데 지금은 비었다/딴 오더다.
            if state.order_id is not None:
                self._finish(job, JobStatus.FAILED, "로봇이 다른 오더로 전환됨")
            elif state.state == RobotState.ERROR:
                self._finish(job, JobStatus.FAILED, "로봇이 ERROR 상태")
            elif state.state in (RobotState.IDLE, RobotState.CHARGING):
                if job.external_owner and any(isinstance(c, ActionCommand)
                        and c.kind in ("pickup", "drop") and job.done_commands <= i
                        for i, c in enumerate(job.commands)):
                    self._finish(job, JobStatus.FAILED, "배송 단계 완료 보고 누락 — 적재·하차 확인 필요")
                    return
                job.done_commands = len(job.commands)
                self._finish(job, JobStatus.SUCCESS, None)

    def _finish(self, job: Job, status: JobStatus, error: Optional[str]) -> None:
        if job.plan:
            self._release_elevators(job)
        job.status = status
        job.error = error
        job.touch()
        self._store.save(job)
        log.info("job %s -> %s%s", job.job_id, status.value, f" ({error})" if error else "")

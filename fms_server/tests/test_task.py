"""job (move) — JobStore + JobCoordinator 단위 테스트."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from common.schemas import (
    BatteryInfo,
    BusySubState,
    ConnectionState,
    ErrorInfo,
    ErrorLevel,
    Pose,
    RobotState,
    State,
)
from fms_server.mapkit.store import compute_map_uid
from fms_server.fleet import Fleet
from fms_server.sites import SiteRegistry
from fms_server.task import JobCoordinator, JobError, JobStatus, JobStore, compile_to_order
from fms_server.tests.factories import make_state

VENDOR = Path("maps/641931de9eae7cecb34d5765")


def _vendor_files() -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in VENDOR.iterdir() if p.is_file()}


class FakeMqtt:
    def __init__(self) -> None:
        self.orders = []
        self.instants = []

    def publish_order(self, order) -> None:
        self.orders.append(order)

    def publish_instant(self, action) -> None:
        self.instants.append(action)


@pytest.fixture
def env(tmp_path):
    """hq / floor-1(active) / AMR-001 등록 / location 'dest' + fleet 에 로봇 online."""
    registry = SiteRegistry(tmp_path)
    registry.create_site("hq", "본사")
    result = registry.map_store("hq").ingest_upload("floor-1", _vendor_files(), activate=True)
    version = result.version
    registry.register_robot("hq", "AMR-001", "floor-1")
    registry.location_store("hq").create("dest", "floor-1", 3.0, 4.0, 0.0)
    registry.location_store("hq").create("dest2", "floor-1", 5.0, 6.0, 1.0)

    fleet = Fleet(stale_after=3.0)
    mqtt = FakeMqtt()
    store = JobStore(tmp_path)
    coord = JobCoordinator(fleet, registry, store, mqtt)
    coord.start()

    def robot_online(robot_id="AMR-001", state=RobotState.IDLE, order_id=None, mv=version):
        fleet.apply_connection(robot_id, ConnectionState.ONLINE)
        fleet.apply_state(
            make_state(robot_id, state, order_id=order_id, map_id=compute_map_uid("hq", "floor-1"), map_version=mv)
        )

    robot_online()
    yield dict(
        registry=registry, fleet=fleet, mqtt=mqtt, store=store, coord=coord,
        version=version, robot_online=robot_online,
    )
    coord.stop()


# =============================================================================
# JobStore
# =============================================================================

def test_create_job_pending(env):
    job = env["store"].create("hq", [{"type": "move", "target": "dest"}], "AMR-001")
    assert job.status == JobStatus.PENDING
    assert job.job_id == "hq-J0001"
    assert len(job.commands) == 1


def test_create_rejects_unknown_command(env):
    with pytest.raises(JobError):
        env["store"].create("hq", [{"type": "fly", "target": "x"}], "AMR-001")
    with pytest.raises(JobError):
        env["store"].create("hq", [{"type": "docking", "target": "x"}], "AMR-001")


def test_create_rejects_empty(env):
    with pytest.raises(JobError):
        env["store"].create("hq", [], "AMR-001")


def test_job_ids_increment_per_site(env):
    a = env["store"].create("hq", [{"type": "move", "target": "dest"}])
    b = env["store"].create("hq", [{"type": "move", "target": "dest"}])
    assert (a.job_id, b.job_id) == ("hq-J0001", "hq-J0002")


def test_persistence_reload(env, tmp_path):
    env["store"].create("hq", [{"type": "move", "target": "dest"}], "AMR-001")
    reloaded = JobStore(tmp_path)
    jobs = reloaded.list("hq")
    assert len(jobs) == 1 and jobs[0].commands[0].target == "dest"


# =============================================================================
# compile_to_order
# =============================================================================

def test_compile_builds_nodes_in_order(env):
    job = env["store"].create(
        "hq",
        [{"type": "move", "target": "dest"}, {"type": "move", "target": "dest2"}],
        "AMR-001",
    )
    job.order_id = job.job_id
    order = compile_to_order(job, env["registry"])
    assert order.robot_id == "AMR-001"
    assert [n.sequence_id for n in order.nodes] == [0, 1]
    assert all(n.released for n in order.nodes)
    assert (order.nodes[0].position.x, order.nodes[0].position.y) == (3.0, 4.0)
    assert order.nodes[1].position.theta == 1.0


def test_compile_rejects_unknown_location(env):
    job = env["store"].create("hq", [{"type": "move", "target": "ghost"}], "AMR-001")
    with pytest.raises(JobError):
        compile_to_order(job, env["registry"])


def test_compile_rejects_wrong_map(env):
    env["registry"].map_store("hq").ingest_upload("floor-2", _vendor_files(), activate=True)
    env["registry"].location_store("hq").create("f2", "floor-2", 1.0, 2.0)
    job = env["store"].create("hq", [{"type": "move", "target": "f2"}], "AMR-001")
    with pytest.raises(JobError):  # 로봇 배정 맵은 floor-1
        compile_to_order(job, env["registry"])


# =============================================================================
# dispatch
# =============================================================================

def test_dispatch_publishes_order_and_goes_working(env):
    job = env["store"].create("hq", [{"type": "move", "target": "dest"}], "AMR-001")
    out = env["coord"].dispatch(job.job_id)
    assert out.status == JobStatus.ASSIGNED
    assert len(env["mqtt"].orders) == 1
    assert env["mqtt"].orders[0].order_id == job.job_id
    assert env["mqtt"].orders[0].task_id == job.job_id


def test_dispatch_rejects_busy_robot(env):
    env["robot_online"](state=RobotState.BUSY)
    job = env["store"].create("hq", [{"type": "move", "target": "dest"}], "AMR-001")
    with pytest.raises(JobError):
        env["coord"].dispatch(job.job_id)
    assert env["store"].get(job.job_id).status == JobStatus.PENDING


def test_dispatch_rejects_stale_map_version(env):
    env["robot_online"](mv="old000000000")
    job = env["store"].create("hq", [{"type": "move", "target": "dest"}], "AMR-001")
    with pytest.raises(JobError):
        env["coord"].dispatch(job.job_id)


def test_dispatch_rejects_unknown_robot_state(env):
    job = env["store"].create("hq", [{"type": "move", "target": "dest"}], "AMR-002")
    with pytest.raises(JobError):
        env["coord"].dispatch(job.job_id)


def test_double_dispatch_rejected(env):
    job = env["store"].create("hq", [{"type": "move", "target": "dest"}], "AMR-001")
    env["coord"].dispatch(job.job_id)
    with pytest.raises(JobError):
        env["coord"].dispatch(job.job_id)


# =============================================================================
# 로봇 보고 -> job 상태
# =============================================================================

def _report(fleet, robot_id, *, state, order_id, version, errors=None, last_seq=0):
    fleet.apply_state(
        State(
            header_id=9,
            timestamp=time.time(),
            robot_id=robot_id,
            state=state,
            sub_state=BusySubState.MOVING if state == RobotState.BUSY else None,
            pose=Pose(x=0.0, y=0.0, theta=0.0),
            last_node_id="n0",
            last_node_sequence_id=last_seq,
            order_id=order_id,
            battery=BatteryInfo(charge=80.0),
            map_id=compute_map_uid("hq", "floor-1"),
            map_version=version,
            errors=errors or [],
        )
    )


def test_job_succeeds_when_robot_clears_order(env):
    v = env["version"]
    job = env["store"].create("hq", [{"type": "move", "target": "dest"}], "AMR-001")
    env["coord"].dispatch(job.job_id)

    _report(env["fleet"], "AMR-001", state=RobotState.BUSY, order_id=job.job_id, version=v)
    assert env["store"].get(job.job_id).status == JobStatus.WORKING
    assert env["store"].get(job.job_id).acknowledged is True

    _report(env["fleet"], "AMR-001", state=RobotState.IDLE, order_id=None, version=v)
    done = env["store"].get(job.job_id)
    assert done.status == JobStatus.SUCCESS
    assert done.done_commands == 1


def test_job_fails_on_fatal_error(env):
    v = env["version"]
    job = env["store"].create("hq", [{"type": "move", "target": "dest"}], "AMR-001")
    env["coord"].dispatch(job.job_id)
    _report(env["fleet"], "AMR-001", state=RobotState.BUSY, order_id=job.job_id, version=v)
    _report(
        env["fleet"], "AMR-001", state=RobotState.ERROR, order_id=None, version=v,
        errors=[ErrorInfo(error_type="NO_PATH", level=ErrorLevel.FATAL)],
    )
    done = env["store"].get(job.job_id)
    assert done.status == JobStatus.FAILED
    assert "NO_PATH" in done.error


def test_order_clear_before_ack_does_not_succeed(env):
    """디스패치 직후 로봇이 아직 오더를 못 물었을 때 order_id=None 을 완료로 보면 안 된다."""
    v = env["version"]
    job = env["store"].create("hq", [{"type": "move", "target": "dest"}], "AMR-001")
    env["coord"].dispatch(job.job_id)
    _report(env["fleet"], "AMR-001", state=RobotState.IDLE, order_id=None, version=v)
    assert env["store"].get(job.job_id).status == JobStatus.ASSIGNED


# =============================================================================
# cancel
# =============================================================================

def test_cancel_working_publishes_instant(env):
    job = env["store"].create("hq", [{"type": "move", "target": "dest"}], "AMR-001")
    env["coord"].dispatch(job.job_id)
    out = env["coord"].cancel(job.job_id)
    assert out.status == JobStatus.CANCELING
    assert len(env["mqtt"].instants) == 1
    assert env["mqtt"].instants[0].action_type.value == "CANCEL_ORDER"


def test_cancel_pending_no_instant(env):
    job = env["store"].create("hq", [{"type": "move", "target": "dest"}], "AMR-001")
    env["coord"].cancel(job.job_id)
    assert env["mqtt"].instants == []


def test_cancel_terminal_rejected(env):
    job = env["store"].create("hq", [{"type": "move", "target": "dest"}], "AMR-001")
    env["coord"].cancel(job.job_id)
    with pytest.raises(JobError):
        env["coord"].cancel(job.job_id)

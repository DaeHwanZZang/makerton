"""다층 이동: 엘리베이터 시뮬레이터 칸, 문 방향 기하, 다층 job stage 진행."""
from __future__ import annotations

import io
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from common import elevator_proto as P
from common.map_annotations import Point
from common.schemas import ConnectionState, InstantActionType, LocalizationState, RobotState
from fms_server.elevator_ctl import CarState
from fms_server.elevators import ElevatorError, ElevatorStore, Stop, stop_geometry
from fms_server.fleet import Fleet
from fms_server.sites import SiteRegistry
from fms_server.task import JobCoordinator, JobStatus, JobStore
from fms_server.tests.factories import make_state

REPO = Path(__file__).resolve().parents[2]
from fms_server import elevator_sim  # noqa: E402


# --- 엘리베이터 시뮬레이터 칸 ------------------------------------------------

def run(car, seconds, dt=0.05):
    for _ in range(int(seconds / dt)):
        car.step(dt)


def test_car_call_hold_and_release():
    car = elevator_sim.ElevatorCar("e", ["f1", "f3"], floor_time=2, door_time=1, open_time=2)
    assert car.at == "f1" and car.door == P.DOOR_CLOSED
    car.command(P.CMD_CALL, "f1", hold=True)
    run(car, 1.2)
    assert car.door == P.DOOR_OPEN and car.held
    run(car, 10)  # hold 라 안 닫힌다
    assert car.door == P.DOOR_OPEN
    car.command(P.CMD_GO, "f3", hold=True)
    car.command(P.CMD_RELEASE)
    run(car, 0.5)
    run(car, 12)
    assert car.at == "f3" and car.door == P.DOOR_OPEN and car.held
    car.command(P.CMD_RELEASE)
    run(car, 6)
    assert car.door == P.DOOR_CLOSED and not car.held


def test_car_moves_in_between_and_ignores_bad_stop():
    car = elevator_sim.ElevatorCar("e", ["f1", "f2", "f3"], floor_time=1, door_time=0.5, open_time=0.5)
    assert not car.command(P.CMD_CALL, "f9")
    car.command(P.CMD_CALL, "f3")
    run(car, 0.2)
    assert car.moving and car.at is None and car.target == "f3"
    run(car, 3)
    assert car.at == "f3"


def test_builtin_sim_follows_registered_elevators(tmp_path):
    """FMS 내장 시뮬레이터는 승강기 연결을 추가/변경/삭제하면 칸을 따라 만든다."""
    published = []

    class Store:
        def __init__(self):
            self.items = [SimpleNamespace(elevator_id="E1", stops=[SimpleNamespace(map_id="f1"),
                                                                     SimpleNamespace(map_id="f3")])]

        def list(self, site_id):
            return self.items

    store = Store()
    registry = SimpleNamespace(list_sites=lambda: [SimpleNamespace(site_id="s")])
    settings = SimpleNamespace(mqtt_client_id="t", mqtt_host="localhost", mqtt_port=1, mqtt_keepalive=5)
    sim = elevator_sim.BuiltinElevatorSim(settings, store, registry)
    sim._client.publish = lambda *a, **k: published.append(a)
    sim.sync()
    assert sim.cars["E1"].stops == ["f1", "f3"]
    store.items[0].stops.append(SimpleNamespace(map_id="f5"))
    sim.sync()
    assert sim.cars["E1"].stops == ["f1", "f3", "f5"]
    store.items = []
    sim.sync()
    assert "E1" not in sim.cars and published[-1][1] == b""  # retain 지움


# --- 문 방향 기하 -----------------------------------------------------------

class Grid:
    """x 3..5 y 2..4 구역. blocked 변 바깥은 벽."""

    def __init__(self, blocked=()):
        self.blocked = set(blocked)

    def is_free(self, x, y):
        if not (0 <= x <= 10 and 0 <= y <= 8):
            return False
        if 2.0 <= x <= 6.0 and 1.0 <= y <= 5.0 and not (3 <= x <= 5 and 2 <= y <= 4):  # 구역 둘레 띠
            side = "E" if x > 5 else "W" if x < 3 else "S" if y > 4 else "N"
            return side not in self.blocked
        return True


ZONE = SimpleNamespace(polygon=[Point(x=3, y=2), Point(x=5, y=2), Point(x=5, y=4), Point(x=3, y=4)])


def test_geometry_picks_open_side():
    g = stop_geometry(ZONE, Grid(blocked="NSE"))  # 서쪽만 열림
    assert g.inside == (4.0, 3.0, pytest.approx(0.0))  # 동쪽(안쪽)을 본다
    assert g.outside[0] == pytest.approx(4 - 1 - 0.5) and g.outside[1] == pytest.approx(3.0)
    assert g.distance == pytest.approx(1.5)


def test_geometry_entry_theta_override_and_closed_error():
    g = stop_geometry(ZONE, Grid(), entry_theta=3.14159265)  # 문은 동쪽
    assert g.outside[0] == pytest.approx(5.5 + 0.0, abs=0.01) or g.outside[0] > 5
    with pytest.raises(ElevatorError):
        stop_geometry(ZONE, Grid(blocked="NSEW"))


@pytest.mark.parametrize('entry_theta',[None,3.141592653589793/6])
def test_rotated_elevator_uses_its_actual_door_side(entry_theta):
    import math
    from common.map_annotations import rectangle_polygon
    angle = math.pi/6
    class RotatedGrid:
        def is_free(self,x,y):
            dx,dy = x-4,y-3
            return Grid(blocked='NSE').is_free(4+math.cos(angle)*dx+math.sin(angle)*dy,
                                               3-math.sin(angle)*dx+math.cos(angle)*dy)
    zone = SimpleNamespace(polygon=rectangle_polygon(3,2,2,2,30))
    g = stop_geometry(zone,RotatedGrid(),entry_theta=entry_theta)
    assert g.distance == pytest.approx(1.5)
    assert g.outside == pytest.approx((4-1.5*math.cos(angle),3-1.5*math.sin(angle),angle))


# --- 다층 job -------------------------------------------------------------

class FakeMqtt:
    connected = True

    def __init__(self):
        self.orders, self.instants = [], []

    def publish_order(self, o):
        self.orders.append(o)

    def publish_instant(self, a):
        self.instants.append(a)


class FakeCtl:
    def __init__(self):
        self.cars, self.leases, self.log, self.published = {}, {}, [], []

    def state(self, eid):
        return self.cars.get(eid)

    def acquire(self, eid, owner):
        return self.leases.setdefault(eid, owner) == owner

    def lease_owner(self, eid):
        return self.leases.get(eid)

    def release_lease(self, eid, owner):
        if self.leases.get(eid) == owner:
            del self.leases[eid]

    def call(self, eid, m, hold=True):
        self.log.append(("call", eid, m))

    def go(self, eid, m, hold=True):
        self.log.append(("go", eid, m))

    def release(self, eid):
        self.log.append(("release", eid))

    def publish_json(self, topic, body):
        self.published.append((topic, body))

    def set_car(self, eid, at, door, held=False, moving=False):
        self.cars[eid] = CarState(eid, at, None, door, moving, held, time.time())


def _png():
    img = io.BytesIO()
    Image.new("L", (100, 80), 255).save(img, format="PNG")
    return img.getvalue()


def _upload(store, map_id, zone):
    files = {
        "fms_map.json": json.dumps({"name": map_id, "image": "grid.png", "resolution": 0.1, "origin_x": 0,
                                    "origin_y": 0, "width_px": 100, "height_px": 80}).encode(),
        "grid.png": _png(),
        "location_meta.json": json.dumps({"locations": [
            {"unique_id": f"{map_id}-a", "name": f"{map_id}-a", "type": "station", "pose": [1.0, 1.0, 0.0]}]}).encode(),
        "zone_meta.json": json.dumps({"zones": [{"id": "lift", "name": "리프트", "type": "elevator",
                                                 "polygon": zone}]}).encode(),
    }
    return store.ingest_upload(map_id, files, activate=True).version


@pytest.fixture
def env(tmp_path):
    registry = SiteRegistry(tmp_path)
    registry.create_site("s", "s")
    store = registry.map_store("s")
    zone = [[3, 2], [5, 2], [5, 4], [3, 4]]
    ver = {m: _upload(store, m, zone) for m in ("f1", "f3")}
    registry.register_robot("s", "AMR-001", "f1")
    locs = registry.location_store("s")
    locs.create("home", "f1", 1.0, 1.0, 0.0)
    locs.create("dest3", "f3", 8.0, 6.0, 0.0)
    elevators = ElevatorStore(tmp_path, registry)
    elevators.create("s", "elev-A", "A", [Stop("f1", "lift", "1F"), Stop("f3", "lift", "3F")])
    fleet, mqtt, ctl, jobs = Fleet(), FakeMqtt(), FakeCtl(), JobStore(tmp_path)
    coord = JobCoordinator(fleet, registry, jobs, mqtt, elevators=elevators, elevator_ctl=ctl)
    coord.start()
    fleet.apply_connection("AMR-001", ConnectionState.ONLINE)

    def robot(map_id="f1", state=RobotState.IDLE, order_id=None, loc=LocalizationState.LOCALIZED, **kw):
        fleet.apply_state(make_state("AMR-001", state, order_id=order_id, map_id=store.map_uid(map_id),
                                     map_version=ver[map_id], localization=loc, **kw))

    robot()
    yield SimpleNamespace(registry=registry, fleet=fleet, mqtt=mqtt, ctl=ctl, jobs=jobs, coord=coord,
                          robot=robot, store=store)
    coord.stop()


def finish_order(env, map_id="f1"):
    oid = env.mqtt.orders[-1].order_id
    env.robot(map_id, RobotState.BUSY, order_id=oid)
    env.robot(map_id)
    return oid


@pytest.mark.parametrize('traffic_limits',[False,True])
def test_multifloor_ride_full_cycle(env, traffic_limits):
    if traffic_limits:
        env.coord.traffic.set_limit('s','f1',1)
        env.coord.traffic.set_limit('s','f3',1)
    job = env.jobs.create("s", [{"type": "move", "target": "dest3"}])
    env.coord.dispatch(job.job_id, "AMR-001")
    assert [s["t"] for s in job.plan] == [
        "order", "call", "order", "ride", "order", "release", "switch", "localize", "order"]
    assert job.status == JobStatus.ASSIGNED
    # 1) 승강장 앞으로 이동 (호출은 도착한 뒤)
    first = env.mqtt.orders[0]
    assert first.order_id == f"{job.job_id}-s0" and first.nodes[0].action is None
    env.ctl.set_car("elev-A", "f3", P.DOOR_CLOSED)
    env.coord.tick_multifloor()
    assert not env.ctl.log
    finish_order(env)
    # 2) 호출하고 문이 열릴 때까지 기다린다
    assert ("call", "elev-A", "f1") in env.ctl.log and job.stage == 1
    env.ctl.set_car("elev-A", "f1", P.DOOR_OPENING)
    env.coord.tick_multifloor()
    assert job.stage == 1
    env.ctl.set_car("elev-A", "f1", P.DOOR_OPEN, held=True)
    env.coord.tick_multifloor()
    # 3) 전진 탑승
    assert job.stage == 2
    act = env.mqtt.orders[-1].nodes[0].action
    assert act.action_type.value == "STRAIGHT" and act.params["distance"] == pytest.approx(1.5)
    assert act.params["speed"] == pytest.approx(0.6)          # 승강기 탑승 속도
    finish_order(env)
    # 4) 층 이동: GO + 출발 층 hold 해제. 맵은 아직 안 바꾼다
    assert ("go", "elev-A", "f3") in env.ctl.log and ("release", "elev-A") in env.ctl.log
    if traffic_limits:
        env.coord.tick_traffic()
        assert 'floor:s/f1:AMR-001' not in env.coord.traffic.data['leases']
        assert env.coord.traffic.data['leases']['floor:s/f3:AMR-001']['future']
    assert env.registry.assignment_for("AMR-001").map_id == "f1"
    env.ctl.set_car("elev-A", None, P.DOOR_CLOSED, moving=True)
    env.coord.tick_multifloor()
    assert job.stage == 3
    env.ctl.set_car("elev-A", "f3", P.DOOR_OPEN, held=True)
    env.coord.tick_multifloor()
    # 5) 후진 하차 (출발 층 맵 좌표: 칸 안 자세에서 뒤로)
    assert job.stage == 4
    out = env.mqtt.orders[-1]
    assert out.nodes[0].position.x == pytest.approx(4.0)
    assert out.nodes[0].action.params["distance"] == pytest.approx(-1.5)
    assert out.nodes[0].action.params["speed"] == pytest.approx(0.6)   # 승강기 하차 속도
    oid = out.order_id
    env.robot("f1", RobotState.BUSY, order_id=oid)
    env.robot("f1", x=5.52, y=3.03)  # 하차 끝 (승강장 앞 5.5,3 에서 2 cm 오차)
    # 6) 승강기 놓기 -> 맵 변경
    assert env.ctl.lease_owner("elev-A") is None and job.stage == 6
    assert env.registry.assignment_for("AMR-001").map_id == "f3"
    assert not env.ctl.published  # 새 맵 받기 전에는 teleport 안 한다
    env.robot("f3", loc=LocalizationState.UNLOCALIZED)
    # 7) teleport (하차 오차 그대로) + 위치 힌트 (승강장 앞 공칭 자세)
    assert job.stage == 7
    topic, body = env.ctl.published[0]
    assert topic == "sim/v1/AMR-001/teleport" and body["map_id"]
    assert (body["x"], body["y"]) == (pytest.approx(5.52), pytest.approx(3.03))
    time.sleep(0.7)
    env.coord.tick_multifloor()
    hint = env.mqtt.instants[-1]
    assert hint.action_type == InstantActionType.INIT_POSITION and hint.params["x"] == pytest.approx(5.5)
    hint_at = job.stage_data["hint_at"]
    env.robot("f3", loc=LocalizationState.LOCALIZING)
    env.coord.tick_multifloor()
    assert job.stage == 7
    job.stage_data["hint_at"] = hint_at - 2.0
    env.robot("f3")
    # 8) 목적지
    assert job.stage == 8
    last = env.mqtt.orders[-1]
    assert (last.nodes[0].position.x, last.nodes[0].position.y) == (8.0, 6.0)
    finish_order(env, "f3")
    assert job.status == JobStatus.SUCCESS and job.done_commands == 1
    assert not env.coord.traffic.data['leases']


def test_destination_capacity_reserved_before_boarding_and_cancel_releases(env):
    env.coord.traffic.set_limit('s','f3',1)
    env.ctl.set_car('elev-A','f1',P.DOOR_CLOSED)
    env.registry.register_robot('s','AMR-002','f3')
    busy = make_state('AMR-002',RobotState.BUSY,order_id='external',
                      map_id=env.store.map_uid('f3'),map_version=env.store.active_version('f3'))
    env.fleet.apply_state(busy)
    job = env.jobs.create('s',[{'type':'move','target':'dest3'}])
    env.coord.dispatch(job.job_id,'AMR-001')
    finish_order(env)
    assert job.stage == 1
    assert not env.ctl.log  # 도착 층 정원이 없으면 승강기도 잡지 않는다.
    assert '탑승 전 대기' in job.error
    env.fleet.apply_state(busy.model_copy(update={'timestamp':time.time(),'state':RobotState.IDLE,
                                                 'sub_state':None,'order_id':None}))
    env.coord.tick_multifloor()
    leases = env.coord.traffic.data['leases']
    assert leases['floor:s/f3:AMR-001']['future']
    assert ('call','elev-A','f1') in env.ctl.log
    env.coord.cancel(job.job_id)
    env.robot()
    assert job.status == JobStatus.CANCELED
    assert not env.coord.traffic.data['leases']


def test_nearest_elevator_by_driving_distance(env, tmp_path):
    """직선거리는 가깝지만 벽 너머인 승강기보다 돌아가지 않는 승강기를 고른다."""
    from fms_server.route import path_distances

    class Walled:
        """x=5 에 y 0..7 벽 (y 7..8 만 열림)."""
        def __init__(self):
            import numpy as np
            self.metadata = SimpleNamespace(resolution=0.05, origin_x=0.0, origin_y=0.0)
            g = np.full((160, 200), 2, dtype=np.uint8)
            g[0:140, 98:102] = 0
            self._g = g

        def classify_grid(self):
            return self._g

    d = path_distances(Walled(), (4.0, 1.0), [(6.0, 1.0), (1.0, 1.0)])
    assert d[1] == pytest.approx(3.0, abs=0.3)
    assert d[0] > 12  # 벽 끝(y 7)까지 돌아간다


def test_route_transfer_pose():
    from fms_server.route import transfer_pose
    x, y, th = transfer_pose((1.1, 2.0, 0.05), (1.0, 2.0, 0.0), (10.0, 5.0, 1.5708))
    assert (x, y, th) == (pytest.approx(10.0), pytest.approx(5.1), pytest.approx(1.6208))


def test_elevator_busy_waits_then_fails_job_releases(env):
    env.ctl.leases["elev-A"] = "someone-else"
    job = env.jobs.create("s", [{"type": "move", "target": "dest3"}])
    env.coord.dispatch(job.job_id, "AMR-001")
    finish_order(env)
    assert job.stage == 1 and "다른 작업" in job.error
    assert not env.ctl.log
    del env.ctl.leases["elev-A"]
    env.ctl.set_car("elev-A", "f1", P.DOOR_CLOSED)
    env.coord.tick_multifloor()
    assert ("call", "elev-A", "f1") in env.ctl.log and env.ctl.lease_owner("elev-A") == job.job_id
    # 로봇 FATAL -> 실패하고 승강기를 놓는다
    from common.schemas import ErrorInfo, ErrorLevel
    s = make_state("AMR-001", RobotState.ERROR, map_id=env.store.map_uid("f1"), map_version=job.plan and
                   env.store.active_version("f1"))
    s.errors.append(ErrorInfo(error_type="COLLISION", level=ErrorLevel.FATAL, description="x"))
    env.fleet.apply_state(s)
    assert job.status == JobStatus.FAILED
    assert ("release", "elev-A") in env.ctl.log and env.ctl.lease_owner("elev-A") is None


def test_no_elevator_route_rejected(env):
    from fms_server.task import JobError
    env.registry.map_store("s")
    # f5 맵은 승강기가 안 선다
    _upload(env.store, "f5", [[3, 2], [5, 2], [5, 4], [3, 4]])
    env.registry.location_store("s").create("dest5", "f5", 8.0, 6.0, 0.0)
    job = env.jobs.create("s", [{"type": "move", "target": "dest5"}])
    with pytest.raises(JobError, match="승강기 연결이 없다"):
        env.coord.dispatch(job.job_id, "AMR-001")

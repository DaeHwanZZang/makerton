"""엘리베이터 연결 API: 층(맵)마다 그린 elevator 구역을 "같은 승강기" 로 묶는다."""
from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from fms_server.main import app
from fms_server.sites import SiteRegistry

SITE = "elev-test"
BASE = f"/api/v1/sites/{SITE}"


def _upload(store, map_id):
    image = io.BytesIO()
    Image.new("L", (80, 60), 255).save(image, format="PNG")
    files = {
        "fms_map.json": json.dumps({"name": map_id, "image": "grid.png", "resolution": 0.1,
                                    "origin_x": 0, "origin_y": 0, "width_px": 80, "height_px": 60}).encode(),
        "grid.png": image.getvalue(),
        "location_meta.json": json.dumps({"locations": []}).encode(),
        "zone_meta.json": json.dumps({"zones": []}).encode(),
    }
    return store.ingest_upload(map_id, files, activate=True).version


def _zone(client, map_id, version, name, kind="elevator", x=1.0):
    r = client.post(f"{BASE}/maps/{map_id}/zones",
                    json=dict(base_version=version, name=name, type=kind, x=x, y=1, width=2, height=2))
    assert r.status_code == 200, r.text
    return r.json()["zone_id"], r.json()["version"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    with TestClient(app) as client:
        registry = SiteRegistry(tmp_path)
        app.state.sites = registry
        app.state.coordinator._registry = registry
        app.state.broadcaster._registry = registry
        app.state.elevators = None  # 임시 registry/data_root 로 다시 만들게 한다
        app.state.settings = type("S", (), {"data_root": str(tmp_path)})()
        monkeypatch.setattr(app.state.coordinator, "notify_map_activated", lambda *a: [])
        registry.create_site(SITE, "엘리베이터 시험")
        store = registry.map_store(SITE)
        ver = {m: _upload(store, m) for m in ("f1", "f3", "f5")}
        z = {}
        z["f1a"], ver["f1"] = _zone(client, "f1", ver["f1"], "엘리베이터1", x=1)
        z["f1b"], ver["f1"] = _zone(client, "f1", ver["f1"], "엘리베이터2", x=4)
        z["f1x"], ver["f1"] = _zone(client, "f1", ver["f1"], "금지", kind="prohibit", x=6)
        z["f3a"], ver["f3"] = _zone(client, "f3", ver["f3"], "엘리베이터1", x=1)
        z["f5a"], ver["f5"] = _zone(client, "f5", ver["f5"], "엘리베이터1", x=1)
        yield client, z, ver, tmp_path


def stops(z, *pairs):
    return [{"map_id": m, "zone_id": z[k], "floor": f} for m, k, f in pairs]


def test_link_zones_into_one_elevator(env):
    client, z, _, tmp = env
    body = {"elevator_id": "elev-A", "name": "A동 1호기",
            "stops": stops(z, ("f1", "f1a", "1F"), ("f3", "f3a", "3F"), ("f5", "f5a", "5F"))}
    r = client.post(f"{BASE}/elevators", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert [s["floor"] for s in out["stops"]] == ["1F", "3F", "5F"]
    assert all(not s["missing"] and s["center"] for s in out["stops"])
    assert out["stops"][0]["zone_name"] == "엘리베이터1"
    assert (tmp / "sites" / SITE / "elevators.json").is_file()
    assert client.get(f"{BASE}/elevators").json()["elevators"][0]["elevator_id"] == "elev-A"
    # 재시작해도 남는다
    from fms_server.elevators import ElevatorStore
    assert [e.elevator_id for e in ElevatorStore(tmp, app.state.sites).list(SITE)] == ["elev-A"]


def test_unlinked_lists_only_free_elevator_zones(env):
    client, z, _, _ = env
    client.post(f"{BASE}/elevators", json={"elevator_id": "A", "stops": stops(z, ("f1", "f1a", ""), ("f3", "f3a", ""))})
    free = {(u["map_id"], u["zone_id"]) for u in client.get(f"{BASE}/elevators/unlinked").json()["zones"]}
    assert free == {("f1", z["f1b"]), ("f5", z["f5a"])}  # 금지구역/이미 묶인 구역은 없다


def test_rules_are_enforced(env):
    client, z, _, _ = env
    def post(eid, st):
        return client.post(f"{BASE}/elevators", json={"elevator_id": eid, "stops": st})
    assert post("A", stops(z, ("f1", "f1a", ""))).status_code == 422                       # 층 1개
    assert post("A", stops(z, ("f1", "f1a", ""), ("f1", "f1b", ""))).status_code == 422    # 같은 층 두 번
    assert post("A", stops(z, ("f1", "f1x", ""), ("f3", "f3a", ""))).status_code == 422    # 금지구역
    assert post("A", [{"map_id": "f1", "zone_id": z["f1a"]}, {"map_id": "f3", "zone_id": z["f5a"]}]).status_code == 422  # 다른 맵 구역
    assert post("A", stops(z, ("f1", "f1a", ""), ("nope", "f3a", ""))).status_code == 422  # 없는 맵
    assert post("bad id!", stops(z, ("f1", "f1a", ""), ("f3", "f3a", ""))).status_code == 422
    assert post("A", stops(z, ("f1", "f1a", ""), ("f3", "f3a", ""))).status_code == 200
    assert post("A", stops(z, ("f1", "f1b", ""), ("f5", "f5a", ""))).status_code == 409    # id 중복
    assert post("B", stops(z, ("f1", "f1a", ""), ("f5", "f5a", ""))).status_code == 409    # 구역이 이미 A 에 묶임
    assert client.get(f"/api/v1/sites/none/elevators").status_code == 404


def test_update_and_delete(env):
    client, z, _, _ = env
    client.post(f"{BASE}/elevators", json={"elevator_id": "A", "stops": stops(z, ("f1", "f1a", ""), ("f3", "f3a", ""))})
    r = client.put(f"{BASE}/elevators/A", json={"name": "새 이름", "stops": stops(z, ("f1", "f1a", "1F"), ("f3", "f3a", "3F"), ("f5", "f5a", "5F"))})
    assert r.status_code == 200 and r.json()["name"] == "새 이름" and len(r.json()["stops"]) == 3
    assert client.put(f"{BASE}/elevators/A", json={"name": "이름만"}).json()["stops"][2]["floor"] == "5F"
    assert client.put(f"{BASE}/elevators/NOPE", json={"name": "x"}).status_code == 404
    assert client.delete(f"{BASE}/elevators/A").status_code == 200
    assert client.get(f"{BASE}/elevators/A").status_code == 404
    assert client.get(f"{BASE}/elevators").json()["elevators"] == []


def test_deleted_zone_shows_as_missing_not_removed(env):
    client, z, ver, _ = env
    client.post(f"{BASE}/elevators", json={"elevator_id": "A", "stops": stops(z, ("f1", "f1a", ""), ("f3", "f3a", ""))})
    store = app.state.sites.map_store(SITE)
    r = client.delete(f"{BASE}/maps/f1/zones/{z['f1a']}", params={"base_version": store.active_version("f1")})
    assert r.status_code == 200, r.text
    out = client.get(f"{BASE}/elevators/A").json()
    assert [s["missing"] for s in out["stops"]] == [True, False]


def _register_runtime_elevator(client, z):
    response = client.post(f'{BASE}/elevators', json={
        'elevator_id': 'A', 'name': '1호기',
        'stops': stops(z, ('f1', 'f1a', '1F'), ('f3', 'f3a', '3F')),
    })
    assert response.status_code == 200, response.text


def test_runtime_waits_for_state_instead_of_guessing_first_floor(env):
    client, z, _, _ = env
    _register_runtime_elevator(client, z)
    runtime = client.get(f'{BASE}/elevators').json()['elevators'][0]['runtime']
    assert runtime['liveness'] == 'UNKNOWN'
    assert runtime['current_floor'] is None
    assert runtime['at'] is None and runtime['moving'] is None
    assert runtime['received_at'] is None


def test_simulator_mqtt_state_appears_in_runtime_api(env):
    from types import SimpleNamespace
    from common import elevator_proto as proto
    from fms_server.elevator_sim import ElevatorCar

    client, z, _, _ = env
    _register_runtime_elevator(client, z)
    car = ElevatorCar('A', ['f1', 'f3'], floor_time=1, door_time=1, open_time=1)
    controller = app.state.elevator_ctl

    def report():
        controller._on_message(None, None, SimpleNamespace(
            topic=proto.topic_state('A'), payload=json.dumps(car.state()).encode()))
        return client.get(f'{BASE}/elevators').json()['elevators'][0]['runtime']

    state = report()
    assert state['current_floor'] == '1F'
    assert state['liveness'] == 'ONLINE'
    assert state['door'] == 'CLOSED' and state['moving'] is False
    car.command(proto.CMD_CALL, 'f3', hold=True)
    car.step(.01)
    state = report()
    assert state['at'] is None and state['current_floor'] is None
    assert state['moving'] is True and state['target_floor'] == '3F'
    car.step(1.1)
    state = report()
    assert state['current_floor'] == '3F' and state['door'] == 'OPENING'
    car.step(1.1)
    state = report()
    assert state['door'] == 'OPEN' and state['held'] is True
    controller.acquire('A', 'job-1')
    assert client.get(f'{BASE}/elevators/A').json()['runtime']['lease_owner'] == 'job-1'
    car.command(proto.CMD_RELEASE)
    car.step(1.1)
    assert report()['door'] == 'CLOSING'
    car.step(1.1)
    assert report()['door'] == 'CLOSED'


def test_stale_runtime_preserves_last_floor_but_control_state_expires(env):
    import time
    from fms_server.elevator_ctl import CarState

    client, z, _, _ = env
    _register_runtime_elevator(client, z)
    controller = app.state.elevator_ctl
    controller.apply_state(CarState('A', 'f3', None, 'CLOSED', False, False, time.time()-30))
    runtime = client.get(f'{BASE}/elevators/A').json()['runtime']
    assert runtime['liveness'] == 'STALE'
    assert runtime['current_floor'] == '3F'
    assert runtime['door'] == 'CLOSED'
    assert controller.state('A') is None  # 제어 경로는 오래된 상태를 사용하지 않는다.
    controller.apply_state(CarState('not-registered', 'f1', None, 'OPEN', False, False, time.time()))
    assert len(client.get(f'{BASE}/elevators').json()['elevators']) == 1


def test_runtime_falls_back_to_map_id_for_unnamed_floor(env):
    import time
    from fms_server.elevator_ctl import CarState

    client, z, _, _ = env
    client.post(f'{BASE}/elevators', json={
        'elevator_id': 'A', 'stops': stops(z, ('f1', 'f1a', ''), ('f3', 'f3a', '')),
    })
    app.state.elevator_ctl.apply_state(CarState('A', 'f1', 'f3', 'CLOSED', False, False, time.time()))
    runtime = client.get(f'{BASE}/elevators').json()['elevators'][0]['runtime']
    assert runtime['current_floor'] == 'f1' and runtime['target_floor'] == 'f3'

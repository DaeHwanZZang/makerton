"""픽업스테이션 zone+node 동시 편집·검증·참조 보호 회귀."""
from __future__ import annotations

import io
import json
import math

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from common.map_annotations import LocationType, MapAnnotations, ZoneType
from fms_server.main import app
from fms_server.pickup_stations import list_stations
from fms_server.sites import SiteRegistry

SITE = '/api/v1/sites/ps'
BASE = SITE + '/maps/f1'


@pytest.fixture
def env(tmp_path, monkeypatch):
    with TestClient(app) as client:
        app.state.allocator.stop()
        registry = SiteRegistry(tmp_path)
        registry.create_site('ps', '픽업 현장')
        app.state.sites = registry
        app.state.coordinator._registry = registry
        app.state.broadcaster._registry = registry
        # 60x40 px, 0.1 m. 오른쪽 아래 (4~5, 1~2) 는 OCCUPIED 벽
        img = Image.new('L', (60, 40), 255)
        for px in range(50, 60):
            for py in range(30, 40):
                img.putpixel((px, py), 0)
        image = io.BytesIO()
        img.save(image, format='PNG')
        files = {
            'fms_map.json': json.dumps(dict(name='f1', image='grid.png', resolution=.1,
                                            origin_x=-1, origin_y=-2, width_px=60, height_px=40)).encode(),
            'grid.png': image.getvalue(),
            'zone_meta.json': json.dumps({'zones': [{'id': 'z1', 'name': '금지', 'type': 'prohibit',
                                                    'polygon': [[0, 0], [1, 0], [1, 1], [0, 1]]}]}).encode(),
        }
        store = registry.map_store('ps')
        result = store.ingest_upload('f1', files, activate=True)
        notifications = []
        monkeypatch.setattr(app.state.coordinator, 'notify_map_activated',
                            lambda site, map_id: notifications.append((site, map_id)) or ['AMR-001'])
        _clear_site_records()
        yield client, registry, store, result.version, notifications
        _clear_site_records()


def _clear_site_records():
    # 작업·프리셋 저장소는 테스트 세션 공용 데이터 루트다. 다른 테스트로 참조가 새지 않게 비운다.
    jobs = app.state.jobs
    with jobs._lock:
        for job in jobs.list('ps'):
            jobs._jobs.pop(job.job_id, None)
        jobs._persist_site('ps')
    for preset in app.state.presets.list('ps'):
        app.state.presets.delete('ps', preset['preset_id'])


def body(version, **changes):
    data = dict(base_version=version, name='보관소 A',
                zone=dict(x=2, y=-1, width=1.5, height=1),
                node=dict(x=2.5, y=0.5, theta=math.pi / 2))
    return data | changes


def create(client, version, **changes):
    r = client.post(BASE + '/pickup-stations', json=body(version, **changes))
    assert r.status_code == 200, r.text
    return r.json()


def test_create_links_zone_and_node_without_baking(env):
    client, registry, store, version, notifications = env
    before = store.load_map_model('f1').classify_grid().copy()
    out = create(client, version)
    assert out['station_id'].startswith('PS-') and out['map_changed']
    assert out['version'] != version and store.active_version('f1') == out['version']
    assert notifications == [('ps', 'f1')]

    zone = store.load_annotations('f1').zone_by_id(out['station_id'])
    assert zone.type == ZoneType.PICKUP and zone.name == '보관소 A'
    loc = registry.location_store('ps').get('보관소 A')
    assert loc.type == LocationType.PICKUP.value and loc.zone_id == out['station_id']
    assert (loc.x, loc.y, loc.theta) == (2.5, 0.5, math.pi / 2)
    # 표시용 zone 은 주행금지로 굽지 않는다.
    assert (store.load_map_model('f1').classify_grid() == before).all()

    listed = client.get(SITE + '/pickup-stations').json()
    assert [(s['station_id'], s['name'], s['status']) for s in listed] == [
        (out['station_id'], '보관소 A', 'ok')]
    assert listed[0]['node'] == {'x': 2.5, 'y': 0.5, 'theta': math.pi / 2}
    assert len(listed[0]['zone']['polygon']) == 4
    loc_out = client.get(SITE + '/locations/' + '보관소 A').json()
    assert loc_out['zone_id'] == out['station_id']


def test_node_outside_zone_is_allowed(env):
    client, registry, store, version, _ = env
    out = create(client, version, node=dict(x=1.5, y=-0.5, theta=0))
    assert registry.location_store('ps').get('보관소 A').x == 1.5
    assert client.get(SITE + '/pickup-stations').json()[0]['status'] == 'ok'
    assert out['station_id']


@pytest.mark.parametrize('node, message', [
    (dict(x=50, y=0, theta=0), '맵 범위'),
    (dict(x=4.5, y=1.5, theta=0), '주행 가능한 셀'),     # 벽
    (dict(x=0.5, y=0.5, theta=0), '주행 가능한 셀'),     # 주행금지구역
])
def test_rejects_unreachable_node(env, node, message):
    client, registry, store, version, notifications = env
    r = client.post(BASE + '/pickup-stations', json=body(version, node=node))
    assert r.status_code == 422 and message in r.json()['detail']
    assert store.active_version('f1') == version and notifications == []
    assert registry.location_store('ps').list() == []


def test_rejects_duplicate_name_stale_version_and_bad_zone(env):
    client, registry, store, version, _ = env
    first = create(client, version)
    r = client.post(BASE + '/pickup-stations', json=body(first['version']))
    assert r.status_code == 422 and '같은 이름' in r.json()['detail']
    r = client.post(BASE + '/pickup-stations', json=body(version, name='보관소 B'))
    assert r.status_code == 409
    r = client.post(BASE + '/pickup-stations', json=body(first['version'], name='보관소 B',
                                                          zone=dict(x=4.9, y=0, width=2, height=1)))
    assert r.status_code == 422 and '맵 범위' in r.json()['detail']
    assert [s['name'] for s in client.get(SITE + '/pickup-stations').json()] == ['보관소 A']


def test_update_node_only_keeps_map_version(env):
    client, registry, store, version, notifications = env
    out = create(client, version)
    notifications.clear()
    r = client.put(f"{BASE}/pickup-stations/{out['station_id']}", json={
        'base_version': out['version'], 'zone': dict(x=2, y=-1, width=1.5, height=1),
        'node': dict(x=3, y=0.5, theta=0)})
    assert r.status_code == 200, r.text
    assert r.json()['map_changed'] is False and r.json()['version'] == out['version']
    assert notifications == []
    assert registry.location_store('ps').get('보관소 A').x == 3

    r = client.put(f"{BASE}/pickup-stations/{out['station_id']}", json={
        'base_version': out['version'], 'zone': dict(x=2, y=-1.5, width=1, height=1),
        'node': dict(x=3, y=0.5, theta=0)})
    assert r.status_code == 200 and r.json()['map_changed'] is True
    assert notifications == [('ps', 'f1')]
    zone = store.load_annotations('f1').zone_by_id(out['station_id'])
    assert zone.polygon[0].y == -1.5


def test_generic_apis_cannot_split_station(env):
    client, registry, store, version, _ = env
    out = create(client, version)
    sid, v = out['station_id'], out['version']
    assert client.post(SITE + '/locations', json=dict(name='가짜', map_id='f1', x=2, y=0,
                                                      type='pickup')).status_code == 422
    assert client.delete(SITE + '/locations/' + '보관소 A').status_code == 422
    r = client.put(SITE + '/locations/' + '보관소 A', json=dict(x=3))
    assert r.status_code == 422
    r = client.put(SITE + '/locations/' + '보관소 A', json=dict(type='station'))
    assert r.status_code == 422
    r = client.put(f'{BASE}/zones/{sid}', json=dict(base_version=v, name='x', type='prohibit',
                                                     x=0, y=0, width=1, height=1))
    assert r.status_code == 422
    assert client.delete(f'{BASE}/zones/{sid}', params={'base_version': v}).status_code == 422
    assert client.get(SITE + '/pickup-stations').json()[0]['status'] == 'ok'


def test_rename_keeps_station_id(env):
    client, registry, store, version, _ = env
    out = create(client, version)
    loc = registry.location_store('ps').get('보관소 A')
    # 웹 목적지 편집은 좌표를 같이 보낸다. 값이 같으면 이름 변경만으로 본다.
    r = client.put(SITE + '/locations/' + '보관소 A', json=dict(
        name='보관소 1', map_id='f1', x=loc.x, y=loc.y, theta=loc.theta, type='pickup'))
    assert r.status_code == 200, r.text
    listed = client.get(SITE + '/pickup-stations').json()
    assert [(s['station_id'], s['name'], s['status']) for s in listed] == [
        (out['station_id'], '보관소 1', 'ok')]
    assert store.active_version('f1') == out['version']


def test_delete_blocked_by_references_then_removes_both(env):
    client, registry, store, version, notifications = env
    out = create(client, version)
    sid, v = out['station_id'], out['version']
    preset = client.post(SITE + '/presets', json={'name': '적재', 'commands': [
        {'type': 'move', 'target': '보관소 A'}]})
    assert preset.status_code == 200, preset.text
    r = client.delete(f'{BASE}/pickup-stations/{sid}', params={'base_version': v})
    assert r.status_code == 409 and '프리셋' in r.json()['detail']
    client.delete(SITE + '/presets/' + preset.json()['preset_id'])

    job = app.state.jobs.create('ps', [{'type': 'move', 'target': '보관소 A'}])
    r = client.delete(f'{BASE}/pickup-stations/{sid}', params={'base_version': v})
    assert r.status_code == 409 and job.job_id in r.json()['detail']
    app.state.jobs.delete(job.job_id)

    notifications.clear()
    r = client.delete(f'{BASE}/pickup-stations/{sid}', params={'base_version': v})
    assert r.status_code == 200, r.text
    assert notifications == [('ps', 'f1')]
    assert store.load_annotations('f1').zone_by_id(sid) is None
    assert registry.location_store('ps').list() == []
    assert client.get(SITE + '/pickup-stations').json() == []


def test_node_move_blocked_while_job_active(env):
    client, registry, store, version, _ = env
    out = create(client, version)
    job = app.state.jobs.create('ps', [{'type': 'move', 'target': '보관소 A'}])
    job.status = type(job.status)('working')
    app.state.jobs.save(job)
    r = client.put(f"{BASE}/pickup-stations/{out['station_id']}", json={
        'base_version': out['version'], 'zone': dict(x=2, y=-1, width=1.5, height=1),
        'node': dict(x=3, y=0.5, theta=0)})
    assert r.status_code == 409
    # zone 만 바꾸는 것은 허용
    r = client.put(f"{BASE}/pickup-stations/{out['station_id']}", json={
        'base_version': out['version'], 'zone': dict(x=2, y=-1, width=2, height=1),
        'node': dict(x=2.5, y=0.5, theta=math.pi / 2)})
    assert r.status_code == 200, r.text


def test_broken_link_is_reported_and_cleanable(env):
    client, registry, store, version, _ = env
    out = create(client, version)
    sid = out['station_id']
    # 프로세스 중단으로 zone 은 사라지고 node 만 남은 상태를 흉내낸다.
    store.edit_zone('f1', out['version'], sid)
    stations = list_stations(store, registry.location_store('ps'))
    assert len(stations) == 1 and not stations[0].ok
    assert '픽업 구역' in stations[0].problems[0]
    v = store.active_version('f1')
    r = client.put(f'{BASE}/pickup-stations/{sid}', json={
        'base_version': v, 'zone': dict(x=2, y=-1, width=1, height=1), 'node': dict(x=2.5, y=0.5)})
    assert r.status_code == 422 and '손상' in r.json()['detail']
    r = client.delete(f'{BASE}/pickup-stations/{sid}', params={'base_version': v})
    assert r.status_code == 200, r.text
    assert r.json()['map_changed'] is False
    assert registry.location_store('ps').list() == []


def test_unknown_station_404(env):
    client, _, _, version, _ = env
    r = client.put(f'{BASE}/pickup-stations/PS-none', json={
        'base_version': version, 'zone': dict(x=2, y=-1, width=1, height=1), 'node': dict(x=2.5, y=0.5)})
    assert r.status_code == 404


def test_pickup_zone_type_round_trips():
    data = {'map_id': 'm', 'map_version': 'v', 'zones': [
        {'zone_id': 'PS-1', 'type': 'pickup', 'polygon': [{'x': 0, 'y': 0}, {'x': 1, 'y': 0}, {'x': 1, 'y': 1}]}]}
    ann = MapAnnotations.model_validate(data)
    assert ann.zones[0].type == ZoneType.PICKUP
    assert ann.prohibit_zones() == []

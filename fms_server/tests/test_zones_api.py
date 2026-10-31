"""사각형 구역 저장 → 재굽기 → 새 버전 활성화/로봇 알림 회귀 검증."""
from __future__ import annotations

import io
import json
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from common.map_annotations import ZoneType
from fms_server.api.zones import RectangleZoneRequest
from fms_server.main import app
from fms_server.mapkit.store import MapVersionConflict
from fms_server.sites import SiteRegistry

BASE = '/api/v1/sites/zone-test/maps/hall'


@pytest.fixture
def setup(tmp_path, monkeypatch):
    with TestClient(app) as client:
        registry = SiteRegistry(tmp_path)
        app.state.sites = registry
        app.state.coordinator._registry = registry
        app.state.broadcaster._registry = registry
        registry.create_site('zone-test', '구역 테스트')
        store = registry.map_store('zone-test')
        image = io.BytesIO()
        Image.new('L', (80, 60), 255).save(image, format='PNG')
        files = {
            'fms_map.json': json.dumps({
                'name': 'hall', 'image': 'grid.png', 'resolution': 0.1,
                'origin_x': -1, 'origin_y': -2, 'width_px': 80, 'height_px': 60,
            }).encode(),
            'grid.png': image.getvalue(),
            'location_meta.json': json.dumps({'locations': [
                {'unique_id': 'loc1', 'name': '작업대', 'type': 'station', 'pose': [5, 2, 0]}
            ]}).encode(),
            'zone_meta.json': json.dumps({'zones': [
                {'id': 'original', 'name': '원본 다각형', 'type': 'slow',
                 'polygon': [[4, 0], [5, 0], [5, 1]]}
            ]}).encode(),
        }
        result = store.ingest_upload('hall', files, activate=True)
        notifications = []
        def notify(site, map_id):
            notifications.append((site, map_id))
            return ['AMR-001']
        monkeypatch.setattr(app.state.coordinator, 'notify_map_activated', notify)
        yield client, store, result.version, notifications


def rectangle(version, kind='prohibit', **changes):
    body = dict(base_version=version, name='새 구역', type=kind, x=0, y=0, width=1, height=1)
    return body | changes


def test_forbidden_rectangle_resize_delete_restores_grid(setup):
    client, store, original_version, notifications = setup
    original_codes = store.load_map_model('hall').classify_grid().copy()
    original_bundle = store.bundle_zip('hall', original_version)
    r = client.post(BASE+'/zones', json=rectangle(original_version))
    assert r.status_code == 200, r.text
    added = r.json()
    assert added['notified_robots'] == ['AMR-001']
    assert added['version'] != original_version
    assert store.active_version('hall') == added['version']
    assert store.load_map_model('hall').cell_at(.5, .5).value == 'OCCUPIED'
    annotations = store.load_annotations('hall')
    assert len(annotations.zones) == 2
    assert annotations.zones[0].zone_id == 'original'
    assert annotations.locations[0].name == '작업대'
    assert store.bundle_zip('hall', original_version) == original_bundle

    # 기존 구역의 크기를 바꾸면 이전 면적이 grid에 잔존하지 않는다.
    r = client.put(BASE+'/zones/'+added['zone_id'], json=rectangle(
        added['version'], x=1, y=0, width=2, height=2))
    assert r.status_code == 200, r.text
    resized = r.json()
    model = store.load_map_model('hall')
    assert model.cell_at(.5, .5).value == 'FREE'
    assert model.cell_at(2, 1).value == 'OCCUPIED'
    assert store.verify_integrity('hall') == []

    # MapStore를 다시 만들어도 저장한 구역을 읽을 수 있다.
    reloaded = app.state.sites.map_store('zone-test')
    assert reloaded.load_annotations('hall').zones[-1].zone_id == added['zone_id']
    r = client.delete(BASE+'/zones/'+added['zone_id'], params={'base_version': resized['version']})
    assert r.status_code == 200, r.text
    assert np.array_equal(store.load_map_model('hall').classify_grid(), original_codes)
    assert [z.zone_id for z in store.load_annotations('hall').zones] == ['original']
    assert len(notifications) == 3


def test_elevator_is_preserved_and_does_not_block_grid(setup):
    client, store, version, _ = setup
    codes = store.load_map_model('hall').classify_grid().copy()
    r = client.post(BASE+'/zones', json=rectangle(version, 'elevator', name='엘리베이터 1'))
    assert r.status_code == 200, r.text
    zone = store.load_annotations('hall').zones[-1]
    assert zone.type is ZoneType.ELEVATOR
    assert np.array_equal(store.load_map_model('hall').classify_grid(), codes)
    ann = client.get(BASE+'/files/annotations.json').json()
    assert ann['zones'][-1]['type'] == 'elevator'
    assert ann['map_version'] == r.json()['version']
    # 유형 변경도 맵에 반영한다.
    changed = client.put(BASE+'/zones/'+zone.zone_id, json=rectangle(r.json()['version']))
    assert changed.status_code == 200
    assert store.load_map_model('hall').cell_at(.5, .5).value == 'OCCUPIED'


@pytest.mark.parametrize('change', [
    {'width': 0}, {'height': -1}, {'width': .01}, {'x': -2}, {'x': 6.5},
    {'type': 'unknown'}, {'name': ''}, {'extra': True},
])
def test_invalid_rectangle_does_not_change_map(setup, change):
    client, store, version, notifications = setup
    r = client.post(BASE+'/zones', json=rectangle(version, **change))
    assert r.status_code == 422, r.text
    assert store.active_version('hall') == version
    assert len(store.get_map('hall').versions) == 1
    assert notifications == []


def test_stale_edit_and_missing_zone_are_rejected(setup):
    client, store, version, notifications = setup
    r = client.post(BASE+'/zones', json=rectangle(version))
    active = r.json()['version']
    assert client.post(BASE+'/zones', json=rectangle(version)).status_code == 409
    assert client.delete(BASE+'/zones/absent', params={'base_version': active}).status_code == 404
    assert client.put(BASE+'/zones/absent', json=rectangle(active)).status_code == 404
    assert store.active_version('hall') == active
    assert len(notifications) == 1


def test_parallel_edit_does_not_lose_zone(setup):
    _, store, version, _ = setup
    zone = RectangleZoneRequest(**rectangle(version)).zone('concurrent')
    # 실제 서버와 같이 서로 다른 MapStore 인스턴스에서 동시에 저장한다.
    def edit(_):
        other = app.state.sites.map_store('zone-test')
        try:
            other.edit_zone('hall', version, zone.zone_id, zone, create=True)
            return 'saved'
        except MapVersionConflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(edit, range(2))) == ['conflict', 'saved']
    assert len(store.load_annotations('hall').zones) == 2

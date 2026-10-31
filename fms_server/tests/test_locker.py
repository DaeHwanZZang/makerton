"""보관소 배송: 실제 작업 코디네이터로 2개 묶음·트레이·물품별 PICK/DROP·트레이 반납·장부를 검증한다."""
import io
import json

import pytest
from PIL import Image

from common.schemas import ActionProgress, ActionStatus, ConnectionState, RobotState
from fms_server.cargo import TRAY_RETURN_PREFIX
from fms_server.fleet import Fleet
from fms_server.locker import LockerBridge, LockerConfig
from fms_server.pickup_stations import Rect, create_station
from fms_server.sites import SiteRegistry
from fms_server.task import JobCoordinator, JobError, JobStatus, JobStore
from fms_server.tests.factories import make_state


class Mqtt:
    connected = True

    def __init__(self):
        self.orders, self.events, self.handlers = [], [], {}

    def subscribe_json(self, topic, handler):
        self.handlers[topic] = handler

    def publish_json(self, topic, payload, retain=False):
        self.events.append((topic, payload))

    def publish_order(self, order):
        self.orders.append(order)

    def publish_instant(self, action):
        pass


@pytest.fixture
def env(tmp_path):
    registry = SiteRegistry(tmp_path)
    registry.create_site('hq', '단지')
    stream = io.BytesIO()
    Image.new('L', (100, 100), 255).save(stream, format='PNG')
    files = {'fms_map.json': json.dumps(dict(name='hall', image='grid.png', resolution=.1,
             origin_x=0, origin_y=0, width_px=100, height_px=100)).encode(), 'grid.png': stream.getvalue()}
    result = registry.map_store('hq').ingest_upload('f1', files, activate=True)
    registry.register_robot('hq', 'R1', 'f1')
    locations = registry.location_store('hq')
    locations.create('locker', 'f1', 1, 1, 0)
    locations.create('home', 'f1', 3, 3, 0)
    locations.create('home2', 'f1', 4, 4, 0)
    fleet, mqtt, jobs = Fleet(), Mqtt(), JobStore(tmp_path)
    coord = JobCoordinator(fleet, registry, jobs, mqtt)
    coord.start()
    state = make_state('R1', map_id=result.map_uid, map_version=result.version)
    fleet.apply_connection('R1', ConnectionState.ONLINE)
    fleet.apply_state(state)
    bridge = LockerBridge(registry, fleet, jobs, coord, mqtt, tmp_path)
    bridge.configure(LockerConfig(site_id='hq', pickup_target='locker',
                                  households={'101-101': 'home', '101-102': 'home2'}))
    yield bridge, coord, jobs, mqtt, fleet, state, tmp_path
    coord.stop()


def items(*receipts, trays=('TRAY_A', 'TRAY_B'), ho=(101, 102)):
    return [{'receipt_no': r, 'dong': 101, 'ho': ho[i % len(ho)], 'size': 'S', 'tray': trays[i % len(trays)]}
            for i, r in enumerate(receipts)]


def request(bridge, job_id='J1', receipts=('p1', 'p2')):
    bridge.receive({'type': 'DELIVERY_JOB', 'job_id': job_id, 'items': items(*receipts)})
    bridge.tick()  # 로봇 예약
    bridge.tick()  # 보관소 이동 발행
    return bridge.deliveries[job_id]


def report(env, statuses, *, clear=True):
    """현재 하위 작업 오더의 액션 상태를 보고한다. statuses 는 액션 노드 순서대로."""
    bridge, coord, jobs, mqtt, fleet, state, _ = env
    d = next(d for d in bridge.deliveries.values() if d.get('child_id') and d['phase'] != 'done')
    job = jobs.get(d['child_id'])
    order = mqtt.orders[-1]
    actions = [n.action for n in order.nodes if n.action]
    progress = [ActionProgress(action_id=a.action_id, status=statuses[i] if i < len(statuses) else ActionStatus.WAITING)
                for i, a in enumerate(actions)]
    fleet.apply_state(state.model_copy(update={
        'state': RobotState.BUSY, 'sub_state': 'ACTING', 'order_id': job.order_id,
        'last_node_id': order.nodes[-1].node_id, 'last_node_sequence_id': len(order.nodes) - 1,
        'action_states': progress}))
    bridge.tick()  # 진행 중 관측 (RUNNING/FINISHED → 장부)
    if clear:
        fleet.apply_state(state.model_copy(update={'order_id': None}))
        bridge.tick()
    return job


def finish(env, *, finished=True):
    bridge, *_ , mqtt = env[0], env[3]
    count = sum(1 for n in mqtt.orders[-1].nodes if n.action)
    return report(env, [ActionStatus.FINISHED if finished else ActionStatus.RUNNING] * count)


def confirmed(bridge, values, job_id='J1'):
    bridge.receive({'type': 'BUNDLE_CONFIRMED', 'job_id': job_id, 'robot_id': 'R1', 'items': values})
    bridge.tick()


def payloads(order):
    return [(n.action.action_type.value, n.action.payload_id) for n in order.nodes
            if n.action and n.action.action_type.value != 'WAIT']


def results(mqtt):
    return [(p['receipt_no'], p['result']) for t, p in mqtt.events if p.get('type') == 'DELIVERY_RESULT']


def test_full_delivery_two_trays_drop_each_and_return_trays(env):
    bridge, coord, jobs, mqtt, fleet, state, path = env
    cargo = bridge.cargo
    d = request(bridge)
    ordinary = jobs.create('hq', [{'type': 'move', 'target': 'home'}])
    with pytest.raises(JobError, match='예약'):
        coord.dispatch(ordinary.job_id, 'R1')
    finish(env)
    assert d['phase'] == 'awaiting_bundle'
    assert mqtt.events[-1][1]['type'] == 'ROBOT_ARRIVED'

    # 예상 목록과 다르게 직접 수령된 p1 대신 p3 가 들어간 최종 묶음
    confirmed(bridge, items('p3', 'p2'))
    bundle = cargo.bundle(d['bundle_id'])
    tray = {i['tray_id']: i for i in bundle['items']}
    assert (tray['TRAY_A']['receipt_no'], tray['TRAY_B']['receipt_no']) == ('p3', 'p2')
    a, b = tray['TRAY_A']['parcel_id'], tray['TRAY_B']['parcel_id']
    assert a.startswith('P-') and bundle['bundle_id'].startswith('B-')
    # 장부 저장이 PICK 발행보다 먼저다.
    assert cargo.parcel(a)['status'] == 'planned' and cargo.parcel(a)['assigned_robot_id'] == 'R1'
    assert cargo.parcel(a)['onboard_robot_id'] is None
    assert payloads(mqtt.orders[-1]) == [('PICK', a), ('PICK', b)]
    assert (path / 'cargo.json').is_file()

    # 첫 PICK 만 진행 중일 때 → loading, 출발 안 함
    report(env, [ActionStatus.RUNNING], clear=False)
    assert cargo.parcel(a)['status'] == 'loading' and cargo.parcel(b)['status'] == 'planned'
    assert d['phase'] == 'loading'
    finish(env)
    assert [cargo.parcel(p)['status'] for p in (a, b)] == ['onboard', 'onboard']
    assert d['phase'] == 'delivering'
    trays = {t['tray_id']: t for t in cargo.robot_cargo('R1')['trays']}
    assert trays['TRAY_A']['state'] == trays['TRAY_B']['state'] == 'occupied'

    bridge.tick()
    assert payloads(mqtt.orders[-1]) == [('DROP', a)]          # 101-101 home 먼저
    report(env, [ActionStatus.RUNNING], clear=False)
    assert cargo.parcel(a)['status'] == 'unloading'
    finish(env)
    parcel = cargo.parcel(a)
    assert parcel['status'] == 'delivered'
    assert parcel['drop_position'] == {'map_id': 'f1', 'x': 3.0, 'y': 3.0, 'theta': 0.0, 'target': 'home'}
    assert cargo.parcel(b)['status'] == 'onboard'             # 다른 트레이는 그대로
    assert cargo.bundle(d['bundle_id'])['progress'] == '1/2'
    trays = {t['tray_id']: t['state'] for t in cargo.robot_cargo('R1')['trays']}
    assert trays == {'TRAY_A': 'empty', 'TRAY_B': 'occupied'}
    assert results(mqtt) == [('p3', 'DELIVERED')]

    bridge.tick()
    assert payloads(mqtt.orders[-1]) == [('DROP', b)]
    finish(env)
    assert d['phase'] == 'returning'
    with pytest.raises(JobError):
        coord.dispatch(ordinary.job_id, 'R1')

    bridge.tick()
    order = mqtt.orders[-1]
    assert payloads(order) == [('DROP', f"{TRAY_RETURN_PREFIX}{d['bundle_id']}")]
    assert (order.nodes[0].position.x, order.nodes[0].position.y) == (1, 1)   # 보관소 node
    finish(env)
    assert d['phase'] == 'done'
    view = cargo.bundle(d['bundle_id'])
    assert (view['status'], view['summary'], view['progress']) == ('closed', '배송 완료', '2/2')
    assert all(t['state'] == 'empty' for t in cargo.robot_cargo('R1')['trays'])
    assert mqtt.events[-1][1]['type'] == 'JOB_DONE'
    coord.dispatch(ordinary.job_id, 'R1')
    before = len(mqtt.orders)
    bridge.receive({'type': 'DELIVERY_JOB', 'job_id': 'J1', 'items': items('p1', 'p2')})
    assert len(mqtt.orders) == before
    assert mqtt.events[-1][1]['type'] == 'JOB_DONE'


def test_single_item_final_bundle_does_not_pick(env):
    bridge, coord, jobs, mqtt, *_ = env
    d = request(bridge)
    finish(env)
    before = len(mqtt.orders)
    confirmed(bridge, items('p1'))
    assert d['phase'] == 'blocked' and '2개' in d['error']
    assert len(mqtt.orders) == before
    assert bridge.cargo.parcels('hq') == []


def test_same_tray_twice_is_rejected(env):
    bridge, *_ = env
    d = request(bridge)
    finish(env)
    confirmed(bridge, items('p1', 'p2', trays=('TRAY_A', 'TRAY_A')))
    assert d['phase'] == 'blocked' and '트레이' in d['error']
    assert bridge.cargo.parcels('hq') == []


def test_partial_pick_blocks_then_return_marks_returned(env):
    bridge, coord, jobs, mqtt, fleet, state, _ = env
    cargo = bridge.cargo
    d = request(bridge)
    finish(env)
    confirmed(bridge, items('p1', 'p2'))
    a, b = d['parcels']
    # 첫 PICK 완료, 두 번째 PICK 진행 중에 오더가 끝남 → FINISHED 누락
    job = report(env, [ActionStatus.FINISHED, ActionStatus.RUNNING])
    assert job.status == JobStatus.FAILED
    assert d['phase'] == 'blocked'
    assert (cargo.parcel(a)['status'], cargo.parcel(b)['status']) == ('onboard', 'unknown')
    assert cargo.bundle(d['bundle_id'])['status'] == 'blocked'
    assert results(mqtt) == []                                  # 부분 적재로 출발하지 않음
    with pytest.raises(JobError):
        bridge.recover_return('J1', [], False)
    bridge.recover_return('J1', [], True)
    assert cargo.parcel(b)['status'] == 'onboard'               # 운영자 확인: 로봇 적재로 정정
    bridge.tick()
    assert payloads(mqtt.orders[-1])[0][1].startswith(TRAY_RETURN_PREFIX)
    finish(env)
    assert d['phase'] == 'done'
    assert [cargo.parcel(p)['status'] for p in (a, b)] == ['returned', 'returned']
    assert cargo.bundle(d['bundle_id'])['summary'] == '미배송 반납'
    assert sorted(results(mqtt)) == [('p1', 'FAILED'), ('p2', 'FAILED')]


def test_drop_without_finished_is_unknown_until_operator_confirms(env):
    bridge, coord, jobs, mqtt, fleet, state, _ = env
    cargo = bridge.cargo
    d = request(bridge)
    finish(env)
    confirmed(bridge, items('p1', 'p2', ho=(101, 101)))       # 같은 세대 → 한 정지점에서 DROP 두 번
    finish(env)
    bridge.tick()
    assert [kind for kind, _ in payloads(mqtt.orders[-1])] == ['DROP', 'DROP']
    a, b = d['parcels']
    job = report(env, [ActionStatus.FINISHED, ActionStatus.RUNNING])
    assert job.status == JobStatus.FAILED and d['phase'] == 'blocked'
    assert (cargo.parcel(a)['status'], cargo.parcel(b)['status']) == ('delivered', 'unknown')
    assert not results(mqtt)                                     # 세대 결과는 하위 작업 성공 후에만
    bridge.recover_return('J1', ['p1'], True)
    assert cargo.parcel(b)['status'] == 'onboard'
    bridge.tick()
    finish(env)
    assert d['results'] == {'p1': 'DELIVERED', 'p2': 'FAILED'}
    assert (cargo.parcel(a)['status'], cargo.parcel(b)['status']) == ('delivered', 'returned')
    assert cargo.bundle(d['bundle_id'])['summary'] == '일부 배송'


def test_retry_reuses_parcel_id_and_busy_trays_block_new_bundle(env):
    bridge, coord, jobs, mqtt, fleet, state, _ = env
    cargo = bridge.cargo
    d = request(bridge)
    finish(env)
    confirmed(bridge, items('p1', 'p2'))
    first = list(d['parcels'])
    report(env, [ActionStatus.FINISHED, ActionStatus.RUNNING])
    bridge.recover_return('J1', [], True)
    bridge.tick()
    finish(env)
    assert d['phase'] == 'done'
    # 실패 물품이 다시 배송되면 같은 parcel_id 를 쓰고 새 묶음 ID 를 받는다.
    d2 = request(bridge, 'J2', ('p1', 'p3'))
    finish(env)
    confirmed(bridge, items('p1', 'p3'), job_id='J2')
    parcel = cargo.parcel(first[0])
    assert parcel['status'] == 'planned' and parcel['attempts'] == 2
    assert parcel['bundle_id'] == d2['bundle_id'] != d['bundle_id']
    assert d2['parcels'][0] == first[0]


def test_restart_keeps_reservation_cargo_and_does_not_republish_order(env):
    bridge, coord, jobs, mqtt, fleet, state, path = env
    d = request(bridge)
    finish(env)
    confirmed(bridge, items('p1', 'p2'))
    count = len(mqtt.orders)
    restored = LockerBridge(bridge.registry, fleet, JobStore(path), coord, mqtt, path)
    restored.tick()
    assert len(mqtt.orders) == count
    assert restored.cargo.bundle(d['bundle_id'])['status'] == 'loading'
    assert [t['state'] for t in restored.cargo.robot_cargo('R1')['trays']] == ['reserved', 'reserved']
    ordinary = jobs.create('hq', [{'type': 'move', 'target': 'home'}])
    with pytest.raises(JobError):
        coord.dispatch(ordinary.job_id, 'R1')


def test_duplicate_confirmation_and_cancel_empty_load(env):
    bridge, coord, jobs, mqtt, fleet, state, _ = env
    d = request(bridge)
    finish(env)
    bridge.receive({'type': 'JOB_CANCELLED', 'job_id': 'J1'})
    bridge.tick()
    assert d['phase'] == 'done'
    assert 'R1' not in coord._reservations


def test_wrong_robot_confirmation_is_rejected(env):
    bridge, *_ = env
    request(bridge)
    finish(env)
    with pytest.raises(JobError):
        bridge.receive({'type': 'BUNDLE_CONFIRMED', 'job_id': 'J1', 'robot_id': 'OTHER', 'items': items('p1', 'p2')})
    assert bridge.deliveries['J1']['phase'] == 'awaiting_bundle'


def test_missing_mapping_waits_for_configuration(env):
    bridge, *_ = env
    bridge.receive({'type': 'DELIVERY_JOB', 'job_id': 'J1', 'items': [{'receipt_no': 'p1', 'dong': 102, 'ho': 101}]})
    bridge.tick()
    d = bridge.deliveries['J1']
    assert d['phase'] == 'queued' and d['robot_id'] is None
    bridge.configure(LockerConfig(site_id='hq', pickup_target='locker', households={'102-101': 'home'}))
    bridge.tick()
    assert d['phase'] == 'to_pickup'


def test_final_bundle_missing_target_blocks_with_items_preserved(env):
    bridge, *_ = env
    d = request(bridge)
    finish(env)
    confirmed(bridge, [{'receipt_no': 'p1', 'dong': 102, 'ho': 101, 'tray': 'TRAY_A'},
                       {'receipt_no': 'p2', 'dong': 101, 'ho': 101, 'tray': 'TRAY_B'}])
    assert d['phase'] == 'blocked'
    assert d['items'][0]['receipt_no'] == 'p1'
    assert bridge.coordinator._reservations['R1'] == 'J1'
    assert bridge.cargo.parcels('hq') == []


def test_pickup_station_reference_drives_pickup_and_tray_return(env):
    bridge, coord, jobs, mqtt, fleet, state, _ = env
    registry = bridge.registry
    maps, locations = registry.map_store('hq'), registry.location_store('hq')
    station = create_station(maps, locations, 'f1', maps.active_version('f1'), '보관소 A',
                             Rect(5, 5, 1, 1), 6.5, 5.5, 0)
    # 맵 버전이 바뀌었으므로 로봇이 새 버전을 보고한 상태로 맞춘다.
    state = state.model_copy(update={'map_version': station.version})
    fleet.apply_state(state)
    env = (bridge, coord, jobs, mqtt, fleet, state, env[6])
    with pytest.raises(ValueError):
        LockerConfig(site_id='hq', households={'101-101': 'home'})
    with pytest.raises(ValueError):
        LockerConfig(site_id='hq', pickup_target='locker', pickup_station_id=station.station_id,
                     households={'101-101': 'home'})
    bridge.configure(LockerConfig(site_id='hq', pickup_station_id=station.station_id,
                                  households={'101-101': 'home', '101-102': 'home2'}))
    d = request(bridge)
    assert d['pickup_name'] == '보관소 A'
    assert (mqtt.orders[-1].nodes[0].position.x, mqtt.orders[-1].nodes[0].position.y) == (6.5, 5.5)
    finish(env)
    confirmed(bridge, items('p1', 'p2'))
    parcel = bridge.cargo.parcel(d['parcels'][0])
    assert parcel['pickup_station_id'] == station.station_id
    assert (mqtt.orders[-1].nodes[0].position.x, mqtt.orders[-1].nodes[0].position.y) == (6.5, 5.5)
    # 스테이션 이름을 바꿔도 불변 ID 로 계속 찾는다.
    locations.update('보관소 A', new_name='보관소 1')
    assert bridge._pickup_name(bridge.config.model_dump()) == '보관소 1'


def test_rest_config_and_managed_job_guards(env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from fms_server.api.cargo import router as cargo_router
    from fms_server.api.locker import router as locker_router
    from fms_server.api.jobs import router as jobs_router
    bridge, coord, jobs, mqtt, fleet, state, _ = env
    app = FastAPI()
    app.include_router(locker_router)
    app.include_router(jobs_router)
    app.include_router(cargo_router)
    app.state.locker, app.state.jobs, app.state.cargo = bridge, jobs, bridge.cargo
    app.state.sites, app.state.job_coordinator = bridge.registry, coord
    with TestClient(app) as client:
        assert client.get('/api/v1/locker').json()['config']['site_id'] == 'hq'
        response = client.put('/api/v1/locker/config', json={
            'site_id': 'hq', 'pickup_target': 'ghost', 'households': {'101-101': 'home'}})
        assert response.status_code == 422
        d = request(bridge)
        base = f'/api/v1/sites/hq/jobs/{d["child_id"]}'
        assert client.post(base + '/dispatch', json={'robot_id': 'R1'}).status_code == 409
        assert client.delete(base).status_code == 409
        assert client.post('/api/v1/locker/deliveries/J1/return', json={}).status_code == 409
        finish(env)
        confirmed(bridge, items('p1', 'p2'))
        report(env, [ActionStatus.FINISHED, ActionStatus.RUNNING])
        a, b = d['parcels']
        view = client.get('/api/v1/sites/hq/cargo').json()
        assert {t['tray_id']: t['state'] for t in view['robots']['R1']['trays']} == {
            'TRAY_A': 'occupied', 'TRAY_B': 'unknown'}
        assert client.get('/api/v1/robots/R1/cargo').json()['bundle']['bundle_id'] == d['bundle_id']
        assert client.get('/api/v1/sites/other/parcels/' + b).status_code == 404
        r = client.post(f'/api/v1/sites/hq/parcels/{b}/resolve', json={'result': 'delivered', 'reason': ''})
        assert r.status_code == 422
        r = client.post(f'/api/v1/sites/hq/parcels/{b}/resolve', json={'result': 'not_loaded', 'reason': '보관소에 그대로 있음'})
        assert r.status_code == 200 and r.json()['status'] == 'canceled'
        assert r.json()['history'][-1]['reason'].startswith('운영자 확정')
        assert [p['status'] for p in client.get('/api/v1/sites/hq/parcels').json()] == ['onboard', 'canceled']
        assert client.get('/api/v1/sites/hq/delivery-bundles').json()[0]['bundle_id'] == d['bundle_id']


def test_multifloor_stage_preserves_pick_drop_duration_and_finished_gate(env):
    from types import SimpleNamespace
    from fms_server.route import build_plan
    from fms_server.task import _parse_command
    bridge, coord, jobs, mqtt, fleet, state, _ = env
    commands = [{'type': 'drop', 'target': 'home', 'duration': 3, 'params': {'payload_id': 'p1'}},
                {'type': 'wait', 'duration': 1}]
    parsed = [_parse_command(c) for c in commands]
    step = {'map_id': 'f1', 'pose': {'x': 3, 'y': 3, 'theta': 0}}
    plan = build_plan(parsed, [step, step], 'f1', 'hq', SimpleNamespace(list=lambda site: []), registry=bridge.registry)
    job = jobs.create('hq', commands, 'R1', resolved_steps=[step, step], external_owner='J1')
    job.plan, job.status = plan, JobStatus.ASSIGNED
    assert plan[0]['nodes'][0]['action']['duration'] == 3
    order = coord._stage_order(job, plan[0], f'{job.job_id}-s0')
    assert order.nodes[0].action.action_type.value == 'DROP'
    assert order.nodes[0].action.payload_id == 'p1'
    job.stage_data = {'sent_at': 0, 'ack': True}
    job.order_snapshot = order.model_dump(mode='json')
    record = fleet.get('R1')
    coord._run_stage(job, plan[0], record)
    assert job.status == JobStatus.FAILED  # FINISHED 없이 stage 완료로 넘어가지 않는다


def test_duplicate_confirm_does_not_replace_loaded_manifest(env):
    bridge, *_ = env
    d = request(bridge)
    finish(env)
    confirmed(bridge, items('p1', 'p2'))
    child_id, bundle_id = d['child_id'], d['bundle_id']
    confirmed(bridge, items('p3', 'p4'))
    assert d['items'][0]['receipt_no'] == 'p1'
    assert (d['child_id'], d['bundle_id']) == (child_id, bundle_id)

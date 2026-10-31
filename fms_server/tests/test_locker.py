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
    assert trays['TRAY_A']['state'] == trays['TRAY_B']['state'] == 'loaded'
    progress = bridge.snapshot()['deliveries'][0]['progress']
    assert progress['status'] == 'active' and progress['current'].endswith('이동')
    assert [s['status'] for s in progress['steps'][:5]] == ['done'] * 5   # 배정·이동·확정·적재 2

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
    assert trays == {'TRAY_A': 'empty_tray', 'TRAY_B': 'loaded'}
    assert results(mqtt) == [('p3', 'DELIVERED')]

    bridge.tick()
    assert payloads(mqtt.orders[-1]) == [('DROP', b)]
    finish(env)
    assert d['phase'] == 'returning'
    with pytest.raises(JobError):
        coord.dispatch(ordinary.job_id, 'R1')

    bridge.tick()
    order = mqtt.orders[-1]
    # 칸마다 반납 DROP. 이전 방식(pickup_target)은 픽업 지점에서 반납한다.
    assert payloads(order) == [('DROP', f"{TRAY_RETURN_PREFIX}{d['bundle_id']}:TRAY_A"),
                               ('DROP', f"{TRAY_RETURN_PREFIX}{d['bundle_id']}:TRAY_B")]
    assert (order.nodes[0].position.x, order.nodes[0].position.y) == (1, 1)   # 보관소 node
    report(env, [ActionStatus.FINISHED, ActionStatus.RUNNING], clear=False)
    trays = {t['tray_id']: t['state'] for t in cargo.robot_cargo('R1')['trays']}
    assert trays == {'TRAY_A': 'none', 'TRAY_B': 'returning'}
    assert d['phase'] == 'returning'
    finish(env)
    assert d['phase'] == 'done'
    view = cargo.bundle(d['bundle_id'])
    assert (view['status'], view['summary'], view['progress']) == ('closed', '배송 완료', '2/2')
    assert all(t['state'] == 'none' for t in cargo.robot_cargo('R1')['trays'])
    progress = bridge.snapshot()['deliveries'][0]['progress']
    assert progress['status'] == 'done' and progress['done'] == progress['total']
    marks = [e['mark'] for e in d['timeline']]
    assert marks[0] == 'queued:0' and marks[-1].startswith('done:') and 'returning:2' in marks
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


def test_visit_order_follows_tray_order_not_household_sort(env):
    bridge, coord, jobs, mqtt, fleet, state, _ = env
    d = request(bridge)
    finish(env)
    # 보관소가 101-102 를 먼저 들르도록 TRAY_A 에 실었다. 세대 문자열 순서(101-101 먼저)를 따르지 않는다.
    # v4 추가 필드(building·unit·building_name·floor)는 받기만 한다.
    v4 = [{'receipt_no': 'p1', 'dong': 101, 'ho': 101, 'building': 101, 'unit': 101, 'building_name': '101동',
           'floor': 1, 'tray': 'TRAY_B'},
          {'receipt_no': 'p2', 'dong': 101, 'ho': 102, 'building': 101, 'unit': 102, 'building_name': '101동',
           'floor': 1, 'tray': 'TRAY_A'}]
    bridge.receive({'type': 'BUNDLE_CONFIRMED', 'job_id': 'J1', 'robot_id': 'R1', 'items': v4,
                    'summary': {'kind': 'RESERVED', 'slot_start': '2026-11-01T15:00:00+09:00'}})
    bridge.tick()
    assert [s['household'] for s in d['stops']] == ['101-102', '101-101']
    finish(env)
    bridge.tick()
    tray = {i['tray_id']: i['parcel_id'] for i in bridge.cargo.bundle(d['bundle_id'])['items']}
    assert payloads(mqtt.orders[-1]) == [('DROP', tray['TRAY_A'])]
    assert mqtt.orders[-1].nodes[0].position.x == 4      # home2 (101-102)
    steps = bridge.snapshot()['deliveries'][0]['progress']['steps']
    assert [s['label'] for s in steps if s['key'].startswith('move:')] == ['101-102 이동', '101-101 이동']


def test_locker_cancel_before_robot_assignment_ends_quietly(env):
    bridge, coord, jobs, mqtt, fleet, state, _ = env
    bridge.receive({'type': 'DELIVERY_JOB', 'job_id': 'J1',
                    'items': [{'receipt_no': 'p1', 'dong': 109, 'ho': 101, 'tray': 'TRAY_A'},
                              {'receipt_no': 'p2', 'dong': 101, 'ho': 101, 'tray': 'TRAY_B'}]})
    bridge.tick()
    d = bridge.deliveries['J1']
    assert d['phase'] == 'queued' and d['error']               # 109-101 연결 없음 → 확인 필요
    sent = len(mqtt.events)
    bridge.receive({'type': 'JOB_CANCELLED', 'job_id': 'J1', 'reason': 'EMPTIED'})
    bridge.tick()
    assert d['phase'] == 'canceled' and d['robot_id'] is None
    assert d['error'] == '보관소 취소: EMPTIED'
    assert len(mqtt.events) == sent                             # JOB_REJECTED 를 보내지 않는다
    bridge.receive({'type': 'DELIVERY_JOB', 'job_id': 'J1', 'items': items('p1', 'p2')})
    assert len(mqtt.events) == sent
    progress = bridge.snapshot()['deliveries'][0]['progress']
    assert progress['status'] == 'canceled' and progress['current'] == '배송 취소'
    with pytest.raises(JobError):
        bridge.cancel_queued('J1')                              # 이미 끝난 배송


def test_locker_cancel_while_moving_to_pickup_cancels_move_and_docks(dock_env):
    bridge, coord, jobs, mqtt, fleet, state = dock_env[:6]
    d = request(bridge)
    assert d['phase'] == 'to_pickup' and d['child_id']
    move = jobs.get(d['child_id'])
    bridge.receive({'type': 'JOB_CANCELLED', 'job_id': 'J1', 'reason': 'EMPTIED'})
    bridge.tick()
    assert jobs.get(move.job_id).status.value in ('canceling', 'canceled')
    assert d['phase'] == 'docking' and d['locker_cancel'] == 'EMPTIED'
    progress = bridge.snapshot()['deliveries'][0]['progress']
    status = {s['key']: s['status'] for s in progress['steps']}
    assert status['bundle'] == status['pick:0'] == status['return'] == 'canceled'
    assert status['dock'] == 'active'
    # 로봇이 이동 오더 취소를 보고하면 도킹 오더가 나간다.
    fleet.apply_state(state.model_copy(update={'order_id': None}))
    for _ in range(3):
        bridge.tick()
    dock = jobs.get(d['child_id'])
    assert dock.job_id != move.job_id and dock.commands[0].kind == 'docking'
    assert dock.commands[0].target.startswith('charger-')
    # 픽업 지점 도착 뒤에 늦게 온 취소·적재 후 취소는 무시한다.
    bridge.receive({'type': 'JOB_CANCELLED', 'job_id': 'J1', 'reason': 'EMPTIED'})
    assert d['phase'] == 'docking'


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
                             Rect(5, 5, 1, 1), 6.5, 5.5, 0, 6.5, 7.5, 0)
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
    assert d['return_name'] == '보관소 A 반납'
    finish(env)
    bridge.tick()
    finish(env)                       # 101-101
    bridge.tick()
    finish(env)                       # 101-102
    assert d['phase'] == 'returning'
    bridge.tick()
    order = mqtt.orders[-1]
    assert (order.nodes[0].position.x, order.nodes[0].position.y) == (6.5, 7.5)   # 반납 node
    assert [k for k, _ in payloads(order)] == ['DROP', 'DROP']
    finish(env)
    assert d['phase'] == 'done'
    assert all(t['state'] == 'none' for t in bridge.cargo.robot_cargo('R1')['trays'])
    # 스테이션 이름을 바꿔도 불변 ID 로 계속 찾는다.
    locations.update('보관소 A', new_name='보관소 1')
    assert bridge._pickup_name(bridge.config.model_dump()) == '보관소 1'
    assert bridge._station_targets(bridge.config.model_dump())[1] == '보관소 A 반납'


def test_station_without_return_node_is_not_usable(env):
    bridge, *_ = env
    registry = bridge.registry
    maps, locations = registry.map_store('hq'), registry.location_store('hq')
    station = create_station(maps, locations, 'f1', maps.active_version('f1'), '보관소 B',
                             Rect(5, 5, 1, 1), 6.5, 5.5, 0, 6.5, 7.5, 0)
    locations.delete('보관소 B 반납', allow_pickup=True)
    with pytest.raises(JobError, match='반납'):
        bridge.configure(LockerConfig(site_id='hq', pickup_station_id=station.station_id,
                                      households={'101-101': 'home'}))


def test_unreturned_trays_refuse_new_bundle(env):
    bridge, coord, jobs, mqtt, fleet, state, _ = env
    d = request(bridge)
    finish(env)
    confirmed(bridge, items('p1', 'p2'))
    finish(env)
    bridge.tick()
    finish(env)
    bridge.tick()
    finish(env)
    bridge.tick()
    # 반납 DROP 이 FINISHED 없이 끝남 → 반납 중이던 칸은 unknown, 새 묶음 거절
    report(env, [ActionStatus.RUNNING, ActionStatus.WAITING])
    assert d['phase'] == 'blocked'
    trays = {t['tray_id']: t['state'] for t in bridge.cargo.robot_cargo('R1')['trays']}
    assert trays == {'TRAY_A': 'unknown', 'TRAY_B': 'empty_tray'}
    with pytest.raises(Exception, match='반납'):
        bridge.cargo.open_bundle(site_id='hq', delivery_job_id='J9', robot_id='R1', locker_id='LG-01',
                                 station_id=None, station_name='locker',
                                 items=[{'receipt_no': 'x1', 'dong': 101, 'ho': 101, 'tray': 'TRAY_A',
                                         'household': '101-101', 'target': 'home', 'step': {}},
                                        {'receipt_no': 'x2', 'dong': 101, 'ho': 102, 'tray': 'TRAY_B',
                                         'household': '101-102', 'target': 'home2', 'step': {}}])
    # 운영자 확인 후 복귀하면 두 칸을 다시 반납한다.
    bridge.recover_return('J1', [], True)
    bridge.tick()
    assert [k for k, _ in payloads(mqtt.orders[-1])] == ['DROP', 'DROP']
    finish(env)
    assert d['phase'] == 'done'
    assert all(t['state'] == 'none' for t in bridge.cargo.robot_cargo('R1')['trays'])


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
            'TRAY_A': 'loaded', 'TRAY_B': 'unknown'}
        delivery = view['deliveries'][0]
        assert delivery['job_id'] == 'J1' and 'config' not in delivery
        failed = [s for s in delivery['progress']['steps'] if s['status'] == 'failed']
        assert len(failed) == 1 and failed[0]['key'] == 'pick:TRAY_B'
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


def test_unmapped_household_needs_attention_and_cancel_rejects_to_locker(env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from fms_server.api.locker import router as locker_router
    bridge, coord, jobs, mqtt, fleet, state, _ = env
    # 101-101 은 연결, 105-101 은 연결 없음 → 배정 대기에 머문다
    bridge.receive({'type': 'DELIVERY_JOB', 'job_id': 'J1', 'items': [
        {'receipt_no': 'ok1', 'dong': 101, 'ho': 101}, {'receipt_no': 'bad1', 'dong': 105, 'ho': 101}]})
    bridge.tick()
    d = bridge.deliveries['J1']
    assert d['phase'] == 'queued' and d['robot_id'] is None and '105-101' in d['error']
    progress = bridge.snapshot()['deliveries'][0]['progress']
    assert progress['status'] == 'attention'
    assert progress['steps'][0]['status'] == 'failed' and '105-101' in progress['steps'][0]['detail']

    app = FastAPI()
    app.include_router(locker_router)
    app.state.locker = bridge
    with TestClient(app) as client:
        r = client.post('/api/v1/locker/deliveries/J1/cancel', json={'reason': ''})
        assert r.status_code == 200, r.text
        assert r.json()['unroutable'] == ['bad1']
        assert client.post('/api/v1/locker/deliveries/J1/cancel', json={}).status_code == 409
        assert client.post('/api/v1/locker/deliveries/NOPE/cancel', json={}).status_code == 404
    assert d['phase'] == 'canceled' and d['error'].startswith('운영자 취소: 세대 105-101')
    rejected = [p for t, p in mqtt.events if p.get('type') == 'JOB_REJECTED']
    assert rejected[-1] == {'type': 'JOB_REJECTED', 'job_id': 'J1', 'reason': d['error'], 'unroutable': ['bad1']}
    progress = bridge.snapshot()['deliveries'][0]['progress']
    assert progress['status'] == 'canceled' and progress['current'] == '배송 취소'
    # 취소 후 tick 은 로봇을 예약하지 않고, 보관소 재전송에는 같은 취소를 다시 알린다.
    bridge.tick()
    assert d['robot_id'] is None and 'R1' not in coord._reservations
    before = len(rejected)
    bridge.receive({'type': 'DELIVERY_JOB', 'job_id': 'J1', 'items': [{'receipt_no': 'ok1', 'dong': 101, 'ho': 101}]})
    assert len([p for t, p in mqtt.events if p.get('type') == 'JOB_REJECTED']) == before + 1


def test_cancel_after_robot_reserved_is_refused(env):
    bridge, *_ = env
    d = request(bridge)
    assert d['phase'] == 'to_pickup'
    with pytest.raises(JobError, match='배정 전'):
        bridge.cancel_queued('J1')


@pytest.fixture
def dock_env(tmp_path):
    """env 와 같지만 맵에 물리 충전기 2개와 충전소 목적지가 있다."""
    registry = SiteRegistry(tmp_path)
    registry.create_site('hq', '단지')
    stream = io.BytesIO()
    Image.new('L', (100, 100), 255).save(stream, format='PNG')
    chargers = {'chargers': [
        {'id': 'near', 'x': 5, 'y': 1, 'theta': 0, 'entry': 'FORWARD'},
        {'id': 'far', 'x': 8, 'y': 8, 'theta': 0, 'entry': 'BACKWARD'}]}
    files = {'fms_map.json': json.dumps(dict(name='hall', image='grid.png', resolution=.1,
             origin_x=0, origin_y=0, width_px=100, height_px=100)).encode(), 'grid.png': stream.getvalue(),
             'chargers.json': json.dumps(chargers).encode()}
    result = registry.map_store('hq').ingest_upload('f1', files, activate=True)
    registry.register_robot('hq', 'R1', 'f1')
    locations = registry.location_store('hq')
    locations.create('locker', 'f1', 1, 1, 0)
    locations.create('home', 'f1', 3, 3, 0)
    locations.create('home2', 'f1', 4, 4, 0)
    locations.create('charger-near', 'f1', 5, 1, 0, 'charger')
    locations.create('charger-far', 'f1', 8, 8, 0, 'charger')
    locations.create('fake-charger', 'f1', 2, 8, 0, 'charger')   # 물리 충전기 없음 → 후보 아님
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


def deliver_to_return(env):
    bridge, *_ = env
    d = request(bridge)
    finish(env)
    confirmed(bridge, items('p1', 'p2'))
    finish(env)               # 적재
    bridge.tick()
    finish(env)               # 101-101
    bridge.tick()
    finish(env)               # 101-102
    bridge.tick()
    return d


def test_tray_return_then_docks_at_nearest_charger_within_same_delivery(dock_env):
    bridge, coord, jobs, mqtt, fleet, state, _ = dock_env
    d = deliver_to_return(dock_env)
    assert d['phase'] == 'returning'
    finish(dock_env)          # 트레이 반납
    assert d['phase'] == 'docking' and d['charger'] == 'charger-near'
    # 보관소에는 반납 완료 시점에 결과·JOB_DONE 을 보낸다.
    assert mqtt.events[-1][1]['type'] == 'JOB_DONE'
    assert set(results(mqtt)) == {('p1', 'DELIVERED'), ('p2', 'DELIVERED')}   # 재전송은 중복 안전
    order = mqtt.orders[-1]
    action = order.nodes[0].action
    assert action.action_type.value == 'DOCK' and action.params['entry'] == 'FORWARD'
    assert action.params['min_charge'] == 30
    assert (order.nodes[0].position.x, order.nodes[0].position.y) == (5, 1)
    # 도킹 중에도 같은 배송이 로봇을 예약한다.
    ordinary = jobs.create('hq', [{'type': 'move', 'target': 'home'}])
    with pytest.raises(JobError):
        coord.dispatch(ordinary.job_id, 'R1')
    progress = bridge.snapshot()['deliveries'][0]['progress']
    assert progress['current'] == '충전 스테이션 도킹' and progress['steps'][-1]['detail'].startswith('charger-near')
    finish(dock_env)          # 도킹 완료
    assert d['phase'] == 'done' and 'R1' not in coord._reservations
    progress = bridge.snapshot()['deliveries'][0]['progress']
    assert progress['status'] == 'done' and progress['done'] == progress['total'] == 12
    assert len([1 for t, p in mqtt.events if p.get('type') == 'JOB_DONE']) == 1


def test_docking_skips_busy_charger_and_failure_does_not_block(dock_env):
    bridge, coord, jobs, mqtt, fleet, state, _ = dock_env
    # 다른 작업이 가까운 충전소로 향하고 있다 → 먼 충전소(BACKWARD 진입)
    jobs.create('hq', [{'type': 'docking', 'target': 'charger-near',
                        'params': {'entry': 'FORWARD', 'undock_distance': .6, 'speed': .2, 'min_charge': 50}}], 'R2')
    d = deliver_to_return(dock_env)
    finish(dock_env)
    assert d['charger'] == 'charger-far'
    assert mqtt.orders[-1].nodes[0].action.params['entry'] == 'BACKWARD'
    # 로봇이 도킹 실패(FATAL DOCK_FAILED) → 물품 결과는 이미 확정이므로 배송은 끝내고 예약을 푼다.
    from common.schemas import ErrorInfo, ErrorLevel
    job = report(dock_env, [ActionStatus.RUNNING], clear=False)
    fleet.apply_state(state.model_copy(update={
        'state': RobotState.ERROR, 'order_id': job.order_id,
        'errors': [ErrorInfo(error_type='DOCK_FAILED', level=ErrorLevel.FATAL, description='충전기 정렬 실패')]}))
    bridge.tick()
    assert d['phase'] == 'done' and d['dock_note'].startswith('충전 도킹 실패')
    assert 'R1' not in coord._reservations
    dock = bridge.snapshot()['deliveries'][0]['progress']['steps'][-1]
    assert dock['status'] == 'canceled' and '도킹 실패' in dock['detail']


def test_docking_off_or_unknown_charger(dock_env):
    bridge, coord, *_ = dock_env
    with pytest.raises(JobError, match='충전소'):
        bridge.configure(LockerConfig(site_id='hq', pickup_target='locker', charger_target='fake-charger',
                                      households={'101-101': 'home', '101-102': 'home2'}))
    bridge.configure(LockerConfig(site_id='hq', pickup_target='locker', charger_target='off',
                                  households={'101-101': 'home', '101-102': 'home2'}))
    d = deliver_to_return(dock_env)
    finish(dock_env)
    assert d['phase'] == 'done' and d['dock_note'] == '도킹 안 함 (설정)' and 'R1' not in coord._reservations


def test_charger_with_blocked_approach_is_not_chosen(dock_env):
    bridge, coord, jobs, mqtt, fleet, state, path = dock_env
    # 가까운 충전기 진입 지점(FORWARD → 뒤쪽 0.6 m)을 벽으로 막는다 → 먼 충전소를 고른다
    import types
    real = bridge.registry.map_store('hq').load_map_model('f1')
    blocked = types.SimpleNamespace(in_bounds=real.in_bounds,
                                    is_free=lambda x, y: not (abs(x - 4.4) < .2 and abs(y - 1) < .2) and real.is_free(x, y))
    store = bridge.registry.map_store('hq')
    store.load_map_model = lambda map_id, version=None: blocked
    original = bridge.registry.map_store
    bridge.registry.map_store = lambda site: store if site == 'hq' else original(site)
    d = deliver_to_return(dock_env)
    finish(dock_env)
    assert d['charger'] == 'charger-far'
    # 모두 막히면 도킹하지 않고 사유를 남긴다
    store.load_map_model = lambda map_id, version=None: types.SimpleNamespace(in_bounds=real.in_bounds,
                                                                               is_free=lambda x, y: False)
    charger, note = bridge._choose_charger(d)
    assert charger is None and '진입 방향' in note and 'charger-near' in note


def test_clear_floor_hides_drop_marks_but_keeps_records(dock_env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from fms_server.api.cargo import router as cargo_router
    bridge, coord = dock_env[:2]
    d = deliver_to_return(dock_env)
    cargo = bridge.cargo
    delivered = [p for p in cargo.parcels('hq') if p['status'] == 'delivered']
    assert len(delivered) == 2 and all(p['drop_position'] for p in delivered)
    app = FastAPI()
    app.include_router(cargo_router)
    app.state.cargo, app.state.sites, app.state.job_coordinator = cargo, bridge.registry, coord
    with TestClient(app) as client:
        assert client.post('/api/v1/sites/ghost/parcels/clear-floor', json={}).status_code == 404
        # 다른 맵(층)만 지우면 이 맵 표시는 그대로
        assert client.post('/api/v1/sites/hq/parcels/clear-floor', json={'map_id': 'f3'}).json()['count'] == 0
        res = client.post('/api/v1/sites/hq/parcels/clear-floor', json={}).json()
        assert res['count'] == 2
        assert client.post('/api/v1/sites/hq/parcels/clear-floor', json={}).json()['count'] == 0
    for p in cargo.parcels('hq'):
        assert p['status'] == 'delivered' and p['drop_position'] and p['floor_cleared_at']
        assert p['history'][-1]['reason'] == '바닥 하차 표시 정리'
    assert d['bundle_id']

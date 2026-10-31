"""교통 제어의 중복 허가·대기 node 경합·재시작·취소 회귀 검증."""
import io
import json
import time

import pytest
from PIL import Image
from fastapi.testclient import TestClient

from common.schemas import BusySubState, LocalizationState, Order, Pose, RobotState
from fms_server.fleet import Fleet
from fms_server.main import app
from fms_server.sites import SiteRegistry
from fms_server.task import JobCoordinator, JobError, JobStore
from fms_server.tests.factories import make_state
from fms_server.tests.test_task import FakeMqtt

BASE = '/api/v1/sites/hq/maps/f1'


def pose(x,y):
    return dict(x=x,y=y,theta=0)


def zone_body(version):
    return dict(base_version=version,name='교차로',type='occupancy',x=7,y=0,width=2,height=10,
                gates=[dict(gate_id='west',wait=pose(5,3),exit=pose(11,3)),
                       dict(gate_id='east',wait=pose(11,7),exit=pose(5,7))])


@pytest.fixture
def env(tmp_path, monkeypatch):
    with TestClient(app) as client:
        app.state.allocator.stop()
        app.state.job_coordinator.stop()
        registry = SiteRegistry(tmp_path)
        registry.create_site('hq','시험')
        img = io.BytesIO()
        Image.new('L',(80,50),255).save(img,format='PNG')
        result = registry.map_store('hq').ingest_upload('f1',{
            'fms_map.json':json.dumps(dict(name='hall',image='grid.png',resolution=.2,
                                          origin_x=0,origin_y=0,width_px=80,height_px=50)).encode(),
            'grid.png':img.getvalue()},activate=True)
        fleet, jobs, mqtt = Fleet(), JobStore(tmp_path), FakeMqtt()
        coord = JobCoordinator(fleet,registry,jobs,mqtt)
        coord.start()
        app.state.sites, app.state.fleet = registry, fleet
        app.state.jobs, app.state.job_coordinator = jobs, coord
        app.state.coordinator._registry = registry
        app.state.broadcaster._registry = registry
        monkeypatch.setattr(app.state.coordinator,'notify_map_activated',lambda *args: [])
        def report(rid,x,y,order=None,node=None,**changes):
            state = make_state(rid,x=x,y=y,map_id=registry.map_store('hq').map_uid('f1'),
                               map_version=registry.map_store('hq').active_version('f1'),
                               state=RobotState.BUSY if order else RobotState.IDLE,
                               order_id=order.order_id if order else None)
            update = dict(traffic_control=True,timestamp=time.time(),**changes)
            if order:
                update.update(order_update_id=order.order_update_id,sub_state=BusySubState.WAITING)
            if node is not None:
                update.update(last_node_id=order.nodes[node].node_id,last_node_sequence_id=node)
            fleet.apply_state(state.model_copy(update=update))
        for rid,x,y in [('R1',3,3),('R2',13,7),('R3',3,1)]:
            registry.register_robot('hq',rid,'f1')
            report(rid,x,y)
        registry.location_store('hq').create('east','f1',13,3,0)
        registry.location_store('hq').create('west','f1',3,7,0)
        yield dict(client=client,registry=registry,fleet=fleet,jobs=jobs,mqtt=mqtt,
                   coord=coord,report=report,version=result.version,tmp=tmp_path)
        coord.stop()


def add_zone(e):
    r = e['client'].post(BASE+'/zones',json=zone_body(e['version']))
    assert r.status_code == 200, r.text
    for rid,x,y in [('R1',3,3),('R2',13,7),('R3',3,1)]:
        e['report'](rid,x,y)
    return r.json()


def dispatch(e,rid,target):
    job = e['jobs'].create('hq',[dict(type='move',target=target)],rid)
    e['coord'].dispatch(job.job_id)
    return job, Order.model_validate(job.order_snapshot)


def latest(job):
    return Order.model_validate(job.order_snapshot)


def leases(e,kind):
    return [v for v in e['coord'].traffic.data['leases'].values() if v['kind']==kind]


def test_zone_is_not_baked_and_gate_snapshots_survive_rebake(env):
    z = add_zone(env)
    store = env['registry'].map_store('hq')
    assert store.load_map_model('f1').is_free(8,5)
    ann = store.load_annotations('f1')
    assert ann.zones[-1].gates[0].wait.x == 5
    r = env['client'].post(BASE+'/zones',json=dict(base_version=z['version'],name='표시',type='elevator',x=1,y=8,width=1,height=1))
    assert r.status_code == 200, r.text
    assert store.load_annotations('f1').zones[0].gates == ann.zones[0].gates
    assert store.verify_integrity('f1') == []


def test_floor_limit_queues_and_releases_after_confirmed_completion(env):
    assert env['client'].put(BASE+'/traffic',json={'max_moving_robots':1}).status_code == 200
    a, oa = dispatch(env,'R1','east')
    b, ob = dispatch(env,'R2','west')
    assert all(n.released for n in oa.nodes)
    assert not any(n.released for n in ob.nodes)
    env['report']('R2',13,7,ob)  # 대기 오더도 정원에 추가 집계하지 않는다.
    env['report']('R1',3,3,oa)
    assert len(leases(env,'floor')) == 1
    env['report']('R1',13,3)
    assert a.status.value == 'success'
    assert all(n.released for n in latest(b).nodes)
    assert leases(env,'floor')[0]['robot'] == 'R2'


def test_opposite_directions_only_one_enters_and_exit_releases(env):
    add_zone(env)
    a, oa = dispatch(env,'R1','east')
    b, ob = dispatch(env,'R2','west')
    assert [n.released for n in oa.nodes] == [True,False,False]
    env['report']('R1',5,3,oa,node=0)
    assert latest(a).nodes[1].released
    assert latest(a).nodes[1].allowed_zone_ids
    env['report']('R2',11,7,ob,node=0)
    assert not latest(b).nodes[1].released
    assert len(leases(env,'zone')) == 1
    env['report']('R1',11,3,latest(a),node=1)
    assert latest(a).nodes[-1].released
    assert latest(b).nodes[1].released
    assert len(leases(env,'zone')) == 1
    assert leases(env,'zone')[0]['robot']=='R2'
    # 추가한 대기·출구 node를 원래 작업 완료로 세지 않는다.
    assert a.done_commands == 0


def test_same_wait_node_is_reserved_before_approach(env):
    add_zone(env)
    a, oa = dispatch(env,'R1','east')
    b, ob = dispatch(env,'R3','east')
    assert oa.nodes[0].released
    assert not any(n.released for n in ob.nodes)
    assert len(leases(env,'wait')) == 1


def test_cancel_inside_and_restart_keep_zone_blocked(env):
    add_zone(env)
    a, oa = dispatch(env,'R1','east')
    env['report']('R1',5,3,oa,node=0)
    env['report']('R1',8,3,latest(a),node=0)
    env['coord'].cancel(a.job_id)
    env['report']('R1',8,3)
    assert a.status.value == 'canceled'
    assert len(leases(env,'zone')) == 1
    env['coord'].stop()
    restored = JobCoordinator(env['fleet'],env['registry'],JobStore(env['tmp']),env['mqtt'])
    env['coord'] = restored
    restored.start()
    restored.tick_traffic()
    assert len(leases(env,'zone')) == 1
    env['report']('R1',11,3)
    assert not leases(env,'zone')


def test_lost_localization_and_disconnect_never_release(env):
    add_zone(env)
    a, oa = dispatch(env,'R1','east')
    env['report']('R1',5,3,oa,node=0)
    env['report']('R1',11,3,latest(a),node=1,localization=LocalizationState.UNLOCALIZED)
    assert len(leases(env,'zone')) == 1
    env['fleet'].forget('R1')
    env['coord'].tick_traffic()
    assert len(leases(env,'zone')) == 1


def test_validation_old_client_and_busy_zone_edit(env):
    body = zone_body(env['version'])
    body['gates'] = []
    assert env['client'].post(BASE+'/zones',json=body).status_code == 422
    assert env['client'].put(BASE+'/traffic',json={'max_moving_robots':0}).status_code == 422
    assert env['client'].put(BASE+'/traffic',json={'max_moving_robots':True}).status_code == 422
    z = add_zone(env)
    old = env['fleet'].get('R1').state.model_copy(update={'traffic_control':False})
    env['fleet'].apply_state(old)
    with pytest.raises(JobError,match='지원하지'):
        dispatch(env,'R1','east')
    env['report']('R1',3,3)
    dispatch(env,'R1','east')
    r = env['client'].delete(BASE+'/zones/'+z['zone_id'],params={'base_version':z['version']})
    assert r.status_code == 409


def test_chassis_clearance_and_unknown_peer_block_entry(env):
    add_zone(env)
    a, oa = dispatch(env,'R1','east')
    b, ob = dispatch(env,'R2','west')
    env['report']('R1',5,3,oa,node=0)
    env['report']('R2',11,7,ob,node=0)
    # 중심만 나왔고 차체는 걸쳐 있다. 잘못된 exit 노드 보고만으로 해제하지 않는다.
    env['report']('R1',9.2,3,latest(a),node=1)
    assert not latest(b).nodes[1].released
    assert leases(env,'zone')[0]['robot']=='R1'
    env['fleet'].forget('R3')
    env['report']('R1',11,3,latest(a),node=1)
    assert not latest(b).nodes[1].released
    state = env['client'].get(BASE+'/traffic').json()
    assert state['zones'][0]['status']=='blocked'
    env['report']('R3',3,1)
    assert latest(b).nodes[1].released


def test_other_map_edits_cannot_bypass_live_traffic_guard(env):
    z = add_zone(env)
    dispatch(env,'R1','east')
    r = env['client'].post(BASE+'/activate',json={'version':env['version']})
    assert r.status_code == 409
    r = env['client'].post(BASE+'/zones',json=dict(base_version=z['version'],name='표시',
                                                 type='elevator',x=1,y=8,width=1,height=1))
    assert r.status_code == 409
    assert env['registry'].map_store('hq').active_version('f1') == z['version']


def test_lower_limit_preserves_existing_permits(env):
    env['client'].put(BASE+'/traffic',json={'max_moving_robots':2})
    a, _ = dispatch(env,'R1','east')
    b, _ = dispatch(env,'R2','west')
    assert env['client'].put(BASE+'/traffic',json={'max_moving_robots':1}).status_code == 200
    c, oc = dispatch(env,'R3','east')
    assert all(n.released for n in latest(a).nodes+latest(b).nodes)
    assert not any(n.released for n in oc.nodes)
    assert len(leases(env,'floor'))==2


def test_rotated_zones_use_actual_boundary_for_overlap_mask_and_legacy_leases(env):
    from fms_server.traffic import ZonePlanner, bounds, intersects
    client = env['client']
    def rotated(version,cx,cy):
        return dict(base_version=version,name='회전 교차로',type='occupancy',
                    x=cx-.5,y=cy-3,width=1,height=6,rotation=45,
                    gates=[dict(gate_id='diagonal',wait=pose(cx-3,cy-3),exit=pose(cx+3,cy+3))])
    first = client.post(BASE+'/zones',json=rotated(env['version'],8,5))
    assert first.status_code == 200,first.text
    store = env['registry'].map_store('hq')
    z = store.load_annotations('f1').zone_by_id(first.json()['zone_id'])
    frame = bounds(z)
    assert intersects(frame,Pose(**pose(8,5)),0)
    assert not intersects(frame,Pose(**pose(6,3)),0)  # 외접 사각형의 빈 모서리
    assert intersects((7,0,9,10),Pose(**pose(8,5)),.5)  # 이전 예약 포맷
    assert not intersects((7,0,9,10),Pose(**pose(10,5)),.5)
    planner = ZonePlanner(store.load_map_model('f1'),[z],env['fleet'].get('R1').state)
    x,y = planner.model.world_to_pixel(6,3)
    assert planner.mask(None)[y,x]
    x,y = planner.model.world_to_pixel(8,5)
    assert not planner.mask(None)[y,x]
    second = client.post(BASE+'/zones',json=rotated(first.json()['version'],9.4,6.4))
    assert second.status_code == 200,second.text  # 외접 사각형은 겹쳐도 실제 존은 분리됨
    overlap = client.post(BASE+'/zones',json=rotated(second.json()['version'],8.1,5.1))
    assert overlap.status_code == 422

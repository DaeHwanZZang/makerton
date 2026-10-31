"""프리셋 게시 → FMS 배정 → 복합 오더 진행 및 예약 충돌 검증."""
import io
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from common.schemas import ActionProgress, ActionStatus, ConnectionState, RobotState
from fms_server.main import app
from fms_server.presets import PresetStore
from fms_server.sites import SiteRegistry
from fms_server.task import JobCoordinator, JobError, JobStatus, JobStore
from fms_server.tests.factories import make_state

BASE='/api/v1/sites/hq'


class Mqtt:
    connected=True
    def __init__(self): self.orders=[]; self.instants=[]
    def publish_order(self, order): self.orders.append(order)
    def publish_instant(self, action): self.instants.append(action)


@pytest.fixture
def env(tmp_path):
    with TestClient(app) as client:
        app.state.allocator.stop()
        registry=SiteRegistry(tmp_path)
        registry.create_site('hq','본사')
        stream=io.BytesIO(); Image.new('L',(100,100),255).save(stream,format='PNG')
        files={'fms_map.json':json.dumps(dict(name='hall',image='grid.png',resolution=.1,
                origin_x=0,origin_y=0,width_px=100,height_px=100)).encode(),
               'grid.png':stream.getvalue(), 'chargers.json':json.dumps({'chargers':[
                   {'id':'CH-1','x':5,'y':5,'theta':0,'entry':'BACKWARD'}]}).encode()}
        result=registry.map_store('hq').ingest_upload('f1',files,activate=True)
        locations=registry.location_store('hq')
        locations.create('a1','f1',1,1,0)
        locations.create('a2','f1',2,2,0)
        locations.create('a3','f1',3,3,0)
        locations.create('charger','f1',5,5,0)
        mqtt=Mqtt(); jobs=JobStore(tmp_path)
        coord=app.state.job_coordinator
        coord._registry=registry;coord._store=jobs;coord._mqtt=mqtt
        app.state.sites=registry;app.state.jobs=jobs;app.state.presets=PresetStore(tmp_path)
        app.state.coordinator._registry=registry;app.state.broadcaster._registry=registry
        def online(robot='AMR-001', **kwargs):
            if not registry.assignment_for(robot):registry.register_robot('hq',robot,'f1')
            state=make_state(robot,map_id=result.map_uid,map_version=result.version,**kwargs)
            app.state.fleet.apply_connection(robot,ConnectionState.ONLINE)
            app.state.fleet.apply_state(state)
            return state
        yield client,registry,jobs,coord,mqtt,online


def preset(client, commands=None):
    body={'name':'순회 후 도킹','commands':commands or [
        {'type':'move','target':'a1'},{'type':'move','target':'a2'},
        {'type':'wait','duration':10},{'type':'move','target':'a3'},
        {'type':'docking','target':'charger','params':{
            'entry':'BACKWARD','undock_distance':.6,'speed':.2,'min_charge':80}}]}
    r=client.post(BASE+'/presets',json=body)
    assert r.status_code==200,r.text
    return r.json()


def test_board_dispatch_compiles_bundle_and_tracks_wait(env):
    c,registry,jobs,coord,mqtt,online=env
    p=preset(c);state=online()
    r=c.post(BASE+f'/presets/{p["preset_id"]}/publish',json={})
    assert r.status_code==200,r.text
    job=jobs.get(r.json()['job_id'])
    assert job.status==JobStatus.PENDING and job.auto_assign
    coord.allocate_pending()
    assert job.status==JobStatus.ASSIGNED
    assert len(mqtt.orders)==1
    order=mqtt.orders[0]
    assert len(order.nodes)==5
    assert order.nodes[2].position==order.nodes[1].position
    assert order.nodes[2].action.action_type.value=='WAIT'
    assert order.nodes[-1].action.params['min_charge']==80
    # 마지막 도달 노드가 대기 노드여도 액션 종료 전에는 완료 단계로 세지 않는다.
    state=state.model_copy(update={'state':RobotState.BUSY,'sub_state':'ACTING',
        'order_id':job.order_id,'last_node_id':order.nodes[2].node_id,'last_node_sequence_id':2,
        'action_states':[ActionProgress(action_id=order.nodes[2].action.action_id,status=ActionStatus.RUNNING)]})
    app.state.fleet.apply_state(state)
    assert job.status==JobStatus.WORKING and job.done_commands==2
    state.action_states[0].status=ActionStatus.FINISHED
    app.state.fleet.apply_state(state)
    assert job.done_commands==3
    state=state.model_copy(update={'state':RobotState.CHARGING,'sub_state':None,'order_id':None})
    app.state.fleet.apply_state(state)
    assert job.status==JobStatus.SUCCESS and job.done_commands==5
    assert registry.map_store('hq').file_path('f1','chargers.json').is_file()


def test_preset_and_locations_changes_do_not_change_published_job(env,tmp_path):
    c,registry,jobs,coord,mqtt,online=env
    p=preset(c,[{'type':'move','target':'a1'}])
    job_id=c.post(BASE+f'/presets/{p["preset_id"]}/publish',json={}).json()['job_id']
    registry.location_store('hq').update('a1',x=8)
    r=c.put(BASE+f'/presets/{p["preset_id"]}',json={'name':'수정된 프리셋','commands':[{'type':'move','target':'a2'}]})
    assert r.status_code==200
    assert c.delete(BASE+f'/presets/{p["preset_id"]}').status_code==200
    # 디스크 재시작 후에도 스냅샷과 게시판 상태를 유지한다.
    reload=JobStore(tmp_path)
    assert reload.get(job_id).resolved_steps[0]['pose']['x']==1
    assert reload.get(job_id).preset_name=='순회 후 도킹'
    assert reload.get(job_id).auto_assign
    online();coord.allocate_pending()
    assert mqtt.orders[0].nodes[0].position.x==1
    assert jobs.get(job_id).status==JobStatus.ASSIGNED


def test_fifo_reservation_prevents_idle_robot_double_assignment(env):
    c,registry,jobs,coord,mqtt,online=env
    p=preset(c,[{'type':'move','target':'a1'}]);online('AMR-002');online('AMR-001')
    ids=[c.post(BASE+f'/presets/{p["preset_id"]}/publish',json={}).json()['job_id'] for _ in range(3)]
    with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(lambda _:coord.allocate_pending(),range(2)))
    assert [jobs.get(i).robot_id for i in ids]==['AMR-001','AMR-002',None]
    assert [jobs.get(i).status for i in ids]==[JobStatus.ASSIGNED,JobStatus.ASSIGNED,JobStatus.PENDING]
    assert len(mqtt.orders)==2
    # 아직 로봇이 IDLE을 보고해도 새 작업을 주지 않는다.
    coord.allocate_pending();assert len(mqtt.orders)==2


def test_manual_and_automatic_dispatch_are_serialized(env):
    c,_,jobs,coord,mqtt,online=env
    p=preset(c,[{'type':'move','target':'a1'}]);online()
    ids=[c.post(BASE+f'/presets/{p["preset_id"]}/publish',json={}).json()['job_id'] for _ in range(2)]
    def dispatch(i):
        try:coord.dispatch(i,'AMR-001');return 'ok'
        except JobError:return 'reserved'
    with ThreadPoolExecutor(max_workers=2) as pool:assert sorted(pool.map(dispatch,ids))==['ok','reserved']
    assert len(mqtt.orders)==1


def test_invalid_map_and_disconnected_mqtt_stay_pending(env):
    c,_,jobs,coord,mqtt,online=env
    p=preset(c,[{'type':'move','target':'a1'}]);state=online()
    job_id=c.post(BASE+f'/presets/{p["preset_id"]}/publish',json={}).json()['job_id']
    mqtt.connected=False;coord.allocate_pending();assert mqtt.orders==[]
    mqtt.connected=True;state.map_id='other-map';app.state.fleet.apply_state(state)
    coord.allocate_pending();assert mqtt.orders==[]
    assert jobs.get(job_id).status==JobStatus.PENDING


def test_direct_assignment_and_cancel_hold_reservation_until_ack(env):
    c,_,jobs,coord,mqtt,online=env
    p=preset(c,[{'type':'move','target':'a1'}]);state=online()
    r=c.post(BASE+f'/presets/{p["preset_id"]}/publish',json={'robot_id':'AMR-001'})
    assert r.status_code==200,r.text
    job=jobs.get(r.json()['job_id']);assert job.status==JobStatus.ASSIGNED
    coord.cancel(job.job_id);assert job.status==JobStatus.CANCELING
    app.state.fleet.apply_state(state);assert job.status==JobStatus.CANCELING
    state.timestamp=time.time()+.01;app.state.fleet.apply_state(state)
    assert job.status==JobStatus.CANCELED


@pytest.mark.parametrize('commands',[
    [{'type':'wait','duration':1}], [{'type':'move','target':'missing'}],
    [{'type':'wait','duration':0}], [{'type':'docking','target':'a1'}],
])
def test_invalid_preset_steps_rejected(env,commands):
    c,*_=env
    assert c.post(BASE+'/presets',json={'name':'오류','commands':commands}).status_code==422


def test_docking_on_non_charger_rejected_before_posting(env):
    c,_,jobs,_,_,_=env
    p=preset(c,[{'type':'move','target':'a1'},{'type':'docking','target':'a1','params':{
        'entry':'FORWARD','undock_distance':.6,'min_charge':80}}])
    r=c.post(BASE+f'/presets/{p["preset_id"]}/publish',json={})
    assert r.status_code==422
    assert jobs.list()==[]


def test_allocator_loop_picks_up_posted_job_and_stops(env):
    from fms_server.allocator import JobAllocator
    c,_,jobs,coord,mqtt,online=env
    online();p=preset(c,[{'type':'move','target':'a1'}])
    job_id=c.post(BASE+f'/presets/{p["preset_id"]}/publish',json={}).json()['job_id']
    worker=JobAllocator(coord)
    try:
        worker.start()
        deadline=time.monotonic()+3
        while jobs.get(job_id).status==JobStatus.PENDING and time.monotonic()<deadline:
            time.sleep(.05)
        assert jobs.get(job_id).status==JobStatus.ASSIGNED
        assert len(mqtt.orders)==1
    finally:
        worker.stop()
    assert not worker._thread.is_alive()


def test_charger_bundle_hash_and_zone_rebake_preserve_chargers(env):
    import zipfile
    from fms_server.api.zones import RectangleZoneRequest
    _,registry,_,_,_,_=env
    store=registry.map_store('hq');base=store.active_version('f1')
    bundle=zipfile.ZipFile(io.BytesIO(store.bundle_zip('f1')))
    assert 'chargers.json' in bundle.namelist()
    assert store.verify_integrity('f1')==[]
    source=store._version_dir('f1',base)/'source'
    files={p.name:p.read_bytes() for p in source.iterdir() if p.is_file()}
    files['chargers.json']=files['chargers.json'].replace(b'CH-1',b'CH-2')
    changed=store.ingest_upload('f1',files,activate=True)
    assert changed.version!=base
    zone=RectangleZoneRequest(base_version=changed.version,name='금지',type='prohibit',
                              x=7,y=7,width=1,height=1).zone('test-zone')
    edited=store.edit_zone('f1',changed.version,'test-zone',zone,create=True)
    assert edited.version!=changed.version
    assert json.loads(store.file_path('f1','chargers.json').read_text())['chargers'][0]['id']=='CH-2'
    assert store.verify_integrity('f1')==[]


def test_location_rename_updates_preset_and_legacy_pending_not_snapshot(env):
    c,registry,jobs,_,_,_=env
    p=preset(c,[{'type':'move','target':'a1'}])
    posted=c.post(BASE+f'/presets/{p["preset_id"]}/publish',json={}).json()['job_id']
    legacy=jobs.create('hq',[{'type':'move','target':'a1'}])
    r=c.put(BASE+'/locations/a1',json={'name':'renamed','x':7})
    assert r.status_code==200,r.text
    assert registry.location_store('hq').get('renamed').x==7
    assert app.state.presets.get('hq',p['preset_id'])['commands'][0]['target']=='renamed'
    assert legacy.commands[0].target=='renamed'
    snapshot=jobs.get(posted)
    assert snapshot.commands[0].target=='a1'
    assert snapshot.resolved_steps[0]['pose']['x']==1
    next_job=c.post(BASE+f'/presets/{p["preset_id"]}/publish',json={})
    assert next_job.status_code==200,next_job.text
    assert jobs.get(next_job.json()['job_id']).resolved_steps[0]['pose']['x']==7


def test_rename_rolls_back_when_preset_save_fails(env,monkeypatch):
    c,registry,jobs,_,_,_=env
    p=preset(c,[{'type':'move','target':'a1'}])
    legacy=jobs.create('hq',[{'type':'move','target':'a1'}])
    def failed(*args):raise OSError('테스트 저장 실패')
    monkeypatch.setattr(app.state.presets,'_write',failed)
    with pytest.raises(OSError):
        c.put(BASE+'/locations/a1',json={'name':'renamed'})
    assert registry.location_store('hq').get('a1').x==1
    assert [l.name for l in registry.location_store('hq').list()]==['a1','a2','a3','charger']
    assert app.state.presets.get('hq',p['preset_id'])['commands'][0]['target']=='a1'
    assert jobs.get(legacy.job_id).commands[0].target=='a1'

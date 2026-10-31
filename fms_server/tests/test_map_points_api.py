"""실시간 맵의 목적지/도킹 스테이션 등록 → 영속화·번들·도킹 작업 연결 검증."""
import io
import json
import math
import zipfile
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from common.schemas import Pose
from fms_server.locations import LocationStore
from fms_server.main import app
from fms_server.mapkit.chargers import require_charger
from fms_server.sites import SiteRegistry

BASE='/api/v1/sites/hq/maps/f1'


@pytest.fixture
def env(tmp_path, monkeypatch):
    with TestClient(app) as client:
        app.state.allocator.stop()
        registry=SiteRegistry(tmp_path);registry.create_site('hq','현장')
        app.state.sites=registry
        app.state.coordinator._registry=registry;app.state.broadcaster._registry=registry
        image=io.BytesIO();Image.new('L',(60,40),255).save(image,format='PNG')
        files={'fms_map.json':json.dumps(dict(name='test',image='grid.png',resolution=.1,
                origin_x=-1,origin_y=-2,width_px=60,height_px=40)).encode(),
               'grid.png':image.getvalue(),
               'zone_meta.json':json.dumps({'zones':[{'id':'z1','name':'금지','type':'prohibit',
                                  'polygon':[[0,0],[1,0],[1,1],[0,1]]}]}).encode()}
        store=registry.map_store('hq');result=store.ingest_upload('f1',files,activate=True)
        notifications=[]
        monkeypatch.setattr(app.state.coordinator,'notify_map_activated',lambda site,map_id:notifications.append((site,map_id)) or ['AMR-001'])
        yield client,registry,store,result.version,notifications


def payload(version,**values):
    return dict(base_version=version,name='지점-A',x=2,y=1,theta=0)|values


def test_map_location_registration_keeps_version(env):
    client,registry,store,version,notifications=env
    r=client.post(BASE+'/locations',json=payload(version,type='parking',theta=math.pi/2))
    assert r.status_code==200,r.text
    loc=registry.location_store('hq').get('지점-A')
    assert loc.map_id=='f1' and loc.type=='parking'
    assert loc.theta==math.pi/2 and loc.x==2
    assert store.active_version('f1')==version
    assert notifications==[]
    assert len(store.get_map('f1').versions)==1


def test_docking_station_creates_destination_and_physical_charger(env):
    client,registry,store,version,notifications=env
    before=store.load_map_model('f1').classify_grid().copy()
    r=client.post(BASE+'/docking-stations',json=payload(version,name='충전-A',entry='BACKWARD',theta=-math.pi/2))
    assert r.status_code==200,r.text
    active=r.json()['version']
    assert active!=version and store.active_version('f1')==active
    assert r.json()['notified_robots']==['AMR-001']
    loc=registry.location_store('hq').get('충전-A')
    assert loc.type=='charger'
    require_charger(store,'f1',Pose(x=2,y=1,theta=-math.pi/2),'BACKWARD')
    assert np.array_equal(before,store.load_map_model('f1').classify_grid())
    assert store.load_annotations('f1').zones[0].zone_id=='z1'
    bundle=zipfile.ZipFile(io.BytesIO(store.bundle_zip('f1')))
    chargers=json.loads(bundle.read('chargers.json'))['chargers']
    assert chargers[0]['id']=='충전-A' and chargers[0]['entry']=='BACKWARD'
    assert 'chargers.json' not in zipfile.ZipFile(io.BytesIO(store.bundle_zip('f1',version))).namelist()
    assert store.verify_integrity('f1')==[]
    assert notifications==[('hq','f1')]


@pytest.mark.parametrize('change',[
    {'x':10},{'y':-3},{'theta':4},{'name':''},{'entry':'SIDEWAYS'},
])
def test_invalid_station_leaves_no_destination_or_active_version_change(env,change):
    client,registry,store,version,notifications=env
    r=client.post(BASE+'/docking-stations',json=payload(version,**change))
    assert r.status_code==422,r.text
    assert registry.location_store('hq').list()==[]
    assert store.active_version('f1')==version
    assert len(store.get_map('f1').versions)==1
    assert notifications==[]


def test_stale_map_and_duplicate_name_do_not_partially_register(env):
    client,registry,store,version,_=env
    registry.location_store('hq').create('이미 있음','f1',2,1)
    r=client.post(BASE+'/docking-stations',json=payload(version,name='이미 있음'))
    assert r.status_code==422
    r=client.post(BASE+'/locations',json=payload('stale'))
    assert r.status_code==409
    assert store.active_version('f1')==version
    assert len(registry.location_store('hq').list())==1


def test_location_save_failure_does_not_activate_prepared_charger_map(env,monkeypatch):
    client,registry,store,version,notifications=env
    def failed(*args):raise OSError('테스트 저장 실패')
    monkeypatch.setattr(LocationStore,'_save',failed)
    with pytest.raises(OSError):
        client.post(BASE+'/docking-stations',json=payload(version))
    assert store.active_version('f1')==version
    assert registry.location_store('hq').list()==[]
    assert notifications==[]


def test_shared_location_lock_preserves_concurrent_registrations(env):
    _,registry,_,_,_=env
    def create(i):
        registry.location_store('hq').create(f'지점-{i}','f1',2,1)
    with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(create,range(8)))
    assert len(registry.location_store('hq').list())==8

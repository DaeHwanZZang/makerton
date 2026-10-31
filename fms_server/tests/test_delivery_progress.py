"""배송 1건 진행 단계 계산: 묶음 확정 전 예상 목록·같은 세대 이동 1회·엘리베이터 stage 세부·차단."""
from types import SimpleNamespace

from fms_server.delivery_progress import delivery_progress


class Cargo:
    def __init__(self, bundle=None):
        self._bundle = bundle

    def bundle(self, bundle_id):
        return self._bundle


class Jobs:
    def __init__(self, job=None):
        self.job = job

    def get(self, job_id):
        return self.job


def record(phase, **extra):
    d = {'job_id': 'J1', 'phase': phase, 'robot_id': 'R1', 'child_id': 'C1', 'error': None,
         'pickup_name': 'PS', 'return_name': 'PS 반납', 'index': 0, 'stops': [],
         'proposed_items': [{'receipt_no': 'r1', 'dong': 101, 'ho': 301},
                            {'receipt_no': 'r2', 'dong': 101, 'ho': 301}],
         'timeline': [{'mark': 'queued:0', 'phase': 'queued', 'index': 0, 'at': 1.0},
                      {'mark': f'{phase}:0', 'phase': phase, 'index': 0, 'at': 2.0}]}
    d.update(extra)
    return d


def bundle(states, trays):
    return {'bundle_id': 'B-1', 'items': [
        {'parcel_id': 'P-aaaaaaaaaa1', 'tray_id': 'TRAY_A', 'household': '101-301', 'status': states[0]},
        {'parcel_id': 'P-bbbbbbbbbb2', 'tray_id': 'TRAY_B', 'household': '101-301', 'status': states[1]}],
        'trays': {t: {'state': s} for t, s in zip(('TRAY_A', 'TRAY_B'), trays)}}


def test_before_bundle_uses_proposed_items_and_elevator_detail():
    job = SimpleNamespace(status=SimpleNamespace(value='working'), stage=1,
                          plan=[{'t': 'order'}, {'t': 'call', 'elevator_id': 2}])
    p = delivery_progress(record('to_pickup'), Jobs(job), Cargo())
    keys = [s['key'] for s in p['steps']]
    # 같은 세대 2개 → 이동 1회 + 하차 2회
    assert keys == ['assign', 'to_pickup', 'bundle', 'pick:0', 'pick:1', 'move:0',
                    'drop:r1', 'drop:r2', 'return_move', 'return', 'dock']
    assert p['current'] == '픽업 지점 이동' and '엘리베이터 2호기 호출 대기' in p['current_detail']
    assert p['done'] == 1 and p['status'] == 'active' and p['started_at'] == 1.0


def test_delivering_same_household_one_move_and_tray_states():
    stops = [{'household': '101-301', 'target': 'home', 'parcels': ['P-aaaaaaaaaa1', 'P-bbbbbbbbbb2']}]
    d = record('delivering', bundle_id='B-1', stops=stops)
    p = delivery_progress(d, Jobs(None), Cargo(bundle(['delivered', 'unloading'], ['empty_tray', 'unloading'])))
    status = {s['key']: s['status'] for s in p['steps']}
    assert status['move:0'] == 'done' and status['drop:P-aaaaaaaaaa1'] == 'done'
    assert status['drop:P-bbbbbbbbbb2'] == 'active' and status['return'] == 'pending'
    assert p['current'] == '하차 TRAY_B'


def test_blocked_marks_current_step_failed_and_done_counts_all():
    stops = [{'household': '101-301', 'target': 'home', 'parcels': ['P-aaaaaaaaaa1', 'P-bbbbbbbbbb2']}]
    d = record('returning', bundle_id='B-1', stops=stops, index=1, return_trays=['TRAY_A', 'TRAY_B'])
    d['phase'], d['error'] = 'blocked', '반납 실패'
    p = delivery_progress(d, Jobs(None), Cargo(bundle(['delivered', 'delivered'], ['unknown', 'empty_tray'])))
    failed = [s for s in p['steps'] if s['status'] == 'failed']
    assert len(failed) == 1 and failed[0]['detail'] == '반납 실패' and p['status'] == 'blocked'
    assert failed[0]['key'] == 'return'          # 반납 액션이 시작됐으므로 이동은 완료로 본다

    d = record('done', bundle_id='B-1', stops=stops, index=1, return_trays=['TRAY_A', 'TRAY_B'])
    p = delivery_progress(d, Jobs(None), Cargo(bundle(['delivered', 'delivered'], ['none', 'none'])))
    assert p['done'] == p['total'] and p['current'] == '배송 완료' and p['finished_at'] == 2.0

import { test, before, after, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from '../src/api-server.mjs';
import { TOPICS, LockerUnavailableError } from '../src/locker-client.mjs';

const SITE = {
  type: 'SITE_CONFIG', initialized: true, version: 3,
  site: {
    name: '테스트 캠퍼스', building_term: '건물', unit_term: '호실',
    buildings: [{ code: 1, name: '가천관', unit_from: 101, unit_to: 103, missing: [], extra: [], units: [101, 102, 103] }],
    service_start: '08:00', service_end: '22:00', version: 3
  }
};

// 보관소 서버(server/)를 흉내 내는 가짜 MQTT 클라이언트. protocol.md 의 응답 형식을 따른다.
function fakeLocker() {
  const households = new Map([
    ['1-101', { building: 1, unit: 101, default_mode: 'ROBOT', password: '1234', must_change: true }],
    ['1-102', { building: 1, unit: 102, default_mode: 'ROBOT', password: '1234', must_change: true }]
  ]);
  const items = new Map();
  const sessions = new Map();
  const seats = new Map();      // slot_start → [{ receipt_no, building, unit }]
  const notifications = [];
  const calls = [];
  const listeners = new Set();
  let connected = true;
  let robots = [];
  let tokenSeq = 0;

  const household = s => households.get(`${s.building}-${s.unit}`);
  const view = h => ({
    building: h.building, unit: h.unit, building_name: '가천관', label: `가천관 ${h.unit}호`,
    user_id: `${h.building}-${h.unit}`, default_mode: h.default_mode, must_change: h.must_change
  });
  const parcel = it => ({
    receipt_no: it.receipt_no, tracking_no: it.tracking_no, status: it.status, mode: it.mode, plan: it.plan,
    storage_area: it.storage_area, door: it.door, fragile: it.fragile, direct_lock: it.direct_lock,
    choice_deadline: it.plan === 'CHOICE_OPEN' ? '2026-11-01T15:30:00+09:00' : null,
    slot_start: it.slot_start, slot_end: it.slot_start ? it.slot_start.replace(':00:00', ':10:00') : null,
    slot_label: it.slot_start ? '15:00~15:10' : null, change_until: null, robot_id: it.robot_id,
    stored_at: it.stored_at, registered_at: it.stored_at, updated_at: it.stored_at,
    fixed: it.plan === 'AUTO', changeable: ['STORED', 'JOB_ASSIGNED'].includes(it.status) && !it.direct_lock && it.plan !== 'AUTO',
    reservable: it.status === 'STORED' && it.mode === 'ROBOT' && !it.direct_lock && ['CHOICE_OPEN', 'RESERVED'].includes(it.plan)
  });
  const mine = s => [...items.values()].filter(it => it.building === s.building && it.unit === s.unit && !it.hidden);
  const own = (s, receipt) => mine(s).find(it => it.receipt_no === receipt);

  async function request(topic, body) {
    if (!connected) throw new LockerUnavailableError('보관소 서버에 연결되어 있지 않습니다.');
    calls.push({ topic, body });
    if (topic === TOPICS.login) {
      const h = households.get(`${body.building}-${body.unit}`);
      if (!h || body.password !== h.password) return { status: 'DENIED', message: '건물·호실 또는 비밀번호가 올바르지 않습니다.', remaining: 4 };
      const token = `tok_fake${String(++tokenSeq).padStart(16, '0')}`;
      sessions.set(token, { building: h.building, unit: h.unit });
      return { status: 'OK', session: token, ttl_sec: 604800, must_change: h.must_change, household: view(h), parcels: [] };
    }
    if (topic === TOPICS.logout) { sessions.delete(body.session); return { status: 'OK' }; }
    const s = sessions.get(body.session);
    if (!s) return { status: 'EXPIRED_SESSION', message: '로그인이 만료되었습니다.' };
    const h = household(s);
    switch (topic) {
      case TOPICS.parcels:
        return { status: 'OK', server_time: '2026-11-01T14:00:00+09:00', household: view(h), unread: notifications.filter(n => !n.read_at).length, parcels: mine(s).map(parcel) };
      case TOPICS.password:
        if (body.current_password !== h.password) return { status: 'DENIED', message: '현재 비밀번호가 올바르지 않습니다.' };
        if (body.new_password === '1234') return { status: 'INVALID', message: '초기 비밀번호(1234)는 쓸 수 없습니다.' };
        h.password = body.new_password;
        h.must_change = false;
        return { status: 'OK', revoked: 0, household: view(h) };
      case TOPICS.defaultMode: {
        h.default_mode = body.mode;
        let changed = 0; let skipped = 0;
        for (const it of mine(s)) {
          if (it.status !== 'STORED' || it.mode === body.mode) continue;
          if (it.direct_lock || it.plan === 'AUTO') { skipped += 1; continue; }
          it.mode = body.mode;
          it.plan = body.mode === 'ROBOT' ? 'CHOICE_OPEN' : null;
          changed += 1;
        }
        return { status: 'OK', default_mode: body.mode, changed, skipped, parcels: mine(s).map(parcel) };
      }
      case TOPICS.slots:
        return {
          status: 'OK', server_time: '2026-11-01T14:00:00+09:00', service_start: '08:00', service_end: '22:00', seats_per_slot: 2,
          slots: ['2026-11-01T15:00:00+09:00', '2026-11-01T15:10:00+09:00'].map(start => {
            const taken = seats.get(start) || [];
            const my = taken.filter(x => x.building === s.building && x.unit === s.unit);
            return { start, end: start, label: start.slice(11, 16), seats_left: 2 - taken.length, available: 2 - taken.length, restricted: false, mine: my.length, mine_receipts: my.map(x => x.receipt_no) };
          })
        };
      case TOPICS.reserve: {
        const taken = seats.get(body.slot_start) || [];
        const need = body.receipt_nos.filter(r => !taken.some(x => x.receipt_no === r)).length;
        if (2 - taken.length < need) return { status: 'SLOT_FULL', message: '방금 다른 세대가 예약해 자리가 부족합니다.', seats_left: 2 - taken.length };
        for (const r of body.receipt_nos) {
          const it = own(s, r);
          if (!it) return { status: 'NOT_FOUND', message: '내 세대의 택배를 찾을 수 없습니다.' };
          if (it.direct_lock) return { status: 'LOCKED_DIRECT', message: '취급 주의 물품은 직접 수령만 가능합니다.' };
        }
        for (const r of body.receipt_nos) {
          const it = own(s, r);
          taken.push({ receipt_no: r, building: s.building, unit: s.unit });
          it.plan = 'RESERVED';
          it.slot_start = body.slot_start;
        }
        seats.set(body.slot_start, taken);
        return { status: 'OK', slot_start: body.slot_start, slot_end: body.slot_start, label: '15:00~15:10', receipt_nos: body.receipt_nos, parcels: mine(s).map(parcel) };
      }
      case TOPICS.notifications:
        if (body.read === 'all') notifications.forEach(n => { n.read_at = 'now'; });
        else if (Array.isArray(body.read)) notifications.filter(n => body.read.includes(n.id)).forEach(n => { n.read_at = 'now'; });
        return { status: 'OK', unread: notifications.filter(n => !n.read_at).length, notifications };
      default:
    }
    const it = own(s, body.receipt_no);
    if (!it) return { status: 'NOT_FOUND', message: '내 세대의 택배를 찾을 수 없습니다.' };
    switch (topic) {
      case TOPICS.itemMode:
        if (it.direct_lock && body.mode === 'ROBOT') return { status: 'LOCKED_DIRECT', message: '규격 초과 물품은 직접 수령만 가능합니다.' };
        if (it.plan === 'AUTO') return { status: 'FIXED', message: '배송이 확정되어 바꿀 수 없습니다.' };
        it.mode = body.mode;
        it.plan = body.mode === 'ROBOT' ? 'CHOICE_OPEN' : null;
        return { status: 'OK', receipt_no: it.receipt_no, mode: it.mode, parcel: parcel(it) };
      case TOPICS.reserveCancel:
        if (it.plan !== 'RESERVED') return { status: 'NOT_RESERVED', message: '예약된 택배가 아닙니다.' };
        seats.set(it.slot_start, (seats.get(it.slot_start) || []).filter(x => x.receipt_no !== it.receipt_no));
        it.plan = 'CHOICE_OPEN';
        it.slot_start = null;
        return { status: 'OK', receipt_no: it.receipt_no, plan: it.plan, parcels: mine(s).map(parcel) };
      case TOPICS.pickupCode:
        if (it.mode !== 'DIRECT' || it.status !== 'STORED') return { status: 'NOT_AVAILABLE', message: '직접 수령으로 보관 중인 택배만 인증코드를 볼 수 있습니다.' };
        return { status: 'OK', code: it.pickup_code };
      case TOPICS.hide:
        if (!['DELIVERED', 'PICKED'].includes(it.status)) return { status: 'NOT_COMPLETED', message: '배송·수령이 끝난 택배만 삭제할 수 있습니다.' };
        it.hidden = true;
        return { status: 'OK' };
      default:
        return { status: 'INVALID', message: '알 수 없는 요청' };
    }
  }

  return {
    request,
    robots: () => robots,
    site: () => SITE,
    serverOnline: () => true,
    isConnected: () => connected,
    onEvent: fn => { listeners.add(fn); return () => listeners.delete(fn); },
    emit: (kind, message) => listeners.forEach(fn => fn(kind, message)),
    setConnected: value => { connected = value; },
    setRobots: value => { robots = value; },
    addItem: (receipt, fields = {}) => items.set(receipt, {
      receipt_no: receipt, building: 1, unit: 101, status: 'STORED', mode: 'ROBOT', plan: 'CHOICE_OPEN',
      storage_area: 'GRID', door: null, fragile: false, direct_lock: null, slot_start: null, robot_id: null,
      tracking_no: null, pickup_code: '123456', stored_at: '2026-11-01T14:00:00+09:00', ...fields
    }),
    addNotification: n => notifications.push({ read_at: null, ...n }),
    reset() {
      items.clear(); seats.clear(); notifications.length = 0; calls.length = 0;
      households.forEach(h => Object.assign(h, { default_mode: 'ROBOT', password: '1234', must_change: true }));
      connected = true; robots = [];
    },
    expireAll: () => sessions.clear(),
    items, calls, seats
  };
}

let locker, server, base;
before(async () => {
  locker = fakeLocker();
  server = createServer({ locker, loginLimit: 1000 });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  base = `http://127.0.0.1:${server.address().port}`;
});
after(async () => {
  server.closeAllConnections?.();
  await new Promise(resolve => server.close(resolve));
});
beforeEach(() => locker.reset());

const request = (path, options = {}) => fetch(`${base}${path}`, options);
const json = (body, method = 'POST') => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
async function login(unit = 101, password = '1234') {
  const res = await request('/api/auth/login', json({ building: 1, unit, password }));
  assert.equal(res.status, 200);
  return res.headers.get('set-cookie').split(';')[0];
}
const withCookie = (cookie, options = {}) => ({ ...options, headers: { ...(options.headers || {}), cookie } });

test('resident web serves the page and the site config from the locker', async () => {
  assert.equal((await (await request('/api/health')).json()).locker, 'connected');
  const config = await (await request('/api/config')).json();
  assert.deepEqual([config.initialized, config.version, config.site.buildings[0].name], [true, 3, '가천관']);
  assert.match(await (await request('/')).text(), /내 택배/);
  assert.equal((await request('/admin')).status, 404);
  assert.equal((await request('/api/auth/register', json({}))).status, 410);
});

test('login uses building and unit on the resident channel and keeps the locker token', async () => {
  assert.equal((await request('/api/auth/login', json({ building: 'A', unit: 101, password: '1234' }))).status, 400);
  const wrong = await request('/api/auth/login', json({ building: 1, unit: 101, password: '0000' }));
  assert.equal(wrong.status, 401);
  assert.equal((await wrong.json()).error.remaining, 4);
  const cookie = await login();
  assert.match(cookie, /^lg_session=tok_/);
  const me = await (await request('/api/me', withCookie(cookie))).json();
  assert.deepEqual([me.user.name, me.user.building, me.user.unit, me.user.mustChange], ['가천관 101호', '1', '101', true]);
  const sent = locker.calls.find(call => call.topic === TOPICS.login).body;
  assert.deepEqual([sent.channel, sent.building, sent.unit], ['RESIDENT', 1, 101]);
});

test('password change validates digits and clears the initial-password flag', async () => {
  const cookie = await login();
  const change = body => request('/api/user/password', withCookie(cookie, json(body, 'PATCH')));
  assert.equal((await change({ currentPassword: '1234', newPassword: '12a4' })).status, 400);
  const wrong = await change({ currentPassword: '9999', newPassword: '4321' });
  assert.equal((await wrong.json()).error.code, 'WRONG_PASSWORD');
  const ok = await change({ currentPassword: '1234', newPassword: '4321' });
  assert.equal(ok.status, 200);
  assert.equal((await ok.json()).user.mustChange, false);
});

test('parcels carry plan, lock and slot fields', async () => {
  locker.addItem('20261101-AAAA0001');
  locker.addItem('20261101-AAAA0002', { mode: 'DIRECT', plan: null, storage_area: 'OVERSIZE', door: 3, direct_lock: 'OVERSIZE' });
  locker.addItem('20261101-AAAA0003', { status: 'LOADED', plan: 'AUTO', robot_id: 'R1' });
  locker.addItem('20261101-AAAA0004', { status: 'REGISTERED' });
  const cookie = await login();
  const { parcels } = await (await request('/api/user/parcels', withCookie(cookie))).json();
  const byId = Object.fromEntries(parcels.map(p => [p.id, p]));
  assert.equal(parcels.length, 3);
  assert.deepEqual([byId['20261101-AAAA0001'].statusLabel, byId['20261101-AAAA0001'].reservable], ['배송 시간 선택 가능', true]);
  assert.deepEqual([byId['20261101-AAAA0002'].deliveryMethod, byId['20261101-AAAA0002'].door, byId['20261101-AAAA0002'].lockLabel],
    ['PICKUP', 3, '규격 초과 · 직접 수령만 가능']);
  assert.deepEqual([byId['20261101-AAAA0003'].status, byId['20261101-AAAA0003'].robotId], ['IN_TRANSIT', 'R1']);
});

test('default preference reports changed and skipped parcels', async () => {
  locker.addItem('20261101-BBBB0001');
  locker.addItem('20261101-BBBB0002', { plan: 'AUTO' });
  const cookie = await login();
  const res = await request('/api/user/preferences', withCookie(cookie, json({ defaultDelivery: 'PICKUP' }, 'PATCH')));
  const { preference } = await res.json();
  assert.deepEqual([preference.defaultDelivery, preference.changed, preference.skipped], ['PICKUP', 1, 1]);
  assert.equal(locker.calls.at(-1).body.mode, 'DIRECT');
});

test('direct-only parcels cannot switch to robot delivery', async () => {
  locker.addItem('20261101-CCCC0001', { mode: 'DIRECT', plan: null, direct_lock: 'FRAGILE', fragile: true });
  const cookie = await login();
  const res = await request('/api/user/parcels/20261101-CCCC0001/delivery', withCookie(cookie, json({ method: 'ROBOT' }, 'PATCH')));
  assert.equal(res.status, 409);
  assert.equal((await res.json()).error.code, 'DIRECT_ONLY');
});

test('slot reservation is all-or-nothing and reports seats left when full', async () => {
  locker.addItem('20261101-DDDD0001');
  locker.addItem('20261101-DDDD0002');
  locker.addItem('20261101-DDDD0003', { unit: 102 });
  const start = '2026-11-01T15:00:00+09:00';
  const other = await login(102);
  const first = await request('/api/user/reservations', withCookie(other, json({ slotStart: start, parcelIds: ['20261101-DDDD0003'] })));
  assert.equal(first.status, 200);

  const cookie = await login();
  const slots = await (await request('/api/user/slots', withCookie(cookie))).json();
  assert.deepEqual(slots.slots.map(s => s.seatsLeft), [1, 2]);
  const full = await request('/api/user/reservations',
    withCookie(cookie, json({ slotStart: start, parcelIds: ['20261101-DDDD0001', '20261101-DDDD0002'] })));
  assert.equal(full.status, 409);
  const error = (await full.json()).error;
  assert.deepEqual([error.code, error.seatsLeft], ['SLOT_FULL', 1]);
  assert.equal(locker.items.get('20261101-DDDD0001').plan, 'CHOICE_OPEN');

  const ok = await request('/api/user/reservations',
    withCookie(cookie, json({ slotStart: '2026-11-01T15:10:00+09:00', parcelIds: ['20261101-DDDD0001', '20261101-DDDD0002'] })));
  const body = await ok.json();
  assert.equal(ok.status, 200);
  assert.deepEqual(body.parcels.map(p => p.plan), ['RESERVED', 'RESERVED']);
  const cancel = await request('/api/user/reservations/20261101-DDDD0001', withCookie(cookie, { method: 'DELETE' }));
  assert.equal((await cancel.json()).plan, 'CHOICE_OPEN');
  assert.equal((await request('/api/user/reservations', withCookie(cookie, json({ slotStart: start, parcelIds: [] })))).status, 400);
});

test('pickup code and list removal follow locker rules', async () => {
  locker.addItem('20261101-EEEE0001', { pickup_code: '654321' });
  locker.addItem('20261101-EEEE0002', { status: 'DELIVERED', plan: null });
  const cookie = await login();
  const code = '/api/user/parcels/20261101-EEEE0001/pickup-code';
  assert.equal((await request(code, withCookie(cookie))).status, 409);
  locker.items.get('20261101-EEEE0001').mode = 'DIRECT';
  assert.equal((await (await request(code, withCookie(cookie))).json()).code, '654321');
  assert.equal((await request(code, withCookie(await login(102)))).status, 404);
  const remove = id => request(`/api/user/parcels/${id}`, withCookie(cookie, { method: 'DELETE' }));
  assert.equal((await remove('20261101-EEEE0001')).status, 409);
  assert.equal((await remove('20261101-EEEE0002')).status, 200);
});

test('notifications list and mark as read', async () => {
  locker.addNotification({ id: 1, kind: 'ARRIVED_CHOICE', title: '택배가 도착했어요', body: '...', receipt_no: 'X', created_at: 'now' });
  locker.addNotification({ id: 2, kind: 'AUTO_FIXED', title: '자동 배송 확정', body: '...', receipt_no: 'X', created_at: 'now' });
  const cookie = await login();
  const list = await (await request('/api/user/notifications', withCookie(cookie))).json();
  assert.deepEqual([list.unread, list.notifications[0].parcelId, list.notifications[0].read], [2, 'X', false]);
  const one = await (await request('/api/user/notifications/read', withCookie(cookie, json({ ids: [1] })))).json();
  assert.equal(one.unread, 1);
  const all = await (await request('/api/user/notifications/read', withCookie(cookie, json({ all: true })))).json();
  assert.equal(all.unread, 0);
});

test('robot view shows only robots carrying my parcels', async () => {
  locker.setRobots([
    { robot_id: 'R1', status: '배송 중', location: '가천관 1층', battery: 88, receivedAt: Date.now() },
    { robot_id: 'R2', status: '배송 중', location: '비전타워', battery: 90, receivedAt: Date.now() }
  ]);
  locker.addItem('20261101-FFFF0001', { status: 'LOADED', plan: 'AUTO', robot_id: 'R1' });
  locker.addItem('20261101-FFFF0002', { unit: 102, status: 'LOADED', plan: 'AUTO', robot_id: 'R2' });
  const cookie = await login();
  const { robots } = await (await request('/api/user/robots', withCookie(cookie))).json();
  assert.deepEqual(robots.map(r => [r.id, r.connected, r.parcelIds]), [['R1', true, ['20261101-FFFF0001']]]);
});

test('server-sent events deliver only my household notifications', async () => {
  const cookie = await login();
  const controller = new AbortController();
  const res = await request('/api/user/events', withCookie(cookie, { signal: controller.signal }));
  assert.equal(res.headers.get('content-type'), 'text/event-stream; charset=utf-8');
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let text = '';
  const readUntil = async needle => {
    while (!text.includes(needle)) {
      const { value, done } = await reader.read();
      if (done) break;
      text += decoder.decode(value);
    }
  };
  await readUntil('event: ready');
  locker.emit('push', { type: 'NOTIFICATION', household: { building: 1, unit: 102 }, notification: { id: 9, title: '남의 알림' } });
  locker.emit('push', { type: 'NOTIFICATION', household: { building: 1, unit: 101 }, notification: { id: 10, title: '내 알림' } });
  locker.emit('slots', { slots: [{ start: '2026-11-01T15:00:00+09:00', seats_left: 0 }] });
  await readUntil('event: slots');
  controller.abort();
  assert.match(text, /내 알림/);
  assert.doesNotMatch(text, /남의 알림/);
  assert.match(text, /"seatsLeft":0/);
});

test('login attempts are rate limited per address', async () => {
  const limited = createServer({ locker, loginLimit: 2 });
  await new Promise(resolve => limited.listen(0, '127.0.0.1', resolve));
  const url = `http://127.0.0.1:${limited.address().port}/api/auth/login`;
  const attempt = () => fetch(url, json({ building: 1, unit: 101, password: '0000' }));
  assert.equal((await attempt()).status, 401);
  assert.equal((await attempt()).status, 401);
  assert.equal((await attempt()).status, 429);
  await new Promise(resolve => limited.close(resolve));
});

test('locker outages and expired sessions are reported', async () => {
  const cookie = await login();
  locker.setConnected(false);
  assert.equal((await request('/api/user/parcels', withCookie(cookie))).status, 503);
  locker.setConnected(true);
  locker.expireAll();
  const expired = await request('/api/user/parcels', withCookie(cookie));
  assert.equal(expired.status, 401);
  assert.match(expired.headers.get('set-cookie'), /Max-Age=0/);
  assert.equal((await request('/api/me')).status, 401);
});

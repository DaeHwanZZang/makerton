import { test, before, after, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from '../src/api-server.mjs';
import { TOPICS, LockerUnavailableError } from '../src/locker-client.mjs';

// 보관소 서버(server/)를 흉내 내는 가짜 MQTT 클라이언트. protocol.md 의 응답 형식을 따른다.
function fakeLocker() {
  const households = new Map([
    ['101-412', { dong: 101, ho: 412, default_mode: 'ROBOT' }],
    ['101-411', { dong: 101, ho: 411, default_mode: 'ROBOT' }]
  ]);
  const items = new Map();
  const sessions = new Map();
  const calls = [];
  let connected = true;
  let robots = [];
  const changeable = status => ['STORED', 'JOB_ASSIGNED'].includes(status);
  const ownItem = (session, receipt) => {
    const item = items.get(receipt);
    return item && item.dong === session.dong && item.ho === session.ho ? item : null;
  };

  async function request(topic, body) {
    if (!connected) throw new LockerUnavailableError('보관소 서버에 연결되어 있지 않습니다.');
    calls.push({ topic, body });
    if (topic === TOPICS.login) {
      const household = households.get(body.user_id);
      if (!household || body.password !== '1234') return { status: 'DENIED', message: '아이디 또는 비밀번호가 올바르지 않습니다.' };
      const token = `tok_${sessions.size + 1}`;
      sessions.set(token, { ...household, userId: body.user_id });
      return { status: 'OK', session: token, household, parcels: [] };
    }
    if (topic === TOPICS.logout) { sessions.delete(body.session); return { status: 'OK' }; }
    const session = sessions.get(body.session);
    if (!session) return { status: 'EXPIRED_SESSION', message: '로그인이 만료되었습니다.' };
    const household = households.get(session.userId);
    if (topic === TOPICS.parcels) {
      const parcels = [...items.values()].filter(it => it.dong === session.dong && it.ho === session.ho && !it.hidden)
        .map(it => ({ ...it, changeable: changeable(it.status) }));
      return { status: 'OK', household, parcels };
    }
    if (topic === TOPICS.defaultMode) {
      household.default_mode = body.mode;
      let changed = 0;
      for (const it of items.values()) {
        if (it.dong === session.dong && it.ho === session.ho && changeable(it.status) && it.mode !== body.mode) {
          it.mode = body.mode;
          changed += 1;
        }
      }
      return { status: 'OK', default_mode: body.mode, changed };
    }
    const item = ownItem(session, body.receipt_no);
    if (!item) return { status: 'NOT_FOUND', message: '해당 택배를 찾을 수 없습니다.' };
    if (topic === TOPICS.itemMode) {
      if (household.default_mode !== 'ROBOT') return { status: 'DENIED', message: "세대 기본값이 '직접 수령'이면 물품별로 바꿀 수 없습니다." };
      if (!changeable(item.status)) return { status: 'NOT_CHANGEABLE', message: '이미 로봇에 적재된 택배입니다.' };
      item.mode = body.mode;
      return { status: 'OK', receipt_no: item.receipt_no, mode: item.mode };
    }
    if (topic === TOPICS.pickupCode) {
      if (!changeable(item.status) || item.mode !== 'DIRECT') return { status: 'NOT_AVAILABLE', message: '직접 수령으로 보관 중인 택배만 인증코드를 볼 수 있습니다.' };
      return { status: 'OK', code: item.pickup_code };
    }
    if (topic === TOPICS.hide) {
      if (!['DELIVERED', 'PICKED'].includes(item.status)) return { status: 'NOT_COMPLETED', message: '배송·수령이 끝난 택배만 삭제할 수 있습니다.' };
      item.hidden = true;
      return { status: 'OK' };
    }
    return { status: 'INVALID', message: '알 수 없는 요청' };
  }

  return {
    request,
    robots: () => robots,
    isConnected: () => connected,
    setConnected: value => { connected = value; },
    setRobots: value => { robots = value; },
    addItem: (receipt, fields) => items.set(receipt, {
      receipt_no: receipt, dong: 101, ho: 412, size: 'M', status: 'STORED', mode: 'ROBOT', tracking_no: null,
      pickup_code: '123456', registered_at: '2026-10-31T14:00:00+09:00', stored_at: '2026-10-31T14:01:00+09:00',
      updated_at: '2026-10-31T14:01:00+09:00', ...fields
    }),
    expireAll: () => sessions.clear(),
    resetModes: () => households.forEach(household => { household.default_mode = 'ROBOT'; }),
    items, calls
  };
}

let locker, server, base;
before(async () => {
  locker = fakeLocker();
  server = createServer({ locker });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  base = `http://127.0.0.1:${server.address().port}`;
});
after(async () => { await new Promise(resolve => server.close(resolve)); });
beforeEach(() => {
  locker.items.clear();
  locker.resetModes();
  locker.setConnected(true);
  locker.setRobots([]);
});

const request = (path, options = {}) => fetch(`${base}${path}`, options);
const json = body => ({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
async function login(userId = '101-412', password = '1234') {
  const res = await request('/api/auth/login', json({ userId, password }));
  assert.equal(res.status, 200);
  return res.headers.get('set-cookie').split(';')[0];
}
const withCookie = (cookie, options = {}) => ({ ...options, headers: { ...(options.headers || {}), cookie } });

test('user web is served while kiosk, admin and registration are disabled', async () => {
  assert.equal((await (await request('/api/health')).json()).locker, 'connected');
  assert.equal((await (await request('/api/config')).json()).buildings.length, 6);
  assert.match(await (await request('/')).text(), /내 택배/);
  assert.equal((await request('/admin')).status, 404);
  assert.equal((await request('/intake')).status, 404);
  assert.equal((await request('/api/parcels', json({}))).status, 404);
  const register = await request('/api/auth/register', json({}));
  assert.equal(register.status, 410);
});

test('login uses household id and resident channel', async () => {
  assert.equal((await request('/api/auth/login', json({ userId: 'someone@example.com', password: '1234' }))).status, 400);
  assert.equal((await request('/api/auth/login', json({ userId: '101-412', password: '0000' }))).status, 401);
  const cookie = await login();
  const me = await (await request('/api/me', withCookie(cookie))).json();
  assert.deepEqual([me.user.name, me.user.building, me.user.unit, me.user.defaultDelivery], ['101동 412호', '101', '412', 'ROBOT']);
  assert.equal(locker.calls.find(call => call.topic === TOPICS.login).body.channel, 'RESIDENT');
});

test('parcels are mapped from locker statuses and modes', async () => {
  locker.addItem('20261031-AAAA0001', { status: 'JOB_ASSIGNED', mode: 'ROBOT' });
  locker.addItem('20261031-AAAA0002', { status: 'STORED', mode: 'DIRECT', tracking_no: 'CJ1234567890' });
  locker.addItem('20261031-AAAA0003', { status: 'LOADED' });
  locker.addItem('20261031-AAAA0004', { status: 'PICKED', mode: 'DIRECT' });
  locker.addItem('20261031-AAAA0005', { status: 'REGISTERED' });
  const cookie = await login();
  const { parcels } = await (await request('/api/user/parcels', withCookie(cookie))).json();
  const byId = Object.fromEntries(parcels.map(p => [p.id, p]));
  assert.equal(parcels.length, 4);
  assert.deepEqual([byId['20261031-AAAA0001'].status, byId['20261031-AAAA0001'].statusLabel, byId['20261031-AAAA0001'].canChooseDelivery],
    ['STORED', '로봇 운송 준비 중', true]);
  assert.deepEqual([byId['20261031-AAAA0002'].deliveryMethod, byId['20261031-AAAA0002'].trackingNumber], ['PICKUP', 'CJ1234567890']);
  assert.deepEqual([byId['20261031-AAAA0003'].status, byId['20261031-AAAA0003'].canChooseDelivery], ['IN_TRANSIT', false]);
  assert.equal(byId['20261031-AAAA0004'].statusLabel, '수령 완료');
});

test('default preference toggles every changeable parcel through the locker', async () => {
  locker.addItem('20261031-BBBB0001', { status: 'STORED' });
  locker.addItem('20261031-BBBB0002', { status: 'LOADED' });
  const cookie = await login();
  const res = await request('/api/user/preferences', withCookie(cookie, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ defaultDelivery: 'PICKUP' })
  }));
  const { preference } = await res.json();
  assert.deepEqual([preference.defaultDelivery, preference.changed], ['PICKUP', 1]);
  assert.equal(locker.calls.at(-1).body.mode, 'DIRECT');
  assert.equal(locker.items.get('20261031-BBBB0001').mode, 'DIRECT');
  assert.equal(locker.items.get('20261031-BBBB0002').mode, 'ROBOT');
  const current = await (await request('/api/user/preferences', withCookie(cookie))).json();
  assert.equal(current.preference.defaultDelivery, 'PICKUP');
});

test('per-parcel robot choice is locked when the household default is pickup', async () => {
  locker.addItem('20261031-CCCC0001', { status: 'STORED' });
  const cookie = await login();
  const patch = method => request('/api/user/parcels/20261031-CCCC0001/delivery', withCookie(cookie, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ method })
  }));
  const toPickup = await patch('PICKUP');
  assert.equal(toPickup.status, 200);
  assert.equal((await toPickup.json()).parcel.deliveryMethod, 'PICKUP');
  await request('/api/user/preferences', withCookie(cookie, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ defaultDelivery: 'PICKUP' })
  }));
  const toRobot = await patch('ROBOT');
  assert.equal(toRobot.status, 409);
  assert.equal((await toRobot.json()).error.code, 'DELIVERY_CHOICE_LOCKED');
});

test('pickup code is only available for stored direct-pickup parcels', async () => {
  locker.addItem('20261031-DDDD0001', { status: 'STORED', mode: 'ROBOT', pickup_code: '654321' });
  const cookie = await login();
  const path = '/api/user/parcels/20261031-DDDD0001/pickup-code';
  assert.equal((await request(path, withCookie(cookie))).status, 409);
  locker.items.get('20261031-DDDD0001').mode = 'DIRECT';
  assert.equal((await (await request(path, withCookie(cookie))).json()).code, '654321');
  const other = await login('101-411');
  assert.equal((await request(path, withCookie(other))).status, 404);
});

test('only completed parcels can be removed from the list', async () => {
  locker.addItem('20261031-EEEE0001', { status: 'STORED' });
  locker.addItem('20261031-EEEE0002', { status: 'DELIVERED' });
  const cookie = await login();
  const remove = id => request(`/api/user/parcels/${id}`, withCookie(cookie, { method: 'DELETE' }));
  assert.equal((await remove('20261031-EEEE0001')).status, 409);
  assert.equal((await remove('20261031-EEEE0002')).status, 200);
  const { parcels } = await (await request('/api/user/parcels', withCookie(cookie))).json();
  assert.deepEqual(parcels.map(p => p.id), ['20261031-EEEE0001']);
});

test('robot status comes from FMS telemetry and goes stale', async () => {
  locker.setRobots([
    { robot_id: 'R1', status: '배송 중', location: '101동 412호', battery: 88, available: false, receivedAt: Date.now() },
    { robot_id: 'R2', status: '대기 중', location: '공동 보관소', battery: 90, available: true, receivedAt: Date.now() - 60000 }
  ]);
  const cookie = await login();
  const { robots } = await (await request('/api/user/robots', withCookie(cookie))).json();
  assert.deepEqual(robots.map(r => [r.id, r.connected, r.available, r.status]),
    [['R1', true, false, '배송 중'], ['R2', false, false, '통신 끊김']]);
});

test('locker outages and expired locker sessions are reported', async () => {
  const cookie = await login();
  locker.setConnected(false);
  assert.equal((await request('/api/user/parcels', withCookie(cookie))).status, 503);
  locker.setConnected(true);
  locker.expireAll();
  assert.equal((await request('/api/user/parcels', withCookie(cookie))).status, 401);
  assert.equal((await request('/api/me', withCookie(cookie))).status, 401);
});

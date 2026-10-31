import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { createServer } from '../src/api-server.mjs';
import { TOPICS, LockerUnavailableError } from '../src/locker-client.mjs';

// 보관소 서버 관리자 토픽을 흉내 내는 가짜 클라이언트. protocol.md 의 응답 형식을 따른다.
function fakeLocker() {
  const calls = [];
  const sessions = new Set();
  let connected = true;
  let initialized = false;
  async function request(topic, body) {
    if (!connected) throw new LockerUnavailableError('보관소 서버에 연결되어 있지 않습니다.');
    calls.push({ topic, body });
    if (topic === TOPICS.login) {
      if (body.password !== 'admin1234') return { type: 'ADMIN_LOGIN_RESPONSE', status: 'DENIED', message: '아이디 또는 비밀번호가 올바르지 않습니다.', remaining: 4 };
      const token = `tok_admin${String(sessions.size + 1).padStart(16, '0')}`;
      sessions.add(token);
      return { type: 'ADMIN_LOGIN_RESPONSE', request_id: 'x', status: 'OK', session: token, ttl_sec: 1800, admin_id: 'admin', default_password: true, initialized, site_name: null };
    }
    if (topic === TOPICS.logout) { sessions.delete(body.session); return { status: 'OK' }; }
    if (!sessions.has(body.session)) return { status: 'EXPIRED_SESSION', message: '관리자 로그인이 만료되었습니다.' };
    switch (topic) {
      case TOPICS.site:
        return { status: 'OK', initialized, site: null, public: null, defaults: { grid_bins: 300 }, accounts: { total: 0, active: 0, initial_password: 0 } };
      case TOPICS.sitePreview:
        if (!body.site?.name) return { status: 'INVALID', message: '사이트 이름을(를) 입력하세요.' };
        return { status: 'OK', account_count: 36, buildings: [{ code: 1, name: '가천관', count: 36, guide: '101~606호실' }] };
      case TOPICS.siteInit:
        if (initialized) return { status: 'ALREADY_INITIALIZED', message: '이미 초기 설정이 되어 있습니다.' };
        initialized = true;
        return { status: 'OK', site: body.site, accounts: 36 };
      case TOPICS.siteUpdate:
        return { status: 'BLOCKED', message: '보관·배송 중인 물품이 있는 호실은 뺄 수 없습니다: 가천관 101호실', blocked: ['보관·배송 중인 물품이 있는 호실은 뺄 수 없습니다: 가천관 101호실'] };
      case TOPICS.resetPassword:
        return { status: 'OK', user_id: body.user_id, revoked: 1 };
      case TOPICS.accounts:
        return { status: 'OK', building: Number(body.building) || 1, accounts: [], stats: {}, buildings: [] };
      case TOPICS.codePickup:
        return { status: 'OK', confirmed: body.confirm, item: { receipt_no: 'R1', label: '가천관 101호실' } };
      case TOPICS.events:
        return { status: 'OK', events: [], all: body.all, limit: body.limit };
      default:
        return { status: 'OK' };
    }
  }
  return {
    request, calls, site: () => null, serverOnline: () => true, isConnected: () => connected,
    setConnected: value => { connected = value; }
  };
}

let locker, server, base;
before(async () => {
  locker = fakeLocker();
  server = createServer({ locker, loginLimit: 1000 });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  base = `http://127.0.0.1:${server.address().port}`;
});
after(async () => { await new Promise(resolve => server.close(resolve)); });

const json = (body, method = 'POST', headers = {}) => ({ method, headers: { 'Content-Type': 'application/json', ...headers }, body: JSON.stringify(body) });
const withCookie = (cookie, options = {}) => ({ ...options, headers: { ...(options.headers || {}), cookie } });
async function login() {
  const res = await fetch(`${base}/api/admin/login`, json({ password: 'admin1234' }));
  assert.equal(res.status, 200);
  return res.headers.get('set-cookie').split(';')[0];
}

test('admin page is served and admin APIs need a session', async () => {
  assert.match(await (await fetch(`${base}/`)).text(), /LastGate 관리자/);
  assert.equal((await fetch(`${base}/api/admin/site`)).status, 401);
});

test('login keeps the token in an http-only cookie and hides it from the body', async () => {
  const wrong = await fetch(`${base}/api/admin/login`, json({ password: 'nope' }));
  assert.equal(wrong.status, 401);
  assert.equal((await wrong.json()).error.remaining, 4);
  const res = await fetch(`${base}/api/admin/login`, json({ password: 'admin1234' }));
  const body = await res.json();
  assert.equal(body.session, undefined);
  assert.equal(body.default_password, true);
  assert.match(res.headers.get('set-cookie'), /^lg_admin=tok_.*HttpOnly; SameSite=Strict/);
});

test('site preview, init and blocked update map to HTTP statuses', async () => {
  const cookie = await login();
  const preview = await fetch(`${base}/api/admin/site/preview`, withCookie(cookie, json({ site: { name: '테스트' } })));
  assert.equal((await preview.json()).account_count, 36);
  const invalid = await fetch(`${base}/api/admin/site/preview`, withCookie(cookie, json({ site: {} })));
  assert.equal(invalid.status, 400);
  assert.equal((await fetch(`${base}/api/admin/site/init`, withCookie(cookie, json({ site: { name: '테스트' } })))).status, 200);
  const again = await fetch(`${base}/api/admin/site/init`, withCookie(cookie, json({ site: { name: '테스트' } })));
  assert.equal(again.status, 409);
  assert.equal((await again.json()).error.code, 'ALREADY_INITIALIZED');
  const blocked = await fetch(`${base}/api/admin/site`, withCookie(cookie, json({ site: { name: '테스트' } }, 'PUT')));
  assert.equal(blocked.status, 409);
  assert.equal((await blocked.json()).error.blocked.length, 1);
});

test('account and code pickup routes pass ids and flags to the locker', async () => {
  const cookie = await login();
  const reset = await fetch(`${base}/api/admin/accounts/1-101/reset-password`, withCookie(cookie, { method: 'POST' }));
  assert.equal((await reset.json()).revoked, 1);
  assert.equal(locker.calls.at(-1).body.user_id, '1-101');
  assert.equal((await fetch(`${base}/api/admin/accounts/abc/reset-password`, withCookie(cookie, { method: 'POST' }))).status, 404);
  await fetch(`${base}/api/admin/accounts?building=2&q=4`, withCookie(cookie));
  assert.deepEqual([locker.calls.at(-1).body.building, locker.calls.at(-1).body.q], ['2', '4']);
  const pickup = await fetch(`${base}/api/admin/code-pickup`, withCookie(cookie, json({ code: '123456', confirm: true })));
  assert.equal((await pickup.json()).confirmed, true);
  const events = await (await fetch(`${base}/api/admin/events?all=1&limit=50`, withCookie(cookie))).json();
  assert.deepEqual([events.all, events.limit], [true, 50]);
});

test('requests from other hosts and origins are refused', async () => {
  const cookie = await login();
  // fetch 는 Host 헤더를 못 바꾸므로 http.request 로 보낸다 (DNS 리바인딩 흉내)
  const rebinding = await new Promise((resolve, reject) => {
    const req = http.request(`${base}/api/admin/site`, { headers: { cookie, host: 'evil.example.com' } }, resolve);
    req.on('error', reject);
    req.end();
  });
  rebinding.resume();
  assert.equal(rebinding.statusCode, 403);
  const crossSite = await fetch(`${base}/api/admin/site/init`, withCookie(cookie, json({ site: {} }, 'POST', { origin: 'http://evil.example.com' })));
  assert.equal(crossSite.status, 403);
});

test('expired sessions clear the cookie and locker outages return 503', async () => {
  const stale = 'lg_admin=tok_admin9999999999999999';
  const res = await fetch(`${base}/api/admin/site`, withCookie(stale));
  assert.equal(res.status, 401);
  assert.match(res.headers.get('set-cookie'), /Max-Age=0/);
  const cookie = await login();
  locker.setConnected(false);
  assert.equal((await fetch(`${base}/api/admin/site`, withCookie(cookie))).status, 503);
  locker.setConnected(true);
});

test('login attempts are rate limited', async () => {
  const limited = createServer({ locker, loginLimit: 1 });
  await new Promise(resolve => limited.listen(0, '127.0.0.1', resolve));
  const url = `http://127.0.0.1:${limited.address().port}/api/admin/login`;
  assert.equal((await fetch(url, json({ password: 'x' }))).status, 401);
  assert.equal((await fetch(url, json({ password: 'x' }))).status, 429);
  await new Promise(resolve => limited.close(resolve));
});

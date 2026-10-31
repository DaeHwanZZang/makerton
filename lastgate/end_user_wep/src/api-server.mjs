import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { STATUSES, ValidationError, userNotification } from './parcel-service.mjs';
import {
  sessionFromToken, loginUser, logoutUser, changePassword, getMe, getOverview, listUserParcels,
  getUserPickupCode, chooseDelivery, hideCompletedParcel, listUserRobots, getDeliveryPreference,
  updateDeliveryPreference, listSlots, reserveSlot, cancelReservation, listNotifications, readNotifications
} from './user-service.mjs';

const PUBLIC_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../public');
const ASSETS = {
  '/': ['user.html', 'text/html; charset=utf-8'],
  '/index.html': ['user.html', 'text/html; charset=utf-8'],
  '/user': ['user.html', 'text/html; charset=utf-8'],
  '/user.html': ['user.html', 'text/html; charset=utf-8'],
  '/assets/user.js': ['assets/user.js', 'text/javascript; charset=utf-8'],
  '/assets/user.css': ['assets/user.css', 'text/css; charset=utf-8']
};
const LOGIN_LIMIT = 10;                 // IP 당 5분에 로그인 10번 (계정 잠금은 보관소 서버가 5회 실패 시)
const LOGIN_WINDOW_MS = 5 * 60 * 1000;
const SSE_PING_MS = 25000;

const send = (res, status, body, headers = {}) => {
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
    ...headers
  });
  res.end(JSON.stringify(body));
};
const fail = (res, status, code, message, extra = {}) => send(res, status, { error: { code, message, ...extra } });

function cookies(req) {
  return Object.fromEntries(String(req.headers.cookie || '').split(';').map(part => {
    const index = part.indexOf('=');
    if (index < 0) return ['', ''];
    return [part.slice(0, index).trim(), decodeURIComponent(part.slice(index + 1).trim())];
  }).filter(([key]) => key));
}

function sessionCookie(token, maxAgeSec) {
  const secure = process.env.NODE_ENV === 'production' ? '; Secure' : '';
  return `lg_session=${encodeURIComponent(token)}; Path=/; HttpOnly; SameSite=Lax; Max-Age=${maxAgeSec}${secure}`;
}
const CLEAR_COOKIE = 'lg_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0';

function requireSession(req) {
  const session = sessionFromToken(cookies(req).lg_session);
  if (!session) throw new ValidationError('AUTH_REQUIRED', '로그인이 필요합니다.', 401);
  return session;
}

async function parseJson(req) {
  let body = '';
  for await (const chunk of req) {
    body += chunk;
    if (body.length > 32768) throw new ValidationError('BODY_TOO_LARGE', '요청 데이터가 너무 큽니다.', 413);
  }
  try { return JSON.parse(body || '{}'); }
  catch { throw new ValidationError('INVALID_JSON', 'JSON 요청 형식이 올바르지 않습니다.'); }
}

function createRateLimiter(limit, windowMs) {
  const hits = new Map();
  return key => {
    const now = Date.now();
    for (const [k, v] of hits) if (v.resetAt <= now) hits.delete(k);
    const entry = hits.get(key) || { count: 0, resetAt: now + windowMs };
    entry.count += 1;
    hits.set(key, entry);
    if (entry.count > limit) {
      throw new ValidationError('TOO_MANY_ATTEMPTS', '로그인 시도가 너무 많습니다. 잠시 후 다시 시도해 주세요.', 429,
        { retryAfterSec: Math.ceil((entry.resetAt - now) / 1000) });
    }
  };
}

// 실시간 알림(SSE). 보관소 서버가 세대 웹 서버로 보낸 푸시를 해당 세대 브라우저에만 전달한다.
function createEventHub(locker) {
  const clients = new Set();   // { res, key: "건물-호실" }
  const write = (res, event, data) => res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
  const toHousehold = (household, event, data) => {
    if (!household) return;
    const key = `${household.building}-${household.unit}`;
    for (const client of clients) if (client.key === key) write(client.res, event, data);
  };
  const toAll = (event, data) => { for (const client of clients) write(client.res, event, data); };

  locker.onEvent?.((kind, message) => {
    if (kind === 'push' && message.type === 'NOTIFICATION') {
      toHousehold(message.household, 'notification', userNotification(message.notification));
    } else if (kind === 'push' && message.type === 'PARCELS_UPDATE') {
      toHousehold(message.household, 'parcels', { at: Date.now() });
    } else if (kind === 'slots') {
      toAll('slots', { slots: (message.slots || []).map(s => ({ start: s.start, seatsLeft: s.seats_left })) });
    } else if (kind === 'site') {
      toAll('config', { initialized: Boolean(message.initialized), version: message.version ?? 0 });
    } else if (kind === 'server') {
      toAll('server', { online: message.online });
    }
  });
  const ping = setInterval(() => { for (const client of clients) client.res.write(': ping\n\n'); }, SSE_PING_MS);
  ping.unref();

  return {
    open(req, res, household) {
      res.writeHead(200, {
        'Content-Type': 'text/event-stream; charset=utf-8',
        'Cache-Control': 'no-store',
        Connection: 'keep-alive',
        'X-Accel-Buffering': 'no'
      });
      res.write('retry: 5000\n\n');
      const client = { res, key: `${household.building}-${household.unit}` };
      clients.add(client);
      write(res, 'ready', { household: client.key });
      req.on('close', () => clients.delete(client));
    },
    size: () => clients.size,
    close() {
      clearInterval(ping);
      for (const client of clients) client.res.end();
      clients.clear();
    }
  };
}

function siteConfig(locker) {
  const message = locker.site();
  if (!message) return { initialized: null, version: 0, site: null };   // 아직 수신 전
  return { initialized: Boolean(message.initialized), version: message.version ?? 0, site: message.site || null };
}

// locker: 보관소 서버 MQTT 클라이언트 (src/locker-client.mjs). 테스트에서는 가짜를 넣는다.
export function createServer({ locker, loginLimit = LOGIN_LIMIT }) {
  if (!locker) throw new Error('Locker client is required');
  const hub = createEventHub(locker);
  const limitLogin = createRateLimiter(loginLimit, LOGIN_WINDOW_MS);
  const server = http.createServer(async (req, res) => {
    res.setHeader('X-Content-Type-Options', 'nosniff');
    res.setHeader('X-Frame-Options', 'DENY');
    res.setHeader('Referrer-Policy', 'no-referrer');
    const url = new URL(req.url, 'http://localhost');
    const pathname = url.pathname;
    const route = (method, pattern) => {
      if (req.method !== method) return null;
      if (typeof pattern === 'string') return pathname === pattern ? [] : null;
      const match = pathname.match(pattern);
      return match ? match.slice(1) : null;
    };
    try {
      if (route('GET', '/api/health')) {
        return send(res, 200, {
          ok: true, service: 'lastgate-resident-web',
          locker: locker.isConnected() ? 'connected' : 'disconnected',
          server: locker.serverOnline?.() ?? null
        });
      }
      if (route('GET', '/api/config')) {
        return send(res, 200, { ...siteConfig(locker), statuses: STATUSES });
      }
      if (route('POST', '/api/auth/register')) {
        throw new ValidationError('REGISTRATION_DISABLED',
          '세대 계정은 관리자 초기 설정 때 미리 만들어집니다. 건물·호실과 초기 비밀번호로 로그인해 주세요.', 410);
      }
      if (route('POST', '/api/auth/login')) {
        limitLogin(req.socket.remoteAddress || 'unknown');
        const { user, token, ttlSec } = await loginUser(locker, await parseJson(req));
        return send(res, 200, { user }, { 'Set-Cookie': sessionCookie(token, ttlSec || 7 * 86400) });
      }
      if (route('POST', '/api/auth/logout')) {
        await logoutUser(locker, sessionFromToken(cookies(req).lg_session));
        return send(res, 200, { ok: true }, { 'Set-Cookie': CLEAR_COOKIE });
      }
      if (route('GET', '/api/me')) {
        return send(res, 200, await getMe(locker, requireSession(req)));
      }
      if (route('PATCH', '/api/user/password')) {
        const session = requireSession(req);
        return send(res, 200, await changePassword(locker, session, await parseJson(req)));
      }
      if (route('GET', '/api/user/preferences')) {
        return send(res, 200, { preference: await getDeliveryPreference(locker, requireSession(req)) });
      }
      if (route('PATCH', '/api/user/preferences')) {
        const session = requireSession(req);
        return send(res, 200, { preference: await updateDeliveryPreference(locker, session, await parseJson(req)) });
      }
      if (route('GET', '/api/user/parcels')) {
        return send(res, 200, await listUserParcels(locker, requireSession(req)));
      }
      let params;
      if ((params = route('GET', /^\/api\/user\/parcels\/([A-Z0-9-]+)\/pickup-code$/))) {
        return send(res, 200, await getUserPickupCode(locker, requireSession(req), params[0]));
      }
      if ((params = route('PATCH', /^\/api\/user\/parcels\/([A-Z0-9-]+)\/delivery$/))) {
        const session = requireSession(req);
        return send(res, 200, { parcel: await chooseDelivery(locker, session, params[0], await parseJson(req)) });
      }
      if ((params = route('DELETE', /^\/api\/user\/parcels\/([A-Z0-9-]+)$/))) {
        return send(res, 200, await hideCompletedParcel(locker, requireSession(req), params[0]));
      }
      if (route('GET', '/api/user/slots')) {
        return send(res, 200, await listSlots(locker, requireSession(req)));
      }
      if (route('POST', '/api/user/reservations')) {
        const session = requireSession(req);
        return send(res, 200, await reserveSlot(locker, session, await parseJson(req)));
      }
      if ((params = route('DELETE', /^\/api\/user\/reservations\/([A-Z0-9-]+)$/))) {
        return send(res, 200, await cancelReservation(locker, requireSession(req), params[0]));
      }
      if (route('GET', '/api/user/notifications')) {
        return send(res, 200, await listNotifications(locker, requireSession(req)));
      }
      if (route('POST', '/api/user/notifications/read')) {
        const session = requireSession(req);
        return send(res, 200, await readNotifications(locker, session, await parseJson(req)));
      }
      if (route('GET', '/api/user/robots')) {
        return send(res, 200, { robots: await listUserRobots(locker, requireSession(req)) });
      }
      if (route('GET', '/api/user/events')) {
        const { household } = await getOverview(locker, requireSession(req));   // 세션 확인 겸 세대 확인
        return hub.open(req, res, household);
      }
      // 택배 접수와 관리자 기능은 웹 키오스크(web/)·관리자 웹(admin_web/)이 담당한다.
      if (req.method === 'GET' && ASSETS[pathname]) {
        const [file, mime] = ASSETS[pathname];
        const contents = await fs.readFile(path.join(PUBLIC_DIR, file));
        res.writeHead(200, { 'Content-Type': mime, 'Cache-Control': 'no-cache' });
        return res.end(contents);
      }
      return fail(res, 404, 'NOT_FOUND', '요청한 페이지나 API를 찾을 수 없습니다.');
    } catch (err) {
      if (err instanceof ValidationError) {
        const headers = err.status === 401 ? { 'Set-Cookie': CLEAR_COOKIE } : {};
        return send(res, err.status, { error: { code: err.code, message: err.message, ...err.extra } }, headers);
      }
      console.error('API error:', err);
      return fail(res, 500, 'INTERNAL_ERROR', '서버 처리 중 문제가 발생했습니다.');
    }
  });
  server.on('close', () => hub.close());
  server.hub = hub;
  return server;
}

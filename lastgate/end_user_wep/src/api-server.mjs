import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { BUILDINGS, SIZES, STATUSES, ValidationError } from './parcel-service.mjs';
import {
  registerUser, loginUser, createSessionStore, createSession, getSession, deleteSession,
  listUserParcels, getUserPickupCode, chooseDelivery, hideCompletedParcel, listUserRobots,
  getDeliveryPreference, updateDeliveryPreference
} from './user-service.mjs';

const PUBLIC_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../public');
const ASSETS = {
  '/': ['user.html', 'text/html; charset=utf-8'],
  '/index.html': ['user.html', 'text/html; charset=utf-8'],
  // 기사 키오스크·운영자 화면은 web/ 의 웹 키오스크로 대체되어 제공하지 않는다.
  // '/intake', '/pickup': ['index.html', ...], '/admin': ['admin.html', ...]
  '/user': ['user.html', 'text/html; charset=utf-8'],
  '/user.html': ['user.html', 'text/html; charset=utf-8'],
  '/assets/user.js': ['assets/user.js', 'text/javascript; charset=utf-8'],
  '/assets/user.css': ['assets/user.css', 'text/css; charset=utf-8']
};

const send = (res, status, body, headers = {}) => {
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
    ...headers
  });
  res.end(JSON.stringify(body));
};
const fail = (res, status, code, message) => send(res, status, { error: { code, message } });

function cookies(req) {
  return Object.fromEntries(String(req.headers.cookie || '').split(';').map(part => {
    const index = part.indexOf('=');
    if (index < 0) return ['', ''];
    return [part.slice(0, index).trim(), decodeURIComponent(part.slice(index + 1).trim())];
  }).filter(([key]) => key));
}

function sessionCookie(token, expiresAt) {
  const secure = process.env.NODE_ENV === 'production' ? '; Secure' : '';
  return `lg_session=${encodeURIComponent(token)}; Path=/; HttpOnly; SameSite=Lax; Expires=${expiresAt.toUTCString()}${secure}`;
}

function requireSession(sessions, req) {
  const session = getSession(sessions, cookies(req).lg_session);
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

// locker: 보관소 서버 MQTT 클라이언트 (src/locker-client.mjs). 테스트에서는 가짜를 넣는다.
export function createServer({ locker, sessions = createSessionStore() }) {
  if (!locker) throw new Error('Locker client is required');
  return http.createServer(async (req, res) => {
    res.setHeader('X-Content-Type-Options', 'nosniff');
    res.setHeader('X-Frame-Options', 'DENY');
    res.setHeader('Referrer-Policy', 'no-referrer');
    const url = new URL(req.url, 'http://localhost');
    const pathname = url.pathname;
    try {
      if (req.method === 'GET' && pathname === '/api/health') {
        return send(res, 200, {
          ok: true, service: 'lastgate-resident-web', locker: locker.isConnected() ? 'connected' : 'disconnected'
        });
      }
      if (req.method === 'GET' && pathname === '/api/config') {
        return send(res, 200, { buildings: BUILDINGS, sizes: SIZES, statuses: STATUSES });
      }
      if (req.method === 'POST' && pathname === '/api/auth/register') {
        return registerUser();
      }
      if (req.method === 'POST' && pathname === '/api/auth/login') {
        const { user, lockerSession } = await loginUser(locker, await parseJson(req));
        const session = createSession(sessions, { user, lockerSession });
        return send(res, 200, { user }, { 'Set-Cookie': sessionCookie(session.token, session.expiresAt) });
      }
      if (req.method === 'POST' && pathname === '/api/auth/logout') {
        await deleteSession(sessions, locker, cookies(req).lg_session);
        return send(res, 200, { ok: true }, { 'Set-Cookie': 'lg_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0' });
      }
      if (req.method === 'GET' && pathname === '/api/me') {
        return send(res, 200, { user: requireSession(sessions, req).user });
      }
      if (pathname === '/api/user/preferences') {
        const session = requireSession(sessions, req);
        if (req.method === 'GET') return send(res, 200, { preference: await getDeliveryPreference(locker, session) });
        if (req.method === 'PATCH') {
          return send(res, 200, { preference: await updateDeliveryPreference(locker, session, await parseJson(req)) });
        }
      }
      if (req.method === 'GET' && pathname === '/api/user/parcels') {
        const session = requireSession(sessions, req);
        return send(res, 200, { parcels: await listUserParcels(locker, session) });
      }
      const userPickupCode = pathname.match(/^\/api\/user\/parcels\/([A-Z0-9-]+)\/pickup-code$/);
      if (req.method === 'GET' && userPickupCode) {
        const session = requireSession(sessions, req);
        return send(res, 200, await getUserPickupCode(locker, session, userPickupCode[1]), { 'Cache-Control': 'no-store' });
      }
      const userParcelRemoval = pathname.match(/^\/api\/user\/parcels\/([A-Z0-9-]+)$/);
      if (req.method === 'DELETE' && userParcelRemoval) {
        const session = requireSession(sessions, req);
        return send(res, 200, await hideCompletedParcel(locker, session, userParcelRemoval[1]));
      }
      if (req.method === 'GET' && pathname === '/api/user/robots') {
        requireSession(sessions, req);
        return send(res, 200, { robots: listUserRobots(locker) });
      }
      const userDelivery = pathname.match(/^\/api\/user\/parcels\/([A-Z0-9-]+)\/delivery$/);
      if (req.method === 'PATCH' && userDelivery) {
        const session = requireSession(sessions, req);
        return send(res, 200, { parcel: await chooseDelivery(locker, session, userDelivery[1], await parseJson(req)) });
      }
      // 택배 접수(POST /api/parcels)와 관리자 API 는 보관소 서버·웹 키오스크가 담당한다.
      if (req.method === 'GET' && ASSETS[pathname]) {
        const [file, mime] = ASSETS[pathname];
        const contents = await fs.readFile(path.join(PUBLIC_DIR, file));
        res.writeHead(200, { 'Content-Type': mime, 'Cache-Control': 'no-cache' });
        return res.end(contents);
      }
      return fail(res, 404, 'NOT_FOUND', '요청한 페이지나 API를 찾을 수 없습니다.');
    } catch (err) {
      if (err instanceof ValidationError) return fail(res, err.status, err.code, err.message);
      console.error('API error:', err);
      return fail(res, 500, 'INTERNAL_ERROR', '서버 처리 중 문제가 발생했습니다.');
    }
  });
}

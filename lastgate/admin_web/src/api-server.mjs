// 관리자 웹 서버(BFF). REST(/api/admin/*) 요청을 보관소 서버 관리자 토픽(MQTT)으로 옮긴다.
// 계정·설정은 보관소 서버가 저장하고, 여기서는 세션 쿠키(lg_admin = 보관소 서버 관리자 토큰)만 다룬다.
// 보관소 PC 전용: 127.0.0.1 에만 열고, Host·Origin 을 확인해 다른 사이트에서 오는 요청을 막는다.
import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { TOPICS, LockerUnavailableError } from './locker-client.mjs';

const PUBLIC_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../public');
const ASSETS = {
  '/': ['admin.html', 'text/html; charset=utf-8'],
  '/index.html': ['admin.html', 'text/html; charset=utf-8'],
  '/assets/admin.js': ['assets/admin.js', 'text/javascript; charset=utf-8'],
  '/assets/admin.css': ['assets/admin.css', 'text/css; charset=utf-8']
};
const LOCAL_HOSTS = new Set(['127.0.0.1', 'localhost', '[::1]']);
const TOKEN_RE = /^tok_[A-Za-z0-9_-]{16,64}$/;
const LOGIN_LIMIT = 10;
const LOGIN_WINDOW_MS = 5 * 60 * 1000;

// 보관소 서버 응답 상태 → HTTP 상태
const STATUS_HTTP = {
  OK: 200, INVALID: 400, DENIED: 403, EXPIRED_SESSION: 401, NOT_FOUND: 404, LOCKED: 423,
  ALREADY_INITIALIZED: 409, NOT_INITIALIZED: 409, BLOCKED: 409, MISMATCH: 409, ALREADY_LOADED: 409
};

export class HttpError extends Error {
  constructor(status, code, message, extra = {}) {
    super(message);
    Object.assign(this, { status, code, extra });
  }
}

const send = (res, status, body, headers = {}) => {
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store', ...headers });
  res.end(JSON.stringify(body));
};

function cookies(req) {
  return Object.fromEntries(String(req.headers.cookie || '').split(';').map(part => {
    const index = part.indexOf('=');
    return index < 0 ? ['', ''] : [part.slice(0, index).trim(), decodeURIComponent(part.slice(index + 1).trim())];
  }).filter(([key]) => key));
}
// 브라우저를 닫으면 사라지는 쿠키. 30분 무활동 만료는 보관소 서버가 판단한다.
const sessionCookie = token => `lg_admin=${encodeURIComponent(token)}; Path=/; HttpOnly; SameSite=Strict`;
const CLEAR_COOKIE = 'lg_admin=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0';

async function parseJson(req) {
  let body = '';
  for await (const chunk of req) {
    body += chunk;
    if (body.length > 1024 * 1024) throw new HttpError(413, 'BODY_TOO_LARGE', '요청 데이터가 너무 큽니다.');
  }
  try { return JSON.parse(body || '{}'); }
  catch { throw new HttpError(400, 'INVALID_JSON', 'JSON 요청 형식이 올바르지 않습니다.'); }
}

function hostName(value) {
  try { return new URL(`http://${value}`).hostname; } catch { return ''; }
}

// DNS 리바인딩·다른 사이트의 폼 전송을 막는다.
function checkOrigin(req, allowedHosts) {
  const host = hostName(req.headers.host || '');
  if (!allowedHosts.has(host)) throw new HttpError(403, 'FORBIDDEN_HOST', '관리자 웹은 보관소 PC 에서만 열 수 있습니다.');
  if (req.method !== 'GET' && req.method !== 'HEAD') {
    const origin = req.headers.origin;
    if (origin && hostName(origin.replace(/^https?:\/\//, '')) !== host) {
      throw new HttpError(403, 'FORBIDDEN_ORIGIN', '다른 사이트에서 보낸 요청은 받지 않습니다.');
    }
  }
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
      throw new HttpError(429, 'TOO_MANY_ATTEMPTS', '로그인 시도가 너무 많습니다. 잠시 후 다시 시도하세요.',
        { retry_after_sec: Math.ceil((entry.resetAt - now) / 1000) });
    }
  };
}

async function request(locker, topic, body) {
  try {
    return await locker.request(topic, body);
  } catch (error) {
    if (error instanceof LockerUnavailableError) throw new HttpError(503, 'LOCKER_UNAVAILABLE', error.message);
    throw error;
  }
}

// 응답을 그대로 넘기되 MQTT 전용 필드(type, request_id)는 뺀다. 실패는 { error: {...} } 로 바꾼다.
function toHttp(result, overrides = {}) {
  const { type, request_id: _, status, message, ...rest } = result;
  if (status === 'OK') return rest;
  const http = overrides[status] ?? STATUS_HTTP[status] ?? 502;
  throw new HttpError(http, status || 'LOCKER_ERROR', message || '보관소 서버가 요청을 처리하지 못했습니다.', rest);
}

const encodeId = value => String(value ?? '').trim();

// [메서드, 경로(문자열|정규식), 토픽, 요청 본문 만들기]. 세션이 필요한 관리자 API 표.
const ROUTES = [
  ['PATCH', '/api/admin/password', TOPICS.password, ({ body }) => ({
    current_password: String(body.currentPassword ?? ''), new_password: String(body.newPassword ?? '') })],
  ['GET', '/api/admin/site', TOPICS.site, () => ({})],
  ['POST', '/api/admin/site/preview', TOPICS.sitePreview, ({ body }) => ({ site: body.site })],
  ['POST', '/api/admin/site/init', TOPICS.siteInit, ({ body }) => ({ site: body.site })],
  ['PUT', '/api/admin/site', TOPICS.siteUpdate, ({ body }) => ({ site: body.site })],
  ['POST', '/api/admin/site/reset', TOPICS.siteReset, ({ body }) => ({
    password: String(body.password ?? ''), confirm_name: String(body.confirmName ?? '') })],
  ['GET', '/api/admin/accounts', TOPICS.accounts, ({ url }) => ({
    building: url.searchParams.get('building') || undefined, q: url.searchParams.get('q') || undefined })],
  ['POST', /^\/api\/admin\/accounts\/([0-9]+-[0-9]+)\/reset-password$/, TOPICS.resetPassword, ({ params }) => ({ user_id: params[0] })],
  ['POST', /^\/api\/admin\/accounts\/([0-9]+-[0-9]+)\/change-occupant$/, TOPICS.changeOccupant, ({ params }) => ({ user_id: params[0] })],
  ['POST', /^\/api\/admin\/accounts\/([0-9]+-[0-9]+)\/unlock$/, TOPICS.unlock, ({ params }) => ({ user_id: params[0] })],
  ['GET', '/api/admin/storage', TOPICS.storage, () => ({})],
  ['POST', '/api/admin/code-pickup', TOPICS.codePickup, ({ body }) => ({
    code: encodeId(body.code), confirm: body.confirm === true })],
  ['GET', '/api/admin/events', TOPICS.events, ({ url }) => ({
    all: url.searchParams.get('all') === '1', limit: Number(url.searchParams.get('limit')) || 100 })]
];

export function createServer({ locker, port = 4190, loginLimit = LOGIN_LIMIT, extraHosts = [] }) {
  if (!locker) throw new Error('Locker client is required');
  const allowedHosts = new Set([...LOCAL_HOSTS, ...extraHosts]);
  const limitLogin = createRateLimiter(loginLimit, LOGIN_WINDOW_MS);

  return http.createServer(async (req, res) => {
    res.setHeader('X-Content-Type-Options', 'nosniff');
    res.setHeader('X-Frame-Options', 'DENY');
    res.setHeader('Referrer-Policy', 'no-referrer');
    res.setHeader('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'");
    const url = new URL(req.url, `http://localhost:${port}`);
    const pathname = url.pathname;
    try {
      checkOrigin(req, allowedHosts);
      if (req.method === 'GET' && pathname === '/api/health') {
        return send(res, 200, {
          ok: true, service: 'lastgate-admin-web',
          locker: locker.isConnected() ? 'connected' : 'disconnected', server: locker.serverOnline?.() ?? null
        });
      }
      if (req.method === 'GET' && pathname === '/api/admin/config') {
        const site = locker.site();
        return send(res, 200, { initialized: site ? Boolean(site.initialized) : null, version: site?.version ?? 0,
          site_name: site?.site?.name ?? null });
      }
      if (req.method === 'POST' && pathname === '/api/admin/login') {
        limitLogin(req.socket.remoteAddress || 'unknown');
        const body = await parseJson(req);
        const result = toHttp(await request(locker, TOPICS.login, {
          admin_id: String(body.adminId || 'admin'), password: String(body.password ?? '')
        }), { DENIED: 401 });
        const { session, ...rest } = result;
        return send(res, 200, rest, { 'Set-Cookie': sessionCookie(session) });
      }
      if (req.method === 'POST' && pathname === '/api/admin/logout') {
        const token = cookies(req).lg_admin;
        if (token && TOKEN_RE.test(token)) await request(locker, TOPICS.logout, { session: token }).catch(() => {});
        return send(res, 200, { ok: true }, { 'Set-Cookie': CLEAR_COOKIE });
      }
      for (const [method, pattern, topic, build] of ROUTES) {
        if (req.method !== method) continue;
        const params = typeof pattern === 'string' ? (pathname === pattern ? [] : null) : pathname.match(pattern)?.slice(1);
        if (!params) continue;
        const token = cookies(req).lg_admin;
        if (!token || !TOKEN_RE.test(token)) throw new HttpError(401, 'AUTH_REQUIRED', '관리자 로그인이 필요합니다.');
        const body = ['POST', 'PUT', 'PATCH'].includes(method) ? await parseJson(req) : {};
        const result = await request(locker, topic, { ...build({ body, url, params }), session: token });
        return send(res, 200, toHttp(result));
      }
      if (req.method === 'GET' && ASSETS[pathname]) {
        const [file, mime] = ASSETS[pathname];
        const contents = await fs.readFile(path.join(PUBLIC_DIR, file));
        res.writeHead(200, { 'Content-Type': mime, 'Cache-Control': 'no-cache' });
        return res.end(contents);
      }
      throw new HttpError(404, 'NOT_FOUND', '요청한 페이지나 API를 찾을 수 없습니다.');
    } catch (error) {
      if (error instanceof HttpError) {
        const headers = error.status === 401 && error.code !== 'DENIED' ? { 'Set-Cookie': CLEAR_COOKIE } : {};
        return send(res, error.status, { error: { code: error.code, message: error.message, ...error.extra } }, headers);
      }
      console.error('API error:', error);
      return send(res, 500, { error: { code: 'INTERNAL_ERROR', message: '서버 처리 중 문제가 발생했습니다.' } });
    }
  });
}

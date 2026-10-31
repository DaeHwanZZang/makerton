import crypto from 'node:crypto';
import { ValidationError, fromMode, toMode, userParcel } from './parcel-service.mjs';
import { TOPICS, LockerUnavailableError } from './locker-client.mjs';

const SESSION_DAYS = 7;
const ROBOT_STALE_MS = 30000; // FMS 하트비트(10초)가 3번 끊기면 통신 끊김으로 표시

// 보관소 서버 응답 상태 → HTTP 오류
const LOCKER_ERRORS = {
  DENIED: [409, 'DELIVERY_CHOICE_LOCKED'],
  NOT_CHANGEABLE: [409, 'DELIVERY_CHOICE_LOCKED'],
  NOT_AVAILABLE: [409, 'PICKUP_CODE_UNAVAILABLE'],
  NOT_COMPLETED: [409, 'PARCEL_NOT_DELIVERED'],
  NOT_FOUND: [404, 'NOT_FOUND'],
  INVALID: [400, 'INVALID_INPUT']
};

async function send(locker, topic, body) {
  try {
    return await locker.request(topic, body);
  } catch (error) {
    if (error instanceof LockerUnavailableError) throw new ValidationError('LOCKER_UNAVAILABLE', error.message, 503);
    throw error;
  }
}

// 로그인한 세대의 보관소 세션으로 요청하고, 실패 상태를 HTTP 오류로 바꾼다.
async function call(locker, session, topic, body = {}) {
  const result = await send(locker, topic, { ...body, session: session.lockerSession });
  if (result.status === 'OK') return result;
  if (result.status === 'EXPIRED_SESSION') {
    session.expired = true;
    throw new ValidationError('AUTH_REQUIRED', '로그인이 만료되었습니다. 다시 로그인해 주세요.', 401);
  }
  const [status, code] = LOCKER_ERRORS[result.status] || [502, 'LOCKER_ERROR'];
  throw new ValidationError(code, result.message || '보관소 서버가 요청을 처리하지 못했습니다.', status);
}

function publicUser(userId, household) {
  return {
    id: userId,
    userId,
    name: `${household.dong}동 ${household.ho}호`,
    building: String(household.dong),
    unit: String(household.ho),
    defaultDelivery: fromMode(household.default_mode)
  };
}

function deliveryMethod(value, message) {
  const method = String(value ?? '').trim().toUpperCase();
  if (!['PICKUP', 'ROBOT'].includes(method)) throw new ValidationError('INVALID_DELIVERY_METHOD', message);
  return method;
}

// --- 계정·세션 -----------------------------------------------------------
export function registerUser() {
  throw new ValidationError('REGISTRATION_DISABLED',
    '세대 계정은 미리 발급됩니다. 아이디(동-호)와 관리사무소에서 받은 비밀번호로 로그인해 주세요.', 410);
}

export async function loginUser(locker, input) {
  const userId = String(input?.userId ?? '').trim();
  const password = String(input?.password ?? '');
  if (!/^\d{3}-\d{3,4}$/.test(userId)) {
    throw new ValidationError('INVALID_USER_ID', "아이디는 '동-호수' 형식으로 입력해 주세요. (예: 101-412)");
  }
  if (!password) throw new ValidationError('INVALID_PASSWORD', '비밀번호를 입력해 주세요.');
  const result = await send(locker, TOPICS.login, { user_id: userId, password, channel: 'RESIDENT' });
  if (result.status !== 'OK') {
    throw new ValidationError('INVALID_CREDENTIALS', result.message || '아이디 또는 비밀번호가 올바르지 않습니다.', 401);
  }
  return { user: publicUser(userId, result.household), lockerSession: result.session };
}

export function createSessionStore() {
  return new Map();
}

const tokenHash = token => crypto.createHash('sha256').update(token).digest('hex');

export function createSession(sessions, { user, lockerSession }) {
  const token = crypto.randomBytes(32).toString('hex');
  const createdAt = new Date();
  const expiresAt = new Date(createdAt.getTime() + SESSION_DAYS * 24 * 60 * 60 * 1000);
  for (const [key, session] of sessions) if (session.expiresAt <= createdAt) sessions.delete(key);
  sessions.set(tokenHash(token), { user, lockerSession, expiresAt, expired: false });
  return { token, expiresAt };
}

export function getSession(sessions, token) {
  if (!token || !/^[0-9a-f]{64}$/i.test(token)) return null;
  const key = tokenHash(token);
  const session = sessions.get(key);
  if (!session || session.expired || session.expiresAt <= new Date()) {
    sessions.delete(key);
    return null;
  }
  return session;
}

export async function deleteSession(sessions, locker, token) {
  const session = getSession(sessions, token);
  if (!session) return;
  sessions.delete(tokenHash(token));
  await send(locker, TOPICS.logout, { session: session.lockerSession }).catch(() => {});
}

// --- 수령 방식 -----------------------------------------------------------
export async function getDeliveryPreference(locker, session) {
  const result = await call(locker, session, TOPICS.parcels);
  session.user.defaultDelivery = fromMode(result.household.default_mode);
  return { defaultDelivery: session.user.defaultDelivery, updatedAt: null };
}

// 세대 기본값(토글). 바꾸면 보관소 서버가 아직 로봇에 실리지 않은 택배를 모두 함께 바꾼다.
export async function updateDeliveryPreference(locker, session, input) {
  const method = deliveryMethod(input?.defaultDelivery, '기본 수령 방법을 선택해 주세요.');
  const result = await call(locker, session, TOPICS.defaultMode, { mode: toMode(method) });
  session.user.defaultDelivery = method;
  return { defaultDelivery: method, updatedAt: new Date().toISOString(), changed: result.changed ?? 0 };
}

export async function chooseDelivery(locker, session, parcelId, input) {
  const method = deliveryMethod(input?.method, '수령 방법을 선택해 주세요.');
  await call(locker, session, TOPICS.itemMode, { receipt_no: String(parcelId ?? '').trim(), mode: toMode(method) });
  const parcels = await listUserParcels(locker, session);
  return parcels.find(parcel => parcel.id === parcelId) || { id: parcelId, deliveryMethod: method };
}

// --- 택배 ---------------------------------------------------------------
export async function listUserParcels(locker, session) {
  const result = await call(locker, session, TOPICS.parcels);
  session.user.defaultDelivery = fromMode(result.household.default_mode);
  return result.parcels.map(userParcel).filter(Boolean);
}

export async function getUserPickupCode(locker, session, parcelId) {
  const result = await call(locker, session, TOPICS.pickupCode, { receipt_no: String(parcelId ?? '').trim() });
  return { code: result.code };
}

export async function hideCompletedParcel(locker, session, parcelId) {
  await call(locker, session, TOPICS.hide, { receipt_no: String(parcelId ?? '').trim() });
  return { ok: true };
}

// --- 로봇 (FMS 가 보내는 상태) ------------------------------------------
function robotSnapshot(raw) {
  const fresh = Date.now() - raw.receivedAt < ROBOT_STALE_MS;
  const connected = fresh && raw.status !== '통신 끊김';
  const battery = Number(raw.battery);
  return {
    id: raw.robot_id,
    connectionMode: 'MQTT',
    mqttTopic: `locker/robot/${raw.robot_id}/state`,
    connected,
    available: connected && raw.available !== false,
    userVisible: true,
    status: connected ? String(raw.status ?? '대기 중') : '통신 끊김',
    location: String(raw.location ?? '-'),
    battery: Number.isFinite(battery) ? battery : '-',
    lastSeen: new Date(raw.receivedAt).toISOString()
  };
}

export function listUserRobots(locker) {
  return locker.robots().map(robotSnapshot).sort((a, b) => a.id.localeCompare(b.id));
}

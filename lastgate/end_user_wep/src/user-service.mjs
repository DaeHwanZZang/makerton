// 세대 웹 서버(BFF) ↔ 보관소 서버 요청 모음.
// 세대 웹은 계정·택배를 저장하지 않는다. 쿠키에는 보관소 서버가 발급한 세션 토큰(7일)이 그대로 들어가고,
// 매 요청마다 보관소 서버가 세션을 확인한다. 그래서 세대 웹 서버를 재시작해도 로그인이 유지된다.
import {
  ValidationError, fromMode, toMode, userParcel, publicUser, userSlot, userNotification
} from './parcel-service.mjs';
import { TOPICS, LockerUnavailableError } from './locker-client.mjs';

const ROBOT_STALE_MS = 30000; // FMS 하트비트(10초)가 3번 끊기면 통신 끊김으로 표시
const TOKEN_RE = /^tok_[A-Za-z0-9_-]{16,64}$/;

// 보관소 서버 응답 상태 → [HTTP 상태, 오류 코드]
const LOCKER_ERRORS = {
  DENIED: [409, 'DELIVERY_CHOICE_LOCKED'],
  NOT_CHANGEABLE: [409, 'DELIVERY_CHOICE_LOCKED'],
  LOCKED_DIRECT: [409, 'DIRECT_ONLY'],
  FIXED: [409, 'DELIVERY_FIXED'],
  TOO_LATE: [409, 'TOO_LATE'],
  SLOT_FULL: [409, 'SLOT_FULL'],
  SLOT_UNAVAILABLE: [409, 'SLOT_UNAVAILABLE'],
  NOT_RESERVED: [409, 'NOT_RESERVED'],
  NOT_ROBOT: [409, 'NOT_ROBOT'],
  NOT_AVAILABLE: [409, 'PICKUP_CODE_UNAVAILABLE'],
  NOT_COMPLETED: [409, 'PARCEL_NOT_DELIVERED'],
  NOT_FOUND: [404, 'NOT_FOUND'],
  INVALID: [400, 'INVALID_INPUT'],
  NOT_INITIALIZED: [503, 'NOT_INITIALIZED']
};

async function send(locker, topic, body) {
  try {
    return await locker.request(topic, body);
  } catch (error) {
    if (error instanceof LockerUnavailableError) throw new ValidationError('LOCKER_UNAVAILABLE', error.message, 503);
    throw error;
  }
}

function lockerError(result, overrides = {}) {
  const [status, code] = overrides[result.status] || LOCKER_ERRORS[result.status] || [502, 'LOCKER_ERROR'];
  const extra = {};
  if (result.seats_left !== undefined) extra.seatsLeft = result.seats_left;
  if (result.remaining !== undefined) extra.remaining = result.remaining;
  if (result.retry_after_sec !== undefined) extra.retryAfterSec = result.retry_after_sec;
  return new ValidationError(code, result.message || '보관소 서버가 요청을 처리하지 못했습니다.', status, extra);
}

// 로그인한 세대의 세션으로 요청하고, 실패 상태를 HTTP 오류로 바꾼다.
async function call(locker, session, topic, body = {}, overrides) {
  const result = await send(locker, topic, { ...body, session: session.token });
  if (result.status === 'OK') return result;
  if (result.status === 'EXPIRED_SESSION') {
    throw new ValidationError('AUTH_REQUIRED', '로그인이 만료되었습니다. 다시 로그인해 주세요.', 401);
  }
  throw lockerError(result, overrides);
}

const receipt = value => String(value ?? '').trim();

function deliveryMethod(value, message) {
  const method = String(value ?? '').trim().toUpperCase();
  if (!['PICKUP', 'ROBOT'].includes(method)) throw new ValidationError('INVALID_DELIVERY_METHOD', message);
  return method;
}

// --- 계정·세션 -----------------------------------------------------------
/** 쿠키 값 → 세션. 형식만 확인하고, 유효 여부는 보관소 서버가 판단한다. */
export function sessionFromToken(token) {
  return token && TOKEN_RE.test(token) ? { token } : null;
}

/** 입력: { building, unit, password } (또는 userId "건물코드-호실") */
export async function loginUser(locker, input) {
  let building = String(input?.building ?? '').trim();
  let unit = String(input?.unit ?? '').trim();
  const userId = String(input?.userId ?? '').trim();
  if (userId && (!building || !unit)) [building, unit] = userId.split('-');
  const password = String(input?.password ?? '');
  if (!/^\d{1,3}$/.test(building || '')) throw new ValidationError('INVALID_BUILDING', '건물을 선택해 주세요.');
  if (!/^\d{1,5}$/.test(unit || '')) throw new ValidationError('INVALID_UNIT', '호실을 숫자로 입력해 주세요.');
  if (!password) throw new ValidationError('INVALID_PASSWORD', '비밀번호를 입력해 주세요.');
  const result = await send(locker, TOPICS.login, {
    building: Number(building), unit: Number(unit), password, channel: 'RESIDENT'
  });
  if (result.status !== 'OK') {
    throw lockerError(result, { DENIED: [401, 'INVALID_CREDENTIALS'], LOCKED: [423, 'ACCOUNT_LOCKED'] });
  }
  return {
    user: publicUser({ ...result.household, must_change: result.must_change }),
    token: result.session,
    ttlSec: result.ttl_sec
  };
}

export async function logoutUser(locker, session) {
  if (!session) return;
  await send(locker, TOPICS.logout, { session: session.token }).catch(() => {});
}

export async function changePassword(locker, session, input) {
  const currentPassword = String(input?.currentPassword ?? '');
  const newPassword = String(input?.newPassword ?? '');
  if (!/^\d{4,8}$/.test(newPassword)) throw new ValidationError('INVALID_PASSWORD', '새 비밀번호는 숫자 4~8자리입니다.');
  const result = await call(locker, session, TOPICS.password,
    { current_password: currentPassword, new_password: newPassword },
    { DENIED: [400, 'WRONG_PASSWORD'], INVALID: [400, 'INVALID_PASSWORD'] });
  return { user: publicUser(result.household), revoked: result.revoked ?? 0 };
}

// --- 내 정보·택배 --------------------------------------------------------
/** 세대 정보 + 택배 목록 + 읽지 않은 알림 수. 세션 확인도 겸한다. */
export async function getOverview(locker, session) {
  const result = await call(locker, session, TOPICS.parcels);
  return {
    serverTime: result.server_time,
    user: publicUser(result.household),
    household: { building: result.household.building, unit: result.household.unit },
    unread: result.unread ?? 0,
    parcels: result.parcels.map(userParcel).filter(Boolean)
  };
}

export async function getMe(locker, session) {
  const { user, unread } = await getOverview(locker, session);
  return { user, unread };
}

export async function listUserParcels(locker, session) {
  const { serverTime, user, unread, parcels } = await getOverview(locker, session);
  return { serverTime, user, unread, parcels };
}

export async function getUserPickupCode(locker, session, parcelId) {
  const result = await call(locker, session, TOPICS.pickupCode, { receipt_no: receipt(parcelId) });
  return { code: result.code };
}

export async function hideCompletedParcel(locker, session, parcelId) {
  await call(locker, session, TOPICS.hide, { receipt_no: receipt(parcelId) });
  return { ok: true };
}

// --- 수령 방식 -----------------------------------------------------------
export async function getDeliveryPreference(locker, session) {
  const { user } = await getOverview(locker, session);
  return { defaultDelivery: user.defaultDelivery };
}

// 세대 기본값(토글). 바꾸면 보관소 서버가 아직 확정되지 않은 택배를 모두 함께 바꾼다.
// 직접 수령 전용·배송 확정 물품은 바뀌지 않고 skipped 로 센다.
export async function updateDeliveryPreference(locker, session, input) {
  const method = deliveryMethod(input?.defaultDelivery, '기본 수령 방법을 선택해 주세요.');
  const result = await call(locker, session, TOPICS.defaultMode, { mode: toMode(method) });
  return {
    defaultDelivery: fromMode(result.default_mode),
    changed: result.changed ?? 0,
    skipped: result.skipped ?? 0,
    parcels: (result.parcels || []).map(userParcel).filter(Boolean)
  };
}

export async function chooseDelivery(locker, session, parcelId, input) {
  const method = deliveryMethod(input?.method, '수령 방법을 선택해 주세요.');
  const result = await call(locker, session, TOPICS.itemMode, { receipt_no: receipt(parcelId), mode: toMode(method) });
  return userParcel(result.parcel) || { id: parcelId, deliveryMethod: method };
}

// --- 배송 시간 예약 ------------------------------------------------------
export async function listSlots(locker, session) {
  const result = await call(locker, session, TOPICS.slots);
  return {
    serverTime: result.server_time,
    serviceStart: result.service_start,
    serviceEnd: result.service_end,
    seatsPerSlot: result.seats_per_slot,
    slots: result.slots.map(userSlot)
  };
}

/** 입력: { slotStart, parcelIds: [1~2개] }. 2개는 모두 잡히거나 모두 실패한다. */
export async function reserveSlot(locker, session, input) {
  const slotStart = String(input?.slotStart ?? '').trim();
  const ids = Array.isArray(input?.parcelIds) ? [...new Set(input.parcelIds.map(receipt).filter(Boolean))] : [];
  if (!slotStart) throw new ValidationError('INVALID_SLOT', '배송 시간을 골라 주세요.');
  if (ids.length < 1 || ids.length > 2) throw new ValidationError('INVALID_PARCELS', '예약할 택배를 1~2개 골라 주세요.');
  const result = await call(locker, session, TOPICS.reserve, { slot_start: slotStart, receipt_nos: ids });
  return {
    slot: { start: result.slot_start, end: result.slot_end, label: result.label },
    parcelIds: result.receipt_nos,
    parcels: result.parcels.map(userParcel).filter(Boolean)
  };
}

export async function cancelReservation(locker, session, parcelId) {
  const result = await call(locker, session, TOPICS.reserveCancel, { receipt_no: receipt(parcelId) });
  return { plan: result.plan, parcels: result.parcels.map(userParcel).filter(Boolean) };
}

// --- 알림 ---------------------------------------------------------------
export async function listNotifications(locker, session, read) {
  const body = read === undefined ? {} : { read };
  const result = await call(locker, session, TOPICS.notifications, body);
  return { unread: result.unread ?? 0, notifications: result.notifications.map(userNotification) };
}

export function readNotifications(locker, session, input) {
  if (input?.all === true) return listNotifications(locker, session, 'all');
  const ids = Array.isArray(input?.ids) ? input.ids.map(Number).filter(Number.isInteger) : [];
  if (!ids.length) throw new ValidationError('INVALID_INPUT', '읽음 처리할 알림을 골라 주세요.');
  return listNotifications(locker, session, ids);
}

// --- 로봇 (FMS 가 보내는 상태) ------------------------------------------
function robotSnapshot(raw, parcelIds) {
  const fresh = Date.now() - raw.receivedAt < ROBOT_STALE_MS;
  const connected = fresh && raw.status !== '통신 끊김';
  const battery = Number(raw.battery);
  return {
    id: raw.robot_id,
    connected,
    status: connected ? String(raw.status ?? '배송 중') : '통신 끊김',
    location: String(raw.location ?? '-'),
    battery: Number.isFinite(battery) ? battery : null,
    parcelIds,
    lastSeen: new Date(raw.receivedAt).toISOString()
  };
}

/** 내 택배를 싣고 있는 로봇만 보여준다. 다른 세대 배송 정보는 노출하지 않는다. */
export async function listUserRobots(locker, session) {
  const { parcels } = await getOverview(locker, session);
  const carrying = new Map();
  for (const parcel of parcels) {
    if (parcel.status === 'IN_TRANSIT' && parcel.robotId) {
      carrying.set(parcel.robotId, [...(carrying.get(parcel.robotId) || []), parcel.id]);
    }
  }
  const states = new Map(locker.robots().map(raw => [raw.robot_id, raw]));
  return [...carrying].map(([id, parcelIds]) => (states.has(id)
    ? robotSnapshot(states.get(id), parcelIds)
    : { id, connected: false, status: '상태 수신 대기', location: '-', battery: null, parcelIds, lastSeen: null }))
    .sort((a, b) => a.id.localeCompare(b.id));
}

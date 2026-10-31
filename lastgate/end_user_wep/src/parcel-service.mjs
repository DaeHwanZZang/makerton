// 세대 웹에서 쓰는 상수와 보관소 서버 데이터 → 화면 데이터 변환.
// 택배 접수·보관·예약·상태 변경은 보관소 서버(server/)가 담당한다.
// 건물·호실 목록은 하드코딩하지 않고 보관소 서버의 현장 설정(locker/config/site)을 그대로 쓴다.

export const STATUSES = {
  STORED: '보관 중',
  IN_TRANSIT: '로봇 배송 중',
  DELIVERED: '배송 완료',
  PICKED_UP: '수령 완료'
};

export class ValidationError extends Error {
  constructor(code, message, status = 400, extra = {}) {
    super(message);
    this.name = 'ValidationError';
    this.code = code;
    this.status = status;
    this.extra = extra;
  }
}

// 보관소 서버 상태 → 세대 웹 상태. REGISTERED(투입 전)·CANCELLED 는 표시하지 않는다.
const WEB_STATUS = {
  STORED: 'STORED',
  JOB_ASSIGNED: 'STORED',     // 배송이 발행됐지만 아직 로봇에 실리기 전
  LOADED: 'IN_TRANSIT',
  DELIVERED: 'DELIVERED',
  PICKED: 'PICKED_UP'
};

const LOCK_LABELS = {
  FRAGILE: '취급 주의 · 직접 수령만 가능',
  OVERSIZE: '규격 초과 · 직접 수령만 가능',
  FAILED: '배달 2회 실패 · 직접 수령만 가능'
};

// 보관소 서버는 DIRECT, 세대 웹 화면·API 는 PICKUP 이라는 이름을 쓴다. 바꾸는 곳은 여기뿐이다.
export const fromMode = mode => (mode === 'DIRECT' ? 'PICKUP' : 'ROBOT');
export const toMode = method => (method === 'PICKUP' ? 'DIRECT' : 'ROBOT');

function statusLabel(item, status) {
  if (status !== 'STORED') return STATUSES[status];
  if (item.mode === 'DIRECT') return '직접 수령 대기';
  if (item.status === 'JOB_ASSIGNED') return '로봇 배송 준비 중';
  if (item.plan === 'RESERVED') return '배송 예약됨';
  if (item.plan === 'AUTO') return '자동 배송 확정';
  if (item.plan === 'CHOICE_OPEN') return '배송 시간 선택 가능';
  return '보관 중';
}

export function userParcel(item) {
  const status = WEB_STATUS[item.status];
  if (!status) return null;
  return {
    id: item.receipt_no,
    trackingNumber: item.tracking_no || null,
    status,
    statusLabel: statusLabel(item, status),
    deliveryMethod: fromMode(item.mode),
    plan: item.plan || null,                      // CHOICE_OPEN | RESERVED | AUTO | null(직접 수령)
    storageArea: item.storage_area,               // GRID | OVERSIZE
    door: item.door ?? null,
    fragile: Boolean(item.fragile),
    directLock: item.direct_lock || null,
    lockLabel: item.direct_lock ? LOCK_LABELS[item.direct_lock] || '직접 수령만 가능' : null,
    choiceDeadline: item.choice_deadline || null,
    slot: item.slot_start ? {
      start: item.slot_start, end: item.slot_end, label: item.slot_label, changeUntil: item.change_until
    } : null,
    robotId: item.robot_id || null,
    dispatched: item.status === 'JOB_ASSIGNED',
    fixed: Boolean(item.fixed),
    canChooseDelivery: Boolean(item.changeable),
    reservable: Boolean(item.reservable),
    createdAt: item.stored_at || item.registered_at,
    updatedAt: item.updated_at
  };
}

export function publicUser(household) {
  return {
    userId: household.user_id || `${household.building}-${household.unit}`,
    name: household.label || `${household.building}-${household.unit}`,
    building: String(household.building),
    buildingName: household.building_name || String(household.building),
    unit: String(household.unit),
    defaultDelivery: fromMode(household.default_mode),
    mustChange: Boolean(household.must_change)
  };
}

export function userSlot(slot) {
  return {
    start: slot.start,
    end: slot.end,
    label: slot.label,
    seatsLeft: slot.seats_left,
    available: slot.available,
    restricted: Boolean(slot.restricted),
    mine: slot.mine,
    mineParcelIds: slot.mine_receipts || []
  };
}

export function userNotification(row) {
  return {
    id: row.id,
    kind: row.kind,
    title: row.title,
    body: row.body,
    parcelId: row.receipt_no || null,
    createdAt: row.created_at,
    read: Boolean(row.read_at)
  };
}

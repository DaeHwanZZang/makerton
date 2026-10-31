// 세대 웹에서 쓰는 상수와 보관소 서버 데이터 → 화면 데이터 변환.
// 택배 접수·보관·상태 변경은 보관소 서버(server/)가 담당한다.

export const BUILDINGS = ['101', '102', '103', '104', '105', '106'];
export const SIZES = [
  { code: 'S', label: '소형', detail: '문서·소형 상자' },
  { code: 'M', label: '중형', detail: '신발 상자·일반 택배' },
  { code: 'L', label: '대형', detail: '대형 상자·부피 큰 물품' }
];
export const STATUSES = {
  STORED: '보관 완료',
  IN_TRANSIT: '로봇 배송 중',
  DELIVERED: '배송 완료',
  PICKED_UP: '수령 완료'
};

export class ValidationError extends Error {
  constructor(code, message, status = 400) {
    super(message);
    this.name = 'ValidationError';
    this.code = code;
    this.status = status;
  }
}

// 보관소 서버 상태 → 세대 웹 상태. REGISTERED(투입 전)·CANCELLED 는 표시하지 않는다.
const WEB_STATUS = {
  STORED: 'STORED',
  JOB_ASSIGNED: 'STORED',     // 로봇 Job 후보지만 아직 실리기 전이라 변경 가능
  LOADED: 'IN_TRANSIT',
  DELIVERED: 'DELIVERED',
  PICKED: 'PICKED_UP'
};

// 보관소 서버는 DIRECT, 세대 웹 화면·API 는 PICKUP 이라는 이름을 쓴다.
export const fromMode = mode => (mode === 'DIRECT' ? 'PICKUP' : 'ROBOT');
export const toMode = method => (method === 'PICKUP' ? 'DIRECT' : 'ROBOT');

export function userParcel(item) {
  const status = WEB_STATUS[item.status];
  if (!status) return null;
  const deliveryMethod = fromMode(item.mode);
  return {
    id: item.receipt_no,
    trackingNumber: item.tracking_no || null,
    size: item.size,
    status,
    statusLabel: status === 'STORED' && deliveryMethod === 'ROBOT' ? '로봇 운송 준비 중' : STATUSES[status],
    deliveryMethod,
    robotId: null,
    canChooseDelivery: Boolean(item.changeable),
    createdAt: item.stored_at || item.registered_at,
    updatedAt: item.updated_at
  };
}

// 서버의 server/config.py 와 같은 값을 유지한다.

export const BROKER_URL: string =
  import.meta.env.VITE_BROKER_URL ?? `ws://${window.location.hostname || "localhost"}:9001`;

export const TOPICS = {
  register: "locker/input/register",
  portConfirm: "locker/input/port_confirm",
  inputStats: "locker/input/stats",
  login: "locker/auth/login",
  logout: "locker/auth/logout",
  parcels: "locker/output/parcels",
  retrieve: "locker/output/retrieve",
  summary: "locker/state/summary",
  clientRes: (clientId: string) => `locker/client/${clientId}/res`,
  clientOnline: (clientId: string) => `locker/client/${clientId}/online`,
};

export const DONGS = [101, 102, 103, 104, 105, 106];
export const FLOORS = 4;
export const UNITS_PER_FLOOR = 12;

export type SizeKey = "S" | "M" | "L";

export const SIZES: { key: SizeKey; label: string; desc: string; limit: string }[] = [
  { key: "S", label: "소형", desc: "문서, 작은 소포", limit: "5kg 이하" },
  { key: "M", label: "중형", desc: "일반 택배 상자", limit: "10kg 이하" },
  { key: "L", label: "대형", desc: "부피가 큰 상자", limit: "20kg 이하" },
];

export const sizeLabel = (key: string) => SIZES.find((s) => s.key === key)?.label ?? key;

export const TRACKING_RE = /^[A-Za-z0-9]{6,40}$/;

export const IDLE_LOGOUT_SEC = 60;

/** 호수 검증. 통과하면 null, 아니면 안내 문구 */
export function validateHo(value: string): string | null {
  if (!/^\d{3,4}$/.test(value)) return "호수는 숫자 3~4자리로 입력하세요.";
  const ho = Number(value);
  const floor = Math.floor(ho / 100);
  const unit = ho % 100;
  if (floor < 1 || floor > FLOORS || unit < 1 || unit > UNITS_PER_FLOOR) {
    return `101호 ~ ${FLOORS}${String(UNITS_PER_FLOOR).padStart(2, "0")}호 범위로 입력하세요 (층 1~${FLOORS}, 호 01~${UNITS_PER_FLOOR}).`;
  }
  return null;
}

/** URL 의 ?id= 로 키오스크 번호를 바꿀 수 있다 (예: /input?id=INPUT-02) */
export function kioskId(fallback: string): string {
  return new URLSearchParams(window.location.search).get("id") ?? fallback;
}

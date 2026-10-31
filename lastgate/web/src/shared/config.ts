// 토픽·형식은 서버의 server/config.py 와 같은 값을 유지한다.
// 건물·호실 같은 현장 설정은 여기 두지 않고 서버가 발행하는 locker/config/site(retained)를 쓴다.

export const BROKER_URL: string =
  import.meta.env.VITE_BROKER_URL ?? `ws://${window.location.hostname || "localhost"}:9001`;

/** 시작 화면의 관리자 웹 링크. 관리자 웹은 보관소 PC(127.0.0.1)에서만 열린다. */
export const ADMIN_URL: string = import.meta.env.VITE_ADMIN_URL ?? "http://localhost:4191";

export const TOPICS = {
  register: "locker/input/register",
  portConfirm: "locker/input/port_confirm",
  inputStats: "locker/input/stats",
  login: "locker/auth/login",
  logout: "locker/auth/logout",
  parcels: "locker/output/parcels",
  retrieve: "locker/output/retrieve",
  summary: "locker/state/summary",
  site: "locker/config/site",
  serverOnline: "locker/server/online",
  clientRes: (clientId: string) => `locker/client/${clientId}/res`,
  clientOnline: (clientId: string) => `locker/client/${clientId}/online`,
};

export type Building = {
  code: number;
  name: string;
  unit_from: number;
  unit_to: number;
  missing: string;
  extra: string;
  guide: string;        // 서버가 만든 입력 전 안내 (예: "101~614호 · 없는 호실: 404호, 5층 전체")
  units: number[];
};

export type Site = {
  name: string;
  building_term: string;
  unit_term: string;
  buildings: Building[];
  grid_limit_mm: number[];
  grid_bins: number;
  oversize_doors: number;
  robots: number;
  service_start: string;
  service_end: string;
  version: number;
};

export type SiteMessage = { type: "SITE_CONFIG"; initialized: boolean; version: number; site: Site | null };

export const TRACKING_RE = /^[A-Za-z0-9]{6,40}$/;

export const IDLE_LOGOUT_SEC = 60;

/** 600×400×220mm 같은 규격 문구 */
export const limitText = (site: Site | null) => (site?.grid_limit_mm ?? [600, 400, 220]).join("×") + "mm";

/** prefix 뒤에 digit 을 눌렀을 때 이어질 수 있는 호실이 있는지 (없는 호실로만 가는 키는 끈다) */
export function keyLeadsToUnit(units: number[], prefix: string, digit: string): boolean {
  const next = prefix + digit;
  if (next === "0") return false;
  return units.some((u) => String(u).startsWith(next));
}

/** 입력값 검사. ok=완성된 호실, partial=더 입력해야 함, 그 외 오류 문구 */
export function checkUnit(building: Building | null, unitTerm: string, value: string):
  { state: "empty" | "ok" | "partial" | "error"; message?: string } {
  if (!value) return { state: "empty" };
  if (!building) return { state: "error", message: "먼저 건물을 선택하세요." };
  if (building.units.includes(Number(value))) return { state: "ok" };
  if (building.units.some((u) => String(u).startsWith(value))) {
    return { state: "partial", message: `${unitTerm} 번호를 끝까지 입력하세요.` };
  }
  return { state: "error", message: `${building.name}에 ${value}${unitTerm}는 없습니다.` };
}

export const unitLabel = (site: Site | null, building: number | null | undefined, unit: number | string) => {
  const b = site?.buildings.find((x) => x.code === building);
  return `${b?.name ?? building ?? ""} ${unit}${site?.unit_term ?? ""}`.trim();
};

/** URL 의 ?id= 로 키오스크 번호를 바꿀 수 있다 (예: /input?id=INPUT-02) */
export function kioskId(fallback: string): string {
  return new URLSearchParams(window.location.search).get("id") ?? fallback;
}

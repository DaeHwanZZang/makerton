import { useCallback, useEffect, useState } from "react";
import { TOPICS } from "./config";
import { LockerClient, type Msg } from "./mqtt";

/** 화면 수명 동안 MQTT 클라이언트 하나를 유지한다. */
export function useLocker(kioskId: string) {
  const [client, setClient] = useState<LockerClient | null>(null);
  const [connected, setConnected] = useState(false);
  useEffect(() => {
    const c = new LockerClient(kioskId);
    setClient(c);
    const off = c.onStatus(setConnected);
    return () => {
      off();
      c.end();
    };
  }, [kioskId]);
  return { client, connected };
}

/** 카운트다운 표시용 현재 시각 (ms) */
export function useNow(intervalMs = 250) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(t);
  }, [intervalMs]);
  return now;
}

const DAY_MS = 24 * 60 * 60 * 1000;
const KST_OFFSET_MS = 9 * 60 * 60 * 1000;

/** 다음 KST 자정까지 남은 시간. 서버 날짜(KST)와 맞추려고 기기 시간대와 관계없이 계산한다. */
export function msUntilKstMidnight(now = Date.now()) {
  return DAY_MS - ((now + KST_OFFSET_MS) % DAY_MS);
}

/**
 * 이 입력 키오스크의 오늘 등록 건수 (서버 DB 기준).
 * 연결될 때와 KST 자정마다 서버에 다시 물어보므로, 화면을 켜 둔 채 날짜가 바뀌어도 0부터 다시 센다.
 * 반환하는 apply 에 서버 응답(today_count 포함)을 넘기면 즉시 반영한다.
 */
export function useTodayCount(client: LockerClient | null, connected: boolean, kioskId: string) {
  const [count, setCount] = useState<number | null>(null);
  const apply = useCallback((res: Msg) => {
    if (typeof res.today_count === "number") setCount(res.today_count);
  }, []);
  useEffect(() => {
    if (!client || !connected) return;
    let timer = 0;
    const refresh = () =>
      client.request(TOPICS.inputStats, { kiosk_id: kioskId }).then(apply).catch(() => undefined);
    const schedule = () => {
      // 자정 직후 서버 날짜가 확실히 바뀐 뒤에 묻도록 1초 여유
      timer = window.setTimeout(() => {
        refresh();
        schedule();
      }, msUntilKstMidnight() + 1000);
    };
    refresh();
    schedule();
    return () => window.clearTimeout(timer);
  }, [client, connected, kioskId, apply]);
  return [count, apply] as const;
}

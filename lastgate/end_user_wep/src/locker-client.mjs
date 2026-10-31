import os from 'node:os';
import crypto from 'node:crypto';
import mqtt from 'mqtt';
import { COMMON_TOPICS, LockerUnavailableError, createLockerClient as createClient } from '../../shared/locker-client.mjs';

export { LockerUnavailableError };

// 보관소 서버(server/)와 주고받는 MQTT 토픽. 규격은 lastgate/protocol.md 참고.
export const TOPICS = {
  ...COMMON_TOPICS,
  login: 'locker/auth/login',
  logout: 'locker/auth/logout',
  password: 'locker/auth/password',
  parcels: 'locker/resident/parcels',
  defaultMode: 'locker/resident/default_mode',
  itemMode: 'locker/resident/item_mode',
  pickupCode: 'locker/resident/pickup_code',
  hide: 'locker/resident/hide',
  slots: 'locker/resident/slots',
  reserve: 'locker/resident/reserve',
  reserveCancel: 'locker/resident/reserve_cancel',
  notifications: 'locker/resident/notifications'
};

// 보관소 서버는 로그인할 때의 client_id 로 세대 알림을 보낸다. 재시작해도 알림을 계속 받도록
// PC 이름·포트로 고정 접미어를 만든다. 세대 웹을 여러 대 띄우면 RESIDENT_WEB_ID 로 서로 다르게 지정한다.
function stableSuffix() {
  const seed = process.env.RESIDENT_WEB_ID || `${os.hostname()}:${process.env.PORT || 4180}`;
  return crypto.createHash('sha256').update(seed).digest('hex').slice(0, 8).toUpperCase();
}

/** 세대 웹 서버용 클라이언트. 로봇 상태·예약 칸 변동도 구독한다. */
export function createLockerClient({ url, connect = mqtt.connect, ...options } = {}) {
  return createClient({
    url, connect, clientPrefix: 'RESIDENT-WEB-', clientSuffix: stableSuffix(),
    subscribe: [TOPICS.robotState, TOPICS.slotsUpdate],
    ...options
  });
}

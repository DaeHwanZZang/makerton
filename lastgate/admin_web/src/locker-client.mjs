import mqtt from 'mqtt';
import { COMMON_TOPICS, LockerUnavailableError, createLockerClient as createClient } from '../../shared/locker-client.mjs';

export { LockerUnavailableError };

// 관리자 토픽. 보관소 서버는 client_id 가 ADMIN-WEB- 으로 시작하는 요청만 받는다. 규격은 lastgate/protocol.md 참고.
export const TOPICS = {
  ...COMMON_TOPICS,
  login: 'locker/admin/login',
  logout: 'locker/admin/logout',
  password: 'locker/admin/password',
  site: 'locker/admin/site/get',
  sitePreview: 'locker/admin/site/preview',
  siteInit: 'locker/admin/site/init',
  siteUpdate: 'locker/admin/site/update',
  siteReset: 'locker/admin/site/reset',
  accounts: 'locker/admin/accounts/list',
  resetPassword: 'locker/admin/accounts/reset_password',
  changeOccupant: 'locker/admin/accounts/change_occupant',
  unlock: 'locker/admin/accounts/unlock',
  storage: 'locker/admin/storage',
  codePickup: 'locker/admin/code_pickup',
  events: 'locker/admin/events',
  demoSkip: 'locker/admin/demo/skip_wait'
};

export function createLockerClient({ url, connect = mqtt.connect, ...options } = {}) {
  // 초기 설정은 계정 수천 개를 만들 수 있어 응답 대기를 넉넉히 둔다.
  return createClient({ url, connect, clientPrefix: 'ADMIN-WEB-', timeoutMs: 20000, ...options });
}

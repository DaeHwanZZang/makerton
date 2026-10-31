// 보관소 서버(server/)와 MQTT 로 요청·응답하는 클라이언트. 세대 웹·관리자 웹 공용.
// - 요청마다 request_id 를 붙이고, 응답은 이 클라이언트 전용 토픽(locker/client/{id}/res)에서만 받는다.
// - 응답이 아닌 전용 토픽 메시지(NOTIFICATION, PARCELS_UPDATE)와 공용 토픽(현장 설정, 예약 칸 변동,
//   로봇 상태, 서버 online)은 onEvent(kind, message) 로 넘긴다.
// mqtt 패키지는 각 앱의 node_modules 에 있으므로 connect 함수를 주입받는다.
import crypto from 'node:crypto';

export class LockerUnavailableError extends Error {}

export const COMMON_TOPICS = {
  site: 'locker/config/site',
  slotsUpdate: 'locker/resident/slots_update',
  serverOnline: 'locker/server/online',
  robotState: 'locker/robot/+/state',
  clientRes: clientId => `locker/client/${clientId}/res`
};

/**
 * @param {object} options
 * @param {string} options.url            브로커 주소 (mqtt://host:1883)
 * @param {Function} options.connect      mqtt.connect
 * @param {string} options.clientPrefix   RESIDENT-WEB- / ADMIN-WEB- (서버가 접두어로 권한을 구분)
 * @param {string[]} [options.subscribe]  추가로 구독할 공용 토픽
 * @param {string} [options.clientSuffix] 고정 접미어. 재시작해도 같은 client_id 를 써야
 *                                        서버가 기존 세션으로 보내는 알림을 계속 받는다.
 */
export function createLockerClient({
  url, connect, clientPrefix, clientSuffix, subscribe = [], timeoutMs = 5000,
  username = process.env.LOCKER_BROKER_USER, password = process.env.LOCKER_BROKER_PASS,
  onStatus = () => {}, onEvent = () => {}
}) {
  if (typeof connect !== 'function') throw new Error('mqtt connect 함수가 필요합니다.');
  const suffix = clientSuffix || crypto.randomBytes(3).toString('hex').toUpperCase();
  const clientId = `${clientPrefix}${suffix}`;
  const resTopic = COMMON_TOPICS.clientRes(clientId);
  const pending = new Map();
  const robots = new Map();
  const listeners = new Set();
  const emit = (kind, message) => {
    onEvent(kind, message);
    for (const listener of listeners) {
      try { listener(kind, message); } catch (error) { console.error('[MQTT] 이벤트 처리 오류:', error); }
    }
  };
  let connected = false;
  let serverOnline = null;     // null = 아직 모름
  let site = null;             // 마지막 SITE_CONFIG

  const client = connect(url, {
    clientId, clean: true, reconnectPeriod: 2000, connectTimeout: 5000,
    ...(username ? { username, password } : {})
  });
  const setConnected = value => {
    if (connected === value) return;
    connected = value;
    onStatus(value);
  };
  client.on('connect', () => {
    client.subscribe([resTopic, COMMON_TOPICS.site, COMMON_TOPICS.serverOnline, ...subscribe], { qos: 1 });
    setConnected(true);
  });
  client.on('close', () => setConnected(false));
  client.on('offline', () => setConnected(false));
  client.on('error', () => {}); // 브로커가 없으면 계속 재연결
  client.on('message', (topic, payload) => {
    const text = payload.toString();
    if (topic === COMMON_TOPICS.serverOnline) {   // 일반 문자열 "online"/"offline"
      serverOnline = text.trim() === 'online';
      emit('server', { online: serverOnline });
      return;
    }
    let message;
    try { message = JSON.parse(text); } catch { return; }
    if (!message || typeof message !== 'object') return;
    if (topic === resTopic) {
      const waiter = pending.get(message.request_id);
      if (waiter) waiter(message);
      else emit('push', message);
      return;
    }
    if (topic === COMMON_TOPICS.site) {
      site = message;
      emit('site', message);
      return;
    }
    if (topic === COMMON_TOPICS.slotsUpdate) {
      emit('slots', message);
      return;
    }
    const robot = topic.match(/^locker\/robot\/([^/]+)\/state$/);
    if (robot) {
      const state = { ...message, robot_id: robot[1], receivedAt: Date.now() };
      robots.set(robot[1], state);
      emit('robot', state);
    }
  });

  function request(topic, body, { timeout = timeoutMs } = {}) {
    if (!connected) return Promise.reject(new LockerUnavailableError('보관소 서버에 연결되어 있지 않습니다.'));
    if (serverOnline === false) return Promise.reject(new LockerUnavailableError('보관소 서버가 꺼져 있습니다.'));
    const requestId = crypto.randomUUID();
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(requestId);
        reject(new LockerUnavailableError('보관소 서버가 응답하지 않습니다. 잠시 후 다시 시도해 주세요.'));
      }, timeout);
      pending.set(requestId, message => {
        clearTimeout(timer);
        pending.delete(requestId);
        resolve(message);
      });
      client.publish(topic, JSON.stringify({
        ...body, request_id: requestId, client_id: clientId, timestamp: new Date().toISOString()
      }), { qos: 1 });
    });
  }

  return {
    clientId,
    request,
    robots: () => [...robots.values()],
    site: () => site,
    serverOnline: () => serverOnline,
    isConnected: () => connected,
    /** 이벤트 구독. 반환값을 호출하면 구독 해제. */
    onEvent: listener => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    close: () => new Promise(resolve => client.end(false, {}, resolve))
  };
}

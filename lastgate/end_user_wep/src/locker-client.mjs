import crypto from 'node:crypto';
import mqtt from 'mqtt';

// 보관소 서버(server/)와 주고받는 MQTT 토픽. 규격은 저장소 루트의 protocol.md 참고.
export const TOPICS = {
  login: 'locker/auth/login',
  logout: 'locker/auth/logout',
  parcels: 'locker/resident/parcels',
  defaultMode: 'locker/resident/default_mode',
  itemMode: 'locker/resident/item_mode',
  pickupCode: 'locker/resident/pickup_code',
  hide: 'locker/resident/hide',
  robotState: 'locker/robot/+/state',
  clientRes: clientId => `locker/client/${clientId}/res`
};

export class LockerUnavailableError extends Error {}

/**
 * 키오스크와 같은 방식(request_id + 전용 응답 토픽)으로 보관소 서버에 요청한다.
 * FMS 가 보내는 로봇 상태(retained)도 구독해 최신값을 보관한다.
 */
export function createLockerClient({ url, timeoutMs = 5000, onStatus = () => {}, connect = mqtt.connect } = {}) {
  const clientId = `RESIDENT-WEB-${crypto.randomBytes(3).toString('hex').toUpperCase()}`;
  const resTopic = TOPICS.clientRes(clientId);
  const pending = new Map();
  const robots = new Map();
  let connected = false;

  const client = connect(url, { clientId, clean: true, reconnectPeriod: 2000, connectTimeout: 5000 });
  const setConnected = value => {
    if (connected === value) return;
    connected = value;
    onStatus(value);
  };
  client.on('connect', () => {
    client.subscribe([resTopic, TOPICS.robotState], { qos: 1 });
    setConnected(true);
  });
  client.on('close', () => setConnected(false));
  client.on('offline', () => setConnected(false));
  client.on('error', () => {}); // 브로커가 없으면 계속 재연결
  client.on('message', (topic, payload) => {
    let message;
    try { message = JSON.parse(payload.toString()); } catch { return; }
    if (!message || typeof message !== 'object') return;
    if (topic === resTopic) {
      const waiter = pending.get(message.request_id);
      if (waiter) waiter(message);
      return;
    }
    const robot = topic.match(/^locker\/robot\/([^/]+)\/state$/);
    if (robot) robots.set(robot[1], { ...message, robot_id: robot[1], receivedAt: Date.now() });
  });

  function request(topic, body) {
    if (!connected) return Promise.reject(new LockerUnavailableError('보관소 서버에 연결되어 있지 않습니다.'));
    const requestId = crypto.randomUUID();
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(requestId);
        reject(new LockerUnavailableError('보관소 서버가 응답하지 않습니다. 잠시 후 다시 시도해 주세요.'));
      }, timeoutMs);
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
    request,
    robots: () => [...robots.values()],
    isConnected: () => connected,
    close: () => new Promise(resolve => client.end(false, {}, resolve))
  };
}

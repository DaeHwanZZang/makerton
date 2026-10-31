// 브라우저 ↔ 보관소 서버 MQTT 클라이언트 (Mosquitto WebSocket).
// - 요청마다 request_id 를 붙이고, 응답은 이 클라이언트 전용 토픽에서만 받는다.
// - request_id 가 없는 전용 토픽 메시지(PARCELS_UPDATE, REGISTER_CANCELLED)는 push 로 전달한다.
import mqtt from "mqtt";
import type { MqttClient } from "mqtt";
import { BROKER_URL, TOPICS } from "./config";

export type Msg = Record<string, any>;

const randomId = () => Math.random().toString(16).slice(2, 10) + Date.now().toString(16).slice(-4);
const decoder = new TextDecoder();

export class LockerClient {
  readonly clientId: string;
  connected = false;
  lastSummary: Msg | null = null;
  private mq: MqttClient;
  private pending = new Map<string, { resolve: (m: Msg) => void; reject: (e: Error) => void; timer: number }>();
  private pushFns = new Set<(m: Msg) => void>();
  private statusFns = new Set<(c: boolean) => void>();
  private summaryFns = new Set<(m: Msg) => void>();

  constructor(kioskId: string) {
    this.clientId = `${kioskId}-${randomId().slice(0, 6).toUpperCase()}`;
    const resTopic = TOPICS.clientRes(this.clientId);
    const onlineTopic = TOPICS.clientOnline(this.clientId);
    this.mq = mqtt.connect(BROKER_URL, {
      clientId: this.clientId,
      clean: true,
      reconnectPeriod: 2000,
      connectTimeout: 5000,
      will: { topic: onlineTopic, payload: "offline", qos: 1, retain: true },
    });
    this.mq.on("connect", () => {
      this.mq.subscribe([resTopic, TOPICS.summary], { qos: 1 });
      this.mq.publish(onlineTopic, "online", { qos: 1, retain: true });
      this.setConnected(true);
    });
    this.mq.on("close", () => this.setConnected(false));
    this.mq.on("offline", () => this.setConnected(false));
    this.mq.on("message", (topic, payload) => {
      let msg: Msg;
      try {
        msg = JSON.parse(decoder.decode(payload));
      } catch {
        return;
      }
      if (topic === TOPICS.summary) {
        this.lastSummary = msg;
        this.summaryFns.forEach((fn) => fn(msg));
        return;
      }
      if (topic !== resTopic) return;
      const waiter = msg.request_id ? this.pending.get(msg.request_id) : undefined;
      if (waiter) {
        window.clearTimeout(waiter.timer);
        this.pending.delete(msg.request_id);
        waiter.resolve(msg);
      } else {
        this.pushFns.forEach((fn) => fn(msg));
      }
    });
  }

  request(topic: string, body: Msg, timeoutMs = 6000): Promise<Msg> {
    if (!this.connected) return Promise.reject(new Error("서버에 연결되어 있지 않습니다."));
    const request_id = randomId();
    return new Promise((resolve, reject) => {
      const timer = window.setTimeout(() => {
        this.pending.delete(request_id);
        reject(new Error("서버 응답이 없습니다. 잠시 후 다시 시도해 주세요."));
      }, timeoutMs);
      this.pending.set(request_id, { resolve, reject, timer });
      const message = { ...body, request_id, client_id: this.clientId, timestamp: new Date().toISOString() };
      this.mq.publish(topic, JSON.stringify(message), { qos: 1 });
    });
  }

  onPush(fn: (m: Msg) => void) {
    this.pushFns.add(fn);
    return () => void this.pushFns.delete(fn);
  }

  onStatus(fn: (connected: boolean) => void) {
    this.statusFns.add(fn);
    fn(this.connected);
    return () => void this.statusFns.delete(fn);
  }

  onSummary(fn: (m: Msg) => void) {
    this.summaryFns.add(fn);
    if (this.lastSummary) fn(this.lastSummary);
    return () => void this.summaryFns.delete(fn);
  }

  end() {
    this.pending.forEach((p) => {
      window.clearTimeout(p.timer);
      p.reject(new Error("연결 종료"));
    });
    this.pending.clear();
    this.mq.end(true);
  }

  private setConnected(value: boolean) {
    if (this.connected === value) return;
    this.connected = value;
    this.statusFns.forEach((fn) => fn(value));
  }
}

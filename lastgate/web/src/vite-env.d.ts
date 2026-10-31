/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** MQTT WebSocket 주소 (예: ws://192.168.0.50:9001). 없으면 현재 호스트의 9001 포트 */
  readonly VITE_BROKER_URL?: string;
}

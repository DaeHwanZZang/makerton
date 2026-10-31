"""paho-mqtt 래퍼 (서버·테스트 도구 공용).

- JSON 자동 직렬화/역직렬화
- 토픽별 핸들러 등록, 핸들러 예외가 네트워크 루프를 멈추지 않도록 보호
- LWT(Last Will) 로 online/offline 표시
- connect_async + loop_start: 브로커가 늦게 떠도 자동 재연결
"""
from __future__ import annotations

import json
import traceback
import uuid
from typing import Callable

import paho.mqtt.client as mqtt

import config


class MqttClient:
    def __init__(self, client_id: str, online_topic: str | None = None):
        self.client_id = client_id
        self._handlers: dict[str, Callable[[dict], None]] = {}
        self._connect_cbs: list[Callable[[bool], None]] = []
        self.connected = False

        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"{client_id}-{uuid.uuid4().hex[:6]}",
            clean_session=True,
        )
        if config.BROKER_USERNAME:
            self._client.username_pw_set(config.BROKER_USERNAME, config.BROKER_PASSWORD)

        self._online_topic = online_topic
        if online_topic:
            self._client.will_set(online_topic, "offline", qos=config.QOS, retain=True)

        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    # --- 공개 API --------------------------------------------------------
    def on(self, topic: str, handler: Callable[[dict], None]) -> None:
        self._handlers[topic] = handler
        if self.connected:
            self._client.subscribe(topic, qos=config.QOS)

    def on_connection_change(self, cb: Callable[[bool], None]) -> None:
        self._connect_cbs.append(cb)

    def connect(self) -> None:
        self._client.connect_async(config.BROKER_HOST, config.BROKER_PORT, config.BROKER_KEEPALIVE)
        self._client.loop_start()

    def publish(self, topic: str, payload: dict, retain: bool = False) -> None:
        if not self.connected:
            raise ConnectionError("브로커 연결 대기 중")
        info = self._client.publish(topic, json.dumps(payload, ensure_ascii=False),
                                    qos=config.QOS, retain=retain)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise ConnectionError(f"발행 실패: {info.rc}")

    def disconnect(self) -> None:
        if self._online_topic:
            self._client.publish(self._online_topic, "offline", qos=config.QOS, retain=True)
        self._client.loop_stop()
        self._client.disconnect()

    @staticmethod
    def new_request_id() -> str:
        return uuid.uuid4().hex[:12]

    # --- 내부 콜백 -------------------------------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties):
        self.connected = not reason_code.is_failure
        if self.connected:
            for topic in self._handlers:
                client.subscribe(topic, qos=config.QOS)
            if self._online_topic:
                client.publish(self._online_topic, "online", qos=config.QOS, retain=True)
        for cb in self._connect_cbs:
            cb(self.connected)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected = False
        for cb in self._connect_cbs:
            cb(False)

    def _on_message(self, client, userdata, msg):
        handler = self._handlers.get(msg.topic)
        if not handler:
            return
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(payload, dict):
            return
        try:
            handler(payload)
        except Exception:
            traceback.print_exc()

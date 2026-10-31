"""테스트 도구용 요청/응답 헬퍼. 키오스크와 같은 방식(request_id + 전용 응답 토픽)으로 요청한다."""
from __future__ import annotations

import queue
import time
import uuid

import config
from mqtt_client import MqttClient


class Rpc:
    def __init__(self, name: str):
        self.client_id = f"{name}-{uuid.uuid4().hex[:6]}"
        self.mqtt = MqttClient(self.client_id)
        self._waiters: dict[str, queue.Queue] = {}
        self.pushes: queue.Queue = queue.Queue()
        self.mqtt.on(config.TOPIC_CLIENT_RES.format(client_id=self.client_id), self._on_response)
        self.mqtt.connect()
        deadline = time.time() + 5
        while not self.mqtt.connected:
            if time.time() > deadline:
                raise SystemExit(f"브로커({config.BROKER_HOST}:{config.BROKER_PORT})에 연결할 수 없습니다.")
            time.sleep(0.05)
        time.sleep(0.3)                     # 응답 토픽 구독이 끝날 때까지 잠시 대기

    def _on_response(self, payload: dict):
        waiter = self._waiters.get(payload.get("request_id"))
        (waiter or self.pushes).put(payload)

    def request(self, topic: str, body: dict, timeout: float = 5.0) -> dict:
        request_id = MqttClient.new_request_id()
        waiter: queue.Queue = queue.Queue()
        self._waiters[request_id] = waiter
        self.mqtt.publish(topic, {**body, "request_id": request_id, "client_id": self.client_id,
                                  "timestamp": config.now_kst().isoformat()})
        try:
            return waiter.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"서버 응답 없음: {topic} (server.py 가 실행 중인지 확인)") from None
        finally:
            self._waiters.pop(request_id, None)

    def site_config(self, timeout: float = 3.0) -> dict | None:
        """retained 현장 설정(locker/config/site). 초기 설정 전이면 site 가 None."""
        box: queue.Queue = queue.Queue()
        self.mqtt.on(config.TOPIC_SITE_CONFIG, box.put)
        try:
            return box.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self):
        self.mqtt.disconnect()

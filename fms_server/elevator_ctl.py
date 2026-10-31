"""
엘리베이터 제어기 클라이언트
===========================

실제(또는 시뮬레이터) 승강기 제어기와 MQTT 로 말한다 (`common/elevator_proto.py`).
FMS 는 승강기를 시뮬레이션하지 않는다 — 여기는 상태를 받아 두고 명령을 보내며,
**승강기 한 대를 한 job 이 독점하는 임대(lease)** 만 직접 관리한다.

임대는 메모리 상태다. 서버를 다시 띄우면 사라지고, 진행 중 job 이 다음 tick 에 다시 잡는다 (idempotent).
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Optional

import paho.mqtt.client as mqtt

from common import elevator_proto as P

log = logging.getLogger("fms.elevator")


@dataclass(frozen=True)
class CarState:
    elevator_id: str
    at: Optional[str]
    target: Optional[str]
    door: str
    moving: bool
    held: bool
    received_at: float

    def open_at(self, map_id: str) -> bool:
        return self.at == map_id and self.door == P.DOOR_OPEN


class ElevatorController:
    def __init__(self, settings, *, stale_after: float = 5.0) -> None:
        self._settings = settings
        self._stale_after = stale_after
        self._lock = threading.RLock()
        self._cars: dict[str, CarState] = {}
        self._leases: dict[str, str] = {}
        self._listeners: list = []
        self._connected = False
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"{settings.mqtt_client_id}-elevator", clean_session=True)
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

    # -- 수명 주기 --
    def start(self) -> None:
        try:
            self._client.connect_async(self._settings.mqtt_host, self._settings.mqtt_port, self._settings.mqtt_keepalive)
            self._client.loop_start()
        except Exception:
            log.exception("엘리베이터 MQTT 시작 실패 (재연결은 paho 담당)")

    def stop(self) -> None:
        try:
            self._client.disconnect()
        finally:
            self._client.loop_stop()

    def add_listener(self, cb) -> None:
        self._listeners.append(cb)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code == 0:
            self._connected = True
            client.subscribe(P.TOPIC_STATE_ALL, 0)

    def _on_message(self, client, userdata, msg) -> None:
        parts = msg.topic.split("/")
        if len(parts) != 4 or parts[3] != "state" or not msg.payload:
            return
        try:
            body = json.loads(msg.payload)
            car = CarState(parts[2], body.get("at"), body.get("target"), str(body.get("door")),
                           bool(body.get("moving")), bool(body.get("held")), time.time())
        except (ValueError, AttributeError):
            log.warning("%s: 잘못된 state", msg.topic)
            return
        self.apply_state(car)

    def apply_state(self, car: CarState) -> None:
        with self._lock:
            self._cars[car.elevator_id] = car
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(car)
            except Exception:
                log.exception("엘리베이터 리스너 실패")

    # -- 조회 --
    def state(self, elevator_id: str) -> Optional[CarState]:
        """최근(stale_after 이내) 상태. 제어기가 죽었으면 None."""
        with self._lock:
            c = self._cars.get(elevator_id)
        if c is None or time.time() - c.received_at > self._stale_after:
            return None
        return c

    def snapshot(self, elevator_id: str) -> dict:
        """관제 표시용 상태. 오래된 수신값도 보존하되 현재 상태와 구분한다."""
        with self._lock:
            car = self._cars.get(elevator_id)
            owner = self._leases.get(elevator_id)
        if car is None:
            return dict(liveness="UNKNOWN", at=None, target=None, door=None,
                        moving=None, held=None, received_at=None, lease_owner=owner)
        fresh = time.time() - car.received_at <= self._stale_after
        return dict(liveness="ONLINE" if fresh else "STALE", at=car.at,
                    target=car.target, door=car.door, moving=car.moving, held=car.held,
                    received_at=car.received_at, lease_owner=owner)

    # -- 임대 --
    def acquire(self, elevator_id: str, owner: str) -> bool:
        with self._lock:
            cur = self._leases.setdefault(elevator_id, owner)
            return cur == owner

    def lease_owner(self, elevator_id: str) -> Optional[str]:
        with self._lock:
            return self._leases.get(elevator_id)

    def release_lease(self, elevator_id: str, owner: str) -> None:
        with self._lock:
            if self._leases.get(elevator_id) == owner:
                del self._leases[elevator_id]

    # -- 명령 --
    def _send(self, elevator_id: str, body: dict) -> None:
        body = {"cmd_id": uuid.uuid4().hex[:12], **body}
        self._client.publish(P.topic_cmd(elevator_id), json.dumps(body), qos=1)
        log.info("엘리베이터 %s 명령 %s", elevator_id, body)

    def call(self, elevator_id: str, map_id: str, hold: bool = True) -> None:
        self._send(elevator_id, {"type": P.CMD_CALL, "map_id": map_id, "hold": hold})

    def go(self, elevator_id: str, map_id: str, hold: bool = True) -> None:
        self._send(elevator_id, {"type": P.CMD_GO, "map_id": map_id, "hold": hold})

    def release(self, elevator_id: str) -> None:
        self._send(elevator_id, {"type": P.CMD_RELEASE})

    def publish_json(self, topic: str, body: dict) -> None:
        self._client.publish(topic, json.dumps(body), qos=1)

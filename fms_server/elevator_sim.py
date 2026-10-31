"""
가상 엘리베이터 (시뮬레이터)
===========================

`common/elevator_proto.py` 계약대로 승강기를 흉내 낸다. 두 가지로 쓴다.

- **FMS 내장** (기본 켜짐, `FMS_ELEVATOR_SIM=0` 이면 끔): `BuiltinElevatorSim` 이 FMS 프로세스 안에서 돈다.
  등록된 승강기 연결(`elevators.json`)을 2 초마다 다시 읽어 칸을 만들고/고치고/지운다 — UI 에서 승강기를
  짝지으면 바로 탈 수 있다. FMS 와 같은 브로커에 MQTT 로 state 를 내고 cmd 를 받는다 (FMS 쪽
  `elevator_ctl.py` 는 이게 내장인지 외부인지 모른다).
- **외부 프로세스**: `tools/elevator_sim.py` (FMS 내장을 끄고 따로 띄울 때, 또는 실제 제어기 대역).

실제 엘리베이터 제어기를 붙일 때는 내장을 꺼야 한다 (같은 토픽에 state 를 두 군데서 내면 안 된다).

칸 하나 = `ElevatorCar`. 요청은 FIFO: 그 층까지 이동(층당 floor_time) -> 문 열림(door_time) ->
(hold 가 아니면 open_time 뒤) 닫힘 -> 다음 요청. hold 요청은 RELEASE 가 올 때까지 안 닫는다.
"""

from __future__ import annotations

import json
import logging
import queue as queue_mod
import threading
import time
from collections import deque

from common import elevator_proto as P

log = logging.getLogger("fms.elevator_sim")


class ElevatorCar:
    """한 대의 칸. MQTT 를 모른다 (step(dt) 로 시간을 먹인다)."""

    def __init__(self, elevator_id: str, stops: list[str], *, floor_time: float = 3.0, door_time: float = 1.5,
                 open_time: float = 3.0) -> None:
        if len(stops) < 2:
            raise ValueError("stops 는 2개 이상")
        self.elevator_id = elevator_id
        self.stops = list(stops)
        self.floor_time, self.door_time, self.open_time = floor_time, door_time, open_time
        self.at: str | None = self.stops[0]
        self.target: str | None = None
        self.door = P.DOOR_CLOSED
        self.held = False
        self.queue: deque[tuple[str, bool]] = deque()  # (map_id, hold)
        self._travel_left = 0.0
        self._door_timer = 0.0
        self._open_timer = 0.0
        self._cur_hold = False
        self.version = 0  # 상태가 바뀔 때마다 증가 (발행 판단용)

    @property
    def moving(self) -> bool:
        return self.at is None

    def command(self, ctype: str, map_id: str | None = None, hold: bool = False) -> bool:
        if ctype == P.CMD_RELEASE:
            self.held = False
            self._cur_hold = False
            self.version += 1
            return True
        if ctype not in (P.CMD_CALL, P.CMD_GO) or map_id not in self.stops:
            return False
        # 이미 그 층에 서 있고 문이 열려/열리는 중이면 바로 hold 만 건다
        if self.at == map_id and not self.queue and self.door in (P.DOOR_OPEN, P.DOOR_OPENING):
            self.held = self.held or hold
            self._open_timer = 0.0
        elif (map_id, hold) not in self.queue and not (self.target == map_id and self._cur_hold == hold):
            self.queue.append((map_id, hold))
        self.version += 1
        return True

    def _set_door(self, door: str) -> None:
        self.door = door
        self.version += 1

    def step(self, dt: float) -> None:
        if self.door == P.DOOR_OPENING:
            self._door_timer -= dt
            if self._door_timer <= 0:
                self._set_door(P.DOOR_OPEN)
                self._open_timer = 0.0
            return
        if self.door == P.DOOR_CLOSING:
            self._door_timer -= dt
            if self._door_timer <= 0:
                self._set_door(P.DOOR_CLOSED)
            return
        if self.door == P.DOOR_OPEN:
            self._open_timer += dt
            if self.held or self._open_timer < self.open_time:
                return
            # 다음 요청이 같은 층이면 닫지 않고 소비한다
            while self.queue and self.queue[0][0] == self.at:
                _, hold = self.queue.popleft()
                self.held = self.held or hold
                self._open_timer = 0.0
            if self.held or self._open_timer == 0.0:
                return
            self.target = None
            self._cur_hold = False
            self._door_timer = self.door_time
            self._set_door(P.DOOR_CLOSING)
            return
        # 문 닫힘
        if self.moving:
            self._travel_left -= dt
            if self._travel_left <= 0:
                self.at = self.target
                self._door_timer = self.door_time
                self._set_door(P.DOOR_OPENING)
                if self._cur_hold:
                    self.held = True
            return
        if not self.queue:
            return
        map_id, hold = self.queue.popleft()
        self.target, self._cur_hold = map_id, hold
        if map_id == self.at:
            self._door_timer = self.door_time
            if hold:
                self.held = True
            self._set_door(P.DOOR_OPENING)
            return
        hops = abs(self.stops.index(map_id) - self.stops.index(self.at))
        self._travel_left = hops * self.floor_time
        self.at = None
        self.version += 1

    def state(self) -> dict:
        return {
            "elevator_id": self.elevator_id, "timestamp": time.time(), "stops": self.stops,
            "at": self.at, "target": self.target, "door": self.door, "moving": self.moving, "held": self.held,
        }


class BuiltinElevatorSim:
    """FMS 프로세스 안의 가상 엘리베이터들. 등록된 승강기 연결을 따라간다."""

    def __init__(self, settings, elevator_store, registry, *, tick: float = 0.05, sync_every: float = 2.0,
                 floor_time: float = 3.0, door_time: float = 1.5, open_time: float = 3.0) -> None:
        import paho.mqtt.client as mqtt

        self._settings = settings
        self._store_src = elevator_store  # ElevatorStore 또는 그걸 돌려주는 callable
        self._registry_src = registry
        self._tick, self._sync_every = tick, sync_every
        self._car_args = dict(floor_time=floor_time, door_time=door_time, open_time=open_time)
        self.cars: dict[str, ElevatorCar] = {}
        self._inbox: queue_mod.Queue = queue_mod.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                                   client_id=f"{settings.mqtt_client_id}-elevator-sim", clean_session=True)
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client.on_connect = lambda c, *_a: c.subscribe(f"{P.TOPIC_PREFIX}/+/cmd", 1)
        self._client.on_message = self._on_message

    @staticmethod
    def _get(src):
        return src() if callable(src) else src

    # -- 수명 주기 --
    def start(self) -> None:
        try:
            self._client.connect_async(self._settings.mqtt_host, self._settings.mqtt_port, self._settings.mqtt_keepalive)
            self._client.loop_start()
        except Exception:
            log.exception("가상 엘리베이터 MQTT 시작 실패 (재연결은 paho 담당)")
        self._thread = threading.Thread(target=self._run, name="elevator-sim", daemon=True)
        self._thread.start()
        log.info("가상 엘리베이터 시작 (FMS 내장, 끄려면 FMS_ELEVATOR_SIM=0)")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        try:
            self._client.disconnect()
        finally:
            self._client.loop_stop()

    # -- MQTT --
    def _on_message(self, _c, _u, msg) -> None:
        parts = msg.topic.split("/")
        if len(parts) != 4:
            return
        try:
            self._inbox.put((parts[2], json.loads(msg.payload)))
        except ValueError:
            log.warning("%s: 잘못된 cmd", msg.topic)

    # -- 등록 승강기 따라가기 --
    def sync(self) -> None:
        """elevators.json 의 승강기 연결과 칸 목록을 맞춘다."""
        store, registry = self._get(self._store_src), self._get(self._registry_src)
        if store is None or registry is None:
            return
        want: dict[str, list[str]] = {}
        for site in registry.list_sites():
            for e in store.list(site.site_id):
                if e.elevator_id in want:
                    log.warning("승강기 id %s 가 여러 site 에 있다 — 토픽이 겹쳐 하나만 시뮬레이션한다", e.elevator_id)
                    continue
                want[e.elevator_id] = [s.map_id for s in e.stops]
        for eid in list(self.cars):
            if eid not in want:
                del self.cars[eid]
                self._client.publish(P.topic_state(eid), b"", 1, True)  # retain 지우기
                log.info("가상 엘리베이터 %s 제거", eid)
        for eid, stops in want.items():
            car = self.cars.get(eid)
            if car is None:
                self.cars[eid] = ElevatorCar(eid, stops, **self._car_args)
                log.info("가상 엘리베이터 %s 생성 (%s)", eid, "/".join(stops))
            elif car.stops != stops:
                idle = car.door == P.DOOR_CLOSED and not car.moving and not car.queue and not car.held
                if idle:  # 쓰는 중에는 바꾸지 않는다 (다음 sync 때 다시 본다)
                    new = ElevatorCar(eid, stops, **self._car_args)
                    if car.at in stops:
                        new.at = car.at
                    self.cars[eid] = new
                    log.info("가상 엘리베이터 %s 층 변경 (%s)", eid, "/".join(stops))

    def _run(self) -> None:
        last_sync = 0.0
        published: dict[str, tuple[int, float]] = {}
        prev = time.time()
        while not self._stop.wait(self._tick):
            now = time.time()
            dt, prev = now - prev, now
            if now - last_sync >= self._sync_every:
                last_sync = now
                try:
                    self.sync()
                except Exception:
                    log.exception("가상 엘리베이터 동기화 실패")
            while True:
                try:
                    eid, body = self._inbox.get_nowait()
                except queue_mod.Empty:
                    break
                car = self.cars.get(eid)
                ok = car is not None and car.command(str(body.get("type")), body.get("map_id"), bool(body.get("hold")))
                log.info("가상 엘리베이터 %s %s %s hold=%s -> %s", eid, body.get("type"), body.get("map_id", ""),
                         body.get("hold", False), "ok" if ok else "무시")
            for eid, car in list(self.cars.items()):
                car.step(dt)
                v, t = published.get(eid, (-1, 0.0))
                if car.version != v or now - t >= 1.0:
                    self._client.publish(P.topic_state(eid), json.dumps(car.state()), 0, True)
                    published[eid] = (car.version, now)

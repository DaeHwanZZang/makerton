"""LastGate 보관소 서버 실행 진입점.

  python server.py           # 실행 (현장 설정이 없으면 관리자 웹 초기 설정을 기다린다)
  python server.py --reset   # DB 를 지우고 처음부터
"""
from __future__ import annotations

import argparse
import signal
import threading

import config
from core import LockerCore, log
from mqtt_client import MqttClient
from store import Store


def main():
    parser = argparse.ArgumentParser(description="LastGate 보관소 서버")
    parser.add_argument("--reset", action="store_true", help="DB 를 초기화하고 시작")
    args = parser.parse_args()

    store = Store(config.DB_PATH, reset=args.reset)
    mqtt = MqttClient("locker-server", online_topic=config.TOPIC_SERVER_ONLINE)
    core = LockerCore(store, publish=mqtt.publish)
    core.bootstrap()

    for topic in core.topics:
        mqtt.on(topic, lambda payload, t=topic: core.handle(t, payload))

    def on_connection(ok: bool):
        log("MQTT", "브로커 연결됨" if ok else "브로커 연결 끊김 — 자동 재시도")
        if ok:      # 연결 전에 쌓인 outbox(설정·요약 retained 포함)를 바로 보낸다
            threading.Thread(target=core.heartbeat, daemon=True).start()

    mqtt.on_connection_change(on_connection)
    mqtt.connect()
    log("서버", f"브로커 {config.BROKER_HOST}:{config.BROKER_PORT} · DB {config.DB_PATH} · 종료 Ctrl+C")

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    ticks = 0
    while not stop.wait(1):          # 1초 단위로 깨어나 Ctrl+C 에 빠르게 반응 (Windows 포함)
        ticks += 1
        if ticks % config.HEARTBEAT_SEC == 0:
            core.heartbeat()
    mqtt.disconnect()
    log("서버", "종료")


if __name__ == "__main__":
    main()

"""FMS(로봇 시뮬레이터) 대역.

기존 로봇 시뮬레이터를 연결하기 전에 배달 흐름을 끝까지 돌려보기 위한 도구다.
fms_adapter.py 의 메시지 형식으로 응답한다.

  python -m tools.fms_stub                       # 5초 뒤 도착, 물품당 2초 배달
  python -m tools.fms_stub --arrive 3 --per-item 1 --fail-rate 0.2
"""
from __future__ import annotations

import argparse
import random
import threading
import time

import config
from mqtt_client import MqttClient


def main():
    parser = argparse.ArgumentParser(description="FMS(로봇 시뮬레이터) 대역")
    parser.add_argument("--robot", default="R1")
    parser.add_argument("--arrive", type=float, default=5.0, help="Job 수신 후 보관소 도착까지(초)")
    parser.add_argument("--per-item", type=float, default=2.0, help="물품 1건 배달 시간(초)")
    parser.add_argument("--fail-rate", type=float, default=0.0, help="배달 실패 확률 0~1")
    args = parser.parse_args()

    mqtt = MqttClient("fms-stub")
    seen: set[tuple[str, str]] = set()
    # 세대 웹 '로봇 현황'용 상태 (retained). 실제 FMS 도 같은 토픽으로 보내면 된다
    state = {"status": "대기 중", "location": "공동 보관소", "battery": 92, "available": True}
    state_topic = config.TOPIC_ROBOT_STATE.format(robot_id=args.robot)

    def publish(payload: dict):
        mqtt.publish(config.TOPIC_FMS_EVENT, {**payload, "timestamp": config.now_kst().isoformat()})

    def publish_state(**changes):
        state.update(changes)
        mqtt.publish(state_topic, {"robot_id": args.robot, **state,
                                   "timestamp": config.now_kst().isoformat()}, retain=True)

    def arrive(job_id: str):
        publish_state(status="적재 중", location="공동 보관소")
        publish({"type": "ROBOT_ARRIVED", "job_id": job_id, "robot_id": args.robot})

    def on_job(msg: dict):
        if msg.get("type") != "DELIVERY_JOB" or ("JOB", msg["job_id"]) in seen:
            return
        seen.add(("JOB", msg["job_id"]))
        s = msg["summary"]
        print(f"\n[FMS] {msg['job_id']} 수신 (예상 {len(msg['items'])}건, 동 {s['dongs']}) "
              f"→ {args.arrive:.0f}초 뒤 {args.robot} 보관소 도착")
        publish_state(status="보관소로 이동 중", available=False)
        threading.Timer(args.arrive, arrive, [msg["job_id"]]).start()

    def on_bundle(msg: dict):
        kind, job_id = msg.get("type"), msg.get("job_id")
        if (kind, job_id) in seen:
            return
        seen.add((kind, job_id))
        if kind == "JOB_CANCELLED":
            print(f"[FMS] {job_id} 취소됨 ({msg.get('reason')})")
            publish_state(status="대기 중", available=True)
            return
        if kind != "BUNDLE_CONFIRMED":
            return
        s = msg["summary"]
        print(f"[FMS] {job_id} 적재 확정 · {len(msg['items'])}건 · {s['weight_kg']}kg")
        for it in msg["items"]:
            print(f"      {it['tray']:<7} {it['dong']}동 {it['ho']}호 [{it['size']}]")
        threading.Thread(target=deliver, args=(msg,), daemon=True).start()

    def deliver(msg: dict):
        for it in sorted(msg["items"], key=lambda i: (i["dong"], i["ho"])):
            publish_state(status="배송 중", location=f"{it['dong']}동 {it['ho']}호",
                          battery=max(20, state["battery"] - 1))
            time.sleep(args.per_item)
            failed = random.random() < args.fail_rate
            publish({"type": "DELIVERY_RESULT", "job_id": msg["job_id"],
                     "receipt_no": it["receipt_no"],
                     "result": "FAILED" if failed else "DELIVERED",
                     "reason": "부재" if failed else None})
            print(f"[FMS] {it['dong']}동 {it['ho']}호 {'배달 실패' if failed else '배달 완료'}")
        publish_state(status="대기 중", location="공동 보관소", available=True)
        publish({"type": "JOB_DONE", "job_id": msg["job_id"], "robot_id": args.robot})
        print(f"[FMS] {msg['job_id']} 완료, 로봇 복귀")

    def on_connection(ok: bool):
        print(f"[FMS] 브로커 {'연결됨' if ok else '연결 끊김'}")
        if ok:
            publish_state()

    mqtt.on(config.TOPIC_FMS_JOB, on_job)
    mqtt.on(config.TOPIC_FMS_BUNDLE, on_bundle)
    mqtt.on_connection_change(on_connection)
    mqtt.connect()
    print("[FMS] 대역 실행 중 · 종료 Ctrl+C")
    try:
        ticks = 0
        while True:
            time.sleep(1)
            ticks += 1
            if ticks % 10 == 0 and mqtt.connected:   # 하트비트: 세대 웹이 통신 끊김을 판단하는 기준
                publish_state()
    except KeyboardInterrupt:
        publish_state(status="통신 끊김", available=False)
        time.sleep(0.3)
        mqtt.disconnect()


if __name__ == "__main__":
    main()

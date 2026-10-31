#!/usr/bin/env python3
"""엘리베이터 시뮬레이터 (외부 프로세스). `common/elevator_proto.py` 계약대로 state 를 발행하고 CALL/GO/RELEASE 를 받는다.

FMS 가 기본으로 같은 시뮬레이터를 내장해 돌린다 (`fms_server/elevator_sim.py`). 이 스크립트는 FMS 를
`FMS_ELEVATOR_SIM=0` 으로 띄웠을 때만 쓴다 — 둘 다 돌리면 같은 토픽에 state 가 두 군데서 나온다.

    .venv/bin/python tools/elevator_sim.py --fms http://localhost:8000 --site gachon_ai      # FMS 에 등록된 승강기를 전부 띄운다
    .venv/bin/python tools/elevator_sim.py --elevator elev-A=f1,f3 --elevator elev-B=f1,f3   # 직접 지정 (id=층 map_id 목록)

승강기마다 칸 하나. 요청을 FIFO 로 처리한다: 그 층까지 이동(층당 --floor-time 초) -> 문 열림(--door-time) ->
(hold 가 아니면 --open-time 뒤) 닫힘 -> 다음 요청. hold 요청은 RELEASE 가 올 때까지 안 닫는다.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import elevator_proto as P  # noqa: E402
from fms_server.elevator_sim import ElevatorCar  # noqa: E402  (칸 로직은 FMS 내장과 공유)


def _fetch_elevators(fms: str, site: str) -> dict[str, list[str]]:
    with urllib.request.urlopen(f"{fms.rstrip('/')}/api/v1/sites/{site}/elevators", timeout=5) as r:
        data = json.load(r)
    return {e["elevator_id"]: [s["map_id"] for s in e["stops"]] for e in data["elevators"]}


def main() -> None:
    import paho.mqtt.client as mqtt

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--fms", default="http://localhost:8000")
    ap.add_argument("--site", help="이 site 의 승강기를 FMS REST 에서 읽어 전부 띄운다")
    ap.add_argument("--elevator", action="append", default=[], metavar="ID=MAP,MAP", help="직접 지정 (여러 번 가능)")
    ap.add_argument("--floor-time", type=float, default=3.0, help="한 층 이동 시간(초)")
    ap.add_argument("--door-time", type=float, default=1.5, help="문 열림/닫힘 시간(초)")
    ap.add_argument("--open-time", type=float, default=3.0, help="hold 가 아닐 때 문 열려 있는 시간(초)")
    args = ap.parse_args()

    defs: dict[str, list[str]] = {}
    if args.site:
        defs.update(_fetch_elevators(args.fms, args.site))
    for item in args.elevator:
        eid, _, maps = item.partition("=")
        defs[eid] = [m for m in maps.split(",") if m]
    if not defs:
        ap.error("승강기가 없다: --site 또는 --elevator 를 줘라")
    cars = {eid: ElevatorCar(eid, stops, floor_time=args.floor_time, door_time=args.door_time, open_time=args.open_time)
            for eid, stops in defs.items()}

    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id="elevator-sim")

    def on_connect(c, *_a):
        for eid in cars:
            c.subscribe(P.topic_cmd(eid), 1)
        print(f"연결됨. 승강기 {', '.join(f'{e}({'/'.join(s.stops)})' for e, s in cars.items())}", flush=True)

    def on_message(_c, _u, msg):
        eid = msg.topic.split("/")[2]
        car = cars.get(eid)
        try:
            body = json.loads(msg.payload)
        except ValueError:
            return
        if car is None:
            return
        ok = car.command(str(body.get("type")), body.get("map_id"), bool(body.get("hold", False)))
        print(f"[{eid}] {body.get('type')} {body.get('map_id', '')} hold={body.get('hold', False)} -> {'ok' if ok else 'ignored'}",
              flush=True)

    client.on_connect, client.on_message = on_connect, on_message
    client.connect(args.host, args.port, 30)
    client.loop_start()
    last_pub = {eid: (-1, 0.0) for eid in cars}
    tick, prev = 0.05, time.time()
    try:
        while True:
            time.sleep(tick)
            now = time.time()
            dt, prev = now - prev, now
            for eid, car in cars.items():
                car.step(dt)
                v, t = last_pub[eid]
                if car.version != v or now - t >= 1.0:
                    client.publish(P.topic_state(eid), json.dumps(car.state()), 0, True)
                    last_pub[eid] = (car.version, now)
    except KeyboardInterrupt:
        pass
    finally:
        client.loop_stop()


if __name__ == "__main__":
    main()

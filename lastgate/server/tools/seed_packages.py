"""택배기사 접수를 대신 흘려보내는 도구: 접수 → 투입 완료 를 N건 반복한다.

현장 설정(locker/config/site)의 건물·호실 중에서 고른다. 초기 설정 전이면 tools.init_site 를 먼저 실행한다.

  python -m tools.seed_packages                        # 12건, 무작위 건물·호실
  python -m tools.seed_packages 30
  python -m tools.seed_packages 3 --building 101 --unit 412
  python -m tools.seed_packages 2 --fragile             # 취급 주의 (직접 수령 고정)
  python -m tools.seed_packages 1 --oversize            # 배송 불가 보관함
"""
from __future__ import annotations

import argparse
import random
import time

import config
from tools.rpc import Rpc


def main():
    parser = argparse.ArgumentParser(description="테스트 택배 자동 접수")
    parser.add_argument("count", nargs="?", type=int, default=12)
    parser.add_argument("--building", type=int, help="건물 번호")
    parser.add_argument("--unit", type=int, help="호실 번호")
    parser.add_argument("--fragile", action="store_true")
    parser.add_argument("--oversize", action="store_true")
    parser.add_argument("--interval", type=float, default=0.2, help="건당 간격(초)")
    args = parser.parse_args()

    rpc = Rpc("SEEDER")
    msg = rpc.site_config()
    site = (msg or {}).get("site")
    if not site:
        rpc.close()
        raise SystemExit("현장 설정이 없습니다. 관리자 웹 초기 설정 또는 python -m tools.init_site 를 먼저 실행하세요.")
    buildings = {b["code"]: b for b in site["buildings"]}
    area = "OVERSIZE" if args.oversize else "GRID"
    stored = 0
    for i in range(args.count):
        b = buildings.get(args.building) or random.choice(list(buildings.values()))
        unit = args.unit or random.choice(b["units"])
        label = f"{b['name']} {unit}{site['unit_term']}"
        res = rpc.request(config.TOPIC_REGISTER, {"building": b["code"], "unit": unit, "area": area,
                                                  "fragile": args.fragile, "kiosk_id": "SEEDER"})
        if res.get("status") != "OK":
            print(f"{i + 1:>3}. {label} 접수 실패: {res.get('message')}")
            continue
        conf = rpc.request(config.TOPIC_PORT_CONFIRM, {"receipt_no": res["receipt_no"],
                                                       "port": area, "source": "SEED"})
        ok = conf.get("status") == "OK"
        stored += ok
        print(f"{i + 1:>3}. {label} {res['receipt_no']} → {res['space_id']} "
              f"{'보관 완료' if ok else '투입 실패: ' + str(conf.get('message'))}")
        time.sleep(args.interval)
    rpc.close()
    print(f"완료: {stored}/{args.count}건 보관")


if __name__ == "__main__":
    main()

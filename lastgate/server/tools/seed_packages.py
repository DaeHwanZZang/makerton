"""택배기사 접수를 대신 흘려보내는 도구: 접수 → 투입 완료 를 N건 반복한다.

  python -m tools.seed_packages            # 12건, 무작위 동·호수·크기
  python -m tools.seed_packages 30
  python -m tools.seed_packages 3 --dong 101 --ho 412 --size M
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
    parser.add_argument("--dong", type=int)
    parser.add_argument("--ho", type=int)
    parser.add_argument("--size", choices=config.SIZES)
    parser.add_argument("--interval", type=float, default=0.2, help="건당 간격(초)")
    args = parser.parse_args()

    rpc = Rpc("SEEDER")
    stored = 0
    for i in range(args.count):
        dong = args.dong or random.choice(config.VALID_DONGS)
        ho = args.ho or random.randint(1, config.FLOORS) * 100 + random.randint(1, config.UNITS_PER_FLOOR)
        size = args.size or random.choices(config.SIZES, weights=[0.5, 0.35, 0.15])[0]
        res = rpc.request(config.TOPIC_REGISTER, {"dong": dong, "ho": ho, "size": size})
        if res.get("status") != "OK":
            print(f"{i + 1:>3}. {dong}동 {ho}호 [{size}] 접수 실패: {res.get('message')}")
            continue
        conf = rpc.request(config.TOPIC_PORT_CONFIRM, {"receipt_no": res["receipt_no"],
                                                       "port": size, "source": "SEED"})
        ok = conf.get("status") == "OK"
        stored += ok
        print(f"{i + 1:>3}. {dong}동 {ho}호 [{size}] {res['receipt_no']} "
              f"{'보관 완료' if ok else '투입 실패: ' + str(conf.get('message'))}")
        time.sleep(args.interval)
    rpc.close()
    print(f"완료: {stored}/{args.count}건 보관")


if __name__ == "__main__":
    main()

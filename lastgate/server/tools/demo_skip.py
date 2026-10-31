"""대회 시연 전용: 로봇 배송 대기 시간을 바로 끝낸다 (관리자 웹 '보관 현황 → 대기 건너뛰기'와 같다).

보관소 서버를 LASTGATE_DEMO=1 로 실행했을 때만 동작한다.
  - 배송 시간 예약 가능 시간(보관 후 30분) → 지금 자동 배송 확정
  - 예약한 물품 → 칸 시작 15분 전까지 기다리지 않고 지금 발행
로봇은 물품이 2개 모여야 출발한다. 진행 중 배송이 로봇 대수만큼 있으면 끝난 뒤 발행한다.

  python -m tools.demo_skip
  python -m tools.demo_skip --password <관리자 비밀번호>
"""
from __future__ import annotations

import argparse

import config
from tools.rpc import Rpc


def main():
    parser = argparse.ArgumentParser(description="시연: 배송 대기 건너뛰기")
    parser.add_argument("--password", default=config.ADMIN_ENV_PASSWORD or config.ADMIN_DEFAULT_PASSWORD)
    args = parser.parse_args()
    rpc = Rpc(config.ADMIN_CLIENT_PREFIX + "DEMO")
    try:
        login = rpc.request(config.TOPIC_ADMIN_LOGIN, {"password": args.password})
        if login.get("status") != "OK":
            raise SystemExit(f"관리자 로그인 실패: {login.get('message')}")
        res = rpc.request(config.TOPIC_ADMIN_DEMO_SKIP, {"session": login["session"]})
        if res.get("status") != "OK":
            raise SystemExit(f"실패: {res.get('message')}")
        print(res["summary"])
        for job in res["jobs"]:
            print(f"  발행: {job}")
        for item in res["waiting"]:
            print(f"  대기: {item['label']} {item['receipt_no']}")
        rpc.request(config.TOPIC_ADMIN_LOGOUT, {"session": login["session"]})
    finally:
        rpc.close()


if __name__ == "__main__":
    main()

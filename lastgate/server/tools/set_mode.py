"""수령 방식을 바꾸는 명령줄 도구. 세대 웹과 같은 토픽·세션 방식으로 요청한다.

  python -m tools.set_mode 101-412 --list                        # 현재 기본값과 보관 물품
  python -m tools.set_mode 101-412 DIRECT                        # 세대 기본값(토글) → 일괄 변경
  python -m tools.set_mode 101-412 ROBOT
  python -m tools.set_mode 101-412 DIRECT --item 20261031-89B536F3   # 물품별 변경
아이디는 '{건물 번호}-{호실}' 이다.
"""
from __future__ import annotations

import argparse

import config
from tools.rpc import Rpc


def print_parcels(household: dict, parcels: list[dict]):
    print(f"{household['label']} · 기본값 {household['default_mode']}")
    if not parcels:
        print("  보관 중인 택배 없음")
    for p in parcels:
        lock = f" 고정:{p['direct_lock']}" if p.get("direct_lock") else ""
        print(f"  {p['receipt_no']}  {p['space_id']:<6} {p['mode']:<6} {p.get('plan') or '':<11}"
              f"{lock}  도착 {p['arrived_at']}")


def main():
    parser = argparse.ArgumentParser(description="세대 수령 방식 변경")
    parser.add_argument("user_id", help="아이디 (예: 101-412)")
    parser.add_argument("mode", nargs="?", choices=["ROBOT", "DIRECT"])
    parser.add_argument("--item", help="물품별로 바꿀 접수번호")
    parser.add_argument("--list", action="store_true", help="현재 상태만 보기")
    parser.add_argument("--password", default=config.SEED_PASSWORD)
    args = parser.parse_args()
    if not args.mode and not args.list:
        parser.error("mode(ROBOT/DIRECT) 또는 --list 가 필요합니다.")

    rpc = Rpc("SET-MODE")
    login = rpc.request(config.TOPIC_LOGIN, {"user_id": args.user_id, "password": args.password})
    if login.get("status") != "OK":
        rpc.close()
        raise SystemExit(f"로그인 실패: {login.get('message')}")
    session = login["session"]
    try:
        if args.mode and args.item:
            res = rpc.request(config.TOPIC_ITEM_MODE,
                              {"session": session, "receipt_no": args.item, "mode": args.mode})
            print(f"물품별 변경: {res.get('status')} {res.get('message', '')}")
        elif args.mode:
            res = rpc.request(config.TOPIC_DEFAULT_MODE, {"session": session, "mode": args.mode})
            print(f"기본값 변경: {res.get('status')} · 일괄 변경 {res.get('changed', 0)}건 · "
                  f"고정 {res.get('skipped', 0)}건")
        state = rpc.request(config.TOPIC_PARCELS, {"session": session})
        print_parcels(state["household"], state["parcels"])
    finally:
        rpc.request(config.TOPIC_LOGOUT, {"session": session})
        rpc.close()


if __name__ == "__main__":
    main()

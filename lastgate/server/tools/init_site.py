"""데모용 관리자 초기 설정. 관리자 웹과 같은 토픽으로 요청한다 (client_id ADMIN-WEB-*).

  python -m tools.init_site                          # 예시 아파트: 101~106동, 101~412호 (288세대)
  python -m tools.init_site --campus                 # 예시 대학교: 공학관·인문관
  python -m tools.init_site --file site.json         # 직접 만든 설정 (형식은 protocol.md 의 site)
  python -m tools.init_site --password <관리자 비밀번호>
이미 초기 설정이 되어 있으면 아무것도 바꾸지 않는다.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import config
from tools.rpc import Rpc

APARTMENT = {
    "name": "LastGate 아파트", "building_term": "동", "unit_term": "호",
    "buildings": [{"code": d, "name": f"{d}동", "unit_from": 101, "unit_to": 412} for d in range(101, 107)],
}
CAMPUS = {
    "name": "LastGate 대학교", "building_term": "관", "unit_term": "호실",
    "buildings": [
        {"code": 1, "name": "공학관", "unit_from": 101, "unit_to": 614, "missing": "404, 501~514"},
        {"code": 2, "name": "인문관", "unit_from": 101, "unit_to": 310, "extra": "1501"},
    ],
}


def main():
    parser = argparse.ArgumentParser(description="데모 현장 초기 설정")
    parser.add_argument("--campus", action="store_true", help="대학교 예시 사용")
    parser.add_argument("--file", type=Path, help="설정 JSON 파일")
    parser.add_argument("--password", default=config.ADMIN_ENV_PASSWORD or config.ADMIN_DEFAULT_PASSWORD)
    args = parser.parse_args()
    site = json.loads(args.file.read_text(encoding="utf-8")) if args.file else (CAMPUS if args.campus else APARTMENT)
    site = {**config.SITE_DEFAULTS, **site}

    rpc = Rpc(config.ADMIN_CLIENT_PREFIX + "INIT")
    try:
        login = rpc.request(config.TOPIC_ADMIN_LOGIN, {"password": args.password})
        if login.get("status") != "OK":
            raise SystemExit(f"관리자 로그인 실패: {login.get('message')}")
        if login.get("initialized"):
            print(f"이미 초기 설정됨: {login.get('site_name')} (변경은 관리자 웹 '설정 변경')")
            return
        session = login["session"]
        preview = rpc.request(config.TOPIC_ADMIN_SITE_PREVIEW, {"session": session, "site": site})
        if preview.get("status") != "OK":
            raise SystemExit(f"설정 오류: {preview.get('message')}")
        for b in preview["buildings"]:
            print(f"  {b['name']} (번호 {b['code']}) · {b['count']}개 · {b['guide']}")
        res = rpc.request(config.TOPIC_ADMIN_SITE_INIT, {"session": session, "site": site}, timeout=30)
        if res.get("status") != "OK":
            raise SystemExit(f"초기 설정 실패: {res.get('message')}")
        print(f"초기 설정 완료: {site['name']} · 계정 {res['accounts']}개 (비밀번호 {config.SEED_PASSWORD})")
        rpc.request(config.TOPIC_ADMIN_LOGOUT, {"session": session})
    finally:
        rpc.close()


if __name__ == "__main__":
    main()

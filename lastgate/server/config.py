"""LastGate 보관소 서버 설정.

브로커 주소, 토픽, 단지 규모, 박스 규격, 알고리즘 파라미터를 한곳에서 관리한다.
브로커와 DB 경로는 환경변수로 덮어쓸 수 있다.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# KST 는 서머타임이 없으므로 고정 오프셋 사용 (Windows 에 tzdata 가 없어도 동작)
KST = timezone(timedelta(hours=9), "KST")


def now_kst() -> datetime:
    return datetime.now(KST)


# --- MQTT 브로커 (내부 서버) ---------------------------------------------
BROKER_HOST = os.getenv("LOCKER_BROKER_HOST", "localhost")
BROKER_PORT = int(os.getenv("LOCKER_BROKER_PORT", "1883"))
BROKER_KEEPALIVE = 30
BROKER_USERNAME = os.getenv("LOCKER_BROKER_USER") or None
BROKER_PASSWORD = os.getenv("LOCKER_BROKER_PASS") or None
QOS = 1

# --- 저장소 --------------------------------------------------------------
DB_PATH = Path(os.getenv("LOCKER_DB_PATH", str(BASE_DIR / "data" / "locker.db")))

# --- 토픽 ----------------------------------------------------------------
# 키오스크 → 서버
TOPIC_REGISTER = "locker/input/register"
TOPIC_PORT_CONFIRM = "locker/input/port_confirm"
TOPIC_INPUT_STATS = "locker/input/stats"
TOPIC_LOGIN = "locker/auth/login"
TOPIC_LOGOUT = "locker/auth/logout"
TOPIC_PARCELS = "locker/output/parcels"
TOPIC_RETRIEVE = "locker/output/retrieve"
# 세대 웹(end_user_wep) / tools/set_mode.py → 서버
TOPIC_DEFAULT_MODE = "locker/resident/default_mode"
TOPIC_ITEM_MODE = "locker/resident/item_mode"
TOPIC_RESIDENT_PARCELS = "locker/resident/parcels"
TOPIC_PICKUP_CODE = "locker/resident/pickup_code"
TOPIC_HIDE = "locker/resident/hide"
# 서버 → 클라이언트 전용 응답·알림
TOPIC_CLIENT_RES = "locker/client/{client_id}/res"
TOPIC_CLIENT_ONLINE = "locker/client/{client_id}/online"
# 서버 → 전체
TOPIC_SUMMARY = "locker/state/summary"
TOPIC_STORAGE_LOG = "locker/storage/log"
TOPIC_SERVER_ONLINE = "locker/server/online"
# 서버 ↔ FMS(로봇 시뮬레이터) — fms_adapter.py 에서만 사용
TOPIC_FMS_JOB = "locker/robot/job"
TOPIC_FMS_BUNDLE = "locker/robot/bundle"
TOPIC_FMS_EVENT = "locker/robot/event"
# FMS → 전체 (retained). 서버는 쓰지 않고 세대 웹의 로봇 현황이 구독한다
TOPIC_ROBOT_STATE = "locker/robot/{robot_id}/state"

# --- 단지 ----------------------------------------------------------------
VALID_DONGS = [101, 102, 103, 104, 105, 106]
FLOORS = 4                 # 1~4층
UNITS_PER_FLOOR = 12       # 01~12호  → 101호 ~ 412호
SEED_PASSWORD = "1234"     # 데모용 공통 초기 비밀번호 (해시로 저장)
BCRYPT_ROUNDS = 8          # 288개 계정 최초 seed 를 수 초 안에 끝내기 위한 값

# --- 크기 ----------------------------------------------------------------
SIZES = ("S", "M", "L")
SIZE_RANK = {"S": 1, "M": 2, "L": 3}
SIZE_WEIGHT = {"S": 5, "M": 10, "L": 20}     # 배달 가중치(kg)

# --- 로봇 트레이 ---------------------------------------------------------
# 직사각형 로봇 적재부를 둘로 나눈 고정 슬롯. 트레이 하나에 물품 하나.
# 트레이 치수·허용 중량·크기별 적합성은 미정이라 S/M/L 모두 한 트레이에 싣는다.
TRAYS = ("TRAY_A", "TRAY_B")
BUNDLE_SIZE = len(TRAYS)                     # 물품이 이 수만큼 모여야 새 배송을 출발한다
ROBOT_MAX_WEIGHT = 150                       # 정의 값 (현재 가중치로는 최대 40kg)

# --- 배달 묶음 / Job -----------------------------------------------------
AGING_LIMIT_SEC = 3 * 3600
DONG_ORDER = VALID_DONGS                     # 인접 동 판단 순서
MAX_ACTIVE_JOBS = 1                          # 동시에 진행할 Job 수 (= 로봇 대수)

# --- 수령 방식 -----------------------------------------------------------
DEFAULT_MODE = "ROBOT"
MAX_FAIL = 2                                 # 배달 실패 누적 시 직접 수령 전환

# --- 보관소 랙 (VirtualStorage) -----------------------------------------
# 통로 양쪽(A/B) 랙, 열 0 이 입출력구 쪽. 하단 L, 중단 M, 상단 S.
RACK_SIDES = ("A", "B")
RACK_COLUMNS = 24
RACK_LEVELS = {1: "L", 2: "M", 3: "M", 4: "S", 5: "S", 6: "S"}
BAND_COLUMNS = {"B1": range(0, 6), "B2": range(6, 17), "B3": range(17, 24)}
PORT_POSITION = (-1, 1)                      # 입력구·출력구 위치 (열, 단)
CRANE_VX = 1.0                               # 열/초
CRANE_VY = 0.5                               # 단/초
CRANE_T_PICK = 3.0                           # 집기/놓기 시간(초)
PLACE_ALPHA = 1.0                            # 출력구까지 시간
PLACE_BETA = 0.5                             # 같은 동 클러스터 중심까지 시간
PLACE_GAMMA = 5.0                            # 셀 크기 낭비

# --- 시간 ----------------------------------------------------------------
PORT_TIMEOUT_SEC = 60                        # 접수 후 투입 대기
SESSION_TTL_SEC = 600                        # 키오스크 로그인
RESIDENT_SESSION_TTL_SEC = 7 * 24 * 3600     # 세대 웹 로그인 (개인 기기)
RESIDENT_HISTORY_DAYS = 14                   # 세대 웹에 보여줄 완료 택배 기간
HEARTBEAT_SEC = 10

TRACKING_NO_RE = r"[A-Za-z0-9]{6,40}"


def parse_unit(dong: int, ho: int):
    """(동, 호수) 검증. 반환: (ok, floor, reason)"""
    if dong not in VALID_DONGS:
        return False, None, f"존재하지 않는 동입니다: {dong}동"
    floor, unit = divmod(ho, 100)
    if not (1 <= floor <= FLOORS and 1 <= unit <= UNITS_PER_FLOOR):
        return False, None, (f"유효하지 않은 호수입니다: {ho}호 "
                             f"(101~{FLOORS}{UNITS_PER_FLOOR:02d}호, 층 1~{FLOORS}, "
                             f"호 01~{UNITS_PER_FLOOR:02d})")
    return True, floor, ""


def all_units():
    """단지의 모든 (동, 호) — 계정 seed 용."""
    for dong in VALID_DONGS:
        for floor in range(1, FLOORS + 1):
            for unit in range(1, UNITS_PER_FLOOR + 1):
                yield dong, floor * 100 + unit

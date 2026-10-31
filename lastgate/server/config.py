"""LastGate 보관소 서버 설정.

브로커 주소, 토픽, 시간 규칙, 알고리즘 파라미터를 한곳에서 관리한다.
건물·호실·보관 공간 수 같은 현장 설정은 코드가 아니라 관리자 초기 설정(site)으로 DB 에 저장한다.
브로커와 DB 경로, 관리자 기본 비밀번호는 환경변수로 덮어쓸 수 있다.
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
TOPIC_PASSWORD = "locker/auth/password"
TOPIC_PARCELS = "locker/output/parcels"
TOPIC_RETRIEVE = "locker/output/retrieve"
# 세대 웹(end_user_wep) / tools/set_mode.py → 서버
TOPIC_DEFAULT_MODE = "locker/resident/default_mode"
TOPIC_ITEM_MODE = "locker/resident/item_mode"
TOPIC_RESIDENT_PARCELS = "locker/resident/parcels"
TOPIC_PICKUP_CODE = "locker/resident/pickup_code"
TOPIC_HIDE = "locker/resident/hide"
TOPIC_SLOTS = "locker/resident/slots"
TOPIC_RESERVE = "locker/resident/reserve"
TOPIC_RESERVE_CANCEL = "locker/resident/reserve_cancel"
TOPIC_NOTIFICATIONS = "locker/resident/notifications"
# 관리자 웹(admin_web) → 서버
TOPIC_ADMIN_LOGIN = "locker/admin/login"
TOPIC_ADMIN_LOGOUT = "locker/admin/logout"
TOPIC_ADMIN_PASSWORD = "locker/admin/password"
TOPIC_ADMIN_SITE = "locker/admin/site/get"
TOPIC_ADMIN_SITE_PREVIEW = "locker/admin/site/preview"
TOPIC_ADMIN_SITE_INIT = "locker/admin/site/init"
TOPIC_ADMIN_SITE_UPDATE = "locker/admin/site/update"
TOPIC_ADMIN_SITE_RESET = "locker/admin/site/reset"
TOPIC_ADMIN_ACCOUNTS = "locker/admin/accounts/list"
TOPIC_ADMIN_RESET_PASSWORD = "locker/admin/accounts/reset_password"
TOPIC_ADMIN_CHANGE_OCCUPANT = "locker/admin/accounts/change_occupant"
TOPIC_ADMIN_UNLOCK = "locker/admin/accounts/unlock"
TOPIC_ADMIN_STORAGE = "locker/admin/storage"
TOPIC_ADMIN_CODE_PICKUP = "locker/admin/code_pickup"
TOPIC_ADMIN_EVENTS = "locker/admin/events"
TOPIC_ADMIN_DEMO_SKIP = "locker/admin/demo/skip_wait"   # 시연 전용 (DEMO_MODE 일 때만)
# 서버 → 클라이언트 전용 응답·알림
TOPIC_CLIENT_RES = "locker/client/{client_id}/res"
TOPIC_CLIENT_ONLINE = "locker/client/{client_id}/online"
# 서버 → 전체
TOPIC_SITE_CONFIG = "locker/config/site"          # retained. 공개 현장 설정
TOPIC_SLOTS_UPDATE = "locker/resident/slots_update"  # 예약 칸 남은 자리 변동
TOPIC_SUMMARY = "locker/state/summary"
TOPIC_STORAGE_LOG = "locker/storage/log"
TOPIC_SERVER_ONLINE = "locker/server/online"
# 서버 ↔ FMS(로봇 시뮬레이터) — fms_adapter.py 에서만 사용
TOPIC_FMS_JOB = "locker/robot/job"
TOPIC_FMS_BUNDLE = "locker/robot/bundle"
TOPIC_FMS_EVENT = "locker/robot/event"
# FMS → 전체 (retained). 서버는 쓰지 않고 세대 웹의 로봇 현황이 구독한다
TOPIC_ROBOT_STATE = "locker/robot/{robot_id}/state"

# --- 계정 ----------------------------------------------------------------
SEED_PASSWORD = "1234"     # 세대 계정 초기·초기화 비밀번호 (해시로 저장)
BCRYPT_ROUNDS = 8          # 수백 개 계정 생성을 수 초 안에 끝내기 위한 값
PASSWORD_RE = r"\d{4,8}"   # 출력 키오스크가 숫자 키패드라 숫자만 허용
LOGIN_MAX_FAIL = 5
LOGIN_LOCK_SEC = 5 * 60
ADMIN_ID = "admin"
ADMIN_ENV_PASSWORD = os.getenv("LASTGATE_ADMIN_PASSWORD") or None
ADMIN_DEFAULT_PASSWORD = "admin1234"
ADMIN_PASSWORD_RE = r"(?=.*[A-Za-z])(?=.*\d).{8,64}"
# 세대 웹 채널(긴 세션)과 관리자 요청은 이 client_id 로만 받는다.
# 운영 브로커 ACL 은 이 접두어를 각 서버 계정에만 허용한다 (lastgate/README.md 의 ACL 예시).
RESIDENT_CLIENT_PREFIX = "RESIDENT-WEB-"
ADMIN_CLIENT_PREFIX = "ADMIN-WEB-"

# --- 현장 설정 기본값 (관리자 초기 설정 화면의 기본값) ---------------------
SITE_DEFAULTS = {
    "grid_bins": 300,               # 그리드 보관함 수 (보관함 1개 = 물품 1개)
    "oversize_doors": 20,           # 배송 불가 보관함 칸 수
    "robots": 1,                    # 로봇 대수 = 동시에 진행할 배송 수
    "service_start": "08:00",       # 배송 시간 예약 가능 시간대
    "service_end": "22:00",
    "second_seat_same_building": False,   # 예약 칸의 두 번째 자리는 첫 자리와 같은 건물만
}
MAX_UNITS = 20000                   # init 한 번에 만들 수 있는 계정 수 상한

# --- 보관 공간 -----------------------------------------------------------
GRID_LIMIT_MM = (600, 400, 220)     # 그리드 보관함 안쪽 치수. 넘으면 배송 불가 보관함
GRID_PORT = "GRID"                  # 그리드 입력구·출력구

# --- 로봇 ----------------------------------------------------------------
# 로봇 위에 그리드 보관함 2개를 싣는다. 트레이 이름은 FMS 와 맞춘 고정 자리 이름이다.
TRAYS = ("TRAY_A", "TRAY_B")
BUNDLE_MAX = len(TRAYS)
BUNDLE_MIN = 2                      # 로봇은 물품이 2개 모여야 출발한다. 1개면 짝을 기다린다
AGING_LIMIT_SEC = 3 * 3600

# --- 수령 방식·배송 시간 예약 --------------------------------------------
DEFAULT_MODE = "ROBOT"
MAX_FAIL = 2                        # 배달 실패 누적 시 직접 수령 고정
CHOICE_WINDOW_SEC = 30 * 60         # 보관 후 배송 시간 예약 가능 시간
CHOICE_REMINDER_SEC = 10 * 60       # 예약 마감 몇 초 전에 알릴지
SLOT_MINUTES = 10
SLOT_SEATS = 2                      # 칸당 자리 = 로봇 1회 운행에 싣는 보관함 수
SLOT_LEAD_SEC = 20 * 60             # 지금부터 이 시간 뒤의 칸부터 예약. 변경·취소 마감도 칸 시작 20분 전
DISPATCH_LEAD_SEC = 15 * 60         # 예약 칸 시작 15분 전에 배송 발행
SLOT_HORIZON_SEC = 24 * 3600
SLOT_PROTECT_SEC = 30 * 60          # 이 시간 안에 출발할 예약이 있으면 마지막 로봇에 자동 배송을 맡기지 않음

# --- 대회 시연 ---------------------------------------------------------------
# 켜면 관리자 웹 '대기 건너뛰기'로 예약 가능 시간(30분)·예약 칸 발행 대기(칸 시작 15분 전)를 바로 끝낼 수 있다.
# 운영에서는 끈다. 2개가 모여야 출발하는 규칙과 로봇 대수 제한은 시연 중에도 그대로다.
DEMO_MODE = os.getenv("LASTGATE_DEMO", "0").strip().lower() in ("1", "true", "yes", "on")

# --- 시간 ----------------------------------------------------------------
PORT_TIMEOUT_SEC = 60                        # 접수 후 투입 대기
SESSION_TTL_SEC = 600                        # 키오스크 로그인
RESIDENT_SESSION_TTL_SEC = 7 * 24 * 3600     # 세대 웹 로그인 (개인 기기)
ADMIN_SESSION_TTL_SEC = 30 * 60              # 관리자 웹
RESIDENT_HISTORY_DAYS = 14                   # 세대 웹에 보여줄 완료 택배 기간
NOTIFICATION_KEEP = 50                       # 세대 웹 알림 목록 길이
HEARTBEAT_SEC = 10

TRACKING_NO_RE = r"[A-Za-z0-9]{6,40}"

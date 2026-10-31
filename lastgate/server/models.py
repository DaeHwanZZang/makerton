"""보관소 도메인 모델."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Status(str, Enum):
    REGISTERED = "REGISTERED"      # 접수됨, 입력구 투입 대기 (보관 공간은 이미 잡혀 있음)
    STORED = "STORED"              # 보관 중
    JOB_ASSIGNED = "JOB_ASSIGNED"  # 로봇 배송 발행됨 (로봇 도착 전, 키오스크 수령 가능)
    LOADED = "LOADED"              # 로봇에 적재 확정·배달 중
    DELIVERED = "DELIVERED"
    PICKED = "PICKED"              # 출력 키오스크·관리자 코드로 직접 수령
    CANCELLED = "CANCELLED"        # 투입 시간 초과 등

    def __str__(self) -> str:
        return self.value


class Mode(str, Enum):
    ROBOT = "ROBOT"
    DIRECT = "DIRECT"

    def __str__(self) -> str:
        return self.value


class Plan(str, Enum):
    """로봇 배송 물품의 배송 시간 계획."""
    CHOICE_OPEN = "CHOICE_OPEN"    # 보관 후 30분: 세대가 배송 시간을 예약할 수 있음. 출발하지 않음
    RESERVED = "RESERVED"          # 배송 시간 예약됨 (slot_start)
    AUTO = "AUTO"                  # 자동 배송 확정. 세대 웹에서 바꿀 수 없음

    def __str__(self) -> str:
        return self.value


class Area(str, Enum):
    GRID = "GRID"                  # 그리드 보관함 (로봇 배송 가능)
    OVERSIZE = "OVERSIZE"          # 배송 불가 보관함 (규격 초과, 문 번호로 수령)

    def __str__(self) -> str:
        return self.value


class DirectLock(str, Enum):
    """직접 수령 고정 사유. 세대 기본값·물품별 변경이 건너뛴다."""
    FRAGILE = "FRAGILE"
    OVERSIZE = "OVERSIZE"
    FAILED = "FAILED"              # 배달 실패 누적

    def __str__(self) -> str:
        return self.value


ACTIVE_STATUSES = (Status.REGISTERED, Status.STORED, Status.JOB_ASSIGNED, Status.LOADED)
PICKABLE_STATUSES = (Status.STORED, Status.JOB_ASSIGNED)
MODE_CHANGEABLE_STATUSES = (Status.REGISTERED, Status.STORED, Status.JOB_ASSIGNED)
COMPLETED_STATUSES = (Status.DELIVERED, Status.PICKED)


@dataclass
class Item:
    receipt_no: str
    building: int                      # 건물 번호 (FMS 메시지의 dong)
    unit: int                          # 호실 번호 (FMS 메시지의 ho)
    mode: str
    status: str
    pickup_code: str
    registered_at: str
    storage_area: str = Area.GRID.value
    space_id: str | None = None        # G-001 / OV-03. 로봇에 실린 보관함은 JOB_DONE 까지 유지
    fragile: int = 0
    direct_lock: str | None = None
    plan: str | None = None
    choice_deadline: str | None = None # 배송 시간 예약 마감 (보관 + 30분)
    slot_start: str | None = None      # 예약한 배송 시간 칸 시작
    auto_at: str | None = None         # 자동 배송 확정 시각 (짝 대기 기준)
    notified: str = ""                 # 보낸 알림 표시 (REMIND, DELAY …)
    tracking_no: str | None = None
    input_client: str | None = None
    input_kiosk: str | None = None     # 접수한 입력 키오스크 번호 (예: INPUT-01)
    hidden_at: str | None = None       # 세대 웹 목록에서 숨긴 시각 (완료 택배만)
    job_id: str | None = None
    robot_id: str | None = None
    fail_count: int = 0
    stored_at: str | None = None
    updated_at: str | None = None

    @property
    def floor(self) -> int:
        return self.unit // 100

    @property
    def household(self) -> tuple[int, int]:
        return self.building, self.unit

    def wait_seconds(self, now: datetime, since: str | None = None) -> float:
        base = datetime.fromisoformat(since or self.stored_at or self.registered_at)
        return max(0.0, (now - base).total_seconds())

    def has_note(self, key: str) -> bool:
        return key in self.notified.split(",")

    def add_note(self, key: str):
        if not self.has_note(key):
            self.notified = ",".join(filter(None, [self.notified, key]))


@dataclass
class Bundle:
    """로봇 1회 배달 묶음. 물품 1~2개를 두 트레이에 하나씩 싣는다."""
    items: list[Item]
    trays: dict[str, str]                            # receipt_no -> tray_id (TRAY_A/TRAY_B)
    metrics: dict = field(default_factory=dict)
    slot_start: str | None = None                    # 예약 배송 묶음이면 칸 시작

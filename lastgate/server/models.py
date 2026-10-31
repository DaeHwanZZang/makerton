"""보관소 도메인 모델."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

import config


class Status(str, Enum):
    REGISTERED = "REGISTERED"      # 접수됨, 입력구 투입 대기
    STORED = "STORED"              # 보관 중
    JOB_ASSIGNED = "JOB_ASSIGNED"  # 로봇 Job 후보 (로봇 도착 전, 키오스크 수령 가능)
    LOADED = "LOADED"              # 로봇에 적재 확정·배달 중
    DELIVERED = "DELIVERED"
    PICKED = "PICKED"              # 출력 키오스크에서 직접 수령
    CANCELLED = "CANCELLED"        # 투입 시간 초과 등

    def __str__(self) -> str:
        return self.value


class Mode(str, Enum):
    ROBOT = "ROBOT"
    DIRECT = "DIRECT"

    def __str__(self) -> str:
        return self.value


ACTIVE_STATUSES = (Status.REGISTERED, Status.STORED, Status.JOB_ASSIGNED, Status.LOADED)
PICKABLE_STATUSES = (Status.STORED, Status.JOB_ASSIGNED)
MODE_CHANGEABLE_STATUSES = (Status.REGISTERED, Status.STORED, Status.JOB_ASSIGNED)
COMPLETED_STATUSES = (Status.DELIVERED, Status.PICKED)


@dataclass
class Item:
    receipt_no: str
    dong: int
    ho: int
    size: str
    mode: str
    status: str
    pickup_code: str
    registered_at: str
    tracking_no: str | None = None
    input_client: str | None = None
    input_kiosk: str | None = None     # 접수한 입력 키오스크 번호 (예: INPUT-01)
    hidden_at: str | None = None       # 세대 웹 목록에서 숨긴 시각 (완료 택배만)
    slot_id: str | None = None
    job_id: str | None = None
    fail_count: int = 0
    stored_at: str | None = None
    updated_at: str | None = None

    @property
    def floor(self) -> int:
        return self.ho // 100

    @property
    def household(self) -> tuple[int, int]:
        return self.dong, self.ho

    @property
    def weight(self) -> int:
        return config.SIZE_WEIGHT[self.size]

    def wait_seconds(self, now: datetime) -> float:
        base = datetime.fromisoformat(self.stored_at or self.registered_at)
        return max(0.0, (now - base).total_seconds())


@dataclass
class Slot:
    slot_id: str
    side: str
    col: int
    level: int
    size: str
    band: str
    receipt_no: str | None = None
    dong: int | None = None

    @property
    def occupied(self) -> bool:
        return self.receipt_no is not None


@dataclass
class Bundle:
    """로봇 1회 배달 묶음. 물품 2개를 두 트레이에 하나씩 싣는다."""
    items: list[Item]
    trays: dict[str, str]                            # receipt_no -> tray_id (TRAY_A/TRAY_B)
    metrics: dict = field(default_factory=dict)

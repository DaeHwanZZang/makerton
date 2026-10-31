"""배송 시간 예약 칸 계산 (순수 함수).

칸은 10분 단위(:00, :10 …)이고 칸마다 2자리다. 예약은 지금부터 20분 뒤의 칸부터
24시간 안, 배송 시간대(관리자 설정) 안에서만 받는다. 칸 시작 15분 전에 배송을 발행하고,
변경·취소는 칸 시작 20분 전까지 받는다.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import config


def floor_slot(dt: datetime) -> datetime:
    dt = dt.astimezone(config.KST).replace(second=0, microsecond=0)
    return dt - timedelta(minutes=dt.minute % config.SLOT_MINUTES)


def ceil_slot(dt: datetime) -> datetime:
    base = floor_slot(dt)
    return base if base == dt.astimezone(config.KST) else base + timedelta(minutes=config.SLOT_MINUTES)


def slot_end(start: datetime) -> datetime:
    return start + timedelta(minutes=config.SLOT_MINUTES)


def cutoff(start: datetime) -> datetime:
    """변경·취소 마감 = 칸 시작 20분 전."""
    return start - timedelta(seconds=config.SLOT_LEAD_SEC)


def dispatch_at(start: datetime) -> datetime:
    return start - timedelta(seconds=config.DISPATCH_LEAD_SEC)


def in_service(start: datetime, service_start: str, service_end: str) -> bool:
    begin = start.strftime("%H:%M")
    end = slot_end(start).strftime("%H:%M")
    if end == "00:00":
        end = "24:00"
    return service_start <= begin and end <= service_end


def bookable_starts(now: datetime, service_start: str, service_end: str) -> list[datetime]:
    first = ceil_slot(now + timedelta(seconds=config.SLOT_LEAD_SEC))
    last = now + timedelta(seconds=config.SLOT_HORIZON_SEC)
    starts, t = [], first
    while t < last:
        if in_service(t, service_start, service_end):
            starts.append(t)
        t += timedelta(minutes=config.SLOT_MINUTES)
    return starts


def parse_start(value) -> datetime | None:
    """클라이언트가 보낸 칸 시작(ISO) → KST datetime. 10분 경계가 아니면 None."""
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=config.KST)
    dt = dt.astimezone(config.KST)
    return dt if floor_slot(dt) == dt else None


def label(start: datetime) -> str:
    return f"{start:%H:%M}~{slot_end(start):%H:%M}"

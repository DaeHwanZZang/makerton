"""로봇 배달 묶음 구성 — 두 트레이에 그리드 보관함 하나씩, 1~2개.

자동 배송(AUTO)
  - 기준 물품: 오래 기다린(aged) 물품 중 가장 오래된 것, 없으면 자동 배송 확정이 가장 이른 물품.
  - 짝 물품: 같은 세대 → 같은 건물·층 → 같은 건물(가까운 층) → 표시 순서가 가까운 건물.
  - 짝이 없으면 기준 물품이 확정 후 PAIR_WAIT_SEC(20분) 기다린 뒤 1개로 출발한다.
예약 배송(RESERVED)
  - 같은 칸의 물품(최대 2개, 1~2세대)을 함께 싣는다.
  - 1자리만 찼으면 같은 건물의 자동 배송 물품 하나를 더 실을 수 있다. 예약 세대를 먼저 들른다.
트레이 배정은 방문 순서대로 TRAY_A, TRAY_B. 트레이 ID 는 로봇 안 고정 슬롯 이름이다.
"""
from __future__ import annotations

from datetime import datetime

import config
from models import Bundle, Item


def _since(item: Item) -> str | None:
    return item.auto_at or item.stored_at


def _aged(item: Item, now: datetime) -> bool:
    return item.wait_seconds(now) >= config.AGING_LIMIT_SEC


def tier(anchor: Item, other: Item, order: dict[int, int]) -> tuple[int, int]:
    if other.household == anchor.household:
        return 0, 0
    if other.building == anchor.building and other.floor == anchor.floor:
        return 1, 0
    if other.building == anchor.building:
        return 2, abs(other.floor - anchor.floor)
    return 3, abs(order.get(other.building, 999) - order.get(anchor.building, 999))


def _partner(anchor: Item, pool: list[Item], now: datetime, order: dict[int, int]) -> Item | None:
    rest = [it for it in pool if it.receipt_no != anchor.receipt_no]
    if not rest:
        return None
    return min(rest, key=lambda it: (tier(anchor, it, order), 0 if _aged(it, now) else 1,
                                     -it.wait_seconds(now), it.receipt_no))


def build_auto_bundle(pool: list[Item], now: datetime, order: dict[int, int]) -> Bundle | None:
    """자동 배송 대상(pool)으로 1회 묶음. 짝이 있으면 2개, 없으면 대기 시간이 지나야 1개."""
    if not pool:
        return None
    aged = [it for it in pool if _aged(it, now)]
    anchor = min(aged or pool, key=lambda it: (-it.wait_seconds(now, _since(it)), it.receipt_no))
    partner = _partner(anchor, pool, now, order)
    if partner:
        items = [anchor, partner]
    elif anchor.wait_seconds(now, _since(anchor)) >= config.PAIR_WAIT_SEC:
        items = [anchor]
    else:
        return None
    ordered = visit_order(items, order)
    return Bundle(items=ordered, trays=assign_trays(ordered), metrics=bundle_metrics(ordered, now, "AUTO"))


def build_slot_bundle(reserved: list[Item], fillers: list[Item], now: datetime,
                      order: dict[int, int], slot_start: str) -> Bundle | None:
    """예약 칸 묶음. reserved 는 같은 칸 물품(1~2개), fillers 는 자동 배송 대기 물품."""
    if not reserved:
        return None
    reserved = reserved[:config.BUNDLE_MAX]
    if len(reserved) == 1:
        anchor = reserved[0]
        same = [it for it in fillers if it.building == anchor.building]
        filler = _partner(anchor, same, now, order)
        ordered = [anchor, filler] if filler else [anchor]      # 예약 세대를 먼저 들른다
    else:
        ordered = visit_order(reserved, order)
    metrics = bundle_metrics(ordered, now, "RESERVED")
    metrics["filled"] = len(ordered) - len(reserved)
    return Bundle(items=ordered, trays=assign_trays(ordered), metrics=metrics, slot_start=slot_start)


def visit_order(items: list[Item], order: dict[int, int]) -> list[Item]:
    """건물 표시 순서 → 층 → 호실 순으로 방문한다."""
    return sorted(items, key=lambda i: (order.get(i.building, 999), i.floor, i.unit, i.receipt_no))


def assign_trays(items: list[Item]) -> dict[str, str]:
    """방문 순서대로 물품 하나당 트레이 하나. 트레이 수보다 많으면 오류."""
    if len(items) > len(config.TRAYS):
        raise ValueError(f"트레이는 {len(config.TRAYS)}개뿐입니다: 물품 {len(items)}개")
    return {it.receipt_no: tray for it, tray in zip(items, config.TRAYS)}


def bundle_metrics(items: list[Item], now: datetime, kind: str) -> dict:
    return {
        "kind": kind,
        "items": len(items),
        "trays": len(config.TRAYS),
        "buildings": sorted({it.building for it in items}),
        "households": len({it.household for it in items}),
        "floors": len({(it.building, it.floor) for it in items}),
        "max_wait_sec": round(max((it.wait_seconds(now) for it in items), default=0)),
    }

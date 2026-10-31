"""로봇 배달 묶음 구성 — 두 트레이에 물품 하나씩, 총 2개.

규칙
  - 로봇 배달 대상 물품이 BUNDLE_SIZE(2)개 미만이면 묶음을 만들지 않는다 (보관소에서 대기).
  - 기준 물품: 오래 기다린(aged) 물품 중 가장 오래된 것, 없으면 가장 오래 기다린 물품.
  - 짝 물품: 같은 세대 → 같은 동·층 → 같은 동 → 나머지 순. 같은 단계면 aged → 오래 기다린 순.
  - 트레이 배정은 동·호·접수번호 순으로 TRAY_A, TRAY_B. 트레이 ID 는 로봇 안 고정 슬롯 이름이다.

크기(S/M/L)는 트레이 적합성 기준이 정해지지 않아 묶음 구성에 쓰지 않는다. 무게 상한만 본다.
"""
from __future__ import annotations

from datetime import datetime

import config
from models import Bundle, Item


def _aged(item: Item, now: datetime) -> bool:
    return item.wait_seconds(now) >= config.AGING_LIMIT_SEC


def _anchor(pool: list[Item], now: datetime) -> Item:
    aged = [it for it in pool if _aged(it, now)]
    candidates = aged or pool
    # 가장 오래 기다린 물품. 동률이면 접수번호가 작은 쪽 (결정적)
    return min(candidates, key=lambda it: (-it.wait_seconds(now), it.receipt_no))


def _tier(anchor: Item, other: Item) -> int:
    if other.household == anchor.household:
        return 0
    if (other.dong, other.floor) == (anchor.dong, anchor.floor):
        return 1
    if other.dong == anchor.dong:
        return 2
    return 3


def build_bundle(pool: list[Item], now: datetime) -> Bundle | None:
    """로봇 배달 대상(pool)으로 1회 배달 묶음을 만든다. BUNDLE_SIZE 개를 못 채우면 None."""
    if len(pool) < config.BUNDLE_SIZE:
        return None
    chosen = [_anchor(pool, now)]
    while len(chosen) < config.BUNDLE_SIZE:
        weight = sum(it.weight for it in chosen)
        rest = [it for it in pool if it not in chosen
                and weight + it.weight <= config.ROBOT_MAX_WEIGHT]
        if not rest:
            return None
        anchor = chosen[0]
        chosen.append(min(rest, key=lambda it: (_tier(anchor, it), 0 if _aged(it, now) else 1,
                                                -it.wait_seconds(now), it.receipt_no)))
    return Bundle(items=chosen, trays=assign_trays(chosen), metrics=bundle_metrics(chosen, now))


def assign_trays(items: list[Item]) -> dict[str, str]:
    """물품 하나당 트레이 하나. 트레이 수보다 많으면 오류."""
    if len(items) > len(config.TRAYS):
        raise ValueError(f"트레이는 {len(config.TRAYS)}개뿐입니다: 물품 {len(items)}개")
    ordered = sorted(items, key=lambda i: (i.dong, i.ho, i.receipt_no))
    return {it.receipt_no: tray for it, tray in zip(ordered, config.TRAYS)}


def bundle_metrics(items: list[Item], now: datetime) -> dict:
    return {
        "count": {s: sum(1 for it in items if it.size == s) for s in config.SIZES},
        "items": len(items),
        "trays": len(config.TRAYS),
        "weight_kg": sum(it.weight for it in items),
        "dongs": sorted({it.dong for it in items}),
        "households": len({it.household for it in items}),
        "floors": len({(it.dong, it.floor) for it in items}),
        "max_wait_sec": round(max((it.wait_seconds(now) for it in items), default=0)),
    }

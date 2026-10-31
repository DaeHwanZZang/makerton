"""보관 공간 배정 — 그리드 보관함과 배송 불가 보관함.

  · 그리드 보관함(G-001…): 보관함 1개에 한 세대의 물품 1개. 로봇은 보관함째 트레이에 싣고,
    빈 보관함이 돌아오기 전(JOB_DONE)까지는 사용 중으로 센다.
  · 배송 불가 보관함(OV-01…): 규격(600×400×220mm) 초과 물품. 문 번호로 직접 수령만 가능.

점유 상태의 원본은 DB 물품의 space_id 다. 접수 시 칸을 잡고, 수령·취소·빈 보관함 반납 시 푼다.
실제 그리드 보관소(크레인·셔틀)를 붙일 때는 allocate()/log() 자리에서 장비 명령을 보내면 된다.
"""
from __future__ import annotations

from typing import Callable

from models import Area, Item

PREFIX = {Area.GRID.value: "G-", Area.OVERSIZE.value: "OV-"}
WIDTH = {Area.GRID.value: 3, Area.OVERSIZE.value: 2}


def space_name(area: str, index: int) -> str:
    return f"{PREFIX[area]}{index:0{WIDTH[area]}d}"


def space_index(space_id: str | None) -> int | None:
    try:
        return int(str(space_id).split("-", 1)[1])
    except (IndexError, ValueError):
        return None


class SpaceStorage:
    def __init__(self, store, on_log: Callable[[str, dict], None] | None = None):
        self.store = store
        self.on_log = on_log or (lambda kind, data: None)

    def free_count(self, area: str, total: int) -> int:
        return max(0, total - len(self.store.used_spaces(area)))

    def allocate(self, area: str, total: int) -> str | None:
        """비어 있는 가장 작은 번호의 칸. 없으면 None."""
        used = {space_index(s) for s in self.store.used_spaces(area)}
        for index in range(1, total + 1):
            if index not in used:
                return space_name(area, index)
        return None

    def max_used_index(self, area: str) -> int:
        return max((space_index(s) or 0 for s in self.store.used_spaces(area)), default=0)

    def occupancy(self, grid_total: int, oversize_total: int) -> dict:
        grid_used = len(self.store.used_spaces(Area.GRID.value))
        oversize_used = len(self.store.used_spaces(Area.OVERSIZE.value))
        return {
            "grid": {"used": grid_used, "on_robot": self.store.count_on_robot(), "total": grid_total,
                     "ratio": round(grid_used / grid_total, 3) if grid_total else 0},
            "oversize": {"used": oversize_used, "total": oversize_total,
                         "ratio": round(oversize_used / oversize_total, 3) if oversize_total else 0},
        }

    def log(self, kind: str, item: Item, **data):
        self.on_log(kind, {"receipt_no": item.receipt_no, "area": item.storage_area,
                           "space_id": item.space_id, **data})

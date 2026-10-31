"""보관소 어댑터와 가상 보관소.

StorageAdapter 는 서버가 보관소에 요구하는 동작이다. 지금은 하드웨어나 보관소
시뮬레이터가 없으므로 VirtualStorage 가 셀 배치와 크레인 동작을 서버 안에서 바로
완료 처리한다. 실제 보관소를 붙일 때는 같은 인터페이스의 다른 구현으로 바꾸면 된다.

배치 원칙(설계 5장): 곧 꺼낼 물품은 출력구 가까이, 같은 동 물품은 붙여서.
  · 띠: ROBOT 모드 → B1(Hot), DIRECT 모드 → B2(Warm), 넘치면 바깥 띠
  · 셀 점수: α·T(셀, 출력구) + β·T(셀, 같은 동 클러스터 중심) + γ·크기 낭비
  · T 는 크레인 수평·수직 동시 이동을 가정한 체비셰프 시간
"""
from __future__ import annotations

from typing import Callable, Iterable, Protocol

import config
from models import Item, Mode, Slot

BAND_FALLBACK = {"B1": ["B1", "B2", "B3"], "B2": ["B2", "B3", "B1"], "B3": ["B3", "B2", "B1"]}


class StorageAdapter(Protocol):
    def restore(self, items: Iterable[Item]) -> None: ...
    def can_accept(self, pending_sizes: list[str]) -> bool: ...
    def place(self, item: Item) -> str | None: ...
    def retrieve(self, item: Item, port: str) -> None: ...
    def load_robot(self, items: list[Item]) -> None: ...
    def reslot(self, item: Item) -> str | None: ...
    def occupancy(self) -> dict: ...


def crane_time(a: tuple[float, float], b: tuple[float, float]) -> float:
    return max(abs(a[0] - b[0]) / config.CRANE_VX,
               abs(a[1] - b[1]) / config.CRANE_VY) + config.CRANE_T_PICK


class VirtualStorage:
    def __init__(self, on_log: Callable[[str, dict], None] | None = None):
        self.on_log = on_log or (lambda kind, data: None)
        self.slots: dict[str, Slot] = {}
        for side in config.RACK_SIDES:
            for col in range(config.RACK_COLUMNS):
                band = next(b for b, cols in config.BAND_COLUMNS.items() if col in cols)
                for level, size in config.RACK_LEVELS.items():
                    slot_id = f"{side}-C{col:02d}-L{level}"
                    self.slots[slot_id] = Slot(slot_id, side, col, level, size, band)

    # --- 상태 복원 -------------------------------------------------------
    def restore(self, items: Iterable[Item]) -> None:
        """DB 의 보관 중 물품으로 셀 점유 상태를 다시 만든다."""
        for slot in self.slots.values():
            self._free(slot)
        for item in items:
            slot = self.slots.get(item.slot_id or "")
            if slot:
                slot.receipt_no, slot.dong = item.receipt_no, item.dong

    # --- 용량 ------------------------------------------------------------
    def can_accept(self, pending_sizes: list[str]) -> bool:
        """접수 대기 물품까지 모두 셀에 들어갈 수 있는지 (큰 물품부터 가장 작은 맞는 셀)."""
        free = sorted(config.SIZE_RANK[s.size] for s in self.slots.values() if not s.occupied)
        for rank in sorted((config.SIZE_RANK[s] for s in pending_sizes), reverse=True):
            fit = next((i for i, r in enumerate(free) if r >= rank), None)
            if fit is None:
                return False
            free.pop(fit)
        return True

    def occupancy(self) -> dict:
        result = {}
        for size in config.SIZES:
            cells = [s for s in self.slots.values() if s.size == size]
            used = sum(1 for s in cells if s.occupied)
            result[size] = {"used": used, "total": len(cells),
                            "ratio": round(used / len(cells), 3) if cells else 0}
        return result

    # --- 배치 ------------------------------------------------------------
    @staticmethod
    def target_band(item: Item) -> str:
        return "B1" if item.mode == Mode.ROBOT else "B2"

    def place(self, item: Item) -> str | None:
        slot = self._choose(item, BAND_FALLBACK[self.target_band(item)])
        if not slot:
            return None
        self._occupy(slot, item)
        self.on_log("STORE", {"receipt_no": item.receipt_no, "slot_id": slot.slot_id,
                              "band": slot.band,
                              "crane_sec": round(crane_time(config.PORT_POSITION, self._pos(slot)), 1)})
        return slot.slot_id

    def retrieve(self, item: Item, port: str) -> None:
        slot = self.slots.get(item.slot_id or "")
        if not slot:
            return
        self._free(slot)
        self.on_log("RETRIEVE", {"receipt_no": item.receipt_no, "slot_id": slot.slot_id,
                                 "port": port,
                                 "crane_sec": round(crane_time(self._pos(slot), config.PORT_POSITION), 1)})

    def load_robot(self, items: list[Item]) -> None:
        """로봇 출력구로 묶음 인출. 가까운 셀부터 꺼내 첫 물품이 빨리 준비되게 한다."""
        ordered = sorted(items, key=lambda it: crane_time(
            config.PORT_POSITION, self._pos(self.slots[it.slot_id])) if it.slot_id in self.slots else 0)
        for item in ordered:
            self.retrieve(item, "ROBOT")

    def reslot(self, item: Item) -> str | None:
        """수령 방식이 바뀐 물품을 목표 띠로 옮긴다 (가상 보관소라 즉시 완료)."""
        current = self.slots.get(item.slot_id or "")
        target = self.target_band(item)
        if not current or current.band == target:
            return None
        new = self._choose(item, [target])
        if not new:
            return None
        self._free(current)
        self._occupy(new, item)
        self.on_log("MOVE", {"receipt_no": item.receipt_no, "from": current.slot_id,
                             "to": new.slot_id,
                             "crane_sec": round(crane_time(self._pos(current), self._pos(new)), 1)})
        return new.slot_id

    # --- 내부 ------------------------------------------------------------
    @staticmethod
    def _pos(slot: Slot) -> tuple[int, int]:
        return slot.col, slot.level

    def _cluster_center(self, dong: int, band: str) -> tuple[float, float] | None:
        same = [self._pos(s) for s in self.slots.values()
                if s.band == band and s.occupied and s.dong == dong]
        if not same:
            return None
        return sum(p[0] for p in same) / len(same), sum(p[1] for p in same) / len(same)

    def _choose(self, item: Item, bands: list[str]) -> Slot | None:
        want = config.SIZE_RANK[item.size]
        for band in bands:
            cands = [s for s in self.slots.values()
                     if s.band == band and not s.occupied and config.SIZE_RANK[s.size] >= want]
            if not cands:
                continue
            center = self._cluster_center(item.dong, band)

            def cost(s: Slot) -> float:
                c = config.PLACE_ALPHA * crane_time(self._pos(s), config.PORT_POSITION)
                if center:
                    c += config.PLACE_BETA * crane_time(self._pos(s), center)
                c += config.PLACE_GAMMA * (config.SIZE_RANK[s.size] - want)
                return c

            return min(cands, key=lambda s: (cost(s), s.slot_id))
        return None

    @staticmethod
    def _occupy(slot: Slot, item: Item):
        slot.receipt_no, slot.dong = item.receipt_no, item.dong

    @staticmethod
    def _free(slot: Slot):
        slot.receipt_no, slot.dong = None, None

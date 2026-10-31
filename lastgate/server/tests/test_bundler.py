"""배달 묶음 구성 테스트.  실행: server 폴더에서 python -m unittest discover tests"""
import random
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from bundler import build_bundle  # noqa: E402
from models import Item  # noqa: E402

NOW = datetime(2026, 10, 31, 15, 0, tzinfo=config.KST)
_seq = iter(range(1, 1_000_000))


def item(dong, ho, size, wait_min=10):
    n = next(_seq)
    at = (NOW - timedelta(minutes=wait_min)).isoformat()
    return Item(receipt_no=f"20261031-{n:08X}", dong=dong, ho=ho, size=size, mode="ROBOT",
                status="STORED", pickup_code=f"{n:06d}", registered_at=at, stored_at=at)


class BundleTests(unittest.TestCase):
    def assertValid(self, bundle):
        self.assertEqual(len(bundle.items), config.BUNDLE_SIZE)
        self.assertEqual(len({it.receipt_no for it in bundle.items}), config.BUNDLE_SIZE)
        self.assertEqual(sorted(bundle.trays.values()), sorted(config.TRAYS))
        self.assertEqual(set(bundle.trays), {it.receipt_no for it in bundle.items})
        self.assertLessEqual(sum(it.weight for it in bundle.items), config.ROBOT_MAX_WEIGHT)
        self.assertEqual(bundle.metrics["items"], config.BUNDLE_SIZE)

    def test_empty_pool(self):
        self.assertIsNone(build_bundle([], NOW))

    def test_single_item_waits(self):
        self.assertIsNone(build_bundle([item(101, 101, "L")], NOW))

    def test_two_items_take_one_tray_each(self):
        a, b = item(101, 305, "L"), item(101, 203, "S")
        bundle = build_bundle([a, b], NOW)
        self.assertValid(bundle)
        # 동·호 순으로 TRAY_A, TRAY_B
        self.assertEqual(bundle.trays, {b.receipt_no: "TRAY_A", a.receipt_no: "TRAY_B"})

    def test_bundle_never_exceeds_two(self):
        pool = [item(101, 101, "S") for _ in range(10)]
        self.assertValid(build_bundle(pool, NOW))

    def test_oldest_item_is_anchor(self):
        old = item(103, 412, "M", wait_min=60)
        pool = [item(101, 101, "S", wait_min=5), item(101, 102, "S", wait_min=6), old]
        self.assertIn(old, build_bundle(pool, NOW).items)

    def test_aged_item_beats_household_grouping(self):
        aged = item(105, 412, "L", wait_min=4 * 60)
        pool = [item(101, 101, "S", wait_min=100), item(101, 101, "S", wait_min=90), aged]
        self.assertIn(aged, build_bundle(pool, NOW).items)

    def test_partner_prefers_same_household_then_floor_then_dong(self):
        anchor = item(101, 301, "S", wait_min=100)
        other_dong = item(102, 301, "S", wait_min=99)
        same_dong = item(101, 101, "S", wait_min=50)
        same_floor = item(101, 302, "S", wait_min=40)
        same_household = item(101, 301, "S", wait_min=30)
        pool = [anchor, other_dong, same_dong, same_floor, same_household]
        def chosen():
            return {it.receipt_no for it in build_bundle(pool, NOW).items}
        self.assertEqual(chosen(), {anchor.receipt_no, same_household.receipt_no})
        pool.remove(same_household)
        self.assertEqual(chosen(), {anchor.receipt_no, same_floor.receipt_no})
        pool.remove(same_floor)
        self.assertEqual(chosen(), {anchor.receipt_no, same_dong.receipt_no})

    def test_random_pools_valid(self):
        rng = random.Random(7)
        for _ in range(300):
            pool = [item(rng.choice(config.VALID_DONGS), rng.randint(1, 4) * 100 + rng.randint(1, 12),
                         rng.choice(config.SIZES), wait_min=rng.randint(0, 300))
                    for _ in range(rng.randint(2, 40))]
            self.assertValid(build_bundle(pool, NOW))


if __name__ == "__main__":
    unittest.main()

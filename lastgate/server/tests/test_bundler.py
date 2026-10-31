"""묶음 구성·현장 설정 계산 단위 테스트."""
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import reservations as rsv  # noqa: E402
from bundler import build_auto_bundle, build_slot_bundle  # noqa: E402
from models import Item  # noqa: E402
from site_config import Building, Site, SiteError, parse_unit_list  # noqa: E402

NOW = datetime(2026, 10, 31, 14, 0, tzinfo=config.KST)
ORDER = {1: 0, 2: 1, 3: 2}


def item(receipt, building=1, unit=101, auto_minutes_ago=0):
    at = (NOW - timedelta(minutes=auto_minutes_ago)).isoformat()
    return Item(receipt_no=receipt, building=building, unit=unit, mode="ROBOT", status="STORED",
                pickup_code="000000", registered_at=at, stored_at=at, auto_at=at, plan="AUTO")


class AutoBundleTests(unittest.TestCase):
    def test_pair_tiers(self):
        anchor = item("A", 1, 301, 10)
        pool = [anchor, item("B", 3, 301, 5), item("C", 1, 501, 1), item("D", 1, 302, 1), item("E", 1, 301, 1)]
        self.assertEqual([i.receipt_no for i in build_auto_bundle(pool, NOW, ORDER).items[:2]].count("E"), 1)
        pool.remove(pool[4])          # 같은 세대 없음 → 같은 층
        self.assertIn("D", {i.receipt_no for i in build_auto_bundle(pool, NOW, ORDER).items})
        pool.remove(pool[3])          # 같은 층 없음 → 같은 건물
        self.assertIn("C", {i.receipt_no for i in build_auto_bundle(pool, NOW, ORDER).items})
        pool.remove(pool[2])          # 다른 건물
        self.assertIn("B", {i.receipt_no for i in build_auto_bundle(pool, NOW, ORDER).items})

    def test_nearest_building_by_order(self):
        pool = [item("A", 1, 101, 10), item("C", 3, 101, 5), item("B", 2, 101, 1)]
        self.assertEqual({i.receipt_no for i in build_auto_bundle(pool, NOW, ORDER).items}, {"A", "B"})

    def test_single_waits(self):
        self.assertIsNone(build_auto_bundle([item("A", auto_minutes_ago=19)], NOW, ORDER))
        bundle = build_auto_bundle([item("A", auto_minutes_ago=20)], NOW, ORDER)
        self.assertEqual(bundle.trays, {"A": "TRAY_A"})

    def test_visit_order_trays(self):
        bundle = build_auto_bundle([item("A", 2, 301, 10), item("B", 1, 501, 1)], NOW, ORDER)
        self.assertEqual(bundle.trays, {"B": "TRAY_A", "A": "TRAY_B"})


class SlotBundleTests(unittest.TestCase):
    def test_filler_same_building_reserved_first(self):
        reserved = [item("R", 1, 501)]
        fillers = [item("X", 2, 501, 30), item("Y", 1, 101, 5)]
        bundle = build_slot_bundle(reserved, fillers, NOW, ORDER, "slot")
        self.assertEqual([i.receipt_no for i in bundle.items], ["R", "Y"])
        self.assertEqual(bundle.metrics["filled"], 1)
        self.assertEqual(build_slot_bundle(reserved, [fillers[0]], NOW, ORDER, "slot").trays, {"R": "TRAY_A"})

    def test_two_households_building_floor_order(self):
        bundle = build_slot_bundle([item("H", 2, 101), item("L", 1, 601)], [], NOW, ORDER, "slot")
        self.assertEqual([i.receipt_no for i in bundle.items], ["L", "H"])


class SiteTests(unittest.TestCase):
    def test_units_and_guide(self):
        b = Building(code=1, name="공학관", unit_from=101, unit_to=614, missing="404, 501~514", extra="1501")
        units = b.units()
        self.assertEqual(len(units), 84 - 15 + 1)
        self.assertNotIn(404, units)
        self.assertIn(1501, units)
        self.assertEqual(b.guide("호"), "101~614호 · 없는 호실: 404호, 5층 전체 · 추가 호실: 1501호")
        b2 = Building(code=2, name="A", unit_from=101, unit_to=105, missing="102~103")
        self.assertEqual(b2.guide("호"), "101~105호 · 없는 호실: 102~103호")

    def test_parse_errors(self):
        self.assertEqual(parse_unit_list("404, 501~514", "x"), [(404, 404), (501, 514)])
        for bad in ("4a4", "514~501", "12"):
            with self.assertRaises(SiteError):
                parse_unit_list(bad, "x")

    def test_validation(self):
        base = {"name": "A", "building_term": "동", "unit_term": "호",
                "buildings": [{"code": 101, "name": "101동", "unit_from": 101, "unit_to": 412}]}
        self.assertEqual(Site.from_dict(base).account_count(), 48)
        for patch in ({"code": 0}, {"code": 1000}, {"unit_from": 100}, {"unit_from": 112, "unit_to": 401},
                      {"missing": "101~412"}, {"code": "1a"}):
            data = dict(base, buildings=[dict(base["buildings"][0], **patch)])
            with self.assertRaises(SiteError, msg=patch):
                Site.from_dict(data)
        with self.assertRaises(SiteError):
            Site.from_dict(dict(base, service_start="22:00", service_end="08:00"))


class SlotTimeTests(unittest.TestCase):
    def test_bookable(self):
        starts = rsv.bookable_starts(NOW.replace(minute=3), "08:00", "22:00")
        self.assertEqual(starts[0], NOW.replace(minute=30))
        self.assertTrue(all(s.minute % 10 == 0 for s in starts))
        self.assertEqual(rsv.label(starts[0]), "14:30~14:40")
        self.assertIsNone(rsv.parse_start("2026-10-31T14:35:00+09:00"))
        self.assertEqual(rsv.parse_start("2026-10-31T05:30:00+00:00"), NOW.replace(minute=30))


if __name__ == "__main__":
    unittest.main()

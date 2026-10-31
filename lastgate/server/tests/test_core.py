"""서버 핵심 흐름 테스트 (MQTT 없이 LockerCore 직접 호출)."""
import sqlite3
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from core import LockerCore  # noqa: E402
from store import Store  # noqa: E402

config.BCRYPT_ROUNDS = 4          # 테스트 속도용
T = config
ADMIN = "ADMIN-WEB-TEST"
RESIDENT = "RESIDENT-WEB-TEST"

SITE = {
    "name": "테스트대학교", "building_term": "관", "unit_term": "호",
    "buildings": [
        {"code": 1, "name": "공학관", "unit_from": 101, "unit_to": 614, "missing": "404, 501~514"},
        {"code": 2, "name": "인문관", "unit_from": 101, "unit_to": 303, "extra": "1501"},
    ],
    "grid_bins": 10, "oversize_doors": 2, "robots": 1,
    "service_start": "08:00", "service_end": "22:00",
}


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 31, 14, 0, tzinfo=config.KST)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class Base(unittest.TestCase):
    site = SITE
    initialize = True

    def setUp(self):
        self.clock = Clock()
        self.sent = []
        self.store = Store(":memory:")
        self.core = LockerCore(self.store, publish=lambda t, p, retain=False: self.sent.append((t, p)),
                               now_fn=self.clock)
        self.core.bootstrap()
        self.admin = None
        if self.initialize:
            self.admin = self.admin_login()
            res = self.req(T.TOPIC_ADMIN_SITE_INIT, client=ADMIN, session=self.admin, site=self.site)
            self.assertEqual(res["status"], "OK", res)

    # --- 헬퍼 ------------------------------------------------------------
    def req(self, topic, client="INPUT-01", request_id=None, **body):
        request_id = request_id or uuid.uuid4().hex
        self.core.handle(topic, {**body, "request_id": request_id, "client_id": client})
        res_topic = T.TOPIC_CLIENT_RES.format(client_id=client)
        return next(p for t, p in reversed(self.sent)
                    if t == res_topic and p.get("request_id") == request_id)

    def admin_login(self, password=None):
        res = self.req(T.TOPIC_ADMIN_LOGIN, client=ADMIN, password=password or T.ADMIN_DEFAULT_PASSWORD)
        self.assertEqual(res["status"], "OK", res)
        return res["session"]

    def published(self, topic, type_=None):
        return [p for t, p in self.sent if t == topic and (type_ is None or p.get("type") == type_)]

    def to_client(self, client, type_):
        return self.published(T.TOPIC_CLIENT_RES.format(client_id=client), type_)

    def fms(self, **payload):
        self.core.handle(T.TOPIC_FMS_EVENT, payload)

    def store_item(self, building=1, unit=101, area="GRID", fragile=False):
        res = self.req(T.TOPIC_REGISTER, building=building, unit=unit, area=area, fragile=fragile)
        self.assertEqual(res["status"], "OK", res)
        conf = self.req(T.TOPIC_PORT_CONFIRM, receipt_no=res["receipt_no"], port=area)
        self.assertEqual(conf["status"], "OK", conf)
        return res["receipt_no"]

    def login(self, building=1, unit=101, client="OUTPUT-01", password="1234", channel=None):
        body = {"building": building, "unit": unit, "password": password}
        if channel:
            body["channel"] = channel
        res = self.req(T.TOPIC_LOGIN, client=client, **body)
        self.assertEqual(res["status"], "OK", res)
        return res["session"]

    def item(self, receipt):
        return self.store.get_item(receipt)

    def jobs(self):
        """발행된 Job (heartbeat 재전송은 같은 job_id 라 하나로 센다)."""
        unique = {}
        for job in self.published(T.TOPIC_FMS_JOB, "DELIVERY_JOB"):
            unique.setdefault(job["job_id"], job)
        return list(unique.values())

    def tick(self, seconds):
        self.clock.advance(seconds)
        self.core.heartbeat()


class InitTests(Base):
    initialize = False

    def test_not_initialized(self):
        config_msg = self.published(T.TOPIC_SITE_CONFIG)[-1]
        self.assertFalse(config_msg["initialized"])
        self.assertEqual(self.req(T.TOPIC_REGISTER, building=1, unit=101)["status"], "NOT_INITIALIZED")
        self.assertEqual(self.req(T.TOPIC_LOGIN, building=1, unit=101, password="1234")["status"],
                         "NOT_INITIALIZED")

    def test_admin_login_and_lockout(self):
        res = self.req(T.TOPIC_ADMIN_LOGIN, client=ADMIN, password=T.ADMIN_DEFAULT_PASSWORD)
        self.assertTrue(res["default_password"])
        self.assertFalse(res["initialized"])
        # 관리자 토픽은 관리자 웹 client_id 로만
        self.assertEqual(self.req(T.TOPIC_ADMIN_LOGIN, client="INPUT-01",
                                  password=T.ADMIN_DEFAULT_PASSWORD)["status"], "DENIED")
        for _ in range(T.LOGIN_MAX_FAIL - 1):
            self.assertEqual(self.req(T.TOPIC_ADMIN_LOGIN, client=ADMIN, password="x")["status"], "DENIED")
        self.assertEqual(self.req(T.TOPIC_ADMIN_LOGIN, client=ADMIN, password="x")["status"], "LOCKED")
        self.assertEqual(self.req(T.TOPIC_ADMIN_LOGIN, client=ADMIN,
                                  password=T.ADMIN_DEFAULT_PASSWORD)["status"], "LOCKED")
        self.clock.advance(T.LOGIN_LOCK_SEC + 1)
        self.assertEqual(self.req(T.TOPIC_ADMIN_LOGIN, client=ADMIN,
                                  password=T.ADMIN_DEFAULT_PASSWORD)["status"], "OK")

    def test_preview_and_init(self):
        token = self.admin_login()
        preview = self.req(T.TOPIC_ADMIN_SITE_PREVIEW, client=ADMIN, session=token, site=SITE)
        self.assertEqual(preview["account_count"], 69 + 10)      # 공학관 84-15, 인문관 9+1
        self.assertEqual(preview["buildings"][0]["guide"], "101~614호 · 없는 호실: 404호, 5층 전체")
        self.assertEqual(preview["buildings"][1]["guide"], "101~303호 · 추가 호실: 1501호")
        bad = dict(SITE, buildings=[dict(SITE["buildings"][0]), dict(SITE["buildings"][0])])
        self.assertEqual(self.req(T.TOPIC_ADMIN_SITE_INIT, client=ADMIN, session=token,
                                  site=bad)["status"], "INVALID")
        res = self.req(T.TOPIC_ADMIN_SITE_INIT, client=ADMIN, session=token, site=SITE)
        self.assertEqual(res["accounts"], 79)
        self.assertEqual(self.req(T.TOPIC_ADMIN_SITE_INIT, client=ADMIN, session=token,
                                  site=SITE)["status"], "ALREADY_INITIALIZED")
        msg = self.published(T.TOPIC_SITE_CONFIG)[-1]
        self.assertTrue(msg["initialized"])
        self.assertEqual(msg["site"]["buildings"][0]["name"], "공학관")
        self.assertNotIn(404, msg["site"]["buildings"][0]["units"])
        self.assertIn(1501, msg["site"]["buildings"][1]["units"])
        account = self.store.get_account("1-101")
        self.assertTrue(account["must_change"])


class AccountTests(Base):
    def test_login_formats_and_lockout(self):
        res = self.req(T.TOPIC_LOGIN, client="OUTPUT-01", building=2, unit=1501, password="1234")
        self.assertEqual(res["status"], "OK")
        self.assertTrue(res["must_change"])
        self.assertEqual(res["household"]["label"], "인문관 1501호")
        self.assertEqual(self.req(T.TOPIC_LOGIN, user_id="1-614", password="1234")["status"], "OK")
        self.assertEqual(self.req(T.TOPIC_LOGIN, building=1, unit=404, password="1234")["status"], "DENIED")
        for i in range(T.LOGIN_MAX_FAIL - 1):
            res = self.req(T.TOPIC_LOGIN, building=1, unit=102, password="0000")
            self.assertEqual(res["status"], "DENIED")
            self.assertEqual(res["remaining"], T.LOGIN_MAX_FAIL - 1 - i)
        self.assertEqual(self.req(T.TOPIC_LOGIN, building=1, unit=102, password="0000")["status"], "LOCKED")
        self.assertEqual(self.req(T.TOPIC_LOGIN, building=1, unit=102, password="1234")["status"], "LOCKED")
        self.assertEqual(self.req(T.TOPIC_ADMIN_UNLOCK, client=ADMIN, session=self.admin,
                                  user_id="1-102")["status"], "OK")
        self.assertEqual(self.req(T.TOPIC_LOGIN, building=1, unit=102, password="1234")["status"], "OK")

    def test_resident_channel_requires_resident_server(self):
        res = self.req(T.TOPIC_LOGIN, client="OUTPUT-01", building=1, unit=101, password="1234",
                       channel="RESIDENT")
        self.assertEqual(res["status"], "DENIED")
        token = self.login(client=RESIDENT, channel="RESIDENT")
        session = self.store.get_session(__import__("auth").token_hash(token))
        self.assertEqual(session["channel"], "RESIDENT")
        self.assertEqual(session["ttl_sec"], T.RESIDENT_SESSION_TTL_SEC)

    def test_password_change(self):
        web = self.login(client=RESIDENT, channel="RESIDENT")
        kiosk = self.login(client="OUTPUT-01")
        for new, status in [("12a4", "INVALID"), ("123", "INVALID"), ("1234", "INVALID"),
                            ("123456789", "INVALID")]:
            self.assertEqual(self.req(T.TOPIC_PASSWORD, client=RESIDENT, session=web,
                                      current_password="1234", new_password=new)["status"], status, new)
        self.assertEqual(self.req(T.TOPIC_PASSWORD, client=RESIDENT, session=web,
                                  current_password="9999", new_password="5678")["status"], "DENIED")
        res = self.req(T.TOPIC_PASSWORD, client=RESIDENT, session=web,
                       current_password="1234", new_password="5678")
        self.assertEqual(res["status"], "OK")
        self.assertEqual(res["revoked"], 1)
        self.assertFalse(self.store.get_account("1-101")["must_change"])
        self.assertEqual(self.req(T.TOPIC_PARCELS, client="OUTPUT-01", session=kiosk)["status"],
                         "EXPIRED_SESSION")
        self.assertEqual(self.req(T.TOPIC_PARCELS, client=RESIDENT, session=web)["status"], "OK")
        self.assertEqual(self.req(T.TOPIC_LOGIN, building=1, unit=101, password="5678")["status"], "OK")
        # 관리자 비밀번호 초기화 → 1234, 세션 종료
        res = self.req(T.TOPIC_ADMIN_RESET_PASSWORD, client=ADMIN, session=self.admin, user_id="1-101")
        self.assertEqual(res["status"], "OK")
        self.assertEqual(self.req(T.TOPIC_PARCELS, client=RESIDENT, session=web)["status"], "EXPIRED_SESSION")
        self.assertTrue(self.req(T.TOPIC_LOGIN, building=1, unit=101, password="1234")["must_change"])

    def test_sessions_survive_restart(self):
        token = self.login(client=RESIDENT, channel="RESIDENT")
        core2 = LockerCore(self.store, publish=lambda t, p, retain=False: self.sent.append((t, p)),
                           now_fn=self.clock)
        core2.bootstrap()
        self.core = core2
        self.assertEqual(self.req(T.TOPIC_RESIDENT_PARCELS, client=RESIDENT, session=token)["status"], "OK")

    def test_admin_password_change(self):
        res = self.req(T.TOPIC_ADMIN_PASSWORD, client=ADMIN, session=self.admin,
                       current_password=T.ADMIN_DEFAULT_PASSWORD, new_password="short")
        self.assertEqual(res["status"], "INVALID")
        res = self.req(T.TOPIC_ADMIN_PASSWORD, client=ADMIN, session=self.admin,
                       current_password=T.ADMIN_DEFAULT_PASSWORD, new_password="newpass99")
        self.assertEqual(res["status"], "OK")
        login = self.req(T.TOPIC_ADMIN_LOGIN, client=ADMIN, password="newpass99")
        self.assertFalse(login["default_password"])


class RegisterTests(Base):
    def test_validation(self):
        for building, unit in [(3, 101), (1, 404), (1, 505), (1, 615), (1, 100), (2, 1502), (None, 101)]:
            res = self.req(T.TOPIC_REGISTER, building=building, unit=unit)
            self.assertEqual(res["status"], "REJECT", (building, unit))
        self.assertIn("5층 전체", self.req(T.TOPIC_REGISTER, building=1, unit=505)["message"])
        self.assertEqual(self.req(T.TOPIC_REGISTER, building=1, unit=101, tracking_no="ab")["status"], "REJECT")
        self.assertEqual(self.req(T.TOPIC_REGISTER, building=1, unit=101, area="HUGE")["status"], "REJECT")

    def test_one_item_per_space_and_capacity(self):
        receipts = [self.store_item(unit=101 + i) for i in range(10)]
        spaces = {self.item(r).space_id for r in receipts}
        self.assertEqual(len(spaces), 10)
        self.assertEqual(self.req(T.TOPIC_REGISTER, building=1, unit=201)["status"], "STORAGE_FULL")
        # 배송 불가 보관함은 따로 센다
        door = self.req(T.TOPIC_REGISTER, building=1, unit=201, area="OVERSIZE")
        self.assertEqual(door["status"], "OK")
        self.assertEqual(door["door"], 1)
        self.assertIn("01번 문", door["message"])
        self.store_item(unit=202, area="OVERSIZE")
        self.assertEqual(self.req(T.TOPIC_REGISTER, building=1, unit=203, area="OVERSIZE")["status"],
                         "OVERSIZE_FULL")
        # 수령하면 칸이 비어 다시 받는다
        session = self.login(unit=101)
        self.assertEqual(self.req(T.TOPIC_RETRIEVE, client="OUTPUT-01", session=session,
                                  receipt_no=receipts[0])["status"], "OK")
        self.assertEqual(self.req(T.TOPIC_REGISTER, building=1, unit=201)["status"], "OK")

    def test_timeout_frees_space(self):
        res = self.req(T.TOPIC_REGISTER, building=1, unit=101)
        self.tick(T.PORT_TIMEOUT_SEC)
        self.assertEqual(self.item(res["receipt_no"]).status, "CANCELLED")
        self.assertIsNone(self.item(res["receipt_no"]).space_id)
        self.assertEqual(self.to_client("INPUT-01", "REGISTER_CANCELLED")[-1]["reason"], "TIMEOUT")
        self.assertEqual(self.req(T.TOPIC_PORT_CONFIRM, receipt_no=res["receipt_no"])["status"], "CANCELLED")

    def test_direct_locks(self):
        fragile = self.store_item(fragile=True)
        oversize = self.store_item(area="OVERSIZE")
        for receipt, lock in [(fragile, "FRAGILE"), (oversize, "OVERSIZE")]:
            it = self.item(receipt)
            self.assertEqual((it.mode, it.direct_lock, it.plan), ("DIRECT", lock, None))
        web = self.login(client=RESIDENT, channel="RESIDENT")
        res = self.req(T.TOPIC_ITEM_MODE, client=RESIDENT, session=web, receipt_no=fragile, mode="ROBOT")
        self.assertEqual(res["status"], "LOCKED_DIRECT")
        res = self.req(T.TOPIC_DEFAULT_MODE, client=RESIDENT, session=web, mode="DIRECT")
        res = self.req(T.TOPIC_DEFAULT_MODE, client=RESIDENT, session=web, mode="ROBOT")
        self.assertEqual(res["skipped"], 2)
        self.assertEqual(self.item(fragile).mode, "DIRECT")
        parcels = {p["receipt_no"]: p for p in res["parcels"]}
        self.assertEqual(parcels[oversize]["door"], 1)
        self.assertFalse(parcels[oversize]["changeable"])
        kinds = [n["kind"] for n in self.req(T.TOPIC_NOTIFICATIONS, client=RESIDENT,
                                             session=web)["notifications"]]
        self.assertEqual(kinds.count("ARRIVED_DIRECT"), 2)


class ChoiceAndAutoTests(Base):
    def test_choice_window_then_auto_pair(self):
        web = self.login(client=RESIDENT, channel="RESIDENT")
        a = self.store_item(unit=101)
        self.assertEqual(self.item(a).plan, "CHOICE_OPEN")
        note = self.to_client(RESIDENT, "NOTIFICATION")[-1]["notification"]
        self.assertEqual(note["kind"], "ARRIVED_CHOICE")
        self.assertIn("14:30", note["body"])
        b = self.store_item(unit=102)
        self.assertEqual(self.jobs(), [])                    # 예약 가능 시간에는 출발하지 않음
        self.tick(20 * 60)
        self.assertIn("CHOICE_REMINDER", [n["notification"]["kind"]
                                          for n in self.to_client(RESIDENT, "NOTIFICATION")])
        self.assertEqual(self.jobs(), [])
        self.tick(10 * 60)
        self.assertEqual(self.item(a).plan, "AUTO")
        self.assertEqual(self.item(b).plan, "AUTO")
        jobs = self.jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual({i["receipt_no"] for i in jobs[0]["items"]}, {a, b})
        item = jobs[0]["items"][0]
        self.assertEqual((item["dong"], item["ho"], item["building_name"]), (1, 101, "공학관"))
        # 확정된 물품은 세대 웹에서 바꿀 수 없지만 키오스크 수령은 가능
        res = self.req(T.TOPIC_ITEM_MODE, client=RESIDENT, session=web, receipt_no=a, mode="DIRECT")
        self.assertEqual(res["status"], "FIXED")

    def test_single_auto_waits_for_pair(self):
        a = self.store_item()
        self.tick(T.CHOICE_WINDOW_SEC)
        self.assertEqual(self.jobs(), [])
        self.tick(T.AGING_LIMIT_SEC + 60)                    # 오래 기다려도 1개로는 출발하지 않는다
        self.assertEqual(self.jobs(), [])
        b = self.store_item(building=2, unit=101)
        self.tick(T.CHOICE_WINDOW_SEC)
        self.assertEqual({i["receipt_no"] for i in self.jobs()[0]["items"]}, {a, b})

    def test_pairing_prefers_same_floor(self):
        anchor = self.store_item(building=1, unit=101)
        self.clock.advance(1)
        self.store_item(building=2, unit=101)
        self.store_item(building=1, unit=301)
        same_floor = self.store_item(building=1, unit=103)
        self.tick(T.CHOICE_WINDOW_SEC)
        # 가장 먼저 자동 배송 확정된 물품이 기준 → 같은 건물·층 물품과 묶는다
        self.assertEqual({i["receipt_no"] for i in self.jobs()[0]["items"]}, {anchor, same_floor})

    def test_household_direct_blocks_robot(self):
        web = self.login(client=RESIDENT, channel="RESIDENT")
        a = self.store_item()
        res = self.req(T.TOPIC_DEFAULT_MODE, client=RESIDENT, session=web, mode="DIRECT")
        self.assertEqual(res["changed"], 1)
        self.assertEqual(self.req(T.TOPIC_ITEM_MODE, client=RESIDENT, session=web, receipt_no=a,
                                  mode="ROBOT")["status"], "DENIED")
        self.req(T.TOPIC_DEFAULT_MODE, client=RESIDENT, session=web, mode="ROBOT")
        self.assertEqual(self.item(a).plan, "CHOICE_OPEN")      # 아직 30분 안
        code = self.req(T.TOPIC_PICKUP_CODE, client=RESIDENT, session=web, receipt_no=a)
        self.assertEqual(code["status"], "NOT_AVAILABLE")


class ReservationTests(Base):
    def slots(self, session, client=RESIDENT):
        res = self.req(T.TOPIC_SLOTS, client=client, session=session)
        self.assertEqual(res["status"], "OK")
        return res["slots"]

    def test_slot_list_bounds(self):
        web = self.login(client=RESIDENT, channel="RESIDENT")
        slots = self.slots(web)
        self.assertEqual(slots[0]["start"], "2026-10-31T14:20:00+09:00")   # 20분 뒤부터
        self.assertEqual(slots[0]["label"], "14:20~14:30")
        self.assertTrue(all("08:00" <= s["label"][:5] and s["label"][6:] <= "22:00" for s in slots))
        self.assertEqual(slots[-1]["start"], "2026-11-01T13:50:00+09:00")  # 24시간 안
        self.clock.advance(5 * 60)                                           # 14:05 → 14:30 부터
        self.assertEqual(self.slots(web)[0]["start"], "2026-10-31T14:30:00+09:00")

    def test_two_seats_first_come(self):
        a1, a2 = self.store_item(unit=101), self.store_item(unit=101)
        b1, b2 = self.store_item(unit=102), self.store_item(unit=102)
        wa = self.login(unit=101, client=RESIDENT, channel="RESIDENT")
        wb = self.login(unit=102, client=RESIDENT, channel="RESIDENT")
        slot = "2026-10-31T15:00:00+09:00"
        res = self.req(T.TOPIC_RESERVE, client=RESIDENT, session=wa, slot_start=slot, receipt_nos=[a1])
        self.assertEqual(res["status"], "OK")
        update = self.published(T.TOPIC_SLOTS_UPDATE)[-1]
        self.assertEqual(update["slots"], [{"start": slot, "seats_left": 1}])
        # B 는 2개를 원하지만 1자리뿐 → 전부 실패
        res = self.req(T.TOPIC_RESERVE, client=RESIDENT, session=wb, slot_start=slot, receipt_nos=[b1, b2])
        self.assertEqual((res["status"], res["seats_left"]), ("SLOT_FULL", 1))
        self.assertEqual(self.item(b1).plan, "CHOICE_OPEN")
        res = self.req(T.TOPIC_RESERVE, client=RESIDENT, session=wb, slot_start=slot, receipt_nos=[b1])
        self.assertEqual(res["status"], "OK")
        res = self.req(T.TOPIC_RESERVE, client=RESIDENT, session=wa, slot_start=slot, receipt_nos=[a2])
        self.assertEqual((res["status"], res["seats_left"]), ("SLOT_FULL", 0))
        view = {s["start"]: s for s in self.slots(wa)}[slot]
        self.assertEqual((view["seats_left"], view["mine"]), (0, 1))
        # 한 세대가 두 자리 모두
        slot2 = "2026-10-31T15:10:00+09:00"
        res = self.req(T.TOPIC_RESERVE, client=RESIDENT, session=wa, slot_start=slot2, receipt_nos=[a2, a1])
        self.assertEqual(res["status"], "OK")                # a1 은 15:00 → 15:10 으로 변경
        self.assertEqual(self.item(a1).slot_start, slot2)
        self.assertEqual({s["start"]: s for s in self.slots(wb)}[slot]["seats_left"], 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM slots").fetchone()[0], 3)

    def test_rules(self):
        a = self.store_item()
        web = self.login(client=RESIDENT, channel="RESIDENT")
        too_soon = "2026-10-31T14:10:00+09:00"
        self.assertEqual(self.req(T.TOPIC_RESERVE, client=RESIDENT, session=web, slot_start=too_soon,
                                  receipt_nos=[a])["status"], "SLOT_UNAVAILABLE")
        night = "2026-10-31T22:00:00+09:00"
        self.assertEqual(self.req(T.TOPIC_RESERVE, client=RESIDENT, session=web, slot_start=night,
                                  receipt_nos=[a])["status"], "SLOT_UNAVAILABLE")
        odd = "2026-10-31T15:05:00+09:00"
        self.assertEqual(self.req(T.TOPIC_RESERVE, client=RESIDENT, session=web, slot_start=odd,
                                  receipt_nos=[a])["status"], "SLOT_UNAVAILABLE")
        fragile = self.store_item(fragile=True)
        self.assertEqual(self.req(T.TOPIC_RESERVE, client=RESIDENT, session=web,
                                  slot_start="2026-10-31T15:00:00+09:00",
                                  receipt_nos=[fragile])["status"], "LOCKED_DIRECT")
        slot = "2026-10-31T15:00:00+09:00"
        self.assertEqual(self.req(T.TOPIC_RESERVE, client=RESIDENT, session=web, slot_start=slot,
                                  receipt_nos=[a])["status"], "OK")
        # 예약 가능 시간(30분)이 지난 뒤 취소 → 자동 배송 확정
        self.tick(31 * 60)                                    # 14:31, 칸 15:00 의 마감 14:40
        self.assertEqual(self.item(a).plan, "RESERVED")
        res = self.req(T.TOPIC_RESERVE_CANCEL, client=RESIDENT, session=web, receipt_no=a)
        self.assertEqual((res["status"], res["plan"]), ("OK", "AUTO"))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM slots").fetchone()[0], 0)

    def test_change_closes_20_minutes_before(self):
        a = self.store_item()
        web = self.login(client=RESIDENT, channel="RESIDENT")
        slot = "2026-10-31T14:30:00+09:00"
        self.assertEqual(self.req(T.TOPIC_RESERVE, client=RESIDENT, session=web, slot_start=slot,
                                  receipt_nos=[a])["status"], "OK")
        self.clock.advance(10 * 60)                           # 14:10 = 마감
        self.assertEqual(self.req(T.TOPIC_RESERVE_CANCEL, client=RESIDENT, session=web,
                                  receipt_no=a)["status"], "TOO_LATE")
        self.assertEqual(self.req(T.TOPIC_ITEM_MODE, client=RESIDENT, session=web, receipt_no=a,
                                  mode="DIRECT")["status"], "FIXED")

    def test_dispatch_with_same_building_filler_and_delay(self):
        web = self.login(unit=101, client=RESIDENT, channel="RESIDENT")
        filler_other = self.store_item(building=2, unit=101)
        filler = self.store_item(building=1, unit=301)
        a = self.store_item(unit=101)
        slot = "2026-10-31T15:00:00+09:00"
        self.assertEqual(self.req(T.TOPIC_RESERVE, client=RESIDENT, session=web, slot_start=slot,
                                  receipt_nos=[a])["status"], "OK")
        self.tick(T.CHOICE_WINDOW_SEC)                        # 14:30 필러들 자동 배송 확정
        self.assertEqual(self.jobs(), [])                     # 예약 출발(14:45)이 30분 안 → 마지막 로봇 보호
        self.tick(15 * 60)                                    # 14:45 = 칸 시작 15분 전
        job = self.jobs()[-1]
        self.assertEqual([i["receipt_no"] for i in job["items"]], [a, filler])   # 예약 세대 먼저
        self.assertEqual(job["summary"]["kind"], "RESERVED")
        self.assertNotIn(filler_other, [i["receipt_no"] for i in job["items"]])
        self.tick(15 * 60)                                    # 15:00 출발 전 → 지연 알림
        kinds = [n["notification"]["kind"] for n in self.to_client(RESIDENT, "NOTIFICATION")]
        self.assertIn("DELAYED", kinds)

    def test_lone_reservations_pair_across_due_slots(self):
        wa = self.login(building=1, unit=101, client=RESIDENT, channel="RESIDENT")
        wb = self.login(building=2, unit=101, client=RESIDENT, channel="RESIDENT")
        a = self.store_item(building=1, unit=101)
        b = self.store_item(building=2, unit=101)
        self.req(T.TOPIC_RESERVE, client=RESIDENT, session=wa, slot_start="2026-10-31T15:00:00+09:00",
                 receipt_nos=[a])
        self.req(T.TOPIC_RESERVE, client=RESIDENT, session=wb, slot_start="2026-10-31T15:10:00+09:00",
                 receipt_nos=[b])
        self.tick(45 * 60)                                    # 14:45 = a 칸 발행 시각. 짝이 없어 기다린다
        self.assertEqual(self.jobs(), [])
        self.tick(10 * 60)                                    # 14:55 = b 칸 발행 시각 → 둘을 묶는다
        job = self.jobs()[-1]
        self.assertEqual([i["receipt_no"] for i in job["items"]], [a, b])
        self.assertEqual(job["summary"]["kind"], "RESERVED")

    def test_second_seat_same_building_option(self):
        token = self.admin
        site = dict(SITE, second_seat_same_building=True)
        self.assertEqual(self.req(T.TOPIC_ADMIN_SITE_UPDATE, client=ADMIN, session=token,
                                  site=site)["status"], "OK")
        a = self.store_item(building=1, unit=101)
        b = self.store_item(building=2, unit=101)
        wa = self.login(building=1, unit=101, client=RESIDENT, channel="RESIDENT")
        wb = self.login(building=2, unit=101, client=RESIDENT, channel="RESIDENT")
        slot = "2026-10-31T15:00:00+09:00"
        self.req(T.TOPIC_RESERVE, client=RESIDENT, session=wa, slot_start=slot, receipt_nos=[a])
        self.assertEqual(self.req(T.TOPIC_RESERVE, client=RESIDENT, session=wb, slot_start=slot,
                                  receipt_nos=[b])["status"], "SLOT_FULL")
        view = {s["start"]: s for s in self.req(T.TOPIC_SLOTS, client=RESIDENT, session=wb)["slots"]}[slot]
        self.assertTrue(view["restricted"])


class RobotTests(Base):
    def auto_pair(self, units=(101, 102)):
        receipts = [self.store_item(unit=u) for u in units]
        self.tick(T.CHOICE_WINDOW_SEC)
        return receipts, self.jobs()[-1]["job_id"]

    def test_late_binding_one_left_refills_from_pool(self):
        (a, b), job_id = self.auto_pair()
        c = self.store_item(unit=103)
        self.tick(T.CHOICE_WINDOW_SEC)                        # c 자동 배송 확정. 로봇 1대라 대기
        session = self.login(unit=102)
        res = self.req(T.TOPIC_RETRIEVE, client="OUTPUT-01", session=session, receipt_no=b)
        self.assertEqual(res["status"], "OK")                 # 적재 전에는 꺼낼 수 있다
        self.fms(type="ROBOT_ARRIVED", job_id=job_id, robot_id="R1")
        bundle = self.published(T.TOPIC_FMS_BUNDLE, "BUNDLE_CONFIRMED")[-1]
        self.assertEqual([(i["receipt_no"], i["tray"]) for i in bundle["items"]],
                         [(a, "TRAY_A"), (c, "TRAY_B")])
        for receipt in (a, c):
            self.assertEqual((self.item(receipt).status, self.item(receipt).job_id), ("LOADED", job_id))
        self.assertEqual(self.item(a).robot_id, "R1")
        self.assertEqual(self.req(T.TOPIC_RETRIEVE, client="OUTPUT-01", session=self.login(unit=101),
                                  receipt_no=a)["status"], "ALREADY_LOADED")

    def test_late_binding_one_left_without_pair_cancels(self):
        (a, b), job_id = self.auto_pair()
        self.req(T.TOPIC_RETRIEVE, client="OUTPUT-01", session=self.login(unit=102), receipt_no=b)
        self.fms(type="ROBOT_ARRIVED", job_id=job_id, robot_id="R1")
        self.assertEqual(self.published(T.TOPIC_FMS_BUNDLE, "BUNDLE_CONFIRMED"), [])
        cancel = self.published(T.TOPIC_FMS_BUNDLE, "JOB_CANCELLED")
        self.assertEqual(cancel[-1]["reason"], "NOT_ENOUGH")
        it = self.item(a)
        self.assertEqual((it.status, it.job_id, it.plan), ("STORED", None, "AUTO"))
        self.tick(60)
        self.assertEqual(len(self.jobs()), 1)                 # 1개로는 다시 발행하지 않는다
        d = self.store_item(unit=104)
        self.tick(T.CHOICE_WINDOW_SEC)
        self.assertEqual({i["receipt_no"] for i in self.jobs()[-1]["items"]}, {a, d})

    def test_emptied_job_cancels_before_arrival(self):
        (a, b), job_id = self.auto_pair(units=(101, 101))
        session = self.login(unit=101)
        self.req(T.TOPIC_RETRIEVE, client="OUTPUT-01", session=session, receipt_no=a)
        self.assertEqual(self.published(T.TOPIC_FMS_BUNDLE, "JOB_CANCELLED"), [])
        self.req(T.TOPIC_RETRIEVE, client="OUTPUT-01", session=session, receipt_no=b)
        cancel = self.published(T.TOPIC_FMS_BUNDLE, "JOB_CANCELLED")
        self.assertEqual(len(cancel), 1)
        self.assertEqual(cancel[0]["reason"], "EMPTIED")
        # FMS 가 도착 전 취소를 무시해도, 도착하면 같은 취소를 다시 보낸다
        self.fms(type="ROBOT_ARRIVED", job_id=job_id, robot_id="R1")
        self.assertEqual(len(self.published(T.TOPIC_FMS_BUNDLE, "JOB_CANCELLED")), 2)

    def test_delivery_keeps_bin_until_job_done(self):
        web = self.login(unit=101, client=RESIDENT, channel="RESIDENT")
        (a, b), job_id = self.auto_pair()
        self.fms(type="ROBOT_ARRIVED", job_id=job_id, robot_id="R1")
        self.assertIn("DEPARTED", [n["notification"]["kind"] for n in self.to_client(RESIDENT, "NOTIFICATION")])
        self.fms(type="DELIVERY_RESULT", job_id=job_id, receipt_no=a, result="DELIVERED")
        self.assertEqual(self.item(a).status, "DELIVERED")
        occupancy = self.core.summary()["occupancy"]["grid"]
        self.assertEqual((occupancy["used"], occupancy["on_robot"]), (2, 2))
        self.fms(type="DELIVERY_RESULT", job_id=job_id, receipt_no=b, result="FAILED", reason="부재")
        self.assertEqual(self.item(b).status, "LOADED")       # 보관함은 로봇과 함께 돌아온다
        self.fms(type="JOB_DONE", job_id=job_id)
        self.assertIsNone(self.item(a).space_id)
        it = self.item(b)
        self.assertEqual((it.status, it.plan, it.fail_count), ("STORED", "AUTO", 1))
        self.assertIsNotNone(it.space_id)
        self.assertEqual(self.core.summary()["occupancy"]["grid"]["used"], 1)
        parcels = self.req(T.TOPIC_RESIDENT_PARCELS, client=RESIDENT, session=web)["parcels"]
        self.assertEqual({p["receipt_no"]: p["status"] for p in parcels}[a], "DELIVERED")

    def test_two_failures_lock_direct(self):
        (a, b), _ = self.auto_pair()
        for attempt in range(2):
            job_id = self.jobs()[-1]["job_id"]
            self.fms(type="ROBOT_ARRIVED", job_id=job_id, robot_id="R1")
            self.fms(type="DELIVERY_RESULT", job_id=job_id, receipt_no=a, result="FAILED")
            self.fms(type="DELIVERY_RESULT", job_id=job_id, receipt_no=b, result="FAILED")
            self.fms(type="JOB_DONE", job_id=job_id)
            if attempt == 0:
                self.assertEqual(len(self.jobs()), 2)         # 둘 다 재입고 → 다시 묶여 발행
        for receipt in (a, b):
            it = self.item(receipt)
            self.assertEqual((it.mode, it.direct_lock, it.fail_count), ("DIRECT", "FAILED", 2))

    def test_fms_rejection_switches_unroutable_to_direct_and_reissues(self):
        (a, b), job_id = self.auto_pair()
        c = self.store_item(unit=103)
        self.tick(T.CHOICE_WINDOW_SEC)
        self.fms(type="JOB_REJECTED", job_id=job_id, reason="세대 1-101 목적지 연결 없음", unroutable=[a])
        self.assertEqual(self.store.get_job(job_id)["status"], "CANCELLED")
        it = self.item(a)
        self.assertEqual((it.status, it.mode, it.job_id, it.direct_lock), ("STORED", "DIRECT", None, None))
        # 나머지는 다음 배송 후보. 갈 수 없는 물품 없이 새 Job 을 발행한다.
        new_job = self.jobs()[-1]
        self.assertNotEqual(new_job["job_id"], job_id)
        self.assertEqual({i["receipt_no"] for i in new_job["items"]}, {b, c})
        # 중복 취소·적재 확정 후 취소는 무시
        self.fms(type="JOB_REJECTED", job_id=job_id, reason="중복", unroutable=[b])
        self.assertEqual(self.item(b).mode, "ROBOT")
        self.fms(type="ROBOT_ARRIVED", job_id=new_job["job_id"], robot_id="R1")
        self.fms(type="JOB_REJECTED", job_id=new_job["job_id"], reason="늦음", unroutable=[b])
        self.assertEqual(self.store.get_job(new_job["job_id"])["status"], "CONFIRMED")
        self.assertEqual(self.item(b).status, "LOADED")

    def test_robots_limit_jobs(self):
        self.auto_pair(units=(101, 102))
        self.store_item(unit=103)
        self.store_item(unit=104)
        self.tick(T.CHOICE_WINDOW_SEC)
        self.assertEqual(len(self.jobs()), 1)                 # 로봇 1대 → 동시에 1건

    def test_duplicate_request_is_idempotent(self):
        rid = "same-request"
        first = self.req(T.TOPIC_REGISTER, request_id=rid, building=1, unit=101)
        second = self.req(T.TOPIC_REGISTER, request_id=rid, building=1, unit=101)
        self.assertEqual(first["receipt_no"], second["receipt_no"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM items").fetchone()[0], 1)


class DemoTests(Base):
    """대회 시연 전용 '대기 건너뛰기' (LASTGATE_DEMO=1)."""

    def setUp(self):
        super().setUp()
        self._demo = T.DEMO_MODE
        T.DEMO_MODE = True
        self.addCleanup(setattr, T, "DEMO_MODE", self._demo)

    def skip(self):
        return self.req(T.TOPIC_ADMIN_DEMO_SKIP, client=ADMIN, session=self.admin)

    def test_disabled_outside_demo(self):
        T.DEMO_MODE = False
        self.store_item()
        self.assertEqual(self.skip()["status"], "DISABLED")
        self.assertFalse(self.req(T.TOPIC_ADMIN_STORAGE, client=ADMIN, session=self.admin)["demo"])

    def test_skip_choice_window_and_reservation_dispatches_now(self):
        web = self.login(building=1, unit=101, client=RESIDENT, channel="RESIDENT")
        a = self.store_item(building=1, unit=101)
        b = self.store_item(building=2, unit=101)
        slot = "2026-10-31T15:00:00+09:00"                    # 지금 14:00 → 원래 14:45 발행
        self.assertEqual(self.req(T.TOPIC_RESERVE, client=RESIDENT, session=web, slot_start=slot,
                                  receipt_nos=[a])["status"], "OK")
        self.assertEqual(self.jobs(), [])
        res = self.skip()
        self.assertEqual((res["status"], res["auto"], res["rushed"]), ("OK", [b], [a]))
        self.assertEqual(len(res["jobs"]), 1)
        job = self.jobs()[-1]
        self.assertEqual([i["receipt_no"] for i in job["items"]], [a, b])   # 예약 세대 먼저
        self.assertEqual(job["summary"]["kind"], "RESERVED")
        self.assertIn("새 배송 1건", res["summary"])
        self.assertTrue(self.req(T.TOPIC_ADMIN_STORAGE, client=ADMIN, session=self.admin)["demo"])

    def test_single_item_still_waits_for_pair(self):
        a = self.store_item()
        res = self.skip()
        self.assertEqual((res["auto"], res["jobs"]), ([a], []))
        self.assertEqual([w["receipt_no"] for w in res["waiting"]], [a])
        self.assertIn("2개가 모여야 출발", res["summary"])
        self.assertEqual(self.jobs(), [])


class AdminTests(Base):
    def test_update_adds_and_blocks(self):
        token = self.admin
        site = dict(SITE, buildings=[dict(SITE["buildings"][0], missing="404"),
                                     SITE["buildings"][1],
                                     {"code": 3, "name": "기숙사", "unit_from": 101, "unit_to": 102}])
        preview = self.req(T.TOPIC_ADMIN_SITE_PREVIEW, client=ADMIN, session=token, site=site)
        self.assertEqual(preview["diff"]["added"], 14 + 2)
        res = self.req(T.TOPIC_ADMIN_SITE_UPDATE, client=ADMIN, session=token, site=site)
        self.assertEqual((res["status"], res["added"]), ("OK", 16))
        self.assertTrue(self.store.get_account("3-102")["must_change"])
        self.assertEqual(self.published(T.TOPIC_SITE_CONFIG)[-1]["version"], 2)
        # 물품이 있는 호실은 뺄 수 없다
        self.store_item(building=3, unit=101)
        res = self.req(T.TOPIC_ADMIN_SITE_UPDATE, client=ADMIN, session=token, site=SITE)
        self.assertEqual(res["status"], "BLOCKED")
        # 사용 중인 칸보다 줄일 수 없다
        for unit in (101, 102, 103):
            self.store_item(building=1, unit=unit)
        small = dict(site, grid_bins=2)
        self.assertEqual(self.req(T.TOPIC_ADMIN_SITE_UPDATE, client=ADMIN, session=token,
                                  site=small)["status"], "BLOCKED")
        # 빈 호실 제거 → 계정 비활성
        fewer = dict(site, buildings=site["buildings"][:2] + [dict(site["buildings"][2], unit_to=101)])
        self.assertEqual(self.req(T.TOPIC_ADMIN_SITE_UPDATE, client=ADMIN, session=token,
                                  site=fewer)["status"], "OK")
        self.assertFalse(self.store.get_account("3-102")["active"])
        self.assertEqual(self.req(T.TOPIC_LOGIN, building=3, unit=102, password="1234")["status"], "DENIED")

    def test_change_occupant_hides_history(self):
        web = self.login(client=RESIDENT, channel="RESIDENT")
        a = self.store_item()
        self.req(T.TOPIC_RETRIEVE, client="OUTPUT-01", session=self.login(), receipt_no=a)
        self.assertEqual(len(self.req(T.TOPIC_RESIDENT_PARCELS, client=RESIDENT, session=web)["parcels"]), 1)
        self.req(T.TOPIC_DEFAULT_MODE, client=RESIDENT, session=web, mode="DIRECT")
        self.clock.advance(60)
        res = self.req(T.TOPIC_ADMIN_CHANGE_OCCUPANT, client=ADMIN, session=self.admin, user_id="1-101")
        self.assertEqual(res["status"], "OK")
        web = self.login(client=RESIDENT, channel="RESIDENT")
        res = self.req(T.TOPIC_RESIDENT_PARCELS, client=RESIDENT, session=web)
        self.assertEqual(res["parcels"], [])
        self.assertEqual(res["household"]["default_mode"], "ROBOT")
        self.assertEqual(self.req(T.TOPIC_NOTIFICATIONS, client=RESIDENT, session=web)["notifications"], [])

    def test_code_pickup_and_storage(self):
        a = self.store_item(area="OVERSIZE")
        code = self.item(a).pickup_code
        res = self.req(T.TOPIC_ADMIN_CODE_PICKUP, client=ADMIN, session=self.admin, code=code)
        self.assertEqual((res["status"], res["confirmed"], res["item"]["door"]), ("OK", False, 1))
        storage = self.req(T.TOPIC_ADMIN_STORAGE, client=ADMIN, session=self.admin)
        self.assertEqual(storage["occupancy"]["oversize"]["used"], 1)
        res = self.req(T.TOPIC_ADMIN_CODE_PICKUP, client=ADMIN, session=self.admin, code=code, confirm=True)
        self.assertTrue(res["confirmed"])
        self.assertEqual(self.item(a).status, "PICKED")
        events = self.req(T.TOPIC_ADMIN_EVENTS, client=ADMIN, session=self.admin)["events"]
        self.assertIn("ADMIN_CODE_PICKUP", [e["kind"] for e in events])

    def test_full_reset(self):
        token = self.admin
        a = self.store_item()
        res = self.req(T.TOPIC_ADMIN_SITE_RESET, client=ADMIN, session=token,
                       password=T.ADMIN_DEFAULT_PASSWORD, confirm_name="테스트대학교")
        self.assertEqual(res["status"], "BLOCKED")
        self.req(T.TOPIC_RETRIEVE, client="OUTPUT-01", session=self.login(), receipt_no=a)
        self.assertEqual(self.req(T.TOPIC_ADMIN_SITE_RESET, client=ADMIN, session=token,
                                  password=T.ADMIN_DEFAULT_PASSWORD, confirm_name="다른이름")["status"],
                         "MISMATCH")
        res = self.req(T.TOPIC_ADMIN_SITE_RESET, client=ADMIN, session=token,
                       password=T.ADMIN_DEFAULT_PASSWORD, confirm_name="테스트대학교")
        self.assertEqual(res["status"], "OK")
        msg = self.published(T.TOPIC_SITE_CONFIG)[-1]
        self.assertFalse(msg["initialized"])
        self.assertEqual(self.store.account_stats()["total"], 0)
        res = self.req(T.TOPIC_ADMIN_SITE_INIT, client=ADMIN, session=token, site=SITE)
        self.assertEqual(res["status"], "OK")
        self.assertEqual(self.published(T.TOPIC_SITE_CONFIG)[-1]["version"], 2)   # 버전은 줄지 않음


class MigrationTests(unittest.TestCase):
    def test_v3_db_is_converted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "locker.db"
            db = sqlite3.connect(path)
            db.executescript("""
                CREATE TABLE households (dong INTEGER, ho INTEGER, default_mode TEXT, PRIMARY KEY (dong, ho));
                CREATE TABLE accounts (user_id TEXT PRIMARY KEY, dong INTEGER, ho INTEGER, password_hash TEXT);
                CREATE TABLE items (receipt_no TEXT PRIMARY KEY, dong INTEGER, ho INTEGER, size TEXT,
                    mode TEXT, status TEXT, pickup_code TEXT, registered_at TEXT, tracking_no TEXT,
                    input_client TEXT, slot_id TEXT, job_id TEXT, fail_count INTEGER DEFAULT 0,
                    stored_at TEXT, updated_at TEXT);
                CREATE TABLE jobs (job_id TEXT PRIMARY KEY, status TEXT, robot_id TEXT, created_at TEXT,
                    confirmed_at TEXT, done_at TEXT, bundle TEXT, metrics TEXT);
            """)
            from auth import hash_password
            seed = hash_password("1234")
            for dong in (101, 102):
                for ho in (101, 102, 201, 202):
                    if (dong, ho) == (102, 202):
                        continue
                    db.execute("INSERT INTO households VALUES (?, ?, ?)", (dong, ho, "DIRECT" if ho == 101 else "ROBOT"))
                    db.execute("INSERT INTO accounts VALUES (?, ?, ?, ?)", (f"{dong}-{ho}", dong, ho, seed))
            db.execute("INSERT INTO items VALUES ('R1', 101, 102, 'M', 'ROBOT', 'STORED', '123456', "
                       "'2026-10-30T10:00:00+09:00', NULL, NULL, 'A-C01-L1', NULL, 0, "
                       "'2026-10-30T10:01:00+09:00', NULL)")
            db.commit()
            db.close()

            store = Store(path)
            self.assertTrue(store.migrated_from_v3)
            site = store.get_site()
            self.assertEqual([b["name"] for b in site["buildings"]], ["101동", "102동"])
            self.assertEqual(site["buildings"][1]["missing"], "202")
            account = store.get_account("101-101")
            self.assertEqual((account["building"], account["default_mode"], account["must_change"]),
                             (101, "DIRECT", 1))
            item = store.get_item("R1")
            self.assertEqual((item.building, item.unit, item.space_id, item.plan), (101, 102, "G-001", "AUTO"))
            core = LockerCore(store, publish=lambda *a, **k: None)
            core.bootstrap()
            self.assertEqual(core.site.label(101, 102), "101동 102호")
            store.db.close()


if __name__ == "__main__":
    unittest.main()

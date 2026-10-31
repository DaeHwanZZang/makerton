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
from storage import VirtualStorage  # noqa: E402
from store import Store  # noqa: E402

config.BCRYPT_ROUNDS = 4          # 테스트 속도용
T = config


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 31, 14, 0, tzinfo=config.KST)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.sent = []
        self.store = Store(":memory:")
        self.core = LockerCore(self.store, VirtualStorage(),
                               publish=lambda t, p, retain=False: self.sent.append((t, p)),
                               now_fn=self.clock)
        self.core.bootstrap()

    # --- 헬퍼 ------------------------------------------------------------
    def req(self, topic, client="INPUT-01", request_id=None, **body):
        request_id = request_id or uuid.uuid4().hex
        self.core.handle(topic, {**body, "request_id": request_id, "client_id": client})
        res_topic = T.TOPIC_CLIENT_RES.format(client_id=client)
        return next(p for t, p in reversed(self.sent)
                    if t == res_topic and p.get("request_id") == request_id)

    def published(self, topic, type_=None):
        return [p for t, p in self.sent if t == topic and (type_ is None or p.get("type") == type_)]

    def fms(self, **payload):
        self.core.handle(T.TOPIC_FMS_EVENT, payload)

    def store_item(self, dong=101, ho=412, size="S"):
        res = self.req(T.TOPIC_REGISTER, dong=dong, ho=ho, size=size)
        self.assertEqual(res["status"], "OK", res)
        conf = self.req(T.TOPIC_PORT_CONFIRM, receipt_no=res["receipt_no"], port=size)
        self.assertEqual(conf["status"], "OK", conf)
        return res["receipt_no"]

    def store_pair(self, dong=101, ho=412, partner_ho=411, size="S"):
        """배송은 물품 2개가 모여야 시작한다. 기준 물품과 짝 물품을 보관하고 (기준, 짝, job_id)."""
        first = self.store_item(dong=dong, ho=ho, size=size)
        self.clock.advance(1)
        second = self.store_item(dong=dong, ho=partner_ho, size=size)
        job_id = self.published(T.TOPIC_FMS_JOB, "DELIVERY_JOB")[-1]["job_id"]
        return first, second, job_id

    def login(self, user="101-412", client="OUTPUT-01"):
        res = self.req(T.TOPIC_LOGIN, client=client, user_id=user, password="1234")
        self.assertEqual(res["status"], "OK", res)
        return res["session"]

    def status(self, receipt):
        return self.store.get_item(receipt).status

    # --- 계정·접수 -------------------------------------------------------
    def test_seeded_accounts(self):
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 288)
        self.assertEqual(self.req(T.TOPIC_LOGIN, user_id="106-412", password="1234")["status"], "OK")
        self.assertEqual(self.req(T.TOPIC_LOGIN, user_id="101-412", password="0000")["status"], "DENIED")

    def test_register_validation(self):
        for dong, ho in [(107, 101), (101, 413), (101, 501), (101, 100), (101, 1203)]:
            self.assertEqual(self.req(T.TOPIC_REGISTER, dong=dong, ho=ho, size="S")["status"], "REJECT")
        self.assertEqual(self.req(T.TOPIC_REGISTER, dong=101, ho=101, size="S",
                                  tracking_no="ab")["status"], "REJECT")
        self.assertEqual(self.req(T.TOPIC_REGISTER, dong=101, ho=101, size="S",
                                  tracking_no="CJ12345678")["status"], "OK")

    def test_receipt_format_and_code_hidden(self):
        res = self.req(T.TOPIC_REGISTER, dong=101, ho=101, size="M")
        self.assertRegex(res["receipt_no"], r"^20261031-[0-9A-F]{8}$")
        self.assertNotIn("pickup_code", res)
        self.assertRegex(self.store.get_item(res["receipt_no"]).pickup_code, r"^\d{6}$")

    def test_duplicate_request_is_idempotent(self):
        a = self.req(T.TOPIC_REGISTER, request_id="dup-1", dong=101, ho=101, size="S")
        b = self.req(T.TOPIC_REGISTER, request_id="dup-1", dong=101, ho=101, size="S")
        self.assertEqual(a["receipt_no"], b["receipt_no"])
        self.assertEqual(self.store.status_counts().get("REGISTERED"), 1)

    def test_port_timeout_cancels_registration(self):
        res = self.req(T.TOPIC_REGISTER, dong=101, ho=101, size="S")
        self.clock.advance(T.PORT_TIMEOUT_SEC + 1)
        self.core.heartbeat()
        self.assertEqual(self.status(res["receipt_no"]), "CANCELLED")
        self.assertTrue(self.published(T.TOPIC_CLIENT_RES.format(client_id="INPUT-01"),
                                       "REGISTER_CANCELLED"))
        conf = self.req(T.TOPIC_PORT_CONFIRM, receipt_no=res["receipt_no"], port="S")
        self.assertEqual(conf["status"], "CANCELLED")

    def test_today_count_per_kiosk_resets_at_midnight(self):
        def store_via(kiosk, client, ho=101):
            res = self.req(T.TOPIC_REGISTER, client=client, dong=101, ho=ho, size="S", kiosk_id=kiosk)
            return self.req(T.TOPIC_PORT_CONFIRM, client=client, receipt_no=res["receipt_no"], port="S")

        self.assertEqual(store_via("INPUT-01", "INPUT-01-A")["today_count"], 1)
        self.assertEqual(store_via("INPUT-01", "INPUT-01-B")["today_count"], 2)   # 새로고침(새 접속)해도 이어짐
        self.assertEqual(store_via("INPUT-02", "INPUT-02-A")["today_count"], 1)   # 키오스크별로 따로
        self.req(T.TOPIC_REGISTER, client="INPUT-01-A", dong=101, ho=102, size="S", kiosk_id="INPUT-01")
        stats = self.req(T.TOPIC_INPUT_STATS, client="INPUT-01-A", kiosk_id="INPUT-01")
        self.assertEqual((stats["date"], stats["today_count"]), ("2026-10-31", 2))  # 투입 전 접수는 제외

        self.clock.now = datetime(2026, 11, 1, 0, 0, 1, tzinfo=config.KST)
        stats = self.req(T.TOPIC_INPUT_STATS, client="INPUT-01-A", kiosk_id="INPUT-01")
        self.assertEqual((stats["date"], stats["today_count"]), ("2026-11-01", 0))
        self.assertEqual(store_via("INPUT-01", "INPUT-01-A", ho=103)["today_count"], 1)

    def test_old_db_gets_input_kiosk_column(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "old.db"
            con = sqlite3.connect(path)
            con.execute("CREATE TABLE items (receipt_no TEXT PRIMARY KEY, dong INTEGER, ho INTEGER, "
                        "size TEXT, mode TEXT, status TEXT, pickup_code TEXT, registered_at TEXT, "
                        "tracking_no TEXT, input_client TEXT, slot_id TEXT, job_id TEXT, "
                        "fail_count INTEGER DEFAULT 0, stored_at TEXT, updated_at TEXT)")
            con.close()
            store = Store(path)
            columns = {r["name"] for r in store.db.execute("PRAGMA table_info(items)")}
            store.db.close()
        self.assertIn("input_kiosk", columns)

    # --- 로봇 배달 -------------------------------------------------------
    def test_single_item_waits_for_partner(self):
        r = self.store_item()
        self.assertEqual(self.published(T.TOPIC_FMS_JOB, "DELIVERY_JOB"), [])
        self.assertEqual(self.status(r), "STORED")
        self.core.heartbeat()
        self.assertEqual(self.published(T.TOPIC_FMS_JOB, "DELIVERY_JOB"), [])

    def test_full_delivery_flow(self):
        r, partner, job_id = self.store_pair()
        jobs = self.published(T.TOPIC_FMS_JOB, "DELIVERY_JOB")
        self.assertEqual(len(jobs), 1)
        self.assertEqual({i["receipt_no"] for i in jobs[0]["items"]}, {r, partner})
        self.assertEqual((self.status(r), self.status(partner)), ("JOB_ASSIGNED", "JOB_ASSIGNED"))

        self.fms(type="ROBOT_ARRIVED", job_id=job_id, robot_id="R1")
        bundle = self.published(T.TOPIC_FMS_BUNDLE, "BUNDLE_CONFIRMED")[-1]
        # 동·호 순으로 TRAY_A, TRAY_B 에 하나씩
        self.assertEqual([(i["receipt_no"], i["tray"]) for i in bundle["items"]],
                         [(partner, "TRAY_A"), (r, "TRAY_B")])
        self.assertEqual(set(bundle["items"][0]), {"receipt_no", "dong", "ho", "size", "tray"})
        self.assertEqual(bundle["summary"]["trays"], {"TRAY_A": partner, "TRAY_B": r})
        self.assertEqual(self.status(r), "LOADED")

        self.fms(type="DELIVERY_RESULT", job_id=job_id, receipt_no=r, result="DELIVERED")
        self.fms(type="DELIVERY_RESULT", job_id=job_id, receipt_no=partner, result="DELIVERED")
        self.fms(type="JOB_DONE", job_id=job_id)
        self.assertEqual((self.status(r), self.status(partner)), ("DELIVERED", "DELIVERED"))
        self.assertEqual(self.store.get_job(job_id)["status"], "DONE")

    def test_third_item_waits_for_next_bundle(self):
        a, b, job_id = self.store_pair(ho=101, partner_ho=102)
        c = self.store_item(ho=103)                       # Job 진행 중 → 다음 묶음 대기
        self.assertEqual(self.status(c), "STORED")
        self.fms(type="ROBOT_ARRIVED", job_id=job_id, robot_id="R1")
        bundle = self.published(T.TOPIC_FMS_BUNDLE, "BUNDLE_CONFIRMED")[-1]
        self.assertEqual({i["receipt_no"] for i in bundle["items"]}, {a, b})
        self.assertEqual(self.status(c), "STORED")
        for r in (a, b):
            self.fms(type="DELIVERY_RESULT", job_id=job_id, receipt_no=r, result="DELIVERED")
        self.fms(type="JOB_DONE", job_id=job_id)
        self.assertEqual(len(self.published(T.TOPIC_FMS_JOB, "DELIVERY_JOB")), 1)  # c 혼자는 출발 안 함

    def test_late_binding_replaces_item_picked_before_arrival(self):
        a, b, job_id = self.store_pair(ho=101, partner_ho=102)
        c = self.store_item(ho=103)
        session = self.login(user="101-102")
        self.assertEqual(self.req(T.TOPIC_RETRIEVE, client="OUTPUT-01", session=session,
                                  receipt_no=b)["status"], "OK")
        self.fms(type="ROBOT_ARRIVED", job_id=job_id, robot_id="R1")
        bundle = self.published(T.TOPIC_FMS_BUNDLE, "BUNDLE_CONFIRMED")[-1]
        self.assertEqual({i["receipt_no"] for i in bundle["items"]}, {a, c})

    def test_duplicate_robot_arrived_resends_same_bundle(self):
        _, _, job_id = self.store_pair()
        self.fms(type="ROBOT_ARRIVED", job_id=job_id, robot_id="R1")
        self.fms(type="ROBOT_ARRIVED", job_id=job_id, robot_id="R1")
        bundles = self.published(T.TOPIC_FMS_BUNDLE, "BUNDLE_CONFIRMED")
        self.assertEqual(len(bundles), 2)
        self.assertEqual(bundles[0]["items"], bundles[1]["items"])

    def test_two_failures_switch_to_direct(self):
        r = self.store_item()
        for n in range(T.MAX_FAIL):
            self.clock.advance(1)
            partner = self.store_item(ho=401 + n)          # 매 배송 짝 물품
            job_id = self.published(T.TOPIC_FMS_JOB)[-1]["job_id"]
            self.fms(type="ROBOT_ARRIVED", job_id=job_id, robot_id="R1")
            self.fms(type="DELIVERY_RESULT", job_id=job_id, receipt_no=partner, result="DELIVERED")
            self.fms(type="DELIVERY_RESULT", job_id=job_id, receipt_no=r, result="FAILED")
            self.fms(type="JOB_DONE", job_id=job_id)
        item = self.store.get_item(r)
        self.assertEqual((item.status, item.mode, item.fail_count), ("STORED", "DIRECT", 2))

    # --- 출력 키오스크 ---------------------------------------------------
    def test_pickup_before_robot_loads(self):
        r, partner, job_id = self.store_pair()
        session = self.login()
        res = self.req(T.TOPIC_RETRIEVE, client="OUTPUT-01", session=session, receipt_no=r)
        self.assertEqual(res["status"], "OK")
        self.assertEqual(self.status(r), "PICKED")
        # 도착 시점에 1개뿐이면 적재하지 않고 취소한다. 남은 물품은 다음 짝을 기다린다.
        self.fms(type="ROBOT_ARRIVED", job_id=job_id, robot_id="R1")
        self.assertTrue(self.published(T.TOPIC_FMS_BUNDLE, "JOB_CANCELLED"))
        self.assertFalse(self.published(T.TOPIC_FMS_BUNDLE, "BUNDLE_CONFIRMED"))
        self.assertEqual(self.status(partner), "STORED")

    def test_pickup_after_loaded_is_rejected(self):
        r, _, job_id = self.store_pair()
        self.fms(type="ROBOT_ARRIVED", job_id=job_id, robot_id="R1")
        session = self.login()
        res = self.req(T.TOPIC_RETRIEVE, client="OUTPUT-01", session=session, receipt_no=r)
        self.assertEqual(res["status"], "ALREADY_LOADED")

    def test_parcels_update_pushed_to_logged_in_kiosk(self):
        self.login()
        r = self.store_item()
        pushes = self.published(T.TOPIC_CLIENT_RES.format(client_id="OUTPUT-01"), "PARCELS_UPDATE")
        self.assertIn(r, [p["receipt_no"] for p in pushes[-1]["parcels"]])

    def test_other_household_cannot_retrieve(self):
        r = self.store_item(ho=412)
        session = self.login(user="101-411")
        res = self.req(T.TOPIC_RETRIEVE, client="OUTPUT-01", session=session, receipt_no=r)
        self.assertEqual(res["status"], "NOT_FOUND")

    # --- 수령 방식 -------------------------------------------------------
    def test_default_direct_blocks_robot_jobs(self):
        session = self.login()
        res = self.req(T.TOPIC_DEFAULT_MODE, client="OUTPUT-01", session=session, mode="DIRECT")
        self.assertEqual(res["status"], "OK")
        r = self.store_item()
        self.store_item()
        self.assertEqual(self.store.get_item(r).mode, "DIRECT")
        self.assertEqual(self.published(T.TOPIC_FMS_JOB), [])

    def test_toggle_reverts_assigned_items(self):
        r, _, _ = self.store_pair(partner_ho=412)
        self.assertEqual(self.status(r), "JOB_ASSIGNED")
        session = self.login()
        res = self.req(T.TOPIC_DEFAULT_MODE, client="OUTPUT-01", session=session, mode="DIRECT")
        self.assertEqual(res["changed"], 2)
        item = self.store.get_item(r)
        self.assertEqual((item.status, item.mode, item.job_id), ("STORED", "DIRECT", None))

    def test_item_mode_rules(self):
        r = self.store_item()
        session = self.login()
        res = self.req(T.TOPIC_ITEM_MODE, client="OUTPUT-01", session=session, receipt_no=r, mode="DIRECT")
        self.assertEqual(res["status"], "OK")
        self.assertEqual(self.store.get_item(r).mode, "DIRECT")
        self.req(T.TOPIC_DEFAULT_MODE, client="OUTPUT-01", session=session, mode="DIRECT")
        res = self.req(T.TOPIC_ITEM_MODE, client="OUTPUT-01", session=session, receipt_no=r, mode="ROBOT")
        self.assertEqual(res["status"], "DENIED")

    def test_expired_session(self):
        session = self.login()
        self.clock.advance(T.SESSION_TTL_SEC + 1)
        res = self.req(T.TOPIC_PARCELS, client="OUTPUT-01", session=session)
        self.assertEqual(res["status"], "EXPIRED_SESSION")

    def test_summary_is_retained_state(self):
        self.store_item()
        summary = self.published(T.TOPIC_SUMMARY)[-1]
        self.assertEqual(summary["occupancy"]["S"]["used"], 1)
        self.assertEqual(summary["waiting"]["ROBOT"], 1)

    # --- 세대 웹 ---------------------------------------------------------
    def resident_login(self, user="101-412"):
        res = self.req(T.TOPIC_LOGIN, client="RESIDENT-WEB", user_id=user, password="1234",
                       channel="RESIDENT")
        self.assertEqual(res["status"], "OK", res)
        return res["session"]

    def resident_parcels(self, session):
        res = self.req(T.TOPIC_RESIDENT_PARCELS, client="RESIDENT-WEB", session=session)
        self.assertEqual(res["status"], "OK", res)
        return res["parcels"]

    def test_resident_session_outlives_kiosk_session(self):
        kiosk = self.login()
        resident = self.resident_login()
        self.clock.advance(T.SESSION_TTL_SEC + 1)
        self.assertEqual(self.req(T.TOPIC_PARCELS, client="OUTPUT-01", session=kiosk)["status"],
                         "EXPIRED_SESSION")
        self.resident_parcels(resident)

    def test_resident_parcels_include_history_and_hide(self):
        r, _, job_id = self.store_pair()
        session = self.resident_login()
        self.assertEqual([(p["receipt_no"], p["status"], p["changeable"]) for p in self.resident_parcels(session)],
                         [(r, "JOB_ASSIGNED", True)])
        self.assertEqual(self.req(T.TOPIC_HIDE, client="RESIDENT-WEB", session=session,
                                  receipt_no=r)["status"], "NOT_COMPLETED")

        self.fms(type="ROBOT_ARRIVED", job_id=job_id, robot_id="R1")
        parcel = self.resident_parcels(session)[0]
        self.assertEqual((parcel["status"], parcel["changeable"]), ("LOADED", False))

        self.fms(type="DELIVERY_RESULT", job_id=job_id, receipt_no=r, result="DELIVERED")
        self.assertEqual(self.resident_parcels(session)[0]["status"], "DELIVERED")
        self.assertEqual(self.req(T.TOPIC_HIDE, client="RESIDENT-WEB", session=session,
                                  receipt_no=r)["status"], "OK")
        self.assertEqual(self.resident_parcels(session), [])

    def test_completed_parcels_drop_after_history_window(self):
        r = self.store_item()
        session = self.login()
        self.req(T.TOPIC_RETRIEVE, client="OUTPUT-01", session=session, receipt_no=r)
        self.assertEqual(self.resident_parcels(self.resident_login())[0]["status"], "PICKED")
        self.clock.advance((T.RESIDENT_HISTORY_DAYS + 1) * 86400)
        self.assertEqual(self.resident_parcels(self.resident_login()), [])

    def test_pickup_code_only_for_own_direct_stored_parcel(self):
        r = self.store_item()
        session = self.resident_login()
        self.assertEqual(self.req(T.TOPIC_PICKUP_CODE, client="RESIDENT-WEB", session=session,
                                  receipt_no=r)["status"], "NOT_AVAILABLE")
        self.req(T.TOPIC_ITEM_MODE, client="RESIDENT-WEB", session=session, receipt_no=r, mode="DIRECT")
        res = self.req(T.TOPIC_PICKUP_CODE, client="RESIDENT-WEB", session=session, receipt_no=r)
        self.assertEqual(res["code"], self.store.get_item(r).pickup_code)
        other = self.resident_login(user="101-411")
        self.assertEqual(self.req(T.TOPIC_PICKUP_CODE, client="RESIDENT-WEB", session=other,
                                  receipt_no=r)["status"], "NOT_FOUND")


    def test_heartbeat_resends_active_job_and_confirmed_bundle(self):
        self.store_pair()
        job_id = self.store.active_jobs()[0]["job_id"]
        self.sent.clear()
        self.core.heartbeat()
        self.assertTrue(self.published(T.TOPIC_FMS_JOB, "DELIVERY_JOB"))
        self.fms(type="ROBOT_ARRIVED", job_id=job_id, robot_id="R1")
        self.sent.clear()
        self.core.heartbeat()
        self.assertTrue(self.published(T.TOPIC_FMS_BUNDLE, "BUNDLE_CONFIRMED"))

    def test_done_before_loading_releases_candidate_items(self):
        receipt, _, job_id = self.store_pair()
        self.fms(type="JOB_DONE", job_id=job_id)
        item = self.store.get_item(receipt)
        self.assertNotEqual(item.job_id, job_id)
        self.assertEqual(item.status, "JOB_ASSIGNED")  # 다음 배송으로 다시 발행한다

    def test_fms_rejection_switches_unroutable_to_direct_and_reissues(self):
        first, second, job_id = self.store_pair()
        third = self.store_item(dong=101, ho=410)
        self.fms(type="JOB_REJECTED", job_id=job_id, reason="세대 101-412 목적지 연결 없음", unroutable=[first])
        self.assertEqual(self.store.get_job(job_id)["status"], "CANCELLED")
        item = self.store.get_item(first)
        self.assertEqual((item.status, item.mode, item.job_id), ("STORED", "DIRECT", None))
        # 나머지는 다음 배송 후보. 갈 수 없는 물품 없이 새 Job 을 발행한다.
        new_job = self.published(T.TOPIC_FMS_JOB, "DELIVERY_JOB")[-1]
        self.assertNotEqual(new_job["job_id"], job_id)
        self.assertEqual({i["receipt_no"] for i in new_job["items"]}, {second, third})
        # 중복 취소·적재 확정 후 취소는 무시
        self.fms(type="JOB_REJECTED", job_id=job_id, reason="중복", unroutable=[second])
        self.assertEqual(self.store.get_item(second).mode, "ROBOT")
        self.fms(type="ROBOT_ARRIVED", job_id=new_job["job_id"], robot_id="R1")
        self.fms(type="JOB_REJECTED", job_id=new_job["job_id"], reason="늦음", unroutable=[second])
        self.assertEqual(self.store.get_job(new_job["job_id"])["status"], "CONFIRMED")
        self.assertEqual(self.status(second), "LOADED")

    def test_outbox_survives_restart_and_transaction_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "locker.db"
            store = Store(path)
            with store.transaction():
                store.enqueue_message(T.TOPIC_FMS_EVENT, {"type": "test"})
            with self.assertRaises(ValueError):
                with store.transaction():
                    store.enqueue_message("should/rollback", {})
                    raise ValueError("rollback")
            store.db.close()
            restored = Store(path)
            messages = restored.pending_messages()
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0]["topic"], T.TOPIC_FMS_EVENT)
            restored.db.close()

    def test_publish_failure_keeps_durable_outbox(self):
        def disconnected(*args, **kwargs):
            raise ConnectionError("offline")
        self.core.publish = disconnected
        self.core.handle(T.TOPIC_REGISTER, {"client_id": "INPUT-01", "request_id": "offline-register",
                                           "dong": 101, "ho": 101, "size": "S"})
        self.assertTrue(self.store.pending_messages())
        self.core.publish = lambda *args, **kwargs: None
        self.core.heartbeat()
        self.assertFalse(self.store.pending_messages())


if __name__ == "__main__":
    unittest.main()

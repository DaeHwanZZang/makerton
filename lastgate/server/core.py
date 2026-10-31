"""보관소 서버 핵심 로직 (MQTT 와 분리되어 있어 단위 테스트 가능).

- 모든 요청은 락 하나로 직렬화하고, 요청 하나를 SQLite 트랜잭션 하나로 처리한다.
- 발행할 메시지는 outbox 에 모았다가 커밋 후에 보낸다 (롤백 시 버림).
- 클라이언트 응답은 요청자 전용 토픽(locker/client/{client_id}/res)으로만 보낸다.
- (client_id, request_id) 로 중복 요청을 걸러 같은 응답을 다시 보낸다 (QoS 1 대비).
"""
from __future__ import annotations

import random
import re
import threading
import traceback
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Callable

import config
from auth import Session, SessionManager, hash_password, verify_password
from bundler import build_bundle
from fms_adapter import FmsAdapter
from models import (ACTIVE_STATUSES, COMPLETED_STATUSES, MODE_CHANGEABLE_STATUSES,
                    PICKABLE_STATUSES, Item, Mode, Status)
from storage import StorageAdapter
from store import Store

RESPONSE_TYPES = {
    config.TOPIC_REGISTER: "REGISTER_RESPONSE",
    config.TOPIC_PORT_CONFIRM: "REGISTER_CONFIRMED",
    config.TOPIC_INPUT_STATS: "INPUT_STATS_RESPONSE",
    config.TOPIC_LOGIN: "LOGIN_RESPONSE",
    config.TOPIC_LOGOUT: "LOGOUT_RESPONSE",
    config.TOPIC_PARCELS: "PARCELS_RESPONSE",
    config.TOPIC_RETRIEVE: "RETRIEVE_RESPONSE",
    config.TOPIC_DEFAULT_MODE: "DEFAULT_MODE_RESPONSE",
    config.TOPIC_ITEM_MODE: "ITEM_MODE_RESPONSE",
    config.TOPIC_RESIDENT_PARCELS: "RESIDENT_PARCELS_RESPONSE",
    config.TOPIC_PICKUP_CODE: "PICKUP_CODE_RESPONSE",
    config.TOPIC_HIDE: "HIDE_RESPONSE",
}


def log(tag: str, message: str):
    print(f"[{config.now_kst():%H:%M:%S}] [{tag}] {message}", flush=True)


class RequestError(Exception):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class LockerCore:
    IDEMPOTENCY_CACHE = 2000

    def __init__(self, store: Store, storage: StorageAdapter, publish: Callable[..., None],
                 now_fn: Callable[[], datetime] = config.now_kst):
        self.store = store
        self.storage = storage
        self.publish = publish
        self.now = now_fn
        self.lock = threading.RLock()
        self.sessions = SessionManager()
        self.fms = FmsAdapter(self._emit, now_fn)
        self._outbox: list[tuple[str, dict, bool]] = []
        self._responses: OrderedDict[tuple[str, str], dict] = OrderedDict()
        self._touched: set[tuple[int, int]] = set()
        self._dirty = True
        if hasattr(storage, "on_log"):
            storage.on_log = self._storage_log
        self.client_routes = {
            config.TOPIC_REGISTER: self._on_register,
            config.TOPIC_PORT_CONFIRM: self._on_port_confirm,
            config.TOPIC_INPUT_STATS: self._on_input_stats,
            config.TOPIC_LOGIN: self._on_login,
            config.TOPIC_LOGOUT: self._on_logout,
            config.TOPIC_PARCELS: self._on_parcels,
            config.TOPIC_RETRIEVE: self._on_retrieve,
            config.TOPIC_DEFAULT_MODE: self._on_default_mode,
            config.TOPIC_ITEM_MODE: self._on_item_mode,
            config.TOPIC_RESIDENT_PARCELS: self._on_resident_parcels,
            config.TOPIC_PICKUP_CODE: self._on_pickup_code,
            config.TOPIC_HIDE: self._on_hide,
        }

    @property
    def topics(self) -> list[str]:
        return [*self.client_routes, *self.fms.topics]

    # --- 시작 ------------------------------------------------------------
    def bootstrap(self):
        with self.lock:
            if self.store.households_empty():
                log("초기화", f"세대 계정 생성 중… (아이디 '동-호', 초기 비밀번호 {config.SEED_PASSWORD})")
                rows = [(d, h, f"{d}-{h}", hash_password(config.SEED_PASSWORD))
                        for d, h in config.all_units()]
                self.store.seed_households(rows, config.DEFAULT_MODE)
                log("초기화", f"{len(rows)}세대 계정 생성 완료")
            self._resync_storage()
            with self.store.transaction():
                self._issue_jobs()
            self._flush()

    # --- 진입점 ----------------------------------------------------------
    def handle(self, topic: str, payload: dict):
        with self.lock:
            if topic in self.client_routes:
                self._handle_client(topic, payload)
            elif topic in self.fms.topics:
                mark = len(self._outbox)
                try:
                    with self.store.transaction():
                        self._on_fms_event(payload)
                except Exception:
                    log("오류", traceback.format_exc())
                    del self._outbox[mark:]
                    self._resync_storage()
            self._flush()

    def heartbeat(self):
        """주기 호출: 투입 시간 초과 취소, 세션 정리, Job 재검사, 상태 하트비트."""
        with self.lock:
            mark = len(self._outbox)
            try:
                with self.store.transaction():
                    now = self.now()
                    for item in self.store.items([Status.REGISTERED]):
                        waited = (now - datetime.fromisoformat(item.registered_at)).total_seconds()
                        if waited >= config.PORT_TIMEOUT_SEC:
                            item.status = Status.CANCELLED
                            self._save(item)
                            log("접수취소", f"{item.receipt_no} 투입 시간 초과")
                            if item.input_client:
                                self._send(item.input_client, {
                                    "type": "REGISTER_CANCELLED", "receipt_no": item.receipt_no,
                                    "reason": "TIMEOUT",
                                    "message": "투입이 확인되지 않아 접수가 취소되었습니다."})
                    self.sessions.purge(now)
                    self._issue_jobs()
                    # 연결 단절·서버 재시작 후에도 같은 Job/최종 묶음을 다시 알린다.
                    for active in self.store.active_jobs():
                        job = self.store.get_job(active["job_id"])
                        self.fms.resend(job["bundle"])
            except Exception:
                log("오류", traceback.format_exc())
                del self._outbox[mark:]
                self._resync_storage()
            self._dirty = True
            self._flush()

    def _handle_client(self, topic: str, payload: dict):
        client_id = str(payload.get("client_id") or "")
        request_id = str(payload.get("request_id") or "")
        if not client_id or not request_id:
            log("경고", f"client_id/request_id 없는 요청 무시: {topic}")
            return
        key = (client_id, request_id)
        if key in self._responses:
            self._send(client_id, self._responses[key])
            return
        mark = len(self._outbox)
        try:
            with self.store.transaction():
                response = self.client_routes[topic](payload, client_id)
        except RequestError as e:
            del self._outbox[mark:]
            response = {"type": RESPONSE_TYPES[topic], "status": e.status, "message": e.message}
        except Exception:
            log("오류", traceback.format_exc())
            del self._outbox[mark:]
            self._resync_storage()
            response = {"type": RESPONSE_TYPES[topic], "status": "ERROR",
                        "message": "서버 오류가 발생했습니다. 다시 시도해 주세요."}
        response["request_id"] = request_id
        self._responses[key] = response
        while len(self._responses) > self.IDEMPOTENCY_CACHE:
            self._responses.popitem(last=False)
        self._send(client_id, response)

    # --- 입력 키오스크 ---------------------------------------------------
    def _on_register(self, p: dict, client_id: str) -> dict:
        dong, ho = _to_int(p.get("dong")), _to_int(p.get("ho"))
        size = str(p.get("size") or "").upper()
        tracking = str(p.get("tracking_no") or "").strip() or None
        kiosk = str(p.get("kiosk_id") or "").strip() or None
        if dong is None or ho is None:
            raise RequestError("REJECT", "동과 호수를 입력하세요.")
        ok, _, reason = config.parse_unit(dong, ho)
        if not ok:
            raise RequestError("REJECT", reason)
        if size not in config.SIZES:
            raise RequestError("REJECT", "택배 크기를 선택하세요 (S/M/L).")
        if tracking and not re.fullmatch(config.TRACKING_NO_RE, tracking):
            raise RequestError("REJECT", "송장번호는 영문·숫자 6~40자리입니다.")
        pending = [it.size for it in self.store.items([Status.REGISTERED])]
        if not self.storage.can_accept(pending + [size]):
            raise RequestError("PORT_FULL", f"{size} 크기 보관 공간이 부족합니다. 관리실에 맡겨 주세요.")

        now = self.now()
        item = Item(
            receipt_no=self._new_receipt(now), dong=dong, ho=ho, size=size,
            mode=self.store.get_default_mode(dong, ho) or config.DEFAULT_MODE,
            status=Status.REGISTERED, pickup_code=self._new_pickup_code(),
            registered_at=now.isoformat(), tracking_no=tracking, input_client=client_id,
            input_kiosk=kiosk, updated_at=now.isoformat())
        self.store.insert_item(item)
        self._dirty = True
        log("접수", f"{item.receipt_no} {dong}동 {ho}호 [{size}] → {size} 입력구 투입 대기")
        # 인증코드는 기사 화면에 보내지 않는다
        return {"type": "REGISTER_RESPONSE", "status": "OK", "receipt_no": item.receipt_no,
                "input_port": size, "timeout_sec": config.PORT_TIMEOUT_SEC}

    def _on_port_confirm(self, p: dict, client_id: str) -> dict:
        receipt = str(p.get("receipt_no") or "")
        port = str(p.get("port") or "").upper()
        source = str(p.get("source") or "KIOSK")
        item = self.store.get_item(receipt)
        if not item:
            raise RequestError("NOT_FOUND", "접수 정보를 찾을 수 없습니다.")
        if item.status == Status.CANCELLED:
            raise RequestError("CANCELLED", "투입 시간이 지나 접수가 취소되었습니다. 다시 접수해 주세요.")
        if item.status != Status.REGISTERED:      # 이미 보관됨 (중복 감지)
            return {"type": "REGISTER_CONFIRMED", "status": "OK", "receipt_no": receipt,
                    **self._today_stats(item.input_kiosk)}
        if port and port != item.size:
            raise RequestError("PORT_MISMATCH", f"{item.size} 입력구에 넣어주세요.")

        slot_id = self.storage.place(item)
        if not slot_id:                            # 예약했으므로 정상이라면 발생하지 않음
            item.status = Status.CANCELLED
            self._save(item)
            return {"type": "REGISTER_CONFIRMED", "status": "PORT_FULL", "receipt_no": receipt,
                    "message": "보관 공간이 부족합니다. 관리실에 맡겨 주세요."}
        item.status = Status.STORED
        item.slot_id = slot_id
        item.stored_at = self.now().isoformat()
        self._save(item)
        log("보관", f"{receipt} {item.dong}동 {item.ho}호 [{item.size}] → {slot_id} "
                   f"({item.mode}, 감지: {source})")
        self._issue_jobs()
        return {"type": "REGISTER_CONFIRMED", "status": "OK", "receipt_no": receipt,
                **self._today_stats(item.input_kiosk)}

    def _on_input_stats(self, p: dict, client_id: str) -> dict:
        kiosk = str(p.get("kiosk_id") or "").strip()
        if not kiosk:
            raise RequestError("INVALID", "kiosk_id 가 필요합니다.")
        return {"type": "INPUT_STATS_RESPONSE", "status": "OK", "kiosk_id": kiosk,
                **self._today_stats(kiosk)}

    def _today_stats(self, kiosk: str | None) -> dict:
        """키오스크의 오늘(KST) 등록 건수. 날짜를 함께 보내 화면이 자정 경계를 알 수 있게 한다."""
        today = self.now().astimezone(config.KST).date().isoformat()
        return {"date": today, "today_count": self.store.count_stored_on(kiosk, today) if kiosk else 0}

    # --- 출력 키오스크 / 세대 웹 -----------------------------------------
    def _on_login(self, p: dict, client_id: str) -> dict:
        user_id = str(p.get("user_id") or "").strip()
        account = self.store.get_account(user_id)
        if not account or not verify_password(str(p.get("password") or ""), account["password_hash"]):
            raise RequestError("DENIED", "아이디 또는 비밀번호가 올바르지 않습니다.")
        # 세대 웹은 개인 기기라 오래 유지하고, 공용 키오스크는 짧게 유지한다
        resident = str(p.get("channel") or "").upper() == "RESIDENT"
        ttl = config.RESIDENT_SESSION_TTL_SEC if resident else config.SESSION_TTL_SEC
        session = self.sessions.create(user_id, account["dong"], account["ho"], client_id,
                                       self.now(), ttl)
        log("로그인", f"{user_id} ({client_id}{', 세대 웹' if resident else ''})")
        return {"type": "LOGIN_RESPONSE", "status": "OK", "session": session.token,
                "household": self._household(session.dong, session.ho),
                "parcels": self._parcels(session.dong, session.ho)}

    def _on_logout(self, p: dict, client_id: str) -> dict:
        self.sessions.revoke(p.get("session"))
        return {"type": "LOGOUT_RESPONSE", "status": "OK"}

    def _on_parcels(self, p: dict, client_id: str) -> dict:
        s = self._session(p)
        return {"type": "PARCELS_RESPONSE", "status": "OK",
                "household": self._household(s.dong, s.ho), "parcels": self._parcels(s.dong, s.ho)}

    def _on_retrieve(self, p: dict, client_id: str) -> dict:
        s = self._session(p)
        receipt = str(p.get("receipt_no") or "")
        item = self.store.get_item(receipt)
        if not item or item.household != (s.dong, s.ho):
            raise RequestError("NOT_FOUND", "해당 택배를 찾을 수 없습니다.")
        if item.status in (Status.LOADED, Status.DELIVERED):
            raise RequestError("ALREADY_LOADED", "이미 로봇에 적재된 택배입니다.")
        if item.status not in PICKABLE_STATUSES:
            raise RequestError("NOT_FOUND", "보관 중인 택배가 아닙니다.")
        self.storage.retrieve(item, "PICKUP")
        item.status = Status.PICKED
        item.slot_id = None
        item.job_id = None
        self._save(item)
        log("수령", f"{receipt} {item.dong}동 {item.ho}호 → 수령 출력구")
        return {"type": "RETRIEVE_RESPONSE", "status": "OK", "receipt_no": receipt,
                "out_port": "PICKUP"}

    def _on_default_mode(self, p: dict, client_id: str) -> dict:
        s = self._session(p)
        mode = _to_mode(p.get("mode"))
        self.store.set_default_mode(s.dong, s.ho, mode)
        changed = 0
        for item in self.store.items(MODE_CHANGEABLE_STATUSES, household=(s.dong, s.ho)):
            if item.mode != mode:
                self._apply_mode(item, mode)
                changed += 1
        self._touched.add((s.dong, s.ho))
        self._dirty = True
        log("수령방식", f"{s.dong}동 {s.ho}호 기본값 → {mode} (물품 {changed}건 일괄 변경)")
        self._issue_jobs()
        return {"type": "DEFAULT_MODE_RESPONSE", "status": "OK", "default_mode": mode,
                "changed": changed, "parcels": self._parcels(s.dong, s.ho)}

    def _on_item_mode(self, p: dict, client_id: str) -> dict:
        s = self._session(p)
        mode = _to_mode(p.get("mode"))
        receipt = str(p.get("receipt_no") or "")
        item = self.store.get_item(receipt)
        if not item or item.household != (s.dong, s.ho):
            raise RequestError("NOT_FOUND", "해당 택배를 찾을 수 없습니다.")
        if self.store.get_default_mode(s.dong, s.ho) != Mode.ROBOT:
            raise RequestError("DENIED", "세대 기본값이 '직접 수령'이면 물품별로 바꿀 수 없습니다.")
        if item.status not in PICKABLE_STATUSES:
            raise RequestError("NOT_CHANGEABLE", "이미 로봇에 적재되었거나 보관 중이 아닌 택배입니다.")
        if item.mode != mode:
            self._apply_mode(item, mode)
            log("수령방식", f"{receipt} → {mode}")
            self._issue_jobs()
        return {"type": "ITEM_MODE_RESPONSE", "status": "OK", "receipt_no": receipt, "mode": mode}

    # --- 세대 웹 ---------------------------------------------------------
    def _on_resident_parcels(self, p: dict, client_id: str) -> dict:
        s = self._session(p)
        since = (self.now() - timedelta(days=config.RESIDENT_HISTORY_DAYS)).isoformat()
        items = self.store.resident_items(
            (s.dong, s.ho), [*PICKABLE_STATUSES, Status.LOADED], COMPLETED_STATUSES, since)
        return {"type": "RESIDENT_PARCELS_RESPONSE", "status": "OK",
                "household": self._household(s.dong, s.ho),
                "parcels": [{"receipt_no": it.receipt_no, "tracking_no": it.tracking_no,
                             "size": it.size, "status": it.status, "mode": it.mode,
                             "registered_at": it.registered_at, "stored_at": it.stored_at,
                             "updated_at": it.updated_at,
                             "changeable": it.status in PICKABLE_STATUSES}
                            for it in items]}

    def _on_pickup_code(self, p: dict, client_id: str) -> dict:
        s = self._session(p)
        item = self.store.get_item(str(p.get("receipt_no") or ""))
        if not item or item.household != (s.dong, s.ho) or item.hidden_at:
            raise RequestError("NOT_FOUND", "내 세대의 택배를 찾을 수 없습니다.")
        if item.status not in PICKABLE_STATUSES or item.mode != Mode.DIRECT:
            raise RequestError("NOT_AVAILABLE", "직접 수령으로 보관 중인 택배만 인증코드를 볼 수 있습니다.")
        return {"type": "PICKUP_CODE_RESPONSE", "status": "OK", "receipt_no": item.receipt_no,
                "code": item.pickup_code}

    def _on_hide(self, p: dict, client_id: str) -> dict:
        s = self._session(p)
        item = self.store.get_item(str(p.get("receipt_no") or ""))
        if not item or item.household != (s.dong, s.ho):
            raise RequestError("NOT_FOUND", "내 세대의 택배를 찾을 수 없습니다.")
        if item.status not in COMPLETED_STATUSES:
            raise RequestError("NOT_COMPLETED", "배송·수령이 끝난 택배만 목록에서 삭제할 수 있습니다.")
        if not item.hidden_at:
            item.hidden_at = self.now().isoformat()
            self._save(item)
        return {"type": "HIDE_RESPONSE", "status": "OK", "receipt_no": item.receipt_no}

    # --- FMS -------------------------------------------------------------
    def _on_fms_event(self, payload: dict):
        event = self.fms.parse_event(payload)
        if not event:
            log("FMS", f"해석할 수 없는 메시지 무시: {payload.get('type')}")
            return
        job = self.store.get_job(event["job_id"])
        if not job:
            log("FMS", f"알 수 없는 Job 무시: {event['job_id']}")
            return
        if event["kind"] == "ROBOT_ARRIVED":
            self._on_robot_arrived(job, event.get("robot_id"))
        elif event["kind"] == "DELIVERY_RESULT":
            self._on_delivery_result(job, event)
        elif event["kind"] == "JOB_DONE":
            self._on_job_done(job)
        elif event["kind"] == "JOB_REJECTED":
            self._on_job_rejected(job, event)

    def _on_robot_arrived(self, job: dict, robot_id: str | None):
        job_id = job["job_id"]
        if job["status"] in ("CONFIRMED", "CANCELLED"):    # 중복 도착 → 같은 메시지 재전송
            self.fms.resend(job["bundle"])
            return
        if job["status"] != "PENDING":
            return
        now = self.now()
        assigned = self.store.items([Status.JOB_ASSIGNED], job_id=job_id)
        expected = [it for it in assigned if it.mode == Mode.ROBOT]
        pool = expected + self.store.items([Status.STORED], mode=Mode.ROBOT, unassigned=True)
        # 도착 시점에 다시 2개인지 확인한다. 예상 물품이 그대로면 유지하고, 직접 수령 등으로
        # 줄었을 때만 대기 물품으로 채운다. 그래도 1개면 적재하지 않는다.
        bundle = build_bundle(expected, now) or build_bundle(pool, now)
        chosen = {it.receipt_no for it in bundle.items} if bundle else set()
        for it in assigned:
            if it.receipt_no not in chosen:
                it.status, it.job_id = Status.STORED, None
                self._save(it)
        if not bundle:
            payload = self.fms.send_cancel(job_id, "NO_ITEMS")
            self.store.update_job(job_id, status="CANCELLED", robot_id=robot_id,
                                  done_at=now.isoformat(), bundle=payload)
            self._dirty = True
            log("Job", f"{job_id} 취소 — 로봇 도착 시점에 실을 물품이 {config.BUNDLE_SIZE}개 미만")
            return

        self.storage.load_robot(bundle.items)
        for it in bundle.items:
            it.status, it.slot_id, it.job_id = Status.LOADED, None, job_id
            self._save(it)
        payload = self.fms.send_bundle(job_id, robot_id, bundle)
        self.store.update_job(job_id, status="CONFIRMED", robot_id=robot_id,
                              confirmed_at=now.isoformat(), bundle=payload, metrics=bundle.metrics)
        self.store.log_event(now.isoformat(), "BUNDLE", {"job_id": job_id, **bundle.metrics})
        trays = ", ".join(f"{bundle.trays[it.receipt_no]}={it.receipt_no}" for it in bundle.items)
        log("적재확정", f"{job_id} 로봇 {robot_id} · {trays} · {bundle.metrics['weight_kg']}kg")

    def _on_delivery_result(self, job: dict, event: dict):
        item = self.store.get_item(event["receipt_no"])
        if not item or item.job_id != job["job_id"] or item.status != Status.LOADED:
            return                                          # 중복·무관한 결과
        if event["result"] == "DELIVERED":
            item.status = Status.DELIVERED
            self._save(item)
            log("배달완료", f"{item.receipt_no} {item.dong}동 {item.ho}호")
        else:
            self._return_failed(item, event.get("reason") or "배달 실패")

    def _on_job_done(self, job: dict):
        if job["status"] in ("DONE", "CANCELLED"):
            return
        for item in self.store.items([Status.LOADED], job_id=job["job_id"]):
            self._return_failed(item, "결과 미보고")
        for item in self.store.items([Status.JOB_ASSIGNED], job_id=job["job_id"]):
            item.status, item.job_id = Status.STORED, None
            self._save(item)
        self.store.update_job(job["job_id"], status="DONE", done_at=self.now().isoformat())
        self._dirty = True
        log("Job", f"{job['job_id']} 완료")
        self._issue_jobs()

    def _on_job_rejected(self, job: dict, event: dict):
        """FMS 가 로봇 배정 전에 배송을 취소했다 (예: 세대 목적지 연결 없음).

        로봇에 싣기 전이므로 물품은 보관 셀에 그대로 있다. FMS 가 갈 수 없다고 한 세대의 물품은
        직접 수령으로 바꿔 같은 묶음이 다시 발행되어 멈추는 것을 막고, 나머지는 다음 배송 후보로 돌린다.
        """
        if job["status"] != "PENDING":
            if job["status"] == "CONFIRMED":
                log("FMS", f"{job['job_id']} 취소 무시 — 이미 적재 확정됨")
            return
        unroutable = set(event["unroutable"])
        direct = []
        for item in self.store.items([Status.JOB_ASSIGNED], job_id=job["job_id"]):
            item.status, item.job_id = Status.STORED, None
            if item.receipt_no in unroutable and item.mode == Mode.ROBOT:
                self._apply_mode(item, Mode.DIRECT)    # 수령 방식에 맞는 보관 구역으로 재배치 포함
                direct.append(f"{item.receipt_no}({item.dong}-{item.ho})")
            else:
                self._save(item)
        now = self.now()
        self.store.update_job(job["job_id"], status="CANCELLED", done_at=now.isoformat())
        self.store.log_event(now.isoformat(), "JOB_REJECTED",
                             {"job_id": job["job_id"], "reason": event["reason"], "direct": direct})
        self._dirty = True
        log("Job", f"{job['job_id']} FMS 취소 — {event['reason'] or '사유 없음'}"
                   + (f" · 직접 수령 전환 {', '.join(direct)}" if direct else ""))
        self._issue_jobs()

    def _return_failed(self, item: Item, reason: str):
        """배달 실패 물품 재입고. MAX_FAIL 회 실패하면 직접 수령으로 전환."""
        item.fail_count += 1
        item.job_id = None
        if item.fail_count >= config.MAX_FAIL:
            item.mode = Mode.DIRECT
        slot_id = self.storage.place(item)
        if slot_id:
            item.status, item.slot_id = Status.STORED, slot_id
        else:
            item.status = Status.CANCELLED
        self._save(item)
        log("배달실패", f"{item.receipt_no} {reason} (누적 {item.fail_count}회, "
                      f"{'재입고 ' + slot_id if slot_id else '보관 공간 없음 → 관리실'}, {item.mode})")

    # --- Job 발행 --------------------------------------------------------
    def _issue_jobs(self):
        """로봇 배달 대상 물품이 BUNDLE_SIZE(2)개 모이면 Job 을 보낸다. 1개는 보관소에서 대기한다."""
        while len(self.store.active_jobs()) < config.MAX_ACTIVE_JOBS:
            now = self.now()
            bundle = build_bundle(self.store.items([Status.STORED], mode=Mode.ROBOT, unassigned=True), now)
            if not bundle:
                return
            job_id = self._new_job_id(now)
            for it in bundle.items:
                it.status, it.job_id = Status.JOB_ASSIGNED, job_id
                self._save(it)
            payload = self.fms.send_job(job_id, bundle)
            self.store.insert_job(job_id, now.isoformat(), payload, bundle.metrics)
            log("Job", f"{job_id} 발행 (예상) · {len(bundle.items)}건 · 동 {bundle.metrics['dongs']}")

    # --- 보조 ------------------------------------------------------------
    def _apply_mode(self, item: Item, mode: str):
        item.mode = mode
        if item.status == Status.JOB_ASSIGNED and mode == Mode.DIRECT:
            item.status, item.job_id = Status.STORED, None
        if item.slot_id:
            new_slot = self.storage.reslot(item)
            if new_slot:
                item.slot_id = new_slot
        self._save(item)

    def _session(self, p: dict) -> Session:
        session = self.sessions.get(p.get("session"), self.now())
        if not session:
            raise RequestError("EXPIRED_SESSION", "로그인이 만료되었습니다. 다시 로그인해 주세요.")
        return session

    def _household(self, dong: int, ho: int) -> dict:
        return {"dong": dong, "ho": ho, "default_mode": self.store.get_default_mode(dong, ho)}

    def _parcels(self, dong: int, ho: int) -> list[dict]:
        return [{"receipt_no": it.receipt_no, "size": it.size, "arrived_at": it.stored_at,
                 "mode": it.mode}
                for it in self.store.items(PICKABLE_STATUSES, household=(dong, ho))]

    def _save(self, item: Item):
        item.updated_at = self.now().isoformat()
        self.store.update_item(item)
        self._touched.add(item.household)
        self._dirty = True

    def _new_receipt(self, now: datetime) -> str:
        while True:
            receipt = f"{now:%Y%m%d}-{uuid.uuid4().hex[:8].upper()}"
            if not self.store.receipt_exists(receipt):
                return receipt

    def _new_pickup_code(self) -> str:
        active = self.store.active_pickup_codes(ACTIVE_STATUSES)
        while True:
            code = f"{random.randint(0, 999_999):06d}"
            if code not in active:
                return code

    def _new_job_id(self, now: datetime) -> str:
        prefix = f"J{now:%Y%m%d}-"
        return f"{prefix}{self.store.count_jobs_with_prefix(prefix) + 1:04d}"

    def _resync_storage(self):
        self.storage.restore(self.store.items([Status.STORED, Status.JOB_ASSIGNED]))

    def _storage_log(self, kind: str, data: dict):
        at = self.now().isoformat()
        self.store.log_event(at, kind, data)
        self._emit(config.TOPIC_STORAGE_LOG, {"type": kind, "at": at, **data})

    # --- 발행 ------------------------------------------------------------
    def _emit(self, topic: str, payload: dict, retain: bool = False):
        self._outbox.append((topic, payload, retain))
        self.store.enqueue_message(topic, payload, retain)

    def _send(self, client_id: str, payload: dict):
        self._emit(config.TOPIC_CLIENT_RES.format(client_id=client_id), payload)

    def summary(self) -> dict:
        waiting = self.store.mode_counts(PICKABLE_STATUSES)
        return {
            "type": "SUMMARY",
            "timestamp": self.now().isoformat(),
            "occupancy": self.storage.occupancy(),
            "status": self.store.status_counts(),
            "waiting": {"ROBOT": waiting.get(Mode.ROBOT.value, 0),
                        "DIRECT": waiting.get(Mode.DIRECT.value, 0)},
            "active_jobs": self.store.active_jobs(),
        }

    def _flush(self):
        """커밋된 변경을 발행: 세션 목록 갱신 → 상태 요약 → outbox."""
        for dong, ho in self._touched:
            parcels = None
            for session in self.sessions.for_household(dong, ho):
                parcels = parcels if parcels is not None else self._parcels(dong, ho)
                self._send(session.client_id, {"type": "PARCELS_UPDATE",
                                               "household": self._household(dong, ho),
                                               "parcels": parcels})
        self._touched.clear()
        if self._dirty:
            self._emit(config.TOPIC_SUMMARY, self.summary(), retain=True)
            self._dirty = False
        self._outbox = []
        import json
        for message in self.store.pending_messages():
            try:
                self.publish(message["topic"], json.loads(message["payload"]), retain=bool(message["retain"]))
            except Exception as exc:
                log("MQTT", f"발행 보류 (재연결 후 재시도): {exc}")
                break
            self.store.message_sent(message["id"])


def _to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_mode(value) -> str:
    mode = str(value or "").upper()
    if mode not in Mode.__members__:
        raise RequestError("INVALID", "mode 는 ROBOT 또는 DIRECT 여야 합니다.")
    return mode

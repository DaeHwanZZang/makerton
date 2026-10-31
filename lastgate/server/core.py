"""보관소 서버 핵심 로직 (MQTT 와 분리되어 있어 단위 테스트 가능).

- 모든 요청은 락 하나로 직렬화하고, 요청 하나를 SQLite 트랜잭션 하나로 처리한다.
- 발행할 메시지는 outbox 에 모았다가 커밋 후에 보낸다 (롤백 시 버림).
- 클라이언트 응답은 요청자 전용 토픽(locker/client/{client_id}/res)으로만 보낸다.
- (client_id, request_id) 로 중복 요청을 걸러 같은 응답을 다시 보낸다 (QoS 1 대비).
- 관리자 초기 설정 전에는 관리자 요청 외에는 NOT_INITIALIZED 로 답한다.
"""
from __future__ import annotations

import json
import random
import re
import threading
import traceback
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Callable

import config
import reservations as rsv
from admin import AdminMixin
from auth import Sessions, hash_password, verify_password
from bundler import (assign_trays, build_auto_bundle, build_slot_bundle, bundle_metrics, pick_filler,
                     visit_order)
from common import RequestError, log
from common import locked_for as _locked_for
from common import to_int as _to_int
from fms_adapter import FmsAdapter
from models import (ACTIVE_STATUSES, COMPLETED_STATUSES, MODE_CHANGEABLE_STATUSES,
                    PICKABLE_STATUSES, Area, Bundle, DirectLock, Item, Mode, Plan, Status)
from site_config import Site
from storage import SpaceStorage, space_index
from store import Store

USER_ROUTES = {
    config.TOPIC_REGISTER: ("_on_register", "REGISTER_RESPONSE"),
    config.TOPIC_PORT_CONFIRM: ("_on_port_confirm", "REGISTER_CONFIRMED"),
    config.TOPIC_INPUT_STATS: ("_on_input_stats", "INPUT_STATS_RESPONSE"),
    config.TOPIC_LOGIN: ("_on_login", "LOGIN_RESPONSE"),
    config.TOPIC_LOGOUT: ("_on_logout", "LOGOUT_RESPONSE"),
    config.TOPIC_PASSWORD: ("_on_password", "PASSWORD_RESPONSE"),
    config.TOPIC_PARCELS: ("_on_parcels", "PARCELS_RESPONSE"),
    config.TOPIC_RETRIEVE: ("_on_retrieve", "RETRIEVE_RESPONSE"),
    config.TOPIC_DEFAULT_MODE: ("_on_default_mode", "DEFAULT_MODE_RESPONSE"),
    config.TOPIC_ITEM_MODE: ("_on_item_mode", "ITEM_MODE_RESPONSE"),
    config.TOPIC_RESIDENT_PARCELS: ("_on_resident_parcels", "RESIDENT_PARCELS_RESPONSE"),
    config.TOPIC_PICKUP_CODE: ("_on_pickup_code", "PICKUP_CODE_RESPONSE"),
    config.TOPIC_HIDE: ("_on_hide", "HIDE_RESPONSE"),
    config.TOPIC_SLOTS: ("_on_slots", "SLOTS_RESPONSE"),
    config.TOPIC_RESERVE: ("_on_reserve", "RESERVE_RESPONSE"),
    config.TOPIC_RESERVE_CANCEL: ("_on_reserve_cancel", "RESERVE_CANCEL_RESPONSE"),
    config.TOPIC_NOTIFICATIONS: ("_on_notifications", "NOTIFICATIONS_RESPONSE"),
}


class LockerCore(AdminMixin):
    IDEMPOTENCY_CACHE = 2000

    def __init__(self, store: Store, publish: Callable[..., None],
                 now_fn: Callable[[], datetime] = config.now_kst):
        self.store = store
        self.publish = publish
        self.now = now_fn
        self.lock = threading.RLock()
        self.sessions = Sessions(store)
        self.storage = SpaceStorage(store, on_log=self._storage_log)
        self.fms = FmsAdapter(self._emit, now_fn, building_name=self._building_name)
        self.site: Site | None = None
        self._outbox: list[tuple[str, dict, bool]] = []
        self._responses: OrderedDict[tuple[str, str], dict] = OrderedDict()
        self._touched: set[tuple[int, int]] = set()
        self._slots_changed: set[str] = set()
        self._dirty = True
        self.routes = {**USER_ROUTES, **self.ADMIN_ROUTES}

    @property
    def topics(self) -> list[str]:
        return [*self.routes, *self.fms.topics]

    # --- 시작 ------------------------------------------------------------
    def bootstrap(self):
        with self.lock:
            with self.store.transaction():
                self._load_site()
                self._seed_admin()
                if self.store.migrated_from_v3:
                    log("초기화", "v3 DB 를 v4 형식으로 변환했습니다 (동→건물, 크기 정보 제거)")
                if not self.site:
                    log("초기화", "현장 설정이 없습니다 — 관리자 웹에서 초기 설정을 해 주세요")
                self._publish_site()
                self._issue_jobs()
            self._flush()

    def _load_site(self):
        data = self.store.get_site()
        self.site = Site.load(data) if data else None

    # --- 진입점 ----------------------------------------------------------
    def handle(self, topic: str, payload: dict):
        with self.lock:
            if topic in self.routes:
                self._handle_client(topic, payload)
            elif topic in self.fms.topics:
                mark = len(self._outbox)
                try:
                    with self.store.transaction():
                        self._on_fms_event(payload)
                except Exception:
                    log("오류", traceback.format_exc())
                    del self._outbox[mark:]
            self._flush()

    def heartbeat(self):
        """주기 호출: 투입 시간 초과, 세션 정리, 예약 마감·지연 알림, 배송 발행, Job 재전송."""
        with self.lock:
            mark = len(self._outbox)
            try:
                with self.store.transaction():
                    now = self.now()
                    self._expire_registrations(now)
                    self.sessions.purge(now)
                    self._advance_plans(now)
                    self._issue_jobs()
                    # 연결 단절·서버 재시작 후에도 같은 Job/최종 묶음을 다시 알린다.
                    for active in self.store.active_jobs():
                        job = self.store.get_job(active["job_id"])
                        self.fms.resend(job["bundle"])
                    self.store.purge_seats((now - timedelta(days=2)).isoformat())
            except Exception:
                log("오류", traceback.format_exc())
                del self._outbox[mark:]
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
        handler_name, response_type = self.routes[topic]
        mark = len(self._outbox)
        try:
            with self.store.transaction():
                response = getattr(self, handler_name)(payload, client_id)
        except RequestError as e:
            del self._outbox[mark:]
            self._load_site()
            response = {"type": response_type, "status": e.status, "message": e.message, **e.extra}
        except Exception:
            log("오류", traceback.format_exc())
            del self._outbox[mark:]
            self._load_site()
            response = {"type": response_type, "status": "ERROR",
                        "message": "서버 오류가 발생했습니다. 다시 시도해 주세요."}
        response.setdefault("type", response_type)
        response["request_id"] = request_id
        self._responses[key] = response
        while len(self._responses) > self.IDEMPOTENCY_CACHE:
            self._responses.popitem(last=False)
        self._send(client_id, response)

    # --- 입력 키오스크 ---------------------------------------------------
    def _on_register(self, p: dict, client_id: str) -> dict:
        site = self._site()
        building = _to_int(p.get("building", p.get("dong")))
        unit = _to_int(p.get("unit", p.get("ho")))
        area = str(p.get("area") or Area.GRID.value).upper()
        fragile = bool(p.get("fragile"))
        tracking = str(p.get("tracking_no") or "").strip() or None
        kiosk = str(p.get("kiosk_id") or "").strip() or None
        error = site.unit_error(building, unit)
        if error:
            raise RequestError("REJECT", error)
        if area not in Area.__members__:
            raise RequestError("REJECT", "보관 구분(일반 / 규격 초과)을 선택하세요.")
        if tracking and not re.fullmatch(config.TRACKING_NO_RE, tracking):
            raise RequestError("REJECT", "송장번호는 영문·숫자 6~40자리입니다.")
        oversize = area == Area.OVERSIZE.value
        space = self.storage.allocate(area, site.oversize_doors if oversize else site.grid_bins)
        if not space:
            if oversize:
                raise RequestError("OVERSIZE_FULL", "배송 불가 보관함이 모두 찼습니다. 관리실에 맡겨 주세요.")
            raise RequestError("STORAGE_FULL", "보관함이 모두 찼습니다. 관리실에 맡겨 주세요.")

        now = self.now()
        lock = DirectLock.OVERSIZE.value if oversize else DirectLock.FRAGILE.value if fragile else None
        mode = Mode.DIRECT.value if lock else (self.store.get_default_mode(building, unit)
                                               or config.DEFAULT_MODE)
        item = Item(
            receipt_no=self._new_receipt(now), building=building, unit=unit, mode=mode,
            status=Status.REGISTERED.value, pickup_code=self._new_pickup_code(),
            registered_at=now.isoformat(), storage_area=area, space_id=space,
            fragile=int(fragile), direct_lock=lock, tracking_no=tracking, input_client=client_id,
            input_kiosk=kiosk, updated_at=now.isoformat())
        self.store.insert_item(item)
        self._dirty = True
        door = space_index(space) if oversize else None
        log("접수", f"{item.receipt_no} {site.label(building, unit)} → {space}"
                   f"{' (취급 주의)' if fragile else ''} 투입 대기")
        # 인증코드는 기사 화면에 보내지 않는다
        return {"type": "REGISTER_RESPONSE", "status": "OK", "receipt_no": item.receipt_no,
                "storage_area": area, "space_id": space, "door": door, "mode": mode,
                "direct_lock": lock, "label": site.label(building, unit),
                "input_port": area, "timeout_sec": config.PORT_TIMEOUT_SEC,
                "message": (f"배송 불가 보관함 {door:02d}번 문이 열렸습니다." if oversize
                            else "입력구에 올라온 보관함에 넣어주세요.")}

    def _on_port_confirm(self, p: dict, client_id: str) -> dict:
        self._site()
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
        if port and port != item.storage_area:
            raise RequestError("PORT_MISMATCH", self._space_text(item) + "에 넣어주세요.")
        now = self.now()
        item.status = Status.STORED.value
        item.stored_at = now.isoformat()
        item.choice_deadline = (now + timedelta(seconds=config.CHOICE_WINDOW_SEC)).isoformat()
        if item.mode == Mode.ROBOT:
            item.plan = Plan.CHOICE_OPEN.value
        self._save(item)
        self.storage.log("STORE", item, source=source)
        self._notify_arrival(item)
        log("보관", f"{receipt} {self._label(item)} → {item.space_id} ({item.mode}"
                   f"{', ' + item.direct_lock if item.direct_lock else ''}, 감지: {source})")
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

    # --- 로그인·비밀번호 -------------------------------------------------
    def _on_login(self, p: dict, client_id: str) -> dict:
        site = self._site()
        now = self.now()
        user_id = str(p.get("user_id") or "").strip()
        if user_id:
            account = self.store.get_account(user_id)
        else:
            building, unit = _to_int(p.get("building")), _to_int(p.get("unit"))
            account = (self.store.get_account_by_unit(building, unit)
                       if building is not None and unit is not None else None)
        resident = str(p.get("channel") or "").upper() == "RESIDENT"
        if resident and not client_id.startswith(config.RESIDENT_CLIENT_PREFIX):
            raise RequestError("DENIED", "세대 웹 로그인은 세대 웹 서버를 통해서만 할 수 있습니다.")
        denied = {"type": "LOGIN_RESPONSE", "status": "DENIED",
                  "message": f"{site.building_term}·{site.unit_term} 또는 비밀번호가 올바르지 않습니다."}
        if not account or not account["active"]:
            return denied
        locked = _locked_for(account, now)
        if locked:
            return {"type": "LOGIN_RESPONSE", "status": "LOCKED", "retry_after_sec": locked,
                    "message": f"비밀번호를 {config.LOGIN_MAX_FAIL}번 틀려 잠겼습니다. "
                               f"{-(-locked // 60)}분 뒤 다시 시도하거나 관리실에 문의하세요."}
        if not verify_password(str(p.get("password") or ""), account["password_hash"]):
            # 실패 횟수는 커밋해야 하므로 예외 대신 응답을 그대로 돌려준다
            return self._login_failed(account, now, denied, "accounts")
        self.store.update_account(account["user_id"], failed_count=0, locked_until=None)
        ttl = config.RESIDENT_SESSION_TTL_SEC if resident else config.SESSION_TTL_SEC
        token = self.sessions.create(kind="USER", user_id=account["user_id"],
                                     channel="RESIDENT" if resident else "KIOSK",
                                     client_id=client_id, now=now, ttl_sec=ttl,
                                     building=account["building"], unit=account["unit"])
        log("로그인", f"{account['user_id']} ({client_id}{', 세대 웹' if resident else ''})")
        return {"type": "LOGIN_RESPONSE", "status": "OK", "session": token, "ttl_sec": ttl,
                "must_change": bool(account["must_change"]),
                "household": self._household(account["building"], account["unit"]),
                "parcels": self._kiosk_parcels(account["building"], account["unit"])}

    def _login_failed(self, account: dict, now: datetime, denied: dict, table: str) -> dict:
        key = "user_id" if table == "accounts" else "admin_id"
        update = self.store.update_account if table == "accounts" else self.store.update_admin
        count = account["failed_count"] + 1
        if count >= config.LOGIN_MAX_FAIL:
            until = now + timedelta(seconds=config.LOGIN_LOCK_SEC)
            update(account[key], failed_count=0, locked_until=until.isoformat())
            log("로그인", f"{account[key]} 비밀번호 {count}회 실패 → {config.LOGIN_LOCK_SEC // 60}분 잠금")
            return {**denied, "status": "LOCKED", "retry_after_sec": config.LOGIN_LOCK_SEC,
                    "message": f"비밀번호를 {count}번 틀려 {config.LOGIN_LOCK_SEC // 60}분 동안 잠겼습니다."}
        update(account[key], failed_count=count)
        return {**denied, "remaining": config.LOGIN_MAX_FAIL - count}

    def _on_logout(self, p: dict, client_id: str) -> dict:
        self.sessions.revoke(p.get("session"))
        return {"type": "LOGOUT_RESPONSE", "status": "OK"}

    def _on_password(self, p: dict, client_id: str) -> dict:
        s = self._session(p)
        account = self.store.get_account(s["user_id"])
        current = str(p.get("current_password") or "")
        new = str(p.get("new_password") or "")
        if not re.fullmatch(config.PASSWORD_RE, new):
            raise RequestError("INVALID", "새 비밀번호는 숫자 4~8자리입니다.")
        if new == config.SEED_PASSWORD:
            raise RequestError("INVALID", f"초기 비밀번호({config.SEED_PASSWORD})는 쓸 수 없습니다.")
        if new == current:
            raise RequestError("INVALID", "현재 비밀번호와 다른 번호를 입력하세요.")
        if not account or not verify_password(current, account["password_hash"]):
            raise RequestError("DENIED", "현재 비밀번호가 올바르지 않습니다.")
        now = self.now()
        self.store.update_account(s["user_id"], password_hash=hash_password(new), must_change=0,
                                  password_changed_at=now.isoformat(), failed_count=0, locked_until=None)
        revoked = self.sessions.revoke_user("USER", s["user_id"], keep_token=p.get("session"))
        log("비밀번호", f"{s['user_id']} 변경 (다른 기기 로그인 {revoked}개 종료)")
        return {"type": "PASSWORD_RESPONSE", "status": "OK", "revoked": revoked,
                "household": self._household(s["building"], s["unit"])}

    # --- 출력 키오스크 ---------------------------------------------------
    def _on_parcels(self, p: dict, client_id: str) -> dict:
        s = self._session(p)
        return {"type": "PARCELS_RESPONSE", "status": "OK",
                "household": self._household(s["building"], s["unit"]),
                "parcels": self._kiosk_parcels(s["building"], s["unit"])}

    def _on_retrieve(self, p: dict, client_id: str) -> dict:
        s = self._session(p)
        receipt = str(p.get("receipt_no") or "")
        item = self.store.get_item(receipt)
        if not item or item.household != (s["building"], s["unit"]):
            raise RequestError("NOT_FOUND", "해당 택배를 찾을 수 없습니다.")
        info = self._retrieve(item, "KIOSK")
        return {"type": "RETRIEVE_RESPONSE", "status": "OK", "receipt_no": receipt, **info}

    def _retrieve(self, item: Item, by: str) -> dict:
        """키오스크·관리자 코드 수령 공통. 로봇 적재 전이면 배송 예정이어도 꺼낼 수 있다."""
        if item.status in (Status.LOADED, Status.DELIVERED):
            raise RequestError("ALREADY_LOADED", "이미 로봇에 적재된 택배입니다.")
        if item.status not in PICKABLE_STATUSES:
            raise RequestError("NOT_FOUND", "보관 중인 택배가 아닙니다.")
        info = {"storage_area": item.storage_area, "space_id": item.space_id,
                "door": space_index(item.space_id) if item.storage_area == Area.OVERSIZE else None,
                "out_port": item.storage_area}
        job_id = item.job_id if item.status == Status.JOB_ASSIGNED else None
        self._release_seat(item)
        self.storage.log("RETRIEVE", item, by=by)
        item.status, item.space_id, item.job_id, item.plan = Status.PICKED.value, None, None, None
        self._save(item)
        if job_id:
            self._cancel_if_empty(job_id)
        log("수령", f"{item.receipt_no} {self._label(item)} → {info['space_id']} ({by})")
        return info

    # --- 수령 방식 -------------------------------------------------------
    def _on_default_mode(self, p: dict, client_id: str) -> dict:
        s = self._session(p)
        mode = _to_mode(p.get("mode"))
        household = (s["building"], s["unit"])
        now = self.now()
        self.store.set_default_mode(*household, mode)
        changed = skipped = 0
        for item in self.store.items(MODE_CHANGEABLE_STATUSES, household=household):
            if item.mode == mode:
                continue
            if item.direct_lock or self._fixed(item, now):
                skipped += 1
                continue
            self._apply_mode(item, mode, now)
            changed += 1
        self._touched.add(household)
        self._dirty = True
        log("수령방식", f"{self.site.label(*household)} 기본값 → {mode} "
                       f"(물품 {changed}건 변경, {skipped}건 고정)")
        self._issue_jobs()
        return {"type": "DEFAULT_MODE_RESPONSE", "status": "OK", "default_mode": mode,
                "changed": changed, "skipped": skipped,
                "parcels": self._resident_parcels(household, now)}

    def _on_item_mode(self, p: dict, client_id: str) -> dict:
        s = self._session(p)
        mode = _to_mode(p.get("mode"))
        receipt = str(p.get("receipt_no") or "")
        now = self.now()
        item = self.store.get_item(receipt)
        if not item or item.household != (s["building"], s["unit"]):
            raise RequestError("NOT_FOUND", "해당 택배를 찾을 수 없습니다.")
        if item.direct_lock and mode == Mode.ROBOT:
            raise RequestError("LOCKED_DIRECT", _lock_text(item.direct_lock))
        if mode == Mode.ROBOT and self.store.get_default_mode(*item.household) != Mode.ROBOT:
            raise RequestError("DENIED", "세대 기본값이 '직접 수령'이면 로봇 배송을 고를 수 없습니다.")
        if item.status not in MODE_CHANGEABLE_STATUSES:
            raise RequestError("NOT_CHANGEABLE", "이미 로봇에 적재되었거나 보관 중이 아닌 택배입니다.")
        if item.mode != mode:
            if self._fixed(item, now):
                raise RequestError("FIXED", "배송이 확정되어 바꿀 수 없습니다. 보관소에서 직접 찾아가실 수는 있습니다.")
            self._apply_mode(item, mode, now)
            log("수령방식", f"{receipt} → {mode}")
            self._issue_jobs()
        return {"type": "ITEM_MODE_RESPONSE", "status": "OK", "receipt_no": receipt, "mode": item.mode,
                "parcel": self._resident_parcel(item, now)}

    # --- 세대 웹 ---------------------------------------------------------
    def _on_resident_parcels(self, p: dict, client_id: str) -> dict:
        s = self._session(p)
        household = (s["building"], s["unit"])
        now = self.now()
        account = self.store.get_account(s["user_id"])
        return {"type": "RESIDENT_PARCELS_RESPONSE", "status": "OK",
                "server_time": now.isoformat(),
                "household": self._household(*household),
                "unread": self.store.unread_count(*household, account["occupant_since"]),
                "parcels": self._resident_parcels(household, now)}

    def _on_pickup_code(self, p: dict, client_id: str) -> dict:
        s = self._session(p)
        item = self.store.get_item(str(p.get("receipt_no") or ""))
        if not item or item.household != (s["building"], s["unit"]) or item.hidden_at:
            raise RequestError("NOT_FOUND", "내 세대의 택배를 찾을 수 없습니다.")
        if item.status not in PICKABLE_STATUSES or item.mode != Mode.DIRECT:
            raise RequestError("NOT_AVAILABLE", "직접 수령으로 보관 중인 택배만 인증코드를 볼 수 있습니다.")
        return {"type": "PICKUP_CODE_RESPONSE", "status": "OK", "receipt_no": item.receipt_no,
                "code": item.pickup_code}

    def _on_hide(self, p: dict, client_id: str) -> dict:
        s = self._session(p)
        item = self.store.get_item(str(p.get("receipt_no") or ""))
        if not item or item.household != (s["building"], s["unit"]):
            raise RequestError("NOT_FOUND", "내 세대의 택배를 찾을 수 없습니다.")
        if item.status not in COMPLETED_STATUSES:
            raise RequestError("NOT_COMPLETED", "배송·수령이 끝난 택배만 목록에서 삭제할 수 있습니다.")
        if not item.hidden_at:
            item.hidden_at = self.now().isoformat()
            self._save(item)
        return {"type": "HIDE_RESPONSE", "status": "OK", "receipt_no": item.receipt_no}

    def _on_notifications(self, p: dict, client_id: str) -> dict:
        s = self._session(p)
        household = (s["building"], s["unit"])
        now = self.now()
        since = self.store.get_account(s["user_id"])["occupant_since"]
        read = p.get("read")
        if read == "all":
            self.store.mark_notifications_read(*household, now.isoformat(), None)
        elif isinstance(read, list) and read:
            ids = [i for i in (_to_int(x) for x in read) if i is not None]
            if ids:
                self.store.mark_notifications_read(*household, now.isoformat(), ids)
        rows = self.store.notifications(*household, config.NOTIFICATION_KEEP, since)
        return {"type": "NOTIFICATIONS_RESPONSE", "status": "OK",
                "unread": self.store.unread_count(*household, since),
                "notifications": [_notification(r) for r in rows]}

    # --- 배송 시간 예약 --------------------------------------------------
    def _on_slots(self, p: dict, client_id: str) -> dict:
        s = self._session(p)
        site = self._site()
        now = self.now()
        return {"type": "SLOTS_RESPONSE", "status": "OK", "server_time": now.isoformat(),
                "service_start": site.service_start, "service_end": site.service_end,
                "seats_per_slot": config.SLOT_SEATS,
                "slots": self._slot_view((s["building"], s["unit"]), now)}

    def _slot_view(self, household: tuple[int, int] | None, now: datetime) -> list[dict]:
        site = self.site
        starts = rsv.bookable_starts(now, site.service_start, site.service_end)
        if not starts:
            return []
        taken: dict[str, list[dict]] = {}
        for seat in self.store.seats(starts[0].isoformat(), starts[-1].isoformat()):
            taken.setdefault(seat["slot_start"], []).append(seat)
        out = []
        for start in starts:
            key = start.isoformat()
            seats = taken.get(key, [])
            mine = [x for x in seats if household and (x["building"], x["unit"]) == household]
            left = config.SLOT_SEATS - len(seats)
            restricted = bool(household and site.second_seat_same_building and left > 0
                              and any(x["building"] != household[0] for x in seats))
            out.append({"start": key, "end": rsv.slot_end(start).isoformat(),
                        "label": rsv.label(start), "seats_left": left,
                        "available": 0 if restricted else left, "restricted": restricted,
                        "mine": len(mine), "mine_receipts": [x["receipt_no"] for x in mine]})
        return out

    def _on_reserve(self, p: dict, client_id: str) -> dict:
        s = self._session(p)
        site = self._site()
        now = self.now()
        household = (s["building"], s["unit"])
        start = rsv.parse_start(p.get("slot_start"))
        receipts = p.get("receipt_nos")
        if isinstance(p.get("receipt_no"), str):
            receipts = [p["receipt_no"]]
        if not isinstance(receipts, list) or not 1 <= len(set(receipts)) <= config.SLOT_SEATS:
            raise RequestError("INVALID", f"예약할 택배를 1~{config.SLOT_SEATS}개 고르세요.")
        receipts = list(dict.fromkeys(str(r) for r in receipts))
        if not start or start not in rsv.bookable_starts(now, site.service_start, site.service_end):
            raise RequestError("SLOT_UNAVAILABLE",
                               "예약할 수 없는 시간입니다. 20분 뒤부터 배송 시간대 안에서 고르세요.")
        key = start.isoformat()
        items = []
        for receipt in receipts:
            item = self.store.get_item(receipt)
            if not item or item.household != household:
                raise RequestError("NOT_FOUND", "내 세대의 택배를 찾을 수 없습니다.")
            self._check_reservable(item, now)
            items.append(item)
        seats = self.store.seats_of(key)
        holding = [x for x in seats if x["receipt_no"] in receipts]
        others = [x for x in seats if x["receipt_no"] not in receipts]
        free = [n for n in range(1, config.SLOT_SEATS + 1) if n not in {x["seat"] for x in seats}]
        need = len(items) - len(holding)
        if site.second_seat_same_building and any(x["building"] != household[0] for x in others):
            raise RequestError("SLOT_FULL", "이 시간은 다른 건물 세대가 먼저 예약해 같은 건물만 함께 예약할 수 있습니다.",
                               seats_left=0)
        if len(free) < need:
            raise RequestError("SLOT_FULL", "방금 다른 세대가 예약해 자리가 부족합니다. 다른 시간을 골라 주세요."
                               if free else "이 시간은 예약이 모두 찼습니다.", seats_left=len(free))
        for item in items:
            if item.slot_start == key:
                continue
            self._release_seat(item)
            self.store.take_seat(key, free.pop(0), item, now.isoformat())
            item.plan, item.slot_start, item.auto_at = Plan.RESERVED.value, key, None
            item.notified = ",".join(n for n in item.notified.split(",") if n and n not in ("DELAY", "RUSH"))
            self._save(item)
        self._slots_changed.add(key)
        log("예약", f"{site.label(*household)} {rsv.label(start)} ← {', '.join(receipts)}")
        return {"type": "RESERVE_RESPONSE", "status": "OK", "slot_start": key,
                "slot_end": rsv.slot_end(start).isoformat(), "label": rsv.label(start),
                "receipt_nos": receipts, "parcels": self._resident_parcels(household, now)}

    def _check_reservable(self, item: Item, now: datetime):
        if item.direct_lock:
            raise RequestError("LOCKED_DIRECT", _lock_text(item.direct_lock))
        if item.mode != Mode.ROBOT:
            raise RequestError("NOT_ROBOT", "로봇 배송으로 바꾼 뒤 배송 시간을 예약하세요.")
        if item.status != Status.STORED:
            raise RequestError("NOT_CHANGEABLE", "보관 중인 택배만 배송 시간을 예약할 수 있습니다.")
        if item.plan == Plan.AUTO:
            raise RequestError("FIXED", "자동 배송이 확정되어 예약할 수 없습니다.")
        if item.plan == Plan.RESERVED and now >= rsv.cutoff(datetime.fromisoformat(item.slot_start)):
            raise RequestError("TOO_LATE", "배송 20분 전부터는 예약을 바꿀 수 없습니다.")

    def _on_reserve_cancel(self, p: dict, client_id: str) -> dict:
        s = self._session(p)
        now = self.now()
        household = (s["building"], s["unit"])
        item = self.store.get_item(str(p.get("receipt_no") or ""))
        if not item or item.household != household:
            raise RequestError("NOT_FOUND", "내 세대의 택배를 찾을 수 없습니다.")
        if item.plan != Plan.RESERVED or item.status != Status.STORED:
            raise RequestError("NOT_RESERVED", "예약된 택배가 아닙니다.")
        if now >= rsv.cutoff(datetime.fromisoformat(item.slot_start)):
            raise RequestError("TOO_LATE", "배송 20분 전부터는 예약을 취소할 수 없습니다.")
        self._release_seat(item)
        if item.choice_deadline and now < datetime.fromisoformat(item.choice_deadline):
            item.plan = Plan.CHOICE_OPEN.value
            self._save(item)
        else:
            self._make_auto(item, now)     # 예약 가능 시간이 지났으면 자동 배송 확정
        log("예약취소", f"{item.receipt_no} → {item.plan}")
        self._issue_jobs()
        return {"type": "RESERVE_CANCEL_RESPONSE", "status": "OK", "receipt_no": item.receipt_no,
                "plan": item.plan, "parcels": self._resident_parcels(household, now)}

    # --- 주기 처리 -------------------------------------------------------
    def _expire_registrations(self, now: datetime):
        for item in self.store.items([Status.REGISTERED]):
            if item.wait_seconds(now, item.registered_at) >= config.PORT_TIMEOUT_SEC:
                item.status, item.space_id = Status.CANCELLED.value, None
                self._save(item)
                log("접수취소", f"{item.receipt_no} 투입 시간 초과")
                if item.input_client:
                    self._send(item.input_client, {
                        "type": "REGISTER_CANCELLED", "receipt_no": item.receipt_no,
                        "reason": "TIMEOUT", "message": "투입이 확인되지 않아 접수가 취소되었습니다."})

    def _advance_plans(self, now: datetime):
        """예약 마감 10분 전 알림 → 마감 시 자동 배송 확정 → 예약 칸 시작까지 미출발이면 지연 알림."""
        for item in self.store.items([Status.STORED], plan=Plan.CHOICE_OPEN.value, mode=Mode.ROBOT.value):
            deadline = datetime.fromisoformat(item.choice_deadline)
            if now >= deadline:
                self._make_auto(item, deadline)
            elif (deadline - now).total_seconds() <= config.CHOICE_REMINDER_SEC and not item.has_note("REMIND"):
                item.add_note("REMIND")
                self._save(item)
                left = max(1, round((deadline - now).total_seconds() / 60))
                self._notify(item, "CHOICE_REMINDER", "배송 시간 예약이 곧 마감돼요",
                             f"{left}분 뒤({deadline:%H:%M}) 자동 배송으로 확정됩니다. 원하는 시간이 있으면 지금 예약하세요.")
        for item in self.store.items(PICKABLE_STATUSES, plan=Plan.RESERVED.value, mode=Mode.ROBOT.value):
            start = datetime.fromisoformat(item.slot_start)
            if now >= start and not item.has_note("DELAY"):
                item.add_note("DELAY")
                self._save(item)
                self._notify(item, "DELAYED", "배송이 조금 늦어지고 있어요",
                             f"예약하신 {rsv.label(start)}에 로봇이 아직 출발하지 못했어요. "
                             "순서가 되면 바로 출발합니다.")

    def _make_auto(self, item: Item, at: datetime):
        item.plan, item.auto_at = Plan.AUTO.value, at.isoformat()
        self._save(item)
        self._notify(item, "AUTO_FIXED", "자동 배송으로 확정됐어요",
                     "가까운 시간에 로봇이 문 앞까지 배송합니다. 그 전에 보관소에서 직접 찾아가셔도 됩니다.")

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
        if job["status"] in ("CONFIRMED", "CANCELLED"):    # 중복 도착·도착 전 취소 → 같은 메시지 재전송
            self.fms.resend(job["bundle"])
            return
        if job["status"] != "PENDING":
            return
        now = self.now()
        planned = [i["receipt_no"] for i in (job["bundle"] or {}).get("items", [])]
        assigned = {it.receipt_no: it for it in self.store.items([Status.JOB_ASSIGNED], job_id=job_id)}
        # 늦은 확정: 도착 시점에 남은 물품만 싣는다. 2개가 안 되면 대기 물품으로 채우고,
        # 그래도 2개가 안 되면 출발하지 않는다 (JOB_CANCELLED, 남은 물품은 보관 중으로 되돌림).
        items = [assigned[r] for r in planned if r in assigned and assigned[r].mode == Mode.ROBOT]
        items += [it for r, it in assigned.items() if r not in planned and it.mode == Mode.ROBOT]
        items = items[:config.BUNDLE_MAX]
        if len(items) == 1:
            filler = pick_filler(items[0], self._fillers(now, exclude={items[0].receipt_no}),
                                 now, self.site.order() if self.site else {})
            if filler:
                filler.status, filler.job_id = Status.JOB_ASSIGNED.value, job_id
                self._save(filler)
                log("적재보충", f"{job_id} {filler.receipt_no} {self._label(filler)}")
                items.append(filler)
                if (job["metrics"] or {}).get("kind") != "RESERVED":
                    items = visit_order(items, self.site.order() if self.site else {})
        if len(items) < config.BUNDLE_MIN:
            items = []
        loading = {it.receipt_no for it in items}
        for it in assigned.values():
            if it.receipt_no not in loading:
                it.status, it.job_id = Status.STORED.value, None
                self._save(it)
        if not items:
            reason = "NOT_ENOUGH" if assigned else "NO_ITEMS"
            payload = self.fms.send_cancel(job_id, reason)
            self.store.update_job(job_id, status="CANCELLED", robot_id=robot_id,
                                  done_at=now.isoformat(), bundle=payload)
            self._dirty = True
            log("Job", f"{job_id} 취소 — 로봇 도착 시점에 실을 물품이 {config.BUNDLE_MIN}개가 안 됨 ({reason})")
            return
        bundle = Bundle(items=items, trays=assign_trays(items),
                        metrics={**(job["metrics"] or {}), **bundle_metrics(items, now, (job["metrics"] or {}).get("kind", "AUTO"))},
                        slot_start=(job["bundle"] or {}).get("summary", {}).get("slot_start"))
        notified = set()
        for it in items:
            it.status, it.robot_id = Status.LOADED.value, robot_id
            self._save(it)
            self.storage.log("LOAD_ROBOT", it, robot_id=robot_id, tray=bundle.trays[it.receipt_no])
            if it.household not in notified:
                notified.add(it.household)
                count = sum(1 for x in items if x.household == it.household)
                self._notify(it, "DEPARTED", "로봇이 출발했어요",
                             f"택배 {count}개를 싣고 {self._label(it)}(으)로 가고 있어요.")
        payload = self.fms.send_bundle(job_id, robot_id, bundle)
        self.store.update_job(job_id, status="CONFIRMED", robot_id=robot_id,
                              confirmed_at=now.isoformat(), bundle=payload, metrics=bundle.metrics)
        self.store.log_event(now.isoformat(), "BUNDLE", {"job_id": job_id, **bundle.metrics})
        trays = ", ".join(f"{bundle.trays[it.receipt_no]}={it.receipt_no}" for it in items)
        log("적재확정", f"{job_id} 로봇 {robot_id} · {trays}")

    def _on_delivery_result(self, job: dict, event: dict):
        item = self.store.get_item(event["receipt_no"])
        if not item or item.job_id != job["job_id"] or item.status != Status.LOADED:
            return                                          # 중복·무관한 결과
        if item.has_note("RETURNING"):
            return
        if event["result"] == "DELIVERED":
            item.status = Status.DELIVERED.value
            self._save(item)
            self._notify(item, "DELIVERED", "문 앞에 배송했어요", "로봇이 택배를 문 앞에 두고 돌아갑니다.")
            log("배달완료", f"{item.receipt_no} {self._label(item)}")
        else:
            # 보관함은 로봇과 함께 돌아온다. JOB_DONE 때 보관 중으로 되돌린다.
            item.fail_count += 1
            item.add_note("RETURNING")
            self._save(item)
            locked = item.fail_count >= config.MAX_FAIL
            self._notify(item, "FAILED", "배송하지 못했어요",
                         "택배는 보관소로 돌아갑니다. " + ("출력 키오스크에서 직접 찾아가세요." if locked
                                                  else "잠시 뒤 다시 배송합니다."))
            log("배달실패", f"{item.receipt_no} {event.get('reason') or ''} (누적 {item.fail_count}회)")

    def _on_job_done(self, job: dict):
        if job["status"] in ("DONE", "CANCELLED"):
            return
        now = self.now()
        for item in self.store.items([Status.LOADED], job_id=job["job_id"]):
            if not item.has_note("RETURNING"):              # 결과 미보고는 실패로 본다
                item.fail_count += 1
            self._return_to_storage(item, now)
        for item in self.store.items([Status.DELIVERED], job_id=job["job_id"]):
            if item.space_id:                                # 빈 보관함 반납
                item.space_id = None
                self._save(item)
        for item in self.store.items([Status.JOB_ASSIGNED], job_id=job["job_id"]):
            item.status, item.job_id = Status.STORED.value, None
            self._save(item)
        self.store.update_job(job["job_id"], status="DONE", done_at=now.isoformat())
        self._dirty = True
        log("Job", f"{job['job_id']} 완료")
        self._issue_jobs()

    def _on_job_rejected(self, job: dict, event: dict):
        """FMS 가 로봇 배정 전에 배송을 취소했다 (예: 세대 목적지 연결 없음).

        로봇에 싣기 전이므로 물품은 보관함에 그대로 있다. FMS 가 갈 수 없다고 한 세대의 물품은
        직접 수령으로 바꿔 같은 묶음이 다시 발행되어 멈추는 것을 막고, 나머지는 다음 배송 후보로 돌린다.
        직접 수령 고정(direct_lock)은 하지 않는다. 세대 웹에서 로봇 배송으로 되돌릴 수 있다.
        """
        if job["status"] != "PENDING":
            if job["status"] == "CONFIRMED":
                log("FMS", f"{job['job_id']} 취소 무시 — 이미 적재 확정됨")
            return
        now = self.now()
        unroutable = set(event["unroutable"])
        direct = []
        for item in self.store.items([Status.JOB_ASSIGNED], job_id=job["job_id"]):
            item.status, item.job_id = Status.STORED.value, None
            if item.receipt_no in unroutable and item.mode == Mode.ROBOT:
                self._apply_mode(item, Mode.DIRECT.value, now)
                direct.append(f"{item.receipt_no}({self._label(item)})")
            else:
                self._save(item)
        self.store.update_job(job["job_id"], status="CANCELLED", done_at=now.isoformat())
        self.store.log_event(now.isoformat(), "JOB_REJECTED",
                             {"job_id": job["job_id"], "reason": event["reason"], "direct": direct})
        self._dirty = True
        log("Job", f"{job['job_id']} FMS 취소 — {event['reason'] or '사유 없음'}"
                   + (f" · 직접 수령 전환 {', '.join(direct)}" if direct else ""))
        self._issue_jobs()

    def _return_to_storage(self, item: Item, now: datetime):
        """배달 실패 물품을 보관함째 되돌린다. MAX_FAIL 회 실패하면 직접 수령 고정."""
        self._release_seat(item)
        item.status, item.job_id, item.robot_id = Status.STORED.value, None, None
        item.notified = ",".join(n for n in item.notified.split(",") if n and n != "RETURNING")
        if item.fail_count >= config.MAX_FAIL:
            item.mode, item.direct_lock, item.plan = Mode.DIRECT.value, DirectLock.FAILED.value, None
            self._save(item)
        else:
            self._make_auto(item, now)
        self.storage.log("RETURN", item, fail_count=item.fail_count)
        log("재입고", f"{item.receipt_no} {item.space_id} (누적 실패 {item.fail_count}회, {item.mode})")

    # --- 배송 발행 -------------------------------------------------------
    def _issue_jobs(self):
        """예약 칸(시작 15분 전) → 자동 배송 순으로 로봇 수만큼 배송을 발행한다."""
        if not self.site:
            return
        now = self.now()
        order = self.site.order()
        free = self.site.robots - len(self.store.active_jobs())
        reserved = self.store.items([Status.STORED], plan=Plan.RESERVED.value, mode=Mode.ROBOT.value,
                                    unassigned=True)
        starts = sorted({it.slot_start for it in reserved})
        used: set[str] = set()
        for key in starts:
            if free <= 0:
                break
            items = [it for it in reserved if it.slot_start == key and it.receipt_no not in used]
            if not any(self._due(it, now) for it in items):
                continue
            if not items:
                continue
            bundle = build_slot_bundle(items, self._fillers(now, exclude=used | {it.receipt_no for it in items}),
                                       now, order, key)
            if bundle:
                self._create_job(bundle, now)
                used |= {it.receipt_no for it in bundle.items}
                free -= 1
        # 곧 출발할 예약이 있으면 마지막 로봇은 자동 배송에 쓰지 않는다
        soon = now + timedelta(seconds=config.SLOT_PROTECT_SEC)
        protect = any(rsv.dispatch_at(datetime.fromisoformat(it.slot_start)) <= soon
                      for it in self.store.items([Status.STORED], plan=Plan.RESERVED.value,
                                                 mode=Mode.ROBOT.value, unassigned=True))
        while free > (1 if protect else 0):
            pool = self.store.items([Status.STORED], plan=Plan.AUTO.value, mode=Mode.ROBOT.value,
                                    unassigned=True)
            bundle = build_auto_bundle(pool, now, order)
            if not bundle:
                return
            self._create_job(bundle, now)
            free -= 1

    def _create_job(self, bundle: Bundle, now: datetime):
        job_id = self._new_job_id(now)
        for it in bundle.items:
            it.status, it.job_id = Status.JOB_ASSIGNED.value, job_id
            self._save(it)
        payload = self.fms.send_job(job_id, bundle)
        self.store.insert_job(job_id, now.isoformat(), payload, bundle.metrics)
        self._dirty = True
        log("Job", f"{job_id} 발행 ({bundle.metrics['kind']}) · {len(bundle.items)}건 · "
                   f"{', '.join(self._label(it) for it in bundle.items)}")

    def _fillers(self, now: datetime, exclude: set[str]) -> list[Item]:
        """1개 남은 묶음의 짝 후보: 자동 배송 대기 물품 + 발행 시각이 지난 예약 물품 (아직 배송 미발행)."""
        out = []
        for it in self.store.items([Status.STORED], mode=Mode.ROBOT.value, unassigned=True):
            if it.receipt_no in exclude or it.direct_lock:
                continue
            if it.plan == Plan.AUTO or (it.plan == Plan.RESERVED and self._due(it, now)):
                out.append(it)
        return out

    @staticmethod
    def _due(item: Item, now: datetime) -> bool:
        """예약 물품의 발행 시각(칸 시작 15분 전)이 됐는지. 시연 '대기 건너뛰기'(RUSH)면 바로."""
        if item.has_note("RUSH"):
            return True
        return bool(item.slot_start) and now >= rsv.dispatch_at(datetime.fromisoformat(item.slot_start))

    def _cancel_if_empty(self, job_id: str):
        """도착 전 Job 의 물품이 모두 빠지면 바로 취소를 보낸다."""
        job = self.store.get_job(job_id)
        if not job or job["status"] != "PENDING" or self.store.items([Status.JOB_ASSIGNED], job_id=job_id):
            return
        payload = self.fms.send_cancel(job_id, "EMPTIED")
        self.store.update_job(job_id, status="CANCELLED", done_at=self.now().isoformat(), bundle=payload)
        self._dirty = True
        log("Job", f"{job_id} 취소 — 로봇 도착 전에 물품이 모두 빠짐")

    # --- 보조 ------------------------------------------------------------
    def _site(self) -> Site:
        if not self.site:
            raise RequestError("NOT_INITIALIZED", "관리자 초기 설정이 필요합니다.")
        return self.site

    def _fixed(self, item: Item, now: datetime) -> bool:
        """자동 배송 확정·예약 변경 마감·배송 발행된 물품은 세대 웹에서 바꿀 수 없다."""
        if item.status == Status.JOB_ASSIGNED or item.plan == Plan.AUTO:
            return True
        return bool(item.plan == Plan.RESERVED and item.slot_start
                    and now >= rsv.cutoff(datetime.fromisoformat(item.slot_start)))

    def _apply_mode(self, item: Item, mode: str, now: datetime):
        item.mode = mode
        if mode == Mode.DIRECT:
            self._release_seat(item)
            item.plan = None
        elif item.status != Status.REGISTERED:
            open_ = item.choice_deadline and now < datetime.fromisoformat(item.choice_deadline)
            item.plan = Plan.CHOICE_OPEN.value if open_ else Plan.AUTO.value
            item.auto_at = None if open_ else now.isoformat()
        self._save(item)

    def _release_seat(self, item: Item):
        if item.slot_start:
            self.store.release_seat(item.receipt_no)
            self._slots_changed.add(item.slot_start)
            item.slot_start = None

    def _session(self, p: dict) -> dict:
        self._site()
        session = self.sessions.get(p.get("session"), self.now(), "USER")
        if not session:
            raise RequestError("EXPIRED_SESSION", "로그인이 만료되었습니다. 다시 로그인해 주세요.")
        return session

    def _household(self, building: int, unit: int) -> dict:
        account = self.store.get_account_by_unit(building, unit) or {}
        return {"building": building, "building_name": self._building_name(building), "unit": unit,
                "label": self.site.label(building, unit) if self.site else f"{building}-{unit}",
                "user_id": account.get("user_id"), "default_mode": account.get("default_mode"),
                "must_change": bool(account.get("must_change"))}

    def _kiosk_parcels(self, building: int, unit: int) -> list[dict]:
        return [{"receipt_no": it.receipt_no, "arrived_at": it.stored_at, "mode": it.mode,
                 "status": it.status, "storage_area": it.storage_area, "space_id": it.space_id,
                 "door": space_index(it.space_id) if it.storage_area == Area.OVERSIZE else None,
                 "fragile": bool(it.fragile), "direct_lock": it.direct_lock, "plan": it.plan,
                 "slot_start": it.slot_start}
                for it in self.store.items(PICKABLE_STATUSES, household=(building, unit))]

    def _resident_parcels(self, household: tuple[int, int], now: datetime) -> list[dict]:
        since = (now - timedelta(days=config.RESIDENT_HISTORY_DAYS)).isoformat()
        account = self.store.get_account_by_unit(*household) or {}
        items = self.store.resident_items(household, [*PICKABLE_STATUSES, Status.LOADED],
                                          COMPLETED_STATUSES, since, account.get("occupant_since"))
        return [self._resident_parcel(it, now) for it in items]

    def _resident_parcel(self, it: Item, now: datetime) -> dict:
        start = datetime.fromisoformat(it.slot_start) if it.slot_start else None
        fixed = self._fixed(it, now)
        pickable = it.status in PICKABLE_STATUSES
        return {"receipt_no": it.receipt_no, "tracking_no": it.tracking_no, "status": it.status,
                "mode": it.mode, "plan": it.plan, "storage_area": it.storage_area,
                "door": space_index(it.space_id) if it.storage_area == Area.OVERSIZE else None,
                "fragile": bool(it.fragile), "direct_lock": it.direct_lock,
                "choice_deadline": it.choice_deadline if it.plan == Plan.CHOICE_OPEN else None,
                "slot_start": it.slot_start, "slot_end": rsv.slot_end(start).isoformat() if start else None,
                "slot_label": rsv.label(start) if start else None,
                "change_until": rsv.cutoff(start).isoformat() if start else None,
                "robot_id": it.robot_id if it.status == Status.LOADED else None,
                "registered_at": it.registered_at, "stored_at": it.stored_at, "updated_at": it.updated_at,
                "fixed": fixed,
                "changeable": pickable and not it.direct_lock and not fixed,
                "reservable": (it.status == Status.STORED and it.mode == Mode.ROBOT and not it.direct_lock
                               and not fixed and it.plan in (Plan.CHOICE_OPEN, Plan.RESERVED))}

    def _notify(self, item: Item, kind: str, title: str, body: str):
        """세대 알림 저장 + 세대 웹 서버로 푸시 (세대 웹은 SSE 로 브라우저에 전달)."""
        now = self.now().isoformat()
        row = {"building": item.building, "unit": item.unit, "kind": kind, "title": title,
               "body": body, "receipt_no": item.receipt_no, "created_at": now}
        row["id"] = self.store.add_notification(row)
        self.store.prune_notifications(item.building, item.unit, config.NOTIFICATION_KEEP)
        clients = {s["client_id"] for s in self.sessions.household(item.building, item.unit, self.now())
                   if s["channel"] == "RESIDENT"}
        for client_id in sorted(clients):
            self._send(client_id, {"type": "NOTIFICATION",
                                   "household": {"building": item.building, "unit": item.unit},
                                   "notification": _notification(row)})

    def _notify_arrival(self, item: Item):
        if item.mode == Mode.ROBOT:
            deadline = datetime.fromisoformat(item.choice_deadline)
            self._notify(item, "ARRIVED_CHOICE", "택배가 도착했어요",
                         f"{deadline:%H:%M}까지 배송 시간을 예약할 수 있어요. 예약하지 않으면 자동 배송됩니다.")
        elif item.direct_lock == DirectLock.OVERSIZE:
            self._notify(item, "ARRIVED_DIRECT", "직접 수령 택배가 도착했어요",
                         f"규격 초과 물품이라 배송 불가 보관함 {space_index(item.space_id):02d}번에 보관했어요. "
                         "출력 키오스크에서 찾아가세요.")
        elif item.direct_lock == DirectLock.FRAGILE:
            self._notify(item, "ARRIVED_DIRECT", "직접 수령 택배가 도착했어요",
                         "취급 주의 물품이라 로봇 배송 없이 직접 수령만 가능해요. 출력 키오스크에서 찾아가세요.")
        else:
            self._notify(item, "ARRIVED_DIRECT", "택배가 도착했어요",
                         "직접 수령으로 보관 중이에요. 출력 키오스크에서 찾아가세요.")

    def _space_text(self, item: Item) -> str:
        if item.storage_area == Area.OVERSIZE:
            return f"배송 불가 보관함 {space_index(item.space_id):02d}번 문"
        return "그리드 입력구 보관함"

    def _label(self, item: Item) -> str:
        return self.site.label(item.building, item.unit) if self.site else f"{item.building}-{item.unit}"

    def _building_name(self, code: int) -> str:
        return self.site.building_name(code) if self.site else str(code)

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

    def _publish_site(self):
        self._emit(config.TOPIC_SITE_CONFIG, {
            "type": "SITE_CONFIG", "initialized": self.site is not None,
            "version": self.site.version if self.site else 0,
            "site": self.site.public() if self.site else None,
            "timestamp": self.now().isoformat()}, retain=True)
        self._dirty = True

    def summary(self) -> dict:
        waiting = self.store.mode_counts(PICKABLE_STATUSES)
        site = self.site
        return {
            "type": "SUMMARY",
            "timestamp": self.now().isoformat(),
            "initialized": site is not None,
            "site_version": site.version if site else 0,
            "occupancy": self.storage.occupancy(site.grid_bins if site else 0,
                                                site.oversize_doors if site else 0),
            "status": self.store.status_counts(),
            "waiting": {"ROBOT": waiting.get(Mode.ROBOT.value, 0),
                        "DIRECT": waiting.get(Mode.DIRECT.value, 0)},
            "robots": site.robots if site else 0,
            "active_jobs": self.store.active_jobs(),
        }

    def _flush(self):
        """커밋된 변경을 발행: 세대 목록 갱신 → 예약 칸 변동 → 상태 요약 → outbox."""
        now = self.now()
        for building, unit in self._touched:
            clients = {s["client_id"] for s in self.sessions.household(building, unit, now)}
            if not clients or not self.site:
                continue
            update = {"type": "PARCELS_UPDATE", "household": self._household(building, unit),
                      "parcels": self._kiosk_parcels(building, unit)}
            for client_id in sorted(clients):
                self._send(client_id, update)
        self._touched.clear()
        if self._slots_changed and self.site:
            counts: dict[str, int] = {}
            for key in self._slots_changed:
                counts[key] = config.SLOT_SEATS - len(self.store.seats_of(key))
            self._emit(config.TOPIC_SLOTS_UPDATE, {
                "type": "SLOTS_UPDATE", "timestamp": now.isoformat(),
                "slots": [{"start": k, "seats_left": v} for k, v in sorted(counts.items())]})
        self._slots_changed.clear()
        if self._dirty:
            self._emit(config.TOPIC_SUMMARY, self.summary(), retain=True)
            self._dirty = False
        self._outbox = []
        for message in self.store.pending_messages():
            try:
                self.publish(message["topic"], json.loads(message["payload"]), retain=bool(message["retain"]))
            except Exception as exc:
                log("MQTT", f"발행 보류 (재연결 후 재시도): {exc}")
                break
            self.store.message_sent(message["id"])


def _to_mode(value) -> str:
    mode = str(value or "").upper()
    if mode not in Mode.__members__:
        raise RequestError("INVALID", "mode 는 ROBOT 또는 DIRECT 여야 합니다.")
    return mode


def _lock_text(lock: str) -> str:
    return {
        DirectLock.FRAGILE.value: "취급 주의 물품은 직접 수령만 가능합니다.",
        DirectLock.OVERSIZE.value: "규격 초과 물품은 배송 불가 보관함에 있어 직접 수령만 가능합니다.",
        DirectLock.FAILED.value: "배달에 두 번 실패한 물품은 직접 수령만 가능합니다.",
    }.get(lock, "직접 수령만 가능한 물품입니다.")


def _notification(row: dict) -> dict:
    return {k: row.get(k) for k in ("id", "kind", "title", "body", "receipt_no", "created_at", "read_at")}

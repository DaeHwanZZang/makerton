"""관리자 요청 처리 (LockerCore 에 섞어 쓰는 mixin).

관리자 웹(admin_web) BFF 만 client_id 접두어 ADMIN-WEB- 로 이 토픽들을 쓴다.
  · 초기 설정 / 설정 변경 / 전체 재설정
  · 세대 계정 목록 / 비밀번호 초기화(1234) / 입주자 변경 / 잠금 해제
  · 보관 현황 / 코드로 수령 처리 / 작업 기록
변경 작업은 events 테이블에 ADMIN_* 로 남긴다.
"""
from __future__ import annotations

import re
from datetime import datetime

import config
from auth import hash_password, verify_password
from common import RequestError, locked_for, log, to_int
from models import ACTIVE_STATUSES, Area, Mode, Plan, Status
from site_config import Site, SiteError
from storage import space_index

import reservations as rsv


class AdminMixin:
    ADMIN_ROUTES = {
        config.TOPIC_ADMIN_LOGIN: ("_admin_login", "ADMIN_LOGIN_RESPONSE"),
        config.TOPIC_ADMIN_LOGOUT: ("_admin_logout", "ADMIN_LOGOUT_RESPONSE"),
        config.TOPIC_ADMIN_PASSWORD: ("_admin_password", "ADMIN_PASSWORD_RESPONSE"),
        config.TOPIC_ADMIN_SITE: ("_admin_site_get", "ADMIN_SITE_RESPONSE"),
        config.TOPIC_ADMIN_SITE_PREVIEW: ("_admin_site_preview", "ADMIN_SITE_PREVIEW_RESPONSE"),
        config.TOPIC_ADMIN_SITE_INIT: ("_admin_site_init", "ADMIN_SITE_INIT_RESPONSE"),
        config.TOPIC_ADMIN_SITE_UPDATE: ("_admin_site_update", "ADMIN_SITE_UPDATE_RESPONSE"),
        config.TOPIC_ADMIN_SITE_RESET: ("_admin_site_reset", "ADMIN_SITE_RESET_RESPONSE"),
        config.TOPIC_ADMIN_ACCOUNTS: ("_admin_accounts", "ADMIN_ACCOUNTS_RESPONSE"),
        config.TOPIC_ADMIN_RESET_PASSWORD: ("_admin_reset_password", "ADMIN_RESET_PASSWORD_RESPONSE"),
        config.TOPIC_ADMIN_CHANGE_OCCUPANT: ("_admin_change_occupant", "ADMIN_CHANGE_OCCUPANT_RESPONSE"),
        config.TOPIC_ADMIN_UNLOCK: ("_admin_unlock", "ADMIN_UNLOCK_RESPONSE"),
        config.TOPIC_ADMIN_STORAGE: ("_admin_storage", "ADMIN_STORAGE_RESPONSE"),
        config.TOPIC_ADMIN_CODE_PICKUP: ("_admin_code_pickup", "ADMIN_CODE_PICKUP_RESPONSE"),
        config.TOPIC_ADMIN_EVENTS: ("_admin_events", "ADMIN_EVENTS_RESPONSE"),
        config.TOPIC_ADMIN_DEMO_SKIP: ("_admin_demo_skip", "ADMIN_DEMO_SKIP_RESPONSE"),
    }

    # --- 계정·세션 -------------------------------------------------------
    def _seed_admin(self):
        if not self.store.get_admin(config.ADMIN_ID):
            password = config.ADMIN_ENV_PASSWORD or config.ADMIN_DEFAULT_PASSWORD
            self.store.insert_admin(config.ADMIN_ID, hash_password(password))
            log("초기화", f"관리자 계정 '{config.ADMIN_ID}' 생성"
                        f"{'' if config.ADMIN_ENV_PASSWORD else ' (기본 비밀번호 — 로그인 후 바꾸세요)'}")

    def _admin_client(self, client_id: str):
        if not client_id.startswith(config.ADMIN_CLIENT_PREFIX):
            raise RequestError("DENIED", "관리자 요청은 관리자 웹 서버를 통해서만 받을 수 있습니다.")

    def _admin(self, p: dict, client_id: str) -> dict:
        self._admin_client(client_id)
        session = self.sessions.get(p.get("session"), self.now(), "ADMIN")
        if not session:
            raise RequestError("EXPIRED_SESSION", "관리자 로그인이 만료되었습니다. 다시 로그인해 주세요.")
        return session

    def _admin_log(self, kind: str, admin_id: str, data: dict):
        self.store.log_event(self.now().isoformat(), kind, {"admin": admin_id, **data})

    def _admin_login(self, p: dict, client_id: str) -> dict:
        self._admin_client(client_id)
        now = self.now()
        admin = self.store.get_admin(str(p.get("admin_id") or config.ADMIN_ID).strip())
        denied = {"type": "ADMIN_LOGIN_RESPONSE", "status": "DENIED",
                  "message": "아이디 또는 비밀번호가 올바르지 않습니다."}
        if not admin:
            return denied
        locked = locked_for(admin, now)
        if locked:
            return {**denied, "status": "LOCKED", "retry_after_sec": locked,
                    "message": f"비밀번호를 여러 번 틀려 잠겼습니다. {-(-locked // 60)}분 뒤 다시 시도하세요."}
        if not verify_password(str(p.get("password") or ""), admin["password_hash"]):
            return self._login_failed(admin, now, denied, "admins")
        self.store.update_admin(admin["admin_id"], failed_count=0, locked_until=None)
        token = self.sessions.create(kind="ADMIN", user_id=admin["admin_id"], channel="ADMIN",
                                     client_id=client_id, now=now, ttl_sec=config.ADMIN_SESSION_TTL_SEC)
        self._admin_log("ADMIN_LOGIN", admin["admin_id"], {})
        return {"status": "OK", "session": token, "ttl_sec": config.ADMIN_SESSION_TTL_SEC,
                "admin_id": admin["admin_id"], **self._admin_flags(admin)}

    def _admin_flags(self, admin: dict) -> dict:
        return {"default_password": verify_password(config.ADMIN_DEFAULT_PASSWORD, admin["password_hash"]),
                "initialized": self.site is not None,
                "site_name": self.site.name if self.site else None}

    def _admin_logout(self, p: dict, client_id: str) -> dict:
        self._admin_client(client_id)
        self.sessions.revoke(p.get("session"))
        return {"status": "OK"}

    def _admin_password(self, p: dict, client_id: str) -> dict:
        s = self._admin(p, client_id)
        admin = self.store.get_admin(s["user_id"])
        current = str(p.get("current_password") or "")
        new = str(p.get("new_password") or "")
        if not re.fullmatch(config.ADMIN_PASSWORD_RE, new):
            raise RequestError("INVALID", "새 비밀번호는 영문과 숫자를 섞어 8자 이상입니다.")
        if new in (current, config.ADMIN_DEFAULT_PASSWORD):
            raise RequestError("INVALID", "현재·기본 비밀번호와 다른 비밀번호를 입력하세요.")
        if not verify_password(current, admin["password_hash"]):
            raise RequestError("DENIED", "현재 비밀번호가 올바르지 않습니다.")
        self.store.update_admin(s["user_id"], password_hash=hash_password(new),
                                password_changed_at=self.now().isoformat())
        revoked = self.sessions.revoke_user("ADMIN", s["user_id"], keep_token=p.get("session"))
        self._admin_log("ADMIN_PASSWORD", s["user_id"], {"revoked": revoked})
        return {"status": "OK", "revoked": revoked, "default_password": False}

    # --- 현장 설정 -------------------------------------------------------
    def _admin_site_get(self, p: dict, client_id: str) -> dict:
        s = self._admin(p, client_id)
        admin = self.store.get_admin(s["user_id"])
        site = self.site
        return {"status": "OK", **self._admin_flags(admin),
                "site": site.to_dict() if site else None,
                "public": site.public() if site else None,
                "defaults": config.SITE_DEFAULTS,
                "accounts": self.store.account_stats(),
                "active_items": len(self.store.items(ACTIVE_STATUSES)),
                "occupancy": self.summary()["occupancy"], "demo": config.DEMO_MODE}

    def _candidate(self, p: dict) -> Site:
        try:
            return Site.from_dict(p.get("site"))
        except SiteError as e:
            raise RequestError("INVALID", str(e)) from None

    def _site_diff(self, new: Site) -> dict:
        old = self.site
        old_keys = set(old.households()) if old else set()
        new_keys = set(new.households())
        added, removed = new_keys - old_keys, old_keys - new_keys
        blocked = []
        busy = sorted({it.household for it in self.store.items(ACTIVE_STATUSES) if it.household in removed})
        if busy:
            labels = [old.label(*h) for h in busy[:5]]
            more = f" 외 {len(busy) - 5}곳" if len(busy) > 5 else ""
            blocked.append(f"보관·배송 중인 물품이 있는 호실은 뺄 수 없습니다: {', '.join(labels)}{more}")
        for area, total, name in ((Area.GRID.value, new.grid_bins, "그리드 보관함"),
                                  (Area.OVERSIZE.value, new.oversize_doors, "배송 불가 보관함")):
            used = self.storage.max_used_index(area)
            if used > total:
                blocked.append(f"{name} {used}번 칸을 사용 중이라 {used}개보다 줄일 수 없습니다.")
        # 건물 번호는 계정 아이디라 바꾸지 않는다. 번호가 다르면 다른 건물(기존 건물 삭제 + 새 건물)이다.
        return {"added": len(added), "removed": len(removed), "blocked": blocked,
                "added_keys": added, "removed_keys": removed}

    def _admin_site_preview(self, p: dict, client_id: str) -> dict:
        self._admin(p, client_id)
        site = self._candidate(p)
        out = {"status": "OK", "account_count": site.account_count(),
               "buildings": [{"code": b.code, "name": b.name, "count": len(site.units_of(b.code)),
                              "guide": b.guide(site.unit_term)} for b in site.buildings]}
        if self.site:
            diff = self._site_diff(site)
            out["diff"] = {k: diff[k] for k in ("added", "removed", "blocked")}
        return out

    def _admin_site_init(self, p: dict, client_id: str) -> dict:
        s = self._admin(p, client_id)
        if self.site:
            raise RequestError("ALREADY_INITIALIZED", "이미 초기 설정이 되어 있습니다. '설정 변경'을 사용하세요.")
        site = self._candidate(p)
        now = self.now()
        seed = hash_password(config.SEED_PASSWORD)   # 같은 초기 비밀번호라 해시 하나를 함께 쓴다
        self.store.db.execute("DELETE FROM accounts")
        self.store.insert_accounts(self._account_row(b, u, seed, now) for b, u in site.households())
        self._commit_site(site, now)
        self._admin_log("ADMIN_SITE_INIT", s["user_id"],
                        {"name": site.name, "buildings": len(site.buildings), "accounts": site.account_count()})
        log("관리자", f"초기 설정 '{site.name}' · 건물 {len(site.buildings)}개 · 계정 {site.account_count()}개")
        return {"status": "OK", "site": site.to_dict(), "accounts": site.account_count()}

    def _admin_site_update(self, p: dict, client_id: str) -> dict:
        s = self._admin(p, client_id)
        if not self.site:
            raise RequestError("NOT_INITIALIZED", "먼저 초기 설정을 하세요.")
        site = self._candidate(p)
        diff = self._site_diff(site)
        if diff["blocked"]:
            raise RequestError("BLOCKED", diff["blocked"][0], blocked=diff["blocked"])
        now = self.now()
        seed = hash_password(config.SEED_PASSWORD) if diff["added_keys"] else None
        for building, unit in sorted(diff["removed_keys"]):
            account = self.store.get_account_by_unit(building, unit)
            if account:
                self.store.update_account(account["user_id"], active=0)
                self.sessions.revoke_user("USER", account["user_id"])
        new_rows = []
        for building, unit in sorted(diff["added_keys"]):
            account = self.store.get_account_by_unit(building, unit)
            if account:      # 다시 생긴 호실은 새 입주자로 보고 초기화
                self.store.update_account(account["user_id"], active=1, password_hash=seed, must_change=1,
                                          default_mode=config.DEFAULT_MODE, failed_count=0,
                                          locked_until=None, occupant_since=now.isoformat())
            else:
                new_rows.append(self._account_row(building, unit, seed, now))
        self.store.insert_accounts(new_rows)
        robots_changed = site.robots != self.site.robots
        self._commit_site(site, now)
        self._admin_log("ADMIN_SITE_UPDATE", s["user_id"],
                        {"version": site.version, "added": diff["added"], "removed": diff["removed"]})
        log("관리자", f"설정 변경 v{site.version} · 호실 +{diff['added']} −{diff['removed']}")
        if robots_changed:
            self._issue_jobs()
        return {"status": "OK", "site": site.to_dict(), "added": diff["added"], "removed": diff["removed"]}

    def _admin_site_reset(self, p: dict, client_id: str) -> dict:
        s = self._admin(p, client_id)
        if not self.site:
            raise RequestError("NOT_INITIALIZED", "초기 설정이 되어 있지 않습니다.")
        admin = self.store.get_admin(s["user_id"])
        if not verify_password(str(p.get("password") or ""), admin["password_hash"]):
            raise RequestError("DENIED", "관리자 비밀번호가 올바르지 않습니다.")
        if str(p.get("confirm_name") or "").strip() != self.site.name:
            raise RequestError("MISMATCH", f"확인을 위해 사이트 이름 '{self.site.name}' 을 그대로 입력하세요.")
        active = len(self.store.items(ACTIVE_STATUSES))
        if active or self.store.active_jobs():
            raise RequestError("BLOCKED", f"보관·배송 중인 물품이 {active}개 있어 전체 재설정할 수 없습니다.")
        name = self.site.name
        self.store.delete_all_items()
        self.store.db.execute("DELETE FROM accounts")
        self.store.delete_sessions_of_kind("USER")
        self.store.delete_site()
        self.site = None
        self._publish_site()
        self._admin_log("ADMIN_SITE_RESET", s["user_id"], {"name": name})
        log("관리자", f"전체 재설정 — '{name}' 설정·계정·기록 삭제")
        return {"status": "OK"}

    def _commit_site(self, site: Site, now: datetime):
        site.version = self.store.next_site_version()
        site.updated_at = now.isoformat()
        self.store.save_site(site.to_dict())
        self.site = site
        self._publish_site()

    @staticmethod
    def _account_row(building: int, unit: int, seed: str, now: datetime) -> dict:
        return {"user_id": f"{building}-{unit}", "building": building, "unit": unit,
                "password_hash": seed, "default_mode": config.DEFAULT_MODE, "must_change": 1,
                "failed_count": 0, "active": 1, "created_at": now.isoformat(),
                "occupant_since": now.isoformat()}

    # --- 세대 계정 -------------------------------------------------------
    def _admin_accounts(self, p: dict, client_id: str) -> dict:
        self._admin(p, client_id)
        site = self._site()
        building = to_int(p.get("building"))
        if not site.building(building):
            building = site.buildings[0].code
        now = self.now()
        query = str(p.get("q") or "").strip()
        rows = []
        for a in self.store.accounts(building=building):
            if query and query not in str(a["unit"]):
                continue
            locked = bool(a["locked_until"] and a["locked_until"] > now.isoformat())
            rows.append({"user_id": a["user_id"], "building": a["building"], "unit": a["unit"],
                         "label": site.label(a["building"], a["unit"]), "active": bool(a["active"]),
                         "must_change": bool(a["must_change"]), "locked": locked,
                         "failed_count": a["failed_count"], "default_mode": a["default_mode"],
                         "occupant_since": a["occupant_since"],
                         "password_changed_at": a["password_changed_at"]})
        return {"status": "OK", "building": building, "accounts": rows,
                "stats": self.store.account_stats(),
                "buildings": [{"code": b.code, "name": b.name} for b in site.buildings]}

    def _target_account(self, p: dict) -> dict:
        account = self.store.get_account(str(p.get("user_id") or "").strip())
        if not account:
            raise RequestError("NOT_FOUND", "세대 계정을 찾을 수 없습니다.")
        return account

    def _admin_reset_password(self, p: dict, client_id: str) -> dict:
        s = self._admin(p, client_id)
        account = self._target_account(p)
        self._reset_account(account)
        revoked = self.sessions.revoke_user("USER", account["user_id"])
        self._admin_log("ADMIN_RESET_PASSWORD", s["user_id"], {"user_id": account["user_id"], "revoked": revoked})
        log("관리자", f"{account['user_id']} 비밀번호 초기화 ({config.SEED_PASSWORD})")
        return {"status": "OK", "user_id": account["user_id"], "revoked": revoked}

    def _admin_change_occupant(self, p: dict, client_id: str) -> dict:
        s = self._admin(p, client_id)
        account = self._target_account(p)
        now = self.now()
        self._reset_account(account, default_mode=config.DEFAULT_MODE, occupant_since=now.isoformat())
        revoked = self.sessions.revoke_user("USER", account["user_id"])
        self._admin_log("ADMIN_CHANGE_OCCUPANT", s["user_id"], {"user_id": account["user_id"]})
        log("관리자", f"{account['user_id']} 입주자 변경 (비밀번호 {config.SEED_PASSWORD}, 이전 기록 숨김)")
        return {"status": "OK", "user_id": account["user_id"], "revoked": revoked}

    def _reset_account(self, account: dict, **extra):
        self.store.update_account(account["user_id"], password_hash=hash_password(config.SEED_PASSWORD),
                                  must_change=1, failed_count=0, locked_until=None,
                                  password_changed_at=None, **extra)

    def _admin_unlock(self, p: dict, client_id: str) -> dict:
        s = self._admin(p, client_id)
        account = self._target_account(p)
        self.store.update_account(account["user_id"], failed_count=0, locked_until=None)
        self._admin_log("ADMIN_UNLOCK", s["user_id"], {"user_id": account["user_id"]})
        return {"status": "OK", "user_id": account["user_id"]}

    # --- 보관 현황·코드 수령·기록 ----------------------------------------
    def _admin_storage(self, p: dict, client_id: str) -> dict:
        self._admin(p, client_id)
        site = self._site()
        items = []
        for it in self.store.items(ACTIVE_STATUSES):
            start = datetime.fromisoformat(it.slot_start) if it.slot_start else None
            items.append({"receipt_no": it.receipt_no, "label": site.label(it.building, it.unit),
                          "building": it.building, "unit": it.unit, "status": it.status,
                          "mode": it.mode, "plan": it.plan, "storage_area": it.storage_area,
                          "space_id": it.space_id, "fragile": bool(it.fragile),
                          "direct_lock": it.direct_lock, "slot_label": rsv.label(start) if start else None,
                          "job_id": it.job_id, "robot_id": it.robot_id, "stored_at": it.stored_at,
                          "registered_at": it.registered_at, "tracking_no": it.tracking_no})
        summary = self.summary()
        return {"status": "OK", "occupancy": summary["occupancy"], "waiting": summary["waiting"],
                "active_jobs": summary["active_jobs"], "items": items, "demo": config.DEMO_MODE}

    def _admin_code_pickup(self, p: dict, client_id: str) -> dict:
        s = self._admin(p, client_id)
        site = self._site()
        code = str(p.get("code") or "").strip()
        if not re.fullmatch(r"\d{6}", code):
            raise RequestError("INVALID", "인증코드 6자리를 입력하세요.")
        items = self.store.items_by_pickup_code(code, ACTIVE_STATUSES)
        if not items:
            raise RequestError("NOT_FOUND", "이 인증코드로 보관 중인 택배가 없습니다.")
        item = items[0]
        info = {"receipt_no": item.receipt_no, "label": site.label(item.building, item.unit),
                "storage_area": item.storage_area, "space_id": item.space_id, "status": item.status,
                "door": space_index(item.space_id) if item.storage_area == Area.OVERSIZE else None,
                "fragile": bool(item.fragile), "mode": item.mode}
        if not p.get("confirm"):
            if item.status not in ("STORED", "JOB_ASSIGNED"):
                raise RequestError("ALREADY_LOADED", "이미 로봇에 적재된 택배입니다.")
            return {"status": "OK", "confirmed": False, "item": info}
        self._retrieve(item, "ADMIN")
        self._admin_log("ADMIN_CODE_PICKUP", s["user_id"], {"receipt_no": item.receipt_no,
                                                           "label": info["label"]})
        return {"status": "OK", "confirmed": True, "item": info}

    def _admin_events(self, p: dict, client_id: str) -> dict:
        self._admin(p, client_id)
        limit = min(500, max(1, to_int(p.get("limit")) or 100))
        prefix = None if p.get("all") else "ADMIN_"
        return {"status": "OK", "events": self.store.events(limit, prefix)}

    # --- 대회 시연 -------------------------------------------------------
    def _admin_demo_skip(self, p: dict, client_id: str) -> dict:
        """시연 전용: 로봇 배송 대기 시간을 바로 끝낸다 (LASTGATE_DEMO=1 일 때만).

        - 배송 시간 예약 가능 시간(CHOICE_OPEN, 보관 후 30분) → 지금 자동 배송 확정
        - 예약(RESERVED) → 칸 시작 15분 전까지 기다리지 않고 지금 발행 대상 (물품에 RUSH 표시)
        그 뒤 배송 발행을 바로 검사한다. 2개가 모여야 출발하는 규칙과 로봇 대수 제한은 그대로다.
        """
        s = self._admin(p, client_id)
        if not config.DEMO_MODE:
            raise RequestError("DISABLED", "시연 기능이 꺼져 있습니다. 보관소 서버를 LASTGATE_DEMO=1 로 실행하세요.")
        site = self._site()
        now = self.now()
        auto, rushed = [], []
        for it in self.store.items([Status.STORED], mode=Mode.ROBOT.value, unassigned=True):
            if it.direct_lock:
                continue
            if it.plan == Plan.CHOICE_OPEN:
                self._make_auto(it, now)
                auto.append(it.receipt_no)
            elif it.plan == Plan.RESERVED and not it.has_note("RUSH"):
                it.add_note("RUSH")
                self._save(it)
                rushed.append(it.receipt_no)
        before = {j["job_id"] for j in self.store.active_jobs()}
        self._issue_jobs()
        jobs = [j["job_id"] for j in self.store.active_jobs() if j["job_id"] not in before]
        left = [it for it in self.store.items([Status.STORED], mode=Mode.ROBOT.value, unassigned=True)
                if not it.direct_lock and it.plan in (Plan.AUTO, Plan.RESERVED)]
        busy = len(self.store.active_jobs()) >= site.robots
        parts = [f"자동 배송 확정 {len(auto)}개", f"예약 즉시 발행 {len(rushed)}개", f"새 배송 {len(jobs)}건"]
        if left:
            parts.append(f"대기 {len(left)}개 (" + ("로봇이 모두 배송 중" if busy else "2개가 모여야 출발") + ")")
        self._admin_log("ADMIN_DEMO_SKIP", s["user_id"], {"auto": auto, "rushed": rushed, "jobs": jobs})
        log("시연", " · ".join(parts))
        return {"status": "OK", "auto": auto, "rushed": rushed, "jobs": jobs,
                "waiting": [{"receipt_no": it.receipt_no, "label": site.label(it.building, it.unit)} for it in left],
                "robots_busy": busy, "summary": " · ".join(parts)}

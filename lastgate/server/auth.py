"""계정 인증과 세션 관리.

비밀번호는 bcrypt 해시로만 저장한다. 세션은 DB 에 토큰 해시(sha256)로 저장해
서버를 재시작해도 세대 웹·관리자 웹 로그인이 유지된다.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

import bcrypt

import config


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(config.BCRYPT_ROUNDS)).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Sessions:
    """kind: USER(세대 계정) / ADMIN. channel: KIOSK / RESIDENT / ADMIN."""

    def __init__(self, store):
        self.store = store

    def create(self, *, kind: str, user_id: str, channel: str, client_id: str, now: datetime,
               ttl_sec: int, building: int | None = None, unit: int | None = None) -> str:
        token = "tok_" + secrets.token_urlsafe(24)
        self.store.insert_session({
            "token_hash": token_hash(token), "kind": kind, "user_id": user_id,
            "building": building, "unit": unit, "channel": channel, "client_id": client_id,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=ttl_sec)).isoformat(), "ttl_sec": ttl_sec})
        return token

    def get(self, token: str | None, now: datetime, kind: str) -> dict | None:
        """유효하면 만료 시각을 연장해 돌려준다."""
        if not token or not isinstance(token, str):
            return None
        hashed = token_hash(token)
        session = self.store.get_session(hashed)
        if not session or session["kind"] != kind:
            return None
        if session["expires_at"] <= now.isoformat():
            self.store.delete_session(hashed)
            return None
        expires = (now + timedelta(seconds=session["ttl_sec"])).isoformat()
        self.store.touch_session(hashed, expires)
        session["expires_at"] = expires
        return session

    def revoke(self, token: str | None):
        if token and isinstance(token, str):
            self.store.delete_session(token_hash(token))

    def revoke_user(self, kind: str, user_id: str, keep_token: str | None = None) -> int:
        return self.store.delete_user_sessions(kind, user_id,
                                               token_hash(keep_token) if keep_token else None)

    def household(self, building: int, unit: int, now: datetime) -> list[dict]:
        return self.store.household_sessions(building, unit, now.isoformat())

    def purge(self, now: datetime):
        self.store.purge_sessions(now.isoformat())

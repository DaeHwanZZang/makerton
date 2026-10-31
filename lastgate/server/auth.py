"""계정 인증과 세션 관리.

비밀번호는 bcrypt 해시로만 저장한다. 세션은 메모리에 두며, 서버를 재시작하면
다시 로그인해야 한다(키오스크는 60초 무조작 시 자동 로그아웃하므로 문제없음).
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
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


@dataclass
class Session:
    token: str
    user_id: str
    dong: int
    ho: int
    client_id: str
    expires_at: datetime
    ttl_sec: int = config.SESSION_TTL_SEC


class SessionManager:
    def __init__(self):
        self._sessions: dict[str, Session] = {}

    def create(self, user_id: str, dong: int, ho: int, client_id: str, now: datetime,
               ttl_sec: int = config.SESSION_TTL_SEC) -> Session:
        token = "tok_" + secrets.token_urlsafe(18)
        session = Session(token, user_id, dong, ho, client_id,
                          now + timedelta(seconds=ttl_sec), ttl_sec)
        self._sessions[token] = session
        return session

    def get(self, token: str | None, now: datetime) -> Session | None:
        session = self._sessions.get(token or "")
        if not session:
            return None
        if session.expires_at <= now:
            self._sessions.pop(session.token, None)
            return None
        session.expires_at = now + timedelta(seconds=session.ttl_sec)
        return session

    def revoke(self, token: str | None):
        self._sessions.pop(token or "", None)

    def for_household(self, dong: int, ho: int) -> list[Session]:
        return [s for s in self._sessions.values() if s.dong == dong and s.ho == ho]

    def purge(self, now: datetime):
        for token in [t for t, s in self._sessions.items() if s.expires_at <= now]:
            del self._sessions[token]

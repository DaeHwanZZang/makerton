"""core.py·admin.py 공용 보조."""
from __future__ import annotations

from datetime import datetime

import config


def log(tag: str, message: str):
    print(f"[{config.now_kst():%H:%M:%S}] [{tag}] {message}", flush=True)


class RequestError(Exception):
    """요청 거절. status·message(+추가 필드)가 그대로 응답이 되고 트랜잭션은 롤백된다."""

    def __init__(self, status: str, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


def locked_for(account: dict, now: datetime) -> int:
    """로그인 잠금이 남았으면 남은 초, 아니면 0."""
    until = account.get("locked_until")
    if not until:
        return 0
    left = (datetime.fromisoformat(until) - now).total_seconds()
    return int(left) + 1 if left > 0 else 0


def to_int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None

"""SQLite 상태 저장소.

서버 재시작 후에도 세대·계정·물품·Job 상태가 유지된다.
동시 접근은 LockerCore 의 락으로 직렬화하고, 요청 하나를 트랜잭션 하나로 묶는다.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, fields
from pathlib import Path
from typing import Iterable

from models import Item

SCHEMA = """
CREATE TABLE IF NOT EXISTS households (
    dong INTEGER NOT NULL,
    ho INTEGER NOT NULL,
    default_mode TEXT NOT NULL,
    PRIMARY KEY (dong, ho)
);
CREATE TABLE IF NOT EXISTS accounts (
    user_id TEXT PRIMARY KEY,
    dong INTEGER NOT NULL,
    ho INTEGER NOT NULL,
    password_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    receipt_no TEXT PRIMARY KEY,
    dong INTEGER NOT NULL,
    ho INTEGER NOT NULL,
    size TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    pickup_code TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    tracking_no TEXT,
    input_client TEXT,
    input_kiosk TEXT,
    hidden_at TEXT,
    slot_id TEXT,
    job_id TEXT,
    fail_count INTEGER NOT NULL DEFAULT 0,
    stored_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_status ON items (status);
CREATE INDEX IF NOT EXISTS idx_items_household ON items (dong, ho);
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,          -- PENDING / CONFIRMED / DONE / CANCELLED
    robot_id TEXT,
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    done_at TEXT,
    bundle TEXT,                   -- FMS 로 보낸 묶음 (JSON)
    metrics TEXT                   -- 묶음 효율 지표 (JSON)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    data TEXT
);
CREATE TABLE IF NOT EXISTS mqtt_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT NOT NULL,
    payload TEXT NOT NULL,
    retain INTEGER NOT NULL DEFAULT 0
);
"""

ITEM_FIELDS = [f.name for f in fields(Item)]
ACTIVE_JOB_STATUSES = ("PENDING", "CONFIRMED")


class Store:
    def __init__(self, path: Path | str, reset: bool = False):
        if str(path) != ":memory:":
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            if reset and path.exists():
                path.unlink()
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(items)")}
        for column in ("input_kiosk", "hidden_at"):      # 이전 버전 DB 호환
            if column not in columns:
                self.db.execute(f"ALTER TABLE items ADD COLUMN {column} TEXT")

    @contextmanager
    def transaction(self):
        if self.db.in_transaction:      # 중첩 호출은 바깥 트랜잭션에 합류
            yield
            return
        self.db.execute("BEGIN")
        try:
            yield
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    # --- 세대·계정 -------------------------------------------------------
    def households_empty(self) -> bool:
        return self.db.execute("SELECT COUNT(*) FROM households").fetchone()[0] == 0

    def seed_households(self, rows: Iterable[tuple[int, int, str, str]], default_mode: str):
        """rows: (dong, ho, user_id, password_hash)"""
        with self.transaction():
            for dong, ho, user_id, pw_hash in rows:
                self.db.execute("INSERT INTO households VALUES (?, ?, ?)", (dong, ho, default_mode))
                self.db.execute("INSERT INTO accounts VALUES (?, ?, ?, ?)",
                                (user_id, dong, ho, pw_hash))

    def get_default_mode(self, dong: int, ho: int) -> str | None:
        row = self.db.execute("SELECT default_mode FROM households WHERE dong=? AND ho=?",
                              (dong, ho)).fetchone()
        return row["default_mode"] if row else None

    def set_default_mode(self, dong: int, ho: int, mode: str):
        self.db.execute("UPDATE households SET default_mode=? WHERE dong=? AND ho=?",
                        (mode, dong, ho))

    def get_account(self, user_id: str):
        return self.db.execute("SELECT * FROM accounts WHERE user_id=?", (user_id,)).fetchone()

    # --- 물품 ------------------------------------------------------------
    def insert_item(self, item: Item):
        cols = ", ".join(ITEM_FIELDS)
        marks = ", ".join("?" for _ in ITEM_FIELDS)
        self.db.execute(f"INSERT INTO items ({cols}) VALUES ({marks})",
                        [getattr(item, f) for f in ITEM_FIELDS])

    def update_item(self, item: Item):
        sets = ", ".join(f"{f}=?" for f in ITEM_FIELDS if f != "receipt_no")
        values = [getattr(item, f) for f in ITEM_FIELDS if f != "receipt_no"]
        self.db.execute(f"UPDATE items SET {sets} WHERE receipt_no=?", values + [item.receipt_no])

    def get_item(self, receipt_no: str) -> Item | None:
        row = self.db.execute("SELECT * FROM items WHERE receipt_no=?", (receipt_no,)).fetchone()
        return Item(**dict(row)) if row else None

    def items(self, statuses: Iterable[str], *, mode: str | None = None,
              household: tuple[int, int] | None = None, job_id: str | None = None,
              unassigned: bool = False) -> list[Item]:
        statuses = [str(s.value if hasattr(s, "value") else s) for s in statuses]
        sql = f"SELECT * FROM items WHERE status IN ({','.join('?' for _ in statuses)})"
        args: list = list(statuses)
        if mode:
            sql += " AND mode=?"
            args.append(mode)
        if household:
            sql += " AND dong=? AND ho=?"
            args.extend(household)
        if job_id:
            sql += " AND job_id=?"
            args.append(job_id)
        if unassigned:
            sql += " AND job_id IS NULL"
        sql += " ORDER BY COALESCE(stored_at, registered_at)"
        return [Item(**dict(r)) for r in self.db.execute(sql, args)]

    def resident_items(self, household: tuple[int, int], active: Iterable[str],
                       completed: Iterable[str], completed_since: str) -> list[Item]:
        """세대 웹 목록: 진행 중 물품 전부 + since 이후 끝난 물품. 숨긴 물품은 제외."""
        active = [str(s.value if hasattr(s, "value") else s) for s in active]
        completed = [str(s.value if hasattr(s, "value") else s) for s in completed]
        rows = self.db.execute(
            f"SELECT * FROM items WHERE dong=? AND ho=? AND hidden_at IS NULL AND ("
            f"status IN ({','.join('?' for _ in active)}) OR "
            f"(status IN ({','.join('?' for _ in completed)}) AND updated_at >= ?)) "
            f"ORDER BY registered_at DESC",
            [*household, *active, *completed, completed_since])
        return [Item(**dict(r)) for r in rows]

    def receipt_exists(self, receipt_no: str) -> bool:
        return self.db.execute("SELECT 1 FROM items WHERE receipt_no=?",
                               (receipt_no,)).fetchone() is not None

    def count_stored_on(self, input_kiosk: str, date: str) -> int:
        """input_kiosk 로 접수되어 date(YYYY-MM-DD, KST)에 보관 완료된 물품 수."""
        return self.db.execute(
            "SELECT COUNT(*) FROM items WHERE input_kiosk=? AND substr(stored_at, 1, 10)=?",
            (input_kiosk, date)).fetchone()[0]

    def active_pickup_codes(self, active_statuses: Iterable[str]) -> set[str]:
        statuses = [str(s.value if hasattr(s, "value") else s) for s in active_statuses]
        rows = self.db.execute(
            f"SELECT pickup_code FROM items WHERE status IN ({','.join('?' for _ in statuses)})",
            statuses)
        return {r[0] for r in rows}

    def status_counts(self) -> dict[str, int]:
        rows = self.db.execute("SELECT status, COUNT(*) FROM items GROUP BY status")
        return {r[0]: r[1] for r in rows}

    def mode_counts(self, statuses: Iterable[str]) -> dict[str, int]:
        statuses = [str(s.value if hasattr(s, "value") else s) for s in statuses]
        rows = self.db.execute(
            f"SELECT mode, COUNT(*) FROM items WHERE status IN "
            f"({','.join('?' for _ in statuses)}) GROUP BY mode", statuses)
        return {r[0]: r[1] for r in rows}

    # --- Job -------------------------------------------------------------
    def insert_job(self, job_id: str, created_at: str, bundle: dict, metrics: dict):
        self.db.execute(
            "INSERT INTO jobs (job_id, status, created_at, bundle, metrics) VALUES (?, ?, ?, ?, ?)",
            (job_id, "PENDING", created_at, json.dumps(bundle, ensure_ascii=False),
             json.dumps(metrics, ensure_ascii=False)))

    def update_job(self, job_id: str, **values):
        for key in ("bundle", "metrics"):
            if key in values and not isinstance(values[key], str):
                values[key] = json.dumps(values[key], ensure_ascii=False)
        sets = ", ".join(f"{k}=?" for k in values)
        self.db.execute(f"UPDATE jobs SET {sets} WHERE job_id=?", [*values.values(), job_id])

    def get_job(self, job_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            return None
        job = dict(row)
        for key in ("bundle", "metrics"):
            job[key] = json.loads(job[key]) if job[key] else None
        return job

    def active_jobs(self) -> list[dict]:
        rows = self.db.execute(
            f"SELECT job_id, status FROM jobs WHERE status IN "
            f"({','.join('?' for _ in ACTIVE_JOB_STATUSES)}) ORDER BY created_at",
            ACTIVE_JOB_STATUSES)
        return [dict(r) for r in rows]

    def count_jobs_with_prefix(self, prefix: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM jobs WHERE job_id LIKE ?",
                               (prefix + "%",)).fetchone()[0]

    # --- 이벤트 로그 (시뮬레이션 지표 분석용) -----------------------------
    def log_event(self, at: str, kind: str, data: dict):
        self.db.execute("INSERT INTO events (at, kind, data) VALUES (?, ?, ?)",
                        (at, kind, json.dumps(data, ensure_ascii=False)))

    def enqueue_message(self, topic, payload, retain=False):
        self.db.execute("INSERT INTO mqtt_outbox (topic, payload, retain) VALUES (?, ?, ?)",
                        (topic, json.dumps(payload, ensure_ascii=False), int(retain)))

    def pending_messages(self):
        return list(self.db.execute("SELECT * FROM mqtt_outbox ORDER BY id"))

    def message_sent(self, message_id):
        self.db.execute("DELETE FROM mqtt_outbox WHERE id=?", (message_id,))


def item_to_dict(item: Item) -> dict:
    return asdict(item)

"""SQLite 상태 저장소.

서버 재시작 후에도 현장 설정·계정·세션·물품·예약·알림·Job 상태가 유지된다.
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
CREATE TABLE IF NOT EXISTS site (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    data TEXT NOT NULL,            -- 관리자 현장 설정 (JSON)
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    user_id TEXT PRIMARY KEY,      -- '{건물 번호}-{호실}'
    building INTEGER NOT NULL,
    unit INTEGER NOT NULL,
    password_hash TEXT NOT NULL,
    default_mode TEXT NOT NULL,
    must_change INTEGER NOT NULL DEFAULT 1,
    password_changed_at TEXT,
    failed_count INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    occupant_since TEXT,           -- 입주자 변경 시각. 이전 입주자의 완료 기록을 가린다
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT,
    UNIQUE (building, unit)
);
CREATE TABLE IF NOT EXISTS admins (
    admin_id TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    failed_count INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    password_changed_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,   -- 토큰 원문은 저장하지 않는다 (sha256)
    kind TEXT NOT NULL,            -- USER / ADMIN
    user_id TEXT NOT NULL,
    building INTEGER,
    unit INTEGER,
    channel TEXT NOT NULL,         -- KIOSK / RESIDENT / ADMIN
    client_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    ttl_sec INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_household ON sessions (building, unit);
CREATE TABLE IF NOT EXISTS items (
    receipt_no TEXT PRIMARY KEY,
    building INTEGER NOT NULL,
    unit INTEGER NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    pickup_code TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    storage_area TEXT NOT NULL DEFAULT 'GRID',
    space_id TEXT,
    fragile INTEGER NOT NULL DEFAULT 0,
    direct_lock TEXT,
    plan TEXT,
    choice_deadline TEXT,
    slot_start TEXT,
    auto_at TEXT,
    notified TEXT NOT NULL DEFAULT '',
    tracking_no TEXT,
    input_client TEXT,
    input_kiosk TEXT,
    hidden_at TEXT,
    job_id TEXT,
    robot_id TEXT,
    fail_count INTEGER NOT NULL DEFAULT 0,
    stored_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_status ON items (status);
CREATE INDEX IF NOT EXISTS idx_items_household ON items (building, unit);
CREATE TABLE IF NOT EXISTS slots (
    slot_start TEXT NOT NULL,      -- 배송 시간 칸 시작 (10분 단위)
    seat INTEGER NOT NULL,         -- 1, 2. 칸당 자리 = 로봇 1회 운행 보관함 수
    receipt_no TEXT NOT NULL UNIQUE,
    building INTEGER NOT NULL,
    unit INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (slot_start, seat)
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    building INTEGER NOT NULL,
    unit INTEGER NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    receipt_no TEXT,
    created_at TEXT NOT NULL,
    read_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_notifications_household ON notifications (building, unit, id);
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
ACCOUNT_COLUMNS = ("user_id", "building", "unit", "password_hash", "default_mode", "must_change",
                   "password_changed_at", "failed_count", "locked_until", "occupant_since",
                   "active", "created_at")


def _values(statuses: Iterable) -> list[str]:
    return [str(s.value if hasattr(s, "value") else s) for s in statuses]


def _marks(values: list) -> str:
    return ",".join("?" for _ in values)


class Store:
    def __init__(self, path: Path | str, reset: bool = False):
        if str(path) != ":memory:":
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            if reset and path.exists():
                path.unlink()
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        legacy = self._detach_legacy()
        self.db.executescript(SCHEMA)
        self.migrated_from_v3 = False
        if legacy:
            from migrate import migrate_v3
            with self.transaction():
                migrate_v3(self)
            self.migrated_from_v3 = True

    def _detach_legacy(self) -> bool:
        """v3 DB(households·dong/ho 컬럼)면 옛 테이블 이름을 v3_* 로 바꿔 둔다."""
        tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "households" not in tables:
            return False
        for name in ("households", "accounts", "items"):
            if name in tables:
                self.db.execute(f"ALTER TABLE {name} RENAME TO v3_{name}")
        return True

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

    # --- 현장 설정 -------------------------------------------------------
    def get_site(self) -> dict | None:
        row = self.db.execute("SELECT data FROM site WHERE id=1").fetchone()
        return json.loads(row["data"]) if row else None

    def save_site(self, data: dict):
        self.db.execute("INSERT INTO site (id, data, version) VALUES (1, ?, ?) "
                        "ON CONFLICT(id) DO UPDATE SET data=excluded.data, version=excluded.version",
                        (json.dumps(data, ensure_ascii=False), data["version"]))

    def delete_site(self):
        self.db.execute("DELETE FROM site")

    def next_site_version(self) -> int:
        """설정 버전은 전체 재설정 뒤에도 줄지 않는다 (클라이언트가 버전 변화로 새로고침)."""
        row = self.db.execute("SELECT value FROM meta WHERE key='site_version'").fetchone()
        version = int(row["value"]) + 1 if row else 1
        self.db.execute("INSERT INTO meta (key, value) VALUES ('site_version', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(version),))
        return version

    # --- 세대 계정 -------------------------------------------------------
    def insert_accounts(self, rows: Iterable[dict]):
        cols = ", ".join(ACCOUNT_COLUMNS)
        self.db.executemany(
            f"INSERT INTO accounts ({cols}) VALUES ({_marks(ACCOUNT_COLUMNS)})",
            [[row.get(c) for c in ACCOUNT_COLUMNS] for row in rows])

    def get_account(self, user_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM accounts WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def get_account_by_unit(self, building: int, unit: int) -> dict | None:
        row = self.db.execute("SELECT * FROM accounts WHERE building=? AND unit=?",
                              (building, unit)).fetchone()
        return dict(row) if row else None

    def update_account(self, user_id: str, **values):
        sets = ", ".join(f"{k}=?" for k in values)
        self.db.execute(f"UPDATE accounts SET {sets} WHERE user_id=?", [*values.values(), user_id])

    def accounts(self, building: int | None = None, active: bool | None = None) -> list[dict]:
        sql, args = "SELECT * FROM accounts WHERE 1=1", []
        if building is not None:
            sql += " AND building=?"
            args.append(building)
        if active is not None:
            sql += " AND active=?"
            args.append(int(active))
        sql += " ORDER BY building, unit"
        return [dict(r) for r in self.db.execute(sql, args)]

    def account_keys(self) -> set[tuple[int, int]]:
        return {(r[0], r[1]) for r in self.db.execute("SELECT building, unit FROM accounts")}

    def account_stats(self) -> dict:
        row = self.db.execute(
            "SELECT COUNT(*) AS total, SUM(active) AS active, "
            "SUM(CASE WHEN active=1 AND must_change=1 THEN 1 ELSE 0 END) AS initial "
            "FROM accounts").fetchone()
        return {"total": row["total"] or 0, "active": row["active"] or 0,
                "initial_password": row["initial"] or 0}

    def get_default_mode(self, building: int, unit: int) -> str | None:
        row = self.db.execute("SELECT default_mode FROM accounts WHERE building=? AND unit=?",
                              (building, unit)).fetchone()
        return row["default_mode"] if row else None

    def set_default_mode(self, building: int, unit: int, mode: str):
        self.db.execute("UPDATE accounts SET default_mode=? WHERE building=? AND unit=?",
                        (mode, building, unit))

    # --- 관리자 ----------------------------------------------------------
    def get_admin(self, admin_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM admins WHERE admin_id=?", (admin_id,)).fetchone()
        return dict(row) if row else None

    def insert_admin(self, admin_id: str, password_hash: str):
        self.db.execute("INSERT INTO admins (admin_id, password_hash) VALUES (?, ?)",
                        (admin_id, password_hash))

    def update_admin(self, admin_id: str, **values):
        sets = ", ".join(f"{k}=?" for k in values)
        self.db.execute(f"UPDATE admins SET {sets} WHERE admin_id=?", [*values.values(), admin_id])

    # --- 세션 ------------------------------------------------------------
    def insert_session(self, row: dict):
        cols = list(row)
        self.db.execute(f"INSERT INTO sessions ({', '.join(cols)}) VALUES ({_marks(cols)})",
                        [row[c] for c in cols])

    def get_session(self, token_hash: str) -> dict | None:
        row = self.db.execute("SELECT * FROM sessions WHERE token_hash=?", (token_hash,)).fetchone()
        return dict(row) if row else None

    def touch_session(self, token_hash: str, expires_at: str):
        self.db.execute("UPDATE sessions SET expires_at=? WHERE token_hash=?", (expires_at, token_hash))

    def delete_session(self, token_hash: str):
        self.db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))

    def delete_user_sessions(self, kind: str, user_id: str, keep: str | None = None) -> int:
        cur = self.db.execute("DELETE FROM sessions WHERE kind=? AND user_id=? AND token_hash IS NOT ?",
                              (kind, user_id, keep))
        return cur.rowcount

    def delete_sessions_of_kind(self, kind: str):
        self.db.execute("DELETE FROM sessions WHERE kind=?", (kind,))

    def household_sessions(self, building: int, unit: int, now: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM sessions WHERE kind='USER' AND building=? AND unit=? AND expires_at>?",
            (building, unit, now))
        return [dict(r) for r in rows]

    def purge_sessions(self, now: str):
        self.db.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))

    # --- 물품 ------------------------------------------------------------
    def insert_item(self, item: Item):
        cols = ", ".join(ITEM_FIELDS)
        self.db.execute(f"INSERT INTO items ({cols}) VALUES ({_marks(ITEM_FIELDS)})",
                        [getattr(item, f) for f in ITEM_FIELDS])

    def update_item(self, item: Item):
        sets = ", ".join(f"{f}=?" for f in ITEM_FIELDS if f != "receipt_no")
        values = [getattr(item, f) for f in ITEM_FIELDS if f != "receipt_no"]
        self.db.execute(f"UPDATE items SET {sets} WHERE receipt_no=?", values + [item.receipt_no])

    def get_item(self, receipt_no: str) -> Item | None:
        row = self.db.execute("SELECT * FROM items WHERE receipt_no=?", (receipt_no,)).fetchone()
        return Item(**dict(row)) if row else None

    def items(self, statuses: Iterable, *, mode: str | None = None,
              household: tuple[int, int] | None = None, building: int | None = None,
              job_id: str | None = None, unassigned: bool = False, plan: str | None = None,
              slot_start: str | None = None, area: str | None = None) -> list[Item]:
        statuses = _values(statuses)
        sql = f"SELECT * FROM items WHERE status IN ({_marks(statuses)})"
        args: list = list(statuses)
        for column, value in (("mode", mode), ("job_id", job_id), ("plan", plan),
                              ("slot_start", slot_start), ("storage_area", area),
                              ("building", building)):
            if value is not None:
                sql += f" AND {column}=?"
                args.append(str(value))
        if household:
            sql += " AND building=? AND unit=?"
            args.extend(household)
        if unassigned:
            sql += " AND job_id IS NULL"
        sql += " ORDER BY COALESCE(stored_at, registered_at), receipt_no"
        return [Item(**dict(r)) for r in self.db.execute(sql, args)]

    def items_by_pickup_code(self, code: str, statuses: Iterable) -> list[Item]:
        statuses = _values(statuses)
        rows = self.db.execute(
            f"SELECT * FROM items WHERE pickup_code=? AND status IN ({_marks(statuses)})",
            [code, *statuses])
        return [Item(**dict(r)) for r in rows]

    def resident_items(self, household: tuple[int, int], active: Iterable,
                       completed: Iterable, completed_since: str,
                       occupant_since: str | None = None) -> list[Item]:
        """세대 웹 목록: 진행 중 물품 전부 + since 이후 끝난 물품. 숨긴 물품과
        입주자 변경 전에 접수된 완료 물품은 제외."""
        active, completed = _values(active), _values(completed)
        rows = self.db.execute(
            f"SELECT * FROM items WHERE building=? AND unit=? AND hidden_at IS NULL AND ("
            f"status IN ({_marks(active)}) OR "
            f"(status IN ({_marks(completed)}) AND updated_at >= ? AND registered_at >= ?)) "
            f"ORDER BY registered_at DESC",
            [*household, *active, *completed, completed_since, occupant_since or ""])
        return [Item(**dict(r)) for r in rows]

    def count_active_in(self, statuses: Iterable, building: int, units: Iterable[int] | None = None) -> int:
        statuses = _values(statuses)
        sql = f"SELECT unit FROM items WHERE building=? AND status IN ({_marks(statuses)})"
        rows = [r[0] for r in self.db.execute(sql, [building, *statuses])]
        if units is None:
            return len(rows)
        wanted = set(units)
        return sum(1 for u in rows if u in wanted)

    def used_spaces(self, area: str) -> list[str]:
        rows = self.db.execute("SELECT space_id FROM items WHERE storage_area=? AND space_id IS NOT NULL",
                               (area,))
        return [r[0] for r in rows]

    def count_on_robot(self) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM items WHERE space_id IS NOT NULL AND status IN ('LOADED', 'DELIVERED')"
        ).fetchone()[0]

    def receipt_exists(self, receipt_no: str) -> bool:
        return self.db.execute("SELECT 1 FROM items WHERE receipt_no=?",
                               (receipt_no,)).fetchone() is not None

    def count_stored_on(self, input_kiosk: str, date: str) -> int:
        """input_kiosk 로 접수되어 date(YYYY-MM-DD, KST)에 보관 완료된 물품 수."""
        return self.db.execute(
            "SELECT COUNT(*) FROM items WHERE input_kiosk=? AND substr(stored_at, 1, 10)=?",
            (input_kiosk, date)).fetchone()[0]

    def active_pickup_codes(self, active_statuses: Iterable) -> set[str]:
        statuses = _values(active_statuses)
        rows = self.db.execute(
            f"SELECT pickup_code FROM items WHERE status IN ({_marks(statuses)})", statuses)
        return {r[0] for r in rows}

    def status_counts(self) -> dict[str, int]:
        rows = self.db.execute("SELECT status, COUNT(*) FROM items GROUP BY status")
        return {r[0]: r[1] for r in rows}

    def mode_counts(self, statuses: Iterable) -> dict[str, int]:
        statuses = _values(statuses)
        rows = self.db.execute(
            f"SELECT mode, COUNT(*) FROM items WHERE status IN ({_marks(statuses)}) GROUP BY mode",
            statuses)
        return {r[0]: r[1] for r in rows}

    def delete_all_items(self):
        for table in ("items", "slots", "notifications", "jobs"):
            self.db.execute(f"DELETE FROM {table}")

    # --- 배송 시간 예약 칸 -----------------------------------------------
    def seats(self, start_from: str, start_to: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM slots WHERE slot_start>=? AND slot_start<=? ORDER BY slot_start, seat",
            (start_from, start_to))
        return [dict(r) for r in rows]

    def seats_of(self, slot_start: str) -> list[dict]:
        rows = self.db.execute("SELECT * FROM slots WHERE slot_start=? ORDER BY seat", (slot_start,))
        return [dict(r) for r in rows]

    def take_seat(self, slot_start: str, seat: int, item: Item, now: str):
        self.db.execute("INSERT INTO slots (slot_start, seat, receipt_no, building, unit, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (slot_start, seat, item.receipt_no, item.building, item.unit, now))

    def release_seat(self, receipt_no: str) -> str | None:
        row = self.db.execute("SELECT slot_start FROM slots WHERE receipt_no=?", (receipt_no,)).fetchone()
        if not row:
            return None
        self.db.execute("DELETE FROM slots WHERE receipt_no=?", (receipt_no,))
        return row["slot_start"]

    def purge_seats(self, before: str):
        self.db.execute("DELETE FROM slots WHERE slot_start<?", (before,))

    # --- 알림 ------------------------------------------------------------
    def add_notification(self, row: dict) -> int:
        cols = list(row)
        cur = self.db.execute(
            f"INSERT INTO notifications ({', '.join(cols)}) VALUES ({_marks(cols)})",
            [row[c] for c in cols])
        return cur.lastrowid

    def notifications(self, building: int, unit: int, limit: int, since: str | None = None) -> list[dict]:
        sql = "SELECT * FROM notifications WHERE building=? AND unit=?"
        args: list = [building, unit]
        if since:
            sql += " AND created_at>=?"
            args.append(since)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.db.execute(sql, args)]

    def mark_notifications_read(self, building: int, unit: int, now: str, ids: list[int] | None):
        sql = "UPDATE notifications SET read_at=? WHERE building=? AND unit=? AND read_at IS NULL"
        args: list = [now, building, unit]
        if ids:
            sql += f" AND id IN ({_marks(ids)})"
            args.extend(ids)
        self.db.execute(sql, args)

    def unread_count(self, building: int, unit: int, since: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM notifications WHERE building=? AND unit=? AND read_at IS NULL"
        args: list = [building, unit]
        if since:
            sql += " AND created_at>=?"
            args.append(since)
        return self.db.execute(sql, args).fetchone()[0]

    def prune_notifications(self, building: int, unit: int, keep: int):
        self.db.execute(
            "DELETE FROM notifications WHERE building=? AND unit=? AND id NOT IN "
            "(SELECT id FROM notifications WHERE building=? AND unit=? ORDER BY id DESC LIMIT ?)",
            (building, unit, building, unit, keep))

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
            f"SELECT job_id, status, robot_id FROM jobs WHERE status IN "
            f"({_marks(ACTIVE_JOB_STATUSES)}) ORDER BY created_at",
            ACTIVE_JOB_STATUSES)
        return [dict(r) for r in rows]

    def count_jobs_with_prefix(self, prefix: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM jobs WHERE job_id LIKE ?",
                               (prefix + "%",)).fetchone()[0]

    # --- 이벤트 로그 (시뮬레이션 지표 분석·관리자 작업 기록) ---------------
    def log_event(self, at: str, kind: str, data: dict):
        self.db.execute("INSERT INTO events (at, kind, data) VALUES (?, ?, ?)",
                        (at, kind, json.dumps(data, ensure_ascii=False)))

    def events(self, limit: int, prefix: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM events", []
        if prefix:
            sql += " WHERE kind LIKE ?"
            args.append(prefix + "%")
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return [{**dict(r), "data": json.loads(r["data"]) if r["data"] else None}
                for r in self.db.execute(sql, args)]

    def enqueue_message(self, topic, payload, retain=False):
        self.db.execute("INSERT INTO mqtt_outbox (topic, payload, retain) VALUES (?, ?, ?)",
                        (topic, json.dumps(payload, ensure_ascii=False), int(retain)))

    def pending_messages(self):
        return list(self.db.execute("SELECT * FROM mqtt_outbox ORDER BY id"))

    def message_sent(self, message_id):
        self.db.execute("DELETE FROM mqtt_outbox WHERE id=?", (message_id,))


def item_to_dict(item: Item) -> dict:
    return asdict(item)

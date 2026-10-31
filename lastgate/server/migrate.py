"""v3 DB(동·호 고정, 크기 S/M/L) → v4 DB 변환.

Store 가 v3 테이블을 v3_* 로 이름을 바꿔 둔 뒤 같은 트랜잭션 안에서 호출한다.
  · 옛 세대 목록으로 현장 설정을 만든다 (건물 번호 = 동, 이름 'NNN동', 용어 동/호)
  · 계정 해시·세대 기본값을 옮긴다. 비밀번호가 아직 1234 인 계정은 변경 필요로 표시
  · 물품은 크기를 버리고 그리드 보관함에 순서대로 배정한다. 로봇 물품은 자동 배송 확정
"""
from __future__ import annotations

import json

import config
from auth import verify_password
from models import ACTIVE_STATUSES, Area, DirectLock, Mode, Plan, Status
from site_config import Site, _compress

ACTIVE = {s.value for s in ACTIVE_STATUSES}


def migrate_v3(store) -> None:
    db = store.db
    now = config.now_kst().isoformat()
    households = [dict(r) for r in db.execute("SELECT * FROM v3_households ORDER BY dong, ho")]
    by_dong: dict[int, list[int]] = {}
    for h in households:
        by_dong.setdefault(h["dong"], []).append(h["ho"])

    if by_dong:
        buildings = []
        for dong, units in by_dong.items():
            floors = [u // 100 for u in units]
            rooms = [u % 100 for u in units]
            unit_from = min(floors) * 100 + min(rooms)
            unit_to = max(floors) * 100 + max(rooms)
            base = [f * 100 + r for f in range(min(floors), max(floors) + 1)
                    for r in range(min(rooms), max(rooms) + 1)]
            missing = [u for u in base if u not in set(units)]
            buildings.append({"code": dong, "name": f"{dong}동", "unit_from": unit_from,
                              "unit_to": unit_to,
                              "missing": ", ".join(s.replace("호", "") for s in _compress(missing, "호"))})
        site = Site.from_dict({"name": "LastGate", "building_term": "동", "unit_term": "호",
                               "buildings": buildings})
        site.version = store.next_site_version()
        site.updated_at = now
        store.save_site(site.to_dict())

    modes = {(h["dong"], h["ho"]): h["default_mode"] for h in households}
    rows = []
    for acc in db.execute("SELECT * FROM v3_accounts"):
        key = (acc["dong"], acc["ho"])
        rows.append({"user_id": f"{acc['dong']}-{acc['ho']}", "building": acc["dong"],
                     "unit": acc["ho"], "password_hash": acc["password_hash"],
                     "default_mode": modes.get(key, config.DEFAULT_MODE),
                     "must_change": int(verify_password(config.SEED_PASSWORD, acc["password_hash"])),
                     "failed_count": 0, "active": 1, "created_at": now})
    store.insert_accounts(rows)

    columns = {r["name"] for r in db.execute("PRAGMA table_info(v3_items)")}
    next_bin = 1
    for old in db.execute("SELECT * FROM v3_items ORDER BY registered_at"):
        old = dict(old)
        active = old["status"] in ACTIVE
        failed = old.get("fail_count", 0) >= config.MAX_FAIL
        robot = old["mode"] == Mode.ROBOT.value and not failed
        space = None
        if active:
            space = f"G-{next_bin:03d}"
            next_bin += 1
        db.execute(
            "INSERT INTO items (receipt_no, building, unit, mode, status, pickup_code, registered_at, "
            "storage_area, space_id, direct_lock, plan, auto_at, tracking_no, input_client, input_kiosk, "
            "hidden_at, job_id, fail_count, stored_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (old["receipt_no"], old["dong"], old["ho"],
             Mode.ROBOT.value if robot else Mode.DIRECT.value, old["status"], old["pickup_code"],
             old["registered_at"], Area.GRID.value, space,
             DirectLock.FAILED.value if failed else None,
             Plan.AUTO.value if robot and active else None, now if robot and active else None,
             old.get("tracking_no"), old.get("input_client"),
             old.get("input_kiosk") if "input_kiosk" in columns else None,
             old.get("hidden_at") if "hidden_at" in columns else None,
             old.get("job_id"), old.get("fail_count", 0), old.get("stored_at"), old.get("updated_at")))
    # 예상 Job 은 크기 기반 묶음이라 새 규칙으로 다시 짠다. 적재된 Job 은 그대로 둔다.
    pending = [r[0] for r in db.execute("SELECT job_id FROM jobs WHERE status='PENDING'")]
    for job_id in pending:
        db.execute("UPDATE items SET status=?, job_id=NULL WHERE job_id=? AND status=?",
                   (Status.STORED.value, job_id, Status.JOB_ASSIGNED.value))
        cancel = {"type": "JOB_CANCELLED", "job_id": job_id, "reason": "MIGRATED", "timestamp": now}
        db.execute("UPDATE jobs SET status='CANCELLED', done_at=?, bundle=? WHERE job_id=?",
                   (now, json.dumps(cancel, ensure_ascii=False), job_id))
        store.enqueue_message(config.TOPIC_FMS_BUNDLE, cancel)
    for name in ("v3_households", "v3_accounts", "v3_items"):
        db.execute(f"DROP TABLE {name}")
    store.log_event(now, "MIGRATE_V3", {"households": len(households), "buildings": len(by_dong)})

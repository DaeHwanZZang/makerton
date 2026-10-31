"""
보관소 배송 1건을 하나의 배송 작업으로 보여주기 위한 단계 목록
==============================================================

실행은 하위 작업 여러 개(픽업 이동·적재·세대별 하차·반납)로 나뉘어 있다. 묶음이 로봇 도착 후에야
확정되므로(늦은 확정) 처음부터 오더 하나로 만들 수 없기 때문이다. 화면(로봇 팝업·진행 상세 창·작업 게시판·
보관소 화면)은 이 함수가 만든 단계 목록 하나를 같이 쓴다.

단계 상태: done / active / pending / failed / unknown / canceled.
배송 상태: active / waiting(배정 대기) / attention(배정 대기지만 설정 확인 필요) / blocked / canceled / done.
묶음 확정 전에는 예상 목록(proposed_items)으로 그리고, 확정 후 실제 물품·세대로 바꾼다.
"""

from __future__ import annotations

from typing import Optional

PHASES = ("queued", "to_pickup", "awaiting_bundle", "loading", "delivering", "returning", "docking", "done")
_DONE_PARCEL = ("onboard", "unloading", "delivered", "returned")


def _effective_phase(d: dict) -> str:
    """차단(blocked)이면 차단 직전 단계를 쓴다."""
    if d["phase"] == "canceled":
        return "queued"
    if d["phase"] != "blocked":
        return d["phase"]
    for entry in reversed(d.get("timeline") or []):
        if entry["phase"] in PHASES:
            return entry["phase"]
    return "loading" if d.get("bundle_id") else "to_pickup"


def _stage_detail(job, with_order_label: bool = True) -> Optional[str]:
    """하위 작업이 지금 무엇을 기다리는지 (다층 이동이면 엘리베이터 단계)."""
    if job is None:
        return None
    if job.status.value == "assigned":
        return "로봇 수신 대기"
    if job.status.value == "canceling":
        return "취소 확인 중"
    if not job.plan or job.stage >= len(job.plan):
        return None
    stage = job.plan[job.stage]
    kind = stage.get("t")
    if kind == "call":
        return f"엘리베이터 {stage.get('elevator_id')}호기 호출 대기"
    if kind == "ride":
        return f"엘리베이터 {stage.get('elevator_id')}호기 이동 ({stage.get('from_map')} → {stage.get('to_map')})"
    if kind in ("switch", "localize"):
        return "층 전환·위치 확인"
    if kind == "release":
        return "엘리베이터 해제"
    if kind == "order":
        return (stage.get("label") or None) if with_order_label else None
    return None


def delivery_progress(d: dict, jobs, cargo) -> dict:
    phase = _effective_phase(d)
    rank = PHASES.index(phase) if phase in PHASES else 0
    finished = d["phase"] == "done"
    blocked = d["phase"] == "blocked"
    canceled = d["phase"] == "canceled"
    # 배정 대기인데 사유가 있으면(세대 연결 없음 등) 운영자 확인이 필요하다. 설정을 고치거나 배송을 취소한다.
    attention = d["phase"] == "queued" and bool(d.get("error"))
    # 보관소가 적재 전에 취소했다 (JOB_CANCELLED). 적재·하차·반납 단계는 건너뛰고 도킹만 한다.
    aborted = bool(d.get("locker_cancel")) and not d.get("bundle_id") and d["phase"] != "canceled"
    aborted_note = f"보관소 취소: {d.get('locker_cancel')}"
    child = None
    if d.get("child_id"):
        try:
            child = jobs.get(d["child_id"])
        except Exception:
            child = None
    bundle = None
    if d.get("bundle_id"):
        try:
            bundle = cargo.bundle(d["bundle_id"])
        except Exception:
            bundle = None
    trays = (bundle or {}).get("trays", {})
    parcels = {i["parcel_id"]: i for i in (bundle or {}).get("items", [])}

    def status_of_parcel(pid, done_states):
        state = (parcels.get(pid) or {}).get("status")
        if state in done_states:
            return "done"
        if state in ("loading", "unloading"):
            return "active"
        if state == "unknown":
            return "unknown"
        return "pending"

    steps = []

    def add(key, label, status, detail=None, mark=None):
        steps.append({"key": key, "label": label, "status": status, "detail": detail, "mark": mark})

    add("assign", "로봇 배정", "done" if d.get("robot_id") else "active",
        d.get("robot_id") or "가용 로봇 대기")
    add("to_pickup", "픽업 지점 이동",
        "done" if rank > PHASES.index("to_pickup") else "active" if phase == "to_pickup" else "pending",
        d.get("pickup_name"), "to_pickup")
    add("bundle", "최종 묶음 확정",
        "canceled" if aborted else "done" if bundle else "active" if phase == "awaiting_bundle" else "pending",
        aborted_note if aborted else f'{bundle["bundle_id"][:14]}…' if bundle else "물품 2개 확인 대기", "loading")

    if bundle:
        by_tray = sorted(bundle["items"], key=lambda i: i["tray_id"])
        for item in by_tray:
            add(f"pick:{item['tray_id']}", f"트레이 적재 {item['tray_id']}",
                status_of_parcel(item["parcel_id"], _DONE_PARCEL),
                f"{item['parcel_id'][:10]}… → {item['household']}", "loading")
        for i, stop in enumerate(d.get("stops") or []):
            stop_parcels = stop.get("parcels") or []
            moved = d.get("index", 0) > i or any(
                (parcels.get(p) or {}).get("status") in ("unloading", "delivered", "unknown") for p in stop_parcels)
            add(f"move:{i}", f"{stop['household']} 이동",
                "done" if moved or rank > PHASES.index("delivering")
                else "active" if phase == "delivering" and d.get("index", 0) == i else "pending",
                stop.get("target"), f"delivering:{i}")
            for pid in stop_parcels:
                tray = (parcels.get(pid) or {}).get("tray_id", "")
                add(f"drop:{pid}", f"하차 {tray}", status_of_parcel(pid, ("delivered",)),
                    f"{pid[:10]}… · {stop['household']}", f"delivering:{i}")
    else:
        # 확정 전: 예상 목록으로 같은 모양을 그린다. 방문 순서 = 트레이 순서.
        proposed = d.get("proposed_items") or []
        rank = {"TRAY_A": 0, "TRAY_B": 1}
        proposed = [it for _, it in sorted(enumerate(proposed),
                                           key=lambda p: (rank.get(p[1].get("tray"), 2), p[0]))]
        status, detail = ("canceled", "적재 안 함") if aborted else ("pending", "예상")
        for n in range(max(len(proposed), 2)):
            add(f"pick:{n}", f"트레이 적재 {n + 1}", status, detail)
        households = list(dict.fromkeys(f"{it['dong']}-{it['ho']}" for it in proposed))
        for i, household in enumerate(households):
            add(f"move:{i}", f"{household} 이동", status, detail)
            for it in [it for it in proposed if f"{it['dong']}-{it['ho']}" == household]:
                add(f"drop:{it['receipt_no']}", "하차", status, f"{it['receipt_no']} · {detail}")

    returned = [t for t in (d.get("return_trays") or []) if trays.get(t, {}).get("state") == "none"]
    returning_now = any(t.get("state") == "returning" for t in trays.values())
    uncertain = phase == "returning" and any(t.get("state") == "unknown" for t in trays.values())
    returned_all = d["phase"] in ("docking", "done")   # 트레이 반납 완료 (도킹 중이거나 끝남)
    at_return = returned_all or returning_now or bool(returned) or uncertain   # 반납 액션이 시작됐으면 도착한 것
    total_return = len(d.get("return_trays") or []) or len(trays) or 2
    if aborted:
        add("return_move", "반납 지점 이동", "canceled", "반납할 트레이 없음", "returning")
        add("return", "트레이 반납", "canceled", "반납할 트레이 없음", "returning")
    else:
        add("return_move", "반납 지점 이동",
            "done" if at_return else "active" if phase == "returning" else "pending",
            d.get("return_name"), "returning")
        add("return", "트레이 반납",
            "done" if returned_all else "active" if returning_now or returned
            else "unknown" if uncertain else "pending",
            f"{len(returned) if not returned_all else total_return}/{total_return}", "returning")
    # 반납 뒤 충전 스테이션 도킹까지가 한 배송이다. 도킹하지 못해도(빈 충전소 없음·실패) 배송은 끝난다.
    note = d.get("dock_note")
    add("dock", "충전 스테이션 도킹",
        ("canceled" if note else "done") if finished else "active" if d["phase"] == "docking" else "pending",
        note or d.get("charger") or ("자동 선택" if d.get("config", {}).get("charger_target", "auto") == "auto"
                                     else d.get("config", {}).get("charger_target")), "docking")

    # 진행 중 단계는 하나만. 앞에서 처음 끝나지 않은 단계를 현재 단계로 본다.
    current = next((s for s in steps if s["status"] not in ("done", "canceled")), None)
    if current is not None and not finished:
        if canceled:
            current["status"] = "canceled"
            current["detail"] = d.get("error") or current["detail"]
        elif blocked or attention:
            current["status"] = "failed"
            current["detail"] = d.get("error") or current["detail"]
        elif current["status"] == "pending":
            current["status"] = "active"
        moving = current["key"] in ("to_pickup", "return_move", "dock") or current["key"].startswith("move:")
        if current["status"] == "active":
            # 적재·하차·반납 단계에는 오더 이름("목적지로 이동")을 붙이지 않는다. 엘리베이터 단계는 항상 붙인다.
            extra = _stage_detail(child, with_order_label=moving)
            if extra:
                current["detail"] = f"{current['detail']} · {extra}" if current["detail"] else extra
    for step in steps:
        if step is not current and step["status"] == "active":
            step["status"] = "pending"   # 앞 단계가 안 끝났으면 뒤 단계는 대기로 표시

    timeline = d.get("timeline") or []
    starts = {}
    for entry in timeline:
        starts.setdefault(entry["mark"], entry["at"])
        starts.setdefault(entry["phase"], entry["at"])
    for step in steps:
        mark = step.pop("mark")
        # 시작 시각은 시작한 단계에만 붙인다 (대기 단계는 비움).
        step["at"] = None if step["status"] == "pending" else (
            starts.get(mark) or (starts.get(mark.split(":")[0]) if mark else None))

    done = sum(1 for s in steps if s["status"] in ("done", "canceled") or (finished and s["key"] == "dock"))
    return {
        "steps": steps,
        "done": done,
        "total": len(steps),
        "current": "배송 완료" if finished else "배송 취소" if canceled else (current["label"] if current else ""),
        "current_detail": None if finished or current is None else current["detail"],
        "status": ("done" if finished else "canceled" if canceled else "blocked" if blocked
                   else "attention" if attention else "waiting" if d["phase"] == "queued" else "active"),
        "started_at": timeline[0]["at"] if timeline else None,
        "finished_at": timeline[-1]["at"] if finished and timeline else None,
    }

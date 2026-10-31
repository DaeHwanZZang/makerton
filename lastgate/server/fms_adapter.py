"""서버 ↔ FMS(로봇 시뮬레이터) 메시지 변환.

FMS 와 주고받는 토픽·메시지 형식은 이 파일에만 있다. 기존 로봇 시뮬레이터의 실제
규격이 정해지면 send_* 의 페이로드와 parse_event() 만 고치면 된다.

흐름 (늦은 확정)
  서버 → DELIVERY_JOB (예상 묶음, 물품 2개)  FMS 가 로봇 배정·사전 계획
  FMS  → ROBOT_ARRIVED                   로봇이 보관소 출력구에 도착
  서버 → BUNDLE_CONFIRMED (최종 묶음)    FMS 가 경로·엘리베이터 계획 후 출발
  FMS  → DELIVERY_RESULT / JOB_DONE      물품별 결과
  FMS  → JOB_REJECTED                    로봇 배정 전 운영자 취소 (목적지 연결 없는 세대 물품 목록)
  서버 → JOB_CANCELLED                   도착 시점에 실을 물품이 없을 때
"""
from __future__ import annotations

from typing import Callable

import config
from models import Bundle


class FmsAdapter:
    topics = [config.TOPIC_FMS_EVENT]

    def __init__(self, publish: Callable[..., None], now_fn=config.now_kst):
        self._publish = publish
        self._now = now_fn

    # --- 서버 → FMS ------------------------------------------------------
    def send_job(self, job_id: str, bundle: Bundle) -> dict:
        payload = {"type": "DELIVERY_JOB", "job_id": job_id, **self.bundle_body(bundle),
                   "timestamp": self._now().isoformat()}
        self._publish(config.TOPIC_FMS_JOB, payload)
        return payload

    def send_bundle(self, job_id: str, robot_id: str | None, bundle: Bundle) -> dict:
        payload = {"type": "BUNDLE_CONFIRMED", "job_id": job_id, "robot_id": robot_id,
                   **self.bundle_body(bundle), "timestamp": self._now().isoformat()}
        self._publish(config.TOPIC_FMS_BUNDLE, payload)
        return payload

    def send_cancel(self, job_id: str, reason: str) -> dict:
        payload = {"type": "JOB_CANCELLED", "job_id": job_id, "reason": reason,
                   "timestamp": self._now().isoformat()}
        self._publish(config.TOPIC_FMS_BUNDLE, payload)
        return payload

    def resend(self, payload: dict):
        """중복 ROBOT_ARRIVED 등에 같은 메시지를 다시 보낸다."""
        topic = config.TOPIC_FMS_JOB if payload.get("type") == "DELIVERY_JOB" else config.TOPIC_FMS_BUNDLE
        self._publish(topic, payload)

    @staticmethod
    def bundle_body(bundle: Bundle) -> dict:
        """물품마다 트레이 하나. 트레이 순(TRAY_A, TRAY_B)으로 보낸다."""
        order = {tray: i for i, tray in enumerate(config.TRAYS)}
        items = [{"receipt_no": it.receipt_no, "dong": it.dong, "ho": it.ho, "size": it.size,
                  "tray": bundle.trays[it.receipt_no]}
                 for it in sorted(bundle.items, key=lambda i: order[bundle.trays[i.receipt_no]])]
        m = bundle.metrics
        return {
            "items": items,
            "summary": {
                "count": m["count"],
                "trays": {item["tray"]: item["receipt_no"] for item in items},
                "weight_kg": m["weight_kg"],
                "dongs": m["dongs"],
                "households": m["households"],
            },
        }

    # --- FMS → 서버 ------------------------------------------------------
    @staticmethod
    def parse_event(payload: dict) -> dict | None:
        kind = payload.get("type")
        job_id = payload.get("job_id")
        if not job_id:
            return None
        if kind == "ROBOT_ARRIVED":
            return {"kind": kind, "job_id": job_id, "robot_id": payload.get("robot_id")}
        if kind == "DELIVERY_RESULT":
            result = str(payload.get("result", "")).upper()
            if result not in ("DELIVERED", "FAILED") or not payload.get("receipt_no"):
                return None
            return {"kind": kind, "job_id": job_id, "receipt_no": payload["receipt_no"],
                    "result": result, "reason": payload.get("reason")}
        if kind == "JOB_DONE":
            return {"kind": kind, "job_id": job_id}
        if kind == "JOB_REJECTED":
            # 로봇 배정 전 FMS 운영자 취소. unroutable = FMS 에 목적지 연결이 없는 세대의 물품
            unroutable = payload.get("unroutable") or []
            if not isinstance(unroutable, list) or not all(isinstance(r, str) for r in unroutable):
                return None
            return {"kind": kind, "job_id": job_id, "reason": str(payload.get("reason") or ""),
                    "unroutable": unroutable}
        return None

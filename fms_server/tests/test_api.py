"""로봇 REST + WebSocket API 테스트. TestClient 가 lifespan 을 돌린다 (MQTT 는
브로커 없이 뜸 — connect_async 라 블로킹 안 함).

site 저장소는 테스트별 임시 디렉터리로 갈아끼운다.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from common.schemas import ConnectionState, InstantAction, InstantActionType, LocalizationState, RobotState
from fms_server.main import app
from fms_server.sites import SiteRegistry
from fms_server.tests.factories import make_state


@pytest.fixture
def client(tmp_path):
    with TestClient(app) as c:
        registry = SiteRegistry(tmp_path)
        app.state.sites = registry
        app.state.coordinator._registry = registry
        app.state.broadcaster._registry = registry
        yield c


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["robots_known"] == 0
    assert body["mqtt_connected"] is False
    assert body["sites"] == 0


def test_web_ui_assets_are_served(client):
    """페이지의 로컬 자산 링크가 FastAPI에서 실제로 열려야 한다."""
    from html.parser import HTMLParser

    class Assets(HTMLParser):
        def __init__(self):
            super().__init__()
            self.urls = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "script" and "src" in attrs:
                self.urls.append(attrs["src"])
            if tag == "link" and attrs.get("rel") == "stylesheet":
                self.urls.append(attrs["href"])

    page = client.get("/")
    assert page.status_code == 200
    assets = Assets()
    assets.feed(page.text)
    assert len(assets.urls) >= 2
    for url in assets.urls:
        assert url.startswith("/static/")
        response = client.get(url)
        assert response.status_code == 200
        assert "text/html" not in response.headers["content-type"]


def test_robots_empty(client):
    body = client.get("/api/v1/robots").json()
    assert body == {
        "robots": [], "count": 0, "available_count": 0, "unregistered_count": 0,
    }


def test_unregistered_robot_is_observed_not_available(client):
    fleet = app.state.fleet
    fleet.apply_state(make_state("AMR-001", RobotState.IDLE))
    fleet.apply_connection("AMR-001", ConnectionState.ONLINE)
    try:
        body = client.get("/api/v1/robots").json()
        assert body["count"] == 1
        assert body["unregistered_count"] == 1
        assert body["available_count"] == 0  # 미등록이라 job 대상 아님
        robot = body["robots"][0]
        assert robot["robot_id"] == "AMR-001"
        assert robot["liveness"] == "UNREGISTERED"
        assert robot["registered"] is False
        assert robot["state"]["state"] == "IDLE"  # state 는 관측됨
    finally:
        fleet.forget("AMR-001")


def test_robot_detail_404(client):
    assert client.get("/api/v1/robots/NOPE").status_code == 404


def test_websocket_snapshot_then_event(client):
    fleet = app.state.fleet
    fleet.apply_state(make_state("AMR-777", RobotState.IDLE))
    try:
        with client.websocket_connect("/api/v1/robots/stream") as ws:
            snap = ws.receive_json()
            assert snap["type"] == "snapshot"
            assert any(r["robot_id"] == "AMR-777" for r in snap["robots"])

            fleet.apply_connection("AMR-777", ConnectionState.ONLINE)
            evt = ws.receive_json()
            assert evt["type"] == "event"
            assert evt["kind"] == "connection"
            assert evt["robot_id"] == "AMR-777"
            assert evt["robot"]["liveness"] == "UNREGISTERED"
    finally:
        fleet.forget("AMR-777")


def test_init_position_sends_hint_with_current_map(client, monkeypatch):
    fleet = app.state.fleet
    sent = []
    monkeypatch.setattr(app.state.mqtt, "publish_instant", lambda a: sent.append(a))
    fleet.apply_state(make_state("AMR-001", RobotState.IDLE, map_id="uid-3f", map_version="v1",
                                 localization=LocalizationState.UNLOCALIZED))
    try:
        r = client.post("/api/v1/robots/AMR-001/init-position", json={"x": 90.0, "y": 45.0, "theta": 1.57})
        assert r.status_code == 200
        assert len(sent) == 1
        a = sent[0]
        assert a.action_type == InstantActionType.INIT_POSITION
        assert a.params == {"x": 90.0, "y": 45.0, "theta": 1.57, "map_id": "uid-3f"}
    finally:
        fleet.forget("AMR-001")


def test_init_position_unknown_robot_404(client):
    assert client.post("/api/v1/robots/NOPE/init-position", json={"x": 1, "y": 2}).status_code == 404


def test_unlocalized_robot_is_not_available():
    s = make_state("AMR-001", RobotState.IDLE, localization=LocalizationState.UNLOCALIZED)
    assert not s.is_available
    assert s.display_state == "IDLE [UNLOCALIZED]"
    assert make_state("AMR-001", RobotState.IDLE).is_available


def test_init_position_requires_xy():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        InstantAction(header_id=0, timestamp=0, robot_id="A", action_id="i",
                      action_type=InstantActionType.INIT_POSITION, params={"x": 1.0})

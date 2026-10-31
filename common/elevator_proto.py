"""엘리베이터 MQTT 계약 (FMS <-> 엘리베이터 제어기). 시뮬레이터(`tools/elevator_sim.py`)도 같은 계약을 쓴다.

실제 엘리베이터는 FMS 가 시뮬레이션하지 않는다 — 별도 프로세스(제어기)가 상태를 발행하고 명령을 받는다.

    elev/v1/{elevator_id}/state   제어기 -> FMS   (retain, 바뀔 때 + 1 s 마다)
    elev/v1/{elevator_id}/cmd     FMS -> 제어기   (QoS 1)

state 페이로드 (JSON):
    elevator_id   str
    timestamp     float (epoch s)
    stops         [map_id, ...]       층 순서 (아래에서 위로 아니라 등록 순서)
    at            map_id | null       서 있는 층 (움직이는 중이면 null)
    target        map_id | null       지금 향하는 층
    door          CLOSED | OPENING | OPEN | CLOSING
    moving        bool
    held          bool                문 열림 유지 중 (RELEASE 전까지 안 닫힘)

cmd 페이로드 (JSON):
    cmd_id  str
    type    CALL | GO | RELEASE
    map_id  CALL/GO 의 대상 층 (stop 의 map_id)
    hold    CALL/GO: 도착해서 문이 열리면 RELEASE 가 올 때까지 열어 둔다 (로봇 승하차용)

CALL 은 그 층 승강장에서 부르기, GO 는 칸 안에서 층 누르기. 제어기에게는 둘 다 "그 층으로 가서 문을 연다" 이다.
RELEASE 는 hold 를 푼다 (문이 닫히고 다음 요청으로 간다).
"""

from __future__ import annotations

TOPIC_PREFIX = "elev/v1"
TOPIC_STATE_ALL = f"{TOPIC_PREFIX}/+/state"

DOOR_CLOSED = "CLOSED"
DOOR_OPENING = "OPENING"
DOOR_OPEN = "OPEN"
DOOR_CLOSING = "CLOSING"

CMD_CALL = "CALL"
CMD_GO = "GO"
CMD_RELEASE = "RELEASE"


def topic_state(elevator_id: str) -> str:
    return f"{TOPIC_PREFIX}/{elevator_id}/state"


def topic_cmd(elevator_id: str) -> str:
    return f"{TOPIC_PREFIX}/{elevator_id}/cmd"


def topic_sim_teleport(robot_id: str) -> str:
    """시뮬 전용: 로봇 클라이언트의 참 자세를 옮긴다 (프로토콜 밖). 페이로드 {x, y, theta, map_id?}."""
    return f"sim/v1/{robot_id}/teleport"

# 승강기 연결과 층간 이동

[FMS 안내](../fms_server/README.md) · [개발 구조](architecture.md) · [알려진 문제](known-issues.md)

## 층별 구역 연결

1. 각 층의 활성 맵에서 **＋ → 구역**을 열고 `엘리베이터` 유형의 사각형을 그린다.
2. **엘리베이터** 메뉴에서 같은 승강기에 속하는 구역을 층별로 하나씩 선택한다.
3. 승강기 ID·이름·층 표시 이름을 지정하고 연결을 저장한다. 최소 두 층이 필요하다.
4. 문 방향 자동 판정이 맞지 않으면 stop의 `entry_theta`를 지정한다. 단위는 rad이며 칸 안쪽을 보는 방향이다.

이미 다른 승강기에 연결된 구역은 중복 연결할 수 없다.
구역 삭제나 활성 맵 변경으로 참조를 찾지 못하면 연결에 `missing`이 표시된다.
설정은 `sites/{site_id}/elevators.json`에 보관한다.

사각형은 원형 회전 핸들/각도 입력으로 돌릴 수 있다. 자동 문 방향과 탑승 거리는 회전한 실제 변을 기준으로 계산한다.
명시한 `entry_theta`는 구역 각도에 더하는 값이 아니라 기존처럼 world 기준 진입 방향(라디안)이다.

## 다층 작업

프리셋에는 다른 층의 목적지도 넣을 수 있다. FMS는 로봇의 현재 배정 맵에서 목적지 맵까지
승강기 연결을 탐색하고 작업 사이에 이동 stage를 삽입한다.

```text
승강장 앞으로 이동 → 호출 및 문 열림 대기 → 전진 탑승
→ 목표 층으로 이동 및 문 열림 대기 → 후진 하차
→ 승강기 임대 해제 → 배정 맵 전환/SET_MAP → 위치 확인 → 목적지 이동
```

- 맵은 로봇이 하차한 뒤 바꾼다. 하차는 아직 출발 층 좌표계에서 STRAIGHT 후진으로 수행한다.
- 탑승(전진)·하차(후진) STRAIGHT는 `speed` 0.6 m/s로 보낸다 (`fms_server/route.py` `ELEVATOR_STRAIGHT_SPEED`). 도킹·주차 직선 진입은 로봇 기본값 0.2 m/s 그대로다.
- 전진 탑승·후진 하차를 전제로 하므로 층별 문 방향이 다른 연결은 계획 시 거절한다.
- 같은 두 층을 잇는 승강기가 여러 대면 승강장까지의 주행 거리를 비교한다. 비교용 격자는 약 0.25 m이며 로봇 폭 팽창을 적용한 A*와는 다르다. 거리 계산 실패 시 직선거리를 사용한다.
- 다른 작업 계획에 포함된 승강기는 다른 후보가 있으면 피한다. 승강기 한 대의 임대는 작업 하나가 독점한다. 임대 대기 이유는 작업의 `error`에 표시된다.
- 단일 층 작업의 `total_stages`는 0이다. 다층 응답에는 `stage`, `total_stages`, `stage_label`이 포함된다.

구현은 `fms_server/route.py`(계획), `task.py`(stage 실행), `elevator_ctl.py`(제어기 상태·임대)로 나뉜다.

[교통 제어](traffic.md)의 층별 이동 정원이 있으면 승강기 임대 전에 도착 층 자리를 예약한다.
정원이 차면 탑승 전 승강장에서 기다린다. 승강기 칸·직선 탑승/하차 구간은 점유 존과 분리한다.
프리셋 게시 시 연결·문 방향을 검사하고 배정 시 로봇 위치와 다른 작업을 반영해 계획한다.

## 시뮬레이터

FMS는 기본으로 `fms_server/elevator_sim.py`의 내장 가상 승강기를 실행한다.
등록된 승강기 연결을 약 2초마다 확인하고 MQTT로 상태를 발행한다.

실제 제어기나 외부 `tools/elevator_sim.py`를 사용할 때는 내장을 끈다.
아래 명령은 **리포 루트**에서 실행한다.

```bash
FMS_ELEVATOR_SIM=0 .venv/bin/uvicorn fms_server.main:app --host 127.0.0.1 --port 8000
```

외부 시뮬레이터 실행 예시도 **리포 루트** 기준이다. 먼저 해당 현장과 승강기를 등록한다.

```bash
.venv/bin/python tools/elevator_sim.py --site hq
```

내장과 외부 시뮬레이터를 동시에 켜면 같은 승강기 토픽에 상태가 중복 발행된다.
토픽에는 site가 없으므로 승강기 ID는 현장 사이에서도 겹치지 않아야 한다.

층 전환의 로봇 위치 이동은 시뮬레이터 전용 `sim/v1/{robot_id}/teleport`를 사용한다.
도착 층의 하차 자세로 옮긴 뒤 UNLOCALIZED로 만들고 INIT_POSITION으로 위치를 확인한다.
물리적으로 층을 이동하는 실제 로봇용 인터페이스와는 구분한다.

## MQTT 계약

원본은 [common/elevator_proto.py](../common/elevator_proto.py)다.

| 토픽 | 방향 | 내용 |
|---|---|---|
| `elev/v1/{id}/cmd` | FMS → 제어기 | `CALL`, `GO`, `RELEASE` |
| `elev/v1/{id}/state` | 제어기 → FMS | 층·문·이동·열림 유지 상태, retained |
| `sim/v1/{robot_id}/teleport` | FMS → 시뮬 로봇 | `{x,y,theta,map_id?}` |

`CALL`은 승강장 호출, `GO`는 목표 층 이동이다. `hold=true`면 도착 후 RELEASE까지 문을 유지한다.
state의 `at`은 맵 ID이며 층간 이동 중에는 null이다. `target`은 목표 맵 ID다.

## 운행 상태 화면

엘리베이터 페이지 상단 표는 현재 층·목표 층·동작·문·수신 상태를 1초마다 조회한다.
`static/elevator-status.js`는 표만 갱신해 아래 연결 편집 폼과 선택을 유지한다.

- 아직 수신하지 않았으면 `UNKNOWN`과 `—`를 표시한다.
- 이동 중 `at=null`은 `층간 이동`으로 표시한다. 첫 등록 층을 대신 표시하지 않는다.
- 기본 5초 이상 수신이 지연되면 `STALE`과 마지막 수신값을 보여 준다.
- 서버 조회 실패와 제어기 상태 수신 지연을 구분한다.

API의 `runtime`에는 `liveness`, `at`, `current_floor`, `target`, `target_floor`,
`door`, `moving`, `held`, `received_at`, `lease_owner`가 포함된다.
표시용 snapshot은 오래된 값을 보존하지만 제어용 `ElevatorController.state()`는 오래된 상태를 차단한다.

## 제약

실패·취소 후 로봇이 칸 안에 남았을 때 자동 복구하는 절차는 없다.
서버 재시작 시 메모리 임대가 사라지며 진행 작업이 다음 tick에서 다시 확보한다.
승하차 중 사람·장애물과 승강기 안전 인터록은 구현하지 않았다.

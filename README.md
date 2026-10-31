# AMR 시뮬레이터

C++ 로봇 클라이언트, 웹 FMS, Qt 관측 뷰어를 함께 개발하는 프로젝트다.
로봇 한 대를 프로세스 하나로 실행하며 MuJoCo로 이동·슬립·관성·벽 접촉을 계산한다.
FMS는 작업 프리셋과 게시판을 관리하고 가용 로봇에 작업을 배정한다.

## 구성

| 경로 | 역할 |
|---|---|
| `src/`, `include/` | C++20 로봇 클라이언트, Qt 로컬 제어판 |
| `fms_server/` | FastAPI 서버, 웹 UI, 작업 배정, 맵·승강기 관리 |
| `viewer/` | Python/PySide6 관측 뷰어, 로봇을 따라 맵 전환 |
| `common/` | Python 메시지·맵·승강기 계약 원본 |
| `broker/` | 개발용 Mosquitto 설정 |
| `tools/` | 통합 런처, 맵 생성·편집, 명령 발행, 회귀 검증 |
| `maps/` | 로컬 실행과 업로드에 사용할 맵 샘플 |
| `docs/` | 로봇·물리·맵 도구·구현 구조의 상세 설명 |

로봇 오더/상태와 승강기 명령/상태는 MQTT+JSON으로 주고받는다.
웹 UI는 REST/WebSocket을 사용하고 맵 번들은 HTTP로 전달한다.
Qt 뷰어는 MQTT 구독과 HTTP 조회만 하며 제어 명령을 발행하지 않는다.
다른 리포의 코드를 실행 환경에 둘 필요는 없다.

## 주요 기능

- 실시간 맵: 로봇·구역·목적지·충전/주차 지점 표시, 로봇 상세 팝업의 실시간 갱신.
- 맵 위 추가: 좌측 하단 **＋**에서 사각형 구역, 목적지, 충전 도킹 스테이션 등록.
- 작업: 이동·대기·충전 도킹·주차 단계를 프리셋으로 저장하고 직접 배정 또는 게시판 자동 배정.
- 맵: 벤더 맵과 자체 생성 맵 업로드, 버전 관리, 주행금지구역 굽기, 로봇 동기화.
- 다층 이동: 층별 엘리베이터 구역 연결, 승강기 선택·호출·탑승·하차·맵 전환·위치 확인.
- 물리·관측: MuJoCo 차동구동, 위치 추정, 배터리, 동적 장애물, CSV 기록과 Qt 디버그 표시.

승강기는 기본으로 FMS 내장 시뮬레이터를 사용한다. 기능 범위와 제약은
[FMS 안내](fms_server/README.md), [승강기 안내](docs/elevators.md), [알려진 문제](docs/known-issues.md)를 참고한다.

## 설치

이하 명령은 별도 표시가 없으면 **리포 루트**에서 실행한다. Python 3.11 이상이 필요하다.
주 개발 환경은 macOS Apple Silicon이다.

### macOS

```bash
brew install mosquitto cjson qt jq
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
tools/setup_mujoco.sh
make
```

`setup_mujoco.sh`는 MuJoCo 휠에서 헤더·라이브러리를 꺼내 `third_party/mujoco/`에 설치한다.
`make`는 `robot_client`와 `tools/send_order`, `tools/place_obstacle`, `tools/obstacle_zone`을 만든다.
프런트엔드 빌드 도구나 npm 설치는 필요 없다.

### Ubuntu

아래는 **Ubuntu 전용** 명령이며 리포 루트에서 실행한다.
MuJoCo 설치 스크립트의 Linux 실행은 미검증이다.

```bash
sudo apt install clang build-essential mosquitto mosquitto-clients libmosquitto-dev libcjson-dev qt6-base-dev pkg-config jq curl unzip python3-pip python3-venv
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
tools/setup_mujoco.sh
make
```

## 빠른 시작

### 통합 런처

리포 루트에서 실행한다.

```bash
.venv/bin/python tools/launcher.py
```

브로커·FMS·로봇·뷰어·맵 도구를 실행하고 정지할 수 있다.
FMS 사용 시 다음 순서로 준비한다.

1. 브로커와 FMS를 시작하고 `http://localhost:8000/`을 연다.
2. **현장 설정**에서 현장을 등록한다.
3. **맵 관리**에서 맵을 업로드·활성화한다. 첫 실행은 `maps/empty_hall1/fms_map.json`과 같은 폴더의 `edited_navi_gridmap.png`를 사용할 수 있다.
4. 로봇을 `(맵 없음)`으로 실행하고 **로봇** 메뉴에서 현장과 맵에 배정한다. 위 샘플의 시작 자세는 `(3, 3, 0)`을 사용한다.
5. 로봇의 맵 수신이 끝나면 위치 힌트를 주고 `LOCALIZED`를 확인한다.
6. 실시간 맵의 **＋ → 목적지**에서 지점을 등록한다.
7. **작업 프리셋**을 저장한 뒤 **작업 게시판**에서 자동 배정으로 게시하거나 로봇을 지정한다.

### 터미널에서 개별 실행

터미널을 나눠 모두 **리포 루트**에서 실행한다.

```bash
# 터미널 1: 개발 브로커
mosquitto -c broker/mosquitto.conf -v
```

```bash
# 터미널 2: FMS와 웹 UI (단일 프로세스)
.venv/bin/uvicorn fms_server.main:app --host 127.0.0.1 --port 8000
```

```bash
# 터미널 3: FMS가 맵을 전달할 로봇
./robot_client --id AMR-001 --x 3 --y 3 --gui
```

웹 UI에서 현장·맵·로봇 등록 후, **맵 동기화가 끝난 다음** 리포 루트에서 위치 힌트를 보낸다.

```bash
tools/send_order AMR-001 --instant INIT_POSITION --param x=3 --param y=3 --param theta=0
```

`--x/--y/--theta`는 실제로 놓인 자세이며 위치 추정 완료를 의미하지 않는다.
시작 직후와 다른 층으로 맵을 바꾼 직후에는 `UNLOCALIZED` 상태다.
대칭 복도나 힌트 오차로 위치 확인이 실패할 수 있으므로 `State.localization`을 확인한다.

### FMS 없이 로봇만 실행

브로커를 켠 뒤 리포 루트에서 실행한다. 맵의 FREE 영역에 시작해야 한다.

```bash
./robot_client --id AMR-001 --map maps/empty_hall1/fms_map.json --x 3 --y 3 --gui
```

별도 터미널의 리포 루트에서 위치 힌트와 오더를 보낸다.
`LOCALIZED` 확인 후 오더를 발행한다.

```bash
tools/send_order AMR-001 --instant INIT_POSITION --param x=3 --param y=3 --param theta=0
tools/send_order AMR-001 --path 8,3 --order-id demo-1
```

### Qt 뷰어

리포 루트의 공용 가상환경을 사용하며 **`viewer/`에서** 실행한다.

```bash
cd viewer
../.venv/bin/python tools/fleet_monitor_qt.py --follow AMR-001 --fms http://localhost:8000
```

FMS 없이 로컬 맵으로 실행한 로봇은 정적 맵을 지정한다. 실행 디렉터리는 `viewer/`다.

```bash
../.venv/bin/python tools/fleet_monitor_qt.py ../maps/empty_hall1/fms_map.json
```

## 검증

리포 루트에서 실행한다.

```bash
# Python FMS 회귀 검증 (테스트가 MQTT 포트와 데이터 루트를 격리)
.venv/bin/python -m pytest fms_server/tests/ -q

# C++ 물리/주행 회귀 검증 (make 후, 자체 브로커 포트 18851 사용)
.venv/bin/python tools/physics_test.py
```

물리 검증은 전체 실행에 수 분이 걸린다. 대상 시나리오 선택과 수동 확인 범위는
[개발 구조](docs/architecture.md)와 [에이전트 작업 규칙](AGENTS.md)을 참고한다.

## 문서

| 문서 | 내용 |
|---|---|
| [FMS 안내](fms_server/README.md) | 웹 UI, 맵·목적지·작업, REST API, 저장 구조와 설정 |
| [로봇 클라이언트](docs/robot-client.md) | 위치 확인, 액션, 물리·센서·기록, SET_MAP |
| [맵 도구](docs/map-tools.md) | 좌표계, 런처, 생성·편집·노이즈 정리 |
| [승강기](docs/elevators.md) | 층 연결, 다층 실행, MQTT와 내장 시뮬레이터 |
| [Qt 뷰어](viewer/README.md) | 설치, 맵 따라가기, 조작 키와 디버그 표시 |
| [브로커](broker/README.md) | 개발 설정, 실행과 통신 확인 |
| [개발 구조](docs/architecture.md) | 모듈 책임, 스레드·물리·프로토콜 변경 규칙 |
| [알려진 문제](docs/known-issues.md) | 현재 제약과 해결된 문제 기록 |

`logs/`, `.run/`, `map_cache/`, `fms_server/maps/` 등은 실행 중 만들어지는 데이터다.
파일별 역할과 보존 규칙은 FMS 안내와 `.gitignore`를 확인한다.

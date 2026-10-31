# 에이전트 작업 규칙

이 문서는 Codex·Claude Code 등 이 프로젝트를 수정하는 에이전트의 공통 기준이다.
2026-10-31 코드 기준으로 정리했다. 기능의 실제 상태는 코드와 테스트를 우선 확인한다.
사용자 안내를 이 파일에 중복해서 늘리지 말고 해당 문서를 갱신한다.

## 문서 역할

| 문서 | 책임 |
|---|---|
| [README](README.md) | 개요·설치·빠른 시작·문서 탐색 |
| [FMS 안내](fms_server/README.md) | 웹 UI·작업·맵·API·설정·저장 구조 |
| [로봇 안내](docs/robot-client.md) | 로컬라이징·액션·물리·기록·맵 수신 |
| [맵 도구](docs/map-tools.md) | 좌표계·생성·편집·런처 |
| [승강기 안내](docs/elevators.md) | 다층 이동·MQTT·시뮬레이터·운행 표시 |
| [교통 제어](docs/traffic.md) | 맵별 이동 정원·점유 존·대기/출구 node·예약 복원 |
| [개발 구조](docs/architecture.md) | 모듈 책임과 세부 구현 불변조건 |
| [알려진 문제](docs/known-issues.md) | 현재 한계와 해결 기록 |
| `viewer/README.md`, `broker/README.md` | 구성 요소별 실행·조작 |
| `CLAUDE.md` | 이 문서를 읽도록 안내하는 진입점 |

## 작업과 Git

- 사용자와 한국어로 소통한다. 코드 주석과 문서도 한국어로 쓰고 기존 스타일을 따른다.
- 코드를 대조해 사실과 추측을 구분한다. 다른 에이전트가 동시에 작업할 수 있으므로 수정 직전 파일을 다시 확인하고 그 변경을 덮어쓰지 않는다.
- 기능 변경 시 해당 사용자 문서와 이 파일의 영향을 받는 규칙을 함께 갱신한다. 명령어는 실행 디렉터리를 적는다.
- 이 프로젝트는 공개 게시를 전제로 한다. push·태그·공개 리포 변경은 사용자 확인 후 수행한다. Git 메타데이터가 없는 복사 폴더일 수 있으므로 remote·브랜치·태그가 있다고 가정하지 않는다.
- 버전 올리기를 요청받으면 실제 마지막 annotated 태그를 확인해 minor를 올리고 변경 요약을 태그 메시지에 적는다. 과거 문서의 v1.0~v1.4 기록을 현재 태그 조회 결과로 취급하지 않는다.
- C++ 클라이언트를 다른 조직 리포로 수동 반영하는 운영을 고려해 `src/`, `include/`, C++ 도구의 루트 경로를 옮기지 않는다.
- 삭제·force push·DB drop 등 파괴적 작업은 실행 전 확인을 받는다. 이 문서 정리 작업은 Git 초기화나 게시 권한을 뜻하지 않는다.

## 환경과 실행

주 환경은 macOS Apple Silicon과 zsh다. Linux 전용 명령은 구분한다.
설치는 루트 README를 따른다. 아래 명령은 모두 **리포 루트**에서 실행한다.

```bash
make
.venv/bin/python tools/launcher.py
.venv/bin/uvicorn fms_server.main:app --host 127.0.0.1 --port 8000
.venv/bin/python -m pytest fms_server/tests/ -q
.venv/bin/python tools/physics_test.py
```

- 빌드는 C++20·`clang++`·Makefile이며 cmake를 사용하지 않는다. 새 소스는 `CORE_SRC`에 넣는다. `gui.cpp`는 별도 GUI_OBJ다.
- `-MMD -MP` 헤더 의존성을 유지한다. 스키마 헤더 변경 후 stale 오브젝트를 섞으면 구조체 레이아웃이 달라 런타임 오류가 날 수 있다.
- MuJoCo는 `tools/setup_mujoco.sh`가 `third_party/mujoco/`에 설치한다. `physics_world.cpp`만 MuJoCo 헤더를 include한다. macOS install name 수정과 절대 rpath를 고려한다.
- 런타임 맵 수신에 `curl`/`unzip`, launch.sh에 `jq`가 필요하다.
- zsh는 문자열 변수의 인자 자동 분리를 하지 않는다. 인자 목록은 배열을 우선 쓰고 기존 문자열 확장은 `${=VAR}`를 확인한다. macOS sed의 in-place 옵션도 Linux와 다르다.
- 런처 프로세스는 별도 세션이다. 확인은 `pgrep -fl robot_client` 또는 `ps -ax`로 한다.
- Docker 사용이 필요하면 Docker Desktop의 호스트 포트와 localhost를 사용한다. 호스트에 Linux용 systemctl/ufw/apt나 `/var/lib/docker`가 있다고 가정하지 않는다.

## 코드 구조와 계약

- 로봇 한 대는 C++ 프로세스 하나다. FMS는 `fms_server/`, 관측 뷰어는 `viewer/`, 계약 원본은 `common/`에 있다. 외부 AMR_master가 실행 의존성인 것은 아니다.
- Order/InstantAction/State는 `common/schemas.py`가 Python 원본이다. 뷰어에 대응하는 common 파일 사본과 C++ `include/schemas.hpp`·`src/schemas.cpp`를 함께 맞춘다.
- 맵 주석은 `common/map_annotations.py`, 승강기 MQTT는 `common/elevator_proto.py`다. 뷰어 사본 일치는 FMS의 test_common_sync가 검사한다.
- `State`는 extra 필드를 거절한다. 물리 디버그는 별도 `fms/v1/{id}/physics` 토픽을 사용하며 직렬화와 뷰어 표시를 함께 고친다.
- `sim/v1/{id}/teleport`는 시뮬레이터 전용이다. 실제 로봇 제어나 표준 State 계약으로 취급하지 않는다.

## C++ 변경 시 불변조건

- 메인 루프는 50 ms tick과 200 ms State 발행을 사용한다. MQTT 수신→새 맵 교체→GUI 명령→executor.tick→발행 순서를 유지한다.
- Qt는 메인 스레드, 로봇 루프는 GUI 사용 시 백그라운드다. executor 공유 접근은 exec_mutex, GUI 명령은 CommandQueue로 처리한다.
- executor의 map은 비소유 포인터다. main이 수명을 소유하고 교체는 set_map으로 한다. MapSync 다운로드는 tick을 막지 않는 워커에서 수행한다.
- physics 멤버는 그것을 참조하는 kinematics보다 먼저 선언한다. kinematics는 명령 생성기이며 실제 이동은 PhysicsWorld가 계산한다.
- 제어·계획·도착·State.pose는 추정 자세를 사용한다. 참 자세는 충돌·기록·디버그와 가상 측정 생성에만 사용한다.
- 시작과 다른 맵 UID 전환은 UNLOCALIZED다. INIT_POSITION/Pose Hint로 스캔 매칭 후 LOCALIZED여야 이동·작업 수락이 가능하다. 같은 UID의 버전 갱신은 위치 확인을 유지한다.
- Force Localize는 순간이동·오더 폐기·도킹 해제를 동반하는 디버그 조작이다. 일반 위치 힌트와 혼동하지 않는다.
- 충돌 원판 반지름은 width/2−0.04이고 planner는 width/2를 팽창한다. 형상을 바꾸면 계획 여유도 함께 검토한다.
- 직선 이동/도킹/언도킹은 straight 상태가 소유한다. pause/e-stop 재개와 도킹 취소 후 다음 오더의 언도킹 상태를 보존한다.
- DOCK은 chargers.json의 물리 설비와 실제 접점을 확인한다. CHARGE는 접점 없이 시작할 수 없다. min_charge는 다음 작업 수락 조건이다.
- PSO는 경유점 개수와 목표점을 바꾸지 않는다. 동적 장애물 회피에는 적용하지 않고 충돌 샘플이 늘면 A* 결과를 사용한다.

세부 수치·센서·물리 파라미터·경유점 예외는 개발 구조와 로봇 안내를 참고한다.

## FMS 변경 시 불변조건

- 단일 프로세스 운영이다. 파일 저장과 프로세스 내부 락을 다중 worker에 확장한 것으로 설명하지 않는다.
- 교통 제어는 배정과 같은 코디네이터 락을 쓴다. 이동·대기 node·존 예약은 발행 전에 저장하고 취소·TTL·맵 변경만으로 해제하지 않는다. 새 State의 차체 이탈·정지 증거를 확인한다. 삽입 node는 원래 작업 진행률에 포함하지 않는다. 점유 존 맵의 C++ 재탐색도 노드별 미허가 존을 차단하며 PSO를 적용하지 않는다.
- 프리셋 게시 시 명령·목적지 좌표·맵을 작업에 복사한다. 프리셋/목적지 수정이 생성된 작업을 바꾸지 않아야 한다.
- 자동 배정은 auto_assign=true인 pending만 대상이다. FIFO와 로봇 ID 순서로 검사하고 수동/자동 배정은 같은 코디네이터 락을 사용한다.
- assigned/working/canceling의 로봇 예약을 유지한다. 수신 불명확 작업을 자동 재게시하거나 다른 로봇에 넘기지 않는다. 액션 FINISHED 전에는 해당 단계를 완료로 세지 않는다.
- 로봇이 현재 배정 맵 UID/활성 버전을 보고하고 수락 가능한 상태여야 배정한다. 목적지가 다른 층이면 승강기 계획을 수행한다. 무조건 같은 층으로 제한하지 않는다.
- 승강기 계획은 route.py, 실행은 task.py, 제어 상태·임대는 elevator_ctl.py다. 내장 가상 승강기는 기본 켜짐이고 외부 제어기 사용 시 FMS_ELEVATOR_SIM=0으로 끈다.
- 층 전환은 하차 후 맵 전환→시뮬 teleport→위치 확인 순서다. 방향·임대·실패 제약은 승강기 안내를 따른다.
- 데이터 미수신과 실제 0 값을 구분한다. 운행 표시용 snapshot의 stale 값 보존과 제어용 stale 차단을 혼동하지 않는다.
- LastGate 물품·계정 장부는 `lastgate/server/`의 SQLite, 배송 운행은 `fms_server/locker.py`가 소유한다. 단계 사이와 오류 차단 중에도 배송 로봇 예약을 유지하고 수동·자동 배정은 같은 코디네이터 락에서 검사한다.
- 배송 묶음은 물품 2개·트레이 2개(TRAY_A/B, 트레이당 1개)다. FMS 물품 장부(`cargo.py`)에 ID·트레이를 저장한 뒤 PICK을 발행하고, 상태는 PICK/DROP·칸별 트레이 반납 FINISHED로만 확정한다. 트레이는 탈착식이며 칸 상태가 모두 none이어야 새 묶음을 받는다. 증거 없으면 unknown, 자동 재실행·자동 해제 금지.
- 배송은 보관소 도착 후 최종 묶음만 실행한다. PICK/DROP FINISHED를 확인하고, 실패 물품 재입고·JOB_DONE은 보관소 복귀 뒤 보고한다. 불확실 인도는 운영자가 적재물을 대조한다. 실행·설정은 `lastgate/README.md`를 따른다.

## 맵과 편집

- 원점은 이미지 왼쪽 위의 world 좌표이며 y는 아래로 증가한다. `px=floor((x-origin_x)/resolution)`이고 y도 같은 식이다. y를 뒤집지 않는다.
- FREE만 주행 가능하며 UNKNOWN/맵 밖은 막힌 것으로 취급한다. planner 팽창을 바꾸면 tools/clean_map.py의 inflate_free와 생성 검사도 맞춘다.
- grid_cfg.grid가 있으면 벤더 좌표계를 우선한다. 없으면 fms_map.json과 참조 PNG를 사용한다. location/zone 메타는 선택이다.
- 원본 edited_navi_gridmap.png는 source에 보존한다. 로봇용 이미지는 navi_gridmap.baked.png이며 구역 편집은 원본에서 새 버전으로 다시 굽는다.
- 사각형 zone 회전은 중심 기준이며 실제 polygon으로 저장한다. Python/C++의 점유 판정·경로 마스크·승강기 기하는 회전 경계를 사용한다. 연결된 픽업·반납·대기·출구 node는 함께 돌리지 않는다.
- chargers.json은 선택 번들 파일이고 버전 해시에 포함한다. 웹 도킹 등록은 목적지 저장 성공 후 준비한 새 맵을 활성화한다.
- 픽업스테이션은 pickup zone(맵 버전)과 `type=pickup`·`type=tray_return` location(`zone_id`)의 1:1:1 묶음이다. 반납 지점이 없으면 broken이다. 생성·삭제·위치 변경은 픽업스테이션 API로만 하고 일반 zone/location API 우회를 막는다. pickup zone을 굽거나 미지 type으로 떨어뜨리지 않도록 common·viewer 사본을 함께 바꾼다.
- 일반 목적지는 맵 버전을 바꾸지 않는다. 이름 변경 시 프리셋/스냅샷 없는 pending 참조를 갱신하고 기존 작업 스냅샷과 물리 충전기 ID는 보존한다.
- LocationStore의 공유 락과 파일 교체, base_version 충돌 검사를 유지한다. 편집 초안을 주기 갱신으로 덮어쓰지 않는다.
- `maps/gui_map`의 수동 원점·edits는 재생성하지 않는다. 생성 맵의 (0,0)은 UNKNOWN 여백일 수 있다.

## 검증과 완료 조건

- Python 서버·저장소·API 변경: FMS pytest를 실행한다. 테스트는 기본 포트 18859와 임시 데이터 루트로 격리하지만 외부 환경변수로 바꿨다면 먼저 확인한다.
- 이동·도착·충돌·도킹 변경: make 후 physics_test의 해당 시나리오와 필요한 전체 회귀를 실행한다. 자체 브로커는 18851을 사용한다.
- 수동 주행 확인: 브로커→FREE 위치의 로봇→맵 수신→INIT_POSITION→LOCALIZED→오더→State 순서다.
- 맵 도구 변경: 출력 PNG/JSON을 직접 확인한다. Qt는 QT_QPA_PLATFORM=offscreen과 grab 또는 AMR_GUI_SNAPSHOT으로 배치를 확인할 수 있다.
- 웹 변경: JS 문법과 관련 API/데이터 흐름을 확인한다. 실제 브라우저를 확인하지 못했다면 완료 보고에서 구분한다.
- 문서 변경: 상대 링크, 명령 실행 디렉터리, API/설정/기능 상태와 코드의 일치를 검사한다. 문서 정리만을 이유로 코드나 사용자 실행 데이터를 수정하지 않는다.

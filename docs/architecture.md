# 개발 구조와 구현 규칙

[프로젝트 시작 안내](../README.md) · [에이전트 작업 규칙](../AGENTS.md)

구현의 세부 규칙을 모은 문서다. 설치·실행은 README, 서버 사용/API는 FMS README에서 관리한다.
기존 설계의 검증 수치와 성능 값은 개발 당시의 관측 참고값이며 환경에 따라 달라진다.

## 구성 요소와 통신

| 구성 요소 | 책임 | 통신 |
|---|---|---|
| C++ 로봇 클라이언트 | 로봇별 오더 실행·경로계획·물리·위치 추정 | MQTT 상태/오더, HTTP 맵 다운로드 |
| FMS | 현장·맵·목적지·작업·승강기 관리, 자동 배정 | REST/WebSocket, 로봇·승강기 MQTT |
| Qt 뷰어 | 관측과 표시, 로봇을 따라 맵 전환 | MQTT 구독과 FMS HTTP GET |
| Mosquitto | 메시지 중계 | 개발 기본 localhost:1883 |

`common/schemas.py`는 로봇 메시지, `common/map_model.py`는 맵,
`common/map_annotations.py`는 주석, `common/elevator_proto.py`는 승강기 MQTT 계약의 원본이다.
FMS는 루트 common을 직접 사용한다. 뷰어의 common은 사본이며, C++ 계약은 수동 포팅한다.

## 아키텍처 (C++ 클라이언트)

- **진입점 `src/main.cpp`**:
  - 순서: 인자 파싱 → `MapModel` 로드(옵션) → 시작 위치 검사 → `OrderExecutor` + `RobotComm` + `MapSync` 조립 → `robot_loop`.
  - 루프: `Ticker` 로 50 ms tick (`TICK_PERIOD`), 200 ms 마다 `State` 발행 (`STATE_PERIOD`).
  - 한 tick 안의 순서: MQTT 수신(`comm.drain()`) → `map_sync.take_ready()` 로 새 맵 교체 → GUI 명령 처리 → `executor.tick(dt)` → 상태 발행.
- **스레드 모델**: `--gui` 면 `robot_loop` 를 백그라운드 스레드로, Qt 이벤트루프를 메인 스레드로 돌린다. 공유 상태는 `exec_mutex` 로 보호한다. GUI→루프 명령은 `CommandQueue` (`include/command_queue.hpp`: Move/Relocalize/Start/Stop/Estop/Mode/Configure/ResyncMap/SetBattery) 로 보낸다. 맵 다운로드는 별도 워커 스레드(`MapSync`)라 tick 을 막지 않는다. `executor` 를 건드리는 코드는 항상 이 락 규칙을 따라야 한다.
- **`OrderExecutor` (`executor.cpp`, 가장 큼)**:
  - 처리 범위: 오더 상태머신 (released/order_update_id/거절 규칙), e-stop/PAUSE/MANUAL, 세그먼트 단위 경로계획, 액션(PICK/DROP/CHARGE/WAIT/STRAIGHT/DOCK/PARK), 충돌 감지, 동적 장애물 재계획.
  - 소유 객체: `PhysicsWorld`(MuJoCo, `physics`), `TrapezoidalKinematics`(사다리꼴 궤적 = 명령 생성기, 위의 world 를 가리킨다), `BatterySim`, `DynamicObstacleField`, `Recorder`(`--record` 일 때만). 멤버 선언 순서가 중요하다: `physics` 가 `kinematics` 보다 먼저여야 한다.
  - `tick(dt)` 는 `dt` 를 0.1 초로 자른 뒤 `tick_logic` 을 돌고 `Recorder` 에 한 줄 쓴다. 움직이지 않는 분기(idle/액션/대기)는 `kinematics.brake(dt)` 를 불러 굴러가던 로봇을 감속시킨다.
  - `map` 포인터는 소유하지 않는다. `main` 의 `map_model`/`synced_map` 이 소유한다. 맵 교체는 `set_map()` 으로만 한다.
- **로컬라이징 상태** (`State.localization`: UNLOCALIZED | LOCALIZING | LOCALIZED, `localization_score`):
  - UNLOCALIZED 가 되는 때: 켜자마자, 다른 맵(uid)으로 `set_map()` 했을 때(같은 uid 의 버전 교체는 유지), 로컬라이징 실패.
  - LOCALIZED 가 아니면 `motion_inhibited()` 이고 `can_accept_job()` 이 거절한다 (Python `State.is_available` 도 같은 조건). 즉시명령은 받는다.
  - 위치 힌트: MQTT `INIT_POSITION`(params x, y, theta, map_id 선택 — 지금 맵 uid 와 다르면 무시), 제어판 Pose Hint(`CmdLocalizeHint`), FMS `POST /api/v1/robots/{id}/init-position`. 추정을 힌트로 두고 LOCALIZING 1 초(`localize_time`) 뒤 스캔 매칭 결과로 LOCALIZED, 또는 WARNING `LOCALIZATION_FAILED` + UNLOCALIZED. 맵이 없으면 힌트를 거절한다.
  - 스캔 매칭 (`scan_matcher.hpp/cpp`, 엔진/executor 를 모름): 참 자세에서 가상 라이다(360 빔, 20 m, σ 2 cm)를 맵에 레이캐스트(DDA, 비-FREE 에서 반사)하고, 힌트 ±1.5 m/±30° 안을 likelihood field(벽 경계까지 EDT 거리, 0.05 m 격자)로 거친 탐색(0.1 m, 2°) → 상위 3개 고운 탐색(0.01 m, 0.25°). 점수 0.8 미만 / 끝점 20 개 미만이면 실패. gachon 에서 맞는 자세 점수 0.93~0.97, 1회 약 17 ms, 필드 생성 약 0.1 s(맵당 lazy, `set_map` 에서 버림). 힌트가 창 밖이면 실패하고, 대칭 복도에서는 틀린 자세로 잡힐 수 있다 (의도한 현실 동작).
  - 추적 보정(`Localizer`)은 LOCALIZED 일 때만 켠다 (`set_tracking`). 측정-추정 차이가 0.5 m/0.5 rad 를 넘으면 측정을 버린다(`max_residual`) — 틀린 위치로 잡혔을 때 참 자세로 몰래 고쳐지지 않게.
  - 강제 로컬라이징 = `force_relocalize` (제어판 Force Localize, `CmdRelocalize`): 로봇(참 자세)을 그 자세로 옮기고 바로 LOCALIZED(점수 1). 오더를 버리고 도킹을 푼다. 디버그/시뮬 전용이라 MQTT 에는 없다.
- **시뮬 전용 teleport** (`sim/v1/{id}/teleport` `{x,y,theta,map_id?}`): `comm.cpp` 가 받아 `SimTeleport` 로 큐에 넣고 `executor.sim_teleport()` 가 참 자세만 옮기며 UNLOCALIZED 로 만든다 (오더/도킹 버림, 다른 map_id 면 무시). 엘리베이터가 로봇을 옮기는 시뮬이다. 프로토콜 밖이라 `State`/`schemas.py` 와 무관하다.
- **위치 오류 규칙** (v1.3):
  - 시작 위치가 비-FREE 여도 프로세스는 경고 로그만 남기고 뜬다 (`main.cpp` 시작 검사). 벽 속에서는 라이다가 아무것도 못 봐서 힌트로는 로컬라이징이 안 된다.
  - 오더를 받은 뒤 경로가 없으면 `replan()` 이 `PlanningError` 를 잡아 FATAL `NO_PATH` 를 올리고 ERROR 상태가 된다 (설명에 로봇 좌표 포함).
  - `force_relocalize` 로 FREE 칸에 옮기면(또는 맵이 없으면) `NO_PATH`/`COLLISION` 오류를 지운다. 비-FREE 칸이면 경고만 남긴다.
  - `COLLISION` 은 이동 중에만 검사한다 (물리: 몸체-벽 접촉, 물리 설명).
- **물리 (MuJoCo, v1.4 이후)**:
  - 구조: `TrapezoidalKinematics` 는 이상 궤적을 내는 명령 생성기이고, 위치는 `PhysicsWorld` 가 정한다. `step()` 은 `sync_from_world()` 로 측정 자세를 읽고 → 이상 궤적을 한 걸음 내고 → 그 변위를 (v, w) 로 바꿔 `world.drive()` → `world.step(dt)` → 다시 측정한다. `kinematics.x/y/theta` 는 항상 마지막 **추정** 자세다 (오도메트리 + 위치추정 보정, 참 자세 아님). `executor` 는 이 값만 보면 된다.
  - 모드 스위치는 없다. `world_ == nullptr` 이면 순수 운동학(이전 동작)으로 도는 분기가 `kinematics.cpp` 에 남아 있지만 executor 는 항상 world 를 붙인다.
  - `physics_world.cpp` 만 `mujoco.h` 를 include 한다 (pimpl). 월드: 바닥 평면 + 차동구동 몸체(바퀴 구 2, 무마찰 캐스터 2, 충돌 원판) + 벽 상자. 맵 → 벽은 비-FREE 칸을 직사각형으로 병합하고(`merge_blocked_cells`), 로봇 주변 8 m 창만 MJCF 에 넣는다. 3 m 벗어나면 `rebuild()` 로 재컴파일하고 qpos/qvel/ctrl 을 옮긴다. 월드는 `set_map()` 때마다 맵 그리드에서 다시 만든다.
  - **충돌 형상은 원판(반지름 `width/2 - 0.04`)이다.** planner 가 반지름 `width/2` 원으로 팽창시키기 때문에 맞춘 것이고, 직사각형으로 하면 좁은 통로 모퉁이에서 회전 중 COLLISION 이 난다 (`gen_loop` 에서 확인). `length` 는 표시와 관성에만 쓴다. 이 결정을 바꾸면 planner 팽창도 같이 봐야 한다.
  - 충돌: 몸체-벽 접촉력 20 N 이상이 0.25 초 지속되면 `kinematics.collided()` 가 true 가 되고 `check_collision` 이 FATAL `COLLISION` 을 올린다. 기존 "중심점이 비-FREE" 검사도 남아 있다. 시작/강제 위치가 벽 안이면 `ghost` 로 몸체-벽 충돌을 끈다 (v1.3 의 "벽 위에서도 뜬다" 보존, 엔진이 튕겨내지 못하게).
  - 도착: 경로는 실제로 멈추고(`at_rest`) 마지막 지점 3 cm 이내여야 `is_path_complete()`. 이상 궤적이 끝났는데 못 닿으면 `goal_` 을 다시 건다. 직선 이동은 이상 궤적 목표를 매 tick 지금 heading 진행선 위로 투영하고(옆 방향 오차는 직선으로 못 줄인다), 목표 방위로 조향하며, 진행 방향 남은 거리 1 cm 이내에서 서면 끝난다. 끝난 뒤 정착 대기(`settle_`)도 `straight_active()` 에 포함되어 executor 의 `finish_straight` 가 실제로 선 뒤에 불린다.
  - 이상 궤적 속도는 실제 속도 ±0.3 m/s (각속도 ±0.5 rad/s)로 묶는다. 방위 허용치는 서 있다 출발할 때 0.05 rad, 달리는 중 0.3 rad 이고(이상 궤적은 0.02), 큰 꺾임(0.3 rad 초과) 경유점에서 서도록 `stop_distance_ahead()` 로 감속한다. 이걸 빼면 슬립/지연 때문에 서고-돌고를 반복하거나 모퉁이에서 순간 정지(수 m/s² 감속)한다.
  - 바퀴 서보 게인 `kServoGain = 6`: 60 이면 접촉 compliance 와 얽혀 정지 후에도 떨린다. 일단 6 이어도 슬립이 쓸 만하게 나온다.
  - `emergency_stop()`/`set_pose()` 는 world 를 즉시 정지/순간이동시킨다. `hold()` 와 `brake()` 는 `max_deceleration` 으로 감속시킨다. world 는 `kinematics.step/hold/brake` 안에서만 진행하므로 호출이 없으면 시간이 멈춘다.
  - 뷰어 디버그 토픽: `fms/v1/{id}/physics` (`PhysicsDebug`, `schemas.hpp`). `State` 는 뷰어 스키마가 `extra=forbid` 라 필드를 못 더해서 프로토콜 밖 토픽으로 뺐다. `main` 루프가 tick 마다 `executor.build_physics()` 를 발행한다. 뷰어는 pydantic 없이 dict 로 읽고(`fleet_monitor.py`), `qt_viewer.py` 가 외삽/궤적/GT/패널(`p`,`l`,`o`,`k`)로 그린다. 뷰어 몸체 = 보고 위치(`est_*` = State.pose), 청록 GT = 참 위치(`x y theta`), 노란 점선 = 보정 전 오도메트리(`odom_*`). 필드를 바꾸면 `schemas.cpp` `PhysicsDebug::to_json_string` 과 `qt_viewer.py` `_physics_text`/`display_pose`/`truth_pose` 를 같이 고친다.
  - GUI 제어판(`gui.cpp`)에는 같은 값을 보이는 Physics 절과 마찰/질량 설정칸이 있다 (`CmdConfigure.friction/mass` -> `apply_robot_geometry`). `AMR_GUI_SNAPSHOT=파일.png` 이면 1.5 초 뒤 창을 PNG 로 저장한다 (offscreen 레이아웃 확인용).
  - **추정 자세 기반 제어 + 위치추정 보정 (기본 켜짐)**: `sync_from_world()` 가 `world.est_*` 를 읽는다. 추정 = `Localizer`(`localizer.hpp/cpp`)가 오도메트리(`odom_*`, 바퀴 엔코더 적분)에 얹는 보정 오프셋 `T` (`추정 = T ∘ 오도메트리`). `Localizer::update()` 는 `PhysicsWorld::step()` 끝에서 주기(`--loc-rate`, 기본 5 Hz)마다 `z = 참 자세 + 노이즈` 로 추정을 `gain` 만큼 당기고 `T` 를 다시 푼다 (한 번에 0.15 m/0.1 rad 상한, dropout 확률로 측정 누락). `--loc-rate 0` 이면 보정 없음(추정 = 순수 오도메트리). `Localizer` 는 엔진을 모른다 (참 자세는 호출자가 넘긴다) — 나중에 EKF 등으로 바꿀 때 이 클래스만 갈아낀다. 제어/경로계획/도착/`State.pose` 가 전부 추정으로 돈다. 참 자세(`world.x()` 등)는 충돌 판정(`check_collision`), 기록, `physics` 토픽(뷰어)에만 쓴다. `teleport`/`set_pose` 는 오도메트리를 그 자세로 맞추고 `T` 를 지운다. 경로선은 뷰어에서 추정 좌표로 그린다.
  - 추정에 노이즈가 있어서 `kinematics.cpp` `step_ideal` 이 5 cm 안의 경유점(마지막 제외)을 건너뛰고 2 cm 안이면 방위를 유지한다 (아니면 구간 경계의 겹친 경유점 방위가 노이즈로 흔들려 제자리 헌팅한다). 도착 허용(3 cm)보다 `kAimMinDist` 를 작게 해야 마지막 보정 접근(옆 3 cm 를 돌아서 가기)이 된다.
  - 기록: `--record DIR` → `recorder.cpp` CSV(+meta.json). 열 정의는 [로봇 안내](robot-client.md)를 참고한다. 오도메트리/추정/참 자세의 상세는 State 추가 필드가 아니라 기록과 physics 디버그 토픽으로 제공한다.
  - 물리가 아닌 것: 동적 장애물(사각형 좌표), 로봇끼리 상호작용, IMU. 라이다는 초기 로컬라이징(스캔 매칭)에만 쓰고, 주행 중 추적 보정은 여전히 `참 자세 + 노이즈` 측정이다. 가상 라이다는 맵만 본다 (동적 장애물/다른 로봇은 안 보인다).
  - 디버깅: `AMR_PHYSICS_DUMP=파일` 로 마지막 MJCF 를 덤프해 파이썬 `mujoco` 로 재현할 수 있다. 서보/접촉 파라미터는 그렇게 스윕해서 정했다.
- **직선 이동 / 도킹 / 주차** (v1.4):
  - `straight_`(StraightMotion: ACTION/DOCK_IN/UNDOCK)가 있으면 `tick` 은 `tick_straight` 만 돈다. 경로계획 없이 `kinematics.set_straight()` 로 heading 을 유지한 채 목표점까지 간다. PAUSE/e-stop 은 kinematics 쪽 목표를 지우므로 executor 가 목표를 들고 있다가 재개 때 다시 건다. `replan()` 은 `straight_` 가 있으면 아무것도 안 한다.
  - STRAIGHT: 노드 도착·정렬 후 노드 theta 방향 `distance` 만큼.
  - DOCK/PARK: `plan_segment_path` 가 해당 노드 목표를 `approach_point()`(진입 완료 자세에서 `undock_distance` 만큼 앞/뒤)로 바꾼다. 도착하면 직선으로 진입하고, 진입을 시작하는 순간 `docked_`(exit 지점 = 진입 시작 자세)를 만든다.
  - 언도킹은 액션이 아니다. `replan()` 이 세그먼트를 만들 때 `docked_` 가 있으면 먼저 `start_undock()` 으로 exit 지점까지 직선 이동한다. `docked_` 는 언도킹 완료 때만 지운다 (취소돼도 남음). 강제 위치지정은 지운다.
  - DOCK 은 `min_charge` 미만이면 `idle_state()` 가 CHARGING 을 돌려줘서 `can_accept_job()` 이 거절한다. `clear_order()` 는 도킹 중이면 `battery.charging` 을 유지한다.
  - **충전기는 맵의 물리 설비다** (`chargers.hpp/cpp`, 맵 폴더 `chargers.json`: id, x, y, theta = 도킹 완료 자세, entry 선택, tol_xy 0.06 m / tol_theta 0.1 rad). executor 가 생성자/`set_map()` 에서 `map->base_dir` 기준으로 읽는다. DOCK 은 노드 0.3 m/0.35 rad 안에 충전기가 없으면 FATAL `DOCK_FAILED` (진입 안 함). 진입이 끝나면 **참 자세**가 충전기 허용 오차 안이고 entry 가 맞아야 충전하고, 아니면 FATAL `DOCK_FAILED` (docked_ 는 남아 다음 이동 때 먼저 빠져나온다). `CHARGE` 액션은 충전기 접점에 붙어 있을 때만 된다 (아니면 FATAL `CHARGE_FAILED`). FMS 번들(`mapkit/store.py` `BUNDLE_FILES`)은 `chargers.json`을 선택 파일로 싣고 버전 해시에 포함한다. 웹 도킹 스테이션 등록은 named location과 충전기 설비를 함께 만든다.
  - **배터리 임계값** (`--battery-low` 20, `--battery-critical` 5, `check_battery()` 가 tick 마다): low 아래 WARNING `LOW_BATTERY`(+2% 히스테리시스로 해제). critical 아래로 **내려가며 통과하는 순간** FATAL `BATTERY_CRITICAL` (오더 버림, RESET_ERROR 뒤 재발생 없음). critical 아래에서는 `accept_order` 가 DOCK 노드가 있는 오더만 받는다. 0% 면 FATAL `BATTERY_EMPTY` + `motion_inhibited` (충전 중 제외, RESET_ERROR 해도 다시 걸린다 — 제어판 배터리 버튼으로만 살린다).
  - 마커 보정은 일부러 넣지 않았다 (사용자 결정). v1.4 에는 도착 오차도 없었지만 물리 도입 뒤에는 물리가 만든다 (보통 수 mm, 물리 설명).
- **경로계획**: `planner.cpp` A* (로봇 크기 반영, 동적 장애물 사각형 입력). 장애물은 센서 범위(`--sensor-range`) 안에서만 인식한다. [알려진 문제](known-issues.md) 참고.
- **PSO 경로 다듬기** (`path_refine.cpp`, `--path-refine`, 기본 꺼짐):
  - 위치: `plan_segment_path()` 에서 구간별 A* 결과의 중간 경유점을 PSO 로 미세 조정한다.
  - 경유점 **개수와 목표점은 바꾸지 않는다** (`segment_wp_bounds_` 진행도 추적 때문).
  - 동적 장애물 회피 경로에는 적용하지 않는다 (거리 장이 정적 맵 기준이라서).
  - 충돌 샘플이 A* 보다 늘면 원본을 쓴다.
  - 거리 장(`ClearanceField`)은 executor 가 lazy 생성하고 `set_map()` 에서 버린다. seed 는 `--seed` 와 로봇 id 에서 파생한다.
- **프로토콜**: `schemas.hpp/cpp` 가 메시지와 JSON 직렬화(cJSON)를 정의한다.
  - 메시지: Order / InstantAction / State.
  - 토픽: `fms/v1/{id}/{order,instant,state,connection}`.
  - `Action.params`(v1.4 추가, float|str|bool map): STRAIGHT/DOCK/PARK 필수 인자. 검증은 `schemas.cpp` `check_action_params()` 와 `common/schemas.py` `Action._check_params` 가 같은 규칙.
  - InstantAction 종류: `CANCEL_ORDER, PAUSE, RESUME, RESET_ERROR, SET_MAP, INJECT_FAULT, INJECT_OBSTACLE, CLEAR_OBSTACLE, START_OBSTACLE_ZONE, STOP_OBSTACLE_ZONE, INIT_POSITION`. `send_order --instant` 은 `--param KEY=VALUE` 를 params 로 싣는다.
  - `State.localization`/`localization_score` 는 2026-10-31 추가 (Python 기본값 UNLOCALIZED/0).
  - `comm.cpp` 는 libmosquitto 래퍼다 (LWT, 재접속, `connection` retained 토픽).
  - **스키마 원본은 루트 `common/schemas.py` 다.** 바꾸면 `viewer/common/` 에 복사하고 C++ `schemas.hpp/cpp` 를 손으로 맞춘다.
- **맵 로드/동기화**: `map_model.cpp` 가 `fms_map.json` 과 그 `image` png(`stb_image`)를 읽는다. `SET_MAP` 을 받으면 `map_sync.cpp` 가 zip 을 받아 `map_cache/<map_uid>/` 에 풀고 `State.map_id/map_version` 으로 보고한다. FMS 는 이 값이 일치하는 로봇에만 job 을 배정한다.
- **C++ `tools/`**: `send_order`, `place_obstacle`, `obstacle_zone` 은 별도 실행 파일이고 `schemas.o` 만 링크한다. `mqtt_once.hpp` 는 1회 발행 헬퍼다.

## 아키텍처 (Python 도구, `tools/`)

같은 폴더끼리 import 한다 (`gen_map` → `clean_map`, `synth_map` → `gen_map`, `map_editor` → `map_edit_model`/`clean_map`, `launcher` → `launcher_core`).

- **`gen_map.py`**:
  - 입력: 레이아웃 텍스트 (`tools/map_layouts/`, `.`=통로 / `#`=벽, 1 글자 = 1 m).
  - 생성: marching squares 로 타일링하고 각 타일에 `grid.png` 실제 조각을 붙인다. 벽은 항상 타일 가운데 선이다.
  - 검사: `planner.cpp` 의 inflate 규칙을 Python 으로 재현했다 (`clean_map.inflate_free`). **planner 규칙이 바뀌면 같이 고친다.** 통로 사분면에 노이즈 픽셀을 허용하지 않는 이유도 이 팽창 때문이다.
  - 출력: `grid.png` + `edited_navi_gridmap.png` + `gen_info.json` + `layout.txt`. `edited_navi_gridmap.png` 는 최초 생성 때만 복사하고 이후에는 보존한다 (편집기/FMS 웹 UI 수정용). 메타의 `image` 는 이 파일이다.
- **`synth_map.py`**: 같은 레이아웃을 픽셀 마스크로 편다 (칸 중심 = 타일 모서리, gen_map 과 같은 배치). 경계 양쪽 `wall-px/2` 에 벽을 긋고 질감 노이즈를 맵 전체에 한 번에 깐다. 조각 경계가 없어서 이음매가 없다. 저장/검사/파싱은 `gen_map` 의 `save_map`/`check`/`load_layout` 을 쓴다.
  - `--cut X1,Y1,X2,Y2` (world m): 직선 바깥을 막힘으로 잘라 사선 벽을 만든다 (통로 칸 중심 평균 쪽이 남는다). 레이아웃은 1 m 격자라 사선이 계단이 되므로 직사각형 레이아웃 + `--cut` 으로 쓴다. 직선 근처 칸 중심은 통로 검사에서 뺀다. 예: `tools/map_layouts/loop3.txt` (사다리꼴 100×50, 왼쪽 26 m).
  - `--walls FILE` (줄마다 `X1 Y1 X2 Y2` world m): 칸 경계 위에 두께 1 px(+벽 질감) 선분 벽을 긋는다. `#` 칸은 1 m 두께 덩어리라 얇은 내부 벽은 이쪽으로 쓴다. 벽선 코앞(로봇 반폭 + 벽/테두리)에 중심이 있는 칸은 통로 검사에서 뺀다. 문은 선분을 끊어 내고 1.5 m 이상으로 둔다. 예: `tools/map_layouts/loop3.walls`. GUI 에도 같은 칸이 있다.
  - `--void 'x,y x,y x,y ...'` (world m, 여러 개는 `;`): 다각형을 막힘(회색 128)으로 칠한다 (건물 중정 등). 경계에 벽선이 생기고 다각형 안/코앞의 칸 중심은 통로 검사에서 뺀다. GUI 에도 있다. 예: `loop3` 사다리꼴 중정.
  - `--carve 'X1,Y1,X2,Y2;...'`: `--void` 뒤에 적용해 회색 영역 안에 통로 사각형을 판다 (엘리베이터 홀 등). 복도와 닿는 변은 복도 쪽으로 조금 넘기면 벽이 안 생기고 나머지 변에는 벽선이 생긴다. GUI 에도 있다.
- **`clean_map.py`**:
  - 처리 순서: 통로 구멍 메움 → FREE opening → 작은 FREE 섬 삭제 → FREE 에서 먼 비-FREE 를 128 로 → 선택(실선 벽, 값 평탄화).
  - 프리셋: light/normal/strong.
  - scipy 없이 morphology 와 run-length 라벨링을 직접 구현했다.
- **`map_gen_gui.py`**: 옵션 폼 + 시드 랜덤 재생성 버튼. 생성은 자식 QProcess 로 돌린다.
- **`map_editor.py` / `map_edit_model.py`**:
  - 데이터 흐름: grid.png + `edits.json`(world 좌표 도형: rect/line/path, 흰·검·회 3값) → `edited_navi_gridmap.png`.
  - 렌더 규칙은 모델(GUI 없음)이 가진다: 픽셀 중심이 도형 안이면 칠하고 AA 는 없다. 편집기는 UI 만 한다.
  - 시작 시 grid.png 와 edited_navi_gridmap.png 둘 다 필요하다. 저장은 수동이다.
  - **원점 재지정 (O 도구)**: 고른 픽셀 모서리가 world (0,0) 이 된다. 도형 world 좌표를 같이 평행이동해서 픽셀 위치는 그대로다. `fms_map.json` 의 origin 은 저장할 때만, 확인을 받고 쓴다. `edits.json.origin` 은 도형을 저장한 당시의 좌표계이고, `load_edits` 가 이 값이 맵 origin 과 다르면 보정한다.
  - 통로 끊김 검사는 `clean_map.inflate_free`/`label` 을 재사용한다 (로봇 반폭 8 px).
- **통합 런처 `launcher.py` / `launcher_core.py` / `launcher_tools.json`**:
  - `launcher_core` 는 Qt 가 없어서 단독으로 테스트할 수 있다.
  - 툴 목록은 `launcher_tools.json` 에 있고 코드는 툴 이름을 모른다. 예외는 브로커로, `listens` 키로 식별한다. **새 툴은 json 항목만 추가하면 붙는다.** 스키마 설명은 json 의 `_설명` 키에 있다.
  - mode: `single` / `per_map` / `multi`(폼으로 여러 개, `id_fmt`/`step` 으로 자동 이름·좌표) / `oneshot`.
  - cmd 템플릿: `{python}`, `{repo}`, `{host}`, `{port}`, `{map}`, `{폼 키}` 를 치환한다. 조건부 인자는 `{"if": 키, "args": [...]}`.
  - 그 밖의 키: `requires`(["broker"]: 없으면 자동 시작 제안), `needs_map`, `needs_build`(없으면 make 제안), `cwd`, `stop_timeout_s`, `env`({이름: 값}, 값도 치환. 설정을 환경변수로만 받는 `fms_server` 용).
  - 값은 `default_values` 위에 저장값을 덮는다 (`values_for`). 그래서 param 을 새로 추가해도 예전 저장값/재부착 항목이 KeyError 를 내지 않는다.
  - 프로세스: `Popen(start_new_session=True)` 로 띄우고 출력은 `logs/<이름>.log` 로 보낸다. 그래서 런처가 죽어도 자식은 살아남는다. 화면 로그는 이 파일을 tail 한 것이다 (최근 2000 줄).
  - 재부착: 다음 실행 때 `.run/launcher.json` 의 PID 와 명령(`ps -p PID -o command=`)을 대조해서 다시 붙는다.
  - 정지: SIGTERM 을 보낸다. `stop_timeout_s` 가 지나면 사용자 확인 후 프로세스 그룹에 SIGKILL 을 보낸다.
  - **파이썬 Qt 툴(`map_editor.py`, `map_gen_gui.py`)은 SIGTERM/SIGINT 를 `window.close()` 로 바꿔** closeEvent(저장 확인 등)를 타게 했다. 새 Qt 툴을 추가하면 같은 처리를 넣는다 (signal 핸들러 + 200 ms no-op `QTimer`, 이 타이머가 없으면 Qt 루프가 시그널을 못 받는다).
  - PATH 에 brew `sbin` 을 더한다 (mosquitto 위치).


## FMS와 웹 UI

- `main.py`의 lifespan이 저장소·MQTT·맵 코디네이터·작업 코디네이터·승강기 제어기·내장 시뮬레이터·배정 루프를 시작하고 종료한다.
- `task.py`는 작업 상태와 단일 층 Order, 다층 stage 실행을 담당한다. `route.py`가 층간 계획을 만들고 `elevator_ctl.py`가 수신 상태와 임대를 관리한다.
- `allocator.py`는 게시판 작업을 1초마다 확인한다. 수동·자동 배정과 상태 갱신은 작업 코디네이터의 같은 락을 사용한다.
- 지도 버전 저장은 `mapkit/store.py`, 읽기·검증은 `ingest.py`, 주행금지구역 굽기는 `bake.py`가 담당한다. 수정 시 이전 baked 이미지가 아니라 source를 재사용한다.
- `locations.py`의 공유 RLock과 임시 파일 교체가 요청 간 쓰기를 보호한다. 목적지 이름 변경은 프리셋과 미해결 수동 pending 작업의 참조를 갱신하며, 이미 생성된 작업 스냅샷은 유지한다.
- 픽업스테이션(`pickup_stations.py`, `api/pickup_stations.py`)은 맵 버전의 pickup zone과 `locations.json`의 `type=pickup` location을 `zone_id`로 1:1 연결한다.
  락 순서는 `JobCoordinator.lock → _EDIT_LOCK → LocationStore._lock`이다. 생성·zone 변경은 `edit_zone(activate=False)`로 새 버전을 준비하고
  location을 쓴 뒤 활성화한다. 두 파일은 원자적이지 않으므로 목록 조회가 양쪽을 대조해 broken을 표시하고 삭제로 정리한다.
  pickup zone은 `prohibit_zones()`에 들지 않아 굽지 않는다.
- HTML/CSS/JS는 `fms_server/static/`에서 제공하며 npm 빌드나 CDN은 필요 없다. `fms.js`는 현장·맵 선택과 Canvas, `zone-editor.js`/`map-points.js`는 배치 편집, `jobs.js`는 프리셋과 게시판을 맡는다.
- `robot-details.js`는 DOM을 유지하며 팝업 데이터를 갱신한다. `elevator-status.js`는 운행 상태 표만 갱신해 연결 편집 폼을 보존한다. 서버 미연결, 상태 미수신, 오래된 마지막 수신값을 구분한다.
- 뷰어의 따라가기 모드는 `map_follow.py`가 로봇 보고 UID/버전에 맞는 맵을 찾고 `MapCanvas.set_map()`으로 교체한다. GUI 조작은 Qt 메인 스레드에서 수행한다.

## 세부 계약 변경 시

1. Python 원본 common을 바꾸면 뷰어에 대응 사본이 있는 파일도 동기화한다.
2. 로봇 프로토콜이면 `include/schemas.hpp`, `src/schemas.cpp`와 생산자·소비자를 함께 확인한다.
3. planner 팽창 규칙을 바꾸면 `tools/clean_map.py`의 `inflate_free`와 생성 맵 검사도 맞춘다.
4. physics 디버그 필드를 바꾸면 C++ 직렬화와 Qt 뷰어의 표시·자세 변환을 같이 확인한다.
5. 기능 설명은 해당 사용자 문서 한 곳에서 관리하고 다른 문서는 링크로 연결한다.

## LastGate 배송 연동

- 보관소 `lastgate/server/core.py`가 물품·계정·묶음을 SQLite에 저장하고 `locker/robot/*` 계약을 발행한다.
  DB 트랜잭션에 발행 대기열도 함께 기록하며 heartbeat로 active Job을 재통지한다.
- `fms_server/locker.py`가 FMS MQTT 콜백의 큐를 별도 워커에서 소비한다. 네트워크 콜백에서 배송 계획을 수행하지 않는다.
- 배송 전이와 작업 dispatch는 `JobCoordinator.lock`을 공유한다. `external_owner`와 로봇 예약으로
  보관소 접근·최종 묶음 대기·PICK·DROP·복귀·오류 차단 사이의 중복 배정을 막는다.
- 예약과 진행은 `locker_deliveries.json`에서 시작 시 복원한다. 하위 작업 생성 후 연결 저장 전에 중단되면
  external_owner와 단계 이름으로 기존 작업을 찾아 재사용한다. assigned/working 오더는 재발행하지 않는다.
- LastGate 확정 목록이 물품 장부의 기준이다. FMS는 목적지와 좌표를 복사해 하위 작업을 만들고 PICK/DROP FINISHED를 관측한다.
  마지막 액션 뒤 1초 대기로 종료 보고를 관측하며 놓친 보고는 완료로 추정하지 않는다.
- 물품별 DELIVERED는 DROP 후, FAILED와 JOB_DONE은 보관소 복귀 후 발행한다. 불확실 인도는 운영자가
  실제 인도 목록과 잔여 적재물을 대조한 뒤 복귀시킨다. `docs/pickup-station.md`는 픽업스테이션·트레이 설계안이며 구현 단계를 표시한다.
- FMS 물품 장부 `cargo.py`(`cargo.json`)가 물품 ID·묶음·트레이·상태를 소유한다. `locker.py`가 최종 묶음을 장부에 저장한 뒤
  물품별 PICK/DROP 하위 작업을 만들고, tick마다 로봇 action_states를 관측해 RUNNING/FINISHED를 반영한다.
  하위 작업이 FINISHED 없이 끝나면 시작이 관측된 물품만 unknown이 된다. 복귀 후 `TRAY_RETURN:{bundle_id}` DROP 완료로 묶음을 닫는다.

# 로봇 클라이언트와 물리 시뮬레이션

[프로젝트 시작 안내](../README.md) · [개발 구조](architecture.md) · [알려진 문제](known-issues.md)

별도 표시가 없는 명령은 **리포 루트**에서 실행한다. 설치와 빌드는 루트 README를 참고한다.
로봇 한 대마다 C++ 프로세스 하나를 실행하며, MuJoCo가 바퀴·관성·벽 접촉을 계산한다.

다중 로봇의 층별 이동 제한·점유 존은 [교통 제어](traffic.md)를 따른다. 점유 존을 사용할 때는
FMS와 클라이언트를 함께 갱신한다. 미허가 존은 경로 재탐색에서도 막으며 직선 이동은 `TRAFFIC_BLOCKED`로 차단한다.

## 실행

```bash
./robot_client --id AMR-001 --map maps/warehouse/warehouse.json --x 1.2 --y -4.0
./robot_client --id AMR-002 --x 0 --y 0 --gui    # 제어판 창(Qt)
./robot_client --id AMR-003 --host <FMS IP>      # --map 없이: FMS 가 SET_MAP 으로 맵을 내려준다
./launch.sh                                       # 인터랙티브 실행기 — N대 한번에
```

옵션은 `./robot_client --help`. 브로커 주소는 `--host`/`--port` (기본
localhost:1883). `launch.sh`는 이 폴더 안 `maps/`에서만 맵을 찾는다 —
외부 맵을 갱신하려면 이 폴더에도 직접 반영한다.

### 로컬라이징 (켜자마자 위치 힌트 필요)

로봇은 **UNLOCALIZED** 로 뜬다 (`--x/--y/--theta` 는 로봇을 실제로 놓은 자리일 뿐, 로봇은 그걸 모른다).
위치를 알려 주기 전에는 움직이지 않고 오더도 거절한다. 다른 층 맵으로 바뀐 직후(`SET_MAP` 으로 다른 uid)도 같다.

```bash
# 위치 힌트: 실제 자리에서 ±1.5 m, ±30° 안이면 된다. 로봇이 가상 라이다 스캔 매칭으로 정밀 위치를 잡는다 (약 1 초)
tools/send_order AMR-001 --instant INIT_POSITION --param x=90 --param y=45 --param theta=0
curl -X POST http://localhost:8000/api/v1/robots/AMR-001/init-position -H 'Content-Type: application/json' -d '{"x":90,"y":45,"theta":0}'   # FMS 로
```

- 제어판(`--gui`) Localization 칸: **Pose Hint**(위와 같음), **Force Localize**(디버그: 로봇을 그 자리로 옮기고 바로 LOCALIZED, 오더는 버린다).
- `State.localization` = `UNLOCALIZED | LOCALIZING | LOCALIZED`, `localization_score` = 매칭 점수(0~1, 맞으면 보통 0.93 이상, 0.8 미만은 실패).
- 실패하면 WARNING `LOCALIZATION_FAILED` 가 붙고 UNLOCALIZED 로 남는다 (힌트가 너무 틀림 / 벽 속 / 주변에 벽이 안 보임). 더 정확한 힌트를 다시 준다.
- 대칭인 긴 복도에서는 틀린 자리로 잡힐 수 있다 (실제 로봇과 같다). 뷰어 `o`(GT) 로 참 위치와 비교할 수 있다.
- 뷰어: 위치를 모르는 로봇은 흐린 빨간 점선으로 그린다.

### 직선 이동 / 도킹 / 주차 액션

오더 노드의 `action` 으로 지정한다. 셋 다 `params` 가 필수이고, 값이 틀리면 로봇이 오더를 통째로 버린다
(`send_order` 가 보내기 전에 같은 검증을 한다).

| action_type | params | 동작 |
|---|---|---|
| `STRAIGHT` | `distance` (m, + 전진 / - 후진), `speed` (선택, 기본 0.2 m/s) | 노드에 도착해 노드 theta 로 정렬한 뒤, 경로계획 없이 그 방향으로 직선 이동 |
| `DOCK` | `entry` (`FORWARD`/`BACKWARD`), `undock_distance` (m), `min_charge` (%), `speed` (선택) | 충전기에 진입해 충전하며 대기 |
| `PARK` | `entry`, `undock_distance`, `speed` (선택) | 정밀 주차 후 대기 (충전 없음) |

- DOCK/PARK 의 노드 position 은 **진입을 마친 자세**다. 로봇은 그 앞 `undock_distance` 지점까지
  A* 로 가서 theta 로 정렬하고 직선으로 들어간다. `FORWARD` 는 전진 진입, `BACKWARD` 는 후진 진입이다.
  충전기가 벽에 붙어 있어 A* 로는 못 들어가는 칸이라도 된다 (진입 직선은 팽창 검사를 안 한다). 다만 물리 몸체
  (반지름 `width/2 - 0.04`)가 실제로 들어갈 자리여야 한다. 벽에 `0.21 m` 보다 가깝게 찍으면 벽에 눌려
  COLLISION 이다. 진입 시작 지점은 A* 가 닿는 곳이어야 하니 `undock_distance` 를 충분히 준다.
- 빠져나오기(언도킹)는 별도 액션이 없다. 도킹/주차 상태에서 이동이 필요한 오더를 받으면 먼저 진입했던
  직선을 되짚어 나온다. 전진 진입했으면 후진으로, 후진 진입했으면 전진으로 나온다. 진입이나 언도킹 도중
  취소돼도 다음 오더에서 먼저 빠져나온다.
- DOCK 후 충전량이 `min_charge` 미만이면 state 가 `CHARGING` 이고 새 job 을 거절한다. 넘으면 `IDLE` 로
  바뀌어 job 을 받는다 (충전은 언도킹 전까지 계속, `battery.charging=true`).
- **충전기는 맵 폴더의 `chargers.json` 에 둔다** (`fms_map.json` 옆). DOCK 노드 근처(0.3 m)에 충전기가 없으면
  FATAL `DOCK_FAILED` 로 들어가지 않는다. 진입이 끝났을 때 실제 몸이 충전기 자세에서 `tol_xy`(기본 6 cm)/`tol_theta`(0.1 rad)
  안이고 `entry`(접점 방향)가 맞아야 충전한다. 아니면 `DOCK_FAILED` (설명에 어긋난 양). `CHARGE` 액션은 충전기에
  붙어 있을 때만 된다 (`CHARGE_FAILED`). 예: `maps/empty_hall1/chargers.json`
  ```json
  {"chargers": [{"id": "CH-1", "x": 30.0, "y": 6.0, "theta": 0.0, "entry": "BACKWARD"}]}
  ```
  FMS 번들은 `chargers.json`을 선택 파일로 전달한다. 웹에서 도킹 스테이션을 추가하거나, 맵 업로드에 충전기 파일을 포함한다.
- 배터리: `--battery-low`(기본 20%) 아래면 WARNING `LOW_BATTERY`. `--battery-critical`(기본 5%) 아래로 떨어지는 순간
  FATAL `BATTERY_CRITICAL`(오더 버림)이고, RESET_ERROR 뒤에는 DOCK 이 들어 있는 오더만 받는다. 0% 면 FATAL
  `BATTERY_EMPTY` 로 움직이지 못한다 (제어판 배터리 버튼으로 살린다).
- 직선 이동 중에도 충돌 감지(COLLISION), 동적 장애물 대기(우회는 안 함), PAUSE/e-stop 정지·재개가 그대로 된다.
  강제 위치지정을 하면 도킹 상태가 풀린다.
- 마커 보정은 시뮬레이션하지 않는다. 도착 오차는 물리가 만든다: 직선 이동은 진행 방향 1 cm 이내, 옆 방향은
  목표 방위로 조향해서 보통 수 mm 이내다 (아래 물리 절).

```bash
# 리포 루트에서. theta 는 --path 의 세 번째 값 (라디안)
tools/send_order AMR-001 --path 3,1.5,0 --order-id s1 --action STRAIGHT --param distance=-1.0
tools/send_order AMR-001 --path 14.75,5,0 --order-id d1 --action DOCK \
    --param entry=BACKWARD --param undock_distance=0.6 --param min_charge=80
tools/send_order AMR-001 --path 14.75,5,3.14159 --order-id p1 --action PARK \
    --param entry=FORWARD --param undock_distance=0.6
```

### 물리 시뮬레이션 (MuJoCo)

로봇 이동은 MuJoCo 물리 엔진이 계산한다 (기본 동작이고 끌 수 없다). 바퀴 슬립, 관성, 벽과의 접촉이 그대로 나온다.

```text
오더 -> A* 경로 -> 사다리꼴 이상 궤적 -> (v, w) -> 바퀴 속도 서보 -> MuJoCo (2 ms x 25 / tick) -> 실제 자세
                      ^---------- 매 tick 실제(측정) 자세에서 다시 출발 ----------------------'
```

- **이상 궤적은 명령 생성기다.** 기존 `TrapezoidalKinematics` 가 측정 자세에서 한 걸음 낸 변위를 몸체 목표 속도 (v, w) 로
  바꿔 바퀴에 준다. 실제가 못 따라가면 다음 tick 에 그 자리에서 다시 계산한다 (폐루프).
- **로봇은 자기 위치를 추정으로 안다 (참 자세를 모른다).** `State.pose`, 경로 추종, 도착 판정은 전부 추정 자세로 돈다.
  추정 = 바퀴 엔코더 적분(오도메트리) + 위치추정 보정이다. 엔코더는 슬립을 못 봐서 오도메트리는 틀어지고(저마찰이면 12 m 에
  1 m 이상), 보정이 그걸 고친다 (아래 "위치추정 보정"). 참 자세(GT)는 물리 엔진과 뷰어(`physics` 토픽)에만 있고, 벽 충돌 판정은
  참 자세 기준이다. `State.velocity` 는 실제 속력(크기)이다. 강제 재위치(GUI)는 오도메트리와 추정을 그 자세로 맞춘다.
- **몸체 모델**: 차동구동 + 캐스터 2개. 질량 60 kg (`--mass`), 바퀴 반지름 0.075 m, 바퀴 간격 `width - 0.15` m,
  바퀴 토크 한계 12 N·m, 바퀴-바닥 마찰 `--friction` (기본 0.8). 질량 분포는 `length x width x 0.2 m` 직육면체 기준이다.
- **충돌 형상은 원판이다 (반지름 `width/2 - 0.04`).** planner 가 로봇을 반지름 `width/2` 원으로 보고 팽창시키므로
  같은 가정이어야 계획된 경로에서 제자리 회전하다 벽에 부딪히지 않는다. `length` 는 State/뷰어 표시와 관성에만 쓰인다.
  직사각형으로 하면 폭 1 m 통로 모퉁이에서 회전 중 충돌이 났다 (실제로 확인).
- **벽**: FREE 가 아닌 칸(OCCUPIED/UNKNOWN/맵 밖)을 직사각형으로 병합해 상자로 만든다 (실제 맵 2347x1687 px 이 22,897 개, 8 ms).
  로봇 주변 8 m 창 안의 상자만 모델에 넣고, 창 중심에서 3 m 벗어나면 모델을 다시 컴파일한다 (상태는 이어받는다). 그래서 맵
  크기와 tick 비용이 상관없다 (실제 맵 주행 중 CPU 약 4%). 맵 그리드가 원본이고 물리 월드는 로드/`SET_MAP` 때마다 거기서 만든다.
- **충돌 판정**: 몸체-벽 접촉력이 20 N 이상으로 0.25 초 이어지면 FATAL `COLLISION` 이다 (로봇은 그 자리에서 선다).
  시작/강제 위치가 벽 안이면 (v1.3 의 "벽 위에서도 일단 뜬다") 몸체-벽 충돌을 꺼서 엔진이 로봇을 튕겨내지 않게 한다. 기록의 `ghost` 열.
- **정지와 도착**: 경로 끝은 실제로 멈추고 마지막 지점에서 3 cm 이내여야 도착이다 (못 닿으면 마지막 지점을 다시 건다).
  직선 이동(STRAIGHT/DOCK/PARK/언도킹)은 목표 방위로 조향하며 가고, 진행 방향 남은 거리 1 cm 이내에서 서면 끝이다.
  경로의 큰 꺾임(0.3 rad 초과)에서는 감속해 서서 돌고, 작은 꺾임은 서지 않고 조향하며 지나간다.
- **PAUSE/e-stop/강제 정지는 즉시 선다.** 오더 취소나 대기 중에 굴러가고 있으면 `max_deceleration` 으로 감속해 선다.
- **tick 의 dt 는 0.1 초로 자른다.** A* 가 루프를 수백 ms 막았다 돌아와도 로봇이 한 번에 튀지 않는다 (막혀 있던 시간은 건너뛴다).
- **서보 게인은 6 으로 낮게 잡았다.** 60 이면 접촉 compliance 와 얽혀 정지 후에도 몇 cm/s 로 계속 떨렸다.

슬립은 마찰이 낮거나 가속 요구가 마찰 한계를 넘을 때 생긴다. 예: `--friction 0.2 --max-acceleration 1.0`. 정상 마찰(0.8)에서는
슬립률 중앙값이 0.001 이고 추정 오차(오도메트리 대 참)는 12 m 에 2~15 cm 정도다 (제자리 회전 때 바퀴 접촉 때문에 몸체가 오도메트리보다 3~4% 더 돈다).
한계: `--friction 0.12 --max-acceleration 3` 처럼 극단이면 제자리 회전이 접지력을 넘어 방위가 ±0.5 rad 로 헌팅하며 거의 못 간다.
마찰 0.15~0.5, 가속 0.6~2.0 은 완주한다.

**위치추정 보정 (라이다 시뮬레이션, 기본 켜짐)**: 주기마다 "라이다 측정" `z = GT + 가우시안 노이즈` 를 받아 추정을 z 쪽으로
`gain` 만큼 당긴다 (AMCL 이 map->odom 변환을 고치는 구조: `추정 = T ∘ 오도메트리`, `T` 를 갱신). 한 번에 고치는 양은
0.15 m / 0.1 rad 로 묶어 추정이 튀지 않게 하고, 측정은 가끔 빠진다. 보정은 GT 를 노이즈 낀 측정값으로만 쓴다 (로봇이 GT 를 직접 알지 못한다).

| 옵션 | 기본 | 뜻 |
|---|---|---|
| `--loc-rate HZ` | 5 | 보정 주기. **0 이면 보정 없는 순수 오도메트리** (오차 누적 시험용) |
| `--loc-noise-xy M` | 0.02 | 측정 노이즈 위치 표준편차 |
| `--loc-noise-theta RAD` | 0.01 | 측정 노이즈 방위 표준편차 |
| `--loc-gain K` | 0.15 | 한 번에 측정 쪽으로 당기는 비율 (0~1) |
| `--loc-dropout P` | 0.05 | 측정이 빠질 확률 |

`--friction 0.3` 로 4 개 노드를 도는 시험에서 보정이 켜져 있으면 추정 오차가 최대 3 cm 였고 (보정 전 오도메트리는 1 m 이상 벌어짐),
`--loc-rate 0` 이면 추정이 그대로 벌어진다. 낮은 마찰 + 보정 주기가 느리면 제자리 회전에서 보정 지연 때문에 추정이 흔들릴 수 있다 (5 Hz, gain 0.15 는 μ 0.3 까지 완주).
기록 CSV 에는 `x y theta` (GT), `odom_*` (보정 전), `est_*` (보정된 추정) 가 모두 있다. 노이즈에도 경유점 방위가 흔들리지 않도록
`kinematics.cpp` 가 5 cm 안의 경유점은 건너뛰고 2 cm 안이면 방위를 유지한다.

**뷰어 물리 디버그**: 로봇이 `fms/v1/{id}/physics` 토픽으로 tick 마다(20 Hz) 물리 관측값을 발행한다. `State`의 메시지 계약을 확장하지 않고
별도 디버그 토픽으로 제공한다. 필드 변경 시 C++ 직렬화와 뷰어 표시를 함께 맞춘다. 내용: 실제 자세/속도(`v_fwd`, `v_lat`, `yaw_rate`),
바퀴 속도/목표/슬립, 오도메트리(보정 전)/추정(보정 후) 자세, 위치추정 설정과 통계, 몸체 목표 속도, 벽 접촉력, ghost/collided/at_rest, 마찰/질량/한계, 남은 경로 길이와 목표.
Qt 뷰어는 이걸로 자세를 외삽해 부드럽게 그리고(30 fps), 왼쪽 패널(`p` 토글)에 값을 띄우며, 지나온 궤적(`l`), **GT(참 위치, 오차 없음)**
(청록 윤곽 + 십자 + "GT" 라벨 + 청록 점선 궤적, `o`)와 보정 전 순수 오도메트리(노란 점선 윤곽, `i`)를 그린다.
몸체는 로봇이 보고한 위치(`State.pose` = 보정된 추정)이고,
GT 와 벌어진 만큼이 누적 오차다 (패널 `error`). 보정 알고리즘의 기준값은 `physics` 토픽의 `x y theta` 또는 기록 CSV 의 `x y theta` 열이다, 속도 벡터(초록)와, 벽에 눌릴 때 빨간 GT 윤곽을 그린다. 계획 경로는 현재 구간 굵게 + 남은 구간 눈금선 + 경유점 점 +
목표 링과 남은 거리로 표시하고, 이미 지난 경유점은 뺀다. `k` 는 궤적과 피크값(최대 슬립/접촉력)을 지운다.
로컬 제어판(`--gui`)에도 같은 값(Physics 절)과 마찰/질량 설정칸이 있다. 제어판 레이아웃은 `AMR_GUI_SNAPSHOT=파일.png` 로
(`QT_QPA_PLATFORM=offscreen` 과 함께) 1.5 초 뒤 PNG 로 저장해 볼 수 있다.

**데이터 기록 (`--record DIR`)**: tick(50 ms) 마다 한 줄씩 `DIR/<id>_<시작시각>.csv` 에 쓰고, 설정은 `.meta.json` 에 둔다
(크기, 질량, 마찰, 최대 속도/가속도, 시작 자세, seed 등). 오더 하나가 에피소드이고 `order_id` 열로 나눈다 (비어 있으면 대기).

| 열 | 내용 |
|---|---|
| `t`, `wall_time` | 로봇 시작 후 시뮬레이션 시간(s), epoch |
| `state`, `sub_state`, `order_id`, `node_index`, `action` | 상태와 오더 진행 |
| `x y theta` | 실제 자세 (엔진 값) |
| `odom_x odom_y odom_theta` | 바퀴 엔코더만 적분한 자세 |
| `est_x est_y est_theta` | 위치추정 보정이 반영된 자세 |
| `v_fwd v_lat yaw_rate` | 몸체 좌표 실제 속도 (전진 +, 왼쪽 +) |
| `cmd_v cmd_w` | 이상 궤적이 준 몸체 목표 속도 |
| `wheel_w_l/r`, `wheel_cmd_l/r` | 바퀴 각속도와 목표 (rad/s) |
| `slip_l slip_r` | 바퀴별 종방향 슬립률 (0 = 구름, ±1 이상 = 헛돎) |
| `wall_force`, `ghost` | 몸체-벽 최대 접촉력(N), 벽 안 시작 여부 |
| `battery`, `charging`, `detected_obstacles`, `error` | 배터리, 감지 장애물 수, 첫 오류 종류 |

물리가 아닌 것: 동적 장애물은 사각형 좌표일 뿐 물리 객체가 아니다 (로봇이 밀지 못하고 센서 반경으로만 인식). 로봇끼리는
프로세스가 따로라 서로 부딪히지 않는다. IMU 는 없다. 가상 라이다는 초기 로컬라이징(스캔 매칭)에만 쓰고 맵만 본다.

디버깅: `AMR_PHYSICS_DUMP=파일` 환경변수를 주면 마지막으로 컴파일한 MJCF 를 그 파일에 쓴다 (파이썬 `mujoco` 로 열어 볼 수 있다).
통합 테스트: 리포 루트에서 `.venv/bin/python tools/physics_test.py [시나리오...]`로 실행한다.
empty_hall1에서 이동·직선·도킹·충돌·일시정지·저마찰·장거리·장애물·위치 확인 등을 검증하며 전체 실행에는 수 분이 걸린다.

## 맵 동기화 (SET_MAP 다운로더)

FMS 에 로봇이 등록돼 있으면, 접속 후 FMS 가 `fms/v1/{id}/instant` 로 `SET_MAP`
(params: `map_id`=map_uid, `map_version`, `url`) 을 보낸다. 로봇은:

1. `url` 로 번들 zip 을 HTTP GET (`curl`), `--map-cache DIR` (기본 `map_cache/`)
   밑 `<map_uid>/` 에 압축해제 (`unzip`). manifest 의 version 이 이미 캐시에 있는
   것과 같으면 다운로드 생략
2. `<map_uid>/fms_map.json` 으로 `MapModel` 리로드 → 현재 주행 맵 교체 → 현재
   위치 기준 A\* 재계산
3. `State.map_id`(=map_uid)/`map_version` 을 보고. FMS 는 이 값이 배정 맵의 활성
   버전과 같은 로봇에게만 job 을 준다

다운로드는 백그라운드 스레드에서 돈다 — tick 루프(주행/상태보고)를 막지 않고,
실패해도 로봇은 기존 맵으로 계속 돈다. `--gui` 의 **Map Sync** 패널에서 상태
(NONE/SYNCING/SYNCED/FAILED)·map_uid·version·에러를 보고 **Re-sync** 로 재시도.
`curl`/`unzip` 은 fork+exec (shell 안 거침). `map_cache/` 는 gitignore.

## GUI 스레드 모델

Qt 는 메인스레드 전용이라 `--gui` 주면 tick/MQTT 루프를 백그라운드 스레드로
보내고, `CommandQueue`(GUI→루프)와 `std::mutex`(공유 상태 읽기/쓰기)로 보호한다.


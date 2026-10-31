# FMS 서버와 웹 UI

[프로젝트 시작 안내](../README.md) · [승강기와 층간 이동](../docs/elevators.md) · [개발 구조](../docs/architecture.md)

로봇의 MQTT 상태를 수집하고 현장·맵·목적지·작업·승강기를 관리하는 FastAPI 서버다.
웹 UI는 서버의 `/`, API 문서는 `/docs`에서 제공한다. 프런트엔드 빌드나 외부 CDN은 필요 없다.
실행에 다른 리포를 가져올 필요는 없다.

## 실행과 설정

아래 명령은 모두 **리포 루트**에서 실행한다. 설치·C++ 빌드는 루트 README를 참고한다.

```bash
.venv/bin/uvicorn fms_server.main:app --host 127.0.0.1 --port 8000
```

`http://localhost:8000/`에서 UI를 연다. 개발 중 코드 재시작이 필요하면 `--reload`를 추가한다.
파일 저장소·메모리 예약·MQTT 클라이언트를 사용하므로 **worker 1개의 단일 FMS 프로세스**로 실행한다.
기본 브로커 설정은 [broker 안내](../broker/README.md)를 참고한다.

| 환경변수 | 기본값 | 의미 |
|---|---|---|
| `FMS_MQTT_HOST` | `localhost` | 브로커 주소 |
| `FMS_MQTT_PORT` | `1883` | 브로커 포트 |
| `FMS_MQTT_KEEPALIVE` | `30` | MQTT keepalive, 초 |
| `FMS_MQTT_CLIENT_ID` | `fms-server` | MQTT 클라이언트 ID |
| `FMS_MQTT_REQUIRED` | `0` | MQTT 시작 오류를 예외로 올릴지 여부 |
| `FMS_ROBOT_STALE_AFTER` | `3.0` | 로봇 상태 수신 지연 기준, 초 |
| `FMS_DATA_ROOT` | `fms_server/maps` | 현장·맵·작업 영속 데이터 경로 |
| `FMS_PUBLIC_URL` | `http://localhost:8000` | 로봇에 알리는 맵 다운로드 URL의 기준 |
| `FMS_ELEVATOR_SIM` | `1` | 내장 가상 승강기 사용 여부 |

기본 설정에서는 MQTT에 붙지 못해도 서버는 실행되며 재연결을 시도한다.
`FMS_MQTT_REQUIRED=1`은 시작 호출의 오류를 전파하는 옵션이며, 비동기 브로커 연결 완료를 보장하지 않는다.
MQTT 연결 상태는 `/health`와 UI에서 확인한다. 다른 PC의 로봇을 연결할 때는
`FMS_PUBLIC_URL`을 로봇에서 접근 가능한 FMS 주소로 지정한다.

## 웹 UI 사용 순서

1. **현장 설정**에서 현장을 만든다.
2. **맵 관리**에서 층별 맵을 업로드하고 버전을 활성화한다.
3. **로봇**에서 로봇 ID와 배정 맵을 등록한다. 미등록 로봇은 관측만 하고 배정·SET_MAP 대상에서는 제외한다.
4. 로봇이 활성 맵을 받은 뒤 위치 힌트를 주고 `LOCALIZED`를 확인한다.
5. **실시간 맵**에서 목적지·구역·충전 스테이션을 추가한다.
6. **작업 프리셋**을 만들고 **작업 게시판**에서 게시하거나 로봇을 지정한다.
7. 여러 층을 쓸 때는 **엘리베이터**에서 층별 구역을 연결한다.

### 실시간 맵

선택한 활성 맵에 로봇·구역·목적지·충전/주차 위치를 표시한다.
드래그 이동, 휠 확대/축소, 전체 맞춤을 지원한다. 맵에 포커스가 있으면 `+`/`-`/`0`도 쓴다.
로봇이 보고한 맵 UID와 버전이 현재 맵과 일치할 때만 표시한다.

좌측 하단 **＋** 메뉴:

| 항목 | 조작 | 저장 결과 |
|---|---|---|
| 구역 | 드래그 생성, 8방향 핸들/숫자로 사각형 조절 | 새 맵 버전 활성화·전달 |
| 목적지 | 클릭 위치, 드래그 방향, 좌표·각도 입력 | 현재 맵의 named location |
| 도킹 스테이션 | 위치·방향과 전진/후진 진입 지정 | 목적지 + 물리 충전기, 새 맵 버전 |

웹 각도 입력은 **도(°)**이며 API의 `theta`는 **라디안**이다.
구역은 `주행금지(prohibit)` 또는 `엘리베이터(elevator)`를 선택한다.
주행금지는 grid에 OCCUPIED로 굽고, 엘리베이터는 층간 경로 계획에 쓸 주석으로 저장한다.
감속/일방통행 유형은 표시만 하며 해당 주행 제어를 구현한 것은 아니다.
기존 임의 다각형을 사각형으로 자동 변환하지 않는다.

객체 상세는 선택할 때만 연다. 로봇 팝업은 상태·배터리·현재 작업과 단계 진행률을 갱신하고,
좌표·방향·맵 버전 등은 펼침 영역에 둔다. 오류·주의는 바로 표시하며 수신 지연 시 마지막 값임을 알린다.
편집 중 자동 갱신은 초안을 지우지 않고, 저장 중에는 화면 전환을 막는다.

### 목적지와 충전기

- 관리자 목적지의 이름은 현장 전체에서 유일하며 각 항목이 맵 ID와 자세를 갖는다.
- 이름 변경 시 프리셋과 좌표 스냅샷이 없는 수동 pending 작업의 참조를 갱신한다. 이미 생성된 작업의 좌표/오더와 충전기 ID는 유지한다.
- 맵의 `annotations.json` 위치는 버전에 귀속된 원본이다. **목적지·도킹**에서 가져와 작업 목적지로 등록할 수 있다.
- 일반 location API는 맵 밖/주행불가 위치를 경고로 알린다. 실시간 맵 추가 API는 맵 밖 좌표와 오래된 `base_version`을 거절한다.
- 도킹 스테이션은 현재 충전용이다. 웹 등록은 named location과 `chargers.json` 설비를 함께 만든다. 목적지 저장 실패 시 준비한 충전기 버전을 활성화하지 않는다.
- 충전기는 위치·방향·진입 방향이 도킹 목적지와 일치해야 한다. `type=charger` 목적지만 등록하는 것으로 물리 충전기가 생기지는 않는다.

## 맵 입력과 버전

| 입력 | 필수 파일 | 좌표계 기준 |
|---|---|---|
| SLAM 벤더 맵 | `grid_cfg.grid` + 항법 PNG | grid_cfg의 ox/oy, scale, 크기 |
| 생성 맵 | `fms_map.json` + image가 가리키는 PNG | JSON의 해상도·원점·크기·임계값 |

둘 다 있으면 `grid_cfg.grid`를 우선한다. 생성 맵은 이미지 크기와 메타데이터의 일치를 검사한다.
`location_meta.json`, `zone_meta.json`, `map_meta.json`, `chargers.json`은 선택 파일이다.
위치·구역 파일이 없으면 빈 annotations를 만든다. 생성 맵의 image는 같은 폴더의 파일명이어야 한다.

- `map_id`: 현장 안의 관리자 라벨. 예: `floor-1`.
- `map_uid`: `sha256(site_id/map_id)[:24]`. 로봇이 보고하는 전역 맵 ID.
- `version`: 메타·구운 이미지·주석과 선택 충전기 파일의 내용 해시를 결합한 12자 값. 주석의 map_version 필드는 순환 해시를 피하도록 별도 처리한다.
- 동일 입력·동일 정리 설정은 동일 버전으로 저장한다.
- 원본은 `source/`에 보존하고 로봇에는 `navi_gridmap.baked.png`를 전달한다. `fms_map.json.image`도 이 파일을 가리킨다.
- 구역 이동·축소·삭제는 source로 다시 굽는다. 이전 baked 이미지를 재입력하면 삭제한 금지 영역이 남으므로 사용하지 않는다.
- `despeckle` 기본값은 0이다. 현재 제거 픽셀은 OCCUPIED→UNKNOWN이므로 FREE 주행 영역을 넓히지 않는다.

맵 활성화·배정 변경은 MQTT SET_MAP으로 알리고, 파일 자체는 HTTP 번들로 전달한다.
로봇이 배정 맵의 UID/활성 버전을 보고해야 작업을 배정한다.

맵 검증 CLI는 **리포 루트**에서 실행한다. `--write`는 벤더 좌표계에서 메타를 생성하거나 검증한 메타를 저장한다.

```bash
.venv/bin/python -m fms_server.mapkit.ingest maps/gen_loop
```

## 작업 프리셋과 게시판

프리셋은 단계 목록을 재사용하는 템플릿이다. 이동·대기·충전 도킹·주차를 순서대로 구성한다.
`wait`는 직전 위치를 사용하며 첫 단계에는 목적지를 지정해야 한다. 웹 편집기는 이동 뒤에 대기를 추가한다.

```json
{"name":"순회 후 도킹","commands":[
  {"type":"move","target":"a1"},
  {"type":"move","target":"a2"},
  {"type":"wait","duration":10},
  {"type":"move","target":"a3"},
  {"type":"docking","target":"충전소-A","params":{
    "entry":"BACKWARD","undock_distance":0.6,"speed":0.2,"min_charge":80
  }}
]}
```

목적지는 사전에 등록한다. 게시할 때 명령·목적지 좌표·맵을 작업에 복사하므로 이후 프리셋 수정/삭제에 영향을 받지 않는다.
`parking`은 같은 진입 설정을 쓰되 충전기와 `min_charge`를 요구하지 않는다.
`min_charge`는 도킹 완료 조건이 아니라 **다음 작업을 받을 수 있는 배터리 기준**이다.
충전 도킹 완료는 실제 접점 확인으로 판정한다.

### 배정 정책과 상태

- 프리셋 publish에 robot_id가 없으면 자동 배정 게시판에 pending으로 등록한다. 있으면 해당 로봇에 즉시 배정을 시도한다.
- FMS는 1초 주기로 게시 순서를 확인하고 로봇 ID 순으로 가용 후보를 검사한다.
- MQTT 연결, 같은 현장, 현재 배정 맵의 UID/활성 버전, 최신 ONLINE 상태, AUTO·위치 확인·정지 해제·작업 없음, 배터리>0 조건을 확인한다.
- 목적지가 다른 층이면 [승강기 경로](../docs/elevators.md)를 계획한다. 로봇이 목적지와 같은 층이어야만 하는 것은 아니다.
- 후보가 없으면 pending과 대기 이유를 유지한다. 기존 `/jobs`로 만든 수동 pending은 자동 배정하지 않는다.

```text
pending → assigned → working → success / failed
                    취소 요청 → canceling → canceled
```

assigned는 로봇 수신 확인 전에도 예약을 유지한다. assigned/working/canceling 작업은 중복 배정·삭제할 수 없다.
액션 단계는 노드 도착이 아니라 FINISHED 보고로 진행률을 계산한다.
수신 여부가 불명확한 작업은 자동 재게시하지 않는다. 다층 실행의 stage와 한계는 승강기 안내를 참고한다.

## REST와 실시간 API

서버 실행 후 `/docs`에서 최신 요청/응답 스키마를 확인한다.
아래 경로는 `/api/v1` 기준이며 `{site}`는 site_id, `{map}`은 관리자 map_id다.

| 경로 | 메서드 | 역할 |
|---|---|---|
| `/sites` / `/sites/{site}` | GET·POST / GET·DELETE | 현장 |
| `/sites/{site}/maps` / `.../maps/{map}` | GET·POST / GET | 맵 업로드·목록·상세 |
| `.../maps/{map}/manifest`, `/bundle`, `/files/{filename}` | GET | 메타·번들·파일, version 쿼리 지원 |
| `.../maps/{map}/activate` | POST | 맵 버전 활성화 |
| `/sites/{site}/robots` / `.../robots/{id}` | GET·POST / PUT·DELETE | 로봇 등록·재배정 |
| `/sites/{site}/locations` / `.../locations/{name}` | GET·POST / GET·PUT·DELETE | 목적지와 이름 변경 |
| `.../maps/{map}/locations`, `/docking-stations` | POST | 맵에서 직접 추가, base_version 검사 |
| `.../maps/{map}/zones` / `.../zones/{id}` | POST / PUT·DELETE | 사각형 구역, base_version 검사 |
| `/sites/{site}/presets` / `.../presets/{id}` | GET·POST / PUT·DELETE | 작업 프리셋 |
| `.../presets/{id}/publish` | POST | `{}` 자동 배정 / `{"robot_id":"AMR-001"}` 직접 배정 |
| `/sites/{site}/jobs` / `.../jobs/{id}` | GET·POST / GET·DELETE | 작업 생성·조회 |
| `.../jobs/{id}/dispatch`, `/cancel` | POST | 수동 배정·취소 |
| `/sites/{site}/elevators` / `.../elevators/{id}` | GET·POST / GET·PUT·DELETE | 승강기 연결·운행 상태 |
| `.../elevators/unlinked` | GET | 연결되지 않은 구역 |
| `/robots`, `/robots/{id}`, `/robots/stream` | GET, GET, WebSocket | 플릿 상태와 변경 스트림 |
| `/robots/{id}/init-position` | POST | 위치 힌트 |

등록·업로드 예시는 **리포 루트**에서 실행한다. 브로커·FMS는 별도 터미널에서 먼저 켠다.

```bash
curl -X POST http://localhost:8000/api/v1/sites \
  -H 'Content-Type: application/json' --data '{"site_id":"demo","name":"데모 현장"}'
curl -X POST http://localhost:8000/api/v1/sites/demo/maps \
  -F 'map_id=hall' -F 'activate=true' \
  -F 'files=@maps/empty_hall1/fms_map.json' \
  -F 'files=@maps/empty_hall1/edited_navi_gridmap.png'
curl -X POST http://localhost:8000/api/v1/sites/demo/robots \
  -H 'Content-Type: application/json' --data '{"robot_id":"AMR-001","map_id":"hall"}'
```

## 저장 구조

`FMS_DATA_ROOT` 아래에 저장한다. 기본 디렉터리 `fms_server/maps/`는 gitignore 대상이다.

```text
sites/{site_id}/
  site.json
  locations.json
  presets.json
  jobs.json
  elevators.json
  maps/{map_id}/
    active
    {version}/
      fms_map.json
      navi_gridmap.baked.png
      annotations.json
      manifest.json
      chargers.json             # 충전기가 있는 버전만
      source/                   # 원본, 번들에는 제외
```

## 코드 위치와 검증

| 모듈 | 책임 |
|---|---|
| `main.py`, `config.py` | 수명 주기와 설정 |
| `sites.py`, `locations.py`, `presets.py` | 현장·목적지·템플릿 저장 |
| `fleet.py`, `mqtt_client.py`, `coordinator.py` | 상태 수집과 맵 동기화 |
| `task.py`, `allocator.py`, `route.py` | 작업 상태·자동 배정·층간 계획 |
| `elevators.py`, `elevator_ctl.py`, `elevator_sim.py` | 승강기 연결·제어·시뮬레이션 |
| `mapkit/` | 입력 검증·주석·맵 굽기·버전 저장 |
| `api/`, `static/` | REST/WebSocket과 웹 UI |

검증 명령은 **리포 루트**에서 실행한다. 테스트는 MQTT 포트와 데이터 루트를 격리한다.

```bash
.venv/bin/python -m pytest fms_server/tests/ -q
```

트래픽 예약·데드락 처리와 자동 충전 스케줄링은 아직 골격이다.
전체 제약과 해결 기록은 [알려진 문제](../docs/known-issues.md)에 모아 둔다.

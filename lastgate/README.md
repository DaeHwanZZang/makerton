# LastGate 보관소·키오스크·세대 웹·관리자 웹 (v4.0)

LastGate는 택배 접수, 보관, 직접 수령, 배송 시간 예약, 배송 묶음을 맡습니다. 로봇 배정·주행·승강기는 상위 저장소의 FMS가 맡습니다.
v4.0부터는 동·호가 고정되어 있지 않습니다. 관리자가 처음 실행할 때 현장(건물·호실)을 설정합니다.

| 경로 | 역할 |
|---|---|
| `server/` | Python·SQLite 보관소 서버. 현장 설정, 계정, 접수, 보관함 배정, 수령 방식, 배송 예약, 배송 묶음, 알림 |
| `web/` | React·TypeScript 웹 키오스크. `/input`(기사 입력), `/output`(직접 수령) |
| `end_user_wep/` | 세대 웹. 입주민 화면과 Node.js HTTP→MQTT 중계(BFF). 원본 폴더 이름을 유지합니다 → [세대 웹 안내](end_user_wep/README.md) |
| `admin_web/` | 관리자 웹. 보관소 PC 전용(`127.0.0.1:4190`) → [관리자 웹 안내](admin_web/README.md) |
| `shared/` | 세대 웹·관리자 웹이 함께 쓰는 MQTT 요청·응답 클라이언트(`locker-client.mjs`) |
| [`protocol.md`](protocol.md) | LastGate MQTT 메시지 명세 (v4.0) |
| [`../fms_server/locker.py`](../fms_server/locker.py) | FMS 배송 연동·로봇 예약·복귀 (LastGate v4.0과 맞지 않는 부분은 아래 [FMS 연동](#fms-연동-현재-맞지-않는-부분)) |

## 설치·실행

macOS 기준입니다. Python은 루트의 공용 `.venv`를 쓰고, Node.js 20.6 이상이 더 필요합니다.
아래 설치 명령은 **리포 루트**에서 실행합니다.

```bash
.venv/bin/pip install -r requirements.txt
npm --prefix lastgate/web ci
npm --prefix lastgate/end_user_wep ci
npm --prefix lastgate/admin_web ci
```

**리포 루트**에서 `.venv/bin/python tools/launcher.py` 를 실행하면 브로커·FMS·로봇과 함께
LastGate 보관소 서버, 웹 키오스크, 세대 웹, 관리자 웹을 하나씩 시작할 수 있습니다.
FMS 대역(`server/tools/fms_stub.py`)은 실제 FMS와 동시에 실행하지 않습니다.

하나씩 따로 실행할 때는 **리포 루트**에서 브로커와 FMS를 켭니다.

```bash
mosquitto -c broker/mosquitto.conf -v
```

```bash
.venv/bin/uvicorn fms_server.main:app --host 127.0.0.1 --port 8000
```

보관소 서버는 **`lastgate/server/`** 에서 실행합니다.

```bash
../../.venv/bin/python server.py
```

웹 키오스크는 **`lastgate/web/`** 에서 실행합니다.

```bash
npm run dev
```

세대 웹은 **`lastgate/end_user_wep/`** 에서 실행합니다.

```bash
npm start
```

관리자 웹은 **`lastgate/admin_web/`** 에서 실행합니다.

```bash
npm start
```

| 주소 | 화면 |
|---|---|
| `http://localhost:8000/` | FMS 관제 |
| `http://localhost:8000/static/locker.html` | 보관소 목적지 연결·배송 현황·오류 복귀 (FMS) |
| `http://localhost:4173/input` | 기사 입력 키오스크 |
| `http://localhost:4173/output` | 직접 수령 키오스크 |
| `http://localhost:4180/` | 세대 웹 |
| `http://127.0.0.1:4190/` | 관리자 웹 (보관소 PC에서만) |

DB는 `lastgate/server/data/locker.db` 에 저장됩니다. 서버는 사용자 DB를 자동으로 지우지 않습니다.
v3 DB(동·호 고정, 크기 S/M/L)가 있으면 첫 실행 때 v4로 변환합니다(`server/migrate.py`).
옛 동·호는 현장 설정이 되고, 계정과 진행 중 물품은 그대로 옮겨집니다.

## 처음 설정

1. 보관소 서버와 관리자 웹을 켜고 `http://127.0.0.1:4190/` 에서 `admin` / `admin1234` 로 로그인합니다.
   서버를 처음 실행할 때 `LASTGATE_ADMIN_PASSWORD` 환경변수가 있으면 그 값이 관리자 비밀번호가 됩니다. 로그인한 뒤 바로 바꿉니다.
2. **초기 설정**에 아래 항목을 넣습니다. 미리보기에서 만들어질 계정 수와 호실 안내를 확인하고 저장합니다.
   - 사이트 이름과 용어(예: 동/호, 건물/호실)
   - 건물: 번호 1~999, 이름, 호실 범위, 없는 호실, 추가 호실
   - 보관함 수, 배송 불가 보관함 문 수, 로봇 대수, 배송 시간대
3. 호실마다 세대 계정이 만들어집니다. 아이디는 `{건물번호}-{호실}`, 초기 비밀번호는 `1234` 입니다. 첫 로그인 때 비밀번호를 바꾸라는 안내가 나옵니다.
4. 키오스크와 세대 웹은 retained 메시지 `locker/config/site` 로 설정을 받습니다. 설정이 바뀌면 사용자가 입력 중이 아닐 때 화면을 새로 불러옵니다.

시연할 때는 관리자 웹 대신 **`lastgate/server/`** 에서 예시 설정을 바로 넣을 수 있습니다.

```bash
../../.venv/bin/python -m tools.init_site            # 예시 아파트 101~106동 × 101~412호 (288세대)
```

```bash
../../.venv/bin/python -m tools.init_site --campus   # 예시 대학교 (없는 호실·추가 호실 포함)
```

테스트 택배는 **`lastgate/server/`** 에서 `../../.venv/bin/python -m tools.seed_packages 12 --building 101 --unit 101` 처럼 접수합니다.
`--fragile`, `--oversize` 를 붙이면 직접 수령 전용으로 접수합니다.

## 보관·수령 규칙

- 입력 키오스크에서 건물·호실을 입력하고, 물품이 일반(입력구 틀 600×400×220mm 안)인지 규격 초과인지, 취급 주의인지 고릅니다.
  - 일반: 그리드 보관함 1칸에 물품 1개
  - 규격 초과: 배송 불가 보관함 문 1개에 물품 1개
- 규격 초과·취급 주의 물품과 배달에 2번 실패한 물품은 **직접 수령 전용**이라 로봇 배송으로 바꿀 수 없습니다.
- 세대 기본 수령 방식은 `ROBOT` 입니다. 세대 웹에서 기본값과 물품별 방식을 바꿀 수 있습니다.
- 직접 수령은 출력 키오스크(세대 로그인)에서 하거나, 세대 웹의 인증코드 6자리로 관리자 웹 **코드로 수령 처리**에서 합니다.

## 배송 시간 예약과 배송

- 로봇 배송 물품은 보관된 뒤 **30분 동안** 배송 시간을 고를 수 있습니다(`CHOICE_OPEN`). 마감 10분 전에 알림이 가고, 고르지 않으면 자동 배송(`AUTO`)으로 확정됩니다.
- 예약 칸은 **10분 단위**이고 칸마다 **2자리, 선착순**입니다. 한 세대가 2자리를 모두 쓰거나, 두 세대가 1자리씩 쓸 수 있습니다.
  물품 2개를 한 번에 예약하면 둘 다 잡히거나 둘 다 실패합니다.
- 지금부터 20분 뒤 칸부터 24시간 안, 배송 시간대 안에서 고릅니다. 변경·취소는 칸 시작 20분 전까지 됩니다.
- **로봇은 물품이 2개 모여야 출발합니다.** 1개만으로는 배송을 발행하지 않고 짝을 기다립니다(기다리는 동안에도 직접 수령은 됩니다).
- 예약 칸은 시작 15분 전에 배송을 발행합니다. 예약 물품이 1개뿐이면 자동 배송 물품이나 발행 시각이 지난 다른 칸의 예약 물품 하나를
  함께 싣습니다(같은 건물 우선, 없으면 가까운 건물). 예약 세대를 먼저 들릅니다. 짝이 없으면 기다리고, 칸이 시작됐는데 출발하지 못했으면 지연 알림이 갑니다.
- 자동 배송 물품은 2개를 짝지어 보냅니다(같은 세대 → 같은 층 → 같은 건물 → 가까운 건물).
  30분 안에 출발할 예약이 있으면 마지막 로봇은 자동 배송에 쓰지 않습니다.
- 늦은 확정: 로봇이 출력구에 도착한 시점에 남은 물품만 싣습니다. 1개만 남았으면 대기 중인 물품으로 2개를 채우고,
  채우지 못하면 출발하지 않습니다(`JOB_CANCELLED`, 남은 물품은 보관 중으로 돌아가 다음 짝을 기다립니다).
  로봇 도착 전에 모두 빠지면 바로 Job을 취소합니다.
- 세대 웹은 SSE로 알림·목록·남은 자리를 바로 받고, 60초마다 한 번 더 확인합니다. 로봇 현황에는 내 택배를 싣고 있는 로봇만 보입니다.

배송 묶음 형식, 알림 종류, 오류 코드는 [protocol.md](protocol.md)에 있습니다.

## 실제 로봇 연결 순서

1. FMS에서 현장·맵·승강기 연결을 준비하고 로봇을 등록합니다.
2. 맵의 **＋ → 픽업스테이션**으로 구역·픽업 지점(트레이 적재)·반납 지점(빈 트레이 반납)을 만들고, 배송할 세대의 목적지를 등록합니다.
   반납 지점이 없는 기존 스테이션은 구역을 선택해 **반납 지점 추가**를 해야 배송에 쓸 수 있습니다.
3. **보관소 배송**에서 현장·픽업스테이션·`건물-호실 → 목적지` 연결을 저장합니다.
   LastGate v4.0의 `dong`/`ho` 는 `건물번호`/`호실` 과 같은 값입니다. 예: 건물 1의 101호실 → `1-101` (세대 계정 아이디와 같음).
4. 로봇이 맵 활성 버전을 받았는지, INIT_POSITION 뒤 LOCALIZED 상태인지 확인합니다.
5. 입력 키오스크에서 접수하고 투입을 완료합니다. 로봇 배송 물품이 **2개 모이면** 예약 칸이나 자동 배송 규칙에 따라 배송이 발행됩니다.
6. 세대 웹에서 출발·배송 완료 알림을 확인합니다. 로봇은 그 뒤 픽업스테이션 반납 지점에 빈 트레이를 반납하고 가까운 충전 스테이션에 도킹합니다
   (보관소 배송 화면의 '반납 후 충전 스테이션'에서 자동·도킹 안 함·특정 충전소를 고릅니다).
   진행은 FMS 로봇 팝업의 [진행 상세]나 작업 게시판의 배송 행에서 볼 수 있습니다.

FMS의 PICK·DROP 하위 작업, 물품 장부(`cargo.json`), 실패 시 `blocked` 처리와 복귀 절차는
[`../fms_server/locker.py`](../fms_server/locker.py)와 [FMS 안내](../fms_server/README.md)를 따릅니다.

### FMS 연동

- FMS는 트레이 순서(`TRAY_A` → `TRAY_B`)대로 세대를 방문합니다. 같은 세대 물품은 이동을 공유하고 DROP은 물품마다 따로 합니다.
- 로봇 도착 전 `JOB_CANCELLED`(`EMPTIED`)를 받으면 FMS는 배정 전이면 배송을 끝내고, 이동 중이면 이동을 멈추고 충전 스테이션으로 보냅니다.
- 세대 목적지 연결이 없으면 FMS 배송은 로봇 배정 전 '확인 필요'로 멈춥니다. 보관소 배송 화면에서 세대를 연결하면 이어서 진행하고,
  연결할 수 없으면 작업 게시판(또는 보관소 배송 화면)에서 **배송 취소**를 누릅니다. FMS는 `JOB_REJECTED`를 보내고,
  연결 없는 세대의 물품은 직접 수령으로 바뀝니다(세대 웹에서 로봇 배송으로 되돌릴 수 있습니다). 나머지 물품은 다음 짝을 기다립니다.
- 묶음 `summary.kind`·`slot_start`, 물품 `building_name`·`floor` 는 FMS가 받기만 하고 아직 쓰지 않습니다.

FMS 없이 배송 흐름만 시험할 때는 **`lastgate/server/`** 의 FMS 대역을 씁니다(`../../.venv/bin/python -m tools.fms_stub`).

## 실패·재시작

- 보관소 서버는 SQLite 발행 대기열(outbox)과 10초 active Job 재통지로 연결 단절을 복구합니다. 중복 수신을 전제로 합니다.
- 같은 `(client_id, request_id)` 요청은 한 번만 처리하고 같은 응답을 다시 보냅니다.
- 세대 웹 서버는 세션을 저장하지 않습니다(쿠키 = 서버 세션 토큰). 재시작해도 다시 로그인하지 않아도 됩니다.
- 보관소 서버는 프로세스 하나로 실행합니다. 보관소 서버를 여러 개 두거나 worker 여러 개로 운영하는 것은 지원하지 않습니다.

| 환경변수 | 쓰는 곳 | 뜻 |
|---|---|---|
| `LOCKER_BROKER_HOST`, `LOCKER_BROKER_PORT` | 보관소 서버·세대 웹·관리자 웹 | MQTT 브로커 (기본 localhost:1883) |
| `LOCKER_BROKER_URL` | 세대 웹·관리자 웹 | 브로커 전체 주소 (HOST/PORT 대신) |
| `LOCKER_BROKER_USER`, `LOCKER_BROKER_PASS` | 보관소 서버·세대 웹·관리자 웹 | 브로커 계정 (운영 시. [protocol.md 11장](protocol.md#11-운영-시-mqtt-접근-제어-권장)) |
| `LOCKER_DB_PATH` | 보관소 서버 | DB 위치 |
| `LASTGATE_ADMIN_PASSWORD` | 보관소 서버 | 처음 만들 관리자 비밀번호 |
| `PORT`, `HOST` | 세대 웹(4180)·관리자 웹(4190, 127.0.0.1) | 웹 서버 주소 |
| `RESIDENT_WEB_ID` | 세대 웹 | 세대 웹 서버 MQTT client_id 의 고정 부분 (서버를 여러 대 둘 때 서로 다르게) |
| `VITE_BROKER_URL` | 웹 키오스크 | 브로커 WebSocket 주소 (기본 `ws://<접속 호스트>:9001`) |
| `VITE_ADMIN_URL` | 웹 키오스크 | 런처 화면의 관리자 웹 링크 (기본 `http://localhost:4190`) |
| `FMS_MQTT_HOST`, `FMS_MQTT_PORT`, `FMS_DATA_ROOT` | FMS | |

외부 단말을 연결할 때는 브로커 바인딩과 접근 정책도 따로 설정해야 합니다. 시연 환경은 익명 접속입니다.

## 검증

보관소 단위 테스트는 **`lastgate/server/`** 에서 실행합니다.

```bash
../../.venv/bin/python -m pytest tests -q
```

웹 빌드와 BFF 테스트는 **리포 루트**에서 실행합니다.

```bash
npm --prefix lastgate/web run build
```

```bash
npm --prefix lastgate/end_user_wep test
```

```bash
npm --prefix lastgate/admin_web test
```

FMS 회귀 테스트는 **리포 루트**에서 `.venv/bin/python -m pytest fms_server/tests/ -q` 로 실행합니다. 1개 묶음 처리는 FMS를 바꾼 뒤 확인해야 합니다.

`end_user_wep/` 안의 이전 운영자·기사 화면(`admin.html`, `kiosk.js`, `index.html`, `styles.css`)과 자체 DB 모듈(`db.mjs`)은 쓰지 않는 보관 코드입니다.
`lastgate_v2_update.mjs` 는 소스를 덮어쓰는 이전 도구이므로 실행하지 않습니다.

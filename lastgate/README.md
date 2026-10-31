# LastGate 보관소·키오스크·세대 웹

택배 접수·보관·직접 수령·배송 묶음은 LastGate가, 로봇 배정·주행·승강기는 상위 저장소의 FMS가 담당한다.
원본 `solo_prj`의 사용 중인 서버·웹 두 개를 가져왔다. 이전 Tkinter 키오스크는 포함하지 않는다.

| 경로 | 역할 |
|---|---|
| `server/` | Python·SQLite 보관소. 계정, 접수, 수령 방식, 적재 묶음, 물품 상태 |
| `web/` | React·TypeScript 웹 키오스크. `/input`, `/output` |
| `end_user_wep/` | 입주민 화면과 Node.js HTTP→MQTT 중계. 원본 폴더 이름 유지 |
| [`protocol.md`](protocol.md) | LastGate MQTT 메시지 |
| [`../fms_server/locker.py`](../fms_server/locker.py) | FMS 배송 연동·로봇 예약·복귀 |

## 설치·실행

macOS 기준이다. Python은 루트 공용 `.venv`를 사용한다. Node.js 20.6 이상이 추가로 필요하다.
아래 설치 명령은 **리포 루트**에서 실행한다.

```bash
.venv/bin/pip install -r requirements.txt
npm --prefix lastgate/web ci
npm --prefix lastgate/end_user_wep ci
```

**리포 루트**에서 `.venv/bin/python tools/launcher.py`를 실행하면 브로커·FMS·로봇과 함께
LastGate 보관소 서버·웹 키오스크·세대 웹을 각각 시작할 수 있다. 각 항목은 별도로 시작한다.
FMS 대역(`server/tools/fms_stub.py`)은 실제 FMS와 동시에 실행하지 않는다.

개별 실행 시 **리포 루트**에서 브로커와 FMS를 각각 켠다.

```bash
mosquitto -c broker/mosquitto.conf -v
.venv/bin/uvicorn fms_server.main:app --host 127.0.0.1 --port 8000
```

보관소 서버는 **`lastgate/server/`**에서 실행한다.

```bash
../../.venv/bin/python server.py
```

웹 키오스크는 **`lastgate/web/`**에서 실행한다.

```bash
npm run dev
```

세대 웹은 **`lastgate/end_user_wep/`**에서 실행한다.

```bash
npm start
```

| 주소 | 화면 |
|---|---|
| `http://localhost:8000/` | FMS 관제 |
| `http://localhost:8000/static/locker.html` | 보관소 목적지 연결·배송 현황·오류 복귀 |
| `http://localhost:4173/input` | 기사 입력 키오스크 |
| `http://localhost:4173/output` | 주민 직접 수령 키오스크 |
| `http://localhost:4180/` | 세대 웹 |

최초 보관소 실행 시 101~106동 × 4층 × 층당 12호, 총 288세대가 생성된다.
아이디는 `101-101` 같은 동-호, 시연용 초기 비밀번호는 `1234`다.
DB는 `lastgate/server/data/locker.db`에 저장한다. 사용자 DB를 자동 초기화하지 않는다.

## 실제 로봇 연결 순서

1. FMS에서 현장·맵·승강기 연결을 준비하고 로봇을 등록한다.
2. 맵의 **＋ → 픽업스테이션**으로 구역·픽업 지점(트레이 적재)·반납 지점(빈 트레이 반납)을 만들고, 배송할 세대의 목적지를 등록한다.
   반납 지점이 없는 기존 스테이션은 구역을 선택해 **반납 지점 추가**를 해야 배송에 쓸 수 있다.
3. **보관소 배송**에서 현장·픽업스테이션·`동-호 → 목적지` 연결을 저장한다.
   이전 설정의 일반 목적지(`pickup_target`)도 동작하지만 픽업스테이션을 선택해 저장하면 전환된다.
   등록되지 않은 세대의 예상 배송은 배정하지 않고 연결 설정을 기다린다.
4. 로봇이 맵 활성 버전을 받고 INIT_POSITION 이후 LOCALIZED인지 확인한다.
5. 입력 키오스크에서 접수하고 투입 완료한다. 기본 수령 방식이 ROBOT인 물품이 **2개 모이면** 자동 배송이 시작된다.
6. 세대 웹에서 배송 완료를 확인한다. 로봇은 이후 픽업스테이션 반납 지점에 빈 트레이를 반납하고 가까운 충전 스테이션에 도킹한다
   (보관소 배송 화면의 '반납 후 충전 스테이션'에서 자동·도킹 안 함·특정 충전소를 고른다).
   진행은 FMS 로봇 팝업의 [진행 상세]나 작업 게시판의 배송 행에서 볼 수 있다.

## 배송과 예약

로봇 적재부는 두 트레이(`TRAY_A`, `TRAY_B`)이며 트레이 하나에 물품 하나, 배송 묶음은 물품 2개다.
`DELIVERY_JOB`은 예상 목록이다. FMS는 로봇을 예약해 픽업스테이션 node로 보내고 도착 후 `ROBOT_ARRIVED`를 보낸다.
그때 보관소가 직접 수령·수령 방식 변경·추가 입고를 반영해 다시 2개인지 확인하고 `BUNDLE_CONFIRMED`를 확정한다.
1개가 되면 적재하지 않고 취소하며 남은 물품은 다음 짝을 기다린다.
최종 목록만 배송한다. 같은 세대의 물품도 이동은 공유하되 DROP은 물품마다 따로 한다. 세대는 동·호 문자열 순서로 방문한다.
실제 거리 최적화는 아직 하지 않는다. 다른 맵의 세대는 기존 승강기 계획·실행기를 사용한다.

FMS는 물품별 PICK → 세대별 물품별 DROP → 반납 지점 이동·칸별 트레이 반납 → 충전 스테이션 도킹을 각각 하위 작업으로 만들고
그 사이에도 같은 로봇의 예약을 유지한다. 물품 ID·묶음·트레이·상태는 FMS 물품 장부(`cargo.json`)에 저장한다.
일반 작업의 수동·자동 배정은 이 예약을 넘을 수 없다. 각 작업의 맵·좌표는 스냅샷으로 보존한다.
하위 작업은 작업 게시판에서 취소할 수 있지만 수동 재배정·삭제할 수 없다.

PICK·DROP은 기존 C++ 시뮬레이터의 시간 기반 액션이다. 기본 2초 후 액션 FINISHED를 인도로 간주한다.
이는 실제 문 열림·적재 센서·주민 수령 인증을 구현한 것이 아니다. 마지막 액션 종료 보고를 관측하도록
뒤에 1초 대기를 둔다. FINISHED 보고를 놓치면 인도 완료로 추측하지 않고 해당 배송을 차단한다.

보관소는 최종 목록 확정 때 물품을 LOADED로 표시한다. 이는 FMS 장부의 `onboard`(PICK FINISHED)와 다르며
물리 적재 센서 확인과도 구별해야 한다. 두 PICK이 모두 FINISHED여야 출발하고, 하나만 되면 차단한다.
DROP FINISHED가 되면 FMS 맵에 해당 물품을 그 작업 위치의 바닥에 표시한다(논리적 하차 결과, 팔·낙하 물리 없음).
복귀 후 트레이 반납 DROP이 FINISHED가 되면 남은 물품은 `returned`, 트레이는 비고 묶음이 닫힌다.
DROP 성공한 물품은 즉시 DELIVERED 결과를 보내고, JOB_DONE은 로봇이 보관소에 복귀한 뒤 보낸다.
실패 물품의 FAILED 결과도 복귀가 확인된 뒤 보내므로 이동 중에 가상 재입고되지 않는다.

## 실패·재시작

- 상태·로봇 예약·하위 작업 연결은 FMS 데이터 루트의 `locker_deliveries.json`에 저장한다.
- 재시작 시 예약을 복원하고 기존 assigned/working 작업을 감시한다. 수신 불명확 오더를 재발행하거나
  다른 로봇으로 넘기지 않는다. 계속 assigned인 작업은 게시판에서 취소하고 로봇의 취소 보고를 기다린다.
- 실패·취소·완료 보고 누락 시 배송은 `blocked`가 된다. 로봇 예약은 유지한다.
- 보관소 배송 화면에서 실제 인도 완료 물품과 로봇에 남은 물품을 대조한 뒤 **적재물 확인·복귀**를 요청한다.
  로봇 오류 해제·LOCALIZED·가용 상태가 확보되면 원래 보관소 좌표로 복귀한다. 미확인 실행 중 작업이 있으면 복귀 요청을 거절한다.
- 세대 목적지 연결이 없으면 배송은 로봇 배정 전 '확인 필요'로 멈추고, 보관소는 진행 중 배송이 끝날 때까지 새 배송을 만들지 않는다.
  보관소 배송 화면에서 세대를 연결하면 이어서 진행한다. 연결할 수 없으면 작업 게시판(또는 보관소 배송 화면)에서 **배송 취소**를 누른다.
  연결 없는 세대의 물품은 직접 수령으로 바뀌고(세대 웹에서 로봇 배송으로 되돌릴 수 있다), 나머지 물품으로 새 배송을 만든다.
- FMS 결과는 중복 안전하게 재전송한다. 보관소는 SQLite 발행 대기열과 10초 active Job 재통지로 연결 단절을 복구한다.
  브로커 PUBACK와 DB 커밋을 하나의 트랜잭션으로 묶는 구조는 아니며, 중복 수신을 전제로 한다.
- 보관소는 단일 프로세스로 실행한다. 복수 보관소 서버·멀티 worker 운영은 지원하지 않는다.

설정은 `/api/v1/locker/config`에 저장한다. FMS 재시작 없이 적용되며, 이미 예약한 배송의 좌표는 유지한다.
보관소는 `LOCKER_BROKER_HOST`, `LOCKER_BROKER_PORT`, `LOCKER_DB_PATH`,
FMS는 `FMS_MQTT_HOST`, `FMS_MQTT_PORT`, `FMS_DATA_ROOT` 환경변수를 사용한다.
키오스크 WebSocket 주소는 `VITE_BROKER_URL`로 바꿀 수 있다. 기본 공용 브로커는 localhost TCP 1883·WS 9001이다.
외부 단말 연결 시 브로커 바인딩·접근 정책도 별도로 설정해야 한다.

## 검증

**리포 루트**에서 FMS·연동 회귀를 실행한다.

```bash
.venv/bin/python -m pytest fms_server/tests/ -q
npm --prefix lastgate/web run build
npm --prefix lastgate/end_user_wep test
```

보관소 단위 테스트는 **`lastgate/server/`**에서 실행한다.

```bash
../../.venv/bin/python -m unittest discover -s tests -q
```

이전 운영자·기사 화면과 자체 DB 모듈은 `end_user_wep/` 안에 보관 코드로 남아 있다.
`lastgate_v2_update.mjs`는 소스를 덮어쓰는 이전 도구이므로 실행하지 않는다.
세대 웹의 실제 API는 [세대 웹 안내](end_user_wep/README.md)를 참고한다.

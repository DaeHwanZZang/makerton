# LastGate MQTT 프로토콜 v3.3

- 모든 메시지는 **JSON(UTF-8)**, **QoS 1**
- 브로커(내부 서버): TCP `1883`(서버·FMS·도구), WebSocket `9001`(웹 키오스크)
- 클라이언트 요청에는 `request_id`(요청마다 고유)와 `client_id`(접속마다 고유)를 넣습니다.
- 서버는 응답을 **요청한 클라이언트 전용 토픽** `locker/client/{client_id}/res` 로만 보냅니다.
- 서버는 `(client_id, request_id)` 로 중복 요청을 걸러, 같은 요청이 두 번 와도 한 번만 처리하고 같은 응답을 다시 보냅니다.

## 1. 토픽

| 토픽 | 발행 → 구독 | 메시지 |
|---|---|---|
| `locker/input/register` | 입력 키오스크 → 서버 | `REGISTER_REQUEST` |
| `locker/input/port_confirm` | 입력 키오스크(또는 센서) → 서버 | `PORT_DETECTED` |
| `locker/input/stats` | 입력 키오스크 → 서버 | `INPUT_STATS_REQUEST` (오늘 등록 건수) |
| `locker/auth/login` | 출력 키오스크 → 서버 | `LOGIN_REQUEST` |
| `locker/auth/logout` | 출력 키오스크 → 서버 | `LOGOUT_REQUEST` |
| `locker/output/parcels` | 출력 키오스크 → 서버 | `PARCELS_REQUEST` |
| `locker/output/retrieve` | 출력 키오스크 → 서버 | `RETRIEVE_REQUEST` |
| `locker/resident/default_mode` | 세대 웹 · `tools/set_mode.py` → 서버 | `DEFAULT_MODE_REQUEST` |
| `locker/resident/item_mode` | 세대 웹 · `tools/set_mode.py` → 서버 | `ITEM_MODE_REQUEST` |
| `locker/resident/parcels` | 세대 웹 → 서버 | `RESIDENT_PARCELS_REQUEST` (배송·수령 이력 포함) |
| `locker/resident/pickup_code` | 세대 웹 → 서버 | `PICKUP_CODE_REQUEST` |
| `locker/resident/hide` | 세대 웹 → 서버 | `HIDE_REQUEST` (완료 택배 목록에서 숨김) |
| `locker/robot/{robot_id}/state` (retained) | FMS → 세대 웹 | 로봇 상태 (서버는 사용하지 않음) |
| `locker/client/{client_id}/res` | 서버 → 해당 클라이언트 | 모든 응답, `PARCELS_UPDATE`, `REGISTER_CANCELLED` |
| `locker/state/summary` (retained) | 서버 → 전체 | `SUMMARY` (상태가 바뀔 때마다 + 10초 하트비트) |
| `locker/storage/log` | 서버 → 전체 | 가상 보관소 크레인 동작 로그 (`STORE`/`RETRIEVE`/`MOVE`) |
| `locker/robot/job` | 서버 → FMS | `DELIVERY_JOB` |
| `locker/robot/bundle` | 서버 → FMS | `BUNDLE_CONFIRMED`, `JOB_CANCELLED` |
| `locker/robot/event` | FMS → 서버 | `ROBOT_ARRIVED`, `DELIVERY_RESULT`, `JOB_DONE` |
| `locker/client/{id}/online`, `locker/server/online` (retained) | 각 클라이언트(LWT) | `online` / `offline` |

## 2. 입력 키오스크

```jsonc
// → locker/input/register
{ "type": "REGISTER_REQUEST", "request_id": "a1", "client_id": "INPUT-01-3F2A1C",
  "kiosk_id": "INPUT-01", "dong": 101, "ho": 412, "size": "M", "tracking_no": null }

// ← 응답 (인증코드는 포함하지 않음)
{ "type": "REGISTER_RESPONSE", "request_id": "a1", "status": "OK",
  "receipt_no": "20261031-89B536F3", "input_port": "M", "timeout_sec": 60 }
```
- `status`: `OK` | `REJECT`(동·호수·크기·송장번호 오류) | `PORT_FULL`(해당 크기 셀 부족)
- 동 101~106, 호수 101~412(층 1~4, 호 01~12), 송장번호는 선택(영문·숫자 6~40자)

```jsonc
// → locker/input/port_confirm  ([투입 완료] 버튼. 센서를 붙이면 source: "SENSOR")
{ "type": "PORT_DETECTED", "request_id": "a2", "client_id": "INPUT-01-3F2A1C",
  "receipt_no": "20261031-89B536F3", "port": "M", "source": "KIOSK" }

// ← 응답 (today_count: 이 키오스크의 오늘 등록 건수)
{ "type": "REGISTER_CONFIRMED", "request_id": "a2", "status": "OK", "receipt_no": "20261031-89B536F3",
  "date": "2026-10-31", "today_count": 12 }
```
- `status`: `OK` | `CANCELLED`(투입 시간 초과) | `NOT_FOUND` | `PORT_MISMATCH`
- `kiosk_id` 는 키오스크 번호(접속마다 바뀌는 `client_id` 와 별개). 웹은 `/input?id=INPUT-02` 로 바꿀 수 있습니다.

```jsonc
// → locker/input/stats   (화면 연결 시, 그리고 KST 자정마다 요청)
{ "type": "INPUT_STATS_REQUEST", "request_id": "a3", "client_id": "INPUT-01-3F2A1C", "kiosk_id": "INPUT-01" }

// ← 응답
{ "type": "INPUT_STATS_RESPONSE", "request_id": "a3", "status": "OK", "kiosk_id": "INPUT-01",
  "date": "2026-10-31", "today_count": 12 }
```
- 오늘 등록 건수 = 이 키오스크로 접수되어 오늘(KST) **투입까지 완료된** 물품 수. 서버 DB 기준이라 새로고침이나 업무 종료와 관계없이 유지되고, 날짜가 바뀌면 0부터 셉니다.
- 접수 후 60초 안에 투입이 확인되지 않으면 서버가 접수를 취소하고 알림을 보냅니다:
  `{ "type": "REGISTER_CANCELLED", "receipt_no": "…", "reason": "TIMEOUT", "message": "…" }`

## 3. 출력 키오스크

```jsonc
// → locker/auth/login   (아이디 '동-호', 데모 초기 비밀번호 1234)
{ "type": "LOGIN_REQUEST", "request_id": "b1", "client_id": "OUTPUT-01-9A0B1C",
  "user_id": "101-412", "password": "1234" }

// ← 응답
{ "type": "LOGIN_RESPONSE", "request_id": "b1", "status": "OK", "session": "tok_…",
  "household": { "dong": 101, "ho": 412, "default_mode": "ROBOT" },
  "parcels": [ { "receipt_no": "20261031-89B536F3", "size": "M",
                 "arrived_at": "2026-10-31T14:41:17+09:00", "mode": "ROBOT" } ] }
```
- `parcels` 에는 로봇이 아직 가져가지 않은 보관 물품(`STORED`, `JOB_ASSIGNED`)만 들어갑니다. 수령 방식(mode)과는 관계없습니다.
- `status`: `OK` | `DENIED`

```jsonc
// → locker/output/retrieve
{ "type": "RETRIEVE_REQUEST", "request_id": "b2", "client_id": "…", "session": "tok_…",
  "receipt_no": "20261031-89B536F3" }

// ← 응답
{ "type": "RETRIEVE_RESPONSE", "request_id": "b2", "status": "OK",
  "receipt_no": "20261031-89B536F3", "out_port": "PICKUP" }
```
- `status`: `OK` | `ALREADY_LOADED`(이미 로봇에 적재) | `NOT_FOUND` | `EXPIRED_SESSION`
- `locker/output/parcels` (`PARCELS_REQUEST`, `session` 필요) → `PARCELS_RESPONSE` 는 `LOGIN_RESPONSE` 와 같은 `household`, `parcels` 를 돌려줍니다.
- 로그인한 세대의 목록이 바뀌면(로봇 적재, 새 물품 보관 등) 서버가 알립니다:
  `{ "type": "PARCELS_UPDATE", "household": {…}, "parcels": […] }`

## 4. 수령 방식 (세대 웹 · `tools/set_mode.py`)

```jsonc
// → locker/resident/default_mode   (토글: 미적재 물품 일괄 변경)
{ "type": "DEFAULT_MODE_REQUEST", "request_id": "c1", "client_id": "…", "session": "tok_…", "mode": "DIRECT" }
// ← { "type": "DEFAULT_MODE_RESPONSE", "status": "OK", "default_mode": "DIRECT", "changed": 2, "parcels": […] }

// → locker/resident/item_mode      (물품별: 세대 기본값이 ROBOT 일 때만)
{ "type": "ITEM_MODE_REQUEST", "request_id": "c2", "client_id": "…", "session": "tok_…",
  "receipt_no": "20261031-89B536F3", "mode": "DIRECT" }
// ← { "type": "ITEM_MODE_RESPONSE", "status": "OK" | "DENIED" | "NOT_CHANGEABLE" | "NOT_FOUND", … }
```

## 4-1. 세대 웹 (`end_user_wep`)

세대 웹 서버(Node)가 입주민 대신 아래 요청을 보냅니다. 로그인은 출력 키오스크와 같은 `locker/auth/login` 을 쓰되
`"channel": "RESIDENT"` 를 붙이면 세션이 7일 유지됩니다(키오스크는 10분).

```jsonc
// → locker/auth/login
{ "type": "LOGIN_REQUEST", "request_id": "d0", "client_id": "RESIDENT-WEB-1A2B3C",
  "user_id": "101-412", "password": "1234", "channel": "RESIDENT" }

// → locker/resident/parcels  (진행 중 전부 + 최근 14일 안에 끝난 택배, 숨긴 것 제외)
{ "type": "RESIDENT_PARCELS_REQUEST", "request_id": "d1", "client_id": "…", "session": "tok_…" }
// ← 응답
{ "type": "RESIDENT_PARCELS_RESPONSE", "status": "OK",
  "household": { "dong": 101, "ho": 412, "default_mode": "ROBOT" },
  "parcels": [ { "receipt_no": "20261031-89B536F3", "tracking_no": null, "size": "M",
                 "status": "JOB_ASSIGNED", "mode": "ROBOT",
                 "registered_at": "…", "stored_at": "…", "updated_at": "…",
                 "changeable": true } ] }

// → locker/resident/pickup_code  (내 택배 · 보관 중 · 직접 수령일 때만)
{ "type": "PICKUP_CODE_REQUEST", "request_id": "d2", "client_id": "…", "session": "tok_…",
  "receipt_no": "20261031-89B536F3" }
// ← { "type": "PICKUP_CODE_RESPONSE", "status": "OK" | "NOT_AVAILABLE" | "NOT_FOUND", "code": "358204" }

// → locker/resident/hide  (DELIVERED · PICKED 만)
{ "type": "HIDE_REQUEST", "request_id": "d3", "client_id": "…", "session": "tok_…",
  "receipt_no": "20261031-89B536F3" }
// ← { "type": "HIDE_RESPONSE", "status": "OK" | "NOT_COMPLETED" | "NOT_FOUND" }
```

- `changeable` 은 상태 기준(`STORED`, `JOB_ASSIGNED`)입니다. 세대 기본값이 `DIRECT` 이면 물품별 `ROBOT` 변경은 `DENIED` 로 거절됩니다.
- 로봇 현황은 FMS 가 보내는 상태를 세대 웹이 직접 구독합니다. 10초마다 다시 보내고, 30초 넘게 오지 않으면 세대 웹은 "통신 끊김"으로 표시합니다.

```jsonc
// FMS → locker/robot/R1/state  (retained)
{ "robot_id": "R1", "status": "배송 중", "location": "101동 412호", "battery": 88,
  "available": false, "timestamp": "2026-10-31T15:02:30+09:00" }
```

## 5. 서버 ↔ FMS (로봇 시뮬레이터)

FMS 와의 형식은 `server/fms_adapter.py` 한 파일에서만 다룹니다.

```
서버 → DELIVERY_JOB        로봇 배달 대상 물품이 2개 모이면 (예상 묶음, 트레이 2개)
FMS  → ROBOT_ARRIVED       로봇이 보관소 출력구에 도착
서버 → BUNDLE_CONFIRMED    도착 시점에 묶음을 다시 계산해 최종 확정 (또는 JOB_CANCELLED)
FMS  → DELIVERY_RESULT     물품별 결과 (DELIVERED / FAILED)
FMS  → JOB_DONE            로봇 복귀. 서버는 다음 Job 을 검사
```

```jsonc
// 서버 → locker/robot/bundle
{ "type": "BUNDLE_CONFIRMED", "job_id": "J20261031-0017", "robot_id": "R1",
  "items": [
    { "receipt_no": "20261031-5E6F7A8B", "dong": 101, "ho": 305, "size": "S", "tray": "TRAY_A" },
    { "receipt_no": "20261031-89B536F3", "dong": 101, "ho": 412, "size": "L", "tray": "TRAY_B" }
  ],
  "summary": { "count": { "S": 1, "M": 0, "L": 1 },
               "trays": { "TRAY_A": "20261031-5E6F7A8B", "TRAY_B": "20261031-89B536F3" },
               "weight_kg": 25, "dongs": [101], "households": 2 },
  "timestamp": "2026-10-31T15:02:11+09:00" }

// FMS → locker/robot/event
{ "type": "ROBOT_ARRIVED", "job_id": "J20261031-0017", "robot_id": "R1" }
{ "type": "DELIVERY_RESULT", "job_id": "J20261031-0017", "receipt_no": "20261031-89B536F3",
  "result": "DELIVERED" }
{ "type": "JOB_DONE", "job_id": "J20261031-0017", "robot_id": "R1" }
```
- `DELIVERY_JOB` 은 `BUNDLE_CONFIRMED` 와 같은 형식(예상 묶음, `robot_id` 없음)입니다.
- 로봇 적재부: 직사각형 로봇을 둘로 나눈 트레이 `TRAY_A`, `TRAY_B`. 트레이 하나에 물품 하나, 묶음은 정확히 2개다.
  로봇 배달 대상이 1개뿐이면 Job 을 만들지 않고 보관소에서 짝을 기다린다. 트레이 배정은 동·호·접수번호 순이다.
  크기(S/M/L)별 트레이 적합성 기준은 아직 없어 모든 크기를 한 트레이에 싣는다. 무게 상한은 150kg.
- 로봇 도착(`ROBOT_ARRIVED`) 때 다시 2개인지 확인한다. 직접 수령 등으로 1개가 되면 대체 물품을 넣고, 없으면 `JOB_CANCELLED`.
- FMS 는 `BUNDLE_CONFIRMED` 를 받으면 물품마다 FMS 물품 ID(`P-…`)와 묶음 ID(`B-…`)를 부여해 저장한 뒤
  물품별 PICK·DROP 을 실행하고, 복귀 후 픽업스테이션에서 트레이 반납을 확인한다 (`fms_server/cargo.py`).
- 배달 실패 물품은 재입고되고, 2회 실패하면 직접 수령(DIRECT)으로 바뀝니다.
- `JOB_DONE` 때까지 결과가 오지 않은 물품은 실패로 처리합니다.

## 6. 상태 요약 (retained)

```jsonc
// 서버 → locker/state/summary
{ "type": "SUMMARY", "timestamp": "…",
  "occupancy": { "S": { "used": 12, "total": 144, "ratio": 0.083 }, "M": {…}, "L": {…} },
  "status": { "STORED": 9, "JOB_ASSIGNED": 3, "LOADED": 0, "DELIVERED": 21, "PICKED": 4 },
  "waiting": { "ROBOT": 10, "DIRECT": 2 },
  "active_jobs": [ { "job_id": "J20261031-0017", "status": "PENDING" } ] }
```

## 7. 물품 상태

```
REGISTERED ─[투입 확인]→ STORED ─┬─ 출력 키오스크 수령 → PICKED
                                 │   (ROBOT 물품이 2개 모일 때까지 STORED 로 대기)
   └─(60초 초과)→ CANCELLED      └─ ROBOT 모드 · Job 후보 → JOB_ASSIGNED ─(로봇 도착·적재 확정)→ LOADED
                                                                     → DELIVERED / 실패 시 STORED
```
`STORED`, `JOB_ASSIGNED` 까지는 출력 키오스크에서 수령할 수 있습니다.

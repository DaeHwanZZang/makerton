# LastGate MQTT 프로토콜 v4.0

- 모든 메시지는 **JSON(UTF-8)**, **QoS 1**. 예외: `locker/server/online`, `locker/client/{id}/online` 은 일반 문자열 `online`/`offline`.
- 브로커(내부 서버): TCP `1883`(보관소 서버·FMS·세대 웹·관리자 웹·도구), WebSocket `9001`(웹 키오스크)
- 클라이언트 요청에는 `request_id`(요청마다 고유)와 `client_id`(접속마다 고유)를 넣습니다.
- 서버는 응답을 **요청한 클라이언트 전용 토픽** `locker/client/{client_id}/res` 로만 보냅니다.
- 서버는 `(client_id, request_id)` 로 중복 요청을 걸러, 같은 요청이 두 번 와도 한 번만 처리하고 같은 응답을 다시 보냅니다.
- 응답의 `status` 는 `OK` 또는 오류 코드이고, 오류일 때 `message` 에 화면에 그대로 보여 줄 한국어 문구가 들어갑니다.
- **초기 설정 전**에는 관리자 로그인·작업 기록을 뺀 모든 요청이 `NOT_INITIALIZED` 로 거절됩니다.
- `client_id` 접두어로 권한을 구분합니다: `RESIDENT-WEB-`(세대 웹 서버 — `channel: "RESIDENT"` 로그인에 필수), `ADMIN-WEB-`(관리자 웹 서버 — 모든 `locker/admin/*` 에 필수).

> v3.3 → v4.0 바뀐 점: 동·호 고정(101~106동) 대신 관리자 초기 설정의 건물·호실, 크기(S/M/L) 제거와 보관함 1칸 = 물품 1개,
> 규격 초과·취급 주의 물품은 직접 수령 전용, 배송 시간 예약(10분 칸 · 2자리 · 선착순), 세대 알림, 관리자 토픽 추가.
> 요청 필드 `dong`/`ho` 는 `building`/`unit` 의 옛 이름으로 계속 받습니다.

## 1. 토픽

| 토픽 | 발행 → 구독 | 메시지 |
|---|---|---|
| `locker/config/site` (retained) | 서버 → 전체 | `SITE_CONFIG` 현장 설정 (바뀔 때마다 `version` 증가) |
| `locker/input/register` | 입력 키오스크 → 서버 | `REGISTER_REQUEST` |
| `locker/input/port_confirm` | 입력 키오스크(또는 센서) → 서버 | `PORT_DETECTED` |
| `locker/input/stats` | 입력 키오스크 → 서버 | `INPUT_STATS_REQUEST` (오늘 등록 건수) |
| `locker/auth/login` · `logout` · `password` | 출력 키오스크·세대 웹 → 서버 | 세대 로그인·로그아웃·비밀번호 변경 |
| `locker/output/parcels` · `retrieve` | 출력 키오스크 → 서버 | 보관 물품 조회·수령 |
| `locker/resident/default_mode` · `item_mode` | 세대 웹 · `tools/set_mode.py` → 서버 | 수령 방식 |
| `locker/resident/parcels` | 세대 웹 → 서버 | 택배 목록(이력 포함) |
| `locker/resident/pickup_code` · `hide` | 세대 웹 → 서버 | 인증코드 보기 · 완료 택배 숨김 |
| `locker/resident/slots` · `reserve` · `reserve_cancel` | 세대 웹 → 서버 | 배송 시간 예약 |
| `locker/resident/notifications` | 세대 웹 → 서버 | 알림 목록 · 읽음 처리 |
| `locker/resident/slots_update` | 서버 → 세대 웹 | `SLOTS_UPDATE` 예약 칸 남은 자리 변동 |
| `locker/admin/*` | 관리자 웹 → 서버 | 8장 참고 |
| `locker/robot/{robot_id}/state` (retained) | FMS → 세대 웹 | 로봇 상태 (서버는 사용하지 않음) |
| `locker/client/{client_id}/res` | 서버 → 해당 클라이언트 | 모든 응답, `PARCELS_UPDATE`, `NOTIFICATION`, `REGISTER_CANCELLED` |
| `locker/state/summary` (retained) | 서버 → 전체 | `SUMMARY` (상태가 바뀔 때마다 + 10초 하트비트) |
| `locker/storage/log` | 서버 → 전체 | 보관함 동작 로그 (`STORE`/`RETRIEVE`/`LOAD_ROBOT`/`RETURN`) |
| `locker/robot/job` | 서버 → FMS | `DELIVERY_JOB` |
| `locker/robot/bundle` | 서버 → FMS | `BUNDLE_CONFIRMED`, `JOB_CANCELLED` |
| `locker/robot/event` | FMS → 서버 | `ROBOT_ARRIVED`, `DELIVERY_RESULT`, `JOB_DONE`, `JOB_REJECTED` |
| `locker/client/{id}/online`, `locker/server/online` (retained) | 각 클라이언트(LWT) | `online` / `offline` |

## 2. 현장 설정 (retained)

```jsonc
// 서버 → locker/config/site
{ "type": "SITE_CONFIG", "initialized": true, "version": 3, "timestamp": "…",
  "site": {
    "name": "가천대 글로벌캠퍼스", "building_term": "건물", "unit_term": "호실",
    "buildings": [
      { "code": 1, "name": "가천관", "unit_from": 101, "unit_to": 406, "missing": "304", "extra": "501",
        "guide": "101~406호실 · 없는 호실: 304호실 · 추가 호실: 501호실",
        "units": [101, 102, 103, 104, 105, 106, 201, …, 406, 501] } ],
    "grid_bins": 300, "oversize_doors": 20, "robots": 1,
    "service_start": "08:00", "service_end": "22:00", "second_seat_same_building": false,
    "grid_limit_mm": [600, 400, 220], "version": 3, "updated_at": "…" } }
// 초기 설정 전: { "type": "SITE_CONFIG", "initialized": false, "version": 0, "site": null }
```
- 건물 `code`(1~999)는 세대 계정 아이디 앞부분이라 바꿀 수 없습니다. 호실은 `층×100+호` 범위에서 `missing` 을 빼고 `extra` 를 더한 목록입니다.
- 키오스크·세대 웹은 `units` 로 입력을 검사하고(없는 호실로만 이어지는 숫자 키는 비활성), `guide` 를 안내 문구로 씁니다.
- 클라이언트는 `version` 이 바뀌면 **사용자가 입력 중이 아닐 때** 화면을 새로 불러옵니다.

## 3. 입력 키오스크

```jsonc
// → locker/input/register
{ "type": "REGISTER_REQUEST", "request_id": "a1", "client_id": "INPUT-01-3F2A1C",
  "kiosk_id": "INPUT-01", "building": 1, "unit": 101, "area": "GRID", "fragile": false, "tracking_no": null }

// ← 응답 (인증코드는 포함하지 않음)
{ "type": "REGISTER_RESPONSE", "request_id": "a1", "status": "OK",
  "receipt_no": "20261101-89B536F3", "storage_area": "GRID", "space_id": "G-012", "door": null,
  "mode": "ROBOT", "direct_lock": null, "label": "가천관 101호실", "input_port": "GRID", "timeout_sec": 60,
  "message": "입력구에 올라온 보관함에 넣어주세요." }
```
- `area`: `GRID`(일반 · 입력구 틀 600×400×220mm 안) | `OVERSIZE`(규격 초과 → 배송 불가 보관함 문 하나)
- `fragile: true` 또는 `OVERSIZE` 이면 **직접 수령 전용**(`direct_lock` = `FRAGILE`/`OVERSIZE`, `mode` = `DIRECT`)으로 접수합니다.
- 그리드 보관함 1칸, 배송 불가 보관함 문 1개에 물품 1개만 넣습니다. 가장 낮은 번호의 빈 칸을 줍니다(`G-001`…, `OV-01`…).
- `status`: `OK` | `REJECT`(없는 건물·호실, 송장번호 형식) | `STORAGE_FULL` | `OVERSIZE_FULL` | `NOT_INITIALIZED`
- `OVERSIZE` 응답은 `door`(문 번호)와 `"배송 불가 보관함 03번 문이 열렸습니다."` 문구를 돌려줍니다.

```jsonc
// → locker/input/port_confirm  ([투입 완료] 버튼. 센서를 붙이면 source: "SENSOR")
{ "type": "PORT_DETECTED", "request_id": "a2", "client_id": "INPUT-01-3F2A1C",
  "receipt_no": "20261101-89B536F3", "port": "GRID", "source": "KIOSK" }
// ← { "type": "REGISTER_CONFIRMED", "status": "OK", "receipt_no": "…", "date": "2026-11-01", "today_count": 12 }
```
- `status`: `OK` | `CANCELLED`(투입 시간 초과) | `NOT_FOUND` | `PORT_MISMATCH`
- 투입이 확인되면 보관 완료(`STORED`)이고, 세대에 도착 알림(`ARRIVED_CHOICE`/`ARRIVED_DIRECT`)이 갑니다.
- `locker/input/stats` → `INPUT_STATS_RESPONSE { kiosk_id, date, today_count }`: 이 키오스크로 오늘(KST) 투입까지 끝난 건수.
- 접수 후 60초 안에 투입이 확인되지 않으면 서버가 접수를 취소하고 알립니다:
  `{ "type": "REGISTER_CANCELLED", "receipt_no": "…", "reason": "TIMEOUT", "message": "…" }`

## 4. 세대 로그인·비밀번호 (출력 키오스크 · 세대 웹 공통)

```jsonc
// → locker/auth/login   (건물·호실 + 비밀번호. 옛 형식 user_id "1-101" 도 받음)
{ "type": "LOGIN_REQUEST", "request_id": "b1", "client_id": "OUTPUT-01-9A0B1C",
  "building": 1, "unit": 101, "password": "1234" }

// ← 응답
{ "type": "LOGIN_RESPONSE", "request_id": "b1", "status": "OK", "session": "tok_…", "ttl_sec": 600,
  "must_change": true,
  "household": { "building": 1, "building_name": "가천관", "unit": 101, "label": "가천관 101호실",
                 "user_id": "1-101", "default_mode": "ROBOT", "must_change": true },
  "parcels": [ { "receipt_no": "…", "arrived_at": "…", "mode": "ROBOT", "status": "STORED",
                 "storage_area": "GRID", "space_id": "G-012", "door": null, "fragile": false,
                 "direct_lock": null, "plan": "CHOICE_OPEN", "slot_start": null } ] }
```
- 세대 계정은 초기 설정 때 호실마다 만들어집니다. 아이디 `{건물코드}-{호실}`, 초기 비밀번호 `1234`, `must_change: true`.
- `status`: `OK` | `DENIED`(+`remaining` 남은 시도) | `LOCKED`(5번 틀리면 5분 잠금, `retry_after_sec`)
- `"channel": "RESIDENT"` 를 붙이면 세션이 7일(키오스크는 10분) 유지됩니다. `client_id` 가 `RESIDENT-WEB-` 으로 시작해야 합니다.
- `locker/auth/logout { session }` → `LOGOUT_RESPONSE`

```jsonc
// → locker/auth/password
{ "type": "PASSWORD_REQUEST", "request_id": "b2", "client_id": "…", "session": "tok_…",
  "current_password": "1234", "new_password": "5807" }
// ← { "type": "PASSWORD_RESPONSE", "status": "OK", "revoked": 1, "household": {…, "must_change": false} }
```
- 새 비밀번호: 숫자 4~8자리, `1234`·현재 비밀번호 불가(`INVALID`). 현재 비밀번호가 틀리면 `DENIED`.
- 바꾸면 같은 세대의 **다른 세션은 모두 종료**됩니다(`revoked` = 종료한 세션 수).

## 5. 출력 키오스크

```jsonc
// → locker/output/retrieve
{ "type": "RETRIEVE_REQUEST", "request_id": "b3", "client_id": "…", "session": "tok_…", "receipt_no": "…" }
// ← { "type": "RETRIEVE_RESPONSE", "status": "OK", "receipt_no": "…",
//     "storage_area": "OVERSIZE", "space_id": "OV-03", "door": 3, "out_port": "OVERSIZE" }
```
- `parcels` 에는 로봇이 아직 싣지 않은 보관 물품(`STORED`, `JOB_ASSIGNED`)이 모두 들어갑니다. 배송 예약·자동 배송 확정 물품도 꺼낼 수 있고, 꺼내면 예약 자리가 풀립니다.
- `status`: `OK` | `ALREADY_LOADED`(이미 로봇에 적재) | `NOT_FOUND` | `EXPIRED_SESSION`
- `locker/output/parcels { session }` → `PARCELS_RESPONSE { household, parcels }` (로그인 응답과 같은 형식)
- 로그인한 세대의 목록이 바뀌면 서버가 알립니다: `{ "type": "PARCELS_UPDATE", "household": {…}, "parcels": […] }`

## 6. 세대 웹 (`end_user_wep`)

세대 웹 서버(Node BFF)가 입주민 대신 아래 요청을 보냅니다. 브라우저 ↔ 세대 웹 서버 REST API 는 [세대 웹 안내](end_user_wep/README.md)에 있습니다.

### 6-1. 택배 목록·수령 방식

```jsonc
// → locker/resident/parcels  (진행 중 전부 + 최근 14일 안에 끝난 택배. 숨긴 것·입주 전 것 제외)
{ "type": "RESIDENT_PARCELS_REQUEST", "request_id": "d1", "client_id": "RESIDENT-WEB-1A2B3C", "session": "tok_…" }
// ← 응답
{ "type": "RESIDENT_PARCELS_RESPONSE", "status": "OK", "server_time": "…", "unread": 2,
  "household": {…},
  "parcels": [ { "receipt_no": "…", "tracking_no": null, "status": "STORED", "mode": "ROBOT",
                 "plan": "RESERVED", "storage_area": "GRID", "door": null, "fragile": false, "direct_lock": null,
                 "choice_deadline": null,
                 "slot_start": "2026-11-01T19:10:00+09:00", "slot_end": "…T19:20:00+09:00",
                 "slot_label": "19:10~19:20", "change_until": "…T18:50:00+09:00",
                 "robot_id": null, "registered_at": "…", "stored_at": "…", "updated_at": "…",
                 "fixed": false, "changeable": true, "reservable": true } ] }
```
- `plan`(로봇 배송 물품만): `CHOICE_OPEN`(보관 후 30분 동안 시간 선택 가능, `choice_deadline`) → `RESERVED`(예약됨) 또는 `AUTO`(자동 배송 확정). 직접 수령 물품은 `null`.
- `fixed`: 자동 배송 확정·예약 변경 마감(배송 20분 전)·배송 발행(`JOB_ASSIGNED`) 이후. 이때는 수령 방식을 바꿀 수 없고 출력 키오스크에서 꺼내는 것만 됩니다.
- `changeable`: 수령 방식을 바꿀 수 있음. `reservable`: 예약하거나 예약을 바꿀 수 있음.

```jsonc
// → locker/resident/default_mode   (세대 기본값. 바꿀 수 있는 보관 물품도 함께 바꿈)
{ "type": "DEFAULT_MODE_REQUEST", "request_id": "c1", "client_id": "…", "session": "tok_…", "mode": "DIRECT" }
// ← { "type": "DEFAULT_MODE_RESPONSE", "status": "OK", "default_mode": "DIRECT", "changed": 2, "skipped": 1, "parcels": […] }

// → locker/resident/item_mode      (물품별)
{ "type": "ITEM_MODE_REQUEST", "request_id": "c2", "client_id": "…", "session": "tok_…",
  "receipt_no": "…", "mode": "ROBOT" }
// ← { "type": "ITEM_MODE_RESPONSE", "status": "OK", "receipt_no": "…", "mode": "ROBOT", "parcel": {…} }
```
- `skipped`: 직접 수령 전용·배송 확정이라 바꾸지 않은 물품 수.
- `item_mode` 오류: `LOCKED_DIRECT`(직접 수령 전용을 로봇으로) | `DENIED`(세대 기본값이 DIRECT 인데 로봇으로) | `NOT_CHANGEABLE` | `FIXED` | `NOT_FOUND`
- 로봇으로 바꾸면 보관 후 30분이 안 지났으면 `CHOICE_OPEN`, 지났으면 바로 `AUTO`. 직접 수령으로 바꾸면 예약 자리가 풀립니다.

### 6-2. 배송 시간 예약

```jsonc
// → locker/resident/slots
{ "type": "SLOTS_REQUEST", "request_id": "e1", "client_id": "…", "session": "tok_…" }
// ← 응답 (지금부터 20분 뒤 ~ 24시간 안, 배송 시간대 안의 10분 칸)
{ "type": "SLOTS_RESPONSE", "status": "OK", "server_time": "…", "service_start": "08:00", "service_end": "22:00",
  "seats_per_slot": 2,
  "slots": [ { "start": "…T19:10:00+09:00", "end": "…T19:20:00+09:00", "label": "19:10~19:20",
               "seats_left": 1, "available": 1, "restricted": false, "mine": 1, "mine_receipts": ["…"] } ] }

// → locker/resident/reserve   (1~2개. 2개는 모두 잡히거나 모두 실패)
{ "type": "RESERVE_REQUEST", "request_id": "e2", "client_id": "…", "session": "tok_…",
  "slot_start": "2026-11-01T19:10:00+09:00", "receipt_nos": ["…", "…"] }
// ← { "type": "RESERVE_RESPONSE", "status": "OK", "slot_start": "…", "slot_end": "…", "label": "19:10~19:20",
//     "receipt_nos": [...], "parcels": [...] }
// ← 자리 부족: { "status": "SLOT_FULL", "seats_left": 1, "message": "방금 다른 세대가 예약해 자리가 부족합니다. …" }

// → locker/resident/reserve_cancel
{ "type": "RESERVE_CANCEL_REQUEST", "request_id": "e3", "client_id": "…", "session": "tok_…", "receipt_no": "…" }
// ← { "type": "RESERVE_CANCEL_RESPONSE", "status": "OK", "receipt_no": "…", "plan": "CHOICE_OPEN" | "AUTO", "parcels": [...] }
```
- 칸마다 **2자리, 선착순**. 한 세대가 2자리를 모두 써도 되고, 서로 다른 두 세대가 1자리씩 써도 됩니다.
- 이미 예약한 물품을 다른 칸으로 옮기면 원래 자리는 풀립니다. 같은 칸에 이미 잡은 내 자리는 다시 쓰입니다.
- `second_seat_same_building: true` 이면 다른 건물 세대가 먼저 잡은 칸은 같은 건물만 예약할 수 있습니다(`restricted`).
- 오류: `SLOT_UNAVAILABLE`(예약 가능 범위 밖) | `SLOT_FULL`(+`seats_left`) | `LOCKED_DIRECT` | `NOT_ROBOT` | `NOT_CHANGEABLE` | `FIXED` | `TOO_LATE`(배송 20분 전부터 변경·취소 불가) | `NOT_RESERVED` | `INVALID` | `NOT_FOUND`
- 취소하면 보관 후 30분 안이면 `CHOICE_OPEN` 으로, 지났으면 `AUTO` 로 돌아갑니다.
- 자리가 바뀔 때마다 서버가 전체에 알립니다(세대 웹은 이를 SSE 로 브라우저에 전달):
  `locker/resident/slots_update` ← `{ "type": "SLOTS_UPDATE", "timestamp": "…", "slots": [ { "start": "…", "seats_left": 0 } ] }`

### 6-3. 인증코드·숨김·알림

```jsonc
// → locker/resident/pickup_code  (내 택배 · 보관 중 · 직접 수령일 때만)
// ← { "type": "PICKUP_CODE_RESPONSE", "status": "OK" | "NOT_AVAILABLE" | "NOT_FOUND", "code": "358204" }

// → locker/resident/hide  (DELIVERED · PICKED 만)
// ← { "type": "HIDE_RESPONSE", "status": "OK" | "NOT_COMPLETED" | "NOT_FOUND" }

// → locker/resident/notifications   (read: "all" 또는 알림 id 목록. 없으면 조회만)
{ "type": "NOTIFICATIONS_REQUEST", "request_id": "f1", "client_id": "…", "session": "tok_…", "read": [12, 13] }
// ← { "type": "NOTIFICATIONS_RESPONSE", "status": "OK", "unread": 0,
//     "notifications": [ { "id": 13, "kind": "DEPARTED", "title": "로봇이 출발했어요", "body": "…",
//                          "receipt_no": "…", "created_at": "…", "read_at": "…" } ] }
```
- 알림은 세대마다 최근 50개를 보관하고, 입주자 변경 이전 알림은 보이지 않습니다.
- 알림이 생기면 서버가 그 세대의 세대 웹 세션 `client_id` 로 바로 보냅니다:
  `{ "type": "NOTIFICATION", "household": { "building": 1, "unit": 101 }, "notification": {…} }`

| `kind` | 시점 |
|---|---|
| `ARRIVED_CHOICE` | 로봇 배송 택배 보관 완료 (예약 마감 시각 안내) |
| `ARRIVED_DIRECT` | 직접 수령 택배 보관 완료 (규격 초과는 문 번호 안내) |
| `CHOICE_REMINDER` | 시간 선택 마감 10분 전 |
| `AUTO_FIXED` | 시간 선택 마감 → 자동 배송 확정 (배달 실패 후 재배송도 포함) |
| `DELAYED` | 예약 칸이 시작됐는데 로봇이 아직 출발하지 않음 |
| `DEPARTED` | 로봇 적재 확정·출발 |
| `DELIVERED` / `FAILED` | 배달 완료 / 실패 (2회 실패하면 직접 수령 전용) |

- 로봇 현황은 FMS 가 보내는 상태를 세대 웹이 직접 구독하고, **내 택배를 싣고 있는 로봇만** 보여 줍니다. 30초 넘게 상태가 오지 않으면 "통신 끊김".

```jsonc
// FMS → locker/robot/R1/state  (retained)
{ "robot_id": "R1", "status": "배송 중", "location": "가천관 1층", "battery": 88,
  "available": false, "timestamp": "…" }
```

## 7. 배송 (서버 ↔ FMS)

FMS 와의 형식은 `server/fms_adapter.py` 한 파일에서만 다룹니다.

```
서버 → DELIVERY_JOB        배송 발행 (예상 묶음, 항상 2개)
FMS  → ROBOT_ARRIVED       로봇이 보관소 출력구에 도착
서버 → BUNDLE_CONFIRMED    도착 시점에 남은 물품으로 최종 확정 (항상 2개) — 2개를 못 채우면 JOB_CANCELLED
FMS  → DELIVERY_RESULT     물품별 결과 (DELIVERED / FAILED)
FMS  → JOB_DONE            로봇 복귀. 서버는 다음 배송을 검사
FMS  → JOB_REJECTED        로봇 배정 전 운영자 취소 (세대 목적지 연결 없음 등). 서버는 다음 배송을 검사
```

배송 발행 규칙 (로봇 대수 = 동시에 진행하는 배송 수). **물품이 2개 모여야 발행·출발합니다** (`config.BUNDLE_MIN`):
1. **예약 칸**: 칸 시작 15분 전에 그 칸 물품(1~2개)으로 발행. 1개뿐이면 짝 하나를 함께 싣습니다.
   짝 후보는 자동 배송 물품과 발행 시각이 지난 다른 칸의 예약 물품이고, 같은 세대 → 같은 층 → 같은 건물 → 가까운 건물 순으로 고릅니다.
   예약 세대를 먼저 들릅니다. 짝이 없으면 발행하지 않고 기다립니다.
2. **자동 배송(`AUTO`)**: 2개를 짝지어 발행(같은 건물·가까운 층 우선). 짝이 없으면 발행하지 않고 기다립니다.
3. 30분 안에 출발할 예약이 있으면 마지막 로봇은 자동 배송에 쓰지 않습니다.
4. 늦은 확정: 로봇 도착 때 직접 수령 등으로 빠진 물품은 싣지 않습니다. 1개 남으면 위 짝 후보로 채워 2개로 출발하고,
   채우지 못하면 `JOB_CANCELLED { reason: "NOT_ENOUGH" }`, 0개면 `NO_ITEMS`. 남은 물품은 보관 중으로 돌아갑니다.
   로봇 도착 전에 모두 빠지면 바로 `JOB_CANCELLED { reason: "EMPTIED" }` 를 보냅니다. 1개만 빠지면 도착 때 채웁니다.

```jsonc
// 서버 → locker/robot/bundle
{ "type": "BUNDLE_CONFIRMED", "job_id": "J20261101-0017", "robot_id": "R1",
  "items": [
    { "receipt_no": "…5E6F", "dong": 1, "ho": 305, "building": 1, "building_name": "가천관", "unit": 305, "floor": 3, "tray": "TRAY_A" },
    { "receipt_no": "…89B5", "dong": 1, "ho": 412, "building": 1, "building_name": "가천관", "unit": 412, "floor": 4, "tray": "TRAY_B" }
  ],
  "summary": { "count": 2, "trays": { "TRAY_A": "…5E6F", "TRAY_B": "…89B5" },
               "buildings": [1], "households": 2, "kind": "AUTO" | "RESERVED", "slot_start": null },
  "timestamp": "…" }
// 서버 → locker/robot/bundle
{ "type": "JOB_CANCELLED", "job_id": "J20261101-0018", "reason": "NO_ITEMS" | "NOT_ENOUGH" | "EMPTIED", "timestamp": "…" }

// FMS → locker/robot/event
{ "type": "ROBOT_ARRIVED", "job_id": "J20261101-0017", "robot_id": "R1" }
{ "type": "DELIVERY_RESULT", "job_id": "J20261101-0017", "receipt_no": "…", "result": "DELIVERED" }
{ "type": "JOB_DONE", "job_id": "J20261101-0017", "robot_id": "R1" }
{ "type": "JOB_REJECTED", "job_id": "J20261101-0019", "reason": "운영자 취소: 세대 1-305 목적지 연결이 없다",
  "unroutable": ["20261101-630EBBF4"] }
```
- `DELIVERY_JOB` 은 `BUNDLE_CONFIRMED` 와 같은 형식(예상 묶음, `robot_id` 없음)입니다.
- `dong`/`ho` 는 기존 FMS 호환용으로 `building`/`unit` 과 같은 값입니다. 크기·무게 필드는 v4.0 에서 빠졌습니다.
- 로봇 적재부: 트레이 `TRAY_A`, `TRAY_B`. 트레이 하나에 보관함(물품) 하나, 묶음은 정확히 2개. 트레이 순서 = 방문 순서.
- `JOB_REJECTED`는 로봇 배정 전(`PENDING`) Job 에만 적용합니다. Job 은 `CANCELLED`, 물품은 보관 중(`STORED`)으로 돌아갑니다.
  `unroutable`(FMS 에 세대 목적지 연결이 없는 물품)은 직접 수령(`DIRECT`, 고정 아님)으로 바꿔 같은 묶음이 다시 발행되어 멈추지 않게 하고,
  나머지 물품으로 다음 배송을 검사합니다. 적재 확정(`CONFIRMED`) 뒤에 온 `JOB_REJECTED`는 무시합니다.
  FMS 는 취소한 Job 의 `DELIVERY_JOB` 재통지를 받으면 같은 `JOB_REJECTED`를 다시 보냅니다.
- 배달 실패 물품은 보관함째 돌아와 `JOB_DONE` 때 보관 중으로 되돌리고 자동 배송으로 다시 보냅니다. 2회 실패하면 직접 수령 전용(`direct_lock: FAILED`).
- `JOB_DONE` 때까지 결과가 오지 않은 물품은 실패로 처리합니다. 배달 완료 물품의 보관함은 `JOB_DONE` 때 비웁니다.
- 중복 `ROBOT_ARRIVED` 나 이미 취소한 Job 의 도착에는 같은 `BUNDLE_CONFIRMED`/`JOB_CANCELLED` 를 다시 보냅니다.

## 8. 관리자 (`admin_web`)

관리자 웹 서버(Node BFF, `127.0.0.1:4190`)만 보냅니다. `client_id` 가 `ADMIN-WEB-` 으로 시작하지 않으면 `DENIED`.
로그인·로그아웃 외에는 모두 `session`(관리자 토큰, 30분 무활동 만료)이 필요하고, 응답 `type` 은 `ADMIN_…_RESPONSE` 입니다.

| 토픽 | 요청 필드 | 응답 (OK) |
|---|---|---|
| `locker/admin/login` | `admin_id`(기본 `admin`), `password` | `session`, `ttl_sec`, `default_password`, `initialized`, `site_name` |
| `locker/admin/logout` | `session` | — |
| `locker/admin/password` | `current_password`, `new_password` (영문+숫자 8자 이상) | `revoked` |
| `locker/admin/site/get` | — | `site`(전체), `public`, `defaults`, `accounts`(통계), `active_items`, `occupancy` |
| `locker/admin/site/preview` | `site` | `account_count`, `buildings[{code,name,count,guide}]`, `diff{added,removed,blocked}`(설정 후) |
| `locker/admin/site/init` | `site` | `site`, `accounts` (계정 생성 수) |
| `locker/admin/site/update` | `site` | `site`, `added`, `removed` |
| `locker/admin/site/reset` | `password`, `confirm_name`(사이트 이름 그대로) | — |
| `locker/admin/accounts/list` | `building`, `q`(호실 검색) | `accounts[]`, `stats`, `buildings` |
| `locker/admin/accounts/reset_password` | `user_id` | `revoked` (비밀번호 1234, must_change) |
| `locker/admin/accounts/change_occupant` | `user_id` | `revoked` (1234 · 기본 ROBOT · 이전 기록 숨김) |
| `locker/admin/accounts/unlock` | `user_id` | — |
| `locker/admin/storage` | — | `occupancy`, `waiting`, `active_jobs`, `items[]` |
| `locker/admin/code_pickup` | `code`(6자리), `confirm` | `confirmed`, `item{label, storage_area, space_id, door, …}` |
| `locker/admin/events` | `limit`(≤500), `all` | `events[]` (기본 `ADMIN_*` 작업 기록만) |

```jsonc
// site (init · update · preview 공통)
{ "name": "가천대 글로벌캠퍼스", "building_term": "건물", "unit_term": "호실",
  "buildings": [ { "code": 1, "name": "가천관", "unit_from": 101, "unit_to": 406, "missing": "304", "extra": "501" } ],
  "grid_bins": 300, "oversize_doors": 20, "robots": 1,
  "service_start": "08:00", "service_end": "22:00", "second_seat_same_building": false }
```
- 관리자 계정은 서버 첫 실행 때 `admin` / `admin1234`(환경변수 `LASTGATE_ADMIN_PASSWORD` 가 있으면 그 값)로 만들어집니다. 기본 비밀번호면 `default_password: true`.
- 관리자 로그인도 5번 틀리면 5분 잠깁니다(`LOCKED`).
- 오류: `INVALID`(설정 검증 실패 — `message` 에 어느 칸인지) | `ALREADY_INITIALIZED` | `NOT_INITIALIZED` |
  `BLOCKED`(+`blocked[]`: 보관·배송 중 물품이 있는 호실 제외, 사용 중인 칸보다 보관함 수 줄이기, 보관 물품이 있는데 전체 재설정) |
  `MISMATCH`(전체 재설정 이름 확인) | `DENIED` | `NOT_FOUND` | `ALREADY_LOADED` | `EXPIRED_SESSION`
- 설정 변경: 빠진 호실의 계정은 비활성화(로그인 불가), 다시 생긴 호실은 새 입주자로 초기화, 새 호실은 계정 생성. 저장할 때마다 `SITE_CONFIG` 의 `version` 이 오릅니다.
- 건물 번호를 바꾸는 것은 "기존 건물 삭제 + 새 건물 추가"로 처리됩니다.
- 전체 재설정: 현장 설정·세대 계정·물품 기록·세대 세션을 지웁니다. 작업 기록(`events`)과 관리자 계정은 남습니다.
- 모든 관리자 작업은 `ADMIN_*` 이벤트(`ADMIN_LOGIN`, `ADMIN_SITE_INIT`, `ADMIN_RESET_PASSWORD`, `ADMIN_CODE_PICKUP` …)로 기록됩니다.

## 9. 상태 요약 (retained)

```jsonc
// 서버 → locker/state/summary
{ "type": "SUMMARY", "timestamp": "…", "initialized": true, "site_version": 3,
  "occupancy": { "grid": { "used": 27, "on_robot": 2, "total": 300, "ratio": 0.09 },
                 "oversize": { "used": 11, "total": 20, "ratio": 0.55 } },
  "status": { "STORED": 9, "JOB_ASSIGNED": 1, "LOADED": 2, "DELIVERED": 21, "PICKED": 4 },
  "waiting": { "ROBOT": 10, "DIRECT": 2 }, "robots": 1,
  "active_jobs": [ { "job_id": "J20261101-0017", "status": "PENDING" } ] }
```
- `grid.used` 에는 로봇에 실려 나간 보관함(`on_robot`)도 들어갑니다. 보관함은 `JOB_DONE` 으로 돌아올 때까지 사용 중입니다.

## 10. 물품 상태

```
REGISTERED ─[투입 확인]→ STORED ─┬─ 출력 키오스크·관리자 코드 수령 → PICKED
   └─(60초 초과)→ CANCELLED       │
                                  └─ ROBOT · 배송 발행 → JOB_ASSIGNED ─(로봇 도착·적재 확정)→ LOADED
                                                                   → DELIVERED / 실패 시 STORED

로봇 배송 물품의 plan:  CHOICE_OPEN ─(30분 경과)→ AUTO
                          └─[예약]→ RESERVED ─[취소]→ CHOICE_OPEN 또는 AUTO
```
`STORED`, `JOB_ASSIGNED` 까지는 출력 키오스크에서 수령할 수 있습니다.

## 11. 운영 시 MQTT 접근 제어 (권장)

시연 환경은 익명 접속입니다. 실제 설치에서는 브로커 계정·ACL 로 `client_id` 접두어 권한을 실제로 강제하세요(서버의 접두어 검사는 보조 수단입니다).

| 계정 | 발행 허용 | 구독 허용 |
|---|---|---|
| `locker-server` | `locker/#` | `locker/#` |
| `kiosk` (WebSocket) | `locker/input/#`, `locker/auth/#`, `locker/output/#` | `locker/client/+/res`(자기 것), `locker/config/site`, `locker/state/summary`, `locker/server/online` |
| `resident-web` | `locker/auth/#`, `locker/resident/#` | `locker/client/RESIDENT-WEB-+/res`, `locker/config/site`, `locker/resident/slots_update`, `locker/robot/+/state`, `locker/server/online` |
| `admin-web` | `locker/admin/#` | `locker/client/ADMIN-WEB-+/res`, `locker/config/site`, `locker/server/online` |
| `fms` | `locker/robot/event`, `locker/robot/+/state` | `locker/robot/job`, `locker/robot/bundle` |

브로커 계정은 `LOCKER_BROKER_USER` / `LOCKER_BROKER_PASS` 환경변수로 넣습니다(보관소 서버·세대 웹·관리자 웹 공통).

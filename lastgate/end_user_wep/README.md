# LastGate | 세대 웹 (입주민용, v4.0)

입주민 휴대폰 화면입니다. 기능은 다음과 같습니다.
- 내 택배 확인
- 배송 시간 예약(10분 칸 · 2자리 · 선착순)
- 수령 방식 변경(세대 기본값 · 택배별)
- 직접 수령 인증코드
- 알림
- 내 택배를 싣고 있는 로봇의 현황
- 비밀번호 변경

> 택배 데이터는 이 폴더가 아니라 **보관소 서버(`../server`)** 에 있습니다.
> 이 Node 서버(BFF)는 브라우저의 REST 요청을 받아 MQTT로 보관소 서버에 전달하는 **중계 서버**이고, 세션을 따로 저장하지 않습니다.
> 전체 구조는 [LastGate README](../README.md)에, 메시지 규격은 [protocol.md](../protocol.md) 6장에 있습니다.

```
입주민 휴대폰 ─ HTTP·SSE ─▶ [세대 웹 서버 (이 폴더, :4180)] ─ MQTT 1883 ─▶ [브로커] ◀─ [보관소 서버]
                                        ▲                                     ▲
                                        └──── 로봇 상태 구독 ───────────────── [FMS]
```

## 1. 실행

- Node.js 20.6 이상. 공통 MQTT 클라이언트는 `../shared/locker-client.mjs` 를 씁니다.
- 브로커와 보관소 서버가 먼저 떠 있어야 하고, 관리자 초기 설정이 끝나야 로그인할 수 있습니다.

**`lastgate/end_user_wep/`** 에서 실행합니다.

```bash
npm ci
```

```bash
npm start
```

| 주소 | 용도 |
|---|---|
| `http://127.0.0.1:4180/` | 세대 웹 |
| `http://127.0.0.1:4180/api/health` | 서버·보관소 연결 확인 (`locker: connected`, `server: online`) |

**로그인**: 건물을 고르고(건물이 하나뿐이면 선택 칸이 숨겨짐) 호실과 비밀번호를 입력합니다.
계정은 관리자 초기 설정 때 호실마다 만들어지고, 출력 키오스크와 같은 계정입니다. 초기 비밀번호는 `1234` 이고, 바꾸기 전까지 화면 위쪽에 안내가 나옵니다.
회원가입은 없습니다.

설정은 환경변수로 바꿉니다. `.env.example` 을 `.env` 로 복사한 뒤 `npm run start:env` 로 실행해도 됩니다.

| 변수 | 기본값 | 의미 |
|---|---|---|
| `PORT` | `4180` | 웹 키오스크(Vite, 4173)와 겹치지 않게 4180 |
| `HOST` | `127.0.0.1` | 같은 와이파이의 휴대폰에서 열려면 `0.0.0.0` |
| `LOCKER_BROKER_HOST` / `LOCKER_BROKER_PORT` | `localhost` / `1883` | 보관소 서버와 같은 브로커 |
| `LOCKER_BROKER_URL` | — | 브로커 전체 주소 (예: `mqtt://192.168.0.50:1883`) |
| `LOCKER_BROKER_USER` / `LOCKER_BROKER_PASS` | — | 브로커 계정 (운영 시) |
| `RESIDENT_WEB_ID` | `hostname:PORT` | MQTT `client_id` 고정 부분. 재시작해도 같은 id 로 접속해야 서버 알림이 이어집니다. 세대 웹 서버를 여러 대 띄우면 서로 다르게 |

## 2. 화면 동작

- **내 택배**: 진행 중 택배 전부와 최근 14일 안에 끝난 택배를 보여 줍니다. 끝난 택배는 삭제(숨김)할 수 있습니다.
  - 로봇 배송 택배는 보관 후 30분 동안 **배송 시간 고르기** / **직접 수령으로 바꾸기**를 할 수 있고, 남은 시간이 표시됩니다.
  - 예약한 택배는 칸 시작 20분 전까지 변경·취소할 수 있습니다. 그 뒤와 자동 배송 확정 뒤에는 출력 키오스크에서 꺼내는 것만 됩니다.
  - 규격 초과·취급 주의·2회 배달 실패 택배는 직접 수령 전용으로 표시되고, 인증코드를 볼 수 있습니다(60초 뒤 자동으로 가려짐).
- **배송 시간 고르기**: 칸마다 자리 2개를 점으로 보여 줍니다(빈 자리 · 다른 세대 · 내 예약). 택배를 1~2개 골라 예약합니다.
  다른 세대가 방금 자리를 잡으면 창을 연 채로 바로 반영되고 "방금 다른 세대가 예약했어요" 가 나옵니다.
- **배송 로봇**: 내 택배를 싣고 있는 로봇만 보여 줍니다. 30초 넘게 상태가 오지 않으면 "통신 끊김".
- **알림**: 도착, 시간 선택 마감 10분 전, 자동 배송 확정, 지연, 출발, 배송 완료·실패. 새 알림이 오면 토스트로 보여 줍니다.
- 갱신: SSE로 바로 받고, 60초마다 한 번 더 확인합니다. 관리자가 현장 설정을 바꾸면, 창이나 입력 중인 칸이 없을 때 화면을 새로 불러옵니다.

## 3. REST API

모든 응답은 JSON입니다. 오류는 `{ "error": { "code": "…", "message": "화면에 보여 줄 문구", … } }` 형식입니다.
로그인하면 `lg_session` 쿠키(HttpOnly, SameSite=Lax, 7일)에 보관소 서버 세션 토큰이 들어갑니다. 401 응답은 쿠키를 지웁니다.

| 메서드·경로 | 요청 | 응답 |
|---|---|---|
| `GET /api/health` | — | `ok`, `locker`, `server` |
| `GET /api/config` | — | `initialized`, `version`, `site`(공개 현장 설정), `statuses` |
| `POST /api/auth/login` | `{ building, unit, password }` (또는 `userId: "1-101"`) | `{ user }` + 쿠키. IP당 5분에 10번 넘게 시도하면 429 |
| `POST /api/auth/logout` | — | `{ ok }` |
| `POST /api/auth/register` | — | 410 `REGISTRATION_DISABLED` |
| `GET /api/me` | — | `{ user, unread }` |
| `PATCH /api/user/password` | `{ currentPassword, newPassword }` (숫자 4~8자리) | `{ user, revoked }` |
| `GET /api/user/preferences` | — | `{ preference: { defaultDelivery } }` |
| `PATCH /api/user/preferences` | `{ defaultDelivery: "PICKUP" \| "DIRECT" }` | `{ preference: { defaultDelivery, changed, skipped, parcels } }` |
| `GET /api/user/parcels` | — | `{ serverTime, user, unread, parcels }` |
| `GET /api/user/parcels/:id/pickup-code` | — | `{ code }` |
| `PATCH /api/user/parcels/:id/delivery` | `{ method: "PICKUP" \| "DIRECT" }` | `{ parcel }` |
| `DELETE /api/user/parcels/:id` | — | `{ ok }` (끝난 택배만) |
| `GET /api/user/slots` | — | `{ serverTime, serviceStart, serviceEnd, seatsPerSlot, slots }` |
| `POST /api/user/reservations` | `{ slotStart, parcelIds: [1~2개] }` | `{ slot, parcelIds, parcels }` |
| `DELETE /api/user/reservations/:parcelId` | — | `{ plan, parcels }` |
| `GET /api/user/notifications` | — | `{ unread, notifications }` |
| `POST /api/user/notifications/read` | `{ all: true }` 또는 `{ ids: [..] }` | `{ unread, notifications }` |
| `GET /api/user/robots` | — | `{ robots: [{ id, connected, status, location, battery, parcelIds, lastSeen }] }` |
| `GET /api/user/events` | — | SSE (아래) |

화면 용어 `PICKUP`(로봇 배송)·`DIRECT`(직접 수령)와 보관소 서버의 `ROBOT`·`DIRECT` 변환은 이 서버에서만 합니다.

```jsonc
// user
{ "userId": "1-101", "name": "가천관 101호실", "building": 1, "buildingName": "가천관", "unit": 101,
  "defaultDelivery": "PICKUP", "mustChange": false }
// parcel
{ "id": "20261101-89B536F3", "trackingNumber": null, "status": "STORED", "statusLabel": "보관 중",
  "deliveryMethod": "PICKUP", "plan": "RESERVED", "storageArea": "GRID", "door": null,
  "fragile": false, "directLock": null, "lockLabel": null, "choiceDeadline": null,
  "slot": { "start": "…", "end": "…", "label": "19:10~19:20", "changeUntil": "…" },
  "robotId": null, "dispatched": false, "fixed": false, "canChooseDelivery": true, "reservable": true,
  "createdAt": "…", "updatedAt": "…" }
// slot
{ "start": "…T19:10:00+09:00", "end": "…", "label": "19:10~19:20", "seatsLeft": 1, "available": 1,
  "restricted": false, "mine": 1, "mineParcelIds": ["…"] }
// notification
{ "id": 13, "kind": "DEPARTED", "title": "로봇이 출발했어요", "body": "…", "parcelId": "…", "createdAt": "…", "read": false }
```

주요 오류 코드:

| HTTP | 코드 |
|---|---|
| 401 | `AUTH_REQUIRED`(로그인 없음·만료), `INVALID_CREDENTIALS`(+`remaining`) |
| 423 | `ACCOUNT_LOCKED` |
| 400 | `WRONG_PASSWORD` |
| 409 | `DELIVERY_CHOICE_LOCKED`, `DIRECT_ONLY`, `DELIVERY_FIXED`, `TOO_LATE`, `SLOT_FULL`(+`seatsLeft`), `SLOT_UNAVAILABLE`, `NOT_RESERVED`, `NOT_ROBOT`, `PICKUP_CODE_UNAVAILABLE`, `PARCEL_NOT_DELIVERED` |
| 503 | `NOT_INITIALIZED`, `LOCKER_UNAVAILABLE` |

### SSE `GET /api/user/events`

로그인한 세대의 이벤트만 보냅니다. 25초마다 주석 줄(`: ping`)을 보내 연결을 유지합니다.

| event | data |
|---|---|
| `ready` | `{ household: "1-101" }` 연결됨 |
| `notification` | 알림 하나 (위 notification 형식) |
| `parcels` | `{ at }` 내 택배 목록이 바뀜 → 다시 불러오기 |
| `slots` | `{ slots: [{ start, seatsLeft }] }` 예약 칸 남은 자리 변동 (모든 세대 공통) |
| `config` | `{ initialized, version }` 현장 설정이 바뀜 |
| `server` | `{ online }` 보관소 서버 연결 상태 |

## 4. 테스트

**`lastgate/end_user_wep/`** 에서 실행합니다. 가짜 보관소 클라이언트로 API·SSE를 확인합니다.

```bash
npm test
```

## 5. 쓰지 않는 파일

`src/db.mjs`, `public/admin.html`, `public/index.html`, `public/assets/admin.js`, `public/assets/kiosk.js`, `public/assets/styles.css` 는
이전 단독 실행판의 보관 코드로, 서버가 내보내지 않습니다. `lastgate_v2_update.mjs` 는 소스를 덮어쓰는 이전 도구이므로 실행하지 않습니다.

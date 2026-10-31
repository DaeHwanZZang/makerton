# LastGate | 세대 웹 (입주민용)

아파트 공동배송 거점 **메이커톤 시연용 프로토타입**의 입주민 화면입니다.
내 택배 확인, 우리 집 기본 수령 방법(토글), 택배별 수령 방법 변경, 직접 수령 인증코드, 배송 로봇 현황을 제공합니다.

> 택배 데이터는 이 폴더가 아니라 **보관소 서버(`../server`)**에 있습니다.
> 이 Node 서버는 브라우저의 REST 요청을 받아 MQTT로 보관소 서버에 전달하는 **중계 서버**입니다.
> 전체 구조와 메시지 규격은 저장소 루트의 `README.md`, `protocol.md`를 참고하세요.

```
입주민 휴대폰 ─ HTTP ─▶ [세대 웹 서버 (이 폴더, :4180)] ─ MQTT 1883 ─▶ [브로커] ◀─ [보관소 서버]
                                       ▲                                   ▲
                                       └──── 로봇 상태 구독 ─────────────── [FMS]
```

## 1. 실행 준비

- **Node.js 20.6 이상**
- 처음 한 번 패키지를 설치합니다(MQTT 클라이언트 `mqtt` 1개).
- 브로커와 보관소 서버가 먼저 떠 있어야 로그인할 수 있습니다(루트 README 3장).

```bash
npm install
npm start
```

| 주소 | 용도 |
|---|---|
| `http://127.0.0.1:4180/` | 세대 웹 |
| `http://127.0.0.1:4180/api/health` | 서버·보관소 연결 확인 (`locker: connected`) |

**로그인**: 세대 아이디 `동-호`(예: `101-412`)와 비밀번호. 데모 초기 비밀번호는 `1234`입니다.
계정은 보관소 서버가 101~106동 × 101~412호(288세대) 분량으로 미리 만들며, 출력 키오스크와 같은 계정입니다.
회원가입은 없습니다.

설정은 환경변수로 바꿉니다. `.env.example`을 `.env`로 복사한 뒤 `npm run start:env`로 실행해도 됩니다.

| 변수 | 기본값 | 의미 |
|---|---|---|
| `PORT` | `4180` | 웹 키오스크(Vite, 4173)와 겹치지 않게 4180 |
| `HOST` | `127.0.0.1` | 같은 와이파이의 휴대폰에서 열려면 `0.0.0.0` |
| `LOCKER_BROKER_HOST` / `LOCKER_BROKER_PORT` | `localhost` / `1883` | 보관소 서버와 같은 브로커 |
| `LOCKER_BROKER_URL` | — | 전체 주소로 지정할 때 (예: `mqtt://192.168.0.50:1883`) |

종료: 터미널에서 `Ctrl+C`.

## 2. 화면과 동작

| 기능 | 동작 |
|---|---|
| 내 택배 | 보관 중인 택배 전부 + 최근 14일 안에 배송·수령이 끝난 택배 |
| 기본 설정(토글) | **바꾸는 즉시 아직 로봇에 실리지 않은 택배가 모두 같은 방법으로 바뀌고**, 앞으로 등록되는 택배에도 적용 |
| 택배별 변경 | 여러 개 선택 → [로봇 배송] / [직접 수령]. 기본 설정이 직접 수령이면 [로봇 배송]은 비활성 |
| 인증코드 보기 | 직접 수령으로 보관 중인 택배만. 60초 뒤 자동으로 숨김. 수령 자체는 출력 키오스크에서 아이디/비밀번호로 로그인해 진행하며, 코드는 보조 수단 |
| 완료 택배 삭제 | 배송 완료·수령 완료 택배를 내 목록에서 숨김 |
| 로봇 현황 | FMS가 보내는 로봇 상태(위치·배터리). 30초 넘게 소식이 없으면 "통신 끊김" |

상태 표시는 보관소 서버 상태를 이렇게 바꿔 보여줍니다.

| 보관소 서버 | 세대 웹 | 수령 방법 변경 |
|---|---|---|
| `STORED`, `JOB_ASSIGNED` | 보관 완료 / 로봇 운송 준비 중 | 가능 |
| `LOADED` | 로봇 배송 중 (`IN_TRANSIT`) | 불가 |
| `DELIVERED` | 배송 완료 | 불가, 삭제 가능 |
| `PICKED` | 수령 완료 (`PICKED_UP`) | 불가, 삭제 가능 |
| `REGISTERED`, `CANCELLED` | 표시하지 않음 | — |

## 3. 프로젝트 구조

```text
end_user_wep/
├── public/
│   ├── user.html          # 세대 웹 (사용 중)
│   ├── index.html         # 예전 기사 키오스크 (사용 안 함, web/ 의 웹 키오스크로 대체)
│   ├── admin.html         # 예전 운영자 데모 (사용 안 함)
│   └── assets/
│       ├── user.css, user.js
│       └── styles.css, kiosk.js, admin.js   # 예전 화면용
├── src/
│   ├── locker-client.mjs  # 보관소 서버 MQTT 요청·응답, 로봇 상태 구독
│   ├── user-service.mjs   # 로그인·세션, 택배·수령 방법·로봇 (보관소 서버 호출)
│   ├── parcel-service.mjs # 상수, 보관소 데이터 → 화면 데이터 변환
│   ├── api-server.mjs     # REST API 및 화면 제공
│   └── db.mjs             # 예전 자체 SQLite (사용 안 함)
├── tests/api.test.mjs     # 가짜 보관소 클라이언트로 API 검증
├── server.mjs             # 서버 시작
└── .env.example
```

> `lastgate_v2_update.mjs`는 예전 소스로 파일을 **덮어쓰는** 보관용 도구입니다. 실행하지 마세요.
> `data/lastgate.sqlite`는 예전 자체 DB로 더 이상 읽지 않습니다.

## 4. REST API

모든 사용자 API에는 로그인 쿠키(`lg_session`, 7일)가 필요합니다. 오류는 `{ "error": { "code", "message" } }` 형식입니다.

| Method | 경로 | 내용 |
|---|---|---|
| `GET` | `/api/health` | `{ ok, locker: "connected" \| "disconnected" }` |
| `GET` | `/api/config` | 동·크기·상태 목록 |
| `POST` | `/api/auth/login` | `{ "userId": "101-412", "password": "1234" }` |
| `POST` | `/api/auth/logout` | 로그아웃 |
| `GET` | `/api/me` | 로그인한 세대 정보 |
| `GET` / `PATCH` | `/api/user/preferences` | 기본 수령 방법 조회 / 변경 `{ "defaultDelivery": "PICKUP" \| "ROBOT" }` → 응답에 일괄 변경 건수 `changed` |
| `GET` | `/api/user/parcels` | 내 택배 목록 |
| `PATCH` | `/api/user/parcels/:id/delivery` | 택배별 변경 `{ "method": "PICKUP" \| "ROBOT" }` |
| `GET` | `/api/user/parcels/:id/pickup-code` | 직접 수령 인증코드 |
| `DELETE` | `/api/user/parcels/:id` | 완료 택배를 목록에서 숨김 |
| `GET` | `/api/user/robots` | 로봇 현황 |

`:id`는 보관소 접수번호(`YYYYMMDD-XXXXXXXX`)입니다.

주요 오류 코드:

| HTTP | 코드 | 상황 |
|---|---|---|
| 401 | `AUTH_REQUIRED`, `INVALID_CREDENTIALS` | 로그인 필요·만료, 아이디/비밀번호 불일치 |
| 409 | `DELIVERY_CHOICE_LOCKED` | 이미 로봇에 실림, 또는 기본 설정이 직접 수령인데 로봇 배송 선택 |
| 409 | `PICKUP_CODE_UNAVAILABLE`, `PARCEL_NOT_DELIVERED` | 인증코드 조건 불충족, 완료 전 삭제 |
| 410 | `REGISTRATION_DISABLED` | 회원가입 (계정은 미리 발급) |
| 503 | `LOCKER_UNAVAILABLE` | 브로커·보관소 서버 연결 끊김 또는 응답 없음 |

택배 접수(`POST /api/parcels`), 관리자 API, 공개 추적 API는 보관소 서버와 웹 키오스크가 담당하므로 제공하지 않습니다.

## 5. 로봇 담당 팀원에게 전달할 사항

세대 웹의 로봇 현황은 FMS가 아래 토픽으로 상태를 보내면 표시됩니다(테스트용 `server/tools/fms_stub.py`가 같은 형식으로 보냄).

```jsonc
// 토픽: locker/robot/{robot_id}/state   (retain, 10초마다 다시 발행)
{ "robot_id": "R1", "status": "배송 중", "location": "101동 412호", "battery": 88, "available": false }
```

배달 지시·결과 메시지는 `protocol.md` 5장(보관소 서버 ↔ FMS)을 따릅니다.

## 6. 테스트

```bash
npm test
```

가짜 보관소 클라이언트를 넣어 로그인, 상태·수령 방법 변환, 토글 일괄 변경, 물품별 변경 제한, 인증코드, 완료 택배 삭제, 로봇 통신 끊김, 보관소 연결 끊김·세션 만료를 검사합니다.

## 7. 실제 배포 전에 필요한 보완

- 계정 비밀번호 변경·재설정, 실사용 개인정보 처리방침
- HTTPS, 브로커 계정·ACL, 요청 속도 제한
- 세대 웹 서버가 여러 대일 때 세션 공유(지금은 서버 메모리에 보관)

**현재는 행사 시연용 소프트웨어입니다. 실제 입주민 정보를 넣어 인터넷에 공개하지 마세요.**

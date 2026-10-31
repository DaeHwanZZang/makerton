# LastGate | 관리자 웹 (보관소 PC 전용, v4.0)

현장 초기 설정과 운영 관리를 하는 화면입니다.
- 현장 초기 설정·설정 변경
- 세대 계정 조회, 비밀번호 초기화, 입주자 변경, 잠금 해제
- 보관 현황
- 인증코드로 수령 처리
- 작업 기록
- 전체 재설정
- 관리자 비밀번호 변경

> 데이터는 보관소 서버(`../server`)에 있습니다. 이 Node 서버(BFF)는 브라우저 요청을 MQTT `locker/admin/*` 로 전달하고,
> `client_id` 는 `ADMIN-WEB-` 으로 시작합니다. 메시지 규격은 [protocol.md](../protocol.md) 8장에 있습니다.

## 1. 실행

**`lastgate/admin_web/`** 에서 실행합니다. 브로커와 보관소 서버가 먼저 떠 있어야 합니다.

```bash
npm ci
```

```bash
npm start
```

`http://127.0.0.1:4190/` 에서 `admin` / `admin1234` 로 로그인합니다.
보관소 서버를 처음 실행할 때 `LASTGATE_ADMIN_PASSWORD` 가 있으면 그 값이 관리자 비밀번호가 됩니다. 기본 비밀번호인 동안은 바꾸라는 안내가 나옵니다.

| 변수 | 기본값 | 의미 |
|---|---|---|
| `PORT` | `4190` | |
| `HOST` | `127.0.0.1` | 보관소 PC에서만 엽니다. 다른 값을 쓰면 시작할 때 경고합니다 |
| `LOCKER_BROKER_HOST` / `LOCKER_BROKER_PORT` / `LOCKER_BROKER_URL` | `localhost` / `1883` | 보관소 서버와 같은 브로커 |
| `LOCKER_BROKER_USER` / `LOCKER_BROKER_PASS` | — | 브로커 계정 (운영 시) |

## 2. 보안

- 쿠키 `lg_admin`: HttpOnly, SameSite=Strict, 브라우저를 닫으면 사라지는 세션 쿠키입니다. 서버 세션은 30분 동안 사용하지 않으면 만료되고, 화면도 30분 동안 조작이 없으면 로그아웃합니다.
- `Host`·`Origin` 이 localhost / 127.0.0.1 / [::1] 이 아니면 거절합니다(DNS 리바인딩·CSRF 방지).
- CSP `default-src 'self'`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` 를 씁니다.
- 로그인은 IP당 5분에 10번까지만 받습니다(429). 보관소 서버도 5번 틀리면 5분 잠급니다.
- 관리자 작업은 모두 보관소 서버에 `ADMIN_*` 이벤트로 기록됩니다.

## 3. REST API

성공하면 보관소 서버 응답에서 `type`·`request_id`·`status`·`message` 를 뺀 나머지(snake_case 그대로)를 돌려줍니다.
실패하면 `{ "error": { "code": "<보관소 status>", "message": "…", … } }` 를 돌려줍니다.

| 메서드·경로 | 요청 | MQTT 토픽 |
|---|---|---|
| `GET /api/health` | — | (연결 상태) |
| `GET /api/admin/config` | — | (retained `locker/config/site`) `initialized`, `version`, `site_name` |
| `POST /api/admin/login` | `{ adminId, password }` | `locker/admin/login` |
| `POST /api/admin/logout` | — | `locker/admin/logout` |
| `PATCH /api/admin/password` | `{ currentPassword, newPassword }` | `locker/admin/password` |
| `GET /api/admin/site` | — | `locker/admin/site/get` |
| `POST /api/admin/site/preview` | `{ site }` | `locker/admin/site/preview` |
| `POST /api/admin/site/init` | `{ site }` | `locker/admin/site/init` |
| `PUT /api/admin/site` | `{ site }` | `locker/admin/site/update` |
| `POST /api/admin/site/reset` | `{ password, confirmName }` | `locker/admin/site/reset` |
| `GET /api/admin/accounts?building=&q=` | — | `locker/admin/accounts/list` |
| `POST /api/admin/accounts/:userId/reset-password` | — | `locker/admin/accounts/reset_password` |
| `POST /api/admin/accounts/:userId/change-occupant` | — | `locker/admin/accounts/change_occupant` |
| `POST /api/admin/accounts/:userId/unlock` | — | `locker/admin/accounts/unlock` |
| `GET /api/admin/storage` | — | `locker/admin/storage` |
| `POST /api/admin/code-pickup` | `{ code, confirm }` (`confirm: false` 면 조회만) | `locker/admin/code_pickup` |
| `GET /api/admin/events?all=1&limit=100` | — | `locker/admin/events` |

| HTTP | 보관소 status |
|---|---|
| 400 | `INVALID` |
| 401 | `AUTH_REQUIRED`, `EXPIRED_SESSION`, 로그인 `DENIED` |
| 403 | `DENIED` |
| 404 | `NOT_FOUND` |
| 409 | `ALREADY_INITIALIZED`, `NOT_INITIALIZED`, `BLOCKED`(+`blocked`), `MISMATCH`, `ALREADY_LOADED` |
| 423 | `LOCKED`(+`retry_after_sec`) |
| 503 | `LOCKER_UNAVAILABLE` |

## 4. 화면 메모

- **초기 설정 / 설정 변경**: 같은 양식을 씁니다. 입력하면 0.4초 뒤 미리보기(계정 수·호실 안내·추가/삭제 호실)를 다시 계산합니다.
  기존 건물 번호는 바꿀 수 없습니다. 보관·배송 중 물품이 있는 호실을 빼려 하면 저장이 막히고(`BLOCKED`) 이유가 표시됩니다.
- **입주자 변경**: 비밀번호를 1234로, 기본 수령 방식을 로봇 배송으로 되돌리고, 이전 입주자의 택배 기록·알림을 숨깁니다. 다른 기기의 로그인은 끝납니다.
- **코드로 수령 처리**: 세대 웹 인증코드 6자리로 물품을 찾고, 확인을 누르면 수령 처리합니다. 로봇에 이미 실린 물품은 처리할 수 없습니다.
- **전체 재설정**: 보관·배송 중 물품이 없을 때만 됩니다. 사이트 이름과 관리자 비밀번호를 다시 입력해야 합니다.

## 5. 테스트

**`lastgate/admin_web/`** 에서 실행합니다.

```bash
npm test
```

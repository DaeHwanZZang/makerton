# 개발용 MQTT 브로커

[프로젝트 시작 안내](../README.md) · [메시지 계약](../common/schemas.py) · [승강기 계약](../common/elevator_proto.py)

로봇·FMS·뷰어가 사용하는 Mosquitto 설정이다. 기본 설정은 한 PC에서 개발하는 용도다.

## 설치와 실행

다음 명령은 **리포 루트**, macOS 기준이다.

```bash
brew install mosquitto
mosquitto -c broker/mosquitto.conf -v
```

Ubuntu 설치 명령은 루트 README를 참고한다. 해당 목록에는 브로커와 조회 도구인 `mosquitto-clients`가 포함된다.

## 기본 설정

| 항목 | 값 |
|---|---|
| 리스너 | `127.0.0.1:1883` |
| 익명 연결 | 허용 |
| 메시지 persistence | 꺼짐 |
| max_queued_messages | `10000` |
| max_inflight_messages | `100` |

다른 PC에서는 기본 리스너에 접속할 수 없다. 원격 구성을 쓰려면 브로커의 리스너·접근 정책과
각 클라이언트의 호스트·인증 지원을 함께 확인해야 한다. 현재 로봇/FMS 기본 옵션에는 사용자명·비밀번호 설정이 없다.
호스트의 방화벽과 네트워크 구성이 필요할 수 있으며 이 문서의 기본 실행 범위는 localhost다.

## 상태 확인

아래 조회는 **리포 루트**에서 실행한다. 종료는 Ctrl+C다.

```bash
mosquitto_sub -h localhost -p 1883 -t 'fms/v1/#' -v
```

승강기 상태는 `elev/v1/+/state`, 로봇 물리 디버그는 `fms/v1/+/physics`를 구독한다.
브로커가 재시작되면 persistence가 꺼져 있으므로 저장 메시지는 유지되지 않는다.

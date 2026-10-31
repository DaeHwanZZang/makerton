# Qt 플릿 뷰어

[프로젝트 시작 안내](../README.md) · [로봇과 물리](../docs/robot-client.md) · [메시지 계약](../common/schemas.py)

Python/PySide6 기반 관측 전용 뷰어다. MQTT 상태·오더·물리 토픽을 구독하고,
필요한 맵을 HTTP GET으로 받는다. 로봇이나 FMS에 제어 명령을 발행하지 않는다.

## 실행

루트 README에서 공용 `.venv`를 만든 뒤 **`viewer/`에서** 실행한다.

```bash
cd viewer
../.venv/bin/python tools/fleet_monitor_qt.py --follow AMR-001 --fms http://localhost:8000
```

브로커 주소는 `--host`/`--port`로 지정하며 기본값은 localhost:1883이다.
`--follow`를 생략하면 맵을 보고하는 첫 로봇을 따른다.
정적 맵 확인도 **`viewer/`에서** 실행한다.

```bash
../.venv/bin/python tools/fleet_monitor_qt.py ../maps/empty_hall1/fms_map.json
```

뷰어만 따로 설치할 때는 **`viewer/`에서** 다음 명령으로 전용 venv를 만들 수 있다.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python tools/fleet_monitor_qt.py --follow AMR-001
```

## 맵 따라가기

- 로봇의 `State.map_id`(FMS map_uid)와 `map_version`을 기준으로 맵을 선택한다.
- 먼저 같은 PC의 `../map_cache/<uid>/`에서 버전이 맞는 맵을 찾고, 없으면 FMS 번들을 `viewer/.map_cache/`에 다운로드한다.
- 엘리베이터로 맵 UID가 바뀌면 뷰어 맵도 전환한다. 다른 맵의 로봇은 본문에 그리지 않고 상태 패널에서 구분한다.
- `--map`으로 로컬 실행한 로봇은 FMS map_id를 보고하지 않을 수 있다. 정적 맵이나 `--fallback-map JSON`을 사용한다. 런처는 선택한 맵을 fallback으로 전달한다.
- 구역은 번들의 annotations를 사용하며 엘리베이터는 보라색이다. 이미지는 메타데이터의 image를 따른다.

따라가기 모드는 맵 인자를 생략하거나 `--follow`를 지정했을 때 동작한다.

## 조작 키

| 키 | 동작 |
|---|---|
| `t` | 계획 경로·경유점·목표 표시 |
| `p` | 물리 상세 패널 |
| `l` | 지나온 궤적 |
| `o` | 참 자세(GT), 청록 윤곽·점선 궤적 |
| `i` | 보정 전 오도메트리, 노란 점선 |
| `k` | 궤적·피크값 초기화 |
| `f` | 로봇 시점 고정 순환·해제 |
| `g` / `c` / `z` / `r` | 격자 / 색상 / 구역 / 보기 초기화 |

마우스 드래그는 시점 고정을 해제하며 휠 확대/축소는 유지한다.
몸체는 State.pose의 추정 위치다. 물리 토픽이 있을 때만 GT·오도메트리·슬립 등 추가 정보를 그린다.
토픽이 없는 다른 구현의 로봇은 기본 State 정보로 표시한다.

## 코드 위치

| 파일 | 역할 |
|---|---|
| `tools/fleet_monitor_qt.py` | 진입점과 MapFollower |
| `tools/fleet_monitor.py` | MQTT 구독·로봇 데이터 |
| `tools/map_follow.py` | 로컬 캐시와 FMS 번들에서 맵 찾기 |
| `tools/qt_viewer.py` | QGraphicsView 렌더링과 조작 |
| `tools/map_viewer.py` | 레거시 정적 matplotlib 표시 |
| `common/` | 루트 common의 대응 파일 사본 |

프로토콜/맵 계약 변경 시 루트와 뷰어 사본을 함께 갱신하고 test_common_sync를 실행한다.
로컬 정적 샘플은 루트 maps를 공유하며 `viewer/maps` 사본은 없다.

# 맵 좌표계와 도구

[프로젝트 시작 안내](../README.md) · [FMS 맵 업로드](../fms_server/README.md) · [개발 구조](architecture.md)

아래 명령은 모두 **리포 루트**에서 실행한다. 맵 생성·편집은 루트 `.venv`를 사용한다.

## 맵과 좌표계

- 메타 `fms_map.json` 의 키는 `image`, `resolution`, `origin_x`/`origin_y`, `width_px`/`height_px`, 임계값 `free_thresh`/`occupied_thresh` 이다. 런처와 `launch.sh` 는 `image`, `resolution`, `width_px` 를 모두 가진 json 을 메타로 본다.
- `origin` 은 **왼쪽 위 픽셀의 world 좌표**이고 y 는 아래로 증가한다. 변환식은 `px = floor((x - origin_x) / res)` (`map_model.cpp`).
- 칸 분류: 회색값 `>= free_thresh` 이면 FREE, `<= occupied_thresh` 이면 OCCUPIED, 그 사이는 UNKNOWN, 맵 밖은 OCCUPIED. **주행 가능 = FREE 만** (`drivable()`).
- planner 는 비-FREE 칸 전부를 로봇 반폭(`ceil(width/2/res)` px)만큼 팽창시킨 뒤 A* 를 돌린다. 시작점이나 목표점이 비-FREE 면 `PlanningError` 를 던진다.
- 생성 맵(`gen_map`/`synth_map`)은 res 0.03125 (32 px/m)를 쓴다. 사방에 회색 여백 10 타일을 두고 origin 을 -10 으로 잡아서, 레이아웃 칸 (r,c) 의 중심이 world `(c+1, r+1)` m 가 된다. 그래서 world (0,0) 은 여백(UNKNOWN) 위라 주행할 수 없다. 기본 `--x 0 --y 0` 으로 띄우면 오더가 NO_PATH 로 실패한다.
- 같은 폴더에 맵을 다시 생성하면 origin 이 -여백 으로 초기화된다. 편집기에서 바꾼 원점은 사라진다.
- `maps/` 구성:
  - `641931de9eae7cecb34d5765`, `warehouse`: 실제 맵이다. 실제 맵 샘플이며 외부 원본 변경은 자동으로 반영되지 않는다.
  - `gen_loop`, `synth_loop`, `gui_map`, `test1~3`: 생성 맵이다.
  - `gui_map`: 사용자가 편집기로 원점을 (-11.65625, -11.625)로 바꾸고 `edits.json` 을 붙였다. 함부로 다시 생성하지 않는다.
  - `test1`: `grid.png`/`edited_navi_gridmap.png` 가 없어서 편집기로 열 수 없다.

## 통합 런처 (`tools/launcher.py`)

브로커, 로봇 클라이언트, 뷰어, 맵 편집기, 맵 생성기를 한 창에서 켜고 끈다.

```bash
# 리포 루트에서 (최초 1회: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt)
.venv/bin/python tools/launcher.py
```

- 각 프로그램은 별도 프로세스로 뜨고 자기 창은 따로 뜬다. 출력은 `logs/<이름>.log` 에 남고 화면에는 마지막 2000줄만 보인다.
- 위쪽 브로커 host/port/맵은 공통 값이다. 로봇·뷰어는 브로커가 꺼져 있으면 먼저 켤지 묻는다.
- `추가 ▾` 에서 로봇(대수 지정, 이름은 AMR-NNN 자동, y 는 대수마다 1 m 씩 증가), 맵 편집기(선택한 맵, 맵당 1개), 빌드(make)를 추가한다.
- 정지는 SIGTERM 이다. 맵 편집기는 저장 안 한 수정이 있으면 확인 창이 뜨고, 제한 시간(편집기 30 초)이 지나면 강제 종료할지 묻는다.
- 런처를 닫을 때 "전부 정지 / 그대로 두고 종료 / 취소" 를 고른다. 그대로 두고 종료하거나 비정상 종료하면 다음 실행 때 `.run/launcher.json` 으로 프로세스를 다시 찾아 붙는다.
- 로봇은 UNLOCALIZED 로 뜬다. 제어판(GUI) Pose Hint 나 `send_order --instant INIT_POSITION` 으로 위치를 알려 줘야 주행한다 ([로봇 위치 확인](robot-client.md) 참고).
- 로봇 시작 위치가 벽/미탐색이어도 실행은 된다 (`start position ... is not drivable` 경고). 벽 속에서는 힌트로 위치를 못 잡으니 제어판 Force Localize 로 주행 가능한 칸에 옮긴다 (`NO_PATH`/`COLLISION` 오류도 풀린다). 레이아웃 맵은 칸 (r,c) 중심이 world ((c+1) m, (r+1) m). 생성 맵의 world (0,0) 은 회색 여백이다.
- FMS 서버 항목: `uvicorn fms_server.main:app` 을 띄운다. 위쪽 브로커 host/port 가 `FMS_MQTT_HOST/PORT` 로 넘어가고, 설정창에서 HTTP 포트·바인드 주소·`FMS_PUBLIC_URL`·데이터 폴더를 정한다. 로봇을 `(맵 없음)` 으로 띄우면 FMS 가 SET_MAP 으로 맵을 내려준다 (로봇 설정 `SET_MAP 맵 캐시 폴더`).
- 새 툴은 `tools/launcher_tools.json` 에 항목만 추가하면 붙는다 (파일 안 `_설명` 참고). `launch.sh`/`stop.sh` 는 따로 그대로 쓸 수 있다 (`.run/pids` 는 런처가 쓰지 않는다).

## 테스트 맵 생성 (`tools/gen_map.py`)

손으로 그린 맵(`maps/warehouse`)은 너무 정형화돼 있어서, 실제 SLAM 맵
(`maps/641931.../grid.png`) 에서 1 m 타일 조각을 잘라 레이아웃대로 이어붙인다.
점선 벽, 벽 너머 회색(미탐색), 레이저 줄무늬가 원본 그대로 남는다.

```bash
python3.11 -m venv .venv && .venv/bin/pip install numpy pillow   # 리포 루트. viewer/.venv 를 써도 된다
.venv/bin/python tools/gen_map.py tools/map_layouts/loop.txt --out maps/gen_loop --seed 1
./robot_client --id AMR-001 --map maps/gen_loop/fms_map.json --x 1.0 --y 1.0
```

- 레이아웃은 텍스트 한 글자 = 1 m: `.` 통로, `#` 막힘, `;` 주석. 바깥 1칸은 자동으로 막힌다
- 출력 파일: `grid.png`(생성 결과, 다시 생성하면 덮어씀), `edited_navi_gridmap.png`(편집기/FMS 웹 UI
  수정용. **최초 생성 때만** `grid.png` 복사본으로 만들고 이후엔 건드리지 않음 -- 새 레이아웃으로 다시
  만들려면 이 파일을 지우고 생성), `fms_map.json`(image 가 `edited_navi_gridmap.png` 를 가리킴)
- 여백: 사방에 `--margin-tiles`(기본 10 = 320 px = 10 m) 만큼 회색(미탐색) 여백. 검사는 여백 전 이미지로 한다
- 좌표: origin = (-여백 m, -여백 m) 이라 여백과 상관없이 레이아웃 (행 r, 열 c) 칸 중심 =
  world (c+1, r+1) m. 칸 중심 좌표는 전부 양수라 `send_order --path` 로 바로 보낼 수 있다
  (`send_order` 는 음수 좌표를 못 받음)
- 생성 후 planner 와 같은 규칙(비-FREE 를 로봇 반폭만큼 팽창)으로 모든 통로 칸이
  주행 가능하고 하나로 이어지는지 검사한다. 실패하면 seed 를 바꿔 `--tries` 번 재시도
- 원본에 모서리 조각이 드물어서 일부 모서리는 일자벽 조각 두 장으로 합성된다.
  타일 경계(1 m 격자)에서 줄무늬가 끊기는 건 남아 있다 — 표시 문제, 주행 영향 없음
- 대각선으로만 맞닿은 통로는 이어지지 않는다 (planner 도 못 지나간다)

## 원본 없이 한 번에 그리기 (`tools/synth_map.py`)

조각을 이어붙이면 조각 경계마다 벽 위치와 노이즈가 어긋난다. `synth_map.py` 는 같은
레이아웃으로 맵 전체에 벽(경계 양쪽 1 px 씩 = 32 px 타일의 16, 17 번째 픽셀)을 긋고
질감 노이즈를 맵 단위로 깔아서 이음매가 없다. 좌표, 검사, 출력 파일은 `gen_map.py` 와 같다.

```bash
.venv/bin/python tools/synth_map.py tools/map_layouts/loop.txt --out maps/synth_loop --seed 1   # 리포 루트에서
```

- 질감: 통로 247~255 저주파, 벽 20~60, 벽에서 `--fringe-px`(기본 2) 안에 회색 테두리
  (`--fringe-prob`). 통로 쪽 테두리는 비-FREE 라 planner 기준 통로가 테두리 폭만큼 좁아진다
- 불규칙성 (기본 켜짐, 0 으로 끄면 실선 직선 벽):
  - `--wobble-px 1.0` 벽 흔들림. 0 이 아니면 벽이 정확히 16, 17 번째 픽셀이 아니다
  - `--dash-gap 0.25 --dash-px 5` 점선 벽. 벽 픽셀의 25% 를 128 로 끊는다 (128 도 비-FREE 라 주행 영향 없음)
  - `--fur-prob 0.06 --fur-len 10` 털. 끊긴 곳에서 막힘 쪽으로 가는 1 px FREE 선. planner 팽창으로 지워진다
  - `--ray-prob 0.012 --ray-len 150` 긴 광선. 끊긴 곳에서 부채꼴로 최대 150 px, 일부는 옅은 회색.
    여백도 같이 그려서 회색 영역으로 길게 뻗는다 (통로/다른 벽을 만나면 멈춤). 검사는 여백을 잘라낸 이미지로 한다

## 생성 GUI (`tools/map_gen_gui.py`)

레이아웃 편집, 방식 선택(합성 / 실제 조각), 옵션 입력, 생성 버튼, 결과 미리보기와 로그가
한 창에 있다. 내부에서는 `synth_map.py` / `gen_map.py` 를 그대로 실행하므로 CLI 와 결과가 같다.
`seed 랜덤 재생성` 버튼은 옵션은 그대로 두고 seed 만 무작위로 바꿔 바로 다시 생성한다 (덮어쓰기 확인 없음).
마지막으로 쓴 옵션과 레이아웃 텍스트는 `QSettings` 로 저장돼 다음 실행 때 복원된다 (생성 시, 창 닫을 때 저장).

```bash
.venv/bin/pip install PySide6                 # 리포 루트. numpy, pillow 와 같이 필요
.venv/bin/python tools/map_gen_gui.py         # 기본 출력 maps/gui_map (이미 있으면 덮어쓸지 묻는다)
```

## 맵 편집기 (`tools/map_editor.py`)

`grid.png` 위에 흰색/검정/회색 도형(사각형·선분·자유형)을 얹어 `edited_navi_gridmap.png` 를 만든다.
이미지에 직접 그리지 않고 도형을 world 좌표(m)로 `edits.json` 에 저장하므로, 다시 열어 도형을 선택·이동·꼭짓점 수정·
색/굵기 변경·삭제할 수 있고 줌해도 모양이 그대로다. 색은 255/0/128 세 값뿐이고 안티앨리어싱이 없다 (중간 회색값은 UNKNOWN 장애물이 되기 때문).

- 맵 폴더에 `grid.png` 와 `edited_navi_gridmap.png` 가 둘 다 없으면 경고를 띄우고 시작하지 않는다.
- 저장은 Ctrl+S 수동뿐이다. `edits.json` + `edited_navi_gridmap.png` 를 쓴다. `fms_map.json` 은 원점을 바꿨을 때만 `origin_x`/`origin_y` 두 값을 고친다 (저장 전 확인).
- **원점 지정 (O)**: 클릭한 지점(가장 가까운 픽셀 모서리)을 world (0, 0) 으로 만든다. 이미지와 도형은 그대로이고 좌표계만 옮겨진다 (도형 world 좌표는 같이 평행이동, 실행 취소 가능). 화면에 보라색 원 + x(빨강)/y(초록) 축으로 현재 원점이 표시된다. 원점이 바뀌면 로봇 시작 위치, 오더/구역 좌표처럼 이 맵의 world 좌표를 쓰는 값도 같이 바뀌어야 한다. 주의: 맵 생성기로 같은 폴더에 다시 생성하면 origin 이 -여백 으로 되돌아간다.
  저장 직후 주행 가능 영역(로봇 반폭 8 px 팽창 기준)이 늘어났으면 통로 끊김 경고를 띄운다.
- 디스크의 `edited_navi_gridmap.png` 가 `grid.png + edits.json` 렌더와 다르면(다른 도구로 수정) 첫 저장 때 덮어쓸지 묻는다.
- `gen_map`/`synth_map` 이 `grid.png` 를 다시 만들어도 `edits.json` 은 남는다. 새 `grid.png` 위에 같은 도형이 다시 얹힌다.
- 조작: V 선택 / R 사각형 / L 선분(Shift 15° 단위) / P 자유형, 휠 줌, 가운데·오른쪽 버튼 또는 Space+드래그 이동,
  F 전체 보기, Delete 삭제, Ctrl+Z / Ctrl+Shift+Z 실행 취소·다시 실행. `결과 미리보기` 는 저장될 실제 픽셀을 보여 준다.
- 데스크톱 편집기의 렌더 규칙은 GUI 없는 `tools/map_edit_model.py`에 있다.
  로컬 `--map` 로봇과 정적 맵 뷰어는 파일을 다시 로드해야 반영된다. FMS에 등록된 맵은 수정본을 업로드·활성화하면 SET_MAP으로 전달된다.
  웹의 zone 편집은 원본 보존과 버전 재굽기를 사용하는 별도 경로다.

```bash
.venv/bin/python tools/map_editor.py maps/gen_loop/fms_map.json   # 리포 루트에서 (PySide6, numpy, pillow)
```

## 노이즈 정리 (`tools/clean_map.py`)

생성 맵(또는 아무 맵)의 레이저 줄무늬, 벽 바깥 털, 통로 점을 지운다. 생성할 때
`--clean` 으로 붙이거나, 이미 있는 맵에 따로 돌린다 (리포 루트에서):

```bash
.venv/bin/python tools/gen_map.py tools/map_layouts/loop.txt --out maps/gen_loop --seed 1 --clean normal
.venv/bin/python tools/clean_map.py maps/gen_loop/fms_map.json --out maps/gen_loop_clean --level normal
```

| 프리셋 | 지우는 것 | 남는 것 |
|---|---|---|
| `light` | 통로 점, 막힘 영역 줄무늬, 멀리 튄 점 | 벽 털, 점선 벽, 질감 |
| `normal` | + 벽 틈으로 삐져나간 털 | 점선 벽, 벽 앞 회색 테두리, 통로 질감 |
| `strong` | + 원래 벽 (통로 경계에 실선 벽을 새로 긋고 값 평탄화) | 통로 모양만. 손으로 그린 맵에 가깝다 |

- 단계별 값은 `--fill-hole-px`, `--open-px`, `--min-free-px`, `--wall-reach-px`,
  `--[no-]solid-walls`, `--[no-]flatten` 로 덮어쓴다 (`gen_map.py` 에서는 `--clean-` 접두사)
- 정리 전후 planner 기준 주행 가능 면적을 출력한다. 통로 점을 메우면 면적이 늘고,
  털 깎기(opening)는 줄인다. `gen_map.py --clean` 은 정리 뒤에 레이아웃 검사를 돈다
- 원본 SLAM 맵에서 벽 틈으로 넓게 퍼진 부채꼴 FREE 는 통로와 이어져 있고 폭도 넓어서
  안 지워진다 (생성 맵에는 조각 추출 단계에서 걸러져 거의 없다)


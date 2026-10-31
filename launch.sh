#!/usr/bin/env bash
# 로봇 클라이언트(C++) 인터랙티브 실행기.
# CLI 옵션을 하나씩 물어보고, 로봇 대수만큼 robot_client 프로세스를 띄운다.
# 맵은 robot_new/maps/ 안에서만 찾는다 (robot_new 는 독립 리포지토리로 뗄 수 있어야 하므로
# 프로젝트 루트 maps/ 를 참조하지 않는다 -- CLAUDE.md 루트 maps/ 와는 별개의 사본).
# bash 3.2(macOS 기본) 호환을 위해 nounset(-u) 은 안 쓴다 -- 빈 배열 확장이
# 버전별로 다르게 동작해서 오탐이 잦다. Ubuntu(CLAUDE.md 최종 타깃)의 bash4+ 에서도 동일하게 동작한다.
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

BIN="./robot_client"
MAPS_DIR="./maps"
LOG_DIR="./logs"
RUN_DIR="./.run"
PIDS_FILE="$RUN_DIR/pids"

mkdir -p "$LOG_DIR" "$RUN_DIR"
: > "$PIDS_FILE"

# -- 입력 헬퍼 ---------------------------------------------------------------

ask() {  # ask "질문" "기본값" -> stdout 에 answer 출력
    local prompt="$1" default="${2:-}"
    local answer
    if [[ -n "$default" ]]; then
        read -r -p "$prompt [$default]: " answer
        echo "${answer:-$default}"
    else
        read -r -p "$prompt: " answer
        echo "$answer"
    fi
}

ask_yn() {  # ask_yn "질문" "y|n(기본)" -> "y" 또는 "n"
    local prompt="$1" default="${2:-n}"
    local answer
    while true; do
        read -r -p "$prompt (y/n) [$default]: " answer
        answer="${answer:-$default}"
        case "$answer" in
            y|Y) echo "y"; return ;;
            n|N) echo "n"; return ;;
            *) echo "y 또는 n 만 입력하세요" >&2 ;;
        esac
    done
}

ask_int() {  # ask_int "질문" "기본값"
    local prompt="$1" default="$2"
    local answer
    while true; do
        read -r -p "$prompt [$default]: " answer
        answer="${answer:-$default}"
        if [[ "$answer" =~ ^-?[0-9]+$ ]]; then echo "$answer"; return; fi
        echo "정수를 입력하세요" >&2
    done
}

ask_num() {  # ask_num "질문" "기본값"  (실수 허용)
    local prompt="$1" default="$2"
    local answer
    while true; do
        read -r -p "$prompt [$default]: " answer
        answer="${answer:-$default}"
        if [[ "$answer" =~ ^-?[0-9]+([.][0-9]+)?$ ]]; then echo "$answer"; return; fi
        echo "숫자를 입력하세요" >&2
    done
}

# -- 0) 바이너리 확인 ---------------------------------------------------------

if [[ ! -x "$BIN" ]]; then
    echo "robot_client 바이너리가 없다."
    if [[ "$(ask_yn "지금 make 할까요?" "y")" == "y" ]]; then
        make || { echo "빌드 실패. 직접 make 해보고 다시 실행하세요."; exit 1; }
    else
        echo "빌드 후 다시 실행하세요 (cd robot_new && make)."; exit 1
    fi
fi

# -- 1) 브로커 접속 정보 -------------------------------------------------------

HOST="$(ask "MQTT 브로커 host" "localhost")"
PORT="$(ask_int "MQTT 브로커 port" "1883")"

if ! nc -z -w1 "$HOST" "$PORT" 2>/dev/null; then
    echo "경고: $HOST:$PORT 응답 없음 -- mosquitto 가 떠 있는지 확인하세요 (계속 진행함)"
fi

# -- 2) 맵 선택 ---------------------------------------------------------------

MAP_ARG=""
MAP_DIRS=()
while IFS= read -r d; do
    [[ -n "$d" ]] && MAP_DIRS+=("$d")
done < <(find "$MAPS_DIR" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort)

if [[ ${#MAP_DIRS[@]} -eq 0 ]]; then
    echo "$MAPS_DIR 안에 맵 폴더가 없다 -- 맵 없이 진행 (경로계획/충돌감지 꺼짐)"
else
    echo "사용할 맵을 선택하세요:"
    echo "  0) 맵 없이 진행"
    for i in "${!MAP_DIRS[@]}"; do
        echo "  $((i + 1))) $(basename "${MAP_DIRS[$i]}")"
    done
    while true; do
        choice="$(ask_int "번호 선택" "0")"
        if [[ "$choice" -eq 0 ]]; then
            break
        elif [[ "$choice" -ge 1 && "$choice" -le ${#MAP_DIRS[@]} ]]; then
            dir="${MAP_DIRS[$((choice - 1))]}"
            # MapModel 이 읽는 메타데이터 json 자동 탐지: image/resolution/width_px 3개 키를
            # 모두 가진 파일만 후보로 삼는다 (벤더 맵 폴더는 map_meta.json 등 곁다리 json 도 있음)
            json=""
            while IFS= read -r f; do
                if jq -e 'has("image") and has("resolution") and has("width_px")' "$f" >/dev/null 2>&1; then
                    json="$f"; break
                fi
            done < <(find "$dir" -maxdepth 1 -name "*.json")
            if [[ -z "$json" ]]; then
                echo "이 폴더에서 맵 메타데이터 json 을 못 찾음. 다른 걸 고르세요"
                continue
            fi
            MAP_ARG="$json"
            echo "-> $MAP_ARG"
            break
        else
            echo "범위 밖 번호"
        fi
    done
fi

# -- 3) 공통 물리 파라미터 -----------------------------------------------------

ROBOT_WIDTH="$(ask_num "로봇 폭 (m)" "0.5")"
ROBOT_LENGTH="$(ask_num "로봇 길이 (m)" "0.7")"
MAX_VELOCITY="$(ask_num "최대 속도 (m/s)" "1.2")"
MAX_ACCEL="$(ask_num "최대 가속도 (m/s^2)" "0.6")"
BATTERY="$(ask_num "시작 배터리 (%)" "100")"
SPEED_NOISE="$(ask_num "속도 노이즈 비율 (0.05=5%)" "0.05")"
SENSOR_RANGE="$(ask_num "동적 장애물 감지 반경 (m)" "3.0")"
LOG_LEVEL="$(ask "로그 레벨 (DEBUG/INFO/WARNING)" "INFO")"

# -- 4) 동적 장애물(SIM 구역) 공통 설정 -----------------------------------------

OBSTACLE_ARGS=()
if [[ "$(ask_yn "구역 통과형 동적 장애물을 켤까요? (모든 로봇 공통 적용)" "n")" == "y" ]]; then
    ZONE="$(ask "구역 XMIN,YMIN,XMAX,YMAX" "-2,3,2,4")"
    SIDES="$(ask "진입-이탈 면 (1=위 2=오 3=아래 4=왼, 예: 4-2, 빈값=랜덤)" "")"
    SPAWN_INTERVAL="$(ask_num "스폰 주기 (초)" "5.0")"
    MAX_COUNT="$(ask_int "동시 최대 개수" "3")"
    OBS_WIDTH="$(ask_num "장애물 가로 (m)" "0.4")"
    OBS_HEIGHT="$(ask_num "장애물 세로 (m)" "0.4")"
    OBS_SPEED="$(ask_num "장애물 속도 (m/s)" "0.5")"
    OBSTACLE_ARGS=(--obstacle-zone "$ZONE" --obstacle-spawn-interval "$SPAWN_INTERVAL"
                   --obstacle-max-count "$MAX_COUNT" --obstacle-width "$OBS_WIDTH"
                   --obstacle-height "$OBS_HEIGHT" --obstacle-speed "$OBS_SPEED")
    [[ -n "$SIDES" ]] && OBSTACLE_ARGS+=(--obstacle-sides "$SIDES")
fi

# -- 5) 로봇 대수 -------------------------------------------------------------

N="$(ask_int "로봇 대수" "1")"
if [[ "$N" -lt 1 ]]; then echo "1대 이상이어야 한다"; exit 1; fi

# -- 6) 로봇별 정보 입력 -------------------------------------------------------
# GUI(로봇 제어판)는 이제 진짜 Qt 창이라 별도 터미널이 필요 없다 -- 백그라운드로
# 띄워도 창은 화면에 알아서 뜬다. 그래서 헤드리스 로봇과 완전히 같은 방식(nohup)으로
# 실행하고, --gui 플래그만 붙는다.

declare -a IDS XS YS THETAS GUIS

echo
echo "=== 로봇 $N 대 정보 입력 ==="
for ((i = 0; i < N; i++)); do
    echo "-- 로봇 $((i + 1))/$N --"
    default_id="AMR-$(printf '%03d' $((i + 1)))"
    id="$(ask "로봇 ID" "$default_id")"
    x="$(ask_num "시작 x (m)" "0.0")"
    y="$(ask_num "시작 y (m)" "$i")"
    theta="$(ask_num "시작 theta (rad)" "0.0")"
    gui="$(ask_yn "이 로봇 제어판(GUI) 켤까요?" "n")"
    IDS+=("$id"); XS+=("$x"); YS+=("$y"); THETAS+=("$theta"); GUIS+=("$gui")
done

# -- 7) 커맨드 구성 + 요약 -----------------------------------------------------

COMMON_ARGS=(--host "$HOST" --port "$PORT" --robot-width "$ROBOT_WIDTH" --robot-length "$ROBOT_LENGTH"
             --max-velocity "$MAX_VELOCITY" --max-acceleration "$MAX_ACCEL" --battery "$BATTERY"
             --speed-noise "$SPEED_NOISE" --sensor-range "$SENSOR_RANGE" --log-level "$LOG_LEVEL")
[[ -n "$MAP_ARG" ]] && COMMON_ARGS+=(--map "$MAP_ARG")
COMMON_ARGS+=("${OBSTACLE_ARGS[@]}")

echo
echo "=== 실행 요약 ==="
for ((i = 0; i < N; i++)); do
    gui_note=""
    [[ "${GUIS[$i]}" == "y" ]] && gui_note=" [GUI 창]"
    echo "  ${IDS[$i]}  x=${XS[$i]} y=${YS[$i]} theta=${THETAS[$i]}$gui_note"
done
echo "  공통 옵션: ${COMMON_ARGS[*]}"
echo

if [[ "$(ask_yn "이대로 실행할까요?" "y")" != "y" ]]; then
    echo "취소함"; exit 0
fi

# -- 8) 실행 ------------------------------------------------------------------

for ((i = 0; i < N; i++)); do
    id="${IDS[$i]}"
    cmd=("$BIN" --id "$id" --x "${XS[$i]}" --y "${YS[$i]}" --theta "${THETAS[$i]}" "${COMMON_ARGS[@]}")
    [[ "${GUIS[$i]}" == "y" ]] && cmd+=(--gui)

    nohup "${cmd[@]}" > "$LOG_DIR/$id.log" 2>&1 &
    echo "$!" >> "$PIDS_FILE"
    if [[ "${GUIS[$i]}" == "y" ]]; then
        echo "[$id] 백그라운드 실행 (pid $!) -- 제어판 창이 화면에 뜬다"
    else
        echo "[$id] 백그라운드 실행 (pid $!, 로그: $LOG_DIR/$id.log)"
    fi
done

echo
echo "실행 완료. 헤드리스 로봇 로그: tail -f $LOG_DIR/*.log"
echo "Ctrl+C 로 전부 종료 (GUI 창도 같이 닫힌다)."

# -- 9) foreground 대기, Ctrl+C 시 정리 ----------------------------------------

cleanup() {
    echo
    echo "종료 신호 수신 -- 백그라운드 로봇 정리 중..."
    if [[ -s "$PIDS_FILE" ]]; then
        while IFS= read -r pid; do
            kill "$pid" 2>/dev/null
        done < "$PIDS_FILE"
    fi
    : > "$PIDS_FILE"
    echo "완료"
    exit 0
}
trap cleanup INT TERM

if [[ -s "$PIDS_FILE" ]]; then
    wait
else
    echo "(백그라운드 로봇 없음 -- GUI 로봇만 실행됨. 스크립트 종료)"
fi

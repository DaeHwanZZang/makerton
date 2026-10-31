#!/usr/bin/env bash
# launch.sh 가 백그라운드로 띄운 로봇들을 정지한다.
# launch.sh 를 Ctrl+C 없이 그냥 터미널을 닫아버린 경우 등을 대비한 별도 도구.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDS_FILE="$SCRIPT_DIR/.run/pids"

if [[ ! -s "$PIDS_FILE" ]]; then
    echo "종료할 로봇이 없다 (${PIDS_FILE} 비어있음)"
    exit 0
fi

count=0
while IFS= read -r pid; do
    [[ -z "$pid" ]] && continue
    if kill "$pid" 2>/dev/null; then
        echo "종료: pid $pid"
        count=$((count + 1))
    fi
done < "$PIDS_FILE"

: > "$PIDS_FILE"
echo "$count 개 프로세스 종료함"

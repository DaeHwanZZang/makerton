#!/usr/bin/env bash
# MuJoCo(헤더 + 공유 라이브러리)를 third_party/mujoco/ 에 설치한다. 리포 루트 어디서 실행해도 된다.
#   tools/setup_mujoco.sh                 # 기본 버전
#   MUJOCO_VERSION=3.16.0 tools/setup_mujoco.sh
# brew 패키지가 없어서 PyPI 휠(mujoco)에서 C 라이브러리만 꺼내 쓴다. 파이썬 바인딩은 쓰지 않는다.
# 휠은 플랫폼에 맞는 것을 pip 가 고른다 (macOS arm64/x86_64, Linux x86_64/aarch64).
set -euo pipefail

VERSION="${MUJOCO_VERSION:-3.16.0}"
cd "$(dirname "$0")/.."
DEST=third_party/mujoco

if [ -f "$DEST/include/mujoco/mujoco.h" ] && compgen -G "$DEST/lib/libmujoco*" >/dev/null; then
    echo "MuJoCo 이미 설치됨: $DEST (다시 받으려면 $DEST 를 지우고 실행)"
    exit 0
fi

PY=python3
[ -x .venv/bin/python ] && PY=.venv/bin/python

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "MuJoCo $VERSION 휠 받는 중..."
"$PY" -m pip download "mujoco==$VERSION" --no-deps --only-binary=:all: -d "$TMP" >/dev/null
unzip -q "$TMP"/mujoco-*.whl -d "$TMP/whl"

mkdir -p "$DEST/include" "$DEST/lib"
rm -rf "$DEST/include/mujoco"
cp -R "$TMP/whl/mujoco/include/mujoco" "$DEST/include/"

case "$(uname -s)" in
    Darwin)
        LIB="$(basename "$(ls "$TMP"/whl/mujoco/libmujoco.*.dylib | head -n 1)")"
        cp "$TMP/whl/mujoco/$LIB" "$DEST/lib/$LIB"
        # 휠의 install name 은 mujoco.framework 경로라 그대로는 링크가 안 된다. @rpath/<파일명> 으로 바꾼다
        install_name_tool -id "@rpath/$LIB" "$DEST/lib/$LIB"
        codesign -f -s - "$DEST/lib/$LIB" >/dev/null 2>&1
        ln -sf "$LIB" "$DEST/lib/libmujoco.dylib"
        ;;
    *)
        LIB="$(basename "$(ls "$TMP"/whl/mujoco/libmujoco.so.* | head -n 1)")"
        cp "$TMP/whl/mujoco/$LIB" "$DEST/lib/$LIB"
        ln -sf "$LIB" "$DEST/lib/libmujoco.so"
        ;;
esac

echo "설치 완료: $DEST ($LIB)"

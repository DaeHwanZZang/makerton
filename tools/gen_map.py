#!/usr/bin/env python3
"""
실제 SLAM 맵 조각으로 임의 맵 만들기
====================================

`maps/warehouse` 같은 손으로 그린 맵은 너무 정형화돼 있어서, 실제 센서 맵
(`maps/641931.../grid.png`)에서 1 m 타일 조각을 잘라 레이아웃대로 이어붙인다.

    .venv/bin/python tools/gen_map.py tools/map_layouts/loop.txt --out maps/gen_loop --seed 1   # 리포 루트에서

레이아웃 (텍스트, 한 글자 = 1 m 칸)
    .   통로 (FREE)
    #   막힘 (실제 맵처럼 회색 = 미탐색 으로 그려진다)
    ;   로 시작하는 줄은 주석. 짧은 줄은 # 로 채운다. 바깥 1칸은 자동으로 막는다.

타일링 (marching squares / dual grid)
    레이아웃 칸의 중심이 타일의 모서리에 온다. 타일 하나는 4개 사분면으로 나뉘고
    각 사분면은 가장 가까운 칸의 통로/막힘을 따른다. 그래서 통로와 막힘의 경계
    (= 벽)는 항상 타일의 가운데 선 위에 온다. 사분면 패턴 16가지는 회전을 빼면
    5종류다:
        EMPTY   전부 막힘          FULL   전부 통로
        EDGE    절반 통로 (일자벽)  OUTER  1/4 통로 (바깥 모서리, ㄱ자벽)
        INNER   3/4 통로 (안쪽 모서리)
    대각선 2칸만 통로인 SADDLE 은 원본에 거의 없어서 OUTER 두 장을 합성한다.

조각 추출
    원본을 1 px 간격으로 훑으며 사분면 패턴이 맞고, 경계가 가운데 선에 있고,
    그 선에 벽(OCCUPIED) 픽셀이 충분한 창만 고른다. 4방향 모두에서 찾은 뒤
    기준 방향으로 돌려 풀에 넣는다 (90도 회전으로 조각 수 4배).
    원본에는 깔끔한 모서리가 드물다 (grid.png 기준 OUTER 1, INNER 6 개). 그래서
    모서리 조각이 --min-real 개보다 적으면 그 비율만큼 EDGE 두 장(가로벽 + 세로벽)의
    절반씩을 붙인 합성 모서리를 섞는다.

밝기
    조각마다 통로 밝기가 달라 바둑판처럼 보이므로, 타일마다 FREE 픽셀 평균을
    FULL 조각들의 중앙값에 맞춘다. free_thresh 아래로는 안 내려서 셀 분류는 그대로다.

여백
    사방에 --margin-tiles (기본 10 = 320 px) 만큼 회색 128 여백을 붙인다. 검사는 여백 전 이미지로 한다.

좌표
    origin = (-여백 m, -여백 m) 이라 여백과 상관없이 레이아웃 (행 r, 열 c) 칸 중심이 world
    ((c+1)·tile_m, (r+1)·tile_m) 이다. 생성 후 통로 칸 좌표 예시와 robot_client 실행 예를 출력한다.

통로 노이즈
    planner 는 FREE 가 아닌 픽셀을 전부 로봇 반폭만큼 팽창시킨다. 통로 안에
    회색 점 하나만 있어도 지름 0.5 m 장애물이 되므로, 통로 사분면 안쪽(벽에서
    --margin-px 이상 떨어진 곳)에 FREE 가 아닌 픽셀이 있는 조각은 버린다.

검사
    결과를 planner 와 같은 방식(비-FREE 를 로봇 반폭 원으로 팽창)으로 팽창한 뒤,
    모든 통로 칸 중심이 주행 가능하고 하나로 이어지는지 본다. 실패하면 seed 를
    바꿔 --tries 번까지 다시 뽑는다.

노이즈 정리 (--clean light|normal|strong)
    붙인 결과에 clean_map.py 후처리를 돌린 뒤 검사한다. 레이저 줄무늬, 벽 바깥 털,
    통로 점을 지운다. 단계와 프리셋은 clean_map.py 참고. 기본 none (원본 질감 그대로).

출력 (--out 폴더)
    fms_map.json   MapModel 메타데이터 (클라이언트/뷰어/launch.sh 가 읽는다). image 는 edited_navi_gridmap.png
    grid.png       생성 결과 (8-bit 그레이스케일). 다시 생성하면 덮어쓴다
    edited_navi_gridmap.png  편집기/FMS 웹 UI 의 수정이 쓰이는 파일. 최초 생성 때만 grid.png 복사본으로 만들고
                   이후엔 건드리지 않는다 (지우고 생성하면 다시 만든다)
    layout.txt     입력 레이아웃 사본
    gen_info.json  재현용 정보 (원본, seed, 파라미터)

의존성: numpy, Pillow (viewer/requirements.txt 에 포함)
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import asdict
from pathlib import Path

import numpy as np
from PIL import Image

from clean_map import PRESETS, CleanOptions, add_option_args, clean, inflate_free, label, options_from_args

REPO = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = REPO / "maps/641931de9eae7cecb34d5765/fms_map.json"
DEFAULT_SOURCE_IMAGE = "grid.png"  # SLAM 원본. edited_navi_gridmap.png 는 사람이 덧칠한 것

EMPTY, FULL, EDGE, OUTER, INNER = "EMPTY", "FULL", "EDGE", "OUTER", "INNER"

# 기준 방향의 사분면 패턴 [[TL, TR], [BL, BR]] (True = 통로)
CANONICAL = {
    EMPTY: np.array([[0, 0], [0, 0]], bool),
    FULL: np.array([[1, 1], [1, 1]], bool),
    EDGE: np.array([[1, 1], [0, 0]], bool),   # 위쪽 절반 통로
    OUTER: np.array([[1, 0], [0, 0]], bool),  # 좌상 1/4 통로
    INNER: np.array([[1, 1], [1, 0]], bool),  # 우하 1/4 만 막힘
}


def pattern_key(p: np.ndarray) -> int:
    return int(p[0, 0]) << 3 | int(p[0, 1]) << 2 | int(p[1, 0]) << 1 | int(p[1, 1])


def build_orientations() -> dict[int, tuple[str, int]]:
    """사분면 패턴 -> (종류, 기준 방향에서 np.rot90 몇 번). SADDLE 2개는 빠진다."""
    table: dict[int, tuple[str, int]] = {}
    for kind, canon in CANONICAL.items():
        for k in range(4):
            table.setdefault(pattern_key(np.rot90(canon, k)), (kind, k))
    return table


ORIENT = build_orientations()
SADDLE_KEYS = {pattern_key(np.array([[1, 0], [0, 1]], bool)), pattern_key(np.array([[0, 1], [1, 0]], bool))}


# -- 레이아웃 ----------------------------------------------------------------

def load_layout(path: Path) -> np.ndarray:
    rows = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if not line or line.startswith(";"):
            continue
        bad = set(line) - {".", "#"}
        if bad:
            raise SystemExit(f"{path}: 레이아웃에 쓸 수 없는 글자 {sorted(bad)} (. 또는 # 만)")
        rows.append(line)
    if not rows:
        raise SystemExit(f"{path}: 레이아웃이 비었다")
    w = max(len(r) for r in rows)
    grid = np.array([[c == "." for c in r.ljust(w, "#")] for r in rows], bool)
    if not grid.any():
        raise SystemExit(f"{path}: 통로(.) 가 하나도 없다")
    return grid


# -- 원본에서 조각 추출 --------------------------------------------------------

class Source:
    def __init__(self, meta_path: Path, image_name: str):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        self.meta_path = meta_path
        self.resolution = float(meta["resolution"])
        self.occupied_thresh = int(meta.get("occupied_thresh", 64))
        self.free_thresh = int(meta.get("free_thresh", 192))
        self.image_path = meta_path.parent / image_name
        grey = np.asarray(Image.open(self.image_path).convert("L"), dtype=np.uint8)
        # 탐색된 영역(완전 회색 128 이 아닌 곳) 둘레만 훑는다. 나머지는 쓸모없고 느리다
        ys, xs = np.nonzero(grey != 128)
        pad = 64
        y0, y1 = max(0, ys.min() - pad), min(grey.shape[0], ys.max() + pad + 1)
        x0, x1 = max(0, xs.min() - pad), min(grey.shape[1], xs.max() + pad + 1)
        self.grey = grey[y0:y1, x0:x1]
        self.free = self.grey >= self.free_thresh
        self.occ = self.grey <= self.occupied_thresh
        self.I_free = integral(self.free)
        self.I_nonfree = integral(~self.free)
        self.I_occ = integral(self.occ)


def integral(a: np.ndarray) -> np.ndarray:
    out = np.zeros((a.shape[0] + 1, a.shape[1] + 1), np.int32)
    out[1:, 1:] = a.astype(np.int32).cumsum(0).cumsum(1)
    return out


def rect_sums(I: np.ndarray, T: int, y0: int, x0: int, y1: int, x1: int) -> np.ndarray:
    """모든 T×T 창(좌상단 기준)에 대해 창 안 [y0,y1)×[x0,x1) 사각형의 합."""
    ny, nx = I.shape[0] - T, I.shape[1] - T
    return (I[y1:y1 + ny, x1:x1 + nx] - I[y0:y0 + ny, x1:x1 + nx]
            - I[y1:y1 + ny, x0:x0 + nx] + I[y0:y0 + ny, x0:x0 + nx])


def find_windows(src: Source, pattern: np.ndarray, T: int, args) -> np.ndarray:
    """사분면 패턴에 맞는 창의 좌상단 좌표들 (N, 2) [y, x]."""
    h = T // 2
    m = args.margin_px
    wb = args.wall_band_px
    ok = np.ones((src.grey.shape[0] - T + 1, src.grey.shape[1] - T + 1), bool)

    for qi in range(2):
        for qj in range(2):
            qy0, qx0 = qi * h, qj * h
            qy1, qx1 = qy0 + h, qx0 + h
            if pattern[qi, qj]:
                # 통로 사분면: 막힘 쪽 경계에서 margin 만큼 뺀 안쪽은 전부 FREE 여야 한다
                if not pattern[1 - qi, qj]:
                    if qi == 0: qy1 -= m
                    else: qy0 += m
                if not pattern[qi, 1 - qj]:
                    if qj == 0: qx1 -= m
                    else: qx0 += m
                ok &= rect_sums(src.I_nonfree, T, qy0, qx0, qy1, qx1) <= args.max_noise
                # 경계 쪽 띠도 대부분 FREE (경계가 가운데 선보다 통로 쪽으로 밀려 있으면 안 됨)
                ok &= rect_sums(src.I_free, T, qi * h, qj * h, qi * h + h, qj * h + h) >= 0.9 * h * h
            else:
                # 막힘 사분면: FREE 가 거의 없어야 한다 (레이저 줄무늬 정도만 허용)
                ok &= rect_sums(src.I_free, T, qy0, qx0, qy1, qx1) <= args.unknown_free_max * h * h

    # 통로/막힘이 갈리는 가운데 선 반쪽마다 벽 픽셀이 있어야 한다
    for qj in range(2):  # 가로 경계: 위아래 사분면이 다른 열
        if pattern[0, qj] != pattern[1, qj]:
            ok &= rect_sums(src.I_occ, T, h - wb, qj * h, h + wb, qj * h + h) >= args.wall_min * h
    for qi in range(2):  # 세로 경계
        if pattern[qi, 0] != pattern[qi, 1]:
            ok &= rect_sums(src.I_occ, T, qi * h, h - wb, qi * h + h, h + wb) >= args.wall_min * h
    if not pattern.any():
        ok &= rect_sums(src.I_occ, T, 0, 0, T, T) <= 0.01 * T * T  # 빈 타일에 벽 조각이 끼면 이상하다

    return np.argwhere(ok)


def thin_out(points: np.ndarray, step: int, rng: np.random.Generator) -> np.ndarray:
    """거의 같은 조각이 몰리지 않게, step 칸 격자 한 칸에 하나만 남긴다."""
    if len(points) == 0:
        return points
    points = points[rng.permutation(len(points))]
    _, first = np.unique(points // step, axis=0, return_index=True)
    return points[np.sort(first)]


def build_pools(src: Source, T: int, args, rng: np.random.Generator) -> dict[str, list[np.ndarray]]:
    pools: dict[str, list[np.ndarray]] = {kind: [] for kind in CANONICAL}
    for kind, canon in CANONICAL.items():
        for k in range(4):
            pattern = np.rot90(canon, k)
            if k and kind in (EMPTY, FULL):
                continue  # 회전 대칭. 대신 아래에서 조각을 돌려 쓴다
            for y, x in thin_out(find_windows(src, pattern, T, args), T // 2, rng):
                patch = src.grey[y:y + T, x:x + T]
                # 기준 방향으로 되돌려 저장 (rot90(canon, k) 를 -k 번 돌리면 canon)
                pools[kind].append(np.rot90(patch, -k).copy())
    return pools


# -- 조립 --------------------------------------------------------------------

class Composer:
    """조각 풀에서 타일을 골라 붙인다. 모서리 조각이 모자라면 EDGE 두 장으로 합성한다."""

    def __init__(self, pools, T: int, src: Source, min_real: int, rng: np.random.Generator):
        self.pools, self.T, self.h = pools, T, T // 2
        self.occupied_thresh, self.free_thresh = src.occupied_thresh, src.free_thresh
        self.min_real = min_real
        self.rng = rng
        # 통로 밝기 기준: FULL 조각들의 FREE 픽셀 평균의 중앙값
        means = [p[p >= self.free_thresh].mean() for p in pools[FULL]]
        self.free_target = float(np.median(means)) if means else 250.0

    def oriented(self, pattern) -> np.ndarray:
        kind, k = ORIENT[pattern_key(np.array(pattern, bool))]
        return np.rot90(self.canonical(kind), k)

    def canonical(self, kind: str) -> np.ndarray:
        pool = self.pools[kind]
        if kind in (OUTER, INNER):
            # 원본에 모서리가 드물다. 풀이 min_real 보다 작으면 그 비율만큼만 실제 조각을 쓴다
            if not pool or self.rng.random() >= len(pool) / self.min_real:
                return self.synth_corner(kind)
        patch = pool[self.rng.integers(len(pool))]
        if kind in (EMPTY, FULL):
            patch = np.rot90(patch, self.rng.integers(4))  # 대칭 타일은 아무 방향으로
        return patch

    def synth_corner(self, kind: str) -> np.ndarray:
        """기준 방향 모서리를 EDGE 두 장(가로벽 A, 세로벽 B)의 절반씩으로 만든다.
        사분면은 영역별로 가져오고, 벽 픽셀은 쓴 절반에서 모두 덧그린다."""
        h = self.h
        a = self.oriented([[1, 1], [0, 0]])  # 위 통로, 가운데 가로벽
        b = self.oriented([[1, 0], [1, 0]])  # 왼쪽 통로, 가운데 세로벽
        if kind == OUTER:  # 좌상만 통로
            out = self.canonical(EMPTY).copy()
            out[:, :h] = a[:, :h]       # TL, BL: A 의 왼쪽 절반 (TL 아래에 가로벽)
            out[:h, h:] = b[:h, h:]     # TR: B 의 위쪽 절반 (TL 오른쪽 세로벽 바깥)
            a_half, b_half = (slice(None), slice(0, h)), (slice(0, h), slice(None))
        else:  # INNER: 우하만 막힘
            out = self.canonical(FULL).copy()  # TL 은 벽 없는 통로여야 해서 FULL 에서
            out[:, h:] = a[:, h:]       # TR, BR: A 의 오른쪽 절반 (TR 아래에 가로벽)
            out[h:, :h] = b[h:, :h]     # BL: B 의 아래쪽 절반 (BL 오른쪽에 세로벽)
            a_half, b_half = (slice(None), slice(h, None)), (slice(h, None), slice(None))
        for src, half in ((a, a_half), (b, b_half)):
            region = src[half]
            wall = region <= self.occupied_thresh
            out[half][wall] = region[wall]
        return out

    def saddle(self, pattern: np.ndarray) -> np.ndarray:
        """대각선 2칸 통로. OUTER 두 장을 사분면 단위로 합친다 (벽 픽셀은 둘 다 살린다)."""
        h = self.h
        (ai, aj), (bi, bj) = [(qi, qj) for qi in range(2) for qj in range(2) if pattern[qi, qj]]
        one = lambda i, j: [[(i, j) == (0, 0), (i, j) == (0, 1)], [(i, j) == (1, 0), (i, j) == (1, 1)]]
        a, b = self.oriented(one(ai, aj)), self.oriented(one(bi, bj))
        out = a.copy()
        out[bi * h:bi * h + h, bj * h:bj * h + h] = b[bi * h:bi * h + h, bj * h:bj * h + h]
        wall = b <= self.occupied_thresh
        out[wall] = b[wall]
        return out

    def tone(self, tile: np.ndarray) -> np.ndarray:
        """타일마다 통로 밝기가 달라 바둑판처럼 보이는 걸 줄인다. FREE 픽셀만 평균을
        맞추고 free_thresh 아래로는 안 내린다 (셀 분류는 그대로)."""
        free = tile >= self.free_thresh
        if not free.any():
            return tile
        shifted = tile[free].astype(np.int16) + round(self.free_target - tile[free].mean())
        out = tile.copy()
        out[free] = np.clip(shifted, self.free_thresh, 255).astype(np.uint8)
        return out

    def compose(self, layout: np.ndarray) -> np.ndarray:
        T = self.T
        padded = np.pad(layout, 1, constant_values=False)
        rows, cols = padded.shape[0] - 1, padded.shape[1] - 1
        img = np.empty((rows * T, cols * T), np.uint8)
        for r in range(rows):
            for c in range(cols):
                pattern = padded[r:r + 2, c:c + 2]
                tile = self.saddle(pattern) if pattern_key(pattern) in SADDLE_KEYS else self.oriented(pattern)
                img[r * T:(r + 1) * T, c * T:(c + 1) * T] = self.tone(tile)
        return img


# -- 검사 (planner.cpp 의 inflate 와 같은 규칙, inflate_free 는 clean_map.py) --------

def check(img: np.ndarray, layout: np.ndarray, T: int, free_thresh: int, radius_px: int) -> list[str]:
    drivable = inflate_free(img >= free_thresh, radius_px)
    labels, _ = label(drivable)  # 대각 이동은 두 직교 이웃이 모두 주행 가능해야 해서 4-연결과 같다
    problems = []
    comps = set()
    for r, c in np.argwhere(layout):
        py, px = (r + 1) * T, (c + 1) * T  # 칸 중심 = 타일 모서리
        lab = labels[py, px]
        if not lab:
            problems.append(f"칸 (행 {r}, 열 {c}) 중심이 로봇 반폭 팽창 후 주행 불가")
        else:
            comps.add(lab)
    if len(comps) > 1:
        problems.append(f"통로가 {len(comps)} 조각으로 끊겼다")
    return problems


def save_map(out: Path, name: str | None, img: np.ndarray, layout: np.ndarray, layout_path: Path, T: int,
             resolution: float, occupied_thresh: int, free_thresh: int, info: dict, margin_tiles: int = 10,
             pad_margin: bool = True) -> None:
    """grid.png / edited_navi_gridmap.png / fms_map.json / layout.txt / gen_info.json 을 쓰고 좌표 예시를 출력한다.
    synth_map.py 도 같이 쓴다.

    - 사방에 margin_tiles 타일(기본 10 = 320 px = 10 m)만큼 회색(미탐색) 여백을 붙인다. origin 을
      -margin 으로 둬서 레이아웃 칸 중심의 world 좌표는 여백과 상관없이 ((c+1)·tile, (r+1)·tile) 그대로다.
    - edited_navi_gridmap.png (맵 편집기 / FMS 웹 UI 가 수정사항을 쓰는 파일, 메타의 image 가 가리킨다)는
      최초 생성 때만 grid.png 복사본으로 만든다. 이미 있으면 사람 수정을 지우지 않으려고 건드리지 않는다.
    """
    out.mkdir(parents=True, exist_ok=True)
    m = margin_tiles * T
    if pad_margin:  # False: 호출한 쪽이 이미 여백을 포함해서 그렸다 (synth_map)
        img = np.pad(img, m, constant_values=128)
    h_px, w_px = img.shape
    # origin = 이미지 좌상단 픽셀의 world 좌표 (map_model 규약). 여백만큼 음수지만 칸 중심 좌표는 양수다.
    # (send_order --path 는 '-' 로 시작하는 인자를 옵션으로 봐서 음수 좌표를 못 받는다)
    origin_x = origin_y = round(-m * resolution, 6)
    Image.fromarray(img, "L").save(out / "grid.png")
    edited = out / "edited_navi_gridmap.png"
    info["edited_created"] = not edited.exists()
    if info["edited_created"]:
        shutil.copyfile(out / "grid.png", edited)
    else:
        w_old, h_old = Image.open(edited).size
        if (w_old, h_old) != (w_px, h_px):
            print(f"경고: 기존 {edited.name} 크기 {w_old}×{h_old} 가 새 grid.png {w_px}×{h_px} 와 다르다. "
                  f"origin/크기가 안 맞으니 {edited.name} 을 지우고 다시 생성하라", file=sys.stderr)
        w_px, h_px = w_old, h_old
    info["margin_tiles"] = margin_tiles
    meta = {
        "name": name or out.name,
        "image": edited.name,
        "resolution": resolution,
        "origin_x": origin_x,
        "origin_y": origin_y,
        "width_px": w_px,
        "height_px": h_px,
        "occupied_thresh": occupied_thresh,
        "free_thresh": free_thresh,
    }
    (out / "fms_map.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    shutil.copyfile(layout_path, out / "layout.txt")
    (out / "gen_info.json").write_text(json.dumps(info, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    tile_m = T * resolution
    spots = [((c + 1) * tile_m, (r + 1) * tile_m) for r, c in np.argwhere(layout)]
    print(f"저장: {out}/fms_map.json ({w_px * resolution:.1f} m × {h_px * resolution:.1f} m, 여백 {margin_tiles} 타일, "
          f"seed {info.get('seed')}, edited_navi_gridmap.png {'새로 생성' if info['edited_created'] else '기존 유지'})")
    print("통로 칸 중심 예시 (world x, y):", ", ".join(f"({x:.1f}, {y:.1f})" for x, y in spots[:: max(1, len(spots) // 5)][:5]))
    x, y = spots[0]
    print(f"실행 예: ./robot_client --id AMR-001 --map {out}/fms_map.json --x {x:.1f} --y {y:.1f}")


# -- main --------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="실제 SLAM 맵 조각으로 임의 맵 생성")
    ap.add_argument("layout", type=Path, help="레이아웃 텍스트 (. 통로 / # 막힘, 한 글자 = 1 타일)")
    ap.add_argument("--out", type=Path, required=True, help="출력 폴더 (예: maps/gen_loop)")
    ap.add_argument("--name", help="맵 이름 (기본: 출력 폴더 이름)")
    ap.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="원본 맵 메타데이터 json")
    ap.add_argument("--source-image", default=DEFAULT_SOURCE_IMAGE, help="원본 이미지 (메타 json 기준 상대경로)")
    ap.add_argument("--tile-m", type=float, default=1.0, help="타일 한 변 (m)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--tries", type=int, default=20, help="검사 실패 시 seed+1, +2... 로 다시 뽑는 횟수")
    ap.add_argument("--robot-width", type=float, default=0.5, help="검사용 로봇 폭 (robot_client 기본값과 같음)")
    ap.add_argument("--margin-px", type=int, default=4, help="통로 사분면에서 벽 쪽 노이즈 허용 폭")
    ap.add_argument("--max-noise", type=int, default=0, help="통로 사분면 안쪽에 허용할 비-FREE 픽셀 수")
    ap.add_argument("--wall-band-px", type=int, default=3, help="가운데 선 ± 이 폭 안에서 벽 픽셀을 센다")
    ap.add_argument("--wall-min", type=float, default=0.5, help="경계 길이 대비 벽 픽셀 비율 최소값")
    ap.add_argument("--unknown-free-max", type=float, default=0.08, help="막힘 사분면의 FREE 픽셀 비율 최대값")
    ap.add_argument("--min-real", type=int, default=10, help="모서리 실제 조각이 이보다 적으면 합성 조각을 섞는다")
    ap.add_argument("--margin-tiles", type=int, default=10, help="사방 회색(미탐색) 여백 타일 수 (10 = 320 px = 10 m)")
    ap.add_argument("--force", action="store_true", help="검사에 실패해도 마지막 결과를 저장")
    ap.add_argument("--clean", choices=["none", *sorted(PRESETS)], default="none",
                    help="생성 후 노이즈 정리 프리셋 (clean_map.py)")
    add_option_args(ap.add_argument_group("정리 옵션 (프리셋 값을 덮어쓴다)"), prefix="clean-")
    args = ap.parse_args()
    clean_opt: CleanOptions | None = None if args.clean == "none" else options_from_args(args, args.clean, "clean-")

    src = Source(args.source, args.source_image)
    T = round(args.tile_m / src.resolution)
    if T < 8 or T % 2:
        raise SystemExit(f"타일 픽셀 수 {T} 가 짝수가 아니거나 너무 작다 (tile-m / resolution)")
    if abs(T * src.resolution - args.tile_m) > 1e-6:
        print(f"경고: 타일 {args.tile_m} m 가 픽셀로 딱 안 나뉜다. 실제 {T * src.resolution:.4f} m", file=sys.stderr)

    layout = load_layout(args.layout)
    pools = build_pools(src, T, args, np.random.default_rng(args.seed))
    print("조각 수:", ", ".join(f"{k} {len(v)}" for k, v in pools.items()),
          f"(OUTER/INNER 가 {args.min_real} 개 미만이면 EDGE 로 합성해 섞는다)")
    missing = [k for k in (EMPTY, FULL, EDGE) if not pools[k]]  # 모서리는 EDGE 로 합성 가능
    if missing:
        raise SystemExit(f"원본에서 {missing} 조각을 못 찾았다. --wall-min / --max-noise 를 완화해 보라")

    radius_px = int(np.ceil((args.robot_width / 2) / src.resolution))
    img, problems, seed = None, [], args.seed
    for i in range(max(1, args.tries)):
        seed = args.seed + i
        img = Composer(pools, T, src, args.min_real, np.random.default_rng(seed)).compose(layout)
        if clean_opt:
            img, clean_stats = clean(img, src.occupied_thresh, src.free_thresh, clean_opt)
        problems = check(img, layout, T, src.free_thresh, radius_px)
        if not problems:
            break
        print(f"seed {seed}: 검사 실패 ({problems[0]}{' 외' if len(problems) > 1 else ''})")
    if problems:
        print("\n".join(problems[:10]), file=sys.stderr)
        if not args.force:
            print("검사를 통과한 결과가 없다. 레이아웃 통로를 넓히거나 --force 로 저장", file=sys.stderr)
            return 1

    info = {
        "source": str(src.image_path.relative_to(REPO)) if src.image_path.is_relative_to(REPO) else str(src.image_path),
        "seed": seed,
        "tile_m": T * src.resolution,
        "tile_px": T,
        "check_passed": not problems,
        "clean": {"level": args.clean, **asdict(clean_opt)} if clean_opt else None,
        "params": {k: getattr(args, k) for k in
                   ("robot_width", "margin_px", "max_noise", "wall_band_px", "wall_min", "unknown_free_max",
                    "min_real")},
    }
    if clean_opt:
        print("정리 (" + args.clean + "):", ", ".join(f"{k} {v}" for k, v in clean_stats.items()), "(px)")
    save_map(args.out, args.name, img, layout, args.layout, T, src.resolution, src.occupied_thresh,
             src.free_thresh, info, args.margin_tiles)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
맵 노이즈 정리 (후처리)
=======================

SLAM 맵(또는 gen_map.py 결과)의 지저분한 부분을 지운다. 셀 분류
(FREE >= free_thresh, OCCUPIED <= occupied_thresh, 그 사이 UNKNOWN) 기준으로만 판단한다.

    .venv/bin/python tools/clean_map.py maps/gen_loop/fms_map.json --out maps/gen_loop_clean   # 리포 루트에서
    .venv/bin/python tools/gen_map.py LAYOUT --out DIR --clean normal                          # 생성 직후 적용

단계 (이 순서로 돈다)
    1. 통로 구멍 메우기    FREE 로 둘러싸인 작은 비-FREE 덩어리(8-연결, --fill-hole-px 이하)를
                           통로 색으로. planner 는 비-FREE 를 전부 팽창하므로 점 하나가
                           지름 0.5 m 장애물이 된다
    2. 털 깎기 (opening)   FREE 를 --open-px 만큼 깎았다 다시 키운다. 벽 틈으로 삐져나간
                           폭 2·open-px 이하의 가는 FREE 가 사라진다
    3. 줄무늬 지우기       --min-free-px 보다 작은 FREE 섬(4-연결)을 UNKNOWN 으로.
                           막힘 영역 안의 레이저 줄무늬가 여기서 지워진다
    4. 벽 띠 밖 정리       FREE 에서 --wall-reach-px 보다 먼 비-FREE 를 전부 회색 128 로.
                           벽 바깥으로 튄 검은 털, 막힘 영역 안의 옅은 회색 줄무늬
                           (UNKNOWN 값이라 3 에 안 걸린다)가 사라진다. 실제 벽은 FREE 에서
                           1~5 px 안에 있어서(gen_loop 기준) 6 px 이면 벽과 그 앞 회색 테두리는 남는다
    5. 벽 잇기 (선택)      FREE 에서 2 px 안의 비-FREE 를 전부 OCCUPIED 로. 점선 벽이 실선이 된다
    6. 평탄화 (선택)       FREE 254, UNKNOWN 128, OCCUPIED 0 으로 통일. 타일 경계 밝기 차이도 사라진다

프리셋 (--level, 개별 옵션으로 덮어쓸 수 있다)
    light    1, 3, 4  통로 점, 막힘 영역 줄무늬, 멀리 튄 점만. 벽 털은 그대로
    normal   1~4      + 벽 틈으로 삐져나간 FREE 털. 점선 벽과 통로 질감은 남는다 (실제 맵 느낌 유지)
    strong   1~6      원래 벽은 지우고 통로 경계에 실선 벽. 손으로 그린 맵에 가깝다

주행 영향
    2, 3 은 FREE 를 줄인다. 그래서 정리 전후로 planner 규칙(비-FREE 를 로봇 반폭 원으로
    팽창)의 주행 가능 영역을 비교해 출력한다. gen_map.py --clean 은 정리 뒤에 레이아웃 검사를 돈다.

의존성: numpy, Pillow (scipy 없이 morphology/라벨링을 직접 구현)
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import numpy as np
from PIL import Image


# -- morphology / 라벨링 ------------------------------------------------------

def _grow(a: np.ndarray, r: int, axis: int) -> np.ndarray:
    out = a.copy()
    for d in range(1, r + 1):
        if axis == 0:
            out[d:] |= a[:-d]
            out[:-d] |= a[d:]
        else:
            out[:, d:] |= a[:, :-d]
            out[:, :-d] |= a[:, d:]
    return out


def dilate(mask: np.ndarray, r: int) -> np.ndarray:
    """(2r+1)×(2r+1) 정사각형 팽창. 정사각형이라 직각 모서리가 opening 후에도 안 깎인다."""
    return _grow(_grow(mask, r, 0), r, 1) if r > 0 else mask.copy()


def erode(mask: np.ndarray, r: int) -> np.ndarray:
    return ~dilate(~mask, r)


def label(mask: np.ndarray, conn8: bool = False) -> tuple[np.ndarray, int]:
    """연결 성분 라벨링 (run-length + union-find). 픽셀 단위 flood fill 보다 훨씬 빨라서
    2347×1687 원본도 몇 초 안에 끝난다. 반환: (라벨 배열, 성분 수), 배경 0."""
    H, W = mask.shape
    m = np.zeros((H, W + 2), np.int8)
    m[:, 1:-1] = mask
    d = np.diff(m, axis=1)
    rows, starts = np.nonzero(d == 1)
    _, ends = np.nonzero(d == -1)  # 같은 행 우선 순서라 starts 와 짝이 맞는다 (끝은 exclusive)
    n_runs = len(rows)
    labels = np.zeros((H, W), np.int32)
    if n_runs == 0:
        return labels, 0

    parent = list(range(n_runs))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    row_first = np.searchsorted(rows, np.arange(H + 1)).tolist()
    s_l, e_l = starts.tolist(), ends.tolist()
    k = 1 if conn8 else 0  # 8-연결이면 대각으로 맞닿은 run 도 잇는다
    for r in range(1, H):
        i, i_end = row_first[r - 1], row_first[r]
        j, j_end = row_first[r], row_first[r + 1]
        while i < i_end and j < j_end:
            if s_l[j] < e_l[i] + k and s_l[i] < e_l[j] + k:
                a, b = find(i), find(j)
                if a != b:
                    parent[max(a, b)] = min(a, b)
            if e_l[i] < e_l[j]:
                i += 1
            else:
                j += 1

    roots = np.array([find(i) for i in range(n_runs)])
    _, run_label = np.unique(roots, return_inverse=True)
    run_label = (run_label + 1).tolist()
    for idx in range(n_runs):
        labels[rows[idx], s_l[idx]:e_l[idx]] = run_label[idx]
    return labels, int(max(run_label))


def inflate_free(free: np.ndarray, radius_px: int) -> np.ndarray:
    """planner.cpp 의 inflate 와 같은 규칙: 비-FREE 를 반경 radius_px 원으로 팽창한 뒤 남은 FREE.
    planner 규칙이 바뀌면 같이 고친다."""
    blocked = ~free
    out = blocked.copy()
    H, W = free.shape
    for dy in range(-radius_px, radius_px + 1):
        for dx in range(-radius_px, radius_px + 1):
            if dx * dx + dy * dy > radius_px * radius_px or (dx == 0 and dy == 0):
                continue
            # 거리 radius 안에 막힌 픽셀이 있으면 막힘
            out[max(0, -dy):H - max(0, dy), max(0, -dx):W - max(0, dx)] |= \
                blocked[max(0, dy):H - max(0, -dy), max(0, dx):W - max(0, -dx)]
    return ~out


# -- 정리 --------------------------------------------------------------------

@dataclass(frozen=True)
class CleanOptions:
    fill_hole_px: int = 0      # 1. 이 크기 이하 통로 구멍을 메운다 (0 = 안 함)
    open_px: int = 0           # 2. FREE opening 반경 (0 = 안 함)
    min_free_px: int = 0       # 3. 이보다 작은 FREE 섬을 지운다 (0 = 안 함)
    wall_reach_px: int = 0     # 4. FREE 에서 이보다 먼 비-FREE 를 128 로 (0 = 안 함)
    solid_walls: bool = False  # 5. 벽 잇기
    flatten: bool = False      # 6. 값 평탄화


PRESETS = {
    "light": CleanOptions(fill_hole_px=64, min_free_px=256, wall_reach_px=8),
    "normal": CleanOptions(fill_hole_px=64, open_px=1, min_free_px=256, wall_reach_px=6),
    "strong": CleanOptions(fill_hole_px=256, open_px=2, min_free_px=1024, wall_reach_px=1,
                           solid_walls=True, flatten=True),  # 원래 벽은 지우고 5 에서 새로 긋는다
}
UNKNOWN_VALUE, FREE_FLAT, OCC_FLAT = 128, 254, 0


def clean(img: np.ndarray, occupied_thresh: int, free_thresh: int, opt: CleanOptions) -> tuple[np.ndarray, dict]:
    """반환: (정리된 이미지, 단계별 바뀐 픽셀 수)."""
    out = img.copy()
    stats: dict[str, int] = {}
    free = out >= free_thresh
    free_fill = int(np.median(out[free])) if free.any() else FREE_FLAT

    if opt.fill_hole_px > 0:
        # 비-FREE 는 8-연결로 묶는다. 대각으로만 큰 회색 영역에 닿은 점은 구멍이 아니다
        # (4-연결로 묶으면 벽 틈 너머 회색까지 통로로 메워질 수 있다)
        lab, _ = label(~free, conn8=True)
        sizes = np.bincount(lab.ravel())
        border = np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))
        small = sizes <= opt.fill_hole_px
        small[0] = False
        small[border] = False
        hole = small[lab]
        out[hole] = free_fill
        free |= hole
        stats["통로 구멍 메움"] = int(hole.sum())

    if opt.open_px > 0:
        opened = dilate(erode(free, opt.open_px), opt.open_px) & free
        cut = free & ~opened
        out[cut] = UNKNOWN_VALUE
        free = opened
        stats["털 깎음 (FREE→UNKNOWN)"] = int(cut.sum())

    if opt.min_free_px > 0:
        lab, _ = label(free)
        sizes = np.bincount(lab.ravel())
        small = sizes < opt.min_free_px
        small[0] = False
        speck = small[lab]
        out[speck] = UNKNOWN_VALUE
        free &= ~speck
        stats["FREE 섬 지움"] = int(speck.sum())

    occ = out <= occupied_thresh
    wall_value = int(np.median(out[occ])) if occ.any() else OCC_FLAT
    if opt.wall_reach_px > 0:
        far = ~free & ~dilate(free, opt.wall_reach_px) & (out != UNKNOWN_VALUE)
        out[far] = UNKNOWN_VALUE
        occ &= ~far
        stats["벽 띠 밖 정리"] = int(far.sum())

    if opt.solid_walls:
        rim = ~free & ~occ & dilate(free, 2)
        out[rim] = wall_value
        occ |= rim
        stats["벽 이음"] = int(rim.sum())

    if opt.flatten:
        before = out.copy()
        out[:] = UNKNOWN_VALUE
        out[free] = FREE_FLAT
        out[occ] = OCC_FLAT
        stats["평탄화"] = int((before != out).sum())

    return out, stats


def drivable_summary(img: np.ndarray, free_thresh: int, radius_px: int) -> tuple[int, int]:
    """(주행 가능 픽셀 수, 가장 큰 연결 영역 픽셀 수). 정리 전후 비교용."""
    lab, n = label(inflate_free(img >= free_thresh, radius_px))
    if n == 0:
        return 0, 0
    sizes = np.bincount(lab.ravel())[1:]
    return int(sizes.sum()), int(sizes.max())


def add_option_args(ap: argparse.ArgumentParser, prefix: str = "") -> None:
    """개별 옵션. 주면 프리셋 값을 덮어쓴다."""
    ap.add_argument(f"--{prefix}fill-hole-px", type=int, help="1. 이 크기(px) 이하 통로 구멍을 메운다")
    ap.add_argument(f"--{prefix}open-px", type=int, help="2. FREE opening 반경 px (털 깎기)")
    ap.add_argument(f"--{prefix}min-free-px", type=int, help="3. 이보다 작은 FREE 섬(px)을 지운다")
    ap.add_argument(f"--{prefix}wall-reach-px", type=int, help="4. FREE 에서 이 px 보다 먼 비-FREE 를 회색 128 로")
    ap.add_argument(f"--{prefix}solid-walls", action=argparse.BooleanOptionalAction, default=None, help="5. 벽 잇기")
    ap.add_argument(f"--{prefix}flatten", action=argparse.BooleanOptionalAction, default=None, help="6. 값 평탄화")


def options_from_args(args, level: str, prefix: str = "") -> CleanOptions:
    opt = PRESETS[level]
    over = {}
    for field in asdict(opt):
        v = getattr(args, prefix.replace("-", "_") + field, None)
        if v is not None:
            over[field] = v
    return replace(opt, **over)


def print_stats(stats: dict, before: tuple[int, int], after: tuple[int, int], resolution: float) -> None:
    print("정리:", ", ".join(f"{k} {v}" for k, v in stats.items()) or "바뀐 것 없음", "(px)")
    a = resolution * resolution
    print(f"주행 가능 영역 (planner 팽창 후): {before[0] * a:.1f} → {after[0] * a:.1f} m², "
          f"가장 큰 연결 영역 {before[1] * a:.1f} → {after[1] * a:.1f} m²")


def main() -> int:
    ap = argparse.ArgumentParser(description="맵 노이즈 정리 (후처리)")
    ap.add_argument("map", type=Path, help="맵 메타데이터 json (fms_map.json 등)")
    ap.add_argument("--out", type=Path, required=True, help="출력 폴더. 메타 json + 정리된 이미지를 쓴다")
    ap.add_argument("--level", choices=sorted(PRESETS), default="normal")
    ap.add_argument("--robot-width", type=float, default=0.5, help="주행 영역 비교용 로봇 폭 (robot_client 기본값)")
    ap.add_argument("--overwrite", action="store_true", help="--out 이 원본 폴더여도 이미지를 덮어쓴다")
    add_option_args(ap)
    args = ap.parse_args()

    meta = json.loads(args.map.read_text(encoding="utf-8"))
    src_img = args.map.parent / meta["image"]
    if args.out.resolve() == args.map.parent.resolve() and not args.overwrite:
        raise SystemExit("--out 이 원본 폴더다. 원본 이미지를 덮어쓰려면 --overwrite")

    img = np.asarray(Image.open(src_img).convert("L"), dtype=np.uint8)
    occ_t, free_t = int(meta.get("occupied_thresh", 64)), int(meta.get("free_thresh", 192))
    res = float(meta["resolution"])
    radius_px = int(np.ceil((args.robot_width / 2) / res))
    opt = options_from_args(args, args.level)

    cleaned, stats = clean(img, occ_t, free_t, opt)
    print_stats(stats, drivable_summary(img, free_t, radius_px), drivable_summary(cleaned, free_t, radius_px), res)

    args.out.mkdir(parents=True, exist_ok=True)
    Image.fromarray(cleaned, "L").save(args.out / Path(meta["image"]).name)
    meta["image"] = Path(meta["image"]).name
    out_meta = args.out / args.map.name
    out_meta.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"저장: {out_meta} (level {args.level}, {asdict(opt)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

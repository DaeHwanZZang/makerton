#!/usr/bin/env python3
"""
레이아웃에서 맵 전체를 한 번에 그리기 (원본 맵 조각 없이)
=========================================================

gen_map.py 는 실제 맵 조각을 이어붙여서 조각 경계마다 벽 위치/노이즈가 어긋난다.
여기서는 레이아웃 마스크 전체에 벽을 긋고 질감 노이즈를 맵 단위로 한 번에 깔아서
이음매가 원리적으로 없다. 모서리도 따로 처리할 필요가 없다.

    .venv/bin/python tools/synth_map.py tools/map_layouts/loop.txt --out maps/synth_loop --seed 1   # 리포 루트에서

레이아웃 형식, 타일 배치(칸 중심 = 타일 모서리), 좌표(origin 0,0), 검사, 출력 파일은
gen_map.py 와 같다. 같은 레이아웃이면 두 결과를 겹쳐 비교할 수 있다.

벽
    통로/막힘 경계 양쪽 --wall-px/2 씩. 기본 2 px 이면 32 px 타일에서 1 부터 센 16, 17 번
    픽셀(경계 선 바로 양옆)이 벽이다. 판정은 8-이웃이라 모서리에서도 끊기지 않는다.

질감 / 불규칙성
    통로      247~255, 저주파 노이즈 (픽셀 단위 무작위면 모래처럼 보인다)
    벽        20~60 (occupied_thresh 64 이하)
    벽 테두리 벽에서 --fringe-px 안. 통로 쪽은 옅은 회색 150~185 (실제 맵의 160~170 띠),
              막힘 쪽은 어두운 회색 70~120. 벽에 가까울수록 확률이 높다 (--fringe-prob 가 1 px 째 확률)
    막힘      128 (실제 맵의 미탐색 값)
    통로 쪽 테두리는 비-FREE 라 planner 팽창 기준으로 통로가 최대 fringe-px 만큼 좁아진다.
    벽 흔들림  통로 마스크를 저주파 변위장으로 비틀어 벽이 ±wobble-px 들쭉날쭉. 모서리도 같이 흔들린다
              (0 이 아니면 벽이 정확히 16, 17 번째 픽셀이 아니다)
    점선 벽   벽 픽셀의 dash-gap 비율을 128(미탐색)로 끊는다. 끊김 길이는 dash-px 정도.
              128 도 비-FREE 라 planner 에는 여전히 장애물이라 통로 검사에 영향이 없다
    긴 광선   털과 같지만 부채꼴로 최대 ray-len px 까지. 여백까지 같이 그려서 회색 영역으로 길게 뻗는다
              (여백은 render 단계에서 붙인다. 검사는 여백을 잘라낸 이미지로 한다)
    털        끊긴 곳에서 막힘 쪽으로 가는 FREE(200~255) 선. 레이저가 벽 틈으로 샌 흔적.
              길이 fur-len 이하, 1 px 폭, 방향은 벽 바깥쪽 ±25도. fur-prob 는 끊긴 픽셀마다의 확률.
              통로와 이어진 1 px 선이라 planner 팽창으로 지워져 주행에 영향이 없다

사선 벽
    --cut X1,Y1,X2,Y2  world 좌표(m) 직선 바깥을 막힘으로 자른다 (통로 칸 중심 평균 쪽이 남는다).
    레이아웃은 1 m 격자라 사선이 계단이 되므로, 레이아웃은 직사각형으로 두고 이 옵션으로 픽셀 단위로 긋는다.
    직선 쪽 벽은 일반 벽과 같은 질감/두께. 직선에 가까운 칸 중심은 통로 검사에서 제외한다.

내부 벽
    --walls FILE  줄마다 'X1 Y1 X2 Y2' (world m). 1 m 격자 칸 경계 위에 두께 1 px(+질감) 선분 벽을 긋는다.
    '#' 칸은 1 m 두께 덩어리라서 얇은 벽은 이쪽으로 쓴다. 벽 선은 칸 중심에서 0.5 m 떨어진 칸 경계(x.5)에 두면
    통로 검사를 통과한다. 문은 선분을 끊어서 낸다 (로봇 폭 0.5 m 이므로 1.5 m 이상 권장).

회색 영역 (중정)
    --void 'x,y x,y x,y ...'  다각형(world m)을 막힘(회색 128)으로 칠한다. 경계에는 일반 벽처럼 선이 생긴다.
    여러 개는 ';'. 다각형 안/코앞의 칸 중심은 통로 검사에서 뺀다.

통로 사각형 파기
    --carve 'X1,Y1,X2,Y2;...'  --void 뒤에 적용한다. 회색 영역 안에 열린 칸(엘리베이터 홀 등)을 만든다.
    복도와 닿는 변을 복도 쪽으로 조금 넘기면 그 변에는 벽이 안 생기고 나머지 세 변에는 벽선이 생긴다.

의존성: numpy, Pillow
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image

from clean_map import dilate
from gen_map import check, load_layout, save_map

OCCUPIED_THRESH, FREE_THRESH = 64, 192  # maps/641931... 와 같은 값
UNKNOWN_VALUE = 128


def value_noise(shape: tuple[int, int], cell_px: int, rng: np.random.Generator) -> np.ndarray:
    """cell_px 간격 격자의 난수를 bicubic 으로 보간한 0~1 저주파 노이즈."""
    H, W = shape
    gh, gw = H // cell_px + 3, W // cell_px + 3
    coarse = Image.fromarray(rng.random((gh, gw)).astype(np.float32), "F")
    fine = np.asarray(coarse.resize((gw * cell_px, gh * cell_px), Image.BICUBIC))
    oy, ox = rng.integers(cell_px), rng.integers(cell_px)  # 격자 위치가 타일 격자와 겹치지 않게
    return np.clip(fine[oy:oy + H, ox:ox + W], 0.0, 1.0)


def rings(core: np.ndarray, region: np.ndarray, n: int) -> list[np.ndarray]:
    """core 에서 Chebyshev 거리 1..n 인 region 안 픽셀들 (거리별 마스크)."""
    out, prev = [], core
    for _ in range(n):
        cur = dilate(prev, 1)
        out.append(cur & ~prev & region)
        prev = cur
    return out


def layout_mask(layout: np.ndarray, T: int) -> np.ndarray:
    """칸 중심이 타일 모서리에 오도록 (gen_map 과 같은 배치) 픽셀 단위 통로 마스크를 만든다.
    바깥 1칸은 막힘. 결과 크기 (rows+1)·T × (cols+1)·T."""
    padded = np.pad(layout, 1, constant_values=False)
    up = padded.repeat(T, axis=0).repeat(T, axis=1)
    h = T // 2
    return up[h:-h, h:-h]


def cut_mask(free: np.ndarray, line: tuple[float, float, float, float], res: float,
             keep: tuple[float, float]) -> np.ndarray:
    """통로 마스크를 world 좌표 직선 (x1,y1)-(x2,y2) 로 잘라 keep 점이 있는 쪽만 남긴다.
    픽셀 (i,j) 의 중심은 world ((j+0.5)·res, (i+0.5)·res) (layout_mask 좌표 = 레이아웃 칸 중심 (c+1, r+1) m)."""
    x1, y1, x2, y2 = line
    nx, ny = y2 - y1, x1 - x2  # 직선 법선
    norm = np.hypot(nx, ny)
    if norm == 0:
        raise SystemExit("--cut 의 두 점이 같다")
    sign = np.sign(((keep[0] - x1) * nx + (keep[1] - y1) * ny) / norm)
    if sign == 0:
        raise SystemExit("--cut 직선이 통로 중심을 지난다. 어느 쪽을 남길지 알 수 없다")
    yy, xx = np.indices(free.shape)
    side = ((xx + 0.5) * res - x1) * nx + ((yy + 0.5) * res - y1) * ny
    return free & (side * sign >= 0)


def load_walls(path: Path) -> list[tuple[float, float, float, float]]:
    """내부 벽 파일: 한 줄에 'X1 Y1 X2 Y2' (world m, 쉼표/공백 모두 가능). ';' 로 시작하면 주석."""
    segs = []
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.split(";")[0].replace(",", " ").split()
        if not line:
            continue
        try:
            assert len(line) == 4
            segs.append(tuple(float(v) for v in line))
        except (ValueError, AssertionError):
            raise SystemExit(f"{path}:{n}: 'X1 Y1 X2 Y2' (world m) 형식이 아니다")
    return segs


def draw_walls(free: np.ndarray, segs, res: float) -> np.ndarray:
    """통로 마스크에 선분(두께 1 px)을 막힘으로 긋는다. 벽 질감은 일반 벽과 같게 render 가 양옆으로 입힌다.
    칸 안쪽 얇은 벽(칸 경계 위, 예: x=85.5)을 그릴 때 쓴다. 문은 선분을 끊어서 만든다."""
    free = free.copy()
    H, W = free.shape
    for x1, y1, x2, y2 in segs:
        a, b = np.array([x1, y1]) / res, np.array([x2, y2]) / res
        x0, x_1 = int(max(0, min(a[0], b[0]) - 2)), int(min(W, max(a[0], b[0]) + 3))
        y0, y_1 = int(max(0, min(a[1], b[1]) - 2)), int(min(H, max(a[1], b[1]) + 3))
        yy, xx = np.mgrid[y0:y_1, x0:x_1]
        px, py = xx + 0.5, yy + 0.5
        d = b - a
        t = np.clip(((px - a[0]) * d[0] + (py - a[1]) * d[1]) / max(float(d @ d), 1e-9), 0.0, 1.0)
        dist = np.hypot(px - (a[0] + t * d[0]), py - (a[1] + t * d[1]))
        free[y0:y_1, x0:x_1] &= dist > 0.5
    return free


def parse_void(text: str) -> list[list[tuple[float, float]]]:
    """'x,y x,y x,y ...' 다각형. ';' 로 여러 개."""
    polys = []
    for part in text.split(";"):
        if not part.strip():
            continue
        try:
            pts = [tuple(float(v) for v in tok.split(",")) for tok in part.split()]
            assert len(pts) >= 3 and all(len(p) == 2 for p in pts)
        except (ValueError, AssertionError):
            raise SystemExit("--void 는 'x,y x,y x,y ...' (world m, 꼭짓점 3개 이상, 여러 개는 ;)")
        polys.append(pts)
    return polys


def void_mask(shape: tuple[int, int], polys, res: float) -> np.ndarray:
    """world 좌표 다각형들을 픽셀 마스크로 채운다."""
    from PIL import ImageDraw
    img = Image.new("1", (shape[1], shape[0]), 0)
    d = ImageDraw.Draw(img)
    for pts in polys:
        d.polygon([(x / res - 0.5, y / res - 0.5) for x, y in pts], fill=1)
    return np.asarray(img, bool)


def wobble(free: np.ndarray, amp: float, rng: np.random.Generator) -> np.ndarray:
    """통로 마스크를 저주파 변위장으로 비틀어 경계를 ±amp px 흔든다."""
    if amp <= 0:
        return free
    H, W = free.shape
    dy = np.rint((value_noise(free.shape, 10, rng) - 0.5) * 2 * amp).astype(int)
    dx = np.rint((value_noise(free.shape, 10, rng) - 0.5) * 2 * amp).astype(int)
    yy, xx = np.indices(free.shape)
    return free[np.clip(yy + dy, 0, H - 1), np.clip(xx + dx, 0, W - 1)]


def shoot(img: np.ndarray, free: np.ndarray, wall: np.ndarray, gap: np.ndarray, grad: tuple,
          prob: float, min_len: int, max_len: int, fan: int, spread: float, dim_ratio: float,
          rng: np.random.Generator) -> None:
    """끊긴 벽 픽셀에서 막힘 쪽으로 1 px 선(레이저가 벽 틈으로 샌 흔적)을 긋는다. 시작마다 fan 개의
    선이 spread rad 안에서 부채꼴로 퍼진다. 통로나 다른 벽을 만나면 멈춘다 (통로/벽은 안 건드린다)."""
    gy, gx = grad
    H, W = img.shape
    for y, x in np.argwhere(gap & (rng.random(gap.shape) < prob)):
        if np.hypot(gy[y, x], gx[y, x]) < 1e-3:
            continue
        base = np.arctan2(-gy[y, x], -gx[y, x])  # 막힘 쪽 (통로 방향 그래디언트의 반대)
        for _ in range(int(rng.integers(1, fan + 1))):
            ang = base + rng.uniform(-spread, spread)
            dim = rng.random() < dim_ratio  # 일부는 옅은 회색 (UNKNOWN 값) 줄무늬
            for t in range(int(rng.integers(min_len, max_len + 1))):
                py, px = int(round(y + t * np.sin(ang))), int(round(x + t * np.cos(ang)))
                if not (0 <= py < H and 0 <= px < W) or free[py, px] or (t > 1 and wall[py, px]):
                    break
                if not gap[py, px]:
                    img[py, px] = rng.integers(140, 186) if dim else rng.integers(200, 256)


def render(free: np.ndarray, args, rng: np.random.Generator) -> np.ndarray:
    free = wobble(free, args.wobble_px, rng)
    blocked = ~free
    half = args.wall_px // 2
    wall = (blocked & dilate(free, half)) | (free & dilate(blocked, half))
    shape = free.shape

    img = np.full(shape, UNKNOWN_VALUE, np.uint8)

    tex = 0.6 * value_noise(shape, 16, rng) + 0.4 * value_noise(shape, 4, rng)
    floor = free & ~wall
    img[floor] = np.rint(247 + 8 * tex[floor]).astype(np.uint8)

    wtex = 0.5 * value_noise(shape, 3, rng) + 0.5 * rng.random(shape)
    img[wall] = np.rint(20 + 40 * wtex[wall]).astype(np.uint8)

    # 테두리: 벽에서 멀수록 확률이 선형으로 준다. 저주파 노이즈로 확률을 0.5~1.5 배 흔들어 뭉치게 한다
    clump = 0.5 + value_noise(shape, 6, rng)
    shade = rng.random(shape)
    n = args.fringe_px
    for side, (lo, hi) in ((floor, (150, 185)), (blocked & ~wall, (70, 120))):
        for d, ring in enumerate(rings(wall, side, n), start=1):
            p = args.fringe_prob * (n + 1 - d) / n
            hit = ring & (rng.random(shape) < p * clump)
            img[hit] = np.rint(lo + (hi - lo) * shade[hit]).astype(np.uint8)

    if args.dash_gap > 0:
        n = value_noise(shape, args.dash_px, rng)
        gap = wall & (n < np.quantile(n[wall], args.dash_gap))
        img[gap] = UNKNOWN_VALUE
        from PIL import ImageFilter
        blur = np.asarray(Image.fromarray((free * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(2)),
                          np.float32)
        grad = np.gradient(blur)
        if args.fur_prob > 0:  # 털: 짧은 가시. 벽 근처에만
            shoot(img, free, wall, gap, grad, args.fur_prob, max(1, args.fur_len // 2), args.fur_len, 1, 0.45, 0.0, rng)
        if args.ray_prob > 0:  # 긴 광선: 여백(미탐색 영역) 쪽으로 길게 뻗는 줄무늬
            shoot(img, free, wall, gap, grad, args.ray_prob, args.ray_len // 4, args.ray_len, 4, 0.35, 0.3, rng)
    return img


def main() -> int:
    ap = argparse.ArgumentParser(description="레이아웃에서 맵 전체를 한 번에 그리기 (질감 노이즈)")
    ap.add_argument("layout", type=Path, help="레이아웃 텍스트 (. 통로 / # 막힘, 한 글자 = 1 타일)")
    ap.add_argument("--out", type=Path, required=True, help="출력 폴더 (예: maps/synth_loop)")
    ap.add_argument("--name", help="맵 이름 (기본: 출력 폴더 이름)")
    ap.add_argument("--resolution", type=float, default=0.03125, help="m/px (기본: 원본 SLAM 맵과 같은 31.25 mm)")
    ap.add_argument("--tile-m", type=float, default=1.0, help="레이아웃 한 칸 (m)")
    ap.add_argument("--wall-px", type=int, default=2, help="벽 두께 px (짝수, 경계 양쪽 절반씩)")
    ap.add_argument("--fringe-px", type=int, default=2, help="벽 테두리 노이즈 폭 px")
    ap.add_argument("--fringe-prob", type=float, default=0.5, help="벽 바로 옆(1 px 째) 테두리 노이즈 확률")
    ap.add_argument("--wobble-px", type=float, default=1.0, help="벽 흔들림 진폭 px (0 = 정확한 직선)")
    ap.add_argument("--dash-gap", type=float, default=0.25, help="점선 벽: 끊기는 벽 픽셀 비율 0~1 (0 = 실선)")
    ap.add_argument("--dash-px", type=int, default=5, help="점선 벽: 끊김/이음 길이 px 정도")
    ap.add_argument("--fur-prob", type=float, default=0.06, help="털: 끊긴 픽셀마다 털이 시작할 확률 (0 = 없음)")
    ap.add_argument("--fur-len", type=int, default=10, help="털 최대 길이 px")
    ap.add_argument("--ray-prob", type=float, default=0.012, help="긴 광선: 끊긴 픽셀마다 시작할 확률 (0 = 없음)")
    ap.add_argument("--ray-len", type=int, default=150, help="긴 광선 최대 길이 px (여백으로 뻗는다)")
    ap.add_argument("--cut", metavar="X1,Y1,X2,Y2",
                    help="world 좌표(m) 직선으로 통로를 잘라 통로 중심 쪽만 남긴다 (사선 벽). 칸 (r,c) 중심 = (c+1, r+1)")
    ap.add_argument("--walls", type=Path, metavar="FILE",
                    help="내부 벽 파일 (줄마다 'X1 Y1 X2 Y2' world m). 칸 경계 위에 1 px 얇은 벽을 긋는다. 문은 선분을 끊어서 낸다")
    ap.add_argument("--void", metavar="POLY",
                    help="가운데 뚫린 곳(중정) 등 회색(미탐색, 주행 불가)으로 칠할 다각형. 'x,y x,y x,y ...' world m, 여러 개는 ;")
    ap.add_argument("--carve", metavar="RECTS",
                    help="--void 등으로 막힌 곳에 통로(흰색) 사각형을 판다 (엘리베이터 홀 등). 'X1,Y1,X2,Y2' world m, 여러 개는 ;. "
                         "복도와 이어질 변은 복도 쪽으로 조금 넘겨 그리면 벽이 안 생긴다")
    ap.add_argument("--margin-tiles", type=int, default=10, help="사방 회색(미탐색) 여백 타일 수 (10 = 320 px = 10 m)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--tries", type=int, default=20, help="검사 실패 시 seed+1, +2... 로 다시 그리는 횟수")
    ap.add_argument("--robot-width", type=float, default=0.5, help="검사용 로봇 폭 (robot_client 기본값과 같음)")
    ap.add_argument("--force", action="store_true", help="검사에 실패해도 마지막 결과를 저장")
    args = ap.parse_args()

    T = round(args.tile_m / args.resolution)
    if T < 8 or T % 2 or abs(T * args.resolution - args.tile_m) > 1e-6:
        raise SystemExit(f"tile-m / resolution = {args.tile_m / args.resolution} 가 8 이상 짝수 정수가 아니다")
    if args.wall_px < 2 or args.wall_px % 2:
        raise SystemExit("--wall-px 는 2 이상 짝수 (경계 양쪽에 절반씩)")

    layout = load_layout(args.layout)
    m = args.margin_tiles * T
    base = layout_mask(layout, T)
    radius_px = int(np.ceil((args.robot_width / 2) / args.resolution))
    check_layout = layout
    if args.cut:
        try:
            line = tuple(float(v) for v in args.cut.split(","))
            assert len(line) == 4
        except (ValueError, AssertionError):
            raise SystemExit("--cut 은 X1,Y1,X2,Y2 (world m)")
        rr, cc = np.nonzero(layout)
        keep = (cc.mean() + 1, rr.mean() + 1)  # 통로 칸 중심의 평균점 쪽을 남긴다
        base = cut_mask(base, line, args.resolution, keep)
        # 잘린 쪽이나 사선 코앞(로봇 반폭 + 벽/테두리)에 중심이 있는 칸은 검사 대상에서 뺀다
        x1, y1, x2, y2 = line
        nx, ny = y2 - y1, x1 - x2
        sign = np.sign((keep[0] - x1) * nx + (keep[1] - y1) * ny)
        dist = sign * ((cc + 1 - x1) * nx + (rr + 1 - y1) * ny) / np.hypot(nx, ny)
        need = (radius_px + args.wall_px / 2 + args.fringe_px + args.wobble_px + 1) * args.resolution
        check_layout = layout.copy()
        check_layout[rr[dist < need], cc[dist < need]] = False
    if args.walls:
        segs = load_walls(args.walls)
        base = draw_walls(base, segs, args.resolution)
        # 칸 경계(x.5)가 아닌 곳에 그은 벽 코앞에 중심이 있는 칸도 검사 대상에서 뺀다
        need = (radius_px + args.wall_px / 2 + args.fringe_px + args.wobble_px + 1) * args.resolution
        rr, cc = np.nonzero(check_layout)
        near = np.zeros(len(rr), bool)
        for x1, y1, x2, y2 in segs:
            d = np.array([x2 - x1, y2 - y1])
            t = np.clip(((cc + 1 - x1) * d[0] + (rr + 1 - y1) * d[1]) / max(float(d @ d), 1e-9), 0.0, 1.0)
            near |= np.hypot(cc + 1 - (x1 + t * d[0]), rr + 1 - (y1 + t * d[1])) < need
        if near.any():
            check_layout = check_layout.copy()
            check_layout[rr[near], cc[near]] = False
    if args.void:
        vm = void_mask(base.shape, parse_void(args.void), args.resolution)
        base = base & ~vm
        # 구멍 안이나 코앞(로봇 반폭 + 벽/테두리)에 중심이 있는 칸은 검사 대상에서 뺀다
        need_px = int(np.ceil(radius_px + args.wall_px / 2 + args.fringe_px + args.wobble_px + 1))
        near = dilate(vm, need_px)
        rr, cc = np.nonzero(check_layout)
        hit = near[(rr + 1) * T, (cc + 1) * T]
        if hit.any():
            check_layout = check_layout.copy()
            check_layout[rr[hit], cc[hit]] = False
    if args.carve:
        for part in args.carve.split(";"):
            if not part.strip():
                continue
            try:
                x1, y1, x2, y2 = (float(v) for v in part.split(","))
            except ValueError:
                raise SystemExit("--carve 는 'X1,Y1,X2,Y2' (world m, 여러 개는 ;)")
            res = args.resolution
            j0, j1 = int(round(min(x1, x2) / res)), int(round(max(x1, x2) / res))
            i0, i1 = int(round(min(y1, y2) / res)), int(round(max(y1, y2) / res))
            base[i0:i1, j0:j1] = True  # 회색 영역에 통로 사각형을 판다. 통로와 닿는 변은 벽 없이 이어진다
    free = np.pad(base, m, constant_values=False)  # 여백도 같이 그려서 광선이 이어진다

    img, problems, seed = None, [], args.seed
    for i in range(max(1, args.tries)):
        seed = args.seed + i
        img = render(free, args, np.random.default_rng(seed))
        problems = check(img[m:img.shape[0] - m, m:img.shape[1] - m], check_layout, T, FREE_THRESH, radius_px)
        if not problems:
            break
        print(f"seed {seed}: 검사 실패 ({problems[0]}{' 외' if len(problems) > 1 else ''})")
    if problems:
        print("\n".join(problems[:10]), file=sys.stderr)
        if not args.force:
            print("검사를 통과한 결과가 없다. 레이아웃 통로를 넓히거나 --fringe-px 를 줄이거나 --force", file=sys.stderr)
            return 1

    info = {
        "source": "synthetic (synth_map.py)",
        "seed": seed,
        "tile_m": T * args.resolution,
        "tile_px": T,
        "check_passed": not problems,
        "cut": args.cut,
        "walls": str(args.walls) if args.walls else None,
        "void": args.void,
        "carve": args.carve,
        "params": {k: getattr(args, k) for k in ("robot_width", "wall_px", "fringe_px", "fringe_prob", "wobble_px", "dash_gap",
                                                            "dash_px", "fur_prob", "fur_len", "ray_prob", "ray_len")},
    }
    save_map(args.out, args.name, img, layout, args.layout, T, args.resolution, OCCUPIED_THRESH, FREE_THRESH, info,
             args.margin_tiles, pad_margin=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())

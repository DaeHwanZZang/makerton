#!/usr/bin/env python3
"""
맵 편집 모델 (GUI 없음)
=======================

편집기(map_editor.py)와 나중의 FMS 웹 UI 가 같은 규칙으로 렌더하도록 도형 정의와 래스터화를 한 곳에 둔다.
의존성은 numpy 만이고, PNG 입출력(pillow)은 호출하는 쪽이 한다.

    grid.png (원본, 수정 안 함) + edits.json (도형 목록) -> render() -> edited_navi_gridmap.png

- 도형 좌표는 world 미터(fms_map.json 의 origin/resolution 기준, y 아래로 증가)로 저장한다.
  해상도·여백이 바뀌거나 화면을 줌해도 도형 모양이 그대로다.
- 색은 흰색(255, FREE) / 검정(0, OCCUPIED) / 회색(128, UNKNOWN) 3값뿐이다. 안티앨리어싱이 없어서
  중간 회색값(= UNKNOWN 장애물)이 생기지 않는다.
- 픽셀 (i, j) 는 중심 (i+0.5, j+0.5) 가 도형 안에 들어오면 칠한다. 같은 입력이면 항상 같은 결과다.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

VERSION = 1
COLORS = {"white": 255, "black": 0, "gray": 128}
TYPES = ("rect", "line", "path")
MIN_RADIUS_PX = 0.5  # 굵기가 1 px 보다 가늘어도 1 px 선으로 그린다 (사라지는 도형 방지)


@dataclass
class Shape:
    type: str                     # rect | line | path
    color: str                    # white | black | gray
    points: list[list[float]]     # world 미터. rect/line 은 2점, path 는 2점 이상
    width_m: float = 0.0625       # 선 굵기 (line/path, rect 윤곽)
    filled: bool = True           # rect 만 의미 있음

    def to_dict(self) -> dict:
        d = {"type": self.type, "color": self.color, "points": [[round(x, 5), round(y, 5)] for x, y in self.points]}
        if self.type != "rect" or not self.filled:
            d["width_m"] = round(self.width_m, 5)
        if self.type == "rect":
            d["filled"] = self.filled
        return d

    @staticmethod
    def from_dict(d: dict) -> "Shape":
        if d.get("type") not in TYPES:
            raise ValueError(f"알 수 없는 도형 type: {d.get('type')!r}")
        if d.get("color") not in COLORS:
            raise ValueError(f"알 수 없는 색: {d.get('color')!r}")
        pts = [[float(x), float(y)] for x, y in d["points"]]
        if len(pts) < 2 or (d["type"] != "path" and len(pts) != 2):
            raise ValueError(f"{d['type']} 도형의 점 개수가 맞지 않는다: {len(pts)}")
        return Shape(d["type"], d["color"], pts, float(d.get("width_m", 0.0625)), bool(d.get("filled", True)))

    def copy(self) -> "Shape":
        return Shape(self.type, self.color, [p[:] for p in self.points], self.width_m, self.filled)


@dataclass
class MapGeom:
    """fms_map.json 에서 좌표 변환에 필요한 값."""
    resolution: float
    origin_x: float
    origin_y: float
    width_px: int
    height_px: int

    @staticmethod
    def from_meta(meta: dict) -> "MapGeom":
        return MapGeom(float(meta["resolution"]), float(meta["origin_x"]), float(meta["origin_y"]),
                       int(meta["width_px"]), int(meta["height_px"]))

    def to_px(self, x: float, y: float) -> tuple[float, float]:
        return (x - self.origin_x) / self.resolution, (y - self.origin_y) / self.resolution

    def to_world(self, px: float, py: float) -> tuple[float, float]:
        return self.origin_x + px * self.resolution, self.origin_y + py * self.resolution


# -- 파일 --------------------------------------------------------------------

def load_edits(path: Path, geom: MapGeom) -> list[Shape]:
    """edits.json 을 읽는다. 없으면 빈 목록. 맵과 크기/해상도가 다르면 ValueError."""
    if not path.exists():
        return []
    d = json.loads(path.read_text(encoding="utf-8"))
    if d.get("version") != VERSION:
        raise ValueError(f"edits.json version {d.get('version')} 은 지원하지 않는다 (지원: {VERSION})")
    if d.get("size_px") != [geom.width_px, geom.height_px] or abs(d.get("resolution", 0) - geom.resolution) > 1e-9:
        raise ValueError(f"edits.json 의 맵 크기/해상도가 fms_map.json 과 다르다: {d.get('size_px')} / {d.get('resolution')}")
    shapes = [Shape.from_dict(s) for s in d["shapes"]]
    # edits.json 의 origin 은 도형을 저장했을 때의 좌표계다. 맵 origin 이 그동안 바뀌었으면(원점 재지정)
    # 도형이 이미지 위 같은 픽셀에 있도록 world 좌표를 평행이동한다.
    ox, oy = d.get("origin", [geom.origin_x, geom.origin_y])
    dx, dy = geom.origin_x - ox, geom.origin_y - oy
    if abs(dx) > 1e-9 or abs(dy) > 1e-9:
        for sh in shapes:
            move_shape(sh, dx, dy)
    return shapes


def save_edits(path: Path, geom: MapGeom, shapes: list[Shape], base_image: str = "grid.png") -> None:
    d = {
        "version": VERSION,
        "base_image": base_image,
        "size_px": [geom.width_px, geom.height_px],
        "resolution": geom.resolution,
        "origin": [geom.origin_x, geom.origin_y],
        "shapes": [s.to_dict() for s in shapes],
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)


# -- 래스터화 ----------------------------------------------------------------

def _seg_mask(out: np.ndarray, value: int, a: tuple[float, float], b: tuple[float, float], r: float) -> None:
    """선분 ab (px 좌표) 에서 반경 r 이내(둥근 끝)인 픽셀 중심을 value 로 칠한다."""
    H, W = out.shape
    x0 = max(0, int(math.floor(min(a[0], b[0]) - r - 1)))
    x1 = min(W, int(math.ceil(max(a[0], b[0]) + r + 1)))
    y0 = max(0, int(math.floor(min(a[1], b[1]) - r - 1)))
    y1 = min(H, int(math.ceil(max(a[1], b[1]) + r + 1)))
    if x0 >= x1 or y0 >= y1:
        return
    ys, xs = np.mgrid[y0:y1, x0:x1]
    cx, cy = xs + 0.5, ys + 0.5
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    if L2 < 1e-12:
        t = np.zeros_like(cx)
    else:
        t = np.clip(((cx - a[0]) * dx + (cy - a[1]) * dy) / L2, 0.0, 1.0)
    d2 = (cx - (a[0] + t * dx)) ** 2 + (cy - (a[1] + t * dy)) ** 2
    out[y0:y1, x0:x1][d2 <= r * r + 1e-9] = value


def _rect_mask(out: np.ndarray, value: int, x0: float, y0: float, x1: float, y1: float) -> None:
    """px 좌표 사각형 [x0,x1]x[y0,y1] 안에 중심이 들어오는 픽셀을 칠한다. 비어 있으면 아무것도 안 한다."""
    H, W = out.shape
    i0 = max(0, int(math.ceil(x0 - 0.5)))
    i1 = min(W - 1, int(math.floor(x1 - 0.5)))
    j0 = max(0, int(math.ceil(y0 - 0.5)))
    j1 = min(H - 1, int(math.floor(y1 - 0.5)))
    if i0 <= i1 and j0 <= j1:
        out[j0:j1 + 1, i0:i1 + 1] = value


def draw_shape(img: np.ndarray, shape: Shape, geom: MapGeom) -> None:
    value = COLORS[shape.color]
    pts = [geom.to_px(x, y) for x, y in shape.points]
    r = max(shape.width_m / geom.resolution / 2.0, MIN_RADIUS_PX)
    if shape.type == "rect":
        (ax, ay), (bx, by) = pts
        x0, x1, y0, y1 = min(ax, bx), max(ax, bx), min(ay, by), max(ay, by)
        if shape.filled:
            _rect_mask(img, value, x0, y0, x1, y1)
            return
        # 윤곽: 굵기 2r 짜리 띠 4개의 합 (모서리는 각진 이음, 안쪽이 좁으면 자연히 다 채워진다)
        _rect_mask(img, value, x0 - r, y0 - r, x1 + r, y0 + r)
        _rect_mask(img, value, x0 - r, y1 - r, x1 + r, y1 + r)
        _rect_mask(img, value, x0 - r, y0 - r, x0 + r, y1 + r)
        _rect_mask(img, value, x1 - r, y0 - r, x1 + r, y1 + r)
    else:
        if len(pts) == 1:
            pts = pts * 2
        for a, b in zip(pts, pts[1:]):
            _seg_mask(img, value, a, b, r)


def render(base: np.ndarray, shapes: list[Shape], geom: MapGeom) -> np.ndarray:
    """base(uint8, HxW) 위에 shapes 를 순서대로(뒤가 위) 칠한 새 배열을 돌려준다."""
    if base.shape != (geom.height_px, geom.width_px):
        raise ValueError(f"base 크기 {base.shape[::-1]} 가 맵 크기 {(geom.width_px, geom.height_px)} 와 다르다")
    img = base.copy()
    for s in shapes:
        draw_shape(img, s, geom)
    return img


# -- 편집 보조 ---------------------------------------------------------------

def rdp(points: list[list[float]], eps: float) -> list[list[float]]:
    """Ramer-Douglas-Peucker 단순화 (자유형 그리기 점 줄이기). 반복 구현이라 재귀 한도와 무관."""
    n = len(points)
    if n < 3:
        return [p[:] for p in points]
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        s, e = stack.pop()
        ax, ay = points[s]
        bx, by = points[e]
        dx, dy = bx - ax, by - ay
        L = math.hypot(dx, dy)
        best, bi = -1.0, -1
        for i in range(s + 1, e):
            px, py = points[i]
            d = math.hypot(px - ax, py - ay) if L < 1e-12 else abs(dx * (ay - py) - (ax - px) * dy) / L
            if d > best:
                best, bi = d, i
        if best > eps:
            keep[bi] = True
            stack += [(s, bi), (bi, e)]
    return [points[i][:] for i in range(n) if keep[i]]


def dist_to_segment(p: tuple[float, float], a, b) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 < 1e-12 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def hit_test(shape: Shape, p: tuple[float, float], tol_m: float) -> bool:
    """world 좌표 p 가 도형 위인가. tol_m: 가는 선도 집을 수 있게 주는 여유 (화면 몇 px 를 world 로 바꾼 값)."""
    r = shape.width_m / 2.0 + tol_m
    pts = shape.points
    if shape.type == "rect":
        (ax, ay), (bx, by) = pts
        x0, x1, y0, y1 = min(ax, bx), max(ax, bx), min(ay, by), max(ay, by)
        if shape.filled:
            return x0 - tol_m <= p[0] <= x1 + tol_m and y0 - tol_m <= p[1] <= y1 + tol_m
        corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        return any(dist_to_segment(p, corners[i], corners[(i + 1) % 4]) <= r for i in range(4))
    return any(dist_to_segment(p, a, b) <= r for a, b in zip(pts, pts[1:]))


def move_shape(shape: Shape, dx: float, dy: float) -> None:
    for pt in shape.points:
        pt[0] += dx
        pt[1] += dy

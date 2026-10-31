#!/usr/bin/env python3
"""
맵 편집기 (PySide6)
===================

grid.png 위에 흰색/검정/회색 도형(사각형·선분·자유형)을 얹어 edited_navi_gridmap.png 를 만든다.
이미지에 직접 그리지 않고 도형을 world 좌표로 edits.json 에 저장하므로 다시 열어 이동·수정·삭제할 수 있고,
줌해도 도형 모양이 그대로다. 렌더 규칙은 map_edit_model.py (GUI 없음) 에 있다.

    .venv/bin/python tools/map_editor.py maps/gen_loop/fms_map.json    # 리포 루트에서

- 맵 폴더에 grid.png 와 edited_navi_gridmap.png 가 모두 있어야 시작한다 (없으면 경고 후 종료).
- 저장은 Ctrl+S 수동뿐이다: edits.json 과 edited_navi_gridmap.png 를 쓴다. fms_map.json 은 원점(O)을 바꿨을 때만
  origin_x/origin_y 두 값을 고쳐 쓴다 (저장 전에 확인한다).
- 조작: V 선택 / R 사각형 / L 선분 (Shift: 15° 단위) / P 자유형 / O 원점 지정, 휠 줌, 가운데·오른쪽 버튼 또는 Space+드래그 이동,
  F 전체 보기, Delete 삭제, Ctrl+Z 실행 취소, Ctrl+Shift+Z(또는 Ctrl+Y) 다시 실행.
- 원점 지정: 클릭한 지점(가장 가까운 픽셀 모서리)이 world (0, 0) 이 된다. 이미지와 도형은 그대로이고 좌표계만 옮겨진다
  (도형의 world 좌표는 같이 평행이동). 실행 취소 가능.
"""

from __future__ import annotations

import json
import math
import signal
import sys
from pathlib import Path

import numpy as np
from PIL import Image
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QImage, QKeySequence, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QDoubleSpinBox, QFileDialog, QHBoxLayout, QLabel, QListWidget,
    QMainWindow, QMessageBox, QPushButton, QRadioButton, QVBoxLayout, QWidget,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import map_edit_model as M  # noqa: E402
from clean_map import inflate_free, label  # noqa: E402

ROBOT_RADIUS_PX = 8        # planner.cpp 의 로봇 반폭 (0.25 m / 0.03125). planner 규칙이 바뀌면 같이 고친다.
MIN_COMPONENT_PX = 400     # 이보다 작은 주행 가능 조각은 끊김 경고에서 센 제외
HANDLE_PX = 6              # 꼭짓점 핸들 반경 (화면 px)
PICK_TOL_PX = 4            # 가는 선도 집을 수 있게 주는 여유 (화면 px)
QCOLORS = {"white": QColor(255, 255, 255), "black": QColor(0, 0, 0), "gray": QColor(128, 128, 128)}
COLOR_KO = {"white": "흰색", "black": "검정", "gray": "회색"}
TYPE_KO = {"rect": "사각형", "line": "선분", "path": "자유형"}


def drivable_components(img: np.ndarray, free_thresh: int) -> int:
    lab, n = label(inflate_free(img >= free_thresh, ROBOT_RADIUS_PX))
    if n == 0:
        return 0
    return int((np.bincount(lab.ravel())[1:] >= MIN_COMPONENT_PX).sum())


def gray_qimage(arr: np.ndarray) -> QImage:
    h, w = arr.shape
    return QImage(np.ascontiguousarray(arr).data, w, h, w, QImage.Format_Grayscale8).copy()


class Canvas(QWidget):
    """줌/이동 가능한 편집 화면. 도형은 world 좌표 벡터로 들고 있고, 그릴 때마다 현재 줌으로 변환한다."""

    def __init__(self, geom: M.MapGeom, base: np.ndarray, shapes: list[M.Shape]) -> None:
        super().__init__()
        self.geom, self.base = geom, base
        self.base_img = gray_qimage(base)
        self.shapes = shapes
        self.selected = -1
        self.tool = "select"
        self.color, self.width_m, self.filled = "black", 0.0625, True
        self.zoom, self.off = 1.0, QPointF(0, 0)
        self.show_raster = False
        self._raster_cache: QImage | None = None
        self.draft: M.Shape | None = None
        self.drag: dict | None = None
        self.pan_last: QPointF | None = None
        self.space = False
        self.undo: list[tuple[list[M.Shape], int, tuple[float, float]]] = []
        self.redo: list[tuple[list[M.Shape], int, tuple[float, float]]] = []
        self.hover_corner: tuple[int, int] | None = None   # 원점 도구가 가리키는 픽셀 모서리
        self.dirty = False
        self.on_change = lambda: None       # 도형 목록/선택이 바뀌면 (패널 갱신)
        self.on_cursor = lambda s: None
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumSize(480, 360)

    # -- 좌표 ---------------------------------------------------------------
    def ipx_to_screen(self, x: float, y: float) -> QPointF:
        return QPointF(self.off.x() + x * self.zoom, self.off.y() + y * self.zoom)

    def screen_to_world(self, p: QPointF) -> tuple[float, float]:
        return self.geom.to_world((p.x() - self.off.x()) / self.zoom, (p.y() - self.off.y()) / self.zoom)

    def world_to_screen(self, x: float, y: float) -> QPointF:
        return self.ipx_to_screen(*self.geom.to_px(x, y))

    def fit(self) -> None:
        g = self.geom
        self.zoom = max(0.05, min(self.width() / g.width_px, self.height() / g.height_px) * 0.95)
        self.off = QPointF((self.width() - g.width_px * self.zoom) / 2, (self.height() - g.height_px * self.zoom) / 2)
        self.update()

    def showEvent(self, e) -> None:
        super().showEvent(e)
        if not getattr(self, "_fitted", False):
            self._fitted = True
            self.fit()

    # -- 변경 이력 ------------------------------------------------------------
    def _snap(self) -> tuple[list[M.Shape], int, tuple[float, float]]:
        return [s.copy() for s in self.shapes], self.selected, (self.geom.origin_x, self.geom.origin_y)

    def _restore(self, snap) -> None:
        self.shapes, self.selected, (self.geom.origin_x, self.geom.origin_y) = snap

    def set_origin_px(self, cx: int, cy: int) -> None:
        """이미지 위 픽셀 모서리 (cx, cy) 를 world (0, 0) 으로 만든다. 도형은 같은 픽셀에 남도록 world 좌표를 옮긴다."""
        g = self.geom
        nx, ny = round(-cx * g.resolution, 6), round(-cy * g.resolution, 6)
        dx, dy = nx - g.origin_x, ny - g.origin_y
        if abs(dx) < 1e-9 and abs(dy) < 1e-9:
            return
        self.push_undo()
        for s in self.shapes:
            M.move_shape(s, dx, dy)
        g.origin_x, g.origin_y = nx, ny
        self.changed()

    def push_undo(self, snap=None) -> None:
        self.undo.append(snap or self._snap())
        self.redo.clear()
        self.changed()

    def changed(self) -> None:
        self.dirty = True
        self._raster_cache = None
        self.on_change()
        self.update()

    def do_undo(self) -> None:
        if self.undo:
            self.redo.append(self._snap())
            self._restore(self.undo.pop())
            self.changed()

    def do_redo(self) -> None:
        if self.redo:
            self.undo.append(self._snap())
            self._restore(self.redo.pop())
            self.changed()

    def select(self, idx: int) -> None:
        self.selected = idx
        self.on_change()
        self.update()

    def delete_selected(self) -> None:
        if 0 <= self.selected < len(self.shapes):
            self.push_undo()
            del self.shapes[self.selected]
            self.selected = -1
            self.changed()

    def move_selected_z(self, d: int) -> None:
        i, j = self.selected, self.selected + d
        if 0 <= i < len(self.shapes) and 0 <= j < len(self.shapes):
            self.push_undo()
            self.shapes[i], self.shapes[j] = self.shapes[j], self.shapes[i]
            self.selected = j
            self.changed()

    def edit_selected(self, **kw) -> None:
        """선택 도형의 속성(color/width_m/filled)을 바꾼다."""
        if 0 <= self.selected < len(self.shapes):
            s = self.shapes[self.selected]
            if all(getattr(s, k) == v for k, v in kw.items()):
                return
            self.push_undo()
            for k, v in kw.items():
                setattr(s, k, v)
            self.changed()

    def raster(self) -> QImage:
        if self._raster_cache is None:
            self._raster_cache = gray_qimage(M.render(self.base, self.shapes, self.geom))
        return self._raster_cache

    # -- 그리기 ---------------------------------------------------------------
    def _paint_shape(self, p: QPainter, s: M.Shape) -> None:
        g = self.geom
        pts = [QPointF(*g.to_px(x, y)) for x, y in s.points]
        w = max(s.width_m / g.resolution, 1.0)
        col = QCOLORS[s.color]
        if s.type == "rect":
            r = QRectF(pts[0], pts[1]).normalized()
            if s.filled:
                p.fillRect(r, col)
            else:
                pen = QPen(col, w)
                pen.setJoinStyle(Qt.MiterJoin)
                p.setPen(pen)
                p.setBrush(Qt.NoBrush)
                p.drawRect(r)
        else:
            pen = QPen(col, w, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            p.setPen(pen)
            p.drawPolyline(QPolygonF(pts))

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(60, 60, 60))
        p.translate(self.off)
        p.scale(self.zoom, self.zoom)
        g = self.geom
        if self.show_raster:
            p.drawImage(0, 0, self.raster())
        else:
            p.drawImage(0, 0, self.base_img)
            for s in self.shapes:
                self._paint_shape(p, s)
        if self.draft:
            self._paint_shape(p, self.draft)
        # 맵 경계
        edge = QPen(QColor(255, 200, 0), 1)
        edge.setCosmetic(True)
        p.setPen(edge)
        p.setBrush(Qt.NoBrush)
        p.drawRect(QRectF(0, 0, g.width_px, g.height_px))
        # 선택 표시 + 핸들 (화면 좌표)
        if 0 <= self.selected < len(self.shapes):
            s = self.shapes[self.selected]
            hi = QPen(QColor(0, 200, 255), 1, Qt.DashLine)
            hi.setCosmetic(True)
            p.setPen(hi)
            pts = [QPointF(*g.to_px(x, y)) for x, y in s.points]
            if s.type == "rect":
                p.drawRect(QRectF(pts[0], pts[1]).normalized())
            else:
                p.drawPolyline(QPolygonF(pts))
            p.resetTransform()
            p.setPen(QPen(QColor(0, 200, 255), 1))
            p.setBrush(QColor(255, 255, 255))
            for h in self._handles(s):
                sp = self.world_to_screen(*h)
                p.drawRect(QRectF(sp.x() - 4, sp.y() - 4, 8, 8))
        # 원점 (world 0,0) 표시: 화면 크기 고정. x 는 오른쪽(빨강), y 는 아래쪽(초록)
        p.resetTransform()
        o = self.ipx_to_screen(*g.to_px(0.0, 0.0))
        p.setPen(QPen(QColor(255, 70, 70), 2))
        p.drawLine(o, o + QPointF(36, 0))
        p.drawText(o + QPointF(40, 4), "x")
        p.setPen(QPen(QColor(70, 220, 70), 2))
        p.drawLine(o, o + QPointF(0, 36))
        p.drawText(o + QPointF(-3, 50), "y")
        p.setPen(QPen(QColor(255, 0, 255), 2))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(o, 5, 5)
        p.drawText(o + QPointF(8, -8), "(0, 0)")
        if self.tool == "origin" and self.hover_corner:  # 클릭하면 원점이 될 위치 미리보기
            h = self.ipx_to_screen(*self.hover_corner)
            p.setPen(QPen(QColor(255, 255, 0), 1, Qt.DashLine))
            p.drawLine(QPointF(h.x() - 14, h.y()), QPointF(h.x() + 14, h.y()))
            p.drawLine(QPointF(h.x(), h.y() - 14), QPointF(h.x(), h.y() + 14))
        p.end()

    def _handles(self, s: M.Shape) -> list[tuple[float, float]]:
        if s.type == "rect":
            (ax, ay), (bx, by) = s.points
            x0, x1, y0, y1 = min(ax, bx), max(ax, bx), min(ay, by), max(ay, by)
            return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        return [(x, y) for x, y in s.points]

    # -- 입력 -----------------------------------------------------------------
    def wheelEvent(self, e) -> None:
        f = 1.25 ** (e.angleDelta().y() / 120.0)
        nz = max(0.05, min(60.0, self.zoom * f))
        f = nz / self.zoom
        c = e.position()
        self.off = QPointF(c.x() - (c.x() - self.off.x()) * f, c.y() - (c.y() - self.off.y()) * f)
        self.zoom = nz
        self.update()
        self._report(c)

    def keyPressEvent(self, e) -> None:
        k = e.key()
        if k == Qt.Key_Space:
            self.space = True
        elif k in (Qt.Key_Delete, Qt.Key_Backspace):
            self.delete_selected()
        elif k == Qt.Key_Escape:
            self.draft = None
            self.drag = None
            self.select(-1)
        elif k == Qt.Key_F:
            self.fit()
        else:
            super().keyPressEvent(e)

    def keyReleaseEvent(self, e) -> None:
        if e.key() == Qt.Key_Space:
            self.space = False
        super().keyReleaseEvent(e)

    def _report(self, pos: QPointF) -> None:
        x, y = self.screen_to_world(pos)
        px, py = self.geom.to_px(x, y)
        self.on_cursor(f"world ({x:.3f}, {y:.3f}) m   픽셀 ({int(math.floor(px))}, {int(math.floor(py))})   줌 {self.zoom * 100:.0f}%")

    def _snap_angle(self, a, b) -> list[float]:
        dx, dy = b[0] - a[0], b[1] - a[1]
        L = math.hypot(dx, dy)
        ang = round(math.atan2(dy, dx) / (math.pi / 12)) * (math.pi / 12)
        return [a[0] + L * math.cos(ang), a[1] + L * math.sin(ang)]

    def _snap_square(self, a, b) -> list[float]:
        dx, dy = b[0] - a[0], b[1] - a[1]
        s = max(abs(dx), abs(dy))
        return [a[0] + math.copysign(s, dx or 1), a[1] + math.copysign(s, dy or 1)]

    def _corner_at(self, pos: QPointF) -> tuple[int, int]:
        """화면 위치에서 가장 가까운 픽셀 모서리 (이미지 픽셀 좌표)."""
        return (round((pos.x() - self.off.x()) / self.zoom), round((pos.y() - self.off.y()) / self.zoom))

    def _pick_handle(self, pos: QPointF) -> int:
        if not (0 <= self.selected < len(self.shapes)):
            return -1
        for i, h in enumerate(self._handles(self.shapes[self.selected])):
            sp = self.world_to_screen(*h)
            if abs(sp.x() - pos.x()) <= HANDLE_PX and abs(sp.y() - pos.y()) <= HANDLE_PX:
                return i
        return -1

    def _pick_shape(self, pos: QPointF) -> int:
        w = self.screen_to_world(pos)
        tol = PICK_TOL_PX / self.zoom * self.geom.resolution
        for i in range(len(self.shapes) - 1, -1, -1):
            if M.hit_test(self.shapes[i], w, tol):
                return i
        return -1

    def mousePressEvent(self, e) -> None:
        self.setFocus()
        pos = e.position()
        if e.button() in (Qt.MiddleButton, Qt.RightButton) or (e.button() == Qt.LeftButton and self.space):
            self.pan_last = pos
            return
        if e.button() != Qt.LeftButton:
            return
        if self.tool == "origin":
            self.set_origin_px(*self._corner_at(pos))
            return
        w = list(self.screen_to_world(pos))
        if self.tool == "select":
            h = self._pick_handle(pos)
            if h >= 0:
                s = self.shapes[self.selected]
                if s.type == "rect":  # 핸들 번호가 모서리와 대응하도록 먼저 정규화
                    (ax, ay), (bx, by) = s.points
                    s.points = [[min(ax, bx), min(ay, by)], [max(ax, bx), max(ay, by)]]
                self.drag = {"kind": "handle", "h": h, "snap": self._snap(), "moved": False}
                return
            i = self._pick_shape(pos)
            self.select(i)
            if i >= 0:
                self.drag = {"kind": "move", "last": w, "snap": self._snap(), "moved": False}
            return
        self.select(-1)
        if self.tool in ("rect", "line"):
            self.draft = M.Shape(self.tool, self.color, [w[:], w[:]], self.width_m, self.filled)
        else:
            self.draft = M.Shape("path", self.color, [w[:]], self.width_m)

    def mouseMoveEvent(self, e) -> None:
        pos = e.position()
        self._report(pos)
        if self.pan_last is not None:
            self.off += pos - self.pan_last
            self.pan_last = pos
            self.update()
            return
        if self.tool == "origin":
            self.hover_corner = self._corner_at(pos)
            self.update()
            return
        w = list(self.screen_to_world(pos))
        if self.draft:
            d = self.draft
            if d.type == "path":
                last = d.points[-1]
                if math.hypot(w[0] - last[0], w[1] - last[1]) >= 1.0 / self.zoom * self.geom.resolution:
                    d.points.append(w)
            else:
                shift = bool(e.modifiers() & Qt.ShiftModifier)
                d.points[1] = (self._snap_angle(d.points[0], w) if d.type == "line" else self._snap_square(d.points[0], w)) if shift else w
            self.update()
        elif self.drag:
            dr = self.drag
            if not dr["moved"]:  # 클릭만 한 경우엔 이력에 안 남긴다
                self.push_undo(dr["snap"])
                dr["moved"] = True
            s = self.shapes[self.selected]
            if dr["kind"] == "move":
                M.move_shape(s, w[0] - dr["last"][0], w[1] - dr["last"][1])
                dr["last"] = w
            else:
                h = dr["h"]
                if s.type == "rect":
                    px = 0 if h in (0, 3) else 1
                    py = 0 if h in (0, 1) else 1
                    s.points[px][0] = w[0]
                    s.points[py][1] = w[1]
                else:
                    s.points[h] = w
            self.changed()
        elif self.tool == "select":
            self.setCursor(Qt.SizeAllCursor if self._pick_handle(pos) >= 0 else Qt.ArrowCursor)

    def mouseReleaseEvent(self, e) -> None:
        if e.button() in (Qt.MiddleButton, Qt.RightButton, Qt.LeftButton) and self.pan_last is not None:
            self.pan_last = None
            return
        if e.button() != Qt.LeftButton:
            return
        self.drag = None
        d, self.draft = self.draft, None
        if d is None:
            return
        res = self.geom.resolution
        if d.type == "path":
            d.points = M.rdp(d.points, res * 0.7)
            if len(d.points) < 2:
                self.update()
                return
        else:
            (ax, ay), (bx, by) = d.points
            if math.hypot(bx - ax, by - ay) < 1.0 / self.zoom * res * 2:  # 클릭 수준이면 버린다
                self.update()
                return
        self.push_undo()
        self.shapes.append(d)
        self.selected = len(self.shapes) - 1
        self.changed()


class Editor(QMainWindow):
    def __init__(self, meta_path: Path, meta: dict, geom: M.MapGeom, base: np.ndarray, shapes: list[M.Shape],
                 needs_confirm: bool) -> None:
        super().__init__()
        self.dir = meta_path.parent
        self.meta, self.geom, self.base = meta, geom, base
        self.free_thresh = int(meta.get("free_thresh", 192))
        self.needs_confirm = needs_confirm  # 디스크의 edited 가 grid+edits 렌더와 달라서 첫 저장 때 확인
        self.baseline_comps = drivable_components(M.render(base, shapes, geom), self.free_thresh)
        self.saved_origin = (geom.origin_x, geom.origin_y)  # 디스크 fms_map.json 의 값
        self.canvas = Canvas(geom, base, shapes)
        self.canvas.on_change = self.refresh_panel
        self.canvas.on_cursor = lambda s: self.cursor_label.setText(s)
        self._building = False

        side = QWidget()
        side.setFixedWidth(250)
        v = QVBoxLayout(side)

        v.addWidget(QLabel("도구"))
        self.tool_group = QButtonGroup(self)
        row = QHBoxLayout()
        for key, text in (("select", "선택(V)"), ("rect", "사각형(R)"), ("line", "선분(L)"), ("path", "자유형(P)")):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setProperty("tool", key)
            b.setFocusPolicy(Qt.NoFocus)
            self.tool_group.addButton(b)
            row.addWidget(b)
            if key == "select":
                b.setChecked(True)
        v.addLayout(row)
        row = QHBoxLayout()
        b = QPushButton("원점 지정(O)")
        b.setCheckable(True)
        b.setProperty("tool", "origin")
        b.setFocusPolicy(Qt.NoFocus)
        b.setToolTip("클릭한 픽셀 모서리를 world (0, 0) 으로 만든다. 이미지/도형은 그대로, 좌표계만 옮긴다.")
        self.tool_group.addButton(b)
        row.addWidget(b)
        v.addLayout(row)
        self.origin_label = QLabel()
        self.origin_label.setWordWrap(True)
        v.addWidget(self.origin_label)
        self.tool_group.buttonClicked.connect(lambda b: self.set_tool(b.property("tool")))

        v.addWidget(QLabel("색"))
        row = QHBoxLayout()
        self.color_btns = {}
        for key in ("white", "black", "gray"):
            r = QRadioButton(COLOR_KO[key])
            r.setFocusPolicy(Qt.NoFocus)
            r.toggled.connect(lambda on, k=key: on and self.on_color(k))
            self.color_btns[key] = r
            row.addWidget(r)
        self.color_btns["black"].setChecked(True)
        v.addLayout(row)

        row = QHBoxLayout()
        row.addWidget(QLabel("굵기(m)"))
        self.width = QDoubleSpinBox()
        self.width.setDecimals(3)
        self.width.setRange(0.01, 5.0)
        self.width.setSingleStep(0.01)
        self.width.setValue(0.0625)
        self.width.valueChanged.connect(self.on_width)
        row.addWidget(self.width)
        self.width_px = QLabel()
        row.addWidget(self.width_px)
        v.addLayout(row)
        self.on_width(self.width.value())

        self.filled = QCheckBox("사각형 채움")
        self.filled.setChecked(True)
        self.filled.setFocusPolicy(Qt.NoFocus)
        self.filled.toggled.connect(self.on_filled)
        v.addWidget(self.filled)

        self.raster_chk = QCheckBox("결과 미리보기 (실제 픽셀)")
        self.raster_chk.setFocusPolicy(Qt.NoFocus)
        self.raster_chk.toggled.connect(self.on_raster)
        v.addWidget(self.raster_chk)

        v.addWidget(QLabel("도형 (위쪽이 위에 덮임)"))
        self.listw = QListWidget()
        self.listw.setFocusPolicy(Qt.NoFocus)
        self.listw.currentRowChanged.connect(self.on_row)
        v.addWidget(self.listw, 1)
        row = QHBoxLayout()
        for text, fn in (("위로", lambda: self.canvas.move_selected_z(+1)), ("아래로", lambda: self.canvas.move_selected_z(-1)),
                         ("삭제", self.canvas.delete_selected)):
            b = QPushButton(text)
            b.setFocusPolicy(Qt.NoFocus)
            b.clicked.connect(fn)
            row.addWidget(b)
        v.addLayout(row)
        save = QPushButton("저장 (Ctrl+S)")
        save.setFocusPolicy(Qt.NoFocus)
        save.clicked.connect(self.save)
        v.addWidget(save)

        root = QWidget()
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self.canvas, 1)
        h.addWidget(side)
        self.setCentralWidget(root)
        self.cursor_label = QLabel()
        self.statusBar().addPermanentWidget(self.cursor_label)

        def act(key, fn):
            a = QAction(self)
            a.setShortcut(QKeySequence(key))
            a.triggered.connect(fn)
            self.addAction(a)

        act("Ctrl+S", self.save)
        act("Ctrl+Z", self.canvas.do_undo)
        act("Ctrl+Shift+Z", self.canvas.do_redo)
        act("Ctrl+Y", self.canvas.do_redo)
        for key, tool in (("V", "select"), ("R", "rect"), ("L", "line"), ("P", "path"), ("O", "origin")):
            act(key, lambda t=tool: self.pick_tool(t))
        self.refresh_panel()
        self.resize(1280, 800)

    # -- 패널 <-> 캔버스 ------------------------------------------------------
    def pick_tool(self, tool: str) -> None:
        for b in self.tool_group.buttons():
            if b.property("tool") == tool:
                b.setChecked(True)
        self.set_tool(tool)

    def set_tool(self, tool: str) -> None:
        self.canvas.tool = tool
        self.canvas.hover_corner = None
        self.canvas.setCursor(Qt.CrossCursor if tool == "origin" else Qt.ArrowCursor)
        if tool != "select":
            self.canvas.select(-1)
        self.canvas.update()

    def on_color(self, key: str) -> None:
        if self._building:
            return
        self.canvas.color = key
        self.canvas.edit_selected(color=key)

    def on_width(self, val: float) -> None:
        self.width_px.setText(f"= {val / self.geom.resolution:.1f} px")
        if self._building:
            return
        self.canvas.width_m = val
        self.canvas.edit_selected(width_m=val)

    def on_filled(self, on: bool) -> None:
        if self._building:
            return
        self.canvas.filled = on
        s = self.canvas.shapes[self.canvas.selected] if 0 <= self.canvas.selected < len(self.canvas.shapes) else None
        if s and s.type == "rect":
            self.canvas.edit_selected(filled=on)

    def on_raster(self, on: bool) -> None:
        self.canvas.show_raster = on
        self.canvas.update()

    def on_row(self, row: int) -> None:
        if self._building:
            return
        n = len(self.canvas.shapes)
        self.canvas.select(n - 1 - row if row >= 0 else -1)

    def refresh_panel(self) -> None:
        c = self.canvas
        self._building = True
        try:
            self.listw.clear()
            n = len(c.shapes)
            for i in range(n - 1, -1, -1):
                s = c.shapes[i]
                self.listw.addItem(f"#{i + 1}  {TYPE_KO[s.type]}  {COLOR_KO[s.color]}")
            if 0 <= c.selected < n:
                self.listw.setCurrentRow(n - 1 - c.selected)
                s = c.shapes[c.selected]
                self.color_btns[s.color].setChecked(True)
                self.width.setValue(s.width_m)
                if s.type == "rect":
                    self.filled.setChecked(s.filled)
        finally:
            self._building = False
        g = self.geom
        ox, oy = -g.origin_x / g.resolution, -g.origin_y / g.resolution
        moved = (g.origin_x, g.origin_y) != self.saved_origin
        self.origin_label.setText(f"원점 (0,0) = 픽셀 ({ox:g}, {oy:g})\norigin_x/y = ({g.origin_x:g}, {g.origin_y:g}) m"
                                  + ("  [저장 전]" if moved else ""))
        self.setWindowTitle(f"맵 편집기 - {self.meta.get('name', self.dir.name)}{' *' if c.dirty else ''}")

    # -- 저장 -----------------------------------------------------------------
    def save(self) -> None:
        c = self.canvas
        if self.needs_confirm:
            r = QMessageBox.question(
                self, "덮어쓰기 확인",
                "디스크의 edited_navi_gridmap.png 가 grid.png + edits.json 으로 만든 결과와 다르다.\n"
                "(다른 도구로 직접 수정했을 수 있다.) 저장하면 이 파일을 grid.png + 도형으로 새로 만든다. 계속할까?")
            if r != QMessageBox.Yes:
                return
            self.needs_confirm = False
        g = self.geom
        origin = (g.origin_x, g.origin_y)
        if origin != self.saved_origin:
            r = QMessageBox.question(
                self, "원점 변경 확인",
                f"fms_map.json 의 origin 을 ({self.saved_origin[0]:g}, {self.saved_origin[1]:g}) 에서 ({origin[0]:g}, {origin[1]:g}) 로 바꾼다.\n"
                "이 맵의 world 좌표를 쓰는 곳(로봇 시작 위치, 오더/구역 좌표, FMS 쪽 저장값)은 같이 바꿔야 한다. 계속할까?")
            if r != QMessageBox.Yes:
                return
            try:
                self.write_origin(origin)
            except (OSError, ValueError, KeyError) as ex:
                QMessageBox.critical(self, "fms_map.json 저장 실패", str(ex))
                return
        img = M.render(self.base, c.shapes, self.geom)
        M.save_edits(self.dir / "edits.json", self.geom, c.shapes)
        Image.fromarray(img, "L").save(self.dir / "edited_navi_gridmap.png")
        c.dirty = False
        self.refresh_panel()
        comps = drivable_components(img, self.free_thresh)
        msg = f"저장 완료: 도형 {len(c.shapes)}개, 주행 가능 영역 {comps}조각"
        if comps > self.baseline_comps:
            QMessageBox.warning(self, "통로 끊김 의심",
                                f"주행 가능 영역이 {self.baseline_comps}조각에서 {comps}조각으로 늘었다 (로봇 반폭 {ROBOT_RADIUS_PX}px 팽창 기준).\n"
                                "벽이 통로를 막았는지 확인해라. 저장은 이미 끝났다.")
        self.baseline_comps = comps
        self.statusBar().showMessage(msg, 8000)

    def write_origin(self, origin: tuple[float, float]) -> None:
        """fms_map.json 의 origin_x/origin_y 두 값만 고쳐 원자적으로 쓴다 (다른 키는 디스크에 있는 그대로)."""
        path = self.dir / "fms_map.json"
        meta = json.loads(path.read_text(encoding="utf-8"))
        meta["origin_x"], meta["origin_y"] = origin
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
        self.meta["origin_x"], self.meta["origin_y"] = origin
        self.saved_origin = origin

    def closeEvent(self, e) -> None:
        if self.canvas.dirty:
            r = QMessageBox.question(self, "저장 안 됨", "저장하지 않은 수정이 있다. 버리고 종료할까?")
            if r != QMessageBox.Yes:
                e.ignore()
                return
        e.accept()


def fail(msg: str) -> int:
    QMessageBox.critical(None, "맵 편집기를 시작할 수 없다", msg)
    print(f"오류: {msg}", file=sys.stderr)
    return 1


def load_map(meta_path: Path):
    """필수 파일을 검사하고 (meta, geom, base, shapes, needs_confirm) 을 돌려준다. 문제면 ValueError(사용자용 메시지)."""
    if not meta_path.is_file():
        raise ValueError(f"fms_map.json 이 없다: {meta_path}")
    d = meta_path.parent
    missing = [n for n in ("grid.png", "edited_navi_gridmap.png") if not (d / n).is_file()]
    if missing:
        raise ValueError(f"맵 폴더 {d} 에 필수 파일이 없다: {', '.join(missing)}\n"
                         "grid.png 와 edited_navi_gridmap.png 가 모두 있어야 편집할 수 있다.")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    geom = M.MapGeom.from_meta(meta)
    base = np.array(Image.open(d / "grid.png").convert("L"))
    edited = np.array(Image.open(d / "edited_navi_gridmap.png").convert("L"))
    want = (geom.height_px, geom.width_px)
    for name, a in (("grid.png", base), ("edited_navi_gridmap.png", edited)):
        if a.shape != want:
            raise ValueError(f"{name} 크기 {a.shape[::-1]} 가 fms_map.json 의 {want[::-1]} 와 다르다.")
    shapes = M.load_edits(d / "edits.json", geom)
    needs_confirm = not np.array_equal(M.render(base, shapes, geom), edited)
    return meta, geom, base, shapes, needs_confirm


def main() -> int:
    app = QApplication(sys.argv)
    if len(sys.argv) > 1:
        meta_path = Path(sys.argv[1])
        if meta_path.is_dir():
            meta_path = meta_path / "fms_map.json"
    else:
        f, _ = QFileDialog.getOpenFileName(None, "fms_map.json 선택", str(Path(__file__).resolve().parent.parent / "maps"),
                                           "fms_map.json (fms_map.json);;JSON (*.json)")
        if not f:
            return 0
        meta_path = Path(f)
    try:
        meta, geom, base, shapes, needs_confirm = load_map(meta_path)
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as ex:
        return fail(str(ex))
    if meta.get("image") != "edited_navi_gridmap.png":
        QMessageBox.warning(None, "메타 이미지 불일치",
                            f"fms_map.json 의 image 가 {meta.get('image')!r} 이다. 편집기는 edited_navi_gridmap.png 에 저장하므로 "
                            "로봇/뷰어가 이 결과를 쓰려면 image 를 edited_navi_gridmap.png 로 바꿔야 한다. (편집기는 수정하지 않는다.)")
    w = Editor(meta_path, meta, geom, base, shapes, needs_confirm)
    w.show()
    # 런처가 보내는 SIGTERM/SIGINT 는 창 닫기로 바꾼다 -> closeEvent 의 "저장 안 됨" 확인이 그대로 뜬다.
    # 취소하면 계속 실행된다. 빈 타이머는 Qt 루프 중에도 파이썬 시그널 핸들러가 돌 기회를 주려는 것.
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: QTimer.singleShot(0, w.close))
    tick = QTimer()
    tick.timeout.connect(lambda: None)
    tick.start(200)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

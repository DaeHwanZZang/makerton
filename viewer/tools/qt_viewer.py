"""
Qt 라이브 플릿 뷰어 (표시 전용)
==============================

matplotlib 버전(`tools/map_viewer.py`)을 대체하는 실시간 뷰어. PySide6 +
QGraphicsView 기반 — GoCart Viz 같은 로봇 시각화 툴의 표준 구성이다.

**이 모듈도 로봇을 시뮬레이션하지 않는다.** 여전히 순수 표시 레이어다.
matplotlib 버전과 똑같이 로봇이 보고한 값만 그리고, 아무것도 발행하지 않는다.
MQTT 구독/파싱은 `tools/fleet_monitor.py` 의 `FleetMonitor` 를 그대로 재사용한다
(그쪽은 처음부터 렌더러에 의존하지 않게 짜여 있었다 — `update_robot()` /
`set_connection()` 두 메서드만 호출한다).

라이브로 보려면:
    python tools/fleet_monitor_qt.py maps/warehouse/warehouse.json

matplotlib 버전 대비 얻는 것
    - 확대/축소/이동이 Qt 기본 기능 (QGraphicsView) — 직접 짤 필요 없다
    - 창 크기 조절 시 그냥 더 넓은 캔버스가 보인다 (matplotlib 의
      aspect="datalim" 같은 우회가 필요 없다)
    - 로봇/경로가 진짜 객체(QGraphicsItem)라서 좌표만 갱신하면 된다
"""

from __future__ import annotations

import json
import math
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush, QColor, QFont, QIcon, QImage, QPainterPath, QPen, QPixmap, QPolygonF,
)
from PySide6.QtWidgets import (
    QGraphicsLineItem, QGraphicsPathItem, QGraphicsPixmapItem, QGraphicsPolygonItem,
    QGraphicsRectItem, QGraphicsScene, QGraphicsView,
    QLabel, QMainWindow,
)

from common.map_model import MapModel

ICON_PATH = Path(__file__).resolve().parent.parent / "assets" / "icons" / "fleet_viewer_icon.png"

# matplotlib 버전과 같은 팔레트를 쓴다 (일관성)
CELL_COLORS = [QColor("#1c1c1e"), QColor("#9aa0a6"), QColor("#ffffff")]  # OCCUPIED/UNKNOWN/FREE
ROBOT_PALETTE = [
    "#e5484d", "#0090ff", "#30a46c", "#f5a623", "#8e4ec6",
    "#e93d82", "#12a594", "#d6409f", "#f76b15", "#3e63dd",
    "#46a758", "#ab4aba", "#e54666", "#0d9488", "#ca8a04",
    "#7c3aed", "#dc2626", "#0284c7", "#65a30d", "#c026d3",
]

STALE_AFTER = 2.0          # 이 시간 동안 상태 보고가 없으면 흐리게
EVENT_LOG_MAX_LINES = 14
PATH_TICK_SPACING_PX = 14  # 경로선 눈금 간격 (화면 픽셀 아니라 scene 단위 — 아래 참고)
PHYSICS_FRESH = 0.5        # 이 시간 안에 받은 physics 토픽만 믿고 자세를 외삽한다
EXTRAPOLATE_MAX = 0.2      # 외삽은 최대 이만큼만 (그 이상은 멈춰 보이는 쪽이 낫다)
TRAIL_MAX = 4000           # 지나간 궤적 점 개수 상한
TRAIL_MIN_STEP = 0.03      # 이만큼(m) 움직여야 궤적에 점을 더한다
WP_PASSED_DIST = 0.20      # 로봇이 경유점에 이만큼(m) 안으로 오면 이미 지난 걸로 보고 선에서 뺀다
FORCE_ALERT = 5.0          # 벽 접촉력(N)이 이보다 크면 GT 윤곽을 빨갛게
GT_COLOR = "#00e5ff"       # 참 위치(ground truth) 표시 색
ODOM_COLOR = "#ffd60a"     # 보정 전 순수 오도메트리 표시 색

# zone 타입별 색 — vendor zone_meta.json 을 그리는 용도 (표시 전용, 로봇 내비게이션엔
# 관여 안 한다 — 그쪽은 fms_server 가 grid 에 구워서 각 로봇이 직접 받는다)
ZONE_COLORS = {
    "prohibit": QColor(255, 59, 48),   # 진입 금지 — 빨강
    "occupancy": QColor(89, 200, 190),  # 한 대씩 통과하는 점유 존
    "elevator": QColor(171, 154, 238), # 엘리베이터 구역 — 보라 (표시 전용)
    "slow": QColor(255, 159, 10),      # 감속 구역 — 주황
    "oneway": QColor(10, 132, 255),    # 일방통행 — 파랑
}
ZONE_DEFAULT_COLOR = QColor(154, 160, 166)  # 모르는 타입 — 회색


# -- 좌표 변환 ----------------------------------------------------------------
# scene 좌표계 = 이미지 픽셀 좌표계 그대로 쓴다 (1 scene unit = 1 px = resolution m).
# common/map_model.py 가 이제 world y 도 아래로 증가하는 규약(origin=좌상단, 뒤집기 없음)
# 을 쓰므로 QGraphicsView 의 기본 y-down 좌표계와 방향이 그대로 맞아 떨어진다 —
# 뒤집을 필요가 없다. world_to_pixel/pixel_to_world 와 동일한 식의 연속값 버전이다.

def world_to_scene(meta, x: float, y: float) -> QPointF:
    sx = (x - meta.origin_x) / meta.resolution
    sy = (y - meta.origin_y) / meta.resolution
    return QPointF(sx, sy)


def scene_to_world(meta, sx: float, sy: float) -> tuple[float, float]:
    x = meta.origin_x + sx * meta.resolution
    y = meta.origin_y + sy * meta.resolution
    return x, y


@dataclass
class RobotView:
    """뷰어가 아는 로봇 하나. 전부 로봇이 보고한 값이다 (matplotlib 버전과 동일 계약)."""

    robot_id: str
    x: float = 0.0
    y: float = 0.0
    theta: float = 0.0
    velocity: float = 0.0
    width: float = 0.5
    length: float = 0.7
    state: str = "?"
    battery: Optional[float] = None
    connection: str = "?"
    order_id: Optional[str] = None
    remaining_nodes: int = 0
    errors: list[str] = field(default_factory=list)
    last_seen: float = 0.0
    planned_path: list[tuple[float, float]] = field(default_factory=list)
    detected_obstacles: list[tuple[float, float, float, float]] = field(default_factory=list)
    has_pose: bool = False
    map_uid: Optional[str] = None           # 로봇이 보고한 State.map_id (FMS 맵 uid). None = 로컬 --map / 모름
    # -- 물리 디버그 (fms/v1/{id}/physics, 로봇이 tick 마다(20 Hz) 발행). 없으면 State 값만 쓴다 --
    physics: Optional[dict] = None
    physics_at: float = 0.0
    accel: float = 0.0                      # 측정 속도의 미분 (EMA)
    peak_slip: float = 0.0                  # 오더 동안 최대 |slip|
    peak_force: float = 0.0                 # 최대 벽 접촉력
    trail: deque = field(default_factory=lambda: deque(maxlen=TRAIL_MAX))      # 로봇이 보고한(추정) 위치 궤적
    gt_trail: deque = field(default_factory=lambda: deque(maxlen=TRAIL_MAX))   # 참 위치 궤적
    trail_dirty: bool = False
    # 이번 refresh 에서 그리는 자세 (물리 토픽으로 외삽한 값).
    # 로봇이 보고하는 자세 (오도메트리 추정 = State.pose). 몸체와 계획 경로는 이 좌표계다
    dx: float = 0.0
    dy: float = 0.0
    dth: float = 0.0

    def is_stale(self, now: float) -> bool:
        return self.has_pose and (now - self.last_seen) > STALE_AFTER

    def physics_fresh(self, now: float) -> bool:
        return self.physics is not None and (now - self.physics_at) < PHYSICS_FRESH

    def _extrapolate(self, now: float, x: float, y: float, theta: float) -> tuple[float, float, float]:
        p = self.physics
        dt = max(0.0, min(now - self.physics_at, EXTRAPOLATE_MAX))
        th = theta + p["yaw_rate"] * dt / 2   # 중점 방위로 적분
        return (x + p["v_fwd"] * math.cos(th) * dt - p["v_lat"] * math.sin(th) * dt,
                y + p["v_fwd"] * math.sin(th) * dt + p["v_lat"] * math.cos(th) * dt,
                theta + p["yaw_rate"] * dt)

    def display_pose(self, now: float) -> tuple[float, float, float]:
        """로봇이 보고하는 자세(추정). State(5 Hz) 보다 physics(20 Hz)가 새로우면 그 odom 값에서 속도로 외삽한다."""
        if not self.physics_fresh(now):
            return self.x, self.y, self.theta
        p = self.physics
        if "est_x" in p:   # 위치추정 보정을 얹은 추정 (State.pose). 옛 로봇은 est 가 없어 순수 오도메트리를 쓴다
            return self._extrapolate(now, p["est_x"], p["est_y"], p["est_theta"])
        return self._extrapolate(now, p["odom_x"], p["odom_y"], p["odom_theta"])

    def raw_odom_pose(self, now: float) -> Optional[tuple[float, float, float]]:
        """보정 전 순수 오도메트리 (바퀴 엔코더 적분만)."""
        if not self.physics_fresh(now):
            return None
        p = self.physics
        return self._extrapolate(now, p["odom_x"], p["odom_y"], p["odom_theta"])

    def truth_pose(self, now: float) -> Optional[tuple[float, float, float]]:
        """참 자세 (물리 엔진, 오차 없음). 물리 토픽이 없는 로봇은 None."""
        if not self.physics_fresh(now):
            return None
        p = self.physics
        return self._extrapolate(now, p["x"], p["y"], p["theta"])


class _OutlinedLabel(QGraphicsPathItem):
    """
    검은 테두리 + 흰색 채우기 텍스트. 흰 배경(FREE 셀) 위에서 흰 글자가 안 보이던
    문제 때문에 도입 — QGraphicsSimpleTextItem 은 외곽선을 못 그려서, 텍스트를
    QPainterPath 로 만들어 pen(외곽선)/brush(채우기)를 따로 준다. 배경이 검든
    희든 항상 보인다.
    """

    def __init__(self) -> None:
        super().__init__()
        self._font = QFont()
        outline = QPen(QColor("black"))
        outline.setWidthF(2.2)
        outline.setCosmetic(True)
        outline.setJoinStyle(Qt.RoundJoin)
        self.setPen(outline)
        self.setBrush(QBrush(QColor("white")))

    def font(self) -> QFont:
        return self._font

    def setFont(self, font: QFont) -> None:
        self._font = font

    def setText(self, text: str) -> None:
        path = QPainterPath()
        path.addText(0, 0, self._font, text)
        self.setPath(path)


class _RobotItem:
    """로봇 하나를 그리는 QGraphicsItem 묶음 (그룹 아님 — 라벨은 회전시키면 안 돼서 따로 둔다)."""

    def __init__(self, scene: QGraphicsScene, color: QColor):
        pen = QPen(QColor("white"))
        pen.setWidthF(1.4)
        pen.setCosmetic(True)

        self.body = QGraphicsRectItem()
        self.body.setBrush(QBrush(color))
        self.body.setPen(pen)
        self._pen = pen
        self._unloc_pen = QPen(QColor("#d92d43"))  # 위치를 모름: 빨간 점선 윤곽 + 흐리게
        self._unloc_pen.setWidthF(1.6)
        self._unloc_pen.setCosmetic(True)
        self._unloc_pen.setStyle(Qt.DashLine)
        self.body.setZValue(5)
        scene.addItem(self.body)

        # heading 은 body 의 자식이라 body 의 회전을 그대로 물려받는다
        self.heading = QGraphicsPolygonItem(self.body)
        self.heading.setBrush(QBrush(color))
        self.heading.setPen(pen)
        self.heading.setZValue(6)

        self.label = _OutlinedLabel()
        f = self.label.font()
        f.setPointSizeF(8.0)
        f.setBold(True)
        self.label.setFont(f)
        self.label.setZValue(7)
        scene.addItem(self.label)

        path_pen = QPen(color)
        path_pen.setWidthF(1.6)
        path_pen.setCosmetic(True)
        self.path_item = QGraphicsPathItem()
        self.path_item.setPen(path_pen)
        self.path_item.setZValue(3)
        scene.addItem(self.path_item)

        # 현재 구간(로봇 -> 다음 경유점): 남은 경로보다 굵고 밝게
        leg_pen = QPen(color.lighter(130))
        leg_pen.setWidthF(2.6)
        leg_pen.setCosmetic(True)
        self.leg_item = QGraphicsPathItem()
        self.leg_item.setPen(leg_pen)
        self.leg_item.setZValue(3.5)
        scene.addItem(self.leg_item)

        # 경유점 점 + 최종 목표 링
        wp_pen = QPen(QColor("white"))
        wp_pen.setWidthF(1.0)
        wp_pen.setCosmetic(True)
        self.wp_item = QGraphicsPathItem()
        self.wp_item.setPen(wp_pen)
        self.wp_item.setBrush(QBrush(color))
        self.wp_item.setZValue(3.6)
        scene.addItem(self.wp_item)
        goal_pen = QPen(color.lighter(150))
        goal_pen.setWidthF(2.2)
        goal_pen.setCosmetic(True)
        self.goal_item = QGraphicsPathItem()
        self.goal_item.setPen(goal_pen)
        self.goal_item.setZValue(3.7)
        scene.addItem(self.goal_item)
        self.goal_label = _OutlinedLabel()
        gf = self.goal_label.font()
        gf.setPointSizeF(7.5)
        gf.setBold(True)
        self.goal_label.setFont(gf)
        self.goal_label.setZValue(7)
        scene.addItem(self.goal_label)

        # 지나온 궤적 (실제 물리 자세)
        trail_pen = QPen(color)
        trail_pen.setWidthF(1.3)
        trail_pen.setCosmetic(True)
        self.trail_item = QGraphicsPathItem()
        self.trail_item.setPen(trail_pen)
        self.trail_item.setZValue(2.5)
        scene.addItem(self.trail_item)

        # 참 위치(ground truth, 물리 엔진 값, 오차 없음): 청록 윤곽 + 중심 십자 + "GT" 라벨 + 점선 궤적.
        # 몸체(로봇이 보고한 추정 위치)와 벌어진 만큼이 누적 오차다. 보정 기준으로 쓴다
        gt_pen = QPen(QColor(GT_COLOR))
        gt_pen.setWidthF(1.8)
        gt_pen.setCosmetic(True)
        self.gt = QGraphicsRectItem()
        self.gt.setPen(gt_pen)
        self.gt.setZValue(6.2)
        self.gt.setVisible(False)
        scene.addItem(self.gt)
        self.gt_cross = QGraphicsPathItem(self.gt)   # gt 의 자식: 위치/회전을 따라간다
        self.gt_cross.setPen(gt_pen)
        self.gt_label = _OutlinedLabel()
        lf = self.gt_label.font()
        lf.setPointSizeF(7.0)
        lf.setBold(True)
        self.gt_label.setFont(lf)
        self.gt_label.setBrush(QBrush(QColor(GT_COLOR)))
        self.gt_label.setText("GT")
        self.gt_label.setZValue(7)
        self.gt_label.setVisible(False)
        scene.addItem(self.gt_label)
        # 보정 전 순수 오도메트리 (노란 점선 윤곽): 위치추정 보정이 얼마나 일하고 있는지 보인다
        odom_pen = QPen(QColor(ODOM_COLOR))
        odom_pen.setWidthF(1.2)
        odom_pen.setCosmetic(True)
        odom_pen.setStyle(Qt.DashLine)
        self.odom = QGraphicsRectItem()
        self.odom.setPen(odom_pen)
        self.odom.setZValue(4.5)
        self.odom.setVisible(False)
        scene.addItem(self.odom)
        gt_trail_pen = QPen(QColor(GT_COLOR))
        gt_trail_pen.setWidthF(1.3)
        gt_trail_pen.setCosmetic(True)
        gt_trail_pen.setStyle(Qt.DashLine)
        self.gt_trail_item = QGraphicsPathItem()
        self.gt_trail_item.setPen(gt_trail_pen)
        self.gt_trail_item.setZValue(2.6)
        scene.addItem(self.gt_trail_item)

        # 속도 벡터 (진행방향, 길이 = 속도 m/s)
        vel_pen = QPen(QColor("#32d74b"))
        vel_pen.setWidthF(2.4)
        vel_pen.setCosmetic(True)
        self.vel_item = QGraphicsLineItem()
        self.vel_item.setPen(vel_pen)
        self.vel_item.setZValue(6.5)
        self.vel_item.setVisible(False)
        scene.addItem(self.vel_item)

        # 이 로봇이 감지한 동적 장애물 — 개수가 매번 바뀌므로 필요한 만큼 만들고
        # 남는 건 재사용한다 (update_obstacles 에서 관리)
        self.obstacle_items: list[QGraphicsRectItem] = []

    def remove(self, scene: QGraphicsScene) -> None:
        for item in (self.body, self.label, self.path_item, self.leg_item, self.wp_item, self.goal_item,
                     self.goal_label, self.trail_item, self.odom, self.gt, self.gt_label, self.gt_trail_item, self.vel_item, *self.obstacle_items):
            scene.removeItem(item)

    def update_pose(self, meta, view: RobotView, label_text: str, alpha: float) -> None:
        length_px = max(view.length, 1e-3) / meta.resolution
        width_px = max(view.width, 1e-3) / meta.resolution

        self.body.setRect(-length_px / 2, -width_px / 2, length_px, width_px)
        tip = length_px * 0.5
        back = -length_px * 0.1
        half_w = width_px * 0.32
        self.heading.setPolygon(QPolygonF([
            QPointF(tip, 0), QPointF(back, -half_w), QPointF(back, half_w),
        ]))

        pos = world_to_scene(meta, view.dx, view.dy)
        self.body.setPos(pos)
        # world 도 y-down 이라 atan2(dy,dx) 가 Qt 의 "양수=시계방향" 규약과 그대로 맞는다
        self.body.setRotation(math.degrees(view.dth))
        # display_state 는 LOCALIZED 가 아닐 때만 "[UNLOCALIZED]"/"[LOCALIZING]" 을 붙인다 (common/schemas.py)
        unloc = "[UNLOCALIZED]" in view.state or "[LOCALIZING]" in view.state
        self.body.setPen(self._unloc_pen if unloc else self._pen)
        self.body.setOpacity(alpha * (0.4 if unloc else 1.0))

        self.label.setText(label_text)
        br = self.label.boundingRect()
        self.label.setPos(pos.x() - br.width() / 2, pos.y() - width_px * 0.9 - br.height())
        self.label.setOpacity(alpha)

    def update_path(self, meta, view: RobotView, show: bool, alpha: float) -> None:
        """계획 경로. 이미 지난 경유점은 빼고(State 는 5 Hz 라 한참 늦는다) 로봇이 보고하는 현재 위치(추정)에서 시작한다."""
        empty = QPainterPath()
        pts = list(view.planned_path)
        # 지난 경유점 제거: 마지막 하나는 남긴다 (도착 직전까지 목표를 보여준다)
        while len(pts) > 1 and math.hypot(pts[0][0] - view.dx, pts[0][1] - view.dy) < WP_PASSED_DIST:
            pts.pop(0)
        if not show or not pts:
            for it in (self.path_item, self.leg_item, self.wp_item, self.goal_item):
                it.setPath(empty)
            self.goal_label.setText("")
            return
        start = world_to_scene(meta, view.dx, view.dy)
        points = [start] + [world_to_scene(meta, x, y) for x, y in pts]

        # 현재 구간(굵게) + 남은 구간(눈금선)
        leg = QPainterPath(points[0])
        leg.lineTo(points[1])
        self.leg_item.setPath(leg)
        self.leg_item.setOpacity(alpha)
        rest = points[1:]
        self.path_item.setPath(_ticked_path(rest) if len(rest) > 1 else empty)
        self.path_item.setOpacity(alpha)

        # 경유점 점 (마지막은 목표 링으로 따로)
        r = 0.10 / meta.resolution
        dots = QPainterPath()
        for p in points[1:-1]:
            dots.addEllipse(p, r, r)
        self.wp_item.setPath(dots)
        self.wp_item.setOpacity(alpha)

        g = points[-1]
        gr = 0.18 / meta.resolution
        ring = QPainterPath()
        ring.addEllipse(g, gr, gr)
        ring.addEllipse(g, gr * 0.35, gr * 0.35)
        self.goal_item.setPath(ring)
        self.goal_item.setOpacity(alpha)

        remain = sum(math.hypot(b[0] - a[0], b[1] - a[1])
                     for a, b in zip([(view.dx, view.dy)] + pts, pts))
        self.goal_label.setText(f"{remain:.1f} m")
        br = self.goal_label.boundingRect()
        self.goal_label.setPos(g.x() - br.width() / 2, g.y() - gr - br.height() - 2)
        self.goal_label.setOpacity(alpha)

    def update_physics_items(self, meta, view: RobotView, show_trail: bool, show_gt: bool, show_odom: bool,
                             alpha: float, now: float) -> None:
        """궤적, 참 위치(GT) 윤곽, 속도 벡터. 물리 토픽이 없으면 GT/속도는 숨긴다."""
        def polyline(points) -> QPainterPath:
            path = QPainterPath()
            pts = [world_to_scene(meta, x, y) for x, y in points]
            if len(pts) >= 2:
                path.moveTo(pts[0])
                for q in pts[1:]:
                    path.lineTo(q)
            return path

        if view.trail_dirty:
            self.trail_item.setPath(polyline(view.trail))
            self.gt_trail_item.setPath(polyline(view.gt_trail))
            view.trail_dirty = False
        self.trail_item.setVisible(show_trail)
        self.trail_item.setOpacity(alpha * 0.8)
        self.gt_trail_item.setVisible(show_trail and show_gt)
        self.gt_trail_item.setOpacity(alpha * 0.8)

        truth = view.truth_pose(now)
        p = view.physics
        raw = view.raw_odom_pose(now)
        if raw is not None and "est_x" in (p or {}):
            lp = max(view.length, 1e-3) / meta.resolution
            wp = max(view.width, 1e-3) / meta.resolution
            self.odom.setRect(-lp / 2, -wp / 2, lp, wp)
            self.odom.setPos(world_to_scene(meta, raw[0], raw[1]))
            self.odom.setRotation(math.degrees(raw[2]))
            self.odom.setVisible(show_odom)
            self.odom.setOpacity(alpha)
        else:
            self.odom.setVisible(False)
        if truth is None or p is None:
            self.gt.setVisible(False)
            self.gt_label.setVisible(False)
            self.vel_item.setVisible(False)
            return
        gx, gy, gth = truth
        length_px = max(view.length, 1e-3) / meta.resolution
        width_px = max(view.width, 1e-3) / meta.resolution
        self.gt.setRect(-length_px / 2, -width_px / 2, length_px, width_px)
        cross = QPainterPath()
        c = min(length_px, width_px) * 0.3
        cross.moveTo(-c, 0)
        cross.lineTo(length_px / 2, 0)   # 앞쪽으로 긴 십자 = 참 방위
        cross.moveTo(0, -c)
        cross.lineTo(0, c)
        self.gt_cross.setPath(cross)
        pos = world_to_scene(meta, gx, gy)
        self.gt.setPos(pos)
        self.gt.setRotation(math.degrees(gth))
        # 벽에 눌리면(접촉력 큼) 참 위치 윤곽을 빨갛게 -- 벽에 닿는 건 참 몸체다
        pen = self.gt.pen()
        hit = p["wall_force"] > FORCE_ALERT
        pen.setColor(QColor("#ff453a") if hit else QColor(GT_COLOR))
        pen.setWidthF(3.0 if hit else 1.8)
        self.gt.setPen(pen)
        self.gt_cross.setPen(pen)
        self.gt.setVisible(show_gt)
        self.gt.setOpacity(alpha)
        br = self.gt_label.boundingRect()
        self.gt_label.setPos(pos.x() - br.width() / 2, pos.y() + width_px * 0.75)
        self.gt_label.setVisible(show_gt)
        self.gt_label.setOpacity(alpha)

        # 속도 벡터: 몸체(보고 위치) 중심에서 진행방향(후진이면 뒤)으로 v 미터
        c, sn = math.cos(view.dth), math.sin(view.dth)
        v = p["v_fwd"]
        a = world_to_scene(meta, view.dx, view.dy)
        b = world_to_scene(meta, view.dx + c * v * 1.0, view.dy + sn * v * 1.0)
        self.vel_item.setLine(a.x(), a.y(), b.x(), b.y())
        self.vel_item.setVisible(abs(v) > 0.01)
        self.vel_item.setOpacity(alpha)

    def update_obstacles(self, scene: QGraphicsScene, meta, view: RobotView, alpha: float) -> None:
        """이 로봇이 지금 감지한 장애물만 그린다 — 다른 로봇 화면엔 안 보일 수 있다."""
        obstacles = view.detected_obstacles
        while len(self.obstacle_items) < len(obstacles):
            item = QGraphicsRectItem()
            pen = QPen(QColor("white"))
            pen.setWidthF(1.0)
            pen.setCosmetic(True)
            item.setPen(pen)
            item.setBrush(QBrush(QColor("black")))   # 검은색 단색 — 정적 장애물과 같은 색으로 통일
            item.setZValue(4)
            scene.addItem(item)
            self.obstacle_items.append(item)
        while len(self.obstacle_items) > len(obstacles):
            scene.removeItem(self.obstacle_items.pop())

        for item, (ox, oy, ow, oh) in zip(self.obstacle_items, obstacles):
            w_px, h_px = ow / meta.resolution, oh / meta.resolution
            item.setRect(-w_px / 2, -h_px / 2, w_px, h_px)
            item.setPos(world_to_scene(meta, ox, oy))
            item.setOpacity(alpha)


def _ticked_path(points: list[QPointF]) -> QPainterPath:
    """GoCart Viz 스타일 — 경로선 위에 진행방향과 수직인 짧은 눈금을 일정 간격으로 찍는다."""
    path = QPainterPath(points[0])
    for p in points[1:]:
        path.lineTo(p)

    since_tick = 0.0
    tick_len = PATH_TICK_SPACING_PX * 0.4
    for a, b in zip(points, points[1:]):
        seg = QPointF(b.x() - a.x(), b.y() - a.y())
        seg_len = math.hypot(seg.x(), seg.y())
        if seg_len < 1e-6:
            continue
        ux, uy = seg.x() / seg_len, seg.y() / seg_len
        nx, ny = -uy, ux  # 진행방향에 수직인 단위벡터
        d = PATH_TICK_SPACING_PX - since_tick
        while d < seg_len:
            cx, cy = a.x() + ux * d, a.y() + uy * d
            path.moveTo(cx - nx * tick_len / 2, cy - ny * tick_len / 2)
            path.lineTo(cx + nx * tick_len / 2, cy + ny * tick_len / 2)
            path.moveTo(cx, cy)  # 다음 lineTo 가 눈금에서 이어지지 않도록
            d += PATH_TICK_SPACING_PX
        since_tick = seg_len - (d - PATH_TICK_SPACING_PX)
    return path


class MapCanvas(QGraphicsView):
    """맵 + 로봇을 그리는 캔버스. 확대/이동은 Qt 기본 기능을 그대로 쓴다."""

    def __init__(self, map_model: MapModel, parent=None, *, map_uid: Optional[str] = None,
                 map_label: Optional[str] = None):
        super().__init__(parent)
        self.map = map_model
        # 지금 보여 주는 맵의 uid (FMS 맵일 때). None 이면 정적 맵: 모든 로봇을 그린다.
        # 값이 있으면 다른 맵(층)을 보고한 로봇은 그리지 않고 패널에 "다른 층" 으로만 적는다
        self.map_uid = map_uid
        self.map_label = map_label or map_model.metadata.name
        self.uid_labels: dict[str, str] = {}   # uid -> 표시 이름 (fleet_monitor_qt 가 채운다)
        self.status_note = ""                  # 제목줄 끝에 붙는 상태 (맵 찾는 중 등)
        self.robots: dict[str, RobotView] = {}
        self._robot_items: dict[str, _RobotItem] = {}
        self._show_grid = True
        self._show_planned_path = True
        self._show_physics_hud = True
        self._show_trail = True
        self._show_gt = True
        self._show_odom = True
        self._follow: Optional[str] = None   # 시점을 따라다닐 로봇 id (f 로 순환, 드래그하면 해제)
        # robot_id -> 그 로봇의 가장 최근 이벤트 한 줄. 예전엔 deque 로 모든 로봇의
        # 이벤트를 시간순으로 쌓아서, 화면에 안 남은 지 오래된 로봇의 옛날 이벤트가
        # 계속 보이는 문제가 있었다 — 이제 로봇마다 최신 값 하나만 남긴다
        self._events: dict[str, str] = {}

        self.setRenderHint(self._antialiasing_hint())
        self.setDragMode(QGraphicsView.ScrollHandDrag)   # 드래그 이동 — Qt 기본 제공
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setBackgroundBrush(QBrush(QColor("#0b0b0d")))
        self.setMouseTracking(True)

        self._show_zones = True
        self._zone_items: list[QGraphicsPolygonItem] = []

        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._build_map_layer()
        self._build_zones()
        self._build_hud()
        self._fit_done = False

    @staticmethod
    def _antialiasing_hint():
        from PySide6.QtGui import QPainter
        return QPainter.Antialiasing

    # -- 맵 배경 ------------------------------------------------------------

    def _build_map_layer(self) -> None:
        m = self.map.metadata
        codes = self.map.classify_grid()  # 0=OCCUPIED,1=UNKNOWN,2=FREE
        rgb = np.zeros((*codes.shape, 3), dtype=np.uint8)
        for code, color in enumerate(CELL_COLORS):
            mask = codes == code
            rgb[mask, 0] = color.red()
            rgb[mask, 1] = color.green()
            rgb[mask, 2] = color.blue()
        self._classified_rgb = np.ascontiguousarray(rgb)
        self._raw_grey = np.ascontiguousarray(self.map.grid)

        self._map_item = QGraphicsPixmapItem()
        self._map_item.setZValue(0)
        self._scene.addItem(self._map_item)
        self._classified = True
        self._refresh_map_pixmap()

        self._scene.setSceneRect(0, 0, m.width_px, m.height_px)

        self._grid_item = QGraphicsPathItem()
        pen = QPen(QColor(0, 144, 255, 70))
        pen.setWidthF(0.0)
        pen.setCosmetic(True)
        self._grid_item.setPen(pen)
        self._grid_item.setZValue(1)
        self._scene.addItem(self._grid_item)
        self._build_grid_path()

        origin_pen = QPen(QColor("#ff3b30"))
        origin_pen.setWidthF(2.0)
        origin_pen.setCosmetic(True)
        cross = QPainterPath()
        r = 0.15 / m.resolution
        ox, oy = world_to_scene(m, 0.0, 0.0).x(), world_to_scene(m, 0.0, 0.0).y()
        cross.moveTo(ox - r, oy)
        cross.lineTo(ox + r, oy)
        cross.moveTo(ox, oy - r)
        cross.lineTo(ox, oy + r)
        origin_item = QGraphicsPathItem(cross)
        origin_item.setPen(origin_pen)
        origin_item.setZValue(9)
        self._scene.addItem(origin_item)

    def _build_zones(self) -> None:
        """
        `zone_meta.json` (벤더 원본, 맵 메타데이터와 같은 폴더) 을 읽어 다각형을
        그린다. 좌표는 이미 world 프레임이다 (fms_server/mapkit/bake.py 가 origin
        보정 없이 그대로 쓰는 것과 동일 — 검증됨). 파일이 없으면 조용히 건너뛴다.
        **표시 전용** — 로봇 내비게이션(장애물 회피)은 이 파일을 안 보고, FMS 가
        prohibit zone 을 grid 에 구워서 로봇에게 내려주는 별도 경로를 그대로 쓴다.
        """
        # FMS 번들(SET_MAP 캐시)은 annotations.json (polygon = [{x,y}]), 벤더 원본은 zone_meta.json ([[x,y]])
        zone_path = self.map.base_dir / "annotations.json"
        if not zone_path.is_file():
            zone_path = self.map.base_dir / "zone_meta.json"
        if not zone_path.is_file():
            return
        try:
            data = json.loads(zone_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[zone] {zone_path} 읽기 실패, 건너뜀: {exc}")
            return

        def xy(p):
            return (float(p["x"]), float(p["y"])) if isinstance(p, dict) else (float(p[0]), float(p[1]))

        m = self.map.metadata
        for entry in data.get("zones", []):
            polygon = entry.get("polygon") or []
            if len(polygon) < 3:
                continue
            ztype = str(entry.get("type", "")).lower()
            color = ZONE_COLORS.get(ztype, ZONE_DEFAULT_COLOR)

            points = [world_to_scene(m, *xy(p)) for p in polygon]
            item = QGraphicsPolygonItem(QPolygonF(points))
            fill = QColor(color)
            fill.setAlpha(90)
            item.setBrush(QBrush(fill))
            pen = QPen(color)
            pen.setWidthF(1.5)
            pen.setCosmetic(True)
            item.setPen(pen)
            item.setZValue(2)  # 맵(0)/격자(1) 위, 로봇(3+) 아래
            item.setToolTip(f"{entry.get('name', '')} ({ztype or 'unknown'})".strip())
            self._scene.addItem(item)
            self._zone_items.append(item)

        if self._zone_items:
            print(f"[zone] {zone_path.name} 에서 {len(self._zone_items)}개 zone 표시")

    def _refresh_map_pixmap(self) -> None:
        m = self.map.metadata
        if self._classified:
            arr = self._classified_rgb
            image = QImage(arr.data, m.width_px, m.height_px, 3 * m.width_px, QImage.Format_RGB888)
        else:
            arr = self._raw_grey
            image = QImage(arr.data, m.width_px, m.height_px, m.width_px, QImage.Format_Grayscale8)
        self._map_item.setPixmap(QPixmap.fromImage(image.copy()))

    def _build_grid_path(self, step_m: float = 1.0) -> None:
        m = self.map.metadata
        step_px = step_m / m.resolution
        path = QPainterPath()
        x = 0.0
        while x <= m.width_px:
            path.moveTo(x, 0)
            path.lineTo(x, m.height_px)
            x += step_px
        y = 0.0
        while y <= m.height_px:
            path.moveTo(0, y)
            path.lineTo(m.width_px, y)
            y += step_px
        self._grid_item.setPath(path)
        self._grid_item.setVisible(self._show_grid)

    # -- HUD 오버레이 (viewport 자식 위젯, scene 이 아니라 화면에 고정) -----------

    def _build_hud(self) -> None:
        def hud_label() -> QLabel:
            lbl = QLabel(self.viewport())
            lbl.setStyleSheet(
                "background-color: rgba(0,0,0,140); color: white; "
                "padding: 4px 7px; border-radius: 4px;"
            )
            lbl.setFont(QFont("Menlo, Consolas, monospace", 9))
            lbl.setTextFormat(Qt.PlainText)
            lbl.show()
            return lbl

        self.title_label = hud_label()
        self.legend_label = hud_label()
        self._update_title()
        self._update_legend()

        self.event_label = hud_label()
        self.event_label.setText("")
        self.event_label.setWordWrap(False)

        self.info_label = hud_label()
        self.info_label.setText("no robots")

        self.coord_label = hud_label()
        self.coord_label.setText("")

        # 물리 디버그 패널 (왼쪽, 이벤트 로그 아래). p 로 토글
        self.physics_label = hud_label()
        self.physics_label.setText("")

        self._layout_hud()

    def _update_title(self) -> None:
        m = self.map.metadata
        text = f"{self.map_label}  —  {m.width_m:g} x {m.height_m:g} m  @ {m.resolution:g} m/px"
        if self._follow:
            text += f"   [follow {self._follow}]"
        if self.status_note:
            text += f"   {self.status_note}"
        self.title_label.setText(text)

    def _update_legend(self) -> None:
        legend = "■ OCCUPIED   ■ UNKNOWN   ■ FREE"
        if self._zone_items:
            legend += "     ▨ PROHIBIT   ▨ ELEVATOR   ▨ SLOW   ▨ ONEWAY  (z 토글)"
        legend += "\n키: t 경로  p 물리패널  l 궤적  o GT(참 위치)  i 순수오도(노란)  f 시점고정  k 지우기  g 격자  r 보기초기화"
        self.legend_label.setText(legend)

    def set_status(self, note: str) -> None:
        if note != self.status_note:
            self.status_note = note
            self._update_title()
            self._layout_hud()

    def set_follow(self, robot_id: Optional[str]) -> None:
        self._follow = robot_id
        self._update_title()
        self._layout_hud()

    def set_map(self, map_model: MapModel, map_uid: Optional[str] = None, map_label: Optional[str] = None) -> None:
        """다른 맵(층)으로 바꾼다. 로봇 데이터는 유지하고 그림/궤적은 새 맵 좌표로 다시 그린다."""
        old_res = self.map.metadata.resolution
        was_placeholder = not self.map.metadata.image   # placeholder_model() 에서 처음 실제 맵으로
        self._scene.clear()          # 맵/격자/원점/구역/로봇 그림 전부 (C++ 객체가 지워진다)
        self._robot_items.clear()    # 지워진 그림을 다시 만지지 않게 참조도 버린다
        self._zone_items = []
        self.map = map_model
        self.map_uid = map_uid
        self.map_label = map_label or map_model.metadata.name
        for v in self.robots.values():   # 다른 층 좌표의 궤적은 의미가 없다
            v.trail.clear()
            v.gt_trail.clear()
            v.trail_dirty = True
        self._build_map_layer()
        self._build_zones()
        for item in self._zone_items:
            item.setVisible(self._show_zones)
        self._update_title()
        self._update_legend()
        self._layout_hud()
        if was_placeholder or self._follow is None:
            self.reset_view()   # 층 전체를 보여 준다 (따라가기 중이면 중심은 refresh 가 로봇에 맞춘다)
        else:
            f = old_res / map_model.metadata.resolution  # 화면 1 px 당 m 를 그대로 (층마다 해상도가 달라도 같은 줌)
            self.scale(f, f)

    def _on_this_map(self, view: RobotView) -> bool:
        return self.map_uid is None or view.map_uid is None or view.map_uid == self.map_uid

    def _layout_hud(self) -> None:
        # setText() 는 크기를 자동으로 안 늘려준다 — 배치 전에 매번 다시 재야 한다
        for lbl in (self.title_label, self.legend_label, self.event_label,
                    self.info_label, self.coord_label, self.physics_label):
            lbl.adjustSize()

        vp = self.viewport().rect()
        pad = 8
        self.title_label.move(pad, pad)
        self.legend_label.move(pad, vp.height() - self.legend_label.height() - pad)
        self.event_label.move(pad, self.title_label.geometry().bottom() + 4)
        self.physics_label.move(pad, self.event_label.geometry().bottom() + 6)
        self.physics_label.setVisible(self._show_physics_hud and bool(self.physics_label.text()))
        self.info_label.move(vp.width() - self.info_label.width() - pad, pad)
        self.coord_label.move(
            vp.width() - self.coord_label.width() - pad,
            vp.height() - self.coord_label.height() - pad,
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._layout_hud()
        if not self._fit_done:
            self.reset_view()
            self._fit_done = True

    # -- 로봇 데이터 갱신 (표시용 입력, fleet_monitor 가 호출) ------------------

    def update_robot(
        self, robot_id: str, x: float, y: float, theta: float = 0.0, *,
        velocity: float = 0.0, width: float = 0.5, length: float = 0.7,
        state: str = "?", battery: Optional[float] = None,
        order_id: Optional[str] = None, remaining_nodes: int = 0,
        errors: Optional[list[str]] = None,
        planned_path: Optional[list[tuple[float, float]]] = None,
        detected_obstacles: Optional[list[tuple[float, float, float, float]]] = None,
        map_id: Optional[str] = None,
        now: float = 0.0,
    ) -> None:
        view = self.robots.get(robot_id)
        if view is None:
            view = RobotView(robot_id=robot_id)
            self.robots[robot_id] = view
        if map_id != view.map_uid:   # 층이 바뀌었다: 이전 층 궤적은 버린다
            view.trail.clear()
            view.gt_trail.clear()
            view.trail_dirty = True
        view.map_uid = map_id
        view.x, view.y, view.theta = x, y, theta
        view.velocity = velocity
        view.width, view.length = width, length
        view.state = state
        view.battery = battery
        view.order_id = order_id
        view.remaining_nodes = remaining_nodes
        view.errors = errors or []
        view.planned_path = list(planned_path) if planned_path else []
        view.detected_obstacles = list(detected_obstacles) if detected_obstacles else []
        view.last_seen = now
        view.has_pose = True
        if not view.physics_fresh(now):  # 물리 토픽이 없는 로봇은 State 자세로 궤적을 쌓는다
            self._add_trail(view, x, y)

    @staticmethod
    def _add_trail(view: RobotView, x: float, y: float, trail: Optional[deque] = None) -> None:
        trail = view.trail if trail is None else trail
        if trail:
            lx, ly = trail[-1]
            d = math.hypot(x - lx, y - ly)
            if d > 1.0:      # 순간이동(강제 위치지정/재시작): 이어 그리면 가로지르는 선이 생긴다
                trail.clear()
            elif d < TRAIL_MIN_STEP:
                return
        trail.append((x, y))
        view.trail_dirty = True

    def update_physics(self, robot_id: str, data: dict, now: float = 0.0) -> None:
        """로봇이 발행한 물리 디버그 값 (fms/v1/{id}/physics). 표시 전용."""
        view = self.robots.get(robot_id)
        if view is None:
            view = RobotView(robot_id=robot_id)
            self.robots[robot_id] = view
        prev = view.physics
        if prev is not None and data["sim_time"] > prev["sim_time"]:
            dt = data["sim_time"] - prev["sim_time"]
            a = (data["v_fwd"] - prev["v_fwd"]) / dt
            view.accel = 0.5 * view.accel + 0.5 * a
        if prev is None or data["sim_time"] < prev["sim_time"]:   # 첫 값, 또는 로봇 재시작
            view.peak_slip = view.peak_force = 0.0
        if data["state"] == "IDLE" and data["at_rest"] and prev is not None and prev["state"] != "IDLE":
            view.peak_slip = view.peak_force = 0.0
        view.peak_slip = max(view.peak_slip, abs(data["slip"][0]), abs(data["slip"][1]))
        view.peak_force = max(view.peak_force, data["wall_force"])
        view.physics = data
        view.physics_at = now
        self._add_trail(view, data["odom_x"], data["odom_y"])           # 로봇이 보고한 위치 (= State.pose)
        self._add_trail(view, data["x"], data["y"], view.gt_trail)      # 참 위치

    @staticmethod
    def _physics_text(view: RobotView) -> str:
        p = view.physics
        if p is None:
            return ""
        sl, sr = p["slip"]
        has_est = "est_x" in p
        ex, ey, eth = (p["est_x"], p["est_y"], p["est_theta"]) if has_est else (p["odom_x"], p["odom_y"], p["odom_theta"])

        def ang(a: float) -> float:
            return math.degrees(math.atan2(math.sin(a), math.cos(a)))
        drift = math.hypot(ex - p["x"], ey - p["y"])           # 보고 위치 - GT
        dth = ang(eth - p["theta"])
        raw = math.hypot(p["odom_x"] - p["x"], p["odom_y"] - p["y"])   # 보정 전 오도메트리 - GT
        raw_th = ang(p["odom_theta"] - p["theta"])
        flags = ["rest" if p["at_rest"] else "moving"]
        if p["straight"]:
            flags.append("straight")
        if p["ghost"]:
            flags.append("GHOST")
        if p["collided"]:
            flags.append("COLLIDED")
        act = p["action"] or p["sub_state"] or "-"
        goal = p["goal"]
        lines = [
            f"{view.robot_id} physics   t={p['sim_time']:.1f}s   {p['state']}/{act}",
            f"  report  {ex:8.3f} {ey:8.3f}  th {eth:+.3f}   (State.pose, body)",
            f"  GT      {p['x']:8.3f} {p['y']:8.3f}  th {p['theta']:+.3f}   (truth, cyan)",
            f"  error   {drift * 100:.1f} cm  {dth:+.1f} deg   (report - GT)",
            f"  odom    {p['odom_x']:8.3f} {p['odom_y']:8.3f}  th {p['odom_theta']:+.3f}   (raw encoders, yellow)",
            f"  o-error {raw * 100:.1f} cm  {raw_th:+.1f} deg   (raw odom - GT)",
            (f"  loc     {p['loc_rate']:.0f} Hz  noise {p['loc_sigma_xy'] * 100:.1f} cm/{p['loc_sigma_theta']:.3f} rad  gain {p['loc_gain']:.2f}"
             f"  fixes {p['loc_corrections']} (drop {p['loc_dropouts']})  last {p['loc_step_xy'] * 100:.1f} cm"
             if has_est and p['loc_rate'] > 0 else "  loc     off (no localization correction)"),
            f"  v fwd   {p['v_fwd']:+7.3f} m/s   lat {p['v_lat']:+.3f}",
            f"  yaw     {p['yaw_rate']:+7.3f} rad/s",
            f"  accel   {view.accel:+7.3f} m/s2   (max {p['max_acceleration']:.2f})",
            f"  cmd     v {p['cmd_v']:+.3f}  w {p['cmd_w']:+.3f}   err v {p['cmd_v'] - p['v_fwd']:+.3f}",
            f"  wheel   L {p['wheel_w'][0]:+6.2f}  R {p['wheel_w'][1]:+6.2f} rad/s",
            f"  w cmd   L {p['wheel_cmd'][0]:+6.2f}  R {p['wheel_cmd'][1]:+6.2f}",
            f"  slip    L {sl:+.3f}  R {sr:+.3f}   peak {view.peak_slip:.3f}",
            f"  wall    {p['wall_force']:6.1f} N   peak {view.peak_force:.1f} N",
            f"  floor   mu {p['friction']:.2f}   mass {p['mass']:.0f} kg   wheel r {p['wheel_radius']:.3f} track {p['track']:.2f}",
            f"  limit   v {p['max_velocity']:.2f} m/s   torque {p['max_wheel_torque']:.0f} Nm",
            f"  path    {p['path_points']} wp   {p['path_remaining']:.2f} m left"
            + (f"   goal {goal[0]:.2f},{goal[1]:.2f}" if goal else ""),
            f"  flags   {' '.join(flags)}",
        ]
        return "\n".join(lines)

    def set_connection(self, robot_id: str, connection: str, now: float = 0.0) -> None:
        view = self.robots.get(robot_id)
        if view is None:
            view = RobotView(robot_id=robot_id, last_seen=now)
            self.robots[robot_id] = view
        view.connection = connection

    def remove_robot(self, robot_id: str) -> None:
        self.robots.pop(robot_id, None)
        self._events.pop(robot_id, None)
        item = self._robot_items.pop(robot_id, None)
        if item is not None:
            item.remove(self._scene)
        self.event_label.setText("\n".join(self._events[k] for k in sorted(self._events)))
        self._layout_hud()

    def append_event(self, robot_id: str, text: str) -> None:
        """등록된 로봇마다 가장 최근 이벤트 한 줄만 남긴다 — 지나간 이벤트나
        더 이상 안 보이는 로봇의 옛 기록이 패널에 계속 남지 않게 한다."""
        self._events[robot_id] = text
        lines = [self._events[k] for k in sorted(self._events)][-EVENT_LOG_MAX_LINES:]
        self.event_label.setText("\n".join(lines))
        self._layout_hud()

    def robot_color(self, robot_id: str) -> QColor:
        ordered = sorted(self.robots)
        idx = ordered.index(robot_id) if robot_id in ordered else 0
        return QColor(ROBOT_PALETTE[idx % len(ROBOT_PALETTE)])

    # -- 렌더링 갱신 ----------------------------------------------------------

    def refresh(self, now: float = 0.0) -> None:
        m = self.map.metadata
        live = sum(1 for v in self.robots.values() if v.has_pose)
        lines = [f"ROBOTS: {live} reporting / {len(self.robots)} known", ""]

        seen = set()
        for robot_id, view in sorted(self.robots.items()):
            if not view.has_pose:
                lines.append(f"- {robot_id}  (no state)")
                lines.append(f"    conn {view.connection}")
                lines.append("")
                continue
            if not self._on_this_map(view):
                where = self.uid_labels.get(view.map_uid, view.map_uid)
                lines.append(f"- {robot_id}  {view.state}")
                lines.append(f"    다른 맵: {where}")
                lines.append("")
                continue
            seen.add(robot_id)
            color = self.robot_color(robot_id)
            stale = view.is_stale(now)
            alpha = 0.3 if stale else 0.95
            label_text = robot_id.replace("AMR-", "")

            item = self._robot_items.get(robot_id)
            if item is None:
                item = _RobotItem(self._scene, color)
                self._robot_items[robot_id] = item
            view.dx, view.dy, view.dth = view.display_pose(now)
            item.update_pose(m, view, label_text, alpha)
            if robot_id == self._follow:
                self.centerOn(world_to_scene(m, view.dx, view.dy))   # 줌은 그대로, 중심만 로봇에 고정
            item.update_path(m, view, self._show_planned_path, 0.25 if stale else 0.75)
            item.update_physics_items(m, view, self._show_trail, self._show_gt, self._show_odom, 0.25 if stale else 0.9, now)
            item.update_obstacles(self._scene, m, view, 0.25 if stale else 0.85)

            mark = "x" if stale else "*"
            battery = f"{view.battery:5.1f}%" if view.battery is not None else "    ?"
            lines.append(f"{mark} {robot_id}  {view.state}")
            lines.append(f"    xy   {view.x:7.2f}, {view.y:6.2f}")
            lines.append(f"    th   {view.theta:7.3f} rad")
            v_shown = view.physics["v_fwd"] if view.physics_fresh(now) else view.velocity
            lines.append(f"    v    {v_shown:+7.2f} m/s")
            lines.append(f"    bat  {battery}")
            truth = view.truth_pose(now)
            if truth is not None:
                err = math.hypot(view.dx - truth[0], view.dy - truth[1])
                lines.append(f"    GT   {truth[0]:7.2f}, {truth[1]:6.2f}  err {err * 100:.1f} cm")
            if view.order_id:
                lines.append(f"    ord  {view.order_id} ({view.remaining_nodes} left)")
            if view.connection not in ("ONLINE", "?"):
                lines.append(f"    conn {view.connection}")
            if view.errors:
                lines.append(f"    ERR  {', '.join(view.errors)}")
            if stale:
                lines.append(f"    (no report for {now - view.last_seen:.0f}s)")
            lines.append("")

        for robot_id in [r for r in self._robot_items if r not in seen]:
            self._robot_items.pop(robot_id).remove(self._scene)

        self.info_label.setText("\n".join(lines) if self.robots else "no robots")
        blocks = [self._physics_text(v) for _, v in sorted(self.robots.items()) if v.physics_fresh(now)]
        self.physics_label.setText("\n\n".join(b for b in blocks if b))
        self._layout_hud()

    # -- 조작: 확대/이동/토글 --------------------------------------------------

    def wheelEvent(self, event) -> None:
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)

    def reset_view(self) -> None:
        self.fitInView(QRectF(0, 0, self.map.metadata.width_px, self.map.metadata.height_px),
                        Qt.KeepAspectRatio)

    def _cycle_follow(self) -> None:
        """시점 고정: 꺼짐 -> 로봇1 -> 로봇2 ... -> 꺼짐. 줌은 스크롤로 그대로 조절한다."""
        ids = sorted(r for r, v in self.robots.items() if v.has_pose)
        order = [None] + ids
        idx = order.index(self._follow) if self._follow in order else 0
        self._follow = order[(idx + 1) % len(order)]
        self._update_title()
        self._layout_hud()

    def mousePressEvent(self, event) -> None:
        if self._follow is not None:   # 드래그로 이동하려는 순간 고정을 푼다
            self._follow = None
            self._update_title()
            self._layout_hud()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        super().mouseMoveEvent(event)
        scene_pos = self.mapToScene(event.position().toPoint())
        x, y = scene_to_world(self.map.metadata, scene_pos.x(), scene_pos.y())
        if self.map.in_bounds(x, y):
            cell = self.map.cell_at(x, y)
            mark = "drivable" if cell.drivable else "blocked"
            self.coord_label.setText(f"x={x:7.2f} m  y={y:7.2f} m   {cell.value} ({mark})")
        else:
            self.coord_label.setText(f"x={x:7.2f} m  y={y:7.2f} m   [out of bounds]")
        self.coord_label.adjustSize()
        self._layout_hud()

    def keyPressEvent(self, event) -> None:
        key = event.text().lower()
        if key == "g":
            self._show_grid = not self._show_grid
            self._grid_item.setVisible(self._show_grid)
        elif key == "c":
            self._classified = not self._classified
            self._refresh_map_pixmap()
        elif key == "t":
            self._show_planned_path = not self._show_planned_path
        elif key == "p":
            self._show_physics_hud = not self._show_physics_hud
            self._layout_hud()
        elif key == "l":
            self._show_trail = not self._show_trail
        elif key == "o":
            self._show_gt = not self._show_gt
        elif key == "i":
            self._show_odom = not self._show_odom
        elif key == "k":
            for v in self.robots.values():
                v.trail.clear()
                v.gt_trail.clear()
                v.trail_dirty = True
                v.peak_slip = v.peak_force = 0.0
        elif key == "z":
            self._show_zones = not self._show_zones
            for item in self._zone_items:
                item.setVisible(self._show_zones)
        elif key == "f":
            self._cycle_follow()
        elif key == "r":
            self.reset_view()
        else:
            super().keyPressEvent(event)


class FleetViewWindow(QMainWindow):
    """맵 전체를 채우는 창. 정보는 전부 그 위 오버레이다."""

    def __init__(self, map_model: MapModel, title: str = "Fleet Viz", *, map_uid: Optional[str] = None,
                 map_label: Optional[str] = None):
        super().__init__()
        self.setWindowTitle(title)
        if ICON_PATH.exists():
            self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.canvas = MapCanvas(map_model, map_uid=map_uid, map_label=map_label)
        self.setCentralWidget(self.canvas)
        self.resize(1280, 800)

    # MapCanvas 로 위임 (fleet_monitor.py 가 부르는 인터페이스 그대로)
    def update_robot(self, *a, **kw) -> None:
        self.canvas.update_robot(*a, **kw)

    def set_connection(self, *a, **kw) -> None:
        self.canvas.set_connection(*a, **kw)

    def remove_robot(self, *a, **kw) -> None:
        self.canvas.remove_robot(*a, **kw)

    def append_event(self, robot_id: str, text: str) -> None:
        self.canvas.append_event(robot_id, text)

    def update_physics(self, *a, **kw) -> None:
        self.canvas.update_physics(*a, **kw)

    def refresh(self, now: float = 0.0) -> None:
        self.canvas.refresh(now)

    def set_map(self, map_model: MapModel, map_uid: Optional[str] = None, map_label: Optional[str] = None) -> None:
        self.canvas.set_map(map_model, map_uid, map_label)
        self.setWindowTitle(f"Fleet Viz — {self.canvas.map_label}")


def placeholder_model(name: str = "(로봇 맵 기다리는 중)") -> MapModel:
    """맵 파일 없이 뜰 때 쓰는 빈 회색 맵 (20 x 12 m). 로봇 맵을 찾으면 set_map 으로 바뀐다."""
    from common.map_model import MapMetadata

    meta = MapMetadata(name=name, image="", resolution=0.1, width_px=200, height_px=120)
    model = MapModel(meta)
    model._grid = np.full((meta.height_px, meta.width_px), 128, dtype=np.uint8)
    return model

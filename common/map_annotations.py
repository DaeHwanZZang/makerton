"""
맵 주석 (annotations) — location / zone 계약
===========================================

`common/schemas.py` 가 FMS<->로봇의 **메시지** 계약이라면, 이 파일은 두 쪽이
공유하는 **맵에 딸린 정적 현장 데이터** 계약이다.

    location : 이름 붙은 목표 지점 (station, charger, ...). Task 는 좌표가 아니라
               이름으로 목적지를 지정하고, FMS 가 여기서 Pose 로 바꿔 Order 에 싣는다.
    zone     : 다각형 구역 (prohibit, elevator, pickup ...). prohibit 는 FMS 가 서버에서 grid 에
               구워버리므로(navi_gridmap.baked.png) 로봇은 보통 안 읽는다. 하지만
               런타임 동적 zone / 시각화를 위해 계약으로 유지한다.

이건 **맵 하나에 귀속**된다 (`annotations.json`). 시설 단위의 "Site" 개념과
다르다 — Site 는 맵 여러 개 + 로봇을 묶는 상위 개념이고 `fms_server/sites.py` 에
있다. 이 파일은 맵 1개의 주석일 뿐이다.

동기화 대상
-----------
`common/` 이 원본. 바뀌면:
    include/map_model.hpp + src/map_model.cpp (C++ 점유 존 경계 읽기)
    viewer/common/map_annotations.py (파일 복사, fms_server/tests/test_common_sync.py 가 검사)

벤더 원본 (`location_meta.json`, `zone_meta.json`)은 이 스키마와 필드명이
다르다 (pose 가 배열, theta 가 0~2pi 등). 변환은 `fms_server` 의 맵 인제스트가
담당하고, 이 파일은 **정규화된 뒤의** 모양만 정의한다.

Pydantic v2 기준.
"""

from __future__ import annotations

import math
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from common.schemas import Point, Pose


def normalize_theta(theta: float) -> float:
    """임의 각도를 -pi ~ pi 로 접는다.

    벤더 location pose 의 theta 는 0~2pi 로 온다 (예: 4.71 = 270도). 그대로는
    `Pose.theta` 의 -3.15~3.15 검증에 걸린다. 인제스트가 이걸 거쳐 저장한다.
    """
    return math.atan2(math.sin(theta), math.cos(theta))


def rectangle_polygon(x: float, y: float, width: float, height: float,
                      rotation: float = 0) -> list[Point]:
    """회전 전 좌상단·크기로 정의한 사각형을 중심 기준으로 회전한다. 각도는 도 단위다."""
    if rotation % 360 == 0:
        return [Point(x=x,y=y),Point(x=x+width,y=y),Point(x=x+width,y=y+height),Point(x=x,y=y+height)]
    cx, cy = x + width / 2, y + height / 2
    angle = math.radians(rotation % 360)
    c, s = math.cos(angle), math.sin(angle)
    return [Point(x=cx+c*dx-s*dy, y=cy+s*dx+c*dy)
            for dx,dy in ((-width/2,-height/2),(width/2,-height/2),
                          (width/2,height/2),(-width/2,height/2))]


def rectangle_frame(polygon) -> tuple[float, float, float, float, float, float]:
    """직사각형의 중심·가로 단위축·반너비·반높이. 회전/꼭짓점 오차를 허용한다."""
    pts = list(polygon)
    if len(pts) == 5 and math.hypot(pts[0].x-pts[-1].x,pts[0].y-pts[-1].y) < 1e-8:
        pts.pop()
    if len(pts) != 4 or not all(math.isfinite(v) for p in pts for v in (p.x,p.y)):
        raise ValueError("직사각형의 꼭짓점 4개가 필요하다")
    a,b,c,d = pts
    ux,uy,vx,vy = b.x-a.x,b.y-a.y,d.x-a.x,d.y-a.y
    width,height = math.hypot(ux,uy),math.hypot(vx,vy)
    if (min(width,height) <= 1e-9 or abs(ux*vx+uy*vy) > 1e-6*width*height
            or math.hypot(c.x-(b.x+d.x-a.x),c.y-(b.y+d.y-a.y)) > 1e-6*max(width,height)):
        raise ValueError("인접한 변이 직각인 직사각형이어야 한다")
    return ((a.x+c.x)/2,(a.y+c.y)/2,ux/width,uy/width,width/2,height/2)


def rectangles_overlap(first, second) -> bool:
    """분리축 검사. 회전 사각형의 경계 접촉도 겹침으로 취급한다."""
    ax,ay,au,av,aw,ah = rectangle_frame(first)
    bx,by,bu,bv,bw,bh = rectangle_frame(second)
    for x,y in ((au,av),(-av,au),(bu,bv),(-bv,bu)):
        ra = aw*abs(au*x+av*y)+ah*abs(-av*x+au*y)
        rb = bw*abs(bu*x+bv*y)+bh*abs(-bv*x+bu*y)
        if abs((bx-ax)*x+(by-ay)*y) > ra+rb+1e-9:
            return False
    return True


# =============================================================================
# Location
# =============================================================================

class LocationType(str, Enum):
    """
    location 의 용도. 벤더 맵은 자유 문자열을 쓰므로 (`station` 등) 모르는 값은
    OTHER 로 떨어뜨린다 — 스키마 검증이 벤더 맵을 거부하면 안 된다.
    """

    STATION = "station"       # 픽업/드롭 등 일반 작업 지점
    CHARGER = "charger"       # 충전 스테이션
    PARKING = "parking"       # 대기/주차 지점
    WAYPOINT = "waypoint"     # 단순 경유점 (작업 없음)
    PICKUP = "pickup"         # 픽업스테이션 node. site location 의 zone_id 로 pickup zone 과 1:1 연결
    TRAY_RETURN = "tray_return"  # 픽업스테이션 반납 node. 배송 후 빈 트레이를 내려놓는다 (zone_id 로 연결)
    OTHER = "other"

    @classmethod
    def _missing_(cls, value: object) -> "LocationType":
        return cls.OTHER


class Location(BaseModel):
    """이름 붙은 목표 지점 하나 (정규화 완료)."""

    model_config = ConfigDict(extra="forbid")

    location_id: str = Field(min_length=1, description="맵 내 고유 ID (벤더 unique_id)")
    name: str = Field(min_length=1, description="사람이 쓰는 이름. Task 목적지 지정에 쓴다")
    type: LocationType = LocationType.OTHER
    pose: Pose = Field(description="목표 자세. theta 는 -pi~pi 로 정규화되어 있어야 한다")
    marker_id: Optional[int] = Field(default=None, description="벤더 마커 번호 (있으면)")


# =============================================================================
# Zone
# =============================================================================

class ZoneType(str, Enum):
    """
    zone 의 종류. 역시 모르는 값은 PROHIBIT 로 떨어뜨린다 — 안전한 쪽
    (막힌 것으로 취급) 이 기본값이다.
    """

    PROHIBIT = "prohibit"     # 진입 금지. FMS 가 grid 에 OCCUPIED 로 굽는다
    ELEVATOR = "elevator"     # 엘리베이터 구역. 위치 표시용, 호출/탑승 제어는 별도
    PICKUP = "pickup"         # 픽업스테이션 구역. 적재 장치 위치 표시용. 굽지 않고 동작도 없다
    OCCUPANCY = "occupancy"   # 한 대씩 통과하는 점유 존. 정적 이미지에 굽지 않는다
    SLOW = "slow"             # 감속 구역 (미구현, 예약)
    ONEWAY = "oneway"         # 일방통행 (미구현, 예약)

    @classmethod
    def _missing_(cls, value: object) -> "ZoneType":
        return cls.PROHIBIT


class TrafficGate(BaseModel):
    """진입 방향별 대기·출구 node. 좌표는 존 버전에 복사해 보존한다."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    gate_id: str = Field(min_length=1)
    wait: Pose
    exit: Pose

    @model_validator(mode="after")
    def finite_coordinates(self):
        if not all(math.isfinite(v) for p in (self.wait, self.exit) for v in (p.x,p.y,p.theta)):
            raise ValueError("대기·출구 node는 유한한 좌표여야 한다")
        return self


class Zone(BaseModel):
    """다각형 구역 하나 (정규화 완료)."""

    model_config = ConfigDict(extra="forbid")

    zone_id: str = Field(min_length=1)
    name: str = ""
    type: ZoneType = ZoneType.PROHIBIT
    polygon: list[Point] = Field(min_length=3, description="꼭짓점 목록 (닫힘은 암묵적)")
    gates: list[TrafficGate] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_polygon(self) -> "Zone":
        if self.type == ZoneType.OCCUPANCY:
            rectangle_frame(self.polygon)
            if not self.gates or len({g.gate_id for g in self.gates}) != len(self.gates):
                raise ValueError("점유 존에는 고유 ID의 대기·출구 node 쌍이 필요하다")
        elif self.gates:
            raise ValueError("대기·출구 node는 점유 존에만 지정할 수 있다")
        # 첫 점 == 끝 점으로 명시적으로 닫아 보낸 경우는 허용하되 최소 3개는 유지.
        if len(self.polygon) >= 2 and self.polygon[0] == self.polygon[-1] and len(self.polygon) < 4:
            raise ValueError("닫힌 폴리곤이면 꼭짓점이 4개 이상이어야 한다 (첫 점 반복 포함)")
        return self

    def contains(self, x: float, y: float) -> bool:
        """점이 폴리곤 내부인가 (ray casting). 경계는 구현 정의."""
        pts = self.polygon
        n = len(pts)
        inside = False
        j = n - 1
        for i in range(n):
            xi, yi = pts[i].x, pts[i].y
            xj, yj = pts[j].x, pts[j].y
            if (yi > y) != (yj > y):
                x_cross = (xj - xi) * (y - yi) / (yj - yi) + xi
                if x < x_cross:
                    inside = not inside
            j = i
        return inside


# =============================================================================
# MapAnnotations — 맵 하나에 딸린 location/zone 전체
# =============================================================================

class MapAnnotations(BaseModel):
    """맵 하나의 정적 현장 데이터. 맵 버전 디렉터리 안에 `annotations.json` 으로 저장한다."""

    model_config = ConfigDict(extra="forbid")

    map_id: str = Field(
        min_length=1,
        description="FMS 전역 고유 맵 id (map_uid). fms_map.json 의 name 과 같은 값이다",
    )
    map_version: str = Field(min_length=1, description="어느 맵 버전 기준으로 만든 좌표인지")
    locations: list[Location] = Field(default_factory=list)
    zones: list[Zone] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_unique_ids(self) -> "MapAnnotations":
        loc_ids = [loc.location_id for loc in self.locations]
        if len(set(loc_ids)) != len(loc_ids):
            raise ValueError("location_id 가 중복되었다")
        loc_names = [loc.name for loc in self.locations]
        if len(set(loc_names)) != len(loc_names):
            raise ValueError("location name 이 중복되었다 (Task 목적지 지정이 모호해진다)")
        zone_ids = [z.zone_id for z in self.zones]
        if len(set(zone_ids)) != len(zone_ids):
            raise ValueError("zone_id 가 중복되었다")
        return self

    def location_by_name(self, name: str) -> Optional[Location]:
        for loc in self.locations:
            if loc.name == name:
                return loc
        return None

    def location_by_id(self, location_id: str) -> Optional[Location]:
        for loc in self.locations:
            if loc.location_id == location_id:
                return loc
        return None

    def zone_by_id(self, zone_id: str) -> Optional[Zone]:
        for z in self.zones:
            if z.zone_id == zone_id:
                return z
        return None

    def prohibit_zones(self) -> list[Zone]:
        return [z for z in self.zones if z.type == ZoneType.PROHIBIT]

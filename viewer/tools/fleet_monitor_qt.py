"""
플릿 모니터 (Qt 뷰어 버전)
=========================

`tools/fleet_monitor.py` 와 MQTT 구독 로직은 완전히 동일하다 (그 파일의
`FleetMonitor` 클래스를 그대로 재사용한다 — 렌더러에 의존하지 않게 짜여
있었기 때문에 뷰어만 matplotlib -> Qt 로 갈아끼우면 됐다).

    python tools/fleet_monitor_qt.py                       # 로봇을 따라간다: 첫 로봇이 떠 있는 맵을 보여 준다
    python tools/fleet_monitor_qt.py --follow AMR-002      # 그 로봇의 맵 (엘리베이터로 층을 바꾸면 따라 바뀐다)
    python tools/fleet_monitor_qt.py maps/warehouse/warehouse.json   # 정적 맵 (따라가지 않음)

따라가기: 로봇이 보고한 State.map_id/map_version 으로 맵을 찾는다 (`tools/map_follow.py`: 로컬 map_cache ->
FMS 번들). 따라가는 로봇의 맵이 바뀌면 화면 맵을 바꾸고, 다른 맵(층)에 있는 로봇은 그리지 않고 패널에만 적는다.
`--map` 로컬 맵으로 띄운 로봇은 map_id 를 보고하지 않아 따라갈 수 없다 (정적 맵 인자를 줘야 한다).

조작
    드래그        화면 이동 (Qt 기본 ScrollHandDrag)
    스크롤        확대/축소 (커서 위치 기준, Qt 기본 AnchorUnderMouse)
    g             격자 토글
    c             색상 모드 토글 (분류색 <-> 원본 그레이스케일)
    t             계획 경로 토글
    p             물리 디버그 패널 토글 (속도/바퀴/슬립/오도메트리/접촉력 ...)
    l             지나온 궤적 토글
    o             GT(참 위치, 청록 윤곽/점선 궤적) 토글 -- 몸체(로봇이 보고한 추정 위치)와 벌어진 만큼이 오차
    i             보정 전 순수 오도메트리(노란 점선 윤곽) 토글
    k             궤적/피크값 지우기
    f             시점 고정 (로봇1 -> 로봇2 ... -> 해제 순환). 드래그하면 해제, 스크롤 줌은 유지
    r             보기 초기화 (맵 전체)
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtGui import QIcon  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from common.map_model import MapModel  # noqa: E402
from tools.fleet_monitor import FleetMonitor  # noqa: E402
from tools.map_follow import MapResolver  # noqa: E402
from tools.qt_viewer import ICON_PATH, FleetViewWindow, placeholder_model  # noqa: E402

log = logging.getLogger("monitor-qt")

REFRESH_PERIOD_MS = 33   # 30 fps. 로봇 자세는 물리 토픽(20 Hz)을 속도로 외삽해서 그린다
APP_NAME = "Fleet Viewer"


def _set_macos_menu_bar_name(name: str) -> None:
    """macOS 메뉴바(애플 로고 옆)에 뜨는 앱 이름을 바꾼다. pyobjc 없으면 조용히 넘어감."""
    if sys.platform != "darwin":
        return
    try:
        from Foundation import NSBundle
        info = NSBundle.mainBundle().localizedInfoDictionary() or NSBundle.mainBundle().infoDictionary()
        if info is not None:
            info["CFBundleName"] = name
    except Exception:
        pass


class MapFollower:
    """따라갈 로봇이 보고한 맵으로 창의 맵을 맞춘다. 메인 스레드(타이머)에서만 부른다."""

    RETRY_AFTER = 10.0  # 못 찾은 맵을 다시 찾아보는 간격 (s)

    def __init__(self, window: FleetViewWindow, monitor: FleetMonitor, resolver: MapResolver,
                 target: str | None, *, has_fallback: bool = False) -> None:
        self.window, self.monitor, self.resolver = window, monitor, resolver
        self.has_fallback = has_fallback    # 맵 id 를 보고하지 않는 로봇(로컬 --map)용 맵을 이미 띄워 뒀다
        self.fixed_target = target          # None = 자동 (맵을 보고하는 첫 로봇)
        self.target: str | None = None
        self.shown: tuple[str, str | None] | None = None
        self.wanted: tuple[str, str | None] | None = None
        self.failed: dict[tuple[str, str | None], float] = {}

    def _pick_target(self) -> str | None:
        if self.fixed_target:
            return self.fixed_target
        maps = self.monitor.robot_maps
        if self.target in maps:
            return self.target
        return next((r for r in sorted(maps) if maps[r][0]), None)

    def tick(self, now: float) -> None:
        canvas = self.window.canvas
        for r in self.resolver.poll():
            key = (r.uid, r.version)
            if r.model is None:
                self.failed[key] = now
                canvas.set_status(f"[맵 못 찾음: {r.error}]")
                self.window.append_event(self.target or "-", f"map {r.uid[:8]} 못 찾음: {r.error}")
                log.warning("맵 %s@%s 못 찾음: %s", r.uid, r.version, r.error)
            elif key == self.wanted:
                self.window.set_map(r.model, r.uid, r.label)
                self.shown = key
                canvas.set_follow(self.target)  # 층이 바뀌면 시점도 다시 로봇에 붙인다
                canvas.set_status("")
                self.window.append_event(self.target or "-", f"{self.target} | map {r.label}")
                log.info("맵 표시: %s (%s@%s)", r.label, r.uid, r.version)
        canvas.uid_labels.update(self.resolver.labels)

        target = self._pick_target()
        if target != self.target:
            self.target = target
            canvas.set_follow(target)
        if target is None:
            canvas.set_status("[로봇 기다리는 중]")
            return
        uid, version = self.monitor.robot_maps.get(target, (None, None))
        if uid is None:
            if self.shown is None:
                canvas.set_status("" if self.has_fallback else
                                  f"[{target}: 맵 보고 없음 — 로컬 --map 로봇이면 --fallback-map 으로 맵을 준다]")
            return
        key = (uid, version)
        if key == self.shown or key == self.wanted and key not in self.failed:
            return
        if key in self.failed and now - self.failed[key] < self.RETRY_AFTER:
            return
        self.failed.pop(key, None)
        self.wanted = key
        canvas.set_status(f"[{target} 맵 불러오는 중...]")
        self.resolver.request(uid, version)


def main() -> int:
    parser = argparse.ArgumentParser(description="MQTT 로 로봇들을 관측해 맵 위에 표시 (Qt 뷰어)")
    parser.add_argument("metadata", nargs="?", help="맵 메타데이터 JSON 경로 (주면 정적 맵. 없으면 로봇을 따라간다)")
    parser.add_argument("--follow", metavar="ROBOT_ID",
                        help="이 로봇이 떠 있는 맵을 보여 주고 층이 바뀌면 따라 바꾼다 (기본: 맵을 보고하는 첫 로봇)")
    parser.add_argument("--fms", default="http://localhost:8000",
                        help="맵 번들을 받을 FMS 주소 (로컬 map_cache 에 없을 때). 빈 문자열이면 안 받는다")
    parser.add_argument("--fallback-map", metavar="JSON",
                        help="따라가기 모드에서 처음 보여 줄 맵. map_id 를 보고하지 않는 로봇(로컬 --map)은 이 맵에 그린다 "
                             "(런처가 고른 맵을 넘긴다)")
    parser.add_argument("--map-cache", action="append", default=[], metavar="DIR",
                        help="로봇 SET_MAP 캐시 폴더 추가 (기본: 리포 루트 map_cache)")
    parser.add_argument("--host", default="localhost", help="MQTT 브로커 호스트")
    parser.add_argument("--port", type=int, default=1883, help="MQTT 브로커 포트")
    parser.add_argument("--no-orders", action="store_true", help="order 토픽은 구독하지 않는다")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    _set_macos_menu_bar_name(APP_NAME)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    if ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(ICON_PATH)))

    follow = bool(args.follow) or args.metadata is None
    first_map = args.metadata or args.fallback_map
    map_model = MapModel.load(first_map) if first_map else placeholder_model()
    window = FleetViewWindow(map_model, title=f"Fleet Viz — {map_model.metadata.name}")
    window.show()

    monitor = FleetMonitor(
        window, args.host, args.port,
        watch_orders=not args.no_orders, on_event=window.append_event,
    )
    try:
        monitor.start()
    except OSError as e:
        log.error("브로커 접속 실패 (%s:%d): %s — mosquitto 가 떠 있는지 확인",
                  args.host, args.port, e)
        return 1

    follower = None
    if follow:
        resolver = MapResolver([Path(p) for p in args.map_cache], args.fms or None)
        follower = MapFollower(window, monitor, resolver, args.follow, has_fallback=bool(args.fallback_map))

    def on_timer() -> None:
        now = time.time()
        monitor.pump()
        if follower is not None:
            follower.tick(now)
        window.refresh(now)

    timer = QTimer()
    timer.setInterval(REFRESH_PERIOD_MS)
    timer.timeout.connect(on_timer)
    timer.start()

    print(f"브로커 {args.host}:{args.port} 관측 중. 로봇 프로세스를 띄우면 여기 나타난다.\n")
    try:
        return app.exec()
    finally:
        timer.stop()
        monitor.stop()


if __name__ == "__main__":
    sys.exit(main())

"""
로봇이 떠 있는 맵 찾기 (뷰어용)
==============================

로봇은 `State.map_id`(= FMS map_uid) / `map_version` 으로 지금 쓰는 맵을 보고한다. 뷰어는 그 값으로 맵 파일을 찾는다.

찾는 순서 (먼저 찾은 것):
1. 로컬 맵 캐시 — 같은 PC 의 로봇이 SET_MAP 으로 받아 푼 `map_cache/<uid>/` (리포 루트 `map_cache` + `--map-cache`).
   `manifest.json` 의 version 이 보고한 버전과 같아야 쓴다.
2. FMS — `GET /api/v1/sites` -> 각 site 의 `/maps` 에서 map_uid 를 찾아 `/bundle?version=` zip 을 받아
   `viewer/.map_cache/<uid>/<version>/` 에 푼다 (읽기만 한다. 로봇에게는 아무것도 보내지 않는다).

`--map` 으로 로컬 맵을 연 로봇은 map_id 를 보고하지 않는다 (None) — 그런 로봇은 따라갈 맵을 알 수 없다.

조회는 느릴 수 있어(번들 수 MB) 워커 스레드에서 하고, 결과는 `poll()` 로 메인 스레드가 꺼내 간다.
"""

from __future__ import annotations

import io
import json
import logging
import queue
import threading
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from common.map_model import MapModel

log = logging.getLogger("map-follow")

VIEWER_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CACHES = [VIEWER_ROOT.parent / "map_cache"]   # 리포 안에서 돌 때 로봇 캐시 (없으면 무시)
DOWNLOAD_CACHE = VIEWER_ROOT / ".map_cache"


@dataclass
class ResolvedMap:
    uid: str
    version: Optional[str]
    model: Optional[MapModel]
    label: str            # 화면 표시용 이름 ("gachon_ai / f1")
    error: Optional[str] = None


def _label_from_manifest(path: Path, uid: str) -> str:
    try:
        m = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        if m.get("site_id") and m.get("map_id"):
            return f"{m['site_id']} / {m['map_id']}"
    except (OSError, ValueError):
        pass
    return uid


def _manifest_version(path: Path) -> Optional[str]:
    try:
        return json.loads((path / "manifest.json").read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError):
        return None


class MapResolver:
    def __init__(self, caches: Optional[list[Path]] = None, fms_url: Optional[str] = "http://localhost:8000",
                 timeout: float = 10.0) -> None:
        self.caches = [Path(c) for c in (caches or [])] + DEFAULT_CACHES
        self.fms_url = fms_url.rstrip("/") if fms_url else None
        self.timeout = timeout
        self._results: "queue.Queue[ResolvedMap]" = queue.Queue()
        self._pending: set[tuple[str, Optional[str]]] = set()
        self._lock = threading.Lock()
        self.labels: dict[str, str] = {}   # uid -> 표시 이름 (찾은 적 있는 맵)

    # -- 메인 스레드 --
    def request(self, uid: str, version: Optional[str]) -> None:
        """비동기 조회 시작. 같은 (uid, version) 이 진행 중이면 무시."""
        key = (uid, version)
        with self._lock:
            if key in self._pending:
                return
            self._pending.add(key)
        threading.Thread(target=self._work, args=key, daemon=True, name=f"map-{uid[:8]}").start()

    def poll(self) -> list[ResolvedMap]:
        out = []
        while True:
            try:
                r = self._results.get_nowait()
            except queue.Empty:
                return out
            with self._lock:
                self._pending.discard((r.uid, r.version))
            if r.model is not None:
                self.labels[r.uid] = r.label
            out.append(r)

    # -- 워커 --
    def _work(self, uid: str, version: Optional[str]) -> None:
        try:
            self._results.put(self.resolve(uid, version))
        except Exception as exc:  # noqa: BLE001 — 뷰어는 죽지 않는다
            log.exception("맵 조회 실패 %s", uid)
            self._results.put(ResolvedMap(uid, version, None, uid, f"{type(exc).__name__}: {exc}"))

    def resolve(self, uid: str, version: Optional[str]) -> ResolvedMap:
        """동기 조회 (테스트/워커용)."""
        candidates = [root / uid for root in self.caches]          # 로봇 캐시: <root>/<uid>/ (최신 버전 하나)
        if version:
            candidates.append(DOWNLOAD_CACHE / uid / version)        # 내려받은 캐시: <uid>/<version>/
        for d in candidates:
            meta = d / "fms_map.json"
            if not meta.is_file():
                continue
            if version and _manifest_version(d) not in (version, None):
                continue  # 같은 맵의 다른 버전 (로봇 캐시가 이미 새 버전으로 덮였거나 아직 옛 버전)
            log.info("맵 %s@%s: 로컬 %s", uid, version, d)
            return ResolvedMap(uid, version, MapModel.load(meta), _label_from_manifest(d, uid))
        if not self.fms_url:
            return ResolvedMap(uid, version, None, uid, "로컬 캐시에 없고 FMS 조회가 꺼져 있다")
        site_map = self._find_on_fms(uid)
        if site_map is None:
            return ResolvedMap(uid, version, None, uid, f"FMS({self.fms_url})에 map_uid {uid} 가 없다")
        site_id, map_id, active = site_map
        ver = version or active
        url = f"{self.fms_url}/api/v1/sites/{site_id}/maps/{map_id}/bundle" + (f"?version={ver}" if ver else "")
        with urllib.request.urlopen(url, timeout=self.timeout) as r:
            data = r.read()
        target = DOWNLOAD_CACHE / uid / (ver or "active")
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for name in z.namelist():
                if "/" in name or "\\" in name or name.startswith("."):
                    continue  # 번들은 평평한 파일 목록이다. 경로가 섞이면 버린다
                (target / name).write_bytes(z.read(name))
        log.info("맵 %s@%s: FMS 에서 받음 (%s / %s, %d KB)", uid, ver, site_id, map_id, len(data) // 1024)
        return ResolvedMap(uid, version, MapModel.load(target / "fms_map.json"), f"{site_id} / {map_id}")

    def _get_json(self, path: str):
        with urllib.request.urlopen(f"{self.fms_url}{path}", timeout=self.timeout) as r:
            return json.load(r)

    def _find_on_fms(self, uid: str) -> Optional[tuple[str, str, Optional[str]]]:
        try:
            for site in self._get_json("/api/v1/sites"):
                for m in self._get_json(f"/api/v1/sites/{site['site_id']}/maps"):
                    if m.get("map_uid") == uid:
                        return site["site_id"], m["map_id"], m.get("active_version")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.warning("FMS 조회 실패 (%s): %s", self.fms_url, exc)
        return None

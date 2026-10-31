"""
런처 핵심 로직 (GUI 없음)
========================

launcher.py 가 쓰는 툴 등록표 로딩, 명령줄 조립, 프로세스 시작/정지/로그 읽기, 실행 상태 파일을 모았다.
Qt 를 몰라서 단독으로 테스트할 수 있다.

프로세스는 subprocess.Popen(start_new_session=True) 로 띄우고 출력은 logs/<이름>.log 파일로 보낸다.
화면 로그는 그 파일을 끝에서부터 읽어 만든다. 그래서 런처가 죽어도 자식이 같이 죽지 않고,
다음 실행 때 .run/launcher.json 으로 다시 찾아 붙을 수 있다.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
REGISTRY = Path(__file__).resolve().parent / "launcher_tools.json"
LOG_DIR = REPO / "logs"
RUN_FILE = REPO / ".run" / "launcher.json"
MAX_LINES = 2000
ADOPT_TAIL_BYTES = 32768
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
MODES = ("single", "per_map", "multi", "oneshot")


# -- 등록표 -------------------------------------------------------------------

def load_registry(path: Path = REGISTRY) -> list[dict]:
    """등록표를 읽고 검사한다. 문제면 ValueError(사용자용 메시지)."""
    try:
        tools = json.loads(path.read_text(encoding="utf-8"))["tools"]
    except (OSError, ValueError, KeyError) as ex:
        raise ValueError(f"툴 등록표를 읽을 수 없다 ({path}): {ex}")
    seen: set[str] = set()
    for t in tools:
        for k in ("id", "name", "mode", "cmd"):
            if k not in t:
                raise ValueError(f"등록표 항목에 {k!r} 가 없다: {t.get('id', t)}")
        if t["id"] in seen:
            raise ValueError(f"등록표 id 중복: {t['id']}")
        seen.add(t["id"])
        if t["mode"] not in MODES:
            raise ValueError(f"{t['id']}: 알 수 없는 mode {t['mode']!r} (지원: {', '.join(MODES)})")
        if t["mode"] == "multi" and "multi" not in t:
            raise ValueError(f"{t['id']}: multi 모드는 'multi' 설정(id_param/id_fmt)이 필요하다")
        for p in t.get("params", []):
            if p.get("type") not in ("str", "int", "float", "bool", "choice") or "key" not in p:
                raise ValueError(f"{t['id']}: 잘못된 param {p}")
        env = t.get("env", {})
        if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise ValueError(f"{t['id']}: env 는 {{이름: 문자열}} 이어야 한다")
    return tools


def default_values(spec: dict) -> dict[str, Any]:
    return {p["key"]: p.get("default", "" if p["type"] in ("str", "choice") else 0) for p in spec.get("params", [])}


def build_argv(spec: dict, values: dict[str, Any]) -> list[str]:
    """등록표 cmd 를 값으로 치환해 argv 를 만든다. 없는 키를 쓰면 KeyError."""
    def fmt(s: str) -> str:
        return s.format_map(values)

    out: list[str] = []
    for item in spec["cmd"]:
        if isinstance(item, str):
            out.append(fmt(item))
        elif values.get(item["if"]):
            out += [fmt(a) for a in item["args"]]
    return out


def build_env(spec: dict, values: dict[str, Any]) -> dict[str, str]:
    """등록표 env 를 값으로 치환한다 (설정을 환경변수로만 받는 툴용, 예: fms_server). 없는 키면 KeyError."""
    return {k: v.format_map(values) for k, v in spec.get("env", {}).items()}


def next_id(spec: dict, taken: set[str], start: int = 1) -> tuple[str, int]:
    """multi 툴의 자동 이름 중 안 쓰인 첫 번째와 그 번호."""
    n = start
    while spec["multi"]["id_fmt"].format(n=n) in taken:
        n += 1
    return spec["multi"]["id_fmt"].format(n=n), n


# -- 맵 목록 ------------------------------------------------------------------

def find_meta(map_dir: Path) -> Path | None:
    """맵 폴더에서 image/resolution/width_px 를 모두 가진 json 을 메타로 본다 (launch.sh 와 같은 기준)."""
    for f in sorted(map_dir.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and {"image", "resolution", "width_px"} <= d.keys():
            return f
    return None


def scan_maps(repo: Path = REPO) -> list[tuple[str, str]]:
    """[(폴더 이름, 메타 절대경로)]. 메타를 못 찾은 폴더는 뺀다."""
    out = []
    for d in sorted((repo / "maps").glob("*")):
        if d.is_dir() and (m := find_meta(d)):
            out.append((d.name, str(m)))
    return out


# -- 프로세스 -----------------------------------------------------------------

def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def pid_command(pid: int) -> str:
    r = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
    return r.stdout.strip()


def safe_name(s: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", s).strip("_") or "proc"


class Inst:
    """관리 대상 프로세스 1개 (정지 상태의 목록 항목 포함)."""

    def __init__(self, tool: str, name: str, values: dict[str, Any] | None = None, map_path: str = "",
                 logname: str | None = None) -> None:
        self.tool = tool
        self.name = name
        self.values = values or {}
        self.map_path = map_path            # per_map 툴이 고정해 둔 맵
        self.logname = logname or safe_name(name)
        self.argv: list[str] = []
        self.cwd = REPO
        self.pid: int | None = None
        self.popen: subprocess.Popen | None = None
        self.started = 0.0
        self.exit_code: int | None = None
        self.status = "정지"
        self.bad = False                    # 예상 못 한 종료 (빨간색)
        self.user_stop = False
        self.stop_deadline: float | None = None
        self.stop_timeout = 5.0
        self.force_asked = False
        self.restart_pending = False
        self.adopted = False
        self.lines: deque[str] = deque(maxlen=MAX_LINES)
        self._log_off = 0
        self._partial = b""

    @property
    def running(self) -> bool:
        return self.pid is not None

    @property
    def log_path(self) -> Path:
        return LOG_DIR / f"{self.logname}.log"

    def launch(self, argv: list[str], cwd: Path, env_extra: dict[str, str] | None = None) -> None:
        LOG_DIR.mkdir(exist_ok=True)
        self.argv, self.cwd = argv, cwd
        with open(self.log_path, "ab") as lf:
            self._log_off = lf.tell()
            lf.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} $ {' '.join(argv)}\n".encode())
            lf.flush()
            env = dict(os.environ, PYTHONUNBUFFERED="1", **(env_extra or {}))
            self.popen = subprocess.Popen(argv, cwd=cwd, stdout=lf, stderr=subprocess.STDOUT,
                                          stdin=subprocess.DEVNULL, start_new_session=True, env=env)
        self.pid = self.popen.pid
        self.started = time.time()
        self.exit_code = None
        self.status = "실행 중"
        self.bad = False
        self.user_stop = False
        self.stop_deadline = None
        self.force_asked = False
        self._partial = b""
        self.lines.clear()

    def adopt(self, pid: int, argv: list[str], cwd: Path, started: float) -> None:
        """이전 런처 세션이 띄운 프로세스에 다시 붙는다 (로그는 파일 끝 일부만 보인다)."""
        self.pid, self.argv, self.cwd, self.started = pid, argv, cwd, started
        self.popen, self.adopted, self.status, self.bad = None, True, "실행 중", False
        size = self.log_path.stat().st_size if self.log_path.exists() else 0
        self._log_off = max(0, size - ADOPT_TAIL_BYTES)
        self._partial = b""
        self.lines.clear()
        if self._log_off:
            self.read_log()
            if self.lines:
                self.lines.popleft()  # 잘린 첫 줄

    def read_log(self) -> list[str]:
        """로그 파일에서 새로 쓰인 완성된 줄들을 읽어 lines 에 쌓고 돌려준다."""
        try:
            with open(self.log_path, "rb") as f:
                f.seek(self._log_off)
                data = f.read()
        except OSError:
            return []
        self._log_off += len(data)
        parts = (self._partial + data).split(b"\n")
        self._partial = parts.pop()
        new = [_ANSI.sub("", p.decode(errors="replace").rstrip("\r")) for p in parts]
        self.lines.extend(new)
        return new

    def poll(self) -> bool:
        """종료를 감지하면 상태를 정리하고 True. 호출 쪽이 재시작 같은 후처리를 한다."""
        if self.pid is None:
            return False
        if self.popen is not None:
            rc = self.popen.poll()
            alive = rc is None
        else:
            rc, alive = None, pid_alive(self.pid)
        if alive:
            return False
        self.read_log()
        if self._partial:
            self.lines.append(self._partial.decode(errors="replace"))
            self._partial = b""
        self.pid, self.popen, self.exit_code = None, None, rc
        self.stop_deadline = None
        if self.user_stop:
            self.status, self.bad = "정지", False
        elif rc in (0, None):
            self.status, self.bad = ("종료" if rc is None else "종료(0)"), False
        else:
            self.status, self.bad = f"오류 종료({rc})", True
        self.user_stop = False
        return True

    def terminate(self, timeout_s: float | None = None) -> None:
        """SIGTERM 을 보내고 정지 마감 시각을 건다. 자식 정리는 프로그램 자신의 종료 처리에 맡긴다."""
        if self.pid is None:
            return
        self.user_stop = True
        self.force_asked = False
        self.stop_deadline = time.time() + (timeout_s if timeout_s is not None else self.stop_timeout)
        self.status = "정지 중…"
        try:
            os.kill(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def kill(self) -> None:
        """프로세스 그룹 전체를 SIGKILL (start_new_session 이라 pgid == pid)."""
        if self.pid is None:
            return
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def cancel_stop(self) -> None:
        """정지 요청이 거부됐다 (예: 편집기에서 '저장 안 됨' 확인을 취소)."""
        self.user_stop = False
        self.stop_deadline = None
        self.status = "실행 중 (정지 거부됨)"

    def elapsed(self) -> str:
        if self.pid is None:
            return ""
        s = int(time.time() - self.started)
        return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


# -- 실행 상태 파일 -------------------------------------------------------------

def save_state(insts: list[Inst], path: Path = RUN_FILE) -> None:
    """실행 중인 프로세스만 기록한다 (고아 복구용). 원자적으로 쓴다."""
    rows = [{"name": i.name, "tool": i.tool, "pid": i.pid, "argv": i.argv, "cwd": str(i.cwd),
             "started": i.started, "values": i.values, "map_path": i.map_path, "logname": i.logname}
            for i in insts if i.pid is not None]
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_orphans(path: Path = RUN_FILE) -> list[dict]:
    """상태 파일에서 아직 살아 있고 명령까지 일치하는 항목만 돌려준다 (PID 재사용 방지)."""
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for r in rows:
        try:
            pid, argv = int(r["pid"]), r["argv"]
        except (KeyError, TypeError, ValueError):
            continue
        if not argv or not pid_alive(pid):
            continue
        # 인터프리터로 띄운 툴은 argv[0] 이 'python' 뿐이라 스크립트 이름으로 대조한다
        key = argv[1] if Path(argv[0]).name.startswith("python") and len(argv) > 1 else argv[0]
        if Path(key).name in pid_command(pid):
            out.append(r)
    return out

#!/usr/bin/env python3
"""
통합 런처 (PySide6)
===================

브로커, 로봇 클라이언트, 뷰어, 맵 편집기, 맵 생성기 같은 하위 프로세스를 한 창에서 켜고 끈다.
각 프로그램은 별도 프로세스로 뜨고 자기 창은 따로 뜬다 (런처 안에 넣지 않는다).
어떤 툴을 관리할지는 tools/launcher_tools.json 에 적는다 -- 새 툴은 항목만 추가하면 된다.

    .venv/bin/python tools/launcher.py      # 리포 루트에서 (PySide6 필요. 뷰어는 pydantic, paho-mqtt 도 필요)

- 출력은 logs/<이름>.log 에 남고 화면에는 마지막 2000줄만 보인다.
- 정지는 SIGTERM 이다. 제한 시간 안에 안 끝나면 강제 종료할지 묻는다.
- 실행 중인 프로세스는 .run/launcher.json 에 기록한다. 런처가 비정상 종료돼도 다음 실행 때 다시 찾아 붙는다.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QSettings, Qt, QTimer, QUrl  # noqa: E402
from PySide6.QtGui import QColor, QDesktopServices, QFont  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout,
    QWidget,
)

import launcher_core as C  # noqa: E402
from launcher_core import Inst, REPO  # noqa: E402

# mosquitto 가 brew 의 sbin 에 있어 GUI 에서 띄운 환경 PATH 에 없을 수 있다
EXTRA_PATH = "/opt/homebrew/sbin:/opt/homebrew/bin:/usr/local/sbin:/usr/local/bin"
FULL_PATH = os.environ.get("PATH", "") + ":" + EXTRA_PATH
COLORS = {"run": QColor("#3a9a4a"), "bad": QColor("#d9534f"), "stopping": QColor("#e0a030"), "idle": QColor("#888888")}


def port_open(host: str, port: int, timeout: float = 0.15) -> bool:
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


# -- 파라미터 폼 ---------------------------------------------------------------

class ParamDialog(QDialog):
    """등록표 params 로 만든 입력 폼. add 모드면 대수와 '바로 시작' 체크가 붙는다."""

    def __init__(self, parent: QWidget, title: str, spec: dict, values: dict[str, Any], add: bool = False) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.spec = spec
        self.widgets: dict[str, QWidget] = {}
        form = QFormLayout()
        self.count: QSpinBox | None = None
        if add and spec["mode"] == "multi":
            self.count = QSpinBox()
            self.count.setRange(1, 50)
            self.count.setToolTip("2 이상이면 이름은 자동 번호, 등록표 step 키(예: y)는 대수마다 더해진다")
            form.addRow("대수", self.count)
        group = None
        for p in spec.get("params", []):
            if p.get("group") != group:
                group = p.get("group")
                if group:
                    form.addRow(QLabel(f"<b>{group}</b>"))
            w = self._make(p, values.get(p["key"], p.get("default")))
            self.widgets[p["key"]] = w
            form.addRow(p.get("label", p["key"]), w)
        body = QWidget()
        body.setLayout(form)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        scroll.setMinimumHeight(min(560, 60 + 32 * len(spec.get("params", [])) + (30 if self.count else 0)))
        self.autostart = QCheckBox("추가 후 바로 시작")
        self.autostart.setChecked(True)
        self.autostart.setVisible(add)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(scroll)
        lay.addWidget(self.autostart)
        lay.addWidget(bb)
        self.resize(480, 0)

    @staticmethod
    def _make(p: dict, v: Any) -> QWidget:
        t = p["type"]
        if t == "bool":
            w = QCheckBox()
            w.setChecked(bool(v))
        elif t == "int":
            w = QSpinBox()
            w.setRange(p.get("min", -1_000_000), p.get("max", 1_000_000))
            w.setValue(int(v or 0))
        elif t == "float":
            w = QDoubleSpinBox()
            w.setRange(p.get("min", -1e6), p.get("max", 1e6))
            w.setDecimals(3)
            w.setSingleStep(p.get("step", 0.1))
            w.setValue(float(v or 0.0))
        elif t == "choice":
            w = QComboBox()
            w.addItems(p["choices"])
            w.setCurrentText(str(v))
        else:
            w = QLineEdit("" if v is None else str(v))
        return w

    def values(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for p in self.spec.get("params", []):
            w = self.widgets[p["key"]]
            t = p["type"]
            if t == "bool":
                out[p["key"]] = w.isChecked()
            elif t == "int":
                out[p["key"]] = w.value()
            elif t == "float":
                out[p["key"]] = round(w.value(), 4)
            elif t == "choice":
                out[p["key"]] = w.currentText()
            else:
                out[p["key"]] = w.text().strip()
        return out


# -- 메인 창 -------------------------------------------------------------------

class Launcher(QMainWindow):
    def __init__(self, tools: list[dict]) -> None:
        super().__init__()
        self.setWindowTitle("AMR 시뮬레이터 런처")
        self.resize(1150, 680)
        self.tools = tools
        self.spec = {t["id"]: t for t in tools}
        self.insts: list[Inst] = []
        self.closing = False
        self.ticks = 0
        self.settings = QSettings("AMR-simulator", "launcher")

        # -- 공통 설정 줄 --
        self.host = QLineEdit(str(self.settings.value("host", "localhost")))
        self.host.setFixedWidth(130)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        try:
            self.port.setValue(int(self.settings.value("port", 1883)))
        except (TypeError, ValueError):
            self.port.setValue(1883)
        self.map_combo = QComboBox()
        self.map_combo.setMinimumWidth(200)
        reload_btn = QPushButton("맵 목록 새로고침")
        reload_btn.clicked.connect(self.reload_maps)
        self.broker_label = QLabel("")
        top = QHBoxLayout()
        for w in (QLabel("브로커 host"), self.host, QLabel("port"), self.port, QLabel("  맵"), self.map_combo, reload_btn):
            top.addWidget(w)
        top.addStretch()
        top.addWidget(self.broker_label)

        # -- 프로세스 표 + 로그 --
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["이름", "상태", "PID", "경과"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.table.itemSelectionChanged.connect(self.on_select)

        self.cmd_label = QLabel("")
        self.cmd_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.cmd_label.setWordWrap(True)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(C.MAX_LINES)
        mono = QFont("Menlo")
        mono.setStyleHint(QFont.Monospace)
        self.log.setFont(mono)
        self.log.setLineWrapMode(QPlainTextEdit.NoWrap)
        clear_btn = QPushButton("화면 지우기")
        clear_btn.clicked.connect(self.log.clear)
        open_btn = QPushButton("로그 파일 열기")
        open_btn.clicked.connect(self.open_log_file)
        logbar = QHBoxLayout()
        logbar.addWidget(self.cmd_label, 1)
        logbar.addWidget(clear_btn)
        logbar.addWidget(open_btn)
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.addLayout(logbar)
        rv.addWidget(self.log, 1)
        split = QSplitter()
        split.addWidget(self.table)
        split.addWidget(right)
        split.setSizes([460, 690])

        # -- 버튼 줄 --
        self.btn_start = QPushButton("시작")
        self.btn_stop = QPushButton("정지")
        self.btn_restart = QPushButton("재시작")
        self.btn_conf = QPushButton("설정…")
        self.btn_remove = QPushButton("목록에서 제거")
        self.btn_start.clicked.connect(lambda: self.with_sel(self.start))
        self.btn_stop.clicked.connect(lambda: self.with_sel(self.stop))
        self.btn_restart.clicked.connect(lambda: self.with_sel(self.restart))
        self.btn_conf.clicked.connect(lambda: self.with_sel(self.configure))
        self.btn_remove.clicked.connect(lambda: self.with_sel(self.remove))
        add_btn = QToolButton()
        add_btn.setText("추가 ▾")
        add_btn.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(add_btn)
        for t in tools:
            if t["mode"] != "single":
                label = {"multi": "추가…", "per_map": "(선택한 맵으로 실행)", "oneshot": "실행"}[t["mode"]]
                menu.addAction(f"{t['name']} {label}", lambda tid=t["id"]: self.add_tool(tid))
        add_btn.setMenu(menu)
        stop_all = QPushButton("전부 정지")
        stop_all.clicked.connect(self.stop_all)
        bar = QHBoxLayout()
        for w in (add_btn, self.btn_start, self.btn_stop, self.btn_restart, self.btn_conf, self.btn_remove):
            bar.addWidget(w)
        bar.addStretch()
        bar.addWidget(stop_all)

        central = QWidget()
        v = QVBoxLayout(central)
        v.addLayout(top)
        v.addWidget(split, 1)
        v.addLayout(bar)
        self.setCentralWidget(central)

        for t in tools:
            if t["mode"] == "single":
                vals = C.default_values(t)
                vals.update(self.saved_params(t["id"]))
                self.add_inst(Inst(t["id"], t["name"], vals, logname=t["id"]))
        self.reload_maps(initial=True)
        self.restore_orphans()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(200)
        if self.insts:
            self.table.selectRow(0)

    # -- 설정 저장 --
    def saved_params(self, tool_id: str) -> dict[str, Any]:
        try:
            d = json.loads(str(self.settings.value(f"params/{tool_id}", "{}")))
            return d if isinstance(d, dict) else {}
        except ValueError:
            return {}

    def save_params(self, tool_id: str, values: dict[str, Any]) -> None:
        self.settings.setValue(f"params/{tool_id}", json.dumps(values, ensure_ascii=False))

    # -- 공통 값 --
    def common(self) -> dict[str, Any]:
        return {"python": sys.executable, "repo": str(REPO), "host": self.host.text().strip() or "localhost",
                "port": self.port.value(), "map": self.map_combo.currentData() or ""}

    def values_for(self, inst: Inst) -> dict[str, Any]:
        # 기본값을 먼저 깐다: 등록표에 새 param 이 생겨도 예전에 저장/재부착한 값으로 KeyError 가 안 나게
        v = {**C.default_values(self.spec[inst.tool]), **inst.values, **self.common()}
        if inst.map_path:
            v["map"] = inst.map_path
        return v

    def reload_maps(self, initial: bool = False) -> None:
        prev = self.settings.value("map", "") if initial else self.map_combo.currentData()
        self.map_combo.clear()
        self.map_combo.addItem("(맵 없음)", "")
        for name, path in C.scan_maps():
            self.map_combo.addItem(name, path)
        i = self.map_combo.findData(prev) if prev else -1  # 저장된 맵이 없거나 사라졌으면 첫 맵
        self.map_combo.setCurrentIndex(i if i >= 0 else (1 if self.map_combo.count() > 1 else 0))

    # -- 표 --
    def selected(self) -> Inst | None:
        r = self.table.currentRow()
        return self.insts[r] if 0 <= r < len(self.insts) else None

    def with_sel(self, fn) -> None:
        inst = self.selected()
        if inst is None:
            QMessageBox.information(self, "선택 없음", "목록에서 항목을 먼저 고른다.")
            return
        fn(inst)

    def add_inst(self, inst: Inst) -> bool:
        if any(i.name == inst.name for i in self.insts):
            QMessageBox.warning(self, "이름 중복", f"{inst.name!r} 이(가) 이미 목록에 있다. 먼저 제거하거나 그 항목을 쓴다.")
            return False
        self.insts.append(inst)
        self.table.insertRow(self.table.rowCount())
        for c in range(4):
            self.table.setItem(self.table.rowCount() - 1, c, QTableWidgetItem(""))
        self.update_row(len(self.insts) - 1)
        return True

    def update_row(self, r: int) -> None:
        inst = self.insts[r]
        cells = [inst.name, inst.status, str(inst.pid or ""), inst.elapsed()]
        if inst.running:
            color = COLORS["stopping"] if inst.user_stop else COLORS["run"]
        else:
            color = COLORS["bad"] if inst.bad else COLORS["idle"]
        for c, text in enumerate(cells):
            it = self.table.item(r, c)
            if it.text() != text:
                it.setText(text)
            if c == 1:
                it.setForeground(color)
        self.table.item(r, 0).setToolTip(" ".join(inst.argv) if inst.argv else "")

    def on_select(self) -> None:
        self.show_log(self.selected())

    def show_log(self, inst: Inst | None) -> None:
        self.log.setPlainText("\n".join(inst.lines) if inst else "")
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())
        self.cmd_label.setText(f"$ {' '.join(inst.argv)}" if inst and inst.argv else "")

    def open_log_file(self) -> None:
        inst = self.selected()
        if inst and inst.log_path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(inst.log_path)))
        else:
            QMessageBox.information(self, "로그 없음", "아직 로그 파일이 없다.")

    # -- 주기 처리 --
    def tick(self) -> None:
        self.ticks += 1
        sel = self.selected()
        dirty = False
        for r, inst in enumerate(self.insts):
            if inst.running:
                new = inst.read_log()
                if new and inst is sel:
                    self.log.appendPlainText("\n".join(new))
                if inst.poll():
                    dirty = True
                    if inst is sel:
                        self.show_log(inst)
                    if inst.restart_pending:
                        inst.restart_pending = False
                        QTimer.singleShot(0, lambda i=inst: self.start(i))
                elif (inst.stop_deadline and time.time() > inst.stop_deadline and not inst.force_asked
                      and not self.closing):
                    inst.force_asked = True
                    QTimer.singleShot(0, lambda i=inst: self.ask_force(i))
            self.update_row(r)
        if dirty:
            C.save_state(self.insts)
        if self.ticks % 5 == 0:
            self.check_broker()

    def check_broker(self) -> None:
        host, port = self.common()["host"], self.common()["port"]
        ok = port_open(host, port)
        self.broker_label.setText(f"브로커 {host}:{port} " + ("응답 있음" if ok else "응답 없음"))
        for inst in self.insts:
            if self.spec[inst.tool].get("listens") and not inst.running and inst.status in ("정지", "외부 브로커 실행 중"):
                port_b = self.spec[inst.tool]["listens"]
                inst.status = "외부 브로커 실행 중" if port_open("127.0.0.1", port_b) else "정지"

    # -- 시작/정지 --
    def start(self, inst: Inst) -> None:
        if inst.running:
            return
        spec = self.spec[inst.tool]
        v = self.values_for(inst)
        if spec.get("needs_map") and not v["map"]:
            QMessageBox.warning(self, "맵 없음", f"{inst.name} 은(는) 맵이 필요하다. 위에서 맵을 고른다.")
            return
        try:
            argv = C.build_argv(spec, v)
            env = C.build_env(spec, v)
        except KeyError as ex:
            QMessageBox.critical(self, "등록표 오류", f"{inst.tool} 의 cmd 에 정의되지 않은 키: {ex}")
            return
        cwd = REPO / spec.get("cwd", ".")
        exe = argv[0]
        found = (cwd / exe).exists() if "/" in exe else shutil.which(exe, path=FULL_PATH)
        if not found:
            self.missing_exe(inst, spec, exe)
            return
        if spec.get("listens"):  # 브로커
            if port_open("127.0.0.1", spec["listens"]):
                QMessageBox.information(self, "브로커 이미 실행 중",
                                        f"127.0.0.1:{spec['listens']} 에서 이미 브로커가 응답한다. 외부 브로커를 그대로 쓴다.")
                return
            if v["port"] != spec["listens"] and QMessageBox.question(
                    self, "포트 불일치",
                    f"broker/mosquitto.conf 는 {spec['listens']} 번 포트로 고정이다. 위 port({v['port']}) 와 다르다. 그래도 시작할까?"
                    ) != QMessageBox.Yes:
                return
        if "broker" in spec.get("requires", []) and not port_open(v["host"], v["port"]):
            broker = next((i for i in self.insts if self.spec[i.tool].get("listens")), None)
            local = v["host"] in ("localhost", "127.0.0.1")
            if broker and local and not broker.running:
                if QMessageBox.question(self, "브로커 꺼짐", f"{v['host']}:{v['port']} 에 브로커가 없다. 브로커를 먼저 켤까?") == QMessageBox.Yes:
                    self.start(broker)
                    self.wait_broker_then(inst)
                return
            if QMessageBox.question(self, "브로커 응답 없음",
                                    f"{v['host']}:{v['port']} 응답이 없다. 그래도 {inst.name} 을(를) 시작할까?") != QMessageBox.Yes:
                return
        self.launch(inst, spec, argv, cwd, env)

    def launch(self, inst: Inst, spec: dict, argv: list[str], cwd: Path, env: dict[str, str]) -> None:
        inst.stop_timeout = float(spec.get("stop_timeout_s", 5))
        try:
            inst.launch(argv, cwd, {"PATH": FULL_PATH, **env})
        except OSError as ex:
            inst.status, inst.bad = f"시작 실패: {ex}", True
            self.refresh_all()
            return
        C.save_state(self.insts)
        self.refresh_all()
        if inst is self.selected():
            self.show_log(inst)

    def wait_broker_then(self, inst: Inst, tries: int = 25) -> None:
        c = self.common()
        if port_open(c["host"], c["port"]):
            self.start(inst)
        elif tries <= 0:
            QMessageBox.warning(self, "브로커 시작 실패", "브로커가 5초 안에 응답하지 않았다. 브로커 로그를 확인한다.")
        else:
            QTimer.singleShot(200, lambda: self.wait_broker_then(inst, tries - 1))

    def missing_exe(self, inst: Inst, spec: dict, exe: str) -> None:
        if spec.get("needs_build") and "make" in self.spec:
            if QMessageBox.question(self, "실행 파일 없음", f"{exe} 이(가) 없다. make 로 빌드할까? (끝난 뒤 다시 시작한다)") == QMessageBox.Yes:
                self.add_tool("make")
            return
        QMessageBox.critical(self, "실행 파일 없음", f"{exe} 을(를) 찾을 수 없다.\n(brew install 등으로 설치했는지 확인)")

    def stop(self, inst: Inst) -> None:
        if inst.running and not inst.user_stop:
            inst.terminate()
            self.refresh_all()

    def restart(self, inst: Inst) -> None:
        if inst.running:
            inst.restart_pending = True
            self.stop(inst)
        else:
            self.start(inst)

    def stop_all(self) -> None:
        for inst in self.insts:
            self.stop(inst)

    def ask_force(self, inst: Inst) -> None:
        if not inst.running:
            return
        r = QMessageBox.question(self, "정지 지연",
                                 f"{inst.name} 이(가) {inst.stop_timeout:.0f}초 안에 끝나지 않았다.\n"
                                 "강제 종료할까? (아니오: 정지를 취소하고 계속 실행)")
        if not inst.running:
            return
        if r == QMessageBox.Yes:
            inst.kill()
        else:
            inst.cancel_stop()
            inst.restart_pending = False

    def refresh_all(self) -> None:
        for r in range(len(self.insts)):
            self.update_row(r)

    # -- 추가/설정/제거 --
    def add_tool(self, tool_id: str) -> None:
        spec = self.spec[tool_id]
        mode = spec["mode"]
        if mode == "per_map":
            m = self.common()["map"]
            if not m:
                QMessageBox.warning(self, "맵 없음", f"{spec['name']} 은(는) 맵이 필요하다. 위에서 맵을 고른다.")
                return
            d = Path(m).parent.name
            inst = Inst(tool_id, f"{spec['name']} [{d}]", map_path=m, logname=C.safe_name(f"{tool_id}-{d}"))
            if self.add_inst(inst):
                self.start(inst)
        elif mode == "oneshot":
            inst = next((i for i in self.insts if i.tool == tool_id), None)
            vals = {**C.default_values(spec), **self.saved_params(tool_id)}
            if spec.get("params"):
                dlg = ParamDialog(self, spec["name"], spec, vals)
                if dlg.exec() != QDialog.Accepted:
                    return
                vals = dlg.values()
                self.save_params(tool_id, vals)
            if inst is None:
                inst = Inst(tool_id, spec["name"], vals, logname=tool_id)
                self.add_inst(inst)
            elif inst.running:
                QMessageBox.information(self, "실행 중", f"{inst.name} 은(는) 이미 실행 중이다.")
                return
            else:
                inst.values = vals
            self.table.selectRow(self.insts.index(inst))
            self.start(inst)
        else:
            self.add_multi(spec)

    def add_multi(self, spec: dict) -> None:
        cfg = spec["multi"]
        idp = cfg["id_param"]
        taken = {i.name for i in self.insts}
        vals = {**C.default_values(spec), **self.saved_params(spec["id"])}
        vals[idp] = C.next_id(spec, taken)[0]
        dlg = ParamDialog(self, f"{spec['name']} 추가", spec, vals, add=True)
        if dlg.exec() != QDialog.Accepted:
            return
        base = dlg.values()
        self.save_params(spec["id"], {k: v for k, v in base.items() if k != idp})
        n = dlg.count.value() if dlg.count else 1
        new: list[Inst] = []
        n0 = C.next_id(spec, taken)[1]
        for i in range(n):
            v = dict(base)
            if n > 1:
                while cfg["id_fmt"].format(n=n0) in taken:
                    n0 += 1
                v[idp] = cfg["id_fmt"].format(n=n0)
                n0 += 1
            if not v[idp] or v[idp] in taken:
                QMessageBox.warning(self, "이름 오류", f"이름이 비었거나 이미 있다: {v[idp]!r}")
                return
            taken.add(v[idp])
            for k, d in cfg.get("step", {}).items():
                v[k] = round(base[k] + i * d, 4)
            new.append(Inst(spec["id"], v[idp], v))
        for inst in new:
            self.add_inst(inst)
        if new:
            self.table.selectRow(self.insts.index(new[0]))
        if dlg.autostart.isChecked():
            for inst in new:
                self.start(inst)

    def configure(self, inst: Inst) -> None:
        spec = self.spec[inst.tool]
        if not spec.get("params"):
            QMessageBox.information(self, "설정 없음", f"{inst.name} 은(는) 설정 항목이 없다.")
            return
        if inst.running:
            QMessageBox.information(self, "실행 중", "정지한 뒤에 설정을 바꿀 수 있다.")
            return
        dlg = ParamDialog(self, f"{inst.name} 설정", spec, inst.values)
        if dlg.exec() != QDialog.Accepted:
            return
        vals = dlg.values()
        idp = spec.get("multi", {}).get("id_param")
        if idp and vals[idp] != inst.name:
            if not vals[idp] or any(i.name == vals[idp] for i in self.insts):
                QMessageBox.warning(self, "이름 오류", f"이름이 비었거나 이미 있다: {vals[idp]!r}")
                return
            inst.name, inst.logname = vals[idp], C.safe_name(vals[idp])
        inst.values = vals
        if spec["mode"] != "multi":
            self.save_params(inst.tool, vals)
        self.refresh_all()

    def remove(self, inst: Inst) -> None:
        if inst.running:
            QMessageBox.information(self, "실행 중", "정지한 뒤에 제거할 수 있다.")
            return
        if self.spec[inst.tool]["mode"] == "single":
            QMessageBox.information(self, "제거 불가", "고정 항목(브로커, 뷰어 등)은 목록에서 지울 수 없다.")
            return
        r = self.insts.index(inst)
        self.insts.pop(r)
        self.table.removeRow(r)

    # -- 이전 세션 복구 --
    def restore_orphans(self) -> None:
        orphans = C.load_orphans()
        if not orphans:
            C.save_state(self.insts)
            return
        box = QMessageBox(self)
        box.setWindowTitle("이전 세션 프로세스")
        box.setText(f"이전 런처가 띄운 프로세스 {len(orphans)}개가 아직 실행 중이다.\n"
                    + "\n".join(f"  {r['name']} (pid {r['pid']})" for r in orphans[:12]))
        adopt = box.addButton("목록에 가져오기", QMessageBox.AcceptRole)
        kill = box.addButton("전부 정지", QMessageBox.DestructiveRole)
        box.addButton("무시", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is kill:
            for r in orphans:
                try:
                    os.kill(int(r["pid"]), signal.SIGTERM)
                except ProcessLookupError:
                    pass
        elif box.clickedButton() is adopt:
            for r in orphans:
                if r.get("tool") not in self.spec:
                    continue
                inst = next((i for i in self.insts if i.tool == r["tool"] and not i.running
                             and self.spec[i.tool]["mode"] == "single"), None)
                if inst is None:
                    inst = Inst(r["tool"], r["name"], r.get("values") or {}, r.get("map_path", ""), r.get("logname"))
                    if not self.add_inst(inst):
                        continue
                inst.stop_timeout = float(self.spec[inst.tool].get("stop_timeout_s", 5))
                inst.adopt(int(r["pid"]), r["argv"], Path(r["cwd"]), float(r.get("started", time.time())))
        C.save_state(self.insts)
        self.refresh_all()

    # -- 종료 --
    def closeEvent(self, e) -> None:
        running = [i for i in self.insts if i.running]
        if running:
            box = QMessageBox(self)
            box.setWindowTitle("실행 중인 프로세스")
            box.setText(f"실행 중인 프로세스가 {len(running)}개 있다.")
            stop = box.addButton("전부 정지하고 종료", QMessageBox.AcceptRole)
            leave = box.addButton("그대로 두고 종료", QMessageBox.DestructiveRole)
            box.addButton("취소", QMessageBox.RejectRole)
            box.exec()
            c = box.clickedButton()
            if c is stop:
                if not self.stop_all_blocking(running):
                    e.ignore()
                    return
            elif c is leave:
                C.save_state(self.insts)  # 다음 실행 때 다시 찾아 붙는다
            else:
                e.ignore()
                return
        self.settings.setValue("host", self.host.text())
        self.settings.setValue("port", self.port.value())
        self.settings.setValue("map", self.map_combo.currentData() or "")
        self.timer.stop()
        if not any(i.running for i in self.insts):
            C.save_state(self.insts)
        e.accept()

    def stop_all_blocking(self, running: list[Inst]) -> bool:
        """전부 SIGTERM 후 끝날 때까지 이벤트를 돌리며 기다린다. 남으면 강제 종료할지 묻고, 취소하면 False."""
        self.closing = True
        for inst in running:
            inst.terminate()
        deadline = time.time() + max(i.stop_timeout for i in running)
        while time.time() < deadline and any(i.running for i in running):
            QApplication.processEvents()
            for i in running:
                i.poll()
            time.sleep(0.05)
        left = [i for i in running if i.running]
        if left:
            r = QMessageBox.question(self, "정지 지연", "아직 끝나지 않은 프로세스:\n" + "\n".join(f"  {i.name}" for i in left)
                                     + "\n\n강제 종료할까? (아니오: 종료 취소)")
            if r != QMessageBox.Yes:
                for i in left:
                    i.cancel_stop()
                self.closing = False
                return False
            for i in left:
                i.kill()
            end = time.time() + 2
            while time.time() < end and any(i.running for i in left):
                for i in left:
                    i.poll()
                time.sleep(0.05)
        C.save_state(self.insts)
        return True


def main() -> int:
    try:
        tools = C.load_registry()
    except ValueError as ex:
        app = QApplication(sys.argv)
        QMessageBox.critical(None, "런처를 시작할 수 없다", str(ex))
        return 1
    app = QApplication(sys.argv)
    win = Launcher(tools)
    win.show()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: QTimer.singleShot(0, win.close))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

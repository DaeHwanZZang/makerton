#!/usr/bin/env python3
"""
맵 생성 GUI (PySide6)
=====================

synth_map.py / gen_map.py 를 옵션 입력 + 시작 버튼으로 돌리고 결과를 바로 미리본다.
생성은 두 스크립트를 QProcess 로 실행하는 것뿐이라 CLI 와 결과가 같다.

    .venv/bin/python tools/map_gen_gui.py        # 리포 루트에서 (PySide6, numpy, pillow 필요)
"""

from __future__ import annotations

import os
import random
import signal
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QProcess, QSettings, Qt, QTimer
from PySide6.QtGui import QFont, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QSplitter, QVBoxLayout, QWidget,
    QLineEdit,
)

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent
SAMPLE_LAYOUT = TOOLS / "map_layouts" / "loop.txt"
DEFAULT_OUT = REPO / "maps" / "gui_map"


class Preview(QLabel):
    """창 크기에 맞춰 비율을 유지하며 줄여 보여주는 미리보기."""

    def __init__(self) -> None:
        super().__init__("생성 결과가 여기에 표시된다")
        self._pix: QPixmap | None = None
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(320, 240)
        self.setStyleSheet("background:#444; color:#ccc")

    def show_image(self, path: Path) -> None:
        pix = QPixmap(str(path))
        self._pix = None if pix.isNull() else pix
        if self._pix is None:
            self.setText(f"이미지를 못 읽었다: {path}")
        self._refit()

    def _refit(self) -> None:
        if self._pix:
            self.setPixmap(self._pix.scaled(self.size(), Qt.KeepAspectRatio, Qt.FastTransformation))

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._refit()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("맵 생성기")
        self.resize(1100, 720)
        self.proc: QProcess | None = None
        self.out_dir: Path | None = None
        self.tmp_layout: str | None = None

        # -- 왼쪽: 레이아웃 편집 --
        self.layout_edit = QPlainTextEdit()
        mono = QFont("Menlo")
        mono.setStyleHint(QFont.Monospace)
        self.layout_edit.setFont(mono)
        self.layout_edit.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.layout_edit.setPlainText(SAMPLE_LAYOUT.read_text(encoding="utf-8"))
        open_btn, sample_btn = QPushButton("열기…"), QPushButton("샘플")
        open_btn.clicked.connect(self.open_layout)
        sample_btn.clicked.connect(lambda: self.layout_edit.setPlainText(SAMPLE_LAYOUT.read_text(encoding="utf-8")))
        row = QHBoxLayout()
        row.addWidget(QLabel(". 통로  # 막힘  ; 주석 (한 글자 = 1 m)"))
        row.addStretch()
        row.addWidget(open_btn)
        row.addWidget(sample_btn)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.addLayout(row)
        lv.addWidget(self.layout_edit)

        # -- 가운데: 옵션 --
        self.mode = QComboBox()
        self.mode.addItems(["합성 (synth_map)", "실제 조각 (gen_map)"])
        self.out_edit = QLineEdit(str(DEFAULT_OUT.relative_to(REPO)))
        browse = QPushButton("…")
        browse.setFixedWidth(32)
        browse.clicked.connect(self.browse_out)
        out_row = QHBoxLayout()
        out_row.addWidget(self.out_edit)
        out_row.addWidget(browse)
        self.seed = QSpinBox()
        self.seed.setRange(0, 999999)
        self.seed.setValue(1)
        self.tries = QSpinBox()
        self.tries.setRange(1, 200)
        self.tries.setValue(20)
        self.tries.setToolTip("검사 실패 시 seed+1, +2... 로 다시 만드는 횟수")
        self.robot_w = QDoubleSpinBox()
        self.robot_w.setRange(0.1, 3.0)
        self.robot_w.setSingleStep(0.1)
        self.robot_w.setValue(0.5)
        self.robot_w.setSuffix(" m")
        self.margin = QSpinBox()
        self.margin.setRange(0, 50)
        self.margin.setValue(10)
        self.margin.setSuffix(" 타일")
        self.margin.setToolTip("사방 회색(미탐색) 여백. 1 타일 = 1 m")
        self.force = QCheckBox("검사 실패해도 저장")
        common = QGroupBox("공통")
        f = QFormLayout(common)
        f.addRow("방식", self.mode)
        f.addRow("출력 폴더", out_row)
        f.addRow("seed", self.seed)
        f.addRow("재시도 횟수", self.tries)
        f.addRow("검사용 로봇 폭", self.robot_w)
        f.addRow("사방 여백", self.margin)
        f.addRow(self.force)

        self.wall_px = QSpinBox()
        self.wall_px.setRange(2, 8)
        self.wall_px.setSingleStep(2)
        self.wall_px.setValue(2)
        self.wall_px.setSuffix(" px")
        self.wall_px.setToolTip("벽 두께. 경계 양쪽에 절반씩 (짝수만)")
        self.fringe_px = QSpinBox()
        self.fringe_px.setRange(0, 6)
        self.fringe_px.setValue(2)
        self.fringe_px.setSuffix(" px")
        self.fringe_px.setToolTip("벽 테두리 노이즈 폭. 0 이면 테두리 없음")
        self.fringe_prob = QDoubleSpinBox()
        self.fringe_prob.setRange(0.0, 1.0)
        self.fringe_prob.setSingleStep(0.1)
        self.fringe_prob.setValue(0.5)
        self.fringe_prob.setToolTip("벽 바로 옆 1 px 째 노이즈 확률")
        self.wobble_px = QDoubleSpinBox()
        self.wobble_px.setRange(0.0, 4.0)
        self.wobble_px.setSingleStep(0.5)
        self.wobble_px.setValue(1.0)
        self.wobble_px.setSuffix(" px")
        self.wobble_px.setToolTip("벽 흔들림 진폭. 0 이면 정확한 직선 (벽이 16, 17 번째 픽셀)")
        self.dash_gap = QDoubleSpinBox()
        self.dash_gap.setRange(0.0, 0.9)
        self.dash_gap.setSingleStep(0.05)
        self.dash_gap.setValue(0.25)
        self.dash_gap.setToolTip("점선 벽: 끊기는 벽 픽셀 비율. 0 이면 실선")
        self.dash_px = QSpinBox()
        self.dash_px.setRange(1, 30)
        self.dash_px.setValue(5)
        self.dash_px.setSuffix(" px")
        self.dash_px.setToolTip("점선 벽: 끊김/이음 길이")
        self.fur_prob = QDoubleSpinBox()
        self.fur_prob.setRange(0.0, 1.0)
        self.fur_prob.setSingleStep(0.02)
        self.fur_prob.setDecimals(2)
        self.fur_prob.setValue(0.06)
        self.fur_prob.setToolTip("털: 끊긴 벽 픽셀마다 털이 시작할 확률. 0 이면 없음")
        self.fur_len = QSpinBox()
        self.fur_len.setRange(1, 40)
        self.fur_len.setValue(10)
        self.fur_len.setSuffix(" px")
        self.ray_prob = QDoubleSpinBox()
        self.ray_prob.setRange(0.0, 0.2)
        self.ray_prob.setSingleStep(0.004)
        self.ray_prob.setDecimals(3)
        self.ray_prob.setValue(0.012)
        self.ray_prob.setToolTip("긴 광선: 끊긴 벽 픽셀마다 시작할 확률. 0 이면 없음")
        self.ray_len = QSpinBox()
        self.ray_len.setRange(10, 1000)
        self.ray_len.setValue(150)
        self.ray_len.setSuffix(" px")
        self.cut = QLineEdit()
        self.cut.setPlaceholderText("X1,Y1,X2,Y2 (비우면 없음)")
        self.cut.setToolTip("world 좌표(m) 직선 바깥을 막힘으로 잘라 사선 벽을 만든다. 칸 (r,c) 중심 = (c+1, r+1).\n"
                            "예: 0.5,24.5,100.5,0.5 (직사각형 레이아웃의 왼쪽 위를 사선으로 자름)")
        self.walls = QLineEdit()
        self.walls.setPlaceholderText("내부 벽 파일 경로 (비우면 없음)")
        self.walls.setToolTip("줄마다 'X1 Y1 X2 Y2' (world m). 칸 경계 위에 얇은 선 벽을 긋는다 (예: tools/map_layouts/loop3.walls)")
        self.void = QLineEdit()
        self.void.setPlaceholderText("x,y x,y x,y ... (비우면 없음)")
        self.void.setToolTip("가운데 뚫린 곳 등을 회색(주행 불가)으로 칠할 다각형. world m, 여러 개는 ;")
        self.carve = QLineEdit()
        self.carve.setPlaceholderText("X1,Y1,X2,Y2;... (비우면 없음)")
        self.carve.setToolTip("회색 영역 안에 통로(흰색) 사각형을 판다. world m, 여러 개는 ;. 복도 쪽 변은 복도 안으로 조금 넘겨 그린다")
        self.synth_box = QGroupBox("합성 옵션")
        f = QFormLayout(self.synth_box)
        f.addRow("벽 두께", self.wall_px)
        f.addRow("테두리 폭", self.fringe_px)
        f.addRow("테두리 확률", self.fringe_prob)
        f.addRow("벽 흔들림", self.wobble_px)
        f.addRow("점선 끊김 비율", self.dash_gap)
        f.addRow("점선 길이", self.dash_px)
        f.addRow("털 확률", self.fur_prob)
        f.addRow("털 길이", self.fur_len)
        f.addRow("긴 광선 확률", self.ray_prob)
        f.addRow("긴 광선 길이", self.ray_len)
        f.addRow("사선 자르기", self.cut)
        f.addRow("내부 벽 파일", self.walls)
        f.addRow("회색 영역", self.void)
        f.addRow("통로 사각형", self.carve)

        self.clean = QComboBox()
        self.clean.addItems(["none", "light", "normal", "strong"])
        self.clean.setCurrentText("normal")
        self.clean.setToolTip("생성 후 노이즈 정리 (clean_map.py)")
        self.patch_box = QGroupBox("조각 옵션")
        f = QFormLayout(self.patch_box)
        f.addRow("노이즈 정리", self.clean)

        self.start = QPushButton("생성")
        self.start.setStyleSheet("font-weight:bold; padding:8px")
        self.start.clicked.connect(lambda: self.generate())
        self.reroll = QPushButton("seed 랜덤 재생성")
        self.reroll.setStyleSheet("padding:8px")
        self.reroll.setToolTip("옵션은 그대로 두고 seed 만 무작위로 바꿔서 바로 다시 생성 (덮어쓰기 확인 없음)")
        self.reroll.clicked.connect(self.generate_random)
        self.mode.currentIndexChanged.connect(self.sync_mode)

        mid = QWidget()
        mv = QVBoxLayout(mid)
        mv.addWidget(common)
        mv.addWidget(self.synth_box)
        mv.addWidget(self.patch_box)
        mv.addStretch()
        btns = QHBoxLayout()
        btns.addWidget(self.start, 1)
        btns.addWidget(self.reroll, 1)
        mv.addLayout(btns)
        mid.setMaximumWidth(330)

        # -- 오른쪽: 미리보기 + 로그 --
        self.preview = Preview()
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(170)
        self.log.setFont(mono)
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.addWidget(self.preview, 1)
        rv.addWidget(self.log)

        split = QSplitter()
        split.addWidget(left)
        split.addWidget(mid)
        split.addWidget(right)
        split.setSizes([280, 320, 500])
        self.setCentralWidget(split)

        # 마지막으로 쓴 옵션 (QSettings: macOS ~/Library/Preferences/AMR-simulator.map_gen_gui.plist)
        self.settings = QSettings("AMR-simulator", "map_gen_gui")
        self.fields = {
            "layout": self.layout_edit, "mode": self.mode, "out": self.out_edit, "seed": self.seed,
            "tries": self.tries, "robot_w": self.robot_w, "margin": self.margin, "force": self.force,
            "wall_px": self.wall_px, "fringe_px": self.fringe_px, "fringe_prob": self.fringe_prob,
            "wobble_px": self.wobble_px, "dash_gap": self.dash_gap, "dash_px": self.dash_px,
            "fur_prob": self.fur_prob, "fur_len": self.fur_len, "ray_prob": self.ray_prob,
            "ray_len": self.ray_len, "cut": self.cut, "walls": self.walls, "void": self.void, "carve": self.carve, "clean": self.clean,
        }
        self.load_settings()
        self.sync_mode()

    # -- 옵션 저장/복원 --
    def save_settings(self) -> None:
        for key, w in self.fields.items():
            if isinstance(w, QPlainTextEdit):
                v = w.toPlainText()
            elif isinstance(w, QComboBox):
                v = w.currentText()
            elif isinstance(w, QCheckBox):
                v = w.isChecked()
            elif isinstance(w, QLineEdit):
                v = w.text()
            else:
                v = w.value()
            self.settings.setValue(key, v)

    def load_settings(self) -> None:
        for key, w in self.fields.items():
            if not self.settings.contains(key):
                continue
            v = self.settings.value(key)
            try:
                if isinstance(w, QPlainTextEdit):
                    w.setPlainText(str(v))
                elif isinstance(w, QComboBox):
                    w.setCurrentText(str(v))
                elif isinstance(w, QCheckBox):
                    w.setChecked(str(v).lower() == "true")
                elif isinstance(w, QLineEdit):
                    w.setText(str(v))
                elif isinstance(w, QSpinBox):
                    w.setValue(int(v))
                else:
                    w.setValue(float(v))
            except (TypeError, ValueError):
                pass  # 깨진 값은 기본값 유지

    def closeEvent(self, e) -> None:
        self.save_settings()
        if self.proc:  # 생성 중에 닫으면 자식 스크립트도 같이 끝낸다 (고아 프로세스 방지)
            self.proc.kill()
            self.proc.waitForFinished(2000)
        super().closeEvent(e)

    # -- 동작 --
    def sync_mode(self) -> None:
        synth = self.mode.currentIndex() == 0
        self.synth_box.setVisible(synth)
        self.patch_box.setVisible(not synth)

    def open_layout(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "레이아웃 열기", str(TOOLS / "map_layouts"), "텍스트 (*.txt);;전체 (*)")
        if path:
            self.layout_edit.setPlainText(Path(path).read_text(encoding="utf-8"))

    def browse_out(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "출력 폴더", str(REPO / "maps"))
        if path:
            self.out_edit.setText(path)

    def say(self, text: str) -> None:
        self.log.appendPlainText(text.rstrip())

    def generate_random(self) -> None:
        if self.proc:
            return
        self.seed.setValue(random.randint(0, self.seed.maximum()))
        self.generate(confirm=False)

    def generate(self, confirm: bool = True) -> None:
        if self.proc:
            return
        out = Path(self.out_edit.text().strip() or str(DEFAULT_OUT))
        out = out if out.is_absolute() else REPO / out
        if confirm and (out / "fms_map.json").exists():
            ok = QMessageBox.question(self, "덮어쓰기", f"{out} 에 이미 맵이 있다. grid.png 를 덮어쓸까?\n(edited_navi_gridmap.png 는 그대로 둔다)") == QMessageBox.Yes
            if not ok:
                return
        self.save_settings()
        fd, self.tmp_layout = tempfile.mkstemp(suffix=".txt", prefix="layout_")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(self.layout_edit.toPlainText())

        synth = self.mode.currentIndex() == 0
        args = [str(TOOLS / ("synth_map.py" if synth else "gen_map.py")), self.tmp_layout, "--out", str(out),
                "--name", out.name, "--seed", str(self.seed.value()), "--tries", str(self.tries.value()),
                "--robot-width", str(self.robot_w.value()), "--margin-tiles", str(self.margin.value())]
        if self.force.isChecked():
            args.append("--force")
        if synth:
            args += ["--wall-px", str(self.wall_px.value()), "--fringe-px", str(self.fringe_px.value()),
                     "--fringe-prob", str(self.fringe_prob.value()), "--wobble-px", str(self.wobble_px.value()),
                     "--dash-gap", str(self.dash_gap.value()), "--dash-px", str(self.dash_px.value()),
                     "--fur-prob", str(self.fur_prob.value()), "--fur-len", str(self.fur_len.value()),
                     "--ray-prob", str(self.ray_prob.value()), "--ray-len", str(self.ray_len.value())]
            if self.cut.text().strip():
                args += ["--cut", self.cut.text().strip()]
            if self.carve.text().strip():
                args += ["--carve", self.carve.text().strip()]
            if self.void.text().strip():
                args += ["--void", self.void.text().strip()]
            if self.walls.text().strip():
                w = Path(self.walls.text().strip())
                args += ["--walls", str(w if w.is_absolute() else REPO / w)]
        else:
            args += ["--clean", self.clean.currentText()]

        self.out_dir = out
        self.log.clear()
        self.say("$ python " + " ".join(Path(a).name if a == self.tmp_layout else a for a in args))
        self.start.setEnabled(False)
        self.reroll.setEnabled(False)
        self.start.setText("생성 중…")
        self.proc = QProcess(self)
        self.proc.setWorkingDirectory(str(REPO))
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.readyReadStandardOutput.connect(lambda: self.say(bytes(self.proc.readAllStandardOutput()).decode(errors="replace")))
        self.proc.finished.connect(self.done)
        self.proc.start(sys.executable, args)

    def done(self, code: int, _status) -> None:
        self.proc = None
        self.start.setEnabled(True)
        self.reroll.setEnabled(True)
        self.start.setText("생성")
        if self.tmp_layout:
            Path(self.tmp_layout).unlink(missing_ok=True)
        if code == 0 and self.out_dir:
            self.preview.show_image(self.out_dir / "grid.png")
        else:
            self.say(f"실패 (종료 코드 {code})")


def main() -> int:
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    # 런처가 보내는 SIGTERM/SIGINT 는 창 닫기로 바꾼다 (옵션 저장 + 자식 정리가 closeEvent 에 있다)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: QTimer.singleShot(0, win.close))
    tick = QTimer()
    tick.timeout.connect(lambda: None)
    tick.start(200)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

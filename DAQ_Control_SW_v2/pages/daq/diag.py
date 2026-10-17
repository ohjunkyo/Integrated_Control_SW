"""DAQ System > DAQ Diag, mirroring ui_manager._create_daq_diagnostics_tab:
connection verdict, Run Diagnostics Now, known problems, read-only checks and
the output log. The manual tools run their sudo command through pkexec (it asks
for the password) with the output streamed here, and a failed diagnosis offers
its fix the same way (core.maintenance)."""
import threading
from datetime import datetime

from PyQt5.QtCore import QObject, QTimer, pyqtSignal
from PyQt5.QtWidgets import (QGridLayout, QGroupBox, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QPushButton,
                             QVBoxLayout, QWidget)

from core import daq_diag, env, maintenance, status


class _Bus(QObject):
    text = pyqtSignal(str)
    verdict = pyqtSignal(bool, str, str)
    tool_done = pyqtSignal(object, object)   # exit code, callback


class DiagPage(QWidget):
    def __init__(self):
        super().__init__()
        self.busy = False
        self.bus = _Bus()
        self.bus.text.connect(self._append)
        self.bus.verdict.connect(self._verdict)
        self.bus.tool_done.connect(lambda rc, fn: fn(rc))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)

        s1 = QGroupBox("DAQ connection")
        v = QVBoxLayout(s1)
        self.title = QLabel("Not checked yet.")
        self.title.setStyleSheet("font-size: 13pt; font-weight: 700;")
        v.addWidget(self.title)
        self.detail = QLabel("")
        self.detail.setObjectName("key")
        v.addWidget(self.detail)
        row = QHBoxLayout()
        self.run_btn = QPushButton("Run Diagnostics Now")
        self.run_btn.clicked.connect(self.run)
        row.addWidget(self.run_btn)
        row.addStretch(1)
        v.addLayout(row)
        lay.addWidget(s1)

        s3 = QGroupBox("Known problems")
        g = QGridLayout(s3)
        for r, (t, line) in enumerate(daq_diag.CAUSES):
            a = QLabel(t)
            a.setStyleSheet("font-weight: 700;")
            b = QLabel(line)
            b.setStyleSheet("font-weight: normal;")
            g.addWidget(a, r, 0)
            g.addWidget(b, r, 1)
        g.setColumnStretch(1, 1)
        tools = QHBoxLayout()
        tools.addWidget(QLabel("Manual tools:"))
        for t in daq_diag.MANUAL:
            b = QPushButton(t)
            b.clicked.connect(lambda _=False, c=maintenance.MANUAL[t]: self.run_tool(c))
            tools.addWidget(b)
        tools.addStretch(1)
        g.addLayout(tools, len(daq_diag.CAUSES), 0, 1, 2)
        checks = QHBoxLayout()
        checks.addWidget(QLabel("Checks:"))
        for label, cmd in daq_diag.CHECKS:
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, l=label, c=cmd: self._check(l, c))
            checks.addWidget(b)
        checks.addStretch(1)
        g.addLayout(checks, len(daq_diag.CAUSES) + 1, 0, 1, 2)
        lay.addWidget(s3)

        s2 = QGroupBox("Output")
        ol = QVBoxLayout(s2)
        bar = QHBoxLayout()
        bar.addStretch(1)
        clear = QPushButton("Clear")
        bar.addWidget(clear)
        ol.addLayout(bar)
        self.out = QPlainTextEdit()
        self.out.setObjectName("dark")
        self.out.setReadOnly(True)
        clear.clicked.connect(self.out.clear)
        ol.addWidget(self.out, 1)
        lay.addWidget(s2, 1)

    def _append(self, text):
        self.out.appendPlainText(text)

    def run(self):
        if self.busy:
            return
        self.busy = True
        self.run_btn.setEnabled(False)
        self._append(f"\n=== DAQ diagnostics  {datetime.now():%H:%M:%S} ===")
        base = env.read_config().get("BasePath", env.ADC_DIR)

        def work():
            try:
                ok, title, fix = daq_diag.diagnose(base, status.scan_running(), self.bus.text.emit)
            except Exception as e:
                ok, title, fix = False, "Diagnostics failed", str(e)
            self.bus.verdict.emit(ok, title, fix)
        threading.Thread(target=work, daemon=True).start()

    def _verdict(self, ok, title, fix):
        self.busy = False
        self.run_btn.setEnabled(True)
        fix, action = maintenance.FIX.get(title, (fix, None))
        self.title.setText(title)
        self.title.setStyleSheet(f"font-size: 13pt; font-weight: 700; color: {'#28a745' if ok else '#dc3545'};")
        self.detail.setText(fix)
        self._append(f"=> {title}: {fix}")
        if ok:
            QMessageBox.information(self, "DAQ diagnostics", "DAQ connected." if title == "DAQ connected" else title)
            return
        if action is None:
            if QMessageBox.warning(self, "DAQ diagnostics", f"{title}.\n\n{fix}\n\nOK: check again.",
                                   QMessageBox.Ok | QMessageBox.Cancel) == QMessageBox.Ok:
                self.run()
            return
        label, cmd, wait_s = action
        if QMessageBox.warning(self, "DAQ diagnostics", f"{title}.\n\n{label} now? (asks for the password)",
                               QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            self.run_tool(cmd, on_done=lambda _rc: QTimer.singleShot(int(wait_s * 1000), self.run))

    def run_tool(self, cmd, on_done=None):
        """pkexec <cmd> with its output streamed to Output, as the Tk _diag_run_cmd."""
        def work():
            rc = maintenance.stream(maintenance.pkexec(cmd), lambda t: self.bus.text.emit(t.rstrip("\n")))
            if on_done:
                self.bus.tool_done.emit(rc, on_done)
        threading.Thread(target=work, daemon=True).start()

    def _check(self, label, cmd):
        self._append(f"\n$ {cmd}")
        threading.Thread(target=lambda: self.bus.text.emit(daq_diag.run_check(cmd)), daemon=True).start()

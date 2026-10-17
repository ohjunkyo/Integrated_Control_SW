"""Logs tab, mirroring ui_manager._create_log_center_tab (Integrated Log Center).
Text logs are shown as Time | Level | Message tables, CSV logs and the HV DB as
column tables (shared soft-dark LogTable). Gather copies every log and the HV DB
into LOG/_UNIFIED (core.file_ops), as the Tk app."""
import glob
import os
import sqlite3
import threading

from PyQt5.QtCore import QObject, QTimer, pyqtSignal
from PyQt5.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QMessageBox, QPushButton, QTabWidget, QVBoxLayout,
                             QWidget)

from core import env, file_ops
from widgets.log_table import LogTable

SOURCES = {   # every log view of the app in one place (tab order)
    "App": (os.path.join(env.DAQ_DIR, "logs"), "log_*.txt"),
    "Scan": (os.path.join(env.DAQ_DIR, "logs"), "scan_live_*.txt"),
    "DAQ": (os.path.join(env.ADC_DIR, "LOG", "DAQ"), "TakingLog_*.txt"),
    "Laser": (os.path.join(env.ADC_DIR, "LOG", "LASER"), "laser_data_*.csv"),
    "UPS": (os.path.join(env.DAQ_DIR, "LOG", "UPS"), "ups_*.csv"),
}
GATHER = ("DAQ", "Laser", "UPS", "App")   # what the Tk app's Gather copies
FOLLOW_MS = 2000
MAX_FILES = 30          # newest files offered in the picker
MAX_BYTES = 2 * 1024 * 1024   # daily laser CSVs can be huge: show the tail only


def read_tail(path):
    size = os.path.getsize(path)
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:   # keep \r: progress lines
        if size <= MAX_BYTES:
            return f.read()
        f.seek(size - MAX_BYTES)
        tail = f.read()
    return (f"[... file is {size / 1048576:.1f} MB -- showing the last {MAX_BYTES // 1048576} MB only ...]\n"
            + tail[tail.find("\n") + 1:])


class FileLog(QWidget):
    def __init__(self, name):
        super().__init__()
        self.dir, self.pattern = SOURCES[name]
        lay = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(QLabel("File:"))
        self.combo = QComboBox()
        self.combo.setMinimumWidth(340)
        self.combo.currentTextChanged.connect(self.load)
        row.addWidget(self.combo)
        b = QPushButton("Refresh")
        b.clicked.connect(self.refresh)
        row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        self.table = LogTable()
        lay.addWidget(self.table, 1)
        self._path, self._pos = None, 0
        self.timer = QTimer(self)          # the newest text log follows new lines
        self.timer.timeout.connect(self._follow)
        self.timer.start(FOLLOW_MS)
        self.refresh()

    def _follow(self):
        if not self.isVisible():
            return
        files = sorted(glob.glob(os.path.join(self.dir, self.pattern)), key=os.path.getmtime, reverse=True)
        if files and os.path.basename(files[0]) != (self.combo.itemText(0) if self.combo.count() else ""):
            if self.combo.currentIndex() <= 0:    # a newer file appeared while following: switch to it
                self.refresh()
            return
        if self._path is None or self.combo.currentIndex() != 0 or self._path.endswith(".csv"):
            return
        try:
            with open(self._path, encoding="utf-8", errors="replace", newline="") as f:
                f.seek(self._pos)
                chunk = f.read()
                self._pos = f.tell()
        except OSError:
            return
        if chunk:
            self.table.feed(chunk)

    def refresh(self):
        files = sorted(glob.glob(os.path.join(self.dir, self.pattern)), key=os.path.getmtime, reverse=True)
        self.combo.blockSignals(True)
        self.combo.clear()
        self.combo.addItems([os.path.basename(f) for f in files[:MAX_FILES]])
        self.combo.blockSignals(False)
        if files:
            self.load(self.combo.currentText())
        else:
            self.table.message(f"(No log files found in {self.dir})")

    def load(self, name):
        if not name:
            return
        path = os.path.join(self.dir, name)
        try:
            text = read_tail(path)
        except Exception as e:
            self.table.set_columns(("Time", "Level", "Message"), (84, 96))
            self.table.message(f"Could not read {path}: {e}")
            return
        if name.endswith(".csv"):
            with open(path, encoding="utf-8", errors="replace") as f:
                header = f.readline().strip().split(",")
            rows = [ln.split(",") for ln in text.splitlines() if ln and not ln.startswith(("timestamp", "[..."))]
            rows = [r for r in rows if len(r) == len(header)]   # drops a half-written last line
            for r in rows:
                r[0] = r[0].replace("T", " ")[:19]
            self.table.set_columns(header, [160] + [90] * (len(header) - 1))
            self.table.set_rows(rows)
        else:
            self.table.set_columns(("Time", "Level", "Message"), (84, 96))
            self.table.set_text(text)
        self._path = path
        try:
            self._pos = os.path.getsize(path)
        except OSError:
            self._pos = 0


class HvLog(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(QLabel("Channel:"))
        self.ch = QComboBox()
        self.ch.addItems(["Ch0", "Ch1", "Ch2", "Ch3"])
        row.addWidget(self.ch)
        row.addWidget(QLabel("Last N:"))
        self.n = QComboBox()
        self.n.addItems(["100", "500", "2000"])
        row.addWidget(self.n)
        b = QPushButton("Load")
        b.clicked.connect(self.refresh)
        row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        self.table = LogTable()
        lay.addWidget(self.table, 1)
        self.ch.currentTextChanged.connect(self.refresh)
        self.n.currentTextChanged.connect(self.refresh)
        self.refresh()

    def refresh(self, *_a):
        if not os.path.exists(env.HV_DB):
            self.table.message(f"(HV DB not found: {env.HV_DB})")
            return
        ch = self.ch.currentText()
        cols = ["timestamp", f"{ch}_V", f"{ch}_I_L", f"{ch}_I_H", "Dark_Box_1_T", "Dark_Box_1_H"]
        try:
            con = sqlite3.connect(f"file:{env.HV_DB}?mode=ro", uri=True)
            recs = con.execute(f"SELECT {','.join(cols)} FROM monitoring_data ORDER BY rowid DESC LIMIT ?",
                               (int(self.n.currentText()),)).fetchall()
            con.close()
        except Exception as e:
            self.table.message(f"HV DB query failed: {e}")
            return

        def f(v):
            return f"{v:.2f}" if isinstance(v, (int, float)) else ("" if v is None else str(v))
        self.table.set_columns(["Time", f"{ch} V", f"{ch} I_L", f"{ch} I_H", "Box1 T", "Box1 H"],
                               (160, 90, 90, 90, 80, 80))
        self.table.set_rows([[str(r[0]).replace("T", " ")[:19]] + [f(v) for v in r[1:]] for r in reversed(recs)])


class _Bus(QObject):
    gathered = pyqtSignal(int, str, str)   # copied, destination, error


class LogsPage(QWidget):
    def __init__(self):
        super().__init__()
        self.bus = _Bus()
        self.bus.gathered.connect(self._on_gathered)
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        t = QLabel("Integrated Log Center")
        t.setStyleSheet("font-size: 12pt; font-weight: 600;")
        top.addWidget(t)
        top.addStretch(1)
        self.gather_btn = QPushButton("Gather all logs into one folder")
        self.gather_btn.clicked.connect(self.gather)
        top.addWidget(self.gather_btn)
        refresh = QPushButton("Refresh all")
        top.addWidget(refresh)
        lay.addLayout(top)
        tabs = QTabWidget()
        self.subtabs = [FileLog(n) for n in SOURCES] + [HvLog()]
        for w, name in zip(self.subtabs, list(SOURCES) + ["HV"]):
            tabs.addTab(w, name)
        refresh.clicked.connect(lambda: [w.refresh() for w in self.subtabs])
        lay.addWidget(tabs, 1)

    def gather(self):
        self.gather_btn.setEnabled(False)

        def work():
            try:
                copied, dest = file_ops.gather_logs({k: SOURCES[k] for k in GATHER}, env.HV_DB)
                self.bus.gathered.emit(copied, dest, "")
            except Exception as e:
                self.bus.gathered.emit(0, "", str(e))
        threading.Thread(target=work, daemon=True).start()

    def _on_gathered(self, copied, dest, error):
        self.gather_btn.setEnabled(True)
        if error:
            QMessageBox.critical(self, "Gather Failed", f"Could not gather logs:\n{error}")
        else:
            QMessageBox.information(self, "Logs Gathered", f"Copied {copied} log file(s) into:\n{dest}\n\n"
                                                           "(Originals were left untouched.)")

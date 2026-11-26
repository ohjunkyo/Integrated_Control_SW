# launcher.py
import os
import signal
import subprocess
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "DAQ_Control_SW_v2"))

from PyQt5.QtCore import Qt, QTimer  # noqa: E402
from PyQt5.QtWidgets import (QApplication, QGroupBox, QHBoxLayout, QLabel, QMessageBox,  # noqa: E402
                             QPushButton, QVBoxLayout, QWidget)

import instance_lock  # noqa: E402
from window_focus import focus_window, HV_TITLE, V2_TITLE  # noqa: E402
from app import theme  # noqa: E402

V2_PYTHON = os.path.join(HERE, "venv", "bin", "python")
APP_LOG_DIR = os.path.expanduser("~/logs")
LOG_MAX = 20 * 1024 * 1024
SCAN_MARKER = "/tmp/daq_flags/active_scan"
SETTINGS = os.path.expanduser("~/.config/precal/daq_v2.json")

# key: (label, lock, window title, command, working dir)
APPS = {
    "daq_v2": ("DAQ Control v2", "daq_v2", V2_TITLE,
               [V2_PYTHON if os.path.exists(V2_PYTHON) else "python3",
                os.path.join(HERE, "DAQ_Control_SW_v2", "main.py")],
               os.path.join(HERE, "DAQ_Control_SW_v2")),
    "hv": ("HV Monitor", "hv", HV_TITLE,
           ["python3", os.path.join(HERE, "HV_Control_SW", "monitoring_app.py"),
            os.path.join(HERE, "HV_Control_SW", "config_precal.json")],
           os.path.join(HERE, "HV_Control_SW")),
}
OTHER_LOCKS = ("daq", "laser_gui")   # retired apps, still stopped by Stop All


def _dark_mode():
    import json
    try:
        with open(SETTINGS) as f:
            return bool(json.load(f).get("dark_mode"))
    except (OSError, ValueError):
        return False


def _scan_running():
    try:
        tag, pid = open(SCAN_MARKER).read().split()[:2]
    except (OSError, ValueError):
        return ""
    return tag if os.path.exists(f"/proc/{pid}") else ""


def _app_log(key):
    """App output goes to a file, so closing the launch terminal cannot break the app's prints."""
    os.makedirs(APP_LOG_DIR, exist_ok=True)
    path = os.path.join(APP_LOG_DIR, f"launcher_{key}.log")
    try:
        if os.path.getsize(path) > LOG_MAX:
            os.replace(path, path + ".1")
    except OSError:
        pass
    f = open(path, "a", buffering=1)
    f.write(f"=== started by launcher {datetime.now():%Y-%m-%d %H:%M:%S}\n")
    return f


class AppCard(QGroupBox):
    def __init__(self, launcher, key):
        label = APPS[key][0]
        super().__init__(label)
        self.launcher, self.key, self.label = launcher, key, label
        row = QHBoxLayout(self)
        row.setSpacing(10)
        self.lamp = QLabel()
        self.lamp.setFixedSize(12, 12)
        self.state = QLabel()
        self.state.setStyleSheet("font-weight: 600;")
        self.detail = QLabel()
        self.detail.setStyleSheet(f"color: {theme.C['muted']}; font-weight: 400;")
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(self.state)
        text.addWidget(self.detail)
        self.btn = QPushButton()
        self.btn.setMinimumWidth(110)
        self.btn.clicked.connect(self._clicked)
        row.addWidget(self.lamp, 0, Qt.AlignVCenter)
        row.addLayout(text, 1)
        row.addWidget(self.btn, 0, Qt.AlignVCenter)

    def refresh(self):
        info = instance_lock.holder(APPS[self.key][1])
        proc = self.launcher.procs.get(self.key)
        if info:
            state, color, detail, btn, kind = ("Running", theme.C["good"],
                                               instance_lock.describe(info), "Show", "move")
        elif proc is not None and proc.poll() is None:
            state, color, detail, btn, kind = "Starting", theme.C["warn"], f"PID {proc.pid}", "Show", "move"
        else:
            state, color, detail, btn, kind = "Stopped", theme.C["disabled"], "", "Start", "start"
        self.state.setText(state)
        self.detail.setText(detail)
        self.detail.setVisible(bool(detail))
        self.lamp.setStyleSheet(f"background: {color}; border-radius: 6px;")
        self.btn.setText(btn)
        theme.role(self.btn, kind)

    def _clicked(self):
        if self.btn.text() == "Start":
            self.launcher.start(self.key)
        elif focus_window(APPS[self.key][2]) != "raised":
            QMessageBox.warning(self, self.label, f"The running {self.label} has no window; it may be hung.")
        self.refresh()


class Launcher(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Integrated Control Launcher")
        self.procs = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(theme.SPACE + 4, theme.SPACE, theme.SPACE + 4, theme.SPACE + 4)
        lay.setSpacing(theme.SPACE)
        title = QLabel("Integrated Control")
        title.setStyleSheet(f"font-size: {theme.PT['title'] + 2}pt; font-weight: 700;")
        lay.addWidget(title)
        self.cards = [AppCard(self, k) for k in APPS]
        for c in self.cards:
            lay.addWidget(c)
        lay.addStretch(1)
        bottom = QHBoxLayout()
        close = QPushButton("Close Launcher")
        theme.role(close, "neutral")
        close.clicked.connect(self.close)
        stop = QPushButton("Stop All and Exit")
        theme.role(stop, "danger")
        stop.clicked.connect(self.stop_all)
        bottom.addWidget(close, 1)
        bottom.addWidget(stop, 1)
        lay.addLayout(bottom)
        self.resize(440, 330)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(2000)
        self.refresh()

    def refresh(self):
        for proc in self.procs.values():
            proc.poll()   # reap exited apps (no zombies)
        for c in self.cards:
            c.refresh()

    def start(self, key):
        label, lock, _title, cmd, cwd = APPS[key]
        if instance_lock.is_running(lock):
            return
        try:
            log = _app_log(key)
            self.procs[key] = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=log,
                                               stderr=subprocess.STDOUT, start_new_session=True)
            log.close()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to start {label}:\n{e}")

    def stop_all(self):
        locks = [APPS[k][1] for k in APPS] + list(OTHER_LOCKS)
        running = [n for n in locks if instance_lock.is_running(n)]
        mine = [p for p in self.procs.values() if p.poll() is None]
        if not running and not mine:
            self.close()
            return
        names = "\n".join(instance_lock.APP_LABELS.get(n, n) for n in running)
        tag = _scan_running()
        warn = f"\n\nA General Scan ({tag}) is running and will be cut off." if tag else ""
        if QMessageBox.question(self, "Stop All and Exit", f"Stop every running application?\n\n{names}{warn}",
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            for p in mine:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            for n in running:
                instance_lock.terminate(n)
        finally:
            QApplication.restoreOverrideCursor()
        self.close()


def main():
    signal.signal(signal.SIGHUP, signal.SIG_IGN)   # closing the launch terminal leaves the launcher up
    os.chdir(HERE)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    theme.apply(app, dark=_dark_mode())
    w = Launcher()
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()

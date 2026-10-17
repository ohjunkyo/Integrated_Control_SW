"""DAQ Control v2 (Qt): the DAQ / laser / UPS control app, successor of the Tk app.

Same layout as the Tk app; see README.md for the package layout. While the Tk app is
open it owns the hardware and v2 only shows its files; with the Tk app closed v2 drives
the stages, lasers, UPS and scans itself (one master-password Unlock, as in the Tk app).

Run:  ~/Integrated_Control_SW/venv/bin/python ~/Integrated_Control_SW/DAQ_Control_SW_v2/main.py
"""
import faulthandler
import os
import signal
import sys
import time
import traceback
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import env  # puts the Tk app's modules on sys.path

from PyQt5.QtCore import QTimer, qInstallMessageHandler  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from app import theme  # noqa: E402
from app.window import MainWindow  # noqa: E402


def _log_exception(kind, value, tb):
    """PyQt5 aborts the whole app on an exception in a slot; log it and keep running instead."""
    text = "".join(traceback.format_exception(kind, value, tb))
    sys.stderr.write(text)
    try:
        path = os.path.join(env.DAQ_DIR, "logs", f"log_{datetime.now():%Y-%m-%d}.txt")
        with open(path, "a") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [ERROR] v2 unhandled exception: "
                    f"{kind.__name__}: {value}\n")
    except OSError:
        pass


def _crash_log():
    """Hard crashes (segfault / Qt abort) leave no Python traceback; keep their stack and Qt's last words."""
    path = os.path.join(env.DAQ_DIR, "logs", "v2_crash.log")
    try:
        f = open(path, "a", buffering=1)
    except OSError:
        return
    f.write(f"=== v2 started {datetime.now():%Y-%m-%d %H:%M:%S} pid {os.getpid()}\n")
    faulthandler.enable(file=f, all_threads=True)

    def qt_message(mode, context, message):
        f.write(f"[{datetime.now():%H:%M:%S}] Qt({int(mode)}) {message}\n")
        sys.stderr.write(message + "\n")
    qInstallMessageHandler(qt_message)
    main._crash_file = f


def _heartbeat():
    """From the GUI thread, so a stale file means a hung event loop (scripts/daq_heartbeat_watchdog.sh)."""
    try:
        with open(os.path.join(env.DAQ_DIR, "logs", "heartbeat_v2.txt"), "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def _scan_warning():
    """Extra text for Replace when the running copy may be in the middle of a scan."""
    try:
        tag, pid = open("/tmp/daq_flags/active_scan").read().split()[:2]
        if os.path.exists(f"/proc/{pid}"):
            return f"A General Scan ({tag}) is running in that copy. Replacing it ABORTS the scan."
    except (OSError, ValueError):
        pass
    return ""


def _single_instance():
    """One v2 per PC (instance lock daq_v2), and the Tk app's warning about the old Laser Control."""
    import instance_lock
    from PyQt5.QtWidgets import QMessageBox
    instance_lock.ensure_single_qt("daq_v2", "DAQ Control v2", warn_if=_scan_warning)
    if instance_lock.is_running("laser_gui"):
        QMessageBox.warning(None, "Old Laser Control is open",
                            "The old Laser Control window is running. It turns every laser OFF when it connects "
                            "and fights this panel for the laser USB devices.\n\nClose it before taking data.")


class _Splash:
    """Start-up window: app name and the step being loaded (text only)."""

    def __init__(self, app):
        from PyQt5.QtCore import Qt
        from PyQt5.QtGui import QColor, QFont, QPainter, QPixmap
        from PyQt5.QtWidgets import QSplashScreen
        self.app = app
        pm = QPixmap(560, 200)
        pm.fill(QColor(theme.C["card"]))
        p = QPainter(pm)
        p.setPen(QColor(theme.C["border"]))
        p.drawRect(0, 0, 559, 199)
        p.fillRect(0, 0, 560, 6, QColor(theme.C["accent"]))
        f = QFont(theme.UI_FONT)
        f.setPointSizeF(20)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(theme.C["text"]))
        p.drawText(28, 70, "Korean System DAQ Control v2")
        f.setPointSizeF(10)
        f.setBold(False)
        p.setFont(f)
        p.setPen(QColor(theme.C["muted"]))
        p.drawText(28, 98, "Hyper-K PMT precalibration")
        p.end()
        self.w = QSplashScreen(pm, Qt.WindowStaysOnTopHint)
        self.w.show()
        self.step("Loading")

    def step(self, text):
        from PyQt5.QtCore import Qt
        from PyQt5.QtGui import QColor
        self.w.showMessage(f"  {text}...", Qt.AlignLeft | Qt.AlignBottom, QColor(theme.C["accent"]))
        self.app.processEvents()

    def finish(self, window):
        self.w.finish(window)


def main():
    sys.excepthook = _log_exception
    _crash_log()
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)   # real exit codes for DAQ / analysis jobs
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    theme.apply(app, dark=env.settings().get("dark_mode", False))
    if not env.NO_HW:
        _single_instance()
    splash = _Splash(app)
    w = MainWindow(progress=splash.step)
    app._main_window = w
    w.show()
    splash.finish(w)
    beat = QTimer(app)
    beat.timeout.connect(_heartbeat)
    beat.start(1000)
    _heartbeat()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()

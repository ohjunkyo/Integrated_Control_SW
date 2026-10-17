"""UPS Status tab, mirroring ui_manager._create_ups_monitoring_tab.

While the Tk app is closed v2 owns the UPS (core/ups.py: polling, CSV logging, controls);
while it runs, this page shows the LOG/UPS/ups_<date>.csv rows the Tk app writes and all
controls are off. Port, Change..., Connect and the shutdown also need Unlock.
"""
import glob
import os
import threading
import time
from datetime import datetime

import matplotlib.dates as mdates
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (QComboBox, QDialog, QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel,
                             QLineEdit, QMessageBox, QProgressBar, QPushButton, QVBoxLayout, QWidget)

from app import theme
from core import ups
from core.access import ACCESS

OUTLET_LABELS = ["DAQ, PC, Electronics, etc.", "High voltage", "Empty", "Empty"]
GATED = ("combo", "change", "connect", "shutdown")   # change UPS state: Unlock required
TK_OWNS = "Used by the Tk app"


def _read_passive():
    """Rows for the read-only view: the last 24 h, else the newest log file."""
    rows = ups.load_history()
    path = ups.csv_path(datetime.now())
    if not rows:
        files = sorted(glob.glob(os.path.join(ups.ups_dir(), "ups_*.csv")))
        if not files:
            return [], None
        path = files[-1]
        day = datetime.strptime(os.path.basename(path)[4:12], "%Y%m%d")
        rows = ups.read_csv(path, day)
    elif not os.path.exists(path):
        path = ups.csv_path(rows[-1][0])
    return rows, path


class _Bus(QObject):
    event = pyqtSignal(str, object)
    done = pyqtSignal(str, object)


class OutletView(QWidget):
    """The UPS output: one switched output feeds all four outlets (BA100R has no per-outlet control or
    metering), so one lamp with the total load, and the operator's labels of what is plugged in.
    State 0 off, 1 on, 2 shutdown sent, None unknown."""

    TEXT = {1: ("ON", "#28a745"), 0: ("OFF", "#6c757d"), 2: ("SHUTDOWN SENT", "#dc3545"), None: ("UNKNOWN", "#adb5bd")}

    def __init__(self):
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        self.lamp = QLabel()
        self.lamp.setFixedSize(34, 34)
        top.addWidget(self.lamp)
        col = QVBoxLayout()
        self.state = QLabel()
        self.state.setStyleSheet("font-size: 14pt; font-weight: 800;")
        self.load = QLabel("--")
        self.load.setStyleSheet("font-size: 11pt; font-weight: 700;")
        col.addWidget(self.state)
        col.addWidget(self.load)
        top.addLayout(col)
        top.addStretch(1)
        v.addLayout(top)
        g = QGridLayout()
        g.setVerticalSpacing(2)
        for i, name in enumerate(OUTLET_LABELS):
            k = QLabel(f"Outlet {i + 1}")
            k.setObjectName("key")
            val = QLabel(name)
            val.setStyleSheet("font-weight: 600;")
            g.addWidget(k, i, 0)
            g.addWidget(val, i, 1)
        g.setColumnStretch(1, 1)
        v.addLayout(g)
        self.set_states([None] * 4)

    def set_states(self, states):
        st = states[0] if states else None
        text, color = self.TEXT.get(st, self.TEXT[None])
        self.state.setText(f"Output {text}")
        self.state.setStyleSheet(f"font-size: 14pt; font-weight: 800; color: {color};")
        self.lamp.setStyleSheet(f"background: {color}; border: 1px solid #333; border-radius: 17px;")

    def set_load(self, watt, pct=None):
        if watt is None:
            self.load.setText("--")
        else:
            self.load.setText(f"Load {watt:.0f} W" + (f"  ({pct:.0f} %)" if pct is not None else ""))


class CountdownDialog(QDialog):
    """Tk _run_shutdown_countdown: 5 s, cancellable; accept() = send."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("SHUTDOWN COUNTDOWN")
        self.setFixedSize(360, 160)
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self.setModal(True)
        v = QVBoxLayout(self)
        self.lbl = QLabel()
        self.lbl.setAlignment(Qt.AlignCenter)
        self.lbl.setStyleSheet(f"color: {theme.C['bad']}; font-size: 18pt; font-weight: 700;")
        v.addWidget(self.lbl, 1)
        cancel = QPushButton("CANCEL SHUTDOWN")
        cancel.setMinimumWidth(180)
        cancel.clicked.connect(self.reject)
        v.addWidget(cancel, 0, Qt.AlignHCenter)
        self.n = 5
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self._show_n()
        self.timer.start(1000)

    def _show_n(self):
        self.lbl.setText(f"Shutdown in  {self.n} ...")

    def _tick(self):
        self.n -= 1
        if self.n <= 0:
            self.timer.stop()
            self.accept()
        else:
            self._show_n()

    def done(self, r):
        self.timer.stop()
        super().done(r)


class UpsPage(QWidget):
    def __init__(self):
        super().__init__()
        self.svc = ups.service()
        self.bus = _Bus(self)
        self.bus.event.connect(self._on_event)
        self.bus.done.connect(self._on_done)
        self.svc.listeners.append(self._forward)
        # Tk button states (ui_manager / ups_manager); effective = this and ownership and access
        self.v1 = {"combo": True, "change": False, "search": True, "connect": False, "refresh": False,
                   "diag": True, "shutdown": True}

        lay = QVBoxLayout(self)
        lay.setContentsMargins(15, 15, 15, 15)

        conn = QGroupBox("UPS Connection (RS232C) OMRON BA100R ds-1423816")
        cl = QHBoxLayout(conn)
        cl.addWidget(QLabel("Port:"))
        self.port = QComboBox()
        self.port.setEditable(True)
        self.port.setMinimumWidth(160)
        cl.addWidget(self.port)
        self.ctl = {"combo": self.port}
        for key, text, fn in (("change", "Change...", self._change_port), ("search", "Search Ports", self._search),
                              ("connect", "Connect UPS", self._toggle_connection),
                              ("refresh", "Refresh Status", self._refresh_status),
                              ("diag", "Diagnosis", self._diagnose)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            cl.addWidget(b)
            self.ctl[key] = b
        cl.addStretch(1)
        self.conn_status = QLabel()
        cl.addWidget(self.conn_status)
        lay.addWidget(conn)

        mid = QHBoxLayout()
        gauge = QGroupBox("Power Levels")
        gl = QVBoxLayout(gauge)
        self.bars, self.bar_vals = {}, {}
        for key, name in (("batt", "Battery Level"), ("load", "UPS Load")):
            gl.addWidget(QLabel(name))
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setTextVisible(False)
            gl.addWidget(bar)
            v = QLabel()
            v.setAlignment(Qt.AlignCenter)
            gl.addWidget(v)
            self.bars[key], self.bar_vals[key] = bar, v
        gl.addStretch(1)
        mid.addWidget(gauge, 1)

        info = QGroupBox("Electrical Info")
        il = QGridLayout(info)
        self.info = {}
        for i, (label, key) in enumerate([("Input Voltage", "vin"), ("Output Voltage", "vout"),
                                          ("Frequency", "freq"), ("Current Status", "status")]):
            k = QLabel(f"{label}:")
            k.setStyleSheet("font-size: 13pt; font-weight: normal;")
            v = QLabel("n/a")
            v.setWordWrap(key == "status")
            il.addWidget(k, i, 0)
            il.addWidget(v, i, 1)
            self.info[key] = v
        il.setColumnStretch(1, 1)
        mid.addWidget(info, 2)

        outlet = QGroupBox("UPS Output")
        ol = QVBoxLayout(outlet)
        self.outlets = OutletView()
        ol.addWidget(self.outlets)
        ol.addStretch(1)
        mid.addWidget(outlet, 1)
        lay.addLayout(mid)

        graph = QGroupBox("UPS Real-time Trend")
        grl = QVBoxLayout(graph)
        self.fig = Figure(figsize=(10, 8), dpi=100, constrained_layout=True)
        self.axes = [self.fig.add_subplot(2, 2, i + 1) for i in range(4)]
        self.canvas = FigureCanvasQTAgg(self.fig)
        grl.addWidget(self.canvas, 1)
        lay.addWidget(graph, 1)

        shut = QPushButton("EXECUTE SYSTEM WIDE SHUTDOWN")
        shut.setStyleSheet("QPushButton { background: #dc3545; color: white; font-size: 12pt; font-weight: 700;"
                           " border: none; min-height: 34px; }"
                           "QPushButton:disabled { background: #e9a3aa; color: white; }")
        shut.clicked.connect(self._shutdown_all)
        self.ctl["shutdown"] = shut
        row = QHBoxLayout()
        row.addSpacing(100)
        row.addWidget(shut, 1)
        row.addSpacing(100)
        lay.addLayout(row)

        self._critical_box = None
        ACCESS.listeners.append(self._paint_access)
        if self.svc.port:
            self.port.setEditText(self.svc.port)
        if self.svc.connected:
            self._v1_connected(self.svc.port)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(60_000)   # read-only view: the Tk app logs one row per minute
        self.refresh()

    # ---------------------------------------------------------------- service events
    def _forward(self, ev, payload):
        self.bus.event.emit(ev, payload)    # RuntimeError once the page is deleted -> listener dropped

    def _on_event(self, ev, payload):
        if ev == "connected":
            self._v1_connected(payload)
        elif ev == "disconnected":
            self._v1_disconnected()
        elif ev == "owner":
            self.refresh()
        elif ev == "reading":
            self._show_live()
            if self.isVisible():
                self._plot(self.svc.history_snapshot())
            return
        elif ev == "outlets":
            self.outlets.set_states(payload)
            return
        elif ev == "dialog":
            kind, title, text = payload
            {"info": QMessageBox.information, "warning": QMessageBox.warning,
             "critical": QMessageBox.critical}[kind](self, title, text)
        elif ev == "critical":
            return                      # shown by the main window, whichever tab is open
        self._paint()

    def _critical(self, text):
        box = QMessageBox(QMessageBox.Warning, "UPS CRITICAL ALERT", text, QMessageBox.Ok, self)
        box.setAttribute(Qt.WA_DeleteOnClose)
        box.open()                      # not exec_: the poll keeps updating the page meanwhile
        self._critical_box = box

    def _v1_connected(self, port):
        self.port.setEditText(port or "")
        self.v1.update(combo=False, search=False, connect=True, refresh=True, change=True)
        self._paint()

    def _v1_disconnected(self):
        self.v1.update(combo=True, search=True, connect=True, refresh=False, change=False)
        self._paint()

    # ---------------------------------------------------------------- enable / disable
    def _paint_access(self, _on=None):
        self._paint()

    def _paint(self):
        owns, unlocked = self.svc.owns, ACCESS.unlocked
        for key, w in self.ctl.items():
            ok = self.v1[key] and owns and (unlocked or key not in GATED)
            w.setEnabled(ok)
            w.setToolTip("" if ok else TK_OWNS if not owns else "Unlock to use" if key in GATED and not unlocked
                         else "")
        self.ctl["connect"].setText("Disconnect UPS" if self.svc.connected else "Connect UPS")
        if owns:
            st = self.svc.conn_status
            color = (theme.C["good"] if st.startswith("Connected") else
                     theme.C["bad"] if st in ("UPS H/W Not Found", "No Response") else theme.C["text"])
            self.conn_status.setText(st)
            self.conn_status.setStyleSheet(f"color: {color}; font-weight: 700;")

    # ---------------------------------------------------------------- data
    def refresh(self):
        if self.svc.owns:
            self._show_live()
            self._plot(self.svc.history_snapshot())
        else:
            self._show_passive()
        self._paint()

    def _set_info(self, vin, vout, freq, status, level="normal"):
        color = {"warn": theme.C["warn"], "bad": theme.C["bad"]}.get(level, theme.C["text"])
        for key, text in (("vin", vin), ("vout", vout), ("freq", freq), ("status", status)):
            v = self.info[key]
            v.setText(text)
            size = "13pt" if key == "status" else "20pt"
            v.setStyleSheet(f"font-size: {size}; font-weight: 700; color: {color};")

    def _set_bars(self, batt, load):
        for key, val in (("batt", batt), ("load", load)):
            bar, lbl = self.bars[key], self.bar_vals[key]
            bar.setEnabled(val is not None)
            bar.setValue(int(val or 0))
            if val is None:
                lbl.setText("n/a (not logged)")
                lbl.setStyleSheet(f"color: {theme.C['hint']}; font-weight: normal;")
            else:
                lbl.setText(str(int(val)))
                lbl.setStyleSheet("font-size: 11pt; font-weight: 700;")

    def _show_live(self):
        r = self.svc.reading
        if r is None:            # Tk start values
            self._set_info("--- V", "--- V", "-- Hz", "Unknown")
            self._set_bars(0, 0)
        else:
            self._set_info(f"{r['vin']:.1f} V", f"{r['vout']:.1f} V", f"{r['freq']:.1f} Hz", r["status"], r["level"])
            self._set_bars(r["batt_pct"], int(r["load"]))
        self.outlets.set_states(self.svc.outlets)
        self.outlets.set_load(r["watt"] if r else None, r["load"] if r else None)

    def _show_passive(self):
        rows, path = _read_passive()
        if not rows:
            self.conn_status.setText("No UPS log found")
            self.conn_status.setStyleSheet(f"color: {theme.C['bad']}; font-weight: 700;")
            self._set_info("n/a", "n/a", "n/a", "n/a")
            self._set_bars(None, None)
            self.outlets.set_states([None] * 4)
            self.outlets.set_load(None)
            self._plot([])
            return
        age = time.time() - os.path.getmtime(path) if os.path.exists(path) else float("inf")
        fresh = age <= 180
        self.conn_status.setText("Logging" if fresh else f"No new log for {age / 60:.0f} min")
        self.conn_status.setStyleSheet(f"color: {theme.C['good'] if fresh else theme.C['bad']}; font-weight: 700;")
        _t, watt, _temp, vin, vout = rows[-1]
        self._set_info(f"{vin:.1f} V", f"{vout:.1f} V", "n/a", "n/a")   # frequency and status word are not logged
        self._set_bars(None, None)
        on = (1 if watt > 0 else 0) if fresh else None
        self.outlets.set_states([on] * 4)
        self.outlets.set_load(watt if fresh else None)
        self._plot(rows)

    def _plot(self, rows):
        step = max(1, len(rows) // 500)
        rs = rows[::step]
        times = [r[0] for r in rs]
        for ax, idx, title, color in zip(self.axes, (1, 2, 3, 4),
                                         ("Power (W)", "Internal Temp (C)", "Input Voltage (V)", "Output Voltage (V)"),
                                         ("red", "orange", "blue", "green")):
            ax.clear()
            ax.plot(times, [r[idx] for r in rs], color=color, linewidth=1.2)
            ax.set_title(title, fontsize=10, fontweight="bold")
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
            ax.xaxis.set_major_locator(mdates.AutoDateLocator())
            ax.grid(True, alpha=0.2)
            ax.tick_params(labelsize=8)
        self.canvas.draw_idle()

    def showEvent(self, e):
        super().showEvent(e)
        if self.svc.owns:
            self._plot(self.svc.history_snapshot())

    # ---------------------------------------------------------------- controls
    def _run(self, what, fn):
        def work():
            try:
                res = fn()
            except Exception as e:
                res = e
            self.bus.done.emit(what, res)
        threading.Thread(target=work, daemon=True).start()

    def _on_done(self, what, res):
        if what == "search":
            if isinstance(res, Exception):
                QMessageBox.warning(self, "Search Ports", str(res))
                return
            if res:
                self.port.clear()
                self.port.addItems(res)
                self.port.setCurrentIndex(0)
            self.v1.update(connect=True, refresh=True)
            self._paint()
        elif what == "shutdown":
            ok, err = res if not isinstance(res, Exception) else (False, str(res))
            if ok:
                QMessageBox.warning(self, "SHUTDOWN INITIATED",
                                    "UPS shutdown command sent (S.5).\n\n"
                                    "Power will cut in approximately 30 seconds.\n\n"
                                    "Save all work NOW!")
            else:
                QMessageBox.critical(self, "Shutdown Error", f"Failed to send shutdown command:\n{err}")

    def _search(self):
        self._run("search", self.svc.search_ports)

    def _toggle_connection(self):
        if self.svc.connected:
            self.svc.disconnect()
            return
        err = self.svc.connect_manual(self.port.currentText().strip())
        if err:
            QMessageBox.warning(self, "Warning", err)
        self._paint()

    def _refresh_status(self):
        self.svc.manual_refresh()

    def _diagnose(self):
        self._run("diag", self.svc.diagnose)

    def _change_port(self):
        pwd, ok = QInputDialog.getText(self, "Admin", "Enter Admin Password to change port:", QLineEdit.Password)
        if not ok:
            return
        if not self.svc.check_password(pwd):
            QMessageBox.critical(self, "Access Denied", "Incorrect password.")
            return
        self.svc.unlock_port()

    def _shutdown_all(self):
        """Tk shutdown_ups_all: connected check, admin password, final warning, 5 s countdown, S.5."""
        if not self.svc.connected:
            QMessageBox.warning(self, "Connection Error", "UPS is not connected via RS232C.")
            return
        pwd, ok = QInputDialog.getText(self, "Admin Password Required",
                                       "SYSTEM WIDE SHUTDOWN will cut power to ALL devices.\n\n"
                                       "Enter Admin Password to confirm:", QLineEdit.Password)
        if not ok:
            return
        if not self.svc.check_password(pwd):
            QMessageBox.critical(self, "Access Denied", "Incorrect password. Shutdown cancelled.")
            return
        ans = QMessageBox.warning(self, "FINAL WARNING",
                                  "SYSTEM WIDE SHUTDOWN\n\n"
                                  "All connected devices will lose power.\n"
                                  "This will send S.5 command: UPS output cuts in ~30 s.\n\n"
                                  "Are you ABSOLUTELY SURE?",
                                  QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ans != QMessageBox.Yes:
            return
        dlg = CountdownDialog(self)
        if dlg.exec_() != QDialog.Accepted:
            self.svc.cancel_shutdown()
            return
        self._run("shutdown", self.svc.send_shutdown)

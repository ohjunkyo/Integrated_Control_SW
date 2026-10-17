"""Laser Control tab, mirroring ui_manager._create_laser_control_tab: one sub-tab
per wavelength with the connection bar, Power Control / Current Settings /
Pulse Width / Trigger Control on the left (Historical Plot and Laser Session Log
below), Live Status and the 3-panel Real-time Monitoring plot on the right.

While the Tk app is closed v2 drives the boards itself (core.laser): live status,
Connect / LD / TEC / currents / pulse width / trigger, interlock recovery. While
the Tk app runs it owns the lasers and this tab shows its laser_data CSV logs.
Below the controls a date picker drives the Historical Plot (optionally all
wavelengths overlaid) and the LD ON History table.
"""
import csv
import glob
import os
import time
from datetime import datetime

import matplotlib.dates as mdates
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from PyQt5.QtCore import QDate, QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QDateEdit, QDialog, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
                             QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QSplitter, QTabBar, QTabWidget,
                             QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from app import theme
from core import env
from core import laser as L
from core.access import ACCESS, _admin_password
from widgets import basic
from widgets.basic import LAMP_ON

LAMP_IDLE = "#9aa0a6"

WAVELENGTHS = ["375nm", "405nm", "450nm", "473nm"]
LOG_DIR = os.path.join(env.ADC_DIR, "LOG", "LASER")
LIVE_ITEMS = [("LD Status", "ld"), ("TEC Status", "tec"), ("Temperature", "temp"), ("Pulse Width", "pw"),
              ("Bias (set)", "bias"), ("Pulse (set)", "pulse"), ("PD (actual, combined) [mA]", "pd")]


def _f(v, default=float("nan")):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def read_log(path):
    rows = []
    try:
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                if None in r.values():   # half-written last line
                    continue
                try:
                    r["_t"] = datetime.fromisoformat(r["timestamp"])
                except (KeyError, ValueError, TypeError):
                    continue
                rows.append(r)
    except OSError:
        pass
    return rows


def last_row(path):
    """Last complete row of a laser CSV, reading only the file's tail."""
    try:
        with open(path, "rb") as f:
            header = f.readline().decode().strip().split(",")
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 4096))
            lines = f.read().decode(errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines[1:]):
        vals = line.split(",")
        if len(vals) == len(header):
            return dict(zip(header, vals))
    return None


def todays_log(wl):
    path = os.path.join(LOG_DIR, f"laser_data_{wl}_{datetime.now():%Y%m%d}.csv")
    if os.path.exists(path):
        return path
    files = sorted(glob.glob(os.path.join(LOG_DIR, f"laser_data_{wl}_*.csv")))
    return files[-1] if files else None


class _Bus(QObject):
    event = pyqtSignal(str, object, object)


def controller():
    """The live laser controller, or None (tests without hardware)."""
    return None if env.NO_HW else L.laser()


class LaserTab(QWidget):
    def __init__(self, wl, page):
        super().__init__()
        self.wl, self.page = wl, page
        self.ctl = controller()
        self.controls = []          # enabled only while unlocked and v2 owns the lasers
        self.pw_unlocked = False
        lay = QVBoxLayout(self)

        bar = QFrame()
        bar.setFrameShape(QFrame.StyledPanel)
        bl = QHBoxLayout(bar)
        self.conn = QLabel("Disconnected")
        self.conn.setStyleSheet(f"font-size: 12pt; font-weight: 700; color: {theme.C['bad']};")
        bl.addWidget(self.conn)
        bl.addSpacing(20)
        for t, fn in (("Connect", self._connect), ("Disconnect", self._disconnect), ("Refresh", self.refresh)):
            b = QPushButton(t)
            b.clicked.connect(fn)
            if t != "Refresh":
                self.controls.append(b)
            bl.addWidget(b)
        bl.addStretch(1)
        lay.addWidget(bar)

        split = QSplitter(Qt.Horizontal)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)

        pwr = QGroupBox(f"Power Control ({wl})")
        pl = QHBoxLayout(pwr)
        for t, fn in (("LD ON", lambda: self._ld(True)), ("LD OFF", lambda: self._ld(False)),
                      ("TEC ON", lambda: self._tec(True)), ("TEC OFF", lambda: self._tec(False))):
            b = QPushButton(t)
            b.clicked.connect(fn)
            self.controls.append(b)
            pl.addWidget(b)
        ll.addWidget(pwr)

        cur = QGroupBox("Current Settings (mA)")
        cl = QGridLayout(cur)
        self.set_bias, self.set_pulse = QLineEdit("0.0"), QLineEdit("0.0")
        self.controls += [self.set_bias, self.set_pulse]
        for i, (t, e) in enumerate([("Bias:", self.set_bias), ("Pulse:", self.set_pulse)]):
            cl.addWidget(QLabel(t), i, 0)
            e.setMaximumWidth(110)
            cl.addWidget(e, i, 1, Qt.AlignLeft)
        b = QPushButton("Apply Currents")
        b.clicked.connect(self._apply_currents)
        self.controls.append(b)
        cl.addWidget(b, 2, 0, 1, 2)
        ll.addWidget(cur)

        pw = QGroupBox("Pulse Width (ps) / TEC Setpoint (°C)")
        pwv = QVBoxLayout(pw)
        pwl = QHBoxLayout()
        pwv.addLayout(pwl)
        self.pw_btn = QPushButton("Admin Unlock")
        self.pw_btn.clicked.connect(self._pw_unlock)
        pwl.addWidget(self.pw_btn)
        self.set_pw = QLineEdit()
        self.set_pw.setReadOnly(True)
        self.set_pw.setMaximumWidth(90)
        pwl.addWidget(self.set_pw)
        b = QPushButton("Default")
        b.clicked.connect(self._pw_default)
        pwl.addWidget(b)
        b2 = QPushButton("Apply")
        b2.clicked.connect(self._pw_apply)
        self.controls += [self.pw_btn, b, b2]
        pwl.addWidget(b2)
        pwl.addStretch(1)
        tcl = QHBoxLayout()
        pwv.addLayout(tcl)
        tcl.addWidget(QLabel("TEC"))
        self.set_tec = QLineEdit()
        self.set_tec.setReadOnly(True)
        self.set_tec.setMaximumWidth(90)
        tcl.addWidget(self.set_tec)
        b3 = QPushButton("Apply")
        b3.clicked.connect(self._tec_apply)
        self.controls.append(b3)
        tcl.addWidget(b3)
        tcl.addStretch(1)
        ll.addWidget(pw)


        trig = QGroupBox(f"Trigger Control ({wl})")
        tl = QHBoxLayout(trig)
        tl.addWidget(QLabel("Mode:"))
        self.mode = QComboBox()
        self.mode.addItems(["Internal (PG1)", "Internal (PG2)", "External"])
        self.mode.setCurrentText("External")
        self.mode.currentTextChanged.connect(self._mode_changed)
        tl.addWidget(self.mode)
        self.freq = QLineEdit("10000000")
        self.freq.setMinimumWidth(110)
        tl.addWidget(self.freq)
        self.freq_btn = QPushButton("Apply")
        self.freq_btn.clicked.connect(self._apply_trigger)
        tl.addWidget(self.freq_btn)
        self.controls += [self.mode, self.freq, self.freq_btn]
        self.mode_disp = QLabel("Current: --")
        self.mode_disp.setStyleSheet(f"font-weight: 700; color: {theme.C['bad']};")
        tl.addWidget(self.mode_disp)
        tl.addStretch(1)
        ll.addWidget(trig)

        # One date picker drives both tabs below
        hrow = QHBoxLayout()
        hrow.addWidget(QLabel("Date:"))
        self.hist_date = QDateEdit(QDate.currentDate())
        self.hist_date.setCalendarPopup(True)
        self.hist_date.setDisplayFormat("yyyy-MM-dd")
        self.hist_date.dateChanged.connect(lambda _d: self.load_history())
        hrow.addWidget(self.hist_date)
        self.overlay = QCheckBox("Overlay other wavelengths")
        self.overlay.toggled.connect(lambda _on: self.load_history())
        hrow.addWidget(self.overlay)
        hrow.addStretch(1)
        hist_btn = QPushButton("Load History")
        hist_btn.clicked.connect(self.load_history)
        hrow.addWidget(hist_btn)
        ll.addLayout(hrow)

        lnb = QTabWidget()
        hist = QWidget()
        hl = QVBoxLayout(hist)
        self.hist_label = QLabel()
        self.hist_label.setStyleSheet("font-weight: normal;")
        hl.addWidget(self.hist_label)
        self.fig_h = Figure(figsize=(4, 2.5), dpi=80, constrained_layout=True)
        self.canvas_h = FigureCanvasQTAgg(self.fig_h)
        hl.addWidget(self.canvas_h, 1)
        lnb.addTab(hist, "Historical Plot")
        self.ldon = QTreeWidget()
        self.ldon.setHeaderLabels(["Start", "End", "Duration", "Pulse (mA)"])
        self.ldon.setRootIsDecorated(False)
        for i, w in enumerate((230, 150, 90, 90)):
            self.ldon.setColumnWidth(i, w)
        lnb.addTab(self.ldon, "LD ON History")
        ll.addWidget(lnb, 1)
        split.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        live = QGroupBox("Live Status")
        gl = QGridLayout(live)
        self.live = {}
        for i, (t, key) in enumerate(LIVE_ITEMS):
            row, col = (i, 0) if i < 4 else (i - 4, 2)   # two columns: state | currents
            gl.addWidget(QLabel(f"{t}:"), row, col)
            v = QLabel("--")
            v.setFrameShape(QFrame.Panel)
            v.setFrameShadow(QFrame.Sunken)
            v.setMinimumWidth(130)
            v.setStyleSheet("font-weight: normal; padding: 1px 6px;")
            gl.addWidget(v, row, col + 1, Qt.AlignLeft)
            self.live[key] = v
        gl.setColumnStretch(1, 1)
        gl.setColumnStretch(3, 1)
        gl.setHorizontalSpacing(12)
        rl.addWidget(live)

        rt = QGroupBox(f"Real-time Monitoring ({wl})")
        rtl = QVBoxLayout(rt)
        self.fig = Figure(figsize=(6, 8), dpi=100, constrained_layout=True)
        self.ax_temp = self.fig.add_subplot(3, 1, 1)
        self.ax_curr = self.fig.add_subplot(3, 1, 2, sharex=self.ax_temp)
        self.ax_pd = self.fig.add_subplot(3, 1, 3, sharex=self.ax_temp)
        self.canvas = FigureCanvasQTAgg(self.fig)
        rtl.addWidget(self.canvas)
        rl.addWidget(rt, 1)
        split.addWidget(right)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 2)
        lay.addWidget(split, 1)

    # ---- live control (v2 owns the lasers) -----------------------------------
    def is_live(self):
        return self.ctl is not None and self.ctl.owns()

    def paint_access(self, on=None):
        on = ACCESS.unlocked if on is None else on
        enabled = on and self.is_live()
        for w in self.controls:
            w.setEnabled(enabled)
            w.setToolTip("" if enabled else ("Unlock to use" if self.is_live() else "The Tk app owns the lasers"))
        self.set_pw.setReadOnly(not (enabled and self.pw_unlocked))
        self.set_tec.setReadOnly(not (enabled and self.pw_unlocked))
        self._mode_changed(self.mode.currentText())

    def _warn(self, text):
        QMessageBox.warning(self, f"Laser {self.wl}", text)

    def _connect(self):
        ok, msg = self.ctl.connect(self.wl)
        if not ok:
            QMessageBox.critical(self, "Connection Error", f"Failed to connect {self.wl}: {msg}")
        self.refresh()

    def _disconnect(self):
        self._filled = False
        self.ctl.disconnect(self.wl)
        self.refresh()

    def _ld(self, state):
        if not self.ctl.connected(self.wl):
            return self._warn(f"{self.wl} is not connected.")
        others = self.ctl.lasers_on(except_wl=self.wl) if state else []
        if others and QMessageBox.question(
                self, "Safety Interlock",
                f"Laser {', '.join(others)} is currently ON.\n\nTo turn on {self.wl}, the others must be turned OFF."
                "\nProceed?") != QMessageBox.Yes:
            self.ctl.log(self.wl, f"[WARNING] Operation cancelled: {self.wl} ON blocked by user.")
            return
        self.ctl.set_ld(self.wl, state)

    def _tec(self, state):
        if not self.ctl.connected(self.wl):
            return self._warn(f"{self.wl} is not connected.")
        self.ctl.set_tec(self.wl, state)

    def _apply_currents(self):
        try:
            bias, pulse = float(self.set_bias.text()), float(self.set_pulse.text())
        except ValueError:
            return self._warn("Bias and Pulse must be numbers (mA).")
        ok, msg = self.ctl.apply_currents(self.wl, bias, pulse)
        if not ok:
            QMessageBox.critical(self, "LD Current Limit" if "COMBINED" in msg else f"Laser {self.wl}", msg)

    def _pw_unlock(self):
        pwd, ok = QInputDialog.getText(self, "Admin Unlock", "Enter Admin Password:", QLineEdit.Password)
        if not ok:
            return
        if pwd != _admin_password():
            QMessageBox.critical(self, "Admin Unlock", "Incorrect password.")
            return
        self.pw_unlocked = True
        self.pw_btn.setText("Unlocked")
        self.pw_btn.setEnabled(False)
        self.set_pw.setReadOnly(False)
        self.set_tec.setReadOnly(False)

    def _pw_relock(self):
        self.pw_unlocked = False
        self.pw_btn.setText("Admin Unlock")
        self.set_tec.setReadOnly(True)
        self.paint_access()

    def _tec_apply(self):
        if not self.pw_unlocked:
            return self._warn("Admin Unlock first.")
        raw = self.set_tec.text().strip()
        try:
            new = float(raw)
        except ValueError:
            return QMessageBox.critical(self, "TEC Setpoint", f"'{raw}' is not a temperature.")
        if QMessageBox.question(self, "Confirm TEC Setpoint Change",
                                f"Change the {self.wl} TEC setpoint to {new:.1f} C?") != QMessageBox.Yes:
            return
        ok, msg = self.ctl.apply_temp_setpoint(self.wl, new)
        if not ok:
            QMessageBox.critical(self, "TEC Setpoint", msg)
        self._pw_relock()

    def _pw_default(self):
        default = self.ctl.settings[self.wl].pulse_width_default_ps if self.ctl else None
        if default is None:
            QMessageBox.information(self, "Pulse Width",
                                    f"{self.wl}: original value not read yet -- reconnect or wait for the next status poll.")
            return
        self.set_pw.setText(str(default))

    def _pw_apply(self):
        raw = self.set_pw.text().strip()
        try:
            new = int(raw)
        except ValueError:
            return QMessageBox.critical(self, "Pulse Width", f"'{raw}' is not a whole number of picoseconds.")
        if not L.PULSE_WIDTH_MIN_PS <= new <= L.PULSE_WIDTH_MAX_PS:
            return QMessageBox.critical(self, "Pulse Width",
                                        f"{new} ps is out of the valid range ({L.PULSE_WIDTH_MIN_PS}-"
                                        f"{L.PULSE_WIDTH_MAX_PS} ps).")
        default = self.ctl.settings[self.wl].pulse_width_default_ps
        if QMessageBox.question(self, "Confirm Pulse Width Change",
                                f"Change {self.wl} pulse width to {new} ps?\n\n(currently running: "
                                f"{default if default is not None else 'unknown'} ps)\n\n"
                                "This changes the actual light pulse, not just its brightness.") != QMessageBox.Yes:
            return
        ok, msg = self.ctl.apply_pulse_width(self.wl, new)
        if not ok:
            QMessageBox.critical(self, "Pulse Width", msg)
        self._pw_relock()

    def _mode_changed(self, mode):
        internal = mode != "External"
        live_ok = ACCESS.unlocked and self.is_live()
        self.freq.setEnabled(live_ok and internal)
        self.freq_btn.setEnabled(live_ok and internal)
        if live_ok and mode == "External" and self.ctl.settings[self.wl].trigger_mode != "External":
            self.ctl.apply_trigger(self.wl, "External", self.freq.text())   # Tk applies a mode change at once

    def _apply_trigger(self):
        ok, msg = self.ctl.apply_trigger(self.wl, self.mode.currentText(), self.freq.text())
        if not ok:
            QMessageBox.critical(self, "Error", msg)

    def refresh(self):
        if self.is_live():
            return self._refresh_live()
        path = todays_log(self.wl)
        rows = read_log(path) if path else []
        fresh = bool(path) and time.time() - os.path.getmtime(path) <= 60
        self.conn.setText("Connected (logging)" if fresh else "Disconnected (no recent log)")
        self.conn.setStyleSheet(f"font-size: 12pt; font-weight: 700; color: {theme.C['good'] if fresh else theme.C['bad']};")
        if not rows:
            return
        r = rows[-1]
        ld, tec = r.get("ld_on") == "1", r.get("tec_on") == "1"
        on_off = {True: ("ON", "#28a745"), False: ("OFF", "#dc3545")}
        for key, st in (("ld", ld), ("tec", tec)):
            txt, col = on_off[st]
            self.live[key].setText(txt)
            self.live[key].setStyleSheet(f"font-weight: 700; padding: 1px 6px; color: {col};")
        self.live["temp"].setText(f"{_f(r.get('temp_c')):.1f} °C")
        self.live["bias"].setText(f"{_f(r.get('bias_ma')):.1f} mA")
        self.live["pulse"].setText(f"{_f(r.get('pulse_ma')):.1f} mA")
        self.live["pw"].setText(f"{r.get('pulse_width_ps') or '--'} ps")
        pd = _f(r.get("pd_current_ma"))
        self.live["pd"].setText("n/a" if pd != pd else f"{pd:.3f}")
        self.set_bias.setText(r.get("bias_ma", ""))
        self.set_pulse.setText(r.get("pulse_ma", ""))
        self.set_pw.setText(r.get("pulse_width_ps", ""))
        self.freq.setText(r.get("freq_hz", ""))
        mode = r.get("trigger_mode", "")
        if self.mode.findText(mode) < 0 and mode:
            self.mode.addItem(mode)
        self.mode.setCurrentText(mode)
        self.mode_disp.setText(f"Current: {mode or '--'}")
        self._plot_live(rows)

    def _refresh_live(self):
        c, wl = self.ctl, self.wl
        st = c.status(wl)
        con = c.connected(wl)
        fault = c.comm_error[wl]
        if fault:
            txt, col = ("USB DISCONNECTED", theme.C["bad"]) if c.reason[wl] == "USB" else ("INTERLOCK", "#fd7e14")
        else:
            txt, col = ("Connected", theme.C["good"]) if con else ("Disconnected", theme.C["bad"])
        self.conn.setText(txt)
        self.conn.setStyleSheet(f"font-size: 12pt; font-weight: 700; color: {col};")
        on_off = {True: ("ON", "#28a745"), False: ("OFF", "#dc3545")}
        if con and st:
            for key, flag in (("ld", st.get("ld_on", False)), ("tec", st.get("tec_on", False))):
                t, colr = on_off[flag]
                if key == "ld" and (st.get("alarm") or st.get("interlock")):
                    t, colr = "INTERLOCK", "#fd7e14"
                self.live[key].setText(t)
                self.live[key].setStyleSheet(f"font-weight: 700; padding: 1px 6px; color: {colr};")
            self.live["temp"].setText(f"{st.get('ld_temp', 0.0):.2f} °C")
            self.live["bias"].setText(f"{st.get('bias', 0.0):.2f} mA")
            self.live["pulse"].setText(f"{st.get('pulse', 0.0):.2f} mA")
            pw = c.settings[wl].pulse_width_default_ps
            self.live["pw"].setText(f"{pw} ps" if pw is not None else "-- ps")
            self.live["pd"].setText(f"{st.get('pd_current', 0.0) * 1000.0:.3f}" if st.get("pd_valid") else "n/a (no PD)")
            if pw is not None and not self.set_pw.text():
                self.set_pw.setText(str(pw))
            if not getattr(self, "_filled", False):   # entry boxes start from what the board runs
                self.set_bias.setText(f"{st.get('bias', 0.0):.1f}")
                self.set_pulse.setText(f"{st.get('pulse', 0.0):.1f}")
                self._filled = True
        else:
            for key in self.live:
                self.live[key].setText("--")
                self.live[key].setStyleSheet("font-weight: normal; padding: 1px 6px;")
        self.mode_disp.setText(f"Current: {c.settings[wl].trigger_mode}")
        h = c.history[wl]
        n = len(h["time"])
        if n > 1:
            start = max(0, n - 3600)   # last hour at 1 s
            cols = [list(h[k])[start:] for k in ("time", "temp", "pulse", "bias", "ld_on", "pd_current")]
            rows = [{"_t": t, "temp_c": te, "pulse_ma": pu, "bias_ma": bi, "ld_on": "1" if on else "0",
                     "pd_current_ma": pd * 1000.0} for t, te, pu, bi, on, pd in zip(*cols)]
            self._plot_live(rows)

    def _plot_live(self, rows):
        step = max(1, len(rows) // 1000)
        rs = rows[::step]
        t = [r["_t"] for r in rs]
        ld = [r.get("ld_on") == "1" for r in rs]
        nan = float("nan")

        def shade(ax):
            start = None
            for i, on in enumerate(ld):
                if on and start is None:
                    start = t[i]
                elif not on and start is not None:
                    ax.axvspan(start, t[i], color="#2ecc71", alpha=0.12, linewidth=0)
                    start = None
            if start is not None:
                ax.axvspan(start, t[-1], color="#2ecc71", alpha=0.12, linewidth=0)

        pulse = [_f(r.get("pulse_ma")) for r in rs]
        pd = [_f(r.get("pd_current_ma")) for r in rs]
        self.ax_temp.clear()
        self.ax_temp.plot(t, [_f(r.get("temp_c")) for r in rs], "r-", linewidth=1)
        self.ax_temp.set_ylabel("Temp (°C)", color="r")
        self.ax_curr.clear()
        self.ax_curr.plot(t, [p if not o else nan for p, o in zip(pulse, ld)], color="#b0b0b0", linewidth=1.0,
                          label="Pulse (LD off)")
        self.ax_curr.plot(t, [p if o else nan for p, o in zip(pulse, ld)], color="#2e7d32", linewidth=1.6,
                          label="Pulse (LD on)")
        self.ax_curr.plot(t, [_f(r.get("bias_ma"), 0.0) for r in rs], color="purple", linewidth=1, label="Bias")
        self.ax_curr.set_ylabel("Current (mA)", color="g")
        self.ax_curr.legend(loc="upper left", fontsize=8)
        self.ax_pd.clear()
        self.ax_pd.plot(t, [v if not o else nan for v, o in zip(pd, ld)], color="#b0b0b0", linewidth=1.0,
                        label="PD (LD off)")
        self.ax_pd.plot(t, [v if o else nan for v, o in zip(pd, ld)], color="#1565c0", linewidth=1.6,
                        label="PD (LD on)")
        self.ax_pd.set_ylabel("PD Current (mA)", color="#1565c0")
        self.ax_pd.legend(loc="upper left", fontsize=8)
        for ax in (self.ax_temp, self.ax_curr, self.ax_pd):
            ax.grid(True, alpha=0.3)
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
            shade(ax)
        for ax in (self.ax_temp, self.ax_curr):
            ax.tick_params(labelbottom=False)
        for lab in self.ax_pd.get_xticklabels():
            lab.set_rotation(30)
            lab.set_ha("right")
        self.canvas.draw_idle()

    def _log_for(self, wl, day):
        return os.path.join(LOG_DIR, f"laser_data_{wl}_{day}.csv")

    def load_history(self):
        """Plot the chosen day's log (optionally all wavelengths) and list its LD ON periods."""
        day = self.hist_date.date().toString("yyyyMMdd")
        wls = WAVELENGTHS if self.overlay.isChecked() else [self.wl]
        data = {wl: read_log(self._log_for(wl, day)) for wl in wls}
        own = data.get(self.wl, [])
        n = sum(len(v) for v in data.values())
        self.hist_label.setText(f"{self.hist_date.date().toString('yyyy-MM-dd')}: "
                                + (f"{n} rows" if n else "no laser log for this day"))
        self.fig_h.clf()
        ax1 = self.fig_h.add_subplot(2, 1, 1)
        ax2 = self.fig_h.add_subplot(2, 1, 2, sharex=ax1)
        for wl, rows in data.items():
            if not rows:
                continue
            t = [r["_t"] for r in rows]
            kw = {"label": wl, "lw": 1.6 if wl == self.wl else 1.0}
            ax1.plot(t, [_f(r.get("temp_c")) for r in rows], **kw)
            ax2.plot(t, [_f(r.get("pulse_ma")) for r in rows], **kw)
        ax1.set_ylabel("Temp (\u00b0C)")
        ax2.set_ylabel("Pulse (mA)")
        for ax in (ax1, ax2):
            ax.grid(True, alpha=0.3)
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        if len(wls) > 1 and n:
            ax1.legend(fontsize=8, loc="upper right", ncol=4)
        ax1.tick_params(labelbottom=False)
        self.canvas_h.draw_idle()
        self._fill_ldon(own)

    def _fill_ldon(self, rows):
        self.ldon.clear()
        spans, start = [], None
        for r in rows:
            on = r.get("ld_on") == "1"
            if on and start is None:
                start, pulses = r, []
            if on:
                pulses.append(_f(r.get("pulse_ma")))
                last = r
            elif start is not None:
                spans.append((start, r, pulses, False))
                start = None
        if start is not None:   # still on at the end of the log
            spans.append((start, last, pulses, True))
        items = []
        for s0, s1, pulses, open_end in spans:
            dur = int((s1["_t"] - s0["_t"]).total_seconds())
            p = [x for x in pulses if x == x]
            pul = "" if not p else (f"{p[0]:.1f}" if max(p) - min(p) < 0.05 else f"{min(p):.1f}-{max(p):.1f}")
            start_txt = s0["_t"].strftime("%Y-%m-%d %H:%M:%S")
            if s0 is rows[0]:   # already ON when this day's log began
                start_txt += " (on before)"
            items.append(QTreeWidgetItem([start_txt,
                                          "still ON" if open_end else s1["_t"].strftime("%Y-%m-%d %H:%M:%S"),
                                          f"{dur // 3600:d}:{dur % 3600 // 60:02d}:{dur % 60:02d}", pul]))
        if not items:
            items.append(QTreeWidgetItem(["(LD was not ON this day)", "", "", ""]))
        self.ldon.addTopLevelItems(items)


class LaserPage(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        self.sub = QTabWidget()
        self.tabs = []
        for wl in WAVELENGTHS:
            t = LaserTab(wl, self)
            self.tabs.append(t)
            self.sub.addTab(t, wl)
        self.tab_lamps = {}        # wl -> {"ld": lamp, "tec": lamp}, as the Tk tab marks [L:● T:●]
        for i, wl in enumerate(WAVELENGTHS):
            box = QWidget()
            h = QHBoxLayout(box)
            h.setContentsMargins(2, 0, 2, 0)
            h.setSpacing(3)
            lamps = {}
            for key, text in (("ld", "LD"), ("tec", "TEC")):
                lbl = QLabel(text)
                lbl.setStyleSheet("font-size: 8pt; font-weight: 700;")
                h.addWidget(lbl)
                lamps[key] = basic.make_lamp(9)
                h.addWidget(lamps[key])
            self.sub.tabBar().setTabButton(i, QTabBar.RightSide, box)
            self.tab_lamps[wl] = lamps
        self._csv_tick = 0
        self.bus = _Bus()
        self.bus.event.connect(self._on_event)
        self.ctl = controller()
        if self.ctl is not None:
            self.ctl.listeners.append(lambda ev, wl, p: self.bus.event.emit(ev, wl, p))
            for wl in [w for w in L.WAVELENGTHS if w in self.ctl._asking]:   # recovered before this page existed
                QTimer.singleShot(0, lambda wl=wl: self._recovery(wl, self.ctl.reason[wl]))
        ACCESS.listeners.append(self._paint_access)
        self._owner = None
        lay.addWidget(self.sub)
        self.sub.currentChanged.connect(lambda _i: self.refresh())
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        for t in self.tabs:
            t.load_history()
        self.refresh()

    def _paint_access(self, on=None):
        for t in self.tabs:
            t.paint_access(on)

    def _on_event(self, event, wl, payload):
        tab = next((t for t in self.tabs if t.wl == wl), None)
        if event == "recovered" and tab is not None:
            self._recovery(wl, payload)
        elif event in ("state", "fault") and tab is not None and tab is self.sub.currentWidget():
            tab.refresh()

    def _recovery(self, wl, reason):
        """Same three choices as the Tk app after a USB loss or interlock clears."""
        dlg = QDialog(self)
        usb = reason == "USB"
        dlg.setWindowTitle(f"USB Reconnected - {wl}" if usb else f"Interlock Recovery - {wl}")
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(f"[{wl}] USB connection restored.\nDo you want to re-enable control?" if usb else
                           f"[{wl}] Interlock release detected.\nDo you want to reconnect?"))
        row = QHBoxLayout()
        choice = {}
        for text, key in (("Normal Connect", "normal"), ("Force Connect (Admin)", "admin"), ("Cancel", "cancel")):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, k=key: (choice.update(k=k), dlg.accept()))
            row.addWidget(b)
        v.addLayout(row)
        dlg.exec_()
        k = choice.get("k", "cancel")
        if k == "admin":
            pwd, ok = QInputDialog.getText(self, "Admin", "Enter Admin Password:", QLineEdit.Password)
            if not ok:
                k = "cancel"
            elif pwd != L.FORCE_CONNECT_PASSWORD:
                QMessageBox.critical(self, "Error", "Incorrect password.")
                return self._recovery(wl, reason)
        if k == "cancel":
            self.ctl.cancel_recovery(wl)
            return
        keep = False
        was_on = self.ctl.status(wl).get("ld_on", False)
        if k == "admin" and was_on:
            keep = QMessageBox.question(self, "LD Status Alert (Admin)",
                                        f"[WARNING] Hardware LD for {wl} is currently ON!\n\nFor safety, do you want "
                                        "to turn OFF the laser?\n(Click No to keep it ON)") != QMessageBox.Yes
        self.ctl.post_reconnect(wl, admin=(k == "admin"), keep_on=keep)
        if k == "normal" and was_on:
            QMessageBox.information(self, "Safety Action",
                                    "Connected with normal privileges. Laser has been safely forced OFF.")

    def _tab_state(self, wl, live):
        """(ld, tec, alarm) for the tab lamps, or None when disconnected / faulted / no recent log."""
        if live:
            c = self.ctl
            if c.comm_error[wl] or not c.connected(wl):
                return None
            st = c.status(wl)
            return (bool(st.get("ld_on")), bool(st.get("tec_on")), bool(st.get("alarm") or st.get("interlock")))
        path = todays_log(wl)
        if not path or time.time() - os.path.getmtime(path) > 60:
            return None
        r = last_row(path)
        return (r.get("ld_on") == "1", r.get("tec_on") == "1", False) if r else None

    def _paint_tab_lamps(self, live):
        for wl, lamps in self.tab_lamps.items():
            try:
                state = self._tab_state(wl, live)
            except Exception:
                state = None
            if state is None:
                colors = ("#dc3545", "#dc3545")
            else:
                ld, tec, alarm = state
                colors = ("#fd7e14" if alarm else LAMP_ON if ld else LAMP_IDLE, LAMP_ON if tec else LAMP_IDLE)
            for key, color in zip(("ld", "tec"), colors):
                basic.paint_lamp(lamps[key], color)

    def refresh(self):
        owner = self.ctl is not None and self.ctl.owns()
        if owner != self._owner:           # ownership changed: re-evaluate every control
            self._owner = owner
            self._paint_access()
        self._csv_tick = (self._csv_tick + 1) % 5
        if owner or self._csv_tick == 1:   # log files only change every few seconds
            self._paint_tab_lamps(owner)
        if self.isVisible() or not getattr(self, "_drawn", False):
            self.sub.currentWidget().refresh()
            self._drawn = True

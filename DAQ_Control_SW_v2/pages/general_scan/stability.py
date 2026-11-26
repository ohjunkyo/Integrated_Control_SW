"""General Scan > Stability Run, mirroring stability_run.StabilityRunUI: run parameters,
duration / disk estimate, the count that fits in a time window, and Start / Stop (core/
stability.py: repeat config in the header, one manual Run DAQ launch, restore on the end).
The right side shows the newest stability run from Data/run_registry.csv."""
import csv
import os
import time
from datetime import datetime, timedelta

import matplotlib.dates as mdates
from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QBrush, QColor
from PyQt5.QtWidgets import (QSizePolicy, QApplication, QCheckBox, QComboBox, QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel,
                             QLineEdit, QMessageBox, QPushButton, QStackedWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                             QWidget)

from app import theme
from app.spinner import Spinner
from core import env, status
from core import stability as stab
from core.access import ACCESS
from core.live_scan import REGISTRY
from core.stability import RUN
from widgets.plot_zoom import PlotZoom as _Zoom
from widgets.record_layout import RecordHover


def stability_datasets():
    """Every stability run in the registry, newest first; each is its rows (one date, consecutive seq),
    oldest first."""
    try:
        rows = [r for r in csv.DictReader(open(REGISTRY, encoding="utf-8")) if r.get("kind") == "stability"]
    except OSError:
        return []
    rows.sort(key=lambda r: r.get("registered_at", ""))
    sets = []
    for r in rows:
        try:
            follows = sets and r["date"] == sets[-1][-1]["date"] and int(r["seq"]) == int(sets[-1][-1]["seq"]) + 1
        except (KeyError, ValueError):
            follows = False
        if follows:
            sets[-1].append(r)
        else:
            sets.append([r])
    return sets[::-1]


def latest_stability():
    """Rows of the newest stability run, oldest first."""
    sets = stability_datasets()
    return sets[0] if sets else []


class _Bus(QObject):
    changed = pyqtSignal()
    ext_loaded = pyqtSignal(object, list)
    rates_loaded = pyqtSignal(list)


_EXT_CACHE = {}   # FinalResult path -> (mtime, point)


def _dark_rates(path):
    """{ch: (kHz, 0)} from NoiseCountRate_ch<N> (dark pulses in the pre-trigger window) of a FinalResult file."""
    out = {}
    try:
        import uproot
        with uproot.open(path) as f:
            for ch in (0, 1, 2):
                try:
                    out[ch] = (float(f[f"NoiseCountRate_ch{ch}"].member("fVal")) / 1e3, 0.0)
                except Exception:
                    continue
    except Exception:
        pass
    return out


def _ext_points(rows):
    """Live Scan points (QE, Gain, ... per channel) of the finished runs of one stability dataset."""
    from core.live_scan import RESULT_DIR, LiveScanModel, registry
    m, reg = LiveScanModel(), registry()
    out = []
    for r in rows:
        if not r.get("status", "").startswith("done"):
            continue
        try:
            run = int(r["run"])
        except (KeyError, ValueError):
            continue
        path = os.path.join(RESULT_DIR, f"precal_result_kor_run_{r['date']}_{run:03d}.root")
        try:
            mt = os.path.getmtime(path)
        except OSError:
            continue
        hit = _EXT_CACHE.get(path)
        if hit is None or hit[0] != mt:
            pt = m._read_point(r["date"], run, path, reg)
            if pt is None:
                continue
            pt["data"]["rate"] = _dark_rates(path)
            _EXT_CACHE[path] = hit = (mt, pt)
        out.append(hit[1])
    return out


def _value(text="-"):
    lab = QLabel(text)
    lab.setObjectName("value")
    return lab


def _wrap(lab):
    """Long value lines wrap instead of widening the page."""
    lab.setWordWrap(True)
    lab.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
    lab.setMinimumWidth(120)
    return lab


class StabilityPage(QWidget):
    def __init__(self):
        super().__init__()
        self._own_params = None      # operator's (mode, interval, count) while a run's plan is shown
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 4, 12, 12)
        lay.setSpacing(12)
        t = QLabel("Stability Run")
        t.setObjectName("pageTitle")
        lay.addWidget(t)
        row1 = QHBoxLayout()
        row1.setSpacing(12)
        row2 = QHBoxLayout()
        row2.setSpacing(12)
        box = QGroupBox("Run parameters")
        g = QGridLayout(box)
        g.setVerticalSpacing(10)
        cfg = env.read_config()
        self.events = QLineEdit(cfg.get("Events", "").strip() or "300000")
        self.events.setReadOnly(True)      # the DAQ reads Events from config3.h (Global Settings)
        self._events_edited = False
        self.window_text = cfg.get("TimeWindow", "").strip() or str(stab.DEFAULT_TIME_WINDOW)
        self.interval = QLineEdit("600")
        self.count = QLineEdit("100")
        self._dark = stab.load_dark()
        self.mode = QComboBox()
        self.mode.addItems(["External trigger", "Self trigger"])
        self.mode.setCurrentIndex(1 if self._dark.get("mode") == "dark" else 0)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        g.addWidget(QLabel("Mode"), 0, 0)
        g.addWidget(self.mode, 0, 1, 1, 2)
        self.labels = []
        for r, (label, w, unit) in enumerate((("Events per acquisition", self.events, "events"),
                                              ("Interval between acquisitions", self.interval, "s"),
                                              ("Number of acquisitions", self.count, "count")), start=1):
            lab = QLabel(label)
            self.labels.append(lab)
            g.addWidget(lab, r, 0)
            w.setFixedWidth(100)
            w.textChanged.connect(self._recalc)
            g.addWidget(w, r, 1)
            u = QLabel(unit)
            u.setObjectName("key")
            g.addWidget(u, r, 2)
            if w is self.events:
                self.events_unit = u
        g.setColumnStretch(3, 1)
        ctrl = QHBoxLayout()
        self.start_btn = QPushButton("Start Stability Run")
        self.start_btn.clicked.connect(self._start)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self._stop)
        for b in (self.start_btn, self.stop_btn):
            b.setProperty("variant", "large")
            ctrl.addWidget(b)
        self.state = _wrap(QLabel())
        ctrl.addStretch(1)
        g.addLayout(ctrl, 4, 0, 1, 4)
        g.addWidget(self.state, 5, 0, 1, 4)     # own row: the run state is too long beside the buttons
        box.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        row1.addWidget(box, 1, Qt.AlignTop)

        self.seqbox = seqbox = QGroupBox("Laser Sequence")
        sl = QVBoxLayout(seqbox)
        sl.setSpacing(8)
        self.seq_note = QLabel("")
        self.seq_note.setStyleSheet(f"color: {theme.C['bad']};")
        sl.addWidget(self.seq_note)
        self.seq_stack = QStackedWidget()
        self.seq_box = QWidget()
        self.seq_grid = QGridLayout(self.seq_box)
        self.seq_grid.setContentsMargins(0, 0, 0, 0)
        self.seq_grid.setHorizontalSpacing(12)
        self.seq_grid.setVerticalSpacing(6)
        self.seq_stack.addWidget(self.seq_box)
        self.seq_stack.addWidget(self._build_dark())
        sl.addWidget(self.seq_stack)
        sl.addStretch(1)
        self.rows = {}
        row1.addWidget(seqbox, 1)

        calc = QGroupBox("Duration")
        c = QGridLayout(calc)
        c.setVerticalSpacing(8)
        self.l_per, self.l_total, self.l_end, self.l_disk = _value(), _value(), _value(), _value()
        for lab in (self.l_per, self.l_total, self.l_end, self.l_disk):
            _wrap(lab)
        for r, (name, lab) in enumerate((("Per acquisition", self.l_per), ("Total duration", self.l_total),
                                         ("Would finish at", self.l_end), ("Disk needed", self.l_disk))):
            k = QLabel(name)
            k.setObjectName("key")
            c.addWidget(k, r, 0)
            c.addWidget(lab, r, 1, 1, 4)
        k = QLabel("Available time")
        k.setObjectName("key")
        c.addWidget(k, 4, 0)
        self.hours = QLineEdit("12")
        self.hours.setFixedWidth(80)
        self.hours.textChanged.connect(self._recalc)
        c.addWidget(self.hours, 4, 1)
        u = QLabel("hours")
        u.setObjectName("key")
        c.addWidget(u, 4, 2)
        self.l_fit = _wrap(_value())
        c.addWidget(self.l_fit, 5, 0, 1, 3)
        self.use_btn = QPushButton("Use this count")
        self.use_btn.clicked.connect(self._use_fitted)
        c.addWidget(self.use_btn, 5, 3)
        c.setColumnStretch(4, 1)
        calc.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        left = QWidget()
        left_col = QVBoxLayout(left)
        left_col.setContentsMargins(0, 0, 0, 0)
        left_col.setSpacing(12)
        left_col.addWidget(calc)
        left_col.addStretch(1)
        row2.addWidget(left, 1)

        last = QGroupBox("Stability run (run registry)")
        self.grid = QGridLayout(last)
        self.grid.setVerticalSpacing(8)
        self.grid.setHorizontalSpacing(16)
        dk = QLabel("Dataset")
        dk.setObjectName("key")
        self.dataset = QComboBox()
        self.dataset.activated.connect(lambda _i: self._dataset_picked())
        self.grid.addWidget(dk, 0, 0)
        self.grid.addWidget(self.dataset, 0, 1, 1, 3)
        self._sets, self._shown_key = [], None
        self.vals = {}
        names = ("State", "Runs", "Progress", "Failed", "Wavelength / laser",
                 "Events", "Measured interval", "Started", "Last acquisition", "Date")
        for i, name in enumerate(names):
            r, half = i % 5 + 1, i // 5
            k = QLabel(name)
            k.setObjectName("key")
            v = _value()
            v.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)   # long values are cut, not widening the page
            self.grid.addWidget(k, r, half * 2)
            self.grid.addWidget(v, r, half * 2 + 1)
            self.vals[name] = v
        self.grid.setColumnStretch(1, 1)
        self.grid.setColumnStretch(3, 1)
        last.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self.reg_box = last
        left_col.addWidget(last)
        left_col.addStretch(1)
        self.right_stack = QStackedWidget()
        self.right_stack.addWidget(self._build_ext_box())
        self.right_stack.addWidget(self._build_rate_box())
        self.row2 = row2
        row2.addWidget(self.right_stack, 2)   # the plots get two thirds
        sections = QGridLayout()   # one grid for both rows, so their column edges line up
        sections.setHorizontalSpacing(12)
        sections.setVerticalSpacing(12)
        for r, row in enumerate((row1, row2)):
            for c in range(2):
                item = row.takeAt(0)
                sections.addWidget(item.widget(), r, c, item.alignment())
        sections.setColumnStretch(0, 2)
        sections.setColumnStretch(1, 3)
        lay.addLayout(sections)
        acq = QGroupBox("Acquisitions of this run")
        al = QVBoxLayout(acq)
        self.table = QTreeWidget()
        self.table.setHeaderLabels(["#", "Run", "Time", "Status", "Wavelength", "Laser [mA]", "HV1 / HV2 / HV3 [V]",
                                    "Events", "File"])
        self.table.setRootIsDecorated(False)
        self.table.setAlternatingRowColors(True)
        self.table.setUniformRowHeights(True)
        self.table.setMinimumHeight(300)
        for i, w in enumerate((50, 70, 170, 110, 110, 130, 170, 90)):
            self.table.setColumnWidth(i, w)
        al.addWidget(self.table)
        lay.addWidget(acq, 1)

        self._fitted = 0
        self.bus = _Bus()
        self.bus.changed.connect(self._paint)
        self.bus.ext_loaded.connect(self._draw_ext)
        self.bus.rates_loaded.connect(self._draw_rates)
        RUN.listeners.append(self.bus.changed.emit)
        ACCESS.listeners.append(self._paint_access)
        self._paint_access()
        self._sync_seq()
        self._laser_ic = (self.interval.text(), self.count.text())
        self._mode_changed()
        self.laser_timer = QTimer(self)
        self.laser_timer.timeout.connect(lambda: self._paint_lasers())
        self.laser_timer.start(2000)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(10000)
        self.refresh()

    # ---------------------------------------------------------------- state
    def _paint_access(self, on=None):
        self._paint()

    def _paint(self):
        on, running = ACCESS.unlocked, RUN.running
        dark = self._is_dark()
        multi = self._multi()
        self.mode.setEnabled(on and not running)
        for w in (self.events, self.hours, self.use_btn):
            w.setEnabled(on and not running and not dark)
        for w in (self.interval, self.count):
            w.setEnabled(on and not running and (dark or not multi))
        self._paint_lasers(on and not running)
        self._paint_dark(on and not running)
        ready = not dark or stab.dark_backend_ready()
        self.start_btn.setEnabled(on and not running and ready)
        self.stop_btn.setEnabled(running)
        for w in (self.start_btn, self.events, self.interval, self.count, self.hours, self.use_btn):
            w.setToolTip("" if on else "Unlock to use")
        text, st = RUN.status
        if not ready and not running:
            text = "Self trigger: waiting for the script_v7.sh update."
        self.state.setText(text)
        self.state.setStyleSheet(f"color: {theme.C['good'] if st == 'running' else theme.C['muted']};")
        self._show_running_plan(running)

    @staticmethod
    def _running_plan():
        """(dark, interval text, count text) of the run that is going, or None."""
        if not RUN.running:
            return None
        if not RUN.plan:
            dk = getattr(RUN, "dark", None)
            if dk is None or not getattr(dk, "cycles", None):
                return None
            return True, f"{float(dk.cycle_iv):g}", str(int(dk.cycles))
        _b, cnt, iv = RUN.plan[min(RUN.idx, len(RUN.plan) - 1)]
        return False, f"{float(iv):g}", str(int(cnt))

    def _set_quiet(self, w, text):
        if w.text() != text:
            w.blockSignals(True)
            w.setText(text)
            w.blockSignals(False)

    def _show_running_plan(self, running):
        """The running plan in the (disabled) Run parameters; the operator's values return when it ends."""
        plan = self._running_plan() if running else None
        if plan is not None:
            dark, iv, cnt = plan
            if self._own_params is None:
                self._own_params = (self.mode.currentIndex(), self.interval.text(), self.count.text())
            if dark != self._is_dark():
                self.mode.blockSignals(True)
                self.mode.setCurrentIndex(1 if dark else 0)
                self.mode.blockSignals(False)
                self.seq_stack.setCurrentIndex(1 if dark else 0)
                texts = (("Events per acquisition", "Cycle period (start to start)", "Number of cycles") if dark else
                         ("Events per acquisition", "Interval between acquisitions", "Number of acquisitions"))
                for lab, t in zip(self.labels, texts):
                    lab.setText(t)
            self._set_quiet(self.interval, iv)
            self._set_quiet(self.count, cnt)
            self._running_duration(dark)
        elif self._own_params is not None:
            mode, iv, cnt = self._own_params
            self._own_params = None
            if self.mode.currentIndex() != mode:
                self.mode.blockSignals(True)
                self.mode.setCurrentIndex(mode)
                self.mode.blockSignals(False)
                self._mode_changed()
            self._set_quiet(self.interval, iv)
            self._set_quiet(self.count, cnt)
            self._recalc()

    def _running_duration(self, dark):
        """Duration of the running plan, without saving anything to the panel's files."""
        try:
            if dark:
                dk = RUN.dark
                one = sum(stab.dark_step_seconds(s) + s[7] for s in dk.steps)
                total = stab.dark_total(dk.steps, dk.cycles, dk.cycle_iv)
                self.l_per.setText(f"{stab.fmt(one)} per cycle")
                self.l_total.setText(f"{stab.fmt(total)}   ({total / 3600:.2f} h)   {dk.cycles} cycles, "
                                     f"{dk.cycles * len(dk.steps)} runs")
                per = sum(stab.dark_step_bytes(s) for s in dk.steps)
                need, n = per * dk.cycles, dk.cycles
            else:
                ev = float(self.events.text())
                total = stab.plan_total(RUN.plan, ev)
                n = sum(c for _b, c, _i in RUN.plan)
                self.l_per.setText(f"{stab.fmt(stab.per_acquisition(ev))}")
                self.l_total.setText(f"{stab.fmt(total)}   ({total / 3600:.2f} h)   {n} acquisitions")
                per, need, _f = stab.disk(ev, n, stab.time_window(self.window_text))
            _p, _n, free = stab.disk(1, 1, 1)
            txt, color = f"~{stab.fmt_size(need)}  ({stab.fmt_size(per)} x {n})", theme.C["text"]
            if free is not None and need > free:
                txt, color = f"{txt}   -   only {stab.fmt_size(free)} free, WILL NOT FIT", theme.C["bad"]
            elif free is not None:
                txt, color = f"{txt}   -   {stab.fmt_size(free)} free", theme.C["good"]
            self.l_disk.setText(txt)
            self.l_disk.setStyleSheet(f"color: {color};")
            if RUN.started_at is not None:
                self.l_end.setText((RUN.started_at + timedelta(seconds=total)).strftime("%Y-%m-%d %H:%M:%S"))
            self.l_fit.setText("-")
        except (AttributeError, TypeError, ValueError):
            pass

    # ---------------------------------------------------------------- laser sequence
    def _paint_lasers(self, editable=None):
        """A wavelength whose laser is not connected cannot be selected."""
        from core.quick_setup import lasers_unavailable
        if editable is None:
            editable = ACCESS.unlocked and not RUN.running
        off = lasers_unavailable()
        for wl, row in self.rows.items():
            dead = wl in off
            row["name"].setText(f"{wl} (not connected)" if dead else wl)
            row["on"].setEnabled(editable)      # stays editable so a dead row can still be unticked
            for w in (row["bias"], row["pulse"], row["len"], row["unit"], row["iv"]):
                w.setEnabled(editable and not dead)

    def _multi(self):
        """Wavelength blocks run when any row is checked; otherwise the current laser, no switching."""
        return any(r["on"].isChecked() for r in self.rows.values())

    def _sync_seq(self):
        """The four wavelength rows, built once: On, Bias, Pulse (defaults from the General Scan Laser
        Sequence panel), Length as hours or a count, Interval. Values persist in stability_sequence.json."""
        if self.rows:
            return
        from core.scan import SEQ_ORDER
        saved = stab.load_sequence()
        defaults = stab.laser_panel_values()
        for c, h in enumerate(("On", "Wavelength", "Bias", "Pulse", "Length", "", "Interval", "Count", "Time")):
            k = QLabel(h)
            k.setObjectName("key")
            self.seq_grid.addWidget(k, 0, c)
        for r, wl in enumerate(SEQ_ORDER, start=1):
            v = saved.get(wl, {})
            d_bias, d_pulse = defaults.get(wl, ("", ""))
            row = {"on": QCheckBox(), "bias": QLineEdit(str(v.get("bias", d_bias))),
                   "pulse": QLineEdit(str(v.get("pulse", d_pulse))), "len": QLineEdit(str(v.get("len", "1"))),
                   "unit": QComboBox(), "iv": QLineEdit(str(v.get("iv", self.interval.text() or "600"))),
                   "l_cnt": _value(), "l_time": _value()}
            row["unit"].addItems(["h", "count"])
            row["unit"].setCurrentText(v.get("unit", "h"))
            row["on"].setChecked(bool(v.get("on", False)))
            for w in (row["bias"], row["pulse"], row["len"], row["iv"]):
                w.setFixedWidth(70)
                w.textChanged.connect(self._seq_changed)
            row["unit"].currentIndexChanged.connect(self._seq_changed)
            row["on"].toggled.connect(self._seq_changed)
            self.rows[wl] = row
            name = QLabel(wl)
            name.setObjectName("value")
            row["name"] = name
            self.seq_grid.addWidget(row["on"], r, 0)
            self.seq_grid.addWidget(name, r, 1)
            for c, key in ((2, "bias"), (3, "pulse"), (6, "iv")):
                cell = QWidget()
                hl = QHBoxLayout(cell)
                hl.setContentsMargins(0, 0, 0, 0)
                hl.addWidget(row[key])
                u = QLabel("s" if key == "iv" else "mA")
                u.setObjectName("key")
                hl.addWidget(u)
                self.seq_grid.addWidget(cell, r, c)
            self.seq_grid.addWidget(row["len"], r, 4)
            self.seq_grid.addWidget(row["unit"], r, 5)
            self.seq_grid.addWidget(row["l_cnt"], r, 7)
            self.seq_grid.addWidget(row["l_time"], r, 8)
        self.seq_grid.setColumnStretch(9, 1)
        self._recalc()
        self._paint()

    def _seq_changed(self, *_):
        self._paint()
        try:
            stab.save_sequence({wl: {"on": r["on"].isChecked(), "bias": r["bias"].text(), "pulse": r["pulse"].text(),
                                     "len": r["len"].text(), "unit": r["unit"].currentText(), "iv": r["iv"].text()}
                                for wl, r in self.rows.items()})
        except OSError:
            pass
        self._recalc()

    def _sequence(self):
        """[(wl, bias, pulse)] of the checked rows; None when a checked row's current is not a number."""
        seq = []
        for wl, r in self.rows.items():
            if not r["on"].isChecked():
                continue
            try:
                seq.append((wl, float(r["bias"].text()), float(r["pulse"].text())))
            except ValueError:
                return None
        return seq

    def _plan(self):
        """[(block, count, interval)]; block None = current laser only. None when a field is not usable."""
        n = self._nums()
        if not n:
            return None
        ev, _rate, iv, cnt = n
        if not self._multi():
            self.seq_note.setText("")
            return [(None, cnt, iv)]
        seq = self._sequence()
        if seq is None:
            self.seq_note.setText("A Bias or Pulse value is not a number")
            return None
        from core.laser import LD_TOTAL_CURRENT_LIMIT_MA
        plan = []
        for block in seq:
            if block[1] + block[2] > LD_TOTAL_CURRENT_LIMIT_MA:
                self.seq_note.setText(f"{block[0]}: Bias + Pulse exceeds {LD_TOTAL_CURRENT_LIMIT_MA:.0f} mA")
                return None
            row = self.rows[block[0]]
            try:
                length, biv = float(row["len"].text()), float(row["iv"].text())
            except ValueError:
                self.seq_note.setText(f"{block[0]}: Length or Interval is not a number")
                return None
            c = stab.block_count(length, row["unit"].currentText(), ev, biv) if length > 0 and biv >= 0 else 0
            if c <= 0:
                self.seq_note.setText(f"{block[0]}: not even one acquisition fits")
                return None
            plan.append((block, c, biv))
        self.seq_note.setText("")
        return plan

    # ---------------------------------------------------------------- estimate
    def _nums(self):
        count = self.count.text() if not self._multi() else "1"
        interval = self.interval.text() if not self._multi() else "0"
        return stab.nums(self.events.text(), interval, count)

    def _recalc(self, *_):
        if self._is_dark():
            self._recalc_dark()
            return
        n = self._nums()
        plan = self._plan()
        for row in self.rows.values():
            row["l_cnt"].setText("-")
            row["l_time"].setText("-")
        if not n or not plan:
            for lab in (self.l_per, self.l_total, self.l_end, self.l_fit, self.l_disk):
                lab.setText("-")
            return
        ev, rate, iv, cnt = n
        acq = stab.per_acquisition(ev)
        multi = plan[0][0] is not None
        for block, c, biv in plan:
            if block is not None:
                self.rows[block[0]]["l_cnt"].setText(str(c))
                self.rows[block[0]]["l_time"].setText(stab.fmt(stab.total_seconds(ev, biv, c)))
        n_acq = sum(c for _b, c, _i in plan)
        total = stab.plan_total(plan, ev)
        self.l_per.setText(f"{stab.fmt(acq)}   (acquisition {stab.fmt(ev / rate)} at {rate:.0f} Hz, "
                           f"{stab.rate_source()}, + overhead {int(stab.OVERHEAD_S)}s)")
        self.l_total.setText(f"{stab.fmt(total)}   ({total / 3600:.2f} h)"
                             + (f"   {len(plan)} wavelengths, {n_acq} acquisitions" if multi else ""))
        self.l_end.setText((datetime.now() + timedelta(seconds=total)).strftime("%Y-%m-%d %H:%M:%S"))
        per_run, need, free = stab.disk(ev, n_acq, stab.time_window(self.window_text))
        txt = f"~{stab.fmt_size(need)}  ({stab.fmt_size(per_run)} x {n_acq})"
        color = theme.C["text"]
        if free is not None and need > free:
            txt, color = f"{txt}   -   only {stab.fmt_size(free)} free, WILL NOT FIT", theme.C["bad"]
        elif free is not None and need > free * 0.8:
            txt, color = f"{txt}   -   {stab.fmt_size(free)} free, leaves little margin", theme.C["warn"]
        elif free is not None:
            txt, color = f"{txt}   -   {stab.fmt_size(free)} free", theme.C["good"]
        self.l_disk.setText(txt)
        self.l_disk.setStyleSheet(f"color: {color};")
        try:
            hours = float(self.hours.text())
        except ValueError:
            hours = 0
        if hours <= 0 or multi:
            self.l_fit.setText("-")
            self._fitted = 0
            return
        self._fitted = stab.fit_count(hours, ev, iv)
        if self._fitted <= 0:
            self.l_fit.setText(f"Not even one acquisition fits in {hours:g} h.")
        else:
            used = self._fitted * acq + max(0, self._fitted - 1) * iv
            self.l_fit.setText(f"{self._fitted} acquisitions fit in {hours:g} h (uses {stab.fmt(used)})")

    def _use_fitted(self):
        if self._fitted > 0:
            self.count.setText(str(self._fitted))

    # ---------------------------------------------------------------- run
    def _run_mode(self):
        left = getattr(self.window(), "left", None)
        try:
            _category, manual, fmt = left.run_mode()
            return manual, fmt
        except Exception:
            return "laser", "root"

    def _lasers_off_for_dark(self):
        """Dark needs every LD off (the Tk run_daq offers to switch them off). -> True to continue."""
        from core import quick_setup
        ctl = quick_setup.laser_controller()
        on = ctl.lasers_on() if ctl is not None else []
        if not on:
            return True
        if QMessageBox.question(self, "Laser ON - Dark DAQ Blocked",
                                f"Laser(s) currently ON: {', '.join(on)}\n\nDark mode requires the laser to be OFF."
                                "\n\nTurn laser(s) OFF now and continue?") != QMessageBox.Yes:
            stab._log("[WARNING] Dark DAQ cancelled: laser still ON.")
            return False
        for wl in on:
            ctl.set_ld(wl, False)
        t0 = time.time()
        while ctl.lasers_on() and time.time() - t0 < 5:
            QApplication.processEvents()
            time.sleep(0.1)
        if ctl.lasers_on():
            QMessageBox.critical(self, "Error", f"Failed to turn off laser(s): {', '.join(ctl.lasers_on())}")
            return False
        for wl in on:
            stab._log(f"[INFO] Dark DAQ: laser {wl} LD turned OFF.")
        return True

    def _start(self):
        from core import schedule, scripts
        from core.jobs import JOBS
        if RUN.running:
            return
        if not ACCESS.unlocked:
            QMessageBox.warning(self, "Locked", "Please unlock controls first.")
            return
        if schedule.general_scan_running():
            QMessageBox.warning(self, "General Scan running", "A General Scan is in progress. Stop it before "
                                "starting a Stability Run - both drive the same digitizer.")
            return
        if self._is_dark():
            self._start_dark()
            return
        n = self._nums()
        plan = self._plan()
        if not n or not plan:
            QMessageBox.critical(self, "Stability Run", "Check the run parameters - some field is empty or non-numeric."
                                 + ("\nLaser Sequence: check the wavelengths and each row's Length / Interval."
                                    if self._multi() else ""))
            return
        ev, _rate, iv, cnt = n
        seq = [b for b, _c, _i in plan if b is not None]
        from core.quick_setup import lasers_unavailable
        dead = [b[0] for b in seq if b[0] in lasers_unavailable()]
        if dead:
            QMessageBox.warning(self, "Laser not connected", f"{', '.join(dead)}: laser not connected.\n\n"
                                "Uncheck it in the Laser Sequence or connect the laser.")
            return
        n_acq = sum(c for _b, c, _i in plan)
        total = stab.plan_total(plan, ev)
        end = (datetime.now() + timedelta(seconds=total)).strftime("%Y-%m-%d %H:%M:%S")
        _per_run, need, free = stab.disk(ev, n_acq, stab.time_window(self.window_text))
        disk_line = f"Estimated disk:     ~{stab.fmt_size(need)}"
        if free is not None:
            disk_line += f"   ({stab.fmt_size(free)} free)"
        if free is not None and need > free and QMessageBox.warning(
                self, "Not enough disk space",
                f"This run needs about {stab.fmt_size(need)} but only {stab.fmt_size(free)} is free.\n\n"
                "It will fill the disk and the run will fail partway through.\n\nStart anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        cfg = env.read_config()
        try:
            rows, warn = stab.preflight_rows(cfg)
            cfg_text = "\nConfiguration (config3.h):\n" + "\n".join("  %-13s %s" % r for r in rows) + "\n"
            if warn:
                cfg_text += "\n".join("  CHECK: " + w for w in warn) + "\n"
        except Exception as e:
            cfg_text = "\nConfiguration: could not be read (%s)\n" % e
        try:
            hv_rows, hv_block, hv_warn = stab.check_hv(cfg)
        except Exception as e:
            hv_rows, hv_block, hv_warn = [("HV:", "unknown")], [], [f"HV state cannot be verified ({e})."]
        if hv_block:
            msg = "\n".join(hv_block)
            stab._log(f"[CRITICAL] Stability Run not started -- {msg}")
            QMessageBox.critical(self, "HV: No Current", msg + "\n\nThe Stability Run was not started.")
            return
        from bfield_preflight import coils_off_since
        off_since = coils_off_since()
        if off_since and QMessageBox.warning(
                self, "B-field Coils OFF", f"All B-field coil currents (I1-I4) are 0 A since {off_since}.\n\n"
                                           "Start the Stability Run anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        cfg_text += "\nHV (from HV Monitor):\n" + "\n".join("  %-13s %s" % r for r in hv_rows) + "\n"
        if hv_warn:
            cfg_text += "\n".join("  CHECK: " + w for w in hv_warn) + "\n"
        if seq:
            head = (f"Acquisitions of {int(ev):,} events, one wavelength after another "
                    "(the previous LD is switched OFF first):\n"
                    + "\n".join(f"  {b[0]}   Pulse {b[2]:g} mA   {c} x, {int(i)} s apart   "
                                 f"({stab.fmt(stab.total_seconds(ev, i, c))})" for b, c, i in plan) + "\n\n")
        else:
            head = f"{cnt} acquisitions of {int(ev):,} events, {int(iv)} s apart.\n\n"
        if QMessageBox.question(
                self, "Start Stability Run",
                f"{head}"
                f"Estimated duration: {stab.fmt(total)}\nEstimated finish:   {end}\n{disk_line}\n{cfg_text}\n"
                "The stage will NOT move - make sure the PMTs are already at the angle you want to measure.\n\n"
                "Start now?") != QMessageBox.Yes:
            return
        if stab.hk_backend():
            QMessageBox.warning(self, "Stability Run", "The HK Digitizer manual acquisition is not in v2 yet.")
            return
        if scripts.daq_running() or JOBS.running("daq"):
            QMessageBox.warning(self, "DAQ Already Running", "An instance of 'execute_DAQ' is already running.")
            return
        manual, fmt = self._run_mode()
        if manual == "dark" and seq:
            QMessageBox.warning(self, "Stability Run", "Laser Sequence needs the Laser manual type, not Dark.")
            return
        if manual == "dark" and not self._lasers_off_for_dark():
            return
        if manual != "dark" and not seq:
            from pages.laser_check import confirm_laser_config
            if not confirm_laser_config(self):
                return
        from pages.trigger_check import confirm_trigger_rate
        if not confirm_trigger_rate(self, stab.hk_backend()):
            return
        from core.stage import stage
        angles = {d: stage().read_now(d) for d in (2, 3)}
        angles = {d: a for d, a in angles.items() if a[0] is not None}
        ok, msg = RUN.start(env.config_file(), ev, plan, manual, fmt, angles)
        if not ok:
            QMessageBox.critical(self, "Stability Run", msg)
            return
        self._show_output(RUN.slot)

    def _show_output(self, slot):
        w = self.window()
        if hasattr(w, "show_page"):
            w.show_page("Output")
        from pages.daq import output
        if output.live() is not None:
            output.live().focus(slot)

    def _stop(self):
        if not RUN.running:
            return
        if QMessageBox.question(self, "Stop Stability Run", "Stop the current Stability Run?\n\nThe acquisition in "
                                "progress is terminated; data already written stays on disk.") != QMessageBox.Yes:
            return
        RUN.stop()

    # ---------------------------------------------------------------- dark mode
    def _is_dark(self):
        return self.mode.currentIndex() == 1

    def _build_dark(self):
        """Dark Sequence: one row per run of a cycle, run top to bottom; saved in dark_sequence.json."""
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)
        for c, h in enumerate(("On", "Channel", "Trigger", "Threshold", "Window", "Post-trigger", "Events", "Wait after",
                                "Run time")):
            k = QLabel(h)
            k.setObjectName("key")
            grid.addWidget(k, 0, c)
        self.dark_rows = {}
        for r, (key, label, trig, _ch) in enumerate(stab.DARK_ROWS, start=1):
            val = self._dark["rows"][key]
            row = {"on": QCheckBox(), "thr": QLineEdit(str(val["thr"])), "win": QLineEdit(str(val["win"])),
                   "pt": QLineEdit(str(val["pt"])), "ev": QLineEdit(str(val["ev"])), "iv": QLineEdit(str(val["iv"])),
                   "l_time": _value()}
            row["on"].setChecked(bool(val["on"]))
            row["on"].toggled.connect(self._dark_changed)
            name = QLabel(label)
            name.setObjectName("value")
            grid.addWidget(row["on"], r, 0)
            grid.addWidget(name, r, 1)
            grid.addWidget(QLabel(trig), r, 2)
            for c, k, unit, width in ((3, "thr", "mV", 52), (4, "win", "smp", 56), (5, "pt", "%", 44),
                                          (6, "ev", "", 74), (7, "iv", "s", 52)):
                row[k].setFixedWidth(width)
                row[k].textChanged.connect(self._dark_changed)
                cell = QWidget()
                hl = QHBoxLayout(cell)
                hl.setContentsMargins(0, 0, 0, 0)
                hl.addWidget(row[k])
                if unit:
                    u = QLabel(unit)
                    u.setObjectName("key")
                    hl.addWidget(u)
                grid.addWidget(cell, r, c)
                row["cell_" + k] = cell
            grid.addWidget(row["l_time"], r, 8)
            row["hover"] = RecordHover([row["cell_win"], row["cell_pt"]],
                                       lambda row=row, trig=trig: self._row_record(row, trig))
            self.dark_rows[key] = row
        grid.setColumnStretch(9, 1)
        v.addLayout(grid)
        pr = QHBoxLayout()
        k = QLabel("Preset")
        k.setObjectName("key")
        pr.addWidget(k)
        self.preset = QComboBox()
        self.preset.setMinimumWidth(180)
        pr.addWidget(self.preset)
        self.load_btn = QPushButton("Load")
        self.load_btn.clicked.connect(self._load_preset)
        self.save_btn = QPushButton("Save as...")
        self.save_btn.clicked.connect(self._save_preset)
        pr.addWidget(self.load_btn)
        pr.addWidget(self.save_btn)
        pr.addStretch(1)
        v.addLayout(pr)
        self._fill_presets()
        return w

    @staticmethod
    def _row_record(row, trig):
        try:
            return int(float(row["win"].text())), int(float(row["pt"].text())), "self" if trig == "Self" else "external"
        except ValueError:
            return None

    def _fill_presets(self):
        self.preset.clear()
        self.preset.addItems(sorted(stab.dark_presets()))

    def _dark_state(self):
        return {"mode": "dark" if self._is_dark() else "laser",
                "interval": self.interval.text(), "count": self.count.text(),
                "rows": {k: {"on": r["on"].isChecked(), "thr": r["thr"].text(), "win": r["win"].text(),
                             "pt": r["pt"].text(), "ev": r["ev"].text(), "iv": r["iv"].text()} for k, r in self.dark_rows.items()}}

    def _apply_dark_state(self, data):
        for k, r in self.dark_rows.items():
            val = data.get("rows", {}).get(k, {})
            r["on"].setChecked(bool(val.get("on", r["on"].isChecked())))
            for f in ("thr", "win", "pt", "ev", "iv"):
                if f in val:
                    r[f].setText(str(val[f]))
        if data.get("interval"):
            self.interval.setText(str(data["interval"]))
        if data.get("count"):
            self.count.setText(str(data["count"]))

    def _load_preset(self):
        data = stab.dark_presets().get(self.preset.currentText())
        if data:
            self._apply_dark_state(data)
            self._dark_changed()

    def _save_preset(self):
        name, ok = QInputDialog.getText(self, "Save Dark preset", "Preset name:", text=self.preset.currentText())
        name = name.strip()
        if not ok or not name:
            return
        try:
            stab.save_dark_preset(name, self._dark_state())
        except OSError as e:
            QMessageBox.critical(self, "Save Dark preset", f"Could not save: {e}")
            return
        self._fill_presets()
        self.preset.setCurrentText(name)

    def _dark_changed(self, *_):
        self._recalc()
        self._paint()

    def _mode_changed(self, *_):
        dark = self._is_dark()
        self.seq_stack.setCurrentIndex(1 if dark else 0)
        self.right_stack.setCurrentIndex(1 if dark else 0)
        self.reg_box.setVisible(not dark)
        self.rate_canvas.setMinimumHeight(380 if dark else 0)   # a hidden stack page still sets the minimum
        self.ext_canvas.setMinimumHeight(0 if dark else 560)
        self._ext_key = None
        self._shown_key = self._rate_key = None
        self.table.clear()
        self.table.setHeaderLabels(["#", "Run", "Time", "PMT", "Channel", "Dark [kHz]", "Pulses [kHz]", "Events", "File"]
                                   if dark else ["#", "Run", "Time", "Status", "Wavelength", "Laser [mA]",
                                                 "HV1 / HV2 / HV3 [V]", "Events", "File"])
        for w in (self.labels[0], self.events, self.events_unit):
            w.setVisible(not dark)
        self.seqbox.setTitle("Self-trigger Sequence" if dark else "Laser Sequence")
        texts = (("Events per acquisition", "Cycle period (start to start)", "Number of cycles") if dark else
                 ("Events per acquisition", "Interval between acquisitions", "Number of acquisitions"))
        for lab, t in zip(self.labels, texts):
            lab.setText(t)
        if dark:
            self._laser_ic = (self.interval.text(), self.count.text())
            iv, cnt = str(self._dark.get("interval") or "60"), str(self._dark.get("count") or "10")
            self.interval.setText(iv)
            self.count.setText(cnt)
        elif self.labels[1].property("dark"):
            self.interval.setText(self._laser_ic[0])
            self.count.setText(self._laser_ic[1])
        self.labels[1].setProperty("dark", dark)
        try:
            data = stab.load_dark()
            data["mode"] = "dark" if dark else "laser"
            stab.save_dark(data)
        except OSError:
            pass
        self.seq_note.setText("")
        self._recalc()
        self._paint()
        if hasattr(self, "timer"):
            self.refresh()

    def _paint_dark(self, editable):
        for r in self.dark_rows.values():
            r["on"].setEnabled(editable)
            for f in ("thr", "win", "pt", "ev", "iv"):
                r[f].setEnabled(editable and r["on"].isChecked())
        for b in (self.preset, self.load_btn, self.save_btn):
            b.setEnabled(editable)

    def _dark_plan(self):
        """(steps, cycles, cycle interval) or None, with the reason in the note line."""
        steps = stab.dark_steps(self._dark_state()["rows"])
        if steps is None:
            self.seq_note.setText("A Threshold, Window, Post-trigger, Events or Interval value is out of range")
            return None
        if not steps:
            self.seq_note.setText("No row is ticked")
            return None
        try:
            cyc, civ = int(float(self.count.text())), float(self.interval.text())
        except ValueError:
            self.seq_note.setText("Interval or Number of cycles is not a number")
            return None
        if cyc <= 0 or civ < 0:
            self.seq_note.setText("Interval or Number of cycles is out of range")
            return None
        self.seq_note.setText("")
        return steps, cyc, civ

    def _recalc_dark(self):
        self._dark = self._dark_state()
        try:
            stab.save_dark(self._dark)
        except OSError:
            pass
        for r in self.dark_rows.values():
            r["l_time"].setText("-")
        plan = self._dark_plan()
        self.l_fit.setText("-")
        self._fitted = 0
        if not plan:
            for lab in (self.l_per, self.l_total, self.l_end, self.l_disk):
                lab.setText("-")
            return
        steps, cyc, civ = plan
        for s in steps:
            self.dark_rows[s[0]]["l_time"].setText(stab.fmt(stab.dark_step_seconds(s)))
        one = sum(stab.dark_step_seconds(s) + s[7] for s in steps)
        total = stab.dark_total(steps, cyc, civ)
        self.l_per.setText(f"{stab.fmt(one)} per cycle   (self-trigger at ~{stab.DARK_RATE_HZ / 1000:.0f} kHz, "
                           f"external at {stab.rate_hz():.0f} Hz, {stab.rate_source()})")
        self.l_total.setText(f"{stab.fmt(total)}   ({total / 3600:.2f} h)   {cyc} cycles, {cyc * len(steps)} runs")
        self.l_end.setText((datetime.now() + timedelta(seconds=total)).strftime("%Y-%m-%d %H:%M:%S"))
        per_cycle = sum(stab.dark_step_bytes(s) for s in steps)
        need = per_cycle * cyc
        _p, _n, free = stab.disk(1, 1, 1)
        txt, color = f"~{stab.fmt_size(need)}  ({stab.fmt_size(per_cycle)} x {cyc})", theme.C["text"]
        if free is not None and need > free:
            txt, color = f"{txt}   -   only {stab.fmt_size(free)} free, WILL NOT FIT", theme.C["bad"]
        elif free is not None:
            txt, color = f"{txt}   -   {stab.fmt_size(free)} free", theme.C["good"]
        self.l_disk.setText(txt)
        self.l_disk.setStyleSheet(f"color: {color};")

    def _start_dark(self):
        from core import scripts
        from core.jobs import JOBS
        plan = self._dark_plan()
        if not plan:
            QMessageBox.critical(self, "Dark Stability Run", "Check the Dark Sequence: " + self.seq_note.text() + ".")
            return
        if not stab.dark_backend_ready():
            QMessageBox.warning(self, "Dark Stability Run", "script_v7.sh does not take the per-run dark settings yet.")
            return
        if stab.hk_backend():
            QMessageBox.warning(self, "Dark Stability Run", "Dark mode runs on the CAEN digitizer only.")
            return
        if scripts.daq_running() or JOBS.running("daq"):
            QMessageBox.warning(self, "DAQ Already Running", "An instance of 'execute_DAQ' is already running.")
            return
        steps, cyc, civ = plan
        total = stab.dark_total(steps, cyc, civ)
        end = (datetime.now() + timedelta(seconds=total)).strftime("%Y-%m-%d %H:%M:%S")
        try:
            hv_rows, hv_block, _hv_warn = stab.check_hv(env.read_config())
        except Exception as e:
            hv_rows, hv_block = [("HV:", f"unknown ({e})")], []
        if hv_block:
            QMessageBox.critical(self, "HV: No Current", "\n".join(hv_block) + "\n\nThe Dark Stability Run was not started.")
            return
        lines = "\n".join(f"  {s[1]:<20} {s[2]:<9} {s[4]:g} mV   {s[5]} samples   post {s[8]} %   {s[6]:,} events   then {s[7]:g} s"
                          for s in steps)
        if QMessageBox.question(
                self, "Start Dark Stability Run",
                f"One cycle, top to bottom:\n{lines}\n\n{cyc} cycles, one every {int(civ)} s (start to start).\n"
                f"Estimated duration: {stab.fmt(total)}\nEstimated finish:   {end}\n\n"
                "HV (from HV Monitor):\n" + "\n".join("  %-13s %s" % r for r in hv_rows) + "\n\n"
                "Every LD must stay OFF; the run stops if one is switched on.\n"
                "The function generator keeps running for the external-trigger run.\n\nStart now?") != QMessageBox.Yes:
            return
        if not self._lasers_off_for_dark():
            return
        from pages.trigger_check import confirm_trigger_rate
        if not confirm_trigger_rate(self, False):   # dark runs are CAEN only
            return
        from core.stage import stage
        angles = {d: stage().read_now(d) for d in (2, 3)}
        angles = {d: a for d, a in angles.items() if a[0] is not None}
        _manual, fmt = self._run_mode()
        ok, msg = RUN.dark.start(steps, cyc, civ, fmt, angles)
        if not ok:
            QMessageBox.critical(self, "Dark Stability Run", msg)
            return
        self._show_output(RUN.slot)

    # ---------------------------------------------------------------- external trigger: QE / Gain vs time
    EXT_METRICS = (("qe", "Relative QE [%]"), ("gain", "Gain [pC]"), ("rate", "Dark rate [kHz]"))

    def _build_ext_box(self):
        from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
        from matplotlib.figure import Figure
        box = QGroupBox("QE / Gain / Dark rate per PMT")
        v = QVBoxLayout(box)
        top = QHBoxLayout()
        self.ext_note = QLabel()
        self.ext_note.setObjectName("key")
        top.addWidget(self.ext_note)
        top.addStretch(1)
        auto, home = QPushButton("Auto Scale"), QPushButton("Reset View")
        top.addWidget(auto)
        top.addWidget(home)
        v.addLayout(top)
        self.ext_fig = Figure(figsize=(10, 5.8), dpi=100)
        self.ext_ax_qe = self.ext_fig.add_subplot(311)
        self.ext_ax_gain = self.ext_fig.add_subplot(312, sharex=self.ext_ax_qe)
        self.ext_ax_rate = self.ext_fig.add_subplot(313, sharex=self.ext_ax_qe)
        self.ext_fig.subplots_adjust(left=0.08, right=0.99, top=0.89, bottom=0.08, hspace=0.12)
        self.ext_canvas = FigureCanvasQTAgg(self.ext_fig)
        self.ext_canvas.setMinimumHeight(560)
        self.ext_canvas.mpl_connect("pick_event", self._on_ext_pick)
        self.ext_zoom = _Zoom(self.ext_canvas, self._clear_ext_callout)
        auto.clicked.connect(self.ext_zoom.autoscale)
        home.clicked.connect(self.ext_zoom.reset)
        v.addWidget(self.ext_canvas)
        self._ext_key, self._ext_points, self._ext_pick, self._ext_callout = None, [], {}, None
        self._ext_busy = False
        return box

    def _refresh_ext(self, rows):
        key = tuple((r.get("date"), r.get("run"), r.get("status")) for r in rows)
        if key == self._ext_key or self._ext_busy:
            return
        self._ext_key, self._ext_busy = key, True
        self.ext_note.setText("")
        if rows:
            if not hasattr(self, "ext_spin"):
                self.ext_spin = Spinner(self.ext_canvas)
            self.ext_spin.start()
        import threading

        def work():
            try:
                pts = _ext_points(rows)
            except Exception as e:
                from core import applog
                applog.log(f"[WARN] Stability plot: could not read the runs ({e})")
                pts = []
            self.bus.ext_loaded.emit(key, pts)
        threading.Thread(target=work, daemon=True).start()

    def _draw_ext(self, key, pts):
        self._ext_busy = False
        if hasattr(self, "ext_spin"):
            self.ext_spin.stop()
        if key != self._ext_key:
            self._ext_key = None   # the dataset changed meanwhile: read again on the next refresh
            return
        self._ext_points, self._ext_pick, self._ext_callout = pts, {}, None
        handles = []
        for ax, (mk, label) in zip((self.ext_ax_qe, self.ext_ax_gain, self.ext_ax_rate), self.EXT_METRICS):
            ax.clear()
            ax.grid(alpha=0.3)
            ax.set_ylabel(label)
            for ch in (0, 1, 2):
                rec = [p for p in pts if p["data"].get(mk, {}).get(ch)
                       and (mk != "gain" or p["data"][mk][ch][0] > 0)]   # gain -1 = failed fit
                if not rec:
                    continue
                marker, c = self.RATE_STYLE[ch]
                role = ("Monitor", "Rot1", "Rot2")[ch]
                cont = ax.errorbar([datetime.fromtimestamp(p["time"]) for p in rec],
                                   [p["data"][mk][ch][0] for p in rec],
                                   yerr=[p["data"][mk][ch][1] for p in rec] if mk != "rate" else None,
                                   fmt=marker, ms=5, capsize=2, color=c, linestyle="-", lw=0.8,
                                   markerfacecolor="none", label=f"{rec[0]['sn'].get(ch, f'ch{ch}')} ({role})",
                                   picker=4)
                self._ext_pick[cont.lines[0]] = rec
                if mk == "qe":
                    handles.append(cont)
        self.ext_ax_qe.tick_params(labelbottom=False)
        self.ext_ax_gain.tick_params(labelbottom=False)
        self.ext_ax_rate.set_xlabel("Time")
        self.ext_ax_rate.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        self.ext_fig.legends.clear()
        if handles:
            self.ext_ax_qe.legend(handles=handles, loc="lower left", bbox_to_anchor=(0.0, 1.02, 1.0, 0.1),
                                  mode="expand", borderaxespad=0, ncol=3, fontsize=8.5, framealpha=0.9)
        self.ext_note.setText(f"{len(pts)} run(s) with results" if pts else "no analysed runs yet")
        self.ext_zoom.set_home()
        self.ext_canvas.draw_idle()

    def _clear_ext_callout(self):
        if getattr(self, "_ext_callout", None) is not None:
            try:
                self._ext_callout.remove()
            except Exception:
                pass
            self._ext_callout = None
            self.ext_canvas.draw_idle()
        self._ext_last = None

    def _on_ext_pick(self, event):
        """Click a marker: run, time, QE and Gain of every PMT in that run; click it again to close."""
        rec = self._ext_pick.get(event.artist)
        if not rec or not len(event.ind):
            return
        if self.ext_zoom.picked:   # one popup per click, even when several markers lie under it
            return
        self.ext_zoom.picked = True
        idx = event.ind[0]
        if getattr(self, "_ext_last", None) == (event.artist, idx):
            self._clear_ext_callout()
            return
        self._ext_last = (event.artist, idx)
        p = rec[idx]
        lines = [f"run {p['run']:03d}   {datetime.fromtimestamp(p['time']):%m-%d %H:%M:%S}",
                 f"{'':12} {'QE [%]':<13} {'Gain [pC]':<13} Rate [kHz]"]
        for ch in (0, 1, 2):
            qe, gain = p["data"].get("qe", {}).get(ch), p["data"].get("gain", {}).get(ch)
            rate = p["data"].get("rate", {}).get(ch)
            if not (qe or gain or rate):
                continue
            role = ("Mon ", "Rot1", "Rot2")[ch]
            q = f"{qe[0]:.3f}±{qe[1]:.3f}" if qe else "-"
            g = f"{gain[0]:.3f}±{gain[1]:.3f}" if gain else "-"
            rt = f"{rate[0]:.2f}" if rate else "-"
            lines.append(f"{role} {p['sn'].get(ch, ''):<7} {q:<13} {g:<13} {rt}")
        if self._ext_callout is not None:
            try:
                self._ext_callout.remove()
            except Exception:
                pass
        ax = event.artist.axes
        x0, y0 = event.artist.get_data()[0][idx], event.artist.get_data()[1][idx]
        xnum = mdates.date2num(x0) if isinstance(x0, datetime) else x0
        xlo, xhi = ax.get_xlim()
        ylo, yhi = ax.get_ylim()
        right = xhi > xlo and (xnum - xlo) / (xhi - xlo) > 0.5
        top = yhi > ylo and (y0 - ylo) / (yhi - ylo) > 0.5
        self._ext_callout = ax.annotate(
            "\n".join(lines), xy=(x0, y0), xytext=(-14 if right else 14, -14 if top else 14),
            textcoords="offset points", fontsize=8, family="monospace", color="#e8ecf4",
            ha="right" if right else "left", va="top" if top else "bottom", multialignment="left", zorder=50,
            bbox=dict(boxstyle="round,pad=0.6,rounding_size=0.8", fc="#1b1f2a", ec="#3ddc84", lw=1.1, alpha=0.95),
            arrowprops=dict(arrowstyle="->", color="#3ddc84", lw=1.2))
        self.ext_canvas.draw_idle()

    # ---------------------------------------------------------------- self-trigger rate
    RATE_STYLE = {0: ("s", "#BA7517"), 1: ("o", "#378ADD"), 2: ("^", "#1D9E75")}   # as the Live Scan tab

    def _build_rate_box(self):
        from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
        from matplotlib.figure import Figure
        box = QGroupBox("Self-trigger rate")
        v = QVBoxLayout(box)
        top = QHBoxLayout()
        top.addStretch(1)
        auto, home = QPushButton("Auto Scale"), QPushButton("Reset View")
        top.addWidget(auto)
        top.addWidget(home)
        v.addLayout(top)
        self.rate_fig = Figure(figsize=(10, 4), dpi=100)
        self.rate_ax = self.rate_fig.add_subplot(111)
        self.rate_fig.subplots_adjust(left=0.07, right=0.99, top=0.82, bottom=0.13)
        self.rate_canvas = FigureCanvasQTAgg(self.rate_fig)
        self.rate_canvas.setMinimumHeight(380)
        self.rate_canvas.mpl_connect("pick_event", self._on_rate_pick)
        self.rate_zoom = _Zoom(self.rate_canvas, self._clear_rate_callout)
        auto.clicked.connect(self.rate_zoom.autoscale)
        home.clicked.connect(self.rate_zoom.reset)
        v.addWidget(self.rate_canvas)
        self._rate_key = None
        self._rate_busy = False
        self._rate_recs, self._rate_pick, self._rate_callout = [], {}, None
        return box

    CYCLE_GAP_S = 600   # runs of the other PMTs within this time belong to the clicked cycle

    def _clear_rate_callout(self):
        if getattr(self, "_rate_callout", None) is not None:
            try:
                self._rate_callout.remove()
            except Exception:
                pass
            self._rate_callout = None
            self.rate_canvas.draw_idle()
        self._rate_last = None

    def _on_rate_pick(self, event):
        """Click a marker: time, runs and the rates of every PMT in that cycle; click it again to close."""
        pts = self._rate_pick.get(event.artist)
        if not pts or not len(event.ind):
            return
        if self.rate_zoom.picked:   # one popup per click, even when several markers lie under it
            return
        self.rate_zoom.picked = True
        idx = event.ind[0]
        if getattr(self, "_rate_last", None) == (event.artist, idx):
            self._clear_rate_callout()
            return
        self._rate_last = (event.artist, idx)
        p = pts[idx]
        lines = [f"{p['time']:%m-%d %H:%M:%S}"]
        runs = []
        for ch in (0, 1, 2):
            near = [r for r in self._rate_recs if r["ch"] == ch
                    and abs((r["time"] - p["time"]).total_seconds()) <= self.CYCLE_GAP_S]
            if not near:
                continue
            r = min(near, key=lambda q: abs((q["time"] - p["time"]).total_seconds()))
            runs.append(r["run"])
            role = ("Mon ", "Rot1", "Rot2")[ch]
            clean = f"{r['clean'] / 1e3:6.2f}" if r.get("clean") else "     -"
            mark = ">" if r is p else " "
            lines.append(f"{mark}{role} {r['sn']:<7} dark {r['rate'] / 1e3:6.2f}  pulses {clean} kHz")
        lines.insert(1, "runs " + ", ".join(runs))
        if self._rate_callout is not None:
            try:
                self._rate_callout.remove()
            except Exception:
                pass
        ax = self.rate_ax
        x0, y0 = event.artist.get_data()[0][idx], event.artist.get_data()[1][idx]
        xnum = mdates.date2num(x0) if isinstance(x0, datetime) else x0
        xlo, xhi = ax.get_xlim()
        ylo, yhi = ax.get_ylim()
        right = xhi > xlo and (xnum - xlo) / (xhi - xlo) > 0.6
        top = yhi > ylo and (y0 - ylo) / (yhi - ylo) > 0.6
        self._rate_callout = ax.annotate(
            "\n".join(lines), xy=(x0, y0), xytext=(-14 if right else 14, -14 if top else 14),
            textcoords="offset points", fontsize=8, family="monospace", color="#e8ecf4",
            ha="right" if right else "left", va="top" if top else "bottom", multialignment="left", zorder=50,
            bbox=dict(boxstyle="round,pad=0.6,rounding_size=0.8", fc="#1b1f2a", ec="#3ddc84", lw=1.1, alpha=0.95),
            arrowprops=dict(arrowstyle="->", color="#3ddc84", lw=1.2))
        self.rate_canvas.draw_idle()

    def _refresh_rates(self):
        if self._rate_busy:
            return
        self._rate_busy = True
        if not hasattr(self, "rate_spin"):
            self.rate_spin = Spinner(self.rate_canvas)
        self.rate_spin.start()
        import threading

        def work():
            try:
                recs = stab.self_trigger_rates()
            except Exception:
                recs = []
            self.bus.rates_loaded.emit(recs)
        threading.Thread(target=work, daemon=True).start()   # RAW files are read from the USB HDD

    def _draw_rates(self, recs):
        self._rate_busy = False
        self.rate_spin.stop()
        if not self._is_dark():
            return
        key = tuple(r["run"] for r in recs)
        if key == self._rate_key:
            return
        self._rate_key = key
        ax = self.rate_ax
        ax.clear()
        ax.grid(alpha=0.3)
        handles = []
        self._rate_recs, self._rate_pick, self._rate_callout = recs, {}, None
        for ch in (0, 1, 2):
            pts = [r for r in recs if r["ch"] == ch]
            if not pts:
                continue
            mk, c = self.RATE_STYLE[ch]
            role = ("Monitor", "Rot1", "Rot2")[ch]
            rec = ax.errorbar([r["time"] for r in pts], [r["rate"] / 1e3 for r in pts],
                              yerr=[r["err"] / 1e3 for r in pts], fmt=mk, ms=5, capsize=2, color=c, linestyle=":",
                              lw=0.8, markerfacecolor="none", label=f"{pts[0]['sn']} ({role}) dark",
                              picker=4)
            self._rate_pick[rec.lines[0]] = pts
            cl = [r for r in pts if r.get("clean")]
            if cl:
                line, = ax.plot([r["time"] for r in cl], [r["clean"] / 1e3 for r in cl], mk, ms=4.5, color=c,
                                linestyle="-", lw=0.8, label=f"{pts[0]['sn']} ({role}) pulses >= thr", picker=4)
                handles.append(line)
                self._rate_pick[line] = cl
            handles.append(rec)
        if recs:
            import matplotlib.dates as mdates
            ys = [r["rate"] / 1e3 for r in recs] + [r["clean"] / 1e3 for r in recs if r.get("clean")]
            mid, half = (max(ys) + min(ys)) / 2, max((max(ys) - min(ys)) / 2 * 1.3, 0.05 * max(ys))
            ax.set_ylim(mid - half, mid + half * 1.25)
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
            ax.legend(handles=handles, loc="lower left", bbox_to_anchor=(0.0, 1.02, 1.0, 0.1), mode="expand",
                      borderaxespad=0, ncol=3, fontsize=8.5, framealpha=0.9)   # columns: Monitor, Rot1, Rot2
            self.rate_fig.suptitle(f"{recs[0]['time']:%Y%m%d}  ·  runs {recs[0]['run']}-{recs[-1]['run']}",
                                   fontsize=8, color="#555555", x=0.99, y=0.995, ha="right")
        ax.set_ylabel("Rate [kHz]")
        ax.set_xlabel("Time")
        self.rate_zoom.set_home()
        self.rate_canvas.draw_idle()
        self.table.clear()
        for i, r in enumerate(reversed(recs)):
            it = QTreeWidgetItem([str(len(recs) - i), r["run"], r["time"].strftime("%Y-%m-%d %H:%M:%S"), r["sn"],
                                  f"ch{r['ch']}", f"{r['rate'] / 1e3:.2f}",
                                  f"{r['clean'] / 1e3:.2f}" if r.get("clean") else "-", str(r["events"]),
                                  r["file"]])
            for c in range(8):
                it.setTextAlignment(c, Qt.AlignCenter)
            it.setForeground(3, QBrush(QColor(self.RATE_STYLE[r["ch"]][1])))
            self.table.addTopLevelItem(it)

    # ---------------------------------------------------------------- registry
    def _selected_rows(self):
        """Rows of the dataset picked in the box; the newest unless an older one was picked."""
        sets = stability_datasets()
        keys = [(s[0].get("date"), s[0].get("run")) for s in sets]
        old = [(s[0].get("date"), s[0].get("run")) for s in self._sets]
        pick = self.dataset.currentIndex()
        picked = old[pick] if 0 < pick < len(old) else None   # index 0 follows the newest run
        if keys != old:
            self._sets = sets
            self.dataset.blockSignals(True)
            self.dataset.clear()
            for i, s in enumerate(sets):
                self.dataset.addItem(f"{s[0].get('date', '?')}   runs {s[0].get('run')}-{s[-1].get('run')}   "
                                     f"{s[-1].get('wavelength', '?')} nm   ({len(s)} acq.)"
                                     + ("   newest" if i == 0 else ""))
            self.dataset.setCurrentIndex(keys.index(picked) if picked in keys else 0)
            self.dataset.blockSignals(False)
        i = self.dataset.currentIndex()
        return sets[i] if 0 <= i < len(sets) else []

    def _dataset_picked(self):
        self.refresh()

    def refresh(self):
        if not self._events_edited and not RUN.running:
            ev = env.read_config().get("Events", "").strip()
            if ev and ev != self.events.text():
                self.events.setText(ev)
        self._sync_seq()
        if not RUN.running:
            self._paint()
        if self._is_dark():
            self._refresh_rates()
            return
        rows = self._selected_rows()
        self._refresh_ext(rows)
        if not rows:
            self.vals["State"].setText("no stability run in the registry")
            return
        first, last = rows[0], rows[-1]
        key = (first.get("date"), first.get("run"))
        if self._shown_key != key:              # another dataset: rebuild the table
            self._shown_key = key
            self.table.clear()
        if self.table.topLevelItemCount() != len(rows):
            self.table.clear()
            for r in reversed(rows):   # newest first
                it = QTreeWidgetItem([r.get("seq", ""), r.get("run", ""), r.get("registered_at", "").replace("T", " "),
                                      r.get("status", ""), f"{r.get('wavelength', '?')} nm", r.get("laser_mA", ""),
                                      f"{r.get('hv1')} / {r.get('hv2')} / {r.get('hv3')}",
                                      r.get("events", ""), r.get("file", "")])
                for c in (0, 1, 3, 4, 5, 6, 7):
                    it.setTextAlignment(c, Qt.AlignCenter)
                if not r.get("status", "").startswith("done"):
                    for c in range(9):
                        it.setForeground(c, QBrush(QColor(theme.C["bad"])))
                self.table.addTopLevelItem(it)
        seq, nseq = int(last.get("seq") or 0), int(last.get("nseq") or 0)
        running = status.scan_running() and seq < nseq
        self.vals["State"].setText("RUNNING" if running else ("finished" if seq >= nseq else "stopped early"))
        self.vals["State"].setStyleSheet(f"color: {theme.C['good'] if running else theme.C['text']};")
        self.vals["Date"].setText(last["date"])
        self.vals["Runs"].setText(f"{first['run']} - {last['run']}")
        self.vals["Progress"].setText(f"{seq} / {nseq} acquisitions")
        failed = [r["run"] for r in rows if not r.get("status", "").startswith("done")]
        self.vals["Failed"].setText(f"{len(failed)}" + (f"  (runs {', '.join(failed[:8])}{' ...' if len(failed) > 8 else ''})"
                                                         if failed else ""))
        self.vals["Failed"].setStyleSheet(f"color: {theme.C['bad'] if failed else theme.C['text']};")
        self.vals["Wavelength / laser"].setText(f"{last.get('wavelength', '?')} nm / {last.get('laser_mA', '?')} mA")
        self.vals["Events"].setText(last.get("events", "?"))
        try:
            t = [datetime.fromisoformat(r["registered_at"]) for r in rows]
            gaps = sorted((b - a).total_seconds() for a, b in zip(t, t[1:]))
            self.vals["Measured interval"].setText(f"{gaps[len(gaps) // 2]:.0f} s (median)" if gaps else "-")
            self.vals["Started"].setText(t[0].strftime("%Y-%m-%d %H:%M:%S"))
            self.vals["Last acquisition"].setText(t[-1].strftime("%Y-%m-%d %H:%M:%S"))
        except (KeyError, ValueError):
            pass

"""General Scan > Stability Run, mirroring stability_run.StabilityRunUI: run parameters,
duration / disk estimate, the count that fits in a time window, and Start / Stop (core/
stability.py: repeat config in the header, one manual Run DAQ launch, restore on the end).
The right side shows the newest stability run from Data/run_registry.csv."""
import csv
import time
from datetime import datetime, timedelta

from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QBrush, QColor
from PyQt5.QtWidgets import (QApplication, QCheckBox, QComboBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                             QMessageBox, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from app import theme
from core import env, status
from core import stability as stab
from core.access import ACCESS
from core.live_scan import REGISTRY
from core.stability import RUN


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


def _value(text="-"):
    lab = QLabel(text)
    lab.setObjectName("value")
    return lab


class StabilityPage(QWidget):
    def __init__(self):
        super().__init__()
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
        for r, (label, w, unit) in enumerate((("Events per acquisition", self.events, "events"),
                                              ("Interval between acquisitions", self.interval, "s"),
                                              ("Number of acquisitions", self.count, "count"))):
            g.addWidget(QLabel(label), r, 0)
            w.setFixedWidth(120)
            w.textChanged.connect(self._recalc)
            g.addWidget(w, r, 1)
            u = QLabel(unit)
            u.setObjectName("key")
            g.addWidget(u, r, 2)
        g.setColumnStretch(3, 1)
        ctrl = QHBoxLayout()
        self.start_btn = QPushButton("Start Stability Run")
        self.start_btn.clicked.connect(self._start)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self._stop)
        for b in (self.start_btn, self.stop_btn):
            b.setProperty("variant", "large")
            ctrl.addWidget(b)
        self.state = QLabel()
        ctrl.addSpacing(16)
        ctrl.addWidget(self.state, 1)
        g.addLayout(ctrl, 3, 0, 1, 4)
        row1.addWidget(box, 1)

        seqbox = QGroupBox("Laser Sequence")
        sl = QVBoxLayout(seqbox)
        sl.setSpacing(8)
        self.seq_note = QLabel("")
        self.seq_note.setStyleSheet(f"color: {theme.C['bad']};")
        sl.addWidget(self.seq_note)
        self.seq_box = QWidget()
        self.seq_grid = QGridLayout(self.seq_box)
        self.seq_grid.setContentsMargins(0, 0, 0, 0)
        self.seq_grid.setHorizontalSpacing(12)
        self.seq_grid.setVerticalSpacing(6)
        sl.addWidget(self.seq_box)
        self.rows = {}
        row1.addWidget(seqbox, 1)

        calc = QGroupBox("Duration")
        c = QGridLayout(calc)
        c.setVerticalSpacing(8)
        self.l_per, self.l_total, self.l_end, self.l_disk = _value(), _value(), _value(), _value()
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
        self.l_fit = _value()
        c.addWidget(self.l_fit, 5, 0, 1, 3)
        self.use_btn = QPushButton("Use this count")
        self.use_btn.clicked.connect(self._use_fitted)
        c.addWidget(self.use_btn, 5, 3)
        c.setColumnStretch(4, 1)
        row2.addWidget(calc, 1)

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
            self.grid.addWidget(k, r, half * 2)
            self.grid.addWidget(v, r, half * 2 + 1)
            self.vals[name] = v
        self.grid.setColumnStretch(1, 1)
        self.grid.setColumnStretch(3, 1)
        row2.addWidget(last, 1)
        lay.addLayout(row1)
        lay.addLayout(row2)
        acq = QGroupBox("Acquisitions of this run")
        al = QVBoxLayout(acq)
        self.table = QTreeWidget()
        self.table.setHeaderLabels(["#", "Run", "Time", "Status", "Wavelength", "Laser [mA]", "HV1 / HV2 / HV3 [V]",
                                    "Events", "File"])
        self.table.setRootIsDecorated(False)
        self.table.setAlternatingRowColors(True)
        self.table.setUniformRowHeights(True)
        self.table.setMinimumHeight(200)
        for i, w in enumerate((50, 70, 170, 110, 110, 100, 170, 90)):
            self.table.setColumnWidth(i, w)
        al.addWidget(self.table)
        lay.addWidget(acq, 1)

        self._fitted = 0
        self.bus = _Bus()
        self.bus.changed.connect(self._paint)
        RUN.listeners.append(self.bus.changed.emit)
        ACCESS.listeners.append(self._paint_access)
        self._paint_access()
        self._sync_seq()
        self._recalc()
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
        multi = self._multi()
        for w in (self.events, self.hours, self.use_btn):
            w.setEnabled(on and not running)
        for w in (self.interval, self.count):
            w.setEnabled(on and not running and not multi)
        self._paint_lasers(on and not running)
        self.start_btn.setEnabled(on and not running)
        self.stop_btn.setEnabled(running)
        for w in (self.start_btn, self.events, self.interval, self.count, self.hours, self.use_btn):
            w.setToolTip("" if on else "Unlock to use")
        text, st = RUN.status
        self.state.setText(text)
        self.state.setStyleSheet(f"color: {theme.C['good'] if st == 'running' else theme.C['muted']};")

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
        self.l_per.setText(f"{stab.fmt(acq)}   (acquisition {stab.fmt(ev / rate)} at {rate:.0f} Hz "
                           f"+ overhead {int(stab.OVERHEAD_S)}s)")
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
        rows = self._selected_rows()
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

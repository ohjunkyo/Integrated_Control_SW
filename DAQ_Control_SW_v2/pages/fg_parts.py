"""Function Generator tab parts: one sub-tab per DT1081B section, the logic analyser, the device settings
and the change log. Forms are built from core.fg_schema; every write needs Unlock and a confirmation."""
import csv
import json
import os
import threading

from PyQt5.QtCore import QObject, Qt, pyqtSignal
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout,
                             QInputDialog, QLabel, QLineEdit, QListWidget, QMessageBox, QPlainTextEdit, QPushButton,
                             QScrollArea, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from app import theme
from core import fg_schema as S
from core import function_generator as FG
from core.access import ACCESS
from core.jobs import JOBS
from widgets import basic

WHO = "v2 GUI"
SECTIONS = "ABCD"
IN_STD = (["NIM", "TTL", "DISCRIMINATOR"], [0, 1, 2])
SUB_STD = (["NIM", "TTL"], [0, 1])
OUT_STD = (["NIM", "TTL"], [0, 1])
IMP = (["50 Ohm", "1 kOhm"], [True, False])
THRESH = {0: (-800, 2800, "mV"), 1: (0, 1800, "mV"), 2: (0, 65000, "lsb")}


class Runner(QObject):
    """Run a blocking call on a thread and hand (result, error) back to the GUI thread."""
    done = pyqtSignal(object, object, object)

    def __init__(self):
        super().__init__()
        self.done.connect(lambda cb, res, err: cb(res, err))

    def run(self, fn, cb):
        def work():
            try:
                self.done.emit(cb, fn(), None)
            except Exception as e:
                self.done.emit(cb, None, str(e))
        threading.Thread(target=work, daemon=True).start()


def combo(labels, values, current=None):
    c = QComboBox()
    for lab, val in zip(labels, values):
        c.addItem(lab, val)
    if current is not None:
        set_combo(c, current)
    return c


def set_combo(c, value):
    i = c.findData(value)
    if i >= 0:
        c.setCurrentIndex(i)


def confirm(parent, title, lines, section=None):
    """Yes/No with the change spelled out; warns when a scan is running (A = DAQ trigger, B = laser triggers)."""
    txt = "\n".join(lines)
    if JOBS.any_running():
        txt += "\n\nWARNING: a scan or stability run is in progress."
    if section in (0, 1):
        txt += f"\n\nSection {SECTIONS[section]} drives the {'DAQ trigger' if section == 0 else 'laser triggers'}."
    return QMessageBox.question(parent, title, txt + "\n\nApply?", QMessageBox.Yes | QMessageBox.No,
                                QMessageBox.No) == QMessageBox.Yes


def int_field(text, lo, hi, step, label):
    try:
        v = int(str(text).replace(",", "").strip())
    except ValueError:
        raise ValueError(f"{label}: enter an integer.")
    if not lo <= v <= hi:
        raise ValueError(f"{label}: must be between {lo:,} and {hi:,}.")
    if step > 1 and v % step:
        raise ValueError(f"{label}: must be a multiple of {step}.")
    return v


def fmt_int(v):
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return str(v)


class _Locked:
    """Mix-in: widgets in self.controls follow the Unlock state."""

    def _init_lock(self):
        self.controls = []
        ACCESS.listeners.append(self._paint_access)

    def _paint_access(self, unlocked):
        for w in self.controls:
            try:
                if isinstance(w, QCheckBox):   # keep the checked state readable while locked
                    w.setAttribute(Qt.WA_TransparentForMouseEvents, not unlocked)
                    w.setFocusPolicy(Qt.StrongFocus if unlocked else Qt.NoFocus)
                elif isinstance(w, (QLineEdit, QPlainTextEdit)):
                    w.setReadOnly(not unlocked or getattr(w, "_force_ro", False))
                else:
                    w.setEnabled(bool(unlocked))
            except RuntimeError:
                pass


# =========================================================================================
class SectionTab(QWidget, _Locked):
    def __init__(self, section, page):
        super().__init__()
        self.section, self.page = section, page
        self.runner = Runner()
        self.state = None          # last read_section() result
        self.dac = 0
        self.fn = None             # function the forms were built for
        self.tt_stream = None
        self.tt_events = []
        self._init_lock()

        lay = QHBoxLayout(self)
        split = QSplitter(Qt.Horizontal)
        lay.addWidget(split)

        # ---- left: function, I/O standards, channels
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)

        fbox = QGroupBox("Function")
        fl = QHBoxLayout(fbox)
        self.cur_fn = QLabel("-")
        self.cur_fn.setStyleSheet("font-size: 12pt; font-weight: 700;")
        fl.addWidget(self.cur_fn)
        fl.addSpacing(20)
        self.fn_combo = combo([v[0] for v in S.FUNCTIONS.values()], list(S.FUNCTIONS))
        fl.addWidget(self.fn_combo)
        b = QPushButton("Change Function")
        b.clicked.connect(self._change_function)
        fl.addWidget(b)
        self.controls += [self.fn_combo, b]
        fl.addStretch(1)
        ll.addWidget(fbox)

        io = QGroupBox("Input / Output Standard")
        g = QGridLayout(io)
        self.in_std_lbl = QLabel("Inputs")
        g.addWidget(self.in_std_lbl, 0, 0)
        self.in_std = combo(*IN_STD)
        self.in_std.currentIndexChanged.connect(self._thr_enable)
        g.addWidget(self.in_std, 0, 1)
        self.sub_lbl = QLabel("IN 3, IN 6")
        g.addWidget(self.sub_lbl, 0, 2)
        self.sub_std = combo(*SUB_STD)
        g.addWidget(self.sub_std, 0, 3)
        self.thr_lbl = QLabel("Threshold (mV)")
        g.addWidget(self.thr_lbl, 1, 0)
        self.thr = QLineEdit()
        self.thr.setFixedWidth(110)
        g.addWidget(self.thr, 1, 1)
        g.addWidget(QLabel("Input impedance"), 1, 2)
        self.in_imp = combo(*IMP)
        g.addWidget(self.in_imp, 1, 3)
        g.addWidget(QLabel("Outputs"), 2, 0)
        self.out_std = combo(*OUT_STD)
        g.addWidget(self.out_std, 2, 1)
        g.addWidget(QLabel("Output impedance"), 2, 2)
        self.out_imp = combo(*IMP)
        g.addWidget(self.out_imp, 2, 3)
        b = QPushButton("Apply I/O")
        b.clicked.connect(self._apply_io)
        g.addWidget(b, 3, 0, 1, 2)
        b2 = QPushButton("Reload")
        b2.clicked.connect(lambda: self.reload(force=True))
        g.addWidget(b2, 3, 2, 1, 2)
        self.controls += [self.in_std, self.sub_std, self.thr, self.in_imp, self.out_std, self.out_imp, b]
        ll.addWidget(io)

        ch = QGroupBox("Channels")
        cl = QVBoxLayout(ch)
        self.ch_table = QTableWidget(10, 9)
        self.ch_table.setHorizontalHeaderLabels(["Ch", "Role", "Used", "On", "G&D / Mono", "Gate / Mono (ns)",
                                                 "Delay (ns)", "Invert", ""])
        self.ch_table.verticalHeader().setVisible(False)
        self.ch_table.horizontalHeader().setStretchLastSection(True)
        self.ch_widgets = []
        for r in range(10):
            is_in = r < 6
            n = r + 1 if is_in else r - 5
            self.ch_table.setItem(r, 0, QTableWidgetItem(f"{'IN' if is_in else 'OUT'} {n}"))
            self.ch_table.setItem(r, 1, QTableWidgetItem(""))
            used = basic.make_lamp(11)
            self.ch_table.setCellWidget(r, 2, _center(used))
            st, ge, inv = QCheckBox(), QCheckBox(), QCheckBox()
            gv, dv = QLineEdit(), QLineEdit()
            gv.setFixedWidth(66)
            dv.setFixedWidth(66)
            ab = QPushButton("Apply")
            ab.setFixedWidth(80)
            ab.clicked.connect(lambda _c=False, row=r: self._apply_channel(row))
            self.ch_table.setCellWidget(r, 3, _center(st))
            self.ch_table.setCellWidget(r, 4, _center(ge))
            self.ch_table.setCellWidget(r, 5, gv)
            if is_in:
                self.ch_table.setCellWidget(r, 6, dv)
            self.ch_table.setCellWidget(r, 7, _center(inv))
            self.ch_table.setCellWidget(r, 8, ab)
            self.ch_widgets.append({"used": used, "status": st, "enable": ge, "gate": gv, "delay": dv, "invert": inv})
            self.controls += [st, ge, gv, dv, inv, ab]
        self.ch_table.resizeColumnsToContents()
        self.ch_table.horizontalHeader().setStretchLastSection(False)
        self.ch_table.setColumnWidth(1, 70)
        self.ch_table.setColumnWidth(8, 84)
        for r in range(10):
            self.ch_table.setRowHeight(r, 31)
        self.ch_table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.ch_table.setFixedHeight(self.ch_table.horizontalHeader().sizeHint().height() + 10 * 31 + 4)
        cl.addWidget(self.ch_table)
        rb = QPushButton("Read Channels")
        rb.clicked.connect(self._read_channels)
        cl.addWidget(rb)
        ll.addWidget(ch)
        ll.addStretch(1)
        split.addWidget(left)

        # ---- right: parameters, results / run controls
        right = QWidget()
        self.rl = QVBoxLayout(right)
        self.rl.setContentsMargins(0, 0, 0, 0)
        self.param_box = QGroupBox("Parameters")
        self.param_lay = QVBoxLayout(self.param_box)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.param_box)
        self.rl.addWidget(scroll, 3)
        self.res_box = QGroupBox("Results")
        self.res_lay = QVBoxLayout(self.res_box)
        self.rl.addWidget(self.res_box, 2)
        split.addWidget(right)
        split.setSizes([780, 560])
        self._paint_access(ACCESS.unlocked)

    # ---- reading --------------------------------------------------------------------
    def reload(self, force=False):
        fn = self.page.functions.get(self.section)
        if not fn:
            return
        self.runner.run(lambda: FG.read_section(self.section, fn), lambda r, e: self._loaded(r, e, force))

    def _loaded(self, st, err, force):
        if err:
            self.page.set_status(f"Section {SECTIONS[self.section]}: {err}", False)
            return
        self.state = st
        fn = st["function"]
        self.cur_fn.setText(S.display(fn))
        if force or fn != self.fn:
            set_combo(self.fn_combo, fn)
            self._fill_io(st)
            self._build_params(fn, st["config"])
            self._build_results(fn)
            self._read_channels()
            self.fn = fn
        self._roles(fn, st.get("ch_status", []))
        self._show_results(st)

    def poll(self):
        """Light refresh of live values (results, run state, channel use)."""
        fn = self.page.functions.get(self.section)
        if fn and fn != self.fn:
            self.reload(force=True)
        elif fn:
            self.runner.run(lambda: FG.read_section(self.section, fn), lambda r, e: self._loaded(r, e, False) if not e else None)

    def _roles(self, fn, ch_status):
        ins, outs = S.ROLES.get(fn, ({}, {}))
        for r in range(10):
            role = ins.get(r + 1, "") if r < 6 else outs.get(r - 5, "")
            self.ch_table.item(r, 1).setText(role)
            used = ch_status[r] if r < len(ch_status) else 0
            basic.paint_lamp(self.ch_widgets[r]["used"], basic.LAMP_ON if used else theme.C.get("hint", "#999"))

    def _fill_io(self, st):
        self.dac = self.page.device.get("dac", 0) or 0
        lo, hi, unit = THRESH.get(self.dac, THRESH[0])
        self.thr_lbl.setText(f"Threshold ({unit})")
        i, o = st.get("input", {}), st.get("output", {})
        set_combo(self.in_std, i.get("standard"))
        set_combo(self.sub_std, i.get("standard_sub"))
        self.thr.setText(str(i.get("threshold", "")))
        set_combo(self.in_imp, bool(i.get("imp", True)))
        set_combo(self.out_std, o.get("standard"))
        set_combo(self.out_imp, bool(o.get("imp", True)))
        special = st["function"] in S.SPECIAL_IO and self.dac != 1
        self.sub_lbl.setVisible(special)
        self.sub_std.setVisible(special)
        self.in_std_lbl.setText("IN 1, 2, 4, 5" if special else "Inputs")
        self._thr_enable()

    def _thr_enable(self):
        self.thr._force_ro = self.in_std.currentData() != 2
        self.thr.setReadOnly(self.thr._force_ro or not ACCESS.unlocked)

    def _read_channels(self):
        self.runner.run(lambda: FG.read_channels(self.section), self._channels_loaded)

    def _channels_loaded(self, res, err):
        if err:
            return
        ins, outs = res
        for r, c in enumerate(ins + outs):
            w = self.ch_widgets[r]
            w["status"].setChecked(bool(c.get("status")))
            w["enable"].setChecked(bool(c.get("enable_gd" if r < 6 else "enable_mono")))
            w["gate"].setText(str(c.get("gate" if r < 6 else "mono_value", "")))
            w["delay"].setText(str(c.get("delay", "")) if r < 6 else "")
            w["invert"].setChecked(bool(c.get("invert")))

    # ---- parameter form -------------------------------------------------------------
    def _clear(self, lay):
        while lay.count():
            it = lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
            elif it.layout():
                self._clear(it.layout())

    def _build_params(self, fn, cfg):
        self._clear(self.param_lay)
        self.form = {}
        spec = S.SPECS.get(fn, {})
        self.param_box.setTitle(f"Parameters - {S.display(fn)}")
        en, co = S.read_enables(fn, cfg)
        if spec.get("enables"):
            row = QHBoxLayout()
            self.form["enables"] = []
            for k, (lab, _i, _l) in enumerate(spec["enables"]):
                cb = QCheckBox(lab)
                cb.setChecked(en[k])
                row.addWidget(cb)
                self.form["enables"].append(cb)
                self.controls.append(cb)
            row.addStretch(1)
            self.param_lay.addLayout(row)
        if spec.get("coinc_flag"):
            row = QHBoxLayout()
            self.form["coinc"] = []
            for k, (lab, _i, _l) in enumerate(spec["enables"]):
                cb = QCheckBox(f"Coincidence {lab}")
                cb.setChecked(co[k])
                row.addWidget(cb)
                self.form["coinc"].append(cb)
                self.controls.append(cb)
            row.addStretch(1)
            self.param_lay.addLayout(row)
        fl = QFormLayout()
        if spec.get("bypass"):
            b = combo(S.BYPASS, list(range(5)), int(cfg.get("bypass_section", 0)) if cfg.get("bypass_enable") else 0)
            fl.addRow("Bypass section", b)
            self.form["bypass_section"] = ("enum", b)
            self.controls.append(b)
        for key, label, kind, extra in spec.get("fields", []):
            v = cfg.get(key)
            if kind == "bool":
                w = QCheckBox()
                w.setChecked(bool(v))
            elif kind == "enum":
                w = combo(extra[0], extra[1], v)
            else:
                w = QLineEdit(fmt_int(v) if v is not None else "")
                w.setFixedWidth(160)
            fl.addRow(label, w)
            self.form[key] = (kind, w, extra, label)
            self.controls.append(w)
        if spec.get("thresholds"):
            ths = cfg.get("thresholds") or []
            self.form["thresholds"] = []
            for k, lab in enumerate(["IN 1", "IN 2", "IN 4", "IN 5"]):
                w = QLineEdit(fmt_int(ths[k].get("threshold", 0)) if k < len(ths) else "0")
                w.setFixedWidth(160)
                fl.addRow(f"Threshold {lab} (Hz)", w)
                self.form["thresholds"].append(w)
                self.controls.append(w)
        if spec.get("windows"):
            self._windows_form(fl, fn, cfg, spec["windows"])
        if spec.get("lut"):
            self._lut_form(fl, cfg)
        if spec.get("pattern"):
            self._file_form(fl, cfg, "pattern", "Pattern file")
        self.param_lay.addLayout(fl)
        b = QPushButton("Apply Parameters")
        b.clicked.connect(self._apply_params)
        self.controls.append(b)
        self.param_lay.addWidget(b)
        self.param_lay.addStretch(1)
        self._paint_access(ACCESS.unlocked)

    def _windows_form(self, fl, fn, cfg, maxn):
        modes = (["Equal", "Custom file"], [0, 1]) if fn == "tof" else (["Equal"], [0])
        m = combo(*modes, cfg.get("win_mode", 0))
        fl.addRow("Window mode", m)
        wv = QLineEdit(fmt_int(cfg.get("win_value", 10)))
        wn = QLineEdit(fmt_int(cfg.get("win_number", 1)))
        fl.addRow("Window width (ns)", wv)
        fl.addRow(f"Number of windows (1-{maxn})", wn)
        self.form["_win"] = (m, wv, wn, maxn)
        self.controls += [m, wv, wn]
        if fn == "tof":
            self._file_form(fl, cfg, "width", "Window file")

    def _file_form(self, fl, cfg, kind, label):
        files = combo([], [])
        new = QPlainTextEdit()
        new.setMaximumHeight(110)
        new_name = QLineEdit()
        mode = combo(["Use saved file", "Create new file"], [0, 1], cfg.get("file_mode", 0))
        fl.addRow(f"{label} mode", mode)
        fl.addRow(f"Saved {label.lower()}", files)
        fl.addRow("New file name", new_name)
        fl.addRow("New file content", new)
        self.form["_file"] = (kind, mode, files, new_name, new, cfg.get("file_name"))
        self.controls += [mode, files, new_name, new]
        self.runner.run(lambda: FG.list_files(kind), lambda r, e: self._fill_files(files, r, cfg.get("file_name")))

    def _fill_files(self, box, names, current):
        if names is None:
            return
        box.clear()
        for n in names:
            box.addItem(n[:-5] if n.endswith(".json") else n, n[:-5] if n.endswith(".json") else n)
        if current:
            set_combo(box, current[:-5] if str(current).endswith(".json") else current)

    def _lut_form(self, fl, cfg):
        n = combo([str(i) for i in range(2, 7)], list(range(2, 7)), cfg.get("number_input", 2))
        fl.addRow("Inputs used", n)
        self.form["_lut_n"] = n
        self.controls.append(n)
        self._file_form(fl, cfg, "lut", "LUT file")

    def _collect(self, fn):
        spec = S.SPECS.get(fn, {})
        cfg = (self.state or {}).get("config", {})
        enables = [cb.isChecked() for cb in self.form.get("enables", [])]
        coinc = [cb.isChecked() for cb in self.form.get("coinc", [])]
        values = {}
        for key, item in self.form.items():
            if key.startswith("_") or key in ("enables", "coinc", "thresholds"):
                continue
            if key == "bypass_section":
                values[key] = item[1].currentData()
                continue
            kind, w, extra, label = item
            if kind == "bool":
                values[key] = w.isChecked()
            elif kind == "enum":
                values[key] = w.currentData()
            else:
                values[key] = int_field(w.text(), *extra, label)
        if "thresholds" in self.form:
            values["thresholds"] = [int_field(w.text(), 0, 100000000, 1, "Threshold") for w in self.form["thresholds"]]
        if fn == "pulse_generator" and values["period"] < values["width"] + 10:
            raise ValueError("Period must be at least width + 10 ns.")
        extra = {}
        if "_win" in self.form:
            m, wv, wn, maxn = self.form["_win"]
            extra["win_mode"] = m.currentData()
            if m.currentData() == 0:
                extra["win_value"] = int_field(wv.text(), 10, 1000000000, 10, "Window width")
                extra["win_number"] = int_field(wn.text(), 1, maxn, 1, "Number of windows")
        if "_file" in self.form and (fn != "tof" or extra.get("win_mode") == 1):
            kind, mode, files, new_name, new, _cur = self.form["_file"]
            fm = mode.currentData()
            extra["file_mode"] = fm
            if fm == 0:
                if not files.currentData():
                    raise ValueError("Select a saved file.")
                extra["file_name"] = files.currentData()
            else:
                import re
                name = new_name.text().strip()
                if not re.match(FG.NAME_RE, name):
                    raise ValueError("File name: letters, digits, - + _ only, fewer than 20 characters.")
                lines = [ln.strip() for ln in new.toPlainText().splitlines() if ln.strip()]
                extra["file_name"] = name
                if kind == "pattern":
                    if not lines or any(len(ln) != 4 or set(ln) - {"0", "1"} for ln in lines) or len(lines) > 2048:
                        raise ValueError("Pattern: 1-2048 lines of exactly four 0/1 characters.")
                    extra["pattern_values"] = [{"pattern": i, "value": int(ln, 2)} for i, ln in enumerate(lines)]
                    extra["total_number"] = len(lines)
                elif kind == "width":
                    vals = [int_field(ln, 10, 1000000000, 10, "Window width") for ln in lines]
                    if not vals or len(vals) > 2048:
                        raise ValueError("Window file: 1-2048 widths.")
                    extra["win_values"] = [{"window": i, "value": v} for i, v in enumerate(vals)]
                    extra["win_number"] = len(vals)
                elif kind == "lut":
                    n = self.form["_lut_n"].currentData()
                    if len(lines) != 2 ** n or any(len(ln) != 4 or set(ln) - {"0", "1"} for ln in lines):
                        raise ValueError(f"LUT: exactly {2 ** n} lines of four 0/1 characters.")
                    extra["lut_values"] = [{"input": i, "output": int(ln, 2)} for i, ln in enumerate(lines)]
        if fn == "lut":
            n = self.form["_lut_n"].currentData()
            extra.update({"lemo_out_enables": [{"lemo": i, "enable": True} for i in range(4)],
                          "lemo_in_enables": [{"lemo": i, "enable": bool(enables[i])} for i in range(6)],
                          "number_input": n, "total_number": 2 ** n})
            if extra.get("file_mode") == 0:
                extra.pop("lut_values", None)
        return S.to_params(fn, self.section, enables, coinc, values, extra)

    # ---- results --------------------------------------------------------------------
    def _build_results(self, fn):
        self._clear(self.res_lay)
        spec = S.SPECS.get(fn, {})
        self.res_table = None
        self.res_plot = None
        self.run_lamp = None
        row = QHBoxLayout()
        if fn in S.RUN_BUTTONS:
            self.run_lamp = basic.make_lamp(13)
            row.addWidget(QLabel("Running"))
            row.addWidget(self.run_lamp)
            row.addSpacing(10)
            for lab, ch in S.RUN_BUTTONS[fn]:
                b = QPushButton(lab)
                b.clicked.connect(lambda _c=False, ch=ch, lab=lab: self._reset(ch, lab))
                row.addWidget(b)
                self.controls.append(b)
        for lab, ch in spec.get("reset", []):
            b = QPushButton(lab)
            b.clicked.connect(lambda _c=False, ch=ch, lab=lab: self._reset(ch, lab))
            row.addWidget(b)
            self.controls.append(b)
        row.addStretch(1)
        if spec.get("timetag"):
            self._timetag_controls(row)
        self.res_lay.addLayout(row)
        res = spec.get("results")
        if res and res[0] == "spectrum":
            from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
            from matplotlib.figure import Figure
            self.res_fig = Figure(figsize=(5, 2.6), tight_layout=True)
            self.res_plot = FigureCanvasQTAgg(self.res_fig)
            self.res_lay.addWidget(self.res_plot, 1)
        elif res:
            cols = {"counters": ["Value"], "rates": ["Rate (Hz)"], "rates_alarm": ["Rate (Hz)", "Alarm"],
                    "counter_timer": ["Value", "Target", "Mode"], "chronom": ["Time (ns)", "Running"]}[res[0]]
            self.res_table = QTableWidget(len(res[1]), len(cols))
            self.res_table.setHorizontalHeaderLabels(cols)
            self.res_table.setVerticalHeaderLabels(res[1])
            self.res_table.horizontalHeader().setStretchLastSection(True)
            self.res_lay.addWidget(self.res_table, 1)
        elif not spec.get("timetag"):
            self.res_lay.addWidget(QLabel("-"))
        self.res_lay.addStretch(0)
        self._paint_access(ACCESS.unlocked)

    def _show_results(self, st):
        fn = st["function"]
        if self.run_lamp is not None and "running" in st:
            basic.set_lamp(self.run_lamp, st.get("running") == 1)
        res = S.SPECS.get(fn, {}).get("results")
        data = st.get("results") or {}
        if not res or not data:
            return
        kind = res[0]
        if kind == "spectrum" and self.res_plot is not None:
            ax = (self.res_fig.axes or [self.res_fig.add_subplot(111)])[0]
            ax.clear()
            t = data.get("time") or []
            edges = []
            acc = 0
            for w in t:
                edges.append(acc)
                acc += w
            for k, lab in enumerate(res[1]):
                y = data.get(f"spectrum{k + 1}") or []
                if y:
                    x = edges[:len(y)] if len(edges) >= len(y) else list(range(len(y)))
                    ax.step(x, y, where="post", label=lab)
            ax.set_xlabel("Time (ns)" if edges else "Window")
            ax.set_ylabel("Counts")
            ax.legend(fontsize=8)
            self.res_plot.draw_idle()
            return
        if self.res_table is None:
            return
        cnt = data.get("counters") or []
        for r in range(self.res_table.rowCount()):
            c = cnt[r] if r < len(cnt) else {}
            if kind in ("counters", "rates"):
                vals = [c.get("value", "")]
            elif kind == "rates_alarm":
                v = c.get("value", "")
                vals = [f"{v:,.3f}" if isinstance(v, (int, float)) else v, "ALARM" if c.get("alarm") else ""]
            elif kind == "counter_timer":
                vals = [c.get("value", ""), c.get("target_value", ""), "FCTW"[c.get("target_type", 0)] if isinstance(c.get("target_type"), int) else ""]
            else:   # chronom: value * 10 ns
                v = c.get("value")
                vals = [fmt_int(v * 10) if isinstance(v, (int, float)) else "", "yes" if c.get("status") == 1 else ""]
            for k, v in enumerate(vals):
                self.res_table.setItem(r, k, QTableWidgetItem(fmt_int(v) if isinstance(v, int) else str(v)))

    # ---- time tagging stream ---------------------------------------------------------
    def _timetag_controls(self, row):
        self.tt_start = QPushButton("Start Tagging")
        self.tt_stop = QPushButton("Stop")
        self.tt_save = QPushButton("Save CSV")
        self.tt_count = QLabel("0 events")
        self.tt_start.clicked.connect(self._tt_start)
        self.tt_stop.clicked.connect(self._tt_stop)
        self.tt_save.clicked.connect(self._tt_save)
        for w in (self.tt_start, self.tt_stop, self.tt_save, self.tt_count):
            row.addWidget(w)
        self.controls += [self.tt_start, self.tt_stop]
        self.tt_view = QPlainTextEdit()
        self.tt_view.setReadOnly(True)
        self.tt_view.setMaximumBlockCount(500)
        self.res_lay.addWidget(self.tt_view, 1)

    class _TT(QObject):
        events = pyqtSignal(object)
        error = pyqtSignal(str)

    def _tt_start(self):
        if not ACCESS.unlocked or self.tt_stream is not None:
            return
        if not confirm(self, "Time Tagging", [f"Start time tagging on section {SECTIONS[self.section]}."], self.section):
            return
        self._tt_sig = self._TT()
        self._tt_sig.events.connect(self._tt_add)
        self._tt_sig.error.connect(lambda m: (self.page.set_status(f"time tag: {m}", False), self._tt_ended()))
        self.tt_events = []
        self.tt_stream = FG.TimeTagStream(self.section, self._tt_sig.events.emit, self._tt_sig.error.emit)
        self.tt_stream.start(WHO)

    def _tt_add(self, evs):
        for e in evs:
            s = json.dumps(e).strip("[]").replace(",", " ", 1)
            self.tt_events.append(s)
            self.tt_view.appendPlainText(s)
        self.tt_count.setText(f"{len(self.tt_events):,} events")

    def _tt_stop(self):
        if self.tt_stream is not None:
            self.tt_stream.stop()
        self._tt_ended()

    def _tt_ended(self):
        self.tt_stream = None

    def _tt_save(self):
        if not self.tt_events:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save time tags", os.path.join(FG.LOG_DIR, "timetag.csv"), "CSV (*.csv)")
        if path:
            with open(path, "w", newline="") as f:
                w = csv.writer(f)
                for e in self.tt_events:
                    w.writerow(e.split())

    # ---- writes ---------------------------------------------------------------------
    def _guard(self):
        if not ACCESS.unlocked:
            QMessageBox.information(self, "Function Generator", "Unlock first.")
            return False
        return True

    def _after(self, what):
        def cb(res, err):
            if err:
                QMessageBox.warning(self, "Function Generator", f"{what} failed:\n{err}")
                self.page.set_status(f"{what} failed: {err}", False)
            else:
                self.page.set_status(f"{what}: done", True)
            self.page.refresh_all()
            self.reload(force=True)
            self.page.log_tab.fill()
        return cb

    def _change_function(self):
        if not self._guard():
            return
        new = self.fn_combo.currentData()
        old = self.page.functions.get(self.section)
        if new == old:
            return
        if not confirm(self, "Change Function", [f"Section {SECTIONS[self.section]}: {S.display(old)} -> {S.display(new)}",
                                                 "The section output changes immediately."], self.section):
            return
        self.runner.run(lambda: FG.select_function(self.section, new, WHO), self._after("Change function"))

    def _apply_params(self):
        if not self._guard() or not self.fn:
            return
        try:
            p = self._collect(self.fn)
        except ValueError as e:
            QMessageBox.warning(self, "Function Generator", str(e))
            return
        cur = dict((self.state or {}).get("config", {}))
        show = {k: v for k, v in p.items() if k not in ("section", "pattern_values", "lut_values", "win_values")}
        if not confirm(self, "Apply Parameters", [f"Section {SECTIONS[self.section]} ({S.display(self.fn)})",
                                                  f"Now: {_brief(cur)}", f"New: {_brief(show)}"], self.section):
            return
        fn = self.fn
        self.runner.run(lambda: FG.configure_function(self.section, fn, p, WHO), self._after("Apply parameters"))

    def _apply_io(self):
        if not self._guard():
            return
        try:
            inp = {"standard": self.in_std.currentData(), "imp": self.in_imp.currentData()}
            lo, hi, unit = THRESH.get(self.dac, THRESH[0])
            inp["threshold"] = int_field(self.thr.text() or "0", lo, hi, 1, f"Threshold ({unit})")
            if self.sub_std.isVisible():
                inp["standard_sub"] = self.sub_std.currentData()
            out = {"standard": self.out_std.currentData(), "imp": self.out_imp.currentData()}
        except ValueError as e:
            QMessageBox.warning(self, "Function Generator", str(e))
            return
        lines = [f"Section {SECTIONS[self.section]} inputs: {self.in_std.currentText()}, {self.in_imp.currentText()}, "
                 f"threshold {inp['threshold']}", f"Outputs: {self.out_std.currentText()}, {self.out_imp.currentText()}"]
        if not confirm(self, "Apply I/O", lines, self.section):
            return
        self.runner.run(lambda: FG.configure_io(self.section, inp, out, WHO), self._after("Apply I/O"))

    def _apply_channel(self, row):
        if not self._guard():
            return
        w = self.ch_widgets[row]
        is_in = row < 6
        ch = row if is_in else row - 6
        try:
            if is_in:
                vals = {"status": w["status"].isChecked(), "enable_gd": w["enable"].isChecked(),
                        "gate": int_field(w["gate"].text() or "15", 15, 10000, 5, "Gate"),
                        "delay": int_field(w["delay"].text() or "0", 0, 10000, 5, "Delay"), "invert": w["invert"].isChecked()}
            else:
                vals = {"status": w["status"].isChecked(), "enable_mono": w["enable"].isChecked(),
                        "mono_value": int_field(w["gate"].text() or "15", 15, 1000, 5, "Monostable value"),
                        "invert": w["invert"].isChecked()}
        except ValueError as e:
            QMessageBox.warning(self, "Function Generator", str(e))
            return
        name = f"{'IN' if is_in else 'OUT'} {ch + 1}"
        if not confirm(self, "Channel Settings", [f"Section {SECTIONS[self.section]} {name}: {_brief(vals)}"], self.section):
            return
        self.runner.run(lambda: FG.configure_channel(self.section, "in" if is_in else "out", ch, vals, WHO),
                        self._after(f"{name} settings"))

    def _reset(self, ch, label):
        if not self._guard():
            return
        if not confirm(self, label, [f"Section {SECTIONS[self.section]}: {label}"], self.section):
            return
        fn = self.fn
        self.runner.run(lambda: FG.reset_channel(self.section, fn, ch, label, WHO), self._after(label))


def _center(w):
    box = QWidget()
    h = QHBoxLayout(box)
    h.setContentsMargins(0, 0, 0, 0)
    h.addWidget(w, 0, Qt.AlignCenter)
    return box


def _brief(d):
    if not isinstance(d, dict):
        return str(d)
    parts = []
    for k, v in d.items():
        if k in ("section", "lemo_enables", "lemo_in_enables", "lemo_out_enables"):
            continue
        parts.append(f"{k}={fmt_int(v) if isinstance(v, int) and not isinstance(v, bool) else v}")
    le = d.get("lemo_enables") or d.get("lemo_in_enables")
    if le:
        parts.append("on=" + "".join("1" if e.get("enable") else "0" for e in le))
    return ", ".join(parts)


# =========================================================================================
class LogicAnalyserTab(QWidget, _Locked):
    """Trigger setup and one-shot capture of all 24 inputs and 16 outputs."""

    def __init__(self, page):
        super().__init__()
        self.page = page
        self.runner = Runner()
        self._init_lock()
        lay = QHBoxLayout(self)
        left = QVBoxLayout()
        trig = QGroupBox("Trigger")
        tl = QGridLayout(trig)
        tl.addWidget(QLabel("Mode input"), 0, 0)
        self.mode_in = combo(["AND", "OR", "OFF"], [0, 1, 2])
        tl.addWidget(self.mode_in, 0, 1)
        tl.addWidget(QLabel("Mode output"), 1, 0)
        self.mode_out = combo(["AND", "OR", "OFF"], [0, 1, 2])
        tl.addWidget(self.mode_out, 1, 1)
        tl.addWidget(QLabel("Edge"), 2, 0)
        self.edge = combo(["Rising", "Falling"], [0, 1])
        tl.addWidget(self.edge, 2, 1)
        self.trig_cb = {}
        grid = QGridLayout()
        for s in range(4):
            grid.addWidget(QLabel(SECTIONS[s]), 0, s + 1, Qt.AlignCenter)
        names = [f"in{n}" for n in range(1, 7)] + [f"out{n}" for n in range(1, 5)]
        for r, nm in enumerate(names):
            grid.addWidget(QLabel(nm.upper().replace("IN", "IN ").replace("OUT", "OUT ")), r + 1, 0)
            for s in range(4):
                cb = QCheckBox()
                grid.addWidget(cb, r + 1, s + 1, Qt.AlignCenter)
                self.trig_cb[f"sec{s + 1}_{nm}"] = cb
                self.controls.append(cb)
        tl.addLayout(grid, 3, 0, 1, 2)
        rb = QPushButton("Read Trigger")
        rb.clicked.connect(self.read_trigger)
        ab = QPushButton("Apply Trigger")
        ab.clicked.connect(self._apply_trigger)
        tl.addWidget(rb, 4, 0)
        tl.addWidget(ab, 4, 1)
        self.controls += [self.mode_in, self.mode_out, self.edge, ab]
        left.addWidget(trig)
        cap = QGroupBox("Capture")
        cl = QFormLayout(cap)
        self.show_sec = QComboBox()
        self.show_sec.addItems(["All", "A", "B", "C", "D"])
        self.show_sec.currentIndexChanged.connect(self._plot)
        cl.addRow("Show section", self.show_sec)
        self.timeout = QLineEdit("10")
        cl.addRow("Wait for trigger (s)", self.timeout)
        b = QPushButton("Arm and Capture")
        b.clicked.connect(self._capture)
        cl.addRow(b)
        self.cap_info = QLabel("")
        cl.addRow(self.cap_info)
        left.addWidget(cap)
        left.addStretch(1)
        lw = QWidget()
        lw.setLayout(left)
        lw.setMaximumWidth(380)
        lay.addWidget(lw)
        from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
        from matplotlib.figure import Figure
        self.fig = Figure(figsize=(8, 6), tight_layout=True)
        self.canvas = FigureCanvasQTAgg(self.fig)
        lay.addWidget(self.canvas, 1)
        self.data = None
        self._paint_access(ACCESS.unlocked)

    def read_trigger(self):
        self.runner.run(FG.la_trigger, self._trig_loaded)

    def _trig_loaded(self, cfg, err):
        if err or not cfg:
            return
        set_combo(self.mode_in, cfg.get("mode_in"))
        set_combo(self.mode_out, cfg.get("mode_out"))
        set_combo(self.edge, 0 if cfg.get("edge", 0) == 0 else 1)
        for k, cb in self.trig_cb.items():
            cb.setChecked(bool(cfg.get(k)))

    def _apply_trigger(self):
        if not ACCESS.unlocked:
            return
        cfg = {"mode_in": self.mode_in.currentData(), "mode_out": self.mode_out.currentData(), "edge": self.edge.currentData()}
        cfg.update({k: cb.isChecked() for k, cb in self.trig_cb.items()})
        if not confirm(self, "Logic Analyser Trigger", ["Write the logic analyser trigger setup."]):
            return
        self.runner.run(lambda: FG.set_la_trigger(cfg, WHO), lambda r, e: (self._trig_loaded(r, e), self.page.log_tab.fill()))

    def _capture(self):
        try:
            t = float(self.timeout.text())
        except ValueError:
            t = 10.0
        self.cap_info.setText("waiting for trigger ...")
        self.runner.run(lambda: FG.la_capture(t), self._captured)

    def _captured(self, data, err):
        if err:
            self.cap_info.setText(f"failed: {err}")
            return
        if not data:
            self.cap_info.setText("no trigger within the wait time")
            return
        self.data = data
        step = data.get("time", 0)
        n = len((data.get("inputs") or [[]])[0])
        self.cap_info.setText(f"{n} samples x {step} ns = {n * step / 1000:,.1f} us")
        self._plot()

    def _plot(self):
        self.fig.clear()
        if not self.data:
            self.canvas.draw_idle()
            return
        ax = self.fig.add_subplot(111)
        step = self.data.get("time", 1) or 1
        sel = self.show_sec.currentIndex() - 1
        rows = []
        for s in range(4):
            if sel >= 0 and s != sel:
                continue
            for n in range(6):
                rows.append((f"{SECTIONS[s]} IN {n + 1}", self.data["inputs"][s * 6 + n]))
            for n in range(4):
                rows.append((f"{SECTIONS[s]} OUT {n + 1}", self.data["outputs"][s * 4 + n]))
        for k, (lab, y) in enumerate(rows):
            off = (len(rows) - 1 - k) * 1.5
            ax.step([i * step for i in range(len(y))], [off + 0.9 * v for v in y], where="post", lw=0.8)
        ax.set_yticks([(len(rows) - 1 - k) * 1.5 + 0.45 for k in range(len(rows))])
        ax.set_yticklabels([r[0] for r in rows], fontsize=7)
        ax.set_xlabel("Time (ns)")
        ax.grid(axis="x", alpha=0.3)
        self.canvas.draw_idle()


# =========================================================================================
class DeviceTab(QWidget, _Locked):
    def __init__(self, page):
        super().__init__()
        self.page = page
        self.runner = Runner()
        self._init_lock()
        lay = QHBoxLayout(self)
        col1 = QVBoxLayout()

        info = QGroupBox("Device")
        self.info = QFormLayout(info)
        self.info_lbl = {}
        for k in ("Model", "Serial number", "Software", "FPGA", "Zynq", "MAC", "DAC version", "License", "Network"):
            lab = QLabel("-")
            lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.info.addRow(k, lab)
            self.info_lbl[k] = lab
        col1.addWidget(info)

        clk = QGroupBox("Clock")
        cl = QGridLayout(clk)
        cl.addWidget(QLabel("Source"), 0, 0)
        self.clk_src = QLabel("-")
        cl.addWidget(self.clk_src, 0, 1)
        cl.addWidget(QLabel("Clock output"), 1, 0)
        self.clk_out = QLabel("-")
        cl.addWidget(self.clk_out, 1, 1)
        btns = [("Check External Clock", self._check_clk, False), ("Clock Output ON", lambda: self._dev("enable_clk_out", "Clock output ON"), True),
                ("Clock Output OFF", lambda: self._dev("disable_clk_out", "Clock output OFF"), True),
                ("Use Internal Clock", lambda: self._dev("apply_int_clk", "Use internal clock", reboot=True), True),
                ("Use External Clock", lambda: self._dev("apply_ext_clk", "Use external clock", reboot=True), True)]
        for i, (t, fn, locked) in enumerate(btns):
            b = QPushButton(t)
            b.clicked.connect(fn)
            cl.addWidget(b, 2 + i // 2, i % 2)
            if locked:
                self.controls.append(b)
        col1.addWidget(clk)

        misc = QGroupBox("Maintenance")
        ml = QHBoxLayout(misc)
        for t, cmd in (("Calibrate Delay Lines", "calibrate_delay_lines"), ("Locate Device ON", "start_alarm"),
                       ("Locate Device OFF", "stop_alarm")):
            b = QPushButton(t)
            b.clicked.connect(lambda _c=False, cmd=cmd, t=t: self._dev(cmd, t))
            ml.addWidget(b)
            self.controls.append(b)
        col1.addWidget(misc)

        files = QGroupBox("Files on the Device")
        fl = QVBoxLayout(files)
        row = QHBoxLayout()
        self.kind = combo(["Configurations", "LUT", "Pattern", "Window widths"], list(FG.FILE_KINDS))
        self.kind.currentIndexChanged.connect(self.list_files)
        row.addWidget(self.kind)
        rb = QPushButton("Refresh")
        rb.clicked.connect(self.list_files)
        row.addWidget(rb)
        row.addStretch(1)
        fl.addLayout(row)
        self.file_list = QListWidget()
        fl.addWidget(self.file_list, 1)
        grid = QGridLayout()
        acts = [("View", self._view, False), ("Load into Device", self._load, True), ("Save Current As", self._save_as, True),
                ("Rename", self._rename, True), ("Upload from File", self._upload, True), ("Delete", self._delete, True)]
        for i, (t, fn, locked) in enumerate(acts):
            b = QPushButton(t)
            b.clicked.connect(fn)
            grid.addWidget(b, i // 3, i % 3)
            if locked:
                self.controls.append(b)
        fl.addLayout(grid)
        col1.addWidget(files, 1)
        c1 = QWidget()
        c1.setLayout(col1)
        lay.addWidget(c1, 1)

        panel = QGroupBox("Front Panel")
        pl = QVBoxLayout(panel)
        self.screen = QLabel()
        self.screen.setAlignment(Qt.AlignCenter)
        self.screen.setMinimumSize(320, 427)
        self.screen.mousePressEvent = self._screen_click
        pl.addWidget(self.screen, 1)
        sb = QPushButton("Refresh Screen")
        sb.clicked.connect(self.grab_screen)
        pl.addWidget(sb)
        self.remote = QCheckBox("Remote touch")
        pl.addWidget(self.remote)
        self.controls.append(self.remote)
        lay.addWidget(panel)
        self._scale = 1.0
        self._paint_access(ACCESS.unlocked)

    # ---- reads
    def show_device(self, d):
        v = d.get("version") or {}
        model = {0: "N1081A/B", 1: "DT1081B", 2: "N1081B"}.get(d.get("model"), str(d.get("model")))
        for k, val in (("Model", model), ("Serial number", v.get("serial_number")), ("Software", v.get("software_version")),
                       ("FPGA", v.get("fpga_version")), ("Zynq", v.get("zynq_version")), ("MAC", v.get("mac_address")),
                       ("DAC version", {0: "0 (mV)", 1: "1 (mV)", 2: "2 (not calibrated)"}.get(d.get("dac"), d.get("dac"))),
                       ("License", "valid" if d.get("license") else "missing")):
            self.info_lbl[k].setText(str(val if val is not None else "-"))
        self.clk_src.setText({"1": "External", "2": "Internal", "0": "Internal (external missing)"}.get(d.get("clock"), "-"))
        self.clk_out.setText("ON" if d.get("clk_out") == 1 else "OFF" if d.get("clk_out") == 0 else "-")

    def first_show(self):
        self.list_files()
        self.grab_screen()
        self.runner.run(FG.eth_config, lambda r, e: self.info_lbl["Network"].setText(
            f"{r.get('ip')} / {r.get('nm')}, gw {r.get('gw')}, {'DHCP' if r.get('dhcp') else 'static'}" if r else "-"))

    def list_files(self):
        kind = self.kind.currentData()
        self.runner.run(lambda: FG.list_files(kind), self._files_loaded)

    def _files_loaded(self, names, err):
        self.file_list.clear()
        for n in names or []:
            self.file_list.addItem(n)

    def _sel(self):
        it = self.file_list.currentItem()
        if not it:
            QMessageBox.information(self, "Files", "Select a file first.")
            return None
        n = it.text()
        return n[:-5] if n.endswith(".json") else n

    def _view(self):
        name = self._sel()
        if not name:
            return
        kind = self.kind.currentData()
        self.runner.run(lambda: FG.download(name, kind), lambda r, e: self._show_text(name, r, e))

    def _show_text(self, name, data, err):
        txt = err or json.dumps(data, indent=2)
        box = QMessageBox(self)
        box.setWindowTitle(name)
        box.setText(f"{name} ({self.kind.currentText()})")
        box.setDetailedText(txt)
        box.exec_()

    def grab_screen(self):
        self.runner.run(FG.screenshot, self._screen_loaded)

    def _screen_loaded(self, bmp, err):
        if err or not bmp:
            self.screen.setText(f"screen not available: {err}")
            return
        pm = QPixmap()
        pm.loadFromData(bmp)
        self._scale = 427 / max(pm.height(), 1)
        self.screen.setPixmap(pm.scaledToHeight(427, Qt.SmoothTransformation))

    def _screen_click(self, e):
        if not (ACCESS.unlocked and self.remote.isChecked()) or self.screen.pixmap() is None:
            return
        pm = self.screen.pixmap()
        x0 = (self.screen.width() - pm.width()) // 2
        y0 = (self.screen.height() - pm.height()) // 2
        x, y = int((e.x() - x0) / self._scale), int((e.y() - y0) / self._scale)
        if x < 0 or y < 0:
            return
        self.runner.run(lambda: FG.device_action("remote_click", f"screen tap {x},{y}", WHO, top={"x": x, "y": y}, callback="settings"),
                        lambda r, err: self.grab_screen())

    # ---- writes
    def _dev(self, cmd, label, reboot=False):
        if not ACCESS.unlocked:
            return
        lines = [label]
        if reboot:
            lines.append("The device reboots: the DAQ trigger and laser triggers stop until it is back.")
        if not confirm(self, label, lines):
            return
        self.runner.run(lambda: FG.device_action(cmd, label, WHO), self._done(label))

    def _check_clk(self):
        self.runner.run(FG.check_clock, lambda r, e: QMessageBox.information(
            self, "External Clock", err_or(e, "valid external clock present" if r == 1 else "no valid external clock")))

    def _done(self, label):
        def cb(res, err):
            if err:
                QMessageBox.warning(self, "Function Generator", f"{label} failed:\n{err}")
            self.page.set_status(f"{label}: {'failed' if err else 'done'}", not err)
            self.page.refresh_all()
            self.list_files()
            self.page.log_tab.fill()
        return cb

    def _load(self):
        name = self._sel()
        if not name or not ACCESS.unlocked or self.kind.currentData() != "config":
            return
        if not confirm(self, "Load Configuration", [f"Load '{name}' into the device.", "All four sections are reprogrammed."]):
            return
        self.runner.run(lambda: FG.device_action("load_config", f"load config {name}", WHO, {"file_name": name}), self._done("Load configuration"))

    def _save_as(self):
        if not ACCESS.unlocked or self.kind.currentData() != "config":
            return
        import re
        name, ok = QInputDialog.getText(self, "Save Current Setup", "Name:")
        if not ok or not name:
            return
        if not re.match(FG.NAME_RE, name):
            QMessageBox.warning(self, "Save", "Letters, digits, - + _ only, fewer than 20 characters.")
            return
        self.runner.run(lambda: FG.device_action("create_config", f"save config {name}", WHO, {"new_name": f"{name}.json"}),
                        self._done("Save configuration"))

    def _rename(self):
        old = self._sel()
        if not old or not ACCESS.unlocked or self.kind.currentData() != "config":
            return
        import re
        new, ok = QInputDialog.getText(self, "Rename", f"New name for '{old}':")
        if not ok or not new or not re.match(FG.NAME_RE, new):
            return
        self.runner.run(lambda: FG.device_action("rename_config", f"rename config {old} -> {new}", WHO,
                                                 {"old_name": old, "new_name": new, "function": "config"}), self._done("Rename"))

    def _upload(self):
        if not ACCESS.unlocked or self.kind.currentData() != "config":
            return
        path, _ = QFileDialog.getOpenFileName(self, "Upload configuration", os.path.expanduser("~"), "JSON (*.json)")
        if not path:
            return
        name = os.path.splitext(os.path.basename(path))[0]
        try:
            text = open(path).read()
            json.loads(text)
        except Exception as e:
            QMessageBox.warning(self, "Upload", f"Not a JSON file: {e}")
            return
        if not confirm(self, "Upload Configuration", [f"Upload '{name}' to the device (stored, not loaded)."]):
            return
        self.runner.run(lambda: FG.upload_config(name, text, WHO), self._done("Upload"))

    def _delete(self):
        name = self._sel()
        if not name or not ACCESS.unlocked:
            return
        kind = self.kind.currentData()
        if name == "Default":
            return
        if not confirm(self, "Delete File", [f"Delete '{name}' ({self.kind.currentText()}) from the device.", "This cannot be undone."]):
            return
        self.runner.run(lambda: FG.device_action("delete_config", f"delete {kind} {name}", WHO, {"file_name": name, "function": kind}),
                        self._done("Delete"))


def err_or(err, text):
    return f"failed: {err}" if err else text


# =========================================================================================
class LogTab(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Time", "Who", "Section", "Action", "Before", "After"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        lay.addWidget(self.table)
        row = QHBoxLayout()
        b = QPushButton("Refresh")
        b.clicked.connect(self.fill)
        row.addWidget(b)
        b2 = QPushButton("Open Folder")
        b2.clicked.connect(lambda: os.makedirs(FG.LOG_DIR, exist_ok=True) or os.system(f"xdg-open '{FG.LOG_DIR}' &"))
        row.addWidget(b2)
        row.addStretch(1)
        lay.addLayout(row)

    def fill(self):
        rows = FG.history(500)
        self.table.setRowCount(len(rows))
        for r, row in enumerate(reversed(rows)):
            for c, v in enumerate(row):
                self.table.setItem(r, c, QTableWidgetItem(v))
        self.table.resizeColumnsToContents()

"""Function Generator tab, simple layout: the CAEN DT1081B that makes the DAQ and laser triggers.

One screen: connection bar, one card per section (function, key values, connector lamps) and the
change log. Every change goes through the Change dialog (function, parameters, I/O standard) and
needs this tab's own Admin Unlock (the master password, independent of the global control
unlock). Device details open in a small read-only window. Uses core.function_generator for
all device traffic; nothing here talks to the device directly.
"""
import os

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame, QGridLayout,
                             QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton,
                             QTableWidget,
                             QTableWidgetItem, QVBoxLayout, QWidget)

from app import theme
from core import env
from core import fg_schema as S
from core import function_generator as FG
from core.access import check_password
from core.jobs import JOBS
from pages.fg_parts import Runner
from widgets import basic

REFRESH_MS = 3000
WHO = "v2 GUI"
SECTIONS = "ABCD"
STD_IN = {0: "NIM", 1: "TTL", 2: "DISC"}
STD_OUT = {0: "NIM", 1: "TTL"}
IN_STD = (["NIM", "TTL", "DISCRIMINATOR"], [0, 1, 2])
SUB_STD = (["NIM", "TTL"], [0, 1])
OUT_STD = (["NIM", "TTL"], [0, 1])
IMP = (["50 Ohm", "1 kOhm"], [True, False])
THRESH = {0: (-800, 2800, "mV"), 1: (0, 1800, "mV"), 2: (0, 65000, "lsb")}
DRIVES = {0: "the DAQ trigger (and the HK digitizer)", 1: "the four laser heads"}
# the functions we actually use; the rest appear only behind "Show all functions"
USED = ["pulse_generator", "fanout"]
# functions whose parameters need files, spectra or threshold tables: not editable from this dialog
COMPLEX = {fn for fn, spec in S.SPECS.items()
           if any(k in spec for k in ("lut", "pattern", "windows", "thresholds", "timetag"))}


def _combo(labels, values, current=None):
    c = QComboBox()
    for lab, val in zip(labels, values):
        c.addItem(lab, val)
    if current is not None:
        _set_combo(c, current)
    return c


def _set_combo(c, value):
    i = c.findData(value)
    if i >= 0:
        c.setCurrentIndex(i)


def _int(text, lo, hi, step, label):
    try:
        v = int(str(text).replace(",", "").strip())
    except ValueError:
        raise ValueError(f"{label}: enter an integer.")
    if not lo <= v <= hi:
        raise ValueError(f"{label}: must be between {lo:,} and {hi:,}.")
    if step > 1 and v % step:
        raise ValueError(f"{label}: must be a multiple of {step}.")
    return v


def _fmt(v):
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return str(v)


def _rate(period_ns):
    if not period_ns:
        return "-"
    r = 1e9 / period_ns
    return f"{r / 1e6:g} MHz" if r >= 1e6 else f"{r / 1e3:g} kHz" if r >= 1e3 else f"{r:g} Hz"


def _io_text(i, o):
    inp = STD_IN.get(i.get("standard"), "?")
    if i.get("standard") == 2:
        inp += f" {i.get('threshold')} mV"
    return f"{inp}, {'50 Ohm' if i.get('imp', True) else '1 kOhm'}", STD_OUT.get(o.get("standard"), "?")


def card_lines(st):
    """[(label, value)] shown on a section card."""
    fn, cfg = st["function"], st.get("config", {}) or {}
    i, o = st.get("input", {}) or {}, st.get("output", {}) or {}
    in_txt, out_txt = _io_text(i, o)
    if "error" in cfg:
        return [("Config", "read failed"), ("Input", in_txt), ("Output", out_txt)]
    if fn == "pulse_generator":
        return [("Period", f"{_fmt(cfg.get('period'))} ns"), ("Width", f"{_fmt(cfg.get('width'))} ns"),
                ("Rate", f"{_rate(cfg.get('period'))}, {'Poisson' if cfg.get('frequency_type') else 'fixed'}"),
                ("Output", out_txt)]
    lines = [("Input", in_txt), ("Output", out_txt)]
    if fn == "fanout":
        on = [str(k + 1) for k, e in enumerate(cfg.get("lemo_enables") or []) if e.get("enable")]
        lines.append(("Route", f"IN 1 -> OUT {', '.join(on)}" if on else "no output enabled"))
    else:
        s = S.summary(fn, cfg)
        if s:
            lines.append(("Setting", s))
    return lines


# =========================================================================================
class SectionCard(QGroupBox):
    def __init__(self, section, on_change):
        super().__init__(f"Section {SECTIONS[section]}")
        self.section = section
        g = QGridLayout(self)
        g.setContentsMargins(14, 10, 14, 12)
        g.setVerticalSpacing(10)
        self.fn = QLabel("-")
        self.fn.setStyleSheet(f"font-size: 15pt; font-weight: 700; color: {theme.C['accent']};")
        g.addWidget(self.fn, 0, 0, 1, 2, Qt.AlignLeft)
        self.rows = []
        for r in range(4):
            k, v = QLabel(""), QLabel("")
            k.setObjectName("key")
            k.setStyleSheet("font-size: 12.5pt;")
            v.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            v.setStyleSheet("font-size: 12.5pt; font-weight: 600;")
            g.addWidget(k, 1 + r, 0)
            g.addWidget(v, 1 + r, 1)
            self.rows.append((k, v))
        self.in_lamps = self._lamp_row(g, 5, "IN", 6)
        self.out_lamps = self._lamp_row(g, 6, "OUT", 4)
        g.setRowStretch(7, 1)   # keeps the button at the bottom when the card is taller than its rows
        self.btn = QPushButton("Change...")
        self.btn.setStyleSheet("font-size: 12pt; padding: 8px;")
        self.btn.clicked.connect(lambda: on_change(section))
        g.addWidget(self.btn, 8, 0, 1, 2)
        g.setColumnStretch(1, 1)

    @staticmethod
    def _lamp_row(g, row, name, n):
        h = QHBoxLayout()
        h.setSpacing(8)
        lab = QLabel(name)
        lab.setObjectName("key")
        lab.setStyleSheet("font-size: 12.5pt;")
        lab.setFixedWidth(44)
        h.addWidget(lab)
        lamps = []
        for i in range(n):
            num = QLabel(str(i + 1))
            num.setObjectName("key")
            num.setStyleSheet("font-size: 12pt;")
            lamp = basic.make_lamp(14)
            h.addWidget(num)
            h.addWidget(lamp)
            h.addSpacing(6)
            lamps.append(lamp)
        h.addStretch(1)
        g.addLayout(h, row, 0, 1, 2)
        return lamps

    def set_access(self, unlocked):
        self.btn.setEnabled(bool(unlocked))
        self.btn.setToolTip("" if unlocked else "Admin Unlock to change")

    def show_state(self, st):
        fn = st["function"]
        self.fn.setText(S.display(fn))
        lines = card_lines(st)
        for r, (k, v) in enumerate(self.rows):
            k.setText(lines[r][0] if r < len(lines) else "")
            v.setText(lines[r][1] if r < len(lines) else "")
        ins, outs = S.ROLES.get(fn, ({}, {}))
        cs = st.get("ch_status", [])
        off = theme.C.get("hint", "#999")
        for k, lamp in enumerate(self.in_lamps):
            used = (k + 1) in ins and (cs[k] if k < len(cs) else 1)
            basic.paint_lamp(lamp, basic.LAMP_ON if used else off)
            lamp.setToolTip(f"IN {k + 1}: {ins.get(k + 1, 'unused')}")
        cfg = st.get("config", {}) or {}
        out_en = None
        if fn in ("fanout", "pulse_generator", "digital_generator", "pattern_generator"):
            out_en = [bool(e.get("enable")) for e in (cfg.get("lemo_enables") or [])]
        for k, lamp in enumerate(self.out_lamps):
            used = (k + 1) in outs and (out_en[k] if out_en and k < len(out_en) else True)
            basic.paint_lamp(lamp, basic.LAMP_ON if used else off)
            lamp.setToolTip(f"OUT {k + 1}: {outs.get(k + 1, 'unused')}")


# =========================================================================================
class ChangeDialog(QDialog):
    """Function, parameters and I/O standard of one section; one Apply, one confirmation."""

    def __init__(self, parent, section, state, dac, is_unlocked):
        super().__init__(parent)
        self.section, self.state, self.dac = section, state, dac or 0
        self.is_unlocked = is_unlocked
        self.cur_fn = state["function"]
        self.runner = Runner()
        self.form = {}
        self.setWindowTitle(f"Section {SECTIONS[section]} - Change")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)

        warn = []
        if section in DRIVES:
            warn.append(f"Section {SECTIONS[section]} drives {DRIVES[section]}.")
        if JOBS.any_running():
            warn.append("A scan or stability run is in progress.")
        if warn:
            w = QLabel("\n".join(warn))
            w.setStyleSheet(f"color: {theme.C['bad']}; font-weight: 600;")
            lay.addWidget(w)

        # ---- function
        fbox = QGroupBox("Function")
        fl = QHBoxLayout(fbox)
        self.fn_combo = QComboBox()
        self.fn_combo.currentIndexChanged.connect(self._fn_changed)
        fl.addWidget(self.fn_combo, 1)
        self.show_all = QCheckBox("Show all functions")
        self.show_all.toggled.connect(self._fill_functions)
        fl.addWidget(self.show_all)
        lay.addWidget(fbox)
        self.fn_note = QLabel("")
        self.fn_note.setObjectName("hint")
        self.fn_note.setWordWrap(True)
        lay.addWidget(self.fn_note)

        # ---- parameters
        self.pbox = QGroupBox("Parameters")
        self.play = QVBoxLayout(self.pbox)
        lay.addWidget(self.pbox)

        # ---- I/O standard
        io = QGroupBox("Input / Output standard")
        g = QGridLayout(io)
        i, o = state.get("input", {}) or {}, state.get("output", {}) or {}
        self.in_lbl = QLabel("Inputs")
        g.addWidget(self.in_lbl, 0, 0)
        self.in_std = _combo(*IN_STD, i.get("standard"))
        self.in_std.currentIndexChanged.connect(self._thr_enable)
        g.addWidget(self.in_std, 0, 1)
        self.sub_lbl = QLabel("IN 3, IN 6")
        g.addWidget(self.sub_lbl, 0, 2)
        self.sub_std = _combo(*SUB_STD, i.get("standard_sub"))
        g.addWidget(self.sub_std, 0, 3)
        lo, hi, unit = THRESH.get(self.dac, THRESH[0])
        g.addWidget(QLabel(f"Threshold ({unit})"), 1, 0)
        self.thr = QLineEdit(str(i.get("threshold", "")))
        self.thr.setFixedWidth(110)
        g.addWidget(self.thr, 1, 1)
        g.addWidget(QLabel("Input impedance"), 1, 2)
        self.in_imp = _combo(*IMP, bool(i.get("imp", True)))
        g.addWidget(self.in_imp, 1, 3)
        g.addWidget(QLabel("Outputs"), 2, 0)
        self.out_std = _combo(*OUT_STD, o.get("standard"))
        g.addWidget(self.out_std, 2, 1)
        g.addWidget(QLabel("Output impedance"), 2, 2)
        self.out_imp = _combo(*IMP, bool(o.get("imp", True)))
        g.addWidget(self.out_imp, 2, 3)
        lay.addWidget(io)

        self.msg = QLabel("")
        self.msg.setObjectName("hint")
        self.msg.setWordWrap(True)
        lay.addWidget(self.msg)
        bb = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.apply_btn = bb.addButton("Apply", QDialogButtonBox.AcceptRole)
        theme.role(self.apply_btn, "warn")
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self._apply)
        lay.addWidget(bb)

        self._fill_functions()
        self._build_params(self.cur_fn, state.get("config", {}) or {})
        self._thr_enable()

    # ---- function list
    def _fill_functions(self):
        fns = list(S.FUNCTIONS) if self.show_all.isChecked() else list(USED)
        if self.cur_fn not in fns:
            fns.insert(0, self.cur_fn)
        sel = self.fn_combo.currentData() if self.fn_combo.count() else self.cur_fn
        self.fn_combo.blockSignals(True)
        self.fn_combo.clear()
        for fn in fns:
            self.fn_combo.addItem(S.display(fn) + ("  (current)" if fn == self.cur_fn else ""), fn)
        _set_combo(self.fn_combo, sel if sel in fns else self.cur_fn)
        self.fn_combo.blockSignals(False)
        self._fn_changed()

    def _fn_changed(self):
        fn = self.fn_combo.currentData()
        if fn is None:
            return
        if fn == self.cur_fn:
            self.fn_note.setText("")
            self._build_params(fn, self.state.get("config", {}) or {})
        else:
            self.fn_note.setText(f"Changing the function replaces the section output immediately: "
                                 f"{S.display(self.cur_fn)} -> {S.display(fn)}. Fill in its parameters below.")
            self._build_params(fn, {})
        special = fn in S.SPECIAL_IO and self.dac != 1
        self.sub_lbl.setVisible(special)
        self.sub_std.setVisible(special)
        self.in_lbl.setText("IN 1, 2, 4, 5" if special else "Inputs")

    # ---- parameter form (enables, coincidence flags, bypass, plain fields)
    def _clear(self, lay):
        while lay.count():
            it = lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
            elif it.layout():
                self._clear(it.layout())

    def _build_params(self, fn, cfg):
        self._clear(self.play)
        self.form = {}
        spec = S.SPECS.get(fn, {})
        self.pbox.setTitle(f"Parameters - {S.display(fn)}")
        if fn in COMPLEX:
            self.play.addWidget(QLabel("This function needs files or tables: set it up on the device web UI."))
            self.apply_btn.setEnabled(False)
            return
        self.apply_btn.setEnabled(True)
        en, co = S.read_enables(fn, cfg) if cfg else ([True] * len(spec.get("enables", [])), [False] * len(spec.get("enables", [])))
        if spec.get("enables"):
            row = QHBoxLayout()
            self.form["enables"] = []
            for k, (lab, _i, _l) in enumerate(spec["enables"]):
                cb = QCheckBox(lab)
                cb.setChecked(en[k])
                row.addWidget(cb)
                self.form["enables"].append(cb)
            row.addStretch(1)
            self.play.addLayout(row)
        if spec.get("coinc_flag"):
            row = QHBoxLayout()
            self.form["coinc"] = []
            for k, (lab, _i, _l) in enumerate(spec["enables"]):
                cb = QCheckBox(f"Coincidence {lab}")
                cb.setChecked(co[k])
                row.addWidget(cb)
                self.form["coinc"].append(cb)
            row.addStretch(1)
            self.play.addLayout(row)
        fl = QFormLayout()
        if spec.get("bypass"):
            b = _combo(S.BYPASS, list(range(5)), int(cfg.get("bypass_section", 0)) if cfg.get("bypass_enable") else 0)
            fl.addRow("Bypass section", b)
            self.form["bypass_section"] = ("bypass", b, None, "Bypass")
        for key, label, kind, extra in spec.get("fields", []):
            v = cfg.get(key)
            if kind == "bool":
                w = QCheckBox()
                w.setChecked(bool(v))
            elif kind == "enum":
                w = _combo(extra[0], extra[1], v)
            else:
                w = QLineEdit(_fmt(v) if v is not None else "")
                w.setFixedWidth(160)
            fl.addRow(label, w)
            self.form[key] = (kind, w, extra, label)
        self.play.addLayout(fl)
        if not self.form:
            self.play.addWidget(QLabel("No parameters."))

    def _thr_enable(self):
        self.thr.setEnabled(self.in_std.currentData() == 2)

    # ---- collect
    def _params(self, fn):
        enables = [cb.isChecked() for cb in self.form.get("enables", [])]
        coinc = [cb.isChecked() for cb in self.form.get("coinc", [])]
        values = {}
        for key, item in self.form.items():
            if key in ("enables", "coinc"):
                continue
            kind, w, extra, label = item
            if kind == "bypass":
                values[key] = w.currentData()
            elif kind == "bool":
                values[key] = w.isChecked()
            elif kind == "enum":
                values[key] = w.currentData()
            else:
                values[key] = _int(w.text(), *extra, label)
        if fn == "pulse_generator":
            why = FG.validate(values["period"], values["width"])
            if why:
                raise ValueError(why)
        return S.to_params(fn, self.section, enables, coinc, values)

    def _io(self):
        inp = {"standard": self.in_std.currentData(), "imp": self.in_imp.currentData()}
        lo, hi, unit = THRESH.get(self.dac, THRESH[0])
        inp["threshold"] = _int(self.thr.text() or "0", lo, hi, 1, f"Threshold ({unit})")
        if self.sub_std.isVisible():
            inp["standard_sub"] = self.sub_std.currentData()
        out = {"standard": self.out_std.currentData(), "imp": self.out_imp.currentData()}
        return inp, out

    @staticmethod
    def _same(a, b, keys):
        return all(a.get(k) == b.get(k) for k in keys)

    def _apply(self):
        new_fn = self.fn_combo.currentData()
        try:
            params = self._params(new_fn)
            inp, out = self._io()
        except (ValueError, KeyError) as e:
            QMessageBox.warning(self, "Function Generator", str(e))
            return
        cur = self.state.get("config", {}) or {}
        ci, co = self.state.get("input", {}) or {}, self.state.get("output", {}) or {}
        steps, lines = [], []
        if new_fn != self.cur_fn:
            steps.append(("function", new_fn))
            lines.append(f"Function: {S.display(self.cur_fn)} -> {S.display(new_fn)}")
        shown = {k: v for k, v in params.items() if k != "section"}
        if new_fn != self.cur_fn or any(cur.get(k) != v for k, v in shown.items()):
            steps.append(("params", params))
            lines.append(f"Parameters: {_brief(shown)}")
            if new_fn == self.cur_fn:
                lines.append(f"   (now: {_brief({k: cur.get(k) for k in shown})})")
        if not (self._same(inp, ci, inp.keys()) and self._same(out, co, out.keys())):
            steps.append(("io", (inp, out)))
            lines.append(f"Inputs: {self.in_std.currentText()}, {self.in_imp.currentText()}, threshold {inp['threshold']}"
                         f"   Outputs: {self.out_std.currentText()}, {self.out_imp.currentText()}")
        if not steps:
            self.msg.setText("Nothing changed.")
            return
        if not self.is_unlocked():
            QMessageBox.information(self, "Function Generator", "Admin Unlock first.")
            return
        txt = f"Section {SECTIONS[self.section]}\n\n" + "\n".join(lines)
        if self.section in DRIVES:
            txt += f"\n\nSection {SECTIONS[self.section]} drives {DRIVES[self.section]}."
        if JOBS.any_running():
            txt += "\n\nWARNING: a scan or stability run is in progress."
        if QMessageBox.question(self, "Apply changes", txt + "\n\nApply?", QMessageBox.Yes | QMessageBox.No,
                                QMessageBox.No) != QMessageBox.Yes:
            return
        self.apply_btn.setEnabled(False)
        self.msg.setText("Applying...")
        sec = self.section

        def work():
            for kind, arg in steps:
                if kind == "function":
                    FG.select_function(sec, arg, WHO)
                elif kind == "params":
                    FG.configure_function(sec, new_fn, arg, WHO)
                else:
                    FG.configure_io(sec, arg[0], arg[1], WHO)
            return True
        self.runner.run(work, self._applied)

    def _applied(self, _res, err):
        if err:
            self.apply_btn.setEnabled(True)
            self.msg.setText(f"Failed: {err}")
            QMessageBox.warning(self, "Function Generator", f"Apply failed:\n{err}\n\nThe section may be partly changed; check the cards.")
            return
        self.accept()


def _brief(d):
    parts = []
    for k, v in d.items():
        if k == "lemo_enables":
            v = "".join("1" if e.get("enable") else "0" for e in v)
        elif isinstance(v, bool):
            v = "on" if v else "off"
        parts.append(f"{k}={v}")
    return ", ".join(parts)


# =========================================================================================
class DeviceDialog(QDialog):
    """Read-only device summary: model, versions, clock, network."""

    def __init__(self, parent, device, eth):
        super().__init__(parent)
        self.setWindowTitle("DT1081B - Device")
        g = QGridLayout(self)
        v = device.get("version") or {}
        clk = {"1": "external", "2": "internal", "0": "internal (external missing)"}.get(device.get("clock"), "?")
        rows = [("Model", "DT1081B"), ("Serial number", v.get("serial_number", "?")),
                ("Software", v.get("software_version", "?")), ("FPGA", v.get("fpga_version", "?")),
                ("Clock", f"{clk}{', clock out on' if device.get('clk_out') == 1 else ''}"),
                ("License", str(device.get("license", "?"))), ("Alarm", str(device.get("alarm", "?"))),
                ("MAC", v.get("mac_address", "?")),
                ("Network", f"{eth.get('ip', '?')} / {eth.get('nm', '?')}, gw {eth.get('gw', '?')}, "
                            f"{'DHCP' if eth.get('dhcp') else 'static'}")]
        for r, (k, val) in enumerate(rows):
            kl = QLabel(k)
            kl.setObjectName("key")
            g.addWidget(kl, r, 0)
            g.addWidget(QLabel(val), r, 1)
        b = QPushButton("Close")
        b.clicked.connect(self.accept)
        g.addWidget(b, len(rows), 0, 1, 2)


# =========================================================================================
class FunctionGeneratorPage(QWidget):
    def __init__(self):
        super().__init__()
        self.runner = Runner()
        self.device = {}
        self.states = {}
        self.unlocked = False   # this tab's own lock, not core.access
        self._busy = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)

        bar = QFrame()
        bar.setFrameShape(QFrame.StyledPanel)
        bl = QHBoxLayout(bar)
        self.conn = QLabel("Disconnected")
        self.conn.setStyleSheet(f"font-size: 12pt; font-weight: 700; color: {theme.C['bad']};")
        bl.addWidget(self.conn)
        bl.addSpacing(10)
        self.dev_info = QLabel("")
        self.dev_info.setObjectName("key")
        bl.addWidget(self.dev_info)
        bl.addStretch(1)
        self.lock = QLabel("")
        bl.addWidget(self.lock)
        self.unlock_btn = QPushButton("Admin Unlock")
        self.unlock_btn.clicked.connect(self._toggle_unlock)
        bl.addWidget(self.unlock_btn)
        bl.addSpacing(10)
        bl.addWidget(QLabel("Host:"))
        h, _p = FG.host()
        self.host = QLineEdit(h)
        self.host.setFixedWidth(130)
        bl.addWidget(self.host)
        b = QPushButton("Reconnect")
        b.clicked.connect(self._reconnect)
        bl.addWidget(b)
        b2 = QPushButton("Refresh")
        b2.clicked.connect(self.refresh_all)
        bl.addWidget(b2)
        b3 = QPushButton("Device info...")
        b3.clicked.connect(self._device_info)
        bl.addWidget(b3)
        lay.addWidget(bar)

        cards = QHBoxLayout()
        cards.setSpacing(theme.SPACE)
        self.cards = [SectionCard(s, self._change) for s in range(4)]
        for c in self.cards:
            cards.addWidget(c, 1)
        lay.addLayout(cards, 3)

        logbox = QGroupBox("Change log")
        ll = QVBoxLayout(logbox)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Time", "Who", "Section", "Action", "Before", "After"])
        hh = self.table.horizontalHeader()
        for c in range(4):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.Stretch)   # Before / After share the remaining width
        hh.setSectionResizeMode(5, QHeaderView.Stretch)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        ll.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.status = QLabel("")
        row.addWidget(self.status, 1)
        b = QPushButton("Open log folder")
        b.clicked.connect(lambda: os.makedirs(FG.LOG_DIR, exist_ok=True) or os.system(f"xdg-open '{FG.LOG_DIR}' &"))
        row.addWidget(b)
        ll.addLayout(row)
        lay.addWidget(logbox, 2)

        self._paint_access()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(REFRESH_MS)
        self.fill_log()
        QTimer.singleShot(0, self.refresh_all)

    # ---- access (tab-local; the global control unlock does not open this tab)
    def _toggle_unlock(self):
        if self.unlocked:
            self.unlocked = False
        else:
            pwd, ok = QInputDialog.getText(self, "Admin Unlock", "Enter Admin Password:", QLineEdit.Password)
            if not ok:
                return
            if not check_password(pwd):
                QMessageBox.critical(self, "Admin Unlock", "Incorrect password.")
                return
            self.unlocked = True
        self._paint_access()

    def _paint_access(self):
        unlocked = self.unlocked
        self.lock.setText("Unlocked" if unlocked else "Locked")
        self.lock.setStyleSheet(f"font-weight: 700; color: {theme.C['warn'] if unlocked else theme.C['muted']};")
        self.unlock_btn.setText("Lock" if unlocked else "Admin Unlock")
        for c in self.cards:
            c.set_access(unlocked)

    # ---- refresh
    def showEvent(self, e):
        super().showEvent(e)
        self.refresh_all()

    def _tick(self):
        if self.isVisible():
            self.refresh_all()

    def refresh_all(self):
        if self._busy:
            return
        self._busy = True

        def work():
            d = FG.read_device()
            return d, [FG.read_section(s, d["functions"].get(s, "")) for s in range(4)]
        self.runner.run(work, self._loaded)

    def _loaded(self, res, err):
        self._busy = False
        if err:
            self.conn.setText("Disconnected")
            self.conn.setStyleSheet(f"font-size: 12pt; font-weight: 700; color: {theme.C['bad']};")
            self.set_status(err, False)
            return
        d, secs = res
        self.device = d
        h, _p = FG.host()
        self.conn.setText(f"Connected  {h}")
        self.conn.setStyleSheet(f"font-size: 12pt; font-weight: 700; color: {theme.C['good']};")
        v = d.get("version") or {}
        clk = {"1": "external", "2": "internal", "0": "internal (external missing)"}.get(d.get("clock"), "?")
        self.dev_info.setText(f"DT1081B  S/N {v.get('serial_number', '?')}  SW {v.get('software_version', '?')}  clock {clk}")
        for c, st in zip(self.cards, secs):
            self.states[c.section] = st
            c.show_state(st)

    def set_status(self, text, ok):
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {theme.C['good'] if ok else theme.C['bad']};")

    def _reconnect(self):
        env.save_setting("fg_host", self.host.text().strip() or FG.DEFAULT_HOST)
        self.refresh_all()

    def fill_log(self):
        rows = FG.history(300)
        self.table.setRowCount(len(rows))
        for r, row in enumerate(reversed(rows)):
            for c, v in enumerate(row):
                self.table.setItem(r, c, QTableWidgetItem(v))

    # ---- dialogs
    def _change(self, section):
        if not self.unlocked:
            QMessageBox.information(self, "Function Generator", "Admin Unlock first.")
            return
        st = self.states.get(section)
        if not st or not st.get("function"):
            QMessageBox.information(self, "Function Generator", "No device state yet. Refresh first.")
            return
        dlg = ChangeDialog(self, section, st, self.device.get("dac", 0), lambda: self.unlocked)
        if dlg.exec_() == QDialog.Accepted:
            self.set_status(f"Section {SECTIONS[section]}: changed", True)
            self.fill_log()
            self.refresh_all()

    def _device_info(self):
        def work():
            d = FG.read_device()
            try:
                eth = FG.eth_config() or {}
            except Exception:
                eth = {}
            return d, eth

        def show(res, err):
            if err:
                self.set_status(err, False)
                return
            DeviceDialog(self, res[0], res[1]).exec_()
        self.runner.run(work, show)

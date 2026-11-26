"""Daily Routine main tab: preset row, the Steps tree, a Timeline of the night and the Current Step
box. All state lives in core/routine.py; the page edits the current routine and shows view()."""
import copy
import json
import os
import re
import threading
from datetime import datetime, timedelta

from PyQt5.QtCore import QObject, QRectF, Qt, QTime, QTimer, pyqtSignal
from PyQt5.QtGui import QBrush, QColor, QFontMetrics, QPainter, QPen
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout,
                             QProgressBar,
                             QHeaderView, QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QRadioButton,
                             QTimeEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import (QAbstractItemView, QApplication, QShortcut, QSizePolicy, QTableWidget, QTableWidgetItem,
                             QTabWidget)

from app import theme
from app.spinner import Spinner
from core import applog
from core import routine as rt
from core.access import ACCESS
from widgets.snackbar import Snackbar

KIND_NAMES = {"general_scan": "General Scan", "hk_scan": "HK Digitizer Scan", "wavelength_scan": "Wavelength Scan", "dark_rate": "Dark Rate",
              "stability_qe": "Stability (QE/Gain)", "hv_scan": "HV Scan", "hv_apply": "HV Apply"}
KIND_HELP = {
    "general_scan": "Laser-ON uniformity scan over configured angle grid (uses Schedule Manager engine).",
    "hk_scan": "General Scan read out by the HK Digitizer; timing and run numbers follow the HK configuration.",
    "wavelength_scan": "General Scan using selected wavelengths in Laser Sequence; panel settings restored after.",
    "dark_rate": "Laser-OFF self-trigger runs; measures channel dark rate.",
    "stability_qe": "Laser-ON Stability Run at current stage angles; one block per wavelength.",
    "hv_scan": "Laser-ON runs at multiple HV offsets (tilt 0, base HV restored after) for gain curve.",
    "hv_apply": "Sets HV on selected channels and waits until voltage settles.",
}
OPEN_PAGE = {"general_scan": "General Scan", "hk_scan": "General Scan", "wavelength_scan": "General Scan", "dark_rate": "Stability Run",
             "stability_qe": "Stability Run", "hv_scan": "HV Monitor",
             "hv_apply": "HV Monitor"}
CHANNELS = (("mon", "Mon"), ("rot1", "Rot1"), ("rot2", "Rot2"))
DEFAULT_OPTIONS = {s["kind"]: s["options"] for s in reversed(rt.DEFAULT_ROUTINE["steps"])}
DEFAULT_OPTIONS.update({k: v for k, v in rt.KIND_DEFAULTS.items() if k not in DEFAULT_OPTIONS})
WAVELENGTHS = ("405nm", "375nm", "450nm", "473nm")
COLS = ("", "Step", "Start", "Options", "Est. Dur.", "Status", "", "")
STATUS_COLOR = {"Done": "good", "Running": "warn", "Failed": "bad", "Skipped": "muted", "Disabled": "muted"}
BAR_STATE = {"Done": "done", "Running": "active", "Failed": "error"}
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
REFRESH_MS = 5000
LOG_LINES = 4


def _now():
    return rt._now()


def _clock_at(dt, now):
    return dt.strftime("%H:%M") if dt.date() == now.date() else dt.strftime("%m-%d %H:%M")


def _dur(seconds):
    m = int(round(float(seconds or 0) / 60))
    if m < 1:
        return "-"
    h, m = divmod(m, 60)
    return " ".join(p for p in (f"{h} h" if h else "", f"{m} min" if m else "") if p)


def _hm(delta):
    m = int(delta.total_seconds() // 60)
    return f"+{m // 60:02d}:{m % 60:02d}"


def _mant(m):
    m = float(m)
    return f"{m:.1f}" if round(m, 1) == m else f"{m:g}"


def _trig(o):
    hz = o.get("trigger_hz")
    if not hz:
        return ""
    hz = float(hz)
    return "   " + (f"{hz / 1000:g} kHz" if hz >= 1000 else f"{hz:g} Hz")


def _rot(o):
    flags, _err = rt.rotate_choice(o)
    return " / ".join(lab for _k, lab, a in rt.ROTATE if flags[a]) or "no PMT"


def _lasers(step):
    """'375 (118 mA) + 473 (120 mA)' of a laser step, the pulse current from the file the step reads."""
    try:
        ls = rt.step_lasers(step)
    except Exception:
        return ""
    return " + ".join(f"{wl[:-2]} ({p:g} mA)" if p is not None else f"{wl[:-2]} nm" for wl, _b, p in ls)


def _summary(step):
    """One line of a step's options, per kind."""
    o, k = step.get("options") or {}, step.get("kind")
    try:
        if k == "general_scan":
            axes = int(o.get("axes", 2))
            ax = {1: "X", 2: "X + Y"}.get(axes, f"{axes} axes")
            return (f"{_lasers(step) or 'no wavelength'}   {int(o.get('points', 46))} points   {ax}   "
                    f"{int(o.get('events', 300000)):,} ev{_trig(o)}   {_rot(o)}"
                    + (f"   Y at {o['y_start']}" if o.get("y_start") else ""))
        if k == "hk_scan":
            return (f"HK Digitizer   {_lasers(step) or 'no wavelength'}   {int(o.get('points', 46))} points   X + Y   "
                    f"{_rot(o)}")
        if k == "wavelength_scan":
            axes = int(o.get("axes", 2))
            ax = {1: "X", 2: "X + Y"}.get(axes, f"{axes} axes")
            wl = _lasers(step) or "no wavelength"
            return (f"{wl}   {int(o.get('points', 46))} points   {ax}   {int(o.get('events', 300000)):,} ev"
                    f"{_trig(o)}   {_rot(o)}")
        if k == "stability_qe":
            wl = _lasers(step) or "no wavelength"
            size = "until next step" if o.get("until_next_step") else f"x {rt.qe_cycles(o)} each"
            return (f"{wl}   {size}   every {float(o.get('interval_s', 60)):g} s   "
                    f"{int(o.get('events', 300000)):,} ev{_trig(o)}"
                    + (f"   {int(o['samples'])} smp" if o.get("samples") else ""))
        if k == "dark_rate":
            when = "until next step" if o.get("until_next_step", True) else f"x {int(o.get('cycles', 1))}"
            chans = o.get("channels") or [c for c, _ in CHANNELS]
            ch = " / ".join(lab for key, lab in CHANNELS if key in chans) or "no channel"
            return (f"every {float(o.get('every_min', 15)):g} min {when}   {ch}   {int(o.get('events', 100000)):,} ev"
                    f"   {rt.dark_thr(o):g} mV")
        if k == "hv_scan":
            offs = " / ".join(f"{float(v):+g}" if float(v) else "0" for v in o.get("offsets_v") or [])
            wl = _lasers(step) or str(o.get("wavelength") or rt.HVSCAN_WL)[:-2] + " nm"
            chs = o.get("channels")
            chs = list(rt.HVSCAN_CHANNELS) if chs is None else chs
            ch = " / ".join(lab for key, lab in CHANNELS if key in chs) or "no PMT"
            return (f"{ch}   {wl}   {offs or 'no offset'} V   {int(o.get('events', 100000)):,} ev{_trig(o)}   "
                    f"settle {_dur(o.get('settle_s', 180))}")
        if k == "hv_apply" and o.get("mode") == "gain_curve":
            per = sorted((str(c), v) for c, v in (o.get("gain_mantissa_ch") or {}).items() if v not in (None, ""))
            pc = ", ".join(f"Ch{c} {_mant(v)}" for c, v in per)
            return (f"Gain curve   {_mant(o.get('gain_mantissa', 1.0))} x 10^7" + (f" ({pc})" if pc else "")
                    + f"   gain delay {_dur(o.get('gain_delay_s', 180))}")
        if k == "hv_apply":
            mode = "fixed"
            volts = sorted(((str(c), v) for c, v in (o.get("volts") or {}).items() if v not in (None, "")))
            vt = ", ".join(f"Ch{c} {float(v):.0f} V" for c, v in volts) or "Configured HV"
            return f"{mode}   {vt}   gain delay {_dur(o.get('gain_delay_s', 180))}"
    except (TypeError, ValueError):
        pass
    return ", ".join(f"{a}={b}" for a, b in o.items())


def _short(line):
    """'[YYYY-mm-dd HH:MM:SS] msg' -> 'HH:MM:SS  msg'."""
    if line.startswith("[") and len(line) > 22 and line[20] == "]":
        return line[12:20] + "  " + line[22:]
    return line


def routine_log_lines(n=LOG_LINES, now=None):
    """The last n app-log lines of the runner ('Routine:'), newest last; yesterday's file fills up."""
    now = now or datetime.now()
    out = []
    for back in (0, 1):
        path = os.path.join(applog.LOG_DIR, f"log_{now - timedelta(days=back):%Y-%m-%d}.txt")
        try:
            with open(path, "rb") as f:
                f.seek(0, 2)
                f.seek(max(f.tell() - 65536, 0))
                text = f.read().decode("utf-8", "replace")
        except OSError:
            continue
        out = [ln for ln in text.splitlines() if "Routine:" in ln][-n:] + out
        if len(out) >= n:
            break
    return [_short(ln) for ln in out[-n:]]


def slot_chain(slots, origin):
    """The night's slot times in list order (each after the previous), for the night the origin belongs to."""
    out, t = [], origin - timedelta(hours=6)    # a start shortly after midnight still belongs to that night
    for hhmm in slots:
        t = rt._clock_target(hhmm, t)
        out.append(t)
    return out


def day_marks(v, now):
    """{step index: '+1d'} for clock-time steps due on a later date than the routine start (now when idle)."""
    if v["active"]:
        origin = rt._parse(v.get("started_at")) or now
        dues = {s["index"]: rt._parse(s.get("due")) for s in v["steps"]}
    else:
        origin = now
        fake = {"steps": [dict(s) for s in v["steps"]]}
        rt.assign_due(fake, now)
        dues = {s["index"]: rt._parse(s["due"]) for s in fake["steps"]}
    out = {}
    for s in v["steps"]:
        due = dues.get(s["index"])
        if s["start"] != rt.SEQUENTIAL and due is not None and (due.date() - origin.date()).days > 0:
            out[s["index"]] = f"+{(due.date() - origin.date()).days}d"
    return out


def timeline_model(v, now, running_left=None, units=None):
    """Bars (name, status, start, end, overrun_from), slot lines, estimated end and the overrun past the
    last slot. Without runtime stamps: a preview from now (due times and estimates as a start now would give)."""
    steps = [dict(s) for s in v["steps"] if s["enabled"]]
    has_run = any(s.get("started") or s.get("due") for s in steps)
    origin = (rt._parse(v.get("started_at")) if has_run else None) or now
    if not has_run:
        for s in steps:
            s.update(started=None, finished=None, due=None)
        fake = {"steps": steps}
        rt.assign_due(fake, now)
        for s in steps:
            s["estimate_s"] = rt.estimate_seconds(s, fake, now)
    chain = slot_chain(v["slots"], origin)
    last_slot = chain[-1] if chain else None
    slots = sorted({t + timedelta(days=d) for t in chain for d in (-1, 0, 1, 2)})
    bars, cursor, unit_map = [], origin, {}
    for s in steps:
        started, finished = rt._parse(s["started"]), rt._parse(s["finished"])
        if s["status"] == "Skipped" and started is None:
            bars.append((s["name"], s["status"], None, None, None))
            continue
        a = started or rt._parse(s["due"]) or cursor
        if started is None:
            a = max(a, cursor, now) if s["status"] == "Pending" else max(a, cursor)
        b = finished or a + timedelta(seconds=float(s.get("estimate_s") or 0))
        if s["status"] == "Running":
            b = max(b, now) if running_left is None else now + timedelta(seconds=running_left)
            if units:
                unit_map[len(bars)] = units
        ov = next((t for t in slots if t > a), None)
        bars.append((s["name"], s["status"], a, b, ov if ov is not None and b > ov else None))
        cursor = b
    end = max((b for _n, _s, _a, b, _o in bars if b), default=None)
    late = end - last_slot if end and last_slot and end > last_slot else None
    return {"origin": origin, "bars": bars, "slots": slots, "last_slot": last_slot, "end": end, "late": late,
            "units": unit_map,
            "est": {s["index"]: s.get("estimate_s") for s in steps}}


class Timeline(QWidget):
    ROW, TOP, BOTTOM, MARK = 22, 24, 38, 16
    UNIT_FILL = {"done": "done", "current": "active", "pending": "wait"}

    def _paint_units(self, p, units, xa, xb, y):
        """The running bar in equal cells per unit; a 1 px gap only where the cells are wide enough."""
        n = len(units)
        w = max(xb - xa, 3) / n
        gap = 1 if w >= 4 else 0
        for k, (state, label) in enumerate(units):
            r = QRectF(xa + k * w, y + 1, max(w - gap, 1), 16)
            color = theme.C["bad"] if state == "error" else theme.STATE[self.UNIT_FILL.get(state, "wait")][0]
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(color))
            p.drawRect(r)
            self._unit_rects.append((r, label))

    def mouseMoveEvent(self, e):
        from PyQt5.QtWidgets import QToolTip
        hit = next((label for r, label in getattr(self, "_unit_rects", []) if r.contains(e.pos())), None)
        if hit:
            QToolTip.showText(e.globalPos(), hit, self)
        else:
            QToolTip.hideText()

    def __init__(self):
        super().__init__()
        self.model, self.now = None, None
        self.setMinimumHeight(150)
        self.setMouseTracking(True)       # unit tooltips on hover

    def set_model(self, model, now):
        self.model, self.now = model, now
        rows = len(model["bars"]) if model else 0
        self.setMinimumHeight(max(150, self.TOP + rows * self.ROW + self.MARK + self.BOTTOM))
        self.update()

    def paintEvent(self, _e):
        C, m = theme.C, self.model
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        fm = QFontMetrics(self.font())
        if not m or not m["bars"]:
            p.setPen(QColor(C["hint"]))
            p.drawText(QRectF(0, 0, w, h), Qt.AlignCenter, "No enabled step.")
            return
        now, top, bottom = self.now, self.TOP, self.BOTTOM
        lw = min(max(fm.horizontalAdvance(b[0]) for b in m["bars"]) + 16, 220)
        x0, x1 = lw + 6, w - fm.horizontalAdvance("Overrun +00:00") - 16
        times = [t for b in m["bars"] for t in (b[2], b[3]) if t] + [m["origin"], now]   # up to the last step
        t_lo = min(times).replace(minute=0, second=0, microsecond=0)
        t_hi = max(times).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        span = (t_hi - t_lo).total_seconds() / 3600

        def X(t):
            return x0 + (t - t_lo).total_seconds() / 3600 / span * (x1 - x0)

        step = next((s for s in (1, 2, 3, 4, 6, 12) if (x1 - x0) / span * s >= 56), 24)
        t = t_lo
        while t <= t_hi:
            if t.hour % step == 0:
                x = int(X(t))
                p.setPen(QPen(QColor(C["border"]), 1))
                p.drawLine(x, top - 6, x, h - bottom)
                p.setPen(QColor(C["muted"]))
                p.drawText(x - fm.horizontalAdvance("00:00") // 2, h - bottom + 16, f"{t:%H:%M}")
            t += timedelta(hours=1)
        p.setPen(QColor(C["hint"]))
        p.drawText(QRectF(x0, h - 16, x1 - x0, 14), Qt.AlignCenter, "Time (HH:MM)")
        used = []
        for t in m["slots"]:
            if not t_lo <= t <= t_hi:
                continue
            x = int(X(t))
            p.setPen(QPen(QColor(C["hint"]), 1, Qt.DashLine))
            p.drawLine(x, 4, x, h - bottom)
            label = f"Slot {t:%H:%M}"
            tw = fm.horizontalAdvance(label)
            y = 13 if all(abs(x - u) > tw + 6 for u in used) else h - bottom - 4
            used.append(x)
            p.setPen(QColor(C["hint"]))
            p.drawText(x + 3, y, label)
        y = top
        self._unit_rects, self._unit_rects_rows = [], []
        for name, status, a, b, ov in m["bars"]:
            p.setPen(QColor(C["disabled"] if status == "Skipped" else C["text"]))
            p.drawText(6, y + 14, fm.elidedText(name, Qt.ElideRight, lw - 10))
            if a is not None:
                fill, _fg, border = theme.STATE[BAR_STATE.get(status, "wait")]
                xa, xb = X(a), X(b)
                xo = X(ov) if ov else xb
                p.setPen(QPen(QColor(border), 1))
                p.setBrush(QColor(fill))
                p.drawRoundedRect(QRectF(xa, y, max(xo - xa, 3), 18), 3, 3)
                units = (m.get("units") or {}).get(len(self._unit_rects_rows))
                if units:
                    self._paint_units(p, units, xa, xb, y)
                if ov:
                    p.setPen(QPen(QColor(C["warn"]), 1))
                    p.setBrush(QBrush(QColor(C["warn"]), Qt.BDiagPattern))
                    p.drawRect(QRectF(xo, y, max(xb - xo, 3), 18))
                    p.setPen(QColor(C["bad"]))
                    p.drawText(int(xb) + 4, y + 14, f"Overrun {_hm(b - ov)}")
            self._unit_rects_rows.append(name)
            y += self.ROW
        if t_lo <= now <= t_hi:
            x = int(X(now))
            p.setPen(QPen(QColor(C["accent"]), 2))
            p.drawLine(x, 4, x, h - bottom)
            p.setPen(QColor(C["accent"]))
            p.drawText(x + 4, h - bottom - 4, f"Now {now:%H:%M}")


class StepTree(QTreeWidget):
    """Steps tree whose rows can be dragged; the drop only reports (from, to) and the page rebuilds the
    routine, so Qt never moves rows (and their Options / Open buttons) itself."""
    moved = pyqtSignal(int, int)

    def __init__(self):
        super().__init__()
        self.can_drag = lambda: True
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)

    def startDrag(self, actions):
        if self.can_drag():
            super().startDrag(actions)

    @staticmethod
    def target(src, row, below, n):
        """Index the dragged row ends up at: dropped above (below=False) or below row; row None = after the last."""
        t = n if row is None else row + (1 if below else 0)
        return t - 1 if t > src else t

    def dropEvent(self, e):
        src = self.indexOfTopLevelItem(self.currentItem())
        it = self.itemAt(e.pos())
        n = self.topLevelItemCount()
        if it is None:
            row, below = None, True
        else:
            row = self.indexOfTopLevelItem(it)
            below = (self.dropIndicatorPosition() == QAbstractItemView.BelowItem
                     or e.pos().y() > self.visualItemRect(it).center().y())
        e.ignore()                  # no Qt-side move; the page rebuilds the rows
        if 0 <= src < n:
            to = self.target(src, row, below, n)
            if to != src:
                self.moved.emit(src, to)


def _int(text, label, lo=1):
    try:
        v = int(str(text).strip())
    except ValueError:
        raise ValueError(f"{label}: integer required.")
    if v < lo:
        raise ValueError(f"{label}: must be >= {lo}.")
    return v


def _float(text, label, lo=0.0):
    try:
        v = float(str(text).strip())
    except ValueError:
        raise ValueError(f"{label}: number required.")
    if v < lo:
        raise ValueError(f"{label}: must be >= {lo:g}.")
    return v


class _Form(QWidget):
    """The option fields of one step kind; values() validates and returns the options dict."""

    def __init__(self, kind, options=None):
        super().__init__()
        self.kind = kind
        o = dict(DEFAULT_OPTIONS.get(kind, {}))
        o.update(options or {})
        lay = QFormLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.f = {}

        def line(key, label, value):
            e = QLineEdit(str(value))
            self.f[key] = e
            lay.addRow(label, e)

        def gain(key, label, value):
            row = QHBoxLayout()
            e = QLineEdit("" if value in (None, "") else f"{float(value):g}")
            e.setMaximumWidth(90)
            self.f[key] = e
            row.addWidget(e)
            row.addWidget(QLabel("x 10^7"))
            row.addStretch(1)
            lay.addRow(label, row)

        if kind in ("general_scan", "hk_scan"):
            self.f["wavelength"] = QComboBox()
            self.f["wavelength"].addItem("Laser Sequence", "")
            wl, keys = rt.scan_wavelength(o), rt.wavelength_keys()
            for w in keys + ([wl] if wl and wl not in keys else []):
                self.f["wavelength"].addItem(w[:-2] if w.endswith("nm") else w, w)
            self.f["wavelength"].setCurrentIndex(max(0, self.f["wavelength"].findData(wl)))
            lay.addRow("Wavelength (nm)", self.f["wavelength"])
            if kind == "general_scan":   # HK readout: events and trigger rate come from the HK configuration
                line("events", "Events", o.get("events", 300000))
                self._trigger_row(line, o)
            self._grid_rows(line, o)
            line("angles", "Angles (deg)", ", ".join(str(int(a)) for a in o.get("angles") or []))
            self.f["angles"].setPlaceholderText("Blank = scan grid; a list must include -55, 0, 55")
            self.f["angles"].editingFinished.connect(self._angles_edited)
            if kind == "general_scan":
                line("y_start", "Y axis start (HH:MM)", o.get("y_start") or "")
                self.f["y_start"].setPlaceholderText("Blank = no wait before the Y axis")
            self._rotate_row(lay, o)
        elif kind in ("wavelength_scan", "stability_qe"):
            row = QHBoxLayout()
            on = o.get("wavelengths") or []
            for wl in WAVELENGTHS:
                cb = QCheckBox(wl[:-2])
                cb.setChecked(wl in on)
                self.f[wl] = cb
                row.addWidget(cb)
            row.addStretch(1)
            lay.addRow("Wavelengths (nm)", row)
            line("events", "Events", o.get("events", 300000))
            if kind == "wavelength_scan":
                self._trigger_row(line, o)
                self._grid_rows(line, o)
                self._rotate_row(lay, o)
            else:
                self._trigger_row(line, o)
                self.f["until_next_step"] = QCheckBox()
                self.f["until_next_step"].setChecked(bool(o.get("until_next_step")))
                lay.addRow("Run until next step", self.f["until_next_step"])
                line("cycles", "Cycles", rt.qe_cycles(o))
                self.f["cycles"].setReadOnly(self.f["until_next_step"].isChecked())   # unused while the window sets the count
                self.f["until_next_step"].toggled.connect(self.f["cycles"].setReadOnly)
                line("interval_s", "Interval (s)", o.get("interval_s", 60))
                line("samples", "Samples (window)", o.get("samples") or "")
                self.f["samples"].setPlaceholderText("Blank = configured window")
        elif kind == "dark_rate":
            line("every_min", "Every (min)", o.get("every_min", 15))
            self.f["until_next_step"] = QCheckBox()
            self.f["until_next_step"].setChecked(bool(o.get("until_next_step", True)))
            lay.addRow("Run until next step", self.f["until_next_step"])
            line("cycles", "Cycles", o.get("cycles", 1))
            self.f["cycles"].setReadOnly(self.f["until_next_step"].isChecked())   # unused while the window sets the count
            self.f["until_next_step"].toggled.connect(self.f["cycles"].setReadOnly)
            line("events", "Events", o.get("events", 100000))
            line("thr_mv", "Threshold (mV)", f"{rt.dark_thr(o):g}")
            row = QHBoxLayout()
            on = o.get("channels") or [c for c, _ in CHANNELS]
            for key, lab in CHANNELS:
                cb = QCheckBox(lab)
                cb.setChecked(key in on)
                self.f[key] = cb
                row.addWidget(cb)
            row.addStretch(1)
            lay.addRow("Channels", row)
        elif kind == "hv_scan":
            self.f["wavelength"] = QComboBox()
            for wl in WAVELENGTHS:
                self.f["wavelength"].addItem(wl[:-2], wl)
            self.f["wavelength"].setCurrentIndex(max(0, self.f["wavelength"].findData(o.get("wavelength") or rt.HVSCAN_WL)))
            lay.addRow("Wavelength (nm)", self.f["wavelength"])
            row = QHBoxLayout()
            on = o.get("channels")
            on = list(rt.HVSCAN_CHANNELS) if on is None else on
            for key, lab in CHANNELS:
                cb = QCheckBox(lab)
                cb.setChecked(key in on)
                self.f["hv_" + key] = cb
                row.addWidget(cb)
            row.addStretch(1)
            lay.addRow("PMTs", row)
            line("offsets_v", "Offsets (V)", ", ".join(f"{float(v):g}" for v in o.get("offsets_v") or []))
            lim = rt._hv_limits()
            if lim is not None:
                note = QLabel(f"Max {float(lim.get('max_step_v', 250)):.0f} V per HV change; larger changes go in steps")
                note.setStyleSheet(f"color: {theme.C['muted']};")
                lay.addRow("", note)
            line("events", "Events", o.get("events", 100000))
            self._trigger_row(line, o)
            line("settle_s", "Settle (s)", o.get("settle_s", 180))
            gain("gain_mantissa", "Target gain", o.get("gain_mantissa", 1.0))
            self.f["fit_mode"] = QComboBox()
            for m, text in rt.FIT_MODES.items():
                self.f["fit_mode"].addItem(text, m)
            self.f["fit_mode"].setCurrentIndex(max(0, self.f["fit_mode"].findData(int(o.get("fit_mode", 2)))))
            lay.addRow("Fit mode", self.f["fit_mode"])
            line("fit_timeout_s", "Fit timeout (s)", f"{float(o.get('fit_timeout_s', rt.FIT_TIMEOUT_S)):g}")
        elif kind == "hv_apply":
            self.f["mode"] = QComboBox()
            self.f["mode"].addItem("Fixed", "fixed")
            self.f["mode"].addItem("Gain curve", "gain_curve")
            self.f["mode"].setCurrentIndex(1 if o.get("mode") == "gain_curve" else 0)
            lay.addRow("Mode", self.f["mode"])
            volts = o.get("volts") or {}
            for ch in range(3):
                v = volts.get(str(ch), volts.get(ch, ""))
                line(f"ch{ch}", f"Ch{ch} (V)", "" if v in (None, "") else f"{float(v):g}")
                self.f[f"ch{ch}"].setPlaceholderText("Blank = Configured HV")
            gain("gain_mantissa", "Target gain", o.get("gain_mantissa", 1.0))
            per = o.get("gain_mantissa_ch") or {}
            for ch in range(3):
                gain(f"gm{ch}", f"Ch{ch} target", per.get(str(ch), per.get(ch, "")))
            line("max_age_h", "Reference from latest HV scan (hours)", f"{float(o.get('max_age_h', 24)):g}")
            line("gain_delay_s", "Gain delay (s)", o.get("gain_delay_s", 180))
            self.f["restore_hv"] = QCheckBox()
            self.f["restore_hv"].setChecked(bool(o.get("restore_hv", True)))
            lay.addRow("Restore default HV when routine ends", self.f["restore_hv"])
            self.f["on_unusable"] = QComboBox()
            self.f["on_unusable"].addItem("Use default HV and continue", "configured")
            self.f["on_unusable"].addItem("Stop routine", "stop")
            self.f["on_unusable"].setCurrentIndex(max(0, self.f["on_unusable"].findData(o.get("on_unusable", "configured"))))
            lay.addRow("If gain curve fit is unusable", self.f["on_unusable"])
            self.f["mode"].currentIndexChanged.connect(self._mode_changed)
            self._mode_changed()
        self._est_row(lay)

    def _est_row(self, lay):
        self.est = QLabel("-")
        self.est.setObjectName("value")
        lay.addRow("Est. Dur.", self.est)
        self.estimator = lambda opts: rt.estimate_seconds({"kind": self.kind, "name": "", "options": opts})
        self.window_fn = None
        for w in self.f.values():
            sig = (getattr(w, "textChanged", None) if isinstance(w, QLineEdit) else
                   getattr(w, "toggled", None) if isinstance(w, QCheckBox) else
                   getattr(w, "currentIndexChanged", None))
            if sig is not None:
                sig.connect(lambda *_a: self.update_est())
        self.update_est()

    def set_estimator(self, fn):
        """fn(options) -> seconds; the page passes one that knows the step's place in the routine."""
        self.estimator = fn
        self.update_est()

    def set_window(self, fn):
        """fn(options) -> (start, end) datetimes of the step in the routine timeline, or None."""
        self.window_fn = fn
        self.update_est()

    def update_est(self):
        try:
            opts = self.values()
            sec = self.estimator(opts)
        except Exception:
            self.est.setText("-")
            return
        text = _dur(sec) if sec else "-"
        try:
            win = self.window_fn(opts) if self.window_fn else None
        except Exception:
            win = None
        if win:
            now = rt._now()
            text += f"   {_clock_at(win[0], now)} - {_clock_at(win[1], now)}"
        if self.kind == "general_scan":   # when the X axis ends, to choose the Y axis start
            try:
                x = rt.x_axis_seconds(opts)
            except Exception:
                x = None
            if x and win:
                x_done, y_ready = (win[0] + timedelta(seconds=s) for s in x)
                text += f"\nX axis done about {_clock_at(x_done, now)}; Y axis can start from about {_clock_at(y_ready, now)}"
            elif x:
                text += f"\nX axis done after about {_dur(x[0])}; Y axis can start after about {_dur(x[1])}"
        self.est.setText(text)

    def _mode_changed(self, _i=None):
        gc = self.f["mode"].currentData() == "gain_curve"
        for ch in range(3):
            self.f[f"ch{ch}"].setEnabled(not gc)
            self.f[f"gm{ch}"].setEnabled(gc)
        self.f["gain_mantissa"].setEnabled(gc)
        self.f["on_unusable"].setEnabled(gc)
        self.f["max_age_h"].setEnabled(gc)

    @staticmethod
    def _mantissa(text, label):
        v = _float(text, label)
        if v <= 0:
            raise ValueError(f"{label}: must be > 0.")
        return v

    def _trigger_row(self, line, o):
        hz = o.get("trigger_hz")
        line("trigger_hz", "Trigger rate (Hz)", f"{float(hz):g}" if hz else "")
        self.f["trigger_hz"].setPlaceholderText("unchanged")

    def _trigger_value(self):
        t = self.f["trigger_hz"].text().strip()
        if not t:
            return None
        v = _float(t, "Trigger rate (Hz)", lo=1.0)
        if v > 1e6:
            raise ValueError("Trigger rate (Hz): must be <= 1000000.")
        return v

    def _thr_value(self):
        lo, hi = rt.DARK_THR_RANGE
        v = _float(self.f["thr_mv"].text(), "Threshold (mV)", lo=lo)
        if v > hi:
            raise ValueError(f"Threshold (mV): must be <= {hi:g}.")
        return v

    def _rotate_row(self, lay, o):
        row = QHBoxLayout()
        flags, _err = rt.rotate_choice(o)
        for key, lab, attr in rt.ROTATE:
            cb = QCheckBox(lab)
            cb.setChecked(flags[attr])
            self.f["rotate_" + key] = cb
            row.addWidget(cb)
        row.addStretch(1)
        lay.addRow("Rotate", row)

    def _rotate_value(self):
        on = [key for key, _l, _a in rt.ROTATE if self.f["rotate_" + key].isChecked()]
        if not on:
            raise ValueError("Rotate: select Rot1 or Rot2.")
        return on

    def _grid_rows(self, line, o):
        """Points/Axes come from the scan engine's angle grid, so they are shown read-only."""
        points, axes = rt._grid(o)
        line("points", "Points", points)
        line("axes", "Axes", axes)
        for key in ("points", "axes"):
            self.f[key].setReadOnly(True)

    def _angles_value(self):
        """Tilt angle list of the step ([] = the scan grid); raises ValueError on a bad list."""
        parts = [t for t in re.split(r"[,\s]+", self.f["angles"].text().strip()) if t]
        angles, err = rt.scan_angles({"angles": parts})
        if err:
            raise ValueError(err + ".")
        return angles or []

    def _angles_edited(self):
        try:
            self.f["points"].setText(str(rt._grid({"angles": self._angles_value()})[0]))
        except ValueError:
            pass

    def values(self):
        f, k = self.f, self.kind
        if k == "hk_scan":
            angles = self._angles_value()
            out = {"points": _int(f["points"].text(), "Points"), "axes": _int(f["axes"].text(), "Axes"),
                   "rotate": self._rotate_value()}
            if angles:
                out["angles"], out["points"] = angles, 2 * len(angles)
            if f["wavelength"].currentData():
                out["wavelength"] = f["wavelength"].currentData()
            return out
        if k == "general_scan":
            angles = self._angles_value()
            out = {"events": _int(f["events"].text(), "Events"), "points": _int(f["points"].text(), "Points"),
                   "axes": _int(f["axes"].text(), "Axes"), "rotate": self._rotate_value(),
                   "trigger_hz": self._trigger_value()}
            if angles:
                out["angles"], out["points"] = angles, 2 * len(angles)
            if f["wavelength"].currentData():
                out["wavelength"] = f["wavelength"].currentData()
            y = f["y_start"].text().strip()
            if y:
                if rt._clock(y) is None:
                    raise ValueError("Y axis start: use HH:MM (24 h).")
                out["y_start"] = "%02d:%02d" % rt._clock(y)
            return out
        if k in ("wavelength_scan", "stability_qe"):
            wls = [wl for wl in WAVELENGTHS if f[wl].isChecked()]
            if not wls:
                raise ValueError("Wavelengths: select at least one.")
            if k == "wavelength_scan":
                return {"wavelengths": wls, "events": _int(f["events"].text(), "Events"),
                        "points": _int(f["points"].text(), "Points"), "axes": _int(f["axes"].text(), "Axes"),
                        "rotate": self._rotate_value(), "trigger_hz": self._trigger_value()}
            smp = f["samples"].text().strip()
            return {"wavelengths": wls, "events": _int(f["events"].text(), "Events"),
                    "cycles": _int(f["cycles"].text(), "Cycles"), "until_next_step": f["until_next_step"].isChecked(),
                    "interval_s": _float(f["interval_s"].text(), "Interval (s)"), "trigger_hz": self._trigger_value(),
                    "samples": _int(smp, "Samples (window)", lo=16) if smp else None}
        if k == "dark_rate":
            return {"every_min": _int(f["every_min"].text(), "Every (min)"),
                    "until_next_step": f["until_next_step"].isChecked(),
                    "cycles": _int(f["cycles"].text(), "Cycles"), "events": _int(f["events"].text(), "Events"),
                    "channels": [key for key, _ in CHANNELS if f[key].isChecked()],
                    "thr_mv": self._thr_value()}
        if k == "hv_scan":
            parts = [t for t in re.split(r"[,\s]+", f["offsets_v"].text().strip()) if t]
            offs = [_float(t, "Offsets (V)", lo=-1e9) for t in parts]
            if not offs:
                raise ValueError("Offsets (V): at least one value required.")
            chs = [key for key, _l in CHANNELS if f["hv_" + key].isChecked()]
            if not chs:
                raise ValueError("PMTs: select at least one.")
            return {"wavelength": f["wavelength"].currentData(), "channels": chs, "offsets_v": offs,
                    "events": _int(f["events"].text(), "Events"),
                    "settle_s": _float(f["settle_s"].text(), "Settle (s)"),
                    "gain_mantissa": self._mantissa(f["gain_mantissa"].text(), "Target gain"),
                    "fit_mode": f["fit_mode"].currentData(),
                    "fit_timeout_s": self._mantissa(f["fit_timeout_s"].text(), "Fit timeout (s)"),
                    "trigger_hz": self._trigger_value()}
        volts = {}
        for ch in range(3):
            t = f[f"ch{ch}"].text().strip()
            if t:
                volts[str(ch)] = _float(t, f"Ch{ch} (V)")
        per = {}
        for ch in range(3):
            t = f[f"gm{ch}"].text().strip()
            if t:
                per[str(ch)] = self._mantissa(t, f"Ch{ch} target")
        return {"mode": f["mode"].currentData(), "volts": volts,
                "gain_delay_s": _float(f["gain_delay_s"].text(), "Gain delay (s)"),
                "gain_mantissa": self._mantissa(f["gain_mantissa"].text(), "Target gain"),
                "gain_mantissa_ch": per, "accept_provisional": False,
                "max_age_h": self._mantissa(f["max_age_h"].text(), "Reference from latest HV scan (hours)"),
                "restore_hv": f["restore_hv"].isChecked(), "on_unusable": f["on_unusable"].currentData()}


class _StartRow(QWidget):
    """Start: 'At HH:MM' or 'Sequential'."""

    def __init__(self, start=rt.SEQUENTIAL):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.at = QRadioButton("At")
        self.time = QTimeEdit(QTime(23, 0))
        self.time.setDisplayFormat("HH:mm")
        self.seq = QRadioButton("Sequential")
        self.seq.setToolTip("Starts immediately after previous enabled step finishes.")
        self.at.setToolTip("Starts at scheduled clock time (next day if past).")
        for w in (self.at, self.time, self.seq):
            row.addWidget(w)
        row.addStretch(1)
        if start == rt.SEQUENTIAL:
            self.seq.setChecked(True)
        else:
            self.at.setChecked(True)
            self.time.setTime(QTime.fromString(start, "HH:mm"))
        self.at.toggled.connect(self.time.setEnabled)
        self.time.setEnabled(self.at.isChecked())

    def value(self):
        return self.time.time().toString("HH:mm") if self.at.isChecked() else rt.SEQUENTIAL


def _buttons(dlg, form):
    box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    box.accepted.connect(dlg.accept)
    box.rejected.connect(dlg.reject)
    form.addRow(box)


class AddStepDialog(QDialog):
    """Add Step...: kind, name, enabled, start; the options start from the kind's defaults."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Add Step")
        self.setWindowFlags(self.windowFlags() | Qt.WindowMinMaxButtonsHint)
        self._result = None
        form = QFormLayout(self)
        self.kind = QComboBox()
        for k in rt.KINDS:
            self.kind.addItem(KIND_NAMES[k], k)
        form.addRow("Kind", self.kind)
        self.name = QLineEdit(KIND_NAMES[rt.KINDS[0]])
        form.addRow("Name", self.name)
        self.enabled = QCheckBox()
        self.enabled.setChecked(True)
        form.addRow("Enabled", self.enabled)
        self.start = _StartRow("23:00")
        form.addRow("Start", self.start)
        self.wls = QWidget()
        row = QHBoxLayout(self.wls)
        row.setContentsMargins(0, 0, 0, 0)
        self.wl_boxes = {}
        on = (DEFAULT_OPTIONS.get("wavelength_scan") or {}).get("wavelengths") or []
        for wl in rt.wavelength_keys():
            cb = QCheckBox(wl[:-2] if wl.endswith("nm") else wl)
            cb.setChecked(wl in on)
            self.wl_boxes[wl] = cb
            row.addWidget(cb)
        row.addStretch(1)
        form.addRow("Wavelengths (nm)", self.wls)
        _buttons(self, form)
        self.kind.currentIndexChanged.connect(self._kind_changed)
        self._kind_changed(self.kind.currentIndex())

    def _kind_changed(self, i):
        if self.name.text().strip() in KIND_NAMES.values():
            self.name.setText(KIND_NAMES[rt.KINDS[i]])
        multi = self.kind.itemData(i) == "wavelength_scan"
        self.wls.setVisible(multi)
        lab = self.layout().labelForField(self.wls)
        if lab is not None:
            lab.setVisible(multi)

    def accept(self):
        name = self.name.text().strip()
        if not name:
            QMessageBox.warning(self, "Add Step", "Name: required.")
            return
        kind = self.kind.currentData()
        opts = copy.deepcopy(DEFAULT_OPTIONS.get(kind, {}))
        if kind == "wavelength_scan":           # added as one General Scan step per wavelength
            opts["wavelengths"] = [wl for wl, cb in self.wl_boxes.items() if cb.isChecked()]
            if not opts["wavelengths"]:
                QMessageBox.warning(self, "Add Step", "Wavelengths: select at least one.")
                return
        first = rt.new_step(kind, name, self.start.value(), opts, self.enabled.isChecked())
        self._result = rt.split_wavelength_steps([first])[0]
        super().accept()

    def steps(self):
        return list(self._result or [])

    def step(self):
        return (self._result or [None])[0]


class OptionsDialog(QDialog):
    """Options... of one step: name, start and the options of its kind."""

    def __init__(self, parent, step):
        super().__init__(parent)
        label = KIND_NAMES[step["kind"]]
        self.setWindowFlags(self.windowFlags() | Qt.WindowMinMaxButtonsHint)
        self.setWindowTitle(f"Options: {step['name']}" + ("" if step["name"] == label else f" ({label})"))
        self._step = step
        self._result = None
        self.setMinimumWidth(560)
        form = QFormLayout(self)
        kind = QLabel(f"<b>{KIND_NAMES[step['kind']]}</b>")
        form.addRow("Kind", kind)
        hint = QLabel(KIND_HELP[step["kind"]])
        hint.setWordWrap(True)
        form.addRow("", hint)
        self.name = QLineEdit(step["name"])
        form.addRow("Name", self.name)
        self.start = _StartRow(step["start"])
        form.addRow("Start", self.start)
        self.form = _Form(step["kind"], step["options"])
        form.addRow(self.form)
        self.start.at.toggled.connect(lambda *_a: self.form.update_est())
        self.start.time.timeChanged.connect(lambda *_a: self.form.update_est())
        _buttons(self, form)

    def read_only(self):
        for w in (self.name, self.start, self.form):
            w.setEnabled(False)
        box = self.findChild(QDialogButtonBox)
        if box is not None:
            box.setStandardButtons(QDialogButtonBox.Close)
            box.rejected.connect(self.reject)

    def accept(self):
        try:
            name = self.name.text().strip()
            if not name:
                raise ValueError("Name: required.")
            s = dict(self._step, name=name, start=self.start.value(), options=self.form.values())
            self._result = rt.normalize_step(s)
        except ValueError as e:
            QMessageBox.warning(self, "Options", str(e))
            return
        super().accept()

    def step(self):
        return self._result


class SlotsDialog(QDialog):
    """Slots...: the slot times in order (timeline lines, overrun and the pre-run check)."""

    def __init__(self, parent, slots):
        super().__init__(parent)
        self.setWindowTitle("Slots")
        self._result = None
        form = QFormLayout(self)
        self.table = QTableWidget(0, 1)
        self.table.setHorizontalHeaderLabels(["Time"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setMinimumHeight(200)
        form.addRow(self.table)
        row = QHBoxLayout()
        self.add_btn = QPushButton("Add")
        self.add_btn.clicked.connect(lambda: self.add())
        self.remove_btn = QPushButton("Remove")
        self.remove_btn.clicked.connect(lambda: self.remove())
        row.addWidget(self.add_btn)
        row.addWidget(self.remove_btn)
        row.addStretch(1)
        form.addRow(row)
        _buttons(self, form)
        for t in slots:
            self.add(t)

    def add(self, hhmm=None):
        if hhmm is None:
            last = self.values()[-1:]
            hhmm = QTime.fromString(last[0], "HH:mm").addSecs(3600).toString("HH:mm") if last else "23:00"
        edit = QTimeEdit(QTime.fromString(hhmm, "HH:mm"))
        edit.setDisplayFormat("HH:mm")
        for w in [edit] + edit.findChildren(QLineEdit):
            w.installEventFilter(self)      # focusing a time edit selects its row (for Remove)
        n = self.table.rowCount()
        self.table.insertRow(n)
        self.table.setCellWidget(n, 0, edit)
        self.table.setCurrentCell(n, 0)

    def eventFilter(self, obj, ev):
        if ev.type() == ev.FocusIn:
            for i in range(self.table.rowCount()):
                w = self.table.cellWidget(i, 0)
                if w is obj or (w is not None and w.isAncestorOf(obj)):
                    self.table.setCurrentCell(i, 0)
        return super().eventFilter(obj, ev)

    def remove(self, row=None):
        row = self.table.currentRow() if row is None else row
        if 0 <= row < self.table.rowCount():
            self.table.removeRow(row)

    def values(self):
        return [self.table.cellWidget(i, 0).time().toString("HH:mm") for i in range(self.table.rowCount())]

    def accept(self):
        self._result = self.values()
        super().accept()

    def slots(self):
        return self._result


class _Bus(QObject):
    changed = pyqtSignal()


class _DryBus(QObject):
    done = pyqtSignal(object, object)   # (kind "summary" / "test", result)


class PreRunDialog(QDialog):
    """Pre-run Check: summary, then the dummy test per step; Start only after a test without errors.
    check_only: Close instead of Start, nothing can start the routine."""
    LEVEL_COLOR = {"error": "bad", "warn": "warn", "ok": "text"}

    def __init__(self, parent=None, check_only=False):
        super().__init__(parent)
        self.check_only = check_only
        self.setWindowTitle("Pre-run Check")
        self.setWindowFlags(self.windowFlags() | Qt.WindowMinMaxButtonsHint)
        self.setMinimumSize(760, 520)
        self.result_ = None
        self._running = False
        lay = QVBoxLayout(self)
        self.summary = QTreeWidget()
        self.summary.setHeaderHidden(True)
        self.summary.setRootIsDecorated(False)
        self.summary.setMaximumHeight(180)
        lay.addWidget(self.summary)
        row = QHBoxLayout()
        self.test_btn = QPushButton("Pre-run dummy test")
        theme.role(self.test_btn, "accent")
        self.test_btn.clicked.connect(lambda: self.run_test())
        row.addWidget(self.test_btn)
        self.verdict = QLabel("")
        row.addWidget(self.verdict)
        row.addStretch(1)
        lay.addLayout(row)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        lay.addWidget(self.tree, 1)
        box = QHBoxLayout()
        box.addStretch(1)
        self.start_btn = None
        if not check_only:
            self.start_btn = QPushButton("Start")
            theme.role(self.start_btn, "start")
            self.start_btn.setEnabled(False)
            self.start_btn.clicked.connect(self.accept)
            box.addWidget(self.start_btn)
        self.cancel_btn = QPushButton("Close" if check_only else "Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        box.addWidget(self.cancel_btn)
        lay.addLayout(box)
        self.bus = _DryBus()
        self.bus.done.connect(self._done, Qt.QueuedConnection)
        self.sum_spin, self.tree_spin = Spinner(self.summary, 40), Spinner(self.tree)
        self.sum_spin.start()
        self._spawn("summary", rt.precheck)

    def _spawn(self, kind, fn):
        def work():
            try:
                res = fn()
            except Exception as e:
                res = e
            try:
                self.bus.done.emit(kind, res)
            except RuntimeError:   # dialog already closed
                pass
        threading.Thread(target=work, daemon=True, name="routine-dry-run").start()

    def run_test(self, sync=False):
        """Run rt.dry_run() in a worker thread (sync=True: here, for tests)."""
        if self._running:
            return
        self._running = True
        if self.start_btn is not None:
            self.start_btn.setEnabled(False)
        self.test_btn.setEnabled(False)
        self.verdict.setText("Running...")
        if sync:
            try:
                res = rt.dry_run()
            except Exception as e:
                res = e
            self._done("test", res)
        else:
            self.tree_spin.start()
            self._spawn("test", rt.dry_run)

    def _item(self, parent, level, text):
        it = QTreeWidgetItem([text])
        it.setToolTip(0, text)   # long lines are elided in the tree
        it.setForeground(0, QColor(theme.C[self.LEVEL_COLOR.get(level, "text")]))
        it.setData(0, Qt.UserRole, level)
        (parent.addChild(it) if isinstance(parent, QTreeWidgetItem) else parent.addTopLevelItem(it))
        return it

    def _fill_summary(self, lines):
        self.summary.clear()
        for level, text in lines:
            self._item(self.summary, level, text)

    def _done(self, kind, res):
        if isinstance(res, Exception):
            res = {"steps": [], "summary": [("error", f"{type(res).__name__}: {res}")]} if kind == "test" \
                else [("error", f"{type(res).__name__}: {res}")]
        mode = "check-only" if self.check_only else "start"
        (self.sum_spin if kind == "summary" else self.tree_spin).stop()
        if kind == "summary":
            if self.result_ is None:
                self._fill_summary(res)
                rt.set_last_check(mode, res)
            return
        self._running = False
        self.test_btn.setEnabled(True)
        self.result_ = res
        rt.set_last_check(mode, res["summary"], res["steps"])
        self._fill_summary(res["summary"])
        self.tree.clear()
        for s in res["steps"]:
            parent = self._item(self.tree, "ok", f"{s['start']:%m-%d %H:%M} - {s['end']:%H:%M}  {s['name']}")
            worst = "ok"
            for level, text in s["lines"]:
                self._item(parent, level, text)
                worst = "error" if level == "error" or worst == "error" else level if level == "warn" else worst
            parent.setForeground(0, QColor(theme.C[self.LEVEL_COLOR[worst]]))
            parent.setExpanded(True)
        errors, warns = self.counts()
        self.verdict.setText(f"{errors} error(s), {warns} warning(s)")
        self.verdict.setStyleSheet(f"color: {theme.C['bad' if errors else 'warn' if warns else 'good']};")
        if self.start_btn is not None:
            self.start_btn.setEnabled(errors == 0)

    def counts(self):
        """(errors, warnings) of the last dummy test."""
        if not self.result_:
            return 0, 0
        lines = list(self.result_["summary"]) + [ln for s in self.result_["steps"] for ln in s["lines"]]
        return sum(lv == "error" for lv, _t in lines), sum(lv == "warn" for lv, _t in lines)


def issues_text(n):
    """Alarm button tooltip from issue_counts()."""
    words = (("Error", "error"), ("Warning", "warning"), ("Note", "note"))
    parts = [f"{n[k]} {w}{'' if n[k] == 1 else 's'}" for k, w in words if n.get(k)]
    return " | ".join(parts) if parts else "No alarms"


def issues_role(n):
    """The most urgent level: red error, orange warning or note, green when clear."""
    return "danger" if n.get("Error") else "warn" if n.get("Warning") or n.get("Note") else "start"


class IssuesDialog(QDialog):
    """Routine issues and the last Pre-run Check; a view, rebuilt on every open."""
    LEVEL_COLOR = {"Error": "bad", "Warning": "warn", "Note": "text", "error": "bad", "warn": "warn", "ok": "text"}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Routine Issues")
        self.setWindowFlags(self.windowFlags() | Qt.WindowMinMaxButtonsHint)
        self.setModal(False)
        self.setMinimumSize(860, 500)
        self.on_ack = None
        self._sig = None
        lay = QVBoxLayout(self)
        self.tabs = QTabWidget()
        steps = QWidget()
        v = QVBoxLayout(steps)
        row = QHBoxLayout()
        self.errors_only = QCheckBox("Show errors only")
        self.errors_only.toggled.connect(lambda _on: self.rebuild())
        row.addWidget(self.errors_only)
        row.addStretch(1)
        self.copy_btn = QPushButton("Copy")
        self.copy_btn.clicked.connect(self.copy)
        self.ack_btn = QPushButton("Acknowledge")
        self.ack_btn.clicked.connect(self.acknowledge)
        row.addWidget(self.copy_btn)
        row.addWidget(self.ack_btn)
        v.addLayout(row)
        self.tree = QTableWidget(0, 4)
        self.tree.setHorizontalHeaderLabels(["Time", "Step", "Level", "Message"])
        self.tree.verticalHeader().setVisible(False)
        self.tree.setWordWrap(True)
        self.tree.setTextElideMode(Qt.ElideNone)
        self.tree.setShowGrid(False)
        self.tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        hd = self.tree.horizontalHeader()
        hd.setStretchLastSection(True)
        hd.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        for c in range(3):
            hd.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hd.sectionResized.connect(lambda *_a: self.tree.resizeRowsToContents())
        v.addWidget(self.tree)
        QShortcut(QKeySequence.Copy, self.tree, activated=lambda: self.copy(selected=True))
        self.tabs.addTab(steps, "Steps")
        chk = QWidget()
        v2 = QVBoxLayout(chk)
        self.check_head = QLabel("")
        v2.addWidget(self.check_head)
        self.check_tree = QTreeWidget()
        self.check_tree.setHeaderHidden(True)
        v2.addWidget(self.check_tree)
        self.tabs.addTab(chk, "Last Pre-run Check")
        lay.addWidget(self.tabs)
        box = QHBoxLayout()
        box.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        box.addWidget(close)
        lay.addLayout(box)

    def _paint(self, it, key, cols):
        col = QColor(theme.C[key])
        for c in range(cols):
            it.setForeground(c, col)

    def rebuild(self):
        items, chk = rt.issues(), rt.last_check
        sig = (json.dumps(items), self.errors_only.isChecked(), json.dumps(chk))
        if sig == self._sig:
            return
        self._sig = sig
        self.tree.setRowCount(0)
        for d in items:
            if self.errors_only.isChecked() and d["level"] != "Error":
                continue
            row = self.tree.rowCount()
            self.tree.insertRow(row)
            col = QColor(theme.C["muted" if d["acked"] else self.LEVEL_COLOR[d["level"]]])
            for c, text in enumerate((d["time"] or "-", d["step"], d["level"], d["message"])):
                it = QTableWidgetItem(text)
                it.setForeground(col)
                self.tree.setItem(row, c, it)
        self.tree.resizeRowsToContents()
        self.check_tree.clear()
        self.check_head.setText(f"{chk['time']}   {chk['mode']}" if chk else "")
        if chk:
            for lv, t in chk["summary"]:
                it = QTreeWidgetItem([t])
                self._paint(it, self.LEVEL_COLOR.get(lv, "text"), 1)
                self.check_tree.addTopLevelItem(it)
            for s in chk["steps"]:
                parent = QTreeWidgetItem([f"{(s['start'] or '')[5:16]} - {(s['end'] or '')[11:16]}  {s['name']}"])
                worst = "ok"
                for lv, t in s["lines"]:
                    it = QTreeWidgetItem([t])
                    self._paint(it, self.LEVEL_COLOR.get(lv, "text"), 1)
                    parent.addChild(it)
                    worst = "error" if "error" in (lv, worst) else "warn" if "warn" in (lv, worst) else "ok"
                self._paint(parent, self.LEVEL_COLOR[worst], 1)
                self.check_tree.addTopLevelItem(parent)
                parent.setExpanded(True)

    def rows(self, selected=False):
        rows = sorted({i.row() for i in self.tree.selectedIndexes()}) if selected else range(self.tree.rowCount())
        return ["\t".join(self.tree.item(r, c).text() for c in range(4)) for r in rows]

    def copy(self, selected=False):
        QApplication.clipboard().setText("\n".join(self.rows(selected)))

    def acknowledge(self):
        rt.ack_issues()
        self.rebuild()
        if self.on_ack:
            self.on_ack()


class DailyRoutinePage(QWidget):
    def __init__(self):
        super().__init__()
        root = QVBoxLayout(self)
        root.setContentsMargins(theme.SPACE, theme.SPACE, theme.SPACE, theme.SPACE)
        root.setSpacing(theme.SPACE)
        top = QHBoxLayout()
        top.addWidget(QLabel("Preset"))
        self.preset = QComboBox()
        self.preset.setMinimumWidth(160)
        top.addWidget(self.preset)
        self.preset.currentIndexChanged.connect(lambda _i: self._paint_load())
        self.load_btn = QPushButton("Load...")
        self.load_btn.clicked.connect(self._load)
        self.new_btn = QPushButton("New...")
        self.new_btn.clicked.connect(self._new)
        self.save_btn = QPushButton("Save As...")
        self.save_btn.clicked.connect(self._save_as)
        self.delete_btn = QPushButton("Delete...")
        self.delete_btn.clicked.connect(self._delete)
        self.reset_btn = QPushButton("Reset Default...")
        self.reset_btn.clicked.connect(self._reset)
        for b in (self.load_btn, self.new_btn, self.save_btn, self.delete_btn, self.reset_btn):
            top.addWidget(b)
        top.addStretch(1)
        self.start_btn = QPushButton("Start Routine")
        theme.role(self.start_btn, "start")
        self.start_btn.clicked.connect(self._start)
        self.stop_btn = QPushButton("Stop Routine")
        theme.role(self.stop_btn, "danger")
        self.stop_btn.clicked.connect(self._stop)
        self._issues_dlg = None
        self.issues_btn = QPushButton("Alarm")
        self.issues_btn.clicked.connect(self._issues)
        top.addWidget(self.issues_btn)
        self.badge = QLabel(self)
        self.badge.setAlignment(Qt.AlignCenter)
        self.badge.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.badge.hide()
        self.issues_btn.installEventFilter(self)
        self.check_btn = QPushButton("Pre-run dummy test")
        self.check_btn.clicked.connect(self._check)
        top.addWidget(self.check_btn)
        top.addWidget(self.start_btn)
        top.addWidget(self.stop_btn)
        root.addLayout(top)

        self.steps_box = QGroupBox("Steps")
        v = QVBoxLayout(self.steps_box)
        self.tree = StepTree()
        self.tree.can_drag = self._editable
        self.tree.moved.connect(self._dragged)
        self.tree.setObjectName("routineSteps")
        self.tree.setHeaderLabels(list(COLS) + [""])   # last column takes the spare width
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.itemChanged.connect(self._toggled)
        self.tree.itemDoubleClicked.connect(lambda it, _c: self._options(self.tree.indexOfTopLevelItem(it)))
        hd = self.tree.header()
        hd.setStretchLastSection(False)
        hd.setSectionResizeMode(len(COLS), QHeaderView.Stretch)
        for c in range(len(COLS)):
            hd.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hd.setMinimumSectionSize(60)
        for c, w in ((6, 112), (7, 88)):   # button columns: the cell widgets are not measured
            hd.setSectionResizeMode(c, QHeaderView.Fixed)
            hd.resizeSection(c, w)
        v.addWidget(self.tree)
        row = QHBoxLayout()
        self.add_btn = QPushButton("Add Step...")
        self.add_btn.clicked.connect(self._add)
        self.dup_btn = QPushButton("Duplicate Step")
        self.dup_btn.clicked.connect(self._duplicate)
        self.remove_btn = QPushButton("Remove Step")
        self.remove_btn.clicked.connect(self._remove)
        self.up_btn = QPushButton("Move Up")
        self.up_btn.clicked.connect(lambda: self._move(-1))
        self.down_btn = QPushButton("Move Down")
        self.down_btn.clicked.connect(lambda: self._move(1))
        self.skip_btn = QPushButton("Skip Step")
        theme.role(self.skip_btn, "warn")
        self.skip_btn.clicked.connect(self._skip)
        for b in (self.add_btn, self.dup_btn, self.remove_btn, self.up_btn, self.down_btn):
            row.addWidget(b)
        row.addSpacing(theme.SPACE)
        row.addWidget(self.skip_btn)
        row.addStretch(1)
        self.slots_btn = QPushButton("Slots...")
        self.slots_btn.clicked.connect(self._slots)
        row.addWidget(self.slots_btn)
        row.addSpacing(theme.SPACE)
        self.home_cb = QCheckBox("Move stages to origin after scans and at end")
        self.home_cb.toggled.connect(self._home_toggled)
        row.addWidget(self.home_cb)
        row.addSpacing(theme.SPACE)
        self.on_error = QLabel("On Error: Stop")
        row.addWidget(self.on_error)
        row.addSpacing(theme.SPACE * 2)
        self.est_end = QLabel("Est. End: -")
        row.addWidget(self.est_end)
        v.addLayout(row)
        root.addWidget(self.steps_box)

        tl = QGroupBox("Timeline")
        tv = QVBoxLayout(tl)
        self.timeline = Timeline()
        tv.addWidget(self.timeline)
        root.addWidget(tl)

        self.cur_box = QGroupBox("Current Step: -")
        cv = QVBoxLayout(self.cur_box)
        self.prog_line = QLabel("")
        self.prog_line.setStyleSheet("font-size: 14pt; font-weight: 700;")
        head = QHBoxLayout()
        head.addWidget(self.prog_line, 1)
        self.acq_btn = QPushButton("Go to Acquisition")
        self.acq_btn.clicked.connect(lambda: self.window().show_page("Acquisition"))
        head.addWidget(self.acq_btn)
        cv.addLayout(head)
        self.prog_bar = QProgressBar()
        self.prog_bar.setRange(0, 1000)
        self.prog_bar.setTextVisible(True)
        self.prog_bar.setFixedHeight(30)
        cv.addWidget(self.prog_bar)
        self.hv_line = QLabel("")
        self.hv_line.setStyleSheet("font-size: 13pt; font-weight: 600;")
        cv.addWidget(self.hv_line)
        self.run_line = QLabel("")
        cv.addWidget(self.run_line)
        from pages.daq.output import ProgressCard
        self.run_card = ProgressCard()        # the same card as DAQ System > Acquisition
        self.run_card.hide()
        cv.addWidget(self.run_card)
        self.log_labels = []
        for _ in range(LOG_LINES):
            lab = QLabel()
            lab.setStyleSheet(f'font-family: "{theme.MONO_FONT}"; font-size: {theme.PT["mono"] + 2}pt;')
            cv.addWidget(lab)
            self.log_labels.append(lab)
        for lb in [self.prog_line, self.run_line] + self.log_labels:   # long text is cut at the window edge
            lb.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        root.addWidget(self.cur_box)
        root.addStretch(1)

        self.snack = Snackbar(self)
        self._sig = None
        self._presets = None
        self._filling = False
        self.edit_widgets = [self.preset, self.load_btn, self.new_btn, self.save_btn, self.delete_btn, self.reset_btn,
                             self.add_btn, self.dup_btn,
                             self.remove_btn, self.up_btn, self.down_btn, self.home_cb, self.slots_btn]
        self.opt_buttons = []
        self.bus = _Bus()
        self.bus.changed.connect(self.refresh, Qt.QueuedConnection)
        rt.listeners.append(self.bus.changed.emit)
        ACCESS.listeners.append(self._paint_access)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(REFRESH_MS)
        self.refresh()

    # ---------------------------------------------------------------- view
    def refresh(self):
        now = _now()
        rt.start_from_request()
        v = rt.view()
        self._progress = rt.progress(now)
        m = timeline_model(v, now, self._progress.get("step_left_s") if self._progress.get("active") else None,
                           self._progress.get("units"))
        self._m_end = m["end"]
        keep = ("enabled", "kind", "name", "start", "options", "status", "note", "started", "finished", "due")
        marks = day_marks(v, now)
        try:
            self._rec = rt.recommended_starts(now=now)
        except Exception:
            self._rec = {}
        sig = (json.dumps([{k: s[k] for k in keep} for s in v["steps"]], sort_keys=True), v["name"], v["state"],
               (self._progress.get("index"), self._progress.get("text")),
               v["started_at"], tuple(int(x or 0) // 60 for x in m["est"].values()), tuple(sorted(marks.items())),
               tuple(sorted((i, a.isoformat(), b.isoformat()) for i, (a, b) in self._rec.items())))
        if sig != self._sig:
            self._sig = sig
            self._fill(v, m, marks)
        self._fill_presets(v["name"])
        self.home_cb.blockSignals(True)
        self.home_cb.setChecked(bool(v.get("home_stage", True)))
        self.home_cb.blockSignals(False)
        self.timeline.set_model(m, now)
        text = f"Est. End: {m['end']:%H:%M}" if m["end"] else "Est. End: -"
        if m["late"]:
            text += f' <span style="color: {theme.C["bad"]};">({_hm(m["late"])})</span>'
        self.est_end.setText(text)
        cur = v["current"]
        name = v["steps"][cur]["name"] if cur is not None and cur < len(v["steps"]) else "-"
        self.cur_box.setTitle(f"Current Step: {name}")
        self._paint_progress(now)
        lines = routine_log_lines()
        for lab, line in zip(self.log_labels, lines + [""] * (LOG_LINES - len(lines))):
            lab.setText(line)
            lab.setToolTip(line)
        self._paint_issues()
        self._paint_access()

    def _paint_progress(self, now):
        p = rt.progress(now)
        self._progress = p
        on = p.get("active")
        for w in (self.prog_line, self.prog_bar, self.hv_line):
            w.setVisible(bool(on))
        if on:
            from core.hv_label import now_volts
            v = now_volts()
            self.hv_line.setText("HV now   " + ("   ".join(f"{n} {x:.0f} V" for n, x in zip(("Mon", "Rot1", "Rot2"), v))
                                                if v else "HV Monitor reading unavailable"))
        if not on:
            self.run_line.hide()
            self.run_card.hide()
            return
        left = _dur(p["step_left_s"]) if p.get("step_left_s") else "-"
        nxt = p.get("next_daq_s")
        self.prog_line.setText(f"Step {p['pos']}/{p['n']}   {p['step']}" + (f"   {p['text']}" if p["text"] else "")
                               + (f"   next DAQ in {max(nxt, 0):.0f} s" if nxt is not None else "")
                               + f"   step left {left}   routine ends {(getattr(self, '_m_end', None) or p['end']):%H:%M}")
        self.prog_line.setToolTip(self.prog_line.text())
        frac = p.get("frac")
        self.prog_bar.setValue(int(1000 * frac) if frac is not None else 0)
        self.prog_bar.setFormat(f"{100 * frac:.0f} %" if frac is not None else "")
        a = p.get("acq")
        self.run_line.setVisible(bool(a))
        job = None
        if a:
            from core.jobs import JOBS, LABELS
            job = JOBS.jobs.get(a["slot"])
            self.run_line.setText((f"Run {a['run']}   " if a["run"] else "") + LABELS.get(a["slot"], a["slot"]))
        if job is not None and job.progress is not None:
            self.run_card.show_progress(job.progress, True)
            self.run_card.show()
        else:
            self.run_card.hide()

    def _paint_issues(self):
        n = rt.issue_counts(rt.issues())
        self.issues_btn.setToolTip(issues_text(n))
        role = issues_role(n)
        theme.role(self.issues_btn, role)
        count = sum(n.values())
        self.badge.setText(str(count))
        self.badge.setStyleSheet(f"background: {theme.ROLES[role][0]}; color: {theme.ROLES[role][3]};"
                                 " border: 2px solid white; border-radius: 10px; font-size: 8pt; font-weight: 700;"
                                 " padding: 0 5px;")
        self.badge.setVisible(count > 0)
        self._place_badge()
        if self._issues_dlg is not None and self._issues_dlg.isVisible():
            self._issues_dlg.rebuild()

    def eventFilter(self, obj, ev):
        if obj is self.issues_btn and ev.type() in (ev.Move, ev.Resize, ev.Show):
            self._place_badge()
        return super().eventFilter(obj, ev)

    def _place_badge(self):
        """Count badge over the Alarm button's top-right corner."""
        self.badge.adjustSize()
        w = max(20, self.badge.width())
        self.badge.resize(w, 20)
        corner = self.issues_btn.mapTo(self, self.issues_btn.rect().topRight())
        self.badge.move(corner.x() - w + 8, corner.y() - 8)
        self.badge.raise_()

    def _issues(self):
        if self._issues_dlg is None:
            self._issues_dlg = IssuesDialog(self)
            self._issues_dlg.on_ack = self.refresh
        self._issues_dlg.rebuild()
        self._issues_dlg.show()
        self._issues_dlg.raise_()
        self._issues_dlg.activateWindow()

    def _fill(self, v, m, marks=None):
        C = theme.C
        marks = marks or {}
        self._filling = True
        sel = self._index()
        self.tree.clear()
        self.opt_buttons = []
        for s in v["steps"]:
            est = m["est"].get(s["index"], s["estimate_s"])
            start = "Sequential" if s["start"] == rt.SEQUENTIAL else s["start"]
            if s["index"] in marks:
                start += " " + marks[s["index"]]
            status = s["status"]
            prog = getattr(self, "_progress", None) or {}
            if status == "Running" and prog.get("index") == s["index"] and prog.get("text"):
                status = f"Running {prog['text'].split(' ', 1)[-1]}"
            it = QTreeWidgetItem(["", s["name"], start,
                                  _summary(s), _dur(est), status, "", ""])
            it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable | Qt.ItemIsDragEnabled)
            it.setCheckState(0, Qt.Checked if s["enabled"] else Qt.Unchecked)
            it.setTextAlignment(4, Qt.AlignRight | Qt.AlignVCenter)
            if s["note"]:
                it.setToolTip(5, s["note"])
            rec = getattr(self, "_rec", {}).get(s["index"])
            if rec and s["enabled"]:
                it.setToolTip(2, f"Previous step finishes at {rec[1]:%H:%M}. Recommended start is {rec[0]:%H:%M}.")
                it.setForeground(2, QColor(C["warn"]))
            key = STATUS_COLOR.get(s["status"])
            if key:
                it.setForeground(5, QColor(C[key]))
            if not s["enabled"]:
                for c in range(1, 5):
                    it.setForeground(c, QColor(C["disabled"]))
            if s["status"] == "Running":
                for c in (1, 5):
                    f = it.font(c)
                    f.setBold(True)
                    it.setFont(c, f)
            self.tree.addTopLevelItem(it)
            opt = QPushButton("Options...")
            opt.clicked.connect(lambda _c, i=s["index"]: self._options(i))
            opn = QPushButton("Open")
            opn.clicked.connect(lambda _c, i=s["index"]: self._open(i))
            for b in (opt, opn):
                b.setStyleSheet("padding: 2px 10px; min-height: 18px;")
            self.tree.setItemWidget(it, 6, opt)
            self.tree.setItemWidget(it, 7, opn)
            self.opt_buttons.append(opt)
        n = self.tree.topLevelItemCount()
        if 0 <= sel < n:
            self.tree.setCurrentItem(self.tree.topLevelItem(sel))
        self._filling = False
        rh = self.tree.sizeHintForRow(0) if n else 30
        self.tree.setFixedHeight(self.tree.header().sizeHint().height() + max(rh, 24) * max(n, 2) + 8)
        started = rt._parse(v["started_at"])
        self.steps_box.setTitle(f"Steps [Running - {started:%H:%M}]" if v["active"] and started else "Steps")

    def _fill_presets(self, name):
        names = rt.list_presets()
        if (names, name) == self._presets:
            return
        self._presets = (names, name)
        self.preset.blockSignals(True)
        self.preset.clear()
        for n in names:
            self.preset.addItem("Default" if n == "default" else n, n)
        if name not in names:
            self.preset.addItem(name, name)
        self.preset.setCurrentIndex(self.preset.findData(name))
        self.preset.blockSignals(False)

    def _paint_access(self, on=None):
        on, active = ACCESS.unlocked, rt.is_active()
        edit = on and not active
        tip = "" if on else "Unlock to use"
        for w in self.edit_widgets + [self.start_btn]:
            w.setEnabled(edit)
            w.setToolTip(tip)
        for b in self.opt_buttons:      # always viewable; _options decides edit or read-only
            b.setEnabled(True)
            b.setToolTip("")
        for b in (self.stop_btn, self.skip_btn):
            b.setEnabled(on and active)
            b.setToolTip(tip)
        self.check_btn.setEnabled(edit)
        self.check_btn.setToolTip("No hardware actions" if on else tip)
        self._filling = True
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable
            it.setFlags(flags | Qt.ItemIsUserCheckable if edit else flags)
        self._filling = False

    @staticmethod
    def _editable():
        return ACCESS.unlocked and not rt.is_active()

    def _index(self):
        it = self.tree.currentItem()
        return self.tree.indexOfTopLevelItem(it) if it is not None else -1

    # ---------------------------------------------------------------- edits (all through routine.set_current)
    def _commit(self, r, select=None):
        """Replace the current routine; the tree is rebuilt on the next event loop pass, never inside its own signal."""
        try:
            rt.set_current(r)
        except (RuntimeError, ValueError) as e:
            QMessageBox.warning(self, "Daily Routine", str(e))
        QTimer.singleShot(0, lambda: self._after_commit(select))

    def _after_commit(self, select):
        self.refresh()
        if select is not None and 0 <= select < self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(select))

    def _toggled(self, item, col):
        if self._filling or col != 0:
            return
        i = self.tree.indexOfTopLevelItem(item)
        r = copy.deepcopy(rt.current())
        if 0 <= i < len(r["steps"]):
            r["steps"][i]["enabled"] = item.checkState(0) == Qt.Checked
            self._commit(r, i)

    def _home_toggled(self, on):
        if not self._editable():
            return
        r = copy.deepcopy(rt.current())
        r["home_stage"] = bool(on)
        self._commit(r)

    def _slots(self):
        if not self._editable():
            return
        dlg = SlotsDialog(self, rt.current()["slots"])
        if dlg.exec_() != QDialog.Accepted or not self._editable():
            return
        try:
            rt.set_slots(dlg.slots())
        except (RuntimeError, ValueError, OSError) as e:
            QMessageBox.warning(self, "Slots", str(e))
        QTimer.singleShot(0, lambda: self._after_commit(None))

    def _add(self):
        if not self._editable():
            return
        dlg = AddStepDialog(self)
        if dlg.exec_() != QDialog.Accepted:
            return
        r = copy.deepcopy(rt.current())
        i = self._index()
        at = i + 1 if 0 <= i < len(r["steps"]) else len(r["steps"])   # after the selected step
        r["steps"][at:at] = dlg.steps()
        self._commit(r, at)

    def _options(self, i):
        running = rt.is_active()
        r = copy.deepcopy(rt.current())
        if not 0 <= i < len(r["steps"]):
            return
        dlg = OptionsDialog(self, r["steps"][i])
        dlg.form.set_estimator(lambda opts: self._estimate(i, opts))
        dlg.form.set_window(lambda opts: self._window(i, opts, dlg.start.value()))
        if not (ACCESS.unlocked and (not running or r["steps"][i]["status"] == "Pending")):
            dlg.read_only()
            dlg.exec_()
            return
        if running:
            dlg.start.setEnabled(False)       # due times are fixed when the routine starts
        if dlg.exec_() != QDialog.Accepted:
            return
        if running:
            try:
                rt.update_pending_step(i, dlg.step()["name"], dlg.step()["options"])
            except (RuntimeError, ValueError) as e:
                QMessageBox.warning(self, "Daily Routine", str(e))
            QTimer.singleShot(0, lambda: self._after_commit(i))
            return
        r["steps"][i] = dlg.step()
        self._commit(r, i)

    @staticmethod
    def _estimate(i, opts):
        """Seconds of step i with these options, at its place in the routine (dark 'until next step' needs it)."""
        r = copy.deepcopy(rt.current())
        if not 0 <= i < len(r["steps"]):
            return 0.0
        s = r["steps"][i]
        s["options"] = opts
        now = rt._now()
        if not rt.is_active():
            rt.assign_due(r, now)
        return rt.estimate_seconds(s, r, rt._parse(s.get("due")) or now)

    @staticmethod
    def _window(i, opts, start):
        """(start, end) of step i with these options and start setting, as the timeline preview places it."""
        r = copy.deepcopy(rt.current())
        if not 0 <= i < len(r["steps"]) or not r["steps"][i].get("enabled", True):
            return None
        r["steps"][i].update(options=opts, start=start)
        now = rt._now()
        for k, st in enumerate(r["steps"]):
            st["index"] = k
            if not rt.is_active():
                st.update(started=None, finished=None, due=None, status="Pending" if st["enabled"] else "Disabled")
            elif st["enabled"]:   # a running routine keeps its due times; the bar length is the estimate
                st["estimate_s"] = rt.estimate_seconds(st, r, rt._parse(st.get("due")) or now)
        m = timeline_model(r, now)
        pos = sum(1 for st in r["steps"][:i] if st["enabled"])
        _n, _s, a, b, _o = m["bars"][pos]
        return (a, b) if a and b else None

    def _duplicate(self):
        if not self._editable():
            return
        i = self._index()
        r = copy.deepcopy(rt.current())
        if not 0 <= i < len(r["steps"]):
            return
        src = r["steps"][i]
        names = {s["name"] for s in r["steps"]}
        n = 2
        while f"{src['name']} {n}" in names:
            n += 1
        dup = {k: copy.deepcopy(src[k]) for k in ("enabled", "kind", "start", "options")}
        dup["name"] = f"{src['name']} {n}"
        r["steps"].insert(i + 1, rt.normalize_step(dup))
        self._commit(r, i + 1)

    def _remove(self):
        if not self._editable():
            return
        i = self._index()
        r = copy.deepcopy(rt.current())
        if not 0 <= i < len(r["steps"]):
            return
        removed = r["steps"].pop(i)
        self._commit(r, min(i, len(r["steps"]) - 1))
        self.snack.show_message(f"Step removed: {removed['name']}", "Undo", lambda: self._restore(i, removed))

    def _restore(self, i, step):
        if not self._editable():
            return
        r = copy.deepcopy(rt.current())
        r["steps"].insert(min(i, len(r["steps"])), step)
        self._commit(r, min(i, len(r["steps"]) - 1))

    def _move(self, d):
        if not self._editable():
            return
        i = self._index()
        r = copy.deepcopy(rt.current())
        j = i + d
        if not (0 <= i < len(r["steps"]) and 0 <= j < len(r["steps"])):
            return
        r["steps"][i], r["steps"][j] = r["steps"][j], r["steps"][i]
        self._commit(r, j)

    def _dragged(self, src, to):
        if not self._editable():
            return
        r = copy.deepcopy(rt.current())
        if not (0 <= src < len(r["steps"]) and 0 <= to < len(r["steps"])):
            return
        r["steps"].insert(to, r["steps"].pop(src))
        self._commit(r, to)

    def _open(self, i):
        steps = rt.current()["steps"]
        win = self.window()
        if 0 <= i < len(steps) and hasattr(win, "show_page"):
            win.show_page(OPEN_PAGE[steps[i]["kind"]])

    # ---------------------------------------------------------------- run control
    def _check(self):
        if self._editable():
            PreRunDialog(self, check_only=True).exec_()

    def _start(self):
        if not self._editable():
            return
        if rt.is_active():
            QMessageBox.warning(self, "Start Routine", "A routine is already running.")
            return
        from pages.trigger_check import confirm_trigger_rate
        if not confirm_trigger_rate(self, rt._backend_name()[0] == "HK"):
            return
        if PreRunDialog(self).exec_() != QDialog.Accepted or not self._editable():
            return
        late = rt.late_start_text()
        if late and QMessageBox.warning(self, "Start Routine", late + ".\n\nStart anyway?",
                                        QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        ok, msg = rt.start_routine()
        if not ok:
            QMessageBox.warning(self, "Start Routine", msg)
        self.refresh()

    def _stop(self):
        if not ACCESS.unlocked:
            return
        if rt.view()["current"] is not None and QMessageBox.question(
                self, "Stop Routine", "Stop the routine? The running step is aborted.") != QMessageBox.Yes:
            return
        rt.stop_routine()
        self.refresh()

    def _skip(self):
        if not ACCESS.unlocked:
            return
        rt.skip_step()
        self.refresh()

    # ---------------------------------------------------------------- presets
    def _load(self):
        if not self._editable():
            return
        name = self.preset.currentData()
        if not name:
            return
        try:
            rt.load(name)
        except (OSError, ValueError, RuntimeError) as e:
            QMessageBox.warning(self, "Load", f"Preset '{name}': {e}")
        self.refresh()
        self._paint_load()

    def _paint_load(self):
        """Load turns orange while the picked preset is not the loaded routine."""
        picked = self.preset.currentData()
        pending = bool(picked) and picked != rt.current()["name"]
        theme.role(self.load_btn, "warn" if pending else None)

    def _new(self):
        if not self._editable():
            return
        name, ok = QInputDialog.getText(self, "New Preset", "Preset name:")
        name = name.strip()
        if not ok or not name:
            return
        if not NAME_RE.match(name):
            QMessageBox.warning(self, "New Preset", "Preset name: letters, digits, _ and - only.")
            return
        try:
            rt.new_preset(name)
        except (OSError, ValueError, RuntimeError) as e:
            QMessageBox.warning(self, "New Preset", str(e))
        self.refresh()
        self._paint_load()

    def _delete(self):
        if not self._editable():
            return
        name = self.preset.currentData()
        if not name:
            return
        if name == "default":
            QMessageBox.warning(self, "Delete Preset", "The default preset cannot be deleted.")
            return
        if QMessageBox.question(self, "Delete Preset", f"Delete preset '{name}'?") != QMessageBox.Yes:
            return
        try:
            rt.delete_preset(name)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "Delete Preset", str(e))
        self._presets = None
        self.refresh()
        self._paint_load()

    def _save_as(self):
        if not self._editable():
            return
        cur = rt.current()["name"]
        name, ok = QInputDialog.getText(self, "Save As", "Preset name:", text="" if cur == "default" else cur)
        name = name.strip()
        if not ok or not name:
            return
        if not NAME_RE.match(name):
            QMessageBox.warning(self, "Save As", "Preset name: letters, digits, _ and - only.")
            return
        if name in rt.list_presets() and QMessageBox.question(
                self, "Save As", f"Overwrite preset '{name}'?") != QMessageBox.Yes:
            return
        try:
            rt.save(name)
        except OSError as e:
            QMessageBox.warning(self, "Save As", str(e))
        self.refresh()

    def _reset(self):
        if not self._editable():
            return
        if QMessageBox.question(self, "Reset Default", "Reset the default preset and load it?") != QMessageBox.Yes:
            return
        try:
            rt.reset_default()
        except (OSError, ValueError, RuntimeError) as e:
            QMessageBox.warning(self, "Reset Default", str(e))
        self.refresh()

"""DAQ System > Output, like the Tk app's Output tab: one pane per job slot started from v2
(Produce 1-3, Analysis 1-3, ...), each with its state (Running / Done / Failed / Stopped),
a Stop button and the live console; the state also shows as a lamp on the pane's tab.
The last two tabs follow the Tk app's own DAQ and analysis log files."""
import os

from PyQt5 import sip
from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtGui import QTextCursor
from PyQt5.QtWidgets import (QCheckBox, QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QProgressBar, QPushButton, QTabBar, QTabWidget,
                             QVBoxLayout, QWidget)

from app import theme
from core import env
from core.jobs import JOBS, LABELS
from widgets import basic

LOG_DIR = os.path.join(env.ADC_DIR, "LOG", "DAQ")
STATE = {"running": ("Running", "#1a7f37"), "done": ("Done", "#0b63b6"), "failed": ("Failed", "#b91c1c"),
         "stopped": ("Stopped", "#b45309"), "idle": ("Idle", "#8a9099")}
INSTANCE = None


def live():
    """The page, or None if not built yet or deleted by a window rebuild."""
    return INSTANCE if INSTANCE is not None and not sip.isdeleted(INSTANCE) else None


class _Bus(QObject):
    event = pyqtSignal(str, str, object)


BUS = None


def bus():
    """Qt bridge for JOBS events (created on the GUI thread, before any job starts)."""
    global BUS
    if BUS is None:
        BUS = _Bus()
        JOBS.listeners.append(lambda ev, slot, p: BUS.event.emit(ev, slot, p))
    return BUS


class ProgressCard(QFrame):
    """Acquisition progress: percentage, a thick bar with the event count, rate / time left / sequence,
    and the latest pedestal per channel."""

    def __init__(self):
        super().__init__()
        self.setObjectName("progressCard")
        c = theme.C
        self.setStyleSheet(
            f"QFrame#progressCard {{ background: {c['card']}; border: 1px solid {c['border']}; border-radius: 8px; }}"
            f"QProgressBar {{ border: 1px solid {c['border']}; border-radius: 6px; background: {c['field_ro']};"
            " text-align: center; font-size: 11pt; font-weight: 700; }"
            f"QProgressBar::chunk {{ background: {c['accent']}; border-radius: 5px; }}"
            f"QLabel#tileCap {{ color: {c['muted']}; font-size: 8.5pt; font-weight: 600; }}"
            "QLabel#tileVal { font-size: 13pt; font-weight: 700; }")
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 10, 14, 10)
        v.setSpacing(8)
        top = QHBoxLayout()
        self.pct = QLabel("0%")
        self.pct.setStyleSheet(f"font-size: 22pt; font-weight: 800; color: {c['accent']};")
        self.pct.setMinimumWidth(90)
        top.addWidget(self.pct)
        self.bar = QProgressBar()
        self.bar.setFixedHeight(28)
        top.addWidget(self.bar, 1)
        v.addLayout(top)
        tiles = QHBoxLayout()
        tiles.setSpacing(18)
        self.vals = {}
        for key, cap in (("rate", "RATE"), ("left", "REMAINING"), ("seq", "SEQUENCE")):
            tiles.addLayout(self._tile(key, cap))
        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setStyleSheet(f"color: {c['border']};")
        tiles.addWidget(sep)
        for ch in range(4):
            tiles.addLayout(self._tile(f"ch{ch}", f"PEDESTAL CH{ch}", mono=True))
        tiles.addStretch(1)
        v.addLayout(tiles)

    def _tile(self, key, caption, mono=False):
        box = QVBoxLayout()
        box.setSpacing(0)
        cap = QLabel(caption)
        cap.setObjectName("tileCap")
        val = QLabel("--")
        val.setObjectName("tileVal")
        if mono:
            val.setStyleSheet(f"font-family: '{theme.MONO_FONT}';")
        box.addWidget(cap)
        box.addWidget(val)
        self.vals[key] = val
        return box

    def show_progress(self, p, running):   # not update(): that is QWidget's repaint
        total = p.total or 0
        self.bar.setRange(0, max(total, 1))
        self.bar.setValue(min(p.evt, total) if total else 0)
        mark = getattr(p, "mark", "")      # "~" when estimated from the RAW file size
        self.bar.setFormat(f"{mark}{p.evt:,} / {total:,} events" if total else f"{mark}{p.evt:,} events")
        self.pct.setText(f"{mark}{100.0 * p.evt / total:.0f}%" if total else "--")
        self.vals["rate"].setText(f"{mark}{p.rate / 1000:.2f} k evt/s" if running and p.rate else "--")
        left = p.remaining_s() if running else None
        self.vals["left"].setText(f"{mark}{int(left) // 60}:{int(left) % 60:02d}" if left is not None else
                                  ("done" if not running else "--"))
        self.vals["seq"].setText(f"{p.seq[0]} / {p.seq[1]}" if p.seq else "1 / 1")
        peds = {}
        for part in p.ped.split("|"):
            name, _, value = part.strip().partition(":")
            if name.startswith("Ch") and value:
                peds[name[2:]] = value.strip()
        for ch in range(4):
            self.vals[f"ch{ch}"].setText(peds.get(str(ch), "--"))


class LastRunCard(QFrame):
    """The newest row of the run registry, shown while nothing is acquiring."""
    FIELDS = (("run", "RUN"), ("date", "DATE"), ("kind", "KIND"), ("wl", "WAVELENGTH"), ("events", "EVENTS"),
              ("status", "STATUS"), ("time", "REGISTERED"))

    def __init__(self):
        super().__init__()
        self.setObjectName("lastRun")
        c = theme.C
        self.setStyleSheet(f"QFrame#lastRun {{ background: {c['card']}; border: 1px solid {c['border']};"
                           " border-radius: 8px; }"
                           f"QLabel#tileCap {{ color: {c['muted']}; font-size: 8.5pt; font-weight: 600; }}"
                           "QLabel#tileVal { font-size: 13pt; font-weight: 700; }")
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 10, 14, 10)
        t = QLabel("Last acquisition")
        t.setStyleSheet("font-size: 12pt; font-weight: 700;")
        v.addWidget(t)
        row = QHBoxLayout()
        row.setSpacing(28)
        self.vals = {}
        for key, cap in self.FIELDS:
            box = QVBoxLayout()
            box.setSpacing(0)
            a = QLabel(cap)
            a.setObjectName("tileCap")
            b = QLabel("--")
            b.setObjectName("tileVal")
            box.addWidget(a)
            box.addWidget(b)
            row.addLayout(box)
            self.vals[key] = b
        row.addStretch(1)
        v.addLayout(row)

    def refresh(self):
        import csv
        from core.live_scan import REGISTRY
        try:
            with open(REGISTRY, encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        except OSError:
            rows = []
        if not rows:
            return False
        r = rows[-1]
        ok = r.get("status", "").startswith("done")
        vals = {"run": r.get("run", ""), "date": r.get("date", ""),
                "kind": f"{r.get('kind', '')} / {r.get('mode', '')}", "wl": f"{r.get('wavelength', '')} nm",
                "events": f"{int(float(r['events'])):,}" if r.get("events") else "--",
                "status": r.get("status", ""), "time": r.get("registered_at", "").replace("T", " ")[:16]}
        for key, val in vals.items():
            self.vals[key].setText(val)
        self.vals["status"].setStyleSheet(f"color: {theme.STATE['done' if ok else 'error'][0]};")
        return True


class _Estimate:
    """Progress-like record from the RAW file size, with the rate from the previous sample."""
    mark, ped, seq = "~", "", None

    def __init__(self, evt, total, prev, t):
        self.evt, self.total, self.t = evt, total, t
        self.rate = (evt - prev.evt) / (t - prev.t) if prev and evt > prev.evt and t > prev.t else None

    def remaining_s(self):
        return (self.total - self.evt) / self.rate if self.rate else None


class JobPane(QWidget):
    def __init__(self, slot):
        super().__init__()
        self.slot = slot
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 8, 0, 0)
        head = QHBoxLayout()
        self.lamp = basic.make_lamp(12)
        head.addWidget(self.lamp)
        self.state = QLabel()
        self.state.setStyleSheet("font-size: 11pt; font-weight: 700;")
        head.addWidget(self.state)
        self.what = QLabel()
        self.what.setObjectName("key")
        head.addWidget(self.what)
        head.addStretch(1)
        self.follow = QCheckBox("Auto-scroll")
        self.follow.setChecked(True)
        self.follow.toggled.connect(lambda on: on and self._to_end())
        head.addWidget(self.follow)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(lambda: JOBS.stop(self.slot))
        head.addWidget(self.stop_btn)
        clear = QPushButton("Clear")
        clear.clicked.connect(lambda: self.console.clear())
        head.addWidget(clear)
        v.addLayout(head)
        self.prog_box = ProgressCard()
        self.prog_box.hide()
        v.addWidget(self.prog_box)
        self._running = False
        self.console = QPlainTextEdit()
        self.console.setObjectName("dark")
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(5000)
        self.console.setLineWrapMode(QPlainTextEdit.NoWrap)
        v.addWidget(self.console, 1)
        self._cr = False          # last chunk ended in \r: the next text overwrites that line
        self._started = None
        self._scrolling = False   # True while the pane itself moves the scroll bar
        self.console.verticalScrollBar().valueChanged.connect(self._user_scrolled)

    def _to_end(self):
        bar = self.console.verticalScrollBar()
        self._scrolling = True
        bar.setValue(bar.maximum())
        self._scrolling = False

    def _user_scrolled(self, value):
        """Scrolling up stops following the output; scrolling back to the bottom resumes it."""
        if self._scrolling:
            return
        at_end = value >= self.console.verticalScrollBar().maximum() - 2
        if at_end != self.follow.isChecked():
            self.follow.blockSignals(True)
            self.follow.setChecked(at_end)
            self.follow.blockSignals(False)

    def set_state(self, job):
        text, color = STATE.get(job.state, STATE["idle"])
        if job.state in ("failed",) and job.code not in (None, -1):
            text = f"Failed (exit {job.code})"
        self.state.setText(text)
        self.state.setStyleSheet(f"font-size: 11pt; font-weight: 700; color: {color};")
        basic.paint_lamp(self.lamp, color)
        self.what.setText(f"{job.name}   started {job.started:%H:%M:%S}")
        self.stop_btn.setEnabled(job.state == "running")
        self._running = job.state == "running"
        if self._running and job.started != self._started:   # a new job: follow it from the start
            self._started = job.started
            self.follow.setChecked(True)
            self._to_end()
        if job.progress is None:
            self.prog_box.hide()
        else:
            self.set_progress(job.progress)

    def set_progress(self, p):
        self.prog_box.show_progress(p, self._running)
        self.prog_box.show()

    def write(self, text):
        cur = self.console.textCursor()
        cur.movePosition(QTextCursor.End)
        parts = text.split("\r")
        for i, part in enumerate(parts):
            after_cr = i > 0 or self._cr
            if after_cr and part and not part.startswith("\n"):   # \r then text: overwrite the line
                cur.movePosition(QTextCursor.StartOfBlock, QTextCursor.KeepAnchor)
                cur.removeSelectedText()
            cur.insertText(part)
            if part:
                self._cr = False
        self._cr = text.endswith("\r") or (self._cr and not text)
        if self.follow.isChecked():
            self._to_end()


class OutputPage(QWidget):
    def __init__(self):
        super().__init__()
        global INSTANCE
        INSTANCE = self
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 12)
        self.tk_card = ProgressCard()      # an acquisition the other app runs: estimated from its RAW file
        self.tk_card.hide()
        lay.addWidget(self.tk_card)
        self.last_card = LastRunCard()
        lay.addWidget(self.last_card)
        self._est = None
        self.tabs = QTabWidget()
        self.panes = {}
        lay.addWidget(self.tabs, 1)
        bus().event.connect(self._on_event)
        from PyQt5.QtCore import QTimer
        self.est_timer = QTimer(self)
        self.est_timer.timeout.connect(self._estimate)
        self.est_timer.start(1000)
        for slot, job in sorted(JOBS.jobs.items()):   # jobs started before this page was built
            pane = self._pane(slot)
            pane.write("".join(job.output))
            self._paint(slot, job)

    def _estimate(self):
        """Progress card for an acquisition this app did not start (its event lines are not in any file)."""
        from core import scan_progress, status
        own = any(JOBS.running(s) for s in ("daq", "general_scan", "stability"))
        if not self.isVisible():
            return
        acquiring = status._daq_running()
        if not acquiring:
            self.last_card.setVisible(self.last_card.refresh())
        else:
            self.last_card.hide()
        if own or not acquiring:
            self.tk_card.hide()
            self._est = None
            return
        snap = scan_progress.snapshot()
        try:
            events_total = int(float(env.read_config().get("Events", "")))
        except (ValueError, OSError):
            events_total = None
        p = scan_progress.point_progress(snap.get("done", []), events_total)
        if not p or not p["total"]:
            self.tk_card.hide()
            return
        now = __import__("time").time()
        prev = self._est
        est = _Estimate(p["events"], p["total"], prev, now)
        self._est = est if prev is None or est.evt < prev.evt or now - prev.t > 5 else prev
        self.tk_card.show_progress(est, True)
        self.tk_card.show()

    def _pane(self, slot):
        if slot not in self.panes:
            pane = JobPane(slot)
            i = len(self.panes)
            self.tabs.insertTab(i, pane, LABELS.get(slot, slot))
            lamp = basic.make_lamp(10)
            self.tabs.tabBar().setTabButton(i, QTabBar.RightSide, lamp)
            pane.tab_lamp = lamp
            self.panes[slot] = pane
        return self.panes[slot]

    def _paint(self, slot, job):
        pane = self._pane(slot)
        pane.set_state(job)
        basic.paint_lamp(pane.tab_lamp, STATE.get(job.state, STATE["idle"])[1])

    def _on_event(self, event, slot, payload):
        if event == "state":
            self._paint(slot, payload)
        elif event == "output":
            self._pane(slot).write(payload)
        elif event == "progress":
            self._pane(slot).set_progress(payload)

    def focus(self, slot):
        if slot in self.panes:
            self.tabs.setCurrentWidget(self.panes[slot])

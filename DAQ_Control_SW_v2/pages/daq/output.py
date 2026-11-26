"""DAQ System > Output, like the Tk app's Output tab: one pane per job slot started from v2
(Produce 1-3, Analysis 1-3, ...), each with its state (Running / Done / Failed / Stopped),
a Stop button and the live console; the state also shows as a lamp on the pane's tab.
The last two tabs follow the Tk app's own DAQ and analysis log files."""
import os
import re
import time
from datetime import datetime

from PyQt5 import sip
from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtGui import QColor, QTextBlockFormat, QTextCharFormat, QTextCursor
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QApplication, QCheckBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QProgressBar, QPushButton, QSplitter, QTabBar, QTabWidget,
                             QVBoxLayout, QWidget)

from app import theme
from core import env
from core.jobs import JOBS, LABELS, NO_INPUT
from widgets import basic

LOG_DIR = os.path.join(env.ADC_DIR, "LOG", "DAQ")
ACQ_SLOTS = ("daq", "general_scan", "stability", "hk")   # one digitizer: one acquisition at a time
ACQ_KEY, ACQ_LABEL = "acquisition", "DAQ"
JOB_BANNER = re.compile(r"^\n===== .* started @ \d\d:\d\d:\d\d =====\n$")   # replaced by the pane header
JOB_END = re.compile(r"^===== .* (finished \(exit 0\)|FAILED \(exit -?\d+\)) =====\n\n$")   # replaced by a result bar
SILENT_S = 20
TS = re.compile(r"^\d\d:\d\d:\d\d  ", re.M)
ERR_LINE = re.compile(r"\[(ERROR|CRITICAL|FATAL)\]|Traceback \(most recent|\bFAILED\b|^\s*(ERROR|Error|error)\b|"
                      r"No route to host|Connection refused|Connection timed out|Permission denied|"
                      r"command not found|No such file or directory")
WARN_LINE = re.compile(r"\[WARN(ING)?\]|^\s*(WARNING|Warning)\b")
CON = {"ts": "#7c8496", "bar_bg": "#2b3040", "bar_fg": "#e8ebf2", "err_bg": "#4a1f24", "err_fg": "#ffb4b4",
       "warn": "#f0c674", "ok": "#8fd19e"}


def _dur(seconds):
    s = max(0, int(seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def end_bar(job):
    """(text, colour) of the bar that closes a run."""
    if job is None:
        return "Finished", CON["bar_fg"]
    if job.state == "stopped":
        res, col = "Stopped", CON["warn"]
    elif job.code == 0:
        res, col = "Done", CON["ok"]
    else:
        res, col = f"Failed (exit {job.code})", CON["err_fg"]
    ran = f"  ·  ran {_dur((job.ended - job.started).total_seconds())}" if job.ended else ""
    return f"{job.name}  ·  {res}{ran}", col


def pane_key(slot):
    return ACQ_KEY if slot in ACQ_SLOTS else slot


def run_context(slot):
    """Short 'what is this run' text: the routine step, the Stability engine status or the General Scan point."""
    parts = []
    try:
        from core import routine as rt
        v = rt.view()
        cur = v["current"]
        if v["active"] and cur is not None and cur < len(v["steps"]):
            parts.append(f"Routine: {v['steps'][cur]['name']}")
    except Exception:
        pass
    try:
        if slot == "general_scan":
            from core import scan_progress
            snap = scan_progress.snapshot()
            if snap.get("running"):
                parts.append(f"point {len(snap.get('done') or []) + 1}/{snap.get('total') or '?'}")
        else:
            from core import stability as stab
            if stab.RUN.running:
                parts.append(str(stab.RUN.status[0]).split(" - ")[0])
    except Exception:
        pass
    return " · ".join(p for p in parts if p)
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
        for key, cap in (("rate", "RATE"), ("readout", "READOUT"), ("recorded", "TRIGGERS RECORDED"),
                         ("left", "REMAINING"), ("seq", "SEQUENCE")):
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
        mbps = getattr(p, "mbps", None) if running else None
        self.vals["readout"].setText(f"{mark}{mbps:.1f} MB/s" if mbps else "--")
        frac = getattr(p, "recorded_fraction", None) if running else None
        self.vals["recorded"].setText(f"{mark}{100 * min(frac, 1.0):.1f} %" if frac else "--")
        self.vals["recorded"].setStyleSheet(f"color: {theme.C['bad']};" if frac and frac < 0.98 else "")
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
        self.active = slot        # the acquisition pane: the slot it follows now
        self.seen = set()         # (slot, started) of jobs that already got a header
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
        self.silent = QLabel()
        self.silent.setStyleSheet(f"color: {theme.C['warn']}; font-weight: 700;")
        head.addWidget(self.silent)
        head.addStretch(1)
        self.routine_btn = QPushButton("Go to Routine")
        self.routine_btn.clicked.connect(lambda: self.window().show_page("Daily Routine"))
        self.routine_btn.hide()
        head.addWidget(self.routine_btn)
        self.follow = QCheckBox("Auto-scroll")
        self.follow.setChecked(True)
        self.follow.toggled.connect(lambda on: on and self._to_end())
        head.addWidget(self.follow)
        self.show_cmd = QCheckBox("Show Command")
        self.show_cmd.setChecked(True)
        self.show_cmd.toggled.connect(lambda _on: self._show_cmd())
        head.addWidget(self.show_cmd)
        self.hide_wave = QCheckBox("Hide Waveform")
        self.hide_wave.setStyleSheet(f"QCheckBox {{ font-weight: 700; color: {theme.C['accent']}; }}")
        self.hide_wave.toggled.connect(lambda on: live() and live().set_wave_hidden(on))
        head.addWidget(self.hide_wave)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(lambda: JOBS.stop(self.active))
        head.addWidget(self.stop_btn)
        copy = QPushButton("Copy")
        copy.clicked.connect(self._copy)
        head.addWidget(copy)
        clear = QPushButton("Clear")
        clear.clicked.connect(self._clear)
        head.addWidget(clear)
        v.addLayout(head)
        self.cmd = QLabel()
        self.cmd.setWordWrap(True)
        self.cmd.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.cmd.setStyleSheet(f"font-family: '{theme.MONO_FONT}'; color: {theme.C['muted']};"
                               f" background: {theme.C['field_ro']}; border: 1px solid {theme.C['border']};"
                               " border-radius: 6px; padding: 4px 8px;")
        self.cmd.hide()
        v.addWidget(self.cmd)
        self.prog_box = ProgressCard()
        self.prog_box.hide()
        v.addWidget(self.prog_box)
        self._running = False
        self.console = QPlainTextEdit()
        self.console.setObjectName("dark")
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(5000)
        self.console.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.body = QSplitter(Qt.Horizontal)      # console | live waveform (moved here by the page)
        self.body.setChildrenCollapsible(False)
        self.body.addWidget(self.console)
        self.body.splitterMoved.connect(lambda *_: live() and live().remember_split(self.body))
        v.addWidget(self.body, 1)
        self.input_row = QWidget()
        row = QHBoxLayout(self.input_row)
        row.setContentsMargins(0, 4, 0, 0)
        row.addWidget(QLabel("Input"))
        self.entry = QLineEdit()
        self.entry.setEchoMode(QLineEdit.Password)
        self.entry.returnPressed.connect(self._send)
        row.addWidget(self.entry, 1)
        show = QCheckBox("Show")
        show.toggled.connect(lambda on: self.entry.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password))
        row.addWidget(show)
        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self._send)
        row.addWidget(self.send_btn)
        self.input_row.setVisible(slot not in NO_INPUT)
        self.entry.setEnabled(False)
        self.send_btn.setEnabled(False)
        v.addWidget(self.input_row)
        self._cr = False          # last chunk ended in \r: the next text overwrites that line
        self._started = None
        self._scrolling = False   # True while the pane itself moves the scroll bar
        self.console.verticalScrollBar().valueChanged.connect(self._user_scrolled)
        self._job, self._last_out, self._got_out = None, 0.0, False
        self.empty = QLabel("No output yet", self.console.viewport())
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setStyleSheet(f"color: {CON['ts']}; background: transparent;")
        self.empty.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.empty.hide()
        self.f_ts, self.f_text = self._fmt(CON["ts"]), QTextCharFormat()

    @staticmethod
    def _fmt(color, bold=False):
        f = QTextCharFormat()
        f.setForeground(QColor(color))
        if bold:
            f.setFontWeight(700)
        return f

    def _show_cmd(self):
        self.cmd.setVisible(self.show_cmd.isChecked() and bool(self.cmd.text()))

    def _copy(self):
        """Selection (or everything) without the line timestamps."""
        cur = self.console.textCursor()
        text = cur.selection().toPlainText() if cur.hasSelection() else self.console.toPlainText()
        QApplication.clipboard().setText(TS.sub("", text))

    def _clear(self):
        self.console.clear()
        self._cr = False

    def tick(self):
        """Elapsed time, silence warning and the empty-console text; called every second."""
        job = self._job
        if job is None:
            return
        running = job.state == "running"
        if running:
            extra = f"elapsed {_dur((datetime.now() - job.started).total_seconds())}"
        elif job.ended:
            extra = f"ran {_dur((job.ended - job.started).total_seconds())}"
        else:
            extra = ""
        self.what.setText(f"{job.name}   started {job.started:%H:%M:%S}   {extra}".rstrip())
        quiet = time.time() - self._last_out
        self.silent.setText(f"no output for {int(quiet)} s" if running and quiet >= SILENT_S else "")
        self.empty.setGeometry(self.console.viewport().rect())
        self.empty.setVisible(running and not self._got_out)

    def _send(self):
        text = self.entry.text()
        if text and JOBS.send(self.active, text):
            self.entry.clear()

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
        self.active = job.slot
        self.input_row.setVisible(job.slot not in NO_INPUT)
        text, color = STATE.get(job.state, STATE["idle"])
        if job.state in ("failed",) and job.code not in (None, -1):
            text = f"Failed (exit {job.code})"
        self.state.setText(text)
        self.state.setStyleSheet(f"font-size: 11pt; font-weight: 700; color: {color};")
        basic.paint_lamp(self.lamp, color)
        if job is not self._job:
            self._job, self._last_out, self._got_out = job, time.time(), False
        self.cmd.setText(job.command or "")
        self._show_cmd()
        self.tick()
        self.stop_btn.setEnabled(job.state == "running")
        self._running = job.state == "running"
        self.entry.setEnabled(self._running)
        self.send_btn.setEnabled(self._running)
        if self._running and job.started != self._started:   # a new job: follow it from the start
            self._started = job.started
            self.follow.setChecked(True)
            self._to_end()
        if job.progress is None:
            self.prog_box.hide()
        else:
            self.set_progress(job.progress)

    def set_progress(self, p):
        self._last_out, self._got_out = time.time(), True
        self.empty.hide()
        self.prog_box.show_progress(p, self._running)
        self.prog_box.show()

    def write(self, text, stamp=True):
        if text:
            self._last_out, self._got_out = time.time(), True
            self.empty.hide()
        cur = self.console.textCursor()
        cur.movePosition(QTextCursor.End)
        parts = text.split("\r")
        for i, part in enumerate(parts):
            after_cr = i > 0 or self._cr
            if after_cr and part and not part.startswith("\n"):   # \r then text: overwrite the line
                cur.movePosition(QTextCursor.StartOfBlock, QTextCursor.KeepAnchor)
                cur.removeSelectedText()
            self._insert(cur, part, stamp)
            if part:
                self._cr = False
        self._cr = text.endswith("\r") or (self._cr and not text)
        if self.follow.isChecked():
            self._to_end()

    def _insert(self, cur, text, stamp):
        for seg in re.split(r"(\n)", text):
            if seg == "\n":
                self._finish_line(cur)
                cur.insertText("\n", self.f_text)
                cur.setBlockFormat(QTextBlockFormat())
            elif seg:
                if stamp and cur.positionInBlock() == 0:
                    cur.insertText(datetime.now().strftime("%H:%M:%S  "), self.f_ts)
                cur.insertText(seg, self.f_text)

    def _finish_line(self, cur):
        """Colour a completed line: errors on a red band, warnings in amber."""
        block = cur.block()
        t = block.text()
        m = TS.match(t)
        body = t[m.end():] if m else t
        if ERR_LINE.search(body):
            fg, bg = CON["err_fg"], CON["err_bg"]
        elif WARN_LINE.search(body):
            fg, bg = CON["warn"], None
        else:
            return
        sel = QTextCursor(block)
        sel.setPosition(block.position() + len(t) - len(body))
        sel.setPosition(block.position() + len(t), QTextCursor.KeepAnchor)
        sel.mergeCharFormat(self._fmt(fg))
        if bg:
            bf = QTextBlockFormat()
            bf.setBackground(QColor(bg))
            sel.setBlockFormat(bf)

    def bar(self, text, color=None, gap=False):
        """One full-width band line: a run header (gap=True leaves a blank line above) or its result."""
        cur = self.console.textCursor()
        cur.movePosition(QTextCursor.End)
        if cur.block().text():
            self._finish_line(cur)
            cur.insertText("\n", self.f_text)
            cur.setBlockFormat(QTextBlockFormat())
        if gap and cur.position() > 0:
            cur.insertText("\n", self.f_text)
        bf = QTextBlockFormat()
        bf.setBackground(QColor(CON["bar_bg"]))
        cur.setBlockFormat(bf)
        cur.insertText("  " + text, self._fmt(color or CON["bar_fg"], bold=True))
        cur.insertText("\n", self.f_text)
        cur.setBlockFormat(QTextBlockFormat())
        self._cr = False
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
        from pages.daq.live_wave import LiveWaveform
        self.wave = LiveWaveform()
        self.wave_hidden = False
        self.split_ratio = 0.4                 # console share of the console | waveform row
        self.holder = QWidget()                # the waveform's place while no job pane exists
        QVBoxLayout(self.holder).setContentsMargins(0, 0, 0, 0)
        self.holder.layout().addWidget(self.wave)
        lay.addWidget(self.holder, 1)
        lay.addWidget(self.tabs, 1)
        self.tabs.hide()
        self.tabs.currentChanged.connect(lambda _i: self._place_wave())
        bus().event.connect(self._on_event)
        from PyQt5.QtCore import QTimer
        self.est_timer = QTimer(self)
        self.est_timer.timeout.connect(self._estimate)
        self.est_timer.timeout.connect(lambda: [p.tick() for p in self.panes.values()])
        self.est_timer.start(1000)
        for slot, job in sorted(JOBS.jobs.items(), key=lambda kv: kv[1].started):   # jobs started before this page
            pane = self._pane(slot)
            self._header(pane, job)
            for t in job.output:
                if JOB_END.match(t):
                    pane.bar(*end_bar(job))
                elif not JOB_BANNER.match(t):
                    pane.write(t, stamp=False)   # arrival times of earlier output are unknown
            self._paint(slot, job)

    def _estimate(self):
        """Progress card for an acquisition this app did not start (its event lines are not in any file)."""
        from core import routine as rt
        from core import scan_progress, status
        pane = self.panes.get(ACQ_KEY)
        if pane is not None:
            pane.routine_btn.setVisible(rt.is_active())
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

    def _place_wave(self):
        pane = self.tabs.currentWidget()
        if pane is None:
            return
        if self.wave.parent() is not pane.body:
            pane.body.addWidget(self.wave)
        w = max(pane.body.width(), 100)
        pane.body.setSizes([int(w * self.split_ratio), w - int(w * self.split_ratio)])
        self.wave.setVisible(not self.wave_hidden)

    def remember_split(self, body):
        sizes = body.sizes()
        if len(sizes) == 2 and sum(sizes) > 0 and sizes[1] > 0:
            self.split_ratio = sizes[0] / float(sum(sizes))

    def set_wave_hidden(self, on):
        self.wave_hidden = bool(on)
        self.wave.setVisible(not on)
        for p in self.panes.values():
            p.hide_wave.blockSignals(True)
            p.hide_wave.setChecked(self.wave_hidden)
            p.hide_wave.blockSignals(False)

    def _header(self, pane, job):
        """One header bar per run: source, context, start time."""
        key = (job.slot, job.started)
        if key in pane.seen:
            return
        pane.seen.add(key)
        ctx = run_context(job.slot) if job.state == "running" else ""
        parts = [LABELS.get(job.slot, job.slot), job.name] + ([ctx] if ctx else []) + [f"{job.started:%H:%M:%S}"]
        pane.bar("  ·  ".join(parts), gap=True)

    def _pane(self, slot):
        key = pane_key(slot)
        if key not in self.panes:
            pane = JobPane(key)
            pane.hide_wave.setChecked(self.wave_hidden)
            i = len(self.panes)
            self.tabs.insertTab(i, pane, ACQ_LABEL if key == ACQ_KEY else LABELS.get(slot, slot))
            if self.tabs.isHidden():
                self.holder.hide()
                self.tabs.show()
                self._place_wave()
            lamp = basic.make_lamp(10)
            self.tabs.tabBar().setTabButton(i, QTabBar.RightSide, lamp)
            pane.tab_lamp = lamp
            self.panes[key] = pane
        return self.panes[key]

    def _paint(self, slot, job):
        pane = self._pane(slot)
        if job.state == "running":
            self._header(pane, job)
        elif slot in ACQ_SLOTS and slot != pane.active and JOBS.running(pane.active):
            return                # a late state of an older run must not hide the running one
        pane.set_state(job)
        basic.paint_lamp(pane.tab_lamp, STATE.get(job.state, STATE["idle"])[1])

    def _on_event(self, event, slot, payload):
        if event == "state":
            self._paint(slot, payload)
        elif event == "output":
            if JOB_BANNER.match(payload):
                return
            if JOB_END.match(payload):
                self._pane(slot).bar(*end_bar(JOBS.jobs.get(slot)))
                return
            self._pane(slot).write(payload)
        elif event == "progress":
            pane = self._pane(slot)
            if slot not in ACQ_SLOTS or slot == pane.active:
                pane.set_progress(payload)

    def focus(self, slot):
        key = pane_key(slot)
        if key in self.panes:
            self.tabs.setCurrentWidget(self.panes[key])

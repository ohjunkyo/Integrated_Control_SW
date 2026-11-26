"""Shared log table (soft dark): Time | Level | Message for text logs, or plain
columns for CSV/DB logs. Used by Live Scan Logs, DAQ Log, Live Console and the
Logs tab."""
import glob
import os
import re

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import QAbstractItemView, QHeaderView, QTreeWidget, QTreeWidgetItem

BG, BG_ALT, HEADER, BORDER = "#232838", "#272d3f", "#2b3246", "#2f3547"
TEXT, DIM = "#d6dae3", "#6f7790"
LEVEL_COLOR = {"INFO": "#7cb7ff", "SUCCESS": "#6fd49a", "WARNING": "#f2c35b", "ERROR": "#ff7b7b",
               "CRITICAL": "#ff7b7b", "DEBUG": DIM}
STYLE = f"""
QTreeWidget {{ background: {BG}; alternate-background-color: {BG_ALT}; color: {TEXT};
    border: 1px solid {BORDER}; border-radius: 8px; padding: 4px; }}
QTreeWidget::item {{ padding: 2px 4px; border: none; }}
QTreeWidget::item:selected {{ background: #3a4462; color: #ffffff; }}
QHeaderView::section {{ background: {HEADER}; color: #aab1c4; border: none;
    border-bottom: 1px solid {BORDER}; padding: 5px 6px; font-weight: 600; }}
QScrollBar:vertical {{ background: {BG}; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #3a4256; border-radius: 4px; min-height: 24px; }}
QScrollBar:horizontal {{ background: {BG}; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #3a4256; border-radius: 4px; min-width: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
"""

_TS = re.compile(r"^\[(\d{4}-\d{2}-\d{2}[ T](\d{2}:\d{2}:\d{2}))[^\]]*\]\s*")
_TS_BARE = re.compile(r"^(\d{4}-\d{2}-\d{2}[ T](\d{2}:\d{2}(?::\d{2})?))[,.\d]*\s*(\|\s*)?")
_TAG = re.compile(r"^\[(INFO|WARNING|WARN|ERROR|CRITICAL|SUCCESS|DEBUG)\]\s*", re.I)
_EMOJI = re.compile("[\U0001f000-\U0001faff\u2600-\u27bf\ufe0f\u23e9-\u23ff]+ ?")   # v2 shows no emoji
_PIPE_LVL = re.compile(r"^(INFO|WARNING|ERROR|CRITICAL|DEBUG)\s*\|\s*", re.I)


def keyword_level(msg):
    """Same keyword rules as ui_automation.add_auto_log."""
    up = msg.upper()
    if any(k in up for k in ("ERROR", "FAIL", "CRITICAL", "\U0001f6a8", "❌")):
        return "ERROR"
    if any(k in up for k in ("WARNING", "ALERT", "⚠")):
        return "WARNING"
    if any(k in up for k in ("SUCCESS", "DONE", "COMPLETED", "✅")):
        return "SUCCESS"
    if any(k in up for k in ("INFO", "MOVE", "SCANNING", "SYNC", "▶")):
        return "INFO"
    return ""


def parse_line(line):
    """-> (time, full_timestamp, level, message) for the log formats used here."""
    line = line.rstrip("\n")
    full = t = ""
    m = _TS.match(line) or _TS_BARE.match(line)
    if m:
        full, t, line = m.group(1), m.group(2), line[m.end():]
    m = _TAG.match(line) or _PIPE_LVL.match(line)
    if m:
        lvl = m.group(1).upper()
        lvl = "WARNING" if lvl == "WARN" else lvl
        line = line[m.end():]
    else:
        lvl = keyword_level(line)
    return t, full, lvl, _EMOJI.sub("", line).strip()


def terminal_lines(text):
    """Lines as a terminal shows them: a \r progress update overwrites its line instead of adding one."""
    out = []
    for line in text.split("\n"):
        segs = [s for s in line.split("\r") if s]
        out.append(segs[-1] if segs else "")
    return out


class LogTable(QTreeWidget):
    def __init__(self, columns=("Time", "Level", "Message"), widths=(84, 96), max_rows=5000):
        super().__init__()
        self.max_rows = max_rows
        self.setStyleSheet(STYLE)
        self.setRootIsDecorated(False)
        self.setAlternatingRowColors(True)
        self.setUniformRowHeights(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setFont(QFont("DejaVu Sans Mono", 9))
        self.set_columns(columns, widths)

    def set_columns(self, columns, widths=()):
        self.clear()
        self.setHeaderLabels(list(columns))
        for i, w in enumerate(widths):
            self.setColumnWidth(i, w)
        self.text_mode = list(columns) == ["Time", "Level", "Message"]
        self._pending, self._live = "", None

    def _at_end(self):
        sb = self.verticalScrollBar()
        return sb.value() >= sb.maximum() - 2

    def _trim_and_scroll(self, follow):
        extra = self.topLevelItemCount() - self.max_rows
        if extra > 0:
            for _ in range(extra):
                self.takeTopLevelItem(0)
        if follow:
            self.scrollToBottom()

    def add_lines(self, lines):
        """Append text-log lines as Time | Level | Message rows."""
        follow = self._at_end()
        bold = QFont(self.font())
        bold.setBold(True)
        items = []
        for raw in lines[-self.max_rows:]:
            if not raw.strip():
                continue
            t, full, lvl, msg = parse_line(raw)
            it = QTreeWidgetItem([t, lvl, msg])
            it.setForeground(0, QColor(DIM))
            if full:
                it.setToolTip(0, full)
            it.setToolTip(2, msg)
            if lvl:
                it.setForeground(1, QColor(LEVEL_COLOR.get(lvl, TEXT)))
                it.setFont(1, bold)
            if lvl in ("WARNING", "ERROR", "CRITICAL"):
                it.setForeground(2, QColor(LEVEL_COLOR[lvl]))
            items.append(it)
        self.addTopLevelItems(items)
        self._trim_and_scroll(follow)

    def feed(self, chunk):
        """Append raw log text; the unfinished last line stays one row, updated in place."""
        text = self._pending + chunk
        cut = text.rfind("\n") + 1
        done, self._pending = text[:cut], text[cut:]
        if self._live is not None:
            i = self.indexOfTopLevelItem(self._live)
            if i >= 0:
                self.takeTopLevelItem(i)
            self._live = None
        if done:
            self.add_lines(terminal_lines(done))
        live = terminal_lines(self._pending)[-1] if self._pending else ""
        if live.strip():
            self.add_lines([live])
            self._live = self.topLevelItem(self.topLevelItemCount() - 1)

    def clear(self):
        super().clear()
        self._pending, self._live = "", None

    def set_text(self, text):
        self.clear()
        self.add_lines(terminal_lines(text))
        self.scrollToBottom()

    def set_rows(self, rows, dim_first=True):
        """Replace the content with plain rows (CSV / DB data)."""
        self.clear()
        items = []
        for r in rows[-self.max_rows:]:
            it = QTreeWidgetItem([str(v) for v in r])
            if dim_first:
                it.setForeground(0, QColor(DIM))
            for c in range(1, len(r)):
                it.setTextAlignment(c, Qt.AlignRight | Qt.AlignVCenter)
            items.append(it)
        self.addTopLevelItems(items)
        self.header().resizeSections(QHeaderView.ResizeToContents)
        self.scrollToBottom()

    def message(self, text):
        """Single dim line, e.g. 'no log yet'."""
        self.clear()
        it = QTreeWidgetItem(["", "", text] if self.text_mode else [text])
        it.setForeground(2 if self.text_mode else 0, QColor(DIM))
        self.addTopLevelItem(it)


class FileTail(LogTable):
    """LogTable that follows the newest file matching a glob, reading only new bytes."""

    def __init__(self, pattern, empty="No log yet.", max_rows=2000, interval_ms=2000, first_bytes=200_000):
        super().__init__(max_rows=max_rows)
        self.pattern, self.empty, self.first_bytes = pattern, empty, first_bytes
        self._path, self._pos = None, 0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(interval_ms)
        self.refresh()

    def refresh(self):
        files = glob.glob(self.pattern)
        if not files:
            if self._path is None:
                self.message(self.empty)
                self._path = ""
            return
        path = max(files, key=os.path.getmtime)
        if path != self._path:   # a newer file appeared: start over on it
            self._path = path
            self._pos = max(0, os.path.getsize(path) - self.first_bytes)   # tail only on first load
            self.clear()
        try:
            with open(path, encoding="utf-8", errors="replace", newline="") as f:   # keep \r
                f.seek(self._pos)
                chunk = f.read()
                self._pos = f.tell()
        except OSError:
            return
        if chunk:
            self.feed(chunk)

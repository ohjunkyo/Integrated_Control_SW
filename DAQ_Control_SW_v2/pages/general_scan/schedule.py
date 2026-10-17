"""General Scan > Schedule Manager, mirroring ui_automation._build_schedule_tab: date / time
row with Add Schedule / Cancel Selected, the Queued Schedules list (max 3) and the saved
configuration of the selected schedule. The queue and its runner live in core/schedule.py;
while the Tk app runs it owns the queue and this page only shows queued_schedules.json."""
from datetime import datetime

from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
                             QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from core import schedule
from core.access import ACCESS


class _Bus(QObject):
    changed = pyqtSignal()
    call = pyqtSignal(object)


_GUI = None


def qt_dispatcher():
    """dispatch(fn) for schedule.set_starter: runs fn on the GUI thread. Call it on the GUI thread."""
    global _GUI
    if _GUI is None:
        _GUI = _Bus()
        _GUI.call.connect(lambda fn: fn())
    return _GUI.call.emit


class SchedulePage(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        top = QHBoxLayout()
        top.addWidget(QLabel("Date:"))
        self.date = QLineEdit(datetime.now().strftime("%Y-%m-%d"))
        top.addWidget(self.date)
        hint = QLabel("(YYYY-MM-DD)")
        hint.setObjectName("hint")
        top.addWidget(hint)
        top.addSpacing(20)
        top.addWidget(QLabel("Time (JST):"))
        self.hour = QLineEdit(datetime.now().strftime("%H"))
        self.minute = QLineEdit("00")
        for e in (self.hour, self.minute):
            e.setFixedWidth(40)
            e.setAlignment(Qt.AlignCenter)
        top.addWidget(self.hour)
        top.addWidget(QLabel(":"))
        top.addWidget(self.minute)
        top.addSpacing(15)
        self.add_btn = QPushButton("Add Schedule")
        self.add_btn.clicked.connect(self._add)
        top.addWidget(self.add_btn)
        self.cancel_btn = QPushButton("Cancel Selected")
        self.cancel_btn.clicked.connect(self._cancel)
        top.addWidget(self.cancel_btn)
        top.addStretch(1)
        lay.addLayout(top)

        split = QSplitter(Qt.Horizontal)
        lbox = QGroupBox(f"Queued Schedules (Max {schedule.MAX_SCHEDULES})")
        ll = QVBoxLayout(lbox)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Target Time (JST)", "Status"])
        self.tree.setRootIsDecorated(False)
        self.tree.setColumnWidth(0, 160)
        self.tree.itemSelectionChanged.connect(self._preview)
        ll.addWidget(self.tree)
        split.addWidget(lbox)
        rbox = QGroupBox("Live Configuration Preview")
        rl = QVBoxLayout(rbox)
        self.detail = QPlainTextEdit()
        self.detail.setObjectName("dark")
        self.detail.setReadOnly(True)
        rl.addWidget(self.detail)
        split.addWidget(rbox)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 2)
        lay.addWidget(split, 1)
        self.items = None
        self.bus = _Bus()
        self.bus.changed.connect(self.refresh)
        schedule.listeners.append(self.bus.changed.emit)
        self.guarded = [self.date, self.hour, self.minute, self.add_btn, self.cancel_btn]
        ACCESS.listeners.append(self._paint_access)
        self._paint_access()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(10000)
        self.refresh()

    def _paint_access(self, on=None):
        on = ACCESS.unlocked if on is None else on
        for w in self.guarded:
            w.setEnabled(on)
            w.setToolTip("" if on else "Unlock to use")

    def refresh(self):
        items = schedule.view()
        if items == self.items:
            return
        self.items = items
        self.tree.clear()
        for time_str, st, _cfg in items:
            it = QTreeWidgetItem([time_str, st])
            it.setTextAlignment(0, Qt.AlignCenter)
            it.setTextAlignment(1, Qt.AlignCenter)
            self.tree.addTopLevelItem(it)
        self.detail.clear()

    def _index(self):
        it = self.tree.currentItem()
        return self.tree.indexOfTopLevelItem(it) if it is not None and it.isSelected() else -1

    def _preview(self):
        i = self._index()
        if 0 <= i < len(self.items or []):
            time_str, _st, cfg = self.items[i]
            text = f"=== Saved Configuration for {time_str} ===\n\n" + "".join(f"{k}: {v}\n" for k, v in cfg.items())
            self.detail.setPlainText(text)

    def _add(self):
        if not ACCESS.unlocked:
            return
        ok, title, msg = schedule.add(self.date.text(), self.hour.text(), self.minute.text())
        if not ok:
            (QMessageBox.warning if title == "Limit Reached" else QMessageBox.critical)(self, title, msg)
            return
        self.refresh()

    def _cancel(self):
        if not ACCESS.unlocked:
            return
        i = self._index()
        if i < 0:
            return
        if not schedule.remove(i):
            QMessageBox.warning(self, "Schedule Manager", schedule.TK_OWNS if schedule.tk_owns() else
                                "That schedule no longer exists.")
        self.refresh()
        self.detail.clear()

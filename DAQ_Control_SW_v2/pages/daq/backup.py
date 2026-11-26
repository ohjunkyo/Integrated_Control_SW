"""DAQ System > Backup, mirroring ui_manager._create_backup_viewer: the
sync_data_v2.sh lifecycle log (~/logs/backup_lifecycle.jsonl) as a table.
Run backup now / Dry run start sync_data_v2.sh in the Output tab's Backup slot
(core.maintenance), as the Tk app's console; the table refreshes when it ends."""
import json
import os

from PyQt5.QtCore import QObject, QTimer, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (QHBoxLayout, QLabel, QMessageBox, QPushButton, QTreeWidget, QTreeWidgetItem,
                             QVBoxLayout, QWidget)

from app import theme
from core import file_ops, jobs, maintenance

LIFECYCLE_LOG = os.path.expanduser("~/logs/backup_lifecycle.jsonl")
MAX_ROWS = 500
STATUS_COLOR = {"ok": "good", "fail": "bad", "skip": "hint"}   # theme.C keys


class _Bus(QObject):
    done = pyqtSignal(int)


class BackupPage(QWidget):
    def __init__(self):
        super().__init__()
        self.bus = _Bus()
        self.bus.done.connect(lambda _code: QTimer.singleShot(500, self.refresh))
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.summary = QLabel("Not loaded yet.")
        self.summary.setStyleSheet("font-size: 11pt; font-weight: 700;")
        top.addWidget(self.summary)
        self.disk = QLabel()
        self.disk.setObjectName("hint")
        top.addSpacing(14)
        top.addWidget(self.disk)
        top.addStretch(1)
        ref = QPushButton("Refresh")
        ref.clicked.connect(self.refresh)
        top.addWidget(ref)
        for t, dry in (("Run backup now", False), ("Dry run", True)):
            b = QPushButton(t)
            b.clicked.connect(lambda _=False, d=dry: self.run_backup(d))
            top.addWidget(b)
        lay.addLayout(top)

        self.warn = QLabel()
        self.warn.setWordWrap(True)
        self.warn.setStyleSheet("background: #f8d7da; color: #721c24; font-weight: 700; padding: 6px 10px;")
        self.warn.hide()
        lay.addWidget(self.warn)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Time", "Event", "Status", "File", "Detail"])
        self.tree.setRootIsDecorated(False)
        for i, w in enumerate((150, 90, 80, 330, 300)):
            self.tree.setColumnWidth(i, w)
        lay.addWidget(self.tree, 1)
        self.refresh()

    def refresh(self):
        self.tree.clear()
        if not os.path.exists(LIFECYCLE_LOG):
            self.summary.setText("No backup has run yet.")
            self.summary.setStyleSheet(f"font-size: 11pt; font-weight: 700; color: {theme.C['hint']};")
            self.disk.setText(f"(expected log: {LIFECYCLE_LOG})")
            return
        events = []
        try:
            with open(LIFECYCLE_LOG, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            events.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue   # tolerate a half-written last line
        except OSError as e:
            self.summary.setText(f"Cannot read log: {e}")
            return

        last = next((e for e in reversed(events) if e.get("event") == "run" and e.get("status") == "done"), None)
        if last:
            failed = int(last.get("failed", 0) or 0)
            self.summary.setText(f"Last backup {last.get('ts', '')[:16].replace('T', ' ')}  ·  "
                                 f"{last.get('deleted', '0')} files reclaimed  ·  {last.get('freed_gb', '0')} GB freed")
            self.summary.setStyleSheet(f"font-size: 11pt; font-weight: 700; color: {'#b91c1c' if failed else '#1a7f37'};")
            self.disk.setText(f"local free {last.get('local_free_before', '?')} → {last.get('local_free_after', '?')}")
        else:
            self.summary.setText("No completed run in log yet.")
            self.summary.setStyleSheet(f"font-size: 11pt; font-weight: 700; color: {theme.C['hint']};")

        stuck = [e for e in events if e.get("event") in ("verify", "delete", "copy") and e.get("status") == "fail"]
        if stuck:
            names = ", ".join(e.get("file", e.get("subdir", "?")) for e in stuck[-5:])
            self.warn.setText(f"{len(stuck)} file(s) were NOT reclaimed and remain on local disk (latest: {names}). "
                              "They were kept on purpose: their backup could not be verified.")
            self.warn.show()
        else:
            self.warn.hide()

        items = []
        for e in events[-MAX_ROWS:]:
            st = e.get("status", "")
            detail = " ".join(f"{k}={v}" for k, v in e.items() if k not in ("ts", "event", "status", "file"))
            it = QTreeWidgetItem([e.get("ts", "")[:19].replace("T", " "), e.get("event", ""), st,
                                  e.get("file", e.get("subdir", "")), detail])
            if st in STATUS_COLOR:
                for c in range(5):
                    it.setForeground(c, QColor(theme.C[STATUS_COLOR[st]]))
            items.append(it)
        self.tree.addTopLevelItems(items)
        if items:
            self.tree.scrollToItem(items[-1])

    def run_backup(self, dry_run=False):
        """As the Tk _run_backup_now: confirm a real run, then sync_data_v2.sh in the Backup job slot."""
        if not os.path.exists(maintenance.BACKUP_SCRIPT):
            QMessageBox.critical(self, "Backup", f"Script not found:\n{maintenance.BACKUP_SCRIPT}")
            return
        if not dry_run and QMessageBox.question(self, "Run backup now", maintenance.BACKUP_QUESTION) != QMessageBox.Yes:
            return
        if maintenance.BACKUP_KIND not in jobs.KINDS:
            QMessageBox.warning(self, "Backup", "No Backup job slot in this build (core/jobs.py KINDS).")
            return
        cmd = maintenance.backup_command(dry_run)
        slot, why = jobs.JOBS.start(maintenance.BACKUP_KIND, maintenance.BACKUP_NAME, cmd,
                                    on_done=self.bus.done.emit)
        if slot is None:
            QMessageBox.warning(self, "Console Busy", why)
            return
        file_ops.log(f"[INFO] Console job '{maintenance.BACKUP_NAME}' [{slot}]: {cmd}")
        win = self.window()
        if hasattr(win, "show_page"):
            win.show_page("Output")
        from pages.daq import output
        if output.live() is not None:
            output.live().focus(slot)

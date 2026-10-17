"""General Scan > Scan History, mirroring ui_automation._build_history_tab."""
import glob
import json
import os

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QGroupBox, QHBoxLayout, QPlainTextEdit, QPushButton, QSplitter, QTreeWidget,
                             QTreeWidgetItem, QVBoxLayout, QWidget)

from core import env
from core.history import scan_backend


class HistoryPage(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        split = QSplitter(Qt.Horizontal)

        box = QGroupBox("Past General Scans")
        bl = QVBoxLayout(box)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Date (JST)", "End Time", "DAQ", "Shifter", "Result"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        for i, w in enumerate([100, 80, 110, 100, 80]):
            self.tree.setColumnWidth(i, w)
            self.tree.headerItem().setTextAlignment(i, Qt.AlignCenter)
        self.tree.currentItemChanged.connect(self.show_detail)
        bl.addWidget(self.tree)
        split.addWidget(box)

        dbox = QGroupBox("Run Details && Configuration")
        dl = QVBoxLayout(dbox)
        self.detail = QPlainTextEdit()
        self.detail.setObjectName("dark")
        self.detail.setReadOnly(True)
        dl.addWidget(self.detail)
        split.addWidget(dbox)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 2)
        box.setMinimumWidth(540)     # all five list columns fit without clipping
        split.setSizes([540, 900])
        lay.addWidget(split, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        b = QPushButton("Refresh History")
        b.clicked.connect(self.refresh)
        row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        self.refresh()

    def refresh(self):
        self.tree.clear()
        for f in sorted(glob.glob(os.path.join(env.DAQ_DIR, "LOG", "ScanHistory", "history_*.json")), reverse=True):
            try:
                with open(f, encoding="utf-8") as fh:
                    d = json.load(fh)
            except Exception:
                continue
            daq = scan_backend(env.DAQ_DIR, d)
            it = QTreeWidgetItem([str(d.get("date", "")), str(d.get("end_time", "")),
                                  "unknown" if daq == "?" else daq, str(d.get("shifter", "")), str(d.get("status", ""))])
            for c in range(5):
                it.setTextAlignment(c, Qt.AlignCenter)
            it.setData(0, Qt.UserRole, f)
            self.tree.addTopLevelItem(it)

    def show_detail(self, item, _prev=None):
        if item is None:
            return
        try:
            with open(item.data(0, Qt.UserRole), encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            return
        daq = scan_backend(env.DAQ_DIR, d)
        text = f"=== Scan Finished at {d.get('date')} {d.get('end_time')} ===\n"
        text += f"Shifter: {d.get('shifter')}\nStatus: {d.get('status')}\n"
        text += f"DAQ: {'unknown' if daq == '?' else daq}{'' if d.get('daq_backend') else ' (inferred)'}\n"
        text += "-" * 50 + "\n[ Configuration Snapshot ]\n"
        text += "".join(f"{k}: {v}\n" for k, v in (d.get("config") or {}).items())
        self.detail.setPlainText(text)

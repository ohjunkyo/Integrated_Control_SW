"""Information tab, mirroring managers/info_tab.InfoTab (PMT history)."""
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QPushButton, QTreeWidget, QTreeWidgetItem,
                             QVBoxLayout, QWidget)

from core import env
from core.history import build_pmt_history, hv_text, load_scan_entries

COLS = ["PMT", "Position", "DAQ", "HV [V]", "From", "To", "Scans", "Wavelength [nm]", "Notes"]


class InformationPage(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        head = QHBoxLayout()
        t = QLabel("PMT history")
        t.setStyleSheet("font-size: 14pt; font-weight: 600;")
        head.addWidget(t)
        head.addStretch(1)
        head.addWidget(QLabel("Filter PMT:"))
        self.filter = QLineEdit()
        self.filter.setFixedWidth(160)
        self.filter.textChanged.connect(self.render)
        head.addWidget(self.filter)
        b = QPushButton("Refresh")
        b.clicked.connect(self.refresh)
        head.addWidget(b)
        lay.addLayout(head)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(COLS)
        self.tree.setAlternatingRowColors(True)
        for i, w in enumerate([150, 90, 150, 110, 100, 100, 60, 160, 360]):
            self.tree.setColumnWidth(i, w)
        lay.addWidget(self.tree, 1)
        self.status = QLabel()
        self.status.setObjectName("hint")
        lay.addWidget(self.status)
        self.hist = {}
        self.refresh()

    def refresh(self):
        entries = load_scan_entries(env.DAQ_DIR)
        for i, e in enumerate(entries):
            e["idx"] = i
        self.hist = build_pmt_history(entries)
        span = f"{entries[0]['date']} – {entries[-1]['date']}" if entries else "no scans"
        self.status.setText(f"{len(entries)} scans ({span}), {len(self.hist)} PMTs")
        self.render()

    def render(self):
        opened = {self.tree.topLevelItem(i).data(0, Qt.UserRole)
                  for i in range(self.tree.topLevelItemCount()) if self.tree.topLevelItem(i).isExpanded()}
        self.tree.clear()
        flt = self.filter.text().strip().upper()
        bold = QFont()
        bold.setBold(True)
        for sn, periods in sorted(self.hist.items(), key=lambda kv: kv[1][-1]["to"], reverse=True):
            label = sn + "/".join(dict.fromkeys(p["dir"] for p in periods if p["dir"]))
            if flt and flt not in label.upper():
                continue
            count = {}
            for p in periods:
                if p["hvs"]:
                    count[hv_text(p)] = count.get(hv_text(p), 0) + p["n"]
            main_hv = max(count, key=count.get) if count else ""
            if count and count[main_hv] == 1 and len(count) > 1:
                hvs = [h for p in periods for h in p["hvs"]]
                main_hv = f"{min(hvs)}–{max(hvs)}"
            daq = "/".join(dict.fromkeys(p["daq"] for p in periods if p["daq"] != "?")) or "unknown"
            top = QTreeWidgetItem([label, "/".join(dict.fromkeys(p["pos"] for p in periods)), daq, main_hv,
                                   periods[0]["from"], periods[-1]["to"], str(sum(p["n"] for p in periods)), "", ""])
            top.setData(0, Qt.UserRole, sn)
            for c in range(len(COLS)):
                top.setFont(c, bold)
                if 0 < c < 7:
                    top.setTextAlignment(c, Qt.AlignCenter)
            for p in periods:
                ch = QTreeWidgetItem(top, [sn + p["dir"], p["pos"], "unknown" if p["daq"] == "?" else p["daq"],
                                           hv_text(p) if p["hvs"] else "", p["from"], p["to"], str(p["n"]),
                                           ", ".join(p["wl"]), "; ".join(p["notes"])])
                for c in range(1, 8):
                    ch.setTextAlignment(c, Qt.AlignCenter)
            self.tree.addTopLevelItem(top)
            top.setExpanded(sn in opened)

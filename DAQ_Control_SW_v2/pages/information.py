"""DAQ System > History: PMT history (which PMT sat where, at which HV, when), from the scan history
files, as managers/info_tab.InfoTab."""
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QBrush, QColor, QFont
from PyQt5.QtWidgets import (QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QTreeWidget, QTreeWidgetItem,
                             QVBoxLayout, QWidget)

from app import theme
from core import env
from core.history import build_pmt_history, hv_text, load_scan_entries

COLS = ["PMT", "Position", "DAQ", "HV [V]", "Period", "Scans", "Wavelength [nm]", "Notes"]
NUMERIC = {3, 5}   # columns sorted by their leading number


def _num(text):
    digits = ""
    for ch in text.strip():
        if ch.isdigit():
            digits += ch
        else:
            break
    return int(digits) if digits else -1


class _Item(QTreeWidgetItem):
    def __lt__(self, other):
        c = self.treeWidget().sortColumn() if self.treeWidget() else 0
        if c in NUMERIC:
            return _num(self.text(c)) < _num(other.text(c))
        return self.text(c) < other.text(c)
WIDTHS = [170, 110, 135, 110, 220, 60, 160]


class InformationPage(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 12)
        head = QHBoxLayout()
        t = QLabel("PMT history")
        t.setStyleSheet("font-size: 14pt; font-weight: 600;")
        head.addWidget(t)
        self.status = QLabel()
        self.status.setObjectName("hint")
        head.addWidget(self.status)
        head.addStretch(1)
        head.addWidget(QLabel("Filter PMT:"))
        self.filter = QLineEdit()
        self.filter.setFixedWidth(160)
        self.filter.textChanged.connect(self.render)
        head.addWidget(self.filter)
        for text, fn in (("Expand all", lambda: self._expand(True)), ("Collapse all", lambda: self._expand(False)),
                         ("Refresh", self.refresh)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            head.addWidget(b)
        lay.addLayout(head)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(COLS)
        self.tree.setAlternatingRowColors(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setIndentation(22)
        self.tree.setStyleSheet("QTreeWidget::item { padding: 5px 6px; }")
        for i, w in enumerate(WIDTHS):
            self.tree.setColumnWidth(i, w)
        self.tree.setSortingEnabled(True)
        self.tree.header().setSortIndicatorShown(True)
        self.tree.header().setStretchLastSection(True)
        self.tree.header().setSectionResizeMode(len(COLS) - 1, QHeaderView.Stretch)
        lay.addWidget(self.tree, 1)
        self.hist = {}
        self.refresh()

    def _expand(self, on):
        for i in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(i).setExpanded(on)

    def refresh(self):
        entries = load_scan_entries(env.DAQ_DIR)
        for i, e in enumerate(entries):
            e["idx"] = i
        self.hist = build_pmt_history(entries)
        span = f"{entries[0]['date']} - {entries[-1]['date']}" if entries else "no scans"
        self.status.setText(f"   {len(entries)} scans ({span}), {len(self.hist)} PMTs")
        self.render()

    def render(self):
        opened = {self.tree.topLevelItem(i).data(0, Qt.UserRole)
                  for i in range(self.tree.topLevelItemCount()) if self.tree.topLevelItem(i).isExpanded()}
        order = (self.tree.header().sortIndicatorSection(), self.tree.header().sortIndicatorOrder())
        self.tree.setSortingEnabled(False)
        self.tree.clear()
        flt = self.filter.text().strip().upper()
        bold = QFont()
        bold.setBold(True)
        group_bg = QBrush(QColor(theme.C["header"]))
        shown = 0
        for sn, periods in sorted(self.hist.items(), key=lambda kv: kv[1][-1]["to"], reverse=True):
            label = sn + "/".join(dict.fromkeys(p["dir"] for p in periods if p["dir"]))
            if flt and flt not in label.upper():
                continue
            shown += 1
            count = {}
            for p in periods:
                if p["hvs"]:
                    count[hv_text(p)] = count.get(hv_text(p), 0) + p["n"]
            main_hv = max(count, key=count.get) if count else ""
            if count and count[main_hv] == 1 and len(count) > 1:
                hvs = [h for p in periods for h in p["hvs"]]
                main_hv = f"{min(hvs)}-{max(hvs)}"
            daq = "/".join(dict.fromkeys(p["daq"] for p in periods if p["daq"] != "?")) or "unknown"
            wls = ", ".join(dict.fromkeys(w for p in periods for w in p["wl"]))
            top = _Item([label, "/".join(dict.fromkeys(p["pos"] for p in periods)), daq, main_hv,
                                   f"{periods[0]['from']} - {periods[-1]['to']}", str(sum(p["n"] for p in periods)),
                                   wls, f"{len(periods)} period(s)"])
            top.setData(0, Qt.UserRole, sn)
            for c in range(len(COLS)):
                top.setFont(c, bold)
                top.setBackground(c, group_bg)
                if 0 < c < 7:
                    top.setTextAlignment(c, Qt.AlignCenter)
            for p in periods:
                note = "; ".join(p["notes"])
                ch = _Item(top, [sn + p["dir"], p["pos"], "unknown" if p["daq"] == "?" else p["daq"],
                                           hv_text(p) if p["hvs"] else "", f"{p['from']} - {p['to']}", str(p["n"]),
                                           ", ".join(p["wl"]), note])
                ch.setToolTip(len(COLS) - 1, note)
                for c in range(1, 7):
                    ch.setTextAlignment(c, Qt.AlignCenter)
            self.tree.addTopLevelItem(top)
            top.setExpanded(sn in opened or (bool(flt) and shown <= 3))
        self.tree.setSortingEnabled(True)
        if order[0] >= 0:
            self.tree.sortItems(*order)

"""Run Numbers: a small non-modal window with the run-number ranges; the selected file's range is highlighted."""
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (QAbstractItemView, QDialog, QHeaderView, QLabel, QPushButton, QTableWidget,
                             QTableWidgetItem, QVBoxLayout)

from app import theme
from core import run_info


class RunNumbersDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Run Numbers")
        self.setMinimumWidth(480)
        lay = QVBoxLayout(self)
        self.table = QTableWidget(len(run_info.PLAN), 3)
        self.table.setHorizontalHeaderLabels(["Range", "Use", "Detail"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setShowGrid(False)
        for r, (a, b, use, detail) in enumerate(run_info.PLAN):
            for c, text in enumerate((f"{a:03d}-{b:03d}", use, detail)):
                self.table.setItem(r, c, QTableWidgetItem(text))
        hd = self.table.horizontalHeader()
        hd.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hd.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hd.setStretchLastSection(True)
        self.table.setFixedHeight(self.table.horizontalHeader().height()
                                  + sum(self.table.rowHeight(r) for r in range(len(run_info.PLAN))) + 4)
        lay.addWidget(self.table)
        self.note = QLabel(run_info.NOTE)
        self.note.setStyleSheet(f"color: {theme.C['muted']};")
        lay.addWidget(self.note)
        self.sel = QLabel("")
        lay.addWidget(self.sel)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        lay.addWidget(close, 0, Qt.AlignRight)
        self.row = None

    def show_path(self, path):
        ro = run_info.run_of(path)
        sm = None if ro else run_info.summary_of(path)
        if sm and sm["kind"] == "scan":
            ro = (sm["date"], sm["run0"])
        self.row = run_info.row_of(*ro) if ro else None
        self.sel.setText(f"Selected: {run_info.summary_text(path)}" if sm else
                         f"Selected: {ro[0]}_{ro[1]:03d}" if ro else "")
        bold, plain = QFont(), QFont()
        bold.setBold(True)
        for r in range(self.table.rowCount()):
            on = r == self.row
            for c in range(3):
                it = self.table.item(r, c)
                it.setFont(bold if on else plain)
                it.setBackground(QColor(theme.C["accent_soft"]) if on else QColor(0, 0, 0, 0))


def open_for(owner, path):
    """Show (or reuse) the owner's Run Numbers window, highlighting path's range."""
    dlg = getattr(owner, "_run_numbers_dlg", None)
    if dlg is None:
        dlg = owner._run_numbers_dlg = RunNumbersDialog(owner)
    dlg.show_path(path)
    dlg.show()
    dlg.raise_()


def follow(owner, path):
    """Update an open Run Numbers window when the selection changes."""
    dlg = getattr(owner, "_run_numbers_dlg", None)
    if dlg is not None and dlg.isVisible():
        dlg.show_path(path)

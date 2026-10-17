"""Scan Progress Matrix (Live Scan > Full Grid): scan progress on top, then one table per PMT with X/Y
rows by tilt columns, in the theme.STATE colours (waiting / moving / taking data / done / error). Filled
from core.scan_progress every 2 s, so it shows the running scan whichever app runs it."""
from datetime import datetime

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (QDialog, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QProgressBar, QScrollArea,
                             QVBoxLayout, QWidget)

from app import scan_bridge, theme
from core import env, scan_progress, status

CELL = {"wait": "-", "move": "MOV", "active": "DAQ", "done": "OK", "error": "ERR"}
LEGEND = (("waiting", "wait"), ("moving", "move"), ("taking data", "active"), ("done", "done"), ("error", "error"))


def _paint(lb, state, chip=False):
    bg, fg, border = theme.STATE[state]
    radius = 9 if chip else 4
    lb.setStyleSheet(f"background: {bg}; color: {fg}; border: {2 if state == 'error' else 1}px solid {border};"
                     f" border-radius: {radius}px; font-weight: 700;" + (" padding: 2px 10px;" if chip else
                                                                       " font-size: 9pt;"))


class ScanMatrixDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Scan Progress Matrix")
        self.resize(1460, 560)
        host = scan_bridge.init()
        self.tilts = host.auto_mgr.build_tilt_angles()
        cfg = env.read_config()
        self.pmts = [f"{cfg.get(f'SN{d}', f'SN{d}')}-{cfg.get(f'direction{d}', '')}".rstrip("-") for d in (2, 3)]
        c = theme.C
        v = QVBoxLayout(self)
        head = QHBoxLayout()
        self.title = QLabel("Scan Progress Matrix")
        self.title.setStyleSheet("font-size: 13pt; font-weight: 700;")
        head.addWidget(self.title)
        self.daq_chip = QLabel()
        self.block_chip = QLabel()
        for chip in (self.daq_chip, self.block_chip):
            chip.setFixedHeight(24)
            head.addWidget(chip)
        head.addStretch(1)
        for text, state in LEGEND:
            lb = QLabel(text)
            lb.setFixedHeight(24)
            _paint(lb, state, chip=True)
            head.addWidget(lb)
        v.addLayout(head)
        row = QHBoxLayout()
        self.pct = QLabel("--")
        self.pct.setStyleSheet(f"font-size: 18pt; font-weight: 800; color: {c['accent']};")
        self.pct.setMinimumWidth(70)
        row.addWidget(self.pct)
        self.bar = QProgressBar()
        self.bar.setFixedHeight(24)
        self.bar.setStyleSheet(f"QProgressBar {{ border: 1px solid {c['border']}; border-radius: 6px;"
                               f" background: {c['card']}; text-align: center; font-weight: 700; }}"
                               f"QProgressBar::chunk {{ background: {theme.STATE['done'][0]}; border-radius: 5px; }}")
        row.addWidget(self.bar, 1)
        self.eta = QLabel()
        self.eta.setObjectName("key")
        row.addWidget(self.eta)
        v.addLayout(row)

        area = QScrollArea()
        area.setWidgetResizable(True)
        body = QWidget()
        bl = QVBoxLayout(body)
        self.cells = {}
        for pmt in self.pmts:
            box = QGroupBox(pmt)
            g = QGridLayout(box)
            g.setHorizontalSpacing(3)
            g.setVerticalSpacing(3)
            for col, t in enumerate(self.tilts, start=1):
                h = QLabel(f"{t:g}°")
                h.setAlignment(Qt.AlignCenter)
                h.setObjectName("key")
                g.addWidget(h, 0, col)
            for r, axis in enumerate(("X", "Y"), start=1):
                lab = QLabel(f"{axis}-axis")
                lab.setStyleSheet("font-weight: 700;")
                g.addWidget(lab, r, 0)
                for col, t in enumerate(self.tilts, start=1):
                    cell = QLabel()
                    cell.setAlignment(Qt.AlignCenter)
                    cell.setMinimumSize(46, 30)
                    g.addWidget(cell, r, col)
                    self.cells.setdefault((axis, float(t)), []).append(cell)
            bl.addWidget(box)
        bl.addStretch(1)
        area.setWidget(body)
        v.addWidget(area, 1)
        self._shown = {}
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(2000)
        self.refresh()

    def _chip(self, lb, text, bg):
        lb.setText(text)
        lb.setStyleSheet(f"background: {bg}; color: white; border-radius: 9px; padding: 2px 10px; font-weight: 700;")
        lb.show()

    def refresh(self):
        try:
            s = scan_progress.snapshot()
        except Exception:
            return
        states = {}
        if s.get("running"):
            for r in s["done"]:
                try:
                    states[(r["axis"], float(r["tilt"]))] = "done" if r["status"] == "OK" else "error"
                except (TypeError, ValueError):
                    pass
            axis, tilt = s["current"]
            if axis is not None and tilt is not None:
                moving = any(m for _t, _r, m in status.live_angles().values())
                states[(axis, float(tilt))] = "move" if moving else "active"
            self.title.setText(f"Scan Progress Matrix   -   General Scan {s['tag']}")
            backend = status.daq_backend() or "CAEN"
            self._chip(self.daq_chip, backend, theme.daq_color("hk" if backend.startswith("HK") else "caen")[0])
            if s["block"]:
                i, n, wl = s["block"]
                self._chip(self.block_chip, f"{wl.replace('nm', ' nm')}  block {i}/{n}", theme.C["muted"])
            done, total = len(s["done"]), max(1, s["total"])
            self.pct.setText(f"{100 * done // total}%")
            self.bar.setRange(0, total)
            self.bar.setValue(min(done, total))
            self.bar.setFormat(f"Point {min(done + 1, total)} / {total}")
            eta = s["eta_s"]
            self.eta.setText(f"   ETA {int(eta) // 3600}h {int(eta) % 3600 // 60:02d}m   (finishes "
                             f"{datetime.fromtimestamp(s['now'] + eta):%H:%M})" if eta else "   ETA --")
        else:
            self.title.setText("Scan Progress Matrix   -   no scan running")
            self.daq_chip.hide()
            self.block_chip.hide()
        for key, cells in self.cells.items():
            st = states.get(key, "wait")
            if self._shown.get(key) == st:
                continue
            self._shown[key] = st
            for cell in cells:
                cell.setText(CELL[st])
                _paint(cell, st)


_open = None


def open_matrix(parent):
    """One window, refocused if already open (as the Tk popup)."""
    global _open
    if _open is not None:
        try:
            _open.raise_()
            _open.activateWindow()
            return _open
        except RuntimeError:
            _open = None
    _open = ScanMatrixDialog(parent)
    _open.setAttribute(Qt.WA_DeleteOnClose)
    _open.destroyed.connect(lambda *_: globals().__setitem__("_open", None))
    _open.show()
    return _open

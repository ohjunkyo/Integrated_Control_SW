"""General Scan > Live Scan > Live Console: the running scan step by step (scan progress, the point
being taken, finished points), from core.scan_progress, whichever app runs the scan."""
import time
from datetime import datetime

from PyQt5.QtCore import QTimer
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QProgressBar, QTreeWidget, QTreeWidgetItem,
                             QVBoxLayout, QWidget)

from app import theme
from core import scan_progress, status

PHASES = {"ACQUIRING": theme.STATE["active"][:2], "MOVING": theme.STATE["move"][:2], "SETTLING": ("#6c757d", "#ffffff")}
STATUS_COLOR = {"OK": "good", "ERROR": "bad", "ERR": "bad"}   # theme.C keys


def _hms(s):
    s = int(max(0, s))
    return f"{s // 3600}h {s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def _chip(text="", bg="#6c757d", fg="white"):
    lb = QLabel(text)
    lb.setStyleSheet(f"background: {bg}; color: {fg}; border-radius: 10px; padding: 3px 12px; font-weight: 700;")
    return lb


class _Bar(QProgressBar):
    def __init__(self, color, height):
        super().__init__()
        self.setFixedHeight(height)
        self.set_color(color)

    def set_color(self, color):
        c = theme.C
        self.setStyleSheet(f"QProgressBar {{ border: 1px solid {c['border']}; border-radius: 6px;"
                           f" background: {c['field_ro']}; text-align: center; font-weight: 700; }}"
                           f"QProgressBar::chunk {{ background: {color}; border-radius: 5px; }}")


class LiveConsole(QWidget):
    def __init__(self):
        super().__init__()
        c = theme.C
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        head = QHBoxLayout()
        self.title = QLabel("No General Scan running")
        self.title.setStyleSheet("font-size: 12pt; font-weight: 700;")
        head.addWidget(self.title)
        self.daq_chip = _chip()
        self.block_chip = _chip(bg=c["muted"])
        head.addWidget(self.daq_chip)
        head.addWidget(self.block_chip)
        head.addStretch(1)
        v.addLayout(head)

        self.body = QWidget()
        bv = QVBoxLayout(self.body)
        bv.setContentsMargins(0, 0, 0, 0)
        bv.setSpacing(8)
        row = QHBoxLayout()
        self.pct = QLabel("--")
        self.pct.setStyleSheet(f"font-size: 20pt; font-weight: 800; color: {c['accent']};")
        self.pct.setMinimumWidth(80)
        row.addWidget(self.pct)
        self.scan_bar = _Bar(theme.STATE["done"][0], 26)
        row.addWidget(self.scan_bar, 1)
        bv.addLayout(row)
        self.eta = QLabel()
        self.eta.setObjectName("key")
        bv.addWidget(self.eta)

        self.card = QFrame()
        self.card.setObjectName("pointCard")
        self.card.setStyleSheet(f"QFrame#pointCard {{ background: {c['card']}; border: 1px solid {c['border']};"
                                " border-radius: 8px; }")
        cv = QVBoxLayout(self.card)
        l1 = QHBoxLayout()
        self.now = QLabel()
        self.now.setStyleSheet("font-size: 12pt; font-weight: 700;")
        l1.addWidget(self.now)
        l1.addStretch(1)
        self.phase = _chip()
        l1.addWidget(self.phase)
        cv.addLayout(l1)
        self.point_bar = _Bar(PHASES["ACQUIRING"][0], 18)
        cv.addWidget(self.point_bar)
        g = QGridLayout()
        self.vals = {}
        for i, (key, cap) in enumerate((("rate", "RATE"), ("left", "POINT LEFT"), ("ch0", "PED CH0"),
                                        ("ch1", "PED CH1"), ("ch2", "PED CH2"), ("ch3", "PED CH3"))):
            a = QLabel(cap)
            a.setStyleSheet(f"color: {c['muted']}; font-size: 8.5pt; font-weight: 600;")
            b = QLabel("--")
            b.setStyleSheet("font-size: 11.5pt; font-weight: 700;")
            g.addWidget(a, 0, i)
            g.addWidget(b, 1, i)
            self.vals[key] = b
        cv.addLayout(g)
        bv.addWidget(self.card)
        v.addWidget(self.body)

        lab = QLabel("Finished points (newest first)")
        lab.setObjectName("key")
        v.addWidget(lab)
        self.steps = QTreeWidget()
        self.steps.setRootIsDecorated(False)
        self.steps.setHeaderLabels(["Time", "Axis", "Tilt", "WL", "Status", "Run", "Took"])
        for i, w in enumerate((64, 46, 60, 64, 70, 56)):
            self.steps.setColumnWidth(i, w)
        v.addWidget(self.steps, 1)
        self.body.hide()

        self._last = None          # (events, time) for the rate when events are estimated
        self._idle_at = 0          # last time the finished scan was read while idle
        self._shown = None         # number of finished rows on screen
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.refresh()

    # ------------------------------------------------------------------------------------------
    def _phase(self, s):
        try:
            if any(m for _t, _r, m in status.live_angles().values()):
                return "MOVING"
            if status._daq_running():
                return "ACQUIRING"
        except Exception:
            pass
        return "SETTLING"

    def refresh(self):
        if not self.isVisible() and self._shown is not None:
            return
        try:
            s = scan_progress.snapshot()
        except Exception:
            return
        if not s.get("running"):
            self.daq_chip.hide()
            self.block_chip.hide()
            self.body.hide()
            if time.time() - self._idle_at < 10 and self._shown is not None:
                return
            self._idle_at = time.time()
            last = scan_progress.last_scan()
            if last is None:
                self.title.setText("No General Scan running")
                self._fill([], None)
                return
            end = datetime.fromtimestamp(last["ended"]).strftime("%m-%d %H:%M")
            self.title.setText(f"Last General Scan {last['tag']}   {last['status'].lower()} {end}")
            self._fill(last["done"], ("last", last["tag"], len(last["done"])))
            return
        self.body.show()
        self.title.setText(f"General Scan {s['tag']}")
        backend = status.daq_backend() or "CAEN"
        strong, _soft = theme.daq_color("hk" if backend.startswith("HK") else "caen")
        self.daq_chip.setText(backend)
        self.daq_chip.setStyleSheet(f"background: {strong}; color: white; border-radius: 10px; padding: 3px 12px;"
                                    " font-weight: 700;")
        self.daq_chip.show()
        if s["block"]:
            i, n, wl = s["block"]
            self.block_chip.setText(f"{wl.replace('nm', ' nm')}  block {i}/{n}")
            self.block_chip.show()
        else:
            self.block_chip.hide()

        done, total = len(s["done"]), max(1, s["total"])
        self.pct.setText(f"{100 * done // total}%")
        self.scan_bar.setRange(0, total)
        self.scan_bar.setValue(min(done, total))
        self.scan_bar.setFormat(f"Point {min(done + 1, total)} / {total}")
        started = datetime.fromtimestamp(s["started"]).strftime("%H:%M")
        if s["eta_s"] is not None:
            end = datetime.fromtimestamp(s["now"] + s["eta_s"]).strftime("%H:%M")
            self.eta.setText(f"Scan ETA  {_hms(s['eta_s'])}   (finishes {end})      started {started}")
        else:
            self.eta.setText(f"Scan ETA  --      started {started}")

        axis, tilt = s["current"]
        self.now.setText(f"Now   {axis}-axis   tilt {tilt}°" if axis is not None else "Now   --")
        phase = self._phase(s)
        self.phase.setText(phase)
        self.phase.setStyleSheet(f"background: {PHASES[phase][0]}; color: {PHASES[phase][1]}; border-radius: 10px;"
                                 " padding: 3px 12px; font-weight: 700;")
        p = s["point"]
        if p and p["total"]:
            ev, tot = p["events"], p["total"]
            self.point_bar.set_color(PHASES["ACQUIRING" if phase == "ACQUIRING" else "SETTLING"][0])
            self.point_bar.setRange(0, tot)
            self.point_bar.setValue(min(ev, tot))
            mark = "" if p["exact"] else "~"
            self.point_bar.setFormat(f"{mark}{ev:,} / {tot:,} events")
            now = time.time()
            rate = None
            if self._last and ev > self._last[0] and now > self._last[1]:
                rate = (ev - self._last[0]) / (now - self._last[1])
            if self._last is None or ev < self._last[0] or now - self._last[1] > 5:
                self._last = (ev, now)
            self.vals["rate"].setText(f"{mark}{rate / 1000:.2f} k evt/s" if rate else "--")
            self.vals["left"].setText(f"{mark}{_hms((tot - ev) / rate)}" if rate else "--")
            peds = {}
            for part in p["ped"].split("|"):
                name, _, value = part.strip().partition(":")
                if name.startswith("Ch") and value:
                    peds[name[2:]] = value.strip()
            for ch in range(4):
                self.vals[f"ch{ch}"].setText(peds.get(str(ch), "--"))
        else:
            self.point_bar.setRange(0, 1)
            self.point_bar.setValue(0)
            self.point_bar.setFormat("waiting for the point to start")
            for val in self.vals.values():
                val.setText("--")

        self._fill(s["done"], (s["tag"], done))

    def _fill(self, rows, key):
        if self._shown == key and key is not None:
            return
        self._shown = key
        self.steps.clear()
        for r in rows:
            it = QTreeWidgetItem([datetime.fromtimestamp(r["t"]).strftime("%H:%M"), str(r["axis"]),
                                  f"{r['tilt']}°", str(r["wl"] or ""), r["status"], r["run"],
                                  f"{int(r['took_s']) // 60}m{int(r['took_s']) % 60:02d}s"])
            it.setForeground(4, QColor(theme.C.get(STATUS_COLOR.get(r["status"], "text"), theme.C["text"])))
            self.steps.addTopLevelItem(it)

"""Function Generator tab: the CAEN DT1081B that makes the DAQ and laser triggers.

Top: connection, device summary and one card per section (function, I/O standards, connector use).
Sub-tabs: Section A-D (every function with its parameters, I/O and per-channel settings, results),
Logic Analyser, Device (clock, files, front panel) and Change Log. Writes need Unlock.
"""
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTabWidget,
                             QVBoxLayout, QWidget)

from app import theme
from core import env
from core import fg_schema as S
from core import function_generator as FG
from pages.fg_parts import SECTIONS, DeviceTab, LogicAnalyserTab, LogTab, Runner, SectionTab
from widgets import basic

REFRESH_MS = 3000
STD_IN = {0: "NIM", 1: "TTL", 2: "DISC"}
STD_OUT = {0: "NIM", 1: "TTL"}


class SectionCard(QFrame):
    def __init__(self, section, on_click):
        super().__init__()
        self.section = section
        self.setFrameShape(QFrame.StyledPanel)
        self.setCursor(Qt.PointingHandCursor)
        self._on_click = on_click
        g = QGridLayout(self)
        g.setContentsMargins(10, 8, 10, 8)
        g.setVerticalSpacing(4)
        t = QLabel(f"Section {SECTIONS[section]}")
        t.setStyleSheet("font-size: 12pt; font-weight: 700;")
        g.addWidget(t, 0, 0, 1, 2)
        self.fn = QLabel("-")
        self.fn.setStyleSheet("font-size: 12pt; font-weight: 700;")
        g.addWidget(self.fn, 0, 2, 1, 5, Qt.AlignRight)
        self.summary = QLabel("")
        g.addWidget(self.summary, 1, 0, 1, 7)
        self.io = QLabel("")
        self.io.setObjectName("key")
        g.addWidget(self.io, 2, 0, 1, 7)
        self.in_lamps = self._lamp_row(g, 3, "IN", 6)
        self.out_lamps = self._lamp_row(g, 4, "OUT", 4)

    @staticmethod
    def _lamp_row(g, row, name, n):
        h = QHBoxLayout()
        h.setSpacing(6)
        lab = QLabel(name)
        lab.setFixedWidth(34)
        h.addWidget(lab)
        lamps = []
        for i in range(n):
            lamp = basic.make_lamp(11)
            num = QLabel(str(i + 1))
            num.setObjectName("key")
            h.addWidget(num)
            h.addWidget(lamp)
            h.addSpacing(4)
            lamps.append(lamp)
        h.addStretch(1)
        g.addLayout(h, row, 0, 1, 7)
        return lamps

    def mousePressEvent(self, e):
        self._on_click(self.section)

    def show_state(self, st):
        fn = st["function"]
        self.fn.setText(S.display(fn))
        self.summary.setText(S.summary(fn, st.get("config", {})) or " ")
        i, o = st.get("input", {}), st.get("output", {})
        inp = STD_IN.get(i.get("standard"), "?")
        if i.get("standard") == 2:
            inp += f" {i.get('threshold')} mV"
        self.io.setText(f"IN {inp}, {'50 Ohm' if i.get('imp') else '1 kOhm'}   |   OUT {STD_OUT.get(o.get('standard'), '?')}")
        ins, outs = S.ROLES.get(fn, ({}, {}))
        cs = st.get("ch_status", [])
        off = theme.C.get("hint", "#999")
        for k, lamp in enumerate(self.in_lamps):
            used = (k + 1) in ins and (cs[k] if k < len(cs) else 1)
            basic.paint_lamp(lamp, basic.LAMP_ON if used else off)
            lamp.setToolTip(f"IN {k + 1}: {ins.get(k + 1, 'unused')}")
        cfg = st.get("config", {})
        out_en = None
        if fn in ("fanout", "pulse_generator", "digital_generator", "pattern_generator"):
            out_en = [bool(e.get("enable")) for e in (cfg.get("lemo_enables") or [])]
        for k, lamp in enumerate(self.out_lamps):
            used = (k + 1) in outs and (out_en[k] if out_en and k < len(out_en) else True)
            basic.paint_lamp(lamp, basic.LAMP_ON if used else off)
            lamp.setToolTip(f"OUT {k + 1}: {outs.get(k + 1, 'unused')}")


class FunctionGeneratorPage(QWidget):
    def __init__(self):
        super().__init__()
        self.runner = Runner()
        self.device = {}
        self.functions = {}
        self._busy = False
        self._shown_device = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)

        bar = QFrame()
        bar.setFrameShape(QFrame.StyledPanel)
        bl = QHBoxLayout(bar)
        self.conn = QLabel("Disconnected")
        self.conn.setStyleSheet(f"font-size: 12pt; font-weight: 700; color: {theme.C['bad']};")
        bl.addWidget(self.conn)
        bl.addSpacing(15)
        bl.addWidget(QLabel("Host:"))
        h, _p = FG.host()
        self.host = QLineEdit(h)
        self.host.setFixedWidth(140)
        bl.addWidget(self.host)
        b = QPushButton("Reconnect")
        b.clicked.connect(self._reconnect)
        bl.addWidget(b)
        b2 = QPushButton("Refresh")
        b2.clicked.connect(self.refresh_all)
        bl.addWidget(b2)
        bl.addSpacing(15)
        self.dev_info = QLabel("")
        self.dev_info.setObjectName("key")
        bl.addWidget(self.dev_info)
        bl.addStretch(1)
        self.status = QLabel("")
        bl.addWidget(self.status)
        lay.addWidget(bar)

        cards = QHBoxLayout()
        self.cards = [SectionCard(s, self._goto) for s in range(4)]
        for c in self.cards:
            cards.addWidget(c, 1)
        lay.addLayout(cards)

        self.sub = QTabWidget()
        self.sections = [SectionTab(s, self) for s in range(4)]
        for s, t in enumerate(self.sections):
            self.sub.addTab(t, f"Section {SECTIONS[s]}")
        self.la_tab = LogicAnalyserTab(self)
        self.dev_tab = DeviceTab(self)
        self.log_tab = LogTab()
        self.sub.addTab(self.la_tab, "Logic Analyser")
        self.sub.addTab(self.dev_tab, "Device")
        self.sub.addTab(self.log_tab, "Change Log")
        self.sub.currentChanged.connect(self._tab_changed)
        lay.addWidget(self.sub, 1)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(REFRESH_MS)
        self.log_tab.fill()
        QTimer.singleShot(0, self.refresh_all)

    # ---- navigation
    def _goto(self, section):
        self.sub.setCurrentIndex(section)

    def _tab_changed(self, i):
        w = self.sub.widget(i)
        if isinstance(w, SectionTab):
            w.reload(force=w.fn is None)
        elif w is self.dev_tab and not self._shown_device:
            self._shown_device = True
            self.dev_tab.first_show()
        elif w is self.la_tab:
            self.la_tab.read_trigger()
        elif w is self.log_tab:
            self.log_tab.fill()

    # ---- refresh
    def showEvent(self, e):
        super().showEvent(e)
        self.refresh_all()

    def _tick(self):
        if not self.isVisible():
            return
        self.refresh_all()
        w = self.sub.currentWidget()
        if isinstance(w, SectionTab) and w.fn is not None:
            w.poll()

    def refresh_all(self):
        if self._busy:
            return
        self._busy = True

        def work():
            d = FG.read_device()
            secs = [FG.read_section(s, d["functions"].get(s, "")) for s in range(4)]
            return d, secs
        self.runner.run(work, self._loaded)

    def _loaded(self, res, err):
        self._busy = False
        if err:
            self.conn.setText("Disconnected")
            self.conn.setStyleSheet(f"font-size: 12pt; font-weight: 700; color: {theme.C['bad']};")
            self.set_status(err, False)
            return
        d, secs = res
        first = not self.functions
        self.device, self.functions = d, d["functions"]
        h, p = FG.host()
        self.conn.setText(f"Connected  {h}")
        self.conn.setStyleSheet(f"font-size: 12pt; font-weight: 700; color: {theme.C['good']};")
        v = d.get("version") or {}
        clk = {"1": "external", "2": "internal", "0": "internal (external missing)"}.get(d.get("clock"), "?")
        self.dev_info.setText(f"DT1081B  S/N {v.get('serial_number', '?')}  SW {v.get('software_version', '?')}  "
                              f"clock {clk}{', clock out on' if d.get('clk_out') == 1 else ''}")
        for c, st in zip(self.cards, secs):
            c.show_state(st)
        self.dev_tab.show_device(d)
        if first:
            w = self.sub.currentWidget()
            if isinstance(w, SectionTab):
                w.reload(force=True)

    def set_status(self, text, ok):
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {theme.C['good'] if ok else theme.C['bad']};")

    def _reconnect(self):
        env.save_setting("fg_host", self.host.text().strip() or FG.DEFAULT_HOST)
        self.functions = {}
        self.refresh_all()

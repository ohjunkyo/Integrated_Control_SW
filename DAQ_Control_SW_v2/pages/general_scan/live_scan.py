"""General Scan > Live Scan, mirroring the Tk tab: toolbar on top, Live Console
(left, 40 %) and the plot (right, 60 %). Data comes from core/live_scan.py;
polling (file discovery + ROOT reads) runs in a worker thread."""
import threading
import time

from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from PyQt5.QtCore import QObject, QTimer, pyqtSignal
from PyQt5.QtWidgets import (QAction, QButtonGroup, QCheckBox, QFrame, QGroupBox, QHBoxLayout, QLabel, QMenu,
                             QPushButton, QRadioButton, QToolButton, QVBoxLayout, QWidget)

from app import theme
from core import status
from core.live_scan import METRICS, LiveScanModel, uproot

MUTED, TEXT = theme.C["muted"], theme.C["text"]
POLL_MS = 4000
CH_STYLE = {1: ("o", "#378ADD"), 2: ("^", "#1D9E75")}   # single dataset: colour per PMT
MON_STYLE = ("s", "#BA7517")
CH_MARKER = {0: "s", 1: "o", 2: "^"}                     # overlay: colour = dataset, marker = PMT
DS_COLORS = ["#378ADD", "#D85A30", "#7F77DD", "#1D9E75", "#D4537E", "#BA7517"]
DS_STYLES = ["-", "--", ":", "-."]


class _Bus(QObject):
    polled = pyqtSignal(bool, str)   # changed, error text


class LiveScanPage(QWidget):
    def __init__(self):
        super().__init__()
        self.model = LiveScanModel(lambda: ("GS", None, None) if status.scan_running() else None)
        self._callout = None
        self._pick_map = {}
        self._polling = False
        self.bus = _Bus()
        self.bus.polled.connect(self._on_polled)
        self._build()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(POLL_MS)
        self.poll()

    # ---------------------------------------------------------------- widgets
    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        row1 = QHBoxLayout()
        self.ds_btn = QToolButton()
        self.ds_btn.setText("Dataset: (none)")
        self.ds_btn.setMinimumWidth(340)
        self.ds_btn.setPopupMode(QToolButton.InstantPopup)
        self.ds_menu = QMenu(self.ds_btn)
        self.ds_btn.setMenu(self.ds_menu)
        row1.addWidget(self.ds_btn)
        self.live_chip = QLabel("LIVE")
        self.live_chip.setStyleSheet("background: #2e9e4f; color: white; font-weight: 600; padding: 2px 8px;")
        self.live_chip.hide()
        row1.addWidget(self.live_chip)
        self.status = QLabel("waiting for data...")
        self.status.setStyleSheet(f"color: {MUTED};")
        row1.addWidget(self.status, 1)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.poll)
        row1.addWidget(refresh)
        grid = QPushButton("Full Grid")
        grid.clicked.connect(self._full_grid)
        row1.addWidget(grid)
        lay.addLayout(row1)

        row2 = QHBoxLayout()
        self.groups = {}
        m = self.model
        for name, label, options, value in (
                ("metric", "Metric", [("QE", "qe"), ("Gain", "gain"), ("TTS", "tts"), ("ChargeRes", "chres")], m.metric),
                ("axis", "X axis", [("Raw Stage", "raw"), ("Hamamatsu", "hamamatsu"), ("Time", "time")], m.axis_mode),
                ("filter", "Scan axis", [("Both", "both"), ("X", "X"), ("Y", "Y")], m.axis_filter)):
            row2.addWidget(QLabel(label + ":"))
            g = QButtonGroup(self)
            for text, val in options:
                rb = QRadioButton(text)
                rb.setProperty("val", val)
                rb.setChecked(value == val)
                g.addButton(rb)
                row2.addWidget(rb)
            g.buttonClicked.connect(lambda btn, n=name: self._on_radio(n, btn.property("val")))
            self.groups[name] = g
            sep = QFrame()
            sep.setFrameShape(QFrame.VLine)
            sep.setStyleSheet(f"color: {theme.C['border']};")
            row2.addWidget(sep)
        mon = QCheckBox("Monitor-normalize")
        mon.toggled.connect(self._on_mon)
        row2.addWidget(mon)
        row2.addStretch(1)
        lay.addLayout(row2)

        body = QHBoxLayout()
        console = QGroupBox("Live Console")
        cl = QVBoxLayout(console)
        cl.addWidget(self._build_console())
        body.addWidget(console, 4)
        plot = QWidget()
        pl = QVBoxLayout(plot)
        pl.setContentsMargins(0, 0, 0, 0)
        self.fig = Figure(figsize=(7.2, 5.0), dpi=100)
        gs = self.fig.add_gridspec(3, 1, hspace=0.08)
        self.ax_test = self.fig.add_subplot(gs[0:2, 0])
        self.ax_mon = self.fig.add_subplot(gs[2, 0], sharex=self.ax_test)
        self.fig.subplots_adjust(left=0.11, right=0.985, top=0.95, bottom=0.1)
        self.canvas = FigureCanvasQTAgg(self.fig)
        self.canvas.mpl_connect("pick_event", self._on_pick)
        pl.addWidget(self.canvas, 1)
        body.addWidget(plot, 6)
        lay.addLayout(body, 1)
        self.redraw()

    # ---------------------------------------------------------------- polling
    def _full_grid(self):
        from pages.general_scan.scan_matrix import open_matrix
        open_matrix(self)

    def _build_console(self):
        from pages.general_scan.live_console import LiveConsole
        self.live_console = LiveConsole()
        return self.live_console

    def poll(self):
        if uproot is None:
            self.status.setText("uproot not installed")
            return
        if self._polling:
            return
        self._polling = True

        def work():
            try:
                changed, err = self.model.poll(), ""
            except Exception as e:
                changed, err = False, str(e)
            self.bus.polled.emit(changed, err)
        threading.Thread(target=work, daemon=True).start()

    def _on_polled(self, changed, err):
        self._polling = False
        if err:
            self.status.setText(f"poll failed: {err}")
            return
        self._sync_radio("axis", self.model.axis_mode)
        if changed:
            self._refresh_menu()
            self.redraw()
        self._update_status()

    # ---------------------------------------------------------------- controls
    def _on_radio(self, name, val):
        m = self.model
        if name == "metric":
            m.metric = val
        elif name == "axis":
            m.axis_mode, m.axis_auto = val, False
        else:
            m.axis_filter = val
        self.redraw()

    def _on_mon(self, on):
        self.model.mon_norm = on
        self.redraw()

    def _sync_radio(self, name, val):
        for b in self.groups[name].buttons():
            if b.property("val") == val:
                b.setChecked(True)

    def _refresh_menu(self):
        m = self.ds_menu
        m.clear()
        model = self.model
        if not model.order:
            m.addAction("(no data yet)").setEnabled(False)
            return
        recent, more = model.menu_keys()

        def add(menu, key):
            a = QAction(model.label(key), menu)
            a.setCheckable(True)
            a.setChecked(key in model.selected)
            a.toggled.connect(lambda on, k=key: self._toggle(k, on))
            menu.addAction(a)
        for key in recent:
            add(m, key)
        if more:
            m.addSeparator()
            sub = m.addMenu("More")
            for key in more:
                add(sub, key)
        m.addSeparator()
        m.addAction("Follow newest", self._follow_newest)
        sel = model.selected
        self.ds_btn.setText("Dataset: " + (model.label(sel[0]) if len(sel) == 1
                                           else f"{len(sel)} selected" if sel else "(none)"))

    def _toggle(self, key, on):
        self.model.toggle(key, on)
        self._callout = None
        self._sync_radio("axis", self.model.axis_mode)
        self._refresh_menu()
        self.redraw()
        self.poll()

    def _follow_newest(self):
        self.model.follow_newest()
        self.poll()

    def _update_status(self):
        txt, live = self.model.status()
        if txt is None:
            self.status.setText("waiting for data...")
            self.status.setStyleSheet(f"color: {MUTED};")
            self.live_chip.hide()
            return
        self.status.setText(txt)
        self.status.setStyleSheet(f"color: {theme.C['good'] if live else TEXT};")
        self.live_chip.setVisible(live)

    # ---------------------------------------------------------------- drawing
    @staticmethod
    def _headroom(ax, frac=0.22):
        lo, hi = ax.get_ylim()
        if hi > lo:
            ax.set_ylim(lo, hi + (hi - lo) * frac)

    def redraw(self):
        m = self.model
        for ax in (self.ax_test, self.ax_mon):
            ax.clear()
            ax.grid(alpha=0.3)
        self._callout = None
        self._pick_map = {}
        label = METRICS[m.metric][0]
        multi = len(m.selected) > 1
        any_data, ds_handles = False, []
        for i, key in enumerate(list(m.selected)):
            color, ls = DS_COLORS[i % len(DS_COLORS)], DS_STYLES[i % len(DS_STYLES)]
            for ax, chans in ((self.ax_test, (1, 2)), (self.ax_mon, (0,))):
                for ch in chans:
                    xs, ys, es, recs = m.series(key, ch)
                    if not xs:
                        continue
                    any_data = True
                    if multi:
                        c, mk, lab = color, CH_MARKER[ch], None
                    else:
                        mk, c = CH_STYLE.get(ch, MON_STYLE)
                        sn = recs[0]["sn"].get(ch, f"ch{ch}")
                        lab = f"{sn} (monitor)" if ch == 0 else sn
                    fills = [c if p["axis"].get(ch) != "Y" or m.axis_mode != "time" else "none" for p in recs]
                    cont = ax.errorbar(xs, ys, yerr=es, fmt=mk, ms=4.5, capsize=2, color=c, linestyle="none",
                                       label=lab, picker=6)
                    line = cont.lines[0]
                    if multi and m.axis_mode != "time":
                        for axis_name in ("X", "Y"):
                            pts = sorted((x, y) for x, y, p in zip(xs, ys, recs) if p["axis"].get(ch) == axis_name)
                            if len(pts) > 1:
                                ax.plot([q[0] for q in pts], [q[1] for q in pts], color=c, linestyle=ls, lw=1)
                    if m.axis_mode == "time" and "none" in fills:
                        line.set_markerfacecolor("none")
                        solid = [k for k, f in enumerate(fills) if f != "none"]
                        if solid:
                            ax.plot([xs[k] for k in solid], [ys[k] for k in solid], mk, ms=4.5, color=c,
                                    linestyle="none")
                    self._pick_map[line] = (key, ch, recs)
            if multi:
                ds_handles.append(Line2D([0], [0], color=color, linestyle=ls, lw=2,
                                         label=f"{chr(65 + i)}  {m.label(key)}"))
        xlabel = {"raw": "Raw Stage angle [degree]", "hamamatsu": "Hamamatsu incidence angle [degree]",
                  "time": "Elapsed time since dataset start [h]"}[m.axis_mode]
        self.ax_test.set_ylabel(f"{label} / Monitor" if m.mon_norm else label)
        self.ax_mon.set_ylabel(label.split(" (")[0].split(" [")[0] + " (mon.)")
        self.ax_mon.set_xlabel(xlabel)
        self.ax_test.tick_params(labelbottom=False)
        if len(m.selected) == 1 and m.selected[0] in m.sessions:
            s = m.sessions[m.selected[0]]
            self.fig.suptitle(f"{s['date']}  ·  {s['kind']} {s['wl'] or '?'} nm  ·  runs "
                              f"{s['runs'][0]}-{s['runs'][-1]}", fontsize=8, color="#555555", x=0.99, y=0.995,
                              ha="right")
        else:
            self.fig.suptitle("")
        if any_data:
            for ax in (self.ax_test, self.ax_mon):
                self._headroom(ax)
            if multi:
                pmt = [Line2D([0], [0], marker=CH_MARKER[c], color="#555555", linestyle="none", label=n)
                       for c, n in ((1, "Rot1"), (2, "Rot2"))]
                leg = self.ax_test.legend(handles=ds_handles + pmt, loc="upper right", fontsize=7, framealpha=0.92)
            else:
                leg = self.ax_test.legend(loc="upper right", fontsize=7, framealpha=0.9, markerscale=0.8)
                self.ax_mon.legend(loc="upper right", fontsize=7, framealpha=0.9, markerscale=0.8).set_zorder(5)
            leg.set_zorder(5)
        else:
            self.ax_test.text(0.5, 0.5, "No points yet.\nPoints appear as each one finishes analysis.",
                              ha="center", va="center", transform=self.ax_test.transAxes, color="#888", fontsize=10)
        self.canvas.draw_idle()

    def _on_pick(self, event):
        """Click a point: all four metrics, plus the same point in every other overlaid dataset."""
        entry = self._pick_map.get(event.artist)
        if not entry or not len(event.ind):
            return
        m = self.model
        key, ch, recs = entry
        idx = event.ind[0]
        p = recs[idx]
        tag = chr(65 + m.selected.index(key)) + " · " if len(m.selected) > 1 and key in m.selected else ""
        lines = [f"{tag}run {p['run']:03d}  ·  {p['sn'].get(ch, f'ch{ch}')}",
                 f"{p['axis'].get(ch, '?')} {p['raw'][ch]:+.0f}°  ham {p['ham'][ch]:+.1f}°",
                 time.strftime("%m-%d %H:%M", time.localtime(p["time"]))]
        for mk, (label, *_r) in METRICS.items():
            v = p["data"].get(mk, {}).get(ch)
            if v:
                short = label.split(" (")[0].split(" [")[0]
                lines.append(f"{short:<6} {v[0]:.3g}" + (f" ± {v[1]:.2g}" if v[1] else ""))
        mine = p["data"].get(m.metric, {}).get(ch)
        for j, other in enumerate(m.selected):
            if other == key:
                continue
            q = m.matching(other, ch, p)
            ov = q["data"].get(m.metric, {}).get(ch) if q else None
            if ov and mine and mine[0]:
                lines.append(f"{chr(65 + j)}: {ov[0]:.3g} ({(ov[0] / mine[0] - 1) * 100:+.1f}%)")
        ax = self.ax_test if ch in (1, 2) else self.ax_mon
        x0, y0 = event.artist.get_data()[0][idx], event.artist.get_data()[1][idx]
        if self._callout is not None:
            try:
                self._callout.remove()
            except Exception:
                pass
        xlo, xhi = ax.get_xlim()
        ylo, yhi = ax.get_ylim()
        right = xhi > xlo and (x0 - xlo) / (xhi - xlo) > 0.7
        top = yhi > ylo and (y0 - ylo) / (yhi - ylo) > 0.7
        self._callout = ax.annotate(
            "\n".join(lines), xy=(x0, y0), xytext=(-14 if right else 14, -14 if top else 14),
            textcoords="offset points", fontsize=8, family="monospace", color="#e8ecf4",
            ha="right" if right else "left", va="top" if top else "bottom", zorder=50,
            bbox=dict(boxstyle="round,pad=0.6,rounding_size=0.8", fc="#1b1f2a", ec="#3ddc84", lw=1.1, alpha=0.95),
            arrowprops=dict(arrowstyle="->", color="#3ddc84", lw=1.2))
        self.canvas.draw_idle()

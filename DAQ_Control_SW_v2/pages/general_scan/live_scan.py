"""General Scan > Live Scan, mirroring the Tk tab: toolbar on top, Live Console
(left, 40 %) and the plot card (right, 60 %; the same card as Daily Routine > Results). Data comes from
core/live_scan.py; polling (file discovery + ROOT reads) runs in a worker thread."""
import os
import threading
import time

from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (QAction, QButtonGroup, QCheckBox, QFileDialog, QFrame, QGroupBox, QHBoxLayout, QLabel,
                             QMenu, QPushButton, QRadioButton, QToolButton, QVBoxLayout, QWidget)

from app import theme
from app.spinner import Spinner
from core import env, status
from core import hk_results as HK
from core.live_scan import METRICS, LiveScanModel, uproot
from pages.routine_results import _Card, _GripSplitter

MUTED, TEXT = theme.C["muted"], theme.C["text"]
POLL_MS = 4000
CH_STYLE = {1: ("o", "#378ADD"), 2: ("^", "#1D9E75")}   # single dataset: colour per PMT
MON_STYLE = ("s", "#BA7517")
CH_MARKER = {0: "s", 1: "o", 2: "^"}                     # overlay: colour = dataset, marker = PMT
DS_COLORS = ["#378ADD", "#D85A30", "#7F77DD", "#1D9E75", "#D4537E", "#BA7517"]
DS_STYLES = ["-", "--", ":", "-."]
FAIL_ALPHA = 0.5   # failed-fit points: open, faded, no error bar
CH_NAME = {0: "Monitor", 1: "Rot1", 2: "Rot2"}
EXPORT_DIR = os.path.join(env.ADC_DIR, "Data", "LiveScan")
NOTE_S = 10                  # an export message stays in the status line this long
METRIC_NAMES = {"caen": {"qe": "QE", "gain": "Gain", "tts": "TTS", "chres": "ChargeRes"},
                "hk": {"qe": "Hits / Trigger", "gain": "Charge", "tts": "Timing FWHM", "chres": "ChargeRes"}}


class _Bus(QObject):
    polled = pyqtSignal(bool, str)   # changed, error text


class LiveScanPage(QWidget):
    def __init__(self):
        super().__init__()
        self.model = LiveScanModel(lambda: ("GS", None, None) if status.scan_running() else None)
        self._callout = None
        self._pick_map = {}
        self._view_key = None
        self._note_until = 0.0
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
        refresh.clicked.connect(lambda: self.poll(user=True))
        row1.addWidget(refresh)
        grid = QPushButton("Full Grid")
        grid.clicked.connect(self._full_grid)
        row1.addWidget(grid)
        export = QPushButton("Export...")
        export.clicked.connect(self.export_png)
        row1.addWidget(export)
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

        body = _GripSplitter(Qt.Horizontal)
        console = QGroupBox("Live Console")
        cl = QVBoxLayout(console)
        cl.addWidget(self._build_console())
        body.addWidget(console)
        self.card = _Card("Live Plot", rows=2)
        self.fig, self.canvas = self.card.fig, self.card.canvas
        self.fig.clear()   # test PMTs get two thirds of the height, the monitor the rest
        gs = self.fig.add_gridspec(3, 1)
        self.ax_test = self.fig.add_subplot(gs[0:2, 0])
        self.ax_mon = self.fig.add_subplot(gs[2, 0], sharex=self.ax_test)
        self.card.axes = [self.ax_test, self.ax_mon]
        self.card.zoom.on_clear = self._clear_callout
        self.canvas.mpl_connect("pick_event", self._on_pick)
        body.addWidget(self.card)
        body.setStretchFactor(0, 4)
        body.setStretchFactor(1, 6)
        body.setChildrenCollapsible(False)
        lay.addWidget(body, 1)
        self.redraw()

    # ---------------------------------------------------------------- polling
    def _full_grid(self):
        from pages.general_scan.scan_matrix import open_matrix
        open_matrix(self)

    def _build_console(self):
        from pages.general_scan.live_console import LiveConsole
        self.live_console = LiveConsole()
        return self.live_console

    def poll(self, user=False):
        if uproot is None:
            self.status.setText("uproot not installed")
            return
        if user:
            if not hasattr(self, "spin"):
                self.spin = Spinner(self)
            self.spin.start()
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
        if hasattr(self, "spin"):
            self.spin.stop()
        if err:
            self.status.setText(f"poll failed: {err}")
            return
        self._sync_radio("axis", self.model.axis_mode)
        self._sync_metric_names()
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

    def _sync_metric_names(self):
        """Metric buttons carry the names of the digitizer whose datasets are shown."""
        names = METRIC_NAMES["hk" if self.model.backend() == "hk" else "caen"]
        for b in self.groups["metric"].buttons():
            val = b.property("val")
            if b.text() != names[val]:
                b.setText(names[val])
            b.setEnabled(not (self.model.backend() == "hk" and val not in HK.METRICS))

    def _metric_label(self):
        m = self.model
        caen, hk = METRICS[m.metric][0], HK.METRICS.get(m.metric, "not measured")
        return {"caen": caen, "hk": hk}.get(m.backend(), f"{caen}  |  HK: {hk}")

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
        self._sync_metric_names()
        self._refresh_menu()
        self.redraw()
        self.poll(user=True)

    def _follow_newest(self):
        self.model.follow_newest()
        self.poll(user=True)

    def _update_status(self):
        if time.time() < self._note_until:
            return
        txt, live = self.model.status()
        if txt is None:
            self.status.setText("waiting for data...")
            self.status.setStyleSheet(f"color: {MUTED};")
            self.live_chip.hide()
            return
        self.status.setText(txt)
        self.status.setStyleSheet(f"color: {theme.C['good'] if live else TEXT};")
        self.live_chip.setVisible(live)

    def export_png(self):
        """The plot card as shown (legend, plot and run line) in one PNG."""
        os.makedirs(EXPORT_DIR, exist_ok=True)
        name = f"livescan_{self.model.metric}_{time.strftime('%Y%m%d_%H%M%S')}.png"
        path = QFileDialog.getSaveFileName(self, "Export plot", os.path.join(EXPORT_DIR, name), "PNG (*.png)")[0]
        if not path:
            return
        if not path.lower().endswith(".png"):
            path += ".png"
        ok = self.card.grab().save(path, "PNG")
        self.status.setText(f"Exported: {path}" if ok else f"Export failed: {path}")
        self.status.setStyleSheet(f"color: {TEXT};")
        self._note_until = time.time() + NOTE_S

    # ---------------------------------------------------------------- drawing
    def _clear_callout(self):
        if self._callout is not None:
            try:
                self._callout.remove()
            except Exception:
                pass
            self._callout = None

    def _foot(self):
        """Which runs are drawn; with several datasets, what the letters stand for."""
        m = self.model
        sel = [k for k in m.selected if k in m.sessions]
        if len(m.selected) > 1:
            return "     ".join(f"{chr(65 + i)} = {m.label(k)}" for i, k in enumerate(m.selected))
        if not sel:
            return ""
        s = m.sessions[sel[0]]
        return f"{s['date']}  ·  {s['kind']} {s['wl'] or '?'} nm  ·  runs {s['runs'][0]}-{s['runs'][-1]}"

    def redraw(self):
        m = self.model
        card = self.card
        view = (m.metric, m.axis_mode, m.axis_filter, m.mon_norm, tuple(m.selected))
        if view != self._view_key:   # another quantity or dataset: its own ranges again
            self._view_key = view
            card.forget_view()
        card.clear(self._foot())
        self._callout = None
        self._pick_map = {}
        label = self._metric_label()
        multi = len(m.selected) > 1
        entries = []
        for i, key in enumerate(list(m.selected)):
            color, ls = DS_COLORS[i % len(DS_COLORS)], DS_STYLES[i % len(DS_STYLES)]
            for ax, chans in ((self.ax_test, (1, 2)), (self.ax_mon, (0,))):
                for ch in chans:
                    xs, ys, es, recs = m.series(key, ch)
                    fx, fy, _fe, frecs = m.series(key, ch, failed=True)
                    if not xs and not fx:
                        continue
                    if multi:
                        c, mk, lab = color, CH_MARKER[ch], f"{chr(65 + i)} {CH_NAME[ch]}"
                    else:
                        mk, c = CH_STYLE.get(ch, MON_STYLE)
                        sn = (recs or frecs)[0]["sn"].get(ch, f"ch{ch}")
                        lab = f"{sn} (monitor)" if ch == 0 else sn
                    fills = [c if p["axis"].get(ch) != "Y" or m.axis_mode != "time" else "none" for p in recs]
                    cont = ax.errorbar(xs, ys, yerr=es, fmt=mk, ms=4.5, capsize=2, color=c, linestyle="none",
                                       picker=6)
                    line, extra = cont.lines[0], []
                    if multi and m.axis_mode != "time":
                        for axis_name in ("X", "Y"):
                            pts = sorted((x, y) for x, y, p in zip(xs, ys, recs) if p["axis"].get(ch) == axis_name)
                            if len(pts) > 1:
                                extra += ax.plot([q[0] for q in pts], [q[1] for q in pts], color=c, linestyle=ls,
                                                 lw=1)
                    if m.axis_mode == "time" and "none" in fills:
                        line.set_markerfacecolor("none")
                        solid = [k for k, f in enumerate(fills) if f != "none"]
                        if solid:
                            extra += ax.plot([xs[k] for k in solid], [ys[k] for k in solid], mk, ms=4.5, color=c,
                                             linestyle="none")
                    if fx:
                        bad = ax.plot(fx, fy, mk, ms=4.5, mfc="none", mew=1, color=c, linestyle="none",
                                      alpha=FAIL_ALPHA, picker=6)[0]
                        self._pick_map[bad] = (key, ch, frecs)
                        extra.append(bad)
                    self._pick_map[line] = (key, ch, recs)
                    # one legend entry hides the points together with their error bars and lines
                    entries.append((lab, ax, line, [line, *cont[1], *cont[2], *extra]))
        xlabel = {"raw": "Raw Stage angle [degree]", "hamamatsu": "Hamamatsu incidence angle [degree]",
                  "time": "Elapsed time since dataset start [h]"}[m.axis_mode]
        self.ax_test.set_ylabel(f"{label} / Monitor" if m.mon_norm else label, fontsize=9)
        self.ax_mon.set_ylabel(label.split(" (")[0].split(" [")[0] + " (mon.)", fontsize=9)
        self.ax_mon.set_xlabel(xlabel, fontsize=9)
        self.ax_test.tick_params(labelbottom=False)
        if entries:
            for lab, ax, line, arts in entries:
                for a in arts:
                    a.set_visible(lab not in card.hidden)
                card.entries[lab] = (ax, line, arts)
            card.strip.set_items([(lab, line, arts) for lab, _ax, line, arts in entries])
        else:
            hint = ("No points read yet.\nHK points need their hit and event trees (HK Live Monitor on the HK PC,\n"
                    "or the Hit-tree / Event-tree post steps)." if m.backend() == "hk"
                    else "No points yet.\nPoints appear as each one finishes analysis.")
            self.ax_test.text(0.5, 0.5, hint,
                              ha="center", va="center", transform=self.ax_test.transAxes, color="#888", fontsize=10)
        card.done()

    def _on_pick(self, event):
        """Click a point: all four metrics, plus the same point in every other overlaid dataset."""
        entry = self._pick_map.get(event.artist)
        me = event.mouseevent
        if (not entry or not len(event.ind) or me.button != 1 or not event.artist.get_visible()
                or self.card._edge(me) is not None):
            return
        self.card.zoom.picked = True   # the release of this click must not close the callout
        m = self.model
        key, ch, recs = entry
        idx = event.ind[0]
        p = recs[idx]
        tag = chr(65 + m.selected.index(key)) + " · " if len(m.selected) > 1 and key in m.selected else ""
        lines = [f"{tag}run {p['run']:03d}  ·  {p['sn'].get(ch, f'ch{ch}')}",
                 f"{p['axis'].get(ch, '?')} {p['raw'][ch]:+.0f}°  ham {p['ham'][ch]:+.1f}°",
                 time.strftime("%m-%d %H:%M", time.localtime(p["time"]))]
        names = ({k: (v,) for k, v in HK.METRICS.items() if k != "fwhm"} if p.get("hk") else METRICS)
        for mk, (label, *_r) in names.items():
            v = p["data"].get(mk, {}).get(ch)
            fb = p.get("fit", {}).get(mk, {}).get(ch) if v and v[0] <= 0 and mk != "qe" else None
            if fb:
                short = label.split(" (")[0].split(" [")[0]
                lines.append(f"{short:<6} {fb[0]:.3g}  fit failed")
            elif v:
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
        self._clear_callout()
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
        if p.get("hk"):
            self._show_hk_point(p)

    def _show_hk_point(self, rec):
        if getattr(self, "hk_window", None) is None:
            from widgets.hk_point import HKPointWindow
            self.hk_window = HKPointWindow(self.window())
        self.hk_window.show_point(rec)

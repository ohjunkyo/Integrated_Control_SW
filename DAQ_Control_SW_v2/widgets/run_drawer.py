"""Run drawer: slides in from the right edge of the window and shows the charge and time distributions of one run."""
import math
import threading

import numpy as np
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
from matplotlib.colors import LogNorm
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from PyQt5.QtCore import QEasingCurve, QEvent, QObject, QPoint, QPropertyAnimation, Qt, pyqtSignal
from PyQt5.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
                             QLineEdit, QPushButton, QRadioButton, QShortcut, QSizePolicy, QVBoxLayout)
from PyQt5.QtGui import QDoubleValidator, QKeySequence

from app import theme
from core import run_contour as RC
from core import run_detail as RD
from widgets.plot_zoom import PlotZoom

WIDTH_FRAC, MIN_W, MAX_W = 0.34, 420, 720
OPEN_MS, CLOSE_MS = 240, 180
HANDLE_W = 22
FIT_COLOR = "#d62728"
COMP_COLORS = {0: "#E08A00", 1: "#222222", 2: "#7B3FBF", 3: "#C2185B"}   # pedestal, 1 / 2 / 3 p.e.
COMP_STYLES = {"sum": "-", "gaus": "--", "back": ":"}
TIME_PAD_NS = (30.0, 50.0)     # shown before and after the fitted time range
CONTOUR_ASPECT = 0.55         # height / width of the contour frame
WHEEL_STEP = 0.8               # range factor of one wheel notch
ROWS = ("Charge", "Time")
HINT = "Drag: zoom in    Drag legend: move it    Wheel: zoom X in / out    Ctrl + wheel: zoom Y    Right-click: reset    Esc: close"


class _Bus(QObject):
    loaded = pyqtSignal(int, object)
    contour = pyqtSignal(int, object)


class RunDrawer(QFrame):
    def __init__(self, host, colors):
        """host: the page the drawer covers (top to bottom); colors: {ch: plot colour}."""
        super().__init__(host.window())
        self.host, self.colors = host, colors
        self.setObjectName("runDrawer")
        self.setStyleSheet(f"#runDrawer {{ background: {theme.C['card']}; border-left: 1px solid {theme.C['border']}; }}")
        root = QHBoxLayout(self)
        root.setContentsMargins(1, 0, 0, 0)
        root.setSpacing(0)
        self.close_btn = QPushButton("\u203a")   # the handle on the left edge pushes the drawer back out
        self.close_btn.setObjectName("runDrawerHandle")
        self.close_btn.setFixedWidth(HANDLE_W)
        self.close_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.close_btn.setCursor(Qt.PointingHandCursor)
        self.close_btn.setToolTip("Hide (Esc)")
        self.close_btn.setStyleSheet(
            f"#runDrawerHandle {{ border: none; border-right: 1px solid {theme.C['border']}; border-radius: 0;"
            f" background: transparent; color: {theme.C['muted']}; font-size: 20pt; padding: 0; }}"
            f"#runDrawerHandle:hover {{ background: {theme.C['btn_hover']}; }}")
        self.close_btn.clicked.connect(self.close_drawer)
        root.addWidget(self.close_btn)
        v = QVBoxLayout()
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(6)
        root.addLayout(v, 1)
        head = QHBoxLayout()
        self.title = QLabel("")
        f = self.title.font()
        f.setBold(True)
        f.setPointSizeF(theme.PT["section"])
        self.title.setFont(f)
        head.addWidget(self.title)
        head.addStretch(1)
        v.addLayout(head)
        self.sub = QLabel("")
        self.sub.setWordWrap(True)
        self.sub.setStyleSheet(f"color: {theme.C['muted']};")
        v.addWidget(self.sub)
        row = QHBoxLayout()
        row.addWidget(QLabel("PMT:"))
        self.group, self.radios = QButtonGroup(self), {}
        for ch, name in RD.ROLE.items():
            rb = QRadioButton(name)
            self.group.addButton(rb, ch)
            self.radios[ch] = rb
            row.addWidget(rb)
        self.group.buttonClicked[int].connect(self._pick_ch)
        row.addStretch(1)
        self.contour = QCheckBox("Contour")
        self.contour.toggled.connect(self._toggle_contour)
        row.addWidget(self.contour)
        self.logy = QCheckBox("Log Y (charge)")
        self.logy.setChecked(True)
        self.logy.toggled.connect(lambda _on: self._draw())
        row.addWidget(self.logy)
        self.comps = QCheckBox("Components")
        self.comps.setChecked(True)
        self.comps.setToolTip("Pedestal, Gaussian and backscatter parts of the charge fit")
        self.comps.toggled.connect(lambda _on: self._draw())
        row.addWidget(self.comps)
        v.addLayout(row)
        grid = QGridLayout()
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(3)
        self.edits, self.row_labels = {}, []
        for r, name in enumerate(ROWS):
            self.row_labels.append(QLabel(name))
            grid.addWidget(self.row_labels[r], r, 0)
            for k, axis in enumerate("XY"):
                grid.addWidget(QLabel(axis), r, 1 + 3 * k, Qt.AlignRight)
                for j in range(2):
                    e = QLineEdit()
                    e.setValidator(QDoubleValidator(e))
                    e.setMinimumWidth(84)
                    e.setToolTip(f"{name} {axis} {'max' if j else 'min'}")
                    e.editingFinished.connect(lambda r=r: self._apply_range(r))
                    self.edits[(r, axis, j)] = e
                    grid.addWidget(e, r, 2 + 3 * k + j)
        self.auto_btn, self.home_btn = QPushButton("Auto Scale"), QPushButton("Reset View")
        grid.addWidget(self.auto_btn, 0, 8)
        grid.addWidget(self.home_btn, 1, 8)
        grid.setColumnStretch(7, 1)
        v.addLayout(grid)
        self.fig = Figure(facecolor="white")
        self.canvas = FigureCanvasQTAgg(self.fig)
        self.ax_q, self.ax_t = self.fig.add_subplot(2, 1, 1), self.fig.add_subplot(2, 1, 2)
        self.ax_c = self.fig.add_subplot(1, 1, 1)
        self.ax_c.set_visible(False)
        v.addWidget(self.canvas, 1)
        self.zoom = PlotZoom(self.canvas, on_zoom=self._sync_range, on_home=self._sync_range)
        self.home_btn.clicked.connect(self._reset_view)
        self.auto_btn.clicked.connect(self._auto_scale)
        self.canvas.mpl_connect("scroll_event", self._wheel)
        self.state = QLabel("")
        self.state.setWordWrap(True)
        self.state.setStyleSheet(f"color: {theme.C['muted']};")
        v.addWidget(self.state)
        self.anim = QPropertyAnimation(self, b"pos", self)
        self.anim.finished.connect(self._anim_done)
        self._open, self._moving, self._seq, self._data, self._ch, self._info = False, False, 0, None, 1, ""
        self._leg_loc, self._contour = {}, None
        self.bus = _Bus()
        self.bus.loaded.connect(self._loaded)
        self.bus.contour.connect(self._contour_loaded)
        self.esc = QShortcut(QKeySequence(Qt.Key_Escape), self.window())
        self.esc.activated.connect(self.close_drawer)
        self.esc.setEnabled(False)
        self.window().installEventFilter(self)
        host.installEventFilter(self)
        self.hide()

    # ---------------------------------------------------------------- slide
    def _rect(self):
        """(x open, x closed, y, w, h): flush with the right window edge, as tall as the host page."""
        win = self.window()
        w = int(min(max(win.width() * WIDTH_FRAC, MIN_W), MAX_W, win.width()))
        vis = self.host.visibleRegion().boundingRect()   # a scrolled page is taller than what is shown
        y = self.host.mapTo(win, vis.topLeft()).y()
        return win.width() - w, win.width(), y, w, min(vis.height(), win.height() - y)

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Resize and self._open and not self._moving:
            x, _xc, y, w, h = self._rect()
            self.setGeometry(x, y, w, h)
        return False

    def _slide(self, x0, x1, y, ms, curve):
        self._moving = True
        self.anim.stop()
        self.anim.setDuration(ms)
        self.anim.setEasingCurve(curve)
        self.anim.setStartValue(QPoint(x0, y))
        self.anim.setEndValue(QPoint(x1, y))
        self.anim.start()

    def _anim_done(self):
        self._moving = False
        if not self._open:
            self.hide()
        else:
            self._draw()   # plots are drawn once the drawer stands still

    def show_run(self, date, run, ch):
        """Slide in (when closed) and show this run; an open drawer only swaps its content."""
        self._ch = ch
        self.title.setText(f"Run {date}_{int(run):03d}")
        self.sub.setText("")
        self.state.setText("Loading...")
        self._data = self._contour = None
        if not self._open:
            self._open = True
            for ax in (self.ax_q, self.ax_t, self.ax_c):   # an empty sheet slides in; the plots follow
                ax.set_visible(False)
            self.canvas.draw_idle()
            x, xc, y, w, h = self._rect()
            self.setGeometry(xc, y, w, h)
            self.show()
            self.raise_()
            self.esc.setEnabled(True)
            self._slide(xc, x, y, OPEN_MS, QEasingCurve.OutCubic)
        self._seq += 1
        seq = self._seq

        def work():
            try:
                res = RD.load(date, run)
            except Exception as e:
                res = e
            self.bus.loaded.emit(seq, res)
        threading.Thread(target=work, daemon=True, name="run-detail").start()

    def close_drawer(self, animate=True):
        if not self._open:
            return
        self._open = False
        self._seq += 1
        self.esc.setEnabled(False)
        if animate and self.isVisible():
            x, xc, y, _w, _h = self._rect()
            self._slide(self.x(), xc, y, CLOSE_MS, QEasingCurve.InCubic)
        else:
            self.anim.stop()
            self._moving = False
            self.hide()

    # ---------------------------------------------------------------- content
    def _loaded(self, seq, res):
        if seq != self._seq or not self._open:
            return
        if isinstance(res, Exception):
            self._data = None
            for ax in (self.ax_q, self.ax_t, self.ax_c):
                ax.set_visible(False)
            self.canvas.draw_idle()
            self.state.setText(str(res))
            return
        self._data = res
        chans = res["chans"]
        if self._ch not in chans and chans:
            self._ch = sorted(chans)[0]
        for ch, rb in self.radios.items():
            rb.setEnabled(ch in chans)
            rb.setChecked(ch == self._ch)
        i = res["info"]
        parts = [str(i.get("RunMode") or "")]
        if str(i.get("RunMode", "")).lower() == "laser":
            parts.append(f"{i.get('Wavelength', '?')} nm  {i.get('Laser_mA', '?')} mA")
        self._info = "   ".join(p for p in parts if p)
        self.state.setText(HINT if chans else "No histograms in this file")
        if self.contour.isChecked():
            self._request_contour()
        if not self._moving:
            self._draw()

    def _toggle_contour(self, on):
        self.row_labels[0].setText("Contour" if on else ROWS[0])
        for (r, _axis, _j), e in self.edits.items():
            e.setEnabled(not on or r == 0)
        self.logy.setEnabled(not on)
        self.comps.setEnabled(not on)
        self.auto_btn.setEnabled(not on)
        if on and self._data is not None and self._contour is None:
            self._request_contour()
        self._draw()

    def _request_contour(self):
        seq, date, run = self._seq, self._data["date"], self._data["run"]
        self.state.setText("Building contour from the RAW file...")

        def work():
            try:
                res = RC.load(date, run)
            except Exception as e:
                res = e
            self.bus.contour.emit(seq, res)
        threading.Thread(target=work, daemon=True, name="run-contour").start()

    def _contour_loaded(self, seq, res):
        if seq != self._seq or not self._open:
            return
        self._contour = res
        self.state.setText(str(res) if isinstance(res, Exception) else HINT)
        if not self._moving:
            self._draw()

    def _pick_ch(self, ch):
        self._ch = ch
        self._draw()

    # ---------------------------------------------------------------- view
    def _axes(self):
        return (self.ax_c,) if self.contour.isChecked() else (self.ax_q, self.ax_t)

    def _sync_range(self):
        """Range boxes follow the plots."""
        for r, ax in enumerate(self._axes()):
            for axis, lim in (("X", ax.get_xlim()), ("Y", ax.get_ylim())):
                for j in range(2):
                    e = self.edits[(r, axis, j)]
                    e.setText(f"{lim[j]:.5g}")
                    e.setCursorPosition(0)

    def _apply_range(self, r):
        if r >= len(self._axes()):
            return
        ax = self._axes()[r]
        if self._data is None or not ax.get_visible():
            return
        try:
            x0, x1, y0, y1 = (float(self.edits[(r, a, j)].text()) for a in "XY" for j in range(2))
        except ValueError:
            return self._sync_range()
        shown = lambda lim: [float(f"{v:.5g}") for v in lim]   # untouched boxes keep the exact range
        if x0 < x1 and [x0, x1] != shown(ax.get_xlim()):
            ax.set_xlim(x0, x1)
        if y0 < y1 and (y0 > 0 or ax.get_yscale() != "log") and [y0, y1] != shown(ax.get_ylim()):
            ax.set_ylim(y0, y1)
        self._sync_range()   # a rejected entry goes back to the shown range
        self.canvas.draw_idle()

    def _reset_view(self):
        self.zoom.reset()
        self._sync_range()

    def _hists(self):
        c = self._data["chans"][self._ch]
        return ((self.ax_q, c["charge"]), (self.ax_t, c["time"]))

    def _auto_scale(self):
        """Y fitted to the bins inside the current X range (the X zoom is kept)."""
        if self._data is None or self._ch not in self._data["chans"] or self.contour.isChecked():
            return
        for ax, h in self._hists():
            lo, hi = ax.get_xlim()
            e = h["edges"]
            vals = h["values"][(e[1:] > lo) & (e[:-1] < hi)]
            top = float(vals.max()) if len(vals) else 0.0
            if top <= 0:
                continue
            if ax.get_yscale() == "log":
                ax.set_ylim(0.5, top * 2)
            else:
                ax.set_ylim(0, top * 1.1)
        self._sync_range()
        self.canvas.draw_idle()

    def _wheel(self, ev):
        ax = ev.inaxes
        if ax not in self._axes() or ev.xdata is None or self._data is None:
            return
        f = WHEEL_STEP if ev.button == "up" else 1 / WHEEL_STEP
        if QApplication.keyboardModifiers() & Qt.ControlModifier:
            lo, hi = ax.get_ylim()
            if ax.get_yscale() == "log":   # zoom in decades around the cursor
                c, lo, hi = math.log10(ev.ydata), math.log10(lo), math.log10(hi)
                ax.set_ylim(10 ** (c - (c - lo) * f), 10 ** (c + (hi - c) * f))
            else:
                ax.set_ylim(ev.ydata - (ev.ydata - lo) * f, ev.ydata + (hi - ev.ydata) * f)
        else:
            lo, hi = ax.get_xlim()
            ax.set_xlim(ev.xdata - (ev.xdata - lo) * f, ev.xdata + (hi - ev.xdata) * f)
        self._sync_range()
        self.canvas.draw_idle()

    def _clear(self):
        for ax, xl in ((self.ax_q, "Charge (pC)"), (self.ax_t, "Time (ns)")):
            leg = ax.get_legend()
            if leg is not None and isinstance(leg._loc, tuple):   # dragged: keep the place
                self._leg_loc[ax] = leg._loc
            ax.clear()
            ax.set_visible(True)
            ax.grid(True, alpha=0.3)
            ax.tick_params(labelsize=8)
            ax.set_xlabel(xl, fontsize=8)
            ax.set_ylabel("Entries", fontsize=8)

    def _legend(self, ax, title, notes):
        """Values sit in a legend inside the axes; it can be dragged off the data and stays where it was put."""
        handles, labels = ax.get_legend_handles_labels()
        for n in notes:
            if n:
                handles.append(Line2D([], [], ls="none"))
                labels.append(n)
        leg = ax.legend(handles, labels, title=title, loc=self._leg_loc.get(ax, "upper right"), fontsize=8,
                        title_fontsize=8, framealpha=0.85, handlelength=1.6)
        leg.set_draggable(True)
        leg._legend_box.align = "left"
        leg.get_title().set_fontweight("bold")

    @staticmethod
    def _pm(stats, key, err, unit, scale=1.0, fmt="{:.3g}"):
        v = stats.get(key)
        if v is None or v <= 0:
            return ""
        e = stats.get(err)
        return fmt.format(v * scale) + (f" +- {fmt.format(e * scale)}" if e and e > 0 else "") + f" {unit}"

    def _draw(self):
        d = self._data
        if d is None or self._ch not in d["chans"]:
            return
        c, info = d["chans"][self._ch], d["info"]
        n = self._ch + 1
        self.sub.setText("   ".join(p for p in (self._info, f"{RD.ROLE[self._ch]} {info.get(f'SN{n}', '')}".strip(),
                                                f"HV {info[f'HV{n}']} V" if f"HV{n}" in info else "") if p))
        if self.contour.isChecked():
            return self._draw_contour()
        color, st = self.colors.get(self._ch, "#378ADD"), c["stats"]
        self.ax_c.set_visible(False)
        self._clear()
        for ax, h in ((self.ax_q, c["charge"]), (self.ax_t, c["time"])):
            ax.stairs(h["values"], h["edges"], color=color, lw=1, label=f"Data  {int(h['values'].sum())} entries")
            if h["fit"]:
                chi = f"  chi2/ndf {h['chi2']:.0f}/{h['ndf']:.0f}" if h.get("ndf") else ""
                ax.plot(h["fit"][0], h["fit"][1], color=FIT_COLOR, lw=1.2, label="Fit" + chi)
        q, t = c["charge"], c["time"]
        if self.comps.isChecked() and q["fit"]:
            for label, n, kind, y in RD.spe_components(q.get("params"), q["fit"][0]):
                self.ax_q.plot(q["fit"][0], y, color=COMP_COLORS.get(n, "#888888"), lw=0.9, ls=COMP_STYLES[kind], label=label)
        nz = q["values"].nonzero()[0]
        if len(nz):
            self.ax_q.set_xlim(q["edges"][nz[0]], q["edges"][nz[-1] + 1])
        if self.logy.isChecked() and len(nz):
            self.ax_q.set_yscale("log")
            self.ax_q.set_ylim(0.5, q["values"].max() * 2)
        spe = st.get("spe_mean")
        if spe and spe > 0:
            self.ax_q.axvline(spe, color="#555555", lw=0.8, ls=":",
                              label="SPE " + self._pm(st, "spe_mean", "spe_mean_error", "pC", fmt="{:.3f}"))
        qe = self._pm(st, "relativeQE_raw", "relativeQE_raw_err", "%", fmt="{:.2f}")
        self._legend(self.ax_q, "Charge", ["QE " + qe if qe else "",
                                           f"P/V {st['peak_to_valley']:.2f}" if st.get("peak_to_valley", 0) > 0 else ""])
        if t["fit"]:
            self.ax_t.set_xlim(t["fit"][0][0] - TIME_PAD_NS[0], t["fit"][0][-1] + TIME_PAD_NS[1])
        fwhm = self._pm(st, "fwhm_exG", "fwhm_exG_err", "ns", RD.NS_PER_SAMPLE, "{:.2f}")
        self._legend(self.ax_t, "Time", [f"TTS FWHM {fwhm}" if fwhm and t["fit"] else
                                         "no time fit" if t["values"].any() else "no data"])
        self.fig.tight_layout(pad=0.9)
        self.zoom.set_home()
        self._sync_range()
        self.canvas.draw_idle()

    def _draw_contour(self):
        """Counts per sample and voltage of every waveform of the run, as Draw_Contour_v3.C shows them."""
        for ax in (self.ax_q, self.ax_t):
            ax.set_visible(False)
        ax, h = self.ax_c, self._contour.get(self._ch) if isinstance(self._contour, dict) else None
        ax.clear()
        ax.set_visible(h is not None)
        if h is None:
            if isinstance(self._contour, dict):
                self.state.setText("No waveforms of this channel in the RAW file")
            return self.canvas.draw_idle()
        counts = np.ma.masked_equal(h["counts"], 0)
        im = ax.imshow(counts, origin="lower", aspect="auto", extent=h["extent"], cmap="afmhot", interpolation="nearest",
                       norm=LogNorm(1, max(float(counts.max()), 2.0)))
        ax.set_ylim(h["ped_mV"] - RC.VIEW_MV[0], h["ped_mV"] + RC.VIEW_MV[1])
        ax.set_box_aspect(CONTOUR_ASPECT)
        ax.set_anchor("N")
        ax.grid(True, alpha=0.3)
        ax.tick_params(labelsize=8)
        ax.set_xlabel(f"Sample ({RD.NS_PER_SAMPLE:g} ns / sample)", fontsize=8)
        ax.set_ylabel("Voltage (mV)", fontsize=8)
        cax = ax.inset_axes([1.02, 0.0, 0.035, 1.0])
        self.fig.colorbar(im, cax=cax)
        cax.tick_params(labelsize=8)
        self.fig.tight_layout(pad=0.9)
        self.zoom.set_home()
        self._sync_range()
        self.canvas.draw_idle()

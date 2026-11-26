"""Output tab: live waveforms of the running acquisition, one panel per channel, from the DAQ's
/dev/shm snapshot: density of all events of the last 0.5 s with the last event on top, relative to the
channel baseline from the mean of up to 200 events."""
import time

import numpy as np
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
from matplotlib.colors import LogNorm
from matplotlib.figure import Figure
from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QCheckBox, QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout

from app import theme
from core import daq_live
from core import waveform as W

CHANNELS = ((0, "CH0 Monitor"), (1, "CH1 Rot1"), (2, "CH2 Rot2"), (3, "CH3 Trigger"))
BASE_SAMPLES = 50          # baseline = mean of the first samples of the mean waveform
PMT_YLIM_MV = (-3.0, 3.0)          # fixed, baseline in the middle
TRIGGER_YLIM_MV = (-1000.0, 1000.0)
STALE_S = 5.0
LAST_C, PED_SPAN_C, SIG_SPAN_C = "black", "#e0e7ff", "#fee2e2"
REFRESH_MS = 500
MAP_CMAP = "viridis"       # dark at one count, so a single pulse still shows on the white panel
MAP_ALPHA = 0.6


class LiveWaveform(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("liveWave")
        c = theme.C
        self.setStyleSheet(f"QFrame#liveWave {{ background: {c['card']}; border: 1px solid {c['border']};"
                           " border-radius: 8px; }")
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(4)
        self.head = QLabel("--")
        self.head.setStyleSheet("font-size: 11pt; font-weight: 700;")
        self.head.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)   # a long run name never widens the page
        self.head.setMinimumWidth(0)
        self.density = QCheckBox("Overlay all events")
        self.density.setToolTip("Density of all events of the last 0.5 s behind the last event")
        self.density.setChecked(True)
        self.density.toggled.connect(self._redraw)
        top = QHBoxLayout()
        top.addWidget(self.head, 1)
        top.addWidget(self.density)
        v.addLayout(top)
        self.fig = Figure(facecolor="white")
        self.canvas = FigureCanvasQTAgg(self.fig)
        self.canvas.setMinimumSize(300, 240)
        v.addWidget(self.canvas, 1)
        self.axes, self.lines, self.info, self.off, self.maps = {}, {}, {}, {}, {}
        for i, (ch, title) in enumerate(CHANNELS):
            ax = self.fig.add_subplot(2, 2, i + 1)
            ax.set_title(title, fontsize=9, loc="left")
            ax.tick_params(labelsize=7)
            ax.grid(True, alpha=0.3)
            last, = ax.plot([], [], color=LAST_C, lw=0.9, zorder=3)
            self.maps[ch] = ax.imshow(np.zeros((2, 2)), origin="lower", aspect="auto", cmap=MAP_CMAP,
                                      interpolation="nearest", norm=LogNorm(1, 2), zorder=2, alpha=MAP_ALPHA,
                                      visible=False)
            self.info[ch] = ax.text(0.99, 0.97, "", transform=ax.transAxes, ha="right", va="top", fontsize=7,
                                    bbox=dict(facecolor="white", edgecolor="none", alpha=0.8, pad=1.5), zorder=5)
            self.axes[ch], self.lines[ch] = ax, last
            self.off[ch] = ax.text(0.5, 0.5, "Off", transform=ax.transAxes, ha="center", va="center", fontsize=13,
                                   color="#8a909c", visible=False)
        for ax in (self.axes[2], self.axes[3]):
            ax.set_xlabel("ns", fontsize=8)
        for ax in (self.axes[0], self.axes[2]):
            ax.set_ylabel("mV", fontsize=8)
        self.fig.subplots_adjust(left=0.08, right=0.98, top=0.93, bottom=0.09, hspace=0.32, wspace=0.18)
        self._mtime = None
        self._prev = None          # (events, elapsed) of the previous snapshot, for the rate
        self._rate = None
        self._spans = []
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(REFRESH_MS)

    def _redraw(self):
        self._mtime = None
        self.refresh()

    def refresh(self):
        if not self.isVisible():
            return
        s = daq_live.read()
        if s is None:
            return
        age = time.time() - s["time"]
        if s["state"] == "running" and age > STALE_S:
            state, color = "No update", theme.C["warn"]
        elif s["state"] == "running":
            state, color = "Running", theme.C["good"]
        else:
            state, color = "Finished", theme.C["muted"]
        key = (s["time"], s["events"], state)
        if key == self._mtime:
            return
        self._mtime = key
        if self._prev and s["events"] > self._prev[0] and s["elapsed_s"] > self._prev[1]:
            self._rate = (s["events"] - self._prev[0]) / (s["elapsed_s"] - self._prev[1])
        elif s["elapsed_s"] > 0 and (not self._prev or s["events"] < self._prev[0]):
            self._rate = s["events"] / s["elapsed_s"]
        self._prev = (s["events"], s["elapsed_s"])
        run = s["output"].rsplit("/", 1)[-1] or "-"
        rate = f"{self._rate / 1000:.2f} kHz" if self._rate else "--"
        gap = "&nbsp;" * 4
        parts = [run, s["mode"], f'{s["events"]:,} events', rate,
                 f'<span style="color: {color};">{state}</span>', time.strftime("%H:%M:%S", time.localtime(s["time"]))]
        self.head.setText(gap.join(parts))
        self._draw(s)

    def _draw(self, s):
        rl = s["record_length"]
        x = np.arange(rl) * W.TIME_PER_SAMPLE
        for sp in self._spans:
            sp.remove()
        self._spans = []
        windows = None
        if s["mode"].lower() == "laser":   # the windows the analysis code will use for this run
            ped_s, ped_e, sig_s, sig_e = W.laser_windows(s["output"], rl, s["post_trigger"])
            windows = ((ped_s, ped_e), (sig_s, sig_e))
        for ch, _title in CHANNELS:
            ax, last_l = self.axes[ch], self.lines[ch]
            on = ch in s["mean"]
            ax.set_facecolor("white" if on else "#eceef1")
            ax.grid(True, alpha=0.3) if on else ax.grid(False)
            ax.tick_params(left=on, bottom=on, labelleft=on, labelbottom=on)
            ax.xaxis.label.set_visible(on)
            ax.yaxis.label.set_visible(on)
            self.off[ch].set_visible(not on)
            dm = s.get("map", {}).get(ch) if on and ch != W.TRIGGER_CH and self.density.isChecked() else None
            self.maps[ch].set_visible(dm is not None)
            if not on:
                last_l.set_data([], [])
                self.info[ch].set_text("")
                continue
            mean = s["mean"][ch].astype(float)
            last = s["last"][ch].astype(float)
            base = float(mean[:BASE_SAMPLES].mean())
            rms = float(last[:BASE_SAMPLES].std()) * W.ADC_TO_MV
            yl = (last - base) * W.ADC_TO_MV
            last_l.set_data(x, yl)
            last_l.set_linewidth(0.6 if dm else 0.9)
            if dm:
                counts, im = dm["counts"], self.maps[ch]
                rows = np.flatnonzero(counts.any(axis=1))
                r0, r1 = (int(rows[0]), int(rows[-1]) + 1) if len(rows) else (0, 1)
                counts = counts[r0:r1]                     # only the filled rows are drawn
                y0 = (dm["lo"] + r0 - 0.5 - base) * W.ADC_TO_MV
                y1 = y0 + (r1 - r0) * W.ADC_TO_MV
                im.set_data(np.ma.masked_equal(counts, 0))
                im.set_extent((-0.5 * W.TIME_PER_SAMPLE, (rl - 0.5) * W.TIME_PER_SAMPLE, y0, y1))
                im.set_norm(LogNorm(1, max(float(counts.max()), 2.0)))
            ax.set_xlim(0, x[-1])
            ax.set_ylim(*(TRIGGER_YLIM_MV if ch == W.TRIGGER_CH else PMT_YLIM_MV))
            if windows and ch != W.TRIGGER_CH:
                for (a, b), col in zip(windows, (PED_SPAN_C, SIG_SPAN_C)):
                    if 0 <= a < b <= rl:
                        self._spans.append(ax.axvspan(a * W.TIME_PER_SAMPLE, b * W.TIME_PER_SAMPLE,
                                                      color=col, alpha=0.6, lw=0, zorder=0))
            info = f"base {base:.1f} ADC  rms {rms:.2f} mV"
            if ch == W.TRIGGER_CH:
                info += f"  min {float(yl.min()):.1f} mV"
            self.info[ch].set_text(info)
        self.canvas.draw_idle()

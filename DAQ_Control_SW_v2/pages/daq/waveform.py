"""DAQ System > Waveform: same controls and plots as the Tk Waveform Inspection
panel (managers/waveform_viewer.py), built on core/waveform.py.

Slow work (open, averaging, searches, noise scan) runs in worker threads and
reports back through Qt signals.
"""
import os
import threading

import numpy as np
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle
from PyQt5 import sip
from PyQt5.QtCore import QObject, Qt, pyqtSignal
from PyQt5.QtWidgets import (QButtonGroup, QComboBox, QDialog, QFileDialog, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
                             QLabel, QLineEdit, QListWidget, QMessageBox, QPushButton, QRadioButton, QVBoxLayout, QWidget)

from app import theme
from core import waveform as W

BG, WAVE, PED_C = "white", "#0b63b6", "#6366f1"
GRID_C, SPINE_C, TICK_C, LABEL_C = "#e5e7eb", "#d1d5db", "#6b7280", "#374151"
PED_SPAN_C, SIG_SPAN_C = "#e0e7ff", "#fee2e2"
MAX_SLOTS = 8


class _Bus(QObject):
    call = pyqtSignal(object)


def _btn(text, bg=None, fg=None):
    b = QPushButton(text)
    bg, fg = bg or theme.C["hover"], fg or theme.C["text"]
    _paint(b, bg, fg)
    return b


def _paint(b, bg, fg="white"):
    b.setStyleSheet(f"QPushButton {{ background: {bg}; color: {fg}; border: none; border-radius: 4px;"
                    " padding: 4px 12px; min-height: 20px; font-weight: 600; } QPushButton:disabled { color: " + theme.C["disabled"] + "; }")


def _edit(text, width=60):
    e = QLineEdit(text)
    e.setFixedWidth(width)
    e.setAlignment(Qt.AlignCenter)
    return e


def _vsep():
    s = QFrame()
    s.setFrameShape(QFrame.VLine)
    s.setStyleSheet(f"color: {theme.C['border']};")
    return s


INSTANCE = None   # the live page, so the left panel's Waveform inspection button can load a file


def live():
    """The page, or None if not built yet or deleted by a window rebuild."""
    return INSTANCE if INSTANCE is not None and not sip.isdeleted(INSTANCE) else None


class WaveformPage(QWidget):
    def __init__(self):
        super().__init__()
        global INSTANCE
        INSTANCE = self
        self.setFocusPolicy(Qt.StrongFocus)
        self.run = None
        self.cur = 0
        self.fft = False
        self.avg_spec = self.avg_wave = None
        self.avg_busy = False
        self._job = None          # token of the running search / scan
        self._noise_dlg = None
        self._drag = self._rect = None
        self.bus = _Bus()
        self.bus.call.connect(lambda fn: fn())
        self._build()
        self._placeholder()

    def later(self, fn):
        """Run fn on the GUI thread (callable from worker threads)."""
        self.bus.call.emit(fn)

    # ------------------------------------------------------------------ UI
    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        cards = QHBoxLayout()
        cards.setSpacing(12)

        ev = QGroupBox("Event")
        el = QVBoxLayout(ev)
        fb = QHBoxLayout()
        for text, fn, bg in (("Use selected file", self.load_selection, "#17a2b8"),
                             ("Open...", self.open_dialog, "#17a2b8"), ("Clear", self.clear, "#6b7280")):
            b = _btn(text, bg, "white")
            b.clicked.connect(fn)
            fb.addWidget(b)
        self.fft_btn = _btn("FFT")
        self.fft_btn.clicked.connect(self.toggle_fft)
        fb.addWidget(self.fft_btn)
        g = _btn("Guide")
        g.clicked.connect(self.guide)
        fb.addWidget(g)
        fb.addSpacing(8)
        self.file_lbl = QLabel("-")
        self.file_lbl.setStyleSheet(f"color: {theme.C['accent']}; font-weight: 600;")
        fb.addWidget(self.file_lbl, 1)
        el.addLayout(fb)
        fb = QHBoxLayout()
        for text, tip, fn, rep in (("|\u25C0", "First event", lambda: self.goto(0), False),
                                   ("\u25C0", "Previous event (hold to repeat)", lambda: self.goto(self.cur - 1), True)):
            fb.addWidget(self._nav(text, tip, fn, rep))
        self.entry = _edit("0", 80)
        self.entry.returnPressed.connect(lambda: self.goto(self._int(self.entry.text(), self.cur)))
        fb.addWidget(self.entry)
        self.total = QLabel("/ -")
        fb.addWidget(self.total)
        for text, tip, fn, rep in (("\u25B6", "Next event (hold to repeat)", lambda: self.goto(self.cur + 1), True),
                                   ("\u25B6|", "Last event",
                                    lambda: self.goto(self.run.n_entries - 1 if self.run else 0), False)):
            fb.addWidget(self._nav(text, tip, fn, rep))
        fb.addStretch(1)
        el.addLayout(fb)
        top = QHBoxLayout()
        self.mode = QButtonGroup(self)
        for i, text in enumerate(("Single event", "Average")):
            rb = QRadioButton(text)
            rb.setChecked(i == 0)
            self.mode.addButton(rb, i)
            top.addWidget(rb)
        self.mode.buttonClicked.connect(lambda _b: self.redraw())
        top.addSpacing(8)
        top.addWidget(QLabel("from"))
        self.avg_from, self.avg_to = _edit("0", 70), _edit("1000", 70)
        top.addWidget(self.avg_from)
        top.addWidget(QLabel("to"))
        top.addWidget(self.avg_to)
        for text, fn in (("All", self._range_all), ("1k", lambda: self._range_n(1000)),
                         ("10k", lambda: self._range_n(10000))):
            b = _btn(text)
            b.clicked.connect(fn)
            top.addWidget(b)
        self.avg_btn = _btn("Compute Avg", "#7c3aed", "white")
        self.avg_btn.clicked.connect(self.compute_average)
        top.addWidget(self.avg_btn)
        top.addStretch(1)
        el.addLayout(top)
        cards.addWidget(ev, 3)

        se = QGroupBox("Jump to event")
        sl = QGridLayout(se)
        sl.setHorizontalSpacing(8)
        self.op = QComboBox()
        self.op.addItems(["<", ">", "|q|>"])
        self.thresh = _edit("-0.5", 70)
        self.search_btn = _btn("Search", "#fd7e14", "white")
        self.search_btn.clicked.connect(self.charge_search)
        self.mv_thresh = _edit("5.0", 70)
        self.mv_box = QHBoxLayout()
        self.mv_group = QButtonGroup(self)
        self.find_btn = _btn("Find", "#0891b2", "white")
        self.find_btn.clicked.connect(self.peak_search)
        self.noise_freq = QComboBox()
        self.noise_freq.addItems(["All"] + [str(f) for f in W.NOISE_FREQS_MHZ])
        self.noise_amp = _edit("10", 70)
        self.scan_btn = _btn("Scan", "#065f46", "white")
        self.scan_btn.clicked.connect(self.noise_scan)
        rows = (("Charge", [self.op, self.thresh, QLabel("pC")], self.search_btn),
                ("Peak >", [self.mv_thresh, QLabel("mV  ch:"), self.mv_box], self.find_btn),
                ("Noise", [self.noise_freq, QLabel("MHz >"), self.noise_amp, QLabel("uV")], self.scan_btn))
        for r, (name, items, btn) in enumerate(rows):
            k = QLabel(name)
            k.setObjectName("key")
            sl.addWidget(k, r, 0)
            h = QHBoxLayout()
            h.setSpacing(6)
            for it in items:
                (h.addLayout if isinstance(it, QHBoxLayout) else h.addWidget)(it)
            sl.addLayout(h, r, 1)
            sl.addWidget(btn, r, 2)
        sl.setColumnStretch(3, 1)
        cards.addWidget(se, 2)
        lay.addLayout(cards)

        rg = QGroupBox("Plot ranges")
        rl = QHBoxLayout(rg)
        self.slot_box = QGridLayout()
        self.slot_box.setHorizontalSpacing(4)
        rl.addLayout(self.slot_box)
        rl.addWidget(_vsep())
        rng = QGridLayout()
        self.ped_s, self.ped_e = _edit(str(W.PED_START), 60), _edit(str(W.PED_END), 60)
        self.sig_s, self.sig_e = _edit(str(W.SIG_START), 60), _edit(str(W.SIG_END), 60)
        for r, (title, a, b, d) in enumerate((("Pedestal", self.ped_s, self.ped_e, "ped"),
                                              ("Signal", self.sig_s, self.sig_e, "sig"))):
            rng.addWidget(QLabel(title), r, 0)
            for c, e in enumerate((a, b)):
                e.returnPressed.connect(self.redraw)
                rng.addWidget(e, r, 1 + c)
            x = _btn("Reset")
            x.clicked.connect(lambda _=False, a=a, b=b, d=d: self._reset_range(a, b, d))
            rng.addWidget(x, r, 3)
        pc = _btn("Ped Center", "#4f46e5", "white")
        pc.clicked.connect(self.ped_center)
        rng.addWidget(pc, 0, 4)
        rl.addLayout(rng)
        rl.addStretch(1)
        lay.addWidget(rg)
        st = QHBoxLayout()
        self.avg_status = QLabel()
        self.avg_status.setObjectName("hint")
        st.addWidget(self.avg_status)
        st.addStretch(1)
        self.status = QLabel("")
        self.status.setObjectName("hint")
        st.addWidget(self.status)
        lay.addLayout(st)

        self.slot_lbl = [f"CH{i}" for i in range(MAX_SLOTS)]
        self.y_lo = [_edit("5.0", 46) for _ in range(MAX_SLOTS)]
        self.y_hi = [_edit("5.0", 46) for _ in range(MAX_SLOTS)]
        self.x_s = [_edit("", 50) for _ in range(MAX_SLOTS)]
        self.x_e = [_edit("", 50) for _ in range(MAX_SLOTS)]
        for e in self.y_lo + self.y_hi + self.x_s + self.x_e:
            e.returnPressed.connect(self.redraw)
        self.n_slots = 4
        self._slots(4)

        self.fig = Figure(facecolor=BG)
        self.canvas = FigureCanvasQTAgg(self.fig)
        self.canvas.mpl_connect("button_press_event", self._press)
        self.canvas.mpl_connect("motion_notify_event", self._motion)
        self.canvas.mpl_connect("button_release_event", self._release)
        lay.addWidget(self.canvas, 1)

    def _nav(self, text, tip, fn, repeat):
        b = _btn(text, "#343a40", "white")
        b.setToolTip(tip)
        b.setMinimumWidth(44)
        b.setAutoRepeat(repeat)
        b.setAutoRepeatDelay(400)
        b.setAutoRepeatInterval(80)
        b.clicked.connect(fn)
        return b

    def _slots(self, n):
        self.n_slots = n
        for e in self.y_lo + self.y_hi + self.x_s + self.x_e:   # keep the entries alive across the rebuild
            e.setParent(None)
        while self.slot_box.count():
            w = self.slot_box.takeAt(0).widget()
            if w is not None:
                w.setParent(None)
        for i in range(n):
            f = QWidget()
            h = QHBoxLayout(f)
            h.setContentsMargins(0, 0, 6, 0)
            h.setSpacing(4)
            name = QLabel(self.slot_lbl[i])
            name.setStyleSheet(f"color: {theme.C['accent']}; font-weight: 700;")
            for w in (name, QLabel("Y -"), self.y_lo[i], QLabel("/+"), self.y_hi[i], QLabel("mV  X"),
                      self.x_s[i], QLabel("-"), self.x_e[i]):
                h.addWidget(w)
            self.slot_box.addWidget(f, i // 2, i % 2)   # two channels per row, as in the Tk panel
        ap = _btn("Apply", "#16a34a", "white")
        ap.clicked.connect(self.redraw)
        self.slot_box.addWidget(ap, 0, 2)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _int(text, default):
        try:
            return int(text)
        except ValueError:
            return default

    @staticmethod
    def _float(text, default):
        try:
            return float(text)
        except ValueError:
            return default

    def _range(self, a, b, default):
        """(start, end) from two entries, clamped; default when empty or inverted."""
        s, e = self._int(a.text(), None), self._int(b.text(), None)
        if s is None or e is None:
            return default
        n = self.run.n_samples if self.run else default[1] + 1
        s, e = max(0, min(s, n - 1)), max(0, min(e, n))
        return (s, e) if e > s else default

    def _file_window(self, which):
        """The loaded file's own analysis window (production macro rules), or the laser defaults."""
        r = self.run
        if which == "ped":
            return (r.ped_start, r.ped_end) if r else (W.PED_START, W.PED_END)
        return (r.sig_start, r.sig_end) if r else (W.SIG_START, W.SIG_END)

    def ped_range(self):
        return self._range(self.ped_s, self.ped_e, self._file_window("ped"))

    def sig_range(self):
        return self._range(self.sig_s, self.sig_e, self._file_window("sig"))

    def _reset_range(self, a, b, which):
        default = self._file_window(which)
        a.setText(str(default[0]))
        b.setText(str(default[1]))
        self.redraw()

    def _need_file(self):
        if self.run is None:
            QMessageBox.warning(self, "No File", "Load a file first.")
            return False
        return True

    def _busy_btn(self, btn, busy, text, bg):
        btn.setText("Cancel" if busy else text)
        _paint(btn, "#dc3545" if busy else bg)

    # ------------------------------------------------------------------ file
    def open_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select RAW .root file", "", "ROOT files (*.root);;All files (*)")
        if path:
            self.open_path(path)

    def load_selection(self):
        from pages.daq import data_files
        page = data_files.live()
        paths = page.selected_paths() if page is not None else []
        if not paths:
            QMessageBox.warning(self, "No Selection", "Select a RAW file in the Data Files tab first.")
        elif len(paths) > 1:
            QMessageBox.warning(self, "Multiple Files", "Select only one file for Waveform Inspection.")
        else:
            self.open_path(paths[0])

    def open_path(self, path):
        self.status.setText("Loading...")
        self._placeholder()

        def work():
            try:
                run = W.open_run(path)
                self.later(lambda: self._loaded(run))
            except Exception as e:
                self.later(lambda e=e: self.status.setText(f"Error: {e}"))
        threading.Thread(target=work, daemon=True).start()

    def _loaded(self, run):
        self.run, self.cur = run, 0
        self.avg_spec = self.avg_wave = None
        self.ped_s.setText(str(run.ped_start))
        self.ped_e.setText(str(run.ped_end))
        self.sig_s.setText(str(run.sig_start))
        self.sig_e.setText(str(run.sig_end))
        self.file_lbl.setText(os.path.basename(run.path))
        self.total.setText(f"/ {run.n_entries - 1}")
        self.status.setText(f"{run.n_entries:,} entries  .  {len(run.channels)} ch  .  {run.n_samples} samples/ch"
                            f"  .  ped [{run.ped_start}-{run.ped_end}]  sig [{run.sig_start}-{run.sig_end}]")
        n = min(len(run.channels), MAX_SLOTS)
        for i, ch in enumerate(run.channels[:n]):
            self.slot_lbl[i] = f"CH{ch}"
            self.y_lo[i].setText("5.0")
            self.y_hi[i].setText("5.0")
            self.x_s[i].setText("")
            self.x_e[i].setText("")
        self._slots(n)
        for b in self.mv_group.buttons():
            self.mv_group.removeButton(b)
            b.setParent(None)
        for i, ch in enumerate(run.channels):
            rb = QRadioButton(f"CH{ch}")
            rb.setChecked(i == 0)
            self.mv_group.addButton(rb, ch)
            self.mv_box.addWidget(rb)
        self.goto(0)

    def clear(self):
        self.run, self.cur = None, 0
        self.file_lbl.setText("-")
        self.entry.setText("0")
        self.total.setText("/ -")
        self.status.setText("Load a RAW .root file to begin.")
        self._placeholder()

    # ------------------------------------------------------------------ navigation / drawing
    def goto(self, n):
        if self.run is None:
            return
        self.cur = max(0, min(n, self.run.n_entries - 1))
        self.entry.setText(str(self.cur))
        self.redraw()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Left:
            self.goto(self.cur - 1)
        elif e.key() == Qt.Key_Right:
            self.goto(self.cur + 1)
        else:
            super().keyPressEvent(e)

    def redraw(self):
        if self.run is None:
            return
        try:
            flat = self.run.entry(self.cur)
        except Exception as e:
            self.status.setText(f"Read error: {e}")
            return
        try:
            self._render(flat)
        except Exception as e:
            self.status.setText(f"Render error: {e}")

    def _grid(self):
        n = len(self.run.channels)
        cols = min(2, n)
        return (n + cols - 1) // cols, cols

    def _style(self, ax, xlabel, ylabel):
        ax.tick_params(axis="both", colors=TICK_C, labelsize=8)
        for sp in ax.spines.values():
            sp.set_edgecolor(SPINE_C)
        ax.set_xlabel(xlabel, color=TICK_C, fontsize=8)
        ax.set_ylabel(ylabel, color=TICK_C, fontsize=8)
        ax.grid(True, color=GRID_C, linewidth=0.5)
        ax.legend(fontsize=7, framealpha=0.7, loc="upper right")

    def _finish(self):
        try:
            self.fig.tight_layout(pad=1.5)
        except Exception:
            pass
        self.canvas.draw_idle()

    def _render(self, flat):
        avg = self.mode.checkedId() == 1
        if self.fft:
            if avg:
                self._render_fft_avg() if self.avg_spec else self._message(
                    "Click  'Compute Avg FFT'  to average the selected event range")
            else:
                self._render_fft(flat)
            return
        if avg:
            if self.avg_wave is None:
                self._message("Click  'Compute Avg Waveform'  to average the selected event range")
                return
            n = self.run.n_samples
            flat = np.zeros((max(self.run.channels) + 1) * n)
            for ch, w in self.avg_wave.items():
                flat[ch * n:(ch + 1) * n] = w
        if not self.run.channels:
            return
        rows, cols = self._grid()
        self.fig.clear()
        self.fig.set_facecolor(BG)
        ped, sig = self.ped_range(), self.sig_range()
        tag = "AVG" if avg else f"#{self.cur}"
        for i, ch in enumerate(self.run.channels[:self.n_slots]):
            ax = self.fig.add_subplot(rows, cols, i + 1)
            ax.set_facecolor(BG)
            seg = self.run.segment(flat, ch)
            v = seg * W.ADC_TO_MV
            p_adc = W.pedestal(seg, *ped)
            p_mv = p_adc * W.ADC_TO_MV
            if ch == W.TRIGGER_CH:
                ax.set_title(f"CH{ch}  {tag}  |  Trigger", color=LABEL_C, fontsize=9, fontweight="bold", pad=4)
                ax.set_ylim(p_mv - 100, p_mv + 200)
            else:
                q = W.charge_pC(seg, p_adc, *sig)
                ax.set_title(f"CH{ch}  {tag}  |  {q:+.3f} pC", color=LABEL_C, fontsize=9, fontweight="bold", pad=4)
                ax.axvspan(*ped, alpha=0.35, color=PED_SPAN_C, lw=0)
                ax.axvspan(*sig, alpha=0.35, color=SIG_SPAN_C, lw=0)
                ax.set_ylim(p_mv - self._float(self.y_lo[i].text(), 5.0), p_mv + self._float(self.y_hi[i].text(), 5.0))
            ax.plot(np.arange(len(seg)), v, color=WAVE, linewidth=0.7)
            ax.axhline(p_mv, color=PED_C, linewidth=1.0, linestyle="--", alpha=0.8, label=f"ped {p_mv:.2f} mV")
            ax.set_xlim(self._int(self.x_s[i].text(), 0), self._int(self.x_e[i].text(), len(seg)))
            self._style(ax, "Sample  [2 ns/sample]", "Voltage [mV]")
        self._finish()

    def _peak_axes(self, ax, freqs, mag, color, title, ylabel, clocks=False):
        floor = float(np.median(mag[1:]))
        b = int(np.argmax(mag[1:]) + 1)
        ax.plot(freqs, mag, color=color, linewidth=0.8)
        ax.axvline(freqs[b], color="#dc2626", linewidth=1.0, linestyle="--", alpha=0.8, label=f"peak {freqs[b]:.1f} MHz")
        ax.axhline(floor, color="#9ca3af", linewidth=0.8, linestyle=":", alpha=0.7, label=f"noise ~{floor * 1e3:.1f} uV")
        ax.annotate(f"{freqs[b]:.1f} MHz\n{mag[b] * 1e3:.2f} uV", xy=(freqs[b], mag[b]),
                    xytext=(freqs[b] + W.FS_MHZ * 0.04, mag[b] * 0.85), fontsize=7, color="#dc2626",
                    arrowprops=dict(arrowstyle="-", color="#dc2626", lw=0.8))
        if clocks:
            top = ax.get_ylim()[1] if ax.get_ylim()[1] > 0 else float(np.max(mag))
            for f, lbl in W.NOISE_FREQ_LABELS.items():
                ax.axvline(f, color="#f59e0b", linewidth=0.8, linestyle=":", alpha=0.6)
                ax.text(f, top * 0.97, lbl, rotation=90, fontsize=6, color="#b45309", va="top", ha="right", alpha=0.8)
        ax.set_title(title.format(peak=f"{freqs[b]:.1f} MHz peak"), color=LABEL_C, fontsize=9, fontweight="bold", pad=4)
        ax.set_xlim(0, W.FS_MHZ / 2)
        self._style(ax, "Frequency [MHz]", ylabel)

    def _render_fft(self, flat):
        rows, cols = self._grid()
        self.fig.clear()
        self.fig.set_facecolor(BG)
        n = self.run.n_samples
        for i, ch in enumerate(self.run.channels[:self.n_slots]):
            seg = self.run.segment(flat, ch)
            ps, pe = self.ped_range()
            freqs, mag = W.spectrum((seg - np.mean(seg[ps:pe])) * W.ADC_TO_MV, n)
            ax = self.fig.add_subplot(rows, cols, i + 1)
            ax.set_facecolor(BG)
            what = "Trigger" if ch == W.TRIGGER_CH else "{peak}"
            self._peak_axes(ax, freqs, mag, "#7c3aed", f"CH{ch}  #{self.cur}  FFT  |  {what}", "Amplitude [mV]")
        self._finish()

    def _render_fft_avg(self):
        rows, cols = self._grid()
        self.fig.clear()
        self.fig.set_facecolor(BG)
        freqs = np.fft.rfftfreq(self.run.n_samples, d=W.DT) / 1e6
        for i, ch in enumerate(self.run.channels[:self.n_slots]):
            mag = self.avg_spec.get(ch)
            if mag is None:
                continue
            ax = self.fig.add_subplot(rows, cols, i + 1)
            ax.set_facecolor(BG)
            what = "Trigger" if ch == W.TRIGGER_CH else "{peak}"
            self._peak_axes(ax, freqs, mag, "#0891b2", f"CH{ch}  AVG FFT  |  {what}", "RMS Amplitude [mV]", clocks=True)
        self._finish()

    def _message(self, msg, bg="#f9fafb"):
        self.fig.clear()
        self.fig.set_facecolor(bg)
        ax = self.fig.add_subplot(111)
        ax.set_facecolor(bg)
        ax.text(0.5, 0.5, msg, ha="center", va="center", fontsize=12, color="#6b7280", transform=ax.transAxes)
        ax.axis("off")
        self.canvas.draw_idle()

    def _placeholder(self):
        self.fig.clear()
        self.fig.set_facecolor(BG)
        ax = self.fig.add_subplot(111)
        ax.axis("off")
        self.canvas.draw_idle()

    def toggle_fft(self):
        self.fft = not self.fft
        _paint(self.fft_btn, "#4f46e5" if self.fft else theme.C["hover"], "white" if self.fft else theme.C["text"])
        if not self.avg_busy:
            self.avg_btn.setText("Compute Avg FFT" if self.fft else "Compute Avg Waveform")
        self.redraw()

    def ped_center(self):
        """Fit each channel's Y range to the current event's spread around the pedestal."""
        if self.run is None:
            return
        flat = self.run.entry(self.cur)
        for i, ch in enumerate(self.run.channels[:self.n_slots]):
            if ch == W.TRIGGER_CH:
                continue
            v = self.run.segment(flat, ch) * W.ADC_TO_MV
            ps, pe = self.ped_range()
            p = float(np.mean(v[ps:pe]))
            pad = max(0.5, (v.max() - v.min()) * 0.15)
            self.y_lo[i].setText(str(max(0.5, round(p - v.min() + pad, 1))))
            self.y_hi[i].setText(str(max(0.5, round(v.max() - p + pad, 1))))
        self.redraw()

    # ------------------------------------------------------------------ averaging
    def _range_all(self):
        if not self.avg_busy and self.run:
            self.avg_from.setText("0")
            self.avg_to.setText(str(self.run.n_entries))

    def _range_n(self, n):
        if not self.avg_busy and self.run:
            self.avg_to.setText(str(min(self._int(self.avg_from.text(), 0) + n, self.run.n_entries)))

    def _avg_msg(self, text, color="#6b7280"):
        self.avg_status.setText(text)
        self.avg_status.setStyleSheet(f"color: {color}; font-weight: normal;")

    def compute_average(self):
        if self.avg_busy:            # the button doubles as Cancel
            self._avg_stop = True
            self._avg_msg("Cancelling...", "#f0ad4e")
            return
        if self.run is None:
            return
        a, b = self._int(self.avg_from.text(), None), self._int(self.avg_to.text(), None)
        if a is None or b is None:
            self._avg_msg("Invalid range", "#dc3545")
            return
        if a < 0 or b <= a:
            self._avg_msg("Bad range (from < to required)", "#dc3545")
            return
        b = min(b, self.run.n_entries)
        total = b - a
        if total < 2:
            self._avg_msg("Need at least 2 events", "#dc3545")
            return
        self.mode.button(1).setChecked(True)   # computing an average means showing it
        self.avg_busy, self._avg_stop = True, False
        self._busy_btn(self.avg_btn, True, "", "")
        self.avg_from.setEnabled(False)
        self.avg_to.setEnabled(False)
        self._avg_msg(f"Computing 0/{total}...")
        run, ped = self.run, self.ped_range()

        def work():
            try:
                res = W.average(run, a, b, ped, lambda: self._avg_stop,
                                lambda c: self.later(lambda c=c: self._avg_msg(
                                    f"Computing {c:,}/{total:,} ({100 * c // total}%)...")))
                err = None
            except Exception as e:
                res, err = None, e
            self.later(lambda: self._avg_done(run, res, err))
        threading.Thread(target=work, daemon=True).start()

    def _avg_done(self, run, res, err):
        self.avg_busy = False
        self.avg_btn.setText("Compute Avg FFT" if self.fft else "Compute Avg Waveform")
        _paint(self.avg_btn, "#7c3aed")
        self.avg_from.setEnabled(True)
        self.avg_to.setEnabled(True)
        if run is not self.run:      # a different file was loaded meanwhile
            return
        if err is not None:
            self._avg_msg(f"Error: {err}", "#dc3545")
            return
        spec, wave, count, stopped = res
        if not count:
            self._avg_msg("No events read", "#dc3545")
            return
        self.avg_spec, self.avg_wave = spec, wave   # partial result is still shown after a cancel
        self._avg_msg(f"{'Cancelled' if stopped else 'Done'} - {count:,} events averaged",
                      "#f0ad4e" if stopped else "#16a34a")
        self.redraw()

    # ------------------------------------------------------------------ searches
    def _start_job(self, btn, run_fn, done_fn, desc):
        """Run a search in a thread; the button turns into Cancel meanwhile."""
        token = object()
        self._job = token
        btn._busy = True
        self._busy_btn(btn, True, "", "")
        self.status.setText(f"Searching for {desc}...")
        run = self.run
        total = run.n_entries

        def progress(b):
            self.later(lambda: self._job is token and self.status.setText(
                f"Searching... {b:,}/{total:,}  ({100 * b // max(total, 1)}%)"))

        def work():
            try:
                res, err = run_fn(lambda: self._job is not token, progress), None
            except Exception as e:
                res, err = None, e
            self.later(lambda: self._job is token and done_fn(res, err))
        threading.Thread(target=work, daemon=True).start()
        return token

    def _cancel_job(self, msg):
        self._job = None
        self.status.setText(msg)
        self._reset_buttons()

    def _reset_buttons(self):
        for btn, text, bg in ((self.search_btn, "Search", "#fd7e14"), (self.find_btn, "Find", "#0891b2"),
                              (self.scan_btn, "Scan", "#065f46")):
            if getattr(btn, "_busy", False):
                btn._busy = False
                self._busy_btn(btn, False, text, bg)

    def _found(self, res, err, desc):
        self._job = None
        self._reset_buttons()
        if err is not None:
            self.status.setText(f"Search error: {err}")
        elif res is not None and res >= 0:
            self.status.setText(f"Found {desc} at entry #{res}")
            self.goto(res)
        else:
            self.status.setText(f"No more events with {desc}")

    def charge_search(self):
        if getattr(self.search_btn, "_busy", False):
            return self._cancel_job("Search cancelled.")
        if not self._need_file():
            return
        try:
            thr = float(self.thresh.text())
        except ValueError:
            return QMessageBox.warning(self, "Invalid", "Enter a numeric pC threshold.")
        op = self.op.currentText()
        match, desc = {">": (lambda q: q > thr, f"charge > {thr} pC"),
                       "|q|>": (lambda q: np.abs(q) > thr, f"|charge| > {thr} pC")}.get(
            op, (lambda q: q < thr, f"charge < {thr} pC"))
        ped, sig, start = self.ped_range(), self.sig_range(), self.cur + 1
        self._start_job(self.search_btn,
                        lambda stop, prog: W.find_charge(self.run, start, match, ped, sig, stop, prog),
                        lambda r, e: self._found(r, e, desc), desc)

    def peak_search(self):
        if getattr(self.find_btn, "_busy", False):
            return self._cancel_job("Search cancelled.")
        if not self._need_file():
            return
        try:
            thr = float(self.mv_thresh.text())
        except ValueError:
            return QMessageBox.warning(self, "Invalid", "Enter a numeric mV threshold.")
        ch = self.mv_group.checkedId()
        if ch not in self.run.channels:
            return QMessageBox.warning(self, "Channel", f"CH{ch} not in this file.")
        desc, ped, start = f"peak > {thr} mV on CH{ch}", self.ped_range(), self.cur + 1
        self._start_job(self.find_btn,
                        lambda stop, prog: W.find_peak(self.run, start, ch, thr, ped, stop, prog),
                        lambda r, e: self._found(r, e, desc), desc)

    def noise_scan(self):
        if getattr(self.scan_btn, "_busy", False):
            return self._cancel_job("Noise scan cancelled.")
        if not self._need_file():
            return
        try:
            thr = float(self.noise_amp.text())
        except ValueError:
            return QMessageBox.warning(self, "Invalid", "Enter a numeric uV threshold.")
        sel = self.noise_freq.currentText()
        freqs = W.NOISE_FREQS_MHZ if sel == "All" else [float(sel)]

        def done(res, err):
            self._job = None
            self._reset_buttons()
            if err is not None:
                self.status.setText(f"Scan error: {err}")
                return
            results, _stopped = res
            self.status.setText(f"Noise scan done - {len(results)} events found")
            self._noise_results(results, thr, sel)
        self._start_job(self.scan_btn, lambda stop, prog: W.noise_scan(self.run, freqs, thr, stop, prog),
                        done, f">{thr} uV at {sel} MHz")

    def _noise_results(self, results, thr, sel):
        if self._noise_dlg is not None:
            self._noise_dlg.close()
        dlg = self._noise_dlg = QDialog(self)
        dlg.setWindowTitle(f"Noise Scan - >{thr} uV @ {sel} MHz")
        dlg.resize(380, 420)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(f"{len(results)} events  |  click to jump"))
        rows = sorted(results, key=lambda x: -x[3])
        lst = QListWidget()
        for ev, ch, f, amp in rows:
            lst.addItem(f"#{ev:7d}  CH{ch}  {f:6.1f} MHz  {amp:7.1f} uV")
        lst.currentRowChanged.connect(lambda i: i >= 0 and self.goto(rows[i][0]))
        v.addWidget(lst, 1)
        c = QPushButton("Close")
        c.clicked.connect(dlg.close)
        v.addWidget(c)
        dlg.show()

    # ------------------------------------------------------------------ drag zoom (right-click resets)
    def _press(self, ev):
        if ev.inaxes is None:
            return
        ax = ev.inaxes
        if ev.button == 3:
            idx = self.fig.get_axes().index(ax) if ax in self.fig.get_axes() else -1
            if self.fft:
                ax.set_xlim(0, W.FS_MHZ / 2)
                self.canvas.draw_idle()
            elif 0 <= idx < self.n_slots:
                for e, t in ((self.x_s[idx], ""), (self.x_e[idx], ""), (self.y_lo[idx], "5.0"), (self.y_hi[idx], "5.0")):
                    e.setText(t)
                self.redraw()
            return
        if ev.button != 1 or ev.xdata is None:
            return
        self._drag = (ax, ev.xdata, ev.ydata)
        self._rect = Rectangle((ev.xdata, ev.ydata), 0, 0, linewidth=1.2, edgecolor="#2563eb",
                               facecolor="#2563eb", alpha=0.15, zorder=10)
        ax.add_patch(self._rect)
        self.canvas.draw_idle()

    def _motion(self, ev):
        if self._drag is None or self._rect is None or ev.inaxes is not self._drag[0] or ev.xdata is None:
            return
        _ax, x0, y0 = self._drag
        self._rect.set_xy((min(x0, ev.xdata), min(y0, ev.ydata)))
        self._rect.set_width(abs(ev.xdata - x0))
        self._rect.set_height(abs(ev.ydata - y0))
        self.canvas.draw_idle()

    def _release(self, ev):
        if self._rect is not None:
            try:
                self._rect.remove()
            except Exception:
                pass
            self._rect = None
        drag, self._drag = self._drag, None
        if drag is None or ev.button != 1 or ev.inaxes is not drag[0] or ev.xdata is None or ev.ydata is None:
            self.canvas.draw_idle()
            return
        ax, x0, y0 = drag
        dx, dy = abs(ev.xdata - x0), abs(ev.ydata - y0)
        if dx < 1e-9 and dy < 1e-9:
            self.canvas.draw_idle()
            return
        x_lo, x_hi = sorted([x0, ev.xdata])
        y_lo, y_hi = sorted([y0, ev.ydata])
        if self.fft:
            ax.set_xlim(x_lo, x_hi)
            self.canvas.draw_idle()
            return
        axes = self.fig.get_axes()
        idx = axes.index(ax) if ax in axes else -1
        if not 0 <= idx < self.n_slots:
            return
        if dx > 1e-9:
            self.x_s[idx].setText(str(int(round(x_lo))))
            self.x_e[idx].setText(str(int(round(x_hi))))
        if dy > 1e-9:
            seg = self.run.segment(self.run.entry(self.cur), self.run.channels[idx])
            ps, pe = self.ped_range()
            p = float(np.mean(seg[ps:pe])) * W.ADC_TO_MV
            self.y_lo[idx].setText(str(round(max(0.1, p - y_lo), 2)))
            self.y_hi[idx].setText(str(round(max(0.1, y_hi - p), 2)))
        self.redraw()

    def guide(self):
        QMessageBox.information(self, "Waveform Inspection Guide", (
            "1. Select a RAW .root file in the Data Files tab\n"
            "2. Click 'Use selected file' (or 'Open...')\n\n"
            "Navigation: First / Prev / Next / Last (hold to repeat), arrow keys,\n"
            "or type an entry number + Enter.\n\n"
            "Axes: Y - / + is the mV range below / above the pedestal per channel,\n"
            "X the sample range (blank = full record). Drag on a plot to zoom,\n"
            "right-click to reset. Ped Center fits Y to the current event.\n\n"
            "Charge search: <  below,  >  above,  |q|>  magnitude above the threshold\n"
            "(signal of either polarity). It searches forward from the current event;\n"
            "click the button again to cancel."))

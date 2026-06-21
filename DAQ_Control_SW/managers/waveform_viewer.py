"""
Embedded Waveform Inspection panel.

Reads RAW .root files directly with uproot (no ROOT installation needed)
and renders channel waveforms in a matplotlib canvas inside the DAQ UI.

Physics constants mirror Analysis.cpp / config3.h:
  ADC_to_mV   = 0.1220703125   (= 2000 / 16384)
  Impedance   = 50 Ω
  TimePerSample = 2 ns
  charge_pC   = TimePerSample * ADC_to_mV * Σ(ped - adc[i]) / Impedance
"""

import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import numpy as np
import uproot
import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

# ── Physics constants (from Analysis.cpp / config3.h) ─────────────────────
ADC_TO_MV      = 0.1220703125   # mV per ADC count (2000 / 16384)
IMPEDANCE      = 50.0           # Ohm
TIME_PER_SAMPLE = 2.0           # ns per sample
TRIGGER_CH     = 3
PED_START, PED_END = 100, 300
SIG_START, SIG_END = 610, 660

# ── Light theme colours ───────────────────────────────────────────────────
BG      = "white"
FG      = "#1f2937"
WAVE    = "#1d4ed8"
PED_C   = "#6366f1"
SIG_C   = "#dc2626"
GRID_C  = "#e5e7eb"
SPINE_C = "#d1d5db"
PED_SPAN_C = "#e0e7ff"   # light indigo for pedestal region
SIG_SPAN_C = "#fee2e2"   # light red for signal region


def _pedestal(adc: np.ndarray) -> float:
    return float(np.mean(adc[PED_START:PED_END]))


def _charge_pC(adc: np.ndarray, ped: float) -> float:
    integral = np.sum(ped - adc[SIG_START:SIG_END].astype(float))
    return TIME_PER_SAMPLE * ADC_TO_MV * integral / IMPEDANCE


class WaveformViewerPanel:
    """Embedded waveform inspection panel — replaces the terminal ROOT viewer."""

    def __init__(self, parent: tk.Widget, controller):
        self.parent = parent
        self.controller = controller

        self._tree       = None
        self._n_entries  = 0
        self._n_samples  = 0
        self._channels   : list[int] = []
        self._cur_entry  = 0
        self._search_id  = None   # cancel token for in-progress search

        # ── Per-channel axis settings (up to 8 slots, dynamically rebuilt) ─
        MAX_SLOTS = 8
        self._slot_y_lo    = [tk.DoubleVar(value=5.0) for _ in range(MAX_SLOTS)]
        self._slot_y_hi    = [tk.DoubleVar(value=5.0) for _ in range(MAX_SLOTS)]
        self._slot_x_s     = [tk.StringVar(value="")  for _ in range(MAX_SLOTS)]
        self._slot_x_e     = [tk.StringVar(value="")  for _ in range(MAX_SLOTS)]
        self._slot_lbl_var = [tk.StringVar(value=f"CH{i}") for i in range(MAX_SLOTS)]
        self._n_slots      = 4          # active slot count (updated on load)
        self._ch_grid      = None       # reference to the channel grid frame
        self._fft_mode     = False      # False = time domain, True = FFT
        self._fft_avg_mode = tk.BooleanVar(value=False)  # False=Single, True=Average
        self._avg_from     = tk.StringVar(value="0")
        self._avg_to       = tk.StringVar(value="1000")
        self._avg_result   = None       # cached {ch: mag_array} after compute
        self._avg_computing = False     # guard against concurrent compute
        self._thresh = tk.StringVar(value="-0.5")

        self._status = tk.StringVar(value="Load a RAW .root file to begin.")
        self._file_lbl = tk.StringVar(value="—")
        self._entry_var = tk.StringVar(value="0")

        self._build_ui(parent)

    # ══════════════════════════════════════════════════════════════════════
    # UI construction
    # ══════════════════════════════════════════════════════════════════════

    def _build_ui(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=0)
        parent.rowconfigure(1, weight=0)   # FFT controls (hidden until FFT ON)
        parent.rowconfigure(2, weight=0)   # per-channel axis bar
        parent.rowconfigure(3, weight=1)   # canvas

        # ── Row 0: file bar ──────────────────────────────────────────────
        fb = ttk.Frame(parent, padding=(8, 6, 8, 2))
        fb.grid(row=0, column=0, sticky="ew")

        ttk.Label(fb, text="File:", font=("Helvetica", 10, "bold")).pack(side=tk.LEFT)
        ttk.Label(fb, textvariable=self._file_lbl,
                  foreground="#1d4ed8", font=("Helvetica", 10)).pack(side=tk.LEFT, padx=(4, 10))

        tk.Button(fb, text="⟳ Use selected file", command=self._load_from_selection,
                  bg="#17a2b8", fg="white", font=("Helvetica", 9, "bold"),
                  relief="flat", padx=10).pack(side=tk.LEFT)
        tk.Button(fb, text="✕ Clear", command=self._clear,
                  bg="#6b7280", fg="white", font=("Helvetica", 9, "bold"),
                  relief="flat", padx=8).pack(side=tk.LEFT, padx=(4, 0))
        self._fft_btn = tk.Button(fb, text="📊 FFT", command=self._toggle_fft,
                  bg="#e5e7eb", fg="#374151", font=("Helvetica", 9, "bold"),
                  relief="flat", padx=8)
        self._fft_btn.pack(side=tk.LEFT, padx=(4, 0))
        tk.Button(fb, text="ⓘ", command=self._show_guide,
                  bg="#e5e7eb", fg="#374151", font=("Helvetica", 10, "bold"),
                  relief="flat", padx=6).pack(side=tk.LEFT, padx=(4, 0))

        ttk.Separator(fb, orient="vertical").pack(side=tk.LEFT, fill=tk.Y, padx=10)

        # Navigation
        self._total_lbl = ttk.Label(fb, text="/ —", font=("Helvetica", 10))
        tk.Button(fb, text="⏮", command=lambda: self._goto(0),
                  bg="#343a40", fg="white", relief="flat", padx=6).pack(side=tk.LEFT)
        tk.Button(fb, text="◀", command=self._prev,
                  bg="#343a40", fg="white", relief="flat", padx=8).pack(side=tk.LEFT, padx=(2, 0))
        e = ttk.Entry(fb, textvariable=self._entry_var, width=8, justify="center",
                      font=("Helvetica", 10, "bold"))
        e.pack(side=tk.LEFT, padx=4)
        e.bind("<Return>", lambda _: self._goto_var())
        self._total_lbl.pack(side=tk.LEFT)
        tk.Button(fb, text="▶", command=self._next,
                  bg="#343a40", fg="white", relief="flat", padx=8).pack(side=tk.LEFT, padx=(4, 0))
        tk.Button(fb, text="⏭", command=lambda: self._goto(max(0, self._n_entries - 1)),
                  bg="#343a40", fg="white", relief="flat", padx=6).pack(side=tk.LEFT, padx=(2, 0))

        ttk.Separator(fb, orient="vertical").pack(side=tk.LEFT, fill=tk.Y, padx=10)

        # Charge search
        ttk.Label(fb, text="Jump: charge <", font=("Helvetica", 9)).pack(side=tk.LEFT)
        ttk.Entry(fb, textvariable=self._thresh, width=7, justify="center").pack(side=tk.LEFT, padx=2)
        ttk.Label(fb, text="pC", font=("Helvetica", 9)).pack(side=tk.LEFT)
        self._search_btn = tk.Button(fb, text="🔍 Search", command=self._start_search,
                                     bg="#fd7e14", fg="white",
                                     font=("Helvetica", 9, "bold"), relief="flat", padx=8)
        self._search_btn.pack(side=tk.LEFT, padx=(4, 0))

        # ── Row 1: FFT controls (hidden until FFT is ON) ─────────────────
        self._fft_ctrl_frame = ttk.Frame(parent, padding=(8, 4, 8, 4))
        # Not grid()-placed yet; shown via _toggle_fft_controls()
        ttk.Label(self._fft_ctrl_frame, text="FFT Mode:",
                  font=("Helvetica", 9, "bold")).pack(side=tk.LEFT)
        ttk.Radiobutton(self._fft_ctrl_frame, text="Single event",
                        variable=self._fft_avg_mode, value=False,
                        command=self._on_fft_mode_change).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Radiobutton(self._fft_ctrl_frame, text="Average",
                        variable=self._fft_avg_mode, value=True,
                        command=self._on_fft_mode_change).pack(side=tk.LEFT, padx=(6, 0))

        ttk.Separator(self._fft_ctrl_frame, orient="vertical").pack(side=tk.LEFT, fill=tk.Y, padx=10)

        ttk.Label(self._fft_ctrl_frame, text="Event range  from:",
                  font=("Helvetica", 9)).pack(side=tk.LEFT)
        ttk.Entry(self._fft_ctrl_frame, textvariable=self._avg_from,
                  width=7, justify="center").pack(side=tk.LEFT, padx=3)
        ttk.Label(self._fft_ctrl_frame, text="to:",
                  font=("Helvetica", 9)).pack(side=tk.LEFT)
        ttk.Entry(self._fft_ctrl_frame, textvariable=self._avg_to,
                  width=7, justify="center").pack(side=tk.LEFT, padx=3)

        self._avg_compute_btn = tk.Button(
            self._fft_ctrl_frame, text="Compute Avg FFT",
            command=self._start_avg_fft,
            bg="#7c3aed", fg="white", font=("Helvetica", 9, "bold"),
            relief="flat", padx=10)
        self._avg_compute_btn.pack(side=tk.LEFT, padx=(8, 0))

        self._avg_status_lbl = ttk.Label(self._fft_ctrl_frame, text="",
                                         font=("Helvetica", 9), foreground="#6b7280")
        self._avg_status_lbl.pack(side=tk.LEFT, padx=(8, 0))

        # ── Row 2: per-channel axis bar ───────────────────────────────────
        ab = ttk.Frame(parent, padding=(8, 2, 8, 4))
        ab.grid(row=2, column=0, sticky="ew")
        ab.columnconfigure(0, weight=1)

        # Button strip (right)
        btn_strip = ttk.Frame(ab)
        btn_strip.grid(row=0, column=1, rowspan=2, sticky="ne", padx=(10, 0))
        tk.Button(btn_strip, text="⊙ Ped Center", command=self._ped_center,
                  bg="#4f46e5", fg="white", font=("Helvetica", 9, "bold"),
                  relief="flat", padx=8).pack(side=tk.LEFT, padx=(0, 4))
        tk.Button(btn_strip, text="Apply", command=self._redraw,
                  bg="#374151", fg="white", font=("Helvetica", 9, "bold"),
                  relief="flat", padx=8).pack(side=tk.LEFT)

        # Status label (below buttons)
        ttk.Label(ab, textvariable=self._status,
                  foreground="#6c757d", font=("Helvetica", 9, "italic")
                  ).grid(row=1, column=1, sticky="se", pady=(2, 0))

        # Per-channel controls grid (rebuilt dynamically after file load)
        self._ch_grid_parent = ab
        self._ch_grid = ttk.Frame(ab)
        self._ch_grid.grid(row=0, column=0, rowspan=2, sticky="w")
        self._build_slot_controls(4)   # placeholder: 4 slots before any file loaded

        # ── Row 2: matplotlib canvas ─────────────────────────────────────
        self._fig = Figure(facecolor=BG)
        self._canvas = FigureCanvasTkAgg(self._fig, master=parent)
        self._canvas.get_tk_widget().grid(row=3, column=0, sticky="nsew", padx=4, pady=(0, 4))

        # Keyboard nav (only when this panel's parent is focused)
        parent.bind("<Left>",  lambda _: self._prev())
        parent.bind("<Right>", lambda _: self._next())

        self._draw_placeholder()

    def _draw_placeholder(self):
        self._fig.clear()
        self._fig.set_facecolor(BG)
        ax = self._fig.add_subplot(111)
        ax.set_facecolor(BG)
        GUIDE_C = "#9ca3af"
        lines = [
            (0.60, "①  Select a file in the  Data Files  tab"),
            (0.49, "②  Click  [Waveform Inspection]  button  (or Ctrl+S)"),
            (0.38, "③  Use  [<]  [>]  buttons or  keyboard  arrow keys  to navigate"),
            (0.27, "④  Use  [Search]  to jump to events below a charge threshold"),
        ]
        for y, txt in lines:
            ax.text(0.5, y, txt, ha="center", va="center",
                    color=GUIDE_C, fontsize=12, transform=ax.transAxes)
        ax.set_xticks([]); ax.set_yticks([])
        for sp in ax.spines.values(): sp.set_visible(False)
        self._canvas.draw()

    # ══════════════════════════════════════════════════════════════════════
    # File loading
    # ══════════════════════════════════════════════════════════════════════

    def _open_file(self):
        path = filedialog.askopenfilename(
            title="Select RAW .root file",
            filetypes=[("ROOT files", "*.root"), ("All files", "*.*")])
        if path:
            self._load_file(path)

    def _load_from_selection(self):
        paths = self.controller.ui.get_selected_file_paths()
        if not paths:
            messagebox.showwarning("No Selection",
                                   "Select a RAW file in the Data Files tab first.")
            return
        if len(paths) > 1:
            messagebox.showwarning("Multiple Files",
                                   "Select only one file for Waveform Inspection.")
            return
        self._load_file(paths[0])

    def _load_file(self, path: str):
        import os
        self._status.set("Loading…")
        self._draw_placeholder()

        def worker():
            try:
                f = uproot.open(path)
                tree = f["T"]
                n = tree.num_entries
                rec_len = int(tree["RecordLength"].array(
                    entry_start=0, entry_stop=1, library="np")[0])
                keys = [k.split(";")[0] for k in tree.keys()]
                channels = [i for i in range(8) if f"OffsetValue{i}" in keys]
                self.parent.after(0, lambda: self._on_loaded(
                    path, tree, n, rec_len, channels))
            except Exception as exc:
                self.parent.after(0, lambda e=exc: self._status.set(f"Error: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def _on_loaded(self, path, tree, n_entries, n_samples, channels):
        import os
        self._tree = tree
        self._n_entries = n_entries
        self._n_samples = n_samples
        self._channels  = channels
        self._cur_entry = 0
        self._file_lbl.set(os.path.basename(path))
        self._total_lbl.config(text=f"/ {n_entries - 1}")
        self._status.set(
            f"{n_entries:,} entries  •  {len(channels)} ch  •  {n_samples} samples/ch"
            f"  •  sig [{SIG_START}–{SIG_END}]")
        # Rebuild per-channel axis controls to match actual channel count
        n_slots = min(len(channels), 8)
        for slot, ch in enumerate(channels[:n_slots]):
            self._slot_lbl_var[slot].set(f"CH{ch}")
            # Reset axis settings for newly loaded file
            self._slot_y_lo[slot].set(5.0)
            self._slot_y_hi[slot].set(5.0)
            self._slot_x_s[slot].set("")
            self._slot_x_e[slot].set("")
        self._build_slot_controls(n_slots)
        self._show_entry(0)

    # ══════════════════════════════════════════════════════════════════════
    # Navigation
    # ══════════════════════════════════════════════════════════════════════

    def _next(self):
        if self._tree: self._show_entry(self._cur_entry + 1)

    def _prev(self):
        if self._tree: self._show_entry(self._cur_entry - 1)

    def _goto(self, n: int):
        if self._tree: self._show_entry(n)

    def _goto_var(self):
        try:
            self._goto(int(self._entry_var.get()))
        except ValueError:
            pass

    def _redraw(self):
        if self._tree: self._show_entry(self._cur_entry)

    def _toggle_fft(self):
        self._fft_mode = not self._fft_mode
        if self._fft_mode:
            self._fft_btn.config(bg="#4f46e5", fg="white")
            self._fft_ctrl_frame.grid(row=1, column=0, sticky="ew")
        else:
            self._fft_btn.config(bg="#e5e7eb", fg="#374151")
            self._fft_ctrl_frame.grid_remove()
            self._avg_result = None   # clear cache when FFT turned off
        if self._tree:
            self._show_entry(self._cur_entry)

    def _on_fft_mode_change(self):
        """Called when Single ↔ Average radio is toggled."""
        self._avg_result = None   # cached avg is invalid for the other mode
        if self._tree:
            self._show_entry(self._cur_entry)

    def _start_avg_fft(self):
        """Validate range and start background average-FFT computation."""
        if not self._tree or self._avg_computing:
            return
        try:
            ev_from = int(self._avg_from.get())
            ev_to   = int(self._avg_to.get())
        except ValueError:
            self._avg_status_lbl.config(text="Invalid range", foreground="#dc3545")
            return
        if ev_from < 0 or ev_to <= ev_from:
            self._avg_status_lbl.config(text="Bad range (from < to required)", foreground="#dc3545")
            return
        ev_to    = min(ev_to, self._n_entries)
        n_events = ev_to - ev_from
        if n_events < 2:
            self._avg_status_lbl.config(text="Need at least 2 events", foreground="#dc3545")
            return

        self._avg_computing = True
        self._avg_compute_btn.config(state="disabled")
        self._avg_status_lbl.config(text=f"Computing 0/{n_events}…", foreground="#6b7280")
        self._avg_result = None

        def compute():
            DT     = 2e-9
            n      = self._n_samples
            window = np.hanning(n)
            # Accumulate power (|FFT|²) per channel; RMS average = sqrt(mean(power))
            power_acc = {ch: np.zeros(n // 2 + 1, dtype=np.float64)
                         for ch in self._channels}
            count = 0
            for ev in range(ev_from, min(ev_to, self._n_entries)):
                try:
                    adc_flat = self._tree["ADC"].array(
                        entry_start=ev, entry_stop=ev + 1, library="np")[0]
                except Exception:
                    continue
                for ch in self._channels:
                    seg    = adc_flat[ch * n: (ch + 1) * n]
                    ped    = float(np.mean(seg[PED_START:PED_END]))
                    sig_mv = (seg - ped) * ADC_TO_MV
                    mag    = np.abs(np.fft.rfft(sig_mv * window)) * 2.0 / n
                    power_acc[ch] += mag ** 2
                count += 1
                if count % 100 == 0:
                    p = count
                    self._avg_status_lbl.master.after(
                        0, lambda p=p: self._avg_status_lbl.config(
                            text=f"Computing {p}/{n_events}…", foreground="#6b7280"))

            result = {}
            if count > 0:
                for ch in self._channels:
                    result[ch] = np.sqrt(power_acc[ch] / count)   # RMS amplitude [mV]

            def finish():
                self._avg_computing = False
                self._avg_compute_btn.config(state="normal")
                if count == 0:
                    self._avg_status_lbl.config(text="No events read", foreground="#dc3545")
                    return
                self._avg_result = result
                self._avg_status_lbl.config(
                    text=f"Done — {count} events averaged", foreground="#16a34a")
                if self._tree:
                    self._show_entry(self._cur_entry)

            self._avg_status_lbl.master.after(0, finish)

        threading.Thread(target=compute, daemon=True).start()

    # ══════════════════════════════════════════════════════════════════════
    # Entry rendering
    # ══════════════════════════════════════════════════════════════════════

    def _show_entry(self, n: int):
        n = max(0, min(n, self._n_entries - 1))
        self._cur_entry = n
        self._entry_var.set(str(n))
        try:
            raw = self._tree["ADC"].array(
                entry_start=n, entry_stop=n + 1, library="np")
            adc_flat = np.asarray(raw[0]).astype(np.float64).ravel()
        except Exception:
            try:
                import awkward as ak
                raw = self._tree["ADC"].array(
                    entry_start=n, entry_stop=n + 1, library="ak")
                adc_flat = np.asarray(ak.to_numpy(raw[0])).astype(np.float64).ravel()
            except Exception as exc2:
                self._status.set(f"Read error: {exc2}")
                return
        try:
            self._render(adc_flat, n)
        except Exception as exc:
            import traceback
            self._status.set(f"Render error: {exc}")
            traceback.print_exc()

    def _render(self, adc_flat: np.ndarray, entry: int):
        if self._fft_mode:
            if self._fft_avg_mode.get():
                # Average mode: show cached result if available; else placeholder
                if self._avg_result is not None:
                    self._render_fft_avg(self._avg_result)
                else:
                    self._render_fft_avg_placeholder()
            else:
                self._render_fft(adc_flat, entry)
            return
        n_ch = len(self._channels)
        if n_ch == 0:
            return

        # 2-column grid → roughly 4:3 per panel in a landscape canvas
        n_cols = min(2, n_ch)
        n_rows = (n_ch + n_cols - 1) // n_cols

        self._fig.clear()
        self._fig.set_facecolor(BG)

        samples = np.arange(self._n_samples)
        TICK_C  = "#6b7280"
        LABEL_C = "#374151"

        for idx, ch in enumerate(self._channels):
            slot = idx
            if slot >= self._n_slots:
                break
            try:
                y_lo = float(self._slot_y_lo[slot].get())
            except (tk.TclError, ValueError):
                y_lo = 5.0
            try:
                y_hi = float(self._slot_y_hi[slot].get())
            except (tk.TclError, ValueError):
                y_hi = 5.0
            x_s_str = self._slot_x_s[slot].get().strip()
            x_e_str = self._slot_x_e[slot].get().strip()
            x_s = int(x_s_str) if x_s_str else 0
            x_e = int(x_e_str) if x_e_str else self._n_samples

            ax = self._fig.add_subplot(n_rows, n_cols, idx + 1)
            ax.set_facecolor(BG)

            seg     = adc_flat[ch * self._n_samples: (ch + 1) * self._n_samples]
            voltage = seg * ADC_TO_MV
            ped_adc = _pedestal(seg)
            ped_mv  = ped_adc * ADC_TO_MV

            if ch == TRIGGER_CH:
                ax.set_title(f"CH{ch}  #{entry}  │  Trigger",
                             color=LABEL_C, fontsize=9, fontweight="bold", pad=4)
                ax.set_ylim(ped_mv - 100, ped_mv + 200)
            else:
                q = _charge_pC(seg, ped_adc)
                ax.set_title(f"CH{ch}  #{entry}  │  {q:+.3f} pC",
                             color=LABEL_C, fontsize=9, fontweight="bold", pad=4)
                ax.axvspan(PED_START, PED_END, alpha=0.35, color=PED_SPAN_C, lw=0)
                ax.axvspan(SIG_START, SIG_END, alpha=0.35, color=SIG_SPAN_C, lw=0)
                ax.set_ylim(ped_mv - y_lo, ped_mv + y_hi)

            ax.plot(samples, voltage, color=WAVE, linewidth=0.7)
            ax.axhline(ped_mv, color=PED_C, linewidth=1.0, linestyle="--", alpha=0.7)
            ax.set_xlim(x_s, x_e)
            ax.tick_params(axis="both", colors=TICK_C, labelsize=8)
            for sp in ax.spines.values():
                sp.set_edgecolor(SPINE_C)
            ax.set_xlabel("Sample  [2 ns/sample]", color=TICK_C, fontsize=8)
            ax.set_ylabel("Voltage [mV]", color=TICK_C, fontsize=8)
            ax.grid(True, color=GRID_C, linewidth=0.5, linestyle="-")

        try:
            self._fig.tight_layout(pad=1.5)
        except Exception:
            pass
        self._canvas.draw()
        self._canvas.get_tk_widget().update_idletasks()

    def _render_fft(self, adc_flat: np.ndarray, entry: int):
        """Render FFT spectrum for each channel (500 MHz sampling → 0–250 MHz)."""
        n_ch = len(self._channels)
        if n_ch == 0:
            return

        DT      = 2e-9          # 2 ns per sample → 500 MHz sampling
        FS_MHZ  = 1.0 / DT / 1e6   # 500 MHz
        n       = self._n_samples
        freqs   = np.fft.rfftfreq(n, d=DT) / 1e6   # MHz

        n_cols  = min(2, n_ch)
        n_rows  = (n_ch + n_cols - 1) // n_cols

        self._fig.clear()
        self._fig.set_facecolor(BG)

        TICK_C  = "#6b7280"
        LABEL_C = "#374151"
        FFT_C   = "#7c3aed"   # purple for FFT

        window = np.hanning(n)   # Hann window — reduces spectral leakage

        for idx, ch in enumerate(self._channels):
            slot = idx
            if slot >= self._n_slots:
                break

            seg    = adc_flat[ch * n: (ch + 1) * n]
            ped    = float(np.mean(seg[PED_START:PED_END]))
            sig_mv = (seg - ped) * ADC_TO_MV  # subtract DC pedestal

            # FFT magnitude spectrum (one-sided, peak-amplitude normalised)
            fft_raw = np.fft.rfft(sig_mv * window)
            mag     = np.abs(fft_raw) * 2.0 / n   # mV amplitude

            # Peak frequency (skip bin 0 = DC)
            peak_bin  = int(np.argmax(mag[1:]) + 1)
            peak_freq = freqs[peak_bin]
            peak_amp  = mag[peak_bin]

            # RMS noise floor (median of spectrum, robust estimate)
            noise_floor = float(np.median(mag[1:]))

            ax = self._fig.add_subplot(n_rows, n_cols, idx + 1)
            ax.set_facecolor(BG)

            ax.plot(freqs, mag, color=FFT_C, linewidth=0.8)
            ax.axvline(peak_freq, color="#dc2626", linewidth=1.0,
                       linestyle="--", alpha=0.8, label=f"peak {peak_freq:.1f} MHz")
            ax.axhline(noise_floor, color="#9ca3af", linewidth=0.8,
                       linestyle=":", alpha=0.7, label=f"noise ~{noise_floor*1e3:.1f} uV")

            # Annotate peak
            ax.annotate(f"{peak_freq:.1f} MHz\n{peak_amp*1e3:.2f} uV",
                        xy=(peak_freq, peak_amp),
                        xytext=(peak_freq + FS_MHZ * 0.04, peak_amp * 0.85),
                        fontsize=7, color="#dc2626",
                        arrowprops=dict(arrowstyle="-", color="#dc2626", lw=0.8))

            title_ch = "Trigger" if ch == TRIGGER_CH else f"{peak_freq:.1f} MHz peak"
            ax.set_title(f"CH{ch}  #{entry}  FFT  │  {title_ch}",
                         color=LABEL_C, fontsize=9, fontweight="bold", pad=4)

            ax.set_xlim(0, FS_MHZ / 2)
            ax.set_xlabel("Frequency [MHz]", color=TICK_C, fontsize=8)
            ax.set_ylabel("Amplitude [mV]", color=TICK_C, fontsize=8)
            ax.tick_params(axis="both", colors=TICK_C, labelsize=8)
            for sp in ax.spines.values():
                sp.set_edgecolor(SPINE_C)
            ax.grid(True, color=GRID_C, linewidth=0.5)
            ax.legend(fontsize=7, framealpha=0.7, loc="upper right")

        try:
            self._fig.tight_layout(pad=1.5)
        except Exception:
            pass
        self._canvas.draw()
        self._canvas.get_tk_widget().update_idletasks()

    def _render_fft_avg_placeholder(self):
        """Show a message prompting the user to click Compute."""
        self._fig.clear()
        self._fig.set_facecolor("#f9fafb")
        ax = self._fig.add_subplot(1, 1, 1)
        ax.set_facecolor("#f9fafb")
        ax.text(0.5, 0.5,
                "Click  'Compute Avg FFT'  to average the selected event range",
                ha="center", va="center", fontsize=12, color="#6b7280",
                transform=ax.transAxes)
        ax.axis("off")
        self._canvas.draw()

    def _render_fft_avg(self, result: dict):
        """Render the pre-computed RMS-average FFT spectrum for each channel."""
        n_ch = len(self._channels)
        if n_ch == 0:
            return

        DT     = 2e-9
        FS_MHZ = 1.0 / DT / 1e6   # 500 MHz
        n      = self._n_samples
        freqs  = np.fft.rfftfreq(n, d=DT) / 1e6   # MHz

        n_cols = min(2, n_ch)
        n_rows = (n_ch + n_cols - 1) // n_cols

        self._fig.clear()
        self._fig.set_facecolor(BG)

        TICK_C  = "#6b7280"
        LABEL_C = "#374151"
        AVG_C   = "#0891b2"   # cyan-blue for averaged spectrum

        for idx, ch in enumerate(self._channels):
            if idx >= self._n_slots:
                break
            mag = result.get(ch)
            if mag is None:
                continue

            noise_floor = float(np.median(mag[1:]))
            peak_bin    = int(np.argmax(mag[1:]) + 1)
            peak_freq   = freqs[peak_bin]
            peak_amp    = mag[peak_bin]

            ax = self._fig.add_subplot(n_rows, n_cols, idx + 1)
            ax.set_facecolor(BG)

            ax.plot(freqs, mag, color=AVG_C, linewidth=0.9)
            ax.axvline(peak_freq, color="#dc2626", linewidth=1.0,
                       linestyle="--", alpha=0.8, label=f"peak {peak_freq:.1f} MHz")
            ax.axhline(noise_floor, color="#9ca3af", linewidth=0.8,
                       linestyle=":", alpha=0.7, label=f"noise ~{noise_floor*1e3:.1f} uV")

            ax.annotate(f"{peak_freq:.1f} MHz\n{peak_amp*1e3:.2f} uV",
                        xy=(peak_freq, peak_amp),
                        xytext=(peak_freq + FS_MHZ * 0.04, peak_amp * 0.85),
                        fontsize=7, color="#dc2626",
                        arrowprops=dict(arrowstyle="-", color="#dc2626", lw=0.8))

            label = "Trigger" if ch == TRIGGER_CH else f"{peak_freq:.1f} MHz peak"
            n_ev = self._avg_to.get() + "–" + self._avg_from.get()
            ax.set_title(f"CH{ch}  AVG FFT  │  {label}",
                         color=LABEL_C, fontsize=9, fontweight="bold", pad=4)

            ax.set_xlim(0, FS_MHZ / 2)
            ax.set_xlabel("Frequency [MHz]", color=TICK_C, fontsize=8)
            ax.set_ylabel("RMS Amplitude [mV]", color=TICK_C, fontsize=8)
            ax.tick_params(axis="both", colors=TICK_C, labelsize=8)
            for sp in ax.spines.values():
                sp.set_edgecolor(SPINE_C)
            ax.grid(True, color=GRID_C, linewidth=0.5)
            ax.legend(fontsize=7, framealpha=0.7, loc="upper right")

        try:
            self._fig.tight_layout(pad=1.5)
        except Exception:
            pass
        self._canvas.draw()
        self._canvas.get_tk_widget().update_idletasks()

    def _build_slot_controls(self, n_slots: int):
        """Rebuild the per-channel axis control grid for n_slots channels."""
        self._n_slots = n_slots
        # Destroy old widgets
        for w in self._ch_grid.winfo_children():
            w.destroy()
        n_cols = 2 if n_slots > 1 else 1
        for slot in range(n_slots):
            ri = slot // n_cols
            ci = slot % n_cols
            sf = ttk.Frame(self._ch_grid, padding=(0, 1, 12, 1))
            sf.grid(row=ri, column=ci, sticky="w")

            ttk.Label(sf, textvariable=self._slot_lbl_var[slot],
                      font=("Helvetica", 9, "bold"),
                      foreground="#1d4ed8", width=4).pack(side=tk.LEFT)
            ttk.Label(sf, text="Y –", font=("Helvetica", 9)).pack(side=tk.LEFT)
            ttk.Entry(sf, textvariable=self._slot_y_lo[slot],
                      width=5, justify="center").pack(side=tk.LEFT, padx=1)
            ttk.Label(sf, text="/+", font=("Helvetica", 9)).pack(side=tk.LEFT)
            ttk.Entry(sf, textvariable=self._slot_y_hi[slot],
                      width=5, justify="center").pack(side=tk.LEFT, padx=(1, 4))
            ttk.Label(sf, text="mV  X", font=("Helvetica", 9)).pack(side=tk.LEFT)
            ttk.Entry(sf, textvariable=self._slot_x_s[slot],
                      width=5, justify="center").pack(side=tk.LEFT, padx=(2, 1))
            ttk.Label(sf, text="–", font=("Helvetica", 9)).pack(side=tk.LEFT)
            ttk.Entry(sf, textvariable=self._slot_x_e[slot],
                      width=5, justify="center").pack(side=tk.LEFT, padx=1)

    def _clear(self):
        """Reset panel to the initial guide screen without losing nothing on disk."""
        self._tree      = None
        self._n_entries = 0
        self._n_samples = 0
        self._channels  = []
        self._cur_entry = 0
        self._file_lbl.set("—")
        self._entry_var.set("0")
        self._total_lbl.config(text="/ —")
        self._status.set("Load a RAW .root file to begin.")
        for slot in range(4):
            self._slot_lbl_var[slot].set(f"CH{slot}")
        self._draw_placeholder()

    def _show_guide(self):
        """Show usage guide as a popup (keeps current file loaded)."""
        msg = (
            "Waveform Inspection — Quick Guide\n"
            "─────────────────────────────────\n"
            "① Select a RAW .root file in the  Data Files  tab\n"
            "② Click  ⟳ Use selected file  (or  🔬 Waveform Inspection  button)\n\n"
            "Navigation\n"
            "  ⏮ / ⏭   First / Last event\n"
            "  ◀ / ▶    Prev / Next event\n"
            "  ← / →    Keyboard shortcut (left/right arrow)\n"
            "  Entry box   Type number + Enter to jump directly\n\n"
            "Axis controls  (bottom bar)\n"
            "  Y –/+    Lower / upper mV range around pedestal  (per channel)\n"
            "  X        Sample range  (blank = full 0–1024)\n"
            "  ⊙ Ped Center   Auto-fit Y to current event's actual signal spread\n"
            "  Apply    Apply manual changes\n\n"
            "Charge search\n"
            "  Jump: charge <  [threshold] pC  →  🔍 Search\n"
            "  Searches forward from current event; ⏹ Cancel anytime\n\n"
            "✕ Clear   Returns to this guide screen (file stays on disk)"
        )
        messagebox.showinfo("Waveform Inspection Guide", msg)

    def _ped_center(self):
        """Auto-fit Y range for each channel: compute actual signal spread around
        the pedestal in the current entry and update per-slot y_lo / y_hi."""
        if self._tree is None:
            return
        try:
            raw = self._tree["ADC"].array(
                entry_start=self._cur_entry,
                entry_stop=self._cur_entry + 1, library="np")
            adc_flat = np.asarray(raw[0]).astype(np.float64).ravel()
        except Exception:
            try:
                import awkward as ak
                raw = self._tree["ADC"].array(
                    entry_start=self._cur_entry,
                    entry_stop=self._cur_entry + 1, library="ak")
                adc_flat = np.asarray(ak.to_numpy(raw[0])).astype(np.float64).ravel()
            except Exception:
                return

        for idx, ch in enumerate(self._channels[:self._n_slots]):
            if ch == TRIGGER_CH:
                continue
            seg     = adc_flat[ch * self._n_samples: (ch + 1) * self._n_samples]
            voltage = seg * ADC_TO_MV
            ped_mv  = float(np.mean(seg[PED_START:PED_END])) * ADC_TO_MV
            v_min   = float(voltage.min())
            v_max   = float(voltage.max())
            pad     = max(0.5, (v_max - v_min) * 0.15)  # 15% padding
            lo = max(0.5, round(ped_mv - v_min + pad, 1))
            hi = max(0.5, round(v_max - ped_mv + pad, 1))
            self._slot_y_lo[idx].set(lo)
            self._slot_y_hi[idx].set(hi)

        self._redraw()

    # ══════════════════════════════════════════════════════════════════════
    # Charge search (background thread)
    # ══════════════════════════════════════════════════════════════════════

    def _start_search(self):
        if self._tree is None:
            messagebox.showwarning("No File", "Load a file first.")
            return
        try:
            thresh = float(self._thresh.get())
        except ValueError:
            messagebox.showwarning("Invalid", "Enter a numeric pC threshold.")
            return

        # Cancel any in-progress search
        self._search_id = object()
        token = self._search_id

        self._search_btn.config(text="⏹ Cancel", command=self._cancel_search,
                                bg="#dc3545")
        self._status.set(f"Searching for charge < {thresh} pC  (from entry {self._cur_entry + 1})…")

        start = self._cur_entry + 1

        def worker():
            found = -1
            BATCH = 1000
            try:
                for b_start in range(start, self._n_entries, BATCH):
                    if self._search_id is not token:
                        return  # cancelled
                    b_end = min(b_start + BATCH, self._n_entries)
                    adcs = self._tree["ADC"].array(
                        entry_start=b_start, entry_stop=b_end, library="np")

                    for i, adc_flat in enumerate(adcs):
                        entry = b_start + i
                        for ch in self._channels:
                            if ch == TRIGGER_CH:
                                continue
                            seg = adc_flat[ch * self._n_samples: (ch + 1) * self._n_samples].astype(np.float64)
                            q = _charge_pC(seg, _pedestal(seg))
                            if q < thresh:
                                found = entry
                                break
                        if found >= 0:
                            break
                    if found >= 0:
                        break
                    # Progress update every 10 batches
                    if (b_start // BATCH) % 10 == 0:
                        pct = 100 * b_start / self._n_entries
                        self.parent.after(0, lambda p=pct, b=b_start: self._status.set(
                            f"Searching… {b:,}/{self._n_entries:,}  ({p:.0f}%)"))
            except Exception as exc:
                self.parent.after(0, lambda e=exc: self._status.set(f"Search error: {e}"))
                self.parent.after(0, self._reset_search_btn)
                return

            def on_done():
                self._reset_search_btn()
                if found >= 0:
                    self._status.set(
                        f"Found charge < {thresh} pC at entry #{found}")
                    self._show_entry(found)
                else:
                    self._status.set(f"No more events with charge < {thresh} pC")

            self.parent.after(0, on_done)

        threading.Thread(target=worker, daemon=True).start()

    def _cancel_search(self):
        self._search_id = object()   # invalidate token → worker stops at next batch
        self._status.set("Search cancelled.")
        self._reset_search_btn()

    def _reset_search_btn(self):
        self._search_btn.config(text="🔍 Search", command=self._start_search,
                                bg="#fd7e14")

    # ══════════════════════════════════════════════════════════════════════
    # Public: load a file path directly (called from main.py run_waveform)
    # ══════════════════════════════════════════════════════════════════════

    def open_path(self, path: str):
        self._load_file(path)

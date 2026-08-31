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
SIG_START, SIG_END = 595, 645  # shifted 15 samples earlier total (cable shortened -> signal arrives sooner; matches prod_ntp_v7.C's sigStart)

# Known CAEN V1730 internal clock frequencies (MHz) — appear as coherent spurs
NOISE_FREQS_MHZ = [50.0, 62.5, 100.0, 125.0]
NOISE_FREQ_LABELS = {50.0: "PLL ref", 62.5: "Cntr read", 100.0: "2× PLL", 125.0: "Trig clk"}

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


def _pedestal(adc: np.ndarray, ped_start: int = PED_START, ped_end: int = PED_END) -> float:
    sl = adc[ped_start:ped_end]
    return float(np.mean(sl)) if len(sl) > 0 else 0.0


def _charge_pC(adc: np.ndarray, ped: float,
               sig_start: int = SIG_START, sig_end: int = SIG_END) -> float:
    integral = np.sum(ped - adc[sig_start:sig_end].astype(float))
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
        self._avg_result   = None       # cached {ch: rms_mag} FFT spectrum
        self._avg_wave     = None       # cached {ch: mean_adc} time-domain waveform
        self._avg_computing = False     # guard against concurrent compute
        self._avg_cancel   = False      # set True to abort an in-flight compute
        self._thresh = tk.StringVar(value="-0.5")
        # Charge-search comparison operator. "|q|>" finds events with a real pulse
        # of either polarity (magnitude above threshold) — i.e. "events with signal".
        self._search_op = tk.StringVar(value="<")

        # mV peak search
        self._mv_thresh  = tk.StringVar(value="5.0")
        self._mv_search_ch = tk.IntVar(value=0)   # which channel to search (radio)

        # Noise scan (FFT amplitude at known clock frequencies)
        self._noise_freq_var = tk.StringVar(value="All")
        self._noise_amp_var  = tk.StringVar(value="10")  # uV threshold
        self._noise_scan_id  = None
        self._noise_scan_win = None

        # Drag-zoom state per subplot (axis index → [x0, y0, press_data_coord])
        self._drag_start = None   # (ax, x0_data, y0_data, axis: 'x'|'y'|'xy')

        # User-adjustable pedestal window (defaults mirror Analysis.cpp constants).
        # Changing it moves the dashed average-pedestal line AND recomputes charge,
        # since charge = Σ(pedestal − adc) over the signal window.
        self._ped_start_var = tk.StringVar(value=str(PED_START))
        self._ped_end_var   = tk.StringVar(value=str(PED_END))
        self._sig_start_var = tk.StringVar(value=str(SIG_START))
        self._sig_end_var   = tk.StringVar(value=str(SIG_END))

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
        btn_prev = tk.Button(fb, text="◀", bg="#343a40", fg="white", relief="flat", padx=8)
        btn_prev.pack(side=tk.LEFT, padx=(2, 0))
        self._bind_hold(btn_prev, self._prev)
        e = ttk.Entry(fb, textvariable=self._entry_var, width=8, justify="center",
                      font=("Helvetica", 10, "bold"))
        e.pack(side=tk.LEFT, padx=4)
        e.bind("<Return>", lambda _: self._goto_var())
        self._total_lbl.pack(side=tk.LEFT)
        btn_next = tk.Button(fb, text="▶", bg="#343a40", fg="white", relief="flat", padx=8)
        btn_next.pack(side=tk.LEFT, padx=(4, 0))
        self._bind_hold(btn_next, self._next)
        tk.Button(fb, text="⏭", command=lambda: self._goto(max(0, self._n_entries - 1)),
                  bg="#343a40", fg="white", relief="flat", padx=6).pack(side=tk.LEFT, padx=(2, 0))

        ttk.Separator(fb, orient="vertical").pack(side=tk.LEFT, fill=tk.Y, padx=10)

        # Charge search — operator selectable so you can find signal events of
        # either polarity ( |q|> ), not only charge-below-threshold ( < ).
        ttk.Label(fb, text="Jump: charge", font=("Helvetica", 9)).pack(side=tk.LEFT)
        op_combo = ttk.Combobox(fb, textvariable=self._search_op, width=5, state="readonly",
                                values=["<", ">", "|q|>"], justify="center")
        op_combo.pack(side=tk.LEFT, padx=2)
        ttk.Entry(fb, textvariable=self._thresh, width=7, justify="center").pack(side=tk.LEFT, padx=2)
        ttk.Label(fb, text="pC", font=("Helvetica", 9)).pack(side=tk.LEFT)
        self._search_btn = tk.Button(fb, text="🔍 Search", command=self._start_search,
                                     bg="#fd7e14", fg="white",
                                     font=("Helvetica", 9, "bold"), relief="flat", padx=8)
        self._search_btn.pack(side=tk.LEFT, padx=(4, 0))

        # mV search widgets are placed in Row 1 (fft_ctrl_frame) to avoid overflow

        # ── Row 1: averaging controls (always visible; apply to BOTH the
        #    time-domain waveform and the FFT spectrum) ────────────────────
        self._fft_ctrl_frame = ttk.Frame(parent, padding=(8, 4, 8, 4))
        self._fft_ctrl_frame.grid(row=1, column=0, sticky="ew")

        # Two stacked rows so the controls never overflow horizontally:
        #   top → display / averaging   |   bottom → jump & scan searches
        ctrl_top = ttk.Frame(self._fft_ctrl_frame)
        ctrl_top.pack(side=tk.TOP, fill=tk.X)
        ctrl_bot = ttk.Frame(self._fft_ctrl_frame)
        ctrl_bot.pack(side=tk.TOP, fill=tk.X, pady=(4, 0))

        ttk.Label(ctrl_top, text="Display:",
                  font=("Helvetica", 9, "bold")).pack(side=tk.LEFT)
        ttk.Radiobutton(ctrl_top, text="Single event",
                        variable=self._fft_avg_mode, value=False,
                        command=self._on_fft_mode_change).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Radiobutton(ctrl_top, text="Average",
                        variable=self._fft_avg_mode, value=True,
                        command=self._on_fft_mode_change).pack(side=tk.LEFT, padx=(6, 0))

        ttk.Separator(ctrl_top, orient="vertical").pack(side=tk.LEFT, fill=tk.Y, padx=10)

        ttk.Label(ctrl_top, text="Event range  from:",
                  font=("Helvetica", 9)).pack(side=tk.LEFT)
        self._avg_from_entry = ttk.Entry(ctrl_top, textvariable=self._avg_from,
                                         width=7, justify="center")
        self._avg_from_entry.pack(side=tk.LEFT, padx=3)
        ttk.Label(ctrl_top, text="to:",
                  font=("Helvetica", 9)).pack(side=tk.LEFT)
        self._avg_to_entry = ttk.Entry(ctrl_top, textvariable=self._avg_to,
                                       width=7, justify="center")
        self._avg_to_entry.pack(side=tk.LEFT, padx=3)

        # Quick presets — save typing for common ranges.
        tk.Button(ctrl_top, text="All", command=self._avg_range_all,
                  bg="#e5e7eb", fg="#374151", font=("Helvetica", 8, "bold"),
                  relief="flat", padx=6).pack(side=tk.LEFT, padx=(4, 0))
        tk.Button(ctrl_top, text="1k", command=lambda: self._avg_range_quick(1000),
                  bg="#e5e7eb", fg="#374151", font=("Helvetica", 8, "bold"),
                  relief="flat", padx=6).pack(side=tk.LEFT, padx=(2, 0))
        tk.Button(ctrl_top, text="10k", command=lambda: self._avg_range_quick(10000),
                  bg="#e5e7eb", fg="#374151", font=("Helvetica", 8, "bold"),
                  relief="flat", padx=6).pack(side=tk.LEFT, padx=(2, 0))

        self._avg_compute_btn = tk.Button(
            ctrl_top, text="Compute Average",
            command=self._start_avg_fft,
            bg="#7c3aed", fg="white", font=("Helvetica", 9, "bold"),
            relief="flat", padx=10)
        self._avg_compute_btn.pack(side=tk.LEFT, padx=(8, 0))

        self._avg_status_lbl = ttk.Label(ctrl_top, text="",
                                         font=("Helvetica", 9), foreground="#6b7280")
        self._avg_status_lbl.pack(side=tk.LEFT, padx=(8, 0))

        # mV peak search — bottom row
        ttk.Label(ctrl_bot, text="Jump: peak >", font=("Helvetica", 9)).pack(side=tk.LEFT)
        ttk.Entry(ctrl_bot, textvariable=self._mv_thresh,
                  width=6, justify="center").pack(side=tk.LEFT, padx=2)
        ttk.Label(ctrl_bot, text="mV  ch:", font=("Helvetica", 9)).pack(side=tk.LEFT, padx=(2, 0))
        self._mv_ch_frame = ttk.Frame(ctrl_bot)
        self._mv_ch_frame.pack(side=tk.LEFT, padx=2)
        self._mv_search_btn = tk.Button(ctrl_bot, text="🔍 Find",
                                        command=self._start_mv_search,
                                        bg="#0891b2", fg="white",
                                        font=("Helvetica", 9, "bold"), relief="flat", padx=8)
        self._mv_search_btn.pack(side=tk.LEFT, padx=(4, 0))

        # Noise scan — scan all events by FFT amplitude at CAEN clock frequencies
        ttk.Separator(ctrl_bot, orient="vertical").pack(side=tk.LEFT, fill=tk.Y, padx=10)
        ttk.Label(ctrl_bot, text="Noise scan:", font=("Helvetica", 9)).pack(side=tk.LEFT)
        self._noise_freq_combo = ttk.Combobox(
            ctrl_bot, textvariable=self._noise_freq_var, width=6, state="readonly",
            values=["All"] + [str(f) for f in NOISE_FREQS_MHZ])
        self._noise_freq_combo.pack(side=tk.LEFT, padx=(4, 2))
        ttk.Label(ctrl_bot, text="MHz >", font=("Helvetica", 9)).pack(side=tk.LEFT)
        ttk.Entry(ctrl_bot, textvariable=self._noise_amp_var,
                  width=5, justify="center").pack(side=tk.LEFT, padx=2)
        ttk.Label(ctrl_bot, text="uV", font=("Helvetica", 9)).pack(side=tk.LEFT)
        self._noise_scan_btn = tk.Button(
            ctrl_bot, text="📡 Scan",
            command=self._start_noise_scan,
            bg="#065f46", fg="white", font=("Helvetica", 9, "bold"), relief="flat", padx=8)
        self._noise_scan_btn.pack(side=tk.LEFT, padx=(4, 0))

        # ── Row 2: per-channel axis bar ───────────────────────────────────
        ab = ttk.Frame(parent, padding=(8, 2, 8, 4))
        ab.grid(row=2, column=0, sticky="ew")
        ab.columnconfigure(0, weight=1)

        # Button strip (right) — single row to avoid vertical clipping
        btn_strip = ttk.Frame(ab)
        btn_strip.grid(row=0, column=1, rowspan=2, sticky="ne", padx=(10, 0))

        # Pedestal-window controls (global; drives ped line + charge)
        ttk.Label(btn_strip, text="Pedestal range:", font=("Helvetica", 9, "bold")).pack(side=tk.LEFT)
        ped_s_entry = ttk.Entry(btn_strip, textvariable=self._ped_start_var,
                                width=6, justify="center")
        ped_s_entry.pack(side=tk.LEFT, padx=(4, 1))
        ttk.Label(btn_strip, text="–", font=("Helvetica", 9)).pack(side=tk.LEFT)
        ped_e_entry = ttk.Entry(btn_strip, textvariable=self._ped_end_var,
                                width=6, justify="center")
        ped_e_entry.pack(side=tk.LEFT, padx=(1, 4))
        ped_s_entry.bind("<Return>", lambda _: self._redraw())
        ped_e_entry.bind("<Return>", lambda _: self._redraw())
        tk.Button(btn_strip, text="↺", command=self._reset_ped_range,
                  bg="#e5e7eb", fg="#374151", font=("Helvetica", 9, "bold"),
                  relief="flat", padx=5).pack(side=tk.LEFT, padx=(0, 10))

        ttk.Label(btn_strip, text="Signal range:", font=("Helvetica", 9, "bold")).pack(side=tk.LEFT)
        sig_s_entry = ttk.Entry(btn_strip, textvariable=self._sig_start_var,
                                width=6, justify="center")
        sig_s_entry.pack(side=tk.LEFT, padx=(4, 1))
        ttk.Label(btn_strip, text="–", font=("Helvetica", 9)).pack(side=tk.LEFT)
        sig_e_entry = ttk.Entry(btn_strip, textvariable=self._sig_end_var,
                                width=6, justify="center")
        sig_e_entry.pack(side=tk.LEFT, padx=(1, 4))
        sig_s_entry.bind("<Return>", lambda _: self._redraw())
        sig_e_entry.bind("<Return>", lambda _: self._redraw())
        tk.Button(btn_strip, text="↺", command=self._reset_sig_range,
                  bg="#e5e7eb", fg="#374151", font=("Helvetica", 9, "bold"),
                  relief="flat", padx=5).pack(side=tk.LEFT, padx=(0, 10))

        tk.Button(btn_strip, text="⊙ Ped Center", command=self._ped_center,
                  bg="#4f46e5", fg="white", font=("Helvetica", 9, "bold"),
                  relief="flat", padx=8).pack(side=tk.LEFT, padx=(0, 4))

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

        # Drag-zoom with rubber-band feedback; right-click to unzoom
        self._drag_rect_patch = None   # live Rectangle overlay during drag
        self._canvas.mpl_connect("button_press_event",   self._drag_press)
        self._canvas.mpl_connect("motion_notify_event",  self._drag_motion)
        self._canvas.mpl_connect("button_release_event", self._drag_release)

        # Keyboard nav (only when this panel's parent is focused)
        parent.bind("<Left>",  lambda _: self._prev())
        parent.bind("<Right>", lambda _: self._next())

        self._update_avg_btn_label()   # "Compute Avg Waveform" (FFT off by default)
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
                keys = [k.split(";")[0] for k in tree.keys()]

                # RecordLength / PostTrigger / OffsetValue<N> are constant for a
                # whole run, so from ADC_test8 (2026-08-08) they are written ONCE
                # into the RunInfo tree instead of being repeated across all
                # ~300k entries of "T". Files taken before that still carry them
                # per-event on "T", so read from whichever tree actually has
                # them -- both formats must stay readable. This mirrors
                # prod_ntp_v7.C's `constsOnT` handling; the viewer was left on
                # the old layout and so could not open any file recorded after
                # the change (2026-08-26).
                info = None
                for info_name in ("RunInfo", "InfoTree"):
                    try:
                        info = f[info_name]
                        break
                    except Exception:
                        continue

                def _const(name, default):
                    """One per-run constant, from T if present, else RunInfo."""
                    if name in keys:
                        return int(tree[name].array(
                            entry_start=0, entry_stop=1, library="np")[0])
                    if info is not None:
                        try:
                            return int(info[name].array(library="np")[0])
                        except Exception:
                            pass
                    return default

                rec_len = _const("RecordLength", 1024)
                post_trigger = _const("PostTrigger", 60)

                # Which channels were enabled. Old files: one OffsetValue<N>
                # branch per active channel on "T". New files: those branches
                # moved to RunInfo, which also stores the digitizer channel mask
                # ("00001111", written MSB-first so the leftmost char is ch7) --
                # preferred, since it states what was enabled outright instead
                # of inferring it from which branches happen to exist.
                channels = [i for i in range(8) if f"OffsetValue{i}" in keys]
                if not channels and info is not None:
                    info_keys = [k.split(";")[0] for k in info.keys()]
                    if "ChannelMask" in info_keys:
                        try:
                            raw_mask = info["ChannelMask"].array(library="np")[0]
                            if isinstance(raw_mask, bytes):
                                raw_mask = raw_mask.decode("ascii", "ignore")
                            mask = str(raw_mask).strip()
                            channels = [i for i in range(min(len(mask), 8))
                                        if mask[len(mask) - 1 - i] == "1"]
                        except Exception:
                            channels = []
                    if not channels:          # fall back to the moved branches
                        channels = [i for i in range(8)
                                    if f"OffsetValue{i}" in info_keys]

                # Detect dark mode (same logic as prod_ntp_v7.C)
                dark_mode = False
                if info is not None:
                    try:
                        run_mode = info["RunMode"].array(library="np")[0]
                        if isinstance(run_mode, bytes):
                            run_mode = run_mode.decode("ascii", "ignore")
                        if "dark" in str(run_mode).strip().lower():
                            dark_mode = True
                    except Exception:
                        pass

                trg_point = int(rec_len * (1.0 - post_trigger / 100.0))
                sig_s = trg_point if dark_mode else SIG_START
                sig_e = min(sig_s + 50, rec_len)

                self.parent.after(0, lambda: self._on_loaded(
                    path, tree, n, rec_len, channels, sig_s, sig_e))
            except Exception as exc:
                self.parent.after(0, lambda e=exc: self._status.set(f"Error: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def _on_loaded(self, path, tree, n_entries, n_samples, channels, sig_s=SIG_START, sig_e=SIG_END):
        import os
        self._tree = tree
        self._n_entries = n_entries
        self._n_samples = n_samples
        self._channels  = channels
        self._cur_entry = 0
        self._sig_start_var.set(str(sig_s))
        self._sig_end_var.set(str(sig_e))
        self._file_lbl.set(os.path.basename(path))
        self._total_lbl.config(text=f"/ {n_entries - 1}")
        self._status.set(
            f"{n_entries:,} entries  •  {len(channels)} ch  •  {n_samples} samples/ch"
            f"  •  sig [{sig_s}–{sig_e}]")
        # Clear cached averages — they are keyed to the old file's sample count
        self._avg_result  = None
        self._avg_wave    = None
        self._avg_cancel  = False
        self._avg_computing = False

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
        self._rebuild_mv_ch_radios(channels)
        self._show_entry(0)

    # ══════════════════════════════════════════════════════════════════════
    # Navigation
    # ══════════════════════════════════════════════════════════════════════

    def _next(self):
        if self._tree: self._show_entry(self._cur_entry + 1)

    def _prev(self):
        if self._tree: self._show_entry(self._cur_entry - 1)

    def _bind_hold(self, btn: tk.Button, action, initial_ms: int = 400, repeat_ms: int = 80):
        """Press-and-hold auto-repeat: fires action once on click, then repeatedly after a delay."""
        _job = [None]

        def _cancel():
            if _job[0]:
                btn.after_cancel(_job[0])
                _job[0] = None

        def _repeat():
            action()
            _job[0] = btn.after(repeat_ms, _repeat)

        def _press(_):
            action()
            _cancel()
            _job[0] = btn.after(initial_ms, _repeat)

        def _release(_):
            _cancel()

        btn.bind("<ButtonPress-1>",   _press)
        btn.bind("<ButtonRelease-1>", _release)

    def _rebuild_mv_ch_radios(self, channels):
        """Rebuild mV-search channel radio buttons to match loaded file's channels."""
        for w in self._mv_ch_frame.winfo_children():
            w.destroy()
        pmt_channels = [ch for ch in channels if ch != TRIGGER_CH]
        if not pmt_channels:
            pmt_channels = channels
        if self._mv_search_ch.get() not in pmt_channels:
            self._mv_search_ch.set(pmt_channels[0] if pmt_channels else 0)
        for ch in pmt_channels:
            ttk.Radiobutton(self._mv_ch_frame, text=f"CH{ch}",
                            variable=self._mv_search_ch, value=ch).pack(side=tk.LEFT)

    # ── Drag-zoom (ROOT-style: drag on plot to set axis range) ────────────

    def _drag_press(self, event):
        """Left-drag starts zoom selection; right-click resets axis."""
        if event.inaxes is None:
            return
        ax = event.inaxes

        if event.button == 3:
            # Right-click → unzoom: reset this subplot's axis vars to defaults
            axes = self._fig.get_axes()
            try:
                idx = axes.index(ax)
            except ValueError:
                return
            if not self._fft_mode and idx < len(self._channels):
                self._slot_x_s[idx].set("")
                self._slot_x_e[idx].set("")
                self._slot_y_lo[idx].set(5.0)
                self._slot_y_hi[idx].set(5.0)
                self._redraw()
            elif self._fft_mode:
                DT = 2e-9
                ax.set_xlim(0, 1.0 / DT / 1e6 / 2)
                self._canvas.draw_idle()
            return

        if event.button != 1 or event.xdata is None:
            return
        self._drag_start = (ax, event.xdata, event.ydata)
        # Create rubber-band rectangle overlay
        from matplotlib.patches import Rectangle
        self._drag_rect_patch = Rectangle(
            (event.xdata, event.ydata), 0, 0,
            linewidth=1.2, edgecolor="#2563eb", facecolor="#2563eb",
            alpha=0.15, zorder=10)
        ax.add_patch(self._drag_rect_patch)
        self._canvas.draw_idle()

    def _drag_motion(self, event):
        """Update rubber-band rectangle as mouse moves."""
        if self._drag_start is None or self._drag_rect_patch is None:
            return
        ax0, x0, y0 = self._drag_start
        if event.inaxes is not ax0 or event.xdata is None:
            return
        x1, y1 = event.xdata, event.ydata
        x_lo, x_hi = sorted([x0, x1])
        y_lo, y_hi = sorted([y0, y1])
        self._drag_rect_patch.set_xy((x_lo, y_lo))
        self._drag_rect_patch.set_width(x_hi - x_lo)
        self._drag_rect_patch.set_height(y_hi - y_lo)
        self._canvas.draw_idle()

    def _drag_release(self, event):
        """Apply zoom from drag release position."""
        # Remove rubber band regardless
        if self._drag_rect_patch is not None:
            try:
                self._drag_rect_patch.remove()
            except Exception:
                pass
            self._drag_rect_patch = None

        if self._drag_start is None or event.button != 1:
            self._drag_start = None
            self._canvas.draw_idle()
            return

        ax0, x0, y0 = self._drag_start
        self._drag_start = None

        if event.inaxes is not ax0 or event.xdata is None or event.ydata is None:
            self._canvas.draw_idle()
            return

        x1, y1 = event.xdata, event.ydata
        dx, dy = abs(x1 - x0), abs(y1 - y0)
        if dx < 1e-9 and dy < 1e-9:
            self._canvas.draw_idle()
            return

        x_lo, x_hi = sorted([x0, x1])
        y_lo, y_hi = sorted([y0, y1])

        axes = self._fig.get_axes()
        try:
            idx = axes.index(ax0)
        except ValueError:
            return

        if self._fft_mode:
            if dx > 1e-9:
                ax0.set_xlim(x_lo, x_hi)
                self._canvas.draw_idle()
            return

        if idx < len(self._channels):
            if dx > 1e-9:
                self._slot_x_s[idx].set(str(int(round(x_lo))))
                self._slot_x_e[idx].set(str(int(round(x_hi))))
            if dy > 1e-9 and idx < self._n_slots:
                try:
                    raw = self._tree["ADC"].array(
                        entry_start=self._cur_entry,
                        entry_stop=self._cur_entry + 1, library="np")
                    ch = self._channels[idx]
                    s = np.asarray(raw[0]).astype(np.float64)
                    seg_v = s[ch * self._n_samples:(ch + 1) * self._n_samples]
                    ped_mv = float(np.mean(seg_v[PED_START:PED_END])) * ADC_TO_MV
                except Exception:
                    ped_mv = (y_lo + y_hi) / 2
                lo = round(max(0.1, ped_mv - y_lo), 2)
                hi = round(max(0.1, y_hi - ped_mv), 2)
                self._slot_y_lo[idx].set(lo)
                self._slot_y_hi[idx].set(hi)
            self._redraw()

    def _goto(self, n: int):
        if self._tree: self._show_entry(n)

    def _goto_var(self):
        try:
            self._goto(int(self._entry_var.get()))
        except ValueError:
            pass

    def _redraw(self):
        if self._tree: self._show_entry(self._cur_entry)

    def _get_ped_range(self):
        """Return the current (start, end) pedestal window from the UI, clamped
        to valid sample bounds. Falls back to defaults on invalid input."""
        try:
            ps = int(self._ped_start_var.get())
            pe = int(self._ped_end_var.get())
        except (ValueError, tk.TclError):
            return PED_START, PED_END
        n = self._n_samples or (PED_END + 1)
        ps = max(0, min(ps, n - 1))
        pe = max(0, min(pe, n))
        if pe <= ps:                      # guard against an empty/inverted window
            return PED_START, PED_END
        return ps, pe

    def _reset_ped_range(self):
        """Restore the default pedestal window and redraw."""
        self._ped_start_var.set(str(PED_START))
        self._ped_end_var.set(str(PED_END))
        self._redraw()

    def _get_sig_range(self):
        """Return the current (start, end) signal window from the UI, clamped to valid bounds."""
        try:
            ss = int(self._sig_start_var.get())
            se = int(self._sig_end_var.get())
        except (ValueError, tk.TclError):
            return SIG_START, SIG_END
        n = self._n_samples or (SIG_END + 1)
        ss = max(0, min(ss, n - 1))
        se = max(0, min(se, n))
        if se <= ss:
            return SIG_START, SIG_END
        return ss, se

    def _reset_sig_range(self):
        """Restore the default signal window and redraw."""
        self._sig_start_var.set(str(SIG_START))
        self._sig_end_var.set(str(SIG_END))
        self._redraw()

    def _toggle_fft(self):
        # FFT and Single/Average are independent: the averaging row stays visible.
        self._fft_mode = not self._fft_mode
        if self._fft_mode:
            self._fft_btn.config(bg="#4f46e5", fg="white")
        else:
            self._fft_btn.config(bg="#e5e7eb", fg="#374151")
        self._update_avg_btn_label()
        if self._tree:
            self._show_entry(self._cur_entry)

    def _update_avg_btn_label(self):
        """Compute button label reflects what 'Average' will produce in this view."""
        if self._avg_computing:
            return
        self._avg_compute_btn.config(
            text="Compute Avg FFT" if self._fft_mode else "Compute Avg Waveform")

    def _on_fft_mode_change(self):
        """Called when Single ↔ Average radio is toggled."""
        if self._tree:
            self._show_entry(self._cur_entry)

    def _avg_range_all(self):
        """Quick preset: average over the entire loaded file."""
        if self._avg_computing:
            return
        self._avg_from.set("0")
        self._avg_to.set(str(self._n_entries))

    def _avg_range_quick(self, n: int):
        """Quick preset: average the first N events from the current 'from'."""
        if self._avg_computing:
            return
        try:
            start = int(self._avg_from.get())
        except ValueError:
            start = 0
        self._avg_to.set(str(min(start + n, self._n_entries)))

    def _start_avg_fft(self):
        """Validate range and start (or cancel) background average-FFT computation.

        The Compute button doubles as a Cancel button while a job runs.
        """
        # If a job is already running, this click means "Cancel".
        if self._avg_computing:
            self._avg_cancel = True
            self._avg_status_lbl.config(text="Cancelling…", foreground="#f0ad4e")
            return

        if not self._tree:
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

        # Computing an average implies the user wants to SEE the average → switch on.
        self._fft_avg_mode.set(True)

        self._avg_computing = True
        self._avg_cancel    = False
        # Re-purpose the button as a Cancel control while the job runs.
        self._avg_compute_btn.config(text="✕ Cancel", bg="#dc3545")
        self._avg_from_entry.config(state="disabled")
        self._avg_to_entry.config(state="disabled")
        self._avg_status_lbl.config(text=f"Computing 0/{n_events}…", foreground="#6b7280")
        self._avg_result = None
        self._avg_wave   = None

        ped_start, ped_end = self._get_ped_range()
        BATCH = 2000   # events per uproot read — batching is the main speedup

        def compute():
            # One pass produces BOTH outputs:
            #   • result[ch]  = RMS-average |FFT| spectrum   (for FFT view)
            #   • wave[ch]    = mean raw ADC waveform        (for time-domain view)
            result = {}
            wave   = {}
            count  = 0
            err    = None
            cancelled = False
            try:
                n      = self._n_samples
                window = np.hanning(n).astype(np.float64)   # shape (n,)
                power_acc = {ch: np.zeros(n // 2 + 1, dtype=np.float64)
                             for ch in self._channels}
                adc_acc   = {ch: np.zeros(n, dtype=np.float64)
                             for ch in self._channels}

                ev = ev_from
                while ev < ev_to:
                    if self._avg_cancel:
                        cancelled = True
                        break
                    stop = min(ev + BATCH, ev_to)
                    try:
                        # Read a whole block at once → (n_block, n_ch*n_samples).
                        # One uproot call per BATCH events instead of per event:
                        # ~100× fewer I/O round-trips, the dominant cost.
                        block = self._tree["ADC"].array(
                            entry_start=ev, entry_stop=stop, library="np")
                        block = np.asarray(block, dtype=np.float64)
                    except Exception:
                        ev = stop
                        continue

                    n_block = block.shape[0]
                    # Reshape flat (n_block, n_ch*n) → (n_block, n_ch, n)
                    block = block.reshape(n_block, -1, n)

                    for ch in self._channels:
                        seg    = block[:, ch, :]                       # (n_block, n)
                        adc_acc[ch] += seg.sum(axis=0)                 # mean waveform later
                        ped    = seg[:, ped_start:ped_end].mean(axis=1, keepdims=True)
                        sig_mv = (seg - ped) * ADC_TO_MV
                        # Batched real FFT along the sample axis → (n_block, n//2+1)
                        mag    = np.abs(np.fft.rfft(sig_mv * window, axis=1)) * 2.0 / n
                        power_acc[ch] += (mag ** 2).sum(axis=0)

                    count += n_block
                    ev = stop
                    p = count
                    self._avg_status_lbl.master.after(
                        0, lambda p=p: self._avg_status_lbl.config(
                            text=f"Computing {p:,}/{n_events:,} "
                                 f"({100*p//n_events}%)…", foreground="#6b7280"))

                if count > 0:
                    for ch in self._channels:
                        result[ch] = np.sqrt(power_acc[ch] / count)   # RMS amplitude [mV]
                        wave[ch]   = adc_acc[ch] / count              # mean ADC counts
            except Exception as e:
                err = e

            def finish():
                self._avg_computing = False
                self._avg_cancel    = False
                self._update_avg_btn_label()
                self._avg_compute_btn.config(bg="#7c3aed")
                self._avg_from_entry.config(state="normal")
                self._avg_to_entry.config(state="normal")
                if err is not None:
                    self._avg_status_lbl.config(text=f"Error: {err}", foreground="#dc3545")
                    return
                if count == 0:
                    self._avg_status_lbl.config(text="No events read", foreground="#dc3545")
                    return
                # Render partial result even if cancelled — useful for a quick look.
                self._avg_result = result
                self._avg_wave   = wave
                tag = "Cancelled" if cancelled else "Done"
                color = "#f0ad4e" if cancelled else "#16a34a"
                self._avg_status_lbl.config(
                    text=f"{tag} — {count:,} events averaged", foreground=color)
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
        is_avg = self._fft_avg_mode.get()
        if self._fft_mode:
            if is_avg:
                # Average mode: show cached result if available; else placeholder
                if self._avg_result is not None:
                    self._render_fft_avg(self._avg_result)
                else:
                    self._render_fft_avg_placeholder()
            else:
                self._render_fft(adc_flat, entry)
            return

        # Time-domain average: rebuild a flat ADC array from the mean waveform
        # and fall through to the normal renderer (so pedestal/charge/axes all work).
        if is_avg:
            if self._avg_wave is None:
                self._render_avg_wave_placeholder()
                return
            n = self._n_samples
            flat = np.zeros((max(self._channels) + 1) * n, dtype=np.float64)
            for ch, mean_adc in self._avg_wave.items():
                flat[ch * n:(ch + 1) * n] = mean_adc
            adc_flat = flat

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

        ped_start, ped_end = self._get_ped_range()   # user-adjustable window
        sig_start, sig_end = self._get_sig_range()

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

            ax = self._fig.add_subplot(n_rows, n_cols, idx + 1)
            ax.set_facecolor(BG)

            seg     = adc_flat[ch * self._n_samples: (ch + 1) * self._n_samples]
            n_seg   = len(seg)                  # actual sample count (matches file's RecordLength)
            samples = np.arange(n_seg)
            x_e     = int(x_e_str) if x_e_str else n_seg
            voltage = seg * ADC_TO_MV
            ped_adc = _pedestal(seg, ped_start, ped_end)
            ped_mv  = ped_adc * ADC_TO_MV

            tag = "AVG" if is_avg else f"#{entry}"
            if ch == TRIGGER_CH:
                ax.set_title(f"CH{ch}  {tag}  │  Trigger",
                             color=LABEL_C, fontsize=9, fontweight="bold", pad=4)
                ax.set_ylim(ped_mv - 100, ped_mv + 200)
            else:
                q = _charge_pC(seg, ped_adc, sig_start, sig_end)
                ax.set_title(f"CH{ch}  {tag}  │  {q:+.3f} pC",
                             color=LABEL_C, fontsize=9, fontweight="bold", pad=4)
                ax.axvspan(ped_start, ped_end, alpha=0.35, color=PED_SPAN_C, lw=0)
                ax.axvspan(sig_start, sig_end, alpha=0.35, color=SIG_SPAN_C, lw=0)
                ax.set_ylim(ped_mv - y_lo, ped_mv + y_hi)

            ax.plot(samples, voltage, color=WAVE, linewidth=0.7)
            # Average-pedestal line, labelled with its value in mV
            ax.axhline(ped_mv, color=PED_C, linewidth=1.0, linestyle="--", alpha=0.8,
                       label=f"ped {ped_mv:.2f} mV")
            ax.legend(fontsize=7, framealpha=0.7, loc="upper right")
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
        """Show a message prompting the user to click Compute (FFT mode)."""
        self._avg_placeholder("Click  'Compute Avg FFT'  to average the selected event range")

    def _render_avg_wave_placeholder(self):
        """Show a message prompting the user to click Compute (time-domain mode)."""
        self._avg_placeholder("Click  'Compute Avg Waveform'  to average the selected event range")

    def _avg_placeholder(self, msg: str):
        self._fig.clear()
        self._fig.set_facecolor("#f9fafb")
        ax = self._fig.add_subplot(1, 1, 1)
        ax.set_facecolor("#f9fafb")
        ax.text(0.5, 0.5, msg, ha="center", va="center",
                fontsize=12, color="#6b7280", transform=ax.transAxes)
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

            # Mark known CAEN internal clock frequencies
            y_max = ax.get_ylim()[1] if ax.get_ylim()[1] > 0 else float(np.max(mag))
            for f_mhz, f_lbl in NOISE_FREQ_LABELS.items():
                ax.axvline(f_mhz, color="#f59e0b", linewidth=0.8, linestyle=":", alpha=0.6)
                ax.text(f_mhz, y_max * 0.97, f_lbl, rotation=90,
                        fontsize=6, color="#b45309", va="top", ha="right", alpha=0.8)

            label = "Trigger" if ch == TRIGGER_CH else f"{peak_freq:.1f} MHz peak"
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
            ey_lo = ttk.Entry(sf, textvariable=self._slot_y_lo[slot], width=5, justify="center")
            ey_lo.pack(side=tk.LEFT, padx=1)
            ttk.Label(sf, text="/+", font=("Helvetica", 9)).pack(side=tk.LEFT)
            ey_hi = ttk.Entry(sf, textvariable=self._slot_y_hi[slot], width=5, justify="center")
            ey_hi.pack(side=tk.LEFT, padx=(1, 4))
            ttk.Label(sf, text="mV  X", font=("Helvetica", 9)).pack(side=tk.LEFT)
            ex_s = ttk.Entry(sf, textvariable=self._slot_x_s[slot], width=5, justify="center")
            ex_s.pack(side=tk.LEFT, padx=(2, 1))
            ttk.Label(sf, text="–", font=("Helvetica", 9)).pack(side=tk.LEFT)
            ex_e = ttk.Entry(sf, textvariable=self._slot_x_e[slot], width=5, justify="center")
            ex_e.pack(side=tk.LEFT, padx=1)
            # Enter in any axis entry triggers redraw
            for entry_w in (ey_lo, ey_hi, ex_s, ex_e):
                entry_w.bind("<Return>", lambda _: self._redraw())

        # Apply button — placed inline after the slot grid, clearly visible
        apply_frame = ttk.Frame(self._ch_grid, padding=(8, 0, 0, 0))
        apply_frame.grid(row=0, column=n_cols, rowspan=2, sticky="w")
        tk.Button(apply_frame, text="✓ Apply", command=self._redraw,
                  bg="#16a34a", fg="white", font=("Helvetica", 9, "bold"),
                  relief="flat", padx=10, pady=3).pack(side=tk.LEFT)

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
            "  Jump: charge  [< / > / |q|>]  [threshold] pC  →  🔍 Search\n"
            "    <    : charge below threshold (e.g. overshoot / negative events)\n"
            "    >    : charge above threshold\n"
            "    |q|> : magnitude above threshold — finds SIGNAL events of either polarity\n"
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

        op = self._search_op.get()
        # Build the match test + a human-readable description for the status line.
        if op == ">":
            match = lambda q: q > thresh
            desc = f"charge > {thresh} pC"
        elif op == "|q|>":
            match = lambda q: abs(q) > thresh        # signal of either polarity
            desc = f"|charge| > {thresh} pC"
        else:  # "<"
            match = lambda q: q < thresh
            desc = f"charge < {thresh} pC"

        # Cancel any in-progress search
        self._search_id = object()
        token = self._search_id

        self._search_btn.config(text="⏹ Cancel", command=self._cancel_search,
                                bg="#dc3545")
        self._status.set(f"Searching for {desc}  (from entry {self._cur_entry + 1})…")

        start = self._cur_entry + 1
        # Use the same pedestal/signal windows as the display so the search agrees with
        # the charge shown on screen.
        ped_start, ped_end = self._get_ped_range()
        sig_start, sig_end = self._get_sig_range()

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
                            q = _charge_pC(seg, _pedestal(seg, ped_start, ped_end), sig_start, sig_end)
                            if match(q):
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
                    self._status.set(f"Found {desc} at entry #{found}")
                    self._show_entry(found)
                else:
                    self._status.set(f"No more events with {desc}")

            self.parent.after(0, on_done)

        threading.Thread(target=worker, daemon=True).start()

    def _cancel_search(self):
        self._search_id = object()   # invalidate token → worker stops at next batch
        self._status.set("Search cancelled.")
        self._reset_search_btn()

    def _reset_search_btn(self):
        self._search_btn.config(text="🔍 Search", command=self._start_search,
                                bg="#fd7e14")

    # ── mV peak search ────────────────────────────────────────────────────

    def _start_mv_search(self):
        """Find next event where selected channel has a peak deviation > threshold mV."""
        if self._tree is None:
            messagebox.showwarning("No File", "Load a file first.")
            return
        try:
            mv_thr = float(self._mv_thresh.get())
        except ValueError:
            messagebox.showwarning("Invalid", "Enter a numeric mV threshold.")
            return
        ch_target = self._mv_search_ch.get()
        if ch_target not in self._channels:
            messagebox.showwarning("Channel", f"CH{ch_target} not in this file.")
            return

        # Cancel any in-progress search
        self._search_id = object()
        token = self._search_id
        desc = f"peak > {mv_thr} mV on CH{ch_target}"
        self._mv_search_btn.config(text="⏹ Cancel", command=self._cancel_mv_search,
                                   bg="#dc3545")
        self._status.set(f"Searching for {desc}…")

        start = self._cur_entry + 1
        ped_start, ped_end = self._get_ped_range()
        n = self._n_samples

        def worker():
            found = -1
            BATCH = 1000
            try:
                for b_start in range(start, self._n_entries, BATCH):
                    if self._search_id is not token:
                        return
                    b_end = min(b_start + BATCH, self._n_entries)
                    adcs = self._tree["ADC"].array(
                        entry_start=b_start, entry_stop=b_end, library="np")
                    for i, adc_flat in enumerate(adcs):
                        seg = adc_flat[ch_target * n:(ch_target + 1) * n].astype(np.float64)
                        ped_mv = float(np.mean(seg[ped_start:ped_end])) * ADC_TO_MV
                        voltage = seg * ADC_TO_MV
                        peak_dev = float(np.max(np.abs(voltage - ped_mv)))
                        if peak_dev > mv_thr:
                            found = b_start + i
                            break
                    if found >= 0:
                        break
                    if (b_start // BATCH) % 10 == 0:
                        pct = 100 * b_start / self._n_entries
                        self.parent.after(0, lambda p=pct, b=b_start: self._status.set(
                            f"Searching… {b:,}/{self._n_entries:,}  ({p:.0f}%)"))
            except Exception as exc:
                self.parent.after(0, lambda e=exc: self._status.set(f"Search error: {e}"))
                self.parent.after(0, self._reset_mv_search_btn)
                return

            def on_done():
                self._reset_mv_search_btn()
                if found >= 0:
                    self._status.set(f"Found {desc} at entry #{found}")
                    self._show_entry(found)
                else:
                    self._status.set(f"No more events with {desc}")

            self.parent.after(0, on_done)

        threading.Thread(target=worker, daemon=True).start()

    def _cancel_mv_search(self):
        self._search_id = object()
        self._status.set("Search cancelled.")
        self._reset_mv_search_btn()

    def _reset_mv_search_btn(self):
        self._mv_search_btn.config(text="🔍 Find", command=self._start_mv_search,
                                   bg="#0891b2")

    # ══════════════════════════════════════════════════════════════════════
    # Noise scan — find events with high FFT amplitude at clock frequencies
    # ══════════════════════════════════════════════════════════════════════

    def _start_noise_scan(self):
        if self._tree is None:
            messagebox.showwarning("No File", "Load a file first.")
            return
        try:
            amp_thr_uv = float(self._noise_amp_var.get())
        except ValueError:
            messagebox.showwarning("Invalid", "Enter a numeric uV threshold.")
            return

        freq_sel = self._noise_freq_var.get()
        if freq_sel == "All":
            target_freqs = NOISE_FREQS_MHZ
        else:
            try:
                target_freqs = [float(freq_sel)]
            except ValueError:
                target_freqs = NOISE_FREQS_MHZ

        self._noise_scan_id = object()
        token = self._noise_scan_id
        self._noise_scan_btn.config(text="⏹ Stop", command=self._cancel_noise_scan,
                                    bg="#dc3545")
        self._status.set(f"Noise scan: looking for >{amp_thr_uv} uV at {freq_sel} MHz…")

        n = self._n_samples
        DT = 2e-9
        freqs_arr = np.fft.rfftfreq(n, d=DT) / 1e6  # MHz
        # Find bin indices for each target frequency
        target_bins = [int(np.argmin(np.abs(freqs_arr - f))) for f in target_freqs]
        window = np.hanning(n).astype(np.float64)
        n_total = self._n_entries
        BATCH = 500

        def worker():
            results = []  # list of (event_idx, freq_mhz, amp_uv)
            try:
                for b_start in range(0, n_total, BATCH):
                    if self._noise_scan_id is not token:
                        break
                    b_end = min(b_start + BATCH, n_total)
                    block = self._tree["ADC"].array(
                        entry_start=b_start, entry_stop=b_end, library="np")
                    block = np.asarray(block, dtype=np.float64)
                    block = block.reshape(b_end - b_start, -1, n)

                    pmt_chs = [ch for ch in self._channels if ch != TRIGGER_CH]
                    for i in range(block.shape[0]):
                        ev = b_start + i
                        for ch in pmt_chs:
                            seg = block[i, ch, :]
                            ped = float(seg[PED_START:PED_END].mean())
                            sig_mv = (seg - ped) * ADC_TO_MV
                            mag = np.abs(np.fft.rfft(sig_mv * window)) * 2.0 / n
                            for f_mhz, b_idx in zip(target_freqs, target_bins):
                                amp_uv = mag[b_idx] * 1e3
                                if amp_uv > amp_thr_uv:
                                    results.append((ev, ch, f_mhz, amp_uv))
                                    break  # one hit per event is enough

                    if (b_start // BATCH) % 4 == 0:
                        pct = 100 * b_start // n_total
                        self.parent.after(0, lambda p=pct, b=b_start: self._status.set(
                            f"Noise scan: {b:,}/{n_total:,} ({p}%)…"))
            except Exception as exc:
                self.parent.after(0, lambda e=exc: self._status.set(f"Scan error: {e}"))

            def finish():
                self._reset_noise_scan_btn()
                self._status.set(f"Noise scan done — {len(results)} events found")
                self._show_noise_results(results, amp_thr_uv, freq_sel)
            self.parent.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    def _cancel_noise_scan(self):
        self._noise_scan_id = object()
        self._status.set("Noise scan cancelled.")
        self._reset_noise_scan_btn()

    def _reset_noise_scan_btn(self):
        self._noise_scan_btn.config(text="📡 Scan", command=self._start_noise_scan,
                                    bg="#065f46")

    def _show_noise_results(self, results, amp_thr_uv, freq_sel):
        """Show scan results in a small popup; clicking an entry jumps to that event."""
        if self._noise_scan_win and self._noise_scan_win.winfo_exists():
            self._noise_scan_win.destroy()

        win = tk.Toplevel(self.parent)
        self._noise_scan_win = win
        win.title(f"Noise Scan — >{amp_thr_uv} uV @ {freq_sel} MHz")
        win.geometry("360x400")

        ttk.Label(win, text=f"{len(results)} events  |  click to jump",
                  font=("Helvetica", 9, "italic"), foreground="#6b7280").pack(pady=(6, 2))

        frame = ttk.Frame(win)
        frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)

        sb = ttk.Scrollbar(frame, orient=tk.VERTICAL)
        lb = tk.Listbox(frame, yscrollcommand=sb.set, font=("Courier", 9),
                        selectmode=tk.SINGLE, activestyle="dotbox")
        sb.config(command=lb.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        sorted_results = sorted(results, key=lambda x: -x[3])  # sort by amp desc
        for ev, ch, f_mhz, amp_uv in sorted_results:
            lb.insert(tk.END, f"#{ev:7d}  CH{ch}  {f_mhz:6.1f} MHz  {amp_uv:7.1f} uV")

        def on_select(event):
            sel = lb.curselection()
            if sel:
                ev = sorted_results[sel[0]][0]
                self._goto(ev)

        lb.bind("<<ListboxSelect>>", on_select)
        tk.Button(win, text="Close", command=win.destroy,
                  bg="#6b7280", fg="white", relief="flat", padx=10).pack(pady=6)

    # ══════════════════════════════════════════════════════════════════════
    # Public: load a file path directly (called from main.py run_waveform)
    # ══════════════════════════════════════════════════════════════════════

    def open_path(self, path: str):
        self._load_file(path)

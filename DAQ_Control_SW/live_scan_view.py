"""Live scan view: QE/Gain/TTS/ChargeResolution per point, for the running
scan and for any earlier dataset (General Scan block or Stability run).

Datasets are built from the FinalResult files on disk, labelled from the run
registry (Data/run_registry.csv) when a run is in it. Several datasets can be
overlaid: each gets its own color, each PMT its own marker shape. The x axis
is the raw stage angle, the Hamamatsu incidence angle, or elapsed time.

IMPORTANT -- values are RAW, uncorrected per-point results (no Monitor
normalization unless ticked, no dark-count subtraction). Cross-check with
`./analyze.sh uniformity <tag> <start> <end>` for the final analysis.
"""
import csv
import glob
import os
import re
import time
import tkinter as tk
from datetime import datetime
from tkinter import ttk

from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

import angle_convert

try:
    import uproot
except ImportError:                                  # pragma: no cover
    uproot = None

_RUN_RE = re.compile(r"precal_result_kor_run_(\d{8})_(\d+)\.root$")
ADC_DIR = os.path.expanduser("~/ADC/ADC_test")
REGISTRY = os.path.join(ADC_DIR, "Data", "run_registry.csv")
RAW_DIRS = [os.path.join(ADC_DIR, "Data", "RAW", "Laser"), os.path.join(ADC_DIR, "Data", "RAW", "Dark"),
            os.path.expanduser("~/external_HDD_1_4T/Data_Backup/RAW/Laser"),
            os.path.expanduser("~/external_HDD_1_4T/Data_Backup/RAW/Dark")]
KIND_SHORT = {"general": "GS", "stability": "ST", "manual": "MN", "dummy": "DM"}

# App palette (main.py _setup_theme).
BG, TEXT, MUTED, BORDER = "#eef0f3", "#1f2430", "#5f6672", "#d3d7dd"


class LiveScanView:
    """Owns the toolbar, the plot and its poller (Live Scan tab)."""

    # Single dataset: the familiar per-PMT colors. Overlay: color = dataset,
    # marker = PMT, so it still reads in black and white.
    CH_STYLE = {1: ("o", "#378ADD"), 2: ("^", "#1D9E75")}
    MON_STYLE = ("s", "#BA7517")
    CH_MARKER = {0: "s", 1: "o", 2: "^"}
    DS_COLORS = ["#378ADD", "#D85A30", "#7F77DD", "#1D9E75", "#D4537E", "#BA7517"]
    DS_STYLES = ["-", "--", ":", "-."]

    METRICS = {
        # key -> (label, tree branch, err branch or None, transform)
        "qe":    ("Relative QE (%)", "relativeQE_raw", "relativeQE_raw_err", None),
        "gain":  ("Gain / SPE charge [pC]", "spe_mean", "spe_mean_error", None),
        "tts":   ("TTS [ns]", "rms_exG", None, lambda v: v * 2.0),
        "chres": ("Charge Resolution [%]", "charge_resolution", "charge_resolution_err", None),
    }

    POLL_MS = 4000
    MENU_RECENT = 4
    MENU_MORE = 30

    def __init__(self, toolbar_parent, plot_parent, controller, extra_buttons=()):
        self.controller = controller
        self.parent = plot_parent
        self._points = {}            # (date, run) -> record
        self._scanned = {}           # path -> mtime of the last successful read
        self._wl_cache = {}          # path -> wavelength, for menu labels
        self._sessions = {}          # key -> session dict
        self._order = []             # session keys, newest first
        self._selected = []          # session keys, in overlay order
        self._touched = False        # operator picked datasets by hand
        self._live_key = None
        self._callout = None
        self._axis_mode = tk.StringVar(value="raw")     # raw | hamamatsu | time
        self._axis_auto = False      # axis was switched to time automatically
        self._axis_filter = tk.StringVar(value="both")
        self._metric = tk.StringVar(value="qe")
        self._mon_norm = tk.BooleanVar(value=False)
        self._after_id = None
        self._build(toolbar_parent, plot_parent, extra_buttons)
        self.schedule_poll()

    # ---------------------------------------------------------------- UI
    def _build(self, bar, plot_parent, extra_buttons):
        row1 = ttk.Frame(bar)
        row1.pack(fill=tk.X)
        row2 = ttk.Frame(bar)
        row2.pack(fill=tk.X, pady=(4, 0))

        self._ds_btn = ttk.Menubutton(row1, text="Dataset: (none)", width=44)
        self._ds_menu = tk.Menu(self._ds_btn, tearoff=False, font=("Helvetica", 10))
        self._ds_btn["menu"] = self._ds_menu
        self._ds_btn.pack(side=tk.LEFT)
        self._live_chip = tk.Label(row1, text="LIVE", bg="#2e9e4f", fg="white",
                                   font=("Helvetica", 10, "bold"), padx=8)
        self._status = ttk.Label(row1, text="waiting for data…", foreground=MUTED,
                                 width=64, anchor="w")
        self._status.pack(side=tk.LEFT, padx=(10, 0))
        for text, cmd in reversed(list(extra_buttons)):
            ttk.Button(row1, text=text, command=cmd).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(row1, text="Refresh", command=self.refresh_now).pack(side=tk.RIGHT, padx=(6, 0))

        def radio_group(label, options, var, cmd):
            ttk.Label(row2, text=label + ":").pack(side=tk.LEFT, padx=(0, 2))
            for text, val in options:
                ttk.Radiobutton(row2, text=text, value=val, variable=var,
                                command=cmd).pack(side=tk.LEFT, padx=2)
            ttk.Separator(row2, orient="vertical").pack(side=tk.LEFT, fill=tk.Y, padx=8)

        radio_group("Metric", [("QE", "qe"), ("Gain", "gain"), ("TTS", "tts"),
                               ("ChargeRes", "chres")], self._metric, self.redraw)
        radio_group("X axis", [("Raw Stage", "raw"), ("Hamamatsu", "hamamatsu"), ("Time", "time")],
                    self._axis_mode, self._on_axis)
        radio_group("Scan axis", [("Both", "both"), ("X", "X"), ("Y", "Y")],
                    self._axis_filter, self.redraw)
        ttk.Checkbutton(row2, text="Monitor-normalize", variable=self._mon_norm,
                        command=self.redraw).pack(side=tk.LEFT)

        note = ttk.Frame(plot_parent)
        note.pack(fill=tk.X, pady=(0, 2))
        ttk.Label(note, text="Raw, uncorrected values -- cross-check with "
                             "./analyze.sh uniformity for the final analysis.",
                  foreground="#a15c00", font=("Helvetica", 9, "italic")).pack(anchor="w")

        self.fig = Figure(figsize=(7.2, 5.0), dpi=100)
        gs = self.fig.add_gridspec(3, 1, hspace=0.08)
        self.ax_test = self.fig.add_subplot(gs[0:2, 0])
        self.ax_mon = self.fig.add_subplot(gs[2, 0], sharex=self.ax_test)
        self.fig.subplots_adjust(left=0.11, right=0.985, top=0.95, bottom=0.1)
        self.canvas = FigureCanvasTkAgg(self.fig, master=plot_parent)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        self.canvas.mpl_connect("pick_event", self._on_pick)
        self.redraw()

    def _on_axis(self):
        self._axis_auto = False
        self.redraw()

    # ------------------------------------------------------------- data
    def _result_dir(self):
        base = None
        getp = getattr(self.controller, "_get_daq_path", None)
        if getp:
            try:
                base = getp()
            except Exception:
                base = None
        return os.path.join(base or ADC_DIR, "Data", "FinalResult")

    @staticmethod
    def _registry():
        out = {}
        try:
            with open(REGISTRY, encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    try:
                        out[(r["date"], int(r["run"]))] = r
                    except (KeyError, ValueError):
                        pass
        except OSError:
            pass
        return out

    def _live_state(self):
        """(kind, date, block) of what is being taken right now, or None."""
        auto = getattr(self.controller, "auto_mgr", None)
        if auto is not None and getattr(auto, "is_running", False):
            date = os.environ.get("SCAN_START_DATE", "") or datetime.now().strftime("%Y%m%d")
            return ("GS", date, getattr(auto, "current_scan_block", None))
        stab = getattr(getattr(self.controller, "auto_ui", None), "stability_run_ui", None)
        if stab is not None and getattr(stab, "_running", False):
            return ("ST", None, None)
        return None

    def _discover(self):
        """Group every FinalResult file into datasets."""
        reg = self._registry()
        by_date = {}
        for path in glob.glob(os.path.join(self._result_dir(), "precal_result_kor_run_*.root")):
            m = _RUN_RE.search(os.path.basename(path))
            if m:
                by_date.setdefault(m.group(1), {})[int(m.group(2))] = path
        sessions = {}
        for date, runs in by_date.items():
            prev, key = None, None
            for run in sorted(runs):
                r = reg.get((date, run))
                kind = KIND_SHORT.get(r["kind"], "MN") if r else (
                    "GS" if run < 700 else "DK" if run < 800 else "ST")
                if kind in ("GS", "DM"):
                    key = (date, kind, run // 100)
                elif (key is None or prev is None or run != prev + 1 or key[1] != kind
                      or (r is not None and r.get("seq") == "1")):
                    key = (date, kind, run)
                s = sessions.setdefault(key, {"key": key, "date": date, "kind": kind, "runs": [],
                                              "paths": {}, "wl": None, "t0": None})
                s["runs"].append(run)
                s["paths"][run] = runs[run]
                if r is not None:
                    s["wl"] = s["wl"] or r.get("wavelength")
                    try:
                        t = datetime.fromisoformat(r["registered_at"]).timestamp()
                        s["t0"] = t if s["t0"] is None else min(s["t0"], t)
                    except (KeyError, ValueError):
                        pass
                prev = run
        sessions = self._split_by_wavelength(sessions)
        for s in sessions.values():
            if s["kind"] == "ST" and len(s["runs"]) == 1 and (s["date"], s["runs"][0]) not in reg:
                s["kind"] = "MN"
            if s["wl"] is None:
                p = next((self._points.get((s["date"], r)) for r in s["runs"]
                          if (s["date"], r) in self._points), None)
                s["wl"] = p.get("wl") if p else self._wl_cache.get(s["paths"][s["runs"][0]])
            if s["t0"] is None:
                try:
                    s["t0"] = os.path.getmtime(s["paths"][s["runs"][0]])
                except OSError:
                    s["t0"] = 0

        live = self._live_state()
        self._live_key = None
        if live:
            kind, date, block = live
            cands = [s for s in sessions.values() if s["kind"] == kind and (date is None or s["date"] == date)
                     and (block is None or s["key"][2] == block // 100)]
            if cands:
                self._live_key = max(cands, key=lambda s: (s["date"], s["runs"][-1]))["key"]
        self._sessions = sessions
        self._order = sorted(sessions, key=lambda k: (sessions[k]["date"], sessions[k]["t0"]), reverse=True)

    def _split_by_wavelength(self, sessions):
        """Contiguous runs without registry rows can hold two stability runs
        back to back (e.g. 450 nm then 473 nm); split where the wavelength of
        the loaded points changes."""
        out = {}
        for key, s in sessions.items():
            wls = [(r, self._points[(s["date"], r)]["wl"]) for r in s["runs"] if (s["date"], r) in self._points]
            if len({w for _, w in wls}) < 2:
                out[key] = s
                continue
            cut, prev_wl, cur = [], None, None
            wl_of = dict(wls)
            for r in s["runs"]:
                w = wl_of.get(r, prev_wl)
                if cur is None or w != prev_wl:
                    k2 = (s["date"], s["kind"], r)
                    cur = out[k2] = {"key": k2, "date": s["date"], "kind": s["kind"], "runs": [],
                                     "paths": {}, "wl": w, "t0": None}
                cur["runs"].append(r)
                cur["paths"][r] = s["paths"][r]
                prev_wl = w
        return out

    def _first_wavelength(self, s):
        path = s["paths"][s["runs"][0]]
        if path not in self._wl_cache:
            try:
                with uproot.open(path) as f:
                    self._wl_cache[path] = str(f["RunInfo"]["Wavelength"].array(library="np")[0])
            except Exception:
                self._wl_cache[path] = None
        return self._wl_cache[path]

    def _label(self, key, long=False):
        s = self._sessions.get(key)
        if not s:
            return str(key)
        d = f"{s['date'][4:6]}-{s['date'][6:]}"
        rng = f"{s['runs'][0]:03d}–{s['runs'][-1]:03d}" if len(s["runs"]) > 1 else f"{s['runs'][0]:03d}"
        wl = s["wl"] or "?"
        txt = f"{d}  {s['kind']} {wl}  {rng}"
        if key == self._live_key:
            txt += "  live"
        elif long:
            txt += f"  ({len(s['runs'])})"
        return txt

    def _point_time(self, date, run, path, reg):
        r = reg.get((date, run))
        if r:
            try:
                return datetime.fromisoformat(r["registered_at"]).timestamp()
            except (KeyError, ValueError):
                pass
        for d in RAW_DIRS:
            raw = os.path.join(d, f"precal_raw_kor_run_{date}_{run:03d}.root")
            if os.path.exists(raw):
                return os.path.getmtime(raw)
        return os.path.getmtime(path)

    def _read_point(self, date, run, path, reg):
        try:
            with uproot.open(path) as f:
                ri = f["RunInfo"].arrays(
                    ["SN1", "SN2", "SN3", "Direction2", "Direction3",
                     "RawTiltAngle2", "RawRotateAngle2", "RawTiltAngle3", "RawRotateAngle3",
                     "Wavelength", "Shifter"], library="np")

                def s(key):
                    v = ri[key][0]
                    return v.decode() if isinstance(v, bytes) else str(v)

                tilt2, rot2 = float(ri["RawTiltAngle2"][0]), float(ri["RawRotateAngle2"][0])
                tilt3, rot3 = float(ri["RawTiltAngle3"][0]), float(ri["RawRotateAngle3"][0])
                ham2, axis2 = angle_convert.get_hamamatsu_angle(s("Direction2"), tilt2, rot2)
                ham3, axis3 = angle_convert.get_hamamatsu_angle(s("Direction3"), tilt3, rot3)
                data = {mk: {} for mk in self.METRICS}
                for ch in (0, 1, 2):
                    try:
                        t = f[f"tree_ch{ch}"]
                    except Exception:
                        continue
                    for mk, (_l, branch, errbranch, xform) in self.METRICS.items():
                        try:
                            v = float(t[branch].array(library="np")[0])
                            e = float(t[errbranch].array(library="np")[0]) if errbranch else 0.0
                            if xform:
                                v, e = xform(v), (xform(e) if errbranch else 0.0)
                            data[mk][ch] = (v, e)
                        except Exception:
                            continue
        except Exception:
            return None
        return {
            "run": run, "date": date, "path": path, "mtime": os.path.getmtime(path),
            "time": self._point_time(date, run, path, reg),
            "wl": s("Wavelength"), "shifter": s("Shifter"),
            # the monitor never moves: plot it against device 2's stage angle
            "raw": {0: tilt2, 1: tilt2, 2: tilt3},
            "ham": {0: ham2, 1: ham2, 2: ham3},
            "axis": {0: axis2, 1: axis2, 2: axis3},
            "sn": {0: s("SN1"), 1: s("SN2"), 2: s("SN3")},
            "data": data,
        }

    # ------------------------------------------------------------- polling
    def schedule_poll(self):
        if getattr(self.controller, "_shutting_down", False):
            return
        try:
            self.poll_once()
        except Exception as e:
            self.controller._log(f"[WARNING] Live scan view poll failed: {e}")
        self._after_id = self.parent.after(self.POLL_MS, self.schedule_poll)

    def poll_once(self):
        if uproot is None:
            self._status.config(text="uproot not installed", foreground="#b91c1c")
            return
        before = (list(self._order[:1]), self._live_key)
        self._discover()
        if not self._order:
            return
        newest = self._order[0]
        if not self._touched:
            want = [self._live_key or newest]
            if want != self._selected:
                self._selected = want
                self._auto_axis()
        else:
            fixed = []
            for k in self._selected:
                if k not in self._sessions:     # split by wavelength since it was picked
                    k = next((n for n, s in self._sessions.items()
                              if s["date"] == k[0] and s["kind"] == k[1] and k[2] in s["runs"]), None)
                if k and k not in fixed:
                    fixed.append(k)
            self._selected = fixed
            if self._live_key and self._live_key not in self._selected and before[1] != self._live_key:
                self._selected.append(self._live_key)
        reg = self._registry()
        got = False
        for key in self._selected:
            s = self._sessions[key]
            for run in s["runs"]:
                path = s["paths"][run]
                try:
                    mt = os.path.getmtime(path)
                except OSError:
                    continue
                if self._scanned.get(path) == mt:
                    continue
                rec = self._read_point(s["date"], run, path, reg)
                if rec:          # a file mid-write fails to parse; retry next poll
                    self._scanned[path] = mt
                    self._points[(s["date"], run)] = rec
                    got = True
                    if not s["wl"]:
                        s["wl"] = rec["wl"]
        if got or before != (list(self._order[:1]), self._live_key):
            self._refresh_menu()
            self.redraw()
        self._update_status()

    def refresh_now(self):
        if self._after_id is not None:
            self.parent.after_cancel(self._after_id)
        self.schedule_poll()

    def reset(self):
        """A new scan started: follow it."""
        self._touched = False
        self._selected = []
        self._callout = None
        self.refresh_now()

    def newest_point(self):
        pts = [p for (d, r), p in self._points.items()
               if self._selected and self._sessions.get(self._selected[0])
               and d == self._sessions[self._selected[0]]["date"]
               and r in self._sessions[self._selected[0]]["runs"]]
        return max(pts, key=lambda p: p["time"]) if pts else None

    def _auto_axis(self):
        kinds = {self._sessions[k]["kind"] for k in self._selected if k in self._sessions}
        if kinds and kinds <= {"ST", "MN"}:
            if self._axis_mode.get() != "time":
                self._axis_mode.set("time")
                self._axis_auto = True
        elif self._axis_auto:
            self._axis_mode.set("raw")
            self._axis_auto = False

    def _update_status(self):
        if not self._selected:
            self._status.config(text="waiting for data…", foreground=MUTED)
            self._live_chip.pack_forget()
            return
        key = self._selected[0]
        s = self._sessions.get(key)
        pts = [self._points[(s["date"], r)] for r in s["runs"] if (s["date"], r) in self._points] if s else []
        n = len(pts)
        total = None
        if key == self._live_key and s["kind"] == "GS":
            auto = getattr(self.controller, "auto_mgr", None)
            try:
                total = len(auto.build_tilt_angles()) * 2
            except Exception:
                total = None
        txt = f"{n} / {total} points" if total else f"{n} points"
        if pts:
            t = max(p["time"] for p in pts)
            age = time.time() - t
            ago = f"{int(age)} s ago" if age < 120 else f"{int(age // 60)} min ago" if age < 7200 \
                else f"{age / 3600:.1f} h ago"
            txt += f"  ·  newest {time.strftime('%m-%d %H:%M', time.localtime(t))} ({ago})"
        if len(self._selected) > 1:
            txt += f"  ·  {len(self._selected)} datasets"
        self._status.config(text=txt, foreground="#1a7f37" if key == self._live_key else TEXT)
        if key == self._live_key:
            self._live_chip.pack(side=tk.LEFT, padx=(8, 0), after=self._ds_btn)
        else:
            self._live_chip.pack_forget()

    # ------------------------------------------------------------- menu
    def _refresh_menu(self):
        m = self._ds_menu
        m.delete(0, "end")
        if not self._order:
            m.add_command(label="(no data yet)", state="disabled")
            return

        def add(menu, key):
            var = tk.BooleanVar(value=key in self._selected)
            menu.add_checkbutton(label=self._label(key), variable=var,
                                 command=lambda k=key, v=var: self._toggle(k, v.get()))
            self._menu_vars.append(var)

        self._menu_vars = []
        for key in self._order[:self.MENU_RECENT + self.MENU_MORE]:
            s = self._sessions[key]
            if not s["wl"] and uproot is not None:
                s["wl"] = self._first_wavelength(s)
        for key in self._order[:self.MENU_RECENT]:
            add(m, key)
        rest = self._order[self.MENU_RECENT:self.MENU_RECENT + self.MENU_MORE]
        if rest:
            more = tk.Menu(m, tearoff=False, font=("Helvetica", 10))
            for key in rest:
                add(more, key)
            m.add_separator()
            m.add_cascade(label="More", menu=more)
        m.add_separator()
        m.add_command(label="Follow newest", command=self._follow_newest)
        self._ds_btn.config(text="Dataset: " + (self._label(self._selected[0]) if len(self._selected) == 1
                                                 else f"{len(self._selected)} selected" if self._selected
                                                 else "(none)"))

    def _toggle(self, key, on):
        self._touched = True
        if on and key not in self._selected:
            self._selected.append(key)
        elif not on and key in self._selected:
            self._selected.remove(key)
        self._callout = None
        self._auto_axis()
        self._refresh_menu()
        self.refresh_now()
        self.redraw()

    def _follow_newest(self):
        self._touched = False
        self._selected = []
        self.refresh_now()

    # ------------------------------------------------------------- drawing
    def _series(self, key, ch):
        """(x, y, yerr, recs) for one dataset and channel."""
        s = self._sessions.get(key)
        if not s:
            return [], [], [], []
        mode, mkey, want = self._axis_mode.get(), self._metric.get(), self._axis_filter.get()
        do_norm = self._mon_norm.get() and ch != 0
        recs = sorted((self._points[(s["date"], r)] for r in s["runs"] if (s["date"], r) in self._points),
                      key=lambda p: p["time"])
        t0 = recs[0]["time"] if recs else 0
        xs, ys, es, keep = [], [], [], []
        for p in recs:
            pair = p["data"].get(mkey, {}).get(ch)
            if pair is None or (want != "both" and p["axis"].get(ch) != want):
                continue
            val, err = pair
            if do_norm:
                mon = p["data"].get(mkey, {}).get(0)
                if mon is None or mon[0] == 0:
                    continue
                ratio = val / mon[0]
                rel = ((err / val) ** 2 + (mon[1] / mon[0]) ** 2) ** 0.5 if val else 0.0
                val, err = ratio, ratio * rel
            xs.append((p["time"] - t0) / 3600.0 if mode == "time" else
                      p["raw"][ch] if mode == "raw" else p["ham"][ch])
            ys.append(val)
            es.append(err)
            keep.append(p)
        return xs, ys, es, keep

    @staticmethod
    def _headroom(ax, frac=0.22):
        lo, hi = ax.get_ylim()
        if hi > lo:
            ax.set_ylim(lo, hi + (hi - lo) * frac)

    def redraw(self):
        for ax in (self.ax_test, self.ax_mon):
            ax.clear()
            ax.grid(alpha=0.3)
        self._callout = None
        self._pick_map = {}
        mode = self._axis_mode.get()
        mkey = self._metric.get()
        label = self.METRICS[mkey][0]
        multi = len(self._selected) > 1
        any_data = False
        ds_handles = []

        for i, key in enumerate(self._selected):
            color = self.DS_COLORS[i % len(self.DS_COLORS)]
            ls = self.DS_STYLES[i % len(self.DS_STYLES)]
            for ax, chans in ((self.ax_test, (1, 2)), (self.ax_mon, (0,))):
                for ch in chans:
                    xs, ys, es, recs = self._series(key, ch)
                    if not xs:
                        continue
                    any_data = True
                    if multi:
                        c, mk = color, self.CH_MARKER[ch]
                        lab = None
                    else:
                        mk, c = self.CH_STYLE.get(ch, self.MON_STYLE)
                        sn = recs[0]["sn"].get(ch, f"ch{ch}")
                        lab = f"{sn} (monitor)" if ch == 0 else sn
                    fills = [c if p["axis"].get(ch) != "Y" or mode != "time" else "none" for p in recs]
                    cont = ax.errorbar(xs, ys, yerr=es, fmt=mk, ms=4.5, capsize=2, color=c,
                                       linestyle="none", label=lab, picker=6)
                    line = cont.lines[0]
                    if multi and mode != "time":
                        for axis_name in ("X", "Y"):
                            pts = sorted((x, y) for x, y, p in zip(xs, ys, recs) if p["axis"].get(ch) == axis_name)
                            if len(pts) > 1:
                                ax.plot([q[0] for q in pts], [q[1] for q in pts], color=c, linestyle=ls, lw=1)
                    if mode == "time" and "none" in fills:
                        line.set_markerfacecolor("none")
                        solid = [k for k, f in enumerate(fills) if f != "none"]
                        if solid:
                            ax.plot([xs[k] for k in solid], [ys[k] for k in solid], mk, ms=4.5,
                                    color=c, linestyle="none")
                    self._pick_map[line] = (key, ch, recs)
            if multi:
                ds_handles.append(Line2D([0], [0], color=color, linestyle=self.DS_STYLES[i % len(self.DS_STYLES)],
                                         lw=2, label=f"{chr(65 + i)}  {self._label(key)}"))

        xlabel = {"raw": "Raw Stage angle [degree]", "hamamatsu": "Hamamatsu incidence angle [degree]",
                  "time": "Elapsed time since dataset start [h]"}[mode]
        self.ax_test.set_ylabel(f"{label} / Monitor" if self._mon_norm.get() else label)
        self.ax_mon.set_ylabel(label.split(" (")[0].split(" [")[0] + " (mon.)")
        self.ax_mon.set_xlabel(xlabel)
        self.ax_test.tick_params(labelbottom=False)

        if self._selected and len(self._selected) == 1 and self._selected[0] in self._sessions:
            s = self._sessions[self._selected[0]]
            self.fig.suptitle(f"{s['date']}  ·  {s['kind']} {s['wl'] or '?'} nm  ·  runs "
                              f"{s['runs'][0]}–{s['runs'][-1]}", fontsize=8, color="#555555",
                              x=0.99, y=0.995, ha="right")
        else:
            self.fig.suptitle("")

        if any_data:
            for ax in (self.ax_test, self.ax_mon):
                self._headroom(ax)
            if multi:
                pmt = [Line2D([0], [0], marker=self.CH_MARKER[c], color="#555555", linestyle="none",
                              label=n) for c, n in ((1, "Rot1"), (2, "Rot2"))]
                leg = self.ax_test.legend(handles=ds_handles + pmt, loc="upper right", fontsize=7,
                                          framealpha=0.92)
            else:
                leg = self.ax_test.legend(loc="upper right", fontsize=7, framealpha=0.9, markerscale=0.8)
                self.ax_mon.legend(loc="upper right", fontsize=7, framealpha=0.9,
                                   markerscale=0.8).set_zorder(5)
            leg.set_zorder(5)
        else:
            self.ax_test.text(0.5, 0.5, "No points yet.\nPoints appear as each one finishes analysis.",
                              ha="center", va="center", transform=self.ax_test.transAxes,
                              color="#888", fontsize=10)
        self.canvas.draw_idle()

    def _on_pick(self, event):
        """Click a point: all four metrics in a callout, plus the same point in
        every other overlaid dataset with its % difference."""
        entry = self._pick_map.get(event.artist)
        if not entry or not len(event.ind):
            return
        key, ch, recs = entry
        idx = event.ind[0]
        p = recs[idx]
        tag = chr(65 + self._selected.index(key)) + " · " if len(self._selected) > 1 else ""
        sn = p["sn"].get(ch, f"ch{ch}")
        lines = [f"{tag}run {p['run']:03d}  ·  {sn}",
                 f"{p['axis'].get(ch, '?')} {p['raw'][ch]:+.0f}°  ham {p['ham'][ch]:+.1f}°",
                 time.strftime("%m-%d %H:%M", time.localtime(p["time"]))]
        for mk, (label, *_r) in self.METRICS.items():
            v = p["data"].get(mk, {}).get(ch)
            if v:
                short = label.split(" (")[0].split(" [")[0]
                lines.append(f"{short:<6} {v[0]:.3g}" + (f" ± {v[1]:.2g}" if v[1] else ""))
        mkey = self._metric.get()
        mine = p["data"].get(mkey, {}).get(ch)
        for j, other in enumerate(self._selected):
            if other == key or other not in self._sessions:
                continue
            s = self._sessions[other]
            match = [q for (d, r), q in self._points.items() if d == s["date"] and r in s["runs"]
                     and q["axis"].get(ch) == p["axis"].get(ch) and q["raw"][ch] == p["raw"][ch]]
            if match and mine:
                ov = match[0]["data"].get(mkey, {}).get(ch)
                if ov and mine[0]:
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
            bbox=dict(boxstyle="round,pad=0.6,rounding_size=0.8", fc="#1b1f2a", ec="#3ddc84",
                      lw=1.1, alpha=0.95),
            arrowprops=dict(arrowstyle="->", color="#3ddc84", lw=1.2))
        self.canvas.draw_idle()
        show = getattr(self.controller, "show_scan_point_card", None)
        if show:
            show(p["run"], p)

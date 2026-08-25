"""Live scan view: QE/Gain/TTS/ChargeResolution vs angle, filling in as a
General Scan progresses.

Replaces the Scan Progress Matrix's job of answering "how far along are we".
A 46-cell grid showed progress but not *quality* -- an "OK" cell looked the
same whether the point was good or garbage, so a bad point was only
discoverable hours later when the Uniformity report was finally built. Here
progress is implicit (points appear left to right) and the physics is visible
while there is still time to react.

Data source is each point's FinalResult file, read with uproot rather than by
shelling out to ROOT: the view refreshes on a timer, and a ~1 s ROOT startup
per refresh would leave the plot lagging several points behind the scan.

IMPORTANT -- this shows RAW, uncorrected per-point values (no Monitor
normalization, no dark-count subtraction), because that is all a single
FinalResult file carries before the full Uniformity pass. It is a fast sanity
check, not the final analysis: cross-check anything that looks off against
`./analyze.sh uniformity <tag> <start> <end>`, which applies the real
corrections.
"""
import os
import re
import glob
import time
import tkinter as tk
from tkinter import ttk

from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

import angle_convert

try:
    import uproot
except ImportError:                                  # pragma: no cover
    uproot = None

_RUN_RE = re.compile(r"precal_result_kor_run_(\d{8})_(\d+)\.root$")


class LiveScanView:
    """Owns the plot + its poller. Embedded into the Scan Progress Matrix tab."""

    # (marker shape, color) per channel -- shape carries the distinction so
    # this still reads on a black/white printout or for anyone colorblind,
    # color is just the faster cue for people who don't need that (2026-08-22,
    # user: "마커는 다르게 해줘 흑백이면 구분안되잖아").
    CH_STYLE = {1: ("o", "#378ADD"), 2: ("^", "#1D9E75")}
    MON_STYLE = ("s", "#BA7517")

    METRICS = {
        # key -> (label, tree branch, err branch or None, transform)
        "qe":    ("Relative QE (%)", "relativeQE_raw", "relativeQE_raw_err", None),
        "gain":  ("Gain / SPE charge [pC]", "spe_mean", "spe_mean_error", None),
        "tts":   ("TTS [ns]", "rms_exG", None, lambda v: v * 2.0),
        "chres": ("Charge Resolution [%]", "charge_resolution", "charge_resolution_err", None),
    }

    POLL_MS = 4000          # cheap: a directory listing + a few tiny reads

    def __init__(self, toolbar_parent, plot_parent, controller):
        self.controller = controller
        self.parent = plot_parent    # used for scheduling .after() callbacks
        self._points = {}       # run_number -> dict(...)
        self._scanned_files = {}    # path -> mtime at last successful read
        self._current_block = None    # (date, set of run numbers) currently shown
        self._axis_mode = tk.StringVar(value="raw")     # "raw" | "hamamatsu"
        self._axis_filter = tk.StringVar(value="both")  # "both" | "X" | "Y"
        self._metric = tk.StringVar(value="qe")
        self._mon_norm = tk.BooleanVar(value=False)
        self._after_id = None
        self._build(toolbar_parent, plot_parent)
        self.schedule_poll()

    # ---------------------------------------------------------------- UI
    def _build(self, toolbar_parent, plot_parent):
        # Toolbar lives in its own parent (a full-width row above Live
        # Console + plot); layout of THAT row is owned by ui_automation.py
        # per the operator's sketch: toolbar on top, Console(4) | plot(6)
        # below it (2026-08-22).
        def radio_group(label, options, var):
            ttk.Label(toolbar_parent, text=label + ":").pack(side=tk.LEFT, padx=(0, 2))
            for text, val in options:
                ttk.Radiobutton(toolbar_parent, text=text, value=val, variable=var,
                                command=self.redraw).pack(side=tk.LEFT, padx=(2, 2))
            ttk.Separator(toolbar_parent, orient="vertical").pack(
                side=tk.LEFT, fill=tk.Y, padx=8)

        radio_group("Metric", [("QE", "qe"), ("Gain", "gain"),
                               ("TTS", "tts"), ("ChargeRes", "chres")], self._metric)
        radio_group("X axis", [("Raw Stage", "raw"), ("Hamamatsu", "hamamatsu")],
                    self._axis_mode)
        radio_group("Scan axis", [("Both", "both"), ("X", "X"), ("Y", "Y")],
                    self._axis_filter)

        # Divides each test-PMT point by the Monitor's own value from the SAME
        # run, cancelling the common-mode laser/thermal wobble the Monitor
        # panel below tracks (2026-08-22: user noticed the raw top panel
        # echoes the Monitor panel's shape and asked for this).
        ttk.Checkbutton(toolbar_parent, text="Monitor-normalize", variable=self._mon_norm,
                        command=self.redraw).pack(side=tk.LEFT, padx=(0, 8))

        # Manual refresh -- the timer poll is only every 4s and is skipped
        # entirely while idle-fallback hasn't noticed a new block yet; this
        # forces an immediate re-scan on demand (2026-08-22, user: "Refresh도
        # 추가해줄래? 혹시 모르니").
        ttk.Button(toolbar_parent, text="🔄 Refresh", command=self.refresh_now).pack(
            side=tk.LEFT, padx=(0, 8))

        self._status = ttk.Label(toolbar_parent, text="waiting for data…", foreground="#888")
        self._status.pack(side=tk.RIGHT)
        self._recency = ttk.Label(toolbar_parent, text="", foreground="#888",
                                  font=("Helvetica", 9))
        self._recency.pack(side=tk.RIGHT, padx=(0, 10))

        # Second row: "this is raw, not final" disclaimer -- otherwise
        # nothing on screen says these numbers skip Monitor normalization /
        # dark-count subtraction (2026-08-22: "데이터가 언제적 데이터인건지도
        # 모르겠네").
        note_row = ttk.Frame(plot_parent)
        note_row.pack(fill=tk.X, pady=(0, 2))
        ttk.Label(note_row, text="Raw, uncorrected values -- cross-check with "
                                 "./analyze.sh uniformity for the final analysis.",
                 foreground="#a15c00", font=("Helvetica", 9, "italic")).pack(anchor="w")

        # Two stacked pads: test PMTs on top (the measurement), monitor below
        # (the reference). Sharing the x axis makes it obvious at a glance
        # whether a wobble in the top pad is real or just the laser moving --
        # if the monitor wobbles the same way, it is the laser.
        self.fig = Figure(figsize=(7.2, 5.0), dpi=100)
        gs = self.fig.add_gridspec(3, 1, hspace=0.08)
        self.ax_test = self.fig.add_subplot(gs[0:2, 0])
        self.ax_mon = self.fig.add_subplot(gs[2, 0], sharex=self.ax_test)
        self.fig.subplots_adjust(left=0.13, right=0.98, top=0.95, bottom=0.11)

        self.canvas = FigureCanvasTkAgg(self.fig, master=plot_parent)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        self.canvas.mpl_connect("pick_event", self._on_pick)
        self.redraw()

    # ------------------------------------------------------------- polling
    def schedule_poll(self):
        if getattr(self.controller, "_shutting_down", False):
            return
        try:
            self.poll_once()
        except Exception as e:                       # never let the UI die on this
            self.controller._log(f"[WARNING] Live scan view poll failed: {e}")
        self._after_id = self.parent.after(self.POLL_MS, self.schedule_poll)

    def _result_dir(self):
        base = None
        getp = getattr(self.controller, "_get_daq_path", None)
        if getp:
            try:
                base = getp()
            except Exception:
                base = None
        base = base or os.path.expanduser("~/ADC/ADC_test")
        return os.path.join(base, "Data", "FinalResult")

    def _target_block(self):
        """Which (date, run) files belong on screen right now.

        While a scan is running, SCAN_START_DATE pins it exactly. Otherwise
        (idle GUI, just looking at the last scan) pick the single most recent
        CONTIGUOUS run of files -- same date, consecutive run numbers ending
        at the newest file on disk. A plain "last 60 files" mixed the tail of
        one block with the whole of the next block that happened to follow it
        the same day (2026-08-22: showed 60 points when the scan was 46,
        because two 46-point blocks landed on the same date and the slice cut
        across both)."""
        date = os.environ.get("SCAN_START_DATE", "")
        if date:
            return date, None    # None = no filter, take every run for this date

        all_files = glob.glob(os.path.join(self._result_dir(), "precal_result_kor_run_*.root"))
        parsed = []
        for f in all_files:
            m = _RUN_RE.search(os.path.basename(f))
            if m:
                parsed.append((m.group(1), int(m.group(2)), f))
        if not parsed:
            return None, None
        parsed.sort()
        last_date, last_run, _ = parsed[-1]
        block_runs = {last_run}
        expect = last_run - 1
        for d, r, _ in reversed(parsed[:-1]):
            if d == last_date and r == expect:
                block_runs.add(r)
                expect -= 1
            elif d == last_date and r == last_run:
                continue                              # duplicate/dup-safety
            else:
                break
        return last_date, block_runs

    def poll_once(self):
        """Pick up any FinalResult files belonging to the current block."""
        if uproot is None:
            self._status.config(text="uproot not installed", foreground="#b91c1c")
            return

        date, block_runs = self._target_block()
        if date is None:
            return
        key = (date, frozenset(block_runs) if block_runs is not None else None)
        if key != self._current_block:
            # A different block than what's on screen (new scan started, or
            # the idle-fallback moved to a newer finished block) -> clear.
            self._current_block = key
            self._points.clear()
            self._scanned_files.clear()

        pattern = os.path.join(self._result_dir(), f"precal_result_kor_run_{date}_*.root")
        files = sorted(glob.glob(pattern))
        if block_runs is not None:
            files = [f for f in files
                    if (m := _RUN_RE.search(os.path.basename(f))) and int(m.group(2)) in block_runs]

        # Re-read a file if its mtime moved since last time, not just the
        # first time it's seen -- an aborted point that gets manually
        # reprocessed writes to the SAME filename, and without this the
        # stale first reading would stick forever (2026-08-22, user: "다시
        # 받아야 하는 경우에는 자동으로 지워지나?").
        new = [f for f in files
              if self._scanned_files.get(f) != os.path.getmtime(f)]
        got_any = False
        for path in new:
            rec = self._read_point(path)
            if rec:
                # Only mark as done once actually readable -- a file mid-write
                # by prod/read_ntp_v7 fails to parse and, if marked scanned
                # anyway, would be silently skipped forever instead of being
                # retried on the next poll.
                self._scanned_files[path] = rec["mtime"]
                self._points[rec["run"]] = rec
                got_any = True
        if got_any:
            self.redraw()
        self._update_recency()

    def refresh_now(self):
        """Manual refresh button: re-check immediately instead of waiting out
        the timer, and cancel/reschedule so the next automatic tick doesn't
        land right on top of this one."""
        if self._after_id is not None:
            self.parent.after_cancel(self._after_id)
        self.schedule_poll()

    def _update_recency(self):
        if not self._points:
            self._recency.config(text="")
            return
        latest = max((p["mtime"] for p in self._points.values()), default=0)
        if not latest:
            self._recency.config(text="")
            return
        age_s = time.time() - latest
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(latest))
        if age_s < 120:
            age_txt = f"{int(age_s)}s ago"
        elif age_s < 7200:
            age_txt = f"{int(age_s // 60)}m ago"
        else:
            age_txt = f"{age_s / 3600:.1f}h ago"
        self._recency.config(text=f"Newest point: {when}  ({age_txt})")

    def _read_point(self, path):
        """Extract one point's angles + per-channel metrics. None if unusable."""
        try:
            with uproot.open(path) as f:
                ri = f["RunInfo"].arrays(
                    ["SN1", "SN2", "SN3", "Direction2", "Direction3",
                     "RawTiltAngle2", "RawRotateAngle2",
                     "RawTiltAngle3", "RawRotateAngle3"], library="np")

                def s(key):
                    v = ri[key][0]
                    return v.decode() if isinstance(v, bytes) else str(v)

                tilt2 = float(ri["RawTiltAngle2"][0])
                rot2 = float(ri["RawRotateAngle2"][0])
                tilt3 = float(ri["RawTiltAngle3"][0])
                rot3 = float(ri["RawRotateAngle3"][0])
                dir2, dir3 = s("Direction2"), s("Direction3")

                ham2, axis2 = angle_convert.get_hamamatsu_angle(dir2, tilt2, rot2)
                ham3, axis3 = angle_convert.get_hamamatsu_angle(dir3, tilt3, rot3)

                # metric_key -> {ch: (value, err)}
                data = {mk: {} for mk in self.METRICS}
                for ch in (0, 1, 2):
                    try:
                        t = f[f"tree_ch{ch}"]
                    except Exception:
                        continue
                    for mk, (_label, branch, errbranch, xform) in self.METRICS.items():
                        try:
                            v = float(t[branch].array(library="np")[0])
                            if xform:
                                v = xform(v)
                            e = float(t[errbranch].array(library="np")[0]) if errbranch else 0.0
                            if xform and errbranch:
                                e = xform(e)
                            data[mk][ch] = (v, e)
                        except Exception:
                            continue
        except Exception:
            return None                     # still being written, or truncated

        base = os.path.basename(path)
        m = _RUN_RE.search(base)
        if not m:
            return None

        return {
            "run": int(m.group(2)), "path": path,
            "mtime": os.path.getmtime(path) if os.path.exists(path) else 0,
            # ch0 (monitor) is plotted against device 2's stage angle, matching
            # what Draw_Uniformity_Norm_v7 does -- the monitor never moves, so
            # its own angle would be a meaningless constant.
            "raw": {0: tilt2, 1: tilt2, 2: tilt3},
            "ham": {0: ham2, 1: ham2, 2: ham3},
            "axis": {0: axis2, 1: axis2, 2: axis3},
            "sn": {0: s("SN1"), 1: s("SN2"), 2: s("SN3")},
            "data": data,
        }

    # ------------------------------------------------------------- drawing
    def _series(self, ch):
        """(x, y, yerr, runs) for one channel, current metric + axis settings.

        With Monitor-normalize on and ch != 0, y becomes ch_value / mon_value
        from the SAME run (relative errors added in quadrature) -- this is the
        same ratio Draw_Uniformity_Norm_v7's MonNorm_* graphs compute, just
        per-point and live instead of over a finished block."""
        xkey = "raw" if self._axis_mode.get() == "raw" else "ham"
        mkey = self._metric.get()
        want = self._axis_filter.get()
        do_norm = self._mon_norm.get() and ch != 0
        xs, ys, es, runs = [], [], [], []
        for run in sorted(self._points):
            p = self._points[run]
            pair = p["data"].get(mkey, {}).get(ch)
            if pair is None:
                continue
            if want != "both" and p["axis"].get(ch) != want:
                continue
            val, err = pair
            if do_norm:
                mon = p["data"].get(mkey, {}).get(0)
                if mon is None or mon[0] == 0:
                    continue
                mval, merr = mon
                ratio = val / mval
                rel = 0.0
                if val:
                    rel = ((err / val) ** 2 + (merr / mval) ** 2) ** 0.5
                val, err = ratio, ratio * rel
            xs.append(p[xkey][ch])
            ys.append(val)
            es.append(err)
            runs.append(run)
        return xs, ys, es, runs

    @staticmethod
    def _reserve_legend_headroom(ax, frac=0.22):
        """Extend the y axis upward so the legend box has empty space to sit
        in instead of covering the top-most points -- same idea Draw_
        Overlay_Uniformity_v7.C uses (DataConditionReserveLegendHeadroom),
        just in matplotlib instead of ROOT (2026-08-22, user: "Legend가
        데이터를 가리네")."""
        lo, hi = ax.get_ylim()
        span = hi - lo
        if span <= 0:
            return
        ax.set_ylim(lo, hi + span * frac)

    def redraw(self):
        for ax in (self.ax_test, self.ax_mon):
            ax.clear()
            ax.grid(alpha=0.3)

        self._pick_map = {}
        any_data = False
        mkey = self._metric.get()
        metric_label = self.METRICS[mkey][0]

        for ch in (1, 2):
            xs, ys, es, runs = self._series(ch)
            if not xs:
                continue
            any_data = True
            marker, color = self.CH_STYLE[ch]
            sn = self._points[runs[0]]["sn"].get(ch, f"ch{ch}")
            line = self.ax_test.errorbar(
                xs, ys, yerr=es, fmt=marker, ms=4.5, capsize=2,
                color=color, label=sn, picker=6)
            self._pick_map[line.lines[0]] = (ch, runs)

        xs, ys, es, runs = self._series(0)
        if xs:
            any_data = True
            marker, color = self.MON_STYLE
            sn = self._points[runs[0]]["sn"].get(0, "monitor")
            self.ax_mon.errorbar(xs, ys, yerr=es, fmt=marker, ms=4, capsize=2,
                                 color=color, label=f"{sn} (monitor)")

        xlabel = ("Raw Stage angle [degree]" if self._axis_mode.get() == "raw"
                  else "Hamamatsu incidence angle [degree]")
        top_label = (f"{metric_label} / Monitor" if self._mon_norm.get() else metric_label)
        self.ax_test.set_ylabel(top_label)
        self.ax_mon.set_ylabel(metric_label.split(" (")[0].split(" [")[0] + " (mon.)")
        self.ax_mon.set_xlabel(xlabel)
        self.ax_test.tick_params(labelbottom=False)
        if any_data:
            for ax in (self.ax_test, self.ax_mon):
                self._reserve_legend_headroom(ax)
                ax.legend(loc="upper right", fontsize=7, framealpha=0.9, markerscale=0.8)
            self._status.config(text=f"{len(self._points)} points", foreground="#1a7f37")
        else:
            self.ax_test.text(0.5, 0.5, "No scan points yet.\nPoints appear here as each "
                                        "one finishes analysis.",
                              ha="center", va="center", transform=self.ax_test.transAxes,
                              color="#888", fontsize=10)
            self._status.config(text="waiting for data…", foreground="#888")

        self.canvas.draw_idle()

    def _on_pick(self, event):
        """Click a point -> offer its per-run plots."""
        entry = self._pick_map.get(event.artist)
        if not entry or not len(event.ind):
            return
        ch, runs = entry
        run = runs[event.ind[0]]
        p = self._points.get(run)
        if not p:
            return
        show = getattr(self.controller, "show_scan_point_card", None)
        if show:
            show(run, p)
        else:
            mkey = self._metric.get()
            val = p["data"].get(mkey, {}).get(ch, (None, None))[0]
            self.controller._log(
                f"[INFO] Live view: run {run:03d}  raw={p['raw'][ch]:.1f}deg  "
                f"ham={p['ham'][ch]:.2f}deg  {mkey}={val}")

    def reset(self):
        """Drop accumulated points (called when a new scan starts)."""
        self._points.clear()
        self._scanned_files.clear()
        self._current_block = None
        self.redraw()

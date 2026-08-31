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
        self._callout = None    # the one active click-annotation bubble, if any
        self._axis_mode = tk.StringVar(value="raw")     # "raw" | "hamamatsu"
        self._axis_filter = tk.StringVar(value="both")  # "both" | "X" | "Y"
        self._metric = tk.StringVar(value="qe")
        self._mon_norm = tk.BooleanVar(value=False)
        # Which 100-run block to plot -- "All" (default) or one specific
        # block ("000-099", "100-199", ...). SCAN_START_EPOCH-based filtering
        # (see _target_block) now correctly follows one scan across MULTIPLE
        # blocks (a repeat pass, or a new wavelength), so a scan that used two
        # blocks shows both merged into one plot with no way to see just one
        # (2026-08-29, user: "여러 데이터 셋이 나왔는데... 선택도 할 수 있게
        # 해주지" -- 92 points from two 46-point blocks overlaid together).
        # See _block_vars/_block_order/_selected_blocks below for the actual
        # Dataset checklist state.
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

        # Dataset checklist: which scan session(s) to plot. The one still
        # growing is labelled "Live scan" and defaults ON; every earlier
        # finished session is "Record N" (chronological) and defaults OFF --
        # visible on demand, not merged in automatically (2026-08-29).
        # Rebuilt by _refresh_block_menu() whenever a run lands in a session
        # not seen yet.
        self._block_vars = {}      # block_start(int) -> tk.BooleanVar
        self._block_order = []     # block_start(int), first-seen order
        self._block_labels = {}    # block_start(int) -> menu/legend label (English)
        self._live_session = False    # set each poll_once(); see _block_key()
        self._block_menubtn = ttk.Menubutton(toolbar_parent, text="📦 Dataset")
        self._block_menu = tk.Menu(self._block_menubtn, tearoff=False)
        self._block_menubtn["menu"] = self._block_menu
        self._block_menubtn.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Separator(toolbar_parent, orient="vertical").pack(
            side=tk.LEFT, fill=tk.Y, padx=8)

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
            # A SECOND scan on the SAME date used to have no run-number
            # filter at all here ("None = take every run for this date"),
            # so its points got mixed in with whatever an earlier scan today
            # had already written -- one noisy-looking overlaid curve
            # instead of two separate ones (2026-08-28). start_general_scan()
            # now records the highest run number that existed BEFORE this
            # scan started; only run numbers above that belong to it.
            # ('min', N) is a distinct, hashable marker (not a real run
            # number set) -- poll_once() below filters on it directly rather
            # than materializing every run > N into a set.
            # Filters by wall-clock TIME now, not run number (was ('min', N):
            # "run number > N belongs to this scan"). _assign_run_block picks
            # the next 100-run block by scanning for the highest one already
            # used today -- but once the daily backup reclaims a block's
            # local RAW files, that evidence disappears and the block can
            # look "free" again, so a later scan the same day can be
            # reassigned a LOWER block than an earlier one (2026-08-29: a
            # scan starting at run 000 after an earlier one had already
            # reached run 145 -- every one of its points was <= the old
            # min_run and the plot showed nothing all scan long). A run
            # number can be reused; a wall-clock timestamp cannot.
            start_epoch_s = os.environ.get("SCAN_START_EPOCH", "")
            if start_epoch_s:
                try:
                    return date, ('after', float(start_epoch_s))
                except ValueError:
                    pass
            return date, None    # fallback: no threshold recorded, take every run for this date

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
        # block_runs is either: None (no filter), a set of exact run numbers
        # (idle-fallback's contiguous-block detection), or ('after', epoch)
        # (a live scan -- only files WRITTEN after that moment are this
        # scan's own). All three are already hashable as-is; only the plain
        # set needed frozenset()ing.
        is_after_marker = isinstance(block_runs, tuple) and block_runs[:1] == ('after',)
        # A live scan's own points span run-number blocks purely because the
        # run counter wrapped past 99 -- treat them as ONE dataset regardless
        # of //100 block boundary (see _block_key()). Only in idle/history
        # mode does a //100 block boundary mean a genuinely separate dataset
        # (2026-08-29, user: "지금 뭐가 문제인지 모르겠는데 2세트가 뜨네" --
        # a single still-running 92-point scan split into 000-099/100-199
        # and shown as two toggleable "recordings").
        self._live_session = is_after_marker
        key = (date, block_runs if (block_runs is None or is_after_marker)
               else frozenset(block_runs))
        if key != self._current_block:
            # A different block than what's on screen (new scan started, or
            # the idle-fallback moved to a newer finished block) -> clear.
            self._current_block = key
            self._points.clear()
            self._scanned_files.clear()

        pattern = os.path.join(self._result_dir(), f"precal_result_kor_run_{date}_*.root")
        files = sorted(glob.glob(pattern))
        if is_after_marker:
            start_epoch = block_runs[1]
            files = [f for f in files if os.path.getmtime(f) >= start_epoch]
        elif block_runs is not None:
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
            self._refresh_block_menu()
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
    def _series(self, ch, block=None):
        """(x, y, yerr, runs) for one channel, current metric + axis settings.

        With Monitor-normalize on and ch != 0, y becomes ch_value / mon_value
        from the SAME run (relative errors added in quadrature) -- this is the
        same ratio Draw_Uniformity_Norm_v7's MonNorm_* graphs compute, just
        per-point and live instead of over a finished block.

        block=None: every block the Dataset checklist has selected, merged
        into one series (used when only one block is selected -- the common
        case). block=<int>: just that one block's points, regardless of the
        checklist -- used by redraw() to plot each overlaid dataset as its
        own line/color/legend entry instead of one blended line."""
        xkey = "raw" if self._axis_mode.get() == "raw" else "ham"
        mkey = self._metric.get()
        want = self._axis_filter.get()
        do_norm = self._mon_norm.get() and ch != 0
        sel_blocks = {block} if block is not None else self._selected_blocks()
        xs, ys, es, runs = [], [], [], []
        for run in sorted(self._points):
            if sel_blocks is not None and self._block_key(run) not in sel_blocks:
                continue
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
        self._callout = None    # ax.clear() above already dropped the artist

        self._pick_map = {}
        any_data = False
        mkey = self._metric.get()
        metric_label = self.METRICS[mkey][0]

        # Plot each selected Dataset block as its OWN line/color/legend entry
        # instead of blending them into one -- overlaying two blocks used to
        # draw as a single mixed-color line with no way to tell which point
        # came from which dataset (2026-08-29, user: "그래프에 여러 데이터가
        # 겹쳐서 나와 구분이 전혀 안 갑니다"). Older blocks are drawn more
        # transparent so the currently-running one still reads as "the" line
        # at a glance. Block tags are only appended to the legend label when
        # more than one block is actually on screen, so the common single-
        # block case stays exactly as clean as before.
        sel_blocks = self._selected_blocks()
        blocks_now = sorted({self._block_key(run) for run in self._points})
        active_blocks = (sorted(sel_blocks) if sel_blocks is not None else blocks_now) or [None]
        multi = len(active_blocks) > 1

        for ch in (1, 2):
            marker, color = self.CH_STYLE[ch]
            for i, b in enumerate(active_blocks):
                xs, ys, es, runs = self._series(ch, block=b)
                if not xs:
                    continue
                any_data = True
                sn = self._points[runs[0]]["sn"].get(ch, f"ch{ch}")
                label = sn if not multi else f"{sn} - {self._block_labels.get(b, str(b))}"
                alpha = 1.0 if (b == max(blocks_now, default=b)) else 0.45
                line = self.ax_test.errorbar(
                    xs, ys, yerr=es, fmt=marker, ms=4.5, capsize=2,
                    color=color, alpha=alpha, label=label, picker=6)
                self._pick_map[line.lines[0]] = (ch, runs)

        marker, color = self.MON_STYLE
        for b in active_blocks:
            xs, ys, es, runs = self._series(0, block=b)
            if not xs:
                continue
            any_data = True
            sn = self._points[runs[0]]["sn"].get(0, "monitor")
            label = f"{sn} (monitor)" if not multi else f"{sn} (mon.) - {self._block_labels.get(b, str(b))}"
            alpha = 1.0 if (b == max(blocks_now, default=b)) else 0.45
            mon_line = self.ax_mon.errorbar(
                xs, ys, yerr=es, fmt=marker, ms=4, capsize=2,
                color=color, alpha=alpha, label=label, picker=6)
            self._pick_map[mon_line.lines[0]] = (0, runs)

        xlabel = ("Raw Stage angle [degree]" if self._axis_mode.get() == "raw"
                  else "Hamamatsu incidence angle [degree]")
        top_label = (f"{metric_label} / Monitor" if self._mon_norm.get() else metric_label)
        self.ax_test.set_ylabel(top_label)
        self.ax_mon.set_ylabel(metric_label.split(" (")[0].split(" [")[0] + " (mon.)")
        self.ax_mon.set_xlabel(xlabel)
        self.ax_test.tick_params(labelbottom=False)
        # Which scan this is, in the corner -- with a fresh run-number filter
        # per scan (see _target_block's ('min', N) marker) it's now always
        # exactly one scan's data on screen, but a screenshot or a glance
        # back after switching tabs has no other way to tell WHICH one
        # (2026-08-28, user asked for this once the multi-scan mixing was
        # fixed). Not a legend entry (that's per-channel, see ax.legend below)
        # -- this is a single small text label identifying the dataset itself.
        date, block_runs = self._current_block if self._current_block else (None, None)
        if date and self._points:
            runs = sorted(self._points.keys())
            self.fig.suptitle(f"{date}  ·  runs {runs[0]}–{runs[-1]}  ({len(runs)} pts)",
                              fontsize=8, color="#555555", x=0.99, y=0.995, ha="right")

        if any_data:
            for ax in (self.ax_test, self.ax_mon):
                self._reserve_legend_headroom(ax)
                leg = ax.legend(loc="upper right", fontsize=(6.5 if multi else 7),
                                framealpha=0.9, markerscale=0.8)
                # Explicit zorder so a callout bubble (drawn on pick, zorder
                # below) is never trapped underneath the legend box -- with
                # multiple blocks overlaid the legend has more entries and
                # covers more of the corner, so this matters more than before
                # (2026-08-29, user: "메시지 박스를 범례가 가리는 문제").
                leg.set_zorder(5)
            self._status.config(text=f"{len(self._points)} points", foreground="#1a7f37")
        else:
            self.ax_test.text(0.5, 0.5, "No scan points yet.\nPoints appear here as each "
                                        "one finishes analysis.",
                              ha="center", va="center", transform=self.ax_test.transAxes,
                              color="#888", fontsize=10)
            self._status.config(text="waiting for data…", foreground="#888")

        self.canvas.draw_idle()

    def _on_pick(self, event):
        """Click a point -> show all 4 metrics (QE/Gain/TTS/ChargeRes) for
        that point in a callout bubble anchored right on the plot, instead of
        (or in addition to) a plain log line -- a log entry only tells you
        the ONE metric currently selected in the radio buttons and you have
        to go find it in the Log tab; the point itself already carries all
        four, no reason not to show them all where you clicked (2026-08-28,
        user: "그래프 누르면 QE,Charge,TTS,Resolution 결과가 말풍선 박스로")."""
        entry = self._pick_map.get(event.artist)
        if not entry or not len(event.ind):
            return
        ch, runs = entry
        run = runs[event.ind[0]]
        p = self._points.get(run)
        if not p:
            return

        sn = p["sn"].get(ch, f"ch{ch}")
        lines = [f"Run {run:03d}  ·  {sn}",
                f"raw={p['raw'][ch]:.1f}°  ham={p['ham'][ch]:.2f}°"]
        for mk, (label, *_rest) in self.METRICS.items():
            val, err = p["data"].get(mk, {}).get(ch, (None, None))
            if val is None:
                continue
            short = label.split(" (")[0].split(" [")[0]
            lines.append(f"{short}: {val:.3g}" + (f" ± {err:.2g}" if err else ""))
        text = "\n".join(lines)

        ax = self.ax_test if ch in (1, 2) else self.ax_mon
        idx = event.ind[0]
        xdata, ydata = event.artist.get_data()
        x0, y0 = xdata[idx], ydata[idx]

        if self._callout is not None:
            try:
                self._callout.remove()
            except Exception:
                pass
            self._callout = None

        # Flip the callout to whichever side of the point has room, instead
        # of always offsetting (+14,+14) -- a point near the right or top
        # edge pushed the box outside the axes (clipped, or hanging off the
        # figure) where it was unreadable (2026-08-29, user: "오른쪽 포인트를
        # 가리키면 가려져서"). Measured against the DATA range, not pixels, so
        # it doesn't need a draw pass first.
        xlo, xhi = ax.get_xlim(); ylo, yhi = ax.get_ylim()
        near_right = (x0 - xlo) / (xhi - xlo) > 0.7 if xhi > xlo else False
        near_top = (y0 - ylo) / (yhi - ylo) > 0.7 if yhi > ylo else False
        dx = -14 if near_right else 14
        dy = -14 if near_top else 14
        ha = "right" if near_right else "left"
        va = "top" if near_top else "bottom"

        # Dark, rounded, semi-transparent bubble matching the app's own
        # console palette (bar_bg #1b1f2a / accent #3ddc84) instead of the
        # old plain yellow sticky-note box (2026-08-29, user: "메시지 박스의
        # 디자인이... 너무 투박합니다"). zorder pinned above the legend
        # (zorder=5, set above) so the two never fight for the same corner.
        self._callout = ax.annotate(
            text, xy=(x0, y0), xytext=(dx, dy), textcoords="offset points",
            fontsize=8, family="monospace", color="#e8ecf4", ha=ha, va=va,
            zorder=50,
            bbox=dict(boxstyle="round,pad=0.6,rounding_size=0.8",
                      fc="#1b1f2a", ec="#3ddc84", lw=1.1, alpha=0.94),
            arrowprops=dict(arrowstyle="->", color="#3ddc84", lw=1.2))
        self.canvas.draw_idle()

        show = getattr(self.controller, "show_scan_point_card", None)
        if show:
            show(run, p)

    def reset(self):
        """Drop accumulated points (called when a new scan starts)."""
        self._points.clear()
        self._scanned_files.clear()
        self._current_block = None
        self._block_vars.clear()
        self._block_order.clear()
        self._block_labels.clear()
        self._refresh_block_menu()
        self.redraw()

    def _block_key(self, run):
        """Which Dataset-checklist group a run belongs to. During a live scan
        every point is the SAME dataset no matter which //100 block its run
        number falls in (the block boundary is just where the run counter
        wrapped); only in idle/history mode does //100 mark a genuinely
        different past scan."""
        return 0 if self._live_session else (run // 100) * 100

    def _refresh_block_menu(self):
        """Rebuild the Dataset checklist from whatever blocks are currently
        in self._points. The highest-numbered block is always "Live scan"
        (still growing, or the last one touched) and defaults ON; every
        other block is "Record N" in the order it was first seen and
        defaults OFF, UNLESS the operator already set that block's checkbox
        themselves -- an existing BooleanVar's value is never overwritten,
        only newly-appeared blocks get a fresh default."""
        blocks_now = sorted({self._block_key(run) for run in self._points})
        live_block = max(blocks_now) if blocks_now else None
        for b in blocks_now:
            if b not in self._block_vars:
                self._block_order.append(b)
                # Only the block that IS live right now starts checked; this
                # matters when several blocks appear in the same refresh
                # (e.g. GUI restart backfilling old + active blocks at once)
                # -- without this, none of them counts as "superseded" below
                # and they'd all default ON.
                self._block_vars[b] = tk.BooleanVar(value=(b == live_block))
        # Re-derive defaults: a block becomes "Record N" (default OFF) only
        # the FIRST time it stops being the live one; after that the operator's
        # own choice sticks (no re-flagging on every poll).
        newly_superseded = getattr(self, "_last_live_block", None)
        if newly_superseded is not None and newly_superseded != live_block \
                and newly_superseded in self._block_vars \
                and not getattr(self, "_block_touched", {}).get(newly_superseded, False):
            self._block_vars[newly_superseded].set(False)
        self._last_live_block = live_block

        self._block_menu.delete(0, "end")
        ordered = [b for b in self._block_order if b in self._block_vars]
        # English throughout -- both the Menu label and the matplotlib
        # legend suffix use the same string. Matplotlib's default font can't
        # render Hangul anyway (would come out as tofu boxes), and program
        # text must be English project-wide (2026-08-29, user: "영어로
        # 써줘야해").
        #
        # Label by the actual date + actual runs present, not a synthetic
        # "Record N (000-099)" range that implies every run in that span
        # exists -- a scan that skipped/aborted points never has a clean
        # 0-99 run of files, so that range was misleading (2026-08-29, user:
        # "run도 000~099가 아니라 진짜 있는 런으로 해야지").
        date = self._current_block[0] if self._current_block else None
        self._block_labels = {}
        for b in ordered:
            runs_in_block = sorted(r for r in self._points if self._block_key(r) == b)
            rng = f"run {runs_in_block[0]}-{runs_in_block[-1]}" if runs_in_block else ""
            if b == live_block:
                label = f"Live scan ({rng})" if rng else "Live scan"
            else:
                label = f"{date} ({rng})" if date else f"Dataset ({rng})"
            self._block_labels[b] = label

            def _on_toggle(blk=b):
                self._block_touched = getattr(self, "_block_touched", {})
                self._block_touched[blk] = True
                self.redraw()
            self._block_menu.add_checkbutton(label=label, variable=self._block_vars[b],
                                             command=_on_toggle)
        if not ordered:
            self._block_menu.add_command(label="(no data yet)", state="disabled")

    def _selected_blocks(self):
        """Set of block_start values currently checked ON, or None if there
        is only ever been one block (no filtering needed -- avoids an empty
        plot before the menu has had a chance to build on first data)."""
        if not self._block_vars:
            return None
        return {b for b, v in self._block_vars.items() if v.get()}

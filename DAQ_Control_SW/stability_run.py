"""Stability Run: repeated acquisitions at a FIXED angle.

Distinct from General Scan, which moves the stage between every point. Here
nothing moves -- the same angle is measured over and over to watch QE/Gain/TTS
drift with time. The repetition is done by script_v7.sh's own
NumSequences/IntervalTime loop (one launch, N internal iterations), not by the
Python side.

SAFETY -- why this file writes config3.h and then puts it back:
On 2026-08-15 a Stability run left NumSequences=100 / IntervalTime=600 behind
in config3.h. The next General Scan inherited them, so every scan point
spawned a 100-iteration background loop whose angle arguments were frozen at
that point's values while the stage moved on -- ~20 orphaned acquisitions
writing wrong-angle data, surviving both Stop and an app restart.
Two independent guards now exist:
  1. AutomationManager._force_single_sequence_config() resets 1/0 at the start
     of EVERY General Scan, whatever was left behind.
  2. This module restores 1/0 as soon as its own run finishes or is stopped.
Guard 1 is the one that actually prevents the incident; guard 2 keeps the file
tidy so the next person reading config3.h isn't misled.
"""
import os
import csv
import re
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime, timedelta


class StabilityRunUI:
    # Effective acquisition rate, events/second, from the DAQ's own summary
    # line ("Total Events : 300000 / Elapsed Time : 300.1 s"). A constant, not
    # an input: it is set by the laser trigger rate, which the operator does
    # not change per run, and an editable field for it was just one more way
    # to end up with a wrong estimate. The value is shown in the per-
    # acquisition breakdown so the assumption stays visible.
    RATE_HZ = 1000.0
    # Fixed cost per iteration outside the acquisition itself: board open, DAC
    # settle, file close, analysis chain launch. Measured ~20-30 s.
    OVERHEAD_S = 25.0
    # RAW file size, measured on run 20260817_000: 923,609,147 bytes for
    # 300,000 events at TimeWindow=1024 -> 3079 B/event, i.e. ~3.007 B per
    # event per sample after ROOT compression. Scaling by TimeWindow keeps the
    # estimate roughly right when the record length changes; it is an estimate
    # either way, so the UI marks it with "~".
    BYTES_PER_EVENT_SAMPLE = 3078.7 / 1024.0
    DEFAULT_TIME_WINDOW = 1024
    # TEC/LD switch-on and settle per wavelength block (rotation_manager._prepare_laser_block).
    LASER_PREP_S = 20.0

    def __init__(self, parent, controller):
        self.controller = controller
        self.parent = parent
        self._running = False
        self._build(parent)

    # ------------------------------------------------------------------ UI
    def _build(self, parent):
        wrap = ttk.Frame(parent, padding=10)
        wrap.pack(fill=tk.BOTH, expand=True)

        ttk.Label(wrap, text="Stability Run — repeated acquisitions at a fixed angle",
                  font=("Helvetica", 13, "bold")).pack(anchor="w")
        ttk.Label(wrap, text="The stage does not move. Position the PMTs first "
                             "(Manual Control), then start.",
                  foreground="#666").pack(anchor="w", pady=(0, 10))

        # ── inputs ──────────────────────────────────────────────────────
        box = ttk.LabelFrame(wrap, text=" Run parameters ", padding=10)
        box.pack(fill=tk.X)
        # Only the LAST column absorbs slack. Giving the entry column weight
        # stretched the entries and shoved the unit labels to the far right
        # edge, leaving a comically wide gap mid-row (2026-08-26).
        box.columnconfigure(3, weight=1)

        self.v_events = tk.StringVar()
        self.v_interval = tk.StringVar(value="600")
        self.v_count = tk.StringVar(value="100")
        self.v_window = tk.StringVar(value=str(self.DEFAULT_TIME_WINDOW))

        def row(r, label, var, unit, tip=""):
            ttk.Label(box, text=label).grid(row=r, column=0, sticky="w", pady=3)
            e = ttk.Entry(box, textvariable=var, width=12)
            e.grid(row=r, column=1, sticky="w", padx=(6, 4))
            ttk.Label(box, text=unit, foreground="#666", width=7,
                      anchor="w").grid(row=r, column=2, sticky="w")
            if tip:
                ttk.Label(box, text=tip, foreground="#888",
                          font=("Helvetica", 9)).grid(row=r, column=3, sticky="w")
            var.trace_add("write", lambda *a: self._recalc())
            return e

        row(0, "Events per acquisition", self.v_events, "events", "from config3.h (Events)").config(state="readonly")
        self.e_interval = row(1, "Interval between acquisitions", self.v_interval, "s", "script_v7.sh IntervalTime")
        self.e_count = row(2, "Number of acquisitions", self.v_count, "count", "script_v7.sh NumSequences")

        self.v_multi = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text="Laser Sequence", variable=self.v_multi,
                        command=self._on_multi).grid(row=3, column=0, sticky="w", pady=3)
        self.l_seq = ttk.Label(box, text="", foreground="#b91c1c")
        self.l_seq.grid(row=3, column=1, columnspan=3, sticky="w", padx=(6, 0))
        self.seq_box = ttk.Frame(box)
        self.seq_box.grid(row=4, column=0, columnspan=4, sticky="w", pady=(4, 0))
        self.seq_vars, self._seq_key = {}, None

        # ── calculator ─────────────────────────────────────────────────
        calc = ttk.LabelFrame(wrap, text=" Duration ⇄ count ", padding=10)
        calc.pack(fill=tk.X, pady=(10, 0))
        calc.columnconfigure(1, weight=1)

        ttk.Label(calc, text="Per acquisition:").grid(row=0, column=0, sticky="w")
        self.l_per = ttk.Label(calc, text="—", font=("Helvetica", 11, "bold"))
        self.l_per.grid(row=0, column=1, sticky="w", padx=(8, 0))

        ttk.Label(calc, text="Total duration:").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.l_total = ttk.Label(calc, text="—", font=("Helvetica", 11, "bold"),
                                 foreground="#1a5fb4")
        self.l_total.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(4, 0))

        ttk.Label(calc, text="Would finish at:").grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.l_end = ttk.Label(calc, text="—", font=("Helvetica", 11, "bold"),
                               foreground="#1a5fb4")
        self.l_end.grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(4, 0))

        # Disk is the constraint that actually stopped a run before (local hit
        # 96% on 2026-08-22), so it belongs next to the duration rather than
        # being something to work out afterwards.
        ttk.Label(calc, text="Disk needed:").grid(row=3, column=0, sticky="w", pady=(4, 0))
        self.l_disk = ttk.Label(calc, text="—", font=("Helvetica", 11, "bold"))
        self.l_disk.grid(row=3, column=1, columnspan=3, sticky="w", padx=(8, 0), pady=(4, 0))

        ttk.Separator(calc, orient="horizontal").grid(row=4, column=0, columnspan=4,
                                                      sticky="ew", pady=8)

        # Reverse direction: give it a window, get the count that fits.
        ttk.Label(calc, text="Or: available time").grid(row=5, column=0, sticky="w")
        self.v_hours = tk.StringVar(value="12")
        e = ttk.Entry(calc, textvariable=self.v_hours, width=10)
        e.grid(row=5, column=1, sticky="w", padx=(8, 4))
        ttk.Label(calc, text="hours", foreground="#666").grid(row=5, column=2, sticky="w")
        self.v_hours.trace_add("write", lambda *a: self._recalc())
        self.l_fit = ttk.Label(calc, text="—", foreground="#1a5fb4",
                               font=("Helvetica", 11, "bold"))
        self.l_fit.grid(row=6, column=0, columnspan=4, sticky="w", pady=(4, 0))
        ttk.Button(calc, text="Use this count",
                   command=self._apply_fitted_count).grid(row=6, column=3, sticky="e")

        # ── run control ────────────────────────────────────────────────
        ctrl = ttk.Frame(wrap)
        ctrl.pack(fill=tk.X, pady=(12, 0))
        self.btn_start = tk.Button(ctrl, text="▶  Start Stability Run", height=2,
                                   bg="#2e9e4f", fg="white", relief="flat",
                                   font=("Helvetica", 11, "bold"),
                                   command=self.start_run)
        self.btn_start.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.btn_stop = tk.Button(ctrl, text="⏹  Stop", height=2, width=12,
                                  bg="#c0392b", fg="white", relief="flat",
                                  font=("Helvetica", 11, "bold"),
                                  state=tk.DISABLED, command=self.stop_run)
        self.btn_stop.pack(side=tk.LEFT, padx=(8, 0))

        self.l_status = ttk.Label(wrap, text="Idle.", foreground="#666")
        self.l_status.pack(anchor="w", pady=(8, 0))

        warn = tk.Label(
            wrap,
            text=("While this runs, config3.h holds NumSequences/IntervalTime for the repeat "
                  "loop. They are restored to 1/0 when it ends, and a General Scan resets them "
                  "on its own start regardless — so a leftover value cannot affect a scan."),
            bg="#fff3cd", fg="#7a5b00", anchor="w", justify="left",
            wraplength=760, padx=10, pady=6)
        warn.pack(fill=tk.X, pady=(10, 0))

        self._load_events_from_config()
        self._recalc()
        self._refresh_seq()

    # ------------------------------------------------------------- helpers
    def _load_events_from_config(self):
        for key, var, default in (("Events", self.v_events, "300000"),
                                  ("TimeWindow", self.v_window, str(self.DEFAULT_TIME_WINDOW))):
            try:
                val = self.controller.config_manager.get_config_value(key)
                if val:
                    var.set(str(val).strip())
            except Exception:
                pass
            if not var.get():
                var.set(default)

    def _laser_sequence(self):
        """Checked rows of General Scan's Laser Sequence panel as [(wl, bias, pulse)];
        None when a checked row has a non-numeric current."""
        vars_ = getattr(getattr(self.controller, "auto_ui", None), "laser_seq_vars", None) or {}
        seq = []
        for wl in ["405nm", "375nm", "450nm", "473nm"]:
            v = vars_.get(wl)
            if not v or not v["on"].get():
                continue
            try:
                seq.append((wl, float(v["bias"].get()), float(v["pulse"].get())))
            except (ValueError, tk.TclError):
                return None
        return seq

    def _on_multi(self):
        state = tk.DISABLED if self.v_multi.get() else tk.NORMAL
        self.e_interval.config(state=state)
        self.e_count.config(state=state)
        self._seq_key = None
        self._refresh_seq(reschedule=False)

    def _sync_config(self):
        """Follow Events/TimeWindow in config3.h: the DAQ reads them from there, not from this tab."""
        try:
            path = self.controller.config_manager.filepath
            mtime = os.path.getmtime(path)
        except Exception:
            return
        if mtime == getattr(self, "_cfg_mtime", None):
            return
        self._cfg_mtime = mtime
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            return
        for key, var in (("Events", self.v_events), ("TimeWindow", self.v_window)):
            m = re.search(r"const\s+(?:int|std::string)\s+%s\s*=\s*\"?(\d+)\"?\s*;" % key, text)
            if m and var.get() != m.group(1):
                var.set(m.group(1))
                self._recalc()

    def _refresh_seq(self, reschedule=True):
        self._sync_config()
        seq = self._laser_sequence() if self.v_multi.get() else []
        key = (self.v_multi.get(), tuple(seq) if seq is not None else None)
        if key != self._seq_key:
            self._seq_key = key
            self._build_seq_rows(seq or [])
            if not self.v_multi.get():
                self.l_seq.config(text="")
            elif seq is None:
                self.l_seq.config(text="Invalid Bias/Pulse value in the General Scan Laser Sequence panel")
            elif not seq:
                self.l_seq.config(text="No wavelength checked in the General Scan Laser Sequence panel")
            else:
                self.l_seq.config(text="")
            self._recalc()
        if reschedule:
            try:
                self.parent.after(2000, self._refresh_seq)
            except tk.TclError:
                pass

    def _build_seq_rows(self, seq):
        for w in self.seq_box.winfo_children():
            w.destroy()
        if not seq:
            return
        for c, h in enumerate(["Wavelength", "Pulse", "Length", "", "Interval", "Count", "Time"]):
            ttk.Label(self.seq_box, text=h, foreground="#666").grid(row=0, column=c, sticky="w", padx=(0, 10))
        for r, (wl, _b, pulse) in enumerate(seq, start=1):
            v = self.seq_vars.get(wl)
            if v is None:
                v = {"len": tk.StringVar(value="1"), "unit": tk.StringVar(value="h"),
                     "iv": tk.StringVar(value=self.v_interval.get() or "600")}
                for var in (v["len"], v["unit"], v["iv"]):
                    var.trace_add("write", lambda *a: self._recalc())
                self.seq_vars[wl] = v
            ttk.Label(self.seq_box, text=wl, font=("Helvetica", 10, "bold")).grid(
                row=r, column=0, sticky="w", padx=(0, 10), pady=2)
            ttk.Label(self.seq_box, text=f"{pulse:g} mA").grid(row=r, column=1, sticky="w", padx=(0, 10))
            ttk.Entry(self.seq_box, textvariable=v["len"], width=7).grid(row=r, column=2, sticky="w")
            ttk.Combobox(self.seq_box, textvariable=v["unit"], values=["h", "count"], width=6,
                         state="readonly").grid(row=r, column=3, sticky="w", padx=(4, 10))
            f = ttk.Frame(self.seq_box)
            f.grid(row=r, column=4, sticky="w", padx=(0, 10))
            ttk.Entry(f, textvariable=v["iv"], width=7).pack(side=tk.LEFT)
            ttk.Label(f, text="s", foreground="#666").pack(side=tk.LEFT, padx=(3, 0))
            v["l_cnt"] = ttk.Label(self.seq_box, text="—")
            v["l_cnt"].grid(row=r, column=5, sticky="w", padx=(0, 10))
            v["l_time"] = ttk.Label(self.seq_box, text="—")
            v["l_time"].grid(row=r, column=6, sticky="w")

    def _plan(self):
        """[(block, count, interval)] for the run, block None = current laser only; None if invalid."""
        n = self._nums()
        if not n:
            return None
        ev, rate, iv, cnt = n
        if not self.v_multi.get():
            return [(None, cnt, iv)]
        seq = self._laser_sequence()
        if not seq:
            return None
        acq = ev / rate + self.OVERHEAD_S
        plan = []
        for block in seq:
            v = self.seq_vars.get(block[0])
            try:
                length, biv = float(v["len"].get()), float(v["iv"].get())
            except (TypeError, ValueError, tk.TclError):
                return None
            if length <= 0 or biv < 0:
                return None
            c = int((length * 3600 + biv) // (acq + biv)) if v["unit"].get() == "h" else int(length)
            if c <= 0:
                return None
            plan.append((block, c, biv))
        return plan

    def _raw_dir(self):
        """Where RAW files land, for the free-space check."""
        getp = getattr(self.controller, "_get_daq_path", None)
        base = None
        if getp:
            try:
                base = getp()
            except Exception:
                base = None
        base = base or os.path.expanduser("~/ADC/ADC_test")
        d = os.path.join(base, "Data", "RAW", "Laser")
        return d if os.path.isdir(d) else os.path.expanduser("~")

    @staticmethod
    def _fmt_size(nbytes):
        gb = nbytes / (1024 ** 3)
        if gb >= 1024:
            return f"{gb / 1024:.2f} TB"
        if gb >= 1:
            return f"{gb:.2f} GB"
        return f"{nbytes / (1024 ** 2):.0f} MB"

    def _nums(self):
        """Parsed inputs, or None when any field isn't usable yet."""
        try:
            ev = float(self.v_events.get())
            rate = self.RATE_HZ
            iv = float(self.v_interval.get())
            cnt = int(float(self.v_count.get()))
        except (ValueError, TypeError):
            return None
        if ev <= 0 or rate <= 0 or iv < 0 or cnt <= 0:
            return None
        return ev, rate, iv, cnt

    @staticmethod
    def _fmt(seconds):
        seconds = int(max(0, seconds))
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}h {m:02d}m {s:02d}s"
        if m:
            return f"{m}m {s:02d}s"
        return f"{s}s"

    def _block_time(self, acq, cnt, iv):
        # The interval sits BETWEEN acquisitions, so N acquisitions have N-1
        # gaps -- counting N gaps overstates a 100-point run by 10 minutes.
        return cnt * acq + max(0, cnt - 1) * iv

    def _total_time(self, plan, acq):
        prep = self.LASER_PREP_S * len(plan) if plan[0][0] is not None else 0.0
        return sum(self._block_time(acq, c, i) for _b, c, i in plan) + prep

    def _recalc(self, *_):
        if not hasattr(self, "l_disk"):
            return
        n = self._nums()
        plan = self._plan()
        for v in self.seq_vars.values():
            if "l_cnt" in v:
                try:
                    v["l_cnt"].config(text="—")
                    v["l_time"].config(text="—")
                except tk.TclError:
                    pass
        if not n or not plan:
            for lbl in (self.l_per, self.l_total, self.l_end, self.l_fit, self.l_disk):
                lbl.config(text="—")
            return
        ev, rate, iv, cnt = n
        acq = ev / rate + self.OVERHEAD_S
        multi = plan[0][0] is not None
        for block, c, biv in plan:
            if block is not None and "l_cnt" in self.seq_vars[block[0]]:
                self.seq_vars[block[0]]["l_cnt"].config(text=str(c))
                self.seq_vars[block[0]]["l_time"].config(text=self._fmt(self._block_time(acq, c, biv)))
        nb = len(plan)
        n_acq = sum(c for _b, c, _i in plan)
        prep = self.LASER_PREP_S * nb if multi else 0.0
        total = self._total_time(plan, acq)

        self.l_per.config(text=f"{self._fmt(acq)}   "
                               f"(acquisition {self._fmt(ev / rate)} at {rate:.0f} Hz "
                               f"+ overhead {int(self.OVERHEAD_S)}s)")
        self.l_total.config(text=f"{self._fmt(total)}   ({total / 3600:.2f} h)"
                                 + (f"   {nb} wavelengths, {n_acq} acquisitions" if multi else ""))
        self.l_end.config(text=(datetime.now() + timedelta(seconds=total)).strftime("%Y-%m-%d %H:%M:%S"))

        # Disk: RAW dominates (the produced/result files are ~3% of it), and
        # RAW is written locally before the nightly backup moves it off.
        try:
            window = float(self.v_window.get())
        except (ValueError, TypeError):
            window = self.DEFAULT_TIME_WINDOW
        per_run = ev * window * self.BYTES_PER_EVENT_SAMPLE
        need = per_run * n_acq
        try:
            st = os.statvfs(self._raw_dir())
            free = st.f_bavail * st.f_frsize
        except OSError:
            free = None
        txt = f"~{self._fmt_size(need)}  ({self._fmt_size(per_run)} x {n_acq})"
        if free is None:
            self.l_disk.config(text=txt, foreground="#333")
        elif need > free:
            self.l_disk.config(
                text=f"{txt}   —   only {self._fmt_size(free)} free, WILL NOT FIT",
                foreground="#b91c1c")
        elif need > free * 0.8:
            self.l_disk.config(
                text=f"{txt}   —   {self._fmt_size(free)} free, leaves little margin",
                foreground="#a15c00")
        else:
            self.l_disk.config(text=f"{txt}   —   {self._fmt_size(free)} free",
                               foreground="#1a7f37")

        try:
            hours = float(self.v_hours.get())
        except (ValueError, TypeError):
            self.l_fit.config(text="—")
            return
        if hours <= 0 or multi:
            self.l_fit.config(text="—")
            return
        window = hours * 3600
        # Invert total = cnt*acq + (cnt-1)*iv  ->  cnt = (window + iv) / (acq + iv)
        fitted = int((window + iv) // (acq + iv))
        self._fitted = max(0, fitted)
        if self._fitted <= 0:
            self.l_fit.config(text=f"Not even one acquisition fits in {hours:g} h.")
        else:
            used = self._block_time(acq, self._fitted, iv)
            self.l_fit.config(text=f"{self._fitted} acquisitions fit in {hours:g} h (uses {self._fmt(used)})")

    def _apply_fitted_count(self):
        if getattr(self, "_fitted", 0) > 0:
            self.v_count.set(str(self._fitted))

    # --------------------------------------------------------------- config
    def _write_repeat_config(self, num_sequences, interval_time):
        """Set NumSequences/IntervalTime in config3.h (atomic write)."""
        path = self.controller.config_manager.filepath
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        new = re.sub(r"const int NumSequences\s*=\s*\d+\s*;",
                     f"const int NumSequences = {int(num_sequences)};", content)
        new = re.sub(r"const int IntervalTime\s*=\s*\d+\s*;",
                     f"const int IntervalTime = {int(interval_time)};", new)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(new); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
        try:
            self.controller.config_manager.reload()
        except Exception:
            pass

    def _restore_repeat_config(self):
        try:
            self._write_repeat_config(1, 0)
            self.controller._log("[INFO] Stability Run: NumSequences/IntervalTime restored to 1/0.")
        except Exception as e:
            self.controller._log(f"[ERROR] Stability Run: could not restore config3.h: {e}")

    # ------------------------------------------------------------------ run
    def start_run(self):
        if self._running:
            return
        if not getattr(getattr(self.controller, "access_mgr", None), "unlocked", True):
            messagebox.showwarning("Locked", "Please unlock controls first.")
            return
        auto = getattr(self.controller, "auto_mgr", None)
        if auto is not None and getattr(auto, "is_running", False):
            messagebox.showwarning("General Scan running",
                                   "A General Scan is in progress. Stop it before starting a "
                                   "Stability Run — both drive the same digitizer.")
            return
        n = self._nums()
        if not n:
            messagebox.showerror("Stability Run", "Check the run parameters — "
                                                  "some field is empty or non-numeric.")
            return
        ev, rate, iv, cnt = n
        plan = self._plan()
        if not plan:
            messagebox.showerror("Stability Run", "Laser Sequence: check the wavelengths and each row's "
                                                  "Length / Interval.")
            return
        seq = [b for b, _c, _i in plan if b is not None]
        acq = ev / rate + self.OVERHEAD_S
        n_acq = sum(c for _b, c, _i in plan)
        total = self._total_time(plan, acq)
        end = (datetime.now() + timedelta(seconds=total)).strftime("%Y-%m-%d %H:%M:%S")

        try:
            window = float(self.v_window.get())
        except (ValueError, TypeError):
            window = self.DEFAULT_TIME_WINDOW
        need = ev * window * self.BYTES_PER_EVENT_SAMPLE * n_acq
        try:
            st = os.statvfs(self._raw_dir())
            free = st.f_bavail * st.f_frsize
        except OSError:
            free = None

        disk_line = f"Estimated disk:     ~{self._fmt_size(need)}"
        if free is not None:
            disk_line += f"   ({self._fmt_size(free)} free)"
        if free is not None and need > free:
            if not messagebox.askyesno(
                    "Not enough disk space",
                    f"This run needs about {self._fmt_size(need)} but only "
                    f"{self._fmt_size(free)} is free.\n\n"
                    "It will fill the disk and the run will fail partway through.\n\n"
                    "Start anyway?", icon="warning"):
                return

        # config3.h is stamped into every RunInfo and cannot be fixed after the
        # fact, so show it here -- a 375 nm set was once recorded with
        # Laser = "0" simply because the field had never been saved.
        try:
            cfg_rows, cfg_warn = self.controller.config_manager.preflight_rows()
            cfg_block = "\n".join("  %-13s %s" % (lbl, val) for lbl, val in cfg_rows)
            cfg_text = "\nConfiguration (config3.h):\n" + cfg_block + "\n"
            if cfg_warn:
                cfg_text += "\n".join("  CHECK: " + w for w in cfg_warn) + "\n"
        except Exception as e:
            cfg_text = "\nConfiguration: could not be read (%s)\n" % e

        from hv_preflight import check_hv
        try:
            hv_cfg = self.controller.config_manager.get_all_variables()
        except Exception:
            hv_cfg = {}
        hv_rows, hv_block, hv_warn = check_hv(hv_cfg)
        if hv_block:
            msg = "\n".join(hv_block)
            self.controller._log(f"[CRITICAL] Stability Run not started -- {msg}")
            messagebox.showerror("HV: No Current", msg + "\n\nThe Stability Run was not started.")
            return
        cfg_text += "\nHV (from HV Monitor):\n" + "\n".join("  %-13s %s" % r for r in hv_rows) + "\n"
        if hv_warn:
            cfg_text += "\n".join("  CHECK: " + w for w in hv_warn) + "\n"

        if seq:
            head = (f"Acquisitions of {int(ev):,} events, one wavelength after another "
                    "(the previous LD is switched OFF first):\n"
                    + "\n".join(f"  {b[0]}   Pulse {b[2]:g} mA   {c} x, {int(i)} s apart   "
                                 f"({self._fmt(self._block_time(acq, c, i))})" for b, c, i in plan) + "\n\n")
        else:
            head = f"{cnt} acquisitions of {int(ev):,} events, {int(iv)} s apart.\n\n"
        if not messagebox.askyesno(
                "Start Stability Run",
                f"{head}"
                f"Estimated duration: {self._fmt(total)}\n"
                f"Estimated finish:   {end}\n"
                f"{disk_line}\n"
                f"{cfg_text}\n"
                "The stage will NOT move — make sure the PMTs are already at the "
                "angle you want to measure.\n\nStart now?"):
            return

        self._plan_run, self._blocks, self._block_idx = plan, [b for b, _c, _i in plan], 0
        self._skipped, self._last_code = [], None
        self._running = True
        self._run_started_at = datetime.now()
        self._planned = n_acq
        self._est_finish = datetime.now() + timedelta(seconds=total)
        self.btn_start.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)
        self.l_status.config(text=f"Running — {n_acq} acquisitions"
                                  + (f", {len(seq)} wavelengths" if seq else "") + f", est. finish {end}",
                             foreground="#1a7f37")
        self.controller._log("[INFO] Stability Run started: " + "; ".join(
            f"{b[0] if b else 'current laser'} NumSequences={c} IntervalTime={i:.0f}s" for b, c, i in plan)
            + f", est. finish {end}")

        # Slack start/finish/error, matching General Scan's own notifications
        # -- 2026-09-05, user: "Stabiltiy Run도 오류가 발생하면 Slack으로
        # 메세지 보내줘 / 시작과 끝도".
        notifier = getattr(self.controller, "notifier", None)
        if notifier and notifier.enabled:
            try:
                notifier.send("Stability Run started",
                              "".join(f"{b[0] if b else 'Current laser'}: {c} acquisitions, {int(i)} s apart\n"
                                      for b, c, i in plan)
                              + f"{int(ev):,} events each. Est. finish: {end}",
                              level="info", dedupe_key=None, blocking=False)
            except Exception as e:
                self.controller._log(f"[WARNING] Stability Run start-notification failed: {type(e).__name__}.")

        self._start_block()

    # ----------------------------------------------------- wavelength blocks
    def _sleep(self, seconds):
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(0.5)

    def _start_block(self):
        if not self._running:
            return
        block = self._blocks[self._block_idx]
        if block is None:
            self._launch_block(True)
            return
        n = len(self._blocks)
        self.l_status.config(text=f"Block {self._block_idx + 1}/{n}: preparing {block[0]} (TEC/LD ON)...",
                             foreground="#1a7f37")
        self.controller._log(f"[INFO] Stability Run block {self._block_idx + 1}/{n}: {block[0]}")

        def work():
            try:
                return self.controller.auto_mgr._prepare_laser_block(block, sleep=self._sleep)
            except Exception as e:
                self.controller._log(f"[ERROR] Stability Run: laser {block[0]} preparation failed: {e}")
                return False
        self._in_thread(work, self._launch_block)

    def _in_thread(self, work, then):
        """Run blocking laser I/O off the Tk thread; `then(result)` runs back on the Tk thread."""
        box = {}
        t = threading.Thread(target=lambda: box.setdefault("r", work()), daemon=True)
        t.start()

        def poll():
            if t.is_alive():
                self.parent.after(300, poll)
            else:
                then(box.get("r"))
        self.parent.after(300, poll)

    def _launch_block(self, ok):
        if not self._running:
            return
        block = self._blocks[self._block_idx]
        if block is not None:
            if not ok:
                self._skipped.append(block[0])
                self.controller._log(f"[WARNING] Stability Run: {block[0]} skipped (laser not ready).")
                self._next_block()
                return
        _b, cnt, iv = self._plan_run[self._block_idx]
        try:
            self._write_repeat_config(cnt, iv)
        except Exception as e:
            messagebox.showerror("Stability Run", f"Could not write config3.h:\n{e}")
            self._finish(f"Stopped: could not write config3.h ({e}).", exit_code=1)
            return
        if block is not None:
            wl, bias, pulse = block
            self.controller.auto_mgr._apply_laser_config(wl, pulse, bias)
            self.l_status.config(text=f"Block {self._block_idx + 1}/{len(self._blocks)}: {wl}, "
                                      f"{cnt} acquisitions, {int(iv)} s apart", foreground="#1a7f37")
        # script_v7.sh does the repeating internally, so this is ONE launch per
        # block -- the Python side must not also loop over acquisitions, or the
        # two would fight over the single digitizer (the 2026-08-15 failure mode).
        self.controller.run_daq(category="manual")
        self._watch_for_finish()

    def _block_finished(self, code):
        self._last_code = code
        block = self._blocks[self._block_idx]
        if code not in (None, 0):
            self._finish("Finished.", exit_code=code)
            return
        if block is None:
            self._next_block()
            return

        def work():
            try:
                self.controller.auto_mgr._finish_laser_block(block[0])
            except Exception as e:
                self.controller._log(f"[WARNING] Stability Run: {block[0]} LD OFF failed: {e}")
        self._in_thread(work, lambda _r: self._next_block())

    def _next_block(self):
        if not self._running:
            return
        self._block_idx += 1
        if self._block_idx < len(self._blocks):
            self._start_block()
        else:
            self._finish("Finished.", exit_code=self._last_code)

    def _run_ranges(self):
        """'405nm: 800-899' per wavelength, from the run registry rows of this run."""
        try:
            base = os.path.expanduser("~/ADC/ADC_test")
            getp = getattr(self.controller, "_get_daq_path", None)
            if getp:
                base = getp() or base
            path = os.path.join(base, "Data", "run_registry.csv")
            start = self._run_started_at.strftime("%Y-%m-%dT%H:%M:%S")
            runs = {}
            with open(path, newline="") as f:
                for r in csv.DictReader(f):
                    if r.get("registered_at", "") >= start and r.get("kind") in ("stability", "manual"):
                        runs.setdefault(r.get("wavelength", "?"), []).append(int(r["run"]))
            return [f"{wl} nm: runs {min(v):03d}-{max(v):03d} ({len(v)})" for wl, v in runs.items()]
        except Exception:
            return []

    def _watch_for_finish(self):
        """Poll the console slot; restore config once the launcher exits.

        BUG (found 2026-09-05, user: "Stability Run이... 끝나면 IntervalTime,
        Num sequence가 안바껴"): main.py's own reader thread clears
        _console_procs["daq"] to None as soon as it detects the process
        exited (see run_console_job's reader()/done()). If THIS poll (every
        5s) happens to land after that clear but we only ever treated
        "proc is not None and proc.poll() is not None" as finished, a proc
        that's already been nulled never satisfies that check -- it just
        reschedules forever and _restore_repeat_config() never runs. Since
        start_run() calls run_daq() (which sets the slot synchronously)
        BEFORE the first _watch_for_finish(), any None seen here can only
        mean "already finished and cleared", never "not started yet".
        """
        if not self._running:
            return
        procs = getattr(self.controller, "_console_procs", {})
        proc = procs.get("daq")
        if proc is None:
            # Slot already cleared by main.py -- finished, but the exit code
            # is gone with it, so we can't tell success from failure here.
            self._block_finished(None)
            return
        code = proc.poll()
        if code is not None:
            self._block_finished(code)
            return
        self.parent.after(5000, self._watch_for_finish)

    def stop_run(self):
        if not self._running:
            return
        if not messagebox.askyesno("Stop Stability Run",
                                   "Stop the current Stability Run?\n\n"
                                   "The acquisition in progress is terminated; data already "
                                   "written stays on disk."):
            return
        try:
            self.controller.stop_console_job("daq")
        except Exception as e:
            self.controller._log(f"[WARNING] Stability Run stop: {e}")
        self._finish("Stopped by operator.", exit_code=None, stopped_by_operator=True)

    def _finish(self, msg, exit_code=None, stopped_by_operator=False):
        if not self._running:
            return
        self._running = False
        self._restore_repeat_config()
        ranges = self._run_ranges() if any(getattr(self, "_blocks", [None])) else []
        if getattr(self, "_skipped", None):
            msg += f" Skipped (laser not ready): {', '.join(self._skipped)}."
        for line in ranges:
            self.controller._log(f"[INFO] Stability Run {line}")
        self.btn_start.config(state=tk.NORMAL)
        self.btn_stop.config(state=tk.DISABLED)
        self.l_status.config(text=msg, foreground="#666")
        self.controller._log(f"[INFO] Stability Run: {msg}")

        notifier = getattr(self.controller, "notifier", None)
        if notifier and notifier.enabled:
            try:
                start = getattr(self, "_run_started_at", None)
                dur_line = ""
                if start is not None:
                    dur_min = (datetime.now() - start).total_seconds() / 60.0
                    dur_line = f"Duration: {dur_min:.0f} min\n"
                dur_line += "".join(f"{line}\n" for line in ranges)
                if getattr(self, "_skipped", None):
                    dur_line += f"Skipped (laser not ready): {', '.join(self._skipped)}\n"
                if stopped_by_operator:
                    notifier.send("Stability Run stopped", f"{dur_line}Stopped by operator.",
                                  level="info", dedupe_key=None, blocking=False)
                elif exit_code is not None and exit_code != 0:
                    # A real failed launcher exit -- worth a warning-level ping,
                    # same severity General Scan uses for a BAD RUN.
                    notifier.send("Stability Run finished: FAILED",
                                  f"{dur_line}Exit code: {exit_code}\n"
                                  "Check the Console tab / TakingLog for details.",
                                  level="warning", dedupe_key=None, blocking=False)
                else:
                    # exit_code is None here in the (rare) race where main.py
                    # already cleared the slot before we could read it -- that
                    # just means we can't confirm success/failure, not that it
                    # failed, so this stays an info-level "done" ping rather
                    # than a warning.
                    notifier.send("Stability Run finished", f"{dur_line}".strip() or "Done.",
                                  level="info", dedupe_key=None, blocking=False)
            except Exception as e:
                self.controller._log(f"[WARNING] Stability Run finish-notification failed: {type(e).__name__}.")

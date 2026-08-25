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
import re
import time
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime, timedelta


class StabilityRunUI:
    # Effective acquisition rate, events/second. The DAQ's own summary line
    # ("Total Events : 300000 / Elapsed Time : 300.1 s") gives 1000 Hz; it is
    # exposed in the UI because it depends on trigger rate and can be
    # re-measured rather than trusted forever.
    DEFAULT_RATE_HZ = 1000.0
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
        self.v_rate = tk.StringVar(value=f"{self.DEFAULT_RATE_HZ:.0f}")
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

        row(0, "Events per acquisition", self.v_events, "events", "from config3.h (Events)")
        row(1, "Acquisition rate", self.v_rate, "Hz", "measured; edit if the trigger rate changed")
        row(2, "Interval between acquisitions", self.v_interval, "s", "script_v7.sh IntervalTime")
        row(3, "Number of acquisitions", self.v_count, "count", "script_v7.sh NumSequences")

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
            rate = float(self.v_rate.get())
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

    def _recalc(self, *_):
        n = self._nums()
        if not n:
            for lbl in (self.l_per, self.l_total, self.l_end, self.l_fit):
                lbl.config(text="—")
            return
        ev, rate, iv, cnt = n
        acq = ev / rate + self.OVERHEAD_S
        # The interval sits BETWEEN acquisitions, so N acquisitions have N-1
        # gaps -- counting N gaps overstates a 100-point run by 10 minutes.
        total = cnt * acq + max(0, cnt - 1) * iv

        self.l_per.config(text=f"{self._fmt(acq)}   "
                               f"(acquisition {self._fmt(ev / rate)} + overhead {int(self.OVERHEAD_S)}s)")
        self.l_total.config(text=f"{self._fmt(total)}   ({total / 3600:.2f} h)")
        self.l_end.config(text=(datetime.now() + timedelta(seconds=total)).strftime("%Y-%m-%d %H:%M:%S"))

        # Disk: RAW dominates (the produced/result files are ~3% of it), and
        # RAW is written locally before the nightly backup moves it off.
        try:
            window = float(self.v_window.get())
        except (ValueError, TypeError):
            window = self.DEFAULT_TIME_WINDOW
        per_run = ev * window * self.BYTES_PER_EVENT_SAMPLE
        need = per_run * cnt
        try:
            st = os.statvfs(self._raw_dir())
            free = st.f_bavail * st.f_frsize
        except OSError:
            free = None
        txt = f"~{self._fmt_size(need)}  ({self._fmt_size(per_run)} x {cnt})"
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
        if hours <= 0:
            self.l_fit.config(text="—")
            return
        window = hours * 3600
        # Invert total = cnt*acq + (cnt-1)*iv  ->  cnt = (window + iv) / (acq + iv)
        fitted = int((window + iv) // (acq + iv))
        self._fitted = max(0, fitted)
        if self._fitted <= 0:
            self.l_fit.config(text=f"Not even one acquisition fits in {hours:g} h.")
        else:
            used = self._fitted * acq + max(0, self._fitted - 1) * iv
            self.l_fit.config(text=f"{self._fitted} acquisitions fit in {hours:g} h "
                                   f"(uses {self._fmt(used)})")

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
        total = cnt * (ev / rate + self.OVERHEAD_S) + max(0, cnt - 1) * iv
        end = (datetime.now() + timedelta(seconds=total)).strftime("%Y-%m-%d %H:%M:%S")

        try:
            window = float(self.v_window.get())
        except (ValueError, TypeError):
            window = self.DEFAULT_TIME_WINDOW
        need = ev * window * self.BYTES_PER_EVENT_SAMPLE * cnt
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

        if not messagebox.askyesno(
                "Start Stability Run",
                f"{cnt} acquisitions of {int(ev):,} events, {int(iv)} s apart.\n\n"
                f"Estimated duration: {self._fmt(total)}\n"
                f"Estimated finish:   {end}\n"
                f"{disk_line}\n\n"
                "The stage will NOT move — make sure the PMTs are already at the "
                "angle you want to measure.\n\nStart now?"):
            return

        try:
            self._write_repeat_config(cnt, iv)
        except Exception as e:
            messagebox.showerror("Stability Run", f"Could not write config3.h:\n{e}")
            return

        self._running = True
        self.btn_start.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)
        self.l_status.config(text=f"Running — {cnt} acquisitions, est. finish {end}",
                             foreground="#1a7f37")
        self.controller._log(f"[INFO] Stability Run started: NumSequences={cnt}, "
                             f"IntervalTime={iv:.0f}s, est. finish {end}")

        # script_v7.sh does the repeating internally, so this is ONE launch --
        # the Python side must not also loop, or the two would fight over the
        # single digitizer (that is exactly the 2026-08-15 failure mode).
        self.controller.run_daq()
        self._watch_for_finish()

    def _watch_for_finish(self):
        """Poll the console slot; restore config once the launcher exits."""
        if not self._running:
            return
        procs = getattr(self.controller, "_console_procs", {})
        proc = procs.get("daq")
        if proc is not None and proc.poll() is not None:
            self._finish("Finished.")
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
        self._finish("Stopped by operator.")

    def _finish(self, msg):
        self._running = False
        self._restore_repeat_config()
        self.btn_start.config(state=tk.NORMAL)
        self.btn_stop.config(state=tk.DISABLED)
        self.l_status.config(text=msg, foreground="#666")
        self.controller._log(f"[INFO] Stability Run: {msg}")

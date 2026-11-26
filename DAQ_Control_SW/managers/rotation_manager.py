from datetime import datetime, timezone, timedelta
import threading
import time
import os
import re
import json
import glob
import subprocess
import signal
import shutil

from managers import angle_convert

class _TkDialogs:
    """tkinter.messagebox, imported on first use; a host without Tk passes its own as controller.dialogs."""

    def __getattr__(self, name):
        from tkinter import messagebox
        return getattr(messagebox, name)


_TK_DIALOGS = _TkDialogs()


class AutomationManager:
    # Whether Slack alerts name the Shifter/Expert (one switch for every call site).
    NOTIFY_INCLUDE_OPERATOR = True

    # See _mark_point_error: this many failures IN A ROW aborts the scan
    # instead of running to completion skipping every point.
    CONSECUTIVE_ERROR_LIMIT = 3

    # Failure-reason substring -> canned next step; a fixed set, not a guessed diagnosis.
    _ERROR_HINTS = (
        ("motor comm timeout", "Scan continues automatically (retry+skip already applied); check the stage if this repeats."),
        ("DAQ never started", "Console may have been busy with another job; check the Console tab."),
        ("digitizer not answering", "Power-cycle the digitizer and replug its USB cable, then check DAQ Status."),
        ("DAQ process hung", "execute_DAQ_v2 was force-killed by the watchdog; check the digitizer."),
        ("angles do not match", "Data was written but the angle doesn't match target; treat that run as suspect."),
        ("event count", "Run finished short of the expected event count; treat that run as suspect."),
        ("missing on disk", "RAW file not found in any known location (local or external); check Data Files."),
    )

    def _error_hint(self, reason):
        for needle, hint in self._ERROR_HINTS:
            if needle in reason:
                return hint
        return "See the log for detail."

    # Cache so a busy Slack/dialog moment doesn't re-glob+re-parse every
    # scanmap file on every call within the same process lifetime.
    _NOMINAL_PER_PT_CACHE = {}

    def _estimate_nominal_per_point_seconds(self, fallback=220.0):
        """Measured seconds per point for the ETA at scan start: median gap between consecutive point times in the
        recent scanmap_*.json files (long gaps from paused or resumed scans are dropped)."""
        hk = getattr(self, "daq_backend", "caen") == "hk"
        if hk in self._NOMINAL_PER_PT_CACHE:
            return self._NOMINAL_PER_PT_CACHE[hk]
        try:
            base = os.path.join(self.controller.base_dir, "LOG", "ScanHistory")
            files = sorted(glob.glob(os.path.join(base, "scanmap_*.json")),
                          key=os.path.getmtime, reverse=True)
            gaps, used = [], 0
            for fp in files:
                if used >= 5:
                    break
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    continue
                # only scans of the same DAQ backend: HK points are tens of seconds, CAEN points minutes
                entries = [e for e in data.values() if e.get("kind") == "scan" and e.get("time")
                           and str(e.get("file", "")).startswith("HK:") == hk]
                if not entries:
                    continue
                used += 1
                times = sorted(
                    datetime.strptime(e["time"], "%Y-%m-%d %H:%M:%S") for e in entries)
                for a, b in zip(times, times[1:]):
                    gap = (b - a).total_seconds()
                    # Skip non-positive gaps and gaps over 20 min (pauses or stops, not a point's cost).
                    if 0 < gap <= 1200:
                        gaps.append(gap)
            if len(gaps) < 5:
                return fallback   # not enough real data yet -- keep the old guess
            gaps.sort()
            nominal = gaps[len(gaps) // 2]   # median -- robust to the occasional slow point
            self._NOMINAL_PER_PT_CACHE[hk] = nominal
            return nominal
        except Exception:
            return fallback

    def _operator_line(self, shifter=None, expert=None):
        if not self.NOTIFY_INCLUDE_OPERATOR:
            return ""
        shifter = shifter or "-"
        expert = expert or "-"
        return f"Shifter: {shifter}  ·  Expert: {expert}"

    def daq_backend_label(self):
        """'CAEN' / 'HK Digitizer' / 'Dummy' -- recorded with every scan and shown in its notifications."""
        try:
            if self.controller.auto_ui.dummy_var.get():
                return "Dummy"
        except Exception:
            pass
        return "HK Digitizer" if getattr(self, "daq_backend", "caen") == "hk" else "CAEN"

    def _daq_line(self):
        label = self.daq_backend_label()
        if label == "HK Digitizer":
            return (f"DAQ: HK Digitizer :warning: data go to the HK PC "
                    f"({self.hk_config.get('ssh_target', '?')}), no CAEN data")
        return f"DAQ: {label}"

    def _notify_scan_started(self, cfg, sn2_name, sn3_name, shifter, expert):
        notifier = getattr(self.controller, 'notifier', None)
        if not notifier or not notifier.enabled:
            return
        try:
            points_per_axis = len(self.build_tilt_angles())
            n_wl = max(1, len(self.laser_sequence)) if getattr(self, 'laser_sequence', None) else 1
            total_steps = points_per_axis * 2 * n_wl
            is_dummy = self.controller.auto_ui.dummy_var.get()
            hk = self.hk_config
            nominal_per_pt = 1 if is_dummy else (
                self._estimate_nominal_per_point_seconds() if self.daq_backend != "hk"
                else float(hk.get("acq_time", 10)) + float(hk.get("move_delay", 20)) + self.daq_settle_time)
            eta_min = total_steps * nominal_per_pt / 60.0

            wl_list = ", ".join(w for w, _b, _p in self.laser_sequence) if getattr(self, 'laser_sequence', None) else "-"
            lines = [
                self._daq_line(),
                f"SN2 {sn2_name} ({cfg.get('direction2', '?')}) / SN3 {sn3_name} ({cfg.get('direction3', '?')})   cable",
                f"HV: {cfg.get('HV1','-')}/{cfg.get('HV2','-')}/{cfg.get('HV3','-')}   "
                f"Tilt step: {self.tilt_step}°   Points: {total_steps}",
                f"Wavelengths: {wl_list}",
                f"Est. duration: ~{eta_min:.0f} min",
            ]
            op = self._operator_line(shifter, expert)
            if op:
                lines.append(op)
            title = "General Scan started" + (" (HK Digitizer)" if self.daq_backend_label() == "HK Digitizer" else "")
            notifier.send(title, "\n".join(lines), level="info",
                          dedupe_key=None, blocking=False, key="scan_started")
        except Exception as e:
            self.controller._log(f"[WARNING] Scan-start notification failed: {type(e).__name__}.")

    def _notify_scan_finished(self, start, end, shifter, errors, warnings=(), skipped=()):
        notifier = getattr(self.controller, 'notifier', None)
        if not notifier or not notifier.enabled:
            return
        try:
            dur_min = (end - start).total_seconds() / 60.0
            warn_lines = [f"{len(warnings)} warning(s) from the file audit:"] + [f"  • {w}" for w in list(warnings)[:5]] \
                if warnings else []
            if errors or skipped:
                # One line per error kind (count + next step) instead of every point's detail.
                by_kind = {}
                for e in errors:
                    reason = e.split(":", 1)[1].strip() if ":" in e else e
                    matched = next((needle for needle, _ in self._ERROR_HINTS if needle in reason), None)
                    key = matched or reason
                    by_kind.setdefault(key, []).append(reason)
                lines = [self._daq_line()] + ([f"Wavelength(s) skipped: {', '.join(skipped)}"] if skipped else []) \
                    + ([f"{len(errors)} point(s) affected:"] if errors else [])
                for kind, occurrences in by_kind.items():
                    hint = self._error_hint(occurrences[0])
                    lines.append(f"  • {kind} ×{len(occurrences)} — {hint}")
                lines += warn_lines
                op = self._operator_line(shifter, getattr(self, '_current_expert', None))
                if op:
                    lines.append(op)
                why = [f"{len(errors)} error(s)"] * bool(errors) + [f"{len(skipped)} wavelength(s) skipped"] * bool(skipped)
                notifier.send(f"General Scan finished: BAD RUN ({', '.join(why)})",
                              "\n".join(lines), level="warning", dedupe_key=None, blocking=False,
                              key="scan_finished_bad")
            else:
                lines = [self._daq_line(), f"Duration: {dur_min:.0f} min"] + warn_lines
                op = self._operator_line(shifter, getattr(self, '_current_expert', None))
                if op:
                    lines.append(op)
                notifier.send("General Scan finished: GOOD RUN", "\n".join(lines),
                              level="info", dedupe_key=None, blocking=False, key="scan_finished_good")
        except Exception as e:
            self.controller._log(f"[WARNING] Scan-finish notification failed: {type(e).__name__}.")

    def __init__(self, controller):
        self.controller = controller
        self.is_running = False
        self.pause_event = threading.Event() 
        self.pause_event.set()               
        self.resume_data = None
        self.state_file = os.path.join(self.controller.base_dir, "scan_recovery_state.json")
        self.initial_angles = None
        self.reset_in_progress = False   # Reset Angle sequence running
        self._reset_cancel = False       # operator pressed Cancel on the reset dialog

        # Digitizer that takes the data: 'caen' (config3.h) or 'hk' (2nd PC, config3.h unused).
        self.daq_backend = "caen"

        # HK Digitizer settings (not config3.h): trigger, wait acq_time, wait move_delay, then rotate.
        self.hk_config_file = os.path.join(self.controller.base_dir, "hk_config.json")
        self.hk_config = {
            "run_number": 0,
            "acq_time": 10.0,      # --l : seconds of data-taking per point
            "move_delay": 20.0,    # extra wait AFTER acq before rotating
            "trigger_hz": 1000000.0,   # logic-unit trigger rate set when HK Digitizer is selected
            # ScanManager options, sent in this order after "-i <run_id> --l <acq>".
            # bare = flag only (sent when on); otherwise "flag value", omitted when value is blank.
            "scan_options": [
                {"label": "Threshold preset", "flag": "--threpreset", "value": "", "on": True, "bare": True},
                {"label": "Gate", "flag": "--gatelist", "value": "30", "on": True, "bare": False},
                {"label": "Pedestal run", "flag": "--pedestal", "value": "", "on": False, "bare": True},
                {"label": "Trigger ch", "flag": "--trgch", "value": "", "on": True, "bare": False},
                {"label": "Trigger Vth", "flag": "--trgch-Vth", "value": "", "on": True, "bare": False},
                {"label": "Channels", "flag": "--chanlist", "value": "", "on": True, "bare": False},
            ],
            # Commands run in order on each .bin just written; {bin} = that file, {base} = without .bin.
            "post_steps": [
                {"name": "Hit-tree", "on": False,
                 "cmd": 'RunAnalysisChain.py --inputBinFile "{bin}" -t "{base}_hittree.root"'},
                {"name": "Event-tree", "on": False,
                 "cmd": 'RunAnalysisChain.py --inputHitTreeFile "{base}_hittree.root" -T "{base}_eventtree.root"'},
            ],
            "work_dir": "~/hkelec/DiscreteSoftware/data/{date}/",  # cd'd into before ScanManager; {date}=YYYYMMDD
            "ssh_target": "hkpd@hkdaq",              # user@host to run the HK DAQ on
            "setup_cmd": ". ~/setup_hkelec.sh",      # sourced before ScanManager
            # Stage 2 (on hkpd): DPB board bring-up; its address lives only in dpb_host.
            "dpb_host": "192.168.0.163",
            "dpb_setup_cmd": ("ssh -t root@{dpb_host} 'cd /run/media/*/script && "
                              "bash run-socat.sh 1 9001 && bash run-socat.sh 4 9004 && "
                              "bash run-timing.sh'"),
            # Stage 3: vmodem via a minicom runscript written fresh on hkpd each time.
            "vmodem_device": "~/hkelec/DiscreteSoftware/DPBDaemon/vmodem/vmodem0",
            "vmodem_keys": "m,O",   # comma-separated keys sent in order via the runscript
            # Absolute path: a non-interactive SSH shell doesn't inherit the
            # interactive PATH, so the bare script name is "command not found".
            "scan_manager": "/home/hkpd/hkelec/pmt-scan/scripts/ScanManager_kawabata_precalib.py",
            # {run}/{tilt}/{rot}/{rot3} make every HK file name unique and record its angles (see hk_format_run_id).
            "run_id": "Run{run}_normal_elecV4SN03_testrunDARK_daqch0_trigchnone_hvON_laserOFF_T{tilt}_R2{rot}_R3{rot3}",  # filename (-i)
        }
        self._load_hk_config()

        # Session-only: DPB Setup / vmodem already run once (re-running asks first, not blocked).
        self.hk_dpb_setup_done = False
        self.hk_vmodem_done = False

        ######### Plz don't modified #########3
        self.tilt_step = 5.0
        self.rot_step = 45.0
        self.safe_move_step = 15.0  
        self.rest_time = 5.0

        # Per-device scan participation: a General Scan can hold Rot1 and/or Rot2 fixed.
        self.rotate_dev2 = True
        self.rotate_dev3 = True

        self.scan_range = {"start": -55, "end": 55}   # TILT mechanical limit
        self.rot_range  = {"start": 0, "end": 135}     # ROTATION mechanical limit
        # Tilt is always swept raw -55 -> +55; folded cables are corrected in analysis, not here.
        # Angles visited whatever tilt_step is (limits and boresight).
        self.mandatory_tilt_angles = [-55, 0, 55]
        # Explicit tilt list for one scan (set by the Daily Routine); None = the step grid.
        self.tilt_angles_override = None
        # Epoch time before which no Y-Axis data is taken in one scan (set by the Daily Routine); None = no wait.
        self.y_axis_start = None

        # Configurable via Danger Zone -> Params (admin-gated), same as
        # tilt_step/rot_step/rest_time above.
        self.daq_settle_time = 5.0   # seconds to wait after the motor arrives, before triggering DAQ
        # Angles re-measured on both axes after each wavelength block, tagged 'repeat' (empty = off).
        self.repeat_angles = []

        # Measured DAQ throughput (events/s) that sizes the hung-run watchdog; None until the first point.
        self.daq_rate_ema = None

        # Measured motor speed (deg/s) that sizes the per-step move timeout.
        self.motor_rate_ema = None

        # Slowest DAQ start-up seen this session (a max, not an average: too short a window corrupts data).
        self.daq_startup_max = 0.0

        self.schedule_file = os.path.join(self.controller.base_dir, "queued_schedules.json")
        self.schedules = [] 
        self._load_schedules_from_disk()
        self.schedule_thread_running = False
        self.history_dir = os.path.join(self.controller.base_dir, "LOG", "ScanHistory")
        os.makedirs(self.history_dir, exist_ok=True)

    def _dlg(self):
        return getattr(self.controller, "dialogs", None) or _TK_DIALOGS

    def scan_events(self, cfg=None):
        """Events per point of this scan: the one-scan override (Daily Routine) or config3.h Events."""
        if getattr(self, "events_override", None):
            return int(self.events_override)
        try:   # read the file itself, as script_v7.sh does: the cached copy can be stale
            cm = self.controller.config_manager
            disk = type(cm)(cm.filepath).get_config_value("Events")
            if disk:
                return int(float(disk))
        except Exception:
            pass
        cfg = cfg if cfg is not None else self.controller.config_manager.get_all_variables()
        return int(float(cfg.get("Events", 0) or 0))

    def build_tilt_angles(self):
        """Single source of truth for the tilt-angle list a General Scan
        visits -- the step grid (scan_range/tilt_step) with mandatory_tilt_angles
        merged in and clipped to scan_range, so a step that doesn't evenly
        divide the range can't silently drop -55/0/+55. Both the scan loop
        (_scan_axes_block) and the Scan Progress Matrix (ui_automation.py's
        _current_tilt_angles) call this so they can never disagree."""
        start, end = self.scan_range["start"], self.scan_range["end"]
        custom = getattr(self, "tilt_angles_override", None)
        if custom:
            angles = {int(a) for a in custom if start <= a <= end}
        else:
            step = int(self.tilt_step) if self.tilt_step else 5
            angles = set(range(start, end + 1, step))
        for a in self.mandatory_tilt_angles:
            if start <= a <= end:
                angles.add(int(a))
        return sorted(angles)

    def _wide_confirm(self, title, header, rows, question, accent="#0a84ff"):
        """A WIDE, readable Yes/No confirmation modal -- replaces
        messagebox.askyesno for the scan pre-flight checklists, whose fixed
        narrow width force-wrapped long lines (SSH target, ScanManager path,
        per-point timing) into an unreadable stack. Returns True/False.

        `rows` is a list of (label, value) pairs shown as an aligned two-column
        table; label=None renders a full-width separator/plain line. Runs on
        the Tk main thread (start_general_scan is a button handler), grabs
        input, and blocks until the operator answers."""
        hook = getattr(self._dlg(), "wide_confirm", None)
        if hook is not None and self._dlg() is not _TK_DIALOGS:
            return hook(title, header, rows, question, accent)
        import tkinter as tk
        parent = self.controller.master
        win = tk.Toplevel(parent)
        win.title(title)
        win.transient(parent)
        win.resizable(True, True)   # False,False also strips the min/max window buttons on this WM
        win.configure(bg="#ffffff")

        outer = tk.Frame(win, bg="#ffffff", padx=22, pady=18)
        outer.pack(fill=tk.BOTH, expand=True)

        tk.Label(outer, text=header, font=("Helvetica", 15, "bold"),
                 bg="#ffffff", fg=accent, anchor="w").pack(fill=tk.X, pady=(0, 12))

        table = tk.Frame(outer, bg="#ffffff")
        table.pack(fill=tk.X)
        table.columnconfigure(1, weight=1)
        r = 0
        for item in rows:
            label, value = item
            if label is None:
                if value == "---":
                    tk.Frame(table, height=1, bg="#d9dce1").grid(
                        row=r, column=0, columnspan=2, sticky="ew", pady=8)
                else:
                    tk.Label(table, text=value, font=("Helvetica", 11),
                             bg="#ffffff", fg="#444", anchor="w", justify="left",
                             wraplength=620).grid(row=r, column=0, columnspan=2, sticky="w", pady=2)
            else:
                tk.Label(table, text=label, font=("Helvetica", 11, "bold"),
                         bg="#ffffff", fg="#333", anchor="ne").grid(
                    row=r, column=0, sticky="ne", padx=(0, 14), pady=3)
                tk.Label(table, text=str(value), font=("Consolas", 11),
                         bg="#ffffff", fg="#111", anchor="w", justify="left",
                         wraplength=560).grid(row=r, column=1, sticky="w", pady=3)
            r += 1

        tk.Label(outer, text=question, font=("Helvetica", 12, "bold"),
                 bg="#ffffff", fg="#111", anchor="w").pack(fill=tk.X, pady=(16, 14))

        result = {"ok": False}
        btns = tk.Frame(outer, bg="#ffffff")
        btns.pack(fill=tk.X)

        def _yes():
            result["ok"] = True
            win.destroy()

        def _no():
            result["ok"] = False
            win.destroy()

        tk.Button(btns, text="Cancel", font=("Helvetica", 11, "bold"), width=12,
                  bg="#e9ecef", fg="#333", relief="flat", command=_no).pack(side=tk.RIGHT, padx=(8, 0))
        tk.Button(btns, text="✓ Start Scan", font=("Helvetica", 11, "bold"), width=14,
                  bg=accent, fg="white", relief="flat", command=_yes).pack(side=tk.RIGHT)

        win.protocol("WM_DELETE_WINDOW", _no)
        win.update_idletasks()
        # Center over the main window.
        try:
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            w, h = win.winfo_width(), win.winfo_height()
            win.geometry(f"+{px + (pw - w) // 2}+{py + (ph - h) // 3}")
        except Exception:
            pass
        win.grab_set()
        win.wait_window()
        return result["ok"]

    def _safe_sleep(self, seconds, bypass_check=False):
        """Sleeps but aborts immediately if Stop/Emergency is triggered."""
        start = time.time()
        while time.time() - start < seconds:
            if not self.is_running and not bypass_check: break
            if self._reset_cancel: break   # Reset dialog's Cancel, honored even with bypass_check=True
            self.pause_event.wait()
            if not self.is_running and not bypass_check: break
            time.sleep(0.5)

    DAQ_FLAG_DIR = "/tmp/daq_flags"

    def _daq_flag_active(self, since):
        """Is execute_DAQ_v2 for this point running? script_v7.sh touches daq_<run>.flag right before the DAQ command
        and removes it right after, so the flag brackets its lifetime exactly (pgrep misses start-up and exit).
        `since`: ignore a flag older than this point's launch (left over from a killed run)."""
        try:
            for name in os.listdir(self.DAQ_FLAG_DIR):
                if not (name.startswith("daq_") and name.endswith(".flag")):
                    continue
                path = os.path.join(self.DAQ_FLAG_DIR, name)
                try:
                    if os.path.getctime(path) >= since - 2:
                        return True
                except OSError:
                    continue
        except FileNotFoundError:
            # No flag dir yet (first run or an old script_v7.sh): fall back to the process-name check.
            check = subprocess.run(
                'pgrep -x execute_DAQ_v2 | xargs -r ps -o args= -p 2>/dev/null | grep -v -- "-j"',
                shell=True, capture_output=True, text=True, timeout=15)
            return bool(check.stdout.strip())
        return False

    DIGITIZER_DEAD = "digitizer not answering"   # core/routine.py stops the routine on this text

    def _digitizer_answers(self, cfg):
        """execute_DAQ_v2 -j after a failed point -> True / False, or None when the check could not run."""
        base = (cfg.get("DaqProgramPath") or cfg.get("BasePath") or "").rstrip("/")
        if not base:
            return None
        for _ in range(10):   # the killed run must release the board first
            if subprocess.run(["pgrep", "-x", "execute_DAQ_v2"], capture_output=True).returncode != 0:
                break
            time.sleep(1)
        env = os.environ.copy()
        extra = [d for d in (os.path.join(os.path.dirname(base), "lib"), "/opt/root/lib") if os.path.isdir(d)]
        env["LD_LIBRARY_PATH"] = ":".join(extra + ([env["LD_LIBRARY_PATH"]] if env.get("LD_LIBRARY_PATH") else []))
        try:
            r = subprocess.run([os.path.join(base, "execute_DAQ_v2"), "-j"], capture_output=True, text=True,
                               timeout=30, env=env, start_new_session=True)
        except Exception as e:
            self.controller._log(f"[WARNING] Digitizer check could not run: {type(e).__name__}.")
            return None
        if r.returncode == 127:   # binary or library missing: not a digitizer verdict
            return None
        return r.returncode == 0

    MOTOR_RATE_FLOOR_DEG_S = 0.5   # pessimistic until motor_rate_ema calibrates

    # Attempts per safe step (2 = one retry for a drive that answers but did not move).
    MOVE_ATTEMPTS = 2

    # Cap on how long a still-creeping stage may extend its wait (prevents an endless stall).
    MOVE_CREEP_GRACE_S = 120.0

    def _wait_for_motors(self, bypass_check=False, timeout=None, step_deg=None):
        """Poll rot_mgr.is_moving until both devices clear their lock; False after `timeout` s so the caller can
        skip the point. `timeout=None` sizes it from `step_deg` and the measured motor speed."""
        if timeout is None:
            deg = step_deg if step_deg else 45.0   # matches the old fixed case's rot_step
            rate = self.motor_rate_ema or self.MOTOR_RATE_FLOOR_DEG_S
            timeout = max(30.0, deg / rate * 2.5 + 10.0)
        rm = self.controller.rot_mgr
        t_start = time.time()
        deadline = t_start + timeout

        def should_continue():
            # Reset dialog's Cancel is honored even with bypass_check=True.
            if self._reset_cancel:
                return False
            return bool(self.is_running or bypass_check)

        # Wait/timeout policy lives in rot_mgr.wait_until_stopped; here only the step budget and 'skip point'.
        for dev in (2, 3):
            remaining = max(0.0, deadline - time.time())
            outcome = rm.wait_until_stopped(dev, remaining, should_continue=should_continue)
            if outcome == rm.WAIT_CANCELLED:
                self.controller._log(
                    f"[INFO] _wait_for_motors: wait on Device {dev} cancelled "
                    f"(stop/reset requested) -- not waiting out the full timeout.")
                return False
            if outcome == rm.WAIT_TIMEOUT:
                # wait_until_stopped logged why and released the lock, so the next point is re-commanded.
                return False

        if step_deg:
            elapsed = time.time() - t_start
            if elapsed >= 1.0:   # skip near-zero noops
                observed_rate = step_deg / elapsed
                self.motor_rate_ema = (observed_rate if self.motor_rate_ema is None
                                       else 0.7 * self.motor_rate_ema + 0.3 * observed_rate)
        return True

    def _move_safely_stepped(self, target_2, target_3, axis_type, bypass_check=False, step_override=None):
        """Returns True if the move completed, False if a motor lock never
        cleared (see _wait_for_motors) — callers that can safely skip a single
        scan point on this failure should check the return value."""
        step_size = step_override if step_override else (self.tilt_step if axis_type == "tilt" else self.rot_step)

        # 1. Read the current angles
        curr_t2, curr_r2 = self.controller.rot_mgr.read_angles(2)
        curr_t3, curr_r3 = self.controller.rot_mgr.read_angles(3)

        c2 = curr_t2 if axis_type == "tilt" else curr_r2
        c3 = curr_t3 if axis_type == "tilt" else curr_r3

        if c2 is None: c2 = target_2
        if c3 is None: c3 = target_3

        # A device with scanning disabled keeps its own position as target, so it never moves.
        if not self.rotate_dev2:
            target_2 = c2
        if not self.rotate_dev3:
            target_3 = c3

        while self.is_running or bypass_check:
            if self._reset_cancel:   # operator cancelled a Reset Angle mid-move
                return False
            # Pause holds at a step boundary (stage settled); re-check is_running after it so Stop never moves on.
            if not bypass_check:
                self.pause_event.wait()
                if not self.is_running:
                    break
            diff2 = target_2 - c2
            diff3 = target_3 - c3

            if abs(diff2) <= 0.5 and abs(diff3) <= 0.5:
                break

            move2 = min(abs(diff2), step_size) * (1 if diff2 > 0 else -1) if abs(diff2) > 0.5 else 0
            move3 = min(abs(diff3), step_size) * (1 if diff3 > 0 else -1) if abs(diff3) > 0.5 else 0

            next2 = c2 + move2
            next3 = c3 + move3

            self.controller._log(f"[INFO] Safe Step {axis_type.upper()}: Dev2 -> {next2:.1f}, Dev3 -> {next3:.1f}")

            # Re-issue the step if the stage does not confirm arrival (the drive may answer without moving).
            rm = self.controller.rot_mgr
            step_deg = max(abs(move2), abs(move3))
            attempt = 0
            issue_next = True
            # Absolute bound so "still creeping" can never wait forever.
            creep_deadline = time.time() + self.MOVE_CREEP_GRACE_S

            while True:
                if issue_next:
                    attempt += 1
                    if axis_type == "tilt":
                        if move2 != 0: rm.move_tilt_only(2, next2, skip_lock=bypass_check)
                        if move3 != 0: rm.move_tilt_only(3, next3, skip_lock=bypass_check)
                    else:
                        if move2 != 0: rm.move_rot_only(2, next2, skip_lock=bypass_check)
                        if move3 != 0: rm.move_rot_only(3, next3, skip_lock=bypass_check)

                if self._wait_for_motors(bypass_check, step_deg=step_deg):
                    break

                # Give up immediately on operator Stop / Reset-cancel: that is a
                # deliberate abort, not a hardware hiccup worth retrying.
                if self._reset_cancel or not (self.is_running or bypass_check):
                    return False

                # Re-command only a stage that stopped short; a slow but moving stage is just waited for.
                creeping = [d for d in (2, 3)
                            if rm.is_moving.get(d, False)
                            and rm.classify_motion(d) == rm.MOTION_CREEPING]
                if creeping and time.time() < creep_deadline:
                    self.controller._log(
                        f"[INFO] Device {', '.join(map(str, creeping))} still travelling "
                        f"(angle changing) -- extending the wait instead of re-commanding.")
                    issue_next = False
                    continue

                if attempt >= self.MOVE_ATTEMPTS:
                    self.controller._log(
                        f"[CRITICAL] Step {axis_type.upper()} still not confirmed after "
                        f"{attempt} attempt(s) -- giving up on this point "
                        f"(Dev2: {rm.classify_motion(2)}, Dev3: {rm.classify_motion(3)}).")
                    return False

                self.controller._log(
                    f"[WARNING] Step {axis_type.upper()} not confirmed "
                    f"(attempt {attempt}/{self.MOVE_ATTEMPTS}) -- re-issuing the move "
                    f"(Dev2: {rm.classify_motion(2)}, Dev3: {rm.classify_motion(3)}).")
                issue_next = True

            if abs(target_2 - next2) > 0.5 or abs(target_3 - next3) > 0.5:
                self.controller._log(f"[INFO] Step reached. Waiting {self.rest_time}s for hardware safety...")
                self._safe_sleep(self.rest_time, bypass_check)

            c2, c3 = next2, next3
        return True

    def _rot_with_offset(self, direction, offset):
        """Rotation target for one PMT's cable `direction` plus an axis `offset` (0 = X, 90 = Y), so both PMTs stay
        co-oriented. Targets outside rot_range are folded by 180 deg (same axis, mirrored cathode region), as
        angle_convert.h expects; the tilt command stays un-flipped."""
        # int: execute_DAQ_v2 declares --rot2/--rot3 as int.
        return angle_convert.rot_with_offset(direction, offset)

    def _get_rot_for_cable(self, axis, direction):
        return self._rot_with_offset(direction, 0 if axis == "X" else 90)

    def _wait_y_axis_start(self):
        """Hold at the Y-Axis start position until y_axis_start. -> "stopped" on abort, else None."""
        t = getattr(self, "y_axis_start", None)
        if not t or time.time() >= t:
            return None
        self.controller._log(f"[INFO] Y-Axis aligned; waiting until {datetime.fromtimestamp(t):%H:%M:%S} "
                             f"({(t - time.time()) / 60:.0f} min) before its data.")
        while self.is_running and time.time() < t and not self._reset_cancel:
            self._safe_sleep(min(5.0, t - time.time()))
        if not self.is_running:
            return "stopped"
        self.controller._log("[INFO] Y-Axis start time reached; taking Y-Axis data.")
        return None

    def _set_axis_rot_targets(self, axis, cfg):
        """Rotation targets for both devices on `axis`. Returns (r2, r3)."""
        off = 0 if axis == "X" else 90
        r2 = self._rot_with_offset(cfg.get("direction2", "B"), off)
        r3 = self._rot_with_offset(cfg.get("direction3", "B"), off)
        self.controller._log(
            f"[INFO] {axis}-axis rotation targets: Dev2 -> {r2:.1f} deg, Dev3 -> {r3:.1f} deg "
            f"(cable {cfg.get('direction2','B')}/{cfg.get('direction3','B')}).")
        return r2, r3

    def _force_single_sequence_config(self):
        """Force NumSequences=1 / IntervalTime=0 for a General Scan: script_v7.sh's repeat loop keeps its angle
        arguments, so later sequences would record stale angles and outlive the point."""
        path = self.controller.config_manager.filepath
        try:
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
            new_content = re.sub(r'const int NumSequences\s*=\s*\d+\s*;',
                                 'const int NumSequences = 1;', content)
            new_content = re.sub(r'const int IntervalTime\s*=\s*\d+\s*;',
                                 'const int IntervalTime = 0;', new_content)
            if new_content != content:
                self.controller._log(
                    "[WARNING] General Scan start: NumSequences/IntervalTime were left over "
                    "from a different run mode (e.g. Stability) -- forced back to 1/0 to "
                    "prevent stale-angle orphan acquisitions.")
                tmp = path + ".tmp"
                with open(tmp, 'w', encoding='utf-8') as f:
                    f.write(new_content); f.flush(); os.fsync(f.fileno())
                os.replace(tmp, path)
        except Exception as e:
            self.controller._log(f"[ERROR] Could not verify/force NumSequences=1 in config3.h: {e}")

    def start_general_scan(self, skip_validation=False):
        self.is_skipping_validation = skip_validation

        if not skip_validation:
            if not self.controller.access_mgr.unlocked:
                self._dlg().showwarning("Locked", "🔒 Please click 'Unlock Controls' first.")
                return

        if self.is_running: return

        self._force_single_sequence_config()

        # Show the Live Scan tab when a scan starts.
        try:
            if hasattr(self.controller, 'auto_ui') and hasattr(self.controller.auto_ui, 'matrix_tab'):
                self.controller.auto_ui.upper_notebook.select(self.controller.auto_ui.matrix_tab)
            if hasattr(self.controller.auto_ui, 'live_scan_view'):
                self.controller.auto_ui.live_scan_view.reset()
        except Exception:
            pass

        os.environ["SCAN_START_DATE"] = datetime.now().strftime("%Y%m%d")
        # Wall-clock scan start: Live Scan / Full Grid tell this scan's files apart by time, not run number.
        os.environ["SCAN_START_EPOCH"] = str(time.time())
        self._write_active_scan_marker(os.environ["SCAN_START_DATE"])
        # Highest run number of today before this scan, so the live view shows only this scan's points.
        try:
            date_tag = os.environ["SCAN_START_DATE"]
            result_dir = os.path.join(os.path.expanduser("~/ADC/ADC_test"), "Data", "FinalResult")
            existing = glob.glob(os.path.join(result_dir, f"precal_result_kor_run_{date_tag}_*.root"))
            max_run = -1
            for f in existing:
                m = re.search(r"_(\d+)\.root$", os.path.basename(f))
                if m:
                    max_run = max(max_run, int(m.group(1)))
            os.environ["SCAN_START_MIN_RUN"] = str(max_run)
        except Exception as e:
            os.environ["SCAN_START_MIN_RUN"] = "-1"
            self.controller._log(f"[WARNING] Could not determine SCAN_START_MIN_RUN: {e}")

        cfg = self.controller.config_manager.get_all_variables()
        is_dummy = self.controller.auto_ui.dummy_var.get()

        # Events this scan should record, checked against every RAW file by _verify_scan_files().
        try:
            self._scan_expected_events = self.scan_events(cfg)
        except Exception:
            self._scan_expected_events = 0

        # config3.h has no RunMode: take the Scan Mode radio of the General Scan tab.
        if hasattr(self.controller.auto_ui, 'scan_mode_var'):
            cfg["RunMode"] = "Dark" if self.controller.auto_ui.scan_mode_var.get() == "dark" else "Laser"

        # Laser Sequence: checked wavelengths run as sequential blocks (405, 375, 450, 473); dark/dummy ignore it.
        self.laser_sequence = []
        if not is_dummy and cfg.get("RunMode", "Laser").lower() != "dark" \
                and hasattr(self.controller.auto_ui, 'get_laser_sequence'):
            seq = self.controller.auto_ui.get_laser_sequence()
            if seq is None:
                self._dlg().showerror("Laser Sequence",
                                     "Invalid Bias/Pulse value in the Laser Sequence panel.")
                return
            if not seq:
                self._dlg().showwarning("Laser Sequence",
                                       "No wavelength is selected in the Laser Sequence panel.")
                return
            self.laser_sequence = seq
        n_wl = max(1, len(self.laser_sequence))

        points_per_axis = len(self.build_tilt_angles())
        steps_per_block = points_per_axis * 2
        total_steps = steps_per_block * n_wl
        backend = getattr(self, "daq_backend", "caen")
        # A scan with more points than one block takes consecutive blocks (70 points -> 100 run numbers).
        self.scan_block_runs = self.SCAN_BLOCK * max(1, -(-steps_per_block // self.SCAN_BLOCK))
        day_runs = self.MAX_SCAN_BLOCK + self.SCAN_BLOCK
        if backend != "hk" and not is_dummy and self.scan_block_runs > day_runs:
            self._clear_active_scan_marker()
            self.controller._log(f"[ERROR] General Scan not started: {steps_per_block} points per wavelength "
                                 f"exceed the {day_runs} General Scan run numbers of a day.")
            if not skip_validation:
                self._dlg().showerror("Too Many Points",
                                      f"{steps_per_block} points per wavelength exceed the {day_runs} General Scan run numbers of a day.\n"
                                      "Use fewer tilt angles, so the scan was not started.")
            return

        # In HK mode the CAEN 220s/step estimate is meaningless -- timing is
        # acq + move_delay + settle, driven open-loop by Master.
        if backend == "hk" and not is_dummy:
            hk = self.hk_config
            per_pt = hk["acq_time"] + hk["move_delay"] + self.daq_settle_time
            hk_total = total_steps * per_pt
            opt_disp = self.hk_option_args(hk).strip() or "(none)"
            self.controller._log(
                f"[INFO] HK Pre-flight: {total_steps} pts ({steps_per_block}×{n_wl} wl), "
                f"acq {hk['acq_time']}s + delay {hk['move_delay']}s + settle {self.daq_settle_time}s "
                f"= {per_pt:.0f}s/pt → est {hk_total/60:.1f} min. "
                f"run# start {hk['run_number']}, target {hk['ssh_target']}, "
                f"options {opt_disp}.")
            if not skip_validation:
                rows = [
                    ("Backend:", "HK Digitizer  (local config3.h NOT used)"),
                    ("SSH Target:", hk['ssh_target']),
                    ("ScanManager:", hk['scan_manager']),
                    (None, "---"),
                    ("Run # start:", hk['run_number']),
                    ("Options:", opt_disp),
                    ("Per point:", f"{hk['acq_time']}s acq  +  {hk['move_delay']}s move-delay  "
                                   f"+  {self.daq_settle_time}s settle   =  {per_pt:.0f}s"),
                    ("Points:", f"{total_steps}   ({steps_per_block} angles × {n_wl} wavelength block(s))"),
                    ("Est. total:", f"{hk_total/60:.1f} min   (~{hk_total/3600.0:.2f} h)"),
                ]
                if not self._wide_confirm(
                        "HK Digitizer Checklist", "🖧 HK Digitizer Pre-flight",
                        rows, "Start the HK-synced scan?", accent="#8e44ad"):
                    return
            total_seconds = hk_total
        else:
            total_seconds = total_steps * (self._estimate_nominal_per_point_seconds() if not is_dummy else 1)

        hv_rows, hv_warn = [], []
        if backend == "caen" and not is_dummy:
            from hv_preflight import check_hv
            hv_rows, hv_block, hv_warn = check_hv(cfg)
            if hv_block:
                msg = "\n".join(hv_block)
                self.controller._log(f"[CRITICAL] Scan not started -- {msg}")
                self._clear_active_scan_marker()
                if skip_validation:
                    self._scan_errors = [f"HV pre-flight: {b}" for b in hv_block]
                    self.controller.auto_ui.update_start_button(False)
                    notifier = getattr(self.controller, 'notifier', None)
                    if notifier and notifier.enabled:
                        notifier.send("Scheduled scan NOT started: HV no current", msg,
                                      level="warning", dedupe_key=None, blocking=False,
                                      key="scan_not_started")
                else:
                    self._dlg().showerror("HV: No Current", msg + "\n\nThe scan was not started.")
                return
            for w in hv_warn:
                self.controller._log(f"[WARNING] HV pre-flight: {w}")

        if not is_dummy:
            from bfield_preflight import coils_off_since
            off_since = coils_off_since()
            if off_since:
                self.controller._log(f"[WARNING] B-field coils are OFF (I1-I4 = 0 A since {off_since}).")
                if not skip_validation and not self._dlg().askyesno(
                        "B-field Coils OFF", f"All B-field coil currents (I1-I4) are 0 A since {off_since}.\n\n"
                                             "Start the scan anyway?"):
                    self._clear_active_scan_marker()
                    return

        if backend == "caen" and not is_dummy and not skip_validation:
            raw_path = cfg.get("RawDataPath", "")
            if not os.path.exists(raw_path):
                self._dlg().showerror("Error", f"Save path not found:\n{raw_path}")
                return
            
            usage = shutil.disk_usage(raw_path)
            free_gb = usage.free / (1024**3)
            
            estimated_required_gb = total_steps * 0.8
            
            if free_gb < estimated_required_gb:
                warning_msg = (
                    f"⚠️ SEVERE STORAGE WARNING!\n\n"
                    f"1 Full Scan (X, Y axis) requires {total_steps} DAQ executions.\n"
                    f"Estimated required space is about {estimated_required_gb:.1f} GB.\n\n"
                    f"Current available space: {free_gb:.1f} GB\n\n"
                    f"There is a high risk of a system crash due to a full disk during the scan.\n"
                    f"Are you sure you want to force the scan?"
                )
                if not self._dlg().askyesno("Storage Warning", warning_msg):
                    return

            sn2, dir2 = cfg.get("SN2", "N/A"), cfg.get("direction2", "N/A")
            sn3, dir3 = cfg.get("SN3", "N/A"), cfg.get("direction3", "N/A")

            rows = [
                ("Backend:", "CAEN DAQ  —  local config3.h"),
                ("Est. space:", f"~{estimated_required_gb:.1f} GB required"),
                ("Free space:", f"{free_gb:.1f} GB available  (OK)"),
            ]
            if self.laser_sequence:
                lm = getattr(self.controller, 'laser_mgr', None)
                marks = []
                for wl, _b, _p in self.laser_sequence:
                    inst = lm.laser_instances.get(wl) if lm else None
                    ok = bool(inst and inst.is_connected())
                    marks.append(f"{wl} ({'connected' if ok else 'DISCONNECTED -- will skip'})")
                rows.append(("Wavelength(s):", ", ".join(marks)))
            rows.append(("Rotate:", self.controller.auto_ui._format_rotate_devices(self)))
            rows += [
                (None, "---"),
                ("Target SN2:", f"{sn2}   (Cable Dir: {dir2})"),
                ("Target SN3:", f"{sn3}   (Cable Dir: {dir3})"),
                ("Points:", f"{total_steps}   ({steps_per_block} angles x {n_wl} wavelength block(s))"),
            ]

            # config3.h is stamped into every RunInfo and cannot be fixed later, so show it before the scan.
            cfg_rows, cfg_warn = self.controller.config_manager.preflight_rows()
            rows.append((None, "---"))
            rows.append(("Configuration:", "config3.h  (stamped into every run)"))
            rows += cfg_rows
            for w in cfg_warn:
                rows.append(("CHECK:", w))
            rows.append((None, "---"))
            rows += hv_rows
            for w in hv_warn:
                rows.append(("CHECK:", w))
            if not self._wide_confirm(
                    "Pre-flight Checklist", "🚀 CAEN DAQ Pre-flight",
                    rows, "Is the hardware setup correct? Start the scan?", accent="#0a84ff"):
                return

        self.resume_data = None
        if os.path.exists(self.state_file):
            if skip_validation:
                self.controller._log("[INFO] ⏰ Scheduled scan: Clearing old recovery data for a clean start.")
                os.remove(self.state_file) 
                ans = False 
            else:
                ans = self._dlg().askyesno(
                    "Recovery Found", 
                    "🚨 A record of an abnormally terminated scan was found.\n\n"
                    "Would you like to resume from the last angle?\n"
                    "(Click 'No' to delete the record and start over)"
                )
            
            if ans:
                try:
                    with open(self.state_file, 'r') as f:
                        self.resume_data = json.load(f)
                except Exception as e:
                    self.controller._log(f"Recovery load failed: {e}")
            elif os.path.exists(self.state_file):
                os.remove(self.state_file)

        t2, r2 = self.controller.rot_mgr.read_angles(2)
        t3, r3 = self.controller.rot_mgr.read_angles(3)
        self.initial_angles = {
            2: {"tilt": t2 if t2 is not None else 0.0, "rot": r2 if r2 is not None else 0.0},
            3: {"tilt": t3 if t3 is not None else 0.0, "rot": r3 if r3 is not None else 0.0}
        }
        self.controller._log(f"Saved initial angles for Reset: Dev2({t2}, {r2}), Dev3({t3}, {r3})")

        status_msg = "SYSTEM STATUS: SCHEDULED RUN IN PROGRESS..." if skip_validation else "SYSTEM STATUS: SCANNING..."
        
        self.controller.auto_ui.update_start_button(True, status_text=status_msg)
        #self.controller.auto_ui.update_start_button(True)

        if not self._assign_run_block(cfg):
            self._clear_active_scan_marker()
            self.controller.auto_ui.update_start_button(False)
            if not skip_validation:
                self._dlg().showerror("No Run Block Left",
                                      f"All General Scan run blocks (000-{self.MAX_SCAN_BLOCK}) are already used today.\n"
                                      "Runs 500+ are reserved for HV scan, dark and stability runs, so the scan was not started.")
            return
        # =========================================================================
        self.is_running = True

        if hasattr(self.controller.auto_ui, 'start_eta_countdown'):
            self.controller.auto_ui.start_eta_countdown(total_seconds, total_steps)

        threading.Thread(target=self._scan_sequence, daemon=True).start()

    MAX_SCAN_BLOCK = 450
    SCAN_BLOCK = 50   # runs per General Scan block (v2 core/run_info.py SCAN_BLOCK)

    def _assign_run_block(self, cfg):
        """Pick the next free 50-run block (000/050/100...) for the scan date.
        Called once at scan start and again for every wavelength block, so each
        wavelength's runs live in their own block — same numbering a separate
        scan session would have received.

        CAEN-only: this numbers .root files under config3.h's RawDataPath,
        which HK Digitizer mode doesn't write to or read (it has its own
        run_number counter in hk_config). No-op there so the log doesn't show
        a meaningless "New Scan Block Assigned" during an HK run."""
        if getattr(self, "daq_backend", "caen") == "hk":
            return True
        import re
        raw_path = cfg.get("RawDataPath", "./Data/RAW/")
        date_tag = os.environ.get("SCAN_START_DATE") or datetime.now().strftime("%Y%m%d")
        mode = cfg.get("RunMode", "Laser")
        mode_dir = "Dark" if mode.lower() == "dark" else "Laser"
        search_path = os.path.join(raw_path, mode_dir, f"*_{date_tag}_*.root")

        # Also check FinalResult: the backup reclaims local RAW, so RAW alone forgets blocks used earlier today.
        final_search = os.path.join(os.path.expanduser("~/ADC/ADC_test/Data/FinalResult"),
                                    f"precal_result_kor_run_{date_tag}_*.root")

        ext_search = os.path.join("/home/precalkor/external_HDD_1_4T/Data_Backup/RAW", mode_dir,
                                  f"*_{date_tag}_*.root")

        max_block = -self.SCAN_BLOCK
        for pattern in (search_path, final_search, ext_search):
            for f in glob.glob(pattern):
                match = re.search(r'_([0-9]{3})\.root', f)
                if match:
                    num = int(match.group(1))
                    if num < 500:
                        max_block = max(max_block, num // self.SCAN_BLOCK * self.SCAN_BLOCK)

        registry = os.path.expanduser("~/ADC/ADC_test/Data/run_registry.csv")
        try:
            with open(registry, encoding="utf-8") as f:
                for line in f.readlines()[1:]:
                    cols = line.split(",")
                    if len(cols) > 2 and cols[1] == date_tag and cols[2].strip().isdigit():
                        num = int(cols[2])
                        if num < 500:
                            max_block = max(max_block, num // self.SCAN_BLOCK * self.SCAN_BLOCK)
        except FileNotFoundError:
            pass
        except Exception as e:
            self.controller._log(f"[WARNING] Could not read run registry: {e}")

        nxt = max_block + self.SCAN_BLOCK if max_block >= 0 else 0
        runs = int(getattr(self, "scan_block_runs", self.SCAN_BLOCK) or self.SCAN_BLOCK)
        if nxt + runs - self.SCAN_BLOCK > self.MAX_SCAN_BLOCK:   # 500 HV scan, 600 dark, 700 manual, 800+ stability
            self.controller._log(f"[ERROR] No free General Scan run block left for {date_tag}: this scan needs runs "
                                 f"{nxt:03d}-{nxt + runs - 1:03d}, blocks end at {self.MAX_SCAN_BLOCK + self.SCAN_BLOCK - 1} "
                                 f"(500+ is reserved for HV scan/dark/stability).")
            return False
        self.current_scan_block = nxt
        self.controller._log(f"[INFO] New Scan Block Assigned: {self.current_scan_block:03d}")
        return True

    def schedule_general_scan(self, time_str):
        if not self.controller.access_mgr.unlocked:
            self._dlg().showwarning("Locked", "🔒 Please click 'Unlock Controls' first.")
            return
        if self.is_running: return

        try:
            target_time = datetime.strptime(time_str.strip(), "%H:%M").time()
        except ValueError:
            self._dlg().showerror("Invalid Time", "Please use HH:MM format (e.g., 14:30).")
            return

        cfg = self.controller.config_manager.get_all_variables()
        is_dummy = self.controller.auto_ui.dummy_var.get()
        points_per_axis = len(self.build_tilt_angles())
        total_steps = points_per_axis * 2

        if not is_dummy:
            raw_path = cfg.get("RawDataPath", "")
            if not os.path.exists(raw_path):
                self._dlg().showerror("Error", f"Save path not found:\n{raw_path}")
                return
            
            usage = shutil.disk_usage(raw_path)
            free_gb = usage.free / (1024**3)
            estimated_required_gb = total_steps * 0.8
            
            if free_gb < estimated_required_gb:
                if not self._dlg().askyesno("Storage Warning", f"⚠️ Low Storage: {free_gb:.1f} GB left. Schedule anyway?"): 
                    return

            sn2, dir2 = cfg.get("SN2", "N/A"), cfg.get("direction2", "N/A")
            sn3, dir3 = cfg.get("SN3", "N/A"), cfg.get("direction3", "N/A")
            
            if sn2 == "N/A" or sn3 == "N/A":
                if not self._dlg().askyesno("Missing Info", "SN2 or SN3 is missing! Schedule anyway?"): return

            checklist_msg = (
                f"⏰ Schedule Pre-flight Checklist (JST)\n\n"
                f"• Target Time: {time_str} (JST)\n"
                f"• Free Space: {free_gb:.1f} GB (OK)\n"
                f"• Target SN2: {sn2} (Dir: {dir2})\n"
                f"• Target SN3: {sn3} (Dir: {dir3})\n\n"
                f"Are these parameters correct? The scan will start automatically at {time_str} JST."
            )
            if not self._dlg().askyesno("Schedule Checklist", checklist_msg): return

        self.is_scheduled = True
        
        self.controller.auto_ui.add_auto_log(f"⏰ Scan scheduled successfully for {time_str} (JST).")
        self._update_scan_status_label(f"SCHEDULED: {time_str} (JST)", "#007ACC")
        self.controller.auto_ui.btn_start.config(state="disabled")
        
        threading.Thread(target=self._wait_for_schedule, args=(target_time,), daemon=True).start()

    def _wait_for_schedule(self, target_time):
        JST = timezone(timedelta(hours=9))  
        
        while self.is_scheduled:
            now_jst = datetime.now(JST)
            
            if now_jst.hour == target_time.hour and now_jst.minute == target_time.minute:
                self.controller.auto_ui.add_auto_log(f"▶ Scheduled time ({target_time.strftime('%H:%M')} JST) reached. Starting auto-scan...")
                self.is_scheduled = False
                
                self.controller.master.after(0, lambda: self.start_general_scan(skip_validation=True))
                break
            time.sleep(10)


    def cancel_schedule(self):
        self.is_scheduled = False
        self.controller._log("[INFO] ⏰ Scheduled scan cancelled by user.")
        self._update_scan_status_label("SYSTEM STATUS: IDLE", "gray")
        self.controller.auto_ui.btn_start.config(state="normal")

    def _save_state(self, axis, tilt, step):
        state = {"axis": axis, "tilt": tilt, "step": step,
                 "wl_idx": getattr(self, '_current_wl_idx', 0)}
        try:
            with open(self.state_file, 'w') as f:
                json.dump(state, f)
        except: pass

    def _scan_sequence(self):
        """Scan orchestrator. Runs one full X/Y tilt scan per wavelength block
        from the Laser Sequence panel (or a single legacy block when the panel
        is empty / dark mode / dummy)."""
        start_time = datetime.now()
        # Per-scan error tally read by _show_scan_summary(), so 'GOOD RUN' means no point failed.
        self._scan_errors, self._scan_warnings = [], []
        self._consecutive_errors = 0
        is_dummy = self.controller.auto_ui.dummy_var.get()
        cfg = self.controller.config_manager.get_all_variables()
        # Same RunMode injection as start_general_scan().
        if hasattr(self.controller.auto_ui, 'scan_mode_var'):
            cfg["RunMode"] = "Dark" if self.controller.auto_ui.scan_mode_var.get() == "dark" else "Laser"

        shifter = cfg.get("Shift_worker", "").strip()
        expert = cfg.get("Expert", "N/A").strip()
        self.controller.auto_ui.add_auto_log(f"Scan Started (Shifter: {shifter} / Expert: {expert})")

        sn2_name = cfg.get("SN2", "SN2")
        sn3_name = cfg.get("SN3", "SN3")

        self._current_expert = expert   # read back by _notify_scan_finished; _show_scan_summary only gets shifter
        self._notify_scan_started(cfg, sn2_name, sn3_name, shifter, expert)

        points_per_axis = len(self.build_tilt_angles())
        steps_per_block = points_per_axis * 2

        # ── Wavelength blocks: [(wl, bias, pulse), ...] or [None] = legacy
        #    single-config scan with no laser control.
        seq = list(getattr(self, 'laser_sequence', []) or [])
        if is_dummy or cfg.get("RunMode", "Laser").lower() == "dark":
            seq = []
        blocks = seq if seq else [None]
        n_wl = len(blocks)

        start_axis = "X"
        start_tilt = self.scan_range["start"]
        skip_until_match = False
        start_wl_idx = 0
        resume_step = 0

        if self.resume_data:
            start_axis = self.resume_data.get("axis", "X")
            start_tilt = self.resume_data.get("tilt", self.scan_range["start"])
            resume_step = self.resume_data.get("step", 0)
            start_wl_idx = min(self.resume_data.get("wl_idx", 0), n_wl - 1)
            skip_until_match = True
            self.controller._log(
                f"🔄 Recovery Mode: Target position -> {start_axis}-Axis, {start_tilt}°"
                + (f" (block {start_wl_idx + 1}/{n_wl})" if n_wl > 1 else ""))

        # Remove a stray 'GeneralScan' tmux session left by an old version.
        if not is_dummy:
            subprocess.run(['tmux', 'kill-session', '-t', 'GeneralScan'], capture_output=True)

        completed_blocks, skipped_blocks = [], []
        self._scan_skipped, self._scan_audited = skipped_blocks, False   # skipped: read by the v2 routine and Slack
        try:
            for wl_idx, block in enumerate(blocks):
                if wl_idx < start_wl_idx:
                    continue
                if not self.is_running:
                    return
                self._current_wl_idx = wl_idx
                self._current_block_wl = block[0] if block else None
                first_block = (wl_idx == start_wl_idx)

                if block:
                    wl, bias, pulse = block
                    self.controller.auto_ui.add_auto_log(f"🔦 Laser block {wl_idx + 1}/{n_wl}: {wl}")
                    if not self._prepare_laser_block(block):
                        skipped_blocks.append(wl)
                        self.controller.auto_ui.add_auto_log(f"⚠ {wl} block skipped (laser not ready).")
                        continue
                    if not self.is_running:
                        return
                    # Each wavelength gets its own 50-run block — the same
                    # numbering a separate scan session would have received.
                    if not is_dummy and not first_block and not self._assign_run_block(cfg):
                        skipped_blocks.extend(b[0] for b in blocks[wl_idx:] if b)
                        self.controller.auto_ui.add_auto_log(
                            f"[ERROR] {wl} and later blocks skipped: no free run block below 500 today.")
                        break
                    self._apply_laser_config(wl, pulse, bias)
                    self.controller.auto_ui.update_start_button(
                        True, status_text=f"SYSTEM STATUS: SCANNING [{wl}] ({wl_idx + 1}/{n_wl})")
                    self.controller.master.after(
                        0, lambda w=wl, i=wl_idx:
                        self.controller.auto_ui.set_matrix_wavelength(f"{w} ({i + 1}/{n_wl})"))
                    if not first_block:
                        self.controller.master.after(0, self.controller.auto_ui.reset_matrix_cells)

                # [ETA] per-block timing baseline — each wavelength re-measures
                # its own step pace, so the ETA always refers to the current block.
                self.scan_t0 = time.time()
                self.scan_total_steps = steps_per_block
                self.scan_current_step = 0   # not the last scan's count
                self.scan_done_steps = 0
                self.scan_last_done_t = self.scan_t0

                res = self._scan_axes_block(
                    cfg, is_dummy, sn2_name, sn3_name, points_per_axis, steps_per_block,
                    start_axis if (skip_until_match and first_block) else "X",
                    start_tilt if (skip_until_match and first_block) else self.scan_range["start"],
                    skip_until_match and first_block,
                    resume_step if (skip_until_match and first_block) else 0)

                if block:
                    self._finish_laser_block(block[0])
                if res == "stopped":
                    return
                if res == "usb_lost":
                    skipped_blocks.append(self._current_block_wl or "?")
                    self.controller.auto_ui.add_auto_log(
                        f"⚠ {self._current_block_wl} block aborted (laser USB lost) — moving to next wavelength.")
                    continue
                if block:
                    completed_blocks.append(block[0])

            # Scan finished
            if os.path.exists(self.state_file):
                os.remove(self.state_file)

            end_time = datetime.now()
            # Audit before is_running drops: the verdict is complete then, and the GUI thread never reads RAW files.
            try:
                self._verify_scan_files(start_time)
            except Exception as e:
                self._scan_warnings.append(f"file audit failed ({e})")
            self._scan_audited = True
            self.is_running = False
            if seq:
                summary = f"Completed: {', '.join(completed_blocks) or '-'}"
                if skipped_blocks:
                    summary += f" | Skipped: {', '.join(skipped_blocks)}"
                self.controller._log(f"[INFO] Laser sequence finished. {summary}")
                self.controller.auto_ui.add_auto_log(f"🔦 Laser sequence done. {summary}")
            self.controller._log("Automation sequence completed successfully.")

            # [FIXED] Redirect the UI dialog box creation to the main GUI thread to prevent unexpected Tcl/Tk thread crash
            self.controller.master.after(0, lambda: self._show_scan_summary(start_time, end_time, shifter))

        except Exception as e:
            self.controller._log(f"[ERROR] Auto Run Thread Error: {e}")

        finally:
            self._current_block_wl = None
            self.controller.auto_ui.update_start_button(False)
            self.is_running = False

    def _scan_axes_block(self, cfg, is_dummy, sn2_name, sn3_name, points_per_axis,
                         total_steps, start_axis, start_tilt, skip_until_match,
                         current_step):
        """One full X/Y tilt scan (the pre-multi-wavelength scan body).
        Returns "done", "stopped" (operator abort) or "usb_lost" (active
        block's laser disappeared -> caller skips to the next wavelength)."""
        try:
            for axis in ["X", "Y"]:
                if skip_until_match and axis != start_axis:
                    current_step += points_per_axis
                    continue
                
                r2, r3 = self._set_axis_rot_targets(axis, cfg)

                if not is_dummy:
                    self.controller._log(f"[INFO] --- Checking {axis}-Axis Rotation ---")
                    _, curr_r2 = self.controller.rot_mgr.read_angles(2)
                    _, curr_r3 = self.controller.rot_mgr.read_angles(3)
                    
                    already_at_rot = (curr_r2 is not None and abs(curr_r2 - r2) < 0.5) and \
                                     (curr_r3 is not None and abs(curr_r3 - r3) < 0.5)

                    axis_ok = True
                    if not already_at_rot:
                        self.controller._log(f"[INFO] Rotation mismatch. Moving TILT to 0.0 first.")
                        axis_ok = self._move_safely_stepped(0.0, 0.0, "tilt", bypass_check=self.is_skipping_validation, step_override=self.safe_move_step)
                        self._wait_for_physical_angle(2, target_tilt=0.0, bypass_check=self.is_skipping_validation)
                        self._wait_for_physical_angle(3, target_tilt=0.0, bypass_check=self.is_skipping_validation)

                        if axis_ok:
                            axis_ok = self._move_safely_stepped(r2, r3, "rot", bypass_check=self.is_skipping_validation)
                        self._wait_for_physical_angle(2, target_rot=r2, bypass_check=self.is_skipping_validation)
                        self._wait_for_physical_angle(3, target_rot=r3, bypass_check=self.is_skipping_validation)
                        self._safe_sleep(2.0, bypass_check=self.is_skipping_validation)

                    # Move to this axis' start point right after it is aligned
                    target_init_tilt = start_tilt if skip_until_match else self.scan_range["start"]
                    if axis_ok:
                        self.controller._log(f"[INFO] Axis Aligned. Moving to start tilt: {target_init_tilt}°")
                        axis_ok = self._move_safely_stepped(target_init_tilt, target_init_tilt, "tilt",
                                                 bypass_check=self.is_skipping_validation,
                                                 step_override=self.safe_move_step)
                        self._wait_for_physical_angle(2, target_tilt=target_init_tilt, bypass_check=self.is_skipping_validation)
                        self._wait_for_physical_angle(3, target_tilt=target_init_tilt, bypass_check=self.is_skipping_validation)

                    if not axis_ok and not self.is_running:
                        return "stopped"   # a stop or abort during the alignment is not a motor fault

                    if not axis_ok:
                        # Motor timeout while aligning this axis: mark its points ERROR RUN, notify once, go to the next axis.
                        n_pts = len(self.build_tilt_angles())
                        reason = "axis alignment failed (motor comm timeout)"
                        self.controller._log(
                            f"🚨 [ERROR RUN] {axis}-Axis alignment failed (motor comm timeout). "
                            f"Skipping all {n_pts} {axis}-Axis points this block.")
                        for tilt in self.build_tilt_angles():
                            self._mark_point_error(sn2_name, sn3_name, axis, tilt, reason, notify=False)
                            current_step += 1
                        notifier = getattr(self.controller, 'notifier', None)
                        if notifier and notifier.enabled:
                            try:
                                notifier.send(
                                    f"General Scan: {axis}-Axis skipped ({n_pts} points)",
                                    f"Reason: {reason}\n"
                                    f"Log: [ERROR RUN] {axis}-Axis alignment failed (motor comm timeout). "
                                    f"Skipping all {n_pts} {axis}-Axis points this block.",
                                    level="warning",
                                    dedupe_key=f"axis_align_failed_{axis}_{reason}",
                                    blocking=False, key="scan_point_skipped")
                            except Exception as e:
                                self.controller._log(f"[WARNING] Axis-error notification failed: {type(e).__name__}.")
                        self._update_progress_ui(current_step, total_steps)
                        continue

                    if axis == "Y" and self._wait_y_axis_start() == "stopped":
                        return "stopped"

                for tilt in self.build_tilt_angles():

                    if not self.is_running: return "stopped"

                    if skip_until_match:
                        if axis == start_axis and tilt == start_tilt:
                            skip_until_match = False 
                        else:
                            self.controller.auto_ui.update_cell(sn2_name, tilt, axis, "done")
                            self.controller.auto_ui.update_cell(sn3_name, tilt, axis, "done")
                            continue

                    if hasattr(self.controller, 'ups_mgr') and self.controller.ups_mgr.ups_serial:
                        ups_msg = self.controller.ui.ups_vars["status_msg"].get()
                        ups_fault = next((label for key, label in (("POWER FAILURE", "Battery Mode"),
                                                                   ("OVERLOAD", "Overload"))
                                          if key in ups_msg.upper()), None)
                        if ups_fault:
                            self.controller._log(f"🚨 [INTERLOCK] UPS {ups_fault}! Automation paused.")
                            self.pause_event.clear()
                            self.controller.auto_ui.update_stop_button(False)

                    self.pause_event.wait() # wait while paused
                    if not self.is_running: return "stopped"

                    # Active-wavelength laser check: interlock pauses the scan, USB loss skips to the next wavelength.
                    if getattr(self, '_current_block_wl', None) and not is_dummy:
                        lstate = self._check_block_laser()
                        if lstate == "interlock":
                            self.controller._log(
                                f"🚨 [INTERLOCK] Laser {self._current_block_wl} interlock tripped! "
                                "Scan paused — resolve the interlock, then press Continue.")
                            self.pause_event.clear()
                            self.controller.auto_ui.update_stop_button(False)
                            self.pause_event.wait()
                            if not self.is_running: return "stopped"
                        elif lstate == "usb":
                            return "usb_lost"
                        # No point is taken unless TEC and LD read ON.
                        while True:
                            why = self._verify_block_laser_on()
                            if not why:
                                break
                            self.controller._log(f"🚨 Laser {self._current_block_wl}: {why}. "
                                                 "Scan paused -- fix the laser, then press Continue.")
                            notifier = getattr(self.controller, 'notifier', None)
                            if notifier:
                                notifier.send(f"Scan paused: laser {self._current_block_wl} not ON",
                                              f"{why}.\nNo DAQ is taken until it is fixed and Continue is pressed.",
                                              level="critical", dedupe_key=f"laser-off-{self._current_block_wl}",
                                              key="scan_paused_laser")
                            self.pause_event.clear()
                            self.controller.auto_ui.update_stop_button(False)
                            self.pause_event.wait()
                            if not self.is_running: return "stopped"

                    self._save_state(axis, tilt, current_step)
                    # Point being taken now, for the Scan Matrix card.
                    self.current_axis, self.current_tilt = axis, tilt
                    
                    self.controller.auto_ui.update_cell(sn2_name, tilt, axis, "move")
                    self.controller.auto_ui.update_cell(sn3_name, tilt, axis, "move")
                    # Show moving vs acquiring on the Run Control status line.
                    self.controller.auto_ui.update_start_button(
                        True, status_text=f"SYSTEM STATUS: MOVING to {tilt}° ({axis}-Axis)")
                    self._current_axis, self._current_tilt = axis, tilt
                    if hasattr(self.controller, 'ui'):
                        self.controller.ui.console_set_status(
                            f"▶ Running: General Scan DAQ  ·  Point {current_step + 1}/{total_steps}"
                            f"  ·  Tilt {tilt}° ({axis}-Axis)  ·  moving",
                            slot="general_scan", state="running")
                        self.controller.ui.console_begin_point(
                            "general_scan",
                            f"Point {current_step + 1}/{total_steps} · Tilt {tilt}° ({axis}-Axis)")
                        eta = self.get_eta_seconds()
                        self.controller.ui.console_set_scan_progress(
                            current_step + 1, total_steps, axis, tilt,
                            eta_seconds=(eta[0] if eta else None), slot="general_scan")

                    # 2. Step move and physical check
                    if not is_dummy:
                        self.controller._log(f"[INFO] Scanning: Moving TILT to {tilt} deg...")
                        point_ok = self._move_safely_stepped(tilt, tilt, "tilt", bypass_check=self.is_skipping_validation)
                        if not point_ok:
                            # Motor never confirmed arrival: skip this point (ERROR RUN) and keep scanning.
                            self._mark_point_error(sn2_name, sn3_name, axis, tilt,
                                                    "motor comm timeout (did not confirm arrival)")
                            current_step += 1
                            self._update_progress_ui(current_step, total_steps)
                            continue
                        self._wait_for_physical_angle(2, target_tilt=tilt)
                        self._wait_for_physical_angle(3, target_tilt=tilt)

                        self.controller._log(f"[INFO] Motor arrived. Waiting {self.daq_settle_time}s for stabilization...")
                        self._safe_sleep(self.daq_settle_time)
                        #self.controller._log(f"[INFO] Syncing current angles (Tilt: {tilt}°) to config before DAQ...")
                        self.controller.auto_ui.update_config_angles(sn2_name, tilt, r2)
                        self.controller.auto_ui.update_config_angles(sn3_name, tilt, r3)

                        if hasattr(self.controller, 'auto_ui'):
                            self.controller.auto_ui.notebook.after(100, self.controller.refresh_all_data)
                    else:
                        time.sleep(0.5)

                    self.controller.auto_ui.update_cell(sn2_name, tilt, axis, "daq")
                    self.controller.auto_ui.update_cell(sn3_name, tilt, axis, "daq")
                    self.controller.auto_ui.update_start_button(
                        True, status_text=f"SYSTEM STATUS: ACQUIRING at {tilt}° ({axis}-Axis)")
                    if hasattr(self.controller, 'ui'):
                        self.controller.ui.console_set_status(
                            f"▶ Running: General Scan DAQ  ·  Point {current_step + 1}/{total_steps}"
                            f"  ·  Tilt {tilt}° ({axis}-Axis)  ·  acquiring",
                            slot="general_scan", state="running")

                    current_step += 1
                    self._update_progress_ui(current_step, total_steps)

                    # 3. Run the DAQ
                    res = self._execute_daq_point(cfg, is_dummy, sn2_name, sn3_name, axis, tilt, r2, r3)
                    if res == "stopped":
                        return "stopped"

                    # Mark the step done in the UI
                    self.controller.auto_ui.update_cell(sn2_name, tilt, axis, "done")
                    self.controller.auto_ui.update_cell(sn3_name, tilt, axis, "done")

            # Reproducibility recheck: revisit configured angles after the block, tagged 'repeat' (not overwriting).
            if not is_dummy and self.repeat_angles:
                self.controller._log(
                    "[INFO] Repeatability recheck: revisiting "
                    f"{self._format_repeat_points(self.repeat_angles)} ...")

                dir2 = cfg.get("direction2", "B")
                dir3 = cfg.get("direction3", "B")

                # rot is an axis offset on each PMT's cable base (0 = X, 90 = Y); other offsets get an 'R<rot>' key.
                points = []
                for (tilt, rot) in self.repeat_angles:
                    rmod = rot % 360
                    axis_lbl = "X" if rmod == 0 else ("Y" if rmod == 90 else f"R{rot:g}")
                    points.append({
                        "tilt": float(tilt), "rot": float(rot),
                        "r2": self._rot_with_offset(dir2, rot),
                        "r3": self._rot_with_offset(dir3, rot),
                        "axis": axis_lbl,
                    })

                # Visit rotation groups and tilts nearest-first (a rotation change forces tilt 0 first).
                groups = {}
                for p in points:
                    groups.setdefault(p["rot"], []).append(p)

                _ang = lambda dv: 180 - abs(abs(dv) % 360 - 180)   # 0..180 shortest angular gap
                cur_tilt, cur_r2 = self.controller.rot_mgr.read_angles(2)
                cur_r2 = cur_r2 if cur_r2 is not None else 0.0
                cur_tilt = cur_tilt if cur_tilt is not None else 0.0
                ordered_rots = sorted(groups, key=lambda rv: _ang(self._rot_with_offset(dir2, rv) - cur_r2))

                running_tilt = cur_tilt
                for rv in ordered_rots:
                    grp = groups[rv]
                    r2, r3 = grp[0]["r2"], grp[0]["r3"]

                    self.controller._log(f"[INFO] --- Repeat check: aligning rotation offset {rv:g}° (r2={r2:g}, r3={r3:g}) ---")
                    _, curr_r2 = self.controller.rot_mgr.read_angles(2)
                    _, curr_r3 = self.controller.rot_mgr.read_angles(3)
                    already_at_rot = (curr_r2 is not None and abs(curr_r2 - r2) < 0.5) and \
                                     (curr_r3 is not None and abs(curr_r3 - r3) < 0.5)
                    if not already_at_rot:
                        # tilt must be 0 before rotating (hardware interlock).
                        self._move_safely_stepped(0.0, 0.0, "tilt", bypass_check=self.is_skipping_validation, step_override=self.safe_move_step)
                        self._wait_for_physical_angle(2, target_tilt=0.0, bypass_check=self.is_skipping_validation)
                        self._wait_for_physical_angle(3, target_tilt=0.0, bypass_check=self.is_skipping_validation)
                        self._move_safely_stepped(r2, r3, "rot", bypass_check=self.is_skipping_validation)
                        self._wait_for_physical_angle(2, target_rot=r2, bypass_check=self.is_skipping_validation)
                        self._wait_for_physical_angle(3, target_rot=r3, bypass_check=self.is_skipping_validation)
                        self._safe_sleep(2.0, bypass_check=self.is_skipping_validation)
                        running_tilt = 0.0

                    for p in sorted(grp, key=lambda q: abs(q["tilt"] - running_tilt)):
                        if not self.is_running: return "stopped"
                        self.pause_event.wait()
                        tilt = p["tilt"]

                        self.controller._log(f"[INFO] Repeat check: moving tilt to {tilt:g}° (rot offset {rv:g}°)...")
                        point_ok = self._move_safely_stepped(tilt, tilt, "tilt", bypass_check=self.is_skipping_validation, step_override=self.safe_move_step)
                        if not point_ok:
                            self.controller._log(f"[WARNING] Repeat check move failed for tilt {tilt:g}° rot {rv:g}° -- skipping this recheck point.")
                            continue
                        self._wait_for_physical_angle(2, target_tilt=tilt, bypass_check=self.is_skipping_validation)
                        self._wait_for_physical_angle(3, target_tilt=tilt, bypass_check=self.is_skipping_validation)
                        running_tilt = tilt

                        self.controller._log(f"[INFO] Motor arrived. Waiting {self.daq_settle_time}s for stabilization...")
                        self._safe_sleep(self.daq_settle_time)

                        res = self._execute_daq_point(cfg, is_dummy, sn2_name, sn3_name, p["axis"], tilt, p["r2"], p["r3"], tag="repeat")
                        if res == "stopped":
                            return "stopped"

            return "done"

        except Exception as e:
            self.controller._log(f"[ERROR] Scan block error: {e}")
            return "stopped"

    @staticmethod
    def hk_legacy_options(hk):
        """Convert a pre-list hk_config (threshold_preset, trg_channel, ...,
        auto_hittree/auto_eventtree) to scan_options/post_steps, keeping the
        exact flag order the old formatter emitted."""
        d = dict(hk)
        if "scan_options" not in d:
            thr = str(d.get("threshold_preset") or "").strip()
            d["scan_options"] = [
                {"label": "Threshold preset", "flag": "--threpreset" if thr in ("", "1") else f"--threpreset{thr}",
                 "value": "", "on": True, "bare": True},
                {"label": "Gate", "flag": "--gatelist", "value": str(d.get("gatelist", 30)), "on": True, "bare": False},
                {"label": "Pedestal run", "flag": "--pedestal", "value": "", "on": bool(d.get("pedestal_mode")), "bare": True},
                {"label": "Trigger ch", "flag": "--trgch", "value": str(d.get("trg_channel") or ""), "on": True, "bare": False},
                {"label": "Trigger Vth", "flag": "--trgch-Vth", "value": str(d.get("trg_vth") or ""), "on": True, "bare": False},
                {"label": "Channels", "flag": "--chanlist", "value": str(d.get("chanlist") or ""), "on": True, "bare": False},
            ]
        if "post_steps" not in d:
            ev = bool(d.get("auto_eventtree"))
            d["post_steps"] = [
                {"name": "Hit-tree", "on": bool(d.get("auto_hittree")) or ev,
                 "cmd": 'RunAnalysisChain.py --inputBinFile "{bin}" -t "{base}_hittree.root"'},
                {"name": "Event-tree", "on": ev,
                 "cmd": 'RunAnalysisChain.py --inputHitTreeFile "{base}_hittree.root" -T "{base}_eventtree.root"'},
            ]
        for k in ("gatelist", "chanlist", "threshold_preset", "trg_channel", "trg_vth",
                  "pedestal_mode", "auto_hittree", "auto_eventtree"):
            d.pop(k, None)
        return d

    @staticmethod
    def hk_option_args(hk):
        """ScanManager option string (leading space per option) from scan_options."""
        out = ""
        for o in hk.get("scan_options", []):
            flag = str(o.get("flag") or "").strip()
            if not flag or not o.get("on"):
                continue
            if o.get("bare"):
                out += f" {flag}"
            else:
                val = str(o.get("value") or "").strip()
                if val:
                    out += f" {flag} {val}"
        return out

    @staticmethod
    def hk_option_on(hk, flag):
        return any(str(o.get("flag")).strip() == flag and o.get("on") for o in hk.get("scan_options", []))

    @staticmethod
    def hk_option_value(hk, flag):
        for o in hk.get("scan_options", []):
            if str(o.get("flag")).strip() == flag and o.get("on"):
                return str(o.get("value") or "").strip()
        return ""

    @staticmethod
    def hk_post_cmd(hk, run_id):
        """Loop over the .bin files this acquisition just wrote and chain the
        enabled post_steps on each. ScanManager appends its own suffix to -i
        (e.g. "<run_id>-001-1mV-Gate30.bin"), so the file is globbed, and
        -mmin -1 skips older files with the same prefix. Empty if no step is on."""
        steps = [str(p.get("cmd") or "").strip() for p in hk.get("post_steps", []) if p.get("on")]
        steps = [c.replace("{bin}", "$f").replace("{base}", "${f%.bin}") for c in steps if c]
        if not steps:
            return ""
        return ('for f in ' + run_id + '*.bin; do '
                '[ -f "$f" ] || continue; '
                '[ "$(find "$f" -mmin -1)" ] || continue; '
                + " && ".join(steps) + '; done')

    @staticmethod
    def hk_format_remote(hk, run_id, acq_time):
        """Pure formatter: `<setup_cmd> && cd <work_dir> && <scan_manager> -i
        <run_id> --l <acq> <scan_options> [&& <post_steps loop>]`. Static so
        the HK Config dialog can preview UNSAVED values with the same code
        the real trigger uses."""
        work_dir = (hk.get("work_dir") or "").strip()
        if "{date}" in work_dir:
            work_dir = work_dir.replace("{date}", datetime.now().strftime("%Y%m%d"))
        cd_part = f"cd {work_dir} && " if work_dir else ""
        scan_cmd = (f"{hk['scan_manager']} -i {run_id} --l {int(round(acq_time))}"
                    f"{AutomationManager.hk_option_args(hk)}")
        post = AutomationManager.hk_post_cmd(hk, run_id)
        return f"{hk['setup_cmd']} && {cd_part}{scan_cmd}" + (f" && {post}" if post else "")

    def hk_build_remote(self, run_id, acq_time):
        return self.hk_format_remote(self.hk_config, run_id, acq_time)

    @staticmethod
    def hk_format_vmodem(hk):
        """Pure formatter for the vmodem (stage 3) command. minicom is
        interactive, so instead of requiring the operator to hand-create a
        runscript file on hkpd (easy to forget / go stale), we WRITE the
        runscript fresh every run from `vmodem_keys` (e.g. "m,O") via a
        heredoc, then invoke minicom -S against it. This guarantees the keys
        configured here are always the ones actually sent."""
        device = (hk.get("vmodem_device") or "").strip()
        keys = [k.strip() for k in (hk.get("vmodem_keys") or "").split(",") if k.strip()]
        sends = "\n".join(f'send "{k}"' for k in keys) if keys else 'send ""'
        return (f"cat > ~/vmodem.runscript <<'EOF'\n{sends}\n"
                f"! killall -q minicom || true\nEOF\n"
                f"minicom -S ~/vmodem.runscript -D {device}")

    def hk_build_vmodem_remote(self):
        return self.hk_format_vmodem(self.hk_config)

    def hk_dpb_alive_count(self):
        """Read-only: number of `socat` processes alive on the DPB board (nested SSH via the HK PC), so the
        DPB Setup guard survives a master restart. Returns -1 when the check itself failed (unknown, not zero)."""
        dpb_host = (self.hk_config.get("dpb_host") or "").strip()
        if not dpb_host:
            return -1
        rc, out = self._hk_ssh(
            f"ssh -o BatchMode=yes -o ConnectTimeout=8 root@{dpb_host} "
            "'pgrep -c -f socat' 2>/dev/null",
            wait=True, timeout=15)
        try:
            return int(out.strip().splitlines()[-1])
        except Exception:
            return -1

    def _hk_ssh(self, remote, wait=False, timeout=None):
        """SSH the given remote command to hk_config['ssh_target']. Wraps it in
        a login bash so ScanManager's PATH/env from setup_hkelec.sh is present.
        wait=False fires and returns (open-loop scan); wait=True blocks and
        returns (rc, output) for the Test button."""
        import shlex
        hk = self.hk_config
        inner = f"bash -lc {shlex.quote(remote)}"
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
               hk["ssh_target"], inner]
        if wait:
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
                return p.returncode, (p.stdout + p.stderr)
            except Exception as e:
                return -1, str(e)
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except Exception as e:
            self.controller._log(f"[ERROR] HK trigger failed ({e}); continuing open-loop.")

    def hk_run_in_console(self, remote, job_name="HK", on_complete=None):
        """Run a remote HK command and STREAM its stdout/stderr live into the
        'HK Digitizer' console sub-tab (Output tab), same as the CAEN DAQ
        stream. Non-blocking: the console's own reader thread crawls the
        terminal output line by line. If given, on_complete(exit_code) fires
        on the main thread once the SSH session (and therefore the remote
        ScanManager invocation) actually exits -- used by the scan loop to
        wait for real completion instead of a blind acq_time sleep."""
        import shlex
        hk = self.hk_config
        inner = f"bash -lc {shlex.quote(remote)}"
        cmd_str = ("ssh -tt -o BatchMode=yes -o ConnectTimeout=8 "
                   f"{hk['ssh_target']} {shlex.quote(inner)}")
        try:
            self.controller.ui.ensure_console_pane("hk")
        except Exception:
            pass
        self.controller._run_job_in_console([cmd_str], job_name=job_name, slot="hk",
                                            on_complete=on_complete)

    def hk_format_run_id(self, run_no, tilt, rot, rot3=None):
        """Substitute {run}/{tilt}/{rot2}/{rot3} (plus the older {rot} alias
        for {rot2}) in the run_id template. A BARE {run} is treated as
        3-digit zero-padded (001, 002, ...) so filenames sort cleanly; an
        explicit format spec like {run:04d} is honoured as-is.

        `tilt` is the single shared scan-axis angle (same for SN2 and SN3);
        `rot`/`rot2` is SN2's rotation offset, `rot3` is SN3's -- two
        separate, device-INDEXED tokens (2 <-> SN2, 3 <-> SN3) so a template
        stays correct if a second device is added later. There is no
        {tilt1}/{tilt2}: both devices move to the same tilt on the same
        scan axis, only their rotation differs, so a per-device tilt token
        doesn't exist -- using an unrecognized token like {tilt1} would raise
        KeyError, silently fall through to the `except` below, and return the
        UNFORMATTED template (still literally containing "{run}") for every
        point, i.e. every acquisition would collide on the exact same
        filename with no error shown.

        When rot3 isn't available (e.g. manual acquire, which only reads one
        device's live angle), it falls back to `rot` so a template using
        {rot3} still resolves instead of silently returning `base`.

        {trgch}/{trgvth} are the values of the --trgch / --trgch-Vth options
        (empty when the option is off or missing).

        {mode} follows the --pedestal option -> "ped" / "normal", matching
        the HK side's own convention. It is a macro rather than typed text
        because the two have to agree: a literal "normal" in the template
        stayed put when the checkbox was ticked, so a --pedestal run wrote a
        file whose own name claimed it was a normal run."""
        base = self.hk_config.get("run_id", "Run%03d" % run_no)
        tmpl = base.replace("{run}", "{run:03d}")   # bare {run} → 001; {run:0Nd} untouched
        rot3_val = rot3 if rot3 is not None else rot
        trg_ch = self.hk_option_value(self.hk_config, "--trgch")
        trg_vth = self.hk_option_value(self.hk_config, "--trgch-Vth")
        mode = "ped" if self.hk_option_on(self.hk_config, "--pedestal") else "normal"
        try:
            return tmpl.format(run=int(run_no), tilt=tilt, rot=rot, rot2=rot, rot3=rot3_val,
                               trgch=trg_ch, trgvth=trg_vth, mode=mode)
        except Exception:
            return base

    def hk_manual_acquire(self):
        """One HK acquisition triggered MANUALLY (not from the scan loop), at
        the stage's current angle. Streams to the HK console and advances the
        shared run number -- same counter General Scan uses, so manual and
        auto runs never collide."""
        hk = self.hk_config
        run_no = hk.get("run_number", 0)
        acq = float(hk.get("acq_time", 10.0))
        t2, r2 = self.controller.rot_mgr.read_angles(2)
        tilt = t2 if t2 is not None else 0.0
        rot = r2 if r2 is not None else 0.0
        # 'MAN_' marks manual runs (real data, advances the counter); 'TEST_' runs are throwaway.
        run_id = "MAN_" + self.hk_format_run_id(run_no, tilt, rot)
        self.controller._log(
            f"[INFO] HK MANUAL acquire run#{run_no} @ tilt {tilt}°, rot {rot}°, acq {acq}s.")
        self.hk_run_in_console(self.hk_build_remote(run_id, acq),
                               job_name=f"HK manual run#{run_no}")
        hk["run_number"] = run_no + 1
        self.save_hk_config()

    def hk_resolved_work_dir(self):
        """work_dir with {date} expanded -- the directory acquisitions land in
        TODAY. Shared by the command formatter and the remote run-number probe
        so they can never disagree about which folder is in play."""
        wd = (self.hk_config.get("work_dir") or "").strip()
        return wd.replace("{date}", datetime.now().strftime("%Y%m%d")) if "{date}" in wd else wd

    def hk_probe_remote_runs(self):
        """Read the HK PC's CURRENT data directory and report the highest run
        number actually on disk there.

        run_number is otherwise a purely local counter with no link to reality:
        it does not follow the {date} folder rollover, and it knows nothing
        about runs someone took by hand on the HK PC. Either way the counter
        can point at a number that already exists, and the next acquisition
        silently overwrites that file. This is the read-only reconciliation --
        `ls` only, nothing is written or installed on the HK PC.

        Returns a dict: {ok, dir, n_files, runs, next, error}. `next` is the
        lowest free number (max found + 1), or 0 for an empty/new folder.
        """
        import re
        wd = self.hk_resolved_work_dir()
        if not wd:
            return {"ok": False, "error": "work_dir is empty", "dir": "", "n_files": 0,
                    "runs": [], "next": None}
        rc, out = self._hk_ssh(f"ls -1 {wd} 2>/dev/null", wait=True, timeout=20)
        if rc != 0:
            return {"ok": False, "error": (out or "").strip()[:300] or f"ssh exit {rc}",
                    "dir": wd, "n_files": 0, "runs": [], "next": None}
        names = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
        # Run<n> and MAN_Run<n> occupy run numbers; TEST_Run<n> never advances the counter.
        runs = sorted({int(m.group(1)) for ln in names
                       for m in [re.match(r"^(?:MAN_)?Run0*(\d+)", ln)] if m})
        return {"ok": True, "error": "", "dir": wd, "n_files": len(names),
                "runs": runs, "next": (max(runs) + 1) if runs else 0}

    def hk_build_dpb_setup(self):
        """DPB bring-up command with {dpb_host} substituted. Kept as a macro so
        the board's address is configured in exactly one field."""
        hk = self.hk_config
        cmd = hk.get("dpb_setup_cmd", "") or ""
        try:
            return cmd.format(dpb_host=(hk.get("dpb_host") or "").strip())
        except (KeyError, IndexError):
            return cmd   # unknown token -> send as typed rather than silently blanking it

    def hk_build_remote_current(self, prefix="", suffix=""):
        """ScanManager command for a one-off run at the current stage angle, run_id macros substituted. The run
        counter is not advanced; `prefix` (e.g. 'TEST_') marks throwaway check runs."""
        hk = self.hk_config
        try:
            t2, r2 = self.controller.rot_mgr.read_angles(2)
        except Exception:
            t2, r2 = None, None
        run_id = prefix + self.hk_format_run_id(
            hk.get("run_number", 0),
            t2 if t2 is not None else 0.0,
            r2 if r2 is not None else 0.0) + suffix
        return self.hk_build_remote(run_id, float(hk.get("acq_time", 10.0)))

    def hk_test_trigger(self):
        """Run the current (field-built) command on the HK PC and return
        (rc, output). Used by the HK Config dialog's 'Test Trigger (Dummy)'
        button to sanity-check the SSH link + ScanManager before a real scan."""
        hk = self.hk_config
        remote = self.hk_build_remote_current(prefix="TEST_")
        self.controller._log(f"[INFO] HK TEST trigger → {hk['ssh_target']}: {remote}")
        rc, out = self._hk_ssh(remote, wait=True, timeout=120)
        self.controller._log(f"[INFO] HK TEST result rc={rc}: {out.strip()[:500]}")
        return rc, out

    def _execute_hk_point(self, axis, tilt, r2, r3, tag=None):
        """One HK acquisition point: trigger, wait for the remote SSH session to exit (ScanManager finished), wait
        move_delay, record and advance the run number. Bounded by acq*3 + 60 s; all waits honour pause/abort."""
        hk = self.hk_config
        run_no = hk.get("run_number", 0)
        acq = float(hk.get("acq_time", 10.0))
        delay = float(hk.get("move_delay", 20.0))

        # Encode run number and live angles into the run id ({run} is zero-padded to 3 digits).
        run_id = self.hk_format_run_id(run_no, tilt, r2, rot3=r3)

        self.controller._log(
            f"[INFO] HK point run#{run_no} @ (tilt {tilt}°, r2 {r2}°, r3 {r3}°): "
            f"acq {acq}s + delay {delay}s (waiting for real completion).")

        done_event = threading.Event()
        result = {"code": None}

        def _on_done(code):
            result["code"] = code
            done_event.set()

        self.hk_run_in_console(self.hk_build_remote(run_id, acq),
                               job_name=f"HK run#{run_no}", on_complete=_on_done)

        timeout = acq * 3 + 60.0
        start = time.time()
        while not done_event.is_set() and (time.time() - start) < timeout:
            if not self.is_running: return "stopped"
            self.pause_event.wait()
            if not self.is_running: return "stopped"
            time.sleep(0.5)

        if not done_event.is_set():
            self.controller._log(
                f"[WARNING] HK run#{run_no}: no completion signal after "
                f"{timeout:.0f}s (SSH may be hung) -- proceeding anyway.")
        elif result["code"] != 0:
            self.controller._log(
                f"[WARNING] HK run#{run_no} exited with code {result['code']} "
                f"(ScanManager may have failed) -- point recorded, verify manually.")

        if not self.is_running: return "stopped"
        self._safe_sleep(delay)                  # buffer before rotation
        if not self.is_running: return "stopped"

        filepath = f"HK:{hk.get('ssh_target', '')}:{run_id}"
        self._record_scan_point(axis, tilt, r2, r3, filepath, tag=tag)
        hk["run_number"] = run_no + 1
        self.save_hk_config()
        return None

    def _execute_daq_point(self, cfg, is_dummy, sn2_name, sn3_name, axis, tilt, r2, r3, tag=None):
        """Trigger one DAQ run at the stage's CURRENT position and wait for
        it to finish (watchdog-protected), then record the resulting file.

        Shared by the main per-point scan loop and the post-block
        reproducibility recheck (repeat_angles) -- both need the exact same
        trigger/watchdog/verify/record sequence, just at a different point
        in the overall scan. `tag` (e.g. "repeat") is forwarded to
        _record_scan_point so a recheck measurement is stored as an
        ADDITIONAL scanmap entry instead of overwriting the original point.

        Returns "stopped" if the operator aborted mid-wait, else None.
        """
        if is_dummy:
            time.sleep(0.5)
            return None

        # HK Digitizer: trigger, wait for the remote session to exit (bounded), then move_delay.
        if getattr(self, "daq_backend", "caen") == "hk":
            return self._execute_hk_point(axis, tilt, r2, r3, tag=tag)

        # Only files created after this launch belong to this point.
        daq_launch_time = time.time()
        self.controller.run_daq(tilt=tilt, r2=r2, r3=r3)

        # Wait for execute_DAQ_v2 to appear; a timeout is a failed launch (not 'done'), so clear the backlog.
        # 90 s floor until a start-up is measured, then the slowest seen plus margin.
        DAQ_STARTUP_FLOOR_S = 90
        grace = max(DAQ_STARTUP_FLOOR_S, int(self.daq_startup_max * 1.5) + 30)
        daq_started = False
        startup_wait = 0
        t_launch = time.time()
        while startup_wait < grace:
            if not self.is_running: return "stopped"
            if self._daq_flag_active(daq_launch_time):
                daq_started = True
                observed = time.time() - t_launch
                if observed > self.daq_startup_max:
                    self.daq_startup_max = observed
                    self.controller._log(
                        f"[INFO] DAQ start-up took {observed:.0f}s (new session maximum); "
                        f"launch grace is now {max(DAQ_STARTUP_FLOOR_S, int(observed * 1.5) + 30)}s.")
                break
            time.sleep(1); startup_wait += 1

        if not daq_started:
            # Kill the launcher too, so a pending script cannot start acquiring after the stage moved.
            self.controller._log(
                f"[CRITICAL] execute_DAQ_v2 never started within {grace}s "
                f"({axis}-Axis {tilt}°). Clearing any queued launch and skipping this point.")
            self._signal_own('script_v7.sh', signal.SIGTERM)
            self._graceful_kill_daq()
            self._mark_point_error(sn2_name, sn3_name, axis, tilt,
                                   "DAQ never started (launch timed out)")
            return None

        # Dynamically switch watchdog directory based on the active configuration mode (Dark vs Laser)
        run_mode_str = cfg.get("RunMode", "Laser")
        mode_dir = "Dark" if run_mode_str.lower() == "dark" else "Laser"

        last_size = 0
        stagnant_count = 0
        raw_path = cfg.get("RawDataPath", "./Data/RAW/")
        search_path = os.path.join(raw_path, mode_dir, "*.root")

        # Seconds of constant file size before a run counts as hung (longer for self-trigger).
        stagnation_limit = 60 if mode_dir == "Dark" else 30

        # Hung-run timeout from the measured throughput; DAQ_RATE_FLOOR_EVT_S only until the first point.
        DAQ_RATE_FLOOR_EVT_S = 400
        try:
            n_events = self.scan_events(cfg) or 50000
        except (TypeError, ValueError):
            n_events = 50000
        rate = self.daq_rate_ema or DAQ_RATE_FLOOR_EVT_S
        max_wait_time = max(600, int(n_events / rate * 1.5) + 120)
        elapsed = 0
        point_file = None     # this point's own RAW file, once it appears
        daq_hung = False

        def _fresh_files():
            # Only files created after this launch (not the previous point's file).
            out = []
            for f in glob.glob(search_path):
                try:
                    if os.path.getctime(f) >= daq_launch_time - 2:
                        out.append(f)
                except OSError:
                    pass
            return out

        while elapsed < max_wait_time:
            if not self.is_running: return "stopped"
            self.pause_event.wait()

            fresh = _fresh_files()
            if fresh:
                try:
                    point_file = max(fresh, key=os.path.getctime)
                    current_size = os.path.getsize(point_file)

                    if current_size == last_size and current_size > 0:
                        stagnant_count += 1
                    else:
                        stagnant_count = 0

                    if stagnant_count > stagnation_limit:
                        self.controller._log(f"[CRITICAL] Watchdog: DAQ hung at {point_file} "
                                             f"(size {current_size} constant for {stagnant_count}s). Killing process.")
                        self._graceful_kill_daq()
                        daq_hung = True
                        break
                    last_size = current_size
                except (FileNotFoundError, PermissionError) as ex:
                    self.controller._log(f"[WARNING] Watchdog filesystem race caught: {ex}")
            else:
                # This point's file hasn't appeared yet (DAQ still arming).
                # Don't accumulate stagnation on stale files.
                stagnant_count = 0

            if not self._daq_flag_active(daq_launch_time):
                # The flag file brackets execute_DAQ_v2's lifetime, so one negative read is enough.
                self.controller._log(f"[INFO] DAQ finished in {elapsed}s.")
                # Calibrate the rate estimate (EMA) from a real finished point; elapsed >= 5 s only.
                if elapsed >= 5:
                    observed_rate = n_events / elapsed
                    self.daq_rate_ema = (observed_rate if self.daq_rate_ema is None
                                         else 0.7 * self.daq_rate_ema + 0.3 * observed_rate)
                break
            time.sleep(1); elapsed += 1
        else:
            # Loop exhausted max_wait_time without the DAQ process ever
            # exiting or tripping the stagnation watchdog.
            self.controller._log(f"[CRITICAL] Watchdog: DAQ exceeded {max_wait_time}s wait. Killing process.")
            self._graceful_kill_daq()
            daq_hung = True

        # Verify integrity of THIS point's file (not just the newest in the
        # dir, which could be a later point if timing raced).
        if not daq_hung and point_file is None:
            point_file = max(_fresh_files(), key=os.path.getctime, default=None)
        if daq_hung or point_file is None:
            reason = ("DAQ process hung (watchdog killed execute_DAQ_v2)" if daq_hung
                      else "DAQ exited without a RAW file")
            if self.is_running and self._digitizer_answers(cfg) is False:
                reason += f"; {self.DIGITIZER_DEAD} (open failed)"
                self.controller._log(f"[CRITICAL] Digitizer does not answer ({axis}-Axis {tilt}°); "
                                     "aborting the scan. Power-cycle the digitizer and replug its USB cable.")
                # A dead board fails every point: abort on this one instead of after CONSECUTIVE_ERROR_LIMIT.
                self._consecutive_errors = self.CONSECUTIVE_ERROR_LIMIT - 1
            self._mark_point_error(sn2_name, sn3_name, axis, tilt, reason)
        if not daq_hung and point_file:
            self._verify_file_integrity(point_file)
            # Record the point only if the file's RunInfo angles match the commanded ones.
            if self._verify_recorded_angles(point_file, tilt, r2, r3):
                # Persist (axis, tilt) -> RAW file so the Scan Matrix point card
                # can open this point's data later.
                self._record_scan_point(axis, tilt, r2, r3, point_file, tag=tag)
            else:
                # Specific reason from _verify_recorded_angles, generic text otherwise.
                detail = getattr(self, '_last_angle_check_detail', None)
                self._mark_point_error(sn2_name, sn3_name, axis, tilt,
                                       detail or "recorded angles do not match the commanded point")

        if self.is_running:
            self.controller._log("[INFO] DAQ Done. Waiting 5s for safety...")
            self._safe_sleep(5.0)
        return None

    # ── Multi-wavelength laser block helpers ────────────────────────────────
    def _auto_connect_laser(self, wl):
        """Attempt a synchronous connect for a laser the scan needs but that
        was never connected this session -- laser_manager's own auto-reconnect
        loop (update_laser_status_loop) only revives lasers already in
        expected_connections, so a laser nobody ever clicked "Connect" on is
        otherwise skipped for the whole scan even if the hardware is fine.

        Safe to call from the scan's background thread: only touches the
        instrument driver (inst.connect), never a Tk widget directly. Any UI
        refresh is marshalled onto the main thread via .after(0, ...), same
        pattern as laser_manager's own reconnect_task."""
        lm = getattr(self.controller, 'laser_mgr', None)
        if not lm:
            return False
        inst = lm.laser_instances.get(wl)
        if not inst:
            return False
        target_path = lm.laser_port_mapping.get(wl)
        self.controller._log(f"[INFO] {wl} not connected — attempting auto-connect for the scan...")
        try:
            success, msg = inst.connect(dev_path=target_path)
        except Exception as e:
            success, msg = False, str(e)

        if not success:
            self.controller._log(f"[WARNING] {wl} auto-connect failed: {msg}")
            return False

        lm.expected_connections.add(wl)   # hand off to the normal auto-reconnect loop from here on
        lm.comm_error_flags[wl] = False
        self.controller._log(f"[INFO] {wl} auto-connected successfully for the scan.")

        def _refresh_ui(w=wl, i=inst):
            vd = lm.app.ui.laser_tabs_data.get(w) if hasattr(lm.app, 'ui') else None
            if vd:
                vd["conn_status_txt"].set("Connected")
                vd["conn_label_obj"].config(foreground="#28a745")
                if i.update_status():
                    vd["ld_status"].set("ON" if i.status.get('ld_on', False) else "OFF")
        if hasattr(self.controller, 'master') and self.controller.master.winfo_exists():
            self.controller.master.after(0, _refresh_ui)
        return True

    # Laser readiness: TEC and LD must be confirmed ON by the board's own status
    # (update_status() reads them back) before and during every DAQ point.
    LASER_SET_TRIES = 3
    LD_TOTAL_CURRENT_LIMIT_MA = 200.0   # bias + pulse, same as core/laser.py
    LASER_CONFIRM_S = 6.0      # the status read-back lags the command by a few seconds

    def _laser_status(self, inst):
        """Fresh status read from the board; None if the read failed."""
        try:
            return dict(inst.status) if inst.update_status() else None
        except Exception:
            return None

    def _laser_set_verified(self, inst, wl, what, state):
        """Switch TEC or LD and confirm it from the status read-back, retrying."""
        setter = inst.set_tec_on if what == "tec" else inst.set_ld_on
        word = f"{what.upper()} {'ON' if state else 'OFF'}"
        for k in range(1, self.LASER_SET_TRIES + 1):
            if setter(state):
                deadline = time.time() + self.LASER_CONFIRM_S
                while time.time() < deadline:
                    time.sleep(0.5)
                    st = self._laser_status(inst)
                    if st is not None and bool(st.get(f"{what}_on")) == state:
                        return True
            else:
                time.sleep(1.0)
            self.controller._log(f"[WARNING] Laser {wl} {word} not confirmed (try {k}/{self.LASER_SET_TRIES}).")
        return False

    def _laser_not_ready(self, wl, reason):
        """Log + Slack, and make sure the LD is not left on half-prepared."""
        self.controller._log(f"[ERROR] Laser {wl} not ready: {reason} -- no DAQ for this block.")
        notifier = getattr(self.controller, 'notifier', None)
        if notifier:
            notifier.send(f"Laser {wl} not ready", f"{reason}\nThis wavelength block was skipped (no DAQ taken).",
                          level="warning", dedupe_key=f"laser-not-ready-{wl}", key="laser_not_ready")
        try:
            inst = self.controller.laser_mgr.laser_instances.get(wl)
            if inst and inst.is_connected():
                inst.set_ld_on(False)
        except Exception:
            pass
        return False

    def _prepare_laser_block(self, block, sleep=None):
        """Blocking laser preparation, called from the scan thread.
        Exclusive LD (all others OFF first) -> TEC ON (confirmed) -> Bias/Pulse ->
        LD ON (confirmed) -> settle -> both still ON. Returns True when the block
        may start, False to skip it."""
        wl, bias, pulse = block
        if float(bias) + float(pulse) > self.LD_TOTAL_CURRENT_LIMIT_MA:   # the driver clamps each one silently
            return self._laser_not_ready(wl, f"Bias {float(bias):.1f} + Pulse {float(pulse):.1f} mA exceeds the "
                                             f"{self.LD_TOTAL_CURRENT_LIMIT_MA:.0f} mA combined LD limit")
        lm = getattr(self.controller, 'laser_mgr', None)
        inst = lm.laser_instances.get(wl) if lm else None
        if not inst:
            return self._laser_not_ready(wl, "no driver instance")
        if not inst.is_connected() and not self._auto_connect_laser(wl):
            return self._laser_not_ready(wl, "not connected (auto-connect failed)")
        try:
            for owl, oinst in lm.laser_instances.items():
                if owl != wl and oinst.is_connected():
                    try:
                        oinst.set_ld_on(False)
                    except Exception:
                        pass
            if not self._laser_set_verified(inst, wl, "tec", True):
                return self._laser_not_ready(wl, "TEC did not switch ON")
            self.controller._log(f"[INFO] Laser {wl} TEC ON (confirmed).")
            inst.set_bias_current(float(bias))
            inst.set_pulse_current(float(pulse))
            time.sleep(0.5)
            if not self._laser_set_verified(inst, wl, "ld", True):
                return self._laser_not_ready(wl, "LD did not switch ON")
            self.controller._log(f"[INFO] Laser {wl} ready: Bias={bias} mA, Pulse={pulse} mA, TEC ON, LD ON.")
            # Short settle so the first DAQ point is taken with stable output.
            (sleep or self._safe_sleep)(10.0)
            st = self._laser_status(inst)
            if st is None or not (st.get("tec_on") and st.get("ld_on")):
                return self._laser_not_ready(wl, "TEC/LD not ON after the settle time")
            return True
        except Exception as e:
            return self._laser_not_ready(wl, f"preparation error: {e}")

    def _verify_block_laser_on(self):
        """Before each DAQ point: TEC and LD of the active block must read ON.
        One recovery attempt (TEC, then LD); returns a reason if still off."""
        wl = getattr(self, '_current_block_wl', None)
        lm = getattr(self.controller, 'laser_mgr', None)
        inst = lm.laser_instances.get(wl) if lm and wl else None
        if not inst:
            return ""
        st = self._laser_status(inst)
        if st is None:
            st = self._laser_status(inst)       # one retry; a lost link is _check_block_laser's job
            if st is None:
                return ""
        if st.get("tec_on") and st.get("ld_on"):
            return ""
        off = " and ".join(n for n, k in (("TEC", "tec_on"), ("LD", "ld_on")) if not st.get(k))
        self.controller._log(f"[WARNING] Laser {wl} {off} read OFF before a DAQ point -- trying to restore.")
        if not st.get("tec_on"):
            if not self._laser_set_verified(inst, wl, "tec", True):
                return f"{off} OFF and TEC could not be restored"
        if not self._laser_set_verified(inst, wl, "ld", True):
            return f"{off} OFF and LD could not be restored"
        self._safe_sleep(10.0)
        self.controller._log(f"[INFO] Laser {wl} restored: TEC ON, LD ON.")
        return ""

    def _finish_laser_block(self, wl):
        """LD OFF at the end of a wavelength block (safety between blocks).
        TEC is left ON so a later block with the same laser restarts warm."""
        try:
            inst = self.controller.laser_mgr.laser_instances.get(wl)
            if inst and inst.is_connected():
                if self._laser_set_verified(inst, wl, "ld", False):
                    self.controller._log(f"[INFO] Laser {wl} LD OFF (block finished).")
                else:
                    self.controller._log(f"[WARNING] Laser {wl} LD OFF not confirmed after the block.")
        except Exception as e:
            self.controller._log(f"[WARNING] Failed to switch {wl} LD off: {e}")

    def _check_block_laser(self):
        """Health of the active block's laser: "" ok, "interlock", or "usb"."""
        lm = getattr(self.controller, 'laser_mgr', None)
        wl = getattr(self, '_current_block_wl', None)
        if not lm or not wl:
            return ""
        inst = lm.laser_instances.get(wl)
        lost = lm.comm_error_flags.get(wl, False) or not inst or not inst.is_connected()
        if not lost:
            return ""
        return "interlock" if lm._disc_reason.get(wl) == "INTERLOCK" else "usb"

    def _own_pids(self, name):
        """PIDs of `name` inside this app's own DAQ console jobs (each runs in
        its own process group). Falls back to every matching process except
        the 2 s `execute_DAQ_v2 -j` link probe when no job is tracked, so a
        run started by hand elsewhere is only hit when nothing else is known."""
        match = ["-f", name] if name.endswith(".sh") else ["-x", name]
        pids = set()
        for slot in ("general_scan", "daq"):
            proc = getattr(self.controller, "_console_procs", {}).get(slot)
            if proc is not None and proc.poll() is None:
                r = subprocess.run(["pgrep", "-g", str(proc.pid)] + match, capture_output=True, text=True)
                pids.update(int(x) for x in r.stdout.split())
        if not pids:
            r = subprocess.run(["pgrep"] + match, capture_output=True, text=True)
            pids.update(int(x) for x in r.stdout.split())
        own = []
        for pid in pids:
            try:
                args = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
            except OSError:
                continue
            if b"-j" not in args:
                own.append(pid)
        return own

    def _signal_own(self, name, sig):
        for pid in self._own_pids(name):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass

    def _graceful_kill_daq(self):
        """Stop execute_DAQ_v2 so it can close its ROOT file: SIGINT, then SIGTERM, SIGKILL only as a last resort
        (SIGKILL leaves the file without TTree keys)."""
        try:
            targets = self._own_pids('execute_DAQ_v2')
            if not targets:
                return
            for sig in (signal.SIGINT, signal.SIGTERM):
                for pid in targets:
                    try: os.kill(pid, sig)
                    except ProcessLookupError: pass
                for _ in range(8):   # up to ~8s for a clean shutdown per signal
                    time.sleep(1)
                    if not any(os.path.exists(f"/proc/{pid}") for pid in targets):
                        self.controller._log("[INFO] Watchdog: execute_DAQ_v2 stopped gracefully (file closed).")
                        return
            for pid in targets:
                try: os.kill(pid, signal.SIGKILL)
                except ProcessLookupError: pass
            self.controller._log("[WARNING] Watchdog: execute_DAQ_v2 ignored SIGINT/SIGTERM; forced SIGKILL (file may be truncated).")
        except Exception as e:
            self._signal_own('execute_DAQ_v2', signal.SIGKILL)
            self.controller._log(f"[WARNING] Watchdog graceful-kill error ({e}); forced SIGKILL.")

    def _apply_laser_config(self, wl, pulse, bias=0.0):
        """Write the active wavelength/current into config3.h so every DAQ run
        in this block records them (script_v7.sh re-reads the config per run,
        and the values land in RunInfo / Scan History snapshots).

        Laser records the TOTAL drive current (pulse + bias), matching the manual
        convention (e.g. 185 = 182+3). NOTE is deliberately left untouched -- it is
        a user-owned free field; the pulse/bias breakdown is kept in the laser CSV
        logs (LOG/LASER/laser_data_*.csv), not crammed into NOTE."""
        try:
            import re
            path = self.controller.config_manager.filepath
            with open(path, 'r') as f:
                content = f.read()
            nm = wl.replace("nm", "")
            total = float(pulse) + float(bias)
            content = re.sub(r'const std::string Wavelength\s*=\s*".*";',
                             f'const std::string Wavelength = "{nm}";', content)
            content = re.sub(r'const std::string Laser\s*=\s*".*";',
                             f'const std::string Laser = "{round(total, 1):g}";', content)
            # Atomic write: the DAQ launcher reads config3.h on the next run.
            tmp = path + ".tmp"
            with open(tmp, 'w') as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            self.controller.config_manager.reload()
            self.controller._log(f"[INFO] config3.h updated: Wavelength={nm}, "
                                 f"Laser={total:g} mA (pulse {float(pulse):g} + bias {float(bias):g})")
        except Exception as e:
            self.controller._log(f"[WARNING] Failed to update laser config: {e}")


    @staticmethod
    def _format_repeat_points(pairs):
        """Human-readable '30@0, -30@0' for a list of (tilt, rot) pairs."""
        return ", ".join(f"{t:g}@{r:g}" for (t, r) in pairs) if pairs else "None"

    def _record_scan_point(self, axis, tilt, r2, r3, filepath, tag=None):
        """Persist the (axis, tilt) -> RAW-file mapping for the Scan Matrix
        point card (clicking a cell lists every run recorded for that point).
        One JSON per scan date under LOG/ScanHistory, so it survives app
        restarts and also doubles as an independent angle<->run record.
        Keyed by axis_tilt_wavelength (not just axis_tilt): a multi-wavelength
        scan revisits the same (axis, tilt) once per wavelength block on the
        same day, and a plain axis_tilt key would let each block overwrite
        the previous one's record.

        `tag` (e.g. "repeat") is appended to the key and stored as `kind`,
        so a reproducibility-recheck measurement at an angle that's also
        part of the normal scan range lands in its OWN entry instead of
        overwriting the original point -- both stay available for the
        analysis side to compare."""
        # A written RAW file resets the consecutive-failure streak.
        self._consecutive_errors = 0
        try:
            wl = getattr(self, '_current_block_wl', None) or "-"
            date_tag = os.environ.get("SCAN_START_DATE") or datetime.now().strftime("%Y%m%d")
            map_dir = os.path.join(self.controller.base_dir, "LOG", "ScanHistory")
            os.makedirs(map_dir, exist_ok=True)
            map_path = os.path.join(map_dir, f"scanmap_{date_tag}.json")
            data = {}
            if os.path.exists(map_path):
                try:
                    with open(map_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    data = {}
            key = f"{axis}_{tilt}_{wl}" + (f"_{tag}" if tag else "")
            data[key] = {
                "file": filepath, "axis": axis, "tilt": tilt, "wl": wl,
                "rot2": r2, "rot3": r3,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "status": "OK",
                "kind": tag or "scan",
            }
            with open(map_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            self.controller._log(f"[WARNING] Failed to record scan-point map: {e}")

    def _mark_point_error(self, sn2_name, sn3_name, axis, tilt, reason, notify=True):
        """A scan point could not be completed (e.g. a motor Modbus comm
        timeout — see _wait_for_motors). Skip it rather than hanging the whole
        scan: mark the matrix cell ERR, record it in scanmap with
        status="ERROR" (so the point card shows it, and it stays out of the
        'good run' set since no RAW file/analysis was produced for it), and
        append it to a per-date ERROR RUN list for quick review."""
        self.controller._log(f"[ERROR RUN] {axis}-Axis {tilt}° skipped: {reason}")
        self.controller.auto_ui.update_cell(sn2_name, tilt, axis, "error")
        self.controller.auto_ui.update_cell(sn3_name, tilt, axis, "error")
        # In-memory tally for _show_scan_summary().
        if not hasattr(self, '_scan_errors'):
            self._scan_errors, self._scan_warnings = [], []
        self._scan_errors.append(f"{axis}-Axis {tilt}°: {reason}")

        # Slack alert for every skipped point, with axis, angle and the reason.
        notifier = getattr(self.controller, 'notifier', None)
        if notify and notifier and notifier.enabled:
            try:
                notifier.send(
                    f"General Scan: point skipped ({axis}-Axis {tilt}°)",
                    f"Reason: {reason}\n"
                    f"Log: [ERROR RUN] {axis}-Axis {tilt}° skipped: {reason}",
                    level="warning",
                    dedupe_key=f"point_error_{axis}_{tilt}_{reason}",
                    blocking=False, key="scan_point_skipped")
            except Exception as e:
                self.controller._log(f"[WARNING] Point-error notification failed: {type(e).__name__}.")

        # Abort after CONSECUTIVE_ERROR_LIMIT failures in a row (stuck stage or DAQ); one success resets it.
        self._consecutive_errors = getattr(self, '_consecutive_errors', 0) + 1
        if self._consecutive_errors >= self.CONSECUTIVE_ERROR_LIMIT and self.is_running:
            self.controller._log(
                f"[CRITICAL] {self._consecutive_errors} consecutive point failures -- "
                f"aborting the scan instead of continuing to skip every remaining point.")
            notifier = getattr(self.controller, 'notifier', None)
            if notifier and notifier.enabled:
                try:
                    notifier.send(
                        "General Scan ABORTED",
                        f"{self._consecutive_errors} consecutive point failures "
                        f"(latest: {axis}-Axis {tilt}°: {reason}).\n"
                        f"Scan stopped automatically -- check the hardware before resuming.",
                        level="critical", dedupe_key="consecutive_error_abort", blocking=False,
                        key="scan_aborted")
                except Exception as e:
                    self.controller._log(f"[WARNING] Abort notification failed: {type(e).__name__}.")
            self.is_running = False
            self.pause_event.set()   # don't leave a paused thread stuck waiting forever
        try:
            wl = getattr(self, '_current_block_wl', None) or "-"
            date_tag = os.environ.get("SCAN_START_DATE") or datetime.now().strftime("%Y%m%d")
            map_dir = os.path.join(self.controller.base_dir, "LOG", "ScanHistory")
            os.makedirs(map_dir, exist_ok=True)
            map_path = os.path.join(map_dir, f"scanmap_{date_tag}.json")
            data = {}
            if os.path.exists(map_path):
                try:
                    with open(map_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    data = {}
            data[f"{axis}_{tilt}_{wl}"] = {
                "file": None, "axis": axis, "tilt": tilt, "wl": wl,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "status": "ERROR", "reason": reason,
            }
            with open(map_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)

            error_list_path = os.path.join(map_dir, f"error_runs_{date_tag}.txt")
            with open(error_list_path, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\t{axis}_{tilt}\t{reason}\n")
        except Exception as e:
            self.controller._log(f"[WARNING] Failed to record ERROR RUN: {e}")

    def _update_progress_ui(self, current, total):
        # [ETA] Count finished steps of this session; the ETA label is updated only by ui_automation.
        self.scan_done_steps = getattr(self, 'scan_done_steps', 0) + 1
        self.scan_last_done_t = time.time()
        self.scan_total_steps = total
        self.scan_current_step = current
        if hasattr(self.controller, 'ui'):
            eta = self.get_eta_seconds()
            eta_seconds = eta[0] if eta else None
            self.controller.ui.console_set_scan_progress(
                current, total, getattr(self, '_current_axis', None),
                getattr(self, '_current_tilt', None), eta_seconds=eta_seconds,
                slot="general_scan")

    def get_eta_seconds(self):
        """Remaining time from the measured mean per finished step.
        Returns (eta_seconds, current_step, total_steps); a nominal estimate before the first step."""
        total = getattr(self, 'scan_total_steps', 0)
        current = getattr(self, 'scan_current_step', 0)
        if total <= 0:
            return None
        remaining = max(0, total - current)
        done = getattr(self, 'scan_done_steps', 0)
        t0 = getattr(self, 'scan_t0', None)
        if done <= 0 or t0 is None:
            # Before the first finished step: a backend-aware nominal (CAEN ~220 s/pt, HK much shorter).
            if self.controller.auto_ui.dummy_var.get():
                nominal = 1
            elif getattr(self, "daq_backend", "caen") == "hk":
                hk = self.hk_config
                nominal = (float(hk.get("acq_time", 10.0))
                           + float(hk.get("move_delay", 20.0))
                           + self.daq_settle_time)
            else:
                nominal = self._estimate_nominal_per_point_seconds()
            return (remaining * nominal, current, total)
        avg = (self.scan_last_done_t - t0) / done          # measured mean per step
        into_current = time.time() - self.scan_last_done_t  # minus the current step's elapsed time
        eta = max(0.0, avg * remaining - into_current)
        return (eta, current, total)

    # Tells ~/sync_data_v2.sh which date a running scan writes ('<date_tag> <pid>'; stale pid is ignored).
    ACTIVE_SCAN_MARKER = "/tmp/daq_flags/active_scan"

    def _write_active_scan_marker(self, date_tag):
        try:
            os.makedirs(os.path.dirname(self.ACTIVE_SCAN_MARKER), exist_ok=True)
            with open(self.ACTIVE_SCAN_MARKER, "w", encoding="utf-8") as f:
                f.write(f"{date_tag} {os.getpid()}\n")
            self.controller._log(
                f"[INFO] Backup hold marker set for {date_tag} "
                f"-- sync_data_v2.sh will leave this scan's RAW files on local until it finishes.")
        except Exception as e:
            # Never let this stop a scan; worst case is the old false-positive.
            self.controller._log(f"[WARNING] Could not write active-scan marker: {e}")

    def _clear_active_scan_marker(self):
        try:
            if os.path.exists(self.ACTIVE_SCAN_MARKER):
                os.remove(self.ACTIVE_SCAN_MARKER)
                self.controller._log("[INFO] Backup hold marker cleared -- this scan's RAW files may now be reclaimed.")
        except Exception as e:
            self.controller._log(f"[WARNING] Could not clear active-scan marker: {e}")

    # Everywhere a finished RAW file can live (the backup moves local RAW to the external HDD).
    RAW_SEARCH_DIRS = (
        "/home/precalkor/ADC/ADC_test/Data/RAW/Laser",
        "/home/precalkor/Data/RAW/Laser",
        "/home/precalkor/external_HDD_1_4T/Data_Backup/RAW/Laser",
        "/media/precalkor/HD-EDS-E/Data_Backup/RAW/Laser",
        "/home/precalkor/ADC/ADC_test/Data/RAW/Dark",
        "/home/precalkor/Data/RAW/Dark",
        "/home/precalkor/external_HDD_1_4T/Data_Backup/RAW/Dark",
        "/media/precalkor/HD-EDS-E/Data_Backup/RAW/Dark",
    )

    def _locate_raw_file(self, recorded_path):
        """Return a readable path for this run's RAW file, or None if it is
        genuinely nowhere. Checks the recorded path first, then every other
        known RAW location under the same basename."""
        if not recorded_path:
            return None
        if os.path.exists(recorded_path):
            return recorded_path
        base = os.path.basename(recorded_path)
        for d in self.RAW_SEARCH_DIRS:
            cand = os.path.join(d, base)
            if os.path.exists(cand):
                return cand
        return None

    def _verify_scan_files(self, start=None):
        """Post-scan audit: open every point's RAW file and compare it with what the scan intended (angles, events);
        findings go to self._scan_warnings for _show_scan_summary() -- the data exists, so they are not errors."""
        if getattr(self, "daq_backend", "caen") == "hk":
            # HK RAW files live on the 2nd PC: skip the local audit.
            self.controller._log("[INFO] Post-scan file verification skipped: HK Digitizer backend (RAW files live on hkpd, not checked locally).")
            return
        try:
            import uproot
        except ImportError:
            self.controller._log("[WARNING] Post-scan file verification skipped: uproot not installed.")
            return
        date_tag = os.environ.get("SCAN_START_DATE") or datetime.now().strftime("%Y%m%d")
        map_path = os.path.join(self.controller.base_dir, "LOG", "ScanHistory", f"scanmap_{date_tag}.json")
        if not os.path.exists(map_path):
            return
        try:
            with open(map_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            self.controller._log(f"[WARNING] Post-scan verification: couldn't read {map_path}: {e}")
            return

        exp_events = getattr(self, '_scan_expected_events', 0)
        ANGLE_TOL_DEG = 1.0
        # The date's map also holds earlier scans of the same day: audit only this one.
        t0 = start.strftime("%Y-%m-%d %H:%M:%S") if start else ""

        for key, entry in data.items():
            if t0 and str(entry.get("time", "")) < t0:
                continue
            if entry.get("status") != "OK" or entry.get("kind", "scan") != "scan":
                continue   # errors already tallied live; "repeat" points aren't part of the main scan range
            path = self._locate_raw_file(entry.get("file"))
            if path is None:
                recorded = entry.get("file")
                if not recorded:
                    continue
                self._scan_warnings.append(f"{key}: recorded file missing on disk ({recorded})")
                continue
            try:
                ri = uproot.open(path)["RunInfo"]
                g = lambda k: ri[k].array(library="np")[0]
                dec = lambda v: v.decode() if isinstance(v, bytes) else v

                n_entries = uproot.open(path)["T"].num_entries
                if exp_events and n_entries != exp_events:
                    self._scan_warnings.append(
                        f"{key}: event count {n_entries} != expected {exp_events} (truncated/short run?)")

                tilt = entry.get("tilt")
                for dev, angle_key in ((2, "RawTiltAngle2"), (3, "RawTiltAngle3")):
                    try:
                        got = float(g(angle_key))
                        if tilt is not None and abs(got - float(tilt)) > ANGLE_TOL_DEG:
                            self._scan_warnings.append(
                                f"{key}: Dev{dev} tilt in file is {got:.1f}°, expected {tilt}° "
                                f"(stage moved mid-acquisition?)")
                    except Exception:
                        pass
            except Exception as e:
                self._scan_warnings.append(f"{key}: couldn't verify file ({e})")

    def _show_scan_summary(self, start, end, shifter):
        self.save_scan_history(start, end, shifter, is_success=True) # saved on success

        if not hasattr(self, '_scan_errors'):
            self._scan_errors, self._scan_warnings = [], []
        # Audit FIRST (the scan thread normally did), release the backup hold second: it needs the RAW files on disk.
        if not getattr(self, '_scan_audited', False):
            self._verify_scan_files(start)
        self._scan_audited = False
        self._clear_active_scan_marker()

        # Status from the error tally, not an unconditional 'GOOD RUN'; audit findings are warnings.
        errors = list(getattr(self, '_scan_errors', []) or [])
        warnings = list(getattr(self, '_scan_warnings', []) or [])
        skipped = list(getattr(self, '_scan_skipped', []) or [])
        if errors:
            status_line = f"⚠ COMPLETED WITH {len(errors)} ERROR(S)"
            if warnings:
                status_line += f", {len(warnings)} WARNING(S)"
        elif warnings:
            status_line = f"COMPLETED WITH {len(warnings)} WARNING(S)"
        else:
            status_line = "GOOD RUN"
        if skipped:
            status_line = f"⚠ SKIPPED {', '.join(skipped)}" + ("" if status_line == "GOOD RUN" else f"; {status_line}")
        error_block = ("\n".join([f"   - {e}" for e in errors] + [f"   - warning: {w}" for w in warnings])
                       if errors or warnings else None)

        # Compact by default; details are one click away.
        compact = (
            f"📊 Scan Result Summary\n"
            f"--------------------------\n"
            f"• Start: {start.strftime('%H:%M:%S')}\n"
            f"• End: {end.strftime('%H:%M:%S')}\n"
            f"• Shifter: {shifter}\n"
            f"• Target: SN2, SN3\n"
            f"• Run Status: {status_line}\n"
            f"--------------------------\n"
            f"Start the NEXT RUN with UI reset?"
        )
        self.controller._log(
            f"[INFO] Scan Result Summary: {status_line}"
            + (f" ({len(errors)} point(s): {'; '.join(errors)})" if errors else "")
            + (f" (warnings: {'; '.join(warnings)})" if warnings else ""))

        self._notify_scan_finished(start, end, shifter, errors, warnings, skipped)

        # Non-modal: a blocking dialog would stall the next scheduled scan overnight.
        def _next_run():
            self.controller.auto_ui.reset_matrix()
            self.reset_all_angles()
            self.controller._log("User selected NEXT RUN. UI & Hardware Reset initiated.")
            self.controller.refresh_all_data()

        hook = getattr(self._dlg(), "scan_summary", None)
        if hook is not None and self._dlg() is not _TK_DIALOGS:
            hook(compact, error_block, _next_run)
            return
        import tkinter as tk
        from tkinter import ttk
        win = tk.Toplevel(self.controller.master)
        win.title("Scan Completed")
        win.resizable(False, False)
        frame = ttk.Frame(win, padding=16)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frame, text=compact, justify=tk.LEFT,
                 font=("Courier", 10)).pack(anchor="w")

        details_frame = ttk.Frame(frame)   # populated + shown only on demand
        details_visible = {"on": False}

        def _toggle_details():
            if details_visible["on"]:
                details_frame.pack_forget()
                toggle_btn.config(text=f"▸ View details ({len(errors)})")
                details_visible["on"] = False
            else:
                details_frame.pack(fill=tk.X, pady=(8, 0), before=btn_row)
                toggle_btn.config(text="▾ Hide details")
                details_visible["on"] = True

        if error_block:
            ttk.Label(details_frame, text=error_block, justify=tk.LEFT,
                     font=("Courier", 10), foreground="#b91c1c").pack(anchor="w")

        def _on_yes():
            _next_run()
            win.destroy()

        btn_row = ttk.Frame(frame)
        btn_row.pack(fill=tk.X, pady=(12, 0))
        ttk.Button(btn_row, text="Yes", command=_on_yes).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(btn_row, text="No", command=win.destroy).pack(side=tk.RIGHT)
        if error_block:
            toggle_btn = ttk.Button(btn_row, text=f"▸ View details ({len(errors)})",
                                    command=_toggle_details)
            toggle_btn.pack(side=tk.LEFT)

    def stop_automation(self):
        """Safely stops the automation scan sequence and updates grid states."""
        self.is_running = False
        self._clear_active_scan_marker()   # never leave the backup held by an aborted scan
        self.pause_event.set()
        
        for (sn, tilt, axis), cell in self.controller.auto_ui.cells.items():
            if cell.cget("text") != "OK":
                self.controller.auto_ui.update_cell(sn, tilt, axis, "wait")

        self.controller.auto_ui.update_start_button(False)
        
        self.controller._log("[INFO] Automation stopped cleanly. Current progress is preserved.")

    def handle_stop_continue(self):
        if not self.is_running:
            self.controller._log("⚠️ Please start the run first.")
            return

        if self.pause_event.is_set():
            self.pause_event.clear()
            self.controller._log("⏸ Automation Paused. Waiting for Continue...")
            self.controller.auto_ui.update_stop_button(False) 
        else:
            self.pause_event.set()
            self.controller._log("▶ Resuming Automation...")
            self.controller.auto_ui.update_stop_button(True) 

    def abort_run(self):
        if not self.is_running: return

        current = getattr(self, 'scan_current_step', None)
        total = getattr(self, 'scan_total_steps', None)
        axis = getattr(self, '_current_axis', None)
        tilt = getattr(self, '_current_tilt', None)

        self.is_running = False
        self._clear_active_scan_marker()   # never leave the backup held by an aborted scan
        self.pause_event.set()

        notifier = getattr(self.controller, 'notifier', None)
        if notifier and notifier.enabled:
            where = (f"at Point {current}/{total}" if current and total else "") + \
                    (f", {axis}-Axis {tilt}°" if axis is not None else "")
            try:
                notifier.send("General Scan ABORTED", f"Aborted by operator {where}".strip(", "),
                              level="warning", dedupe_key=None, blocking=False, key="scan_aborted")
            except Exception as e:
                self.controller._log(f"[WARNING] Abort notification failed: {type(e).__name__}.")

        is_dummy = self.controller.auto_ui.dummy_var.get()
        if not is_dummy:
            self._signal_own('execute_DAQ_v2', signal.SIGTERM)
            # Also stop the General Scan console job so the slot is free right after Abort.
            if hasattr(self.controller, 'stop_console_job'):
                self.controller.stop_console_job('general_scan')

        if os.path.exists(self.state_file):
            try:
                os.remove(self.state_file)
            except Exception: pass

        self.controller._log("🛑 Scan Aborted by user. Ready for a fresh start.")
        self.controller.auto_ui.update_stop_button(True) 


    def _wait_for_physical_angle(self, dev_num, target_tilt=None, target_rot=None, bypass_check=False):
        """Polls the hardware until target angle is reached, with anti-jam stagnation monitoring."""
        # A device held fixed never reaches a scan rotation; do not wait for it.
        if dev_num == 2 and not self.rotate_dev2:
            self.controller._log("[INFO] Device 2 rotation disabled for this scan -- skipping physical-angle wait.")
            return
        if dev_num == 3 and not self.rotate_dev3:
            self.controller._log("[INFO] Device 3 rotation disabled for this scan -- skipping physical-angle wait.")
            return
        self.controller._log(f"[INFO] Waiting for Device {dev_num} to physically reach target...")
        
        retry_count = 0
        max_retries = 120 # 120 * 0.5s = 60 Seconds Safety Timeout Cap

        # [INTEGRATION] Tracking metrics to catch hardware physical stalls (e.g., caught cables)
        last_tracked_tilt = None
        last_tracked_rot = None
        stagnant_cycles = 0
        max_stagnant_allowed = 10 # 10 * 0.5s = 5 seconds of absolute zero movement

        while self.is_running or bypass_check or not self.pause_event.is_set():
            if self._reset_cancel:   # operator cancelled a Reset Angle mid-wait
                break
            curr_tilt, curr_rot = self.controller.rot_mgr.read_angles(dev_num)

            tilt_ok = True
            rot_ok = True

            if target_tilt is not None:
                tilt_ok = abs(curr_tilt - target_tilt) < 0.5 if curr_tilt is not None else False

            if target_rot is not None:
                rot_ok = abs(curr_rot - target_rot) < 0.5 if curr_rot is not None else False

            if tilt_ok and rot_ok:
                self.controller._log(f"[SUCCESS] Device {dev_num} arrived at physical target smoothly.")
                break

            # Anti-Jamming Verification: Check if the mechanical drive is stuck due to caught cables
            if last_tracked_tilt is not None and curr_tilt is not None and target_tilt is not None:
                if abs(curr_tilt - last_tracked_tilt) < 0.01 and not tilt_ok:
                    stagnant_cycles += 1
                else:
                    stagnant_cycles = 0

            if last_tracked_rot is not None and curr_rot is not None and target_rot is not None:
                if abs(curr_rot - last_tracked_rot) < 0.01 and not rot_ok:
                    stagnant_cycles += 1

            # Trigger emergency interlock action if motor torque stalls for more than 5 seconds
            if stagnant_cycles >= max_stagnant_allowed:
                self.controller._log(f"[CRITICAL] Motor Jam / Cable Snag Detected on Device {dev_num}! Forcing Emergency Stop.")
                
                # Kill hardware movement immediately to prevent torque damage
                self.controller.rot_mgr.stop_rotation(dev_num)
                
                # Pop up immediate critical alert notification window to the active shifter
                self.controller.master.after(0, lambda d=dev_num: self._dlg().showerror(
                    "HARDWARE EMERGENCY INTERLOCK",
                    f"🚨 MECHANICAL BLOCKAGE DETECTED!\n\n"
                    f"Device {d} has stalled for more than 5 seconds.\n"
                    f"The scan sequence has been forced to ABORT to protect cables and drive motors.\n"
                    f"Please inspect the hardware assembly immediately."
                ))
                
                if not bypass_check:
                    self.is_running = False
                    self.pause_event.clear()
                break

            # Cache current coordinates for the next differentiation loop cycle
            last_tracked_tilt = curr_tilt
            last_tracked_rot = curr_rot

            retry_count += 1
            if retry_count > max_retries:
                self.controller._log(f"[CRITICAL] Timeout tracking device {dev_num}. Hardware communication failure suspected.")
                if not bypass_check:
                    self.is_running = False
                    self.pause_event.clear()
                break

            time.sleep(0.5)

    def reset_all_angles(self):
        """Strictly sequential reset: Tilt to 0 -> Confirm -> Rotation to 0 -> Confirm.
        Shows a live progress dialog with a Cancel button; Cancel stops the
        motors and leaves the stage wherever it is."""
        if not self.controller.access_mgr.unlocked:
            self.controller._log("[WARNING] Reset angle blocked: controls are locked.")
            return
        if self.reset_in_progress:
            self.controller._log("[INFO] Reset angle already in progress -- ignoring duplicate click.")
            return
        self.controller._log("[INFO] Reset angle: starting hardware reset thread.")
        self._reset_cancel = False
        self.reset_in_progress = True

        def _do_cancel():
            if self._reset_cancel:
                return
            self._reset_cancel = True
            prog.set_status("Cancelling — stopping motors…")
            self.controller._log("[INFO] Reset Angle: Cancel requested by operator.")
            try:
                self.controller.rot_mgr.stop_rotation(2)
                self.controller.rot_mgr.stop_rotation(3)
            except Exception:
                pass

        hook = getattr(self._dlg(), "progress", None)
        if hook is not None and self._dlg() is not _TK_DIALOGS:
            prog = hook("Reset Angle", "Reset Angle in progress", "Cancel Reset", _do_cancel)
        else:
            prog = self._tk_progress(_do_cancel)

        def _set_status(text):
            prog.set_status(text)

        def _close_win():
            prog.close()

        def _reset_sequence():
            try:
                # TEST RUN never moves the real motors: simulate the phased dialog.
                if self.controller.auto_ui.dummy_var.get():
                    self.controller._log(
                        "[INFO] Reset Angle (TEST RUN): simulating -- no hardware move issued.")
                    _set_status("Phase 1 / 2 · Moving TILT → 0° (simulated)")
                    self._safe_sleep(1.0, bypass_check=True)
                    if not self._reset_cancel:
                        _set_status("Phase 2 / 2 · Moving ROTATION → 0° (simulated)")
                        self._safe_sleep(1.0, bypass_check=True)
                    if self._reset_cancel:
                        self.controller._log("[INFO] Reset Angle (TEST RUN) cancelled.")
                        self.controller.master.after(0, lambda: self._dlg().showinfo(
                            "Reset Cancelled", "Simulated reset was cancelled."))
                    else:
                        self.controller._log("✅ Reset Completed (TEST RUN, simulated).")
                        _set_status("✅ Reset complete (simulated)")
                    return

                # Phase 1: tilt to 0 first; both waits time out on their own and honour Cancel.
                _set_status("Phase 1 / 2 · Moving TILT → 0°")
                self.controller._log("[INFO] Reset Phase 1: Moving TILT to 0.0")
                ok = self._move_safely_stepped(0.0, 0.0, "tilt", bypass_check=True, step_override=self.safe_move_step)
                if ok and not self._reset_cancel:
                    self._wait_for_physical_angle(2, target_tilt=0.0, bypass_check=True)
                    self._wait_for_physical_angle(3, target_tilt=0.0, bypass_check=True)
                    self._safe_sleep(1.5)
                    if not self._reset_cancel:
                        _set_status("Phase 2 / 2 · Moving ROTATION → 0°")
                        self.controller._log("[INFO] Reset Phase 2: Moving ROTATION to 0.0")
                        ok = self._move_safely_stepped(0.0, 0.0, "rot", bypass_check=True)

                if self._reset_cancel:
                    self.controller._log("[INFO] Reset Angle CANCELLED. Stage left at its current position.")
                    self.controller.master.after(0, lambda: self._dlg().showinfo(
                        "Reset Cancelled",
                        "Reset Angle was cancelled.\n\n"
                        "The stage is left wherever it stopped — verify the read-back\n"
                        "angles with 'Get Current' before continuing."))
                elif ok:
                    self._wait_for_physical_angle(2, target_rot=0.0, bypass_check=True)
                    self._wait_for_physical_angle(3, target_rot=0.0, bypass_check=True)
                    self.controller._log("✅ Reset Completed: All axes confirmed at (0.0, 0.0)")
                    _set_status("✅ Reset complete (0°, 0°)")
                    notifier = getattr(self.controller, 'notifier', None)
                    if notifier and notifier.enabled:
                        try:
                            notifier.send("Reset Angle completed", "Both stages confirmed at (0°, 0°).",
                                          level="info", dedupe_key=None, blocking=False, key="reset_angle")
                        except Exception as e:
                            self.controller._log(f"[WARNING] Reset-complete notification failed: {type(e).__name__}.")
                else:
                    self.controller._log(
                        "🚨 [ERROR] Reset Angle FAILED — a motor did not confirm arrival "
                        "(comm timeout). Stage may be at a mid-point, not (0,0).")
                    self.controller.master.after(0, lambda: self._dlg().showerror(
                        "Reset Failed",
                        "Reset Angle stopped partway through — a motor did not confirm\n"
                        "arrival (Modbus comm timeout). The stage is likely NOT at (0, 0).\n\n"
                        "Try Reset again, or move it manually with the Move Tilt/Rot controls\n"
                        "and verify the read-back angle."))
                    notifier = getattr(self.controller, 'notifier', None)
                    if notifier and notifier.enabled:
                        try:
                            rm = self.controller.rot_mgr
                            notifier.send(
                                "Reset Angle FAILED", "Stage may not be at (0,0) -- verify before resuming.\n"
                                f"Dev2: {rm.describe_motion_state(2)}\nDev3: {rm.describe_motion_state(3)}",
                                level="critical", dedupe_key="reset_failed", blocking=False, key="reset_angle_failed")
                        except Exception as e:
                            self.controller._log(f"[WARNING] Reset-failure notification failed: {type(e).__name__}.")
            finally:
                self.reset_in_progress = False
                self._reset_cancel = False
                _close_win()

        threading.Thread(target=_reset_sequence, daemon=True).start()

    def _tk_progress(self, on_cancel):
        """The Tk app's Reset Angle progress window (set_status / close are thread-safe)."""
        import tkinter as tk
        from tkinter import ttk
        win = tk.Toplevel(self.controller.master)
        win.title("Reset Angle")
        win.transient(self.controller.master)
        win.geometry("400x170")
        win.attributes("-topmost", True)
        status_var = tk.StringVar(value="Starting…")
        ttk.Label(win, text="🔄 Reset Angle in progress",
                  font=("Helvetica", 13, "bold")).pack(pady=(16, 4))
        ttk.Label(win, textvariable=status_var, font=("Helvetica", 11),
                  foreground="#007ACC").pack(pady=(0, 6))
        pb = ttk.Progressbar(win, mode="indeterminate", length=320)
        # 40ms (~25Hz) reads just as smooth as a faster tick and avoids
        # needlessly churning the Tk event loop while this dialog is open.
        pb.pack(pady=4); pb.start(40)
        cancel_btn = ttk.Button(win, text="✖ Cancel Reset", command=on_cancel)
        cancel_btn.pack(pady=(6, 10))
        win.protocol("WM_DELETE_WINDOW", on_cancel)
        master = self.controller.master

        class _Progress:
            @staticmethod
            def set_status(text):
                master.after(0, lambda: status_var.set(text))

            @staticmethod
            def close():
                def _c():
                    try: pb.stop()
                    except Exception: pass
                    try: win.destroy()
                    except Exception: pass
                master.after(0, _c)
        return _Progress()

    def emergency_stop(self):
        self.is_running = False
        self._clear_active_scan_marker()   # never leave the backup held by an aborted scan
        self.pause_event.set() 
        
        if hasattr(self.controller, 'rot_mgr'):
            self.controller.rot_mgr.stop_rotation(2)
            self.controller.rot_mgr.stop_rotation(3)

        is_dummy = self.controller.auto_ui.dummy_var.get()
        if not is_dummy:
            self._signal_own('execute_DAQ_v2', signal.SIGKILL)

        self.controller.auto_ui.update_start_button(False)
        
        self.controller._log("[INFO] Scan Aborted: Process stopped and UI initialized.")

    def _verify_recorded_angles(self, file_path, tilt, r2, r3):
        """Confirm the file's RunInfo angles match the point just acquired (a late launch can record the wrong
        angle). True when they agree or cannot be read, False on a real mismatch."""
        macro = (
            'TFile f("%s");'
            'if (f.IsZombie()) { printf("ANGLES ERR zombie\\n"); }'
            'else { TTree* ri=(TTree*)f.Get("RunInfo");'
            '  if (!ri) printf("ANGLES ERR noruninfo\\n");'
            '  else { int a,b,c; ri->SetBranchAddress("RawTiltAngle2",&a);'
            '    ri->SetBranchAddress("RawRotateAngle2",&b);'
            '    ri->SetBranchAddress("RawRotateAngle3",&c); ri->GetEntry(0);'
            '    printf("ANGLES OK %%d %%d %%d\\n", a, b, c); } }'
        ) % file_path
        try:
            p = subprocess.run(['root', '-l', '-b', '-q', '-e', macro],
                               capture_output=True, text=True, timeout=60)
        except Exception as e:
            self.controller._log(f"[WARNING] Angle verification could not run ({e}); skipping check.")
            return True

        line = next((l for l in p.stdout.splitlines() if l.startswith("ANGLES ")), "")
        if not line:
            self.controller._log("[WARNING] Angle verification produced no result; skipping check.")
            return True
        if line.startswith("ANGLES ERR"):
            detail = (f"{os.path.basename(file_path)}: unreadable RunInfo "
                     f"({line.split()[-1]}) — file is not usable")
            self.controller._log(f"[CRITICAL] {detail}.")
            self._last_angle_check_detail = detail
            return False

        try:
            got_t2, got_r2, got_r3 = (int(v) for v in line.split()[2:5])
        except Exception:
            return True
        want = (int(round(tilt)), int(round(r2)), int(round(r3)))
        if (got_t2, got_r2, got_r3) != want:
            detail = (f"{os.path.basename(file_path)}: recorded angles "
                     f"(tilt {got_t2}, rot2 {got_r2}, rot3 {got_r3}) do NOT match the commanded point "
                     f"(tilt {want[0]}, rot2 {want[1]}, rot3 {want[2]}) — "
                     "the stage most likely moved before this acquisition started")
            self.controller._log(f"[CRITICAL] {detail}.")
            self._last_angle_check_detail = detail
            return False
        return True

    def _verify_file_integrity(self, file_path):
        """Checks if the recorded file has a valid size. Thread-safe version."""
        if not os.path.exists(file_path):
            self.controller.master.after(0, lambda: self._dlg().showwarning("File Missing", f"⚠️ File not found!\n{file_path}"))
            return False
            
        file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
        
        if file_size_mb < 0.05: 
            self.controller.master.after(0, lambda: self._dlg().showwarning("Incomplete Data", 
                                   f"⚠️ Integrity Check Failed!\nFile: {os.path.basename(file_path)}\n"
                                   f"Size: {file_size_mb:.2f} MB is too small."))
            return False
        return True

    def save_scan_history(self, start_time, end_time, shifter, is_success=True):
        JST = timezone(timedelta(hours=9))
        end_time_jst = datetime.now(JST)
        
        cfg_snapshot = self.controller.config_manager.get_all_variables()
        if getattr(self, "events_override", None):
            cfg_snapshot = dict(cfg_snapshot, Events=str(int(self.events_override)))
        history_data = {
            "date": end_time_jst.strftime('%Y-%m-%d'),
            "start_time": start_time.strftime('%H:%M:%S'),
            "end_time": end_time_jst.strftime('%H:%M:%S'),
            "shifter": shifter,
            "status": "SUCCESS" if is_success else "ABORTED/ERROR",
            "daq_backend": self.daq_backend_label(),
            "config": cfg_snapshot
        }
        try:   # what was really applied (registry / HV Monitor), beside the config3.h copy
            from managers import applied_conditions
            history_data["applied"] = applied_conditions.collect(
                os.environ.get("SCAN_START_DATE") or end_time_jst.strftime('%Y%m%d'),
                int(getattr(self, "current_scan_block", 0) or 0),
                int(getattr(self, "scan_block_runs", self.SCAN_BLOCK) or self.SCAN_BLOCK), cfg_snapshot)
        except Exception as e:
            self.controller._log(f"[WARNING] Scan history: applied conditions not recorded: {e}")
        
        file_name = f"history_{end_time_jst.strftime('%Y%m%d_%H%M%S')}.json"
        file_path = os.path.join(self.history_dir, file_name)
        
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(history_data, f, indent=4)
            self.controller._log(f"[INFO] Scan history saved: {file_name}")
            
            if hasattr(self.controller.auto_ui, 'refresh_history_list'):
                self.controller.master.after(0, self.controller.auto_ui.refresh_history_list)
        except Exception as e:
            self.controller._log(f"[ERROR] Failed to save scan history: {e}")

    def add_schedule(self, date_str, hour, minute):
        """Add a scheduled scan and save the queue."""
        if len(self.schedules) >= 3:
            self._dlg().showwarning("Limit Reached", "You can only schedule up to 3 runs.")
            return False

        try:
            time_str = f"{hour.zfill(2)}:{minute.zfill(2)}"
            full_str = f"{date_str} {time_str}"
            
            JST = timezone(timedelta(hours=9))
            target_dt = datetime.strptime(full_str, "%Y-%m-%d %H:%M").replace(tzinfo=JST)
            now_jst = datetime.now(JST)

            if target_dt <= now_jst:
                self._dlg().showerror("Time Error", f"Cannot schedule for a past time.\n(Input: {full_str} JST)")
                return False

            cfg_snapshot = self.controller.config_manager.get_all_variables()
            schedule_item = {
                "time_obj": target_dt,
                "time_str": target_dt.strftime("%Y-%m-%d %H:%M"),
                "config": cfg_snapshot
            }

            self.schedules.append(schedule_item)
            self.schedules.sort(key=lambda x: x["time_obj"])
            
            self._save_schedules_to_disk()
            self.controller._log(f"[INFO] ⏰ Schedule added for {schedule_item['time_str']} JST.")
            self._start_schedule_watchdog()
            return True

        except Exception as e:
            self._dlg().showerror("Format Error", f"Invalid input: {e}")
            return False



    def remove_schedule(self, index):
        if 0 <= index < len(self.schedules):
            removed = self.schedules.pop(index)
            self._save_schedules_to_disk()
            self.controller._log(f"[INFO] ⏰ Scheduled run for {removed['time_str']} JST cancelled.")
    
    def _load_hk_config(self):
        try:
            if os.path.exists(self.hk_config_file):
                with open(self.hk_config_file, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                saved = self.hk_legacy_options(saved)
                # merge onto defaults so a new field never KeyErrors
                self.hk_config.update({k: saved[k] for k in saved if k in self.hk_config})
        except Exception as e:
            self.controller._log(f"[WARNING] Failed to load hk_config: {e}")

    def save_hk_config(self, snapshot=False):
        """snapshot=True (the dialog's Save) also keeps a dated copy in
        hk_configs/ so an earlier setup can be loaded back."""
        try:
            with open(self.hk_config_file, "w", encoding="utf-8") as f:
                json.dump(self.hk_config, f, indent=2)
            if snapshot:
                now = datetime.now()
                d = os.path.join(os.path.dirname(self.hk_config_file), "hk_configs")
                os.makedirs(d, exist_ok=True)
                with open(os.path.join(d, now.strftime("hk_config_%Y%m%d_%H%M%S.json")), "w", encoding="utf-8") as f:
                    json.dump(dict(self.hk_config, saved_at=now.strftime("%Y-%m-%d %H:%M:%S")), f, indent=2)
        except Exception as e:
            self.controller._log(f"[ERROR] Failed to save hk_config: {e}")

    def hk_config_snapshots(self):
        """[(path, saved_at, cfg)] of hk_configs/*.json, newest first."""
        d = os.path.join(os.path.dirname(self.hk_config_file), "hk_configs")
        out = []
        for p in sorted(glob.glob(os.path.join(d, "hk_config_*.json")), reverse=True):
            try:
                with open(p, encoding="utf-8") as f:
                    cfg = self.hk_legacy_options(json.load(f))
                out.append((p, cfg.get("saved_at", os.path.basename(p)), cfg))
            except Exception:
                continue
        return out

    def _save_schedules_to_disk(self):
        try:
            save_data = []
            for s in self.schedules:
                save_data.append({
                    "time_str": s["time_str"],
                    "config": s["config"]
                })
            
            with open(self.schedule_file, 'w', encoding='utf-8') as f:
                json.dump(save_data, f, indent=4)
        except Exception as e:
            self.controller._log(f"[ERROR] Failed to save schedules: {e}")

    def _load_schedules_from_disk(self):
        if not os.path.exists(self.schedule_file):
            return

        try:
            with open(self.schedule_file, 'r', encoding='utf-8') as f:
                load_data = json.load(f)
            
            JST = timezone(timedelta(hours=9))
            now_jst = datetime.now(JST)

            for item in load_data:
                target_dt = datetime.strptime(item["time_str"], "%Y-%m-%d %H:%M").replace(tzinfo=JST)
                
                if target_dt > now_jst:
                    self.schedules.append({
                        "time_obj": target_dt,
                        "time_str": item["time_str"],
                        "config": item["config"]
                    })
            
            if self.schedules:
                self.schedules.sort(key=lambda x: x["time_obj"])
                self._start_schedule_watchdog()
                self.controller._log(f"[INFO] Restored {len(self.schedules)} schedules from disk.")
        except Exception as e:
            self.controller._log(f"[ERROR] Failed to load schedules: {e}")

    def _start_schedule_watchdog(self):
        if self.schedule_thread_running: return
        self.schedule_thread_running = True
        threading.Thread(target=self._schedule_watchdog_loop, daemon=True).start()

    def _schedule_watchdog_loop(self):
        JST = timezone(timedelta(hours=9))

        while self.schedule_thread_running:
            if not self.schedules:
                self.schedule_thread_running = False
                break

            now_jst = datetime.now(JST)
            next_run = self.schedules[0]

            if now_jst >= next_run["time_obj"]:
                self.controller._log(f"[INFO] ▶ Scheduled time ({next_run['time_str']} JST) reached. Starting auto-scan...")

                self.schedules.pop(0)

                if hasattr(self.controller.auto_ui, 'refresh_schedule_list'):
                    self.controller.master.after(0, self.controller.auto_ui.refresh_schedule_list)

                # The previous scan ended with errors: skip the next queued run once and log it.
                last_errors = getattr(self, '_scan_errors', None)
                if last_errors:
                    self.controller._log(
                        f"[CRITICAL] Skipping next scheduled scan: the previous scan "
                        f"completed with {len(last_errors)} error(s) "
                        f"({'; '.join(last_errors)}). Resolve the cause and re-queue "
                        f"manually rather than risk repeating the same failure "
                        f"unattended.")
                elif not self.is_running:
                    self.controller.master.after(0, lambda: self.start_general_scan(skip_validation=True))
                else:
                    self.controller._log("[WARNING] Another scan is already running. Scheduled run skipped.")

            time.sleep(5.0) 

    def _update_scan_status_label(self, text, color):
        """Safely updates the scan status label avoiding AttributeError."""
        if hasattr(self.controller, 'auto_ui') and hasattr(self.controller.auto_ui, 'scan_status_label'):
            try:
                self.controller.master.after(0, lambda: self.controller.auto_ui.scan_status_label.config(text=text, foreground=color))
            except Exception:
                pass

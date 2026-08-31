from datetime import datetime, timezone, timedelta
import threading
import time
import os
import re
import json
import glob
import subprocess
import shutil
import tkinter as tk
from tkinter import messagebox, ttk

from managers import angle_convert

class AutomationManager:
    # Whether Slack alerts name the Shifter/Expert. A single switch here (not
    # a GUI toggle yet) per 2026-08-29 agreement -- flip to False to omit
    # names from every alert without touching the call sites below.
    NOTIFY_INCLUDE_OPERATOR = True

    # See _mark_point_error: this many failures IN A ROW aborts the scan
    # instead of running to completion skipping every point.
    CONSECUTIVE_ERROR_LIMIT = 3

    # reason substrings from _mark_point_error() -> a short, canned next step.
    # Kept static rather than inferred: the failure reasons are already a
    # small fixed set (see _mark_point_error call sites), so guessing a fix
    # per-alert would be guessing, not measuring -- same principle as
    # describe_motion_state() in rotation_control.py preferring "undetermined"
    # over an unfounded diagnosis.
    _ERROR_HINTS = (
        ("motor comm timeout", "Scan continues automatically (retry+skip already applied); check the stage if this repeats."),
        ("DAQ never started", "Console may have been busy with another job; check the Console tab."),
        ("DAQ process hung", "execute_DAQ_v2 was force-killed by the watchdog; check the digitizer."),
        ("angles do not match", "Data was written but the angle doesn't match target; treat that run as suspect."),
        ("event count", "Run finished short of the expected event count; treat that run as suspect."),
        ("HV changed mid-scan", "HV was changed from the HV panel during acquisition; the affected run(s) may be invalid."),
        ("missing on disk", "RAW file not found in any known location (local or external); check Data Files."),
    )

    def _error_hint(self, reason):
        for needle, hint in self._ERROR_HINTS:
            if needle in reason:
                return hint
        return "See the log for detail."

    def _operator_line(self, shifter=None, expert=None):
        if not self.NOTIFY_INCLUDE_OPERATOR:
            return ""
        shifter = shifter or "-"
        expert = expert or "-"
        return f"Shifter: {shifter}  ·  Expert: {expert}"

    def _notify_scan_started(self, cfg, sn2_name, sn3_name, shifter, expert):
        notifier = getattr(self.controller, 'notifier', None)
        if not notifier or not notifier.enabled:
            return
        try:
            points_per_axis = len(self.build_tilt_angles())
            n_wl = max(1, len(self.laser_sequence)) if getattr(self, 'laser_sequence', None) else 1
            total_steps = points_per_axis * 2 * n_wl
            is_dummy = self.controller.auto_ui.dummy_var.get()
            nominal_per_pt = 1 if is_dummy else (220 if self.daq_backend != "hk" else 60)
            eta_min = total_steps * nominal_per_pt / 60.0

            wl_list = ", ".join(w for w, _b, _p in self.laser_sequence) if getattr(self, 'laser_sequence', None) else "-"
            lines = [
                f"SN2 {sn2_name} / SN3 {sn3_name}",
                f"HV: {cfg.get('HV1','-')}/{cfg.get('HV2','-')}/{cfg.get('HV3','-')}   "
                f"Tilt step: {self.tilt_step}°   Points: {total_steps}",
                f"Wavelengths: {wl_list}",
                f"Est. duration: ~{eta_min:.0f} min",
            ]
            op = self._operator_line(shifter, expert)
            if op:
                lines.append(op)
            notifier.send("General Scan started", "\n".join(lines), level="info",
                          dedupe_key=None, blocking=False)
        except Exception as e:
            self.controller._log(f"[WARNING] Scan-start notification failed: {type(e).__name__}.")

    def _notify_scan_finished(self, start, end, shifter, errors):
        notifier = getattr(self.controller, 'notifier', None)
        if not notifier or not notifier.enabled:
            return
        try:
            dur_min = (end - start).total_seconds() / 60.0
            if errors:
                # Group by KIND (which of the fixed _ERROR_HINTS this reason
                # matches), not the full per-point text -- "핵심 로그, 해결
                # 방법... 상세할 필요는 없고 어떤 종류인지" (2026-08-29). Each
                # kind is shown once with its count and canned next step,
                # instead of every point's full path/detail.
                by_kind = {}
                for e in errors:
                    reason = e.split(":", 1)[1].strip() if ":" in e else e
                    matched = next((needle for needle, _ in self._ERROR_HINTS if needle in reason), None)
                    key = matched or reason
                    by_kind.setdefault(key, []).append(reason)
                lines = [f"{len(errors)} point(s) affected:"]
                for kind, occurrences in by_kind.items():
                    hint = self._error_hint(occurrences[0])
                    lines.append(f"  • {kind} ×{len(occurrences)} — {hint}")
                op = self._operator_line(shifter, getattr(self, '_current_expert', None))
                if op:
                    lines.append(op)
                notifier.send(f"General Scan finished: BAD RUN ({len(errors)} error(s))",
                              "\n".join(lines), level="warning", dedupe_key=None, blocking=False)
            else:
                lines = [f"Duration: {dur_min:.0f} min"]
                op = self._operator_line(shifter, getattr(self, '_current_expert', None))
                if op:
                    lines.append(op)
                notifier.send("General Scan finished: GOOD RUN", "\n".join(lines),
                              level="info", dedupe_key=None, blocking=False)
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

        # Which digitizer actually takes the data:
        #   "caen" = Korean DAQ (CAEN), the existing path driven by config3.h.
        #   "hk"   = HK Digitizer on the 2nd PC (rotation-synced; config3.h NOT used).
        self.daq_backend = "caen"

        # HK Digitizer (2nd PC) config -- NOT config3.h. Master drives timing
        # open-loop (the HK side can't signal back): trigger acquisition, wait
        # `acq_time`, wait `move_delay`, then rotate. Persisted to disk.
        self.hk_config_file = os.path.join(self.controller.base_dir, "hk_config.json")
        self.hk_config = {
            "run_number": 0,
            "acq_time": 10.0,      # --l : seconds of data-taking per point
            "move_delay": 20.0,    # extra wait AFTER acq before rotating
            "gatelist": 30,        # --gatelist
            "chanlist": 0,         # --chanlist
            "threshold_preset": "",  # --threpreset VALUE; blank = bare flag (no value)
            "trg_channel": "",     # --trgch VALUE; blank = flag omitted entirely
            "trg_vth": "",         # --trgch-Vth VALUE; blank = flag omitted entirely
            "work_dir": "~/hkelec/DiscreteSoftware/data/{date}/",  # cd'd into before ScanManager; {date}=YYYYMMDD
            "ssh_target": "hkpd@hkdaq",              # user@host to run the HK DAQ on
            "setup_cmd": ". ~/setup_hkelec.sh",      # sourced before ScanManager
            # ── Multi-stage pipeline (each runs ON hkpd, streamed to HK console) ──
            # Stage 2: bring up the DPB board (nested ssh hkpd -> root@dpb-local).
            "dpb_setup_cmd": ("ssh root@dpb-local 'cd /run/media/mmcblk0p1/scripts && "
                              "bash run-socat-all.sh; bash run-daq.sh &'"),
            # Stage 3: vmodem data-processing. minicom is interactive (m/RETURN/O
            # then Ctrl-A Z), so we drive it with an auto-generated minicom
            # runscript (-S) -- the keys below are written into ~/vmodem.runscript
            # on hkpd FRESH every time we run this stage, so there's no manual
            # file to keep in sync and m/O can never silently go missing.
            "vmodem_device": "~/hkelec/DiscreteSoftware/DPBDaemon/vmodem/vmodem0",
            "vmodem_keys": "m,O",   # comma-separated keys sent in order via the runscript
            # Absolute path: a non-interactive SSH shell doesn't inherit the
            # interactive PATH, so the bare script name is "command not found".
            "scan_manager": "/home/hkpd/hkelec/pmt-scan/scripts/ScanManager_kawabata_precalib.py",
            # {run}/{tilt}/{rot}/{rot3} auto-fill per point during a scan or
            # manual acquire (see hk_format_run_id) -- included by default so
            # every HK filename is unique (bare {run} -> zero-padded 001,
            # 002, ...) and records the angle it was taken at. Without {run}
            # in the template, run_number kept incrementing internally but
            # every acquisition wrote to the SAME literal "Run000..."
            # filename, silently overwriting the previous run's data on the
            # HK side. {tilt} is the single shared scan-axis angle (same for
            # both mounts); {rot}=SN2 rotation, {rot3}=SN3 rotation -- kept
            # separate since the two devices can sit at different offsets.
            "run_id": "Run{run}_normal_elecV4SN03_testrunDARK_daqch0_trigchnone_hvON_laserOFF_T{tilt}_R2{rot}_R3{rot3}",  # filename (-i)
        }
        self._load_hk_config()

        # Session-only (not persisted): have stages ② DPB Setup / ③ vmodem
        # already been run once? They're RECOMMENDED to run just once (DPB
        # Setup can spawn a duplicate socat/daq daemon if re-run while the
        # first is still up; vmodem's m/O keys may just re-toggle a mode if
        # sent twice) -- not something we should hard-block though, since a
        # DPB reboot legitimately needs ② redone. The dialog uses these flags
        # to ask "already ran this session -- run again?" instead of silently
        # re-firing.
        self.hk_dpb_setup_done = False
        self.hk_vmodem_done = False

        ######### Plz don't modified #########3
        self.tilt_step = 5.0
        self.rot_step = 45.0
        self.safe_move_step = 15.0  
        self.rest_time = 5.0

        self.scan_range = {"start": -55, "end": 55}   # TILT mechanical limit
        self.rot_range  = {"start": 0, "end": 135}     # ROTATION mechanical limit
        # NOTE: tilt is ALWAYS swept raw -55 -> +55 on both devices, never
        # sign-flipped here. A cable whose rotation is folded 180 deg (see
        # _rot_with_offset) does face the mirrored cathode region, but
        # analysis/angle_convert.h already corrects for that from the cable
        # azimuth (GetHamamatsuAngle's xflip/yflip). Flipping the command too
        # would double-correct AND break the raw-stage "start at -55" rule.
        # Angles that must always be visited regardless of tilt_step, so a
        # coarser step (e.g. 7 deg) can never silently drop the mechanical
        # limits or boresight. Merged into the step grid by build_tilt_angles().
        self.mandatory_tilt_angles = [-55, 0, 55]

        # Configurable via Danger Zone -> Params (admin-gated), same as
        # tilt_step/rot_step/rest_time above.
        self.daq_settle_time = 5.0   # seconds to wait after the motor arrives, before triggering DAQ
        # Reproducibility recheck: angles (deg) to revisit and re-measure on
        # BOTH axes right after each wavelength block's own X/Y scan
        # finishes, tagged "repeat" in the scanmap so they're kept alongside
        # (not overwriting) that block's original measurement at the same
        # angle. Empty by default -- no recheck unless the operator lists
        # angles via Danger Zone -> Params.
        self.repeat_angles = []

        # Self-calibrating DAQ throughput (events/sec), used by
        # _execute_daq_point to size its "is this hung?" watchdog timeout
        # instead of a hardcoded guess. Updated after every point that
        # finishes normally (see _execute_daq_point); None until the first
        # one does, so the very first point of a session still needs a safe
        # floor (see DAQ_RATE_FLOOR_EVT_S below).
        self.daq_rate_ema = None

        # Same self-calibration for motor moves (deg/sec), used by
        # _wait_for_motors to size its per-step timeout instead of a flat 90s
        # regardless of whether the step was a 5 deg tilt nudge or a 45 deg rot
        # step. See _wait_for_motors / _move_safely_stepped.
        self.motor_rate_ema = None

        # Worst DAQ start-up latency seen this session (seconds), used to size
        # the launch grace period in _execute_daq_point. Deliberately a MAXIMUM,
        # not an EMA like the two above: start-up is usually a few seconds but
        # spikes when the previous point's analysis chain is grinding a ~2 GB
        # raw file, and an average would be dragged down by the common fast case
        # and re-create the too-tight window that corrupted 2026-08-13's Y scan.
        # Learning here only ever RELAXES the bound -- too short corrupts data,
        # too long merely wastes time on a genuine launch failure.
        self.daq_startup_max = 0.0

        self.schedule_file = os.path.join(self.controller.base_dir, "queued_schedules.json")
        self.schedules = [] 
        self._load_schedules_from_disk()
        self.schedule_thread_running = False
        self.history_dir = os.path.join(self.controller.base_dir, "LOG", "ScanHistory")
        os.makedirs(self.history_dir, exist_ok=True)

    def build_tilt_angles(self):
        """Single source of truth for the tilt-angle list a General Scan
        visits -- the step grid (scan_range/tilt_step) with mandatory_tilt_angles
        merged in and clipped to scan_range, so a step that doesn't evenly
        divide the range can't silently drop -55/0/+55. Both the scan loop
        (_scan_axes_block) and the Scan Progress Matrix (ui_automation.py's
        _current_tilt_angles) call this so they can never disagree."""
        start, end = self.scan_range["start"], self.scan_range["end"]
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
        """Is execute_DAQ_v2 for THIS point currently running?

        script_v7.sh brackets its `eval "$DAQ_COMMAND"` (the line that
        actually runs execute_DAQ_v2) with `touch daq_<run>.flag` right
        before and `rm -f` right after -- both plain bash builtins with no
        gap, so the flag's presence is authoritative for exactly as long as
        the DAQ command is running. This replaces the previous `pgrep -x
        execute_DAQ_v2` check, which had a real race: pgrep can only see the
        process once exec() has actually replaced it, so right at start-up
        (board connect / DAC settle) or in the instant around exit, pgrep can
        legitimately return empty while the point is still very much in
        progress. On 2026-08-28 that false "not running" reading arrived 9s
        into a run that kept acquiring for another 5+ minutes, and the scan
        moved the stage out from under it -- run 242's file ended up
        physically acquired at two different tilt angles. `touch`/`rm` don't
        have that ambiguous window: the file's existence brackets the DAQ
        command's actual lifetime with nothing in between.

        `since`: only count a flag created at/after this point's own launch
        (same `daq_launch_time - 2` slack used elsewhere in this file) --
        otherwise a leftover flag from a previous point that failed to clean
        up (crash, kill -9) would look like this point's own is still active.
        """
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
            # Flag dir doesn't exist yet (first run ever on this machine, or
            # an old script_v7.sh without the flag lines) -- fall back to the
            # process-name check rather than silently always reporting "not
            # running", which would make every point look like an instant
            # failure.
            check = subprocess.run(
                'pgrep -x execute_DAQ_v2 | xargs -r ps -o args= -p 2>/dev/null | grep -v -- "-j"',
                shell=True, capture_output=True, text=True, timeout=15)
            return bool(check.stdout.strip())
        return False

    MOTOR_RATE_FLOOR_DEG_S = 0.5   # pessimistic until motor_rate_ema calibrates

    # How many times a single safe-step is issued before the point is written
    # off. 2 = one retry; see _move_safely_stepped for why a retry is the right
    # response to "the drive answers but the motor did not move".
    MOVE_ATTEMPTS = 2

    # Upper bound on how long a step may keep extending its wait purely because
    # the stage is still creeping toward target. Without a cap, a stage that
    # inches along forever (mechanical bind) would hold the scan indefinitely --
    # the exact overnight-stall shape this whole watchdog exists to prevent.
    MOVE_CREEP_GRACE_S = 120.0

    def _wait_for_motors(self, bypass_check=False, timeout=None, step_deg=None):
        """Poll rot_mgr.is_moving until both devices clear their lock.
        is_moving[dev] is cleared by rot_mgr's background monitor_loop only
        when read_angles(dev) positively confirms arrival — if Modbus reads to
        one device silently start failing (comm glitch), that lock never
        clears and this used to spin forever. Now bounded by `timeout`
        seconds; on timeout, returns False so the caller can skip this point
        instead of hanging the whole scan (this is what caused the 2026-07-12
        22:40 overnight stall: Device 3 never confirmed arrival).

        `timeout=None` (the normal case) self-calibrates from `step_deg` (the
        largest of this step's two device moves) and self.motor_rate_ema, this
        session's own measured deg/sec -- a flat 90s regardless of step size
        made no sense once rot-fold moves (see angle_convert) introduced much
        larger single steps than the old max (45 deg rot_step) alongside much
        smaller ones (5 deg tilt_step). Pass an explicit `timeout` to opt out.
        """
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

        # The wait/timeout/diagnose/release policy itself lives in
        # rot_mgr.wait_until_stopped -- the single place that knows how to give
        # up on a stage. This method only owns the SCAN-side policy: how long a
        # step of this size should take, and that a timeout means "skip this
        # point" rather than "abort the run".
        for dev in (2, 3):
            remaining = max(0.0, deadline - time.time())
            outcome = rm.wait_until_stopped(dev, remaining, should_continue=should_continue)
            if outcome == rm.WAIT_CANCELLED:
                self.controller._log(
                    f"[INFO] _wait_for_motors: wait on Device {dev} cancelled "
                    f"(stop/reset requested) -- not waiting out the full timeout.")
                return False
            if outcome == rm.WAIT_TIMEOUT:
                # wait_until_stopped already logged why (describe_motion_state)
                # and dropped the stale lock so the next point is re-commanded
                # instead of being refused with "already moving".
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

        # 1. 현재 각도 읽기
        curr_t2, curr_r2 = self.controller.rot_mgr.read_angles(2)
        curr_t3, curr_r3 = self.controller.rot_mgr.read_angles(3)

        c2 = curr_t2 if axis_type == "tilt" else curr_r2
        c3 = curr_t3 if axis_type == "tilt" else curr_r3

        if c2 is None: c2 = target_2
        if c3 is None: c3 = target_3

        while self.is_running or bypass_check:
            if self._reset_cancel:   # operator cancelled a Reset Angle mid-move
                return False
            # Pause takes effect at a STEP BOUNDARY, never mid-motor-move: by
            # the time we loop back here the previous step's _wait_for_motors
            # has already confirmed arrival, so holding leaves the stage
            # settled rather than cutting motion. Until 2026-08-26 the loop's
            # only pause check was the _safe_sleep(rest_time) between steps,
            # which is skipped on the final step -- so any move short enough to
            # be a single step (rot_step is 45 deg, and most General Scan
            # rotations are <= that) ran to completion with Pause having no
            # effect at all. Re-check is_running after the wait: emergency_stop
            # sets is_running False *and* releases pause_event, so a Stop
            # pressed while paused would otherwise fall through and issue
            # another move. break (not return False) matches what the loop
            # already does when is_running drops mid-move.
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

            # Re-issue the step if the stage doesn't confirm arrival.
            #
            # The 2026-08-28 21:34 Device 2 loss was NOT a comm failure, even
            # though that is what the log claimed: reconstructing c2 from the
            # "Safe Step" lines shows read_angles(2) kept returning a healthy
            # -15.0 for the following six minutes while the targets marched
            # -5 -> +45 (only c2 = -15 makes every logged next2 come out -10;
            # a failed read would have substituted c2 = target and produced a
            # marching next2 instead). So the drive was answering fine and the
            # motor simply never executed that one move. A second attempt is
            # exactly what that needs -- and it is now possible, because a
            # timed-out wait releases the motion lock instead of leaving the
            # device permanently refusing commands.
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

                # Only re-command a stage that has actually STOPPED short of
                # target. Re-issuing a move pulses STOP first, so doing it to a
                # stage that is merely travelling slower than the timeout
                # allowed would CUT the motion it was about to finish -- a new
                # failure the retry itself would have introduced. While any
                # device is still creeping, wait again WITHOUT re-commanding.
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
        """Rotation target for one PMT's cable `direction`, plus an axis
        `offset` (deg). offset=0 reproduces the X-axis rotation, offset=90 the
        Y-axis rotation. Keeping the cable-derived base means the two PMTs
        (dev2/dev3), which usually have DIFFERENT cable directions, stay
        correctly co-oriented relative to the laser at any offset.

        The raw target spans [0,360) but the stage only reaches rot_range
        [0,135], so an unreachable target is folded by 180 deg -- the same
        scan axis, just facing the mirrored cathode region. This reproduces
        exactly what analysis expects: angle_convert.h's GetXYRotForDirection
        folds mod 180 the same way (cable H -> x_rot 135 / y_rot 45) and
        recovers the mirrored sign itself from the full cable azimuth, so the
        TILT command must stay un-flipped (raw -55 -> +55).

        Without the fold, cable H on the X axis asks for 315 deg: the stepped
        mover walks 180, 225, ... and every intermediate step trips the range
        check (seen 2026-08-12 on Device 2 / SN EM6400 after the cable moved
        B->H)."""
        # Keep this an int: it is handed to execute_DAQ_v2's --rot2/--rot3,
        # which boost::program_options declares as int -- "135.0" aborts the
        # binary with invalid_option_value.
        return angle_convert.rot_with_offset(direction, offset)

    def _get_rot_for_cable(self, axis, direction):
        return self._rot_with_offset(direction, 0 if axis == "X" else 90)

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
        """General Scan moves the stage between every acquisition, so
        script_v7.sh's built-in NumSequences/IntervalTime repeat loop (meant
        for a Stability run's fixed-angle repeats) must never fire here: since
        the loop's rot2/tilt2/... args are fixed for its whole lifetime, any
        sequence after the first records stale angle metadata once the stage
        has moved on to the scan's next point -- and the Python watchdog only
        waits for the FIRST sequence's execute_DAQ_v2 to exit before advancing,
        so the remaining sequences run on as an orphaned background loop the
        scan has no handle to. On 2026-08-15 a leftover NumSequences=100 /
        IntervalTime=600 from an earlier Stability run left ~20+ of these
        orphans piled up in the GeneralScan tmux pane, still issuing
        acquisitions with wrong angles hours after the scan (and even a GUI
        restart) had moved on. Forcing 1/0 here, unconditionally, at the start
        of every General Scan closes that off regardless of what a previous
        session left in config3.h."""
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
                messagebox.showwarning("Locked", "🔒 Please click 'Unlock Controls' first.")
                return

        if self.is_running: return

        self._force_single_sequence_config()

        # Jump the operator to the Live Scan tab automatically -- previously
        # they had to remember to click over, and a scan could run for a
        # while with no one actually watching the plot (2026-08-22, user:
        # "General Scan하면 바로 저기로 옮겨지게 해줘").
        try:
            if hasattr(self.controller, 'auto_ui') and hasattr(self.controller.auto_ui, 'matrix_tab'):
                self.controller.auto_ui.upper_notebook.select(self.controller.auto_ui.matrix_tab)
            if hasattr(self.controller.auto_ui, 'live_scan_view'):
                self.controller.auto_ui.live_scan_view.reset()
        except Exception:
            pass

        os.environ["SCAN_START_DATE"] = datetime.now().strftime("%Y%m%d")
        # Wall-clock start, for Live Scan / Full Grid to tell "this scan's
        # own files" from an earlier scan's -- see live_scan_view.py's
        # _target_block and ui_automation.py's _backfill_matrix_from_scanmap.
        # A run-NUMBER threshold (the old SCAN_START_MIN_RUN) breaks once
        # _assign_run_block reuses a block a reclaimed-RAW earlier scan also
        # used today; wall-clock time never repeats.
        os.environ["SCAN_START_EPOCH"] = str(time.time())
        self._write_active_scan_marker(os.environ["SCAN_START_DATE"])
        # Live Scan view filters by date while a scan is running (see its
        # _target_block()), but a SECOND General Scan run on the SAME date
        # has no run-number filter to fall back on the way the idle view
        # does, so it plotted every result file for today -- both scans'
        # points mixed into one noisy-looking curve (2026-08-28, user: "여러번
        # 쌓이면 이렇게 데이터가 모이던데"). Record the highest run number that
        # already exists for today BEFORE this scan writes anything, so the
        # live view can filter to "> this" and see only points this scan
        # itself produced.
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

        # Snapshot of "what this scan was supposed to record", read back at
        # completion by _verify_scan_files() and compared against what each
        # point's RAW file actually says. HV specifically: the Quick Setup
        # sync warns if HV changes mid-scan (see ui_automation.py), but that
        # only catches the moment it changes -- this catches it even if the
        # warning was missed, by checking every file's own RunInfo against
        # what the scan believed HV was when it started.
        try:
            self._scan_expected_hv = (cfg.get("HV1"), cfg.get("HV2"), cfg.get("HV3"))
            self._scan_expected_events = int(cfg.get("Events", 0) or 0)
        except Exception:
            self._scan_expected_hv = (None, None, None)
            self._scan_expected_events = 0

        # config3.h has no RunMode field -- config_manager.get_all_variables()
        # never returns one, so this was always defaulting to "Laser" below.
        # The General Scan tab's own Scan Mode radio (ui_automation.py) is the
        # actual source of truth for General Scan; inject it into this local
        # cfg dict so every cfg.get("RunMode", ...) downstream (here and in
        # _scan_sequence/_scan_axes_block, which receive this same dict) sees
        # the real choice instead of the silent "Laser" default.
        if hasattr(self.controller.auto_ui, 'scan_mode_var'):
            cfg["RunMode"] = "Dark" if self.controller.auto_ui.scan_mode_var.get() == "dark" else "Laser"

        # ── Laser Sequence (multi-wavelength scan) ─────────────────────────
        # Checked wavelengths run as sequential full-scan blocks (405→375→450→473
        # order). Dark-mode and dummy runs ignore the panel (no laser involved).
        self.laser_sequence = []
        if not is_dummy and cfg.get("RunMode", "Laser").lower() != "dark" \
                and hasattr(self.controller.auto_ui, 'get_laser_sequence'):
            seq = self.controller.auto_ui.get_laser_sequence()
            if seq is None:
                messagebox.showerror("Laser Sequence",
                                     "Invalid Bias/Pulse value in the Laser Sequence panel.")
                return
            if not seq:
                messagebox.showwarning("Laser Sequence",
                                       "No wavelength is selected in the Laser Sequence panel.")
                return
            self.laser_sequence = seq
        n_wl = max(1, len(self.laser_sequence))

        points_per_axis = len(self.build_tilt_angles())
        steps_per_block = points_per_axis * 2
        total_steps = steps_per_block * n_wl
        backend = getattr(self, "daq_backend", "caen")

        # In HK mode the CAEN 220s/step estimate is meaningless -- timing is
        # acq + move_delay + settle, driven open-loop by Master.
        if backend == "hk" and not is_dummy:
            hk = self.hk_config
            per_pt = hk["acq_time"] + hk["move_delay"] + self.daq_settle_time
            hk_total = total_steps * per_pt
            chan_disp = str(hk.get('chanlist') or '').strip() or "(default, --chanlist omitted)"
            self.controller._log(
                f"[INFO] HK Pre-flight: {total_steps} pts ({steps_per_block}×{n_wl} wl), "
                f"acq {hk['acq_time']}s + delay {hk['move_delay']}s + settle {self.daq_settle_time}s "
                f"= {per_pt:.0f}s/pt → est {hk_total/60:.1f} min. "
                f"run# start {hk['run_number']}, target {hk['ssh_target']}, "
                f"gate {hk['gatelist']}, chan {chan_disp}.")
            if not skip_validation:
                rows = [
                    ("Backend:", "HK Digitizer  (local config3.h NOT used)"),
                    ("SSH Target:", hk['ssh_target']),
                    ("ScanManager:", hk['scan_manager']),
                    (None, "---"),
                    ("Run # start:", hk['run_number']),
                    ("Gate / Chan:", f"{hk['gatelist']} / {chan_disp}"),
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
            total_seconds = total_steps * (220 if not is_dummy else 1)

        if backend == "caen" and not is_dummy and not skip_validation:
            raw_path = cfg.get("RawDataPath", "")
            if not os.path.exists(raw_path):
                messagebox.showerror("Error", f"Save path not found:\n{raw_path}")
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
                if not messagebox.askyesno("Storage Warning", warning_msg):
                    return

            sn2, dir2 = cfg.get("SN2", "N/A"), cfg.get("direction2", "N/A")
            sn3, dir3 = cfg.get("SN3", "N/A"), cfg.get("direction3", "N/A")

            rows = [
                ("Backend:", "Korean DAQ (CAEN)  —  local config3.h"),
                ("Est. space:", f"~{estimated_required_gb:.1f} GB required"),
                ("Free space:", f"{free_gb:.1f} GB available  (OK)"),
            ]
            if self.laser_sequence:
                lm = getattr(self.controller, 'laser_mgr', None)
                marks = []
                for wl, _b, _p in self.laser_sequence:
                    inst = lm.laser_instances.get(wl) if lm else None
                    ok = bool(inst and inst.is_connected())
                    marks.append(f"{wl}{'🟢' if ok else '🔴(will skip)'}")
                rows.append(("Laser plan:", f"{n_wl} block(s):  " + "  →  ".join(marks)))
            rows += [
                (None, "---"),
                ("Target SN2:", f"{sn2}   (Cable Dir: {dir2})"),
                ("Target SN3:", f"{sn3}   (Cable Dir: {dir3})"),
                ("Points:", f"{total_steps}   ({steps_per_block} angles × {n_wl} wavelength block(s))"),
            ]
            if not self._wide_confirm(
                    "Pre-flight Checklist", "🚀 Korean DAQ (CAEN) Pre-flight",
                    rows, "Is the hardware setup correct? Start the scan?", accent="#0a84ff"):
                return

        self.resume_data = None
        if os.path.exists(self.state_file):
            if skip_validation:
                self.controller._log("[INFO] ⏰ Scheduled scan: Clearing old recovery data for a clean start.")
                os.remove(self.state_file) 
                ans = False 
            else:
                ans = messagebox.askyesno(
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

        self._assign_run_block(cfg)
        # =========================================================================
        self.is_running = True

        if hasattr(self.controller.auto_ui, 'start_eta_countdown'):
            self.controller.auto_ui.start_eta_countdown(total_seconds, total_steps)

        threading.Thread(target=self._scan_sequence, daemon=True).start()

    def _assign_run_block(self, cfg):
        """Pick the next free 100-run block (000/100/200...) for the scan date.
        Called once at scan start and again for every wavelength block, so each
        wavelength's runs live in their own block — same numbering a separate
        scan session would have received.

        CAEN-only: this numbers .root files under config3.h's RawDataPath,
        which HK Digitizer mode doesn't write to or read (it has its own
        run_number counter in hk_config). No-op there so the log doesn't show
        a meaningless "New Scan Block Assigned" during an HK run."""
        if getattr(self, "daq_backend", "caen") == "hk":
            return
        import re
        raw_path = cfg.get("RawDataPath", "./Data/RAW/")
        date_tag = os.environ.get("SCAN_START_DATE") or datetime.now().strftime("%Y%m%d")
        mode = cfg.get("RunMode", "Laser")
        mode_dir = "Dark" if mode.lower() == "dark" else "Laser"
        search_path = os.path.join(raw_path, mode_dir, f"*_{date_tag}_*.root")

        # Also check FinalResult, not just local RAW: the daily backup
        # (~/sync_data_v2.sh) reclaims local RAW as soon as it verifies
        # against the external copy, so a block used earlier today can look
        # completely unused here once its RAW files are gone -- "which
        # blocks are already spoken for" then silently forgets them, and a
        # later scan the same day gets reassigned that SAME block. Today it
        # only produced an empty Live Scan plot (SCAN_START_MIN_RUN's floor
        # from the old block came out higher than this scan's new, reused-
        # low run numbers), but the same gap could just as easily overwrite
        # that day's earlier FinalResult/production files outright next time
        # (2026-08-29). FinalResult is never touched by the backup reclaim,
        # so it is a reliable record of every block actually used today,
        # RAW-reclaimed or not.
        final_search = os.path.join(os.path.expanduser("~/ADC/ADC_test/Data/FinalResult"),
                                    f"precal_result_kor_run_{date_tag}_*.root")

        max_block = -100
        for pattern in (search_path, final_search):
            for f in glob.glob(pattern):
                match = re.search(r'_([0-9]{3})\.root', f)
                if match:
                    num = int(match.group(1))
                    if num < 700:
                        max_block = max(max_block, (num // 100) * 100)

        self.current_scan_block = max_block + 100 if max_block >= 0 else 0
        self.controller._log(f"[INFO] New Scan Block Assigned: {self.current_scan_block:03d}")

    def schedule_general_scan(self, time_str):
        if not self.controller.access_mgr.unlocked:
            messagebox.showwarning("Locked", "🔒 Please click 'Unlock Controls' first.")
            return
        if self.is_running: return

        try:
            target_time = datetime.strptime(time_str.strip(), "%H:%M").time()
        except ValueError:
            messagebox.showerror("Invalid Time", "Please use HH:MM format (e.g., 14:30).")
            return

        cfg = self.controller.config_manager.get_all_variables()
        is_dummy = self.controller.auto_ui.dummy_var.get()
        points_per_axis = len(self.build_tilt_angles())
        total_steps = points_per_axis * 2

        if not is_dummy:
            raw_path = cfg.get("RawDataPath", "")
            if not os.path.exists(raw_path):
                messagebox.showerror("Error", f"Save path not found:\n{raw_path}")
                return
            
            usage = shutil.disk_usage(raw_path)
            free_gb = usage.free / (1024**3)
            estimated_required_gb = total_steps * 0.8
            
            if free_gb < estimated_required_gb:
                if not messagebox.askyesno("Storage Warning", f"⚠️ Low Storage: {free_gb:.1f} GB left. Schedule anyway?"): 
                    return

            sn2, dir2 = cfg.get("SN2", "N/A"), cfg.get("direction2", "N/A")
            sn3, dir3 = cfg.get("SN3", "N/A"), cfg.get("direction3", "N/A")
            
            if sn2 == "N/A" or sn3 == "N/A":
                if not messagebox.askyesno("Missing Info", "SN2 or SN3 is missing! Schedule anyway?"): return

            checklist_msg = (
                f"⏰ Schedule Pre-flight Checklist (JST)\n\n"
                f"• Target Time: {time_str} (JST)\n"
                f"• Free Space: {free_gb:.1f} GB (OK)\n"
                f"• Target SN2: {sn2} (Dir: {dir2})\n"
                f"• Target SN3: {sn3} (Dir: {dir3})\n\n"
                f"Are these parameters correct? The scan will start automatically at {time_str} JST."
            )
            if not messagebox.askyesno("Schedule Checklist", checklist_msg): return

        self.is_scheduled = True
        
        self.controller.auto_ui.add_auto_log(f"⏰ Scan scheduled successfully for {time_str} (JST).")
        self._update_scan_status_label(f"SCHEDULED: {time_str} (JST)", "#007ACC")
        self.controller.auto_ui.btn_start.config(state=tk.DISABLED)
        
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
        self.controller.auto_ui.btn_start.config(state=tk.NORMAL)

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
        # Reset the per-scan error tally. _mark_point_error() appends here as
        # points fail; _show_scan_summary() reads it at the end so "GOOD RUN"
        # only means that, instead of just "the scan didn't hang" (2026-08-28:
        # a scan with a skipped point AND a mid-scan HV mismatch still showed
        # a plain "GOOD RUN").
        self._scan_errors = []
        self._consecutive_errors = 0
        is_dummy = self.controller.auto_ui.dummy_var.get()
        cfg = self.controller.config_manager.get_all_variables()
        # Same RunMode injection as start_general_scan() -- see comment there.
        # This cfg dict is threaded into _scan_axes_block() below, so the
        # Dark/Laser watchdog-directory switch there sees it too.
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

        # A stray "GeneralScan" tmux session from an old run is exactly the
        # failure mode this whole class of code used to create -- an orphan
        # queue surviving both Stop and an app restart (2026-08-15). Now that
        # run_daq() launches General Scan points through the Console tab
        # (a tracked subprocess.Popen per point, see main.py run_daq) instead
        # of `tmux send-keys`, no gnome-terminal/tmux session is used or
        # needed here any more. Still clean up a session from a PRE-fix
        # session in case one is lingering on this machine.
        if not is_dummy:
            subprocess.run(['tmux', 'kill-session', '-t', 'GeneralScan'], capture_output=True)

        completed_blocks, skipped_blocks = [], []
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
                    # Each wavelength gets its own 100-run block — the same
                    # numbering a separate scan session would have received.
                    if not is_dummy and not first_block:
                        self._assign_run_block(cfg)
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

            # 스캔 완료 처리
            if os.path.exists(self.state_file):
                os.remove(self.state_file)

            self.is_running = False
            if seq:
                summary = f"Completed: {', '.join(completed_blocks) or '-'}"
                if skipped_blocks:
                    summary += f" | Skipped: {', '.join(skipped_blocks)}"
                self.controller._log(f"[INFO] Laser sequence finished. {summary}")
                self.controller.auto_ui.add_auto_log(f"🔦 Laser sequence done. {summary}")
            self.controller._log("Automation sequence completed successfully.")

            # [FIXED] Redirect the UI dialog box creation to the main GUI thread to prevent unexpected Tcl/Tk thread crash
            self.controller.master.after(0, lambda: self._show_scan_summary(start_time, datetime.now(), shifter))

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

                    # [수정 2] 축이 확인/정렬된 직후에 "해당 축의 시작 지점"으로 이동
                    target_init_tilt = start_tilt if skip_until_match else self.scan_range["start"]
                    if axis_ok:
                        self.controller._log(f"[INFO] Axis Aligned. Moving to start tilt: {target_init_tilt}°")
                        axis_ok = self._move_safely_stepped(target_init_tilt, target_init_tilt, "tilt",
                                                 bypass_check=self.is_skipping_validation,
                                                 step_override=self.safe_move_step)
                        self._wait_for_physical_angle(2, target_tilt=target_init_tilt, bypass_check=self.is_skipping_validation)
                        self._wait_for_physical_angle(3, target_tilt=target_init_tilt, bypass_check=self.is_skipping_validation)

                    if not axis_ok:
                        # Motor comm timeout while aligning this axis — no point in
                        # this axis is reachable, so mark all of them ERROR RUN and
                        # move on to the next axis instead of hanging the scan.
                        self.controller._log(
                            f"🚨 [ERROR RUN] {axis}-Axis alignment failed (motor comm timeout). "
                            f"Skipping all {axis}-Axis points this block.")
                        for tilt in self.build_tilt_angles():
                            self._mark_point_error(sn2_name, sn3_name, axis, tilt, "axis alignment failed (motor comm timeout)")
                            current_step += 1
                        self._update_progress_ui(current_step, total_steps)
                        continue

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
                        if "Battery" in ups_msg or "Fail" in ups_msg:
                            self.controller._log("🚨 [INTERLOCK] UPS Battery Mode! Automation paused.")
                            self.pause_event.clear()
                            self.controller.auto_ui.update_stop_button(False)

                    self.pause_event.wait() # Pause 버튼 눌렸을 때 대기
                    if not self.is_running: return "stopped"

                    # Active-wavelength laser health check (multi-wavelength scan):
                    # interlock -> pause the whole scan (safety, operator must resolve);
                    # USB loss  -> abandon this block and move to the next wavelength.
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

                    self._save_state(axis, tilt, current_step)
                    # Live "currently taking data here" marker for the Scan
                    # Matrix point card (cleared in start_general_scan's
                    # finally-equivalent cleanup and when the scan stops).
                    self.current_axis, self.current_tilt = axis, tilt
                    
                    self.controller.auto_ui.update_cell(sn2_name, tilt, axis, "move")
                    self.controller.auto_ui.update_cell(sn3_name, tilt, axis, "move")
                    # Surface "moving vs acquiring" on the Run Control status
                    # line itself, not just the matrix cell color -- operators
                    # not currently looking at the Scan Progress Matrix tab had
                    # no way to tell a motor move from a stall (2026-08-15).
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

                    # 2. 개별 스텝 이동 및 물리적 확인
                    if not is_dummy:
                        self.controller._log(f"[INFO] Scanning: Moving TILT to {tilt} deg...")
                        point_ok = self._move_safely_stepped(tilt, tilt, "tilt", bypass_check=self.is_skipping_validation)
                        if not point_ok:
                            # This is the exact failure mode behind the 2026-07-12 22:40
                            # overnight stall: a motor never confirmed arrival (Modbus
                            # comm timeout) and the scan hung here forever. Now: skip
                            # just this one point (ERROR RUN) and keep scanning.
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

                    # 3. DAQ 실행 및 동기화
                    res = self._execute_daq_point(cfg, is_dummy, sn2_name, sn3_name, axis, tilt, r2, r3)
                    if res == "stopped":
                        return "stopped"

                    # UI 상태 업데이트 (해당 스텝 완료 표시)
                    self.controller.auto_ui.update_cell(sn2_name, tilt, axis, "done")
                    self.controller.auto_ui.update_cell(sn3_name, tilt, axis, "done")

            # ── Reproducibility recheck ─────────────────────────────────────
            # After this wavelength block's full X/Y scan, revisit each
            # configured angle and take one more independent measurement,
            # tagged "repeat" so it's recorded ALONGSIDE (not over) the
            # original point in the scanmap -- lets later analysis compare
            # "first pass" vs "returned to this angle after the full block"
            # to check for drift/repeatability. Skipped entirely if no
            # angles are configured (the default) or in dummy/test-run mode.
            if not is_dummy and self.repeat_angles:
                self.controller._log(
                    "[INFO] Repeatability recheck: revisiting "
                    f"{self._format_repeat_points(self.repeat_angles)} ...")

                dir2 = cfg.get("direction2", "B")
                dir3 = cfg.get("direction3", "B")

                # Resolve each (tilt, rot) pair into concrete motor targets. `rot`
                # is an axis offset on top of each PMT's cable-derived base
                # (rot=0 -> X-scan, rot=90 -> Y-scan), so both PMTs stay
                # co-oriented. The axis LABEL kept for the scanmap key is "X"/"Y"
                # for the two canonical offsets, else "R<rot>" so an off-axis
                # recheck lands in its own entry.
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

                # Group points by rotation: a rotation change forces tilt->0
                # first (hardware interlock) and is the expensive move, so we do
                # all tilts at one rotation before moving to the next. Visit
                # rotation groups nearest-first from the current rotation, and
                # tilts within a group nearest-first from the current tilt, to
                # minimise total motor travel.
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
    def hk_format_remote(hk, run_id, acq_time):
        """Pure formatter: build the remote ScanManager command from an
        hk_config-shaped dict:
        `<setup_cmd> && cd <work_dir> && <scan_manager> -i <run_id> --l <acq>
         --threpreset[N] --gatelist <g> --chanlist <c>`.
        A @staticmethod (not tied to self.hk_config) so the HK Config dialog
        can call it with the UNSAVED on-screen field values to render a live
        "Final Command" preview -- the preview and the real trigger share this
        one formatter, so they can never drift apart."""
        work_dir = (hk.get("work_dir") or "").strip()
        if "{date}" in work_dir:
            work_dir = work_dir.replace("{date}", datetime.now().strftime("%Y%m%d"))
        cd_part = f"cd {work_dir} && " if work_dir else ""
        # --threpreset is a BARE FLAG, not a valued option: ScanManager exposes
        # --threpreset / --threpreset2 / --threpreset3 as three separate flags
        # (preset 1/2/3). So threshold "2" → "--threpreset2" (suffix, no space);
        # blank or "1" → plain "--threpreset". Passing "--threpreset 2" makes
        # ScanManager reject the trailing "2" as an unrecognized argument.
        thr = (hk.get("threshold_preset") or "").strip()
        thr_part = "--threpreset" if thr in ("", "1") else f"--threpreset{thr}"
        # --trgch / --trgch-Vth / --chanlist are all OPTIONAL valued options:
        # emitted only when a value is present, omitted entirely when blank
        # (so leaving a field empty keeps ScanManager's own default for that
        # option instead of sending an empty/invalid flag it would reject).
        trg_ch = str(hk.get("trg_channel") or "").strip()
        trg_vth = str(hk.get("trg_vth") or "").strip()
        chanlist = str(hk.get("chanlist") or "").strip()
        extra = ""
        if trg_ch:
            extra += f" --trgch {trg_ch}"
        if trg_vth:
            extra += f" --trgch-Vth {trg_vth}"
        if chanlist:
            extra += f" --chanlist {chanlist}"
        return (f"{hk['setup_cmd']} && {cd_part}{hk['scan_manager']} "
                f"-i {run_id} --l {int(round(acq_time))} {thr_part} "
                f"--gatelist {hk['gatelist']}{extra}")

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
        """Read-only check: how many `socat` processes are currently alive on
        dpb-local (nested SSH via hkpd, same path DPB Setup itself uses).

        This is the fix for a real incident (2026-07-26): the "already run
        this session" guard on the ② DPB Setup button was a SESSION-ONLY flag
        (hk_dpb_setup_done) that reset to False on every app restart. So after
        a routine restart of the MASTER app, clicking ② again fired
        run-socat-all.sh/run-daq.sh a second time with no warning at all --
        even though the daemons from before the restart were still alive on
        dpb-local -- and a genuine duplicate socat process on port 9001 was
        found running. A local-only flag can never protect against this,
        since the remote daemon's lifetime is independent of the master GUI's.

        This queries actual remote state instead, so the guard survives a
        master-side restart. It is READ-ONLY (pgrep only) and runs entirely
        FROM the master PC over the same SSH link already used everywhere
        else in this class -- it does not install, write, or leave anything
        on the DAQ PC.

        Returns the live process count, or -1 if the check itself failed
        (SSH down/unreachable) -- callers should treat -1 as "unknown", not
        as "confirmed zero".
        """
        rc, out = self._hk_ssh(
            "ssh -o BatchMode=yes -o ConnectTimeout=8 root@dpb-local "
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

        {trgch}/{trgvth} pull straight from hk_config's trg_channel/trg_vth
        (the Config dialog's Trigger ch/Vth fields) -- these are fixed setup
        values, not per-point live data like tilt/rot, so no argument is
        needed for them; blank in the config just becomes an empty string in
        the filename rather than raising."""
        base = self.hk_config.get("run_id", "Run%03d" % run_no)
        tmpl = base.replace("{run}", "{run:03d}")   # bare {run} → 001; {run:0Nd} untouched
        rot3_val = rot3 if rot3 is not None else rot
        trg_ch = self.hk_config.get("trg_channel", "") or ""
        trg_vth = self.hk_config.get("trg_vth", "") or ""
        try:
            return tmpl.format(run=int(run_no), tilt=tilt, rot=rot, rot2=rot, rot3=rot3_val,
                               trgch=trg_ch, trgvth=trg_vth)
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
        run_id = self.hk_format_run_id(run_no, tilt, rot)
        self.controller._log(
            f"[INFO] HK MANUAL acquire run#{run_no} @ tilt {tilt}°, rot {rot}°, acq {acq}s.")
        self.hk_run_in_console(self.hk_build_remote(run_id, acq),
                               job_name=f"HK manual run#{run_no}")
        hk["run_number"] = run_no + 1
        self.save_hk_config()

    def hk_test_trigger(self):
        """Run the current (field-built) command on the HK PC and return
        (rc, output). Used by the HK Config dialog's 'Test Trigger (Dummy)'
        button to sanity-check the SSH link + ScanManager before a real scan."""
        hk = self.hk_config
        remote = self.hk_build_remote(hk.get("run_id", ""), hk.get("acq_time", 10.0))
        self.controller._log(f"[INFO] HK TEST trigger → {hk['ssh_target']}: {remote}")
        rc, out = self._hk_ssh(remote, wait=True, timeout=120)
        self.controller._log(f"[INFO] HK TEST result rc={rc}: {out.strip()[:500]}")
        return rc, out

    def _execute_hk_point(self, axis, tilt, r2, r3, tag=None):
        """One HK-mode acquisition point: trigger → WAIT FOR THE REMOTE SSH
        SESSION TO ACTUALLY EXIT (== ScanManager finished) → wait move_delay
        (buffer before the outer loop rotates) → record and advance the run
        number. Angle is set automatically by the scan loop (motors already
        moved); it is only logged/encoded, never entered by hand.

        This used to blindly `sleep(acq_time)` and assume the acquisition was
        done, regardless of whether the SSH connection was slow, hung, or the
        remote script actually failed. Confirmed via a real Test Trigger run
        (2026-07-26, exit 0) that ScanManager DOES cleanly exit when finished,
        so we now wait for that real signal instead -- catches a slow/failed
        SSH round-trip that the old fixed sleep would have silently ignored,
        without wasting time when a run legitimately finishes early.

        Bounded by a generous timeout (acq*3 + 60s) so a truly hung SSH
        session can't stall the whole scan forever -- on timeout it logs a
        warning and proceeds anyway, degrading to the old blind-sleep
        behavior rather than hanging. All waits are pause/abort aware."""
        hk = self.hk_config
        run_no = hk.get("run_number", 0)
        acq = float(hk.get("acq_time", 10.0))
        delay = float(hk.get("move_delay", 20.0))

        # Encode this point's run # / live angle into the run identifier so each
        # point's data is uniquely labelled. {run}/{tilt}/{rot} placeholders are
        # substituted (bare {run} → 3-digit zero-padded); else used as-is.
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

        # ── HK Digitizer path (2nd PC) ──────────────────────────────────────
        # Trigger acquisition (send run#, live angle, acq_time, threshold),
        # then WAIT FOR THE REMOTE SSH/ScanManager SESSION TO ACTUALLY EXIT
        # (bounded by a timeout), then wait move_delay before the outer loop
        # rotates. config3.h / CAEN watchdog unused. See _execute_hk_point.
        if getattr(self, "daq_backend", "caen") == "hk":
            return self._execute_hk_point(axis, tilt, r2, r3, tag=tag)

        # Reference time for the watchdog: only files created AFTER this
        # launch belong to THIS point. Captured before run_daq so
        # board-init/self-trigger-arming latency doesn't let the watchdog
        # lock onto the PREVIOUS point's finished file.
        daq_launch_time = time.time()
        self.controller.run_daq(tilt=tilt, r2=r2, r3=r3)

        # Wait for execute_DAQ_v2 to actually appear. script_v7.sh has to open
        # the board and settle the DAC before it execs the binary, and that
        # start-up competes with the previous point's background analysis chain
        # (prod_ntp_v7 on a ~2 GB raw file), so this can legitimately take far
        # longer than the 15 s this used to allow.
        #
        # Falling through on timeout is NOT safe: the watchdog loop below would
        # immediately see no process and report "DAQ finished in 0s", i.e. treat
        # "never started" as "already done". The scan then advanced to the next
        # angle and launched ANOTHER run while the previous one was still queued
        # on the single CAEN board. On 2026-08-13 that queued three launches and
        # produced run 113 stamped tilt=-55 but physically acquired at -25, plus
        # two 15 MB zombie files when the watchdog's pkill caught the backlog.
        # So: give it a generous window, and if it still never starts, treat it
        # as a failed launch and clear the backlog instead of marching on.
        # Floor of 90 s until this session has actually measured a start-up;
        # after that, the slowest real start-up seen so far plus a wide margin
        # (see self.daq_startup_max for why it tracks the max, not an average).
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
            # Kill the launcher too, not just the binary: a script_v7.sh still
            # working its way toward exec would otherwise start acquiring after
            # we have already moved the stage, writing a file whose recorded
            # angles no longer match where the PMTs actually are.
            self.controller._log(
                f"[CRITICAL] execute_DAQ_v2 never started within {grace}s "
                f"({axis}-Axis {tilt}°). Clearing any queued launch and skipping this point.")
            subprocess.run(['pkill', '-f', 'script_v7.sh'], capture_output=True)
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

        # Stagnation window (seconds of constant file size before we call it
        # hung). Self-trigger (dark) fills the ROOT baskets more slowly and
        # less evenly than laser's external trigger, so it needs a more
        # forgiving window to avoid killing a healthy-but-slow run between
        # basket flushes.
        stagnation_limit = 60 if mode_dir == "Dark" else 30

        # Self-calibrating: size the "is this hung?" timeout from THIS session's
        # own measured DAQ throughput (self.daq_rate_ema), not a hardcoded
        # guess -- a fixed 600s (this macro's old value; before that 350s)
        # assumed the ~50000-event default (~51s/point observed) and silently
        # killed a HEALTHY 700000-event run (~646s/point, ~1084 evt/s) right as
        # it was about to finish (seen 2026-08-12, run 100 killed at
        # 620000/700000 events -- one whole scan point lost as a false "hung").
        # DAQ_RATE_FLOOR_EVT_S is used only until the first point of a session
        # completes and calibrates the real rate; deliberately pessimistic so
        # that first point can't itself be killed early.
        DAQ_RATE_FLOOR_EVT_S = 400
        try:
            n_events = int(cfg.get("Events", 50000))
        except (TypeError, ValueError):
            n_events = 50000
        rate = self.daq_rate_ema or DAQ_RATE_FLOOR_EVT_S
        max_wait_time = max(600, int(n_events / rate * 1.5) + 120)
        elapsed = 0
        point_file = None     # this point's own RAW file, once it appears
        daq_hung = False

        def _fresh_files():
            # Only files created after this point's DAQ launched. Filters
            # out the PREVIOUS point's completed file, which is otherwise
            # the newest-by-ctime during startup and would make the
            # watchdog tick on a constant-size stale file (the dark-mode
            # "hung" false positive, 2026-07-17).
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
                # See _daq_flag_active's docstring: the flag file brackets
                # execute_DAQ_v2's actual lifetime with no ambiguous window
                # (unlike the pgrep-by-name check this replaces), so a single
                # negative read here is trustworthy -- no debounce/recheck
                # needed the way the old pgrep-based version required.
                self.controller._log(f"[INFO] DAQ finished in {elapsed}s.")
                # Calibrate the watchdog's rate estimate from this real,
                # successfully-completed point (EMA so one slow/fast outlier
                # can't swing the timeout wildly). elapsed>=5 avoids a
                # division blow-up on a near-instant dummy/error return.
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

        if daq_hung:
            self._mark_point_error(sn2_name, sn3_name, axis, tilt,
                                    "DAQ process hung (watchdog killed execute_DAQ_v2)")
        # Verify integrity of THIS point's file (not just the newest in the
        # dir, which could be a later point if timing raced).
        elif point_file is None:
            point_file = max(_fresh_files(), key=os.path.getctime, default=None)
        if not daq_hung and point_file:
            self._verify_file_integrity(point_file)
            # Only record the point if the file actually describes THIS point --
            # a complete-looking file whose RunInfo angles disagree with the
            # commanded ones is worse than a missing one, because analysis
            # cannot tell it apart from good data (see _verify_recorded_angles).
            if self._verify_recorded_angles(point_file, tilt, r2, r3):
                # Persist (axis, tilt) -> RAW file so the Scan Matrix point card
                # can open this point's data later.
                self._record_scan_point(axis, tilt, r2, r3, point_file, tag=tag)
            else:
                # _verify_recorded_angles stashes the specific reason (e.g.
                # "unreadable RunInfo (noruninfo)" vs a genuine angle
                # mismatch) on self -- fall back to the old generic text if
                # it somehow wasn't set, so this stays safe either way.
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

    def _prepare_laser_block(self, block):
        """Blocking laser preparation, called from the scan thread.
        Exclusive LD (all others OFF first) → TEC ON → temperature-stability
        gate → apply Bias/Pulse → LD ON → settle. Returns True when the block
        may start, False to skip this wavelength."""
        wl, bias, pulse = block
        lm = getattr(self.controller, 'laser_mgr', None)
        inst = lm.laser_instances.get(wl) if lm else None
        if not inst:
            self.controller._log(f"[WARNING] Laser {wl} not connected — block skipped.")
            return False
        if not inst.is_connected() and not self._auto_connect_laser(wl):
            self.controller._log(f"[WARNING] Laser {wl} not connected — block skipped.")
            return False
        try:
            for owl, oinst in lm.laser_instances.items():
                if owl != wl and oinst.is_connected():
                    try:
                        oinst.set_ld_on(False)
                    except Exception:
                        pass
            inst.set_tec_on(True)
            self._wait_laser_temp_stable(wl)
            if not self.is_running:
                return False
            inst.set_bias_current(float(bias))
            inst.set_pulse_current(float(pulse))
            time.sleep(0.5)
            inst.set_ld_on(True)
            self.controller._log(f"[INFO] Laser {wl} ready: Bias={bias} mA, Pulse={pulse} mA, LD ON.")
            # Short settle so the first DAQ point is taken with stable output.
            self._safe_sleep(10.0)
            return True
        except Exception as e:
            self.controller._log(f"[WARNING] Laser {wl} preparation failed: {e} — block skipped.")
            return False

    def _wait_laser_temp_stable(self, wl, max_wait=120):
        """TEC temperature-stability gate: proceed when the last ~30 s of the
        telemetry history moved < 0.3 °C (pre-warmed lasers pass immediately);
        fall through after max_wait so a flaky sensor can't hang the scan."""
        hist = getattr(self.controller.laser_mgr, 'plot_history', {}).get(wl, {})
        self.controller._log(f"[INFO] Waiting for {wl} TEC temperature to stabilise (max {max_wait}s)...")
        t0 = time.time()
        while time.time() - t0 < max_wait:
            if not self.is_running:
                return
            temps = list(hist.get("temp", []))[-31:]
            if len(temps) >= 31 and abs(temps[-1] - temps[0]) < 0.3:
                self.controller._log(f"[INFO] {wl} temperature stable ({temps[-1]:.2f} °C).")
                return
            time.sleep(2)
        self.controller._log(f"[INFO] {wl} stability wait timed out — continuing anyway.")

    def _finish_laser_block(self, wl):
        """LD OFF at the end of a wavelength block (safety between blocks).
        TEC is left ON so a later block with the same laser restarts warm."""
        try:
            inst = self.controller.laser_mgr.laser_instances.get(wl)
            if inst and inst.is_connected():
                inst.set_ld_on(False)
                self.controller._log(f"[INFO] Laser {wl} LD OFF (block finished).")
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

    def _graceful_kill_daq(self):
        """Stop execute_DAQ_v2 as cleanly as possible so it can close its ROOT file.
        A hard `pkill -9` (SIGKILL) leaves the file truncated with no TTree keys
        (unreadable) -- that was the cause of the 2026-07-15 corrupt runs. Try
        SIGINT then SIGTERM first (giving the process time to flush/close), and
        only escalate to SIGKILL if it ignores both. Safe fallback: behaves exactly
        like the old hard kill if the process doesn't handle the softer signals."""
        try:
            for sig in ('-INT', '-TERM'):
                subprocess.run(['pkill', sig, 'execute_DAQ_v2'], capture_output=True)
                for _ in range(8):   # up to ~8s for a clean shutdown per signal
                    time.sleep(1)
                    still = subprocess.run('pgrep -x execute_DAQ_v2', shell=True, capture_output=True)
                    if not still.stdout.strip():
                        self.controller._log("[INFO] Watchdog: execute_DAQ_v2 stopped gracefully (file closed).")
                        return
            subprocess.run(['pkill', '-9', 'execute_DAQ_v2'], capture_output=True)
            self.controller._log("[WARNING] Watchdog: execute_DAQ_v2 ignored SIGINT/SIGTERM; forced SIGKILL (file may be truncated).")
        except Exception as e:
            subprocess.run(['pkill', '-9', 'execute_DAQ_v2'], capture_output=True)
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
                             f'const std::string Laser = "{total:g}";', content)
            # Atomic write (temp + os.replace) -- see config_manager.save_from_ui.
            # This fires per wavelength block mid-scan; a torn write would corrupt
            # config3.h for the shell DAQ launcher reading it on the next run.
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
        # A point made it all the way to a written RAW file: whatever was
        # failing has recovered, so the consecutive-failure streak resets
        # (see _mark_point_error / CONSECUTIVE_ERROR_LIMIT). An occasional bad
        # point in an otherwise-healthy scan must never accumulate toward the
        # abort threshold.
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

    def _mark_point_error(self, sn2_name, sn3_name, axis, tilt, reason):
        """A scan point could not be completed (e.g. a motor Modbus comm
        timeout — see _wait_for_motors). Skip it rather than hanging the whole
        scan: mark the matrix cell ERR, record it in scanmap with
        status="ERROR" (so the point card shows it, and it stays out of the
        'good run' set since no RAW file/analysis was produced for it), and
        append it to a per-date ERROR RUN list for quick review."""
        self.controller._log(f"[ERROR RUN] {axis}-Axis {tilt}° skipped: {reason}")
        self.controller.auto_ui.update_cell(sn2_name, tilt, axis, "error")
        self.controller.auto_ui.update_cell(sn3_name, tilt, axis, "error")
        # Also tally in-memory so _show_scan_summary() can report an accurate
        # status instead of an unconditional "GOOD RUN" (2026-08-28: a scan
        # with a skipped point still showed GOOD RUN, because nothing read
        # this back at the end -- it was only ever written to disk).
        if not hasattr(self, '_scan_errors'):
            self._scan_errors = []
        self._scan_errors.append(f"{axis}-Axis {tilt}°: {reason}")

        # Per-point Slack alert -- every skipped point, not just the
        # consecutive-failure abort case below. Includes the axis/angle and
        # the exact reason/log line so it's actionable from the phone without
        # opening the GUI (2026-08-31, user: "에러가 생기면 Slack에 알림...
        # 이유와 로그, 그리고 축에서 몇도인지").
        notifier = getattr(self.controller, 'notifier', None)
        if notifier and notifier.enabled:
            try:
                notifier.send(
                    f"General Scan: point skipped ({axis}-Axis {tilt}°)",
                    f"Reason: {reason}\n"
                    f"Log: [ERROR RUN] {axis}-Axis {tilt}° skipped: {reason}",
                    level="warning",
                    dedupe_key=f"point_error_{axis}_{tilt}_{reason}",
                    blocking=False)
            except Exception as e:
                self.controller._log(f"[WARNING] Point-error notification failed: {type(e).__name__}.")

        # Abort after CONSECUTIVE_ERROR_LIMIT failures IN A ROW: a scan that
        # keeps skipping every point (stage genuinely stuck, DAQ genuinely
        # down) was allowed to run to completion, quietly producing hours of
        # empty data (2026-08-29, user: "모터가 완전히 고착되었는데도 의미 없는
        # 빈 데이터를 쌓으며 3~4시간을 허비하는 것을 막는 방어 기제"). A single
        # success anywhere resets the streak (see _record_scan_point) -- this
        # is about a STUCK failure mode, not an occasional bad point, which
        # the retry/skip logic already handles fine on its own.
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
                        level="critical", dedupe_key="consecutive_error_abort", blocking=False)
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
        # [ETA] 실제로 한 스텝이 끝날 때마다 '이번 세션' 완료 수와 시각을 기록한다.
        # 화면 라벨 갱신은 ui_automation.update_eta_realtime 가 1초마다 단독으로 담당한다
        # (여기서 직접 라벨을 쓰면 두 곳이 충돌해 깜빡인다).
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
        """완료된 스텝들의 실측 평균으로 남은 시간을 추정한다.
        반환: (eta_seconds, current_step, total_steps). 아직 데이터가 없으면 nominal 추정."""
        total = getattr(self, 'scan_total_steps', 0)
        current = getattr(self, 'scan_current_step', 0)
        if total <= 0:
            return None
        remaining = max(0, total - current)
        done = getattr(self, 'scan_done_steps', 0)
        t0 = getattr(self, 'scan_t0', None)
        if done <= 0 or t0 is None:
            # 첫 스텝 완료 전: 대략값(실측 전이라 어쩔 수 없음). backend마다
            # 포인트당 시간이 크게 달라서 nominal을 backend-aware로 잡는다 --
            # CAEN은 ~220s/pt, HK는 acq+move_delay+settle(보통 ~20s)이라, 예전
            # 처럼 무조건 220을 쓰면 HK 스캔 시작 직후 ETA가 몇 배로 과대표시됐다
            # (예: 46pt x 220s = 2h48m인데 실제론 ~15분). 첫 스텝이 완료되면
            # 어차피 아래 실측 평균으로 대체된다.
            if self.controller.auto_ui.dummy_var.get():
                nominal = 1
            elif getattr(self, "daq_backend", "caen") == "hk":
                hk = self.hk_config
                nominal = (float(hk.get("acq_time", 10.0))
                           + float(hk.get("move_delay", 20.0))
                           + self.daq_settle_time)
            else:
                nominal = 220
            return (remaining * nominal, current, total)
        avg = (self.scan_last_done_t - t0) / done          # 세션 실측 평균/스텝
        into_current = time.time() - self.scan_last_done_t  # 현재 스텝 경과분 차감
        eta = max(0.0, avg * remaining - into_current)
        return (eta, current, total)

    # Marker telling OTHER processes (specifically ~/sync_data_v2.sh) that a
    # General Scan is in flight and which date tag it is writing. The backup
    # script reclaims local RAW files as soon as their external copy verifies,
    # which is correct for finished data but raced this scan's own post-run
    # audit: _verify_scan_files() re-opens every point's RAW file, and files
    # already reclaimed mid-scan were reported as "recorded file missing on
    # disk" -- 19 and 35 false errors on the 2026-08-28/29 runs, on scans that
    # were otherwise clean. Contains "<date_tag> <pid>" so a stale marker from
    # a crashed GUI cannot block backups forever: the reader ignores it once
    # the pid is gone.
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

    # Every place a finished RAW file can legitimately live. The daily backup
    # (~/sync_data_v2.sh) copies local RAW to the external HDD and then deletes
    # the local original, so "not at the recorded path" does NOT mean the run
    # was lost -- it usually means the backup already reclaimed it. Looking
    # only at the recorded path produced 19 and 35 phantom "recorded file
    # missing on disk" errors on the 2026-08-28/29 scans, both of which were
    # otherwise clean. Search every known location instead, so the audit's
    # result cannot depend on backup timing at all.
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

    def _verify_scan_files(self):
        """Post-hoc audit: open every point's own RAW file and check it
        against what the scan intended, instead of only trusting whatever was
        (or wasn't) flagged live via _mark_point_error(). That in-flight
        tally can't catch a point that "succeeded" -- file written, no
        launch/motor error -- but with WRONG metadata inside it. That is
        exactly what happened on 2026-08-28: run 242 was written with no
        error at all, but was physically acquired at two different tilt
        angles (a watchdog false-positive moved the stage mid-acquisition),
        and runs 243/244 recorded the OLD HV while the PMTs were actually
        running at a HV changed 50V higher mid-scan. Appends any mismatch
        found to self._scan_errors, same tally _show_scan_summary() reads.
        """
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

        exp_hv = getattr(self, '_scan_expected_hv', (None, None, None))
        exp_events = getattr(self, '_scan_expected_events', 0)
        ANGLE_TOL_DEG = 1.0

        for key, entry in data.items():
            if entry.get("status") != "OK" or entry.get("kind", "scan") != "scan":
                continue   # errors already tallied live; "repeat" points aren't part of the main scan range
            path = self._locate_raw_file(entry.get("file"))
            if path is None:
                recorded = entry.get("file")
                if not recorded:
                    continue
                self._scan_errors.append(f"{key}: recorded file missing on disk ({recorded})")
                continue
            try:
                ri = uproot.open(path)["RunInfo"]
                g = lambda k: ri[k].array(library="np")[0]
                dec = lambda v: v.decode() if isinstance(v, bytes) else v

                n_entries = uproot.open(path)["T"].num_entries
                if exp_events and n_entries != exp_events:
                    self._scan_errors.append(
                        f"{key}: event count {n_entries} != expected {exp_events} (truncated/short run?)")

                tilt = entry.get("tilt")
                for dev, angle_key in ((2, "RawTiltAngle2"), (3, "RawTiltAngle3")):
                    try:
                        got = float(g(angle_key))
                        if tilt is not None and abs(got - float(tilt)) > ANGLE_TOL_DEG:
                            self._scan_errors.append(
                                f"{key}: Dev{dev} tilt in file is {got:.1f}°, expected {tilt}° "
                                f"(stage moved mid-acquisition?)")
                    except Exception:
                        pass

                for hv_key, expected in zip(("HV2", "HV3"), exp_hv[1:]):
                    if expected is None:
                        continue
                    try:
                        got = str(dec(g(hv_key))).strip()
                        if got and got != str(expected).strip():
                            self._scan_errors.append(
                                f"{key}: {hv_key} in file is {got}, expected {expected} "
                                f"(HV changed mid-scan and not saved before this point?)")
                    except Exception:
                        pass
            except Exception as e:
                self._scan_errors.append(f"{key}: couldn't verify file ({e})")

    def _show_scan_summary(self, start, end, shifter):
        self.save_scan_history(start, end, shifter, is_success=True) # 성공 시 저장

        if not hasattr(self, '_scan_errors'):
            self._scan_errors = []
        # Audit FIRST, release the backup hold second: _verify_scan_files()
        # needs this scan's RAW files still on local disk to open them.
        self._verify_scan_files()
        self._clear_active_scan_marker()

        # Accurate status instead of an unconditional "GOOD RUN" -- a scan
        # that skipped a point (DAQ launch timeout, motor comm failure, ...)
        # used to still show GOOD RUN because nothing here ever looked at
        # _mark_point_error()'s tally (2026-08-28).
        errors = list(getattr(self, '_scan_errors', []) or [])
        if errors:
            status_line = f"⚠ COMPLETED WITH {len(errors)} ERROR(S)"
            error_block = "\n".join(f"   - {e}" for e in errors)
        else:
            status_line = "GOOD RUN"
            error_block = None

        # Compact by default -- the error list can get long on a bad scan, and
        # most of the time (GOOD RUN) there's nothing to show anyway. Details
        # are one click away instead of always taking up space, same idea as
        # the old plain popup but without dumping everything at once.
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
            + (f" ({len(errors)} point(s): {'; '.join(errors)})" if errors else ""))

        self._notify_scan_finished(start, end, shifter, errors)

        # Non-modal: no grab_set(), no wait_window(). A blocking askyesno()
        # here stalls the ENTIRE Tk event loop -- including the thread that
        # starts the NEXT scheduled scan (Schedule Manager allows up to 3
        # queued runs) -- until a human clicks it. Nobody may be at the
        # machine when an overnight scan finishes, so every queued scan after
        # the first would sit stuck behind this exact dialog (same failure
        # mode as the "Console Busy" popup that froze a scan for 3h20m on
        # 2026-08-28, but this one fires on every normal completion, not just
        # a watchdog misfire). The window stays open and answerable whenever
        # someone does return; the scan pipeline itself doesn't wait on it.
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
            self.controller.auto_ui.reset_matrix()
            self.reset_all_angles()
            self.controller._log("User selected NEXT RUN. UI & Hardware Reset initiated.")
            self.controller.refresh_all_data()
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
                              level="warning", dedupe_key=None, blocking=False)
            except Exception as e:
                self.controller._log(f"[WARNING] Abort notification failed: {type(e).__name__}.")

        is_dummy = self.controller.auto_ui.dummy_var.get()
        if not is_dummy:
            subprocess.run(['pkill', '-f', 'execute_DAQ_v2'])
            # Also stop the General Scan console job directly (kills the whole
            # script_v7.sh process group via stop_console_job's os.killpg), so
            # the "general_scan" console slot is freed the instant Abort is
            # pressed instead of waiting out script_v7.sh's own post-kill
            # cleanup -- matters if the operator immediately switches to
            # Manual and clicks Run DAQ right after Abort.
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
                self.controller.master.after(0, lambda d=dev_num: messagebox.showerror(
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

        # ── Progress dialog (built on the main thread) ──────────────────────
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

        def _do_cancel():
            if self._reset_cancel:
                return
            self._reset_cancel = True
            status_var.set("Cancelling — stopping motors…")
            self.controller._log("[INFO] Reset Angle: Cancel requested by operator.")
            try:
                self.controller.rot_mgr.stop_rotation(2)
                self.controller.rot_mgr.stop_rotation(3)
            except Exception:
                pass

        cancel_btn = ttk.Button(win, text="✖ Cancel Reset", command=_do_cancel)
        cancel_btn.pack(pady=(6, 10))
        win.protocol("WM_DELETE_WINDOW", _do_cancel)

        def _set_status(text):
            self.controller.master.after(0, lambda: status_var.set(text))

        def _close_win():
            def _c():
                try: pb.stop()
                except Exception: pass
                try: win.destroy()
                except Exception: pass
            self.controller.master.after(0, _c)

        def _reset_sequence():
            try:
                # TEST RUN (Simulation Mode) must never touch the real motors --
                # every other DAQ/scan path already checks dummy_var, but this
                # one never did, so a Reset Angle click during a TEST RUN scan
                # physically moved the stage (2026-08-15, user: "테스트 런하고
                # 있는데 ... Reset 누르니까 진짜 각도가 움직이네"). Simulate the
                # same phased dialog experience instead of calling into rot_mgr.
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
                        self.controller.master.after(0, lambda: messagebox.showinfo(
                            "Reset Cancelled", "Simulated reset was cancelled."))
                    else:
                        self.controller._log("✅ Reset Completed (TEST RUN, simulated).")
                        _set_status("✅ Reset complete (simulated)")
                    return

                # 1단계: 기울기부터 0도로 이동. bypass_check=True ignores is_running/
                # pause; both _move_safely_stepped and _wait_for_physical_angle
                # time out on their own (2026-07-13 hang fix) and now also honour
                # self._reset_cancel so the Cancel button actually interrupts.
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
                    self.controller.master.after(0, lambda: messagebox.showinfo(
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
                                          level="info", dedupe_key=None, blocking=False)
                        except Exception as e:
                            self.controller._log(f"[WARNING] Reset-complete notification failed: {type(e).__name__}.")
                else:
                    self.controller._log(
                        "🚨 [ERROR] Reset Angle FAILED — a motor did not confirm arrival "
                        "(comm timeout). Stage may be at a mid-point, not (0,0).")
                    self.controller.master.after(0, lambda: messagebox.showerror(
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
                                level="critical", dedupe_key="reset_failed", blocking=False)
                        except Exception as e:
                            self.controller._log(f"[WARNING] Reset-failure notification failed: {type(e).__name__}.")
            finally:
                self.reset_in_progress = False
                self._reset_cancel = False
                _close_win()

        threading.Thread(target=_reset_sequence, daemon=True).start()

    def emergency_stop(self):
        self.is_running = False
        self.pause_event.set() 
        
        if hasattr(self.controller, 'rot_mgr'):
            self.controller.rot_mgr.stop_rotation(2)
            self.controller.rot_mgr.stop_rotation(3)

        is_dummy = self.controller.auto_ui.dummy_var.get()
        if not is_dummy:
            subprocess.run(['pkill', '-9', 'execute_DAQ_v2'], capture_output=True)

        self.controller.auto_ui.update_start_button(False)
        
        self.controller._log("[INFO] Scan Aborted: Process stopped and UI initialized.")

    def _verify_recorded_angles(self, file_path, tilt, r2, r3):
        """Confirm the file's own RunInfo matches the point we just acquired.

        A file can be complete, correctly sized, and still be the WRONG data:
        on 2026-08-13 a queued launch started acquiring only after the stage
        had moved on, so run 113 carried tilt=-55 in RunInfo while physically
        sitting at -25. Nothing downstream could tell -- it looked like a
        perfectly good point and went straight into the uniformity fit.

        Returns True when the angles agree (or when they cannot be read, so a
        ROOT hiccup never fails a good run), False on a real mismatch.
        """
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
            self.controller.master.after(0, lambda: messagebox.showwarning("File Missing", f"⚠️ File not found!\n{file_path}"))
            return False
            
        file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
        
        if file_size_mb < 0.05: 
            self.controller.master.after(0, lambda: messagebox.showwarning("Incomplete Data", 
                                   f"⚠️ Integrity Check Failed!\nFile: {os.path.basename(file_path)}\n"
                                   f"Size: {file_size_mb:.2f} MB is too small."))
            return False
        return True

    def save_scan_history(self, start_time, end_time, shifter, is_success=True):
        JST = timezone(timedelta(hours=9))
        end_time_jst = datetime.now(JST)
        
        cfg_snapshot = self.controller.config_manager.get_all_variables()
        history_data = {
            "date": end_time_jst.strftime('%Y-%m-%d'),
            "start_time": start_time.strftime('%H:%M:%S'),
            "end_time": end_time_jst.strftime('%H:%M:%S'),
            "shifter": shifter,
            "status": "SUCCESS" if is_success else "ABORTED/ERROR",
            "config": cfg_snapshot
        }
        
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
        """[수정본] 스케줄 추가 및 파일 저장"""
        if len(self.schedules) >= 3:
            messagebox.showwarning("Limit Reached", "You can only schedule up to 3 runs.")
            return False

        try:
            time_str = f"{hour.zfill(2)}:{minute.zfill(2)}"
            full_str = f"{date_str} {time_str}"
            
            JST = timezone(timedelta(hours=9))
            target_dt = datetime.strptime(full_str, "%Y-%m-%d %H:%M").replace(tzinfo=JST)
            now_jst = datetime.now(JST)

            if target_dt <= now_jst:
                messagebox.showerror("Time Error", f"Cannot schedule for a past time.\n(Input: {full_str} JST)")
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
            messagebox.showerror("Format Error", f"Invalid input: {e}")
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
                # merge onto defaults so a new field never KeyErrors
                self.hk_config.update({k: saved[k] for k in saved if k in self.hk_config})
        except Exception as e:
            self.controller._log(f"[WARNING] Failed to load hk_config: {e}")

    def save_hk_config(self):
        try:
            with open(self.hk_config_file, "w", encoding="utf-8") as f:
                json.dump(self.hk_config, f, indent=2)
        except Exception as e:
            self.controller._log(f"[ERROR] Failed to save hk_config: {e}")

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

                # If the PREVIOUS scan ended with recorded errors, don't just
                # blindly launch the next queued one -- an unresolved cause
                # (motor comm fault, HV changed mid-scan, ...) will very
                # likely fail the same way again, and unattended it would
                # burn hours producing more bad data with nobody aware until
                # someone happens to open the (now non-blocking) completion
                # popup. Skip once, log loudly, and require the operator to
                # re-queue after checking -- better than silently repeating a
                # known-bad run (2026-08-28).
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

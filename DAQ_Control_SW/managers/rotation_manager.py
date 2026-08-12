from datetime import datetime, timezone, timedelta
import threading
import time
import os
import json
import glob
import subprocess
import shutil
import tkinter as tk
from tkinter import messagebox, ttk

from managers import angle_convert

class AutomationManager:
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
            self.pause_event.wait()
            if not self.is_running and not bypass_check: break
            time.sleep(0.5)

    MOTOR_RATE_FLOOR_DEG_S = 0.5   # pessimistic until motor_rate_ema calibrates

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
        t_start = time.time()
        waited = 0.0
        while self.is_running or bypass_check:
            is_moving_2 = self.controller.rot_mgr.is_moving.get(2, False)
            is_moving_3 = self.controller.rot_mgr.is_moving.get(3, False)
            if not is_moving_2 and not is_moving_3:
                if step_deg and waited >= 1.0:   # skip near-zero noops
                    observed_rate = step_deg / (time.time() - t_start)
                    self.motor_rate_ema = (observed_rate if self.motor_rate_ema is None
                                           else 0.7 * self.motor_rate_ema + 0.3 * observed_rate)
                return True
            time.sleep(0.5)
            waited += 0.5
            if waited >= timeout:
                stuck = [d for d, m in (("2", is_moving_2), ("3", is_moving_3)) if m]
                self.controller._log(
                    f"[CRITICAL] _wait_for_motors timeout ({timeout:.0f}s): "
                    f"Device {', '.join(stuck)} never confirmed arrival "
                    f"(Modbus comm failure suspected).")
                return False
        return False

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
            diff2 = target_2 - c2
            diff3 = target_3 - c3

            if abs(diff2) <= 0.5 and abs(diff3) <= 0.5:
                break

            move2 = min(abs(diff2), step_size) * (1 if diff2 > 0 else -1) if abs(diff2) > 0.5 else 0
            move3 = min(abs(diff3), step_size) * (1 if diff3 > 0 else -1) if abs(diff3) > 0.5 else 0

            next2 = c2 + move2
            next3 = c3 + move3

            self.controller._log(f"[INFO] Safe Step {axis_type.upper()}: Dev2 -> {next2:.1f}, Dev3 -> {next3:.1f}")

            if axis_type == "tilt":
                if move2 != 0: self.controller.rot_mgr.move_tilt_only(2, next2, skip_lock=bypass_check)
                if move3 != 0: self.controller.rot_mgr.move_tilt_only(3, next3, skip_lock=bypass_check)
            else:
                if move2 != 0: self.controller.rot_mgr.move_rot_only(2, next2, skip_lock=bypass_check)
                if move3 != 0: self.controller.rot_mgr.move_rot_only(3, next3, skip_lock=bypass_check)

            if not self._wait_for_motors(bypass_check, step_deg=max(abs(move2), abs(move3))):
                return False

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

    def start_general_scan(self, skip_validation=False):
        self.is_skipping_validation = skip_validation

        if not skip_validation:
            if not self.controller.access_mgr.unlocked:
                messagebox.showwarning("Locked", "🔒 Please click 'Unlock Controls' first.")
                return
        
        if self.is_running: return

        os.environ["SCAN_START_DATE"] = datetime.now().strftime("%Y%m%d")

        cfg = self.controller.config_manager.get_all_variables()
        is_dummy = self.controller.auto_ui.dummy_var.get()

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

        max_block = -100
        for f in glob.glob(search_path):
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

        if not is_dummy:
            subprocess.run(['tmux', 'kill-session', '-t', 'GeneralScan'], capture_output=True)
            term_cmd = ['gnome-terminal', '--title=General Scan DAQ', '--', 'tmux', 'new-session', '-s', 'GeneralScan']
            subprocess.Popen(term_cmd)
            time.sleep(2.0)

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

        startup_wait = 0
        while startup_wait < 15:
            if not self.is_running: return "stopped"
            check = subprocess.run('pgrep -x execute_DAQ_v2 | xargs -r ps -o args= -p 2>/dev/null | grep -v -- "-j"', shell=True, capture_output=True, text=True, timeout=15)
            if check.stdout.strip():
                break
            time.sleep(1); startup_wait += 1

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

            check_proc = subprocess.run('pgrep -x execute_DAQ_v2 | xargs -r ps -o args= -p 2>/dev/null | grep -v -- "-j"', shell=True, capture_output=True, text=True, timeout=15)
            if not check_proc.stdout.strip():
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
            # Persist (axis, tilt) -> RAW file so the Scan Matrix point card
            # can open this point's data later.
            self._record_scan_point(axis, tilt, r2, r3, point_file, tag=tag)

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

    def _show_scan_summary(self, start, end, shifter):
        self.save_scan_history(start, end, shifter, is_success=True) # 성공 시 저장
        
        summary = (
            f"📊 Scan Result Summary\n"
            f"--------------------------\n"
            f"• Start: {start.strftime('%H:%M:%S')}\n"
            f"• End: {end.strftime('%H:%M:%S')}\n"
            f"• Shifter: {shifter}\n"
            f"• Target: SN2, SN3\n"
            f"• Run Status: GOOD RUN\n"
            f"--------------------------\n"
            f"Start the NEXT RUN with UI reset?"
        )
        ans = messagebox.askyesno("Scan Completed", summary)
        if ans is True:
            self.controller.auto_ui.reset_matrix()
            self.reset_all_angles()
            self.controller._log("User selected NEXT RUN. UI & Hardware Reset initiated.")
            self.controller.refresh_all_data()

    def stop_automation(self):
        """Safely stops the automation scan sequence and updates grid states."""
        self.is_running = False
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

        self.is_running = False
        self.pause_event.set() 

        is_dummy = self.controller.auto_ui.dummy_var.get()
        if not is_dummy:
            subprocess.run(['pkill', '-f', 'execute_DAQ_v2'])

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

                if not self.is_running:
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

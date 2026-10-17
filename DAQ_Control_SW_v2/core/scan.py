"""General Scan in v2: the Tk app's AutomationManager (managers/rotation_manager.py) runs
unchanged on a host object that offers what it expects from the Tk App -- logging, a
main-thread dispatcher, the stage and laser managers, the Output-tab consoles, the
scan-panel facade (auto_ui) and the config manager. One scan engine for both apps.

The GUI injects two things before the first use: dispatch(fn, delay_ms) that runs fn on
the GUI thread, and a dialogs object (messagebox-style methods plus wide_confirm /
scan_summary / progress). Everything else here is Qt-free; ScanUI reports to listeners.
"""
import json
import os
import re
import threading
import time
import types
from datetime import datetime, timedelta, timezone

from core import env  # puts the Tk app's modules on sys.path
from core.access import ACCESS
from core.jobs import JOBS

JST = timezone(timedelta(hours=9))
SEQ_ORDER = ["405nm", "375nm", "450nm", "473nm"]


class Var:
    """tk.Variable stand-in."""

    def __init__(self, value=None):
        self._v = value

    def get(self):
        return self._v

    def set(self, value):
        self._v = value


class Widget:
    """Minimal widget stand-in: config()/cget() just record the options."""

    def __init__(self, on_change=None):
        self.opts = {}
        self._on_change = on_change

    def config(self, **kw):
        self.opts.update(kw)
        if self._on_change:
            self._on_change(self)

    configure = config

    def cget(self, key):
        return self.opts.get(key, "")


class _Master:
    def __init__(self, host):
        self.host = host

    def after(self, ms, fn=None):
        if fn is not None:
            self.host.dispatch(fn, int(ms))
        return None

    def after_cancel(self, _id):
        pass

    def winfo_exists(self):
        return True


class _Notebook:
    def __init__(self, host, on_select=None):
        self.host, self.on_select = host, on_select

    def after(self, ms, fn=None):
        return self.host.master.after(ms, fn)

    def select(self, tab=None):
        if self.on_select:
            self.on_select(tab)


class ScanUI:
    """What AutomationManager calls on the Tk scan panel (ui_automation.AutomationUI)."""

    def __init__(self, host):
        self.host = host
        self.listeners = []
        self.dummy_var = Var(False)        # TEST RUN (Simulation Mode)
        self.scan_mode_var = Var("laser")  # Laser (multi-wavelength) / Dark (single scan)
        self.btn_start = Widget(lambda w: self._emit("start_button", w.opts.get("state")))
        self.scan_status_label = Widget(lambda w: self._emit("status", (w.opts.get("text"), w.opts.get("foreground"))))
        self.cells = {}
        self.cell_state = {}               # (sn, tilt, axis) -> wait/move/daq/done/error
        self.matrix_text = ""
        self.notebook = _Notebook(host)
        self.upper_notebook = _Notebook(host, lambda tab: self._emit("show_live_scan", None))
        self.matrix_tab = object()
        self.live_scan_view = self
        self.eta = None                    # (total_seconds, total_steps) of the last countdown start

    def _emit(self, event, payload=None):
        for fn in list(self.listeners):
            try:
                fn(event, payload)
            except Exception:
                pass

    @property
    def sn2_val(self):
        return self.host.config_manager.get_all_variables().get("SN2", "SN2")

    @property
    def sn3_val(self):
        return self.host.config_manager.get_all_variables().get("SN3", "SN3")

    # live_scan_view.reset()
    def reset(self):
        self._emit("live_reset")

    def update_cell(self, sn, tilt, axis, status):
        self.cell_state[(sn, tilt, axis)] = status
        self._emit("cell", (sn, tilt, axis, status))

    def reset_matrix_cells(self):
        self.cell_state.clear()
        self._emit("cells_reset")

    def reset_matrix(self):
        self.cell_state.clear()
        self.eta = None
        self._emit("cells_reset")
        self._emit("eta", None)
        self._emit("live_reset")

    def set_matrix_wavelength(self, text):
        self.matrix_text = text
        self._emit("matrix_wavelength", text)

    def update_start_button(self, is_running, status_text=None):
        self._emit("running", (is_running, status_text))

    def update_stop_button(self, is_running):
        self._emit("pause_button", is_running)

    def start_eta_countdown(self, total_seconds, total_steps):
        self.eta = (total_seconds, total_steps)
        self._emit("eta", self.eta)

    def refresh_schedule_list(self):
        self._emit("schedules")

    def refresh_history_list(self):
        self._emit("history")

    def add_auto_log(self, message):
        """Same file and line format as the Tk panel (logs/scan_live_<date>.txt, JST)."""
        now = datetime.now(JST)
        try:
            path = os.path.join(self.host.base_dir, "logs", f"scan_live_{now:%Y-%m-%d}.txt")
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"[{now:%Y-%m-%d %H:%M:%S}] {message}\n")
        except OSError:
            pass
        self._emit("log", message)

    def update_config_angles(self, sn, tilt, rot):
        """Write Tilt/RotateAngle<n> of this PMT into the config header (atomic), as the Tk panel does."""
        try:
            path = self.host.config_manager.filepath
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            dev = "2" if sn == self.sn2_val else "3"
            t, r = int(round(tilt)), int(round(rot))
            content = re.sub(rf'const std::string TiltAngle{dev}\s*=\s*".*";',
                             f'const std::string TiltAngle{dev} = "{t}";', content)
            content = re.sub(rf'const std::string RotateAngle{dev}\s*=\s*".*";',
                             f'const std::string RotateAngle{dev} = "{r}";', content)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except Exception as e:
            self.host._log(f"[WARNING] Failed to update config file for {sn}: {e}")

    def laser_sequence_path(self):
        return os.path.join(self.host.base_dir, "laser_sequence.json")

    def load_laser_sequence(self):
        try:
            with open(self.laser_sequence_path(), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save_laser_sequence(self, data):
        with open(self.laser_sequence_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def get_laser_sequence(self):
        """[(wl, bias, pulse)] for checked lasers, 405 -> 375 -> 450 -> 473; None if a value is not a number."""
        data = self.load_laser_sequence()
        seq = []
        for wl in SEQ_ORDER:
            v = data.get(wl) or {}
            if not v.get("on"):
                continue
            try:
                seq.append((wl, float(v.get("bias", "")), float(v.get("pulse", ""))))
            except (TypeError, ValueError):
                return None
        return seq

    @staticmethod
    def _format_rotate_devices(auto_mgr):
        d2 = getattr(auto_mgr, "rotate_dev2", True)
        d3 = getattr(auto_mgr, "rotate_dev3", True)
        if d2 and d3:
            return "PMT1 + PMT2 (both)"
        if d2:
            return "PMT1 only (PMT2 fixed)"
        if d3:
            return "PMT2 only (PMT1 fixed)"
        return "NEITHER (invalid)"


class _Consoles:
    """controller.ui as the engine uses it: console status / per-point progress lines."""

    def __init__(self, host):
        self.host = host
        self.ups_vars = {"status_msg": Var("")}

    def ensure_console_pane(self, slot, parent=None):
        pass

    def focus_console(self, slot):
        pass

    def console_set_status(self, text, slot="analysis", state="idle"):
        self.host.scan_ui._emit("console_status", (slot, text, state))

    def console_begin_point(self, *args, **kw):
        self.host.scan_ui._emit("point", (args, kw))

    def console_set_scan_progress(self, *args, **kw):
        self.host.scan_ui._emit("progress", (args, kw))


class _AccessView:
    @property
    def unlocked(self):
        return ACCESS.unlocked

    def verify_password_prompt(self, *args, **kw):
        return self._verify(*args, **kw) if hasattr(self, "_verify") else False


class ScanHost:
    """The parts of the Tk App (main.py) that AutomationManager and RotationManager use."""

    def __init__(self, dispatch, dialogs):
        from config_manager import ConfigManager
        from managers.notifier import Notifier
        from managers.rotation_manager import AutomationManager
        from core import laser as L
        from core.stage import stage
        self.dispatch = dispatch
        self.dialogs = dialogs
        self.base_dir = env.DAQ_DIR
        self.master = _Master(self)
        self.access_mgr = _AccessView()
        self.config_manager = ConfigManager(env.config_file())
        self.notifier = Notifier(log_fn=self._log)
        self._stage = stage()
        self._stage.host.dialogs = dialogs
        self.laser_mgr = None if env.NO_HW else L.laser()
        self.ui = _Consoles(self)
        self.scan_ui = ScanUI(self)
        self.auto_ui = self.scan_ui
        self.log_listeners = []
        self.auto_mgr = AutomationManager(self)
        self._stage.scan_ranges = self.auto_mgr   # the stage uses the scan engine's mechanical limits
        engine_start = self.auto_mgr.start_general_scan

        def guarded_start(skip_validation=False):
            """Both apps load queued_schedules.json; only the hardware owner may start a scan."""
            import instance_lock
            if instance_lock.is_running("daq"):
                self._log("[WARNING] General Scan not started in v2: the Tk DAQ app is open and owns the hardware.")
                return
            if env.NO_HW and not self.scan_ui.dummy_var.get():
                self._log("[WARNING] General Scan not started: hardware is disabled (V2_NO_HW); only TEST RUN works.")
                return
            return engine_start(skip_validation=skip_validation)
        self.auto_mgr.start_general_scan = guarded_start
        # one schedule runner: core.schedule owns queued_schedules.json, the engine only starts the scan
        self.auto_mgr.schedules = []
        self.auto_mgr.schedule_thread_running = False
        self.auto_mgr._start_schedule_watchdog = lambda: None
        from core import hk, schedule
        schedule.set_starter(lambda: self.auto_mgr.start_general_scan(skip_validation=True),
                             is_running=lambda: self.auto_mgr.is_running,
                             last_errors=lambda: getattr(self.auto_mgr, "_scan_errors", None),
                             dispatch=lambda fn: self.dispatch(fn, 0))
        hk.attach(self.auto_mgr)

    @property
    def ups_mgr(self):
        """The engine pauses on UPS trouble when ups_mgr.ups_serial is set (see its UPS interlock)."""
        if env.NO_HW:
            return types.SimpleNamespace(ups_serial=None)
        from core import ups
        svc = ups.service()
        self.ui.ups_vars["status_msg"].set((svc.reading or {}).get("status", "") if svc.owns else "")
        return types.SimpleNamespace(ups_serial=svc.ser if svc.owns else None)

    @property
    def rot_mgr(self):
        if self._stage.mgr is None and not env.NO_HW:
            self._stage._ensure_monitoring()
        return self._stage.mgr

    # ---- App methods ----------------------------------------------------------
    def _log(self, message):
        """logs/log_<date>.txt, the Tk app's format, so its Log tab and the remote page see it."""
        try:
            d = os.path.join(self.base_dir, "logs")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, f"log_{datetime.now():%Y-%m-%d}.txt"), "a") as f:
                f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}\n")
        except OSError:
            pass
        for fn in list(self.log_listeners):
            try:
                fn(message)
            except Exception:
                pass

    def refresh_all_data(self):
        self.config_manager.reload()
        self.scan_ui._emit("refresh")

    def _run_job_in_console(self, command, job_name="Job", env=None, slot="analysis", on_complete=None):
        cmd = command[0] if len(command) == 1 else " ".join(command)
        if JOBS.running(slot):
            self._log(f"[WARNING] Console Busy: a job is already running in slot '{slot}'; launch refused.")
            return
        self._log(f"[INFO] Console job '{job_name}' [{slot}]: {cmd}")
        JOBS.start_in(slot, job_name, cmd, env=env, on_done=on_complete)

    def stop_console_job(self, slot="analysis"):
        JOBS.stop(slot)

    def run_daq(self, tilt=None, r2=None, r3=None, category=None):
        """General Scan point acquisition, as the Tk main.run_daq does while a scan runs."""
        if not ACCESS.unlocked:
            self._log("[WARNING] Run DAQ refused: controls are locked.")
            return
        am = self.auto_mgr
        daq_path = self.config_manager.get_config_value("BasePath") or self.config_manager.get_config_value(
            "DaqProgramPath")
        if not daq_path:
            self._log("[ERROR] BasePath or DaqProgramPath not found in config file.")
            return
        mode = self.scan_ui.scan_mode_var.get() if am.is_running else "laser"
        start_block = "900" if self.scan_ui.dummy_var.get() else str(getattr(am, "current_scan_block", 0))
        command = [os.path.join(daq_path, "script_v7.sh"), mode, self.config_manager.filepath]
        if tilt is not None and r2 is not None and r3 is not None:
            ri2, ti2 = int(round(float(r2))), int(round(float(tilt)))
            ri3, ti3 = int(round(float(r3))), int(round(float(tilt)))
            command += [str(ri2), str(ti2), str(ri3), str(ti3)]
            self._log(f"[INFO] Injecting live angles to DAQ -> R2:{ri2}, T2:{ti2}, R3:{ri3}, T3:{ti3}")
        else:
            command += ["0", "0", "0", "0"]
        command.append(start_block)
        from core.daq_diag import probe_env
        run_env = probe_env(daq_path)
        run_env["FILE_FORMAT"] = "root"
        fixed_date = os.environ.get("SCAN_START_DATE") or datetime.now().strftime("%Y%m%d")
        run_env["SCAN_START_DATE"] = fixed_date
        for k in ("SCAN_START_EPOCH",):
            if os.environ.get(k):
                run_env[k] = os.environ[k]
        prev = JOBS.jobs.get("general_scan")
        if prev is not None and prev.state == "running":
            t0 = time.time()
            while prev.state == "running" and time.time() - t0 < 10:
                time.sleep(0.2)
            if prev.state == "running":
                self._log("[WARNING] Previous General Scan console job still busy after "
                          "10s wait; launching anyway may be refused.")
        self._run_job_in_console(command, job_name="General Scan DAQ", env=run_env, slot="general_scan")
        self._log(f"[INFO] Auto Mode - Fixed Date Injected: {fixed_date}")


_host = None
_lock = threading.Lock()


def host(dispatch=None, dialogs=None):
    """The scan host, created on first call (the GUI passes its dispatcher and dialogs)."""
    global _host
    with _lock:
        if _host is None:
            if dispatch is None or dialogs is None:
                raise RuntimeError("scan host not initialised by the GUI")
            _host = ScanHost(dispatch, dialogs)
        return _host

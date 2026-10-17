"""SN2/SN3 stages (device 2, 3) through the Tk app's RotationManager, so both apps share one
register map, decoder, motion lock, arrival check and rotate-only-at-tilt-0 interlock.

Ownership: v2 talks to the drives while the Tk app is closed, or while v2 holds the controls
(core.access). Otherwise it lets go and the Tk app's logs/live_angles.json is used instead.
Manual moves and stops need Unlock; scan moves come from the scan engine (core.scan);
out of range angles are refused by RotationManager itself, as in the Tk app.
"""
import json
import os
import threading
import time

from core import env  # puts instance_lock on sys.path
import instance_lock  # noqa: E402
from core.access import ACCESS

TILT_RANGE = {"start": -55, "end": 55}   # same mechanical limits as the Tk app
ROT_RANGE = {"start": 0, "end": 135}
MOVING_WINDOW_S = 2.0
LIVE_ANGLES = os.path.join(env.DAQ_DIR, "logs", "live_angles.json")
RESTART_DELAY_S = 3.0     # longer than one monitor sleep, so an old loop has exited


class _AccessView:
    @property
    def unlocked(self):
        return ACCESS.unlocked    # same rule as the Tk app: manual moves need Unlock, scans pass skip_lock


class _Ranges:
    """The scan engine's mechanical limits once it exists, else the same defaults (as in the Tk app)."""

    def __init__(self, stage):
        self.stage = stage

    @property
    def scan_range(self):
        src = self.stage.scan_ranges
        return src.scan_range if src is not None else TILT_RANGE

    @property
    def rot_range(self):
        src = self.stage.scan_ranges
        return src.rot_range if src is not None else ROT_RANGE


class _Host:
    """The attributes RotationManager reads from the Tk controller."""

    def __init__(self, stage):
        self.base_dir = env.DAQ_DIR
        self.access_mgr = _AccessView()
        self.auto_mgr = _Ranges(stage)
        self.dialogs = None          # set by the GUI (out-of-range / interlock messages)
        self.log = []

    def _log(self, msg):
        self.log = (self.log + [(time.time(), msg)])[-50:]


class Stage:
    def __init__(self):
        self.scan_ranges = None      # the AutomationManager, once the scan host exists
        self.host = _Host(self)
        self.mgr = None
        self.latest = {}          # dev -> (tilt, rot, monotonic ts)
        self.reads = {2: 0, 3: 0}
        self._lock = threading.Lock()
        self._tk_seen = 0.0
        self._live_state = {}

    # ---- ownership ---------------------------------------------------------
    def owns_drives(self):
        return ACCESS.unlocked or not instance_lock.is_running("daq")

    def start(self):
        if env.NO_HW:
            return
        threading.Thread(target=self._supervise, daemon=True).start()

    def _supervise(self):
        while True:
            if self.owns_drives():
                if time.monotonic() - self._tk_seen >= RESTART_DELAY_S:
                    self._ensure_monitoring()
            else:
                self._tk_seen = time.monotonic()
                self._release()
            time.sleep(1.0)

    def _ensure_monitoring(self):
        if self.mgr is None:
            try:
                from managers.rotation_control import RotationManager
                self.mgr = RotationManager(self.host)
            except Exception as e:
                self.host._log(f"ERROR: stage init failed: {e!r}")
                return
        if not self.mgr.is_monitoring:
            self.mgr.start_monitoring(self._on_read)

    def _release(self):
        if self.mgr is not None and self.mgr.is_monitoring:
            self.mgr.stop_monitoring()
            for dev in (2, 3):
                self.mgr._invalidate_client(dev)
        with self._lock:
            self.latest = {}

    def _on_read(self, dev, tilt, rot):
        if tilt is None or rot is None:
            return
        self.reads[dev] += 1
        with self._lock:
            self.latest[dev] = (tilt, rot, time.monotonic())
        self._write_live_angles(dev, tilt, rot)

    def _write_live_angles(self, dev, tilt, rot):
        """logs/live_angles.json in the Tk app's format, while v2 owns the drives."""
        now = time.time()
        prev = self._live_state.get(str(dev))
        if prev and now - prev["ts"] < 0.5:
            return
        try:
            moving = bool(self.mgr.is_moving.get(dev, False))
        except Exception:
            moving = False
        self._live_state[str(dev)] = {"tilt": tilt, "rot": rot, "moving": moving, "ts": now}
        try:
            tmp = LIVE_ANGLES + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._live_state, f)
            os.replace(tmp, LIVE_ANGLES)
        except OSError:
            pass

    # ---- reading -----------------------------------------------------------
    def angles(self, max_age=3.0):
        """{dev: (tilt, rot, moving)} from reads newer than max_age seconds."""
        now = time.monotonic()
        with self._lock:
            latest = dict(self.latest)
        out = {}
        for d, (tilt, rot, ts) in latest.items():
            if now - ts > max_age:
                continue
            moving = bool(self.mgr and (self.mgr.is_moving.get(d) or
                                        (self.reads[d] > 1 and now - self.mgr._last_motion_ts[d] < MOVING_WINDOW_S)))
            out[d] = (tilt, rot, moving)
        return out

    def read_now(self, dev):
        if self.mgr is None or not self.owns_drives():
            return None, None
        return self.mgr.read_angles(dev)

    # ---- control (unlocked only) -------------------------------------------
    def _check(self):
        if not ACCESS.unlocked:
            return "Controls are locked."
        if self.mgr is None or not self.mgr.is_monitoring:
            return "Not connected to the stage drives."
        return None

    def _last_error(self, since):
        errs = [m for t, m in self.host.log if t >= since and ("ERROR" in m or "WARNING" in m)]
        return errs[-1] if errs else "Command was not sent."

    def move(self, dev, axis, value):
        """axis 'tilt' or 'rot'. -> (ok, message). Blocks for one Modbus sequence; call off the GUI thread."""
        why = self._check()
        if why:
            return False, why
        t0 = time.time()
        fn = self.mgr.move_tilt_only if axis == "tilt" else self.mgr.move_rot_only
        ok = fn(dev, float(value), skip_lock=True)   # access was checked above; skip_lock also suppresses Tk dialogs
        return (True, "") if ok else (False, self._last_error(t0))

    def stop(self, dev):
        why = self._check()
        if why:
            return False, why
        self.mgr.stop_rotation(dev)
        return True, ""


_stage = None


def stage():
    global _stage
    if _stage is None:
        _stage = Stage()
        _stage.start()
    return _stage


def reader():
    return stage()

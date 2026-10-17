"""Stability Run logic as the Tk app's stability_run.StabilityRunUI: duration / disk estimate,
the pre-flight configuration rows, NumSequences/IntervalTime written into the config header
for script_v7.sh's own repeat loop (restored to 1/0 when the run ends or is stopped), one
manual Run DAQ launch, and the Slack start/finish notifications. Qt-free; listeners are
called from worker threads."""
import csv
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta

from core import env

RATE_HZ = 1000.0                          # events/s, set by the laser trigger rate
OVERHEAD_S = 25.0                         # per acquisition outside the data taking
BYTES_PER_EVENT_SAMPLE = 3078.7 / 1024.0  # RAW size measured on run 20260817_000
DEFAULT_TIME_WINDOW = 1024
LASER_PREP_S = 20.0                       # TEC/LD switch-on and settle per wavelength block


def _log(msg):
    from core import applog
    applog.log(msg)


def fmt(seconds):
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def fmt_size(nbytes):
    gb = nbytes / (1024 ** 3)
    if gb >= 1024:
        return f"{gb / 1024:.2f} TB"
    if gb >= 1:
        return f"{gb:.2f} GB"
    return f"{nbytes / (1024 ** 2):.0f} MB"


def nums(events, interval, count):
    """(events, rate, interval, count) or None when a field is not usable."""
    try:
        ev, iv, cnt = float(events), float(interval), int(float(count))
    except (ValueError, TypeError):
        return None
    if ev <= 0 or iv < 0 or cnt <= 0:
        return None
    return ev, RATE_HZ, iv, cnt


def per_acquisition(ev):
    return ev / RATE_HZ + OVERHEAD_S


def total_seconds(ev, iv, cnt):
    """N acquisitions have N-1 gaps."""
    return cnt * per_acquisition(ev) + max(0, cnt - 1) * iv


def raw_dir(cfg=None):
    """Where RAW files land, for the free-space check (BasePath/Data/RAW/Laser, as the Tk app)."""
    cfg = env.read_config() if cfg is None else cfg
    base = cfg.get("BasePath") or cfg.get("DaqProgramPath") or os.path.expanduser("~/ADC/ADC_test")
    d = os.path.join(base, "Data", "RAW", "Laser")
    return d if os.path.isdir(d) else os.path.expanduser("~")


def time_window(text):
    try:
        return float(text)
    except (ValueError, TypeError):
        return DEFAULT_TIME_WINDOW


def disk(ev, cnt, window, path=None):
    """-> (per_run_bytes, needed_bytes, free_bytes or None)."""
    per_run = ev * window * BYTES_PER_EVENT_SAMPLE
    try:
        st = os.statvfs(path or raw_dir())
        free = st.f_bavail * st.f_frsize
    except OSError:
        free = None
    return per_run, per_run * cnt, free


def laser_sequence():
    """[(wl, bias, pulse)] checked in the General Scan Laser Sequence panel, 405 -> 375 -> 450 -> 473;
    None when a checked row has a non-numeric current."""
    from core.scan import SEQ_ORDER
    try:
        with open(os.path.join(env.DAQ_DIR, "laser_sequence.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
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


def _sequence_path():
    return os.path.join(env.DAQ_DIR, "stability_sequence.json")


def load_sequence():
    """Per-wavelength Stability settings {wl: {on, bias, pulse, len, unit, iv}} from the last session."""
    try:
        with open(_sequence_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_sequence(data):
    with open(_sequence_path(), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def laser_panel_values():
    """{wl: (bias, pulse)} as typed in the General Scan Laser Sequence panel, checked or not."""
    try:
        with open(os.path.join(env.DAQ_DIR, "laser_sequence.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return {wl: (str(v.get("bias", "")), str(v.get("pulse", ""))) for wl, v in data.items() if isinstance(v, dict)}


def block_count(length, unit, ev, iv):
    """Acquisitions of one wavelength block: a count, or how many fit in `length` hours."""
    acq = per_acquisition(ev)
    return int((length * 3600 + iv) // (acq + iv)) if unit == "h" else int(length)


def plan_total(plan, ev):
    """Seconds for [(block, count, interval)]; block None = the current laser, no switching."""
    multi = bool(plan) and plan[0][0] is not None
    return sum(total_seconds(ev, iv, cnt) for _b, cnt, iv in plan) + (LASER_PREP_S * len(plan) if multi else 0.0)


def run_ranges(since):
    """'405 nm: runs 800-808 (9)' per wavelength from the registry rows registered after `since`."""
    try:
        from core.live_scan import REGISTRY
        runs = {}
        with open(REGISTRY, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("registered_at", "") >= since.strftime("%Y-%m-%dT%H:%M:%S") \
                        and r.get("kind") in ("stability", "manual"):
                    runs.setdefault(r.get("wavelength", "?"), []).append(int(r["run"]))
        return [f"{wl} nm: runs {min(v):03d}-{max(v):03d} ({len(v)})" for wl, v in runs.items()]
    except Exception:
        return []


def fit_count(hours, ev, iv):
    """Acquisitions that fit in `hours` (inverts total = cnt*acq + (cnt-1)*iv)."""
    return max(0, int((hours * 3600 + iv) // (per_acquisition(ev) + iv)))


def preflight_rows(cfg):
    """(rows, warnings) of what the header will stamp into RunInfo, as ConfigManager.preflight_rows."""
    def g(key, default="N/A"):
        return str(cfg.get(key, default)).strip() or default

    rows = [
        ("PMT1 (ch0):", "%s   dir %s   HV %s V" % (g('SN1'), g('direction1'), g('HV1'))),
        ("PMT2 (ch1):", "%s   dir %s   HV %s V" % (g('SN2'), g('direction2'), g('HV2'))),
        ("PMT3 (ch2):", "%s   dir %s   HV %s V" % (g('SN3'), g('direction3'), g('HV3'))),
        ("Laser:", "%s nm   %s mA" % (g('Wavelength'), g('Laser'))),
        ("DAQ:", "%s events   window %s   post-trig %s   chmask %s   trig ch %s"
                 % (g('Events'), g('TimeWindow'), g('PostTrigger'), g('ChannelMask'), g('TriggerCh'))),
        ("Repeat:", "NumSequences %s   IntervalTime %s s" % (g('NumSequences'), g('IntervalTime'))),
        ("People:", "shifter %s   /   expert %s" % (g('Shift_worker'), g('Expert'))),
        ("NOTE:", g('NOTE', '(empty)')),
    ]
    warnings = []
    laser = g('Laser', '')
    if laser in ("", "0", "0.0", "N/A"):
        warnings.append('Laser current is "%s" -- every run will record 0 mA.' % (laser or 'empty'))
    for n in (1, 2, 3):
        hv = g('HV%d' % n, '')
        if hv in ("", "0", "N/A"):
            warnings.append('HV%d is "%s".' % (n, hv or 'empty'))
    if g('Wavelength', '') in ("", "0", "N/A"):
        warnings.append('Wavelength is not set.')
    return rows, warnings


def check_hv(cfg):
    """(rows, blockers, warnings) from the Tk app's hv_preflight (Tk-free)."""
    from hv_preflight import check_hv as _check
    return _check(cfg)


def write_repeat_config(path, num_sequences, interval_time):
    """NumSequences / IntervalTime in the header (atomic)."""
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    new = re.sub(r"const int NumSequences\s*=\s*\d+\s*;", f"const int NumSequences = {int(num_sequences)};", content)
    new = re.sub(r"const int IntervalTime\s*=\s*\d+\s*;", f"const int IntervalTime = {int(interval_time)};", new)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(new)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def hk_backend():
    """True when the armed DAQ backend is the HK Digitizer (Tk app's file or the v2 scan engine)."""
    import sys
    from core import status
    if status.daq_backend() == "HK Digitizer":
        return True
    host = getattr(sys.modules.get("core.scan"), "_host", None)
    return getattr(getattr(host, "auto_mgr", None), "daq_backend", "caen") == "hk"


def build_command(manual_type, file_format, angles):
    """The manual Run DAQ launch (script_v7.sh <mode> <config> R2 T2 R3 T3 <700|800>) and its env."""
    from core import scripts
    return scripts.daq("manual", manual_type, file_format, angles)


def _notify(title, body, level):
    try:
        from core import applog
        n = applog.notifier()
        if n.enabled:
            n.send(title, body, level=level, dedupe_key=None, blocking=False)
    except Exception as e:
        _log(f"[WARNING] Stability Run notification failed: {type(e).__name__}.")


class StabilityRun:
    """One Stability Run at a time; survives page rebuilds."""

    def __init__(self):
        self.running = False
        self.slot = None
        self.plan, self.idx, self.skipped = [], 0, []
        self.status = ("Idle.", "idle")     # (text, state) state in idle/running
        self.started_at = None
        self.config_path = None
        self.listeners = []                 # fn() after a state change
        self._token = 0
        self._lock = threading.Lock()

    def _changed(self):
        for fn in list(self.listeners):
            try:
                fn()
            except RuntimeError:
                self.listeners.remove(fn)
            except Exception:
                pass

    def start(self, config_path, ev, plan, manual_type, file_format, angles):
        """plan = [(block, count, interval)], block = (wl, bias, pulse) or None for the current laser.
        One wavelength block after another, each its own script_v7.sh repeat launch. -> (ok, message)."""
        total = plan_total(plan, ev)
        end = (datetime.now() + timedelta(seconds=total)).strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            if self.running:
                return False, "A Stability Run is already running."
            self.running = True
            self.config_path = config_path
            self._token += 1
            token = self._token
        self.plan, self.idx, self.skipped, self.last_code = list(plan), 0, [], None
        self.manual_type, self.file_format, self.angles = manual_type, file_format, angles
        self.started_at = datetime.now()
        multi = plan[0][0] is not None
        n_acq = sum(c for _b, c, _i in plan)
        self.status = (f"Running - {n_acq} acquisitions" + (f", {len(plan)} wavelengths" if multi else "")
                       + f", est. finish {end}", "running")
        _log("[INFO] Stability Run started: " + "; ".join(
            f"{b[0] if b else 'current laser'} NumSequences={c} IntervalTime={i:.0f}s" for b, c, i in plan)
            + f", est. finish {end}")
        _notify("Stability Run started",
                "".join(f"{b[0] if b else 'Current laser'}: {c} acquisitions, {int(i)} s apart\n" for b, c, i in plan)
                + f"{int(ev):,} events each. Est. finish: {end}", "info")
        a = {d: angles.get(d, (None, None)) for d in (2, 3)}
        r2, t2, r3, t3 = (int(round(v)) if v is not None else 0 for v in (a[2][1], a[2][0], a[3][1], a[3][0]))
        _log(f"[INFO] Manual DAQ using live angles -> R2:{r2}, T2:{t2}, R3:{r3}, T3:{t3}")
        self._changed()
        threading.Thread(target=self._run_block, args=(token,), daemon=True).start()
        return True, ""

    # ---- wavelength blocks (worker threads) -----------------------------------
    def _alive(self, token):
        return self.running and token == self._token

    def _sleep(self, seconds):
        end = time.time() + seconds
        while self.running and time.time() < end:
            time.sleep(0.5)

    def _engine(self):
        """The General Scan engine (AutomationManager on the v2 scan host): its laser block helpers."""
        import sys
        h = getattr(sys.modules.get("core.scan"), "_host", None)
        if h is None or getattr(h, "laser_mgr", None) is None:
            return None
        return getattr(h, "auto_mgr", None)

    def _run_block(self, token):
        from core.jobs import JOBS
        if not self._alive(token):
            return
        block, cnt, iv = self.plan[self.idx]
        n = len(self.plan)
        if block is not None:
            wl = block[0]
            self.status = (f"Block {self.idx + 1}/{n}: preparing {wl} (TEC/LD ON)...", "running")
            self._changed()
            _log(f"[INFO] Stability Run block {self.idx + 1}/{n}: {wl}")
            am = self._engine()
            try:
                ok = bool(am and am._prepare_laser_block(block, sleep=self._sleep))
            except Exception as e:
                _log(f"[ERROR] Stability Run: laser {wl} preparation failed: {e}")
                ok = False
            if am is None:
                _log("[ERROR] Stability Run: laser control is not available (no scan engine or hardware).")
            if not self._alive(token):
                return
            if not ok:
                self.skipped.append(wl)
                _log(f"[WARNING] Stability Run: {wl} skipped (laser not ready).")
                self._advance(token)
                return
        try:
            write_repeat_config(self.config_path, cnt, iv)
        except Exception as e:
            self.finish(f"Stopped: could not write config3.h ({e}).", 1, token=token)
            return
        if block is not None:
            from core.quick_setup import apply_laser_config
            apply_laser_config(self.config_path, block[0], block[2], block[1])
            self.status = (f"Block {self.idx + 1}/{n}: {block[0]}, {cnt} acquisitions, {int(iv)} s apart", "running")
        elif self.manual_type != "dark":
            from core.quick_setup import sync_config_from_active_laser
            sync_config_from_active_laser(self.config_path)
        try:
            command, run_env = build_command(self.manual_type, self.file_format, self.angles)
        except Exception as e:
            self.finish(f"Stopped: {e}", 1, token=token)
            return
        slot, why = JOBS.start("daq", "DAQ", command, env=run_env,
                               on_done=lambda code: self._block_done(code, token))
        if slot is None:
            self.finish(f"Stopped: {why}", 1, token=token)
            return
        self.slot = slot
        self._changed()

    def _block_done(self, code, token):
        if not self._alive(token):
            return
        self.last_code = code
        if code not in (None, 0):
            self.finish("Finished.", code, token=token)
            return
        block = self.plan[self.idx][0]
        if block is not None:
            self._laser_off(block[0])
        self._advance(token)

    def _laser_off(self, wl):
        am = self._engine()
        try:
            if am is not None:
                am._finish_laser_block(wl)
        except Exception as e:
            _log(f"[WARNING] Stability Run: {wl} LD OFF failed: {e}")

    def _advance(self, token):
        self.idx += 1
        if self.idx < len(self.plan):
            threading.Thread(target=self._run_block, args=(token,), daemon=True).start()
        else:
            self.finish("Finished.", self.last_code, token=token)

    def stop(self):
        from core.jobs import JOBS
        if not self.running:
            return
        if self.slot:
            try:
                JOBS.stop(self.slot)
            except Exception as e:
                _log(f"[WARNING] Stability Run stop: {e}")
        block = self.plan[self.idx][0] if self.plan and self.idx < len(self.plan) else None
        self.finish("Stopped by operator.", None, stopped=True)
        if block is not None:
            threading.Thread(target=self._laser_off, args=(block[0],), daemon=True).start()

    def finish(self, msg, exit_code=None, stopped=False, token=None):
        with self._lock:
            if not self.running or (token is not None and token != self._token):
                return
            self.running = False
        try:
            write_repeat_config(self.config_path, 1, 0)
            _log("[INFO] Stability Run: NumSequences/IntervalTime restored to 1/0.")
        except Exception as e:
            _log(f"[ERROR] Stability Run: could not restore config3.h: {e}")
        ranges = run_ranges(self.started_at) if getattr(self, "plan", None) and self.plan[0][0] is not None else []
        if getattr(self, "skipped", None):
            msg += f" Skipped (laser not ready): {', '.join(self.skipped)}."
        self.status = (msg, "idle")
        _log(f"[INFO] Stability Run: {msg}")
        for line in ranges:
            _log(f"[INFO] Stability Run {line}")
        dur = ""
        if self.started_at is not None:
            dur = f"Duration: {(datetime.now() - self.started_at).total_seconds() / 60.0:.0f} min\n"
        dur += "".join(f"{line}\n" for line in ranges)
        if stopped:
            _notify("Stability Run stopped", f"{dur}Stopped by operator.", "info")
        elif exit_code is not None and exit_code != 0:
            _notify("Stability Run finished: FAILED",
                    f"{dur}Exit code: {exit_code}\nCheck the Console tab / TakingLog for details.", "warning")
        else:
            _notify("Stability Run finished", dur.strip() or "Done.", "info")
        self._changed()


RUN = StabilityRun()

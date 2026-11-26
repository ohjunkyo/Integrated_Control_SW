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

from core import env, next_daq

RATE_HZ = 1000.0                          # events/s when the function generator cannot be read
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


_rate = {"hz": None, "src": "", "t": 0.0, "busy": False}
RATE_MAX_AGE_S = 60.0


def _read_rate():
    from core import function_generator as FG
    try:
        hz, src = FG.trigger_hz(), "function generator"
    except Exception:
        hz, src = None, ""
    if hz is None:
        hz, src = FG.logged_trigger_hz(), "FG log"
    _rate.update(hz=hz, src=src if hz else "", t=time.time(), busy=False)


def rate_hz():
    """DAQ trigger rate for the estimates: the function generator's setting, else RATE_HZ. Never blocks."""
    if time.time() - _rate["t"] > RATE_MAX_AGE_S and not _rate["busy"]:
        _rate["busy"] = True
        threading.Thread(target=_read_rate, daemon=True).start()
    return _rate["hz"] or RATE_HZ


def rate_source():
    return _rate["src"] or "assumed"


def nums(events, interval, count):
    """(events, rate, interval, count) or None when a field is not usable."""
    try:
        ev, iv, cnt = float(events), float(interval), int(float(count))
    except (ValueError, TypeError):
        return None
    if ev <= 0 or iv < 0 or cnt <= 0:
        return None
    return ev, rate_hz(), iv, cnt


def per_acquisition(ev, hz=None):
    return ev / (hz or rate_hz()) + OVERHEAD_S


def total_seconds(ev, iv, cnt, hz=None):
    """N acquisitions have N-1 gaps."""
    return cnt * per_acquisition(ev, hz) + max(0, cnt - 1) * iv


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


def block_count(length, unit, ev, iv, hz=None):
    """Acquisitions of one wavelength block: a count, or how many fit in `length` hours."""
    acq = per_acquisition(ev, hz)
    return int((length * 3600 + iv) // (acq + iv)) if unit == "h" else int(length)


def plan_total(plan, ev, hz=None):
    """Seconds for [(block, count, interval)]; block None = the current laser, no switching."""
    multi = bool(plan) and plan[0][0] is not None
    return sum(total_seconds(ev, iv, cnt, hz) for _b, cnt, iv in plan) + (LASER_PREP_S * len(plan) if multi else 0.0)


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


def build_command(manual_type, file_format, angles, base=None):
    """The manual Run DAQ launch (script_v7.sh <mode> <config> R2 T2 R3 T3 <base>) and its env."""
    from core import scripts
    return scripts.daq("manual", manual_type, file_format, angles, base=base)


def _notify(title, body, level, key=None):
    try:
        from core import applog
        n = applog.notifier()
        if n.enabled:
            n.send(title, body, level=level, dedupe_key=None, blocking=False, key=key)
    except Exception as e:
        _log(f"[WARNING] Stability Run notification failed: {type(e).__name__}.")


class StabilityRun:
    """One Stability Run at a time; survives page rebuilds."""

    def __init__(self):
        self._running = False
        self.slot = None
        self.plan, self.idx, self.skipped = [], 0, []
        self.status = ("Idle.", "idle")     # (text, state) state in idle/running
        self.started_at = None
        self.config_path = None
        self.listeners = []                 # fn() after a state change
        self.extra_env = {}                 # DAQ_* overrides for script_v7.sh (HV scan)
        self.base = None                    # script_v7.sh run base; None = the manual laser block (800)
        self._token = 0
        self._lock = threading.Lock()

    @property
    def running(self):
        return self._running

    @running.setter
    def running(self, on):
        if bool(on) != self._running:   # hold marker for the nightly sync and the remote reboot check
            import run_markers
            run_markers.set_stability(bool(on))
        self._running = bool(on)

    def _changed(self):
        for fn in list(self.listeners):
            try:
                fn()
            except RuntimeError:
                self.listeners.remove(fn)
            except Exception:
                pass

    def start(self, config_path, ev, plan, manual_type, file_format, angles, extra_env=None, base=None):
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
        self.extra_env = dict(extra_env or {})
        self.base = base
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
                + f"{int(ev):,} events each. Est. finish: {end}", "info", "stability_started")
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
        next_daq.wait_until(end)
        while self.running and time.time() < end:
            time.sleep(0.5)
        next_daq.clear()

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
            command, run_env = build_command(self.manual_type, self.file_format, self.angles, self.base)
        except Exception as e:
            self.finish(f"Stopped: {e}", 1, token=token)
            return
        from core import hv_label
        run_env.update(hv_label.run_env())
        run_env.update(self.extra_env)
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
        if self.config_path:
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
            _notify("Stability Run stopped", f"{dur}Stopped by operator.", "info", "stability_stopped")
        elif exit_code is not None and exit_code != 0:
            _notify("Stability Run finished: FAILED",
                    f"{dur}Exit code: {exit_code}\nCheck the Console tab / TakingLog for details.", "warning", "stability_failed")
        else:
            _notify("Stability Run finished", dur.strip() or "Done.", "info", "stability_finished")
        self._changed()


RUN = StabilityRun()


# ---- Dark mode: self-trigger one PMT at a time, plus an external-trigger run with every LD off ----
DARK_RATE_HZ = 12000.0                    # assumed per-PMT dark rate for the self-trigger time estimate
DARK_BASE = "600"                         # script_v7.sh run base of every Dark Stability Run (self + external)
DARK_KIND = "dark"                        # registry kind of those runs
DARK_ROWS = (("mon", "Monitor (ch0)", "Self", 0), ("rot1", "Rot1 (ch1)", "Self", 1),
             ("rot2", "Rot2 (ch2)", "Self", 2), ("ext", "Reference: all PMTs", "External", None))
DARK_DEFAULTS = {"mon": {"on": True, "thr": "1.5", "win": "256", "pt": "24", "ev": "100000", "iv": "5"},
                 "rot1": {"on": True, "thr": "1.5", "win": "256", "pt": "24", "ev": "100000", "iv": "5"},
                 "rot2": {"on": True, "thr": "1.5", "win": "256", "pt": "24", "ev": "100000", "iv": "5"},
                 "ext": {"on": True, "thr": "1.5", "win": "1024", "pt": "60", "ev": "300000", "iv": "5"}}
DARK_ENV_KEYS = ("DAQ_EVENTS", "DAQ_TIME_WINDOW", "DAQ_POST_TRIGGER", "DAQ_DARK_MASK", "DAQ_DARK_THR_MV",
                 "DAQ_ANALYSIS_THR_MV")


def _dark_path():
    return os.path.join(env.DAQ_DIR, "dark_sequence.json")


def _dark_presets_path():
    return os.path.join(env.DAQ_DIR, "dark_presets.json")


def load_dark():
    """{'rows': {key: {on, thr, win, ev, iv}}, 'interval', 'count'} from the last session, defaults filled in."""
    try:
        with open(_dark_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    rows = data.get("rows", {})
    data["rows"] = {k: {**DARK_DEFAULTS[k], **rows.get(k, {})} for k, *_ in DARK_ROWS}
    return data


def save_dark(data):
    with open(_dark_path(), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def dark_presets():
    try:
        with open(_dark_presets_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_dark_preset(name, data):
    p = dark_presets()
    p[name] = data
    with open(_dark_presets_path(), "w", encoding="utf-8") as f:
        json.dump(p, f, indent=2)


def dark_backend_ready():
    """True once script_v7.sh takes the per-run overrides (DAQ_DARK_MASK etc.) from the environment."""
    try:
        with open(os.path.join(os.path.expanduser("~/ADC/ADC_test"), "script_v7.sh"), encoding="utf-8") as f:
            return "DAQ_DARK_MASK" in f.read()
    except OSError:
        return False


def dark_steps(rows):
    """One cycle: [(key, label, trigger, ch, thr, win, ev, iv, pt)] of the ticked rows in table order;
    None when a ticked row has a field that is not a positive number."""
    steps = []
    for key, label, trig, ch in DARK_ROWS:
        r = rows.get(key, {})
        if not r.get("on"):
            continue
        try:
            thr, win, ev, iv = float(r["thr"]), int(float(r["win"])), int(float(r["ev"])), float(r["iv"])
            pt = int(float(r["pt"]))
        except (KeyError, ValueError, TypeError):
            return None
        if thr <= 0 or win <= 0 or ev <= 0 or iv < 0 or not 0 < pt < 100:
            return None
        steps.append((key, label, trig, ch, thr, win, ev, iv, pt))
    return steps


def dark_step_seconds(step):
    trig, ev = step[2], step[6]
    return ev / (DARK_RATE_HZ if trig == "Self" else rate_hz()) + OVERHEAD_S


def dark_step_bytes(step):
    """RAW size: the self-trigger run records one channel, the external run four."""
    trig, win, ev = step[2], step[5], step[6]
    return ev * win * BYTES_PER_EVENT_SAMPLE * (0.25 if trig == "Self" else 1.0)


def dark_total(steps, cycles, period):
    """Seconds for `cycles` repeats; a cycle starts every `period` s (start to start), or right after
    the previous one when it takes longer. Each step waits its own interval after it."""
    one = sum(dark_step_seconds(s) + s[7] for s in steps) - (steps[-1][7] if steps else 0)
    return max(0, cycles - 1) * max(period, one + (steps[-1][7] if steps else 0)) + one


def dark_env(step):
    """Per-run overrides for script_v7.sh (it keeps the config3.h values for anything not set)."""
    _k, _l, trig, ch, thr, win, ev, _iv, pt = step
    e = {"DAQ_EVENTS": str(ev), "DAQ_TIME_WINDOW": str(win), "DAQ_POST_TRIGGER": str(pt),
         "DAQ_ANALYSIS_THR_MV": f"{thr:g}"}
    if trig == "Self":
        e["DAQ_DARK_MASK"] = "".join("1" if c == ch else "0" for c in range(8))[::-1]
        e["DAQ_DARK_THR_MV"] = f"{thr:g}"
    return e


class DarkRun:
    """Dark Stability Run: cycles of self-trigger runs (one PMT each) and an external-trigger run, LDs off."""

    def __init__(self, owner):
        self.owner = owner

    def start(self, steps, cycles, cycle_iv, file_format, angles):
        run = self.owner
        with run._lock:
            if run.running:
                return False, "A Stability Run is already running."
            run.running = True
            run._token += 1
            token = run._token
        run.plan, run.idx, run.skipped, run.last_code = [], 0, [], None
        run.config_path = None
        run.started_at = datetime.now()
        self.steps, self.cycles, self.cycle_iv = steps, cycles, cycle_iv
        self.file_format, self.angles = file_format, angles
        self.cycle, self.pos = 0, 0
        self.cycle_t0 = time.time()
        total = dark_total(steps, cycles, cycle_iv)
        end = (datetime.now() + timedelta(seconds=total)).strftime("%Y-%m-%d %H:%M:%S")
        run.status = (f"Running - dark, {cycles} cycles x {len(steps)} runs, est. finish {end}", "running")
        _log("[INFO] Dark Stability Run started: " + ", ".join(f"{s[1]} {s[2]} {s[4]:g} mV {s[5]} smp {s[6]} ev"
                                                               for s in steps) + f"; {cycles} cycles, est. finish {end}")
        _notify("Dark Stability Run started", "".join(f"{s[1]}: {s[2]} trigger, {s[4]:g} mV, {s[6]:,} events\n"
                                                      for s in steps) + f"{cycles} cycles. Est. finish: {end}", "info", "dark_started")
        run._changed()
        threading.Thread(target=self._next, args=(token,), daemon=True).start()
        return True, ""

    def _lasers_on(self):
        from core import quick_setup
        ctl = quick_setup.laser_controller()
        return ctl.lasers_on() if ctl is not None else []

    def _next(self, token):
        from core import scripts
        from core.jobs import JOBS
        run = self.owner
        if not run._alive(token):
            return
        on = self._lasers_on()
        if on:
            _log(f"[CRITICAL] Dark Stability Run stopped: laser ON ({', '.join(on)}).")
            _notify("Dark Stability Run stopped", f"Laser ON during a dark run: {', '.join(on)}", "critical", "dark_laser_on")
            run.finish(f"Stopped: laser ON ({', '.join(on)}).", 1, token=token)
            return
        step = self.steps[self.pos]
        mode = "dark" if step[2] == "Self" else "laser"
        try:
            command, run_env = scripts.daq("manual", mode, self.file_format, self.angles, base=DARK_BASE)
        except Exception as e:
            run.finish(f"Stopped: {e}", 1, token=token)
            return
        run_env.update(dark_env(step))
        from core import hv_label
        run_env.update(hv_label.run_env())
        run_env["DAQ_RUN_KIND"] = DARK_KIND
        run.status = (f"Cycle {self.cycle + 1}/{self.cycles}: {step[1]} ({step[2]} trigger)", "running")
        run._changed()
        slot, why = JOBS.start("daq", "DAQ", command, env=run_env, on_done=lambda code: self._done(code, token))
        if slot is None:
            run.finish(f"Stopped: {why}", 1, token=token)
            return
        run.slot = slot
        run._changed()

    def _done(self, code, token):
        run = self.owner
        if not run._alive(token):
            return
        run.last_code = code
        if code not in (None, 0):
            run.finish("Finished.", code, token=token)
            return
        wait = self.steps[self.pos][7]
        self.pos += 1
        if self.pos >= len(self.steps):
            self.pos, self.cycle = 0, self.cycle + 1
            if self.cycle >= self.cycles:
                run.finish("Finished.", code, token=token)
                return
            wait = max(wait, self.cycle_iv - (time.time() - self.cycle_t0))

        def later():
            run._sleep(wait)
            if self.pos == 0:
                self.cycle_t0 = time.time()
            self._next(token)
        threading.Thread(target=later, daemon=True).start()


RUN.dark = DarkRun(RUN)


# ---- self-trigger rate per run: triggers / live time (time-tag span minus readout stalls) of the RAW file ----
_RATE_CACHE = {}
_DETAIL_CACHE = {}
TTT_TICK_S = 8e-9
STALL_GAP_S = 0.5e-3          # gaps this long may hold USB readout pauses
CORR_S = 20e-6                # shorter intervals are followers of the previous pulse
FIT_S = (150e-6, 500e-6)      # interval range of the exponential fit (uncorrelated pulses)
MIN_FIT = 200
_HDD_DARK = "/media/precalkor/HD-EDS-E/Data_Backup/RAW/Dark"


def _raw_dark_path(name):
    cfg = env.read_config()
    base = cfg.get("BasePath") or cfg.get("DaqProgramPath") or os.path.expanduser("~/ADC/ADC_test")
    for d in (os.path.join(base, "Data", "RAW", "Dark"), _HDD_DARK):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return None


def _exp_slope(x, t0, t1):
    """(rate, error) of a truncated exponential fitted to intervals x in [t0, t1) by maximum likelihood, or None."""
    import numpy as np
    n, T = len(x), t1 - t0
    s = float((x - t0).sum())

    def g(lam):
        return n / lam - s - n * T / np.expm1(lam * T)
    lo, hi = 1e-3 / T, 500 / T   # exp(500) still fits a double
    if g(lo) <= 0 or g(hi) >= 0:
        return None
    for _ in range(80):
        mid = (lo * hi) ** 0.5
        lo, hi = (mid, hi) if g(mid) > 0 else (lo, mid)
    lam = (lo * hi) ** 0.5
    info = n * (1 / lam ** 2 - T ** 2 * np.exp(lam * T) / np.expm1(lam * T) ** 2)
    return lam, (float(info ** -0.5) if info > 0 else None)


def _run_rate_detail(path):
    """{ch, raw, live, err, dead, events, uncorr, uncorr_err, corr} of one self-trigger RAW file (rates in Hz,
    dead and corr as fractions); cached by mtime. Dead time is the time in gaps > STALL_GAP_S beyond what the
    exponential of the uncorrelated pulses predicts; corr is the fraction of intervals < CORR_S."""
    key = (path, os.path.getmtime(path))
    if key in _DETAIL_CACHE:
        return _DETAIL_CACHE[key]
    import numpy as np
    import uproot
    with uproot.open(path) as f:
        mask = str(f["RunInfo"]["ChannelMask"].array(library="np")[0])
        tt = f["T"]["TriggerTimeTag"].array(library="np").astype(np.int64)
    pmts = [7 - i for i, c in enumerate(mask[-8:].rjust(8, "0")) if c == "1" and 7 - i != 3]
    d = np.diff(tt)
    d[d < 0] += 2 ** 31
    d = d * TTT_TICK_S
    span = float(d.sum())
    if span <= 0 or len(pmts) != 1:
        return None
    x = d[(d >= FIT_S[0]) & (d < FIT_S[1])]
    fit = _exp_slope(x, *FIT_S) if len(x) >= MIN_FIT else None
    dead = 0.0
    if fit:
        lam = fit[0]
        gap = float(d[d >= STALL_GAP_S].sum())
        expect_n = int((d >= FIT_S[0]).sum()) * np.exp(-lam * (STALL_GAP_S - FIT_S[0]))
        dead = min(max(gap - expect_n * (STALL_GAP_S + 1 / lam), 0.0), gap)
    live = len(d) / (span - dead)
    out = {"ch": pmts[0], "raw": len(d) / span, "live": live, "err": float(live / np.sqrt(len(d))),
           "dead": dead / span, "events": len(tt), "uncorr": fit[0] if fit else None,
           "uncorr_err": fit[1] if fit else None, "corr": float((d < CORR_S).mean())}
    _DETAIL_CACHE[key] = out
    return out


def _run_rate(path):
    """(channel, live-time rate_hz, err_hz, events) of one self-trigger RAW file; cached by mtime."""
    key = (path, os.path.getmtime(path))
    if key in _RATE_CACHE:
        return _RATE_CACHE[key]
    v = _run_rate_detail(path)
    if not v:
        return None
    out = (v["ch"], v["live"], v["err"], v["events"])
    _RATE_CACHE[key] = out
    return out


_CLEAN_CACHE = {}


def _prd_clean_rate(name, ch):
    """DarkRateClean_ch<N> (records with a real pulse at the trigger / live time) from the prd file, or None."""
    base = env.read_config().get("BasePath") or os.path.expanduser("~/ADC/ADC_test")
    p = os.path.join(base, "Data", "production", name.replace("precal_raw_", "precal_prd_"))
    try:
        key = (p, ch, os.path.getmtime(p))
    except OSError:
        return None
    if key in _CLEAN_CACHE:
        return _CLEAN_CACHE[key]
    try:
        import uproot
        with uproot.open(p) as f:
            v = float(f[f"DarkRateClean_ch{ch}"].member("fVal"))
    except Exception:
        return None
    _CLEAN_CACHE[key] = v
    return v


def self_trigger_rates():
    """Self-trigger runs (dark mode, one PMT each) of the newest date that has them, oldest first:
    [{time, run, ch, sn, rate (live time), raw, dead, err, events, file}]."""
    try:
        from core.live_scan import REGISTRY
        rows = [r for r in csv.DictReader(open(REGISTRY, encoding="utf-8"))
                if r.get("mode") == "dark" and r.get("status", "").startswith("done")]
    except OSError:
        return []
    if not rows:
        return []
    last = max(r["date"] for r in rows)
    out = []
    for r in rows:
        if r["date"] != last:
            continue
        path = _raw_dark_path(r.get("file", ""))
        if not path:
            continue
        try:
            v = _run_rate_detail(path)
        except Exception:
            v = None
        if not v:
            continue
        ch = v["ch"]
        out.append({"time": datetime.fromisoformat(r["registered_at"]), "run": r["run"], "ch": ch,
                    "sn": r.get(f"sn{ch + 1}", f"ch{ch}"), "rate": v["live"], "raw": v["raw"],
                    "dead": v["dead"], "err": v["err"], "events": v["events"],
                    "clean": _prd_clean_rate(r.get("file", ""), ch), "file": r.get("file", "")})
    out.sort(key=lambda x: x["time"])
    return out

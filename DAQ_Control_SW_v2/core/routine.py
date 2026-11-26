"""Daily Routine: an ordered list of steps (General Scan, dark rate, HV scan, HV apply) started at
JST clock times or right after the previous step, kept as JSON presets in DAQ_Control_SW/routines/
(the current one in _current.json) and run by one daemon thread as core/schedule.py does.
Qt-free; listeners are called from the runner thread."""
import copy
import glob
import json
import math
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import types
from datetime import datetime, timedelta

from core import env, next_daq
from core.run_info import SCAN_BLOCK
from core.schedule import JST

KINDS = ("general_scan", "hk_scan", "wavelength_scan", "dark_rate", "stability_qe", "hv_scan", "hv_apply")
STATUS = ("Pending", "Running", "Done", "Failed", "Skipped", "Disabled")
SEQUENTIAL = "sequential"
DEFAULT_SLOTS = ["23:00", "03:30", "04:30", "09:00"]   # shared beam-time slots (page timeline)
ROUTINES_DIR = os.path.join(env.DAQ_DIR, "routines")
CURRENT = "_current.json"
TICK_S = 5.0
POLL_S = 1.0                  # executors poll the engine / stop flag at this rate
START_WAIT_S = 60.0           # the General Scan engine must report running within this after start()
LASER_OFF_WAIT_S = 20.0
COLLISION_GUARD_S = 600.0     # a Schedule Manager run due this soon after a step's estimated end still collides
TS = "%Y-%m-%d %H:%M:%S"
HVSCAN_DIR = os.path.join(env.ADC_DIR, "Data", "HVScan")   # <date>/hvscan_<HHMM>.json manifests
HV_LIMITS_FILE = os.path.join(env.ROOT_DIR, "HV_Control_SW", "config_precal.json")   # hv_request_limits
HVSCAN_KIND = "hvscan"        # registry kind of HV scan runs (script_v7.sh DAQ_RUN_KIND)
HVSCAN_BASE = "500"           # script_v7.sh run base of HV scan runs
SCAN_BLOCK_MAX = 450          # last General Scan run block (RotationManager.MAX_SCAN_BLOCK)
SCAN_RUN_LIMIT = 500          # runs from here on are HV scan / dark / manual / stability, not General Scan
DARK_THR_MV = 1.5             # dark_rate threshold when a preset has none
DARK_THR_RANGE = (0.3, 20.0)
EXT_RAW_DIR = "/home/precalkor/external_HDD_1_4T/Data_Backup/RAW"
GAIN_FIT_MACRO = "HVScan_GainCurve.C"
FIT_TIMEOUT_S = 3600.0
FIT_MODES = {0: "Quick", 1: "Rough: check the fit results only", 2: "Detailed (recommended)"}
TILT_TOL_DEG = 0.5            # RotationManager.ARRIVAL_TOLERANCE_DEG
QE_UNTIL_MARGIN_S = 120.0     # a stability run sized to the next step ends this long before it
RESTORE_RETRY_S = 120.0       # a request cancelled by Stop can stay open at the crate this long
FAILED = "failed"             # executor ok value: Failed even on Stop / Skip
RESTORE_LS = "_restore_laser_sequence.json"   # Laser Sequence to put back if the app dies during a wavelength scan
HISTORY = "history"           # routines/history/<start>.json: one snapshot per finished routine run
HISTORY_KEEP = 60
PLACEHOLDER_LIMITS = (0.0, 2300.0, 250.0)     # min, max, step shipped with hv_request_limits
TRIGGER_WARN_HZ = 2300.0      # CAEN 4 ch x 1024 samples readout limit over USB
TRIGGER_TOL = 0.01            # read-back must match the requested rate within 1 %
CAEN_TRIGGER_HZ = 2000.0      # set when CAEN DAQ is selected (HK Digitizer: hk_config trigger_hz)
RECOMMEND_GAP_S = 600.0       # recommended start = previous end + this, rounded up to 5 min

# duration estimate constants, CAEN backend (seconds)
EST = {"caen_point_s": 323.0, "axis_change_s": 189.0, "scan_startup_s": 180.0, "hv_channel_s": 45.0,
       "hv_fit_s": 300.0, "caen_point_overhead_s": 23.0, "hk_point_s": 60.0}   # measured: 323 s/point at 300k events 1 kHz, ~45 s per HV change

DEFAULT_ROUTINE = {
    "name": "default",
    "on_error": "stop",
    "slots": list(DEFAULT_SLOTS),
    "steps": [
        {"enabled": True, "kind": "dark_rate", "name": "Dark Rate", "start": "17:30",
         "options": {"every_min": 15, "until_next_step": True, "cycles": 1, "events": 100000,
                     "channels": ["mon", "rot1", "rot2"], "thr_mv": DARK_THR_MV}},
        {"enabled": False, "kind": "hv_scan", "name": "HV Scan", "start": "20:00",
         "options": {"offsets_v": [-100, -50, 0, 50, 100], "events": 100000, "settle_s": 180,
                     "gain_mantissa": 1.0, "fit_mode": 2, "fit_timeout_s": 3600}},
        {"enabled": True, "kind": "hv_apply", "name": "HV Apply", "start": SEQUENTIAL,
         "options": {"mode": "fixed", "volts": {}, "gain_delay_s": 180, "gain_mantissa": 1.0,
                     "gain_mantissa_ch": {}, "accept_provisional": False, "max_age_h": 24}},
        {"enabled": True, "kind": "general_scan", "name": "General Scan", "start": "23:00",
         "options": {"events": 300000, "points": 46, "axes": 2}},
        {"enabled": False, "kind": "general_scan", "name": "General Scan 2", "start": "04:30",
         "options": {"events": 300000, "points": 46, "axes": 2}},
    ],
}

# options of the kinds that are not in the default routine
KIND_DEFAULTS = {
    "hk_scan": {"points": 46, "axes": 2},   # HK Digitizer readout: timing and run numbers come from hk_config
    "wavelength_scan": {"wavelengths": ["375nm", "473nm"], "events": 300000, "points": 46, "axes": 2},
    "stability_qe": {"wavelengths": ["405nm"], "events": 300000, "cycles": 3, "interval_s": 60},
}

_lock = threading.RLock()
_routine = None
_state = {"active": False, "started_at": None, "idx": None, "stop": False, "skip": False, "result": "idle"}
_worker = None
_runner = None
_waited = {}           # idx -> why the step is held, logged once
listeners = []         # fn() after any state change


def _log(msg):
    from core import applog
    applog.log(msg)


def _now():
    return datetime.now(JST)


def _stamp(dt):
    return dt.strftime(TS) if dt else None


# ---------------------------------------------------------------- data model
def _clock(text):
    """'HH:MM' -> (h, m) or None."""
    try:
        h, m = str(text).strip().split(":")
        h, m = int(h), int(m)
        return (h, m) if 0 <= h < 24 and 0 <= m < 60 else None
    except (TypeError, ValueError):
        return None


def new_step(kind, name="", start=SEQUENTIAL, options=None, enabled=True):
    return normalize_step({"enabled": enabled, "kind": kind, "name": name or kind, "start": start,
                           "options": dict(options or {})})


def normalize_step(step):
    """Fill the fields a step must have; raises ValueError on a bad kind or start time."""
    s = dict(step)
    if s.get("kind") not in KINDS:
        raise ValueError(f"unknown step kind {s.get('kind')!r}")
    start = str(s.get("start", SEQUENTIAL)).strip().lower()
    if start != SEQUENTIAL and _clock(start) is None:
        raise ValueError(f"bad start time {s.get('start')!r} (HH:MM or sequential)")
    s["start"] = start if start == SEQUENTIAL else "%02d:%02d" % _clock(start)
    s["enabled"] = bool(s.get("enabled", True))
    s["name"] = str(s.get("name") or s["kind"])
    s["options"] = dict(s.get("options") or {})
    s["status"] = s.get("status") if s.get("status") in STATUS else ("Pending" if s["enabled"] else "Disabled")
    for k in ("started", "finished", "due"):
        s[k] = s.get(k) or None
    s["note"] = str(s.get("note") or "")
    return s


def normalize(routine):
    r = dict(routine or {})
    r["name"] = str(r.get("name") or "routine")
    r["on_error"] = "stop"
    r["home_stage"] = bool(r.get("home_stage", True))
    slots = [t for t in (DEFAULT_SLOTS if r.get("slots") is None else r["slots"]) if _clock(t)]
    r["slots"] = ["%02d:%02d" % _clock(t) for t in slots]
    r["steps"] = [normalize_step(s) for s in r.get("steps") or []]
    return r


def _definition(routine):
    """The routine without runtime state (what a preset file holds)."""
    out = {k: routine[k] for k in ("name", "on_error", "slots")}
    out["home_stage"] = bool(routine.get("home_stage", True))
    out["steps"] = [{k: s[k] for k in ("enabled", "kind", "name", "start", "options")} for s in routine["steps"]]
    return out


def scan_wavelength(opts):
    """Wavelength of a general_scan step ('' = the Laser Sequence as set)."""
    return str((opts or {}).get("wavelength") or "").strip()


def _engine_order(wls):
    from core.scan import SEQ_ORDER
    wls = list(dict.fromkeys(wls))
    return [w for w in SEQ_ORDER if w in wls] + [w for w in wls if w not in SEQ_ORDER]


def wavelength_keys():
    """Wavelengths of laser_sequence.json in the engine's order; the engine's list when unreadable."""
    from core.scan import SEQ_ORDER
    try:
        with open(_laser_sequence_path(), encoding="utf-8") as f:
            keys = [k for k, v in json.load(f).items() if isinstance(v, dict)]
    except (OSError, ValueError, AttributeError):
        keys = []
    return _engine_order(keys) if keys else list(SEQ_ORDER)


def split_wavelength_steps(steps):
    """Each wavelength_scan step -> one general_scan step per wavelength (engine order, the first keeps the
    start). -> (steps, number of steps split)."""
    out, n = [], 0
    for s in steps:
        opts = dict(s.get("options") or {})
        wls = _engine_order(str(w) for w in opts.pop("wavelengths", None) or [])
        if s.get("kind") != "wavelength_scan" or not wls:
            out.append(s)
            continue
        n += 1
        for j, wl in enumerate(wls):
            out.append(normalize_step({"enabled": s.get("enabled", True), "kind": "general_scan",
                                       "name": f"{s.get('name') or 'Wavelength Scan'} {wl}",
                                       "start": s.get("start", SEQUENTIAL) if j == 0 else SEQUENTIAL,
                                       "options": dict(copy.deepcopy(opts), wavelength=wl)}))
    return out, n


def _split(r, what):
    r["steps"], n = split_wavelength_steps(r["steps"])
    if n:
        _log(f"[INFO] Routine: {what}: {n} Wavelength Scan step(s) split into one General Scan step per "
             f"wavelength ({len(r['steps'])} steps).")
    return n


# ---------------------------------------------------------------- presets and the current routine
def _dir():
    os.makedirs(ROUTINES_DIR, exist_ok=True)
    return ROUTINES_DIR


def _path(name):
    return os.path.join(_dir(), f"{name}.json")


def _write(path, data):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def list_presets():
    try:
        names = [f[:-5] for f in os.listdir(_dir()) if f.endswith(".json") and not f.startswith("_")]
    except OSError:
        return []
    return sorted(names)


def load(name):
    """Make the preset the current routine (its runtime state cleared). Raises while a routine runs."""
    with _lock:
        if _state["active"]:
            raise RuntimeError("a routine is running")
        with open(_path(name), encoding="utf-8") as f:
            r = normalize(json.load(f))
        _split(r, f"preset '{name}'")
        _set_current(r)
    _log(f"[INFO] Routine: preset '{name}' loaded ({len(r['steps'])} steps).")
    return r


def save(name, routine=None):
    """Write the routine (the current one by default) as preset <name>.json."""
    with _lock:
        r = normalize(routine) if routine is not None else current()
        r["name"] = name
        if routine is not None:
            _split(r, f"preset '{name}'")
        _write(_path(name), _definition(dict(r, steps=split_wavelength_steps(r["steps"])[0])))
        if routine is not None and not _state["active"]:
            _set_current(r)
    _log(f"[INFO] Routine: preset '{name}' saved.")
    return r


def new_preset(name):
    """A new preset <name>.json from the built-in default, made current."""
    if os.path.exists(_path(name)):
        raise ValueError(f"preset '{name}' already exists")
    save(name, copy.deepcopy(DEFAULT_ROUTINE))
    return load(name)


def delete_preset(name):
    """Move preset <name>.json to routines/_deleted/ (the default preset cannot be deleted)."""
    if name == "default":
        raise ValueError("the default preset cannot be deleted")
    src = _path(name)
    if not os.path.exists(src):
        raise ValueError(f"preset '{name}' not found")
    dst = os.path.join(_dir(), "_deleted", f"{name}_{datetime.now():%Y%m%d_%H%M%S}.json")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    os.replace(src, dst)
    _log(f"[INFO] Routine: preset '{name}' deleted (moved to {os.path.relpath(dst, _dir())}).")


def reset_default():
    """Rewrite default.json from the built-in default and make it current."""
    _write(_path("default"), DEFAULT_ROUTINE)
    return load("default")


def ensure_default():
    if not os.path.exists(_path("default")):
        _write(_path("default"), DEFAULT_ROUTINE)


def current():
    """The current routine (with runtime state): _current.json after a restart, else the default preset."""
    global _routine
    with _lock:
        if _routine is None:
            ensure_default()
            r = None
            try:
                with open(os.path.join(_dir(), CURRENT), encoding="utf-8") as f:
                    r = normalize(json.load(f))
                for s in r["steps"]:
                    if s["status"] == "Running":
                        s["status"], s["note"] = "Failed", "interrupted by an app restart"
                        hz = (s.get("options") or {}).get("trigger_hz")
                        if hz and s["kind"] in ("general_scan", "wavelength_scan", "hv_scan"):
                            s["note"] += f"; trigger rate not restored (may still be {_fmt_hz(float(hz))})"
                _recover_laser_sequence(r)
                _warn_unrestored(r)
                if all(s["status"] in ("Pending", "Disabled") and not s["started"] for s in r["steps"]):
                    _split(r, f"routine '{r['name']}'")     # a routine with run state keeps its steps
            except (OSError, ValueError) as e:
                if not isinstance(e, FileNotFoundError):
                    _log(f"[WARNING] Routine: {CURRENT} unreadable ({e}); using the default preset.")
            if r is None:
                r = normalize(DEFAULT_ROUTINE)
            _routine = r
            _persist()
        return _routine


def _recover_laser_sequence(r):
    """Put back the Laser Sequence a wavelength scan changed when the app stopped during it."""
    p = os.path.join(_dir(), RESTORE_LS)
    try:
        with open(p, encoding="utf-8") as f:
            saved = json.load(f)
        _write(saved["path"], saved["data"])
        os.remove(p)
    except FileNotFoundError:
        return
    except (OSError, ValueError, KeyError) as e:
        _log(f"[ERROR] Routine: Laser Sequence not restored after the restart: {e}")
        return
    _log("[WARNING] Routine: Laser Sequence restored after an app restart during a wavelength scan.")
    for s in r["steps"]:
        if ((s["kind"] == "wavelength_scan" or scan_wavelength(s["options"]))
                and "interrupted by an app restart" in str(s.get("note"))):
            s["note"] += "; Laser Sequence restored"


def _warn_unrestored(r):
    """After an app restart: HV a routine changed and did not put back is reported; nothing is commanded."""
    scan, apply = r.pop("hv_scan_restore", None), r.pop("hv_restore", None)
    if not scan and not apply:
        return

    def fmt(d):
        try:
            return ", ".join(f"Ch{ch} {float(v):.0f} V" for ch, v in sorted(d.items()))
        except (TypeError, ValueError, AttributeError):
            return str(d)
    lines = []
    if scan:
        lines.append("HV scan base HV not restored, should be: " + fmt(scan))
    if apply:
        lines.append("Header HV not restored after HV apply, should be: " + fmt(apply))
    hit = [s["name"] for s in r["steps"] if "interrupted by an app restart" in str(s.get("note"))]
    lines.append(f"Interrupted step: {', '.join(hit) or 'none (between steps or at the routine end)'}")
    lines.append("Stages: position unverified, the end-of-routine move to the origin did not run")
    _log("[WARNING] Routine: app restarted during a routine; HV may be unrestored. " + "; ".join(lines) + ".")
    _send("Daily Routine: HV may be unrestored after an app restart",
          f"Routine: {r.get('name')}\n" + "\n".join(lines) + "\nNothing was commanded. Check the HV Monitor.",
          "warning", "routine_failed")
    for s in r["steps"]:
        if s["name"] in hit:
            s["note"] += "; HV may be unrestored (see the log)"


def _history_dir():
    return os.path.join(_dir(), HISTORY)


def _save_history(result):
    """Snapshot of the finished run for the Results tab; the oldest beyond HISTORY_KEEP are removed."""
    started = _state.get("started_at") or _now()
    keep = ("name", "kind", "enabled", "start", "status", "started", "finished", "note", "options")
    snap = {"name": _routine["name"], "started_at": _stamp(started), "finished_at": _stamp(_now()), "result": result,
            "steps": [{k: s.get(k) for k in keep} for s in _routine["steps"]]}
    try:
        d = _history_dir()
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{started:%Y%m%d_%H%M%S}.json")
        _write(path, snap)
        for old in sorted(glob.glob(os.path.join(d, "*.json")))[:-HISTORY_KEEP]:
            os.remove(old)
        return path
    except OSError as e:
        _log(f"[WARNING] Routine: run history not saved: {e}")
        return None


def routine_runs():
    """Routine runs for the Results tab, newest first: the running one, then the saved history.
    -> [{"label", "name", "started_at", "finished_at", "result", "steps"}]."""
    out = []
    with _lock:
        r = copy.deepcopy(current())
        active, started = _state["active"], _state["started_at"]
    if active and started is not None:
        out.append({"label": f"{started:%Y-%m-%d %H:%M}  {r['name']}  (running)", "name": r["name"],
                    "started_at": _stamp(started), "finished_at": None, "result": "running", "steps": r["steps"]})
    for p in sorted(glob.glob(os.path.join(_history_dir(), "*.json")), reverse=True):
        try:
            with open(p, encoding="utf-8") as f:
                h = json.load(f)
            t = _parse(h.get("started_at"))
            h["label"] = f"{t:%Y-%m-%d %H:%M}  {h.get('name', '?')}" + (
                f"  ({h['result']})" if h.get("result") not in (None, "finished") else "")
            out.append(h)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    if not out and any(s.get("started") for s in r["steps"]):   # a run from before the history existed
        t = min(_parse(s["started"]) for s in r["steps"] if s.get("started"))
        out.append({"label": f"{t:%Y-%m-%d %H:%M}  {r['name']}", "name": r["name"], "started_at": _stamp(t),
                    "finished_at": None, "result": "", "steps": r["steps"]})
    return out


def _set_current(r):
    global _routine
    for s in r["steps"]:
        s.update(status="Pending" if s["enabled"] else "Disabled", started=None, finished=None, due=None, note="")
    _routine = r
    _changed()


def set_current(routine):
    """Replace the current routine with an edited one (page). Raises while a routine runs."""
    with _lock:
        if _state["active"]:
            raise RuntimeError("a routine is running")
        r = normalize(routine)
        _split(r, f"routine '{r['name']}'")
        _set_current(r)
    return _routine


ACQ_SLOTS = ("daq", "general_scan", "stability", "hk")


def _inside(step, elapsed, est):
    """(text, fraction or None) of the running step from its engine; reads only."""
    kind = step.get("kind")
    try:
        if kind == "dark_rate":
            from core import stability as stab
            d = getattr(stab.RUN, "dark", None)
            if stab.RUN.running and d is not None and getattr(d, "cycles", 0):
                n = max(1, len(d.steps))
                done = d.cycle * n + d.pos
                return f"cycle {min(d.cycle + 1, d.cycles)}/{d.cycles}", min(1.0, done / float(d.cycles * n))
        if kind == "stability_qe":
            from core import stability as stab
            if stab.RUN.running and stab.RUN.plan:
                n = len(stab.RUN.plan)
                return (f"block {min(stab.RUN.idx + 1, n)}/{n}" if n > 1 else ""), None
        if kind in ("general_scan", "hk_scan", "wavelength_scan"):
            from core import scan_progress
            snap = scan_progress.snapshot()
            if snap.get("running") and snap.get("total"):
                done, total = len(snap.get("done") or []), int(snap["total"])
                return f"point {min(done + 1, total)}/{total}", min(1.0, done / float(total))
        if kind == "hv_scan":
            man = _hv_scan.get("manifest")
            offs = (step.get("options") or {}).get("offsets_v") or []
            if man and offs and str(man.get("started") or "") >= str(step.get("started") or ""):
                k = len(man.get("points") or [])
                return f"offset {min(max(k, 1), len(offs))}/{len(offs)}", min(1.0, max(0, k - 1) / float(len(offs)))
    except Exception:
        pass
    return "", None


HV_POINT_STATE = {"done": "done", "running": "current", "hv failed": "error", "run failed": "error",
                  "stopped": "error"}


def _units(step):
    """[(state, label)] of the running step's units (state done / current / pending / error), or None."""
    kind = step.get("kind")
    try:
        if kind == "dark_rate":
            from core import stability as stab
            d = getattr(stab.RUN, "dark", None)
            if stab.RUN.running and d is not None and getattr(d, "cycles", 0):
                return [("done" if i < d.cycle else "current" if i == d.cycle else "pending",
                         f"cycle {i + 1}/{d.cycles}") for i in range(d.cycles)]
        if kind == "hv_scan":
            man = _hv_scan.get("manifest")
            offs = (step.get("options") or {}).get("offsets_v") or []
            if man and offs and str(man.get("started") or "") >= str(step.get("started") or ""):
                pts = man.get("points") or []
                out = []
                for k, off in enumerate(offs):
                    pt = pts[k] if k < len(pts) else None
                    st = HV_POINT_STATE.get(pt.get("status"), "pending") if pt else "pending"
                    runs = ", ".join(str(r) for r in (pt or {}).get("runs") or [])
                    out.append((st, f"offset {float(off):+g} V" + (f"   run {runs}" if runs else "")))
                return out
        if kind in ("general_scan", "hk_scan", "wavelength_scan"):
            from core import scan_progress
            snap = scan_progress.snapshot()
            total = int(snap.get("total") or 0) if snap.get("running") else 0
            if total:
                done = list(reversed(snap.get("done") or []))[:total]
                out = []
                for k, r in enumerate(done):
                    ok = str(r.get("status", "")).upper() == "OK"
                    out.append(("done" if ok else "error", f"point {k + 1}/{total}   {r.get('axis') or ''} "
                                f"{r.get('tilt')}°   run {r.get('run') or '-'}" + ("" if ok else f"   {r.get('status')}")))
                out += [("current" if k == len(done) else "pending", f"point {k + 1}/{total}")
                        for k in range(len(done), total)]
                return out
    except Exception:
        pass
    return None


def _acquisition():
    """The acquisition job running now: {"slot", "evt", "total", "rate", "run"} or None."""
    try:
        from core.jobs import JOBS
        job = next((JOBS.jobs[s] for s in ACQ_SLOTS if s in JOBS.jobs and JOBS.jobs[s].state == "running"), None)
    except Exception:
        job = None
    if job is None:
        return None
    p = job.progress
    run = ""
    try:
        from core import daq_live
        snap = daq_live.read()
        if snap and snap["state"] == "running" and time.time() - snap["time"] < 10:
            run = snap["output"].rsplit("_", 1)[-1].lstrip("0") or "0"
    except Exception:
        pass
    return {"slot": job.slot if hasattr(job, "slot") else "", "evt": getattr(p, "evt", 0) if p else 0,
            "total": getattr(p, "total", 0) if p else 0, "rate": getattr(p, "rate", None) if p else None, "run": run}


def progress(now=None):
    """Everything the progress views show; {"active": False} when no routine runs."""
    now = now or _now()
    with _lock:
        if not _state["active"] or _state["idx"] is None:
            return {"active": False}
        r = copy.deepcopy(current())
        idx = _state["idx"]
    enabled = [i for i, s in enumerate(r["steps"]) if s["enabled"]]
    step = r["steps"][idx]
    started = _parse(step.get("started")) or now
    elapsed = (now - started).total_seconds()
    est = estimate_seconds(step, r, started)
    text, frac = _inside(step, elapsed, est)
    if frac is None and est > 0:
        frac = min(0.99, elapsed / est)
    left = max(0.0, est - elapsed) if est else None
    if frac is not None and frac > 0.05 and est:
        left = max(0.0, elapsed * (1 - frac) / frac)
    cursor = now + timedelta(seconds=left or 0)
    for s in r["steps"][idx + 1:]:
        if s["enabled"] and s["status"] == "Pending":
            due = _parse(s.get("due"))
            start = max(due, cursor) if due else cursor
            cursor = start + timedelta(seconds=estimate_seconds(s, r, start))
    return {"active": True, "name": r["name"], "pos": enabled.index(idx) + 1 if idx in enabled else idx + 1,
            "n": len(enabled), "index": idx, "step": step["name"], "kind": step["kind"], "text": text,
            "frac": frac, "step_left_s": left, "end": cursor, "acq": _acquisition(), "units": _units(step),
            "next_daq_s": next_daq.seconds_left()}


def update_pending_step(i, name, options):
    """Name and options of step i while the routine runs; only a Pending step (the runner reads them at its start)."""
    with _lock:
        steps = current()["steps"]
        if not 0 <= i < len(steps):
            raise ValueError("no such step")
        s = steps[i]
        if _state["active"] and s["status"] != "Pending":
            raise RuntimeError(f"'{s['name']}' is {s['status']}; only Pending steps can be changed while the routine runs")
        s["name"] = str(name or s["name"])
        s["options"] = dict(options or {})
        _changed()
    _log(f"[INFO] Routine: options of '{s['name']}' changed while the routine runs.")


def set_slots(times):
    """Slot times of the current routine in order ([] = none), also written to its preset file."""
    out = []
    for t in times or []:
        c = _clock(t)
        if c is None:
            raise ValueError(f"bad slot time {t!r} (HH:MM)")
        if "%02d:%02d" % c not in out:
            out.append("%02d:%02d" % c)
    with _lock:
        if _state["active"]:
            raise RuntimeError("a routine is running")
        r = current()
        r["slots"] = out
        _changed()
        p = _path(r["name"])
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            d["slots"] = list(out)
            _write(p, d)
    _log(f"[INFO] Routine: slots of '{r['name']}' set to {', '.join(out) or 'none'}.")
    return out


def _persist():
    try:
        _write(os.path.join(_dir(), CURRENT), _routine)
    except OSError as e:
        _log(f"[ERROR] Routine: could not write {CURRENT}: {e}")


def _changed():
    _persist()
    for fn in list(listeners):
        try:
            fn()
        except RuntimeError:   # its widget was deleted
            listeners.remove(fn)
        except Exception:
            pass


# ---------------------------------------------------------------- estimates
def _engine_per_point():
    host = getattr(sys.modules.get("core.scan"), "_host", None)
    try:
        if host is not None:
            return float(host.auto_mgr._estimate_nominal_per_point_seconds(fallback=EST["caen_point_s"]))
    except Exception:
        pass
    return EST["caen_point_s"]


def _config_events():
    try:
        return int(float(env.read_config().get("Events", "")))
    except (TypeError, ValueError, OSError):
        return None


def _point_seconds(opts):
    """Per-point time (CAEN): fixed move/start overhead + the step's events (else config Events) at the step's
    trigger rate (else the current one). HK: the engine's measured time."""
    if _backend_name()[0] == "HK":
        return _engine_per_point()
    from core import stability as stab
    ev = float(opts.get("events") or _config_events() or 300000)
    hz = float(opts.get("trigger_hz") or stab.rate_hz())
    return EST["caen_point_overhead_s"] + ev / hz


TILT_LIMITS, MUST_ANGLES = (-55, 55), (-55, 0, 55)   # AutomationManager.scan_range / mandatory_tilt_angles


def scan_angles(opts):
    """-> (sorted tilt angles or None, error) from options["angles"]; missing = the engine's step grid."""
    raw = (opts or {}).get("angles")
    if not raw:
        return None, ""
    try:
        vals = [float(a) for a in raw]
    except (TypeError, ValueError):
        return None, "Angles: numbers only"
    if any(v != int(v) for v in vals):
        return None, "Angles: whole degrees only"
    angles = sorted({int(v) for v in vals})
    if angles[0] < TILT_LIMITS[0] or angles[-1] > TILT_LIMITS[1]:
        return None, f"Angles: outside {TILT_LIMITS[0]}..+{TILT_LIMITS[1]} deg"
    missing = [a for a in MUST_ANGLES if a not in angles]
    if missing:
        return None, "Angles: " + ", ".join(f"{a:+d}" if a else "0" for a in missing) + " deg must be included"
    return angles, ""


def angles_text(angles):
    return ", ".join(f"{a:+d}" if a else "0" for a in angles)


def _grid(opts):
    """(points, axes) of a scan pass: the step's angle list, else the engine's angle grid (X and Y), else the options."""
    angles = scan_angles(opts)[0]
    if angles:
        return 2 * len(angles), 2
    am = _scan_engine()
    if am is not None:
        try:
            return 2 * len(am.build_tilt_angles()), 2
        except Exception:
            pass
    return int(opts.get("points", 46)), int(opts.get("axes", 2))


def dark_thr(opts):
    v = opts.get("thr_mv")
    return DARK_THR_MV if v in (None, "") else float(v)


def _dark_steps(opts):
    from core import stability as stab
    rows = stab.load_dark()["rows"]
    channels = list(opts.get("channels") or ["mon", "rot1", "rot2"])
    thr = f"{dark_thr(opts):g}"
    for key, row in rows.items():
        row["on"] = key in channels
        row["thr"] = thr
        if opts.get("events"):
            row["ev"] = str(int(opts["events"]))
    return stab.dark_steps(rows) or []


def dark_cycle_seconds(steps):
    from core import stability as stab
    return sum(stab.dark_step_seconds(s) + s[7] for s in steps)


def dark_cycles(opts, steps, until, now):
    """Cycles that fit before `until` (every every_min, the last one ending before it), or the fixed count."""
    period = float(opts.get("every_min", 15)) * 60.0
    cycle = dark_cycle_seconds(steps)
    if opts.get("until_next_step", True) and until is not None:
        window = (until - now).total_seconds() - cycle
        return int(window // max(period, cycle)) + 1 if window >= 0 else 0
    return max(0, int(opts.get("cycles", 1)))


def estimate_seconds(step, routine=None, now=None):
    """Nominal duration of one step; the dark window uses the routine's due times when it has them."""
    from core import stability as stab
    opts = step.get("options") or {}
    kind = step.get("kind")
    try:
        if kind == "general_scan":
            points, axes = _grid(opts)
            total = EST["scan_startup_s"] + points * _point_seconds(opts) + axes * EST["axis_change_s"]
            start = _parse(step.get("due")) or now or _now()
            y_at = y_start_target(opts, start) if axes == 2 else None
            if y_at is not None:   # the Y half runs from its start time
                total = max(total, (y_at - start).total_seconds() + points / 2 * _point_seconds(opts))
            return total
        if kind == "hk_scan":
            points, axes = _grid(opts)
            return EST["scan_startup_s"] + points * EST["hk_point_s"] + axes * EST["axis_change_s"]
        if kind == "wavelength_scan":
            n = len(opts.get("wavelengths") or []) or 1
            points, axes = _grid(opts)
            return EST["scan_startup_s"] + n * (points * _point_seconds(opts) + axes * EST["axis_change_s"])
        if kind == "stability_qe":
            opts = qe_until(opts, _next_clock_due(routine, step) if routine else None,
                            _parse(step.get("due")) or now or _now())
            plan = _qe_plan(opts)
            return stab.plan_total(plan, float(opts.get("events", 300000)), opts.get("trigger_hz")) if plan else 0.0
        if kind == "dark_rate":
            steps = _dark_steps(opts)
            if not steps:
                return 0.0
            period = float(opts.get("every_min", 15)) * 60.0
            until = _next_clock_due(routine, step) if routine else None
            start = _parse(step.get("due")) or now or _now()
            cycles = dark_cycles(opts, steps, until, start)
            return stab.dark_total(steps, cycles, period) if cycles else 0.0
        if kind == "hv_apply":
            n = 3 if opts.get("mode") == "gain_curve" else len(opts.get("volts") or {}) or 3
            return n * EST["hv_channel_s"] + float(opts.get("gain_delay_s", 180))
        if kind == "hv_scan":
            n = len(opts.get("offsets_v") or [])
            if not n:
                return 0.0
            settle = float(opts.get("settle_s", 180))
            hz = float(opts.get("trigger_hz") or stab.rate_hz())
            nch = len(hv_scan_channels(opts)) or 1
            per = nch * EST["hv_channel_s"] + settle + float(opts.get("events", 100000)) / hz + stab.OVERHEAD_S
            return (EST["axis_change_s"] + stab.LASER_PREP_S + n * per + nch * EST["hv_channel_s"] + settle
                    + EST["hv_fit_s"])
    except Exception as e:
        _log(f"[WARNING] Routine: estimate for '{step.get('name')}' failed: {e}")
    return 0.0


# ---------------------------------------------------------------- timing
def _parse(text):
    try:
        return datetime.strptime(text, TS).replace(tzinfo=JST) if text else None
    except (TypeError, ValueError):
        return None


def _clock_target(hhmm, after):
    h, m = _clock(hhmm)
    t = after.replace(hour=h, minute=m, second=0, microsecond=0)
    return t if t > after else t + timedelta(days=1)


def assign_due(routine, started_at):
    """Clock-time steps get their due time: today, or tomorrow when already past at start; never
    earlier than the previous enabled clock step. Sequential steps have none."""
    last = None
    for s in routine["steps"]:
        s["due"] = None
        if not s["enabled"] or s["start"] == SEQUENTIAL:
            continue
        t = _clock_target(s["start"], started_at)
        if last is not None and t < last:
            t += timedelta(days=1)
        last = t
        s["due"] = _stamp(t)


def late_start(routine=None, now=None):
    """(name, HH:MM, due) of the first clock-time step when its time has already passed today, else None."""
    now = now or _now()
    r = routine if routine is not None else current()
    for s in r["steps"]:
        if s["enabled"] and s["start"] != SEQUENTIAL:
            due = _clock_target(s["start"], now)
            return (s["name"], s["start"], due) if due.date() > now.date() else None
    return None


def late_start_text(routine=None, now=None):
    """One line for the operator when the routine would wait until tomorrow; '' otherwise."""
    now = now or _now()
    late = late_start(routine, now)
    if not late:
        return ""
    name, hhmm, due = late
    mins = int((due - now).total_seconds() // 60)
    return (f"'{name}' start time {hhmm} has already passed: it starts TOMORROW {due:%m-%d %H:%M} "
            f"(in {mins // 60} h {mins % 60:02d} min) and every later step waits for it")


def _next_clock_due(routine, step):
    """Due time of the first enabled clock-time step after `step`, or None."""
    steps = routine["steps"]
    i = next((k for k, s in enumerate(steps) if s is step), None)
    if i is None:
        return None
    for s in steps[i + 1:]:
        if s["enabled"] and s["due"]:
            return _parse(s["due"])
    return None


def _ceil5(t):
    t = t.replace(second=0, microsecond=0) + (timedelta(minutes=1) if t.second or t.microsecond else timedelta(0))
    return t + timedelta(minutes=(-t.minute) % 5)


def recommended_starts(routine=None, now=None):
    """{step index: (recommended start, previous step end)} for clock-time steps that would start before the
    previous enabled step ends. Suggestion only; with `routine` given (or idle) it plans a start now."""
    now = now or _now()
    with _lock:
        r = copy.deepcopy(routine if routine is not None else current())
        active = routine is None and _state["active"]
    if not active:
        for s in r["steps"]:
            s.update(started=None, finished=None)
        assign_due(r, now)
    out, prev_end = {}, None
    for i, s in enumerate(r["steps"]):
        if not s["enabled"]:
            continue
        started, finished = _parse(s.get("started")), _parse(s.get("finished"))
        if finished is not None:
            prev_end = finished
            continue
        if s.get("status") == "Skipped":
            continue
        if started is not None:
            prev_end = max(now, started + timedelta(seconds=estimate_seconds(s, r, started)))
            continue
        due = _parse(s.get("due"))
        if due is not None and prev_end is not None and due < prev_end:
            due = _ceil5(prev_end + timedelta(seconds=RECOMMEND_GAP_S))
            out[i] = (due, prev_end)
            s["due"] = _stamp(due)
        free = max(now, prev_end) if prev_end is not None else now
        start = max(due, free) if due is not None else free
        prev_end = start + timedelta(seconds=estimate_seconds(s, r, start))
    return out


# ---------------------------------------------------------------- busy / collision rules
def _busy():
    """What core/schedule.py refuses to start over, plus a Stability/dark run between its acquisitions."""
    from core import schedule, stability
    return schedule._scan_busy() or stability.RUN.running


def _queued_times():
    """Due times of the Schedule Manager queue (the file while the Tk app owns it)."""
    from core import schedule
    out = []
    try:
        for time_str, st, _cfg in schedule.view():
            if st == "WAITING":
                out.append(datetime.strptime(time_str, "%Y-%m-%d %H:%M").replace(tzinfo=JST))
    except Exception:
        pass
    return out


def _collision(step, now):
    """The queued Schedule Manager time that falls inside this step's estimated run (+ guard), or None."""
    end = now + timedelta(seconds=estimate_seconds(step, _routine, now) + COLLISION_GUARD_S)
    for t in _queued_times():
        if t <= end:
            return t
    return None


# ---------------------------------------------------------------- runner
class RunContext:
    """What an executor gets besides its step."""

    def __init__(self, idx, until):
        self.idx = idx
        self.until = until          # next enabled clock-time step's due time, or None

    @staticmethod
    def stop_requested():
        return _state["stop"] or _state["skip"]

    @staticmethod
    def now():
        return _now()


def _set(idx, status, note=None, started=None, finished=None):
    s = _routine["steps"][idx]
    s["status"] = status
    if note is not None:
        s["note"] = note
    if started is not None:
        s["started"] = _stamp(started)
    if finished is not None:
        s["finished"] = _stamp(finished)
    _changed()


def _next_pending():
    for i, s in enumerate(_routine["steps"]):
        if s["status"] == "Pending":
            return i
    return None


def _hold(idx, why, msg):
    if _waited.get(idx) != why:
        _waited[idx] = why
        _log(msg)


def start_routine(now=None):
    """-> (ok, message). Resets every step and starts the runner thread."""
    now = now or _now()
    with _lock:
        r = current()
        if _state["active"]:
            return False, "A routine is already running."
        if hv_restore_active():
            return False, "The previous routine is still finishing (restoring the header HV, stages to the origin)."
        if not any(s["enabled"] for s in r["steps"]):
            return False, "The routine has no enabled step."
        _split(r, f"routine '{r['name']}'")
        for s in r["steps"]:
            s.update(status="Pending" if s["enabled"] else "Disabled", started=None, finished=None, note="")
        assign_due(r, now)
        _state.update(active=True, started_at=now, idx=None, stop=False, skip=False, result="running")
        _state.pop("hv_restore", None)
        _state.pop("hv_fit", None)
        r.pop("hv_restore", None)
        r.pop("hv_scan_restore", None)
        _waited.clear()
        _changed()
        start_runner()
    _log(f"[INFO] Routine: '{r['name']}' started with {sum(s['enabled'] for s in r['steps'])} enabled step(s).")
    late = late_start_text(r, now)
    if late:
        _log(f"[WARNING] Routine: {late}.")
    return True, ""


def _abort_running(reason):
    idx = _state["idx"]
    if idx is None or _worker is None or not _worker.is_alive():
        return False
    kind = _routine["steps"][idx]["kind"]
    fn = ABORTERS.get(kind)
    if fn is not None:
        try:
            fn()
        except Exception as e:
            _log(f"[WARNING] Routine: abort of '{_routine['steps'][idx]['name']}' failed: {e}")
    _log(f"[INFO] Routine: step '{_routine['steps'][idx]['name']}' {reason}.")
    return True


def stop_routine():
    """On Error: Stop. The running step is aborted when its kind has an aborter; nothing further starts."""
    with _lock:
        if not _state["active"]:
            return False
        _state["stop"] = True
        if not _abort_running("abort requested, routine stopping"):
            _finish("stopped")
    return True


def skip_step():
    """Mark the running step (aborting it) or the next pending one Skipped. -> step index or None."""
    with _lock:
        if _state["active"] and _worker is not None and _worker.is_alive():
            _state["skip"] = True
            _abort_running("skip requested")
            return _state["idx"]
        idx = _next_pending() if _routine else None
        if idx is None:
            return None
        _set(idx, "Skipped", note="skipped by operator", finished=_now())
    _log(f"[INFO] Routine: step '{_routine['steps'][idx]['name']}' skipped by operator.")
    return idx


def _send(title, body, level, key):
    try:
        from core import applog
        n = applog.notifier()
        if n.enabled:
            n.send(title, body, level=level, dedupe_key=None, blocking=False, key=key)
    except Exception as e:
        _log(f"[WARNING] Routine: Slack notification failed: {type(e).__name__}.")


def _notify_end(result):
    """Slack: routine_finished (info) or routine_failed (warning, also on stop)."""
    steps = _routine["steps"]
    counts = {}
    for s in steps:
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    started = _state.get("started_at")
    body = (f"Routine: {_routine['name']}\n"
            f"Start: {started:%Y-%m-%d %H:%M} JST\n" if started else f"Routine: {_routine['name']}\n")
    body += f"End: {_now():%Y-%m-%d %H:%M} JST\n"
    body += "Steps: " + ", ".join(f"{k} {counts[k]}" for k in STATUS if k in counts)
    bad = next((s for s in steps if s["status"] == "Failed"), None)
    if bad is not None:
        body += f"\nFailed step: {bad['name']}" + (f": {bad['note']}" if bad["note"] else "")
    if result == "finished" and bad is not None:
        _send("Daily Routine finished with a failed step", body, "warning", "routine_failed")
    elif result == "finished":
        _send("Daily Routine finished", body, "info", "routine_finished")
    else:
        _send(f"Daily Routine {result.upper()}", body, "warning", "routine_failed")


def _finish(result):
    global _restore_thread
    volts = _state.pop("hv_restore", None)
    _routine.pop("hv_scan_restore", None)
    home = bool(_routine.get("home_stage", True)) and not env.NO_HW and _scan_engine() is not None
    if volts or home:
        _restore_thread = threading.Thread(target=_end_actions, args=(volts, home), daemon=True, name="routine-end")
        _restore_thread.start()
    _state.update(active=False, idx=None, result=result)
    _changed()
    hist = _save_history(result)
    _log(f"[INFO] Routine: '{_routine['name']}' {result}.")
    _notify_end(result)
    if hist and any(s.get("status") in ("Done", "Failed") for s in _routine["steps"]):
        from core import summary
        summary.launch([hist], f"routine '{_routine['name']}'")
    from core import summary
    summary.launch_uniformity()


def _start_step(idx, now):
    global _worker
    step = _routine["steps"][idx]
    _state["idx"] = idx
    _state["skip"] = False
    _set(idx, "Running", note="", started=now)
    _log(f"[INFO] Routine: step {idx + 1} '{step['name']}' ({step['kind']}) started.")
    ctx = RunContext(idx, _next_clock_due(_routine, step))
    _worker = threading.Thread(target=_run_step, args=(idx, ctx), daemon=True, name="routine-step")
    _worker.start()


def _run_step(idx, ctx):
    step = _routine["steps"][idx]
    try:
        err = _to_origin(ctx, f"before '{step['name']}'") if step["kind"] in ORIGIN_BEFORE else None
        if err:
            ok, note = False, f"stages not at the origin: {err}"
        else:
            ok, note = EXECUTORS[step["kind"]](step, ctx)
    except Exception as e:
        ok, note = False, f"{type(e).__name__}: {e}"
    if ok is True and step["kind"] in ORIGIN_AFTER and _routine.get("home_stage", True) and not _state["stop"]:
        try:
            err = _to_origin(ctx, f"after '{step['name']}'")
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        if err:
            _log(f"[WARNING] Routine: stages not moved to the origin after '{step['name']}': {err}")
    with _lock:
        if ok == FAILED:
            status = "Failed"
        elif _state["stop"] or _state["skip"]:
            status = "Skipped" if not ok else "Done"
            note = note or ("stopped by operator" if _state["stop"] else "skipped by operator")
        else:
            status = "Done" if ok else "Failed" if ok is False else "Skipped"
        _set(idx, status, note=note or "", finished=_now())
        _state["skip"] = False
        _log(f"[{'INFO' if status != 'Failed' else 'ERROR'}] Routine: step {idx + 1} '{step['name']}' "
             f"{status.lower()}{': ' + note if note else ''}.")
        if _state["stop"]:
            _finish("stopped")
        elif status == "Failed" and (step["kind"] in STOP_ON_FAIL or DIGITIZER_DEAD in (note or "")):
            _finish("failed")
        elif status == "Failed":
            _log("[WARNING] Routine: continuing with the next step.")


def tick(now=None):
    """One runner step. -> what happened (for logs/tests)."""
    now = now or _now()
    with _lock:
        if not _state["active"]:
            return "idle"
        if _worker is not None and _worker.is_alive():
            return "running"
        if _state["stop"]:
            _finish("stopped")
            return "stopped"
        idx = _next_pending()
        if idx is None:
            _finish("finished")
            return "finished"
        step = _routine["steps"][idx]
        due = _parse(step["due"])
        if due is not None and now < due:
            return "waiting"
        from core.access import ACCESS
        if not ACCESS.unlocked:
            _hold(idx, "locked", f"[WARNING] Routine: step '{step['name']}' is due but controls are locked; waiting.")
            return "locked"
        if _busy():
            _hold(idx, "busy", f"[WARNING] Routine: step '{step['name']}' is due but the system is busy; waiting.")
            return "busy"
        clash = _collision(step, now)
        if clash is not None:
            _set(idx, "Skipped", note=f"Schedule Manager run at {clash:%Y-%m-%d %H:%M} JST overlaps this step",
                 finished=now)
            _log(f"[WARNING] Routine: step '{step['name']}' skipped: a Schedule Manager run at "
                 f"{clash:%Y-%m-%d %H:%M} JST would fall inside it.")
            return "skipped: schedule"
        _start_step(idx, now)
        return "started"


def _loop():
    while True:
        try:
            tick()
        except Exception as e:
            _log(f"[ERROR] Routine runner: {e}")
        time.sleep(TICK_S)


def start_runner():
    """Idempotent; the runner thread lives for the whole app."""
    global _runner
    with _lock:
        if _runner is None:
            _runner = threading.Thread(target=_loop, daemon=True, name="routine-runner")
            _runner.start()


def is_active():
    return _state["active"]


START_REQUEST = os.path.expanduser("~/.cache/precal/daq_v2_routine_request")   # JSON {"preset": name}


def start_from_request(path=START_REQUEST):
    """Load and start the preset named in the request file when it exists; the pre-run check must pass.
    -> True when a routine was started."""
    if env.NO_HW or not os.path.exists(path):   # never in the test mode
        return False
    try:
        with open(path, encoding="utf-8") as f:
            name = (json.load(f) or {}).get("preset")
    except (OSError, ValueError):
        name = None
    try:
        os.remove(path)
    except OSError:
        pass
    if not name:
        _log("[WARNING] Routine: start request file without a preset name ignored.")
        return False
    if is_active():
        _log(f"[WARNING] Routine: start request for '{name}' ignored: a routine is running.")
        return False
    try:
        load(name)
    except (OSError, ValueError, RuntimeError) as e:
        _log(f"[ERROR] Routine: start request for '{name}' failed: {e}")
        return False
    res = dry_run(now=_now())
    bad = [t for st in res["steps"] for lv, t in st["lines"] if lv == "error"] + \
          [t for lv, t in res["summary"] if lv == "error"]
    if bad:
        _log(f"[ERROR] Routine: start request for '{name}' refused by the pre-run check: " + "; ".join(bad))
        return False
    ok, msg = start_routine()
    _log(f"[INFO] Routine: '{name}' started by request file." if ok
         else f"[ERROR] Routine: start request for '{name}' failed: {msg}")
    return bool(ok)


def view():
    """Status snapshot for the page."""
    with _lock:
        r = current()
        steps = []
        for i, s in enumerate(r["steps"]):
            d = dict(s)
            d["index"] = i
            d["estimate_s"] = estimate_seconds(s, r)
            steps.append(d)
        return {"name": r["name"], "on_error": r["on_error"], "slots": list(r["slots"]),
                "home_stage": r.get("home_stage", True),
                "state": _state["result"] if not _state["active"] else "running",
                "active": _state["active"], "started_at": _stamp(_state["started_at"]),
                "current": _state["idx"], "steps": steps}


# ---------------------------------------------------------------- issues: derived from the saved step states
LEVELS = ("Error", "Warning", "Note")
WARN_NOTES = ("provisional", "extrapolated", "fit failed", "fit stopped", "gain curve unusable", "warning(s)")
STOP_ON_FAIL = ("hv_scan", "hv_apply")   # a failed step of any other kind: the routine goes on
DIGITIZER_DEAD = "digitizer not answering"   # set by the scan engine; every later step would fail too
last_check = None    # the last Pre-run Check / dummy test of this session


def set_last_check(mode, summary, steps=()):
    """mode 'start' or 'check-only'; steps as dry_run() returns them."""
    global last_check
    last_check = {"time": _stamp(_now()), "mode": mode, "summary": [tuple(x) for x in summary],
                  "steps": [{"name": s["name"], "start": _stamp(s["start"]), "end": _stamp(s["end"]),
                             "lines": [tuple(x) for x in s["lines"]]} for s in steps]}


def _end_limit(r, i, started):
    """(time, text) a step started at `started` should have ended by: the next clock step or slot boundary."""
    cands = []
    for t in r["steps"][i + 1:]:
        if t["enabled"] and t.get("due"):
            d = _parse(t["due"])
            if d is not None and d > started:
                cands.append((d, f"the next clock step '{t['name']}' ({d:%H:%M})"))
            break
    for hhmm in r.get("slots") or []:
        if _clock(hhmm):
            b = _clock_target(hhmm, started)
            cands.append((b, f"the {b:%H:%M} slot"))
    return min(cands, key=lambda c: c[0]) if cands else (None, "")


def issues(routine=None):
    """Errors, warnings and notes of the steps, newest first, from the saved step states (survives a restart).
    -> [{time, step, level, message, acked}]."""
    with _lock:
        r = copy.deepcopy(routine if routine is not None else current())
    ack = r.get("issues_ack") or ""
    out = []
    for i, s in enumerate(r["steps"]):
        st, note = s.get("status"), str(s.get("note") or "").strip()
        when = s.get("finished") or s.get("started") or s.get("due") or ""

        def add(level, msg):
            out.append({"time": when, "step": s["name"], "level": level, "message": msg,
                        "acked": bool(ack) and when <= ack})
        if st == "Failed":
            add("Error", note or "failed")
            note = ""
        elif note and ((st == "Skipped" and "Schedule Manager run" in note) or any(k in note for k in WARN_NOTES)):
            add("Warning", note)
            note = ""
        started, finished = _parse(s.get("started")), _parse(s.get("finished"))
        if started is not None and finished is not None:
            lim, what = _end_limit(r, i, started)
            if lim is not None and finished > lim:
                add("Warning", f"ran past {what} by {int((finished - lim).total_seconds() // 60)} min")
        if note and st != "Done":     # a finished step's summary is not an issue
            add("Note", note)
    out.sort(key=lambda d: d["time"], reverse=True)
    return out


def issue_counts(items):
    """Unacknowledged items per level."""
    n = {lv: 0 for lv in LEVELS}
    for d in items:
        if not d["acked"]:
            n[d["level"]] += 1
    return n


def ack_issues(now=None):
    """Acknowledge: items up to now no longer count (still listed)."""
    with _lock:
        r = current()
        r["issues_ack"] = _stamp(now or _now())
        _changed()


# ---------------------------------------------------------------- executors: (ok, note); ok None = Skipped
def _scan_running(hooks):
    try:
        return bool(hooks["is_running"]()) if hooks["is_running"] else False
    except Exception:
        return False


def _scan_list(hooks, key):
    try:
        return list(hooks[key]() or []) if hooks.get(key) else []
    except Exception:
        return []


ROTATE = (("rot1", "Rot1", "rotate_dev2"), ("rot2", "Rot2", "rotate_dev3"))   # engine Rotate Devices


def rotate_choice(opts):
    """-> ({engine attr: bool}, error) from options["rotate"]; missing = both rotate."""
    rot = (opts or {}).get("rotate")
    on = [k for k, _l, _a in ROTATE] if rot is None else list(rot)
    flags = {attr: key in on for key, _l, attr in ROTATE}
    if not any(flags.values()):
        return flags, "Rotation: select at least one stage (Rot1 / Rot2)"
    return flags, ""


def _scan_engine():
    from core import scan
    return getattr(scan._host, "auto_mgr", None) if scan._host is not None else None


def _fmt_hz(hz):
    if hz >= 1e6:
        return f"{hz / 1e6:g} MHz"
    return f"{hz / 1000:g} kHz" if hz >= 1000 else f"{hz:g} Hz"


def _trigger_write(cfg, period_ns, who):
    from core import fg_schema as S
    from core import function_generator as fg
    enables, _co = S.read_enables("pulse_generator", cfg)
    fg.set_pulse_generator(int(period_ns), int(cfg.get("width") or 0), enables, who=who, frequency_type=0)
    got = fg.trigger_hz()
    want = 1e9 / float(period_ns)
    if not got or abs(got - want) > TRIGGER_TOL * want:
        raise RuntimeError(f"read-back {_fmt_hz(got) if got else 'none'}, requested {_fmt_hz(want)}")


def _trigger_prepare(hz):
    """-> (previous pulse-generator config, error) for a step trigger rate; reads only."""
    from core import function_generator as fg
    try:
        cfg = fg.pulse_generator()
    except Exception as e:
        return None, f"Cannot read function generator: {e}"
    if not cfg:
        return None, "Function generator section A is not in pulse generator mode"
    if cfg.get("frequency_type"):
        return None, "Function generator section A is in Poisson mode"
    period = int(round(1e9 / float(hz)))
    why = fg.validate(period, int(cfg.get("width") or 0))
    if why:
        return None, f"Trigger {_fmt_hz(float(hz))}: {why}"
    return cfg, None


def trigger_rule_text():
    """The trigger rate each DAQ backend needs, as shown to the operator."""
    from core import hk
    return (f"[Require] CAEN DAQ -> {_fmt_hz(CAEN_TRIGGER_HZ)}\n"
            f"[Require] HK Digitizer -> {_fmt_hz(float(hk.config().get('trigger_hz') or 1e6))}")


def caen_trigger_excess():
    """The trigger rate when it is above the CAEN readout limit, else None (also when it cannot be read)."""
    from core import function_generator as fg
    try:
        hz = fg.trigger_hz()
    except Exception:
        return None
    return hz if hz and hz > TRIGGER_WARN_HZ else None


def set_trigger_rate(hz, who):
    """Pulse generator to hz, verified by read-back. -> (previous Hz, error or None); no write when already there."""
    cfg, err = _trigger_prepare(hz)
    if err:
        return None, err
    old = 1e9 / float(cfg["period"])
    period = int(round(1e9 / float(hz)))
    if period == int(float(cfg["period"])):
        return old, None
    try:
        _trigger_write(cfg, period, who)
    except Exception as e:
        return old, f"Trigger rate not set to {_fmt_hz(float(hz))}: {e}"
    _rate_refresh(1e9 / period)
    return old, None


def _rate_refresh(hz=None):
    """Estimates follow the function generator: the read-back rate when known, else re-read it."""
    from core import stability as stab
    if hz:
        stab._rate.update(hz=float(hz), src="function generator", t=time.time())
    else:
        stab._rate["t"] = 0.0


HK_NEEDED = "HK Digitizer Scan needs the HK Digitizer: select it on the General Scan page and connect its signal cables"
CAEN_NEEDED = "the selected DAQ is the HK Digitizer: select CAEN DAQ on the General Scan page (or use an HK Digitizer Scan step)"


def run_hk_scan(step, ctx):
    """General Scan read out by the HK Digitizer: trigger rate, acquisition time and run numbers follow hk_config."""
    if _backend_name()[0] != "HK":
        return False, HK_NEEDED
    wl = scan_wavelength(step.get("options"))
    if wl:
        return _with_laser_sequence(step, ctx, [wl], _scan_with_angles)
    return _scan_with_angles(step, ctx)


def run_general_scan(step, ctx):
    """General Scan with the step's wavelength and trigger rate; both are restored after."""
    if _backend_name()[0] == "HK":
        return False, CAEN_NEEDED
    wl = scan_wavelength(step.get("options"))
    if wl:
        return _with_laser_sequence(step, ctx, [wl], _triggered_scan)
    return _triggered_scan(step, ctx)


def _triggered_scan(step, ctx):
    return _with_trigger(step, ctx, _scan_with_y_start)


Y_START_MAX_H = 12.0   # a Y-axis start time further away than this has already passed


def x_axis_seconds(opts):
    """Estimated (X axis done, Y axis aligned) in seconds from the scan start; None for a one-axis scan."""
    points, axes = _grid(opts)
    if axes != 2:
        return None
    x_done = EST["scan_startup_s"] + EST["axis_change_s"] + points / 2 * _point_seconds(opts)
    return x_done, x_done + EST["axis_change_s"]


def y_start_target(opts, start):
    """The datetime before which the step takes no Y-axis data (options["y_start"], HH:MM), or None."""
    text = str((opts or {}).get("y_start") or "").strip()
    if not text or _clock(text) is None:
        return None
    t = _clock_target(text, start)
    return t if (t - start).total_seconds() <= Y_START_MAX_H * 3600 else None


def _scan_with_y_start(step, ctx):
    """General Scan that holds at the Y-axis start position until the step's Y start time (this scan only)."""
    opts = step.get("options") or {}
    am = _scan_engine()
    target = y_start_target(opts, _now()) if am is not None else None
    if target is None:
        if opts.get("y_start"):
            _log(f"[WARNING] Routine: Y axis start {opts.get('y_start')} of '{step.get('name')}' is not used "
                 "(already passed or not a time); no wait before the Y axis.")
        return _scan_with_events(step, ctx)
    am.y_axis_start = target.timestamp()
    _log(f"[INFO] Routine: Y axis of '{step.get('name')}' starts at {target:%H:%M} JST (waits at its start position).")
    try:
        return _scan_with_events(step, ctx)
    finally:
        am.y_axis_start = None


def run_stability_qe_triggered(step, ctx):
    """Stability (QE/Gain) with the step's trigger rate; the previous rate is restored after."""
    return _with_trigger(step, ctx, run_stability_qe)


def run_hv_scan_triggered(step, ctx):
    """HV scan with the step's trigger rate; the previous rate is restored after."""
    return _with_trigger(step, ctx, run_hv_scan)


def _with_trigger(step, ctx, fn):
    hz = (step.get("options") or {}).get("trigger_hz")
    if not hz:
        return fn(step, ctx)
    name = step.get("name")
    prev, err = _trigger_prepare(hz)
    if err:
        return False, err
    old_period = float(prev["period"])
    new_period = int(round(1e9 / float(hz)))
    if new_period == int(old_period):
        return fn(step, ctx)
    try:
        _trigger_write(prev, new_period, "Daily Routine")
    except Exception as e:
        _log(f"[ERROR] Routine: trigger rate {_fmt_hz(float(hz))} for '{name}' not set: {e}")
        try:
            _trigger_write(prev, old_period, "Daily Routine")
        except Exception:
            pass
        _rate_refresh()
        return False, f"trigger rate not set: {e}"
    _rate_refresh(1e9 / new_period)
    _log(f"[INFO] Routine: trigger rate set to {_fmt_hz(1e9 / new_period)} for '{name}' "
         f"(was {_fmt_hz(1e9 / old_period)}).")
    ok, note = False, "scan not run"
    try:
        ok, note = fn(step, ctx)
    finally:
        try:
            _trigger_write(prev, old_period, "Daily Routine")
            _log(f"[INFO] Routine: trigger rate restored to {_fmt_hz(1e9 / old_period)}.")
        except Exception as e:
            _log(f"[ERROR] Routine: trigger rate not restored to {_fmt_hz(1e9 / old_period)}: {e}")
            ok, note = FAILED, f"{note}; trigger rate not restored to {_fmt_hz(1e9 / old_period)}: {e}"
        _rate_refresh()
    return ok, note


def _scan_with_events(step, ctx):
    """General Scan with the step's events per point (DAQ_EVENTS of this scan only); config3.h is not touched."""
    ev = (step.get("options") or {}).get("events")
    am = _scan_engine()
    if not ev or am is None or int(ev) == _config_events():
        return _scan_with_angles(step, ctx)
    prev = getattr(am, "events_override", None)
    am.events_override = int(ev)
    _log(f"[INFO] Routine: {int(ev):,} events per point for '{step.get('name')}' "
         f"(config3.h {_config_events() or '?'}, unchanged).")
    try:
        return _scan_with_angles(step, ctx)
    finally:
        am.events_override = prev
        _log("[INFO] Routine: events per point back to config3.h.")


def _scan_with_angles(step, ctx):
    """General Scan on the step's tilt angle list (this scan only); the engine's step grid is back after."""
    angles, err = scan_angles(step.get("options"))
    if err:
        return False, err
    am = _scan_engine()
    if not angles or am is None:
        return _run_rotated_scan(step, ctx)
    prev = getattr(am, "tilt_angles_override", None)
    am.tilt_angles_override = list(angles)
    _log(f"[INFO] Routine: {len(angles)} tilt angles per axis for '{step.get('name')}' ({angles_text(angles)}).")
    try:
        return _run_rotated_scan(step, ctx)
    finally:
        am.tilt_angles_override = prev
        _log("[INFO] Routine: tilt angles back to the step grid.")


def _run_rotated_scan(step, ctx):
    """General Scan with the step's Rotate Devices; the engine's previous choice is restored after."""
    flags, err = rotate_choice(step.get("options") or {})
    if err:
        return False, err
    am = _scan_engine()
    if am is None:
        return _run_general_scan(step, ctx)
    prev = {a: getattr(am, a, True) for a in flags}
    try:
        if flags != prev:
            for a, v in flags.items():
                setattr(am, a, v)
            _log(f"[INFO] Routine: Rotate Devices set to {_rotate_text(flags)} for '{step.get('name')}'.")
        return _run_general_scan(step, ctx)
    finally:
        if flags != prev:
            for a, v in prev.items():
                setattr(am, a, v)
            _log(f"[INFO] Routine: Rotate Devices restored to {_rotate_text(prev)}.")


def _rotate_text(flags):
    return " / ".join(lab for _k, lab, a in ROTATE if flags.get(a)) or "none"


def _run_general_scan(step, ctx):
    from core import schedule
    hooks = schedule._hooks
    if hooks["start"] is None:
        return False, "the General Scan engine is not available in this app"
    if _scan_running(hooks):
        return False, "a General Scan is already running"
    (hooks["dispatch"] or (lambda f: f()))(hooks["start"])
    t0 = time.time()
    while not _scan_running(hooks):
        if ctx.stop_requested():
            return False, "stopped before the scan started"
        if time.time() - t0 > START_WAIT_S:
            return False, "the General Scan did not start (see the log)"
        time.sleep(POLL_S)
    while _scan_running(hooks):      # stop/skip abort it through the engine's abort hook
        time.sleep(POLL_S)
    if ctx.stop_requested():
        return False, "scan aborted"
    errs = sorted(_scan_list(hooks, "last_errors"), key=lambda e: DIGITIZER_DEAD not in e)   # keeps it in the note
    warns = _scan_list(hooks, "last_warnings")
    skipped = _scan_list(hooks, "last_skipped")
    if skipped:
        return False, f"wavelength(s) skipped: {', '.join(skipped)}" + "".join(
            f"; {len(x)} {what}" for x, what in ((errs, "point(s) failed"), (warns, "warning(s)")) if x)
    if errs:
        return False, f"{len(errs)} point(s) failed: " + "; ".join(errs)[:300]
    if warns:
        return True, f"scan completed; {len(warns)} warning(s): " + "; ".join(warns)[:300]
    return True, "scan completed"


def _laser_sequence_path():
    return os.path.join(env.DAQ_DIR, "laser_sequence.json")


def _wavelength_check(wls):
    """-> (laser_sequence.json data, error) for a wavelength_scan; reads only."""
    if not wls:
        return None, "No wavelength selected"
    try:
        with open(_laser_sequence_path(), encoding="utf-8") as f:
            original = json.load(f)
    except (OSError, ValueError) as e:
        return None, f"Cannot read laser_sequence.json: {e}"
    missing = [w for w in wls if w not in original]
    if missing:
        return None, f"{', '.join(missing)} is not set in the Laser Sequence"
    for w in wls:
        try:
            float(original[w].get("bias", "")), float(original[w].get("pulse", ""))
        except (TypeError, ValueError):
            return None, f"{w}: Bias or Pulse current is not a valid number in Laser Sequence"
    from core.quick_setup import lasers_unavailable
    dead = [w for w in wls if w in lasers_unavailable()]
    if dead:
        return None, f"Laser not connected: {', '.join(dead)}"
    return original, None


def run_wavelength_scan(step, ctx):
    """Older presets: General Scan with only the step's wavelengths checked in the Laser Sequence."""
    return _with_laser_sequence(step, ctx, list((step.get("options") or {}).get("wavelengths") or []),
                                run_general_scan)


def _with_laser_sequence(step, ctx, wls, fn):
    """fn with only wls checked in the Laser Sequence (bias/pulse kept); the panel is restored after."""
    original, err = _wavelength_check(wls)
    if err:
        return False, err
    path = _laser_sequence_path()
    seq = json.loads(json.dumps(original))
    for w, v in seq.items():
        if isinstance(v, dict):
            v["on"] = w in wls
    restore = os.path.join(_dir(), RESTORE_LS)
    ok, note = False, "scan not run"
    try:
        _write(restore, {"path": path, "data": original})
        _write(path, seq)
        _log(f"[INFO] Routine: Laser Sequence set to {' + '.join(wls)} for '{step.get('name')}'.")
        ok, note = fn(step, ctx)
    finally:
        try:
            _write(path, original)
            _log("[INFO] Routine: Laser Sequence restored.")
            if os.path.exists(restore):
                os.remove(restore)
        except OSError as e:
            _log(f"[ERROR] Routine: could not restore laser_sequence.json: {e}")
    return ok, f"{' + '.join(wls)}: {note}"


def qe_cycles(opts):
    """Acquisitions per wavelength of a stability_qe step: "cycles", or from an older preset's length + unit."""
    from core import stability as stab
    if opts.get("cycles") not in (None, ""):
        return int(opts["cycles"])
    length, unit = float(opts.get("length", 1)), str(opts.get("unit", "h"))
    return stab.block_count(length, unit, float(opts.get("events", 300000)), float(opts.get("interval_s", 60)),
                            opts.get("trigger_hz"))


def qe_until(opts, until, now):
    """Options of a stability_qe step with "cycles" set to what fits before `until` when "until_next_step" is on."""
    from core import stability as stab
    if not opts.get("until_next_step") or until is None:
        return opts
    ev, iv, hz = float(opts.get("events", 300000)), float(opts.get("interval_s", 60)), opts.get("trigger_hz")
    n_wl = max(1, len(opts.get("wavelengths") or []))
    window = (until - now).total_seconds() - QE_UNTIL_MARGIN_S
    n = 0
    while n < 5000 and stab.plan_total([(("wl", 0.0, 0.0), n + 1, iv)] * n_wl, ev, hz) <= window:
        n += 1
    return dict(opts, cycles=n)


def _qe_plan(opts):
    """[(block, count, interval)] of a stability_qe step; bias/pulse from the Stability or Laser Sequence."""
    from core import stability as stab
    from core.scan import SEQ_ORDER
    saved, panel = stab.load_sequence(), stab.laser_panel_values()
    ev = float(opts.get("events", 300000))
    iv = float(opts.get("interval_s", 60))
    count = qe_cycles(opts)
    plan = []
    for wl in [w for w in SEQ_ORDER if w in (opts.get("wavelengths") or [])]:
        src = saved.get(wl) or {}
        bias, pulse = src.get("bias"), src.get("pulse")
        if bias in (None, "") or pulse in (None, ""):
            bias, pulse = panel.get(wl, ("", ""))
        try:
            block = (wl, float(bias), float(pulse))
        except (TypeError, ValueError):
            return None
        if count <= 0:
            return None
        plan.append((block, count, iv))
    return plan


def _qe_check(opts):
    """-> (plan, error) for a stability_qe step; reads only."""
    from core import stability as stab
    from core.laser import LD_TOTAL_CURRENT_LIMIT_MA
    from core.quick_setup import lasers_unavailable
    plan = _qe_plan(opts)
    if not plan:
        return None, "No valid wavelength configuration (check wavelengths, Bias/Pulse, or cycles)"
    for (wl, bias, pulse), _c, _i in plan:
        if bias + pulse > LD_TOTAL_CURRENT_LIMIT_MA:
            return None, f"{wl}: total Bias + Pulse current exceeds {LD_TOTAL_CURRENT_LIMIT_MA:.0f} mA limit"
    dead = [b[0] for b, _c, _i in plan if b[0] in lasers_unavailable()]
    if dead:
        return None, f"Laser not connected: {', '.join(dead)}"
    if stab.hk_backend():
        return None, "HK digitizer manual runs are not available in v2 yet"
    return plan, None


def run_stability_qe(step, ctx):
    """Laser Stability Run (QE/Gain) at the current stage angles, one wavelength block after another."""
    from core import stability as stab
    opts = qe_until(step.get("options") or {}, ctx.until, ctx.now())
    if opts.get("until_next_step") and ctx.until is not None:
        if int(opts["cycles"]) < 1:
            return None, "no stability cycle fits before the next step"
        _log(f"Routine: '{step.get('name')}' runs until the next step: {int(opts['cycles'])} cycle(s) per wavelength.")
    plan, err = _qe_check(opts)
    if err:
        return False, err
    try:
        _rows, block, _warn = stab.check_hv(env.read_config())
        if block:
            return False, "; ".join(block)
    except Exception as e:
        _log(f"[WARNING] Routine: HV pre-flight unavailable ({e}); stability run goes ahead.")
    ev = float(opts.get("events", 300000))
    extra = {"DAQ_EVENTS": str(int(ev))}
    if opts.get("samples"):
        extra["DAQ_TIME_WINDOW"] = str(int(opts["samples"]))
    ok, msg = stab.RUN.start(env.config_file(), ev, plan, "laser", "root", _stage_angles(), extra_env=extra)
    if not ok:
        return False, msg
    while stab.RUN.running:
        if ctx.stop_requested():
            stab.RUN.stop()
        time.sleep(POLL_S)
    if ctx.stop_requested():
        return False, "stability run stopped"
    text = stab.RUN.status[0]
    if getattr(stab.RUN, "last_code", None) not in (None, 0) or text.startswith("Stopped"):
        return False, text
    return True, ", ".join(f"{b[0]} x{c}" for b, c, _i in plan)


def _abort_general_scan():
    from core import schedule
    hooks = schedule._hooks
    if hooks["abort"] is not None:
        (hooks["dispatch"] or (lambda f: f()))(hooks["abort"])


def _stage_angles():
    try:
        from core.stage import stage
        angles = {d: stage().read_now(d) for d in (2, 3)}
        return {d: a for d, a in angles.items() if a[0] is not None}
    except Exception:
        return {}


def _lasers_off(ctx):
    """Switch every LD off for a dark run. -> the ones still on."""
    from core import quick_setup
    ctl = quick_setup.laser_controller()
    if ctl is None:
        return []
    on = ctl.lasers_on()
    for wl in on:
        ctl.set_ld(wl, False)
        _log(f"[INFO] Routine: {wl} LD switched OFF for the dark run.")
    t0 = time.time()
    while on and time.time() - t0 < LASER_OFF_WAIT_S and not ctx.stop_requested():
        time.sleep(POLL_S)
        on = ctl.lasers_on()
    return on


def _dark_check(opts):
    """-> (dark steps, error) for a dark_rate step; reads only."""
    from core import stability as stab
    steps = _dark_steps(opts)
    if not steps:
        return None, "Dark sequence: no valid channel selected"
    if not stab.dark_backend_ready():
        return None, "script_v7.sh does not support per-run dark settings"
    if stab.hk_backend():
        return None, "Dark mode is supported on the CAEN digitizer only"
    return steps, None


def run_dark_rate(step, ctx):
    from core import stability as stab
    opts = step.get("options") or {}
    steps, err = _dark_check(opts)
    if err:
        return False, err
    period = float(opts.get("every_min", 15)) * 60.0
    cycles = dark_cycles(opts, steps, ctx.until, ctx.now())
    if cycles < 1:
        return None, "no dark cycle fits before the next step"
    try:
        _rows, block, _warn = stab.check_hv(env.read_config())
        if block:
            return False, "; ".join(block)
    except Exception as e:
        _log(f"[WARNING] Routine: HV pre-flight unavailable ({e}); dark run goes ahead.")
    still_on = _lasers_off(ctx)
    if still_on:
        return False, f"laser still ON: {', '.join(still_on)}"
    _log(f"[INFO] Routine: dark self-trigger threshold {dark_thr(opts):g} mV.")
    ok, msg = stab.RUN.dark.start(steps, cycles, period, "root", _stage_angles())
    if not ok:
        return False, msg
    while stab.RUN.running:
        if ctx.stop_requested():
            stab.RUN.stop()
        time.sleep(POLL_S)
    if ctx.stop_requested():
        return False, "dark run stopped"
    text = stab.RUN.status[0]
    if getattr(stab.RUN, "last_code", None) not in (None, 0) or text.startswith("Stopped"):
        return False, text
    return True, f"{cycles} cycle(s) every {period / 60:g} min, {len(steps)} run(s) each"


def _abort_dark_rate():
    from core import stability
    stability.RUN.stop()


def _hv_apply_targets(opts, prev=None, fit=None):
    """-> ([(ch, V)], error) of an hv_apply step; reads only."""
    mode = opts.get("mode", "fixed")
    if mode == "gain_curve":
        p = gain_curve_plan(opts, prev, path=fit)
        return (None, "; ".join(p["errors"])) if p["errors"] else (p["items"], None)
    if mode != "fixed":
        return None, f"unknown HV apply mode '{mode}'"
    volts = opts.get("volts") or {}
    if not volts:    # the nominal HV1..3 of the config header (ch0..2)
        cfg = env.read_config()
        volts = {str(ch): cfg.get(f"HV{ch + 1}") for ch in range(3)}
    try:
        items = sorted((int(ch), float(v)) for ch, v in volts.items() if v not in (None, ""))
    except (TypeError, ValueError) as e:
        return None, f"bad volts: {e}"
    if not items:
        return None, "no channel voltage to apply"
    return items, None


HV_FALLBACK_NOTE = "gain curve unusable"   # also in WARN_NOTES


def _fallback_targets(opts):
    """Configured (header) HV of the channels the gain curve would set. -> ([(ch, V)], error)."""
    cfg = env.read_config()
    try:
        f = _state.get("hv_fit") or latest_gain_file()
        scanned = _fit_channels(read_gain_json(f)[0]) if f else [0, 1, 2]
    except (OSError, ValueError, KeyError, TypeError):
        scanned = [0, 1, 2]
    try:
        return [(ch, float(cfg.get(f"HV{ch + 1}"))) for ch in scanned], None
    except (TypeError, ValueError) as e:
        return None, f"Configured HV unreadable: {e}"


def _keep(key, value):
    """Runtime value kept in _current.json for the restart report (None removes it)."""
    with _lock:
        if _routine is None:
            return
        if value is None:
            _routine.pop(key, None)
        else:
            _routine[key] = value
        _changed()


def _note_restore(ch, v):
    _state.setdefault("hv_restore", {})[ch] = v
    _keep("hv_restore", {str(c): x for c, x in _state["hv_restore"].items()})


def _routine_fit():
    """This routine run's HV scan fit, its PMT serials checked against the config header. -> (path, error)."""
    path = _state.get("hv_fit")
    if not path:
        return None, "no HV scan fit from this routine run"
    try:
        d, _fits = read_gain_json(path)
        with open(os.path.join(env.ADC_DIR, d.get("manifest") or ""), encoding="utf-8") as f:
            sn = json.load(f).get("sn") or {}
    except (OSError, ValueError, KeyError, TypeError) as e:
        return None, f"{os.path.basename(path)} or its manifest unreadable: {e}"
    cfg = env.read_config()
    bad = [f"Ch{ch} fit {sn.get(str(ch)) or '?'} vs config3.h {cfg.get(f'SN{ch + 1}') or '?'}"
           for ch in _fit_channels(d)
           if not sn.get(str(ch)) or str(sn.get(str(ch))).strip() != str(cfg.get(f"SN{ch + 1}") or "").strip()]
    if bad:
        return None, "PMT serials of the fit do not match: " + ", ".join(bad)
    return path, None


def _settle_after_skip(ch, rid):
    """Skip during an HV apply: the crate keeps ramping, so wait for VMon to settle (Stop ends the wait). -> note."""
    from core import hv_request
    _log(f"[WARNING] Routine: HV apply skipped during Ch{ch}; waiting for Ch{ch} VMon to settle before the next step.")
    r = hv_request.wait_settled(rid, gain_delay_s=0, skip_gain_delay=True, cancel=lambda: _state["stop"])
    note = r.get("note") or r.get("state")
    _log(f"[{'INFO' if r.get('ok') else 'WARNING'}] Routine: HV apply Ch{ch} after the skip: {note}.")
    return f"skipped during Ch{ch}; {note}"


def run_hv_apply(step, ctx):
    from core import hv_request
    opts = step.get("options") or {}
    if opts.get("mode") == "gain_curve":
        fit, err = _routine_fit()
        if err:
            _log(f"[WARNING] Routine: HV apply: {err}.")
        items, err = (None, err) if err else _hv_apply_targets(opts, fit=fit)
    else:
        items, err = _hv_apply_targets(opts)
    fallback = None
    if err and opts.get("mode") == "gain_curve" and opts.get("on_unusable", "configured") == "configured":
        fallback = err
        items, err = _fallback_targets(opts)
        if not err:
            _log(f"[WARNING] Routine: {HV_FALLBACK_NOTE} ({fallback}); keeping the Configured HV: "
                 + ", ".join(f"Ch{ch} {v:.0f} V" for ch, v in items))
            _send("HV Apply: gain curve unusable", f"{step.get('name')}: {fallback}\nThe routine continues at the "
                  "Configured HV: " + ", ".join(f"Ch{ch} {v:.0f} V" for ch, v in items), "warning", "routine_hv_fallback")
    if err:
        return False, err
    if opts.get("mode") == "gain_curve" and fallback is None:
        _log("[INFO] Routine: HV apply from the gain curve: " + ", ".join(f"Ch{ch} {v:.1f} V" for ch, v in items))
    gain_delay = float(opts.get("gain_delay_s", hv_request.GAIN_DELAY_S))
    restore = None
    if opts.get("restore_hv", True):
        try:
            cfg = env.read_config()
            restore = {ch: float(cfg.get(f"HV{ch + 1}")) for ch, _v in items}
        except (TypeError, ValueError) as e:
            _log(f"[WARNING] Routine: header HV unreadable ({e}); HV is not restored at the routine end.")
    notes = []
    for i, (ch, v) in enumerate(items):
        if ctx.stop_requested():
            return False, "stopped by operator"
        try:
            rid = hv_request.post(ch, v, who="routine", reason=step.get("name") or "daily routine")
        except hv_request.HVRequestError as e:
            return False, f"Ch{ch}: {e}"
        if restore is not None:   # per channel: a stop or failure after this post still restores it
            _note_restore(ch, restore[ch])
        r = hv_request.wait_settled(rid, gain_delay_s=gain_delay, skip_gain_delay=i < len(items) - 1,
                                    cancel=ctx.stop_requested)
        if not r.get("ok") and _state["skip"] and not _state["stop"]:
            return False, _settle_after_skip(ch, rid)
        if not r.get("ok"):
            return False, f"Ch{ch}: {r.get('note') or r.get('state')}"
        notes.append(f"Ch{ch} {v:.0f} V")
    if fallback is not None:
        return True, f"{HV_FALLBACK_NOTE} ({fallback}); Configured HV kept: " + ", ".join(notes)
    return True, ", ".join(notes)


_restore_thread = None


def _restore_hv(volts):
    """Header HV back on the channels an HV apply changed; not cancellable, in its own thread."""
    items = sorted(volts.items())
    _log("[INFO] Routine: restoring the header HV (Hamamatsu gain): " + ", ".join(f"Ch{ch} {v:.0f} V" for ch, v in items))
    _vm, err = _hv_set(items, "routine end: restore header HV", 0, None, [], retry_s=RESTORE_RETRY_S)
    if err:
        _log(f"[ERROR] Routine: header HV restore failed: {err}")
        from core.stability import _notify
        _notify("Routine: header HV restore FAILED", f"{err}\nCheck the HV Monitor before the next run.", "warning")
    else:
        _log("[INFO] Routine: header HV restored.")


def _end_actions(volts, home):
    """Routine end, in its own thread: header HV back, then both stages to the origin."""
    if volts:
        _restore_hv(volts)
        with _lock:   # a failed restore was reported by _restore_hv
            _routine.pop("hv_restore", None)
            _persist()
    if home:
        _home_stage()


def _home_stage():
    am = _scan_engine()
    if am is None:
        return
    if getattr(am, "is_running", False):
        _log("[WARNING] Routine: stages not moved to the origin at the end: a General Scan is running.")
        return
    _log("[INFO] Routine: end of routine, moving both stages to the origin (tilt 0, then rotation 0).")
    try:
        angles, err = _tilt_zero(types.SimpleNamespace(stop_requested=lambda: False), "routine end")
        if not err:
            angles, err = _rot_zero(am)
    except Exception as e:
        angles, err = None, f"{type(e).__name__}: {e}"
    if err:
        _log(f"[ERROR] Routine: stages to the origin at the end failed: {err}")
    else:
        _log("[INFO] Routine: stages at the origin (" + ", ".join(
            f"Dev{d} tilt {angles[d][0]:.2f} rot {angles[d][1]:.2f} deg" for d in sorted(angles)) + ").")


ORIGIN_BEFORE = ("dark_rate", "stability_qe")         # measured at the stage's current angle
ORIGIN_AFTER = ("general_scan", "hk_scan", "wavelength_scan")    # every scan starts from the origin
ENGINE_IDLE_WAIT_S = 120


def _to_origin(ctx, why):
    """Both stages to tilt 0, then rotation 0, unless already there. -> error text or None.
    Without stage control (no hardware, no scan engine) nothing is moved."""
    if env.NO_HW:
        return None
    am = _scan_engine()
    if am is None:
        return None
    t0 = time.time()
    while getattr(am, "is_running", False) or getattr(am, "reset_in_progress", False):
        if ctx.stop_requested():
            return "stopped"
        if time.time() - t0 > ENGINE_IDLE_WAIT_S:
            return "the scan engine is still busy"
        time.sleep(1.0)
    angles = _stage_angles()
    if all(d in angles and abs(angles[d][0]) <= TILT_TOL_DEG and abs(angles[d][1]) <= TILT_TOL_DEG for d in (2, 3)):
        return None
    _log(f"[INFO] Routine: {why}: moving both stages to the origin (tilt 0, then rotation 0).")
    angles, err = _tilt_zero(ctx, why)
    if not err:
        if ctx.stop_requested():
            return "stopped"
        angles, err = _rot_zero(am)
    if not err:
        _log("[INFO] Routine: stages at the origin (" + ", ".join(
            f"Dev{d} tilt {angles[d][0]:.2f} rot {angles[d][1]:.2f} deg" for d in sorted(angles)) + ").")
    return err


def _rot_zero(am):
    """Both stages to rotation 0 after tilt 0 (Reset Angle phase 2), then the readback. -> (angles, error)."""
    _hv_scan["stage"] = am
    try:
        ok = am._move_safely_stepped(0.0, 0.0, "rot", bypass_check=True)
        if ok:
            for d in (2, 3):
                am._wait_for_physical_angle(d, target_rot=0.0, bypass_check=True)
    finally:
        _hv_scan["stage"] = None
        am._reset_cancel = False
    if not ok:
        return None, "rotation 0 move did not complete (see the log)"
    angles = _stage_angles()
    bad = [f"Dev{d} rot {angles[d][1]:.2f} deg" if d in angles else f"Dev{d} no readback"
           for d in (2, 3) if d not in angles or abs(angles[d][1]) > TILT_TOL_DEG]
    if bad:
        return None, "rotation 0 readback check failed: " + ", ".join(bad)
    return angles, None


def hv_restore_active():
    """True while the end-of-routine thread (header HV, stages to the origin) runs."""
    return _restore_thread is not None and _restore_thread.is_alive()


_hv_scan = {"stage": None}    # the scan engine while it moves the stages for an HV scan


def _tilt_zero(ctx, why="HV Scan"):
    """Both stages to tilt 0 with the scan engine's stepped move (Reset Angle phase 1, rotation kept),
    then the readback. -> (angles, error)."""
    host = getattr(sys.modules.get("core.scan"), "_host", None)
    am = getattr(host, "auto_mgr", None)
    if am is None:
        return None, "stage control is not available (no scan engine)"
    if getattr(am, "reset_in_progress", False):
        return None, "a Reset Angle is in progress"
    _log(f"[INFO] Routine: {why} moving both stages to tilt 0.")
    _hv_scan["stage"] = am
    try:
        ok = am._move_safely_stepped(0.0, 0.0, "tilt", bypass_check=True, step_override=am.safe_move_step)
        if ok and not ctx.stop_requested():
            for d in (2, 3):
                am._wait_for_physical_angle(d, target_tilt=0.0, bypass_check=True)
    finally:
        _hv_scan["stage"] = None
        am._reset_cancel = False
    if ctx.stop_requested():
        return None, "stopped during the tilt 0 move"
    if not ok:
        return None, "tilt 0 move did not complete (see the log)"
    angles = _stage_angles()
    bad = [f"Dev{d} tilt {angles[d][0]:.2f} deg" if d in angles else f"Dev{d} no readback"
           for d in (2, 3) if d not in angles or abs(angles[d][0]) > TILT_TOL_DEG]
    if bad:
        return None, "tilt 0 readback check failed: " + ", ".join(bad)
    return angles, None


def _hv_set(volts, reason, gain_delay, cancel, posted, retry_s=0.0):
    """Post and settle [(ch, V)] in turn, the gain delay once after the last. -> ({ch: VMon}, error or None)."""
    from core import hv_request
    vmon = {}
    for i, (ch, v) in enumerate(volts):
        for w in _hv_walk(ch, v):   # intermediate VSets so no request exceeds the HV Monitor's max step
            try:
                rid = hv_request.post(ch, w, who="routine", reason=reason + " (step)")
            except hv_request.HVRequestError as e:
                return vmon, f"Ch{ch}: {e}"
            posted.append(ch)
            r = hv_request.wait_settled(rid, gain_delay_s=0, skip_gain_delay=True, cancel=cancel)
            if not r.get("ok"):
                return vmon, f"Ch{ch}: {r.get('note') or r.get('state')}"
        end = time.time() + retry_s
        while True:
            try:
                rid = hv_request.post(ch, v, who="routine", reason=reason)
                break
            except hv_request.HVRequestError as e:
                if time.time() >= end:
                    return vmon, f"Ch{ch}: {e}"
                time.sleep(POLL_S)
        posted.append(ch)
        r = hv_request.wait_settled(rid, gain_delay_s=gain_delay, skip_gain_delay=i < len(volts) - 1,
                                    cancel=cancel)
        if not r.get("ok"):
            return vmon, f"Ch{ch}: {r.get('note') or r.get('state')}"
        vmon[str(ch)] = r.get("vmon")
    return vmon, None


def _walk_points(start, v, step):
    out = []
    while step > 0 and abs(v - start) > step:   # step is already 10 V below the limit
        start += step if v > start else -step
        out.append(round(start, 1))
    return out


def _hv_walk(ch, v):
    """Intermediate VSets from the channel's VMon to v (none when the limits or VMon are unknown)."""
    lim = _hv_limits()
    cur, _age = _vmon_now(ch)
    if lim is None or cur is None:
        return []
    return _walk_points(cur, v, float(lim.get("max_step_v", 250)) - 10.0)


def _registry_rows():
    """Data rows in the run registry now (it is append-only)."""
    try:
        with open(env.REGISTRY, encoding="utf-8") as f:
            return max(0, sum(1 for _ in f) - 1)
    except OSError:
        return 0


def _registry_set_hv(since, hv):
    """hv1..3 of the hvscan rows after the first `since` data rows (script_v7.sh records the header HV),
    under script_v7.sh's registry lock. -> [run]."""
    import csv
    import fcntl
    path = env.REGISTRY
    with open(path + ".lock", "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        head = rows[0] if rows else []
        col = {k: head.index(k) for k in ("run", "kind", "hv1", "hv2", "hv3") if k in head}
        if len(col) < 5:
            return []
        runs = []
        for r in rows[1 + since:]:
            if len(r) == len(head) and r[col["kind"]] == HVSCAN_KIND:
                for ch in range(3):
                    r[col[f"hv{ch + 1}"]] = f"{hv[ch]:g}"
                runs.append(r[col["run"]])
        if runs:
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "w", newline="", encoding="utf-8") as f:
                csv.writer(f, lineterminator="\n").writerows(rows)
            os.replace(tmp, path)
    return runs


HVSCAN_WL = "405nm"


def laser_values(wl):
    """(bias, pulse) mA of wl from the Stability Sequence, else the General Scan Laser Sequence panel; None if not numbers."""
    from core import stability as stab
    src = stab.load_sequence().get(wl) or {}
    bias, pulse = src.get("bias"), src.get("pulse")
    if bias in (None, "") or pulse in (None, ""):
        bias, pulse = stab.laser_panel_values().get(wl, ("", ""))
    try:
        return float(bias), float(pulse)
    except (TypeError, ValueError):
        return None


def step_lasers(step):
    """[(wl, bias, pulse)] a laser step uses, from the file its executor reads; bias/pulse None when not numbers."""
    from core import stability as stab
    from core.scan import SEQ_ORDER
    k, o = step.get("kind"), step.get("options") or {}
    if k in ("general_scan", "hk_scan") and not scan_wavelength(o):
        return list(stab.laser_sequence() or [])
    if k in ("general_scan", "hk_scan", "wavelength_scan"):
        panel = stab.laser_panel_values()
        out = []
        for wl in _engine_order([scan_wavelength(o)] if k != "wavelength_scan" else o.get("wavelengths") or []):
            try:
                out.append((wl, float(panel[wl][0]), float(panel[wl][1])))
            except (KeyError, TypeError, ValueError):
                out.append((wl, None, None))
        return out
    if k in ("stability_qe", "hv_scan"):
        wls = (o.get("wavelengths") or []) if k == "stability_qe" else [o.get("wavelength") or HVSCAN_WL]
        return [(wl,) + (laser_values(wl) or (None, None)) for wl in SEQ_ORDER if wl in wls]
    return []


def _laser_line(step):
    """Pre-run line 'Laser: 375 nm bias 0 / pulse 118 mA, ...' of a laser step; [] when it uses none."""
    try:
        ls = step_lasers(step)
    except Exception as e:
        return [("warn", f"Laser currents unreadable: {e}")]
    if not ls:
        return []
    return [("ok", "Laser: " + ", ".join(f"{wl[:-2]} nm bias {b:g} / pulse {p:g} mA" if p is not None
                                         else f"{wl[:-2]} nm current unknown" for wl, b, p in ls))]


def laser_block(wl):
    """-> ((wl, bias, pulse), error) from the Stability Sequence, else the General Scan Laser Sequence panel."""
    from core.quick_setup import lasers_unavailable
    v = laser_values(wl)
    if v is None:
        return None, f"{wl}: Bias or Pulse is not a number in the Laser Sequence"
    block = (wl,) + v
    if wl in lasers_unavailable():
        return None, f"Laser not connected: {wl}"
    return block, None


def _hv_scan_run(events, angles, ctx):
    """One laser run of `events` with the laser the HV scan switched on (checked first). -> (ok, note, since)."""
    from core import stability as stab
    block = _hv_scan.get("block")
    on = _laser_state()
    if block is not None and on is not None and block[0] not in on:
        return False, f"{block[0]} LD is off", None
    since = _registry_rows()
    ok, msg = stab.RUN.start(env.config_file(), events, [(None, 1, 0.0)], "laser", "root", angles,
                             extra_env={"DAQ_EVENTS": str(events), "DAQ_RUN_KIND": HVSCAN_KIND}, base=HVSCAN_BASE)
    if not ok:
        return False, msg, None
    while stab.RUN.running:
        if ctx.stop_requested():
            stab.RUN.stop()
        time.sleep(POLL_S)
    if ctx.stop_requested():
        return False, "laser run stopped", since
    text = stab.RUN.status[0]
    if getattr(stab.RUN, "last_code", None) not in (None, 0) or text.startswith("Stopped"):
        return False, text, since
    return True, "", since


def _hv_scan_points(name, ctx, offsets, base, events, settle, angles, man, save, posted, chans=(0, 1, 2)):
    for off in offsets:
        if ctx.stop_requested():
            return False, "stopped by operator"
        hv = [b + off if ch in chans else b for ch, b in enumerate(base)]
        pt = {"offset_v": off, "hv": {str(ch): hv[ch] for ch in range(3)}, "vmon": {}, "runs": [],
              "status": "running"}
        man["points"].append(pt)
        save()
        vmon, err = _hv_set([(ch, hv[ch]) for ch in chans], f"{name}: offset {off:+g} V", settle,
                            ctx.stop_requested, posted)
        pt["vmon"] = vmon
        if err:
            pt["status"] = "stopped" if ctx.stop_requested() else "hv failed"
            save()
            return False, "stopped by operator" if ctx.stop_requested() else f"offset {off:+g} V: {err}"
        ok, note, since = _hv_scan_run(events, angles, ctx)
        if since is not None:
            try:
                pt["runs"] = _registry_set_hv(since, hv)
            except Exception as e:
                _log(f"[WARNING] Routine: HV scan registry update failed: {e}")
            if not pt["runs"]:
                _log(f"[WARNING] Routine: HV scan offset {off:+g} V: no {HVSCAN_KIND} row in the run registry.")
        pt["status"] = "done" if ok else "stopped" if ctx.stop_requested() else "run failed"
        save()
        if not ok:
            return False, "stopped by operator" if ctx.stop_requested() else f"offset {off:+g} V: {note}"
    runs = [r for p in man["points"] for r in p["runs"]]
    return True, f"{len(offsets)} point(s), run(s) {', '.join(runs) or 'none registered'}"


HVSCAN_CHANNELS = ("rot1", "rot2")      # default: the Monitor PMT keeps its HV
CH_KEYS = {"mon": 0, "rot1": 1, "rot2": 2}


def hv_scan_channels(opts):
    """HV channels an hv_scan step changes, from options["channels"] (keys mon / rot1 / rot2)."""
    keys = opts.get("channels")
    keys = HVSCAN_CHANNELS if keys is None else keys
    return sorted(CH_KEYS[k] for k in keys if k in CH_KEYS)


def _hv_scan_options(opts):
    """-> (offsets, events, settle, cfg, base, error) of an hv_scan step; reads only."""
    from core import stability as stab
    bad = (None,) * 5
    try:
        offsets = [float(v) for v in opts.get("offsets_v") or []]
        events = int(opts.get("events", 100000))
        settle = float(opts.get("settle_s", 180))
    except (TypeError, ValueError) as e:
        return bad + (f"Invalid options: {e}",)
    if not offsets:
        return bad + ("No HV offset specified",)
    if not hv_scan_channels(opts):
        return bad + ("No PMT selected for the HV scan",)
    if events <= 0:
        return bad + ("Event count must be positive",)
    cfg = env.read_config()
    try:   # the nominal HV1..3 of the config header (ch0..2)
        base = [float(cfg.get(f"HV{ch + 1}")) for ch in range(3)]
    except (TypeError, ValueError):
        return bad + ("config3.h HV1..3 must be numbers",)
    if stab.hk_backend():
        return bad + ("HV Scan is supported on the CAEN digitizer only",)
    return offsets, events, settle, cfg, base, None


def run_hv_scan(step, ctx):
    from core import stability as stab
    opts = step.get("options") or {}
    name = step.get("name") or "HV Scan"
    _state.pop("hv_fit", None)   # an HV apply after this step uses only this scan's fit
    offsets, events, settle, cfg, base, err = _hv_scan_options(opts)
    if err:
        return False, err
    lblock, err = laser_block(opts.get("wavelength") or HVSCAN_WL)
    if err:
        return False, err
    _hv_scan["block"] = lblock
    try:
        _rows, block, _warn = stab.check_hv(cfg)
        if block:
            return False, "; ".join(block)
    except Exception as e:
        _log(f"[WARNING] Routine: HV pre-flight unavailable ({e}); HV scan goes ahead.")
    t0 = ctx.now()
    path = os.path.join(HVSCAN_DIR, f"{t0:%Y%m%d}", f"hvscan_{t0:%H%M}.json")
    chans = hv_scan_channels(opts)
    man = {"step": name, "started": _stamp(t0), "finished": None, "status": "running", "events": events,
           "channels": chans, "sn": {str(ch): cfg.get(f"SN{ch + 1}") for ch in range(3)},
           "settle_s": settle, "base_hv": {str(ch): base[ch] for ch in range(3)}, "angles": {}, "points": [],
           "restore": None, "note": ""}
    _hv_scan["manifest"] = man       # the progress views read the point count

    def save():
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            _write(path, man)
        except OSError as e:
            _log(f"[WARNING] Routine: HV scan manifest not written: {e}")

    save()
    angles, err = _tilt_zero(ctx)
    if err:
        man.update(finished=_stamp(ctx.now()), status="failed", note=err)
        save()
        return False, err
    man["angles"] = {str(d): {"tilt": a[0], "rot": a[1]} for d, a in angles.items()}
    posted = []
    ok, note = False, "interrupted"
    am = stab.RUN._engine()
    try:
        laser_ok = bool(am and am._prepare_laser_block(lblock, sleep=time.sleep))
    except Exception as e:
        _log(f"[ERROR] Routine: HV scan laser {lblock[0]} preparation failed: {e}")
        laser_ok = False
    if not laser_ok:
        man.update(finished=_stamp(ctx.now()), status="failed", note=f"laser {lblock[0]} not ready")
        save()
        return False, f"laser {lblock[0]} not ready (TEC/LD ON failed)"
    _log(f"[INFO] Routine: HV scan laser {lblock[0]} ON for all points.")
    man["laser"] = {"wavelength": lblock[0], "bias": lblock[1], "pulse": lblock[2]}
    _keep("hv_scan_restore", {str(ch): base[ch] for ch in chans})
    try:
        ok, note = _hv_scan_points(name, ctx, offsets, base, events, settle, angles, man, save, posted, chans)
    except Exception as e:
        ok, note = False, f"{type(e).__name__}: {e}"
    finally:
        try:
            am._finish_laser_block(lblock[0])
            _log(f"[INFO] Routine: HV scan laser {lblock[0]} OFF.")
        except Exception as e:
            _log(f"[WARNING] Routine: HV scan laser {lblock[0]} LD OFF failed: {e}")
        rest_err = None
        if posted:   # HV was touched: back to the header HV, not cancellable by Stop / Skip
            _log("[INFO] Routine: HV scan restoring the base HV"
                 + (" (step skipped: waiting for VMon to settle before the next step)." if _state["skip"] else "."))
            _vm, rest_err = _hv_set([(ch, base[ch]) for ch in chans], f"{name}: restore base HV", 0, None, [],
                                    retry_s=RESTORE_RETRY_S)
            man["restore"] = {"ok": rest_err is None, "note": rest_err or "base HV restored"}
            if rest_err:
                _log(f"[ERROR] Routine: HV scan base HV restore failed: {rest_err}")
                from core.stability import _notify
                _notify("HV Scan: base HV restore FAILED",
                        f"{name}: {rest_err}\nBase HV " + ", ".join(f"Ch{ch} {v:g} V" for ch, v in enumerate(base))
                        + "\nCheck the HV Monitor before the next run.", "warning")
            elif ok and not ctx.stop_requested():
                end = time.time() + settle   # gain settle at the base HV for the next step
                next_daq.wait_until(end)
                while time.time() < end and not ctx.stop_requested():
                    time.sleep(min(POLL_S, max(end - time.time(), 0)))
        if rest_err:
            ok, note = FAILED, f"{note}; base HV restore FAILED: {rest_err}"
        elif posted:
            note += "; base HV restored"
        if not rest_err:
            _keep("hv_scan_restore", None)
        man.update(finished=_stamp(ctx.now()), note=note,
                   status="done" if ok is True else "failed" if ok == FAILED or not ctx.stop_requested()
                   else "stopped")
        save()
    if ok is True and not ctx.stop_requested():   # suggestion only: nothing is applied from the fit here
        fit_ok, fit_note = run_hv_fit(path, opts, ctx)
        if not fit_ok:
            _log(f"[WARNING] Routine: HV scan {fit_note}.")
        note += f"; {fit_note}"
        man["fit"] = {"ok": fit_ok, "note": fit_note}
        save()
        if fit_ok:
            _state["hv_fit"] = path[:-5] + "_gain.json"
            note += f"; fit file {os.path.relpath(_state['hv_fit'], HVSCAN_DIR)}"
    return ok, note


def _abort_hv_scan():
    from core import stability
    am = _hv_scan["stage"]
    if am is not None:   # cancels the stepped move as Reset Angle's Cancel does
        am._reset_cancel = True
        for d in (2, 3):
            try:
                am.controller.rot_mgr.stop_rotation(d)
            except Exception as e:
                _log(f"[WARNING] Routine: stage {d} stop failed: {e}")
    stability.RUN.stop()
    p = _hv_fit["proc"]
    if p is not None:
        _kill_fit(p, wait=False)


# ---------------------------------------------------------------- gain curve: fit after the HV scan, targets for HV apply
_hv_fit = {"proc": None}    # the running fit macro


def hv_fit_command(manifest, mantissa, mode):
    """argv of the gain-curve fit of one HV scan manifest, run in env.ADC_DIR."""
    p = os.path.abspath(manifest)
    rel = os.path.relpath(p, env.ADC_DIR)
    arg = p if rel.startswith("..") else rel
    return ["nice", "-n", "19", "root", "-l", "-b", "-q", f'{GAIN_FIT_MACRO}+("{arg}", {mantissa:g}, {int(mode)})']


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def read_gain_json(path):
    """-> (document, {ch: channel dict}) of a hvscan_<HHMM>_gain.json."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    text = re.sub(r"(:\s*)-?(?:nan|inf)\b", r"\1null", text, flags=re.I)   # C++ stream output, not JSON
    d = json.loads(text)
    return d, {int(c["ch"]): c for c in d.get("channels") or []}


def latest_gain_file():
    """Newest hvscan_*_gain.json under HVSCAN_DIR/<date>/ by mtime, or None."""
    files = glob.glob(os.path.join(HVSCAN_DIR, "*", "hvscan_*_gain.json"))
    return max(files, key=os.path.getmtime) if files else None


def gain_target(c, mantissa, fit_mantissa):
    """-> (V, err or None, extrapolated) for a gain of mantissa x 10^7 from one channel of a _gain.json."""
    # G = a (V/1000)^b, V = 1000 (G/a)^(1/b) (HVScan_GainCurve.C); the macro's hv_target only at its own target
    vt = _num(c.get("hv_target"))
    if fit_mantissa is not None and abs(mantissa - fit_mantissa) < 1e-9 and vt is not None and vt > 0:
        return vt, _num(c.get("hv_target_err")), bool(c.get("extrapolated"))
    a, b = _num(c.get("a_1kV")), _num(c.get("b"))
    if a is None or b is None or a <= 0 or b <= 0:
        return None, None, False
    v = 1000.0 * (mantissa / a) ** (1.0 / b)
    lo, hi = _num(c.get("hv_min")), _num(c.get("hv_max"))
    return v, None, lo is not None and hi is not None and lo > 0 and not lo <= v <= hi


def _flags(c, extrap):
    return [f for f, on in (("provisional", c.get("provisional")), ("extrapolated", extrap)) if on]


def _fit_channels(d):
    """HV channels the fit's HV scan changed (from its manifest); all three for scans before the channel option."""
    try:
        with open(os.path.join(env.ADC_DIR, d.get("manifest") or ""), encoding="utf-8") as f:
            chans = json.load(f).get("channels")
    except (OSError, ValueError, TypeError):
        chans = None
    return sorted(int(c) for c in chans) if chans else [0, 1, 2]


def gain_curve_plan(opts, prev=None, now_ts=None, chans=None, path=None):
    """Targets of an hv_apply step in Gain curve mode from `path` (default the newest _gain.json); reads only.
    prev {ch: V} = start voltages of the step check (None: VMon now). -> {file, age_h, lines, items, errors}."""
    out = {"file": None, "age_h": None, "lines": [], "items": [], "errors": []}
    err = out["errors"].append
    try:
        m0 = float(opts.get("gain_mantissa", 1.0))
        per = {str(k): float(v) for k, v in (opts.get("gain_mantissa_ch") or {}).items() if v not in (None, "")}
        max_age = float(opts.get("max_age_h", 24))
    except (TypeError, ValueError, AttributeError) as e:
        err(f"bad options: {e}")
        return out
    if m0 <= 0 or any(v <= 0 for v in per.values()):
        err("target gain must be positive")
        return out
    path = path or latest_gain_file()
    if path is None:
        err(f"no hvscan_*_gain.json under {HVSCAN_DIR}")
        return out
    age = ((now_ts or time.time()) - os.path.getmtime(path)) / 3600.0
    out.update(file=path, age_h=age)
    out["lines"].append(f"fit file {os.path.relpath(path, HVSCAN_DIR)}, {age:.1f} h old")
    if age > max_age:
        err(f"{os.path.basename(path)} is {age:.1f} h old (max {max_age:g} h)")
    try:
        d, fits = read_gain_json(path)
    except (OSError, ValueError, KeyError, TypeError) as e:
        err(f"{os.path.basename(path)} unreadable: {e}")
        return out
    fit_m = _num(d.get("target_gain_1e7"))
    lim = _hv_limits()
    if lim is None:
        err("hv_request_limits missing in the HV Monitor config")
    accept = False   # a provisional fit (W not fixed) never sets HV
    scanned = _fit_channels(d) if chans is None else chans
    for ch in range(3):
        if ch not in scanned:
            out["lines"].append(f"Ch{ch}: not in the HV scan, HV unchanged")
            continue
        c, m = fits.get(ch), per.get(str(ch), m0)
        if c is None:
            err(f"Ch{ch}: not in the fit file")
            continue
        if not c.get("fitted"):
            err(f"Ch{ch}: not fitted" + (f" ({c['note']})" if c.get("note") else ""))
            continue
        v, ve, extrap = gain_target(c, m, fit_m)
        if v is None:
            err(f"Ch{ch}: no usable a_1kV / b in the fit file")
            continue
        w = _num(c.get("w_shared"))
        fl = _flags(c, extrap)
        out["lines"].append(f"Ch{ch}: {m:g} x 10^7 -> {v:.0f}" + (f" +- {ve:.0f}" if ve is not None else "")
                            + " V" + (f", W {w:.3f}" if w is not None and w >= 0 else ", W free")
                            + (f" ({', '.join(fl)})" if fl else ""))
        if c.get("provisional") and not accept:
            err(f"Ch{ch}: provisional fit (W not fixed), not accepted")
        if extrap:
            err(f"Ch{ch}: {v:.0f} V is extrapolated outside the scanned "
                f"{_num(c.get('hv_min')) or 0:.0f}-{_num(c.get('hv_max')) or 0:.0f} V")
        if lim is not None:
            start = prev.get(ch) if prev is not None else _vmon_now(ch)[0]
            for lv, t in _hv_path_lines(ch, [v], lim, start):
                (err if lv == "error" else out["lines"].append)(t)
        out["items"].append((ch, round(v, 1)))
    return out


def _fit_options(opts):
    """-> (mantissa, mode, timeout_s, error) of the fit after an hv_scan step."""
    try:
        m = float(opts.get("gain_mantissa", 1.0))
        mode = int(opts.get("fit_mode", 2))
        timeout = float(opts.get("fit_timeout_s", FIT_TIMEOUT_S))
    except (TypeError, ValueError) as e:
        return None, None, None, f"Invalid fit options: {e}"
    if m <= 0:
        return None, None, None, "Target gain must be positive"
    if mode not in FIT_MODES:
        return None, None, None, f"Fit mode {mode} must be 0, 1, or 2"
    if timeout <= 0:
        return None, None, None, "Fit timeout must be positive"
    return m, mode, timeout, None


def _kill_fit(proc, wait=True):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        if not wait:
            return
        try:
            proc.wait(5)
            return
        except subprocess.TimeoutExpired:
            pass


def run_hv_fit(manifest, opts, ctx):
    """The gain-curve macro on a finished HV scan manifest, in the calling (runner) thread. -> (ok, note)."""
    m, mode, timeout, err = _fit_options(opts)
    if err:
        return False, f"fit failed: {err}"
    cmd = hv_fit_command(manifest, m, mode)
    stem = manifest[:-5] if manifest.endswith(".json") else manifest
    gpath = stem + "_gain.json"
    _log(f"[INFO] Routine: HV fit started (mode {mode}): {shlex.join(cmd)}")
    t0 = time.time()
    try:
        with open(stem + "_fit.log", "w", encoding="utf-8") as lf:
            proc = subprocess.Popen(cmd, cwd=env.ADC_DIR, stdin=subprocess.DEVNULL, stdout=lf,
                                    stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as e:
        return False, f"fit failed: {e}"
    _hv_fit["proc"] = proc
    try:
        while proc.poll() is None:
            if ctx.stop_requested():
                _kill_fit(proc)
                return False, "fit stopped"
            if time.time() - t0 >= timeout:
                _kill_fit(proc)
                return False, f"fit failed: timeout after {timeout:g} s"
            time.sleep(POLL_S)
    finally:
        _hv_fit["proc"] = None
    if proc.returncode != 0:
        return False, f"fit failed: exit code {proc.returncode}"
    if not os.path.exists(gpath) or os.path.getmtime(gpath) < t0 - 1:
        return False, f"fit failed: {os.path.basename(gpath)} not written"
    try:
        d, chans = read_gain_json(gpath)
    except (OSError, ValueError, KeyError, TypeError) as e:
        return False, f"fit failed: {os.path.basename(gpath)} unreadable: {e}"
    fm = _num(d.get("target_gain_1e7")) or m
    parts = []
    for ch in range(3):
        c = chans.get(ch)
        if not c or not c.get("fitted"):
            why = (c or {}).get("note") or "missing"
            _log(f"[WARNING] Routine: HV fit ch{ch}: not fitted ({why})")
            parts.append(f"Ch{ch} not fitted")
            continue
        vt, vte, w = _num(c.get("hv_target")), _num(c.get("hv_target_err")), _num(c.get("w_shared"))
        fl = _flags(c, bool(c.get("extrapolated")))
        _log(f"[INFO] Routine: HV fit ch{ch}: a={_num(c.get('a_1kV')) or 0:.4g} b={_num(c.get('b')) or 0:.3f} "
             f"HV target {vt or 0:.0f} +- {vte or 0:.0f} V "
             f"({'W %.2f' % w if w is not None and w >= 0 else 'W free'}{''.join(', ' + f for f in fl)})")
        parts.append(f"Ch{ch} {vt or 0:.0f} V" + (f" ({', '.join(fl)})" if fl else ""))
    return True, f"suggested HV for {fm:g} x 10^7: " + ", ".join(parts)


# ---------------------------------------------------------------- dry run: what each step would do, no hardware
def _hv_limits():
    """hv_request_limits of the HV Monitor config, or None."""
    try:
        with open(HV_LIMITS_FILE, encoding="utf-8") as f:
            lim = json.load(f).get("hv_request_limits")
        return lim if isinstance(lim, dict) and lim.get("channels") else None
    except (OSError, ValueError, AttributeError):
        return None


def _ch_limits(lim, ch):
    c = (lim.get("channels") or {}).get(str(ch)) or {}
    return float(c.get("min", 0)), float(c.get("max", 2300))


def _hv_path_lines(ch, path, lim, start_v):
    """Range and step checks of one channel's planned VSet sequence, as the HV Monitor refuses them."""
    out = []
    lo, hi = _ch_limits(lim, ch)
    step = float(lim.get("max_step_v", 250))
    prev = start_v
    for v in path:
        if not lo <= v <= hi:
            out.append(("error", f"Ch{ch}: {v:.0f} V is outside safe limits ({lo:.0f} to {hi:.0f} V)"))
        if prev is not None and abs(v - prev) > step:
            n = len(_walk_points(prev, v, step - 10.0)) + 1
            out.append(("ok", f"Ch{ch}: {prev:.0f} -> {v:.0f} V in {n} steps (max step {step:.0f} V)"))
        prev = v
    return out


def _vmon_now(ch):
    """(VMon, age s) of the newest HV DB row, or (None, None)."""
    try:
        from core import hv_request
        t, v = hv_request._latest_vmon(ch, env.HV_DB)
        return (v, time.time() - t) if t is not None else (None, None)
    except Exception:
        return None, None


def _laser_state():
    """LDs ON now (cached status, read-only), or None when the state cannot be read."""
    try:
        from core import quick_setup
        ctl = quick_setup.laser_controller()
        if ctl is None or not getattr(ctl, "inst", None):
            return None
        return list(ctl.lasers_on())
    except Exception:
        return None


def _hv_preflight_lines():
    from core import stability as stab
    try:
        _rows, block, warn = stab.check_hv(env.read_config())
    except Exception as e:
        return [("warn", f"HV pre-flight check unavailable: {e}")]
    return [("error", b) for b in block] + [("warn", w) for w in warn]


def _backend_name():
    host = getattr(sys.modules.get("core.scan"), "_host", None)
    am = getattr(host, "auto_mgr", None)
    if am is not None:
        return ("HK" if getattr(am, "daq_backend", "caen") == "hk" else "CAEN"), "engine"
    try:
        from core import status
        b = status.daq_backend()
    except Exception:
        b = None
    return ("HK" if b == "HK Digitizer" else "CAEN"), ("Tk app" if b else "default")


def _scan_option_line(opts):
    try:
        points, axes = _grid(opts)
        src = "step angles" if scan_angles(opts)[0] else "engine grid" if _scan_engine() is not None else "options"
        return ("ok", f"Estimate: {points} points on {axes} axes ({src}), {_point_seconds(opts):.0f} s per point")
    except (TypeError, ValueError) as e:
        return ("error", f"Invalid options: {e}")


def _events_lines(opts):
    """Events and grid checks of a General / Wavelength scan step; reads only."""
    out = []
    cfg = _config_events()
    ev = opts.get("events")
    if _backend_name()[0] == "HK":
        out.append(("warn", "Event count: ignored by HK readout (controlled by hk_config acquisition time)"))
    elif not ev or int(ev) == cfg:
        out.append(("ok", f"Events: {cfg:,} per point (from config3.h)") if cfg else
                   ("warn", "Events: cannot read value from config3.h"))
    else:
        was = f"{cfg:,}" if cfg else "?"
        out.append(("ok", f"Events: {int(ev):,} per point for this scan (config3.h default {was} unchanged)"))
    angles, aerr = scan_angles(opts)
    if aerr:
        out.append(("error", aerr))
    elif angles:
        out.append(("ok", f"Angles: {len(angles)} per axis for this scan ({angles_text(angles)})"))
    am = _scan_engine()
    if am is not None and not angles:
        try:
            n = 2 * len(am.build_tilt_angles())
            if int(opts.get("points", n)) != n or int(opts.get("axes", 2)) != 2:
                out.append(("warn", f"Grid: scans {n} points on 2 axes (from General Scan tab); custom Points/Axes options ignored"))
        except Exception:
            pass
    return out


def _trigger_line(opts):
    from core import function_generator as fg
    hz = opts.get("trigger_hz")
    try:
        cur = fg.trigger_hz()
    except Exception as e:
        cur, why = None, f"Cannot read function generator: {e}"
    else:
        why = "" if cur else "function generator section A is not a fixed pulse generator"
    now_txt = _fmt_hz(cur) if cur else "unknown"
    if not hz:
        return [("ok", f"Trigger rate: unchanged ({now_txt})")]
    if why:
        return [("error", f"Trigger {_fmt_hz(float(hz))}: {why}")]
    _cfg, err = _trigger_prepare(hz)
    if err:
        return [("error", err)]
    out = [("ok", f"Trigger rate: {_fmt_hz(float(hz))} (now {now_txt}, restored after)")]
    if float(hz) > TRIGGER_WARN_HZ and _backend_name()[0] != "HK":
        out.append(("warn", f"Trigger rate {_fmt_hz(float(hz))} exceeds the readout limit (~{_fmt_hz(TRIGGER_WARN_HZ)})"))
    return out


def _rotate_line(opts):
    flags, err = rotate_choice(opts)
    if err:
        return ("error", err)
    am = _scan_engine()
    now = "" if am is None else f" (engine now {_rotate_text({a: getattr(am, a, True) for a in flags})})"
    return ("ok", f"Rotate stage: {_rotate_text(flags)}, Monitor stage fixed{now}")


def next_scan_block(date):
    """The next General Scan block for date as RotationManager._assign_run_block picks it; reads only."""
    cfg = env.read_config()
    mode_dir = "Dark" if str(cfg.get("RunMode", "Laser")).lower() == "dark" else "Laser"
    pats = (os.path.join(cfg.get("RawDataPath", "./Data/RAW/"), mode_dir, f"*_{date}_*.root"),
            os.path.join(env.ADC_DIR, "Data", "FinalResult", f"precal_result_kor_run_{date}_*.root"),
            os.path.join(EXT_RAW_DIR, mode_dir, f"*_{date}_*.root"))
    top = -SCAN_BLOCK
    for pat in pats:
        for f in glob.glob(pat):
            m = re.search(r"_([0-9]{3})\.root", f)
            if m and int(m.group(1)) < SCAN_RUN_LIMIT:
                top = max(top, int(m.group(1)) // SCAN_BLOCK * SCAN_BLOCK)
    try:
        with open(env.REGISTRY, encoding="utf-8") as f:
            for line in f.readlines()[1:]:
                c = line.split(",")
                if len(c) > 2 and c[1] == date and c[2].strip().isdigit() and int(c[2]) < SCAN_RUN_LIMIT:
                    top = max(top, int(c[2]) // SCAN_BLOCK * SCAN_BLOCK)
    except OSError:
        pass
    return top + SCAN_BLOCK if top >= 0 else 0


def _dry_blocks(routine, now, n, opts=None):
    """Run base lines of a General Scan step with n wavelength blocks; the walk keeps the next block per date."""
    if _backend_name()[0] == "HK":
        return [("ok", "Run numbers managed by hk_config (HK system)")]
    date = f"{now:%Y%m%d}"
    sim = routine.setdefault("_dry_blocks", {}) if routine is not None else {}
    if date not in sim:
        sim[date] = next_scan_block(date)
    try:
        pts = _grid(opts or {})[0]
    except (TypeError, ValueError):
        pts = 0
    per = SCAN_BLOCK * max(1, -(-pts // SCAN_BLOCK))   # a scan with more points than one block takes consecutive blocks
    blocks = [sim[date] + per * i for i in range(max(1, n))]
    sim[date] = blocks[-1] + per
    out = [("ok", "Base run: " + ", ".join(f"{b:03d}" for b in blocks) + f" ({date})"
            + (f", {per} run numbers each" if per > SCAN_BLOCK else ""))]
    over = [b for b in blocks if b + per - SCAN_BLOCK > SCAN_BLOCK_MAX]
    if over:
        out.append(("warn", f"Base run {over[0]:03d} exceeds limit {SCAN_BLOCK_MAX:03d}: no General Scan block remaining for {date}"))
    return out


def dry_general_scan(step, routine, now):
    if _backend_name()[0] == "HK":
        return [("error", CAEN_NEEDED[0].upper() + CAEN_NEEDED[1:])]
    return _dry_scan(step, routine, now)


def dry_hk_scan(step, routine, now):
    if _backend_name()[0] != "HK":
        return [("error", HK_NEEDED)]
    out = [l for l in _dry_scan(step, routine, now) if not l[1].startswith(("Trigger", "Event"))]
    return out + [("ok", "Trigger rate and acquisition time: HK configuration")]


def _dry_scan(step, routine, now):
    from core import schedule
    from core import stability as stab
    wl = scan_wavelength(step.get("options"))
    if wl:
        out = _dry_wavelengths(step, routine, now, [wl])
        if schedule._hooks["start"] is None:
            out.append(("error", "General Scan engine is unavailable"))
        return out
    out = [_scan_option_line(step.get("options") or {}), _rotate_line(step.get("options") or {})]
    out += _trigger_line(step.get("options") or {}) + _events_lines(step.get("options") or {})
    out += _laser_line(step)
    seq = stab.laser_sequence()
    if seq is None:
        out.append(("error", "Laser Sequence: Bias or Pulse current is not a valid number"))
    elif not seq:
        out.append(("error", "Laser Sequence: no wavelength enabled"))
    else:
        out.append(("ok", "Laser blocks: " + " -> ".join(f"{w} (bias {b:g}, pulse {p:g} mA)" for w, b, p in seq)))
    name, src = _backend_name()
    out.append(("ok", f"Readout system: {name} ({src})"))
    out += _dry_blocks(routine, now, len(seq or ()), step.get("options") or {})
    if schedule._hooks["start"] is None:
        out.append(("error", "General Scan engine is unavailable"))
    return out


def dry_wavelength_scan(step, routine, now):
    return _dry_wavelengths(step, routine, now, list((step.get("options") or {}).get("wavelengths") or []))


def _dry_wavelengths(step, routine, now, wls):
    opts = step.get("options") or {}
    original, err = _wavelength_check(wls)
    if err:
        return [("error", err)]
    out = [_scan_option_line(opts), _rotate_line(opts)] + _trigger_line(opts) + _events_lines(opts) + _laser_line(step)
    for w in wls:
        out.append(("ok", f"{w}: bias {float(original[w]['bias']):g}, pulse {float(original[w]['pulse']):g} mA"))
    out.append(("ok", "Laser Sequence settings will be restored afterwards"))
    name, src = _backend_name()
    out.append(("ok", f"Readout system: {name} ({src})"))
    return out + _dry_blocks(routine, now, len(wls), opts)


def dry_dark_rate(step, routine, now):
    opts = step.get("options") or {}
    steps, err = _dark_check(opts)
    if err:
        return [("error", err)]
    from core import stability as stab
    out = [("ok", "Runs per cycle: " + ", ".join(f"{s[1]} {s[6]:,} ev" for s in steps)),
           ("ok", f"Threshold: {dark_thr(opts):g} mV"),
           ("ok", f"Base run number: {stab.DARK_BASE} (type {stab.DARK_KIND})")]
    period = float(opts.get("every_min", 15)) * 60.0
    cycles = dark_cycles(opts, steps, _next_clock_due(routine, step), now)
    if cycles < 1:
        out.append(("warn", "No cycle fits before the next step: Skipped"))
    else:
        out.append(("ok", f"{cycles} cycle(s) every {period / 60:g} min"))
    on = _laser_state()
    if on is None:
        out.append(("warn", "Laser status unknown"))
    elif on:
        out.append(("warn", f"LD currently ON: {', '.join(on)} (will turn off first)"))
    else:
        out.append(("ok", "All LDs are off"))
    return out + _hv_preflight_lines()


def dry_stability_qe(step, routine, now):
    opts = qe_until(step.get("options") or {}, _next_clock_due(routine, step) if routine else None,
                    _parse(step.get("due")) or now or _now())
    plan, err = _qe_check(opts)
    if err:
        return [("error", err)]
    ev = int(float(opts.get("events", 300000)))
    from core.scripts import BLOCKS
    out = [("ok", f"{wl}: bias {b:g}, pulse {p:g} mA, {c} x {ev:,} events, interval {iv:g} s")
           for (wl, b, p), c, iv in plan]
    out.append(("ok", f"Base run number: {BLOCKS['manual', 'laser']}"))
    return out + _laser_line(step) + _trigger_line(opts) + _hv_preflight_lines()


def dry_hv_scan(step, routine, now):
    offsets, _events, settle, _cfg, base, err = _hv_scan_options(step.get("options") or {})
    if err:
        return [("error", err)]
    lim = _hv_limits()
    if lim is None:
        return [("error", "hv_request_limits is missing in HV Monitor configuration")]
    sim = routine.setdefault("_dry_hv", {})
    out = [("ok", "Reset stage tilt to 0 (rotation angle preserved)"), ("ok", f"Base run number: {HVSCAN_BASE} (type {HVSCAN_KIND})")]
    lb, lerr = laser_block((step.get("options") or {}).get("wavelength") or HVSCAN_WL)
    out.append(("error", lerr) if lerr else ("ok", f"Laser {lb[0]} ON once for all points (bias {lb[1]:g}, "
                                                    f"pulse {lb[2]:g} mA), OFF at the end"))
    out += _trigger_line(step.get("options") or {})
    for ch in range(3):
        if ch not in hv_scan_channels(step.get("options") or {}):
            out.append(("ok", f"Ch{ch}: stays at {base[ch]:.0f} V"))
            continue
        targets = [base[ch] + off for off in offsets]
        out.append(("ok", f"Ch{ch}: base {base[ch]:.0f} V -> " + ", ".join(f"{v:.0f}" for v in targets) + " V"))
        start_v = sim.get(ch) if sim.get(ch) is not None else base[ch]
        out += _hv_path_lines(ch, targets + [base[ch]], lim, start_v)
        sim[ch] = base[ch]
    out.append(("ok", f"Restore base HV and wait {settle:g} s to settle"))
    opts = step.get("options") or {}
    m, mode, timeout, err = _fit_options(opts)
    if err:
        out.append(("error", err))
    else:
        man = os.path.join(HVSCAN_DIR, f"{now:%Y%m%d}", f"hvscan_{now:%H%M}.json")
        out.append(("ok", f"fit mode {mode} ({FIT_MODES[mode]}), timeout {timeout:g} s: "
                          + shlex.join(hv_fit_command(man, m, mode))))
        if mode == 2:
            out.append(("ok", "Detailed fit: common W added to scan_backweight.csv, HV scan runs refitted"))
    return out + _laser_line(step) + _hv_preflight_lines()


def _dry_gain_curve(step, routine, opts):
    sim = routine.setdefault("_dry_hv", {})
    idx = next((i for i, s in enumerate(routine["steps"]) if s is step), len(routine["steps"]))
    scans = [s for s in routine["steps"][:idx] if s["enabled"] and s["kind"] == "hv_scan"]
    scan_first = bool(scans)
    p = gain_curve_plan(opts, sim, chans=hv_scan_channels(scans[-1].get("options") or {}) if scans else None)
    stop = opts.get("on_unusable", "configured") == "stop"
    out = [("ok", "Uses the new fit from the HV scan step before this one")] if scan_first else [
        ("error" if stop else "warn", "No HV scan step before this one: no fit from this routine run")]
    out += [("ok", t) for t in p["lines"]] + [("warn" if scan_first or not stop else "error", t) for t in p["errors"]]
    out.append(("ok", "If the gain curve is unusable: " + ("stop the routine" if stop else "keep the Configured HV and continue")))
    for ch, v in p["items"]:
        sim[ch] = v
    out.append(("ok", f"Gain settling wait: {float(opts.get('gain_delay_s', 180)):g} s"))
    out += _restore_lines(opts, [ch for ch, _v in p["items"]])
    return out


def dry_hv_apply(step, routine, now):
    opts = step.get("options") or {}
    if opts.get("mode") == "gain_curve":
        return _dry_gain_curve(step, routine, opts)
    items, err = _hv_apply_targets(opts)
    if err:
        return [("error", err)]
    lim = _hv_limits()
    if lim is None:
        return [("error", "hv_request_limits is missing in HV Monitor configuration")]
    sim = routine.setdefault("_dry_hv", {})
    out = []
    for ch, v in items:
        prev = sim.get(ch)
        out.append(("ok", f"Ch{ch} -> {v:.0f} V" + (f" (from {prev:.0f} V)" if prev is not None else "")))
        if prev is None:
            out.append(("warn", f"Ch{ch}: monitored voltage (VMon) unknown"))
        out += _hv_path_lines(ch, [v], lim, prev)
        sim[ch] = v
    out.append(("ok", f"Gain settling wait: {float(opts.get('gain_delay_s', 180)):g} s"))
    out += _restore_lines(opts, [ch for ch, _v in items])
    return out


def _restore_lines(opts, chans):
    if not opts.get("restore_hv", True):
        return [("warn", "Header HV is NOT restored at the routine end")]
    try:
        cfg = env.read_config()
        return [("ok", "At the routine end: header HV (Hamamatsu gain) back on "
                       + ", ".join(f"Ch{ch} {float(cfg.get(f'HV{ch + 1}')):.0f} V" for ch in chans))]
    except (TypeError, ValueError):
        return [("warn", "Header HV unreadable: no restore at the routine end")]


def _slot_chain(slots, origin):
    out, t = [], origin - timedelta(hours=6)
    for hhmm in slots:
        t = _clock_target(hhmm, t)
        out.append(t)
    return out


def _summary_lines(r, enabled, now):
    """Checks that do not depend on the step walk."""
    import instance_lock
    out = []
    if _state["active"]:
        out.append(("error", "A routine is already running"))
    if r.get("home_stage", True):
        out.append(("ok", "At the routine end: stages to the origin (tilt 0, rotation 0)") if _scan_engine() is not None else
                   ("warn", "At the routine end: stages to the origin skipped (no scan engine)"))
    try:
        busy = _busy()
    except Exception:
        busy = False
    if busy:
        out.append(("error", "A General Scan or Stability Run is currently running"))
    fast = caen_trigger_excess() if _backend_name()[0] != "HK" else None
    if fast:
        out.append(("error", f"Trigger rate {_fmt_hz(fast)} is too high: [Require] CAEN DAQ -> {_fmt_hz(CAEN_TRIGGER_HZ)}"))
    if any(s["kind"] in ("general_scan", "wavelength_scan") for s in enabled) and _backend_name()[0] != "HK":
        try:
            nb = next_scan_block(f"{now:%Y%m%d}")
            if nb > SCAN_BLOCK_MAX:
                out.append(("warn", f"General Scan run numbers 000-{SCAN_BLOCK_MAX:03d} exhausted for {now:%Y-%m-%d} (next is {nb:03d})"))
        except Exception as e:
            out.append(("warn", f"General Scan run block check failed: {e}"))
    hv_steps = any(s["kind"] in ("hv_scan", "hv_apply") for s in enabled)
    if hv_steps:
        try:
            hv_on = instance_lock.is_running("hv")
        except Exception:
            hv_on = False
        out.append(("ok", "HV Monitor is running") if hv_on else ("error", "HV Monitor is not running"))
    on = _laser_state()
    if on is None:
        out.append(("warn", "Laser status unknown"))
    else:
        out.append(("ok", f"LD ON: {', '.join(on)}" if on else "all LDs off"))
    lim = _hv_limits()
    if lim is None:
        out.append(("error" if hv_steps else "warn", "hv_request_limits missing from configuration"))
    else:
        chans = ", ".join("Ch%d %.0f-%.0f V" % ((ch,) + _ch_limits(lim, ch)) for ch in range(3))
        step_v = float(lim.get('max_step_v', 250))
        placeholder = all(tuple(map(float, _ch_limits(lim, ch))) + (step_v,) == PLACEHOLDER_LIMITS for ch in range(3))
        if placeholder:
            out.append(("warn", f"HV limits are still placeholders: {chans}, max step {step_v:.0f} V"))
        else:
            out.append(("ok", f"HV limits: {chans}, max step {step_v:.0f} V"))
    late = late_start_text(r, now)
    if late:
        out.append(("warn", late))
    clocks = {}
    for s in enabled:
        if s["start"] != SEQUENTIAL:
            clocks.setdefault(s["start"], []).append(s["name"])
    for hhmm, names in clocks.items():
        if len(names) > 1:
            out.append(("error", f"Duplicate start time {hhmm}: {', '.join(names)}"))
    return out


def precheck(routine=None, now=None):
    """The summary checks only (no step walk)."""
    now = now or _now()
    with _lock:
        r = copy.deepcopy(routine if routine is not None else current())
    return _summary_lines(r, [s for s in r["steps"] if s["enabled"]], now)


def dry_run(routine=None, now=None):
    """Walk every enabled step as the runner would, without any hardware action.
    -> {"steps": [{index, name, kind, start, end, lines}], "summary": [(level, text)], "end": datetime}."""
    now = now or _now()
    with _lock:
        r = copy.deepcopy(routine if routine is not None else current())
    for s in r["steps"]:
        s.update(started=None, finished=None, note="")
    assign_due(r, now)
    enabled = [s for s in r["steps"] if s["enabled"]]
    if any(s["kind"] in ("hv_scan", "hv_apply") for s in enabled):
        r["_dry_hv"] = {ch: _vmon_now(ch)[0] for ch in range(3)}
    summary = _summary_lines(r, enabled, now)
    rec = recommended_starts(r, now)
    queued = _queued_times()
    out, cursor = [], now
    for i, s in enumerate(r["steps"]):
        if not s["enabled"]:
            continue
        due = _parse(s["due"])
        start = max(due, cursor) if due else cursor
        s["due"] = _stamp(start) if due else None
        try:
            lines = list(DRY_RUNS[s["kind"]](s, r, start))
        except Exception as e:
            lines = [("error", f"Pre-run check failed: {type(e).__name__}: {e}")]
        if i in rec:
            lines.append(("warn", f"Previous step finishes at {rec[i][1]:%H:%M}: recommended start is {rec[i][0]:%H:%M}"))
            summary.append(("warn", f"{s['name']}: overlaps previous step; recommended start is {rec[i][0]:%H:%M}"))
        end = start + timedelta(seconds=estimate_seconds(s, r, start))
        nxt = _next_clock_due(r, s)
        if nxt is not None and end > nxt:
            lines.append(("warn", f"Overlaps next scheduled step ({nxt:%H:%M}) by {(end - nxt).total_seconds() / 60:.0f} min"))
            summary.append(("warn", f"{s['name']} extends past {nxt:%H:%M}"))
        guard = start + timedelta(seconds=(end - start).total_seconds() + COLLISION_GUARD_S)
        clash = next((t for t in queued if start <= t <= guard), None)
        if clash is not None:
            lines.append(("warn", f"Conflict with Schedule Manager run at {clash:%m-%d %H:%M}: Skipped"))
            summary.append(("warn", f"{s['name']}: Conflict with Schedule Manager run at {clash:%m-%d %H:%M}, step skipped"))
        out.append({"index": i, "name": s["name"], "kind": s["kind"], "start": start, "end": end, "lines": lines})
        cursor = end
    flagged = {t for t in queued for s in out if s["start"] <= t <= s["end"] + timedelta(seconds=COLLISION_GUARD_S)}
    for t in queued:
        if t not in flagged and now <= t <= cursor:
            summary.append(("warn", f"Schedule Manager run scheduled at {t:%m-%d %H:%M} during routine"))
    chain = _slot_chain(r["slots"], now)
    summary.append(("ok", f"Estimated end time: {cursor:%m-%d %H:%M}"))
    if chain and cursor > chain[-1]:
        summary.append(("warn", f"Exceeds last time slot ({chain[-1]:%H:%M}) by {(cursor - chain[-1]).total_seconds() / 60:.0f} min"))
    return {"steps": out, "summary": summary, "end": cursor}


EXECUTORS = {"general_scan": run_general_scan, "hk_scan": run_hk_scan, "wavelength_scan": run_wavelength_scan,
             "stability_qe": run_stability_qe_triggered, "dark_rate": run_dark_rate,
             "hv_scan": run_hv_scan_triggered, "hv_apply": run_hv_apply}
ABORTERS = {"general_scan": _abort_general_scan, "hk_scan": _abort_general_scan, "wavelength_scan": _abort_general_scan,
            "stability_qe": _abort_dark_rate, "dark_rate": _abort_dark_rate, "hv_scan": _abort_hv_scan}
DRY_RUNS = {"general_scan": dry_general_scan, "hk_scan": dry_hk_scan, "wavelength_scan": dry_wavelength_scan,
            "stability_qe": dry_stability_qe, "dark_rate": dry_dark_rate,
            "hv_scan": dry_hv_scan, "hv_apply": dry_hv_apply}

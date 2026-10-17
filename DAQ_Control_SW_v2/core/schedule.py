"""Schedule Manager queue and runner, as the Tk app's AutomationManager (add_schedule,
remove_schedule, _save/_load_schedules, _schedule_watchdog_loop): up to 3 General Scans
queued at JST times in queued_schedules.json, each with a snapshot of the config header.

The runner thread starts only when the General Scan engine registers its start function with
set_starter(); nothing is fired or consumed before that. While the Tk app runs it owns the
queue and fires it, so v2 only fires (and reloads the file first) while the Tk app is closed. Qt-free; listeners()
are called from the runner thread."""
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from core import env

QUEUE = os.path.join(env.DAQ_DIR, "queued_schedules.json")
JST = timezone(timedelta(hours=9))
MAX_SCHEDULES = 3
TICK_S = 5.0
TK_OWNS = "The Tk DAQ app is open and owns the schedule queue."

_lock = threading.RLock()
_items = []            # [{"time_obj", "time_str", "config"}], sorted by time
_loaded = False
_owner = False         # True while v2 owns the queue (Tk app closed)
_runner = None
_hooks = {"start": None, "is_running": None, "last_errors": None, "dispatch": None}
listeners = []         # fn() after the queue changed


def _log(msg):
    from core import applog
    applog.log(msg)


def _notify():
    for fn in list(listeners):
        try:
            fn()
        except RuntimeError:   # its widget was deleted
            listeners.remove(fn)
        except Exception:
            pass


def set_starter(start, is_running=None, last_errors=None, dispatch=None):
    """start(): begin a General Scan without the interactive checks (Tk: start_general_scan(skip_validation=True)).
    is_running() -> bool, last_errors() -> [str] of the previous scan; dispatch(fn) runs fn on the GUI thread.
    Pass start=None to unregister."""
    _hooks.update(start=start, is_running=is_running, last_errors=last_errors, dispatch=dispatch)
    if start is not None:
        start_runner()


def general_scan_running():
    """The registered is_running(), or the v2 scan engine's state once it exists."""
    fn = _hooks["is_running"]
    try:
        if fn is not None:
            return bool(fn())
        import sys
        host = getattr(sys.modules.get("core.scan"), "_host", None)
        return bool(host is not None and host.auto_mgr.is_running)
    except Exception:
        return False


def tk_owns():
    import instance_lock
    return instance_lock.is_running("daq")


# ---------------------------------------------------------------- file
def save():
    data = [{"time_str": s["time_str"], "config": s["config"]} for s in _items]
    tmp = QUEUE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, QUEUE)


def read_file():
    """[{"time_str", "config"}] as stored (past entries included), [] if missing/unreadable."""
    try:
        with open(QUEUE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def load(now=None):
    """Replace the queue with the file's future entries (past ones are dropped, as in the Tk app)."""
    global _loaded
    now = now or datetime.now(JST)
    items, err = [], None
    try:
        if os.path.exists(QUEUE):
            with open(QUEUE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data:
                target = datetime.strptime(item["time_str"], "%Y-%m-%d %H:%M").replace(tzinfo=JST)
                if target > now:
                    items.append({"time_obj": target, "time_str": item["time_str"], "config": item["config"]})
    except Exception as e:
        err = e
    with _lock:
        _items[:] = sorted(items, key=lambda x: x["time_obj"])
        _loaded = True
    if err is not None:
        _log(f"[ERROR] Failed to load schedules: {err}")
    elif items:
        _log(f"[INFO] Restored {len(items)} schedules from disk.")
    _notify()


def _take_over(now=None):
    """-> True when v2 owns the queue; reloads the file when it takes over from the Tk app."""
    global _owner
    if tk_owns():
        _owner = False
        return False
    if not _owner or not _loaded:
        _owner = True
        load(now)
    return True


def items():
    with _lock:
        _take_over()
        return [dict(s) for s in _items]


# ---------------------------------------------------------------- edit
def add(date_str, hour, minute, cfg=None, now=None):
    """-> (ok, title, message); title/message describe a refusal."""
    with _lock:
        if not _take_over():
            return False, "Schedule Manager", TK_OWNS
        if len(_items) >= MAX_SCHEDULES:
            return False, "Limit Reached", f"You can only schedule up to {MAX_SCHEDULES} runs."
        try:
            full_str = f"{date_str.strip()} {str(hour).strip().zfill(2)}:{str(minute).strip().zfill(2)}"
            target = datetime.strptime(full_str, "%Y-%m-%d %H:%M").replace(tzinfo=JST)
        except Exception as e:
            return False, "Format Error", f"Invalid input: {e}"
        if target <= (now or datetime.now(JST)):
            return False, "Time Error", f"Cannot schedule for a past time.\n(Input: {full_str} JST)"
        item = {"time_obj": target, "time_str": target.strftime("%Y-%m-%d %H:%M"),
                "config": cfg if cfg is not None else env.read_config()}
        _items.append(item)
        _items.sort(key=lambda x: x["time_obj"])
        try:
            save()
        except OSError as e:
            _log(f"[ERROR] Failed to save schedules: {e}")
    _log(f"[INFO] Schedule added for {item['time_str']} JST.")
    _notify()
    return True, "", ""


def remove(index):
    with _lock:
        if not _take_over() or not 0 <= index < len(_items):
            return False
        removed = _items.pop(index)
        try:
            save()
        except OSError as e:
            _log(f"[ERROR] Failed to save schedules: {e}")
    _log(f"[INFO] Scheduled run for {removed['time_str']} JST cancelled.")
    _notify()
    return True


# ---------------------------------------------------------------- runner
def _scan_busy():
    if general_scan_running():
        return True
    from core import status
    from core.jobs import JOBS
    return status.scan_running() or JOBS.running("daq")


def tick(now=None):
    """One runner step: fire the first schedule when it is due. -> what happened (for logs/tests)."""
    with _lock:
        if not _take_over(now):
            return "tk owns"
    now = now or datetime.now(JST)
    with _lock:
        if not _items or now < _items[0]["time_obj"]:
            return "waiting"
        due = _items.pop(0)
        try:
            save()
        except OSError as e:
            _log(f"[ERROR] Failed to save schedules: {e}")
    _notify()
    _log(f"[INFO] Scheduled time ({due['time_str']} JST) reached. Starting auto-scan...")
    from core.access import ACCESS
    errors = []
    if _hooks["last_errors"]:
        try:
            errors = list(_hooks["last_errors"]() or [])
        except Exception:
            errors = []
    if errors:
        _log(f"[CRITICAL] Skipping next scheduled scan: the previous scan completed with {len(errors)} error(s) "
             f"({'; '.join(errors)}). Resolve the cause and re-queue manually rather than risk repeating the "
             f"same failure unattended.")
        return "skipped: previous errors"
    if _hooks["start"] is None:
        _log("[WARNING] Scheduled run skipped: the General Scan engine is not available in this app.")
        return "skipped: no starter"
    if not ACCESS.unlocked:
        _log("[WARNING] Scheduled run skipped: controls are locked.")
        return "skipped: locked"
    if _scan_busy():
        _log("[WARNING] Another scan is already running. Scheduled run skipped.")
        return "skipped: busy"
    start, dispatch = _hooks["start"], _hooks["dispatch"]
    (dispatch or (lambda f: f()))(start)
    return "started"


def _loop():
    while True:
        try:
            tick()
        except Exception as e:
            _log(f"[ERROR] Schedule runner: {e}")
        time.sleep(TICK_S)


def start_runner():
    """Idempotent; the runner thread lives for the whole app."""
    global _runner
    with _lock:
        if _runner is None:
            _runner = threading.Thread(target=_loop, daemon=True, name="schedule-runner")
            _runner.start()


def view():
    """[(time_str, status, config)] for display: v2's queue, or the file while the Tk app owns it."""
    now = datetime.now(JST)
    with _lock:
        if _take_over():
            return [(s["time_str"], "WAITING" if s["time_obj"] > now else "PAST", s["config"]) for s in _items]
    out = []
    for s in read_file():
        try:
            due = datetime.strptime(s.get("time_str", ""), "%Y-%m-%d %H:%M").replace(tzinfo=JST)
            st = "WAITING" if due > now else "PAST"
        except (TypeError, ValueError):
            st = "?"
        out.append((s.get("time_str", ""), st, s.get("config", {})))
    return out

"""AUS linear stage state from the newest run log on the AUS PC. Read only (ssh); nothing is ever commanded."""
import subprocess
import time

SSH_HOST = "aus"
STALE_S = 180       # a move line older than this is not a move in progress
POLL_S = 2.0
MAX_WAIT_S = 120    # a PMT1 <-> PMT2 move takes about 20 s
QUIET = (("04:00:00", 40),)   # (clock time the AUS routine moves the stage to the other PMT, seconds to stay clear)
POINT_S = 200       # a scan point started this close before a quiet time would still be taking data in it

_PROBE = r'''
import glob, os, time
d = os.path.expanduser("~/WaveDumpSaves")
ps = (glob.glob(os.path.join(d, "*", "logs_*", "terminal_*.log"))
      + glob.glob(os.path.join(d, "*", "*", "logs_*", "terminal_*.log")))
t_move = None
if ps:
    for line in open(max(ps, key=os.path.getmtime), errors="replace"):
        if "Moving to PMT" in line and "position" in line:
            try:
                t_move = time.mktime(time.strptime(line[:19], "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                t_move = None
        elif "Linear stage ->" in line or "Linear track verified" in line:
            t_move = None
print("STAGE:" + ("moving" if t_move is not None and time.time() - t_move < STALE_S else "idle"))
'''.replace("STALE_S", str(STALE_S))

_said = {"ok": False, "unreadable": False}


def moving(timeout=6.0):
    """True / False, or None when the AUS PC cannot be read."""
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4", SSH_HOST, "python3", "-"],
                           input=_PROBE, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in r.stdout.splitlines():
        if line.startswith("STAGE:"):
            return line == "STAGE:moving"
    return None


def quiet_wait_s(now=None):
    """Seconds a point starting now must wait so that it takes no data during a scheduled AUS stage move."""
    now = time.time() if now is None else now
    lt = time.localtime(now)
    for clock, clear_s in QUIET:
        h, m, sec = (int(x) for x in clock.split(":"))
        t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, m, sec, 0, 0, -1))
        if t - POINT_S <= now < t + clear_s:
            return t + clear_s - now
    return 0.0


def wait_idle(log, keep_going=lambda: True):
    """Blocks through a scheduled AUS stage move and while the stage is moving (at most MAX_WAIT_S).
    An unreadable AUS PC does not block."""
    wait = quiet_wait_s()
    if wait > 0:
        log(f"[INFO] AUS linear stage moves at a scheduled time; waiting {wait:.0f} s before this point's DAQ.")
        t_end = time.time() + wait
        while keep_going() and time.time() < t_end:
            time.sleep(min(POLL_S, max(0.05, t_end - time.time())))
        if not keep_going():
            return
        log("[INFO] Scheduled AUS linear stage move time passed; continuing.")
    state = moving()
    if state is None:
        if not _said["unreadable"]:
            log("[WARNING] AUS linear stage check: AUS PC not readable; continuing without the check.")
        _said.update(unreadable=True, ok=False)
        return
    _said["unreadable"] = False
    if not state:
        if not _said["ok"]:
            log("[INFO] AUS linear stage check: not moving (checked before every scan point).")
            _said["ok"] = True
        return
    log("[INFO] AUS linear stage is moving; waiting before this point's DAQ.")
    t0 = time.time()
    while state and keep_going() and time.time() - t0 < MAX_WAIT_S:
        time.sleep(POLL_S)
        state = moving()
    if not keep_going():
        return
    if state:
        log(f"[WARNING] AUS linear stage still reported moving after {time.time() - t0:.0f} s; continuing.")
    elif state is None:
        log("[WARNING] AUS linear stage check: AUS PC not readable during the wait; continuing.")
    else:
        log(f"[INFO] AUS linear stage stopped after {time.time() - t0:.0f} s; continuing.")

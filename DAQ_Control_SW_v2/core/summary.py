"""Background ROOT summaries (ADC_test/routine_summary.py) after a Daily Routine or a General Scan. Qt-free."""
import json
import os
import re
import subprocess
from collections import defaultdict

from core import env
from core.run_info import SCAN_BLOCK

SCAN_RUN_LIMIT = 500   # General Scan blocks 000-499
_uniformity_queue = []   # (date, first, last) of General Scan blocks finished while a routine is running


def _log(msg):
    from core import applog
    applog.log(msg)


def launch(args, what):
    """Start routine_summary.py <args> --index detached at low priority, one at a time (flock). -> argv or None."""
    if env.NO_HW:
        return None
    script = os.path.join(env.ADC_DIR, "routine_summary.py")
    if not os.path.exists(script):
        _log(f"[WARNING] Summary of {what} not started: {script} not found.")
        return None
    logdir = os.path.join(env.ADC_DIR, "LOG")
    cmd = ["flock", os.path.join(logdir, "routine_summary.lock"), "nice", "-n", "19", "python3", script,
           *args, "--index"]
    try:
        os.makedirs(logdir, exist_ok=True)
        with open(os.path.join(logdir, "routine_summary.log"), "a") as out:
            subprocess.Popen(cmd, cwd=env.ADC_DIR, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                             start_new_session=True)
    except OSError as e:
        _log(f"[WARNING] Summary of {what} not started: {e}")
        return None
    _log(f"[INFO] Summary of {what} started in the background: routine_summary.py {' '.join(args)} --index")
    return cmd


def launch_uniformity():
    """analyze.sh uniformity for each General Scan block of the routine that just ended, one after another."""
    blocks = list(dict.fromkeys(_uniformity_queue))
    del _uniformity_queue[:]
    if env.NO_HW or not blocks:
        return None
    if not os.path.exists(os.path.join(env.ADC_DIR, "analyze.sh")):
        _log("[WARNING] Uniformity after the routine not started: analyze.sh not found.")
        return None
    logdir = os.path.join(env.ADC_DIR, "LOG")
    chain = " ; ".join(f"./analyze.sh uniformity {d} {r0} {r1}" for d, r0, r1 in blocks)
    # same lock as routine_summary.py: the refit rewrites FinalResult, so it waits for the summary
    cmd = ["flock", os.path.join(logdir, "routine_summary.lock"), "nice", "-n", "19", "bash", "-c", chain]
    try:
        os.makedirs(logdir, exist_ok=True)
        with open(os.path.join(logdir, "auto_uniformity.log"), "a") as out:
            subprocess.Popen(cmd, cwd=env.ADC_DIR, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                             start_new_session=True)
    except OSError as e:
        _log(f"[WARNING] Uniformity after the routine not started: {e}")
        return None
    _log("[INFO] Uniformity started in the background for " + ", ".join(f"{d} {r0:03d}-{r1:03d}" for d, r0, r1 in blocks)
         + " (LOG/auto_uniformity.log).")
    return cmd


def scan_blocks(map_path, date, since=None, size=SCAN_BLOCK):
    """[(first, last)] run numbers per run block written to the scan map at or after since (datetime);
    size = run numbers per wavelength of that scan (more than one block for a long scan)."""
    try:
        with open(map_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    t0 = since.strftime("%Y-%m-%d %H:%M:%S") if since else ""
    runs = []
    for e in data.values():
        if not isinstance(e, dict) or e.get("status") != "OK" or str(e.get("time", "")) < t0:
            continue
        m = re.search(rf"_{date}_(\d{{3,4}})\.root$", str(e.get("file") or ""))
        if m and int(m.group(1)) < SCAN_RUN_LIMIT:
            runs.append(int(m.group(1)))
    blocks = defaultdict(list)
    base = min(runs) // SCAN_BLOCK * SCAN_BLOCK if runs and size > SCAN_BLOCK else 0
    for n in runs:
        blocks[(n - base) // max(size, SCAN_BLOCK)].append(n)
    return [(min(v), max(v)) for _b, v in sorted(blocks.items())]


def pack_scan(am, base_dir, start, dummy=False):
    """routine_summary.py --scan for each run block of the General Scan that just ended (CAEN, not TEST RUN)."""
    if dummy or getattr(am, "daq_backend", "caen") == "hk":
        return []
    date = os.environ.get("SCAN_START_DATE") or f"{start:%Y%m%d}"
    path = os.path.join(base_dir, "LOG", "ScanHistory", f"scanmap_{date}.json")
    from core import routine
    out = []
    for r0, r1 in scan_blocks(path, date, start, int(getattr(am, "scan_block_runs", SCAN_BLOCK) or SCAN_BLOCK)):
        if routine.is_active():
            _uniformity_queue.append((date, r0, r1))
        cmd = launch(["--scan", date, f"{r0:03d}", f"{r1:03d}"], f"General Scan {date} {r0:03d}-{r1:03d}")
        if cmd:
            out.append(cmd)
    return out

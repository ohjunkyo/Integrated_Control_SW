"""Step-by-step progress of the running General Scan, read from the files the scan engine writes, so it
works the same whichever app runs the scan: the active-scan marker, scan_recovery_state.json (current
point), the day's scanmap (finished points), scan_live_<date>.txt (laser blocks) and the newest
TakingLog (RAW file of the point being taken). Qt-free."""
import glob
import json
import os
import re
import time
from datetime import datetime, timedelta

from core import env

MARKER = "/tmp/daq_flags/active_scan"
BLOCK = re.compile(r"Laser block (\d+)/(\d+):\s*(\S+)")
RUN_NO = re.compile(r"_(\d+)\.root$")


def _marker():
    """(date_tag, start epoch) of the running scan, or None."""
    try:
        with open(MARKER) as f:
            tag, pid = f.read().split()[:2]
        if not os.path.exists(f"/proc/{pid}"):
            return None
        return tag, os.path.getmtime(MARKER)
    except (OSError, ValueError):
        return None


def _json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _block(started):
    """(i, n, wavelength) of the newest 'Laser block i/n' line written since the scan started."""
    found = None
    for path in sorted(glob.glob(os.path.join(env.DAQ_DIR, "logs", "scan_live_*.txt")))[-2:]:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = BLOCK.search(line)
                    if not m:
                        continue
                    try:
                        t = datetime.strptime(line[1:20], "%Y-%m-%d %H:%M:%S").timestamp()
                    except ValueError:
                        continue
                    if t >= started - 5:
                        found = (int(m.group(1)), int(m.group(2)), m.group(3))
        except OSError:
            pass
    return found


def finished_points(tag, started):
    """Finished points of this scan, newest first: dicts time / axis / tilt / wl / status / run / took_s."""
    data = _json(os.path.join(env.DAQ_DIR, "LOG", "ScanHistory", f"scanmap_{tag}.json")) or {}
    rows = []
    for e in data.values():
        if e.get("kind", "scan") != "scan":
            continue
        try:
            t = datetime.strptime(e.get("time", ""), "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            continue
        if t < started - 5:
            continue
        m = RUN_NO.search(e.get("file", ""))
        rows.append({"t": t, "axis": e.get("axis"), "tilt": e.get("tilt"), "wl": e.get("wl"),
                     "status": e.get("status", ""), "run": m.group(1) if m else "", "file": e.get("file", "")})
    rows.sort(key=lambda r: r["t"])
    prev = started
    for r in rows:
        r["took_s"] = r["t"] - prev
        prev = r["t"]
    return rows[::-1]


def last_scan():
    """The newest finished General Scan from ScanHistory: {'tag', 'started', 'ended', 'done'} or None."""
    files = sorted(glob.glob(os.path.join(env.DAQ_DIR, "LOG", "ScanHistory", "history_*.json")))
    h = _json(files[-1]) if files else None
    if not h:
        return None
    try:
        end = datetime.strptime(f"{h['date']} {h['end_time']}", "%Y-%m-%d %H:%M:%S")
        start = datetime.strptime(f"{h['date']} {h['start_time']}", "%Y-%m-%d %H:%M:%S")
    except (KeyError, ValueError):
        return None
    if start > end:                      # ran past midnight: 'date' is the end date
        start -= timedelta(days=1)
    tag = start.strftime("%Y%m%d")
    done = [r for r in finished_points(tag, start.timestamp()) if r["t"] <= end.timestamp() + 5]
    return {"tag": tag, "started": start.timestamp(), "ended": end.timestamp(), "done": done,
            "status": h.get("status", "")}


def current_raw_file():
    """RAW file of the point being taken: the newest 'Generated File' / 'Target Dir' of the TakingLog."""
    files = glob.glob(os.path.join(env.ADC_DIR, "LOG", "DAQ", "TakingLog_*.txt"))
    if not files:
        return None
    try:
        with open(max(files, key=os.path.getmtime), "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 16384))
            lines = f.read().decode(errors="replace").splitlines()
    except OSError:
        return None
    name = folder = None
    for line in reversed(lines):
        if name is None and "Generated File:" in line:
            name = line.split("Generated File:", 1)[1].strip()
        elif folder is None and "Target Dir" in line:
            folder = line.split(":", 2)[-1].strip() if line.count(":") >= 2 else None
        if name and folder:
            return os.path.join(folder, name)
    return None


def point_progress(done, events_total):
    """Progress of the point being taken: dict events / total / ped / exact, or None.
    Exact from v2's own scan job; when the Tk app runs the scan its event lines stay in its console,
    so the RAW file's size against the previous point's file gives an estimate instead."""
    from core.jobs import JOBS
    job = JOBS.jobs.get("general_scan")
    if job is not None and job.state == "running" and job.progress is not None:
        p = job.progress
        return {"events": p.evt, "total": p.total or events_total, "ped": p.ped, "exact": True}
    path = current_raw_file()
    ref = next((r["file"] for r in done if r.get("file") and os.path.exists(r["file"])), None)
    if ref is None and path:     # a single acquisition: the previous RAW file of the same folder
        older = [f for f in glob.glob(os.path.join(os.path.dirname(path), "*.root"))
                 if os.path.abspath(f) != os.path.abspath(path)]
        ref = max(older, key=os.path.getmtime) if older else None
    if not path or not ref or os.path.abspath(path) == os.path.abspath(ref):
        return None
    try:
        size, full = os.path.getsize(path), os.path.getsize(ref)
    except OSError:
        return {"events": 0, "total": events_total, "ped": "", "exact": False}
    frac = min(1.0, size / full) if full else 0.0
    return {"events": int(frac * (events_total or 0)), "total": events_total, "ped": "", "exact": False}


def planned_total(state, block):
    """Points of the whole scan: the engine's own count when it records one, else tilts x 2 axes x blocks."""
    if state and state.get("total"):
        return int(state["total"])
    try:
        from core import scan
        host = scan._host
        tilts = len(host.auto_mgr.build_tilt_angles()) if host is not None else 23
    except Exception:
        tilts = 23
    return tilts * 2 * (block[1] if block else 1)


def snapshot():
    """Everything the Live Console shows; {'running': False} when no scan runs."""
    mk = _marker()
    if mk is None:
        return {"running": False}
    tag, started = mk
    state = _json(os.path.join(env.DAQ_DIR, "scan_recovery_state.json")) or {}
    block = _block(started)
    done = finished_points(tag, started)
    total = planned_total(state, block)
    took = [r["took_s"] for r in done[:10] if r["took_s"] > 0]
    per_pt = sum(took) / len(took) if took else None
    left = max(0, total - len(done))
    try:
        events_total = int(float(env.read_config().get("Events", "")))
    except (ValueError, OSError):
        events_total = None
    return {"running": True, "tag": tag, "started": started, "block": block, "done": done, "total": total,
            "current": (state.get("axis"), state.get("tilt")), "eta_s": per_pt * left if per_pt else None,
            "per_point_s": per_pt, "point": point_progress(done, events_total), "now": time.time()}

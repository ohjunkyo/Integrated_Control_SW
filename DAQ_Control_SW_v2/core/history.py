"""Scan history: per-scan config snapshots (LOG/ScanHistory/history_*.json), DAQ backend
inference and per-PMT usage periods. Pure functions, no widgets (taken from the Tk app's
managers/info_tab.py so v2 does not import Tk modules)."""
import os
import glob
import json
from datetime import datetime, timedelta

POSITIONS = {1: "Monitor", 2: "PMT1", 3: "PMT2"}


CAEN_SCAN_TABLE = os.path.expanduser("~/ADC/ADC_test/Data/summary/ScanMetadata.csv")
HK_BACKEND_ADDED = "2026-07-24"   # the GUI had no HK Digitizer option before this date


def _caen_windows(_cache={}):
    """(start, end) of every CAEN scan block in the analysis summary (built from CAEN data only)."""
    try:
        mtime = os.path.getmtime(CAEN_SCAN_TABLE)
    except OSError:
        return []
    if _cache.get("mtime") != mtime:
        wins = []
        with open(CAEN_SCAN_TABLE, encoding="utf-8") as f:
            next(f, None)
            for line in f:
                r = line.strip().split(",")
                try:
                    a = datetime.strptime(f"{r[2]} {r[5]}", "%Y%m%d %H:%M")
                    b = datetime.strptime(f"{r[2]} {r[6]}", "%Y%m%d %H:%M")
                except (IndexError, ValueError):
                    continue
                wins.append((a, b if b >= a else b + timedelta(days=1)))
        _cache.update(mtime=mtime, wins=wins)
    return _cache["wins"]


def scan_backend(base_dir, d, _maps={}):
    """DAQ backend of one history record: the stored "daq_backend", else inferred
    from the per-point scanmap (HK points are filed as "HK:<target>:<run_id>"),
    else from overlap with a CAEN scan block in ScanMetadata.csv; "?" if unknown.
    Scans that ended before HK_BACKEND_ADDED can only be CAEN."""
    if d.get("daq_backend"):
        return d["daq_backend"]
    if str(d.get("date", "")) and str(d.get("date")) < HK_BACKEND_ADDED:
        return "CAEN"
    try:
        end = datetime.strptime(f"{d['date']} {d['end_time']}", "%Y-%m-%d %H:%M:%S")
        start = datetime.strptime(f"{d['date']} {d['start_time']}", "%Y-%m-%d %H:%M:%S")
    except (KeyError, ValueError):
        return "?"
    if start > end:
        start -= timedelta(days=1)
    files = []
    for day in {start.strftime("%Y%m%d"), end.strftime("%Y%m%d")}:
        path = os.path.join(base_dir, "LOG", "ScanHistory", f"scanmap_{day}.json")
        try:
            mtime = os.path.getmtime(path)
            if path not in _maps or _maps[path][0] != mtime:
                with open(path, encoding="utf-8") as f:
                    _maps[path] = (mtime, json.load(f))
        except (OSError, ValueError):
            continue
        for v in _maps[path][1].values():
            try:
                t = datetime.strptime(v.get("time", ""), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if start <= t <= end:
                files.append(str(v.get("file", "")))
    if files:
        return "HK Digitizer" if any(f.startswith("HK:") for f in files) else "CAEN"
    if any(a <= end and b >= start for a, b in _caen_windows()):
        return "CAEN"
    return "?"


def load_scan_entries(base_dir):
    """One dict per history file, oldest first."""
    out = []
    for p in sorted(glob.glob(os.path.join(base_dir, "LOG", "ScanHistory", "history_*.json"))):
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        cfg = d.get("config") or {}
        out.append({"date": d.get("date", ""), "start": d.get("start_time", ""), "end": d.get("end_time", ""),
                    "shifter": d.get("shifter", ""), "cfg": cfg, "file": os.path.basename(p),
                    "daq": scan_backend(base_dir, d)})
    return out


def _hv(v):
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def hv_text(p):
    lo, hi = min(p["hvs"]), max(p["hvs"])
    return str(lo) if lo == hi else f"{lo}–{hi}"


def build_pmt_history(entries):
    """{serial: [period, ...]}, a period being consecutive scans with the same
    position, cable direction and DAQ, and an HV within 1 V (1729 and 1730 are one setting)."""
    hist = {}
    for e in entries:
        cfg = e["cfg"]
        for slot, pos in POSITIONS.items():
            sn = str(cfg.get(f"SN{slot}") or "").strip()
            if not sn or sn == "-":
                continue
            d = str(cfg.get(f"direction{slot}") or "").strip()
            hv = _hv(cfg.get(f"HV{slot}"))
            note = str(cfg.get("NOTE") or "").strip()
            wl = str(cfg.get("Wavelength") or "").strip()
            periods = hist.setdefault(sn, [])
            last = periods[-1] if periods else None
            same_hv = last and (hv is None or all(h is None or abs(h - hv) <= 1 for h in last["hvs"]))
            same_daq = last and (last["daq"] == e["daq"] or "?" in (last["daq"], e["daq"]))
            if last and (last["pos"], last["dir"]) == (pos, d) and same_hv and same_daq \
                    and last["last_idx"] == e["idx"] - 1:
                last.update(to=e["date"], n=last["n"] + 1, last_idx=e["idx"])
                if last["daq"] == "?":
                    last["daq"] = e["daq"]
            else:
                last = {"pos": pos, "dir": d, "daq": e["daq"], "hvs": [], "from": e["date"], "to": e["date"], "n": 1,
                        "notes": [], "wl": [], "last_idx": e["idx"]}
                periods.append(last)
            if hv is not None and hv not in last["hvs"]:
                last["hvs"].append(hv)
            if note and note not in last["notes"]:
                last["notes"].append(note)
            if wl and wl not in last["wl"]:
                last["wl"].append(wl)
    return hist


def running_backend(base_dir):
    """Backend of the scan being taken now, from the newest point in the newest scan map."""
    maps = sorted(glob.glob(os.path.join(base_dir, "LOG", "ScanHistory", "scanmap_*.json")))
    try:
        points = json.load(open(maps[-1])).values()
        newest = max(points, key=lambda v: v.get("time", ""))
    except (IndexError, OSError, ValueError, AttributeError):
        return "CAEN"
    return "HK Digitizer" if str(newest.get("file", "")).startswith("HK:") else "CAEN"

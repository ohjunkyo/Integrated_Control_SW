"""Information tab: PMT usage history (serial + cable direction, position, HV, dates)
built from the per-scan config snapshots in LOG/ScanHistory/history_*.json."""
import os
import glob
import json
import tkinter as tk
from tkinter import ttk
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


class InfoTab:
    COLS = [("pos", "Position", 90), ("daq", "DAQ", 110), ("hv", "HV [V]", 110), ("from", "From", 100), ("to", "To", 100),
            ("n", "Scans", 60), ("wl", "Wavelength [nm]", 160), ("notes", "Notes", 360)]

    def __init__(self, parent, controller):
        self.controller = controller
        self.base_dir = controller.base_dir
        frame = ttk.Frame(parent, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        head = ttk.Frame(frame)
        head.pack(fill=tk.X)
        ttk.Label(head, text="PMT history", font=("Helvetica", 14, "bold")).pack(side=tk.LEFT)
        ttk.Button(head, text="Refresh", command=self.refresh).pack(side=tk.RIGHT)
        self.filter_var = tk.StringVar()
        ent = ttk.Entry(head, textvariable=self.filter_var, width=18)
        ent.pack(side=tk.RIGHT, padx=(4, 12))
        ttk.Label(head, text="Filter PMT:").pack(side=tk.RIGHT)
        self.filter_var.trace_add("write", lambda *_a: self.render())
        ttk.Label(frame, text="One row per PMT (most-used HV); expand it for each period with the same "
                              "position, cable direction, DAQ and HV. Source: LOG/ScanHistory (scans started from this GUI); "
                              "DAQ is inferred for scans before it was recorded.",
                  foreground="#6c757d", font=("Helvetica", 9)).pack(anchor="w", pady=(2, 8))

        box = ttk.Frame(frame)
        box.pack(fill=tk.BOTH, expand=True)
        self.tree = ttk.Treeview(box, columns=[c[0] for c in self.COLS], show="tree headings")
        self.tree.heading("#0", text="PMT")
        self.tree.column("#0", width=150, stretch=False)
        for key, title, w in self.COLS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=w, stretch=(key == "notes"),
                             anchor="w" if key == "notes" else "center")
        vsb = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(box, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        box.grid_rowconfigure(0, weight=1)
        box.grid_columnconfigure(0, weight=1)
        self.tree.tag_configure("pmt", font=("Helvetica", 10, "bold"))
        self.status = ttk.Label(frame, text="", foreground="#6c757d", font=("Helvetica", 9))
        self.status.pack(anchor="w", pady=(6, 0))
        self.hist = {}
        self.refresh()

    def refresh(self):
        entries = load_scan_entries(self.base_dir)
        for i, e in enumerate(entries):
            e["idx"] = i
        self.hist = build_pmt_history(entries)
        span = f"{entries[0]['date']} – {entries[-1]['date']}" if entries else "no scans"
        self.status.config(text=f"{len(entries)} scans ({span}), {len(self.hist)} PMTs")
        self.render()

    def render(self):
        open_ids = {i[4:] for i in self.tree.get_children() if self.tree.item(i, "open")}
        self.tree.delete(*self.tree.get_children())
        flt = self.filter_var.get().strip().upper()
        rows = sorted(self.hist.items(), key=lambda kv: kv[1][-1]["to"], reverse=True)
        for sn, periods in rows:
            dirs = "/".join(dict.fromkeys(p["dir"] for p in periods if p["dir"]))
            label = sn + dirs
            if flt and flt not in label.upper():
                continue
            count = {}
            for p in periods:
                if p["hvs"]:
                    key = hv_text(p)
                    count[key] = count.get(key, 0) + p["n"]
            main_hv = max(count, key=count.get) if count else ""
            if count and count[main_hv] == 1 and len(count) > 1:   # no repeated setting (HV scan only)
                hvs = [h for p in periods for h in p["hvs"]]
                main_hv = f"{min(hvs)}–{max(hvs)}"
            pos = "/".join(dict.fromkeys(p["pos"] for p in periods))
            daq = "/".join(dict.fromkeys(p["daq"] for p in periods if p["daq"] != "?")) or "unknown"
            n = sum(p["n"] for p in periods)
            parent = self.tree.insert("", "end", iid=f"pmt:{sn}", text=label, open=sn in open_ids, tags=("pmt",),
                                      values=(pos, daq, main_hv, periods[0]["from"], periods[-1]["to"], n, "", ""))
            for p in periods:
                self.tree.insert(parent, "end", text=sn + p["dir"],
                                 values=(p["pos"], "unknown" if p["daq"] == "?" else p["daq"], hv_text(p) if p["hvs"] else "", p["from"], p["to"], p["n"],
                                         ", ".join(p["wl"]), "; ".join(p["notes"])))

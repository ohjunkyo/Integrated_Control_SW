#!/usr/bin/env python3
"""Append new rows from the logs listed in sources.json to one SQLite DB."""
import argparse
import csv
import fcntl
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import traceback
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_VERSION = 1
TS_NAMES = ("timestamp", "time", "ts", "datetime")
BASE_COLS = ("ts", "ts_epoch", "src")


def ex(p):
    return os.path.expanduser(p) if p else p


def log(cfg, msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    try:
        with open(ex(cfg["log"]), "a") as f:
            f.write(line + "\n")
    except OSError:
        pass
    if sys.stdout.isatty():
        print(line)


def col_name(raw):
    s = raw.strip().lower().replace("%", "pct")
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_") or "col"
    if s[0].isdigit() or s in BASE_COLS:
        s = "c_" + s
    return s


def parse_ts(text):
    """Local-time string -> (ts 'YYYY-MM-DD HH:MM:SS', epoch seconds UTC) or None."""
    try:
        dt = datetime.fromisoformat(text.strip().replace("T", " "))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt.isoformat(sep=" ", timespec="seconds"), int(dt.timestamp())


def value(text, nulls):
    if text is None:
        return None
    t = text.strip()
    if t == "" or t in nulls:
        return None
    if t in ("True", "true"):
        return 1
    if t in ("False", "false"):
        return 0
    try:
        return float(t)
    except ValueError:
        return t


class DB:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.con = sqlite3.connect(path, timeout=60)
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA synchronous=NORMAL")
        self.cols = {}
        self._core()

    def _core(self):
        c = self.con
        c.execute("CREATE TABLE IF NOT EXISTS schema_version (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                  "version INTEGER, applied TEXT, change TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS ingest_state (path TEXT PRIMARY KEY, source TEXT, inode INTEGER, "
                  "offset INTEGER, size INTEGER, mtime REAL, header TEXT, updated_epoch INTEGER)")
        c.execute("CREATE TABLE IF NOT EXISTS source_status (name TEXT PRIMARY KEY, last_ok_epoch INTEGER, "
                  "last_rows INTEGER, last_error TEXT, last_error_epoch INTEGER)")
        have = {r[1] for r in c.execute("PRAGMA table_info(source_status)")}
        for col in ("period_s", "data_epoch", "max_age_s"):   # run period and newest data, for the dashboard
            if col not in have:
                c.execute(f"ALTER TABLE source_status ADD COLUMN {col} INTEGER")
        if c.execute("SELECT COUNT(*) FROM schema_version WHERE version=?", (SCHEMA_VERSION,)).fetchone()[0] == 0:
            self.note(f"schema version {SCHEMA_VERSION}: core tables")
        c.commit()

    def note(self, change):
        self.con.execute("INSERT INTO schema_version (version, applied, change) VALUES (?, ?, ?)",
                         (SCHEMA_VERSION, f"{datetime.now():%Y-%m-%d %H:%M:%S}", change))

    def table(self, name, key=(), unique_on_ts=True):
        """Create the table, its indexes and its v_ view if missing."""
        if name in self.cols:
            return
        exists = self.con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        if not exists:
            extra = "".join(f', "{k}"' for k in key)
            self.con.execute(f'CREATE TABLE "{name}" (ts TEXT, ts_epoch INTEGER, src TEXT{extra})')
            self.con.execute(f'CREATE INDEX "ix_{name}_time" ON "{name}" (ts_epoch)')
            ucols = list(key) + (["ts_epoch"] if unique_on_ts else [])
            if ucols:
                self.con.execute(f'CREATE UNIQUE INDEX "ux_{name}" ON "{name}" ({", ".join(chr(34) + c + chr(34) for c in ucols)})')
            self.con.execute(f'CREATE VIEW IF NOT EXISTS "v_{name}" AS SELECT ts_epoch AS time, * FROM "{name}"')
            self.note(f"create table {name}")
        self.cols[name] = {r[1] for r in self.con.execute(f'PRAGMA table_info("{name}")')}

    def ensure(self, name, cols, rows=()):
        for c in cols:
            if c not in self.cols[name]:
                text = any(isinstance(r.get(c), str) for r in rows)   # declared type steers Grafana's parsing
                self.con.execute(f'ALTER TABLE "{name}" ADD COLUMN "{c}" {"TEXT" if text else "NUMERIC"}')
                self.cols[name].add(c)
                self.note(f"add column {name}.{c}")

    def insert(self, name, rows, mode="OR IGNORE"):
        """rows: list of dicts. -> rows actually written."""
        if not rows:
            return 0
        cols = sorted({k for r in rows for k in r})
        self.ensure(name, cols, rows)
        sql = (f'INSERT {mode} INTO "{name}" ({", ".join(chr(34) + c + chr(34) for c in cols)}) '
               f'VALUES ({", ".join("?" * len(cols))})')
        before = self.con.total_changes
        self.con.executemany(sql, [tuple(r.get(c) for c in cols) for r in rows])
        return self.con.total_changes - before

    def state(self, path):
        r = self.con.execute("SELECT inode, offset, size, mtime, header, updated_epoch FROM ingest_state WHERE path=?",
                             (path,)).fetchone()
        return dict(zip(("inode", "offset", "size", "mtime", "header", "updated_epoch"), r)) if r else None

    def set_state(self, path, source, **kw):
        cur = self.state(path) or {}
        cur.update(kw)
        self.con.execute("INSERT OR REPLACE INTO ingest_state VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (path, source, cur.get("inode"), cur.get("offset"), cur.get("size"), cur.get("mtime"),
                          cur.get("header"), int(time.time())))

    def status(self, name, rows=None, error=None, period=None, data_epoch=None, max_age=None):
        now = int(time.time())
        self.con.execute("INSERT OR IGNORE INTO source_status (name) VALUES (?)", (name,))
        if error is None:
            self.con.execute("UPDATE source_status SET last_ok_epoch=?, last_rows=?, last_error=NULL, "
                             "last_error_epoch=NULL, period_s=?, data_epoch=?, max_age_s=? WHERE name=?",
                             (now, rows, period, data_epoch, max_age, name))
        else:
            self.con.execute("UPDATE source_status SET last_error=?, last_error_epoch=? WHERE name=?",
                             (error[:500], now, name))
        self.con.commit()


# ---------- file sources ----------

def file_date(src, path):
    m = re.search(src["file_date"], os.path.basename(path)) if src.get("file_date") else None
    return m.group(1).replace("-", "") if m else None


def pick_files(src, cfg, backfill):
    since = cfg["since"].replace("-", "")
    recent = time.time() - cfg["recent_days"] * 86400
    out = []
    for p in sorted(glob.glob(ex(src["glob"]))):
        d = file_date(src, p)
        if d is not None and d < since:
            continue
        if not backfill and os.path.getmtime(p) < recent:
            continue
        out.append(p)
    return out


def name_fields(src, path):
    out = {}
    for k, rx in (src.get("name_fields") or {}).items():
        m = re.search(rx, os.path.basename(path))
        if m:
            out[k] = m.group(1)
    return out


def read_new(db, src, path):
    """-> (text of complete new lines, header or None, new state, reset); resets if the file was replaced."""
    st = os.stat(path)
    old = db.state(path)
    reset = old is None or old["inode"] != st.st_ino or st.st_size < (old["offset"] or 0)
    start = 0 if reset else old["offset"]
    if not reset and st.st_size == start:
        return "", old["header"], None, False
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read()
    cut = data.rfind(b"\n") + 1
    new_state = dict(inode=st.st_ino, offset=start + cut, size=st.st_size, mtime=st.st_mtime)
    return data[:cut].decode("utf-8", errors="replace"), (None if reset else old["header"]), new_state, reset


def derive(src, row):
    """Computed columns from sources.json "derived" (Python expressions over the row, math available)."""
    import math
    for name, expr in (src.get("derived") or {}).items():
        try:
            row[name] = eval(expr, {"__builtins__": {}, "sqrt": math.sqrt}, row)
        except Exception:
            row[name] = None
    for name, (lo, hi) in (src.get("valid") or {}).items():   # physically impossible values -> NULL
        v = row.get(name)
        if isinstance(v, (int, float)) and not lo <= v <= hi:
            row[name] = None


def parse_header(header):
    """-> (fields, column names, index of the time column)."""
    hdr = next(csv.reader([header])) if header else []
    return hdr, [col_name(h) for h in hdr], next((i for i, h in enumerate(hdr) if h.strip().lower() in TS_NAMES), 0)


def ingest_csv(db, cfg, src, backfill):
    db.table(src["table"], key=src.get("key", ()))
    nulls = set(src.get("null", []))
    since_epoch = int(datetime.fromisoformat(cfg["since"]).timestamp())
    total = 0
    for path in pick_files(src, cfg, backfill):
        text, header, new_state, _reset = read_new(db, src, path)
        if new_state is None:
            continue
        lines = text.splitlines()
        if header is None and lines:
            header = lines.pop(0)
        hdr, names, tcol = parse_header(header)
        fixed = name_fields(src, path)
        base = os.path.basename(path)
        rows, warned = [], False
        for line in lines:
            rec = next(csv.reader([line]), [])
            if rec and rec[0].strip().lower() in TS_NAMES:   # header rewritten mid-file
                header = line
                hdr, names, tcol = parse_header(header)
                continue
            if not rec or len(rec) <= tcol:
                continue
            t = parse_ts(rec[tcol])
            if t is None or t[1] < since_epoch:
                continue
            if len(rec) > len(names) and not warned:
                log(cfg, f"[WARN] {base}: {len(rec)} fields but {len(names)} header columns; extra ones kept by name")
                warned = True
            row = {"ts": t[0], "ts_epoch": t[1], "src": base, **fixed}
            extra = src.get("extra_cols", [])
            for i, v in enumerate(rec):
                if i == tcol:
                    continue
                k = i - len(names)
                name = names[i] if i < len(names) else (col_name(extra[k]) if k < len(extra) else f"col_{i + 1}")
                row[name] = value(v, nulls)
            derive(src, row)
            rows.append(row)
        total += db.insert(src["table"], rows)
        db.set_state(path, src["name"], header=header, **new_state)
        db.con.commit()
    return total


def ingest_text(db, cfg, src, backfill):
    db.table(src["table"], key=src.get("key", ()), unique_on_ts=False)
    rx = re.compile(src["pattern"])
    total = 0
    for path in pick_files(src, cfg, backfill):
        text, _h, new_state, reset = read_new(db, src, path)
        if new_state is None:
            continue
        base = os.path.basename(path)
        if reset:
            db.con.execute(f'DELETE FROM "{src["table"]}" WHERE src=?', (base,))
        fixed = name_fields(src, path)
        rows = []
        for line in text.splitlines():
            m = rx.match(line)
            if m:
                g = m.groupdict()
                t = parse_ts(g.pop("ts"))
                if t:
                    rows.append({"ts": t[0], "ts_epoch": t[1], "src": base, **fixed, **g})
            elif rows and line.strip():
                rows[-1]["message"] = rows[-1].get("message", "") + "\n" + line
        total += db.insert(src["table"], rows, mode="")
        db.set_state(path, src["name"], **new_state)
        db.con.commit()
    return total


def ingest_json(db, cfg, src, backfill):
    db.table(src["table"])
    with open(ex(src["path"])) as f:
        d = json.load(f)
    t = parse_ts(str(d.get(src.get("ts_field", "ts"), "")))
    if t is None:
        raise ValueError("no timestamp in " + src["path"])
    row = {"ts": t[0], "ts_epoch": t[1], "src": os.path.basename(src["path"])}
    for k, v in d.items():
        if k == src.get("ts_field", "ts") or isinstance(v, (dict, list)):
            continue
        row[col_name(k)] = int(v) if isinstance(v, bool) else v
    n = db.insert(src["table"], [row])
    db.con.commit()
    return n


def ingest_csv_table(db, cfg, src, backfill):
    """Whole file re-read when it changes; rows replaced by key (the run registry is edited in place)."""
    key = [col_name(k) for k in src["key"]]
    db.table(src["table"], key=key, unique_on_ts=False)
    path = ex(src["path"])
    st = os.stat(path)
    old = db.state(path)
    if old and old["mtime"] == st.st_mtime and old["size"] == st.st_size:
        return 0
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        recs = list(csv.reader(f))
    hdr, names = recs[0], [col_name(h) for h in recs[0]]
    tcol = names.index(col_name(src["ts_column"]))
    rows = []
    for rec in recs[1:]:
        if len(rec) < len(hdr):
            continue
        t = parse_ts(rec[tcol]) or (None, None)
        row = {"ts": t[0], "ts_epoch": t[1], "src": os.path.basename(path)}
        for i, v in enumerate(rec[:len(names)]):
            if i != tcol:
                row[names[i]] = value(v, ())
        rows.append(row)
    n = db.insert(src["table"], rows, mode="OR REPLACE")
    if src.get("post") == "classify_runs":
        db.table("laser")
        classify_runs(db)
    db.set_state(path, src["name"], inode=st.st_ino, offset=st.st_size, size=st.st_size, mtime=st.st_mtime)
    db.con.commit()
    return n


# ---------- run purpose and scan grouping ----------

SCAN_GAP_S = 1800      # consecutive general runs (same wavelength, run+1) closer than this form one scan
LD_WINDOW_S = 120      # laser log window around a run start for the LD on/off check
FULL_SCAN_POINTS = 46  # a general scan outside the Daily Routine is counted against the full grid
STEP_MAX_S = 12 * 3600  # a routine step without an end time covers runs for at most this long


def scan_steps(db):
    """General Scan steps of the Daily Routine, newest first: [(start, end, step, routine, points)]."""
    out = []
    for t in ("routine_step", "routine_now"):
        cols = {r[1] for r in db.con.execute(f'PRAGMA table_info("{t}")')}
        if not {"start_epoch", "end_epoch", "step", "routine", "kind"} <= cols:
            continue
        out += db.con.execute(f'SELECT start_epoch, end_epoch, step, routine, {"points" if "points" in cols else "NULL"} '
                              f'FROM "{t}" WHERE kind = \'general_scan\' AND start_epoch IS NOT NULL').fetchall()
    return sorted(out, key=lambda s: s[0], reverse=True)


def scan_kind(steps, t):
    """(label, expected points) of the scan whose first run was registered at t."""
    s = next((s for s in steps if s[0] <= t <= (s[1] or s[0] + STEP_MAX_S)), None)
    if s is None:
        return "", FULL_SCAN_POINTS
    name = f"{s[2]} {s[3]}".lower()
    label = "Short cross" if "cross" in name else "Fine scan" if "fine" in name else "Full scan"
    return label, int(s[4] or FULL_SCAN_POINTS)


def classify_runs(db):
    """daq_run.purpose, scan_id, scan_label and scan_points from kind/mode, the laser log, run timing and the routine."""
    db.ensure("daq_run", ["purpose", "scan_id", "scan_label", "scan_points"],
              [{"purpose": "", "scan_id": "", "scan_label": "", "scan_points": 0}])
    steps = scan_steps(db)
    rows = db.con.execute("SELECT rowid, ts_epoch, CAST(kind AS TEXT), CAST(mode AS TEXT), CAST(wavelength AS INT), "
                          "CAST(date AS INT), CAST(run AS INT) FROM daq_run WHERE ts_epoch IS NOT NULL "
                          "ORDER BY ts_epoch").fetchall()
    has_laser = "laser" in db.cols
    out, scan, prev, what = [], None, None, ("", None)
    for rowid, t, kind, mode, wl, date, run in rows:
        scan_id, label, points = None, "", None
        if kind == "general" and None in (date, run, wl):
            purpose = "Other"   # incomplete registry row: no scan grouping
        elif kind == "general":
            if not (prev and prev[1] == wl and t - prev[0] <= SCAN_GAP_S and run == prev[2] + 1):
                scan, what = f"{date}_{run:03d}_{wl}nm", scan_kind(steps, t)
            prev, scan_id, purpose, (label, points) = (t, wl, run), scan, "General scan", what
        elif kind == "hvscan":
            purpose = "HV scan"
        elif mode == "dark":
            purpose = "Self trigger (dark)"
        elif kind in ("manual", "stability", "dark") and mode == "laser":
            ld = db.con.execute("SELECT COUNT(*), MAX(ld_on) FROM laser WHERE ts_epoch BETWEEN ? AND ? AND wl = ?",
                                (t - LD_WINDOW_S, t + LD_WINDOW_S, f"{wl}nm")).fetchone() if has_laser else (0, None)
            if not ld[0]:
                purpose = "Other"
            elif ld[1] == 1:
                purpose = "Stability" if kind == "stability" else "Manual"
            else:
                purpose = "External trigger (LDs off)"
        else:
            purpose = "Other"
        out.append((purpose, scan_id, label, points, rowid))
    db.con.executemany("UPDATE daq_run SET purpose = ?, scan_id = ?, scan_label = ?, scan_points = ? WHERE rowid = ?", out)
    return len(out)


# ---------- probes ----------

HEALTH_PY = r'''
import json, os, subprocess, time
cfg = json.loads(%r)
def cpu():
    a = [int(x) for x in open("/proc/stat").readline().split()[1:]]
    time.sleep(0.5)
    b = [int(x) for x in open("/proc/stat").readline().split()[1:]]
    tot, idle = sum(b) - sum(a), (b[3] + b[4]) - (a[3] + a[4])
    return round(100.0 * (tot - idle) / tot, 1) if tot else None
mem = {l.split(":")[0]: int(l.split()[1]) for l in open("/proc/meminfo")}
la = os.getloadavg()
out = {"cpu_pct": cpu(), "load1": la[0], "load5": la[1], "load15": la[2], "ncpu": os.cpu_count(),
       "mem_used_pct": round(100.0 * (1 - mem["MemAvailable"] / mem["MemTotal"]), 1),
       "mem_used_gb": round((mem["MemTotal"] - mem["MemAvailable"]) / 1e6, 2), "mem_total_gb": round(mem["MemTotal"] / 1e6, 2),
       "swap_used_pct": round(100.0 * (1 - mem["SwapFree"] / mem["SwapTotal"]), 1) if mem.get("SwapTotal") else None,
       "uptime_h": round(float(open("/proc/uptime").read().split()[0]) / 3600, 1)}
rx = tx = 0
for l in open("/proc/net/dev").readlines()[2:]:
    name, data = l.split(":", 1)
    if name.strip() != "lo" and not name.strip().startswith(("tailscale", "docker", "veth")):
        f = data.split(); rx += int(f[0]); tx += int(f[8])
out["net_rx_bytes"], out["net_tx_bytes"] = rx, tx
temps = []
for z in os.listdir("/sys/class/thermal"):
    try:
        if open("/sys/class/thermal/%%s/type" %% z).read().strip() == "x86_pkg_temp":
            temps.append(int(open("/sys/class/thermal/%%s/temp" %% z).read()) / 1000.0)
    except OSError:
        pass
out["cpu_temp_c"] = max(temps) if temps else None
for name, path in cfg["disks"].items():
    try:
        s = os.statvfs(path)
        out["disk_%%s_pct" %% name] = round(100.0 * (1 - s.f_bavail / s.f_blocks), 1) if s.f_blocks else None
        out["disk_%%s_free_gb" %% name] = round(s.f_bavail * s.f_frsize / 1e9, 1)
        out["mount_%%s" %% name] = int(os.path.ismount(os.path.realpath(path)))
    except OSError:
        out["disk_%%s_pct" %% name] = None
ps = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout.splitlines()
for name, pat in cfg["procs"].items():
    out["proc_" + name] = int(any(pat in l for l in ps))
for name, path in cfg["files"].items():
    try:
        out["file_%%s_age_s" %% name] = int(time.time() - os.path.getmtime(os.path.expanduser(path)))
    except OSError:
        out["file_%%s_age_s" %% name] = None
for unit in cfg["units"]:
    r = subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True, text=True)
    out["unit_" + unit.replace("-", "_").replace(".", "_")] = int(r.stdout.strip() == "active")
vendors = []
for d in os.listdir("/sys/bus/usb/devices") if cfg.get("usb") else []:
    try:
        vendors.append(open("/sys/bus/usb/devices/%%s/idVendor" %% d).read().strip())
    except OSError:
        pass
for name, vendor in cfg.get("usb", {}).items():
    out["usb_" + name] = int(vendor in vendors)
for name, ip in cfg.get("ping", {}).items():
    out["ping_" + name] = int(subprocess.run(["ping", "-c", "1", "-W", "2", ip], capture_output=True).returncode == 0)
# CAEN HV behind a device server: one status GET, nothing is ever written to it.
BAD = ("overcurrent", "overvoltage", "undervoltage", "max_voltage", "trip", "max_power", "over_temperature", "killed")
for name, url in cfg.get("caen_hv", {}).items():
    try:
        import urllib.request
        chans = json.loads(urllib.request.urlopen(url, timeout=6).read())["channels"]
        st = [c.get("status", {}) for c in chans]
        out.update({name + "_link": 1, name + "_n_on": sum(bool(c.get("power_on")) for c in chans),
                    name + "_ramp": int(any(s.get("ramp_up") or s.get("ramp_down") for s in st)),
                    name + "_alarm": int(any(s.get(k) for s in st for k in BAD)),
                    name + "_interlock": int(any(s.get("interlock") for s in st))})
        for c in chans:
            out["%%s_ch%%s_v" %% (name, c["channel"])] = round(c["voltage_mon"], 1)
            out["%%s_ch%%s_i" %% (name, c["channel"])] = round(c["current_mon"], 2)
    except Exception:
        out[name + "_link"] = 0
# Run queue of the probed PC's own scheduler (read only): the running entry, the next pending one, the last log line.
for name, spec in cfg.get("sched", {}).items():
    try:
        q = json.load(open(os.path.expanduser(spec["queue"])))
        ent = q.get("entries", [])
        run = [e for e in ent if e.get("status") == "running"]
        pend = sorted((e for e in ent if e.get("status") == "pending"), key=lambda e: e.get("start_at") or "")
        out.update({name + "_running": int(bool(run)), name + "_name": run[0].get("name") if run else None,
                    name + "_paused": int(bool(q.get("paused"))), name + "_n_pending": len(pend),
                    name + "_next": pend[0].get("start_at") if pend else None,
                    name + "_next_name": pend[0].get("name") if pend else None})
    except Exception:
        out[name + "_running"] = None
    try:
        last = open(os.path.expanduser(spec["log"])).read().strip().splitlines()[-1]
        f = dict(p.split(": ", 1) for p in last.split("] ", 1)[1].split(" | ") if ": " in p)
        out.update({name + "_last": f.get("Run"), name + "_last_status": f.get("Status"),
                    name + "_last_end": last[1:20]})
    except Exception:
        pass
print(json.dumps(out))
'''


WIN_PS = r'''
$cfg = ConvertFrom-Json '%s'
$o = Get-CimInstance Win32_OperatingSystem
$out = [ordered]@{
  cpu_pct = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
  ncpu = [Environment]::ProcessorCount
  mem_used_pct = [math]::Round(100 * (1 - $o.FreePhysicalMemory / $o.TotalVisibleMemorySize), 1)
  mem_used_gb = [math]::Round(($o.TotalVisibleMemorySize - $o.FreePhysicalMemory) * 1024 / 1e9, 2)
  mem_total_gb = [math]::Round($o.TotalVisibleMemorySize * 1024 / 1e9, 2)
  uptime_h = [math]::Round(((Get-Date) - $o.LastBootUpTime).TotalHours, 1) }
$pf = Get-CimInstance Win32_PageFileUsage -ErrorAction SilentlyContinue | Measure-Object -Property CurrentUsage, AllocatedBaseSize -Sum
if ($pf -and $pf[1].Sum) { $out["swap_used_pct"] = [math]::Round(100 * $pf[0].Sum / $pf[1].Sum, 1) }
$ns = Get-NetAdapterStatistics -ErrorAction SilentlyContinue
if ($ns) { $out["net_rx_bytes"] = ($ns | Measure-Object -Property ReceivedBytes -Sum).Sum
           $out["net_tx_bytes"] = ($ns | Measure-Object -Property SentBytes -Sum).Sum }
foreach ($d in $cfg.disks.PSObject.Properties) {
  $ld = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$($d.Value)'"
  if ($ld) { $out["disk_$($d.Name)_pct"] = [math]::Round(100 * (1 - $ld.FreeSpace / $ld.Size), 1)
             $out["disk_$($d.Name)_free_gb"] = [math]::Round($ld.FreeSpace / 1e9, 1) } }
foreach ($p in $cfg.procs.PSObject.Properties) {
  $out["proc_$($p.Name)"] = [int][bool](Get-Process -Name $p.Value -ErrorAction SilentlyContinue) }
foreach ($t in $cfg.tasks.PSObject.Properties) {
  $i = Get-ScheduledTaskInfo -TaskName $t.Value -ErrorAction SilentlyContinue
  if ($i) { $out["task_$($t.Name)_result"] = $i.LastTaskResult
            $out["task_$($t.Name)_age_s"] = [int]((Get-Date) - $i.LastRunTime).TotalSeconds } }
foreach ($f in $cfg.files.PSObject.Properties) {
  $it = Get-Item $f.Value -ErrorAction SilentlyContinue
  if ($it) { $out["file_$($f.Name)_age_s"] = [int]((Get-Date) - $it.LastWriteTime).TotalSeconds } }
"JSON:" + ($out | ConvertTo-Json -Compress)
'''


def remote_cmd(src):
    """Command and stdin that print the health JSON on the probed PC."""
    keys = ("disks", "procs", "units", "files", "tasks", "usb", "ping", "caen_hv", "sched")
    conf = json.dumps({k: src.get(k, {} if k != "units" else []) for k in keys})
    if src.get("os") == "windows":
        import base64
        ps = WIN_PS % conf.replace("'", "''")
        enc = base64.b64encode(ps.encode("utf-16-le")).decode()
        return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", src["ssh"],
                f"powershell -NoProfile -NonInteractive -EncodedCommand {enc}"], None
    script = HEALTH_PY % conf
    if src.get("ssh"):
        return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", src["ssh"], "python3", "-"], script
    return [sys.executable, "-"], script


def probe_due(db, src):
    st = db.state("probe:" + src["name"])
    return st is None or time.time() - (st["updated_epoch"] or 0) >= src.get("every_s", 60) - 5


def net_rates(db, src, row):
    """Bytes/s from the cumulative counters of this host's previous sample."""
    if row.get("net_rx_bytes") is None or "net_rx_bytes" not in db.cols[src["table"]]:
        return {}
    prev = db.con.execute(f'SELECT ts_epoch, net_rx_bytes, net_tx_bytes FROM "{src["table"]}" WHERE host = ? '
                          "AND net_rx_bytes IS NOT NULL ORDER BY ts_epoch DESC LIMIT 1", (src["host"],)).fetchone()
    if not prev or row["ts_epoch"] <= prev[0]:
        return {}
    dt = row["ts_epoch"] - prev[0]
    out = {}
    for k, i in (("net_rx_bps", 1), ("net_tx_bps", 2)):
        cur = row["net_" + k[4:6] + "_bytes"]
        out[k] = (cur - prev[i]) / dt if prev[i] is not None and cur >= prev[i] else None
    return out


def probe_health(db, cfg, src, backfill):
    db.table(src["table"], key=["host"])
    if not probe_due(db, src):
        return 0
    cmd, script = remote_cmd(src)
    now = datetime.now()
    row = {"ts": now.isoformat(sep=" ", timespec="seconds"), "ts_epoch": int(now.timestamp()),
           "src": src["name"], "host": src["host"]}
    try:
        r = subprocess.run(cmd, input=script, capture_output=True, text=True, timeout=60)
        line = next(l for l in reversed(r.stdout.splitlines()) if l.lstrip("JSON:").startswith("{"))
        d = json.loads(line[5:] if line.startswith("JSON:") else line)
        row.update(d, up=1)
        keys = src.get("key_procs", [])
        row["key_procs_ok"] = int(all(d.get("proc_" + k, d.get("unit_" + k)) for k in keys))
        row.update(net_rates(db, src, row))
    except Exception as e:
        row.update(up=0, key_procs_ok=0, error=str(e)[:200])
    n = db.insert(src["table"], [row])
    db.set_state("probe:" + src["name"], src["name"])
    db.con.commit()
    return n


def probe_daq_status(db, cfg, src, backfill):
    db.table(src["table"])
    if not probe_due(db, src):
        return 0
    ps = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout.splitlines()
    now = datetime.now()
    row = {"ts": now.isoformat(sep=" ", timespec="seconds"), "ts_epoch": int(now.timestamp()), "src": src["name"],
           "scan_running": int(scan_running(cfg, ps))}
    try:
        with open(src.get("active_scan_file", "/tmp/daq_flags/active_scan")) as f:
            row["active_scan"] = f.read().strip()[:200] or None
    except OSError:
        row["active_scan"] = None
    for name, pat in src.get("procs", {}).items():
        row["proc_" + name] = int(any(pat in l for l in ps))
    if src.get("hv_db"):
        try:
            row.update(hv_latest(src["hv_db"]))
        except Exception as e:
            row["hv_state"], row["hv_state_code"], row["hv_error"] = "NODATA", 4, str(e)[:200]
    for label, fn, arg in (("v2_lamps", v2_lamps, src.get("v2_dir")), ("v2_scan", v2_scan, src.get("v2_dir")),
                           ("locks", locks, src.get("locks"))):
        if arg:
            try:
                row.update(fn(ex(arg) if isinstance(arg, str) else arg))
            except Exception as e:
                row[label + "_error"] = f"{type(e).__name__}: {e}"[:200]
    for name, spec in src.get("ok_logs", {}).items():
        path, marker = (spec, "[OK]") if isinstance(spec, str) else (spec["path"], spec["marker"])
        try:
            row[name + "_last_ok"] = last_ok(path, marker)
        except OSError:
            row[name + "_last_ok"] = None
    for name, addr in src.get("tcp", {}).items():
        row.update(tcp_check(db, src["table"], name, addr, row["ts_epoch"]))
    for name, vendor in src.get("usb", {}).items():   # device present on this PC's USB bus
        row[name + "_connected"] = int(any(open(f).read().strip() == vendor
                                           for f in glob.glob("/sys/bus/usb/devices/*/idVendor")))
    for name in src.get("not_yet", []):   # reserved lamp without a source yet: NULL shows as N/A
        row[name + "_connected"] = None
    for name, host in src.get("ping", {}).items():   # ICMP only: never open the board's data port
        row[name + "_connected"] = int(subprocess.run(["ping", "-c", "1", "-W", "1", host],
                                                      capture_output=True).returncode == 0)
    n = db.insert(src["table"], [row])
    db.set_state("probe:" + src["name"], src["name"])
    db.con.commit()
    return n


def tcp_check(db, table, name, addr, now):
    """<name>_up from a 2 s TCP connect to host:port (the v2 lamp's test); <name>_last_ok carried over on failure."""
    import socket
    host, port = addr.rsplit(":", 1)
    try:
        with socket.create_connection((host, int(port)), timeout=2.0):
            return {name + "_up": 1, name + "_last_ok": now}
    except OSError:
        prev = None
        if name + "_last_ok" in db.cols[table]:
            prev = db.con.execute(f'SELECT MAX("{name}_last_ok") FROM "{table}"').fetchone()[0]
        return {name + "_up": 0, name + "_last_ok": prev}


# HV state code, also used by grafana/build_dashboard.py: 0 ON, 1 OFF, 2 READERR, 3 ILK/ALARM, 4 NODATA.
HV_CODES = {0: "ON", 1: "OFF", 2: "READERR", 3: "ILK/ALARM", 4: "NODATA"}
_STATS = [f"Ch{c}_Stat" for c in range(4)]
READERR_SQL = "(" + " AND ".join(f"{c} = 4096" for c in _STATS) + " AND " + \
    " AND ".join(f"COALESCE(Ch{c}_V, 0) = 0" for c in range(4)) + ")"
ALARM_MASK = 7128   # OVC OVV MAXV TRIP OVP OVT KILL ILK; not DIS (Ch3 is always disabled) or UNV
HV_CODE_SQL = f"""CASE
  WHEN {" AND ".join(f"{c} IS NULL" for c in _STATS)} THEN 4
  WHEN {READERR_SQL} THEN 2
  WHEN ({" | ".join(f"COALESCE(CAST({c} AS INT), 0)" for c in _STATS)}) & {ALARM_MASK} THEN 3
  WHEN ({" | ".join(f"COALESCE(CAST({c} AS INT), 0)" for c in _STATS[:3])}) & 1 THEN 0
  ELSE 1 END"""
HV_GOOD_SQL = f"((Ch0_Stat IS NOT NULL OR Ch1_Stat IS NOT NULL OR Ch2_Stat IS NOT NULL) AND NOT {READERR_SQL})"


def hv_latest(path):
    """Latest HV row (read-only) -> state, age and channel voltages."""
    con = sqlite3.connect(f"file:{ex(path)}?mode=ro", uri=True, timeout=10)
    try:
        r = con.execute(f"SELECT timestamp, {HV_CODE_SQL}, Ch0_V, Ch1_V, Ch2_V FROM monitoring_data "
                        "ORDER BY timestamp DESC LIMIT 1").fetchone()
        good = con.execute(f"SELECT timestamp FROM monitoring_data WHERE {HV_GOOD_SQL} "
                           "ORDER BY timestamp DESC LIMIT 1").fetchone()
    finally:
        con.close()
    if not r:
        return {"hv_state": "NODATA", "hv_state_code": 4}
    t = parse_ts(r[0])
    g = parse_ts(good[0]) if good else None
    now = int(time.time())
    return {"hv_state": HV_CODES[r[1]], "hv_state_code": r[1], "hv_age_s": now - t[1] if t else None,
            "hv_last_good": g[1] if g else None, "hv_last_good_age_s": now - g[1] if g else None,
            "hv_ch0_v": r[2], "hv_ch1_v": r[3], "hv_ch2_v": r[4]}


def v2_lamps(v2_dir):
    """The v2 status lamps, judged by v2's own core.status thresholds."""
    if v2_dir not in sys.path:
        sys.path.insert(0, v2_dir)
    from core import status
    out = {}
    for name, (ok, detail) in status.system_status().items():
        if name == "DAQ System":   # needs v2's hardware probe; scan_running and locks cover it
            continue
        key = "lamp_" + col_name(name)
        out[key], out[key + "_detail"] = int(bool(ok)), str(detail)[:200]
    return out


def v2_scan(v2_dir):
    """Running General Scan progress from v2's core.scan_progress."""
    if v2_dir not in sys.path:
        sys.path.insert(0, v2_dir)
    from core import scan_progress
    snap = scan_progress.snapshot()
    if not snap.get("running"):
        return {"scan_done": None, "scan_total": None, "scan_tag": "", "scan_block_wl": ""}   # "" makes TEXT columns
    block = snap.get("block") or (None, None, None)
    return {"scan_done": len(snap.get("done") or []), "scan_total": snap.get("total"), "scan_tag": snap.get("tag"),
            "scan_block_wl": block[2]}


def locks(names):
    import instance_lock
    return {"lock_" + n: int(instance_lock.is_running(n)) for n in names}


TS_IN_LINE = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}")


def last_ok(path, marker="[OK]"):
    """Epoch of the last line containing marker in a script log (reads only the tail)."""
    with open(ex(path), "rb") as f:
        f.seek(max(0, os.path.getsize(ex(path)) - 65536))
        lines = f.read().decode("utf-8", errors="replace").splitlines()
    for line in reversed(lines):
        if marker in line:
            m = TS_IN_LINE.search(line)
            t = parse_ts(m.group(0)) if m else None
            return t[1] if t else None
    return None


HK_PY = r'''
import glob, json, os, re
cfg = json.loads(%r)
out = []
for d in sorted(glob.glob(os.path.join(os.path.expanduser(cfg["dir"]), "[0-9]" * 8))):
    if os.path.basename(d) < cfg["since"]:
        continue
    for m in glob.glob(os.path.join(d, "*.metadata.yaml")):
        meta = {}
        for line in open(m, errors="replace"):
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip().strip("'\"")
        base = m[:-len(".metadata.yaml")]
        b = os.stat(base + ".bin") if os.path.exists(base + ".bin") else None
        out.append({"date": os.path.basename(d), "name": os.path.basename(base), "meta": meta,
                    "bin_mtime": b.st_mtime if b else None, "bin_bytes": b.st_size if b else None})
print("JSON:" + json.dumps(out))
'''
HK_NAME = re.compile(r"^(MAN_)?(Run|run)(\d+).*?(?:_T(-?\d+)_1R(-?\d+)_2R(-?\d+))?-(\d+)-[\d.]+mV-Gate(\d+)$")


def probe_hk_runs(db, cfg, src, backfill):
    """HK Digitizer runs from the *.metadata.yaml files the RunManager writes on the HK PC."""
    db.table(src["table"], key=["date", "run_id"], unique_on_ts=False)   # test names (run001...) repeat per day
    if not (backfill or probe_due(db, src)):
        return 0
    since = cfg["since"].replace("-", "") if backfill else \
        datetime.fromtimestamp(time.time() - cfg["recent_days"] * 86400).strftime("%Y%m%d")
    script = HK_PY % json.dumps({"dir": src["dir"], "since": since})
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", src["ssh"], "python3", "-"],
                       input=script, capture_output=True, text=True, timeout=60)
    line = next((l for l in reversed(r.stdout.splitlines()) if l.startswith("JSON:")), None)
    if line is None:
        raise RuntimeError((r.stderr or r.stdout).strip()[-200:] or f"ssh exit {r.returncode}")
    rows = []
    for f in json.loads(line[5:]):
        meta, m = f["meta"], HK_NAME.match(f["name"])
        t = parse_ts(meta.get("TimeStamp", "")) or (None, None)
        if t[1] is None:
            continue
        length = value(meta.get("RunLength", ""), ())
        rows.append({"ts": t[0], "ts_epoch": t[1], "src": src["name"], "run_id": f["name"], "date": f["date"],
                     "run": int(m.group(3)) if m else None,
                     "kind": "dummy" if meta.get("RunMode") == "Dummy" else
                             ("manual" if m.group(1) else "scan" if m.group(2) == "Run" else "test") if m else "other",
                     "mode": meta.get("RunMode") or None, "subrun": int(m.group(7)) if m else None,
                     "tilt2": value(m.group(4) or "", ()) if m else None, "rot2": value(m.group(5) or "", ()) if m else None,
                     "rot3": value(m.group(6) or "", ()) if m else None, "gate": int(m.group(8)) if m else None,
                     "length_s": length, "trigger_ch": meta.get("TriggerChannel") or None,
                     "threshold_mv": value(meta.get("Threshold_mV", ""), ()),
                     "end_epoch": int(f["bin_mtime"]) if f["bin_mtime"] else None,
                     "bin_mb": round(f["bin_bytes"] / 1e6, 1) if f["bin_bytes"] is not None else None,
                     "purpose": "HK Digitizer"})
    n = db.insert(src["table"], rows, mode="OR REPLACE")
    db.set_state("probe:" + src["name"], src["name"])
    db.con.commit()
    return n


AUS_PY = r"""
import glob, json, os, re, time
cfg = json.loads(%r)
ep = lambda text: int(time.mktime(time.strptime(text, "%%Y-%%m-%%d %%H:%%M:%%S")))
out = {"runs": [], "stage": []}
try:
    for line in open(os.path.expanduser(cfg["log"]), errors="replace"):
        m = re.match(r"\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] (.*)", line.strip())
        if m and ep(m.group(1)) >= cfg["since"]:
            out["runs"].append({"end": ep(m.group(1)), "f": dict(p.split(": ", 1) for p in m.group(2).split(" | ") if ": " in p)})
except OSError:
    out["log_error"] = 1
pat = re.compile(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\S* .*?(?:(Linear track verified at PMT(\d) \((\d+)mm\))|"
                 r"(Linear stage -> (\d+)mm \(code: (-?\d+)\))|(Starting linear stage move: PMT(\d) to PMT(\d))|"
                 r"(linear stage move: PMT(\d) to PMT(\d) completed successfully))")
for p in sorted(glob.glob(os.path.join(os.path.expanduser(cfg["dir"]), "*", "logs_*", "terminal_*.log"))):
    if os.path.getmtime(p) < cfg["since"]:
        continue
    folder = os.path.basename(os.path.dirname(os.path.dirname(p)))
    for line in open(p, errors="replace"):
        m = pat.match(line) if "inear" in line else None
        if not m:
            continue
        g, e = m.groups(), {"t": ep(m.group(1)), "folder": folder}
        if g[1]:
            e.update(event="verified", pmt=int(g[2]), mm=int(g[3]))
        elif g[4]:
            e.update(event="move_cmd", mm=int(g[5]), code=int(g[6]))
        elif g[7]:
            e.update(event="move_start", from_pmt=int(g[8]), to_pmt=int(g[9]))
        else:
            e.update(event="move_done", from_pmt=int(g[11]), pmt=int(g[12]), to_pmt=int(g[12]))
        out["stage"].append(e)
print("JSON:" + json.dumps(out))
"""
AUS_DUR = re.compile(r"(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?$")


def probe_aus_runs(db, cfg, src, backfill):
    """AUS runs (run_log.txt) and linear stage events (terminal logs) from the AUS PC, read only."""
    db.table(src["table"], key=["run"])
    db.table(src["stage_table"], key=["folder", "event"])
    if not (backfill or probe_due(db, src)):
        return 0
    first = db.con.execute(f'SELECT COUNT(*) FROM "{src["stage_table"]}"').fetchone()[0] == 0   # empty table: read all
    since = int(parse_ts(cfg["since"])[1] if backfill or first else time.time() - cfg["recent_days"] * 86400)
    script = AUS_PY % json.dumps({"log": src["log"], "dir": src["dir"], "since": since})
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", src["ssh"], "python3", "-"],
                       input=script, capture_output=True, text=True, timeout=60)
    line = next((l for l in reversed(r.stdout.splitlines()) if l.startswith("JSON:")), None)
    if line is None:
        raise RuntimeError((r.stderr or r.stdout).strip()[-200:] or f"ssh exit {r.returncode}")
    d = json.loads(line[5:])
    stamp = lambda t: {"ts": datetime.fromtimestamp(t).isoformat(sep=" ", timespec="seconds"), "ts_epoch": t,
                       "src": src["name"]}
    pmt_at = {mm: pmt for pmt, mm in src.get("pmt_mm", {}).items()}
    stage = []
    for e in d["stage"]:
        t = e.pop("t")
        if e["event"] == "move_cmd" and e.get("code") == 0:
            e["pmt"] = int(pmt_at[e["mm"]]) if e["mm"] in pmt_at else None   # commanded position reached
        elif e["event"] == "move_done":
            e["mm"] = src.get("pmt_mm", {}).get(str(e["pmt"]))
        stage.append(dict(stamp(t), **e))
    n = db.insert(src["stage_table"], stage)
    db.con.commit()
    rows = []
    for x in d["runs"]:
        f, m = x["f"], AUS_DUR.match(x["f"].get("Duration", "").strip())
        dur = sum(int(v) * k for v, k in zip(m.groups(), (3600, 60, 1)) if v) if m and any(m.groups()) else None
        pos = db.con.execute(f'SELECT pmt FROM "{src["stage_table"]}" WHERE ts_epoch <= ? AND pmt IS NOT NULL '
                             "ORDER BY ts_epoch DESC, rowid DESC LIMIT 1", (x["end"],)).fetchone()
        rows.append(dict(stamp(x["end"]), run=f.get("Run"), type=f.get("Type"), pmts=f.get("PMTs"), hv=f.get("HV"),
                         status=f.get("Status"), duration_s=dur, start_epoch=x["end"] - dur if dur is not None else None,
                         end_epoch=x["end"], stage_pmt=pos[0] if pos else None))
    n += db.insert(src["table"], rows, mode="OR REPLACE")
    db.set_state("probe:" + src["name"], src["name"])
    db.con.commit()
    return n


AUS_TELE_PY = r"""
import csv, glob, json, os, time
cfg = json.loads(%r)
out = {"rows": [], "queue": None}
for p in sorted(glob.glob(os.path.join(os.path.expanduser(cfg["dir"]), "*", "logs_*", "telemetry_*.csv"))):
    acc = {}
    try:
        if os.path.getmtime(p) < cfg["since"]:
            continue
        for row in csv.DictReader(open(p, errors="replace")):
            try:
                t = int(time.mktime(time.strptime((row.get("timestamp") or "")[:19], "%%Y-%%m-%%dT%%H:%%M:%%S"))) // 60 * 60
            except ValueError:
                continue
            if t < cfg["since"]:
                continue
            a = acc.setdefault(t, {})
            for k, v in row.items():
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    continue
                if k and v == v:
                    s = a.setdefault(k, [0.0, 0])
                    s[0] += v
                    s[1] += 1
    except OSError:
        continue
    folder = os.path.basename(os.path.dirname(os.path.dirname(p)))
    out["rows"] += [dict({k: round(s[0] / s[1], 4) for k, s in a.items()}, t=t, folder=folder) for t, a in sorted(acc.items())]
try:
    q = json.load(open(os.path.expanduser(cfg["queue"])))
    par = lambda e: e.get("params") or {}
    out["queue"] = {"paused": bool(q.get("paused")), "reason": q.get("pause_reason") or "", "entries": [
        {"name": e.get("name"), "type": e.get("sequence_type"), "serial": par(e).get("serial"),
         "pmt_num": par(e).get("pmt_num"), "start_at": e.get("start_at"), "status": e.get("status"),
         "message": e.get("message"), "started_at": e.get("started_at"), "finished_at": e.get("finished_at"),
         "est_s": e.get("est_duration_s")} for e in q.get("entries", [])]}
except (OSError, ValueError):
    pass
print("JSON:" + json.dumps(out))
"""


def probe_aus_telemetry(db, cfg, src, backfill):
    """AUS HV / laser telemetry (per-run CSV, 1 min averages) and the AUS scheduler queue, read only."""
    db.table(src["table"])
    db.table(src["sched_table"], key=["idx"], unique_on_ts=False)
    if not (backfill or probe_due(db, src)):
        return 0
    last = db.con.execute(f'SELECT MAX(ts_epoch) FROM "{src["table"]}"').fetchone()[0]
    since = int(parse_ts(cfg["since"])[1] if backfill or last is None else last - 600)   # re-read the open minute
    script = AUS_TELE_PY % json.dumps({"dir": src["dir"], "queue": src["queue"], "since": since})
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", src["ssh"], "python3", "-"],
                       input=script, capture_output=True, text=True, timeout=90)
    line = next((l for l in reversed(r.stdout.splitlines()) if l.startswith("JSON:")), None)
    if line is None:
        raise RuntimeError((r.stderr or r.stdout).strip()[-200:] or f"ssh exit {r.returncode}")
    d = json.loads(line[5:])
    stamp = lambda t: {"ts": datetime.fromtimestamp(t).isoformat(sep=" ", timespec="seconds"), "ts_epoch": t,
                       "src": src["name"]}
    n = db.insert(src["table"], [dict(stamp(x.pop("t")), **x) for x in d["rows"]], mode="OR REPLACE")
    if d.get("queue") is not None:   # queue snapshot: rows replaced each pass
        q, rows = d["queue"], []
        when = lambda text: parse_ts((text or "")[:19].replace("T", " ")) or (None, None)
        for i, e in enumerate(q["entries"]):
            plan, t0, t1 = when(e["start_at"]), when(e["started_at"]), when(e["finished_at"])
            rows.append(dict(stamp(int(time.time())), idx=i, name=e["name"], type=(e["type"] or "").split(" [")[0],
                             serial=e["serial"], stage_pmt=e["pmt_num"], status=e["status"], note=e["message"] or "",
                             plan=plan[0], plan_epoch=plan[1], started=t0[0], start_epoch=t0[1], finished=t1[0],
                             end_epoch=t1[1], est_s=e["est_s"], paused=int(q["paused"]), pause_reason=q["reason"]))
        db.con.execute(f'DELETE FROM "{src["sched_table"]}"')
        n += db.insert(src["sched_table"], rows, mode="OR REPLACE")
    db.set_state("probe:" + src["name"], src["name"])
    db.con.commit()
    return n


def routine_steps(steps, routine, extra=None):
    """Daily Routine step dicts -> rows (ts = step start, else the routine start in extra)."""
    rows = []
    for i, s in enumerate(steps):
        t0, t1 = parse_ts(s.get("started") or "") or (None, None), parse_ts(s.get("finished") or "") or (None, None)
        base = t0 if t0[1] is not None else parse_ts((extra or {}).get("run_start") or "") or (None, None)
        opts, points = s.get("options") or {}, None
        if s.get("kind") == "general_scan":   # points the scan is expected to take
            angles = opts.get("angles")
            points = len(angles) * int(opts.get("axes") or 2) if angles else opts.get("points")
        rows.append(dict(extra or {}, ts=base[0], ts_epoch=base[1], routine=routine, idx=i, step=s.get("name"), points=points,
                         kind=s.get("kind"), enabled=1 if s.get("enabled") else 0, plan=s.get("start"),
                         status=s.get("status"), started=s.get("started"), finished=s.get("finished"),
                         start_epoch=t0[1], end_epoch=t1[1], note=s.get("note") or ""))
    return rows


def probe_routine(db, cfg, src, backfill):
    """Daily Routine of DAQ Control v2: _current.json (rows replaced each pass) and the finished runs in history/."""
    if not (backfill or probe_due(db, src)):
        return 0
    d = ex(src["dir"])
    for t, key in (("routine_now", ["idx"]), ("routine_run", ["run_start"]), ("routine_step", ["run_start", "idx"])):
        db.table(t, key=key, unique_on_ts=False)
    n = 0
    with open(os.path.join(d, "_current.json"), encoding="utf-8") as f:
        cur = json.load(f)
    rows = routine_steps(cur.get("steps", []), cur.get("name"))
    stamp = parse_ts(datetime.fromtimestamp(os.path.getmtime(os.path.join(d, "_current.json"))).isoformat())
    for r in rows:
        r.update(src=src["name"], saved=stamp[0])
        if r["ts_epoch"] is None:
            r["ts"], r["ts_epoch"] = stamp
    db.con.execute('DELETE FROM "routine_now"')
    n += db.insert("routine_now", rows, mode="OR REPLACE")
    runs, steps = [], []
    for p in sorted(glob.glob(os.path.join(d, src.get("history", "history"), "*.json"))):
        try:
            with open(p, encoding="utf-8") as f:
                h = json.load(f)
        except (OSError, ValueError):
            continue
        t0, t1 = parse_ts(h.get("started_at") or ""), parse_ts(h.get("finished_at") or "") or (None, None)
        if t0 is None:
            continue
        ss = routine_steps(h.get("steps", []), h.get("name"), {"run_start": t0[0], "src": os.path.basename(p)})
        on = [s for s in ss if s["enabled"]]
        count = lambda st: sum(1 for s in on if s["status"] == st)
        runs.append({"ts": t0[0], "ts_epoch": t0[1], "src": os.path.basename(p), "run_start": t0[0],
                     "routine": h.get("name"), "finished_at": h.get("finished_at"), "end_epoch": t1[1],
                     "result": h.get("result"), "n_steps": len(on), "n_done": count("Done"),
                     "n_failed": count("Failed"), "n_skipped": count("Skipped"),
                     "failed_steps": ", ".join(s["step"] or "?" for s in on if s["status"] == "Failed")})
        steps += ss
    n += db.insert("routine_run", runs, mode="OR REPLACE")
    n += db.insert("routine_step", steps, mode="OR REPLACE")
    db.set_state("probe:" + src["name"], src["name"])
    db.con.commit()
    return n


HANDLERS = {"csv": ingest_csv, "text": ingest_text, "json": ingest_json, "csv_table": ingest_csv_table}
PROBES = {"health": probe_health, "daq_status": probe_daq_status, "hk_runs": probe_hk_runs, "routine": probe_routine,
          "aus_runs": probe_aus_runs, "aus_telemetry": probe_aus_telemetry}


def newest(db, src):
    """Newest ts_epoch this source wrote (probe rows carry src = name, file rows the file name)."""
    t = src["table"]
    if t not in db.cols:
        return None
    where = ' WHERE src = ?' if src["kind"] == "probe" else ''
    return db.con.execute(f'SELECT MAX(ts_epoch) FROM "{t}"{where}', (src["name"],) if where else ()).fetchone()[0]


def scan_running(cfg, ps=None):
    """A scan or DAQ process (cfg scan_pattern) other than the `-j` connection probe."""
    if ps is None:
        ps = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout.splitlines()
    scan = re.compile(cfg["scan_pattern"])
    # Match the program and its script argument only: a shell whose command text merely mentions the pattern is not a scan.
    return any(scan.search(" ".join(l.split()[:2])) and " -j" not in l for l in ps)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "sources.json"))
    ap.add_argument("--db")
    ap.add_argument("--backfill", action="store_true", help="read every file since 'since' (skipped during a scan)")
    ap.add_argument("--only", help="comma-separated source names")
    ap.add_argument("--include-disabled", action="store_true")
    a = ap.parse_args()
    with open(a.config) as f:
        cfg = json.load(f)
    dbpath = ex(a.db or cfg["db"])
    if a.backfill and scan_running(cfg):
        log(cfg, "[SKIP] backfill: a scan is running")
        return 0
    lock = open(dbpath + ".lock", "w") if os.path.isdir(os.path.dirname(dbpath)) else None
    if lock is None:
        os.makedirs(os.path.dirname(dbpath), exist_ok=True)
        lock = open(dbpath + ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0
    db = DB(dbpath)
    only = set(a.only.split(",")) if a.only else None
    failed = 0
    for src in cfg["sources"]:
        if only and src["name"] not in only:
            continue
        if src.get("enabled", True) is False and not (a.include_disabled or (only and src["name"] in only)):
            continue
        fn = PROBES[src["probe"]] if src["kind"] == "probe" else HANDLERS[src["kind"]]
        t0 = time.time()
        try:
            n = fn(db, cfg, src, a.backfill)
            db.status(src["name"], rows=n, period=src.get("every_s", 60), data_epoch=newest(db, src),
                      max_age=src.get("max_age_s"))
            if a.backfill or sys.stdout.isatty():
                log(cfg, f"[OK] {src['name']}: {n} rows in {time.time() - t0:.1f} s")
        except Exception as e:
            failed += 1
            db.con.rollback()
            msg = f"{type(e).__name__}: {e}"
            prev = db.con.execute("SELECT last_error, last_error_epoch FROM source_status WHERE name=?",
                                  (src["name"],)).fetchone()
            if not prev or prev[0] != msg[:500] or time.time() - (prev[1] or 0) > 3600:   # same error: hourly
                log(cfg, f"[FAIL] {src['name']}: {msg}")
                db.status(src["name"], error=msg)
            if sys.stdout.isatty():
                traceback.print_exc()
    db.con.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

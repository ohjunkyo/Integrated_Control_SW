"""Device / acquisition status read from what the running apps already write:
lock files, log freshness, the HV monitor's SQLite DB. Read-only, no widgets."""
import glob
import json
import os
import sqlite3
import subprocess
import threading
import time
from datetime import datetime

from core import env

HV_CHANNELS = [(0, "Monitor"), (1, "Rot1"), (2, "Rot2"), (3, "Ch3")]
READERR = "(Ch0_Stat = 4096 AND Ch1_Stat = 4096 AND Ch2_Stat = 4096 AND Ch3_Stat = 4096)"   # HV worker's placeholder
HV_GOOD = f"(Ch0_Stat IS NOT NULL OR Ch1_Stat IS NOT NULL OR Ch2_Stat IS NOT NULL) AND NOT {READERR}"


def _env_names():
    """Dark-box column -> display name, from the HV monitor's sensor config."""
    names = {}
    try:
        cfg = json.load(open(os.path.join(os.path.dirname(env.HV_DB), "config_precal.json")))
        for s in cfg["arduino_settings"]["sensors"]:
            names[s["name"].replace(" #", "_").replace(" ", "_")] = s.get("display_name", s["name"])
    except Exception:
        pass
    return names


def _latest_csv_row(pattern):
    files = sorted(glob.glob(pattern), key=os.path.getmtime)
    if not files:
        return None, None
    path = files[-1]
    try:
        with open(path, "rb") as f:
            header = f.readline().decode(errors="replace").strip().split(",")
            f.seek(max(f.tell(), os.path.getsize(path) - 4096))   # only the tail is needed
            lines = [ln for ln in f.read().decode(errors="replace").splitlines() if ln.strip()]
        if not lines:
            return None, path
        return dict(zip(header, lines[-1].strip().split(","))), path
    except OSError:
        return None, path


def device_rows():
    """[(name, text, ok)] per device for the Overview tooltips; ok None = neutral."""
    env_names = _env_names()
    rows = []
    if scan_running():
        rows.append(("DAQ", "scan running", True))
    else:
        import instance_lock
        up = instance_lock.is_running("daq")
        rows.append(("DAQ", "Tk app open, idle" if up else "Tk app not running", up))

    try:
        con = sqlite3.connect(f"file:{env.HV_DB}?mode=ro", uri=True, timeout=1)
        con.row_factory = sqlite3.Row
        r = con.execute("SELECT * FROM monitoring_data ORDER BY timestamp DESC LIMIT 1").fetchone()
        con.close()
    except Exception:
        r = None
    fresh = r is not None and time.time() - datetime.fromisoformat(r["timestamp"]).timestamp() <= 60
    if r is None:
        rows.append(("HV", "no data", False))
    elif all(r[f"Ch{ch}_Stat"] == 4096 for ch, _n in HV_CHANNELS):
        rows.append(("HV", "read error", False))
    else:
        for ch, name in HV_CHANNELS:
            v, i, st = r[f"Ch{ch}_V"], r[f"Ch{ch}_I_H"], r[f"Ch{ch}_Stat"]
            if v is None:
                continue
            if st is None:   # ChStatus could not be read
                rows.append((f"HV {name}", f"{v:.0f} V  status unreadable", False))
                continue
            st = int(st)
            if st & 1:
                rows.append((f"HV {name}", f"{v:.0f} V  {i:.1f} uA", fresh))
            else:   # off or disabled is not a fault: neutral lamp
                rows.append((f"HV {name}", "disabled" if st & 1024 else "OFF", None))
        for col, disp in sorted(env_names.items(), key=lambda kv: kv[1]):
            t, h = r[f"{col}_T"] if f"{col}_T" in r.keys() else None, r[f"{col}_H"] if f"{col}_H" in r.keys() else None
            if t is not None:
                rows.append((f"Env {disp}", f"{t:.1f} C  {h:.0f} %RH" if h is not None else f"{t:.1f} C", fresh))

    latest = {}
    for path in glob.glob(os.path.join(env.ADC_DIR, "LOG", "LASER", "laser_data_*nm_*.csv")):
        wl = os.path.basename(path).split("_")[2]
        if wl not in latest or os.path.getmtime(path) > os.path.getmtime(latest[wl]):
            latest[wl] = path
    laser_rows = []
    for wl, path in sorted(latest.items()):
        if time.time() - os.path.getmtime(path) > 60:
            continue
        row, _ = _latest_csv_row(path)
        if not row:
            continue
        ld = row.get("ld_on") == "1"
        try:
            cur = float(row.get("pulse_ma") or 0) + float(row.get("bias_ma") or 0)
        except ValueError:
            cur = 0.0
        laser_rows.append((f"Laser {wl}", f"LD {'ON' if ld else 'OFF'}  {cur:.0f} mA", True))
    rows += laser_rows or [("Laser", "no recent log", False)]

    row, path = _latest_csv_row(os.path.join(env.DAQ_DIR, "LOG", "UPS", "ups_*.csv"))
    ups_fresh = path is not None and time.time() - os.path.getmtime(path) <= 180
    if row:
        rows.append(("UPS", f"Vin {row.get('Vin', '?')} V  Vout {row.get('Vout', '?')} V  {row.get('Watt', '?')} W",
                     ups_fresh))
    else:
        rows.append(("UPS", "no data", False))
    return rows


def scan_running():
    """True while a CAEN scan/stability run is taking data (same check as the operator rules)."""
    try:
        out = subprocess.run(["pgrep", "-af", "ADC_test/script_v7.sh|execute_DAQ_v2"],
                             capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return False
    return any(" -j" not in line and line.strip() for line in out.splitlines())


_busy = (0.0, "")


def active_scan():
    """Tag of the General Scan whose marker names a live process, else None."""
    try:
        tag, pid = open(os.path.join(FLAG_DIR, "active_scan")).read().split()[:2]
    except (OSError, ValueError):
        return None
    return tag if os.path.exists(f"/proc/{pid}") else None


def busy_reason():
    """Why DAQ controls are blocked now, '' when idle (cached 1 s)."""
    global _busy
    if time.time() - _busy[0] < 1.0:
        return _busy[1]
    why = ""
    try:
        from core import schedule
        from core.stability import RUN
        if RUN.running:
            why = "A Stability Run is running."
        elif schedule.general_scan_running():
            why = "A General Scan is running."
    except Exception:
        pass
    if not why:
        tag = active_scan()
        why = (f"A General Scan ({tag}) is running." if tag
               else "An acquisition is running." if scan_running() else "")
    _busy = (time.time(), why)
    return why


def _bfield_last():
    """(seconds since the last logged B-field sample, coils off in it) from the merged log."""
    row, _path = _latest_csv_row(os.path.join(env.ADC_DIR, "log_bfield_*.csv"))
    if not row:
        return None, False
    try:
        age = time.time() - datetime.strptime(row["timestamp"][:19], "%Y-%m-%d %H:%M:%S").timestamp()
    except (KeyError, ValueError):
        return None, False
    try:
        off = all(abs(float(row[c])) < 0.01 for c in ("I1", "I2", "I3", "I4"))
    except (KeyError, ValueError):
        off = False      # N/A is unknown, not off
    return age, off


BACKUP_LOG = os.path.expanduser("~/logs/backup_logs.log")
BACKUP_MAX_AGE = 26 * 3600   # daily at 03:30


def _backup_status():
    """(ok, text) from the last line of the nightly log backup."""
    try:
        with open(BACKUP_LOG, encoding="utf-8", errors="replace") as f:
            line = f.read().strip().splitlines()[-1]
        age = time.time() - datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").timestamp()
    except (OSError, IndexError, ValueError):
        return False, "no backup log"
    ok = "[OK]" in line
    return ok and age <= BACKUP_MAX_AGE, f"{'OK' if ok else 'FAILED'} {line[:16]} ({_age_text(age)})"


def _newest_age(pattern):
    """Seconds since the newest file matching pattern was written, or None."""
    files = glob.glob(pattern)
    if not files:
        return None
    return time.time() - max(os.path.getmtime(f) for f in files)


def _age_text(sec):
    if sec is None:
        return "no data"
    if sec < 120:
        return f"{sec:.0f} s ago"
    if sec < 7200:
        return f"{sec / 60:.0f} min ago"
    if sec < 172800:
        return f"{sec / 3600:.0f} h ago"
    return f"{sec / 86400:.0f} d ago"


def _fresh(sec, max_age):
    return (sec is not None and sec <= max_age), _age_text(sec)


def system_status():
    """{overview name: (ok, detail)} from files the running apps already write.

    Read-only: lock files, log freshness and the HV monitor's SQLite DB. ok is
    True/False; the thresholds match how often each source is written."""
    import instance_lock
    st = {}

    if scan_running():
        st["DAQ System"] = (True, "scan running")
    elif instance_lock.is_running("daq"):
        st["DAQ System"] = (True, "Tk app open, idle")
    elif DAQ_PROBE.ok is not None:
        st["DAQ System"] = DAQ_PROBE.result()
    else:
        st["DAQ System"] = (False, "Tk app not running")

    hv_age = env_age = None
    try:
        con = sqlite3.connect(f"file:{env.HV_DB}?mode=ro", uri=True, timeout=1)
        since = datetime.fromtimestamp(time.time() - 7 * 86400).isoformat()
        hv = con.execute(f"SELECT timestamp FROM monitoring_data WHERE timestamp > ? AND {HV_GOOD} "
                         "ORDER BY timestamp DESC LIMIT 1", (since,)).fetchone()
        box = con.execute("SELECT timestamp FROM monitoring_data WHERE timestamp > ? AND Dark_Box_1_T IS NOT NULL "
                          "ORDER BY timestamp DESC LIMIT 1", (since,)).fetchone()
        con.close()
        if hv:
            hv_age = time.time() - datetime.fromisoformat(hv[0]).timestamp()
        if box:
            env_age = time.time() - datetime.fromisoformat(box[0]).timestamp()
    except Exception:
        pass
    hv_ok, hv_txt = _fresh(hv_age, 60)
    env_ok, env_txt = _fresh(env_age, 60)
    st["HV / Env Sensor"] = (hv_ok and env_ok, f"HV: {hv_txt}, Env: {env_txt}")
    st["Laser Controller"] = _fresh(_newest_age(os.path.join(env.ADC_DIR, "LOG", "LASER", "laser_data_*.csv")), 60)
    b_age, coils_off = _bfield_last()
    b_ok, b_txt = _fresh(b_age, 20 * 60)
    st["B-field Monitor"] = (b_ok and not coils_off, b_txt + (", coils OFF" if coils_off else ""))
    st["Log Backup"] = _backup_status()
    st["OMRON UPS"] = _fresh(_newest_age(os.path.join(env.DAQ_DIR, "LOG", "UPS", "ups_*.csv")), 180)
    return st


LIVE_ANGLES = os.path.join(env.DAQ_DIR, "logs", "live_angles.json")
LIVE_MAX_AGE = 10.0   # s; the Tk app writes every 0.5-1 s while it monitors the motors


def live_angles():
    """{dev_num: (tilt, rot, moving)}: the Tk app's motor monitor while it runs, otherwise the
    drives read directly (core.stage); {} when neither has a fresh reading."""
    from core import stage
    st = stage.stage()
    if st.owns_drives():
        return st.angles()
    try:
        with open(LIVE_ANGLES) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    now = time.time()
    out = {}
    for k, v in data.items():
        try:
            if now - float(v["ts"]) <= LIVE_MAX_AGE and v["tilt"] is not None and v["rot"] is not None:
                out[int(k)] = (float(v["tilt"]), float(v["rot"]), bool(v.get("moving")))
        except (KeyError, TypeError, ValueError):
            continue
    return out


FLAG_DIR = "/tmp/daq_flags"
PIPELINE_STAGES = [("daq", "DAQ"), ("prod", "Produce"), ("read", "Fit"), ("contour", "Waveform")]


def _daq_running():
    """A real acquisition: execute_DAQ_v2 without -j (that is only the connection probe)."""
    try:
        out = subprocess.run(["pgrep", "-ax", "execute_DAQ_v2"], capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return True   # cannot tell -> do not hide a live run
    return any(line.strip() and " -j" not in line for line in out.splitlines())


class _FgProbe:
    """TCP connect to the DT1081B (host, port) every 10 s; the Function Generator tab lamp."""

    def __init__(self):
        self.ok = None
        self._started = False

    def start(self):
        if self._started or env.NO_HW:
            return
        self._started = True
        threading.Thread(target=self._loop, daemon=True).start()

    def result(self):
        from core import function_generator as FG
        host, port = FG.host()
        if self.ok is None:
            return False, f"Function generator {host}:{port}: not probed"
        return (self.ok, f"Function generator {host}:{port}: " + ("reachable" if self.ok else "not reachable"))

    def _loop(self):
        import socket
        from core import function_generator as FG
        while True:
            try:
                host, port = FG.host()
                with socket.create_connection((host, port), timeout=2.0):
                    self.ok = True
            except Exception:
                self.ok = False
            time.sleep(10.0)


FG_PROBE = _FgProbe()


class _DaqProbe:
    """`execute_DAQ_v2 -j` every 2 s while v2 owns the hardware, as the Tk app's _daq_check_loop."""

    def __init__(self):
        self.ok = None          # None: not probing (the Tk app probes its own)
        self.hk = False
        self.hk_ok, self.hk_text, self._hk_at = None, "", 0.0
        self._started = False

    def start(self):
        if self._started or env.NO_HW:
            return
        self._started = True
        threading.Thread(target=self._loop, daemon=True).start()

    def result(self):
        if self.hk:
            if self.hk_ok is None:
                return False, "HK Digitizer selected, checking the HK PC"
            return bool(self.hk_ok), self.hk_text
        return (True, "CAEN digitizer connected") if self.ok else (False, "CAEN digitizer not reachable")

    def _loop(self):
        while True:
            try:
                self._once()
            except Exception:
                self.ok = False
            time.sleep(2.0)

    def _once(self):
        import instance_lock
        from core import scan
        from core.jobs import JOBS
        if instance_lock.is_running("daq"):
            self.ok = None
            return
        host = scan._host
        self.hk = host is not None and getattr(host.auto_mgr, "daq_backend", "caen") == "hk"
        if self.hk:              # the CAEN box is not in use; check the HK PC instead
            self.ok = False
            if time.time() - self._hk_at >= HK_PROBE_S:
                self._hk_at = time.time()
                self._probe_hk()
            return
        self.hk_ok, self._hk_at = None, 0.0
        # a probe that opens the board between two scan points makes the next point fail
        if ((host is not None and host.auto_mgr.is_running) or _daq_running()
                or any(JOBS.running(s) for s in ("daq", "general_scan", "stability"))):
            return
        from core.daq_diag import probe_env
        base = env.read_config().get("BasePath")
        if not base:
            self.ok = False
            return
        r = subprocess.run([os.path.join(base, "execute_DAQ_v2"), "-j"], capture_output=True, timeout=5,
                           env=probe_env(base), start_new_session=True)
        self.ok = r.returncode == 0


    def _probe_hk(self):
        from core import hk
        cfg = hk.config()
        rc, out = hk.test_connection(cfg)
        if rc == 0 and "SCANMGR=OK" in (out or ""):
            self.hk_ok, self.hk_text = True, f"HK PC connected ({cfg.get('ssh_target', '')})"
        elif rc == 0:
            self.hk_ok, self.hk_text = False, "HK PC reachable, ScanManager missing"
        else:
            self.hk_ok, self.hk_text = False, f"HK PC not reachable ({cfg.get('ssh_target', '')})"


HK_PROBE_S = 30.0    # ssh round trip to the HK PC; the CAEN probe runs every 2 s
DAQ_PROBE = _DaqProbe()


def pipeline():
    """(stage key, run) of the pipeline step running now, or (None, None).
    Same flags as the Tk Pipeline Monitor (/tmp/daq_flags/<stage>_<run>.flag), read only:
    a DAQ flag counts only while execute_DAQ_v2 runs, analysis flags older than 20 min are stale."""
    now = time.time()
    for key, _label in PIPELINE_STAGES:
        live = []
        for f in glob.glob(os.path.join(FLAG_DIR, f"{key}_*.flag")):
            try:
                age = now - os.path.getmtime(f)
                run = int(os.path.basename(f).split("_")[1].split(".")[0])
            except (OSError, ValueError, IndexError):
                continue
            if (key == "daq" and (_daq_running() or age <= 5)) or (key != "daq" and age <= 1200):
                live.append(run)
        if live:
            return key, max(live)
    return None, None


PIPE_FRESH_S = 1200      # flags older than this belong to a finished session: show Idle


def pipeline_states():
    """(run, {stage key: 'waiting' | 'active' | 'done' | 'moving'}) for the newest run with a recent flag,
    or (None, {}) when nothing happened recently. Between two General Scan points the DAQ stage reads
    'moving' while the stage motors run."""
    now = time.time()
    newest = {}
    for f in glob.glob(os.path.join(FLAG_DIR, "*_*.flag")):
        try:
            key, run = os.path.basename(f)[:-5].rsplit("_", 1)
            newest[(key, int(run))] = os.path.getmtime(f)
        except (OSError, ValueError):
            continue
    fresh = [r for (_k, r), t in newest.items() if now - t <= PIPE_FRESH_S]
    if not fresh:
        return None, {}
    run = max(fresh)          # a run's analysis can finish after the next run's DAQ has started
    keys = [k for k, _label in PIPELINE_STAGES]
    if ("done", run) in newest:
        states = {k: "done" for k in keys}
    else:
        reached = [i for i, k in enumerate(keys) if (k, run) in newest]
        top = max(reached) if reached else -1
        states = {k: ("done" if i < top else "waiting") for i, k in enumerate(keys)}
        if top >= 0:
            live = _daq_running() if keys[top] == "daq" else now - newest[(keys[top], run)] <= PIPE_FRESH_S
            states[keys[top]] = "active" if live else "done"
    if states.get("daq") != "active" and scan_running():
        try:
            if any(m for _t, _r, m in live_angles().values()):
                states["daq"] = "moving"
        except Exception:
            pass
    return run, states


BACKEND_FILE = os.path.join(env.DAQ_DIR, "logs", "daq_backend.json")
BACKEND_NAMES = {"caen": "CAEN", "hk": "HK Digitizer"}


def daq_backend():
    """Backend armed in the Tk app ("CAEN" / "HK Digitizer"), or None when the Tk app is not running."""
    import instance_lock
    if not instance_lock.is_running("daq"):
        return None
    try:
        d = json.load(open(BACKEND_FILE))
        os.kill(int(d["pid"]), 0)   # written by the Tk app that is running now
        return BACKEND_NAMES.get(d.get("backend"), "CAEN")
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if scan_running():
        from core.history import running_backend
        return running_backend(env.DAQ_DIR)
    return "CAEN"   # the Tk app starts on CAEN

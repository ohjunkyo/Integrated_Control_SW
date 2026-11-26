"""HV recorded with each run: the HV Monitor's latest VMon, passed to script_v7.sh as DAQ_HV1..3
(config3.h only says the nominal HV; a routine changes the HV without writing it)."""
import time

from core import applog, env

MAX_AGE_S = 60.0
LOG_EVERY_S = 60.0
_last_log = {}


def now_volts(max_age=MAX_AGE_S):
    """[VMon ch0..2] of the newest HV DB row, or None when unreadable or older than max_age."""
    from core import hv_request
    out = []
    for ch in range(3):
        t, v = hv_request._latest_vmon(ch, env.HV_DB)
        if t is None or v is None or time.time() - t > max_age:
            return None
        out.append(v)
    return out


def now_state(max_age=MAX_AGE_S):
    """([V ch0..2], [on ch0..2]) of the newest HV DB row: VSet where logged, else VMon;
    None when unreadable or older than max_age."""
    import sqlite3
    from datetime import datetime
    try:
        con = sqlite3.connect(f"file:{env.HV_DB}?mode=ro", uri=True, timeout=1)
        con.row_factory = sqlite3.Row
        r = con.execute("SELECT * FROM monitoring_data ORDER BY timestamp DESC LIMIT 1").fetchone()
        con.close()
    except Exception:
        return None
    if not r or r["timestamp"] is None or time.time() - datetime.fromisoformat(r["timestamp"]).timestamp() > max_age:
        return None
    volts, on = [], []
    for ch in range(3):
        v, st = r[f"Ch{ch}_V"], r[f"Ch{ch}_Stat"]
        if v is None or st is None:
            return None
        vs = r[f"Ch{ch}_VSet"] if f"Ch{ch}_VSet" in r.keys() else None
        volts.append(float(vs if vs is not None else v))
        on.append(bool(int(st) & 1))
    return volts, on


def _say(key, text):
    """One log line per key per LOG_EVERY_S: runs start every few seconds in a scan."""
    now = time.time()
    if now - _last_log.get(key, 0.0) >= LOG_EVERY_S:
        _last_log[key] = now
        applog.log(text)


def run_env():
    """{DAQ_HV1..3} from the HV Monitor when its reading is fresh and ch0-2 are ON; {} otherwise (config3.h HV)."""
    st = now_state()
    if st is None:
        _say("stale", "[WARNING] HV Monitor reading unavailable; this run records the config3.h HV.")
        return {}
    v, on = st
    if not all(on):
        off = ", ".join(f"ch{i}" for i, o in enumerate(on) if not o)
        _say("off", f"[WARNING] HV {off} OFF; this run records the config3.h HV.")
        return {}
    _say("ok", "[INFO] Run HV recorded from the HV Monitor: " + " / ".join(f"{x:.0f}" for x in v) + " V")
    return {f"DAQ_HV{ch + 1}": f"{x:.0f}" for ch, x in enumerate(v)}


routine_env = run_env   # former name

# hv_preflight.py
import os
import sqlite3
from datetime import datetime

HV_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "HV_Control_SW", "monitoring_log.db")

# HV channel n feeds the PMT whose config3.h slot is HV{n+1}.
CHANNELS = ((0, "Monitor", "SN1"), (1, "Rot1", "SN2"), (2, "Rot2", "SN3"))

MAX_AGE_S = 60.0
MIN_V = 500.0
MAX_I = 10.0
READ_ERROR = 4096   # placeholder status the HV worker logs on a read error


def latest_channels(db_path=HV_DB):
    """(age_s, [dict(ch, role, sn_key, v, i, stat, state)]) from the newest row
    that holds a real reading. Raises on an unreadable/empty DB."""
    cols = ["timestamp"] + [f"Ch{c}_{k}" for c, _, _ in CHANNELS for k in ("V", "I_H", "Stat")]
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
    try:
        rows = con.execute(f"SELECT {', '.join(cols)} FROM monitoring_data "
                           "ORDER BY rowid DESC LIMIT 20").fetchall()
    finally:
        con.close()
    if not rows:
        raise ValueError("HV DB is empty")

    def is_placeholder(r):
        try:
            return all(int(r[3 + 3 * k] or 0) == READ_ERROR for k in range(len(CHANNELS)))
        except (TypeError, ValueError):
            return False
    row = next((r for r in rows if not is_placeholder(r)), rows[0])
    try:
        age = (datetime.now() - datetime.fromisoformat(str(row[0]))).total_seconds()
    except ValueError:
        age = float("inf")
    out = []
    for k, (ch, role, sn_key) in enumerate(CHANNELS):
        v, i, st = row[1 + 3 * k], row[2 + 3 * k], row[3 + 3 * k]
        try:
            v, i, st = float(v), float(i), int(st)
        except (TypeError, ValueError):
            out.append(dict(ch=ch, role=role, sn_key=sn_key, v=None, i=None, stat=None, state="NO DATA"))
            continue
        if st == READ_ERROR:
            state = "READ ERROR"
        elif not st & 1:
            state = "OFF"
        elif st & 0b110:
            state = "RAMPING"
        elif v >= MIN_V and i < MAX_I:
            state = "NO CURRENT"
        else:
            state = "ON"
        out.append(dict(ch=ch, role=role, sn_key=sn_key, v=v, i=i, stat=st, state=state))
    return age, out


def check_hv(cfg=None, db_path=HV_DB):
    """Pre-flight view of the HV monitor's latest reading.

    Returns (rows, blockers, warnings): blockers are channels that are ON but
    draw no current; warnings cover anything that cannot be verified.
    """
    cfg = cfg or {}
    rows, blockers, warnings = [], [], []
    try:
        age, chans = latest_channels(db_path)
    except Exception as e:
        warnings.append(f"HV state cannot be verified (HV DB unreadable: {e}).")
        return [("HV:", "unknown")], blockers, warnings
    if age > MAX_AGE_S:
        warnings.append(f"HV state cannot be verified: the HV Monitor has not logged for "
                        f"{age/60:.0f} min (is it running?).")
        return [("HV:", "stale reading")], blockers, warnings

    for c in chans:
        sn = str(cfg.get(c["sn_key"], "")).strip()
        name = f"Ch{c['ch']} ({c['role']} {sn})" if sn else f"Ch{c['ch']} ({c['role']})"
        state = c["state"]
        if state == "NO CURRENT":
            blockers.append(f"{name} HV is ON ({c['v']:.0f} V) but draws no current ({c['i']:.1f} uA) "
                            "-- check the HV cable at the PMT base.")
        elif state == "OFF":
            warnings.append(f"{name} HV is OFF.")
        elif state == "RAMPING":
            warnings.append(f"{name} HV is still ramping.")
        elif state in ("READ ERROR", "NO DATA"):
            warnings.append(f"{name}: no valid HV reading.")
        if c["v"] is None:
            continue
        rows.append((f"HV Ch{c['ch']}:", f"{c['role']:<8}{c['v']:7.0f} V  {c['i']:6.1f} uA   {state}"))
    return rows, blockers, warnings

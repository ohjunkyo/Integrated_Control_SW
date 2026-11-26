"""Conditions actually applied during a General Scan, for the Scan History 'applied' record:
HV / wavelength / laser mA / events from this scan's run registry rows; HV from the HV Monitor when
the scan left no local rows (HK backend); config3.h values last."""
import csv
import os
import sqlite3
from datetime import datetime

REGISTRY = os.path.expanduser("~/ADC/ADC_test/Data/run_registry.csv")
HV_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                     "HV_Control_SW", "monitoring_log.db")
VMON_MAX_AGE_S = 120.0


def _ints(values):
    out = []
    for v in values:
        try:
            n = int(round(float(v)))
        except (TypeError, ValueError):
            continue
        if n not in out:
            out.append(n)
    return sorted(out)


def _strs(values):
    out = []
    for v in values:
        v = str(v or "").strip()
        if v and v != "-" and v not in out:
            out.append(v)
    return out


def registry_rows(date_tag, block, block_size, registry=REGISTRY):
    """This scan's general-scan rows: same date, run inside the block."""
    rows = []
    try:
        with open(registry, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    run = int(r.get("run", ""))
                except ValueError:
                    continue
                if r.get("date") == str(date_tag) and block <= run < block + block_size \
                        and (r.get("kind") or "general") == "general":
                    rows.append(r)
    except OSError:
        pass
    return rows


def vmon_now(hv_db=HV_DB, max_age=VMON_MAX_AGE_S):
    """{slot: V} of the newest HV Monitor row for channels that are ON (VSet where logged, else VMon),
    or None when stale/unreadable."""
    try:
        con = sqlite3.connect(f"file:{hv_db}?mode=ro", uri=True, timeout=1)
        con.row_factory = sqlite3.Row
        r = con.execute("SELECT * FROM monitoring_data ORDER BY timestamp DESC LIMIT 1").fetchone()
        con.close()
        if not r or (datetime.now() - datetime.fromisoformat(r["timestamp"])).total_seconds() > max_age:
            return None
        out = {}
        for ch in range(3):
            v, st = r[f"Ch{ch}_V"], r[f"Ch{ch}_Stat"]
            vs = r[f"Ch{ch}_VSet"] if f"Ch{ch}_VSet" in r.keys() else None
            if v is not None and st is not None and int(st) & 1:
                out[str(ch + 1)] = int(round(float(vs if vs is not None else v)))
        return out or None
    except Exception:
        return None


def collect(date_tag, block, block_size, cfg, registry=REGISTRY, hv_db=HV_DB):
    """-> {"source", "runs", "hv": {slot: [V, ...]}, "wavelengths", "laser_mA", "events"}"""
    rows = registry_rows(date_tag, block, block_size, registry)
    if rows:
        runs = sorted(int(r["run"]) for r in rows)
        return {"source": "registry", "runs": [runs[0], runs[-1]], "n_runs": len(rows),
                "hv": {str(k): _ints(r.get(f"hv{k}") for r in rows) for k in (1, 2, 3)},
                "wavelengths": _strs(r.get("wavelength") for r in rows),
                "laser_mA": _ints(r.get("laser_mA") for r in rows),
                "events": _ints(r.get("events") for r in rows)}
    vm = vmon_now(hv_db)
    out = {"source": "vmon" if vm else "header", "runs": [], "n_runs": 0,
           "hv": {str(k): ([vm[str(k)]] if vm and str(k) in vm else _ints([cfg.get(f"HV{k}")])) for k in (1, 2, 3)},
           "wavelengths": _strs([cfg.get("Wavelength")]), "laser_mA": _ints([cfg.get("Laser")]),
           "events": _ints([cfg.get("Events")])}
    return out

# live_status.py -- content of the Live Console status card (General Scan tab).
#
# Everything here is read from files the rest of the system already writes
# (HV monitor DB, laser CSV logs, run registry, config3.h), so building the
# card never touches hardware.
import csv
import glob
import os
import time
from datetime import datetime

import hv_preflight

ADC_DIR = os.path.expanduser("~/ADC/ADC_test")
REGISTRY = os.path.join(ADC_DIR, "Data", "run_registry.csv")
LASER_DIR = os.path.join(ADC_DIR, "LOG", "LASER")
KIND_SHORT = {"general": "GS", "stability": "ST", "manual": "MN", "dummy": "DM"}


def registry_rows(limit=None):
    try:
        with open(REGISTRY, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except OSError:
        return []
    return rows[-limit:] if limit else rows


def lasers_on(max_age_s=120):
    """[(wavelength_nm, pulse_mA, temp_C)] for lasers whose LD is on now."""
    out = []
    for wl in ("375nm", "405nm", "450nm", "473nm"):
        files = sorted(glob.glob(os.path.join(LASER_DIR, f"laser_data_{wl}_*.csv")))
        if not files:
            continue
        try:
            with open(files[-1], encoding="utf-8") as f:
                head = f.readline().strip().split(",")
                f.seek(max(0, os.path.getsize(files[-1]) - 400))
                last = f.read().strip().splitlines()[-1].split(",")
            d = dict(zip(head, last))
            age = time.time() - datetime.fromisoformat(d["timestamp"]).timestamp()
            if age <= max_age_s and d.get("ld_on") in ("1", "True"):
                out.append((wl[:-2], float(d.get("pulse_ma") or 0), float(d.get("temp_c") or 0)))
        except Exception:
            continue
    return out


def _dur(sec):
    sec = max(0, int(sec))
    h, m = divmod(sec // 60, 60)
    return f"{h} h {m:02d} m" if h else f"{m} min"


def build(controller, prog=None):
    """List of lines; each line is a list of (text, tag) segments."""
    lines = []
    auto = getattr(controller, "auto_mgr", None)
    stab = getattr(getattr(controller, "auto_ui", None), "stability_run_ui", None)
    cfg = {}
    try:
        cfg = controller.config_manager.get_all_variables()
    except Exception:
        pass
    reg = registry_rows(limit=200)
    last = reg[-1] if reg else None

    if auto is not None and getattr(auto, "is_running", False):
        cur, tot, axis, tilt, eta = (prog or {}).get("cur"), (prog or {}).get("tot"), \
            (prog or {}).get("axis"), (prog or {}).get("tilt"), (prog or {}).get("eta")
        wl = cfg.get("Wavelength", "?")
        head = f"{axis} axis  {tilt:+g}°" if axis is not None and tilt is not None else "General Scan"
        lines.append([(head, "card_big")])
        sub = f"General Scan · {wl} nm"
        if cur:
            sub += f" · point {cur}/{tot}" if tot else f" · point {cur}"
        if last:
            sub += f" · run {last['run']}"
        lines.append([(sub, "card_sub")])
        if eta is not None and eta >= 0:
            fin = datetime.fromtimestamp(time.time() + eta).strftime("%H:%M")
            lines.append([(f"ETA {fin}  ({_dur(eta)} left)", "card_sub")])
    elif stab is not None and getattr(stab, "_running", False):
        rows = [r for r in reg if r.get("kind") == "stability"]
        r = rows[-1] if rows else None
        lines.append([("Stability Run", "card_big")])
        sub = f"{r['wavelength']} nm · acquisition {r['seq']}/{r['nseq']} · run {r['run']}" if r else "starting"
        lines.append([(sub, "card_sub")])
        fin = getattr(stab, "_est_finish", None)
        if fin:
            lines.append([(f"ETA {fin.strftime('%m-%d %H:%M')}  ({_dur((fin - datetime.now()).total_seconds())} left)",
                           "card_sub")])
    else:
        lines.append([("Idle", "card_big")])
        if last:
            kind = KIND_SHORT.get(last.get("kind"), last.get("kind", ""))
            lines.append([(f"last run {last['date']}_{last['run']} · {kind} · {last['wavelength']} nm · "
                           f"{last['status']} {last['registered_at'][11:16]}", "card_sub")])
    lines.append([("─" * 60, "card_sep")])

    try:
        age, chans = hv_preflight.latest_channels()
        seg = [("HV     ", "card_key")]
        for c in chans:
            tag = {"ON": "card_ok", "NO CURRENT": "card_bad", "OFF": "card_warn",
                   "RAMPING": "card_warn"}.get(c["state"], "card_bad")
            val = f"{c['i']:.0f} µA" if c["i"] is not None else "--"
            if c["state"] not in ("ON",):
                val += f" {c['state']}"
            seg.append((f"Ch{c['ch']} {val}   ", tag))
        if age > hv_preflight.MAX_AGE_S:
            seg.append((f"(stale {age/60:.0f} min)", "card_bad"))
        lines.append(seg)
    except Exception:
        lines.append([("HV     ", "card_key"), ("no HV reading", "card_bad")])

    on = lasers_on()
    if on:
        lines.append([("LD     ", "card_key")] +
                     [(f"{wl} nm {ma:.0f} mA ({t:.1f} °C)   ", "card_val") for wl, ma, t in on])
    else:
        lines.append([("LD     ", "card_key"), ("all off", "card_dim")])

    lsv = getattr(getattr(controller, "auto_ui", None), "live_scan_view", None)
    newest = lsv.newest_point() if lsv is not None and hasattr(lsv, "newest_point") else None
    if newest:
        q = newest["data"].get("qe", {})
        vals = " / ".join(f"{q[ch][0]:.2f}" for ch in (1, 2) if ch in q)
        mon = f" / mon {q[0][0]:.2f}" if 0 in q else ""
        lines.append([("Last   ", "card_key"), (f"QE {vals}{mon}  (run {newest['run']})", "card_val")])

    shifter, expert = cfg.get("Shift_worker", "-") or "-", cfg.get("Expert", "-") or "-"
    lines.append([("Shift  ", "card_key"), (f"{shifter} · Expert {expert}", "card_val")])
    lines.append([("─" * 60, "card_sep")])

    today = [r for r in reg if r.get("date") == (last or {}).get("date")][-5:]
    for k, r in enumerate(reversed(today)):
        t = r["registered_at"][11:16]
        st = r["status"]
        tag = "card_ok" if st == "done" else "card_bad" if st.startswith(("failed", "interrupted")) else "card_val"
        seg = [(f"{t}  run {r['run']}  ", "card_dim"), (st, tag)]
        nxt = today[len(today) - k] if k > 0 else None
        if st == "done" and nxt is not None:
            try:
                d = (datetime.fromisoformat(nxt["registered_at"]) -
                     datetime.fromisoformat(r["registered_at"])).total_seconds()
                seg.append((f"  {d/60:.1f} min", "card_dim"))
            except ValueError:
                pass
        lines.append(seg)
    return lines

"""Daily Routine > Results data: what one routine run measured, per step kind, from the run registry
(time window of each step), the HV Monitor database and the FinalResult files. Qt-free; reads only."""
import csv
import os
import re
import sqlite3
from datetime import datetime, timedelta

from core import disk_cache, env
from core import routine as rt
from core.schedule import JST

GRACE_S = 120          # a run registered this long after its step finished still belongs to it
VMON_MAX_POINTS = 1500
METRICS = {"qe": "Relative QE (%)", "gain": "Gain / SPE charge [pC]", "fwhm": "TTS FWHM [ns]"}
DARK_ON = "dark_on"
DARK_ON_LABEL = "Dark rate (LD on, pre-trigger window) [kHz]"
LABELS = dict(METRICS, dark_on="Dark rate, LD on [kHz]")   # axis label; DARK_ON_LABEL in titles and tables
TYPES = (("GeneralScan", "General Scan (Daily)"), ("ShortCross", "Short Cross"), ("HK", "HK"), ("Other", "Other"))
ARCHIVE = "history_archive"


def _t(text):
    try:
        d = datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=JST)


def windows(run, kinds, now=None):
    """[(step, start, end)] of the run's steps of these kinds that started; end = finished or now."""
    now = now or rt._now()
    out = []
    starts = [t for t in (rt._parse(s.get("started")) for s in run.get("steps") or [] if s.get("started")) if t is not None]
    for s in run.get("steps") or []:
        if s.get("kind") in kinds and s.get("started"):
            a = rt._parse(s["started"])
            b = rt._parse(s.get("finished")) or now
            if a is not None:
                later = [t for t in starts if t > a]
                if later:   # the grace time after a step must not reach into the next step
                    b = max(a, min(b, min(later) - timedelta(seconds=GRACE_S + 1)))
                out.append((s, a, b))
    return out


def _registry():
    from core.live_scan import REGISTRY
    try:
        with open(REGISTRY, encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def registry_rows(wins, match):
    """Registry rows inside any window (start .. end + GRACE_S) for which match(row) is true, oldest first."""
    out = []
    for r in _registry():
        t = _t(r.get("registered_at"))
        if t is None or not match(r):
            continue
        if any(a <= t <= b + timedelta(seconds=GRACE_S) for _s, a, b in wins):
            out.append(dict(r, _time=t))
    out.sort(key=lambda r: r["_time"])
    return out


def laser_mA(rows):
    """Recorded laser current of registry rows: '118 mA' or '118-120 mA'; '' when none is recorded."""
    v = []
    for r in rows:
        try:
            v.append(float(r.get("laser_mA") or ""))
        except (TypeError, ValueError):
            pass
    if not v:
        return ""
    a, b = min(v), max(v)
    return f"{a:g} mA" if a == b else f"{a:g}-{b:g} mA"


def laser_text(rows):
    """'375 nm 118 mA   473 nm 120 mA' per wavelength of registry rows; '' when no current is recorded."""
    by_wl = {}
    for r in rows:
        by_wl.setdefault(r.get("wavelength") or "?", []).append(r)
    parts = [f"{wl} nm {laser_mA(rs)}" for wl, rs in sorted(by_wl.items()) if laser_mA(rs)]
    return "   ".join(parts)


def run_span(rows):
    """'20261107_600-629' style label of registry rows, or ''."""
    if not rows:
        return ""
    by_date = {}
    for r in rows:
        try:
            by_date.setdefault(r["date"], []).append(int(r["run"]))
        except (KeyError, ValueError):
            pass
    parts = []
    for d, runs in sorted(by_date.items()):
        a, b = min(runs), max(runs)
        parts.append(f"{d}_{a:03d}" + (f"-{b:03d}" if b != a else ""))
    return ", ".join(parts)


# ---------------------------------------------------------------- dark rate
_THR_CACHE = {}


def _prd_thresholds(name):
    """(self-trigger mV, pulse analysis mV) from the prd RunInfo, or None."""
    base = env.read_config().get("BasePath") or os.path.expanduser("~/ADC/ADC_test")
    p = os.path.join(base, "Data", "production", name.replace("precal_raw_", "precal_prd_"))
    try:
        st = disk_cache.stamp(p)
    except OSError:
        return None
    key = (p, st[0])
    if key not in _THR_CACHE:
        _THR_CACHE[key] = disk_cache.get("thresholds", p, st)
    if _THR_CACHE[key] is None:
        try:
            import uproot
            with uproot.open(p) as f:
                ri = f["RunInfo"]
                _THR_CACHE[key] = (float(ri["SelfTrigThrMv"].array(library="np")[0]),
                                   float(ri["AnalysisThrMv"].array(library="np")[0]))
            disk_cache.put("thresholds", p, st, _THR_CACHE[key])
        except Exception:
            pass
    return _THR_CACHE[key]


def thr_text(thr):
    """'1.5 mV' or 'trigger 2 mV, pulses 1.5 mV'; several values joined with '/'."""
    if not thr:
        return ""
    trig = "/".join(f"{v:g}" for v in sorted({t for t, _a in thr}))
    ana = "/".join(f"{v:g}" for v in sorted({a for _t, a in thr}))
    return f"{trig} mV" if trig == ana else f"trigger {trig} mV, pulses {ana} mV"


def dark(run, now=None):
    """{"points": [(time, ch, live_kHz, err_kHz, (clean_kHz, err) | None, (raw_kHz, err), dead, (date, run))],
    "rows": registry rows, "steps": [...], "thr": [(trigger mV, pulse mV)]}."""
    from core import stability as stab
    wins = windows(run, ("dark_rate",), now)
    rows = registry_rows(wins, lambda r: r.get("mode") == "dark" and r.get("status", "").startswith("done"))
    pts, thr = [], set()
    for r in rows:
        t = _prd_thresholds(r.get("file", ""))
        if t:
            thr.add(t)
        path = stab._raw_dark_path(r.get("file", ""))
        if not path:
            continue
        try:
            v = stab._run_rate_detail(path)
        except Exception:
            v = None
        if v:
            ch, rate, err, raw = v["ch"], v["live"], v["err"], v["raw"]
            clean = stab._prd_clean_rate(r.get("file", ""), ch)
            cl = (clean / 1000.0, err / 1000.0 * clean / rate) if clean and rate else None
            rec = (raw / 1000.0, raw / 1000.0 / v["events"] ** 0.5)
            pts.append((r["_time"], ch, rate / 1000.0, err / 1000.0, cl, rec, v["dead"], _run_id(r)))
    return {"points": pts, "rows": rows, "steps": [w[0] for w in wins], "thr": sorted(thr)}


def _run_id(r):
    """(date, run) of a registry row or a FinalResult record, None when it has no run number."""
    try:
        return str(r["date"]), int(r["run"])
    except (KeyError, TypeError, ValueError):
        return None


def _records(rows):
    """[(registry row, FinalResult record)] of analysed runs, read as Live Scan reads them."""
    from core.live_scan import RESULT_DIR, LiveScanModel, registry, uproot
    if uproot is None or not rows:
        return []
    import os
    m, reg, out = LiveScanModel(), registry(), []
    for r in rows:
        try:
            run = int(r["run"])
        except (KeyError, ValueError):
            continue
        path = os.path.join(RESULT_DIR, f"precal_result_kor_run_{r['date']}_{run:03d}.root")
        rec = m._read_point(r["date"], run, path, reg) if os.path.exists(path) else None
        if rec:
            out.append((r, rec))
    return out


def _metric(rec, metric, ch):
    pair = rec["data"].get(metric, {}).get(ch)
    return pair if pair is not None and (metric == "qe" or pair[0] > 0) else None


_DARK_ON = {}


def dark_on(path):
    """{ch: kHz} from NoiseCountRate_ch<N> (Hz, dark pulses in the pre-trigger window of laser runs) of a FinalResult file."""
    try:
        st = disk_cache.stamp(path)
    except OSError:
        return {}
    key = (path, st[0])
    if key not in _DARK_ON:
        got = disk_cache.get("dark_on", path, st)
        if got is None:
            got = {}
            try:
                import uproot
                with uproot.open(path) as f:
                    for ch in (0, 1, 2):
                        try:
                            got[ch] = float(f[f"NoiseCountRate_ch{ch}"].member("fVal")) / 1e3
                        except Exception:
                            continue
                disk_cache.put("dark_on", path, st, got)
            except Exception:
                pass
        _DARK_ON[key] = {int(k): v for k, v in got.items()}
    return _DARK_ON[key]


def _add_dark_on(series, ch, x, rec, run_id):
    v = dark_on(rec.get("path", "")).get(ch)
    if v is not None and v == v:
        key = (DARK_ON, ch)
        xs, ys, es, ids = series.setdefault(key, ([], [], [], []))
        xs.append(x); ys.append(v); es.append(0.0); ids.append(run_id)


def _manifests(a, b):
    import glob, json, os
    out = []
    for p in glob.glob(os.path.join(rt.HVSCAN_DIR, "*", "hvscan_[0-9][0-9][0-9][0-9].json")):
        try:
            with open(p, encoding="utf-8") as f:
                m = json.load(f)
        except (OSError, ValueError):
            continue
        t = rt._parse(m.get("started"))
        if t is not None and a - timedelta(minutes=5) <= t <= b:
            out.append((p, m))
    return sorted(out, key=lambda x: x[1].get("started") or "")


def hv_curve(wins):
    """{(metric, ch): ([HV], [value], [err], [(date, run)])} of the HV scan points (settled VMon, else the requested HV)."""
    out = {}
    if not wins:
        return out
    for path, m in _manifests(min(w[1] for w in wins), max(w[2] for w in wins)):
        import os
        date = os.path.basename(os.path.dirname(path))
        for pt in m.get("points") or []:
            rows = [{"date": date, "run": str(r)} for r in pt.get("runs") or []]
            recs = [rec for _r, rec in _records(rows)]
            vm, req = pt.get("vmon") or {}, pt.get("hv") or {}
            for ch in range(3):
                x = vm.get(str(ch), vm.get(ch)) or req.get(str(ch))
                if x is None:
                    continue
                for metric in METRICS:
                    for rec in recs:
                        pair = _metric(rec, metric, ch)
                        if pair:
                            xs, ys, es, ids = out.setdefault((metric, ch), ([], [], [], []))
                            xs.append(float(x)); ys.append(pair[0]); es.append(pair[1]); ids.append(_run_id(rec))
    return out


def stability(run, now=None):
    """QE / Gain / TTS FWHM of the laser runs of the run's Stability (QE/Gain) steps:
    {"steps", "rows", "series": {(metric, ch): ([time], [value], [err], [(date, run)])}}."""
    wins = windows(run, ("stability_qe",), now)
    rows = registry_rows(wins, lambda r: r.get("mode") == "laser" and r.get("kind") in ("stability", "manual"))
    series = {}
    for r, rec in _records(rows):
        for ch in range(3):
            for metric in METRICS:
                pair = _metric(rec, metric, ch)
                if pair:
                    ts, ys, es, ids = series.setdefault((metric, ch), ([], [], [], []))
                    ts.append(r["_time"]); ys.append(pair[0]); es.append(pair[1]); ids.append(_run_id(r))
            _add_dark_on(series, ch, r["_time"], rec, _run_id(r))
    return {"steps": wins, "rows": rows, "series": series}


def apply_check(run, now=None):
    """Laser check runs of the Stability (QE/Gain) step right after an HV apply step:
    {(metric, ch): ([time], [value], [err], [(date, run)])}, plus the registry rows."""
    steps = [s for s in run.get("steps") or [] if s.get("enabled", True)]
    out, rows = {}, []
    for i, s in enumerate(steps[:-1]):
        nxt = steps[i + 1]
        if s.get("kind") != "hv_apply" or nxt.get("kind") != "stability_qe" or not nxt.get("started"):
            continue
        a = rt._parse(nxt["started"])
        b = rt._parse(nxt.get("finished")) or now or rt._now()
        got = registry_rows([(nxt, a, b)], lambda r: r.get("mode") == "laser" and r.get("kind") in ("stability", "manual"))
        rows += got
        for r, rec in _records(got):
            for ch in range(3):
                for metric in METRICS:
                    pair = _metric(rec, metric, ch)
                    if pair:
                        ts, ys, es, ids = out.setdefault((metric, ch), ([], [], [], []))
                        ts.append(r["_time"]); ys.append(pair[0]); es.append(pair[1]); ids.append(_run_id(r))
    return out, rows


# ---------------------------------------------------------------- HV
def vmon(a, b, db=None):
    """(times, {ch: values}) of HV VMon between a and b from the HV Monitor database, thinned to VMON_MAX_POINTS."""
    db = db or env.HV_DB
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        rows = con.execute("SELECT timestamp, Ch0_V, Ch1_V, Ch2_V FROM monitoring_data "
                           "WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
                           (a.replace(tzinfo=None).isoformat(), b.replace(tzinfo=None).isoformat())).fetchall()
        con.close()
    except Exception:
        return [], {}
    step = max(1, len(rows) // VMON_MAX_POINTS)
    rows = rows[::step]
    ts = [_t(r[0]) for r in rows]
    return ts, {ch: [r[ch + 1] for r in rows] for ch in range(3)}


PC_PER_1E7 = 1.602    # SPE charge of a 1.0 x 10^7 gain


def hv_fit(wins):
    """Newest gain fit of the HV scans in the windows: {"mantissa", "chans": {ch: fit dict}, "file"} or None."""
    import os
    if not wins:
        return None
    for path, _m in reversed(_manifests(min(w[1] for w in wins), max(w[2] for w in wins))):
        gpath = path[:-5] + "_gain.json"
        if not os.path.exists(gpath):
            continue
        try:
            d, chans = rt.read_gain_json(gpath)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        return {"mantissa": rt._num(d.get("target_gain_1e7")) or 1.0, "chans": chans, "file": os.path.basename(gpath)}
    return None


def hv(run, kind, now=None):
    """{"steps": [(step, a, b)], "times", "vmon", "rows"} of the hv_scan or hv_apply steps."""
    wins = windows(run, (kind,), now)
    if not wins:
        return {"steps": [], "times": [], "vmon": {}, "rows": []}
    a = min(w[1] for w in wins) - timedelta(minutes=2)
    b = max(w[2] for w in wins) + timedelta(minutes=2)
    ts, vm = vmon(a, b)
    rows = registry_rows(wins, lambda r: r.get("kind") == rt.HVSCAN_KIND) if kind == "hv_scan" else []
    out = {"steps": wins, "times": ts, "vmon": vm, "rows": rows}
    if kind == "hv_scan":
        out["curve"] = hv_curve(wins)
        out["fit"] = hv_fit(wins)
    else:
        out["check"], out["check_rows"] = apply_check(run, now)
    return out


# ---------------------------------------------------------------- general scan
GENERAL_TITLE = {"general_scan": "General Scan", "wavelength_scan": "Wavelength Scan"}


def mon_ratio(xs, recs, ch):
    """(x, poisson_mu(ch) / poisson_mu(Monitor), err) of the same runs; the error combines the relativeQE_raw
    relative errors of both channels (FinalResult stores no mu error)."""
    from core import routine_stats as RS
    out = ([], [], [])
    for x, p in zip(xs, recs):
        v = RS._mu_gain(p["path"])
        if ch not in v or 0 not in v or v[0][0] <= 0:
            continue
        r = v[ch][0] / v[0][0]
        q, q0 = p["data"]["qe"].get(ch), p["data"]["qe"].get(0)
        rel = ((q[1] / q[0]) ** 2 + (q0[1] / q0[0]) ** 2) ** 0.5 if q and q0 and q[0] and q0[0] else 0.0
        out[0].append(x)
        out[1].append(r)
        out[2].append(r * rel)
    return out


def _step_label(s):
    """Step name; a General Scan step with its own wavelength gets it appended unless the name has it."""
    name = s.get("name") or GENERAL_TITLE.get(s.get("kind"), s.get("kind"))
    wl = rt.scan_wavelength(s.get("options")) if s.get("kind") == "general_scan" else ""
    return name if not wl or wl in name or f"{wl[:-2]} nm" in name else f"{name} {wl}"


def step_names(wins):
    """Display name per step window; repeated names get their step number."""
    names = [_step_label(s) for s, _a, _b in wins]
    return [n if names.count(n) == 1 else f"{n} ({i + 1})" for i, n in enumerate(names)]


def general(run, now=None):
    """General / Wavelength scan steps (CAEN) plus HK Digitizer Scan steps of the run, as one set of series."""
    out = _caen_general(run, now)
    try:
        _add_hk(run, now, out)
    except Exception as e:   # the HK PC being away must not hide the CAEN results
        out["hk"] = {"runs": [], "total": 0, "error": str(e)}
    return out


def _add_hk(run, now, out):
    """HK Digitizer Scan steps: the HK points taken inside each step's time window, under the CAEN series keys
    (hits per trigger as "qe", charge as "gain", timing FWHM as "fwhm"); ids are ("HK" + date, point name)."""
    from core import hk_results as HK
    wins = windows(run, ("hk_scan",), now)
    info = out["hk"] = {"runs": [], "total": 0, "error": ""}
    for (step, a, b), name in zip(wins, step_names(wins)):
        t0, t1 = a.timestamp(), b.timestamp() + GRACE_S
        recs = []
        for date in sorted({a.astimezone(JST).strftime("%Y%m%d"), b.astimezone(JST).strftime("%Y%m%d")}):
            names = [n for n, e in HK.refresh(date).items() if t0 <= e["t"] <= t1]
            info["total"] += len(names)
            HK.request(date, names)
            recs += [p for p in (HK.point(date, n) for n in names) if p]
        info["error"] = HK.last_error
        if not recs:
            continue
        recs.sort(key=lambda p: p["time"])
        info["runs"] += [p["run"] for p in recs]
        for metric in METRICS:
            for xmode, xkey in (("raw", "raw"), ("hamamatsu", "ham")):
                for axis in ("X", "Y"):
                    for ch in (0, 1, 2):
                        pts = [(p, p["data"][metric][ch]) for p in recs
                               if p["axis"].get(ch) == axis and ch in p["data"][metric]]
                        if not pts:
                            continue
                        key = (metric, name, ch, axis, xmode)
                        out["series"][key] = ([p[xkey][ch] for p, _v in pts], [v[0] for _p, v in pts],
                                              [v[1] for _p, v in pts])
                        out["ids"][key] = [("HK" + p["date"], p["name"]) for p, _v in pts]
                        if metric == "qe" and ch:
                            mon = [(p, v, p["data"]["qe"].get(0)) for p, v in pts]
                            mon = [(p, v, m) for p, v, m in mon if m and m[0] > 0 and v[0] > 0]
                            key = ("qe_mon", name, ch, axis, xmode)
                            out["series"][key] = (
                                [p[xkey][ch] for p, _v, _m in mon], [v[0] / m[0] for _p, v, m in mon],
                                [v[0] / m[0] * ((v[1] / v[0]) ** 2 + (m[1] / m[0]) ** 2) ** 0.5 for _p, v, m in mon])
                            out["ids"][key] = [("HK" + p["date"], p["name"]) for p, _v, _m in mon]
        if name not in out["sessions"]:
            out["sessions"].append(name)
            out["steps"].append(name)
            out["kinds"].append("hk_scan")
            out["wls"].append(str(recs[0].get("wl") or ""))


def _next_block(a, b, mine):
    """True when dataset b is the next run block of the scan of dataset a (a scan with more points than one
    block runs over the block edge); mine = (date, run) of the step."""
    edge = b["runs"][0] // rt.SCAN_BLOCK * rt.SCAN_BLOCK
    return (a["kind"] == b["kind"] and a["date"] == b["date"] and a.get("wl") == b.get("wl")
            and a["runs"][0] // rt.SCAN_BLOCK * rt.SCAN_BLOCK == edge - rt.SCAN_BLOCK
            and (b["date"], edge - 1) in mine and (b["date"], edge) in mine)


def _store(out, key, x, y, e, ids, add=False):
    """Store a series and its run ids; add=True appends to what an earlier run block of the same scan stored."""
    if add and key in out["series"]:
        ox, oy, oe = out["series"][key]
        x, y, e, ids = list(ox) + list(x), list(oy) + list(y), list(oe) + list(e), list(out["ids"][key]) + list(ids)
    out["series"][key], out["ids"][key] = (x, y, e), ids


def _caen_general(run, now=None):
    """QE, Gain and TTS FWHM per point of the run's General / Wavelength scan steps, like Live Scan.
    -> {"rows", "series": {(metric, session label, ch, scan axis, x mode): (x, y, err)}, "ids": {same key:
    [(date, run)]}, "fail": {same key: (x, y, ids) of the failed fits, valued from the stored fit function},
    "sessions": [label],
    "steps": [step name per session label], "kinds": [step kind per session label], "wls": [wavelength per label]}."""
    from core.live_scan import LiveScanModel, uproot
    wins = windows(run, ("general_scan", "wavelength_scan"), now)
    rows = registry_rows(wins, lambda r: r.get("kind") == "general")
    out = {"rows": rows, "series": {}, "ids": {}, "fail": {}, "sessions": [], "steps": [], "kinds": [], "wls": []}
    if not rows or uproot is None:
        return out
    want = {(r["date"], int(r["run"])) for r in rows if r.get("run", "").isdigit()}
    m = LiveScanModel()
    m.discover()
    keys = [k for k, s in m.sessions.items() if any((s["date"], x) in want for x in s["runs"])]
    keys.sort(key=lambda k: (m.sessions[k]["date"], m.sessions[k]["runs"][0]))
    m.selected, m.touched = list(keys), True
    m.poll()
    for win, name in zip(wins, step_names(wins)):
        mine = {(r["date"], int(r["run"])) for r in registry_rows([win], lambda r: r.get("kind") == "general")
                if r.get("run", "").isdigit()}
        skeys = [k for k in keys if any((m.sessions[k]["date"], x) in mine for x in m.sessions[k]["runs"])]
        more = {k for a, k in zip(skeys, skeys[1:]) if _next_block(m.sessions[a], m.sessions[k], mine)}
        tag = None
        for k in skeys:
            s = m.sessions[k]
            add = k in more   # second run block of a long scan: same label, points appended
            if not add:
                tag = f"{s['wl']} nm" if s.get("wl") else f"{s['runs'][0]:03d}"
            lab = (f"{name} {tag}" if win[0].get("kind") == "wavelength_scan" or len(skeys) - len(more) > 1
                   else name)
            got = False
            for metric in METRICS:
                m.metric = metric
                for xmode in ("raw", "hamamatsu"):
                    m.axis_mode = xmode
                    for axis in ("X", "Y"):
                        m.axis_filter = axis
                        for ch in (0, 1, 2):
                            if metric != "qe":
                                fx, fy, _fe, frecs = m.series(k, ch, failed=True)
                                fi = [i for i, p in enumerate(frecs) if (p["date"], p["run"]) in mine]
                                if fi:
                                    fkey = (metric, lab, ch, axis, xmode)
                                    new = ([fx[i] for i in fi], [fy[i] for i in fi], [_run_id(frecs[i]) for i in fi])
                                    old = out["fail"].get(fkey) if add else None
                                    out["fail"][fkey] = tuple(a + b for a, b in zip(old, new)) if old else new
                            xs, ys, es, recs = m.series(k, ch)
                            keep = [i for i, p in enumerate(recs) if (p["date"], p["run"]) in mine
                                    and (metric == "qe" or ys[i] > 0)]   # failed fits are stored as -1
                            if keep:
                                got = True
                                _store(out, (metric, lab, ch, axis, xmode), [xs[i] for i in keep],
                                       [ys[i] for i in keep], [es[i] for i in keep], [_run_id(recs[i]) for i in keep], add)
                                if metric == "qe":
                                    dk = {}
                                    for i in keep:
                                        _add_dark_on(dk, ch, xs[i], recs[i], _run_id(recs[i]))
                                    if dk:
                                        x0, y0, e0, i0 = dk[(DARK_ON, ch)]
                                        _store(out, (DARK_ON, lab, ch, axis, xmode), x0, y0, e0, i0, add)
                                if metric == "qe" and ch:
                                    mon = mon_ratio([xs[i] for i in keep], [recs[i] for i in keep], ch)
                                    at = {xs[i]: _run_id(recs[i]) for i in keep}
                                    _store(out, ("qe_mon", lab, ch, axis, xmode), *mon, [at.get(x) for x in mon[0]], add)
            if got and lab not in out["sessions"]:
                out["sessions"].append(lab)
                out["steps"].append(name)
                out["kinds"].append(win[0].get("kind"))
                out["wls"].append(str(s.get("wl") or ""))
    return out


def routine_file(run):
    """Data/Routine/<YYYYMMDD_HHMM>_<name>.root that routine_summary.py writes for the run, or '' without a start time."""
    t = rt._parse(run.get("started_at"))
    if t is None:
        return ""
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", run.get("name") or "routine").strip("_") or "routine"
    return os.path.join(env.ADC_DIR, "Data", "Routine", f"{t:%Y%m%d_%H%M}_{name}.root")


def scan_files(run, now=None):
    """[(label, path)] of the existing Data/Scan files of the run's General / Wavelength steps, one per date and run block."""
    wins = windows(run, ("general_scan", "wavelength_scan"), now)
    out = []
    for win, name in zip(wins, step_names(wins)):
        blocks = {}
        for r in registry_rows([win], lambda r: r.get("kind") == "general"):
            if str(r.get("run", "")).isdigit():
                blocks.setdefault((r["date"], int(r["run"]) // rt.SCAN_BLOCK), []).append(int(r["run"]))
        spans = []   # a scan with more points than one block continues into the next block
        for (date, _b), runs in sorted(blocks.items()):
            if spans and spans[-1][0] == date and max(spans[-1][1]) + 1 == min(runs):
                spans[-1][1].extend(runs)
            else:
                spans.append((date, list(runs)))
        for date, runs in spans:
            r0, r1 = min(runs), max(runs)
            path = os.path.join(env.ADC_DIR, "Data", "Scan", f"{date}_{r0:03d}-{r1:03d}_scan.root")
            if os.path.isfile(path):
                out.append((f"{date} {r0:03d}-{r1:03d}  {name}", path))
    return out


def pmt_text(run, now=None):
    """'Mon EM2740 (A)   Rot1 EC0430 (G)   Rot2 EM1320 (E)' of the run's registered runs; sets in time order joined by ' | '."""
    out = []
    for r in registry_rows(windows(run, rt.KINDS, now), lambda r: True):
        parts = []
        for k, name in ((1, "Mon"), (2, "Rot1"), (3, "Rot2")):
            sn, d = (r.get(f"sn{k}") or "").strip(), (r.get(f"dir{k}") or "").strip()
            if sn:
                parts.append(f"{name} {sn}" + (f" ({d})" if d and d != "-" else ""))
        text = "   ".join(parts)
        if text and text not in out:
            out.append(text)
    return " | ".join(out)


def hv_pmt_text(rows):
    """'Monitor EL8460-G 1760 V   Rot1 EM1320-E 1750 V   Rot2 EM2230-F 1790 V' per PMT set of the rows, in time order;
    several HV values of one set are joined by ' / '."""
    sets = {}
    for r in sorted(rows, key=lambda r: r["_time"]):
        key = tuple(((r.get(f"sn{k}") or "").strip(), (r.get(f"dir{k}") or "").strip()) for k in (1, 2, 3))
        hv = sets.setdefault(key, {1: [], 2: [], 3: []})
        for k in (1, 2, 3):
            v = (r.get(f"hv{k}") or "").strip()
            if v and v not in hv[k]:
                hv[k].append(v)
    out = []
    for key, hv in sets.items():
        parts = []
        for k, name in ((1, "Monitor"), (2, "Rot1"), (3, "Rot2")):
            sn, d = key[k - 1]
            pmt = (sn or "?") + (f"-{d}" if d and d != "-" else "-?")
            vals = sorted(hv[k], key=lambda v: float(v) if v.replace(".", "", 1).isdigit() else 0)
            if sn or vals:
                parts.append(f"{name} {pmt} {' / '.join(vals) or '-'} V")
        if parts:
            out.append("   ".join(parts))
    return out


def routine_type(run):
    """'HK', 'ShortCross', 'GeneralScan' or 'Other' from the step kinds of the steps that ran (else the enabled ones)."""
    steps = run.get("steps") or []
    steps = [s for s in steps if s.get("started")] or [s for s in steps if s.get("enabled", True)]
    kinds = [s.get("kind") for s in steps]
    if "hk_scan" in kinds:
        return "HK"
    gen = [s for s in steps if s.get("kind") in ("general_scan", "wavelength_scan")]
    if not gen:
        return "Other"
    return "GeneralScan" if any(not (s.get("options") or {}).get("angles") for s in gen) else "ShortCross"


def archived_runs():
    """Routine runs moved to routines/history_archive, newest first, labelled like routine_runs() plus '(archived)'."""
    import glob, json
    out = []
    for p in sorted(glob.glob(os.path.join(rt._dir(), ARCHIVE, "*.json")), reverse=True):
        try:
            with open(p, encoding="utf-8") as f:
                h = json.load(f)
            t = rt._parse(h.get("started_at"))
            h["label"] = f"{t:%Y-%m-%d %H:%M}  {h.get('name', '?')}" + (
                f"  ({h['result']})" if h.get("result") not in (None, "finished") else "") + "  (archived)"
            h["archived"] = True
            out.append(h)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return out


def data_counts(runs, now=None):
    """Registry runs inside each routine run's step windows (one registry read for all); an HK Digitizer Scan step
    that ran counts as one (its points are on the HK PC, not in the registry)."""
    import bisect
    reg = sorted(t.timestamp() for t in (_t(r.get("registered_at")) for r in _registry()) if t is not None)
    out = []
    for run in runs:
        hit = set()
        for _s, a, b in windows(run, rt.KINDS, now):
            hit.update(range(bisect.bisect_left(reg, a.timestamp()),
                             bisect.bisect_right(reg, b.timestamp() + GRACE_S)))
        hk = any(s.get("kind") == "hk_scan" and s.get("started") and s.get("finished") for s in run.get("steps") or [])
        out.append(len(hit) + int(hk))
    return out


def report_name(run, _runs=None, _counts=None):
    """'KOR_<start YYYYMMDD>_<HHMM>_<type>'; the start time keeps names unique."""
    t = rt._parse(run.get("started_at"))
    return f"KOR_{t:%Y%m%d_%H%M}_{routine_type(run)}" if t else "KOR_run"


def run_span_all(run, now=None):
    """(first step start, last step end) of the run, or (None, None)."""
    wins = windows(run, rt.KINDS, now)
    return (min(w[1] for w in wins), max(w[2] for w in wins)) if wins else (None, None)


def people(rows):
    """({shifters}, {experts}) from the RunInfo of the first and last run of the rows (FinalResult, else prd)."""
    shifters, experts = set(), set()
    for r in {id(x): x for x in rows[:1] + rows[-1:]}.values():
        try:
            run = int(r["run"])
        except (KeyError, ValueError):
            continue
        for p in (os.path.join(env.ADC_DIR, "Data", "FinalResult", f"precal_result_kor_run_{r['date']}_{run:03d}.root"),
                  os.path.join(env.ADC_DIR, "Data", "production", f"precal_prd_kor_run_{r['date']}_{run:03d}.root")):
            try:
                st = disk_cache.stamp(p)
            except OSError:
                continue
            got = disk_cache.get("people", p, st)
            if got is None:
                try:
                    import uproot
                    with uproot.open(p) as f:
                        ri = f["RunInfo"].arrays(["Shifter", "Expert"], library="np")
                    got = tuple(str(ri[k][0].decode() if isinstance(ri[k][0], bytes) else ri[k][0]).strip()
                                for k in ("Shifter", "Expert"))
                    disk_cache.put("people", p, st, got)
                except Exception:
                    continue
            shifters.add(got[0])
            experts.add(got[1])
            break
    return {x for x in shifters if x and x != "-"}, {x for x in experts if x and x != "-"}


MON_DB = os.path.expanduser("~/Logs/Monitoring/precal_monitor.db")   # precal-monitor ingest (B-field log)
COND_BIN_S = 120
MAGNETOMETERS = (2, 3)   # the B-field log's M2 and M3 sit at this setup
MAG_NAMES = {2: "Rot1 (M2)", 3: "Rot2 (M3)"}   # magnetometer M2 sits at Rot1, M3 at Rot2
# labels as the HV Monitor shows these boxes (config_precal.json display_name)
ENV_SENSORS = (("Rot#1", "Dark_Box_1"), ("Rot#2", "Dark_Box_2"), ("Mon.", "Dark_Box_3"),
               ("Rot1", "Rot1"), ("Rot2", "Rot2"))


def conditions(a, b):
    """Lab conditions between a and b (naive local times): {group: {name: ([datetime], [value])}} with groups
    "T" / "H" (HV Monitor environment sensors), "HV" (VMon per channel; these three as 2 min means),
    "HVI" (IMon high range per channel, uA), "B" (|B| of MAGNETOMETERS, mG) and
    "I" (coil currents, A); series without any value are left out."""
    out = {"T": {}, "H": {}, "HV": {}, "HVI": {}, "B": {}, "I": {}}

    def add(group, name, ts, vs):
        pts = [(t, v) for t, v in zip(ts, vs) if v is not None]
        if pts:
            out[group][name] = ([t for t, _v in pts], [v for _t, v in pts])

    cols = ([(g, n, f"{c}_{g}") for n, c in ENV_SENSORS for g in ("T", "H")]
            + [(g, n, f"Ch{ch}_{c}") for g, c in (("HV", "V"), ("HVI", "I_H"))
               for ch, n in ((0, "Monitor"), (1, "Rot1"), (2, "Rot2"))])
    try:
        con = sqlite3.connect(f"file:{env.HV_DB}?mode=ro", uri=True, timeout=2)
        rows = con.execute(f"SELECT timestamp, {', '.join(c for _g, _n, c in cols)} FROM monitoring_data "
                           "WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
                           (a.isoformat(), b.isoformat())).fetchall()
        con.close()
        bins = {}   # 2 min means: the 10 s samples flicker between 0.1 steps
        for r in rows:
            t = datetime.fromisoformat(r[0])
            bins.setdefault(int(t.timestamp() // COND_BIN_S), []).append(r[1:])
        ts = [datetime.fromtimestamp((k + 0.5) * COND_BIN_S) for k in bins]
        for k, (g, n, _c) in enumerate(cols):
            vals = []
            for grp in bins.values():
                v = [x[k] for x in grp if x[k] is not None]
                vals.append(sum(v) / len(v) if v else None)
            add(g, n, ts, vals)
    except Exception:
        pass
    try:
        con = sqlite3.connect(f"file:{MON_DB}?mode=ro", uri=True, timeout=2)
        rows = con.execute("SELECT ts, m1_mg, m2_mg, m3_mg, m4_mg, i1, i2, i3, i4 FROM bfield "
                           "WHERE ts >= ? AND ts <= ? ORDER BY ts",
                           (f"{a:%Y-%m-%d %H:%M:%S}", f"{b:%Y-%m-%d %H:%M:%S}")).fetchall()
        con.close()
        ts = [datetime.fromisoformat(r[0]) for r in rows]
        for k in range(4):
            if k + 1 in MAGNETOMETERS:
                add("B", MAG_NAMES.get(k + 1, f"M{k + 1}"), ts, [r[1 + k] for r in rows])
            add("I", f"I{k + 1}", ts, [r[5 + k] for r in rows])
    except Exception:
        pass
    return out


def load(run, now=None):
    """Everything the Results tab draws for one routine run (slow: registry, DB and ROOT reads)."""
    out = {"dark": dark(run, now), "hv_scan": hv(run, "hv_scan", now), "stability": stability(run, now),
           "general": general(run, now), "scan_files": scan_files(run, now), "pmts": pmt_text(run, now)}
    disk_cache.flush()
    return out

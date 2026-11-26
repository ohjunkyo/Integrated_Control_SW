"""Daily Routine > Statistics: one block of numbers per step of a routine run (per channel Monitor / Rot1 / Rot2),
from the run registry, FinalResult files, HV scan manifests, the HV Monitor database and the FG change log.
Qt-free; reads only. Rows are strings, ready for the table and the CSV export."""
import csv
import glob
import json
import math
import os
from datetime import datetime, timedelta

from core import env
from core import routine as rt
from core import routine_results as RR
from core.schedule import JST

CHS = (0, 1, 2)
HV_OK_V = 2.0                 # HV Apply: |VMon - target| within this is OK (hv_request settle tolerance)
DRIFT_WARN_PCT = 1.0          # Stability: first -> last gain change above this is flagged
FG_LOG = os.path.join(env.ADC_DIR, "LOG", "FG")
KIND_TITLE = {"general_scan": "General Scan", "hk_scan": "HK Digitizer Scan", "wavelength_scan": "Wavelength Scan", "dark_rate": "Dark Rate",
              "stability_qe": "Stability (QE/Gain)", "hv_scan": "HV Scan", "hv_apply": "HV Apply"}


# ---------------------------------------------------------------- helpers
def _mean_std(v):
    v = [x for x in v if x is not None and math.isfinite(x)]
    if not v:
        return None, None, 0
    m = sum(v) / len(v)
    s = math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1)) if len(v) > 1 else 0.0
    return m, s, len(v)


def _f(x, d=2):
    return "-" if x is None else f"{x:.{d}f}"


def _ms(v, d=2):
    m, s, n = _mean_std(v)
    return "-" if m is None else (f"{m:.{d}f} ± {s:.{d}f}" if n > 1 else f"{m:.{d}f}")


def _spread(v):
    """Relative spread std / mean in %."""
    m, s, n = _mean_std(v)
    return "-" if m in (None, 0) or n < 2 else f"{100.0 * s / abs(m):.1f} %"


def _range(v, d=2):
    v = [x for x in v if x is not None]
    return "-" if not v else f"{min(v):.{d}f} - {max(v):.{d}f}"


def _row(label, vals=None, note="", runs=""):
    vals = vals or {}
    return [label] + [vals.get(ch, "") for ch in CHS] + [runs, note]


def _dur(sec):
    if sec is None:
        return "-"
    m = int(round(sec / 60))
    return f"{m // 60} h {m % 60} min" if m >= 60 else f"{m} min"


def trigger_at(t):
    """Section-A pulse-generator rate (Hz) in force at time t, from the FG change log, or None."""
    best = None
    for path in sorted(glob.glob(os.path.join(FG_LOG, "fg_*.csv"))):
        try:
            with open(path, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    if r.get("section") != "A" or r.get("action") != "configure pulse_generator":
                        continue
                    ts = RR._t(r.get("timestamp"))
                    if ts is None or ts > t:
                        continue
                    cfg = json.loads(r.get("after") or "{}")
                    if float(cfg.get("period") or 0) > 0 and not cfg.get("frequency_type"):
                        if best is None or ts > best[0]:
                            best = (ts, 1e9 / float(cfg["period"]))
        except (OSError, ValueError):
            continue
    return best[1] if best else None


def _points(rows):
    """FinalResult records of the analysed runs among registry-like rows (date, run)."""
    return [rec for _r, rec in RR._records(rows)]


def _vals(points, metric, ch):
    return [pair[0] for pair in (RR._metric(p, metric, ch) for p in points) if pair]


def _dark_on(points, ch):
    return [v for v in (RR.dark_on(p.get("path", "")).get(ch) for p in points) if v is not None]


def _rel_errs(points, metric, ch):
    """Per-point statistical errors in % of the value."""
    return [100.0 * abs(pair[1] / pair[0]) for pair in (RR._metric(p, metric, ch) for p in points)
            if pair and pair[0] and pair[1] is not None and math.isfinite(pair[1])]


def _stat(v):
    """Median of the per-point statistical errors, in %."""
    m = _median(v)
    return "-" if m is None else f"{m:.2f} %"


# ---------------------------------------------------------------- per kind
def _dark(step, a, b):
    from core import stability as stab
    rows = RR.registry_rows([(step, a, b)], lambda r: r.get("mode") == "dark" and r.get("status", "").startswith("done"))
    rate, clean, raw, unc, corr = ({ch: [] for ch in CHS} for _ in range(5))
    for r in rows:
        path = stab._raw_dark_path(r.get("file", ""))
        try:
            v = stab._run_rate_detail(path) if path else None
        except Exception:
            v = None
        if v and v["ch"] in rate:
            ch = v["ch"]
            rate[ch].append(v["live"] / 1000.0)
            raw[ch].append(v["raw"] / 1000.0)
            corr[ch].append(100.0 * v["corr"])
            if v.get("uncorr"):
                unc[ch].append(v["uncorr"] / 1000.0)
            c = stab._prd_clean_rate(r.get("file", ""), ch)
            if c:
                clean[ch].append(c / 1000.0)
    out = [_row("Dark rate [kHz]", {ch: _ms(rate[ch]) for ch in CHS}),
           _row("Recorded rate [kHz]", {ch: _ms(raw[ch]) for ch in CHS}),
           _row("Uncorrelated rate [kHz]", {ch: _ms(unc[ch]) for ch in CHS}),
           _row("Correlated fraction [%]", {ch: _ms(corr[ch], 1) for ch in CHS}),
           _row("Dark rate range [kHz]", {ch: _range(rate[ch]) for ch in CHS}),
           _row("Dark rate spread [%]", {ch: _spread(rate[ch]) for ch in CHS}),
           _row("Run count", {ch: str(len(rate[ch])) for ch in CHS}, runs=RR.run_span(rows))]
    if any(clean.values()):
        out.insert(1, _row("Clean rate [kHz]", {ch: _ms(clean[ch]) for ch in CHS}))
    return out, rows


def _hv_scan(step, a, b):
    out, rows = [], []
    for path, m in RR._manifests(a, b):
        date = os.path.basename(os.path.dirname(path))
        out.append(_row("Base HV [V]", {ch: _f(m.get("base_hv", {}).get(str(ch)), 0) for ch in CHS},
                        os.path.basename(path)))
        for pt in m.get("points") or []:
            pts = _points([{"date": date, "run": str(r)} for r in pt.get("runs") or []])
            vm = pt.get("vmon") or {}
            lab = f"{float(pt.get('offset_v', 0)):+g} V"
            out.append(_row(f"{lab}  HV [V]", {ch: _f(vm.get(str(ch), vm.get(ch)) or pt.get("hv", {}).get(str(ch)), 0)
                                               for ch in CHS}, pt.get("status", ""),
                            runs=", ".join(f"{int(r):03d}" for r in pt.get("runs") or [])))
            out.append(_row(f"{lab}  QE [%]", {ch: _ms(_vals(pts, "qe", ch)) for ch in CHS}))
            out.append(_row(f"{lab}  Gain [pC]", {ch: _ms(_vals(pts, "gain", ch), 3) for ch in CHS}))
            out.append(_row(f"{lab}  TTS FWHM [ns]", {ch: _ms(_vals(pts, "fwhm", ch)) for ch in CHS}))
        gpath = path[:-5] + "_gain.json"
        if os.path.exists(gpath):
            try:
                d, chans = rt.read_gain_json(gpath)
                tgt = rt._num(d.get("target_gain_1e7"))
                out.append(_row("Fit parameters a (1 kV) / b", {ch: (f"{rt._num(chans[ch].get('a_1kV')) or 0:.4g} / "
                                                         f"{rt._num(chans[ch].get('b')) or 0:.2f}")
                                                     if ch in chans and chans[ch].get("fitted") else "not fitted"
                                                     for ch in CHS}))
                out += _fit_rows(chans, tgt or 1.0)
            except (OSError, ValueError, KeyError, TypeError) as e:
                out.append(_row("Gain curve", {}, f"unreadable: {e}"))
    rows = RR.registry_rows([(step, a, b)], lambda r: r.get("kind") == rt.HVSCAN_KIND)
    if not out:
        out.append(_row("HV Scan", {}, "no manifest in this window"))
    return out, rows


def _fit_rows(chans, tgt):
    """HV for 1 x 10^7, for the fit's target gain when it is not 1, and the fit flags per channel."""
    def hv_at(ch, m):
        c = chans.get(ch)
        if not c or not c.get("fitted"):
            return "-"
        v, e, _x = rt.gain_target(c, m, tgt)
        return "-" if not v else f"{v:.0f}" + (f" ± {e:.0f}" if e is not None else "")
    out = [_row("HV for gain 1 x 10^7 [V]", {ch: hv_at(ch, 1.0) for ch in CHS})]
    if abs(tgt - 1.0) > 1e-9:
        out.append(_row(f"HV for target gain {tgt:g} x 10^7 [V]", {ch: hv_at(ch, tgt) for ch in CHS}))
    out.append(_row("Fit flags", {ch: (", ".join(rt._flags(chans[ch], bool(chans[ch].get("extrapolated")))) or "ok")
                                  if ch in chans and chans[ch].get("fitted") else "-" for ch in CHS}))
    return out


def _vmon_at(t):
    """VMon per channel of the newest HV DB row at or before t."""
    ts, vm = RR.vmon(t - timedelta(minutes=2), t)
    return {ch: (vm.get(ch) or [None])[-1] for ch in CHS} if ts else {}


def _hv_apply(step, a, b):
    import re
    target = {}
    for ch, v in re.findall(r"Ch(\d) (\d+(?:\.\d+)?) V", str(step.get("note") or "")):
        target[int(ch)] = float(v)
    end = _vmon_at(b)
    out = [_row("Target VSet [V]", {ch: _f(target.get(ch), 0) for ch in CHS}, (step.get("options") or {}).get("mode", "fixed")),
           _row("VMon at end [V]", {ch: _f(end.get(ch), 1) for ch in CHS})]
    dev, verdict = {}, {}
    for ch in CHS:
        if ch in target and end.get(ch) is not None:
            d = end[ch] - target[ch]
            dev[ch] = f"{d:+.1f}"
            verdict[ch] = "OK" if abs(d) <= HV_OK_V else "Check"
    out.append(_row("HV deviation [V]", dev))
    out.append(_row("Result", verdict))
    return out, []


def _stability_qe(step, a, b):
    rows = RR.registry_rows([(step, a, b)], lambda r: r.get("mode") == "laser" and r.get("kind") in ("stability", "manual")
                            and r.get("status", "").startswith("done"))
    out = []
    by_wl = {}
    for r in rows:
        by_wl.setdefault(r.get("wavelength") or "?", []).append(r)
    for wl, rs in by_wl.items():
        pts = _points(rs)
        g = {ch: _vals(pts, "gain", ch) for ch in CHS}
        out.append(_row(f"{wl} nm  QE [%]", {ch: _ms(_vals(pts, "qe", ch)) for ch in CHS}, f"{len(pts)} of {len(rs)} analysed"
                        + (f"   laser {RR.laser_mA(rs)}" if RR.laser_mA(rs) else ""), runs=RR.run_span(rs)))
        out.append(_row(f"{wl} nm  QE spread [%]", {ch: _spread(_vals(pts, "qe", ch)) for ch in CHS}))
        out.append(_row(f"{wl} nm  Gain [pC]", {ch: _ms(g[ch], 3) for ch in CHS}))
        out.append(_row(f"{wl} nm  TTS FWHM [ns]", {ch: _ms(_vals(pts, "fwhm", ch)) for ch in CHS}))
        out.append(_row(f"{wl} nm  {RR.DARK_ON_LABEL}", {ch: _ms(_dark_on(pts, ch)) for ch in CHS}))
        drift, flag = {}, []
        for ch in CHS:
            if len(g[ch]) >= 2 and g[ch][0]:
                d = 100.0 * (g[ch][-1] - g[ch][0]) / g[ch][0]
                drift[ch] = f"{d:+.2f} %"
                if abs(d) > DRIFT_WARN_PCT:
                    flag.append(["Monitor", "Rot1", "Rot2"][ch])
        out.append(_row(f"{wl} nm  Gain change first -> last [%]", drift,
                        ("Check: " + ", ".join(flag)) if flag else f"stable within {DRIFT_WARN_PCT:g} %" if drift else ""))
    if not out:
        out.append(_row("Stability", {}, "no laser runs in this window"))
    return out, rows


def _general(step, a, b):
    rows = RR.registry_rows([(step, a, b)], lambda r: r.get("kind") == "general")
    out = []
    by_wl = {}
    for r in rows:
        by_wl.setdefault(r.get("wavelength") or "?", []).append(r)
    for wl, rs in by_wl.items():
        pts = _points(rs)
        tag = f"{wl} nm  " if len(by_wl) > 1 else ""
        g = {ch: _vals(pts, "gain", ch) for ch in CHS}
        q = {ch: _vals(pts, "qe", ch) for ch in CHS}
        t = {ch: _vals(pts, "fwhm", ch) for ch in CHS}
        inner = {ch: [p for p in pts if abs(float(p["ham"].get(ch) or 0)) <= 70] for ch in CHS}
        out += [_row(f"{tag}Gain [pC]", {ch: _ms(g[ch], 3) for ch in CHS}),
                _row(f"{tag}Gain range [pC]", {ch: _range(g[ch], 3) for ch in CHS}),
                _row(f"{tag}Gain spread [%]", {ch: _spread(g[ch]) for ch in CHS}),
                _row(f"{tag}QE [%]", {ch: _ms(q[ch]) for ch in CHS}),
                _row(f"{tag}QE range [%]", {ch: _range(q[ch]) for ch in CHS}),
                _row(f"{tag}QE spread [%]", {ch: _spread(q[ch]) for ch in CHS}),
                _row(f"{tag}TTS FWHM [ns]", {ch: _ms(t[ch]) for ch in CHS}),
                _row(f"{tag}{RR.DARK_ON_LABEL}", {ch: _ms(_dark_on(pts, ch)) for ch in CHS}, "mean over all angles"),
                _row(f"{tag}Statistical error QE [%]", {ch: _stat(_rel_errs(inner[ch], "qe", ch)) for ch in CHS},
                     "per point, median inside |angle| <= 70 deg"),
                _row(f"{tag}Statistical error Gain [%]", {ch: _stat(_rel_errs(inner[ch], "gain", ch)) for ch in CHS}),
                _row(f"{tag}Point count", {ch: str(len(q[ch])) for ch in CHS}, f"{len(pts)} of {len(rs)} runs analysed"
                     + (f"   laser {RR.laser_mA(rs)}" if RR.laser_mA(rs) else ""), runs=RR.run_span(rs))]
    if not out:
        out.append(_row("General Scan", {}, "no General Scan runs in this window"))
    return out, rows


def _median(v):
    v = sorted(x for x in v if x is not None and math.isfinite(x))
    if not v:
        return None
    n = len(v)
    return v[n // 2] if n % 2 else 0.5 * (v[n // 2 - 1] + v[n // 2])


_MU_CACHE = {}          # path -> (mtime, values): toggles and reloads do not re-read unchanged files


def _mu_gain(path):
    """{ch: (poisson_mu, spe_mean, gain rel. error, QE rel. error)} of one FinalResult file, cached by mtime."""
    from core import disk_cache
    from core.live_scan import uproot
    try:
        st = disk_cache.stamp(path)
    except OSError:
        return {}
    mt = st[0]
    hit = _MU_CACHE.get(path)
    if hit and hit[0] == mt:
        return hit[1]
    out = disk_cache.get("mu_gain", path, st)
    if out:
        _MU_CACHE[path] = (mt, out)
        return out
    out = {}
    try:
        with uproot.open(path) as f:
            for ch in CHS:
                try:
                    t = f[f"tree_ch{ch}"]
                    b = {k: float(t[k].array(library="np")[0])
                         for k in ("poisson_mu", "spe_mean", "spe_mean_error", "relativeQE_raw", "relativeQE_raw_err")}
                    out[ch] = (b["poisson_mu"], b["spe_mean"],
                               b["spe_mean_error"] / b["spe_mean"] if b["spe_mean"] > 0 else None,
                               abs(b["relativeQE_raw_err"] / b["relativeQE_raw"]) if b["relativeQE_raw"] else None)
                except Exception:
                    continue
    except Exception:
        pass
    if out:
        _MU_CACHE[path] = (mt, out)
        disk_cache.put("mu_gain", path, st, out)
    return out


def _by_angle(rows):
    """{ch: {(scan axis, raw angle): (channel tuple, Monitor tuple)}} of the analysed runs, see _mu_gain."""
    out = {ch: {} for ch in CHS}
    for _r, rec in RR._records(rows):
        v = _mu_gain(rec["path"])
        if 0 not in v:
            continue
        for ch in CHS:
            if ch in v:
                out[ch][(rec["axis"].get(ch), round(float(rec["raw"][ch]), 1))] = (v[ch], v[0])
    return out


def _compare(first, later):
    """Block of later / first General scan: median and sd per channel of the QE (mu / Monitor mu) and gain
    ratios over the angles both scans measured."""
    (n1, rows1), (n2, rows2) = first, later
    a, b = _by_angle(rows1), _by_angle(rows2)
    qe, gain, cnt, qexp, gexp = {}, {}, {}, {}, {}
    for ch in CHS:
        q, g, qe_, ge_ = [], [], [], []
        for k in set(a[ch]) & set(b[ch]):
            (c1, n1_), (c2, n2_) = a[ch][k], b[ch][k]
            m1, mon1, g1, m2, mon2, g2 = c1[0], n1_[0], c1[1], c2[0], n2_[0], c2[1]
            if ch != 0 and min(m1, mon1, m2, mon2) > 0:
                q.append((m2 / mon2) / (m1 / mon1))
                e = [c1[3], n1_[3], c2[3], n2_[3]]
                if None not in e:
                    qe_.append(q[-1] * math.sqrt(sum(x * x for x in e)))
            if g1 > 0 and g2 > 0:
                g.append(g2 / g1)
                if c1[2] is not None and c2[2] is not None:
                    ge_.append(g[-1] * math.hypot(c1[2], c2[2]))
        qe[ch], gain[ch], cnt[ch] = q, g, len(set(a[ch]) & set(b[ch]))
        qexp[ch], gexp[ch] = _median(qe_), _median(ge_)
    rows = [_row("QE ratio (mu / Monitor mu) median", {ch: _f(_median(qe[ch]), 4) for ch in CHS if ch != 0}),
            _row("QE ratio sd", {ch: _f(_mean_std(qe[ch])[1], 4) for ch in CHS if ch != 0}),
            _row("QE ratio sd expected (stat.)", {ch: _f(qexp[ch], 4) for ch in CHS if ch != 0}),
            _row("Gain ratio median", {ch: _f(_median(gain[ch]), 4) for ch in CHS}),
            _row("Gain ratio sd", {ch: _f(_mean_std(gain[ch])[1], 4) for ch in CHS}),
            _row("Gain ratio sd expected (stat.)", {ch: _f(gexp[ch], 4) for ch in CHS}),
            _row("Common angles", {ch: str(cnt[ch]) for ch in CHS}, runs=f"{RR.run_span(rows2)} / {RR.run_span(rows1)}")]
    return {"title": f"{n2} / {n1}", "info": "", "rows": rows}


SECTIONS = {"dark_rate": _dark, "hv_scan": _hv_scan, "hv_apply": _hv_apply, "stability_qe": _stability_qe,
            "general_scan": _general, "wavelength_scan": _general}


def stats(run, now=None):
    """[{"title", "info", "rows": [[item, mon, rot1, rot2, note]]}] per step of the run, in step order."""
    now = now or rt._now()
    out, gen = [], []
    # run windows end before the next step starts, as in the plots
    ends = {id(w[0]): w[2] for w in RR.windows(run, ("dark_rate", "stability_qe", "general_scan", "wavelength_scan"), now)}
    for i, s in enumerate(run.get("steps") or []):
        if not s.get("enabled", True):
            continue
        a, b = rt._parse(s.get("started")), rt._parse(s.get("finished")) or now
        title = f"{i + 1}. {s.get('name') or KIND_TITLE.get(s.get('kind'), s.get('kind'))}"
        if a is None:
            out.append({"title": title, "info": s.get("status") or "not run", "rows": []})
            continue
        opts = s.get("options") or {}
        hz = opts.get("trigger_hz") or trigger_at(a + timedelta(seconds=30))
        fn = SECTIONS.get(s.get("kind"))
        try:
            rows, reg = fn(s, a, ends.get(id(s), b)) if fn else ([], [])
        except Exception as e:
            rows, reg = [_row("Error", {}, f"{type(e).__name__}: {e}")], []
        evs = sorted({r.get("events") for r in reg if r.get("events")})
        est = None
        if not (s.get("kind") == "dark_rate" and opts.get("until_next_step", True)):
            used = dict(opts)
            if len(evs) == 1 and s.get("kind") != "dark_rate":
                used["events"] = int(float(evs[0]))   # the events the runs really took
            from core import stability as stab
            used["trigger_hz"] = hz or stab.RATE_HZ      # no FG record for that time: the nominal rate
            try:
                est = rt.estimate_seconds(dict(s, options=used))
            except Exception:
                est = None
        info = (f"{s.get('status', '')}   {a:%m-%d %H:%M} - {b:%H:%M}   {_dur((b - a).total_seconds())}"
                + (f" (est {_dur(est)})" if est else ""))
        if s.get("kind") not in ("dark_rate", "hv_apply") and hz:
            info += f"   trigger {hz / 1000:g} kHz" if hz >= 1000 else f"   trigger {hz:g} Hz"
        if evs:
            info += "   events " + ", ".join(f"{int(float(e)):,}" for e in evs)
        if s.get("note"):
            rows.append(_row("Note", {}, str(s["note"])))
        out.append({"title": title, "info": info, "rows": rows})
        if s.get("kind") == "general_scan" and reg:
            name = s.get("name") or KIND_TITLE["general_scan"]
            wls = tuple(sorted({str(r.get("wavelength") or "") for r in reg}))
            first = next((g for g in gen if g[2] == wls), None)     # compared with the same wavelength only
            if first:
                try:
                    out.append(_compare(first[:2], (name, reg)))
                except Exception as e:
                    out.append({"title": f"{name} / {first[0]}", "info": "",
                                "rows": [_row("Error", {}, f"{type(e).__name__}: {e}")]})
            gen.append((name, reg, wls))
    from core import disk_cache
    disk_cache.flush()
    return out


def to_csv(path, run, blocks):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["routine", run.get("name", ""), "started", run.get("started_at", ""), "exported",
                    datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")])
        w.writerow(["step", "item", "Monitor (ch0)", "Rot1 (ch1)", "Rot2 (ch2)", "run", "note"])
        for blk in blocks:
            w.writerow([blk["title"], "", "", "", "", "", blk["info"]])
            for r in blk["rows"]:
                w.writerow([blk["title"]] + r)

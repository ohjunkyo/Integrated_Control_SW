"""Live scan data: QE / Gain / TTS / Charge Resolution per point for the running
scan and for any earlier dataset, built from the FinalResult files and labelled
from Data/run_registry.csv. No UI (same rules as the Tk app's live_scan_view.py).

Values are RAW per-point results (no Monitor normalisation unless asked, no dark
subtraction); ./analyze.sh uniformity is the final analysis.
"""
import csv
import glob
import math
import os
import re
import time
from datetime import datetime

import angle_convert   # pure helper of the Tk app (no Tk import); checked against angle_convert.h
from core import disk_cache, env
from core import hk_results as HK
from core.run_info import SCAN_BLOCK

try:
    import uproot
except ImportError:                                  # pragma: no cover
    uproot = None

RUN_RE = re.compile(r"precal_result_kor_run_(\d{8})_(\d+)\.root$")
RESULT_DIR = os.path.join(env.ADC_DIR, "Data", "FinalResult")
REGISTRY = os.path.join(env.ADC_DIR, "Data", "run_registry.csv")
RAW_DIRS = [os.path.join(env.ADC_DIR, "Data", "RAW", "Laser"), os.path.join(env.ADC_DIR, "Data", "RAW", "Dark"),
            os.path.expanduser("~/external_HDD_1_4T/Data_Backup/RAW/Laser"),
            os.path.expanduser("~/external_HDD_1_4T/Data_Backup/RAW/Dark")]
BLOCK_OF = {"GS": SCAN_BLOCK, "DM": 100}   # dummy runs count up from 900 without blocks
KIND_SHORT = {"general": "GS", "stability": "ST", "manual": "MN", "dummy": "DM", "hvscan": "HV", "dark": "DK"}
MENU_KINDS = ("GS", "ST", "DK", "HK")   # single manual / test runs stay out of the dataset menu
HK_DATES = 14                     # HK Digitizer scans of this many recent dates are listed
METRICS = {
    # key -> (label, tree branch, error branch or None, transform)
    "qe":    ("Relative QE (%)", "relativeQE_raw", "relativeQE_raw_err", None),
    "gain":  ("Gain / SPE charge [pC]", "spe_mean", "spe_mean_error", None),
    "tts":   ("TTS [ns]", "rms_exG", None, lambda v: v * 2.0),
    "chres": ("Charge Resolution [%]", "charge_resolution", "charge_resolution_err", None),
    "fwhm":  ("TTS FWHM [ns]", "fwhm_exG", "fwhm_exG_err", lambda v: v * 2.0),
}


def registry():
    out = {}
    try:
        with open(REGISTRY, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    out[(r["date"], int(r["run"]))] = r
                except (KeyError, ValueError):
                    pass
    except OSError:
        pass
    return out


def age_text(t):
    age = time.time() - t
    return f"{int(age)} s ago" if age < 120 else f"{int(age // 60)} min ago" if age < 7200 else f"{age / 3600:.1f} h ago"


_POINT_CACHE = {}   # path -> (mtime, point): unchanged FinalResult files are not re-read
FIT_METRICS = ("gain", "chres", "tts", "fwhm")   # stored as -1 when the fit failed


def _fn_params(h, name):
    """(parameters, errors) of the fit function `name` stored with a histogram, or None."""
    for fn in h.member("fFunctions"):
        am = fn.all_members
        if str(am.get("fName", "")).startswith(name):
            return ([float(v) for v in am["fParams"].all_members["fParameters"]],
                    [float(v) for v in am.get("fParErrors", [])])
    return None


def _exg_shape(x, mu, s, tau):
    """Ex-Gaussian of read_ntp_v7.C without amplitude and background, overflow-safe."""
    z = (s / tau - (x - mu) / s) / math.sqrt(2.0)
    if z < 0:
        return math.exp(s * s / (2 * tau * tau) - (x - mu) / tau) * math.erfc(z)
    g = math.exp(-0.5 * ((x - mu) / s) ** 2)
    if z <= 8:
        return g * math.exp(z * z) * math.erfc(z)
    return g / (z * math.sqrt(math.pi)) * (1 - 0.5 / (z * z) + 0.75 / z ** 4)


def _exg_fwhm(mu, s, tau):
    """FWHM in samples, found like read_ntp_v7.C (peak in [mu-5, mu+20], half maximum above background)."""
    n = 2000
    pk = max((mu - 5 + 25.0 * i / n for i in range(n + 1)), key=lambda x: _exg_shape(x, mu, s, tau))
    half = _exg_shape(pk, mu, s, tau) / 2

    def cross(a, b):
        prev = a
        for i in range(1, n + 1):
            x = a + (b - a) * i / n
            if (_exg_shape(prev, mu, s, tau) - half) * (_exg_shape(x, mu, s, tau) - half) <= 0:
                return (prev + x) / 2
            prev = x
        return None
    lo, hi = cross(pk - 5 * s - 2, pk), cross(pk, pk + 8 * tau + 5)
    return hi - lo if lo is not None and hi is not None else None


def fit_fallback(f, ch, failed):
    """{metric: (value, error)} for the metrics in `failed`, from the fit functions still stored with the
    histograms (the tree holds -1 for a failed fit). Errors ignore covariances; none for the timing ones."""
    out = {}
    if failed & {"gain", "chres"}:
        try:
            got = _fn_params(f[f"Pico_ch{ch}"], "total_fit")
        except Exception:
            got = None
        if got and len(got[0]) == 7:
            p, e = got[0], (got[1] + [0.0] * 7)[:7]
            if "gain" in failed and p[2] + p[3] > 0:
                out["gain"] = (p[2] + p[3], math.hypot(e[2], e[3]))
            if "chres" in failed and p[3] > 0 and p[5] > 0:
                r = p[5] / p[3] * 100
                out["chres"] = (r, r * math.hypot(e[5] / p[5], e[3] / p[3]))
    if failed & {"tts", "fwhm"}:
        try:
            got = _fn_params(f[f"Diff_ch{ch}"], "exGausFit")
        except Exception:
            got = None
        if got and len(got[0]) == 5 and got[0][2] > 0 and got[0][3] > 0:
            mu, s, tau = got[0][1:4]
            if "tts" in failed:
                out["tts"] = (math.hypot(s, tau) * 2.0, 0.0)
            w = _exg_fwhm(mu, s, tau) if "fwhm" in failed else None
            if w and w > 0:
                out["fwhm"] = (w * 2.0, 0.0)
    return out


def failed_metrics(rec, ch):
    """Fit metrics of channel ch stored as failed (-1) in a point record."""
    return {mk for mk in FIT_METRICS if rec["data"].get(mk, {}).get(ch, (1,))[0] <= 0}


class LiveScanModel:
    MENU_RECENT, MENU_MORE = 4, 30

    def __init__(self, live_state=lambda: None):
        self.live_state = live_state   # -> (kind, date or None, block or None) of what is being taken now
        self.points = {}               # (date, run) -> record
        self.scanned = {}              # path -> mtime of the last successful read
        self.wl_cache = {}             # path -> wavelength (menu labels)
        self.sessions, self.order, self.selected = {}, [], []
        self.touched = False           # operator picked datasets by hand
        self.live_key = None
        self.axis_mode, self.axis_auto = "raw", False     # raw | hamamatsu | time
        self.axis_filter, self.metric, self.mon_norm = "both", "qe", False

    # ------------------------------------------------------------ discovery
    def discover(self):
        """Group every FinalResult file into datasets."""
        reg = registry()
        by_date = {}
        for path in glob.glob(os.path.join(RESULT_DIR, "precal_result_kor_run_*.root")):
            m = RUN_RE.search(os.path.basename(path))
            if m:
                by_date.setdefault(m.group(1), {})[int(m.group(2))] = path
        sessions = {}
        for date, runs in by_date.items():
            prev, key = None, None
            for run in sorted(runs):
                r = reg.get((date, run))
                kind = KIND_SHORT.get(r["kind"], "MN") if r else ("GS" if run < 500 else "HV" if run < 600 else "DK" if run < 800 else "ST")
                if kind in ("GS", "DM"):
                    key = (date, kind, run // BLOCK_OF[kind])
                elif (key is None or prev is None or run != prev + 1 or key[1] != kind
                      or (r is not None and r.get("seq") == "1" and kind != "DK")):
                    key = (date, kind, run)
                s = sessions.setdefault(key, {"key": key, "date": date, "kind": kind, "runs": [],
                                              "paths": {}, "wl": None, "t0": None})
                s["runs"].append(run)
                s["paths"][run] = runs[run]
                if r is not None:
                    s["wl"] = s["wl"] or r.get("wavelength")
                    try:
                        t = datetime.fromisoformat(r["registered_at"]).timestamp()
                        s["t0"] = t if s["t0"] is None else min(s["t0"], t)
                    except (KeyError, ValueError):
                        pass
                prev = run
        sessions = self._split_by_wavelength(sessions)
        for s in sessions.values():
            if s["kind"] == "ST" and len(s["runs"]) == 1 and (s["date"], s["runs"][0]) not in reg:
                s["kind"] = "MN"
            if s["wl"] is None:
                p = next((self.points.get((s["date"], r)) for r in s["runs"] if (s["date"], r) in self.points), None)
                s["wl"] = p.get("wl") if p else self.wl_cache.get(s["paths"][s["runs"][0]])
            if s["t0"] is None:
                try:
                    s["t0"] = os.path.getmtime(s["paths"][s["runs"][0]])
                except OSError:
                    s["t0"] = 0
        hk_live = self._add_hk(sessions)
        live = self.live_state()
        self.live_key = None
        if live:
            kind, date, block = live
            cands = [s for s in sessions.values() if s["kind"] == kind and (date is None or s["date"] == date)
                     and (block is None or s["key"][2] == block // BLOCK_OF.get(kind, 100))]
            if cands:
                self.live_key = max(cands, key=lambda s: (s["date"], s["runs"][-1]))["key"]
        self.live_key = self.live_key or hk_live
        self.sessions = sessions
        self.order = sorted((k for k in sessions if sessions[k]["kind"] in MENU_KINDS),
                            key=lambda k: (sessions[k]["date"], sessions[k]["t0"]), reverse=True)

    @staticmethod
    def _pk(s, run):
        """Key of a point in self.points; HK runs have their own numbering, so their own namespace."""
        return (s.get("ns") or s["date"], run)

    def _add_hk(self, sessions):
        """HK Digitizer scans with at least one analysed point (or being taken now). -> key of the live one."""
        live = None
        try:
            for date in HK.dates()[:HK_DATES]:
                for h in HK.sessions(date, listing=date == HK.today()):
                    if not (h["live"] or any(HK.ready(date, n) for n in h["names"].values())):
                        continue
                    key = (date, "HK", h["runs"][0])
                    sessions[key] = {"key": key, "date": date, "ns": "HK" + date, "kind": "HK", "runs": h["runs"],
                                     "paths": h["names"], "wl": h["wl"], "t0": h["t0"]}
                    if h["live"]:
                        live = key
        except Exception:   # the HK PC being away must not stop the CAEN datasets
            pass
        return live

    def _poll_hk(self, s):
        """Queue the scan's unread points for the background reader and take over what it has read."""
        HK.refresh(s["date"])
        HK.request(s["date"], [s["paths"][r] for r in s["runs"]])
        got = False
        for run in s["runs"]:
            rec = HK.point(s["date"], s["paths"][run])
            if rec and self.points.get(self._pk(s, run), {}).get("mtime") != rec["mtime"]:
                self.points[self._pk(s, run)] = rec
                got = True
        return got

    def _split_by_wavelength(self, sessions):
        """Contiguous runs without registry rows can hold two stability runs back to
        back (e.g. 450 nm then 473 nm); split where the loaded points' wavelength changes."""
        out = {}
        for key, s in sessions.items():
            wls = [(r, self.points[(s["date"], r)]["wl"]) for r in s["runs"] if (s["date"], r) in self.points]
            if len({w for _, w in wls}) < 2:
                out[key] = s
                continue
            prev_wl, cur, wl_of = None, None, dict(wls)
            for r in s["runs"]:
                w = wl_of.get(r, prev_wl)
                if cur is None or w != prev_wl:
                    k2 = (s["date"], s["kind"], r)
                    cur = out[k2] = {"key": k2, "date": s["date"], "kind": s["kind"], "runs": [],
                                     "paths": {}, "wl": w, "t0": None}
                cur["runs"].append(r)
                cur["paths"][r] = s["paths"][r]
                prev_wl = w
        return out

    def first_wavelength(self, s):
        path = s["paths"][s["runs"][0]]
        if path not in self.wl_cache:
            try:
                st = disk_cache.stamp(path)
                wl = disk_cache.get("wavelength", path, st)
                if wl is None:
                    with uproot.open(path) as f:
                        wl = str(f["RunInfo"]["Wavelength"].array(library="np")[0])
                    disk_cache.put("wavelength", path, st, wl)
                self.wl_cache[path] = wl
            except Exception:
                self.wl_cache[path] = None
        return self.wl_cache[path]

    def label(self, key, long=False):
        s = self.sessions.get(key)
        if not s:
            return str(key)
        rng = f"{s['runs'][0]:03d}-{s['runs'][-1]:03d}" if len(s["runs"]) > 1 else f"{s['runs'][0]:03d}"
        txt = f"{s['date'][4:6]}-{s['date'][6:]}  {s['kind']} {s['wl'] or '?'}  {rng}"
        if key == self.live_key:
            txt += "  live"
        elif long:
            txt += f"  ({len(s['runs'])})"
        return txt

    def menu_keys(self):
        """(recent, more) session keys for the dataset menu; fills missing wavelengths first."""
        for key in self.order[:self.MENU_RECENT + self.MENU_MORE]:
            s = self.sessions[key]
            if not s["wl"] and uproot is not None:
                s["wl"] = self.first_wavelength(s)
        return self.order[:self.MENU_RECENT], self.order[self.MENU_RECENT:self.MENU_RECENT + self.MENU_MORE]

    # ------------------------------------------------------------ points
    @staticmethod
    def _point_time(date, run, path, reg):
        r = reg.get((date, run))
        if r:
            try:
                return datetime.fromisoformat(r["registered_at"]).timestamp()
            except (KeyError, ValueError):
                pass
        for d in RAW_DIRS:
            raw = os.path.join(d, f"precal_raw_kor_run_{date}_{run:03d}.root")
            if os.path.exists(raw):
                return os.path.getmtime(raw)
        return os.path.getmtime(path)

    def _read_point(self, date, run, path, reg):
        try:
            st = disk_cache.stamp(path)
        except OSError:
            return None
        mt = st[0]
        hit = _POINT_CACHE.get(path)
        if not (hit and hit[0] == mt):
            rec = disk_cache.get("points", path, st)   # read by an earlier session of the app
            if rec and "fit" not in rec and any(failed_metrics(rec, ch) for ch in (0, 1, 2)):
                rec = None   # cached before fit fallbacks were read
            if rec:
                hit = _POINT_CACHE[path] = (mt, rec)
        if hit and hit[0] == mt:
            return dict(hit[1], time=self._point_time(date, run, path, reg))
        rec = self._parse_point(date, run, path, reg)
        if rec:
            rec["mtime"] = mt
            _POINT_CACHE[path] = (mt, rec)
            disk_cache.put("points", path, st, rec)
        elif hit:   # mid-write (refit): keep the last good point, its old mtime makes callers retry
            return dict(hit[1], time=self._point_time(date, run, path, reg))
        return rec

    def _parse_point(self, date, run, path, reg):
        try:
            with uproot.open(path) as f:
                ri = f["RunInfo"].arrays(["SN1", "SN2", "SN3", "Direction2", "Direction3",
                                          "RawTiltAngle2", "RawRotateAngle2", "RawTiltAngle3", "RawRotateAngle3",
                                          "Wavelength", "Shifter"], library="np")

                def s(key):
                    v = ri[key][0]
                    return v.decode() if isinstance(v, bytes) else str(v)

                tilt2, rot2 = float(ri["RawTiltAngle2"][0]), float(ri["RawRotateAngle2"][0])
                tilt3, rot3 = float(ri["RawTiltAngle3"][0]), float(ri["RawRotateAngle3"][0])
                ham2, axis2 = angle_convert.get_hamamatsu_angle(s("Direction2"), tilt2, rot2)
                ham3, axis3 = angle_convert.get_hamamatsu_angle(s("Direction3"), tilt3, rot3)
                data = {mk: {} for mk in METRICS}
                fit = {}
                for ch in (0, 1, 2):
                    try:
                        t = f[f"tree_ch{ch}"]
                    except Exception:
                        continue
                    for mk, (_l, branch, errbranch, xform) in METRICS.items():
                        try:
                            v = float(t[branch].array(library="np")[0])
                            e = float(t[errbranch].array(library="np")[0]) if errbranch else 0.0
                            if xform:
                                v, e = xform(v), (xform(e) if errbranch else 0.0)
                            data[mk][ch] = (v, e)
                        except Exception:
                            continue
                    bad = {mk for mk in FIT_METRICS if data[mk].get(ch, (1,))[0] <= 0}
                    for mk, ve in (fit_fallback(f, ch, bad) if bad else {}).items():
                        fit.setdefault(mk, {})[ch] = ve
        except Exception:
            return None
        return {"run": run, "date": date, "path": path, "mtime": os.path.getmtime(path),
                "time": self._point_time(date, run, path, reg), "wl": s("Wavelength"), "shifter": s("Shifter"),
                # the monitor never moves: plot it against device 2's stage angle
                "raw": {0: tilt2, 1: tilt2, 2: tilt3}, "ham": {0: ham2, 1: ham2, 2: ham3},
                "axis": {0: axis2, 1: axis2, 2: axis3}, "sn": {0: s("SN1"), 1: s("SN2"), 2: s("SN3")},
                "data": data, "fit": fit}

    def poll(self):
        """Rediscover datasets and read new/changed points of the selected ones.
        Returns True when the plot or the menu should be refreshed."""
        before = (list(self.order[:1]), self.live_key)
        self.discover()
        if not self.order:
            return False
        if not self.touched:
            want = [self.live_key or self.order[0]]
            if want != self.selected:
                self.selected = want
                self.auto_axis()
        else:
            fixed = []
            for k in self.selected:
                if k not in self.sessions:     # split by wavelength since it was picked
                    k = next((n for n, s in self.sessions.items()
                              if s["date"] == k[0] and s["kind"] == k[1] and k[2] in s["runs"]), None)
                if k and k not in fixed:
                    fixed.append(k)
            self.selected = fixed
            if self.live_key and self.live_key not in self.selected and before[1] != self.live_key:
                self.selected.append(self.live_key)
        reg, got = registry(), False
        for key in list(self.selected):   # the GUI may change the selection meanwhile
            s = self.sessions.get(key)
            if s is None:
                continue
            if s["kind"] == "HK":
                got = self._poll_hk(s) or got
                continue
            for run in s["runs"]:
                path = s["paths"][run]
                try:
                    mt = os.path.getmtime(path)
                except OSError:
                    continue
                if self.scanned.get(path) == mt:
                    continue
                rec = self._read_point(s["date"], run, path, reg)
                if rec:          # a file mid-write fails to parse; retried next poll
                    self.scanned[path] = rec["mtime"]
                    self.points[(s["date"], run)] = rec
                    got = True
                    s["wl"] = s["wl"] or rec["wl"]
        if got:
            disk_cache.flush()
        return got or before != (list(self.order[:1]), self.live_key)

    # ------------------------------------------------------------ selection
    def auto_axis(self):
        """Stability / manual datasets read best against time; switch back when they go."""
        kinds = {self.sessions[k]["kind"] for k in self.selected if k in self.sessions}
        if kinds and kinds <= {"ST", "MN"}:
            if self.axis_mode != "time":
                self.axis_mode, self.axis_auto = "time", True
        elif self.axis_auto:
            self.axis_mode, self.axis_auto = "raw", False

    def toggle(self, key, on):
        self.touched = True
        if on and key not in self.selected:
            self.selected.append(key)
        elif not on and key in self.selected:
            self.selected.remove(key)
        self.auto_axis()

    def backend(self):
        """"hk" / "caen" when every selected dataset is of that digitizer, else "mixed"."""
        kinds = {self.sessions[k]["kind"] == "HK" for k in self.selected if k in self.sessions}
        return "caen" if kinds <= {False} else "hk" if kinds == {True} else "mixed"

    def follow_newest(self):
        self.touched = False
        self.selected = []

    def status(self):
        """(text, is_live) for the toolbar."""
        if not self.selected or self.selected[0] not in self.sessions:
            return None, False
        key = self.selected[0]
        s = self.sessions[key]
        pts = [self.points[self._pk(s, r)] for r in s["runs"] if self._pk(s, r) in self.points]
        txt = f"{len(pts)} points"
        if s["kind"] == "HK":
            trees = sum(HK.ready(s["date"], n) for n in s["paths"].values())
            txt = f"{len(pts)} of {len(s['runs'])} points" + (f"  ·  {len(s['runs']) - trees} without trees"
                                                              if trees < len(s["runs"]) else "")
            if HK.pending():
                txt += f"  ·  reading {HK.pending()}"
            elif len(pts) < trees and HK.acquiring():
                txt += "  ·  reading waits until the HK DAQ is idle"
        if pts:
            t = max(p["time"] for p in pts)
            txt += f"  ·  newest {time.strftime('%m-%d %H:%M', time.localtime(t))} ({age_text(t)})"
        if len(self.selected) > 1:
            txt += f"  ·  {len(self.selected)} datasets"
        return txt, key == self.live_key

    # ------------------------------------------------------------ series
    def series(self, key, ch, failed=False):
        """(x, y, yerr, records) for one dataset and channel under the current settings.
        failed=True: only the points whose fit failed, valued from the fit stored with the histogram."""
        s = self.sessions.get(key)
        if not s:
            return [], [], [], []
        do_norm = self.mon_norm and ch != 0
        recs = sorted((self.points[self._pk(s, r)] for r in s["runs"] if self._pk(s, r) in self.points),
                      key=lambda p: p["time"])
        t0 = recs[0]["time"] if recs else 0
        xs, ys, es, keep = [], [], [], []
        for p in recs:
            pair = p["data"].get(self.metric, {}).get(ch)
            if pair is None or (self.axis_filter != "both" and p["axis"].get(ch) != self.axis_filter):
                continue
            val, err = pair
            if failed:
                pair = p.get("fit", {}).get(self.metric, {}).get(ch) if val <= 0 else None
                if pair is None:
                    continue
                val, err = pair
            elif self.metric != "qe" and val <= 0:   # failed fit (stored as -1)
                continue
            if do_norm:
                mon = p["data"].get(self.metric, {}).get(0)
                if failed and mon is not None and mon[0] <= 0:
                    mon = p.get("fit", {}).get(self.metric, {}).get(0)
                if mon is None or mon[0] == 0 or (failed and mon[0] < 0):
                    continue
                ratio = val / mon[0]
                rel = ((err / val) ** 2 + (mon[1] / mon[0]) ** 2) ** 0.5 if val else 0.0
                val, err = ratio, ratio * rel
            xs.append((p["time"] - t0) / 3600.0 if self.axis_mode == "time" else
                      p["raw"][ch] if self.axis_mode == "raw" else p["ham"][ch])
            ys.append(val)
            es.append(err)
            keep.append(p)
        return xs, ys, es, keep

    def matching(self, other_key, ch, p):
        """The point of another dataset at the same axis and stage angle as p, or None."""
        s = self.sessions.get(other_key)
        if not s:
            return None
        return next((q for (d, r), q in self.points.items() if d == (s.get("ns") or s["date"]) and r in s["runs"]
                     and q["axis"].get(ch) == p["axis"].get(ch) and q["raw"][ch] == p["raw"][ch]), None)

"""Live scan data: QE / Gain / TTS / Charge Resolution per point for the running
scan and for any earlier dataset, built from the FinalResult files and labelled
from Data/run_registry.csv. No UI (same rules as the Tk app's live_scan_view.py).

Values are RAW per-point results (no Monitor normalisation unless asked, no dark
subtraction); ./analyze.sh uniformity is the final analysis.
"""
import csv
import glob
import os
import re
import time
from datetime import datetime

import angle_convert   # pure helper of the Tk app (no Tk import); checked against angle_convert.h
from core import env

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
KIND_SHORT = {"general": "GS", "stability": "ST", "manual": "MN", "dummy": "DM"}
METRICS = {
    # key -> (label, tree branch, error branch or None, transform)
    "qe":    ("Relative QE (%)", "relativeQE_raw", "relativeQE_raw_err", None),
    "gain":  ("Gain / SPE charge [pC]", "spe_mean", "spe_mean_error", None),
    "tts":   ("TTS [ns]", "rms_exG", None, lambda v: v * 2.0),
    "chres": ("Charge Resolution [%]", "charge_resolution", "charge_resolution_err", None),
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
                kind = KIND_SHORT.get(r["kind"], "MN") if r else ("GS" if run < 700 else "DK" if run < 800 else "ST")
                if kind in ("GS", "DM"):
                    key = (date, kind, run // 100)
                elif (key is None or prev is None or run != prev + 1 or key[1] != kind
                      or (r is not None and r.get("seq") == "1")):
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
        live = self.live_state()
        self.live_key = None
        if live:
            kind, date, block = live
            cands = [s for s in sessions.values() if s["kind"] == kind and (date is None or s["date"] == date)
                     and (block is None or s["key"][2] == block // 100)]
            if cands:
                self.live_key = max(cands, key=lambda s: (s["date"], s["runs"][-1]))["key"]
        self.sessions = sessions
        self.order = sorted(sessions, key=lambda k: (sessions[k]["date"], sessions[k]["t0"]), reverse=True)

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
                with uproot.open(path) as f:
                    self.wl_cache[path] = str(f["RunInfo"]["Wavelength"].array(library="np")[0])
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
        except Exception:
            return None
        return {"run": run, "date": date, "path": path, "mtime": os.path.getmtime(path),
                "time": self._point_time(date, run, path, reg), "wl": s("Wavelength"), "shifter": s("Shifter"),
                # the monitor never moves: plot it against device 2's stage angle
                "raw": {0: tilt2, 1: tilt2, 2: tilt3}, "ham": {0: ham2, 1: ham2, 2: ham3},
                "axis": {0: axis2, 1: axis2, 2: axis3}, "sn": {0: s("SN1"), 1: s("SN2"), 2: s("SN3")},
                "data": data}

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
                    self.scanned[path] = mt
                    self.points[(s["date"], run)] = rec
                    got = True
                    s["wl"] = s["wl"] or rec["wl"]
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

    def follow_newest(self):
        self.touched = False
        self.selected = []

    def status(self):
        """(text, is_live) for the toolbar."""
        if not self.selected or self.selected[0] not in self.sessions:
            return None, False
        key = self.selected[0]
        s = self.sessions[key]
        pts = [self.points[(s["date"], r)] for r in s["runs"] if (s["date"], r) in self.points]
        txt = f"{len(pts)} points"
        if pts:
            t = max(p["time"] for p in pts)
            txt += f"  ·  newest {time.strftime('%m-%d %H:%M', time.localtime(t))} ({age_text(t)})"
        if len(self.selected) > 1:
            txt += f"  ·  {len(self.selected)} datasets"
        return txt, key == self.live_key

    # ------------------------------------------------------------ series
    def series(self, key, ch):
        """(x, y, yerr, records) for one dataset and channel under the current settings."""
        s = self.sessions.get(key)
        if not s:
            return [], [], [], []
        do_norm = self.mon_norm and ch != 0
        recs = sorted((self.points[(s["date"], r)] for r in s["runs"] if (s["date"], r) in self.points),
                      key=lambda p: p["time"])
        t0 = recs[0]["time"] if recs else 0
        xs, ys, es, keep = [], [], [], []
        for p in recs:
            pair = p["data"].get(self.metric, {}).get(ch)
            if pair is None or (self.axis_filter != "both" and p["axis"].get(ch) != self.axis_filter):
                continue
            val, err = pair
            if do_norm:
                mon = p["data"].get(self.metric, {}).get(0)
                if mon is None or mon[0] == 0:
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
        return next((q for (d, r), q in self.points.items() if d == s["date"] and r in s["runs"]
                     and q["axis"].get(ch) == p["axis"].get(ch) and q["raw"][ch] == p["raw"][ch]), None)

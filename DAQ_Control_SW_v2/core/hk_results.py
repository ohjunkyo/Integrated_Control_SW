"""HK Digitizer results: pedestal, charge, timing and ToT histograms of each scan point, read from the hit / event
trees on the HK DAQ PC over SSH (the reader script goes through stdin; nothing is written on that PC) and cached
under Data/HKDigitizer. Definitions follow the HK Live Monitor there: pedestal = hgain of type 1 hits, charge =
hgain of type 0 hits, time = (hit tdc - trigger tdc) * 0.25 ns. Qt-free."""
import json
import os
import re
import shlex
import subprocess
import threading
import time
from datetime import datetime

import numpy as np

from core import config, env, hk
import angle_convert   # the Tk app's helper; core.env puts its folder on the path

CACHE_DIR = os.path.join(env.ADC_DIR, "Data", "HKDigitizer")
NAME_RE = re.compile(r"^(Run0*(\d+)_.*_T(-?\d+(?:\.\d+)?)_1R(-?\d+)_2R(-?\d+).*?)(\.bin|_hittree\.root|_eventtree\.root)$")
SIG_RE = re.compile(r"^Run\d+|_T-?\d+(?:\.\d+)?_1R-?\d+_2R-?\d+")
WL_RE = re.compile(r"(\d{3})nm")
LIST_S, LIST_OLD_S, RETRY_S = 30, 600, 300   # relist today / an older date / after a failed listing
SCAN_GAP_S = 600           # a longer pause between points starts another scan
LIVE_S = 180               # a scan whose newest point is this recent is being taken now
TREE_SETTLE_S = 15         # an event tree younger than this may still be written
FETCH_JOBS = 4             # points read at the same time on the HK PC
Q_LO, Q_N = 200, 300       # hgain histograms: 1 DAC bins from 200
T_BIN, T_N = 0.25, 2000    # time histogram: 0.25 ns bins from 0
TOT_BIN, TOT_N = 2, 250
WIN_NS = (-10.0, 20.0)     # in-time window around the time peak
SIDE_NS = (-130.0, -30.0)  # accidental hits are counted here, before the peak
METRICS = {"qe": "In-time hits per trigger [%]", "gain": "Charge mean - pedestal [DAC]",
           "tts": "Timing FWHM [ns]", "fwhm": "Timing FWHM [ns]"}

REMOTE_PY = r'''
import sys, json, ROOT
ROOT.gROOT.SetBatch(1)
ROOT.gErrorIgnoreLevel = ROOT.kError
def rows(h, ny):
    return [[int(h.GetBinContent(i + 1, j + 1)) for i in range(h.GetNbinsX())] for j in range(ny)]
def one(hit):
    evt = hit.replace("_hittree.root", "_eventtree.root")
    out = {"name": hit.replace("_hittree.root", "")}
    f = ROOT.TFile.Open(hit); t = f.Get("hit")
    t.Draw("channel:hgain>>hp(%Q_N%,%Q_LO%,%Q_HI%,3,0,3)", "channel<3&&type==1", "goff")
    out["ped"] = rows(ROOT.gDirectory.Get("hp"), 3)
    f.Close()
    g = ROOT.TFile.Open(evt); e = g.Get("event")
    out["events"] = int(e.GetEntries())
    cut = "NormalHits.type==0&&NormalHits.channel<3"
    e.Draw("NormalHits.channel:NormalHits.hgain>>hq(%Q_N%,%Q_LO%,%Q_HI%,3,0,3)", cut, "goff")
    out["q"] = rows(ROOT.gDirectory.Get("hq"), 3)
    e.Draw("NormalHits.hgain:NormalHits.channel>>pq(3,0,3)", cut, "prof goff")
    p = ROOT.gDirectory.Get("pq")
    out["q_mean"] = [p.GetBinContent(i + 1) for i in range(3)]
    out["q_n"] = [int(p.GetBinEntries(i + 1)) for i in range(3)]
    e.Draw("NormalHits.channel:(NormalHits.tdc-TriggerHits[0].tdc)*0.25>>ht(%T_N%,0,%T_HI%,3,0,3)", cut, "goff")
    out["dt"] = rows(ROOT.gDirectory.Get("ht"), 3)
    e.Draw("NormalHits.channel:NormalHits.tot>>ho(%TOT_N%,0,%TOT_HI%,3,0,3)", cut, "goff")
    out["tot"] = rows(ROOT.gDirectory.Get("ho"), 3)
    g.Close()
    return out
for hit in sys.argv[1:]:
    try:
        print(json.dumps(one(hit)), flush=True)
    except Exception as x:
        print(json.dumps({"name": hit.replace("_hittree.root", ""), "error": str(x)}), flush=True)
'''.replace("%Q_N%", str(Q_N)).replace("%Q_LO%", str(Q_LO)).replace("%Q_HI%", str(Q_LO + Q_N)) \
   .replace("%T_N%", str(T_N)).replace("%T_HI%", str(T_N * T_BIN)) \
   .replace("%TOT_N%", str(TOT_N)).replace("%TOT_HI%", str(TOT_N * TOT_BIN))

Q_X = Q_LO + np.arange(Q_N) + 0.5
T_X = (np.arange(T_N) + 0.5) * T_BIN
TOT_X = (np.arange(TOT_N) + 0.5) * TOT_BIN

_lock = threading.Lock()
_index = {}        # date -> {"listed": epoch, "ok": bool, "files": {name: {"run","t","size","hit","evt"}}}
_points = {}       # (date, name) -> (cache file mtime, record)
_queue = []        # [(date, name)] waiting for the reader
_failed = {}       # (date, name) -> time of the last failed read
_worker = None
_daq = {"t": 0.0, "n": 0}   # HK DAQ processes seen by the last listing of today's folder
last_error = ""


def _dir(date):
    return os.path.join(CACHE_DIR, date)


def _work_dir(date):
    return (hk.config().get("work_dir") or "").replace("{date}", date)


def today():
    return datetime.now().strftime("%Y%m%d")


def dates():
    """Dates with cached HK listings, plus today; newest first."""
    try:
        got = {d for d in os.listdir(CACHE_DIR) if re.fullmatch(r"\d{8}", d)}
    except OSError:
        got = set()
    return sorted(got | {today()}, reverse=True)


# ---------------------------------------------------------------- listing
def _load_index(date):
    if date not in _index:
        try:
            with open(os.path.join(_dir(date), "index.json"), encoding="utf-8") as f:
                _index[date] = json.load(f)
        except (OSError, ValueError):
            _index[date] = {"listed": 0, "ok": True, "files": {}}
    return _index[date]


def refresh(date, force=False):
    """List the date's HK data folder (ls only) when the cached listing is old. -> {name: entry}."""
    global last_error
    idx = _load_index(date)
    age = time.time() - idx.get("tried", idx.get("listed", 0))
    limit = RETRY_S if not idx.get("ok", True) else LIST_S if date == today() else LIST_OLD_S
    if not force and age < limit:
        return idx["files"]
    idx["tried"] = time.time()
    wd = _work_dir(date)
    if not wd or "{date}" in wd:
        return idx["files"]
    rc, out = hk.ssh(hk.config(), f"{{ cd {shlex.quote(wd)} 2>/dev/null && ls -l --time-style=+%s; }}; "
                     "echo \"##DAQ $(pgrep -fc '[R]unManager.py|[S]canManager.py')\"", wait=True, timeout=20)
    daq = re.search(r"^##DAQ (\d+)", out, re.M)
    if rc != 0 or not daq:
        idx["ok"] = False
        last_error = (out.strip().splitlines() or ["listing failed"])[-1][:200]
        return idx["files"]
    if date == today():
        _daq.update(t=time.time(), n=int(daq.group(1)))
    files = {}
    for line in out.splitlines():
        parts = line.split(None, 6)
        m = NAME_RE.match(parts[6]) if len(parts) == 7 else None
        if not m:
            continue
        e = files.setdefault(m.group(1), {"run": int(m.group(2)), "t": 0, "size": 0, "hit": 0, "evt": 0})
        try:
            size, mt = int(parts[4]), int(parts[5])
        except ValueError:
            continue
        if m.group(6) == ".bin":
            e["t"], e["size"] = mt, size
        else:
            e["hit" if m.group(6).startswith("_hit") else "evt"] = mt if size > 0 else 0
    files = {n: e for n, e in files.items() if e["t"]}
    last_error = ""
    idx.update(listed=time.time(), ok=True, files=files)
    if files:
        os.makedirs(_dir(date), exist_ok=True)
        tmp = os.path.join(_dir(date), "index.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"listed": idx["listed"], "ok": True, "files": files}, f)
        os.replace(tmp, os.path.join(_dir(date), "index.json"))
    return files


def sessions(date, listing=True):
    """Scans of one date: consecutive runs of the same run-ID pattern without a long pause.
    -> [{"date", "runs", "names": {run: name}, "t0", "t1", "wl", "live"}]"""
    files = refresh(date) if listing else _load_index(date)["files"]
    out, cur, prev = [], None, None
    for name, e in sorted(files.items(), key=lambda kv: (kv[1]["t"], kv[1]["run"])):
        sig = SIG_RE.sub("", name)
        if cur is None or sig != cur["sig"] or e["t"] - prev["t"] > SCAN_GAP_S or e["run"] in cur["names"]:
            wl = WL_RE.search(name)
            cur = {"date": date, "runs": [], "names": {}, "sig": sig, "t0": e["t"], "t1": e["t"],
                   "wl": wl.group(1) if wl else None, "live": False}
            out.append(cur)
        cur["runs"].append(e["run"])
        cur["names"][e["run"]] = name
        cur["t1"] = e["t"]
        prev = e
    for s in out:
        s["runs"].sort()
        s["live"] = time.time() - s["t1"] < LIVE_S
    return out


def acquiring():
    """The HK DAQ is taking data (its processes run, or today's newest BIN is recent). Reading trees on that PC
    at the same time costs events, so the reader waits."""
    files = refresh(today())
    if time.time() - _daq["t"] < 2 * LIST_S and _daq["n"] > 0:
        return True
    return bool(files) and time.time() - max(e["t"] for e in files.values()) < LIVE_S


def ready(date, name):
    """Both trees of this point exist and the event tree is no longer being written."""
    e = _load_index(date)["files"].get(name)
    return bool(e and e["hit"] and e["evt"] and time.time() - e["evt"] > TREE_SETTLE_S)


# ---------------------------------------------------------------- reading
def _cache_path(date, name):
    return os.path.join(_dir(date), name + ".json")


def _pmt_setup():
    cfg = config.parse(env.config_file())
    return {"sn": [cfg.get("SN1", "ch0"), cfg.get("SN2", "ch1"), cfg.get("SN3", "ch2")],
            "dir": [cfg.get("direction2", "B"), cfg.get("direction3", "B")]}


def request(date, names):
    """Queue points for the background reader; points without trees, cached or just failed are skipped."""
    global _worker
    if acquiring():
        return
    idx = _load_index(date)["files"]
    with _lock:
        for n in names:
            key = (date, n)
            if key in _queue or time.time() - _failed.get(key, 0) < RETRY_S or not ready(date, n):
                continue
            p = _cache_path(date, n)
            if os.path.exists(p) and _points.get(key, (None, {}))[1].get("evt_mtime", idx[n]["evt"]) == idx[n]["evt"]:
                continue
            _queue.append(key)
        if _queue and (_worker is None or not _worker.is_alive()):
            _worker = threading.Thread(target=_work, daemon=True)
            _worker.start()


def pending():
    with _lock:
        return len(_queue)


def _work():
    while True:
        busy = acquiring()
        with _lock:
            if busy:   # asked again by the pages once the DAQ is idle
                del _queue[:]
            if not _queue:
                return
            date = _queue[0][0]
            batch = [k for k in _queue if k[0] == date][:FETCH_JOBS * 3]
        try:
            _fetch(date, [n for _d, n in batch])
        except Exception as e:   # the reader must not die with points still queued
            globals()["last_error"] = str(e)[:200]
        with _lock:
            for k in batch:
                if k in _queue:
                    _queue.remove(k)
                if not os.path.exists(_cache_path(*k)):
                    _failed[k] = time.time()


def _fetch(date, names):
    global last_error
    cfg = hk.config()
    setup = (cfg.get("setup_cmd") or "true").strip()
    files = " ".join(shlex.quote(n + "_hittree.root") for n in names)
    remote = (f'S=$(cat); {{ {setup}; }} >/dev/null 2>&1; cd {shlex.quote(_work_dir(date))} && '
              f'printf "%s\\n" {files} | xargs -P {FETCH_JOBS} -n 1 python -c "$S" 2>/dev/null')
    p = subprocess.Popen(hk.ssh_argv(cfg, remote), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True)
    killer = threading.Timer(120 + 40 * len(names), p.kill)
    killer.start()
    try:
        p.stdin.write(REMOTE_PY)
        p.stdin.close()
        setup_now, idx = _pmt_setup(), _load_index(date)["files"]
        os.makedirs(_dir(date), exist_ok=True)
        for line in p.stdout:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            name = d.get("name")
            if name not in names:
                continue
            if "error" in d:
                last_error = f"{name[:12]}: {d['error'][:160]}"
                continue
            old = _read_json(_cache_path(date, name)) or {}
            d.update(sn=old.get("sn") or setup_now["sn"], dir=old.get("dir") or setup_now["dir"],
                     evt_mtime=idx.get(name, {}).get("evt", 0), read_at=time.time())
            tmp = _cache_path(date, name) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f)
            os.replace(tmp, _cache_path(date, name))
            try:
                save_image(date, name)
            except Exception as e:   # a missing picture must not lose the point
                last_error = f"image: {e}"[:200]
        p.wait()
    finally:
        killer.cancel()


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def image_dir(date):
    """Folder of the saved point pictures: <ImagePath>/HK/<date>, listed by DAQ System > Images."""
    base = config.parse(env.config_file()).get("ImagePath") or os.path.join(env.ADC_DIR, "Data", "image")
    return os.path.join(base, "HK", date)


def save_image(date, name):
    """Write the histogram picture of one read point; -> its path, or None when the point is not cached."""
    rec = point(date, name)
    if not rec:
        return None
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from core import hk_plot
    fig = Figure(figsize=(15, 10), facecolor="white", layout="constrained")
    FigureCanvasAgg(fig)
    hk_plot.draw(fig, rec)
    os.makedirs(image_dir(date), exist_ok=True)
    path = os.path.join(image_dir(date), f"hk_{date}_run{rec['run']:03d}_{rec['axis'].get(1, 'N')}_tilt{rec['raw'][1]:+03.0f}.png")
    fig.savefig(path, dpi=100)
    return path


# ---------------------------------------------------------------- numbers
def _mean_std(x, y):
    n = y.sum()
    if n <= 0:
        return 0.0, 0.0, 0
    m = float((x * y).sum() / n)
    return m, float(np.sqrt((y * (x - m) ** 2).sum() / n)), int(n)


def _fwhm(x, y):
    """Width at half of the highest bin, with the crossings interpolated between bins."""
    i = int(y.argmax())
    half = y[i] / 2.0
    if half <= 0:
        return 0.0
    lo = i
    while lo > 0 and y[lo] >= half:
        lo -= 1
    hi = i
    while hi < len(y) - 1 and y[hi] >= half:
        hi += 1
    left = x[lo] + (half - y[lo]) / (y[lo + 1] - y[lo]) * (x[lo + 1] - x[lo]) if y[lo + 1] != y[lo] else x[lo]
    right = x[hi] - (half - y[hi]) / (y[hi - 1] - y[hi]) * (x[hi] - x[hi - 1]) if y[hi - 1] != y[hi] else x[hi]
    return float(right - left)


def channel_stats(d, ch):
    """Numbers of one PMT of one point, from its cached histograms."""
    ped, q, dt = np.array(d["ped"][ch], float), np.array(d["q"][ch], float), np.array(d["dt"][ch], float)
    pm, ps, pn = _mean_std(Q_X, ped)
    _qm, qs, _qn = _mean_std(Q_X, q)
    qm, qn = float(d["q_mean"][ch]), int(d["q_n"][ch])
    out = {"ped": pm, "ped_std": ps, "ped_n": pn, "q": qm, "q_n": qn, "events": int(d["events"])}
    if pn and qn:
        out["dq"] = qm - pm
        out["dq_err"] = float(np.sqrt(qs ** 2 / qn + ps ** 2 / pn))
    if dt.sum() > 0 and d["events"] > 0:
        peak = float(T_X[int(dt.argmax())])
        win = (T_X > peak + WIN_NS[0]) & (T_X < peak + WIN_NS[1])
        side = (T_X > peak + SIDE_NS[0]) & (T_X < peak + SIDE_NS[1])
        scale = win.sum() / side.sum() if side.sum() else 0.0
        bkg = dt[side].sum() * scale
        out.update(peak=peak, fwhm=_fwhm(T_X, dt), intime=float(dt[win].sum() - bkg),
                   occ=float((dt[win].sum() - bkg) / d["events"] * 100.0),
                   occ_err=float(np.sqrt(dt[win].sum() + dt[side].sum() * scale ** 2) / d["events"] * 100.0))
    return out


def point(date, name):
    """Live Scan style record of one cached point (None until it has been read): angles, PMT names, the metric
    values under the Live Scan keys and the histograms."""
    path = _cache_path(date, name)
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return None
    hit = _points.get((date, name))
    if hit and hit[0] == mt:
        return hit[1]
    d = _read_json(path)
    m = NAME_RE.match(name + ".bin")
    if not d or not m or "dt" not in d:
        return None
    tilt, r1, r2 = float(m.group(3)), float(m.group(4)), float(m.group(5))
    dirs = d.get("dir") or ["B", "B"]
    ham2, axis2 = angle_convert.get_hamamatsu_angle(dirs[0], tilt, r1)
    ham3, axis3 = angle_convert.get_hamamatsu_angle(dirs[1], tilt, r2)
    stats = {ch: channel_stats(d, ch) for ch in (0, 1, 2)}
    data = {k: {} for k in METRICS}
    for ch, s in stats.items():
        if "occ" in s:
            data["qe"][ch] = (s["occ"], s["occ_err"])
            data["tts"][ch] = data["fwhm"][ch] = (s["fwhm"], 0.0)
        if "dq" in s:
            data["gain"][ch] = (s["dq"], s["dq_err"])
    wl = WL_RE.search(name)
    e = _load_index(date)["files"].get(name, {})
    sn = d.get("sn") or ["ch0", "ch1", "ch2"]
    rec = {"hk": True, "run": int(m.group(2)), "date": date, "name": name, "path": path, "mtime": mt,
           "evt_mtime": d.get("evt_mtime", 0), "time": e.get("t") or d.get("read_at", mt),
           "wl": wl.group(1) if wl else "", "shifter": "",
           "raw": {0: tilt, 1: tilt, 2: tilt}, "ham": {0: ham2, 1: ham2, 2: ham3},
           "axis": {0: axis2, 1: axis2, 2: axis3}, "sn": {0: sn[0], 1: sn[1], 2: sn[2]},
           "data": data, "stats": stats, "events": int(d["events"]),
           "hist": {"ped": d["ped"], "q": d["q"], "dt": d["dt"], "tot": d["tot"]}}
    _points[(date, name)] = (mt, rec)
    return rec

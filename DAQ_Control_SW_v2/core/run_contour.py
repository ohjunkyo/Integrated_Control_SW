"""Waveform contour of one run (counts per sample and voltage), built from its RAW file by a low-priority
child process and kept as an .npz cache. Qt-free; reads only."""
import os
import subprocess
import sys

import numpy as np

from core import env
from core.live_scan import RAW_DIRS

LOCAL_CACHE = os.path.expanduser("~/.cache/precal/contour")
HDD_ROOT = os.path.expanduser("~/external_HDD_1_4T/Data_Backup")
# on the external disk next to the RAW backup; local only while that disk is not mounted
CACHE_DIR = os.path.join(HDD_ROOT, "Contour_cache") if os.path.isdir(HDD_ROOT) else LOCAL_CACHE
BELOW, ABOVE = 820, 82         # ADC counts kept below / above the pedestal (about 100 mV / 10 mV)
VIEW_MV = (10.0, 3.0)          # shown below / above the pedestal at first
BATCH = 2000


def raw_path(date, run):
    for d in RAW_DIRS:
        p = os.path.join(d, f"precal_raw_kor_run_{date}_{int(run):03d}.root")
        if os.path.exists(p):
            return p
    return None


def cache_path(date, run):
    return os.path.join(CACHE_DIR, f"contour_{date}_{int(run):03d}.npz")


def build(date, run):
    """Fill the cache file of this run (called in the child process)."""
    from core import waveform as W
    raw = raw_path(date, run)
    if raw is None:
        raise FileNotFoundError(f"No RAW file for run {date}_{int(run):03d}")
    rf = W.open_run(raw)
    n, height = rf.n_samples, BELOW + ABOVE
    chans = [c for c in rf.channels if c != W.TRIGGER_CH]
    out, lo, cols = {}, {}, np.arange(n) * height
    for _b, block in rf.blocks(0, rf.n_entries, BATCH):
        for ch in chans:
            a = block[:, ch, :].astype(np.int64)
            if ch not in lo:   # pedestal = most frequent count of the first block
                lo[ch] = int(np.bincount(a.ravel()).argmax()) - BELOW
                out[ch] = np.zeros(n * height, np.int64)
            k = a - lo[ch]
            ok = (k >= 0) & (k < height)
            out[ch] += np.bincount((k + cols)[ok], minlength=n * height)
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = cache_path(date, run) + ".tmp.npz"
    np.savez_compressed(tmp, chans=np.array(chans), lo=np.array([lo[c] for c in chans]), n_samples=n,
                        raw_mtime=os.path.getmtime(raw),
                        **{f"h{c}": out[c].reshape(n, height).T.astype(np.uint32) for c in chans})
    os.replace(tmp, cache_path(date, run))


def load(date, run):
    """{ch: {"counts": (voltage, sample) array, "extent": (x0, x1, y0, y1) [sample, mV], "ped_mV"}}; builds the cache when needed."""
    raw, cp = raw_path(date, run), cache_path(date, run)
    fresh = os.path.exists(cp) and (raw is None or float(np.load(cp)["raw_mtime"]) == os.path.getmtime(raw))
    if not fresh:
        if raw is None:
            raise FileNotFoundError(f"No RAW file for run {date}_{int(run):03d}")
        r = subprocess.run(["nice", "-n19", "ionice", "-c3", sys.executable, "-m", "core.run_contour", str(date), str(run)],
                           cwd=env.APP_DIR, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError((r.stderr.strip().splitlines() or ["Contour build failed"])[-1])
    from core.waveform import ADC_TO_MV
    out = {}
    with np.load(cp) as z:
        n = int(z["n_samples"])
        for ch, lo in zip(z["chans"], z["lo"]):
            out[int(ch)] = {"counts": z[f"h{int(ch)}"],
                            "extent": (0, n, (int(lo) - 0.5) * ADC_TO_MV, (int(lo) + BELOW + ABOVE - 0.5) * ADC_TO_MV),
                            "ped_mV": (int(lo) + BELOW) * ADC_TO_MV}
    return out


if __name__ == "__main__":
    build(sys.argv[1], int(sys.argv[2]))

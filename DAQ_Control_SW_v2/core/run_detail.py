"""Charge and time distributions of one analysed run, read from its FinalResult file. Qt-free; reads only."""
import math
import os

import numpy as np

from core import env   # noqa: F401  (sets the import path live_scan needs)
from core.live_scan import RESULT_DIR, uproot

NS_PER_SAMPLE = 2.0
ROLE = {0: "Monitor", 1: "Rot1", 2: "Rot2"}
PE_MAX = 10           # most p.e. terms in the model of read_ntp_v7.C
PE_MIN_ENTRIES = 1.0  # 2+ p.e. terms are drawn while they reach one entry per bin
_erf = np.vectorize(math.erf)


def spe_components(params, x):
    """[(label, n, kind, y)] of the SPE model of read_ntp_v7.C: pedestal (n 0), the 1 p.e. Gaussian, backscatter and
    their sum (kind "gaus" / "back" / "sum"), then one sum per higher p.e. Empty when the fit is not that model or failed."""
    if not params or len(params) != 7:
        return []
    amp, mu, ped, gain, ped_s, spe_s, w = params
    if not (gain > 0 and ped_s > 0 and spe_s > 0 and mu > 0):
        return []
    x = np.asarray(x, float)

    def gaus(m, s):
        return np.exp(-0.5 * ((x - m) / s) ** 2) / (s * math.sqrt(2 * math.pi))

    def pois(n):
        return math.exp(-mu) * mu ** n / math.factorial(n)

    out = [("Pedestal", 0, "sum", amp * pois(0) * gaus(ped, ped_s))]
    for n in range(1, PE_MAX + 1):
        m, s = ped + n * gain, math.sqrt(ped_s ** 2 + n * spe_s ** 2)
        g = amp * pois(n) * (1.0 - w) * gaus(m, s)
        b = amp * pois(n) * w * (_erf((x - ped) / ped_s) - _erf((x - m) / s)) / (2.0 * n * gain)
        if n > 1 and (g + b).max() < PE_MIN_ENTRIES:
            break
        if n == 1:
            out += [("1 p.e. Gaussian", n, "gaus", g), ("1 p.e. backscatter", n, "back", b)]
        out.append((f"{n} p.e.", n, "sum", g + b))
    return out


def path(date, run):
    return os.path.join(RESULT_DIR, f"precal_result_kor_run_{date}_{int(run):03d}.root")


def _hist(f, name, scale=1.0):
    """{"edges", "values", "fit": (x, y) | None, "chi2", "ndf", "params"} of one histogram and the fit saved with it."""
    h = f[name]
    out = {"edges": h.axis().edges() * scale, "values": h.values(), "fit": None, "chi2": None, "ndf": None, "params": None}
    for fn in reversed(list(h.member("fFunctions"))):   # the final fit is stored last (free-W and retried fits come first)
        try:
            save = np.asarray(fn.member("fSave"), float)
            npx = int(fn.member("fNpx"))
            if len(save) >= npx + 3:   # saved curve: npx + 1 values, then its x range
                out["fit"] = (np.linspace(save[-2], save[-1], npx + 1) * scale, save[:npx + 1])
                out["chi2"], out["ndf"] = float(fn.member("fChisquare")), int(fn.member("fNDF"))
                try:
                    out["params"] = [float(v) for v in fn.member("fParams").member("fParameters")]
                except Exception:
                    pass
                break
        except Exception:
            continue
    return out


def load(date, run):
    """{"date", "run", "info": {...}, "chans": {ch: {"charge", "time", "stats": {branch: value}}}}."""
    if uproot is None:
        raise RuntimeError("uproot is not installed")
    p = path(date, run)
    if not os.path.exists(p):
        raise FileNotFoundError(f"No result file for run {date}_{int(run):03d}")
    out = {"date": date, "run": int(run), "info": {}, "chans": {}}
    with uproot.open(p) as f:
        try:
            ri = f["RunInfo"].arrays(library="np")
            out["info"] = {k: (v[0].decode() if isinstance(v[0], bytes) else v[0]) for k, v in ri.items()}
        except Exception:
            pass
        keys = {k.split(";")[0] for k in f.keys()}
        for ch in ROLE:
            if f"Pico_ch{ch}" not in keys or f"Diff_ch{ch}" not in keys:
                continue
            stats = {}
            if f"tree_ch{ch}" in keys:
                stats = {k: float(v[0]) for k, v in f[f"tree_ch{ch}"].arrays(library="np").items() if len(v)}
            out["chans"][ch] = {"charge": _hist(f, f"Pico_ch{ch}"),
                                "time": _hist(f, f"Diff_ch{ch}", NS_PER_SAMPLE), "stats": stats}
    return out

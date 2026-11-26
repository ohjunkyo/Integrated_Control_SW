"""Charge / Time / Contour report pages of one General Scan step: rows = PMT x scan axis, columns = stage tilt."""
import math
import os
import re

import numpy as np
from matplotlib.figure import Figure
from matplotlib.lines import Line2D

from core import env

try:
    import uproot
except ImportError:
    uproot = None
try:
    from PIL import Image
except ImportError:
    Image = None

DATA = os.path.join(env.ADC_DIR, "Data")
TILTS = (-55, -25, 0, 25, 55)
CHS = ((1, "Rot1", 2), (2, "Rot2", 3))   # (channel, holder, registry column index)
AXES = ("X", "Y")
CONTOUR_PX = (440, 396)   # every contour panel is resampled to this size (keeps the PDF small)
GREY = "#777777"
UNRELIABLE_RES = 50.0   # sigma/mu [%] above which a passed SPE fit is marked unreliable (display only)
_erf = np.vectorize(math.erf)


def _open(path):
    try:
        return uproot.open(path) if uproot is not None and os.path.exists(path) else None
    except Exception:
        return None


def _prd(date, run):
    return os.path.join(DATA, "production", f"precal_prd_kor_run_{date}_{run:03d}.root")


def _result(date, run):
    return os.path.join(DATA, "FinalResult", f"precal_result_kor_run_{date}_{run:03d}.root")


def _contour(date, run):
    return os.path.join(DATA, "image", "Contour", f"precal_raw_kor_run_{date}_{run:03d}_Contour.png")


def _tilt(v):
    try:
        t = float(v)
    except (TypeError, ValueError):
        return None
    return int(round(t)) if abs(t - round(t)) < 0.01 and int(round(t)) in TILTS else None


def points(rows):
    """{(ch, axis, tilt): (date, run, Hamamatsu angle)}; the axis comes from ScanAxis_chN of the production file,
    a later run of the same point replaces an earlier one."""
    out = {}
    for r in sorted(rows, key=lambda r: (r.get("date", ""), int(r["run"]) if str(r.get("run", "")).isdigit() else -1)):
        if not str(r.get("run", "")).isdigit():
            continue
        tilts = {ch: _tilt(r.get(f"tilt{col}")) for ch, _h, col in CHS}
        if not any(t is not None for t in tilts.values()):
            continue
        date, run = r["date"], int(r["run"])
        f = _open(_prd(date, run))
        if f is None:
            continue
        for ch, _h, _col in CHS:
            if tilts[ch] is None:
                continue
            try:
                axis = str(f[f"ScanAxis_ch{ch}"].member("fTitle")).strip()[:1].upper()
                ham = float(f[f"HamamatsuAngle_ch{ch}"].member("fVal"))
            except Exception:
                continue
            if axis in AXES:
                out[(ch, axis, tilts[ch])] = (date, run, ham)
    return out


def _pmt(rows, col):
    """'EM2230-F' from the sn / dir columns (the most frequent pair)."""
    seen = {}
    for r in rows:
        sn, d = (r.get(f"sn{col}") or "").strip(), (r.get(f"dir{col}") or "").strip()
        if sn:
            key = sn + (f"-{d}" if d and d != "-" else "")
            seen[key] = seen.get(key, 0) + 1
    return max(seen, key=seen.get) if seen else "?"


def _hv(rows, col):
    vals = sorted({(r.get(f"hv{col}") or "").strip() for r in rows} - {""},
                  key=lambda v: float(v) if v.replace(".", "", 1).isdigit() else 0)
    return ", ".join(vals) or "-"


def _curve(h):
    fn = h.member("fFunctions")
    if not len(fn):
        return None, None, None
    s = np.array(list(fn[0].all_members["fSave"]), dtype=float)
    if len(s) < 4:
        return None, None, fn[0]
    y, lo, hi = s[:-2], s[-2], s[-1]
    return np.linspace(lo, hi, len(y)), y, fn[0]


def _hist(h):
    ax = h.member("fXaxis")
    return np.linspace(ax.member("fXmin"), ax.member("fXmax"), ax.member("fNbins") + 1), h.values()


def _val(t, k):
    return float(t[k].array(library="np", entry_stop=1)[0])


def _note(a, text, color=GREY):
    a.text(0.5, 0.5, text, transform=a.transAxes, ha="center", va="center", fontsize=8, color=color)


def _corner(a, text, bad):
    a.text(0.97, 0.95, text, transform=a.transAxes, ha="right", va="top", fontsize=6.5,
           color="tab:red" if bad else "k")


def _charge(a, f, ch):
    h = f[f"Pico_ch{ch}"]
    e, y = _hist(h)
    a.step(e[:-1], y, where="post", color="k", lw=0.6)
    x, c, fn = _curve(h)
    t = f[f"tree_ch{ch}"]
    if fn is not None:
        try:
            par = [float(v) for v in fn.all_members["fParams"].all_members["fParameters"]]
        except Exception:
            par = []
        if len(par) == 7 and par[3] > 0 and par[4] > 0:
            amp, mu, ped, g, sp, ss, w = par
            xx = np.linspace(-2, 8, 800)
            sn = math.sqrt(sp * sp + ss * ss)
            p1 = amp * math.exp(-mu) * mu
            gaus = lambda m, s: np.exp(-0.5 * ((xx - m) / s) ** 2) / (s * math.sqrt(2 * math.pi))
            g1 = p1 * (1 - w) * gaus(ped + g, sn)
            b1 = p1 * w * (_erf((xx - ped) / sp) - _erf((xx - ped - g) / sn)) / (2 * g)
            a.plot(xx, amp * math.exp(-mu) * gaus(ped, sp), color="orange", lw=0.9)
            a.plot(xx, g1, color="k", lw=0.9, ls="--")
            a.plot(xx, b1, color="k", lw=0.9, ls=":")
            a.plot(xx, g1 + b1, color="k", lw=1.3)
        if x is not None:
            a.plot(x, c, color="tab:red", lw=1.0)
    a.set_yscale("log")
    a.set_xlim(-2, 8)
    a.set_ylim(0.5, max(float(np.max(y)), 1.0) * 3)
    bad = int(_val(t, "spe_fit_status")) == -1
    res = _val(t, "charge_resolution")
    _corner(a, "FIT FAILED" if bad else "Gain %.2f $\\pm$ %.2f pC\n$\\sigma/\\mu$ %.1f %%\nP/V %.1f" % (
        _val(t, "spe_mean"), _val(t, "spe_mean_error"), res, _val(t, "peak_to_valley")), bad)
    if not bad and res > UNRELIABLE_RES:
        a.annotate("UNRELIABLE ($\\sigma/\\mu$ > %g %%)" % UNRELIABLE_RES, (0.97, 0.95), xycoords="axes fraction",
                   xytext=(0, -3 * 6.5 * 1.25), textcoords="offset points", ha="right", va="top", fontsize=6.5,
                   color="darkorange", fontweight="bold")


def _time(a, f, ch):
    h = f[f"Diff_ch{ch}"]
    e, y = _hist(h)
    x, c, _fn = _curve(h)
    pk = e[int(np.argmax(y))]
    a.step((e[:-1] - pk) * 2, y, where="post", color="k", lw=0.6)
    if x is not None:
        a.plot((x - pk) * 2, c, color="tab:red", lw=1.0)
    a.set_xlim(-10, 20)
    a.set_ylim(0, max(float(np.max(y)), 1.0) * 1.25)
    t = f[f"tree_ch{ch}"]
    bad = int(_val(t, "tts_fit_status")) == -1
    _corner(a, "FIT FAILED" if bad else "FWHM %.2f ns\nTTS %.2f ns" % (2 * _val(t, "fwhm_exG"), 2 * _val(t, "rms_exG")), bad)


def _contour_img(a, date, run, ch):
    path = _contour(date, run)
    if Image is None or not os.path.exists(path):
        _note(a, "no image")
        return
    with Image.open(path) as im:
        im = im.convert("RGB")
        w, h = im.size
        crop = im.crop((w * ch // 4 + int(0.02 * w / 4), int(0.09 * h), w * (ch + 1) // 4, h))
        crop = crop.resize(CONTOUR_PX, Image.LANCZOS).quantize(64).convert("RGB")   # <=256 colours: indexed in the PDF
    a.imshow(np.asarray(crop), interpolation="none", aspect="auto")


def pages(label, wl, rows, progress=None):
    """[(kind, Figure)] for 'Charge', 'Time', 'Contour' of one step (CAEN rows of one wavelength).
    A missing point or result is an empty panel with a note."""
    pts = points(rows)
    from core import routine_results as RR
    wl_tag = f"{wl} nm" if wl else ""
    step = re.sub(rf"\s*\b{re.escape(str(wl))}\s*nm\b", "", label).strip() if wl_tag else label
    hv = " / ".join(_hv(rows, col) for _ch, _h, col in CHS)
    head = "   ".join(x for x in (wl_tag, step, f"runs {RR.run_span(rows)}" if rows else "", f"HV {hv} V") if x)
    names = {ch: f"{hold} {_pmt(rows, col)}" for ch, hold, col in CHS}
    files = {}

    def result(date, run):
        if (date, run) not in files:
            files[(date, run)] = _open(_result(date, run))
        return files[(date, run)]

    out = []
    for kind, xlab in (("Charge", "Charge [pC]"), ("Time", "Time from peak [ns]"), ("Contour", "")):
        if progress:
            progress(f"Drawing {kind} {wl_tag} ...")
        img = kind == "Contour"
        fig = Figure(figsize=(11.69, 8.27), facecolor="white")
        axs = fig.subplots(4, 5, sharex=not img)
        for r, (ch, axis) in enumerate((ch, axis) for ch, _h, _c in CHS for axis in AXES):
            for c, tilt in enumerate(TILTS):
                a = axs[r][c]
                pt = pts.get((ch, axis, tilt))
                if pt is None:
                    a.set_title(f"tilt {tilt:+d}$^\\circ$", fontsize=7, pad=2)
                    _note(a, "no point")
                else:
                    date, run, ham = pt
                    a.set_title("tilt %+d$^\\circ$ (Ham %+.1f$^\\circ$)   run %d" % (tilt, ham, run), fontsize=7, pad=2)
                    if img:
                        _contour_img(a, date, run, ch)
                    else:
                        f = result(date, run)
                        try:
                            if f is None:
                                raise FileNotFoundError
                            (_charge if kind == "Charge" else _time)(a, f, ch)
                        except Exception:
                            a.cla()
                            a.set_title("tilt %+d$^\\circ$ (Ham %+.1f$^\\circ$)   run %d" % (tilt, ham, run),
                                        fontsize=7, pad=2)
                            _note(a, "no result")
                if img:
                    a.set_xticks([])
                    a.set_yticks([])
                    for sp in a.spines.values():
                        sp.set_visible(False)
                elif not a.has_data():
                    a.set_yticks([])
                    for sp in a.spines.values():
                        sp.set_color("#cccccc")
                if not img:
                    a.tick_params(labelsize=6)
                    if a.has_data():
                        a.grid(alpha=0.25, lw=0.4)
                    if r == 3:
                        a.set_xlabel(xlab, fontsize=7)
                if c == 0:
                    a.set_ylabel(f"{names[ch]}\n{axis}-axis", fontsize=8)
        if not img:
            axs[0][0].set_xlim(*((-2, 8) if kind == "Charge" else (-10, 20)))
        fig.suptitle(f"{kind}   {head}", fontsize=11)
        fig.subplots_adjust(left=0.065, right=0.99, top=0.92, bottom=0.06 if not img else 0.02,
                            wspace=0.16 if not img else 0.03, hspace=0.22 if not img else 0.12)
        if kind == "Charge":
            hd = [Line2D([], [], color="k", lw=0.6), Line2D([], [], color="orange", lw=0.9),
                  Line2D([], [], color="k", lw=0.9, ls="--"), Line2D([], [], color="k", lw=0.9, ls=":"),
                  Line2D([], [], color="k", lw=1.3), Line2D([], [], color="tab:red", lw=1.0)]
            fig.legend(hd, ["Data", "Pedestal", "1 p.e. Gaussian", "1 p.e. backscatter", "1 p.e.", "Total fit"],
                       loc="upper right", ncol=6, fontsize=7, frameon=False, bbox_to_anchor=(0.99, 0.962))
            fig.subplots_adjust(top=0.905)
        out.append((kind.lower(), fig))
    return out


def blocks(ids):
    """[(date, first run, last run)] of a set of (date, run) ids; runs whose result is missing stay inside."""
    by = {}
    for d, r in ids:
        by.setdefault(d, []).append(r)
    return [(d, min(v), max(v)) for d, v in sorted(by.items())]

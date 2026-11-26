"""Histogram figure of one HK Digitizer scan point (pedestal, charge, hit time, time over threshold of the three
PMTs), drawn into a matplotlib Figure. Qt-free: used by the point window and by the saved images."""
import time

import numpy as np

from core import hk_results as HK

CH = {0: ("Monitor", "#BA7517"), 1: ("Rot1", "#378ADD"), 2: ("Rot2", "#1D9E75")}


def _edges(x):
    step = x[1] - x[0]
    return np.append(x - step / 2, x[-1] + step / 2)


def draw(fig, rec):
    """Fill the figure with the 4 x 3 histogram grid of one point record (core.hk_results.point)."""
    fig.clear()
    axs = fig.subplots(4, 3)
    for ch, (name, color) in CH.items():
        s, h = rec["stats"][ch], rec["hist"]
        ped, peak = s.get("ped", 0.0), s.get("peak")
        rows = (
            (HK.Q_X, h["ped"][ch], "Pedestal hgain [DAC]", (ped - 30, ped + 30), False,
             f"mean {ped:.1f}  std {s.get('ped_std', 0):.1f}"),
            (HK.Q_X, h["q"][ch], "Charge hgain [DAC]", (max(HK.Q_LO, ped - 40), HK.Q_LO + HK.Q_N), True,
             f"hits {s.get('q_n', 0):,}  mean {s.get('q', 0):.1f}\nmean - pedestal {s.get('dq', float('nan')):.1f}"),
            (HK.T_X, h["dt"][ch], "Hit time - trigger time [ns]",
             (peak - 15, peak + 40) if peak is not None else (0, HK.T_N * HK.T_BIN), True,
             (f"peak {peak:.1f} ns  FWHM {s['fwhm']:.2f} ns\nin-time hits {s['intime']:,.0f}"
              f" ({s['occ']:.2f} % of triggers)") if peak is not None else "no hits"),
            (HK.TOT_X, h["tot"][ch], "Time over threshold [TDC counts]", (0, 400), False, ""))
        for r, (x, y, xlabel, lim, log, text) in enumerate(rows):
            ax = axs[r][ch]
            ax.stairs(y, _edges(x), color=color, lw=1.1)
            ax.set_xlim(*lim)
            if log and max(y) > 0:
                ax.set_yscale("log")
            ax.set_xlabel(xlabel, fontsize=9)
            ax.set_ylabel("Counts", fontsize=9)
            ax.grid(color="#cccccc", linestyle=":", linewidth=0.8)
            ax.tick_params(labelsize=8)
            if r == 1:
                ax.axvline(ped, color="#888888", ls="--", lw=0.8)
            if text:
                ax.text(0.97, 0.95, text, transform=ax.transAxes, ha="right", va="top", fontsize=8,
                        bbox=dict(fc="white", ec="#999999", lw=0.6))
            if r == 0:
                ax.set_title(f"{rec['sn'].get(ch, name)}  ({name}, ch{ch})", fontsize=10)
    fig.suptitle(title(rec), fontsize=10)


def title(rec):
    axis = rec["axis"].get(1, "?")
    return (f"HK Digitizer  {rec['date']}  Run{rec['run']:03d}   {axis} axis  tilt {rec['raw'][1]:+.0f} deg   "
            f"{rec['wl']} nm   {rec['events']:,} triggers   {time.strftime('%H:%M', time.localtime(rec['time']))}")

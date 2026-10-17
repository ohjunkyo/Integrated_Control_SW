"""Waveform analysis of RAW .root files (uproot + numpy, no UI).

Same physics and file handling as the Tk app's managers/waveform_viewer.py:
  ADC_to_mV = 2000 / 16384, 50 Ohm, 2 ns per sample,
  charge_pC = TimePerSample * ADC_to_mV * sum(ped - adc[i]) / Impedance.
Searches, averaging and the noise scan work on whole blocks of events at once
(numpy over the event axis) instead of one event at a time.
"""
from dataclasses import dataclass, field

import numpy as np
import uproot

ADC_TO_MV = 0.1220703125
IMPEDANCE = 50.0
TIME_PER_SAMPLE = 2.0          # ns
DT = 2e-9                      # s per sample -> 500 MHz sampling
FS_MHZ = 1.0 / DT / 1e6
TRIGGER_CH = 3
# Laser-run windows of prod_ntp_v7.C (current fibre); open_run() picks the run's own, as the macro does.
PED_START, PED_END = 50, 550
SIG_START, SIG_END = 580, 630
FIBER_SHORTEN_DATE, SECOND_SHIFT_DATE = 20260720, 20260728
DARK_PRESCAN_EVENTS = 5000
NOISE_FREQS_MHZ = [50.0, 62.5, 100.0, 125.0]
NOISE_FREQ_LABELS = {50.0: "PLL ref", 62.5: "Cntr read", 100.0: "2x PLL", 125.0: "Trig clk"}


def pedestal(seg, ped_start=PED_START, ped_end=PED_END):
    sl = seg[ped_start:ped_end]
    return float(np.mean(sl)) if len(sl) else 0.0


def charge_pC(seg, ped, sig_start=SIG_START, sig_end=SIG_END):
    return TIME_PER_SAMPLE * ADC_TO_MV * float(np.sum(ped - seg[sig_start:sig_end].astype(float))) / IMPEDANCE


def spectrum(seg_mv, n):
    """One-sided, Hann-windowed amplitude spectrum [mV] and its frequencies [MHz]."""
    freqs = np.fft.rfftfreq(n, d=DT) / 1e6
    return freqs, np.abs(np.fft.rfft(seg_mv * np.hanning(n))) * 2.0 / n


@dataclass
class RunFile:
    path: str
    tree: object
    n_entries: int
    n_samples: int
    channels: list = field(default_factory=list)
    sig_start: int = SIG_START
    sig_end: int = SIG_END
    ped_start: int = PED_START
    ped_end: int = PED_END

    def entry(self, n):
        """Flat ADC array of one event (all channels back to back)."""
        try:
            raw = self.tree["ADC"].array(entry_start=n, entry_stop=n + 1, library="np")
            return np.asarray(raw[0]).astype(np.float64).ravel()
        except Exception:
            import awkward as ak
            raw = self.tree["ADC"].array(entry_start=n, entry_stop=n + 1, library="ak")
            return np.asarray(ak.to_numpy(raw[0])).astype(np.float64).ravel()

    def segment(self, flat, ch):
        n = self.n_samples
        return flat[ch * n:(ch + 1) * n]

    def blocks(self, start, stop, batch):
        """Yield (first_entry, block) with block shaped (events, channels, samples)."""
        for b in range(start, stop, batch):
            e = min(b + batch, stop)
            arr = np.asarray(self.tree["ADC"].array(entry_start=b, entry_stop=e, library="np"), dtype=np.float64)
            yield b, arr.reshape(e - b, -1, self.n_samples)


def _date_tag(path):
    """First 8-digit '_'-separated token of the file name (the macro's runDateTag), 0 if none."""
    import os
    for tok in os.path.basename(path).split("_"):
        tok = tok.split(".")[0]
        if len(tok) == 8 and tok.isdigit():
            return int(tok)
    return 0


def _dark_center(tree, channels, n_entries, rec_len, trg):
    """Deepest trough of the PMT channels' average waveform over the first 5000 events (> 0.5 mV),
    else the trigger point -- the macro's dark-mode pulse finder."""
    n = min(DARK_PRESCAN_EVENTS, n_entries)
    pmts = [c for c in channels if c != TRIGGER_CH]
    if n <= 0 or not pmts:
        return trg
    total = None
    for b in range(0, n, 1000):
        e = min(b + 1000, n)
        arr = np.asarray(tree["ADC"].array(entry_start=b, entry_stop=e, library="np"), dtype=np.float64)
        part = arr.reshape(e - b, -1, rec_len).sum(axis=0)
        total = part if total is None else total + part
    lo, hi = 5, max(15, rec_len // 4)
    best, depth_best = -1, 0.0
    for ch in pmts:
        avg = total[ch]
        depth = (avg[lo:hi].mean() - avg) / n * ADC_TO_MV
        k = int(np.argmax(depth))
        if depth[k] > depth_best:
            best, depth_best = k, float(depth[k])
    return best if best > 0 and depth_best > 0.5 else trg


def analysis_windows(path, tree, channels, n_entries, rec_len, post_trigger, dark):
    """(ped_start, ped_end, sig_start, sig_end) exactly as the production macro sets them for this file."""
    trg = int(rec_len * (1.0 - post_trigger / 100.0))
    if dark:
        c = _dark_center(tree, channels, n_entries, rec_len, trg)
        ped_s, ped_e, sig_s = 5, max(25, c - 35), max(0, c - 15)
    else:
        tag = _date_tag(path)
        ped_s, ped_e = PED_START, PED_END
        sig_s = 580 if (tag == 0 or tag >= SECOND_SHIFT_DATE) else (595 if tag >= FIBER_SHORTEN_DATE else 610)
    return ped_s, ped_e, sig_s, min(sig_s + 50, rec_len)


def open_run(path):
    """Open a RAW file: entries, record length, enabled channels and the signal window.
    Per-run constants are read from T (older files) or RunInfo (from ADC_test8 on)."""
    f = uproot.open(path)
    tree = f["T"]
    keys = [k.split(";")[0] for k in tree.keys()]
    info = None
    for name in ("RunInfo", "InfoTree"):
        try:
            info = f[name]
            break
        except Exception:
            continue

    def const(name, default):
        if name in keys:
            return int(tree[name].array(entry_start=0, entry_stop=1, library="np")[0])
        if info is not None:
            try:
                return int(info[name].array(library="np")[0])
            except Exception:
                pass
        return default

    rec_len = const("RecordLength", 1024)
    post_trigger = const("PostTrigger", 60)

    channels = [i for i in range(8) if f"OffsetValue{i}" in keys]
    if not channels and info is not None:
        info_keys = [k.split(";")[0] for k in info.keys()]
        if "ChannelMask" in info_keys:   # "00001111": leftmost character is ch7
            try:
                mask = info["ChannelMask"].array(library="np")[0]
                mask = (mask.decode("ascii", "ignore") if isinstance(mask, bytes) else str(mask)).strip()
                channels = [i for i in range(min(len(mask), 8)) if mask[len(mask) - 1 - i] == "1"]
            except Exception:
                channels = []
        if not channels:
            channels = [i for i in range(8) if f"OffsetValue{i}" in info_keys]

    dark = False
    if info is not None:
        try:
            mode = info["RunMode"].array(library="np")[0]
            mode = mode.decode("ascii", "ignore") if isinstance(mode, bytes) else str(mode)
            dark = "dark" in mode.strip().lower()
        except Exception:
            pass
    ped_s, ped_e, sig_s, sig_e = analysis_windows(path, tree, channels, tree.num_entries, rec_len, post_trigger, dark)
    return RunFile(path, tree, tree.num_entries, rec_len, channels, sig_s, sig_e, ped_s, ped_e)


def _charges(block, chans, ped, sig):
    """Charge [pC] per event and channel for a block, shape (events, len(chans))."""
    seg = block[:, chans, :]
    p = seg[:, :, ped[0]:ped[1]].mean(axis=2, keepdims=True)
    return TIME_PER_SAMPLE * ADC_TO_MV * (p - seg[:, :, sig[0]:sig[1]]).sum(axis=2) / IMPEDANCE


def find_charge(run, start, match, ped, sig, stop_flag, progress=None, batch=1000):
    """First entry >= start where any PMT channel's charge satisfies match(q array).
    Returns the entry, -1 if none, or None if stopped."""
    chans = [c for c in run.channels if c != TRIGGER_CH]
    if not chans:
        return -1
    for i, (b, block) in enumerate(run.blocks(start, run.n_entries, batch)):
        if stop_flag():
            return None
        hit = np.nonzero(match(_charges(block, chans, ped, sig)).any(axis=1))[0]
        if hit.size:
            return b + int(hit[0])
        if progress and i % 10 == 0:
            progress(b)
    return -1


def find_peak(run, start, ch, mv_thr, ped, stop_flag, progress=None, batch=1000):
    """First entry >= start where |v - pedestal| exceeds mv_thr on channel ch."""
    for i, (b, block) in enumerate(run.blocks(start, run.n_entries, batch)):
        if stop_flag():
            return None
        seg = block[:, ch, :] * ADC_TO_MV
        dev = np.abs(seg - seg[:, ped[0]:ped[1]].mean(axis=1, keepdims=True)).max(axis=1)
        hit = np.nonzero(dev > mv_thr)[0]
        if hit.size:
            return b + int(hit[0])
        if progress and i % 10 == 0:
            progress(b)
    return -1


def average(run, ev_from, ev_to, ped, stop_flag, progress=None, batch=2000):
    """RMS-average |FFT| [mV] and mean waveform [ADC] per channel over [ev_from, ev_to).
    Returns (spectra, waves, count, stopped)."""
    n = run.n_samples
    window = np.hanning(n)
    power = {ch: np.zeros(n // 2 + 1) for ch in run.channels}
    adc = {ch: np.zeros(n) for ch in run.channels}
    count, stopped = 0, False
    b = ev_from
    while b < ev_to:
        if stop_flag():
            stopped = True
            break
        e = min(b + batch, ev_to)
        try:
            arr = np.asarray(run.tree["ADC"].array(entry_start=b, entry_stop=e, library="np"), dtype=np.float64)
        except Exception:
            b = e
            continue
        block = arr.reshape(e - b, -1, n)
        for ch in run.channels:
            seg = block[:, ch, :]
            adc[ch] += seg.sum(axis=0)
            mv = (seg - seg[:, ped[0]:ped[1]].mean(axis=1, keepdims=True)) * ADC_TO_MV
            power[ch] += ((np.abs(np.fft.rfft(mv * window, axis=1)) * 2.0 / n) ** 2).sum(axis=0)
        count += e - b
        b = e
        if progress:
            progress(count)
    if not count:
        return {}, {}, 0, stopped
    return ({ch: np.sqrt(power[ch] / count) for ch in run.channels},
            {ch: adc[ch] / count for ch in run.channels}, count, stopped)


def noise_scan(run, freqs_mhz, amp_thr_uv, stop_flag, progress=None, batch=500):
    """Events whose FFT amplitude at a clock frequency exceeds amp_thr_uv [uV].
    One hit (the first listed frequency over threshold) per event and channel.
    Returns (results [(event, ch, f_MHz, amp_uV)], stopped)."""
    n = run.n_samples
    grid = np.fft.rfftfreq(n, d=DT) / 1e6
    bins = [int(np.argmin(np.abs(grid - f))) for f in freqs_mhz]
    window = np.hanning(n)
    chans = [c for c in run.channels if c != TRIGGER_CH]
    out = []
    for i, (b, block) in enumerate(run.blocks(0, run.n_entries, batch)):
        if stop_flag():
            return out, True
        for ch in chans:
            seg = block[:, ch, :]
            mv = (seg - seg[:, run.ped_start:run.ped_end].mean(axis=1, keepdims=True)) * ADC_TO_MV
            amp = (np.abs(np.fft.rfft(mv * window, axis=1)) * 2.0 / n)[:, bins] * 1e3   # (events, freqs)
            over = amp > amp_thr_uv
            for ev in np.nonzero(over.any(axis=1))[0]:
                k = int(np.argmax(over[ev]))
                out.append((b + int(ev), ch, freqs_mhz[k], float(amp[ev, k])))
        if progress and i % 4 == 0:
            progress(b)
    return out, False

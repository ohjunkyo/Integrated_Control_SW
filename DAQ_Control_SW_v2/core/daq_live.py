"""Reader of the DAQ live snapshot /dev/shm/precal_daq_live.bin (written by execute_DAQ_v2 every 0.5 s):
per active channel the last event, the mean of up to 200 events of the last 0.5 s and a density map
(counts per ADC count and sample) of all events of the last 0.5 s. Qt-free."""
import os
import struct

import numpy as np

PATH = "/dev/shm/precal_daq_live.bin"
_HEAD = struct.Struct("<4s6IQdd16s200s8I")
_cache = {"mtime": None, "snap": None}


def read(path=PATH):
    """-> dict or None. Keys: state ('running'/'finished'), channels, record_length, post_trigger, n_mean,
    events, elapsed_s, time (unix), mode, output, last {ch: uint16 array}, mean {ch: float32 array},
    n_map, map {ch: {"lo": ADC count of row 0, "counts": uint16 (ADC count, sample)}} (empty from an old DAQ)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    if _cache["mtime"] == st.st_mtime_ns:
        return _cache["snap"]
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    if len(raw) < _HEAD.size:
        return None
    (magic, version, state, nch, rl, post, n_mean, events, elapsed, t, mode, out,
     *chans) = _HEAD.unpack_from(raw)
    if magic != b"PCLV" or version not in (1, 2) or nch > 8 or len(raw) < _HEAD.size + nch * rl * 6:
        return None
    off = _HEAD.size
    last, mean = {}, {}
    for k in range(nch):
        ch = chans[k]
        last[ch] = np.frombuffer(raw, "<u2", rl, off)
        off += 2 * rl
        mean[ch] = np.frombuffer(raw, "<f4", rl, off)
        off += 4 * rl
    n_map, dmap = 0, {}
    if version == 2:
        height, n_map = struct.unpack_from("<2I", raw, off)
        off += 8
        if len(raw) != off + nch * (4 + 2 * height * rl):
            return None
        for k in range(nch):
            lo, = struct.unpack_from("<i", raw, off)
            dmap[chans[k]] = {"lo": lo, "counts": np.frombuffer(raw, "<u2", height * rl, off + 4).reshape(height, rl)}
            off += 4 + 2 * height * rl
    snap = {"state": "finished" if state else "running", "channels": list(chans[:nch]), "record_length": rl,
            "post_trigger": post, "n_mean": n_mean, "events": events, "elapsed_s": elapsed, "time": t,
            "mode": mode.split(b"\0")[0].decode(errors="replace"),
            "output": out.split(b"\0")[0].decode(errors="replace"), "last": last, "mean": mean, "n_map": n_map, "map": dmap}
    _cache.update(mtime=st.st_mtime_ns, snap=snap)
    return snap

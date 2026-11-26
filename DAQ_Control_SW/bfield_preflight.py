# bfield_preflight.py
import glob
import os
import time
from datetime import datetime

ADC_DIR = os.path.expanduser("~/ADC/ADC_test")
COILS = ("I1", "I2", "I3", "I4")
COIL_OFF_A = 0.01
MAX_AGE_S = 3600.0   # logger writes every 10 min; older than this the coil state is unknown
TAIL_BYTES = 65536


def coils_off_since(adc_dir=ADC_DIR):
    """Time the coils went off if the newest fresh sample has I1-I4 all at zero, else None."""
    paths = glob.glob(os.path.join(adc_dir, "log_bfield_*.csv"))
    if not paths:
        return None
    path = max(paths, key=os.path.getmtime)
    try:
        with open(path, "rb") as f:
            header = f.readline().decode(errors="replace").strip().split(",")
            f.seek(max(f.tell(), os.path.getsize(path) - TAIL_BYTES))
            lines = [ln for ln in f.read().decode(errors="replace").splitlines() if ln.strip()]
        idx = [header.index(c) for c in COILS]
    except (OSError, ValueError):
        return None

    def off(line):
        cells = line.strip().split(",")
        try:
            return all(abs(float(cells[i])) < COIL_OFF_A for i in idx), cells[0][:19]
        except (ValueError, IndexError):
            return False, None

    since = None
    for line in reversed(lines[1:]):      # the first tail line may be cut in half
        is_off, ts = off(line)
        if not is_off:
            break
        since = ts
    if since is None:
        return None
    try:
        last = datetime.strptime(off(lines[-1])[1], "%Y-%m-%d %H:%M:%S").timestamp()
    except (TypeError, ValueError):
        return None
    if time.time() - last > MAX_AGE_S:
        return None
    return since[:16]

"""Run hold markers in /tmp/daq_flags ("<date> <pid>"), read by sync_data_v2.sh and remote_ops.py."""
import os
from datetime import datetime

DIR = "/tmp/daq_flags"
STABILITY = os.path.join(DIR, "active_stability")


def set_stability(on):
    try:
        if on:
            os.makedirs(DIR, exist_ok=True)
            with open(STABILITY, "w") as f:
                f.write(f"{datetime.now():%Y%m%d} {os.getpid()}\n")
        elif os.path.exists(STABILITY):
            os.remove(STABILITY)
    except OSError:
        pass


def live(path):
    """True while the marker exists and the process that wrote it is alive."""
    try:
        with open(path) as f:
            pid = int(f.read().split()[1])
        return os.path.exists(f"/proc/{pid}")
    except (OSError, ValueError, IndexError):
        return False

"""When the next data acquisition is due while the routine waits (interval, HV settle, gain delay)."""
import threading
import time

_lock = threading.Lock()
_due = None


def wait_until(t):
    global _due
    with _lock:
        _due = float(t)


def clear():
    global _due
    with _lock:
        _due = None


def seconds_left():
    """Seconds to the next acquisition, or None when nothing is waiting."""
    with _lock:
        due = _due
    if due is None:
        return None
    left = due - time.time()
    return left if left > -2 else None

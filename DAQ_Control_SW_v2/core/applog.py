"""The app log the Tk app writes with its _log: logs/log_<date>.txt, one
'[YYYY-mm-dd HH:MM:SS] message' line per entry. Qt-free."""
import os
from datetime import datetime

from core import env

LOG_DIR = os.path.join(env.DAQ_DIR, "logs")


def log(message):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        now = datetime.now()
        with open(os.path.join(LOG_DIR, f"log_{now:%Y-%m-%d}.txt"), "a") as f:
            f.write(f"[{now:%Y-%m-%d %H:%M:%S}] {message}\n")
    except OSError as e:
        print(f"Error while logging: {e}")


_notifier = None


class _NullNotifier:
    """Stands in for Slack when V2_NO_HW=1 (tests): records, never posts."""
    enabled = False

    def __init__(self):
        self.sent = []

    def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        return False

    def __getattr__(self, name):
        return lambda *a, **k: None


_NULL_NOTIFIER = _NullNotifier()


def notifier():
    """The Tk app's Slack notifier (managers/notifier.py, Tk-free), one per process."""
    global _notifier
    if env.NO_HW:
        return _NULL_NOTIFIER
    if _notifier is None:
        from managers.notifier import Notifier
        _notifier = Notifier(log_fn=log)
    return _notifier

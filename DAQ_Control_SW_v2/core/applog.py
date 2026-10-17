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


def notifier():
    """The Tk app's Slack notifier (managers/notifier.py, Tk-free), one per process."""
    global _notifier
    if _notifier is None:
        from managers.notifier import Notifier
        _notifier = Notifier(log_fn=log)
    return _notifier

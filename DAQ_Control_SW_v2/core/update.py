"""Update & Restart, as the Tk app's status-bar badge: notice newer source files on disk, refuse code
that does not parse, wait for idle (or restart when idle), and relaunch without touching the hardware."""
import ast
import glob
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime

from core import env

V2_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTLE_S = 5.0          # a file an editor is still writing does not count yet


def watched_files():
    """v2's own sources plus the Tk modules it runs (scan engine, stages, config, laser driver)."""
    files = [os.path.join(V2_DIR, "main.py")]
    for sub in ("app", "core", "pages", "pages/daq", "pages/general_scan", "widgets"):
        files += glob.glob(os.path.join(V2_DIR, sub, "*.py"))
    files += glob.glob(os.path.join(env.DAQ_DIR, "*.py")) + glob.glob(os.path.join(env.DAQ_DIR, "managers", "*.py"))
    root = os.path.dirname(env.DAQ_DIR)
    files += [os.path.join(root, "instance_lock.py"), os.path.join(root, "Laser_Control_SW", "app", "laser_driver.py")]
    return [f for f in files if os.path.exists(f)]


class Updater:
    def __init__(self):
        self.baseline = time.time()
        self.available = False
        self.when_idle = False

    def check(self):
        """-> True when an update has just been noticed."""
        if self.available:
            return False
        newest = 0.0
        for p in watched_files():
            try:
                newest = max(newest, os.path.getmtime(p))
            except OSError:
                pass
        if newest > self.baseline and time.time() - newest >= SETTLE_S:
            self.available = True
            return True
        return False

    @staticmethod
    def syntax_error():
        for p in watched_files():
            try:
                with open(p, encoding="utf-8") as f:
                    ast.parse(f.read(), filename=p)
            except SyntaxError as e:
                return f"{os.path.basename(p)}:{e.lineno}: {e.msg}"
            except (OSError, ValueError) as e:
                return f"{os.path.basename(p)}: {e}"
        return None

    @staticmethod
    def busy_reason():
        from core import scan, status
        from core.jobs import JOBS, LABELS
        from core.stage import stage
        host = scan._host
        if host is not None and host.auto_mgr.is_running:
            return "an automated scan is running"
        for slot, job in JOBS.jobs.items():
            if job.state == "running":
                return f"a console job is running ({LABELS.get(slot, slot)})"
        mgr = stage().mgr
        if mgr is not None and any(mgr.is_moving.values()):
            return "a motor is moving"
        if status._daq_running():
            return "a DAQ acquisition (execute_DAQ_v2) is running"
        return None

    @staticmethod
    def restart(app):
        """Relaunch with the same arguments once this process has exited. Lasers keep their LD/TEC state
        (only the USB handles are released), motors get no command, and the new copy reconnects the
        lasers without asking."""
        from core import laser as L
        from core import ups
        ctl = L.laser()
        try:
            ctl._save_connections()            # before the handles go, so the new copy knows what to reopen
            for wl in L.WAVELENGTHS:
                if ctl.connected(wl):
                    ctl.disconnect(wl, user=False)
        except Exception:
            pass
        try:
            ups.service().stop()
        except Exception:
            pass
        entry = os.path.join(V2_DIR, "main.py")
        cmd = " ".join(shlex.quote(a) for a in [sys.executable, entry] + sys.argv[1:])
        watcher = f"while kill -0 {os.getpid()} 2>/dev/null; do sleep 0.3; done; exec {cmd}"
        try:
            with open(os.path.join(env.DAQ_DIR, "logs", "restart.log"), "a") as f:
                f.write(f"[{datetime.now()}] v2 restart -> {cmd}\n")
        except OSError:
            pass
        run_env = dict(os.environ, APP_RESTART_AUTO_RECONNECT="1")
        subprocess.Popen(["bash", "-c", watcher], cwd=V2_DIR, start_new_session=True, env=run_env)
        app.closeAllWindows()
        os._exit(0)   # as the Tk app: a lingering worker thread must not keep the old copy alive


UPDATER = Updater()

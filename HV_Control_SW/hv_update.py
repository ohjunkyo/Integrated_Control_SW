"""Update & Restart for the HV monitor, as DAQ v2's badge: notice newer sources on disk, refuse code that does
not parse, and relaunch with the same arguments. HV output is untouched: the crate keeps its setpoints."""
import ast
import glob
import os
import shlex
import subprocess
import sys
import time

HV_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HV_DIR)
SETTLE_S = 5.0
LOG = os.path.expanduser("~/logs/launcher_hv.log")


def watched_files():
    files = glob.glob(os.path.join(HV_DIR, "*.py")) + glob.glob(os.path.join(HV_DIR, "workers", "*.py"))
    files += [os.path.join(ROOT, n) for n in ("instance_lock.py", "window_focus.py", "hv_request_file.py")]
    return [f for f in files if os.path.exists(f)]


class Updater:
    def __init__(self):
        self.baseline = time.time()
        self.available = False

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
        try:
            import hv_request_file
            if hv_request_file.pending():
                return "an HV change request is being handled"
        except Exception:
            pass
        for pat in ("ADC_test/script_v7.sh", "execute_DAQ_v2"):
            r = subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True)
            if any(pid and int(pid) != os.getpid() for pid in r.stdout.split()):
                return "a DAQ acquisition is running"
        return None

    @staticmethod
    def schedule_relaunch():
        """Start a watcher that relaunches this app once the current process has exited."""
        cmd = " ".join(shlex.quote(a) for a in [sys.executable, os.path.join(HV_DIR, "monitoring_app.py")] + sys.argv[1:])
        watcher = f"while kill -0 {os.getpid()} 2>/dev/null; do sleep 0.3; done; exec {cmd} >> {shlex.quote(LOG)} 2>&1"
        subprocess.Popen(["bash", "-c", watcher], cwd=HV_DIR, start_new_session=True, stdin=subprocess.DEVNULL)


UPDATER = Updater()

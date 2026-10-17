"""Control access, same model as the Tk app: one master-password Unlock opens every control
until Lock. While unlocked v2 holds the 'daq_v2_control' instance lock, so the Tk app refuses
its own Unlock; v2 refuses to unlock while the Tk app is open (it owns the hardware then)."""
import ast
import os

from core import env  # puts instance_lock on sys.path
import instance_lock  # noqa: E402

LOCK_NAME = "daq_v2_control"


def _admin_password():
    """ADMIN_PASSWORD from the Tk app's control_access.py, parsed without importing Tk."""
    path = os.path.join(env.DAQ_DIR, "managers", "control_access.py")
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except (OSError, SyntaxError):
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "ADMIN_PASSWORD" for t in node.targets):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return node.value.value
    return None


class Access:
    def __init__(self):
        self.unlocked = False
        self.listeners = []

    def _notify(self):
        for fn in list(self.listeners):
            try:
                fn(self.unlocked)
            except RuntimeError:   # its widget was deleted (window rebuilt)
                self.listeners.remove(fn)

    def blocker(self):
        """Why unlocking is not possible right now, or None."""
        if instance_lock.is_running("daq"):
            return "The Tk DAQ app is open and owns the hardware. Close it first."
        if _admin_password() is None:
            return "Master password not found in the Tk app's control_access.py."
        return None

    def unlock(self, password):
        """-> (ok, message)."""
        if self.unlocked:
            return True, ""
        why = self.blocker()
        if why:
            return False, why
        if password != _admin_password():
            return False, "Incorrect password."
        held = instance_lock.acquire(LOCK_NAME)
        if held is not None:
            return False, f"Controls are held by another process: {instance_lock.describe(held)}"
        self.unlocked = True
        self._notify()
        return True, ""

    def lock(self):
        if not self.unlocked:
            return
        self.unlocked = False
        instance_lock.release(LOCK_NAME)
        self._notify()


ACCESS = Access()

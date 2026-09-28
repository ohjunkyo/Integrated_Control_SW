# instance_lock.py -- one running copy per app, shared by DAQ, HV monitor,
# laser GUI and the launcher.
#
# flock() is released by the kernel when the holder dies, so a crash never
# leaves a stale lock. A forked child (e.g. the HV CAEN worker) keeps the lock
# until it exits too, which is intended: it still owns the device.
import fcntl
import os
import signal
import sys
import time

LOCK_DIR = os.path.expanduser("~/.cache/precal/locks")

APP_LABELS = {
    "daq": "DAQ/LASER/UPS Control Panel",
    "hv": "HV Monitor",
    "laser_gui": "Laser Control (old)",
}

_held = {}


def _path(name):
    os.makedirs(LOCK_DIR, exist_ok=True)
    return os.path.join(LOCK_DIR, f"{name}.lock")


def _read(fd):
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        parts = os.read(fd, 4096).decode(errors="replace").split("\n")
        pid = int(parts[0]) if parts and parts[0].strip().isdigit() else 0
        return {"pid": pid,
                "started": parts[1] if len(parts) > 1 else "",
                "cmd": parts[2] if len(parts) > 2 else ""}
    except OSError:
        return {"pid": 0, "started": "", "cmd": ""}


def acquire(name):
    """Take the lock. Returns None on success, else the holder's info dict."""
    if name in _held:
        return None
    fd = os.open(_path(name), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        info = _read(fd)
        os.close(fd)
        return info
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, f"{os.getpid()}\n{time.strftime('%Y-%m-%d %H:%M:%S')}\n{' '.join(sys.argv)}\n".encode())
    _held[name] = fd
    return None


def holder(name):
    """Info dict of whoever holds the lock, or None if nobody does."""
    fd = os.open(_path(name), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        info = _read(fd)
        os.close(fd)
        return info
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
    return None


def is_running(name):
    return holder(name) is not None


def pid_alive(pid):
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def terminate(name, timeout=10.0):
    """SIGTERM the holder, SIGKILL after timeout, then wait for the lock to
    free (children may hold it a little longer). True if it is free."""
    info = holder(name)
    if info is None:
        return True
    pid = info["pid"]
    if pid_alive(pid):
        os.kill(pid, signal.SIGTERM)
        end = time.time() + timeout
        while pid_alive(pid) and time.time() < end:
            time.sleep(0.2)
        if pid_alive(pid):
            os.kill(pid, signal.SIGKILL)
    end = time.time() + timeout
    while is_running(name) and time.time() < end:
        time.sleep(0.2)
    return not is_running(name)


def describe(info):
    if not info:
        return ""
    s = f"PID {info['pid'] or '?'}"
    if info.get("started"):
        s += f", started {info['started']}"
    return s


def ensure_single_tk(name, window_title, warn_if=None):
    """Tk apps: take the lock or ask what to do. Returns only if this process
    now holds the lock; otherwise exits. `warn_if()` may return a string that
    is added to the Replace warning (e.g. a scan in progress)."""
    info = acquire(name)
    if info is None:
        return
    import tkinter as tk
    label = APP_LABELS.get(name, name)
    choice = {"v": "cancel"}
    root = tk.Tk()
    root.title(f"{label} is already running")
    root.resizable(False, False)
    tk.Label(root, justify="left", padx=16, pady=12, font=("Helvetica", 11),
             text=f"{label} is already running ({describe(info)}).\n\n"
                  "Show it: bring the running window to the front.\n"
                  "Replace it: stop the running copy and start this one.").pack()
    row = tk.Frame(root, padx=12, pady=10)
    row.pack(fill="x")

    def pick(v):
        choice["v"] = v
        root.destroy()
    tk.Button(row, text="Show it", width=10, command=lambda: pick("show")).pack(side="left", padx=4)
    tk.Button(row, text="Replace it", width=10, command=lambda: pick("replace")).pack(side="left", padx=4)
    tk.Button(row, text="Cancel", width=10, command=lambda: pick("cancel")).pack(side="right", padx=4)
    root.protocol("WM_DELETE_WINDOW", lambda: pick("cancel"))
    root.mainloop()
    _resolve(name, window_title, choice["v"], warn_if, gui="tk")


def ensure_single_qt(name, window_title, parent=None):
    info = acquire(name)
    if info is None:
        return
    from PyQt5.QtWidgets import QMessageBox
    label = APP_LABELS.get(name, name)
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Warning)
    box.setWindowTitle(f"{label} is already running")
    box.setText(f"{label} is already running ({describe(info)}).")
    box.setInformativeText("Show it: bring the running window to the front.\n"
                           "Replace it: stop the running copy and start this one.")
    b_show = box.addButton("Show it", QMessageBox.AcceptRole)
    b_rep = box.addButton("Replace it", QMessageBox.DestructiveRole)
    box.addButton("Cancel", QMessageBox.RejectRole)
    box.exec_()
    clicked = box.clickedButton()
    v = "show" if clicked is b_show else "replace" if clicked is b_rep else "cancel"
    _resolve(name, window_title, v, None, gui="qt")


def _confirm(gui, title, text):
    if gui == "qt":
        from PyQt5.QtWidgets import QMessageBox
        return QMessageBox.question(None, title, text) == QMessageBox.Yes
    import tkinter as tk
    from tkinter import messagebox
    r = tk.Tk(); r.withdraw()
    ok = messagebox.askyesno(title, text, icon="warning", parent=r)
    r.destroy()
    return ok


def _error(gui, title, text):
    if gui == "qt":
        from PyQt5.QtWidgets import QMessageBox
        QMessageBox.critical(None, title, text)
        return
    import tkinter as tk
    from tkinter import messagebox
    r = tk.Tk(); r.withdraw()
    messagebox.showerror(title, text, parent=r)
    r.destroy()


def _resolve(name, window_title, choice, warn_if, gui):
    label = APP_LABELS.get(name, name)
    if choice == "show":
        try:
            from window_focus import focus_window
            if focus_window(window_title) != "raised":
                _error(gui, label, f"The running {label} has no window. It may be hung -- "
                                   "start this one again and choose Replace.")
        except Exception:
            pass
        sys.exit(0)
    if choice != "replace":
        sys.exit(0)
    extra = warn_if() if warn_if else ""
    text = f"Stop the running {label} ({describe(holder(name))}) and start a new one?"
    if extra:
        text += f"\n\n{extra}"
    if not _confirm(gui, f"Replace {label}", text):
        sys.exit(0)
    if not terminate(name):
        _error(gui, label, f"The old {label} did not release its lock within the time limit. "
                           "Check `ps` for leftover processes.")
        sys.exit(1)
    if acquire(name) is not None:
        _error(gui, label, "Another copy started at the same time. Quitting.")
        sys.exit(1)

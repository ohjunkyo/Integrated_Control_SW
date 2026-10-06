# managers/control_access.py
import tkinter as tk
from tkinter import messagebox, simpledialog
import subprocess
import os

ADMIN_PASSWORD = "root"

class ControlAccessManager:
    def __init__(self, controller, password=None): 
        self.controller = controller
        self.unlocked = False


    def is_production_running(self):
        """Check if ANOTHER main.py is already running, ignoring this process.

        pgrep -f 'python.*main.py' matches the WHOLE command line as text --
        which also matches things that merely MENTION "main.py" without
        actually being a running instance of it. The known false positive:
        an Update&Restart in flight spawns a detached bash watcher whose own
        script text literally contains "... exec .../python3 main.py ...";
        that bash process is not python at all, but the substring match still
        counted it as "another instance", falsely blocking Unlock right after
        a restart. Verify each candidate PID's actual executable via /proc so
        only genuine python processes count."""
        try:
            result = subprocess.run(['pgrep', '-f', 'main.py'], capture_output=True, text=True)
            pids = result.stdout.strip().split()
            current_pid = str(os.getpid())

            for pid in pids:
                if pid == current_pid:
                    continue
                try:
                    with open(f"/proc/{pid}/comm") as f:
                        comm = f.read().strip()
                except (FileNotFoundError, ProcessLookupError):
                    continue   # process gone by the time we checked
                except Exception:
                    continue
                if comm.lower().startswith("python"):
                    return True
            return False
        except Exception:
            return False

    def request_unlock(self):
        """Request master password and unlock controls if verified."""
        if self.unlocked:
            self.unlocked = False
            messagebox.showinfo("Lock", "Control access has been locked.")
            return True

        if self.is_production_running():
            messagebox.showerror("Collision Alert", 
                                 "⚠️ Production (main.py) is already running!\n"
                                 "Cannot acquire control access to prevent hardware collision.")
            return False

        pwd = simpledialog.askstring("Security", "Enter Master Password:", show='*')
        if pwd is None:
            return False 
        
        if pwd == ADMIN_PASSWORD:
            self.unlocked = True
            messagebox.showinfo("Success", "Control access has been activated.")
            return True
        else:
            messagebox.showerror("Error", "Incorrect password.")
            return False

    def verify_password_prompt(self, title="Security", prompt="Enter Master Password:",
                               parent=None):
        """One-off password re-check that does NOT read or touch self.unlocked.

        request_unlock() is a toggle tied to the general Unlock Controls
        banner (unlock stays active for the whole session once granted).
        Some actions -- e.g. Scan Parameters -- are meant to demand the
        password EVERY time regardless of that banner's state, so they must
        not call request_unlock() (which would silently re-lock the whole
        system if it happened to already be unlocked, since it's a toggle).
        """
        pwd = self._ask_password(title, prompt, parent)
        if pwd is None:
            return False
        if pwd == ADMIN_PASSWORD:
            return True
        messagebox.showerror("Error", "Incorrect password.",
                             **({"parent": parent} if parent is not None else {}))
        return False

    def _ask_password(self, title, prompt, parent=None):
        """Password box sized for an actual sentence of prompt text.

        simpledialog.askstring() sizes itself to a short label and attaches to
        the default root, so a long prompt came out as a cramped box that also
        rendered BEHIND whichever always-on-top dialog requested it. This one
        is parented to the caller, centered on it, and lifted above it.
        """
        owner = parent if parent is not None else tk._default_root
        if owner is None:
            return simpledialog.askstring(title, prompt, show='*')

        win = tk.Toplevel(owner)
        win.title(title)
        win.resizable(False, False)
        win.transient(owner)

        result = {"value": None}
        body = tk.Frame(win, padx=22, pady=18)
        body.pack(fill=tk.BOTH, expand=True)
        tk.Label(body, text=prompt, font=("Helvetica", 10), anchor="w",
                 justify="left", wraplength=380).pack(fill=tk.X, pady=(0, 10))
        var = tk.StringVar()
        ent = tk.Entry(body, textvariable=var, show="*", font=("Helvetica", 11), width=32)
        ent.pack(fill=tk.X, ipady=4)

        def _ok(_e=None):
            result["value"] = var.get()
            win.destroy()

        def _cancel(_e=None):
            result["value"] = None
            win.destroy()

        btns = tk.Frame(body)
        btns.pack(fill=tk.X, pady=(14, 0))
        tk.Button(btns, text="Cancel", width=10, command=_cancel).pack(side=tk.RIGHT)
        ok_btn = tk.Button(btns, text="OK", width=10, command=_ok,
                           font=("Helvetica", 10, "bold"))
        ok_btn.pack(side=tk.RIGHT, padx=(0, 8))

        win.bind("<Return>", _ok)
        win.bind("<Escape>", _cancel)
        win.protocol("WM_DELETE_WINDOW", _cancel)

        win.update_idletasks()
        w, h = win.winfo_width(), win.winfo_height()
        x = owner.winfo_rootx() + (owner.winfo_width() - w) // 2
        y = owner.winfo_rooty() + (owner.winfo_height() - h) // 3
        win.geometry(f"+{max(0, x)}+{max(0, y)}")

        win.attributes("-topmost", True)
        win.lift()
        ent.focus_force()
        win.grab_set()
        owner.wait_window(win)
        return result["value"]

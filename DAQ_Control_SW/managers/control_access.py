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
        """Check if main.py is already running, ignoring the current process."""
        try:
            result = subprocess.run(['pgrep', '-f', 'python.*main.py'], capture_output=True, text=True)
            pids = result.stdout.strip().split()
            
            current_pid = str(os.getpid())
            
            other_pids = [pid for pid in pids if pid != current_pid]
            
            return len(other_pids) > 0
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

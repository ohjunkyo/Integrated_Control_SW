# managers/control_access.py
import tkinter as tk
from tkinter import messagebox, simpledialog
import subprocess

# 🔑 [통합 비밀번호 관리] 
# 앞으로 비밀번호를 바꿀 때는 아래의 "root"만 수정하면 전체 창에 적용됩니다.
ADMIN_PASSWORD = "root"

class ControlAccessManager:
    def __init__(self, controller, password=None): 
        self.controller = controller
        self.unlocked = False

    def is_production_running(self):
        """Check if main.py (Production) is already running to prevent hardware collisions."""
        try:
            result = subprocess.run(['pgrep', '-f', 'main.py'], capture_output=True, text=True)
            pids = result.stdout.strip().split()
            return len(pids) > 1
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

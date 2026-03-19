# managers/rotation_control.py
import subprocess
import os
import json
from tkinter import messagebox

class RotationManager:
    def __init__(self, controller):
        self.controller = controller
        
        # [최적화 4] 접속 정보를 외부 파일로 관리
        self.hw_config_file = os.path.join(self.controller.base_dir, "hardware_config.json")
        self.load_hardware_config()

    def load_hardware_config(self):
        """JSON에서 윈도우 설정 로드. 파일이 없으면 기본값으로 생성합니다."""
        # 기본값 (Fallback)
        self.win_user = "skkor"
        self.win_ip = "192.168.10.100" 
        self.win_work_dir = r"Desktop\Eqip"
        self.win_python = r".\venv\Scripts\python.exe"

        if os.path.exists(self.hw_config_file):
            try:
                with open(self.hw_config_file, 'r', encoding='utf-8') as f:
                    cfg = json.load(f)
                    self.win_user = cfg.get("win_user", self.win_user)
                    self.win_ip = cfg.get("win_ip", self.win_ip)
                    self.win_work_dir = cfg.get("win_work_dir", self.win_work_dir)
                    self.win_python = cfg.get("win_python", self.win_python)
            except Exception as e:
                self.controller._log(f"⚠️ Failed to load hardware_config.json: {e}")
        else:
            # 파일이 없으면 현재 가지고 있는 기본값으로 json 파일을 자동 생성합니다.
            try:
                with open(self.hw_config_file, 'w', encoding='utf-8') as f:
                    json.dump({
                        "win_user": self.win_user,
                        "win_ip": self.win_ip,
                        "win_work_dir": self.win_work_dir,
                        "win_python": self.win_python
                    }, f, indent=4)
            except: 
                pass

    def move_rotation(self, dev_num, tilt, rot):
        """Core function to move the Windows equipment"""
        if not self.controller.access_mgr.unlocked:
            messagebox.showwarning("Locked", "Control unlock is required.")
            return

        try:
            tilt_val = float(tilt)
            rot_val = float(rot)
        except ValueError:
            messagebox.showerror("Input Error", "Angles must be entered as numeric values.")
            return

        # 1. Tilt Limit Switch
        if not (-55.0 <= tilt_val <= 55.0):
            error_msg = f"DANGER: The entered Tilt angle ({tilt_val}°) is out of the allowed range (-55° to 55°).\nMovement command cancelled to prevent physical collision."
            self.controller._log(f"🚨 Hardware Protection Activated (Limit Switch): {error_msg}")
            messagebox.showerror("Limit Switch Activated", error_msg)
            return

        # 2. Rotation Limit Switch
        if not (0.0 <= rot_val <= 135.0):
            error_msg = f"DANGER: The entered Rotation angle ({rot_val}°) is out of the allowed range (0° to 135°).\nMovement command cancelled to prevent physical collision and cable twisting."
            self.controller._log(f"🚨 Hardware Protection Activated (Limit Switch): {error_msg}")
            messagebox.showerror("Limit Switch Activated", error_msg)
            return

        config_file = f"config_dev{dev_num}.json"

        cmd = f"cd {self.win_work_dir}; {self.win_python} angle.py {tilt_val} {rot_val} {config_file}"
        ssh_cmd = ["ssh", f"{self.win_user}@{self.win_ip}", cmd]

        self.controller._log(f"Rotation Cmd: {ssh_cmd}")
        self.controller._execute_in_new_terminal(ssh_cmd)

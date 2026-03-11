# managers/rotation_control.py
import subprocess
from tkinter import messagebox

class RotationManager:
    def __init__(self, controller):
        self.controller = controller
        # 윈도우 PC 접속 정보 (실제 환경에 맞게 IP 확인 필요)
        self.win_user = "skkor"
        self.win_ip = "192.168.10.100" 
        self.win_work_dir = r"Desktop\Eqip"
        self.win_python = r".\venv\Scripts\python.exe"

    def move_rotation(self, dev_num, tilt, rot):
        """윈도우 장비를 움직이는 핵심 함수"""
        # [인터록] 제어권 승인 여부 확인
        if not self.controller.access_mgr.unlocked:
            messagebox.showwarning("Locked", "제어권 Unlock이 필요합니다.")
            return

        # 명령어 조립 (윈도우의 angle.py 실행)
        config_file = f"config_dev{dev_num}.json"
        cmd = f"cd {self.win_work_dir}; {self.win_python} angle.py {tilt} {rot} {config_file}"
        ssh_cmd = ["ssh", f"{self.win_user}@{self.win_ip}", cmd]

        self.controller._log(f"Rotation Cmd: {ssh_cmd}")
        # main_test.py에 정의된 새 터미널 실행 함수 호출
        self.controller._execute_in_new_terminal(ssh_cmd)

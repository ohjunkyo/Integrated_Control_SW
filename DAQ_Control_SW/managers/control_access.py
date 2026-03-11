# control_access.py
import tkinter as tk
from tkinter import messagebox, simpledialog
import subprocess

class ControlAccessManager:
    def __init__(self, controller, password="root"):
        self.controller = controller
        self.unlocked = False
        self.password = password

    def is_production_running(self):
        """현재 시스템에서 main.py(Production)가 실행 중인지 확인"""
        try:
            # 리눅스 pgrep 명령어로 main.py 프로세스 검색
            result = subprocess.run(['pgrep', '-f', 'main.py'], capture_output=True, text=True)
            pids = result.stdout.strip().split()
            # 자기 자신 외에 다른 main.py가 있는지 확인
            return len(pids) > 1
        except Exception:
            return False

    def request_unlock(self):
        """비밀번호 확인 및 프로세스 충돌 체크 후 제어권 부여"""
        if self.unlocked:
            self.unlocked = False
            messagebox.showinfo("Lock", "장비 제어권이 다시 잠겼습니다.")
            return True

        # 1. Production 실행 여부 체크
        if self.is_production_running():
            messagebox.showerror("Collision Alert", 
                                 "⚠️ Production (main.py)이 이미 실행 중입니다!\n"
                                 "하드웨어 충돌 방지를 위해 제어권을 얻을 수 없습니다.")
            return False

        # 2. 비밀번호 확인
        pwd = simpledialog.askstring("Security", "Enter Master Password:", show='*')
        
        if pwd == self.password:
            self.unlocked = True
            messagebox.showinfo("Success", "제어권이 활성화되었습니다.")
            return True
        else:
            if pwd is not None:
                messagebox.showerror("Error", "비밀번호가 틀렸습니다.")
            return False

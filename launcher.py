# launcher.py
import tkinter as tk
import tkinter.ttk as ttk  # ttk를 사용하여 더 나은 UI 제공
import subprocess
import os
import sys
import time
from datetime import datetime
from tkinter import messagebox

class AppLauncher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Integrated Control Software Launcher")
        self.geometry("550x550")  # 창 크기 조정

        self.configure(bg='#333333')
        button_font = ("Helvetica", 14, "bold")
        label_font = ("Helvetica", 10)
        status_font = ("Helvetica", 10, "bold")

        # --- 메인 버튼 프레임 ---
        button_frame = tk.Frame(self, bg='#333333')
        button_frame.pack(pady=15, fill=tk.X, padx=20)

        daq_button = tk.Button(button_frame, text="Start DAQ Control", font=button_font, bg="#007ACC", fg="white", padx=20, pady=15, command=self.launch_daq_control)
        daq_button.pack(pady=10, fill=tk.X)

        hv_button = tk.Button(button_frame, text="Start HV Monitor", font=button_font, bg="#5CB85C", fg="white", padx=20, pady=15, command=self.launch_hv_monitor)
        hv_button.pack(pady=10, fill=tk.X)

        vm_button = tk.Button(
            button_frame, text="Start Laser (Windows VM)", font=button_font,
            bg="#f0ad4e", fg="white", padx=20, pady=15,
            command=self.launch_vmware_vm
        )
        vm_button.pack(pady=10, fill=tk.X)

        # --- 상태 정보 프레임 ---
        status_frame = ttk.LabelFrame(self, text="Launcher Status", padding="10")
        status_frame.pack(fill=tk.X, expand=True, padx=20, pady=10)
        
        # --- 1. 시간 표시 ---
        self.start_time = datetime.now()
        self.start_time_str = self.start_time.strftime('%Y-%m-%d %H:%M:%S')
        
        self.current_time_var = tk.StringVar()
        self.elapsed_time_var = tk.StringVar()

        self._create_status_row(status_frame, "Start Time:", self.start_time_str, label_font, status_font)
        self._create_status_row(status_frame, "Current Time:", self.current_time_var, label_font, status_font)
        self._create_status_row(status_frame, "Elapsed Time:", self.elapsed_time_var, label_font, status_font)

        # --- 2. 파일 수정 상태 ---
        ttk.Separator(status_frame, orient='horizontal').pack(fill='x', pady=10)

        self.last_mod_file_var = tk.StringVar()
        self.last_mod_time_var = tk.StringVar()
        
        self._create_status_row(status_frame, "Last Modified File:", self.last_mod_file_var, label_font, status_font)
        self._create_status_row(status_frame, "Modified Time:", self.last_mod_time_var, label_font, status_font)
        
        refresh_button = ttk.Button(status_frame, text="Refresh Status 🔄", command=self.update_file_status)
        refresh_button.pack(pady=10)

        # --- 종료 버튼 ---
        exit_button = tk.Button(self, text="Exit Launcher", font=("Helvetica", 10), bg="#868e96", fg="white", command=self.destroy)
        exit_button.pack(pady=15, padx=20)

        # --- 초기화 실행 ---
        self.update_clock()
        self.update_file_status()

    def _create_status_row(self, parent, label_text, string_var, label_font, status_font):
        """상태 표시에 사용할 행을 만듭니다."""
        row_frame = tk.Frame(parent)
        row_frame.pack(fill=tk.X, pady=2)
        
        label = ttk.Label(row_frame, text=label_text, font=label_font, width=18, anchor="w")
        label.pack(side=tk.LEFT)
        
        if isinstance(string_var, str):
            value_label = ttk.Label(row_frame, text=string_var, font=status_font, anchor="w")
        else:
            value_label = ttk.Label(row_frame, textvariable=string_var, font=status_font, anchor="w")
        value_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

    def get_python_executable(self):
        return sys.executable

    def launch_daq_control(self):
        print("Launching DAQ Control...")
        python_exe = self.get_python_executable()
        script_path = os.path.join("DAQ_Control_SW", "main.py")
        subprocess.Popen([python_exe, script_path])

    def launch_hv_monitor(self):
        print("Launching HV Monitor...")
        python_exe = self.get_python_executable()
        script_path = os.path.join("HV_Control_SW", "monitoring_app.py")
        config_path = os.path.join("HV_Control_SW", "config_precal.json")
        command = [python_exe, script_path, config_path]
        try:
            subprocess.Popen(command)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to launch HV Monitor:\n{e}")

    def launch_vmware_vm(self):
        vmx_path = "/home/precalkor/vmware/Windows11/Windows 11 x64.vmx"
        try:
            print(f"Starting VM: {vmx_path}")
            command = ['vmrun', 'start', vmx_path]
            subprocess.Popen(command)
        except FileNotFoundError:
            messagebox.showerror("Error", "'vmrun' command not found.\nPlease ensure VMware Workstation is installed and 'vmrun' is in your system's PATH.")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to start VM: {e}")

    # --- [새로 추가된 함수] ---
    
    def update_clock(self):
        """1초마다 현재 시간과 경과 시간을 업데이트합니다."""
        now = datetime.now()
        self.current_time_var.set(now.strftime('%Y-%m-%d %H:%M:%S'))
        
        elapsed = now - self.start_time
        # 초 단위로 변환 후, 시:분:초로 포맷팅
        total_seconds = int(elapsed.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        self.elapsed_time_var.set(f"{hours:02}:{minutes:02}:{seconds:02}")
        
        # 1초 후에 이 함수를 다시 호출
        self.after(1000, self.update_clock)

    def find_most_recent_file(self, *dirs_to_scan):
        """지정된 디렉토리에서 가장 최근에 수정된 파일을 찾습니다."""
        most_recent_file = None
        max_mtime = 0
        
        # 무시할 디렉토리 및 확장자
        IGNORE_DIRS = {'__pycache__', '.git', 'venv', 'icons', 'logs'}
        IGNORE_EXTS = {'.db', '.log', '.png', '.jpg', '.ico', '.sqlite3', '.json'}

        for directory in dirs_to_scan:
            for root, dirs, files in os.walk(directory, topdown=True):
                # 무시할 디렉토리 방문 X
                dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
                
                for file in files:
                    ext = os.path.splitext(file)[1]
                    if ext in IGNORE_EXTS:
                        continue
                        
                    filepath = os.path.join(root, file)
                    try:
                        mtime = os.path.getmtime(filepath)
                        if mtime > max_mtime:
                            max_mtime = mtime
                            most_recent_file = filepath
                    except OSError:
                        continue
                        
        return most_recent_file, max_mtime

    def update_file_status(self):
        """가장 최근에 수정된 파일을 찾아 UI에 표시합니다."""
        print("Refreshing file status...")
        try:
            file_path, mtime = self.find_most_recent_file("DAQ_Control_SW", "HV_Control_SW")
            
            if file_path:
                # 런처 기준의 상대 경로로 표시
                relative_path = os.path.relpath(file_path)
                timestamp_str = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S')
                
                self.last_mod_file_var.set(relative_path)
                self.last_mod_time_var.set(timestamp_str)
            else:
                self.last_mod_file_var.set("No files found.")
                self.last_mod_time_var.set("N/A")
        except Exception as e:
            self.last_mod_file_var.set("Error scanning files.")
            self.last_mod_time_var.set(f"{e}")

if __name__ == "__main__":
    app = AppLauncher()
    app.mainloop()

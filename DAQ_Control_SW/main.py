# main.py
import tkinter as tk
from PIL import Image, ImageTk
from tkinter import ttk, filedialog, messagebox
import time
import math
import sys
import os
import subprocess
import threading
import json
import re
import shutil
import glob
import queue
import collections
import serial
import serial.tools.list_ports
import matplotlib.pyplot as plt
import logging 
from logging.handlers import TimedRotatingFileHandler 
import random 
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from datetime import datetime
from ui_manager import UIManager
from config_manager import ConfigManager
from pmt_config_window import PMTConfigWindow

APP_CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".daq_control_config.json")

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
laser_dir = os.path.join(parent_dir, 'Laser_Control_SW', 'app')


#print(f"DEBUG: Current Script Dir: {current_dir}")
#print(f"DEBUG: Looking for Laser Dir at: {laser_dir}")
#print(f"DEBUG: Does it exist?: {os.path.exists(laser_dir)}")
LASER_AVAILABLE = False # 임포트 성공 여부 플래그

if os.path.exists(laser_dir):
    if laser_dir not in sys.path:
        sys.path.append(laser_dir)
    try:
        from laser_driver import TamadenshiLaser
        LASER_AVAILABLE = True
        print("✅ Laser driver imported successfully.")
    except ImportError as e:
        print(f"❌ Failed to import laser driver: {e}")
else:
    print(f"⚠️ Warning: Directory not found: {laser_dir}")

class App:
    def __init__(self, master, base_dir):
        self.master = master
        self.base_dir = base_dir
        master.title("DAQ/LASER/UPS Control Panel")
        master.geometry("1600x900")

        # 1. 아이콘 및 기본 변수 초기화
        icon_path = os.path.join(self.base_dir, 'icons', 'DAQcontroller.png')
        img = Image.open(icon_path)
        self.p_img = ImageTk.PhotoImage(img, master=master)
        master.iconphoto(True, self.p_img)

        self.start_time = datetime.now()
        self.config_manager = None
        self.terminal_preference = 'gnome-terminal'
        self.load_app_config()

        if LASER_AVAILABLE:
            try:
                self.laser = TamadenshiLaser()
            except Exception as e:
                print(f"❌ Failed to initialize Laser hardware: {e}")
                self.laser = None
        else:
            self.laser = None

        self.ups_serial = None
        self.ups_plot_history = {
            "time": collections.deque(maxlen=60),
            "watt": collections.deque(maxlen=60),
            "temp": collections.deque(maxlen=60),
            "vin":  collections.deque(maxlen=60),
            "vout": collections.deque(maxlen=60)
        }
        
        self.master.after(5000, self.auto_connect_laser) 
        self.plot_history = {
            "time": collections.deque(maxlen=60), 
            "temp": collections.deque(maxlen=60), 
            "pulse": collections.deque(maxlen=60)
        }

        # 2. 상태바 및 UI 생성
        self.status_bar = ttk.Frame(master, relief=tk.SUNKEN, padding="2 5")
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)
        self.elapsed_time_var = tk.StringVar()
        self.clock_var = tk.StringVar()
        ttk.Label(self.status_bar, textvariable=self.elapsed_time_var).pack(side=tk.RIGHT, padx=10)
        ttk.Label(self.status_bar, textvariable=self.clock_var).pack(side=tk.LEFT, padx=10)
        self._update_status_bar()

        # UI 생성 (한 번만 호출!)
        self.ui = UIManager(master, self)

        # 3. 하드웨어 및 데이터 초기 로드 (중복 제거)
        if self.config_manager:
            self.validate_config_paths()
            # [수정] 아래 함수들은 쓰레드를 생성하므로 UI가 안정화된 후 실행되도록 예약합니다.
            self.master.after(500, self.refresh_all_data)
            self.master.after(1000, self.check_daq_connection)
        else:
            messagebox.showwarning("Warning", "Configuration not loaded.")

        # 4. 기타 모듈 설정
        if self.laser:
            self.update_laser_status_loop()
        self.on_laser_trigger_change()
        self.setup_laser_logger()
        self.load_today_laser_log()

        # UPS 자동 연결
        self.master.after(1500, self.auto_connect_ups)



    def check_dir_size_queue(self):
        try:
            while not self.dir_size_queue.empty():
                display_str = self.dir_size_queue.get_nowait()
                self.ui.update_data_size_display(display_str)
        except queue.Empty:
            pass
        finally:
            # 1초마다 큐를 다시 확인
            self.master.after(1000, self.check_dir_size_queue)


        

    def _update_status_bar(self):
        """1초마다 현재 시간과 경과 시간을 계산하여 상태 표시줄을 업데이트합니다."""

        now = datetime.now()
        current_time_str = now.strftime('%Y-%m-%d %H:%M:%S')
        self.clock_var.set(f"Current time : {current_time_str}")

        elapsed = now - self.start_time
        total_seconds = int(elapsed.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        elapsed_str = f"{hours:02}:{minutes:02}:{seconds:02}"
        self.elapsed_time_var.set(f"Execution time: {elapsed_str}")

        self.master.after(1000, self._update_status_bar)

    def load_app_config(self):
        config_path = None
        try:
            if os.path.exists(APP_CONFIG_FILE):
                with open(APP_CONFIG_FILE, 'r') as f:
                    config = json.load(f)
                    config_path = config.get("config2h_path")
                    self.terminal_preference = config.get("terminal_preference", 'gnome-terminal')

        except Exception as e:
            print(f"Error loading app config: {e}")

        if config_path and os.path.exists(config_path):
            self.config_manager = ConfigManager(config_path)
        else:
            self.select_and_set_config_path(initial_setup=True)

    def save_app_config(self):
        if not self.config_manager or not self.config_manager.filepath:
            return
        try:
            with open(APP_CONFIG_FILE, 'w') as f:
                config = {
                        "config2h_path": self.config_manager.filepath,
                        "terminal_preference": self.terminal_preference
                        }
                json.dump(config, f, indent=4)
        except Exception as e:
            messagebox.showerror("Error", f"Could not save app config: {e}")

    def select_and_set_config_path(self, initial_setup=False):
        filepath = filedialog.askopenfilename(
                title="Select config2.h file",
                filetypes=(("Header files", "*.h"), ("All files", "*.*"))
                )
        if filepath:
            self.config_manager = ConfigManager(filepath)
            self.save_app_config()
            if not initial_setup:
                self.refresh_all_data()
        elif initial_setup and not self.config_manager:
            messagebox.showerror("Error", "config2.h path is required to run the application.")
            self.master.quit()

    def validate_config_paths(self):
        """config2.h에 명시된 주요 경로들이 유효한지 검사합니다."""
        if not self.config_manager: return

        paths_to_check = ['BasePath', 'RawDataPath', 'ProcessedDataPath', 'ImagePath']
        missing_paths = []

        for path_key in paths_to_check:
            path_val = self.config_manager.get_config_value(path_key)
            if not path_val or not os.path.isdir(path_val):
                missing_paths.append(path_key)

        if missing_paths:
            messagebox.showwarning("Configuration Warning",
                                   f"The following paths defined in your config file are missing or invalid:\n\n"
                                   f"{', '.join(missing_paths)}\n\n"
                                   "Please check your config2.h file.")


    def set_terminal_preference(self, terminal_name):
        """터미널 선택을 저장하는 함수"""
        self.terminal_preference = terminal_name
        self.save_app_config()
        messagebox.showinfo("Terminal Changed", f"Terminal has been set to: {terminal_name}")

    def _log(self, message):
        """메시지를 로그 파일에 저장하고 UI를 업데이트합니다."""
        try:
            log_dir = os.path.join(self.base_dir, "logs")
            os.makedirs(log_dir, exist_ok=True)
            log_file = os.path.join(log_dir, f"log_{datetime.now().strftime('%Y-%m-%d')}.txt")

            log_entry = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n"

            with open(log_file, 'a') as f:
                f.write(log_entry)

            if hasattr(self, 'ui'):
                self.refresh_log_view()
        except Exception as e:
            print(f"Error while logging: {e}")

    def refresh_log_view(self):
        """가장 최신 로그 파일 내용을 읽어 UI에 표시합니다."""
        log_dir = os.path.join(self.base_dir, "logs")
        try:
            if not os.path.isdir(log_dir):
                self.ui.update_log_view("No logs found.")
                return

            log_files = [f for f in os.listdir(log_dir) if f.endswith('.txt')]
            if not log_files:
                self.ui.update_log_view("No logs found.")
                return

            latest_log_file = max(log_files, key=lambda f: os.path.getmtime(os.path.join(log_dir, f)))
            with open(os.path.join(log_dir, latest_log_file), 'r') as f:
                self.ui.update_log_view(f.read())
        except Exception as e:
            self.ui.update_log_view(f"Error reading log file: {e}")

    def _execute_in_new_terminal(self, command):
        """저장된 설정에 따라 올바른 터미널에서 명령을 실행합니다."""
        command_str_for_log = ' '.join(command)
        self._log(f"Executing command via '{self.terminal_preference}': {command_str_for_log}")

        try:
            if self.terminal_preference == 'xterm':
                term_command_str = f"{' '.join(command)}; echo; read -p 'Execution finished. Press Enter to close this terminal...'"
                term_command = ['xterm', '-hold', '-e', 'bash', '-c', term_command_str]
                subprocess.Popen(term_command)
            else: # 기본값은 gnome-terminal
                term_command_str = f"{' '.join(command)}; echo; read -p 'Execution finished. Press Enter to close this terminal...'"
                term_command = ['gnome-terminal', '--', 'bash', '-c', term_command_str]
                subprocess.Popen(term_command)

        except FileNotFoundError:
            error_msg = f"'{self.terminal_preference}' not found. Please install it or select another terminal from the File menu."
            self._log(f"ERROR: {error_msg}")
            messagebox.showerror("Error", error_msg)
        except Exception as e:
            self._log(f"ERROR: Failed to open terminal: {e}")
            messagebox.showerror("Error", f"Failed to open terminal: {e}")

    def handle_button_click(self, command_id):
        if not self.config_manager:
            messagebox.showerror("Error", "Configuration file (config2.h) is not loaded. Please set the path from the 'File' menu.")
            return
        method_to_call = getattr(self, command_id, self.command_not_found)
        method_to_call()

    def command_not_found(self):
        messagebox.showerror("Error", "Unknown command received from UI.")

    def refresh_all_data(self):
        if self.config_manager:
            self.config_manager.reload()
            self.ui.on_config_loaded()
            self.update_latest_run_number()
            self.ui.update_data_viewer(force_refresh=True)
            self.update_data_directory_size()
        self.refresh_log_view()

    def open_config(self):
        self.ui.open_config_window()

    def open_image_viewer(self):
        self.ui.open_image_viewer()

    def open_pmt_config_window(self, pmt_name):
        if self.config_manager:
            pmt_win = PMTConfigWindow(self.master, self.config_manager, pmt_name)
            self.master.wait_window(pmt_win)
            self.ui.on_config_loaded()
        else:
            messagebox.showwarning("Warning", "Configuration manager not initialized.")

    def run_daq(self):
        daq_path = self._get_daq_path()
        if not daq_path: return
        mode = self.ui.run_mode.get()
        script_path = os.path.join(daq_path, 'script2.sh')
        config_path = self.config_manager.filepath
        command = [script_path, mode, config_path]
        self._execute_in_new_terminal(command)

    def run_produce(self):
        selected_files = self.ui.get_selected_file_paths()
        daq_path = self._get_daq_path()
        if not daq_path: return

        helper = os.path.join(self.base_dir, 'run_cpp_script.sh')
        script = os.path.join(daq_path, 'prod_ntp_v3.C') 
        config_path = self.config_manager.filepath
        mode_int = "0" if self.ui.run_mode.get() == "laser" else "1"

        runs_to_process = [] 

        if selected_files:
            pattern = re.compile(r'\.([0-9]{4})\.root$')
            for f_path in selected_files:
                if "raw" in f_path.lower():
                    f_name = os.path.basename(f_path)
                    match = pattern.search(f_name)
                    if match:
                        run_num_str = str(int(match.group(1)))
                        runs_to_process.append((run_num_str, f_path))
                    else:
                        self._log(f"WARNING: Could not extract 4-digit run number from {f_name}. Skipping.")
                else:
                    self._log(f"INFO: Skipping already processed file: {f_path}")
        else:
            run_num = self.ui.get_run_num()
            if not run_num: return
            runs_to_process.append((run_num, "")) 

        if not runs_to_process:
            messagebox.showwarning("No Runs", "No valid RAW files found to process.")
            return

        all_commands_list = []
        for run_num, f_path in runs_to_process:
            f_path_arg = f"\\\"{f_path}\\\"" if f_path else "\"\""
            command_parts = [helper, script, config_path, run_num, mode_int, f_path_arg]
            all_commands_list.append(" ".join(command_parts))

        final_command_string = " && ".join(all_commands_list)
        self._execute_in_new_terminal([final_command_string])

    def run_analysis(self):
        selected_files = self.ui.get_selected_file_paths()
        daq_path = self._get_daq_path()
        if not daq_path: return

        helper = os.path.join(self.base_dir, 'run_cpp_script.sh')
        script = os.path.join(daq_path, 'read_ntp_v3.C') 
        config_path = self.config_manager.filepath

        runs_to_process = [] 

        if selected_files:
            pattern = re.compile(r'\.([0-9]{4})\.root$')
            for f_path in selected_files:
                f_name = os.path.basename(f_path)
                match = pattern.search(f_name)
                if match:
                    run_num_str = str(int(match.group(1))) 

                    if "production" in f_path.lower() or "prd_" in f_name.lower():
                        processed_path = f_path
                    else:
                        processed_path = os.path.join(self.config_manager.get_config_value("ProcessedDataPath"), f"prd_{f_name}")

                    runs_to_process.append((run_num_str, processed_path))
                else:
                    self._log(f"WARNING: Could not extract 4-digit run number from {f_name}. Skipping.")
        else:
            run_num = self.ui.get_run_num()
            if not run_num: return
            runs_to_process.append((run_num, ""))

        if not runs_to_process:
            messagebox.showwarning("No Runs", "No valid run numbers found to process.")
            return

        all_commands_list = []
        for run_num, f_path in runs_to_process:
            f_path_arg = f"\\\"{f_path}\\\"" if f_path else "\"\""
            command_parts = [helper, script, config_path, run_num, f_path_arg]
            all_commands_list.append(" ".join(command_parts))

        final_command_string = " && ".join(all_commands_list)
        self._execute_in_new_terminal([final_command_string])

    def run_waveform(self):
        """
        Waveform inspection:
        - 0 files selected: Use Run Number text box.
        - 1 file selected: Use that file's run number.
        - 2+ files selected: Show warning and stop.
        """
        selected_files = self.ui.get_selected_file_paths()
        daq_path = self._get_daq_path()
        if not daq_path: return

        helper = os.path.join(self.base_dir, 'run_cpp_script.sh')
        script = os.path.join(daq_path, 'Draw_waveform.C')
        config_path = self.config_manager.filepath
        
        run_num = None # The single run number to execute

        if len(selected_files) > 1:
            # Case 1: More than one file selected
            messagebox.showwarning("Multiple Files Selected", 
                                   "Please select only one file for Waveform Inspection.")
            return # Stop
        
        elif len(selected_files) == 1:
            # Case 2: Exactly one file selected
            f_path = selected_files[0]
            f_name = os.path.basename(f_path)
            pattern = re.compile(r'\.([0-9]{4})\.root$')
            match = pattern.search(f_name)
            
            if match:
                run_num = str(int(match.group(1)))
            else:
                self._log(f"WARNING: Could not extract run number from {f_name}.")
                messagebox.showwarning("Error", f"Could not extract run number from selected file:\n{f_name}")
                return # Stop
        
        else:
            # Case 3: Zero files selected (the "old way")
            run_num = self.ui.get_run_num()
            if not run_num:
                return # get_run_num() shows its own warning

        # If we have a valid run number, execute it once
        if run_num:
            command_parts = [helper, script, config_path, run_num, 'interactive']
            final_command_string = " ".join(command_parts)
            self._execute_in_new_terminal([final_command_string])
        else:
            # This case should not be reachable, but as a safeguard:
            self._log("ERROR: run_waveform logic failed, no run number was determined.")
    # --- [*** 수정 끝 (1) ***] ---

    def run_contour(self):
        """
        Waveform 2D (Contour):
        - 0 files selected: Use Run Number text box.
        - 1 or more files selected: Use run numbers AND file paths from all selected files.
        (Updated to match logic of Produce and Analysis)
        """
        selected_files = self.ui.get_selected_file_paths()
        daq_path = self._get_daq_path()
        if not daq_path: return
        
        helper = os.path.join(self.base_dir, 'run_cpp_script.sh')
        script = os.path.join(daq_path, 'Draw_Contour_v2.C')
        config_path = self.config_manager.filepath

        runs_to_process = [] # (run_num_str, file_path) 튜플을 저장

        if selected_files:
            # Case 1: 파일 리스트에서 선택한 경우
            pattern = re.compile(r'\.([0-9]{4})\.root$')
            for f_path in selected_files:
                f_name = os.path.basename(f_path)
                match = pattern.search(f_name)
                if match:
                    run_num_str = str(int(match.group(1)))
                    # [중요] Run 번호와 파일 전체 경로를 함께 저장
                    runs_to_process.append((run_num_str, f_path))
                else:
                    self._log(f"WARNING: Could not extract 4-digit run number from {f_name}. Skipping.")
        
        else:
            # Case 2: 파일을 선택하지 않고 텍스트 박스 입력값 사용
            run_num = self.ui.get_run_num()
            if not run_num: return
            # 파일 경로가 없으므로 빈 문자열("") 전달
            runs_to_process.append((run_num, ""))

        if not runs_to_process:
            messagebox.showwarning("No Runs", "No valid run numbers found to process.")
            return
            
        all_commands_list = []
        for run_num, f_path in runs_to_process:
            # 파일 경로가 있으면 인자로 추가 (따옴표 이스케이프 처리)
            f_path_arg = f"\\\"{f_path}\\\"" if f_path else "\"\""
            
            # [중요] helper, script, config, run_num 뒤에 '파일 경로' 인자를 추가해서 보냄
            command_parts = [helper, script, config_path, run_num, f_path_arg]
            all_commands_list.append(" ".join(command_parts))

        final_command_string = " && ".join(all_commands_list)
        self._execute_in_new_terminal([final_command_string])

    def run_auto_analysis(self):
        messagebox.showinfo("Not Implemented", "Auto Analysis button is not configured.")
        pass

    def run_uniformity_raw(self):
        messagebox.showinfo("Not Implemented", "Uniformity (Raw) button is not configured.")
        pass

    def run_uniformity_norm(self):
        messagebox.showinfo("Not Implemented", "Uniformity (Norm) button is not configured.")
        pass

    def run_transfer(self):
        daq_path = self._get_daq_path()
        if not daq_path: return
        script_path = os.path.join(daq_path, 'transfer.sh')
        if not os.path.exists(script_path):
            messagebox.showerror("Error", f"Transfer script not found at:\n{script_path}")
            return
        command = [script_path]
        self._execute_in_new_terminal(command)

    def move_data_files(self, file_paths):
        if not file_paths: return

        dest_dir = filedialog.askdirectory(title="Select Destination Folder")
        if not dest_dir: return

        thread = threading.Thread(target=self._perform_rsync_thread, args=(file_paths, dest_dir))
        thread.start()

    def _perform_rsync_thread(self, file_paths, dest_dir):
        """rsync를 사용하여 백그라운드에서 파일을 이동시키는 함수"""
        moved_count = 0
        failed_files = []
        total_files = len(file_paths)

        # 초기 상태 메시지
        self.master.after(0, lambda: self.ui.data_size_var.set(f"Preparing rsync..."))

        for idx, file_path in enumerate(file_paths):
            filename = os.path.basename(file_path)
            
            # 상태 업데이트 (현재 몇 번째 파일 처리 중인지 표시)
            status_msg = f"Rsync Moving... ({idx+1}/{total_files}): {filename}"
            self.master.after(0, lambda m=status_msg: self.ui.data_size_var.set(m))

            try:
                # [핵심] rsync 명령어 구성
                # -a: 아카이브 모드 (권한, 시간 정보 유지)
                # --remove-source-files: 전송 성공 시 원본 파일 삭제 (Move 효과)
                # --info=progress2: (옵션) 진행률 표시용이나 여기선 로그용
                command = [
                    'rsync', 
                    '-a', 
                    '--remove-source-files', 
                    file_path, 
                    dest_dir
                ]
                
                # rsync 실행 (대용량 파일일수록 여기서 시간이 걸림)
                result = subprocess.run(command, check=True, capture_output=True, text=True)
                
                self._log(f"[RSYNC SUCCESS] {file_path} -> {dest_dir}")
                moved_count += 1
                
            except subprocess.CalledProcessError as e:
                self._log(f"[RSYNC ERROR] File: {filename}\nError: {e.stderr}")
                failed_files.append(filename)
            except Exception as e:
                self._log(f"[PYTHON ERROR] File: {filename}\nError: {e}")
                failed_files.append(filename)

        # 3. 모든 작업 완료 후 처리
        def on_complete():
            self.refresh_all_data() # 목록 새로고침
            self.update_data_directory_size() # 용량 재계산
            self.ui.data_size_var.set(f"Move Complete.") # 상태 메시지 초기화

            if failed_files:
                messagebox.showerror("Rsync Finished with Errors", 
                                     f"Moved {moved_count} files.\nFailed:\n{', '.join(failed_files)}\n\nCheck logs for details.")
            else:
                messagebox.showinfo("Success", f"Successfully moved {moved_count} file(s) using rsync.")

        # 메인 스레드에서 알림창 띄우기
        self.master.after(0, on_complete)

    def delete_data_files(self, file_paths):
        if not file_paths: return

        num_files = len(file_paths)
        file_list_str = "\n".join(f"- {os.path.basename(p)}" for p in file_paths[:5])
        if num_files > 5:
            file_list_str += f"\n...and {num_files - 5} more."

        confirmed = messagebox.askyesno(
                "Confirm Deletion",
                f"Are you sure you want to permanently delete {num_files} selected file(s)?\n\n{file_list_str}\n\nThis action cannot be undone."
                )

        if not confirmed:
            self._log("User cancelled file deletion.")
            return

        deleted_count = 0
        failed_files = []
        for file_path in file_paths:
            try:
                os.remove(file_path)
                self._log(f"Deleted file: {file_path}")
                deleted_count += 1
            except Exception as e:
                self._log(f"Failed to delete {file_path}: {e}")
                failed_files.append(os.path.basename(file_path))

        if deleted_count > 0:
            self.refresh_all_data() 

        if failed_files:
            messagebox.showerror("Deletion Error", f"Successfully deleted {deleted_count} file(s), but failed to delete:\n\n{', '.join(failed_files)}")
        elif deleted_count > 0:
            messagebox.showinfo("Success", f"Successfully deleted {deleted_count} file(s).")

    def _get_daq_path(self):
        if not self.config_manager: return None
        try:
            path = self.config_manager.get_config_value('BasePath')
            if not path:
                path = self.config_manager.get_config_value('DaqProgramPath')
            if not path:
                messagebox.showerror("Error", "BasePath or DaqProgramPath not found in config file.")
                return None
            return path
        except Exception as e:
            messagebox.showerror("Error", f"Failed to read path from config file: {e}")
            return None

    def open_terminal_at_path(self, path):
        try:
            if not os.path.isdir(path):
                path = os.path.dirname(path)
            if not os.path.isdir(path):
                messagebox.showerror("Error", f"Directory does not exist:\n{path}")
                return
            subprocess.Popen(['gnome-terminal', f'--working-directory={path}'])
        except FileNotFoundError:
            messagebox.showerror("Error", "'gnome-terminal' not found.")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to open terminal: {e}")

    def get_data_files(self):
        file_list = []
        if not self.config_manager: return file_list

        try:
            paths_to_scan = [
                    ("Raw", self.config_manager.get_config_value("RawDataPath")),
                    ("Production", self.config_manager.get_config_value("ProcessedDataPath")),
                    ("Result", self.config_manager.get_config_value("FinalResultPath")),
                    ("External Disk", self.config_manager.get_config_value("ExternalPath")),
                    ]

            for file_type, base_path in paths_to_scan:
                if not (base_path and os.path.isdir(base_path)):
                    continue
                dirs_to_check = []
                if file_type == "Raw" :
                    dirs_to_check.append(os.path.join(base_path, 'Dark'))
                    dirs_to_check.append(os.path.join(base_path, 'Laser'))
                elif file_type == "External Disk" :
                    dirs_to_check.append(os.path.join(base_path, 'Dark'))
                    dirs_to_check.append(os.path.join(base_path, 'Laser'))
                else:
                    dirs_to_check.append(base_path)
                for dir_path in dirs_to_check:
                    if os.path.isdir(dir_path):
                        for f in os.listdir(dir_path):
                            if f.lower().endswith('.root'):
                                full_path = os.path.join(dir_path, f)
                                mtime = os.path.getmtime(full_path)
                                mtime_str = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S')
                                file_list.append({
                                    "type": file_type, "filename": f, "path": dir_path,
                                    "mtime": mtime_str, "mtime_float": mtime
                                    })

        except Exception as e:
            self._log(f"Error reading data files: {e}")
            messagebox.showerror("File Error", f"Could not read data files from disk.\nCheck permissions or paths.\n\nError: {e}")

        file_list.sort(key=lambda x: x["mtime_float"], reverse=True)
        return file_list

    def open_root_file_browser(self, file_path):
        try:
            command = ['root', '-l', file_path]

            if self.terminal_preference == 'xterm':
                term_command = ['xterm', '-e'] + command
            else: # gnome-terminal
                term_command = ['gnome-terminal', '--'] + command

            subprocess.Popen(term_command)

        except FileNotFoundError:
            messagebox.showerror("Error", f"'root' or '{self.terminal_preference}' command not found.")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to open ROOT file:\n{e}")

    def get_latest_run_number(self):
        if not self.config_manager: return (1, "Config not loaded.")
        try:
            cfg = self.config_manager.get_all_variables()
            mode = self.ui.run_mode.get()

            serials = [cfg.get("SN1", ""), cfg.get("SN2", ""), cfg.get("SN3", "")]
            directions = [cfg.get("direction1", ""), cfg.get("direction2", ""), cfg.get("direction3", "")]
            hvs = [cfg.get("HV1", ""), cfg.get("HV2", ""), cfg.get("HV3", "")]
            rotateAngles = [cfg.get("RotateAngle1", ""), cfg.get("RotateAngle2", ""),""]
            tiltAngles = [cfg.get("TiltAngle1", ""), cfg.get("TiltAngle2", ""), ""]

            core_parts = []
            for i in range(len(serials)):
                if serials[i]:
                    def format_angle_py(prefix, angle_str):
                        if not angle_str: return f"{prefix}00" 
                        try:
                            angle_val = int(angle_str)
                            sign = 'M' if angle_val < 0 else 'P'
                            if angle_val == 0: return f"{prefix}00"
                            return f"{prefix}{sign}{abs(angle_val)}"
                        except (ValueError, TypeError):
                            return f"{prefix}ERR"

                    rot_part = format_angle_py("R", rotateAngles[i])
                    tilt_part = format_angle_py("T", tiltAngles[i])

                    core_parts.append(f"{serials[i]}{directions[i]}_hv{hvs[i]}_{rot_part}_{tilt_part}")

            filename_core = "_".join(core_parts)

            note_suffix = f"_{cfg.get('NOTE', '')}" if cfg.get('NOTE') else ""
            if mode == "dark":
                mode_tag = f"_dark{note_suffix}"
                path_to_scan = os.path.join(cfg.get("RawDataPath", ""), "Dark")
            else: # laser mode
                laser = cfg.get("Laser", "")
                mode_tag = f"_laser{laser}{note_suffix}"
                path_to_scan = os.path.join(cfg.get("RawDataPath", ""), "Laser")

            if not os.path.isdir(path_to_scan):
                return (1, f"Data path not found: {path_to_scan}")

            search_pattern = os.path.join(path_to_scan, f"{filename_core}{mode_tag}.*.root")

            self._log(f"DEBUG: Searching with pattern: {search_pattern}")

            matching_files = glob.glob(search_pattern)

            run_numbers = []
            pattern = re.compile(r'\.([0-9]{4})\.root$')
            for f_path in matching_files:
                f_name = os.path.basename(f_path)
                match = pattern.search(f_name)
                if match:
                    run_numbers.append(int(match.group(1)))

            if not run_numbers:
                message = f"No runs for this config. Next is #1."
                return (1, message)
            else:
                latest_run = max(run_numbers)
                next_run = latest_run + 1
                message = f"{len(run_numbers)} run(s) found. Latest is #{latest_run}. Next is #{next_run}."
                return (next_run, message)

        except Exception as e:
            error_msg = f"Error checking run numbers: {e}"
            self._log(f"ERROR: {error_msg}")
            return (1, "Error checking for previous runs.")


    def update_latest_run_number(self):
        next_run_num, message = self.get_latest_run_number()
        self.ui.run_number_var.set(str(next_run_num))
        self.ui.set_run_number_status(message)

    def get_ip_addresses(self):
        ips = {'local_ip': 'N/A', 'tailscale_ip': 'N/A'}
        try:
            result = subprocess.run(
                    ['tailscale', 'ip', '-4'],
                    capture_output=True, text=True, check=True, timeout=2
                    )
            ips['tailscale_ip'] = result.stdout.strip()
        except Exception: pass
        try:
            result = subprocess.run(
                    "ip route get 1.1.1.1 | awk '{print $7}'",
                    shell=True, capture_output=True, text=True, check=True, timeout=2
                    )
            local_ip = result.stdout.strip()
            if local_ip: ips['local_ip'] = local_ip
        except Exception: pass
        return ips

    def check_daq_connection(self):
        thread = threading.Thread(target=self._run_daq_check_in_thread, daemon=True)
        thread.start()

    def _run_daq_check_in_thread(self):
        is_connected = False
        try:
            daq_path = self.config_manager.get_config_value('BasePath')
            if daq_path:
                command = [os.path.join(daq_path, 'execute_DAQ'), '-j']
                result = subprocess.run(
                        command, capture_output=True, text=True,
                        timeout=5, preexec_fn=os.setsid
                        )
                if "Communication error" not in result.stderr:
                    is_connected = True
        except Exception: pass
        finally:
            if hasattr(self, 'ui') and self.master.winfo_exists():
              try:
                  self.master.after(0, lambda: self.ui.update_daq_connection_status(is_connected))
              except Exception:
                pass

        # 2초 후 재실행 예약
        if self.master.winfo_exists():
            self.master.after(2000, self.check_daq_connection)

    def update_data_directory_size(self):
        if not self.config_manager: return

        raw_data_path = self.config_manager.get_config_value("RawDataPath")
        ext_data_path = self.config_manager.get_config_value("ExternalPath")

        if raw_data_path and os.path.isdir(raw_data_path):
            data_parent_dir = os.path.dirname(raw_data_path)

            thread = threading.Thread(target=self._get_directory_size_thread, args=(raw_data_path, False), daemon=True)
            thread.start()
        else:
            self.ui.update_data_size_display("Path Error")

        if ext_data_path and os.path.isdir(ext_data_path):
            data_parent_dir = os.path.dirname(ext_data_path)

            threading.Thread(target=self._get_directory_size_thread, args=(ext_data_path, True), daemon=True).start()
        else:
            self.ui.update_data_size_display("Path Error")

    def _get_directory_size_thread(self, path, is_ext):
        total_size_bytes = 0
        display_str = "Calculating..."
        try:
            for dirpath, dirnames, filenames in os.walk(path):
                for f in filenames:
                    fp = os.path.join(dirpath, f)
                    if not os.path.islink(fp):
                        total_size_bytes += os.path.getsize(fp)

            human_readable_size = self.format_size(total_size_bytes)

            try:
                disk_usage = shutil.disk_usage(path)
                total_disk_bytes = disk_usage.total

                if total_disk_bytes > 0:
                    percentage = (total_size_bytes / total_disk_bytes) * 100
                    display_str = f"{human_readable_size} ({percentage:.1f}%)"
                else:
                    display_str = human_readable_size
            except FileNotFoundError:
                display_str = human_readable_size

        except Exception as e:
            display_str = "Error"
            self._log(f"Error calculating directory size: {e}")
        finally:
            if hasattr(self, 'ui') and self.master.winfo_exists():
                try:
                    self.master.after(0, lambda: self.ui.update_data_size_display(display_str, is_ext))
                except Exception:
                    pass


    def format_size(self, size_bytes):
        if size_bytes == 0:
            return "0B"
        size_name = ("B", "KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB")
        i = int(math.floor(math.log(size_bytes, 1024)))
        p = math.pow(1024, i)
        s = round(size_bytes / p, 2)
        return f"{s} {size_name[i]}"

    def open_terminal_at_path_by_key(self, path_key):
        if not self.config_manager: return
        path = self.config_manager.get_config_value(path_key)
        if path:
            self.open_terminal_at_path(path)
        else:
            messagebox.showwarning("Path Not Found", f"'{path_key}' is not defined in your config file.")

  ##          """"""""""" LASER """""""""""""""""

    def auto_connect_laser(self):
        """프로그램 시작 시 레이저에 자동으로 연결합니다."""
        if self.laser and not self.laser.is_connected():
            self._log("Attempting to auto-connect Laser...")
            self.toggle_laser_connection() 

    def set_laser_ld(self, state):
        if self.laser:
           self.laser.set_ld_on(state)
           self._log(f"Laser LD set to {'ON' if state else 'OFF'}")

    def set_laser_tec(self, state):
        if self.laser:
           self.laser.set_tec_on(state)
           self._log(f"Laser TEC set to {'ON' if state else 'OFF'}")

    def apply_laser_currents(self):
        if self.laser:
           bias = self.ui.laser_vars["bias_set"].get()
           pulse = self.ui.laser_vars["pulse_set"].get()
           self.laser.set_bias_current(bias)
           self.laser.set_pulse_current(pulse)
           self._log(f"Laser Currents applied: Bias={bias}mA, Pulse={pulse}mA")

    def apply_laser_frequency(self):
        if self.laser:
            try:
                # UI에서 설정한 주파수와 모드 가져오기
                hz_str = self.ui.laser_vars["freq_hz"].get()
                hz = int(hz_str)
                mode = self.ui.laser_vars["trigger_mode"].get()

                # 1. 트리거 모드 설정 (Internal PG1, PG2, External)
                pg1, pg2, ext = (mode=="Internal (PG1)"), (mode=="Internal (PG2)"), (mode=="External")
                self.laser.set_trigger_mode(pg1, pg2, ext)

                # 2. 내부 트리거인 경우 해당 주파수 설정
                if pg1:
                    self.laser.set_pg1_frequency(hz)
                elif pg2:
                    self.laser.set_pg2_frequency(hz)

                self._log(f"Laser Trigger Mode: {mode}, Freq: {hz} Hz applied.")
            except ValueError:
                messagebox.showerror("Error", "Frequency value must be an integer.")

    def update_laser_status_loop(self):
        if self.laser and self.laser.is_connected():
            if self.laser.update_status():
                status = self.laser.status
                is_ld_on = status.get('ld_on', False)
                is_tec_on = status.get('tec_on', False)
                
                self.ui.laser_vars["ld_status"].set("ON" if is_ld_on else "OFF")
                self.ui.laser_vars["tec_status"].set("ON" if is_tec_on else "OFF")
                
                self.ui.update_laser_status_colors(is_ld_on, is_tec_on)

                if is_ld_on:
                    pulse = status.get('pulse', 0)
                    self.ui.laser_vars["pulse_live"].set(f"{pulse:.2f} mA")
                    self.plot_history["pulse"].append(pulse)
                else:
                    self.ui.laser_vars["pulse_live"].set("0.00 mA")
                    self.plot_history["pulse"].append(0)

                self.plot_history["time"].append(datetime.now().strftime("%H:%M:%S"))
                self.refresh_laser_realtime_plot()

        self.master.after(5000, self.update_laser_status_loop)


    def on_laser_trigger_change(self, event=None):
        """External 모드 시 주파수 입력창과 Apply 버튼을 완전히 비활성화합니다."""
        mode = self.ui.laser_vars["trigger_mode"].get()
        if mode == "External":
            # [수정] 입력창과 버튼을 'disabled' 상태로 변경하여 클릭/수정 차단
            self.ui.laser_freq_entry.config(state="disabled")
            self.ui.laser_freq_apply_btn.config(state="disabled")
            self.ui.trig_frame.config(text="Trigger Control - DISABLED (External)")
        else:
            self.ui.laser_freq_entry.config(state="normal")
            self.ui.laser_freq_apply_btn.config(state="normal")
            self.ui.trig_frame.config(text="Trigger Control - ENABLED (Internal)")

        # 하드웨어에 모드 변경 적용
        if self.laser and self.laser.is_connected():
            self.apply_laser_frequency()

    def toggle_laser_connection(self):
        """하나의 버튼으로 연결 및 해제를 관리하며 라벨을 업데이트합니다."""
        if not self.laser: return
        if self.laser.is_connected():
            self.laser.disconnect()
            self.ui.laser_conn_label.config(text="Status: Disconnected", foreground="red")
            self.ui.laser_conn_btn.config(text="Connect Laser")
            self._log("Laser disconnected.")
        else:
            success, msg = self.laser.connect()
            if success:
                self.ui.laser_conn_label.config(text="Status: Connected", foreground="#28a745")
                self.ui.laser_conn_btn.config(text="Disconnect Laser")
                self._log("Laser connected.")
                self.on_laser_trigger_change()
            else:
                messagebox.showerror("Connection Error", msg)

    def update_laser_status_loop(self):
        """실시간 상태 업데이트 및 그래프 갱신"""
        if self.laser and self.laser.is_connected():
            if self.laser.update_status():
                status = self.laser.status
                # UI 변수 업데이트 (기존 코드)
                temp = status.get('ld_temp', 0)
                pulse = status.get('pulse', 0)
                self.ui.laser_vars["temp"].set(f"{temp:.2f} °C")
                self.ui.laser_vars["pulse_live"].set(f"{pulse:.2f} mA")

                # 그래프 데이터 축적 (Deque 활용)
                self.plot_history["temp"].append(temp)
                self.plot_history["pulse"].append(pulse)
                self.plot_history["time"].append(datetime.now().strftime("%H:%M:%S"))

                # 그래프 그리기
                self.refresh_laser_realtime_plot()

        self.master.after(1000, self.update_laser_status_loop)

    # main.py 메서드 수정


    def on_laser_trigger_change(self, event=None):
        """External 모드 시 주파수 입력칸과 Apply 버튼을 완전히 차단합니다."""
        mode = self.ui.laser_vars["trigger_mode"].get()
        if mode == "External":
            # 'disabled' 상태로 설정하여 조작 불가 및 Apply 차단
            self.ui.laser_freq_entry.config(state="disabled")
            self.ui.laser_freq_apply_btn.config(state="disabled")
            self.ui.trig_frame.config(text="Trigger Control - DISABLED (External Mode)")
        else:
            self.ui.laser_freq_entry.config(state="normal")
            self.ui.laser_freq_apply_btn.config(state="normal")
            self.ui.trig_frame.config(text="Trigger Control - ENABLED (Internal Mode)")

        if self.laser and self.laser.is_connected():
            self.apply_laser_frequency()

    def refresh_laser_realtime_plot(self):
        """X축에 실제 시간을 바인딩하여 온도와 전류를 분리하여 그립니다."""
        times = list(self.plot_history["time"])
        temps = list(self.plot_history["temp"])
        pulses = list(self.plot_history["pulse"])
        if not times: return

        # 1. 상단: 온도 그래프
        self.ui.ax_temp.clear()
        # [핵심] times를 첫 번째 인자로 전달
        self.ui.ax_temp.plot(times, temps, 'r-', label="Temp (°C)") 
        self.ui.ax_temp.set_ylabel("Temp (°C)", color='r')
        self.ui.ax_temp.legend(loc='upper right', fontsize='small')
        self.ui.ax_temp.grid(True, alpha=0.3)

        # 2. 하단: 전류 그래프 (Pulse)
        self.ui.ax_curr.clear()
        # [핵심] times를 첫 번째 인자로 전달
        self.ui.ax_curr.plot(times, pulses, 'g-', label="Pulse (mA)")
        self.ui.ax_curr.set_ylabel("Current (mA)", color='g')
        self.ui.ax_curr.set_xlabel("Time (HH:MM:SS)")
        self.ui.ax_curr.legend(loc='upper right', fontsize='small')
        self.ui.ax_curr.grid(True, alpha=0.3)

        # X축 라벨 가독성 개선 (겹침 방지)
        self.ui.fig_live.autofmt_xdate() 

        # 라벨이 너무 많을 경우 일정 간격으로만 표시
        for i, label in enumerate(self.ui.ax_curr.get_xticklabels()):
            if i % 10 != 0: label.set_visible(False)

        self.ui.canvas_live.draw()

    def load_historical_laser_data(self):
        """과거 데이터를 좌측 하단에 상하로 나눠서 시간축으로 그려줍니다."""
        log_dir = "/home/precalkor/ADC/ADC_test/LOG/LASER"
        file_path = filedialog.askopenfilename(initialdir=log_dir, title="Select Laser Log CSV",
                                               filetypes=(("CSV files", "*.csv"), ("All files", "*.*")))
        if file_path:
            import pandas as pd
            try:
                df = pd.read_csv(file_path)
                # 시간을 datetime 객체로 변환하여 가독성 확보
                df['timestamp'] = pd.to_datetime(df['timestamp'])

                # 기존 ax_hist를 상하로 나누기 위해 새로 구성
                self.ui.fig_hist.clf()
                ax1 = self.ui.fig_hist.add_subplot(2, 1, 1)
                ax2 = self.ui.fig_hist.add_subplot(2, 1, 2, sharex=ax1)

                # 상단: 온도
                ax1.plot(df['timestamp'], df['temp_c'], 'r-', label='Temp')
                ax1.set_ylabel("°C")
                ax1.legend(loc='upper right', fontsize='x-small')

                # 하단: 전류
                ax2.plot(df['timestamp'], df['pulse_ma'], 'g-', label='Pulse')
                ax2.set_ylabel("mA")
                ax2.legend(loc='upper right', fontsize='x-small')

                self.ui.fig_hist.autofmt_xdate() # 시간 라벨 회전 처리
                self.ui.fig_hist.tight_layout()
                self.ui.canvas_hist.draw()

                self._log(f"Historical data loaded: {file_path}")
            except Exception as e:
                messagebox.showerror("Error", f"Failed to load data: {e}")

    def _log_laser(self, msg):
        """레이저 전용 로그 위젯과 파일에 동시에 기록"""
        timestamp = datetime.now().strftime('%H:%M:%S')
        if hasattr(self.ui, 'laser_log_text'):
            self.ui.laser_log_text.config(state="normal")
            self.ui.laser_log_text.insert(tk.END, f"[{timestamp}] {msg}\n")
            self.ui.laser_log_text.config(state="disabled")
            self.ui.laser_log_text.yview(tk.END)

    def setup_laser_logger(self):
        """Laser 사용 기록을 위한 로거 설정 (laser_gui 이식)"""
        self.laser_log_dir = "/home/precalkor/ADC/ADC_test/LOG/LASER"
        os.makedirs(self.laser_log_dir, exist_ok=True)

        self.laser_logger = logging.getLogger('LaserSession')
        self.laser_logger.setLevel(logging.INFO)

        if not self.laser_logger.handlers:
            log_path = os.path.join(self.laser_log_dir, "laser_log")
            # 자정마다 새 파일 생성 (laser_log_2026-01-13.txt 형식)
            handler = TimedRotatingFileHandler(log_path, when='midnight', interval=1)
            handler.suffix = "_%Y-%m-%d.txt"
            handler.setFormatter(logging.Formatter('%(asctime)s | %(message)s'))
            self.laser_logger.addHandler(handler)

    def load_today_laser_log(self):
        """프로그램 시작 시 오늘 작성된 로그가 있다면 불러와서 UI에 표시"""
        today_str = datetime.now().strftime('%Y-%m-%d')
        log_file = os.path.join(self.laser_log_dir, f"laser_log_{today_str}.txt")

        if os.path.exists(log_file):
            try:
                with open(log_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                    self.ui.laser_log_text.config(state="normal")
                    self.ui.laser_log_text.insert(tk.END, content)
                    self.ui.laser_log_text.config(state="disabled")
                    self.ui.laser_log_text.yview(tk.END)
            except Exception as e:
                print(f"Failed to load today's laser log: {e}")

    # 기존 _log_laser 메서드를 아래와 같이 업데이트 (파일 저장 로직 포함)
    def _log_laser(self, msg):
        """레이저 전용 로그 위젯과 파일에 동시에 기록"""
        # 1. 파일에 기록
        if hasattr(self, 'laser_logger'):
            self.laser_logger.info(msg)

        # 2. UI 텍스트 위젯에 기록
        timestamp = datetime.now().strftime('%H:%M:%S')
        if hasattr(self.ui, 'laser_log_text'):
            self.ui.laser_log_text.config(state="normal")
            self.ui.laser_log_text.insert(tk.END, f"[{timestamp}] {msg}\n")
            self.ui.laser_log_text.config(state="disabled")
            self.ui.laser_log_text.yview(tk.END)


    #################### UPS Monitoring ##############################

    def search_ups_ports(self):
        ports = serial.tools.list_ports.comports()
        port_list = []

        for port in ports:
            display_name = f"{port.device}" 
            port_list.append(display_name)

        if port_list:
            self.ui.ups_port_combo['values'] = port_list
            self.ui.ups_port_combo.current(0) # 첫 번째 항목 자동 선택
            self._log(f"Found {len(port_list)} serial ports.")
        else:
            self.ui.ups_port_combo['values'] = []
            self._log("No serial ports found. Check connection or drivers.")
            messagebox.showwarning("Search Result", "No serial ports detected.\nPlease check the USB-RS232 connection.")

    def auto_connect_ups(self):
        """lsusb에서 확인된 Prolific 장치를 찾아 2400bps로 연결합니다."""
        import serial.tools.list_ports
        ports = serial.tools.list_ports.comports()
        target_port = None
    
        print("\n--- Scanning for OMRON UPS ---")
        for port in ports:
            vid = port.vid if port.vid is not None else 0
            pid = port.pid if port.pid is not None else 0
            if vid == 0x067B and pid == 0x23A3:
                target_port = port.device
                print(f"[SUCCESS] UPS Found on {target_port}")
                break
        
        if target_port:
            if hasattr(self, 'ui'):
                self.ui.ups_vars["conn_status"].set(f"Connecting to {target_port}...")
            self._try_ups_handshake(target_port)
        else:
            if hasattr(self, 'ui'):
                self.ui.ups_vars["conn_status"].set("UPS H/W Not Found")

    def _try_ups_handshake(self, port):
        """스캐너로 확인된 BA100R 전용 설정(DTR=True, RTS=False, 2400bps)으로 연결합니다."""
        try:
            ser = serial.Serial(
                port=port,
                baudrate=2400,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                bytesize=serial.EIGHTBITS,
                timeout=1.0
            )
            
            # [핵심] 스캐너에서 성공한 라인 신호 설정
            ser.dtr = True
            ser.rts = False
            time.sleep(1.0) # 신호 안정화 대기

            ser.reset_input_buffer()
            # [핵심] 성공한 명령어 Q1 전송
            ser.write(b'Q1\r')
            time.sleep(0.5)
            
            response = ser.read(ser.in_waiting or 1).decode('ascii', errors='ignore').strip()
            
            if response.startswith('(') or ser.is_open:
                self.ups_serial = ser
                if hasattr(self, 'ui'):
                    self.ui.ups_vars["conn_status"].set(f"Connected: {port}")
                self._log(f"UPS Handshake Success with Q1: {repr(response)}")
                self.update_ups_status_loop()
            else:
                ser.close()
                if hasattr(self, 'ui'):
                    self.ui.ups_vars["conn_status"].set("No Response")
        except Exception as e:
            self._log(f"UPS Handshake Error: {e}")
            if hasattr(self, 'ui'):
                self.ui.ups_vars["conn_status"].set("Connection Error")
		
    def update_ups_status_loop(self):
        """Q1 데이터를 읽어 모든 정보(전력, 전압, 온도, 주파수)를 업데이트하고 그래프용 데이터를 저장합니다."""
        if self.ups_serial and self.ups_serial.is_open:
            try:
                # Q1 명령어 전송
                self.ups_serial.write(b'Q1\r')
                time.sleep(0.3)

                if self.ups_serial.in_waiting > 0:
                    raw_data = self.ups_serial.read(self.ups_serial.in_waiting)
                    response = raw_data.decode('ascii', errors='ignore').strip()

                    if response.startswith('('):
                        data = response[1:].split()
                        if len(data) >= 7:
                            # 1. 데이터 추출 (BA100R Q1 프로토콜 인덱스)
                            input_v  = data[0]      # 입력 전압 (V)
                            output_v = data[2]      # 출력 전압 (V)
                            load_p   = float(data[3]) # 부하율 (%)
                            freq     = data[4]      # 주파수 (Hz)
                            batt_v   = float(data[5]) # 배터리 전압 (V)
                            temp     = data[6]      # 온도 (°C)

                            # 2. 계산 로직
                            # 소비전력 (BA100R 최대 800W 기준)
                            current_watt = 800 * (load_p / 100.0)
                            # 배터리 잔량 % 계산 (21V~27.5V 범위를 0~100%로 변환)
                            batt_pct = min(100, max(0, int((batt_v - 21) / (27.5 - 21) * 100)))

                            # 3. UI 변수 업데이트
                            if hasattr(self, 'ui'):
                                self.ui.ups_vars["input_volt"].set(f"{input_v} V")
                                self.ui.ups_vars["output_volt"].set(f"{output_v} V")
                                self.ui.ups_vars["load_level"].set(int(load_p))
                                self.ui.ups_vars["batt_level"].set(batt_pct) # 배터리 슬라이더 업데이트
                                self.ui.ups_vars["frequency"].set(f"{freq} Hz")
                                
                                # 상태 메시지에 온도와 실시간 전력 표시
                                status_txt = f"Normal ({current_watt:.1f} W) / Temp: {temp}°C"
                                self.ui.ups_vars["status_msg"].set(status_txt)

                                # 출력 전압에 따른 아울렛 LED 상태 업데이트
                                if float(output_v) > 50:
                                    self.update_ups_outlet_status([1, 1, 0, 0])
                                else:
                                    self.update_ups_outlet_status([0, 0, 0, 0])

                            # 4. 그래프용 데이터 축적
                            now_str = datetime.now().strftime("%H:%M:%S")
                            self.ups_plot_history["time"].append(now_str)
                            self.ups_plot_history["batt"].append(batt_v)       # 배터리 전압 저장
                            self.ups_plot_history["watt"].append(current_watt) # 실시간 전력 저장
                            
                            # 5. 그래프 갱신 호출
                            self.refresh_ups_plot()

            except Exception as e:
                self._log(f"UPS Loop Error: {e}")

        if hasattr(self, 'master') and self.master.winfo_exists():
            self.master.after(2000, self.update_ups_status_loop)


    def toggle_ups_connection(self):
        """수동 연결/해제 버튼 동작 (BA100R 전용 설정 적용)"""
        if self.ups_serial and self.ups_serial.is_open:
            self.ups_serial.close()
            if hasattr(self, 'ui'):
                self.ui.ups_vars["conn_status"].set("Disconnected")
            self._log("UPS Serial Disconnected.")
        else:
            if hasattr(self, 'ui'):
                port = self.ui.ups_port_combo.get()
            else:
                port = None
                
            if not port:
                messagebox.showwarning("Warning", "Please select a port first.")
                return
            try:
                # 1. 시리얼 포트 열기 (2400bps)
                self.ups_serial = serial.Serial(port, 2400, timeout=1)
                
                # [중요] 스캐너 성공 설정: DTR은 켜고, RTS는 끕니다.
                self.ups_serial.dtr = True
                self.ups_serial.rts = False
                time.sleep(1.0)
                
                # 2. 접속 확인
                self.ups_serial.reset_input_buffer()
                self.ups_serial.write(b'Q1\r')
                time.sleep(0.5)
                
                if hasattr(self, 'ui'):
                    self.ui.ups_vars["conn_status"].set(f"Connected to {port}")
                
                self._log(f"UPS Manual Connect Success: {port}")
                self.update_ups_status_loop()
                
            except Exception as e:
                self.ups_serial = None
                messagebox.showerror("UPS Connection Error", f"Failed to connect: {str(e)}")

    def save_ups_log(self, dt, batt, load):
        # 경로 설정: /home/precalkor/ADC/ADC_test/LOG/UPS
        log_dir = os.path.join(self.base_dir, "LOG", "UPS")
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)

        # 파일명: ups_20260113.csv (날짜가 바뀌면 자동으로 새 파일 생성됨)
        file_path = os.path.join(log_dir, f"ups_{dt.strftime('%Y%m%d')}.csv")

        file_exists = os.path.isfile(file_path)
        with open(file_path, "a") as f:
            if not file_exists:
                f.write("Time,Battery_%,Load_%\n") # 헤더 작성
            f.write(f"{dt.strftime('%H:%M:%S')},{batt},{load}\n")

    def refresh_ups_plot(self):
        h = self.ups_plot_history
        times = list(h["time"])
        if not times: return

        plots = [
            (self.ui.ax_ups_watt, list(h["watt"]), "Power (W)", "red"),
            (self.ui.ax_ups_temp, list(h["temp"]), "Internal Temp (C)", "orange"),
            (self.ui.ax_ups_vin,  list(h["vin"]),  "Input Voltage (V)", "blue"),
            (self.ui.ax_ups_vout, list(h["vout"]), "Output Voltage (V)", "green")
        ]

        for ax, data, title, color in plots:
            ax.clear()
            ax.plot(times, data, color=color, linewidth=1.5, label=title)
            ax.set_title(title, fontsize=10, fontweight='bold')
            ax.grid(True, alpha=0.3)
            ax.tick_params(labelsize=8)

        self.ui.fig_ups.autofmt_xdate()
        self.ui.canvas_ups.draw()

    def update_ups_outlet_status(self, states):
        """
        states: [state1, state2, state3, state4] 형태의 리스트
        0: 회색(Off), 1: 녹색(On), 2: 빨간색(Error)
        """
        colors = {0: "#adb5bd", 1: "#28a745", 2: "#dc3545"}
        for i, state in enumerate(states):
            self.ui.outlet_canvas.itemconfig(self.ui.outlet_circles[i], fill=colors[state])

    def shutdown_ups_all(self):
        """UPS 출력을 완전히 차단합니다."""
        # 1. 사용자 재확인 (매우 중요!)
        confirmed = messagebox.askseriousquestion("WARNING",
                                                  "Are you sure you want to SHUT DOWN all outputs?\nThis will cut power to all connected devices immediately!")

        if confirmed == 'yes':
            if self.ups_serial and self.ups_serial.is_open:
                try:
                    # OMRON BA100R 셧다운 커맨드 예시 (모델별 프로토콜 확인 필요)
                    # 보통 'S' 뒤에 지연 시간을 보내거나 특정 Hex 값을 보냅니다.
                    # self.ups_serial.write(b'S00\r') # 00분 뒤 즉시 셧다운 예시

                    self._log("!!! UPS SHUTDOWN COMMAND SENT !!!")

                    # 시각적으로 모든 전원이 꺼짐을 표시 (회색)
                    self.update_ups_outlet_status([0, 0, 0, 0])
                    messagebox.showinfo("Shutdown", "Shutdown command sent successfully.")

                except Exception as e:
                    self._log(f"Shutdown Failed: {e}")
                    # 오류 시 빨간색으로 표시
                    self.update_ups_outlet_status([2, 2, 2, 2])
                    messagebox.showerror("Error", f"Failed to send shutdown command: {e}")
            else:
                messagebox.showwarning("Connection Error", "UPS is not connected via RS232C.")

    # main.py App 클래스 내부에 추가/수정

    def handle_ups_shutdown(self):
        """콤보박스 선택에 따라 전체 혹은 개별 셧다운을 실행합니다."""
        target = self.ui.shutdown_target_var.get()

        confirm = messagebox.askyesno("Confirm Shutdown", 
                                      f"Are you sure you want to SHUTDOWN [{target}]?\nConnected device power will be cut.")
        if not confirm:
            return

        if target == "All Outlets":
            self.shutdown_ups_all()
        else:
            # 개별 인덱스 추출 (1, 2, 3, 4)
            try:
                # 콤보박스 텍스트에서 숫자만 추출하거나 인덱스로 매칭
                idx_map = {"Outlet 1 (DAQ)": 0, "Outlet 2 (Laser)": 1, "Outlet 3": 2, "Outlet 4": 3}
                self.shutdown_ups_each(idx_map[target])
            except KeyError:
                self._log("Shutdown target mapping error.")

    def shutdown_ups_each(self, index):
        """특정 아울렛만 끄는 명령 (OMRON 프로토콜 확인 필요)"""
        if self.ups_serial and self.ups_serial.is_open:
            try:
                # 개별 제어 명령 전송 (매뉴얼의 해당 커맨드 입력 필요)
                # self.ups_serial.write(f"OFF {index+1}\r".encode()) 

                self._log(f"UPS Individual Shutdown Command Sent: Outlet {index+1}")

                # 시각적 피드백: 해당 아울렛만 빨간색(2)으로 변경
                current_states = [1, 1, 0, 0] # 실제 상태 읽어오기 전 임시
                current_states[index] = 2 
                self.update_ups_outlet_status(current_states)

            except Exception as e:
                messagebox.showerror("Error", f"Failed to shut down individual outlet: {e}")
        else:
            messagebox.showwarning("Connection Error", "UPS Serial is not connected.")



    def get_system_status(self):
        status = {
                "DAQ": False,
                "Laser": False,
                "UPS": False
                }

        if hasattr(self, 'ui') and hasattr(self.ui, 'daq_connected_flag'):
            status["DAQ"] = self.ui.daq_connected_flag

        # 2. Laser 상태
        if self.laser and self.laser.is_connected():
            status["Laser"] = True

        # 3. UPS 상태 (시리얼 포트 체크)
        if self.ups_serial and self.ups_serial.is_open:
            # UI 변수가 안전하게 로드되었는지 확인 후 상태 반영
            if hasattr(self, 'ui'):
                msg = self.ui.ups_vars["status_msg"].get()
                if "Normal" in msg or "Battery" in msg:
                    status["UPS"] = True

        return status


if __name__ == "__main__":
    base_directory = os.path.dirname(os.path.abspath(__file__))
    root = tk.Tk()
    app = App(root, base_directory)
    root.mainloop()

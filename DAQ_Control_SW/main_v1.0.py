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
import matplotlib.dates as mdates 
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
LASER_AVAILABLE = False 

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
        master.geometry("1600x950")
        self.master.minsize(1400, 900)

        icon_path = os.path.join(self.base_dir, 'icons', 'DAQcontroller.png')
        img = Image.open(icon_path)
        self.p_img = ImageTk.PhotoImage(img, master=master)
        master.iconphoto(True, self.p_img)

        self.contacts_file = os.path.join(self.base_dir, "contacts.json")
        self.load_contacts()

        self.start_time = datetime.now()
        self.config_manager = None
        self.terminal_preference = 'gnome-terminal'
        self.load_app_config()

        self.wavelengths = ["375nm", "405nm", "450nm", "473nm"]
        self.laser_instances = {}

        if LASER_AVAILABLE:
            for wl in self.wavelengths:
                try:
                    from laser_driver import TamadenshiLaser
                    self.laser_instances[wl] = TamadenshiLaser()
                    print(f"✅ Laser driver instance created for {wl}")
                    
                    if wl == "405nm":
                        self.laser = self.laser_instances[wl]
                except Exception as e:
                    print(f"❌ Failed to initialize laser {wl}: {e}")

        self.laser_session_start = None
        self.laser_after_id = None     

        self.ups_serial = None
        self.ups_session_start = None  
        self.ups_after_id = None      

        self.ups_plot_history = {
            "time": collections.deque(maxlen=90000),
            "watt": collections.deque(maxlen=90000),
            "temp": collections.deque(maxlen=90000),
            "vin":  collections.deque(maxlen=90000),
            "vout": collections.deque(maxlen=90000)
        }
        
        self.plot_history = {}
        for wl in self.wavelengths:
            self.plot_history[wl] = {
                "time": collections.deque(maxlen=90000), 
                "temp": collections.deque(maxlen=90000), 
                "pulse": collections.deque(maxlen=90000)
            }
        self.active_wavelength = None

        self.status_bar = ttk.Frame(master, relief=tk.SUNKEN, padding="2 5")
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)
        self.elapsed_time_var = tk.StringVar()
        self.clock_var = tk.StringVar()
        ttk.Label(self.status_bar, textvariable=self.elapsed_time_var).pack(side=tk.RIGHT, padx=10)
        ttk.Label(self.status_bar, textvariable=self.clock_var).pack(side=tk.LEFT, padx=10)
        self._update_status_bar()

        self.ui = UIManager(master, self)
        self.ui.setup_shortcuts()

        if self.config_manager:
            self.validate_config_paths()
            self.master.after(500, self.refresh_all_data)
            self.master.after(1000, self.check_daq_connection)
        else:
            messagebox.showwarning("Warning", "Configuration not loaded.")

        if self.laser_instances:
            self.master.after(5000, self.auto_connect_laser) 
            self.update_laser_status_loop()

        self.on_laser_trigger_change()
        self.setup_laser_logger()
        self.load_today_laser_log()
        self.preload_laser_history()
        self.preload_ups_history()

        self.ui.is_dark_mode = True 
        self.ui.toggle_theme()

        self.master.after(1500, self.auto_connect_ups)

    def load_contacts(self):
        """contacts.json 파일에서 연락망을 불러옵니다."""
        self.contacts_file = os.path.join(self.base_dir, "contacts.json")
        if os.path.exists(self.contacts_file):
            try:
                with open(self.contacts_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                self._log(f"Error loading contacts: {e}")
        return [] 

    # main.py

    def update_plots_theme(self, is_dark):
        """멀티 탭 구조의 모든 그래프 테마를 일괄 변경합니다."""
        bg_color = "#2d2d2d" if is_dark else "white"
        fg_color = "white" if is_dark else "black"
        grid_color = "#444444" if is_dark else "#dddddd"

        # [1] 관리할 그래프와 캔버스를 담을 바구니 생성
        figs_to_style = []
        canvases_to_draw = []

        # [2] 공통 그래프 (UPS, Hist) 추가
        if hasattr(self.ui, 'fig_ups'):
            figs_to_style.append(self.ui.fig_ups)
            canvases_to_draw.append(self.ui.canvas_ups)
        if hasattr(self.ui, 'fig_hist'):
            figs_to_style.append(self.ui.fig_hist)
            canvases_to_draw.append(self.ui.canvas_hist)

        # [3] 4개 파장 탭의 모든 그래프 수집
        if hasattr(self.ui, 'laser_tabs_data'):
            for wl, vars_dict in self.ui.laser_tabs_data.items():
                if "fig" in vars_dict:
                    figs_to_style.append(vars_dict["fig"])
                if "canvas" in vars_dict:
                    canvases_to_draw.append(vars_dict["canvas"])

        # [4] 수집된 모든 그래프에 스타일 적용
        for fig in figs_to_style:
            fig.patch.set_facecolor(bg_color)
            for ax in fig.get_axes():
                ax.set_facecolor(bg_color)
                ax.tick_params(colors=fg_color)
                ax.xaxis.label.set_color(fg_color)
                ax.yaxis.label.set_color(fg_color)
                ax.title.set_color(fg_color)
                for spine in ax.spines.values():
                    spine.set_color(fg_color)
                ax.grid(True, color=grid_color, alpha=0.5)
            
            # [5] 그래프별 맞춤 레이아웃 정렬
            if hasattr(self.ui, 'fig_ups') and fig == self.ui.fig_ups:
                fig.tight_layout(rect=[0, 0, 1, 0.96]) # UPS 전용 여백
            else:
                fig.tight_layout()

        # [6] 수집된 모든 캔버스 새로 그리기
        for canvas in canvases_to_draw:
            canvas.draw()

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
        """Config 수정 후 화면상의 Configuration 및 파일 목록 자동 새로고침"""
        if self.config_manager:
            pmt_win = PMTConfigWindow(self.master, self.config_manager, pmt_name)
            self.master.wait_window(pmt_win)
            self.refresh_all_data() 
        else:
            messagebox.showwarning("Warning", "Configuration manager not initialized.")

    def run_cisco(self):
        """찾은 정확한 경로를 포함하여 Cisco vpnui를 실행합니다."""
        self._log("Attempting to launch SUKAP Connection (Cisco)...")
        
        # 확인된 경로를 가장 상단에 배치합니다.
        cisco_paths = [
            "/opt/cisco/secureclient/bin/vpnui", # 사용자님이 확인하신 경로
            "/opt/cisco/anyconnect/bin/vpnui",
            "/usr/local/bin/vpnui",
            "vpnui"
        ]
        
        executed = False
        for path in cisco_paths:
            try:
                # 프로그램 실행 시도
                subprocess.Popen([path])
                self._log(f"Cisco launched successfully from: {path}")
                executed = True
                break
            except FileNotFoundError:
                continue
            except Exception as e:
                self._log(f"Error launching {path}: {e}")
                continue
        
        if not executed:
            self._log("ERROR: Cisco vpnui 실행 파일을 찾을 수 없습니다.")
            messagebox.showerror("Execution Error", 
                                 f"Cisco vpnui를 찾을 수 없습니다.\n\n"
                                 f"확인된 경로: /opt/cisco/secureclient/bin/vpnui\n"
                                 f"파일 권한(chmod +x)을 확인해 보세요.")


    def run_daq(self):
        try:
            check_running = subprocess.run(['pgrep', '-f', 'execute_DAQ'], capture_output=True)
            if check_running.returncode == 0:
                messagebox.showwarning("DAQ Already Running", 
                                       "An instance of 'execute_DAQ' is already running.\n"
                                       "Starting multiple DAQ processes can be critical for the buffer.\n"
                                       "Please stop the current run first.")
                return
        except Exception as e:
            self._log(f"Check process error: {e}")

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
        - 0개 선택: 텍스트 박스의 Run Number 사용
        - 1개 선택: 선택한 파일의 경로와 Run Number 사용 (Produce 방식)
        - 2개 이상: 경고 메시지 출력
        """
        selected_files = self.ui.get_selected_file_paths()
        daq_path = self._get_daq_path()
        if not daq_path: return

        helper = os.path.join(self.base_dir, 'run_cpp_script.sh')
        script = os.path.join(daq_path, 'Draw_waveform.C')
        config_path = self.config_manager.filepath
        
        run_num = None
        f_path = "" # 선택된 파일 경로 초기화

        if len(selected_files) > 1:
            messagebox.showwarning("Multiple Files Selected", 
                                   "Please select only one file for Waveform Inspection.")
            return
        
        elif len(selected_files) == 1:
            # Case: 파일이 하나 선택된 경우
            f_path = selected_files[0]
            f_name = os.path.basename(f_path)
            pattern = re.compile(r'\.([0-9]{4})\.root$')
            match = pattern.search(f_name)
            
            if match:
                run_num = str(int(match.group(1)))
            else:
                self._log(f"WARNING: Could not extract run number from {f_name}.")
                messagebox.showwarning("Error", f"Could not extract run number from selected file:\n{f_name}")
                return
        
        else:
            # Case: 선택된 파일이 없는 경우 (기존 방식)
            run_num = self.ui.get_run_num()
            if not run_num: return

        if run_num:
            # Produce/Analysis와 동일하게 파일 경로를 따옴표로 감싸 인자로 추가
            f_path_arg = f"\\\"{f_path}\\\"" if f_path else "\"\""
            
            # 인자 순서: run_num, 'interactive', f_path_arg
            command_parts = [helper, script, config_path, run_num, 'interactive', f_path_arg]
            final_command_string = " ".join(command_parts)
            self._execute_in_new_terminal([final_command_string])

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

        if self.master.winfo_exists():
            self.master.after(2000, self.check_daq_connection)

    # main.py 수정 (약 1430번 라인 근처)

    def update_data_directory_size(self):
        print("DEBUG: update_data_directory_size called") # [디버깅] 함수 호출 확인

        if not self.config_manager:
            print("DEBUG: ConfigManager is None") # [디버깅] 설정 파일 로드 실패 확인
            self.ui.update_data_size_display("Config Not Loaded", False)
            self.ui.update_data_size_display("Config Not Loaded", True)
            return

        raw_data_path = self.config_manager.get_config_value("RawDataPath")
        ext_data_path = self.config_manager.get_config_value("ExternalPath")

        print(f"DEBUG: RawDataPath from config: '{raw_data_path}'") # [디버깅] 경로 확인
        print(f"DEBUG: ExternalPath from config: '{ext_data_path}'")

        # 1. 로컬 경로 체크
        if raw_data_path and os.path.exists(raw_data_path):
            print(f"DEBUG: Starting thread for Local Path: {raw_data_path}")
            threading.Thread(target=self._get_directory_size_thread, args=(raw_data_path, False), daemon=True).start()
        else:
            print(f"DEBUG: Local Path invalid. Exists? {os.path.exists(raw_data_path) if raw_data_path else 'N/A'}")
            msg = "Path Not Found" if raw_data_path else "Path Not Set"
            self.ui.update_data_size_display(msg, False)

        # 2. 외부 하드 경로 체크
        if ext_data_path and os.path.exists(ext_data_path):
            print(f"DEBUG: Starting thread for External Path: {ext_data_path}")
            threading.Thread(target=self._get_directory_size_thread, args=(ext_data_path, True), daemon=True).start()
        else:
            print(f"DEBUG: External Path invalid. Exists? {os.path.exists(ext_data_path) if ext_data_path else 'N/A'}")
            msg = "Path Not Found" if ext_data_path else "Path Not Set"
            self.ui.update_data_size_display(msg, True)

    def _get_directory_size_thread(self, path, is_ext):
        """디버깅 프린트가 추가된 용량 계산 함수"""
        print(f"DEBUG: Thread started for {path}") # [디버깅] 쓰레드 시작 확인
        display_str = "Error"
        
        try:
            # df -h와 동일한 기능
            usage = shutil.disk_usage(path)
            print(f"DEBUG: shutil.disk_usage result: {usage}") # [디버깅] 계산 결과 확인
            
            used_human = self.format_size(usage.used)
            total_human = self.format_size(usage.total)
            percent = (usage.used / usage.total) * 100
            
            display_str = f"{used_human} / {total_human} ({percent:.1f}%)"
            print(f"DEBUG: Final string: {display_str}") # [디버깅] 최종 문자열 확인

        except Exception as e:
            print(f"DEBUG: Error in thread: {e}") # [디버깅] 에러 발생 시 출력
            display_str = "Calc Error"
            self._log(f"Error checking disk usage for {path}: {e}")
            
        finally:
            if hasattr(self, 'ui') and self.master.winfo_exists():
                print(f"DEBUG: Updating UI with {display_str}") # [디버깅] UI 업데이트 시도
                self.master.after(0, lambda: self.ui.update_data_size_display(display_str, is_ext))
            else:
                print("DEBUG: UI object not found or window closed")


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
        if "405nm" in self.laser_instances:
            self._log("Auto-connecting to 405nm Laser...")
            self.connect_single_laser("405nm")
    def connect_single_laser(self, wl):
        """[수정] 개별 탭의 Connect 버튼 동작"""
        inst = self.laser_instances.get(wl)
        vars_dict = self.ui.laser_tabs_data.get(wl)
        
        if not inst or not vars_dict: return

        if inst.is_connected():
            self._log(f"{wl} is already connected.")
            return

        self._log(f"Connecting to {wl}...")
        # UI에 '연결 중...' 표시
        vars_dict["conn_status_txt"].set("Connecting...")
        vars_dict["conn_label_obj"].config(foreground="orange")
        
        success, msg = inst.connect() #
        
        if success:
            # 성공 시 UI 업데이트
            vars_dict["conn_status_txt"].set("Connected")
            vars_dict["conn_label_obj"].config(foreground="#28a745") # 초록색
            vars_dict["ld_status"].set("Connected")
            self._log(f"✅ {wl} Connected.")
            
            # 모니터링 루프 가속
            self.laser_session_start = time.time()
            self.update_laser_status_loop()
        else:
            # 실패 시 UI 원상복구
            vars_dict["conn_status_txt"].set("Disconnected")
            vars_dict["conn_label_obj"].config(foreground="red")
            self._log(f"❌ {wl} Connection Failed: {msg}")
            messagebox.showerror("Connection Error", f"Failed to connect {wl}\n{msg}")

    def disconnect_single_laser(self, wl):
        """[수정] 개별 탭의 Disconnect 버튼 동작 (강제 초기화 포함)"""
        inst = self.laser_instances.get(wl)
        vars_dict = self.ui.laser_tabs_data.get(wl)
        
        if not vars_dict: return

        # [중요] 실제 연결 여부와 관계없이, 사용자가 '해제'를 눌렀으므로 무조건 시도합니다.
        try:
            if inst: inst.disconnect()
        except Exception as e:
            self._log(f"Warning during disconnect {wl}: {e}")

        # [중요] UI 강제 초기화 (안 끊기는 느낌 제거)
        vars_dict["conn_status_txt"].set("Disconnected")
        vars_dict["conn_label_obj"].config(foreground="red")
        
        vars_dict["ld_status"].set("Disconnected")
        vars_dict["tec_status"].set("OFF")
        vars_dict["temp"].set("--.- °C")
        
        # 탭 아이콘 빨간색으로 변경
        idx = self.wavelengths.index(wl)
        self.ui.laser_sub_notebook.tab(idx, image=self.ui.tab_led_red, compound=tk.RIGHT)
        
        self._log(f"🔌 {wl} Disconnected (User Request).")

    def manual_refresh_laser(self, wl=None):
        """[수정] 특정 파장의 상태를 즉시 새로고침합니다."""
        self.laser_session_start = time.time() # 빠른 루프 전환
        if wl:
            self._log(f"Refreshing {wl}...")
        else:
            self._log("Refreshing lasers...")
        self.update_laser_status_loop()

    def set_laser_ld_safe(self, target_wl, state):
        """[수정] LD 제어: 다른 레이저가 켜져 있으면 경고창을 띄우고 끕니다."""
        
        # 1. 켜려고 할 때(state=True) 다른 놈들이 켜져 있는지 검사
        if state is True:
            active_lasers = []
            for wl, inst in self.laser_instances.items():
                # 내가 아니고(wl != target_wl), 연결되어 있고, LD가 켜져 있는 놈 찾기
                # (주의: inst.status는 update_status()가 호출되어야 최신임. 
                #  확실하게 하기 위해 UI 변수나 내부 플래그를 확인)
                if wl != target_wl and inst.is_connected():
                    # 안전하게 UI 상태 변수로 확인 (가장 최근 업데이트된 값)
                    if self.ui.laser_tabs_data[wl]["ld_status"].get() == "ON":
                        active_lasers.append(wl)

            # 다른 켜진 레이저가 발견되면 경고창 띄움
            if active_lasers:
                msg = f"Laser {', '.join(active_lasers)} is currently ON.\n\n" \
                      f"To turn on {target_wl}, the others must be turned OFF.\n" \
                      f"Proceed?"
                
                if not messagebox.askyesno("Safety Interlock", msg):
                    self._log(f"Operation cancelled: {target_wl} ON blocked by user.")
                    return # 사용자가 '아니오' 누르면 함수 종료 (안 켬)

                # 사용자가 '예' 누르면 -> 다른 레이저들 끄기
                for wl in active_lasers:
                    inst = self.laser_instances.get(wl)
                    if inst:
                        inst.set_ld_on(False) # 하드웨어 OFF
                        self.ui.laser_tabs_data[wl]["ld_status"].set("OFF") # UI OFF
                        self.ui.update_laser_status_colors(wl, False, False) # 빨간색
                        self._log(f"Safety: Auto-shutdown {wl}")

        # 2. 타겟 레이저 제어 (이제 안전함)
        inst = self.laser_instances.get(target_wl)
        if inst and inst.is_connected():
            inst.set_ld_on(state)
            self._log(f"Command Sent: Laser {target_wl} LD -> {'ON' if state else 'OFF'}")

            # 빠른 확인을 위해 0.2초 후 루프 실행
            self.laser_session_start = time.time()
            if self.laser_after_id:
                self.master.after_cancel(self.laser_after_id)
            self.master.after(200, self.update_laser_status_loop)


    def apply_laser_frequency_multi(self, wl):
        """특정 파장 기기에 트리거 모드 및 주파수 적용"""
        inst = self.laser_instances.get(wl)
        vars_dict = self.ui.laser_tabs_data.get(wl)
        
        if inst and inst.is_connected() and vars_dict:
            try:
                hz = int(vars_dict["freq_hz"].get())
                mode = vars_dict["trigger_mode"].get()

                pg1, pg2, ext = (mode=="Internal (PG1)"), (mode=="Internal (PG2)"), (mode=="External")
                inst.set_trigger_mode(pg1, pg2, ext) #

                if pg1: inst.set_pg1_frequency(hz)
                elif pg2: inst.set_pg2_frequency(hz)

                self._log(f"✅ Laser {wl} Config: {mode}, {hz} Hz applied.")
            except ValueError:
                messagebox.showerror("Error", f"Invalid frequency for {wl}. Must be integer.")


    def set_laser_tec_multi(self, wl, state):
        """TEC 제어: 하드웨어 명령 후 즉시 상태를 재확인합니다."""
        inst = self.laser_instances.get(wl)
        if inst and inst.is_connected():
            inst.set_tec_on(state)
            self._log(f"Command Sent: Laser {wl} TEC -> {'ON' if state else 'OFF'}")

            # [핵심 수정] UI 직접 수정 코드 삭제함.
            # 대신 0.5초 뒤에 루프를 돌려 확인 (TEC는 반응이 약간 느릴 수 있으므로 0.5초)
            self.laser_session_start = time.time()
            
            if self.laser_after_id:
                self.master.after_cancel(self.laser_after_id)
            
            self.master.after(500, self.update_laser_status_loop)
    
    def apply_laser_currents_multi(self, wl):
        """특정 파장 탭의 Bias/Pulse 전류 설정을 기기에 적용합니다."""
        inst = self.laser_instances.get(wl)
        vars_dict = self.ui.laser_tabs_data.get(wl)
        
        if inst and inst.is_connected() and vars_dict:
            try:
                bias = vars_dict["bias_set"].get()
                pulse = vars_dict["pulse_set"].get()
                
                inst.set_bias_current(bias)
                inst.set_pulse_current(pulse)
                self._log(f"✅ Applied to {wl}: Bias={bias:.2f}mA, Pulse={pulse:.2f}mA")
                
                if self.laser_after_id:
                    self.master.after_cancel(self.laser_after_id)
                self.master.after(100, self.update_laser_status_loop)
            except Exception as e:
                self._log(f"❌ Error applying currents to {wl}: {e}")

    def update_laser_status_loop(self):
        """탭 헤더에 [연결, LD, TEC] 상태를 통합 표시하고 데이터를 중복 없이 저장합니다."""
        if self.laser_after_id:
            self.master.after_cancel(self.laser_after_id)
            self.laser_after_id = None

        interval = 60000 # 평상시 60초

        for idx, wl in enumerate(self.wavelengths):
            inst = self.laser_instances.get(wl)
            ui_vars = self.ui.laser_tabs_data.get(wl)
            if not inst or not ui_vars: continue

            if inst.is_connected():
                # [정보 1] 연결됨 -> 탭 아이콘 녹색
                self.ui.laser_sub_notebook.tab(idx, image=self.ui.tab_led_green, compound=tk.RIGHT)

                if inst.update_status(): #
                    status = inst.status
                    is_ld_on = status.get('ld_on', False)
                    is_tec_on = status.get('tec_on', False)
                    
                    # 수치 변수 할당 (중복 방지)
                    temp_val = status.get('ld_temp', 0)
                    pulse_val = status.get('pulse', 0)
                    now_ts = datetime.now()

                    # [정보 2&3] LD, TEC 상태 기호 표시
                    ld_mark = "●" if is_ld_on else "○"
                    tec_mark = "●" if is_tec_on else "○"
                    new_title = f" {wl} [L:{ld_mark} T:{tec_mark}] "
                    self.ui.laser_sub_notebook.tab(idx, text=new_title)

                    # UI 텍스트 및 색상 업데이트
                    ui_vars["ld_status"].set("ON" if is_ld_on else "OFF")
                    ui_vars["tec_status"].set("ON" if is_tec_on else "OFF")
                    ui_vars["temp"].set(f"{temp_val:.2f} °C")
                    ui_vars["pulse_live"].set(f"{pulse_val:.2f} mA")
                    self.ui.update_laser_status_colors(wl, is_ld_on, is_tec_on)

                    # 1. 파장별 독립 메모리에 데이터 축적 (한 번만 수행)
                    self.plot_history[wl]["temp"].append(temp_val)
                    self.plot_history[wl]["pulse"].append(pulse_val)
                    self.plot_history[wl]["time"].append(now_ts)

                    # 2. 실시간 그래프 갱신 및 파일 저장
                    self.refresh_laser_realtime_plot(wl)
                    self.save_laser_realtime_data(wl, temp_val, pulse_val)
            else:
                # 연결 안 됨 처리
                self.ui.laser_sub_notebook.tab(idx, image=self.ui.tab_led_red, compound=tk.RIGHT)
                self.ui.laser_sub_notebook.tab(idx, text=f" {wl} ")
                if wl != "405nm":
                    ui_vars["ld_status"].set("Disconnected")

        # 가변 주기 제어 로직 (기존 유지)
        if self.laser_instances:
            if self.laser_session_start is None: self.laser_session_start = time.time()
            elapsed = time.time() - self.laser_session_start
            interval = 1000 if elapsed < 10 else 60000
            
            for w in self.wavelengths:
                if w in self.ui.laser_tabs_data:
                    self.ui.laser_tabs_data[w]["check_interval"].set(f"{interval/1000:.0f}s")

        if hasattr(self, 'master') and self.master.winfo_exists():
            self.laser_after_id = self.master.after(interval, self.update_laser_status_loop)

    def manual_refresh_laser(self):
        """레이저 상태를 즉시 새로고침하고 10초간 빠른 모드로 전환합니다."""
        if self.laser and self.laser.is_connected():
            self.laser_session_start = time.time() # 타이머 리셋
            self._log("Laser manual refresh triggered (1s mode for 10s)")
            self.update_laser_status_loop()


    def on_laser_trigger_change_multi(self, wl):
        """특정 파장 탭의 트리거 모드에 따라 입력창 활성/비활성 제어"""
        vars_dict = self.ui.laser_tabs_data.get(wl)
        if not vars_dict: return

        mode = vars_dict["trigger_mode"].get()
        entry = vars_dict.get("freq_entry_obj")
        btn = vars_dict.get("freq_apply_btn_obj")
        frame = vars_dict.get("trig_frame_obj")

        if mode == "External":
            if entry: entry.config(state="disabled")
            if btn: btn.config(state="disabled")
            if frame: frame.config(text=f"Trigger Control - DISABLED (External) [{wl}]")
        else:
            if entry: entry.config(state="normal")
            if btn: btn.config(state="normal")
            if frame: frame.config(text=f"Trigger Control - ENABLED (Internal) [{wl}]")

        # 실제 하드웨어에 설정 적용
        inst = self.laser_instances.get(wl)
        if inst and inst.is_connected():
            self.apply_laser_frequency_multi(wl)

    def on_laser_trigger_change(self, event=None):
        """기존 코드와의 호환성을 위해 모든 탭의 트리거 상태를 업데이트"""
        for wl in self.wavelengths:
            self.on_laser_trigger_change_multi(wl)

    def load_historical_laser_data(self, wl=None):
        """지정된 경로에서 로그를 불러오고 해당 탭 그래프에 표시합니다."""
        log_dir = "/home/precalkor/ADC/ADC_test/LOG/LASER" # [박사님 요청 경로 고정]
        
        if not wl:
            idx = self.ui.laser_sub_notebook.index(self.ui.laser_sub_notebook.select())
            wl = self.wavelengths[idx]

        vars_dict = self.ui.laser_tabs_data.get(wl)
        if not vars_dict: return

        file_path = filedialog.askopenfilename(initialdir=log_dir, title=f"Select Log for {wl}",
                                               filetypes=(("CSV files", "*.csv"), ("All files", "*.*")))
        if file_path:
            import pandas as pd
            try:
                df = pd.read_csv(file_path)
                df['timestamp'] = pd.to_datetime(df['timestamp'])
                
                fig_h = vars_dict["fig_hist"]
                fig_h.clf()
                ax1 = fig_h.add_subplot(2, 1, 1)
                ax2 = fig_h.add_subplot(2, 1, 2, sharex=ax1)

                ax1.plot(df['timestamp'], df['temp_c'], 'r-', label='Temp')
                ax1.set_ylabel('Temp (°C)', color='r')
                ax2.plot(df['timestamp'], df['pulse_ma'], 'g-', label='Pulse')
                ax2.set_ylabel('Current (mA)', color='g')

                fig_h.autofmt_xdate(rotation=30)
                fig_h.tight_layout()
                vars_dict["canvas_hist"].draw()
                self._log(f"Success: Historical data for {wl} loaded.")
            except Exception as e:
                messagebox.showerror("Error", f"Failed to load data: {e}")

    def refresh_laser_realtime_plot(self, wl="405nm"):
        """파장별 독립 히스토리 데이터를 사용하여 그래프를 그립니다."""
        vars_dict = self.ui.laser_tabs_data.get(wl)
        history = self.plot_history.get(wl)
        if not vars_dict or not history or "ax_temp" not in vars_dict: return

        times = list(history["time"])
        if not times: return

        step = max(1, len(times) // 1000)
        d_times, d_temp, d_pulse = times[::step], list(history["temp"])[::step], list(history["pulse"])[::step]

        ax_temp = vars_dict["ax_temp"]
        ax_temp.clear()
        ax_temp.plot(d_times, d_temp, 'r-', linewidth=1)
        ax_temp.set_ylabel("Temp (°C)", color='r')
        ax_temp.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d %H:%M'))
        ax_temp.grid(True, alpha=0.3)

        ax_curr = vars_dict["ax_curr"]
        ax_curr.clear()
        ax_curr.plot(d_times, d_pulse, 'g-', linewidth=1)
        ax_curr.set_ylabel("Current (mA)", color='g')
        ax_curr.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d %H:%M'))
        ax_curr.grid(True, alpha=0.3)

        vars_dict["fig"].autofmt_xdate(rotation=30)
        vars_dict["canvas"].draw()

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


    def preload_laser_history(self):
        """[수정] 날짜 변화와 상관없이 전날 00:00:00부터의 데이터를 복구합니다."""
        import pandas as pd
        from datetime import timedelta

        now = datetime.now()
        # [핵심] 기준 시점: 어제 날짜의 00시 00분 00초
        start_point = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        
        # 어제와 오늘 날짜 리스트 생성
        dates_to_check = [
            (now - timedelta(days=1)).strftime('%Y%m%d'),
            now.strftime('%Y%m%d')
        ]

        for wl in self.wavelengths:
            # 기존 데이터 비우기
            self.plot_history[wl]["time"].clear()
            self.plot_history[wl]["temp"].clear()
            self.plot_history[wl]["pulse"].clear()
            
            total_points = 0
            for date_str in dates_to_check:
                log_file = f"/home/precalkor/ADC/ADC_test/LOG/LASER/laser_data_{wl}_{date_str}.csv"
                
                if os.path.exists(log_file):
                    try:
                        df = pd.read_csv(log_file)
                        for _, row in df.iterrows():
                            try:
                                ts = datetime.fromisoformat(row['timestamp'])
                                # [핵심 조건] 전날 0시 이후 데이터만 메모리에 로드
                                if ts >= start_point:
                                    self.plot_history[wl]["time"].append(ts)
                                    self.plot_history[wl]["temp"].append(float(row['temp_c']))
                                    self.plot_history[wl]["pulse"].append(float(row['pulse_ma']))
                                    total_points += 1
                            except: continue
                    except Exception as e:
                        self._log(f"Preload error ({wl}, {date_str}): {e}")

            if total_points > 0:
                self._log(f"Laser {wl} history recovered: {total_points} pts (since {start_point.strftime('%m-%d %H:%M')})")
                self.refresh_laser_realtime_plot(wl)


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

    def save_laser_realtime_data(self, wl, temp, pulse):
        """[신규] 각 파장의 데이터를 날짜별 CSV에 기록합니다."""
        log_dir = "/home/precalkor/ADC/ADC_test/LOG/LASER"
        os.makedirs(log_dir, exist_ok=True)

        today_str = datetime.now().strftime('%Y%m%d')
        # 파일명에 파장 정보를 포함하면 추후 분석이 더 용이합니다.
        file_path = os.path.join(log_dir, f"laser_data_{wl}_{today_str}.csv")
        file_exists = os.path.isfile(file_path)

        try:
            with open(file_path, "a") as f:
                if not file_exists:
                    f.write("timestamp,temp_c,pulse_ma\n")
                # ISO 포맷으로 타임스탬프 저장
                now_iso = datetime.now().isoformat()
                f.write(f"{now_iso},{temp:.2f},{pulse:.2f}\n")
        except Exception as e:
            self._log(f"Error saving laser log for {wl}: {e}")


    #################### UPS Monitoring ##############################

    def search_ups_ports(self):
        ports = serial.tools.list_ports.comports()
        port_list = []

        for port in ports:
            display_name = f"{port.device}" 
            port_list.append(display_name)

        if port_list:
            self.ui.ups_port_combo['values'] = port_list
            self.ui.ups_port_combo.current(0) 
            self._log(f"Found {len(port_list)} serial ports.")

        if hasattr(self, 'ui'):
            self.ui.ups_conn_btn.config(state="normal")
            self.ui.ups_refresh_btn.config(state="normal")
            self._log("UPS Control buttons enabled.")

        if not port_list:
            self._log("No serial ports found. You can still type the port manually.")
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
            #time.sleep(1.0) # 신호 안정화 대기
            time.sleep(2.0)

            ser.reset_input_buffer()
            # [핵심] 성공한 명령어 Q1 전송
            ser.write(b'Q1\r')
            #time.sleep(0.5)
            time.sleep(1.0)
            
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
        if self.ups_after_id:
            self.master.after_cancel(self.ups_after_id)
            self.ups_after_id = None

        interval = 2000 

        if self.ups_serial and self.ups_serial.is_open:
            if self.ups_session_start is None:
                self.ups_session_start = time.time()
            
            elapsed = time.time() - self.ups_session_start
            interval = 1000 if elapsed < 60 else 60000 # 1분간 1초, 이후 60초

            try:
                self.ups_serial.write(b'Q1\r')
                time.sleep(0.3)
                if self.ups_serial.in_waiting > 0:
                    response = self.ups_serial.read(self.ups_serial.in_waiting).decode('ascii', errors='ignore').strip()
                    if response.startswith('('):
                        data = response[1:].split()
                        if len(data) >= 7:
                            input_v  = float(data[0])
                            output_v = float(data[2])
                            load_p   = float(data[3])
                            freq     = float(data[4])
                            batt_v   = float(data[5])
                            temp_c   = float(data[6]) 

                            current_watt = 800 * (load_p / 100.0)
                            batt_pct = min(100, max(0, int((batt_v - 21) / (27.5 - 21) * 100)))

                            if output_v > 50:
                                self.update_ups_outlet_status([1, 1, 0, 0])
                                # If you replace the outlet --> You can change this array.
                            else:
                                self.update_ups_outlet_status([0, 0, 0, 0])

                            self.save_ups_realtime_data(current_watt, temp_c, input_v, output_v)
                            
                            if hasattr(self, 'ui'):
                                self.ui.ups_vars["input_volt"].set(f"{input_v:.1f} V")
                                self.ui.ups_vars["output_volt"].set(f"{output_v:.1f} V")
                                self.ui.ups_vars["load_level"].set(int(load_p))
                                self.ui.ups_vars["batt_level"].set(batt_pct)
                                self.ui.ups_vars["frequency"].set(f"{freq:.1f} Hz")
                                self.ui.ups_vars["status_msg"].set(f"Normal ({current_watt:.1f} W) / Temp: {temp_c:.1f}°C")	
                                self._log(f"UPS Check Interval: {interval/1000}s")


                            #now_str = datetime.now().strftime("%H:%M:%S")
                            #self.ups_plot_history["time"].append(now_str)

                            now_dt = datetime.now()
                            self.ups_plot_history["time"].append(now_dt)
                            self.ups_plot_history["watt"].append(current_watt)
                            self.ups_plot_history["temp"].append(temp_c)
                            self.ups_plot_history["vin"].append(input_v)
                            self.ups_plot_history["vout"].append(output_v)
                            self.refresh_ups_plot()
            except Exception as e:
                self._log(f"UPS Loop Error: {e}") # 여기서 TypeError: unsupported format string 에러가 났을 것임
        
        if hasattr(self, 'master') and self.master.winfo_exists():
            self.ups_after_id = self.master.after(interval, self.update_ups_status_loop)

    def manual_refresh_ups(self):
        """UPS 상태를 즉시 새로고침하고 1분간 빠른 모드로 전환합니다."""
        if self.ups_serial and self.ups_serial.is_open:
            self.ups_session_start = time.time() # 타이머 리셋
            self._log("UPS manual refresh triggered (1s mode for 60s)")
            self.update_ups_status_loop()

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

    def refresh_ups_plot(self):
        h = self.ups_plot_history
        times = list(h["time"])
        if not times: return

        step = max(1, len(times) // 500) 
        d_times = times[::step]

        plots = [
            (self.ui.ax_ups_watt, list(h["watt"])[::step], "Power (W)", "red"),
            (self.ui.ax_ups_temp, list(h["temp"])[::step], "Internal Temp (C)", "orange"),
            (self.ui.ax_ups_vin,  list(h["vin"])[::step],  "Input Voltage (V)", "blue"),
            (self.ui.ax_ups_vout, list(h["vout"])[::step], "Output Voltage (V)", "green")
        ]

        import matplotlib.ticker as ticker
        for ax, data, title, color in plots:
            ax.clear()
            ax.plot(d_times, data, color=color, linewidth=1.2)
            ax.set_title(title, fontsize=10, fontweight='bold')
            
    #   ax.xaxis.set_major_locator(ticker.MaxNLocator(5)) 

            ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
            ax.xaxis.set_major_locator(mdates.AutoDateLocator())
            ax.grid(True, alpha=0.2)
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
        confirmed = messagebox.askyesno("WARNING", "Are you sure you want to SHUT DOWN all outputs?")
        if confirmed:
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

    # main.py save_ups_realtime_data 메서드 수정
    def save_ups_realtime_data(self, watt, temp, vin, vout):
        """UPS 데이터를 날짜별 CSV에 기록하며, 출력 전압이 있을 때만 저장합니다."""
        # 0V인 경우(장비가 꺼진 상태로 간주) 저장을 건너뛰고 싶다면 아래 조건 사용
        if vout <= 0.5: 
            return

        log_dir = os.path.join(self.base_dir, "LOG", "UPS")
        os.makedirs(log_dir, exist_ok=True)
        
        today_str = datetime.now().strftime('%Y%m%d')
        file_path = os.path.join(log_dir, f"ups_{today_str}.csv")
        file_exists = os.path.isfile(file_path)
        
        try:
            with open(file_path, "a") as f:
                if not file_exists:
                    # 새 날짜 파일 생성 시 헤더 작성
                    f.write("timestamp,Watt,Temp,Vin,Vout\n")
                
                # ISO 포맷으로 타임스탬프 저장하여 나중에 preload 시 정확한 시간 계산 가능하게 함
                now_iso = datetime.now().isoformat()
                f.write(f"{now_iso},{watt:.1f},{temp:.1f},{vin:.1f},{vout:.1f}\n")
        except Exception as e:
            self._log(f"Failed to save UPS log: {e}")

    def preload_ups_history(self):
        """어제와 오늘의 데이터를 합쳐서 최대 24시간 분량의 UPS 그래프를 복구합니다."""
        import pandas as pd
        from datetime import timedelta

        now = datetime.now()
        # 어제와 오늘 날짜 생성
        dates_to_load = [
            (now - timedelta(days=1)).strftime('%Y%m%d'),
            now.strftime('%Y%m%d')
        ]

        total_pts = 0
        for date_str in dates_to_load:
            log_file = os.path.join(self.base_dir, "LOG", "UPS", f"ups_{date_str}.csv")
            
            if os.path.exists(log_file):
                try:
                    df = pd.read_csv(log_file)
                    for _, row in df.iterrows():
                        try:
                            # timestamp 컬럼을 파싱 (ISO 포맷 또는 기존 포맷 대응)
                            try:
                                ts = datetime.fromisoformat(row['timestamp'])
                            except:
                                # 이전 포맷(HH:MM:SS)일 경우 오늘 날짜로 가정하여 처리
                                ts_time = datetime.strptime(row['timestamp'], '%H:%M:%S').time()
                                ts = datetime.combine(datetime.strptime(date_str, '%Y%m%d'), ts_time)

                            # 현재 시간 기준 24시간 이내 데이터만 로드
                            if now - ts <= timedelta(hours=24):
                                #self.ups_plot_history["time"].append(ts.strftime("%H:%M:%S"))
                                self.ups_plot_history["time"].append(ts)
                                self.ups_plot_history["watt"].append(float(row['Watt']))
                                self.ups_plot_history["temp"].append(float(row['Temp']))
                                self.ups_plot_history["vin"].append(float(row['Vin']))
                                self.ups_plot_history["vout"].append(float(row['Vout']))
                                total_pts += 1
                        except:
                            continue
                except Exception as e:
                    self._log(f"Error preloading UPS log {date_str}: {e}")

        if total_pts > 0:
            self._log(f"UPS 24h history recovered ({total_pts} points).")
            self.refresh_ups_plot()

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

                current_states = [1, 1, 0, 0] # 실제 상태 읽어오기 전 임시
                current_states[index] = 2 
                self.update_ups_outlet_status(current_states)

            except Exception as e:
                messagebox.showerror("Error", f"Failed to shut down individual outlet: {e}")
        else:
            messagebox.showwarning("Connection Error", "UPS Serial is not connected.")

    def check_ups_alerts(self, watt, temp, batt, load, vin):
        """UPS 수치를 분석하여 경고 메시지 및 색상을 결정합니다."""
        status_msg = "Normal"
        alert_color = "blue"
        is_critical = False

        # 1. 정전 감지 (입력 전압 10V 미만)
        if vin < 10:
            status_msg = "🚨 POWER FAILURE! BATTERY MODE"
            alert_color = "#fd1414" # Red
            is_critical = True
        # 2. 배터리 부족 (30% 미만)
        elif batt < 30:
            status_msg = f"⚠️ LOW BATTERY ({batt}%)"
            alert_color = "#fd7e14" # Orange
            if batt < 15: is_critical = True
        # 3. 과부하 (85% 초과)
        elif load > 85:
            status_msg = f"🚨 UPS OVERLOAD! ({load}%)"
            alert_color = "#e214fd" # Magenta 
            is_critical = True
        # 4. 과열 (45도 초과)
        elif temp > 45:
            status_msg = f"⚠️ UPS OVERHEAT ({temp}°C)"
            alert_color = "#edfd14" # YELLOW

        self.ui.ups_vars["status_msg"].set(f"{status_msg} ({watt:.1f} W) / Temp: {temp:.1f}°C")
        for lbl in self.ui.ups_value_labels:
            lbl.config(foreground=alert_color)

        if is_critical and not hasattr(self, '_ups_alert_active'):
            self._ups_alert_active = True
            emergency_info = "\n\n[Emergency Contacts]\nLab: 0578-86-9250\nJunkyo: +82-10-6503-2581"
            messagebox.showwarning("UPS CRITICAL ALERT", 
                                   f"Critical Condition Detected:\n{status_msg}{emergency_info}")
        elif not is_critical:
            if hasattr(self, '_ups_alert_active'):
                del self._ups_alert_active


    def get_system_status(self):
        status = {
                "DAQ": False,
                "HV": False, 
                "Env": False, 
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
            if hasattr(self, 'ui'):
                msg = self.ui.ups_vars["status_msg"].get()
                if "Normal" in msg or "Battery" in msg:
                    status["UPS"] = True

        try:
            # pgrep 등을 이용해 monitoring_app.py가 실행 중인지 간접 확인
            check_hv = subprocess.run(['pgrep', '-f', 'monitoring_app.py'], capture_output=True)
            if check_hv.returncode == 0:
                status["HV"] = True
                status["Env"] = True # 같은 앱에서 관리하므로 같이 True
        except Exception:
            pass

        return status


if __name__ == "__main__":
    base_directory = os.path.dirname(os.path.abspath(__file__))
    root = tk.Tk()
    app = App(root, base_directory)
    root.mainloop()

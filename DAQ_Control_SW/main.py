# main.py
import tkinter as tk
from PIL import Image, ImageTk
from tkinter import ttk, filedialog, messagebox
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
from datetime import datetime
from ui_manager import UIManager
from config_manager import ConfigManager
from pmt_config_window import PMTConfigWindow

APP_CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".daq_control_config.json")

class App:
    def __init__(self, master, base_dir):
        self.master = master
        self.base_dir = base_dir
        master.title("DAQ Control")
        master.geometry("1600x900")

        icon_path = os.path.join(self.base_dir, 'icons', 'DAQcontroller.png')
        img = Image.open(icon_path)
        self.p_img = ImageTk.PhotoImage(img, master=master) 
        master.iconphoto(True, self.p_img)


        daq_test_dir = "/home/precalkor/ADC/ADC_test/"

        self.start_time = datetime.now()

        self.config_manager = None
        self.terminal_preference = 'gnome-terminal'

        self.load_app_config()

#		config_path = os.path.join(daq_test_dir, 'config2.h')
        self.status_bar = ttk.Frame(master, relief=tk.SUNKEN, padding="2 5")
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)

        self.elapsed_time_var = tk.StringVar()
        ttk.Label(self.status_bar, textvariable=self.elapsed_time_var).pack(side=tk.RIGHT, padx=10)

        self.clock_var = tk.StringVar()
        ttk.Label(self.status_bar, textvariable=self.clock_var).pack(side=tk.LEFT, padx=10)

        self._update_status_bar()

        self.ui = UIManager(master, self)

        if self.config_manager:
            self.validate_config_paths() 
            self.refresh_all_data()
            ip_info = self.get_ip_addresses()
            self.ui.update_ip_display(ip_info)
            self.check_daq_connection()
        else:
            messagebox.showwarning("Warning", "Configuration not loaded. Application will have limited functionality.")

        self.refresh_all_data()

        ip_info = self.get_ip_addresses()
        self.ui.update_ip_display(ip_info)

        self.check_daq_connection()

        # main.py에 추가할 새 함수 (App 클래스 내부에)
    def check_dir_size_queue(self):
        try:
            # 큐에서 모든 메시지를 비동기적으로 가져옴
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

    # --- 로그 처리 기능 ---
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

    # --- [*** 여기가 수정된 부분 (1) ***] ---
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
                    ("Production", self.config_manager.get_config_value("ProcessedDataPath"))
                    ]


            for file_type, base_path in paths_to_scan:
                if not (base_path and os.path.isdir(base_path)):
                    continue
                dirs_to_check = []
                if file_type == "Raw":
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
            self.master.after(0, lambda: self.ui.update_daq_connection_status(is_connected))
            # [MODIFIED] (Req 4) Check every 1 second
            self.master.after(1000, self.check_daq_connection)

    def update_data_directory_size(self):
        if not self.config_manager: return

        raw_data_path = self.config_manager.get_config_value("RawDataPath")

        if raw_data_path and os.path.isdir(raw_data_path):
            data_parent_dir = os.path.dirname(raw_data_path)

            thread = threading.Thread(target=self._get_directory_size_thread, args=(data_parent_dir,), daemon=True)
            thread.start()
        else:
            self.ui.update_data_size_display("Path Error")


    def _get_directory_size_thread(self, path):
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
            self.master.after(0, lambda: self.ui.update_data_size_display(display_str))

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

if __name__ == "__main__":
    base_directory = os.path.dirname(os.path.abspath(__file__))
    root = tk.Tk()
    app = App(root, base_directory)
    root.mainloop()

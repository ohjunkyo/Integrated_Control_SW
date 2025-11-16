# launcher.py
import tkinter as tk
import tkinter.ttk as ttk  
import subprocess
import os
import sys
import time
import signal # [NEW]
from datetime import datetime
from tkinter import messagebox

class AppLauncher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Integrated Control Software Launcher")
        self.geometry("550x550")

        self.configure(bg='#333333')
        button_font = ("Helvetica", 14, "bold")
        label_font = ("Helvetica", 10)
        status_font = ("Helvetica", 10, "bold")
        
        self.processes = []
        
        self.protocol("WM_DELETE_WINDOW", self.on_closing)

        button_frame = tk.Frame(self, bg='#333333')
        button_frame.pack(pady=15, fill=tk.X, padx=20)

        daq_button = tk.Button(button_frame, text="Start DAQ Control", font=button_font, bg="#007ACC", fg="white", padx=20, pady=15, command=self.launch_daq_control)
        daq_button.pack(pady=10, fill=tk.X)

        hv_button = tk.Button(button_frame, text="Start HV Monitor", font=button_font, bg="#5CB85C", fg="white", padx=20, pady=15, command=self.launch_hv_monitor)
        hv_button.pack(pady=10, fill=tk.X)

        vm_button = tk.Button(
            button_frame, text="Start Laser Control (Python)", font=button_font,
            bg="#f0ad4e", fg="white", padx=20, pady=15,
            command=self.launch_laser_control
        )
        vm_button.pack(pady=10, fill=tk.X)

        status_frame = ttk.LabelFrame(self, text="Launcher Status", padding="10")
        status_frame.pack(fill=tk.X, expand=True, padx=20, pady=10)
        
        self.start_time = datetime.now()
        self.start_time_str = self.start_time.strftime('%Y-%m-%d %H:%M:%S')
        
        self.current_time_var = tk.StringVar()
        self.elapsed_time_var = tk.StringVar()

        self._create_status_row(status_frame, "Start Time:", self.start_time_str, label_font, status_font)
        self._create_status_row(status_frame, "Current Time:", self.current_time_var, label_font, status_font)
        self._create_status_row(status_frame, "Elapsed Time:", self.elapsed_time_var, label_font, status_font)

        ttk.Separator(status_frame, orient='horizontal').pack(fill='x', pady=10)

        self.last_mod_file_var = tk.StringVar()
        self.last_mod_time_var = tk.StringVar()
        
        self._create_status_row(status_frame, "Last Modified File:", self.last_mod_file_var, label_font, status_font)
        self._create_status_row(status_frame, "Modified Time:", self.last_mod_time_var, label_font, status_font)
        
        refresh_button = ttk.Button(status_frame, text="Refresh Status 🔄", command=self.update_file_status)
        refresh_button.pack(pady=10)

        exit_button = tk.Button(self, text="Exit Launcher (and All Apps)", font=("Helvetica", 10), bg="#dc3545", fg="white", command=self.on_closing)
        exit_button.pack(pady=15, padx=20)

        self.update_clock()
        self.update_file_status()

    # --- [MODIFIED] Process termination functions ---
    def terminate_all_processes(self):
        """
        Terminates all processes launched by this launcher
        by killing the entire process group.
        """
        print("Terminating all launched processes and their children...")
        for proc in self.processes:
            if proc.poll() is None: # If process is still running
                pgid = 0
                try:
                    # Get the process group ID (PGID)
                    pgid = os.getpgid(proc.pid)
                    print(f"  - Terminating Process Group {pgid} (Parent PID {proc.pid})...")
                    # Send SIGTERM to the entire process group
                    os.killpg(pgid, signal.SIGTERM)
                    # Wait up to 2 seconds for graceful termination
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    print(f"  - Process Group {pgid} did not terminate, killing...")
                    # Send SIGKILL to the entire process group
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                     print(f"  - Process {proc.pid} (PGID {pgid}) already gone.")
                except Exception as e:
                    print(f"  - Error terminating PGID {pgid} (PID {proc.pid}): {e}")
        print("All processes terminated.")

    def on_closing(self):
        """Called on window close or 'Exit' button press."""
        running_processes = [p for p in self.processes if p.poll() is None]
        
        if running_processes:
            msg = f"Do you want to exit the launcher and terminate all {len(running_processes)} running application(s)?\n\n(DAQ, HV, Laser)"
            if messagebox.askyesno("Confirm Exit", msg):
                self.terminate_all_processes()
                self.destroy()
        else:
            if messagebox.askyesno("Confirm Exit", "Do you want to exit the launcher?"):
                self.destroy()

    # --------------------------------------------------

    def _create_status_row(self, parent, label_text, string_var, label_font, status_font):
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
        return "python3"

    def launch_daq_control(self):
        print("Launching DAQ Control...")
        python_exe = self.get_python_executable()
        
        script_path = os.path.join("DAQ_Control_SW", "main.py")
        script_dir = os.path.dirname(script_path)
        if not script_dir: script_dir = "."
        command = [python_exe, os.path.basename(script_path)]
        
        try:
            # [MODIFIED] Use preexec_fn=os.setsid to create a new process group
            proc = subprocess.Popen(command, cwd=script_dir, preexec_fn=os.setsid)
            self.processes.append(proc)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to launch DAQ Control:\n{e}")


    def launch_hv_monitor(self):
        print("Launching HV Monitor...")
        python_exe = self.get_python_executable()

        script_path = os.path.join("HV_Control_SW", "monitoring_app.py")
        config_path = os.path.join("HV_Control_SW", "config_precal.json")
        script_dir = os.path.dirname(script_path)
        if not script_dir: script_dir = "."
        
        command = [python_exe, os.path.basename(script_path), os.path.basename(config_path)]
            
        try:
            # [MODIFIED] Use preexec_fn=os.setsid to create a new process group
            proc = subprocess.Popen(command, cwd=script_dir, preexec_fn=os.setsid)
            self.processes.append(proc)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to launch HV Monitor:\n{e}")

    def launch_laser_control(self):
        print("Launching Laser Control (Python)...")
        python_exe = self.get_python_executable()
        
        script_path = os.path.join("Laser_Contorl_SW", "app", "laser_gui.py")
        script_dir = os.path.dirname(script_path)
        if not script_dir: script_dir = "."

        if not os.path.exists(script_path):
            messagebox.showerror("Error", f"Laser script not found:\n{script_path}")
            return
        
        command = [python_exe, os.path.basename(script_path)]

        try:
            # [MODIFIED] Use preexec_fn=os.setsid to create a new process group
            proc = subprocess.Popen(command, cwd=script_dir, preexec_fn=os.setsid)
            self.processes.append(proc)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to launch Laser Control:\n{e}")

    def update_clock(self):
        now = datetime.now()
        self.current_time_var.set(now.strftime('%Y-%m-%d %H:%M:%S'))
        elapsed = now - self.start_time
        total_seconds = int(elapsed.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        self.elapsed_time_var.set(f"{hours:02}:{minutes:02}:{seconds:02}")
        self.after(1000, self.update_clock)

    def find_most_recent_file(self, *dirs_to_scan):
        most_recent_file = None
        max_mtime = 0
        IGNORE_DIRS = {'__pycache__', '.git', 'venv', 'icons', 'logs'}
        IGNORE_EXTS = {'.db', '.log', '.png', '.jpg', '.ico', '.sqlite3', '.json'}

        for directory in dirs_to_scan:
            directory = os.path.expanduser(directory)
            if not os.path.isdir(directory):
                print(f"Warning: Directory not found, skipping: {directory}")
                continue
                
            for root, dirs, files in os.walk(directory, topdown=True):
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
        print("Refreshing file status...")
        try:
            # 레이저 컨트롤 SW 디렉토리도 스캔에 추가
            file_path, mtime = self.find_most_recent_file("DAQ_Control_SW", "HV_Control_SW", "Laser_Contorl_SW")
            if file_path:
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

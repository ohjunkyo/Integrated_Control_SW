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
        # Auto-reap children (no zombies, no false "already running" hits).
        signal.signal(signal.SIGCHLD, signal.SIG_IGN)
        # Ignore SIGHUP so closing the launch terminal doesn't kill the launcher.
        signal.signal(signal.SIGHUP, signal.SIG_IGN)

        self.title("Integrated Control Software Launcher")
        self.geometry("550x800")

        self.configure(bg='#333333')
        button_font = ("Helvetica", 14, "bold")
        label_font = ("Helvetica", 10)
        status_font = ("Helvetica", 10, "bold")

        self.processes = []

        self.protocol("WM_DELETE_WINDOW", self._hide_to_tray)

        button_frame = tk.Frame(self, bg='#333333')
        button_frame.pack(pady=15, fill=tk.X, padx=20)

        daq_button = tk.Button(button_frame, text="Start DAQ Control", font=button_font, bg="#007ACC", fg="white", padx=20, pady=15, command=self.launch_daq_control)
        daq_button.pack(pady=10, fill=tk.X)

        test_button = tk.Button(button_frame, text="Start DAQ (TEST MODE)", font=button_font, bg="#f0ad4e", fg="black", padx=20, pady=15, command=self.launch_test_control)
        test_button.pack(pady=10, fill=tk.X)

        hv_button = tk.Button(button_frame, text="Start HV Monitor", font=button_font, bg="#5CB85C", fg="white", padx=20, pady=15, command=self.launch_hv_monitor)
        hv_button.pack(pady=10, fill=tk.X)

        vm_button = tk.Button(
                button_frame, text="[Old version] Laser Control (Python)", font=button_font,
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

        btn_row = tk.Frame(self, bg='#333333')
        btn_row.pack(pady=15, padx=20, fill=tk.X)

        tk.Button(btn_row, text="⬇ Minimize to Tray", font=("Helvetica", 10),
                  bg="#555555", fg="white", command=self._hide_to_tray).pack(
                      side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 5))

        tk.Button(btn_row, text="Exit Launcher (and All Apps)", font=("Helvetica", 10),
                  bg="#dc3545", fg="white", command=self.on_closing).pack(
                      side=tk.LEFT, expand=True, fill=tk.X)

        self.update_clock()
        self.update_file_status()

    def is_process_running(self, script_keyword):
        """Return True only if a non-zombie process matching script_keyword exists."""
        try:
            # pgrep finds PIDs; then check each is not a zombie (stat != Z)
            r = subprocess.run(['pgrep', '-f', script_keyword], capture_output=True, text=True)
            if r.returncode != 0:
                return False
            for pid in r.stdout.split():
                stat_path = f"/proc/{pid.strip()}/status"
                try:
                    with open(stat_path) as f:
                        for line in f:
                            if line.startswith("State:"):
                                if "Z" in line:  # zombie — ignore
                                    break
                                return True  # alive, non-zombie
                except OSError:
                    pass  # process already gone
            return False
        except Exception:
            return False

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

    def _hide_to_tray(self):
        """Hide the main window and show a small tray-like floating button."""
        self.withdraw()
        if hasattr(self, '_tray_win') and self._tray_win.winfo_exists():
            return
        tray = tk.Toplevel()
        self._tray_win = tray
        tray.title("")
        tray.resizable(False, False)
        tray.attributes("-topmost", True)
        tray.overrideredirect(True)   # no title bar

        # Position: bottom-right corner
        sw, sh = tray.winfo_screenwidth(), tray.winfo_screenheight()
        w, h = 220, 36
        tray.geometry(f"{w}x{h}+{sw - w - 10}+{sh - h - 50}")

        frame = tk.Frame(tray, bg="#1a1a2e", bd=1, relief="solid")
        frame.pack(fill=tk.BOTH, expand=True)

        tk.Label(frame, text="🔬 ICS Launcher",
                 bg="#1a1a2e", fg="#aaaaff",
                 font=("Helvetica", 9, "bold")).pack(side=tk.LEFT, padx=8)

        tk.Button(frame, text="▲ Open", bg="#007ACC", fg="white",
                  font=("Helvetica", 8, "bold"), relief="flat", padx=6,
                  command=self._show_from_tray).pack(side=tk.RIGHT, padx=4, pady=4)

        tk.Button(frame, text="✕", bg="#555", fg="white",
                  font=("Helvetica", 8), relief="flat", padx=4,
                  command=self.on_closing).pack(side=tk.RIGHT, pady=4)

        # Allow dragging the tray widget
        frame.bind("<ButtonPress-1>", self._tray_drag_start)
        frame.bind("<B1-Motion>", self._tray_drag_move)

    def _show_from_tray(self):
        if hasattr(self, '_tray_win') and self._tray_win.winfo_exists():
            self._tray_win.destroy()
        self.deiconify()
        self.lift()

    def _tray_drag_start(self, event):
        self._drag_x = event.x_root - self._tray_win.winfo_x()
        self._drag_y = event.y_root - self._tray_win.winfo_y()

    def _tray_drag_move(self, event):
        x = event.x_root - self._drag_x
        y = event.y_root - self._drag_y
        self._tray_win.geometry(f"+{x}+{y}")

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
        script_path = os.path.abspath(os.path.join("DAQ_Control_SW", "main.py"))
        test_script_path = os.path.abspath(os.path.join("DAQ_Control_SW", "main_test.py"))
        if self.is_process_running(test_script_path):
            messagebox.showerror("Hardware Collision Alert", "⚠️ Test Mode is currently running!\n\nPlease close Test Mode before starting Production.")
            return

        if self.is_process_running(script_path):
            messagebox.showwarning("Already Running", "DAQ Control Panel is already running.")
            return

        print(f"Launching DAQ Control: {script_path}")
        python_exe = self.get_python_executable()

        script_dir = os.path.dirname(script_path)
        
        command = [python_exe, script_path] 

        try:
            proc = subprocess.Popen(command, cwd=script_dir, preexec_fn=os.setsid)
            self.processes.append(proc)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to launch DAQ Control:\n{e}")

    def launch_test_control(self):
        prod_script_path = os.path.abspath(os.path.join("DAQ_Control_SW", "main.py"))
        test_script_path = os.path.abspath(os.path.join("DAQ_Control_SW", "main_test.py"))

        if self.is_process_running(prod_script_path):
            messagebox.showerror("Hardware Collision Alert", "⚠️ Production (main.py) is currently running!\n\nPlease close the real DAQ Control Panel before starting Test Mode.")
            return

        if self.is_process_running(test_script_path):
            messagebox.showwarning("Already Running", "Test Mode is already running.")
            return

        print(f"Launching Test Mode: {test_script_path}")
        python_exe = self.get_python_executable()
        script_dir = os.path.dirname(test_script_path)

        if not os.path.exists(test_script_path):
            messagebox.showerror("File Not Found", f"Test script not found:\n{test_script_path}\n\nPlease create main_test.py first.")
            return

        command = [python_exe, test_script_path]

        try:
            proc = subprocess.Popen(command, cwd=script_dir, preexec_fn=os.setsid)
            self.processes.append(proc)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to launch Test Mode:\n{e}")


    def launch_hv_monitor(self):
        script_path = os.path.abspath(os.path.join("HV_Control_SW", "monitoring_app.py"))
        
        if self.is_process_running(script_path): 
            messagebox.showwarning("Already Running", "HV Monitor is already running.")
            return

        print(f"Launching HV Monitor: {script_path}")
        python_exe = self.get_python_executable()

        config_path = os.path.abspath(os.path.join("HV_Control_SW", "config_precal.json"))
        script_dir = os.path.dirname(script_path)

        command = [python_exe, script_path, config_path]

        try:
            proc = subprocess.Popen(command, cwd=script_dir, preexec_fn=os.setsid)
            self.processes.append(proc)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to launch HV Monitor:\n{e}")

    def launch_laser_control(self):
        script_path = os.path.abspath(os.path.join("Laser_Control_SW", "app", "laser_gui.py"))

        if self.is_process_running(script_path): 
            messagebox.showwarning("Already Running", "Laser Control is already running.")
            return
            
        print(f"Launching Laser Control: {script_path}")
        python_exe = self.get_python_executable()
        script_dir = os.path.dirname(script_path)

        if not os.path.exists(script_path):
            messagebox.showerror("Error", f"Laser script not found:\n{script_path}")
            return

        command = [python_exe, script_path]

        try:
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
            file_path, mtime = self.find_most_recent_file("DAQ_Control_SW", "HV_Control_SW", "Laser_Control_SW")
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

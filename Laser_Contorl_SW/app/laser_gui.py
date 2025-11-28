#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Tamadenshi Laser Control GUI (Python 3 / tkinter)
Updated Version: Includes Log Rotation, CSV Data Logging, Robust Plotting, and Stable Connection.
"""

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, font, Toplevel
import os
import sys
import logging
from logging.handlers import TimedRotatingFileHandler
import glob
import queue
import json
import csv
from datetime import datetime
from threading import Timer, Thread
from typing import Optional

# --- Plotting Imports ---
try:
    import pandas as pd
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    print("Warning: 'matplotlib' or 'pandas' not found. Plotting tab will be disabled.")
    MATPLOTLIB_AVAILABLE = False

# --- Driver Import ---
try:
    from laser_driver import TamadenshiLaser
except ImportError:
    print("Error: 'laser_driver.py' file not found.")
    sys.exit(1)

# --- Path Configuration ---
LOG_DIR = os.path.expanduser("~/ADC/ADC_test/LOG/LASER")
os.makedirs(LOG_DIR, exist_ok=True)
CONFIG_FILE = os.path.expanduser("~/.laser_control_config.json")

# --- Logger Setup (TimedRotatingFileHandler) ---
# 자정(midnight)마다 파일을 교체하여 날짜별로 로그를 분리합니다.
log = logging.getLogger('LaserControl')
log.setLevel(logging.INFO)

if not log.hasHandlers():
    # 파일 핸들러: 날짜별 자동 회전 (매일 자정에 새 파일)
    log_filename = os.path.join(LOG_DIR, "laser_log")
    handler = TimedRotatingFileHandler(filename=log_filename, when='midnight', interval=1, encoding='utf-8')
    handler.suffix = "_%Y-%m-%d.txt"  # 파일명 뒤에 날짜 붙임
    handler.setFormatter(logging.Formatter('%(asctime)s | %(levelname)s | %(message)s'))
    log.addHandler(handler)
    
    # 콘솔 핸들러 (에러만 출력하거나 간략하게)
    console = logging.StreamHandler()
    console.setLevel(logging.WARNING) # 콘솔에는 경고/에러만 출력 (너무 시끄러움 방지)
    console.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
    log.addHandler(console)
    
    log.propagate = False

# ------------------

class RepeatingTimer(Timer):
    """A Timer that repeats its function call"""
    def run(self):
        while not self.finished.wait(self.interval):
            self.function(*self.args, **self.kwargs)

class LaserControlApp:
    
    def __init__(self, master):
        self.master = master
        self.master.title("Laser Control (Stable)")
        self.master.geometry("550x920") 

        self.laser = TamadenshiLaser()
        self.status_monitor_timer: Optional[RepeatingTimer] = None
        self.is_monitoring = tk.BooleanVar(value=False)
        self.current_status_text = tk.StringVar(value="Current Status: N/A")
        
        self.gui_queue = queue.Queue()
        self.plot_window: Optional[Toplevel] = None 
        
        # Connection Stability Variables
        self.consecutive_errors = 0
        self.MAX_RETRIES = 5  # 5번 연속 실패해야 연결 끊김 처리
        
        # Time variables
        self.start_time = datetime.now()
        self.clock_var = tk.StringVar()
        self.elapsed_time_var = tk.StringVar()

        # --- Status Indicator Variables ---
        self.live_status = {
            "ld_status": tk.StringVar(value="OFF"),
            "tec_status": tk.StringVar(value="OFF"),
            "temp": tk.StringVar(value="--.- °C"),
            "bias": tk.StringVar(value="---.- mA"),
            "pulse": tk.StringVar(value="---.- mA")
        }
        
        # --- Internal Frequency Variables ---
        self.trigger_var = tk.StringVar(value="External")
        self.internal_freq_hz = tk.StringVar(value="10000000") 

        # --- Current Control Variables ---
        self.bias_val = tk.DoubleVar(value=0.0)
        self.pulse_val = tk.DoubleVar(value=0.0)

        # --- Style Configuration ---
        self._configure_styles()

        self.master.protocol("WM_DELETE_WINDOW", self.on_closing)

        # --- Load Settings ---
        self._load_settings()

        # --- Create Widgets ---
        self._create_connection_frame()
        self._create_live_status_frame()
        self._create_main_control_frame()
        self._create_current_control_frame()
        self._create_log_frame()
        self._create_status_bar()
        
        self.log_message("Laser Control GUI started.")
        self.process_gui_queue()
        self._update_status_bar_clock()
        self.auto_connect()

    def _configure_styles(self):
        style = ttk.Style()
        style.configure("TButton", padding=6, relief="flat", font=("Helvetica", 10))
        style.configure("Bold.TButton", padding=6, font=("Helvetica", 10, "bold"), relief="raised")
        style.map("Bold.TButton", background=[('active', '#0056b3')], foreground=[('active', 'white')])
        style.configure("Connect.TButton", padding=6, font=("Helvetica", 10, "bold"), relief="raised")
        style.map("Connect.TButton", background=[('!disabled', '#28a745'), ('active', '#218838')], foreground=[('!disabled', 'white'), ('active', 'white')])
        style.configure("Disconnect.TButton", padding=6, font=("Helvetica", 10, "bold"), relief="raised")
        style.map("Disconnect.TButton", background=[('!disabled', '#dc3545'), ('active', '#c82333')], foreground=[('!disabled', 'white'), ('active', 'white')])
        style.configure("Toolbutton", padding=5, font=("Helvetica", 10))

    def log_message(self, msg: str, level: str = "info"):
        """Logs to file (rotating) and GUI. Prints are minimized."""
        # 1. Log to file (Rolling handled by TimedRotatingFileHandler)
        if level == "info":
            log.info(msg)
        elif level == "warning":
            log.warning(msg)
        elif level == "error":
            log.error(msg)

        # 2. Log to GUI Window
        if hasattr(self, 'session_log_text'):
            timestamp = datetime.now().strftime('%H:%M:%S')
            try:
                self.session_log_text.config(state="normal")
                self.session_log_text.insert(tk.END, f"[{timestamp}] {msg}\n")
                self.session_log_text.config(state="disabled")
                self.session_log_text.yview(tk.END)
            except tk.TclError:
                pass

    def _create_connection_frame(self):
        frame = ttk.LabelFrame(self.master, text="Connection")
        frame.pack(fill=tk.X, padx=10, pady=(10, 5))
        
        self.conn_status_label = ttk.Label(frame, text="Status: Disconnected", foreground="red", font=("Helvetica", 10, "bold"))
        self.conn_status_label.pack(side=tk.LEFT, padx=5, expand=True)
        
        self.connect_btn = ttk.Button(frame, text="Retry Connect", command=self.auto_connect, style="Connect.TButton")
        self.connect_btn.pack(side=tk.RIGHT, padx=5)

    def _create_live_status_frame(self):
        frame = ttk.LabelFrame(self.master, text="Live Status")
        frame.pack(fill=tk.X, padx=10, pady=5)
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(3, weight=1)

        def create_indicator(r, c, text, var_name):
            ttk.Label(frame, text=f"{text}:", font=("Helvetica", 10, "bold")).grid(row=r, column=c, sticky=tk.W, padx=5, pady=3)
            label = ttk.Label(frame, textvariable=self.live_status[var_name], font=("Helvetica", 10), relief="sunken", padding=(5, 2), anchor=tk.E)
            label.grid(row=r, column=c+1, sticky=tk.EW, padx=5, pady=3)

        create_indicator(0, 0, "LD Status", "ld_status")
        create_indicator(1, 0, "Bias Current", "bias")
        create_indicator(0, 2, "TEC Status", "tec_status")
        create_indicator(1, 2, "Pulse Current", "pulse")
        create_indicator(2, 0, "Temperature", "temp") # Grid logic handles span if not specified, keeping simple

    def _create_main_control_frame(self):
        frame = ttk.LabelFrame(self.master, text="Main Control")
        frame.pack(fill=tk.X, padx=10, pady=5)
        
        # LD
        ld_frame = ttk.Frame(frame)
        ld_frame.pack(fill=tk.X, padx=5, pady=5)
        ttk.Label(ld_frame, text="Laser (LD):", width=12).pack(side=tk.LEFT)
        self.ld_on_btn = ttk.Button(ld_frame, text="ON", style="Connect.TButton", command=lambda: self.set_ld_on(True))
        self.ld_on_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        self.ld_off_btn = ttk.Button(ld_frame, text="OFF", style="Disconnect.TButton", command=lambda: self.set_ld_on(False))
        self.ld_off_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)

        # TEC
        tec_frame = ttk.Frame(frame)
        tec_frame.pack(fill=tk.X, padx=5, pady=5)
        ttk.Label(tec_frame, text="Temp (TEC):", width=12).pack(side=tk.LEFT)
        self.tec_on_btn = ttk.Button(tec_frame, text="ON", style="Connect.TButton", command=lambda: self.set_tec_on(True))
        self.tec_on_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        self.tec_off_btn = ttk.Button(tec_frame, text="OFF", style="Disconnect.TButton", command=lambda: self.set_tec_on(False))
        self.tec_off_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        
        # Trigger
        trigger_frame = ttk.Frame(frame)
        trigger_frame.pack(fill=tk.X, pady=5, padx=5)
        ttk.Label(trigger_frame, text="Trigger:", width=12).pack(side=tk.LEFT)
        self.trigger_combo = ttk.Combobox(trigger_frame, textvariable=self.trigger_var, 
                                          values=["External", "Internal (PG1)", "Internal (PG2)"], state="readonly")
        self.trigger_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        self.trigger_combo.bind("<<ComboboxSelected>>", self.on_trigger_select)
        
        # Freq
        self.freq_frame = ttk.LabelFrame(frame, text="Internal Trigger Control (Hz)")
        self.freq_frame.pack(fill=tk.X, padx=5, pady=5)
        freq_entry_frame = ttk.Frame(self.freq_frame)
        freq_entry_frame.pack(fill=tk.X, padx=5, pady=2)
        self.freq_label = ttk.Label(freq_entry_frame, text="Frequency (Hz):", width=15)
        self.freq_label.pack(side=tk.LEFT)
        self.freq_entry = ttk.Entry(freq_entry_frame, textvariable=self.internal_freq_hz)
        self.freq_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.freq_apply_btn = ttk.Button(self.freq_frame, text="Apply Frequency", command=self.apply_frequency, style="Bold.TButton")
        self.freq_apply_btn.pack(fill=tk.X, ipady=5, padx=5, pady=5)
        
        self.on_trigger_select()

    def _create_current_control_frame(self):
        frame = ttk.LabelFrame(self.master, text="Current Control (mA)")
        frame.pack(fill=tk.X, padx=10, pady=5)
        
        self._create_slider_entry_pair(frame, "Bias Current:", self.bias_val, 0.0, 200.0)
        self._create_slider_entry_pair(frame, "Pulse Current:", self.pulse_val, 0.0, 200.0)

        ttk.Separator(frame).pack(fill=tk.X, pady=10)
        self.apply_btn = ttk.Button(frame, text="Apply Currents", command=self.apply_currents, style="Bold.TButton")
        self.apply_btn.pack(fill=tk.X, ipady=5)

    def _create_slider_entry_pair(self, parent, label_text, var, from_, to):
        frame = ttk.Frame(parent)
        frame.pack(fill=tk.X, pady=5)
        ttk.Label(frame, text=label_text, width=15).pack(side=tk.LEFT, padx=5)
        slider = ttk.Scale(frame, from_=from_, to=to, variable=var, orient=tk.HORIZONTAL,
                           command=lambda v: var.set(f"{float(v):.2f}"))
        slider.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        entry = ttk.Entry(frame, textvariable=var, width=7)
        entry.pack(side=tk.RIGHT, padx=5)
        
    def _create_log_frame(self):
        frame = ttk.LabelFrame(self.master, text="Log Viewer & Data")
        frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(5, 10))
        
        self.log_notebook = ttk.Notebook(frame)
        self.log_notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # Session Log
        session_tab = ttk.Frame(self.log_notebook, padding=5)
        self.log_notebook.add(session_tab, text="Session Log")
        self.session_log_text = scrolledtext.ScrolledText(session_tab, wrap=tk.WORD, state="disabled", height=10, bg="#f8f9fa", font=("Monaco", 9))
        self.session_log_text.pack(fill=tk.BOTH, expand=True)

        # Plotter
        plot_tab = ttk.Frame(self.log_notebook, padding=5)
        if MATPLOTLIB_AVAILABLE:
            self.log_notebook.add(plot_tab, text="Data Plotter")
            plot_controls = ttk.Frame(plot_tab)
            plot_controls.pack(fill=tk.X, pady=5)
            
            ttk.Label(plot_controls, text="Select CSV:").pack(side=tk.LEFT, padx=(0, 5))
            self.csv_file_var = tk.StringVar()
            self.csv_combo = ttk.Combobox(plot_controls, textvariable=self.csv_file_var, state="readonly", width=30)
            self.csv_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
            
            ttk.Button(plot_controls, text="Plot Window", command=self.plot_csv_data_popup).pack(side=tk.LEFT, padx=5)
            ttk.Button(plot_controls, text="Refresh", command=self.populate_csv_combo).pack(side=tk.LEFT)
            
            ttk.Label(plot_tab, text=f"Data is auto-saved to:\n{LOG_DIR}", justify=tk.CENTER, foreground="gray").pack(fill=tk.BOTH, expand=True)
        else:
            self.log_notebook.add(plot_tab, text="Data Plotter (Disabled)")
            ttk.Label(plot_tab, text="Install matplotlib/pandas to enable.").pack(pady=20)
        
        self.populate_csv_combo()

    def _create_status_bar(self):
        status_bar_frame = ttk.Frame(self.master, relief=tk.SUNKEN, padding=(5, 3))
        status_bar_frame.pack(side=tk.BOTTOM, fill=tk.X)
        self.status_bar_label = ttk.Label(status_bar_frame, textvariable=self.current_status_text, anchor=tk.W)
        self.status_bar_label.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        
        self.monitor_check = ttk.Checkbutton(status_bar_frame, text="Monitor", variable=self.is_monitoring, command=self.toggle_monitoring, style="Toolbutton")
        self.monitor_check.pack(side=tk.RIGHT, padx=5)
        self.elapsed_time_label = ttk.Label(status_bar_frame, textvariable=self.elapsed_time_var, anchor=tk.E)
        self.elapsed_time_label.pack(side=tk.RIGHT, padx=10)
        self.clock_label = ttk.Label(status_bar_frame, textvariable=self.clock_var, anchor=tk.E)
        self.clock_label.pack(side=tk.RIGHT, padx=10)

    def _update_status_bar_clock(self):
        try:
            now = datetime.now()
            self.clock_var.set(f"Time: {now.strftime('%Y-%m-%d %H:%M:%S')}")
            elapsed = now - self.start_time
            m, s = divmod(elapsed.seconds, 60)
            h, m = divmod(m, 60)
            self.elapsed_time_var.set(f"Elapsed: {h+elapsed.days*24:02}:{m:02}:{s:02}")
            self.master.after(1000, self._update_status_bar_clock)
        except tk.TclError:
            pass

    # --- Configuration ---
    def _load_settings(self):
        try:
            with open(CONFIG_FILE, 'r') as f:
                settings = json.load(f)
            self.bias_val.set(settings.get("bias_ma", 0.0))
            self.pulse_val.set(settings.get("pulse_ma", 0.0))
            self.trigger_var.set(settings.get("trigger_mode", "External"))
            self.internal_freq_hz.set(settings.get("internal_freq_hz", "10000000"))
        except:
            pass # Use defaults

    def _save_settings(self):
        try:
            settings = {
                "bias_ma": self.bias_val.get(),
                "pulse_ma": self.pulse_val.get(),
                "trigger_mode": self.trigger_var.get(),
                "internal_freq_hz": self.internal_freq_hz.get()
            }
            with open(CONFIG_FILE, 'w') as f:
                json.dump(settings, f, indent=4)
        except:
            pass

    # --- Device Control ---
    def auto_connect(self):
        self.log_message("Connecting to device...")
        success, msg = self.laser.connect()
        if success:
            self.conn_status_label.config(text="Status: Connected", foreground="green")
            self.log_message(f"Device connected. {msg}")
            self.connect_btn.config(text="Disconnect", command=self.disconnect, style="Disconnect.TButton")
            self.consecutive_errors = 0 # Reset error counter
            
            # Init state
            self.on_trigger_select(init=True)
            self.apply_currents()
            self.set_ld_on(False)
            self.set_tec_on(False)
            
            self.is_monitoring.set(True)
            self.toggle_monitoring()
        else:
            self.conn_status_label.config(text="Status: Disconnected", foreground="red")
            self.log_message(f"Connection failed: {msg}", "error")
            self.connect_btn.config(text="Retry Connect", command=self.auto_connect, style="Connect.TButton")

    def disconnect(self):
        self.log_message("Disconnecting...")
        self.is_monitoring.set(False)
        self.toggle_monitoring()
        self.safe_shutdown_device()
        self.laser.disconnect()
        self.handle_disconnection_ui()

    def set_ld_on(self, state: bool):
        if not self.laser.is_connected(): return
        if self.laser.set_ld_on(state):
            self.log_message(f"SET_LD: {'ON' if state else 'OFF'}")
        else:
            self.log_message("Failed to set LD state", "error")

    def set_tec_on(self, state: bool):
        if not self.laser.is_connected(): return
        if self.laser.set_tec_on(state):
            self.log_message(f"SET_TEC: {'ON' if state else 'OFF'}")
        else:
            self.log_message("Failed to set TEC state", "error")

    def on_trigger_select(self, event=None, init: bool = False):
        mode = self.trigger_var.get()
        if mode == "External":
            self.freq_frame.config(text="Frequency Control - DISABLED")
            self.freq_entry.state(['disabled'])
            self.freq_apply_btn.state(['disabled'])
        else:
            self.freq_frame.config(text="Frequency Control - ENABLED")
            self.freq_entry.state(['!disabled'])
            self.freq_apply_btn.state(['!disabled'])
            if mode == "Internal (PG1)":
                self.freq_label.config(text="PG1 (100k-250M):")
            else:
                self.freq_label.config(text="PG2 (3k-200k):")
            if not init: self.apply_frequency()

        if not init: 
            self.set_trigger()

    def set_trigger(self):
        if not self.laser.is_connected(): return
        mode = self.trigger_var.get()
        pg1, pg2, ext = (mode=="Internal (PG1)"), (mode=="Internal (PG2)"), (mode=="External")
        if self.laser.set_trigger_mode(pg1=pg1, pg2=pg2, ext=ext):
            self.log_message(f"SET_TRIGGER: {mode}")

    def apply_frequency(self):
        if not self.laser.is_connected(): return
        mode = self.trigger_var.get()
        if mode == "External": return
        try:
            hz = int(self.internal_freq_hz.get())
            if mode == "Internal (PG1)": self.laser.set_pg1_frequency(hz)
            else: self.laser.set_pg2_frequency(hz)
            self.log_message(f"SET_FREQ: {hz} Hz")
        except:
            messagebox.showerror("Error", "Invalid Frequency")

    def apply_currents(self):
        if not self.laser.is_connected(): return
        try:
            b, p = self.bias_val.get(), self.pulse_val.get()
            self.laser.set_bias_current(b)
            self.laser.set_pulse_current(p)
            self.log_message(f"SET_CURRENTS: Bias={b:.2f}, Pulse={p:.2f}")
        except:
            pass

    # --- Monitoring & CSV Logging ---
    def process_gui_queue(self):
        try:
            while not self.gui_queue.empty():
                msg_type, data = self.gui_queue.get_nowait()
                if msg_type == "status":
                    self.update_gui_with_status(data)
                elif msg_type == "disconnect":
                    self.handle_disconnection_ui()
        except: pass
        finally:
            self.master.after(200, self.process_gui_queue)

    def toggle_monitoring(self):
        if self.is_monitoring.get():
            if self.status_monitor_timer is None and self.laser.is_connected():
                # [FIX] 주기를 0.05 -> 0.5로 변경하여 부하 감소
                self.status_monitor_timer = RepeatingTimer(0.5, self.read_status_to_queue)
                self.status_monitor_timer.daemon = True 
                self.status_monitor_timer.start()
        else:
            if self.status_monitor_timer:
                self.status_monitor_timer.cancel()
                self.status_monitor_timer = None
                self.current_status_text.set("Current Status: N/A")

    def read_status_to_queue(self):
        """Background thread: reads status and writes CSV."""
        if not self.laser.is_connected():
            return
            
        if self.laser.update_status():
            self.consecutive_errors = 0 # Success, reset error count
            status = self.laser.status.copy()
            
            # --- [FEATURE] Save Data to CSV ---
            self.save_csv_data(status)
            # ----------------------------------
            
            self.gui_queue.put(("status", status))
        else:
            # [FIX] Retry logic
            self.consecutive_errors += 1
            if self.consecutive_errors > self.MAX_RETRIES:
                self.gui_queue.put(("disconnect", None))

    def save_csv_data(self, status):
        """Appends status data to a daily CSV file."""
        try:
            now = datetime.now()
            date_str = now.strftime("%Y-%m-%d")
            filename = f"laser_data_{date_str}.csv"
            filepath = os.path.join(LOG_DIR, filename)
            
            file_exists = os.path.isfile(filepath)
            
            with open(filepath, 'a', newline='') as f:
                writer = csv.writer(f)
                # Header if new file
                if not file_exists:
                    writer.writerow(["timestamp", "ld_on", "tec_on", "temp_c", "bias_ma", "pulse_ma"])
                
                # Data row
                writer.writerow([
                    now.strftime("%Y-%m-%d %H:%M:%S.%f"),
                    1 if status.get('ld_on') else 0,
                    1 if status.get('tec_on') else 0,
                    f"{status.get('ld_temp', 0):.2f}",
                    f"{status.get('bias', 0):.2f}",
                    f"{status.get('pulse', 0):.2f}"
                ])
        except Exception:
            pass # Don't crash monitoring on file error

    def update_gui_with_status(self, status):
        ld, tec = status.get('ld_on'), status.get('tec_on')
        temp, bias, pulse = status.get('ld_temp', 0), status.get('bias', 0), status.get('pulse', 0)

        self.live_status["ld_status"].set("ON" if ld else "OFF")
        self.live_status["tec_status"].set("ON" if tec else "OFF")
        self.live_status["temp"].set(f"{temp:.2f} °C")
        self.live_status["bias"].set(f"{bias:.2f} mA")
        self.live_status["pulse"].set(f"{pulse:.2f} mA")
        
        self.current_status_text.set(f"Status: LD={'ON' if ld else 'OFF'}, Temp={temp:.1f}C, Bias={bias:.1f}mA")

    def handle_disconnection_ui(self):
        self.conn_status_label.config(text="Status: Disconnected", foreground="red")
        self.connect_btn.config(text="Retry Connect", command=self.auto_connect, style="Connect.TButton")
        self.is_monitoring.set(False)
        self.toggle_monitoring()
        self.log_message("Connection lost (Timeout or Error).", "error")

    # --- Plotting ---
    def populate_csv_combo(self):
        if not MATPLOTLIB_AVAILABLE: return
        self.csv_combo.set('')
        # Scan for laser_data_*.csv
        files = glob.glob(os.path.join(LOG_DIR, "laser_data_*.csv"))
        files.sort(key=os.path.getmtime, reverse=True)
        self.csv_combo['values'] = [os.path.basename(f) for f in files]
        if files: self.csv_combo.set(os.path.basename(files[0]))

    def plot_csv_data_popup(self):
        if not MATPLOTLIB_AVAILABLE: return
        filename = self.csv_file_var.get()
        if not filename: return
        filepath = os.path.join(LOG_DIR, filename)

        if self.plot_window and self.plot_window.winfo_exists():
            self.plot_window.lift()
        else:
            self.plot_window = Toplevel(self.master)
            self.plot_window.title(f"Plot: {filename}")
            self.plot_window.geometry("800x600")
            fig, axes = plt.subplots(3, 1, sharex=True, figsize=(8, 6))
            self.plot_window.canvas = FigureCanvasTkAgg(fig, master=self.plot_window)
            self.plot_window.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
            self.plot_window.axes = axes
            self.plot_window.fig = fig

        try:
            # [FIX] Robust CSV reading
            df = pd.read_csv(filepath, on_bad_lines='skip') 
            # [FIX] Flexible datetime parsing
            df['timestamp'] = pd.to_datetime(df['timestamp'], errors='coerce')
            df = df.dropna(subset=['timestamp']) # invalid date rows drop

            for ax in self.plot_window.axes: ax.clear()
            ax1, ax2, ax3 = self.plot_window.axes
            
            ax1.plot(df['timestamp'], df['temp_c'], 'r-', label='Temp')
            ax1.set_ylabel('Temp (°C)')
            ax1.grid(True)
            
            ax2.plot(df['timestamp'], df['bias_ma'], 'b-', label='Bias')
            ax2.set_ylabel('Bias (mA)')
            ax2.grid(True)

            ax3.plot(df['timestamp'], df['pulse_ma'], 'g-', label='Pulse')
            ax3.set_ylabel('Pulse (mA)')
            ax3.grid(True)
            
            self.plot_window.fig.autofmt_xdate()
            self.plot_window.canvas.draw()
            self.log_message(f"Plotted {filename}")
        except Exception as e:
            self.log_message(f"Plot error: {e}", "error")
            messagebox.showerror("Error", f"Could not plot file:\n{e}")

    def safe_shutdown_device(self):
        if self.laser.is_connected():
            self.laser.set_ld_on(False)
            self.laser.set_bias_current(0.0)
            self.laser.set_pulse_current(0.0)
            self.laser.set_tec_on(False)

    def on_closing(self):
        self._save_settings()
        if self.status_monitor_timer:
            self.status_monitor_timer.cancel()
            if self.status_monitor_timer.is_alive():
                self.status_monitor_timer.join(timeout=1.0)
        self.safe_shutdown_device()
        self.laser.disconnect()
        self.master.destroy()

if __name__ == "__main__":
    try:
        root = tk.Tk()
        default_font = font.nametofont("TkDefaultFont")
        default_font.configure(size=10)
        root.option_add("*Font", default_font)
        app = LaserControlApp(root)
        root.mainloop()
    except Exception as e:
        print(f"Critical: {e}")

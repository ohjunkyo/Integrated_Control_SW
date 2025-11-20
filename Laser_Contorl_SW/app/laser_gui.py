#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Tamadenshi Laser Control GUI (Python 3 / tkinter)
-
This program requires 'laser_driver.py' and the 'hidapi' library.
It also requires 'matplotlib' and 'pandas' for plotting.
"""

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, font, Toplevel
import os
import sys
import logging
import subprocess
import glob
import queue  # For thread-safe UI updates
import json   # For saving settings
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
    print("Please run: pip3 install matplotlib pandas")
    MATPLOTLIB_AVAILABLE = False
# ------------------------------

# Make sure this script can import 'laser_driver.py'
try:
    from laser_driver import TamadenshiLaser
except ImportError:
    print("Error: 'laser_driver.py' file not found.")
    print("Please ensure 'laser_gui.py' and 'laser_driver.py' are in the same directory.")
    sys.exit(1)

# --- Log Configuration ---
# User-requested log path
LOG_DIR = os.path.expanduser("~/ADC/ADC_test/LOG/LASER")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE_PATH = os.path.join(LOG_DIR, f"laser_log_{datetime.now().strftime('%Y-%m-%d')}.txt")

# Logger setup
log = logging.getLogger('LaserControl')
log.setLevel(logging.INFO)
# 포맷 변경: CSV 파싱이 쉽도록 | (파이프)로 구분
file_handler = logging.FileHandler(LOG_FILE_PATH)
file_handler.setFormatter(logging.Formatter('%(asctime)s | %(levelname)s | %(message)s'))

# 핸들러 중복 추가 방지
if not log.hasHandlers():
    log.addHandler(file_handler)
    log.propagate = False
# ------------------


class RepeatingTimer(Timer):
    """A Timer that repeats its function call"""
    def run(self):
        while not self.finished.wait(self.interval):
            self.function(*self.args, **self.kwargs)


class LaserControlApp:
    
    # Config file for saving settings
    CONFIG_FILE = os.path.expanduser("~/.laser_control_config.json")

    def __init__(self, master):
        self.master = master
        self.master.title("Laser Control (Python)")
        self.master.geometry("550x900") 

        self.laser = TamadenshiLaser()
        self.status_monitor_timer: Optional[RepeatingTimer] = None
        self.is_monitoring = tk.BooleanVar(value=False)
        self.current_status_text = tk.StringVar(value="Current Status: N/A")
        
        self.gui_queue = queue.Queue() # <-- Thread-safe queue
        self.plot_window: Optional[Toplevel] = None 
        
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
        style = ttk.Style()
        style.configure("TButton", padding=6, relief="flat", font=("Helvetica", 10))
        style.configure("Bold.TButton", padding=6, font=("Helvetica", 10, "bold"), relief="raised")
        style.map("Bold.TButton",
            background=[('active', '#0056b3')],
            foreground=[('active', 'white')]
        )
        style.configure("Connect.TButton", padding=6, font=("Helvetica", 10, "bold"), relief="raised")
        style.map("Connect.TButton",
            background=[('!disabled', '#28a745'), ('active', '#218838')],
            foreground=[('!disabled', 'white'), ('active', 'white')]
        )
        style.configure("Disconnect.TButton", padding=6, font=("Helvetica", 10, "bold"), relief="raised")
        style.map("Disconnect.TButton",
            background=[('!disabled', '#dc3545'), ('active', '#c82333')],
            foreground=[('!disabled', 'white'), ('active', 'white')]
        )
        style.configure("TLabel", font=("Helvetica", 10))
        style.configure("Bold.TLabel", font=("Helvetica", 10, "bold"))
        style.configure("TLabelframe", padding=10)
        style.configure("TLabelframe.Label", font=("Helvetica", 12, "bold"))
        style.configure("TScale", troughcolor='#d3d3d3')
        style.configure("Toolbutton", padding=5, font=("Helvetica", 10))
        style.map("Toolbutton",
            background=[('selected', '#007ACC'), ('!selected', '#f0f0f0')],
            foreground=[('selected', 'white'), ('!selected', 'black')]
        )

        self.master.protocol("WM_DELETE_WINDOW", self.on_closing)

        # --- Load Settings BEFORE creating widgets ---
        self._load_settings()

        # --- Create Widgets ---
        self._create_connection_frame()
        self._create_live_status_frame()
        self._create_main_control_frame()
        self._create_current_control_frame()
        # self._create_utility_frame() # [REMOVED]
        self._create_log_frame()
        self._create_status_bar()
        
        # GUI ready, start logging
        self.log_message("Laser Control GUI started.")
        self.process_gui_queue()
        self._update_status_bar_clock()
        self.auto_connect()

    def log_message(self, msg: str, level: str = "info"):
        """Logs a message to both the GUI log window and the file."""
        print(msg) # Also print to console
        
        # Log to file
        if level == "info":
            log.info(msg)
        elif level == "warning":
            log.warning(msg)
        elif level == "error":
            log.error(msg)

        # Log to GUI
        if hasattr(self, 'session_log_text'):
            timestamp = datetime.now().strftime('%H:%M:%S')
            try:
                self.session_log_text.config(state="normal")
                self.session_log_text.insert(tk.END, f"[{timestamp}] {msg}\n")
                self.session_log_text.config(state="disabled")
                self.session_log_text.yview(tk.END) # Auto-scroll
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
        """Creates a frame for live status indicators."""
        frame = ttk.LabelFrame(self.master, text="Live Status")
        frame.pack(fill=tk.X, padx=10, pady=5)
        
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(3, weight=1)

        def create_indicator(r, c, text, var_name):
            ttk.Label(frame, text=f"{text}:", font=("Helvetica", 10, "bold")).grid(row=r, column=c, sticky=tk.W, padx=5, pady=3)
            label = ttk.Label(frame, textvariable=self.live_status[var_name], font=("Helvetica", 10), relief="sunken", padding=(5, 2), anchor=tk.E)
            label.grid(row=r, column=c+1, sticky=tk.EW, padx=5, pady=3)
            return label

        # Left Column
        create_indicator(0, 0, "LD Status", "ld_status")
        create_indicator(1, 0, "Bias Current", "bias")
        
        # Right Column
        create_indicator(0, 2, "TEC Status", "tec_status")
        create_indicator(1, 2, "Pulse Current", "pulse")

        # Bottom Row (Temp)
        create_indicator(2, 0, "Temperature", "temp").grid(columnspan=3)


    def _create_main_control_frame(self):
        frame = ttk.LabelFrame(self.master, text="Main Control")
        frame.pack(fill=tk.X, padx=10, pady=5)
        
        # --- LD ON/OFF Buttons ---
        ld_frame = ttk.Frame(frame)
        ld_frame.pack(fill=tk.X, padx=5, pady=5)
        ttk.Label(ld_frame, text="Laser (LD):", width=12).pack(side=tk.LEFT)
        self.ld_on_btn = ttk.Button(ld_frame, text="ON", style="Connect.TButton", command=lambda: self.set_ld_on(True))
        self.ld_on_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        self.ld_off_btn = ttk.Button(ld_frame, text="OFF", style="Disconnect.TButton", command=lambda: self.set_ld_on(False))
        self.ld_off_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)

        # --- TEC ON/OFF Buttons ---
        tec_frame = ttk.Frame(frame)
        tec_frame.pack(fill=tk.X, padx=5, pady=5)
        ttk.Label(tec_frame, text="Temp (TEC):", width=12).pack(side=tk.LEFT)
        self.tec_on_btn = ttk.Button(tec_frame, text="ON", style="Connect.TButton", command=lambda: self.set_tec_on(True))
        self.tec_on_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        self.tec_off_btn = ttk.Button(tec_frame, text="OFF", style="Disconnect.TButton", command=lambda: self.set_tec_on(False))
        self.tec_off_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        
        # --- Trigger Selection ---
        trigger_frame = ttk.Frame(frame)
        trigger_frame.pack(fill=tk.X, pady=5, padx=5)
        ttk.Label(trigger_frame, text="Trigger:", width=12).pack(side=tk.LEFT)
        
        self.trigger_combo = ttk.Combobox(trigger_frame, textvariable=self.trigger_var, 
                                          values=["External", "Internal (PG1)", "Internal (PG2)"],
                                          state="readonly")
        self.trigger_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        self.trigger_combo.bind("<<ComboboxSelected>>", self.on_trigger_select)
        
        # --- Internal Frequency Control Frame ---
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
        
        self.on_trigger_select() # Set initial state


    def _create_current_control_frame(self):
        frame = ttk.LabelFrame(self.master, text="Current Control (mA)")
        frame.pack(fill=tk.X, padx=10, pady=5)
        
        # 1. Bias Current
        self._create_slider_entry_pair(frame, "Bias Current:", self.bias_val, 0.0, 200.0)
        
        # 2. Pulse Current
        self._create_slider_entry_pair(frame, "Pulse Current:", self.pulse_val, 0.0, 200.0)

        # 3. Apply Button
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

    # [REMOVED] _create_utility_frame(self)
        
    def _create_log_frame(self):
        frame = ttk.LabelFrame(self.master, text="Log Viewer")
        frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(5, 10))
        
        self.log_notebook = ttk.Notebook(frame)
        self.log_notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # --- Tab 1: Session Log ---
        session_tab = ttk.Frame(self.log_notebook, padding=5)
        self.log_notebook.add(session_tab, text="Session Log")

        self.session_log_text = scrolledtext.ScrolledText(session_tab, wrap=tk.WORD, state="disabled", height=10,
                                                  bg="#f8f9fa", fg="#212529", font=("Monaco", 9))
        self.session_log_text.pack(fill=tk.BOTH, expand=True)

        # --- Tab 2: Log History ---
        history_tab = ttk.Frame(self.log_notebook, padding=5)
        self.log_notebook.add(history_tab, text="Log History")
        
        history_controls = ttk.Frame(history_tab)
        history_controls.pack(fill=tk.X, pady=5)
        
        ttk.Label(history_controls, text="Select Log:").pack(side=tk.LEFT, padx=(0, 5))
        self.log_file_var = tk.StringVar()
        self.log_combo = ttk.Combobox(history_controls, textvariable=self.log_file_var, state="readonly", width=30)
        self.log_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        
        load_btn = ttk.Button(history_controls, text="Load", command=self.load_log_history)
        load_btn.pack(side=tk.LEFT, padx=5)
        
        refresh_btn = ttk.Button(history_controls, text="Refresh", command=self.populate_log_history_combo)
        refresh_btn.pack(side=tk.LEFT)
        
        self.history_log_text = scrolledtext.ScrolledText(history_tab, wrap=tk.WORD, state="disabled", height=10,
                                                  bg="#e9ecef", fg="#212529", font=("Monaco", 9))
        self.history_log_text.pack(fill=tk.BOTH, expand=True, pady=(5,0))
        
        self.log_notebook.bind("<<NotebookTabChanged>>", self.on_tab_change)
        
        # --- Tab 3: Data Plotter ---
        if MATPLOTLIB_AVAILABLE:
            plot_tab = ttk.Frame(self.log_notebook, padding=5)
            self.log_notebook.add(plot_tab, text="Data Plotter")
            
            plot_controls = ttk.Frame(plot_tab)
            plot_controls.pack(fill=tk.X, pady=5)
            
            ttk.Label(plot_controls, text="Select Data File:").pack(side=tk.LEFT, padx=(0, 5))
            self.csv_file_var = tk.StringVar()
            self.csv_combo = ttk.Combobox(plot_controls, textvariable=self.csv_file_var, state="readonly", width=30)
            self.csv_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
            
            plot_load_btn = ttk.Button(plot_controls, text="Plot in New Window", command=self.plot_csv_data_popup)
            plot_load_btn.pack(side=tk.LEFT, padx=5)
            
            plot_refresh_btn = ttk.Button(plot_controls, text="Refresh List", command=self.populate_csv_combo)
            plot_refresh_btn.pack(side=tk.LEFT)
            
            ttk.Label(plot_tab, text="\nSelect a CSV data file and click 'Plot in New Window'.\nData is saved automatically to:\n" + LOG_DIR,
                      justify=tk.CENTER, foreground="gray").pack(fill=tk.BOTH, expand=True)
            
        else:
            plot_tab = ttk.Frame(self.log_notebook, padding=5)
            self.log_notebook.add(plot_tab, text="Data Plotter (Disabled)")
            ttk.Label(plot_tab, text="Plotting requires 'matplotlib' and 'pandas'.\nRun 'pip3 install matplotlib pandas' to enable.", foreground="gray").pack(pady=20)
        
        # Populate lists on startup
        self.populate_log_history_combo()
        self.populate_csv_combo()

    def _create_status_bar(self):
        """Creates a status bar at the bottom"""
        status_bar_frame = ttk.Frame(self.master, relief=tk.SUNKEN, padding=(5, 3))
        status_bar_frame.pack(side=tk.BOTTOM, fill=tk.X)
        
        # --- Left Side: Device Status ---
        self.status_bar_label = ttk.Label(status_bar_frame, textvariable=self.current_status_text, anchor=tk.W)
        self.status_bar_label.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        
        # --- Right Side: Monitor Toggle, Clock, Elapsed Time ---
        self.monitor_check = ttk.Checkbutton(status_bar_frame, text="Monitor", variable=self.is_monitoring, command=self.toggle_monitoring, style="Toolbutton")
        self.monitor_check.pack(side=tk.RIGHT, padx=5)
        
        self.elapsed_time_label = ttk.Label(status_bar_frame, textvariable=self.elapsed_time_var, anchor=tk.E)
        self.elapsed_time_label.pack(side=tk.RIGHT, padx=10)

        self.clock_label = ttk.Label(status_bar_frame, textvariable=self.clock_var, anchor=tk.E)
        self.clock_label.pack(side=tk.RIGHT, padx=10)

    def _update_status_bar_clock(self):
        """Updates the time/elapsed labels in the status bar."""
        try:
            now = datetime.now()
            current_time_str = now.strftime('%Y-%m-%d %H:%M:%S')
            self.clock_var.set(f"Time: {current_time_str}")

            elapsed = now - self.start_time
            total_seconds = int(elapsed.total_seconds())
            hours, remainder = divmod(total_seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            elapsed_str = f"{hours:02}:{minutes:02}:{seconds:02}"
            self.elapsed_time_var.set(f"Elapsed: {elapsed_str}")

            self.master.after(1000, self._update_status_bar_clock)
        except tk.TclError:
            # Window was destroyed, stop loop
            pass

    # --- Settings Save/Load Functions ---
    def _load_settings(self):
        """Loads settings from the JSON config file on startup."""
        try:
            with open(self.CONFIG_FILE, 'r') as f:
                settings = json.load(f)
            
            self.bias_val.set(settings.get("bias_ma", 0.0))
            self.pulse_val.set(settings.get("pulse_ma", 0.0))
            self.trigger_var.set(settings.get("trigger_mode", "External"))
            self.internal_freq_hz.set(settings.get("internal_freq_hz", "10000000"))
            print(f"Loaded settings from {self.CONFIG_FILE}")

        except FileNotFoundError:
            print(f"No config file found ({self.CONFIG_FILE}). Using defaults.")
        except Exception as e:
            print(f"Error loading settings: {e}")

    def _save_settings(self):
        """Saves settings to the JSON config file on closing."""
        try:
            settings = {
                "bias_ma": self.bias_val.get(),
                "pulse_ma": self.pulse_val.get(),
                "trigger_mode": self.trigger_var.get(),
                "internal_freq_hz": self.internal_freq_hz.get()
            }
            with open(self.CONFIG_FILE, 'w') as f:
                json.dump(settings, f, indent=4)
            print(f"Saved settings to {self.CONFIG_FILE}")
        except Exception as e:
            print(f"Error saving settings: {e}")

    # --- GUI Action Functions ---

    def auto_connect(self):
        """Attempts to connect to the device automatically."""
        self.log_message("Connecting to device...")
        success, msg = self.laser.connect()
        
        if success:
            self.conn_status_label.config(text="Status: Connected", foreground="green")
            self.log_message(f"Device connected: {msg}")
            self.connect_btn.config(text="Disconnect", command=self.disconnect, style="Disconnect.TButton")
            
            # On connection, send default/saved values
            self.on_trigger_select(init=True) # Set trigger & freq state
            self.apply_currents() # Apply saved/default currents
            self.set_ld_on(False) # Default to OFF
            self.set_tec_on(False) # Default to OFF
            
            # Start monitoring
            self.is_monitoring.set(True)
            self.toggle_monitoring()
        else:
            self.conn_status_label.config(text="Status: Disconnected", foreground="red")
            self.log_message(f"Connection failed: {msg}", "error")
            self.connect_btn.config(text="Retry Connect", command=self.auto_connect, style="Connect.TButton")

    def disconnect(self):
        """Disconnects from the device."""
        self.log_message("Disconnecting device...")
        self.is_monitoring.set(False)
        self.toggle_monitoring()
        
        # Safety shutdown
        self.safe_shutdown_device() 
        
        self.laser.disconnect()
        self.conn_status_label.config(text="Status: Disconnected", foreground="red")
        self.connect_btn.config(text="Connect", command=self.auto_connect, style="Connect.TButton")
        self.clear_live_status()

    def set_ld_on(self, state: bool):
        """Called by LD ON/OFF buttons."""
        if not self.laser.is_connected(): 
            self.log_message("Cannot turn LD ON/OFF: Not connected.", "error")
            return
        
        if self.laser.set_ld_on(state):
            self.log_message(f"SET_LD: {'ON' if state else 'OFF'}")
            Thread(target=self.read_status_to_queue).start()
        else:
            self.log_message("Failed to set Laser (LD) state", "error")
            self.handle_disconnection_ui()

    def set_tec_on(self, state: bool):
        """Called by TEC ON/OFF buttons."""
        if not self.laser.is_connected(): 
            self.log_message("Cannot turn TEC ON/OFF: Not connected.", "error")
            return

        if self.laser.set_tec_on(state):
            self.log_message(f"SET_TEC: {'ON' if state else 'OFF'}")
            Thread(target=self.read_status_to_queue).start()

        else:
            self.log_message("Failed to set TEC state", "error")
            self.handle_disconnection_ui()

    def on_trigger_select(self, event=None, init: bool = False):
        """Called when the trigger combobox is changed."""
        mode = self.trigger_var.get()
        
        if mode == "External":
            # Disable frequency controls
            self.freq_frame.config(text="Internal Trigger Control (Hz) - DISABLED (External)")
            self.freq_label.state(['disabled'])
            self.freq_entry.state(['disabled'])
            self.freq_apply_btn.state(['disabled'])
            if not init: # Don't log on initial setup
                self.log_message("External Trigger mode selected. Frequency controls disabled.")
            self.set_trigger()
        else:
            # Enable frequency controls
            self.freq_frame.config(text="Internal Trigger Control (Hz) - ENABLED")
            self.freq_label.state(['!disabled'])
            self.freq_entry.state(['!disabled'])
            self.freq_apply_btn.state(['!disabled'])
            
            # Update label based on selection
            if mode == "Internal (PG1)":
                self.freq_label.config(text="PG1 (100k-250M):")
            else: # PG2
                self.freq_label.config(text="PG2 (3k-200k):")

            if not init:
                self.log_message(f"{mode} mode selected. Frequency controls enabled.")
                # Automatically apply settings when selected
                self.set_trigger() 
                self.apply_frequency()

    def set_trigger(self):
        """[Internal] Sets the trigger source on the hardware."""
        if not self.laser.is_connected(): return
        
        mode = self.trigger_var.get()
        pg1, pg2, ext = False, False, False
        if mode == "Internal (PG1)":
            pg1 = True
        elif mode == "Internal (PG2)":
            pg2 = True
        elif mode == "External":
            ext = True
        
        if self.laser.set_trigger_mode(pg1=pg1, pg2=pg2, ext=ext):
            self.log_message(f"SET_TRIGGER: {mode}")
        else:
            self.log_message("Failed to set trigger mode", "error")
            self.handle_disconnection_ui()
            
    def apply_frequency(self):
        """Applies frequency to the selected internal generator."""
        if not self.laser.is_connected(): return
        
        mode = self.trigger_var.get()
        if mode == "External":
            self.log_message("Cannot apply frequency, External Trigger is active.", "warning")
            return

        try:
            freq_hz = int(self.internal_freq_hz.get())
            
            if mode == "Internal (PG1)":
                if self.laser.set_pg1_frequency(freq_hz):
                    self.log_message(f"SET_PG1_FREQ: {freq_hz} Hz")
                else:
                    self.log_message("Failed to set PG1 Frequency", "error")
                    self.handle_disconnection_ui()
            
            elif mode == "Internal (PG2)":
                if self.laser.set_pg2_frequency(freq_hz):
                    self.log_message(f"SET_PG2_FREQ: {freq_hz} Hz")
                else:
                    self.log_message("Failed to set PG2 Frequency", "error")
                    self.handle_disconnection_ui()

        except ValueError:
            messagebox.showerror("Error", "Invalid frequency. Please enter numbers only.")
            self.log_message("Invalid frequency value entered.", "error")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to set frequency: {e}")
            self.log_message(f"Failed to set frequency: {e}", "error")

    def apply_currents(self):
        """Called by 'Apply Currents' button."""
        if not self.laser.is_connected(): 
            self.log_message("Cannot apply currents: Not connected.", "error")
            return
        
        bias = self.bias_val.get()
        pulse = self.pulse_val.get()
        
        try:
            # Set Bias
            if self.laser.set_bias_current(bias):
                self.log_message(f"SET_BIAS: {bias:.2f} mA")
            else:
                self.log_message("Failed to set Bias Current", "error")
                self.handle_disconnection_ui()
                return # Stop if one fails

            # Set Pulse
            if self.laser.set_pulse_current(pulse):
                self.log_message(f"SET_PULSE: {pulse:.2f} mA")
            else:
                self.log_message("Failed to set Pulse Current", "error")
                self.handle_disconnection_ui()

        except Exception as e:
            messagebox.showerror("Error", f"Invalid current value: {e}")
            self.log_message(f"Invalid current value: {e}", "error")

    # --- Thread-safe GUI updates ---
    def process_gui_queue(self):
        """
        [Main Thread] Safely updates the GUI
        with data from the background (monitor) thread.
        """
        try:
            while not self.gui_queue.empty():
                message_type, data = self.gui_queue.get_nowait()
                
                if message_type == "status_update":
                    self.update_gui_with_status(data)
                elif message_type == "connection_lost":
                    self.handle_disconnection_ui()
                
        except queue.Empty:
            pass
        finally:
            # Re-schedule itself to run again
            try:
                self.master.after(100, self.process_gui_queue)
            except tk.TclError:
                pass # Master was destroyed

    def toggle_monitoring(self):
        """Starts/Stops the real-time status monitor thread."""
        if self.is_monitoring.get():
            if self.status_monitor_timer is None and self.laser.is_connected():
                self.log_message("Starting real-time status monitor (1Hz)...")
                self.status_monitor_timer = RepeatingTimer(0.05, self.read_status_to_queue)
                self.status_monitor_timer.daemon = True 
                self.status_monitor_timer.start()
        else:
            if self.status_monitor_timer:
                self.log_message("Stopping real-time status monitor...")
                self.status_monitor_timer.cancel()
                self.status_monitor_timer = None
                self.current_status_text.set("Current Status: N/A")
                self.clear_live_status()

    def read_status_to_queue(self):
        """
        [Thread] Reads device status and puts result in the queue.
        This function runs in the background thread.
        """
        if not self.laser.is_connected():
            self.gui_queue.put(("connection_lost", None))
            return
            
        if self.laser.update_status():
            # Put a copy of the status dictionary into the queue
            status_copy = self.laser.status.copy()
            self.gui_queue.put(("status_update", status_copy))
        else:
            # Failed to read status (disconnected)
            self.gui_queue.put(("connection_lost", None))
            
    def update_gui_with_status(self, status: dict):
        """
        [Main Thread] Safely updates all GUI elements with new status data.
        """
        # 1. Get values
        ld = status.get('ld_on', False)
        tec = status.get('tec_on', False)
        temp = status.get('ld_temp', 0.0)
        bias = status.get('bias', 0.0)
        pulse = status.get('pulse', 0.0)

        # 2. Update top "Live Status" indicators
        self.live_status["ld_status"].set("ON" if ld else "OFF")
        self.live_status["tec_status"].set("ON" if tec else "OFF")
        self.live_status["temp"].set(f"{temp:.2f} °C")
        self.live_status["bias"].set(f"{bias:.2f} mA")
        self.live_status["pulse"].set(f"{pulse:.2f} mA")

        # 3. Update bottom "Status Bar"
        status_str = "LD: {ld}, TEC: {tec}, Temp: {temp:.1f}°C, Bias: {bias:.1f}mA, Pulse: {pulse:.1f}mA".format(
            ld='ON' if ld else 'OFF',
            tec='ON' if tec else 'OFF',
            temp=temp,
            bias=bias,
            pulse=pulse
        )
        self.current_status_text.set(f"Status: {status_str}")
        
    def clear_live_status(self):
        """Resets live status indicators to '---'."""
        self.live_status["ld_status"].set("---")
        self.live_status["tec_status"].set("---")
        self.live_status["temp"].set("--.- °C")
        self.live_status["bias"].set("---.- mA")
        self.live_status["pulse"].set("---.- mA")
        self.current_status_text.set("Current Status: N/A")

    def handle_disconnection_ui(self):
        if self.laser.is_connected(): # If driver doesn't know yet
            self.laser.disconnect()
            
        self.conn_status_label.config(text="Status: Disconnected", foreground="red")
        self.connect_btn.config(text="Retry Connect", command=self.auto_connect, style="Connect.TButton")
        self.is_monitoring.set(False)
        self.toggle_monitoring()
        self.clear_live_status()
        self.log_message("Connection lost. Please check USB and retry.", "error")

    # [REMOVED] All launch_... functions

    def on_tab_change(self, event):
        """Called when any notebook tab is clicked."""
        try:
            selected_tab_index = self.log_notebook.index(self.log_notebook.select())
            selected_tab_text = self.log_notebook.tab(selected_tab_index, "text")
        except tk.TclError:
            return # Tab is changing, ignore error

        if selected_tab_text == "Log History":
            self.populate_log_history_combo()
        elif selected_tab_text == "Data Plotter":
            self.populate_csv_combo()

    def populate_log_history_combo(self):
        """Scans the log directory for .txt and .log files."""
        try:
            self.log_combo.set('') # Clear selection
            log_files = []
            for ext in ("*.txt", "*.log"):
                log_files.extend(glob.glob(os.path.join(LOG_DIR, ext)))
            
            # Sort by modification time (newest first)
            log_files.sort(key=os.path.getmtime, reverse=True)
            
            # Get just the filenames
            self.log_combo['values'] = [os.path.basename(f) for f in log_files]
            if log_files:
                self.log_combo.set(os.path.basename(log_files[0])) # Select newest
                self.load_log_history() # Automatically load newest
        except Exception as e:
            self.log_message(f"Failed to scan log directory: {e}", "error")
            
    def load_log_history(self):
        """Loads the selected log file into the history text view."""
        filename = self.log_file_var.get()
        if not filename:
            return
            
        filepath = os.path.join(LOG_DIR, filename)
        try:
            with open(filepath, 'r') as f:
                content = f.read()
            
            self.history_log_text.config(state="normal")
            self.history_log_text.delete('1.0', tk.END)
            self.history_log_text.insert(tk.END, content)
            self.history_log_text.config(state="disabled")
            self.history_log_text.yview(tk.END) # Scroll to end
        except Exception as e:
            self.log_message(f"Failed to read log file {filename}: {e}", "error")
            self.history_log_text.config(state="normal")
            self.history_log_text.delete('1.0', tk.END)
            self.history_log_text.insert(tk.END, f"Error reading file:\n{e}")
            self.history_log_text.config(state="disabled")
            
    def populate_csv_combo(self):
        """Scans the log directory for .csv data files."""
        if not MATPLOTLIB_AVAILABLE: return
        try:
            self.csv_combo.set('') # Clear selection
            csv_files = glob.glob(os.path.join(LOG_DIR, "laser_data_*.csv"))
            csv_files.sort(key=os.path.getmtime, reverse=True)
            
            self.csv_combo['values'] = [os.path.basename(f) for f in csv_files]
            if csv_files:
                self.csv_combo.set(os.path.basename(csv_files[0])) # Select newest
        except Exception as e:
            self.log_message(f"Failed to scan CSV directory: {e}", "error")
            
    def plot_csv_data_popup(self):
        """Reads the selected CSV and plots it in a NEW window."""
        if not MATPLOTLIB_AVAILABLE: 
            self.log_message("Plotting libraries not available.", "error")
            return
        
        filename = self.csv_file_var.get()
        if not filename:
            self.log_message("No CSV file selected to plot.", "warning")
            return
            
        filepath = os.path.join(LOG_DIR, filename)
        
        # --- Check if plot window already exists ---
        if self.plot_window and self.plot_window.winfo_exists():
            self.plot_window.lift() # Bring to front
        else:
            # Create new Toplevel window
            self.plot_window = Toplevel(self.master)
            self.plot_window.title(f"Data Plot: {filename}")
            self.plot_window.geometry("800x600")
            
            # Create figure and canvas inside the new window
            fig, axes = plt.subplots(3, 1, sharex=True, figsize=(8, 6))
            canvas = FigureCanvasTkAgg(fig, master=self.plot_window)
            canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
            self.plot_window.plot_fig = fig
            self.plot_window.plot_axes = axes
            self.plot_window.plot_canvas = canvas

        # --- Plotting logic (same as before, but on the new window's axes) ---
        try:
            df = pd.read_csv(filepath)
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            
            for ax in self.plot_window.plot_axes:
                ax.clear()
            
            ax1, ax2, ax3 = self.plot_window.plot_axes
            
            ax1.plot(df['timestamp'], df['temp_c'], label='Temp (°C)', color='red')
            ax1.set_ylabel('Temp (°C)')
            ax1.legend(loc='upper right')
            ax1.grid(True)
            
            ax2.plot(df['timestamp'], df['bias_ma'], label='Bias (mA)', color='blue')
            ax2.set_ylabel('Bias (mA)')
            ax2.legend(loc='upper right')
            ax2.grid(True)

            ax3.plot(df['timestamp'], df['pulse_ma'], label='Pulse (mA)', color='green')
            ax3.set_ylabel('Pulse (mA)')
            ax3.set_xlabel('Time')
            ax3.legend(loc='upper right')
            ax3.grid(True)
            
            self.plot_window.plot_fig.autofmt_xdate()
            plt.tight_layout(pad=2)
            
            self.plot_window.plot_canvas.draw()
            self.log_message(f"Successfully plotted data from {filename} in new window.")
            
        except Exception as e:
            self.log_message(f"Failed to plot file {filename}: {e}", "error")
            messagebox.showerror("Plot Error", f"Failed to plot data:\n{e}", parent=self.plot_window)

    def safe_shutdown_device(self):
        """Sends safety commands to turn off all outputs."""
        if self.laser.is_connected():
            print("Sending safety commands (setting currents to 0)...")
            self.log_message("SAFETY: Setting all currents to 0 and turning off LD/TEC.")
            self.laser.set_ld_on(False)
            self.laser.set_bias_current(0.0)
            self.laser.set_pulse_current(0.0)
            self.laser.set_tec_on(False)
            
    # --- [*** MODIFIED: This is the fix ***] ---
    def on_closing(self):
        """Called when the window is closed."""
        self.log_message("Shutting down...")
        
        # Save settings first
        self._save_settings()

        # [FIX] Stop the monitor thread AND wait for it to finish
        if self.status_monitor_timer:
            self.log_message("Stopping monitor thread...")
            self.status_monitor_timer.cancel() # 1. Tell the thread to stop
            
            # 2. Wait for the thread to actually die (max 1.2 sec)
            # This prevents the race condition
            if self.status_monitor_timer.is_alive():
                self.status_monitor_timer.join(timeout=1.2) 
            
            self.log_message("Monitor thread stopped.")

        # Now that the thread is safely stopped, shut down the device
        self.safe_shutdown_device()
        
        if self.laser.is_connected():
            self.laser.disconnect()
        
        if self.plot_window and self.plot_window.winfo_exists():
            self.plot_window.destroy() # Close plot window
            
        self.master.destroy()
    # --- [*** END MODIFICATION ***] ---


if __name__ == "__main__":
    
    # --- Linux udev/sudo Warning ---
    if os.name == 'posix' and os.geteuid() != 0:
        print("Warning: This script might require root privileges (sudo) or")
        print("      'udev' rules setup to access the USB device.")
        print("      (See laser_readme.md for instructions)")
    # -----------------------------

    try:
        root = tk.Tk()
        
        # Mimic font settings from main.py/ui_manager.py
        default_font = font.nametofont("TkDefaultFont")
        default_font.configure(size=10)
        root.option_add("*Font", default_font)

        app = LaserControlApp(root)
        root.mainloop()
    except Exception as e:
        log.error(f"Failed to start GUI: {e}")
        messagebox.showerror("Critical Error", f"Failed to start GUI:\n{e}")

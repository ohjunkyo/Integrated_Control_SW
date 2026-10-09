#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Tamadenshi Laser Control GUI (Python 3 / tkinter) -- standalone edition.

Differences from the older app/laser_gui.py, beyond dropping the hardcoded
~/ADC/... paths:

  * Board picker. The old GUI called connect() with no path, which grabs
    whichever board hidapi happens to enumerate first -- fine with one board,
    a coin flip with four. This one lists them and lets you choose.
  * Combined current limit. The old apply_currents() wrote bias and pulse
    through separate setters, so neither could see the sum; 150+150 mA went
    straight through. This one goes through driver.set_currents(), which
    enforces the board's 200 mA COMBINED ceiling.
  * One CSV, not two. The old GUI wrote its own laser_data_%Y-%m-%d.csv with a
    6-column schema while the driver independently wrote
    laser_data_%Y%m%d.csv with 9 -- two files, two schemas, one directory.
    The driver's logger is now the only writer, so the photodiode and pulse
    width actually make it into the log.
  * Photodiode and pulse width are shown and plotted. The photodiode is the
    only genuinely measured optical quantity on this board (bias/pulse read
    back the DAC setpoint even with the LD off), so it is the one field a
    drift study can use.

Requirements: python3-tk, hidapi. matplotlib/pandas optional (plot tab).
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
from datetime import datetime
from threading import Timer
from typing import Optional

# --- Plotting Imports ---
try:
    import pandas as pd
    import matplotlib
    matplotlib.use("TkAgg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    print("Warning: 'matplotlib' or 'pandas' not found. Plotting tab disabled.")
    MATPLOTLIB_AVAILABLE = False

# --- Driver Import ---
try:
    from laser_driver import TamadenshiLaser, list_devices, DATA_LOG_DIR
except ImportError:
    print("Error: 'laser_driver.py' file not found next to this script.")
    sys.exit(1)

# --- Path Configuration ---
# Single source of truth: the driver already resolved LASER_LOG_DIR (or ./log).
# The GUI must not invent a second location, or its plot tab would list files
# nobody is writing to.
LOG_DIR = DATA_LOG_DIR
HERE = os.path.dirname(os.path.abspath(__file__))
# Settings live next to the script, not in $HOME, so the whole install stays
# copyable and two checkouts don't fight over one config file.
CONFIG_FILE = os.path.join(HERE, "laser_gui_config.json")

log = logging.getLogger('LaserControl')
log.setLevel(logging.INFO)
_text_log_ready = False


def _ensure_text_log():
    """Attach the rotating text-log handler on first use. Deferred (rather
    than done at import) so launching with an unwritable log dir degrades to
    console-only instead of failing to start."""
    global _text_log_ready
    if _text_log_ready:
        return
    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    console.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
    log.addHandler(console)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        handler = TimedRotatingFileHandler(
            filename=os.path.join(LOG_DIR, "laser_log"),
            when='midnight', interval=1, encoding='utf-8')
        handler.suffix = "_%Y-%m-%d.txt"
        handler.setFormatter(
            logging.Formatter('%(asctime)s | %(levelname)s | %(message)s'))
        log.addHandler(handler)
    except Exception as e:
        print(f"⚠️  Text logging to {LOG_DIR} unavailable: {e}")
    log.propagate = False
    _text_log_ready = True


class RepeatingTimer(Timer):
    """A Timer that repeats its function call."""
    def run(self):
        while not self.finished.wait(self.interval):
            self.function(*self.args, **self.kwargs)


class LaserControlApp:

    def __init__(self, master):
        self.master = master
        self.master.title("Laser Control (Standalone)")
        self.master.geometry("620x980")

        _ensure_text_log()

        self.laser = TamadenshiLaser()
        self.status_monitor_timer: Optional[RepeatingTimer] = None
        self.is_monitoring = tk.BooleanVar(value=False)
        self.current_status_text = tk.StringVar(value="Current Status: N/A")

        self.gui_queue = queue.Queue()
        self.plot_window: Optional[Toplevel] = None

        # Connection Stability Variables
        self.consecutive_errors = 0
        self.MAX_RETRIES = 5

        self.start_time = datetime.now()
        self.clock_var = tk.StringVar()
        self.elapsed_time_var = tk.StringVar()

        # --- Device selection ---
        self.devices = []
        self.device_var = tk.StringVar()

        # --- Status Indicator Variables ---
        self.live_status = {
            "ld_status": tk.StringVar(value="OFF"),
            "tec_status": tk.StringVar(value="OFF"),
            "temp": tk.StringVar(value="--.- °C"),
            "bias": tk.StringVar(value="---.- mA"),
            "pulse": tk.StringVar(value="---.- mA"),
            "pd": tk.StringVar(value="--"),
            "pulse_width": tk.StringVar(value="-- ps"),
        }

        self.trigger_var = tk.StringVar(value="External")
        self.internal_freq_hz = tk.StringVar(value="10000000")

        self.bias_val = tk.DoubleVar(value=0.0)
        self.pulse_val = tk.DoubleVar(value=0.0)
        self.total_current_var = tk.StringVar(value="Total: 0.0 / 200 mA")
        self.bias_val.trace_add("write", self._update_total_label)
        self.pulse_val.trace_add("write", self._update_total_label)

        self.pulse_width_var = tk.StringVar(value="")
        self._pulse_width_default: Optional[int] = None

        self._configure_styles()
        self.master.protocol("WM_DELETE_WINDOW", self.on_closing)
        self._load_settings()

        self._create_connection_frame()
        self._create_live_status_frame()
        self._create_main_control_frame()
        self._create_current_control_frame()
        self._create_log_frame()
        self._create_status_bar()

        self.log_message("Laser Control GUI started.")
        self.log_message(f"Logs: {LOG_DIR}")
        self.process_gui_queue()
        self._update_status_bar_clock()
        self.refresh_devices()
        self.auto_connect()

    def _configure_styles(self):
        style = ttk.Style()
        style.configure("TButton", padding=6, relief="flat",
                        font=("Helvetica", 10))
        style.configure("Bold.TButton", padding=6,
                        font=("Helvetica", 10, "bold"), relief="raised")
        style.map("Bold.TButton", background=[('active', '#0056b3')],
                  foreground=[('active', 'white')])
        style.configure("Connect.TButton", padding=6,
                        font=("Helvetica", 10, "bold"), relief="raised")
        style.map("Connect.TButton",
                  background=[('!disabled', '#28a745'), ('active', '#218838')],
                  foreground=[('!disabled', 'white'), ('active', 'white')])
        style.configure("Disconnect.TButton", padding=6,
                        font=("Helvetica", 10, "bold"), relief="raised")
        style.map("Disconnect.TButton",
                  background=[('!disabled', '#dc3545'), ('active', '#c82333')],
                  foreground=[('!disabled', 'white'), ('active', 'white')])
        style.configure("Toolbutton", padding=5, font=("Helvetica", 10))

    def log_message(self, msg: str, level: str = "info"):
        getattr(log, level if level in ("info", "warning", "error") else "info")(msg)
        if hasattr(self, 'session_log_text'):
            timestamp = datetime.now().strftime('%H:%M:%S')
            try:
                self.session_log_text.config(state="normal")
                self.session_log_text.insert(tk.END, f"[{timestamp}] {msg}\n")
                self.session_log_text.config(state="disabled")
                self.session_log_text.yview(tk.END)
            except tk.TclError:
                pass

    # --- Connection ------------------------------------------------------

    def _create_connection_frame(self):
        frame = ttk.LabelFrame(self.master, text="Connection")
        frame.pack(fill=tk.X, padx=10, pady=(10, 5))

        row = ttk.Frame(frame)
        row.pack(fill=tk.X, padx=5, pady=3)
        ttk.Label(row, text="Board:", width=8).pack(side=tk.LEFT)
        self.device_combo = ttk.Combobox(row, textvariable=self.device_var,
                                         state="readonly")
        self.device_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        ttk.Button(row, text="Rescan", command=self.refresh_devices,
                   width=8).pack(side=tk.LEFT)

        row2 = ttk.Frame(frame)
        row2.pack(fill=tk.X, padx=5, pady=3)
        self.conn_status_label = ttk.Label(
            row2, text="Status: Disconnected", foreground="red",
            font=("Helvetica", 10, "bold"))
        self.conn_status_label.pack(side=tk.LEFT, padx=5, expand=True)
        self.connect_btn = ttk.Button(row2, text="Connect",
                                      command=self.auto_connect,
                                      style="Connect.TButton")
        self.connect_btn.pack(side=tk.RIGHT, padx=5)

    def refresh_devices(self):
        """Re-enumerate boards. These boards report no serial number -- every
        one shows up as "Simple HID Device Demo" with a blank serial -- so the
        USB path is the only thing that tells them apart, and it is what the
        label shows."""
        self.devices = list_devices()
        labels = []
        for i, d in enumerate(self.devices):
            path = d['path'].decode(errors='replace')
            sn = d.get('serial_number') or ''
            labels.append(f"[{i}] {path}" + (f"  (SN {sn})" if sn else ""))
        self.device_combo['values'] = labels
        if labels:
            if self.device_var.get() not in labels:
                self.device_combo.current(0)
            self.log_message(f"Found {len(labels)} board(s).")
        else:
            self.device_var.set('')
            self.log_message("No board found. Check USB cable / udev rules.",
                             "warning")

    def _selected_device(self):
        label = self.device_var.get()
        values = list(self.device_combo['values'])
        if not values or label not in values:
            return None
        return self.devices[values.index(label)]

    def auto_connect(self):
        dev = self._selected_device()
        if dev is None:
            self.refresh_devices()
            dev = self._selected_device()
        if dev is None:
            messagebox.showerror("No device", "No Tamadenshi board found.")
            return

        self.log_message(f"Connecting to {dev['path'].decode(errors='replace')} ...")
        success, msg = self.laser.connect(dev['path'])
        if success:
            self.conn_status_label.config(text="Status: Connected",
                                          foreground="green")
            self.log_message(f"Device connected. {msg}")
            self.connect_btn.config(text="Disconnect", command=self.disconnect,
                                    style="Disconnect.TButton")
            self.device_combo.state(["disabled"])
            self.consecutive_errors = 0

            # Bring the board to a known, dark state before anything else --
            # never inherit whatever the previous session left firing.
            self.set_ld_on(False)
            self.set_tec_on(False)
            self.on_trigger_select(init=True)
            self.set_trigger()
            self.apply_currents(confirm=False)
            self._read_pulse_width()

            self.is_monitoring.set(True)
            self.toggle_monitoring()
        else:
            self.conn_status_label.config(text="Status: Disconnected",
                                          foreground="red")
            self.log_message(f"Connection failed: {msg}", "error")
            self.connect_btn.config(text="Connect", command=self.auto_connect,
                                    style="Connect.TButton")

    def disconnect(self):
        self.log_message("Disconnecting...")
        self.is_monitoring.set(False)
        self.toggle_monitoring()
        self.safe_shutdown_device()
        self.laser.disconnect()
        self.handle_disconnection_ui()

    # --- Live status -----------------------------------------------------

    def _create_live_status_frame(self):
        frame = ttk.LabelFrame(self.master, text="Live Status")
        frame.pack(fill=tk.X, padx=10, pady=5)
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(3, weight=1)

        def ind(r, c, text, var_name):
            ttk.Label(frame, text=f"{text}:",
                      font=("Helvetica", 10, "bold")).grid(
                row=r, column=c, sticky=tk.W, padx=5, pady=3)
            ttk.Label(frame, textvariable=self.live_status[var_name],
                      font=("Helvetica", 10), relief="sunken",
                      padding=(5, 2), anchor=tk.E).grid(
                row=r, column=c + 1, sticky=tk.EW, padx=5, pady=3)

        ind(0, 0, "LD Status", "ld_status")
        ind(0, 2, "TEC Status", "tec_status")
        ind(1, 0, "Bias (set)", "bias")
        ind(1, 2, "Pulse (set)", "pulse")
        ind(2, 0, "Temperature", "temp")
        ind(2, 2, "Pulse Width", "pulse_width")
        # The photodiode is the only genuinely MEASURED value here; bias/pulse
        # above are the DAC setpoints echoed back and read the commanded value
        # even with the LD off.
        ind(3, 0, "PD (measured)", "pd")

    # --- Main control ----------------------------------------------------

    def _create_main_control_frame(self):
        frame = ttk.LabelFrame(self.master, text="Main Control")
        frame.pack(fill=tk.X, padx=10, pady=5)

        ld_frame = ttk.Frame(frame)
        ld_frame.pack(fill=tk.X, padx=5, pady=5)
        ttk.Label(ld_frame, text="Laser (LD):", width=12).pack(side=tk.LEFT)
        ttk.Button(ld_frame, text="ON", style="Connect.TButton",
                   command=self.request_ld_on).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        ttk.Button(ld_frame, text="OFF", style="Disconnect.TButton",
                   command=lambda: self.set_ld_on(False)).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=5)

        tec_frame = ttk.Frame(frame)
        tec_frame.pack(fill=tk.X, padx=5, pady=5)
        ttk.Label(tec_frame, text="Temp (TEC):", width=12).pack(side=tk.LEFT)
        ttk.Button(tec_frame, text="ON", style="Connect.TButton",
                   command=lambda: self.set_tec_on(True)).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        ttk.Button(tec_frame, text="OFF", style="Disconnect.TButton",
                   command=lambda: self.set_tec_on(False)).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=5)

        trigger_frame = ttk.Frame(frame)
        trigger_frame.pack(fill=tk.X, pady=5, padx=5)
        ttk.Label(trigger_frame, text="Trigger:", width=12).pack(side=tk.LEFT)
        self.trigger_combo = ttk.Combobox(
            trigger_frame, textvariable=self.trigger_var,
            values=["External", "Internal (PG1)", "Internal (PG2)"],
            state="readonly")
        self.trigger_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        self.trigger_combo.bind("<<ComboboxSelected>>", self.on_trigger_select)

        self.freq_frame = ttk.LabelFrame(frame,
                                         text="Internal Trigger Control (Hz)")
        self.freq_frame.pack(fill=tk.X, padx=5, pady=5)
        fe = ttk.Frame(self.freq_frame)
        fe.pack(fill=tk.X, padx=5, pady=2)
        self.freq_label = ttk.Label(fe, text="Frequency (Hz):", width=15)
        self.freq_label.pack(side=tk.LEFT)
        self.freq_entry = ttk.Entry(fe, textvariable=self.internal_freq_hz)
        self.freq_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.freq_apply_btn = ttk.Button(self.freq_frame,
                                         text="Apply Frequency",
                                         command=self.apply_frequency,
                                         style="Bold.TButton")
        self.freq_apply_btn.pack(fill=tk.X, ipady=5, padx=5, pady=5)

        self.on_trigger_select(init=True)

    # --- Current + pulse width control -----------------------------------

    def _update_total_label(self, *_):
        try:
            total = self.bias_val.get() + self.pulse_val.get()
        except (tk.TclError, ValueError):
            return
        limit = TamadenshiLaser.LD_TOTAL_CURRENT_LIMIT_MA
        self.total_current_var.set(f"Total: {total:.1f} / {limit:.0f} mA")
        if hasattr(self, 'total_label'):
            self.total_label.config(
                foreground="red" if total > limit else "black")

    def _create_current_control_frame(self):
        frame = ttk.LabelFrame(self.master, text="Current Control (mA)")
        frame.pack(fill=tk.X, padx=10, pady=5)

        self._create_slider_entry_pair(frame, "Bias Current:", self.bias_val,
                                       0.0, 200.0)
        self._create_slider_entry_pair(frame, "Pulse Current:", self.pulse_val,
                                       0.0, 200.0)

        # Bias and pulse share one drive path, so the manual's 200 mA ceiling
        # is on their SUM. Showing the running total makes that visible before
        # the driver rejects the write.
        self.total_label = ttk.Label(frame, textvariable=self.total_current_var,
                                     font=("Helvetica", 10, "bold"))
        self.total_label.pack(anchor=tk.E, padx=10)
        self._update_total_label()

        ttk.Button(frame, text="Apply Currents", command=self.apply_currents,
                   style="Bold.TButton").pack(fill=tk.X, ipady=5, pady=(5, 8))

        ttk.Separator(frame).pack(fill=tk.X, pady=5)

        pw = ttk.Frame(frame)
        pw.pack(fill=tk.X, pady=5, padx=5)
        ttk.Label(pw, text="Pulse Width (ps):", width=15).pack(side=tk.LEFT)
        ttk.Entry(pw, textvariable=self.pulse_width_var, width=8).pack(
            side=tk.LEFT, padx=5)
        ttk.Button(pw, text="Read", command=self._read_pulse_width,
                   width=7).pack(side=tk.LEFT, padx=2)
        ttk.Button(pw, text="Default", command=self._restore_pulse_width,
                   width=8).pack(side=tk.LEFT, padx=2)
        ttk.Button(pw, text="Apply", command=self.apply_pulse_width,
                   width=7).pack(side=tk.LEFT, padx=2)

    def _create_slider_entry_pair(self, parent, label_text, var, from_, to):
        frame = ttk.Frame(parent)
        frame.pack(fill=tk.X, pady=5)
        ttk.Label(frame, text=label_text, width=15).pack(side=tk.LEFT, padx=5)
        slider = ttk.Scale(frame, from_=from_, to=to, variable=var,
                           orient=tk.HORIZONTAL,
                           command=lambda v: var.set(round(float(v), 2)))
        slider.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        ttk.Entry(frame, textvariable=var, width=8).pack(side=tk.RIGHT, padx=5)

    def apply_currents(self, confirm: bool = True):
        if not self.laser.is_connected():
            return
        try:
            b, p = float(self.bias_val.get()), float(self.pulse_val.get())
        except (tk.TclError, ValueError):
            messagebox.showerror("Error", "Invalid current value.")
            return
        if confirm and self.laser.status.get('ld_on'):
            if not messagebox.askyesno(
                    "Laser is ON",
                    f"The laser is currently firing.\n\n"
                    f"Change drive currents to bias {b:.1f} mA / "
                    f"pulse {p:.1f} mA now?"):
                return
        # Always through set_currents(): it is the only path that checks the
        # COMBINED limit. Calling the two setters separately cannot see the sum.
        ok, msg = self.laser.set_currents(b, p)
        if ok:
            self.log_message(f"SET_CURRENTS: Bias={b:.2f}, Pulse={p:.2f}")
        else:
            self.log_message(msg, "error")
            messagebox.showerror("LD Current Limit", msg)

    def _read_pulse_width(self):
        if not self.laser.is_connected():
            return
        pw = self.laser.get_pulse_width_ps()
        if pw is None:
            self.log_message("Pulse width read failed (retry).", "warning")
            return
        if self._pulse_width_default is None:
            self._pulse_width_default = pw   # what the board shipped running
        self.pulse_width_var.set(str(pw))
        self.live_status["pulse_width"].set(f"{pw} ps")
        self.log_message(f"GET_PULSE_WIDTH: {pw} ps")

    def _restore_pulse_width(self):
        if self._pulse_width_default is None:
            messagebox.showinfo("Pulse Width",
                                "No stored value yet -- press Read first.")
            return
        self.pulse_width_var.set(str(self._pulse_width_default))

    def apply_pulse_width(self):
        if not self.laser.is_connected():
            return
        try:
            width = int(float(self.pulse_width_var.get()))
        except ValueError:
            messagebox.showerror("Error", "Pulse width must be a number.")
            return
        lo, hi = (TamadenshiLaser.PULSE_WIDTH_MIN_PS,
                  TamadenshiLaser.PULSE_WIDTH_MAX_PS)
        if not (lo <= width <= hi):
            messagebox.showerror("Error",
                                 f"Pulse width must be {lo}-{hi} ps.")
            return
        # Writing slot 0 changes the emitted pulse immediately AND persists to
        # EEPROM, so this is never a dry run.
        if not messagebox.askyesno(
                "Confirm Pulse Width",
                f"Write {width} ps to the board?\n\n"
                f"Currently running: "
                f"{self._pulse_width_default or 'unknown'} ps\n\n"
                "This changes the emitted pulse immediately and is saved to "
                "the board's EEPROM."):
            return
        try:
            ok = self.laser.set_pulse_width_ps(width)
        except ValueError as e:
            messagebox.showerror("Error", str(e))
            return
        if ok:
            self.log_message(f"SET_PULSE_WIDTH: {width} ps")
            self.live_status["pulse_width"].set(f"{width} ps")
        else:
            messagebox.showerror("Error", "Pulse width write failed.")

    # --- Device control --------------------------------------------------

    def request_ld_on(self):
        """LD ON emits light, so it is the one control that always confirms."""
        if not self.laser.is_connected():
            return
        st = self.laser.status
        if not messagebox.askyesno(
                "Turn laser ON?",
                f"The laser will start emitting light.\n\n"
                f"Bias:  {st.get('bias', 0):.1f} mA\n"
                f"Pulse: {st.get('pulse', 0):.1f} mA\n"
                f"Temp:  {st.get('ld_temp', 0):.1f} °C\n\n"
                "Proceed?"):
            return
        self.set_ld_on(True)

    def set_ld_on(self, state: bool):
        if not self.laser.is_connected():
            return
        if self.laser.set_ld_on(state):
            self.log_message(f"SET_LD: {'ON' if state else 'OFF'}")
        else:
            self.log_message("Failed to set LD state", "error")

    def set_tec_on(self, state: bool):
        if not self.laser.is_connected():
            return
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
            self.freq_label.config(
                text="PG1 (100k-250M):" if mode == "Internal (PG1)"
                else "PG2 (3k-200k):")
            if not init:
                self.apply_frequency()
        if not init:
            self.set_trigger()

    def set_trigger(self):
        if not self.laser.is_connected():
            return
        mode = self.trigger_var.get()
        if self.laser.set_trigger_mode(pg1=(mode == "Internal (PG1)"),
                                       pg2=(mode == "Internal (PG2)"),
                                       ext=(mode == "External")):
            self.log_message(f"SET_TRIGGER: {mode}")

    def apply_frequency(self):
        if not self.laser.is_connected():
            return
        mode = self.trigger_var.get()
        if mode == "External":
            return
        try:
            hz = int(self.internal_freq_hz.get())
        except ValueError:
            messagebox.showerror("Error", "Invalid Frequency")
            return
        if mode == "Internal (PG1)":
            self.laser.set_pg1_frequency(hz)
        else:
            self.laser.set_pg2_frequency(hz)
        self.log_message(f"SET_FREQ: {hz} Hz")

    # --- Monitoring ------------------------------------------------------

    def process_gui_queue(self):
        try:
            while not self.gui_queue.empty():
                msg_type, data = self.gui_queue.get_nowait()
                if msg_type == "status":
                    self.update_gui_with_status(data)
                elif msg_type == "disconnect":
                    self.handle_disconnection_ui()
        except Exception:
            pass
        finally:
            self.master.after(200, self.process_gui_queue)

    def toggle_monitoring(self):
        if self.is_monitoring.get():
            if self.status_monitor_timer is None and self.laser.is_connected():
                self.status_monitor_timer = RepeatingTimer(
                    0.5, self.read_status_to_queue)
                self.status_monitor_timer.daemon = True
                self.status_monitor_timer.start()
        else:
            if self.status_monitor_timer:
                self.status_monitor_timer.cancel()
                self.status_monitor_timer = None
                self.current_status_text.set("Current Status: N/A")

    def read_status_to_queue(self):
        """Background thread: read status. The CSV row is written by the
        driver itself (throttled to one line per 10 s while the LD is on), so
        there is exactly one writer and one schema."""
        if not self.laser.is_connected():
            return
        if self.laser.update_status():
            self.consecutive_errors = 0
            self.gui_queue.put(("status", self.laser.status.copy()))
        else:
            self.consecutive_errors += 1
            if self.consecutive_errors > self.MAX_RETRIES:
                self.gui_queue.put(("disconnect", None))

    def update_gui_with_status(self, status):
        ld, tec = status.get('ld_on'), status.get('tec_on')
        temp = status.get('ld_temp', 0)
        bias, pulse = status.get('bias', 0), status.get('pulse', 0)

        self.live_status["ld_status"].set("ON" if ld else "OFF")
        self.live_status["tec_status"].set("ON" if tec else "OFF")
        self.live_status["temp"].set(f"{temp:.2f} °C")
        self.live_status["bias"].set(f"{bias:.2f} mA")
        self.live_status["pulse"].set(f"{pulse:.2f} mA")

        # Blank-by-design when the board's photodiode is dead, rather than
        # showing the log formula's 3.162 mA floor as if it were a measurement.
        if status.get('pd_valid'):
            self.live_status["pd"].set(
                f"{status.get('pd_current', 0.0) * 1000.0:.4f} mA")
        else:
            self.live_status["pd"].set("n/a (no PD)")

        self.current_status_text.set(
            f"Status: LD={'ON' if ld else 'OFF'}, Temp={temp:.1f}C, "
            f"Bias={bias:.1f}mA, Pulse={pulse:.1f}mA")

    def handle_disconnection_ui(self):
        self.conn_status_label.config(text="Status: Disconnected",
                                      foreground="red")
        self.connect_btn.config(text="Connect", command=self.auto_connect,
                                style="Connect.TButton")
        self.device_combo.state(["!disabled"])
        self.is_monitoring.set(False)
        self.toggle_monitoring()
        self.log_message("Connection lost (Timeout or Error).", "error")

    # --- Log / plot ------------------------------------------------------

    def _create_log_frame(self):
        frame = ttk.LabelFrame(self.master, text="Log Viewer & Data")
        frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(5, 10))

        self.log_notebook = ttk.Notebook(frame)
        self.log_notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        session_tab = ttk.Frame(self.log_notebook, padding=5)
        self.log_notebook.add(session_tab, text="Session Log")
        self.session_log_text = scrolledtext.ScrolledText(
            session_tab, wrap=tk.WORD, state="disabled", height=10,
            bg="#f8f9fa", font=("Monospace", 9))
        self.session_log_text.pack(fill=tk.BOTH, expand=True)

        plot_tab = ttk.Frame(self.log_notebook, padding=5)
        if MATPLOTLIB_AVAILABLE:
            self.log_notebook.add(plot_tab, text="Data Plotter")
            pc = ttk.Frame(plot_tab)
            pc.pack(fill=tk.X, pady=5)
            ttk.Label(pc, text="Select CSV:").pack(side=tk.LEFT, padx=(0, 5))
            self.csv_file_var = tk.StringVar()
            self.csv_combo = ttk.Combobox(pc, textvariable=self.csv_file_var,
                                          state="readonly", width=28)
            self.csv_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
            ttk.Button(pc, text="Plot Window",
                       command=self.plot_csv_data_popup).pack(side=tk.LEFT,
                                                              padx=5)
            ttk.Button(pc, text="Refresh",
                       command=self.populate_csv_combo).pack(side=tk.LEFT)
            ttk.Label(plot_tab,
                      text=f"Data is auto-saved to:\n{LOG_DIR}\n\n"
                           "(rows are written every 10 s while the LD is on)",
                      justify=tk.CENTER, foreground="gray").pack(
                fill=tk.BOTH, expand=True)
        else:
            self.log_notebook.add(plot_tab, text="Data Plotter (Disabled)")
            ttk.Label(plot_tab,
                      text="Install matplotlib/pandas to enable.").pack(pady=20)
        self.populate_csv_combo()

    def populate_csv_combo(self):
        if not MATPLOTLIB_AVAILABLE:
            return
        self.csv_combo.set('')
        files = sorted(glob.glob(os.path.join(LOG_DIR, "laser_data_*.csv")),
                       key=os.path.getmtime, reverse=True)
        self.csv_combo['values'] = [os.path.basename(f) for f in files]
        if files:
            self.csv_combo.set(os.path.basename(files[0]))

    def plot_csv_data_popup(self):
        if not MATPLOTLIB_AVAILABLE:
            return
        filename = self.csv_file_var.get()
        if not filename:
            return
        filepath = os.path.join(LOG_DIR, filename)

        try:
            df = pd.read_csv(filepath, on_bad_lines='skip')
            df['timestamp'] = pd.to_datetime(df['timestamp'], errors='coerce')
            df = df.dropna(subset=['timestamp'])
            if df.empty:
                messagebox.showinfo("Plot", "No usable rows in that file.")
                return
        except Exception as e:
            self.log_message(f"Plot error: {e}", "error")
            messagebox.showerror("Error", f"Could not read file:\n{e}")
            return

        if self.plot_window and self.plot_window.winfo_exists():
            self.plot_window.lift()
        else:
            self.plot_window = Toplevel(self.master)
            self.plot_window.title(f"Plot: {filename}")
            self.plot_window.geometry("900x700")
            fig, axes = plt.subplots(3, 1, sharex=True, figsize=(9, 7))
            fig.subplots_adjust(hspace=0.15)
            self.plot_window.canvas = FigureCanvasTkAgg(
                fig, master=self.plot_window)
            self.plot_window.canvas.get_tk_widget().pack(fill=tk.BOTH,
                                                         expand=True)
            self.plot_window.axes = axes
            self.plot_window.fig = fig

        try:
            for ax in self.plot_window.axes:
                ax.clear()
            ax1, ax2, ax3 = self.plot_window.axes

            ax1.plot(df['timestamp'], df['temp_c'], 'r-')
            ax1.set_ylabel('Temp (°C)')
            ax1.grid(True)

            ax2.plot(df['timestamp'], df['bias_ma'], 'b-', label='Bias')
            ax2.plot(df['timestamp'], df['pulse_ma'], 'g-', label='Pulse')
            ax2.set_ylabel('Current (mA)')
            ax2.legend(loc='best', fontsize=8)
            ax2.grid(True)

            # The photodiode panel is the point of this plot for drift work --
            # bias/pulse above are setpoints and are flat by construction.
            if 'pd_current' in df.columns:
                pdv = pd.to_numeric(df['pd_current'], errors='coerce') * 1000.0
                if pdv.notna().any():
                    ax3.plot(df['timestamp'], pdv, 'm-')
                    ax3.set_ylabel('PD (mA)')
                else:
                    ax3.text(0.5, 0.5,
                             "No photodiode data in this file\n"
                             "(this board's monitor PD reads zero)",
                             ha='center', va='center', color='gray',
                             transform=ax3.transAxes)
                    ax3.set_ylabel('PD (mA)')
            ax3.grid(True)

            self.plot_window.fig.autofmt_xdate()
            self.plot_window.canvas.draw()
            self.log_message(f"Plotted {filename}")
        except Exception as e:
            self.log_message(f"Plot error: {e}", "error")
            messagebox.showerror("Error", f"Could not plot file:\n{e}")

    # --- Status bar ------------------------------------------------------

    def _create_status_bar(self):
        bar = ttk.Frame(self.master, relief=tk.SUNKEN, padding=(5, 3))
        bar.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Label(bar, textvariable=self.current_status_text,
                  anchor=tk.W).pack(side=tk.LEFT, fill=tk.X, expand=True,
                                    padx=5)
        ttk.Checkbutton(bar, text="Monitor", variable=self.is_monitoring,
                        command=self.toggle_monitoring,
                        style="Toolbutton").pack(side=tk.RIGHT, padx=5)
        ttk.Label(bar, textvariable=self.elapsed_time_var,
                  anchor=tk.E).pack(side=tk.RIGHT, padx=10)
        ttk.Label(bar, textvariable=self.clock_var,
                  anchor=tk.E).pack(side=tk.RIGHT, padx=10)

    def _update_status_bar_clock(self):
        try:
            now = datetime.now()
            self.clock_var.set(f"Time: {now.strftime('%Y-%m-%d %H:%M:%S')}")
            elapsed = now - self.start_time
            m, s = divmod(elapsed.seconds, 60)
            h, m = divmod(m, 60)
            self.elapsed_time_var.set(
                f"Elapsed: {h + elapsed.days * 24:02}:{m:02}:{s:02}")
            self.master.after(1000, self._update_status_bar_clock)
        except tk.TclError:
            pass

    # --- Settings --------------------------------------------------------

    def _load_settings(self):
        try:
            with open(CONFIG_FILE, 'r') as f:
                s = json.load(f)
            self.bias_val.set(s.get("bias_ma", 0.0))
            self.pulse_val.set(s.get("pulse_ma", 0.0))
            self.trigger_var.set(s.get("trigger_mode", "External"))
            self.internal_freq_hz.set(s.get("internal_freq_hz", "10000000"))
        except Exception:
            pass  # first run, or unreadable -- defaults are fine

    def _save_settings(self):
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump({
                    "bias_ma": self.bias_val.get(),
                    "pulse_ma": self.pulse_val.get(),
                    "trigger_mode": self.trigger_var.get(),
                    "internal_freq_hz": self.internal_freq_hz.get(),
                }, f, indent=4)
        except Exception as e:
            print(f"Could not save settings: {e}")

    # --- Shutdown --------------------------------------------------------

    def safe_shutdown_device(self):
        """Leave the board dark and idle. Also matters for the USB itself:
        hidapi's libusb backend detaches the kernel driver on open, and a
        process that exits without close() leaves the device unusable until
        the cable is replugged."""
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
        raise

# managers/laser_manager.py
import time
import os
import collections
import threading
from datetime import datetime, timedelta
from tkinter import messagebox
import matplotlib.dates as mdates
import pandas as pd
import logging
from tkinter import messagebox, filedialog  
from logging.handlers import TimedRotatingFileHandler
import tkinter as tk
import tkinter.simpledialog as sd 

class LaserManager:
    def __init__(self, app):
        self.app = app
        self.wavelengths = ["375nm", "405nm", "450nm", "473nm"]
        self.laser_port_mapping = {
            wl: path.encode('utf-8') for wl, path in self.app.laser_port_mapping.items()
        }
        self.laser_log_dir = self.app.laser_log_dir
        self.laser_instances = {}

        # [CRITICAL FIX] Added missing connection tracking flags
        self.comm_error_flags = {wl: False for wl in self.wavelengths}
        self._disc_reason    = {wl: "USB"  for wl in self.wavelengths}  # "USB" or "INTERLOCK"
        self.expected_connections = set()
        self._reconnecting = set()  # wavelengths with an in-flight background reconnect

        self.plot_history = {}
        for wl in self.wavelengths:
            self.plot_history[wl] = {
                "time": collections.deque(maxlen=90000),
                "temp": collections.deque(maxlen=90000),
                "pulse": collections.deque(maxlen=90000),
                "bias": collections.deque(maxlen=90000),
                "ld_on": collections.deque(maxlen=90000)   # 1 while the laser diode is ON
            }
            self.load_todays_log(wl)

        self.laser_session_start = None
        self.laser_after_id = None
        self.watchdog_running = False
        self._last_log_time = {wl: 0.0 for wl in self.wavelengths}
        self.start_interlock_watchdog()

    def auto_connect_laser(self):
        last_wls = getattr(self.app, "last_connected_wls", [])
        if not last_wls: return

        msg = f"The following lasers were connected last time:\n[{', '.join(last_wls)}]\n\nDo you want to restore these connections?"
        if not messagebox.askyesno("Laser Auto-Connect", msg, parent=self.app.master):
            self.app._log("Auto-connect cancelled by user.")
            return

        for wl in last_wls:
            if wl in self.laser_instances:
                self.connect_single_laser(wl)

                inst = self.laser_instances.get(wl)
                if inst and inst.is_connected() and inst.update_status():
                    if inst.status.get('ld_on', False):
                            off_msg = f"⚠️ [ {wl} ] Laser LD is currently ON.\n\nDo you want to turn it OFF now?"
                            if messagebox.askyesno("LD Status Alert", off_msg):
                                if self.laser_after_id:
                                    self.app.master.after_cancel(self.laser_after_id)
                                    self.laser_after_id = None
                                
                                inst.set_ld_on(False)
                                #time.sleep(0.5) 
                                inst.update_status() 
                                
                                # 3. 화면(UI) 강제 즉시 업데이트
                                vars_dict = self.app.ui.laser_tabs_data.get(wl)
                                if vars_dict:
                                    vars_dict["ld_status"].set("OFF")
                                    self.app.ui.update_laser_status_colors(wl, False, inst.status.get('tec_on', False))
                                
                                self.app._log(f"🛡️  Safety: {wl} LD turned OFF by user request.")
                                
                                # 4. 루프 재개
                                self.update_laser_status_loop()
                            else:
                                self.app._log(f"⚠️ Warning: {wl} LD remains ON as per user request.")

    def connect_single_laser(self, wl):
        inst = self.laser_instances.get(wl)
        vars_dict = self.app.ui.laser_tabs_data.get(wl)
        target_path = self.laser_port_mapping.get(wl)
        if not inst or not vars_dict: return

        self.app._log(f"Connecting to {wl} via {target_path}...")
        success, msg = inst.connect(dev_path=target_path)

        if success:
            self.expected_connections.add(wl) # Register for auto-recovery
            vars_dict["conn_status_txt"].set("Connected")
            vars_dict["conn_label_obj"].config(foreground="#28a745")
            self.comm_error_flags[wl] = False
            if inst.update_status():
                vars_dict["ld_status"].set("ON" if inst.status.get('ld_on', False) else "OFF")
            
            self.on_laser_trigger_change_multi(wl)
            self.laser_session_start = time.time()
            self.update_laser_status_loop()
            self.app._log(f"✅ {wl} Connected successfully.")
            self.app.save_app_config()
        else:
            self.app._log(f"❌ {wl} Connection Failed: {msg}")
            messagebox.showerror("Connection Error", f"Failed to connect {wl}: {msg}")

    def disconnect_single_laser(self, wl):
        if wl in self.expected_connections:
            self.expected_connections.remove(wl) # Unregister from auto-recovery
        inst = self.laser_instances.get(wl)
        vars_dict = self.app.ui.laser_tabs_data.get(wl)
        if not vars_dict: return
        try:
            if inst: inst.disconnect()
        except: pass
        vars_dict["conn_status_txt"].set("Disconnected")
        vars_dict["conn_label_obj"].config(foreground="red")
        vars_dict["ld_status"].set("Disconnected")
        idx = self.wavelengths.index(wl)
        self.app.ui.laser_sub_notebook.tab(idx, image=self.app.ui.tab_led_red, compound=tk.RIGHT)
        self.app._log(f"🔌 {wl} Disconnected by user.")


    def show_interlock_recovery_dialog(self, wl, inst):
        """Custom dialog for interlock/USB recovery."""
        reason = self._disc_reason.get(wl, "USB")
        if reason == "USB":
            title_txt = f"USB Reconnected - {wl}"
            body_txt  = f"[{wl}] USB connection restored.\nDo you want to re-enable control?"
        else:
            title_txt = f"Interlock Recovery - {wl}"
            body_txt  = f"[{wl}] Interlock release detected.\nDo you want to reconnect?"

        dialog = tk.Toplevel(self.app.master)
        dialog.title(title_txt)
        dialog.geometry("380x150")
        dialog.attributes("-topmost", True)
        dialog.grab_set()

        tk.Label(dialog, text=body_txt, font=("Arial", 10, "bold"), pady=15).pack()

        def on_normal_connect():
            dialog.destroy()
            self._process_post_reconnect(wl, inst, is_admin=False)

        def on_admin_connect():
            pwd = sd.askstring("Admin", "Enter Admin Password:", show='*', parent=dialog)
            if pwd == "1234": 
                self.app._log(f"[INFO] Admin access granted for {wl}.")
                dialog.destroy()
                self._process_post_reconnect(wl, inst, is_admin=True)
            elif pwd is not None:
                messagebox.showerror("Error", "Incorrect password.", parent=dialog)

        def on_cancel():
            self.app._log(f"[WARNING] Connection cancelled by user for {wl}.")
            inst.disconnect()
            self._handle_comm_failure(wl, self.wavelengths.index(wl))
            dialog.destroy()

        btn_frame = tk.Frame(dialog)
        btn_frame.pack(pady=10)

        tk.Button(btn_frame, text="Normal Connect", width=14, command=on_normal_connect).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="Force Connect (Admin)", width=20, command=on_admin_connect, fg="red").pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="Cancel", width=10, command=on_cancel).pack(side=tk.LEFT, padx=5)

    def start_interlock_watchdog(self):
        """Starts a lightweight background thread to monitor interlock status every 1 second."""
        if getattr(self, 'watchdog_running', False):
            return
            
        self.watchdog_running = True
        threading.Thread(target=self._interlock_watchdog_loop, daemon=True).start()
        self.app._log("[INFO] Safety Interlock Watchdog started (1s polling).")

    def _interlock_watchdog_loop(self):
        """Background thread: polls hardware every 1 s for interlock/USB-loss events.
        SAFETY RULE: HID I/O (update_status, set_ld_on) runs here (bg thread).
        All Tkinter calls (_log, widget.set, .config) MUST be dispatched via
        master.after(0, ...) — Tkinter is single-threaded and not reentrant.
        """
        while self.watchdog_running:
            for wl in self.wavelengths:
                inst = self.laser_instances.get(wl)
                ui_vars = self.app.ui.laser_tabs_data.get(wl)

                if inst and inst.is_connected() and not self.comm_error_flags.get(wl, False):
                    try:
                        status_ok = inst.update_status()
                        if status_ok:
                            is_interlock = inst.status.get('alarm', False) or inst.status.get('interlock', False)

                            if is_interlock and not self.comm_error_flags[wl]:
                                self.comm_error_flags[wl] = True
                                self._disc_reason[wl] = "INTERLOCK"
                                try:
                                    inst.set_ld_on(False)   # HW command — safe from bg thread
                                    inst.set_tec_on(False)
                                except Exception:
                                    pass
                                # _log and UI updates MUST run on the main thread (Tkinter is not thread-safe)
                                if hasattr(self.app, 'master') and ui_vars:
                                    self.app.master.after(0, lambda w=wl: (
                                        self.app._log(f"[CRITICAL] Interlock tripped for {w}! LD/TEC forced OFF."),
                                        self._trigger_interlock_ui_alert(w)
                                    ))
                        else:
                            # comm failed → USB disconnected (driver already set device=None via IOError)
                            if not self.comm_error_flags.get(wl, False):
                                self.comm_error_flags[wl] = True
                                self._disc_reason[wl] = "USB"
                                if hasattr(self.app, 'master') and ui_vars:
                                    idx = self.wavelengths.index(wl)
                                    self.app.master.after(0, lambda w=wl, i=idx: (
                                        self.app._log(f"🔌 [{w}] USB comm lost (watchdog detected)."),
                                        self._handle_comm_failure(w, i, "USB")
                                    ))

                    except Exception:
                        # Unexpected exception — treat as comm failure; log on main thread only
                        if not self.comm_error_flags.get(wl, False):
                            self.comm_error_flags[wl] = True
                            self._disc_reason[wl] = "USB"
                            if hasattr(self.app, 'master') and ui_vars:
                                idx = self.wavelengths.index(wl)
                                self.app.master.after(
                                    0, lambda w=wl, i=idx: self._handle_comm_failure(w, i, "USB"))

            time.sleep(1.0)

    def _trigger_interlock_ui_alert(self, wl):
        """Updates UI immediately when interlock is detected by the watchdog."""
        ui_vars = self.app.ui.laser_tabs_data.get(wl)
        if ui_vars:
            ui_vars["ld_status"].set("🔒 INTERLOCK")
            if "ld_label_obj" in ui_vars: 
                ui_vars["ld_label_obj"].config(foreground="#fd7e14")
            self.app.ui.update_laser_status_colors(wl, False, False)
            
        inst = self.laser_instances.get(wl)
        if inst:
            self.show_interlock_recovery_dialog(wl, inst)

    def _process_post_reconnect(self, wl, inst, is_admin=False):
        inst.update_status()
        is_ld_on = inst.status.get('ld_on', False)

        if is_ld_on:
            if is_admin:
                off_msg = f"[WARNING] Hardware LD for {wl} is currently ON!\n\nFor safety, do you want to turn OFF the laser?\n(Click No to keep it ON)"
                if messagebox.askyesno("LD Status Alert (Admin)", off_msg):
                    inst.set_ld_on(False)
                    inst.set_tec_on(False)
                    self.app._log(f"[INFO] Safety: {wl} forced OFF by Admin.")
                else:
                    self.app._log(f"[WARNING] Safety: {wl} kept ON by Admin.")
            else:
                inst.set_ld_on(False)
                inst.set_tec_on(False)
                self.app._log(f"[INFO] Safety: {wl} forced OFF due to normal user privileges.")
                messagebox.showinfo("Safety Action", "Connected with normal privileges. Laser has been safely forced OFF.")
        
        time.sleep(0.1)
        inst.update_status()
        self.comm_error_flags[wl] = False
        
        if hasattr(self.app, 'ui') and hasattr(self.app.ui, 'laser_tabs_data'):
            vars_dict = self.app.ui.laser_tabs_data.get(wl)
            if vars_dict and "trigger_mode" in vars_dict:
                vars_dict["trigger_mode"].set("External") 
                self.app._log(f"[INFO] {wl} Trigger mode forced to External after interlock recovery.")
        
        if hasattr(self, 'on_laser_trigger_change_multi'):
            self.on_laser_trigger_change_multi(wl)
        # =====================================================================

    def manual_refresh_laser(self, wl=None):
        self.laser_session_start = time.time()
        if wl:
            self.app._log(f"Refreshing {wl}...")
        else:
            self.app._log("Refreshing lasers...")
        self.update_laser_status_loop()

    def set_laser_ld_safe(self, target_wl, state):
        if state is True and not getattr(getattr(self.app, 'access_mgr', None), 'unlocked', True):
            from tkinter import messagebox
            messagebox.showwarning(
                "🔒 System Locked",
                "Controls are locked.\n\nPlease click 'Unlock Controls' (top banner) before turning on the Laser.")
            return

        active_lasers = []

        # 1. 켜야 하는 상황(state == True)일 때 기존에 켜진 레이저 탐색
        if state is True:
            for wl, inst in self.laser_instances.items():
                if wl != target_wl and inst.is_connected():
                    if self.app.ui.laser_tabs_data[wl]["ld_status"].get() == "ON":
                        active_lasers.append(wl)

            if active_lasers:
                msg = f"Laser {', '.join(active_lasers)} is currently ON.\n\n" \
                      f"To turn on {target_wl}, the others must be turned OFF.\n" \
                      f"Proceed?"

                if not messagebox.askyesno("Safety Interlock", msg):
                    self.app._log(f"[WARNING] Operation cancelled: {target_wl} ON blocked by user.")
                    return

        inst = self.laser_instances.get(target_wl)
        if not inst or not inst.is_connected():
            return

        def apply_task():
            try:
                # (A) 먼저 켜져있는 레이저들을 안전하게 끕니다.
                if state is True and active_lasers:
                    for wl in active_lasers:
                        old_inst = self.laser_instances.get(wl)
                        if old_inst:
                            old_inst.set_ld_on(False) # 동기적 실행 (완료될 때까지 대기)
                            
                            def update_old_ui(w=wl):
                                self.app.ui.laser_tabs_data[w]["ld_status"].set("OFF")
                                self.app.ui.update_laser_status_colors(w, False, False)
                                self.app._log(f"[INFO] Safety: Auto-shutdown completed for {w}")
                            
                            self.app.master.after(0, update_old_ui)
                    
                    time.sleep(0.5) 

                # (B) 타겟 레이저 상태 변경
                inst.set_ld_on(state)
                time.sleep(0.1)
                
                def update_target_ui():
                    self.app._log(f"[INFO] Command Sent: Laser {target_wl} LD -> {'ON' if state else 'OFF'}")
                    self.laser_session_start = time.time()
                    if self.laser_after_id:
                        self.app.master.after_cancel(self.laser_after_id)
                    self.update_laser_status_loop()
                    
                self.app.master.after(0, update_target_ui)

            except Exception as e:
                self.app.master.after(0, lambda: self.app._log(f"[ERROR] LD control error for {target_wl}: {e}"))

        threading.Thread(target=apply_task, daemon=True).start()


    def apply_laser_frequency_multi(self, wl):
        """Apply trigger mode and frequency to the specified laser wavelength."""
        inst = self.laser_instances.get(wl)
        vars_dict = self.app.ui.laser_tabs_data.get(wl)

        if inst and inst.is_connected() and vars_dict:
            try:
                hz = int(vars_dict["freq_hz"].get())
                mode = vars_dict["trigger_mode"].get()

                pg1 = (mode == "Internal (PG1)")
                pg2 = (mode == "Internal (PG2)")
                ext = (mode == "External")

                # Define the background task to prevent UI freezing
                def apply_task():
                    try:
                        inst.set_trigger_mode(pg1, pg2, ext)
                        time.sleep(0.1)

                        if pg1:
                            inst.set_pg1_frequency(hz)
                        elif pg2:
                            inst.set_pg2_frequency(hz)
                        
                        time.sleep(0.1)

                        # Safely update the UI from the main thread
                        def update_ui():
                            if "current_mode_disp" in vars_dict:
                                vars_dict["current_mode_disp"].set(f"Current: {mode}")
                            self.app._log(f"[INFO] Laser {wl} Config: {mode}, {hz} Hz applied.")

                        self.app.master.after(0, update_ui)

                    except Exception as e:
                        error_msg = f"[ERROR] Failed applying frequency to {wl}: {e}"
                        self.app.master.after(0, lambda: self.app._log(error_msg))

                # Start the hardware communication in a separate thread
                threading.Thread(target=apply_task, daemon=True).start()

            except ValueError:
                messagebox.showerror("Error", f"Invalid frequency for {wl}. Must be an integer.")

    def set_laser_tec_multi(self, wl, state):
        inst = self.laser_instances.get(wl)
        if inst and inst.is_connected():
            def apply_task():
                try:
                    inst.set_tec_on(state)
                    
                    def update_ui():
                        self.app._log(f"[INFO] Command Sent: Laser {wl} TEC -> {'ON' if state else 'OFF'}")
                        self.laser_session_start = time.time()
                        if self.laser_after_id:
                            self.app.master.after_cancel(self.laser_after_id)
                        self.app.master.after(500, self.update_laser_status_loop)
                        
                    self.app.master.after(0, update_ui)
                except Exception as e:
                    self.app.master.after(0, lambda: self.app._log(f"[ERROR] TEC control error for {wl}: {e}"))

            threading.Thread(target=apply_task, daemon=True).start()

    def apply_laser_currents_multi(self, wl):
        inst = self.laser_instances.get(wl)
        vars_dict = self.app.ui.laser_tabs_data.get(wl)

        if inst and inst.is_connected() and vars_dict:
            try:
                bias = vars_dict["bias_set"].get()
                pulse = vars_dict["pulse_set"].get()

                def apply_task():
                    try:
                        inst.set_bias_current(bias)
                        time.sleep(0.2)
                        inst.set_pulse_current(pulse)
                        time.sleep(0.2)
                        # _log touches the Tk log widget, so it must run on the main thread.
                        self.app.master.after(0, lambda: self.app._log(
                            f"[INFO] Applied to {wl}: Bias={bias:.2f}mA, Pulse={pulse:.2f}mA"))
                    except Exception as e:
                        err = str(e)
                        self.app.master.after(0, lambda m=err: self.app._log(
                            f"[ERROR] Failed applying currents to {wl}: {m}"))

                threading.Thread(target=apply_task, daemon=True).start()

                if self.laser_after_id:
                    self.app.master.after_cancel(self.laser_after_id)
                self.app.master.after(500, self.update_laser_status_loop)
            except Exception as e:
                self.app._log(f"[ERROR] Configuration error for {wl}: {e}")

    def _handle_comm_failure(self, wl, idx, reason="USB"):
        """Handle UI when communication is lost.
        reason: "USB"  = physical cable/port disconnect
                "INTERLOCK" = device reachable but interlock tripped
        """
        inst = self.laser_instances.get(wl)
        if inst:
            try:
                inst.set_ld_on(False)
                inst.set_tec_on(False)
            except Exception:
                pass
        ui_vars = self.app.ui.laser_tabs_data.get(wl)
        if not self.comm_error_flags[wl]:
            self._disc_reason[wl] = reason
            if reason == "USB":
                self.app._log(f"🔌 [ {wl} ] USB connection lost — check cable/port.")
            else:
                self.app._log(f"🔒 [ {wl} ] Interlock triggered — check safety circuit.")
            self.comm_error_flags[wl] = True

        reason_now = self._disc_reason.get(wl, "USB")
        label  = "USB DISCONNECTED" if reason_now == "USB" else "INTERLOCK"
        color  = "#dc3545"          if reason_now == "USB" else "#fd7e14"   # red vs orange

        self.app.ui.laser_sub_notebook.tab(idx, text=f" {wl} ", image=self.app.ui.tab_led_red, compound=tk.RIGHT)
        if ui_vars:
            ui_vars["ld_status"].set(label)
            if "ld_label_obj" in ui_vars:
                ui_vars["ld_label_obj"].config(foreground=color)
            self.app.ui.update_laser_status_colors(wl, False, False)

    def on_laser_trigger_change(self, event=None):
        """Initializes all trigger states at startup, or handles active tab on event."""
        try:
            if event is None:
                # [FIX] Startup: Initialize UI states for ALL wavelengths
                for wl in self.wavelengths:
                    self.on_laser_trigger_change_multi(wl)
            else:
                # UI Event: Handle only the currently selected tab
                idx = self.app.ui.laser_sub_notebook.index(self.app.ui.laser_sub_notebook.select())
                wl = self.wavelengths[idx]
                self.on_laser_trigger_change_multi(wl)
        except Exception as e:
            self.app._log(f"⚠️ Trigger initialization error: {e}")

    def on_laser_trigger_change_multi(self, wl):
        """Fix: Toggles input state correctly between External and Internal modes"""
        vars_dict = self.app.ui.laser_tabs_data.get(wl)
        if not vars_dict: return

        mode = vars_dict["trigger_mode"].get()
        entry = vars_dict.get("freq_entry_obj")
        btn = vars_dict.get("freq_apply_btn_obj")
        frame = vars_dict.get("trig_frame_obj")

        if mode == "External":
            if entry: entry.config(state="disabled")
            if btn: btn.config(state="disabled")
            if frame: frame.config(text=f"Trigger Control - DISABLED (External) [{wl}]")
        else: # [Indentation Fix] Internal modes now correctly enable the entry
            if entry: entry.config(state="normal")
            if btn: btn.config(state="normal")
            if frame: frame.config(text=f"Trigger Control - ENABLED (Internal) [{wl}]")

        inst = self.laser_instances.get(wl)
        if inst and inst.is_connected():
            self.apply_laser_frequency_multi(wl)

    def load_historical_laser_data(self, wl=None):
        log_dir = self.laser_log_dir
        if not wl:
            idx = self.app.ui.laser_sub_notebook.index(self.app.ui.laser_sub_notebook.select())
            wl = self.wavelengths[idx]

        vars_dict = self.app.ui.laser_tabs_data.get(wl)
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
                self.app._log(f"Success: Historical data for {wl} loaded.")
            except Exception as e:
                messagebox.showerror("Error", f"Failed to load data: {e}")

    def setup_laser_logger(self):
        os.makedirs(self.laser_log_dir, exist_ok=True)

        self.laser_logger = logging.getLogger('LaserSession')
        self.laser_logger.setLevel(logging.INFO)

        if not self.laser_logger.handlers:
            log_path = os.path.join(self.laser_log_dir, "laser_log")
            handler = TimedRotatingFileHandler(log_path, when='midnight', interval=1)
            handler.suffix = "_%Y-%m-%d.txt"
            handler.setFormatter(logging.Formatter('%(asctime)s | %(message)s'))
            self.laser_logger.addHandler(handler)

    def _log_laser(self, wl, msg):
        """Logs session messages into wavelength-isolated text files and distinct UI widgets."""
        # 1. Write to wavelength-isolated file
        try:
            today_str = datetime.now().strftime('%Y-%m-%d')
            log_file = os.path.join(self.laser_log_dir, f"laser_log_{wl}_{today_str}.txt")
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"[{timestamp}] {msg}\n")
        except Exception as e:
            print(f"Failed to write laser text log for {wl}: {e}")

        # 2. Write to wavelength-isolated UI ScrolledText widget
        time_str = datetime.now().strftime('%H:%M:%S')
        if hasattr(self.app, 'ui') and hasattr(self.app.ui, 'laser_tabs_data'):
            vars_dict = self.app.ui.laser_tabs_data.get(wl)
            if vars_dict and "log_text_obj" in vars_dict:
                widget = vars_dict["log_text_obj"]
                widget.config(state="normal")
                widget.insert(tk.END, f"[{time_str}] {msg}\n")
                widget.config(state="disabled")
                widget.see(tk.END)

    def load_today_laser_log(self):
        """Loads today's text logs for each wavelength upon application startup."""
        today_str = datetime.now().strftime('%Y-%m-%d')
        for wl in self.wavelengths:
            log_file = os.path.join(self.laser_log_dir, f"laser_log_{wl}_{today_str}.txt")
            if os.path.exists(log_file):
                try:
                    with open(log_file, 'r', encoding='utf-8') as f:
                        content = f.read()
                    vars_dict = self.app.ui.laser_tabs_data.get(wl)
                    if vars_dict and "log_text_obj" in vars_dict:
                        widget = vars_dict["log_text_obj"]
                        widget.config(state="normal")
                        widget.insert(tk.END, content)
                        widget.config(state="disabled")
                        widget.see(tk.END)
                except Exception as e:
                    print(f"Failed to load today's laser text log for {wl}: {e}")

    def save_laser_realtime_data(self, wl, temp, pulse, ld_on, tec_on):
        """Saves telemetry data to CSV file with complete state monitoring flags."""
        try:
            log_dir = getattr(self.app, 'laser_log_dir', self.laser_log_dir)
            today_str = datetime.now().strftime('%Y%m%d')
            file_path = os.path.join(log_dir, f"laser_data_{wl}_{today_str}.csv")
            file_exists = os.path.isfile(file_path)

            mode, freq, bias = "Unknown", "0", 0.0
            if hasattr(self.app, 'ui') and hasattr(self.app.ui, 'laser_tabs_data'):
                vars_dict = self.app.ui.laser_tabs_data.get(wl)
                if vars_dict:
                    mode = vars_dict["trigger_mode"].get()
                    freq = vars_dict["freq_hz"].get()
                    bias = vars_dict["bias_set"].get()

            with open(file_path, "a", buffering=1, encoding="utf-8") as f:
                if not file_exists:
                    f.write("timestamp,temp_c,pulse_ma,bias_ma,trigger_mode,freq_hz,ld_on,tec_on\n")
                now_iso = datetime.now().isoformat()
                f.write(f"{now_iso},{temp:.2f},{pulse:.2f},{float(bias):.2f},{mode},{freq},{1 if ld_on else 0},{1 if tec_on else 0}\n")
            
        except Exception as e:
            self.app._log(f"[ERROR] Laser Logging Error ({wl}): {e}")

    def load_todays_log(self, wl):
        """Restores complete telemetry metrics matching the exact CSV structure safely."""
        try:
            log_dir = getattr(self.app, 'laser_log_dir', self.laser_log_dir)
            today_str = datetime.now().strftime('%Y%m%d')
            file_path = os.path.join(log_dir, f"laser_data_{wl}_{today_str}.csv")

            if os.path.exists(file_path):
                df = pd.read_csv(file_path)

                for _, row in df.tail(90000).iterrows():
                    try:
                        dt = datetime.fromisoformat(str(row['timestamp']))
                        self.plot_history[wl]["time"].append(dt)
                        self.plot_history[wl]["temp"].append(float(row['temp_c']))
                        self.plot_history[wl]["pulse"].append(float(row['pulse_ma']))
                        self.plot_history[wl]["bias"].append(float(row.get('bias_ma', 0.0)))
                        self.plot_history[wl]["ld_on"].append(int(float(row.get('ld_on', 0))))
                    except Exception:
                        pass

                if hasattr(self.app, '_log'):
                    self.app._log(f"[INFO] Loaded previous log data for {wl}.")
        except Exception as e:
            if hasattr(self.app, '_log'):
                self.app._log(f"[WARNING] Could not load past logs for {wl}: {e}")

    def preload_laser_history(self):
        """Restores historical telemetry for the past 24 hours safely without memory mismatch."""
        from datetime import timedelta
        now = datetime.now()
        start_point = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        dates_to_check = [(now - timedelta(days=1)).strftime('%Y%m%d'), now.strftime('%Y%m%d')]

        for wl in self.wavelengths:
            self.plot_history[wl]["time"].clear()
            self.plot_history[wl]["temp"].clear()
            self.plot_history[wl]["pulse"].clear()
            self.plot_history[wl]["bias"].clear()
            self.plot_history[wl]["ld_on"].clear()
            total_points = 0

            for date_str in dates_to_check:
                log_file = os.path.join(self.laser_log_dir, f"laser_data_{wl}_{date_str}.csv")
                if os.path.exists(log_file):
                    try:
                        df = pd.read_csv(log_file)
                        for _, row in df.iterrows():
                            try:
                                ts = datetime.fromisoformat(row['timestamp'])
                                if ts >= start_point:
                                    self.plot_history[wl]["time"].append(ts)
                                    self.plot_history[wl]["temp"].append(float(row['temp_c']))
                                    self.plot_history[wl]["pulse"].append(float(row['pulse_ma']))
                                    self.plot_history[wl]["bias"].append(float(row.get('bias_ma', 0.0)))
                                    self.plot_history[wl]["ld_on"].append(int(float(row.get('ld_on', 0))))
                                    total_points += 1
                            except: continue
                    except Exception as e:
                        self.app._log(f"[ERROR] Preload error ({wl}, {date_str}): {e}")

            if total_points > 0:
                self.refresh_laser_realtime_plot(wl)

    def refresh_laser_realtime_plot(self, wl="405nm"):
        """Plots telemetry metrics without clearing user view tracking interactions."""
        vars_dict = self.app.ui.laser_tabs_data.get(wl)
        history = self.plot_history.get(wl)
        if not vars_dict or not history or "ax_temp" not in vars_dict: return

        times = list(history["time"])
        if not times: return

        # Detect user interaction state (Zoom / Pan) using the Matplotlib navigation stack
        toolbar = vars_dict["canvas"].toolbar
        user_zoomed = False
        if toolbar and hasattr(toolbar, '_nav_stack'):
            depth = toolbar._nav_stack.depth() if hasattr(toolbar._nav_stack, 'depth') else len(getattr(toolbar._nav_stack, '_elements', []))
            if depth > 1:
                user_zoomed = True

        ax_temp = vars_dict["ax_temp"]
        ax_curr = vars_dict["ax_curr"]

        # Cache existing bounds before drawing
        old_xlim = ax_temp.get_xlim()
        old_ylim_temp = ax_temp.get_ylim()
        old_ylim_curr = ax_curr.get_ylim()

        step = max(1, len(times) // 1000)
        d_times, d_temp, d_pulse = times[::step], list(history["temp"])[::step], list(history["pulse"])[::step]
        bias_history = list(history.get("bias", []))
        if len(bias_history) < len(times):
            bias_history = [0.0] * (len(times) - len(bias_history)) + bias_history
        d_bias = bias_history[::step]
        # LD on/off state aligned with the time axis (older logs may have no ld_on column)
        ld_history = list(history.get("ld_on", []))
        if len(ld_history) < len(times):
            ld_history = [0] * (len(times) - len(ld_history)) + ld_history
        d_ld = ld_history[::step]

        def _shade_ld_on(ax):
            """Shade the time spans where the LD was ON, so on/off periods are obvious
            even though the hardware keeps reporting the last set Pulse/Bias while OFF."""
            start = None
            for i, on in enumerate(d_ld):
                if on and start is None:
                    start = d_times[i]
                elif not on and start is not None:
                    ax.axvspan(start, d_times[i], color='#2ecc71', alpha=0.12, linewidth=0)
                    start = None
            if start is not None:
                ax.axvspan(start, d_times[-1], color='#2ecc71', alpha=0.12, linewidth=0)

        ax_temp.clear()
        ax_temp.plot(d_times, d_temp, 'r-', linewidth=1)
        ax_temp.set_ylabel("Temp (°C)", color='r')
        ax_temp.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d %H:%M'))
        ax_temp.grid(True, alpha=0.3)
        _shade_ld_on(ax_temp)

        ax_curr.clear()
        # Pulse line split by LD state: bright green while ON, faint grey while OFF
        # (the hardware keeps reporting the last set Pulse value even when the LD is off).
        pulse_on  = [p if ld else float('nan') for p, ld in zip(d_pulse, d_ld)]
        pulse_off = [p if not ld else float('nan') for p, ld in zip(d_pulse, d_ld)]
        ax_curr.plot(d_times, pulse_off, color='#b0b0b0', linewidth=1.0, label='Pulse (LD off)')
        ax_curr.plot(d_times, pulse_on, color='#2e7d32', linewidth=1.6, label='Pulse (LD on)')
        ax_curr.plot(d_times, d_bias, color='purple', linestyle='-', linewidth=1, label='Bias')
        ax_curr.set_ylabel("Current (mA)", color='g')
        ax_curr.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d %H:%M'))
        ax_curr.grid(True, alpha=0.3)
        _shade_ld_on(ax_curr)
        # add an "LD ON" entry to the legend (green band)
        from matplotlib.patches import Patch
        handles, _ = ax_curr.get_legend_handles_labels()
        handles.append(Patch(facecolor='#2ecc71', alpha=0.25, label='LD ON'))
        ax_curr.legend(handles=handles, loc='upper left')

        # Restore limits strictly if zoom or pan is active
        if user_zoomed:
            ax_temp.set_xlim(old_xlim)
            ax_temp.set_ylim(old_ylim_temp)
            ax_curr.set_xlim(old_xlim)
            ax_curr.set_ylim(old_ylim_curr)

        vars_dict["fig"].autofmt_xdate(rotation=30)
        vars_dict["canvas"].draw()

    def update_laser_status_loop(self):
        """Core loop for status tracking with isolated pipeline redirects."""
        if self.laser_after_id:
            self.app.master.after_cancel(self.laser_after_id)
            self.laser_after_id = None

        interval = 1000
        current_time_floored = int(time.time())
        for idx, wl in enumerate(self.wavelengths):
            inst = self.laser_instances.get(wl)
            ui_vars = self.app.ui.laser_tabs_data.get(wl)
            if not inst or not ui_vars: continue

            if inst.is_connected():
                try:
                    status_ok = inst.update_status()
                    if status_ok:
                        if self.comm_error_flags[wl]:
                            self.comm_error_flags[wl] = False 
                            self.app.master.after(10, lambda w=wl, i=inst: self.show_interlock_recovery_dialog(w, i))
                            continue

                        status = inst.status
                        ld_on, tec_on = status.get('ld_on', False), status.get('tec_on', False)
                        temp, pulse = status.get('ld_temp', 0), status.get('pulse', 0)
                        actual_bias = status.get('bias', 0.0)      
                        
                        ld_mark = "●" if ld_on else "○"
                        tec_mark = "●" if tec_on else "○"
                        tab_text = f" {wl} [L:{ld_mark} T:{tec_mark}] "
                        self.app.ui.laser_sub_notebook.tab(idx, text=tab_text, image=self.app.ui.tab_led_green, compound=tk.RIGHT)

                        interlock_alarm = status.get('alarm', False) or status.get('interlock', False)
                        if interlock_alarm:
                            ui_vars["ld_status"].set("🔒 INTERLOCK")
                            if "ld_label_obj" in ui_vars: 
                                ui_vars["ld_label_obj"].config(foreground="#fd7e14")
                        else:
                            ui_vars["ld_status"].set("ON" if ld_on else "OFF")
                            self.app.ui.update_laser_status_colors(wl, ld_on, tec_on)

                        ui_vars["tec_status"].set("ON" if tec_on else "OFF")
                        ui_vars["temp"].set(f"{temp:.2f} °C")
                        ui_vars["pulse_live"].set(f"{pulse:.2f} mA")
                        ui_vars["bias_live"].set(f"{actual_bias:.2f} mA")

                        self.plot_history[wl]["temp"].append(temp)
                        self.plot_history[wl]["pulse"].append(pulse)
                        self.plot_history[wl]["bias"].append(actual_bias)
                        self.plot_history[wl]["time"].append(datetime.now())
                        self.plot_history[wl]["ld_on"].append(1 if ld_on else 0)

                        try:
                            current_tab_idx = self.app.ui.laser_sub_notebook.index(self.app.ui.laser_sub_notebook.select())
                            if idx == current_tab_idx:
                                self.refresh_laser_realtime_plot(wl)
                        except Exception as e:
                            self.app._log(f"[WARNING] Failed to update plot: {e}")
                            pass

                        # Downsample disk logging: write every 10s when active, every 60s otherwise.
                        now_t = time.time()
                        log_interval = 10 if (ld_on or tec_on) else 60
                        if now_t - self._last_log_time.get(wl, 0.0) >= log_interval:
                            self._last_log_time[wl] = now_t
                            self.save_laser_realtime_data(wl, temp, pulse, ld_on, tec_on)
                    else:
                        self._handle_comm_failure(wl, idx, "USB")

                except Exception as e:
                    self.app._log(f"[ERROR] {wl} Comm Error: {e}")
                    try:
                        inst.disconnect()
                    except Exception:
                        pass
                    self._handle_comm_failure(wl, idx, "USB")
            else:
                # Device is not connected — update UI before attempting reconnect
                if not self.comm_error_flags.get(wl, False):
                    self._disc_reason[wl] = "USB"
                    self._handle_comm_failure(wl, idx, "USB")
                if wl in self.expected_connections and wl not in self._reconnecting:
                    # USB (re)connection can block for a second or more; doing it inline
                    # froze the GUI on every 1s poll. Run it off the main thread and guard
                    # against overlapping attempts for the same wavelength.
                    self._reconnecting.add(wl)
                    self.app._log(f"[INFO] {wl} Attempting auto-reconnect...")

                    def reconnect_task(w=wl, i=inst):
                        try:
                            i.connect(dev_path=self.laser_port_mapping.get(w))
                        except Exception as e:
                            self.app.master.after(0, lambda m=str(e): self.app._log(
                                f"[WARNING] {w} reconnect failed: {m}"))
                        finally:
                            self._reconnecting.discard(w)

                    threading.Thread(target=reconnect_task, daemon=True).start()

        if hasattr(self.app, 'master') and self.app.master.winfo_exists():
            if self.laser_session_start and (time.time() - self.laser_session_start < 10):
                interval = 1000
            self.laser_after_id = self.app.master.after(interval, self.update_laser_status_loop)

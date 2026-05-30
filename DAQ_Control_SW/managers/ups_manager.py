# managers/ups_manager.py
import serial
import serial.tools.list_ports
import time
import collections
import os
import pandas as pd
from datetime import datetime, timedelta
import tkinter as tk
from tkinter import messagebox
import matplotlib.dates as mdates
import threading

class UPSManager:
    def __init__(self, app):
        self.app = app
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

    def search_ups_ports(self):
        ports = serial.tools.list_ports.comports()
        port_list = []
        for port in ports:
            display_name = f"{port.device}"
            port_list.append(display_name)

        if port_list:
            self.app.ui.ups_port_combo['values'] = port_list
            self.app.ui.ups_port_combo.current(0)
            self.app._log(f"Found {len(port_list)} serial ports.")

        if hasattr(self.app, 'ui'):
            self.app.ui.ups_conn_btn.config(state="normal")
            self.app.ui.ups_refresh_btn.config(state="normal")
            self.app._log("UPS Control buttons enabled.")

        if not port_list:
            self.app._log("No serial ports found. You can still type the port manually.")
        else:
            self.app._log("UPS ports updated.")

    def diagnose_ups(self):
        self.app._log("🔍 Starting UPS Connection Diagnosis...")
        ports = serial.tools.list_ports.comports()
        target_port = next((p.device for p in ports if p.vid == 0x067B and p.pid == 0x23A3), None)

        if not target_port:
            self.app._log("❌ Step 1 Fail: Prolific USB-Serial Hardware not found.")
            messagebox.showerror("Diagnosis", "UPS USB 장치를 찾을 수 없습니다. (lsusb 확인 요망)")
            return

        if self.ups_serial and self.ups_serial.is_open:
            try:
                self.ups_serial.write(b'Q1\r')
                time.sleep(0.5)
                if self.ups_serial.in_waiting > 0:
                    self.app._log(f"✅ Step 2 Pass: UPS Response received on {target_port}")
                    messagebox.showinfo("Diagnosis", f"정상 연결 중입니다.\n포트: {target_port}")
                else:
                    self.app._log("❌ Step 2 Fail: No response from UPS (Power?)")
                    messagebox.showwarning("Diagnosis", "포트는 인식되나 UPS 응답이 없습니다.")
            except Exception as e:
                self.app._log(f"❌ Error: {e}")
        else:
            self.app._log("ℹ️ Attempting Auto-connect...")
            self.auto_connect_ups()

    def auto_connect_ups(self):
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
            if hasattr(self.app, 'ui'):
                self.app.ui.ups_vars["conn_status"].set(f"Connecting to {target_port}...")
            self._try_ups_handshake(target_port)
        else:
            if hasattr(self.app, 'ui'):
                self.app.ui.ups_vars["conn_status"].set("UPS H/W Not Found")

    def _try_ups_handshake(self, port):
        def handshake_task():
            try:
                ser = serial.Serial(
                    port=port, baudrate=2400, parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE, bytesize=serial.EIGHTBITS, timeout=1.0
                )
                ser.dtr = True
                ser.rts = False
                time.sleep(2.0) # 백그라운드에서만 2초 대기 (GUI는 안 멈춤)

                ser.reset_input_buffer()
                ser.write(b'Q1\r')
                time.sleep(1.0)

                response = ser.read(ser.in_waiting or 1).decode('ascii', errors='ignore').strip()

                if response.startswith('(') or ser.is_open:
                    self.ups_serial = ser
                    if hasattr(self.app, 'ui'):
                        self.app.master.after(0, lambda: self.app.ui.ups_vars["conn_status"].set(f"Connected: {port}"))
                    self.app._log(f"UPS Handshake Success with Q1: {repr(response)}")
                    self.app.master.after(0, self.update_ups_status_loop)
                else:
                    ser.close()
                    if hasattr(self.app, 'ui'):
                        self.app.master.after(0, lambda: self.app.ui.ups_vars["conn_status"].set("No Response"))
            except Exception as e:
                self.app._log(f"UPS Handshake Error: {e}")

        # 메인 화면을 멈추지 않고 백그라운드 스레드에서 실행!
        threading.Thread(target=handshake_task, daemon=True).start()

    def update_ups_status_loop(self):
        """Core loop for UPS status without UI freezing."""
        if self.ups_after_id:
            self.app.master.after_cancel(self.ups_after_id)
            self.ups_after_id = None

        interval = 2000

        if self.ups_serial and self.ups_serial.is_open:
            if self.ups_session_start is None:
                self.ups_session_start = time.time()

            elapsed = time.time() - self.ups_session_start
            interval = 1000 if elapsed < 60 else 60000

            def fetch_ups_task():
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

                                # Safely update UI from the main thread
                                def update_ui():
                                    if output_v > 50:
                                        self.update_ups_outlet_status([1, 1, 0, 0])
                                    else:
                                        self.update_ups_outlet_status([0, 0, 0, 0])

                                    self.save_ups_realtime_data(current_watt, temp_c, input_v, output_v)

                                    if hasattr(self.app, 'ui'):
                                        self.app.ui.ups_vars["input_volt"].set(f"{input_v:.1f} V")
                                        self.app.ui.ups_vars["output_volt"].set(f"{output_v:.1f} V")
                                        self.app.ui.ups_vars["load_level"].set(int(load_p))
                                        self.app.ui.ups_vars["batt_level"].set(batt_pct)
                                        self.app.ui.ups_vars["frequency"].set(f"{freq:.1f} Hz")
                                        self.app.ui.ups_vars["status_msg"].set(f"Normal ({current_watt:.1f} W) / Temp: {temp_c:.1f}°C")
                                    
                                    now_dt = datetime.now()
                                    self.ups_plot_history["time"].append(now_dt)
                                    self.ups_plot_history["watt"].append(current_watt)
                                    self.ups_plot_history["temp"].append(temp_c)
                                    self.ups_plot_history["vin"].append(input_v)
                                    self.ups_plot_history["vout"].append(output_v)
                                    self.refresh_ups_plot()

                                self.app.master.after(0, update_ui)
                except Exception as e:
                    self.app.master.after(0, lambda: self.app._log(f"[ERROR] UPS Loop Error: {e}"))

            threading.Thread(target=fetch_ups_task, daemon=True).start()

        if hasattr(self.app, 'master') and self.app.master.winfo_exists():
            self.ups_after_id = self.app.master.after(interval, self.update_ups_status_loop)

    def manual_refresh_ups(self):
        if self.ups_serial and self.ups_serial.is_open:
            self.ups_session_start = time.time() 
            self.app._log("UPS manual refresh triggered (1s mode for 60s)")
            self.update_ups_status_loop()

    def toggle_ups_connection(self):
        if self.ups_serial and self.ups_serial.is_open:
            self.ups_serial.close()
            if hasattr(self.app, 'ui'):
                self.app.ui.ups_vars["conn_status"].set("Disconnected")
            self.app._log("UPS Serial Disconnected.")
        else:
            port = self.app.ui.ups_port_combo.get() if hasattr(self.app, 'ui') else None
            if not port:
                messagebox.showwarning("Warning", "Please select a port first.")
                return
            
            # [수정] 수동 연결도 스레드로 처리
            def manual_connect_task():
                try:
                    ser = serial.Serial(port, 2400, timeout=1)
                    ser.dtr = True
                    ser.rts = False
                    time.sleep(1.0)

                    ser.reset_input_buffer()
                    ser.write(b'Q1\r')
                    time.sleep(0.5)

                    self.ups_serial = ser
                    if hasattr(self.app, 'ui'):
                        self.app.master.after(0, lambda: self.app.ui.ups_vars["conn_status"].set(f"Connected to {port}"))

                    self.app._log(f"UPS Manual Connect Success: {port}")
                    self.app.master.after(0, self.update_ups_status_loop)

                except Exception as e:
                    self.ups_serial = None
                    error_msg = str(e) 
                    self.app.master.after(0, lambda msg=error_msg: messagebox.showerror("UPS Connection Error", f"Failed to connect: {msg}"))

            if hasattr(self.app, 'ui'):
                self.app.ui.ups_vars["conn_status"].set(f"Connecting to {port}...")
            threading.Thread(target=manual_connect_task, daemon=True).start()

    def refresh_ups_plot(self):
        try:
            if hasattr(self.app, 'ui') and hasattr(self.app.ui, 'main_notebook'):
                current_tab_id = self.app.ui.main_notebook.select()
                tab_text = self.app.ui.main_notebook.tab(current_tab_id, "text")
                if "UPS" not in tab_text:
                    return 
        except Exception:
            pass

        h = self.ups_plot_history
        times = list(h["time"])
        if not times: return

        # 1. Safely track user zoom/pan interaction state across the navigation stack
        toolbar = self.app.ui.ups_toolbar
        user_zoomed = False
        if toolbar and hasattr(toolbar, '_nav_stack'):
            depth = toolbar._nav_stack.depth() if hasattr(toolbar._nav_stack, 'depth') else len(getattr(toolbar._nav_stack, '_elements', []))
            if depth > 1:
                user_zoomed = True

        # 2. Cache old boundary configuration specs before clearing layouts
        old_limits = {}
        axes_list = [self.app.ui.ax_ups_watt, self.app.ui.ax_ups_temp, self.app.ui.ax_ups_vin, self.app.ui.ax_ups_vout]
        if user_zoomed:
            for ax in axes_list:
                old_limits[ax] = (ax.get_xlim(), ax.get_ylim())

        step = max(1, len(times) // 500)
        d_times = times[::step]

        plots = [
            (self.app.ui.ax_ups_watt, list(h["watt"])[::step], "Power (W)", "red"),
            (self.app.ui.ax_ups_temp, list(h["temp"])[::step], "Internal Temp (C)", "orange"),
            (self.app.ui.ax_ups_vin,  list(h["vin"])[::step],  "Input Voltage (V)", "blue"),
            (self.app.ui.ax_ups_vout, list(h["vout"])[::step], "Output Voltage (V)", "green")
        ]

        for ax, data, title, color in plots:
            ax.clear()
            ax.plot(d_times, data, color=color, linewidth=1.2)
            ax.set_title(title, fontsize=10, fontweight='bold')
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
            ax.xaxis.set_major_locator(mdates.AutoDateLocator())
            ax.grid(True, alpha=0.2)
            ax.tick_params(labelsize=8)

        # 3. Strictly restore cached boundary limits to prevent viewport jump back
        if user_zoomed:
            for ax in axes_list:
                if ax in old_limits:
                    ax.set_xlim(old_limits[ax][0])
                    ax.set_ylim(old_limits[ax][1])

        self.app.ui.fig_ups.autofmt_xdate()
        self.app.ui.canvas_ups.draw()

    def update_ups_outlet_status(self, states):
        colors = {0: "#adb5bd", 1: "#28a745", 2: "#dc3545"}
        for i, state in enumerate(states):
            self.app.ui.outlet_canvas.itemconfig(self.app.ui.outlet_circles[i], fill=colors[state])

    def shutdown_ups_all(self):
        confirmed = messagebox.askyesno("WARNING", "Are you sure you want to SHUT DOWN all outputs?")
        if confirmed:
            if self.ups_serial and self.ups_serial.is_open:
                try:
                    self.app._log("!!! UPS SHUTDOWN COMMAND SENT !!!")
                    self.update_ups_outlet_status([0, 0, 0, 0])
                    messagebox.showinfo("Shutdown", "Shutdown command sent successfully.")
                except Exception as e:
                    self.app._log(f"Shutdown Failed: {e}")
                    self.update_ups_outlet_status([2, 2, 2, 2])
                    messagebox.showerror("Error", f"Failed to send shutdown command: {e}")
            else:
                messagebox.showwarning("Connection Error", "UPS is not connected via RS232C.")

    def save_ups_realtime_data(self, watt, temp, vin, vout):
        if vout <= 0.5:
            return
        log_dir = os.path.join(self.app.base_dir, "LOG", "UPS")
        os.makedirs(log_dir, exist_ok=True)

        today_str = datetime.now().strftime('%Y%m%d')
        file_path = os.path.join(log_dir, f"ups_{today_str}.csv")
        file_exists = os.path.isfile(file_path)

        try:
            with open(file_path, "a") as f:
                if not file_exists:
                    f.write("timestamp,Watt,Temp,Vin,Vout\n")
                now_iso = datetime.now().isoformat()
                f.write(f"{now_iso},{watt:.1f},{temp:.1f},{vin:.1f},{vout:.1f}\n")
        except Exception as e:
            self.app._log(f"Failed to save UPS log: {e}")

    def preload_ups_history(self):
        now = datetime.now()
        dates_to_load = [
            (now - timedelta(days=1)).strftime('%Y%m%d'),
            now.strftime('%Y%m%d')
        ]

        total_pts = 0
        for date_str in dates_to_load:
            log_file = os.path.join(self.app.base_dir, "LOG", "UPS", f"ups_{date_str}.csv")
            if os.path.exists(log_file):
                try:
                    df = pd.read_csv(log_file)
                    for _, row in df.iterrows():
                        try:
                            try:
                                ts = datetime.fromisoformat(row['timestamp'])
                            except:
                                ts_time = datetime.strptime(row['timestamp'], '%H:%M:%S').time()
                                ts = datetime.combine(datetime.strptime(date_str, '%Y%m%d'), ts_time)

                            if now - ts <= timedelta(hours=24):
                                self.ups_plot_history["time"].append(ts)
                                self.ups_plot_history["watt"].append(float(row['Watt']))
                                self.ups_plot_history["temp"].append(float(row['Temp']))
                                self.ups_plot_history["vin"].append(float(row['Vin']))
                                self.ups_plot_history["vout"].append(float(row['Vout']))
                                total_pts += 1
                        except:
                            continue
                except Exception as e:
                    self.app._log(f"Error preloading UPS log {date_str}: {e}")

        if total_pts > 0:
            self.app._log(f"UPS 24h history recovered ({total_pts} points).")
            self.refresh_ups_plot()

    def handle_ups_shutdown(self):
        target = self.app.ui.shutdown_target_var.get()
        confirm = messagebox.askyesno("Confirm Shutdown", f"Are you sure you want to SHUTDOWN [{target}]?\nConnected device power will be cut.")
        if not confirm: return

        if target == "All Outlets":
            self.shutdown_ups_all()
        else:
            try:
                idx_map = {"Outlet 1 (DAQ)": 0, "Outlet 2 (Laser)": 1, "Outlet 3": 2, "Outlet 4": 3}
                self.shutdown_ups_each(idx_map[target])
            except KeyError:
                self.app._log("Shutdown target mapping error.")

    def shutdown_ups_each(self, index):
        if self.ups_serial and self.ups_serial.is_open:
            try:
                self.app._log(f"UPS Individual Shutdown Command Sent: Outlet {index+1}")
                current_states = [1, 1, 0, 0] 
                current_states[index] = 2
                self.update_ups_outlet_status(current_states)
            except Exception as e:
                messagebox.showerror("Error", f"Failed to shut down individual outlet: {e}")
        else:
            messagebox.showwarning("Connection Error", "UPS Serial is not connected.")

    def check_ups_alerts(self, watt, temp, batt, load, vin):
        status_msg = "Normal"
        alert_color = "blue"
        is_critical = False

        if vin < 10:
            status_msg = "🚨 POWER FAILURE! BATTERY MODE"
            alert_color = "#fd1414"
            is_critical = True
        elif batt < 30:
            status_msg = f"⚠️ LOW BATTERY ({batt}%)"
            alert_color = "#fd7e14"
            if batt < 15: is_critical = True
        elif load > 85:
            status_msg = f"🚨 UPS OVERLOAD! ({load}%)"
            alert_color = "#e214fd"
            is_critical = True
        elif temp > 45:
            status_msg = f"⚠️ UPS OVERHEAT ({temp}°C)"
            alert_color = "#edfd14"

        self.app.ui.ups_vars["status_msg"].set(f"{status_msg} ({watt:.1f} W) / Temp: {temp:.1f}°C")
        for lbl in self.app.ui.ups_value_labels:
            lbl.config(foreground=alert_color)

        if is_critical and not hasattr(self, '_ups_alert_active'):
            self._ups_alert_active = True
            emergency_info = "\n\n[Emergency Contacts]\nLab: 0578-86-9250\nJunkyo: +82-10-6503-2581"
            messagebox.showwarning("UPS CRITICAL ALERT", f"Critical Condition Detected:\n{status_msg}{emergency_info}")
        elif not is_critical:
            if hasattr(self, '_ups_alert_active'):
                del self._ups_alert_active

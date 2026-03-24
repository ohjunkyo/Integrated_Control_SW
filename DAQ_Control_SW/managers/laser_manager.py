# managers/laser_manager.py
import time
import os
import collections
from datetime import datetime, timedelta
from tkinter import messagebox
import matplotlib.dates as mdates
import pandas as pd
import logging
from tkinter import messagebox, filedialog  
from logging.handlers import TimedRotatingFileHandler
import tkinter as tk
import tkinter.simpledialog as sd # 파일 최상단에 없으면 추가하세요.

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
        self.expected_connections = set() 

        self.plot_history = {}
        for wl in self.wavelengths:
            self.plot_history[wl] = {
                "time": collections.deque(maxlen=90000), 
                "temp": collections.deque(maxlen=90000), 
                "pulse": collections.deque(maxlen=90000)
            }
            self.load_todays_log(wl)

        self.laser_session_start = None
        self.laser_after_id = None

    def auto_connect_laser(self):
        last_wls = getattr(self.app, "last_connected_wls", [])
        if not last_wls: return

        msg = f"The following lasers were connected last time:\n[{', '.join(last_wls)}]\n\nDo you want to restore these connections?"
        if not messagebox.askyesno("Laser Auto-Connect", msg):
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
        """인터락 복구 시 팝업창을 띄우는 커스텀 다이얼로그"""
        dialog = tk.Toplevel(self.app.master)
        dialog.title(f"Interlock Recovery - {wl}")
        dialog.geometry("380x150")
        dialog.attributes("-topmost", True)
        dialog.grab_set() # 팝업이 떠 있는 동안 메인 창 클릭 방지

        tk.Label(dialog, text=f"[{wl}] 인터락 해제가 감지되었습니다.\n다시 연결하시겠습니까?", font=("Arial", 10, "bold"), pady=15).pack()

        def on_normal_connect():
            dialog.destroy()
            self._process_post_reconnect(wl, inst, is_admin=False)

        def on_admin_connect():
            pwd = sd.askstring("Admin", "Admin 비밀번호를 입력하세요:", show='*', parent=dialog)
            if pwd == "1234": # 💡 원하는 Admin 비밀번호로 변경하세요
                self.app._log(f"🔑 [ {wl} ] Admin 권한 승인됨.")
                dialog.destroy()
                self._process_post_reconnect(wl, inst, is_admin=True)
            elif pwd is not None:
                messagebox.showerror("Error", "비밀번호가 틀렸습니다.", parent=dialog)

        def on_cancel():
            self.app._log(f"🚫 [ {wl} ] 사용자가 연결을 취소했습니다.")
            inst.disconnect()
            self._handle_comm_failure(wl, self.wavelengths.index(wl))
            dialog.destroy()

        btn_frame = tk.Frame(dialog)
        btn_frame.pack(pady=10)

        tk.Button(btn_frame, text="일반 연결", width=10, command=on_normal_connect).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="강제 연결 (Admin)", width=15, command=on_admin_connect, fg="red").pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="취소", width=10, command=on_cancel).pack(side=tk.LEFT, padx=5)

    def _process_post_reconnect(self, wl, inst, is_admin=False):
        """팝업창 선택 이후의 레이저 상태 처리 로직"""
        inst.update_status()
        is_ld_on = inst.status.get('ld_on', False)

        if is_ld_on:
            if is_admin:
                # 관리자: Restore 기능과 동일하게 끄거나 켤지 물어봄
                off_msg = f"⚠️ [ {wl} ] 하드웨어 LD가 현재 켜져있습니다(ON)!\n\n안전을 위해 레이저를 끄시겠습니까?\n(아니오 클릭 시 켜진 상태 유지)"
                if messagebox.askyesno("LD Status Alert (Admin)", off_msg):
                    inst.set_ld_on(False)
                    inst.set_tec_on(False)
                    self.app._log(f"🛡️ Safety: [ {wl} ] 관리자 권한으로 레이저 OFF 조치됨.")
                else:
                    self.app._log(f"⚠️ Warning: [ {wl} ] 관리자가 레이저 ON 유지를 선택했습니다.")
            else:
                # 일반 사용자: 묻지 않고 무조건 안전하게 강제 OFF
                inst.set_ld_on(False)
                inst.set_tec_on(False)
                self.app._log(f"🛡️ Safety: [ {wl} ] 일반 연결이므로 안전을 위해 강제 OFF 되었습니다.")
                messagebox.showinfo("Safety Action", "일반 권한으로 연결되어 안전을 위해 레이저가 강제 종료되었습니다.")
        
        time.sleep(0.1)
        inst.update_status()
        self.comm_error_flags[wl] = False # 정상 루프로 복귀

    def manual_refresh_laser(self, wl=None):
        self.laser_session_start = time.time()
        if wl:
            self.app._log(f"Refreshing {wl}...")
        else:
            self.app._log("Refreshing lasers...")
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
                    if self.app.ui.laser_tabs_data[wl]["ld_status"].get() == "ON":
                        active_lasers.append(wl)

            # 다른 켜진 레이저가 발견되면 경고창 띄움
            if active_lasers:
                msg = f"Laser {', '.join(active_lasers)} is currently ON.\n\n" \
                        f"To turn on {target_wl}, the others must be turned OFF.\n" \
                        f"Proceed?"

                if not messagebox.askyesno("Safety Interlock", msg):
                    self.app._log(f"Operation cancelled: {target_wl} ON blocked by user.")
                    return # 사용자가 '아니오' 누르면 함수 종료 (안 켬)

                # 사용자가 '예' 누르면 -> 다른 레이저들 끄기
                for wl in active_lasers:
                    inst = self.laser_instances.get(wl)
                    if inst:
                        inst.set_ld_on(False) # 하드웨어 OFF
                        self.app.ui.laser_tabs_data[wl]["ld_status"].set("OFF") # UI OFF
                        self.app.ui.update_laser_status_colors(wl, False, False) # 빨간색
                        self.app._log(f"Safety: Auto-shutdown {wl}")

        # 2. 타겟 레이저 제어 (이제 안전함)
        inst = self.laser_instances.get(target_wl)
        if inst and inst.is_connected():
            inst.set_ld_on(state)
            self.app._log(f"Command Sent: Laser {target_wl} LD -> {'ON' if state else 'OFF'}")

            # 빠른 확인을 위해 0.2초 후 루프 실행
            self.laser_session_start = time.time()
            if self.laser_after_id:
                self.app.master.after_cancel(self.laser_after_id)
            self.app.master.after(200, self.update_laser_status_loop)


    def apply_laser_frequency_multi(self, wl):
        """특정 파장 기기에 트리거 모드 및 주파수 적용"""
        inst = self.laser_instances.get(wl)
        vars_dict = self.app.ui.laser_tabs_data.get(wl)

        if inst and inst.is_connected() and vars_dict:
            try:
                hz = int(vars_dict["freq_hz"].get())
                mode = vars_dict["trigger_mode"].get()

                pg1, pg2, ext = (mode=="Internal (PG1)"), (mode=="Internal (PG2)"), (mode=="External")
                inst.set_trigger_mode(pg1, pg2, ext) 
                
                time.sleep(0.1) 

                if pg1: inst.set_pg1_frequency(hz)
                elif pg2: inst.set_pg2_frequency(hz)
                
                time.sleep(0.1) 

                if "current_mode_disp" in vars_dict:
                    vars_dict["current_mode_disp"].set(f"Current: {mode}")

                self.app._log(f"✅ Laser {wl} Config: {mode}, {hz} Hz applied.")
            except ValueError:
                messagebox.showerror("Error", f"Invalid frequency for {wl}. Must be integer.")


    def set_laser_tec_multi(self, wl, state):
        """TEC 제어: 하드웨어 명령 후 즉시 상태를 재확인합니다."""
        inst = self.laser_instances.get(wl)
        if inst and inst.is_connected():
            inst.set_tec_on(state)
            self.app._log(f"Command Sent: Laser {wl} TEC -> {'ON' if state else 'OFF'}")

            self.laser_session_start = time.time()

            if self.laser_after_id:
                self.app.master.after_cancel(self.laser_after_id)

            self.app.master.after(500, self.update_laser_status_loop)

    def apply_laser_currents_multi(self, wl):
        """특정 파장 탭의 Bias/Pulse 전류 설정을 기기에 적용합니다."""
        inst = self.laser_instances.get(wl)
        vars_dict = self.app.ui.laser_tabs_data.get(wl)

        if inst and inst.is_connected() and vars_dict:
            try:
                bias = vars_dict["bias_set"].get()
                pulse = vars_dict["pulse_set"].get()

                inst.set_bias_current(bias)
                time.sleep(0.2) 
                inst.set_pulse_current(pulse)
                time.sleep(0.2) 
                self.app._log(f"✅ Applied to {wl}: Bias={bias:.2f}mA, Pulse={pulse:.2f}mA")

                if self.laser_after_id:
                    self.app.master.after_cancel(self.laser_after_id)
                self.app.master.after(100, self.update_laser_status_loop)
            except Exception as e:
                self.app._log(f"❌ Error applying currents to {wl}: {e}")

    def update_laser_status_loop(self):
        """Core loop for status, interlock sync, auto-recovery, and Tab indicators"""
        if self.laser_after_id:
            self.app.master.after_cancel(self.laser_after_id)
            self.laser_after_id = None

        interval = 60000 
        for idx, wl in enumerate(self.wavelengths):
            inst = self.laser_instances.get(wl)
            ui_vars = self.app.ui.laser_tabs_data.get(wl)
            if not inst or not ui_vars: continue

            if inst.is_connected():
                try:
                    status_ok = inst.update_status()

                    if status_ok:
                        
                        # ==========================================
                        if self.comm_error_flags[wl]:
                            self.comm_error_flags[wl] = False 
                            
                            self.app.master.after(10, lambda w=wl, i=inst: self.show_interlock_recovery_dialog(w, i))
                            continue
                        # ==========================================

                        status = inst.status
                        ld_on, tec_on = status.get('ld_on', False), status.get('tec_on', False)

                        temp, pulse = status.get('ld_temp', 0), status.get('pulse', 0)
                        
                        ld_mark = "●" if ld_on else "○"
                        tec_mark = "●" if tec_on else "○"
                        tab_text = f" {wl} [L:{ld_mark} T:{tec_mark}] "
                        self.app.ui.laser_sub_notebook.tab(idx, text=tab_text, image=self.app.ui.tab_led_green, compound=tk.RIGHT)

                        interlock_alarm = status.get('alarm', False) or status.get('interlock', False)
                        
                        if interlock_alarm:
                            ui_vars["ld_status"].set("🔒 INTERLOCK")
                            if "ld_label_obj" in ui_vars: 
                                ui_vars["ld_label_obj"].config(foreground="#fd7e14") # 주황색 경고
                        else:
                            ui_vars["ld_status"].set("ON" if ld_on else "OFF")
                            self.app.ui.update_laser_status_colors(wl, ld_on, tec_on)

                        #ui_vars["ld_status"].set("ON" if ld_on else "OFF")
                        ui_vars["tec_status"].set("ON" if tec_on else "OFF")
                        ui_vars["temp"].set(f"{temp:.2f} °C")
                        ui_vars["pulse_live"].set(f"{pulse:.2f} mA")
                        #self.app.ui.update_laser_status_colors(wl, ld_on, tec_on)

                        # Data recording
                        self.plot_history[wl]["temp"].append(temp)
                        self.plot_history[wl]["pulse"].append(pulse)
                        self.plot_history[wl]["time"].append(datetime.now())
                        self.refresh_laser_realtime_plot(wl)
                        self.save_laser_realtime_data(wl, temp, pulse)
                    else:
                        self._handle_comm_failure(wl, idx)
                except (OSError, serial.SerialException) as e:
                    self.app._log(f"🚨 {wl} Comm Error: {e}")
                    inst.disconnect()
                    self._handle_comm_failure(wl, idx)
            else:
                if wl in self.expected_connections:
                    self.app._log(f"🔄 {wl} Attempting auto-reconnect...")
                    inst.connect(dev_path=self.laser_port_mapping.get(wl))

        if hasattr(self.app, 'master') and self.app.master.winfo_exists():
            if self.laser_session_start and (time.time() - self.laser_session_start < 10):
                interval = 1000
            self.laser_after_id = self.app.master.after(interval, self.update_laser_status_loop)

    def _handle_comm_failure(self, wl, idx):
        """Handle UI when communication is lost"""
        inst = self.laser_instances.get(wl)
        if inst:
            inst.set_ld_on(False)
            inst.set_tec_on(False)
        ui_vars = self.app.ui.laser_tabs_data.get(wl)
        if not self.comm_error_flags[wl]:
            self.app._log(f"🚨 [ {wl} ] Connection lost. Check hardware/interlock.")
            self.comm_error_flags[wl] = True
        
        # 에러 발생 시 탭 제목에서 동그라미 제거 (깔끔하게 파장만 표시)
        self.app.ui.laser_sub_notebook.tab(idx, text=f" {wl} ", image=self.app.ui.tab_led_red, compound=tk.RIGHT)
        
        ui_vars["ld_status"].set("INTERLOCK / ERR")
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

    def refresh_laser_realtime_plot(self, wl="405nm"):
        """파장별 독립 히스토리 데이터를 사용하여 그래프를 그립니다."""
        vars_dict = self.app.ui.laser_tabs_data.get(wl)
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
        os.makedirs(self.laser_log_dir, exist_ok=True)

        self.laser_logger = logging.getLogger('LaserSession')
        self.laser_logger.setLevel(logging.INFO)

        if not self.laser_logger.handlers:
            log_path = os.path.join(self.laser_log_dir, "laser_log")
            handler = TimedRotatingFileHandler(log_path, when='midnight', interval=1)
            handler.suffix = "_%Y-%m-%d.txt"
            handler.setFormatter(logging.Formatter('%(asctime)s | %(message)s'))
            self.laser_logger.addHandler(handler)

    def load_today_laser_log(self):
        """프로그램 시작 시 오늘 작성된 텍스트 로그가 있다면 불러와서 UI에 표시"""
        today_str = datetime.now().strftime('%Y-%m-%d')
        log_file = os.path.join(self.laser_log_dir, "laser_log_" + today_str + ".txt")

        if os.path.exists(log_file):
            try:
                with open(log_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                    self.app.ui.laser_log_text.config(state="normal")
                    self.app.ui.laser_log_text.insert(tk.END, content)
                    self.app.ui.laser_log_text.config(state="disabled")
                    self.app.ui.laser_log_text.yview(tk.END)
            except Exception as e:
                print(f"Failed to load today's laser log: {e}")


    def preload_laser_history(self):
        """Restore history from self.laser_log_dir for the last 24h"""
        import pandas as pd
        from datetime import timedelta
        now = datetime.now()
        start_point = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        dates_to_check = [(now - timedelta(days=1)).strftime('%Y%m%d'), now.strftime('%Y%m%d')]

        for wl in self.wavelengths:
            self.plot_history[wl]["time"].clear()
            self.plot_history[wl]["temp"].clear()
            self.plot_history[wl]["pulse"].clear()
            total_points = 0

            for date_str in dates_to_check:
                # [FIX] Use dynamic log directory instead of hardcoded path
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
                                    total_points += 1
                            except: continue
                    except Exception as e:
                        self.app._log(f"Preload error ({wl}, {date_str}): {e}")

            if total_points > 0:
                self.refresh_laser_realtime_plot(wl)

    def _log_laser(self, msg):
        """레이저 전용 로그 위젯과 파일에 동시에 기록"""
        # 1. 파일에 기록
        if hasattr(self, 'laser_logger'):
            self.laser_logger.info(msg)

        # 2. UI 텍스트 위젯에 기록
        timestamp = datetime.now().strftime('%H:%M:%S')
        if hasattr(self.app.ui, 'laser_log_text'):
            self.app.ui.laser_log_text.config(state="normal")
            self.app.ui.laser_log_text.insert(tk.END, f"[{timestamp}] {msg}\n")
            self.app.ui.laser_log_text.config(state="disabled")
            self.app.ui.laser_log_text.yview(tk.END)

    def load_todays_log(self, wl):
        """Loads today's CSV file to restore the plot history when the GUI starts."""
        try:
            log_dir = getattr(self.app, 'laser_log_dir', self.laser_log_dir)
            today_str = datetime.now().strftime('%Y%m%d')
            file_path = os.path.join(log_dir, f"laser_data_{wl}_{today_str}.csv")

            if os.path.exists(file_path):
                # pandas를 이용해 기존 파일 읽기
                df = pd.read_csv(file_path)

                # 메모리 과부하를 막기 위해 최근 90000개의 데이터만 가져옴
                for _, row in df.tail(90000).iterrows():
                    try:
                        # CSV의 timestamp 형식을 datetime으로 변환 (예: 2026-03-24 15:30:00)
                        dt = datetime.strptime(str(row['timestamp']), '%Y-%m-%d %H:%M:%S')
                        t_num = mdates.date2num(dt)

                        self.plot_history[wl]["time"].append(t_num)
                        self.plot_history[wl]["temp"].append(float(row['temp']))
                        self.plot_history[wl]["pulse"].append(float(row['pulse']))
                    except ValueError:
                        pass # 헤더나 형식이 맞지 않는 줄은 무시

                if hasattr(self.app, '_log'):
                    self.app._log(f"[INFO] Loaded previous log data for {wl}.")
        except Exception as e:
            if hasattr(self.app, '_log'):
                self.app._log(f"[WARNING] Could not load past logs for {wl}: {e}")

    def save_laser_realtime_data(self, wl, temp, pulse):
        """[보완] 경로 강제 확인 및 예외 처리 강화"""
        try:
            # 1. 경로 재확인 및 생성
            log_dir = getattr(self.app, 'laser_log_dir', self.laser_log_dir)
            if not os.path.exists(log_dir):
                os.makedirs(log_dir, exist_ok=True)

            today_str = datetime.now().strftime('%Y%m%d')
            file_path = os.path.join(log_dir, f"laser_data_{wl}_{today_str}.csv")
            file_exists = os.path.isfile(file_path)

            # 2. 메타데이터 수집 (UI에서 안전하게 가져오기)
            mode, freq, bias = "Unknown", "0", 0.0
            if hasattr(self.app, 'ui') and hasattr(self.app.ui, 'laser_tabs_data'):
                vars_dict = self.app.ui.laser_tabs_data.get(wl)
                if vars_dict:
                    mode = vars_dict["trigger_mode"].get()
                    freq = vars_dict["freq_hz"].get()
                    bias = vars_dict["bias_set"].get()

            # 3. 파일 쓰기 (버퍼링 없이 즉시 쓰기)
            with open(file_path, "a", buffering=1) as f:
                if not file_exists:
                    f.write("timestamp,temp_c,pulse_ma,bias_ma,trigger_mode,freq_hz\n")
                now_iso = datetime.now().isoformat()
                f.write(f"{now_iso},{temp:.2f},{pulse:.2f},{float(bias):.2f},{mode},{freq}\n")
            
        except Exception as e:
            # 에러 발생 시 메인 로그에 출력하여 추적 가능하게 함
            self.app._log(f"⚠️ Laser Logging Error ({wl}): {e}")

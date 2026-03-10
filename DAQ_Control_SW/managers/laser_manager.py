# managers/laser_manager.py
import time
import os
import collections
from datetime import datetime, timedelta
from tkinter import messagebox
import matplotlib.dates as mdates
import pandas as pd
import logging
from logging.handlers import TimedRotatingFileHandler
import tkinter as tk

class LaserManager:
    def __init__(self, app):
        self.app = app  # main.py의 App 인스턴스를 참조합니다.

        self.wavelengths = ["375nm", "405nm", "450nm", "473nm"]
        self.laser_port_mapping = {
            wl: path.encode('utf-8') for wl, path in self.app.laser_port_mapping.items()
        }
        self.laser_log_dir = self.app.laser_log_dir
        self.laser_instances = {}

        self.plot_history = {}
        for wl in self.wavelengths:
            self.plot_history[wl] = {
                    "time": collections.deque(maxlen=90000), 
                    "temp": collections.deque(maxlen=90000), 
                    "pulse": collections.deque(maxlen=90000)
                    }

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
                # [연결 1] 통신을 먼저 맺습니다.
                self.connect_single_laser(wl)

                inst = self.laser_instances.get(wl)
                # [연결 2] 하드웨어의 현재 실제 상태를 읽어옵니다.
                if inst and inst.is_connected() and inst.update_status():
                    # [핵심] 만약 하드웨어상에서 LD가 이미 켜져(ON) 있다면?
                    if inst.status.get('ld_on', False):
                            # 사용자에게 끌지 말지 선택권을 줍니다.
                            off_msg = f"⚠️ [ {wl} ] Laser LD is currently ON.\n\nDo you want to turn it OFF now?"
                            if messagebox.askyesno("LD Status Alert", off_msg):
                                # 1. 시리얼 충돌을 막기 위해 백그라운드 루프를 잠시 멈춥니다.
                                if self.laser_after_id:
                                    self.app.master.after_cancel(self.laser_after_id)
                                    self.laser_after_id = None
                                
                                # 2. 확실하게 끄기 명령을 보내고, 장비가 처리할 시간(0.5초)을 줍니다.
                                inst.set_ld_on(False)
                                time.sleep(0.5) 
                                inst.update_status() # 꺼진 상태 확실히 읽어오기
                                
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

        self.app._log(f"Connecting to {wl} via Path {target_path}...")

        success, msg = inst.connect(dev_path=target_path)

        if success:
            vars_dict["conn_status_txt"].set("Connected")
            vars_dict["conn_label_obj"].config(foreground="#28a745")

            if inst.update_status():
                is_ld = inst.status.get('ld_on', False)
                vars_dict["ld_status"].set("ON" if is_ld else "OFF")

            # 빠른 업데이트 모드로 전환 (10초간 1초 주기로 체크)
            self.laser_session_start = time.time()
            self.update_laser_status_loop()

            self.app._log(f"✅ {wl} Connected on Path {target_path}")
            self.app.save_app_config() # 연결 리스트 저장

        else:
            vars_dict["conn_status_txt"].set("Disconnected")
            vars_dict["conn_label_obj"].config(foreground="red")
            self.app._log(f"❌ {wl} Failed: {msg}")

            messagebox.showerror("Connection Error",
                                 f"Failed to connect to {wl} laser.\n\n"
                                 f"Path: {target_path}\n"
                                 f"Reason: {msg}")


    def disconnect_single_laser(self, wl):
        """[수정] 개별 탭의 Disconnect 버튼 동작 (강제 초기화 포함)"""
        inst = self.laser_instances.get(wl)
        vars_dict = self.app.ui.laser_tabs_data.get(wl)

        if not vars_dict: return

        try:
            if inst: inst.disconnect()
        except Exception as e:
            self.app._log(f"Warning during disconnect {wl}: {e}")

        vars_dict["conn_status_txt"].set("Disconnected")
        vars_dict["conn_label_obj"].config(foreground="red")

        vars_dict["ld_status"].set("Disconnected")
        vars_dict["tec_status"].set("OFF")
        vars_dict["temp"].set("--.- °C")

        idx = self.wavelengths.index(wl)
        self.app.ui.laser_sub_notebook.tab(idx, image=self.app.ui.tab_led_red, compound=tk.RIGHT)

        self.app.save_app_config()
        self.app._log(f"🔌 {wl} Disconnected (User Request).")

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
                inst.set_trigger_mode(pg1, pg2, ext) #

                if pg1: inst.set_pg1_frequency(hz)
                elif pg2: inst.set_pg2_frequency(hz)

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
                inst.set_pulse_current(pulse)
                self.app._log(f"✅ Applied to {wl}: Bias={bias:.2f}mA, Pulse={pulse:.2f}mA")

                if self.laser_after_id:
                    self.app.master.after_cancel(self.laser_after_id)
                self.app.master.after(100, self.update_laser_status_loop)
            except Exception as e:
                self.app._log(f"❌ Error applying currents to {wl}: {e}")

    def update_laser_status_loop(self):
        """탭 헤더에 [연결, LD, TEC] 상태를 통합 표시하고 데이터를 중복 없이 저장합니다."""
        if self.laser_after_id:
            self.app.master.after_cancel(self.laser_after_id)
            self.laser_after_id = None

        interval = 60000 # 평상시 60초

        for idx, wl in enumerate(self.wavelengths):
            inst = self.laser_instances.get(wl)
            ui_vars = self.app.ui.laser_tabs_data.get(wl)
            if not inst or not ui_vars: continue

            if inst.is_connected():
                # [정보 1] 연결됨 -> 탭 아이콘 녹색
                self.app.ui.laser_sub_notebook.tab(idx, image=self.app.ui.tab_led_green, compound=tk.RIGHT)

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
                    self.app.ui.laser_sub_notebook.tab(idx, text=new_title)

                    # UI 텍스트 및 색상 업데이트
                    ui_vars["ld_status"].set("ON" if is_ld_on else "OFF")
                    ui_vars["tec_status"].set("ON" if is_tec_on else "OFF")
                    ui_vars["temp"].set(f"{temp_val:.2f} °C")
                    ui_vars["pulse_live"].set(f"{pulse_val:.2f} mA")
                    self.app.ui.update_laser_status_colors(wl, is_ld_on, is_tec_on)

                    # 1. 파장별 독립 메모리에 데이터 축적 (한 번만 수행)
                    self.plot_history[wl]["temp"].append(temp_val)
                    self.plot_history[wl]["pulse"].append(pulse_val)
                    self.plot_history[wl]["time"].append(now_ts)

                    # 2. 실시간 그래프 갱신 및 파일 저장
                    self.refresh_laser_realtime_plot(wl)
                    self.save_laser_realtime_data(wl, temp_val, pulse_val)
            else:
                # 연결 안 됨 처리
                self.app.ui.laser_sub_notebook.tab(idx, image=self.app.ui.tab_led_red, compound=tk.RIGHT)
                self.app.ui.laser_sub_notebook.tab(idx, text=f" {wl} ")
                if wl != "405nm":
                    ui_vars["ld_status"].set("Disconnected")

        # 가변 주기 제어 로직 (기존 유지)
        if self.laser_instances:
            if self.laser_session_start is None: self.laser_session_start = time.time()
            elapsed = time.time() - self.laser_session_start
            interval = 1000 if elapsed < 10 else 60000

            for w in self.wavelengths:
                if w in self.app.ui.laser_tabs_data:
                    self.app.ui.laser_tabs_data[w]["check_interval"].set(f"{interval/1000:.0f}s")

        if hasattr(self, 'master') and self.app.master.winfo_exists():
            self.laser_after_id = self.app.master.after(interval, self.update_laser_status_loop)

    def on_laser_trigger_change_multi(self, wl):
        """특정 파장 탭의 트리거 모드에 따라 입력창 활성/비활성 제어"""
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
            else:
                if entry: entry.config(state="normal")
            if btn: btn.config(state="normal")
            if frame: frame.config(text=f"Trigger Control - ENABLED (Internal) [{wl}]")

        # 실제 하드웨어에 설정 적용
        inst = self.laser_instances.get(wl)
        if inst and inst.is_connected():
            self.apply_laser_frequency_multi(wl)

    def on_laser_trigger_change(self, event=None):
        for wl in self.wavelengths:
            self.on_laser_trigger_change_multi(wl)

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

    def save_laser_realtime_data(self, wl, temp, pulse):
        """[수정] 각 파장의 데이터를 날짜별 CSV에 기록 (실험 메타데이터 포함)"""
        log_dir = self.laser_log_dir
        os.makedirs(log_dir, exist_ok=True)

        today_str = datetime.now().strftime('%Y%m%d')
        file_path = os.path.join(log_dir, f"laser_data_{wl}_{today_str}.csv")
        file_exists = os.path.isfile(file_path)

        # [추가] UI에서 현재 설정된 실험 메타데이터(모드, 주파수, 바이어스)를 가져옵니다.
        vars_dict = self.app.ui.laser_tabs_data.get(wl)
        if vars_dict:
            mode = vars_dict["trigger_mode"].get()
            freq = vars_dict["freq_hz"].get()
            bias = vars_dict["bias_set"].get()
        else:
            mode, freq, bias = "Unknown", "0", 0.0

        try:
            with open(file_path, "a") as f:
                if not file_exists:
                    # 헤더에 실험 조건 컬럼을 추가합니다.
                    f.write("timestamp,temp_c,pulse_ma,bias_ma,trigger_mode,freq_hz\n")
                
                now_iso = datetime.now().isoformat()
                # 데이터와 함께 메타데이터를 한 줄로 예쁘게 저장합니다.
                f.write(f"{now_iso},{temp:.2f},{pulse:.2f},{float(bias):.2f},{mode},{freq}\n")
        except Exception as e:
            self.app._log(f"Error saving laser log for {wl}: {e}")


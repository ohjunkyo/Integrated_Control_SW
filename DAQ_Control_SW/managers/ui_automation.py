# managers/ui_automation.py
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from datetime import datetime

class AutomationUI:
    def __init__(self, notebook, controller):
        self.notebook = notebook
        self.controller = controller
        self.dummy_var = tk.BooleanVar(value=False)
        self.cells = {}
        self.manual_vars = {} 
        self._create_tab()
        self.notebook.after(1500, lambda: self.sync_current_to_inputs(self.sn2_val))
        self.notebook.after(1500, lambda: self.sync_current_to_inputs(self.sn3_val))

    def _create_tab(self):
        self.tab = ttk.Frame(self.notebook)
        self.notebook.add(self.tab, text=" 🤖 General Scan ")

        self.sn2_val = self.controller.config_manager.get_config_value("SN2") or "SN2"
        self.sn3_val = self.controller.config_manager.get_config_value("SN3") or "SN3"

        main_container = ttk.Frame(self.tab, padding=10)
        main_container.pack(fill=tk.BOTH, expand=True)

        # 상하 공간 분배 (매트릭스가 들어갈 하단에 60% 비중 할당)
        main_container.rowconfigure(0, weight=4) 
        main_container.rowconfigure(1, weight=6) 
        main_container.columnconfigure(0, weight=1)

        # --- [상단 영역: Dashboard & Logs & Quick Setup] ---
        self.upper_notebook = ttk.Notebook(main_container)
        self.upper_notebook.grid(row=0, column=0, sticky="nsew", pady=(0, 10))


        # --- 1. Quick Setup 탭 ---
        info_tab = ttk.Frame(self.upper_notebook, padding=15)
        self.upper_notebook.add(info_tab, text=" 📋 Quick Setup ")

        # SN1도 Dir과 Rot을 통일감 있게 추가
        self.qs_vars = {
                "Shift_worker": tk.StringVar(), "Expert": tk.StringVar(), "NOTE": tk.StringVar(), 
                "Laser": tk.StringVar(), "Wavelength": tk.StringVar(),
            "SN1": tk.StringVar(), "HV1": tk.StringVar(), "direction1": tk.StringVar(), "RotateAngle1": tk.StringVar(),
            "SN2": tk.StringVar(), "HV2": tk.StringVar(), "direction2": tk.StringVar(), "RotateAngle2": tk.StringVar(),
            "SN3": tk.StringVar(), "HV3": tk.StringVar(), "direction3": tk.StringVar(), "RotateAngle3": tk.StringVar()
        }

        setup_frame = ttk.LabelFrame(info_tab, text=" ⚙️ Quick Configuration (Edit & Save) ", padding=15)
        setup_frame.pack(fill=tk.BOTH, expand=True)

        entry_font = ("Helvetica", 12, "bold") # 입력칸 폰트 확대
        lbl_font = ("Helvetica", 11, "bold")   # 라벨 폰트 확대

        def make_row(parent, row_idx, items):
            frame = tk.Frame(parent)
            frame.pack(fill=tk.X, pady=8) # 줄 간격 확대
            for i, (label_text, var_key) in enumerate(items):
                tk.Label(frame, text=label_text, font=lbl_font, width=8, anchor="e").pack(side=tk.LEFT, padx=(10 if i>0 else 0, 5))
                tk.Entry(frame, textvariable=self.qs_vars[var_key], font=entry_font, width=12, justify="center").pack(side=tk.LEFT)

        make_row(setup_frame, 0, [("Shifter:", "Shift_worker"), ("Expert:", "Expert"), ("Laser:", "Laser"), ("Note:", "NOTE")])
        ttk.Separator(setup_frame, orient="horizontal").pack(fill=tk.X, pady=10)
        
        # [수정] SN -> Dir(Cable) -> Rot -> HV 순서로 직관적으로 변경
        make_row(setup_frame, 1, [("SN1:", "SN1"), ("Dir(A~H):", "direction1"), ("Rot(°):", "RotateAngle1"), ("HV1(V):", "HV1")])
        make_row(setup_frame, 2, [("SN2:", "SN2"), ("Dir(A~H):", "direction2"), ("Rot(°):", "RotateAngle2"), ("HV2(V):", "HV2")])
        make_row(setup_frame, 3, [("SN3:", "SN3"), ("Dir(A~H):", "direction3"), ("Rot(°):", "RotateAngle3"), ("HV3(V):", "HV3")])

        # [수정] 저장 / 열기 버튼 50:50 황금비율 적용
        btn_frame = tk.Frame(info_tab)
        btn_frame.pack(fill=tk.X, pady=(15, 0))
        btn_frame.columnconfigure(0, weight=1)
        btn_frame.columnconfigure(1, weight=1)

        tk.Button(btn_frame, text="⚙️ Open Global Config (Paths)", bg="#6c757d", fg="white", font=("Helvetica", 12, "bold"), 
                  height=2, command=self.controller.open_config).grid(row=0, column=0, sticky="ew", padx=5)
                  
        tk.Button(btn_frame, text="💾 Save Settings", bg="#28a745", fg="white", font=("Helvetica", 12, "bold"), 
                  height=2, command=self.save_quick_setup).grid(row=0, column=1, sticky="ew", padx=5)

        dash_tab = ttk.Frame(self.upper_notebook, padding=10)
        self.upper_notebook.add(dash_tab, text=" 🎛️ Control Panel (Master) ")

        dash_tab.columnconfigure(0, weight=6) 
        dash_tab.columnconfigure(1, weight=4) 
        dash_tab.rowconfigure(0, weight=1)

        left_ctrl = ttk.LabelFrame(dash_tab, text=" ⚙️ Operation Controls ", padding=15)
        left_ctrl.grid(row=0, column=0, sticky="nsew", padx=(0, 10))

        for c in range(3): left_ctrl.columnconfigure(c, weight=1)
        for r in range(6): left_ctrl.rowconfigure(r, weight=1)
        
        self.dummy_chk = tk.Checkbutton(left_ctrl, text="🧪 TEST RUN (Simulation Mode)",
                                variable=self.dummy_var, font=("Helvetica", 10), fg="#007ACC")
        self.dummy_chk.grid(row=0, column=0, columnspan=3, padx=8, pady=(5, 15), sticky="w")

        self.btn_unlock = tk.Button(left_ctrl, text="🔓 Unlock", bg="#f0ad4e", font=("Helvetica", 12, "bold"), 
                                    height=2, command=self.controller.request_control_unlock)
        self.btn_unlock.grid(row=1, column=0, padx=8, pady=8, sticky="nsew")

        self.btn_start = tk.Button(left_ctrl, text="▶ Start run", bg="#28a745", fg="white", 
                                   font=("Helvetica", 11, "bold"), command=self.controller.auto_mgr.start_general_scan)
        self.btn_start.grid(row=1, column=1, padx=8, pady=8, sticky="nsew")

        self.btn_reset = tk.Button(left_ctrl, text="🔄 Reset\nangle", bg="#6c757d", fg="white", 
                                   font=("Helvetica", 10, "bold"), command=self.confirm_and_reset_angles)
        self.btn_reset.grid(row=1, column=2, rowspan=2, padx=8, pady=8, sticky="nsew")

        self.btn_stop_run = tk.Button(left_ctrl, text="⏹ Stop run", bg="#ffc107", 
                                      font=("Helvetica", 11, "bold"), 
                                      command=self.controller.auto_mgr.handle_stop_continue)
        self.btn_stop_run.grid(row=2, column=1, padx=8, pady=8, sticky="nsew")

        self.scan_status_label = ttk.Label(left_ctrl, text="SYSTEM STATUS: IDLE",
                                           font=("Helvetica", 12, "bold"), foreground="gray")
        self.scan_status_label.grid(row=0, column=1, sticky="e", padx=10)

        self.eta_label = ttk.Label(left_ctrl, text="ETA: --:--:--", font=("Helvetica", 13, "bold"), 
                                   foreground="#007ACC", anchor="center")
        self.eta_label.grid(row=3, column=0, columnspan=3, pady=10, sticky="nsew")

        self.btn_emg_stop = tk.Button(left_ctrl, text="🚨 Emergency", bg="#dc3545", fg="white", 
                                      font=("Helvetica", 8), height=1, command=self.controller.auto_mgr.emergency_stop)
        self.btn_emg_stop.grid(row=4, column=0, columnspan=3, padx=10, pady=5, sticky="s")

        self.params_label = ttk.Label(left_ctrl, text="Scan Params: Tilt 5.0° | Rot 90.0° | Rest 3.0s", font=("Helvetica", 10, "bold"), foreground="#007ACC")
        self.params_label.grid(row=5, column=0, columnspan=2, sticky="w", pady=(10,0))

        ############## REMOVE state=tk.DISABLED ################# 
        self.btn_scan_settings = tk.Button(left_ctrl, text="⚙️ Params (Admin)", command=self.open_scan_params, bg="#f0ad4e", fg="black",state=tk.DISABLED)
        self.btn_scan_settings.grid(row=5, column=2, sticky="e", pady=(10,0))

        right_status = ttk.LabelFrame(dash_tab, text=" 🛰️ Manual Control Panel ", padding=15)
        right_status.grid(row=0, column=1, sticky="nsew")

        for idx, sn in enumerate([self.sn2_val, self.sn3_val]):
            dev_frame = ttk.Frame(right_status)
            dev_frame.pack(fill=tk.X, pady=(0, 40 if idx==0 else 0)) 
            
            lbl = ttk.Label(dev_frame, text=f"{sn} | Status -> Tilt: 0.0°, Rot: 0.0°", 
                font=("Helvetica", 16, "bold"), foreground="#007ACC")
            lbl.pack(anchor="w", pady=(0, 10))
            if not hasattr(self, 'sn_labels'): self.sn_labels = {}
            self.sn_labels[sn] = lbl
            
            input_f = ttk.Frame(dev_frame)
            input_f.pack(fill=tk.X)
            
            t_v = tk.DoubleVar(value=0.0); r_v = tk.DoubleVar(value=0.0)
            
            ttk.Label(input_f, text="Tilt:", font=("Helvetica", 11, "bold")).pack(side=tk.LEFT, padx=(0, 5))
            ttk.Entry(input_f, textvariable=t_v, width=10, font=("Helvetica", 16, "bold"), justify="center").pack(side=tk.LEFT, padx=(0, 15))
            
            ttk.Label(input_f, text="Rot:", font=("Helvetica", 11, "bold")).pack(side=tk.LEFT, padx=(0, 5))
            ttk.Entry(input_f, textvariable=r_v, width=10, font=("Helvetica", 16, "bold"), justify="center").pack(side=tk.LEFT)
            
            self.manual_vars[sn] = (t_v, r_v)
            
            btn_f = ttk.Frame(dev_frame)
            btn_f.pack(fill=tk.X, pady=(12, 0))

            tk.Button(btn_f, text="📥 Get Current", bg="#6c757d", fg="white", font=("Helvetica", 9, "bold"),
                      command=lambda s=sn: self.sync_current_to_inputs(s)).pack(side=tk.LEFT, padx=5)
            """
            tk.Button(btn_f, text="↕️ Move Tilt", bg="#17a2b8", fg="white", font=("Helvetica", 10, "bold"), width=12,
                      command=lambda d=idx+2, s=sn: self.controller.rot_mgr.move_tilt_only(d, self.manual_vars[s][0].get())).pack(side=tk.LEFT, padx=5)

            tk.Button(btn_f, text="🔄 Move Rot", bg="#17a2b8", fg="white", font=("Helvetica", 10, "bold"), width=12,
                      command=lambda d=idx+2, s=sn: self.controller.rot_mgr.move_rot_only(d, self.manual_vars[s][1].get())).pack(side=tk.LEFT, padx=5)
            """
            tk.Button(btn_f, text="↕️ Move Tilt", bg="#17a2b8", fg="white", font=("Helvetica", 10, "bold"), width=12,
                      command=lambda d=idx+2, s=sn: self._move_and_auto_sync(d, s, self.manual_vars[s][0].get(), "tilt")).pack(side=tk.LEFT, padx=5)

            tk.Button(btn_f, text="🔄 Move Rot", bg="#17a2b8", fg="white", font=("Helvetica", 10, "bold"), width=12,
                      command=lambda d=idx+2, s=sn: self._move_and_auto_sync(d, s, self.manual_vars[s][1].get(), "rot")).pack(side=tk.LEFT, padx=5)


            tk.Button(btn_f, text="⏹ Stop", bg="#ffc107", font=("Helvetica", 10, "bold"), width=10,
                      command=lambda d=idx+2: self.controller.rot_mgr.stop_rotation(d)).pack(side=tk.LEFT, padx=5)
   
        # --- 3. Logs 탭 ---
        log_tab = ttk.Frame(self.upper_notebook, padding=10)
        self.upper_notebook.add(log_tab, text=" 📝 Logs ")
        self.log_display = scrolledtext.ScrolledText(log_tab, font=("Courier", 10), bg="#1e1e1e", fg="#d4d4d4")
        self.log_display.pack(fill=tk.BOTH, expand=True)

        # --- [하단 영역: 매트릭스 테이블 (스크롤 및 크기 대폭 확대)] ---
        matrix_container = ttk.Frame(main_container)
        matrix_container.grid(row=1, column=0, sticky="nsew")
        matrix_container.columnconfigure(0, weight=1)
        matrix_container.rowconfigure(0, weight=1)

        self.matrix_canvas = tk.Canvas(matrix_container, highlightthickness=0)
        self.matrix_scrollbar = ttk.Scrollbar(matrix_container, orient="vertical", command=self.matrix_canvas.yview)
        
        self.matrix_scroll_frame = ttk.Frame(self.matrix_canvas)

        self.matrix_scroll_frame.bind(
            "<Configure>",
            lambda e: self.matrix_canvas.configure(scrollregion=self.matrix_canvas.bbox("all"))
        )

        self.matrix_window_id = self.matrix_canvas.create_window((0, 0), window=self.matrix_scroll_frame, anchor="nw")
        
        self.matrix_canvas.bind(
            '<Configure>', 
            lambda e: self.matrix_canvas.itemconfig(self.matrix_window_id, width=e.width)
        )

        self.matrix_canvas.configure(yscrollcommand=self.matrix_scrollbar.set)

        self.matrix_canvas.grid(row=0, column=0, sticky="nsew", pady=(10, 0))
        self.matrix_scrollbar.grid(row=0, column=1, sticky="ns", pady=(10, 0))

        for sn in [self.sn2_val, self.sn3_val]:
            f = ttk.LabelFrame(self.matrix_scroll_frame, text=f" {sn} Scan Progress Matrix ", padding=10)
            f.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=5)
            self._build_horizontal_table(f, sn)

    def _build_horizontal_table(self, parent, sn):
        angles = list(range(-55, 56, 5))
        h_font = ("Helvetica", 11, "bold") 
        d_font = ("Helvetica", 10, "bold") 
        
        parent.columnconfigure(0, weight=0, minsize=110) 
        for col in range(1, len(angles) + 1): 
            parent.columnconfigure(col, weight=1)       

        ttk.Label(parent, text="Axis \\ Tilt", font=h_font, anchor="center").grid(row=0, column=0, sticky="nsew", pady=5)

        for i, tilt in enumerate(angles):
            ttk.Label(parent, text=f"{tilt}°", font=d_font, anchor="center").grid(row=0, column=i+1, sticky="nsew", pady=5)
            
        for r_idx, axis in enumerate(["X", "Y"]):
            ttk.Label(parent, text=f"{axis}-Axis", font=h_font, anchor="center").grid(row=r_idx+1, column=0, sticky="nsew", padx=5)
            
            for i, tilt in enumerate(angles):
                c = tk.Label(parent, text="-", bg="#e9ecef", relief="groove", font=d_font, width=8)
                c.grid(row=r_idx+1, column=i+1, sticky="nsew", padx=2, pady=3, ipady=6)
                self.cells[(sn, tilt, axis)] = c


    def open_scan_params(self):
        """Opens an Admin-only window to configure scan step sizes and rest time."""
        # 1. Admin 권한(Unlock) 확인
        if not self.controller.access_mgr.unlocked:
            if not self.controller.access_mgr.request_unlock():
                self.controller._log("[WARNING] Admin access denied for Scan Parameters.")
                return

        auto_mgr = self.controller.auto_mgr # rotation_manager의 인스턴스

        # 2. 팝업 창 생성
        win = tk.Toplevel(self.notebook)
        win.title("Scan Parameters (Admin)")
        win.geometry("320x250")
        win.attributes("-topmost", True)

        ttk.Label(win, text="Tilt Step (deg):", font=("Helvetica", 11)).grid(row=0, column=0, padx=20, pady=15, sticky="w")
        tilt_var = tk.DoubleVar(value=auto_mgr.tilt_step)
        ttk.Entry(win, textvariable=tilt_var, width=10, font=("Helvetica", 11)).grid(row=0, column=1)

        ttk.Label(win, text="Rot Step (deg):", font=("Helvetica", 11)).grid(row=1, column=0, padx=20, pady=15, sticky="w")
        rot_var = tk.DoubleVar(value=auto_mgr.rot_step)
        ttk.Entry(win, textvariable=rot_var, width=10, font=("Helvetica", 11)).grid(row=1, column=1)

        ttk.Label(win, text="Rest Time (sec):", font=("Helvetica", 11)).grid(row=2, column=0, padx=20, pady=15, sticky="w")
        rest_var = tk.DoubleVar(value=auto_mgr.rest_time)
        ttk.Entry(win, textvariable=rest_var, width=10, font=("Helvetica", 11)).grid(row=2, column=1)

        def save_params():
            auto_mgr.tilt_step = tilt_var.get()
            auto_mgr.rot_step = rot_var.get()
            auto_mgr.rest_time = rest_var.get()

            self.params_label.config(text=f"Scan Params: Tilt {auto_mgr.tilt_step}° | Rot {auto_mgr.rot_step}° | Rest {auto_mgr.rest_time}s")
            self.controller._log(f"[INFO] Scan params updated: Tilt {auto_mgr.tilt_step}°, Rot {auto_mgr.rot_step}°, Rest {auto_mgr.rest_time}s")
            win.destroy()

        btn_save = tk.Button(win, text="Save Parameters", command=save_params, bg="#5cb85c", fg="white", font=("Helvetica", 11, "bold"))
        btn_save.grid(row=3, column=0, columnspan=2, pady=20, ipadx=10, ipady=5)

    def update_run_info(self):
        if not hasattr(self.controller, 'config_manager') or not self.controller.config_manager:
            return
            
        cfg = self.controller.config_manager.get_all_variables()
        for key, var in self.qs_vars.items():
            if key in cfg:
                var.set(str(cfg[key]).strip('"')) 
            else:
                var.set("") 

    def save_quick_setup(self):
        if not hasattr(self.controller, 'config_manager') or not self.controller.config_manager:
            messagebox.showerror("Error", "Configuration is not loaded.")
            return

        entries_dict = {}
        for key, var_obj in self.qs_vars.items():
            class DummyEntry:
                def __init__(self, v): self.v = v
                def get(self): return self.v
            entries_dict[key] = DummyEntry(var_obj.get().strip())

        try:
            self.controller.config_manager.save_from_ui(entries_dict)
            self.controller._log("✅ Quick Setup settings saved to config file.")
            self.controller.refresh_all_data()
        except Exception as e:
            messagebox.showerror("Error", f"Save failed: {e}")

    def update_cell(self, sn, tilt, axis, status):
        colors = {"wait": "#e9ecef", "move": "#ffc107", "daq": "#007bff", "done": "#28a745"}
        if (sn, tilt, axis) in self.cells:
            text = "MOV" if status=="move" else "DAQ" if status=="daq" else "OK" if status=="done" else "-"
            # [핵심 수정] 백그라운드 스레드에서 UI를 변경할 때 멈추는(Deadlock) 현상을 원천 차단합니다.
            self.notebook.after(0, lambda: self.cells[(sn, tilt, axis)].config(bg=colors.get(status, "#e9ecef"), text=text))

    def confirm_and_reset_angles(self):
        """팝업 창 승인 후, 스캔을 완전히 취소하고 하드웨어 원점 복귀 및 UI 리셋을 동시 수행합니다."""
        msg = (
            "⚠️ WARNING: Abort & Hardware Origin Reset\n\n"
            "This will ABORT the current run and physically move both SN2 and SN3 back to the origin (0.0°).\n"
            "The movement may take up to 30~60 seconds.\n\n"
            "Do you want to proceed and start over?"
        )
        
        if messagebox.askyesno("Confirm Reset", msg):
            # 1. 진행/정지 중이던 스캔 스레드를 완전히 강제 종료(Abort) 및 복구 데이터 삭제
            if hasattr(self.controller, 'auto_mgr') and hasattr(self.controller.auto_mgr, 'abort_run'):
                self.controller.auto_mgr.abort_run()
                
            # 2. UI 매트릭스 지우기
            self.reset_matrix()
            
            # 3. 실제 모터 0도로 이동
            if hasattr(self.controller, 'auto_mgr') and hasattr(self.controller.auto_mgr, 'reset_all_angles'):
                self.controller.auto_mgr.reset_all_angles()
                
            self.add_auto_log("🔄 Run Aborted & Origin Reset Initiated: Moving SN2 & SN3 to 0.0°...")

    def reset_matrix(self):
        for cell in self.cells.values(): cell.config(bg="#e9ecef", text="-")
        self.log_display.delete('1.0', tk.END)
        self.eta_label.config(text="ETA: --:--:--")

    def update_dummy_status(self):
        state = "ENABLED" if self.dummy_var.get() else "DISABLED"
        self.add_auto_log(f"🛠️ Test Run Mode: {state}")

    def update_stop_button(self, is_running):
        if is_running:
            self.btn_stop_run.config(text="⏹ Stop run", bg="#ffc107")
        else:
            self.btn_stop_run.config(text="⏯ Continue", bg="#17a2b8", fg="white")

    def update_unlock_ui(self, is_unlocked):
        if is_unlocked:
            self.btn_unlock.config(text="🔓 Lock", bg="#28a745", fg="white")
        else:
            self.btn_unlock.config(text="🔒 Unlock", bg="#f0ad4e", fg="black")

    def lock_manual_panel(self, is_locked):
        """자동화 실행 중 우측 수동 패널을 반투명하게 느끼도록 색상을 변경하거나 비활성화합니다."""
        state = tk.DISABLED if is_locked else tk.NORMAL
        bg_color = "#3d3d3d" if is_locked else self.controller.ui.colors["dark"]["bg"] # 다크모드 기준

        # 실제 위젯들을 순회하며 상태 변경
        for sn in [self.sn2_val, self.sn3_val]:
            # 수동 입력창, 버튼 등을 state=state로 변경하는 로직 추가
            pass

    def update_start_button(self, is_running):
        """스캔 상태에 따라 모든 제어 버튼과 라벨을 동기화합니다."""
        if is_running:
            # 시작할 때: Start 비활성화 / Stop은 'Stop run'(노란색)으로 활성화
            self.btn_start.config(text="⏳ RUNNING...", bg="#6c757d", state=tk.DISABLED)
            self.btn_stop_run.config(text="⏹ Stop run", bg="#ffc107", state=tk.NORMAL)
            self.btn_reset.config(state=tk.DISABLED)
            self.scan_status_label.config(text="SYSTEM STATUS: SCANNING...", foreground="#dc3545")
            
            # [추가] Start 시 메인 화면의 Run DAQ 버튼 비활성화 (중복 방지)
            if hasattr(self.controller, 'ui') and 'run_daq' in self.controller.ui.buttons:
                self.controller.ui.buttons['run_daq'].config(state=tk.DISABLED, text="2. Run DAQ (Scanning)")
        else:
            # 정지(IDLE)할 때: Start 활성화 / Stop은 비활성화
            self.btn_start.config(text="▶ Start run", bg="#28a745", state=tk.NORMAL)
            self.btn_stop_run.config(text="⏹ Stop run", bg="#ffc107", state=tk.DISABLED) 
            self.btn_reset.config(state=tk.NORMAL)
            self.scan_status_label.config(text="SYSTEM STATUS: IDLE", foreground="gray")
            
            if hasattr(self.controller, 'ui') and 'run_daq' in self.controller.ui.buttons:
                if hasattr(self.controller, 'access_mgr') and self.controller.access_mgr.unlocked:
                    self.controller.ui.buttons['run_daq'].config(state=tk.NORMAL, text="2. Run DAQ")


    def update_sn_display(self, dev_num, tilt, rot):
        """백그라운드 스레드에서 받은 각도를 UI 라벨에 갱신합니다."""
        sn = None
        if dev_num == 2: sn = self.sn2_val
        elif dev_num == 3: sn = self.sn3_val
        
        if not sn: return

        t_str = f"{tilt:.1f}" if tilt is not None else "Err"
        r_str = f"{rot:.1f}" if rot is not None else "Err"

        if hasattr(self, 'sn_labels') and sn in self.sn_labels:
            self.notebook.after(0, lambda: self.sn_labels[sn].config(text=f"{sn} | Status -> Tilt: {t_str}°, Rot: {r_str}°"))

    def sync_current_to_inputs(self, sn):
        """Reads hardware angles, updates config3.h first, then syncs to the GUI Helper."""
        if not hasattr(self, 'sn_labels') or sn not in self.sn_labels: return
        status_text = self.sn_labels[sn].cget("text")

        try:
            parts = status_text.split("Tilt: ")[1].split(", Rot: ")
            tilt_val = float(parts[0].replace("°", ""))
            rot_val = float(parts[1].replace("°", ""))

            self.update_config_angles(sn, tilt_val, rot_val)

            if sn in self.manual_vars:
                t_v, r_v = self.manual_vars[sn]
                t_v.set(tilt_val)
                r_v.set(rot_val)

            self.controller._log(f"[INFO] Synced {sn} sequence: Hardware -> Config -> UI (Tilt: {tilt_val}°, Rot: {rot_val}°)")
            
            self.notebook.after(100, self.controller.refresh_all_data)

        except Exception as e:
            self.controller._log(f"[ERROR] Sync failed for {sn}: {e}")

    def update_config_angles(self, sn, tilt, rot):
        """Updates config3.h file using regex."""
        try:
            import re
            config_path = "/home/precalkor/Integrated_Control_SW/DAQ_Control_SW/config3.h"
            
            with open(config_path, 'r', encoding='utf-8') as f:
                content = f.read()
                
            dev_num = "2" if sn == self.sn2_val else "3"
            
            tilt_int = int(round(tilt))
            rot_int = int(round(rot))
            
            content = re.sub(rf'const std::string TiltAngle{dev_num}\s*=\s*".*";', f'const std::string TiltAngle{dev_num} = "{tilt_int}";', content)
            content = re.sub(rf'const std::string RotateAngle{dev_num}\s*=\s*".*";', f'const std::string RotateAngle{dev_num} = "{rot_int}";', content)
            
            with open(config_path, 'w', encoding='utf-8') as f:
                f.write(content)
                
        except Exception as e:
            self.controller._log(f"[WARNING] Failed to update config file for {sn}: {e}")

    def _move_and_auto_sync(self, dev_num, sn, target_val, axis):
        import threading
        import time

        if axis == "tilt":
            self.controller.rot_mgr.move_tilt_only(dev_num, target_val)
        else:
            self.controller.rot_mgr.move_rot_only(dev_num, target_val)

        def _wait_for_stop():
            time.sleep(1.0) 

            while getattr(self.controller.rot_mgr, 'is_moving', {}).get(dev_num, False):
                time.sleep(0.5)

            #self.controller._log(f"[INFO] Movement finished. Auto-syncing {sn}...")
            self.notebook.after(500, lambda: self.sync_current_to_inputs(sn))

        threading.Thread(target=_wait_for_stop, daemon=True).start()


    def start_eta_countdown(self, total_seconds, total_steps):
        """스캔 시작 시 호출되어 카운트다운과 총 용량(800MB/step)을 세팅합니다."""
        self.remaining_eta_seconds = int(total_seconds)
        self.total_est_size_mb = total_steps * 800.0 
        self.update_eta_realtime()

    def update_eta_realtime(self):
        """1초마다 남은 시간을 깎고 화면(self.eta_label)을 갱신합니다."""
        auto_mgr = getattr(self.controller, 'auto_mgr', None)
        
        if not auto_mgr or not auto_mgr.is_running:
            return 
            
        if auto_mgr.pause_event.is_set() and self.remaining_eta_seconds > 0:
            self.remaining_eta_seconds -= 1
            
            m, s = divmod(self.remaining_eta_seconds, 60)
            h, m = divmod(m, 60)
            
            if self.total_est_size_mb >= 1024:
                size_str = f"{self.total_est_size_mb / 1024.0:.1f} GB"
            else:
                size_str = f"{self.total_est_size_mb:.0f} MB"
            
            self.eta_label.config(text=f"Storage Warning: ~ {size_str} | ETA: {int(h):02d}:{int(m):02d}:{int(s):02d}")
            
        self.notebook.after(1000, self.update_eta_realtime)

    def add_auto_log(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_display.config(state=tk.NORMAL)
        self.log_display.insert(tk.END, f"[{timestamp}] {message}\n")

        if int(self.log_display.index('end-1c').split('.')[0]) > 1000:
            self.log_display.delete('1.0', '100.0')

        self.log_display.config(state=tk.DISABLED)
        self.log_display.see(tk.END)

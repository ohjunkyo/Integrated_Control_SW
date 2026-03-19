# managers/ui_automation.py
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from datetime import datetime

class AutomationUI:
    def __init__(self, notebook, controller):
        self.notebook = notebook
        self.controller = controller
        self.dummy_var = tk.BooleanVar(value=True)
        self.cells = {}
        self.manual_vars = {} 
        self._create_tab()

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
            "Shift_worker": tk.StringVar(), "Expert": tk.StringVar(), "NOTE": tk.StringVar(), "Laser": tk.StringVar(),
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

        # --- 2. Dashboard 탭 ---
        dash_tab = ttk.Frame(self.upper_notebook, padding=10)
        self.upper_notebook.add(dash_tab, text=" 🎛️ Dashboard ")

        dash_tab.columnconfigure(0, weight=6) 
        dash_tab.columnconfigure(1, weight=4) 
        dash_tab.rowconfigure(0, weight=1)

        left_ctrl = ttk.LabelFrame(dash_tab, text=" ⚙️ Operation Controls ", padding=15)
        left_ctrl.grid(row=0, column=0, sticky="nsew", padx=(0, 10))

        for c in range(3): left_ctrl.columnconfigure(c, weight=1)
        for r in range(6): left_ctrl.rowconfigure(r, weight=1)
        
        self.dummy_chk = tk.Checkbutton(left_ctrl, text="🧪 TEST RUN (Simulation Mode)", 
                                        variable=self.dummy_var, font=("Helvetica", 13, "bold"), fg="#007ACC")
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

        self.eta_label = ttk.Label(left_ctrl, text="ETA: --:--:--", font=("Helvetica", 13, "bold"), 
                                   foreground="#007ACC", anchor="center")
        self.eta_label.grid(row=3, column=0, columnspan=3, pady=10, sticky="nsew")

        self.btn_emg_stop = tk.Button(left_ctrl, text="🚨 Emergency", bg="#dc3545", fg="white", 
                                      font=("Helvetica", 8), height=1, command=self.controller.auto_mgr.emergency_stop)
        self.btn_emg_stop.grid(row=4, column=0, columnspan=3, padx=10, pady=5, sticky="s")

        right_status = ttk.LabelFrame(dash_tab, text=" 🛰️ Manual Control Panel ", padding=15)
        right_status.grid(row=0, column=1, sticky="nsew")

        for idx, sn in enumerate([self.sn2_val, self.sn3_val]):
            dev_frame = ttk.Frame(right_status)
            dev_frame.pack(fill=tk.X, pady=(0, 40 if idx==0 else 0)) 
            
            lbl = ttk.Label(dev_frame, text=f"{sn} | Status -> Tilt: 0.0°, Rot: 0.0°", 
                            font=("Helvetica", 12, "bold"), foreground="#007ACC")
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
            
            tk.Button(btn_f, text="🚀 Move to Target", bg="#17a2b8", fg="white", font=("Helvetica", 10, "bold"), width=15,
                      command=lambda d=idx+2, s=sn: self.controller.rot_mgr.move_rotation(d, *[v.get() for v in self.manual_vars[s]])).pack(side=tk.LEFT, padx=5)
            tk.Button(btn_f, text="⏹ Stop", bg="#ffc107", font=("Helvetica", 10, "bold"), width=10,
                      command=self.controller.auto_mgr.emergency_stop).pack(side=tk.LEFT, padx=5)

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
            self.cells[(sn, tilt, axis)].config(bg=colors.get(status, "#e9ecef"), text=text)

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

    # (기존에 있던 reset_matrix 함수는 그대로 둡니다)
    def reset_matrix(self):
        for cell in self.cells.values(): cell.config(bg="#e9ecef", text="-")
        self.log_display.delete('1.0', tk.END)
        self.eta_label.config(text="ETA: --:--:--")

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
    def update_sn_display(self, sn, tilt, rot):
        if hasattr(self, 'sn_labels') and sn in self.sn_labels:
            self.sn_labels[sn].config(text=f"{sn} | Status -> Tilt: {tilt}°, Rot: {rot}°")

    def add_auto_log(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_display.config(state=tk.NORMAL)
        self.log_display.insert(tk.END, f"[{timestamp}] {message}\n")

        if int(self.log_display.index('end-1c').split('.')[0]) > 1000:
            self.log_display.delete('1.0', '100.0')

        self.log_display.config(state=tk.DISABLED)
        self.log_display.see(tk.END)

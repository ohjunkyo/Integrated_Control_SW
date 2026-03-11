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

        # [1] 상하 비율 조정: 매트릭스 높이를 1.3배 이상 확보 (3.5:6.5)
        main_container.rowconfigure(0, weight=35) 
        main_container.rowconfigure(1, weight=65) 
        main_container.columnconfigure(0, weight=1)

        # --- [상단 영역: Dashboard & Logs] ---
        self.upper_notebook = ttk.Notebook(main_container)
        self.upper_notebook.grid(row=0, column=0, sticky="nsew", pady=(0, 10))

        dash_tab = ttk.Frame(self.upper_notebook, padding=10)
        self.upper_notebook.add(dash_tab, text=" 🎛️ Dashboard ")

        # 상단 내부 6:4 좌우 비율 유지
        dash_tab.columnconfigure(0, weight=6) 
        dash_tab.columnconfigure(1, weight=4) 
        dash_tab.rowconfigure(0, weight=1)

        # --- [좌측: Operation Controls (6)] ---
        left_ctrl = ttk.LabelFrame(dash_tab, text=" ⚙️ Operation Controls ", padding=15)
        left_ctrl.grid(row=0, column=0, sticky="nsew", padx=(0, 10))

        for c in range(3): left_ctrl.columnconfigure(c, weight=1)
        for r in range(4): left_ctrl.rowconfigure(r, weight=1)

        self.btn_unlock = tk.Button(left_ctrl, text="🔓 Unlock", bg="#f0ad4e", 
                                    font=("Helvetica", 12, "bold"), height=2, 
                                    command=self.controller.request_control_unlock)
        self.btn_unlock.grid(row=0, column=0, padx=10, pady=10, sticky="nsew")

        self.dummy_chk = tk.Checkbutton(left_ctrl, text="Test run", variable=self.dummy_var, font=("Helvetica", 10)) 
        self.dummy_chk.grid(row=1, column=0, padx=10, pady=5, sticky="w")

        self.btn_start = tk.Button(left_ctrl, text="▶ Start run", bg="#28a745", fg="white", 
                                   font=("Helvetica", 11, "bold"), command=self.controller.auto_mgr.start_general_scan)
        self.btn_start.grid(row=0, column=1, padx=10, pady=10, sticky="nsew")

        self.btn_stop_run = tk.Button(left_ctrl, text="⏹ Stop run", bg="#ffc107", 
                                      font=("Helvetica", 11, "bold"), command=self.controller.auto_mgr.stop_run) 
        self.btn_stop_run.grid(row=1, column=1, padx=10, pady=10, sticky="nsew")

        self.btn_reset = tk.Button(left_ctrl, text="🔄 Reset\nangle", bg="#6c757d", fg="white", 
                                   font=("Helvetica", 10, "bold"), command=self.reset_matrix)
        self.btn_reset.grid(row=0, column=2, rowspan=2, padx=10, pady=10, sticky="nsew")

        self.eta_label = ttk.Label(left_ctrl, text="ETA: --:--:--", font=("Helvetica", 13, "bold"), 
                                   foreground="#007ACC", anchor="center")
        self.eta_label.grid(row=2, column=0, columnspan=3, pady=5)

        self.btn_emg_stop = tk.Button(left_ctrl, text="🚨 Emergency", bg="#dc3545", fg="white", 
                                      font=("Helvetica", 8), height=1, command=self.controller.auto_mgr.emergency_stop)
        self.btn_emg_stop.grid(row=3, column=0, columnspan=3, padx=10, pady=5, sticky="s")


        # --- [우측: Manual Control (4) - 라벨링 및 크기 대폭 강화] ---
        right_status = ttk.LabelFrame(dash_tab, text=" 🛰️ Manual Control Panel ", padding=15)
        right_status.grid(row=0, column=1, sticky="nsew")

        for idx, sn in enumerate([self.sn2_val, self.sn3_val]):
            dev_frame = ttk.Frame(right_status)
            dev_frame.pack(fill=tk.X, pady=(0, 40 if idx==0 else 0)) # 간격 더 넓힘
            
            # [수정] 현재 상태 라벨 명확화
            lbl = ttk.Label(dev_frame, text=f"{sn} | Status -> Tilt: 0.0°, Rot: 0.0°", 
                            font=("Helvetica", 12, "bold"), foreground="#007ACC")
            lbl.pack(anchor="w", pady=(0, 10))
            if not hasattr(self, 'sn_labels'): self.sn_labels = {}
            self.sn_labels[sn] = lbl
            
            # [수정] 입력창 영역 (라벨 추가 및 크기 확대)
            input_f = ttk.Frame(dev_frame)
            input_f.pack(fill=tk.X)
            
            t_v = tk.DoubleVar(value=0.0); r_v = tk.DoubleVar(value=0.0)
            
            # Tilt 입력부
            ttk.Label(input_f, text="Tilt:", font=("Helvetica", 11, "bold")).pack(side=tk.LEFT, padx=(0, 5))
            ttk.Entry(input_f, textvariable=t_v, width=10, font=("Helvetica", 16, "bold"), justify="center").pack(side=tk.LEFT, padx=(0, 15))
            
            # Rotation 입력부
            ttk.Label(input_f, text="Rot:", font=("Helvetica", 11, "bold")).pack(side=tk.LEFT, padx=(0, 5))
            ttk.Entry(input_f, textvariable=r_v, width=10, font=("Helvetica", 16, "bold"), justify="center").pack(side=tk.LEFT)
            
            self.manual_vars[sn] = (t_v, r_v)
            
            # 버튼 영역
            btn_f = ttk.Frame(dev_frame)
            btn_f.pack(fill=tk.X, pady=(12, 0))
            
            tk.Button(btn_f, text="🚀 Move to Target", bg="#17a2b8", fg="white", font=("Helvetica", 10, "bold"), width=15,
                      command=lambda d=idx+2, s=sn: self.controller.rot_mgr.move_rotation(d, *[v.get() for v in self.manual_vars[s]])).pack(side=tk.LEFT, padx=5)
            tk.Button(btn_f, text="⏹ Stop", bg="#ffc107", font=("Helvetica", 10, "bold"), width=10,
                      command=self.controller.auto_mgr.emergency_stop).pack(side=tk.LEFT, padx=5)

        # --- Logs 탭 ---
        log_tab = ttk.Frame(self.upper_notebook, padding=10)
        self.upper_notebook.add(log_tab, text=" 📝 Logs ")
        self.log_display = scrolledtext.ScrolledText(log_tab, font=("Courier", 10), bg="#1e1e1e", fg="#d4d4d4")
        self.log_display.pack(fill=tk.BOTH, expand=True)


        # --- [하단 영역: 매트릭스 테이블 (높이 비율 65%로 확대)] ---
        self.bottom_frame = ttk.Frame(main_container)
        self.bottom_frame.grid(row=1, column=0, sticky="nsew")

        for sn in [self.sn2_val, self.sn3_val]:
            f = ttk.LabelFrame(self.bottom_frame, text=f" {sn} Scan Progress Matrix ", padding=8)
            f.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=3)
            self._build_horizontal_table(f, sn)

    def _build_horizontal_table(self, parent, sn):
        angles = list(range(-55, 56, 5))
        h_font = ("Helvetica", 9, "bold"); d_font = ("Helvetica", 8)
        parent.columnconfigure(0, weight=0, minsize=80) 
        for col in range(1, len(angles) + 1): parent.columnconfigure(col, weight=1)       
        for r in range(3): parent.rowconfigure(r, weight=1)

        ttk.Label(parent, text="Axis/Tilt", font=h_font, anchor="center").grid(row=0, column=0, sticky="nsew")
        ttk.Label(parent, text="X-Axis", font=h_font, anchor="center").grid(row=1, column=0, sticky="nsew")
        ttk.Label(parent, text="Y-Axis", font=h_font, anchor="center").grid(row=2, column=0, sticky="nsew")

        for i, tilt in enumerate(angles):
            ttk.Label(parent, text=f"{tilt}°", font=d_font, anchor="center").grid(row=0, column=i+1, sticky="nsew")
            for r_idx, axis in enumerate(["X", "Y"]):
                c = tk.Label(parent, text="-", bg="#e9ecef", relief="groove", font=d_font)
                c.grid(row=r_idx+1, column=i+1, sticky="nsew", padx=1, pady=1)
                self.cells[(sn, tilt, axis)] = c

    def update_cell(self, sn, tilt, axis, status):
        colors = {"wait": "#e9ecef", "move": "#ffc107", "daq": "#007bff", "done": "#28a745"}
        if (sn, tilt, axis) in self.cells:
            text = "MOV" if status=="move" else "DAQ" if status=="daq" else "OK" if status=="done" else "-"
            self.cells[(sn, tilt, axis)].config(bg=colors.get(status, "#e9ecef"), text=text)

    def reset_matrix(self):
        for cell in self.cells.values(): cell.config(bg="#e9ecef", text="-")
        self.log_display.delete('1.0', tk.END)
        self.eta_label.config(text="ETA: --:--:--")

    def update_sn_display(self, sn, tilt, rot):
        if hasattr(self, 'sn_labels') and sn in self.sn_labels: 
            self.sn_labels[sn].config(text=f"{sn} | Status -> Tilt: {tilt}°, Rot: {rot}°")

    def add_auto_log(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_display.insert(tk.END, f"[{timestamp}] {message}\n")
        self.log_display.see(tk.END)

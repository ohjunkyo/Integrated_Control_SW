# managers/ui_automation.py
import os
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from datetime import datetime, timezone, timedelta  

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
        self.refresh_schedule_list()

    def _create_tab(self):
        self.tab = ttk.Frame(self.notebook)
        self.notebook.add(self.tab, text=" 🤖 General Scan ")

        self.sn2_val = self.controller.config_manager.get_config_value("SN2") or "SN2"
        self.sn3_val = self.controller.config_manager.get_config_value("SN3") or "SN3"

        main_container = ttk.Frame(self.tab, padding=10)
        main_container.pack(fill=tk.BOTH, expand=True)

        main_container.rowconfigure(0, weight=1)
        main_container.columnconfigure(0, weight=1)

        self.upper_notebook = ttk.Notebook(main_container)
        self.upper_notebook.grid(row=0, column=0, sticky="nsew")

        # --- 1. Quick Setup 탭 ---
        info_tab = ttk.Frame(self.upper_notebook, padding=15)
        self.upper_notebook.add(info_tab, text=" 📋 Quick Setup ")

        self.qs_vars = {
                "Shift_worker": tk.StringVar(), "Expert": tk.StringVar(), "NOTE": tk.StringVar(), 
                "Laser": tk.StringVar(), "Wavelength": tk.StringVar(),
            "SN1": tk.StringVar(), "HV1": tk.StringVar(), "direction1": tk.StringVar(), "RotateAngle1": tk.StringVar(),
            "SN2": tk.StringVar(), "HV2": tk.StringVar(), "direction2": tk.StringVar(), "RotateAngle2": tk.StringVar(),
            "SN3": tk.StringVar(), "HV3": tk.StringVar(), "direction3": tk.StringVar(), "RotateAngle3": tk.StringVar()
        }

        setup_frame = ttk.LabelFrame(info_tab, text=" ⚙️ Quick Configuration (Edit & Save) ", padding=15)
        setup_frame.pack(fill=tk.BOTH, expand=True)

        entry_font = ("Helvetica", 12, "bold") 
        lbl_font = ("Helvetica", 11, "bold")   

        def make_row(parent, row_idx, items):
            frame = tk.Frame(parent)
            frame.pack(fill=tk.X, pady=8)
            for i, (label_text, var_key) in enumerate(items):
                tk.Label(frame, text=label_text, font=lbl_font, width=8, anchor="e").pack(side=tk.LEFT, padx=(10 if i>0 else 0, 5))
                tk.Entry(frame, textvariable=self.qs_vars[var_key], font=entry_font, width=12, justify="center").pack(side=tk.LEFT)

        make_row(setup_frame, 0, [("Shifter:", "Shift_worker"), ("Expert:", "Expert"), 
                                  ("Laser:", "Laser"), ("Wavelength:", "Wavelength"),
                                  ("Note:", "NOTE")])
        ttk.Separator(setup_frame, orient="horizontal").pack(fill=tk.X, pady=10)
        
        make_row(setup_frame, 1, [("SN1:", "SN1"), ("Dir(A~H):", "direction1"), ("Rot(°):", "RotateAngle1"), ("HV1(V):", "HV1")])
        make_row(setup_frame, 2, [("SN2:", "SN2"), ("Dir(A~H):", "direction2"), ("Rot(°):", "RotateAngle2"), ("HV2(V):", "HV2")])
        make_row(setup_frame, 3, [("SN3:", "SN3"), ("Dir(A~H):", "direction3"), ("Rot(°):", "RotateAngle3"), ("HV3(V):", "HV3")])

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
        dash_tab.rowconfigure(0, weight=0)   # controls row — minimal height
        dash_tab.rowconfigure(1, weight=1)   # matrix row — takes remaining space

        left_ctrl = ttk.LabelFrame(dash_tab, text=" ⚙️ Operation Controls ", padding=8)
        left_ctrl.grid(row=0, column=0, sticky="nsew", padx=(0, 10))

        self.scan_status_label = ttk.Label(left_ctrl, text="SYSTEM STATUS: IDLE",
                                          font=("Helvetica", 14, "bold"), foreground="gray")
        self.scan_status_label.grid(row=0, column=2, sticky="e", padx=5)

        for c in range(3): left_ctrl.columnconfigure(c, weight=1)
        for r in range(6): left_ctrl.rowconfigure(r, weight=1)
        
        self.dummy_chk = tk.Checkbutton(left_ctrl, text="🧪 TEST RUN (Simulation Mode)",
                                variable=self.dummy_var, font=("Helvetica", 10), fg="#007ACC")
        self.dummy_chk.grid(row=0, column=0, columnspan=3, padx=8, pady=(2, 4), sticky="w")

        # [중복 제거] 기존엔 여기에도 Unlock 버튼이 있었으나, 이제 잠금/해제는
        # DAQ 탭 상단의 안전 배너(_create_lock_banner)가 단일 소스로 담당한다.
        # 남은 공간은 Start/Stop 이 채우도록 재배치한다.
        # ── 그룹 1: 주 제어 (Start / Pause) + Reset ─────────────────────────
        primary = ttk.LabelFrame(left_ctrl, text=" ▶ Run Control ", padding=8)
        primary.grid(row=1, column=0, columnspan=3, sticky="nsew", padx=4, pady=(0, 6))
        primary.columnconfigure(0, weight=3)
        primary.columnconfigure(1, weight=3)
        primary.columnconfigure(2, weight=2)

        self.btn_start = tk.Button(primary, text="▶ Start run", bg="#28a745", fg="white",
                                   font=("Helvetica", 12, "bold"), height=1,
                                   command=self.controller.auto_mgr.start_general_scan)
        self.btn_start.grid(row=0, column=0, padx=4, pady=3, sticky="nsew")

        # Pause/Continue 토글(진행상황 보존). 속성명은 호환을 위해 btn_stop_run 유지.
        self.btn_stop_run = tk.Button(primary, text="⏸ Pause", bg="#ffc107",
                                      font=("Helvetica", 12, "bold"), height=1,
                                      command=self.controller.auto_mgr.handle_stop_continue)
        self.btn_stop_run.grid(row=0, column=1, padx=4, pady=3, sticky="nsew")

        self.btn_reset = tk.Button(primary, text="🔄 Reset angle", bg="#6c757d", fg="white",
                                   font=("Helvetica", 10, "bold"),
                                   command=self.confirm_and_reset_angles)
        self.btn_reset.grid(row=0, column=2, padx=4, pady=3, sticky="nsew")

        # ── ETA (강조) ──────────────────────────────────────────────────────
        self.eta_label = ttk.Label(left_ctrl, text="ETA: --:--:--",
                                   font=("Helvetica", 15, "bold"),
                                   foreground="#007ACC", anchor="center")
        self.eta_label.grid(row=2, column=0, columnspan=3, pady=(4, 6), sticky="nsew")

        am = self.controller.auto_mgr
        param_text = f"Scan Params: Tilt {am.tilt_step}° | Rot {am.rot_step}° | Rest {am.rest_time}s"
        self.params_label = ttk.Label(left_ctrl, text=param_text,
                                      font=("Helvetica", 10, "bold"), foreground="#6c757d",
                                      anchor="center")
        self.params_label.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(0, 4))

        ttk.Separator(left_ctrl, orient="horizontal").grid(
            row=4, column=0, columnspan=3, sticky="ew", pady=4)

        # ── 그룹 2: 위험 구역 (Abort) + 안내/Params ─────────────────────────
        danger = ttk.LabelFrame(left_ctrl, text=" 🛑 Danger Zone ", padding=8)
        danger.grid(row=5, column=0, columnspan=3, sticky="nsew", padx=4, pady=(2, 0))
        danger.columnconfigure(0, weight=1)

        self.btn_emg_stop = tk.Button(danger, text="⚠️ Re-Run / Abort Scan", bg="#dc3545",
                                      fg="white", font=("Helvetica", 12, "bold"), height=1,
                                      command=self.confirm_abort)
        self.btn_emg_stop.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        side_btns = ttk.Frame(danger)
        side_btns.grid(row=0, column=1, sticky="ns")
        tk.Button(side_btns, text="ⓘ Stop / Abort Guide", command=self.show_stop_sequences_info,
                  bg="#e9ecef", fg="#333", font=("Helvetica", 9, "bold"),
                  relief="flat").pack(fill=tk.X, pady=(0, 4))
        self.btn_scan_settings = tk.Button(side_btns, text="⚙️ Params", command=self.open_scan_params,
                                           bg="#f0ad4e", fg="black",
                                           font=("Helvetica", 9, "bold"), state=tk.DISABLED)
        self.btn_scan_settings.pack(fill=tk.X)

        right_status = ttk.LabelFrame(dash_tab, text=" 🛰️ Manual Control Panel ", padding=8)
        right_status.grid(row=0, column=1, sticky="nsew")

        self.manual_control_buttons = []
        self.manual_rot_buttons = {}   # dev_num -> (btn_rot, interlock_label)

        for idx, sn in enumerate([self.sn2_val, self.sn3_val]):
            dev_frame = ttk.Frame(right_status)
            dev_frame.pack(fill=tk.X, pady=(0, 10 if idx==0 else 0))

            lbl = ttk.Label(dev_frame, text=f"{sn} | Status -> Tilt: 0.0°, Rot: 0.0°",
                font=("Helvetica", 13, "bold"), foreground="#007ACC")
            lbl.pack(anchor="w", pady=(0, 4))
            if not hasattr(self, 'sn_labels'): self.sn_labels = {}
            self.sn_labels[sn] = lbl
            
            input_f = ttk.Frame(dev_frame)
            input_f.pack(fill=tk.X)
            
            t_v = tk.DoubleVar(value=0.0); r_v = tk.DoubleVar(value=0.0)
            
            ttk.Label(input_f, text="Tilt:", font=("Helvetica", 10, "bold")).pack(side=tk.LEFT, padx=(0, 4))
            ttk.Entry(input_f, textvariable=t_v, width=8, font=("Helvetica", 12, "bold"), justify="center").pack(side=tk.LEFT, padx=(0, 10))

            ttk.Label(input_f, text="Rot:", font=("Helvetica", 10, "bold")).pack(side=tk.LEFT, padx=(0, 4))
            ttk.Entry(input_f, textvariable=r_v, width=8, font=("Helvetica", 12, "bold"), justify="center").pack(side=tk.LEFT)
            
            self.manual_vars[sn] = (t_v, r_v)
            
            btn_f = ttk.Frame(dev_frame)
            btn_f.pack(fill=tk.X, pady=(5, 0))

            btn_get = tk.Button(btn_f, text="🔄 Get Current", 
                                command=lambda s=sn: self.sync_current_to_inputs(s),
                                bg="#007ACC", fg="white", 
                                font=("Helvetica", 9, "bold"),
                                relief="flat", overrelief="raised")
            btn_get.pack(side=tk.LEFT, padx=5)

            btn_tilt = tk.Button(btn_f, text="↕️ Move Tilt", bg="#17a2b8", fg="white", font=("Helvetica", 10, "bold"), width=12,
                      command=lambda d=idx+2, s=sn: self._move_and_auto_sync(d, s, self.manual_vars[s][0].get(), "tilt"))
            btn_tilt.pack(side=tk.LEFT, padx=5)
            self.manual_control_buttons.append(btn_tilt)

            btn_rot = tk.Button(btn_f, text="🔄 Move Rot", bg="#17a2b8", fg="white", font=("Helvetica", 10, "bold"), width=12,
                      command=lambda d=idx+2, s=sn: self._move_and_auto_sync(d, s, self.manual_vars[s][1].get(), "rot"))
            btn_rot.pack(side=tk.LEFT, padx=5)
            self.manual_control_buttons.append(btn_rot)

            tk.Button(btn_f, text="⏹ Stop", bg="#ffc107", font=("Helvetica", 10, "bold"), width=10,
                      command=lambda d=idx+2: self.controller.rot_mgr.stop_rotation(d)).pack(side=tk.LEFT, padx=5)

            # Interlock warning label — shown when tilt != 0
            lock_lbl = tk.Label(btn_f, text="🔒 Tilt to 0° before rotating",
                                fg="#dc3545", bg=btn_f.winfo_toplevel().cget("bg"),
                                font=("Helvetica", 9, "bold"))
            lock_lbl.pack(side=tk.LEFT, padx=(6, 0))
            lock_lbl.pack_forget()   # hidden by default

            self.manual_rot_buttons[idx + 2] = (btn_rot, lock_lbl)

        # ── Scan Progress Matrix (dash_tab row=1, spans both columns) ──────
        matrix_outer = ttk.LabelFrame(dash_tab, text=" 📊 Scan Progress Matrix ", padding=5)
        matrix_outer.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(8, 0))
        matrix_outer.columnconfigure(0, weight=1)
        matrix_outer.rowconfigure(0, weight=1)

        self.matrix_canvas = tk.Canvas(matrix_outer, highlightthickness=0)
        self.matrix_scrollbar = ttk.Scrollbar(matrix_outer, orient="vertical",
                                              command=self.matrix_canvas.yview)
        self.matrix_scroll_frame = ttk.Frame(self.matrix_canvas)

        self.matrix_scroll_frame.bind(
            "<Configure>",
            lambda e: self.matrix_canvas.configure(scrollregion=self.matrix_canvas.bbox("all"))
        )

        self.matrix_window_id = self.matrix_canvas.create_window(
            (0, 0), window=self.matrix_scroll_frame, anchor="nw")
        self.matrix_canvas.bind(
            "<Configure>",
            lambda e: self.matrix_canvas.itemconfig(self.matrix_window_id, width=e.width)
        )
        self.matrix_canvas.configure(yscrollcommand=self.matrix_scrollbar.set)

        self.matrix_canvas.grid(row=0, column=0, sticky="nsew")
        self.matrix_scrollbar.grid(row=0, column=1, sticky="ns")

        for sn in [self.sn2_val, self.sn3_val]:
            f = ttk.LabelFrame(self.matrix_scroll_frame,
                               text=f" {sn} Scan Progress Matrix ", padding=10)
            f.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=5)
            self._build_horizontal_table(f, sn)

        # --- 2. Schedule Managers 탭 ---
        schedule_tab = ttk.Frame(self.upper_notebook, padding=10)
        self.upper_notebook.add(schedule_tab, text=" ⏰ Schedule Manager ")
        self._build_schedule_tab(schedule_tab)

        # --- 3. Logs 탭 ---
        log_tab = ttk.Frame(self.upper_notebook, padding=10)
        self.upper_notebook.add(log_tab, text=" 📝 Live Scan Logs ")
        
        self.log_display = scrolledtext.ScrolledText(log_tab, font=("Consolas", 12, "bold"), bg="#1e1e1e", fg="#e0e0e0")
        self.log_display.pack(fill=tk.BOTH, expand=True)

        self.log_display.tag_config("TIME", foreground="#8c8c8c")     
        self.log_display.tag_config("INFO", foreground="#4da6ff")     
        self.log_display.tag_config("WARNING", foreground="#ffcc00")  
        self.log_display.tag_config("ERROR", foreground="#ff4d4d")    
        self.log_display.tag_config("SUCCESS", foreground="#00e676")  
        self.log_display.tag_config("NORMAL", foreground="#e0e0e0")   

        # --- 4. Scan History ---
        history_tab = ttk.Frame(self.upper_notebook, padding=10)
        self.upper_notebook.add(history_tab, text=" 📊 Scan History ")
        self._build_history_tab(history_tab)


    def _build_horizontal_table(self, parent, sn):
        angles = list(range(-55, 56, 5))
        h_font = ("Helvetica", 13, "bold")
        d_font = ("Helvetica", 12, "bold")
        
        parent.columnconfigure(0, weight=0, minsize=110) 
        for col in range(1, len(angles) + 1): 
            parent.columnconfigure(col, weight=1)       

        ttk.Label(parent, text="Axis \\ Tilt", font=h_font, anchor="center").grid(row=0, column=0, sticky="nsew", pady=5)

        for i, tilt in enumerate(angles):
            ttk.Label(parent, text=f"{tilt}°", font=d_font, anchor="center").grid(row=0, column=i+1, sticky="nsew", pady=5)
            
        for r_idx, axis in enumerate(["X", "Y"]):
            ttk.Label(parent, text=f"{axis}-Axis", font=h_font, anchor="center").grid(row=r_idx+1, column=0, sticky="nsew", padx=5)
            
            for i, tilt in enumerate(angles):
                c = tk.Label(parent, text="-", bg="#e9ecef", relief="groove", font=d_font, width=6)
                c.grid(row=r_idx+1, column=i+1, sticky="nsew", padx=2, pady=5, ipady=16)
                self.cells[(sn, tilt, axis)] = c


    def open_scan_params(self):
        """Opens an Admin-only window to configure scan step sizes and rest time."""
        if not self.controller.access_mgr.unlocked:
            if not self.controller.access_mgr.request_unlock():
                self.controller._log("[WARNING] Admin access denied for Scan Parameters.")
                return

        auto_mgr = self.controller.auto_mgr 

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
        msg = (
            "⚠️ WARNING: Abort & Hardware Origin Reset\n\n"
            "This will ABORT the current run and physically move both SN2 and SN3 back to the origin (0.0°).\n"
            "The movement may take up to 30~60 seconds.\n\n"
            "Do you want to proceed and start over?"
        )
        
        if messagebox.askyesno("Confirm Reset", msg):
            if hasattr(self.controller, 'auto_mgr') and hasattr(self.controller.auto_mgr, 'abort_run'):
                self.controller.auto_mgr.abort_run()
                
            self.reset_matrix()
            
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
            self.btn_stop_run.config(text="⏸ Pause", bg="#ffc107", fg="black")
        else:
            self.btn_stop_run.config(text="⏯ Continue", bg="#17a2b8", fg="white")

    def confirm_abort(self):
        """Three-way dialog: [Yes]=abort+fresh start, [No]=abort+keep resume, [Cancel]=do nothing."""
        am = self.controller.auto_mgr
        msg = (
            "⚠️ Abort & Stop Motors — shutdown sequence:\n"
            "   1. Immediately halt scan loop\n"
            "   2. Stop motors (SN2 · SN3)\n"
            "   3. Force-kill DAQ process (execute_DAQ_v2)\n"
            "   4. Reset UI\n\n"
            "What should happen to the recovery checkpoint?\n\n"
            "  [Yes]     Abort + DELETE checkpoint  →  next Start begins fresh from -55°\n"
            "  [No]      Abort + KEEP checkpoint    →  next Start can resume from last point\n"
            "  [Cancel]  Do nothing — return to scan\n\n"
            "※ The last step's data may be incomplete regardless of choice."
        )
        res = messagebox.askyesnocancel("Re-Run / Abort Scan", msg, icon="warning")
        if res is None:
            return
        am.emergency_stop()
        if res is True:
            try:
                if os.path.exists(am.state_file):
                    os.remove(am.state_file)
            except Exception as e:
                self.controller._log(f"[WARNING] Failed to clear recovery state: {e}")
            am.resume_data = None
            self.add_auto_log("🗑️ Recovery checkpoint deleted — next Start will begin fresh from -55°.")
        else:
            self.add_auto_log("💾 Recovery checkpoint kept — next Start will offer resume.")

    def show_stop_sequences_info(self):
        """Info dialog explaining Pause vs Abort sequences."""
        msg = (
            "■ ⏸ Pause / ⏯ Continue\n"
            "   - Waits for the current step to finish, then holds at the next checkpoint.\n"
            "   - Motors and DAQ are NOT killed; all progress is preserved.\n"
            "   - Press Continue to resume from exactly where it paused.\n\n"
            "■ ⚠️ Re-Run / Abort Scan\n"
            "   1. Halt scan loop immediately\n"
            "   2. Stop motors (SN2 · SN3)\n"
            "   3. Force-kill DAQ process\n"
            "   4. Reset UI\n"
            "   - A dialog will ask: [Yes] delete checkpoint (fresh start from -55°)\n"
            "     / [No] keep checkpoint (resume available at next Start)\n"
            "     / [Cancel] abort nothing.\n\n"
            "■ 🆕 Starting completely fresh\n"
            "   - In the Abort dialog choose [Yes] (deletes checkpoint), OR\n"
            "   - When 'Recovery Found' appears at Start, click [No] to discard it.\n\n"
            "■ 🔄 Reset angle\n"
            "   - Independently moves motors back to 0° (Tilt first, then Rot).\n\n"
            "Summary: use Pause to pause and resume; use Abort to stop completely."
        )
        messagebox.showinfo("Stop / Abort Guide", msg)

    def update_unlock_ui(self, is_unlocked):
        # Control Panel 의 Unlock 버튼은 제거되었고, 잠금 표시는 상단 안전 배너가
        # 담당한다. 하위 호환을 위해 메서드는 남기되, btn_unlock 이 있을 때만 갱신한다.
        btn = getattr(self, 'btn_unlock', None)
        if btn is None:
            return
        if is_unlocked:
            btn.config(text="🔓 Lock", bg="#28a745", fg="white")
        else:
            btn.config(text="🔒 Unlock", bg="#f0ad4e", fg="black")

    def lock_manual_panel(self, is_locked):
        """Secures the manual panel configuration items preventing runtime collision interferences."""
        state = tk.DISABLED if is_locked else tk.NORMAL
        
        if hasattr(self, 'manual_control_buttons'):
            for btn in self.manual_control_buttons:
                if btn.winfo_exists():
                    self.notebook.after(0, lambda b=btn: b.config(state=state))
        
        self.add_auto_log(f"Manual Override Panel structure state set to: {state.upper()}")


    def update_start_button(self, is_running, status_text=None):
        # This is called both from the GUI thread (button callbacks) and from the
        # scan worker thread (e.g. AutomationManager._run_thread's finally block).
        # Touching Tk widgets off the main thread can crash Tcl/Tk, so always apply
        # the widget changes on the main thread.
        def _apply():
            self.lock_manual_panel(is_running)
            if is_running:
                self.btn_start.config(text="⏳ RUNNING...", bg="#6c757d", state=tk.DISABLED)
                self.btn_stop_run.config(text="⏸ Pause", bg="#ffc107", fg="black", state=tk.NORMAL)
                self.btn_reset.config(state=tk.DISABLED)
                display_txt = status_text if status_text else "SYSTEM STATUS: SCANNING..."
                self.scan_status_label.config(text=display_txt, foreground="#dc3545")

                if hasattr(self.controller, 'ui') and 'run_daq' in self.controller.ui.buttons:
                    self.controller.ui.buttons['run_daq'].config(state=tk.DISABLED, text="2. Run DAQ (Scanning)")
            else:
                self.btn_start.config(text="▶ Start run", bg="#28a745", state=tk.NORMAL)
                self.btn_stop_run.config(text="⏸ Pause", bg="#ffc107", fg="black", state=tk.DISABLED)
                self.btn_reset.config(state=tk.NORMAL)
                self.scan_status_label.config(text="SYSTEM STATUS: IDLE", foreground="gray")

                if hasattr(self.controller, 'ui') and 'run_daq' in self.controller.ui.buttons:
                    if hasattr(self.controller, 'access_mgr') and self.controller.access_mgr.unlocked:
                        self.controller.ui.buttons['run_daq'].config(state=tk.NORMAL, text="2. Run DAQ")

        self.notebook.after(0, _apply)

    def update_sn_display(self, dev_num, tilt, rot):
        sn = None
        if dev_num == 2: sn = self.sn2_val
        elif dev_num == 3: sn = self.sn3_val
        
        if not sn: return

        t_str = f"{tilt:.1f}" if tilt is not None else "Err"
        r_str = f"{rot:.1f}" if rot is not None else "Err"

        if hasattr(self, 'sn_labels') and sn in self.sn_labels:
            # Guard against the widget being destroyed before this queued callback
            # fires (e.g. during app shutdown) → avoids TclError "invalid command name".
            tilt_locked = (tilt is not None and abs(tilt) > 0.5)

            def _apply_sn(sn=sn, t_str=t_str, r_str=r_str, dev=dev_num, locked=tilt_locked):
                if getattr(self.controller, '_shutting_down', False):
                    return
                try:
                    self.sn_labels[sn].config(
                        text=f"{sn} | Status -> Tilt: {t_str}°, Rot: {r_str}°")
                    # Enforce rotation interlock: disable Move Rot when tilt != 0
                    if dev in getattr(self, 'manual_rot_buttons', {}):
                        btn_rot, lock_lbl = self.manual_rot_buttons[dev]
                        if locked:
                            btn_rot.config(state=tk.DISABLED, bg="#6c757d")
                            lock_lbl.pack(side=tk.LEFT, padx=(6, 0))
                        else:
                            btn_rot.config(state=tk.NORMAL, bg="#17a2b8")
                            lock_lbl.pack_forget()
                except tk.TclError:
                    pass
            self.notebook.after(0, _apply_sn)

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

    def set_buttons_state(self, state):
        tk_state = tk.NORMAL if state else tk.DISABLED

        if state:
            colors = {
                "start": "#28a745", # Active Green
                "reset": "#f0ad4e", # Warning Orange
                "abort": "#6c757d", # Standard Grey
                "get": "#007ACC"    
            }
            fg_color = "white"
        else:
            colors = {
                "start": "#3a3a3a",
                "reset": "#3a3a3a",
                "abort": "#3a3a3a",
                "get": "#3a3a3a"
            }
            fg_color = "#777777" 

        self.btn_start.config(state=tk_state, bg=colors["start"], fg=fg_color)
        self.btn_reset.config(state=tk_state, bg=colors["reset"], fg=fg_color)
        self.btn_emg_stop.config(state=tk_state, bg=colors["abort"], fg=fg_color)

        if hasattr(self, 'get_current_btns'):
            for btn in self.get_current_btns:
                btn.config(state=tk_state, bg=colors["get"], fg=fg_color)

        self.add_auto_log(f"Control Panel {'Activated 🔓' if state else 'Standby 🔒'}")


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

    def _on_schedule_click(self):
        if self.btn_schedule.cget("text") == "⏰ Set":
            time_str = self.time_var.get()
            if hasattr(self.controller.auto_mgr, 'schedule_general_scan'):
                self.controller.auto_mgr.schedule_general_scan(time_str)
                if getattr(self.controller.auto_mgr, 'is_scheduled', False):
                    self.btn_schedule.config(text="Cancel", bg="#dc3545")
        else:
            if hasattr(self.controller.auto_mgr, 'cancel_schedule'):
                self.controller.auto_mgr.cancel_schedule()
            self.btn_schedule.config(text="⏰ Set", bg="#17a2b8")


    def start_eta_countdown(self, total_seconds, total_steps):
        self.remaining_eta_seconds = int(total_seconds)
        self.total_est_size_mb = total_steps * 800.0 
        self.update_eta_realtime()

    def update_eta_realtime(self):
        auto_mgr = getattr(self.controller, 'auto_mgr', None)

        if not auto_mgr or not auto_mgr.is_running:
            return

        # 일시정지(pause_event 가 clear) 상태면 ETA 를 멈춰서 표시한다.
        paused = not auto_mgr.pause_event.is_set()

        res = auto_mgr.get_eta_seconds() if hasattr(auto_mgr, 'get_eta_seconds') else None
        if res:
            eta, current, total = res
            m, s = divmod(int(eta), 60)
            h, m = divmod(m, 60)
            ts = f"{h:02d}:{m:02d}:{s:02d}"
            if paused:
                self.eta_label.config(text=f"⏸ Paused  |  ETA {ts}  ({current}/{total})")
            else:
                self.eta_label.config(text=f"ETA {ts}   ({current}/{total} steps)")

        self.notebook.after(1000, self.update_eta_realtime)

    def add_auto_log(self, message):
        JST = timezone(timedelta(hours=9))
        timestamp = datetime.now(JST).strftime("%H:%M:%S")
        
        self.log_display.config(state=tk.NORMAL)
        
        self.log_display.insert(tk.END, f"[{timestamp}] ", "TIME")
        
        tag = "NORMAL"
        upper_msg = message.upper()
        
        if any(keyword in upper_msg for keyword in ["ERROR", "FAIL", "CRITICAL", "🚨", "❌"]):
            tag = "ERROR"
        elif any(keyword in upper_msg for keyword in ["WARNING", "ALERT", "⚠️"]):
            tag = "WARNING"
        elif any(keyword in upper_msg for keyword in ["SUCCESS", "DONE", "COMPLETED", "✅"]):
            tag = "SUCCESS"
        elif any(keyword in upper_msg for keyword in ["INFO", "MOVE", "SCANNING", "SYNC", "▶"]):
            tag = "INFO"
            
        self.log_display.insert(tk.END, f"{message}\n", tag)

        if int(self.log_display.index('end-1c').split('.')[0]) > 1000:
            self.log_display.delete('1.0', '100.0')

        self.log_display.config(state=tk.DISABLED)
        self.log_display.see(tk.END)

    # ====================================================================
    # [NEW] Schedule Manager 탭 빌드
    # ====================================================================
    def _build_schedule_tab(self, parent):
        try:
            from tkcalendar import DateEntry
            self.has_calendar = True
        except ImportError:
            self.has_calendar = False

        top_frame = ttk.Frame(parent)
        top_frame.pack(fill=tk.X, pady=10)

        ttk.Label(top_frame, text="Date:", font=("Helvetica", 11, "bold")).pack(side=tk.LEFT, padx=(10, 5))
        if self.has_calendar:
            from tkcalendar import DateEntry
            self.date_picker = DateEntry(top_frame, width=12, background='darkblue', 
                                         foreground='white', borderwidth=2, date_pattern='yyyy-mm-dd')
            self.date_picker.pack(side=tk.LEFT, padx=5)
        else:
            self.date_entry = ttk.Entry(top_frame, width=12)
            self.date_entry.insert(0, datetime.now().strftime("%Y-%m-%d"))
            self.date_entry.pack(side=tk.LEFT, padx=5)
            ttk.Label(top_frame, text="(YYYY-MM-DD)", font=("Helvetica", 8), foreground="gray").pack(side=tk.LEFT)

        ttk.Label(top_frame, text="Time (JST):", font=("Helvetica", 11, "bold")).pack(side=tk.LEFT, padx=(20, 5))
        
        self.sch_hour = tk.StringVar(value=datetime.now().strftime("%H"))
        self.sch_min = tk.StringVar(value="00")
        
        tk.Entry(top_frame, textvariable=self.sch_hour, width=3, font=("Helvetica", 12, "bold"), justify="center").pack(side=tk.LEFT)
        tk.Label(top_frame, text=":", font=("Helvetica", 12, "bold")).pack(side=tk.LEFT)
        tk.Entry(top_frame, textvariable=self.sch_min, width=3, font=("Helvetica", 12, "bold"), justify="center").pack(side=tk.LEFT)

        ttk.Button(top_frame, text="⏰ Add Schedule", command=self._add_schedule_click).pack(side=tk.LEFT, padx=15)
        ttk.Button(top_frame, text="🗑️ Cancel Selected", command=self._cancel_schedule_click).pack(side=tk.LEFT)

        content_pane = ttk.PanedWindow(parent, orient=tk.HORIZONTAL)
        content_pane.pack(fill=tk.BOTH, expand=True, pady=10)

        list_frame = ttk.LabelFrame(content_pane, text=" Queued Schedules (Max 3) ", padding=5)
        content_pane.add(list_frame, weight=1)

        self.schedule_tree = ttk.Treeview(list_frame, columns=("Time", "Status"), show="headings", height=5)
        self.schedule_tree.heading("Time", text="Target Time (JST)")
        self.schedule_tree.heading("Status", text="Status")
        self.schedule_tree.column("Time", width=150, anchor="center")
        self.schedule_tree.column("Status", width=100, anchor="center")
        self.schedule_tree.pack(fill=tk.BOTH, expand=True)
        self.schedule_tree.bind("<<TreeviewSelect>>", self._on_schedule_select)

        detail_frame = ttk.LabelFrame(content_pane, text=" Live Configuration Preview ", padding=5)
        content_pane.add(detail_frame, weight=2)
        
        self.schedule_detail_text = scrolledtext.ScrolledText(detail_frame, font=("Consolas", 10), state=tk.DISABLED, bg="#1e1e1e", fg="#e0e0e0")
        self.schedule_detail_text.pack(fill=tk.BOTH, expand=True)

    def _add_schedule_click(self):
        date_str = self.date_picker.get() if self.has_calendar else self.date_entry.get()
        h, m = self.sch_hour.get(), self.sch_min.get()
        
        success = self.controller.auto_mgr.add_schedule(date_str, h, m)
        if success:
            self.refresh_schedule_list()

    def refresh_schedule_list(self):
        for item in self.schedule_tree.get_children():
            self.schedule_tree.delete(item)
        for s in self.controller.auto_mgr.schedules:
            self.schedule_tree.insert("", tk.END, values=(f"{s['time_str']}", "WAITING"))

    def _cancel_schedule_click(self):
        selected = self.schedule_tree.selection()
        if not selected: return
        index = self.schedule_tree.index(selected[0])
        self.controller.auto_mgr.remove_schedule(index)
        self.refresh_schedule_list()
        self.schedule_detail_text.config(state=tk.NORMAL)
        self.schedule_detail_text.delete('1.0', tk.END)
        self.schedule_detail_text.config(state=tk.DISABLED)

    def _on_schedule_select(self, event):
        selected = self.schedule_tree.selection()
        if not selected: return
        index = self.schedule_tree.index(selected[0])
        cfg = self.controller.auto_mgr.schedules[index]["config"]
       
        current_cfg = self.controller.config_manager.get_all_variables()
        display_text = f"=== Saved Configuration for {self.controller.auto_mgr.schedules[index]['time_str']} ===\n\n"
        for k, v in cfg.items():
            display_text += f"{k}: {v}\n"
            
        self.schedule_detail_text.config(state=tk.NORMAL)
        self.schedule_detail_text.delete('1.0', tk.END)
        self.schedule_detail_text.insert(tk.END, display_text)
        self.schedule_detail_text.config(state=tk.DISABLED)

    # ====================================================================
    # [NEW] Scan History 탭 빌드
    # ====================================================================
    def _build_history_tab(self, parent):
        content_pane = ttk.PanedWindow(parent, orient=tk.HORIZONTAL)
        content_pane.pack(fill=tk.BOTH, expand=True)

        list_frame = ttk.LabelFrame(content_pane, text=" Past General Scans ", padding=5)
        content_pane.add(list_frame, weight=1)

        self.history_tree = ttk.Treeview(list_frame, columns=("Date", "Time", "Shifter", "Result"), show="headings")
        self.history_tree.heading("Date", text="Date (JST)")
        self.history_tree.heading("Time", text="End Time")
        self.history_tree.heading("Shifter", text="Shifter")
        self.history_tree.heading("Result", text="Result")
        self.history_tree.column("Date", width=100, anchor="center")
        self.history_tree.column("Time", width=80, anchor="center")
        self.history_tree.column("Shifter", width=100, anchor="center")
        self.history_tree.column("Result", width=80, anchor="center")
        self.history_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self.history_tree.yview)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.history_tree.configure(yscrollcommand=vsb.set)
        
        self.history_tree.bind("<<TreeviewSelect>>", self._on_history_select)

        detail_frame = ttk.LabelFrame(content_pane, text=" Run Details & Configuration ", padding=5)
        content_pane.add(detail_frame, weight=2)
        
        self.history_detail_text = scrolledtext.ScrolledText(detail_frame, font=("Consolas", 10), state=tk.DISABLED, bg="#1e1e1e", fg="#e0e0e0")
        self.history_detail_text.pack(fill=tk.BOTH, expand=True)

        ttk.Button(parent, text="🔄 Refresh History", command=self.refresh_history_list).pack(pady=5)
        
        # 최초 1회 자동으로 데이터 불러오기
        self.notebook.after(500, self.refresh_history_list)

    def refresh_history_list(self):
        import os
        import glob
        import json
        for item in self.history_tree.get_children():
            self.history_tree.delete(item)
            
        history_dir = os.path.join(self.controller.base_dir, "LOG", "ScanHistory")
        if not os.path.exists(history_dir): return
        
        files = glob.glob(os.path.join(history_dir, "*.json"))
        files.sort(reverse=True) # 최신순
        
        for f in files:
            try:
                with open(f, 'r', encoding='utf-8') as json_file:
                    data = json.load(json_file)
                    self.history_tree.insert("", tk.END, values=(data.get("date"), data.get("end_time"), data.get("shifter"), data.get("status")), tags=(f,))
            except Exception: pass

    def _on_history_select(self, event):
        import json
        selected = self.history_tree.selection()
        if not selected: return
        
        file_path = self.history_tree.item(selected[0], "tags")[0]
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                
            display_text = f"=== Scan Finished at {data.get('date')} {data.get('end_time')} ===\n"
            display_text += f"Shifter: {data.get('shifter')}\nStatus: {data.get('status')}\n"
            display_text += "-"*50 + "\n[ Configuration Snapshot ]\n"
            
            for k, v in data.get("config", {}).items():
                display_text += f"{k}: {v}\n"
                
            self.history_detail_text.config(state=tk.NORMAL)
            self.history_detail_text.delete('1.0', tk.END)
            self.history_detail_text.insert(tk.END, display_text)
            self.history_detail_text.config(state=tk.DISABLED)
        except Exception:
            pass


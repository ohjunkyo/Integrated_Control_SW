# managers/ui_automation.py
import os
import re
import json
import glob
import subprocess
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from datetime import datetime, timezone, timedelta

# customtkinter migration (in progress). Guarded so the app still runs if the
# package is somehow missing -- callers check CTK_AVAILABLE before using ctk.
try:
    import customtkinter as ctk
    CTK_AVAILABLE = True
except Exception:
    ctk = None
    CTK_AVAILABLE = False

# Cable pin position (deg), mirrors angle_convert.h::PosMapAngle exactly.
# Keep these two in sync -- this is the single verified sign source.
_POS_MAP_ANGLE = {'E': 0, 'F': 45, 'G': 90, 'H': 135,
                   'A': 180, 'B': 225, 'C': 270, 'D': 315}


def _xy_rot_for_direction(cable):
    """Port of angle_convert.h::GetXYRotForDirection."""
    pm = _POS_MAP_ANGLE.get(cable.upper(), 180)
    xm = ((pm - 90) % 180 + 180) % 180 - 90
    ym = ((pm - 180) % 180 + 180) % 180 - 90
    x_rot = xm + 180 if xm < 0 else xm
    y_rot = ym + 180 if ym < 0 else ym
    return x_rot, y_rot


def injection_side_label(cable, rot, tilt):
    """Which physical cathode side (X+/X-/Y+/Y-) the laser is currently hitting.

    Port of angle_convert.h::GetHamamatsuAngle's sign logic (verified against the
    R12860-22 bottom-view + rotation-system diagrams for all 8 cable directions).
    Returns "" if rot doesn't match either scan axis for this cable, or tilt≈0.
    """
    if not cable or rot is None or tilt is None:
        return ""
    cable = cable.upper()
    if cable not in _POS_MAP_ANGLE or abs(tilt) < 0.5:
        return ""
    x_rot, y_rot = _xy_rot_for_direction(cable)
    rot_i = round(rot)
    is_x = (rot_i == x_rot)
    is_y = (rot_i == y_rot)
    if not (is_x or is_y):
        return ""

    delta = ((180 - _POS_MAP_ANGLE[cable]) % 360 + 360) % 360
    if is_x:
        xflip = (delta + x_rot) % 360 == 180
        side = "X-" if (xflip == (tilt > 0)) else "X+"
    else:
        yflip = (delta + y_rot) % 360 == 270
        side = "Y+" if (yflip == (tilt > 0)) else "Y-"
    return side


class AutomationUI:
    # ── Centralized visual palette ───────────────────────────────────────────
    # One source of truth for the DAQ panel's colors/fonts so buttons, cards
    # and pills read as one system instead of each widget picking its own
    # ad-hoc hex. Semantic keys (not raw colors) so intent stays obvious at
    # the call site: PALETTE["start"], PALETTE["danger"], etc.
    PALETTE = {
        "bg":          "#f4f5f7",   # panel canvas
        "card":        "#ffffff",   # card surface
        "border":      "#d9dce1",   # hairline
        "text":        "#1f2430",   # primary text
        "text_muted":  "#6c757d",   # secondary/labels
        "accent":      "#007ACC",   # values / highlights
        "start":       "#2e9e4f",   # go / start
        "warn":        "#e8a317",   # pause / stop-motion (amber)
        "danger":      "#d9534f",   # abort / error (red)
        "move":        "#2f86c9",   # manual move (blue)
        "neutral":     "#6c757d",   # reset / secondary
    }
    def __init__(self, notebook, controller):
        self.notebook = notebook
        self.controller = controller
        self.dummy_var = tk.BooleanVar(value=False)
        self.scan_mode_var = tk.StringVar(value="laser")
        self.laser_seq_widgets = []
        self.cells = {}
        self.manual_vars = {}
        self._create_tab()
        self.notebook.after(1500, lambda: self.sync_current_to_inputs(self.sn2_val))
        self.notebook.after(1500, lambda: self.sync_current_to_inputs(self.sn3_val))
        self.refresh_schedule_list()

    def _style_button(self, btn, kind):
        """Apply a semantic color to a tk.Button in-place. `kind` is a
        PALETTE key (start/warn/danger/move/neutral). White text on the
        strong fills, dark text on amber for contrast."""
        bg = self.PALETTE.get(kind, self.PALETTE["neutral"])
        fg = "#412402" if kind == "warn" else "white"
        btn.config(bg=bg, fg=fg, relief="flat", activebackground=bg,
                   activeforeground=fg, bd=0, highlightthickness=0)

    def _create_tab(self):
        self.tab = ttk.Frame(self.notebook)
        self.notebook.add(self.tab, text=" General Scan ")

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
        self.upper_notebook.add(info_tab, text=" Quick Setup ")

        self.qs_vars = {
                "Shift_worker": tk.StringVar(), "Expert": tk.StringVar(), "NOTE": tk.StringVar(),
                "Laser": tk.StringVar(), "Wavelength": tk.StringVar(), "BField": tk.StringVar(),
                "BField_X": tk.StringVar(), "BField_Y": tk.StringVar(),
                "BField_ZTop": tk.StringVar(), "BField_ZBottom": tk.StringVar(),
            "SN1": tk.StringVar(), "HV1": tk.StringVar(), "direction1": tk.StringVar(),
            "RotateAngle1": tk.StringVar(), "TiltAngle1": tk.StringVar(),
            "SN2": tk.StringVar(), "HV2": tk.StringVar(), "direction2": tk.StringVar(),
            "RotateAngle2": tk.StringVar(), "TiltAngle2": tk.StringVar(),
            "SN3": tk.StringVar(), "HV3": tk.StringVar(), "direction3": tk.StringVar(),
            "RotateAngle3": tk.StringVar(), "TiltAngle3": tk.StringVar(),
        }

        if CTK_AVAILABLE:
            ctk.set_appearance_mode("light"); ctk.set_default_color_theme("blue")
            setup_frame = ctk.CTkFrame(info_tab)
            setup_frame.pack(fill=tk.X)
            ctk.CTkLabel(setup_frame, text="⚙️  Quick Configuration (Edit & Save)",
                         font=ctk.CTkFont(size=15, weight="bold")).pack(anchor="w", padx=14, pady=(12, 4))

            def make_row(parent, row_idx, items):
                frame = ctk.CTkFrame(parent, fg_color="transparent")
                frame.pack(fill=tk.X, padx=12, pady=6)
                for i, (label_text, var_key) in enumerate(items):
                    ctk.CTkLabel(frame, text=label_text, width=70, anchor="e",
                                 font=ctk.CTkFont(size=12, weight="bold")).pack(side=tk.LEFT, padx=(12 if i > 0 else 0, 5))
                    if var_key == "BField":
                        ctk.CTkOptionMenu(frame, variable=self.qs_vars[var_key],
                                          values=["ON", "OFF"], width=80).pack(side=tk.LEFT)
                    else:
                        ctk.CTkEntry(frame, textvariable=self.qs_vars[var_key],
                                     width=110, justify="center").pack(side=tk.LEFT)

            make_row(setup_frame, 0, [("Shifter:", "Shift_worker"), ("Expert:", "Expert"),
                                      ("Laser:", "Laser"), ("Wavelength:", "Wavelength"),
                                      ("B-field:", "BField"), ("Note:", "NOTE")])
            make_row(setup_frame, 1, [("X(A):", "BField_X"), ("Y(A):", "BField_Y"),
                                      ("Z-Top(A):", "BField_ZTop"), ("Z-Bottom(A):", "BField_ZBottom")])
            # thin divider
            ctk.CTkFrame(setup_frame, height=1, fg_color="#d3d7dd").pack(fill=tk.X, padx=12, pady=8)
            make_row(setup_frame, 2, [("SN1:", "SN1"), ("Dir(A~H):", "direction1"), ("Rot(°):", "RotateAngle1"), ("Tilt(°):", "TiltAngle1"), ("HV1(V):", "HV1")])
            make_row(setup_frame, 3, [("SN2:", "SN2"), ("Dir(A~H):", "direction2"), ("Rot(°):", "RotateAngle2"), ("Tilt(°):", "TiltAngle2"), ("HV2(V):", "HV2")])
            make_row(setup_frame, 4, [("SN3:", "SN3"), ("Dir(A~H):", "direction3"), ("Rot(°):", "RotateAngle3"), ("Tilt(°):", "TiltAngle3"), ("HV3(V):", "HV3")])
            # bottom padding inside the card
            ctk.CTkFrame(setup_frame, height=4, fg_color="transparent").pack()

            btn_frame = ctk.CTkFrame(info_tab, fg_color="transparent")
            btn_frame.pack(fill=tk.X, pady=(14, 0))
            btn_frame.columnconfigure(0, weight=1)
            btn_frame.columnconfigure(1, weight=1)
            ctk.CTkButton(btn_frame, text="⚙️ Open Global Config (Paths)", height=40,
                          fg_color="#6c757d", hover_color="#5a6268",
                          command=self.controller.open_config).grid(row=0, column=0, sticky="ew", padx=5)
            ctk.CTkButton(btn_frame, text="💾 Save Settings", height=40,
                          fg_color="#2e9e4f", hover_color="#268043",
                          command=self.save_quick_setup).grid(row=0, column=1, sticky="ew", padx=5)
        else:
            setup_frame = ttk.LabelFrame(info_tab, text=" ⚙️ Quick Configuration (Edit & Save) ", padding=15)
            setup_frame.pack(fill=tk.BOTH, expand=True)
            entry_font = ("Helvetica", 12, "bold")
            lbl_font = ("Helvetica", 11, "bold")

            def make_row(parent, row_idx, items):
                frame = tk.Frame(parent)
                frame.pack(fill=tk.X, pady=8)
                for i, (label_text, var_key) in enumerate(items):
                    tk.Label(frame, text=label_text, font=lbl_font, width=8, anchor="e").pack(side=tk.LEFT, padx=(10 if i > 0 else 0, 5))
                    if var_key == "BField":
                        ttk.Combobox(frame, textvariable=self.qs_vars[var_key], font=entry_font,
                                     width=6, justify="center", state="readonly", values=("ON", "OFF")).pack(side=tk.LEFT)
                    else:
                        tk.Entry(frame, textvariable=self.qs_vars[var_key], font=entry_font, width=12, justify="center").pack(side=tk.LEFT)

            make_row(setup_frame, 0, [("Shifter:", "Shift_worker"), ("Expert:", "Expert"),
                                      ("Laser:", "Laser"), ("Wavelength:", "Wavelength"),
                                      ("B-field:", "BField"), ("Note:", "NOTE")])
            make_row(setup_frame, 1, [("X(A):", "BField_X"), ("Y(A):", "BField_Y"),
                                      ("Z-Top(A):", "BField_ZTop"), ("Z-Bottom(A):", "BField_ZBottom")])
            ttk.Separator(setup_frame, orient="horizontal").pack(fill=tk.X, pady=10)
            make_row(setup_frame, 2, [("SN1:", "SN1"), ("Dir(A~H):", "direction1"), ("Rot(°):", "RotateAngle1"), ("Tilt(°):", "TiltAngle1"), ("HV1(V):", "HV1")])
            make_row(setup_frame, 3, [("SN2:", "SN2"), ("Dir(A~H):", "direction2"), ("Rot(°):", "RotateAngle2"), ("Tilt(°):", "TiltAngle2"), ("HV2(V):", "HV2")])
            make_row(setup_frame, 4, [("SN3:", "SN3"), ("Dir(A~H):", "direction3"), ("Rot(°):", "RotateAngle3"), ("Tilt(°):", "TiltAngle3"), ("HV3(V):", "HV3")])

            btn_frame = tk.Frame(info_tab)
            btn_frame.pack(fill=tk.X, pady=(15, 0))
            btn_frame.columnconfigure(0, weight=1)
            btn_frame.columnconfigure(1, weight=1)
            tk.Button(btn_frame, text="⚙️ Open Global Config (Paths)", bg="#6c757d", fg="white", font=("Helvetica", 12, "bold"),
                      height=2, command=self.controller.open_config).grid(row=0, column=0, sticky="ew", padx=5)
            tk.Button(btn_frame, text="💾 Save Settings", bg="#28a745", fg="white", font=("Helvetica", 12, "bold"),
                      height=2, command=self.save_quick_setup).grid(row=0, column=1, sticky="ew", padx=5)

        self._create_handover_notes(info_tab)

        dash_tab = ttk.Frame(self.upper_notebook, padding=10)
        self.upper_notebook.add(dash_tab, text=" Control Panel (Master) ")

        dash_tab.columnconfigure(0, weight=6)
        dash_tab.columnconfigure(1, weight=4)
        # Scan Progress Matrix moved out to its own tab (see self.matrix_tab
        # below), so this is now the only row -- let it take the full tab.
        dash_tab.rowconfigure(0, weight=1)

        left_ctrl = ttk.LabelFrame(dash_tab, text=" ⚙️ Operation Controls ", padding=8)
        left_ctrl.grid(row=0, column=0, sticky="nsew", padx=(0, 10))

        for c in range(3): left_ctrl.columnconfigure(c, weight=1)
        # Content rows sit at their natural height; a trailing spacer row (5)
        # absorbs the leftover vertical space. Previously every row had
        # weight=1, which stretched them all -- inflating the Danger Zone and
        # its Re-Run button to fill the whole panel height.
        for r in range(5): left_ctrl.rowconfigure(r, weight=0)
        left_ctrl.rowconfigure(5, weight=1)

        # Header row: TEST RUN toggle (left) and SYSTEM STATUS (centered).
        # Previously both sat in row=0 with the checkbox spanning all 3
        # columns AND the status label placed at column=2 of that same row
        # -- an actual grid overlap, not just a visual crowding issue.
        self.dummy_chk = tk.Checkbutton(left_ctrl, text="🧪 TEST RUN (Simulation Mode)",
                                variable=self.dummy_var, font=("Helvetica", 10), fg="#007ACC")
        self.dummy_chk.grid(row=0, column=0, padx=8, pady=(2, 4), sticky="w")

        self.scan_status_label = ttk.Label(left_ctrl, text="SYSTEM STATUS: IDLE",
                                          font=("Helvetica", 14, "bold"), foreground="gray")
        self.scan_status_label.grid(row=0, column=1, pady=(2, 4))

        # [중복 제거] 기존엔 여기에도 Unlock 버튼이 있었으나, 이제 잠금/해제는
        # DAQ 탭 상단의 안전 배너(_create_lock_banner)가 단일 소스로 담당한다.
        # 남은 공간은 Start/Stop 이 채우도록 재배치한다.
        # ── 그룹 1: 주 제어 (Start / Pause) + Reset ─────────────────────────
        primary = ttk.LabelFrame(left_ctrl, text=" ▶ Run Control ", padding=8)
        primary.grid(row=1, column=0, columnspan=3, sticky="nsew", padx=4, pady=(0, 6))
        primary.columnconfigure(0, weight=3)
        primary.columnconfigure(1, weight=3)
        primary.columnconfigure(2, weight=2)

        # All three share one font/height so they read as one uniform button
        # row (Reset was previously font 10 while the others were 12, which
        # made it look shrunken).
        _RUN_BTN = dict(font=("Helvetica", 12, "bold"), height=2)
        self.btn_start = tk.Button(primary, text="▶ Start run", **_RUN_BTN,
                                   command=self.controller.auto_mgr.start_general_scan)
        self._style_button(self.btn_start, "start")
        self.btn_start.grid(row=0, column=0, padx=4, pady=3, sticky="nsew")

        # Pause/Continue 토글(진행상황 보존). 속성명은 호환을 위해 btn_stop_run 유지.
        self.btn_stop_run = tk.Button(primary, text="⏸ Pause", **_RUN_BTN,
                                      command=self.controller.auto_mgr.handle_stop_continue)
        self._style_button(self.btn_stop_run, "warn")
        self.btn_stop_run.grid(row=0, column=1, padx=4, pady=3, sticky="nsew")

        self.btn_reset = tk.Button(primary, text="🔄 Reset", **_RUN_BTN,
                                   command=self.confirm_and_reset_angles)
        self._style_button(self.btn_reset, "neutral")
        self.btn_reset.grid(row=0, column=2, padx=4, pady=3, sticky="nsew")

        # ── DAQ backend selector: which digitizer takes the data ────────────
        # Korean DAQ (CAEN) is the existing/default path (config3.h-driven).
        # HK Digitizer is the 2nd-PC path -- rotation-synced, and the existing
        # config3.h is NOT used in that mode. Styled as a big segmented
        # toggle (not a plain radio row) since which backend is armed is a
        # high-stakes choice worth being impossible to miss at a glance.
        self.daq_backend_var = tk.StringVar(
            value=getattr(self.controller.auto_mgr, "daq_backend", "caen"))

        backend_card = ttk.Frame(primary)
        backend_card.grid(row=1, column=0, columnspan=3, sticky="ew", padx=4, pady=(8, 0))
        backend_card.columnconfigure((0, 1), weight=1)
        backend_hdr = ttk.Frame(backend_card)
        backend_hdr.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 3))
        ttk.Label(backend_hdr, text="DAQ BACKEND", font=("Helvetica", 9, "bold"),
                  foreground=self.PALETTE["text_muted"]).pack(side=tk.LEFT)
        self.daq_backend_lock_note = ttk.Label(backend_hdr, text="", font=("Helvetica", 9, "bold"),
                                               foreground=self.PALETTE["danger"])
        self.daq_backend_lock_note.pack(side=tk.LEFT, padx=(10, 0))

        self._daq_backend_buttons = {}
        self._daq_backend_locked = False   # True while a scan is running

        def _on_backend_click(value):
            # Switching which digitizer takes the data mid-scan would be a
            # dangerous no-op at best (the running scan already committed to
            # a backend) and confusing at worst -- ignore clicks while locked
            # instead of just hoping the operator doesn't touch it.
            if self._daq_backend_locked:
                return
            self._select_daq_backend(value)

        def _make_backend_btn(value, label, sub):
            f = tk.Frame(backend_card, bd=0, highlightthickness=2)
            btn = tk.Label(f, text=f"{label}\n", font=("Helvetica", 12, "bold"),
                           cursor="hand2", justify="center")
            sub_lbl = tk.Label(f, text=sub, font=("Helvetica", 8), cursor="hand2",
                               justify="center")
            btn.pack(fill=tk.X, pady=(8, 0))
            sub_lbl.pack(fill=tk.X, pady=(0, 8))
            for w in (f, btn, sub_lbl):
                w.bind("<Button-1>", lambda e, v=value: _on_backend_click(v))
            self._daq_backend_buttons[value] = (f, btn, sub_lbl)
            return f

        _make_backend_btn("caen", "🇰🇷 Korean DAQ (CAEN)", "local config3.h").grid(
            row=1, column=0, sticky="nsew", padx=(0, 4))
        _make_backend_btn("hk", "🖧 HK Digitizer", "2nd PC — rotation-synced").grid(
            row=1, column=1, sticky="nsew", padx=(4, 0))

        self.daq_backend_note = ttk.Label(primary, text="", font=("Helvetica", 9, "bold"))
        self.daq_backend_note.grid(row=2, column=0, columnspan=3, sticky="w", padx=4, pady=(4, 0))
        self._select_daq_backend(self.daq_backend_var.get())   # paint initial state

        # ── ETA (강조) ──────────────────────────────────────────────────────
        self.eta_label = ttk.Label(left_ctrl, text="ETA: --:--:--",
                                   font=("Helvetica", 15, "bold"),
                                   foreground=self.PALETTE["accent"], anchor="center")
        self.eta_label.grid(row=2, column=0, columnspan=3, pady=(4, 6), sticky="nsew")

        # Scan Params (Tilt/Rot step, Rest time) used to have a plain read-only
        # label right here. It's now shown live next to the (shrunk) Laser
        # Sequence panel in the Manual Control Panel column instead -- see
        # self.current_params_labels below -- so this row is freed up.
        ttk.Separator(left_ctrl, orient="horizontal").grid(
            row=3, column=0, columnspan=3, sticky="ew", pady=4)

        # ── HK Digitizer configuration button (2nd-PC data path) ────────────
        # Opens the HK Configuration dialog (run #, angle, acq time, threshold,
        # target IP, script path…). Only meaningful when DAQ = HK Digitizer.
        self.btn_hk_config = tk.Button(left_ctrl, text="🖧 HK Digitizer Config",
                                       font=("Helvetica", 11, "bold"), height=2,
                                       command=self.open_hk_config)
        self._style_button(self.btn_hk_config, "move")
        self.btn_hk_config.grid(row=4, column=0, columnspan=3, sticky="ew", padx=4, pady=(0, 6))
        # Enabled only in HK mode (the radio default is CAEN → start disabled).
        self.btn_hk_config.config(
            state=(tk.NORMAL if self.daq_backend_var.get() == "hk" else tk.DISABLED))

        # ── 그룹 2: 위험 구역 (Abort) + 안내/Params ─────────────────────────
        danger = ttk.LabelFrame(left_ctrl, text=" 🛑 Danger Zone ", padding=8)
        danger.grid(row=5, column=0, columnspan=3, sticky="nsew", padx=4, pady=(2, 0))
        danger.columnconfigure(0, weight=1)

        self.btn_emg_stop = tk.Button(danger, text="⚠️ Re-Run / Abort Scan",
                                      font=("Helvetica", 12, "bold"), height=2,
                                      command=self.confirm_abort)
        self._style_button(self.btn_emg_stop, "danger")
        self.btn_emg_stop.grid(row=0, column=0, sticky="new", padx=(0, 6))

        side_btns = ttk.Frame(danger)
        side_btns.grid(row=0, column=1, sticky="new")
        tk.Button(side_btns, text="ⓘ Stop / Abort Guide", command=self.show_stop_sequences_info,
                  bg="#e9ecef", fg="#333", font=("Helvetica", 9, "bold"),
                  relief="flat").pack(fill=tk.X, pady=(0, 4))
        # Enabled unconditionally: open_scan_params() itself gates on
        # access_mgr.unlocked (prompting for the admin password if needed),
        # so the button doesn't need to be pre-disabled here too.
        # Given a taller footprint (height + ipady) so it reads as a primary
        # action, not a cramped afterthought next to the guide button.
        self.btn_scan_settings = tk.Button(side_btns, text="⚙️ Params", command=self.open_scan_params,
                                           font=("Helvetica", 11, "bold"), height=2)
        self._style_button(self.btn_scan_settings, "warn")
        self.btn_scan_settings.pack(fill=tk.X, ipady=4)

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
                                font=("Helvetica", 9, "bold"),
                                overrelief="raised")
            self._style_button(btn_get, "accent")
            btn_get.pack(side=tk.LEFT, padx=5)

            btn_tilt = tk.Button(btn_f, text="↕️ Move Tilt", font=("Helvetica", 10, "bold"), width=12,
                      command=lambda d=idx+2, s=sn: self._move_and_auto_sync(d, s, self.manual_vars[s][0].get(), "tilt"))
            self._style_button(btn_tilt, "move")
            btn_tilt.pack(side=tk.LEFT, padx=5)
            self.manual_control_buttons.append(btn_tilt)

            btn_rot = tk.Button(btn_f, text="🔄 Move Rot", font=("Helvetica", 10, "bold"), width=12,
                      command=lambda d=idx+2, s=sn: self._move_and_auto_sync(d, s, self.manual_vars[s][1].get(), "rot"))
            self._style_button(btn_rot, "move")
            btn_rot.pack(side=tk.LEFT, padx=5)
            self.manual_control_buttons.append(btn_rot)

            btn_stop = tk.Button(btn_f, text="⏹ Stop", font=("Helvetica", 10, "bold"), width=10,
                      command=lambda d=idx+2: self.controller.rot_mgr.stop_rotation(d))
            self._style_button(btn_stop, "warn")
            btn_stop.pack(side=tk.LEFT, padx=5)

            # Interlock warning label — shown when tilt != 0
            lock_lbl = tk.Label(btn_f, text="🔒 Tilt to 0° before rotating",
                                fg="#dc3545", bg=btn_f.winfo_toplevel().cget("bg"),
                                font=("Helvetica", 9, "bold"))
            lock_lbl.pack(side=tk.LEFT, padx=(6, 0))
            lock_lbl.pack_forget()   # hidden by default

            self.manual_rot_buttons[idx + 2] = (btn_rot, lock_lbl)

        # ── Laser Sequence (multi-wavelength scan) ─────────────────────────
        # Placed under the Manual Control Panel (right column) so the left
        # controls stay compact and the Scan Progress Matrix keeps its height.
        # Checked wavelengths are scanned as sequential full-scan blocks in
        # fixed order 405→375→450→473. Bias/Pulse are applied per block; a
        # disconnected laser (red dot) is skipped automatically.
        ttk.Separator(right_status, orient="horizontal").pack(fill=tk.X, pady=(10, 6))

        # Laser Sequence (top) and the read-only Current Scan Parameters panel
        # (bottom) are now STACKED, not side by side -- the params panel was
        # wider than the sequence and the two competing for one row looked
        # unbalanced. Full width each way lets the sequence use readable font
        # sizes again. The params panel is display-only by design -- actually
        # changing Tilt/Rot step etc. still requires Danger Zone -> Params
        # (admin-locked).
        laser_seq = ttk.LabelFrame(right_status, text=" 🔦 Laser Sequence ", padding=8)
        laser_seq.pack(fill=tk.X)

        # ── Scan Mode (Laser multi-λ vs Dark single scan) now lives INSIDE the
        # Laser Sequence box as its header row -- previously a small separate
        # row above the box that was easy to miss. Dark mode grays out the
        # wavelength rows below (no laser blocks apply). Distinct from the
        # shared rb_laser/rb_dark radios in the Run Mode panel (force-disabled
        # while General Scan is active).
        mode_row = ttk.Frame(laser_seq)
        mode_row.grid(row=0, column=0, columnspan=6, sticky="w", pady=(0, 6))
        ttk.Label(mode_row, text="Scan Mode:", font=("Helvetica", 10, "bold")).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Radiobutton(mode_row, text="Laser (multi-λ)", variable=self.scan_mode_var,
                        value="laser", command=self._on_scan_mode_change).pack(side=tk.LEFT, padx=(0, 14))
        ttk.Radiobutton(mode_row, text="Dark (single scan)", variable=self.scan_mode_var,
                        value="dark", command=self._on_scan_mode_change).pack(side=tk.LEFT)

        self.laser_seq_vars = {}
        self.laser_seq_dots = {}
        for r_i, wl in enumerate(["405nm", "375nm", "450nm", "473nm"]):
            grow = r_i + 1   # row 0 is the Scan Mode header
            on_var = tk.BooleanVar(value=(wl == "405nm"))
            bias_var, pulse_var = tk.StringVar(value="0.0"), tk.StringVar(value="0.0")
            self.laser_seq_vars[wl] = {"on": on_var, "bias": bias_var, "pulse": pulse_var}

            cb = tk.Checkbutton(laser_seq, text=wl, variable=on_var,
                           font=("Helvetica", 10, "bold"))
            cb.grid(row=grow, column=0, sticky="w", pady=2)
            ttk.Label(laser_seq, text="Bias", font=("Helvetica", 9)).grid(row=grow, column=1, sticky="e", padx=(10, 2))
            e1 = tk.Entry(laser_seq, textvariable=bias_var, width=7, justify="center", font=("Helvetica", 10))
            e1.grid(row=grow, column=2, sticky="w")
            ttk.Label(laser_seq, text="Pulse", font=("Helvetica", 9)).grid(row=grow, column=3, sticky="e", padx=(12, 2))
            e2 = tk.Entry(laser_seq, textvariable=pulse_var, width=7, justify="center", font=("Helvetica", 10))
            e2.grid(row=grow, column=4, sticky="w")
            dot = tk.Label(laser_seq, text="●", fg="#adb5bd", font=("Helvetica", 12, "bold"))
            dot.grid(row=grow, column=5, sticky="e", padx=(12, 2))
            self.laser_seq_dots[wl] = dot
            self.laser_seq_widgets.extend([cb, e1, e2])

        # Save row: status text on the left, the Save button hard right.
        self.laser_seq_save_lbl = tk.Label(laser_seq, text="", font=("Helvetica", 8),
                                            fg="#2b8a3e")
        self.laser_seq_save_lbl.grid(row=5, column=0, columnspan=4, sticky="w", pady=(8, 0))
        # Colored (not the pale flat ttk) so it actually reads as a button.
        laser_seq_save_btn = tk.Button(laser_seq, text="💾 Save Sequence",
                   font=("Helvetica", 10, "bold"),
                   command=self._on_laser_seq_save_click)
        self._style_button(laser_seq_save_btn, "start")
        laser_seq_save_btn.grid(row=5, column=4, columnspan=2, sticky="e", pady=(8, 0), ipadx=6, ipady=2)
        self.laser_seq_widgets.append(laser_seq_save_btn)

        # ── Current Scan Parameters (read-only), stacked below the sequence ──
        # Mirrors auto_mgr.tilt_step / rot_step / rest_time / daq_settle_time /
        # repeat_angles, refreshed by open_scan_params()'s save_params()
        # whenever an admin actually changes them. Plain ttk.Labels (not
        # Entries) so there's no temptation/ability to edit them from here.
        # Laid out as three columns of label/value pairs so five params stay
        # compact instead of a tall single column.
        params_panel = ttk.LabelFrame(right_status, text=" 📋 Current Scan Parameters ", padding=8)
        params_panel.pack(fill=tk.X, pady=(8, 0))

        am = self.controller.auto_mgr

        self.current_params_labels = {}
        # Four scalar params in a compact 2x2 grid; Repeat Angles gets its own
        # full-width wrapping row below (it can be long).
        scalar_defs = [
            ("tilt_step", "Tilt Step", lambda v: f"{v}°"),
            ("rot_step", "Rot Step", lambda v: f"{v}°"),
            ("rest_time", "Rest Time", lambda v: f"{v}s"),
            ("daq_settle_time", "Settle Time", lambda v: f"{v}s"),
        ]
        for i, (attr, label, fmt) in enumerate(scalar_defs):
            col = (i % 2) * 2
            row = i // 2
            ttk.Label(params_panel, text=f"{label}:", font=("Helvetica", 10)).grid(
                row=row, column=col, sticky="w", pady=3, padx=(0, 4))
            val_lbl = ttk.Label(params_panel, text=fmt(getattr(am, attr)),
                                font=("Helvetica", 13, "bold"), foreground="#007ACC")
            val_lbl.grid(row=row, column=col + 1, sticky="w", padx=(0, 24), pady=3)
            self.current_params_labels[attr] = val_lbl

        # Repeat Angles: own row, value spans the remaining columns and wraps
        # so a long grouped list flows onto multiple lines instead of running
        # off the right edge of the panel.
        rep_row = len(scalar_defs) // 2
        ttk.Label(params_panel, text="Repeat Angles:", font=("Helvetica", 10)).grid(
            row=rep_row, column=0, sticky="nw", pady=3, padx=(0, 4))
        rep_lbl = ttk.Label(params_panel, text=self._format_repeat_angles(am.repeat_angles),
                            font=("Helvetica", 11, "bold"), foreground="#007ACC",
                            wraplength=520, justify="left")
        rep_lbl.grid(row=rep_row, column=1, columnspan=3, sticky="w", padx=(0, 8), pady=3)
        self.current_params_labels["repeat_angles"] = rep_lbl

        ttk.Label(params_panel, text="🔒 Edit via Danger Zone → Params (admin)",
                  font=("Helvetica", 8), foreground="#6c757d").grid(
            row=rep_row + 1, column=0, columnspan=4, sticky="w", pady=(8, 0))

        # Restore the panel from its own last-used file (checkboxes + currents),
        # surviving restarts. If no saved file exists, fall back to seeding the
        # currents from the Laser tab. Persist again whenever a scan is started.
        if not self._load_laser_seq():
            self.notebook.after(2500, self._seed_laser_seq_currents)
        self.notebook.after(3000, self._refresh_laser_seq_dots)

        # Mirror live Laser/Wavelength/HV into Quick Setup (see
        # _poll_quick_setup_hardware). Delayed so laser_mgr/HV DB are ready.
        self.notebook.after(4000, self._poll_quick_setup_hardware)

        # ── Scan Progress Matrix: its own tab (next to Control Panel), not
        # embedded inline below the controls -- gives it the whole tab's
        # height instead of splitting dash_tab's limited vertical space.
        # Added here (before Schedule Manager below) so it lands right after
        # Control Panel (Master) in tab order.
        #
        # 2026-08-22: replaced the inline 46-cell grid with a live QE-vs-angle
        # plot (live_scan_view.LiveScanView) -- a cell only ever said OK/ERR,
        # never whether the data was any good, so a bad point sat undiscovered
        # until the Uniformity report ran hours later. The full grid still
        # exists, just moved into the Expand popup (_open_matrix_popup) for
        # when someone wants the exhaustive per-cell view.
        self.matrix_tab = ttk.Frame(self.upper_notebook, padding=10)
        # "Scan Progress Matrix" no longer fits -- there's no matrix on the
        # main view any more (2026-08-22, user: "탭에서 Scan Progress Matrix가
        # 아니라 다른 명칭으로 해야할 듯"). The grid still exists behind "Full
        # Grid" for anyone who wants it.
        self.upper_notebook.add(self.matrix_tab, text=" Live Scan ")

        # No title here -- the tab itself is already labeled "Scan Progress
        # Matrix", so a second identical LabelFrame title right below it
        # would just be redundant.
        #
        # Layout (2026-08-22, per operator's explicit sketch):
        #   row 0 (full width): toggle toolbar (metric / axis / etc)
        #   row 1: Live Console (weight 4)  |  plot (weight 6)
        # Console pane reuses the same _build_console_pane the Output tab's
        # DAQ/Produce/Analysis slots use (slot="general_scan"), so General
        # Scan's live output shows up right next to the plot it's filling in,
        # not just in a separate tab.
        matrix_outer = ttk.Frame(self.matrix_tab)
        matrix_outer.pack(fill=tk.BOTH, expand=True)
        matrix_outer.columnconfigure(0, weight=4)
        matrix_outer.columnconfigure(1, weight=6)
        matrix_outer.rowconfigure(1, weight=1)

        toolbar_row = ttk.Frame(matrix_outer)
        toolbar_row.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 3))
        ttk.Button(toolbar_row, text="🔍 Full Grid", command=self._open_matrix_popup).pack(
            side=tk.RIGHT)

        # "Output" clashes with the top-level Output tab (a different console
        # collection for Analysis/Produce/etc) -- "Live Console" makes clear
        # this is General Scan's own, separately-placed console (2026-08-15,
        # user: "Output이 두 개잖아").
        console_col = ttk.LabelFrame(matrix_outer, text=" Live Console ", padding=4)
        console_col.grid(row=1, column=0, sticky="nsew", padx=(0, 10))
        # Built lazily via ensure_console_pane the first time a scan actually
        # runs (matches every other console slot); a placeholder keeps the
        # tab from looking broken/empty before that.
        self._matrix_console_placeholder = ttk.Label(
            console_col, text="General Scan output will appear here once a scan starts.",
            foreground="#888888", anchor="center", justify="center", wraplength=300)
        self._matrix_console_placeholder.pack(fill=tk.BOTH, expand=True, pady=40)
        self._matrix_console_frame = console_col

        plot_col = ttk.Frame(matrix_outer)
        plot_col.grid(row=1, column=1, sticky="nsew")
        from live_scan_view import LiveScanView
        self.live_scan_view = LiveScanView(toolbar_row, plot_col, self.controller)

        # Popup-only matrix bookkeeping. self.matrix_frames/self.cells are
        # populated lazily the first time _open_matrix_popup() runs; until
        # then update_cell() simply finds no entry for a key and no-ops (see
        # its `if (sn, tilt, axis) in self.cells:` guard) -- harmless, since
        # the live plot above is now the primary progress indicator anyway.
        self.matrix_frames = {}

        self._matrix_popup = None
        self._update_matrix_tab_color()

        # --- 2. Schedule Managers 탭 ---
        schedule_tab = ttk.Frame(self.upper_notebook, padding=10)
        self.upper_notebook.add(schedule_tab, text=" Schedule Manager ")
        self._build_schedule_tab(schedule_tab)

        # --- 3. Logs 탭 ---
        log_tab = ttk.Frame(self.upper_notebook, padding=10)
        self.upper_notebook.add(log_tab, text=" Live Scan Logs ")
        
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
        self.upper_notebook.add(history_tab, text=" Scan History ")
        self._build_history_tab(history_tab)


    def _build_horizontal_table(self, parent, sn, big=False, vertical=True):
        """Builds the (sn, tilt, axis)-keyed cell grid. Two orientations:

        vertical=True  (main dashboard): tilt-as-rows, axis-as-columns. With
          the step tight enough to reach 13-23 tilt values, tilt-as-columns
          made the matrix very wide and short -- no room beside it for the
          console pane (see build_scan_tab's layout).
        vertical=False (Expand popup): tilt-as-columns, axis-as-rows -- the
          original wide/short layout. The popup has its own window and no
          console to share space with, and the tall vertical list there was
          reported as "way too long" to scroll through (2026-08-15).

        Cell LOOKUP is unaffected either way: self.cells is keyed by
        (sn, tilt, axis), not by grid position."""
        angles = self._current_tilt_angles()
        h_font = ("Helvetica", 16, "bold") if big else ("Helvetica", 13, "bold")
        d_font = ("Helvetica", 15, "bold") if big else ("Helvetica", 12, "bold")
        cell_w = 8 if big else 6
        cell_ipady = 10 if big else 6

        if not vertical:
            parent.columnconfigure(0, weight=0, minsize=90)
            for col in range(1, len(angles) + 1):
                parent.columnconfigure(col, weight=1)

            ttk.Label(parent, text="Axis \\ Tilt", font=h_font, anchor="center").grid(
                row=0, column=0, sticky="nsew", pady=5)
            for i, tilt in enumerate(angles):
                ttk.Label(parent, text=f"{tilt}°", font=d_font, anchor="center").grid(
                    row=0, column=i + 1, sticky="nsew", padx=5)

            for r_idx, axis in enumerate(["X", "Y"]):
                ttk.Label(parent, text=f"{axis}-Axis", font=h_font, anchor="center").grid(
                    row=r_idx + 1, column=0, sticky="nsew", pady=5)
                for i, tilt in enumerate(angles):
                    c = tk.Label(parent, text="-", bg="#e9ecef", relief="groove", font=d_font,
                                 width=cell_w, cursor="hand2")
                    c.grid(row=r_idx + 1, column=i + 1, sticky="nsew", padx=2, pady=2, ipady=cell_ipady)
                    c.bind("<Button-1>", lambda e, a=axis, t=tilt, s=sn: self._show_point_card(a, t, s))
                    self.cells.setdefault((sn, tilt, axis), []).append(c)
            return

        parent.columnconfigure(0, weight=0, minsize=90)
        for col in range(1, 3):
            parent.columnconfigure(col, weight=1)

        ttk.Label(parent, text="Tilt \\ Axis", font=h_font, anchor="center").grid(row=0, column=0, sticky="nsew", pady=5)

        for c_idx, axis in enumerate(["X", "Y"]):
            ttk.Label(parent, text=f"{axis}-Axis", font=h_font, anchor="center").grid(row=0, column=c_idx+1, sticky="nsew", pady=5)

        for i, tilt in enumerate(angles):
            ttk.Label(parent, text=f"{tilt}°", font=d_font, anchor="center").grid(row=i+1, column=0, sticky="nsew", padx=5)

            for c_idx, axis in enumerate(["X", "Y"]):
                c = tk.Label(parent, text="-", bg="#e9ecef", relief="groove", font=d_font,
                             width=cell_w, cursor="hand2")
                c.grid(row=i+1, column=c_idx+1, sticky="nsew", padx=2, pady=2, ipady=cell_ipady)
                # Click a completed (OK) cell to open the point card for that
                # scan point's data (recorded in LOG/ScanHistory/scanmap_*.json).
                c.bind("<Button-1>", lambda e, a=axis, t=tilt, s=sn: self._show_point_card(a, t, s))
                # A key can map to more than one widget: the popup ("expand")
                # matrix builds a second table for the same points, and both
                # copies must update together.
                self.cells.setdefault((sn, tilt, axis), []).append(c)


    # ── Scan Matrix point card ──────────────────────────────────────────────
    def _find_all_scan_points(self, axis, tilt):
        """Every recorded run for this (axis, tilt), across all scan dates and
        wavelengths, newest first. A multi-wavelength scan revisits the same
        (axis, tilt) once per wavelength block (see AutomationManager.
        _record_scan_point), so there can be several entries per point."""
        entries = []
        map_glob = os.path.join(self.controller.base_dir, "LOG", "ScanHistory", "scanmap_*.json")
        prefix, legacy_key = f"{axis}_{tilt}_", f"{axis}_{tilt}"
        for mp in glob.glob(map_glob):
            try:
                with open(mp, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            m = re.search(r"scanmap_(\d+)\.json$", os.path.basename(mp))
            date_tag = m.group(1) if m else "?"
            for key, info in data.items():
                if key == legacy_key or key.startswith(prefix):
                    info = dict(info)
                    info["wl"] = info.get("wl") or "-"
                    info["date_tag"] = date_tag
                    entries.append(info)
        entries.sort(key=lambda e: e.get("time", ""), reverse=True)
        return entries

    def _current_tilt_angles(self):
        auto_mgr = getattr(self.controller, 'auto_mgr', None)
        if auto_mgr is not None and hasattr(auto_mgr, 'build_tilt_angles'):
            return auto_mgr.build_tilt_angles()
        # Fallback (no auto_mgr yet, e.g. very early UI construction).
        scan_range = getattr(auto_mgr, 'scan_range', None) or {"start": -55, "end": 55}
        tilt_step = int(getattr(auto_mgr, 'tilt_step', 5) or 5)
        return list(range(scan_range["start"], scan_range["end"] + 1, tilt_step))

    def _show_point_card(self, axis, tilt, sn):
        """Open (or refresh, if already open) the single shared Scan Point
        card for this (axis, tilt, sn). Previously every matrix-cell click
        opened a brand-new window, so browsing several points left a pile of
        popups the operator had to close one by one. Now there is at most
        one point-card window: a second click anywhere just re-renders it in
        place, and the Prev/Next buttons step through angles the same way."""
        self._point_card_state = {"axis": axis, "tilt": tilt, "sn": sn}
        self._render_point_card()

    def _point_card_nav(self, direction):
        st = getattr(self, "_point_card_state", None)
        if not st:
            return
        angles = self._current_tilt_angles()
        try:
            idx = angles.index(st["tilt"])
        except ValueError:
            return
        new_idx = idx + direction
        if 0 <= new_idx < len(angles):
            st["tilt"] = angles[new_idx]
            self._render_point_card()

    def _render_point_card(self):
        """Render the shared point-card window's content for the CURRENT
        self._point_card_state -- called on first open and again by Prev/Next
        navigation. Every entries-dependent build step recomputes from
        scratch since a different angle can have entirely different runs."""
        import threading
        st = self._point_card_state
        axis, tilt, sn = st["axis"], st["tilt"], st["sn"]
        entries = self._find_all_scan_points(axis, tilt)

        auto_mgr = getattr(self.controller, 'auto_mgr', None)
        is_current = (
            getattr(auto_mgr, 'is_running', False) and
            getattr(auto_mgr, 'current_axis', None) == axis and
            getattr(auto_mgr, 'current_tilt', None) == tilt)
        current_wl = getattr(auto_mgr, '_current_block_wl', None) if is_current else None

        if not CTK_AVAILABLE:
            messagebox.showerror("Missing dependency",
                                 "customtkinter is not installed.\n\nRun:  pip install customtkinter")
            return
        ctk.set_appearance_mode("light"); ctk.set_default_color_theme("blue")

        # Reuse the existing Toplevel if it's still open; otherwise create it
        # once and remember it on self so the NEXT click reuses it too.
        win = getattr(self, "_point_card_win", None)
        if win is not None and win.winfo_exists():
            for w in win.winfo_children():
                w.destroy()
        else:
            win = ctk.CTkToplevel(self.notebook)
            win.geometry("640x480")
            self._point_card_win = win
        win.title(f"Scan Point — {axis}-axis {tilt:+d}° ({sn})")
        win.lift()
        win.focus_force()

        frm = ctk.CTkFrame(win, fg_color="transparent")
        frm.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

        # ── Header: title + Prev/Next angle navigation (same axis/sn) ───────
        header = ctk.CTkFrame(frm, fg_color="transparent")
        header.pack(fill=tk.X, pady=(0, 8))
        angles = self._current_tilt_angles()
        idx = angles.index(tilt) if tilt in angles else -1
        has_prev = idx > 0
        has_next = 0 <= idx < len(angles) - 1
        ctk.CTkButton(header, text="◀ Prev", width=70,
                      state=(tk.NORMAL if has_prev else tk.DISABLED),
                      command=lambda: self._point_card_nav(-1)).pack(side=tk.LEFT)
        ctk.CTkButton(header, text="Next ▶", width=70,
                      state=(tk.NORMAL if has_next else tk.DISABLED),
                      command=lambda: self._point_card_nav(1)).pack(side=tk.LEFT, padx=(6, 12))
        ctk.CTkLabel(header, text=f"{axis}-axis {tilt:+d}°  —  {len(entries)} run(s) recorded",
                     font=ctk.CTkFont(size=15, weight="bold")).pack(side=tk.LEFT)

        if not entries:
            ctk.CTkLabel(frm, text="No recorded data for this angle yet.\n"
                                   "(Use Prev/Next to browse to a completed point.)",
                         text_color="#8a9099").pack(anchor="w", pady=20)
            return

        list_frame = ctk.CTkFrame(frm)
        list_frame.pack(fill=tk.BOTH, expand=True)
        cols = ("daq", "wl", "date", "time", "status")
        tree = ttk.Treeview(list_frame, columns=cols, show="headings", height=6, selectmode="browse")
        for c, w, t in (("daq", 90, "DAQ"), ("wl", 70, "λ"), ("date", 90, "Date"),
                        ("time", 150, "Time"), ("status", 80, "Status")):
            tree.heading(c, text=t)
            tree.column(c, width=w, anchor="center")
        sb = ttk.Scrollbar(list_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        tree.tag_configure("current", background="#28a745", foreground="white")
        tree.tag_configure("error", foreground="#dc3545")

        def _daq_label(info):
            # HK-mode runs are recorded with a "HK:<target>:<run_id>" file
            # string (see _execute_hk_point); CAEN runs store a normal local
            # RAW path. An ERROR run may have no file at all -> unknown.
            f = str(info.get("file", "") or "")
            if f.startswith("HK:"):
                return "🖧 HK"
            if f:
                return "🇰🇷 Korean"
            return "-"

        live_assigned = False
        for i, info in enumerate(entries):
            wl = info.get("wl", "-")
            # Only the single newest matching entry is the run actually being
            # acquired right now (entries is sorted newest-first) -- matching on
            # wavelength alone would mark every past run of that wavelength LIVE.
            live = is_current and not live_assigned and wl == (current_wl or "-")
            if live:
                live_assigned = True
            tags = ("current",) if live else (("error",) if info.get("status") == "ERROR" else ())
            label = f"{wl}  ⏺ LIVE" if live else wl
            tree.insert("", "end", iid=str(i),
                        values=(_daq_label(info), label, info.get("date_tag", "?"),
                                info.get("time", "?"), info.get("status", "OK")),
                        tags=tags)

        detail = ctk.CTkFrame(frm, fg_color="transparent")
        detail.pack(fill=tk.X, pady=(10, 0))

        # Disk-bound lookups (file existence/size, uproot QE read) used to run
        # synchronously on the GUI thread on EVERY row click, uncached -- for
        # a run with 25+ entries that meant re-reading the same ROOT file over
        # and over, blocking the UI each time (felt like "clicking a row
        # lags", even though the ERROR branch right below does no I/O at all
        # and was never actually the slow one). Now cached per raw-file path
        # and resolved in a background thread so no click ever blocks the GUI.
        if not hasattr(self, "_point_detail_io_cache"):
            self._point_detail_io_cache = {}
        self._point_detail_render_token = getattr(self, "_point_detail_render_token", 0) + 1

        def render_detail(info):
            for w in detail.winfo_children():
                w.destroy()

            if info.get("status") == "ERROR":
                ctk.CTkLabel(detail, text=f"⚠ ERROR RUN — {info.get('reason', 'unknown')}",
                             text_color="#dc3545", font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w")
                ctk.CTkLabel(detail, text=f"Time: {info.get('time', '-')}   Wavelength: {info.get('wl', '-')}"
                             ).pack(anchor="w")
                return

            self._point_detail_render_token += 1
            token = self._point_detail_render_token

            cfg = self.controller.config_manager.get_all_variables() if self.controller.config_manager else {}
            raw_p = info.get("file", "") or ""
            base = os.path.basename(raw_p)
            if sn == getattr(self, 'sn2_val', None):
                ch, rot, cable = 1, info.get("rot2"), cfg.get("direction2", "B")
            else:
                ch, rot, cable = 2, info.get("rot3"), cfg.get("direction3", "B")
            try:
                side = injection_side_label(cable, float(rot), float(tilt)) or "-"
            except (TypeError, ValueError):
                side = "-"

            res_p = os.path.join(cfg.get("FinalResultPath", ""), base.replace("_raw_", "_result_"))
            img_dir = cfg.get("ImagePath", "")
            charge_png = os.path.join(img_dir, "ByAnalysis",
                                      base.replace("_raw_", "_result_").replace(".root", "_charge.png"))
            contour_png = os.path.join(img_dir, "Contour", base.replace(".root", "_Contour.png"))

            def row(r, label, value):
                ctk.CTkLabel(detail, text=label, font=ctk.CTkFont(size=13, weight="bold"),
                             anchor="e", width=110).grid(row=r, column=0, sticky="e", pady=2)
                lbl = ctk.CTkLabel(detail, text=value, anchor="w")
                lbl.grid(row=r, column=1, sticky="w", padx=(10, 0), pady=2)
                return lbl

            row(0, "Wavelength:", info.get("wl", "-"))
            size_lbl = row(1, "Run file:", f"{base}   (loading…)")
            row(2, "Angle:", f"Rot {rot}° / Tilt {tilt}°   (Injects: {side})")
            row(3, "Taken:", info.get("time", "?"))

            qe_lbl = ctk.CTkLabel(detail, text="…", font=ctk.CTkFont(size=16, weight="bold"),
                                  text_color="#2e9e4f", anchor="w")
            ctk.CTkLabel(detail, text=f"QE (CH{ch}) [Counting]:", font=ctk.CTkFont(size=14, weight="bold"),
                         anchor="e", width=110).grid(row=4, column=0, sticky="e", pady=(6, 0))
            qe_lbl.grid(row=4, column=1, sticky="w", padx=(10, 0), pady=(6, 0))
            poi_row = {"label": None, "value": None}

            btns = ctk.CTkFrame(detail, fg_color="transparent")
            btns.grid(row=6, column=0, columnspan=2, sticky="w", pady=(12, 0))

            def open_waveform():
                win.destroy()
                self.controller.ui.focus_waveform_tab(raw_p)

            def open_png(path):
                try:
                    subprocess.Popen(["xdg-open", path])
                except Exception as e:
                    messagebox.showerror("Error", f"Could not open image:\n{e}", parent=win)

            # Buttons start disabled and flip on once the background lookup
            # confirms the file exists -- never block on os.path.exists() here.
            wave_btn = ctk.CTkButton(btns, text="📈 Waveform", width=130, command=open_waveform,
                                     state=tk.DISABLED)
            wave_btn.pack(side=tk.LEFT, padx=(0, 6))
            charge_btn = ctk.CTkButton(btns, text="📊 Charge Plot", width=130, fg_color="#6c757d",
                                       hover_color="#5a6268", command=lambda: open_png(charge_png),
                                       state=tk.DISABLED)
            charge_btn.pack(side=tk.LEFT, padx=6)
            contour_btn = ctk.CTkButton(btns, text="🖼 Contour", width=130, fg_color="#6c757d",
                                        hover_color="#5a6268", command=lambda: open_png(contour_png),
                                        state=tk.DISABLED)
            contour_btn.pack(side=tk.LEFT, padx=6)

            def apply_result(result):
                # Stale if the user already selected a different row, or the
                # point-card window was closed/replaced, while this was loading.
                if token != self._point_detail_render_token or not win.winfo_exists():
                    return
                raw_ok, size_str, cnt_str, poi_str, charge_ok, contour_ok = result
                size_lbl.configure(text=f"{base}   ({size_str})")
                qe_lbl.configure(text=cnt_str)
                if poi_str and poi_row["label"] is None:
                    poi_row["label"] = ctk.CTkLabel(detail, text="Poisson (ref):", font=ctk.CTkFont(size=11),
                                                    text_color="#8a9099", anchor="e", width=110)
                    poi_row["label"].grid(row=5, column=0, sticky="e")
                    poi_row["value"] = ctk.CTkLabel(detail, text=poi_str, font=ctk.CTkFont(size=11),
                                                    text_color="#8a9099", anchor="w")
                    poi_row["value"].grid(row=5, column=1, sticky="w", padx=(10, 0))
                wave_btn.configure(state=tk.NORMAL if raw_ok else tk.DISABLED)
                charge_btn.configure(state=tk.NORMAL if charge_ok else tk.DISABLED)
                contour_btn.configure(state=tk.NORMAL if contour_ok else tk.DISABLED)

            cached = self._point_detail_io_cache.get(raw_p)
            if cached is not None:
                apply_result(cached)
                return

            def worker():
                raw_ok = os.path.exists(raw_p)
                res_ok = os.path.exists(res_p)
                size_str = f"{os.path.getsize(raw_p) / 1e6:.0f} MB" if raw_ok else "missing"
                charge_ok = os.path.exists(charge_png)
                contour_ok = os.path.exists(contour_png)

                # Counting method (PHC + Timing cut, "relativeQE") is the
                # PRIMARY QE figure -- shown bold/large as the main value.
                # Poisson (shape-fit, no cut, "poisson_qe") is kept only as a
                # smaller reference line, not given equal billing anymore.
                cnt_str, poi_str = "—", None
                if res_ok:
                    try:
                        import uproot
                        with uproot.open(res_p) as rf:
                            tr = rf[f"tree_ch{ch}"]
                            cnt = float(tr["relativeQE"].array(library="np")[0])
                            poi = float(tr["poisson_qe"].array(library="np")[0])
                            cnt_str = f"{cnt:.2f}%"
                            poi_str = f"{poi:.2f}%"
                    except Exception:
                        cnt_str = "(failed to read result file)"
                else:
                    cnt_str = "—  (run Analysis to compute)"

                result = (raw_ok, size_str, cnt_str, poi_str, charge_ok, contour_ok)
                # Only cache a COMPLETE, final result (analysis done: result
                # file read OK, both QE numbers present). An in-progress run
                # shows "run Analysis to compute" / partial state -- caching
                # that would pin the stale value forever, so a later click
                # after analysis finished would never refresh. Leaving it
                # uncached means such a run is simply re-read next click (cheap
                # for a not-yet-analyzed point), while the 25-past-run browsing
                # case that caused the lag is fully cached.
                if res_ok and poi_str is not None:
                    self._point_detail_io_cache[raw_p] = result
                self.notebook.after(0, lambda: apply_result(result))

            threading.Thread(target=worker, daemon=True).start()

        def on_select(event=None):
            sel = tree.selection()
            if sel:
                render_detail(entries[int(sel[0])])

        tree.bind("<<TreeviewSelect>>", on_select)
        # Select the live run if there is one, otherwise the most recent entry.
        default_idx = next((i for i, info in enumerate(entries)
                            if is_current and info.get("wl", "-") == (current_wl or "-")), 0)
        tree.selection_set(str(default_idx))
        render_detail(entries[default_idx])

    def _format_repeat_angles(self, angles):
        # angles is a list of (tilt, rot) pairs. rot is the axis rotation
        # offset (0 = X-scan, 90 = Y-scan). Grouped BY rotation so a long scan
        # (e.g. 9 tilts at one rotation) reads as one compact "R45° → T[...]"
        # entry instead of a wide comma run that overflows the status panel.
        if not angles:
            return "None"
        groups = {}
        for (t, r) in angles:
            groups.setdefault(r, []).append(t)
        parts = []
        for r in sorted(groups):
            tilts = ", ".join(f"{t:g}" for t in sorted(groups[r]))
            parts.append(f"R{r:g}° → T[{tilts}]")
        return "    ".join(parts)

    def _select_daq_backend(self, backend):
        """Set the DAQ backend and repaint the segmented toggle: the active
        side gets a solid color fill + colored focus ring, the inactive side
        goes flat/grey -- selection should read at a glance, not require
        squinting at a small radio dot."""
        self.daq_backend_var.set(backend)
        self.controller.auto_mgr.daq_backend = backend

        colors = {
            "caen": (self.PALETTE["move"], "white"),
            "hk":   (self.PALETTE["start"], "white"),
        }
        for value, (frame, btn, sub_lbl) in self._daq_backend_buttons.items():
            if value == backend:
                bg, fg = colors[value]
                frame.config(bg=bg, highlightbackground=bg, highlightcolor=bg)
                btn.config(bg=bg, fg=fg)
                sub_lbl.config(bg=bg, fg=fg)
            else:
                bg = "#e9ecef"
                frame.config(bg=bg, highlightbackground="#d9dce1", highlightcolor="#d9dce1")
                btn.config(bg=bg, fg=self.PALETTE["text_muted"])
                sub_lbl.config(bg=bg, fg=self.PALETTE["text_muted"])

        # HK Config button is only usable in HK mode (greyed out otherwise).
        if hasattr(self, "btn_hk_config"):
            self.btn_hk_config.config(state=(tk.NORMAL if backend == "hk" else tk.DISABLED))
        if backend == "hk":
            self.daq_backend_note.configure(
                text="⚠ HK Digitizer mode — local config3.h is NOT used.",
                foreground="#c9820a")
            self.controller._log("[INFO] DAQ backend → HK Digitizer (2nd PC; config3.h ignored).")
        else:
            self.daq_backend_note.configure(text="")
            self.controller._log("[INFO] DAQ backend → Korean DAQ (CAEN).")

        # Reflect the switch immediately in the System Connection Overview
        # badge (otherwise it'd only update on the next 2s dashboard tick).
        try:
            self.controller.ui._refresh_daq_backend_label()
        except Exception:
            pass

    def _on_daq_backend_change(self):
        """Back-compat entry point (some earlier code paths may still call
        this name) -- forwards to _select_daq_backend with the current
        StringVar value."""
        self._select_daq_backend(self.daq_backend_var.get())

    def _lock_daq_backend_toggle(self, locked):
        """Freeze/unfreeze the DAQ backend segmented toggle. Called from
        update_start_button alongside the Start/Pause/Reset lock -- switching
        which digitizer takes the data mid-scan doesn't do anything useful
        (the running scan already committed to a backend) and just invites
        confusion, so the control is disabled for the duration of a scan
        instead of merely hoping the operator leaves it alone."""
        if not hasattr(self, "_daq_backend_buttons"):
            return
        self._daq_backend_locked = locked
        cursor = "arrow" if locked else "hand2"
        for value, (frame, btn, sub_lbl) in self._daq_backend_buttons.items():
            for w in (frame, btn, sub_lbl):
                w.config(cursor=cursor)
        if hasattr(self, "daq_backend_lock_note"):
            self.daq_backend_lock_note.config(
                text="🔒 Locked during scan" if locked else "")

    def open_hk_config(self):
        """HK Digitizer (2nd PC) configuration dialog. These parameters are the
        HK data path -- the local config3.h is NOT used in HK mode. Angle
        (tilt/rot) here is the manual/default; during a scan the live angle of
        each point is sent instead."""
        auto_mgr = self.controller.auto_mgr
        hk = auto_mgr.hk_config
        win = tk.Toplevel(self.notebook)
        win.title("HK Digitizer Configuration")
        # transient() needs an actual TOPLEVEL window, not a plain widget --
        # passing self.notebook (a Notebook widget, not a window) set a bogus
        # WM_TRANSIENT_FOR hint that made the window manager treat this as a
        # fixed-size dialog, silently disabling Maximize/Minimize.
        win.transient(self.notebook.winfo_toplevel())
        win.resizable(True, True)
        win.geometry("820x760")
        win.minsize(600, 460)
        win.attributes("-topmost", True)

        ttk.Label(win, text="🖧 HK Digitizer Configuration",
                  font=("Helvetica", 14, "bold")).pack(anchor="w", padx=18, pady=(16, 0))
        ttk.Label(win, text="2nd-PC data path — local config3.h is not used.",
                  font=("Helvetica", 9), foreground="#6c757d").pack(anchor="w", padx=18, pady=(0, 6))
        # Angles are driven by the scan (motors move each point) — never typed
        # here. Live-readout row so the operator can SEE that automatic angle
        # without leaving this dialog, updated from the actual motor encoders
        # (not from any HK config field).
        angle_live_lbl = ttk.Label(win, text="⟳ Angles (tilt / rot): AUTOMATIC — set per scan point, not here.  |  live: —",
                                   font=("Helvetica", 9, "bold"), foreground="#2e7d32")
        angle_live_lbl.pack(anchor="w", padx=18, pady=(0, 4))

        def _refresh_angle_live():
            if not win.winfo_exists():
                return
            try:
                t2, r2 = self.controller.rot_mgr.read_angles(2)
                t3, r3 = self.controller.rot_mgr.read_angles(3)
                sn2 = f"{t2:.1f}°/{r2:.1f}°" if t2 is not None and r2 is not None else "—"
                sn3 = f"{t3:.1f}°/{r3:.1f}°" if t3 is not None and r3 is not None else "—"
                live = f"SN2 tilt/rot {sn2}   SN3 tilt/rot {sn3}"
            except Exception:
                live = "—"
            angle_live_lbl.config(
                text=f"⟳ Angles (tilt / rot): AUTOMATIC — set per scan point, not here.  |  live: {live}")
            win.after(1000, _refresh_angle_live)
        _refresh_angle_live()

        # ── Admin lock for the "infrastructure" fields (SSH Target and below:
        # where/how commands run on the HK PC) -- same pattern as General
        # Scan's own Params dialog. The per-run fields above (Filename, Run
        # Number, Acq/Delay/Threshold/Gate/Chan, Work Dir) stay always-editable
        # since operators change those routinely; infra fields are rarer and
        # riskier to fat-finger. ──────────────────────────────────────────────
        infra_row = ttk.Frame(win)
        infra_row.pack(anchor="w", padx=18, pady=(0, 8), fill=tk.X)
        infra_lock_btn = tk.Button(infra_row, text="🔒 Admin Unlock (SSH Target & below)",
                                   font=("Helvetica", 9, "bold"))
        self._style_button(infra_lock_btn, "warn")
        infra_lock_btn.pack(side=tk.LEFT)
        infra_note = ttk.Label(infra_row,
                               text="  Locked by default — these control WHERE/HOW commands run on the HK PC.",
                               font=("Helvetica", 8), foreground="#8a9099")
        infra_note.pack(side=tk.LEFT)

        # Bottom-up build order so the stack reads (top→bottom): scrollable
        # fields, live command preview, pipeline stage buttons, Save/Cancel.
        btns = ttk.Frame(win)
        btns.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=(4, 14))
        stages = ttk.LabelFrame(win, text=" Pipeline (streams to Output → HK Digitizer) ", padding=6)
        stages.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=(0, 4))

        # ── Live "Final Command" preview: what will actually be sent, always
        # in sync with the fields above (rebuilt on every keystroke) so a long
        # command never has to be pieced together by eye. ────────────────────
        preview_frame = ttk.LabelFrame(win, text=" Final Command (auto-generated) ", padding=4)
        preview_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=(0, 4))
        preview = tk.Text(preview_frame, height=7, wrap="none", font=("Consolas", 9),
                          bg="#1e1e1e", fg="#d4d4d4", state="disabled")
        prev_vsb = ttk.Scrollbar(preview_frame, orient="vertical", command=preview.yview)
        prev_hsb = ttk.Scrollbar(preview_frame, orient="horizontal", command=preview.xview)
        preview.configure(yscrollcommand=prev_vsb.set, xscrollcommand=prev_hsb.set)
        preview.grid(row=0, column=0, sticky="nsew")
        prev_vsb.grid(row=0, column=1, sticky="ns")
        prev_hsb.grid(row=1, column=0, sticky="ew")
        preview_frame.grid_rowconfigure(0, weight=1)
        preview_frame.grid_columnconfigure(0, weight=1)

        # Scrollable body (vertical AND horizontal): long commands + many
        # fields no longer get clipped -- the card is allowed to grow wider
        # than the canvas, and a horizontal scrollbar reaches the rest.
        body = ttk.Frame(win)
        body.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 4))
        canvas = tk.Canvas(body, highlightthickness=0)
        vsb = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        hsb = ttk.Scrollbar(body, orient="horizontal", command=canvas.xview)
        canvas.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        card = ttk.Frame(canvas, padding=12)
        card_id = canvas.create_window((0, 0), window=card, anchor="nw")
        card.columnconfigure(1, weight=1)
        card.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        def _on_canvas_configure(e):
            # Stretch the card to fill a wide canvas (nice when the dialog is
            # resized larger); but never shrink it below its own natural
            # width, so long single-line fields stay scrollable instead of
            # being clipped.
            canvas.itemconfig(card_id, width=max(e.width, card.winfo_reqwidth()))
            canvas.configure(scrollregion=canvas.bbox("all"))
        canvas.bind("<Configure>", _on_canvas_configure)
        canvas.bind("<Enter>", lambda e: (
            canvas.bind_all("<Button-4>", lambda ev: canvas.yview_scroll(-1, "units")),
            canvas.bind_all("<Button-5>", lambda ev: canvas.yview_scroll(1, "units"))))
        canvas.bind("<Leave>", lambda e: (
            canvas.unbind_all("<Button-4>"), canvas.unbind_all("<Button-5>")))

        # Fields below this point control WHERE/HOW commands run on the HK PC
        # (as opposed to per-run acquisition parameters above) -- locked by
        # default, unlocked via the Admin button above.
        protected_keys = {"ssh_target", "setup_cmd", "scan_manager",
                          "dpb_setup_cmd", "vmodem_device", "vmodem_keys"}

        # (key, label, kind, hint, width). kind: int/float/str/fixed
        fields = [
            ("run_id",        "Filename (-i)",         "str",   "typed here (static); use {run}/{tilt}/{rot2}(SN2)/{rot3}(SN3)/{trgch}/{trgvth} to auto-fill", 70),
            ("run_number",    "Run Number",             "int",   "auto-increments after EVERY acquisition (manual or scan) — see live value →", 14),
            ("acq_time",      "Acq Time --l (s)",       "float", "auto-sent as --l per point", 14),
            ("move_delay",    "Move Delay (s)",         "float", "extra wait AFTER acq, before rotating", 14),
            ("threshold_preset", "Threshold preset", "str", "preset number: blank/1 → --threpreset, 2 → --threpreset2, 3 → --threpreset3", 20),
            ("trg_channel",   "Trigger ch --trgch",     "str",   "trigger channel; blank = flag omitted (default trigger)", 20),
            ("trg_vth",       "Trigger Vth --trgch-Vth", "str",  "trigger threshold voltage; blank = flag omitted", 20),
            ("gatelist",      "Gate --gatelist",        "int",   "gate width sent as --gatelist", 14),
            ("chanlist",      "Chan --chanlist",        "str",   "channels 0–11 (0 = first input); e.g. 0 or 0,1,2; blank = flag omitted, ScanManager default", 20),
            ("work_dir",      "Work Dir (cd)",          "str",   "supports {date} → today (YYYYMMDD); blank = no cd", 60),
            ("ssh_target",    "SSH Target 🔒",          "str",   "user@host, e.g. hkpd@hkdaq", 30),
            ("setup_cmd",     "Setup Cmd 🔒",           "str",   "sourced before ScanManager", 60),
            ("scan_manager",  "ScanManager 🔒",         "str",   None, 70),
            ("dpb_setup_cmd", "DPB Setup (②) 🔒",       "str",   "runs on hkpd; nested ssh → root@dpb-local (socat + daq) — one-time", 70),
            ("vmodem_device", "vmodem Device (③) 🔒",   "str",   None, 60),
            ("vmodem_keys",   "vmodem Keys (③) 🔒",     "str",   "comma-separated keys, auto-written into a runscript each run; default m,O", 24),
        ]
        vars_ = {}
        protected_entries = []
        run_number_live_lbl = None
        run_number_entry = None
        for r, (key, label, kind, hint, width) in enumerate(fields):
            ttk.Label(card, text=f"{label}:", font=("Helvetica", 10)).grid(
                row=r * 2, column=0, sticky="w", padx=(2, 8), pady=(6, 0))
            v = tk.StringVar(value=str(hk.get(key, "")))
            ent = ttk.Entry(card, textvariable=v, width=width)
            if kind == "fixed":
                ent.configure(state="readonly")   # greyed, not editable
            elif key in protected_keys:
                ent.configure(state="disabled")   # greyed until Admin Unlock
                protected_entries.append(ent)
            ent.grid(row=r * 2, column=1, sticky="ew", pady=(6, 0))
            vars_[key] = (v, kind)
            if hint:
                ttk.Label(card, text=hint, font=("Helvetica", 8),
                          foreground="#8a9099").grid(row=r * 2 + 1, column=1, sticky="w")
            if key == "run_number":
                run_number_entry = ent
                # Live counter (reads the shared hk_config dict directly, so it
                # keeps climbing during a scan even while this dialog stays
                # open) -- answers "is it actually auto-incrementing?" at a
                # glance instead of having to reopen the dialog to check.
                run_number_live_lbl = ttk.Label(card, text="", font=("Helvetica", 10, "bold"),
                                                foreground=self.PALETTE["accent"])
                run_number_live_lbl.grid(row=r * 2, column=2, sticky="w", padx=(10, 0))

        def _refresh_run_number_live():
            if not win.winfo_exists():
                return
            live_val = hk.get('run_number', '?')
            run_number_live_lbl.config(text=f"● live: {live_val}")
            # Keep the EDITABLE Run Number field itself in sync with the live
            # value too (skip while the operator has it focused/mid-edit, so
            # typing isn't clobbered underfoot). Without this, the entry was a
            # one-time snapshot from dialog-open, and clicking ANY of ②/③/④/🧪
            # or 💾 Save applied that stale number back onto hk_config -- e.g.
            # a Manual Acquire auto-incremented it to 4, then a later Save (for
            # an unrelated field edit) silently reset it back to the entry's
            # old "3", so the NEXT acquisition overwrote the previous one's
            # file (confirmed 2026-07-26). Now the field can't go stale.
            v, _kind = vars_.get("run_number", (None, None))
            if v is not None and win.focus_get() is not run_number_entry:
                v.set(str(live_val))
            win.after(1000, _refresh_run_number_live)
        _refresh_run_number_live()

        def _unlock_infra():
            if self.controller.access_mgr.verify_password_prompt(
                    "Security", "Enter Master Password (HK Infrastructure Fields):"):
                for ent in protected_entries:
                    ent.configure(state="normal")
                infra_lock_btn.configure(state="disabled", text="🔓 Unlocked")
                infra_note.configure(text="  Unlocked for this dialog session.")
                self.controller._log("[INFO] HK infrastructure fields unlocked (admin).")
            else:
                self.controller._log("[WARNING] Admin unlock denied for HK infrastructure fields.")
        infra_lock_btn.configure(command=_unlock_infra)

        def _collect():
            new = {}
            for key, (v, kind) in vars_.items():
                s = v.get().strip()
                if kind == "int":
                    new[key] = int(float(s))
                elif kind == "float":
                    new[key] = float(s)
                elif kind == "fixed":
                    continue                       # locked -- keep existing value
                else:
                    new[key] = s
            return new

        def _live_dict():
            """Raw (uncommitted) snapshot of every field as typed right now --
            used only to render the preview, tolerant of half-finished input."""
            return {key: v.get() for key, (v, kind) in vars_.items()}

        def _refresh_preview(*_a):
            d = _live_dict()
            try:
                acq = float(d.get("acq_time") or 0)
            except ValueError:
                acq = 0.0
            # Show the run_id with the live {run} substituted (3-digit padded)
            # so the operator sees the actual filename; {tilt}/{rot} stay literal
            # because they're filled per scan point.
            try:
                run_no = int(float(d.get("run_number") or 0))
            except ValueError:
                run_no = 0
            shown_run_id = d.get("run_id", "").replace("{run}", f"{run_no:03d}")
            try:
                scan_cmd = auto_mgr.hk_format_remote(d, shown_run_id, acq)
            except Exception as e:
                scan_cmd = f"(cannot build yet: {e})"
            try:
                vmodem_cmd = auto_mgr.hk_format_vmodem(d)
            except Exception as e:
                vmodem_cmd = f"(cannot build yet: {e})"
            text = (f"Filename: {shown_run_id}\n\n"
                    f"[④ Acquire]\n{scan_cmd}\n\n"
                    f"[③ vmodem]\n{vmodem_cmd}")
            preview.config(state="normal")
            preview.delete("1.0", "end")
            preview.insert("1.0", text)
            preview.config(state="disabled")

        for key, (v, kind) in vars_.items():
            v.trace_add("write", _refresh_preview)
        _refresh_preview()

        def _save():
            # Deliberately does NOT close the dialog -- an operator watching
            # the live Run Number counter during a scan, or iterating on
            # acq/delay values between test shots, needs the window to stay
            # put after Save (closing every time was the reported annoyance).
            try:
                hk.update(_collect())
            except ValueError as e:
                messagebox.showerror("Invalid HK Config",
                                     f"{e}\n\nRun Number/Acq/Delay/Gate must be numbers."
                                     "\nKeeping previous values.")
                return
            auto_mgr.save_hk_config()
            chan_disp = str(hk.get('chanlist') or '').strip() or "(default, --chanlist omitted)"
            self.controller._log(
                f"[INFO] HK config saved: filename {hk['run_id']}, run#{hk['run_number']}, "
                f"acq {hk['acq_time']}s, delay {hk['move_delay']}s, thr '{hk['threshold_preset']}', "
                f"gate {hk['gatelist']}, chan {chan_disp}, target {hk['ssh_target']}")
            save_status_lbl.config(text=f"✅ Saved @ {datetime.now().strftime('%H:%M:%S')}")

        def _run_pipeline_stage(build_fn, name):
            # Save on-screen edits first, then stream build_fn()'s command to
            # the HK console. Empty/unbuildable command = nothing to run.
            try:
                collected = _collect()
            except ValueError as e:
                messagebox.showerror("Invalid HK Config", str(e))
                return
            # run_number auto-increments elsewhere (scan loop / the dashboard's
            # Manual Acquire button) while this dialog can stay open the whole
            # time -- but the Run Number ENTRY here is just a static snapshot
            # from whenever the dialog opened (only the separate "● live: N"
            # label actually tracks it). None of these 4 buttons (②DPB Setup /
            # ③vmodem / ④Acquire / 🧪Test) are "set the run number" actions, so
            # applying the stale entry here silently reset the live counter
            # backward on every click -- confirmed 2026-07-26: two Manual
            # Acquires both logged "run#3" because a DPB Setup/vmodem click in
            # between stomped run_number back to the dialog's stale "3",
            # causing the second acquisition to overwrite the first's file.
            collected.pop("run_number", None)
            hk.update(collected)
            auto_mgr.save_hk_config()
            try:
                cmd = (build_fn() or "").strip()
            except Exception as e:
                messagebox.showerror("Build Error", f"{name}: {e}")
                return
            if not cmd:
                messagebox.showinfo("Nothing to run", f"{name}: command is empty.")
                return
            auto_mgr.hk_run_in_console(cmd, job_name=name)

        def _run_stage_once(build_fn, name, done_attr, risk_note):
            # ② and ③ are RECOMMENDED to run just once per session, not
            # hard-required -- re-running is sometimes legitimate (e.g. the
            # DPB board itself got rebooted), so this only asks for
            # confirmation the 2nd+ time instead of silently re-firing or
            # blocking outright.
            if getattr(auto_mgr, done_attr, False):
                if not messagebox.askyesno(
                        f"{name} already run",
                        f"{name} was already run this session.\n\n{risk_note}\n\n"
                        f"Run it again anyway?"):
                    return
            _run_pipeline_stage(build_fn, name)
            setattr(auto_mgr, done_attr, True)

        def _run_dpb_setup():
            # hk_dpb_setup_done (the guard _run_stage_once uses) is a
            # SESSION-only flag -- it resets to False on every MASTER app
            # restart. Incident 2026-07-26: a routine restart of THIS app
            # reset the flag, so clicking ② again fired with no warning even
            # though the socat/daq daemons from before the restart were still
            # alive on dpb-local -- a real duplicate socat process on port
            # 9001 resulted. A local-only flag can't catch this since the
            # remote daemon's lifetime doesn't depend on the master GUI.
            #
            # So before relying on that flag, do one READ-ONLY check of the
            # remote's actual process count (see hk_dpb_alive_count --
            # nothing is installed or left on the DAQ PC, just a `pgrep`
            # query run FROM here over the same SSH link already used
            # everywhere in this dialog). This entire guard lives and runs
            # only on the master side, per the operator's request not to
            # touch the DAQ PC (which isn't theirs to modify) beyond the
            # existing read-only queries already in use.
            save_status_lbl.config(text="🔍 Checking remote DPB state…")
            win.update_idletasks()
            count = auto_mgr.hk_dpb_alive_count()
            save_status_lbl.config(text="")

            if count == -1:
                # Couldn't verify (SSH down/unreachable) -- fall back to the
                # local session-only flag rather than blocking the operator.
                _run_stage_once(
                    lambda: hk.get("dpb_setup_cmd", ""), "DPB Setup", "hk_dpb_setup_done",
                    "Re-running can spawn a DUPLICATE socat/daq daemon if the first is still up.\n"
                    "(Could not verify live remote state just now -- SSH check failed.)")
                return
            if count >= 2:
                if not messagebox.askyesno(
                        "DPB already running remotely",
                        f"Found {count} live 'socat' process(es) on dpb-local right now --\n"
                        f"DPB Setup already appears to be up and running.\n\n"
                        f"Running it again WILL spawn duplicate socat/daq daemons on the "
                        f"SAME ports (this has happened before and caused a stuck duplicate "
                        f"on port 9001).\n\nRun it again anyway?"):
                    return
            # count == 0 or 1: nothing (or only a partial) daemon set is up,
            # safe to proceed without asking.
            _run_pipeline_stage(lambda: hk.get("dpb_setup_cmd", ""), "DPB Setup")
            auto_mgr.hk_dpb_setup_done = True

        # Stage buttons (② DPB setup is a one-time step; ③ vmodem processing
        # always writes a fresh runscript from vmodem_keys, so m/O are never
        # missing; ④ ScanManager acquire == the same as the Test button below).
        tk.Button(stages, text="② DPB Setup (once)",
                  command=_run_dpb_setup,
                  font=("Helvetica", 9, "bold")).pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(stages, text="③ vmodem Process",
                  command=lambda: _run_stage_once(
                      auto_mgr.hk_build_vmodem_remote, "vmodem Process", "hk_vmodem_done",
                      "Re-sending the m/O keys may just re-toggle the mode instead of setting it."),
                  font=("Helvetica", 9, "bold")).pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(stages, text="④ Acquire (ScanManager)",
                  command=lambda: _run_pipeline_stage(
                      lambda: auto_mgr.hk_build_remote(hk["run_id"], hk["acq_time"]), "HK Acquire"),
                  font=("Helvetica", 9, "bold")).pack(side=tk.LEFT)

        test_btn = tk.Button(btns, text="🧪 Test Trigger (Dummy)",
                             command=lambda: _run_pipeline_stage(
                                 lambda: auto_mgr.hk_build_remote(hk["run_id"], hk["acq_time"]), "HK Test"),
                             font=("Helvetica", 10, "bold"))
        self._style_button(test_btn, "move")
        test_btn.pack(side=tk.LEFT)

        save_status_lbl = ttk.Label(btns, text="", font=("Helvetica", 9),
                                    foreground=self.PALETTE["accent"])
        save_status_lbl.pack(side=tk.RIGHT, padx=(0, 14))

        ttk.Button(btns, text="Close", command=win.destroy).pack(side=tk.RIGHT, padx=(6, 0))
        # Bigger + higher-contrast than the other buttons: this is pressed far
        # more often than Cancel, and no longer auto-closes, so it needs to
        # read as "the main action" rather than a same-size peer.
        save_btn = tk.Button(btns, text="💾  Save HK Config", command=_save,
                             font=("Helvetica", 12, "bold"), padx=14, pady=6)
        self._style_button(save_btn, "start")
        save_btn.pack(side=tk.RIGHT, padx=(0, 6))

    def open_scan_params(self):
        """Opens an Admin-only window to configure scan step sizes and rest time.

        Always re-prompts for the master password, independent of the
        general Unlock Controls banner -- editing scan parameters is a
        separate, always-gated admin action, not something that should be
        implicitly allowed just because the banner happens to be unlocked
        for running scans.
        """
        if not self.controller.access_mgr.verify_password_prompt(
                "Security", "Enter Master Password (Scan Parameters):"):
            self.controller._log("[WARNING] Admin access denied for Scan Parameters.")
            return

        auto_mgr = self.controller.auto_mgr

        # ── customtkinter version (migration PoC) ───────────────────────────
        # First screen converted to CTk: rounded card, modern inputs, cleaner
        # spacing. If customtkinter is somehow missing, tell the operator
        # rather than dead-ending silently.
        if not CTK_AVAILABLE:
            messagebox.showerror("Missing dependency",
                                 "customtkinter is not installed.\n\n"
                                 "Run:  pip install customtkinter")
            return

        ctk.set_appearance_mode("light")   # match the (light) main window
        ctk.set_default_color_theme("blue")

        win = ctk.CTkToplevel(self.notebook)
        win.title("Scan Parameters (Admin)")
        win.geometry("460x760")
        win.attributes("-topmost", True)
        win.grid_columnconfigure(0, weight=1)
        win.grid_rowconfigure(2, weight=1)   # scrollable content expands; buttons stay pinned

        ctk.CTkLabel(win, text="Scan Parameters",
                     font=ctk.CTkFont(size=20, weight="bold")).grid(
            row=0, column=0, sticky="w", padx=24, pady=(22, 0))
        ctk.CTkLabel(win, text="Admin only · applies to General Scan",
                     text_color="#6c757d",
                     font=ctk.CTkFont(size=12)).grid(
            row=1, column=0, sticky="w", padx=24, pady=(0, 12))

        # All parameter cards live inside a scrollable frame so the dialog stays
        # a fixed size no matter how many Repeat-Angle rows are added.
        scroll = ctk.CTkScrollableFrame(win, fg_color="transparent")
        scroll.grid(row=2, column=0, sticky="nsew", padx=0, pady=0)
        scroll.grid_columnconfigure(0, weight=1)

        card = ctk.CTkFrame(scroll, corner_radius=14)
        card.grid(row=0, column=0, sticky="ew", padx=20, pady=(0, 16))
        card.grid_columnconfigure(1, weight=1)

        # StringVar, not DoubleVar: CTkEntry's textvariable callback calls
        # var.get() on every keystroke to check for an empty field, and
        # DoubleVar.get() raises TclError on "" instead of returning it --
        # so simply select-all+backspacing one of these fields (to retype a
        # new value) crashed the callback. Parsed to float in save_params().
        tilt_var   = tk.StringVar(value=str(auto_mgr.tilt_step))
        rot_var    = tk.StringVar(value=str(auto_mgr.rot_step))
        rest_var   = tk.StringVar(value=str(auto_mgr.rest_time))
        settle_var = tk.StringVar(value=str(auto_mgr.daq_settle_time))

        def _row(r, label, var, hint=None, width=110):
            ctk.CTkLabel(card, text=label, anchor="w",
                         font=ctk.CTkFont(size=13)).grid(
                row=r, column=0, sticky="w", padx=(16, 8), pady=(12, 2 if hint else 12))
            ctk.CTkEntry(card, textvariable=var, width=width,
                         justify="center").grid(
                row=r, column=1, sticky="e", padx=(0, 16), pady=(12, 2 if hint else 12))
            if hint:
                ctk.CTkLabel(card, text=hint, anchor="w", justify="left",
                             text_color="#8a9099", font=ctk.CTkFont(size=11)).grid(
                    row=r + 1, column=0, columnspan=2, sticky="w", padx=16, pady=(0, 8))

        _row(0, "Tilt Step (deg)", tilt_var)
        _row(1, "Rot Step (deg)", rot_var)
        _row(2, "Rest Time (sec)", rest_var)
        _row(3, "Settle Time (sec)", settle_var,
             hint="Wait after the motor arrives, before DAQ starts.")

        # ── Repeat Angles: a small Rotation | Tilt grid (3 rows by default,
        #    "+ Add row" for more). Each filled row is one (tilt, rot) recheck
        #    point revisited after every wavelength block. rot = axis rotation
        #    offset (0 = X-scan, 90 = Y-scan). Blank rows are ignored. ──────────
        rep_card = ctk.CTkFrame(scroll, corner_radius=14)
        rep_card.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 16))
        rep_card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(rep_card, text="Repeat Angles",
                     font=ctk.CTkFont(size=13, weight="bold")).grid(
            row=0, column=0, sticky="w", padx=16, pady=(12, 0))
        ctk.CTkLabel(rep_card,
                     text="Revisit these points after each wavelength block.\n"
                          "rot = axis rotation offset (0 = X-scan, 90 = Y-scan). "
                          "Blank rows are ignored.",
                     anchor="w", justify="left", text_color="#8a9099",
                     font=ctk.CTkFont(size=11)).grid(
            row=1, column=0, sticky="w", padx=16, pady=(0, 6))

        grid_holder = ctk.CTkFrame(rep_card, fg_color="transparent")
        grid_holder.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 4))
        grid_holder.grid_columnconfigure((0, 1), weight=1)
        ctk.CTkLabel(grid_holder, text="Rotation (deg)",
                     font=ctk.CTkFont(size=12, weight="bold")).grid(
            row=0, column=0, padx=(0, 6), pady=(0, 2))
        ctk.CTkLabel(grid_holder, text="Tilt (deg)",
                     font=ctk.CTkFont(size=12, weight="bold")).grid(
            row=0, column=1, padx=(6, 0), pady=(0, 2))

        self._rep_rows = []   # list of (rot_var, tilt_var), rebuilt each open

        def add_rep_row(rot="", tilt=""):
            r = len(self._rep_rows) + 1   # row 0 is the header
            rv = tk.StringVar(value=(f"{rot:g}" if isinstance(rot, (int, float)) else rot))
            tv = tk.StringVar(value=(f"{tilt:g}" if isinstance(tilt, (int, float)) else tilt))
            ctk.CTkEntry(grid_holder, textvariable=rv, justify="center").grid(
                row=r, column=0, padx=(0, 6), pady=4, sticky="ew")
            ctk.CTkEntry(grid_holder, textvariable=tv, justify="center").grid(
                row=r, column=1, padx=(6, 0), pady=4, sticky="ew")
            self._rep_rows.append((rv, tv))

        # Prefill from the current configuration (stored as (tilt, rot)), then
        # pad up to 3 empty rows so the grid always starts at 3x2.
        for (t, rr) in auto_mgr.repeat_angles:
            add_rep_row(rr, t)
        while len(self._rep_rows) < 3:
            add_rep_row("", "")

        ctk.CTkButton(rep_card, text="＋ Add row", width=110, height=28,
                      fg_color="transparent", border_width=1,
                      text_color=("#1f2430", "#e5e5e5"),
                      command=lambda: add_rep_row("", "")).grid(
            row=3, column=0, sticky="w", padx=16, pady=(6, 14))

        def save_params():
            try:
                new_tilt_step = float(tilt_var.get())
                new_rot_step = float(rot_var.get())
                new_rest_time = float(rest_var.get())
                new_settle_time = float(settle_var.get())
            except ValueError:
                messagebox.showerror(
                    "Invalid Value",
                    "Tilt Step / Rot Step / Rest Time / Settle Time must all be numbers.")
                return
            auto_mgr.tilt_step = new_tilt_step
            auto_mgr.rot_step = new_rot_step
            auto_mgr.rest_time = new_rest_time
            auto_mgr.daq_settle_time = new_settle_time

            new_angles = []   # stored as (tilt, rot)
            try:
                for (rv, tv) in self._rep_rows:
                    rs, ts = rv.get().strip(), tv.get().strip()
                    if not rs and not ts:
                        continue          # blank row -> skip
                    if not rs or not ts:
                        raise ValueError("row half-filled")
                    new_angles.append((float(ts), float(rs)))
            except ValueError:
                messagebox.showerror(
                    "Invalid Repeat Angles",
                    "Each used row needs BOTH Rotation and Tilt as numbers.\n"
                    "Leave a row completely empty to skip it.\n\n"
                    "Keeping the previous value.")
                return
            auto_mgr.repeat_angles = new_angles

            self.current_params_labels["tilt_step"].config(text=f"{auto_mgr.tilt_step}°")
            self.current_params_labels["rot_step"].config(text=f"{auto_mgr.rot_step}°")
            self.current_params_labels["rest_time"].config(text=f"{auto_mgr.rest_time}s")
            self.current_params_labels["daq_settle_time"].config(text=f"{auto_mgr.daq_settle_time}s")
            self.current_params_labels["repeat_angles"].config(text=self._format_repeat_angles(auto_mgr.repeat_angles))
            self.controller._log(
                f"[INFO] Scan params updated: Tilt {auto_mgr.tilt_step}°, Rot {auto_mgr.rot_step}°, "
                f"Rest {auto_mgr.rest_time}s, Settle {auto_mgr.daq_settle_time}s, "
                f"Repeat Angles {auto_mgr.repeat_angles}")
            win.destroy()

        btns = ctk.CTkFrame(win, fg_color="transparent")
        btns.grid(row=4, column=0, sticky="e", padx=20, pady=(0, 20))
        ctk.CTkButton(btns, text="Cancel", width=90, fg_color="transparent",
                      border_width=1, text_color=("#1f2430", "#e5e5e5"),
                      command=win.destroy).pack(side=tk.LEFT, padx=(0, 8))
        ctk.CTkButton(btns, text="💾  Save Parameters", width=170,
                      fg_color="#2e9e4f", hover_color="#268043",
                      command=save_params).pack(side=tk.LEFT)

    def update_run_info(self):
        if not hasattr(self.controller, 'config_manager') or not self.controller.config_manager:
            return

        cfg = self.controller.config_manager.get_all_variables()
        for key, var in self.qs_vars.items():
            if key in cfg:
                var.set(str(cfg[key]).strip('"'))
            else:
                var.set("")

    def _handover_notes_path(self):
        return os.path.join(self.controller.base_dir, "handover_notes.jsonl")

    def _create_handover_notes(self, parent):
        """Shift-handover notepad -- whoever is remotely operating the system can
        leave a note for the next person (what's running, what to watch for).
        Each save appends a new entry (JSON Lines file), so a full history is
        kept and survives app restarts. Layout: note editor on the left, history
        table on the right."""
        # customtkinter version. Treeview stays ttk (no CTk table); the note
        # editors are CTkTextbox (tk.Text-compatible get/insert/delete, with a
        # built-in scrollbar). CTk has no LabelFrame, so it's a CTkFrame with a
        # bold title label on top.
        if not CTK_AVAILABLE:
            return self._create_handover_notes_legacy(parent)

        frame = ctk.CTkFrame(parent)
        frame.pack(fill=tk.BOTH, expand=True, pady=(15, 0))
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(1, weight=1)

        ctk.CTkLabel(frame, text="📝  Handover Notes (for the next shift)",
                     font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=12, pady=(10, 6))

        # ── Left: note editor ───────────────────────────────────────────────
        left = ctk.CTkFrame(frame, fg_color="transparent")
        left.grid(row=1, column=0, sticky="nsew", padx=(10, 8), pady=(0, 10))
        left.rowconfigure(1, weight=1)
        left.columnconfigure(0, weight=1)

        top = ctk.CTkFrame(left, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(top, text="Your name:", font=ctk.CTkFont(size=12, weight="bold")).pack(side=tk.LEFT)
        self.handover_author_var = tk.StringVar()
        ctk.CTkEntry(top, textvariable=self.handover_author_var, width=120).pack(side=tk.LEFT, padx=(6, 14))
        self.handover_status_lbl = ctk.CTkLabel(top, text="No notes yet",
                                                font=ctk.CTkFont(size=12, weight="bold"),
                                                text_color="#007ACC")
        self.handover_status_lbl.pack(side=tk.LEFT, padx=(0, 14))
        ctk.CTkButton(top, text="💾 Save Note", width=120,
                      command=self.save_handover_note).pack(side=tk.RIGHT)

        self.handover_text = ctk.CTkTextbox(left, font=ctk.CTkFont(size=13), wrap="word")
        self.handover_text.grid(row=1, column=0, sticky="nsew", pady=(8, 0))

        # ── Right: history (List view ⇄ Detail view, swapped in place) ─────
        right = ctk.CTkFrame(frame, fg_color="transparent")
        right.grid(row=1, column=1, sticky="nsew", padx=(8, 10), pady=(0, 10))
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)

        ctk.CTkLabel(right, text="History", font=ctk.CTkFont(size=12, weight="bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 4))

        # --- List view: table of all notes (newest first). Double-click a row
        #     to open the full note in the Detail view.
        self.handover_list_frame = ctk.CTkFrame(right)
        self.handover_list_frame.grid(row=1, column=0, sticky="nsew")
        self.handover_list_frame.rowconfigure(0, weight=1)
        self.handover_list_frame.columnconfigure(0, weight=1)

        style = ttk.Style()
        style.configure("Handover.Treeview", rowheight=48, font=("Helvetica", 10))
        style.configure("Handover.Treeview.Heading", font=("Helvetica", 10, "bold"))

        cols = ("time", "author", "note")
        self.handover_tree = ttk.Treeview(self.handover_list_frame, columns=cols, show="headings",
                                          style="Handover.Treeview", height=10)
        self.handover_tree.heading("time", text="Time")
        self.handover_tree.heading("author", text="Author")
        self.handover_tree.heading("note", text="Note")
        self.handover_tree.column("time", width=130, anchor="w")
        self.handover_tree.column("author", width=80, anchor="w")
        self.handover_tree.column("note", width=260, anchor="w")
        self.handover_tree.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        self.handover_tree.bind("<Double-1>", self._on_handover_row_open)

        vsb = ttk.Scrollbar(self.handover_list_frame, orient="vertical",
                            command=self.handover_tree.yview)
        self.handover_tree.configure(yscrollcommand=vsb.set)
        vsb.grid(row=0, column=1, sticky="ns", pady=6)

        # --- Detail view: full note + Prev/Next/List navigation. Hidden until
        #     a row is opened; occupies the same grid cell as the list.
        self.handover_detail_frame = ctk.CTkFrame(right)
        self.handover_detail_frame.grid(row=1, column=0, sticky="nsew")
        self.handover_detail_frame.rowconfigure(2, weight=1)
        self.handover_detail_frame.columnconfigure(0, weight=1)
        self.handover_detail_frame.grid_remove()

        nav = ctk.CTkFrame(self.handover_detail_frame, fg_color="transparent")
        nav.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 4))
        ctk.CTkButton(nav, text="◀ Prev", width=70,
                      command=lambda: self._handover_detail_nav(-1)).pack(side=tk.LEFT)
        ctk.CTkButton(nav, text="Next ▶", width=70,
                      command=lambda: self._handover_detail_nav(+1)).pack(side=tk.LEFT, padx=(6, 0))
        ctk.CTkButton(nav, text="☰ List", width=70, fg_color="#6c757d", hover_color="#5a6268",
                      command=self._handover_show_list).pack(side=tk.LEFT, padx=(6, 0))
        self.handover_pos_lbl = ctk.CTkLabel(nav, text="", font=ctk.CTkFont(size=12, weight="bold"),
                                             text_color="#888")
        self.handover_pos_lbl.pack(side=tk.RIGHT)

        self.handover_detail_header = ctk.CTkLabel(self.handover_detail_frame, text="",
                                                   font=ctk.CTkFont(size=13, weight="bold"),
                                                   text_color="#007ACC", anchor="w")
        self.handover_detail_header.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 4))

        self.handover_detail_text = ctk.CTkTextbox(self.handover_detail_frame,
                                                   font=ctk.CTkFont(size=13), wrap="word")
        self.handover_detail_text.configure(state="disabled")
        self.handover_detail_text.grid(row=2, column=0, sticky="nsew", padx=6, pady=(0, 6))

        self._handover_entries = []        # chronological (oldest → newest)
        self._handover_detail_idx = None   # index into newest-first order

        self.load_handover_note()

    def _create_handover_notes_legacy(self, parent):
        """Original tk/ttk Handover Notes layout, used only if customtkinter
        is unavailable. Kept so the panel never disappears."""
        frame = ttk.LabelFrame(parent, text=" \U0001F4DD Handover Notes (for the next shift) ", padding=10)
        frame.pack(fill=tk.BOTH, expand=True, pady=(15, 0))
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(0, weight=1)

        left = tk.Frame(frame)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        top = tk.Frame(left)
        top.pack(fill=tk.X)
        tk.Label(top, text="Your name:", font=("Helvetica", 11, "bold")).pack(side=tk.LEFT)
        self.handover_author_var = tk.StringVar()
        tk.Entry(top, textvariable=self.handover_author_var, width=14,
                 font=("Helvetica", 11)).pack(side=tk.LEFT, padx=(4, 15))
        self.handover_status_lbl = tk.Label(top, text="No notes yet", font=("Helvetica", 10, "bold"),
                                            fg="#007ACC")
        self.handover_status_lbl.pack(side=tk.LEFT, padx=(0, 15))
        tk.Button(top, text="\U0001F4BE Save Note", bg="#17a2b8", fg="white",
                  font=("Helvetica", 11, "bold"),
                  command=self.save_handover_note).pack(side=tk.RIGHT)
        self.handover_text = tk.Text(left, height=12, font=("Helvetica", 11), wrap=tk.WORD)
        self.handover_text.pack(fill=tk.BOTH, expand=True, pady=(8, 0))

        right = tk.Frame(frame)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)
        tk.Label(right, text="History", font=("Helvetica", 11, "bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 4))
        self.handover_list_frame = tk.Frame(right)
        self.handover_list_frame.grid(row=1, column=0, sticky="nsew")
        self.handover_list_frame.rowconfigure(0, weight=1)
        self.handover_list_frame.columnconfigure(0, weight=1)
        style = ttk.Style()
        style.configure("Handover.Treeview", rowheight=48, font=("Helvetica", 10))
        style.configure("Handover.Treeview.Heading", font=("Helvetica", 10, "bold"))
        cols = ("time", "author", "note")
        self.handover_tree = ttk.Treeview(self.handover_list_frame, columns=cols, show="headings",
                                          style="Handover.Treeview", height=10)
        for c, t, w in (("time", "Time", 130), ("author", "Author", 80), ("note", "Note", 260)):
            self.handover_tree.heading(c, text=t)
            self.handover_tree.column(c, width=w, anchor="w")
        self.handover_tree.grid(row=0, column=0, sticky="nsew")
        self.handover_tree.bind("<Double-1>", self._on_handover_row_open)
        vsb = ttk.Scrollbar(self.handover_list_frame, orient="vertical",
                            command=self.handover_tree.yview)
        self.handover_tree.configure(yscrollcommand=vsb.set)
        vsb.grid(row=0, column=1, sticky="ns")

        self.handover_detail_frame = tk.Frame(right)
        self.handover_detail_frame.grid(row=1, column=0, sticky="nsew")
        self.handover_detail_frame.rowconfigure(2, weight=1)
        self.handover_detail_frame.columnconfigure(0, weight=1)
        self.handover_detail_frame.grid_remove()
        nav = tk.Frame(self.handover_detail_frame)
        nav.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        tk.Button(nav, text="◀ Prev", font=("Helvetica", 10),
                  command=lambda: self._handover_detail_nav(-1)).pack(side=tk.LEFT)
        tk.Button(nav, text="Next ▶", font=("Helvetica", 10),
                  command=lambda: self._handover_detail_nav(+1)).pack(side=tk.LEFT, padx=(6, 0))
        tk.Button(nav, text="☰ List", font=("Helvetica", 10),
                  command=self._handover_show_list).pack(side=tk.LEFT, padx=(6, 0))
        self.handover_pos_lbl = tk.Label(nav, text="", font=("Helvetica", 10, "bold"), fg="#888")
        self.handover_pos_lbl.pack(side=tk.RIGHT)
        self.handover_detail_header = tk.Label(self.handover_detail_frame, text="",
                                               font=("Helvetica", 11, "bold"),
                                               fg="#007ACC", anchor="w")
        self.handover_detail_header.grid(row=1, column=0, sticky="ew", pady=(0, 4))
        body = tk.Frame(self.handover_detail_frame)
        body.grid(row=2, column=0, sticky="nsew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        self.handover_detail_text = tk.Text(body, font=("Helvetica", 11), wrap=tk.WORD,
                                            state=tk.DISABLED)
        self.handover_detail_text.grid(row=0, column=0, sticky="nsew")
        dvsb = ttk.Scrollbar(body, orient="vertical", command=self.handover_detail_text.yview)
        self.handover_detail_text.configure(yscrollcommand=dvsb.set)
        dvsb.grid(row=0, column=1, sticky="ns")

        self._handover_entries = []
        self._handover_detail_idx = None
        self.load_handover_note()

    # ── Handover history: List ⇄ Detail navigation ─────────────────────────
    def _handover_show_list(self):
        self.handover_detail_frame.grid_remove()
        self.handover_list_frame.grid()

    def _handover_show_detail(self, idx_newest_first):
        """Show the full text of one note. Index 0 = newest note."""
        n = len(self._handover_entries)
        if n == 0:
            return
        idx = max(0, min(n - 1, idx_newest_first))
        self._handover_detail_idx = idx
        entry = self._handover_entries[n - 1 - idx]   # newest-first → chronological

        self.handover_detail_header.configure(
            text=f"{entry.get('time', '')}   ·   {entry.get('author', '')}")
        self.handover_pos_lbl.configure(text=f"{idx + 1} / {n}")
        self.handover_detail_text.configure(state="normal")
        self.handover_detail_text.delete("1.0", tk.END)
        self.handover_detail_text.insert(tk.END, entry.get("note", ""))
        self.handover_detail_text.configure(state="disabled")

        self.handover_list_frame.grid_remove()
        self.handover_detail_frame.grid()

    def _handover_detail_nav(self, step):
        """Prev(-1) = newer note, Next(+1) = older note (list is newest-first)."""
        if self._handover_detail_idx is None:
            return
        self._handover_show_detail(self._handover_detail_idx + step)

    def _on_handover_row_open(self, event):
        item = self.handover_tree.identify_row(event.y)
        if not item:
            return
        idx = self.handover_tree.index(item)   # row order == newest first
        self._handover_show_detail(idx)

    def load_handover_note(self):
        """Populate the history table from the JSONL file, newest entry first."""
        path = self._handover_notes_path()
        entries = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except FileNotFoundError:
            pass
        except Exception as e:
            self.controller._log(f"[WARNING] Failed to load handover notes: {e}")
            return

        self._handover_entries = entries
        if hasattr(self, "handover_tree"):
            self.handover_tree.delete(*self.handover_tree.get_children())
            for entry in reversed(entries):
                preview = entry.get("note", "").replace("\n", " ")
                if len(preview) > 60:
                    preview = preview[:57] + "..."
                self.handover_tree.insert("", tk.END, values=(
                    entry.get("time", ""), entry.get("author", ""), preview))
            # After a reload (e.g. a new note was saved) the detail index may be
            # stale — return to the list view so the table reflects the change.
            if hasattr(self, "handover_detail_frame"):
                self._handover_show_list()

        if entries:
            last = entries[-1]
            self.handover_status_lbl.configure(
                text=f"Last updated: {last.get('time','')} by {last.get('author','')}")

    def save_handover_note(self):
        path = self._handover_notes_path()
        author = self.handover_author_var.get().strip() or "unknown"
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        body = self.handover_text.get("1.0", tk.END).strip()
        if not body:
            messagebox.showwarning("Empty Note", "Write a note before saving.")
            return
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"time": ts, "author": author, "note": body}) + "\n")
            self.handover_status_lbl.configure(text=f"Last updated: {ts} by {author}")
            self.handover_text.delete("1.0", tk.END)
            self.load_handover_note()
            self.controller._log(f"[INFO] Handover note saved by {author}.")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save handover note: {e}")

    def update_quick_setup_live(self, dev_num, tilt, rot):
        """Keep Quick Setup's Rot/Tilt fields showing the live hardware angle for
        SN2/SN3 instead of a stale config3.h snapshot (only refreshed on config
        load otherwise). Called from the same motor-monitoring poll that feeds
        update_sn_display, so no extra hardware reads."""
        if dev_num not in (2, 3) or tilt is None or rot is None:
            return
        rot_key, tilt_key = f"RotateAngle{dev_num}", f"TiltAngle{dev_num}"
        if rot_key not in self.qs_vars or tilt_key not in self.qs_vars:
            return

        def _apply():
            if getattr(self.controller, '_shutting_down', False):
                return
            try:
                self.qs_vars[rot_key].set(f"{rot:.1f}")
                self.qs_vars[tilt_key].set(f"{tilt:.1f}")
            except tk.TclError:
                pass
        self.notebook.after(0, _apply)

    # ── Live hardware -> Quick Setup (Laser / Wavelength / HV) ──────────────
    # Rot/Tilt already track the motors (update_quick_setup_live above). These
    # three used to be hand-typed, so a forgotten edit silently wrote a WRONG
    # Wavelength/Laser/HV into every run's metadata. Now they mirror what the
    # hardware actually reports.
    HV_DB_PATH = "/home/precalkor/Integrated_Control_SW/HV_Control_SW/monitoring_log.db"
    HV_STALE_AFTER_S = 180        # ignore DB rows older than this (HV app down)
    QS_HW_POLL_MS = 3000
    # Live readback jitters around the setpoint (e.g. 1669.2 V for a 1670 V
    # setting). Without a deadband the field would flip 1669<->1670 forever and
    # spam the log, so only adopt a reading that differs by more than this --
    # i.e. a real setting change, not measurement noise.
    QS_TOLERANCE = {"HV1": 5.0, "HV2": 5.0, "HV3": 5.0, "Laser": 1.0}

    def _read_live_laser(self):
        """-> (wavelength_nm:str, total_mA:str) for the single LD that is ON,
        or (None, None) when 0 or >1 are on (ambiguous -> don't touch the UI).
        'Laser' is pulse+bias, matching what _update_laser_config writes."""
        lm = getattr(self.controller, 'laser_mgr', None)
        if not lm:
            return None, None
        on = []
        for wl in getattr(lm, 'wavelengths', []):
            inst = lm.laser_instances.get(wl)
            if not inst or not inst.is_connected():
                continue
            if lm.comm_error_flags.get(wl, False):
                continue
            try:
                if inst.status.get('ld_on', False):
                    on.append((wl, inst.status))
            except Exception:
                continue
        if len(on) != 1:
            return None, None
        wl, st = on[0]
        try:
            total = float(st.get('pulse', 0) or 0) + float(st.get('bias', 0) or 0)
        except (TypeError, ValueError):
            return None, None
        # Readback is fractional (164.98 for a 165 mA setting); 1 decimal keeps
        # genuinely fractional settings while rendering the common case as "165".
        return re.sub(r'\D', '', wl), f"{round(total, 1):g}"

    def _read_live_hv(self):
        """-> {'HV1':str,'HV2':str,'HV3':str} from HV_Control_SW's monitoring DB
        (Ch0/Ch1/Ch2 -> HV1/HV2/HV3), or {} if unavailable/stale. Read-only
        connection so this can never disturb the HV app's own writes."""
        import sqlite3
        if not os.path.exists(self.HV_DB_PATH):
            return {}
        try:
            con = sqlite3.connect(f"file:{self.HV_DB_PATH}?mode=ro", uri=True, timeout=1.0)
            try:
                row = con.execute(
                    "SELECT timestamp, Ch0_V, Ch1_V, Ch2_V FROM monitoring_data "
                    "ORDER BY timestamp DESC LIMIT 1").fetchone()
            finally:
                con.close()
        except Exception:
            return {}
        if not row or row[0] is None:
            return {}
        try:
            ts = datetime.fromisoformat(row[0])
            if (datetime.now() - ts).total_seconds() > self.HV_STALE_AFTER_S:
                return {}          # HV monitoring app not running -> keep last value
        except Exception:
            return {}
        out = {}
        for key, val in zip(("HV1", "HV2", "HV3"), row[1:]):
            if val is None:
                continue
            try:
                v = float(val)
            except (TypeError, ValueError):
                continue
            if v <= 0:             # channel off / not powered -> don't overwrite
                continue
            out[key] = f"{round(v)}"
        return out

    def _poll_quick_setup_hardware(self):
        """Periodic: push live Laser/Wavelength/HV into Quick Setup. Runs on the
        Tk main thread (after-loop), so no cross-thread widget access."""
        try:
            if getattr(self.controller, '_shutting_down', False):
                return
            updates = {}
            wl_nm, laser_ma = self._read_live_laser()
            if wl_nm:
                updates["Wavelength"] = wl_nm
                updates["Laser"] = laser_ma
            updates.update(self._read_live_hv())

            changed = []
            for key, val in updates.items():
                var = self.qs_vars.get(key)
                if var is None:
                    continue
                current = var.get().strip()
                if current == val:
                    continue
                tol = self.QS_TOLERANCE.get(key)
                if tol is not None and current:
                    try:                      # within the deadband -> readback noise, keep as-is
                        if abs(float(current) - float(val)) <= tol:
                            continue
                    except ValueError:
                        pass                  # unparseable field -> let the live value replace it
                var.set(val)
                changed.append(f"{key}: {current or '(blank)'} -> {val}")
            if changed:
                self.controller._log("[INFO] Quick Setup synced from hardware: "
                                     + ", ".join(changed)
                                     + "   (press 'Save Settings' to write config3.h)")
        except Exception:
            pass   # a monitoring convenience must never break the UI loop
        finally:
            try:
                self.notebook.after(self.QS_HW_POLL_MS, self._poll_quick_setup_hardware)
            except tk.TclError:
                pass

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
        colors = {"wait": "#e9ecef", "move": "#ffc107", "daq": "#007bff", "done": "#28a745",
                  "error": "#dc3545"}
        if (sn, tilt, axis) in self.cells:
            text = ("MOV" if status == "move" else "DAQ" if status == "daq" else
                    "OK" if status == "done" else "ERR" if status == "error" else "-")
            widgets = self.cells[(sn, tilt, axis)]
            fg = "white" if status == "error" else "black"
            def _apply():
                for w in widgets:
                    try:
                        w.config(bg=colors.get(status, "#e9ecef"), text=text, fg=fg)
                    except tk.TclError:
                        pass
                self._update_matrix_tab_color()
            # [핵심 수정] 백그라운드 스레드에서 UI를 변경할 때 멈추는(Deadlock) 현상을 원천 차단합니다.
            self.notebook.after(0, _apply)

    def _update_matrix_tab_color(self):
        """Color-code the 'Scan Progress Matrix' tab itself with the same
        color language as the cells (see update_cell's `colors` map): red if
        any point errored, blue while points are actively moving/taking data,
        green once every point is done, gray/idle otherwise. ttk.Notebook has
        no per-tab background API, so this is done via a small solid-color
        PhotoImage set as the tab's icon (compound=LEFT keeps the text)."""
        if not hasattr(self, 'matrix_tab') or not hasattr(self, 'cells'):
            return
        texts = set()
        for widgets in self.cells.values():
            for w in widgets:
                try:
                    texts.add(w.cget("text"))
                except tk.TclError:
                    pass
                break   # one representative widget is enough per cell
        if "ERR" in texts:
            color = "#dc3545"
        elif "MOV" in texts or "DAQ" in texts:
            color = "#007bff"
        elif texts and texts == {"OK"}:
            color = "#28a745"
        else:
            color = "#e9ecef"

        if not hasattr(self, '_matrix_tab_icons'):
            self._matrix_tab_icons = {}
        icon = self._matrix_tab_icons.get(color)
        if icon is None:
            icon = tk.PhotoImage(width=12, height=12)
            icon.put(color, to=(0, 0, 12, 12))
            self._matrix_tab_icons[color] = icon
        try:
            self.upper_notebook.tab(self.matrix_tab, image=icon, compound=tk.LEFT)
        except tk.TclError:
            pass

    def confirm_and_reset_angles(self):
        # Traceability: there was no log evidence Reset Angle's own sequence
        # (_reset_sequence -> "Reset Phase 1/2") ever started on a day it was
        # reported as "not working" -- add explicit click/confirm logging so a
        # silent no-op (locked, dialog dismissed, etc.) is diagnosable instead
        # of indistinguishable from a genuine hang.
        self.controller._log("[INFO] Reset angle button clicked.")
        msg = (
            "⚠️ WARNING: Abort & Hardware Origin Reset\n\n"
            "This will ABORT the current run and physically move both SN2 and SN3 back to the origin (0.0°).\n"
            "The movement may take up to 30~60 seconds.\n\n"
            "Do you want to proceed and start over?"
        )

        if messagebox.askyesno("Confirm Reset", msg):
            self.controller._log("[INFO] Reset angle confirmed by user -- starting.")
            if hasattr(self.controller, 'auto_mgr') and hasattr(self.controller.auto_mgr, 'abort_run'):
                self.controller.auto_mgr.abort_run()

            self.reset_matrix()

            if hasattr(self.controller, 'auto_mgr') and hasattr(self.controller.auto_mgr, 'reset_all_angles'):
                self.controller.auto_mgr.reset_all_angles()

            self.add_auto_log("🔄 Run Aborted & Origin Reset Initiated: Moving SN2 & SN3 to 0.0°...")
        else:
            self.controller._log("[INFO] Reset angle cancelled by user.")

    def reset_matrix(self):
        # self.cells maps each key to a LIST of widgets (inline table + popup copy),
        # so iterate the list. Previously this called .config() on the list itself,
        # raising AttributeError -- which aborted confirm_and_reset_angles() before
        # reset_all_angles() ran, i.e. "Reset angle" silently did nothing.
        for widgets in self.cells.values():
            for cell in (widgets if isinstance(widgets, list) else [widgets]):
                cell.config(bg="#e9ecef", text="-", fg="black")
        self.log_display.delete('1.0', tk.END)
        self.eta_label.config(text="ETA: --:--:--")
        self._update_matrix_tab_color()
        # Reset Angle is an explicit "back to scratch" action (re-homes both
        # stages to 0deg) -- the Live Scan plot should clear along with
        # everything else here rather than keep showing points from the
        # attempt just abandoned (2026-08-22, user asked what Reset does to
        # it; previously: nothing, which wasn't intentional, just missed).
        if hasattr(self, 'live_scan_view'):
            self.live_scan_view.reset()

    # ── Laser Sequence panel helpers (multi-wavelength scan) ────────────────
    def _seed_laser_seq_currents(self):
        """One-shot: pre-fill Bias/Pulse entries from the Laser tab's last-used
        values (only fields still at the 0.0 placeholder are touched)."""
        laser_vars = getattr(getattr(self.controller, 'ui', None), 'laser_vars', None)
        if not laser_vars:
            return
        for wl, v in self.laser_seq_vars.items():
            lv = laser_vars.get(wl)
            if not lv:
                continue
            try:
                if v["bias"].get() in ("", "0.0"):
                    v["bias"].set(f"{lv['bias_set'].get():.1f}")
                if v["pulse"].get() in ("", "0.0"):
                    v["pulse"].set(f"{lv['pulse_set'].get():.1f}")
            except (KeyError, tk.TclError):
                continue

    def _refresh_laser_seq_dots(self):
        """Live 🟢/🔴 connection dot per laser (reuses laser_mgr's instances)."""
        if getattr(self.controller, '_shutting_down', False):
            return
        lm = getattr(self.controller, 'laser_mgr', None)
        for wl, dot in self.laser_seq_dots.items():
            ok = False
            try:
                inst = lm.laser_instances.get(wl) if lm else None
                ok = bool(inst and inst.is_connected())
            except Exception:
                ok = False
            try:
                dot.config(fg="#2f9e44" if ok else "#dc3545")
            except tk.TclError:
                return
        self.notebook.after(3000, self._refresh_laser_seq_dots)

    def _on_scan_mode_change(self):
        """Dark mode runs a single no-wavelength-loop scan (see
        AutomationManager._scan_sequence's `blocks = seq if seq else [None]`
        legacy path), so the Laser Sequence panel doesn't apply -- gray it out
        to make that explicit rather than leaving stale checkboxes selectable."""
        state = tk.DISABLED if self.scan_mode_var.get() == "dark" else tk.NORMAL
        for w in self.laser_seq_widgets:
            try:
                w.config(state=state)
            except tk.TclError:
                pass

    def get_laser_sequence(self):
        """[(wl, bias, pulse)] for checked lasers in fixed 405→375→450→473
        order. Returns None if a checked laser has a non-numeric current.
        Persists the panel state so it survives a restart (values are 'used'
        the moment a scan starts)."""
        seq = []
        for wl in ["405nm", "375nm", "450nm", "473nm"]:
            v = self.laser_seq_vars.get(wl)
            if not v or not v["on"].get():
                continue
            try:
                seq.append((wl, float(v["bias"].get()), float(v["pulse"].get())))
            except ValueError:
                return None
        self._save_laser_seq()
        return seq

    def _on_laser_seq_save_click(self):
        """Manual Save button: persist current values immediately (values are
        also auto-saved at scan start via get_laser_sequence(), but the user
        asked for an explicit control so they don't have to start a scan just
        to lock in a change)."""
        self._save_laser_seq()
        self.laser_seq_save_lbl.config(text="Saved ✓")
        self.notebook.after(2000, lambda: self.laser_seq_save_lbl.config(text=""))

    def _laser_seq_path(self):
        return os.path.join(self.controller.base_dir, "laser_sequence.json")

    def _save_laser_seq(self):
        """Persist checkbox + Bias/Pulse for every wavelength (last-used state)."""
        try:
            data = {wl: {"on": v["on"].get(), "bias": v["bias"].get(), "pulse": v["pulse"].get()}
                    for wl, v in self.laser_seq_vars.items()}
            with open(self._laser_seq_path(), "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            self.controller._log(f"[WARNING] Failed to save laser sequence: {e}")

    def _load_laser_seq(self):
        """Restore the panel from laser_sequence.json. Returns True if a saved
        file was applied, False if none exists (caller then seeds from Laser tab)."""
        path = self._laser_seq_path()
        if not os.path.exists(path):
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return False
        applied = False
        for wl, v in self.laser_seq_vars.items():
            saved = data.get(wl)
            if not saved:
                continue
            try:
                v["on"].set(bool(saved.get("on", v["on"].get())))
                if saved.get("bias", "") != "":
                    v["bias"].set(str(saved["bias"]))
                if saved.get("pulse", "") != "":
                    v["pulse"].set(str(saved["pulse"]))
                applied = True
            except (tk.TclError, ValueError):
                continue
        return applied

    def _open_matrix_popup(self):
        """Open (or refocus) a large Toplevel showing both scan matrices at
        full size. Built as a second, independent set of cell widgets that
        mirror the same (sn, tilt, axis) keys as the main dashboard table —
        update_cell()/reset_matrix_cells() update both copies together."""
        if self._matrix_popup is not None and self._matrix_popup.winfo_exists():
            self._matrix_popup.lift()
            self._matrix_popup.focus_force()
            return

        popup = tk.Toplevel(self.notebook)
        popup.title("Scan Progress Matrix")
        popup.geometry("1400x520")
        self._matrix_popup = popup
        popup_frames = []   # (sn, frame) added to self.matrix_frames by this popup
        popup_cells = []    # (key, widget) added to self.cells by this popup

        def _on_close():
            self._matrix_popup = None
            for sn, f in popup_frames:
                lst = self.matrix_frames.get(sn)
                if lst and f in lst:
                    lst.remove(f)
            for key, w in popup_cells:
                lst = self.cells.get(key)
                if lst and w in lst:
                    lst.remove(w)
            popup.destroy()
        popup.protocol("WM_DELETE_WINDOW", _on_close)

        canvas = tk.Canvas(popup, highlightthickness=0)
        vbar = ttk.Scrollbar(popup, orient="vertical", command=canvas.yview)
        scroll_frame = ttk.Frame(canvas)
        scroll_frame.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win_id = canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win_id, width=e.width))
        canvas.configure(yscrollcommand=vbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vbar.pack(side=tk.RIGHT, fill=tk.Y)

        for sn in [self.sn2_val, self.sn3_val]:
            f = ttk.LabelFrame(scroll_frame, text=f" {sn} Scan Progress Matrix ", padding=14)
            f.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=10, padx=6)
            self.matrix_frames.setdefault(sn, []).append(f)
            popup_frames.append((sn, f))
            n_before = {k: len(v) for k, v in self.cells.items()}
            # Popup keeps the original wide/short orientation (tilt-as-
            # columns) instead of the main dashboard's tall vertical one --
            # the vertical list scrolled forever here and the user asked for
            # the old layout back (2026-08-15, "기존처럼 가로로 하는게 나을 것
            # 같은데... 너무 길다").
            self._build_horizontal_table(f, sn, big=False, vertical=False)
            for key, widgets in self.cells.items():
                if len(widgets) > n_before.get(key, 0):
                    new_w = widgets[-1]
                    popup_cells.append((key, new_w))
                    # Mirror current state of the dashboard cell so the popup
                    # doesn't open showing a blank "-" mid-scan.
                    src = widgets[0]
                    try:
                        new_w.config(bg=src.cget("bg"), text=src.cget("text"))
                    except tk.TclError:
                        pass

    def set_matrix_wavelength(self, text):
        """Show the active wavelength block in the Scan Progress Matrix titles,
        e.g. 'EM5370 Scan Progress Matrix — 405nm (1/3)'."""
        for sn, frames in getattr(self, 'matrix_frames', {}).items():
            for f in frames:
                try:
                    f.config(text=f" {sn} Scan Progress Matrix — {text} ")
                except tk.TclError:
                    pass

    def reset_matrix_cells(self):
        """Clear only the progress cells (keeps logs/ETA) — used between
        wavelength blocks of a multi-wavelength scan."""
        for widgets in self.cells.values():
            for cell in widgets:
                try:
                    cell.config(bg="#e9ecef", text="-", fg="black")
                except tk.TclError:
                    pass
        self._update_matrix_tab_color()

    def update_dummy_status(self):
        state = "ENABLED" if self.dummy_var.get() else "DISABLED"
        self.add_auto_log(f"🛠️ Test Run Mode: {state}")

    def update_stop_button(self, is_running):
        if is_running:
            self.btn_stop_run.config(text="⏸ Pause", bg=self.PALETTE["warn"], fg="#412402")
        else:
            self.btn_stop_run.config(text="⏯ Continue", bg=self.PALETTE["move"], fg="white")

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
            self._lock_daq_backend_toggle(is_running)
            if is_running:
                self.btn_start.config(text="⏳ RUNNING...", bg=self.PALETTE["neutral"], state=tk.DISABLED)
                self.btn_stop_run.config(text="⏸ Pause", bg=self.PALETTE["warn"], fg="#412402", state=tk.NORMAL)
                self.btn_reset.config(state=tk.DISABLED)
                display_txt = status_text if status_text else "SYSTEM STATUS: SCANNING..."
                self.scan_status_label.config(text=display_txt, foreground=self.PALETTE["danger"])

                if hasattr(self.controller, 'ui') and 'run_daq' in self.controller.ui.buttons:
                    self.controller.ui.buttons['run_daq'].config(state=tk.DISABLED, text="2. Run DAQ (Scanning)")
            else:
                self.btn_start.config(text="▶ Start run", bg=self.PALETTE["start"], state=tk.NORMAL)
                self.btn_stop_run.config(text="⏸ Pause", bg=self.PALETTE["warn"], fg="#412402", state=tk.DISABLED)
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

        cable = self.controller.config_manager.get_config_value(f"direction{dev_num}") or ""
        side = injection_side_label(cable, rot, tilt)
        side_str = f", Injects: {side}" if side else ""

        if hasattr(self, 'sn_labels') and sn in self.sn_labels:
            # Guard against the widget being destroyed before this queued callback
            # fires (e.g. during app shutdown) → avoids TclError "invalid command name".
            tilt_locked = (tilt is not None and abs(tilt) > 0.5)

            def _apply_sn(sn=sn, t_str=t_str, r_str=r_str, side_str=side_str, dev=dev_num, locked=tilt_locked):
                if getattr(self.controller, '_shutting_down', False):
                    return
                try:
                    self.sn_labels[sn].config(
                        text=f"{sn} | Status -> Tilt: {t_str}°, Rot: {r_str}°{side_str}")
                    # Enforce rotation interlock: disable Move Rot when tilt != 0
                    if dev in getattr(self, 'manual_rot_buttons', {}):
                        btn_rot, lock_lbl = self.manual_rot_buttons[dev]
                        if locked:
                            btn_rot.config(state=tk.DISABLED, bg=self.PALETTE["neutral"])
                            lock_lbl.pack(side=tk.LEFT, padx=(6, 0))
                        else:
                            btn_rot.config(state=tk.NORMAL, bg=self.PALETTE["move"])
                            lock_lbl.pack_forget()
                except tk.TclError:
                    pass
            self.notebook.after(0, _apply_sn)

    def sync_current_to_inputs(self, sn):
        """Reads hardware angles, updates config3.h first, then syncs to the GUI Helper."""
        if not hasattr(self, 'sn_labels') or sn not in self.sn_labels: return
        status_text = self.sn_labels[sn].cget("text")

        try:
            # Regex instead of split() -- the label may have trailing text after
            # Rot (e.g. ", Injects: X+"), which broke split(", Rot: ")[1] parsing.
            m_tilt = re.search(r'Tilt:\s*(-?[\d.]+)\s*°', status_text)
            m_rot = re.search(r'Rot:\s*(-?[\d.]+)\s*°', status_text)
            if not m_tilt or not m_rot:
                raise ValueError(f"could not parse status text: {status_text!r}")
            tilt_val = float(m_tilt.group(1))
            rot_val = float(m_rot.group(1))

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
            config_path = "/home/precalkor/Integrated_Control_SW/DAQ_Control_SW/config3.h"
            
            with open(config_path, 'r', encoding='utf-8') as f:
                content = f.read()
                
            dev_num = "2" if sn == self.sn2_val else "3"
            
            tilt_int = int(round(tilt))
            rot_int = int(round(rot))
            
            content = re.sub(rf'const std::string TiltAngle{dev_num}\s*=\s*".*";', f'const std::string TiltAngle{dev_num} = "{tilt_int}";', content)
            content = re.sub(rf'const std::string RotateAngle{dev_num}\s*=\s*".*";', f'const std::string RotateAngle{dev_num} = "{rot_int}";', content)

            # Atomic write (temp + os.replace): this runs from the scan thread
            # right before run_daq launches the shell DAQ (which re-reads
            # config3.h via parse_config), so a torn/truncated write here would
            # corrupt the angles recorded for the run -- or every later run.
            tmp = config_path + ".tmp"
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, config_path)

        except Exception as e:
            self.controller._log(f"[WARNING] Failed to update config file for {sn}: {e}")

    def set_buttons_state(self, state):
        tk_state = tk.NORMAL if state else tk.DISABLED

        if state:
            colors = {
                "start": self.PALETTE["start"],
                "reset": self.PALETTE["warn"],
                "abort": self.PALETTE["danger"],
                "get": self.PALETTE["accent"],
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
        """Rebuild the Scan History tree. As the lab accumulates months of
        history_*.json files, re-opening and json.load()-ing every single one
        on the main thread on every refresh gets linearly slower forever --
        same shape of bug as the point-card QE lookup fixed earlier. Parsed
        files are now cached by (path, mtime) so a refresh only re-reads
        files that are new or have changed, and the glob+read itself runs in
        a background thread so even a first-ever cold read can't stall the
        GUI."""
        import threading
        if not hasattr(self, "_history_json_cache"):
            self._history_json_cache = {}   # path -> (mtime, data)
        self._history_refresh_token = getattr(self, "_history_refresh_token", 0) + 1
        token = self._history_refresh_token

        def worker():
            import os
            import glob
            import json
            history_dir = os.path.join(self.controller.base_dir, "LOG", "ScanHistory")
            if not os.path.exists(history_dir):
                self.notebook.after(0, lambda: apply_rows([]))
                return

            # Only the scan-summary records ("history_<ts>.json", written by
            # save_scan_history()) belong here. LOG/ScanHistory also holds
            # "scanmap_<date>.json" (per-point angle->RAW-file lookup, written
            # by _record_scan_point()/_mark_point_error()) -- those have no
            # date/end_time/shifter/status keys, so globbing "*.json" showed
            # them as blank "None" rows in this list.
            files = glob.glob(os.path.join(history_dir, "history_*.json"))
            files.sort(reverse=True)  # 최신순

            rows = []
            for f in files:
                try:
                    mtime = os.path.getmtime(f)
                    cached = self._history_json_cache.get(f)
                    if cached and cached[0] == mtime:
                        data = cached[1]
                    else:
                        with open(f, 'r', encoding='utf-8') as json_file:
                            data = json.load(json_file)
                        self._history_json_cache[f] = (mtime, data)
                    rows.append((f, data.get("date"), data.get("end_time"),
                                data.get("shifter"), data.get("status")))
                except Exception:
                    pass
            self.notebook.after(0, lambda: apply_rows(rows))

        def apply_rows(rows):
            if token != self._history_refresh_token:
                return  # a newer refresh started while this one was loading
            for item in self.history_tree.get_children():
                self.history_tree.delete(item)
            for f, date, end_time, shifter, status in rows:
                self.history_tree.insert("", tk.END, values=(date, end_time, shifter, status), tags=(f,))

        threading.Thread(target=worker, daemon=True).start()

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


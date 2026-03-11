# ui_manager.py
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, font
import os
import json
import math 
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from image_viewer import ImageViewer
from config_window import ConfigWindow 
from datetime import datetime
import requests 
import threading
import io 
import time 
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from PIL import Image, ImageTk

class UIManager:
    def __init__(self, master, controller):
        self.master = master
        self.controller = controller

        if hasattr(self.controller, 'access_mgr'):
            self.unlock_btn = tk.Button(master, text="🔒 Unlock Controls",
                                        command=self.controller.request_control_unlock,
                                        bg="#f0ad4e", fg="black", font=("Helvetica", 10, "bold"))
        else:
            self.unlock_btn = None

        self.default_font = font.nametofont("TkDefaultFont")
        self.default_font.configure(size=11) 
        self.master.option_add("*Font", self.default_font)
    
        self.laser_vars = {
                "ld_status": tk.StringVar(value="OFF"),
                "tec_status": tk.StringVar(value="OFF"),
                "temp": tk.StringVar(value="--.- °C"),
                "bias_live": tk.StringVar(value="---.- mA"),
                "pulse_live": tk.StringVar(value="---.- mA"),
                "check_interval": tk.StringVar(value="1s"),
                "bias_set": tk.DoubleVar(value=0.0),
                "pulse_set": tk.DoubleVar(value=0.0),
                "trigger_mode": tk.StringVar(value="External"),
                "freq_hz": tk.StringVar(value="10000000")
                }

        self.ups_vars = {
                "conn_status": tk.StringVar(value="Disconnected"),
                "input_volt": tk.StringVar(value="--- V"),
                "output_volt": tk.StringVar(value="--- V"),
                "batt_level": tk.IntVar(value=0),
                "load_level": tk.IntVar(value=0),
                "frequency": tk.StringVar(value="-- Hz"),
                "status_msg": tk.StringVar(value="Unknown")
                 }

        style = ttk.Style()
        style.configure("TLabel", font=("Helvetica", 11)) 
        style.configure("TButton", font=("Helvetica", 11, "bold")) 
        style.configure("TLabelframe.Label", font=("Helvetica", 12, "bold")) 
        
        self.tab_led_green = tk.PhotoImage(width=10, height=10)
        self.tab_led_green.put(("#28a745",), to=(0, 0, 10, 10))
        self.tab_led_red = tk.PhotoImage(width=10, height=10)
        self.tab_led_red.put(("#dc3545",), to=(0, 0, 10, 10))
        
        self.data_size_var = tk.StringVar(value="Calculating...")
        self.ext_data_size_var = tk.StringVar(value="Calculating...")

        self.is_dark_mode = False
        self.colors = {
            "light": {
                "bg": "#f0f0f0", 
                "fg": "#000000", 
                "frame_bg": "#ffffff", 
                "text_bg": "#ffffff", 
                "text_fg": "#212529", 
                "accent": "blue"      
            },
            "dark": {
                "bg": "#2d2d2d", 
                "fg": "#ffffff", 
                "frame_bg": "#3d3d3d", 
                "text_bg": "#1e1e1e", 
                "text_fg": "#d4d4d4", 
                "accent": "#00bcff"    
            }
        }

        self.ups_value_labels = []


        self.run_mode = tk.StringVar(value="laser")
        self.run_number_var = tk.StringVar(value="1")
        self.status_indicators = {}
        self.buttons = {}
        self.image_viewer_window = None 

        self._create_menubar()
        self.create_widgets()


    def _create_menubar(self):
        menubar = tk.Menu(self.master)
        self.master.config(menu=menubar)
        file_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="File", menu=file_menu)
        file_menu.add_command(label="Set Config Path...", command=self.controller.select_and_set_config_path)
        file_menu.add_command(label="Open Configuration", command=self.open_config_window)

        terminal_menu = tk.Menu(file_menu, tearoff=0)
        file_menu.add_cascade(label="Select Terminal", menu=terminal_menu)

        self.terminal_var = tk.StringVar(value=self.controller.terminal_preference)

        terminal_menu.add_radiobutton(
                label="gnome-terminal (Default for Local)",
                variable=self.terminal_var,
                value='gnome-terminal',
                command=lambda: self.controller.set_terminal_preference('gnome-terminal')
                )
        terminal_menu.add_radiobutton(
                label="xterm (Recommended for SSH)",
                variable=self.terminal_var,
                value='xterm',
                command=lambda: self.controller.set_terminal_preference('xterm')
                )

        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.master.quit)
        help_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Help", menu=help_menu)
        help_menu.add_command(label="About", command=self.show_about)

        view_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="View", menu=view_menu)
        view_menu.add_command(label="Toggle Dark Mode 🌙", command=self.toggle_theme)

    def show_about(self):
        messagebox.showinfo("About DAQ Control (2026. 03. 10)",
                            """DAQ Control Application 
                      Made by Korean group (CNU, Junkyo OH)
                      If you have any problem, You can contact to here
                      gs1706@naver.com or via Slack """)
    """ UPDATE 2026 03 10 """


    def create_widgets(self):
        self.main_container = tk.Frame(self.master)
        self.main_container.pack(fill=tk.BOTH, expand=True)

        self.main_notebook = ttk.Notebook(self.main_container)
        self.main_notebook.pack(fill=tk.BOTH, expand=True)

        self.daq_main_frame = ttk.Frame(self.main_notebook)
        self.main_notebook.add(self.daq_main_frame, text=" DAQ System ")
        
        self.laser_main_frame = ttk.Frame(self.main_notebook)
        self.main_notebook.add(self.laser_main_frame, text=" Laser Control ")

        self._create_status_dashboard(self.daq_main_frame)

        paned_window = ttk.PanedWindow(self.daq_main_frame, orient=tk.HORIZONTAL)
        paned_window.pack(fill=tk.BOTH, expand=True)
        
        left_scroll_container = ttk.Frame(paned_window)
        paned_window.add(left_scroll_container, weight=0)

        left_canvas = tk.Canvas(left_scroll_container, width=450, highlightthickness=0)
        left_vbar = ttk.Scrollbar(left_scroll_container, orient="vertical", command=left_canvas.yview)
        
        left_pane = ttk.Frame(left_canvas, padding="10")
        
        left_canvas.create_window((0, 0), window=left_pane, anchor="nw", width=450)
        left_canvas.configure(yscrollcommand=left_vbar.set)

        left_pane.bind("<Configure>", lambda e: left_canvas.configure(scrollregion=left_canvas.bbox("all")))

        left_canvas.pack(side="left", fill="both", expand=True)
        left_vbar.pack(side="right", fill="y")

        left_canvas.bind("<Enter>", lambda e: (
            left_canvas.bind_all("<Button-4>", lambda ev: left_canvas.yview_scroll(-1, "units")),
            left_canvas.bind_all("<Button-5>", lambda ev: left_canvas.yview_scroll(1, "units"))
        ))
        left_canvas.bind("<Leave>", lambda e: (
            left_canvas.unbind_all("<Button-4>"),
            left_canvas.unbind_all("<Button-5>")
        ))

        self._create_connection_status_frame(left_pane)
        self._create_run_control_frame(left_pane)
        self._create_dynamic_buttons_frame(left_pane, "Execute Scripts", "scripts")
        self._create_dynamic_buttons_frame(left_pane, "View", "view")
        self._create_path_viewer_frame(left_pane) 

        # 우측 패널: PMT Status 및 데이터 목록
        right_pane = ttk.Frame(paned_window, padding=(0, 10, 10, 10))
        paned_window.add(right_pane, weight=3)

        # 우측 내부 Notebook (Helper, Data Files, Log)
        self.notebook = ttk.Notebook(right_pane)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        ####### Update 3. 11
        from managers.ui_automation import AutomationUI
        self.auto_ui = AutomationUI(self.notebook, self.controller)
        ####### Update 3. 11

        # Tab 1: PMT Rotation Helper (이제 스크롤 없이 바로 보임)
        config_tab = ttk.Frame(self.notebook, padding=(10, 10, 10, 10))
        self.notebook.add(config_tab, text="PMT Rotation Helper")
        self._create_status_frame(config_tab)

        # Tab 2: Data Files (Treeview 자체 스크롤바 사용)
        data_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(data_tab, text="Data Files")
        self._create_data_viewer(data_tab)

        # Tab 3: Log (ScrolledText 자체 스크롤바 사용)
        log_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(log_tab, text="Log")
        self._create_log_viewer(log_tab)

        #  2: Laser Control
        self._create_laser_control_tab(self.laser_main_frame)

        # main tab 3
        self._create_web_monitor_tab(self.main_notebook)
        
        # 4: UPS Status
        self.ups_main_frame = ttk.Frame(self.main_notebook)
        self.main_notebook.add(self.ups_main_frame, text=" UPS Status ")
        self._create_ups_monitoring_tab(self.ups_main_frame)

        # 5: Emergency Contact
        self.contact_frame = ttk.Frame(self.main_notebook)
        self.main_notebook.add(self.contact_frame, text=" ☎️ Emergency ")
        self._create_contact_tab(self.contact_frame)

    def on_config_loaded(self):
        self._update_pmt_status_and_helper() 
        self.update_config_display()
        self.update_path_display()
        if hasattr(self, 'data_tree'):
            self.update_data_viewer(force_refresh=True)

    # ui_manager.py

    def toggle_theme(self):
        self.is_dark_mode = not self.is_dark_mode
        theme = "dark" if self.is_dark_mode else "light"
        c = self.colors[theme]

        style = ttk.Style()
        style.theme_use('clam') 
        
        style.configure(".", background=c["bg"], foreground=c["fg"])
        style.configure("TFrame", background=c["bg"])
        style.configure("TLabel", background=c["bg"], foreground=c["fg"])
        style.configure("TLabelframe", background=c["bg"])
        style.configure("TLabelframe.Label", background=c["bg"], foreground=c["fg"])
        
        style.configure("TNotebook", background=c["bg"], borderwidth=0)
        style.configure("TNotebook.Tab", background=c["frame_bg"], foreground=c["fg"], padding=[10, 5])
        style.map("TNotebook.Tab", background=[("selected", c["accent"])], foreground=[("selected", "white")])

        style.configure("Treeview", 
                        background=c["text_bg"], 
                        foreground=c["text_fg"], 
                        fieldbackground=c["text_bg"])
        style.configure("Treeview.Heading", 
                        background=c["frame_bg"], 
                        foreground=c["fg"])
        style.map("Treeview", background=[('selected', '#4b4b4b')])

        self.master.config(bg=c["bg"])
        self.log_text.config(bg=c["text_bg"], fg=c["text_fg"], insertbackground=c["fg"])
        #self.config_text.config(bg=c["text_bg"], fg=c["text_fg"], insertbackground=c["fg"])
        if hasattr(self, 'laser_log_text'):
            self.laser_log_text.config(bg=c["text_bg"], fg=c["text_fg"])

        accent_color = c["accent"]
        for lbl in self.ups_value_labels:
            lbl.config(foreground=accent_color)

        if hasattr(self, 'data_size_label'):
            self.data_size_label.config(foreground=accent_color)
            self.data_size_label2.config(foreground=accent_color)

        for indicator in self.status_indicators.values():
            indicator["canvas"].config(bg=c["bg"])
        
        self.outlet_canvas.config(bg=c["bg"])
        self._update_pmt_status_and_helper() 

        self.controller.update_plots_theme(self.is_dark_mode)

    def _create_connection_status_frame(self, parent):
        frame = ttk.LabelFrame(parent, text="Connection Status", padding="10")
        frame.pack(fill=tk.X, pady=(0, 3), padx=5)

        self.connection_status_label = ttk.Label(frame, text="Checking...", font=("Helvetica", 10, "bold"))
        self.connection_status_label.pack(pady=(0, 3))

        ip_frame = ttk.Frame(frame)
        ip_frame.pack(fill=tk.X, padx=3)
        ip_frame.columnconfigure(1, weight=1)

#       ttk.Label(ip_frame, text="Local IP:").grid(row=0, column=0, sticky="w", padx=(0, 3))
#       self.local_ip_value = ttk.Label(ip_frame, text="Fetching...", anchor="w")
#       self.local_ip_value.grid(row=0, column=1, sticky="ew")

    def update_ip_display(self, ip_info):
#       self.local_ip_value.config(text=ip_info.get('local_ip', 'N/A'))
        pass

    def update_daq_connection_status(self, is_connected):
        self.daq_connected_flag = is_connected 
        
        if is_connected:
            self.connection_status_label.config(text="DAQ Status: Connected", foreground="#28a745")
        else:
            self.connection_status_label.config(text="DAQ Status: Disconnected", foreground="#dc3545")

    def open_config_window(self):
        if self.controller.config_manager:
            config_win = ConfigWindow(self.master, self.controller.config_manager)
            self.master.wait_window(config_win)
            self.on_config_loaded()
        else:
            messagebox.showwarning("Warning", "Configuration manager not initialized.")

    # ui_manager.py - 286번 라인부터 교체

    def _create_status_frame(self, parent):
        """DAQ System 탭 내부에 리프레시 버튼과 상태 프레임 생성"""
        header_frame = ttk.Frame(parent)
        header_frame.pack(fill=tk.X, padx=5, pady=(5, 0))
        
        ttk.Label(header_frame, text=" PMT Status & Storage Overview (2x2) ", 
                  font=("Helvetica", 12, "bold")).pack(side=tk.LEFT)
        
        # 수동 새로고침 버튼 (클릭 시 config 재로드 및 용량/파일목록 갱신)
        ttk.Button(header_frame, text="Refresh All 🔄", 
                   command=self.controller.refresh_all_data).pack(side=tk.RIGHT)

        self.pmt_status_frame = ttk.LabelFrame(parent, text="", padding="10")
        self.pmt_status_frame.pack(fill=tk.BOTH, expand=True, pady=5, padx=5)

    def _update_pmt_status_and_helper(self):
        """2x2 그리드 내부에 PMT 상세 정보와 설치 가이드를 배치합니다."""
        if not self.controller.config_manager: return

        for widget in self.pmt_status_frame.winfo_children():
            widget.destroy()
        
        self.pmt_status_frame.columnconfigure(0, weight=1)
        self.pmt_status_frame.columnconfigure(1, weight=1)
        self.pmt_status_frame.rowconfigure(0, weight=1)
        self.pmt_status_frame.rowconfigure(1, weight=1)

        cfg = self.controller.config_manager.get_all_variables()
        
        # [각도 정의] A~H 핀의 표준 위치 (A가 9시일 때 기준)
        # A=180, B=225, C=270, D=315, E=0, F=45, G=90, H=135
        POS_MAP_ANGLES = { 
            'E': 0, 'F': 45, 'G': 90, 'H': 135, 
            'A': 180, 'B': 225, 'C': 270, 'D': 315 
        }

        for i in range(1, 4):
            row, col = divmod(i-1, 2)
            cell = ttk.Frame(self.pmt_status_frame, padding=5, relief="groove")
            cell.grid(row=row, column=col, sticky="nsew", padx=3, pady=3)

            sn = cfg.get(f'SN{i}', "N/A")
            
            # [타입] 케이블이 연결된 핀 (A~H)
            cable_type = cfg.get(f'direction{i}', "A").strip().upper() 
            
            hv = cfg.get(f'HV{i}', "0")
            try:
                rot_val = int(cfg.get(f'RotateAngle{i}', "0"))
            except:
                rot_val = 0
            tilt = cfg.get(f'TiltAngle{i}', "0")
            is_active = sn != "N/A" and sn.strip() != ""

            self._create_status_indicator(cell, f"SN{i}", is_active, side=tk.TOP)
            
            # [수정] 회전값과 케이블 타입(A~H)을 모두 전달
            self._create_helper_diagram(cell, rot_val, cable_type, POS_MAP_ANGLES)
            
            info_text = (f"{sn} - {cable_type}\n"
                         f"HV: {hv} V\n"
                         f"Rotation: {rot_val}° / Tilt: {tilt}°")
            
            txt_color = "white" if self.is_dark_mode else "black"
            lbl = ttk.Label(cell, text=info_text, font=("Helvetica", 12, "bold"), 
                            foreground=txt_color, justify=tk.CENTER)
            lbl.pack(pady=5)

        storage_cell = ttk.LabelFrame(self.pmt_status_frame, text=" Storage Capacity ", padding=10)
        storage_cell.grid(row=1, column=1, sticky="nsew", padx=3, pady=3)
        self._create_grid_storage_widget(storage_cell)

    def _create_status_indicator(self, parent, name, is_active, side=tk.TOP):
        bg_color = "#2d2d2d" if self.is_dark_mode else "white"
        txt_color = "white" if self.is_dark_mode else "black"
        
        color = 'gold' if is_active else '#adb5bd'
        canvas_frame = ttk.Frame(parent)
        canvas_frame.pack(side=side, padx=10, pady=5)
        
        canvas = tk.Canvas(canvas_frame, width=82, height=82, bg=bg_color, highlightthickness=0, cursor="hand2")
        canvas.pack()
        
        canvas.create_rectangle(1, 1, 81, 81, outline=txt_color, width=1)
        oval_id = canvas.create_oval(10, 10, 72, 72, fill=color, outline='')
        canvas.create_text(41, 41, text=name, font=("Helvetica", 13, "bold"), fill=txt_color)
        
        canvas.bind("<Button-1>", lambda event, pmt_name=name: self.controller.open_pmt_config_window(pmt_name))
        self.status_indicators[name] = {"canvas": canvas, "oval_id": oval_id}

    def _create_grid_storage_widget(self, parent):
        """2x2 그리드 마지막 칸 전용 용량 위젯"""
        accent = self.colors["dark" if self.is_dark_mode else "light"]["accent"]
        
        title_font = ("Helvetica", 11)
        val_font = ("Helvetica", 16, "bold") # 크고 굵게

        ttk.Label(parent, text="DAQ Storage (Local):", font=title_font).pack(pady=(15, 0))
        
        self.data_size_label = ttk.Label(parent, textvariable=self.data_size_var, 
                                          foreground=accent, font=val_font)
        self.data_size_label.pack(pady=5)

        ttk.Separator(parent, orient='horizontal').pack(fill='x', pady=20)

        ttk.Label(parent, text="External HDD (Backup):", font=title_font).pack()
        
        self.data_size_label2 = ttk.Label(parent, textvariable=self.ext_data_size_var, 
                                           foreground=accent, font=val_font)
        self.data_size_label2.pack(pady=5)

    def _create_helper_diagram(self, parent, rotation_angle, cable_type, pos_map_angles):
        """
        rotation_angle: 물리적 회전 각도 (0도 = 케이블 9시)
        cable_type: 케이블이 연결된 핀 ID ('A'~'H')
        """
        bg_color = "#2d2d2d" if self.is_dark_mode else "white"
        txt_fill = 'white' if self.is_dark_mode else 'black'
        
        canvas = tk.Canvas(parent, width=280, height=200, bg=bg_color, highlightthickness=0) 
        canvas.pack(side=tk.LEFT, padx=10, expand=True)

        C_X, C_Y, R = 140, 100, 65 
        
        # 1. Scan Axis (고정된 기계 좌표계 - 파란색)
        scan_axis_bg = "#3d3d3d" if self.is_dark_mode else "#e7f5ff"
        canvas.create_rectangle(C_X - 8, C_Y - R - 20, C_X + 8, C_Y + R + 20, 
                                fill=scan_axis_bg, outline="")
        
        canvas.create_line(C_X, C_Y - R + 5, C_X, C_Y - R - 15, arrow=tk.LAST, fill="#1971c2", width=3)
        canvas.create_line(C_X, C_Y + R - 5, C_X, C_Y + R + 15, arrow=tk.LAST, fill="#1971c2", width=3)
        canvas.create_text(C_X, C_Y - R - 25, text="Scan Axis", font=("Helvetica", 10, "bold"), fill="#1971c2")

        canvas.create_oval(C_X - R, C_Y - R, C_X + R, C_Y + R, outline='gray', width=2)

        def get_pos(angle_deg, radius):
            rad = math.radians(angle_deg)
            return C_X + radius * math.cos(rad), C_Y - radius * math.sin(rad)

        # -------------------------------------------------------------
        # 1. 케이블 화살표 (물리적 위치)
        # -------------------------------------------------------------
        # 기준: 0도일 때 무조건 9시(180도)
        physical_cable_angle = 180 + rotation_angle
        
        cx1, cy1 = get_pos(physical_cable_angle, R - 5)
        cx2, cy2 = get_pos(physical_cable_angle, R + 30)
        
        canvas.create_line(cx1, cy1, cx2, cy2, arrow=tk.LAST, fill='red', width=3)
        
        # 텍스트 위치 및 앵커
        ctx, cty = get_pos(physical_cable_angle, R + 42)
        norm_angle = physical_cable_angle % 360
        anchor = "center"
        if 45 < norm_angle < 135: anchor = "s"    
        elif 135 <= norm_angle < 225: anchor = "e" 
        elif 225 <= norm_angle < 315: anchor = "n" 
        else: anchor = "w"                         
        
        canvas.create_text(ctx, cty, text="Cable", font=("Helvetica", 10, "bold"), fill="red", anchor=anchor)

        # -------------------------------------------------------------
        # 2. 내부 핀맵 (A~H, DY1/2) 회전 계산
        # -------------------------------------------------------------
        # 논리:
        # - 물리적 케이블 위치(physical_cable_angle)에 'cable_type'에 해당하는 핀이 와야 함.
        # - 예: C타입이면, C핀이 케이블 위치에 오도록 전체 핀맵을 돌려야 함.
        
        # 해당 타입 핀의 표준 각도 (A기준)
        std_type_angle = pos_map_angles.get(cable_type, 180)
        
        # 보정값 = 물리적 케이블 각도 - 표준 핀 각도
        # 예: Rot=0(케이블 180도)에 C타입(표준 270도)을 맞추려면? -> -90도 회전 필요
        pin_offset = physical_cable_angle - std_type_angle

        label_font = ("Helvetica", 12, "bold")
        axis_label_font = ("Helvetica", 11, "bold")

        for char, std_angle in pos_map_angles.items():
            # 각 핀의 최종 각도 = 표준각도 + 보정값
            final_pin_angle = std_angle + pin_offset
            
            lx, ly = get_pos(final_pin_angle, R - 15)
            
            color = 'red' if char == cable_type else txt_fill
            canvas.create_text(lx, ly, text=char, font=label_font, fill=color)

            # PMT 자체 좌표계 (+X, +Y) 표시 (Hamamatsu: A=+Y, G=+X)
            if char == 'A': 
                ax, ay = get_pos(final_pin_angle, R + 12)
                canvas.create_text(ax, ay, text="+Y", font=axis_label_font, fill="#c92a2a")
            elif char == 'G': 
                gx, gy = get_pos(final_pin_angle, R + 12)
                canvas.create_text(gx, gy, text="+X", font=axis_label_font, fill="#1971c2")

        # DY1 / DY2 (표준 위치: G-C 라인 = 90도/270도)
        dy_r = 15
        dy1_x, dy1_y = get_pos(90 + pin_offset, dy_r)  # G쪽
        dy2_x, dy2_y = get_pos(270 + pin_offset, dy_r) # C쪽
        
        canvas.create_oval(C_X-2, C_Y-2, C_X+2, C_Y+2, fill="gray", outline="")
        canvas.create_text(dy1_x, dy1_y, text="DY1", font=("Helvetica", 9, "bold"), fill=txt_fill)
        canvas.create_text(dy2_x, dy2_y, text="DY2", font=("Helvetica", 9, "bold"), fill=txt_fill)


    def _create_helper_text(self, parent, pmt_index, sn, direction, x_map, y_map):
        """회전/틸트 각도를 알려주는 텍스트를 생성합니다."""
        text_frame = ttk.Frame(parent)
        text_frame.pack(side=tk.LEFT, padx=10, fill=tk.X, expand=True)

        msg = ""
        x_tilt_msg = ""
        y_tilt_msg = ""

        if sn and direction:
            try:
                idx = ord(direction.upper()) - ord('A')
                if 0 <= idx < len(x_map):
                    x_rot_ideal = x_map[idx] # "이상적인" 각도 (X-scan용)
                    y_rot_ideal = y_map[idx] # "이상적인" 각도 (Y-scan용)

                    x_rot_display = x_rot_ideal # 모터에 설정할 "실제" 각도
                    y_rot_display = y_rot_ideal # 모터에 설정할 "실제" 각도

                    x_tilt_logic_inverted = False # X축 틸트 방향
                    y_tilt_logic_inverted = True  # Y축 틸트는 기본적으로 반대

                    # --- X축 모터 각도 및 틸트 방향 계산 ---
                    if x_rot_ideal < 0:
                        x_rot_display = x_rot_ideal + 180 # 예: -45 -> 135
                        x_tilt_logic_inverted = not x_tilt_logic_inverted # 틸트 반전
                    elif x_rot_ideal == 180:
                        x_rot_display = 0 # 180 -> 0
                        x_tilt_logic_inverted = not x_tilt_logic_inverted # 틸트 반전

                    # --- Y축 모터 각도 및 틸트 방향 계산 ---
                    if y_rot_ideal < 0:
                        y_rot_display = y_rot_ideal + 180 # 예: -90 -> 90
                        y_tilt_logic_inverted = not y_tilt_logic_inverted # 기본 반전을 다시 반전 -> 정상
                    elif y_rot_ideal == 180:
                        y_rot_display = 0 # 180 -> 0
                        y_tilt_logic_inverted = not y_tilt_logic_inverted # 기본 반전을 다시 반전 -> 정상

                    # --- 메시지 생성 ---
                    x_tilt_msg_inner = "(X+: Tilt +, X-: Tilt -)" if not x_tilt_logic_inverted else "(INVERT TILT: X+: Tilt -, X-: Tilt +)"
                    y_tilt_msg_inner = "(Y+: Tilt -, Y-: Tilt +)" if y_tilt_logic_inverted else "(INVERT TILT: Y+: Tilt +, Y-: Tilt -)"

                    # 0, 45, 90, 135는 Rot=을 표시할 필요 없음
                    x_tilt_msg = f"  {x_tilt_msg_inner}" if x_rot_display == x_rot_ideal and x_rot_ideal >= 0 else f"  (Rot={x_rot_display}°, {x_tilt_msg_inner})"
                    y_tilt_msg = f"  {y_tilt_msg_inner}" if y_rot_display == y_rot_ideal and y_rot_ideal >= 0 else f"  (Rot={y_rot_display}°, {y_tilt_msg_inner})"

                    msg = (
                            f"SN{pmt_index} ({sn} / Dir {direction}):\n"
                            f"  X-Axis Scan: Set Rotation = {x_rot_display}°\n"
                            f"  Y-Axis Scan: Set Rotation = {y_rot_display}°"
                            )

                else:
                    msg = f"SN{pmt_index} ({sn}): Invalid direction '{direction}'"
            except Exception as e:
                msg = f"SN{pmt_index} ({sn}): Error parsing direction '{direction}' ({e})"
        else:
            msg = f"SN{pmt_index}: Not configured."

        label_main = ttk.Label(text_frame, text=msg, font=("Helvetica", 10), anchor="w", justify=tk.LEFT)
        label_main.pack(side=tk.TOP, anchor="w", fill='x')

        # *** [수정 1] ***: 나중에 참조할 수 있도록 변수(label_corr)를 None으로 초기화
        label_corr = None 
        if x_tilt_msg or y_tilt_msg:
            correction_msg = f"{x_tilt_msg}\n{y_tilt_msg}"
            # *** [수정 2] ***: 생성된 라벨을 'label_corr' 변수에 할당
            label_corr = ttk.Label(text_frame, text=correction_msg, foreground="#c92a2a", font=("Helvetica", 10, "bold"), anchor="w", justify=tk.LEFT)
            label_corr.pack(side=tk.TOP, anchor="w", fill='x', pady=(2,0))

        if not (sn and direction):
            label_main.config(foreground="gray")

        # --- [*** 여기가 추가된 수정 사항입니다 ***] ---
        def configure_wraplength(event):
            # 부모 프레임(text_frame)의 너비를 기준으로 래핑 길이를 설정합니다.
            width = event.width - 10 # 약간의 여백(padding)을 줍니다.
            if width > 0:
                label_main.config(wraplength=width)
                # 'label_corr'가 생성된 경우에만 래핑을 설정합니다.
                if label_corr: 
                    label_corr.config(wraplength=width)

        # text_frame의 크기가 변경될 때마다(예: 창 크기 조절) configure_wraplength 함수를 호출합니다.
        text_frame.bind("<Configure>", configure_wraplength)

    # ui_manager_test.py 내부 _create_run_control_frame 수정 (4칸 띄어쓰기)
    def _create_run_control_frame(self, parent):
        frame = ttk.LabelFrame(parent, text=" 📊 Run Mode & Parameters ", padding="10")
        frame.pack(fill=tk.X, pady=5, padx=5)

        # [NEW] 메인 모드 선택 (1번: General / 2번: Manual)
        ttk.Label(frame, text="1. Operation Category:", font=("Helvetica", 10, "bold")).pack(anchor=tk.W)
        
        # main_mode_var는 App에서 관리하도록 controller를 참조합니다.
        # (App.__init__에 self.ui.main_mode_var = tk.StringVar(value="manual") 추가 필요)
        rb_auto = ttk.Radiobutton(frame, text=" General Scan (Auto Control)", 
                                  variable=self.run_mode, value="auto",
                                  command=self.controller.handle_mode_change)
        rb_auto.pack(anchor=tk.W, padx=10, pady=2)

        rb_manual = ttk.Radiobutton(frame, text=" Manual Mode (Laser/Dark Selection)", 
                                    variable=self.run_mode, value="manual",
                                    command=self.controller.handle_mode_change)
        rb_manual.pack(anchor=tk.W, padx=10, pady=2)

        ttk.Separator(frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=10)

        # [NEW] 2번 수동 모드 내 세부 선택 (Manual일 때만 활성화)
        ttk.Label(frame, text="2. Manual Sub-selection:", font=("Helvetica", 10)).pack(anchor=tk.W)
        
        # 수동 모드 변수 (기존 self.manual_mode_var 활용)
        self.manual_type_var = tk.StringVar(value="laser")
        
        self.rb_laser = ttk.Radiobutton(frame, text=" Laser & External trigger (0)", 
                                        variable=self.manual_type_var, value="laser",
                                        command=self.controller.handle_mode_change)
        self.rb_laser.pack(anchor=tk.W, padx=25)

        self.rb_dark = ttk.Radiobutton(frame, text=" Dark & Self trigger (1)", 
                                       variable=self.manual_type_var, value="dark",
                                       command=self.controller.handle_mode_change)
        self.rb_dark.pack(anchor=tk.W, padx=25)

        # Run Number 입력창 (기존 유지)
        ttk.Label(frame, text="Run number (Produce & Analysis):").pack(anchor=tk.W, pady=(15, 0))
        run_entry = ttk.Entry(frame, textvariable=self.run_number_var)
        run_entry.pack(fill=tk.X)
        self.run_num_status_label = ttk.Label(frame, text="", foreground="gray", font=("Helvetica", 8))
        self.run_num_status_label.pack(anchor=tk.W, pady=(2, 0))


    def set_run_number_status(self, message):
        self.run_num_status_label.config(text=message)

    def _create_dynamic_buttons_frame(self, parent, title, frame_id):
        frame = ttk.LabelFrame(parent, text=title, padding="10")
        frame.pack(fill=tk.X, pady=5, padx=5)
        try:
            with open(os.path.join(self.controller.base_dir, 'buttons.json'), 'r') as f:
                buttons_config = json.load(f)

            for config in buttons_config:
                if config['frame'] == frame_id:
                    btn = ttk.Button(
                            frame, text=config['label'],
                            command=lambda cmd=config['command']: self.controller.handle_button_click(cmd)
                            )
                    btn.pack(pady=5, fill=tk.X, expand=True) 
                    self.buttons[config['command']] = btn

        except (FileNotFoundError, json.JSONDecodeError) as e:
            ttk.Label(frame, text=f"Error loading buttons.json: {e}").pack()
        return frame

    def _create_config_viewer(self, parent):
        container_frame = ttk.Frame(parent)
        container_frame.pack(fill=tk.BOTH, expand=True, pady=(10,0))
        container_frame.columnconfigure(0, weight=1)

        top_frame = ttk.Frame(container_frame)
        top_frame.grid(row=0, column=0, sticky="ew")
        top_frame.columnconfigure(0, weight=1) 

        ttk.Label(top_frame, text="Current Configuration", font=("Helvetica", 11, "bold")).grid(row=0, column=0, sticky="w")

        refresh_btn = ttk.Button(top_frame, text="Refresh 🔄", command=self.controller.refresh_all_data)
        refresh_btn.grid(row=1, column=1, sticky="e", padx=5)

        self.config_text = scrolledtext.ScrolledText(container_frame, wrap=tk.WORD, state="disabled", bg="#fdfdfd", fg="#212529", font=("Menlo", 10))
        #self.config_text = scrolledtext.ScrolledText(container_frame, wrap=tk.WORD, state="disabled", bg="#2E2E2E", fg="#E0E0E0", font=("Menlo", 10))
        self.config_text.grid(row=1, column=0, sticky="nsew", pady=(5,0))

        container_frame.rowconfigure(1, weight=1)


    def update_config_display(self):
        """
        if not self.controller.config_manager: return
        self.config_text.tag_configure("comment", foreground="#228B22", font=("Helvetica", 12, "bold"), spacing1=8, spacing3=2)
        self.config_text.tag_configure("key", foreground="#333333", font=("Helvetica", 11, "bold"))
        #self.config_text.tag_configure("key", foreground="#D4D4D4", font=("Helvetica", 11, "bold"))
        self.config_text.tag_configure("value", foreground="#c92a2a", font=("Helvetica", 11))
        self.config_text.tag_configure("error", foreground="#FF0000")
        #self.config_text.config(state="normal")
        self.config_text.delete('1.0', tk.END)
        parsed_data = self.controller.config_manager.get_all_configs_and_comments()
        for item_type, *data in parsed_data:
            if item_type == 'comment':
                self.config_text.insert(tk.END, f"{data[0]}\n", "comment")
            elif item_type == 'variable':
                var_name, value = data
                self.config_text.insert(tk.END, f"    {var_name}: ", "key")
                self.config_text.insert(tk.END, f"{value}\n", "value")
            elif item_type == 'error':
                self.config_text.insert(tk.END, f"Error: {data[0]}\n", "error")
        """
        pass

    def _create_path_viewer_frame(self, parent):
        frame = ttk.LabelFrame(parent, text="File & Directory Paths", padding="10")
        frame.pack(fill=tk.X, pady=5, padx=5)

        self.path_container = ttk.Frame(frame)
        self.path_container.pack(fill=tk.X, pady=(0, 5))

        self.path_labels = {}
        path_keys = ['BasePath', 'RawDataPath', 'ExternalPath'] #DaqProgramPath
        #path_keys = [] #DaqProgramPath

        for key in path_keys:
            path_frame_inner = ttk.Frame(self.path_container)
            path_frame_inner.pack(fill=tk.X, pady=2)

            label = ttk.Label(path_frame_inner, text=f"{key}:", width=16)
            label.pack(side=tk.LEFT)

            path_label = ttk.Label(path_frame_inner, text="N/A", anchor='w')
            path_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
            self.path_labels[key] = path_label 

            open_term_btn = ttk.Button(
                    path_frame_inner, text=">", width=2,
                    command=lambda p=key: self.controller.open_terminal_at_path_by_key(p)
                    )
            open_term_btn.pack(side=tk.RIGHT, padx=(5,0))

        def configure_wraplength(event):
            width = event.width - 150 
            for label in self.path_labels.values():
                label.config(wraplength=width)

        self.path_container.bind("<Configure>", configure_wraplength)

    def update_data_size_display(self, size_str, is_external=False):
        if is_external:
            self.ext_data_size_var.set(size_str)
        else:
            self.data_size_var.set(size_str)

    def update_path_display(self):
        if not self.controller.config_manager:
            for label in self.path_labels.values():
                label.config(text="Config not loaded.")
            return

        for key, label_widget in self.path_labels.items():
            path_value = self.controller.config_manager.get_config_value(key) or "Not Set"
            label_widget.config(text=path_value)


    def _create_data_viewer(self, parent):
        self.all_data_files = []
        self.data_view_vars = {}

        data_paned_window = ttk.PanedWindow(parent, orient=tk.HORIZONTAL)
        data_paned_window.pack(fill=tk.BOTH, expand=True)

        left_data_frame = ttk.Frame(data_paned_window)
        data_paned_window.add(left_data_frame, weight=3)

        self.data_notebook = ttk.Notebook(left_data_frame)
        self.data_notebook.pack(fill=tk.BOTH, expand=True, pady=5)

        for tab_name in ["Raw", "Production", "Result", "External Disk"]:
            tab_frame = ttk.Frame(self.data_notebook)
            self.data_notebook.add(tab_frame, text=f"{tab_name} Data")
            self._create_file_browser_tab(tab_frame, tab_name)

        button_frame = ttk.Frame(left_data_frame)
        button_frame.pack(fill=tk.X, padx=5, pady=(5,0))

        move_button = ttk.Button(button_frame, text="Move Selected File(s) 🚚", command=self.on_move_selected_files)
        move_button.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))

        delete_button = ttk.Button(left_data_frame, text="Delete Selected File(s) 🗑️", command=self.on_delete_selected_files)
        delete_button.pack(fill=tk.X, padx=5, pady=(5,0))

        right_info_frame = ttk.LabelFrame(data_paned_window, text="File Info", padding=10)
        data_paned_window.add(right_info_frame, weight=1)

        self.file_info_label = ttk.Label(right_info_frame, text="Select a file to see details.", justify=tk.LEFT, wraplength=250)
        self.file_info_label.pack(anchor=tk.NW)

    def on_move_selected_files(self):
        files_to_move = self.get_selected_file_paths()

        if not files_to_move:
            messagebox.showwarning("No Selection", "Please select file(s) to move.")
            return

        self.controller.move_data_files(files_to_move)

    def _create_log_viewer(self, parent):
        log_frame = ttk.LabelFrame(parent, text="Log Viewer", padding=5)
        log_frame.pack(fill=tk.BOTH, expand=True)
        self.log_text = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, state="disabled", bg="#1e1e1e", fg="#d4d4d4", font=("Menlo", 10))
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def update_log_view(self, content):
        self.log_text.config(state="normal")
        self.log_text.delete('1.0', tk.END)
        self.log_text.insert(tk.END, content)
        self.log_text.config(state="disabled")
        self.log_text.yview_moveto(1)

    def update_file_info_panel(self, file_path):
        try:
            stat = os.stat(file_path)
            size_mb = stat.st_size / (1024 * 1024)
            mtime = datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
            filename = os.path.basename(file_path)

            info_text = (
                    f"File: {filename}\n\n"
                    f"Size: {size_mb:.2f} MB\n"
                    f"Modified: {mtime}\n"
                    )
            self.file_info_label.config(text=info_text)
        except FileNotFoundError:
            self.file_info_label.config(text=f"File not found:\n{os.path.basename(file_path)}")
        except Exception as e:
            self.file_info_label.config(text=f"Could not get file info:\n{e}")

    def _create_file_browser_tab(self, parent_tab, tab_type):
        control_frame = ttk.Frame(parent_tab, padding=5)
        control_frame.pack(fill=tk.X)

        filter_frame = ttk.LabelFrame(control_frame, text="Filter Mode", padding=5)
        filter_frame.pack(side=tk.LEFT, padx=(0, 10))
        filter_mode = tk.StringVar(value="All")
        ttk.Radiobutton(filter_frame, text="All", variable=filter_mode, value="All", command=self.update_data_viewer).pack(side=tk.LEFT)
        ttk.Radiobutton(filter_frame, text="Dark", variable=filter_mode, value="Dark", command=self.update_data_viewer).pack(side=tk.LEFT)
        ttk.Radiobutton(filter_frame, text="Laser", variable=filter_mode, value="Laser", command=self.update_data_viewer).pack(side=tk.LEFT)
        
        search_frame = ttk.LabelFrame(control_frame, text="Search Files", padding=5)
        search_frame.pack(side=tk.LEFT, padx=(10, 0), fill=tk.X, expand=True)

        search_var = tk.StringVar()
        search_entry = ttk.Entry(search_frame, textvariable=search_var)
        search_entry.pack(fill=tk.X)
        search_var.trace_add("write", lambda *args: self.update_data_viewer())

        sort_frame = ttk.LabelFrame(control_frame, text="Sort By", padding=5)
        sort_frame.pack(side=tk.LEFT)
        sort_mode = tk.StringVar(value="time")
        ttk.Button(sort_frame, text="Name (A-Z)", command=lambda: self._set_sort_and_update(tab_type, 'name')).pack(side=tk.LEFT)
        ttk.Button(sort_frame, text="Time (Newest)", command=lambda: self._set_sort_and_update(tab_type, 'time')).pack(side=tk.LEFT)

        refresh_btn = ttk.Button(control_frame, text="Refresh 🔄", command=self.controller.refresh_all_data)
        refresh_btn.pack(side=tk.RIGHT, padx=5)

        # 1. 여기서 생성한 tree_frame을 부모로 사용해야 합니다.
        tree_frame = ttk.Frame(parent_tab)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # 2. [수정됨] container -> tree_frame으로 변경
        tree = ttk.Treeview(tree_frame, show="headings", selectmode="extended")

        tree["columns"] = ("filename", "path", "mtime")
        tree.column("#0", width=0, stretch=tk.NO) 

        tree.column("filename", width=700, anchor="w", stretch=tk.YES)
        tree.column("path", width=200, anchor="w", stretch=tk.NO)
        tree.column("mtime", width=180, anchor="center", stretch=tk.NO)

        tree.heading("filename", text="File Name")
        tree.heading("path", text="Directory Path")
        tree.heading("mtime", text="Last Modified")

        # 3. [수정됨] Scrollbar의 부모도 tree_frame으로 변경
        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        tree.pack(fill=tk.BOTH, expand=True)

        tree.bind("<Double-1>", self.on_data_file_double_click)
        tree.bind("<<TreeviewSelect>>", self.on_data_file_select)

        self.data_view_vars[tab_type] = {
            "tree": tree,
            "filter_mode": filter_mode,
            "sort_mode": sort_mode,
            "search_var": search_var
        }

    def _set_sort_and_update(self, tab_type, mode):
        """정렬 모드를 설정하고 뷰를 업데이트합니다."""
        self.data_view_vars[tab_type]["sort_mode"].set(mode)
        self.update_data_viewer()

    def update_data_viewer(self, force_refresh=False):
        """파일 목록을 필터링하고 정렬하여 Treeview를 업데이트합니다."""
        if force_refresh:
            self.all_data_files = self.controller.get_data_files()

        for tab_type, vars in self.data_view_vars.items():
            tree = vars["tree"]
            filter_mode = vars["filter_mode"].get()
            sort_mode = vars["sort_mode"].get()
            search_query = vars["search_var"].get().lower()

            # 1. Type Filetering ('Raw' or 'Production')
            filtered_list = [f for f in self.all_data_files if f["type"] == tab_type]

            # 2. Mode Filtering('Dark' or 'Laser')
            if filter_mode != "All":
                keyword = f"_{filter_mode.lower()}"
                filtered_list = [f for f in filtered_list if keyword in f["filename"]]

            # 3. Sort
            if sort_mode == 'name':
                filtered_list.sort(key=lambda x: x["filename"])
            else: # time
                filtered_list.sort(key=lambda x: x["mtime_float"], reverse=True)

            if search_query:
                filtered_list = [f for f in filtered_list if search_query in f["filename"].lower()]

            # 4. Treeview 
            tree.delete(*tree.get_children())
            for file_info in filtered_list:
                tree.insert("", tk.END, values=(file_info["filename"], file_info["path"], file_info["mtime"]))

    def on_data_file_double_click(self, event):
        """Treeview에서 아이템을 더블클릭했을 때 호출됩니다."""
        tree = event.widget 
        if not tree.selection(): return

        item_id = tree.selection()[0]
        item_values = tree.item(item_id, "values")
        if item_values:
            filename, dir_path, _ = item_values
            full_path = os.path.join(dir_path, filename)
            self.controller.open_root_file_browser(full_path)


    def on_data_file_select(self, event):
        """Treeview에서 아이템을 클릭했을 때 정보 패널 업데이트"""
        tree = event.widget
        if not tree.selection(): return
        item_id = tree.selection()[0]
        item_values = tree.item(item_id, "values")
        if item_values:
            filename, dir_path, _ = item_values
            full_path = os.path.join(dir_path, filename)
            self.update_file_info_panel(full_path)

    def open_image_viewer(self):
        if self.image_viewer_window and self.image_viewer_window.winfo_exists():
            self.image_viewer_window.lift()
            self.image_viewer_window.focus_force()
            return

        if self.controller.config_manager:
            self.image_viewer_window = ImageViewer(self.master, self.controller.config_manager)
            self.image_viewer_window.protocol("WM_DELETE_WINDOW", self._on_image_viewer_close)
        else:
            messagebox.showwarning("Warning", "Please set the DAQ configuration file path first.")

    def _on_image_viewer_close(self):
        if self.image_viewer_window:
            self.image_viewer_window.destroy()
        self.image_viewer_window = None

    def on_delete_selected_files(self):
        try:
            if not hasattr(self, 'data_notebook'): 
                messagebox.showerror("Error", "Data notebook not initialized.")
                return

            current_data_tab_index = self.data_notebook.index(self.data_notebook.select())
            tab_text = self.data_notebook.tab(current_data_tab_index, "text")
           # tab_type = "Raw" if "Raw" in tab_text else "Production"
            tab_type = tab_text.replace(" Data", "")
            if tab_type not in self.data_view_vars: return

            tree = self.data_view_vars[tab_type]["tree"]
            selected_items = tree.selection()
            if not selected_items:
                messagebox.showwarning("No Selection", "Please select one or more files to delete.")
                return

            files_to_delete = []
            for item_id in selected_items:
                values = tree.item(item_id, "values")
                if values:
                    filename, dir_path, _ = values
                    full_path = os.path.join(dir_path, filename)
                    files_to_delete.append(full_path)

            if files_to_delete:
                self.controller.delete_data_files(files_to_delete)
        except Exception as e:
            messagebox.showerror("Error", f"Could not get selected files: {e}")

    def get_selected_file_paths(self):
        """현재 활성화된 Data 탭에서 선택된 파일들의 전체 경로 리스트를 반환합니다."""
        try:
            if not hasattr(self, 'data_notebook'): 
                return []

            current_data_tab_index = self.data_notebook.index(self.data_notebook.select())
            tab_text = self.data_notebook.tab(current_data_tab_index, "text")
           # tab_type = "Raw" if "Raw" in tab_text else "Production"
            tab_type = tab_text.replace(" Data", "")



            if tab_type not in self.data_view_vars: 
                return []

            tree = self.data_view_vars[tab_type]["tree"]
            selected_items = tree.selection()
            if not selected_items:
                return []

            files_to_return = []
            for item_id in selected_items:
                values = tree.item(item_id, "values")
                if values:
                    filename, dir_path, _ = values
                    full_path = os.path.join(dir_path, filename)
                    files_to_return.append(full_path)

            return files_to_return
        except Exception:
            return [] # 오류 발생 시 빈 리스트 반환

    def get_run_num(self):
        run_num = self.run_number_var.get()
        if not run_num or not run_num.isdigit():
            messagebox.showwarning("Input Required", "Please enter a valid Run Number.")
            return None
        return run_num

	## """""""""""""""""""""""""" LASER CONFIGURATION """"""""""""""""""""""""""""""""" ##
    def _create_laser_control_tab(self, parent):
        main_container = ttk.Frame(parent, padding=10)
        main_container.pack(fill=tk.BOTH, expand=True)

        # [삭제됨] 상단 공통 연결 프레임 (Laser System Connection) 제거
        # 이제 바로 탭 노트북이 나옵니다.
        
        self.laser_sub_notebook = ttk.Notebook(main_container)
        self.laser_sub_notebook.pack(fill=tk.BOTH, expand=True)

        self.laser_tabs_data = {} 
        wavelengths = ["375nm", "405nm", "450nm", "473nm"]

        for wl in wavelengths:
            tab_frame = ttk.Frame(self.laser_sub_notebook)
            self.laser_sub_notebook.add(tab_frame, text=f" {wl} ")
            
            default_pulse = 132.99 if wl == "405nm" else 0.0
            
            vars_dict = {
                # [NEW] 개별 연결 상태 표시용 문자열 변수
                "conn_status_txt": tk.StringVar(value="Disconnected"), 
                
                "ld_status": tk.StringVar(value="OFF"),
                "tec_status": tk.StringVar(value="OFF"),
                "temp": tk.StringVar(value="--.- °C"),
                "bias_live": tk.StringVar(value="---.- mA"),
                "pulse_live": tk.StringVar(value="---.- mA"),
                "bias_set": tk.DoubleVar(value=0.0),
                "pulse_set": tk.DoubleVar(value=default_pulse),
                "trigger_mode": tk.StringVar(value="External"),
                "freq_hz": tk.StringVar(value="10000000"),
                "check_interval": tk.StringVar(value="1s")
            }
            self.laser_tabs_data[wl] = vars_dict
            self._build_individual_laser_ui(tab_frame, wl, vars_dict)

    def _build_individual_laser_ui(self, tab_parent, wl, vars_dict):
        # [NEW] 1. 탭 최상단: 개별 장비 연결 제어바 생성
        # PanedWindow보다 먼저 pack() 하여 맨 위에 고정시킵니다.
        conn_frame = ttk.Frame(tab_parent, padding=5, relief="groove", borderwidth=1)
        conn_frame.pack(fill=tk.X, padx=5, pady=5)

        # 상태 라벨 (크고 잘 보이게)
        status_lbl = ttk.Label(conn_frame, textvariable=vars_dict["conn_status_txt"], 
                               font=("Helvetica", 12, "bold"), foreground="red")
        status_lbl.pack(side=tk.LEFT, padx=(10, 20))
        vars_dict["conn_label_obj"] = status_lbl # 색상 변경을 위해 객체 저장

        # 제어 버튼들 (main.py의 새 함수들과 연결)
        ttk.Button(conn_frame, text="🔌 Connect", width=12,
                   command=lambda: self.controller.connect_single_laser(wl)).pack(side=tk.LEFT, padx=2)
        
        ttk.Button(conn_frame, text="❌ Disconnect", width=12,
                   command=lambda: self.controller.disconnect_single_laser(wl)).pack(side=tk.LEFT, padx=2)
        
        # 구분선
        ttk.Separator(conn_frame, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10, pady=2)

        # 새로고침 및 히스토리 버튼
        ttk.Button(conn_frame, text="Refresh 🔄", width=10,
                   command=lambda: self.controller.manual_refresh_laser(wl)).pack(side=tk.LEFT, padx=2)

        ttk.Button(conn_frame, text="Load History 📂", 
                   command=lambda: self.controller.load_historical_laser_data(wl)).pack(side=tk.RIGHT, padx=5)


        # [EXISTING] 2. 그 아래에 기존의 좌우 패널(PanedWindow) 레이아웃 배치
        # (여기서부터는 기존 코드와 동일합니다)
        laser_pane = ttk.PanedWindow(tab_parent, orient=tk.HORIZONTAL)
        laser_pane.pack(fill=tk.BOTH, expand=True)

        # --- 좌측 패널 (Settings) ---
        left_pane = ttk.Frame(laser_pane)
        laser_pane.add(left_pane, weight=1)

        self._create_laser_settings_frames_multi(left_pane, wl, vars_dict)

        # --- 우측 패널 (Live Monitor) ---
        right_pane = ttk.Frame(laser_pane)
        laser_pane.add(right_pane, weight=2)

        self._create_laser_live_labels_multi(right_pane, vars_dict)

        # Trigger Control 섹션
        trig_frame = ttk.LabelFrame(left_pane, text=f"Trigger Control ({wl})", padding=10)
        trig_frame.pack(fill=tk.X, pady=5)
        vars_dict["trig_frame_obj"] = trig_frame 

        ttk.Label(trig_frame, text="Mode:").pack(side=tk.LEFT, padx=5)

        mode_combo = ttk.Combobox(trig_frame, textvariable=vars_dict["trigger_mode"], 
                                  values=["Internal (PG1)", "Internal (PG2)", "External"], 
                                  state="readonly", width=15)
        mode_combo.pack(side=tk.LEFT, padx=5)
        mode_combo.bind("<<ComboboxSelected>>", lambda e, w=wl: self.controller.on_laser_trigger_change_multi(w))

        freq_entry = ttk.Entry(trig_frame, textvariable=vars_dict["freq_hz"], width=12)
        freq_entry.pack(side=tk.LEFT, padx=5)
        vars_dict["freq_entry_obj"] = freq_entry

        apply_btn = ttk.Button(trig_frame, text="Apply", 
                               command=lambda w=wl: self.controller.apply_laser_frequency_multi(w))
        apply_btn.pack(side=tk.LEFT, padx=5)
        vars_dict["freq_apply_btn_obj"] = apply_btn

        # 좌측 하단 Notebook (History Plot & Log)
        left_notebook = ttk.Notebook(left_pane)
        left_notebook.pack(fill=tk.BOTH, expand=True, pady=10)

        hist_tab = ttk.Frame(left_notebook)
        left_notebook.add(hist_tab, text=" Historical Plot ")
        
        fig_h, ax_h = plt.subplots(figsize=(4, 2.5), dpi=80)
        canvas_h = FigureCanvasTkAgg(fig_h, master=hist_tab)
        canvas_h.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        
        vars_dict["fig_hist"] = fig_h
        vars_dict["ax_hist"] = ax_h
        vars_dict["canvas_hist"] = canvas_h

        log_tab = ttk.Frame(left_notebook)
        left_notebook.add(log_tab, text=" Laser Session Log ")

        # 우측 실시간 모니터링 그래프
        realtime_container = ttk.LabelFrame(right_pane, text=f"Real-time Monitoring ({wl})", padding=5)
        realtime_container.pack(fill=tk.BOTH, expand=True, pady=5)

        fig_live, (ax_temp, ax_curr) = plt.subplots(2, 1, sharex=True, figsize=(6, 6), dpi=100)
        fig_live.tight_layout(pad=3.0)

        canvas_live = FigureCanvasTkAgg(fig_live, master=realtime_container)
        live_toolbar = NavigationToolbar2Tk(canvas_live, realtime_container)
        live_toolbar.update()
        live_toolbar.pack(side=tk.TOP, fill=tk.X)
        canvas_live.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        
        vars_dict["fig"] = fig_live
        vars_dict["ax_temp"] = ax_temp
        vars_dict["ax_curr"] = ax_curr
        vars_dict["canvas"] = canvas_live

    def _build_historical_plot_ui(self, parent):
        """삭제되었던 히스토리 그래프 영역 복구"""
        self.fig_hist, self.ax_hist = plt.subplots(figsize=(10, 5), dpi=100)
        self.canvas_hist = FigureCanvasTkAgg(self.fig_hist, master=parent)
        self.hist_toolbar = NavigationToolbar2Tk(self.canvas_hist, parent)
        self.hist_toolbar.update()
        self.hist_toolbar.pack(side=tk.TOP, fill=tk.X)
        self.canvas_hist.get_tk_widget().pack(fill=tk.BOTH, expand=True)

    def update_laser_status_colors(self, wl, ld_on, tec_on):
        vars_dict = self.laser_tabs_data.get(wl)
        if not vars_dict: return
    
        ld_color = "#28a745" if ld_on else "#dc3545" 
        tec_color = "#28a745" if tec_on else "#dc3545" 

        if "ld_label_obj" in vars_dict:
            vars_dict["ld_label_obj"].config(foreground=ld_color)
        if "tec_label_obj" in vars_dict:
            vars_dict["tec_label_obj"].config(foreground=tec_color)

    def _create_laser_settings_frames_multi(self, parent, wl, vars_dict):
        """특정 파장 탭 전용 제어 프레임 생성 (초기 상태: DISABLED)"""
        pwr_frame = ttk.LabelFrame(parent, text=f"Power Control ({wl})", padding=10)
        pwr_frame.pack(fill=tk.X, pady=5)
        
        vars_dict["ld_on_btn"] = ttk.Button(pwr_frame, text="LD ON", state=tk.DISABLED,
                                            command=lambda: self.controller.set_laser_ld_safe(wl, True))
        vars_dict["ld_on_btn"].pack(side=tk.LEFT, padx=5)

        vars_dict["ld_off_btn"] = ttk.Button(pwr_frame, text="LD OFF", state=tk.DISABLED,
                                             command=lambda: self.controller.set_laser_ld_safe(wl, False))
        vars_dict["ld_off_btn"].pack(side=tk.LEFT, padx=5)
        
        ttk.Separator(pwr_frame, orient=tk.VERTICAL).pack(side=tk.LEFT, padx=10, fill=tk.Y)
        
        vars_dict["tec_on_btn"] = ttk.Button(pwr_frame, text="TEC ON", state=tk.DISABLED,
                                             command=lambda: self.controller.set_laser_tec_multi(wl, True))
        vars_dict["tec_on_btn"].pack(side=tk.LEFT, padx=5)

        vars_dict["tec_off_btn"] = ttk.Button(pwr_frame, text="TEC OFF", state=tk.DISABLED,
                                              command=lambda: self.controller.set_laser_tec_multi(wl, False))
        vars_dict["tec_off_btn"].pack(side=tk.LEFT, padx=5)

        curr_frame = ttk.LabelFrame(parent, text="Current Settings (mA)", padding=10)
        curr_frame.pack(fill=tk.X, pady=5)
        
        self._create_laser_slider(curr_frame, "Bias:", vars_dict["bias_set"])
        self._create_laser_slider(curr_frame, "Pulse:", vars_dict["pulse_set"])
        
        vars_dict["curr_apply_btn_obj"] = ttk.Button(curr_frame, text="Apply Currents", state=tk.DISABLED,
                                                    command=lambda: self.controller.apply_laser_currents_multi(wl))
        vars_dict["curr_apply_btn_obj"].pack(fill=tk.X, pady=10)

    def _create_laser_live_labels_multi(self, parent, vars_dict):
        """특정 파장 탭의 실시간 상태 표시 라벨 생성"""
        status_grid = ttk.LabelFrame(parent, text="Live Status", padding=10)
        status_grid.pack(fill=tk.X, pady=5)

        items = [
            ("LD Status", "ld_status"),
            ("TEC Status", "tec_status"),
            ("Temperature", "temp"),
            ("Live Pulse", "pulse_live"),
            ("Check Int.", "check_interval")
        ]

        for label_text, var_key in items:
            row = ttk.Frame(status_grid)
            row.pack(fill=tk.X, pady=2)
            
            # 항목 이름 라벨
            ttk.Label(row, text=f"{label_text}:", width=15, font=("Helvetica", 10, "bold")).pack(side=tk.LEFT)
            
            # 실제 값이 표시될 라벨 객체 생성
            # .pack()을 뒤로 빼고 변수 lbl에 먼저 할당합니다.
            lbl = ttk.Label(row, textvariable=vars_dict[var_key], width=15, relief="groove")
            lbl.pack(side=tk.LEFT)

            # 나중에 색상을 바꾸기 위해 특정 항목(LD, TEC)의 라벨 객체만 vars_dict에 저장합니다.
            if var_key == "ld_status":
                vars_dict["ld_label_obj"] = lbl
            if var_key == "tec_status":
                vars_dict["tec_label_obj"] = lbl

    def _create_laser_slider(self, parent, label, var):
        frame = ttk.Frame(parent)
        frame.pack(fill=tk.X, pady=2)
        ttk.Label(frame, text=label, width=10).pack(side=tk.LEFT)
        ttk.Scale(frame, from_=0, to=200, variable=var, orient=tk.HORIZONTAL).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=10)
        ttk.Entry(frame, textvariable=var, width=8).pack(side=tk.LEFT)

    # -----------------------------------------------------------
    # [ Web Monitor ] 탭 관련 메서드
    # -----------------------------------------------------------

    def _on_admin_only_click(self, event):
        """URL 입력창 클릭 시 관리자 권한 경고를 띄우고 클릭을 무효화함"""
        messagebox.showwarning("Access Denied", "Only administrator can modify this URL.")
        return "break" 

    def _create_web_monitor_tab(self, parent_notebook):
        """B-field Monitoring 탭 (스크롤 줌 + 시간 표시 위치 변경)"""
        tab = ttk.Frame(parent_notebook)
        parent_notebook.add(tab, text=" B-field Monitoring ") 

        # 1. 제어 패널
        ctrl_frame = ttk.Frame(tab, padding=5)
        ctrl_frame.pack(fill=tk.X)

        ttk.Label(ctrl_frame, text="Target URL:").pack(side=tk.LEFT, padx=5)
        fixed_url = "https://www-sk1.icrr.u-tokyo.ac.jp/~yufei/precal_monitoring/"
        self.web_url_var = tk.StringVar(value=fixed_url) 
        
        self.url_entry = ttk.Entry(ctrl_frame, textvariable=self.web_url_var, width=50)
        self.url_entry.pack(side=tk.LEFT, padx=5)
        self.url_entry.config(state="readonly", foreground="gray")
        self.url_entry.bind("<Button-1>", self._on_admin_only_click)

        # 줌 컨트롤
        ttk.Label(ctrl_frame, text="Zoom:").pack(side=tk.LEFT, padx=(10, 2))
        self.web_zoom_var = tk.DoubleVar(value=1.0) 
        
        self.zoom_scale = ttk.Scale(ctrl_frame, from_=0.5, to=2.5, 
                                    variable=self.web_zoom_var, orient=tk.HORIZONTAL, length=150)
        self.zoom_scale.pack(side=tk.LEFT, padx=2)
        
        self.zoom_label = ttk.Label(ctrl_frame, text="100%", width=5)
        self.zoom_label.pack(side=tk.LEFT, padx=2)
        
        self.zoom_scale.configure(command=lambda v: self.zoom_label.config(text=f"{float(v)*100:.0f}%"))
        self.zoom_scale.bind("<ButtonRelease-1>", self._on_zoom_release)
        
        ttk.Button(ctrl_frame, text="↺ 100%", width=8, command=self._reset_zoom).pack(side=tk.LEFT, padx=2)

        self.web_btn = ttk.Button(ctrl_frame, text="Start Monitor", command=self.toggle_web_monitoring)
        self.web_btn.pack(side=tk.LEFT, padx=10)

        self.refresh_btn = ttk.Button(ctrl_frame, text="Refresh 🔄", command=self.manual_refresh_web)
        self.refresh_btn.pack(side=tk.LEFT, padx=2)

        self.web_time_label = ttk.Label(ctrl_frame, text="", font=("Helvetica", 14, "bold"), foreground="#007bff")
        self.web_time_label.pack(side=tk.LEFT, padx=15)

        # -------------------------------------------------------------
        # Canvas 생성 (휠 이벤트 추가)
        # -------------------------------------------------------------
        self.canvas_frame = ttk.Frame(tab)
        self.canvas_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        v_scroll = ttk.Scrollbar(self.canvas_frame, orient=tk.VERTICAL)
        h_scroll = ttk.Scrollbar(self.canvas_frame, orient=tk.HORIZONTAL)

        self.web_canvas = tk.Canvas(self.canvas_frame, bg="#e1e1e1",
                                    yscrollcommand=v_scroll.set, xscrollcommand=h_scroll.set)
        
        v_scroll.config(command=self.web_canvas.yview)
        h_scroll.config(command=self.web_canvas.xview)
        
        v_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        h_scroll.pack(side=tk.BOTTOM, fill=tk.X)
        self.web_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # [기존] 드래그 이동 (Pan)
        self.web_canvas.bind("<ButtonPress-1>", self._on_canvas_click)
        self.web_canvas.bind("<B1-Motion>", self._on_canvas_drag)

        # [추가] 마우스 휠로 줌 (Linux: Button-4/5, Windows: MouseWheel)
        self.web_canvas.bind("<Button-4>", self._on_canvas_scroll_zoom) # Linux Scroll UP
        self.web_canvas.bind("<Button-5>", self._on_canvas_scroll_zoom) # Linux Scroll DOWN
        self.web_canvas.bind("<MouseWheel>", self._on_canvas_scroll_zoom) # Windows Scroll

        # 안내 문구
        w_center = 600
        h_center = 350
        self.canvas_text_id = self.web_canvas.create_text(
            w_center, h_center, text="Click 'Start' to verify VPN & Monitor", font=("Helvetica", 14), fill="gray"
        )
        self.web_image_id = None 

        # 변수 초기화
        self.is_monitoring = False
        self.driver = None
        self.monitor_w = 1280
        self.monitor_h = 720
        self.web_connection_status = False
        self.force_refresh_flag = False

    def _on_canvas_click(self, event):
        self.web_canvas.scan_mark(event.x, event.y)

    def _on_canvas_drag(self, event):
        self.web_canvas.scan_dragto(event.x, event.y, gain=1)

    def _on_zoom_release(self, event):
        if self.is_monitoring:
            self.force_refresh_flag = True
            self.web_time_label.config(text="Zooming...")

    def _on_canvas_scroll_zoom(self, event):
        """마우스 휠로 줌 확대/축소 (0.1 단위)"""
        current_zoom = self.web_zoom_var.get()
        new_zoom = current_zoom

        # Linux (Button-4: Up, Button-5: Down) / Windows (delta) 판별
        if event.num == 4 or event.delta > 0:
            new_zoom += 0.1 # 확대
        elif event.num == 5 or event.delta < 0:
            new_zoom -= 0.1 # 축소

        # 범위 제한 (0.5배 ~ 2.5배)
        new_zoom = max(0.5, min(2.5, new_zoom))

        # 값이 변했으면 적용
        if new_zoom != current_zoom:
            self.web_zoom_var.set(new_zoom)
            self.zoom_label.config(text=f"{new_zoom*100:.0f}%")

            # 모니터링 중이라면 즉시 화면 갱신 요청
            if self.is_monitoring:
                self.force_refresh_flag = True
                # 캔버스 중앙에 줌 상태 표시 (잠깐)
                if self.canvas_text_id:
                     self.web_canvas.itemconfig(self.canvas_text_id, text=f"Zoom: {new_zoom*100:.0f}%")

    def _reset_zoom(self):
        """줌을 100%로 초기화하고 즉시 갱신 (Canvas 호환 수정)"""
        self.web_zoom_var.set(1.0)
        self.zoom_label.config(text="100%")
        
        if self.is_monitoring:
            self.force_refresh_flag = True
            # [수정] 이미지가 있으면 굳이 텍스트로 안 바꿔도 됨 (화면 깜빡임 방지)
            # 텍스트가 살아있는 경우에만 업데이트
            if self.canvas_text_id and not self.web_image_id:
                self.web_canvas.itemconfig(self.canvas_text_id, text="Resetting Zoom...")

    def manual_refresh_web(self):
        """사용자가 Refresh 버튼을 누르면 즉시 화면을 갱신합니다."""
        if self.is_monitoring:
            self.force_refresh_flag = True
            self.web_time_label.config(text="Refreshing...", foreground="orange")
        else:
            messagebox.showinfo("Info", "Monitoring is not running.")

    def toggle_web_monitoring(self):
        """모니터링 시작/정지 (Canvas 호환 수정)"""
        if not self.is_monitoring:
            # [시작]
            target_url = self.web_url_var.get()
            
            if not self._check_connection(target_url):
                ans = messagebox.askyesno(
                    "Connection Failed",
                    "Unable to access the website. (VPN verification required)\n\n"
                    "Would you like to run Cisco AnyConnect (VPN) now?"
                )
                if ans:
                    self.controller.run_cisco()
                return

            self.is_monitoring = True
            self.web_btn.config(text="Stop Monitor (Running)") 
            
            if self.canvas_text_id:
                self.web_canvas.itemconfig(self.canvas_text_id, text="Initializing Browser...")
            
            threading.Thread(target=self._start_browser_loop, daemon=True).start()

        else:
            self.is_monitoring = False
            self.web_btn.config(text="Start Monitor") 
            
            self.web_canvas.delete("all")
            self.web_image_id = None
            
            w = self.web_canvas.winfo_width() / 2
            h = self.web_canvas.winfo_height() / 2
            self.canvas_text_id = self.web_canvas.create_text(
                w, h, text="Monitoring Stopped", font=("Helvetica", 14), fill="gray"
            )

            if self.driver:
                self.driver.quit()
                self.driver = None

    def _check_connection(self, url):
        """해당 URL로 짧은 요청을 보내 VPN 연결 여부를 판단"""
        try:
            requests.get(url, timeout=5) 
            return True
        except:
            return False

    def _start_browser_loop(self):
        """[Enhanced] 페이지를 새로고침하여 연결 상태를 확실히 체크"""
        try:
            options = Options()
            options.add_argument("--headless")
            options.add_argument(f"--window-size={self.monitor_w},{self.monitor_h}")
            options.add_argument("--disable-gpu")
            options.add_argument("--no-sandbox")
            
            self.driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
            self.driver.get(self.web_url_var.get())

            while self.is_monitoring:
                if not self.driver: break
                
                try:
                    # 1. 크기 및 줌 설정
                    current_w = self.web_canvas.winfo_width()
                    current_h = self.web_canvas.winfo_height()
                    if current_w > 100: self.monitor_w = current_w
                    if current_h > 100: self.monitor_h = current_h
                    
                    zoom_factor = self.web_zoom_var.get()
                    target_h = int(self.monitor_h * zoom_factor)
                    
                    self.driver.set_window_size(self.monitor_w, target_h)
                    
                    self.driver.refresh()
                    
                    self.driver.execute_script(f"document.body.style.zoom='{zoom_factor}'")

                    png_data = self.driver.get_screenshot_as_png()
                    pil_image = Image.open(io.BytesIO(png_data))
                    
                    self.web_connection_status = True
                    self.master.after(0, lambda img=pil_image: self._update_web_image(img))
                    
                    current_time = time.strftime("%Y-%m-%d %H:%M:%S")
                    self.master.after(0, lambda: self.web_time_label.config(text=f"Updated: {current_time} (interval = 60sec)", foreground="#007bff"))
                    
                    self.force_refresh_flag = False 
                    
                except Exception as e:
                    print(f"Capture Error: {e}")
                    self.web_connection_status = False
                    self.master.after(0, self._show_error_on_canvas)
                    self.master.after(0, lambda: self.web_time_label.config(text="Connection Lost", foreground="red"))

                for _ in range(60): 
                    if not self.is_monitoring: break
                    if self.force_refresh_flag: break 
                    time.sleep(1)

        except Exception as e:
            print(f"Browser Init Error: {e}")
            self.web_connection_status = False
            self.is_monitoring = False
            self.master.after(0, lambda: self.web_btn.config(text="Start Monitor"))
        
        finally:
            self.web_connection_status = False


    def _show_error_on_canvas(self):
        self.web_canvas.delete("all")
        self.web_image_id = None
        w = self.web_canvas.winfo_width() / 2
        h = self.web_canvas.winfo_height() / 2
        self.canvas_text_id = self.web_canvas.create_text(
            w, h, text="Connection Lost\nRetrying...", font=("Helvetica", 14), fill="red", justify="center"
        )

    def _update_web_image(self, pil_image):
        """메인 스레드: 캔버스에 이미지를 그리고 스크롤 영역을 갱신"""
        try:
            # 1. Tkinter 호환 이미지 생성
            photo = ImageTk.PhotoImage(pil_image)
            
            # 2. 기존 이미지 삭제 및 새 이미지 생성
            if self.web_image_id:
                self.web_canvas.delete(self.web_image_id)
                
            # 3. 안내 문구 삭제 (첫 실행 시)
            if self.canvas_text_id:
                self.web_canvas.delete(self.canvas_text_id)
                self.canvas_text_id = None

            # 4. 이미지 그리기 (좌상단 0,0 기준)
            self.web_image_id = self.web_canvas.create_image(0, 0, image=photo, anchor="nw")
            
            # 5. [핵심] 스크롤 영역(ScrollRegion)을 이미지 크기에 맞춤
            # 이렇게 해야 드래그나 스크롤바가 끝까지 닿습니다.
            self.web_canvas.config(scrollregion=self.web_canvas.bbox("all"))
            
            # 6. 이미지 참조 유지 (GC 방지)
            self.web_canvas.image = photo 

            # 시간 업데이트 (Canvas 위에 텍스트로 표시하려면 별도 create_text 필요)
            # 여기서는 간단히 윈도우 타이틀이나 상태바 등으로 대체 가능하나,
            # 깔끔하게 우측 하단에 시간을 띄워드리겠습니다.
            self.web_canvas.delete("timestamp_tag")
            current_time = time.strftime("%H:%M:%S")
            w = pil_image.width
            h = pil_image.height
            # 우측 하단에 반투명 박스 느낌으로 시간 표시
            self.web_canvas.create_text(w - 60, h - 20, text=f"Updated: {current_time}", 
                                        fill="red", font=("Helvetica", 10, "bold"), tag="timestamp_tag")

        except Exception as e:
            print(f"Image Update Error: {e}") 

    def _create_ups_monitoring_tab(self, parent):
        container = ttk.Frame(parent, padding=15)
        container.pack(fill=tk.BOTH, expand=True)

        conn_frame = ttk.LabelFrame(container, text="UPS Connection (RS232C) OMRON BA100R ds-1423816", padding=10)
        conn_frame.pack(fill=tk.X, pady=(0, 15))
        ttk.Label(conn_frame, text="Port:").pack(side=tk.LEFT)
    
        self.ups_port_combo = ttk.Combobox(conn_frame, width=20, state="normal")
        self.ups_port_combo.pack(side=tk.LEFT, padx=5)

        self.ups_search_btn = ttk.Button(conn_frame, text="Search Ports 🔍", 
                                         command=self.controller.search_ups_ports)
        self.ups_search_btn.pack(side=tk.LEFT, padx=5)

        self.ups_conn_btn = ttk.Button(conn_frame, text="Connect UPS", 
                                        command=self.controller.toggle_ups_connection,
                                       state="disabled")
        self.ups_conn_btn.pack(side=tk.LEFT, padx=5)

        self.ups_refresh_btn = ttk.Button(conn_frame, text="Refresh Status 🔄", 
                                        command=self.controller.manual_refresh_ups,
                                          state="disabled")
        self.ups_refresh_btn.pack(side=tk.LEFT, padx=5)

        # [ui_manager.py] _create_ups_monitoring_tab 내부에 추가
        self.ups_diag_btn = ttk.Button(conn_frame, text="Diagnosis 🛠️",
                                       command=self.controller.diagnose_ups)
        self.ups_diag_btn.pack(side=tk.LEFT, padx=5)

        ttk.Label(conn_frame, textvariable=self.ups_vars["conn_status"], 
                  font=("Helvetica", 10, "bold")).pack(side=tk.RIGHT)

        mid_frame = ttk.Frame(container)
        mid_frame.pack(fill=tk.X, pady=5)

        # 2-1: Power Levels (Gauge)
        gauge_pane = ttk.LabelFrame(mid_frame, text=" Power Levels ", padding=10)
        gauge_pane.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))

        ttk.Label(gauge_pane, text="Battery Level").pack(anchor="w")
        self.ups_batt_bar = ttk.Progressbar(gauge_pane, variable=self.ups_vars["batt_level"], maximum=100)
        self.ups_batt_bar.pack(fill=tk.X, pady=2)
        ttk.Label(gauge_pane, textvariable=self.ups_vars["batt_level"], font=("Helvetica", 11, "bold")).pack()

        ttk.Label(gauge_pane, text="UPS Load").pack(anchor="w", pady=(10, 0))
        self.ups_load_bar = ttk.Progressbar(gauge_pane, variable=self.ups_vars["load_level"], maximum=100)
        self.ups_load_bar.pack(fill=tk.X, pady=2)
        ttk.Label(gauge_pane, textvariable=self.ups_vars["load_level"], font=("Helvetica", 11, "bold")).pack()

        # 2-2: Electrical Info (Text)
        info_pane = ttk.LabelFrame(mid_frame, text=" Electrical Info ", padding=10)
        info_pane.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5)

        large_font = ("Helvetica", 30, "bold")
        label_font = ("Helvetica", 14) 

        items = [("Input Voltage", "input_volt"), ("Output Voltage", "output_volt"),
                 ("Frequency", "frequency"), ("Current Status", "status_msg")]

        for label, var_key in items:
            row = ttk.Frame(info_pane)
            row.pack(fill=tk.X, pady=2)
            ttk.Label(row, text=f"{label}:", width=16, font=label_font).pack(side=tk.LEFT)
            val_lbl = ttk.Label(row, textvariable=self.ups_vars[var_key], font=large_font, foreground="blue")
            val_lbl.pack(side=tk.LEFT)
            self.ups_value_labels.append(val_lbl) 

        # 2-3: Outlet Status (2x2 Grid)
        outlet_pane = ttk.LabelFrame(mid_frame, text=" Outlet Status (2x2), You can change this outlet and YOU Must FIX THE CODE (main.py)", padding=10)
        outlet_pane.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(5, 0))

        self.outlet_canvas = tk.Canvas(outlet_pane, width=180, height=140, highlightthickness=0)
        self.outlet_canvas.pack(pady=5)

        self.outlet_circles = []
        labels = ["DAQ, PC, Electroncis, etc.", "High voltage", "Empty", "Empty"]

        # ui_manager.py 내부 수정

        for i in range(4):
            row, col = divmod(i, 2)
            x0, y0 = 30 + (col * 80), 15 + (row * 60)
            x1, y1 = x0 + 40, y0 + 40
            
            color = "#adb5bd" 
            
            circle = self.outlet_canvas.create_oval(x0, y0, x1, y1, fill=color, outline="#333", width=2)
            self.outlet_canvas.create_text(x0 + 20, y1 + 10, text=labels[i], font=("Helvetica", 8, "bold"))
            self.outlet_circles.append(circle)

        ctrl_bar = ttk.Frame(container)
        ctrl_bar.pack(fill=tk.X, pady=(10, 0), side=tk.BOTTOM)

        #ttk.Label(ctrl_bar, text="Target:").pack(side=tk.LEFT)
        #self.shutdown_target_var = tk.StringVar(value="All Outlets")
        #self.shutdown_combo = ttk.Combobox(ctrl_bar, textvariable=self.shutdown_target_var,
         #                                  values=["All Outlets", "Outlet 1 (DAQ)", "Outlet 2 (Laser)", "Outlet 3", "Outlet 4"],
         #                                  state="readonly", width=15)
        #self.shutdown_combo.pack(side=tk.LEFT, padx=10)

        self.btn_ups_shutdown = tk.Button(ctrl_bar, text="⚠️ EXECUTE SYSTEM WIDE SHUTDOWN",
                                          bg="#dc3545", fg="white", font=("Helvetica", 12, "bold"),
                                          height=2, command=self.controller.shutdown_ups_all)
        self.btn_ups_shutdown.pack(fill=tk.X, padx=100)

        graph_frame = ttk.LabelFrame(container, text=" UPS Real-time Trend ", padding=5)
        graph_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        self.fig_ups, self.axes_ups = plt.subplots(2, 2, figsize=(10, 8), dpi=100)
        self.ax_ups_watt = self.axes_ups[0, 0] # 좌상: 전력
        self.ax_ups_temp = self.axes_ups[0, 1] # 우상: 온도
        self.ax_ups_vin  = self.axes_ups[1, 0] # 좌하: 입력전압
        self.ax_ups_vout = self.axes_ups[1, 1] # 우하: 출력전압
        
        self.fig_ups.tight_layout(pad=4.0)
        
        self.canvas_ups = FigureCanvasTkAgg(self.fig_ups, master=graph_frame)
        self.ups_toolbar = NavigationToolbar2Tk(self.canvas_ups, graph_frame)
        self.ups_toolbar.update()
        self.ups_toolbar.pack(side=tk.TOP, fill=tk.X, pady=(5,0))
        self.canvas_ups.get_tk_widget().pack(fill=tk.BOTH, expand=True, pady=(5, 5))

        
    def update_ups_outlet_display(self, load_percent):
        state = 1 if load_percent > 0 else 0
        self.controller.update_ups_outlet_status([state, state, 0, 0])

    ###################################################################
    def _create_status_dashboard(self, parent):
        dashboard = ttk.LabelFrame(parent, text=" System Connection Overview ", padding=10)
        dashboard.pack(fill=tk.X, pady=(0, 10), padx=5)

        if self.unlock_btn:
            self.unlock_btn.master = dashboard 
            self.unlock_btn.pack(side=tk.RIGHT, padx=10)        

        inner_container = ttk.Frame(dashboard)
        inner_container.pack(expand=True)

        self.status_widgets = {}
        devices = [
            ("DAQ System", "DAQ"), 
            ("HV System", "HV"), 
            ("Env Sensor", "Env"), 
            ("Laser Controller", "Laser"), 
            ("B-field Monitor", "B-field"), 
            ("OMRON UPS", "UPS")
        ]

        for i, (label, key) in enumerate(devices):
            frame = ttk.Frame(inner_container)
            frame.pack(side=tk.LEFT, padx=15)

            canvas = tk.Canvas(frame, width=20, height=20, highlightthickness=0)
            canvas.pack(side=tk.LEFT, padx=5)
            led = canvas.create_oval(2, 2, 18, 18, fill="#dc3545", outline="#333") 

            lbl = ttk.Label(frame, text=label, font=("Helvetica", 10, "bold"))
            lbl.pack(side=tk.LEFT)

            self.status_widgets[key] = {"led": led, "canvas": canvas}

        self.master.after(100, self._update_dashboard_loop)


    def _update_dashboard_loop(self):
        statuses = self.controller.get_system_status()

        statuses["B-field"] = getattr(self, "web_connection_status", False)

        # 탭 순서 매핑
        tab_map = {"DAQ": 0, "Laser": 1, "B-field": 2, "UPS": 3}

        for key, connected in statuses.items():
            color = "#28a745" if connected else "#dc3545"
            img = self.tab_led_green if connected else self.tab_led_red

            if key in self.status_widgets:
                self.status_widgets[key]["canvas"].itemconfig(self.status_widgets[key]["led"], fill=color)

            if key in tab_map:
                idx = tab_map[key]
                try:
                    self.main_notebook.tab(idx, image=img, compound=tk.RIGHT)
                except Exception:
                    pass

        self.master.after(2000, self._update_dashboard_loop)

    def _create_contact_tab(self, parent):
        container = ttk.Frame(parent, padding=20)
        container.pack(fill=tk.BOTH, expand=True)

        ttk.Label(container, text="🚨 Emergency Contact Network",
                  font=("Helvetica", 16, "bold"), foreground="#dc3545").pack(pady=(0, 20))

        columns = ("role", "name", "phone", "note")
        tree = ttk.Treeview(container, columns=columns, show="headings", height=15)

        tree.heading("role", text="Role / Affiliation")
        tree.heading("name", text="Name")
        tree.heading("phone", text="Phone Number")
        tree.heading("note", text="Note")

        tree.column("role", width=180, anchor="center")
        tree.column("name", width=150, anchor="center")
        tree.column("phone", width=180, anchor="center")
        tree.column("note", width=500, anchor="w")

        contacts_data = self.controller.load_contacts()
        for c in contacts_data:
            tree.insert("", tk.END, values=(c["role"], c["name"], c["phone"], c["note"]))

        tree.pack(fill=tk.BOTH, expand=True)

        scrollbar = ttk.Scrollbar(container, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    def refresh_ui_state(self):
        """제어권 상태에 따라 UI를 업데이트하되, 운영 모드(main.py)에서는 항상 활성화합니다."""
        
        # [수정] 모드 판별: access_mgr이 있으면 그 상태를 따르고, 없으면(main.py) 항상 True
        if hasattr(self.controller, 'access_mgr'):
            is_unlocked = self.controller.access_mgr.unlocked
        else:
            is_unlocked = True 
            
        state = tk.NORMAL if is_unlocked else tk.DISABLED
        
        # 제어권 버튼 업데이트 (테스트 모드일 때만)
        if self.unlock_btn:
            if is_unlocked:
                self.unlock_btn.config(text="🔓 Controls Active", bg="#28a745", fg="white")
            else:
                self.unlock_btn.config(text="🔒 Unlock Controls", bg="#f0ad4e", fg="black")

        # UPS 관련 버튼들 잠금/해제
        if hasattr(self, 'ups_conn_btn'): self.ups_conn_btn.config(state=state)
        if hasattr(self, 'ups_refresh_btn'): self.ups_refresh_btn.config(state=state)
        if hasattr(self, 'ups_diag_btn'): self.ups_diag_btn.config(state=state)
        if hasattr(self, 'btn_ups_shutdown'): self.btn_ups_shutdown.config(state=state)

        # Laser 관련 버튼들 잠금/해제
        if hasattr(self, 'laser_tabs_data'):
            for wl, vars_dict in self.laser_tabs_data.items():
                if "ld_on_btn" in vars_dict: vars_dict["ld_on_btn"].config(state=state)
                if "ld_off_btn" in vars_dict: vars_dict["ld_off_btn"].config(state=state)
                if "tec_on_btn" in vars_dict: vars_dict["tec_on_btn"].config(state=state)
                if "tec_off_btn" in vars_dict: vars_dict["tec_off_btn"].config(state=state)
                if "curr_apply_btn_obj" in vars_dict: vars_dict["curr_apply_btn_obj"].config(state=state)

    def setup_shortcuts(self):
        """DAQ 탭 전용 단축키 설정"""
        # 1. Configuration: Ctrl + O
        self.master.bind("<Control-o>", lambda e: self.controller.handle_button_click("open_config"))
        
        # 2. Produce: Ctrl + P
        self.master.bind("<Control-p>", lambda e: self.controller.handle_button_click("run_produce"))
        
        # 3. Analysis: Ctrl + A
        self.master.bind("<Control-a>", lambda e: self.controller.handle_button_click("run_analysis"))
        
        # 4. Waveform Inspection: Ctrl + S (요청하신 대로 s로 설정)
        self.master.bind("<Control-s>", lambda e: self.controller.handle_button_click("run_waveform"))
        
        # 5. Image Viewer: Ctrl + I (i로 설정)
        self.master.bind("<Control-i>", lambda e: self.controller.handle_button_click("open_image_viewer"))
        
        # 6. Refresh: F5
        self.master.bind("<F5>", lambda e: self.controller.refresh_data())

# ui_manager_test.py
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
from managers.ui_automation import AutomationUI


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

        self.run_mode = tk.StringVar(value="auto")
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

        right_pane = ttk.Frame(paned_window, padding=(0, 10, 10, 10))
        paned_window.add(right_pane, weight=3)

        self.notebook = ttk.Notebook(right_pane)
        self.notebook.pack(fill=tk.BOTH, expand=True)
        self.auto_ui = AutomationUI(self.notebook, self.controller)

        config_tab = ttk.Frame(self.notebook, padding=(10, 10, 10, 10))
        self.notebook.add(config_tab, text="PMT Setup & Helper")
        self._create_status_frame(config_tab)

        data_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(data_tab, text="Data Files")
        self._create_data_viewer(data_tab)

        log_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(log_tab, text="Log")
        self._create_log_viewer(log_tab)

        self._create_laser_control_tab(self.laser_main_frame)
        self._create_web_monitor_tab(self.main_notebook)
        
        self.ups_main_frame = ttk.Frame(self.main_notebook)
        self.main_notebook.add(self.ups_main_frame, text=" UPS Status ")
        self._create_ups_monitoring_tab(self.ups_main_frame)

        self.contact_frame = ttk.Frame(self.main_notebook)
        self.main_notebook.add(self.contact_frame, text=" ☎️ Emergency ")
        self._create_contact_tab(self.contact_frame)

    def on_config_loaded(self):
        self._update_pmt_status_and_helper() 
        self.update_config_display()
        self.update_path_display()
        if hasattr(self, 'auto_ui') and hasattr(self.auto_ui, 'update_run_info'):
            self.auto_ui.update_run_info()

        if hasattr(self, 'data_tree'):
            self.update_data_viewer(force_refresh=True)

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

    def _create_status_frame(self, parent):
        header_frame = ttk.Frame(parent)
        header_frame.pack(fill=tk.X, padx=5, pady=(5, 0))
        
        ttk.Label(header_frame, text=" PMT Status & Storage Overview (2x2) ", 
                  font=("Helvetica", 12, "bold")).pack(side=tk.LEFT)
        
        ttk.Button(header_frame, text="Refresh All 🔄", 
                   command=self.controller.refresh_all_data).pack(side=tk.RIGHT)

        self.pmt_status_frame = ttk.LabelFrame(parent, text="", padding="10")
        self.pmt_status_frame.pack(fill=tk.BOTH, expand=True, pady=5, padx=5)

    def _update_pmt_status_and_helper(self):
        if not self.controller.config_manager: return

        for widget in self.pmt_status_frame.winfo_children():
            widget.destroy()
        
        self.pmt_status_frame.columnconfigure(0, weight=1)
        self.pmt_status_frame.columnconfigure(1, weight=1)
        self.pmt_status_frame.rowconfigure(0, weight=1)
        self.pmt_status_frame.rowconfigure(1, weight=1)

        cfg = self.controller.config_manager.get_all_variables()
        
        POS_MAP_ANGLES = { 
            'E': 0, 'F': 45, 'G': 90, 'H': 135, 
            'A': 180, 'B': 225, 'C': 270, 'D': 315 
        }

        for i in range(1, 4):
            row, col = divmod(i-1, 2)
            cell = ttk.Frame(self.pmt_status_frame, padding=5, relief="groove")
            cell.grid(row=row, column=col, sticky="nsew", padx=3, pady=3)

            sn = cfg.get(f'SN{i}', "N/A")
            cable_type = cfg.get(f'direction{i}', "A").strip().upper() 
            hv = cfg.get(f'HV{i}', "0")
            try:
                rot_val = int(cfg.get(f'RotateAngle{i}', "0"))
            except:
                rot_val = 0
            tilt = cfg.get(f'TiltAngle{i}', "0")
            is_active = sn != "N/A" and sn.strip() != ""

            self._create_status_indicator(cell, f"SN{i}", is_active, side=tk.TOP)
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
        accent = self.colors["dark" if self.is_dark_mode else "light"]["accent"]
        title_font = ("Helvetica", 11)
        val_font = ("Helvetica", 16, "bold")

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
        bg_color = "#2d2d2d" if self.is_dark_mode else "white"
        txt_fill = 'white' if self.is_dark_mode else 'black'
        
        canvas = tk.Canvas(parent, width=280, height=200, bg=bg_color, highlightthickness=0) 
        canvas.pack(side=tk.LEFT, padx=10, expand=True)

        C_X, C_Y, R = 140, 100, 65 
        
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

        physical_cable_angle = 180 + rotation_angle
        cx1, cy1 = get_pos(physical_cable_angle, R - 5)
        cx2, cy2 = get_pos(physical_cable_angle, R + 30)
        canvas.create_line(cx1, cy1, cx2, cy2, arrow=tk.LAST, fill='red', width=3)
        
        ctx, cty = get_pos(physical_cable_angle, R + 42)
        norm_angle = physical_cable_angle % 360
        anchor = "center"
        if 45 < norm_angle < 135: anchor = "s"    
        elif 135 <= norm_angle < 225: anchor = "e" 
        elif 225 <= norm_angle < 315: anchor = "n" 
        else: anchor = "w"                         
        canvas.create_text(ctx, cty, text="Cable", font=("Helvetica", 10, "bold"), fill="red", anchor=anchor)

        std_type_angle = pos_map_angles.get(cable_type, 180)
        pin_offset = physical_cable_angle - std_type_angle

        label_font = ("Helvetica", 12, "bold")
        axis_label_font = ("Helvetica", 11, "bold")

        for char, std_angle in pos_map_angles.items():
            final_pin_angle = std_angle + pin_offset
            lx, ly = get_pos(final_pin_angle, R - 15)
            color = 'red' if char == cable_type else txt_fill
            canvas.create_text(lx, ly, text=char, font=label_font, fill=color)

            if char == 'A': 
                ax, ay = get_pos(final_pin_angle, R + 12)
                canvas.create_text(ax, ay, text="+Y", font=axis_label_font, fill="#c92a2a")
            elif char == 'G': 
                gx, gy = get_pos(final_pin_angle, R + 12)
                canvas.create_text(gx, gy, text="+X", font=axis_label_font, fill="#1971c2")

        dy_r = 15
        dy1_x, dy1_y = get_pos(90 + pin_offset, dy_r)
        dy2_x, dy2_y = get_pos(270 + pin_offset, dy_r)
        canvas.create_oval(C_X-2, C_Y-2, C_X+2, C_Y+2, fill="gray", outline="")
        canvas.create_text(dy1_x, dy1_y, text="DY1", font=("Helvetica", 9, "bold"), fill=txt_fill)
        canvas.create_text(dy2_x, dy2_y, text="DY2", font=("Helvetica", 9, "bold"), fill=txt_fill)

    def _create_run_control_frame(self, parent):
        frame = ttk.LabelFrame(parent, text=" 📊 Run Mode & Parameters ", padding="10")
        frame.pack(fill=tk.X, pady=5, padx=5)

        ttk.Label(frame, text="1. Operation Category:", font=("Helvetica", 10, "bold")).pack(anchor=tk.W)
        rb_auto = ttk.Radiobutton(frame, text=" General Scan (Auto Control)", 
                                  variable=self.run_mode, value="auto",
                                  command=self.controller.handle_mode_change)
        rb_auto.pack(anchor=tk.W, padx=10, pady=2)

        rb_manual = ttk.Radiobutton(frame, text=" Manual Mode (Laser/Dark Selection)", 
                                    variable=self.run_mode, value="manual",
                                    command=self.controller.handle_mode_change)
        rb_manual.pack(anchor=tk.W, padx=10, pady=2)

        ttk.Separator(frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=10)

        ttk.Label(frame, text="2. Manual Sub-selection:", font=("Helvetica", 10)).pack(anchor=tk.W)
        self.manual_type_var = tk.StringVar(value="laser")
        self.rb_laser = ttk.Radiobutton(frame, text=" Laser & External trigger (0)", 
                                        variable=self.manual_type_var, value="laser",
                                        command=self.controller.handle_mode_change)
        self.rb_laser.pack(anchor=tk.W, padx=25)

        self.rb_dark = ttk.Radiobutton(frame, text=" Dark & Self trigger (1)", 
                                       variable=self.manual_type_var, value="dark",
                                       command=self.controller.handle_mode_change)
        self.rb_dark.pack(anchor=tk.W, padx=25)

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

    def _create_path_viewer_frame(self, parent):
        frame = ttk.LabelFrame(parent, text="File & Directory Paths", padding="10")
        frame.pack(fill=tk.X, pady=5, padx=5)

        self.path_container = ttk.Frame(frame)
        self.path_container.pack(fill=tk.X, pady=(0, 5))

        self.path_labels = {}
        path_keys = ['BasePath', 'RawDataPath', 'ExternalPath']

        for key in path_keys:
            path_frame_inner = ttk.Frame(self.path_container)
            path_frame_inner.pack(fill=tk.X, pady=2)

            ttk.Label(path_frame_inner, text=f"{key}:", width=16).pack(side=tk.LEFT)
            path_label = ttk.Label(path_frame_inner, text="N/A", anchor='w')
            path_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
            self.path_labels[key] = path_label 

            ttk.Button(path_frame_inner, text=">", width=2,
                       command=lambda p=key: self.controller.open_terminal_at_path_by_key(p)).pack(side=tk.RIGHT, padx=(5,0))

        self.path_container.bind("<Configure>", lambda e: [l.config(wraplength=e.width-150) for l in self.path_labels.values()])

    def update_data_size_display(self, size_str, is_external=False):
        if is_external: self.ext_data_size_var.set(size_str)
        else: self.data_size_var.set(size_str)

    def update_path_display(self):
        if not self.controller.config_manager:
            for label in self.path_labels.values(): label.config(text="Config not loaded.")
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
        ttk.Button(button_frame, text="Move Selected File(s) 🚚", command=self.on_move_selected_files).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        ttk.Button(left_data_frame, text="Delete Selected File(s) 🗑️", command=self.on_delete_selected_files).pack(fill=tk.X, padx=5, pady=(5,0))

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
            self.file_info_label.config(text=f"File: {os.path.basename(file_path)}\n\nSize: {size_mb:.2f} MB\nModified: {mtime}")
        except Exception as e:
            self.file_info_label.config(text=f"Error: {e}")

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
        ttk.Entry(search_frame, textvariable=search_var).pack(fill=tk.X)
        search_var.trace_add("write", lambda *args: self.update_data_viewer())

        sort_frame = ttk.LabelFrame(control_frame, text="Sort By", padding=5)
        sort_frame.pack(side=tk.LEFT)
        sort_mode = tk.StringVar(value="time")
        ttk.Button(sort_frame, text="Name", command=lambda: self._set_sort_and_update(tab_type, 'name')).pack(side=tk.LEFT)
        ttk.Button(sort_frame, text="Time", command=lambda: self._set_sort_and_update(tab_type, 'time')).pack(side=tk.LEFT)

        ttk.Button(control_frame, text="Refresh 🔄", command=self.controller.refresh_all_data).pack(side=tk.RIGHT, padx=5)

        tree_frame = ttk.Frame(parent_tab)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        tree = ttk.Treeview(tree_frame, columns=("filename", "path", "mtime"), show="headings", selectmode="extended")
        tree.heading("filename", text="File Name")
        tree.heading("path", text="Path")
        tree.heading("mtime", text="Modified")
        
        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        tree.pack(fill=tk.BOTH, expand=True)

        tree.bind("<Double-1>", self.on_data_file_double_click)
        tree.bind("<<TreeviewSelect>>", self.on_data_file_select)
        self.data_view_vars[tab_type] = {"tree": tree, "filter_mode": filter_mode, "sort_mode": sort_mode, "search_var": search_var}

    def _set_sort_and_update(self, tab_type, mode):
        self.data_view_vars[tab_type]["sort_mode"].set(mode)
        self.update_data_viewer()

    def update_data_viewer(self, force_refresh=False):
        if force_refresh: self.all_data_files = self.controller.get_data_files()
        for tab_type, vars in self.data_view_vars.items():
            tree = vars["tree"]
            filter_mode, sort_mode, search_query = vars["filter_mode"].get(), vars["sort_mode"].get(), vars["search_var"].get().lower()
            filtered = [f for f in self.all_data_files if f["type"] == tab_type]
            if filter_mode != "All": filtered = [f for f in filtered if f"_{filter_mode.lower()}" in f["filename"]]
            if sort_mode == 'name': filtered.sort(key=lambda x: x["filename"])
            else: filtered.sort(key=lambda x: x["mtime_float"], reverse=True)
            if search_query: filtered = [f for f in filtered if search_query in f["filename"].lower()]
            tree.delete(*tree.get_children())
            for f in filtered: tree.insert("", tk.END, values=(f["filename"], f["path"], f["mtime"]))

    def on_data_file_double_click(self, event):
        tree = event.widget 
        if tree.selection():
            filename, dir_path, _ = tree.item(tree.selection()[0], "values")
            self.controller.open_root_file_browser(os.path.join(dir_path, filename))

    def on_data_file_select(self, event):
        tree = event.widget
        if tree.selection():
            filename, dir_path, _ = tree.item(tree.selection()[0], "values")
            self.update_file_info_panel(os.path.join(dir_path, filename))

    def open_image_viewer(self):
        if self.image_viewer_window and self.image_viewer_window.winfo_exists():
            self.image_viewer_window.lift(); self.image_viewer_window.focus_force(); return
        if self.controller.config_manager:
            self.image_viewer_window = ImageViewer(self.master, self.controller.config_manager)
            self.image_viewer_window.protocol("WM_DELETE_WINDOW", self._on_image_viewer_close)
        else: messagebox.showwarning("Warning", "Configuration not loaded.")

    def _on_image_viewer_close(self):
        if self.image_viewer_window: self.image_viewer_window.destroy()
        self.image_viewer_window = None

    def on_delete_selected_files(self):
        try:
            tab_type = self.data_notebook.tab(self.data_notebook.select(), "text").replace(" Data", "")
            tree = self.data_view_vars[tab_type]["tree"]
            selected = tree.selection()
            if not selected: messagebox.showwarning("No Selection", "Select files to delete."); return
            paths = [os.path.join(tree.item(i, "values")[1], tree.item(i, "values")[0]) for i in selected]
            self.controller.delete_data_files(paths)
        except Exception as e: messagebox.showerror("Error", f"Deletion failed: {e}")

    def get_selected_file_paths(self):
        try:
            tab_type = self.data_notebook.tab(self.data_notebook.select(), "text").replace(" Data", "")
            tree = self.data_view_vars[tab_type]["tree"]
            return [os.path.join(tree.item(i, "values")[1], tree.item(i, "values")[0]) for i in tree.selection()]
        except: return []

    def get_run_num(self):
        run_num = self.run_number_var.get()
        if not run_num or not run_num.isdigit():
            messagebox.showwarning("Input Required", "Valid Run Number needed.")
            return None
        return run_num

    def _create_laser_control_tab(self, parent):
        main_container = ttk.Frame(parent, padding=10)
        main_container.pack(fill=tk.BOTH, expand=True)
        self.laser_sub_notebook = ttk.Notebook(main_container)
        self.laser_sub_notebook.pack(fill=tk.BOTH, expand=True)
        self.laser_tabs_data = {} 
        wavelengths = ["375nm", "405nm", "450nm", "473nm"]
        for wl in wavelengths:
            tab_frame = ttk.Frame(self.laser_sub_notebook)
            self.laser_sub_notebook.add(tab_frame, text=f" {wl} ")
            vars_dict = {
                "conn_status_txt": tk.StringVar(value="Disconnected"), 
                "ld_status": tk.StringVar(value="OFF"), "tec_status": tk.StringVar(value="OFF"),
                "temp": tk.StringVar(value="--.- °C"), "bias_live": tk.StringVar(value="---.- mA"),
                "pulse_live": tk.StringVar(value="---.- mA"), "bias_set": tk.DoubleVar(value=0.0),
                "pulse_set": tk.DoubleVar(value=132.99 if wl == "405nm" else 0.0),
                "trigger_mode": tk.StringVar(value="External"), "freq_hz": tk.StringVar(value="10000000"),
                "check_interval": tk.StringVar(value="1s")
            }
            self.laser_tabs_data[wl] = vars_dict
            self._build_individual_laser_ui(tab_frame, wl, vars_dict)

    def _build_individual_laser_ui(self, tab_parent, wl, vars_dict):
        conn_frame = ttk.Frame(tab_parent, padding=5, relief="groove", borderwidth=1)
        conn_frame.pack(fill=tk.X, padx=5, pady=5)
        ttk.Label(conn_frame, textvariable=vars_dict["conn_status_txt"], font=("Helvetica", 12, "bold"), foreground="red").pack(side=tk.LEFT, padx=(10, 20))

        # [바인딩 수정] call_mgr 사용
        ttk.Button(conn_frame, text="🔌 Connect", width=12, command=lambda: self.controller.call_mgr("laser", "connect_single_laser", wl)).pack(side=tk.LEFT, padx=2)
        ttk.Button(conn_frame, text="❌ Disconnect", width=12, command=lambda: self.controller.call_mgr("laser", "disconnect_single_laser", wl)).pack(side=tk.LEFT, padx=2)
        ttk.Separator(conn_frame, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10, pady=2)
        ttk.Button(conn_frame, text="Refresh 🔄", width=10, command=lambda: self.controller.call_mgr("laser", "manual_refresh_laser", wl)).pack(side=tk.LEFT, padx=2)
        ttk.Button(conn_frame, text="Load History 📂", command=lambda: self.controller.call_mgr("laser", "load_historical_laser_data", wl)).pack(side=tk.RIGHT, padx=5)

        laser_pane = ttk.PanedWindow(tab_parent, orient=tk.HORIZONTAL)
        laser_pane.pack(fill=tk.BOTH, expand=True)
        left_pane, right_pane = ttk.Frame(laser_pane), ttk.Frame(laser_pane)
        laser_pane.add(left_pane, weight=1); laser_pane.add(right_pane, weight=2)

        self._create_laser_settings_frames_multi(left_pane, wl, vars_dict)
        self._create_laser_live_labels_multi(right_pane, vars_dict)

        trig_frame = ttk.LabelFrame(left_pane, text=f"Trigger Control ({wl})", padding=10)
        trig_frame.pack(fill=tk.X, pady=5)
        ttk.Label(trig_frame, text="Mode:").pack(side=tk.LEFT, padx=5)
        ttk.Combobox(trig_frame, textvariable=vars_dict["trigger_mode"], values=["Internal (PG1)", "Internal (PG2)", "External"], state="readonly", width=15).pack(side=tk.LEFT, padx=5)
        ttk.Entry(trig_frame, textvariable=vars_dict["freq_hz"], width=12).pack(side=tk.LEFT, padx=5)
        
        # [바인딩 수정] call_mgr 사용
        ttk.Button(trig_frame, text="Apply", command=lambda: self.controller.call_mgr("laser", "apply_laser_frequency_multi", wl)).pack(side=tk.LEFT, padx=5)

        left_notebook = ttk.Notebook(left_pane)
        left_notebook.pack(fill=tk.BOTH, expand=True, pady=10)
        hist_tab = ttk.Frame(left_notebook); left_notebook.add(hist_tab, text=" Historical Plot ")
        vars_dict["fig_hist"], vars_dict["ax_hist"] = plt.subplots(figsize=(4, 2.5), dpi=80)
        vars_dict["canvas_hist"] = FigureCanvasTkAgg(vars_dict["fig_hist"], master=hist_tab)
        vars_dict["canvas_hist"].get_tk_widget().pack(fill=tk.BOTH, expand=True)

        monitor = ttk.LabelFrame(right_pane, text=f"Real-time Monitoring ({wl})", padding=5)
        monitor.pack(fill=tk.BOTH, expand=True, pady=5)
        vars_dict["fig"], (vars_dict["ax_temp"], vars_dict["ax_curr"]) = plt.subplots(2, 1, sharex=True, figsize=(6, 6), dpi=100)
        vars_dict["canvas"] = FigureCanvasTkAgg(vars_dict["fig"], master=monitor)
        vars_dict["canvas"].get_tk_widget().pack(fill=tk.BOTH, expand=True)

    def update_laser_status_colors(self, wl, ld_on, tec_on):
        vars_dict = self.laser_tabs_data.get(wl)
        if vars_dict:
            if "ld_label_obj" in vars_dict: vars_dict["ld_label_obj"].config(foreground="#28a745" if ld_on else "#dc3545")
            if "tec_label_obj" in vars_dict: vars_dict["tec_label_obj"].config(foreground="#28a745" if tec_on else "#dc3545")

    def _create_laser_settings_frames_multi(self, parent, wl, vars_dict):
        pwr_frame = ttk.LabelFrame(parent, text=f"Power Control ({wl})", padding=10)
        pwr_frame.pack(fill=tk.X, pady=5)
        
        # [바인딩 수정] call_mgr 사용
        vars_dict["ld_on_btn"] = ttk.Button(pwr_frame, text="LD ON", state=tk.DISABLED, command=lambda: self.controller.call_mgr("laser", "set_laser_ld_safe", wl, True))
        vars_dict["ld_on_btn"].pack(side=tk.LEFT, padx=5)
        vars_dict["ld_off_btn"] = ttk.Button(pwr_frame, text="LD OFF", state=tk.DISABLED, command=lambda: self.controller.call_mgr("laser", "set_laser_ld_safe", wl, False))
        vars_dict["ld_off_btn"].pack(side=tk.LEFT, padx=5)
        ttk.Separator(pwr_frame, orient=tk.VERTICAL).pack(side=tk.LEFT, padx=10, fill=tk.Y)
        vars_dict["tec_on_btn"] = ttk.Button(pwr_frame, text="TEC ON", state=tk.DISABLED, command=lambda: self.controller.call_mgr("laser", "set_laser_tec_multi", wl, True))
        vars_dict["tec_on_btn"].pack(side=tk.LEFT, padx=5)
        vars_dict["tec_off_btn"] = ttk.Button(pwr_frame, text="TEC OFF", state=tk.DISABLED, command=lambda: self.controller.call_mgr("laser", "set_laser_tec_multi", wl, False))
        vars_dict["tec_off_btn"].pack(side=tk.LEFT, padx=5)

        curr_frame = ttk.LabelFrame(parent, text="Current Settings (mA)", padding=10)
        curr_frame.pack(fill=tk.X, pady=5)
        self._create_laser_slider(curr_frame, "Bias:", vars_dict["bias_set"])
        self._create_laser_slider(curr_frame, "Pulse:", vars_dict["pulse_set"])
        
        # [바인딩 수정] call_mgr 사용
        vars_dict["curr_apply_btn_obj"] = ttk.Button(curr_frame, text="Apply Currents", state=tk.DISABLED, command=lambda: self.controller.call_mgr("laser", "apply_laser_currents_multi", wl))
        vars_dict["curr_apply_btn_obj"].pack(fill=tk.X, pady=10)

    def _create_laser_live_labels_multi(self, parent, vars_dict):
        status_grid = ttk.LabelFrame(parent, text="Live Status", padding=10)
        status_grid.pack(fill=tk.X, pady=5)
        for text, key in [("LD Status", "ld_status"), ("TEC Status", "tec_status"), ("Temperature", "temp"), ("Live Pulse", "pulse_live"), ("Check Int.", "check_interval")]:
            row = ttk.Frame(status_grid); row.pack(fill=tk.X, pady=2)
            ttk.Label(row, text=f"{text}:", width=15, font=("Helvetica", 10, "bold")).pack(side=tk.LEFT)
            lbl = ttk.Label(row, textvariable=vars_dict[key], width=15, relief="groove")
            lbl.pack(side=tk.LEFT)
            if key == "ld_status": vars_dict["ld_label_obj"] = lbl
            if key == "tec_status": vars_dict["tec_label_obj"] = lbl

    def _create_laser_slider(self, parent, label, var):
        frame = ttk.Frame(parent); frame.pack(fill=tk.X, pady=2)
        ttk.Label(frame, text=label, width=10).pack(side=tk.LEFT)
        ttk.Scale(frame, from_=0, to=200, variable=var, orient=tk.HORIZONTAL).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=10)
        ttk.Entry(frame, textvariable=var, width=8).pack(side=tk.LEFT)

    def _create_web_monitor_tab(self, parent_notebook):
        tab = ttk.Frame(parent_notebook); parent_notebook.add(tab, text=" B-field Monitoring ") 
        ctrl = ttk.Frame(tab, padding=5); ctrl.pack(fill=tk.X)
        self.web_url_var = tk.StringVar(value="https://www-sk1.icrr.u-tokyo.ac.jp/~yufei/precal_monitoring/")
        ttk.Entry(ctrl, textvariable=self.web_url_var, width=50, state="readonly").pack(side=tk.LEFT, padx=5)
        self.web_zoom_var = tk.DoubleVar(value=1.0)
        ttk.Scale(ctrl, from_=0.5, to=2.5, variable=self.web_zoom_var, orient=tk.HORIZONTAL, length=150).pack(side=tk.LEFT, padx=2)
        ttk.Button(ctrl, text="Start Monitor", command=self.toggle_web_monitoring).pack(side=tk.LEFT, padx=10)
        self.web_time_label = ttk.Label(ctrl, text="", font=("Helvetica", 14, "bold"), foreground="#007bff")
        self.web_time_label.pack(side=tk.LEFT, padx=15)

        self.canvas_frame = ttk.Frame(tab); self.canvas_frame.pack(fill=tk.BOTH, expand=True)
        self.web_canvas = tk.Canvas(self.canvas_frame, bg="#e1e1e1")
        self.web_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.web_canvas.bind("<ButtonPress-1>", lambda e: self.web_canvas.scan_mark(e.x, e.y))
        self.web_canvas.bind("<B1-Motion>", lambda e: self.web_canvas.scan_dragto(e.x, e.y, gain=1))

    def toggle_web_monitoring(self):
        if not self.is_monitoring:
            self.is_monitoring = True; self.web_btn.config(text="Stop Monitor")
            threading.Thread(target=self._start_browser_loop, daemon=True).start()
        else:
            self.is_monitoring = False; self.web_btn.config(text="Start Monitor")
            if self.driver: self.driver.quit(); self.driver = None

    def _start_browser_loop(self):
        try:
            options = Options(); options.add_argument("--headless")
            self.driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
            while self.is_monitoring:
                self.driver.get(self.web_url_var.get())
                png = self.driver.get_screenshot_as_png()
                img = Image.open(io.BytesIO(png))
                self.master.after(0, lambda: self._update_web_image(img))
                time.sleep(60)
        except Exception as e: print(f"Browser Error: {e}")

    def _update_web_image(self, pil_img):
        photo = ImageTk.PhotoImage(pil_img)
        self.web_canvas.delete("all")
        self.web_canvas.create_image(0, 0, image=photo, anchor="nw")
        self.web_canvas.image = photo

    def _create_ups_monitoring_tab(self, parent):
        container = ttk.Frame(parent, padding=15); container.pack(fill=tk.BOTH, expand=True)
        conn = ttk.LabelFrame(container, text="UPS Connection (RS232C)BA100R", padding=10); conn.pack(fill=tk.X, pady=(0, 15))
        self.ups_port_combo = ttk.Combobox(conn, width=20); self.ups_port_combo.pack(side=tk.LEFT, padx=5)

        # [바인딩 수정] call_mgr 사용 및 .pack() 유지
        self.ups_search_btn = ttk.Button(conn, text="Search Ports 🔍", command=lambda: self.controller.call_mgr("ups", "search_ups_ports"))
        self.ups_search_btn.pack(side=tk.LEFT, padx=5)
        self.ups_conn_btn = ttk.Button(conn, text="Connect UPS", command=lambda: self.controller.call_mgr("ups", "toggle_ups_connection"), state="disabled")
        self.ups_conn_btn.pack(side=tk.LEFT, padx=5)
        self.ups_refresh_btn = ttk.Button(conn, text="Refresh Status 🔄", command=lambda: self.controller.call_mgr("ups", "manual_refresh_ups"), state="disabled")
        self.ups_refresh_btn.pack(side=tk.LEFT, padx=5)
        self.ups_diag_btn = ttk.Button(conn, text="Diagnosis 🛠️", command=lambda: self.controller.call_mgr("ups", "diagnose_ups"))
        self.ups_diag_btn.pack(side=tk.LEFT, padx=5)

        mid = ttk.Frame(container); mid.pack(fill=tk.X, pady=5)
        info = ttk.LabelFrame(mid, text=" Electrical Info ", padding=10); info.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        for l, k in [("Input Volt", "input_volt"), ("Output Volt", "output_volt"), ("Freq", "frequency"), ("Status", "status_msg")]:
            row = ttk.Frame(info); row.pack(fill=tk.X, pady=2)
            ttk.Label(row, text=f"{l}:", width=16, font=("Helvetica", 14)).pack(side=tk.LEFT)
            val = ttk.Label(row, textvariable=self.ups_vars[k], font=("Helvetica", 30, "bold"), foreground="blue")
            val.pack(side=tk.LEFT); self.ups_value_labels.append(val)

        ctrl_bar = ttk.Frame(container); ctrl_bar.pack(fill=tk.X, pady=(10, 0), side=tk.BOTTOM)
        
        # [바인딩 수정] call_mgr 사용 및 .pack() 유지
        self.btn_ups_shutdown = tk.Button(ctrl_bar, text="⚠️ EXECUTE SYSTEM WIDE SHUTDOWN", bg="#dc3545", fg="white", font=("Helvetica", 12, "bold"), height=2, command=lambda: self.controller.call_mgr("ups", "shutdown_ups_all"))
        self.btn_ups_shutdown.pack(fill=tk.X, padx=100)

        graph = ttk.LabelFrame(container, text=" UPS Real-time Trend ", padding=5); graph.pack(fill=tk.BOTH, expand=True)
        self.fig_ups, _ = plt.subplots(2, 2, figsize=(10, 8), dpi=100)
        self.canvas_ups = FigureCanvasTkAgg(self.fig_ups, master=graph)
        self.canvas_ups.get_tk_widget().pack(fill=tk.BOTH, expand=True)

    def _create_status_dashboard(self, parent):
        dashboard = ttk.LabelFrame(parent, text=" System Overview ", padding=10); dashboard.pack(fill=tk.X, pady=(0, 10), padx=5)
        if self.unlock_btn: self.unlock_btn.pack(side=tk.RIGHT, padx=10)        
        inner = ttk.Frame(dashboard); inner.pack(expand=True)
        self.status_widgets = {}
        for dev in ["DAQ", "HV", "Env", "Laser", "B-field", "UPS"]:
            f = ttk.Frame(inner); f.pack(side=tk.LEFT, padx=15)
            c = tk.Canvas(f, width=20, height=20, highlightthickness=0); c.pack(side=tk.LEFT)
            led = c.create_oval(2, 2, 18, 18, fill="#dc3545")
            ttk.Label(f, text=dev, font=("Helvetica", 10, "bold")).pack(side=tk.LEFT)
            self.status_widgets[dev] = {"led": led, "canvas": c}
        self.master.after(100, self._update_dashboard_loop)

    def _update_dashboard_loop(self):
        statuses = self.controller.get_system_status()
        statuses["B-field"] = getattr(self, "web_connection_status", False)
        for key, connected in statuses.items():
            if key in self.status_widgets:
                self.status_widgets[key]["canvas"].itemconfig(self.status_widgets[key]["led"], fill="#28a745" if connected else "#dc3545")
        self.master.after(2000, self._update_dashboard_loop)

    def _create_contact_tab(self, parent):
        container = ttk.Frame(parent, padding=20); container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(container, text="🚨 Emergency Contacts", font=("Helvetica", 16, "bold"), foreground="#dc3545").pack(pady=(0, 20))
        tree = ttk.Treeview(container, columns=("role", "name", "phone", "note"), show="headings", height=15)
        for c in ["role", "name", "phone", "note"]: tree.heading(c, text=c.capitalize())
        for c in self.controller.load_contacts(): tree.insert("", tk.END, values=(c["role"], c["name"], c["phone"], c["note"]))
        tree.pack(fill=tk.BOTH, expand=True)

    def refresh_ui_state(self):
        is_unlocked = getattr(self.controller.access_mgr, 'unlocked', True)
        state = tk.NORMAL if is_unlocked else tk.DISABLED
        bg_locked, fg_locked = "#3a3a3a", "#777777"
        if self.unlock_btn:
            self.unlock_btn.config(text="🔓 Active" if is_unlocked else "🔒 Unlock", bg="#28a745" if is_unlocked else "#f0ad4e")
        if hasattr(self, 'laser_tabs_data'):
            for wl, vars_dict in self.laser_tabs_data.items():
                for k in ["ld_on_btn", "ld_off_btn", "tec_on_btn", "tec_off_btn", "curr_apply_btn_obj"]:
                    if k in vars_dict:
                        btn = vars_dict[k]; btn.config(state=state)
                        if not is_unlocked: btn.config(bg=bg_locked, fg=fg_locked)
        if hasattr(self.controller, 'auto_ui'): self.controller.auto_ui.set_buttons_state(is_unlocked)

    def setup_shortcuts(self):
        for key, cmd in [("<Control-o>", "open_config"), ("<Control-p>", "run_produce"), ("<Control-a>", "run_analysis"), ("<Control-s>", "run_waveform")]:
            self.master.bind(key, lambda e, c=cmd: self.controller.handle_button_click(c))
        self.master.bind("<F5>", lambda e: self.controller.refresh_all_data())

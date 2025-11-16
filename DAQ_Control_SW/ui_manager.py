# ui_manager.py
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, font
import os
import json
import math 
from image_viewer import ImageViewer
from config_window import ConfigWindow 
from datetime import datetime

class UIManager:
    def __init__(self, master, controller):
        self.master = master
        self.controller = controller

        self.default_font = font.nametofont("TkDefaultFont")
        self.default_font.configure(size=11) 
        self.master.option_add("*Font", self.default_font)


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

    def show_about(self):
        messagebox.showinfo("About DAQ Control",
                            """DAQ Control Application 
                      Made by Korean group (CNU, Junkyo OH)
                      For more information or to contribute, please visit the GitHub repository:
                      https://github.com/ohjunkyo/HK_PRECALIB_KOR_SYSTEM""")

    def create_widgets(self):
        paned_window = ttk.PanedWindow(self.master, orient=tk.HORIZONTAL)
        paned_window.pack(fill=tk.BOTH, expand=True)

        left_pane = ttk.Frame(paned_window, width=450, padding="10")
        left_pane.pack_propagate(False)
        paned_window.add(left_pane, weight=1)

        self._create_connection_status_frame(left_pane)
        self._create_run_control_frame(left_pane)
        self._create_dynamic_buttons_frame(left_pane, "Execute Scripts", "scripts")
        self._create_dynamic_buttons_frame(left_pane, "View", "view")
        self._create_path_viewer_frame(left_pane)

        right_pane = ttk.Frame(paned_window, padding=(0, 10, 10, 10))
        paned_window.add(right_pane, weight=3)

        self.notebook = ttk.Notebook(right_pane) 
        self.notebook.pack(fill=tk.BOTH, expand=True)

        # Tab 1: Configuration
        config_tab = ttk.Frame(self.notebook, padding=(10, 10, 0, 0))
        self.notebook.add(config_tab, text="Configuration")
        self._create_status_frame(config_tab)
        self._create_config_viewer(config_tab)

        # Tab 2: Data Files
        data_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(data_tab, text="Data Files")
        self._create_data_viewer(data_tab)

        # Tab 3: Log
        log_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(log_tab, text="Log")
        self._create_log_viewer(log_tab)

    def on_config_loaded(self):
        self._update_pmt_status_and_helper() 
        self.update_config_display()
        self.update_path_display()
        if hasattr(self, 'data_tree'):
            self.update_data_viewer(force_refresh=True)


    def _create_connection_status_frame(self, parent):
        frame = ttk.LabelFrame(parent, text="Connection Status", padding="10")
        frame.pack(fill=tk.X, pady=(0, 3), padx=5)

        self.connection_status_label = ttk.Label(frame, text="Checking...", font=("Helvetica", 10, "bold"))
        self.connection_status_label.pack(pady=(0, 3))

        ip_frame = ttk.Frame(frame)
        ip_frame.pack(fill=tk.X, padx=3)
        ip_frame.columnconfigure(1, weight=1)

        ttk.Label(ip_frame, text="Local IP:").grid(row=0, column=0, sticky="w", padx=(0, 3))
        self.local_ip_value = ttk.Label(ip_frame, text="Fetching...", anchor="w")
        self.local_ip_value.grid(row=0, column=1, sticky="ew")

    def update_ip_display(self, ip_info):
        self.local_ip_value.config(text=ip_info.get('local_ip', 'N/A'))

    def update_daq_connection_status(self, is_connected):
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
        self.pmt_status_frame = ttk.LabelFrame(parent, text="PMT Status & Rotation Helper", padding="10")
        self.pmt_status_frame.pack(fill=tk.BOTH, expand=True, pady=5, padx=5) 

    def _update_pmt_status_and_helper(self):
        if not self.controller.config_manager: return

        for widget in self.pmt_status_frame.winfo_children():
            widget.destroy()
        self.status_indicators.clear()

        cfg = self.controller.config_manager.get_all_variables()

        X_MAP = [0, 45, 90, 135, 180, -45, -90, -135]
        Y_MAP = [90, 135, 180, -45, -90, -135, 0, 45]
        # PMT 고유 좌표 (A=180도, G=90도)
        POS_MAP_ORIGINAL = { 'E': 0, 'F': 45, 'G': 90, 'H': 135, 'A': 180, 'B': 225, 'C': 270, 'D': 315 }

        for i in range(1, 4):
            sn_val = cfg.get(f'SN{i}')
            dir_val = cfg.get(f'direction{i}')
            is_active = sn_val and sn_val.strip()

            pmt_row_frame = ttk.Frame(self.pmt_status_frame)
            pmt_row_frame.pack(fill=tk.X, pady=5)

            self._create_status_indicator(pmt_row_frame, f"SN{i}", is_active, side=tk.LEFT)
            self._create_helper_diagram(pmt_row_frame, dir_val, POS_MAP_ORIGINAL) 
            self._create_helper_text(pmt_row_frame, i, sn_val, dir_val, X_MAP, Y_MAP)

            ttk.Separator(self.pmt_status_frame, orient='horizontal').pack(fill='x', pady=5)


    def _create_status_indicator(self, parent, name, is_active, side=tk.TOP):
        color = 'gold' if is_active else '#adb5bd'
        canvas_frame = ttk.Frame(parent)
        canvas_frame.pack(side=side, padx=10, pady=5)
        #canvas = tk.Canvas(canvas_frame, width=80, height=80, bg="white", highlightthickness=1, cursor="hand2")
        canvas = tk.Canvas(canvas_frame, width=80, height=80, bg="white", highlightthickness=1, cursor="hand2")
        canvas.pack()
        canvas.create_rectangle(2, 2, 80, 80, outline='black', width=1)
        oval_id = canvas.create_oval(10, 10, 72, 72, fill=color, outline='')
        canvas.create_text(41, 41, text=name, font=("Helvetica", 13, "bold"))
        canvas.bind("<Button-1>", lambda event, pmt_name=name: self.controller.open_pmt_config_window(pmt_name))
        self.status_indicators[name] = {"canvas": canvas, "oval_id": oval_id}

    # --- [*** 여기가 완전히 수정된 부분 ***] ---
    def _create_helper_diagram(self, parent, direction, pos_map_original):
        # 캔버스 크기를 늘려 +X, +Y 라벨 공간 확보

        #canvas = tk.Canvas(canvas_frame, width=80, height=80, bg="white", highlightthickness=1, cursor="hand2")
        canvas = tk.Canvas(parent, width=120, height=120) 
        canvas.pack(side=tk.LEFT, padx=10)

        C_X, C_Y, R = 60, 60, 40 # 중심과 반지름
        LABEL_R = R + 12 # +X, +Y 라벨을 그릴 반지름

        # --- 1. 회전 각도 계산 ---
        # 'direction' 라벨이 9시(180도)에 오도록 전체를 회전시킴
        TARGET_CABLE_ANGLE = 180 # 9시 방향 (케이블 고정 위치)
        rotation_offset = 0
        current_dir_char = 'A' # 기본값

        if direction and direction.upper() in pos_map_original:
            current_dir_char = direction.upper()
            # PMT 고유 좌표계에서 현재 direction의 각도 (예: B=225도)
            original_angle_of_current_dir = pos_map_original[current_dir_char]
            # (목표 각도 - 현재 각도) 만큼 회전
            rotation_offset = TARGET_CABLE_ANGLE - original_angle_of_current_dir
            # 예: direction='B' (225도) -> offset = 180 - 225 = -45도
            # 예: direction='G' (90도)  -> offset = 180 - 90  = 90도

        # --- 2. 고정된 "Scan Axis" 그리기 (12시-6시) ---
        # 파워포인트의 파란색 영역처럼 표시
        canvas.create_rectangle(C_X - 6, C_Y - R - 15, C_X + 6, C_Y + R + 15, 
                                fill="#e7f5ff", outline="")
        canvas.create_line(C_X, C_Y - R + 5, C_X, C_Y - R - 12, arrow=tk.LAST, fill="#1971c2", width=2)
        canvas.create_line(C_X, C_Y + R - 5, C_X, C_Y + R + 12, arrow=tk.LAST, fill="#1971c2", width=2)
        canvas.create_text(C_X, C_Y - R - 18, text="Scan Axis", font=("Helvetica", 9, "bold"), fill="#1971c2")

        # --- 3. 고정된 "Cable" 표시 그리기 (9시) ---
        canvas.create_line(C_X - R + 5, C_Y, C_X - R - 12, C_Y, arrow=tk.LAST, fill='red', width=3)
        canvas.create_text(C_X - R - 15, C_Y, text="Cable", font=("Helvetica", 9, "bold"), fill="red", anchor="e")

        # --- 4. 회전하는 PMT 원과 라벨 ---
        canvas.create_oval(C_X - R, C_Y - R, C_X + R, C_Y + R, outline='gray')

        # --- 5. 회전하는 DY1 / DY2 ---
        DY_R = 10 # Dynode 라벨 반지름
        DY_FONT = ("Helvetica", 9, "bold")
        # PMT 고유 좌표 (DY1=9시, DY2=3시)
        DY1_ORIGINAL_ANGLE_DEG = 180 
        DY2_ORIGINAL_ANGLE_DEG = 0   

        # 회전 적용
        new_dy1_angle_rad = math.radians((DY1_ORIGINAL_ANGLE_DEG + rotation_offset) % 360)
        dy1_x = C_X + DY_R * math.cos(new_dy1_angle_rad)
        dy1_y = C_Y - DY_R * math.sin(new_dy1_angle_rad) # Y축 반전

        new_dy2_angle_rad = math.radians((DY2_ORIGINAL_ANGLE_DEG + rotation_offset) % 360)
        dy2_x = C_X + DY_R * math.cos(new_dy2_angle_rad)
        dy2_y = C_Y - DY_R * math.sin(new_dy2_angle_rad)

        canvas.create_text(dy1_x, dy1_y, text="DY1", font=DY_FONT, fill='black')
        canvas.create_text(dy2_x, dy2_y, text="DY2", font=DY_FONT, fill='black')

        # --- 6. 회전하는 A-H 라벨 및 +X/+Y ---
        label_font = ("Helvetica", 10, "bold")
        axis_label_font = ("Helvetica", 10, "bold")

        for char, original_angle_deg in pos_map_original.items():

            # 라벨의 새 각도 계산
            new_angle_deg = (original_angle_deg + rotation_offset) % 360
            new_angle_rad = math.radians(new_angle_deg)

            # A-H 라벨 위치 (원 안쪽)
            x = C_X + (R - 8) * math.cos(new_angle_rad)
            y = C_Y - (R - 8) * math.sin(new_angle_rad) # Y축 반전

            # +X/+Y 라벨 위치 (원 바깥쪽)
            x_ax = C_X + (LABEL_R) * math.cos(new_angle_rad)
            y_ax = C_Y - (LABEL_R) * math.sin(new_angle_rad)

            # 'direction'과 일치하는 라벨은 빨간색으로 표시
            color = 'red' if char == current_dir_char else 'black'
            canvas.create_text(x, y, text=char, font=label_font, fill=color)

            # PMT의 고유 X축(G), Y축(A) 라벨 추가
            if char == 'A':
                canvas.create_text(x_ax, y_ax, text="+Y", font=axis_label_font, fill="#c92a2a")
            elif char == 'G':
                canvas.create_text(x_ax, y_ax, text="+X", font=axis_label_font, fill="#1971c2")
            # --- [*** 수정 끝 ***] ---
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
        # --- [*** 수정 끝 ***] ---

    def _create_run_control_frame(self, parent):
        frame = ttk.LabelFrame(parent, text="Run Control & Parameters", padding="10")
        frame.pack(fill=tk.X, pady=5, padx=5)
        ttk.Label(frame, text="Mode:").pack(anchor=tk.W)
        laser_radio = ttk.Radiobutton(frame, text="Laser (0)", variable=self.run_mode, value="laser", command=self.controller.update_latest_run_number)
        dark_radio = ttk.Radiobutton(frame, text="Dark (1)", variable=self.run_mode, value="dark", command=self.controller.update_latest_run_number)
        laser_radio.pack(anchor=tk.W)
        dark_radio.pack(anchor=tk.W)
        ttk.Label(frame, text="Run number (produce & analyis):").pack(anchor=tk.W, pady=(10, 0))
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
        if not self.controller.config_manager: return
        self.config_text.tag_configure("comment", foreground="#228B22", font=("Helvetica", 12, "bold"), spacing1=8, spacing3=2)
        self.config_text.tag_configure("key", foreground="#333333", font=("Helvetica", 11, "bold"))
        #self.config_text.tag_configure("key", foreground="#D4D4D4", font=("Helvetica", 11, "bold"))
        self.config_text.tag_configure("value", foreground="#c92a2a", font=("Helvetica", 11))
        self.config_text.tag_configure("error", foreground="#FF0000")
        self.config_text.config(state="normal")
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
        self.config_text.config(state="disabled")

    def _create_path_viewer_frame(self, parent):
        frame = ttk.LabelFrame(parent, text="File & Directory Paths", padding="10")
        frame.pack(fill=tk.X, pady=5, padx=5)

        self.path_container = ttk.Frame(frame)
        self.path_container.pack(fill=tk.X, pady=(0, 5))

        self.path_labels = {}
        path_keys = ['BasePath', 'RawDataPath'] #DaqProgramPath

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

        bottom_frame = ttk.Frame(frame)
        bottom_frame.pack(fill=tk.X, pady=(5,0))

        self.data_size_var = tk.StringVar(value="Calculating...")
        ttk.Label(bottom_frame, text="Data Capacity:").pack(side=tk.LEFT)
        self.data_size_label = ttk.Label(bottom_frame, textvariable=self.data_size_var, foreground="blue", font=("Helvetica", 10, "bold"))
        self.data_size_label.pack(side=tk.LEFT, padx=5)


        def configure_wraplength(event):
            width = event.width - 150 
            for label in self.path_labels.values():
                label.config(wraplength=width)

        self.path_container.bind("<Configure>", configure_wraplength)

    def update_data_size_display(self, size_str):
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

        for tab_name in ["Raw", "Production"]:
            tab_frame = ttk.Frame(self.data_notebook)
            self.data_notebook.add(tab_frame, text=f"{tab_name} Data")
            self._create_file_browser_tab(tab_frame, tab_name)

        delete_button = ttk.Button(left_data_frame, text="Delete Selected File(s) 🗑️", command=self.on_delete_selected_files)
        delete_button.pack(fill=tk.X, padx=5, pady=(5,0))

        right_info_frame = ttk.LabelFrame(data_paned_window, text="File Info", padding=10)
        data_paned_window.add(right_info_frame, weight=1)

        self.file_info_label = ttk.Label(right_info_frame, text="Select a file to see details.", justify=tk.LEFT, wraplength=250)
        self.file_info_label.pack(anchor=tk.NW)

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
        """Raw 또는 Production 탭 내부의 UI 요소를 생성하는 헬퍼 함수"""
        control_frame = ttk.Frame(parent_tab, padding=5)
        control_frame.pack(fill=tk.X)

        filter_frame = ttk.LabelFrame(control_frame, text="Filter Mode", padding=5)
        filter_frame.pack(side=tk.LEFT, padx=(0, 10))
        filter_mode = tk.StringVar(value="All")
        ttk.Radiobutton(filter_frame, text="All", variable=filter_mode, value="All", command=self.update_data_viewer).pack(side=tk.LEFT)
        ttk.Radiobutton(filter_frame, text="Dark", variable=filter_mode, value="Dark", command=self.update_data_viewer).pack(side=tk.LEFT)
        ttk.Radiobutton(filter_frame, text="Laser", variable=filter_mode, value="Laser", command=self.update_data_viewer).pack(side=tk.LEFT)

        sort_frame = ttk.LabelFrame(control_frame, text="Sort By", padding=5)
        sort_frame.pack(side=tk.LEFT)
        sort_mode = tk.StringVar(value="time") 
        ttk.Button(sort_frame, text="Name (A-Z)", command=lambda: self._set_sort_and_update(tab_type, 'name')).pack(side=tk.LEFT)
        ttk.Button(sort_frame, text="Time (Newest)", command=lambda: self._set_sort_and_update(tab_type, 'time')).pack(side=tk.LEFT)

        refresh_btn = ttk.Button(control_frame, text="Refresh 🔄", command=self.controller.refresh_all_data)
        refresh_btn.pack(side=tk.RIGHT, padx=5)

        tree_frame = ttk.Frame(parent_tab)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        columns = ("filename", "path", "modified_time")
        tree = ttk.Treeview(tree_frame, columns=columns, show="headings")
        tree.heading("filename", text="File Name")
        tree.heading("path", text="Directory Path")
        tree.heading("modified_time", text="Last Modified")
        tree.column("filename", width=300)
        tree.column("path", width=400)
        tree.column("modified_time", width=150)

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
                "sort_mode": sort_mode
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
            tab_type = "Raw" if "Raw" in tab_text else "Production"

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
            tab_type = "Raw" if "Raw" in tab_text else "Production"

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

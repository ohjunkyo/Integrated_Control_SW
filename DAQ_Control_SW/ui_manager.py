# ui_manager.py
import tkinter as tk


class _Tooltip:
    """Simple hover tooltip for any widget."""
    def __init__(self, widget, text):
        self._widget = widget
        self._text = text
        self._tip = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, _event=None):
        x = self._widget.winfo_rootx() + 20
        y = self._widget.winfo_rooty() + self._widget.winfo_height() + 4
        self._tip = tk.Toplevel(self._widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        lbl = tk.Label(self._tip, text=self._text, justify=tk.LEFT,
                       background="#ffffe0", relief=tk.SOLID, borderwidth=1,
                       font=("Helvetica", 9), wraplength=260)
        lbl.pack()

    def _hide(self, _event=None):
        if self._tip:
            self._tip.destroy()
            self._tip = None


from tkinter import ttk, scrolledtext, messagebox, font
import os
import json
import math
import re
import subprocess
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
        self.file_format = tk.StringVar(value="root")

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
        self._create_lock_banner(self.daq_main_frame)

        paned_window = ttk.PanedWindow(self.daq_main_frame, orient=tk.HORIZONTAL)
        paned_window.pack(fill=tk.BOTH, expand=True)
        
        left_scroll_container = ttk.Frame(paned_window)
        paned_window.add(left_scroll_container, weight=0)

        left_canvas = tk.Canvas(left_scroll_container, width=450, highlightthickness=0)
        left_vbar = ttk.Scrollbar(left_scroll_container, orient="vertical", command=left_canvas.yview)
        
        left_pane = ttk.Frame(left_canvas, padding="10")

        # Keep the embedded window id so we can stretch it to the canvas width.
        left_window_id = left_canvas.create_window((0, 0), window=left_pane, anchor="nw", width=450)
        left_canvas.configure(yscrollcommand=left_vbar.set)

        # scrollregion follows the inner content height
        left_pane.bind("<Configure>", lambda e: left_canvas.configure(scrollregion=left_canvas.bbox("all")))
        # ...and the inner frame width follows the canvas width, so dragging the PanedWindow
        # sash actually reflows the controls instead of leaving blank space or clipping them.
        left_canvas.bind("<Configure>", lambda e: left_canvas.itemconfig(left_window_id, width=e.width))

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
        self._create_pmt_position_widget(left_pane)
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
        self.auto_ui = AutomationUI(self.notebook, self.controller)

        # Tab 1: PMT Rotation Helper (이제 스크롤 없이 바로 보임)
        config_tab = ttk.Frame(self.notebook, padding=(10, 10, 10, 10))
        self.notebook.add(config_tab, text="PMT Setup & Helper")
        self.pmt_setup_tab = config_tab   # ref for the position widget's "Open Setup" jump
        self._create_status_frame(config_tab)

        # Tab 2: Console — DAQ/Produce/Analysis 작업 출력을 별도 터미널 대신
        # UI 안에서 실시간으로 보여준다(터미널 잔여물 제거).
        # 가장 자주 보게 되는 탭이므로 Helper 바로 옆(눈에 띄는 위치)에 둔다.
        self.console_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.console_tab, text="📟 Output")
        self._create_console_viewer(self.console_tab)

        # Tab 3: Waveform Inspection (embedded, replaces external ROOT terminal)
        self.waveform_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.waveform_tab, text="🔬 Waveform")
        self._create_waveform_viewer(self.waveform_tab)

        # Tab 4: Data Files (Treeview 자체 스크롤바 사용)
        data_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(data_tab, text="Data Files")
        self._create_data_viewer(data_tab)

        # Tab 5: Log (ScrolledText 자체 스크롤바 사용)
        log_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(log_tab, text="Log")
        self._create_log_viewer(log_tab)

        # Tab 6: DAQ Diagnostics
        diag_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(diag_tab, text="🔧 DAQ Diag")
        self._create_daq_diagnostics_tab(diag_tab)

        #  2: Laser Control
        self._create_laser_control_tab(self.laser_main_frame)

        # main tab 3
        self._create_web_monitor_tab(self.main_notebook)
        
        # 4: UPS Status
        self.ups_main_frame = ttk.Frame(self.main_notebook)
        self.main_notebook.add(self.ups_main_frame, text=" UPS Status ")
        self._create_ups_monitoring_tab(self.ups_main_frame)

        # 5: Integrated Log Center (read-only viewer over all scattered logs)
        try:
            self.log_center_frame = ttk.Frame(self.main_notebook)
            self.main_notebook.add(self.log_center_frame, text=" 📑 Logs ")
            self._create_log_center_tab(self.log_center_frame)
        except Exception as e:
            print(f"[WARNING] Log Center tab init failed (non-fatal): {e}")

        # 6: Quick Start guide (English)
        try:
            self.quick_start_frame = ttk.Frame(self.main_notebook)
            self.main_notebook.add(self.quick_start_frame, text=" 📖 Quick Start ")
            self._create_quick_start_tab(self.quick_start_frame)
        except Exception as e:
            print(f"[WARNING] Quick Start tab init failed (non-fatal): {e}")

        # 7: Emergency Contact
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

        # Entry/Combobox/Spinbox: the 'clam' theme keeps fieldbackground white, so in dark
        # mode the (now white) text became invisible on the still-white field. Style the
        # field background + text colour explicitly.
        for w in ("TEntry", "TCombobox", "TSpinbox"):
            style.configure(w, fieldbackground=c["text_bg"], foreground=c["text_fg"],
                            background=c["frame_bg"], insertcolor=c["fg"])
            style.map(w,
                      fieldbackground=[("readonly", c["text_bg"]), ("disabled", c["bg"])],
                      foreground=[("readonly", c["text_fg"]), ("disabled", "#777777")])
        # Combobox drop-down list popup (a separate Tk Listbox)
        self.master.option_add("*TCombobox*Listbox.background", c["text_bg"])
        self.master.option_add("*TCombobox*Listbox.foreground", c["text_fg"])
        style.configure("TCheckbutton", background=c["bg"], foreground=c["fg"])
        style.configure("TRadiobutton", background=c["bg"], foreground=c["fg"])

        self.master.config(bg=c["bg"])

        # tk.Text / ScrolledText widgets are NOT ttk, so they must be recoloured by hand;
        # otherwise their white background (or white text) stays mismatched and the content
        # becomes unreadable in dark mode.
        for w in (getattr(self, 'log_text', None), getattr(self, 'config_text', None),
                  getattr(self, 'laser_log_text', None)):
            if w is not None:
                try:
                    w.config(bg=c["text_bg"], fg=c["text_fg"], insertbackground=c["fg"])
                except Exception:
                    pass
        # Per-wavelength laser session log windows
        if hasattr(self, 'laser_tabs_data'):
            for vd in self.laser_tabs_data.values():
                wdg = vd.get("log_text_obj") if isinstance(vd, dict) else None
                if wdg is not None:
                    try:
                        wdg.config(bg=c["text_bg"], fg=c["text_fg"], insertbackground=c["fg"])
                    except Exception:
                        pass
        # Integrated Log Center text windows
        for wdg in getattr(self, 'log_center_texts', []):
            try:
                wdg.config(bg=c["text_bg"], fg=c["text_fg"], insertbackground=c["fg"])
            except Exception:
                pass

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
        self._retheme_pmt_position_widget()

        self.controller.update_plots_theme(self.is_dark_mode)

    # ==================================================================
    #  Integrated Log Center  (read-only viewer over the scattered logs)
    # ==================================================================
    def _theme_text_colors(self):
        c = self.colors["dark" if self.is_dark_mode else "light"]
        return c["text_bg"], c["text_fg"], c["fg"]

    def _create_log_center_tab(self, parent):
        """A single place to browse Laser / DAQ / HV / UPS / App logs.

        It only READS from the existing log locations (no files are moved), so the live
        writers keep working untouched. The 'Gather' button can copy everything into one
        folder for archiving.
        """
        import os
        self.log_center_texts = []

        # Resolve log roots defensively (fall back to known defaults).
        base = self.controller.base_dir
        parent_dir = os.path.dirname(base)
        laser_dir = getattr(self.controller, "laser_log_dir", "/home/precalkor/ADC/ADC_test/LOG/LASER")
        adc_log_root = os.path.dirname(laser_dir.rstrip("/")) or "/home/precalkor/ADC/ADC_test/LOG"

        self._log_sources = {
            "DAQ":   (os.path.join(adc_log_root, "DAQ"), "TakingLog_*.txt"),
            "Laser": (laser_dir,                         "laser_data_*.csv"),
            "UPS":   (os.path.join(base, "LOG", "UPS"),  "ups_*.csv"),
            "App":   (os.path.join(base, "logs"),        "log_*.txt"),
        }
        self._hv_db_path = os.path.join(parent_dir, "HV_Control_SW", "monitoring_log.db")
        self.log_center_widgets = {}

        top = ttk.Frame(parent)
        top.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(top, text="📑 Integrated Log Center", font=("Helvetica", 12, "bold")).pack(side=tk.LEFT, padx=5)
        ttk.Button(top, text="📦 Gather all logs → one folder", command=self._gather_all_logs).pack(side=tk.RIGHT, padx=5)
        ttk.Button(top, text="🔄 Refresh all", command=self._refresh_all_log_subtabs).pack(side=tk.RIGHT, padx=5)

        nb = ttk.Notebook(parent)
        nb.pack(fill=tk.BOTH, expand=True)
        for name in ("DAQ", "Laser", "UPS", "App"):
            self._build_file_log_subtab(nb, name)
        self._build_hv_log_subtab(nb)

    def _build_file_log_subtab(self, nb, name):
        bg, fg, ins = self._theme_text_colors()
        frame = ttk.Frame(nb, padding=6)
        nb.add(frame, text=f" {name} ")

        ctrl = ttk.Frame(frame)
        ctrl.pack(fill=tk.X)
        ttk.Label(ctrl, text="File:").pack(side=tk.LEFT)
        combo = ttk.Combobox(ctrl, state="readonly", width=42)
        combo.pack(side=tk.LEFT, padx=5)
        ttk.Button(ctrl, text="🔄", width=3, command=lambda n=name: self._refresh_log_subtab(n)).pack(side=tk.LEFT)
        combo.bind("<<ComboboxSelected>>", lambda e, n=name: self._load_log_file(n))

        txt = scrolledtext.ScrolledText(frame, wrap=tk.NONE, state="disabled",
                                        bg=bg, fg=fg, insertbackground=ins, font=("Menlo", 9))
        txt.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        self.log_center_texts.append(txt)
        self.log_center_widgets[name] = {"combo": combo, "text": txt}
        self._refresh_log_subtab(name)

    def _refresh_log_subtab(self, name):
        import os, glob
        w = self.log_center_widgets.get(name)
        if not w:
            return
        dir_path, pattern = self._log_sources[name]
        files = sorted(glob.glob(os.path.join(dir_path, pattern)), key=os.path.getmtime, reverse=True)
        names = [os.path.basename(f) for f in files]
        w["combo"]["values"] = names
        if names:
            w["combo"].current(0)
            self._load_log_file(name)
        else:
            self._set_text(w["text"], f"(No log files found in {dir_path})")

    def _refresh_all_log_subtabs(self):
        for name in self.log_center_widgets:
            self._refresh_log_subtab(name)
        if hasattr(self, "_hv_log_widgets"):
            self._load_hv_log()

    def _load_log_file(self, name):
        import os
        w = self.log_center_widgets.get(name)
        if not w:
            return
        sel = w["combo"].get()
        if not sel:
            return
        dir_path, _ = self._log_sources[name]
        path = os.path.join(dir_path, sel)
        try:
            # Daily laser CSVs can be huge; only show the tail to keep the UI responsive.
            MAX_BYTES = 2 * 1024 * 1024
            size = os.path.getsize(path)
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                if size > MAX_BYTES:
                    f.seek(size - MAX_BYTES)
                    tail = f.read()
                    content = (f"[... file is {size/1024/1024:.1f} MB — showing the last "
                               f"{MAX_BYTES//1024//1024} MB only ...]\n" + tail[tail.find("\n") + 1:])
                else:
                    content = f.read()
        except Exception as e:
            content = f"[ERROR] Could not read {path}\n{e}"
        self._set_text(w["text"], content)

    def _build_hv_log_subtab(self, nb):
        bg, fg, ins = self._theme_text_colors()
        frame = ttk.Frame(nb, padding=6)
        nb.add(frame, text=" HV ")

        ctrl = ttk.Frame(frame)
        ctrl.pack(fill=tk.X)
        ttk.Label(ctrl, text="Channel:").pack(side=tk.LEFT)
        ch = ttk.Combobox(ctrl, state="readonly", width=8, values=["Ch0", "Ch1", "Ch2", "Ch3"])
        ch.current(0)
        ch.pack(side=tk.LEFT, padx=5)
        ttk.Label(ctrl, text="Last N:").pack(side=tk.LEFT)
        rows = ttk.Combobox(ctrl, state="readonly", width=8, values=["100", "500", "2000"])
        rows.current(0)
        rows.pack(side=tk.LEFT, padx=5)
        ttk.Button(ctrl, text="🔄 Load", command=self._load_hv_log).pack(side=tk.LEFT, padx=5)
        ch.bind("<<ComboboxSelected>>", lambda e: self._load_hv_log())
        rows.bind("<<ComboboxSelected>>", lambda e: self._load_hv_log())

        txt = scrolledtext.ScrolledText(frame, wrap=tk.NONE, state="disabled",
                                        bg=bg, fg=fg, insertbackground=ins, font=("Menlo", 9))
        txt.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        self.log_center_texts.append(txt)
        self._hv_log_widgets = {"ch": ch, "rows": rows, "text": txt}
        self._load_hv_log()

    def _load_hv_log(self):
        import os, sqlite3
        w = getattr(self, "_hv_log_widgets", None)
        if not w:
            return
        if not os.path.exists(self._hv_db_path):
            self._set_text(w["text"], f"(HV DB not found: {self._hv_db_path})")
            return
        chan = w["ch"].get()           # "Ch0".."Ch3"
        try:
            n = int(w["rows"].get())
        except Exception:
            n = 100
        cols = ["timestamp", f"{chan}_V", f"{chan}_I_L", f"{chan}_I_H",
                "Dark_Box_1_T", "Dark_Box_1_H"]
        try:
            con = sqlite3.connect(f"file:{self._hv_db_path}?mode=ro", uri=True)
            cur = con.cursor()
            q = f"SELECT {','.join(cols)} FROM monitoring_data ORDER BY rowid DESC LIMIT ?"
            recs = cur.execute(q, (n,)).fetchall()
            con.close()
        except Exception as e:
            self._set_text(w["text"], f"[ERROR] HV DB query failed:\n{e}")
            return
        header = f"{'timestamp':<28}{chan+'_V':>12}{chan+'_I_L':>12}{chan+'_I_H':>12}{'Box1_T':>10}{'Box1_H':>10}"
        lines = [header, "-" * len(header)]
        for r in recs:
            ts = str(r[0])
            def f(v):
                return f"{v:.2f}" if isinstance(v, (int, float)) else str(v)
            lines.append(f"{ts:<28}{f(r[1]):>12}{f(r[2]):>12}{f(r[3]):>12}{f(r[4]):>10}{f(r[5]):>10}")
        self._set_text(w["text"], "\n".join(lines))

    def _set_text(self, widget, content):
        try:
            widget.config(state="normal")
            widget.delete("1.0", tk.END)
            widget.insert(tk.END, content)
            widget.config(state="disabled")
        except Exception:
            pass

    def _gather_all_logs(self):
        """Copy (not move) every log file into one archive folder for easy management."""
        import os, glob, shutil
        dest_root = os.path.join(os.path.dirname(self._log_sources["Laser"][0].rstrip("/")), "_UNIFIED")
        copied = 0
        try:
            for name, (dir_path, pattern) in self._log_sources.items():
                out_dir = os.path.join(dest_root, name)
                os.makedirs(out_dir, exist_ok=True)
                for src in glob.glob(os.path.join(dir_path, pattern)):
                    try:
                        shutil.copy2(src, os.path.join(out_dir, os.path.basename(src)))
                        copied += 1
                    except Exception:
                        pass
            # HV DB
            if os.path.exists(self._hv_db_path):
                os.makedirs(os.path.join(dest_root, "HV"), exist_ok=True)
                shutil.copy2(self._hv_db_path, os.path.join(dest_root, "HV", os.path.basename(self._hv_db_path)))
                copied += 1
            messagebox.showinfo("Logs Gathered",
                                f"Copied {copied} log file(s) into:\n{dest_root}\n\n(Originals were left untouched.)")
        except Exception as e:
            messagebox.showerror("Gather Failed", f"Could not gather logs:\n{e}")

    def _create_quick_start_tab(self, parent):
        """A read-only, English quick-start guide for operators."""
        bg, fg, ins = self._theme_text_colors()
        txt = scrolledtext.ScrolledText(parent, wrap=tk.WORD, state="disabled",
                                        bg=bg, fg=fg, insertbackground=ins, font=("Menlo", 10))
        txt.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        if not hasattr(self, "log_center_texts"):
            self.log_center_texts = []
        self.log_center_texts.append(txt)

        guide = (
"============================================================\n"
" PMT PRE-CALIBRATION  —  QUICK START GUIDE\n"
"============================================================\n"
"GOAL\n"
"  Pre-calibrate PMTs mounted on a motorised rotation stage.\n"
"  A pulsed laser illuminates each PMT at many tilt/rotation\n"
"  angles; DAQ records the response for uniformity / QE analysis.\n"
"\n"
"  Data flow:  DAQ (raw)  ─▶  Produce (prd)  ─▶  Analysis (result)\n"
"                          ─▶  Uniformity / Overlay (PNG summary)\n"
"\n"
"  Top status bar: 🟢 = connected / healthy   🔴 = disconnected\n"
"  Output tab (📟 Output): streams all job stdout in real time.\n"
"\n"
"------------------------------------------------------------\n"
" BEFORE YOU START\n"
"------------------------------------------------------------\n"
"  1. General Scan > 'Quick Setup' tab:\n"
"       Set shifter name, expert, note, laser wavelength,\n"
"       PMT serials (SN1/2/3), cable direction, rotation offset,\n"
"       and HV for each PMT. Click 💾 Save Settings.\n"
"  2. Laser Control tab  (e.g. 405 nm sub-tab):\n"
"       Connect → TEC ON → set Pulse current → Apply Currents\n"
"       Trigger must be set to External.\n"
"       ⚠  Only ONE wavelength LD may be ON at a time.\n"
"  3. Verify the top status dots: DAQ System 🟢, HV System 🟢,\n"
"       Laser Controller 🟢, OMRON UPS 🟢.\n"
"  4. Click the green '🔓 CONTROLS ACTIVE' banner to unlock\n"
"       motor control (password required once per session).\n"
"\n"
"------------------------------------------------------------\n"
" 1) GENERAL SCAN   (automated, full tilt sweep)  [recommended]\n"
"------------------------------------------------------------\n"
"  Tab: DAQ System → General Scan → Control Panel (Master)\n"
"\n"
"  1. (Optional) Tick '🧪 TEST RUN' for a dry run without real DAQ.\n"
"  2. Click '▶ Start run'.\n"
"     The system loops automatically for each tilt angle:\n"
"       Move TILT → wait → Run DAQ → Produce → Analysis → Contour\n"
"       → next angle … until the full sweep is done.\n"
"  3. Monitor progress in the Scan Matrix (below the buttons)\n"
"     and in 📟 Output → DAQ Stream.\n"
"  4. Run numbers auto-increment and are locked to the scan-start\n"
"     date (no midnight reset).\n"
"\n"
"  Pause / Resume:\n"
"    ⏸ Pause  — waits for the current step, then holds.\n"
"    ⏯ Continue — resumes from that checkpoint.\n"
"\n"
"  Stop completely:\n"
"    ⚠ Re-Run / Abort Scan  (Danger Zone)\n"
"      [Yes]    → abort + delete checkpoint → next Start is FRESH\n"
"      [No]     → abort + keep checkpoint  → next Start can RESUME\n"
"      [Cancel] → do nothing\n"
"\n"
"  When finished: results are in Data/FinalResult/.\n"
"    '7. Uniformity' → enter date tag + run range → build PNG summary\n"
"    '8. Overlay'    → compare multiple datasets\n"
"    Image Viewer (Ctrl+I) → browse generated plots\n"
"\n"
"------------------------------------------------------------\n"
" 2) MANUAL SCAN   (single runs, no rotation automation)\n"
"------------------------------------------------------------\n"
"  Tab: DAQ System (left 'Execute Scripts' panel)\n"
"\n"
"  1. Unlock Controls (lock banner at top) if moving motors.\n"
"  2. Select mode: Laser (external trigger) or Dark (self trigger).\n"
"  3. Run number is auto-suggested (next free #); edit if needed.\n"
"  4. '2. Run DAQ (Only Click)' → records one raw .root file.\n"
"  5. '3. Produce (Ctrl+P)'     → creates the prd file.\n"
"  6. '4. Analysis (Ctrl+A)'    → creates the result file.\n"
"       ⚠  Analysis needs the prd file — always Produce first.\n"
"  7. '5. Waveform Inspection'  → opens an interactive waveform\n"
"       viewer in a new terminal. A settings dialog lets you set:\n"
"         • Y-axis ±mV around pedestal  (default: 5 mV)\n"
"         • pC threshold for 's' jump   (default: −0.5 pC)\n"
"         • X-axis sample range          (default: full)\n"
"       In the terminal: n=next, s=jump to threshold, #=go to entry\n"
"  8. '6. Waveform (2D Contour)' → builds a 2D time-vs-voltage\n"
"       contour plot. Settings dialog:\n"
"         • Y-axis ±mV around pedestal  (default: 3 mV)\n"
"         • X-axis sample range          (default: full)\n"
"       Output saved to Data/image/Contour/.\n"
"  Tip: select a file in 'Data Files' tab to target that exact\n"
"       run; if nothing is selected, the Run-number box is used.\n"
"       Multiple files can be selected for batch Produce/Analysis.\n"
"\n"
"------------------------------------------------------------\n"
" 3) IF SOMETHING GOES WRONG\n"
"------------------------------------------------------------\n"
"  DAQ hangs / Output tab stuck:\n"
"     Scan watchdog auto-kills a frozen DAQ.\n"
"     Manual kill:  pkill -9 execute_DAQ_v2\n"
"     Then click ⚠ Re-Run / Abort Scan to reset the UI.\n"
"\n"
"  Analysis gives empty or tiny result file:\n"
"     You need the prd file → run Produce first.\n"
"     Auto-pipeline passes the prd path automatically.\n"
"\n"
"  Laser INTERLOCK / comms error:\n"
"     Check the interlock magnets are attached to the enclosure.\n"
"     The LD is forced OFF on error; reconnect from the\n"
"     wavelength tab (the app also auto-reconnects in background).\n"
"\n"
"  Motor 'already moving' / unresponsive:\n"
"     Click Abort. Lock auto-releases on failed connection.\n"
"     Rotation is blocked when TILT ≠ 0 (safety interlock).\n"
"\n"
"  Recovery / Resume after abort:\n"
"     If checkpoint was kept: click Start run → 'Resume' dialog.\n"
"     If checkpoint was deleted (or you chose fresh start):\n"
"     click Start run → scan begins from -55°.\n"
"\n"
"  Emergency:\n"
"     'Emergency' tab → contacts and HV shutdown.\n"
"     Turn LD OFF and stop motors before cutting power.\n"
"\n"
"------------------------------------------------------------\n"
" KEY SHORTCUTS  (when DAQ System tab is focused)\n"
"------------------------------------------------------------\n"
"  Ctrl+P  Produce       Ctrl+A  Analysis\n"
"  Ctrl+S  Waveform      Ctrl+I  Image Viewer\n"
"\n"
"------------------------------------------------------------\n"
" NOTES\n"
"------------------------------------------------------------\n"
"  * Recommended laser: 405 nm.  Also tested: 375 / 450 / 473 nm.\n"
"  * Only ONE LD ON at a time (enforced by the app).\n"
"  * 📟 Output tab turns 🟢 while a job is running; sub-tabs\n"
"    show ✅ Done or ❌ Failed after completion.\n"
"  * 'Logs' tab shows Laser / DAQ / HV / UPS logs in one place.\n"
"  * PMT Setup & Helper tab shows real-time TOP VIEW and\n"
"    RIGHT SIDE VIEW diagrams of each PMT's tilt/rotation angle.\n"
"============================================================\n"
        )
        txt.config(state="normal")
        txt.insert(tk.END, guide)
        txt.config(state="disabled")

    def _create_connection_status_frame(self, parent):
        pass  # DAQ status is shown in the top status bar; no separate panel needed

    def update_ip_display(self, ip_info):
        pass

    def update_daq_connection_status(self, is_connected):
        self.daq_connected_flag = is_connected

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

        # 실시간 재드로우를 위한 helper diagram 레지스트리.
        # pmt_index(1~3) -> {canvas, cable_type, pos_map, dev_num, info_lbl, sn, hv}
        # SN2=dev2, SN3=dev3 은 모터가 있어 라이브 각도로 갱신되고, SN1(모니터)은 정적.
        self.helper_diagrams = {}

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
            try:
                tilt_val = float(cfg.get(f'TiltAngle{i}', "0"))
            except (TypeError, ValueError):
                tilt_val = 0.0
            is_active = sn != "N/A" and sn.strip() != ""

            # SN 노란 버튼 대신, 공간을 거의 안 먹는 '클릭 가능한 제목줄'로 대체한다.
            # (클릭 시 PMT 설정창 열기 기능은 그대로 유지)
            txt_color = "white" if self.is_dark_mode else "black"
            sn_color = "#d39e00" if is_active else "#adb5bd"
            header = ttk.Frame(cell)
            header.pack(fill=tk.X, pady=(0, 2))
            sn_title = tk.Label(header, text=f"SN{i}", font=("Helvetica", 14, "bold"),
                                fg=sn_color, cursor="hand2",
                                bg=("#2d2d2d" if self.is_dark_mode else "#f0f0f0"))
            sn_title.pack(side=tk.LEFT)
            hint = tk.Label(header, text="  ⚙ click to edit", font=("Helvetica", 9),
                            fg="#888", cursor="hand2",
                            bg=("#2d2d2d" if self.is_dark_mode else "#f0f0f0"))
            hint.pack(side=tk.LEFT)
            for w in (sn_title, hint):
                w.bind("<Button-1>",
                       lambda e, n=f"SN{i}": self.controller.open_pmt_config_window(n))

            # 정보 텍스트는 헤더 오른쪽에 둬서 가로/세로 공간을 아낀다.
            info_text = (f"{sn} - {cable_type}   |   HV: {hv} V   |   "
                         f"Rotation: {rot_val}° / Tilt: {tilt_val:.0f}°")
            info_lbl = ttk.Label(header, text=info_text, font=("Helvetica", 10, "bold"),
                                 foreground=txt_color)
            info_lbl.pack(side=tk.RIGHT)

            # 회전/틸트/케이블 타입을 전달하고, 재드로우용으로 pmt_index 도 넘긴다.
            # TOP VIEW + RIGHT SIDE VIEW 두 캔버스를 만들어 body 프레임에 담는다.
            self._create_helper_diagram(cell, i, rot_val, tilt_val, cable_type, POS_MAP_ANGLES)

            # 라이브 업데이트가 'Rotation/Tilt' 값을 갱신할 수 있도록 메타데이터 저장.
            if i in self.helper_diagrams:
                self.helper_diagrams[i]["info_lbl"] = info_lbl
                self.helper_diagrams[i]["sn"] = sn
                self.helper_diagrams[i]["cable_type"] = cable_type
                self.helper_diagrams[i]["hv"] = hv

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

    def _create_helper_diagram(self, parent, pmt_index, rotation_angle, tilt_angle,
                               cable_type, pos_map_angles):
        """PMT 설치 가이드 다이어그램(TOP VIEW)을 만든다.

        - rotation_angle : Scan Axis 둘레의 회전각(0° = 케이블 9시 방향). 핀맵 전체가 회전.
        - tilt_angle     : Scan Axis(수직 고정축) 기준 기울기. TOP VIEW 에서는 디스크가
                           가로로 단축(foreshortening = cos(tilt))되어 타원으로 보인다.
        실시간 갱신을 위해 캔버스/메타데이터를 self.helper_diagrams[pmt_index] 에 저장하고,
        실제 그리기는 _render_helper_diagram() 이 담당한다(라이브 각도로 재호출 가능).
        """
        bg_color = "#2d2d2d" if self.is_dark_mode else "white"

        # 두 캔버스(TOP VIEW + RIGHT SIDE VIEW)를 가로로 담을 body 프레임.
        body = ttk.Frame(parent)
        body.pack(fill=tk.BOTH, expand=True)

        # TOP VIEW (확대: 잘림 방지) — 클릭 시 PMT 설정창 열기
        canvas = tk.Canvas(body, width=300, height=250, bg=bg_color, highlightthickness=0,
                           cursor="hand2")
        canvas.pack(side=tk.LEFT, padx=(4, 2))

        # RIGHT SIDE VIEW (20인치 PMT 기울기 + 레이저 포인터)
        side_canvas = tk.Canvas(body, width=170, height=250, bg=bg_color, highlightthickness=0,
                                cursor="hand2")
        side_canvas.pack(side=tk.LEFT, padx=(2, 4))

        for c in (canvas, side_canvas):
            c.bind("<Button-1>",
                   lambda e, n=f"SN{pmt_index}": self.controller.open_pmt_config_window(n))

        dev_num = pmt_index if pmt_index in (2, 3) else None  # SN2->dev2, SN3->dev3, SN1=모니터
        self.helper_diagrams[pmt_index] = {
            "canvas": canvas,
            "side_canvas": side_canvas,
            "body": body,
            "cable_type": cable_type,
            "pos_map": pos_map_angles,
            "dev_num": dev_num,
            "rotation": rotation_angle,
            "tilt": tilt_angle,
        }
        self._render_helper_diagram(pmt_index, rotation_angle, tilt_angle)

    def _render_helper_diagram(self, pmt_index, rotation_angle, tilt_angle):
        """helper diagram 을 주어진 회전/틸트 각도로 (재)그린다. 캔버스를 비우고 다시 그림."""
        entry = self.helper_diagrams.get(pmt_index)
        if not entry:
            return
        canvas = entry["canvas"]
        try:
            if not canvas.winfo_exists():
                return
        except tk.TclError:
            return

        cable_type = entry["cable_type"]
        pos_map_angles = entry["pos_map"]
        entry["rotation"] = rotation_angle
        entry["tilt"] = tilt_angle

        canvas.delete("all")

        txt_fill = 'white' if self.is_dark_mode else 'black'
        C_X, C_Y, R = 150, 125, 82

        fx = math.cos(math.radians(tilt_angle))
        if abs(fx) < 0.12:
            fx = 0.12 if fx >= 0 else -0.12

        def get_pos(angle_deg, radius):
            rad = math.radians(angle_deg)
            x = C_X + radius * math.cos(rad)
            y = C_Y - radius * math.sin(rad)
            return C_X + (x - C_X) * fx, y

        # ── Pin map offset ────────────────────────────────────────────────
        physical_cable_angle = 180 + rotation_angle
        std_type_angle = pos_map_angles.get(cable_type, 180)
        pin_offset = physical_cable_angle - std_type_angle

        # ── 1. Scan Axis band (blue, fixed) ───────────────────────────────
        scan_axis_bg = "#3d3d3d" if self.is_dark_mode else "#ddeeff"
        canvas.create_rectangle(C_X - 8, C_Y - R - 20, C_X + 8, C_Y + R + 20,
                                fill=scan_axis_bg, outline="")

        # ── 2. +Y axis band (A-pin direction, green) ─────────────────────
        a_std = pos_map_angles.get('A', 180)
        a_ang = a_std + pin_offset
        ax1, ay1 = get_pos(a_ang,       R)
        ax2, ay2 = get_pos(a_ang + 180, R)
        canvas.create_line(ax1, ay1, ax2, ay2, fill="#b2f2bb", width=10, capstyle="round")

        # ── 3. +X axis band (G-pin direction, orange) ────────────────────
        g_std = pos_map_angles.get('G', 90)
        g_ang = g_std + pin_offset
        gx1, gy1 = get_pos(g_ang,       R)
        gx2, gy2 = get_pos(g_ang + 180, R)
        canvas.create_line(gx1, gy1, gx2, gy2, fill="#ffd8a8", width=10, capstyle="round")

        # ── 4. PMT disc ───────────────────────────────────────────────────
        canvas.create_oval(C_X - R * abs(fx), C_Y - R, C_X + R * abs(fx), C_Y + R,
                           outline='gray', width=2)

        # ── 5. Scan Axis arrows & label ───────────────────────────────────
        canvas.create_line(C_X, C_Y - R + 5, C_X, C_Y - R - 15, arrow=tk.LAST, fill="#1971c2", width=3)
        canvas.create_line(C_X, C_Y + R - 5, C_X, C_Y + R + 15, arrow=tk.LAST, fill="#1971c2", width=3)
        sa_label = "Scan Axis"
        sa_color = "#1971c2"
        # Green check when tilt ≈ 0 (laser on scan axis)
        if abs(tilt_angle) < 1.5:
            sa_label = "Scan Axis  ✓"
            sa_color = "#2f9e44"
        canvas.create_text(C_X, C_Y - R - 25, text=sa_label,
                           font=("Helvetica", 10, "bold"), fill=sa_color)

        # ── 6. Live angle text ────────────────────────────────────────────
        canvas.create_text(6, 8, anchor="nw",
                           text=f"Rot {rotation_angle:.0f}°  Tilt {tilt_angle:.0f}°",
                           font=("Helvetica", 9, "bold"), fill=txt_fill)

        # ── 7. Cable arrow ────────────────────────────────────────────────
        cx1, cy1 = get_pos(physical_cable_angle, R - 5)
        cx2, cy2 = get_pos(physical_cable_angle, R + 30)
        canvas.create_line(cx1, cy1, cx2, cy2, arrow=tk.LAST, fill='red', width=3)
        ctx, cty = get_pos(physical_cable_angle, R + 42)
        norm_angle = physical_cable_angle % 360
        if 45 < norm_angle < 135: anchor = "s"
        elif 135 <= norm_angle < 225: anchor = "e"
        elif 225 <= norm_angle < 315: anchor = "n"
        else: anchor = "w"
        # Keep the "Cable" label inside the 300px-wide canvas. With anchor "e"
        # the text extends left of ctx (clips at x<0 for left-pointing cables);
        # with "w" it extends right. Clamp the anchor point accordingly.
        CW, lbl_w, pad = 300, 42, 3
        if anchor == "e":   ctx = max(ctx, pad + lbl_w)
        elif anchor == "w": ctx = min(ctx, CW - pad - lbl_w)
        else:               ctx = max(pad + lbl_w / 2, min(CW - pad - lbl_w / 2, ctx))
        canvas.create_text(ctx, cty, text="Cable", font=("Helvetica", 10, "bold"),
                           fill="red", anchor=anchor)

        # ── 8. Pin labels ─────────────────────────────────────────────────
        label_font     = ("Helvetica", 12, "bold")
        axis_lbl_font  = ("Helvetica", 11, "bold")

        for char, std_angle in pos_map_angles.items():
            final_pin_angle = std_angle + pin_offset
            lx, ly = get_pos(final_pin_angle, R - 15)
            color = 'red' if char == cable_type else txt_fill
            canvas.create_text(lx, ly, text=char, font=label_font, fill=color)

            if char == 'A':
                ay_lx, ay_ly = get_pos(final_pin_angle, R + 14)
                canvas.create_text(ay_lx, ay_ly, text="+Y",
                                   font=axis_lbl_font, fill="#2f9e44")
            elif char == 'G':
                ax_lx, ax_ly = get_pos(final_pin_angle, R + 14)
                canvas.create_text(ax_lx, ax_ly, text="+X",
                                   font=axis_lbl_font, fill="#e67700")

        # ── 9. DY1 / DY2 ──────────────────────────────────────────────────
        dy_r = 15
        dy1_x, dy1_y = get_pos(90  + pin_offset, dy_r)
        dy2_x, dy2_y = get_pos(270 + pin_offset, dy_r)
        canvas.create_oval(C_X - 2, C_Y - 2, C_X + 2, C_Y + 2, fill="gray", outline="")
        canvas.create_text(dy1_x, dy1_y, text="DY1", font=("Helvetica", 9, "bold"), fill=txt_fill)
        canvas.create_text(dy2_x, dy2_y, text="DY2", font=("Helvetica", 9, "bold"), fill=txt_fill)

        canvas.create_text(C_X, 244, text="TOP VIEW", font=("Helvetica", 9, "bold"), fill="#888")

        # RIGHT SIDE VIEW
        self._render_side_view(pmt_index, tilt_angle, pin_offset, pos_map_angles)

    def _render_side_view(self, pmt_index, tilt_angle, pin_offset=0, pos_map_angles=None):
        """RIGHT SIDE VIEW: 20인치 PMT 를 옆에서 본 그림. tilt_angle 만큼 PMT 전체가
        기울고, 센터 위에 세운 막대기(rod) 끝을 레이저 포인터(빨간 빔)가 가리킨다.
        고정 수직 점선(기준축) 대비 막대기가 기울어 tilt 를 직관적으로 보여준다."""
        entry = self.helper_diagrams.get(pmt_index)
        if not entry:
            return
        canvas = entry.get("side_canvas")
        if canvas is None:
            return
        try:
            if not canvas.winfo_exists():
                return
        except tk.TclError:
            return

        canvas.delete("all")
        txt_fill = 'white' if self.is_dark_mode else 'black'

        W, H = 170, 250
        cx, cy = 85, 150          # 반원(돔) 지름의 중심
        Rb = 56                   # 20인치 PMT 반지름

        canvas.create_text(W / 2, 244, text="RIGHT SIDE VIEW",
                           font=("Helvetica", 9, "bold"), fill="#888")

        # 설정 tilt(kr) → PMT 표면 위치각(Hamamatsu) 변환.
        # 출처: Draw_Uniformity_Norm_v7.C 의 ConvertKRtoHamamatsu().
        #   ham = -0.0049*kr^2 + 1.7515*kr - 0.0402   (deg)
        # 반원(PMT)을 이 각도만큼 '기울이고', 레이저는 수직 고정으로 둔다.
        # 예) tilt 55° → 약 81.5° → 반원이 크게 기울어 레이저가 거의 가장자리에 닿는다.
        kr = abs(tilt_angle)
        pos_deg = -0.0049 * kr * kr + 1.7515 * kr - 0.0402
        pos_deg = max(0.0, min(90.0, pos_deg))      # 반원(0~90°) 범위로 클램프
        sign = -1.0 if tilt_angle < 0 else 1.0
        # Screen lean is MIRRORED (viewed from the E side): tilt(+) must land the
        # laser spot on the RIGHT-labeled pin side (e.g. cable A X-scan -> X+ = G),
        # so the dome apex leans LEFT for tilt(+). Verified against angle_convert.h.
        th = math.radians(-sign * pos_deg)          # 반원이 기우는 각도(화면 기준)

        # 돔 로컬좌표(lx: 지름방향, ly: 높이 위쪽 +) → 화면. th 만큼 회전.
        upx, upy = math.sin(th), -math.cos(th)      # 꼭대기 방향
        rxh, ryh = math.cos(th), math.sin(th)       # 지름 방향

        def to_screen(lx, ly):
            return (cx + lx * rxh + ly * upx, cy + lx * ryh + ly * upy)

        canvas.create_text(6, 8, anchor="nw",
                           text=f"Tilt {tilt_angle:.0f}°  →  Pos {sign*pos_deg:.0f}°",
                           font=("Helvetica", 9, "bold"), fill=txt_fill)

        # 고정 수직 기준선(점선) — 반원이 기운 정도 비교용.
        canvas.create_line(cx, cy, cx, cy - Rb - 35, fill="#bbb", dash=(3, 3))

        # 기울어진 반원(PMT).
        bulb_fill = "#274b6d" if self.is_dark_mode else "#d0e7fb"
        pts = []
        spot = None
        for a in range(0, 181, 5):
            rad = math.radians(a)
            sx, sy = to_screen(Rb * math.cos(rad), Rb * math.sin(rad))
            pts.extend([sx, sy])
            # 수직 고정 레이저(x=cx)가 닿는 표면 스폿: x 가 cx 에 가장 가까운 윗점.
            if spot is None or (abs(sx - cx) < abs(spot[0] - cx) - 0.001):
                spot = (sx, sy)
        canvas.create_polygon(*pts, fill=bulb_fill, outline="#5a7fa5", width=2)

        # ── Cable direction labels at the two ends of the visible arc ──────
        # "RIGHT SIDE VIEW" = viewer stands on the East (E) side of the top
        # view, looking West along the A-E axis. From there the top-view
        # North (G, 90°) appears on the viewer's RIGHT and South (C, 270°)
        # on the LEFT. A/E point toward/away from the viewer (hidden).
        # LEFT end of diameter (a=180)  → physical top-view angle 270° (C)
        # RIGHT end of diameter (a=0)   → physical top-view angle 90°  (G)
        if pos_map_angles:
            def _nearest_pin(target_physical):
                target_std = target_physical - pin_offset
                best, best_d = '?', 999
                for ch, sa in pos_map_angles.items():
                    d = abs(((sa - target_std) + 180) % 360 - 180)
                    if d < best_d:
                        best_d, best = d, ch
                return best
            pin_left  = _nearest_pin(270)  # left end  = South (C) in top-view
            pin_right = _nearest_pin(90)   # right end = North (G) in top-view
            lx_l, ly_l = to_screen(-Rb, 0)   # a=180 → left end
            lx_r, ly_r = to_screen( Rb, 0)   # a=0   → right end
            canvas.create_text(lx_l - 10, ly_l, text=pin_left,
                               font=("Helvetica", 10, "bold"), fill="#e67700", anchor="e")
            canvas.create_text(lx_r + 10, ly_r, text=pin_right,
                               font=("Helvetica", 10, "bold"), fill="#e67700", anchor="w")

        # 수직 고정 레이저 빔: 위에서 똑바로 내려와 기울어진 반원 표면을 때린다.
        spot_x, spot_y = spot
        canvas.create_line(cx, cy - Rb - 42, cx, spot_y, fill="red", width=3, arrow=tk.LAST)
        canvas.create_text(cx, cy - Rb - 50, text="Laser",
                           font=("Helvetica", 9, "bold"), fill="red")
        canvas.create_oval(spot_x - 5, spot_y - 5, spot_x + 5, spot_y + 5,
                           fill="#ff5555", outline="red")

        # 중심점
        canvas.create_oval(cx - 2, cy - 2, cx + 2, cy + 2, fill=txt_fill, outline="")

    def update_helper_live(self, dev_num, tilt, rot):
        """모터 모니터링 스레드(start_monitoring 콜백)에서 호출. dev_num(2/3)의 라이브
        각도로 해당 PMT helper diagram 을 실시간 재드로우한다. SN1(모니터)은 모터가
        없어 대상에서 제외된다. 백그라운드 스레드이므로 master.after 로 메인에 넘긴다."""
        if tilt is None or rot is None:
            return
        if not hasattr(self, 'helper_diagrams'):
            return
        pmt_index = dev_num  # SN2=dev2, SN3=dev3
        entry = self.helper_diagrams.get(pmt_index)
        if not entry or entry.get("dev_num") != dev_num:
            return

        def _apply():
            self._render_helper_diagram(pmt_index, rot, tilt)
            lbl = entry.get("info_lbl")
            if lbl is not None:
                try:
                    sn = entry.get("sn", "")
                    ct = entry.get("cable_type", "")
                    hv = entry.get("hv", "0")
                    lbl.config(text=(f"{sn} - {ct}   |   HV: {hv} V   |   "
                                     f"Rotation: {rot:.0f}° / Tilt: {tilt:.0f}°"))
                except tk.TclError:
                    pass

        try:
            self.master.after(0, _apply)
        except tk.TclError:
            pass


    # ══════════════════════════════════════════════════════════════════════
    # Always-visible PMT position widget (left Control Panel)
    # ══════════════════════════════════════════════════════════════════════

    def _create_pmt_position_widget(self, parent):
        """Always-visible read-only PMT position panel (horizontal layout).

        SN1: monitor-only label. SN2/SN3: mini TOP compass + SIDE tilt view + angles + state.
        No action buttons — display only. Updated via update_pmt_position_widget().
        """
        self.pmt_pos_widgets = {}

        frame = ttk.LabelFrame(parent, text=" PMT position (live) ", padding=(6, 4))
        frame.pack(fill=tk.X, pady=(0, 8), padx=2)

        # Horizontal row: SN1 | SN2 | SN3
        row = ttk.Frame(frame)
        row.pack(fill=tk.X)

        # SN1 — monitor only (no motor)
        sn1_col = ttk.Frame(row)
        sn1_col.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 6))
        ttk.Label(sn1_col, text="SN1", font=("Helvetica", 9, "bold")).pack(anchor="center")
        ttk.Label(sn1_col, text="Monitor\n(No Motor)",
                  font=("Helvetica", 8), foreground="#888",
                  justify=tk.CENTER).pack(anchor="center")

        cfg = {}
        try:
            if self.controller.config_manager:
                cfg = self.controller.config_manager.get_all_variables()
        except Exception:
            cfg = {}

        for dev in (2, 3):
            col = ttk.Frame(row)
            col.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 4 if dev == 2 else 0))

            bg = "#2d2d2d" if self.is_dark_mode else "white"

            ttk.Label(col, text=f"SN{dev}", font=("Helvetica", 9, "bold")).pack(anchor="center")

            canvases = ttk.Frame(col)
            canvases.pack(anchor="center")
            top_c = tk.Canvas(canvases, width=48, height=48, bg=bg, highlightthickness=0)
            top_c.pack(side=tk.LEFT, padx=(0, 2))
            side_c = tk.Canvas(canvases, width=56, height=48, bg=bg, highlightthickness=0)
            side_c.pack(side=tk.LEFT)

            ang_lbl = ttk.Label(col, text="R—° T—°", font=("Helvetica", 8))
            ang_lbl.pack(anchor="center")
            state_lbl = tk.Label(col, text="—", font=("Helvetica", 7, "bold"),
                                 fg="#888", bg=bg)
            state_lbl.pack(anchor="center", pady=(1, 0))

            try:
                rot0 = float(cfg.get(f'RotateAngle{dev}', "0") or 0)
                tilt0 = float(cfg.get(f'TiltAngle{dev}', "0") or 0)
            except (TypeError, ValueError):
                rot0, tilt0 = 0.0, 0.0

            self.pmt_pos_widgets[dev] = {
                "top": top_c, "side": side_c,
                "ang": ang_lbl, "state": state_lbl,
                "last_rot": rot0, "last_tilt": tilt0,
            }

            self._draw_pos_compass(top_c, rot0, "#1D9E75")
            self._draw_pos_sideview(side_c, tilt0)
            ang_lbl.config(text=f"R{rot0:.0f}° T{tilt0:.0f}°")

    def _retheme_pmt_position_widget(self):
        """Recolor the position-widget canvases + state labels for the current theme,
        redrawing from the last known angles (the live thread may not fire if motors
        are idle/offline, so we can't rely on it to refresh the background)."""
        for w in getattr(self, 'pmt_pos_widgets', {}).values():
            try:
                bg = "#2d2d2d" if self.is_dark_mode else "white"
                w["state"].config(bg=bg)
                self._draw_pos_compass(w["top"], w.get("last_rot", 0.0), "#1D9E75")
                self._draw_pos_sideview(w["side"], w.get("last_tilt", 0.0))
            except (tk.TclError, KeyError):
                pass

    def _draw_pos_compass(self, canvas, rot_deg, needle_color):
        """Mini TOP view: cable-direction needle around the scan axis (0° = 9 o'clock)."""
        try:
            if not canvas.winfo_exists():
                return
        except tk.TclError:
            return
        canvas.config(bg="#2d2d2d" if self.is_dark_mode else "white")  # keep bg theme-synced
        canvas.delete("all")
        cx, cy, R = 26, 26, 21
        canvas.create_oval(cx - R, cy - R, cx + R, cy + R,
                           outline="#888", width=1)
        # Match the helper diagram convention: physical cable at 180° + rotation.
        ang = math.radians(180 + rot_deg)
        ex, ey = cx + R * math.cos(ang), cy - R * math.sin(ang)
        canvas.create_line(cx, cy, ex, ey, fill=needle_color, width=3,
                           capstyle=tk.ROUND)
        canvas.create_oval(cx - 2, cy - 2, cx + 2, cy + 2,
                           fill=needle_color, outline="")

    def _draw_pos_sideview(self, canvas, tilt_deg):
        """Mini SIDE view: a 20-inch PMT dome tilted by tilt_deg vs a fixed vertical
        reference (the laser axis). Same KR→position-angle conversion as the full
        helper side view, just smaller and label-free."""
        try:
            if not canvas.winfo_exists():
                return
        except tk.TclError:
            return
        canvas.config(bg="#2d2d2d" if self.is_dark_mode else "white")  # keep bg theme-synced
        canvas.delete("all")
        W, H = 60, 52
        cx, cy, Rb = 30, 36, 22

        kr = abs(tilt_deg)
        pos_deg = -0.0049 * kr * kr + 1.7515 * kr - 0.0402
        pos_deg = max(0.0, min(90.0, pos_deg))
        sign = -1.0 if tilt_deg < 0 else 1.0
        th = math.radians(-sign * pos_deg)   # mirrored, same convention as full side view

        upx, upy = math.sin(th), -math.cos(th)
        rxh, ryh = math.cos(th), math.sin(th)

        def to_screen(lx, ly):
            return (cx + lx * rxh + ly * upx, cy + lx * ryh + ly * upy)

        # Fixed vertical reference (laser axis).
        canvas.create_line(cx, cy, cx, cy - Rb - 8, fill="#bbb", dash=(2, 2))

        bulb_fill = "#274b6d" if self.is_dark_mode else "#d0e7fb"
        pts = []
        for a in range(0, 181, 10):
            rad = math.radians(a)
            sx, sy = to_screen(Rb * math.cos(rad), Rb * math.sin(rad))
            pts.extend([sx, sy])
        canvas.create_polygon(*pts, fill=bulb_fill, outline="#5a7fa5", width=1)
        # Laser spot where the vertical axis meets the (tilted) dome top.
        canvas.create_oval(cx - 2, cy - Rb + 2, cx + 2, cy - Rb + 6,
                           fill="#e24b4a", outline="")

    def update_pmt_position_widget(self, dev_num, tilt, rot):
        """Motor-monitoring-thread callback: refresh the live position widget for
        SN2/SN3. Marshals to the main thread (Tkinter is not thread-safe)."""
        if dev_num not in (2, 3) or tilt is None or rot is None:
            return
        w = getattr(self, 'pmt_pos_widgets', {}).get(dev_num)
        if not w:
            return

        def _apply():
            try:
                moving = self.controller.rot_mgr.is_moving.get(dev_num, False)
            except Exception:
                moving = False
            locked = abs(tilt) > 0.5      # rotation locked while tilted (hardware rule)

            w["last_rot"], w["last_tilt"] = rot, tilt
            needle = "#BA7517" if moving else "#1D9E75"
            self._draw_pos_compass(w["top"], rot, needle)
            self._draw_pos_sideview(w["side"], tilt)
            try:
                w["ang"].config(text=f"Rot {rot:.0f}° · Tilt {tilt:.0f}°")
                if moving:
                    w["state"].config(text="● moving", fg="#BA7517")
                elif locked:
                    w["state"].config(text="🔒 tilt to 0° to rotate", fg="#dc3545")
                else:
                    w["state"].config(text="✓ rotation ok", fg="#1D9E75")
            except tk.TclError:
                pass

        try:
            self.master.after(0, _apply)
        except tk.TclError:
            pass

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

        label_corr = None 
        if x_tilt_msg or y_tilt_msg:
            correction_msg = f"{x_tilt_msg}\n{y_tilt_msg}"
            label_corr = ttk.Label(text_frame, text=correction_msg, foreground="#c92a2a", font=("Helvetica", 10, "bold"), anchor="w", justify=tk.LEFT)
            label_corr.pack(side=tk.TOP, anchor="w", fill='x', pady=(2,0))

        if not (sn and direction):
            label_main.config(foreground="gray")

        def configure_wraplength(event):
            width = event.width - 10 
            if width > 0:
                label_main.config(wraplength=width)
                if label_corr: 
                    label_corr.config(wraplength=width)
        text_frame.bind("<Configure>", configure_wraplength)

    def _create_run_control_frame(self, parent):
        frame = ttk.LabelFrame(parent, text=" 📊 Run Mode & Parameters ", padding="10")
        frame.pack(fill=tk.X, pady=5, padx=5)

        ttk.Label(frame, text="1. Operation Category:", font=("Helvetica", 10, "bold")).pack(anchor=tk.W)
        
        row_auto = ttk.Frame(frame)
        row_auto.pack(anchor=tk.W, padx=10, pady=2)
        rb_auto = ttk.Radiobutton(row_auto, text=" General Scan (Auto Control)",
                                  variable=self.run_mode, value="auto",
                                  command=self.controller.handle_mode_change)
        rb_auto.pack(side=tk.LEFT)
        lbl_auto_tip = tk.Label(row_auto, text="?", fg="white", bg="#555555",
                                font=("Helvetica", 8, "bold"), cursor="question_arrow",
                                padx=3, pady=0, relief=tk.FLAT)
        lbl_auto_tip.pack(side=tk.LEFT, padx=(4, 0))
        _Tooltip(lbl_auto_tip,
                 "Automated multi-angle scan.\n"
                 "The system rotates PMTs and runs DAQ\n"
                 "sequentially. Run numbers: 000–699 (7 blocks).")

        row_manual = ttk.Frame(frame)
        row_manual.pack(anchor=tk.W, padx=10, pady=2)
        rb_manual = ttk.Radiobutton(row_manual, text=" Manual Mode (Laser/Dark Selection)",
                                    variable=self.run_mode, value="manual",
                                    command=self.controller.handle_mode_change)
        rb_manual.pack(side=tk.LEFT)
        lbl_manual_tip = tk.Label(row_manual, text="?", fg="white", bg="#555555",
                                  font=("Helvetica", 8, "bold"), cursor="question_arrow",
                                  padx=3, pady=0, relief=tk.FLAT)
        lbl_manual_tip.pack(side=tk.LEFT, padx=(4, 0))
        _Tooltip(lbl_manual_tip,
                 "Manual single run: choose Laser or Dark mode below.\n"
                 "Rotation motors are NOT controlled automatically.")

        ttk.Separator(frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=10)

        ttk.Label(frame, text="2. Manual Sub-selection:", font=("Helvetica", 10)).pack(anchor=tk.W)

        self.manual_type_var = tk.StringVar(value="laser")

        row_laser = ttk.Frame(frame)
        row_laser.pack(anchor=tk.W, padx=25)
        self.rb_laser = ttk.Radiobutton(row_laser, text=" Laser & External trigger (0)",
                                        variable=self.manual_type_var, value="laser",
                                        command=self.controller.handle_mode_change)
        self.rb_laser.pack(side=tk.LEFT)
        lbl_laser_tip = tk.Label(row_laser, text="?", fg="white", bg="#555555",
                                 font=("Helvetica", 8, "bold"), cursor="question_arrow",
                                 padx=3, pady=0, relief=tk.FLAT)
        lbl_laser_tip.pack(side=tk.LEFT, padx=(4, 0))
        _Tooltip(lbl_laser_tip,
                 "Laser run with external trigger input.\n"
                 "Run numbers: 800–849.\n"
                 "Used for QE / gain measurements with laser light.")

        row_dark = ttk.Frame(frame)
        row_dark.pack(anchor=tk.W, padx=25)
        self.rb_dark = ttk.Radiobutton(row_dark, text=" Dark & Self trigger (1)",
                                       variable=self.manual_type_var, value="dark",
                                       command=self.controller.handle_mode_change)
        self.rb_dark.pack(side=tk.LEFT)
        lbl_dark_tip = tk.Label(row_dark, text="?", fg="white", bg="#555555",
                                font=("Helvetica", 8, "bold"), cursor="question_arrow",
                                padx=3, pady=0, relief=tk.FLAT)
        lbl_dark_tip.pack(side=tk.LEFT, padx=(4, 0))
        _Tooltip(lbl_dark_tip,
                 "Dark run: PMT self-trigger, no laser.\n"
                 "Run numbers: 700–749.\n"
                 "Rate Scan analysis runs automatically after DAQ finishes.")
        ######## updated 6.10 
        ttk.Label(frame, text="Output Format:").pack(anchor=tk.W, pady=(10, 0))
        fmt_root_radio = ttk.Radiobutton(frame, text="ROOT (.root)", variable=self.file_format, value="root", command=self.controller.update_latest_run_number)
        fmt_csv_radio = ttk.Radiobutton(frame, text="CSV (.csv) (Analysis Not Supported)", variable=self.file_format, value="csv", command=self.controller.update_latest_run_number)
        fmt_root_radio.pack(anchor=tk.W)
        fmt_csv_radio.pack(anchor=tk.W)
        ######## updated 6.10 ^^ 

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
                    if config.get('disabled', False):
                        btn.config(state="disabled")
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
        data_paned_window.add(left_data_frame, weight=2)

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

        self.file_info_label = ttk.Label(right_info_frame, text="Select a file to see details.", justify=tk.LEFT, wraplength=350)
        self.file_info_label.pack(anchor=tk.NW)

        # Set initial sash: file list ~65%, info ~35%
        def _set_sash(pw=data_paned_window):
            try:
                total = pw.winfo_width()
                if total > 100:
                    pw.sashpos(0, int(total * 0.65))
            except Exception:
                pass
        data_paned_window.after(200, _set_sash)

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

    # ------------------------------------------------------------------
    # DAQ Diagnostics tab
    # ------------------------------------------------------------------
    def _create_daq_diagnostics_tab(self, parent):
        """DAQ connection diagnostics + one-click recovery panel."""
        canvas = tk.Canvas(parent, highlightthickness=0)
        vsb = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        inner = ttk.Frame(canvas)
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win_id, width=e.width))

        # ── Section 1: Live Status ─────────────────────────────────────
        s1 = ttk.LabelFrame(inner, text="Live Status", padding=10)
        s1.pack(fill=tk.X, pady=(0, 8))

        self._diag_status_lbl = ttk.Label(s1, text="Not checked yet.", font=("Helvetica", 11, "bold"))
        self._diag_status_lbl.pack(anchor="w")

        self._diag_detail_lbl = ttk.Label(s1, text="", font=("Helvetica", 9), foreground="#888",
                                          justify=tk.LEFT, wraplength=600)
        self._diag_detail_lbl.pack(anchor="w", pady=(2, 0))

        ttk.Button(s1, text="🔍 Run Diagnostics Now", command=self._run_daq_diagnostics).pack(
            anchor="w", pady=(8, 0))

        # ── Section 2: Known Causes ────────────────────────────────────
        s2 = ttk.LabelFrame(inner, text="Known Causes & Explanations", padding=10)
        s2.pack(fill=tk.X, pady=(0, 8))

        causes = [
            ("🔌  USB Driver Not Loaded  (most common)",
             "The CAENUSBdrvB kernel module must be loaded for the CAEN digitizer to be reachable.\n"
             "If it is not registered in /etc/modules-load.d/, it will NOT auto-load after a reboot,\n"
             "and execute_DAQ_v2 -j will return CommError (-1).\n"
             "Fix: click 'Register Auto-Load on Boot' below — after that, reboots will load it automatically."),
            ("⚡  USB Re-enumeration Delay After Power Cycle",
             "When the CAEN digitizer is power-cycled, the OS needs 2–5 seconds to re-enumerate the USB device.\n"
             "Attempting to connect before enumeration finishes will fail. Wait a few seconds, then retry."),
            ("🔒  USB Device File Permission Issue",
             "Access to /dev/bus/usb/... requires the current user to be in the 'dialout' or 'plugdev' group.\n"
             "If neither group is present, the open call will fail with EACCES (Permission denied).\n"
             "Fix: click 'Add USB Permission Groups' below, then log out and back in."),
            ("📚  Shared Library Path Conflict",
             "A stale 32-bit libCAENComm.so in /usr/lib may shadow the correct 64-bit version in\n"
             "/home/precalkor/ADC/lib, causing 'wrong ELF class' at runtime.\n"
             "This program already prepends the correct path via LD_LIBRARY_PATH.\n"
             "If running execute_DAQ_v2 directly in a terminal, set LD_LIBRARY_PATH manually first."),
        ]

        for title, desc in causes:
            f = ttk.Frame(s2)
            f.pack(fill=tk.X, pady=(0, 8))
            ttk.Label(f, text=title, font=("Helvetica", 10, "bold")).pack(anchor="w")
            ttk.Label(f, text=desc, font=("Helvetica", 9), foreground="#666",
                      justify=tk.LEFT, wraplength=600).pack(anchor="w", padx=(12, 0))

        # ── Section 3: Recovery Actions ────────────────────────────────
        s3 = ttk.LabelFrame(inner, text="Recovery Actions  (sudo required)", padding=10)
        s3.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(s3,
                  text="These buttons run sudo commands. A password prompt may appear in the output window below.",
                  font=("Helvetica", 9), foreground="#888").pack(anchor="w", pady=(0, 8))

        btn_frame = ttk.Frame(s3)
        btn_frame.pack(fill=tk.X)

        actions = [
            ("▶  Load Driver Now\n(modprobe)",
             "sudo modprobe CAENUSBdrvB",
             "Loads the USB driver immediately without rebooting.\nTry connecting to the DAQ right after."),
            ("🔁  Reinstall Drivers\n(auto_DAQ_setup.sh)",
             "sudo bash /home/precalkor/ADC/auto_DAQ_setup.sh",
             "Recompiles and reinstalls all CAEN drivers.\nUse this if the driver broke after a kernel update.\n(Takes a while.)"),
            ("📌  Register Auto-Load\non Boot (caen.conf)",
             "echo 'CAENUSBdrvB' | sudo tee /etc/modules-load.d/caen.conf",
             "Registers the driver to load automatically at every boot.\nAfter this, running auto_DAQ_setup.sh on each reboot is no longer needed."),
            ("🔒  Add USB Permission\nGroups (dialout)",
             "sudo usermod -aG dialout $(whoami) && sudo usermod -aG plugdev $(whoami)",
             "Adds the current user to dialout and plugdev groups.\nLog out and back in for the change to take effect."),
        ]

        for i, (label, cmd, tip) in enumerate(actions):
            col = ttk.Frame(btn_frame)
            col.grid(row=0, column=i, padx=(0, 8), sticky="n")
            tk.Button(col, text=label, command=lambda c=cmd: self._diag_run_cmd(c),
                      bg="#374151", fg="white", font=("Helvetica", 9),
                      relief="flat", padx=8, pady=6, justify=tk.CENTER).pack(fill=tk.X)
            ttk.Label(col, text=tip, font=("Helvetica", 8), foreground="#666",
                      justify=tk.LEFT, wraplength=180).pack(anchor="w", pady=(4, 0))

        # ── Section 4: Individual Checks ──────────────────────────────
        s4 = ttk.LabelFrame(inner, text="Driver & Permission Checks", padding=10)
        s4.pack(fill=tk.X, pady=(0, 8))

        diag_btns = ttk.Frame(s4)
        diag_btns.pack(anchor="w", pady=(0, 6))
        ttk.Button(diag_btns, text="Kernel Module State",
                   command=lambda: self._diag_run_cmd(
                       "lsmod | grep -i caen || echo '[NOT LOADED] CAENUSBdrvB module is not loaded'")).pack(
                       side=tk.LEFT, padx=(0, 6))
        ttk.Button(diag_btns, text="USB Device List",
                   command=lambda: self._diag_run_cmd(
                       "lsusb | grep -i caen || echo '[NOT FOUND] No CAEN USB device detected'")).pack(
                       side=tk.LEFT, padx=(0, 6))
        ttk.Button(diag_btns, text="User Groups & Permissions",
                   command=lambda: self._diag_run_cmd(
                       "id && ls -la /dev/bus/usb/ 2>/dev/null | head -5")).pack(
                       side=tk.LEFT, padx=(0, 6))
        ttk.Button(diag_btns, text="Library Path Check",
                   command=lambda: self._diag_run_cmd(
                       "ldconfig -p | grep -i caen; echo '---'; ls /home/precalkor/ADC/lib/*.so* 2>/dev/null")).pack(
                       side=tk.LEFT)

        # ── Section 5: Output window ───────────────────────────────────
        s5 = ttk.LabelFrame(inner, text="Command Output", padding=6)
        s5.pack(fill=tk.BOTH, expand=True, pady=(0, 0))

        out_bar = ttk.Frame(s5)
        out_bar.pack(fill=tk.X)
        ttk.Label(out_bar, text="Output:", font=("Helvetica", 9)).pack(side=tk.LEFT)
        ttk.Button(out_bar, text="Clear", command=lambda: (
            self._diag_out.config(state="normal"),
            self._diag_out.delete("1.0", tk.END),
            self._diag_out.config(state="disabled")
        )).pack(side=tk.RIGHT)

        self._diag_out = scrolledtext.ScrolledText(
            s5, height=10, wrap=tk.WORD, state="disabled",
            bg="#1e1e1e", fg="#d4d4d4", font=("Courier", 9))
        self._diag_out.pack(fill=tk.BOTH, expand=True, pady=(4, 0))

    def _diag_append(self, text):
        """Thread-safe append to diagnostics output window."""
        def _apply():
            self._diag_out.config(state="normal")
            self._diag_out.insert(tk.END, text)
            self._diag_out.see(tk.END)
            self._diag_out.config(state="disabled")
        self.master.after(0, _apply)

    def _diag_run_cmd(self, cmd):
        """Run a shell command and stream output to the diagnostics pane."""
        self._diag_append(f"\n$ {cmd}\n")

        def _run():
            try:
                proc = subprocess.Popen(
                    cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True)
                for line in proc.stdout:
                    self._diag_append(line)
                proc.wait()
                self._diag_append(f"[exit code: {proc.returncode}]\n")
            except Exception as e:
                self._diag_append(f"[ERROR] {e}\n")

        threading.Thread(target=_run, daemon=True).start()

    def _run_daq_diagnostics(self):
        """Run all diagnostic checks and update the Live Status section."""
        self._diag_append("\n=== DAQ Connection Diagnostics ===\n")

        def _run():
            # 1. Kernel module
            r = subprocess.run("lsmod | grep -i caen", shell=True,
                               capture_output=True, text=True)
            if r.stdout.strip():
                self._diag_append(f"[OK] CAENUSBdrvB module is loaded:\n{r.stdout}")
            else:
                self._diag_append("[FAIL] CAENUSBdrvB module is NOT loaded.\n"
                                  "  → Click 'Load Driver Now' to load it without rebooting.\n"
                                  "  → Click 'Register Auto-Load on Boot' to make it permanent.\n")

            # 2. USB device presence
            r2 = subprocess.run("lsusb | grep -i caen", shell=True,
                                capture_output=True, text=True)
            if r2.stdout.strip():
                self._diag_append(f"[OK] CAEN USB device detected:\n{r2.stdout}")
            else:
                self._diag_append("[FAIL] No CAEN USB device found in lsusb.\n"
                                  "  → Check DAQ power and USB cable.\n")

            # 3. Boot auto-load registration
            caen_conf = "/etc/modules-load.d/caen.conf"
            if os.path.exists(caen_conf):
                self._diag_append(f"[OK] Boot auto-load registered: {caen_conf}\n")
            else:
                self._diag_append("[WARN] /etc/modules-load.d/caen.conf not found.\n"
                                  "  → Driver may not load automatically after reboot.\n"
                                  "  → Click 'Register Auto-Load on Boot' to fix this.\n")

            # 4. execute_DAQ_v2 -j connection test
            try:
                daq_path = self.controller.config_manager.get_config_value('BasePath')
                exe = os.path.join(daq_path, 'execute_DAQ_v2') if daq_path else None
                if exe and os.path.exists(exe):
                    env = self.controller._daq_check_env(daq_path)
                    r3 = subprocess.run([exe, '-j'], capture_output=True, text=True,
                                        timeout=6, env=env)
                    if r3.returncode == 0:
                        self._diag_append(f"[OK] execute_DAQ_v2 -j succeeded (digitizer reachable)\n{r3.stdout}")
                        self.master.after(0, lambda: (
                            self._diag_status_lbl.config(text="● DAQ Connected", foreground="#28a745"),
                            self._diag_detail_lbl.config(text="All checks passed.")))
                    else:
                        self._diag_append(f"[FAIL] execute_DAQ_v2 -j failed (exit {r3.returncode})\n"
                                          f"  stdout: {r3.stdout.strip()}\n"
                                          f"  stderr: {r3.stderr.strip()}\n")
                        self.master.after(0, lambda: (
                            self._diag_status_lbl.config(text="✗ DAQ Not Connected", foreground="#dc3545"),
                            self._diag_detail_lbl.config(
                                text="See output above. Load the driver or check USB, then run diagnostics again.")))
                else:
                    self._diag_append("[WARN] execute_DAQ_v2 not found — check BasePath in config.\n")
            except Exception as e:
                self._diag_append(f"[ERROR] Could not run execute_DAQ_v2: {e}\n")

            self._diag_append("=== Diagnostics complete ===\n")

        threading.Thread(target=_run, daemon=True).start()

    # ------------------------------------------------------------------
    # Waveform Inspection — embedded panel
    # ------------------------------------------------------------------
    def _create_waveform_viewer(self, parent):
        """Create the embedded Waveform Inspection panel."""
        try:
            from managers.waveform_viewer import WaveformViewerPanel
            parent.columnconfigure(0, weight=1)
            parent.rowconfigure(0, weight=1)
            inner = ttk.Frame(parent)
            inner.grid(row=0, column=0, sticky="nsew")
            inner.columnconfigure(0, weight=1)
            inner.rowconfigure(2, weight=1)
            self.waveform_panel = WaveformViewerPanel(inner, self.controller)
        except Exception as exc:
            ttk.Label(parent, text=f"Waveform panel failed to load:\n{exc}",
                      foreground="red", font=("Helvetica", 11)).pack(padx=20, pady=20)
            self.waveform_panel = None

    def focus_waveform_tab(self, file_path: str = None):
        """Switch to the Waveform tab; optionally load a file immediately."""
        try:
            self.notebook.select(self.waveform_tab)
        except Exception:
            pass
        if file_path and self.waveform_panel:
            self.waveform_panel.open_path(file_path)

    # Console (in-UI job output) — gnome-terminal 대체
    # ------------------------------------------------------------------
    def _create_console_viewer(self, parent):
        """DAQ/Produce/Analysis 등 외부 작업의 stdout/stderr 를 UI 안에서 실시간 표시.

        기존엔 작업마다 gnome-terminal 창이 떠서 끝나도 'Press Enter' 상태로
        남아 있었는데, 이 콘솔이 그 역할을 대신한다. 상단 바에 상태/정지/지우기/
        자동스크롤 컨트롤을 두고, 본문은 ScrolledText 로 출력을 누적한다.
        """
        # 리눅스에 항상 존재하는 모노스페이스 폰트를 우선 사용한다(Menlo 는 macOS 전용).
        mono = self._pick_mono_font()

        # 슬롯 분리: DAQ 스트림과 분석(Produce/Analysis/Contour) 출력을 각각 다른
        # 프로세스 + 다른 출력창으로 둔다. 그래서 DAQ 수집 중에도 끝난 run 을
        # 동시에 분석할 수 있다(서로 'Console Busy' 로 막지 않는다).
        self.console_panes = {}

        sub_nb = ttk.Notebook(parent)
        sub_nb.pack(fill=tk.BOTH, expand=True)
        self.console_subnb = sub_nb

        daq_frame = ttk.Frame(sub_nb)
        sub_nb.add(daq_frame, text="⚫ DAQ Stream")
        self._build_console_pane(daq_frame, "daq", mono)

    def ensure_console_pane(self, slot):
        """Lazily create a sub-tab/pane the first time a parallel slot is used
        (produce_1/2/3, analysis_1/2/3). Each parallel job gets its own output tab."""
        if slot in self.console_panes:
            return
        mono = self._pick_mono_font()
        frame = ttk.Frame(self.console_subnb)
        label = self._SLOT_LABELS.get(slot, slot)
        self.console_subnb.add(frame, text=f"⚫ {label}")
        self._build_console_pane(frame, slot, mono)

    # 터미널 ANSI SGR → Tk 텍스트 색상 매핑 (어두운 콘솔에서 읽기 좋은 톤)
    ANSI_RE = re.compile(r'\x1b\[([0-9;]*)m')
    ANSI_FG = {30: "#5c6370", 31: "#e06c75", 32: "#98c379", 33: "#e5c07b",
               34: "#61afef", 35: "#c678dd", 36: "#56b6c2", 37: "#abb2bf"}
    ANSI_FG_BRIGHT = {30: "#7f848e", 31: "#f48771", 32: "#73c991", 33: "#ffd479",
                      34: "#82aaff", 35: "#d886f0", 36: "#67d4e0", 37: "#ffffff"}

    def _ansi_apply(self, pane, codes_str):
        """SGR 코드 문자열(예: '1;35')로 pane 의 색 상태를 갱신하고 태그명을 돌려준다."""
        codes = [int(c) for c in codes_str.split(';') if c != ''] or [0]
        bold = pane.get("ansi_bold", False)
        fg = pane.get("ansi_fg", None)
        for c in codes:
            if c == 0:
                bold = False; fg = None
            elif c == 1:
                bold = True
            elif c == 22:
                bold = False
            elif 30 <= c <= 37:
                fg = c
            elif 90 <= c <= 97:
                fg = c - 60; bold = True
            elif c == 39:
                fg = None
        pane["ansi_bold"] = bold
        pane["ansi_fg"] = fg
        if fg is None:
            tag = None
        else:
            tag = f"ansib{fg}" if bold else f"ansi{fg}"
        pane["ansi_tag"] = tag
        return tag

    def _build_console_pane(self, parent, slot, mono):
        """슬롯(daq/analysis) 하나에 대한 헤더바 + 출력 ScrolledText 를 만든다."""
        bar = tk.Frame(parent, bg="#2d2d2d")
        bar.pack(fill=tk.X)

        status_var = tk.StringVar(value="● Idle")
        status_lbl = tk.Label(bar, textvariable=status_var,
                              font=(mono, 12, "bold"), bg="#2d2d2d", fg="#808080",
                              anchor="w", padx=10, pady=6)
        status_lbl.pack(side=tk.LEFT)

        tk.Button(bar, text="⏹ Stop",
                  command=lambda s=slot: self.controller.stop_console_job(s),
                  bg="#a33", fg="white", relief="flat", padx=10,
                  activebackground="#c44").pack(side=tk.RIGHT, padx=(4, 10), pady=4)
        tk.Button(bar, text="🧹 Clear",
                  command=lambda s=slot: self.clear_console(s),
                  bg="#444", fg="white", relief="flat", padx=10,
                  activebackground="#555").pack(side=tk.RIGHT, padx=4, pady=4)
        term_btn = tk.Button(bar, text="🖥 Terminal",
                  command=lambda s=slot: self._open_last_cmd_in_terminal(s),
                  bg="#2d5a27", fg="white", relief="flat", padx=10,
                  activebackground="#3a7a34")
        term_btn.pack(side=tk.RIGHT, padx=4, pady=4)
        autoscroll = tk.BooleanVar(value=True)
        tk.Checkbutton(bar, text="Auto-scroll", variable=autoscroll,
                       bg="#2d2d2d", fg="#d4d4d4", selectcolor="#2d2d2d",
                       activebackground="#2d2d2d", activeforeground="white",
                       relief="flat").pack(side=tk.RIGHT, padx=8)

        text = scrolledtext.ScrolledText(
            parent, wrap=tk.NONE, state="disabled",
            bg="#1e1e1e", fg="#d4d4d4", insertbackground="#d4d4d4",
            padx=10, pady=8, relief="flat", borderwidth=0,
            font=(mono, 11))
        text.pack(fill=tk.BOTH, expand=True)
        text.tag_config("info",     foreground="#4fc1ff")
        text.tag_config("ok",       foreground="#73c991")
        text.tag_config("err",      foreground="#f48771")
        text.tag_config("warn",     foreground="#e5c07b")
        text.tag_config("progress", foreground="#c678dd")
        text.tag_config("header",   foreground="#ffd479", font=(mono, 11, "bold"))
        text.tag_config("cmd",      foreground="#888888", font=(mono, 10))

        # ANSI(터미널) 색상 태그 — 스크립트가 내보내는 \x1b[..m 코드를 실제 색으로 칠한다.
        for code, color in self.ANSI_FG.items():
            text.tag_config(f"ansi{code}", foreground=color)
        for code, color in self.ANSI_FG_BRIGHT.items():
            text.tag_config(f"ansib{code}", foreground=color)

        self.console_panes[slot] = {
            "frame": parent, "text": text,
            "status_var": status_var, "status_lbl": status_lbl,
            "autoscroll": autoscroll, "term_btn": term_btn,
            "ansi_tag": None, "ansi_bold": False, "ansi_fg": None}

    def _pick_mono_font(self):
        """현재 시스템에 실제로 설치된 모노스페이스 폰트를 골라 반환한다."""
        try:
            available = set(font.families())
        except Exception:
            available = set()
        for name in ("DejaVu Sans Mono", "Liberation Mono", "Ubuntu Mono",
                     "Noto Sans Mono", "Menlo", "Consolas", "Courier New"):
            if name in available:
                return name
        return "TkFixedFont"

    def focus_console(self, slot):
        """Console 탭을 띄우고, 해당 슬롯(daq/analysis) 서브탭을 앞으로 가져온다."""
        try:
            self.notebook.select(self.console_tab)
            pane = self.console_panes.get(slot)
            if pane:
                self.console_subnb.select(pane["frame"])
        except Exception:
            pass

    def _open_last_cmd_in_terminal(self, slot):
        """Open a fresh interactive terminal in the working directory.

        Intentionally does NOT re-run the last command — re-running would spawn a
        second DAQ/analysis job and leave orphan pipeline flags behind. This just
        gives the user a shell (e.g. to inspect files or run a command manually).
        """
        # Pick a sensible working directory: DAQ base path, else project root.
        workdir = None
        try:
            getp = getattr(self.controller, '_get_daq_path', None)
            if getp:
                workdir = getp()
        except Exception:
            workdir = None
        if not workdir or not os.path.isdir(workdir):
            workdir = os.path.dirname(os.path.abspath(__file__))

        try:
            subprocess.Popen(
                ['gnome-terminal', f'--working-directory={workdir}'],
                start_new_session=True)
        except FileNotFoundError:
            # Fallback: xterm
            try:
                subprocess.Popen(['xterm'], cwd=workdir, start_new_session=True)
            except Exception as e:
                messagebox.showerror("Terminal Error", f"Could not open terminal:\n{e}")

    def clear_console(self, slot="analysis"):
        """해당 슬롯 콘솔 내용을 비운다."""
        pane = self.console_panes.get(slot)
        if not pane:
            return
        pane["text"].config(state="normal")
        pane["text"].delete('1.0', tk.END)
        pane["text"].config(state="disabled")
        pane["ansi_tag"] = None
        pane["ansi_bold"] = False
        pane["ansi_fg"] = None

    _CONSOLE_MAX_LINES = 3000  # trim oldest when exceeded

    # Keyword → tag for automatic line colouring in the console.
    _LINE_TAGS = [
        (re.compile(r'^={3,}|^-{3,}'),                          "header"),
        (re.compile(r'\[INFO\]|^\[OK\]|^> PMT '),               "info"),
        (re.compile(r'\[OK\]|succeeded|connection OK|reached',
                    re.IGNORECASE),                              "ok"),
        (re.compile(r'\[ERROR\]|\[FAIL\]|Error:|failed|abort',
                    re.IGNORECASE),                              "err"),
        (re.compile(r'\[WARN\]|Warning:',   re.IGNORECASE),     "warn"),
        (re.compile(r'Processing |^\s*\d+/\d+|\d+\s*%'),        "progress"),
        (re.compile(r'^Changing directory|^Executing with|^Command:'), "cmd"),
    ]

    def _keyword_tag(self, line: str):
        """Return the best-matching tag for a single output line, or None."""
        stripped = line.strip()
        for pattern, tag in self._LINE_TAGS:
            if pattern.search(stripped):
                return tag
        return None

    def _write_segment(self, widget, text, tag, pane):
        """Insert text into widget, applying ANSI colours or keyword tags."""
        if tag:
            widget.insert(tk.END, self.ANSI_RE.sub('', text), tag)
            return

        # If the text has ANSI codes, delegate to ANSI renderer (no keyword coloring).
        if self.ANSI_RE.search(text):
            cur = pane.get("ansi_tag")
            pos = 0
            for m in self.ANSI_RE.finditer(text):
                seg = text[pos:m.start()]
                if seg:
                    widget.insert(tk.END, seg, cur if cur else ())
                cur = self._ansi_apply(pane, m.group(1))
                pos = m.end()
            tail = text[pos:]
            if tail:
                widget.insert(tk.END, tail, cur if cur else ())
            return

        # No ANSI: apply keyword-based line colouring.
        for line in re.split(r'(?<=\n)', text):   # split but keep \n attached
            ktag = self._keyword_tag(line)
            widget.insert(tk.END, line, ktag if ktag else ())

    def console_write(self, text, tag=None, slot="analysis"):
        """Write to console. Must be called from the main thread (master.after)."""
        pane = self.console_panes.get(slot)
        if not pane:
            return
        widget = pane["text"]
        widget.config(state="normal")

        if '\r' in text:
            # \r = carriage return (C++ progress: "Processing... 73%\r" + flush).
            # Popen(text=False) preserves raw \r; text=True would silently convert it to \n.
            #
            # Parsing rules:
            #   \r  → the text BEFORE this \r is the latest overwrite value for this line;
            #          save it as `last_cr` (next \r replaces it, \n commits it).
            #   \n  → commit the current line to completed_lines, reset.
            #   end → if last_cr is non-empty, it is an in-progress progress line:
            #          erase the widget's current last line and redraw with that value.
            #
            # Key insight: "text\r" means "show 'text', cursor back to start".
            # So the segment BEFORE \r is what should be displayed, not discarded.

            text = text.replace('\r\n', '\n')   # Windows CRLF → LF first

            completed_lines = []
            current  = ""    # chars accumulated since last \n or start
            last_cr  = None  # most recent \r-delimited value on this line

            for ch in text:
                if ch == '\r':
                    last_cr = current   # save current as overwrite candidate
                    current = ""        # reset for next segment after \r
                elif ch == '\n':
                    # commit whichever is latest: post-\r text or the last_cr value
                    line_val = current if current else (last_cr or "")
                    completed_lines.append(line_val + '\n')
                    current = ""
                    last_cr = None
                else:
                    current += ch

            # Write completed lines (they ended with \n — normal output)
            if completed_lines:
                self._write_segment(widget, "".join(completed_lines), tag, pane)

            # Determine the live progress value:
            #   last_cr holds the text before the final \r (e.g. "Processing... 73%")
            #   current holds anything after the final \r (usually empty for progress lines)
            progress_val = current if current else last_cr
            if progress_val:
                # Overwrite the widget's current last line in-place
                try:
                    ls = widget.index("end-1c linestart")
                    le = widget.index("end-1c")
                    if ls != le:
                        widget.delete(ls, "end-1c")
                except Exception:
                    pass
                self._write_segment(widget, progress_val, tag, pane)
        else:
            self._write_segment(widget, text, tag, pane)

        # Trim oldest lines to keep widget fast
        line_count = int(widget.index(tk.END).split('.')[0]) - 1
        if line_count > self._CONSOLE_MAX_LINES:
            trim = line_count - self._CONSOLE_MAX_LINES
            widget.delete("1.0", f"{trim + 1}.0")
        widget.config(state="disabled")
        if pane["autoscroll"].get():
            widget.yview_moveto(1)

    # State → (label text, label color, sub-tab prefix, outer-tab indicator)
    _SLOT_STATES = {
        "running": ("▶ Running", "#73c991", "🟢", True),
        "done":    ("✓ Done",    "#4fc1ff", "✅", False),
        "failed":  ("✗ Failed",  "#f48771", "❌", False),
        "stopped": ("⏹ Stopped", "#e5c07b", "⚫", False),
        "idle":    ("● Idle",    "#808080", "⚫", False),
    }
    _SLOT_LABELS = {
        "daq":        "DAQ Stream",
        "analysis":   "Rate Scan",
        "contour":    "Contour",
        "produce_1":  "Produce 1",
        "produce_2":  "Produce 2",
        "produce_3":  "Produce 3",
        "analysis_1": "Analysis 1",
        "analysis_2": "Analysis 2",
        "analysis_3": "Analysis 3",
        "contour_1":  "Contour 1",
        "contour_2":  "Contour 2",
        "contour_3":  "Contour 3",
    }

    # Produce / Analysis / Contour each run in parallel across these slots (max 3 concurrent).
    PRODUCE_SLOTS  = ("produce_1", "produce_2", "produce_3")
    ANALYSIS_SLOTS = ("analysis_1", "analysis_2", "analysis_3")
    CONTOUR_SLOTS  = ("contour_1", "contour_2", "contour_3")

    def console_set_status(self, text, slot="analysis", state="idle"):
        """Update status label color + sub-tab label + outer tab indicator."""
        pane = self.console_panes.get(slot)
        if not pane:
            return
        _, color, dot, _ = self._SLOT_STATES.get(state, self._SLOT_STATES["idle"])
        pane["status_var"].set(text)
        pane["status_lbl"].config(fg=color)
        # Update sub-tab label
        try:
            label = f"{dot} {self._SLOT_LABELS.get(slot, slot)}"
            self.console_subnb.tab(pane["frame"], text=label)
        except Exception:
            pass
        # Update outer "Output" tab — green dot if any slot is running
        self._refresh_output_tab_label()

    def _refresh_output_tab_label(self):
        """Set outer tab to 🟢 Output if any slot is actively running, else 📟 Output."""
        running = any(
            self._SLOT_STATES.get(
                getattr(p.get("status_lbl"), "_state", "idle"), self._SLOT_STATES["idle"]
            )[3]
            for p in self.console_panes.values()
        )
        # Check via status label text instead
        running = any(
            "▶" in p["status_var"].get()
            for p in self.console_panes.values()
        )
        try:
            self.notebook.tab(self.console_tab,
                              text="🟢 Output" if running else "📟 Output")
        except Exception:
            pass

    def update_file_info_panel(self, file_path):
        try:
            stat = os.stat(file_path)
            size_mb = stat.st_size / (1024 * 1024)
            mtime = datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
            filename = os.path.basename(file_path)
            base_info = (f"File: {filename}\n\nSize: {size_mb:.2f} MB\nModified: {mtime}\n")
        except FileNotFoundError:
            self.file_info_label.config(text=f"File not found:\n{os.path.basename(file_path)}")
            return
        except Exception as e:
            self.file_info_label.config(text=f"Could not get file info:\n{e}")
            return

        # For .root files, also surface the RunInfo metadata (SN / HV / angles / shifter).
        if not file_path.lower().endswith('.root'):
            self.file_info_label.config(text=base_info)
            return

        if not hasattr(self, '_runinfo_cache'):
            self._runinfo_cache = {}
        self._info_current_path = file_path

        cached = self._runinfo_cache.get(file_path)
        if cached is not None:
            self.file_info_label.config(text=base_info + "\n" + cached)
            return

        self.file_info_label.config(text=base_info + "\n⏳ Reading run info...")

        def worker():
            meta = self._read_runinfo(file_path)
            self._runinfo_cache[file_path] = meta
            def apply():
                if getattr(self, '_info_current_path', None) == file_path:
                    self.file_info_label.config(text=base_info + "\n" + meta)
            try:
                self.master.after(0, apply)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _read_runinfo(self, file_path):
        """Run dump_runinfo.C through ROOT and format the RunInfo metadata (no uproot needed)."""
        import subprocess
        try:
            daq_path = self.controller._get_daq_path() or os.path.dirname(file_path)
            macro = os.path.join(daq_path, 'dump_runinfo.C')
            if not os.path.exists(macro):
                return "(run info: dump_runinfo.C not found)"
            cmd = ['root', '-l', '-b', '-q', f'{macro}("{file_path}")']
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout
            kv = {}
            for line in out.splitlines():
                if '=' in line and not line.startswith('Processing'):
                    k, _, v = line.partition('=')
                    kv[k.strip()] = v.strip()
            if 'ERR' in kv:
                return f"(run info unavailable: {kv['ERR']})"
            if 'SN1' not in kv and 'RunMode' not in kv:
                return "(no RunInfo in this file)"
            return (
                "── Run Info ──\n"
                f"Mode: {kv.get('RunMode','?')}\n"
                f"Shifter: {kv.get('Shifter','?')}  /  Expert: {kv.get('Expert','?')}\n"
                f"Laser: {kv.get('Laser_mA','?')} mA @ {kv.get('Wavelength','?')} nm\n"
                f"CH0 (mon): {kv.get('SN1','?')}  HV {kv.get('HV1','?')}\n"
                f"CH1: {kv.get('SN2','?')}  HV {kv.get('HV2','?')}\n"
                f"     Rot {kv.get('Rot2','?')}°, Tilt {kv.get('Tilt2','?')}°\n"
                f"CH2: {kv.get('SN3','?')}  HV {kv.get('HV3','?')}\n"
                f"     Rot {kv.get('Rot3','?')}°, Tilt {kv.get('Tilt3','?')}°\n"
                f"Note: {kv.get('NOTE','')}"
            )
        except subprocess.TimeoutExpired:
            return "(run info: ROOT timed out)"
        except FileNotFoundError:
            return "(run info: 'root' not found)"
        except Exception as e:
            return f"(run info error: {e})"

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

            # 2. Mode Filtering ('Dark' or 'Laser')
            # Laser/Dark is determined by the SUBFOLDER (.../RAW/Laser, .../RAW/Dark),
            # not by the filename (raw files are named precal_raw_kor_run_DATE_NNN.root with
            # no mode tag). The old filename-keyword match therefore never hit and the list
            # came up empty. Match the folder path first, and still allow a filename tag as a
            # fallback for older/processed files that encode the mode in the name.
            if filter_mode != "All":
                m = filter_mode.lower()          # "dark" or "laser"
                folder_key = os.sep + m          # "/dark" or "/laser"
                name_key = f"_{m}"
                filtered_list = [
                    f for f in filtered_list
                    if folder_key in f["path"].lower() or name_key in f["filename"].lower()
                ]

            # 3. Sort
            if sort_mode == 'name':
                filtered_list.sort(key=lambda x: x["filename"])
            else: # time
                filtered_list.sort(key=lambda x: x["mtime_float"], reverse=True)

            if search_query:
                filtered_list = [f for f in filtered_list if search_query in f["filename"].lower()]

            # 4. Treeview — insert in one batch after clearing to minimise redraws
            tree.delete(*tree.get_children())
            # Temporarily detach from display during bulk insert (avoids per-row redraws)
            try:
                tree.config(displaycolumns=[])  # hide columns → suppress redraws
            except Exception:
                pass
            for file_info in filtered_list:
                tree.insert("", tk.END, values=(file_info["filename"], file_info["path"], file_info["mtime"]))
            try:
                tree.config(displaycolumns="#all")  # restore
            except Exception:
                pass

    def on_data_file_double_click(self, event):
        """Treeview에서 아이템을 더블클릭했을 때 호출됩니다."""
        tree = event.widget 
        if not tree.selection(): return

        item_id = tree.selection()[0]
        item_values = tree.item(item_id, "values")
        if item_values:
            filename, dir_path, _ = item_values
            full_path = os.path.join(dir_path, filename)
            #self.controller.open_root_file_browser(full_path)
            self.controller.open_data_file_viewer(full_path)


    def on_data_file_select(self, event):
        """Update info panel when a file is selected. Debounced 150ms to avoid
        spawning many ROOT metadata threads during Ctrl/Shift multi-select."""
        if hasattr(self, '_select_debounce_id') and self._select_debounce_id:
            try:
                self.master.after_cancel(self._select_debounce_id)
            except Exception:
                pass
        self._select_debounce_id = self.master.after(150, lambda: self._do_file_select(event.widget))

    def _do_file_select(self, tree):
        self._select_debounce_id = None
        if not tree.selection():
            return
        item_id = tree.selection()[0]
        item_values = tree.item(item_id, "values")
        if item_values:
            filename, dir_path, _ = item_values
            full_path = os.path.join(dir_path, filename)
            self.update_file_info_panel(full_path)
            match = re.search(r'(\d+)(?=[^\d]*\.root$)', filename)
            extracted_run = match.group(1) if match else "0"
            self.run_number_var.set(extracted_run)

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
            
            default_pulse = 133 if wl == "405nm" else 0.0
            
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
                "bias_current": tk.StringVar(value="0.00 mA"),
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

        vars_dict["current_mode_disp"] = tk.StringVar(value="Current: External")
        ttk.Label(trig_frame, textvariable=vars_dict["current_mode_disp"],
                  font=("Helvetica", 10, "bold"), foreground="#c92a2a").pack(side=tk.LEFT, padx=10)

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

        log_text = scrolledtext.ScrolledText(log_tab, wrap=tk.WORD, bg="#1e1e1e", fg="#d4d4d4", font=("Menlo", 10), state="disabled")
        log_text.pack(fill=tk.BOTH, expand=True)
        vars_dict["log_text_obj"] = log_text # Assign object to handle isolated logging entries

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

    def set_laser_controls_state(self, state):
        """기존 레이저 제어 버튼 외에 자동화 탭의 버튼들도 함께 제어합니다."""
        # 1. 기존 레이저 버튼 제어 (state는 'normal' 또는 'disabled')
        if hasattr(self, 'laser_tabs_data'):
            for wl, vars_dict in self.laser_tabs_data.items():
                if "ld_on_btn" in vars_dict: vars_dict["ld_on_btn"].config(state=state)
                if "ld_off_btn" in vars_dict: vars_dict["ld_off_btn"].config(state=state)
                if "tec_on_btn" in vars_dict: vars_dict["tec_on_btn"].config(state=state)
                if "tec_off_btn" in vars_dict: vars_dict["tec_off_btn"].config(state=state)
                if "curr_apply_btn_obj" in vars_dict: vars_dict["curr_apply_btn_obj"].config(state=state)

        is_unlocked = (state == tk.NORMAL)
        if hasattr(self.controller, 'auto_ui'):
            self.controller.auto_ui.set_buttons_state(is_unlocked)

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
            ("Bias Current", "bias_live"),
            ("Pulse Current", "pulse_live"),
            ("Check Int.", "check_interval")
        ]

        for label_text, var_key in items:
            row = ttk.Frame(status_grid)
            row.pack(fill=tk.X, pady=2)
            
            ttk.Label(row, text=f"{label_text}:", width=15, font=("Helvetica", 10, "bold")).pack(side=tk.LEFT)
            
            lbl = ttk.Label(row, textvariable=vars_dict[var_key], width=15, relief="groove")
            lbl.pack(side=tk.LEFT)

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

        self.web_canvas.bind("<ButtonPress-1>", self._on_canvas_click)
        self.web_canvas.bind("<B1-Motion>", self._on_canvas_drag)

        self.web_canvas.bind("<Button-4>", self._on_canvas_scroll_zoom) # Linux Scroll UP
        self.web_canvas.bind("<Button-5>", self._on_canvas_scroll_zoom) # Linux Scroll DOWN
        self.web_canvas.bind("<MouseWheel>", self._on_canvas_scroll_zoom) # Windows Scroll

        w_center = 600
        h_center = 350
        self.canvas_text_id = self.web_canvas.create_text(
            w_center, h_center, text="Click 'Start' to verify VPN & Monitor", font=("Helvetica", 14), fill="gray"
        )
        self.web_image_id = None 

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

        self.ups_change_port_btn = ttk.Button(conn_frame, text="Change...",
                                              command=self.controller.unlock_ups_port,
                                              state="disabled")
        self.ups_change_port_btn.pack(side=tk.LEFT, padx=2)

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
        outlet_pane = ttk.LabelFrame(mid_frame, text=" 🔌 Outlet Status (2x2) ", padding=10)
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

        ttk.Separator(dashboard, orient="horizontal").pack(fill=tk.X, pady=(10, 5))
        self.global_job_status_label = ttk.Label(dashboard, text="📊 Pipeline Monitor: Idle", 
                                                 font=("Helvetica", 11, "bold"), foreground="gray", anchor="center")
        self.global_job_status_label.pack(fill=tk.X, expand=True, pady=2)

        self._init_global_pipeline_watcher()

        self.master.after(100, self._update_dashboard_loop)


    def _create_lock_banner(self, parent):
        """Plan A 안전 잠금 배너.

        DAQ 탭 최상단(System Connection Overview 바로 아래)에 항상 표시되며,
        잠금 상태에 따라 배너 자체가 토글된다.
          - 잠김(LOCKED)  : 키 큰 빨간 배너 + 'Unlock Controls' 버튼 (강한 시각 경고)
          - 해제(ACTIVE)  : 얇은 초록 바 + 'Lock' 버튼 (공간 최소화)
        한 번 Unlock 하면 access_mgr.unlocked 가 True 로 유지되어 프로그램 종료까지
        풀린 상태가 지속되고, Lock 버튼을 누르면 다시 잠긴다(요구사항 4).
        """
        # pady 는 _update_lock_banner 에서 상태별로 다시 설정한다(높이 토글).
        self._lock_banner = tk.Frame(parent, bg="#dc3545")
        self._lock_banner.pack(fill=tk.X, padx=5, pady=(0, 4))

        # Unlock/Lock 토글 버튼을 '왼쪽'에 배치해 눈에 잘 띄게 한다.
        # request_control_unlock() 가 토글 동작(잠금<->해제)을 수행한다.
        self._banner_unlock_btn = tk.Button(
            self._lock_banner,
            text="🔒  Unlock Controls",
            font=("Helvetica", 13, "bold"),
            bg="#f0ad4e", fg="black",
            relief="flat", padx=18, pady=4,
            command=self.controller.request_control_unlock)
        self._banner_unlock_btn.pack(side=tk.LEFT, padx=12, pady=4)

        # 버튼 오른쪽: 잠금 아이콘 + 상태 안내 문구
        # (이 프레임도 상태에 따라 배경색을 바꿔야 한다. 안 그러면 Unlock 후에도
        #  글씨 둘레에 빨간 박스가 남는다.)
        self._lock_left_frame = tk.Frame(self._lock_banner, bg="#dc3545")
        self._lock_left_frame.pack(side=tk.LEFT, padx=4, pady=4)
        left = self._lock_left_frame

        self._lock_icon_lbl = tk.Label(left, text="🔒", font=("Helvetica", 20),
                                       bg="#dc3545", fg="white")
        self._lock_icon_lbl.pack(side=tk.LEFT)

        self._lock_text_lbl = tk.Label(
            left,
            text="  SYSTEM LOCKED  —  Unlock before running DAQ / General Scan or turning on the Laser.",
            font=("Helvetica", 11, "bold"), bg="#dc3545", fg="white")
        self._lock_text_lbl.pack(side=tk.LEFT, padx=8)

        self._update_lock_banner()

    def _update_lock_banner(self):
        """배너 색/문구/버튼을 현재 잠금 상태에 맞춰 1초마다 동기화한다."""
        if getattr(self.controller, '_shutting_down', False):
            return
        is_unlocked = getattr(getattr(self.controller, 'access_mgr', None), 'unlocked', True)
        if is_unlocked:
            # 해제 상태: 얇은 초록 바로 축소하여 공간을 거의 차지하지 않게 한다.
            self._lock_banner.config(bg="#28a745")
            self._lock_left_frame.config(bg="#28a745")
            self._lock_icon_lbl.config(text="🔓", bg="#28a745", font=("Helvetica", 13))
            self._lock_text_lbl.config(
                text="  CONTROLS ACTIVE — system unlocked.",
                bg="#28a745", font=("Helvetica", 10, "bold"))
            self._banner_unlock_btn.config(
                text="🔓  Lock", bg="#1e7e34", fg="white",
                font=("Helvetica", 10, "bold"))
        else:
            # 잠금 상태: 키 큰 빨간 배너로 강하게 경고한다.
            self._lock_banner.config(bg="#dc3545")
            self._lock_left_frame.config(bg="#dc3545")
            self._lock_icon_lbl.config(text="🔒", bg="#dc3545", font=("Helvetica", 20))
            self._lock_text_lbl.config(
                text="  SYSTEM LOCKED  —  Unlock before running DAQ / General Scan or turning on the Laser.",
                bg="#dc3545", font=("Helvetica", 11, "bold"))
            self._banner_unlock_btn.config(
                text="🔒  Unlock Controls", bg="#f0ad4e", fg="black",
                font=("Helvetica", 13, "bold"))

        self.master.after(1000, self._update_lock_banner)

    def _update_dashboard_loop(self):
        if getattr(self.controller, '_shutting_down', False):
            return
        statuses = self.controller.get_system_status()

        statuses["B-field"] = getattr(self, "web_connection_status", False)

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
        """전체 시스템 버튼의 '불빛'을 제어권 상태에 동기화합니다."""
        is_unlocked = getattr(self.controller.access_mgr, 'unlocked', True)
        state = tk.NORMAL if is_unlocked else tk.DISABLED
        
        # Locked 상태 색상 정의 (전등 꺼진 느낌)
        bg_locked = "#3a3a3a"
        fg_locked = "#777777"
        
        # 1. Unlock 버튼 자체의 상태 업데이트
        if self.unlock_btn:
            if is_unlocked:
                self.unlock_btn.config(text="🔓 Controls Active", bg="#28a745", fg="white")
            else:
                self.unlock_btn.config(text="🔒 Unlock Controls", bg="#f0ad4e", fg="black")

        # 2. 레이저 탭 버튼들 '불 끄기'
        if hasattr(self, 'laser_tabs_data'):
            for wl, vars_dict in self.laser_tabs_data.items():
                for btn_key in ["ld_on_btn", "ld_off_btn", "tec_on_btn", "tec_off_btn", "curr_apply_btn_obj"]:
                    if btn_key in vars_dict:
                        btn = vars_dict[btn_key]
                        btn.config(state=state)
                        
                        # [보완] 비활성화 시 시각적으로 '꺼진' 효과 부여
                        if not is_unlocked:
                            try:
                                btn.config(bg=bg_locked, fg=fg_locked)
                            except tk.TclError:
                                pass

        if hasattr(self.controller, 'auto_ui'):
            self.controller.auto_ui.set_buttons_state(is_unlocked)

        # run_daq (Execute Scripts sidebar) 버튼도 잠금 상태에 맞게 동기화
        if 'run_daq' in self.buttons:
            self.buttons['run_daq'].config(state=state)

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
        self.master.bind("<F5>", lambda e: self.controller.refresh_all_data())

    def show_loading_overlay(self, message="Processing..."):
        """Displays a centered loading overlay."""
        if not hasattr(self, 'loading_frame'):
            self.loading_frame = tk.Frame(self.master, bg="#2c2c2e", highlightthickness=2, highlightbackground="#0a84ff")

            self.loading_label = ttk.Label(
                self.loading_frame,
                text=message,
                font=("Helvetica", 16, "bold"),
                foreground="white",
                background="#2c2c2e"
            )
            self.loading_label.pack(padx=50, pady=40)
        else:
            self.loading_label.config(text=message)

        self.loading_frame.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
        self.loading_frame.lift()
        self.master.update_idletasks()

    def hide_loading_overlay(self):
        """Hides the loading overlay."""
        if hasattr(self, 'loading_frame'):
            self.loading_frame.place_forget()


    def _init_global_pipeline_watcher(self):
        """Initializes shared tracking directory anchors for active process telemetry."""
        self.pipeline_flag_dir = "/tmp/daq_flags"
        self.cached_active_run = None
        self._poll_global_pipeline_flags()

    def _purge_stale_flags(self, flag_list, proc_name=None, grace=5):
        """Drop flag files left behind by a dead/cancelled run so the monitor self-heals.

        If proc_name is given and that process is alive, the flags are real -> keep them.
        Otherwise remove any flag older than `grace` seconds and return the survivors.
        """
        if not flag_list:
            return flag_list
        import os, time, subprocess
        if proc_name:
            try:
                # A real acquisition run is `execute_DAQ_v2 <args>` WITHOUT -j.
                # The connection probe (`execute_DAQ_v2 -j`) fires every 2s and must
                # NOT be mistaken for a live run, or stale flags never get purged.
                probe = subprocess.run(
                    f'pgrep -x {proc_name} | xargs -r ps -o args= -p 2>/dev/null | grep -v -- "-j"',
                    shell=True, capture_output=True, text=True)
                if probe.returncode == 0 and probe.stdout.strip():
                    return flag_list  # the run is genuinely active
            except Exception:
                return flag_list  # pgrep unavailable -> don't risk purging a live run
        survivors = []
        now = time.time()
        for fp in flag_list:
            try:
                if (now - os.path.getmtime(fp)) > grace:
                    os.remove(fp)
                else:
                    survivors.append(fp)
            except Exception:
                survivors.append(fp)
        return survivors

    def _poll_global_pipeline_flags(self):
        """Sweeps flag directory every 1s to project centralized process tracking onto the global frame layout."""
        import glob
        import os

        if getattr(self.controller, '_shutting_down', False):
            return

        if hasattr(self, 'master') and self.master.winfo_exists():
            try:
                daq_flags  = glob.glob(os.path.join(self.pipeline_flag_dir, "daq_*.flag"))
                prod_flags = glob.glob(os.path.join(self.pipeline_flag_dir, "prod_*.flag"))
                read_flags = glob.glob(os.path.join(self.pipeline_flag_dir, "read_*.flag"))
                cont_flags = glob.glob(os.path.join(self.pipeline_flag_dir, "contour_*.flag"))

                # Self-heal: a cancelled Run (or a closed terminal/tmux window) can leave its
                # flag behind, which made the monitor keep showing a dead run as "Active".
                # DAQ: trust the flag only while execute_DAQ_v2 is actually running.
                # Analysis steps are short, so just clear any analysis flag older than 20 min.
                daq_flags  = self._purge_stale_flags(daq_flags,  proc_name="execute_DAQ_v2", grace=5)
                prod_flags = self._purge_stale_flags(prod_flags, proc_name=None, grace=1200)
                read_flags = self._purge_stale_flags(read_flags, proc_name=None, grace=1200)
                cont_flags = self._purge_stale_flags(cont_flags, proc_name=None, grace=1200)

                def get_latest_run_from_flags(flag_list):
                    if not flag_list:
                        return None
                    try:
                        nums = [int(os.path.basename(f).split("_")[1].split(".")[0]) for f in flag_list]
                        return str(max(nums))
                    except Exception:
                        return os.path.basename(flag_list[0]).split("_")[1].split(".")[0]

                active_daq_run  = get_latest_run_from_flags(daq_flags)
                active_prod_run = get_latest_run_from_flags(prod_flags)
                active_read_run = get_latest_run_from_flags(read_flags)
                active_cont_run = get_latest_run_from_flags(cont_flags)

                if active_daq_run:
                    self.cached_active_run = active_daq_run
                    self.global_job_status_label.config(text=f"📡 [Run {active_daq_run}] DAQ: Stream Recording Active... (Live Collecting)", foreground="#dc3545")
                elif active_prod_run:
                    self.cached_active_run = active_prod_run
                    self.global_job_status_label.config(text=f"📊 [Run {active_prod_run}] Analysis: Converting Raw ROOT Trees...", foreground="#ffcc00")
                elif active_read_run:
                    self.cached_active_run = active_read_run
                    self.global_job_status_label.config(text=f"📊 [Run {active_read_run}] Analysis: Executing Mathematical Fit Models...", foreground="#ffcc00")
                elif active_cont_run:
                    self.cached_active_run = active_cont_run
                    self.global_job_status_label.config(text=f"📊 [Run {active_cont_run}] Analysis: Rendering Boundary Matrix Contours...", foreground="#ffcc00")
                else:
                    if self.cached_active_run:
                        done_flag = os.path.join(self.pipeline_flag_dir, f"done_{self.cached_active_run}.flag")
                        if os.path.exists(done_flag):
                            self.global_job_status_label.config(text=f"✅ [Run {self.cached_active_run}] Pipeline Sequence Processed Successfully!", foreground="#00e676")
                            try: os.remove(done_flag) # Flush success trigger token cleanly
                            except Exception: pass
                            self.cached_active_run = None
                        else:
                            self.global_job_status_label.config(text="📊 Pipeline Monitor: Idle", foreground="gray")
                    else:
                        self.global_job_status_label.config(text="📊 Pipeline Monitor: Idle", foreground="gray")
            except Exception as e:
                print(f"[WARNING] Centralized pipeline monitoring glitch: {e}")

            self.master.after(150, self._poll_global_pipeline_flags)

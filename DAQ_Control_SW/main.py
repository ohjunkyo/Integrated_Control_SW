# main.py
import warnings
warnings.filterwarnings("ignore", message="Mean of empty slice", category=RuntimeWarning)
warnings.filterwarnings("ignore", message="invalid value encountered in scalar divide", category=RuntimeWarning)

import tkinter as tk
from PIL import Image, ImageTk
from tkinter import ttk, filedialog, messagebox
# customtkinter migration (in progress). Guarded so the app still runs if the
# package is missing; callers check CTK_AVAILABLE before using ctk.
try:
    import customtkinter as ctk
    CTK_AVAILABLE = True

    # Upstream bug workaround (customtkinter 6.0.0, ctk_scrollable_frame.py):
    # CTkScrollableFrame binds its mouse-wheel handler with bind_all(), so it
    # fires for scroll events anywhere in the app, not just inside a
    # scrollable frame. _check_if_valid_scroll() assumes event.widget is
    # always a real Tkinter widget object and calls widget.master on it --
    # but Tk sometimes delivers event.widget as a raw Tcl path STRING instead
    # (observed scrolling over a ttk.Combobox dropdown and over an embedded
    # matplotlib FigureCanvasTkAgg, e.g. the B-field Monitoring tab), which
    # crashes with "AttributeError: 'str' object has no attribute 'master'".
    # Patch: treat a string widget as "not a valid scroll target" instead of
    # recursing into .master.
    try:
        from customtkinter.windows.widgets.ctk_scrollable_frame import CTkScrollableFrame as _CTkScrollableFrame
        _orig_check_valid_scroll = _CTkScrollableFrame._check_if_valid_scroll

        def _patched_check_valid_scroll(self, widget):
            if isinstance(widget, str):
                return False
            return _orig_check_valid_scroll(self, widget)

        _CTkScrollableFrame._check_if_valid_scroll = _patched_check_valid_scroll
    except Exception:
        pass   # best-effort patch; a failure here must never block app startup
except Exception:
    ctk = None
    CTK_AVAILABLE = False
import time
import math
import sys
import os
import signal
import subprocess
import webbrowser
import threading
import json
import re
import shutil
import glob
import queue
import collections
import serial
import serial.tools.list_ports
import matplotlib.pyplot as plt
import matplotlib.dates as mdates 
import logging 
from logging.handlers import TimedRotatingFileHandler 
import random 
import socket

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from datetime import datetime

from ui_manager import UIManager
from config_manager import ConfigManager
from pmt_config_window import PMTConfigWindow
from managers.ups_manager import UPSManager
from managers.laser_manager import LaserManager
from managers.control_access import ControlAccessManager
from managers.rotation_manager import AutomationManager 
from managers.rotation_control import RotationManager 
from managers.ui_automation import AutomationUI

APP_CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".daq_control_config.json")
#APP_CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".daq_control_config_TEST.json")


current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
laser_dir = os.path.join(parent_dir, 'Laser_Control_SW', 'app')
#print(f"DEBUG: Current Script Dir: {current_dir}")
#print(f"DEBUG: Looking for Laser Dir at: {laser_dir}")
#print(f"DEBUG: Does it exist?: {os.path.exists(laser_dir)}")
LASER_AVAILABLE = False 

if os.path.exists(laser_dir):
    if laser_dir not in sys.path:
        sys.path.append(laser_dir)
    try:
        from laser_driver import TamadenshiLaser
        LASER_AVAILABLE = True
        print("✅ Laser driver imported successfully.")
    except ImportError as e:
        print(f"❌ Failed to import laser driver: {e}")
else:
    print(f"⚠️ Warning: Directory not found: {laser_dir}")

class App:
    def __init__(self, master, base_dir):
        self.master = master
        self.base_dir = base_dir

        # [NEW] Smooth Startup Splash Guard: Hide the un-themed main window immediately to prevent light-gray visual flashing
        self.master.withdraw()
        self._show_startup_splash()
        
        self.terminal_preference = 'gnome-terminal'
        self.start_time = datetime.now()
        # When True, periodic .after() poll loops bail out instead of rescheduling.
        # Prevents "invalid command name" TclError spam during shutdown, when a
        # queued callback fires after its target widget has been destroyed.
        self._shutting_down = False
        self.config_manager = None
        self.contacts_file = os.path.join(self.base_dir, "contacts.json")
        
        # 1. 설정 로드
        self.load_app_config()
        self.ui_prefs = self._load_ui_prefs()

        if self.config_manager and self.config_manager.get_config_value("LogDir"):
            base_log_dir = self.config_manager.get_config_value("LogDir")
            self.laser_log_dir = "/home/precalkor/ADC/ADC_test/LOG/LASER"
            os.makedirs(self.laser_log_dir, exist_ok=True)
        else:
            self.laser_log_dir = os.path.join(self.base_dir, "LOG", "LASER")
            os.makedirs(self.laser_log_dir, exist_ok=True) 

        self.laser_port_mapping = {
            "375nm": "1-3.4.4:1.0", "405nm": "1-3.4.1:1.0",
            "450nm": "1-3.4.2:1.0", "473nm": "1-3.4.3:1.0"
        }

        # 2. 로직 매니저 생성
        self.access_mgr = ControlAccessManager(self, password="root")
        self.rot_mgr = RotationManager(self)
        self.auto_mgr = AutomationManager(self)

        # Created before UIManager so the lock banner (always visible, unlike the
        # bottom status bar which can be pushed off-screen on tall content) can
        # bind a badge label to this var.
        self.update_badge_var = tk.StringVar()

        # 3. UI 생성
        self._setup_theme()
        self.ui = UIManager(master, self)
        self.auto_ui = self.ui.auto_ui

        # 4. 하드웨어 매니저 생성 (중복 제거 완료)
        self.laser_mgr = LaserManager(self)
        self.ups_mgr = UPSManager(self)

        master.title(f"DAQ/LASER/UPS Control Panel — {self._version_string()}")
        master.geometry("1600x950")
        self.master.minsize(1400, 900)

        icon_path = os.path.join(self.base_dir, 'icons', 'DAQcontroller.png')
        if os.path.exists(icon_path):
            img = Image.open(icon_path)
            self.p_img = ImageTk.PhotoImage(img, master=master)
            master.iconphoto(True, self.p_img)

        # 5. 연락망 로드
        self.load_contacts()

        # 6. 레이저 인스턴스 생성 로직
        if LASER_AVAILABLE:
            for wl in self.laser_mgr.wavelengths:
                try:
                    self.laser_mgr.laser_instances[wl] = TamadenshiLaser()
                    if wl == "405nm":
                        self.laser = self.laser_mgr.laser_instances[wl]
                except Exception as e:
                    self._log(f"Laser {wl} init failed: {e}")

        # 7. 상태바 세팅
        self._setup_status_bar()

        # 7b. Update & Restart watcher — polls source mtimes; shows a status-bar
        # badge when the code on disk is newer than this running process.
        self._app_code_baseline = time.time()
        self._update_available = False
        self._restart_when_idle = False
        self.master.after(60000, self._check_for_code_updates)

        # 8. 초기 데이터 리프레시 및 스케줄러 등록
        self.ui.setup_shortcuts()
        if self.config_manager:
            self.validate_config_paths()
            self.master.after(500, self.refresh_all_data)
            self.master.after(1000, self.check_daq_connection)
        
        self.ui.is_dark_mode = True
        self.ui.toggle_theme()

        if hasattr(self, 'laser_mgr') and self.laser_mgr.laser_instances:
            self.on_laser_trigger_change()

        self.setup_laser_logger()
        self.load_today_laser_log()
        self.preload_laser_history()
        self.preload_ups_history()

        self.master.after(500, self.auto_connect_ups)
        self.master.after(500, self.auto_connect_laser)
        self.master.after(500, self.handle_mode_change) 
        self.update_laser_status_loop()

        if hasattr(self, 'auto_ui') and hasattr(self.auto_ui, 'update_sn_display'):
            self.rot_mgr.start_monitoring(self._on_live_angles)

        if hasattr(self, 'ui'):
            self.ui.refresh_ui_state()

        if hasattr(self, 'splash') and self.splash.winfo_exists():
            self.splash.destroy()
        
        self.master.update_idletasks()
        self.master.deiconify() # Gracefully project the optimized dark window layout to shifter
        self.master.protocol("WM_DELETE_WINDOW", self.on_closing)

    # [NEW METHOD ADDITION inside App Class]
    def _show_startup_splash(self):
        """Generates a professional dark-themed loading window during initial UI layout parsing."""
        self.splash = tk.Toplevel(self.master)
        self.splash.title("System Loading")
        self.splash.geometry("420x200")
        self.splash.configure(bg="#1e1e1e")
        self.splash.overrideredirect(True) # Eliminate system borders for sleek layout execution
        
        # Center the initialization loading dialog precisely on target display canvas
        screen_w = self.splash.winfo_screenwidth()
        screen_h = self.splash.winfo_screenheight()
        x = (screen_w // 2) - 210
        y = (screen_h // 2) - 100
        self.splash.geometry(f"+{x}+{y}")
        
        # UI Text Indicators Asset Map
        lbl_main = tk.Label(self.splash, text="INTEGRATED DAQ CONTROL PANEL", 
                            font=("Helvetica", 13, "bold"), bg="#1e1e1e", fg="#007ACC")
        lbl_main.pack(pady=(40, 8))
        
        lbl_sub = tk.Label(self.splash, text="Loading hardware matrices & rendering theme elements...", 
                           font=("Helvetica", 10), bg="#1e1e1e", fg="#a6a6a6")
        lbl_sub.pack(pady=5)
        
        cv_bar = tk.Canvas(self.splash, width=280, height=3, bg="#2d2d2d", highlightthickness=0)
        cv_bar.pack(pady=20)
        cv_bar.create_rectangle(0, 0, 140, 3, fill="#007ACC", width=0)
        
        self.splash.update()

    def _setup_status_bar(self):
        self.status_bar = ttk.Frame(self.master, relief=tk.SUNKEN, padding="2 5")
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)

        self.elapsed_time_var = tk.StringVar()
        self.clock_var = tk.StringVar()
        self.moving_indicator_var = tk.StringVar()

        ttk.Label(self.status_bar, textvariable=self.clock_var).pack(side=tk.LEFT, padx=10)
        # Visible from every tab (status_bar is a direct child of master, not the
        # notebook) so a rotation move is never invisible regardless of which
        # panel the operator is looking at -- whether triggered by "Reset angle"
        # or a manual Move Tilt/Rot click.
        self.moving_lbl = tk.Label(self.status_bar, textvariable=self.moving_indicator_var,
                                   font=("Helvetica", 10, "bold"), fg="#dc3545")
        self.moving_lbl.pack(side=tk.LEFT, padx=20)

        # Update & Restart badge — empty until _check_for_code_updates() detects
        # newer source files on disk. Clickable from any tab. (self.update_badge_var
        # itself is created earlier, before UIManager, so the lock banner can also
        # show it — this status-bar copy is a second, redundant place to click it.)
        self.update_badge_lbl = tk.Label(self.status_bar, textvariable=self.update_badge_var,
                                         font=("Helvetica", 10, "bold"), fg="#e67700",
                                         cursor="hand2")
        self.update_badge_lbl.pack(side=tk.LEFT, padx=10)
        self.update_badge_lbl.bind("<Button-1>", lambda e: self._on_update_badge_click())

        ttk.Label(self.status_bar, textvariable=self.elapsed_time_var).pack(side=tk.RIGHT, padx=10)

        self._update_status_bar()

    def is_production_running(self):
        try:
            result = subprocess.run(['pgrep', '-f', 'main.py'], capture_output=True, text=True)
            pids = result.stdout.strip().split()
            return len(pids) > 1
        except Exception:
            return False

    def _on_live_angles(self, dev_num, tilt, rot):
        """모터 모니터링 스레드 콜백. 라이브 각도를 (1) Manual Control Panel 상태 라벨과
        (2) PMT Setup & Helper 다이어그램 양쪽에 전달한다."""
        try:
            self.auto_ui.update_sn_display(dev_num, tilt, rot)
        except Exception:
            pass
        if hasattr(self.auto_ui, 'update_quick_setup_live'):
            try:
                self.auto_ui.update_quick_setup_live(dev_num, tilt, rot)
            except Exception:
                pass
        if hasattr(self, 'ui') and hasattr(self.ui, 'update_helper_live'):
            try:
                self.ui.update_helper_live(dev_num, tilt, rot)
            except Exception:
                pass
        if hasattr(self, 'ui') and hasattr(self.ui, 'update_pmt_position_widget'):
            try:
                self.ui.update_pmt_position_widget(dev_num, tilt, rot)
            except Exception:
                pass
        self._update_moving_indicator()

    def _update_moving_indicator(self):
        """Status-bar indicator so a rotation move (Reset angle, manual Move
        Tilt/Rot, or an auto-scan step) is visible from any tab -- otherwise
        only the changing angle number hints that something is happening."""
        if not hasattr(self, 'moving_indicator_var') or not hasattr(self, 'rot_mgr'):
            return
        moving = getattr(self.rot_mgr, 'is_moving', {})
        names = []
        if moving.get(2):
            names.append(getattr(self.auto_ui, 'sn2_val', 'SN2'))
        if moving.get(3):
            names.append(getattr(self.auto_ui, 'sn3_val', 'SN3'))

        def _apply():
            if getattr(self, '_shutting_down', False):
                return
            try:
                self.moving_indicator_var.set(f"⏳ MOVING: {', '.join(names)}" if names else "")
            except tk.TclError:
                pass
        self.master.after(0, _apply)

    # ══════════════════════════════════════════════════════════════════
    # Update & Restart — detect source-code changes and re-exec in place
    # ══════════════════════════════════════════════════════════════════
    def _watched_source_files(self):
        """App source files whose change should raise the update badge.
        Only *.py — config3.h / buttons.json / logs change during normal
        operation and must NOT count as a code update."""
        files = glob.glob(os.path.join(self.base_dir, "*.py"))
        files += glob.glob(os.path.join(self.base_dir, "managers", "*.py"))
        return files

    def _version_string(self):
        """Short version tag for the title bar: git revision + newest source
        mtime. Lets remote operators see at a glance whether their window is
        running the latest code."""
        rev = ""
        try:
            r = subprocess.run(['git', '-C', self.base_dir, 'rev-parse', '--short', 'HEAD'],
                               capture_output=True, text=True, timeout=2)
            rev = r.stdout.strip()
        except Exception:
            pass
        ts = ""
        try:
            newest = max(os.path.getmtime(p) for p in self._watched_source_files())
            ts = datetime.fromtimestamp(newest).strftime('%b %d %H:%M')
        except Exception:
            pass
        if rev and ts:
            return f"rev {rev} ({ts})"
        return rev or ts or "dev"

    def _check_for_code_updates(self):
        """Periodic watcher (status-bar badge). 5 s debounce so we never react
        to a file an editor is still writing."""
        if getattr(self, '_shutting_down', False):
            return
        try:
            newest = 0.0
            for p in self._watched_source_files():
                try:
                    newest = max(newest, os.path.getmtime(p))
                except OSError:
                    pass
            if (not self._update_available and newest > self._app_code_baseline
                    and (time.time() - newest) >= 5.0):
                self._update_available = True
                self.update_badge_var.set("🔄 Update available — click to restart")
                self._log("[INFO] Source update detected on disk. "
                          "Click the status-bar badge to restart and apply it.")
            if self._update_available and self._restart_when_idle and not self._is_busy():
                self._log("[INFO] System is idle — applying the queued update restart.")
                self._restart_app()
                return
        except Exception:
            pass
        interval = 15000 if self._restart_when_idle else 60000
        self.master.after(interval, self._check_for_code_updates)

    def _busy_reason(self):
        """Return a short human-readable reason a restart would interrupt real
        work, or None if the system is idle. Reasons: an auto scan, a running
        console job (DAQ/Produce/Analysis/...), a motor move, or a live
        execute_DAQ_v2 acquisition process."""
        if getattr(getattr(self, 'auto_mgr', None), 'is_running', False):
            return "an automated scan is running"
        for slot, proc in getattr(self, '_console_procs', {}).items():
            if proc is not None and proc.poll() is None:
                return f"a console job is running ({slot})"
        if any(getattr(getattr(self, 'rot_mgr', None), 'is_moving', {}).values()):
            return "a motor is moving"
        try:
            # Match only the live acquisition BINARY (execute_DAQ_v2), not a
            # bash/gnome-terminal wrapper that merely mentions it in its argv —
            # a wrapper left open on a `read -p` prompt would otherwise make the
            # app look permanently busy and silently block every restart.
            check = subprocess.run(
                'pgrep -x execute_DAQ_v2 | xargs -r ps -o pid=,args= -p 2>/dev/null | grep -v -- "-j"',
                shell=True, capture_output=True)
            if check.returncode == 0 and check.stdout.strip():
                return "a DAQ acquisition (execute_DAQ_v2) is running"
        except Exception:
            pass
        return None

    def _is_busy(self):
        return self._busy_reason() is not None

    def _syntax_check_sources(self):
        """Parse-check every watched file. Returns an error string, or None if
        clean. Guards against restarting into syntactically broken code (the
        app would die on startup and not come back)."""
        import ast
        for p in self._watched_source_files():
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    ast.parse(f.read(), filename=p)
            except SyntaxError as e:
                return f"{os.path.basename(p)}:{e.lineno}: {e.msg}"
            except Exception as e:
                return f"{os.path.basename(p)}: {e}"
        return None

    def _on_update_badge_click(self):
        if not self._update_available:
            self._log("[INFO] Update badge clicked but no update is pending.")
            return
        self._log("[INFO] Update badge clicked — checking whether a restart can proceed.")
        err = self._syntax_check_sources()
        if err:
            messagebox.showerror(
                "Update Error",
                "The updated code has a syntax error — staying on the current version.\n\n"
                f"{err}")
            return
        reason = self._busy_reason()
        if reason:
            self._log(f"[INFO] Restart blocked: {reason}.")
            choice = messagebox.askyesnocancel(
                "Update Pending",
                f"The system looks busy: {reason}.\n\n"
                "Yes  — restart automatically as soon as it becomes idle.\n"
                "No   — force restart RIGHT NOW anyway.\n"
                "Cancel — do nothing.\n\n"
                "(If you believe this 'busy' status is stale, choose No to force it.)")
            if choice is True:
                self._restart_when_idle = True
                self.update_badge_var.set("🔄 Update pending — restarts when idle")
                self._log("[INFO] Update restart queued — will apply when the system is idle.")
            elif choice is False:
                self._log("[WARNING] Operator forced a restart while system reported busy.")
                self._restart_app()
            return
        if messagebox.askokcancel(
                "Restart",
                "Restart now to apply the update?\n\n"
                "• Unsaved Quick Setup edits will be lost.\n"
                "• Hardware state (motors, laser TEC/LD, UPS) is NOT touched."):
            self._restart_app()

    def _restart_app(self):
        """Re-exec this process in place (self-update restart). Hardware is
        deliberately left untouched — no motor commands, laser TEC/LD state
        kept, tmux sessions preserved. Only OS-level handles (serial / HID)
        are released so the new process can reopen them."""
        try:
            self._restart_app_impl()
        except Exception as e:
            # Every risky step inside _restart_app_impl already has its own
            # try/except, but if something UNEXPECTED still escapes, Tkinter's
            # default callback-exception handler would otherwise just log it
            # to stderr and keep the mainloop running -- the app silently
            # stays alive with no restart and no visible error, which read as
            # "the restart button does nothing." Surface it instead.
            self._log(f"[ERROR] Restart failed unexpectedly: {e}")
            try:
                messagebox.showerror("Restart Failed",
                                     f"Restart could not complete:\n{e}\n\n"
                                     "The app is still running on the OLD code. "
                                     "Check Live Scan Logs / logs/restart.log for details.")
            except Exception:
                pass

    def _restart_app_impl(self):
        self._log(f"[INFO] Restarting to apply update (was {self._version_string()}).")
        self._shutting_down = True

        # Snapshot + save "currently connected" laser wavelengths BEFORE
        # disconnecting them below. save_app_config() computes that list from
        # each instance's LIVE is_connected() state -- calling it AFTER the
        # disconnect loop (the previous order) meant every laser had already
        # been marked disconnected, so last_connected_wls was saved as an
        # empty list on EVERY restart, and auto_connect_laser() then bailed
        # out immediately on `if not last_wls: return` post-restart -- the
        # post-restart laser reconnect could never actually fire, regardless
        # of the APP_RESTART_AUTO_RECONNECT flag below.
        try:
            self.save_app_config()
        except Exception:
            pass

        try:
            if hasattr(self, 'ups_mgr') and self.ups_mgr.ups_serial and self.ups_mgr.ups_serial.is_open:
                self.ups_mgr.ups_serial.close()
        except Exception:
            pass
        if hasattr(self, 'laser_mgr'):
            for wl, inst in self.laser_mgr.laser_instances.items():
                try:
                    if inst.is_connected():
                        inst.disconnect()   # frees the HID handle only; LD/TEC on the unit unchanged
                except Exception:
                    pass
        try:
            self.master.destroy()
        except Exception:
            pass
        # Tell the re-exec'd process to skip the "restore last connections?"
        # confirmation dialog and reconnect the lasers immediately — this is a
        # self-triggered restart the user already approved, not a fresh manual
        # launch, and the lasers were connected a second ago. Without this,
        # auto_connect_laser() waits on a modal Yes/No dialog after every
        # Update & Restart and the lasers are left disconnected until someone
        # notices and clicks it.
        os.environ["APP_RESTART_AUTO_RECONNECT"] = "1"

        # Preserve HOW the app was launched (production main.py vs the TEST-MODE
        # main_test.py shim) instead of hardcoding main.py, so a restart keeps
        # the same mode. Fall back to main.py if argv[0] isn't a usable script.
        entry = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0].endswith(".py") \
            and os.path.exists(os.path.abspath(sys.argv[0])) else os.path.join(self.base_dir, "main.py")

        # Robust restart: do NOT rely on os.execv (it silently fails on some
        # remote/venv/threaded setups, and once master.destroy() has run the
        # app would just close without coming back — the reported symptom).
        # Instead spawn a DETACHED watcher that waits for THIS process to fully
        # exit, then launches a fresh app. No execv quirks, and the two never
        # run at once. A breadcrumb file records that a restart was attempted.
        import shlex
        pid = os.getpid()
        cmd = " ".join(shlex.quote(a) for a in ([sys.executable, entry] + sys.argv[1:]))
        watcher = f'while kill -0 {pid} 2>/dev/null; do sleep 0.3; done; cd {shlex.quote(self.base_dir)}; exec {cmd}'
        try:
            with open(os.path.join(self.base_dir, "logs", "restart.log"), "a") as f:
                f.write(f"[{datetime.now()}] restart -> {cmd}\n")
        except Exception:
            pass
        try:
            subprocess.Popen(["bash", "-c", watcher], cwd=self.base_dir,
                             start_new_session=True, env=os.environ.copy())
        except Exception as e:
            print(f"[ERROR] Failed to spawn relauncher: {e}")
            # Last resort: try in-place re-exec so the user isn't left with a
            # dead window.
            try:
                os.execv(sys.executable, [sys.executable, entry] + sys.argv[1:])
            except Exception:
                pass

        try:
            self.master.destroy()
        except Exception:
            pass
        os._exit(0)

    def _ui_prefs_path(self):
        return os.path.join(self.base_dir, "ui_prefs.json")

    def _load_ui_prefs(self):
        """Small standalone JSON for cosmetic UI prefs (font scale, ...),
        separate from the main app config so a bad value here can never
        corrupt hardware/run settings."""
        try:
            with open(self._ui_prefs_path(), "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_ui_prefs(self, **kv):
        prefs = self._load_ui_prefs()
        prefs.update(kv)
        try:
            with open(self._ui_prefs_path(), "w", encoding="utf-8") as f:
                json.dump(prefs, f, indent=2)
            self.ui_prefs = prefs
        except Exception as e:
            self._log(f"[WARNING] Failed to save UI prefs: {e}")

    def _setup_theme(self):
        """One-shot global ttk restyle. Switches from the default theme's
        chunky 3D-beveled borders to a flat, single-surface look with thin
        hairline borders and cleaner notebook tabs. Only touches ttk widgets
        (frames/labels/tabs/entries/treeview) -- the tk.Button action colors
        are owned by AutomationUI.PALETTE and are unaffected. Wrapped in
        try/except so an unsupported style option can never block startup;
        worst case the app falls back to the old look.

        To revert: delete this method and its call in __init__ (a full-file
        backup was also saved under DAQ_Control_SW/_ui_backup_*/)."""
        try:
            BG      = "#eef0f3"   # single neutral surface for all chrome
            CARD    = "#f7f8fa"   # very slightly lighter, for input fields
            WHITE   = "#ffffff"
            TEXT    = "#1f2430"
            MUTED   = "#5f6672"
            ACCENT  = "#007ACC"
            BORDER  = "#d3d7dd"
            SEL_TAB = "#ffffff"
            IDLE_TAB= "#e2e5ea"

            # NOTE: global Tk 'scaling'-based text enlargement was REMOVED. It
            # is the only lever that resizes the app's hardcoded point fonts,
            # but it also inflates the embedded matplotlib canvases (Laser/UPS
            # graphs) and clipped them off their panes -- and it left some
            # fixed-pixel text unscaled. Not worth the breakage; the app runs
            # at native scale. (View -> Text Size was removed accordingly.)

            style = ttk.Style()
            try:
                style.theme_use("clam")   # flat, no 3D bevels; the big visual win
            except tk.TclError:
                pass

            # Base: every ttk widget inherits these unless overridden.
            style.configure(".", background=BG, foreground=TEXT,
                            fieldbackground=WHITE, bordercolor=BORDER,
                            font=("Helvetica", 10))

            style.configure("TFrame", background=BG)
            style.configure("TLabel", background=BG, foreground=TEXT)
            style.configure("TCheckbutton", background=BG, foreground=TEXT)
            style.configure("TRadiobutton", background=BG, foreground=TEXT)
            style.configure("TSeparator", background=BORDER)

            # LabelFrame: thin solid hairline instead of the default ridge,
            # accent-colored title.
            style.configure("TLabelframe", background=BG, bordercolor=BORDER,
                            relief="solid", borderwidth=1)
            style.configure("TLabelframe.Label", background=BG, foreground=ACCENT,
                            font=("Helvetica", 10, "bold"))

            # Entries / combobox / spinbox: white field, thin border.
            for w in ("TEntry", "TCombobox", "TSpinbox"):
                style.configure(w, fieldbackground=WHITE, background=WHITE,
                                bordercolor=BORDER, foreground=TEXT)

            # Treeview (Scan History, etc.): white rows, subtle header.
            style.configure("Treeview", background=WHITE, fieldbackground=WHITE,
                            foreground=TEXT, bordercolor=BORDER)
            style.configure("Treeview.Heading", background=IDLE_TAB,
                            foreground=TEXT, font=("Helvetica", 10, "bold"))

            # Generic ttk.Button (not the colored tk.Button actions): a
            # visible hairline border + a background a shade darker than the
            # panel so the button edge actually reads against it (the earlier
            # near-panel-colored fill made borders vanish).
            style.configure("TButton", background="#e4e7ec", foreground=TEXT,
                            bordercolor="#b9bec7", lightcolor="#e4e7ec",
                            darkcolor="#b9bec7", relief="solid", borderwidth=1,
                            padding=7, focusthickness=0)
            style.map("TButton",
                      background=[("active", "#d5d9e0"), ("pressed", "#c7ccd4")],
                      bordercolor=[("active", "#9aa0ab")])

            # Notebook: clean tabs, selected tab pops white with accent text.
            style.configure("TNotebook", background=BG, borderwidth=0)
            style.configure("TNotebook.Tab", padding=(14, 7),
                            background=IDLE_TAB, foreground=MUTED,
                            font=("Helvetica", 10))
            style.map("TNotebook.Tab",
                      background=[("selected", SEL_TAB)],
                      foreground=[("selected", ACCENT)],
                      expand=[("selected", (1, 1, 1, 0))])

            # Scrollbars: slimmer, neutral.
            for w in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
                style.configure(w, background=IDLE_TAB, troughcolor=BG,
                                bordercolor=BG, arrowcolor=MUTED)

            self.master.configure(bg=BG)
        except Exception as e:
            # Never let a cosmetic setup crash the app.
            print(f"[WARNING] Theme setup skipped: {e}")

    def request_control_unlock(self):
        """비밀번호 확인 후 제어권 활성화 및 자동화 UI 연동"""
        if self.access_mgr.request_unlock():
            self.ui.refresh_ui_state()
            if hasattr(self, 'auto_ui'):
                self.auto_ui.update_unlock_ui(self.access_mgr.unlocked)
            
    def refresh_ui_state(self):
        """제어권 상태에 따라 UI 버튼들의 활성/비활성 상태를 업데이트"""
        state = tk.NORMAL if self.control_unlocked else tk.DISABLED
        # UIManager를 통해 각 버튼의 state를 일괄 변경하는 로직 필요
        # 예: self.ui.btn_laser_connect.config(state=state)
        pass

    def load_contacts(self):
        """contacts.json 파일에서 연락망을 불러옵니다."""
        self.contacts_file = os.path.join(self.base_dir, "contacts.json")
        if os.path.exists(self.contacts_file):
            try:
                with open(self.contacts_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                self._log(f"Error loading contacts: {e}")
        return [] 

    def update_plots_theme(self, is_dark):
        """멀티 탭 구조의 모든 그래프 테마를 일괄 변경합니다."""
        bg_color = "#2d2d2d" if is_dark else "white"
        fg_color = "white" if is_dark else "black"
        grid_color = "#444444" if is_dark else "#dddddd"

        # [1] 관리할 그래프와 캔버스를 담을 바구니 생성
        figs_to_style = []
        canvases_to_draw = []

        # [2] 공통 그래프 (UPS, Hist) 추가
        if hasattr(self.ui, 'fig_ups'):
            figs_to_style.append(self.ui.fig_ups)
            canvases_to_draw.append(self.ui.canvas_ups)
        if hasattr(self.ui, 'fig_hist'):
            figs_to_style.append(self.ui.fig_hist)
            canvases_to_draw.append(self.ui.canvas_hist)

        # [3] 4개 파장 탭의 모든 그래프 수집
        if hasattr(self.ui, 'laser_tabs_data'):
            for wl, vars_dict in self.ui.laser_tabs_data.items():
                if "fig" in vars_dict:
                    figs_to_style.append(vars_dict["fig"])
                if "canvas" in vars_dict:
                    canvases_to_draw.append(vars_dict["canvas"])

        # [4] 수집된 모든 그래프에 스타일 적용
        for fig in figs_to_style:
            fig.patch.set_facecolor(bg_color)
            for ax in fig.get_axes():
                ax.set_facecolor(bg_color)
                ax.tick_params(colors=fg_color)
                ax.xaxis.label.set_color(fg_color)
                ax.yaxis.label.set_color(fg_color)
                ax.title.set_color(fg_color)
                for spine in ax.spines.values():
                    spine.set_color(fg_color)
                ax.grid(True, color=grid_color, alpha=0.5)
            
            # [5] 그래프별 맞춤 레이아웃 정렬
            if hasattr(self.ui, 'fig_ups') and fig == self.ui.fig_ups:
                fig.tight_layout(rect=[0, 0, 1, 0.96]) # UPS 전용 여백
            else:
                fig.tight_layout()

        # [6] 수집된 모든 캔버스 새로 그리기
        for canvas in canvases_to_draw:
            canvas.draw()

    def check_dir_size_queue(self):
        try:
            while not self.dir_size_queue.empty():
                display_str = self.dir_size_queue.get_nowait()
                self.ui.update_data_size_display(display_str)
        except queue.Empty:
            pass
        finally:
            # 1초마다 큐를 다시 확인
            self.master.after(1000, self.check_dir_size_queue)

    def _update_status_bar(self):
        """1초마다 현재 시간과 경과 시간을 계산하여 상태 표시줄을 업데이트합니다."""
        if self._shutting_down:
            return

        now = datetime.now()
        current_time_str = now.strftime('%Y-%m-%d %H:%M:%S')
        self.clock_var.set(f"Current time : {current_time_str}")

        elapsed = now - self.start_time
        total_seconds = int(elapsed.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        elapsed_str = f"{hours:02}:{minutes:02}:{seconds:02}"
        self.elapsed_time_var.set(f"Execution time: {elapsed_str}")

        # Heartbeat for scripts/daq_heartbeat_watchdog.sh: this callback only
        # keeps firing while the Tk mainloop is genuinely alive and pumping
        # events, so a stale file here means the GUI process is truly hung
        # (not just a slow subprocess) -- unlike an sd_notify from a background
        # thread, which would keep "looking alive" even with a deadlocked UI.
        try:
            with open(os.path.join(self.base_dir, "logs", "heartbeat.txt"), "w") as f:
                f.write(str(time.time()))
        except Exception:
            pass

        self.master.after(1000, self._update_status_bar)

    def load_app_config(self):
        config_path = None
        
        self.last_connected_wls = []
        self.laser_port_mapping = {
            "375nm": "1-3.4.4:1.0", "405nm": "1-3.4.1:1.0",
            "450nm": "1-3.4.2:1.0", "473nm": "1-3.4.3:1.0"
        }
        self.laser_log_dir = "/home/precalkor/ADC/ADC_test/LOG/LASER"
        self.terminal_preference = 'gnome-terminal'
        
        try:
            if os.path.exists(APP_CONFIG_FILE):
                with open(APP_CONFIG_FILE, 'r') as f:
                    data = json.load(f)
                    config_path = data.get("config3h_path")
                    
                    self.terminal_preference = data.get("terminal_preference", self.terminal_preference)
                    self.last_connected_wls = data.get("last_connected_wls", [])
                    self.laser_port_mapping = data.get("laser_port_mapping", self.laser_port_mapping)
                    self.laser_log_dir = data.get("laser_log_dir", self.laser_log_dir)
        except: pass

        if not config_path or not os.path.exists(config_path):
            test_h = "/home/precalkor/Integrated_Control_SW/DAQ_Control_SW/config_test.h"
            std_h = "/home/precalkor/Integrated_Control_SW/DAQ_Control_SW/config3.h"
            old_h = "/home/precalkor/Integrated_Control_SW/DAQ_Control_SW/config2.h" 
            config_path = test_h if os.path.exists(test_h) else std_h if os.path.exists(std_h) else old_h if os.path.exists(old_h) else None

        if config_path and os.path.exists(config_path):
            self.config_manager = ConfigManager(config_path)
            self.save_app_config()
            self._log(f"[INFO] Config loaded: {os.path.basename(config_path)}")
        else:
            self.select_and_set_config_path(initial_setup=True)

    def save_app_config(self):
        if not self.config_manager: return
        try:
            connected_list = []
            if hasattr(self, 'laser_mgr'):
                connected_list = [wl for wl, inst in self.laser_mgr.laser_instances.items() if inst.is_connected()]
            
            with open(APP_CONFIG_FILE, 'w') as f:
                config = {
                    "config3h_path": self.config_manager.filepath,
                    "terminal_preference": getattr(self, "terminal_preference", "gnome-terminal"),
                    "last_connected_wls": connected_list,
                    "laser_port_mapping": getattr(self, "laser_port_mapping", {}),
                    "laser_log_dir": getattr(self, "laser_log_dir", "/home/precalkor/ADC/ADC_test/LOG/LASER")
                }
                json.dump(config, f, indent=4)
        except Exception as e:
            print(f"Error saving config: {e}")


    def select_and_set_config_path(self, initial_setup=False):
        filepath = filedialog.askopenfilename(
                title="Select config3.h file",
                filetypes=(("Header files", "*.h"), ("All files", "*.*"))
                )
        if filepath:
            self.config_manager = ConfigManager(filepath)
            self.save_app_config()
            if not initial_setup:
                self.refresh_all_data()
        elif initial_setup and not self.config_manager:
            messagebox.showerror("Error", "config3.h path is required to run the application.")
            self.master.quit()

    def validate_config_paths(self):
        """config3.h에 명시된 주요 경로들이 유효한지 검사합니다."""
        if not self.config_manager: return

        paths_to_check = ['BasePath', 'RawDataPath', 'ProcessedDataPath', 'ImagePath']
        missing_paths = []

        for path_key in paths_to_check:
            path_val = self.config_manager.get_config_value(path_key)
            if not path_val or not os.path.isdir(path_val):
                missing_paths.append(path_key)

        if missing_paths:
            messagebox.showwarning("Configuration Warning",
                                   f"The following paths defined in your config file are missing or invalid:\n\n"
                                   f"{', '.join(missing_paths)}\n\n"
                                   "Please check your config3.h file.")


    def set_terminal_preference(self, terminal_name):
        """터미널 선택을 저장하는 함수"""
        self.terminal_preference = terminal_name
        self.save_app_config()
        messagebox.showinfo("Terminal Changed", f"Terminal has been set to: {terminal_name}")

    def _log(self, message):
        """메시지를 로그 파일에 저장하고 UI를 업데이트합니다."""
        try:
            log_dir = os.path.join(self.base_dir, "logs")
            os.makedirs(log_dir, exist_ok=True)
            log_file = os.path.join(log_dir, f"log_{datetime.now().strftime('%Y-%m-%d')}.txt")

            log_entry = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n"

            with open(log_file, 'a') as f:
                f.write(log_entry)

            if hasattr(self, 'ui'):
                self.refresh_log_view()
        except Exception as e:
            print(f"Error while logging: {e}")

    def refresh_log_view(self):
        """가장 최신 로그 파일 내용을 읽어 UI에 표시합니다."""
        log_dir = os.path.join(self.base_dir, "logs")
        try:
            if not os.path.isdir(log_dir):
                self.ui.update_log_view("No logs found.")
                return

            log_files = [f for f in os.listdir(log_dir) if f.endswith('.txt')]
            if not log_files:
                self.ui.update_log_view("No logs found.")
                return

            latest_log_file = max(log_files, key=lambda f: os.path.getmtime(os.path.join(log_dir, f)))
            with open(os.path.join(log_dir, latest_log_file), 'r') as f:
                self.ui.update_log_view(f.read())
        except Exception as e:
            self.ui.update_log_view(f"Error reading log file: {e}")

    def _execute_in_new_terminal(self, command, auto_close=False, env=None):
        command_str_for_log = ' '.join(command)
        self._log(f"[INFO] Executing command via '{self.terminal_preference}': {command_str_for_log}")

        try:
            if self.terminal_preference == 'xterm':
                if auto_close:
                    term_command_str = f"{' '.join(command)}"
                    term_command = ['xterm', '-e', 'bash', '-c', term_command_str]
                else:
                    term_command_str = f"{' '.join(command)}; echo; read -p 'Execution finished. Press Enter to close this terminal...'"
                    term_command = ['xterm', '-hold', '-e', 'bash', '-c', term_command_str]
                subprocess.Popen(term_command, env=env, start_new_session=True)
            else:
                if auto_close:
                    term_command_str = f"{' '.join(command)}"
                else:
                    term_command_str = f"{' '.join(command)}; echo; read -p 'Execution finished. Press Enter to close this terminal...'"

                term_command = ['gnome-terminal', '--', 'bash', '-c', term_command_str]
                subprocess.Popen(term_command, env=env, start_new_session=True)

        except FileNotFoundError:
            error_msg = f"'{self.terminal_preference}' not found. Please install it or select another terminal from the File menu."
            self._log(f"ERROR: {error_msg}")
            messagebox.showerror("Error", error_msg)
        except Exception as e:
            self._log(f"ERROR: Failed to open terminal: {e}")
            messagebox.showerror("Error", f"Failed to open terminal: {e}")

    def _ask_run_settings(self, title, fields):
        """Show a non-persistent settings dialog before launching a ROOT macro.

        fields: list of (key, label, default, hint) tuples.
        Returns dict {key: str_value} on OK, or None if the user cancelled.
        Settings are NEVER saved — dialog always opens with the supplied defaults.
        """
        result = {}
        cancelled = [False]

        if not CTK_AVAILABLE:
            messagebox.showerror("Missing dependency",
                                 "customtkinter is not installed.\n\nRun:  pip install customtkinter")
            return None
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")

        dlg = ctk.CTkToplevel(self.master)
        dlg.title(title)
        dlg.resizable(True, True)   # False,False also strips the min/max window buttons on this WM
        dlg.grab_set()

        ctk.CTkLabel(dlg, text=title,
                     font=ctk.CTkFont(size=18, weight="bold")).pack(anchor="w", padx=22, pady=(20, 8))

        body = ctk.CTkFrame(dlg, corner_radius=14)
        body.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 12))

        vars_ = {}
        for i, (key, label, default, hint) in enumerate(fields):
            ctk.CTkLabel(body, text=label, anchor="e",
                         font=ctk.CTkFont(size=13)).grid(row=i, column=0, sticky="e", padx=(16, 8), pady=8)
            v = tk.StringVar(value=str(default))
            ctk.CTkEntry(body, textvariable=v, width=120, justify="center").grid(
                row=i, column=1, sticky="w", pady=8)
            if hint:
                ctk.CTkLabel(body, text=hint, text_color="#8a9099",
                             font=ctk.CTkFont(size=11)).grid(
                    row=i, column=2, sticky="w", padx=(8, 16), pady=8)
            vars_[key] = v

        ctk.CTkLabel(dlg, text="↩ Always resets to default on next run",
                     text_color="#6c757d", font=ctk.CTkFont(size=11, slant="italic")).pack(anchor="w", padx=22)

        def on_ok():
            for k, v in vars_.items():
                result[k] = v.get()
            dlg.destroy()

        def on_cancel():
            cancelled[0] = True
            dlg.destroy()

        btn_row = ctk.CTkFrame(dlg, fg_color="transparent")
        btn_row.pack(anchor="e", padx=20, pady=(8, 18))
        ctk.CTkButton(btn_row, text="Cancel", width=90, fg_color="transparent",
                      border_width=1, text_color=("#1f2430", "#e5e5e5"),
                      command=on_cancel).pack(side=tk.LEFT, padx=(0, 8))
        ctk.CTkButton(btn_row, text="▶ Run", width=120, fg_color="#2e9e4f",
                      hover_color="#268043", command=on_ok).pack(side=tk.LEFT)

        dlg.protocol("WM_DELETE_WINDOW", on_cancel)
        dlg.wait_window()
        return None if cancelled[0] else result

    _EVT_LINE_RE = re.compile(r'^\[Evt:\s*\d+\]')

    def _throttle_evt_lines(self, chunk, slot):
        """Collapse execute_DAQ_v2's per-1000-event progress spam ("[Evt: N]
        Pedestal -> ...") to one line every ~2s per slot, instead of forwarding
        every line to the console widget -- a 300000-event point prints ~300
        of these, and a multi-point General Scan buries the messages that
        actually matter (warnings, point-complete, errors) under thousands of
        near-identical lines. Full text still reaches the on-disk taking log
        via script_v7.sh's own tee; this only thins what the UI widget shows.
        Any line that doesn't match the pattern is always kept as-is.

        Chunks are 120ms buffer flushes, not necessarily line-aligned, so a
        trailing partial line (no \\n yet) is held over and prepended to the
        next chunk rather than filtered as if it were a complete line."""
        if not hasattr(self, '_evt_throttle_state'):
            self._evt_throttle_state = {}   # slot -> {"pending": str, "last_kept": float}
        st = self._evt_throttle_state.setdefault(slot, {"pending": "", "last_kept": 0.0})

        text = st["pending"] + chunk
        # Keep any trailing partial line (after the last \n) for next time.
        if text.endswith('\n'):
            st["pending"] = ""
            body = text
        else:
            last_nl = text.rfind('\n')
            if last_nl == -1:
                st["pending"] = text   # whole chunk is still one partial line
                return ""
            st["pending"] = text[last_nl + 1:]
            body = text[:last_nl + 1]

        now = time.time()
        out_lines = []
        for line in body.splitlines(keepends=True):
            if self._EVT_LINE_RE.match(line):
                if now - st["last_kept"] >= 2.0:
                    st["last_kept"] = now
                    out_lines.append(line)
                # else: dropped -- same-second progress spam
            else:
                out_lines.append(line)
        return "".join(out_lines)

    def _run_job_in_console(self, command, job_name="Job", env=None, slot="analysis", on_complete=None):
        """배치형 외부 작업(DAQ/Produce/Analysis 등)을 별도 터미널 대신 UI Console
        탭에서 실행하고 stdout/stderr 를 실시간으로 스트리밍한다.

        - command : 인자 리스트. 단일 문자열 요소면 그대로 shell 로 실행하고,
                    여러 요소면 공백으로 join 하여 실행한다(_execute_in_new_terminal 과 동일 규칙).
        - slot    : "daq"  → DAQ 수집 스트림 슬롯
                    "analysis" → Produce/Analysis/Contour 슬롯
          슬롯별로 프로세스를 따로 관리하므로 DAQ 수집 중에도 분석을 동시에 돌릴 수 있다.
          단, '같은 슬롯' 안에서는 한 번에 하나만 허용한다(파일/하드웨어 충돌 방지).
        ROOT 인터랙티브 매크로(run_waveform 등 stdin 필요)는 이 경로를 쓰지 말 것.
        """
        import threading

        if not hasattr(self, '_console_procs'):
            self._console_procs = {}

        # 같은 슬롯에 이미 실행 중인 작업이 있으면 거부한다(다른 슬롯은 영향 없음).
        existing = self._console_procs.get(slot)
        if existing is not None and existing.poll() is None:
            busy_name = "DAQ stream" if slot == "daq" else "An analysis job"
            messagebox.showwarning("Console Busy",
                                   f"{busy_name} is already running in this slot.\n"
                                   "Please wait for it to finish or press Stop.")
            return

        # 명령을 bash -c 로 감싼다(파이프/&& 등 셸 문법 허용 + 기존 동작과 호환).
        cmd_str = command[0] if len(command) == 1 else " ".join(command)
        argv = ['bash', '-c', cmd_str]

        # Console 탭 + 해당 슬롯 서브탭을 전면에 띄운다.
        self.ui.focus_console(slot)

        # Store last command so "Open in Terminal" can replay it.
        if not hasattr(self, '_console_last_cmd'):
            self._console_last_cmd = {}
        self._console_last_cmd[slot] = cmd_str

        self.ui.console_set_status(f"▶ Running: {job_name}", slot=slot, state="running")
        self.ui.console_write(
            f"\n===== {job_name} started @ {datetime.now().strftime('%H:%M:%S')} =====\n",
            "info", slot=slot)
        self._log(f"[INFO] Console job '{job_name}' [{slot}]: {cmd_str}")

        try:
            # text=False: read raw bytes so \r from C++ progress lines (e.g. "73%\r")
            # is NOT converted to \n by Python's universal-newlines mode.
            # console_write handles \r explicitly to overwrite the current line.
            proc = subprocess.Popen(
                argv, env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=False, start_new_session=True)
        except Exception as e:
            self.ui.console_write(f"[ERROR] Failed to start job: {e}\n", "err", slot=slot)
            self.ui.console_set_status("✗ Failed to start", slot=slot, state="failed")
            return

        self._console_procs[slot] = proc

        # Per-slot output buffer: reader thread appends decoded strings here;
        # a periodic 120ms timer on the main thread drains them in one batch.
        if not hasattr(self, '_console_buffer'):
            self._console_buffer = {}
        self._console_buffer[slot] = []

        _MAX_CHUNK = 32 * 1024  # bytes per flush — keeps UI responsive

        def flush_buffer():
            if getattr(self, '_shutting_down', False):
                return
            buf = self._console_buffer.get(slot)
            if buf:
                chunk = "".join(buf)
                buf.clear()
                self.ui.console_write(self._throttle_evt_lines(chunk, slot), slot=slot)
            # Reschedule while process is alive
            if self._console_procs.get(slot) is proc and proc.poll() is None:
                self.master.after(120, flush_buffer)

        def reader():
            """Read stdout bytes in background; preserves \\r for progress-line overwrite."""
            try:
                # read1() returns whatever is currently in the OS pipe buffer without
                # blocking for a full line — essential for \\r-terminated progress lines
                # (C++: std::cout << "Processing... 73%\\r" << std::flush).
                while True:
                    chunk = proc.stdout.read1(_MAX_CHUNK)
                    if not chunk:
                        break
                    text = chunk.decode('utf-8', errors='replace')
                    buf = self._console_buffer.get(slot)
                    if buf is not None:
                        buf.append(text)
            except Exception:
                pass
            finally:
                code = proc.wait()
                def done():
                    # Final drain
                    buf = self._console_buffer.get(slot, [])
                    if buf:
                        self.ui.console_write("".join(buf), slot=slot)
                        buf.clear()
                    # Release slot so the user can re-run immediately
                    self._console_procs[slot] = None
                    if hasattr(self, '_evt_throttle_state'):
                        self._evt_throttle_state.pop(slot, None)
                    if code == 0:
                        self.ui.console_write(
                            f"===== {job_name} finished (exit 0) =====\n\n", "ok", slot=slot)
                        self.ui.console_set_status("✓ Done", slot=slot, state="done")
                    else:
                        self.ui.console_write(
                            f"===== {job_name} FAILED (exit {code}) =====\n\n", "err", slot=slot)
                        self.ui.console_set_status(
                            f"✗ Failed (exit {code})", slot=slot, state="failed")
                    # Called on EITHER outcome now (was success-only) -- the HK
                    # scan loop needs to know about a failed exit code too, not
                    # just quietly wait out the blind acq_time as if nothing
                    # happened. Existing callers that only cared about success
                    # (there's only one, DAQ's on_complete=None) are unaffected.
                    if on_complete:
                        on_complete(code)
                if not getattr(self, '_shutting_down', False):
                    self.master.after(0, done)

        threading.Thread(target=reader, daemon=True).start()
        self.master.after(120, flush_buffer)

    def send_console_input(self, slot, text):
        """콘솔에서 실행 중인 job의 stdin으로 한 줄을 보낸다 (Enter 포함).
        DPB Setup처럼 nested ssh가 root 비밀번호 등을 대화형으로 물어보는
        경우, 이 경로가 없으면 프롬프트가 콘솔에 찍혀도 입력할 방법이 없어
        영원히 멈춰있게 된다 -- Console 탭의 입력창이 여기로 연결된다."""
        procs = getattr(self, '_console_procs', {})
        proc = procs.get(slot)
        if proc is None or proc.poll() is not None or proc.stdin is None:
            return False
        try:
            proc.stdin.write((text + "\n").encode('utf-8'))
            proc.stdin.flush()
            return True
        except Exception as e:
            self.ui.console_write(f"\n[INPUT] Failed to send: {e}\n", "err", slot=slot)
            return False

    def stop_console_job(self, slot="analysis"):
        """해당 슬롯에서 실행 중인 콘솔 작업을 프로세스 그룹째 종료한다(Stop 버튼)."""
        procs = getattr(self, '_console_procs', {})
        proc = procs.get(slot)
        if proc is None or proc.poll() is not None:
            self.ui.console_set_status("● Idle", slot=slot, state="idle")
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            self.ui.console_write("\n[STOP] Sent SIGTERM to running job.\n", "err", slot=slot)
            self.ui.console_set_status("⏹ Stopped by user", slot=slot, state="stopped")
        except Exception as e:
            self.ui.console_write(f"\n[STOP] Failed to terminate: {e}\n", "err", slot=slot)

    def handle_button_click(self, command_id):
        if not self.config_manager:
            messagebox.showerror("Error", "Configuration file (config3.h) is not loaded. Please set the path from the 'File' menu.")
            return
        method_to_call = getattr(self, command_id, self.command_not_found)
        method_to_call()

    def handle_mode_change(self):
        """모드 선택에 따른 버튼 활성화 및 탭 상태 유지 로직"""
        category = self.ui.run_mode.get()        # 'auto' or 'manual'
        
        if hasattr(self.ui, 'manual_type_var'):
            manual_sub = self.ui.manual_type_var.get() # 'laser' or 'dark'
        else:
            manual_sub = "laser"
        
        self.update_latest_run_number()

        if category == "auto":
            # 수동 선택 옵션 비활성화
            self.ui.rb_laser.config(state=tk.DISABLED, text="Laser & External trigger (Locked)")
            self.ui.rb_dark.config(state=tk.DISABLED, text="Dark & Self trigger (Locked)")
            self._log("[INFO] Mode: General Scan Active. Manual controls locked.")
        else: # manual
            # 수동 선택 옵션 활성화
            self.ui.rb_laser.config(state=tk.NORMAL, text="Laser & External trigger (0)")
            self.ui.rb_dark.config(state=tk.NORMAL, text="Dark & Self trigger (1)")
            self._log(f"[INFO] Mode: Manual ({manual_sub}) Active.")

        if hasattr(self, 'auto_ui') and hasattr(self.auto_ui, 'tab'):
            self.ui.notebook.tab(self.auto_ui.tab, state="normal")

        if self.access_mgr.unlocked and 'run_daq' in self.ui.buttons:
            if hasattr(self, 'auto_mgr') and self.auto_mgr.is_running:
                self.ui.buttons['run_daq'].config(state=tk.DISABLED, text="2. Run DAQ (Scanning)")
            else:
                self.ui.buttons['run_daq'].config(state=tk.NORMAL, text="2. Run DAQ")

        # Dark mode uses the PMT self-trigger, so the laser must normally be OFF
        # (a running laser would dominate the trigger and ruin the dark-rate scan).
        # Prompt the user when switching into Dark, but let them keep it on if they
        # have a reason to.
        if category == "manual" and manual_sub == "dark":
            self._prompt_laser_off_for_dark()

    def _prompt_laser_off_for_dark(self):
        """When entering Dark (self-trigger) mode, offer to turn the laser(s) off."""
        if not hasattr(self, 'laser_mgr'):
            return
        # Which connected lasers currently have their LD ON?
        lasers_on = []
        for wl, inst in self.laser_mgr.laser_instances.items():
            try:
                if inst.is_connected() and \
                   self.ui.laser_tabs_data[wl]["ld_status"].get() == "ON":
                    lasers_on.append(wl)
            except Exception:
                pass

        if not lasers_on:
            return  # nothing on — nothing to ask

        msg = (f"Dark mode uses the PMT self-trigger.\n\n"
               f"Laser(s) currently ON: {', '.join(str(w) for w in lasers_on)}\n\n"
               f"A running laser will dominate the trigger and spoil the dark-rate "
               f"measurement. Turn the laser(s) OFF now?\n\n"
               f"   • OK  — turn laser(s) OFF (recommended)\n"
               f"   • No  — keep laser(s) ON and continue")
        if messagebox.askyesno("Dark Mode — Laser Off?", msg, icon="warning"):
            for wl in lasers_on:
                try:
                    self.laser_mgr.set_laser_ld_safe(wl, False)
                    self._log(f"[INFO] Dark mode: laser {wl} LD turned OFF.")
                except Exception as e:
                    self._log(f"[WARNING] Could not turn off laser {wl}: {e}")
        else:
            self._log("[INFO] Dark mode: user chose to keep laser(s) ON.")

    def command_not_found(self):
        messagebox.showerror("Error", "Unknown command received from UI.")

    def refresh_all_data(self):
        """Runs the heavy data refresh process in a background thread to prevent UI freezing."""
        if hasattr(self, 'ui'):
            self.ui.show_loading_overlay("🔄 Refreshing Data...")

        def background_task():
            try:
                if self.config_manager:
                    self.config_manager.reload()
                
                new_files = self.get_data_files()
                next_run_num, run_msg = self.get_latest_run_number()
                
                def apply_to_ui():
                    if self.config_manager:
                        self.ui.on_config_loaded()
                        self.ui.run_number_var.set(str(next_run_num))
                        self.ui.set_run_number_status(run_msg)
                        
                        self.ui.all_data_files = new_files
                        self.ui.update_data_viewer(force_refresh=False)
                        self.update_data_directory_size()
                        
                    self.refresh_log_view()
                    
                    if hasattr(self, 'ui'):
                        self.ui.hide_loading_overlay()

                self.master.after(0, apply_to_ui)

            except Exception as e:
                # Bind e as a default arg: the except-scope 'e' is gone by the time
                # this deferred lambda runs, which would raise NameError otherwise.
                self.master.after(0, lambda e=e: self._log(f"[ERROR] Refresh failed: {e}"))
                if hasattr(self, 'ui'):
                    self.master.after(0, self.ui.hide_loading_overlay)

        threading.Thread(target=background_task, daemon=True).start()

    def open_config(self):
        self.ui.open_config_window()

    def open_image_viewer(self):
        self.ui.open_image_viewer()

    def open_data_log(self):
        """Opens the live DAQ Data Monitoring dashboard (serve_data_monitoring.py,
        managed by the daq-data-monitoring systemd --user service) in the
        default browser. LAN first since it works without VPN/Tailscale; falls
        back to the Tailscale address in the log line in case LAN is down."""
        url = "http://192.168.10.100:8090/"
        self._log(f"[INFO] Opening Data Log dashboard: {url}  (Tailscale fallback: http://100.119.212.12:8090/)")
        try:
            webbrowser.open(url)
        except Exception as e:
            self._log(f"[WARNING] Could not open browser automatically: {e}")
            messagebox.showinfo("Data Log", f"Open this URL manually:\n{url}")

    def open_pmt_config_window(self, pmt_name):
        """Config 수정 후 화면상의 Configuration 및 파일 목록 자동 새로고침"""
        if self.config_manager:
            pmt_win = PMTConfigWindow(self.master, self.config_manager, pmt_name)
            self.master.wait_window(pmt_win)
            self.refresh_all_data() 
        else:
            messagebox.showwarning("Warning", "Configuration manager not initialized.")

    def run_cisco(self):
        """찾은 정확한 경로를 포함하여 Cisco vpnui를 실행합니다."""
        self._log("Attempting to launch SUKAP Connection (Cisco)...")
        
        cisco_paths = [
            "/opt/cisco/secureclient/bin/vpnui",
            "/opt/cisco/anyconnect/bin/vpnui",
            "/usr/local/bin/vpnui",
            "vpnui"
        ]
        
        executed = False
        for path in cisco_paths:
            try:
                # 프로그램 실행 시도
                subprocess.Popen([path])
                self._log(f"Cisco launched successfully from: {path}")
                executed = True
                break
            except FileNotFoundError:
                continue
            except Exception as e:
                self._log(f"Error launching {path}: {e}")
                continue
        
        if not executed:
            self._log("ERROR: Cisco vpnui 실행 파일을 찾을 수 없습니다.")
            messagebox.showerror("Execution Error", 
                                 f"Cisco vpnui를 찾을 수 없습니다.\n\n"
                                 f"확인된 경로: /opt/cisco/secureclient/bin/vpnui\n"
                                 f"파일 권한(chmod +x)을 확인해 보세요.")

    def _sync_config_from_active_laser(self):
        """Write the currently-active laser's wavelength + pulse/bias into config3.h
        before a manual run, so it no longer has to be hand-edited each time.

        Only fires when EXACTLY ONE LD is ON (the normal manual state -- the app
        enforces one-LD-at-a-time). With zero or multiple LDs on it logs a warning
        and leaves config3.h alone, so a run never records a wrong/stale setting.
        NOTE is never touched (user-owned); the pulse/bias split stays in the laser
        CSV logs. Reuses AutomationManager._apply_laser_config as the single writer."""
        try:
            if not hasattr(self, 'laser_mgr') or not hasattr(self, 'auto_mgr'):
                return
            on = []
            for wl, inst in self.laser_mgr.laser_instances.items():
                try:
                    if inst and inst.is_connected() and inst.status.get('ld_on', False):
                        on.append(wl)
                except Exception:
                    pass
            if len(on) != 1:
                if on:
                    self._log(f"[WARNING] Laser config not auto-synced: {len(on)} LDs ON "
                              f"({', '.join(str(w) for w in on)}). Edit config3.h manually if needed.")
                return
            wl = on[0]
            vd = self.ui.laser_tabs_data.get(wl)
            if not vd:
                return
            pulse = float(vd["pulse_set"].get())
            bias = float(vd["bias_set"].get())
            self.auto_mgr._apply_laser_config(wl, pulse, bias)
        except Exception as e:
            self._log(f"[WARNING] Laser config auto-sync failed: {e}")

    def run_daq(self, tilt=None, r2=None, r3=None):
        if not getattr(getattr(self, 'access_mgr', None), 'unlocked', True):
            messagebox.showwarning(
                "🔒 System Locked",
                "Controls are locked.\n\nPlease click 'Unlock Controls' (top banner) before running DAQ.")
            return

        category = self.ui.run_mode.get()
        is_auto_running = hasattr(self, 'auto_mgr') and self.auto_mgr.is_running
        is_dummy = hasattr(self, 'auto_ui') and self.auto_ui.dummy_var.get()

        # HK Digitizer backend: auto-scan points go through _execute_hk_point and
        # never reach run_daq, so reaching here in HK mode is a MANUAL trigger.
        # Route it to the HK acquisition (streamed to the HK console) instead of
        # the local CAEN execute_DAQ.
        if getattr(getattr(self, 'auto_mgr', None), 'daq_backend', 'caen') == 'hk':
            if not is_auto_running:
                self.auto_mgr.hk_manual_acquire()
            return

        if not is_auto_running:
            try:
                # Check for a real DAQ acquisition run — exclude the connection-check
                # probe (execute_DAQ_v2 -j) which is transient and harmless to overlap.
                check_running = subprocess.run(
                    'pgrep -f "execute_DAQ" | xargs -r ps -o pid=,args= -p 2>/dev/null | grep -v -- "-j"',
                    shell=True, capture_output=True)
                if check_running.returncode == 0 and check_running.stdout.strip():
                    messagebox.showwarning("DAQ Already Running",
                                           "An instance of 'execute_DAQ' is already running.\nPlease close the current terminal first.")
                    return
            except Exception as e:
                self._log(f"[ERROR] Check process error: {e}")

        daq_path = self._get_daq_path()
        if not daq_path: return

        if category == "manual" and hasattr(self.ui, 'manual_type_var'):
            mode = self.ui.manual_type_var.get()
        elif is_auto_running and hasattr(self, 'auto_ui') and hasattr(self.auto_ui, 'scan_mode_var'):
            # General Scan's own Scan Mode radio (Laser multi-wavelength vs Dark
            # single scan) -- previously this branch always fell through to the
            # "laser" default below, so General Scan could never run Dark mode
            # even when the operator wanted a wavelength-loop-free noise scan.
            mode = self.auto_ui.scan_mode_var.get()
        else:
            mode = "laser"

        # Block Dark DAQ if any laser LD is still ON (manual or auto/General Scan)
        if mode == "dark":
            lasers_on = []
            if hasattr(self, 'laser_mgr'):
                for wl, inst in self.laser_mgr.laser_instances.items():
                    try:
                        if inst.is_connected() and \
                           self.ui.laser_tabs_data[wl]["ld_status"].get() == "ON":
                            lasers_on.append(wl)
                    except Exception:
                        pass
            if lasers_on:
                if messagebox.askyesno(
                        "Laser ON — Dark DAQ Blocked",
                        f"Laser(s) currently ON: {', '.join(str(w) for w in lasers_on)}\n\n"
                        f"Dark mode requires the laser to be OFF.\n\n"
                        f"Turn laser(s) OFF now and continue?",
                        icon="warning"):
                    for wl in lasers_on:
                        try:
                            self.laser_mgr.set_laser_ld_safe(wl, False)
                            self._log(f"[INFO] Dark DAQ: laser {wl} LD turned OFF.")
                        except Exception as e:
                            messagebox.showerror("Error", f"Failed to turn off laser {wl}:\n{e}")
                            return
                else:
                    self._log("[WARNING] Dark DAQ cancelled: laser still ON.")
                    return

        start_block = "0"

        if is_auto_running:
            if is_dummy:
                start_block = "900"
            else:
                if hasattr(self, 'auto_mgr') and hasattr(self.auto_mgr, 'current_scan_block'):
                    start_block = str(self.auto_mgr.current_scan_block)
                else:
                    start_block = "0"
        else:
            if is_dummy:
                start_block = "900"
            elif category == "manual":
                start_block = "700" if mode == "dark" else "800"
            else:
                if hasattr(self, 'auto_mgr') and hasattr(self.auto_mgr, 'current_scan_block'):
                    start_block = str(self.auto_mgr.current_scan_block)
                else:
                    start_block = "0"

        # Manual-mode laser<->config linkage: sync config3.h from the active laser
        # so the wavelength/current never has to be hand-edited before a manual run.
        # Skipped during an automated scan (it runs its own _apply_laser_config per
        # block) and for Dark runs (laser is OFF).
        if not is_auto_running and mode != "dark":
            self._sync_config_from_active_laser()

        script_path = os.path.join(daq_path, 'script_v7.sh')
        config_path = self.config_manager.filepath

        command = [script_path, mode, config_path]

        if tilt is not None and r2 is not None and r3 is not None:
            # --rot2/--tilt2/--rot3/--tilt3 are int options in ADC_test7.cpp
            # (boost::program_options): passing "135.0" aborts the binary with
            # invalid_option_value, so round here exactly like the manual path
            # below does. These stay RAW STAGE angles -- rounding only fixes the
            # text form, never the value.
            ri2, ti2 = int(round(float(r2))), int(round(float(tilt)))
            ri3, ti3 = int(round(float(r3))), int(round(float(tilt)))
            command.extend([str(ri2), str(ti2), str(ri3), str(ti3)])
            self._log(f"[INFO] Injecting live angles to DAQ -> R2:{ri2}, T2:{ti2}, R3:{ri3}, T3:{ti3}")
        else:
            # Manual "Run DAQ" click (no explicit angles) — previously hardcoded
            # "0","0","0","0" here, which meant RunInfo recorded angle=0 regardless
            # of where the PMTs were actually positioned. Read the real hardware
            # angles per-device instead so manual runs get correct metadata too.
            t2 = r2v = t3 = r3v = None
            if hasattr(self, 'rot_mgr'):
                try:
                    t2, r2v = self.rot_mgr.read_angles(2)
                    t3, r3v = self.rot_mgr.read_angles(3)
                except Exception as e:
                    self._log(f"[WARNING] Manual DAQ: could not read live angles: {e}")
            # ADC_test7.cpp's --rot2/--tilt2/... options are ints (boost::program_options);
            # read_angles() returns floats (e.g. 135.0), so round before str() or the
            # DAQ binary aborts with "invalid_option_value" (as seen in practice).
            t2 = int(round(t2)) if t2 is not None else 0
            r2v = int(round(r2v)) if r2v is not None else 0
            t3 = int(round(t3)) if t3 is not None else 0
            r3v = int(round(r3v)) if r3v is not None else 0
            command.extend([str(r2v), str(t2), str(r3v), str(t3)])
            self._log(f"[INFO] Manual DAQ using live angles -> R2:{r2v}, T2:{t2}, R3:{r3v}, T3:{t3}")

        command.append(start_block)

        current_env = os.environ.copy()
        if hasattr(self.ui, 'file_format'):
            current_env["FILE_FORMAT"] = self.ui.file_format.get()
        else:
            current_env["FILE_FORMAT"] = "root"  

        if is_auto_running:
            # Use the date fixed at scan start (set by AutomationManager.start_general_scan),
            # NOT datetime.now(). Otherwise a scan that crosses midnight would switch to the
            # new day's date mid-run, and the run numbering would reset back to the 0-block.
            fixed_date = os.environ.get("SCAN_START_DATE") or datetime.now().strftime("%Y%m%d")
            current_env["SCAN_START_DATE"] = fixed_date
            # Was `tmux send-keys` into a long-lived "GeneralScan" pane: that pane
            # is an OS-level keystroke queue Python has no handle on. If a point's
            # script_v7.sh ever ran long (e.g. a leftover Stability NumSequences
            # loop), the NEXT point's send-keys just piled up behind it instead of
            # replacing it, so the automation's own point loop could race ahead of
            # what was actually executing -- and neither Stop nor even restarting
            # the whole app could reach in and kill it, since the pane survives
            # both (2026-08-15: ~20 stale-angle acquisitions queued up this way,
            # some still firing hours after the scan had "finished").
            # _run_job_in_console launches a real, Python-tracked subprocess.Popen
            # per point instead -- one call, one process, .poll()/.kill()-able,
            # and nothing left running once this point's job ends.
            # Build directly inside the Scan Progress Matrix tab (beside the
            # now-vertical matrix) instead of a separate Output sub-tab, the
            # first time a scan actually runs -- see ensure_console_pane's
            # `parent` param and AutomationUI's matrix-tab layout.
            matrix_frame = getattr(getattr(self, 'auto_ui', None), '_matrix_console_frame', None)
            placeholder = getattr(getattr(self, 'auto_ui', None), '_matrix_console_placeholder', None)
            if placeholder is not None and placeholder.winfo_exists():
                placeholder.destroy()
            self.ui.ensure_console_pane("general_scan", parent=matrix_frame)
            # rotation_manager.py's watchdog advances to the NEXT point as soon
            # as execute_DAQ_v2 itself exits, but the wrapping script_v7.sh
            # keeps running a few seconds longer after that (flag cleanup,
            # launching the background analysis window) before this slot's
            # Popen handle actually finishes. Without waiting for that, the
            # very next point's run_daq call would find the "general_scan"
            # slot still marked busy and _run_job_in_console would silently
            # refuse to launch it (a messagebox from a background thread,
            # easy to miss) -- dropping that point's acquisition entirely.
            prev = getattr(self, '_console_procs', {}).get('general_scan')
            if prev is not None:
                wait_start = time.time()
                while prev.poll() is None and time.time() - wait_start < 10:
                    time.sleep(0.2)
                if prev.poll() is None:
                    self._log("[WARNING] Previous General Scan console job still busy after "
                              "10s wait; launching anyway may be refused.")
            self._run_job_in_console(
                command, job_name="General Scan DAQ", env=current_env,
                slot="general_scan", on_complete=None)
            self._log(f"[INFO] Auto Mode - Fixed Date Injected: {fixed_date}")
        else:
            if category == "manual":
                current_env["SCAN_START_DATE"] = ""
            # [DEVELOP] auto Rate Scan after dark DAQ — disabled pending field validation
            # auto_rate = (category == "manual" and mode == "dark")
            self._run_job_in_console(
                command, job_name="DAQ", env=current_env, slot="daq",
                on_complete=None)
                # on_complete=(lambda: self.run_rate_scan(thr_min=0.5, thr_max=5.0)) if auto_rate else None)

    def run_produce(self):
        selected_files = self.ui.get_selected_file_paths()
        daq_path = self._get_daq_path()
        if not daq_path: return

        helper = os.path.join(self.base_dir, 'run_cpp_script_v2.sh')
        script = os.path.join(daq_path, 'prod_ntp_v7.C') 
        config_path = self.config_manager.filepath
        #mode_int = "0" if self.ui.run_mode.get() == "laser" else "1"

        runs_to_process = [] 

        if selected_files:
            pattern = re.compile(r'(\d+)(?=[^\d]*\.root$)')
            #pattern = re.compile(r'_([0-9]+)\.root$')
            for f_path in selected_files:
                if "raw" in f_path.lower():
                    f_name = os.path.basename(f_path)
                    match = pattern.search(f_name)
                    if match:
                        run_num_str = str(int(match.group(1)))
                        runs_to_process.append((run_num_str, f_path))
                    else:
                        self._log(f"WARNING: Could not extract 4-digit run number from {f_name}. Skipping.")
                else:
                    self._log(f"INFO: Skipping already processed file: {f_path}")
        else:
            run_num = self.ui.get_run_num()
            if not run_num: return
            runs_to_process.append((run_num, "")) 

        if not runs_to_process:
            messagebox.showwarning("No Runs", "No valid RAW files found to process.")
            return

        all_commands_list = []
        for run_num, f_path in runs_to_process:
            f_path_arg = f"\\\"{f_path}\\\"" if f_path else "\"\""
            command_parts = [helper, script, config_path, run_num, f_path_arg]
            all_commands_list.append(" ".join(command_parts))

        final_command_string = " && ".join(all_commands_list)

        # Produce runs in parallel across up to 3 slots so different files can be
        # processed at different times without blocking each other.
        slot = self._acquire_produce_slot()
        if slot is None:
            messagebox.showinfo(
                "Produce Busy",
                "3 Produce jobs are already running concurrently.\n\n"
                "Please wait for one to finish before starting another.")
            return
        self.ui.ensure_console_pane(slot)
        self._run_job_in_console([final_command_string], job_name="Produce", slot=slot)

    def _acquire_parallel_slot(self, slots):
        """Return the first free slot among `slots`, or None if all are busy."""
        if not hasattr(self, '_console_procs'):
            self._console_procs = {}
        for slot in slots:
            proc = self._console_procs.get(slot)
            if proc is None or proc.poll() is not None:
                return slot
        return None

    def _acquire_produce_slot(self):
        """Return the first free produce slot (produce_1/2/3), or None if all 3 are busy."""
        return self._acquire_parallel_slot(
            getattr(self.ui, 'PRODUCE_SLOTS', ("produce_1", "produce_2", "produce_3")))

    def _acquire_analysis_slot(self):
        """Return the first free analysis slot (analysis_1/2/3), or None if all 3 are busy."""
        return self._acquire_parallel_slot(
            getattr(self.ui, 'ANALYSIS_SLOTS', ("analysis_1", "analysis_2", "analysis_3")))

    def _acquire_contour_slot(self):
        """Return the first free contour slot (contour_1/2/3), or None if all 3 are busy."""
        return self._acquire_parallel_slot(
            getattr(self.ui, 'CONTOUR_SLOTS', ("contour_1", "contour_2", "contour_3")))

    def _acquire_uniformity_slot(self):
        """Return the first free uniformity slot (uniformity_1/2/3), or None if all 3 are busy.
        Safe to parallelize: the tag/run-range are passed as plain CLI args, no shared
        config file (unlike Overlay, which stays single-slot below)."""
        return self._acquire_parallel_slot(
            getattr(self.ui, 'UNIFORMITY_SLOTS', ("uniformity_1", "uniformity_2", "uniformity_3")))

    def _ask_rate_scan_range(self):
        """Show a dialog asking for threshold range. Returns (thr_min, thr_max) or None if cancelled."""
        import tkinter as tk
        dialog = tk.Toplevel(self.master)
        dialog.title("Rate Scan Range")
        dialog.resizable(True, True)   # False,False also strips the min/max window buttons on this WM
        dialog.grab_set()

        tk.Label(dialog, text="Threshold scan range [mV]", font=("Helvetica", 11, "bold")).grid(
            row=0, column=0, columnspan=2, padx=20, pady=(15, 5))
        tk.Label(dialog, text="Note: Hardware self-trigger fires at ~3 mV.\n"
                              "Values below that are undersampled.", foreground="gray",
                 font=("Helvetica", 9)).grid(row=1, column=0, columnspan=2, padx=20, pady=(0, 10))

        tk.Label(dialog, text="Min [mV]:").grid(row=2, column=0, sticky="e", padx=10, pady=4)
        var_min = tk.StringVar(value="0.5")
        tk.Entry(dialog, textvariable=var_min, width=8).grid(row=2, column=1, sticky="w", padx=10)

        tk.Label(dialog, text="Max [mV]:").grid(row=3, column=0, sticky="e", padx=10, pady=4)
        var_max = tk.StringVar(value="5.0")
        tk.Entry(dialog, textvariable=var_max, width=8).grid(row=3, column=1, sticky="w", padx=10)

        result = [None]
        def ok():
            try:
                lo = float(var_min.get())
                hi = float(var_max.get())
                if lo >= hi or lo < 0:
                    raise ValueError
                result[0] = (lo, hi)
                dialog.destroy()
            except ValueError:
                tk.messagebox.showerror("Invalid Input", "Enter valid numbers where Min < Max and Min >= 0.", parent=dialog)

        btn_frame = tk.Frame(dialog)
        btn_frame.grid(row=4, column=0, columnspan=2, pady=12)
        tk.Button(btn_frame, text="Run", width=8, command=ok).pack(side=tk.LEFT, padx=6)
        tk.Button(btn_frame, text="Cancel", width=8, command=dialog.destroy).pack(side=tk.LEFT, padx=6)

        dialog.wait_window()
        return result[0]

    def run_rate_scan(self, thr_min=None, thr_max=None):
        """Dark-mode threshold scan (RateScan_v7.C).
        If thr_min/thr_max are None (manual button click), prompts the user for range.
        When called automatically after Dark DAQ, defaults 0.5~5.0 mV are used."""
        auto_mode = (thr_min is not None and thr_max is not None)
        if not auto_mode:
            result = self._ask_rate_scan_range()
            if result is None:
                return
            thr_min, thr_max = result

        selected_files = self.ui.get_selected_file_paths()
        daq_path = self._get_daq_path()
        if not daq_path: return

        helper = os.path.join(self.base_dir, 'run_cpp_script_v2.sh')
        script = os.path.join(daq_path, 'RateScan_v7.C')
        config_path = self.config_manager.filepath

        runs_to_process = []
        if selected_files:
            pattern = re.compile(r'(\d+)(?=[^\d]*\.root$)')
            for f_path in selected_files:
                if "raw" not in f_path.lower():
                    self._log(f"[INFO] Rate Scan needs a RAW file; skipping {os.path.basename(f_path)}.")
                    continue
                m = pattern.search(os.path.basename(f_path))
                if m:
                    runs_to_process.append((str(int(m.group(1))), f_path))
        else:
            run_num = self.ui.get_run_num()
            if not run_num: return
            runs_to_process.append((run_num, ""))

        if not runs_to_process:
            messagebox.showwarning("No Dark Runs",
                                   "Select one or more RAW files from a Dark run,\n"
                                   "or enter a run number, then try again.")
            return

        all_commands_list = []
        for run_num, f_path in runs_to_process:
            f_path_arg = f"\\\"{f_path}\\\"" if f_path else "\"\""
            command_parts = [helper, script, config_path, run_num, f_path_arg,
                             str(thr_min), str(thr_max)]
            all_commands_list.append(" ".join(command_parts))

        final_command_string = " && ".join(all_commands_list)
        self.ui.ensure_console_pane("analysis")
        self._run_job_in_console([final_command_string], job_name="Rate Scan", slot="analysis")

    def run_analysis(self):
        selected_files = self.ui.get_selected_file_paths()
        daq_path = self._get_daq_path()
        if not daq_path: return

        helper = os.path.join(self.base_dir, 'run_cpp_script_v2.sh')
        script = os.path.join(daq_path, 'read_ntp_v7.C')
        config_path = self.config_manager.filepath

        runs_to_process = [] 

        if selected_files:
           #pattern = re.compile(r'_([0-9]+)\.root$')
            pattern = re.compile(r'(\d+)(?=[^\d]*\.root$)')
            for f_path in selected_files:
                f_name = os.path.basename(f_path)
                match = pattern.search(f_name)
                if match:
                    run_num_str = str(int(match.group(1))) 

                    if "production" in f_path.lower() or "prd" in f_name.lower():
                        processed_path = f_path
                    else:
                        # raw를 prd로 치환
                        new_f_name = f_name.replace("raw", "prd") if "raw" in f_name else f"prd_{f_name}"
                        processed_path = os.path.join(self.config_manager.get_config_value("ProcessedDataPath"), new_f_name)

                    runs_to_process.append((run_num_str, processed_path))
                else:
                    self._log(f"[WARNING] Could not extract run number from {f_name}. Skipping.")
        else:
            run_num = self.ui.get_run_num()
            if not run_num: return
            runs_to_process.append((run_num, ""))

        if not runs_to_process:
            messagebox.showwarning("No Runs", "No valid run numbers found to process.")
            return

        all_commands_list = []
        for run_num, f_path in runs_to_process:
            f_path_arg = f"\\\"{f_path}\\\"" if f_path else "\"\""
            command_parts = [helper, script, config_path, run_num, f_path_arg]
            all_commands_list.append(" ".join(command_parts))

        final_command_string = " && ".join(all_commands_list)

        # Analysis runs in parallel across up to 3 slots, like Produce.
        slot = self._acquire_analysis_slot()
        if slot is None:
            messagebox.showinfo(
                "Analysis Busy",
                "3 Analysis jobs are already running concurrently.\n\n"
                "Please wait for one to finish before starting another.")
            return
        self.ui.ensure_console_pane(slot)
        self._run_job_in_console([final_command_string], job_name="Analysis", slot=slot)

    def run_waveform(self):
        """Open the embedded Waveform Inspection panel.

        Priority:
          1. File selected in Data Files tab  → load it directly.
          2. No file selected but run number given → search raw data folder.
          3. Neither → prompt user.
        All axis/threshold settings are controlled inside the panel (no dialog).
        """
        selected_files = self.ui.get_selected_file_paths()

        if len(selected_files) > 1:
            messagebox.showwarning("Multiple Files Selected",
                                   "Select only one file for Waveform Inspection.")
            return

        f_path = ""
        if len(selected_files) == 1:
            f_path = selected_files[0]
        else:
            # Try to locate the raw file from the run number
            run_num = self.ui.get_run_num()
            if run_num and self.config_manager:
                raw_base = self.config_manager.get_config_value("RawDataPath") or ""
                for subdir in ("Laser", "Dark", ""):
                    folder = os.path.join(raw_base, subdir) if subdir else raw_base
                    if not os.path.isdir(folder):
                        continue
                    for fname in os.listdir(folder):
                        if fname.lower().endswith(".root"):
                            m = re.search(r'(\d+)(?=[^\d]*\.root$)', fname)
                            if m and str(int(m.group(1))) == str(int(run_num)):
                                f_path = os.path.join(folder, fname)
                                break
                    if f_path:
                        break

        # Focus the Waveform tab; load the file if found
        self.ui.focus_waveform_tab(f_path if f_path else None)
        if not f_path:
            messagebox.showinfo("Waveform Inspection",
                                "No file found. Use '📂 Open file' or '⟳ Use selected file'"
                                " in the Waveform tab to load a RAW .root file.")

    def run_contour(self):
        """
        Waveform 2D (Contour):
        - 0 files selected: Use Run Number text box.
        - 1 or more files selected: Use run numbers AND file paths from all selected files.
        (Updated to match logic of Produce and Analysis)
        """
        selected_files = self.ui.get_selected_file_paths()
        daq_path = self._get_daq_path()
        if not daq_path: return
        
        helper = os.path.join(self.base_dir, 'run_cpp_script_v2.sh')
        script = os.path.join(daq_path, 'Draw_Contour_v3.C')
        config_path = self.config_manager.filepath

        runs_to_process = [] # (run_num_str, file_path) 튜플을 저장

        if selected_files:
            #pattern = re.compile(r'_([0-9]+)\.root$')
            pattern = re.compile(r'(\d+)(?=[^\d]*\.root$)')
            for f_path in selected_files:
                f_name = os.path.basename(f_path)
                match = pattern.search(f_name)
                if match:
                    run_num_str = str(int(match.group(1)))
                    # [중요] Run 번호와 파일 전체 경로를 함께 저장
                    runs_to_process.append((run_num_str, f_path))
                else:
                    self._log(f"WARNING: Could not extract 4-digit run number from {f_name}. Skipping.")
        
        else:
            # Case 2: 파일을 선택하지 않고 텍스트 박스 입력값 사용
            run_num = self.ui.get_run_num()
            if not run_num: return
            # 파일 경로가 없으므로 빈 문자열("") 전달
            runs_to_process.append((run_num, ""))

        if not runs_to_process:
            messagebox.showwarning("No Runs", "No valid run numbers found to process.")
            return
            
        # Ask for axis settings (not persisted — always resets to defaults)
        settings = self._ask_run_settings(
            "Waveform Contour Settings",
            [
                ("y_lo", "Y-axis  –mV (below ped):", "3.0", "mV below pedestal"),
                ("y_hi", "Y-axis  +mV (above ped):", "3.0", "mV above pedestal"),
                ("x_start", "X-axis start:",         "",     "sample # (blank = full range)"),
                ("x_end",   "X-axis end  :",         "",     "sample # (blank = full range)"),
            ])
        if settings is None:
            return  # user cancelled

        y_lo = settings["y_lo"].strip() or "3.0"
        y_hi = settings["y_hi"].strip() or "3.0"
        x_s  = settings["x_start"].strip() or "-1"
        x_e  = settings["x_end"].strip()   or "-1"

        all_commands_list = []
        for run_num, f_path in runs_to_process:
            f_path_arg = f"\\\"{f_path}\\\"" if f_path else "\"\""
            command_parts = [helper, script, config_path, run_num, f_path_arg, y_lo, y_hi, x_s, x_e]
            all_commands_list.append(" ".join(command_parts))

        final_command_string = " && ".join(all_commands_list)

        # Contour runs in parallel across up to 3 slots, like Produce/Analysis.
        slot = self._acquire_contour_slot()
        if slot is None:
            messagebox.showinfo(
                "Contour Busy",
                "3 Contour jobs are already running concurrently.\n\n"
                "Please wait for one to finish before starting another.")
            return
        self.ui.ensure_console_pane(slot)
        self._run_job_in_console([final_command_string], job_name="Contour", slot=slot)

    def run_uniformity(self):
        """Draw_Uniformity_Norm_v7(tag, run_start, run_end): builds per-PMT uniformity summary
        PNGs (Data/image/Uniformity/) from the result files whose name contains <tag>."""
        daq_path = self._get_daq_path()
        if not daq_path:
            return
        script = os.path.join(daq_path, 'Draw_Uniformity_Norm_v7.C')
        if not os.path.exists(script):
            messagebox.showerror("Error", f"Macro not found:\n{script}")
            return

        if not CTK_AVAILABLE:
            messagebox.showerror("Missing dependency",
                                 "customtkinter is not installed.\n\nRun:  pip install customtkinter")
            return
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")

        dlg = ctk.CTkToplevel(self.master)
        dlg.title("Uniformity Analysis")
        dlg.transient(self.master)
        dlg.grab_set()
        dlg.resizable(True, True)   # False,False also strips the min/max window buttons on this WM
        dlg.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(dlg, text="Uniformity Analysis",
                     font=ctk.CTkFont(size=19, weight="bold")).grid(
            row=0, column=0, sticky="w", padx=22, pady=(20, 0))
        ctk.CTkLabel(dlg, justify="left", text_color="#6c757d",
                     font=ctk.CTkFont(size=12),
                     text=("Reads Data/FinalResult/ files whose name contains <tag>, for runs\n"
                           "[start, end]. Saves PNGs to Data/image/Uniformity/. PMT serials /\n"
                           "HV / angles are read automatically from each file.")
                     ).grid(row=1, column=0, sticky="w", padx=22, pady=(2, 12))

        today = datetime.now().strftime("%Y%m%d")
        tag_var = tk.StringVar(value=today)
        start_var = tk.StringVar(value="0")
        end_var = tk.StringVar(value="99")

        # Date tags available in FinalResult/ (so the user can pick instead of typing)
        avail_tags = []
        try:
            rp = self.config_manager.get_config_value("FinalResultPath")
            if rp and os.path.isdir(rp):
                seen = set()
                for fn in os.listdir(rp):
                    m = re.search(r'_(\d{8})_', fn)
                    if m:
                        seen.add(m.group(1))
                avail_tags = sorted(seen, reverse=True)
        except Exception:
            pass

        card = ctk.CTkFrame(dlg, corner_radius=14)
        card.grid(row=2, column=0, sticky="ew", padx=20, pady=(0, 14))
        card.grid_columnconfigure(1, weight=1)

        def _crow(r, label, widget):
            ctk.CTkLabel(card, text=label, anchor="w",
                         font=ctk.CTkFont(size=13)).grid(
                row=r, column=0, sticky="w", padx=(16, 8), pady=10)
            widget.grid(row=r, column=1, sticky="e", padx=(0, 16), pady=10)

        _crow(0, f"Date tag  (e.g. {today})",
              ctk.CTkComboBox(card, variable=tag_var, values=avail_tags, width=160))
        _crow(1, "Run start  (e.g. 0)",
              ctk.CTkEntry(card, textvariable=start_var, width=160, justify="center"))
        _crow(2, "Run end  (e.g. 99)",
              ctk.CTkEntry(card, textvariable=end_var, width=160, justify="center"))

        # Channel selection (which PMTs to draw in the combined plots)
        ch_vars = {0: tk.BooleanVar(value=True),
                   1: tk.BooleanVar(value=True),
                   2: tk.BooleanVar(value=True)}
        ctk.CTkLabel(card, text="Channels", anchor="w",
                     font=ctk.CTkFont(size=13)).grid(row=3, column=0, sticky="w", padx=(16, 8), pady=10)
        ch_frame = ctk.CTkFrame(card, fg_color="transparent")
        ch_frame.grid(row=3, column=1, sticky="e", padx=(0, 16), pady=10)
        for c in (0, 1, 2):
            ctk.CTkCheckBox(ch_frame, text=f"CH{c}", variable=ch_vars[c],
                            width=54).pack(side=tk.LEFT, padx=(0, 6))

        def _go():
            tag = tag_var.get().strip()
            try:
                rs, re_ = int(start_var.get()), int(end_var.get())
            except ValueError:
                messagebox.showerror("Invalid input", "Run start/end must be integers.", parent=dlg)
                return
            if not tag:
                messagebox.showerror("Invalid input", "Date tag is required.", parent=dlg)
                return
            chsel = ",".join(str(c) for c in (0, 1, 2) if ch_vars[c].get())
            if not chsel:
                messagebox.showerror("Invalid input", "Select at least one channel.", parent=dlg)
                return
            slot = self._acquire_uniformity_slot()
            if slot is None:
                messagebox.showinfo(
                    "Uniformity Busy",
                    "3 Uniformity jobs are already running concurrently.\n\n"
                    "Please wait for one to finish before starting another.",
                    parent=dlg)
                return
            dlg.destroy()
            helper = os.path.join(self.base_dir, 'run_cpp_script_v2.sh')
            config_path = self.config_manager.filepath
            tag_arg = f'\\\"{tag}\\\"'
            chsel_arg = f'\\\"{chsel}\\\"'
            cmd = " ".join([helper, script, config_path, tag_arg, str(rs), str(re_), chsel_arg])
            self.ui.ensure_console_pane(slot)
            self._run_job_in_console([cmd], job_name="Uniformity", slot=slot)
            self._log(f"[INFO] Uniformity analysis: tag={tag}, runs {rs}-{re_}")
            messagebox.showinfo("Uniformity Analysis",
                                f"Running for tag={tag}, runs {rs}-{re_}.\n\n"
                                "Progress streams into the Output tab (Uniformity).\n"
                                "PNGs appear in Data/image/Uniformity/ — open the Image Viewer\n"
                                "(then Refresh) once the job shows ✓ Done.")
            self.ui.open_image_viewer()

        btns = ctk.CTkFrame(dlg, fg_color="transparent")
        btns.grid(row=3, column=0, sticky="e", padx=20, pady=(0, 20))
        ctk.CTkButton(btns, text="Cancel", width=90, fg_color="transparent",
                      border_width=1, text_color=("#1f2430", "#e5e5e5"),
                      command=dlg.destroy).pack(side=tk.LEFT, padx=(0, 8))
        ctk.CTkButton(btns, text="Run", width=130, command=_go).pack(side=tk.LEFT)

    def run_overlay(self):
        """Pick which Uniformity datasets to overlay. Writes Data/UNIFORMITY/overlay_tags.txt
        (read by Draw_Overlay_Uniformity_v7.C) and runs the macro."""
        daq_path = self._get_daq_path()
        if not daq_path:
            return
        script = os.path.join(daq_path, 'Draw_Overlay_Uniformity_v7.C')
        if not os.path.exists(script):
            messagebox.showerror("Error", f"Macro not found:\n{script}")
            return

        uni_dir = os.path.join(daq_path, 'Data', 'UNIFORMITY')
        prefix, suffix = 'Graphs_Uniformity_', '.root'
        graphs = sorted(glob.glob(os.path.join(uni_dir, f'{prefix}*{suffix}')))
        tags = [os.path.basename(g)[len(prefix):-len(suffix)] for g in graphs]
        if not tags:
            messagebox.showwarning("No datasets",
                                   f"No {prefix}*{suffix} found in:\n{uni_dir}\n\n"
                                   "Run '7. Uniformity' first to create the per-dataset graph files.")
            return

        if not CTK_AVAILABLE:
            messagebox.showerror("Missing dependency",
                                 "customtkinter is not installed.\n\nRun:  pip install customtkinter")
            return
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")

        dlg = ctk.CTkToplevel(self.master)
        dlg.title("Overlay Uniformity")
        dlg.transient(self.master)
        dlg.grab_set()
        ctk.CTkLabel(dlg, text="Overlay Uniformity",
                     font=ctk.CTkFont(size=19, weight="bold")).pack(anchor="w", padx=22, pady=(20, 0))
        ctk.CTkLabel(dlg, text="Select datasets to overlay. Legend label is optional "
                              "(blank = dataset name).",
                     text_color="#6c757d", font=ctk.CTkFont(size=12)).pack(anchor="w", padx=22, pady=(2, 10))

        # Pre-fill selection from overlay_tags.txt ("tag" or "tag,Label").
        cfg_path = os.path.join(uni_dir, 'overlay_tags.txt')
        preset_selected, preset_label = set(), {}
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path) as f:
                    for ln in f:
                        ln = ln.strip()
                        if ln and not ln.startswith('#'):
                            parts = ln.split(',', 1)
                            tg = parts[0].strip()
                            preset_selected.add(tg)
                            if len(parts) > 1 and parts[1].strip():
                                preset_label[tg] = parts[1].strip()
            except Exception:
                pass

        # Labels persist independently of selection (overlay_tags.txt only ever
        # remembers labels for datasets that were CHECKED the last time you ran
        # Overlay -- an unchecked row's typed label used to vanish on reopen).
        # This file stores every tag's label regardless of checkbox state.
        labels_path = os.path.join(uni_dir, 'overlay_labels.json')
        persisted_labels = {}
        if os.path.exists(labels_path):
            try:
                with open(labels_path) as f:
                    persisted_labels = json.load(f)
            except Exception:
                pass
        for tg, lbl in preset_label.items():
            persisted_labels.setdefault(tg, lbl)
        persisted_labels_loaded = set(persisted_labels.keys())

        # One row per dataset: checkbox to select + entry to set its legend
        # label. CTkScrollableFrame replaces the old Canvas+Scrollbar+inner
        # boilerplate -- it scrolls natively when the list is long.
        scroll = ctk.CTkScrollableFrame(dlg, label_text="Datasets",
                                        height=min(360, max(120, 34 * len(tags))))
        scroll.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 8))

        row_vars = {}   # tag -> (BooleanVar selected, StringVar label)
        for tg in tags:
            row = ctk.CTkFrame(scroll, fg_color="transparent")
            row.pack(fill=tk.X, pady=2)
            sel_var = tk.BooleanVar(value=(tg in preset_selected))
            lbl_var = tk.StringVar(value=persisted_labels.get(tg, ""))
            ctk.CTkCheckBox(row, text="", width=24, variable=sel_var).pack(side=tk.LEFT, padx=(0, 4))
            ctk.CTkLabel(row, text=tg, width=180, anchor="w").pack(side=tk.LEFT)
            ctk.CTkEntry(row, textvariable=lbl_var, width=160,
                         placeholder_text="legend label").pack(side=tk.LEFT, padx=(4, 0))
            row_vars[tg] = (sel_var, lbl_var)

        # Channel selection (which PMTs to overlay)
        ch_vars = {0: tk.BooleanVar(value=True),
                   1: tk.BooleanVar(value=True),
                   2: tk.BooleanVar(value=True)}
        ch_outer = ctk.CTkFrame(dlg, fg_color="transparent")
        ch_outer.pack(padx=20, pady=(0, 4), anchor="w")
        ctk.CTkLabel(ch_outer, text="Channels:", font=ctk.CTkFont(size=13)).pack(side=tk.LEFT, padx=(0, 8))
        for c in (0, 1, 2):
            ctk.CTkCheckBox(ch_outer, text=f"CH{c}", variable=ch_vars[c], width=54).pack(side=tk.LEFT, padx=(0, 6))

        chan_cfg_path = os.path.join(uni_dir, 'overlay_channels.txt')

        def _go():
            sel = [tg for tg in tags if row_vars[tg][0].get()]
            if not sel:
                messagebox.showwarning("No selection", "Select at least one dataset.", parent=dlg)
                return
            chsel = [c for c in (0, 1, 2) if ch_vars[c].get()]
            if not chsel:
                messagebox.showwarning("No channel", "Select at least one channel.", parent=dlg)
                return
            try:
                os.makedirs(uni_dir, exist_ok=True)
                with open(cfg_path, 'w') as f:
                    f.write("# Overlay tag list (edited from the GUI). One 'tag' or 'tag,Label' per line.\n")
                    for tg in sel:
                        label = row_vars[tg][1].get().strip()
                        f.write(f"{tg},{label}\n" if label else f"{tg}\n")
                with open(chan_cfg_path, 'w') as f:
                    f.write(",".join(str(c) for c in chsel) + "\n")
                # Persist every row's label (checked or not) so an unchecked
                # dataset's typed label survives closing/reopening this dialog.
                # Only clear a tag's saved label if it had one loaded at dialog-open
                # time and the user actively blanked it -- an untouched blank field
                # (e.g. a tag whose label was never populated in this session) must
                # not wipe out a label saved by an earlier session.
                for tg in tags:
                    label = row_vars[tg][1].get().strip()
                    had_before = tg in persisted_labels_loaded
                    if label:
                        persisted_labels[tg] = label
                    elif had_before:
                        persisted_labels.pop(tg, None)
                with open(labels_path, 'w') as f:
                    json.dump(persisted_labels, f, indent=2)
            except Exception as e:
                messagebox.showerror("Error", f"Could not write overlay config:\n{e}", parent=dlg)
                return
            dlg.destroy()
            helper = os.path.join(self.base_dir, 'run_cpp_script_v2.sh')
            config_path = self.config_manager.filepath
            cmd = " ".join([helper, script, config_path])
            self.ui.ensure_console_pane("overlay")
            self._run_job_in_console([cmd], job_name="Overlay", slot="overlay")
            self._log(f"[INFO] Overlay Uniformity: {len(sel)} datasets -> {sel}")
            messagebox.showinfo("Overlay Uniformity",
                                f"Overlaying {len(sel)} dataset(s).\n\n"
                                "Progress streams into the Output tab (Overlay).\n"
                                "PNGs appear in Data/image/Uniformity/ — open the Image Viewer\n"
                                "(Refresh) once the job shows ✓ Done.")
            self.ui.open_image_viewer()

        btns = ctk.CTkFrame(dlg, fg_color="transparent")
        btns.pack(anchor="e", padx=20, pady=(4, 18))
        ctk.CTkButton(btns, text="Cancel", width=90, fg_color="transparent",
                      border_width=1, text_color=("#1f2430", "#e5e5e5"),
                      command=dlg.destroy).pack(side=tk.LEFT, padx=(0, 8))
        ctk.CTkButton(btns, text="Overlay", width=130, command=_go).pack(side=tk.LEFT)

    def run_transfer(self):
        daq_path = self._get_daq_path()
        if not daq_path: return
        script_path = os.path.join(daq_path, 'transfer.sh')
        if not os.path.exists(script_path):
            messagebox.showerror("Error", f"Transfer script not found at:\n{script_path}")
            return
        command = [script_path]
        self._execute_in_new_terminal(command)

    def move_data_files(self, file_paths):
        if not file_paths: return

        dest_dir = filedialog.askdirectory(title="Select Destination Folder")
        if not dest_dir: return

        thread = threading.Thread(target=self._perform_rsync_thread, args=(file_paths, dest_dir))
        thread.start()

    def _perform_rsync_thread(self, file_paths, dest_dir):
        moved_count = 0
        failed_files = []
        total_files = len(file_paths)

        # 초기 상태 메시지
        self.master.after(0, lambda: self.ui.data_size_var.set(f"Preparing rsync..."))

        for idx, file_path in enumerate(file_paths):
            filename = os.path.basename(file_path)
            
            # 상태 업데이트 (현재 몇 번째 파일 처리 중인지 표시)
            status_msg = f"Rsync Moving... ({idx+1}/{total_files}): {filename}"
            self.master.after(0, lambda m=status_msg: self.ui.data_size_var.set(m))

            try:
                # [핵심] rsync 명령어 구성
                # -a: 아카이브 모드 (권한, 시간 정보 유지)
                # --remove-source-files: 전송 성공 시 원본 파일 삭제 (Move 효과)
                # --info=progress2: (옵션) 진행률 표시용이나 여기선 로그용
                command = [
                    'rsync', 
                    '-a', 
                    '--remove-source-files', 
                    file_path, 
                    dest_dir
                ]
                
                # rsync 실행 (대용량 파일일수록 여기서 시간이 걸림)
                result = subprocess.run(command, check=True, capture_output=True, text=True)
                
                self._log(f"[RSYNC SUCCESS] {file_path} -> {dest_dir}")
                moved_count += 1
                
            except subprocess.CalledProcessError as e:
                self._log(f"[RSYNC ERROR] File: {filename}\nError: {e.stderr}")
                failed_files.append(filename)
            except Exception as e:
                self._log(f"[PYTHON ERROR] File: {filename}\nError: {e}")
                failed_files.append(filename)

        # 3. 모든 작업 완료 후 처리
        def on_complete():
            self.refresh_all_data() # 목록 새로고침
            self.update_data_directory_size() # 용량 재계산
            self.ui.data_size_var.set(f"Move Complete.") # 상태 메시지 초기화

            if failed_files:
                messagebox.showerror("Rsync Finished with Errors", 
                                     f"Moved {moved_count} files.\nFailed:\n{', '.join(failed_files)}\n\nCheck logs for details.")
            else:
                messagebox.showinfo("Success", f"Successfully moved {moved_count} file(s) using rsync.")

        self.master.after(0, on_complete)

    def delete_data_files(self, file_paths):
        if not file_paths: return

        num_files = len(file_paths)
        file_list_str = "\n".join(f"- {os.path.basename(p)}" for p in file_paths[:5])
        if num_files > 5:
            file_list_str += f"\n...and {num_files - 5} more."

        confirmed = messagebox.askyesno(
            "Confirm Deletion",
            f"Are you sure you want to permanently delete {num_files} selected file(s)?\n\n{file_list_str}\n\nThis action cannot be undone."
        )

        if not confirmed:
            self._log("[INFO] User cancelled file deletion.")
            return

        if hasattr(self, 'ui'):
            self.ui.show_loading_overlay(f"🗑️ Deleting {num_files} file(s)...")

        def delete_task():
            deleted_count = 0
            failed_files = []
            
            for file_path in file_paths:
                try:
                    os.remove(file_path)
                    self._log(f"[INFO] Deleted file: {file_path}")
                    deleted_count += 1
                except Exception as e:
                    self._log(f"[ERROR] Failed to delete {file_path}: {e}")
                    failed_files.append(os.path.basename(file_path))

            def update_ui():
                if hasattr(self, 'ui'):
                    self.ui.hide_loading_overlay()
                
                if deleted_count > 0:
                    self.refresh_all_data() 

                if failed_files:
                    messagebox.showerror("Deletion Error", f"Successfully deleted {deleted_count} file(s), but failed to delete:\n\n{', '.join(failed_files)}")
                elif deleted_count > 0:
                    messagebox.showinfo("Success", f"Successfully deleted {deleted_count} file(s).")

            self.master.after(0, update_ui)

        threading.Thread(target=delete_task, daemon=True).start()

    def _get_daq_path(self):
        if not self.config_manager: return None
        try:
            path = self.config_manager.get_config_value('BasePath')
            if not path:
                path = self.config_manager.get_config_value('DaqProgramPath')
            if not path:
                messagebox.showerror("Error", "BasePath or DaqProgramPath not found in config file.")
                return None
            return path
        except Exception as e:
            messagebox.showerror("Error", f"Failed to read path from config file: {e}")
            return None

    def open_terminal_at_path(self, path):
        try:
            if not os.path.isdir(path):
                path = os.path.dirname(path)
            if not os.path.isdir(path):
                messagebox.showerror("Error", f"Directory does not exist:\n{path}")
                return
            subprocess.Popen(['gnome-terminal', f'--working-directory={path}'])
        except FileNotFoundError:
            messagebox.showerror("Error", "'gnome-terminal' not found.")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to open terminal: {e}")

    def get_data_files(self):
        file_list = []
        if not self.config_manager: return file_list

        try:
            paths_to_scan = [
                    ("Raw", self.config_manager.get_config_value("RawDataPath")),
                    ("Production", self.config_manager.get_config_value("ProcessedDataPath")),
                    ("Result", self.config_manager.get_config_value("FinalResultPath")),
                    ("External Disk", self.config_manager.get_config_value("ExternalPath")),
                    ]

            for file_type, base_path in paths_to_scan:
                if not (base_path and os.path.isdir(base_path)):
                    continue
                dirs_to_check = []
                if file_type == "Raw" :
                    dirs_to_check.append(os.path.join(base_path, 'Dark'))
                    dirs_to_check.append(os.path.join(base_path, 'Laser'))
                elif file_type == "External Disk" :
                    dirs_to_check.append(os.path.join(base_path, 'Dark'))
                    dirs_to_check.append(os.path.join(base_path, 'Laser'))
                else:
                    dirs_to_check.append(base_path)
                for dir_path in dirs_to_check:
                    if os.path.isdir(dir_path):
                        for f in os.listdir(dir_path):
                            #if f.lower().endswith('.root'):
                            if f.lower().endswith('.root') or f.lower().endswith('.csv'):
                                full_path = os.path.join(dir_path, f)
                                mtime = os.path.getmtime(full_path)
                                mtime_str = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S')
                                file_list.append({
                                    "type": file_type, "filename": f, "path": dir_path,
                                    "mtime": mtime_str, "mtime_float": mtime
                                    })

        except Exception as e:
            self._log(f"Error reading data files: {e}")
            messagebox.showerror("File Error", f"Could not read data files from disk.\nCheck permissions or paths.\n\nError: {e}")

        file_list.sort(key=lambda x: x["mtime_float"], reverse=True)
        return file_list

    def open_data_file_viewer(self, file_path):
        if file_path.lower().endswith('.csv'):
            try:
                self._log(f"[INFO] Opening CSV file via Text Editor: {file_path}")
                subprocess.Popen(['gedit', file_path])
            except FileNotFoundError:
                messagebox.showerror("Error", "'gedit' command not found. Please install gedit or change editor command.")
            except Exception as e:
                messagebox.showerror("Error", f"Failed to open file:\n{e}")
            return

        # ROOT file: open an INTERACTIVE ROOT session (with a TBrowser) inside a terminal.
        # A bare subprocess.Popen(['root','-l',file]) has no tty, so ROOT reads EOF and exits
        # immediately — nothing visible. Routing it through a terminal restores the ability
        # to inspect the file structure (TBrowser / .ls / tree->Print()).
        self._log(f"[INFO] Opening ROOT file in a terminal: {file_path}")
        root_cmd = f'root -l "{file_path}" -e "new TBrowser"'
        self._execute_in_new_terminal([root_cmd], auto_close=False)

    def get_latest_run_number(self):
        if not self.config_manager: return (1, "Config not loaded.")
        try:
            cfg = self.config_manager.get_all_variables()
            category = self.ui.run_mode.get() # 'manual' 또는 'general' 등

            is_dummy = hasattr(self, 'auto_ui') and getattr(self.auto_ui.dummy_var, 'get', lambda: False)()
            is_auto_running = hasattr(self, 'auto_mgr') and getattr(self.auto_mgr, 'is_running', False)

            if is_auto_running:
                if is_dummy:
                    start_block = 900
                else:
                    if hasattr(self, 'auto_mgr') and hasattr(self.auto_mgr, 'current_scan_block'):
                        start_block = self.auto_mgr.current_scan_block
                    else:
                        start_block = 0
            else:
                if is_dummy:
                    start_block = 900
                elif category == "manual":
                    manual_mode = self.ui.manual_type_var.get() if hasattr(self.ui, 'manual_type_var') else "dark"
                    start_block = 700 if manual_mode == "dark" else 800
                else:
                    if hasattr(self, 'auto_mgr') and hasattr(self.auto_mgr, 'current_scan_block'):
                        start_block = self.auto_mgr.current_scan_block
                    else:
                        start_block = 0

            upper_bound = start_block + 49

            mode = self.ui.run_mode.get()
            if mode == "dark":
                path_to_scan = os.path.join(cfg.get("RawDataPath", ""), "Dark")
            else:
                path_to_scan = os.path.join(cfg.get("RawDataPath", ""), "Laser")

            if not os.path.isdir(path_to_scan):
                return (start_block, f"Data path not found: {path_to_scan}")

            scan_date = os.environ.get("SCAN_START_DATE", datetime.now().strftime("%Y%m%d"))
            if not scan_date:
                scan_date = datetime.now().strftime("%Y%m%d")
                
            base_prefix = f"precal_raw_kor_run_{scan_date}"

            search_pattern_root = os.path.join(path_to_scan, f"{base_prefix}_*.root")
            search_pattern_csv  = os.path.join(path_to_scan, f"{base_prefix}_*.csv")

            matching_files = glob.glob(search_pattern_root) + glob.glob(search_pattern_csv)
            self._log(f"[INFO] Run checking: Found {len(matching_files)} files matching prefix '{base_prefix}'")

            run_numbers = []
            pattern = re.compile(r'[._]([0-9]+)\.(root|csv)$')
            #pattern = re.compile(r'_([0-9]+)\.(root|csv)$')

            if 'upper_bound' not in locals():
                upper_bound = start_block + 99

            for f_path in matching_files:
                f_name = os.path.basename(f_path)
                match = pattern.search(f_name)
                if match:
                    run_num = int(match.group(1))
                    if start_block <= run_num <= upper_bound:
                        run_numbers.append(run_num)

            if not run_numbers:
                message = f"No runs for this block. Next is #{start_block}."
                return (start_block, message)
            else:
                latest_run = max(run_numbers)
                next_run = latest_run + 1
                message = f"{len(run_numbers)} run(s) found in block {start_block}. Latest is #{latest_run}. Next is #{next_run}."
                return (next_run, message)

        except Exception as e:
            error_msg = f"Error checking run numbers: {e}"
            self._log(f"ERROR: {error_msg}")
            return (800, f"Error checking for previous runs: {e}")


    def update_latest_run_number(self):
        next_run_num, message = self.get_latest_run_number()
        self.ui.run_number_var.set(str(next_run_num))
        self.ui.set_run_number_status(message)

    def get_ip_addresses(self):
        ips = {'local_ip': 'N/A', 'tailscale_ip': 'N/A'}
        try:
            result = subprocess.run(
                    ['tailscale', 'ip', '-4'],
                    capture_output=True, text=True, check=True, timeout=2
                    )
            ips['tailscale_ip'] = result.stdout.strip()
        except Exception: pass
        try:
            result = subprocess.run(
                    "ip route get 1.1.1.1 | awk '{print $7}'",
                    shell=True, capture_output=True, text=True, check=True, timeout=2
                    )
            local_ip = result.stdout.strip()
            if local_ip: ips['local_ip'] = local_ip
        except Exception: pass
        return ips

    def check_daq_connection(self):
        """Starts a single continuous background thread for DAQ status checking."""
        if hasattr(self, '_daq_check_running') and self._daq_check_running:
            return
            
        self._daq_check_running = True
        threading.Thread(target=self._daq_check_loop, daemon=True).start()

    def _daq_check_env(self, daq_path):
        """Environment for running execute_DAQ_v2 -j.

        The binary links the CAEN libs via RUNPATH, but RUNPATH does NOT cover
        transitive deps (libCAENDigitizer → libCAENComm), so the loader falls
        back to the system path where a stale 32-bit libCAENComm.so lives and
        the binary fails to start ("wrong ELF class"). Inject the correct 64-bit
        lib dir (<dirname(BasePath)>/lib) into LD_LIBRARY_PATH so the status
        check works regardless of how the GUI itself was launched.
        """
        env = os.environ.copy()
        extra = []
        try:
            lib_dir = os.path.join(os.path.dirname(daq_path.rstrip('/')), 'lib')
            if os.path.isdir(lib_dir):
                extra.append(lib_dir)
        except Exception:
            pass
        if os.path.isdir('/opt/root/lib'):
            extra.append('/opt/root/lib')
        if extra:
            existing = env.get('LD_LIBRARY_PATH', '')
            env['LD_LIBRARY_PATH'] = ':'.join(extra + ([existing] if existing else []))
        return env

    def _daq_check_loop(self):
        """Continuous background loop for DAQ connection checking with thread-safe compliance."""
        # Loop strictly boundaries on the control boolean variable safety flags
        while getattr(self, '_daq_check_running', False):
            # In HK Digitizer mode the local CAEN box isn't in use (often not
            # even powered), so this probe would otherwise keep spawning
            # execute_DAQ_v2 -j every 2s indefinitely against absent hardware
            # -- each attempt can run up to its 5s timeout, and that sustained
            # background load is what read as "the DAQ app got laggy" while
            # testing HK mode. Skip the CAEN probe entirely while HK is active.
            if getattr(getattr(self, 'auto_mgr', None), 'daq_backend', 'caen') == 'hk':
                try:
                    if hasattr(self, 'master') and self.master.winfo_exists():
                        self.master.after(0, lambda: self.ui.update_daq_connection_status(False))
                except Exception:
                    pass
                time.sleep(2.0)
                continue

            is_connected = False
            try:
                if self.config_manager:
                    daq_path = self.config_manager.get_config_value('BasePath')
                    if daq_path:
                        command = [os.path.join(daq_path, 'execute_DAQ_v2'), '-j']
                        result = subprocess.run(
                            command, capture_output=True, text=True,
                            timeout=5, preexec_fn=os.setsid,
                            env=self._daq_check_env(daq_path)
                        )
                        # execute_DAQ_v2 -j exits 0 when the digitizer is reachable
                        # (or busy with an active run), non-zero otherwise. The exit
                        # code is the reliable signal; stdout/stderr text is not.
                        is_connected = (result.returncode == 0)
            except Exception:
                pass

            try:
                # Safely forward connection results back into the main loop via thread-safe after queue
                if hasattr(self, 'master') and self.master.winfo_exists():
                    self.master.after(0, lambda c=is_connected: self.ui.update_daq_connection_status(c))
            except Exception:
                pass

            time.sleep(2.0)

    def update_data_directory_size(self):

        if not self.config_manager:
            print("DEBUG: ConfigManager is None") # [디버깅] 설정 파일 로드 실패 확인
            self.ui.update_data_size_display("Config Not Loaded", False)
            self.ui.update_data_size_display("Config Not Loaded", True)
            return

        raw_data_path = self.config_manager.get_config_value("RawDataPath")
        ext_data_path = self.config_manager.get_config_value("ExternalPath")
        ext2_data_path = self.config_manager.get_config_value("ExternalPath2")

        #print(f"DEBUG: RawDataPath from config: '{raw_data_path}'") # [디버깅] 경로 확인
        #print(f"DEBUG: ExternalPath from config: '{ext_data_path}'")

        # 1. 로컬 경로 체크
        if raw_data_path and os.path.exists(raw_data_path):
            #print(f"DEBUG: Starting thread for Local Path: {raw_data_path}")
            threading.Thread(target=self._get_directory_size_thread, args=(raw_data_path, False), daemon=True).start()
        else:
            print(f"DEBUG: Local Path invalid. Exists? {os.path.exists(raw_data_path) if raw_data_path else 'N/A'}")
            msg = "Path Not Found" if raw_data_path else "Path Not Set"
            self.ui.update_data_size_display(msg, False)

        # 2. 외부 하드 경로 체크
        if ext_data_path and os.path.exists(ext_data_path):
            #print(f"DEBUG: Starting thread for External Path: {ext_data_path}")
            threading.Thread(target=self._get_directory_size_thread, args=(ext_data_path, True), daemon=True).start()
        else:
            print(f"DEBUG: External Path invalid. Exists? {os.path.exists(ext_data_path) if ext_data_path else 'N/A'}")
            msg = "Path Not Found" if ext_data_path else "Path Not Set"
            self.ui.update_data_size_display(msg, True)

        # 3. 두 번째 외부 하드 경로 체크 (Ext HDD2)
        if ext2_data_path and os.path.exists(ext2_data_path):
            threading.Thread(target=self._get_directory_size_thread,
                             args=(ext2_data_path, "ext2"), daemon=True).start()
        else:
            msg = "Path Not Found" if ext2_data_path else "Path Not Set"
            self.ui.update_data_size_display(msg, "ext2")

    def _get_directory_size_thread(self, path, is_ext):
        """디버깅 프린트가 추가된 용량 계산 함수"""
        #print(f"DEBUG: Thread started for {path}") # [디버깅] 쓰레드 시작 확인
        display_str = "Error"
        
        try:
            # df -h와 동일한 기능
            usage = shutil.disk_usage(path)
            #print(f"DEBUG: shutil.disk_usage result: {usage}") # [디버깅] 계산 결과 확인
            
            used_human = self.format_size(usage.used)
            total_human = self.format_size(usage.total)
            percent = (usage.used / usage.total) * 100
            
            display_str = f"{used_human} / {total_human} ({percent:.1f}%)"
            #print(f"DEBUG: Final string: {display_str}") # [디버깅] 최종 문자열 확인

        except Exception as e:
            print(f"DEBUG: Error in thread: {e}") # [디버깅] 에러 발생 시 출력
            display_str = "Calc Error"
            self._log(f"Error checking disk usage for {path}: {e}")
            
        finally:
            if hasattr(self, 'ui') and self.master.winfo_exists():
                #print(f"DEBUG: Updating UI with {display_str}") # [디버깅] UI 업데이트 시도
                self.master.after(0, lambda: self.ui.update_data_size_display(display_str, is_ext))
            else:
                print("DEBUG: UI object not found or window closed")


    def format_size(self, size_bytes):
        if size_bytes == 0:
            return "0B"
        size_name = ("B", "KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB")
        i = int(math.floor(math.log(size_bytes, 1024)))
        p = math.pow(1024, i)
        s = round(size_bytes / p, 2)
        return f"{s} {size_name[i]}"

    def open_terminal_at_path_by_key(self, path_key):
        if not self.config_manager: return
        path = self.config_manager.get_config_value(path_key)
        if path:
            self.open_terminal_at_path(path)
        else:
            messagebox.showwarning("Path Not Found", f"'{path_key}' is not defined in your config file.")

    #################### Laser Monitoring ##############################
    def auto_connect_laser(self):
        """더미 모드일 때는 레이저 연결을 건너뜁니다."""
        try:
            # 안전하게 속성 존재 여부 확인 후 더미 변수 체크
            if hasattr(self, 'ui') and hasattr(self.ui, 'auto_ui') and getattr(self.ui.auto_ui, 'dummy_var', None):
                if self.ui.auto_ui.dummy_var.get():
                    self._log("[INFO] Dummy Mode: Skipping real Laser connection.")
                    return
        except Exception:
            pass
        self.laser_mgr.auto_connect_laser()

    def connect_single_laser(self, wl): self.laser_mgr.connect_single_laser(wl)
    def disconnect_single_laser(self, wl): self.laser_mgr.disconnect_single_laser(wl)
    def manual_refresh_laser(self, wl=None): self.laser_mgr.manual_refresh_laser(wl)
    def set_laser_ld_safe(self, target_wl, state): self.laser_mgr.set_laser_ld_safe(target_wl, state)
    def apply_laser_frequency_multi(self, wl): self.laser_mgr.apply_laser_frequency_multi(wl)
    def set_laser_tec_multi(self, wl, state): self.laser_mgr.set_laser_tec_multi(wl, state)
    def apply_laser_currents_multi(self, wl): self.laser_mgr.apply_laser_currents_multi(wl)
    def apply_laser_pulse_width_multi(self, wl): self.laser_mgr.apply_laser_pulse_width_multi(wl)
    def update_laser_status_loop(self): self.laser_mgr.update_laser_status_loop()
    def on_laser_trigger_change_multi(self, wl): self.laser_mgr.on_laser_trigger_change_multi(wl)
    def on_laser_trigger_change(self, event=None): self.laser_mgr.on_laser_trigger_change(event)
    def load_historical_laser_data(self, wl=None): self.laser_mgr.load_historical_laser_data(wl)
    def refresh_laser_realtime_plot(self, wl="405nm"): self.laser_mgr.refresh_laser_realtime_plot(wl)
    def setup_laser_logger(self): self.laser_mgr.setup_laser_logger()
    def load_today_laser_log(self): self.laser_mgr.load_today_laser_log()
    def preload_laser_history(self): self.laser_mgr.preload_laser_history()
    def _log_laser(self, wl, msg): self.laser_mgr._log_laser(wl, msg)
    def save_laser_realtime_data(self, wl, temp, pulse, ld_on=False, tec_on=False): 
        self.laser_mgr.save_laser_realtime_data(wl, temp, pulse, ld_on, tec_on)


    #################### Laser Monitoring ##############################

    #################### UPS Monitoring ##############################
    def search_ups_ports(self): self.ups_mgr.search_ups_ports()
    def diagnose_ups(self): self.ups_mgr.diagnose_ups()
    #def auto_connect_ups(self): self.ups_mgr.auto_connect_ups()

    def auto_connect_ups(self):
        try:
            if hasattr(self, 'ui') and hasattr(self.ui, 'auto_ui') and getattr(self.ui.auto_ui, 'dummy_var', None):
                if self.ui.auto_ui.dummy_var.get():
                    self._log("🧪 Dummy Mode: Skipping real UPS connection.")
                    return
        except Exception:
            pass
        self.ups_mgr.auto_connect_ups()


    def _try_ups_handshake(self, port): self.ups_mgr._try_ups_handshake(port)
    def update_ups_status_loop(self): self.ups_mgr.update_ups_status_loop()
    def manual_refresh_ups(self): self.ups_mgr.manual_refresh_ups()
    def toggle_ups_connection(self): self.ups_mgr.toggle_ups_connection()
    def refresh_ups_plot(self): self.ups_mgr.refresh_ups_plot()
    def update_ups_outlet_status(self, states): self.ups_mgr.update_ups_outlet_status(states)
    def shutdown_ups_all(self): self.ups_mgr.shutdown_ups_all()
    def save_ups_realtime_data(self, watt, temp, vin, vout): self.ups_mgr.save_ups_realtime_data(watt, temp, vin, vout)
    def preload_ups_history(self): self.ups_mgr.preload_ups_history()
    def handle_ups_shutdown(self): self.ups_mgr.handle_ups_shutdown()
    def shutdown_ups_each(self, index): self.ups_mgr.shutdown_ups_each(index)
    def check_ups_alerts(self, watt, temp, batt, load, vin): self.ups_mgr.check_ups_alerts(watt, temp, batt, load, vin)
    def unlock_ups_port(self): self.ups_mgr.unlock_ups_port()
    #################### UPS Monitoring ##############################


    def get_system_status(self):
        """NOTE: includes a blocking subprocess.run() (see _check_hv_env_process
        below) -- fine for one-off callers, but the dashboard loop that used
        to call this every 2s has been switched to call _check_hv_env_process()
        from a background thread instead, since running it directly on the Tk
        main thread stalled the whole GUI for the pgrep's duration every tick.
        Kept intact here for any future synchronous caller."""
        status = {
                "DAQ": False,
                "HV": False,
                "Env": False,
                "Laser": False,
                "UPS": False
                }

        if hasattr(self, 'ui') and hasattr(self.ui, 'daq_connected_flag'):
            status["DAQ"] = self.ui.daq_connected_flag
        # 2. Laser 상태
        if hasattr(self, 'laser_mgr') and self.laser_mgr.laser_instances:
            status["Laser"] = any(inst.is_connected() for inst in self.laser_mgr.laser_instances.values())

        # 3. UPS 상태 (시리얼 포트 체크)
        if hasattr(self, 'ups_mgr') and self.ups_mgr.ups_serial and self.ups_mgr.ups_serial.is_open:
            if hasattr(self, 'ui'):
                msg = self.ui.ups_vars["status_msg"].get()
                if "Normal" in msg or "Battery" in msg:
                    status["UPS"] = True

        if self._check_hv_env_process():
            status["HV"] = True
            status["Env"] = True

        return status

    def _check_hv_env_process(self):
        """The pgrep check alone, with no Tk/widget access -- safe to call
        from a background thread. Split out of get_system_status() so the
        dashboard loop can run this off the main thread."""
        try:
            check_hv = subprocess.run(['pgrep', '-f', 'monitoring_app.py'], capture_output=True)
            return check_hv.returncode == 0
        except Exception:
            return False

    def on_closing(self):
        """Shows a shutdown progress dialog and safely releases hardware."""
        if not messagebox.askokcancel("Exit", "Are you sure you want to exit the program?"):
            return

        # Stop all periodic poll loops from rescheduling against soon-to-be-destroyed widgets.
        self._shutting_down = True

        # Immediately cancel any pending after() callbacks from periodic loops so they
        # cannot fire against a partially-destroyed widget and produce Tcl errors.
        try:
            if hasattr(self, 'ups_mgr') and self.ups_mgr.ups_after_id:
                self.master.after_cancel(self.ups_mgr.ups_after_id)
                self.ups_mgr.ups_after_id = None
        except Exception:
            pass
        try:
            if hasattr(self, 'laser_mgr') and self.laser_mgr.laser_after_id:
                self.master.after_cancel(self.laser_mgr.laser_after_id)
                self.laser_mgr.laser_after_id = None
        except Exception:
            pass

        self._log("Shutting down... Releasing hardware resources.")
        self._log("=== Application Closing Process ===")

        # 콘솔에서 실행 중인 배치 작업(슬롯별)이 있으면 프로세스 그룹째 종료해 좀비를 막는다.
        for slot, cproc in getattr(self, '_console_procs', {}).items():
            if cproc is not None and cproc.poll() is None:
                try:
                    os.killpg(os.getpgid(cproc.pid), signal.SIGTERM)
                    self._log(f"[INFO] Terminated running console job [{slot}] on exit.")
                except Exception as e:
                    self._log(f"[WARNING] Failed to terminate console job [{slot}]: {e}")

        shutdown_win = tk.Toplevel(self.master)
        shutdown_win.title("Shutting Down")
        shutdown_win.geometry("380x150")
        shutdown_win.attributes("-topmost", True)
        shutdown_win.protocol("WM_DELETE_WINDOW", lambda: None) 
        
        shutdown_win.update_idletasks()
        x = self.master.winfo_x() + (self.master.winfo_width() // 2) - 190
        y = self.master.winfo_y() + (self.master.winfo_height() // 2) - 75
        shutdown_win.geometry(f"+{x}+{y}")

        lbl_title = ttk.Label(shutdown_win, text="System Shutdown in Progress...", font=("Helvetica", 12, "bold"))
        lbl_title.pack(pady=(15, 10))

        lbl_status = ttk.Label(shutdown_win, text="Initializing...", font=("Helvetica", 10))
        lbl_status.pack(pady=5)

        progress = ttk.Progressbar(shutdown_win, mode='determinate', length=300)
        progress.pack(pady=10)

        def step1_motors():
            lbl_status.config(text="Stopping PMT movement... Please wait.")
            progress['value'] = 25
            shutdown_win.update()
            if hasattr(self, 'rot_mgr'):
                try:
                    self._log("[INFO] Sending STOP commands to all motors before exit...")
                    self.rot_mgr.stop_rotation(2)
                    self.rot_mgr.stop_rotation(3)
                except Exception as e:
                    self._log(f"[ERROR] Failed to stop motors on exit: {e}")
            self.master.after(1000, step2_lasers) 

        def step2_lasers():
            lbl_status.config(text="Disconnecting Lasers...")
            progress['value'] = 60
            shutdown_win.update()
            try:
                self.save_app_config()
            except Exception as e:
                self._log(f"[WARNING] Error saving config on exit: {e}")
            if hasattr(self, 'laser_mgr'):
                for wl, inst in self.laser_mgr.laser_instances.items():
                    if inst.is_connected():
                        try:
                            inst.disconnect()
                            self._log(f"[INFO] Laser {wl} safely disconnected.")
                        except Exception as e:
                            self._log(f"[WARNING] Error disconnecting Laser {wl}: {e}")
            self.master.after(600, step3_ups)

        def step3_ups():
            lbl_status.config(text="Closing UPS connection...")
            progress['value'] = 90
            shutdown_win.update()
            if hasattr(self, 'ups_mgr') and self.ups_mgr.ups_serial and self.ups_mgr.ups_serial.is_open:
                try:
                    self.ups_mgr.ups_serial.close()
                    self._log("[INFO] UPS serial port safely closed.")
                except Exception as e:
                    self._log(f"[WARNING] Error closing UPS port: {e}")
            self.master.after(600, step4_finish)

        def step4_finish():
            lbl_status.config(text="Safe to exit. Goodbye!")
            progress['value'] = 100
            shutdown_win.update()
            self._log("[INFO] Goodbye!")
            self.master.after(500, _terminate)

        def _terminate():
            # Destroy the GUI, then force-exit the process. os._exit guarantees the
            # OS process dies even if a library/daemon thread would otherwise keep
            # the interpreter alive — so the launcher's poll() correctly sees it gone.
            try:
                self.master.destroy()
            except Exception:
                pass
            os._exit(0)

        self.master.after(100, step1_motors)


def launch():
    """애플리케이션 진입점.

    main.py(프로덕션)와 TEST MODE shim 인 main_test.py 가 공통으로 호출한다.
    실제 시뮬레이션 모드는 별도 파일이 아니라 in-app 'TEST RUN (Simulation Mode)'
    체크박스(auto_ui.dummy_var)로 동작하므로, 두 진입점은 동일한 App 을 실행한다.
    """
    # Prevent zombie processes: tell the kernel to auto-reap children we don't
    # explicitly wait() for (gnome-terminal launches, gedit, etc.).
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)

    base_directory = os.path.dirname(os.path.abspath(__file__))

    # 시작 시, 크래시로 남은 '좀비' 런타임 플래그를 안전하게 청소한다.
    # 단, DAQ/분석 프로세스가 실제로 살아있으면 라이브 런 보호를 위해 건너뛴다.
    try:
        import glob

        # Exclude the transient `-j` connection probe — only a real acquisition run counts.
        daq_probe = subprocess.run(
            'pgrep -x execute_DAQ_v2 | xargs -r ps -o args= -p 2>/dev/null | grep -v -- "-j"',
            shell=True, capture_output=True, text=True)
        daq_active = bool(daq_probe.stdout.strip())
        ana_active = subprocess.run(['pgrep', '-f', 'run_cpp_script_v2.sh'], capture_output=True).returncode == 0
        tmux_active = subprocess.run(['pgrep', '-f', 'Ana_Seq'], capture_output=True).returncode == 0

        if daq_active or ana_active or tmux_active:
            print("[WARNING] Active core processes detected in memory. Startup flag flushing aborted to protect live runs.")
        else:
            leftover_flags = glob.glob("/tmp/daq_flags/*.flag")
            if leftover_flags:
                for flag_file in leftover_flags:
                    try:
                        os.remove(flag_file)
                    except Exception:
                        pass
                print(f"[SUCCESS] Safely purged {len(leftover_flags)} dead zombie runtime flags. System is clean.")
            else:
                print("[INFO] No leftover runtime flags found. System pipeline is pristine.")
    except Exception as e:
        print(f"[ERROR] Safeguard initialization flag interlock error: {e}")

    root = tk.Tk()
    app = App(root, base_directory)
    root.mainloop()


if __name__ == "__main__":
    launch()

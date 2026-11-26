"""Main window: the DAQ left panel as a full-height sidebar, and the Tk app's tab
tree (main tabs > DAQ System > General Scan) on the right with status lamps on
the tabs. The HV Monitor tab raises the separate HV app. Pages build lazily."""
import importlib
import os
import re

from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QGuiApplication, QKeySequence
from PyQt5.QtWidgets import (QApplication, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMainWindow,
                             QMessageBox, QPushButton, QShortcut, QSplitter, QTabBar, QTabWidget, QToolButton,
                             QVBoxLayout, QWidget)

from app import theme
from core import config, env, status
import site_secrets  # noqa: E402
from core.access import ACCESS
from core.jobs import JOBS
from pages.daq import output as output_page
from widgets import basic


HV_TAB = "HV Monitor"   # not a page: selecting it raises the HV app window
DAQ_TAB = "DAQ System"
MAIN_TABS = [DAQ_TAB, "Daily Routine", "Laser Control", "B-field Monitoring", HV_TAB, "Function Generator", "UPS Status",
             "Quick Start", "Emergency"]
DAQ_TABS = ["General Scan", "PMT Setup & Helper", "Acquisition", "Waveform", "Data Files", "Images", "History",
            "Backup", "DAQ Diag", "Signal Lamp", "All Logs"]
PAGE_ALIASES = {"Output": "Acquisition", "Log": "All Logs"}   # older callers
GS_TABS = ["Quick Setup", "Control Panel (Master)", "Live Scan", "Stability Run", "Schedule Manager",
           "Scan History"]       # all logs, the DAQ taking log included: DAQ System > All Logs
PORTED = {"Live Scan": ("pages.general_scan.live_scan", "LiveScanPage"), "Scan History": ("pages.general_scan.scan_history", "HistoryPage"),
          "All Logs": ("pages.logs", "LogsPage"), "History": ("pages.information", "InformationPage"),
          "Emergency": ("pages.emergency", "EmergencyPage"),
          "UPS Status": ("pages.ups", "UpsPage"),
          "Data Files": ("pages.daq.data_files", "DataFilesPage"),
          "Laser Control": ("pages.laser", "LaserPage"),
          "Daily Routine": ("pages.routine_results", "DailyRoutineTabs"),
          "Backup": ("pages.daq.backup", "BackupPage"), "PMT Setup & Helper": ("pages.daq.pmt_setup", "PmtSetupPage"), "Waveform": ("pages.daq.waveform", "WaveformPage"),
          "Quick Start": ("pages.quick_start", "QuickStartPage"),
          "Live Scan Logs": ("pages.general_scan.scan_log", "ScanLogPage"),
          "Control Panel (Master)": ("pages.general_scan.control_panel", "ControlPanelPage"),
          "Quick Setup": ("pages.general_scan.quick_setup", "QuickSetupPage"),
          "Schedule Manager": ("pages.general_scan.schedule", "SchedulePage"),
          "Stability Run": ("pages.general_scan.stability", "StabilityPage"),
          "Acquisition": ("pages.daq.output", "OutputPage"),
          "Images": ("pages.daq.images", "ImagesPage"),
          "DAQ Diag": ("pages.daq.diag", "DiagPage"),
          "Signal Lamp": ("pages.signal_lamp", "SignalLampPage"),
          "Function Generator": ("pages.function_generator_simple", "FunctionGeneratorPage"),
          "B-field Monitoring": ("pages.bfield", "BfieldPage")}
# Main tab -> status lamp (core.status.system_status names); the tooltip lists the device values
TAB_LAMP = {DAQ_TAB: "DAQ System", "Laser Control": "Laser Controller", HV_TAB: "HV / Env Sensor",
            "Function Generator": "Function Generator", "UPS Status": "OMRON UPS", "B-field Monitoring": "B-field Monitor",
            "All Logs": "Log Backup"}
# Overview lamp -> device_rows() names shown in its tooltip
LAMP_IDLE = "#8a909c"     # head not connected and not expected (e.g. unplugged on purpose)
DETAIL_PREFIX = {"DAQ System": ("DAQ",), "HV / Env Sensor": ("HV", "Env"), "Laser Controller": ("Laser",),
                 "OMRON UPS": ("UPS",)}


class _UpsBus(QObject):
    event = pyqtSignal(str, object)


def routine_tab_title(errors):
    return f"Daily Routine ({errors})" if errors else "Daily Routine"


def _tab_name(text):
    """Page name of a tab title: without the '&&' escape and a ' (N)' count."""
    return re.sub(r" \(\d+\)$", "", text.replace("&&", "&"))


def _esc(title):
    return title.replace("&", "&&")   # Qt would turn a single "&" into a mnemonic underline


class LazyTabs(QTabWidget):
    """Builds a ported page the first time its tab is shown, so start-up stays fast."""

    def __init__(self, names, builders=None):
        super().__init__()
        self.builders = builders or {}
        for n in names:
            self.addTab(QWidget() if n in self.builders else basic.placeholder(n), _esc(n))
        self.currentChanged.connect(self._build)

    def _build(self, i):
        name = _tab_name(self.tabText(i))
        make = self.builders.pop(name, None)
        if make is None:
            return
        try:
            page = make()
        except Exception as e:
            from core.update import UPDATER
            note = "\nRestart required." if UPDATER.available or UPDATER.check() else ""
            page = QLabel(f"{name} failed to load: {e}{note}")
        holder = self.widget(i)   # fill the empty tab in place: removeTab() would delete its status lamp
        lay = holder.layout() or QVBoxLayout(holder)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(scrolling(page))


def scrolling(page):
    """The page in a frameless scroll area, so a tall page never pushes the window below the screen."""
    from PyQt5.QtWidgets import QFrame, QScrollArea
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.NoFrame)
    area.setStyleSheet(f"QScrollArea, QScrollArea > QWidget > QWidget {{ background: {theme.C['bg']}; }}")
    area.setWidget(page)
    area.verticalScrollBar().setSingleStep(12)   # 3 lines per wheel notch -> 36 px instead of 60
    return area


def builder(name):
    mod, cls = PORTED[name]
    return lambda: getattr(importlib.import_module(mod), cls)()


class MainWindow(QMainWindow):
    def __init__(self, progress=None):
        super().__init__()
        self._progress = progress or (lambda _msg: None)
        self.setWindowTitle("DAQ Control v2")
        screen = QApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else None
        self.resize(min(1500, avail.width()) if avail else 1500, min(920, avail.height() - 40) if avail else 920)
        self._rebuilding = False
        self._closing = False
        self._progress("Building pages")
        self._menus()

        self.gs_tabs = LazyTabs(GS_TABS, {n: builder(n) for n in GS_TABS if n in PORTED})
        self.gs_tabs.setObjectName("gsTabs")
        self.daq_tabs = LazyTabs([], {})
        self.daq_tabs.setObjectName("daqTabs")
        for n in DAQ_TABS:
            if n == "General Scan":
                self.daq_tabs.addTab(self.gs_tabs, n)
            elif n in PORTED:
                self.daq_tabs.builders[n] = builder(n)
                self.daq_tabs.addTab(QWidget(), _esc(n))
            else:
                self.daq_tabs.addTab(basic.placeholder(n), _esc(n))

        daq = QWidget()
        dl = QVBoxLayout(daq)
        dl.setContentsMargins(6, 6, 6, 6)
        dl.addWidget(self._access_bar())
        dl.addWidget(self.daq_tabs, 1)

        self.main_tabs = LazyTabs([], {})
        self.main_tabs.setObjectName("mainTabs")
        for n in MAIN_TABS:
            if n == DAQ_TAB:
                self.main_tabs.addTab(daq, n)
            elif n == HV_TAB:
                self.main_tabs.addTab(basic.placeholder_text(
                    "The HV Monitor is a separate app; this tab brings its window to the front."), n)
            elif n in PORTED:
                self.main_tabs.builders[n] = builder(n)
                self.main_tabs.addTab(QWidget(), n)
            else:
                self.main_tabs.addTab(basic.placeholder(n), n)
        self.tab_lamps = {}
        for i in range(self.main_tabs.count()):
            key = TAB_LAMP.get(self.main_tabs.tabText(i))
            if key == "Laser Controller":
                box = QWidget()
                hl = QHBoxLayout(box)
                hl.setContentsMargins(0, 0, 0, 0)
                hl.setSpacing(3)
                from core.laser import WAVELENGTHS
                self.laser_lamps = {}
                for wl in WAVELENGTHS:      # one lamp per head, so one missing laser does not read as all down
                    lamp = basic.make_lamp(8)
                    lamp.setToolTip(wl)
                    hl.addWidget(lamp)
                    self.laser_lamps[wl] = lamp
                self.main_tabs.tabBar().setTabButton(i, QTabBar.RightSide, box)
            elif key:
                lamp = basic.make_lamp(10)
                self.main_tabs.tabBar().setTabButton(i, QTabBar.RightSide, lamp)
                self.tab_lamps[key] = lamp
        for i in range(self.daq_tabs.count()):     # sub-tab lamps (All Logs -> Log Backup)
            key = TAB_LAMP.get(self.daq_tabs.tabText(i).replace("&&", "&"))
            if key:
                lamp = basic.make_lamp(10)
                self.daq_tabs.tabBar().setTabButton(i, QTabBar.RightSide, lamp)
                self.tab_lamps[key] = lamp
        self._last_main = 0
        self.main_tabs.currentChanged.connect(self._on_main_tab)
        split = self.split = QSplitter(Qt.Horizontal)
        split.addWidget(self._left_panel())
        split.addWidget(self.main_tabs)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([340, 1160])
        self.setCentralWidget(split)
        self._left_w = 340
        self.left_btn = QToolButton()
        self.left_btn.setObjectName("leftToggle")
        c = theme.C
        self.left_btn.setStyleSheet(
            f"QToolButton#leftToggle {{ font-size: 10pt; font-weight: 600; color: {c['accent']}; background: {c['bg']};"
            f" border: 1px solid {c['accent']}; border-radius: 4px; padding: 5px 10px; margin: 2px 6px 2px 2px; }}"
            f"QToolButton#leftToggle:hover {{ background: {c['tab_hover']}; }}")
        self.left_btn.clicked.connect(self.toggle_left)
        self.main_tabs.setCornerWidget(self.left_btn, Qt.TopLeftCorner)
        split.splitterMoved.connect(lambda *_: self._paint_left_btn())
        self._paint_left_btn()
        self.gs_tabs.setCurrentIndex(GS_TABS.index("Quick Setup"))
        self.gs_tabs._build(GS_TABS.index("Quick Setup"))

        output_page.bus().event.connect(self._jobs_lamp)
        from app import scan_bridge
        self._progress("Starting the scan engine")
        scan_bridge.init()                 # scan engine (and its schedule watchdog) from start-up, as in the Tk app
        if not env.NO_HW:
            from core import ups
            self._progress("Starting UPS, stage and digitizer monitoring")
            svc = ups.service()            # UPS polling + CSV logging from start-up, as in the Tk app
            status.DAQ_PROBE.start()       # digitizer connection lamp, as the Tk app's 2 s probe
            status.FG_PROBE.start()        # function generator tab lamp
            self._ups_bus = _UpsBus()
            self._ups_bus.event.connect(self._ups_event)
            svc.listeners.append(lambda ev, p: self._ups_bus.event.emit(ev, p))
        if not env.NO_HW and not getattr(QApplication.instance(), "_lasers_offered", False):
            QApplication.instance()._lasers_offered = True   # once per run, not on every window rebuild
            QTimer.singleShot(1500, self._offer_laser_reconnect)
        for key, cmd in (("Ctrl+O", "open_config"), ("Ctrl+P", "run_produce"), ("Ctrl+A", "run_analysis"),
                         ("Ctrl+S", "run_waveform"), ("Ctrl+I", "open_image_viewer")):   # same keys as the Tk app
            QShortcut(QKeySequence(key), self, activated=lambda c=cmd: self._shortcut(c))
        self.tick = QTimer(self)
        self.tick.timeout.connect(self._update_overview)
        self.tick.start(3000)
        self._progress("Reading device status")
        self._update_overview()

    def _update_overview(self):
        why = status.busy_reason() if ACCESS.unlocked else ""
        self.unlock_btn.setEnabled(not why)
        self.unlock_btn.setToolTip(why)
        try:
            st = status.system_status()
        except Exception:
            st = {}
        try:
            details = status.device_rows()
        except Exception:
            details = []
        if not env.NO_HW:
            from core import laser as L
            ctl = L.laser()
            if ctl.owns():
                on = [wl for wl in L.WAVELENGTHS if ctl.connected(wl) and not ctl.comm_error[wl]]
                lost = [wl for wl in L.WAVELENGTHS if wl in ctl.expected and wl not in on]
                st["Laser Controller"] = (bool(on) and not lost, f"connected: {', '.join(on) or 'none'}"
                                          + (f"; lost: {', '.join(lost)}" if lost else ""))
                for wl, lamp in getattr(self, "laser_lamps", {}).items():
                    ok = wl in on
                    basic.paint_lamp(lamp, basic.LAMP_ON if ok else basic.LAMP_OFF if wl in lost else LAMP_IDLE)
                    lamp.setToolTip(f"{wl}: " + ("connected" if ok else "lost" if wl in lost else "not connected"))
                if ctl._asking and "Laser Control" in self.main_tabs.builders:
                    self.main_tabs.setCurrentIndex(MAIN_TABS.index("Laser Control"))   # its page asks how to resume
        if not env.NO_HW:
            st["Function Generator"] = status.FG_PROBE.result()
        for name, lamp in self.tab_lamps.items():
            ok, text = st.get(name, (False, "check failed"))
            basic.set_lamp(lamp, ok)
            prefixes = DETAIL_PREFIX.get(name, ())
            lines = [f"{n}: {t}" for n, t, _ in details if n.split(" ")[0] in prefixes] or [f"Last log update: {text}"]
            lamp.setToolTip("\n".join(lines))
        try:
            backend = status.daq_backend() or self._own_backend()
        except Exception:
            backend = None
        self.main_tabs.setTabText(MAIN_TABS.index(DAQ_TAB), f"{DAQ_TAB} ({backend})" if backend else DAQ_TAB)
        self._routine_tab()
        if hasattr(self, "left"):
            self.left.refresh()
        self._update_pipeline()

    def _routine_tab(self):
        """'Daily Routine (N)' while N routine errors are unacknowledged."""
        try:
            from core import routine as rt
            n = rt.issue_counts(rt.issues())["Error"]
        except Exception:
            n = 0
        i = MAIN_TABS.index("Daily Routine")
        if self.main_tabs.tabText(i) != routine_tab_title(n):
            self.main_tabs.setTabText(i, routine_tab_title(n))

    def _update_pipeline(self):
        """Chips of the newest run in the theme.STATE colours (white rim: readable on the red and green bar)."""
        try:
            run, states = status.pipeline_states()
        except Exception:
            run, states = None, {}
        red_bar = not ACCESS.unlocked
        for key, chip in self.chips.items():
            st = states.get(key, "waiting")
            chip.setText("MOVING" if st == "moving" else dict(status.PIPELINE_STAGES)[key])
            bg, fg, _b = theme.STATE[{"active": "active", "moving": "move", "done": "done"}.get(st, "wait")]
            chip.setStyleSheet(f"background: {bg}; color: {fg}; border: 2px solid white; border-radius: 10px;"
                               " padding: 2px 11px; font-size: 11pt; font-weight: 700;")
        self.pipe_run.setText(f"Run {run}" if run is not None else "Idle")
        self._update_routine_chip()
        idle = "#ffd7db" if red_bar else "#aeb4c0"
        self.pipe_run.setStyleSheet(f"color: {'white' if run is not None else idle}; font-size: 12pt;"
                                    " font-weight: 700;")

    def _update_routine_chip(self):
        """'Routine 2/5  Dark rate 3/10  ends 22:57' on the green bar while a routine runs."""
        try:
            from core import routine as rt
            p = rt.progress()
        except Exception:
            p = {}
        if not p.get("active"):
            self.routine_btn.hide()
            return
        count = p["text"].split(" ", 1)[-1] if p.get("text") else ""
        self.routine_btn.setText(f"Routine {p['pos']}/{p['n']}   {p['step']}" + (f" {count}" if count else "")
                                 + f"   ends {p['end']:%H:%M}")
        self.routine_btn.show()

    def _on_main_tab(self, i):
        if self.main_tabs.tabText(i) != HV_TAB:
            self._last_main = i
            return
        self.main_tabs.blockSignals(True)
        self.main_tabs.setCurrentIndex(self._last_main)   # bounce back, like the Tk Images tab
        self.main_tabs.blockSignals(False)
        self._open_hv_monitor()

    @staticmethod
    def _own_backend():
        """v2's own DAQ selection while it owns the hardware (the Tk app is closed)."""
        import instance_lock
        from core import scan
        if instance_lock.is_running("daq") or scan._host is None:
            return None
        return status.BACKEND_NAMES.get(getattr(scan._host.auto_mgr, "daq_backend", "caen"), "CAEN")

    def _access_bar(self):
        """Lock banner in the Tk app's position: one master-password Unlock opens every control."""
        bar = self.access_bar = QWidget()
        bar.setObjectName("accessBar")
        h = QHBoxLayout(bar)
        h.setContentsMargins(10, 6, 10, 6)
        self.unlock_btn = QPushButton("Unlock")
        self.unlock_btn.clicked.connect(self._toggle_access)
        h.addWidget(self.unlock_btn)
        self.access_lbl = QLabel()
        h.addWidget(self.access_lbl)
        sep = QLabel("|")
        sep.setStyleSheet("color: #8a9099; font-size: 14pt; padding: 0 10px;")
        h.addWidget(sep)
        title = QLabel("PIPELINE")
        title.setStyleSheet("color: white; font-size: 12pt; font-weight: 800; letter-spacing: 1px;")
        h.addWidget(title)
        self.chips = {}
        for key, label in status.PIPELINE_STAGES:
            chip = QLabel(label)
            self.chips[key] = chip
            h.addWidget(chip)
        h.addSpacing(8)
        self.pipe_run = QLabel()
        h.addWidget(self.pipe_run)
        h.addSpacing(14)
        self.routine_btn = QPushButton()
        self.routine_btn.setObjectName("routineChip")
        self.routine_btn.setCursor(Qt.PointingHandCursor)
        self.routine_btn.setStyleSheet("QPushButton#routineChip { background: transparent; color: white; border: 2px solid"
                                       " white; border-radius: 10px; padding: 2px 10px; font-size: 11pt; font-weight: 700; }")
        self.routine_btn.clicked.connect(lambda: self.show_page("Daily Routine"))
        self.routine_btn.hide()
        h.addWidget(self.routine_btn)
        h.addStretch(1)
        self.alerts_btn = QPushButton("Message to Expert")
        self.alerts_btn.clicked.connect(self._open_slack_alerts)
        h.addWidget(self.alerts_btn)
        h.addSpacing(8)
        self.update_btn = QPushButton()
        self.update_btn.setObjectName("updateBadge")
        self.update_btn.setCursor(Qt.PointingHandCursor)
        self.update_btn.clicked.connect(self._on_update_click)
        self.update_btn.setStyleSheet("QPushButton#updateBadge { background: #e67700; color: #1f2430; font-weight: 700;"
                                      " border: none; border-radius: 4px; padding: 4px 12px; }"
                                      "QPushButton#updateBadge:hover { background: #ff8c1a; }")
        self.update_btn.hide()
        h.addWidget(self.update_btn)
        self.update_timer = QTimer(self)
        self.update_timer.timeout.connect(self._check_update)
        self.update_timer.start(60000)
        self.request_timer = QTimer(self)        # request files: routine start (5 s), restart (see _check_update)
        self.request_timer.timeout.connect(self._check_requests)
        self.request_timer.start(5000)
        QTimer.singleShot(0, self._check_update)
        self.pipe_timer = QTimer(self)
        self.pipe_timer.timeout.connect(self._update_pipeline)
        self.pipe_timer.start(1000)   # stages change within seconds; the rest updates every 3 s
        ACCESS.listeners.append(lambda _on: self._paint_access())
        self._paint_access()
        return bar

    def _open_slack_alerts(self):
        from pages.slack_alerts import SlackAlertsDialog
        SlackAlertsDialog(self).exec_()

    # ---------------------------------------------------------------- update & restart (as the Tk badge)
    RESTART_REQUEST = os.path.expanduser("~/.cache/precal/daq_v2_restart_request")

    def _check_requests(self):
        from core import routine as rt
        try:
            rt.start_from_request()
        except Exception as e:
            from core import applog
            applog.log(f"[ERROR] Routine: start request failed: {type(e).__name__}: {e}")
        if os.path.exists(self.RESTART_REQUEST):
            self._check_update()

    def _check_update(self):
        from core.update import UPDATER
        if UPDATER.check():
            from core import applog
            applog.log("[INFO] Source update detected on disk. Click the update badge to restart and apply it.")
        if os.path.exists(self.RESTART_REQUEST):
            from core import applog
            try:
                os.remove(self.RESTART_REQUEST)
            except OSError:
                pass
            err = UPDATER.syntax_error()
            if err:
                applog.log(f"[ERROR] Restart request ignored: the code on disk has a syntax error ({err}).")
            else:
                UPDATER.available = UPDATER.when_idle = True
                applog.log("[INFO] Restart requested (file) - restarting as soon as the system is idle.")
        if UPDATER.available:
            self.update_btn.setText("Update pending - restarts when idle" if UPDATER.when_idle
                                    else "Update available - click to restart")
            self.update_btn.show()
            if UPDATER.when_idle and not UPDATER.busy_reason():
                from core import applog
                applog.log("[INFO] System is idle - applying the queued update restart.")
                UPDATER.restart(QApplication.instance())
        self.update_timer.setInterval(15000 if UPDATER.when_idle else 60000)

    def _on_update_click(self):
        from core import applog
        from core.update import UPDATER
        if not UPDATER.available:
            return
        applog.log("[INFO] Update badge clicked - checking whether a restart can proceed.")
        err = UPDATER.syntax_error()
        if err:
            QMessageBox.critical(self, "Update Error",
                                 f"The updated code has a syntax error - staying on the current version.\n\n{err}")
            return
        reason = UPDATER.busy_reason()
        if reason:
            applog.log(f"[INFO] Restart blocked: {reason}.")
            box = QMessageBox(QMessageBox.Question, "Update Pending",
                              f"The system looks busy: {reason}.\n\n"
                              "Yes  - restart automatically as soon as it becomes idle.\n"
                              "No   - force restart RIGHT NOW anyway.\n"
                              "Cancel - do nothing.", QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel, self)
            choice = box.exec_()
            if choice == QMessageBox.Yes:
                UPDATER.when_idle = True
                applog.log("[INFO] Update restart queued - will apply when the system is idle.")
                self._check_update()
            elif choice == QMessageBox.No:
                applog.log("[WARNING] Operator forced a restart while system reported busy.")
                UPDATER.restart(QApplication.instance())
            return
        if QMessageBox.question(self, "Restart",
                                "Restart now to apply the update?\n\n"
                                "- Unsaved Quick Setup edits will be lost.\n"
                                "- Hardware state (motors, laser TEC/LD, UPS) is NOT touched.",
                                QMessageBox.Ok | QMessageBox.Cancel) == QMessageBox.Ok:
            applog.log("[INFO] Restarting v2 to apply the update.")
            UPDATER.restart(QApplication.instance())

    def _paint_access(self):
        on = ACCESS.unlocked
        self.access_bar.setStyleSheet(f"#accessBar {{ background: {'#1a7f37' if on else '#dc3545'}; border-radius: 4px; }}"
                                      "#accessBar QLabel { color: white; font-weight: 700; }")
        self.access_lbl.setText("CONTROLS ACTIVE" if on else "SYSTEM LOCKED")
        self.unlock_btn.setText("Lock" if on else "Unlock")
        if hasattr(self, "alerts_btn"):
            self.alerts_btn.setEnabled(on)
        if hasattr(self, "pipe_run"):
            self._update_pipeline()

    def _toggle_access(self):
        if ACCESS.unlocked:
            why = status.busy_reason()
            if why:
                QMessageBox.warning(self, "Lock", why + "\n\nLock after it ends.")
                return
            ACCESS.lock()
            return
        why = ACCESS.blocker()
        if why:
            QMessageBox.warning(self, "Unlock", why)
            return
        pwd, ok = QInputDialog.getText(self, "Security", "Enter Master Password:", QLineEdit.Password)
        if not ok:
            return
        done, msg = ACCESS.unlock(pwd)
        if not done:
            QMessageBox.critical(self, "Unlock", msg)

    def _shortcut(self, cmd):
        from pages.daq import script_actions
        if cmd in script_actions.NEEDS_UNLOCK and not ACCESS.unlocked:
            return
        self.left.actions.run(cmd)

    def _ups_event(self, ev, payload):
        if ev == "power_fail":
            from core import ups
            notes = ups.run_power_fail(payload)
            box = QMessageBox(QMessageBox.Critical, "UPS POWER FAILURE",
                              f"Automatic shutdown: {payload.get('reason')}.\n\n" + "\n".join(notes) +
                              "\n\nHV is switched off next, then the PC powers off.", QMessageBox.Ok, self)
            box.setAttribute(Qt.WA_DeleteOnClose)
            box.open()
            return
        if ev == "critical":
            box = QMessageBox(QMessageBox.Warning, "UPS CRITICAL ALERT", str(payload), QMessageBox.Ok, self)
            box.setAttribute(Qt.WA_DeleteOnClose)
            box.open()                     # non-blocking: polling and scans carry on

    def _offer_laser_reconnect(self):
        """Like the Tk app at start-up: restore the lasers connected last time, and offer to turn off a live LD."""
        from core import laser as L
        after_update = os.environ.pop("APP_RESTART_AUTO_RECONNECT", None)   # set by Update & Restart
        ctl = L.laser()
        wls = [wl for wl in ctl.last_connected if wl in L.WAVELENGTHS]
        if not wls or not ctl.owns():
            return
        if after_update:
            from core import applog
            applog.log(f"[INFO] Post-restart: reconnecting laser(s) {wls} automatically.")
        elif QMessageBox.question(self, "Laser Auto-Connect",
                                  f"The following lasers were connected last time:\n[{', '.join(wls)}]\n\n"
                                  "Do you want to restore these connections?") != QMessageBox.Yes:
            return
        for wl in wls:
            ok, _msg = ctl.connect(wl)
            if ok and ctl.status(wl).get("ld_on", False):
                if QMessageBox.question(self, "LD Status Alert",
                                        f"[ {wl} ] Laser LD is currently ON.\n\nDo you want to turn it OFF now?") \
                        == QMessageBox.Yes:
                    ctl.inst[wl].set_ld_on(False)
                    ctl.log(wl, f"Safety: {wl} LD turned OFF by user request.")
                else:
                    ctl.log(wl, f"Warning: {wl} LD remains ON as per user request.")

    def closeEvent(self, e):
        from core import applog
        applog.log(f"[INFO] closeEvent: spontaneous={e.spontaneous()} rebuilding={self._rebuilding} closing={self._closing}")
        from core.update import UPDATER
        if self._rebuilding or UPDATER.restarting:
            return super().closeEvent(e)
        e.ignore()
        if self._closing:
            return
        from core.update import UPDATER
        busy = UPDATER.busy_reason()
        text = "Are you sure you want to exit the program?"
        if busy:
            text += f"\n\nNote: {busy}. Exiting stops it."
        if QMessageBox.question(self, "Exit", text, QMessageBox.Ok | QMessageBox.Cancel) != QMessageBox.Ok:
            applog.log("[INFO] Exit dialog: Cancel (staying open)")
            return
        applog.log("[INFO] Exit dialog: Ok (shutting down)")
        self._closing = True
        self._shutdown()

    def _shutdown(self):
        """As the Tk app's on_closing: stop console jobs, then motors -> lasers -> UPS with a progress window.
        Lasers keep their LD/TEC state (only the USB handles are released)."""
        from PyQt5.QtWidgets import QDialog, QProgressBar
        from core import applog
        applog.log("Shutting down... Releasing hardware resources.")
        applog.log("=== Application Closing Process ===")
        for slot, job in list(JOBS.jobs.items()):
            if job.state == "running" and JOBS.stop(slot):
                applog.log(f"[INFO] Terminated running console job [{slot}] on exit.")
        dlg = QDialog(self, Qt.Dialog | Qt.CustomizeWindowHint | Qt.WindowTitleHint)
        dlg.setWindowTitle("Shutting Down")
        dlg.setModal(True)
        dlg.setFixedWidth(400)
        v = QVBoxLayout(dlg)
        title = QLabel("System Shutdown in Progress...")
        title.setStyleSheet("font-size: 12pt; font-weight: 700;")
        v.addWidget(title)
        msg = QLabel("Initializing...")
        v.addWidget(msg)
        bar = QProgressBar()
        bar.setTextVisible(False)
        v.addWidget(bar)
        dlg.show()

        def step(text, value):
            msg.setText(text)
            bar.setValue(value)
            QApplication.processEvents()

        def motors():
            step("Stopping PMT movement... Please wait.", 25)
            from core.stage import stage
            st = stage()
            if st.mgr is not None and st.owns_drives():      # never command drives the Tk app owns
                try:
                    applog.log("[INFO] Sending STOP commands to all motors before exit...")
                    st.mgr.stop_rotation(2)
                    st.mgr.stop_rotation(3)
                except Exception as ex:
                    applog.log(f"[ERROR] Failed to stop motors on exit: {ex}")
            QTimer.singleShot(1000, lasers)

        def lasers():
            step("Disconnecting Lasers...", 60)
            if not env.NO_HW:
                from core import laser as L
                ctl = L.laser()
                if ctl.owns():
                    try:
                        ctl._save_connections()
                    except Exception as ex:
                        applog.log(f"[WARNING] Error saving config on exit: {ex}")
                    for wl in L.WAVELENGTHS:
                        if ctl.connected(wl):
                            try:
                                ctl.disconnect(wl, user=False)
                                applog.log(f"[INFO] Laser {wl} safely disconnected.")
                            except Exception as ex:
                                applog.log(f"[WARNING] Error disconnecting Laser {wl}: {ex}")
            QTimer.singleShot(600, ups_close)

        def ups_close():
            step("Closing UPS connection...", 90)
            if not env.NO_HW:
                from core import ups
                svc = ups.service()
                if svc.owns:
                    try:
                        svc.stop()
                        applog.log("[INFO] UPS serial port safely closed.")
                    except Exception as ex:
                        applog.log(f"[WARNING] Error closing UPS port: {ex}")
            QTimer.singleShot(600, finish)

        def finish():
            step("Safe to exit. Goodbye!", 100)
            applog.log("[INFO] Goodbye!")
            ACCESS.lock()
            QTimer.singleShot(500, lambda: os._exit(0))   # as the Tk app: no worker thread keeps the process alive

        QTimer.singleShot(100, motors)

    # ---------------------------------------------------------------- menus
    def _menus(self):
        mb = self.menuBar()
        files = mb.addMenu("Files")
        files.addAction("Load Config...", self._load_config)
        files.addAction("Reload Config", self._rebuild)
        files.addAction("Use the Tk App's Config", self._default_config)
        files.addSeparator()
        files.addAction("Exit", self.close)
        about = mb.addMenu("About")
        about.addAction("About DAQ Control", self._about)
        view = mb.addMenu("View")
        dark = view.addAction("Dark Mode")
        dark.setCheckable(True)
        dark.setChecked(theme.DARK_MODE)
        dark.toggled.connect(self._set_dark)
        view.addAction("Hide / Show Left Panel", lambda: self.toggle_left(), QKeySequence("Ctrl+B"))

    def _load_config(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load Config", os.path.dirname(env.config_file()),
                                              "Header files (*.h);;All files (*)")
        if not path:
            return
        try:
            n = len(config.parse(path))
        except (OSError, UnicodeDecodeError) as e:
            QMessageBox.critical(self, "Load Config", f"Could not read {path}:\n{e}")
            return
        if n == 0:
            QMessageBox.warning(self, "Load Config", f"No settings found in {path}.")
            return
        env.save_setting("config_path", path)
        self._rebuild()

    def _default_config(self):
        env.save_setting("config_path", None)
        self._rebuild()

    def _about(self):
        QMessageBox.about(self, "About DAQ Control",
                          (site_secrets.get("about_text") or "DAQ Control Application\n\n") +
                          f"Config: {env.config_file()}")

    def _set_dark(self, on):
        env.save_setting("dark_mode", bool(on))
        theme.set_dark(on)
        theme.restyle(QApplication.instance())
        self._rebuild()

    def _rebuild(self):
        """Build a fresh window (all pages re-read the config and the palette); jobs, Unlock and the
        stage connection live in core and carry over."""
        new = MainWindow()
        new.setGeometry(self.geometry())
        if self.isMaximized():
            new.showMaximized()
        else:
            new.show()
        QApplication.instance()._main_window = new
        self._rebuilding = True
        self.close()
        self.deleteLater()

    def _open_hv_monitor(self):
        if QGuiApplication.platformName() == "offscreen":   # tests: never touch real windows
            return
        from window_focus import HV_TITLE, focus_window
        if focus_window(HV_TITLE) != "raised":
            QMessageBox.information(self, "HV Monitor", "The HV Monitor is not open.\n"
                                                        "Start it from the launcher.")

    def toggle_left(self):
        """Fold the left panel away for a wide view, or bring it back at its last width."""
        sizes = self.split.sizes()
        if sizes[0] > 0:
            self._left_w = sizes[0]
            self.split.setSizes([0, sum(sizes)])
        else:
            self.split.setSizes([self._left_w, max(sum(sizes) - self._left_w, 1)])
        self._paint_left_btn()

    def _paint_left_btn(self):
        shown = self.split.sizes()[0] > 0
        self.left_btn.setText("\u25C0  Panel" if shown else "\u25B6  Panel")
        self.left_btn.setToolTip(("Hide" if shown else "Show") + " the left panel (Ctrl+B)")

    def _left_panel(self):
        from pages.daq.left_panel import LeftPanel
        self.left = LeftPanel()
        self.left.navigate = self.show_page
        return self.left

    def show_page(self, name):
        """Bring a DAQ System page, a General Scan sub tab or a main tab to the front (builds it on first use)."""
        name = PAGE_ALIASES.get(name, name)
        if name in DAQ_TABS:
            self.main_tabs.setCurrentIndex(MAIN_TABS.index(DAQ_TAB))
            self.daq_tabs.setCurrentIndex(DAQ_TABS.index(name))
        elif name in GS_TABS:
            self.show_page("General Scan")
            self.gs_tabs.setCurrentIndex(GS_TABS.index(name))
        elif name in MAIN_TABS:
            self.main_tabs.setCurrentIndex(MAIN_TABS.index(name))

    def _jobs_lamp(self, *_):
        i = DAQ_TABS.index("Acquisition")
        if self.daq_tabs.tabBar().tabButton(i, QTabBar.RightSide) is None:
            self.daq_tabs.tabBar().setTabButton(i, QTabBar.RightSide, basic.make_lamp(10))
        lamp = self.daq_tabs.tabBar().tabButton(i, QTabBar.RightSide)
        running = JOBS.any_running()
        lamp.setVisible(running)
        if running:
            basic.paint_lamp(lamp, "#1a7f37")

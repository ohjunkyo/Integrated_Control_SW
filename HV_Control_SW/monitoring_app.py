import sys, json, os, time, signal, sqlite3, csv
from datetime import datetime, timedelta 
from PyQt5.QtWidgets import QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton, QDialog, QLineEdit, QComboBox, QDoubleSpinBox, QTabWidget, QDateTimeEdit, QFileDialog, QCheckBox, QFrame, QGroupBox, QScrollArea, QSizePolicy, QMessageBox, QTableWidget, QTableWidgetItem, QHeaderView, QPlainTextEdit
from PyQt5.QtCore import QTimer, Qt, pyqtSignal, QDateTime
from PyQt5.QtGui import QFont, QIcon
import pyqtgraph as pg
import numpy as np
from worker_manager import WorkerManager
from database_manager import DatabaseManager
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from window_focus import focus_window, is_running, DAQ_TITLE, DAQ_PROC, HV_TITLE, V2_TITLE, V2_PROC
import hv_request_file

# Dark Box #1/#2 were swapped in config_precal.json's arduino_settings.sensors
# on 2026-07-27 17:42 (pin 2 was mislabeled "Dark Box #1", pin 3 "Dark Box #2" --
# physically it's the other way around). Data logged BEFORE this timestamp has
# the two swapped; data logged AFTER is correct. Drawn as a vertical marker on
# the T/H Overlay History plot (see load_and_plot_data) so a time range that
# straddles the fix is never misread as one continuous, consistently-labeled
# series.
DARKBOX_RELABEL_TS = datetime(2026, 7, 27, 17, 42, 0).timestamp()

def _flash(btn, ms=300):
    """Hold a button in its pressed look briefly, so a click is visible."""
    btn.setDown(True)
    QTimer.singleShot(ms, lambda: btn.setDown(False))


class AxisRangeDialog(QDialog):
    """Axis ranges of one plot; Apply keeps the window open."""
    def __init__(self, plot, title, fit_y, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Axis Range - {title}")
        self.vb = plot.getPlotItem().vb
        self.boxes = [self.vb] + ([plot.dual_viewbox] if getattr(plot, 'dual_viewbox', None) is not None else [])
        self.fit_y, self.plot = fit_y, plot
        font = QFont(); font.setPointSize(13)
        self.setFont(font)
        lay = QGridLayout(self)
        lay.setHorizontalSpacing(8); lay.setVerticalSpacing(8)
        self.x0, self.x1 = QDateTimeEdit(), QDateTimeEdit()
        for e in (self.x0, self.x1):
            e.setDisplayFormat("yyyy-MM-dd HH:mm:ss"); e.setCalendarPopup(True)
        lay.addWidget(QLabel("Time from"), 0, 0); lay.addWidget(self.x0, 0, 1)
        lay.addWidget(QLabel("to"), 0, 2); lay.addWidget(self.x1, 0, 3)
        quick = QHBoxLayout()
        for text, hours in (("Last 1 h", 1), ("Last 6 h", 6), ("Last 24 h", 24), ("Last 3 d", 72)):
            b = QPushButton(text)
            b.setAutoDefault(False)
            b.clicked.connect(lambda _, h=hours: self._last(h))
            quick.addWidget(b)
        lay.addLayout(quick, 1, 1, 1, 3)
        self.y_edits = []
        for i, name in enumerate(["Y" if len(self.boxes) == 1 else "Left Y", "Right Y"][:len(self.boxes)]):
            lo, hi = QLineEdit(), QLineEdit()
            for e in (lo, hi):
                e.returnPressed.connect(self.apply)
            lay.addWidget(QLabel(f"{name} min"), 2 + i, 0); lay.addWidget(lo, 2 + i, 1)
            lay.addWidget(QLabel("max"), 2 + i, 2); lay.addWidget(hi, 2 + i, 3)
            self.y_edits.append((lo, hi))
        self.msg = QLabel(""); self.msg.setStyleSheet("color: #ff7b72;")
        lay.addWidget(self.msg, 4, 0, 1, 4)
        row = QHBoxLayout(); row.addStretch(1)
        fit, apply, close = QPushButton("Fit Y to Time Range"), QPushButton("Apply"), QPushButton("Close")
        fit.setAutoDefault(False); close.setAutoDefault(False); apply.setDefault(True)
        fit.clicked.connect(self.fit); apply.clicked.connect(self.apply); close.clicked.connect(self.close)
        for b in (fit, apply, close):
            row.addWidget(b)
        lay.addLayout(row, 5, 0, 1, 4)

    def load(self):
        """Fill the boxes with the ranges currently shown."""
        x0, x1 = self.vb.viewRange()[0]
        self.x0.setDateTime(QDateTime.fromSecsSinceEpoch(int(x0)))
        self.x1.setDateTime(QDateTime.fromSecsSinceEpoch(int(round(x1))))
        for vb, (lo, hi) in zip(self.boxes, self.y_edits):
            y0, y1 = vb.viewRange()[1]
            lo.setText(f"{y0:.6g}"); hi.setText(f"{y1:.6g}")
        self.msg.setText("")

    def _last(self, hours):
        now = QDateTime.currentDateTime()
        self.x0.setDateTime(now.addSecs(-3600 * hours)); self.x1.setDateTime(now)
        self.apply(fit=True)

    def _set_x(self):
        x0, x1 = self.x0.dateTime().toSecsSinceEpoch(), self.x1.dateTime().toSecsSinceEpoch()
        if x1 <= x0:
            self.msg.setText("Time 'to' must be after 'from'.")
            return False
        self.vb.setXRange(x0, x1, padding=0)
        return True

    def apply(self, fit=False):
        if not fit:
            try:
                ys = [(float(lo.text()), float(hi.text())) for lo, hi in self.y_edits]
            except ValueError:
                self.msg.setText("Y min / max must be numbers.")
                return
            if any(b <= a for a, b in ys):
                self.msg.setText("Y min must be below Y max.")
                return
        if not self._set_x():
            return
        if fit:
            self.fit_y(self.plot)
        else:
            for vb, (a, b) in zip(self.boxes, ys):
                vb.setYRange(a, b, padding=0)
        self.load()

    def fit(self):
        self.apply(fit=True)


class HVControlPanel(QDialog):
    control_signal = pyqtSignal(str, int, int, str, object)
    def __init__(self, channels, hv_params, styles, parent=None):
        super().__init__(parent); self.setWindowTitle("HV Control Panel"); self.hv_params = hv_params
        self.setStyleSheet(f"background-color: {styles['background_color']}; color: {styles['font_color_main']};")
        self.layout = QGridLayout(self)
        font = QFont(); font.setPointSize(styles['font_size_medium'])
        feedback_font = QFont(); feedback_font.setPointSize(styles['font_size_large']); feedback_font.setBold(True)
        self.channel_selector = QComboBox(); self.channel_selector.setFont(font)
        self.channel_selector.addItems([str(ch) for ch in channels])
        self.voltage_input = QDoubleSpinBox(); self.voltage_input.setRange(0, 8000); self.voltage_input.setFont(font)
        self.current_input = QDoubleSpinBox(); self.current_input.setRange(0, 1000); self.current_input.setFont(font)
        self.set_voltage_btn, self.set_current_btn = QPushButton("Set Voltage"), QPushButton("Set Current")
        self.refresh_btn = QPushButton("Refresh")
        self.set_voltage_btn.setFont(font); self.set_current_btn.setFont(font); self.refresh_btn.setFont(font)
        for b in (self.set_voltage_btn, self.set_current_btn, self.refresh_btn):
            b.setStyleSheet("""
            QPushButton { background-color: #3a3f4b; color: white; border: 1px solid #6b7280; border-radius: 3px; padding: 5px; }
            QPushButton:hover { background-color: #4a5160; border-color: #9aa5b1; }
            QPushButton:pressed { background-color: #3b82f6; border: 2px inset #1e3a8a; }
        """)
        self.power_on_btn, self.power_off_btn = QPushButton("Turn ON"), QPushButton("Turn OFF")
        self.power_on_btn.setStyleSheet("""
            QPushButton { background-color: lightgreen; color: black; border: 1px solid #777777; border-radius: 3px; padding: 5px; }
            QPushButton:hover { background-color: #b4f5b4; border-color: #ffffff; }
            QPushButton:pressed { background-color: #3f9e3f; color: white; border: 2px inset #555555; }
        """)

        self.power_off_btn.setStyleSheet("""
            QPushButton { background-color: lightcoral; color: black; border: 1px solid #777777; border-radius: 3px; padding: 5px; }
            QPushButton:hover { background-color: #f6a5a5; border-color: #ffffff; }
            QPushButton:pressed { background-color: #b83b3b; color: white; border: 2px inset #555555; }
        """)
        self.clear_alarm_btn = QPushButton("Clear Alarm")
        self.clear_alarm_btn.setFont(font)
        self.clear_alarm_btn.setStyleSheet("""
            QPushButton { background-color: #FFB347; color: black; border: 1px solid #777777; border-radius: 3px; padding: 5px; }
            QPushButton:hover { background-color: #ffc978; border-color: #ffffff; }
            QPushButton:pressed { background-color: #c47a12; color: white; border: 2px inset #555555; }
        """)
        self.feedback_label = QLabel("Status: Ready"); self.feedback_label.setFont(feedback_font)
        for label_text, widget, row in [("Target Channel:", self.channel_selector, 0), ("Set Voltage (V):", self.voltage_input, 1), ("Set Current (uA):", self.current_input, 2)]:
            label = QLabel(label_text); label.setFont(font); self.layout.addWidget(label, row, 0)
            self.layout.addWidget(widget, row, 1)
        self.layout.addWidget(self.refresh_btn, 0, 2)
        self.layout.addWidget(self.set_voltage_btn, 1, 2); self.layout.addWidget(self.set_current_btn, 2, 2)
        self.layout.addWidget(self.power_on_btn, 3, 0, 1, 3); self.layout.addWidget(self.power_off_btn, 4, 0, 1, 3)
        self.layout.addWidget(self.clear_alarm_btn, 5, 0, 1, 3)
        self.layout.addWidget(self.feedback_label, 6, 0, 1, 3)
        #self.layout.addWidget(self.feedback_label, 5, 0, 1, 3)
        self.set_voltage_btn.clicked.connect(self.set_voltage); self.set_current_btn.clicked.connect(self.set_current)
        self.power_on_btn.clicked.connect(self.turn_on); self.power_off_btn.clicked.connect(self.turn_off)
        self.channel_selector.currentIndexChanged.connect(self.request_settings_for_channel)
        self.clear_alarm_btn.clicked.connect(self.clear_alarm)
        self.refresh_btn.clicked.connect(self.refresh)
        for btn in (self.set_voltage_btn, self.set_current_btn, self.power_on_btn, self.power_off_btn,
                    self.clear_alarm_btn, self.refresh_btn):
            btn.setAutoDefault(False); btn.setDefault(False)
            btn.setFocusPolicy(Qt.NoFocus)   # a key press can never fire an HV command through button focus
        self.voltage_input.lineEdit().returnPressed.connect(self.set_voltage)
        self.current_input.lineEdit().returnPressed.connect(self.set_current)

    def _sending(self, btn, what):
        _flash(btn)
        self.feedback_label.setStyleSheet("color: #f1fa8c;")
        self.feedback_label.setText(f"Status: Sending {what} ...")
    def clear_alarm(self):
        self._sending(self.clear_alarm_btn, "Clear Alarm")
        self.control_signal.emit('clear_alarm', 0, 0, '', 0)
    def get_ch(self): return int(self.channel_selector.currentText())
    def set_voltage(self):
        self._sending(self.set_voltage_btn, f"Ch{self.get_ch()} VSet = {self.voltage_input.value():.2f} V")
        self.control_signal.emit('set_param', 0, self.get_ch(), self.hv_params['v_set'], self.voltage_input.value())
    def set_current(self):
        self._sending(self.set_current_btn, f"Ch{self.get_ch()} ISet = {self.current_input.value():.2f} uA")
        self.control_signal.emit('set_param', 0, self.get_ch(), self.hv_params['i_set'], self.current_input.value())
    def turn_on(self):
        self._sending(self.power_on_btn, f"Ch{self.get_ch()} Turn ON")
        self.control_signal.emit('set_param', 0, self.get_ch(), self.hv_params['pw'], 1)
    def turn_off(self):
        self._sending(self.power_off_btn, f"Ch{self.get_ch()} Turn OFF")
        self.control_signal.emit('set_param', 0, self.get_ch(), self.hv_params['pw'], 0)
    def update_feedback(self, msg):
        ok = msg.startswith("Success")
        self.feedback_label.setStyleSheet("color: #90EE90;" if ok else "color: #ff7b72;")
        self.feedback_label.setText(f"Status: [{datetime.now():%H:%M:%S}] {msg}")
    def request_settings_for_channel(self): self.control_signal.emit('fetch_settings', 0, self.get_ch(), '', '')
    def refresh(self):
        """Read VSet / ISet of the selected channel back from the crate into the fields."""
        _flash(self.refresh_btn)
        self.feedback_label.setStyleSheet("color: #f1fa8c;")
        self.feedback_label.setText(f"Status: Reading Ch{self.get_ch()} ...")
        self.request_settings_for_channel()
    def set_initial_values(self, settings):
        ch = self.get_ch()
        if ch not in settings: return
        self.voltage_input.setValue(settings[ch]['v_set']); self.current_input.setValue(settings[ch]['i_set'])
        self.feedback_label.setStyleSheet("color: #90EE90;")
        self.feedback_label.setText(f"Status: [{datetime.now():%H:%M:%S}] Ch{ch} VSet {settings[ch]['v_set']:.1f} V, "
                                    f"ISet {settings[ch]['i_set']:.1f} uA")


class HVConfigDialog(QDialog):
    """Edit the HV link section of a config file; optionally restart the monitor with it."""
    LINKS = ["TCPIP", "RS232", "CAENET", "USB", "OPTLINK", "USB_VCP", "USB3", "A4818"]
    SYSTEMS = ["SY1527", "SY2527", "SY4527", "SY5527", "N568", "V65XX", "N1470", "V8100", "N568E", "DT55XX",
               "FTK", "DT55XXE", "N1068", "SMARTHV", "NGPS", "N1168", "R6060"]

    def __init__(self, config_path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("HV Config"); self.setMinimumWidth(560)
        self.setWindowFlags(self.windowFlags() | Qt.WindowMinMaxButtonsHint)
        self.dir = os.path.dirname(os.path.abspath(config_path))
        lay = QGridLayout(self)
        self.file_box = QComboBox(); self.file_box.setEditable(False)
        names = sorted(f for f in os.listdir(self.dir) if f.startswith("config") and f.endswith(".json"))
        cur = os.path.basename(config_path)
        if cur not in names: names.append(cur)
        self.file_box.addItems(names); self.file_box.setCurrentText(cur)
        self.running = cur
        self.system = QComboBox(); self.system.addItems(self.SYSTEMS)
        self.link = QComboBox(); self.link.addItems(self.LINKS)
        self.arg = QLineEdit(); self.chs = QLineEdit()
        self.hint = QLabel(); self.hint.setWordWrap(True); self.hint.setStyleSheet("color: #aaaaaa;")
        rows = [("Config file", self.file_box), ("System type", self.system), ("Link type", self.link),
                ("Connection argument", self.arg), ("Channels (comma separated)", self.chs)]
        for r, (t, w) in enumerate(rows):
            lay.addWidget(QLabel(t), r, 0); lay.addWidget(w, r, 1)
        lay.addWidget(self.hint, len(rows), 0, 1, 2)
        self.msg = QLabel(""); self.msg.setWordWrap(True)
        lay.addWidget(self.msg, len(rows) + 1, 0, 1, 2)
        bl = QHBoxLayout()
        self.save_btn = QPushButton("Save"); self.restart_btn = QPushButton("Save && Restart"); close_btn = QPushButton("Close")
        for b in (self.save_btn, self.restart_btn, close_btn): bl.addWidget(b)
        lay.addLayout(bl, len(rows) + 2, 0, 1, 2)
        self.file_box.currentTextChanged.connect(self.load)
        self.link.currentTextChanged.connect(self._hint)
        self.save_btn.clicked.connect(lambda: self.save(False))
        self.restart_btn.clicked.connect(lambda: self.save(True))
        close_btn.clicked.connect(self.close)
        self.load(cur)

    def _path(self): return os.path.join(self.dir, self.file_box.currentText())

    def _hint(self, _=None):
        t = {"TCPIP": "IP address, e.g. 192.168.10.209",
             "USB_VCP": "port_baud_data_stop_parity_lbus, e.g. ttyUSB1_115200_8_1_0_0 (parity 0 = None)",
             "RS232": "port_baud_data_stop_parity_lbus, e.g. ttyUSB1_9600_8_1_0_0 (parity 0 = None)",
             "USB": "LinkNum_ConetNode_VMEBaseAddress", "OPTLINK": "LinkNum_ConetNode_VMEBaseAddress"}
        self.hint.setText(t.get(self.link.currentText(), ""))

    def load(self, name):
        try:
            with open(os.path.join(self.dir, name), encoding="utf-8") as f: h = json.load(f)["caen_hv_settings"]
        except Exception as e:
            self.msg.setText(f"Cannot read {name}: {e}"); self.msg.setStyleSheet("color: #ff7b72;"); return
        self.system.setCurrentText(h.get("system_type", "")); self.link.setCurrentText(h.get("link_type", ""))
        self.arg.setText(str(h.get("connection_argument", ""))); self.chs.setText(",".join(str(c) for c in h.get("channels_to_monitor", [])))
        self._hint(); self.msg.setText("")

    def save(self, restart):
        path = self._path()
        try:
            chs = [int(x) for x in self.chs.text().replace(" ", "").split(",") if x != ""]
            if not chs: raise ValueError("no channels")
            with open(path, encoding="utf-8") as f: cfg = json.load(f)
            arch = os.path.join(self.dir, "_archive", "config_backups"); os.makedirs(arch, exist_ok=True)
            with open(os.path.join(arch, f"{os.path.basename(path)}.{time.strftime('%Y%m%d_%H%M%S')}"), "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
            h = cfg["caen_hv_settings"]
            h["system_type"] = self.system.currentText(); h["link_type"] = self.link.currentText()
            h["connection_argument"] = self.arg.text().strip(); h["channels_to_monitor"] = chs
            with open(path, "w", encoding="utf-8") as f: json.dump(cfg, f, indent=2)
        except Exception as e:
            self.msg.setText(f"Save failed: {e}"); self.msg.setStyleSheet("color: #ff7b72;"); return
        self.msg.setText(f"Saved {os.path.basename(path)}"); self.msg.setStyleSheet("color: #50fa7b;")
        if restart:
            self.done(2); self.parent().restart_with(path)
        elif os.path.basename(path) == self.running:
            self.msg.setText(f"Saved {os.path.basename(path)} -- restart the HV Monitor to apply")

class MonitoringApp(QMainWindow):
    def __init__(self, config):
        super().__init__()
        self.config = config; self.styles = config['ui_styles']
        self.latest_data = {'sensors': {}, 'hv': {}}
        self.env_port_open = False; self.env_port_open_since = 0.0; self.last_env_valid_time = 0.0; self.env_status_text = None
        self.graph_data = {'time': [], 'temp': {}, 'humi': {}, 'volt': {}, 'curr': {}}
        self.is_dual_current = 'i_mon_low' in self.config['caen_hv_settings']['parameters']
        pg.setConfigOption('background', self.styles['background_color']); pg.setConfigOption('foreground', self.styles['font_color_main'])
        self.plot_colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf']
        self._is_closing = False
        self._no_current_since = {}     # ch -> time the no-current condition started
        self._no_current_alerted = set()  # channels already popped up for this occurrence
        self._power_fail_seen = None
        self._hv_request = None          # accepted request, until the crate worker replies
        self._hv_request_sent = 0.0
        self._hv_link_off = False        # operator closed the software link to the crate
        self.hv_status_msg = ""; self.started_at = time.time()
        self.temp_colors = ['#ff4c4c', '#ff8533', '#ffb366', '#ff66b2', '#ff3399']  # Warm shades
        self.humi_colors = ['#33ccff', '#33ffcc', '#3366ff', '#00ecc6', '#50fa7b']  # Cool shades
        
        self.db_manager = DatabaseManager(f"{config['logging_options']['log_file_prefix']}.db", config)
        self.worker_manager = WorkerManager(self.config)
        
        self.setup_ui()
        self._load_initial_graph_data() 
        self._draw_all_monitor_curves() 
        self.connect_signals()
        self.setup_timers()
        self.worker_manager.start_workers()

    def create_dual_y_plot(self, left_label="Left Axis", right_label="Right Axis", right_range=None, right_color='blue'):
        label_font_size = self.styles.get('font_size_medium', 16)
        tick_font_size = label_font_size - 2
        
        tick_font = QFont(); tick_font.setPointSize(tick_font_size)
        label_font = QFont(); label_font.setPointSize(label_font_size)
        
        plot_widget = pg.PlotWidget(axisItems={'bottom': pg.DateAxisItem(orientation='bottom')})
        p1 = plot_widget.getPlotItem()
        
        p1.getAxis('left').setLabel(left_label)
        p1.getAxis('left').label.setFont(label_font)
        p1.getAxis('left').setTickFont(tick_font)
        p1.getAxis('bottom').setTickFont(tick_font)

        p2 = pg.ViewBox()
        p1.scene().addItem(p2)
        p1.getAxis('right').linkToView(p2)
        p2.setXLink(p1)
        if right_range:
            p2.setYRange(right_range[0], right_range[1]) 
        else:
            p2.enableAutoRange(axis='y', enable=True)

        axis2 = pg.AxisItem('right')
        axis2.setLabel(right_label, color=right_color)
        axis2.label.setFont(label_font)
        axis2.setTickFont(tick_font)
        
        p1.layout.addItem(axis2, 2, 3) 
        axis2.linkToView(p2)

        def update_views():
            p2.setGeometry(p1.vb.sceneBoundingRect())
        
        p1.vb.sigResized.connect(update_views)
        
        plot_widget.dual_viewbox = p2 
        return plot_widget

    def setup_ui(self):
        self.setWindowTitle(self.config['ui_options']['window_title']); self.setGeometry(100, 100, 1800, 950)
        # Flat, neutral-dark design system (accent-blue on charcoal) shared by
        # both tabs -- one palette so buttons/checkboxes/date pickers/cards
        # read as one system instead of each control having its own look.
        ACCENT = "#3b82f6"
        BG = self.styles['background_color']
        CARD = "#242424"
        BORDER = "#3a3a3a"
        TEXT = self.styles['font_color_main']
        self.setStyleSheet(f"""
            QWidget {{
                background-color: {BG};
                color: {TEXT};
                font-family: -apple-system, "Segoe UI", "Helvetica Neue", Arial, sans-serif;
            }}
            QLabel, QCheckBox {{
                color: {TEXT};
            }}
            QCheckBox {{
                spacing: 8px;
                padding: 2px 0px;
            }}
            QCheckBox::indicator {{
                width: 15px; height: 15px;
                border: 1px solid #666666;
                border-radius: 3px;
                background-color: #333333;
            }}
            QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}
            QCheckBox::indicator:checked {{
                background-color: {ACCENT};
                border-color: {ACCENT};
            }}
            QGroupBox {{
                border: 1px solid {BORDER};
                border-radius: 8px;
                margin-top: 14px;
                padding: 10px 8px 8px 8px;
                background-color: {CARD};
                font-weight: bold;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 6px;
                color: #9aa5b1;
            }}
            QComboBox, QDoubleSpinBox, QDateTimeEdit {{
                color: {TEXT};
                background-color: #333333;
                border: 1px solid {BORDER};
                border-radius: 5px;
                padding: 4px 6px;
            }}
            QComboBox:focus, QDoubleSpinBox:focus, QDateTimeEdit:focus {{
                border: 1px solid {ACCENT};
            }}
            QPushButton {{
                color: {TEXT};
                background-color: #333333;
                border: 1px solid {BORDER};
                border-radius: 6px;
                padding: 7px 14px;
                font-weight: 600;
            }}
            QPushButton:hover {{ background-color: #3d3d3d; border-color: #555555; }}
            QPushButton:pressed {{ background-color: #2a2a2a; }}
            QTabWidget::pane {{
                border-top: 1px solid {BORDER};
            }}
            QTabBar::tab {{
                background: transparent;
                color: #9aa5b1;
                padding: 10px 18px;
                font-weight: 600;
            }}
            QTabBar::tab:selected {{
                color: {TEXT};
                border-bottom: 2px solid {ACCENT};
            }}
        """)
        self.tabs = QTabWidget(); self.setCentralWidget(self.tabs)
        self.monitor_tab = QWidget(); self.analysis_tab = QWidget()
        self.diag_tab = QWidget()
        self.tabs.addTab(self.monitor_tab, "Monitoring"); self.tabs.addTab(self.analysis_tab, "Data analysis"); self.tabs.addTab(self.diag_tab, "Diagnostic")
        self.setup_monitor_ui(); self.setup_analysis_ui(); self.setup_diagnostic_ui()


    def setup_monitor_ui(self):
        main_layout = QVBoxLayout(self.monitor_tab)
        font_large = QFont(); font_large.setPointSize(self.styles.get('font_size_large', 14))

        top_dashboard_frame = QFrame()
        top_dashboard_frame.setStyleSheet("""
            QFrame { border: 1px solid #3a3a3a; border-radius: 8px; background-color: #242424; margin-bottom: 5px; }
            QLabel { border: none; background: transparent; padding: 2px; }
        """)
        
        dash_layout = QGridLayout(top_dashboard_frame)
        dash_layout.setContentsMargins(15, 15, 15, 15)
        dash_layout.setSpacing(15)

        # 1-1. 좌측: ENV
        self.env_status_label = QLabel("ENV Status: Waiting...")
        self.env_status_label.setFont(font_large)
        self.env_status_label.setStyleSheet("color: #8be9fd;")
        self.env_status_label.setWordWrap(True)
        self.env_status_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        
        # 1-2. 중앙: HV 연결 상태 및 Logging
        self.hv_status_label = QLabel("Connecting to HV...")
        self.hv_status_label.setFont(font_large)
        self.hv_status_label.setStyleSheet("color: white;")
        self.hv_status_label.setAlignment(Qt.AlignBottom | Qt.AlignLeft)
        
        self.log_status_label = QLabel("Logging: 0 point(s) collected")
        self.log_status_label.setFont(font_large)
        self.log_status_label.setStyleSheet("color: #f1fa8c;")
        self.log_status_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        
        # 1-3. 우측: 인터락 인디케이터 및 버튼들
        self.interlock_indicator = QLabel("HV: NORMAL")
        self.interlock_indicator.setFont(font_large)
        self.interlock_indicator.setAlignment(Qt.AlignCenter)
        self.interlock_indicator.setStyleSheet("background-color: #2ca02c; color: white; font-weight: bold; border-radius: 4px; padding: 5px;")
        
        self.control_panel_btn = QPushButton("Open HV Control Panel")
        self.control_panel_btn.setFont(font_large)
        self.control_panel_btn.setMinimumHeight(35)
        self.control_panel_btn.setStyleSheet("""
            QPushButton { background-color: #6272a4; color: white; border-radius: 4px; font-weight: bold; padding: 5px; }
            QPushButton:hover { background-color: #7384bb; }
            QPushButton:pressed { background-color: #44507a; border: 2px inset #555555; }
        """)
        
        self.all_clear_alarm_btn = QPushButton("All Clear Alarm (Interlock)")
        self.all_clear_alarm_btn.setFont(font_large)
        self.all_clear_alarm_btn.setMinimumHeight(35)
        # [수정] pressed 옵션 추가로 클릭감 구현
        self.all_clear_alarm_btn.setStyleSheet("""
            QPushButton { background-color: #44475a; color: white; border-radius: 4px; font-weight: bold; padding: 5px; }
            QPushButton:pressed { background-color: #6272a4; border: 2px inset #555555; }
        """)
        self.all_clear_alarm_btn.clicked.connect(self.all_clear_alarm)

        self.goto_daq_btn = QPushButton("⇄ Go to DAQ Panel")
        self.goto_daq_btn.setFont(font_large)
        self.goto_daq_btn.setMinimumHeight(35)
        self.goto_daq_btn.setStyleSheet("""
            QPushButton { background-color: #007ACC; color: white; border-radius: 4px; font-weight: bold; padding: 5px; }
            QPushButton:hover { background-color: #1a8fe0; }
            QPushButton:pressed { background-color: #005a96; border: 2px inset #555555; }
        """)
        self.goto_daq_btn.clicked.connect(self.go_to_daq_panel)
        
        # 대시보드 내부 배치 (독립된 좌표계)
        dash_layout.addWidget(self.env_status_label, 0, 0, 2, 1)
        dash_layout.addWidget(self.hv_status_label, 0, 1, 1, 1)
        dash_layout.addWidget(self.log_status_label, 1, 1, 1, 1)
        dash_layout.addWidget(self.interlock_indicator, 0, 2, 1, 1)
        dash_layout.addWidget(self.control_panel_btn, 0, 3, 1, 1)
        dash_layout.addWidget(self.all_clear_alarm_btn, 1, 2, 1, 1)
        dash_layout.addWidget(self.goto_daq_btn, 1, 3, 1, 1)
        
        # 각 영역의 비율 고정 (ENV 넓게, 중앙 적당히, 우측 버튼들 고정)
        dash_layout.setColumnStretch(0, 2)
        dash_layout.setColumnStretch(1, 2)
        dash_layout.setColumnStretch(2, 1)
        dash_layout.setColumnStretch(3, 1)

        main_layout.addWidget(top_dashboard_frame)

        link_row = QHBoxLayout()
        self.env_reconnect_btn = QPushButton("Env"); self.env_reconnect_btn.setToolTip("Close and reopen only the ENV sensor serial link")
        self.hv_connect_btn = QPushButton("HV Connect"); self.hv_connect_btn.setToolTip("Reconnect the software link to the N1470 (HV output unchanged)")
        self.hv_disconnect_btn = QPushButton("Disconnect"); self.hv_disconnect_btn.setToolTip("Close the software link to the N1470 (HV output unchanged)")
        self.refresh_all_btn = QPushButton("Refresh"); self.refresh_all_btn.setToolTip("Read HV and ENV values now")
        self.hv_config_btn = QPushButton("HV Config"); self.hv_config_btn.setToolTip("Choose or edit the HV link settings (config file)")
        for b in (self.env_reconnect_btn, self.hv_connect_btn, self.hv_disconnect_btn, self.refresh_all_btn, self.hv_config_btn):
            b.setFont(font_large); link_row.addWidget(b)
            b.setStyleSheet("""
                QPushButton { background-color: #44475a; color: white; border: 1px solid #6272a4; border-radius: 4px; font-weight: bold; padding: 5px 14px; }
                QPushButton:hover { background-color: #565a73; }
                QPushButton:pressed { background-color: #6272a4; }
                QPushButton:disabled { background-color: #2a2a2a; color: #6b6b6b; border-color: #3a3a3a; }
            """)
        self.link_msg_label = QLabel(""); self.link_msg_label.setFont(font_large)
        link_row.addSpacing(12); link_row.addWidget(self.link_msg_label, 1)
        self.update_btn = QPushButton(); self.update_btn.setFont(font_large); self.update_btn.hide()
        self.update_btn.setStyleSheet("QPushButton { background-color: #e67700; color: #1f2430; font-weight: bold; border-radius: 4px; padding: 5px 14px; }"
                                      "QPushButton:hover { background-color: #f08c00; }")
        self.update_btn.clicked.connect(self.on_update_click)
        link_row.addWidget(self.update_btn)
        self.env_reconnect_btn.clicked.connect(self.env_reconnect)
        self.hv_connect_btn.clicked.connect(self.hv_connect)
        self.hv_disconnect_btn.clicked.connect(self.hv_disconnect)
        self.refresh_all_btn.clicked.connect(self.refresh_all)
        self.hv_config_btn.clicked.connect(self.open_hv_config)
        self.hv_connect_btn.setEnabled(False)
        main_layout.addLayout(link_row)

        self.blink_timer = QTimer(self)
        self.blink_timer.timeout.connect(self.toggle_button_blink)
        self.is_blink_on = False

        # ==========================================
        # 2. 센서(Dark Box T/H) 전용 한 줄 — HV 그리드와 분리해 한눈에 보이게
        # ==========================================
        sensor_frame = QFrame()
        sensor_frame.setStyleSheet("""
            QFrame { border: 1px solid #3a3a3a; border-radius: 8px; background-color: #242424; }
            QLabel { border: none; background: transparent; padding: 2px; }
        """)
        sensor_layout = QGridLayout(sensor_frame)
        sensor_layout.setContentsMargins(12, 8, 12, 8)
        sensor_layout.setSpacing(18)

        # 'name' stays the raw config name (used to build DB column names, e.g.
        # "Dark Box #1" -> Dark_Box_1_T, so renaming the display label never
        # fragments existing history); 'display' is what's actually shown.
        self.sensor_labels = {i: {'name': s['name'], 'display': s.get('display_name', s['name']),
                                   'temp': QLabel(f"{s.get('display_name', s['name'])} T: None"),
                                   'humi': QLabel("H: None")}
                               for i, s in enumerate(self.config['arduino_settings']['sensors'])}

        for i, labels in self.sensor_labels.items():
            labels['temp'].setFont(font_large); labels['humi'].setFont(font_large)
            labels['temp'].setStyleSheet(f"color: {self.styles['font_color_sensor']}; font-weight: bold;")
            labels['humi'].setStyleSheet(f"color: {self.styles['font_color_sensor']};")
            # One column pair per sensor, all on a single row — with only a
            # handful of sensors this reads far cleaner than the old 2-per-row
            # wrap (which left a lone sensor stranded on its own row).
            sensor_layout.addWidget(labels['temp'], 0, i * 2)
            sensor_layout.addWidget(labels['humi'], 0, i * 2 + 1)
        for col in range(len(self.sensor_labels) * 2):
            sensor_layout.setColumnStretch(col, 1)

        main_layout.addWidget(sensor_frame)

        # ==========================================
        # 3. 하단 데이터 라벨 전용 그리드 (전압/전류)
        # ==========================================
        status_layout = QGridLayout()
        status_layout.setContentsMargins(5, 10, 5, 10)
        status_layout.setSpacing(10)
        main_layout.addLayout(status_layout)

        self.hv_labels = {}
        base_row = 0
        headers_added = False
        
        for i, ch in enumerate(self.config['caen_hv_settings']['channels_to_monitor']):
            if not headers_added:
                volt_header = QLabel("Voltage (V):"); volt_header.setFont(font_large); volt_header.setStyleSheet(f"color: {self.styles['font_color_voltage']}; font-weight: bold;")
                status_layout.addWidget(volt_header, base_row, 0, 1, 2)
                
                if self.is_dual_current:
                    # Current L (IMonL, the low-range monitor) never carries a
                    # meaningful reading at these PMT dark-current levels --
                    # always pinned near 0 -- so it's dropped from the display
                    # in favor of a decoded channel Status row (ON/OFF/RAMP
                    # UP/RAMP DOWN/trip cause), which is actually actionable.
                    curH_header = QLabel("Current (uA):"); stat_header = QLabel("Status:")
                    for h in [curH_header, stat_header]: h.setFont(font_large); h.setStyleSheet(f"color: {self.styles['font_color_current']}; font-weight: bold;")
                    status_layout.addWidget(curH_header, base_row + 1, 0, 1, 2)
                    status_layout.addWidget(stat_header, base_row + 2, 0, 1, 2)
                else:
                    cur_header = QLabel("Current (uA):"); cur_header.setFont(font_large); cur_header.setStyleSheet(f"color: {self.styles['font_color_current']}; font-weight: bold;")
                    status_layout.addWidget(cur_header, base_row + 1, 0, 1, 2)
                headers_added = True

            self.hv_labels[ch] = {'v': QLabel("-"), 'i': QLabel("-"), 'ih': QLabel("-"), 'stat': QLabel("-")}
            for label in self.hv_labels[ch].values(): label.setFont(font_large)

            self.hv_labels[ch]['v'].setStyleSheet(f"color: {self.styles['font_color_voltage']};")
            self.hv_labels[ch]['i'].setStyleSheet(f"color: {self.styles['font_color_current']};")
            self.hv_labels[ch]['ih'].setStyleSheet(f"color: {self.styles['font_color_current']};")
            self.hv_labels[ch]['stat'].setStyleSheet("color: #2ca02c; font-weight: bold;")

            col_offset = (i * 2) + 2

            lbl_v = QLabel(f"Ch{ch}:"); lbl_v.setFont(font_large)
            status_layout.addWidget(lbl_v, base_row, col_offset); status_layout.addWidget(self.hv_labels[ch]['v'], base_row, col_offset + 1)

            if self.is_dual_current:
                lbl_ih = QLabel(f"Ch{ch}:"); lbl_ih.setFont(font_large)
                status_layout.addWidget(lbl_ih, base_row + 1, col_offset); status_layout.addWidget(self.hv_labels[ch]['ih'], base_row + 1, col_offset + 1)

                lbl_stat = QLabel(f"Ch{ch}:"); lbl_stat.setFont(font_large)
                status_layout.addWidget(lbl_stat, base_row + 2, col_offset); status_layout.addWidget(self.hv_labels[ch]['stat'], base_row + 2, col_offset + 1)
            else:
                lbl_i = QLabel(f"Ch{ch}:"); lbl_i.setFont(font_large)
                status_layout.addWidget(lbl_i, base_row + 1, col_offset); status_layout.addWidget(self.hv_labels[ch]['i'], base_row + 1, col_offset + 1)

        title_font_size_str = f"{self.styles.get('font_size_large', 18)}pt"
        legend_font_size = self.styles.get('font_size_legend', 10) 
        legend_font_size_str = f"{legend_font_size}pt"
        tick_font = QFont(); tick_font.setPointSize(self.styles.get('font_size_medium', 16) - 2) 
        label_font = QFont(); label_font.setPointSize(self.styles.get('font_size_medium', 16)) 
        
        graph_widget = QWidget(); graph_layout = QGridLayout(graph_widget)
        self.monitor_plots = {k: pg.PlotWidget(axisItems={'bottom': pg.DateAxisItem(orientation='bottom')}) for k in ['temp', 'humi', 'volt', 'curr']}

        for p in self.monitor_plots.values():
            legend = p.addLegend()
            legend.setLabelTextSize(legend_font_size_str)
            # Default pyqtgraph legend has no background/border, so it gets
            # lost against busy plot lines on the dark theme -- give it a
            # solid dark card + a light label color so it's actually legible.
            try:
                legend.setBrush(pg.mkBrush(20, 20, 20, 220))
                legend.setPen(pg.mkPen('#777777', width=1))
                legend.setLabelTextColor('#e0e0e0')
            except Exception:
                pass
            p.getAxis('bottom').setTickFont(tick_font)
            p.getAxis('left').setTickFont(tick_font)
            p.getAxis('left').label.setFont(label_font)

        self.monitor_plots['temp'].setTitle("Temperature", size=title_font_size_str)
        self.monitor_plots['humi'].setTitle("Humidity", size=title_font_size_str)
        self.monitor_plots['volt'].setTitle("HV Voltage", size=title_font_size_str)
        self.monitor_plots['curr'].setTitle("HV Current", size=title_font_size_str)

        # Plain 2x2 grid -- the old 3rd-row "Sensor T/H Overlay" combined
        # chart duplicated exactly what Temperature/Humidity above it already
        # show (both sensors, already overlaid on each), so it was dropped.
        graph_layout.addWidget(self.monitor_plots['temp'], 0, 0); graph_layout.addWidget(self.monitor_plots['humi'], 0, 1); graph_layout.addWidget(self.monitor_plots['volt'], 1, 0); graph_layout.addWidget(self.monitor_plots['curr'], 1, 1)

        for p in self.monitor_plots.values():
            if isinstance(p, pg.PlotWidget):
                p.getViewBox().setMouseMode(pg.ViewBox.RectMode)

        self.monitor_curves = {'temp': {}, 'humi': {}, 'volt': {}, 'curr': {}}

        for i, s in enumerate(self.config['arduino_settings']['sensors']):
            t_pen = pg.mkPen(color=self.temp_colors[i % len(self.temp_colors)], width=3)
            h_pen = pg.mkPen(color=self.humi_colors[i % len(self.humi_colors)], width=3)
            disp = s.get('display_name', s['name'])
            self.monitor_curves['temp'][i] = self.monitor_plots['temp'].plot(pen=t_pen, name=disp)
            self.monitor_curves['humi'][i] = self.monitor_plots['humi'].plot(pen=h_pen, name=disp)

        for i, ch in enumerate(self.config['caen_hv_settings']['channels_to_monitor']):
            pen = pg.mkPen(color=self.plot_colors[i % len(self.plot_colors)], width=3); self.monitor_curves['volt'][ch] = self.monitor_plots['volt'].plot(pen=pen, name=f'Ch{ch}'); self.monitor_curves['curr'][ch] = self.monitor_plots['curr'].plot(pen=pen, name=f'Ch{ch}')


        for p in self.monitor_plots.values():
            self._setup_plot_view(p)
        view_row = QHBoxLayout()   # in the bottom line, so the plots keep their height
        btn_auto, btn_home = QPushButton("Auto Scale"), QPushButton("Reset View")
        btn_auto.clicked.connect(lambda: [self._auto_scale(p) for p in self.monitor_plots.values()])
        btn_home.clicked.connect(lambda: [self._reset_view(p) for p in self.monitor_plots.values()])
        view_row.addWidget(btn_auto); view_row.addWidget(btn_home)

        bottom_layout = QGridLayout(); font_medium = QFont(); font_medium.setPointSize(self.styles['font_size_medium'])
        self.shifter_label = QLabel(self.config['ui_options'].get('shifter_name', '')); self.shifter_label.setFont(font_medium)
        self.datetime_label = QLabel(""); self.datetime_label.setFont(font_medium); self.datetime_label.setAlignment(Qt.AlignRight)
        view_row.addSpacing(16); view_row.addWidget(self.shifter_label); view_row.addStretch(1)
        bottom_layout.addLayout(view_row, 0, 0); bottom_layout.addWidget(self.datetime_label, 0, 1)
        main_layout.addWidget(graph_widget); main_layout.addLayout(bottom_layout)

    def _views(self, plot):
        """(view box, curves) of a plot; a dual-axis plot adds its right-axis box."""
        p = plot.getPlotItem()
        out = [(p.vb, p.listDataItems())]
        p2 = getattr(plot, 'dual_viewbox', None)
        if p2 is not None:
            out.append((p2, [i for i in p2.addedItems if hasattr(i, 'getData')]))
        return out

    def _fit_y(self, vb, curves):
        """Y range of the visible curves inside the current X range."""
        lo, hi = vb.viewRange()[0]
        ys = []
        for c in curves:
            if not c.isVisible():
                continue
            x, y = c.getData()
            if x is None or y is None or not len(x):
                continue
            x, y = np.asarray(x, float), np.asarray(y, float)
            ys.append(y[(x >= lo) & (x <= hi) & np.isfinite(y)])
        ys = np.concatenate(ys) if ys else np.array([])
        if not ys.size:
            return
        y0, y1 = float(ys.min()), float(ys.max())
        pad = max((y1 - y0) * 0.08, abs(y1) * 0.01) or 1.0   # a flat zero line still gets a readable range
        vb.setYRange(y0 - pad, y1 + pad, padding=0)

    def _auto_scale(self, plot):
        """Y fitted to the curves in the current X range (the X zoom is kept)."""
        for vb, curves in self._views(plot):
            self._fit_y(vb, curves)

    def _reset_view(self, plot):
        """Full data range again, following new points."""
        main = plot.getPlotItem().vb
        for vb, _ in self._views(plot):
            vb.enableAutoRange() if vb is main else vb.enableAutoRange(axis='y')

    def _setup_plot_view(self, plot):
        """Right-click menu with Auto Scale / Reset View (and Export) instead of the axis range boxes."""
        from PyQt5.QtWidgets import QMenu
        menu = QMenu(plot)
        menu.addAction("Auto Scale", lambda: self._auto_scale(plot))
        menu.addAction("Reset View", lambda: self._reset_view(plot))
        menu.addAction("Set Range...", lambda: self._range_dialog(plot))
        plot.getPlotItem().setMenuEnabled(False, enableViewBoxMenu=None)
        for vb, _ in self._views(plot):
            vb.getMenu = lambda ev, m=menu: m
        self._hook_legend(plot)

    def _range_dialog(self, plot):
        dlg = getattr(plot, '_range_dlg', None)
        if dlg is None:
            title = plot.getPlotItem().titleLabel.text or "Plot"
            dlg = plot._range_dlg = AxisRangeDialog(plot, title, self._auto_scale, self)
        dlg.load(); dlg.show(); dlg.raise_(); dlg.activateWindow()

    def _hook_legend(self, plot):
        """Click a legend name: Y fitted to that data set only (the line sample still hides/shows it)."""
        legend = plot.getPlotItem().legend
        if legend is None:
            return
        for sample, label in legend.items:
            def click(ev, curve=sample.item, sample=sample):
                if ev.button() != Qt.LeftButton:
                    return
                ev.accept()
                curve.setVisible(True)
                sample.update()
                vb = curve.getViewBox()
                if vb is not None:
                    self._fit_y(vb, [curve])
            label.mouseClickEvent = click

    def _link_msg(self, text, color="#f1fa8c"):
        self.link_msg_label.setStyleSheet(f"color: {color};")
        self.link_msg_label.setText(f"[{datetime.now():%H:%M:%S}] {text}")

    def _set_hv_link_off(self, off):
        self._hv_link_off = off
        self.hv_connect_btn.setEnabled(off); self.hv_disconnect_btn.setEnabled(not off)

    def on_hv_link_status(self, msg):
        self.hv_status_msg = msg
        if "Disconnected by operator" in msg:
            self._set_hv_link_off(True)
        elif "Connection Successful" in msg or "Connecting" in msg:
            self._set_hv_link_off(False)

    def env_reconnect(self):
        _flash(self.env_reconnect_btn)
        self.worker_manager.reconnect_env()
        self._link_msg("ENV serial link: reconnecting ...")

    def open_hv_config(self):
        if not getattr(self, "config_path", None):
            QMessageBox.warning(self, "HV Config", "Config file path is unknown."); return
        HVConfigDialog(self.config_path, self).exec_()

    def restart_with(self, config_path):
        """Start a new monitor with the given config after this one has released the hardware."""
        import subprocess
        here = os.path.dirname(os.path.abspath(__file__))
        subprocess.Popen(["sh", "-c", f'sleep 8; cd "{here}" && exec "{sys.executable}" monitoring_app.py "{config_path}"'],
                         start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.close()

    def hv_connect(self):
        _flash(self.hv_connect_btn)
        self._set_hv_link_off(False)
        self.hv_status_label.setText("Connecting to HV..."); self.hv_status_label.setStyleSheet("color: white;")
        self.worker_manager.hv_link_command('connect')
        self._link_msg("HV link: connecting ...")

    def hv_disconnect(self):
        _flash(self.hv_disconnect_btn)
        req = hv_request_file.pending()
        if req or self._hv_request is not None:
            req = req or self._hv_request
            QMessageBox.warning(self, "HV link", f"A Daily Routine HV request is pending (Ch{req.get('ch')} -> {req.get('vset')} V, "
                                f"{req.get('state')}).\nWait until it has finished, then disconnect.")
            self._link_msg("Disconnect refused: HV request pending", "#ff7b72")
            return
        ans = QMessageBox.question(self, "Disconnect HV link", "Disconnect the HV link? HV output is not changed.",
                                   QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ans != QMessageBox.Yes:
            return
        self._set_hv_link_off(True)
        self.worker_manager.hv_link_command('disconnect')
        self._link_msg("HV link: disconnected (HV output unchanged)")

    def refresh_all(self):
        """One immediate read of HV (crate poll) and ENV (serial buffer), then redraw."""
        _flash(self.refresh_all_btn)
        self.worker_manager.poll_env_now()
        if self._hv_link_off:
            self._link_msg("HV link is disconnected: only ENV refreshed", "#ffb86c")
        else:
            self.worker_manager.hv_link_command('poll_now')
            self._link_msg("Refreshing HV and ENV ...")
        self.update_indicators()
        QTimer.singleShot(1500, self.update_indicators)

    def all_clear_alarm(self):
        _flash(self.all_clear_alarm_btn)
        self.worker_manager.queue_hv_command('clear_alarm', 0, 0, '', 0)
        print("[INFO] Sent All Clear Alarm command to HV System.") 

    def toggle_button_blink(self):
        if self.is_blink_on:
            self.all_clear_alarm_btn.setStyleSheet("""
                QPushButton { background-color: #44475a; color: white; border-radius: 4px; font-weight: bold; padding: 5px; }
                QPushButton:pressed { background-color: #6272a4; border: 2px inset #555555; }
            """)
        else:
            self.all_clear_alarm_btn.setStyleSheet("""
                QPushButton { background-color: #ffb86c; color: black; border-radius: 4px; font-weight: bold; padding: 5px; }
                QPushButton:pressed { background-color: #e6a140; border: 2px inset #555555; }
            """)
        self.is_blink_on = not self.is_blink_on


    def setup_diagnostic_ui(self):
        lay = QVBoxLayout(self.diag_tab)
        self.diag_table = QTableWidget(0, 2)
        self.diag_table.setHorizontalHeaderLabels(["Item", "Value"])
        self.diag_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.diag_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.diag_table.verticalHeader().setVisible(False)
        self.diag_table.setEditTriggers(QTableWidget.NoEditTriggers); self.diag_table.setSelectionMode(QTableWidget.NoSelection)
        self.diag_log = QPlainTextEdit(); self.diag_log.setReadOnly(True); self.diag_log.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.diag_log.setFont(QFont("Monospace", 9))
        self.diag_copy_btn = QPushButton("Copy report"); self.diag_copy_btn.clicked.connect(self.diag_copy_report)
        lay.addWidget(self.diag_table, 3); lay.addWidget(QLabel("Application log (last 30 lines)")); lay.addWidget(self.diag_log, 2)
        row = QHBoxLayout(); row.addStretch(1); row.addWidget(self.diag_copy_btn); lay.addLayout(row)
        self.diag_timer = QTimer(self); self.diag_timer.timeout.connect(self.diag_refresh)
        self.tabs.currentChanged.connect(self._diag_tab_changed)

    def _diag_tab_changed(self, _):
        if self.tabs.currentWidget() is self.diag_tab:
            self.diag_refresh(); self.diag_timer.start(2000)
        else:
            self.diag_timer.stop()

    @staticmethod
    def _fmt_ts(t):
        return f"{datetime.fromtimestamp(t):%Y-%m-%d %H:%M:%S} ({time.time() - t:.0f} s ago)" if t else "never"

    def _app_log_path(self):
        try:
            p = os.readlink("/proc/self/fd/1")
            if os.path.isfile(p): return p
        except OSError:
            pass
        return os.path.expanduser("~/logs/launcher_hv.log")

    def _diag_rows(self):
        rows = []
        hv_fresh = time.time() - getattr(self, 'last_hv_data_time', 0) <= 3.5
        hv_state = "closed by operator" if self._hv_link_off else ("connected" if hv_fresh else "no data (reconnecting)")
        rows += [("HV link", hv_state), ("HV last poll", self._fmt_ts(getattr(self, 'last_hv_data_time', 0))),
                 ("HV worker message", self.hv_status_msg or "-")]
        a = self.config['arduino_settings']; port = a['port']
        rows += [("ENV link", self.env_status_text or "-"), ("ENV port", f"{port} (open: {self.env_port_open})"),
                 ("ENV last valid reading", self._fmt_ts(self.last_env_valid_time))]
        th = []
        for i, d in sorted(self.latest_data['sensors'].items()):
            if i in self.sensor_labels: th.append(f"{self.sensor_labels[i]['display']}: {d.get('t', np.nan):.2f} C / {d.get('h', np.nan):.2f} %")
        rows.append(("ENV last T/H", "; ".join(th) or "-"))
        rows.append((port, os.path.realpath(port) if os.path.exists(port) else "MISSING"))
        byid = "/dev/serial/by-id"
        try: devs = [f"{n} -> {os.path.realpath(os.path.join(byid, n))}" for n in sorted(os.listdir(byid))]
        except OSError: devs = []
        rows.append((byid, "\n".join(devs) or "(none)"))
        db = os.path.abspath(self.db_manager.db_path); last = "-"
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1)
            try: last = con.execute("SELECT MAX(timestamp) FROM monitoring_data").fetchone()[0] or "-"
            finally: con.close()
        except sqlite3.Error as e:
            last = f"unreadable ({e})"
        try: size = f"{os.path.getsize(db) / 1e6:.1f} MB"
        except OSError: size = "missing"
        rows += [("DB file", db), ("DB size", size), ("DB last row", last)]
        req = hv_request_file.read()
        if req:
            rows.append(("hv_request", f"{'PENDING' if req.get('state') in hv_request_file.OPEN else 'last'}: {req.get('state')} "
                         f"Ch{req.get('ch')} -> {req.get('vset')} V by {req.get('who')} at {req.get('ts')}; note: {req.get('note') or '-'}; updated {req.get('updated')}"))
        else:
            rows.append(("hv_request", "none"))
        up = int(time.time() - self.started_at)
        rows.append(("App uptime", f"{up // 86400} d {up % 86400 // 3600:02d}:{up % 3600 // 60:02d}:{up % 60:02d}"))
        return rows

    def _diag_log_tail(self, n=30):
        path = self._app_log_path()
        try:
            with open(path, "rb") as f:
                f.seek(0, 2); f.seek(max(0, f.tell() - 65536))
                lines = f.read().decode("utf-8", errors="replace").splitlines()[-n:]
            return path, "\n".join(lines)
        except OSError as e:
            return path, f"(cannot read: {e})"

    def diag_refresh(self):
        rows = self._diag_rows()
        self.diag_table.setRowCount(len(rows))
        for r, (k, v) in enumerate(rows):
            self.diag_table.setItem(r, 0, QTableWidgetItem(k)); self.diag_table.setItem(r, 1, QTableWidgetItem(v))
        self.diag_table.resizeRowsToContents()
        _, text = self._diag_log_tail()
        bar = self.diag_log.verticalScrollBar(); at_end = bar.value() >= bar.maximum() - 2
        if text != self.diag_log.toPlainText():
            self.diag_log.setPlainText(text)
            if at_end: bar.setValue(bar.maximum())

    def diag_copy_report(self):
        path, text = self._diag_log_tail()
        lines = [f"HV Monitor diagnostic report {datetime.now():%Y-%m-%d %H:%M:%S}"] + [f"{k}: {v}" for k, v in self._diag_rows()]
        lines += [f"--- application log: {path} (last 30 lines) ---", text]
        QApplication.clipboard().setText("\n".join(lines))

    def setup_analysis_ui(self):
        import re
        layout = QVBoxLayout(self.analysis_tab)
        layout.setSpacing(10)
        font_large = QFont(); font_large.setPointSize(self.styles['font_size_large'])
        font_medium = QFont(); font_medium.setPointSize(self.styles.get('font_size_medium', 14))

        # ── Toolbar card: time range + actions ──────────────────────────
        toolbar_frame = QFrame()
        toolbar_frame.setStyleSheet("""
            QFrame { border: 1px solid #3a3a3a; border-radius: 8px; background-color: #242424; }
            QLabel { border: none; background: transparent; }
        """)
        control_layout = QHBoxLayout(toolbar_frame)
        control_layout.setContentsMargins(14, 10, 14, 10)
        control_layout.setSpacing(10)
        self.start_time_edit = QDateTimeEdit(QDateTime.currentDateTime().addDays(-7)); self.start_time_edit.setFont(font_medium)
        self.end_time_edit = QDateTimeEdit(QDateTime.currentDateTime()); self.end_time_edit.setFont(font_medium)
        self.load_data_btn = QPushButton("📈 Load Data"); self.load_data_btn.setFont(font_medium)
        self.load_data_btn.setStyleSheet("QPushButton { background-color: #3b82f6; color: white; } QPushButton:hover { background-color: #4c8bf5; } QPushButton:pressed { background-color: #2f6fd1; }")
        self.export_csv_btn = QPushButton("⬇ Export CSV"); self.export_csv_btn.setFont(font_medium)
        lbl_start = QLabel("Start"); lbl_start.setFont(font_medium); lbl_start.setStyleSheet("color: #9aa5b1;")
        lbl_end = QLabel("End"); lbl_end.setFont(font_medium); lbl_end.setStyleSheet("color: #9aa5b1;")
        control_layout.addWidget(lbl_start); control_layout.addWidget(self.start_time_edit)
        control_layout.addSpacing(12)
        control_layout.addWidget(lbl_end); control_layout.addWidget(self.end_time_edit)
        control_layout.addStretch(1)
        control_layout.addWidget(self.load_data_btn)
        control_layout.addWidget(self.export_csv_btn)
        layout.addWidget(toolbar_frame)

        # ── Series selection: grouped into cards instead of one flat wall
        #    of checkboxes, so Sensors / HV Voltage&Current / HV Status read
        #    as distinct sections at a glance. ──────────────────────────
        self.analysis_checkboxes = {}
        db_cols = [c.split()[0] for c in self.db_manager._get_expected_columns()]
        groups = [("Sensors (T / H)", []), ("HV Voltage & Current", []), ("HV Status", [])]
        for col_name in db_cols:
            if re.match(r"^Ch\d+_Stat$", col_name):
                groups[2][1].append(col_name)
            elif re.match(r"^Ch\d+_(V|VSet|I|I_L|I_H)$", col_name):
                groups[1][1].append(col_name)
            else:
                groups[0][1].append(col_name)

        checkbox_row = QHBoxLayout()
        checkbox_row.setSpacing(10)
        for title, cols in groups:
            if not cols:
                continue
            box = QGroupBox(title)
            grid = QGridLayout(box)
            grid.setSpacing(6)
            per_row = 3
            for idx, col_name in enumerate(cols):
                cb = QCheckBox(col_name); cb.setFont(font_medium)
                self.analysis_checkboxes[col_name] = cb
                grid.addWidget(cb, idx // per_row, idx % per_row)
            checkbox_row.addWidget(box, stretch=len(cols))
        layout.addLayout(checkbox_row)

        select_row = QHBoxLayout()
        btn_all = QPushButton("Select all"); btn_none = QPushButton("Clear all")
        btn_all.setFont(font_medium); btn_none.setFont(font_medium)
        select_row.addWidget(btn_all); select_row.addWidget(btn_none); select_row.addStretch(1)
        btn_auto, btn_home = QPushButton("Auto Scale"), QPushButton("Reset View")
        btn_auto.setFont(font_medium); btn_home.setFont(font_medium)
        btn_auto.clicked.connect(lambda: [self._auto_scale(p) for p in self.analysis_plots.values()])
        btn_home.clicked.connect(lambda: [self._reset_view(p) for p in self.analysis_plots.values()])
        select_row.addWidget(btn_auto); select_row.addWidget(btn_home)
        layout.addLayout(select_row)
        btn_all.clicked.connect(lambda: [cb.setChecked(True) for cb in self.analysis_checkboxes.values()])
        btn_none.clicked.connect(lambda: [cb.setChecked(False) for cb in self.analysis_checkboxes.values()])

        title_font_size_str = f"{self.styles.get('font_size_large', 18)}pt"
        legend_font_size_str = f"{self.styles.get('font_size_legend', 10)}pt"

        self.analysis_plots_widget = QWidget(); graph_layout = QGridLayout(self.analysis_plots_widget)

        self.analysis_plots = {}

        th_color = self.styles.get('font_color_sensor', 'blue')
        self.analysis_plots['temp_humi_overlay'] = self.create_dual_y_plot("Temperature (°C)", "Humidity (%)", None, th_color)
        self.analysis_plots['temp_humi_overlay'].setTitle("T/H Overlay History", size=title_font_size_str)

        hv_curr_color = self.styles.get('font_color_current', 'darkorange')
        self.analysis_plots['hv_curr_overlay'] = self.create_dual_y_plot("HV Voltage (V)", "HV Current (uA)", None, hv_curr_color)
        self.analysis_plots['hv_curr_overlay'].setTitle("HV/Current Overlay History", size=title_font_size_str)

        for plot in self.analysis_plots.values():
            legend = plot.getPlotItem().addLegend()
            legend.setLabelTextSize(legend_font_size_str)
            try:
                legend.setBrush(pg.mkBrush(20, 20, 20, 220))
                legend.setPen(pg.mkPen('#777777', width=1))
                legend.setLabelTextColor('#e0e0e0')
            except Exception:
                pass

        # Stacked vertically (row 0/1, same column) instead of side-by-side --
        # user feedback: easier to read two time-series plots stacked than
        # squeezed horizontally next to each other.
        graph_layout.addWidget(self.analysis_plots['temp_humi_overlay'], 0, 0)
        graph_layout.addWidget(self.analysis_plots['hv_curr_overlay'], 1, 0)

        for p in self.analysis_plots.values():
            if isinstance(p, pg.PlotWidget):
                p.getViewBox().setMouseMode(pg.ViewBox.RectMode)
            self._setup_plot_view(p)

        layout.addWidget(self.analysis_plots_widget, stretch=1)
        self.load_data_btn.clicked.connect(self.load_and_plot_data); self.export_csv_btn.clicked.connect(self.export_analysis_to_csv)

    def connect_signals(self):
        self.worker_manager.arduino_data_ready.connect(self.update_arduino_data); self.worker_manager.caenhv_data_ready.connect(self.update_caenhv_data)
        self.worker_manager.arduino_status_changed.connect(self.on_env_status)
        self.worker_manager.caenhv_status_changed.connect(self.on_hv_link_status)
        self.worker_manager.hv_command_feedback.connect(self.on_hv_feedback); self.worker_manager.hv_initial_settings_ready.connect(self.on_hv_initial_settings_ready)
        self.control_panel_btn.clicked.connect(self.open_control_panel); self.worker_manager.shutdown_complete.connect(self.close)


    def setup_timers(self):
        self.indicator_timer = QTimer(self); self.indicator_timer.timeout.connect(self.update_indicators); self.indicator_timer.start(2000)
        self.capture_timer = QTimer(self); self.capture_timer.timeout.connect(self.capture_data_point); self.capture_timer.start(10000)  # was 60000 -- 2026-09-02, user wanted finer DB logging (Arduino itself reads every 2s, so 10s is still well within its rate)
        self.graph_timer = QTimer(self); self.graph_timer.timeout.connect(self.update_graphs); self.graph_timer.start(60000)
        self.update_timer = QTimer(self); self.update_timer.timeout.connect(self.check_update); self.update_timer.start(60000)
        self.datetime_timer = QTimer(self); self.datetime_timer.timeout.connect(lambda: self.datetime_label.setText(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))); self.datetime_timer.start(1000)

    def _load_initial_graph_data(self):
        try:
            print("Loading initial graph data...")
            #start_dt = (datetime.now() - timedelta(hours=24)).isoformat()
            start_dt = (datetime.now() - timedelta(days=3)).isoformat()
            end_dt = datetime.now().isoformat()
            
            timestamps, data = self.db_manager.fetch_data_range(start_dt, end_dt)
            if not timestamps:
                print("No historical data found.")
                return

            self.graph_data['time'] = timestamps
            
            for i, sensor in enumerate(self.config['arduino_settings']['sensors']):
                name_t = sensor['name'].replace(" ", "_").replace("#", "") + "_T"
                name_h = sensor['name'].replace(" ", "_").replace("#", "") + "_H"
                self.graph_data['temp'][i] = data.get(name_t, [np.nan] * len(timestamps))
                self.graph_data['humi'][i] = data.get(name_h, [np.nan] * len(timestamps))

            for ch in self.config['caen_hv_settings']['channels_to_monitor']:
                name_v = f"Ch{ch}_V"
                self.graph_data['volt'][ch] = data.get(name_v, [np.nan] * len(timestamps))
                
                if self.is_dual_current:
                    name_c = f"Ch{ch}_I_H" 
                else:
                    name_c = f"Ch{ch}_I"
                self.graph_data['curr'][ch] = data.get(name_c, [np.nan] * len(timestamps))
            
            print(f"Loaded {len(timestamps)} data points.")

        except Exception as e:
            print(f"Error loading initial graph data: {e}")

    def _draw_all_monitor_curves(self):
        if not self.graph_data['time']:
            return
            
        for i, curve in self.monitor_curves['temp'].items():
            if i in self.graph_data['temp']:
                curve.setData(self.graph_data['time'], self.graph_data['temp'][i], connect='finite')
        for i, curve in self.monitor_curves['humi'].items():
             if i in self.graph_data['humi']:
                curve.setData(self.graph_data['time'], self.graph_data['humi'][i], connect='finite')
        for ch, curve in self.monitor_curves['volt'].items():
             if ch in self.graph_data['volt']:
                curve.setData(self.graph_data['time'], self.graph_data['volt'][ch], connect='finite')
        for ch, curve in self.monitor_curves['curr'].items():
             if ch in self.graph_data['curr']:
                curve.setData(self.graph_data['time'], self.graph_data['curr'][ch], connect='finite')

    def update_arduino_data(self, idx, temp, humi):
        if idx not in self.sensor_labels:
            return
        self.latest_data['sensors'][idx] = {'t': np.nan if temp is None else temp, 'h': np.nan if humi is None else humi}
        if temp is not None and humi is not None and not (np.isnan(temp) or np.isnan(humi)):
            self.last_env_valid_time = time.time()

    def on_env_status(self, s):
        # "Connection Successful" only means the port opened; data validity is judged in _refresh_env_status.
        self.env_port_open = "Connection Successful" in s
        if self.env_port_open:
            self.env_port_open_since = time.time()
            self.last_env_valid_time = 0.0
            return self._refresh_env_status()
        self._set_env_status(s if "ENV" in s else f"ENV Status: {s}", "#8be9fd")

    def _set_env_status(self, text, color):
        if text != self.env_status_text:
            self.env_status_text = text
            self.env_status_label.setText(text); self.env_status_label.setStyleSheet(f"color: {color};")

    def _refresh_env_status(self, now=None):
        if not self.env_port_open:
            return
        now = time.time() if now is None else now
        if now - self.last_env_valid_time <= 60:
            self._set_env_status("ENV Status: Connection Successful!", "#50fa7b")
        elif now - self.env_port_open_since > 60:
            self._set_env_status("ENV Status: Port open - no sensor data", "#ffb86c")
        else:
            self._set_env_status("ENV Status: Port open - waiting for data...", "#8be9fd")

    def update_caenhv_data(self, results):
        # [해결 3] 데이터가 들어온 시간을 기록하고, 무조건 연결 성공 텍스트 강제 적용!
        self.last_hv_data_time = time.time() 
        if results and all(d.get('stat') == 4096 for d in results):   # worker's READERR placeholder: the crate does not answer
            self.hv_status_label.setText("HV Status: Not responding (read error)")
            self.hv_status_label.setStyleSheet("color: #ffb86c; font-weight: bold;")
        elif results and "Successful" not in self.hv_status_label.text():
            self.hv_status_label.setText("HV Status: Connection Successful!")
            self.hv_status_label.setStyleSheet("color: #50fa7b; font-weight: bold;")
            
        for data_dict in results:
            ch = data_dict['ch']; self.latest_data['hv'][ch] = data_dict
            for key in ['v', 'i', 'il', 'ih']:
                if key in self.latest_data['hv'][ch] and self.latest_data['hv'][ch][key] is None: 
                    self.latest_data['hv'][ch][key] = np.nan

    # CAEN standard channel Status bit-field (same layout the pre-existing
    # bit11/bit12 interlock check already assumed): decode it into a human
    # cause string instead of just a generic "INTERLOCK" flag, so a real trip
    # (e.g. OVC/TRIP) is distinguishable from an external interlock (KILL/
    # ILOCK) at a glance -- this was the whole point of reading Status at all.
    _STATUS_BITS = {
        3: "OVC (overcurrent)", 4: "OVV (overvoltage)", 6: "MAXV",
        7: "TRIP", 8: "OVP", 9: "OVT (over temp)",
        11: "KILL", 12: "EXT INTERLOCK",
    }

    def _decode_status(self, stat):
        if not isinstance(stat, (int, float)) or np.isnan(stat):
            return []
        stat = int(stat)
        return [label for bit, label in self._STATUS_BITS.items() if stat & (1 << bit)]

    def _status_text(self, stat):
        """Human state for the per-channel Status label: alarm cause takes
        priority, otherwise ON/OFF/RAMP UP/RAMP DOWN from the base bits
        (bit0 ON, bit1 RUP, bit2 RDW)."""
        causes = self._decode_status(stat)
        if causes:
            return "⚠ " + "/".join(causes), "#d62728"
        if not isinstance(stat, (int, float)) or np.isnan(stat):
            return "-", "#9aa5b1"
        stat = int(stat)
        if not (stat & 1):
            return "OFF", "#9aa5b1"
        if stat & (1 << 1):
            return "RAMP UP", "#e6a817"
        if stat & (1 << 2):
            return "RAMP DOWN", "#e6a817"
        return "ON", "#2ca02c"

    # A connected PMT base draws ~260-330 uA at operating HV; ~0 means the HV
    # never reaches the tube (loose SHV connector, wrong channel).
    NO_CURRENT_MIN_V = 500.0
    NO_CURRENT_MAX_I = 10.0
    NO_CURRENT_HOLD_S = 10.0
    NO_CURRENT_RAMP_MIN_V = 300.0    # ramping up: the divider already draws ~50 uA here
    NO_CURRENT_RAMP_HOLD_S = 3.0

    def _no_current(self, ch, stat, v, i):
        """True once a channel is ON, or ramping up past RAMP_MIN_V, with no current for the hold time."""
        try:
            st, v, i = int(stat), float(v), float(i)
            ramp_up = bool(st & 0b010)
            min_v, hold = ((self.NO_CURRENT_RAMP_MIN_V, self.NO_CURRENT_RAMP_HOLD_S) if ramp_up
                           else (self.NO_CURRENT_MIN_V, self.NO_CURRENT_HOLD_S))
            bad = (st & 1) and not (st & 0b100) and v >= min_v and i < self.NO_CURRENT_MAX_I
        except (TypeError, ValueError):
            bad = False
        if not bad:
            self._no_current_since.pop(ch, None)
            self._no_current_alerted.discard(ch)
            return False
        start, hold = self._no_current_since.setdefault(ch, (time.time(), hold))   # hold fixed at onset
        return time.time() - start >= hold

    def _popup_no_current(self, chs):
        names = ", ".join(f"Ch{c}" for c in chs)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Critical)
        box.setWindowTitle("HV: No Current")
        box.setText(f"{names} is ON but draws no current.")
        box.setInformativeText("HV is not reaching the PMT. Check the HV (SHV) cable at the PMT base "
                               "and that it is on the right channel.\n\nThis message is shown once; "
                               "the red status stays until current returns.")
        box.setModal(False)
        box.show()

    def go_to_daq_panel(self):
        result = focus_window(DAQ_TITLE)
        if result == 'no-window':
            result = focus_window(V2_TITLE)
        if result == 'raised':
            return
        if result == 'no-window':
            if is_running(DAQ_PROC) or is_running(V2_PROC):
                QMessageBox.information(self, "DAQ Panel", "DAQ Control is running but its window is not ready yet.")
            else:
                QMessageBox.warning(self, "DAQ Panel", "DAQ Control is not running.\nStart it from the Launcher.")
            return
        QMessageBox.warning(self, "DAQ Panel", f"Could not switch to the DAQ panel.\n{result}")

    def _check_power_fail(self):
        """UPS guard's power-failure shutdown: switch every HV channel off (Pw = 0) once per request."""
        import power_fail
        req = power_fail.pending()
        if not req or req["id"] == self._power_fail_seen or power_fail.acked("hv", req):
            return
        self._power_fail_seen = req["id"]
        hv_cfg = self.config['caen_hv_settings']
        chs = hv_cfg['channels_to_monitor']
        self.worker_manager.queue_hv_command('set_param', 0, chs, hv_cfg['parameters']['pw'], 0)
        print(f"[CRITICAL] UPS power failure ({req.get('reason')}): Pw = 0 sent to Ch{chs}.")
        power_fail.ack("hv", req, f"Pw=0 queued for {chs}")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Critical)
        box.setWindowTitle("UPS Power Failure")
        box.setText(f"Automatic shutdown: {req.get('reason')}.\nAll HV channels are being switched off.")
        box.setModal(False)
        box.show()

    HV_REQUEST_REPLY_S = 60   # crate reply expected within this after accept

    def _check_hv_request(self):
        """DAQ Control v2's set-voltage request (hv_request_file): check the limits, queue VSet, write the outcome back."""
        req = hv_request_file.pending()
        if not req:
            return
        if req["state"] == "accepted":
            if self._hv_request is None:
                hv_request_file.update(req, "error", "HV Monitor restarted while the request was in progress")
            elif time.time() - self._hv_request_sent > self.HV_REQUEST_REPLY_S:
                hv_request_file.update(req, "error", "no reply from the crate worker")
                print(f"[WARN] HV request {req['id']}: no reply from the crate worker.")
                self._hv_request = None
            return
        why = self._hv_request_refusal(req)
        if why:
            hv_request_file.update(req, "refused", why, expect=("posted",))
            print(f"[WARN] HV request {req['id']} refused ({req.get('who')}): Ch{req.get('ch')} "
                  f"VSet {req.get('vset')} V -- {why}")
            return
        vmon = self.latest_data['hv'][req["ch"]].get('v')
        if hv_request_file.update(req, "accepted", "VSet queued", vmon=vmon, expect=("posted",)) is None:
            return
        hv_cfg = self.config['caen_hv_settings']
        self.worker_manager.queue_hv_command('set_param', 0, req["ch"], hv_cfg['parameters']['v_set'], req["vset"])
        self._hv_request, self._hv_request_sent = req, time.time()
        print(f"[INFO] HV request {req['id']} accepted ({req.get('who')}): Ch{req['ch']} VSet {req['vset']:.1f} V "
              f"(VMon {vmon:.1f} V, {req.get('reason') or 'no reason given'}).")

    def _hv_request_refusal(self, req):
        """Why the request must not be executed, '' when it may."""
        lim = self.config.get('hv_request_limits', {})
        try:
            ch, vset = int(req["ch"]), float(req["vset"])
        except (KeyError, TypeError, ValueError):
            return "malformed request"
        if ch not in self.config['caen_hv_settings']['channels_to_monitor']:
            return f"Ch{ch} is not a monitored channel"
        if hv_request_file.age(req) > lim.get("max_age_s", hv_request_file.MAX_AGE_S):
            return f"stale request ({hv_request_file.age(req):.0f} s old)"
        if self._hv_request is not None:
            return "another request is still being executed"
        if self._hv_link_off:
            return "HV link disconnected at the HV Monitor"
        chlim = lim.get("channels", {}).get(str(ch), {})
        lo, hi = chlim.get("min", 0), chlim.get("max", 2300)
        if not lo <= vset <= hi:
            return f"{vset:.1f} V is outside the Ch{ch} limits {lo}-{hi} V"
        if time.time() - getattr(self, 'last_hv_data_time', 0) > 10:
            return "no recent HV reading"
        data = self.latest_data['hv'].get(ch) or {}
        stat, vmon = data.get('stat'), data.get('v')
        if not isinstance(stat, (int, float)) or np.isnan(stat):
            return f"Ch{ch} status unreadable"
        stat = int(stat)
        if stat == 4096:
            return f"Ch{ch} read error"
        if self._decode_status(stat):
            return f"Ch{ch} alarm: {', '.join(self._decode_status(stat))}"
        if not stat & 1:
            return f"Ch{ch} is OFF"
        if stat & 6:
            return f"Ch{ch} is ramping"
        if not isinstance(vmon, (int, float)) or np.isnan(vmon):
            return f"Ch{ch} VMon unreadable"
        step = lim.get("max_step_v", 250)
        if abs(vset - vmon) > step + 1.0:   # VMon reads a few tenths off VSet
            return f"step {vset - vmon:+.0f} V from VMon {vmon:.1f} V exceeds the {step} V limit"
        return ""

    def _hv_request_reply(self, msg):
        """Close the accepted request with the crate worker's reply to its VSet command."""
        req = self._hv_request
        if req is None:
            return
        vset_name = self.config['caen_hv_settings']['parameters']['v_set']
        if f"Ch{req['ch']} {vset_name} set to" in msg:
            vmon = (self.latest_data['hv'].get(req['ch']) or {}).get('v')
            hv_request_file.update(req, "done", msg, vmon=vmon)
            print(f"[INFO] HV request {req['id']} done: {msg}")
        elif msg.startswith("Error on Set"):
            hv_request_file.update(req, "error", msg)
            print(f"[WARN] HV request {req['id']} error: {msg}")
        else:
            return
        self._hv_request = None

    def _load_last_interlock(self):
        """Newest interlocked row in the HV DB, so the tooltip survives an app restart."""
        bits = sum(1 << b for b in self._STATUS_BITS)
        try:
            con = sqlite3.connect(f"file:{os.path.abspath(self.db_manager.db_path)}?mode=ro", uri=True, timeout=1)
            cond = " OR ".join(f"(CAST(Ch{c}_Stat AS INTEGER) & {bits})" for c in self.hv_labels)
            placeholder = " AND ".join(f"Ch{c}_Stat = 4096" for c in self.hv_labels)   # worker's READERR row
            r = con.execute(f"SELECT rowid, timestamp, {', '.join(f'Ch{c}_Stat' for c in self.hv_labels)} "
                            f"FROM monitoring_data WHERE ({cond}) AND NOT ({placeholder}) "
                            "ORDER BY rowid DESC LIMIT 1").fetchone()
            if r:
                clear = con.execute(f"SELECT MAX(rowid) FROM monitoring_data WHERE rowid < ? AND NOT ({cond}) "
                                    f"AND NOT ({placeholder}) AND Ch0_Stat IS NOT NULL", (r[0],)).fetchone()[0]
                start = con.execute(f"SELECT timestamp FROM monitoring_data WHERE rowid > ? AND ({cond}) "
                                    f"AND NOT ({placeholder}) ORDER BY rowid LIMIT 1", (clear or 0,)).fetchone()[0]
            con.close()
        except Exception:
            return None
        if not r:
            return None
        causes = sorted({c for st in r[2:] for c in self._decode_status(st)})
        t0, t1 = (str(t).replace('T', ' ')[:19] for t in (start, r[1]))
        return f"Last interlock: {t0} to {t1[11:]}  {', '.join(causes)}"

    def update_indicators(self):
        self._refresh_env_status()
        self._check_power_fail()
        self._check_hv_request()
        if not hasattr(self, "_last_interlock"):
            self._last_interlock = self._load_last_interlock()
            self.interlock_indicator.setToolTip(self._last_interlock or "No interlock recorded")
        is_interlocked = False
        warn_channels = []   # (ch, pct) approaching trip current, not tripped yet
        causes_by_channel = {}   # ch -> tuple(causes), grouped below into trip_causes
        no_current_channels = []
        for i, data in self.latest_data['sensors'].items():
            if i not in self.sensor_labels: continue
            disp = self.sensor_labels[i]['display']
            self.sensor_labels[i]['temp'].setText(f"{disp} T: {data.get('t'):.2f} C" if not np.isnan(data.get('t', np.nan)) else f"{disp} T: None")
            self.sensor_labels[i]['humi'].setText(f"H: {data.get('h'):.2f} %" if not np.isnan(data.get('h', np.nan)) else f"H: None")

        for ch, data in self.latest_data['hv'].items():
            if ch not in self.hv_labels: continue
            self.hv_labels[ch]['v'].setText(f"{data.get('v', 0):.2f}")
            if self.is_dual_current:
                self.hv_labels[ch]['ih'].setText(f"{data.get('ih', 0):.4f}")
            else:
                self.hv_labels[ch]['i'].setText(f"{data.get('i', 0):.4f}")

            stat = data.get('stat')
            if self.is_dual_current and 'stat' in self.hv_labels[ch]:
                txt, color = self._status_text(stat)
                self.hv_labels[ch]['stat'].setText(txt)
                self.hv_labels[ch]['stat'].setStyleSheet(f"color: {color}; font-weight: bold;")
            causes = self._decode_status(stat)
            if causes:
                is_interlocked = True
                causes_by_channel[ch] = tuple(causes)

            i_chk = data.get('ih') if self.is_dual_current else data.get('i')
            if self._no_current(ch, stat, data.get('v'), i_chk):
                no_current_channels.append(ch)
                if 'stat' in self.hv_labels[ch]:
                    self.hv_labels[ch]['stat'].setText("⚠ NO CURRENT")
                    self.hv_labels[ch]['stat'].setStyleSheet("color: #d62728; font-weight: bold;")

            # Early warning: current trending toward the trip threshold (ISet)
            # even though nothing has tripped yet.
            iset = data.get('iset')
            i_now = data.get('ih') if self.is_dual_current else data.get('i')
            try:
                if iset and i_now is not None and not np.isnan(i_now) and float(iset) > 0:
                    pct = float(i_now) / float(iset)
                    if pct >= 0.85 and not causes:
                        warn_channels.append((ch, pct))
            except (TypeError, ValueError):
                pass

        # [FIX] Watchdog sequence: Catch disconnect state and purge old values immediately
        current_time = time.time()
        if not hasattr(self, 'last_hv_data_time'): self.last_hv_data_time = current_time
        
        if current_time - self.last_hv_data_time > 3.5:
            self.hv_status_label.setText("HV Status: DISCONNECTED (link closed)" if self._hv_link_off else "HV Status: DISCONNECTED")
            self.hv_status_label.setStyleSheet("color: #ff5555; font-weight: bold;")
            
            for ch in self.hv_labels:
                self.hv_labels[ch]['v'].setText("N/A")
                if self.is_dual_current:
                    self.hv_labels[ch]['ih'].setText("N/A")
                    self.hv_labels[ch]['stat'].setText("-")
                    self.hv_labels[ch]['stat'].setStyleSheet("color: #9aa5b1; font-weight: bold;")
                else:
                    self.hv_labels[ch]['i'].setText("N/A")

        if "Successful" not in self.hv_status_label.text() and "Not responding" not in self.hv_status_label.text():
            self.blink_timer.stop()
            self.all_clear_alarm_btn.setStyleSheet("background-color: #44475a; color: white; border-radius: 4px; font-weight: bold; padding: 5px;")
            self.interlock_indicator.setText("HV: DISCONNECTED")
            self.interlock_indicator.setStyleSheet("background-color: #555555; color: white; font-weight: bold; padding: 5px; border-radius: 4px;")
            return

        stats = [d.get('stat') for c, d in self.latest_data['hv'].items() if c in self.hv_labels]
        read_error = bool(stats) and all(s == 4096 for s in stats)   # the worker's READERR placeholder
        if not is_interlocked or read_error:
            self._ilk_on = False
        if read_error:
            self.blink_timer.stop()
            self.all_clear_alarm_btn.setStyleSheet("background-color: #44475a; color: white; border-radius: 4px; font-weight: bold; padding: 5px;")
            self.interlock_indicator.setText("HV: READ ERROR")
            self.interlock_indicator.setStyleSheet("background-color: #e67e22; color: white; font-weight: bold; padding: 5px; border-radius: 4px;")
            for lab in self.hv_labels.values():
                lab['v'].setText("N/A")
                if self.is_dual_current: lab['ih'].setText("N/A")
                else: lab['i'].setText("N/A")
                if 'stat' in lab:
                    lab['stat'].setText("READ ERROR")
                    lab['stat'].setStyleSheet("color: #e67e22; font-weight: bold;")
        elif is_interlocked:
            if not self.blink_timer.isActive():
                self.blink_timer.start(500)
            # one segment per group of channels with the same causes
            groups = {}
            for ch, causes in causes_by_channel.items():
                groups.setdefault(causes, []).append(ch)
            trip_causes = []
            for causes, chs in groups.items():
                chs.sort()
                if len(chs) > 1 and chs == list(range(chs[0], chs[-1] + 1)):
                    ch_label = f"Ch{chs[0]}-Ch{chs[-1]}"
                elif len(chs) > 1:
                    ch_label = "Ch" + ",".join(str(c) for c in chs)
                else:
                    ch_label = f"Ch{chs[0]}"
                trip_causes.append(f"{ch_label}: {', '.join(causes)}")
            self.interlock_indicator.setText("HV: " + " | ".join(trip_causes))
            now = time.strftime('%Y-%m-%d %H:%M:%S')
            if not getattr(self, "_ilk_on", False):
                self._ilk_start, self._ilk_on = now, True
            self._last_interlock = f"Last interlock: {self._ilk_start} to {now[11:]}  {' | '.join(trip_causes)}"
            self.interlock_indicator.setToolTip(self._last_interlock)
            self.interlock_indicator.setStyleSheet("background-color: #d62728; color: white; font-weight: bold; padding: 5px; border-radius: 4px;")
        elif no_current_channels:
            self.blink_timer.stop()
            self.all_clear_alarm_btn.setStyleSheet("background-color: #44475a; color: white; border-radius: 4px; font-weight: bold; padding: 5px;")
            names = ", ".join(f"Ch{c}" for c in no_current_channels)
            self.interlock_indicator.setText(f"HV: ⚠ NO CURRENT ({names}) — check HV cable")
            self.interlock_indicator.setStyleSheet("background-color: #d62728; color: white; font-weight: bold; padding: 5px; border-radius: 4px;")
            new = [c for c in no_current_channels if c not in self._no_current_alerted]
            if new:
                self._no_current_alerted.update(new)
                self._popup_no_current(new)
        elif warn_channels:
            self.blink_timer.stop()
            self.all_clear_alarm_btn.setStyleSheet("background-color: #44475a; color: white; border-radius: 4px; font-weight: bold; padding: 5px;")
            warn_txt = ", ".join(f"Ch{ch} {pct*100:.0f}%" for ch, pct in warn_channels)
            self.interlock_indicator.setText(f"HV: ⚠ NEARING TRIP ({warn_txt})")
            self.interlock_indicator.setStyleSheet("background-color: #e6a817; color: black; font-weight: bold; padding: 5px; border-radius: 4px;")
        else:
            self.blink_timer.stop()
            self.all_clear_alarm_btn.setStyleSheet("background-color: #44475a; color: white; border-radius: 4px; font-weight: bold; padding: 5px;")
            self.interlock_indicator.setText("HV: NORMAL")
            self.interlock_indicator.setStyleSheet("background-color: #2ca02c; color: white; font-weight: bold; padding: 5px; border-radius: 4px;")
    
    def update_graphs(self):
        ts = time.time()
        self.graph_data['time'].append(ts)
        max_points = 4320 
        
        if len(self.graph_data['time']) > max_points: self.graph_data['time'].pop(0)
        
        for i, s_info in enumerate(self.config['arduino_settings']['sensors']):
            data = self.latest_data['sensors'].get(i, {'t': np.nan, 'h': np.nan})
            
            for key, curve_dict in [('t', 'temp'), ('h', 'humi')]:
                d_list = self.graph_data[curve_dict].setdefault(i, []); d_list.append(data.get(key, np.nan))
                if len(d_list) > max_points: d_list.pop(0)
                if i in self.monitor_curves[curve_dict]:
                    self.monitor_curves[curve_dict][i].setData(self.graph_data['time'], d_list, connect='finite')

        for ch in self.config['caen_hv_settings']['channels_to_monitor']:
            data = self.latest_data['hv'].get(ch, {})
            volt_list = self.graph_data['volt'].setdefault(ch, []); volt_list.append(data.get('v', np.nan))
            if len(volt_list) > max_points: volt_list.pop(0)
            if ch in self.monitor_curves['volt']:
                self.monitor_curves['volt'][ch].setData(self.graph_data['time'], volt_list, connect='finite')
            current_val = data.get('ih', np.nan) if self.is_dual_current else data.get('i', np.nan)
            curr_list = self.graph_data['curr'].setdefault(ch, []); curr_list.append(current_val)
            if len(curr_list) > max_points: curr_list.pop(0)
            if ch in self.monitor_curves['curr']:
                self.monitor_curves['curr'][ch].setData(self.graph_data['time'], curr_list, connect='finite')

    def capture_data_point(self):
        if self._is_closing: return
        # If HV data hasn't refreshed in a while, the worker is disconnected
        # (see the same 3.5s staleness check in update_indicators) -- log NULL
        # for every HV column instead of replaying the last-known values, so a
        # comm dropout can never be misread later as a real trip-to-0V event.
        hv_stale = (time.time() - getattr(self, 'last_hv_data_time', 0)) > 3.5
        hv_snapshot = {} if hv_stale else self.latest_data['hv'].copy()
        data_point = {'ts': datetime.now().isoformat(), 'sensors': self.latest_data['sensors'].copy(), 'hv': hv_snapshot}
        self.db_manager.log_data(data_point)
        cursor = self.db_manager.conn.cursor(); cursor.execute("SELECT COUNT(*) FROM monitoring_data"); count = cursor.fetchone()[0]
        self.log_status_label.setText(f"Logging: {count} point(s) collected")

    def load_and_plot_data(self):
        start_str = self.start_time_edit.dateTime().toString(Qt.ISODate); end_str = self.end_time_edit.dateTime().toString(Qt.ISODate)
        timestamps, data = self.db_manager.fetch_data_range(start_str, end_str)
        
        title_font_size_str = f"{self.styles.get('font_size_large', 18)}pt"
        legend_font_size_str = f"{self.styles.get('font_size_legend', 10)}pt"
        
        for plot in self.analysis_plots.values(): 
            p1 = plot.getPlotItem()

            # [FIX] Close and remove existing legend container safely to prevent detached object errors.
            # PlotItem.addLegend() only creates a NEW LegendItem when self.legend is
            # None -- otherwise it just returns the existing (here: closed/invisible)
            # one without re-attaching it to the scene. .close() alone doesn't reset
            # that reference, so every reload AFTER the first left p1.legend pointing
            # at a dead legend and addLegend() below silently no-opped -- the legend
            # only ever appeared on the very first load. Must null it out here too.
            if p1.legend:
                p1.legend.close()
                p1.legend = None

            plot.clear()
            if hasattr(plot, 'dual_viewbox'):
                plot.dual_viewbox.clear()

            # [FIX] Re-add a fresh clean Legend instance after clearing the plot frame
            p1.addLegend().setLabelTextSize(legend_font_size_str)
            
            tick_font = QFont(); tick_font.setPointSize(self.styles.get('font_size_medium', 16) - 2)
            label_font = QFont(); label_font.setPointSize(self.styles.get('font_size_medium', 16))
            
            p1.getAxis('bottom').setTickFont(tick_font)
            p1.getAxis('left').setTickFont(tick_font)
            p1.getAxis('left').label.setFont(label_font)
            
            if hasattr(p1, 'getAxis') and p1.getAxis('right'):
                p1.getAxis('right').setTickFont(tick_font)
                p1.getAxis('right').label.setFont(label_font)
            
        if not timestamps: return
        selected_cols = [name for name, cb in self.analysis_checkboxes.items() if cb.isChecked()]
        
        p_th1 = self.analysis_plots['temp_humi_overlay'].getPlotItem()
        p_th2 = self.analysis_plots['temp_humi_overlay'].dual_viewbox
        p_hv1 = self.analysis_plots['hv_curr_overlay'].getPlotItem()
        p_hv2 = self.analysis_plots['hv_curr_overlay'].dual_viewbox

        for name in selected_cols:
            if name not in data or all(v is None for v in data[name]): continue
            values = data[name]
            
            plot_name = name.replace('_', ' ') 
            
            if '_V' in name: 
                ch_idx = 0
                try:
                    ch_num = int(name.split('_')[0].replace('Ch', ''))
                    ch_idx = self.config['caen_hv_settings']['channels_to_monitor'].index(ch_num)
                except: pass
                pen = pg.mkPen(color=self.plot_colors[ch_idx % len(self.plot_colors)], width=3)
                p_hv1.plot(timestamps, values, pen=pen, name=plot_name)
            
            elif '_I_L' in name or '_I_H' in name or name.endswith('_I'):
                ch_idx = 0
                try:
                    ch_num = int(name.split('_')[0].replace('Ch', ''))
                    ch_idx = self.config['caen_hv_settings']['channels_to_monitor'].index(ch_num)
                except: pass
                pen = pg.mkPen(color=self.plot_colors[(ch_idx + 3) % len(self.plot_colors)], width=3, style=Qt.DashLine)
                curve = pg.PlotCurveItem(pen=pen, name=plot_name)
                curve.setData(timestamps, values, connect='finite')
                p_hv2.addItem(curve)
                if p_hv1.legend: p_hv1.legend.addItem(curve, plot_name)

            elif '_T' in name:
                sensor_idx = 0
                for idx, s in enumerate(self.config['arduino_settings']['sensors']):
                    clean_s_name = s['name'].replace(" ", "_").replace("#", "")
                    if name.startswith(clean_s_name):
                        sensor_idx = idx
                        break
                pen = pg.mkPen(color=self.temp_colors[sensor_idx % len(self.temp_colors)], width=3)
                p_th1.plot(timestamps, values, pen=pen, name=plot_name)
            
            elif '_H' in name:
                sensor_idx = 0
                for idx, s in enumerate(self.config['arduino_settings']['sensors']):
                    clean_s_name = s['name'].replace(" ", "_").replace("#", "")
                    if name.startswith(clean_s_name):
                        sensor_idx = idx
                        break
                pen = pg.mkPen(color=self.humi_colors[sensor_idx % len(self.humi_colors)], width=3)
                curve = pg.PlotCurveItem(pen=pen, name=plot_name)
                curve.setData(timestamps, values, connect='finite')
                p_th2.addItem(curve)
                if p_th1.legend: p_th1.legend.addItem(curve, plot_name)

        # [FIX] Force right Y-axis standalone viewboxes to execute auto-scale update layout routines
        p_th2.enableAutoRange(axis='y', enable=True)
        p_hv2.enableAutoRange(axis='y', enable=True)
        for plot in self.analysis_plots.values():
            self._hook_legend(plot)

        # Rot1/Rot2 (pin2/pin3) sensor-ID fix marker -- only draw it if the
        # loaded time range actually straddles the fix, so an out-of-range
        # marker never drags the plot's autoRange out to include a timestamp
        # nowhere near the visible data. Kept even after the DB migration
        # (which consolidated Rot1_T/H, Rot2_T/H into one continuous column
        # per physical sensor across the whole history) as a permanent record
        # of when the config-side pin<->name mapping itself was corrected.
        if timestamps and min(timestamps) <= DARKBOX_RELABEL_TS <= max(timestamps):
            marker = pg.InfiniteLine(pos=DARKBOX_RELABEL_TS, angle=90,
                                     pen=pg.mkPen('#ff5555', width=2, style=Qt.DashLine))
            label = pg.TextItem("Rot1/Rot2 sensor ID fixed", color='#ff5555', anchor=(0, 1))
            label.setPos(DARKBOX_RELABEL_TS, 0)
            p_th1.addItem(marker)
            p_th1.addItem(label)


    def export_analysis_to_csv(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save CSV", f"export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv", "CSV Files (*.csv)")
        if not path: return
        start_str = self.start_time_edit.dateTime().toString(Qt.ISODate); end_str = self.end_time_edit.dateTime().toString(Qt.ISODate)
        _, data = self.db_manager.fetch_data_range(start_str, end_str)
        if not data or not data.get('timestamp'): return
        selected_cols = ['timestamp'] + [name for name, cb in self.analysis_checkboxes.items() if cb.isChecked()]
        with open(path, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([col for col in selected_cols if col in data])
            num_rows = len(data['timestamp'])
            for i in range(num_rows):
                row = [data[col][i] for col in selected_cols if col in data]
                writer.writerow(row)

    def open_control_panel(self):
        if not hasattr(self, 'control_panel'):
            hv_params = self.config['caen_hv_settings']['parameters']
            self.control_panel = HVControlPanel(self.config['caen_hv_settings']['channels_to_monitor'], hv_params, self.styles, self)
            self.control_panel.control_signal.connect(self.worker_manager.queue_hv_command)
        self.control_panel.show(); self.control_panel.raise_(); self.control_panel.request_settings_for_channel()

    def on_hv_feedback(self, msg):
        if hasattr(self, 'control_panel'): self.control_panel.update_feedback(msg)
        self._hv_request_reply(msg)
    def on_hv_initial_settings_ready(self, settings):
        if hasattr(self, 'control_panel'): self.control_panel.set_initial_values(settings)

    def check_update(self):
        from hv_update import UPDATER
        if UPDATER.check():
            print("[INFO] Source update detected on disk. Click the update badge to restart and apply it.")
        if UPDATER.available:
            self.update_btn.setText("Update available - click to restart"); self.update_btn.show()

    def on_update_click(self):
        from hv_update import UPDATER
        err = UPDATER.syntax_error()
        if err:
            QMessageBox.critical(self, "Update Error", f"The updated code has a syntax error - staying on the current version.\n\n{err}")
            return
        reason = UPDATER.busy_reason()
        text = (f"The system looks busy: {reason}.\n\nRestart anyway?" if reason else "Restart now to apply the update?")
        text += "\n\n- HV output is NOT changed (the crate keeps its setpoints).\n- Monitoring and logging pause for a few seconds."
        if QMessageBox.question(self, "Restart", text, QMessageBox.Ok | QMessageBox.Cancel) != QMessageBox.Ok:
            return
        print("[INFO] Restarting the HV monitor to apply the update.")
        UPDATER.schedule_relaunch()
        self.close()

    def closeEvent(self, event):
        if self._is_closing: event.accept(); return
        print("Close button pressed. Initiating shutdown...")
        self._is_closing = True; event.ignore(); self.setEnabled(False)

        # setEnabled(False) alone left the window looking frozen with no
        # feedback while worker_manager.initiate_shutdown() finishes in the
        # background (only a terminal print, easy to miss) -- indistinguishable
        # from the app just hanging. A visible "Shutting down..." overlay on
        # top of the disabled window makes it obvious it's actually closing.
        overlay = QLabel("⏻  Shutting down...\nReleasing hardware, please wait.", self)
        overlay.setAlignment(Qt.AlignCenter)
        overlay.setStyleSheet(
            "background-color: rgba(20, 20, 20, 230); color: #ffcc00; "
            "font-size: 20pt; font-weight: bold; border: none;")
        overlay.setGeometry(0, 0, self.width(), self.height())
        overlay.show(); overlay.raise_()
        QApplication.processEvents()

        for timer in [self.indicator_timer, self.capture_timer, self.graph_timer, self.datetime_timer, self.diag_timer, self.update_timer]: timer.stop()
        self.db_manager.close()
        self.worker_manager.initiate_shutdown()

def load_config(config_file):
    with open(config_file, 'r', encoding='utf-8') as f: return json.load(f)

if __name__ == '__main__':
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    # An inherited SIGCHLD=SIG_IGN makes multiprocessing's is_alive() never turn False.
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    app = QApplication(sys.argv)
    import instance_lock
    instance_lock.ensure_single_qt("hv", HV_TITLE)

    icon_path = '/home/precalkor/Integrated_Control_SW/HV_Control_SW/icons/HVcontrol.ico'
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))
    else:
        print(f"Warning: Icon file not found at {icon_path}")

    default_config = 'config_precal.json'
    config_file = sys.argv[1] if len(sys.argv) > 1 else default_config
    if not os.path.exists(config_file): print(f"Error: Config file '{config_file}' not found."); sys.exit(1)
    config = load_config(config_file)
    window = MonitoringApp(config)
    window.config_path = os.path.abspath(config_file)
    window.show()
    timer = QTimer(); timer.start(500); timer.timeout.connect(lambda: None)
    sys.exit(app.exec_())

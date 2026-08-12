import sys, json, os, time, signal, sqlite3, csv
from datetime import datetime, timedelta 
from PyQt5.QtWidgets import QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton, QDialog, QComboBox, QDoubleSpinBox, QTabWidget, QDateTimeEdit, QFileDialog, QCheckBox, QFrame, QGroupBox, QScrollArea, QSizePolicy
from PyQt5.QtCore import QTimer, Qt, pyqtSignal, QDateTime
from PyQt5.QtGui import QFont, QIcon
import pyqtgraph as pg
import numpy as np
from worker_manager import WorkerManager
from database_manager import DatabaseManager

# Dark Box #1/#2 were swapped in config_precal.json's arduino_settings.sensors
# on 2026-07-27 17:42 (pin 2 was mislabeled "Dark Box #1", pin 3 "Dark Box #2" --
# physically it's the other way around). Data logged BEFORE this timestamp has
# the two swapped; data logged AFTER is correct. Drawn as a vertical marker on
# the T/H Overlay History plot (see load_and_plot_data) so a time range that
# straddles the fix is never misread as one continuous, consistently-labeled
# series.
DARKBOX_RELABEL_TS = datetime(2026, 7, 27, 17, 42, 0).timestamp()

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
        self.set_voltage_btn.setFont(font); self.set_current_btn.setFont(font)
        self.power_on_btn, self.power_off_btn = QPushButton("Turn ON"), QPushButton("Turn OFF")
        self.power_on_btn.setStyleSheet("""
            QPushButton { background-color: lightgreen; color: black; border: 1px solid #777777; border-radius: 3px; padding: 5px; }
            QPushButton:pressed { background-color: #7acc7a; border: 2px inset #555555; }
        """)

        self.power_off_btn.setStyleSheet("""
            QPushButton { background-color: lightcoral; color: black; border: 1px solid #777777; border-radius: 3px; padding: 5px; }
            QPushButton:pressed { background-color: #e06666; border: 2px inset #555555; }
        """)
        self.clear_alarm_btn = QPushButton("Clear Alarm")
        self.clear_alarm_btn.setFont(font)
        self.clear_alarm_btn.setStyleSheet("""
            QPushButton { background-color: #FFB347; color: black; border: 1px solid #777777; border-radius: 3px; padding: 5px; }
            QPushButton:pressed { background-color: #e6a140; border: 2px inset #555555; }
        """)
        self.feedback_label = QLabel("Status: Ready"); self.feedback_label.setFont(feedback_font)
        for label_text, widget, row in [("Target Channel:", self.channel_selector, 0), ("Set Voltage (V):", self.voltage_input, 1), ("Set Current (uA):", self.current_input, 2)]:
            label = QLabel(label_text); label.setFont(font); self.layout.addWidget(label, row, 0)
            if widget in [self.voltage_input, self.current_input]: self.layout.addWidget(widget, row, 1)
            else: self.layout.addWidget(widget, row, 1, 1, 2)
        self.layout.addWidget(self.set_voltage_btn, 1, 2); self.layout.addWidget(self.set_current_btn, 2, 2)
        self.layout.addWidget(self.power_on_btn, 3, 0, 1, 3); self.layout.addWidget(self.power_off_btn, 4, 0, 1, 3)
        self.layout.addWidget(self.clear_alarm_btn, 5, 0, 1, 3)
        self.layout.addWidget(self.feedback_label, 6, 0, 1, 3)
        #self.layout.addWidget(self.feedback_label, 5, 0, 1, 3)
        self.set_voltage_btn.clicked.connect(self.set_voltage); self.set_current_btn.clicked.connect(self.set_current)
        self.power_on_btn.clicked.connect(self.turn_on); self.power_off_btn.clicked.connect(self.turn_off)
        self.channel_selector.currentIndexChanged.connect(self.request_settings_for_channel)
        self.clear_alarm_btn.clicked.connect(self.clear_alarm)

    def clear_alarm(self): 
        self.control_signal.emit('clear_alarm', 0, 0, '', 0)
    def get_ch(self): return int(self.channel_selector.currentText())
    def set_voltage(self): self.control_signal.emit('set_param', 0, self.get_ch(), self.hv_params['v_set'], self.voltage_input.value())
    def set_current(self): self.control_signal.emit('set_param', 0, self.get_ch(), self.hv_params['i_set'], self.current_input.value())
    def turn_on(self): self.control_signal.emit('set_param', 0, self.get_ch(), self.hv_params['pw'], 1)
    def turn_off(self): self.control_signal.emit('set_param', 0, self.get_ch(), self.hv_params['pw'], 0)
    def update_feedback(self, msg): self.feedback_label.setText(f"Status: {msg}")
    def request_settings_for_channel(self): self.control_signal.emit('fetch_settings', 0, self.get_ch(), '', '')
    def set_initial_values(self, settings):
        ch = self.get_ch()
        if ch in settings: self.voltage_input.setValue(settings[ch]['v_set']); self.current_input.setValue(settings[ch]['i_set'])


class MonitoringApp(QMainWindow):
    def __init__(self, config):
        super().__init__()
        self.config = config; self.styles = config['ui_styles']
        self.latest_data = {'sensors': {}, 'hv': {}}
        self.graph_data = {'time': [], 'temp': {}, 'humi': {}, 'volt': {}, 'curr': {}}
        self.is_dual_current = 'i_mon_low' in self.config['caen_hv_settings']['parameters']
        pg.setConfigOption('background', self.styles['background_color']); pg.setConfigOption('foreground', self.styles['font_color_main'])
        self.plot_colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf']
        self._is_closing = False
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
        self.tabs.addTab(self.monitor_tab, "Monitoring"); self.tabs.addTab(self.analysis_tab, "Data analysis")
        self.setup_monitor_ui(); self.setup_analysis_ui()


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
        self.control_panel_btn.setStyleSheet("background-color: #6272a4; color: white; border-radius: 4px; font-weight: bold; padding: 5px;")
        
        self.all_clear_alarm_btn = QPushButton("All Clear Alarm (Interlock)")
        self.all_clear_alarm_btn.setFont(font_large)
        self.all_clear_alarm_btn.setMinimumHeight(35)
        # [수정] pressed 옵션 추가로 클릭감 구현
        self.all_clear_alarm_btn.setStyleSheet("""
            QPushButton { background-color: #44475a; color: white; border-radius: 4px; font-weight: bold; padding: 5px; }
            QPushButton:pressed { background-color: #6272a4; border: 2px inset #555555; }
        """)
        self.all_clear_alarm_btn.clicked.connect(self.all_clear_alarm)
        
        # 대시보드 내부 배치 (독립된 좌표계)
        dash_layout.addWidget(self.env_status_label, 0, 0, 2, 1)
        dash_layout.addWidget(self.hv_status_label, 0, 1, 1, 1)
        dash_layout.addWidget(self.log_status_label, 1, 1, 1, 1)
        dash_layout.addWidget(self.interlock_indicator, 0, 2, 1, 1)
        dash_layout.addWidget(self.control_panel_btn, 0, 3, 1, 1)
        dash_layout.addWidget(self.all_clear_alarm_btn, 1, 2, 1, 2)
        
        # 각 영역의 비율 고정 (ENV 넓게, 중앙 적당히, 우측 버튼들 고정)
        dash_layout.setColumnStretch(0, 2)
        dash_layout.setColumnStretch(1, 2)
        dash_layout.setColumnStretch(2, 1)
        dash_layout.setColumnStretch(3, 1)

        main_layout.addWidget(top_dashboard_frame)

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


        bottom_layout = QGridLayout(); font_medium = QFont(); font_medium.setPointSize(self.styles['font_size_medium'])
        self.shifter_label = QLabel(self.config['ui_options'].get('shifter_name', '')); self.shifter_label.setFont(font_medium)
        self.datetime_label = QLabel(""); self.datetime_label.setFont(font_medium); self.datetime_label.setAlignment(Qt.AlignRight)
        bottom_layout.addWidget(self.shifter_label, 0, 0); bottom_layout.addWidget(self.datetime_label, 0, 1)
        main_layout.addWidget(graph_widget); main_layout.addLayout(bottom_layout)

    def all_clear_alarm(self):
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
            elif re.match(r"^Ch\d+_(V|I|I_L|I_H)$", col_name):
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

        layout.addWidget(self.analysis_plots_widget, stretch=1)
        self.load_data_btn.clicked.connect(self.load_and_plot_data); self.export_csv_btn.clicked.connect(self.export_analysis_to_csv)

    def connect_signals(self):
        self.worker_manager.arduino_data_ready.connect(self.update_arduino_data); self.worker_manager.caenhv_data_ready.connect(self.update_caenhv_data)
        self.worker_manager.arduino_status_changed.connect(lambda s: self.env_status_label.setText(s if "ENV" in s else f"ENV Status: {s}"))
        self.worker_manager.hv_command_feedback.connect(self.on_hv_feedback); self.worker_manager.hv_initial_settings_ready.connect(self.on_hv_initial_settings_ready)
        self.control_panel_btn.clicked.connect(self.open_control_panel); self.worker_manager.shutdown_complete.connect(self.close)


    def setup_timers(self):
        self.indicator_timer = QTimer(self); self.indicator_timer.timeout.connect(self.update_indicators); self.indicator_timer.start(2000)
        self.capture_timer = QTimer(self); self.capture_timer.timeout.connect(self.capture_data_point); self.capture_timer.start(60000)
        self.graph_timer = QTimer(self); self.graph_timer.timeout.connect(self.update_graphs); self.graph_timer.start(60000)
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

    def update_caenhv_data(self, results):
        # [해결 3] 데이터가 들어온 시간을 기록하고, 무조건 연결 성공 텍스트 강제 적용!
        self.last_hv_data_time = time.time() 
        if results and "Successful" not in self.hv_status_label.text():
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

    def update_indicators(self):
        is_interlocked = False
        warn_channels = []   # (ch, pct) approaching trip current, not tripped yet
        causes_by_channel = {}   # ch -> tuple(causes), grouped below into trip_causes
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
            self.hv_status_label.setText("HV Status: DISCONNECTED")
            self.hv_status_label.setStyleSheet("color: #ff5555; font-weight: bold;")
            
            for ch in self.hv_labels:
                self.hv_labels[ch]['v'].setText("N/A")
                if self.is_dual_current:
                    self.hv_labels[ch]['ih'].setText("N/A")
                    self.hv_labels[ch]['stat'].setText("-")
                    self.hv_labels[ch]['stat'].setStyleSheet("color: #9aa5b1; font-weight: bold;")
                else:
                    self.hv_labels[ch]['i'].setText("N/A")

        if "Successful" not in self.hv_status_label.text():
            self.blink_timer.stop()
            self.all_clear_alarm_btn.setStyleSheet("background-color: #44475a; color: white; border-radius: 4px; font-weight: bold; padding: 5px;")
            self.interlock_indicator.setText("HV: DISCONNECTED")
            self.interlock_indicator.setStyleSheet("background-color: #555555; color: white; font-weight: bold; padding: 5px; border-radius: 4px;")
            return

        if is_interlocked:
            if not self.blink_timer.isActive():
                self.blink_timer.start(500)
            # Group channels that share the exact same cause(s) into one
            # segment (e.g. "Ch0-Ch3: EXT INTERLOCK") instead of repeating the
            # identical text per channel -- the board-level interlock always
            # trips every channel together, so 4 separate "EXT INTERLOCK"
            # entries were just noise. Channels with a genuinely different
            # cause (e.g. one channel OVC while the rest are EXT INTERLOCK)
            # still get their own segment.
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
            self.interlock_indicator.setStyleSheet("background-color: #d62728; color: white; font-weight: bold; padding: 5px; border-radius: 4px;")
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
    def on_hv_initial_settings_ready(self, settings):
        if hasattr(self, 'control_panel'): self.control_panel.set_initial_values(settings)

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

        for timer in [self.indicator_timer, self.capture_timer, self.graph_timer, self.datetime_timer]: timer.stop()
        self.db_manager.close()
        self.worker_manager.initiate_shutdown()

def load_config(config_file):
    with open(config_file, 'r', encoding='utf-8') as f: return json.load(f)

if __name__ == '__main__':
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app = QApplication(sys.argv)

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
    window.show()
    timer = QTimer(); timer.start(500); timer.timeout.connect(lambda: None)
    sys.exit(app.exec_())

"""Signal Lamp tab, mirroring ui_manager._create_signal_lamp_tab (PATLITE NE-USB): colour,
pattern, buzzer, volume, Apply / Test Alarm / Off, touch sensor and connection check, through
the same PatliteLamp the laser watchdog uses (core.signal_lamp). While the Tk app is open it
owns the lamp, and the status line only shows whether the beacon is on USB."""
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QGroupBox, QHBoxLayout, QLabel, QMessageBox, QPushButton,
                             QSlider, QVBoxLayout, QWidget)

from app import theme
from core import signal_lamp as SL

COLORS = ["Off", "Red", "Green", "Amber", "Blue", "Purple", "Sky Blue", "White"]
LED_PATTERNS = ["Off", "Steady", "Flash 1", "Flash 2", "Flash 3", "Flash 4", "Flash 5", "Flash 6"]
BUZZER = ["Off", "Continuous", "Sweep", "Intermittent", "Weak Attention", "Strong Attention",
          "Twinkle Twinkle Little Star", "London Bridge"]


def _combo(items, current):
    c = QComboBox()
    c.addItems(items)
    c.setCurrentText(current)
    return c


class SignalLampPage(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        t = QLabel("PATLITE NE-USB Signal Lamp")
        t.setStyleSheet("font-size: 14pt; font-weight: 700;")
        lay.addWidget(t)
        row = QHBoxLayout()
        row.addWidget(QLabel("Status:"))
        self.status = QLabel()
        row.addWidget(self.status)
        row.addSpacing(15)
        b = QPushButton("Check Connection")
        b.clicked.connect(self.refresh)
        row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)

        light = QGroupBox("Light")
        r = QHBoxLayout(light)
        r.addWidget(QLabel("Color:"))
        self.color = _combo(COLORS, "Red")
        r.addWidget(self.color)
        r.addSpacing(20)
        r.addWidget(QLabel("Pattern:"))
        self.pattern = _combo(LED_PATTERNS, "Steady")
        r.addWidget(self.pattern)
        r.addStretch(1)
        lay.addWidget(light)

        buz = QGroupBox("Buzzer")
        v = QVBoxLayout(buz)
        r = QHBoxLayout()
        r.addWidget(QLabel("Pattern:"))
        self.buzzer = _combo(BUZZER, "Off")
        r.addWidget(self.buzzer)
        r.addStretch(1)
        v.addLayout(r)
        r = QHBoxLayout()
        r.addWidget(QLabel("Volume:"))
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 10)
        self.volume.setValue(5)
        self.volume.setFixedWidth(200)
        r.addWidget(self.volume)
        vol_lbl = QLabel("5")
        vol_lbl.setMinimumWidth(24)
        self.volume.valueChanged.connect(lambda n: vol_lbl.setText(str(n)))
        r.addWidget(vol_lbl)
        r.addSpacing(20)
        self.continuous = QCheckBox("Continuous (loop)")
        self.continuous.setChecked(True)
        r.addWidget(self.continuous)
        r.addStretch(1)
        v.addLayout(r)
        lay.addWidget(buz)

        act = QHBoxLayout()
        for text, fn in (("Apply", self.apply), ("Test Alarm (instant)", self.test_alarm), ("Off / Reset", self.off)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            act.addWidget(b)
        act.addStretch(1)
        lay.addLayout(act)

        touch = QGroupBox("Touch Sensor (NE-ST-USB / NE-WT-USB only)")
        r = QHBoxLayout(touch)
        b = QPushButton("Read Touch State")
        b.clicked.connect(self.read_touch)
        r.addWidget(b)
        self.touch = QLabel("-")
        self.touch.setStyleSheet("font-weight: 700;")
        r.addSpacing(15)
        r.addWidget(self.touch)
        r.addStretch(1)
        lay.addWidget(touch)
        lay.addStretch(1)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(30000)
        self.refresh()

    def _paint(self, text, role):
        self.status.setText(text)
        self.status.setStyleSheet(f"font-weight: 700; color: {theme.C[role]};")

    def refresh(self):
        lamp, why = SL.lamp()
        if lamp is None:   # the Tk app (or nobody) owns the lamp: presence on USB only
            found = SL.on_usb()
            owner = ""
            self._paint(f"Lamp on USB{owner}" if found else "Lamp not found on USB", "good" if found else "bad")
            return
        if lamp.probe():
            self._paint("Connected", "good")
        else:
            self._paint("Not connected", "bad")

    def _lamp(self):
        lamp, why = SL.lamp()
        if lamp is None:
            QMessageBox.warning(self, "Signal Lamp", why)
        return lamp

    def apply(self):
        lamp = self._lamp()
        if lamp is None:
            return
        args, kw = SL.light_args(self.color.currentText(), self.pattern.currentText(), self.buzzer.currentText(),
                                 self.continuous.isChecked(), self.volume.value())
        ok = lamp.set_light(*args, **kw)
        self.refresh()
        if not ok:
            QMessageBox.warning(self, "Signal Lamp", "Command failed. Is the lamp connected?")

    def test_alarm(self):
        """The interlock watchdog's steady red, no buzzer."""
        lamp = self._lamp()
        if lamp is None:
            return
        ok = lamp.alarm_interlock()
        self.refresh()
        if not ok:
            QMessageBox.warning(self, "Signal Lamp", "Command failed. Is the lamp connected?")

    def off(self):
        lamp, _why = SL.lamp()
        if lamp is None:
            return
        lamp.reset()
        self.refresh()

    def read_touch(self):
        lamp, _why = SL.lamp()
        if lamp is None:
            self.touch.setText("-")
            return
        self.touch.setText(SL.TOUCH_TEXT.get(lamp.get_touch_state(), "-"))

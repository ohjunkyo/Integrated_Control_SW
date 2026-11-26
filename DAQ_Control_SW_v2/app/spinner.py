"""Spinning ring centered over a widget while its content loads."""
from PyQt5.QtCore import QEvent, QRectF, Qt, QTimer
from PyQt5.QtGui import QColor, QPainter, QPen
from PyQt5.QtWidgets import QWidget

from app import theme


class Spinner(QWidget):
    WIDTH, STEP_MS = 5, 16

    def __init__(self, parent, size=56):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.size, self.angle = size, 0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        parent.installEventFilter(self)
        self.hide()

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Resize:
            self.setGeometry(obj.rect())
        return False

    def start(self):
        self.setGeometry(self.parent().rect())
        self.raise_()
        self.show()
        self.timer.start(self.STEP_MS)

    def stop(self):
        self.timer.stop()
        self.hide()

    def _tick(self):
        self.angle = (self.angle + 6) % 360
        self.update()

    def paintEvent(self, _ev):
        s = min(self.size, self.width() - 8, self.height() - 8)
        if s <= 8:
            return
        w = max(2, round(self.WIDTH * s / 56))
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF((self.width() - s) / 2, (self.height() - s) / 2, s, s)
        p.setPen(QPen(QColor(theme.C["border"]), w))
        p.drawEllipse(r)
        pen = QPen(QColor(theme.C["accent"]), w)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawArc(r, -self.angle * 16, 100 * 16)
        p.end()

"""Record layout sketch: where the trigger and the PMT pulse sit inside one digitizer record,
shown as a hover popup on Window / Post-trigger fields."""
from PyQt5.QtCore import QEvent, QObject, QPoint, QPointF, QRectF, Qt
from PyQt5.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PyQt5.QtWidgets import QFrame, QVBoxLayout, QWidget

from app import theme

NS_PER_SAMPLE = 2
SELF_LATENCY = 63        # samples the DT5730 self-trigger fires after the pulse (run 20260928_701)
LASER_OFFSET = 193       # samples from the external trigger to the laser pulse (Monitor, 405 nm)
LASER_SIG_WINDOW = (171, 221)   # prod_ntp_v7 signal window relative to the trigger


def layout(window, post_pct, mode):
    """(trigger sample, pulse sample) for a record; mode 'self' or 'external'."""
    trig = round(window * (1 - post_pct / 100.0))
    pulse = trig - SELF_LATENCY if mode == "self" else trig + LASER_OFFSET
    return trig, pulse


class RecordSketch(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.window, self.post, self.mode = 256, 24, "self"
        self.setFixedSize(560, 190)

    def set_params(self, window, post, mode):
        self.window, self.post, self.mode = window, post, mode
        self.update()

    def paintEvent(self, _e):
        C = theme.C
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(C["card"]))
        f = QFont(self.font())
        f.setPointSizeF(10)
        p.setFont(f)
        x0, x1, y0, y1 = 20.0, self.width() - 20.0, 52.0, 128.0
        n = max(1, self.window)

        def X(s):
            return x0 + (x1 - x0) * s / n

        trig, pulse = layout(self.window, self.post, self.mode)
        title = (f"{'Self trigger' if self.mode == 'self' else 'External trigger'}   "
                 f"window {self.window} samples ({self.window * NS_PER_SAMPLE} ns)   post-trigger {self.post} %")
        p.setPen(QColor(C["text"]))
        p.drawText(QRectF(x0, 6, x1 - x0, 18), Qt.AlignLeft | Qt.AlignVCenter, title)
        p.fillRect(QRectF(X(0), y0, X(trig) - X(0), y1 - y0), QColor(C["line"]))
        p.fillRect(QRectF(X(trig), y0, X(n) - X(trig), y1 - y0), QColor(C["accent_soft"]))
        p.setPen(QPen(QColor(C["border"]), 1))
        p.drawRect(QRectF(X(0), y0, X(n) - X(0), y1 - y0))
        p.setPen(QColor(C["muted"]))
        p.drawText(QRectF(X(0) + 4, y1 - 18, max(0.0, X(trig) - X(0) - 8), 16), Qt.AlignLeft,
                   f"pre-trigger {100 - self.post} %")
        p.drawText(QRectF(X(trig) + 4, y1 - 18, max(0.0, X(n) - X(trig) - 8), 16), Qt.AlignRight, f"post {self.post} %")
        if self.mode == "external":
            a, b = X(min(n, trig + LASER_SIG_WINDOW[0])), X(min(n, trig + LASER_SIG_WINDOW[1]))
            if b > a:
                band = QColor(C["good"])
                band.setAlpha(50)
                p.fillRect(QRectF(a, y0, b - a, y1 - y0), band)
        inside = 0 <= pulse < n
        if inside:
            px, base, depth = X(pulse), y0 + 14, (y1 - y0) * 0.6
            path = QPainterPath(QPointF(px - 40, base))
            for i in range(-20, 41):
                t = i / 4.0
                v = 0.0 if t < -1.5 else (1.0 - 2.718 ** (-(t + 1.5) * 1.6)) * 2.718 ** (-max(0.0, t) / 1.8)
                path.lineTo(QPointF(px + i * 2.0, base + depth * v))
            p.setPen(QPen(QColor(C["accent"]), 2.4))
            p.drawPath(path)
        p.setPen(QPen(QColor(C["bad"]), 2, Qt.DashLine))
        p.drawLine(QPointF(X(trig), y0 - 8), QPointF(X(trig), y1 + 4))
        p.setPen(QColor(C["bad"]))
        p.drawText(QRectF(X(trig) - 60, y0 - 26, 120, 16), Qt.AlignCenter, f"trigger {trig}")
        p.setPen(QColor(C["muted"]))
        for s in (0, n // 4, n // 2, 3 * n // 4, n):
            p.drawLine(QPointF(X(s), y1), QPointF(X(s), y1 + 5))
            p.drawText(QRectF(X(s) - 40, y1 + 6, 80, 14), Qt.AlignCenter, str(s))
        if inside:
            msg = f"pulse ~sample {pulse} ({pulse * NS_PER_SAMPLE} ns from record start)"
            color = C["bad"] if pulse < 30 or pulse > n - 30 else C["text"]
        else:
            msg, color = "pulse falls outside the record", C["bad"]
        p.setPen(QColor(color))
        p.drawText(QRectF(x0, y1 + 26, x1 - x0, 18), Qt.AlignLeft | Qt.AlignVCenter, msg)
        p.end()


class RecordHover(QObject):
    """Shows a RecordSketch under any of `fields` while the mouse is over it.
    `params()` returns (window, post_pct, mode) or None when the values are not usable."""

    def __init__(self, fields, params):
        super().__init__(fields[0])
        self.params = params
        self.popup = QFrame(fields[0], Qt.ToolTip | Qt.FramelessWindowHint)   # dies with its field
        self.popup.setObjectName("recordPopup")
        self.popup.setStyleSheet(f"QFrame#recordPopup {{ border: 1px solid {theme.C['border']}; }}")
        lay = QVBoxLayout(self.popup)
        lay.setContentsMargins(1, 1, 1, 1)
        self.sketch = RecordSketch()
        lay.addWidget(self.sketch)
        for w in fields:
            w.installEventFilter(self)

    def _update(self):
        v = self.params()
        if v:
            self.sketch.set_params(*v)
        return v

    def eventFilter(self, obj, ev):
        try:
            return self._filter(obj, ev)
        except RuntimeError:      # popup already destroyed during teardown
            return False

    def _filter(self, obj, ev):
        t = ev.type()
        if t == QEvent.Enter:
            if self._update():
                self.popup.adjustSize()
                self.popup.move(obj.mapToGlobal(QPoint(0, obj.height() + 4)))
                self.popup.show()
        elif t in (QEvent.Leave, QEvent.Hide):
            self.popup.hide()
        elif t == QEvent.KeyRelease and self.popup.isVisible():
            self._update()
        return False

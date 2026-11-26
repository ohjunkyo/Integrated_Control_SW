"""Small shared widgets: tab placeholder, status lamp, clickable cell."""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QLabel, QVBoxLayout, QWidget

from app import theme

LAMP_ON, LAMP_OFF = "#28a745", "#dc3545"   # same LED colors as the Tk app


def placeholder(name):
    """Stand-in for a tab that is not ported yet (keeps the tab in its Tk position)."""
    w = QWidget()
    lay = QVBoxLayout(w)
    lab = QLabel(f"{name}\n\nNot ported to v2 yet. Use the Tk app for this tab.")
    lab.setAlignment(Qt.AlignCenter)
    lab.setStyleSheet(f"color: {theme.C['hint']};")
    lay.addWidget(lab)
    return w


def placeholder_text(text):
    """A tab that only carries a short centred message."""
    w = QWidget()
    lay = QVBoxLayout(w)
    lab = QLabel(text)
    lab.setAlignment(Qt.AlignCenter)
    lab.setStyleSheet(f"color: {theme.C['hint']};")
    lay.addWidget(lab)
    return w


def make_lamp(size=14):
    lamp = QLabel()
    lamp.setFixedSize(size, size)
    return lamp


def set_lamp(lamp, ok):
    paint_lamp(lamp, LAMP_ON if ok else LAMP_OFF)


def paint_lamp(lamp, color):
    lamp.setStyleSheet(f"background: {color}; border: 1px solid #333; border-radius: {lamp.width() // 2}px;")


class ClickCell(QWidget):
    """Overview cell that runs a callback when clicked."""

    def __init__(self, on_click):
        super().__init__()
        self._on_click = on_click
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._on_click()

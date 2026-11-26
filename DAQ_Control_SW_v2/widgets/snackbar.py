"""Snackbar: one-line message at the bottom of a page with an optional action (Undo), gone after a few seconds."""
from PyQt5.QtCore import QEvent, QTimer
from PyQt5.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton

from app import theme

DEFAULT_MS = 5000


class Snackbar(QFrame):
    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("snackbar")
        h = QHBoxLayout(self)
        h.setContentsMargins(14, 6, 8, 6)
        h.setSpacing(16)
        self.label = QLabel()
        self.button = QPushButton()
        self.button.clicked.connect(self._act)
        h.addWidget(self.label)
        h.addWidget(self.button)
        self._on_action = None
        self._left = DEFAULT_MS
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.hide)
        parent.installEventFilter(self)
        self.hide()

    def show_message(self, text, action="", on_action=None, ms=DEFAULT_MS):
        self.setStyleSheet(f"#snackbar {{ background: {theme.C['tooltip']}; border-radius: 6px; }}"
                           f"#snackbar QLabel {{ color: {theme.C['term_fg']}; }}")
        self.label.setText(text)
        self.button.setText(action)
        self.button.setVisible(bool(action))
        theme.role(self.button, "accent")
        self._on_action = on_action
        self.adjustSize()
        self._place()
        self.show()
        self.raise_()
        self.timer.start(ms)

    def _act(self):
        self.timer.stop()
        self.hide()
        fn, self._on_action = self._on_action, None
        if fn:
            fn()

    def enterEvent(self, ev):
        self._left = max(self.timer.remainingTime(), 1500)
        self.timer.stop()
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        if self.isVisible():
            self.timer.start(self._left)
        super().leaveEvent(ev)

    def _place(self):
        p = self.parentWidget()
        self.move((p.width() - self.width()) // 2, p.height() - self.height() - 12)

    def eventFilter(self, _obj, ev):
        if ev.type() == QEvent.Resize and self.isVisible():
            self._place()
        return False

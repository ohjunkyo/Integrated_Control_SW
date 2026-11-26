"""Message to Expert: a free message with the sender's name, and the per-alert Send / Digest / Off checklist
with the reminder interval (managers/alert_policy)."""
import threading

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (QComboBox, QDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
                             QPushButton, QScrollArea, QVBoxLayout, QWidget)

MODE_LABELS = (("post", "Send"), ("digest", "Digest"), ("mute", "Off"))
LEVEL_LABELS = (("warning", "Normal"), ("critical", "Urgent"))


class SlackAlertsDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        from managers import alert_policy
        self.policy = alert_policy
        self.data = alert_policy.load()
        self.setWindowTitle("Message to Expert")
        self.resize(560, 720)
        v = QVBoxLayout(self)
        v.addWidget(self._message_box())
        top = QHBoxLayout()
        lab = QLabel("Reminder while it persists")
        lab.setObjectName("key")
        top.addWidget(lab)
        self.still = QComboBox()
        for hours, text in alert_policy.STILL_CHOICES:
            self.still.addItem(text, hours)
        self.still.setCurrentIndex(max(0, self.still.findData(self.data["still_h"])))
        top.addWidget(self.still)
        top.addStretch(1)
        v.addLayout(top)
        area = QScrollArea()
        area.setWidgetResizable(True)
        body = QWidget()
        g = QGridLayout(body)
        g.setHorizontalSpacing(12)
        g.setVerticalSpacing(4)
        self.boxes, row, group = {}, 0, None
        for key, grp, label in alert_policy.CATALOG:
            if grp != group:
                group = grp
                h = QLabel(grp)
                h.setObjectName("key")
                h.setStyleSheet("font-weight: 700; padding-top: 8px;")
                g.addWidget(h, row, 0, 1, 2)
                row += 1
            g.addWidget(QLabel(label), row, 0)
            box = QComboBox()
            for mode, text in MODE_LABELS:
                box.addItem(text, mode)
            box.setCurrentIndex(max(0, box.findData(self.data["items"].get(key, "post"))))
            box.setMinimumWidth(110)
            g.addWidget(box, row, 1, Qt.AlignLeft)
            self.boxes[key] = box
            row += 1
        g.setColumnStretch(0, 1)
        g.setRowStretch(row, 1)
        area.setWidget(body)
        v.addWidget(area, 1)
        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        save = QPushButton("Save")
        save.setStyleSheet("QPushButton { background: #c92a2a; color: white; border: none; font-weight: 600; }")
        save.clicked.connect(self._save)
        btns.addWidget(save)
        v.addLayout(btns)

    def _message_box(self):
        box = QWidget()
        g = QGridLayout(box)
        g.setContentsMargins(0, 0, 0, 8)
        h = QLabel("Send Message")
        h.setObjectName("key")
        h.setStyleSheet("font-weight: 700;")
        g.addWidget(h, 0, 0, 1, 4)
        g.addWidget(QLabel("From"), 1, 0)
        self.sender = QLineEdit(self._shifter())
        g.addWidget(self.sender, 1, 1)
        g.addWidget(QLabel("Level"), 1, 2)
        self.level = QComboBox()
        for level, text in LEVEL_LABELS:
            self.level.addItem(text, level)
        g.addWidget(self.level, 1, 3)
        self.text = QPlainTextEdit()
        self.text.setFixedHeight(80)
        g.addWidget(self.text, 2, 0, 1, 4)
        self.msg_status = QLabel("")
        g.addWidget(self.msg_status, 3, 0, 1, 3)
        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self._send_message)
        g.addWidget(self.send_btn, 3, 3)
        g.setColumnStretch(1, 1)
        return box

    @staticmethod
    def _shifter():
        try:
            from core import env
            return env.read_config().get("Shift_worker", "").strip()
        except Exception:
            return ""

    def _send_message(self):
        name, body = self.sender.text().strip(), self.text.toPlainText().strip()
        if not name or not body:
            self.msg_status.setText("From and message required.")
            return
        from core import applog
        n = applog.notifier()
        if not n.enabled:
            self.msg_status.setText("Slack not configured.")
            return
        shifter = self._shifter() or "-"
        level, result = self.level.currentData(), []
        self.send_btn.setEnabled(False)
        self.msg_status.setText("Sending...")
        threading.Thread(target=lambda: result.append(n.send(f"Message from {name}", f"Shifter: {shifter}\n{body}", level=level,
                                                             blocking=True)), daemon=True).start()

        def done():
            if not result:
                QTimer.singleShot(200, done)
                return
            if self.isHidden():
                return
            self.send_btn.setEnabled(True)
            if result[0]:
                self.text.clear()
                self.msg_status.setText("Sent.")
                applog.log(f"[INFO] Message to Expert sent by {name} ({level}).")
            else:
                self.msg_status.setText("Not sent.")
        QTimer.singleShot(200, done)

    def _save(self):
        items = {k: b.currentData() for k, b in self.boxes.items() if b.currentData() != "post"}
        self.policy.save({"still_h": self.still.currentData(), "items": items})
        self.accept()

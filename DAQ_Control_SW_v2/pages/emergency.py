"""Emergency tab, mirroring ui_manager._create_contact_tab (contacts.json table)."""
import json
import os

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QLabel, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from app import theme
from core import env


class EmergencyPage(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 20, 20, 20)
        title = QLabel("Emergency Contact Network")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(f"font-size: 16pt; font-weight: 700; color: {theme.C['bad']};")
        lay.addWidget(title)
        lay.addSpacing(12)

        tree = QTreeWidget()
        tree.setHeaderLabels(["Role / Affiliation", "Name", "Phone Number", "Note"])
        tree.setRootIsDecorated(False)
        tree.setAlternatingRowColors(True)
        for i, w in enumerate([180, 150, 180, 500]):
            tree.setColumnWidth(i, w)
        try:
            contacts = json.load(open(os.path.join(env.DAQ_DIR, "contacts.json")))
        except (OSError, json.JSONDecodeError) as e:
            contacts = []
            lay.addWidget(QLabel(f"Error loading contacts.json: {e}"))
        for c in contacts:
            item = QTreeWidgetItem([c.get("role", ""), c.get("name", ""), c.get("phone", ""), c.get("note", "")])
            for col in range(3):
                item.setTextAlignment(col, Qt.AlignCenter)
            tree.addTopLevelItem(item)
        lay.addWidget(tree, 1)

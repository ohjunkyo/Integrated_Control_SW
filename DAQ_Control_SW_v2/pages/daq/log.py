"""DAQ System > Log: the acquisition log (TakingLog), following the newest file; every other log is in
the main Logs tab."""
from PyQt5.QtWidgets import QVBoxLayout, QWidget

from pages.logs import FileLog


class DaqLogPage(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 10)
        lay.addWidget(FileLog("DAQ"), 1)

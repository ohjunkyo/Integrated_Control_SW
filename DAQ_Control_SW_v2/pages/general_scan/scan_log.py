"""General Scan > Live Scan Logs: the scan messages the Tk app writes to
logs/scan_live_<date>.txt (add_auto_log), as a Time | Level | Message table.
ScanLogTail is also used by the Live Scan page's Live Console."""
import os

from PyQt5.QtWidgets import QVBoxLayout, QWidget

from core import env
from widgets.log_table import FileTail

LOG_GLOB = os.path.join(env.DAQ_DIR, "logs", "scan_live_*.txt")
EMPTY = "No scan log yet -- messages appear once a General Scan runs (logs/scan_live_<date>.txt)."


class ScanLogTail(FileTail):
    def __init__(self, max_rows=1000):
        super().__init__(LOG_GLOB, EMPTY, max_rows=max_rows)


class ScanLogPage(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        self.table = ScanLogTail()
        lay.addWidget(self.table)

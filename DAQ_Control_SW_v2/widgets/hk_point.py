"""Histograms of one HK Digitizer scan point: pedestal, charge, hit time and time over threshold of the three PMTs."""
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QDialog, QLabel, QVBoxLayout

from core import hk_plot


class HKPointWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("HK Digitizer point")
        self.setWindowFlag(Qt.WindowMaximizeButtonHint, True)
        self.resize(1500, 1000)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        self.fig = Figure(facecolor="white", layout="constrained")   # pads fill the window at any size
        self.canvas = FigureCanvasQTAgg(self.fig)
        lay.addWidget(self.canvas, 1)
        self.note = QLabel("")
        self.note.setWordWrap(True)
        lay.addWidget(self.note)

    def show_point(self, rec):
        hk_plot.draw(self.fig, rec)
        self.canvas.draw_idle()
        self.setWindowTitle(f"HK Digitizer  Run{rec['run']:03d}")
        self.note.setText(rec["name"])
        self.show()
        self.raise_()

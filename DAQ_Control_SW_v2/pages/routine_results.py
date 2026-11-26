"""Daily Routine main tab: Routine (the steps page), Results (what one routine run measured: dark rate,
HV scan and HV apply VMon, General Scan QE / Gain / TTS FWHM as in Live Scan) and Statistics (numbers per step)."""
import html
import os
import subprocess
import threading

from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
from matplotlib.colors import to_hex
from matplotlib.container import Container
from matplotlib.dates import DateFormatter
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.offsetbox import AnnotationBbox, HPacker, TextArea
from PyQt5.QtCore import QObject, QPointF, QRectF, Qt, QTimer, QUrl, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QPainter, QPen, QPixmap, QPolygonF
from PyQt5.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog, QGroupBox,
                             QFrame, QGraphicsOpacityEffect, QHBoxLayout, QHeaderView, QLabel, QMenu, QProgressBar,
                             QPushButton,
                             QRadioButton, QScrollArea, QSizePolicy, QSplitter, QSplitterHandle, QTabWidget,
                             QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from app import theme
from app.spinner import Spinner as _Spinner
from core import routine as rt
from core import applog
from core import drive_upload as drive
from core import env
from core import hk_results as HK
from core import report_pages as RP
from core import routine_results as RR
from core import routine_stats as RS
from core.schedule import JST
from widgets.flow_layout import FlowLayout
from widgets.plot_zoom import PlotZoom
from widgets.run_drawer import RunDrawer

CH = {0: ("Monitor", "#BA7517", "s"), 1: ("Rot1", "#378ADD", "o"), 2: ("Rot2", "#1D9E75", "^")}
AXIS_LS = {"X": "-", "Y": "--"}
OPEN_MK = ["o", "s", "^", "v"]         # open markers (ROOT 24/25/26/32) for steps sharing a panel
FAIL_ALPHA = 0.5                       # failed-fit points: open, faded, no error bar


def fail_handle():
    return Line2D([], [], color="#555555", marker="o", mfc="none", mew=1, ls="none", alpha=FAIL_ALPHA,
                  label="Fit failed")
STEP_SHADE = {0: ["#BA7517", "#6B4209", "#E3B36A", "#C2410C", "#8A6D3B", "#D97706"],
              1: ["#378ADD", "#173F73", "#8FC1F2", "#6C5FD0", "#A855C7", "#2A6F97"],
              2: ["#1D9E75", "#0B5640", "#7DD3B2", "#7A9A12", "#B8A41C", "#3E8E5E"]}
LEGEND_ROWS = 2              # the legend strip scrolls beyond this
EDGE_PX = 6                  # grab distance of a plot border
PICK_PX = 6                  # click distance that selects a data point
MIN_FRAC = 0.15              # smallest plot area, as a fraction of the figure
PEN = {"-": Qt.SolidLine, "--": Qt.DashLine, ":": Qt.DotLine, "-.": Qt.DashDotLine}
N70_DEG = 70.5               # final-result convention (Draw_Reproducibility.C)
N70_MAX_REL = 0.10
N70_METRICS = ("qe", "gain")
MIN_SPAN = 0.05              # value panels show at least +-5 % of the level
POLL_MS = 5000               # new run data is noticed within this
FINAL_DIR = os.path.join(env.ADC_DIR, "Data", "FinalResult")
METRIC_NAMES = {"qe": "QE", "gain": "Gain", "fwhm": "TTS (FWHM)", RR.DARK_ON: "Dark rate (LD on)"}
HK_METRIC_NAMES = {"qe": "Hits / Trigger", "gain": "Charge", "fwhm": "Timing FWHM", RR.DARK_ON: "Dark rate (LD on)"}


def _data_stamp():
    """Changes when a run is registered, a FinalResult file is written or an HK point has been read."""
    out = []
    for path in (env.REGISTRY, FINAL_DIR, os.path.join(HK.CACHE_DIR, HK.today())):
        try:
            out.append(os.stat(path).st_mtime)
        except OSError:
            out.append(None)
    return tuple(out)


def _show_in_folder(path):
    """Select an exported file in the file manager, reusing (and raising) a window that already shows its folder."""
    folder = os.path.dirname(path)
    try:
        uri = QUrl.fromLocalFile(path).toString(QUrl.FullyEncoded)
        r = subprocess.run(["gdbus", "call", "--session", "--dest", "org.freedesktop.FileManager1",
                            "--object-path", "/org/freedesktop/FileManager1",
                            "--method", "org.freedesktop.FileManager1.ShowItems", f"['{uri}']", ""],
                           capture_output=True, timeout=5)
        ok = r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    if not ok:
        subprocess.Popen(["xdg-open", folder], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return

    def _raise():
        # Nautilus only flashes "is ready" for a window in the background; the pager request raises it.
        from window_focus import focus_window
        focus_window(os.path.basename(folder))
    QTimer.singleShot(500, _raise)


class _AutoReload:
    """Mixin: on_show() again when the shown routine run gains data, and when the routine ends."""

    def _watch(self):
        self._stamp = (_data_stamp(), bool(rt.progress().get("active")))
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(POLL_MS)

    def _tick(self):
        if not self.isVisible() or not self._runs:
            return
        stamp = (_data_stamp(), bool(rt.progress().get("active")))
        new_data, ended = stamp[0] != self._stamp[0], self._stamp[1] and not stamp[1]
        self._stamp = stamp
        run = self._runs[max(0, self.runs.currentIndex())]
        reading, self._hk_reading = getattr(self, "_hk_reading", False), HK.pending() > 0   # HK points still arriving
        if ended or (new_data and (run.get("result") == "running" or reading or self._hk_reading)):
            self.on_show()


class DailyRoutineTabs(QWidget):
    def __init__(self):
        super().__init__()
        from pages.daily_routine import DailyRoutinePage
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.routine = DailyRoutinePage()
        self.results = ResultsPage()
        self.stats = StatisticsPage()
        self.tabs.addTab(self.routine, "Routine")
        self.tabs.addTab(self.results, "Results")
        self.tabs.addTab(self.stats, "Statistics")
        self.tabs.currentChanged.connect(self._tab_changed)
        lay.addWidget(self.tabs)

    def _tab_changed(self, i):
        """Results and Statistics follow the run picked on the other one."""
        if i == 1:
            self.results.on_show(self.stats.runs.currentText())
        elif i == 2:
            self.stats.on_show(self.results.runs.currentText())



def _wl_groups(rows):
    """[(wavelength, run span)] of registry rows, in the order the wavelengths were first taken."""
    by = {}
    for r in sorted(rows, key=lambda r: r["_time"]):
        if r.get("wavelength"):
            by.setdefault(r["wavelength"], []).append(r)
    return [(wl, RR.run_span(rs)) for wl, rs in by.items()]


class _Bus(QObject):
    loaded = pyqtSignal(int, object)
    uploaded = pyqtSignal(bool, str)


def _trim_memory():
    """Give the memory of finished report pages back to the system (glibc keeps freed pages otherwise)."""
    import ctypes
    import gc
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass


def _group(*widgets):
    """Widgets kept together on one line of a wrapping control row."""
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(6)
    for x in widgets:
        h.addWidget(x)
    return w


def _sample_pixmap(line, w=30, h=14):
    """Line style and marker of one legend entry."""
    pm = QPixmap(w, h)
    pm.fill(Qt.transparent)
    if line is None:
        return pm
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    col = QColor(to_hex(line.get_color()))
    ls = line.get_linestyle()
    if ls in PEN:
        p.setPen(QPen(col, 1.5, PEN[ls]))
        p.drawLine(1, h // 2, w - 1, h // 2)
    mk, r, c = line.get_marker(), 3.5, QPointF(w / 2, h / 2)
    p.setPen(QPen(col, 1.2))
    p.setBrush(Qt.NoBrush if str(line.get_markerfacecolor()).lower() == "none" else col)
    if mk == "o":
        p.drawEllipse(c, r, r)
    elif mk == "s":
        p.drawRect(QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r))
    elif mk in ("^", "v"):
        s = -1 if mk == "^" else 1
        p.drawPolygon(QPolygonF([QPointF(c.x(), c.y() + s * r), QPointF(c.x() - r, c.y() - s * r),
                                 QPointF(c.x() + r, c.y() - s * r)]))
    p.end()
    return pm


def _entry(handle, ax):
    """(sample line, artists to hide) of one legend handle; proxies have no artists."""
    if isinstance(handle, Container):
        return (handle[0] if len(handle) and handle[0] is not None else None), list(handle.get_children())
    return handle, ([handle] if handle in ax.lines else [])


class _Chip(QWidget):
    def __init__(self, label, line, active, on_toggle, on_fit):
        super().__init__()
        h = QHBoxLayout(self)
        h.setContentsMargins(2, 1, 4, 1)
        h.setSpacing(5)
        self.sample = QLabel()
        self.sample.setPixmap(_sample_pixmap(line))
        self.name = QLabel(label)
        h.addWidget(self.sample)
        h.addWidget(self.name)
        self.active, self.on_toggle, self.on_fit = active, on_toggle, on_fit
        if active:
            self.setCursor(Qt.PointingHandCursor)

    def set_hidden(self, hidden):
        self.name.setStyleSheet(f"color: {theme.C['muted']}; text-decoration: line-through;" if hidden else "")
        fx = QGraphicsOpacityEffect(self.sample)
        fx.setOpacity(0.3 if hidden else 1.0)
        self.sample.setGraphicsEffect(fx)

    def mouseReleaseEvent(self, ev):
        if self.active and ev.button() == Qt.LeftButton and self.rect().contains(ev.pos()):
            (self.on_toggle if ev.pos().x() <= self.sample.geometry().right() + 2 else self.on_fit)()


class _LegendStrip(QScrollArea):
    """Legend above the plot: wraps into rows and scrolls beyond LEGEND_ROWS."""

    def __init__(self, card):
        super().__init__()
        self.card = card
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.NoFrame)
        self.body = QWidget()
        self.flow = FlowLayout(self.body, hspace=14, vspace=2)
        self.flow.justify = False
        self.setWidget(self.body)
        self.chips, self._key = {}, None
        self.hide()

    def set_items(self, items):
        """items: [(label, sample line, artists)]; chips are rebuilt only when the entries change."""
        key = [(lab, None if ln is None else (to_hex(ln.get_color()), ln.get_linestyle(), ln.get_marker(),
                                             str(ln.get_markerfacecolor())), bool(arts)) for lab, ln, arts in items]
        if key != self._key:
            self._key = key
            while self.flow.count():
                self.flow.takeAt(0).widget().deleteLater()
            self.chips = {}
            for lab, ln, arts in items:
                c = _Chip(lab, ln, bool(arts), lambda l=lab: self.card.toggle(l), lambda l=lab: self.card.fit(l))
                self.flow.addWidget(c)
                self.chips[lab] = c
        for lab, c in self.chips.items():
            c.set_hidden(lab in self.card.hidden)
        self.setVisible(bool(items))
        self._fit_height()

    def _fit_height(self):
        if not self.chips:
            return
        row = max(c.sizeHint().height() for c in self.chips.values())
        cap = LEGEND_ROWS * row + (LEGEND_ROWS - 1) * self.flow.vspace
        self.setFixedHeight(min(self.flow.heightForWidth(max(self.viewport().width(), 50)), cap) + 2)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._fit_height()


class _GripHandle(QSplitterHandle):
    """Splitter handle with three dots in a theme colour (the style's own grip is too faint in light mode)."""

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.C["scroll_hover" if self.underMouse() else "scroll_handle"]))
        c, horiz = QPointF(self.rect().center()) + QPointF(0.5, 0.5), self.orientation() == Qt.Horizontal
        for k in (-1, 0, 1):
            p.drawEllipse(c + (QPointF(0, 7 * k) if horiz else QPointF(7 * k, 0)), 2.0, 2.0)

    def enterEvent(self, ev):
        self.update()

    def leaveEvent(self, ev):
        self.update()


class _GripSplitter(QSplitter):
    def createHandle(self):
        return _GripHandle(self.orientation(), self)


class _Canvas(FigureCanvasQTAgg):
    """Size hint fixed at the minimum: matplotlib's follows the current size, so card minimums ratcheted up."""

    def sizeHint(self):
        return self.minimumSize()


class _Card(QGroupBox):
    def __init__(self, title, rows=1, controls=()):
        super().__init__(title)
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 6, 8, 6)
        head = FlowLayout()
        for w in controls:
            head.addWidget(w)
        self.auto_btn, self.home_btn = QPushButton("Auto Scale"), QPushButton("Reset View")
        btns = _group(self.auto_btn, self.home_btn)
        btns.setProperty("flow_right", True)
        head.addWidget(btns)
        self.auto_btn.clicked.connect(self.autoscale)
        self.home_btn.clicked.connect(self.reset_view)
        v.addLayout(head)
        self.hidden = set()       # legend labels the operator switched off
        self.entries = {}         # label -> (axes, sample line, artists)
        self.user_pos = None      # plot area positions after a border drag
        self.pick = {}            # data line -> [(date, run, ch)] per point
        self.on_run = None        # called with (date, run, ch) when a point is clicked
        self._pending = None
        self._resize = None
        self.strip = _LegendStrip(self)
        v.addWidget(self.strip)
        self.fig = Figure(facecolor="white")
        self.canvas = _Canvas(self.fig)
        self.canvas.setMinimumSize(200, 140)
        self.axes = [self.fig.add_subplot(rows, 1, i + 1) for i in range(rows)]
        v.addWidget(self.canvas, 1)
        self.user_view = None
        self.zoom = PlotZoom(self.canvas, on_zoom=self._keep_view, on_home=self.forget_view, min_span=MIN_SPAN,
                             skip=lambda ev: self._edge(ev) is not None)
        self.canvas.mpl_connect("button_press_event", self._edge_press)
        self.canvas.mpl_connect("motion_notify_event", self._edge_motion)
        self.canvas.mpl_connect("button_release_event", lambda _ev: setattr(self, "_resize", None))
        self.canvas.mpl_connect("pick_event", self._on_pick)
        self.canvas.mpl_connect("button_release_event", self._pick_release)
        self.foot = QLabel("")
        self.foot.setWordWrap(True)
        self.foot.setStyleSheet(f"color: {theme.C['muted']};")
        self.foot.linkActivated.connect(self._foot_link)
        v.addWidget(self.foot)
        self.wl_pick, self._span, self._groups, self._tail = None, "", [], ""

    def clear(self, foot=""):
        self.entries = {}
        self.pick = {}
        self.strip.set_items([])
        for ax in self.axes:
            ax.clear()
            ax.grid(True, alpha=0.3)
            ax.tick_params(labelsize=8)
        self.foot.setTextFormat(Qt.PlainText)
        self.foot.setText(foot)

    def foot_runs(self, span, groups, tail=""):
        """Foot 'Runs <span>   375 nm / 405 nm / All' with clickable wavelengths; groups = [(wavelength, run span)]."""
        self._span, self._groups, self._tail = span, groups, tail
        if self.wl_pick not in [g[0] for g in groups] + ["all"]:
            self.wl_pick = None
        self._render_foot()

    def _foot_link(self, href):
        self.wl_pick = None if href == self.wl_pick else href
        self._render_foot()

    def _render_foot(self):
        def h(text):
            return html.escape(text).replace("  ", "&nbsp; ")
        spans = dict(self._groups)
        if self.wl_pick == "all":
            runs = "Runs " + "   ".join(f"{wl} nm {sp}" for wl, sp in self._groups)
        elif self.wl_pick in spans:
            runs = f"Runs {self.wl_pick} nm {spans[self.wl_pick]}"
        else:
            runs = f"Runs {self._span}"
        links = []
        for wl, _sp in sorted(self._groups, key=lambda g: (len(g[0]), g[0])) + [("all", "")]:
            text = "All" if wl == "all" else f"{wl} nm"
            text = f"<b>{h(text)}</b>" if wl == self.wl_pick else h(text)
            links.append(f'<a href="{h(wl)}">{text}</a>')
        self.foot.setTextFormat(Qt.RichText)
        self.foot.setText(f"{h(runs)}&nbsp;&nbsp; {' / '.join(links)}{h(self._tail)}")

    def legend(self, ax, handles=None):
        """Legend strip entries of this plot; data the operator switched off stays off across redraws."""
        items = []
        for h in handles if handles is not None else ax.get_legend_handles_labels()[0]:
            line, arts = _entry(h, ax)
            lab = h.get_label()
            for a in arts:
                a.set_visible(lab not in self.hidden)
            self.entries[lab] = (ax, line, arts)
            items.append((lab, line, arts))
        self.strip.set_items(items)

    def pickable(self, bar, ids, ch):
        """Points of this errorbar open their run when clicked; ids: [(date, run) | None] per point."""
        line = bar[0]
        if line is not None and ids:
            line.set_picker(PICK_PX)
            self.pick[line] = [i and (i[0], i[1], ch) for i in ids]

    def _on_pick(self, ev):
        ids = self.pick.get(ev.artist)
        me = ev.mouseevent
        if not ids or me.button != 1 or not ev.artist.get_visible() or self._pending or self._edge(me) is not None:
            return
        xy = ev.artist.get_transform().transform(ev.artist.get_xydata())
        k = min(ev.ind, key=lambda i: (xy[i][0] - me.x) ** 2 + (xy[i][1] - me.y) ** 2)
        if k < len(ids) and ids[k]:
            self._pending = (ids[k], me.x, me.y)
            self.zoom.picked = True

    def _pick_release(self, ev):
        """A click on a point opens its run; a drag from it is a zoom."""
        pend, self._pending = self._pending, None
        if pend and self.on_run and abs(ev.x - pend[1]) <= 3 and abs(ev.y - pend[2]) <= 3:
            self.on_run(*pend[0])

    def toggle(self, label):
        """Sample click: hide or show this data."""
        self.hidden ^= {label}
        for a in self.entries.get(label, (None, None, []))[2]:
            a.set_visible(label not in self.hidden)
        self.strip.set_items([(lab, ln, arts) for lab, (_ax, ln, arts) in self.entries.items()])
        self.canvas.draw_idle()

    def fit(self, label):
        """Name click: show this data and fit Y to it."""
        if label in self.hidden:
            self.toggle(label)
        ax, line, arts = self.entries.get(label, (None, None, []))
        if ax is not None and line is not None and arts:
            self.zoom.fit_y(ax, [line])

    def done(self):
        self.fig.tight_layout(pad=0.8)
        if self.user_pos:
            for ax, pos in zip(self.axes, self.user_pos):
                ax.set_position(pos)
        self.zoom.set_home()
        if self.user_view:
            for ax, (xl, yl) in zip(self.axes, self.user_view):
                ax.set_xlim(*xl)
                ax.set_ylim(*yl)
        self.canvas.draw_idle()

    # ---------------------------------------------------------------- view
    def forget_view(self):
        """The next drawing uses its own ranges again (new run or new selection)."""
        self.user_view = None

    def forget_run(self):
        self.user_view, self.wl_pick = None, None

    def _keep_view(self):
        self.user_view = [(ax.get_xlim(), ax.get_ylim()) for ax in self.axes]

    def reset_view(self):
        self.user_pos = None
        self.fig.tight_layout(pad=0.8)
        self.zoom.reset()

    # ---------------------------------------------------------------- plot border drag
    def _edge(self, ev):
        """(axes, side) when the pointer is on a plot border, else None."""
        for ax in self.axes:
            b = ax.bbox
            inx = b.x0 - EDGE_PX <= ev.x <= b.x1 + EDGE_PX
            iny = b.y0 - EDGE_PX <= ev.y <= b.y1 + EDGE_PX
            for side, d, ok in (("left", ev.x - b.x0, iny), ("right", ev.x - b.x1, iny),
                                ("bottom", ev.y - b.y0, inx), ("top", ev.y - b.y1, inx)):
                if ok and abs(d) <= EDGE_PX:
                    return ax, side
        return None

    def _edge_press(self, ev):
        if ev.button == 1:
            self._resize = self._edge(ev)

    def _edge_motion(self, ev):
        if self._resize is None:
            if self.zoom.drag is None:
                e = self._edge(ev)
                if e is None:
                    self.canvas.unsetCursor()
                else:
                    self.canvas.setCursor(Qt.SizeHorCursor if e[1] in ("left", "right") else Qt.SizeVerCursor)
            return
        ax, side = self._resize
        x0, y0, x1, y1 = ax.get_position().extents
        fx = min(max(ev.x / self.fig.bbox.width, 0.0), 1.0)
        fy = min(max(ev.y / self.fig.bbox.height, 0.0), 1.0)
        if side == "left":
            x0 = min(fx, x1 - MIN_FRAC)
        elif side == "right":
            x1 = max(fx, x0 + MIN_FRAC)
        elif side == "bottom":
            y0 = min(fy, y1 - MIN_FRAC)
        else:
            y1 = max(fy, y0 + MIN_FRAC)
        ax.set_position([x0, y0, x1 - x0, y1 - y0])
        self.user_pos = [a.get_position().bounds for a in self.axes]
        self.canvas.draw_idle()

    def autoscale(self):
        self.zoom.autoscale()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        need = max(self.heightForWidth(self.width()), 0)   # wrapped control and foot lines need more height
        if need != self.minimumHeight():
            self.setMinimumHeight(need)


class _SavingDialog(QDialog):
    """Modal 'Saving report' box with a progress bar; the report is drawn on the GUI thread, so the page
    pumps events through step() and the operator cannot change the run meanwhile."""

    def __init__(self, parent, title):
        super().__init__(parent, Qt.Dialog | Qt.CustomizeWindowHint | Qt.WindowTitleHint)
        self.setWindowTitle("Report")
        self.setModal(True)
        self.setMinimumWidth(520)
        c = theme.C
        v = QVBoxLayout(self)
        v.setContentsMargins(24, 20, 24, 20)
        head = QLabel(title)
        head.setStyleSheet(f"font-size: 16pt; font-weight: 800; color: {c['accent']};")
        v.addWidget(head)
        self.text = QLabel("Preparing...")
        self.text.setStyleSheet("font-size: 12pt;")
        v.addWidget(self.text)
        self.bar = QProgressBar()
        self.bar.setFixedHeight(26)
        self.bar.setRange(0, 0)
        self.bar.setStyleSheet(f"QProgressBar {{ border: 1px solid {c['border']}; border-radius: 6px;"
                               f" background: {c['card']}; text-align: center; font-weight: 700; }}"
                               f"QProgressBar::chunk {{ background: {theme.STATE['done'][0]}; border-radius: 5px; }}")
        v.addWidget(self.bar)
        self.show()
        QApplication.processEvents()

    def step(self, text, value=None, total=None):
        self.text.setText(text)
        if total is not None:
            self.bar.setRange(0, total)
        if value is not None:
            self.bar.setValue(value)
        QApplication.processEvents()


class _ReportFoot:
    text = ""

    def setText(self, text):
        self.text = text

    def setToolTip(self, _text):
        pass


class _ReportCard:
    """Stands in for a card so the page's drawing code fills one panel of the exported report (ROOT-like style)."""
    FONT = ["Nimbus Roman", "Liberation Serif"]   # Times look-alikes installed here

    def __init__(self, ax):
        self.axes, self.foot, self.handles = [ax], _ReportFoot(), []

    def clear(self, foot=""):
        self.foot.setText(foot)

    def foot_runs(self, _span, _groups, _tail=""):
        pass

    def legend(self, ax, handles=None):
        self.handles = handles if handles is not None else ax.get_legend_handles_labels()[0]   # one legend per page

    def pickable(self, _bar, _ids, _ch):
        pass

    def done(self):
        ax = self.axes[0]
        for ln in ax.get_lines():
            ln.set_markersize(1.6 * ln.get_markersize())
            ln.set_linewidth(1.3 * ln.get_linewidth())
        for t in ax.texts:
            t.set_fontsize(10)
            t.set_fontfamily(self.FONT)
        for lab in (ax.xaxis.label, ax.yaxis.label):
            lab.set_fontsize(15)
            lab.set_fontfamily(self.FONT)
            lab.set_color("black")
        ax.minorticks_on()
        ax.tick_params(which="both", direction="in", top=True, right=True, labelsize=13, labelcolor="black",
                       labelfontfamily=self.FONT)
        ax.tick_params(which="major", length=8)
        ax.tick_params(which="minor", length=4)


class ResultsPage(_AutoReload, QWidget):
    REPORT_DIR = os.path.join(env.ADC_DIR, "Data", "RoutineStats", "reports")

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        top = QHBoxLayout()
        top.addWidget(QLabel("Routine Run"))
        self.runs = QComboBox()
        self.runs.setMinimumWidth(320)
        self.runs.currentIndexChanged.connect(lambda _i: self.load())
        top.addWidget(self.runs)
        self.rtype = QComboBox()
        for text, val in (("All types", "any"),) + tuple((name, key) for key, name in RR.TYPES) + (("Show all", "all"),):
            self.rtype.addItem(text, val)
        self.rtype.currentIndexChanged.connect(lambda _i: self.on_show())
        top.addWidget(self.rtype)
        self._known, self._counts = [], []
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(lambda: self.on_show())
        top.addWidget(self.refresh_btn)
        self.open_routine_btn = QPushButton("Open Routine Data")
        self.open_routine_btn.clicked.connect(self._open_routine)
        top.addWidget(self.open_routine_btn)
        self.open_scan_btn = QPushButton("Open Scan Data")
        self.open_scan_btn.clicked.connect(self._open_scan)
        top.addWidget(self.open_scan_btn)
        self.export_btn = QPushButton("Export...")
        menu = QMenu(self.export_btn)
        menu.addAction("Current view").triggered.connect(lambda _c=False: self.export_png())
        menu.addAction("Report (Recommended, All metrics)").triggered.connect(lambda _c=False: self.export_report())
        self.export_btn.setMenu(menu)
        top.addWidget(self.export_btn)
        self.drive_chk = QCheckBox("Also save to Google Drive")
        self.drive_chk.setToolTip(f"Report PDFs (Export and the automatic one after a routine) are also copied to\n"
                                  f"{drive.FOLDER_URL}\nwith rclone remote '{drive.REMOTE}'.")
        self.drive_chk.setChecked(drive.enabled())
        self.drive_chk.toggled.connect(self._drive_toggled)
        top.addWidget(self.drive_chk)
        self._scan_files = []
        self._sync_open()
        self.pmts = QLabel("")
        self.pmts.setStyleSheet("margin-left: 12px;")
        top.addWidget(self.pmts)
        self.state = QLabel("")
        self.state.setStyleSheet(f"color: {theme.C['muted']};")
        self.state.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)   # a long path clips instead of widening the window
        top.addWidget(self.state, 1)
        top.addStretch(1)
        lay.addLayout(top)
        self.gsel = {"metric": "qe", "xmode": "raw", "axis": "both", "step": "All", "wl": "All", "norm": False,
                     "n70": False}
        self._gen = None
        self.sel = {"dark": "live", "hv_scan": "qe", "stability": "qe"}
        self._res = None
        self.cards = {"dark": _Card("Dark Rate", controls=self._radio_row(
                          "dark", (("Live time", "live"), ("Recorded", "rec"), ("Pulses", "clean")))),
                      "hv_scan": _Card("HV Scan", controls=self._radio_row(
                          "hv_scan", (("QE", "qe"), ("Gain", "gain"), ("TTS (FWHM)", "fwhm")))),
                      "stability": _Card("Stability", controls=self._radio_row(
                          "stability", (("QE", "qe"), ("Gain", "gain"), ("TTS (FWHM)", "fwhm"),
                                        ("Dark rate (LD on)", RR.DARK_ON)))),
                      "general": _Card("General Scan", controls=self._general_controls())}
        # cards in splitters: the operator drags the handles to resize them
        rows = _GripSplitter(Qt.Vertical)
        for left, right in (("dark", "hv_scan"), ("stability", "general")):
            row = _GripSplitter(Qt.Horizontal)
            row.addWidget(self.cards[left])
            row.addWidget(self.cards[right])
            row.setStretchFactor(0, 1)
            row.setStretchFactor(1, 3)
            row.setSizes([1000, 3000])
            row.setChildrenCollapsible(False)
            row.setHandleWidth(8)
            rows.addWidget(row)
        rows.setChildrenCollapsible(False)
        rows.setHandleWidth(8)
        rows.setSizes([1000, 1000])
        lay.addWidget(rows, 1)
        self._runs, self._seq, self._busy = [], 0, False
        self.spin = _Spinner(self)
        self._shown = None   # label of the run on screen: its reloads keep it shown, no spinner
        self._auto, self._was_active = None, bool(rt.progress().get("active"))
        self.auto_timer = QTimer(self)
        self.auto_timer.timeout.connect(self._auto_tick)
        self.auto_timer.start(POLL_MS)
        self.bus = _Bus()
        self.bus.loaded.connect(self._loaded)
        self.bus.uploaded.connect(self._uploaded)
        self._watch()
        self.drawer = None
        for card in self.cards.values():
            card.clear()
            card.on_run = self._show_run

    def _show_run(self, date, run, ch):
        if str(date).startswith("HK"):
            rec = HK.point(str(date)[2:], run)
            if rec:
                if getattr(self, "hk_window", None) is None:
                    from widgets.hk_point import HKPointWindow
                    self.hk_window = HKPointWindow(self.window())
                self.hk_window.show_point(rec)
            return
        if self.drawer is None:   # made on first use: the page has its window by then
            self.drawer = RunDrawer(self, {ch: c[1] for ch, c in CH.items()})
        self.drawer.show_run(date, run, ch)

    def hideEvent(self, ev):
        super().hideEvent(ev)
        if self.drawer is not None:
            self.drawer.close_drawer(animate=False)

    def _radio_row(self, key, options):
        g = QButtonGroup(self)
        radios = []
        for text, val in options:
            rb = QRadioButton(text)
            rb.setProperty("val", val)
            rb.setChecked(self.sel[key] == val)
            g.addButton(rb)
            radios.append(rb)
        g.buttonClicked.connect(lambda btn, k=key: self._pick(k, btn.property("val")))
        return [_group(QLabel("Metric:"), *radios)]

    def _pick(self, key, val):
        self.sel[key] = val
        self.cards[key].forget_view()
        if self._res is not None:
            self._draw_panels(self._res)

    def _general_controls(self):
        self.gstep_label = QLabel("Step:")
        self.gstep = QComboBox()
        self.gstep.addItem("All")
        self.gstep.setMinimumWidth(160)
        self.gstep.currentTextChanged.connect(lambda t: t and self._gsel("step", t))
        self.gwl_label = QLabel("Wavelength:")
        self.gwl = QComboBox()
        self.gwl.addItem("All")
        self.gwl.setMinimumWidth(110)
        self.gwl.currentTextChanged.connect(lambda t: t and self._gsel("wl", t))
        self.gnorm = QCheckBox("Normalize to Monitor")
        self.gnorm.setChecked(self.gsel["norm"])
        self.gnorm.toggled.connect(lambda on: self._gsel("norm", on))
        self.gn70 = QCheckBox("Normalize to \u00b170\u00b0 mean")
        self.gn70.setChecked(self.gsel["n70"])
        self.gn70.toggled.connect(lambda on: self._gsel("n70", on))
        self.gstep_box, self.gwl_box = _group(self.gstep_label, self.gstep), _group(self.gwl_label, self.gwl)
        for w in (self.gstep_box, self.gwl_box):
            w.setVisible(False)
        out = [self.gstep_box, self.gwl_box, self.gnorm, self.gn70]
        self.gbuttons = {}
        for name, label, options in (
                ("metric", "Metric", (("QE", "qe"), ("Gain", "gain"), ("TTS (FWHM)", "fwhm"),
                                      ("Dark rate (LD on)", RR.DARK_ON))),
                ("xmode", "X axis", (("Raw", "raw"), ("Hamamatsu", "hamamatsu"))),
                ("axis", "Scan axis", (("Both", "both"), ("X", "X"), ("Y", "Y")))):
            g = QButtonGroup(self)
            radios = []
            for text, val in options:
                rb = QRadioButton(text)
                rb.setProperty("val", val)
                rb.setChecked(self.gsel[name] == val)
                g.addButton(rb)
                radios.append(rb)
            g.buttonClicked.connect(lambda btn, n=name: self._gsel(n, btn.property("val")))
            self.gbuttons[name] = g
            out.append(_group(QLabel(label + ":"), *radios))
        return out

    def _gsel(self, name, val):
        self.gsel[name] = val
        self.cards["general"].forget_view()
        if self._gen is not None:
            self._draw_general(self._gen)

    def on_show(self, prefer=None):
        """Reload the run list (keeping the selection, or the given label) and the selected run."""
        cur = prefer or self.runs.currentText()
        self._known = rt.routine_runs() + RR.archived_runs()
        try:
            self._counts = RR.data_counts(self._known)
        except Exception:
            self._counts = [1] * len(self._known)
        want = self.rtype.currentData()
        self._runs = [r for r, n in zip(self._known, self._counts) if want == "all" or (
            not r.get("archived") and (n or r.get("result") == "running")
            and want in ("any", RR.routine_type(r)))]
        if prefer and prefer == self._auto and not any(r["label"] == prefer for r in self._runs):
            self._auto = None   # a routine without data gets no report
        self.runs.blockSignals(True)
        self.runs.clear()
        for r in self._runs:
            self.runs.addItem(r["label"])
        i = self.runs.findText(cur)
        self.runs.setCurrentIndex(i if i >= 0 else 0)
        self.runs.blockSignals(False)
        self.load()

    def _routine_path(self):
        i = self.runs.currentIndex()
        return RR.routine_file(self._runs[i]) if 0 <= i < len(self._runs) else ""

    def _sync_open(self):
        path = self._routine_path()
        self.open_routine_btn.setEnabled(bool(path) and os.path.isfile(path))
        self.open_scan_btn.setEnabled(bool(self._scan_files))

    def _open_routine(self):
        from pages.daq.data_files import DataFilesPage
        path = self._routine_path()
        if path and os.path.isfile(path):
            DataFilesPage.open_file(path)

    def _open_scan(self):
        from pages.daq.data_files import DataFilesPage
        files = self._scan_files
        if len(files) == 1:
            DataFilesPage.open_file(files[0][1])
        elif files:
            menu = QMenu(self)
            for label, path in files:
                menu.addAction(label).triggered.connect(lambda _c=False, p=path: DataFilesPage.open_file(p))
            menu.exec_(self.open_scan_btn.mapToGlobal(self.open_scan_btn.rect().bottomLeft()))

    def _export_name(self):
        i = self.runs.currentIndex()
        run = self._runs[i] if 0 <= i < len(self._runs) else {}
        return RR.report_name(run, self._known, self._counts) if run else "KOR_run"

    def _pdf_meta(self):
        i = self.runs.currentIndex()
        name = self._runs[i].get("name", "") if 0 <= i < len(self._runs) else ""
        return {"Title": f"KOR {name} routine report ({self.runs.currentText().strip()})"}

    def _export_path(self, tail, filters):
        os.makedirs(StatisticsPage.EXPORT_DIR, exist_ok=True)
        return QFileDialog.getSaveFileName(
            self, "Export results", os.path.join(StatisticsPage.EXPORT_DIR, self._export_name() + tail), filters)[0]

    def export_png(self):
        """The page as shown in one PNG."""
        path = self._export_path(".png", "PNG (*.png)")
        if not path:
            return
        if not path.lower().endswith(".png"):
            path += ".png"
        if self.grab().save(path, "PNG"):
            self.state.setText(f"Exported: {path}")
            _show_in_folder(path)
        else:
            self.state.setText(f"Export failed: {path}")

    def _report_pages(self, progress=None):
        """Summary and DAQ log first, every option of every card with data, a General Scan page per step and
        wavelength and metric (QE raw, Gain, TTS; X and Y scan axes apart), Charge / Time / Contour pages per CAEN step,
        then the Statistics tables; drawn by the page's own code."""
        res, gen = self._res, self._gen
        head = (self.runs.currentText(), self.pmts.text())
        modes = (("Live time", "live"), ("Recorded", "rec"), ("Pulses", "clean"))
        metrics = (("QE", "qe"), ("Gain", "gain"), ("TTS (FWHM)", "fwhm"))
        keep_sel, keep_gsel, keep_cards = dict(self.sel), dict(self.gsel), dict(self.cards)
        pages = []

        def page(name, key, title, panels, draw, ncols=1, foot="", legend=None):
            ncols = 2 if len(panels) == 4 else ncols
            fig = Figure(figsize=(11.69, 8.27), facecolor="white")
            nrows = -(-len(panels) // ncols)
            handles = []
            for k, (label, setup) in enumerate(panels):
                card = _ReportCard(fig.add_subplot(nrows, ncols, k + 1))
                self.cards[key] = card
                setup()
                draw()
                card.axes[0].set_title(label, fontsize=16, loc="left", fontfamily=card.FONT)
                if key == "general" and not getattr(card, "time_axis", False):
                    self._raw_angle_axis(card.axes[0], gen)
                foot, handles = foot or card.foot.text, legend or handles or card.handles
            ncol = min(len(handles), 6 if len(handles) <= 6 else 4)
            if handles:
                fig.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, 0.91), ncol=ncol,
                           prop={"family": _ReportCard.FONT, "size": 13 if len(handles) <= 6 else 11}, frameon=False,
                           columnspacing=1.0, handlelength=1.6, handletextpad=0.5)
            self._report_head(fig, title, head, foot)
            fig.tight_layout(rect=(0, 0, 1, 0.87 - 0.032 * max(0, -(-len(handles) // max(ncol, 1)) - 1)), pad=1.2)
            pages.append((name, fig))
            if progress:
                progress(f"Drawing {title} ...")

        def opt(key, val):
            return lambda: self.sel.__setitem__(key, val)

        def gaxis(axis, metric="qe"):
            return lambda: self.gsel.update(metric=metric, norm=False, n70=False, axis=axis)

        try:
            for key, title, options, has_data, draw in (
                    ("dark", "Dark Rate", modes, bool(res["dark"]["steps"]), lambda: self._draw_dark(res["dark"])),
                    ("hv_scan", "HV Scan", metrics, bool(res["hv_scan"]["steps"]),
                     lambda: self._draw_hv(self.cards["hv_scan"], res["hv_scan"], scan=True)),
                    ("stability", "Stability", metrics + (("Dark rate (LD on, pre-trigger window)", RR.DARK_ON),),
                     bool(res["stability"]["steps"]),
                     lambda: self._draw_stability(res["stability"]))):
                if has_data:
                    page(key, key, title, [(label, opt(key, val)) for label, val in options], draw)
            if gen and gen["rows"]:   # CAEN scans only: HK Digitizer steps are not in the report
                gmetrics = (("QE (raw)", "qe", ""), ("Gain", "gain", "_gain"), ("TTS (FWHM)", "fwhm", "_tts"),
                            ("Dark rate (LD on, pre-trigger window)", RR.DARK_ON, "_dark"))
                pmts = [Line2D([], [], color=CH[ch][1], marker=CH[ch][2], ms=6, lw=1, label=CH[ch][0]) for ch in (0, 1, 2)]
                self.gsel["xmode"] = "hamamatsu"
                n = len(gen["sessions"])
                sess = zip(gen["sessions"], gen.get("steps") or gen["sessions"], gen.get("wls") or [""] * n)
                gkinds = gen.get("kinds") or []
                for i, (lab, st, wl) in enumerate(sess):
                    if i < len(gkinds) and gkinds[i] == "hk_scan":
                        continue
                    # one page per session (step and wavelength): wavelengths are never overlaid
                    self.gsel.update(step=st, wl=f"{wl} nm" if wl else "All")
                    ids = {x for k, v in gen["ids"].items() if k[1] == lab for x in v if x}
                    foot = f"Runs {RR.run_span([{'date': d, 'run': r} for d, r in ids])}" if ids else ""
                    for mlab, m, suffix in gmetrics:
                        if m == RR.DARK_ON and not any(k[0] == m and k[1] == lab for k in gen["series"]):
                            continue
                        panels = [(f"{mlab}    {a}-axis scan", gaxis(a, m)) for a in ("X", "Y")]
                        bad = any(k[0] == m and k[1] == lab and k[4] == "hamamatsu" for k in gen.get("fail") or {})
                        timed = {"on": False}   # the dark rate page gets a third panel against time
                        if m == RR.DARK_ON:
                            panels = [(t, lambda f=f: (timed.update(on=False), f())) for t, f in panels]
                            panels.append((f"{mlab}    against time", lambda: timed.update(on=True)))
                        page(f"general_{i + 1}{suffix}", "general", lab, panels,
                             lambda: self._draw_dark_time(gen, lab) if timed["on"] else self._draw_general(gen),
                             foot=foot, legend=pmts + [fail_handle()] if bad else pmts)
                for i, (lab, wl) in enumerate(zip(gen["sessions"], gen.get("wls") or [""] * n)):
                    if i < len(gkinds) and gkinds[i] == "hk_scan":
                        continue
                    ids = {x for k, v in gen["ids"].items() if k[1] == lab for x in v if x}
                    rows = [r for r in gen["rows"] if str(r.get("run", "")).isdigit()
                            and any(d == r["date"] and lo <= int(r["run"]) <= hi for d, lo, hi in RP.blocks(ids))]
                    try:
                        pages += [(f"general_{i + 1}_{k}", fig) for k, fig in RP.pages(lab, wl, rows, progress)]
                    except Exception as e:   # never lose the rest of the report over these pages
                        applog.log(f"[WARN] Report: Charge/Time/Contour pages of {lab} failed: {e}")
        finally:
            self.sel, self.gsel = keep_sel, keep_gsel
            self.cards.update(keep_cards)
            self.gnorm.setEnabled(keep_gsel["metric"] == "qe")
            self.gn70.setEnabled(keep_gsel["metric"] in N70_METRICS)
        i = self.runs.currentIndex()
        if 0 <= i < len(self._runs):
            try:
                blocks = RS.stats(self._runs[i])
            except Exception as e:
                blocks = [{"title": "Statistics", "info": f"failed: {e}", "rows": []}]
            if progress:
                progress("Drawing the summary, conditions and statistics ...")
            pages = self._summary_pages(self._runs[i], res, blocks, head) + pages + self._stats_pages(blocks, head)
        return pages

    @staticmethod
    def _report_head(fig, title, head, foot=""):
        """Title left; run label and the PMTs (in their plot colours) right."""
        font = _ReportCard.FONT
        fig.text(0.02, 0.965, title, fontsize=22, fontweight="bold", va="center", fontfamily=font)
        label, pmts = head
        parts = [TextArea(label + "     ", textprops=dict(fontsize=14, fontfamily=font))]
        colors = {"Mon": CH[0][1], "Rot1": CH[1][1], "Rot2": CH[2][1]}
        for k, piece in enumerate(x for grp in pmts.split(" | ") for x in grp.split("   ") if x.strip()):
            parts.append(TextArea(("   " if k else "") + piece.strip(), textprops=dict(
                fontsize=14, fontfamily=font, fontweight="bold", color=colors.get(piece.split()[0], "black"))))
        fig.add_artist(AnnotationBbox(HPacker(children=parts, sep=0, pad=0, align="baseline"), (0.98, 0.965),
                                      xycoords="figure fraction", box_alignment=(1, 0.5), frameon=False))
        fig.text(0.02, 0.925, foot, fontsize=12, va="center", fontfamily=font)

    @classmethod
    def _summary_pages(cls, run, res, blocks, head):
        """First pages: who, when, HV, laser and steps of the routine, then the lab conditions (temperature,
        humidity, B-field, HV VMon) from 12 h before the first step to 10 min after the last, with the
        mean +- sd over the routine itself."""
        from datetime import timedelta
        font = _ReportCard.FONT
        rows = sorted(res["dark"]["rows"] + res["hv_scan"]["rows"] + res["stability"]["rows"]
                      + res["general"]["rows"], key=lambda r: r["_time"])
        a, b = RR.run_span_all(run)
        shifters, experts = RR.people(rows)
        hvsets = RR.hv_pmt_text(rows)
        laser = RR.laser_text([r for r in rows if r.get("mode") == "laser"])
        events = sorted({r.get("events") for r in rows if r.get("events")}, key=lambda e: float(e))
        dur = (b - a).total_seconds() if a else 0
        info = [("Routine", f"{run.get('name', '')}   {run.get('result') or ''}".strip()),
                ("Time", f"{a.astimezone(JST):%Y-%m-%d %H:%M:%S}  -  {b.astimezone(JST):%Y-%m-%d %H:%M:%S}   "
                         f"({int(dur // 3600)} h {int(dur % 3600 // 60)} min)" if a else "not started"),
                ("Shifter", ", ".join(sorted(shifters)) or "-"),
                ("Expert", ", ".join(sorted(experts)) or "-"),
                ("HV (set)", hvsets[0] if hvsets else "-"), *[("", x) for x in hvsets[1:]],
                ("Laser", laser or "-"),
                ("Events", " / ".join(f"{int(float(e)):,}" for e in events) or "-"),
                ("Runs", f"{len(rows)}  ({RR.run_span(rows)})" if rows else "0")]
        fig = Figure(figsize=(11.69, 8.27), facecolor="white")
        cls._report_head(fig, run.get("name") or "Routine Summary", head, "Routine summary")
        y = 0.87
        for k, v in info:
            fig.text(0.03, y, k, fontsize=14, fontweight="bold", fontfamily=font)
            fig.text(0.15, y, v, fontsize=14, fontfamily=font)
            y -= 0.04
        y -= 0.015
        fig.text(0.03, y, "Steps", fontsize=14, fontweight="bold", fontfamily=font)
        for blk in blocks:
            if blk["title"][:1].isdigit():
                fig.text(0.15, y, blk["title"], fontsize=12.5, fontfamily=font)
                fig.text(0.42, y, blk.get("info", "")[:110], fontsize=11, color="#555555", fontfamily=font)
                y -= 0.032
        pages = [("summary", fig)]
        if a is None:
            return pages
        loc = lambda t: t.astimezone(JST).replace(tzinfo=None)
        lo, hi = loc(a - timedelta(hours=12)), loc(b + timedelta(minutes=10))
        ra, rb = loc(a), loc(b)
        cond = RR.conditions(lo, hi)
        colors = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b"]
        hv_colors = {"Monitor": CH[0][1], "Rot1": CH[1][1], "Rot2": CH[2][1]}
        panels = [("T", "Temperature", "Temperature (\u00b0C)", "{:.2f}"), ("H", "Humidity", "Humidity (%)", "{:.1f}"),
                  ("B", "B-field |B|", "|B| (mG)", "{:.1f}"), ("I", "Coil current", "Current (A)", "{:.3f}"),
                  ("HV", "HV voltage (HV Monitor)", "VMon (V)", "{:.1f}"),
                  ("HVI", "HV current (HV Monitor)", "IMon (\u00b5A)", "{:.1f}")]
        panels = [p for p in panels if cond[p[0]] or p[0] != "I"]
        fig = Figure(figsize=(11.69, 8.27), facecolor="white")
        cls._report_head(fig, "Conditions", head,
                         f"{lo:%m-%d %H:%M} - {hi:%m-%d %H:%M}  (routine -12 h to +10 min; shaded = routine)     "
                         "legend: mean \u00b1 sd during the routine")
        step_col = {"general_scan": ("#ffe2c2", "General scan"), "hk_scan": ("#d9f2d9", "HK scan"),
                    "stability_qe": ("#cfe8ff", "Stability"), "dark_rate": ("#e9d5ff", "Dark rate"),
                    "wavelength_scan": ("#fdf2b3", "Wavelength scan")}
        spans, groups = [], []   # coloured step bands; labels with consecutive stability steps merged
        for s, a, b in RR.windows(run, rt.KINDS):
            kd = s.get("kind")
            if kd not in step_col:
                continue
            spans.append((loc(a), loc(b), step_col[kd][0]))
            wl = (s.get("name") or "").split()[-1].replace("nm", "") if s.get("name") else ""
            if kd == "stability_qe" and groups and groups[-1][0] == kd:
                groups[-1][2] = loc(b)
            elif kd != "dark_rate":
                groups.append([kd, loc(a), loc(b), "Stability" if kd == "stability_qe" else
                               ("G" if kd == "general_scan" else "HK " if kd == "hk_scan" else "WL ") + wl])
        nrows = -(-len(panels) // 2)
        for k, (g, title, ylab, fmt) in enumerate(panels):
            ax = fig.add_subplot(nrows, 2, k + 1)
            ax.axvspan(ra, rb, color="#e5e7eb", lw=0, zorder=0)
            for a, b, c in spans:
                ax.axvspan(a, b, color=c, lw=0, zorder=0.5)
                ax.axvline(a, color="#9ca3af", lw=0.5, zorder=0.6)
            for _, a, b, lab in groups:
                ax.text(a + (b - a) / 2, 1.01, lab, transform=ax.get_xaxis_transform(), ha="center", va="bottom",
                        fontsize=8, fontfamily=font)
            for c, (name, (ts, vs)) in enumerate(cond[g].items()):
                inside = [v for t, v in zip(ts, vs) if ra <= t <= rb]
                if inside:
                    m = sum(inside) / len(inside)
                    sd = (sum((v - m) ** 2 for v in inside) / len(inside)) ** 0.5
                    lab = f"{name}  {fmt.format(m)} \u00b1 {fmt.format(sd)}"
                else:
                    lab = f"{name}  (no data in routine)"
                ax.plot(ts, vs, lw=1, color=hv_colors.get(name, colors[c % len(colors)]), label=lab)
            ax.set_xlim(lo, hi)
            ax.xaxis.set_major_formatter(DateFormatter("%m-%d\n%H:%M"))
            ax.set_title(title, fontsize=14, loc="left", fontfamily=font, pad=12)
            ax.set_ylabel(ylab, fontsize=12, fontfamily=font)
            ax.grid(True, color="lightgray", ls=":")
            ax.tick_params(direction="in", labelsize=10)
            if cond[g]:
                ax.legend(loc="best", fontsize=9, prop={"family": font, "size": 9.5}, frameon=True)
            else:
                ax.text(0.5, 0.5, "no reading in this period" + (" (HV from the run settings)" if g == "HV"
                                                                  else ""),
                        transform=ax.transAxes, ha="center", va="center", fontsize=12, color="#555555",
                        fontfamily=font)
        fig.tight_layout(rect=(0, 0, 1, 0.9), pad=1.0)
        used = list(dict.fromkeys(c for _, _, c in spans))
        if used:
            from matplotlib.patches import Patch
            fig.legend(handles=[Patch(color=c, label=next(l for cc, l in step_col.values() if cc == c)) for c in used],
                       loc="upper right", bbox_to_anchor=(0.99, 0.915), ncol=len(used), fontsize=9, frameon=False,
                       prop={"family": font, "size": 9.5})
        pages.append(("conditions", fig))
        return pages

    @classmethod
    def _stats_pages(cls, blocks, head, per_page=30):
        """The Statistics page as table pages (same numbers as its CSV export); long notes wrap."""
        import textwrap
        lines = []   # (kind, cells): "top" = step title, "row" = values, "more" = wrapped run / note text
        for blk in blocks:
            lines.append(("top", [blk["title"], blk.get("info", "")]))
            for r in blk["rows"]:
                r = [str(x) for x in r]
                runs = r[4].split(" / ") if r[4] else [""]
                note = textwrap.wrap(r[5], 52) or [""]
                lines.append(("row", r[:4] + [runs[0], note[0]]))
                for k in range(1, max(len(runs), len(note))):
                    lines.append(("more", ["", "", "", "", runs[k] if k < len(runs) else "",
                                           note[k] if k < len(note) else ""]))
        cols = (0.03, 0.31, 0.40, 0.49, 0.58, 0.69)
        colors = {"OK": "#1a7f37", "Check": "#b45309"}
        pages, npages = [], -(-len(lines) // per_page)
        for n, k in enumerate(range(0, len(lines), per_page)):
            fig = Figure(figsize=(11.69, 8.27), facecolor="white")
            cls._report_head(fig, "Statistics", head, f"page {n + 1} / {npages}")
            y = 0.87
            for x, h in zip(cols, StatisticsPage.COLS):
                fig.text(x, y, h, fontsize=12, fontweight="bold", fontfamily=_ReportCard.FONT)
            fig.add_artist(Line2D([0.02, 0.98], [y - 0.012] * 2, color="black", lw=0.8))
            warn = False
            for kind, row in lines[k:k + per_page]:
                y -= 0.027
                if kind == "top":
                    y -= 0.006
                    fig.text(cols[0], y, row[0], fontsize=12, fontweight="bold", fontfamily=_ReportCard.FONT)
                    fig.text(cols[1], y, row[1], fontsize=10, color="#555555", fontfamily=_ReportCard.FONT)
                    continue
                if kind == "row":
                    warn = row[5].startswith("Check")
                for c, (x, v) in enumerate(zip(cols, row)):
                    col = colors.get(v, colors["Check"] if c == 5 and warn else "black")
                    fig.text(x + (0.01 if c == 0 else 0), y, v, fontsize=10.5, color=col, fontfamily=_ReportCard.FONT)
            pages.append((f"stats_{n + 1}", fig))
        return pages

    @staticmethod
    def _raw_angle_axis(ax, g):
        """Top ticks of a Hamamatsu-angle panel: the raw stage angle of the points (10 deg steps)."""
        at = {}
        for (metric, lab, ch, axis, xmode), ids in g["ids"].items():
            ham = g["series"].get((metric, lab, ch, axis, "hamamatsu"))
            if metric != "qe" or xmode != "raw" or not ham or not ids:
                continue
            by_id = dict(zip(g["ids"].get((metric, lab, ch, axis, "hamamatsu")) or [], ham[0]))
            for raw, i in zip(g["series"][(metric, lab, ch, axis, "raw")][0], ids):
                if i in by_id and raw * by_id[i] >= 0 and abs(raw / 10 - round(raw / 10)) < 1e-6:
                    at[raw] = by_id[i]   # the mirrored axis (opposite sign) is left out
        if not at:
            return
        ax.tick_params(which="both", top=False)
        top = ax.twiny()
        top.set_xlim(ax.get_xlim())
        top.set_xticks(list(at.values()))
        top.set_xticklabels([f"{r:g}".replace("-", "\u2212") for r in at], fontfamily=_ReportCard.FONT, fontsize=12)
        top.tick_params(direction="in", length=8)
        ax.set_title(ax.get_title(loc="left"), fontsize=16, loc="left", fontfamily=_ReportCard.FONT, pad=24)
        ax.set_xlabel("Hamamatsu angle (deg)   (top: raw stage)", fontsize=15,
                      fontfamily=_ReportCard.FONT, color="black")

    def export_report(self):
        """The full report (every card, option, General Scan step and the Statistics), as one PDF or one PNG per page."""
        if self._res is None:
            return
        path = self._export_path(".pdf", "PDF (*.pdf);;PNG, one per page (*.png)")
        if not path:
            return
        dlg, busy, self._busy = _SavingDialog(self, "Saving report ..."), self._busy, True
        try:
            pages = self._report_pages(dlg.step)
            if path.lower().endswith(".png"):
                for k, (key, fig) in enumerate(pages):
                    dlg.step(f"Writing PNG {k + 1} / {len(pages)}", k, len(pages))
                    fig.savefig(f"{path[:-4]}_{key}.png", dpi=200)
                    fig.clear()
                shown = f"{path[:-4]}_{pages[0][0]}.png" if pages else path
            else:
                from matplotlib.backends.backend_pdf import PdfPages
                path = path if path.lower().endswith(".pdf") else path + ".pdf"
                with PdfPages(path, metadata=self._pdf_meta()) as pdf:
                    for k, (_key, fig) in enumerate(pages):
                        dlg.step(f"Writing PDF page {k + 1} / {len(pages)}", k, len(pages))
                        pdf.savefig(fig)
                        fig.clear()
                shown = path
            dlg.step("Saved", len(pages), max(len(pages), 1))
            self.state.setText(f"Exported: {path}  ({len(pages)} pages)")
            if shown == path:
                self._to_drive(path)
            if os.path.exists(shown):
                _show_in_folder(shown)
        except Exception as e:
            self.state.setText(f"Export failed: {e}")
        finally:
            self._busy = busy
            dlg.close()
            pages = None
            _trim_memory()

    def _auto_tick(self):
        """When the routine ends: show its run and save the report without asking."""
        active = bool(rt.progress().get("active"))
        if self._was_active and not active:
            self._auto = "pending"
        self._was_active = active
        if self._auto == "pending" and not self._busy:
            runs = rt.routine_runs()
            self._auto = runs[0]["label"] if runs else None
            if self._auto:
                self.on_show(prefer=self._auto)

    def save_report(self):
        """The report as one PDF in REPORT_DIR (the remote page links the newest)."""
        os.makedirs(self.REPORT_DIR, exist_ok=True)
        base = os.path.join(self.REPORT_DIR, self._export_name())
        dlg, busy, self._busy = _SavingDialog(self, "Saving the routine report ..."), self._busy, True
        try:
            from matplotlib.backends.backend_pdf import PdfPages
            pages = self._report_pages(dlg.step)
            with PdfPages(base + ".pdf", metadata=self._pdf_meta()) as pdf:
                for k, (_key, fig) in enumerate(pages):
                    dlg.step(f"Writing page {k + 1} / {len(pages)}", k, len(pages))
                    pdf.savefig(fig)
                    fig.clear()
            self.state.setText(f"Report saved: {base}.pdf  ({len(pages)} pages)")
            self._to_drive(base + ".pdf")
        except Exception as e:
            self.state.setText(f"Report failed: {e}")
        finally:
            self._busy = busy
            dlg.close()
            pages = None
            _trim_memory()

    def _drive_toggled(self, on):
        try:
            drive.set_enabled(on)
        except OSError as e:
            self.state.setText(f"Google Drive setting not saved: {e}")
            return
        if on:
            ok, why = drive.ready()
            if not ok:
                self.state.setText(f"Google Drive: {why}")

    def _to_drive(self, path):
        """Copy a saved report PDF to the Drive folder in the background when the box is ticked."""
        if not self.drive_chk.isChecked():
            return
        threading.Thread(target=lambda: self.bus.uploaded.emit(*drive.upload(path)), daemon=True,
                         name="report-drive").start()

    def _uploaded(self, ok, msg):
        text = f"Google Drive: uploaded {msg}" if ok else f"Google Drive upload failed: {msg} (kept on this PC)"
        self.state.setText(f"{self.state.text()}   |   {text}" if self.state.text() else text)

    def load(self):
        i = self.runs.currentIndex()
        self._scan_files = []
        self._sync_open()
        if i < 0 or i >= len(self._runs):
            for card in self.cards.values():
                card.clear("No routine run")
                card.done()
            return
        if self._busy:
            return
        self._busy = True
        self._seq += 1
        seq, run = self._seq, self._runs[i]
        self.state.setText("")
        if self._shown != run["label"]:
            self.spin.start()
            # another run: drop the shown one so no card keeps its data under the new run's name
            self._res, self._gen, self._shown = None, None, None
            self.pmts.setText("")
            for card in self.cards.values():
                card.clear("Loading...")
                card.done()

        def work():
            try:
                res = RR.load(run)
            except Exception as e:
                res = e
            self.bus.loaded.emit(seq, res)
        threading.Thread(target=work, daemon=True, name="routine-results").start()

    def _loaded(self, seq, res):
        self._busy = False
        self.spin.stop()
        if seq != self._seq:
            return
        if isinstance(res, Exception):
            self.state.setText(f"Load failed: {res}")
            return
        self.state.setText("")
        shown = self._runs[self.runs.currentIndex()]["label"] if self.runs.currentIndex() >= 0 else None
        if shown != self._shown:
            for card in self.cards.values():
                card.forget_run()
        self._shown = shown
        self._res = res
        self.pmts.setText(res.get("pmts", ""))
        self._scan_files = res.get("scan_files", [])
        self._sync_open()
        self._draw_panels(res)
        self._draw_general(res["general"])
        if self._auto == shown:
            self._auto = None
            self.save_report()

    def _draw_panels(self, res):
        self._draw_dark(res["dark"])
        self._draw_hv(self.cards["hv_scan"], res["hv_scan"], scan=True)
        self._draw_stability(res["stability"])

    # ---------------------------------------------------------------- panels
    @staticmethod
    def _time_axis(ax):
        ax.xaxis.set_major_formatter(DateFormatter("%H:%M", tz=JST))

    def _draw_dark(self, d):
        card = self.cards["dark"]
        if not d["steps"]:
            card.clear("Not run")
            return card.done()
        thr = RR.thr_text(d.get("thr"))
        foot = f"Runs {RR.run_span(d['rows'])}   Self trigger" + (f" {thr}" if thr else "") if d["rows"] else "No dark runs"
        card.clear(foot)
        ax = card.axes[0]
        metric = self.sel["dark"]
        clean = metric == "clean"
        for ch, (name, color, mk) in CH.items():
            p = [x for x in d["points"] if x[1] == ch and (not clean or x[4])]
            if p:
                pairs = [x[4] if clean else x[5] if metric == "rec" else x[2:4] for x in p]
                ys = [v[0] for v in pairs]
                es = [v[1] for v in pairs]
                bar = ax.errorbar([x[0] for x in p], ys, yerr=es, fmt=mk + ":", color=color, ms=4, lw=1, capsize=2,
                                  label=name)
                card.pickable(bar, [x[7] for x in p], ch)
        ax.set_ylabel("Pulse rate (kHz)" if clean else "Dark rate (kHz)", fontsize=8)
        self._time_axis(ax)
        if d["points"]:
            card.legend(ax)
        card.done()

    @staticmethod
    def _draw_fit(ax, fit, series, step_m):
        """Fit curves G = a (V/1 kV)^b, lines at 1.0 x 10^7 and the target gain, the HV reaching each."""
        fit_m = fit["mantissa"] or step_m
        gains = [(1.0, "1.0 x 10^7")] + ([(fit_m, f"target {fit_m:g} x 10^7")] if abs(fit_m - 1.0) > 1e-9 else [])
        hvs = [x for (m, _c), sr in series.items() if m == "gain" for x in sr[0]]
        marks = []
        for ch, c in fit["chans"].items():
            if not c.get("fitted"):
                continue
            for m, _lab in gains:
                v, _e, extrap = rt.gain_target(c, m, fit_m)
                if v:
                    marks.append((ch, m, v, rt._flags(c, extrap)))
                    hvs.append(v)
        if not hvs:
            return
        lo, hi = min(hvs) - 20, max(hvs) + 20
        xs = [lo + (hi - lo) * i / 100 for i in range(101)]
        for ch, c in fit["chans"].items():
            a, b = rt._num(c.get("a_1kV")), rt._num(c.get("b"))
            if ch in CH and c.get("fitted") and a and b:
                ax.plot(xs, [a * (x / 1000.0) ** b for x in xs], ls="--", lw=1, color=CH[ch][1], alpha=0.8)
        for m, lab in gains:
            ax.axhline(m * RR.PC_PER_1E7, color="#555555", lw=0.8, ls=":")
            ax.annotate(lab, (lo, m * RR.PC_PER_1E7), fontsize=6, color="#555555", va="bottom", ha="left")
        for ch, m, v, fl in marks:
            ax.vlines(v, 0, m * RR.PC_PER_1E7, colors=CH[ch][1], ls=":", lw=1)
            ax.annotate(f"{CH[ch][0]} {v:.0f} V" + (f" ({', '.join(fl)})" if fl else ""), (v, 0),
                        xytext=(-2, 3), textcoords="offset points", fontsize=6, color=CH[ch][1], rotation=90,
                        va="bottom", ha="right", clip_on=True)
        ax.set_ylim(bottom=0)

    def _draw_hv(self, card, d, scan):
        if not d["steps"]:
            card.clear("Not run")
            return card.done()
        step = d["steps"][-1][0]
        o = step.get("options") or {}
        if scan:
            offs = " / ".join(f"{float(v):+g}" if float(v) else "0" for v in o.get("offsets_v") or [])
            foot = f"{offs} V   settle {float(o.get('settle_s', 180)) / 60:g} min"
            if d["rows"]:
                foot += f"   Runs {RR.run_span(d['rows'])}"
        else:
            foot = str(step.get("note") or step.get("status") or "")
        card.clear(foot)
        card.foot.setToolTip(str(step.get("note") or ""))
        ax = card.axes[0]
        metric = self.sel["hv_scan" if scan else "hv_apply"]
        if metric != "vmon":
            series = d.get("curve" if scan else "check") or {}
            for ch, (name, color, mk) in CH.items():
                sr = series.get((metric, ch))
                if sr:
                    order = sorted(range(len(sr[0])), key=lambda k: sr[0][k])
                    bar = ax.errorbar([sr[0][k] for k in order], [sr[1][k] for k in order],
                                      yerr=[sr[2][k] for k in order], fmt=mk + "-", color=color, ms=4, lw=1,
                                      capsize=2, label=name)
                    card.pickable(bar, [sr[3][k] for k in order], ch)
            ax.set_ylabel(RR.METRICS[metric], fontsize=8)
            if scan and metric == "gain" and d.get("fit"):
                self._draw_fit(ax, d["fit"], series, float(o.get("gain_mantissa", 1.0)))
            if scan:
                ax.set_xlabel("HV (V)", fontsize=8)
            else:
                self._time_axis(ax)
                if d.get("check_rows"):
                    card.foot.setText(f"{foot}   Check runs {RR.run_span(d['check_rows'])}")
            if ax.has_data():
                card.legend(ax)
            return card.done()
        for ch, (name, color, _mk) in CH.items():
            ys = d["vmon"].get(ch) or []
            if ys and d["times"]:
                ax.plot(d["times"], ys, color=color, lw=1.3, label=f"{name} VMon")
        for _s, a, b in d["steps"]:
            ax.axvspan(a, b, color="#e5e7eb", alpha=0.5, lw=0, zorder=0)
        ax.set_ylabel("VMon (V)", fontsize=8)
        self._time_axis(ax)
        if d["times"]:
            card.legend(ax)
        card.done()

    def _draw_stability(self, d):
        card = self.cards["stability"]
        if not d["steps"]:
            card.clear("Not run")
            return card.done()
        wls = sorted({r.get("wavelength") for r in d["rows"] if r.get("wavelength")})
        card.clear(f"Runs {RR.run_span(d['rows'])}   " + " / ".join(f"{w} nm" for w in wls) if d["rows"]
                   else "No Stability runs")
        if d["rows"]:
            card.foot_runs(RR.run_span(d["rows"]), _wl_groups(d["rows"]))
        ax = card.axes[0]
        metric = self.sel["stability"]
        for ch, (name, color, mk) in CH.items():
            sr = d["series"].get((metric, ch))
            if sr:
                bar = ax.errorbar(sr[0], sr[1], yerr=sr[2], fmt=mk + "-", color=color, ms=4, lw=1, capsize=2, label=name)
                card.pickable(bar, sr[3], ch)
        ax.set_ylabel(RR.LABELS[metric], fontsize=8)
        self._time_axis(ax)
        if ax.has_data():
            card.legend(ax)
        card.done()

    def _step_menu(self, steps):
        """Step selector of the General Scan card: All plus one entry per step, shown for two or more steps."""
        items = ["All"] + steps
        if [self.gstep.itemText(i) for i in range(self.gstep.count())] != items:
            self.gstep.blockSignals(True)
            self.gstep.clear()
            self.gstep.addItems(items)
            if self.gsel["step"] not in items:
                self.gsel["step"] = "All"
            self.gstep.setCurrentText(self.gsel["step"])
            self.gstep.blockSignals(False)
        self.gstep_box.setVisible(len(steps) > 1)

    def _wl_menu(self, wls):
        """Wavelength selector of the General Scan card: All plus one entry per wavelength, shown for two or more."""
        items = ["All"] + [f"{w} nm" for w in wls]
        if [self.gwl.itemText(i) for i in range(self.gwl.count())] != items:
            self.gwl.blockSignals(True)
            self.gwl.clear()
            self.gwl.addItems(items)
            if self.gsel["wl"] not in items:
                self.gsel["wl"] = "All"
            self.gwl.setCurrentText(self.gsel["wl"])
            self.gwl.blockSignals(False)
        self.gwl_box.setVisible(len(wls) > 1)

    @staticmethod
    def _min_span(ax, ys):
        """Keep at least +-MIN_SPAN of the level on the y axis so flat data is not over-zoomed."""
        ys = [y for y in ys if y == y]
        if not ys:
            return
        lo, hi = ax.get_ylim()
        mid, half = (min(ys) + max(ys)) / 2, MIN_SPAN * abs(sorted(ys)[len(ys) // 2])
        if half > 0 and hi - lo < 2 * half:
            ax.set_ylim(mid - half, mid + half)

    @staticmethod
    def _ham_angles(g, key, x, ids, xmode):
        """Hamamatsu angle of each point of a series (its x, or looked up by run in the Hamamatsu series)."""
        if xmode == "hamamatsu":
            return list(x)
        hx = g["series"].get(key + ("hamamatsu",))
        hid = (g.get("ids") or {}).get(key + ("hamamatsu",))
        at = dict(zip(hid, hx[0])) if hx and hid else {}
        return [at.get(i) if i else None for i in (ids or [None] * len(x))]

    @staticmethod
    def _n70_mean(y, e, ham):
        """Plain mean of the points with |Hamamatsu angle| <= N70_DEG (value > 0, relative error <= N70_MAX_REL),
        0 when no point qualifies."""
        use = [v for v, er, h in zip(y, e, ham)
               if h is not None and abs(h) <= N70_DEG and v > 0 and (er or 0) <= N70_MAX_REL * v]
        return sum(use) / len(use) if use else 0

    @staticmethod
    def _join_entry(ax, bar, extra):
        """Put extra artists under the legend entry of an errorbar so they hide and show with it."""
        lab = bar.get_label()
        if not lab or lab.startswith("_") or bar not in ax.containers:
            return
        both = Container(tuple(bar) + (extra,), label=lab)
        bar.set_label("_nolegend_")
        ax.containers[ax.containers.index(bar)] = both

    def _draw_dark_time(self, g, lab):
        """LD-on dark rate of one General Scan session against the run time (X then Y scan, in time order)."""
        card = self.cards["general"]
        card.time_axis = True
        ax = card.axes[0]
        when = {}
        for r in g["rows"]:
            t = r.get("_time")
            if t is not None and str(r.get("run", "")).isdigit():
                when[(str(r["date"]), int(r["run"]))] = t.astimezone(JST).replace(tzinfo=None) if t.tzinfo else t
        spans = {}
        for ch in (0, 1, 2):
            name, color, mk = CH[ch]
            for axis in ("X", "Y"):
                key = (RR.DARK_ON, lab, ch, axis, "raw")
                sr, ids = g["series"].get(key), (g.get("ids") or {}).get(key) or []
                if not sr:
                    continue
                pts = sorted((when[(str(i[0]), int(i[1]))], y, e) for i, y, e in zip(ids, sr[1], sr[2])
                             if i and str(i[1]).isdigit() and (str(i[0]), int(i[1])) in when)
                if not pts:
                    continue
                ts = [q[0] for q in pts]
                ax.errorbar(ts, [q[1] for q in pts], yerr=[q[2] for q in pts], fmt=mk, ls=AXIS_LS[axis], color=color,
                            mfc=color if axis == "X" else "white", ms=3.5, lw=1, capsize=2, label=f"{name} {axis}")
                lo, hi = spans.get(axis, (ts[0], ts[-1]))
                spans[axis] = (min(lo, ts[0]), max(hi, ts[-1]))
        for axis, (lo, hi) in spans.items():
            ax.axvspan(lo, hi, color="#f3f4f6" if axis == "X" else "#fdf6e3", lw=0, zorder=0)
            ax.text(lo + (hi - lo) / 2, 0.97, f"{axis}-axis scan", transform=ax.get_xaxis_transform(), ha="center",
                    va="top", fontsize=8)
        ax.xaxis.set_major_formatter(DateFormatter("%H:%M"))
        ax.set_ylabel(RR.LABELS[RR.DARK_ON], fontsize=8)
        ax.set_xlabel("Time (JST)", fontsize=8)
        card.done()

    def _draw_general(self, g):
        card = self.cards["general"]
        self._gen = g
        steps = list(dict.fromkeys(g.get("steps") or []))
        self._step_menu(steps)
        hk = g.get("hk") or {}
        if not g["rows"] and not hk.get("total"):
            card.clear("No General Scan runs")
            return card.done()
        foot, tail = [], ""
        if g["rows"]:
            ev = g["rows"][0].get("events", "")
            laser = RR.laser_text(g["rows"])
            wls = sorted({r.get("wavelength") for r in g["rows"] if r.get("wavelength")})
            tail = (f"   {int(float(ev)):,} ev" if ev else "") + (f"   Laser {laser}" if laser else "")
            foot.append(f"Runs {RR.run_span(g['rows'])}   " + " / ".join(f"{w} nm" for w in wls) + tail)
        if hk.get("total"):
            runs = hk.get("runs") or []
            foot.append((f"HK runs {min(runs):03d}-{max(runs):03d}" if runs else "HK")
                        + f"   {len(runs)} of {hk['total']} points read"
                        + ("" if len(runs) == hk["total"] else "   (points need hit and event trees)"))
        card.clear("      ".join(foot))
        if g["rows"]:
            card.foot_runs(RR.run_span(g["rows"]), _wl_groups(g["rows"]), tail + "".join("      " + f for f in foot[1:]))
        ax = card.axes[0]
        metric, xmode = self.gsel["metric"], self.gsel["xmode"]
        self.gnorm.setEnabled(metric == "qe")
        self.gn70.setEnabled(metric in N70_METRICS)
        n70 = self.gsel["n70"] and metric in N70_METRICS
        key = "qe_mon" if metric == "qe" and self.gsel["norm"] else metric   # gain is never normalized
        axes = ("X", "Y") if self.gsel["axis"] == "both" else (self.gsel["axis"],)
        pick = self.gsel["step"]
        sess = list(zip(g["sessions"], g.get("steps") or g["sessions"], g.get("wls") or [""] * len(g["sessions"])))
        by_step = [(si, lab, wl) for si, (lab, st, wl) in enumerate(sess) if pick == "All" or st == pick]
        self._wl_menu(sorted({wl for _si, _lab, wl in by_step if wl}, key=lambda w: (len(w), w)))
        wpick = self.gsel["wl"]
        shown = [(si, lab) for si, lab, wl in by_step if wpick == "All" or f"{wl} nm" == wpick]
        kinds = g.get("kinds") or []
        hk_shown = {si < len(kinds) and kinds[si] == "hk_scan" for si, _lab in shown}
        backend = "caen" if hk_shown <= {False} else "hk" if hk_shown == {True} else "mixed"
        for b in self.gbuttons["metric"].buttons():   # the buttons carry the names of the digitizer shown
            b.setText(HK_METRIC_NAMES[b.property("val")] if backend == "hk" else METRIC_NAMES[b.property("val")])
        multi = len(shown) > 1
        ys_all, n_fail = [], 0
        for si, lab in shown:
            short = lab[len(pick):].strip() if pick != "All" and lab.startswith(pick) and lab != pick else lab
            for ch in ((1, 2) if key == "qe_mon" else (0, 1, 2)):   # the Monitor is flat when normalized
                name, color, mk = CH[ch]
                labelled = False
                for axis in axes:
                    sr = g["series"].get((key, lab, ch, axis, xmode))
                    fx, fy, fids = (g.get("fail") or {}).get((key, lab, ch, axis, xmode)) or ([], [], [])
                    if not sr and not fx:
                        continue
                    x, y, e = sr or ([], [], [])
                    ids = (g.get("ids") or {}).get((key, lab, ch, axis, xmode))
                    if n70:
                        m70 = self._n70_mean(y, e, self._ham_angles(g, (key, lab, ch, axis), x, ids, xmode))
                        if m70 > 0:
                            y, e, fy = [v / m70 for v in y], [er / m70 for er in e], [v / m70 for v in fy]
                    ys_all += y
                    if multi:
                        # both axes: one entry per PMT and step, the line style says X or Y
                        text = (None if labelled else f"{name}  {short}") if len(axes) == 2 else f"{name} {axis}  {short}"
                        labelled = True
                        bar = ax.errorbar(x, y, yerr=e, fmt=OPEN_MK[si % len(OPEN_MK)], mfc="none", mew=1,
                                          ls=AXIS_LS[axis], color=STEP_SHADE[ch][si % len(STEP_SHADE[ch])], ms=4, lw=1,
                                          capsize=2, label=text)
                    else:
                        bar = ax.errorbar(x, y, yerr=e, fmt=mk, ls=AXIS_LS[axis], color=color, ms=3.5, lw=1,
                                          capsize=2, label=f"{name} {axis}")
                    card.pickable(bar, ids, ch)
                    if fx:
                        bad = ax.plot(fx, fy, marker=bar[0].get_marker(), ms=bar[0].get_markersize(), mfc="none",
                                      mew=1, ls="none", color=bar[0].get_color(), alpha=FAIL_ALPHA)
                        card.pickable(bad, fids, ch)
                        ys_all += fy
                        n_fail += len(fx)
                        self._join_entry(ax, bar, bad[0])
        caen_label = "QE / Monitor" if key == "qe_mon" else RR.LABELS[metric]
        hk_label = "Hits per trigger / Monitor" if key == "qe_mon" else HK.METRICS.get(metric, "-")
        if n70:
            caen_label, hk_label = (f"{caen_label} / mean(|angle| \u2264 70\u00b0)", f"{hk_label} / mean(|angle| \u2264 70\u00b0)")
        ax.set_ylabel({"caen": caen_label, "hk": hk_label}.get(backend, f"{caen_label}  |  HK: {hk_label}"), fontsize=8)
        ax.set_xlabel("Hamamatsu angle (deg)" if xmode == "hamamatsu" else "Tilt angle (deg, raw stage)", fontsize=8)
        if ax.has_data():
            self._min_span(ax, ys_all)
            handles = ax.get_legend_handles_labels()[0]
            if multi and len(axes) == 2:
                handles += [Line2D([], [], color="#666666", ls=AXIS_LS[a], lw=1, label=f"{a} axis") for a in axes]
            if n_fail:
                handles.append(fail_handle())
            card.legend(ax, handles)
        card.done()


class _StatsBus(QObject):
    loaded = pyqtSignal(int, object)


class StatisticsPage(_AutoReload, QWidget):
    """Numbers per step of one routine run, per channel (Monitor / Rot1 / Rot2); Export writes them as CSV."""
    COLS = ("Step / Item", "Monitor", "Rot1", "Rot2", "Run", "Note")
    EXPORT_DIR = os.path.join(env.ADC_DIR, "Data", "RoutineStats")

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        top = QHBoxLayout()
        top.addWidget(QLabel("Routine Run"))
        self.runs = QComboBox()
        self.runs.setMinimumWidth(320)
        self.runs.currentIndexChanged.connect(lambda _i: self.load())
        top.addWidget(self.runs)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(lambda: self.on_show())
        top.addWidget(self.refresh_btn)
        self.copy_btn = QPushButton("Copy")
        self.copy_btn.clicked.connect(self.copy)
        top.addWidget(self.copy_btn)
        self.export_btn = QPushButton("Export...")
        self.export_btn.clicked.connect(self.export)
        top.addWidget(self.export_btn)
        self.state = QLabel("")
        self.state.setStyleSheet(f"color: {theme.C['muted']};")
        self.state.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)   # a long path clips instead of widening the window
        top.addWidget(self.state, 1)
        top.addStretch(1)
        lay.addLayout(top)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(list(self.COLS))
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        hd = self.tree.header()
        hd.setStretchLastSection(True)
        for c in range(len(self.COLS) - 1):
            hd.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        lay.addWidget(self.tree, 1)
        self._runs, self._seq, self._busy, self.blocks = [], 0, False, []
        self.spin = _Spinner(self)
        self._shown = None   # label of the run on screen: its reloads keep it shown, no spinner
        self.bus = _StatsBus()
        self.bus.loaded.connect(self._loaded)
        self._watch()

    def on_show(self, prefer=None):
        cur = prefer or self.runs.currentText()
        self._runs = rt.routine_runs()
        self.runs.blockSignals(True)
        self.runs.clear()
        for r in self._runs:
            self.runs.addItem(r["label"])
        i = self.runs.findText(cur)
        self.runs.setCurrentIndex(i if i >= 0 else 0)
        self.runs.blockSignals(False)
        self.load()

    def _run(self):
        i = self.runs.currentIndex()
        return self._runs[i] if 0 <= i < len(self._runs) else None

    def load(self):
        run = self._run()
        if run is None:
            self.tree.clear()
            self.blocks = []
            self.state.setText("No routine run")
            return
        if self._busy:
            return
        self._busy = True
        self._seq += 1
        seq = self._seq
        self.state.setText("")
        if self._shown != run["label"]:
            self.spin.start()

        def work():
            try:
                res = RS.stats(run)
            except Exception as e:
                res = e
            self.bus.loaded.emit(seq, res)
        threading.Thread(target=work, daemon=True, name="routine-stats").start()

    def _loaded(self, seq, res):
        self._busy = False
        self.spin.stop()
        if seq != self._seq:
            return
        if isinstance(res, Exception):
            self.state.setText(f"Load failed: {res}")
            return
        self.state.setText("")
        run = self._run()
        self._shown = run["label"] if run else None
        self.blocks = res
        self.tree.clear()
        bold = QFont()
        bold.setBold(True)
        for blk in res:
            top = QTreeWidgetItem([blk["title"], "", "", "", "", blk["info"]])
            top.setFont(0, bold)
            for row in blk["rows"]:
                it = QTreeWidgetItem([str(x) for x in row])
                for c in (1, 2, 3):
                    if row[c] == "OK":
                        it.setForeground(c, QColor(theme.C["good"]))
                    elif row[c] == "Check":
                        it.setForeground(c, QColor(theme.C["warn"]))
                if str(row[5]).startswith("Check"):
                    it.setForeground(5, QColor(theme.C["warn"]))
                top.addChild(it)
            self.tree.addTopLevelItem(top)
            top.setExpanded(True)

    def text(self):
        lines = ["\t".join(self.COLS)]
        for blk in self.blocks:
            lines.append("\t".join([blk["title"], "", "", "", "", blk["info"]]))
            lines += ["\t".join(str(x) for x in r) for r in blk["rows"]]
        return "\n".join(lines)

    def copy(self):
        QApplication.clipboard().setText(self.text())

    def default_path(self):
        run = self._run() or {}
        t = rt._parse(run.get("started_at"))
        stamp = f"{t:%Y%m%d_%H%M}" if t else "run"
        name = "".join(c if c.isalnum() or c in "-_" else "_" for c in run.get("name", "routine"))
        return os.path.join(self.EXPORT_DIR, f"routine_stats_{stamp}_{name}.csv")

    def export(self):
        if not self.blocks:
            return
        os.makedirs(self.EXPORT_DIR, exist_ok=True)
        path, _f = QFileDialog.getSaveFileName(self, "Export statistics", self.default_path(), "CSV (*.csv)")
        if not path:
            return
        try:
            RS.to_csv(path, self._run() or {}, self.blocks)
            self.state.setText(f"Exported: {path}")
            _show_in_folder(path)
        except OSError as e:
            self.state.setText(f"Export failed: {e}")

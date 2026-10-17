"""DAQ System left panel, mirroring ui_manager's left pane top to bottom:
PMT position, Run Mode & Parameters, Execute Scripts, View, File & Directory
Paths, then Storage (v2: moved here from the Tk PMT Setup tab).

Read-only. PMT angles are the Tk app's live motor readings (logs/live_angles.json),
falling back to config3.h (last recorded run) when those are not fresh. device_rows() feeds the Overview lamp tooltips.
"""
import json
import math
import os
import shutil
import subprocess
import threading

from PyQt5.QtCore import QObject, QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPen, QPolygonF
from PyQt5.QtWidgets import (QButtonGroup, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QProgressBar,
                             QPushButton, QRadioButton, QScrollArea, QVBoxLayout, QWidget)

from app import theme
from core import env, status
from core.access import ACCESS
from pages.daq import script_actions
from core.env import read_config

PATH_KEYS = ['BasePath', 'RawDataPath', 'ExternalPath', 'ExternalPath2']
DISKS = [("Local", "RawDataPath"), ("Ext HDD1", "ExternalPath"), ("Ext HDD2", "ExternalPath2")]   # as the Tk app


def _gb(n):
    return f"{n / 1e12:.2f} TB" if n >= 1e12 else f"{n / 1e9:.1f} GB"


class _UsageBus(QObject):
    done = pyqtSignal(str, object)   # disk name, shutil usage or error text


class DiskUsage(QWidget):
    """'<name>: 73.5% used' / '180.0 GB / 245.1 GB' / bar."""

    def __init__(self, name):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(2)
        self.title = QLabel(f"{name}: --")
        self.title.setStyleSheet("font-weight: 600;")
        self.size = QLabel("")
        self.size.setObjectName("key")
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        for w in (self.title, self.size, self.bar):
            lay.addWidget(w)
        self.name = name

    def show_usage(self, u):
        if isinstance(u, str):
            self.title.setText(f"{self.name}: {u}")
            self.size.hide()
            self.bar.hide()
            return
        self.size.show()
        self.bar.show()
        pct = 100.0 * u.used / u.total if u.total else 0.0
        self.title.setText(f"{self.name}: {pct:.1f}% used")
        self.size.setText(f"{_gb(u.used)} / {_gb(u.total)}   ({_gb(u.free)} free)")
        self.bar.setValue(int(pct * 10))
        color = "#dc3545" if pct >= 90 else ("#f0ad4e" if pct >= 80 else "#0b63b6")
        self.bar.setStyleSheet(f"QProgressBar {{ background: {theme.C['line']}; border: none; border-radius: 4px; }}"
                               f"QProgressBar::chunk {{ background: {color}; border-radius: 4px; }}")
NOT_IN_PREVIEW = "Not available in v2 yet. Use the Tk app."


class PositionView(QWidget):
    """Mini TOP compass + SIDE tilt view, same drawing as ui_manager._draw_pos_*."""

    def __init__(self):
        super().__init__()
        self.setFixedSize(110, 52)
        self.rot, self.tilt = 0.0, 0.0

    def set_angles(self, rot, tilt, needle="#1D9E75"):
        self.rot, self.tilt, self.needle = rot, tilt, needle
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(theme.C["card"]))
        # TOP view: cable needle, physical cable at 180 deg + rotation
        cx, cy, R = 26, 26, 21
        p.setPen(QPen(QColor(theme.C["hint"]), 1))
        p.drawEllipse(QPointF(cx, cy), R, R)
        ang = math.radians(180 + self.rot)
        p.setPen(QPen(QColor(getattr(self, "needle", "#1D9E75")), 3, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(cx, cy), QPointF(cx + R * math.cos(ang), cy - R * math.sin(ang)))
        # SIDE view: dome tilted vs the vertical laser axis
        ox, cy2, Rb = 50 + 30, 36, 22
        kr = abs(self.tilt)
        pos = max(0.0, min(90.0, -0.0049 * kr * kr + 1.7515 * kr - 0.0402))
        th = math.radians(-(-1.0 if self.tilt < 0 else 1.0) * pos)
        upx, upy, rxh, ryh = math.sin(th), -math.cos(th), math.cos(th), math.sin(th)
        p.setPen(QPen(QColor("#bbbbbb"), 1, Qt.DashLine))
        p.drawLine(QPointF(ox, cy2), QPointF(ox, cy2 - Rb - 8))
        poly = QPolygonF()
        for a in range(0, 181, 10):
            lx, ly = Rb * math.cos(math.radians(a)), Rb * math.sin(math.radians(a))
            poly.append(QPointF(ox + lx * rxh + ly * upx, cy2 + lx * ryh + ly * upy))
        p.setPen(QPen(QColor("#5a7fa5"), 1))
        p.setBrush(QColor("#d0e7fb"))
        p.drawPolygon(poly)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#e24b4a"))
        p.drawEllipse(QRectF(ox - 2, cy2 - Rb + 2, 4, 4))
        p.end()


class LeftPanel(QScrollArea):
    def __init__(self):
        super().__init__()
        self.setObjectName("sidebar")
        inner = QWidget()
        inner.setObjectName("sidebarBody")
        self.lay = QVBoxLayout(inner)
        self.lay.setContentsMargins(8, 4, 8, 8)
        self.lay.setSpacing(4)
        self.cfg = read_config()
        self.guarded = []
        self.actions = script_actions.Actions(self, self._show_output, self._show_page)
        self._build_position()
        self._build_run_mode()
        self._build_buttons("Execute Scripts", "scripts")
        self._build_buttons("View", "view")
        self._build_paths()
        self._build_storage()
        self.lay.addStretch(1)
        ACCESS.listeners.append(self._paint_access)
        self._paint_access()
        self.setWidget(inner)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setMinimumWidth(330)
        self.refresh()

    # ---- PMT position ------------------------------------------------------
    def _build_position(self):
        box = self.pos_box = QGroupBox("PMT position (live)")
        v = QVBoxLayout(box)
        row = QHBoxLayout()
        self.pos = {}
        for dev in (2, 3):
            col = QVBoxLayout()
            col.addWidget(self._center(QLabel(f"SN{dev}"), bold=True))
            view = PositionView()
            col.addWidget(view, 0, Qt.AlignHCenter)
            ang = self._center(QLabel())
            ang.setStyleSheet("font-size: 9.5pt; font-weight: normal;")
            col.addWidget(ang)
            st = self._center(QLabel())
            col.addWidget(st)
            row.addLayout(col)
            self.pos[dev] = (view, ang, st)
        v.addLayout(row)
        self.lay.addWidget(box)
        self._pos_timer = QTimer(self)
        self._pos_timer.timeout.connect(self._refresh_position)
        self._pos_timer.start(1000)   # the Tk app reads the motors every 0.5-1 s

    # ---- Storage -------------------------------------------------------------
    def _build_storage(self):
        box = QGroupBox("Storage")
        v = QVBoxLayout(box)
        self.disks = {}
        for name, _key in DISKS:
            d = DiskUsage(name)
            v.addWidget(d)
            self.disks[name] = d
        self.lay.addWidget(box)
        self._usage_bus = _UsageBus()
        self._usage_bus.done.connect(lambda name, u: self.disks[name].show_usage(u))
        self._usage_timer = QTimer(self)
        self._usage_timer.timeout.connect(self._refresh_storage)
        self._usage_timer.start(60_000)
        self._refresh_storage()

    def _refresh_storage(self):
        """disk_usage runs in a thread: a dead external mount can block it."""
        cfg = self.cfg or read_config()
        for name, key in DISKS:
            path = cfg.get(key)

            def work(name=name, path=path):
                if not path:
                    res = "path not set"
                elif not os.path.exists(path):
                    res = "not mounted"
                else:
                    try:
                        res = shutil.disk_usage(path)
                    except OSError as e:
                        res = f"error ({e.strerror})"
                self._usage_bus.done.emit(name, res)
            threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _center(lab, bold=False):
        lab.setAlignment(Qt.AlignCenter)
        if bold:
            lab.setStyleSheet("font-weight: 600; font-size: 9.5pt;")
        return lab

    def _refresh_position(self):
        """Live motor angles from the Tk app (logs/live_angles.json); config3.h when not available."""
        live = status.live_angles()
        self.pos_box.setTitle("PMT position (live)" if live else "PMT position (last run)")
        for dev, (view, ang, st) in self.pos.items():
            if dev in live:
                tilt, rot, moving = live[dev]
            else:
                try:
                    rot = float(self.cfg.get(f"RotateAngle{dev}", "0") or 0)
                    tilt = float(self.cfg.get(f"TiltAngle{dev}", "0") or 0)
                except (TypeError, ValueError):
                    rot, tilt = 0.0, 0.0
                moving = False
            view.set_angles(rot, tilt, "#BA7517" if moving else "#1D9E75")
            ang.setText(f"Rot {rot:.0f}° · Tilt {tilt:.0f}°")
            if dev not in live:   # same three states as the Tk widget, without glyphs
                txt, col = "no live reading", theme.C["hint"]
            elif moving:
                txt, col = "moving", "#BA7517"
            elif abs(tilt) > 0.5:
                txt, col = "tilt to 0° to rotate", "#dc3545"
            else:
                txt, col = "rotation ok", "#1D9E75"
            st.setText(txt)
            st.setStyleSheet(f"font-size: 9.5pt; font-weight: 700; color: {col};")

    # ---- Run Mode & Parameters (same controls, disabled) -------------------
    def _build_run_mode(self):
        box = QGroupBox("Run Mode && Parameters")
        v = QVBoxLayout(box)
        v.addWidget(QLabel("1. Operation Category:"))
        self.mode_groups = {}

        def group(name, items, indent=0):
            g = QButtonGroup(box)
            for i, (value, text) in enumerate(items):
                rb = QRadioButton(text)
                rb.setProperty("value", value)
                rb.setChecked(i == 0)
                rb.setStyleSheet(f"font-weight: normal; margin-left: {indent}px;")
                g.addButton(rb)
                v.addWidget(rb)
            self.mode_groups[name] = g
            return g

        group("category", [("auto", "General Scan (Auto Control)"), ("manual", "Manual Mode (Laser/Dark Selection)")])
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet(f"color: {theme.C['border']};")
        v.addWidget(line)
        v.addWidget(QLabel("2. Manual Sub-selection:"))
        group("manual", [("laser", "Laser && External trigger (0)"), ("dark", "Dark && Self trigger (1)")], 14)
        v.addWidget(QLabel("Output Format:"))
        group("format", [("root", "ROOT (.root)"), ("csv", "CSV (.csv) (Analysis Not Supported)")])
        self.mode_groups["category"].buttonToggled.connect(lambda *_: self._paint_access())
        self.lay.addWidget(box)

    def run_mode(self):
        """-> (category, manual type, file format) as the Tk app's run_mode / manual_type_var / file_format."""
        return tuple(self.mode_groups[k].checkedButton().property("value") for k in ("category", "manual", "format"))

    # ---- Execute Scripts / View (buttons.json, disabled) -------------------
    def _build_buttons(self, title, frame_id):
        box = QGroupBox(title)
        v = QVBoxLayout(box)
        if frame_id == "scripts":
            row = QHBoxLayout()
            k = QLabel("Selected")
            k.setObjectName("key")
            row.addWidget(k)
            self.sel_lbl = QLabel("none")
            self.sel_lbl.setWordWrap(True)
            self.sel_lbl.setStyleSheet("font-weight: 600;")
            row.addWidget(self.sel_lbl, 1)
            v.addLayout(row)
            from pages.daq import data_files
            data_files.SELECTION_LISTENERS.append(self._show_selection)
        try:
            buttons = json.load(open(os.path.join(env.DAQ_DIR, "buttons.json")))
        except (OSError, json.JSONDecodeError) as e:
            buttons = []
            v.addWidget(QLabel(f"Error loading buttons.json: {e}"))
        for b in buttons:
            if b.get("frame") == frame_id:
                btn = QPushButton(b["label"])
                btn.setStyleSheet("font-weight: normal; text-align: center;")
                cmd = b.get("command", "")
                if hasattr(self.actions, cmd):
                    btn.clicked.connect(lambda _=False, c=cmd: self.actions.run(c))
                    if cmd in script_actions.NEEDS_UNLOCK:
                        self.guarded.append(btn)
                else:
                    btn.setEnabled(False)
                    btn.setToolTip(NOT_IN_PREVIEW)
                v.addWidget(btn)
        self.lay.addWidget(box)

    navigate = None   # set by the main window: fn(page name)

    def _show_page(self, name):
        if self.navigate:
            self.navigate(name)

    def _show_output(self, slot):
        self._show_page("Output")
        from pages.daq import output
        if output.live() is not None:
            output.live().focus(slot)

    def _show_selection(self, paths):
        if not paths:
            self.sel_lbl.setText("none")
            return
        runs = [script_actions.scripts.run_number(p) or "?" for p in paths]
        from pages.daq import data_files
        page = data_files.live()
        kind = page.tabs.tabText(page.tabs.currentIndex()).replace(" Data", "") if page is not None else "Raw"
        shown = ", ".join(runs[:4]) + (f" +{len(runs) - 4}" if len(runs) > 4 else "")
        self.sel_lbl.setText(f"{kind}: run {shown}" if len(runs) == 1 else f"{kind}: {len(runs)} files (runs {shown})")

    def _paint_access(self, on=None):
        on = ACCESS.unlocked if on is None else on
        for b in self.guarded:
            b.setEnabled(on)
            b.setToolTip("" if on else "Unlock to use")
        manual = self.run_mode()[0] == "manual"
        for name, g in self.mode_groups.items():
            for b in g.buttons():
                b.setEnabled(on and (manual or name != "manual"))

    # ---- File & Directory Paths --------------------------------------------
    def _build_paths(self):
        box = QGroupBox("File && Directory Paths")
        g = QGridLayout(box)
        self.path_labels = {}
        for i, key in enumerate(PATH_KEYS):
            k = QLabel(f"{key}:")
            k.setStyleSheet("font-weight: normal;")
            val = QLabel("N/A")
            val.setWordWrap(True)
            val.setStyleSheet("font-weight: normal;")
            val.setTextInteractionFlags(Qt.TextSelectableByMouse)
            btn = QPushButton(">")
            btn.setFixedWidth(28)
            btn.setStyleSheet("padding: 2px 0; font-weight: 600;")
            btn.setToolTip("Open a terminal here")
            btn.clicked.connect(lambda _=False, k=key: self._open_terminal(k))
            g.addWidget(k, i, 0, Qt.AlignTop)
            g.addWidget(val, i, 1)
            g.addWidget(btn, i, 2, Qt.AlignTop)
            self.path_labels[key] = val
        g.setColumnStretch(1, 1)
        self.lay.addWidget(box)

    def _refresh_paths(self):
        for key, lab in self.path_labels.items():
            lab.setText(self.cfg.get(key) or "N/A")

    def _open_terminal(self, key):
        path = self.cfg.get(key)
        if path and os.path.isdir(path):
            subprocess.Popen(["gnome-terminal", f"--working-directory={path}"], start_new_session=True)

    def refresh(self):
        self.cfg = read_config()
        self._refresh_position()
        self._refresh_paths()

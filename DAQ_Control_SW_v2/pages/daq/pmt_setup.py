"""DAQ System > PMT Setup & Helper, mirroring ui_manager._create_status_frame /
_update_pmt_status_and_helper: one cell per PMT with the TOP VIEW pin map and
the RIGHT SIDE VIEW tilt drawing (same geometry as _render_helper_diagram and
_render_side_view). The fourth cell is a legend (the Tk app's Storage Capacity
cell moved to the left panel). SN2/SN3 angles are the Tk app's live motor
readings (logs/live_angles.json), config3.h when those are not fresh.
"""
import math

from PyQt5.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt5.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PyQt5.QtWidgets import (QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
                             QVBoxLayout, QWidget)

from app import theme
from core import status
from core.env import read_config

# Standard pin angles with cable A at 9 o'clock (same table as the Tk app)
POS_MAP = {'E': 0, 'F': 45, 'G': 90, 'H': 135, 'A': 180, 'B': 225, 'C': 270, 'D': 315}


def _font(size, bold=True):
    f = QFont()
    f.setPointSizeF(size)
    f.setBold(bold)
    return f


def _arrow(p, x1, y1, x2, y2, color, width=3):
    """Line with a filled arrow head at (x2, y2)."""
    p.setPen(QPen(QColor(color), width, Qt.SolidLine, Qt.RoundCap))
    p.drawLine(QPointF(x1, y1), QPointF(x2, y2))
    ang = math.atan2(y2 - y1, x2 - x1)
    size = 4 + 2 * width
    pts = [QPointF(x2, y2)]
    for d in (math.radians(150), math.radians(-150)):
        pts.append(QPointF(x2 + size * math.cos(ang + d), y2 + size * math.sin(ang + d)))
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    p.drawPolygon(QPolygonF(pts))


def _ink():
    return QColor(theme.C["text"])


def _text(p, x, y, text, color=None, size=10, bold=True, anchor="c"):
    """Draw text anchored like Tk (c, n, s, e, w, nw)."""
    p.setFont(_font(size, bold))
    p.setPen(QColor(color) if color is not None else _ink())
    fm = p.fontMetrics()
    w, h = fm.horizontalAdvance(text), fm.ascent()
    dx = {"c": -w / 2, "n": -w / 2, "s": -w / 2, "e": -w, "w": 0, "nw": 0}[anchor]
    dy = {"c": h / 2, "n": h, "s": 0, "e": h / 2, "w": h / 2, "nw": h}[anchor]
    p.drawText(QPointF(x + dx, y + dy), text)


def _tag(p, x, y, text, color, size=9):
    """Centred text on a card-coloured rounded box, readable on top of bands and arrows."""
    p.setFont(_font(size))
    fm = p.fontMetrics()
    w, h = fm.horizontalAdvance(text) + 6, fm.height()
    p.setPen(Qt.NoPen)
    box = QColor(theme.C["card"])
    box.setAlpha(230)
    p.setBrush(box)
    p.drawRoundedRect(QRectF(x - w / 2, y - h / 2, w, h), 3, 3)
    _text(p, x, y, text, color, size)


def _arc_arrow(p, cx, cy, r, a0, a1, color, width=2):
    """Arc from a0 to a1 (degrees, counter-clockwise positive, y up) with a head at a1."""
    p.setPen(QPen(QColor(color), width, Qt.SolidLine, Qt.RoundCap))
    p.setBrush(Qt.NoBrush)
    p.drawArc(QRectF(cx - r, cy - r, 2 * r, 2 * r), int(a0 * 16), int((a1 - a0) * 16))
    t = math.radians(a1)
    ex, ey = cx + r * math.cos(t), cy - r * math.sin(t)
    d = 1 if a1 > a0 else -1   # direction of travel along the arc
    tx, ty = -math.sin(t) * d, -math.cos(t) * d   # screen tangent
    size = 7
    pts = [QPointF(ex + tx * size, ey + ty * size)]
    for s in (1, -1):
        pts.append(QPointF(ex - tx * size * 0.2 + s * math.cos(t) * size * 0.6,
                           ey - ty * size * 0.2 - s * math.sin(t) * size * 0.6))
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    p.drawPolygon(QPolygonF(pts))


class TopView(QWidget):
    W, H = 300, 250

    def __init__(self):
        super().__init__()
        self.setMinimumSize(int(self.W * 0.8), int(self.H * 0.8))
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.cable, self.rot, self.tilt = "A", 0.0, 0.0

    def set_state(self, cable, rot, tilt):
        self.cable, self.rot, self.tilt = cable, rot, tilt
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(theme.C["card"]))
        k = min(self.width() / self.W, self.height() / self.H)   # keep the aspect, centre the drawing
        p.translate((self.width() - self.W * k) / 2, (self.height() - self.H * k) / 2)
        p.scale(k, k)
        CX, CY, R = 150, 125, 82

        def pos(a, r):
            return CX + r * math.cos(math.radians(a)), CY - r * math.sin(math.radians(a))

        cable_ang = 180 + self.rot
        offset = cable_ang - POS_MAP.get(self.cable, 180)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#ddeeff"))   # scan axis band
        p.drawRect(QRectF(CX - 8, CY - R - 20, 16, 2 * R + 40))
        a_ang = POS_MAP["A"] + offset
        for pin, color in (("A", "#b2f2bb"), ("G", "#ffd8a8")):   # +Y / +X bands
            ang = POS_MAP[pin] + offset
            p.setPen(QPen(QColor(color), 10, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(*pos(ang, R)), QPointF(*pos(ang + 180, R)))
        p.setPen(QPen(QColor("gray"), 2))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(CX, CY), R, R)
        _arrow(p, CX, CY - R + 5, CX, CY - R - 15, "#1971c2")
        _arrow(p, CX, CY + R - 5, CX, CY + R + 15, "#1971c2")
        # left of the top arrow, so a +X/+Y label on the axis never covers it; green = laser on axis
        _text(p, CX - 14, CY - R - 14, "Scan Axis", "#1971c2", 10, anchor="e")
        _text(p, 6, 6, f"Rot {self.rot:.0f}°  Tilt {self.tilt:.0f}°", _ink(), 9, anchor="nw")

        x1, y1 = pos(cable_ang, R - 5)
        x2, y2 = pos(cable_ang, R + 30)
        _arrow(p, x1, y1, x2, y2, "red")
        tx, ty = pos(cable_ang, R + 42)
        n = cable_ang % 360
        anchor = "s" if 45 < n < 135 else "e" if 135 <= n < 225 else "n" if 225 <= n < 315 else "w"
        lw, pad = 42, 3   # keep "Cable" inside the canvas
        tx = max(tx, pad + lw) if anchor == "e" else min(tx, self.W - pad - lw) if anchor == "w" \
            else max(pad + lw / 2, min(self.W - pad - lw / 2, tx))
        if anchor in ("e", "w"):
            ty += 14   # below a sideways arrow, clear of the +X/+Y label
            _text(p, tx, ty, "Cable", "red", 10, anchor=anchor)
        else:            # up/down arrow: write beside its tip
            _text(p, x2 + 10, y2, "Cable", "red", 10, anchor="w")

        for ch, std in POS_MAP.items():
            lx, ly = pos(std + offset, R - 15)
            _text(p, lx, ly, ch, "red" if ch == self.cable else _ink(), 12)
            if ch == "A":
                _tag(p, *pos(std + offset, R + 16), "+Y", "#2f9e44", 10)
            elif ch == "G":
                _tag(p, *pos(std + offset, R + 16), "+X", "#e67700", 10)

        # DY1 on the tube centre, DY2 right behind it; the assembly turns with pin A
        rad = math.radians(a_ang)
        radial = (math.cos(rad), -math.sin(rad))
        tangent = (-radial[1], radial[0])

        def box(cx, cy, hw, hh):
            return QPolygonF([QPointF(cx + sw * hw * tangent[0] + sh * hh * radial[0],
                                      cy + sw * hw * tangent[1] + sh * hh * radial[1])
                              for sw, sh in ((-1, -1), (1, -1), (1, 1), (-1, 1))])
        hw, h1, h2 = 13, 13, 7
        d2x, d2y = CX - radial[0] * (h1 + 1 + h2), CY - radial[1] * (h1 + 1 + h2)
        p.setPen(QPen(_ink(), 2))
        p.setBrush(Qt.NoBrush)
        p.drawPolygon(box(CX, CY, hw, h1))
        p.drawPolygon(box(d2x, d2y, hw, h2))
        # labels on opposite ends of the assembly so they never touch each other
        _tag(p, CX + radial[0] * (h1 + 13), CY + radial[1] * (h1 + 13), "DY1", _ink(), 9)
        _tag(p, d2x - radial[0] * (h2 + 13), d2y - radial[1] * (h2 + 13), "DY2", _ink(), 9)
        # +Rot: counter-clockwise seen from above (same sense as the cable arrow moves)
        _arc_arrow(p, self.W - 30, 34, 14, -60, 200, "#7048e8")
        _text(p, self.W - 30, 62, "+Rot", "#7048e8", 9)
        _text(p, CX, 244, "TOP VIEW", theme.C["hint"], 9)
        p.end()


class SideView(QWidget):
    W, H = 170, 250

    def __init__(self):
        super().__init__()
        self.setMinimumSize(int(self.W * 0.8), int(self.H * 0.8))
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.cable, self.rot, self.tilt = "A", 0.0, 0.0

    def set_state(self, cable, rot, tilt):
        self.cable, self.rot, self.tilt = cable, rot, tilt
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(theme.C["card"]))
        k = min(self.width() / self.W, self.height() / self.H)   # keep the aspect, centre the drawing
        p.translate((self.width() - self.W * k) / 2, (self.height() - self.H * k) / 2)
        p.scale(k, k)
        cx, cy, Rb = 85, 150, 56
        _text(p, self.W / 2, 244, "RIGHT SIDE VIEW", theme.C["hint"], 9)
        kr = abs(self.tilt)
        pos_deg = max(0.0, min(90.0, -0.0049 * kr * kr + 1.7515 * kr - 0.0402))   # KR -> Hamamatsu
        sign = -1.0 if self.tilt < 0 else 1.0
        th = math.radians(-sign * pos_deg)   # mirrored: viewed from the E side
        upx, upy, rxh, ryh = math.sin(th), -math.cos(th), math.cos(th), math.sin(th)

        def scr(lx, ly):
            return cx + lx * rxh + ly * upx, cy + lx * ryh + ly * upy

        _text(p, 6, 6, f"Tilt {self.tilt:.0f}°  ->  Pos {sign * pos_deg:.0f}°", _ink(), 9, anchor="nw")
        p.setPen(QPen(QColor("#bbbbbb"), 1, Qt.DashLine))
        p.drawLine(QPointF(cx, cy), QPointF(cx, cy - Rb - 35))
        poly, spot = QPolygonF(), None
        for a in range(0, 181, 5):
            sx, sy = scr(Rb * math.cos(math.radians(a)), Rb * math.sin(math.radians(a)))
            poly.append(QPointF(sx, sy))
            if spot is None or abs(sx - cx) < abs(spot[0] - cx) - 0.001:
                spot = (sx, sy)
        p.setPen(QPen(QColor("#5a7fa5"), 2))
        p.setBrush(QColor("#d0e7fb"))
        p.drawPolygon(poly)

        offset = 180 + self.rot - POS_MAP.get(self.cable, 180)

        def nearest(target):
            std = target - offset
            return min(POS_MAP, key=lambda ch: abs(((POS_MAP[ch] - std) + 180) % 360 - 180))
        lx, ly = scr(-Rb, 0)
        rx, ry = scr(Rb, 0)
        _text(p, lx - 10, ly, nearest(270), "#e67700", 10, anchor="e")
        _text(p, rx + 10, ry, nearest(90), "#e67700", 10, anchor="w")

        # +Tilt leans the dome apex to the left in this view (see th above)
        _arc_arrow(p, cx, cy, Rb + 14, 100, 135, "#7048e8")
        _text(p, cx + (Rb + 30) * math.cos(math.radians(118)), cy - (Rb + 30) * math.sin(math.radians(118)),
              "+Tilt", "#7048e8", 9)
        _arrow(p, cx, cy - Rb - 42, cx, spot[1], "red")
        _text(p, cx, cy - Rb - 50, "Laser", "red", 9)
        p.setPen(QPen(QColor("red"), 1))
        p.setBrush(QColor("#ff5555"))
        p.drawEllipse(QPointF(*spot), 5, 5)
        p.setPen(Qt.NoPen)
        p.setBrush(_ink())
        p.drawEllipse(QPointF(cx, cy), 2, 2)
        p.end()


class PmtCell(QFrame):
    def __init__(self, idx):
        super().__init__()
        self.idx = idx
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(f"PmtCell {{ background: {theme.C['card']}; border: 1px solid {theme.C['border']}; border-radius: 8px; }}")
        lay = QVBoxLayout(self)
        head = QHBoxLayout()
        self.title = QLabel("Monitor PMT" if idx == 1 else f"SN{idx}")   # SN1 is the fixed monitor
        head.addWidget(self.title)
        head.addStretch(1)
        self.info = QLabel()
        self.info.setStyleSheet("font-weight: 600;")
        head.addWidget(self.info)
        lay.addLayout(head)
        body = QHBoxLayout()
        self.top, self.side = TopView(), SideView()
        body.addWidget(self.top, 30)
        body.addWidget(self.side, 17)
        lay.addLayout(body, 1)

    def set_from(self, cfg, live=None):
        """live = (tilt, rot, moving) from the Tk app's motor monitor; else config3.h."""
        i = self.idx
        sn = (cfg.get(f"SN{i}") or "").strip()
        cable = (cfg.get(f"direction{i}") or "A").strip().upper()
        if live:
            tilt, rot, _moving = live
        else:
            try:
                rot = float(cfg.get(f"RotateAngle{i}") or 0)
                tilt = float(cfg.get(f"TiltAngle{i}") or 0)
            except ValueError:
                rot = tilt = 0.0
        self.title.setStyleSheet(f"font-size: 14pt; font-weight: 700; color: {'#d39e00' if sn else '#adb5bd'};")
        self.info.setText(f"{sn or 'N/A'} - {cable}   |   HV: {cfg.get(f'HV{i}', '0')} V   |   "
                          f"Rotation: {rot:.0f}° / Tilt: {tilt:.0f}°"
                          + ("" if i == 1 else ("   (live)" if live else "   (last run)")))
        self.top.set_state(cable, rot, tilt)
        self.side.set_state(cable, rot, tilt)


def _legend():
    box = QGroupBox("How to read the diagrams")
    g = QGridLayout(box)
    rows = [("#ddeeff", "Scan Axis", "fixed stage axis the laser scans along (blue band)"),
            ("#b2f2bb", "+Y (pin A)", "green band: Y-scan direction, through cable pin A"),
            ("#ffd8a8", "+X (pin G)", "orange band: X-scan direction, through pin G"),
            ("#ff6b6b", "Cable", "red arrow and red letter: the pin the cable is on"),
            ("#5a7fa5", "DY1 / DY2", "dynode assembly, turns together with pin A"),
            ("#d0e7fb", "Side view", "dome tilted by the stage; red dot = laser spot")]
    for r, (color, name, text) in enumerate(rows):
        sw = QLabel()
        sw.setFixedSize(14, 14)
        sw.setStyleSheet(f"background: {color}; border: 1px solid {theme.C['hint']}; border-radius: 3px;")
        g.addWidget(sw, r, 0)
        n = QLabel(name)
        n.setStyleSheet("font-weight: 600;")
        g.addWidget(n, r, 1)
        t = QLabel(text)
        t.setObjectName("key")
        t.setWordWrap(True)
        g.addWidget(t, r, 2)
    g.setColumnStretch(2, 1)
    g.setRowStretch(len(rows), 1)
    return box


class PmtSetupPage(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        head = QHBoxLayout()
        t = QLabel("PMT Status Overview")
        t.setObjectName("pageTitle")
        head.addWidget(t)
        head.addStretch(1)
        b = QPushButton("Refresh All")
        b.clicked.connect(self.refresh)
        head.addWidget(b)
        lay.addLayout(head)
        grid = QGridLayout()
        self.cells = [PmtCell(i) for i in (1, 2, 3)]
        for i, c in enumerate(self.cells):
            grid.addWidget(c, *divmod(i, 2))
        grid.addWidget(_legend(), 1, 1)
        grid.setRowStretch(0, 1)
        grid.setRowStretch(1, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid, 1)
        self.refresh()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_live)
        self.timer.start(1000)

    def refresh(self):
        self.cfg = read_config()
        self.refresh_live()

    def refresh_live(self):
        live = status.live_angles()   # SN2 = device 2, SN3 = device 3; the monitor has no motor
        for c in self.cells:
            c.set_from(self.cfg, live.get(c.idx) if c.idx in (2, 3) else None)

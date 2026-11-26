"""B-field Monitoring: coil currents, |B| per magnetometer and temperatures from the synced B-field log."""
import glob
import os
from datetime import datetime

import numpy as np
import pandas as pd
import pyqtgraph as pg
from PyQt5.QtCore import QDateTime, Qt, QTimer
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QDateTimeEdit, QDialog, QGridLayout, QHBoxLayout, QLabel,
                             QLineEdit, QMenu, QMessageBox, QPushButton, QSizePolicy, QVBoxLayout, QWidget)

from app import theme
from core import env

RANGES = {"Last 6h": pd.Timedelta(hours=6), "Last 24h": pd.Timedelta(hours=24),
          "Last 3 days": pd.Timedelta(days=3), "Last 7 days": pd.Timedelta(days=7), "All": None}
CURRENTS = ["I1", "I2", "I3", "I4"]
TEMPS = ["T1", "T2", "T3", "T4", "T5", "T6", "T7"]
MAGS = [f"B_M{n}_mG" for n in (1, 2, 3, 4)]
NAMES = {"T7": "T7 (AUS box)",   # T7 probe sits inside the AUS group's box
         "B_M2_mG": "Rot1 (M2)", "B_M3_mG": "Rot2 (M3)"}   # magnetometer M2 sits at Rot1, M3 at Rot2
PANELS = [("Coil current [A]", CURRENTS, "{:.3f}"), ("|B| [mG]", MAGS, "{:.1f}"), ("Temperature [°C]", TEMPS, "{:.2f}")]
COLORS = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b", "#17becf"]
SYNC_LOG = os.path.expanduser("~/logs/bfield_log_sync.log")
STALE_MIN = 20       # logger samples every 10 min, sync runs every 10 min
COIL_OFF_A = 0.01    # all four coil currents below this: coils switched off
GAP_S = 1800         # samples further apart than this are not joined by a line


def _name(c):
    """Legend name of a log column."""
    return NAMES.get(c, c[2:4] if c.startswith("B_") else c)


def load_bfield():
    """Newest log_bfield_*.csv as a DataFrame; |B| = sqrt(x^2+y^2+z^2)*1000 mG per magnetometer."""
    paths = glob.glob(os.path.join(env.ADC_DIR, "log_bfield_*.csv"))
    if not paths:
        return None
    try:
        df = pd.read_csv(max(paths, key=os.path.getmtime))
    except Exception:
        return None
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")

    def num(s):
        return pd.to_numeric(s.astype(str).str.replace("N/A", "", regex=False), errors="coerce")

    for c in CURRENTS + TEMPS:
        if c in df.columns:
            df[c] = num(df[c])
    for n in (1, 2, 3, 4):
        cols = [f"M{n}_X", f"M{n}_Y", f"M{n}_Z"]
        if all(c in df.columns for c in cols):
            x, y, z = (num(df[c]) for c in cols)
            df[f"B_M{n}_mG"] = (x ** 2 + y ** 2 + z ** 2) ** 0.5 * 1000.0
    df = df.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    off = datetime.now().astimezone().utcoffset().total_seconds()   # log stamps are local time
    df["epoch"] = df["timestamp"].astype("int64") / 1e9 - off
    return df


def coils_off_since(df):
    """Timestamp the coils went off when the newest sample has all of I1-I4 at zero, else None."""
    cur = df[[c for c in CURRENTS if c in df]]
    if cur.shape[1] != len(CURRENTS):
        return None
    off = (cur.abs() < COIL_OFF_A).all(axis=1) & cur.notna().all(axis=1)
    if not off.iloc[-1]:
        return None
    on = off[~off]
    return df["timestamp"].iloc[on.index[-1] + 1 if len(on) else 0]


def break_gaps(x, y):
    """NaN between samples more than GAP_S apart, so connect='finite' leaves the gap open."""
    idx = np.nonzero(np.diff(x) > GAP_S)[0] + 1
    return np.insert(x, idx, x[idx - 1] + 1), np.insert(y, idx, np.nan)


def last_sync():
    """(ok, text) from the sync script's last log line."""
    try:
        with open(SYNC_LOG, encoding="utf-8", errors="replace") as f:
            line = f.read().strip().splitlines()[-1]
    except (OSError, IndexError):
        return None, "no sync log"
    ok = "[OK]" in line
    return ok, f"Sync {'OK' if ok else 'FAILED'} {line[11:16]}"


def fit_y(vb, curves):
    """Y range of the visible curves inside the current X range."""
    lo, hi = vb.viewRange()[0]
    ys = []
    for c in curves:
        if not c.isVisible():
            continue
        x, y = c.getData()
        if x is None or y is None or not len(x):
            continue
        x, y = np.asarray(x, float), np.asarray(y, float)
        ys.append(y[(x >= lo) & (x <= hi) & np.isfinite(y)])
    ys = np.concatenate(ys) if ys else np.array([])
    if not ys.size:
        return
    y0, y1 = float(ys.min()), float(ys.max())
    pad = max((y1 - y0) * 0.08, abs(y1) * 0.01) or 1.0
    vb.setYRange(y0 - pad, y1 + pad, padding=0)


class AxisRangeDialog(QDialog):
    """Axis ranges of one plot; Apply keeps the window open."""

    def __init__(self, plot, title, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Axis Range - {title}")
        self.plot = plot
        self.vb = plot.getPlotItem().vb
        lay = QGridLayout(self)
        lay.setHorizontalSpacing(8)
        lay.setVerticalSpacing(8)
        self.x0, self.x1 = QDateTimeEdit(), QDateTimeEdit()
        for e in (self.x0, self.x1):
            e.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
            e.setCalendarPopup(True)
        lay.addWidget(QLabel("Time from"), 0, 0)
        lay.addWidget(self.x0, 0, 1)
        lay.addWidget(QLabel("to"), 0, 2)
        lay.addWidget(self.x1, 0, 3)
        quick = QHBoxLayout()
        for text, hours in (("Last 1 h", 1), ("Last 6 h", 6), ("Last 24 h", 24), ("Last 3 d", 72)):
            b = QPushButton(text)
            b.setAutoDefault(False)
            b.clicked.connect(lambda _, h=hours: self._last(h))
            quick.addWidget(b)
        lay.addLayout(quick, 1, 1, 1, 3)
        self.lo, self.hi = QLineEdit(), QLineEdit()
        for e in (self.lo, self.hi):
            e.returnPressed.connect(self.apply)
        lay.addWidget(QLabel("Y min"), 2, 0)
        lay.addWidget(self.lo, 2, 1)
        lay.addWidget(QLabel("max"), 2, 2)
        lay.addWidget(self.hi, 2, 3)
        self.msg = QLabel("")
        self.msg.setStyleSheet(f"color: {theme.C['bad']};")
        lay.addWidget(self.msg, 3, 0, 1, 4)
        row = QHBoxLayout()
        row.addStretch(1)
        fit, apply, close = QPushButton("Fit Y to Time Range"), QPushButton("Apply"), QPushButton("Close")
        fit.setAutoDefault(False)
        close.setAutoDefault(False)
        apply.setDefault(True)
        fit.clicked.connect(lambda: self.apply(fit=True))
        apply.clicked.connect(self.apply)
        close.clicked.connect(self.close)
        for b in (fit, apply, close):
            row.addWidget(b)
        lay.addLayout(row, 4, 0, 1, 4)

    def load(self):
        x0, x1 = self.vb.viewRange()[0]
        self.x0.setDateTime(QDateTime.fromSecsSinceEpoch(int(x0)))
        self.x1.setDateTime(QDateTime.fromSecsSinceEpoch(int(round(x1))))
        y0, y1 = self.vb.viewRange()[1]
        self.lo.setText(f"{y0:.6g}")
        self.hi.setText(f"{y1:.6g}")
        self.msg.setText("")

    def _last(self, hours):
        now = QDateTime.currentDateTime()
        self.x0.setDateTime(now.addSecs(-3600 * hours))
        self.x1.setDateTime(now)
        self.apply(fit=True)

    def apply(self, fit=False):
        if not fit:
            try:
                a, b = float(self.lo.text()), float(self.hi.text())
            except ValueError:
                self.msg.setText("Y min / max must be numbers.")
                return
            if b <= a:
                self.msg.setText("Y min must be below Y max.")
                return
        x0, x1 = self.x0.dateTime().toSecsSinceEpoch(), self.x1.dateTime().toSecsSinceEpoch()
        if x1 <= x0:
            self.msg.setText("Time 'to' must be after 'from'.")
            return
        self.vb.setXRange(x0, x1, padding=0)
        if fit:
            fit_y(self.vb, self.plot.getPlotItem().listDataItems())
        else:
            self.vb.setYRange(a, b, padding=0)
        self.load()


class BfieldPage(QWidget):
    def __init__(self):
        super().__init__()
        self.df = None
        self.mark = None
        self.first = True
        pg.setConfigOptions(background=theme.C["card"], foreground=theme.C["text"], antialias=True)
        lay = QVBoxLayout(self)

        ctl = QHBoxLayout()
        ctl.addWidget(QLabel("Range:"))
        self.range = QComboBox()
        self.range.addItems(list(RANGES))
        self.range.setCurrentText("Last 24h")
        self.range.currentTextChanged.connect(lambda _t: self._apply_range())
        ctl.addWidget(self.range)
        self.auto = QCheckBox("Auto-refresh (60s)")
        self.auto.setChecked(True)
        ctl.addSpacing(10)
        ctl.addWidget(self.auto)
        ref = QPushButton("Refresh")
        ref.clicked.connect(self.refresh)
        ctl.addWidget(ref)
        ctl.addSpacing(15)
        ctl.addWidget(QLabel("Jump to:"))
        self.jump = QLineEdit()
        self.jump.setPlaceholderText("YYYY-MM-DD HH:MM")
        self.jump.setMaximumWidth(170)
        self.jump.returnPressed.connect(self._jump)
        ctl.addWidget(self.jump)
        go = QPushButton("Go")
        go.clicked.connect(self._jump)
        ctl.addWidget(go)
        ctl.addSpacing(15)
        auto_scale, home = QPushButton("Auto Scale"), QPushButton("Reset View")
        auto_scale.clicked.connect(lambda: [self._auto_scale(p) for p in self.plots])
        home.clicked.connect(lambda: [self._reset_view(p) for p in self.plots])
        ctl.addWidget(auto_scale)
        ctl.addWidget(home)
        ctl.addStretch(1)
        lay.addLayout(ctl)

        info = QHBoxLayout()     # second line, so long texts never widen the page
        self.view = QLabel()
        self.view.setStyleSheet(f'font-family: "{theme.MONO_FONT}"; font-size: 10pt; color: {theme.C["text"]};')
        self.status = QLabel()
        self.sync = QLabel()
        for w in (self.view, self.status, self.sync):
            w.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            info.addWidget(w, 1)
        lay.addLayout(info)
        self.coils = QLabel()
        self.coils.setStyleSheet(f"background: {theme.C['bad']}; color: white; font-size: 12pt; font-weight: 700;"
                                 " padding: 4px; border-radius: 4px;")
        self.coils.hide()
        lay.addWidget(self.coils)

        self.plots, self.curves, self.marks, self.panel = [], {}, [], {}
        for i, (ylabel, cols, fmt) in enumerate(PANELS):
            p = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem(orientation="bottom")})
            p.setMinimumHeight(120)
            p.setLabel("left", ylabel)
            self.panel[p] = (cols, fmt)
            p.showGrid(x=True, y=True, alpha=0.25)
            p.getViewBox().setMouseMode(pg.ViewBox.RectMode)
            legend = p.addLegend(offset=(10, 10))
            legend.setBrush(pg.mkBrush(theme.C["card"]))
            legend.setPen(pg.mkPen(theme.C["border"], width=1))
            legend.setLabelTextColor(theme.C["text"])
            for j, c in enumerate(cols):
                name = _name(c)
                self.curves[c] = p.plot(pen=pg.mkPen(COLORS[j % len(COLORS)], width=2), name=name)
                self.curves[c].column = c
            m = pg.InfiniteLine(angle=90, pen=pg.mkPen("#d62728", width=2))
            m.hide()
            p.addItem(m)
            self.marks.append(m)
            if i:
                p.setXLink(self.plots[0])
            p.scene().sigMouseClicked.connect(lambda ev, pw=p: self._on_click(ev, pw))
            self._setup_view(p, ylabel)
            p.getPlotItem().vb.sigXRangeChanged.connect(lambda _vb, _r, pw=p: self._show_means(pw))
            self.plots.append(p)
            lay.addWidget(p, 1)

        self.timer = QTimer(self)
        self.timer.timeout.connect(lambda: self.auto.isChecked() and self.refresh())
        self.timer.start(60_000)
        self.refresh()

    # ---------------------------------------------------------------- view control (as the HV monitor)
    def _auto_scale(self, plot):
        fit_y(plot.getPlotItem().vb, plot.getPlotItem().listDataItems())

    def _reset_view(self, plot):
        plot.getPlotItem().vb.enableAutoRange()

    def _setup_view(self, plot, title):
        menu = QMenu(plot)
        menu.addAction("Auto Scale", lambda: self._auto_scale(plot))
        menu.addAction("Reset View", lambda: self._reset_view(plot))
        menu.addAction("Set Range...", lambda: self._range_dialog(plot, title))
        plot.getPlotItem().setMenuEnabled(False, enableViewBoxMenu=None)
        plot.getPlotItem().vb.getMenu = lambda ev, m=menu: m
        legend = plot.getPlotItem().legend
        for sample, label in legend.items:
            def click(ev, curve=sample.item, sample=sample, vb=plot.getPlotItem().vb):
                if ev.button() != Qt.LeftButton:
                    return
                ev.accept()
                curve.setVisible(True)
                sample.update()
                fit_y(vb, [curve])
            label.mouseClickEvent = click

    def _range_dialog(self, plot, title):
        dlg = getattr(plot, "_range_dlg", None)
        if dlg is None:
            dlg = plot._range_dlg = AxisRangeDialog(plot, title, self)
        dlg.load()
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _apply_range(self):
        """Range combo: X window ending at the last sample, Y fitted to it."""
        if self.df is None or self.df.empty:
            return
        win = RANGES[self.range.currentText()]
        x1 = float(self.df["epoch"].iloc[-1])
        x0 = x1 - win.total_seconds() if win is not None else float(self.df["epoch"].iloc[0])
        self.plots[0].getPlotItem().vb.setXRange(x0, x1, padding=0.01)
        for p in self.plots:
            self._auto_scale(p)

    # ---------------------------------------------------------------- legend values
    def _set_legend(self, plot, values):
        """Legend labels 'name  value' from {column: value}."""
        fmt = self.panel[plot][1]
        for sample, label in plot.getPlotItem().legend.items:
            c = sample.item.column
            v = values.get(c)
            name = _name(c)
            if v is not None and np.isfinite(v):
                label.setText(f"{name}  {fmt.format(v)}", color=theme.C["text"])
            else:
                label.setText(f"{name} (no data)", color=theme.C["muted"])

    def _show_means(self, plot):
        """Mean of each curve over the time span on screen (a drag zoom or pan ends the sample view)."""
        if self.df is None:
            return
        self.mark = None
        for m in self.marks:
            m.hide()
        lo, hi = plot.getPlotItem().vb.viewRange()[0]
        x = self.df["epoch"].to_numpy()
        sel = self.df[(x >= lo) & (x <= hi)]
        self._set_legend(plot, {c: float(sel[c].mean()) for c in self.panel[plot][0] if c in sel and sel[c].notna().any()})
        t0, t1 = pd.Timestamp.fromtimestamp(lo), pd.Timestamp.fromtimestamp(hi)
        self.view.setText(f"Mean  {t0:%Y-%m-%d %H:%M} - {t1:%Y-%m-%d %H:%M}  ({len(sel)} samples)")

    # ---------------------------------------------------------------- data
    def _set_status(self, label, text, ok):
        label.setText(text)
        label.setStyleSheet(f"font-size: 11pt; font-weight: 700; color: {theme.C['accent'] if ok else theme.C['bad']};")

    def refresh(self):
        ok, text = last_sync()
        self._set_status(self.sync, text, bool(ok))
        df = load_bfield()
        if df is None or df.empty:
            self._set_status(self.status, "No B-field log found", False)
            return
        self.df = df
        x = df["epoch"].to_numpy()
        for c, curve in self.curves.items():
            if c in df and df[c].notna().any():
                curve.setData(*break_gaps(x, df[c].to_numpy(float)), connect="finite")
                curve.setVisible(True)
            else:
                curve.setData([], [])
                curve.setVisible(False)
                legend = curve.getViewBox().parentItem().legend
                if any(s.item is curve for s, _l in legend.items):
                    legend.removeItem(curve)       # a channel the logger never wrote (e.g. M4, T4)
        last = df["timestamp"].iloc[-1]
        age = (pd.Timestamp.now() - last).total_seconds() / 60
        self._set_status(self.status, f"Last sample: {last} ({age:.0f} min ago)", age < STALE_MIN)
        since = coils_off_since(df)
        self.coils.setText(f"COILS OFF  (I1-I4 = 0 A since {since:%Y-%m-%d %H:%M})" if since is not None else "")
        self.coils.setVisible(since is not None)
        if self.first:
            self.first = False
            self._apply_range()
        if self.mark is not None:
            self._show_point(self.mark)
        else:
            for p in self.plots:
                self._show_means(p)

    # ---------------------------------------------------------------- inspect a sample
    def _on_click(self, ev, plot):
        if ev.button() != Qt.LeftButton or self.df is None:
            return
        vb = plot.getPlotItem().vb
        if not vb.sceneBoundingRect().contains(ev.scenePos()):
            return
        self._show_point(pd.Timestamp.fromtimestamp(vb.mapSceneToView(ev.scenePos()).x()))

    def _jump(self):
        text = self.jump.text().strip()
        if not text or self.df is None:
            return
        try:
            t = pd.Timestamp(text)
            row = self.df.loc[(self.df["timestamp"] - t).abs().idxmin()]
        except Exception:
            QMessageBox.warning(self, "B-field", f"Could not parse '{text}'.\nTry: YYYY-MM-DD HH:MM")
            return
        vb = self.plots[0].getPlotItem().vb
        x0, x1 = vb.viewRange()[0]
        xm = float(row["epoch"])
        if not x0 <= xm <= x1:
            vb.setXRange(xm - (x1 - x0) / 2, xm + (x1 - x0) / 2, padding=0)
        self._show_point(t)   # after the view move: a range change ends the sample view
        if abs((row["timestamp"] - t).total_seconds()) > GAP_S:
            self.view.setText(f"{self.view.text()}   (no data at {t:%Y-%m-%d %H:%M}; nearest sample)")

    def _show_point(self, target):
        df = self.df
        row = df.loc[(df["timestamp"] - target).abs().idxmin()]
        self.mark = row["timestamp"]
        for p in self.plots:
            self._set_legend(p, {c: float(row[c]) for c in self.panel[p][0] if c in row and pd.notna(row[c])})
        self.view.setText(f"Sample  {row['timestamp']:%Y-%m-%d %H:%M:%S}")
        for m in self.marks:
            m.setPos(float(row["epoch"]))
            m.show()

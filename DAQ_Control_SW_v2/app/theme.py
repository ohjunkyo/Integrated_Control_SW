"""Colours, type scale, spacing and the Qt stylesheet.

Neutral grey background and colour only for state (ISA-101 style), text contrast
>= 4.5:1 (WCAG AA), spacing on a 4 px grid, one control height for inputs and buttons.
"""
import os

from PyQt5.QtCore import QEvent, QObject, Qt, QTimer
from PyQt5.QtGui import QColor, QFont, QFontDatabase, QPalette
from PyQt5.QtWidgets import QPushButton

LIGHT = {"bg": "#e9ecf0", "card": "#ffffff", "border": "#cfd5dd", "line": "#e3e7ec",
         "text": "#161b24", "muted": "#4b5563", "hint": "#6b7280",
         "accent": "#0b63b6", "accent_soft": "#e6f0fa", "on_accent": "#ffffff",
         "good": "#1a7f37", "warn": "#b45309", "bad": "#b91c1c",
         "field_ro": "#f6f7f9", "hover": "#f2f4f7", "tab_hover": "#dfe3e8", "disabled": "#9aa1ab",
         "alt_row": "#f7f8fa", "header": "#f3f5f8", "tooltip": "#1f2430",
         "term_bg": "#1e2230", "term_fg": "#e6e9ef", "term_border": "#2c3244", "term_sel": "#3b4a6b",
         "btn": "#e8edf3", "btn_border": "#aab4c0", "btn_hover": "#dbe7f5", "btn_press": "#c4d7ee"}
DARK = {"bg": "#1d2026", "card": "#272b33", "border": "#3a404a", "line": "#323842",
        "text": "#e6e9ef", "muted": "#aeb5c1", "hint": "#9aa2b0",
        "accent": "#58a6ff", "accent_soft": "#22344a", "on_accent": "#0d1117",
        "good": "#3fb950", "warn": "#d29922", "bad": "#ff7b72",
        "field_ro": "#23272e", "hover": "#30353e", "tab_hover": "#2c3139", "disabled": "#848c99",
        "alt_row": "#2b3038", "header": "#2e333c", "tooltip": "#0d1117",
        "term_bg": "#14171c", "term_fg": "#e6e9ef", "term_border": "#2c3244", "term_sel": "#3b4a6b",
        "btn": "#343a44", "btn_border": "#4d5562", "btn_hover": "#3d4757", "btn_press": "#2a3442"}
# Button roles, the Tk app's PALETTE: (fill, hover, pressed, text)
ROLES = {"start": ("#2e9e4f", "#268043", "#1f6b37", "#ffffff"),     # go / start / save
         "warn": ("#e8a317", "#d4920f", "#b97f0c", "#412402"),      # pause / stop motion / params
         "danger": ("#d9534f", "#c9302c", "#a92825", "#ffffff"),    # abort / delete / disconnect
         "move": ("#2f86c9", "#2670ab", "#1f5d8f", "#ffffff"),      # move / continue / edit
         "accent": ("#007acc", "#006bb3", "#005a96", "#ffffff"),    # read / refresh
         "info": ("#17a2b8", "#138496", "#10707f", "#ffffff"),
         "neutral": ("#6c757d", "#5a6268", "#4b5258", "#ffffff"),   # reset / secondary
         "caen": ("#0a84ff", "#0873e0", "#0662bf", "#ffffff"),     # DAQ backends, as the Tk pre-flight accents
         "hk": ("#8e44ad", "#7d3c98", "#6c3483", "#ffffff")}
# Scan / pipeline states, one palette everywhere: (fill, text, border)
STATE = {"wait": ("#e9ecef", "#6b7280", "#d0d5dc"),
         "move": ("#2f86c9", "#ffffff", "#1f5d8f"),
         "active": ("#f0ad4e", "#1f2430", "#c98a1c"),     # taking data or processing
         "done": ("#28a745", "#ffffff", "#1e7e34"),
         "error": ("#dc3545", "#ffffff", "#a71d2a")}
DAQ_SOFT = {"caen": ("#e6f2ff", "#1d3350"), "hk": ("#f3e9f8", "#3a2647")}   # card fill (light, dark)


def daq_color(backend):
    """(strong, soft) colour of a DAQ backend ("caen" / "hk")."""
    key = "hk" if backend == "hk" else "caen"
    return ROLES[key][0], DAQ_SOFT[key][1 if DARK_MODE else 0]
# Default role by button label, so every page follows the same colour rules; theme.role() overrides.
AUTO_ROLES = {
    "start": ("start run", "start stability run", "save", "save sequence", "save parameters",
              "save settings & compile", "apply", "apply currents", "add schedule", "connect", "connect ups",
              "ld on", "load", "load this", "use this count", "run backup now"),
    "info": ("save note",),
    "warn": ("pause", "params", "test alarm (instant)", "cancel shutdown"),
    "danger": ("stop", "delete", "disconnect", "disconnect ups", "re-run / abort scan", "cancel selected",
               "execute system wide shutdown"),
    "move": ("continue", "move tilt", "move rot", "edit", "hk digitizer config", "tec on"),
    "accent": ("get current", "refresh", "refresh all", "refresh history", "refresh status", "check connection",
               "run diagnostics now", "test connection", "sync from hk pc", "read touch state"),
    "neutral": ("reset", "cancel edit", "ld off", "tec off", "open global config (paths)", "off / reset"),
}
_AUTO = {label: kind for kind, labels in AUTO_ROLES.items() for label in labels}
C = dict(LIGHT)      # current palette; pages read it when they build or refresh
DARK_MODE = False

UI_FONT, MONO_FONT = "Inter", "JetBrains Mono"
# Type scale (pt): page title, section title, body, value, small
PT = {"title": 14, "section": 11, "body": 10.5, "value": 11, "small": 9.5, "mono": 10}
CONTROL_H = 30          # px, inputs + buttons
SPACE = 12              # px, page margins and gaps between cards


def load_fonts():
    """Register the bundled fonts; fall back to the system sans/mono if they are missing."""
    global UI_FONT, MONO_FONT
    folder = os.path.join(os.path.dirname(__file__), "fonts")
    fams = set()
    for f in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        if f.endswith(".ttf"):
            fams.update(QFontDatabase.applicationFontFamilies(QFontDatabase.addApplicationFont(os.path.join(folder, f))))
    UI_FONT = "Inter" if "Inter" in fams else "Noto Sans"
    MONO_FONT = "JetBrains Mono" if "JetBrains Mono" in fams else "DejaVu Sans Mono"


def set_dark(on):
    global DARK_MODE
    DARK_MODE = bool(on)
    C.clear()
    C.update(DARK if DARK_MODE else LIGHT)


def palette():
    """Qt palette from C: Fusion draws radio/check indicators and progress troughs from it."""
    pal = QPalette()
    roles = {QPalette.Window: "bg", QPalette.WindowText: "text", QPalette.Base: "card",
             QPalette.AlternateBase: "alt_row", QPalette.Text: "text", QPalette.Button: "card",
             QPalette.ButtonText: "text", QPalette.Highlight: "accent", QPalette.HighlightedText: "on_accent",
             QPalette.ToolTipBase: "tooltip", QPalette.ToolTipText: "term_fg", QPalette.PlaceholderText: "hint",
             QPalette.Light: "hover", QPalette.Midlight: "line", QPalette.Mid: "border", QPalette.Dark: "border",
             QPalette.Shadow: "border", QPalette.Link: "accent"}
    for role, key in roles.items():
        pal.setColor(role, QColor(C[key]))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, QColor(C["disabled"]))
    return pal


def role(btn, kind):
    """Give a button one of the ROLES colours (None: the plain button)."""
    if btn.property("role") == kind:
        return
    btn.setProperty("role", kind)
    btn.style().unpolish(btn)
    btn.style().polish(btn)


REFRESHING = "Refreshing..."


def _refresh_pressed(btn):
    """Refresh buttons say "Refreshing..." from the press until the work behind them has returned."""
    if btn.text() != REFRESHING:
        btn.setProperty("_label", btn.text())
    btn.setMinimumWidth(btn.sizeHint().width())
    btn.setText(REFRESHING)
    btn.repaint()


def _refresh_released(btn):
    def restore():
        try:
            if btn.text() == REFRESHING:
                btn.setText(btn.property("_label"))
        except RuntimeError:      # the page was closed meanwhile
            pass
    QTimer.singleShot(600, restore)   # runs after the clicked handlers have returned


class _HandCursor(QObject):
    """Every push button shows the pointing hand, and gets its AUTO_ROLES colour unless a page set one."""

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Polish and isinstance(obj, QPushButton):
            obj.setCursor(Qt.PointingHandCursor)
            if obj.text().lower().startswith("refresh") and not obj.property("_refresh_hook"):
                obj.setProperty("_refresh_hook", True)
                obj.pressed.connect(lambda b=obj: _refresh_pressed(b))
                obj.released.connect(lambda b=obj: _refresh_released(b))
            if obj.property("role") is None:
                kind = _AUTO.get(obj.text().replace("&&", "&").strip().lower())
                obj.setProperty("role", kind or "")
                if kind:
                    obj.style().polish(obj)
        return False


_HAND = _HandCursor()


def restyle(app):
    app.setPalette(palette())
    app.setStyleSheet(style())


def apply(app, dark=None):
    if dark is not None:
        set_dark(dark)
    load_fonts()
    f = QFont(UI_FONT)
    f.setPointSizeF(PT["body"])
    app.setFont(f)
    app.installEventFilter(_HAND)
    restyle(app)
    _matplotlib_fonts()


def _matplotlib_fonts():
    try:
        from matplotlib import font_manager, rcParams
    except ImportError:
        return
    folder = os.path.join(os.path.dirname(__file__), "fonts")
    for f in ("Inter-Regular.ttf", "Inter-SemiBold.ttf", "JetBrainsMono-Regular.ttf"):
        path = os.path.join(folder, f)
        if os.path.exists(path):
            font_manager.fontManager.addfont(path)
    rcParams["font.family"] = [UI_FONT, "DejaVu Sans"]
    rcParams["font.monospace"] = [MONO_FONT, "DejaVu Sans Mono"]
    rcParams["axes.titleweight"] = "semibold"
    rcParams["text.color"] = rcParams["axes.labelcolor"] = LIGHT["text"]   # plots stay on light paper
    rcParams["xtick.color"] = rcParams["ytick.color"] = LIGHT["muted"]


def _role_rules(pad_v):
    rules = []
    for kind, (fill, hover, press, text) in ROLES.items():
        sel = f'QPushButton[role="{kind}"]'
        rules.append(f"{sel} {{ background: {fill}; color: {text}; border: 1px solid {press}; border-bottom-width: 2px; }}\n"
                     f"{sel}:hover {{ background: {hover}; border-color: {press}; }}\n"
                     f"{sel}:pressed {{ background: {press}; border-bottom-width: 1px; padding-top: {pad_v}px; }}\n"
                     f"{sel}:disabled {{ background: {C['field_ro']}; color: {C['disabled']}; border-color: {C['border']};"
                     " border-bottom-width: 1px; }")
    return "\n".join(rules)


def style():
    pad_v = (CONTROL_H - 20) // 2
    return f"""
QWidget {{ color: {C['text']}; font-family: "{UI_FONT}"; font-size: {PT['body']}pt; }}
QMainWindow, QSplitter, QTabWidget > QWidget {{ background: {C['bg']}; }}

/* Tabs: three levels, each one step smaller, with a thin divider between tabs */
QTabWidget::pane {{ border: none; border-top: 1px solid {C['border']}; background: {C['bg']}; top: -1px; }}
QTabBar {{ font-weight: 500; }}
QTabWidget#mainTabs > QTabBar {{ font-size: 11pt; font-weight: 600; }}
QTabWidget#gsTabs > QTabBar {{ font-size: 10pt; }}
QTabBar::tab {{ background: transparent; color: {C['muted']}; padding: 6px 14px;
    border: none; border-right: 1px solid {C['border']}; border-bottom: 2px solid transparent; }}
QTabBar::tab:last {{ border-right: none; }}
QTabBar::tab:!selected:hover {{ color: {C['text']}; background: {C['tab_hover']}; }}
QTabBar::tab:selected {{ color: {C['accent']}; border-bottom: 2px solid {C['accent']}; }}
QTabWidget#mainTabs > QTabBar::tab {{ padding: 9px 16px; border-bottom: none;
    border-top-left-radius: 6px; border-top-right-radius: 6px; }}
QTabWidget#mainTabs > QTabBar::tab:selected {{ background: {C['accent']}; color: {C['on_accent']}; border-right-color: {C['accent']}; }}
QTabWidget#daqTabs > QTabBar::tab {{ padding: 7px 16px; }}
QTabWidget#gsTabs > QTabBar::tab {{ padding: 4px 14px; margin: 6px 0; border-bottom: none; }}
QTabWidget#gsTabs > QTabBar::tab:selected {{ background: {C['accent_soft']}; color: {C['accent']};
    border-radius: 12px; }}
QTabWidget#gsTabs::pane {{ border-top: none; }}

/* Cards */
QGroupBox {{ background: {C['card']}; border: 1px solid {C['border']}; border-radius: 8px;
    margin-top: 22px; padding: 10px 12px 12px 12px; font-size: {PT['section']}pt; font-weight: 600; }}
QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; left: 2px; top: 0px;
    padding: 0 0 4px 0; color: {C['text']}; }}
QGroupBox QLabel {{ font-size: {PT['body']}pt; font-weight: 400; }}

/* Inputs and buttons share one height */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{ background: {C['card']}; border: 1px solid {C['border']};
    border-radius: 5px; padding: {pad_v - 1}px 8px; min-height: 20px; }}
QLineEdit:read-only {{ background: {C['field_ro']}; }}
QLineEdit:focus, QComboBox:focus {{ border-color: {C['accent']}; }}
QPushButton {{ background: {C['btn']}; border: 1px solid {C['btn_border']}; border-bottom-width: 2px;
    border-radius: 5px; padding: {pad_v - 1}px 14px; min-height: 20px; font-weight: 600; }}
QPushButton:hover {{ background: {C['btn_hover']}; border-color: {C['accent']}; }}
QPushButton:pressed {{ background: {C['btn_press']}; border-bottom-width: 1px; padding-top: {pad_v}px; }}
QPushButton:disabled {{ color: {C['disabled']}; background: {C['field_ro']}; border-color: {C['border']};
    border-bottom-width: 1px; }}
{_role_rules(pad_v)}QPushButton[variant="large"] {{ min-height: 34px; font-size: 12pt; font-weight: 600; }}

/* Lists and tables */
QTreeWidget, QTableWidget, QListWidget, QTreeView, QTableView {{ background: {C['card']};
    border: 1px solid {C['border']}; border-radius: 6px; alternate-background-color: {C['alt_row']}; }}
QTreeView::item, QTableView::item {{ padding: 4px 2px; }}
QTreeView::item:selected, QTableView::item:selected, QListWidget::item:selected {{
    background: {C['accent']}; color: {C['on_accent']}; }}
QHeaderView::section {{ background: {C['header']}; color: {C['muted']}; border: none;
    border-bottom: 1px solid {C['border']}; padding: 6px 8px; font-weight: 600; }}

/* Text and terminals */
QPlainTextEdit, QTextEdit {{ background: {C['card']}; border: 1px solid {C['border']}; border-radius: 6px;
    font-family: "{MONO_FONT}"; font-size: {PT['mono']}pt; }}
QPlainTextEdit#dark {{ background: {C['term_bg']}; color: {C['term_fg']}; border: 1px solid {C['term_border']};
    border-radius: 8px; padding: 10px 12px; selection-background-color: {C['term_sel']}; }}

/* Label roles */
QLabel#pageTitle {{ font-size: {PT['title']}pt; font-weight: 700; }}
QLabel#sub, QLabel#hint {{ color: {C['hint']}; font-size: {PT['small']}pt; font-weight: 400; }}
QLabel#key {{ color: {C['muted']}; font-weight: 400; }}
QLabel#value {{ font-size: {PT['value']}pt; font-weight: 600; color: {C['text']}; }}
/* Sidebar: same type, compact density */
QScrollArea#sidebar, QWidget#sidebarBody {{ background: {C['bg']}; border: none; }}
QScrollArea#sidebar QGroupBox {{ margin-top: 20px; padding: 6px 8px 8px 8px; }}
QScrollArea#sidebar QPushButton {{ padding: 5px 8px; min-height: 20px; }}
QScrollArea#sidebar QRadioButton {{ min-height: 20px; }}
QMenuBar {{ background: {C['card']}; border-bottom: 1px solid {C['border']}; padding: 2px 6px; }}
QMenuBar::item {{ padding: 4px 10px; background: transparent; }}
QMenuBar::item:selected {{ background: {C['tab_hover']}; border-radius: 4px; }}
QMenu {{ background: {C['card']}; border: 1px solid {C['border']}; padding: 4px; }}
QMenu::item {{ padding: 6px 22px 6px 22px; border-radius: 4px; }}
QMenu::item:selected {{ background: {C['accent']}; color: {C['on_accent']}; }}
QMenu::separator {{ height: 1px; background: {C['border']}; margin: 4px 6px; }}
QCheckBox, QRadioButton {{ background: transparent; spacing: 7px; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 14px; height: 14px; background: {C['card']};
    border: 1px solid {C['hint']}; }}
QCheckBox::indicator {{ border-radius: 3px; }}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:checked {{ background: {C['accent']}; border-color: {C['accent']}; }}
QRadioButton::indicator:checked {{ border-color: {C['accent']}; background: qradialgradient(cx: 0.5, cy: 0.5,
    radius: 0.5, fx: 0.5, fy: 0.5, stop: 0 {C['accent']}, stop: 0.55 {C['accent']}, stop: 0.6 {C['card']},
    stop: 1 {C['card']}); }}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{ border-color: {C['disabled']};
    background: {C['field_ro']}; }}
QCheckBox::indicator:checked:disabled {{ background: {C['disabled']}; }}
QRadioButton::indicator:checked:disabled {{ background: qradialgradient(cx: 0.5, cy: 0.5, radius: 0.5, fx: 0.5,
    fy: 0.5, stop: 0 {C['disabled']}, stop: 0.55 {C['disabled']}, stop: 0.6 {C['field_ro']},
    stop: 1 {C['field_ro']}); }}
QScrollBar:vertical {{ background: transparent; width: 12px; }}
QScrollBar::handle:vertical {{ background: {C['border']}; border-radius: 5px; min-height: 30px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 12px; }}
QScrollBar::handle:horizontal {{ background: {C['border']}; border-radius: 5px; min-width: 30px; margin: 2px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QToolTip {{ background: {C['tooltip']}; color: #ffffff; border: none; padding: 6px 8px; }}
"""


STYLE = style()   # without bundled fonts; apply() is the full version

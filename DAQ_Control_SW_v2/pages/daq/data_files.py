"""DAQ System > Data Files, mirroring ui_manager._create_data_viewer: Raw /
Production / Result / External Disk sub-tabs (filter, search, sort, refresh),
File Info on the right, Move (rsync to a chosen folder) and Delete of the
selected files as main.move_data_files / delete_data_files (core.file_ops).

Double-click opens the file the same way the Tk app does (ROOT in a terminal
with a TBrowser, CSV in gedit). Listing, RunInfo reading, moving and deleting
run in worker threads; results come back through Qt signals.
"""
import csv
import os
import re
import shlex
import subprocess
import threading
from datetime import datetime

from PyQt5 import sip
from PyQt5.QtCore import QObject, Qt, pyqtSignal
from PyQt5.QtWidgets import (QAbstractItemView, QButtonGroup, QFileDialog, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                             QMessageBox, QPushButton, QRadioButton, QSplitter, QTabWidget, QTreeWidget, QTreeWidgetItem,
                             QVBoxLayout, QWidget)

from app.spinner import Spinner
from core import env, file_ops
from core import images, run_info

TYPES = ["Raw", "Production", "Result", "External Disk", "Routine", "Scan"]
SUMMARY_DIRS = {"Routine": "Routine", "Scan": "Scan"}   # ADC_DIR/Data/<dir>, written by routine_summary.py
PATH_KEY = {"Raw": "RawDataPath", "Production": "ProcessedDataPath", "Result": "FinalResultPath",
            "External Disk": "ExternalPath"}
MOVE_TEXT, DELETE_TEXT = "Move Selected File(s)", "Delete Selected File(s)"
RUN_RE = re.compile(r"_(\d{8})_(\d{3,4})\.root$")
_registry = {"mtime": None, "rows": {}}


def registry():
    """Rows keyed by (date, run): run_registry.csv, with Data/runinfo_index.csv (RunInfo of every
    production file, index_runinfo.C) filling the runs the registry does not have."""
    paths = [os.path.join(env.ADC_DIR, "Data", "runinfo_index.csv"), os.path.join(env.ADC_DIR, "Data", "run_registry.csv")]
    mtimes = []
    for p in paths:
        try:
            mtimes.append(os.path.getmtime(p))
        except OSError:
            mtimes.append(None)
    if _registry["mtime"] != mtimes:
        rows = {}
        for p in paths:   # the registry comes last, so it wins
            try:
                with open(p, newline="", encoding="utf-8", errors="replace") as f:
                    for r in csv.DictReader(f):
                        rows[(r.get("date", ""), (r.get("run", "") or "").lstrip("0") or "0")] = r
            except OSError:
                continue
        _registry.update(mtime=mtimes, rows=rows)
    return _registry["rows"]


def run_text(filename):
    """'Mon / Rot1 / Rot2  405 nm' of a run file from the registry, '' if unknown."""
    m = RUN_RE.search(filename)
    if not m:
        return ""
    r = registry().get((m.group(1), m.group(2).lstrip("0") or "0"))
    if not r:
        return ""
    def pmt(k, d):   # serial with its cable direction, e.g. EM2740 (A)
        sn, dr = r.get(k) or "-", r.get(d) or ""
        return f"{sn} ({dr})" if dr else sn
    pmts = " / ".join(pmt(k, d) for k, d in (("sn1", "dir1"), ("sn2", "dir2"), ("sn3", "dir3")))
    if (r.get("mode") or "").lower() == "dark":   # LD off: the wavelength field is only the last laser setting
        return f"{pmts}   dark"
    wl = r.get("wavelength") or ""
    return f"{pmts}   {wl} nm" if wl else pmts
INSTANCE = None   # the live DataFilesPage, so other pages (Waveform) can read its selection
SELECTION_LISTENERS = []   # fn(paths), called when the selection or the data tab changes


def live():
    """The page, or None if not built yet or deleted by a window rebuild."""
    return INSTANCE if INSTANCE is not None and not sip.isdeleted(INSTANCE) else None


def _notify():
    page = live()
    paths = page.selected_paths() if page is not None else []
    for fn in list(SELECTION_LISTENERS):
        try:
            fn(paths)
        except RuntimeError:   # its widget was deleted (window rebuilt)
            SELECTION_LISTENERS.remove(fn)


def list_files(cfg):
    """Same scan as main.get_data_files: Raw/External look in Dark/ and Laser/."""
    out = []
    for ftype in TYPES:
        base = (cfg.get(PATH_KEY[ftype]) if ftype in PATH_KEY
                else os.path.join(env.ADC_DIR, "Data", SUMMARY_DIRS[ftype]))
        if not (base and os.path.isdir(base)):
            continue
        dirs = [os.path.join(base, "Dark"), os.path.join(base, "Laser")] if ftype in ("Raw", "External Disk") else [base]
        for d in dirs:
            if not os.path.isdir(d):
                continue
            try:
                names = os.listdir(d)
            except OSError:
                continue
            for f in names:
                if not f.lower().endswith((".root", ".csv")):
                    continue
                try:
                    mt = os.path.getmtime(os.path.join(d, f))
                except OSError:   # file vanished mid-listing (e.g. backup sync)
                    continue
                out.append({"type": ftype, "filename": f, "path": d, "mtime_float": mt, "run": run_text(f) or run_info.summary_text(f),
                            "mtime": datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M:%S")})
    return out


def read_runinfo(path):
    """Same as ui_manager._read_runinfo: dump_runinfo.C through ROOT."""
    macro = os.path.join(env.ADC_DIR, "dump_runinfo.C")
    if not os.path.exists(macro):
        return "(run info: dump_runinfo.C not found)", False
    try:
        res = subprocess.run(["root", "-l", "-b", "-q", f'{macro}("{path}")'],
                             capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return "(run info: ROOT timed out)", False
    except FileNotFoundError:
        return "(run info: 'root' not found)", False
    kv = {}
    for line in res.stdout.splitlines():
        if "=" in line and not line.startswith("Processing"):
            k, _, v = line.partition("=")
            kv[k.strip()] = v.strip()
    if "ERR" in kv:
        return f"(run info unavailable: {kv['ERR']})", False
    if "SN1" not in kv and "RunMode" not in kv:
        return "(no RunInfo in this file)", True
    g = kv.get
    return ("Run Info\n"
            f"Mode: {g('RunMode', '?')}\n"
            f"Shifter: {g('Shifter', '?')}  /  Expert: {g('Expert', '?')}\n"
            f"Laser: {g('Laser_mA', '?')} mA @ {g('Wavelength', '?')} nm\n"
            f"Mon.: {g('SN1', '?')}  HV {g('HV1', '?')}  Dir {g('Direction1', '?')}\n"
            f"Rot#1: {g('SN2', '?')}  HV {g('HV2', '?')}  Dir {g('Direction2', '?')}\n"
            f"     Rot {g('Rot2', '?')}°, Tilt {g('Tilt2', '?')}°\n"
            f"Rot#2: {g('SN3', '?')}  HV {g('HV3', '?')}  Dir {g('Direction3', '?')}\n"
            f"     Rot {g('Rot3', '?')}°, Tilt {g('Tilt3', '?')}°\n"
            f"Note: {g('NOTE', '')}"), True


class _Bus(QObject):
    listed = pyqtSignal(list)
    info = pyqtSignal(str, str)
    progress = pyqtSignal(str)
    moved = pyqtSignal(int, list)
    deleted = pyqtSignal(int, list)


class BrowserTab(QWidget):
    def __init__(self, ftype, page):
        super().__init__()
        self.ftype, self.page = ftype, page
        self.order = (3, Qt.DescendingOrder)   # column, order: the Sort By buttons and header clicks
        lay = QVBoxLayout(self)
        ctl = QHBoxLayout()

        fbox = QGroupBox("Filter Mode")
        fl = QHBoxLayout(fbox)
        self.filter = QButtonGroup(self)
        for i, t in enumerate(["All", "Dark", "Laser"]):
            rb = QRadioButton(t)
            rb.setChecked(i == 0)
            rb.toggled.connect(lambda on: on and self.populate())
            self.filter.addButton(rb)
            fl.addWidget(rb)
        ctl.addWidget(fbox)

        sbox = QGroupBox("Search Files")
        sl = QHBoxLayout(sbox)
        self.search = QLineEdit()
        self.search.setPlaceholderText("file name, run, PMT serial, wavelength")
        self.search.textChanged.connect(self.populate)
        sl.addWidget(self.search)
        ctl.addWidget(sbox, 1)

        obox = QGroupBox("Sort By")
        ol = QHBoxLayout(obox)
        for t, order in [("Name (A-Z)", (0, Qt.AscendingOrder)), ("Time (Newest)", (3, Qt.DescendingOrder))]:
            b = QPushButton(t)
            b.clicked.connect(lambda _=False, o=order: self._set_sort(o))
            ol.addWidget(b)
        ctl.addWidget(obox)

        ref = QPushButton("Refresh")
        ref.clicked.connect(page.reload)
        ctl.addWidget(ref, 0, Qt.AlignBottom)
        info = QPushButton("Run Numbers")
        info.clicked.connect(self._run_info)
        ctl.addWidget(info, 0, Qt.AlignBottom)
        lay.addLayout(ctl)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["File Name", "Contents" if ftype in SUMMARY_DIRS else
                                   "PMTs (Mon / Rot1 / Rot2) (cable)   nm", "Directory Path", "Last Modified"])
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setColumnWidth(0, 330)
        self.tree.setColumnWidth(1, 300)
        self.tree.setColumnWidth(2, 260)
        self.tree.setSortingEnabled(True)
        self.tree.header().setSortIndicatorShown(True)
        self.tree.header().sortIndicatorChanged.connect(lambda c, o: setattr(self, "order", (c, o)))
        self.tree.itemSelectionChanged.connect(self._selected)
        self.tree.itemDoubleClicked.connect(lambda it, _c: self.page.open_file(self._path(it)))
        lay.addWidget(self.tree, 1)

    def _set_sort(self, order):
        self.order = order
        self.tree.sortItems(*order)

    @staticmethod
    def _path(item):
        return os.path.join(item.text(2), item.text(0))

    def populate(self, *_):
        files = [f for f in self.page.files if f["type"] == self.ftype]
        mode = self.filter.checkedButton().text()
        if mode != "All":
            m = mode.lower()
            files = [f for f in files if os.sep + m in f["path"].lower() or f"_{m}" in f["filename"].lower()]
        q = self.search.text()
        if q.strip():
            files = [f for f in files if images.matches(q, f["filename"], f["path"], f.get("run", ""))]
        self.tree.setUpdatesEnabled(False)
        self.tree.setSortingEnabled(False)
        self.tree.clear()
        self.tree.addTopLevelItems([QTreeWidgetItem([f["filename"], f.get("run", ""), f["path"], f["mtime"]])
                                    for f in files])
        self.tree.setSortingEnabled(True)
        self.tree.sortItems(*self.order)
        self.tree.setUpdatesEnabled(True)

    def _selected(self):
        items = self.tree.selectedItems()
        if items:
            self.page.show_info(self._path(items[0]))
            from widgets.run_info import follow
            follow(self.page, self._path(items[0]))
        _notify()

    def _run_info(self):
        from widgets.run_info import open_for
        items = self.tree.selectedItems()
        open_for(self.page, self._path(items[0]) if items else None)


class DataFilesPage(QWidget):
    def __init__(self):
        super().__init__()
        global INSTANCE
        INSTANCE = self
        self.files = []
        self._cache = {}
        self._current = None
        self.bus = _Bus()
        self.bus.listed.connect(self._on_listed)
        self.bus.info.connect(self._on_info)
        self.bus.progress.connect(self._on_progress)
        self.bus.moved.connect(self._on_moved)
        self.bus.deleted.connect(self._on_deleted)

        lay = QVBoxLayout(self)
        split = QSplitter(Qt.Horizontal)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.browsers = []
        for t in TYPES:
            b = BrowserTab(t, self)
            self.browsers.append(b)
            self.tabs.addTab(b, f"{t} Data")
        self.tabs.currentChanged.connect(lambda _i: _notify())
        ll.addWidget(self.tabs, 1)
        self.move_btn = QPushButton(MOVE_TEXT)
        self.move_btn.clicked.connect(self.move_selected)
        ll.addWidget(self.move_btn)
        self.delete_btn = QPushButton(DELETE_TEXT)
        self.delete_btn.clicked.connect(self.delete_selected)
        ll.addWidget(self.delete_btn)
        split.addWidget(left)

        info = QGroupBox("File Info")
        il = QVBoxLayout(info)
        self.info = QLabel("")
        self.info.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.info.setWordWrap(True)
        self.info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.info.setStyleSheet("font-weight: normal;")
        il.addWidget(self.info, 1)
        split.addWidget(info)
        info.setMaximumWidth(340)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        split.setSizes([900, 280])
        lay.addWidget(split, 1)
        self.reload()

    def selected_paths(self):
        b = self.tabs.currentWidget()
        return [b._path(it) for it in b.tree.selectedItems()] if b is not None else []

    def reload(self):
        from core.env import read_config
        cfg = read_config()
        if not hasattr(self, "list_spin"):
            self.list_spin = Spinner(self.tabs)
        self.list_spin.start()
        threading.Thread(target=lambda: self.bus.listed.emit(list_files(cfg)), daemon=True).start()

    def _on_listed(self, files):
        self.list_spin.stop()
        self.files = files
        for b in self.browsers:
            b.populate()

    def show_info(self, path):
        try:
            st = os.stat(path)
        except OSError as e:
            self.info.setText(f"Could not get file info:\n{e}")
            return
        base = (f"File: {os.path.basename(path)}\n\nSize: {st.st_size / 1048576:.2f} MB\n"
                f"Modified: {datetime.fromtimestamp(st.st_mtime):%Y-%m-%d %H:%M:%S}\n")
        summary = run_info.summary_text(path)
        m = None if summary else re.search(r"(\d+)(?=[^\d]*\.root$)", os.path.basename(path))
        if m:
            base += f"Run number: {int(m.group(1))}\n"
        if summary:
            base += f"{summary}\n"
        self._current = (path, base)
        if summary:
            self.info.setText(base)
            return
        if not path.lower().endswith(".root"):
            self.info.setText(base)
            return
        key = (path, st.st_mtime)
        if key in self._cache:
            self.info.setText(base + "\n" + self._cache[key])
            return
        self.info.setText(base)
        if not hasattr(self, "info_spin"):
            self.info_spin = Spinner(self.info, 40)
        self.info_spin.start()

        def work():
            text, ok = read_runinfo(path)
            if ok:   # failures are not cached, so a retry can succeed
                self._cache[key] = text
            self.bus.info.emit(path, text)
        threading.Thread(target=work, daemon=True).start()

    def _on_info(self, path, text):
        if self._current and self._current[0] == path:
            self.info_spin.stop()
            self.info.setText(self._current[1] + "\n" + text)

    # ---------------------------------------------------------------- move / delete
    def _refuse_running(self, paths, action):
        """True (and a message) when a selected run is still being taken."""
        from core import status
        live = file_ops.started_runs(paths) if status.busy_reason() else []
        if live:
            QMessageBox.warning(self, f"Cannot {action}", "These runs are still being taken:\n\n"
                                + "\n".join(os.path.basename(p) for p in live[:10]))
        return bool(live)

    def move_selected(self):
        paths = self.selected_paths()
        if not paths:
            QMessageBox.warning(self, "No Selection", "Please select file(s) to move.")
            return
        if self._refuse_running(paths, "move"):
            return
        dest = QFileDialog.getExistingDirectory(self, "Select Destination Folder")
        if not dest:
            return
        names = "\n".join(os.path.basename(p) for p in paths[:5]) + (f"\n...and {len(paths) - 5} more" if len(paths) > 5 else "")
        if QMessageBox.question(self, "Confirm Move", f"Move {len(paths)} file(s) to\n{dest}?\n\n{names}\n\n"
                                "They are removed from the current folder after copying.",
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self.move_btn.setEnabled(False)
        self.bus.progress.emit("Preparing rsync...")

        def work():
            moved, failed = file_ops.move_files(paths, dest, progress=self.bus.progress.emit)
            self.bus.moved.emit(moved, failed)
        threading.Thread(target=work, daemon=True).start()

    def _on_progress(self, msg):
        self.move_btn.setText(msg.split(":")[0])   # file name in the tooltip keeps the button width
        self.move_btn.setToolTip(msg)

    def _on_moved(self, moved, failed):
        self.move_btn.setToolTip("")
        self.move_btn.setText(MOVE_TEXT)
        self.move_btn.setEnabled(True)
        self.reload()
        if failed:
            QMessageBox.critical(self, "Rsync Finished with Errors",
                                 f"Moved {moved} files.\nFailed:\n{', '.join(failed)}\n\nCheck logs for details.")
        else:
            QMessageBox.information(self, "Success", f"Successfully moved {moved} file(s) using rsync.")

    def delete_selected(self):
        paths = self.selected_paths()
        if not paths:
            QMessageBox.warning(self, "No Selection", "Please select one or more files to delete.")
            return
        if self._refuse_running(paths, "delete"):
            return
        if QMessageBox.question(self, "Confirm Deletion", file_ops.delete_question(paths)) != QMessageBox.Yes:
            file_ops.log("[INFO] User cancelled file deletion.")
            return
        self.delete_btn.setEnabled(False)
        self.delete_btn.setText(f"Deleting {len(paths)} file(s)...")

        def work():
            deleted, failed = file_ops.delete_files(paths)
            self.bus.deleted.emit(deleted, failed)
        threading.Thread(target=work, daemon=True).start()

    def _on_deleted(self, deleted, failed):
        self.delete_btn.setText(DELETE_TEXT)
        self.delete_btn.setEnabled(True)
        if deleted > 0:
            self.reload()
        if failed:
            QMessageBox.critical(self, "Deletion Error", f"Successfully deleted {deleted} file(s), but failed to delete:"
                                                         f"\n\n{', '.join(failed)}")
        elif deleted > 0:
            QMessageBox.information(self, "Success", f"Successfully deleted {deleted} file(s).")

    @staticmethod
    def open_file(path):
        if path.lower().endswith(".csv"):
            subprocess.Popen(["gedit", path], start_new_session=True)
            return
        cmd = f'root -l {shlex.quote(path)} -e "new TBrowser"; exec bash'
        subprocess.Popen(["gnome-terminal", "--", "bash", "-c", cmd], start_new_session=True)

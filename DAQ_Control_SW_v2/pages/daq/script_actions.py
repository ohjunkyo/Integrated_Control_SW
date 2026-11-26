"""Execute Scripts actions for the left panel: build the command (core.scripts), ask for the
few parameters the Tk app asks for, and start the job (core.jobs) in the Output tab."""
import os
import re
import subprocess
from datetime import datetime, timedelta

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                             QFormLayout, QGridLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
                             QMessageBox, QPushButton, QRadioButton, QScrollArea, QTableWidget, QTableWidgetItem,
                             QVBoxLayout, QWidget)

from app import theme
from core import scripts, status
from core.jobs import JOBS

NEEDS_UNLOCK = {"open_config", "run_daq", "run_produce", "run_analysis", "run_contour", "run_uniformity", "run_stability",
                "run_overlay"}


def selection():
    from pages.daq import data_files
    page = data_files.live()
    return page.selected_paths() if page is not None else []


class _Form(QDialog):
    def __init__(self, parent, title, rows):
        """rows: [(key, label, widget)]"""
        super().__init__(parent)
        self.setWindowTitle(title)
        form = QFormLayout(self)
        self.w = {}
        for key, label, widget in rows:
            form.addRow(label, widget)
            self.w[key] = widget
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Run")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)


def _tag_box():
    box = QComboBox()
    box.setEditable(True)
    tags = scripts.date_tags()
    today = datetime.now().strftime("%Y%m%d")
    box.addItems([today] + [t for t in tags if t != today])
    return box


def _range_rows():
    return [("start", "Run start", QLineEdit("0")), ("end", "Run end", QLineEdit("99"))]


def _ints(dlg):
    try:
        return int(dlg.w["start"].text()), int(dlg.w["end"].text())
    except ValueError:
        return None


def _centered(w):
    box = QWidget()
    h = QHBoxLayout(box)
    h.setContentsMargins(0, 0, 0, 0)
    h.addWidget(w, 0, Qt.AlignCenter)
    return box


def _item(text, grey=False):
    it = QTableWidgetItem(text)
    it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
    if grey:
        it.setForeground(QColor(theme.C["hint"]))
    return it


def _channel_row(chans):
    row = QHBoxLayout()
    row.addWidget(QLabel("Channels"))
    boxes = []
    for c in (0, 1, 2):
        cb = QCheckBox(f"CH{c}")
        cb.setChecked(c in chans)
        boxes.append(cb)
        row.addWidget(cb)
    row.addStretch(1)
    return row, boxes


class UniformityDialog(QDialog):
    """Pick General-scan blocks of a date (run_registry.csv) for Draw_Uniformity_Norm_v7."""

    COLS = ["", "Runs", "nm", "Points", "Rot1 / Rot2", "Start", "Report"]

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Uniformity Analysis")
        self.resize(720, 460)
        self.reg, self.hist = scripts.registry_rows(), scripts.scan_history_runs()
        self.active = status.active_scan()
        self.blocks, self.checks = [], []
        v = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("Date"))
        self.date = _tag_box()
        self.date.setMinimumWidth(120)
        self.date.currentTextChanged.connect(self._load)
        top.addWidget(self.date)
        self.count = QLabel()
        self.count.setStyleSheet(f"color: {theme.C['hint']};")
        top.addWidget(self.count)
        top.addStretch(1)
        v.addLayout(top)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        # soft selection: a checked box on an accent-filled row is invisible
        self.table.setStyleSheet(f"QTableView::item:selected {{ background: {theme.C['accent_soft']}; "
                                 f"color: {theme.C['text']}; }}")
        hh = self.table.horizontalHeader()
        for c, w in ((0, 32), (1, 80), (2, 48), (3, 64), (5, 56), (6, 72)):
            hh.setSectionResizeMode(c, QHeaderView.Fixed)
            self.table.setColumnWidth(c, w)
        hh.setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.itemSelectionChanged.connect(self._sync)
        v.addWidget(self.table, 1)
        man = QHBoxLayout()
        self.manual = QCheckBox("Manual run range")
        self.start, self.end = QLineEdit("0"), QLineEdit("45")
        man.addWidget(self.manual)
        for lab, w in (("Run start", self.start), ("Run end", self.end)):
            w.setFixedWidth(64)
            man.addWidget(QLabel(lab))
            man.addWidget(w)
        man.addStretch(1)
        self.manual.toggled.connect(self._sync)
        v.addLayout(man)
        ch, self.chans = _channel_row((0, 1, 2))
        v.addLayout(ch)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.run_btn = bb.button(QDialogButtonBox.Ok)
        self.open_btn = bb.addButton("Open report", QDialogButtonBox.ActionRole)
        self.open_btn.clicked.connect(self._open)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self._load()

    def _load(self, *_):
        date = self.date.currentText().strip()
        self.blocks = scripts.uniformity_blocks(date, self.reg, self.hist, self.active)
        self.checks = []
        self.table.setRowCount(len(self.blocks))
        for r, b in enumerate(self.blocks):
            off = b["state"] in ("aborted", "running")
            cb = QCheckBox()
            cb.setEnabled(not off)
            cb.toggled.connect(self._sync)
            self.checks.append(cb)
            self.table.setCellWidget(r, 0, _centered(cb))
            cells = (f"{b['r0']:03d}-{b['r1']:03d}", b["nm"], f"{b['done']}/{b['total']}", b["pmts"], b["start"],
                     b["state"])
            for c, text in enumerate(cells, 1):
                self.table.setItem(r, c, _item(text, grey=off))
        n = len(self.blocks)
        self.count.setText(f"{n} block{'' if n == 1 else 's'} on this date")
        self.manual.setChecked(n == 0)
        self._sync()

    def _row(self):
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return self.blocks[rows[0].row()] if rows and rows[0].row() < len(self.blocks) else None

    def _sync(self, *_):
        manual = self.manual.isChecked()
        self.start.setEnabled(manual)
        self.end.setEnabled(manual)
        self.table.setEnabled(not manual)
        n = 1 if manual else sum(cb.isChecked() for cb in self.checks)
        self.run_btn.setText(f"Run selected ({n})")
        self.run_btn.setEnabled(n > 0)
        b = self._row()
        self.open_btn.setEnabled(not manual and b is not None and os.path.isfile(b["report"]))

    def _open(self):
        b = self._row()
        if b and os.path.isfile(b["report"]):
            subprocess.Popen(["xdg-open", b["report"]], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def date_tag(self):
        return self.date.currentText().strip()

    def channels(self):
        return [c for c, cb in enumerate(self.chans) if cb.isChecked()]

    def ranges(self):
        """[(start, end)] to run, None when the manual range is not two integers."""
        if self.manual.isChecked():
            try:
                return [(int(self.start.text()), int(self.end.text()))]
            except ValueError:
                return None
        return [(b["r0"], b["r1"]) for b, cb in zip(self.blocks, self.checks) if cb.isChecked()]


class OverlayDialog(QDialog):
    """Pick datasets for Draw_Overlay_Uniformity_v7: use / order # / Dev[%] reference / legend label."""

    PERIODS = (("Last 14 days", 14), ("Last 30 days", 30), ("All", None))
    WAVELENGTHS = ("All nm", "375", "405", "450", "473")

    def __init__(self, parent, tags):
        super().__init__(parent)
        self.setWindowTitle("Overlay Uniformity")
        self.resize(900, 640)
        self.tags = tags
        self.info = scripts.dataset_info(tags)
        sel, labels, ref, order, chans = scripts.overlay_presets()
        self.preset = set(sel)
        v = QVBoxLayout(self)
        top = QHBoxLayout()
        self.period = QComboBox()
        self.period.addItems([p for p, _ in self.PERIODS])
        self.period.currentIndexChanged.connect(self._apply)
        top.addWidget(self.period)
        self.wl = QComboBox()
        self.wl.addItems(self.WAVELENGTHS)
        self.wl.currentIndexChanged.connect(self._apply)
        top.addWidget(self.wl)
        top.addWidget(QLabel("Filter"))
        self.filter = QLineEdit()
        self.filter.textChanged.connect(self._apply)
        top.addWidget(self.filter, 1)
        self.sort = QCheckBox("Sort by order #")
        self.sort.toggled.connect(self._apply)
        top.addWidget(self.sort)
        v.addLayout(top)
        self.table = QTableWidget(len(tags), 7)
        self.table.setHorizontalHeaderLabels(["Use", "#", "Ref", "Dataset", "nm", "Rot1 / Rot2", "Label"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        hh = self.table.horizontalHeader()
        for c, w in ((0, 48), (1, 56), (2, 48), (3, 200), (4, 64), (5, 190)):
            hh.setSectionResizeMode(c, QHeaderView.Fixed)
            self.table.setColumnWidth(c, w)
        hh.setSectionResizeMode(6, QHeaderView.Stretch)
        self.refs = QButtonGroup(self)
        self.rows = {}
        for r, tag in enumerate(tags):
            use = QCheckBox()
            use.setChecked(tag in self.preset)
            num = QLineEdit(order.get(tag, ""))
            rb = QRadioButton()
            rb.setChecked(tag == ref)
            self.refs.addButton(rb)
            lab = QLineEdit(labels.get(tag, ""))
            _, nm, pmts = self.info[tag]
            self.table.setCellWidget(r, 0, _centered(use))
            self.table.setCellWidget(r, 1, num)
            self.table.setCellWidget(r, 2, _centered(rb))
            self.table.setCellWidget(r, 3, QLabel(tag))
            self.table.setCellWidget(r, 4, QLabel(nm))
            self.table.setCellWidget(r, 5, QLabel(pmts))
            self.table.setCellWidget(r, 6, lab)
            self.rows[tag] = (r, use, num, rb, lab)
        v.addWidget(self.table, 1)
        self.hidden = QLabel()
        self.hidden.setStyleSheet(f"color: {theme.C['hint']};")
        v.addWidget(self.hidden)
        ch, self.chans = _channel_row(chans)
        v.addLayout(ch)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Overlay")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self._apply()

    def order(self):
        if not self.sort.isChecked():
            return list(self.tags)

        def key(tag):
            try:
                return (0, float(self.rows[tag][2].text().strip()))
            except ValueError:
                return (1, self.tags.index(tag))
        return sorted(self.tags, key=key)

    def _newest_first(self):
        def key(tag):
            return (self.info[tag][0], [int(n) for n in re.findall(r"\d+", tag[8:])], tag)
        return sorted(self.tags, key=key, reverse=True)

    def _apply(self, *_):
        needle = self.filter.text().strip().lower()
        days = self.PERIODS[self.period.currentIndex()][1]
        cutoff = datetime.now() - timedelta(days=days) if days else None
        wl = self.wl.currentText() if self.wl.currentIndex() > 0 else ""
        older = 0
        shown = self.order() if self.sort.isChecked() else self._newest_first()
        for i, tag in enumerate(shown):
            r = self.rows[tag][0]
            self.table.verticalHeader().moveSection(self.table.verticalHeader().visualIndex(r), i)
            day, nm, pmts = self.info[tag]
            old = cutoff is not None and day < cutoff
            older += old and tag not in self.preset
            match = (not needle or any(needle in s.lower() for s in (tag, nm, pmts))) and (not wl or wl in nm)
            self.table.setRowHidden(r, not (tag in self.preset or (match and not old)))
        self.hidden.setText(f"{older} older dataset{'' if older == 1 else 's'} hidden" if older else "")

    def result_rows(self):
        out, ref = [], ""
        for tag in self.order():
            _, use, num, rb, lab = self.rows[tag]
            out.append((tag, use.isChecked(), lab.text(), num.text()))
            if rb.isChecked():
                ref = tag
        return out, ref, [c for c, cb in enumerate(self.chans) if cb.isChecked()]


class GlobalSettingsDialog(QDialog):
    """Every constant of the config header, two columns; protected keys need the admin password."""

    def __init__(self, parent, path):
        super().__init__(parent)
        from core import config
        self.setWindowTitle("Global Settings")
        self.resize(860, 720)
        self.path = path
        self.original = dict(config.items(path))
        v = QVBoxLayout(self)
        t = QLabel(path)
        t.setObjectName("key")
        v.addWidget(t)
        area = QScrollArea()
        area.setWidgetResizable(True)
        body = QWidget()
        g = QGridLayout(body)
        g.setHorizontalSpacing(8)
        g.setVerticalSpacing(6)
        self.entries, self.labels = {}, {}
        for i, (name, value) in enumerate(self.original.items()):
            row, col = divmod(i, 2)
            lab = QLabel(name)
            lab.setObjectName("key")
            g.addWidget(lab, row, col * 2, Qt.AlignRight | Qt.AlignVCenter)
            e = QLineEdit(value)
            e.setCursorPosition(0)
            e.setMinimumWidth(200)
            g.addWidget(e, row, col * 2 + 1)
            self.entries[name], self.labels[name] = e, lab
            self._lock(name, name in config.PROTECTED)
        if "TimeWindow" in self.entries and "PostTrigger" in self.entries:
            from widgets.record_layout import RecordHover
            keys = ("TimeWindow", "PostTrigger")
            self._record_hover = RecordHover([w for k in keys for w in (self.entries[k], self.labels[k])],
                                             self._record_params)
        g.setColumnStretch(1, 1)
        g.setColumnStretch(3, 1)
        area.setWidget(body)
        v.addWidget(area, 1)
        row = QHBoxLayout()
        self.admin = QPushButton("Admin Unlock")
        self.admin.clicked.connect(self._admin)
        row.addWidget(self.admin)
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        save = QPushButton("Save")
        save.setStyleSheet("QPushButton { background: #c92a2a; color: white; border: none; font-weight: 600; }")
        save.clicked.connect(self.accept)
        row.addWidget(save)
        v.addLayout(row)

    def _record_params(self):
        try:
            return int(self.entries["TimeWindow"].text()), int(self.entries["PostTrigger"].text()), "external"
        except ValueError:
            return None

    def _lock(self, name, locked):
        e, lab = self.entries[name], self.labels[name]
        e.setReadOnly(locked)
        e.setStyleSheet("QLineEdit { background: #2a2d33; color: #7c818c; border: 1px dashed #4a4f59; }" if locked else "")
        lab.setStyleSheet("color: #7c818c;" if locked else "")
        e.setToolTip("Admin only" if locked else "")

    def _admin(self):
        from core.access import check_password
        from core import config
        pwd, ok = QInputDialog.getText(self, "Admin Unlock", "Enter Admin Password:", QLineEdit.Password)
        if not ok:
            return
        if not check_password(pwd):
            QMessageBox.critical(self, "Admin Unlock", "Incorrect password.")
            return
        for name in config.PROTECTED:
            if name in self.entries:
                self._lock(name, False)
        self.admin.setText("Unlocked")
        self.admin.setEnabled(False)

    def changes(self):
        return {k: e.text().strip() for k, e in self.entries.items() if e.text().strip() != self.original[k]}


class Actions:
    def __init__(self, parent, show_output, show_page):
        self.parent, self.show_output, self.show_page = parent, show_output, show_page

    def _start(self, kind, name, command):
        slot, why = JOBS.start(kind, name, command)
        if slot is None:
            QMessageBox.information(self.parent, f"{name} Busy", why)
            return
        self.show_output(slot)

    def _with_selection(self, name):
        paths = selection()
        if not paths:
            QMessageBox.warning(self.parent, name, "Select files in the Data Files tab first.")
            self.show_page("Data Files")
        return paths

    def _build(self, name, fn, *args):
        try:
            return fn(*args)
        except scripts.ScriptError as e:
            QMessageBox.warning(self.parent, name, str(e))
            return None

    def run(self, command):
        getattr(self, command)()

    def open_config(self):
        from core import config, env
        path = env.config_file()
        try:
            dlg = GlobalSettingsDialog(self.parent, path)
        except OSError as e:
            QMessageBox.critical(self.parent, "Global Settings", f"Could not read {path}:\n{e}")
            return
        if dlg.exec_() != QDialog.Accepted:
            return
        changed = dlg.changes()
        if not changed:
            return
        if scripts.daq_running() or JOBS.running("daq"):
            QMessageBox.warning(self.parent, "Global Settings",
                                "A DAQ acquisition is running; saving would rebuild its binary. Try again after it ends.")
            return
        listing = "\n".join(f"{k}: {dlg.original[k]!r} -> {v!r}" for k, v in changed.items())
        if QMessageBox.question(self.parent, "Global Settings", f"Save these changes?\n\n{listing}") != QMessageBox.Yes:
            return
        try:
            config.save(path, changed)
        except OSError as e:
            QMessageBox.critical(self.parent, "Global Settings", f"Failed to save:\n{e}")
            return
        warning = config.rebuild_daq(env.read_config().get("BasePath") or env.ADC_DIR)
        if warning:
            QMessageBox.warning(self.parent, "DAQ Binary Rebuild Failed", warning)
        else:
            QMessageBox.information(self.parent, "Global Settings", "Saved and the DAQ binary was rebuilt.")
        if hasattr(self.parent, "refresh"):
            self.parent.refresh()

    def run_daq(self):
        from core import status
        from core.access import ACCESS
        from core.stage import stage
        if not ACCESS.unlocked:
            return
        why = status.busy_reason()
        if why:
            QMessageBox.warning(self.parent, "Run DAQ", why)
            return
        from core import stability as stab
        if stab.hk_backend():
            from core import hk
            run_no = hk.config().get("run_number", 0)
            if QMessageBox.question(self.parent, "Run DAQ (HK Digitizer)",
                                    f"Start HK manual acquisition run #{run_no} at the current stage angle?\n\n"
                                    f"{hk.config().get('ssh_target', '')}") != QMessageBox.Yes:
                return
            slot, info = hk.manual_acquire()
            if slot is None:
                QMessageBox.information(self.parent, "HK Busy", info)
                return
            self.show_output(slot)
            return
        if scripts.daq_running() or JOBS.running("daq"):
            QMessageBox.warning(self.parent, "DAQ Already Running", "An execute_DAQ acquisition is already running.")
            return
        category, manual_type, fmt = self.parent.run_mode()
        mode = manual_type if category == "manual" else "laser"
        if mode == "dark" and QMessageBox.question(
                self.parent, "Dark DAQ", "Dark mode needs every laser LD OFF.\n\nAre all lasers OFF?") != QMessageBox.Yes:
            return
        if mode != "dark":
            from pages.laser_check import confirm_laser_config
            if not confirm_laser_config(self.parent):
                return
        angles = {d: stage().read_now(d) for d in (2, 3)}
        angles = {d: a for d, a in angles.items() if a[0] is not None}
        built = self._build("Run DAQ", scripts.daq, category, manual_type, fmt, angles)
        if not built:
            return
        command, run_env = built
        block = command.rsplit(" ", 1)[-1]
        from core import config, env
        cfg = config.parse(env.config_file())
        text = (f"Mode: {mode}   Run block: {block}\nFormat: {fmt}\n"
                f"Laser: {cfg.get('Wavelength', '?')} nm   Pulse {cfg.get('Laser', '?')} mA\n"
                f"Angles  SN2 rot/tilt {command.split()[3]}/{command.split()[4]}   "
                f"SN3 rot/tilt {command.split()[5]}/{command.split()[6]}\n\nStart the acquisition?")
        if QMessageBox.question(self.parent, "Run DAQ", text) != QMessageBox.Yes:
            return
        slot, why = JOBS.start("daq", "DAQ", command, env=run_env)
        if slot is None:
            QMessageBox.information(self.parent, "DAQ Busy", why)
            return
        self.show_output(slot)

    def run_produce(self):
        paths = self._with_selection("Produce")
        built = paths and self._build("Produce", scripts.produce, paths)
        if built:
            self._start("produce", "Produce", built[0])

    def run_analysis(self):
        paths = self._with_selection("Analysis")
        built = paths and self._build("Analysis", scripts.analysis, paths)
        if built:
            self._start("analysis", "Analysis", built[0])

    def run_contour(self):
        paths = self._with_selection("Contour")
        if not paths:
            return
        dlg = _Form(self.parent, "Waveform Contour Settings",
                    [("y_lo", "Y-axis -mV (below ped)", QLineEdit("3.0")),
                     ("y_hi", "Y-axis +mV (above ped)", QLineEdit("3.0")),
                     ("x_start", "X-axis start (sample)", QLineEdit("")),
                     ("x_end", "X-axis end (sample)", QLineEdit(""))])
        if dlg.exec_() != QDialog.Accepted:
            return
        t = {k: w.text() for k, w in dlg.w.items()}
        built = self._build("Contour", scripts.contour, paths, t["y_lo"], t["y_hi"], t["x_start"], t["x_end"])
        if built:
            self._start("contour", "Contour", built[0])

    def run_uniformity(self):
        dlg = UniformityDialog(self.parent)
        if dlg.exec_() != QDialog.Accepted:
            return
        ranges, tag, chs = dlg.ranges(), dlg.date_tag(), dlg.channels()
        if not ranges or not tag or not chs:
            QMessageBox.warning(self.parent, "Uniformity",
                                "Date tag, one block or an integer run range, and one channel are required.")
            return
        cmds = []
        for start, end in ranges:
            cmd = self._build("Uniformity", scripts.uniformity, tag, start, end, chs)
            if not cmd:
                return
            cmds.append(f"( {cmd} )")
        self._start("uniformity", "Uniformity", " ; ".join(cmds))   # blocks one after another in one job

    def run_stability(self):
        dlg = _Form(self.parent, "Stability (Monitor Drift)", [("tag", "Date tag", _tag_box()), *_range_rows()])
        if dlg.exec_() != QDialog.Accepted:
            return
        rng = _ints(dlg)
        tag = dlg.w["tag"].currentText().strip()
        if rng is None or not tag:
            QMessageBox.warning(self.parent, "Stability", "Date tag and an integer run range are required.")
            return
        cmd = self._build("Stability", scripts.stability, tag, rng[0], rng[1])
        if cmd:
            self._start("uniformity", "Stability", cmd)

    def run_overlay(self):
        try:
            tags = scripts.overlay_datasets()
            cmd = scripts.overlay_command()
        except scripts.ScriptError as e:
            QMessageBox.warning(self.parent, "Overlay Uniformity", str(e))
            return
        if not tags:
            QMessageBox.warning(self.parent, "Overlay Uniformity", "No Graphs_Uniformity_*.root datasets found.")
            return
        if JOBS.running("overlay"):
            QMessageBox.information(self.parent, "Overlay Busy", "An Overlay job is already running.")
            return
        dlg = OverlayDialog(self.parent, tags)
        if dlg.exec_() != QDialog.Accepted:
            return
        rows, ref, chans = dlg.result_rows()
        if not any(r[1] for r in rows) or not chans:
            QMessageBox.warning(self.parent, "Overlay Uniformity", "Select at least one dataset and one channel.")
            return
        try:
            scripts.write_overlay(rows, ref, chans)
        except OSError as e:
            QMessageBox.critical(self.parent, "Overlay Uniformity", f"Could not write the overlay config:\n{e}")
            return
        self._start("overlay", "Overlay", cmd)

    def run_waveform(self):
        paths = selection()
        self.show_page("Waveform")
        if len(paths) == 1:
            from pages.daq import waveform
            if waveform.live() is not None:
                waveform.live().open_path(paths[0])

    def open_image_viewer(self):
        self.show_page("Images")

    def open_data_log(self):
        """The DAQ Data Monitoring dashboard (serve_data_monitoring.py), LAN address first, as the Tk app."""
        import webbrowser
        from core import applog
        url = "http://192.168.10.100:8090/"
        applog.log(f"[INFO] Opening Data Log dashboard: {url}  (Tailscale fallback: http://100.119.212.12:8090/)")
        try:
            webbrowser.open(url)
        except Exception as e:
            applog.log(f"[WARNING] Could not open browser automatically: {e}")
            QMessageBox.information(self.parent, "Data Log", f"Open this URL manually:\n{url}")

    def run_cisco(self):
        """Cisco Secure Client (SUKAP connection), same search order as the Tk app."""
        import subprocess
        from core import applog
        applog.log("Attempting to launch SUKAP Connection (Cisco)...")
        for path in ("/opt/cisco/secureclient/bin/vpnui", "/opt/cisco/anyconnect/bin/vpnui",
                     "/usr/local/bin/vpnui", "vpnui"):
            try:
                subprocess.Popen([path], start_new_session=True)
                applog.log(f"Cisco launched successfully from: {path}")
                return
            except FileNotFoundError:
                continue
            except Exception as e:
                applog.log(f"Error launching {path}: {e}")
        applog.log("ERROR: Could not find the Cisco vpnui executable.")
        QMessageBox.critical(self.parent, "Execution Error",
                             "Could not find Cisco vpnui.\n\nChecked path: /opt/cisco/secureclient/bin/vpnui\n"
                             "Check the file's permissions (chmod +x).")

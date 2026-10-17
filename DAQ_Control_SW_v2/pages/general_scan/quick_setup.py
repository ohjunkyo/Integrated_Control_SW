"""General Scan > Quick Setup, mirroring ui_automation's Quick Configuration and Handover
Notes: the form shows the DAQ config header plus the live hardware values (SN2/SN3 angles,
HV, the active laser); Save Settings & Compile writes the header and rebuilds the DAQ binary.
Handover notes are appended to / edited in handover_notes.jsonl."""
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QCursor
from PyQt5.QtWidgets import (QApplication, QComboBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                             QMessageBox, QPlainTextEdit, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                             QWidget)

from app import theme
from core import env, status
from core import quick_setup as qs

# (label, config key) rows, same order as the Tk form
ROWS = [[("Shifter", "Shift_worker"), ("Expert", "Expert"), ("Laser", "Laser"), ("Wavelength", "Wavelength"),
         ("B-field", "BField")],
        [("X(A)", "BField_X"), ("Y(A)", "BField_Y"), ("Z-Top(A)", "BField_ZTop"), ("Z-Bottom(A)", "BField_ZBottom")],
        [("Note", "NOTE")],
        None,
        [("SN1", "SN1"), ("Dir(A~H)", "direction1"), ("Rot(°)", "RotateAngle1"), ("Tilt(°)", "TiltAngle1"),
         ("HV1(V)", "HV1")],
        [("SN2", "SN2"), ("Dir(A~H)", "direction2"), ("Rot(°)", "RotateAngle2"), ("Tilt(°)", "TiltAngle2"),
         ("HV2(V)", "HV2")],
        [("SN3", "SN3"), ("Dir(A~H)", "direction3"), ("Rot(°)", "RotateAngle3"), ("Tilt(°)", "TiltAngle3"),
         ("HV3(V)", "HV3")]]
HW_POLL_MS = 3000
UNSAVED = ("HV/Laser/Wavelength changed on hardware but NOT saved -- "
           "click 'Save Settings & Compile' or the run will be mislabeled.")


class _Field:
    """QLineEdit or the B-field ON/OFF box behind one get/set interface."""

    def __init__(self, key, on_edit):
        self.key = key
        if key == "BField":
            self.w = QComboBox()
            self.w.addItems(["ON", "OFF"])
            self.w.activated.connect(lambda _i: on_edit(key))
        else:
            self.w = QLineEdit()
            self.w.setAlignment(Qt.AlignLeft | Qt.AlignVCenter if key == "NOTE" else Qt.AlignCenter)
            self.w.textEdited.connect(lambda _t: on_edit(key))
        self.w.setMinimumWidth(90)
        self.w.setStyleSheet("font-size: 11pt; font-weight: 600;")

    def text(self):
        return self.w.currentText() if isinstance(self.w, QComboBox) else self.w.text()

    def set(self, value):
        if isinstance(self.w, QComboBox):
            if self.w.findText(value) < 0:
                self.w.addItem(value)
            self.w.setCurrentText(value)
        elif self.w.text() != value:
            self.w.setText(value)
            self.w.setCursorPosition(0)


class QuickSetupPage(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 4, 12, 12)
        lay.setSpacing(12)

        form = QGroupBox("Quick Configuration (Edit && Save)")
        g = QGridLayout(form)
        g.setHorizontalSpacing(8)
        g.setVerticalSpacing(8)
        self.fields = {}
        self.dirty = set()   # keys edited by hand or synced from hardware, not saved yet
        r = 0
        for row in ROWS:
            if row is None:
                g.setRowMinimumHeight(r, 10)
                r += 1
                continue
            for c, (label, key) in enumerate(row):
                lab = QLabel(label)
                lab.setObjectName("key")
                lab.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                g.addWidget(lab, r, 2 * c)
                f = _Field(key, self.dirty.add)
                g.addWidget(f.w, r, 2 * c + 1, 1, 9 if key == "NOTE" else 1)   # the note spans the whole form
                self.fields[key] = f
            r += 1
        lay.addWidget(form)
        btns = QHBoxLayout()
        self.open_btn = QPushButton("Open Global Config (Paths)")
        self.open_btn.clicked.connect(self._open_global)
        btns.addWidget(self.open_btn, 1)
        self.save_btn = QPushButton("Save Settings && Compile")
        self.save_btn.clicked.connect(self._save)
        btns.addWidget(self.save_btn, 1)
        lay.addLayout(btns)
        self.banner = QLabel(UNSAVED)
        self.banner.setAlignment(Qt.AlignCenter)
        self.banner.setStyleSheet(f"background: {theme.C['bad']}; color: white; font-weight: 700; padding: 6px;")
        self.banner.hide()
        lay.addWidget(self.banner)

        notes = QGroupBox("Handover Notes (for the next shift)")
        nl = QHBoxLayout(notes)
        left = QVBoxLayout()
        top = QHBoxLayout()
        top.addWidget(QLabel("Your name"))
        self.author = QLineEdit()
        self.author.setFixedWidth(160)
        top.addWidget(self.author)
        self.last = QLabel("No notes yet")
        self.last.setObjectName("hint")
        top.addStretch(1)
        self.cancel_edit_btn = QPushButton("Cancel edit")
        self.cancel_edit_btn.clicked.connect(self._cancel_edit)
        self.cancel_edit_btn.hide()
        top.addWidget(self.cancel_edit_btn)
        self.note_btn = QPushButton("Save Note")
        self.note_btn.clicked.connect(self._save_note)
        top.addWidget(self.note_btn)
        left.addLayout(top)
        left.addWidget(self.last)
        self.text = QPlainTextEdit()
        left.addWidget(self.text, 1)
        nl.addLayout(left, 4)
        right = QVBoxLayout()
        h = QLabel("History")
        h.setObjectName("key")
        right.addWidget(h)
        self.hist = QTreeWidget()
        self.hist.setHeaderLabels(["Time", "Author", "Note"])
        self.hist.setRootIsDecorated(False)
        self.hist.setAlternatingRowColors(True)
        self.hist.setColumnWidth(0, 170)
        self.hist.setColumnWidth(1, 90)
        self.hist.itemSelectionChanged.connect(self._show_note)
        right.addWidget(self.hist, 5)
        acts = QHBoxLayout()
        self.pos = QLabel()
        self.pos.setObjectName("hint")
        acts.addWidget(self.pos)
        acts.addStretch(1)
        self.edit_btn = QPushButton("Edit")
        self.edit_btn.clicked.connect(self._edit_note)
        acts.addWidget(self.edit_btn)
        self.del_btn = QPushButton("Delete")
        self.del_btn.clicked.connect(self._delete_note)
        acts.addWidget(self.del_btn)
        right.addLayout(acts)
        self.full = QPlainTextEdit()
        self.full.setObjectName("dark")
        self.full.setReadOnly(True)
        right.addWidget(self.full, 4)
        nl.addLayout(right, 6)
        lay.addWidget(notes, 1)

        self.entries = []          # chronological
        self.editing = None        # chronological index being edited, None = new note
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(5000)
        self.hw_timer = QTimer(self)
        self.hw_timer.timeout.connect(self._poll_hardware)
        self.hw_timer.start(HW_POLL_MS)
        self.refresh()
        self._poll_hardware()

    # ---------------------------------------------------------------- form
    def refresh(self):
        cfg = env.read_config()
        for key, f in self.fields.items():
            if key not in self.dirty:
                f.set(cfg.get(key, ""))
        self._load_notes()

    def _reload_form(self):
        self.dirty.clear()
        self.banner.hide()
        self.refresh()
        self._poll_hardware()

    def _poll_hardware(self):
        """Live SN2/SN3 angles, HV and the active laser into the form (the Tk app's 3 s poll)."""
        try:
            for dev, (tilt, rot, _moving) in status.live_angles().items():
                if dev in (2, 3):
                    for key, val in ((f"RotateAngle{dev}", f"{rot:.1f}"), (f"TiltAngle{dev}", f"{tilt:.1f}")):
                        if self.fields[key].text() != val:
                            self.fields[key].set(val)
                            self.dirty.add(key)
            updates = {}
            wl_nm, laser_ma = qs.read_live_laser()
            if wl_nm:
                updates["Wavelength"], updates["Laser"] = wl_nm, laser_ma
            updates.update(qs.read_live_hv())
            changes = qs.live_changes({k: self.fields[k].text() for k in updates}, updates)
        except Exception:
            return
        if not changes:
            return
        for key, _old, new in changes:
            self.fields[key].set(new)
            self.dirty.add(key)
        text = ", ".join(f"{k}: {old or '(blank)'} -> {new}" for k, old, new in changes)
        hv = any(k.startswith("HV") for k, _o, _n in changes)
        if hv:
            self.banner.show()
        import instance_lock
        if env.NO_HW or instance_lock.is_running("daq"):   # the Tk app logs its own copy
            return
        if hv:
            qs._log(f"[CRITICAL] HV changed on hardware ({text}) but config3.h was NOT updated -- runs taken from "
                    "now on will have the WRONG HV in their metadata unless you click 'Save Settings & Compile' "
                    "immediately.")
        else:
            qs._log(f"[INFO] Quick Setup synced from hardware: {text}   "
                    "(press 'Save Settings & Compile' to write config3.h)")

    def _open_global(self):
        from pages.daq.script_actions import Actions
        Actions(self, lambda _slot: None, lambda _name: None).open_config()
        self._reload_form()

    def _save(self):
        path = env.config_file()
        values = {k: f.text().strip() for k, f in self.fields.items()}
        QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
        try:
            warning = qs.save_settings(path, values)
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Error", f"Failed to save config file: {e}")
            return
        QApplication.restoreOverrideCursor()
        QMessageBox.information(self, "Success", "Configuration saved successfully.")
        if warning:
            QMessageBox.warning(self, "DAQ Binary Rebuild Failed", warning)
        qs._log("Quick Setup settings saved to config file.")
        self._reload_form()

    # ---------------------------------------------------------------- notes
    def _load_notes(self, force=False):
        try:
            entries = qs.load_notes()
        except FileNotFoundError:
            entries = []
        except Exception as e:
            qs._log(f"[WARNING] Failed to load handover notes: {e}")
            return
        if entries == self.entries and not force:
            return
        self.entries = entries
        self.hist.clear()
        for e in reversed(entries):   # newest first
            preview = e.get("note", "").replace("\n", " ")
            if len(preview) > 60:
                preview = preview[:57] + "..."
            it = QTreeWidgetItem([e.get("time", ""), e.get("author", ""), preview])
            it.setData(0, Qt.UserRole, e.get("note", ""))
            self.hist.addTopLevelItem(it)
        if entries:
            self.last.setText(f"Last updated: {entries[-1].get('time', '')} by {entries[-1].get('author', '')}")
        self.full.clear()
        self.pos.clear()

    def _selected(self):
        """Chronological index of the selected note, or None."""
        it = self.hist.currentItem()
        if it is None or not it.isSelected():
            return None
        i = self.hist.indexOfTopLevelItem(it)
        return len(self.entries) - 1 - i if 0 <= i < len(self.entries) else None

    def _show_note(self):
        idx = self._selected()
        if idx is None:
            self.full.clear()
            self.pos.clear()
            return
        e = self.entries[idx]
        self.full.setPlainText(e.get("note", ""))
        self.pos.setText(f"{e.get('time', '')}   {e.get('author', '')}   "
                         f"{len(self.entries) - idx} / {len(self.entries)}")

    def _save_note(self):
        from datetime import datetime
        author = self.author.text().strip() or "unknown"
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        body = self.text.toPlainText().strip()
        if not body:
            QMessageBox.warning(self, "Empty Note", "Write a note before saving.")
            return
        try:
            if self.editing is not None:
                entries = list(self.entries)
                if not 0 <= self.editing < len(entries):
                    QMessageBox.critical(self, "Error", "That note no longer exists (list changed underneath it).")
                    self._cancel_edit()
                    return
                entries[self.editing] = {"time": ts, "author": author, "note": body}
                qs.write_notes(entries)
                qs._log(f"[INFO] Handover note edited by {author}.")
                self._cancel_edit()
            else:
                qs.append_note(author, body, ts)
                qs._log(f"[INFO] Handover note saved by {author}.")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save handover note: {e}")
            return
        self.last.setText(f"Last updated: {ts} by {author}")
        self.text.clear()
        self._load_notes(force=True)

    def _edit_note(self):
        idx = self._selected()
        if idx is None:
            QMessageBox.information(self, "Edit Note", "Select a note in the history first.")
            return
        e = self.entries[idx]
        self.editing = idx
        self.author.setText(e.get("author", ""))
        self.text.setPlainText(e.get("note", ""))
        self.note_btn.setText("Save Edit")
        self.cancel_edit_btn.show()

    def _cancel_edit(self):
        self.editing = None
        self.text.clear()
        self.note_btn.setText("Save Note")
        self.cancel_edit_btn.hide()

    def _delete_note(self):
        idx = self._selected()
        if idx is None:
            return
        e = self.entries[idx]
        if QMessageBox.question(self, "Delete Note", f"Delete this note?\n\n{e.get('time', '')} · {e.get('author', '')}"
                                f"\n\n{e.get('note', '')[:200]}") != QMessageBox.Yes:
            return
        try:
            entries = list(self.entries)
            del entries[idx]
            qs.write_notes(entries)
            qs._log(f"[INFO] Handover note deleted (was by {e.get('author', '')}).")
            if self.editing == idx:
                self._cancel_edit()
        except Exception as ex:
            QMessageBox.critical(self, "Error", f"Failed to delete handover note: {ex}")
            return
        self._load_notes(force=True)

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
from core import function_generator as FG
from core import quick_setup as qs
from pages.fg_parts import Runner

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
FG_POLL_MS = 5000
UNSAVED = ("HV/Laser/Wavelength changed on hardware but NOT saved -- "
           "click 'Save Settings & Compile' or the run will be mislabeled.")



def _routine_active():
    try:
        from core import routine as rt
        return bool(rt._state.get("active")) or rt.hv_restore_active()   # end-of-routine HV restore included
    except Exception:
        return False


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
        self.scan_rot = {}   # direction key -> label with the X / Y scan rotation angles
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
                if key in ("direction2", "direction3"):
                    f.w.textChanged.connect(lambda _t, k=key: self._show_scan_rot(k))
                    hint = QLabel()
                    hint.setStyleSheet(f"font-size: 11pt; font-weight: 700; color: {theme.C['warn']};")
                    g.addWidget(hint, r, 10)
                    self.scan_rot[key] = hint
            r += 1
        g.setRowMinimumHeight(r, 10)
        r += 1
        self.fg_rate, self.fg_width = QLineEdit(), QLineEdit()   # live DT1081B Section A, read-only
        self.record = QLineEdit()                                 # config3.h TimeWindow / PostTrigger, read-only
        for c, (label, w) in enumerate((("Trigger rate", self.fg_rate), ("Trigger width", self.fg_width),
                                        ("Record", self.record))):
            lab = QLabel(label)
            lab.setObjectName("key")
            lab.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            g.addWidget(lab, r, 2 * c)
            w.setReadOnly(True)
            w.setAlignment(Qt.AlignCenter)
            w.setMinimumWidth(90)
            w.setStyleSheet("font-size: 11pt; font-weight: 600;")
            g.addWidget(w, r, 2 * c + 1)
        from widgets.record_layout import RecordHover
        self._record_hover = RecordHover([self.record], self._record_params)
        lay.addWidget(form)
        btns = QHBoxLayout()
        self.open_btn = QPushButton("Open Global Config (Paths)")
        self.open_btn.clicked.connect(self._open_global)
        btns.addWidget(self.open_btn, 1)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self._refresh_all)
        btns.addWidget(self.refresh_btn)
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
        self.fg_runner = Runner()
        self._fg_busy = False
        self.fg_timer = QTimer(self)
        self.fg_timer.timeout.connect(self._poll_fg)
        self.fg_timer.start(FG_POLL_MS)
        self.refresh()
        self._poll_hardware()
        self._poll_fg()

    # ---------------------------------------------------------------- form
    def _record_params(self):
        cfg = env.read_config()
        try:
            return int(cfg.get("TimeWindow", "")), int(cfg.get("PostTrigger", "")), "external"
        except ValueError:
            return None

    def _show_scan_rot(self, key):
        self.scan_rot[key].setText(qs.scan_rot_text(self.fields[key].text()))

    def refresh(self):
        cfg = env.read_config()
        for key, f in self.fields.items():
            if key not in self.dirty:
                f.set(cfg.get(key, ""))
        self.record.setText(f"{cfg.get('TimeWindow', '?')} smp / {cfg.get('PostTrigger', '?')} %")
        why = status.busy_reason()
        self.save_btn.setEnabled(not why)
        self.save_btn.setToolTip(why)
        self._load_notes()

    def _reload_form(self):
        self.dirty.clear()
        self.banner.hide()
        self.refresh()
        self._poll_hardware()

    def _refresh_all(self):
        """Re-read the saved header, the stage angles, HV, the active laser and the function generator now."""
        self._reload_form()
        self._poll_fg()

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
        if hv and not _routine_active():   # a routine records the HV of its runs itself
            self.banner.show()
        import instance_lock
        if env.NO_HW or instance_lock.is_running("daq"):   # the Tk app logs its own copy
            return
        if hv and _routine_active():
            qs._log(f"[INFO] HV changed by the Daily Routine ({text}); the routine records the HV of its runs.")
        elif hv:
            qs._log(f"[CRITICAL] HV changed on hardware ({text}) but config3.h was NOT updated -- runs taken from "
                    "now on will have the WRONG HV in their metadata unless you click 'Save Settings & Compile' "
                    "immediately.")
        else:
            qs._log(f"[INFO] Quick Setup synced from hardware: {text}   "
                    "(press 'Save Settings & Compile' to write config3.h)")

    def showEvent(self, e):
        super().showEvent(e)
        self._poll_fg()   # the rate may have changed while the tab was hidden

    def _poll_fg(self):
        if self._fg_busy or (not self.isVisible() and self.fg_rate.text()):
            return
        self._fg_busy = True
        self.fg_runner.run(FG.pulse_generator, self._fg_loaded)

    def _fg_loaded(self, cfg, err):
        self._fg_busy = False
        if err:
            self.fg_rate.setText("FG offline")
            self.fg_width.setText("")
            return
        if not cfg:
            self.fg_rate.setText("Section A is not a pulse generator")
            self.fg_width.setText("")
            return
        period, width = cfg.get("period"), cfg.get("width")
        if period:
            f = 1e9 / period
            unit = "MHz" if f >= 1e6 else "kHz" if f >= 1e3 else "Hz"
            f /= 1e6 if unit == "MHz" else 1e3 if unit == "kHz" else 1
            self.fg_rate.setText(f"{f:g} {unit}  ({period:,} ns)")
        else:
            self.fg_rate.setText("?")
        self.fg_width.setText(f"{width} ns" if width is not None else "?")

    def _open_global(self):
        from pages.daq.script_actions import Actions
        Actions(self, lambda _slot: None, lambda _name: None).open_config()
        self._reload_form()

    def _save(self):
        why = status.busy_reason()
        if why:
            QMessageBox.warning(self, "Save Settings", why + "\n\nconfig3.h cannot be changed now.")
            return
        if _routine_active() and QMessageBox.question(
                self, "Save Settings",
                "A Daily Routine is running and sets the HV and laser itself.\n"
                "Saving now writes its temporary values into config3.h.\n\nSave anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
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

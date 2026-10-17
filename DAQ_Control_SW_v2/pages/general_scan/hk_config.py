"""HK Digitizer Config dialog, the Qt form of the Tk app's managers/hk_config_dialog.py:
acquisition fields, ScanManager options and post-processing steps as editable lists (Edit:
add, delete, reorder), the admin-gated Detail settings, the final command preview, the
pipeline stage buttons, Test connection / Test trigger, Save (with a dated snapshot) and
Load of earlier snapshots. Data and commands come from core/hk.py; changes need Unlock."""
import json
import os
import threading
from datetime import datetime

from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QCursor, QFont, QTextCharFormat, QTextCursor
from PyQt5.QtWidgets import (QApplication, QCheckBox, QDialog, QFileDialog, QFrame, QGridLayout, QGroupBox,
                             QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
                             QScrollArea, QSplitter, QTextEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from app import theme
from core import hk, status
from core.access import ACCESS

_OPEN = None


def open_hk_config(parent=None, auto_mgr=None):
    """Show the dialog (one at a time, like the Tk window). auto_mgr: the scan engine whose
    hk_config is edited in place."""
    global _OPEN
    hk.attach(auto_mgr)
    try:
        if _OPEN is not None and _OPEN.isVisible():
            _OPEN.raise_()
            _OPEN.activateWindow()
            return _OPEN
    except RuntimeError:
        pass
    _OPEN = HkConfigDialog(parent)
    _OPEN.show()
    return _OPEN


def _mono():
    f = QFont(theme.MONO_FONT)
    f.setPointSizeF(theme.PT["mono"])
    return f


def _log(msg):
    from core import applog
    applog.log(msg)


def _password_ok(parent, prompt):
    from core.access import _admin_password
    pwd, ok = QInputDialog.getText(parent, "Security", prompt, QLineEdit.Password)
    if not ok:
        return False
    if pwd == _admin_password():
        return True
    QMessageBox.critical(parent, "Error", "Incorrect password.")
    return False


class _Bus(QObject):
    result = pyqtSignal(str, object)


class EditList(QWidget):
    """Rows of options (label, flag, value, on, bare) or steps (name, cmd, on); Edit mode adds
    label/flag editing, reorder, delete and an add row."""

    def __init__(self, items, kind, changed):
        super().__init__()
        self.items = [dict(it) for it in items]
        self.kind = kind            # "options" | "steps"
        self.changed = changed
        self.editing = False
        self.enabled = True
        self.v = QVBoxLayout(self)
        self.v.setContentsMargins(0, 0, 0, 0)
        self.body = None
        self.btn = QPushButton("Edit")
        self.btn.clicked.connect(self.toggle)
        self.render()

    def set_items(self, items):
        self.items = [dict(it) for it in items]
        self.render()

    def toggle(self):
        self.editing = not self.editing
        self.btn.setText("Done" if self.editing else "Edit")
        self.render()

    def move(self, i, d):
        j = i + d
        if 0 <= j < len(self.items):
            self.items[i], self.items[j] = self.items[j], self.items[i]
            self.render()
            self.changed()

    def delete(self, i):
        del self.items[i]
        self.render()
        self.changed()

    def _set(self, it, key, value):
        it[key] = value
        self.changed()

    def _entry(self, it, key, width=0, mono=False):
        e = QLineEdit(str(it.get(key, "")))
        if width:
            e.setFixedWidth(width)
        if mono:
            e.setFont(_mono())
        e.textChanged.connect(lambda t, it=it, key=key: self._set(it, key, t))
        e.setEnabled(self.enabled)
        return e

    def render(self):
        if self.body is not None:
            self.v.removeWidget(self.body)
            self.body.setParent(None)
            self.body.deleteLater()
        self.body = QWidget()
        g = QGridLayout(self.body)
        g.setContentsMargins(0, 0, 0, 0)
        g.setHorizontalSpacing(6)
        g.setVerticalSpacing(4)
        g.setColumnStretch(2, 1)
        if not self.items:
            n = QLabel("(none)")
            n.setObjectName("hint")
            g.addWidget(n, 0, 0)
        for i, it in enumerate(self.items):
            cb = QCheckBox("" if self.editing else (it.get("label") if self.kind == "options" else it.get("name")) or "")
            cb.setChecked(bool(it.get("on")))
            cb.toggled.connect(lambda on, it=it: self._set(it, "on", bool(on)))
            cb.setEnabled(self.enabled)
            g.addWidget(cb, i, 0)
            if self.kind == "options":
                if self.editing:
                    box = QHBoxLayout()
                    box.addWidget(self._entry(it, "label", 150))
                    box.addWidget(self._entry(it, "flag", 140, mono=True))
                    g.addLayout(box, i, 1)
                else:
                    f = QLabel(it.get("flag", ""))
                    f.setFont(_mono())
                    f.setObjectName("key")
                    f.setMinimumWidth(130)
                    g.addWidget(f, i, 1)
                if it.get("bare"):
                    h = QLabel("flag only")
                    h.setObjectName("hint")
                    g.addWidget(h, i, 2)
                else:
                    g.addWidget(self._entry(it, "value"), i, 2)
            elif self.editing:
                g.addWidget(self._entry(it, "name", 150), i, 1)
                g.addWidget(self._entry(it, "cmd", mono=True), i, 2)
            else:
                cmd = it.get("cmd", "")
                c = QLabel(cmd if len(cmd) <= 90 else cmd[:89] + "...")
                c.setFont(_mono())
                c.setObjectName("key")
                c.setToolTip(cmd)
                g.addWidget(c, i, 1, 1, 2)
            if self.editing:
                bx = QHBoxLayout()
                for text, fn in (("Up", lambda _=False, i=i: self.move(i, -1)),
                                 ("Down", lambda _=False, i=i: self.move(i, 1)),
                                 ("Delete", lambda _=False, i=i: self.delete(i))):
                    b = QPushButton(text)
                    b.clicked.connect(fn)
                    if text == "Delete":
                        b.setStyleSheet(f"QPushButton {{ color: {theme.C['bad']}; }}")
                    bx.addWidget(b)
                g.addLayout(bx, i, 3)
        if self.editing:
            self._add_row(g, len(self.items))
        self.v.addWidget(self.body)

    def _add_row(self, g, r):
        opt = self.kind == "options"
        e1 = QLineEdit()
        e1.setPlaceholderText("Label" if opt else "Step name")
        e1.setFixedWidth(150)
        e2 = None
        if opt:
            e2 = QLineEdit()
            e2.setPlaceholderText("--flag")
            e2.setFont(_mono())
            e2.setFixedWidth(140)
        e3 = QLineEdit()
        e3.setPlaceholderText("value (blank = flag only)" if opt
                              else 'command, e.g. Tool.py --in "{bin}" -o "{base}_out.root"')
        if not opt:
            e3.setFont(_mono())
            e3.setToolTip('{bin} = the .bin just written, {base} = the same name without .bin')
        err = QLabel()
        err.setStyleSheet(f"color: {theme.C['bad']};")
        err.hide()

        def add():
            name = e1.text().strip()
            if opt:
                flag, val = e2.text().strip(), e3.text().strip()
                if not name or not flag.startswith("-"):
                    err.setText("Enter a label and a flag starting with '-'.")
                    err.show()
                    return
                self.items.append({"label": name, "flag": flag, "value": val, "on": True, "bare": val == ""})
            else:
                cmd = e3.text().strip()
                if not name or not cmd:
                    err.setText("Enter a step name and a command.")
                    err.show()
                    return
                self.items.append({"name": name, "cmd": cmd, "on": True})
            self.render()
            self.changed()

        box = QHBoxLayout()
        box.addWidget(e1)
        if e2 is not None:
            box.addWidget(e2)
        g.addLayout(box, r, 0, 1, 2)
        g.addWidget(e3, r, 2)
        b = QPushButton("Add parameter" if opt else "Add step")
        b.clicked.connect(add)
        g.addWidget(b, r, 3)
        g.addWidget(err, r + 1, 0, 1, 4)
        for e in (e1, e2, e3):
            if e is not None:
                e.textEdited.connect(lambda _t: err.hide())

    def set_enabled(self, on):
        self.enabled = on
        self.btn.setEnabled(on)
        if not on and self.editing:
            self.toggle()
        else:
            self.render()

    def values(self):
        if self.kind == "options":
            return [{"label": str(it.get("label", "")).strip(), "flag": str(it.get("flag", "")).strip(),
                     "value": str(it.get("value", "")).strip(), "on": bool(it.get("on")), "bare": bool(it.get("bare"))}
                    for it in self.items]
        return [{"name": str(it.get("name", "")).strip(), "cmd": str(it.get("cmd", "")).strip(),
                 "on": bool(it.get("on"))} for it in self.items]


class HkConfigDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("HK Digitizer Configuration")
        self.resize(940, 860)
        self.setMinimumSize(700, 520)
        import instance_lock
        if instance_lock.is_running("daq"):   # the Tk app owns and updates it: show what is on disk
            w = hk.reload()
            if w:
                _log(w)
        self.hk = hk.config()
        self.bus = _Bus()
        self.bus.result.connect(self._on_result)
        root = QVBoxLayout(self)
        self.angle_lbl = QLabel()
        self.angle_lbl.setObjectName("key")
        root.addWidget(self.angle_lbl)

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        bv = QVBoxLayout(body)
        bv.setSpacing(10)

        acq = QGroupBox("Acquisition")
        av = QVBoxLayout(acq)
        core = QGridLayout()
        core.setColumnStretch(1, 1)
        core.setColumnStretch(3, 1)
        self.run_id = QLineEdit(str(self.hk.get("run_id", "")))
        self.run_id.setToolTip("{run} {tilt} {rot2} {rot3} {trgch} {trgvth} {mode} fill in per point")
        self.run_no = QLineEdit(str(self.hk.get("run_number", 0)))
        self.run_no.setFixedWidth(80)
        self.acq = QLineEdit(str(self.hk.get("acq_time", 10.0)))
        self.acq.setFixedWidth(100)
        self.delay = QLineEdit(str(self.hk.get("move_delay", 20.0)))
        self.delay.setFixedWidth(100)
        self.work_dir = QLineEdit(str(self.hk.get("work_dir", "")))
        core.addWidget(QLabel("Filename (-i)"), 0, 0)
        core.addWidget(self.run_id, 0, 1, 1, 3)
        core.addWidget(QLabel("Run number"), 1, 0)
        rn = QHBoxLayout()
        rn.addWidget(self.run_no)
        self.run_live = QLabel()
        self.run_live.setStyleSheet(f"color: {theme.C['accent']}; font-weight: 700;")
        rn.addWidget(self.run_live)
        self.sync_btn = QPushButton("Sync from HK PC")
        self.sync_btn.clicked.connect(self._sync_run_number)
        rn.addWidget(self.sync_btn)
        rn.addStretch(1)
        core.addLayout(rn, 1, 1)
        core.addWidget(QLabel("Acq time --l (s)"), 1, 2)
        core.addWidget(self.acq, 1, 3)
        core.addWidget(QLabel("Move delay (s)"), 2, 0)
        core.addWidget(self.delay, 2, 1)
        core.addWidget(QLabel("Work dir (cd)"), 2, 2)
        core.addWidget(self.work_dir, 2, 3)
        av.addLayout(core)
        self.opts = EditList(self.hk.get("scan_options", []), "options", self._changed)
        av.addLayout(self._bar("ScanManager options", self.opts.btn))
        av.addWidget(self.opts)
        bv.addWidget(acq)

        post = QGroupBox("Post-processing")
        pv = QVBoxLayout(post)
        self.steps = EditList(self.hk.get("post_steps", []), "steps", self._changed)
        pv.addLayout(self._bar("Steps (in order)", self.steps.btn))
        pv.addWidget(self.steps)
        bv.addWidget(post)

        det = QGroupBox()
        dv = QVBoxLayout(det)
        self.det_btn = QPushButton("Detail settings (admin)")
        self.det_btn.setCheckable(True)
        self.det_btn.setStyleSheet("text-align: left; font-weight: 700;")
        self.det_btn.toggled.connect(lambda on: self.det_body.setVisible(on))
        dv.addWidget(self.det_btn)
        self.det_body = QWidget()
        dg = QGridLayout(self.det_body)
        dg.setColumnStretch(1, 1)
        self.unlock_btn = QPushButton("Admin unlock")
        self.unlock_btn.clicked.connect(self._admin_unlock)
        dg.addWidget(self.unlock_btn, 0, 0)
        u, _, h = str(self.hk.get("ssh_target", "") or "").rpartition("@")
        seed = {"ssh_user": u, "ssh_host": h}
        self.pvars = {}
        for r, (key, label, width) in enumerate(hk.PROTECTED, start=1):
            dg.addWidget(QLabel(label), r, 0)
            e = QLineEdit(str(seed.get(key, self.hk.get(key, ""))))
            e.setReadOnly(True)
            if width:
                e.setFixedWidth(width)
                dg.addWidget(e, r, 1, Qt.AlignLeft)
            else:
                dg.addWidget(e, r, 1)
            e.textChanged.connect(self._changed)
            self.pvars[key] = e
        self.det_body.hide()
        self.admin = False
        dv.addWidget(self.det_body)
        bv.addWidget(det)
        bv.addStretch(1)
        area.setWidget(body)
        root.addWidget(area, 1)

        cmd = QGroupBox("Final command")
        cg = QGridLayout(cmd)
        self.summary = QLabel()
        self.summary.setFont(_mono())
        cg.addWidget(self.summary, 0, 0)
        cg.setColumnStretch(0, 1)
        self.full_btn = QPushButton("Show full")
        self.full_btn.clicked.connect(self._toggle_full)
        cg.addWidget(self.full_btn, 0, 1)
        copy = QPushButton("Copy")
        copy.clicked.connect(self._copy)
        cg.addWidget(copy, 0, 2)
        self.full_box = QPlainTextEdit()
        self.full_box.setObjectName("dark")
        self.full_box.setReadOnly(True)
        self.full_box.setFont(_mono())
        self.full_box.setFixedHeight(130)
        self.full_box.hide()
        cg.addWidget(self.full_box, 1, 0, 1, 3)
        root.addWidget(cmd)

        stages = QHBoxLayout()
        k = QLabel("Pipeline")
        k.setStyleSheet("font-weight: 700;")
        stages.addWidget(k)
        self.dpb_btn = QPushButton("2  DPB setup (admin)")
        self.dpb_btn.clicked.connect(lambda: self._locked_stage(self._run_dpb_setup, "DPB Setup"))
        self.vm_btn = QPushButton("3  vmodem (admin)")
        self.vm_btn.clicked.connect(lambda: self._locked_stage(lambda: self._run_stage_once(
            lambda: hk.format_vmodem(self.hk), "vmodem Process", "vmodem_done",
            "Re-sending the m/O keys may just re-toggle the mode instead of setting it."), "vmodem Process"))
        self.acq_btn = QPushButton("4  Acquire (ScanManager)")
        self.acq_btn.clicked.connect(lambda: self._run_pipeline_stage(lambda: hk.build_remote_current(self.hk),
                                                                      "HK Acquire"))
        for b in (self.dpb_btn, self.vm_btn, self.acq_btn):
            stages.addWidget(b)
        stages.addStretch(1)
        root.addLayout(stages)

        btns = QHBoxLayout()
        self.conn_btn = QPushButton("Test connection")
        self.conn_btn.clicked.connect(self._test_connection)
        self.test_btn = QPushButton("Test trigger (dummy)")
        self.test_btn.clicked.connect(lambda: self._run_pipeline_stage(
            lambda: hk.build_remote_current(self.hk, prefix="TEST_"), "HK Test"))
        load = QPushButton("Load...")
        load.clicked.connect(self._load_dialog)
        for b in (self.conn_btn, self.test_btn, load):
            btns.addWidget(b)
        btns.addStretch(1)
        self.status = QLabel()
        self.status.setStyleSheet(f"color: {theme.C['accent']};")
        btns.addWidget(self.status)
        self.save_btn = QPushButton("Save")
        self.save_btn.setProperty("variant", "primary")
        self.save_btn.clicked.connect(self._save)
        btns.addWidget(self.save_btn)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        btns.addWidget(close)
        root.addLayout(btns)

        self.current_acq = ""
        for e in (self.run_id, self.run_no, self.acq, self.delay, self.work_dir):
            e.textChanged.connect(self._changed)
        self.guarded = [self.run_id, self.run_no, self.acq, self.delay, self.work_dir, self.sync_btn, self.save_btn,
                        self.conn_btn, self.test_btn, self.dpb_btn, self.vm_btn, self.acq_btn, self.unlock_btn]
        ACCESS.listeners.append(self._paint_access)
        self._paint_access()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(1000)
        self._tick()
        self._refresh_preview()

    @staticmethod
    def _bar(title, btn):
        h = QHBoxLayout()
        t = QLabel(title)
        t.setObjectName("key")
        h.addWidget(t)
        h.addStretch(1)
        h.addWidget(btn)
        return h

    # ---------------------------------------------------------------- state
    def _paint_access(self, on=None):
        on = ACCESS.unlocked if on is None else on
        for w in self.guarded:
            w.setEnabled(on)
            w.setToolTip("" if on else "Unlock to use")
        self.opts.set_enabled(on)
        self.steps.set_enabled(on)
        for e in self.pvars.values():
            e.setReadOnly(not (on and self.admin))

    def _tick(self):
        live = status.live_angles()

        def fmt(dev):
            if dev not in live:
                return "-"
            tilt, rot, _m = live[dev]
            return f"{tilt:.1f}°/{rot:.1f}°"
        self.angle_lbl.setText(f"Live angles (tilt / rot)   SN2 {fmt(2)}   SN3 {fmt(3)}")
        n = self.hk.get("run_number", "?")
        self.run_live.setText(f"live: {n}")
        if not self.run_no.hasFocus() and self.run_no.text() != str(n):
            self.run_no.setText(str(n))   # a later Save must not push a stale number back

    def _changed(self, *_):
        self._refresh_preview()

    def _ssh_target(self, d):
        uu, hh = (d.get("ssh_user") or "").strip(), (d.get("ssh_host") or "").strip()
        return f"{uu}@{hh}" if uu else hh

    def _collect(self):
        new = {"run_id": self.run_id.text().strip(), "work_dir": self.work_dir.text().strip()}
        try:
            new["run_number"] = int(float(self.run_no.text()))
            new["acq_time"] = float(self.acq.text())
            new["move_delay"] = float(self.delay.text())
        except ValueError:
            raise ValueError("Run number, Acq time and Move delay must be numbers.")
        o, p = self.opts.values(), self.steps.values()
        for x in o:
            if not x["flag"].startswith("-"):
                raise ValueError(f"Option '{x['label'] or '?'}': flag must start with '-'.")
        new["scan_options"], new["post_steps"] = o, p
        d = {k: e.text().strip() for k, e in self.pvars.items()}
        new["ssh_target"] = self._ssh_target(d)
        for k, _l, _w in hk.PROTECTED:
            if k not in ("ssh_user", "ssh_host"):
                new[k] = d[k]
        return new

    def _live(self):
        d = dict(self.hk)
        d.update({k: e.text() for k, e in self.pvars.items()})
        d["ssh_target"] = self._ssh_target(d)
        d["run_id"], d["work_dir"] = self.run_id.text(), self.work_dir.text()
        d["scan_options"], d["post_steps"] = self.opts.values(), self.steps.values()
        return d

    def _refresh_preview(self):
        if not hasattr(self, "summary"):
            return
        d = self._live()
        try:
            acq = float(self.acq.text() or 0)
        except ValueError:
            acq = 0.0
        try:
            run_no = int(float(self.run_no.text() or 0))
        except ValueError:
            run_no = 0
        run_id = d.get("run_id", "").replace("{run}", f"{run_no:03d}")
        try:
            full = hk.format_remote(d, run_id, acq)
            short = (f"{os.path.basename(str(d.get('scan_manager', '')))} -i {run_id} --l {int(round(acq))}"
                     f"{hk.option_args(d)}")
            n = sum(1 for p in d["post_steps"] if p["on"] and p["cmd"])
            if n:
                short += f"   + {n} post step{'s' if n > 1 else ''}"
        except Exception as e:
            full = short = f"(cannot build yet: {e})"
        try:
            vm = hk.format_vmodem(d)
        except Exception as e:
            vm = f"(cannot build yet: {e})"
        self.summary.setText(short if len(short) <= 95 else short[:94] + "...")
        self.summary.setToolTip(short)
        self.current_acq = full
        self.full_box.setPlainText(f"Filename: {run_id}\n\n[4 Acquire]\n{full}\n\n[3 vmodem]\n{vm}")

    def _toggle_full(self):
        on = not self.full_box.isVisible()
        self.full_box.setVisible(on)
        self.full_btn.setText("Hide" if on else "Show full")

    def _copy(self):
        QApplication.clipboard().setText(self.current_acq)
        self.status.setText("Command copied")

    # ---------------------------------------------------------------- admin
    def _admin_unlock(self):
        if _password_ok(self, "Enter Master Password (HK Infrastructure Fields):"):
            self.admin = True
            for e in self.pvars.values():
                e.setReadOnly(not ACCESS.unlocked)
            self.unlock_btn.setText("Unlocked")
            self.unlock_btn.setEnabled(False)
            self.guarded.remove(self.unlock_btn)
            _log("[INFO] HK infrastructure fields unlocked (admin).")
        else:
            _log("[WARNING] Admin unlock denied for HK infrastructure fields.")

    # ---------------------------------------------------------------- save / apply
    def _apply_without_run_number(self):
        new = self._collect()
        new.pop("run_number", None)
        self.hk.update(new)
        hk.save()

    def _save(self):
        if not ACCESS.unlocked:
            return
        try:
            new = self._collect()
        except ValueError as e:
            QMessageBox.critical(self, "Invalid HK Config", f"{e}\n\nKeeping previous values.")
            return
        self.hk.update(new)
        try:
            hk.save(snapshot=True)
        except OSError as e:
            _log(f"[ERROR] Failed to save hk_config: {e}")
            QMessageBox.critical(self, "HK Config", f"Failed to save hk_config:\n{e}")
            return
        h = self.hk
        on = [p["name"] for p in h["post_steps"] if p["on"]]
        _log(f"[INFO] HK config saved: filename {h['run_id']}, run#{h['run_number']}, acq {h['acq_time']}s, "
             f"delay {h['move_delay']}s, options{hk.option_args(h) or ' (none)'}, post {', '.join(on) or 'none'}, "
             f"target {h['ssh_target']}")
        self.status.setText(f"Saved @ {datetime.now():%H:%M:%S}")
        self.opts.render()
        self.steps.render()

    # ---------------------------------------------------------------- background SSH
    def _bg(self, tag, fn):
        threading.Thread(target=lambda: self.bus.result.emit(tag, fn()), daemon=True).start()

    def _on_result(self, tag, res):
        if tag == "conn":
            self._conn_done(*res)
        elif tag == "sync":
            self._sync_done(res)

    def _test_connection(self):
        if not ACCESS.unlocked:
            return
        try:
            self._apply_without_run_number()
        except (ValueError, OSError) as e:
            QMessageBox.critical(self, "Invalid HK Config", str(e))
            return
        self.status.setText(f"Testing {self.hk.get('ssh_target', '')} ...")
        self.conn_btn.setEnabled(False)
        cfg = dict(self.hk)
        self._bg("conn", lambda: hk.test_connection(cfg))

    def _conn_done(self, rc, out):
        self.conn_btn.setEnabled(ACCESS.unlocked)
        target, sm = self.hk.get("ssh_target", ""), self.hk.get("scan_manager", "")
        out = out or ""
        if rc != 0:
            self.status.setText("SSH failed")
            QMessageBox.critical(self, "HK Connection Failed",
                                 f"Target: {target}\n\nssh exit code {rc}\n\n{out.strip()[:800]}\n\n"
                                 "Check the host/IP and user in Detail settings, and that this PC's "
                                 "SSH key is installed on the HK PC (ssh-copy-id).")
            return
        ok_sm = "SCANMGR=OK" in out
        info = " ".join(ln for ln in out.splitlines() if ln.startswith("HOST="))
        self.status.setText("Connected" if ok_sm else "Connected - ScanManager missing")
        QMessageBox.information(self, "HK Connection", f"Target: {target}\n{info}\n\n"
                                + ("ScanManager found:\n" if ok_sm else "ScanManager NOT found at:\n") + sm)

    def _sync_run_number(self):
        if not ACCESS.unlocked:
            return
        try:
            self._apply_without_run_number()
        except (ValueError, OSError) as e:
            QMessageBox.critical(self, "Invalid HK Config", str(e))
            return
        self.status.setText("Reading HK data folder...")
        cfg = dict(self.hk)
        self._bg("sync", lambda: hk.probe_remote_runs(cfg))

    def _sync_done(self, res):
        if not res.get("ok"):
            self.status.setText("Folder read failed")
            QMessageBox.critical(self, "Sync Run Number", f"Could not list the HK data folder.\n\n"
                                 f"Folder: {res.get('dir', '?')}\n\n{res.get('error', '')}")
            return
        nxt, runs, d = res["next"], res["runs"], res["dir"]
        found = f"highest existing: Run{max(runs):03d}" if runs else "no Run-numbered files yet"
        if QMessageBox.question(self, "Sync Run Number", f"Folder: {d}\n{res['n_files']} file(s), {found}.\n\n"
                                f"Set Run Number to {nxt}?  (current: {self.hk.get('run_number')})") != QMessageBox.Yes:
            self.status.setText("Sync cancelled")
            return
        self.hk["run_number"] = nxt
        hk.save()
        self.run_no.setText(str(nxt))
        self.status.setText(f"Run number -> {nxt}")

    # ---------------------------------------------------------------- pipeline stages
    def _run_pipeline_stage(self, build_fn, name):
        if not ACCESS.unlocked:
            return
        try:
            self._apply_without_run_number()
        except (ValueError, OSError) as e:
            QMessageBox.critical(self, "Invalid HK Config", str(e))
            return
        try:
            cmd = (build_fn() or "").strip()
        except Exception as e:
            QMessageBox.critical(self, "Build Error", f"{name}: {e}")
            return
        if not cmd:
            QMessageBox.information(self, "Nothing to run", f"{name}: command is empty.")
            return
        slot, why = hk.run_in_console(self.hk, cmd, job_name=name)
        if slot is None:
            QMessageBox.warning(self, "Console Busy", why)
            return
        w = self.parent().window() if self.parent() is not None else None
        if w is not None and hasattr(w, "show_page"):
            w.show_page("Output")
            from pages.daq import output
            if output.live() is not None:
                output.live().focus(slot)

    def _run_stage_once(self, build_fn, name, flag, risk_note):
        if hk.session_flag(flag) and QMessageBox.question(
                self, f"{name} already run",
                f"{name} was already run this session.\n\n{risk_note}\n\nRun it again anyway?") != QMessageBox.Yes:
            return
        self._run_pipeline_stage(build_fn, name)
        hk.session_flag(flag, True)

    def _run_dpb_setup(self):
        self.status.setText("Checking remote DPB state...")
        QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
        try:
            count = hk.dpb_alive_count(self.hk)
        finally:
            QApplication.restoreOverrideCursor()
        self.status.clear()
        build = lambda: hk.build_dpb_setup(self.hk)   # noqa: E731
        if count == -1:
            self._run_stage_once(build, "DPB Setup", "dpb_setup_done",
                                 "Re-running can spawn a DUPLICATE socat/daq daemon if the first is still up.\n"
                                 "(Could not verify live remote state just now -- SSH check failed.)")
            return
        if count >= 2 and QMessageBox.question(
                self, "DPB already running remotely",
                f"Found {count} live 'socat' process(es) on the DPB board right now --\n"
                "DPB Setup already appears to be up and running.\n\n"
                "Running it again WILL spawn duplicate socat/daq daemons on the SAME ports.\n\n"
                "Run it again anyway?") != QMessageBox.Yes:
            return
        self._run_pipeline_stage(build, "DPB Setup")
        hk.session_flag("dpb_setup_done", True)

    def _locked_stage(self, fn, label):
        if not ACCESS.unlocked:
            return
        if QMessageBox.warning(
                self, f"{label} - normally not needed",
                "On THIS setup the DPB bridge is started and owned by the HK PC side, and it is already running.\n\n"
                "Running this stage can lay a second, differently-configured bridge over the working one and "
                "break the readout path.\n\nOnly continue if the HK-PC side has told you the bridge is down."
                "\n\nContinue to the password prompt?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        if not _password_ok(self, f"Enter Master Password ({label}):"):
            _log(f"[WARNING] {label} denied (admin unlock failed).")
            return
        fn()

    # ---------------------------------------------------------------- load
    def _apply_loaded(self, cfg, label):
        self.run_id.setText(cfg.get("run_id", self.run_id.text()))
        self.acq.setText(str(cfg.get("acq_time", self.acq.text())))
        self.delay.setText(str(cfg.get("move_delay", self.delay.text())))
        self.work_dir.setText(cfg.get("work_dir", self.work_dir.text()))
        self.opts.set_items(cfg.get("scan_options", []))
        self.steps.set_items(cfg.get("post_steps", []))
        note = ""
        if self.admin:
            uu, _, hh = str(cfg.get("ssh_target", "")).rpartition("@")
            for k, _l, _w in hk.PROTECTED:
                val = {"ssh_user": uu, "ssh_host": hh}.get(k, cfg.get(k))
                if val is not None:
                    self.pvars[k].setText(str(val))
        else:
            note = " (detail settings not loaded: unlock first)"
        self._changed()
        self.status.setText(f"Loaded {label}{note} - press Save to apply")

    def _load_dialog(self):
        LoadDialog(self).exec_()


def describe(cfg):
    """[(key, line)] of a config, keyed so two configs compare line by line."""
    rows = [("run_id", f"Filename      {cfg.get('run_id', '')}"),
            ("acq_time", f"Acq time      {cfg.get('acq_time', '?')} s"),
            ("move_delay", f"Move delay    {cfg.get('move_delay', '?')} s"),
            ("work_dir", f"Work dir      {cfg.get('work_dir', '')}"),
            ("", ""), ("", "ScanManager options (in order)")]
    for o in cfg.get("scan_options", []):
        val = "(flag only)" if o.get("bare") else (str(o.get("value", "")) or "(blank, not sent)")
        rows.append((f"opt:{o.get('flag')}",
                     f"  [{'x' if o.get('on') else ' '}] {o.get('label', ''):<18} {o.get('flag', ''):<14} {val}"))
    rows += [("", ""), ("", "Post-processing")]
    for p in cfg.get("post_steps", []):
        rows.append((f"step:{p.get('name')}", f"  [{'x' if p.get('on') else ' '}] {p.get('name', ''):<12} {p.get('cmd', '')}"))
    rows += [("", ""), ("", "Detail settings"), ("ssh_target", f"  SSH target    {cfg.get('ssh_target', '')}")]
    for k, label, _w in hk.PROTECTED:
        if k not in ("ssh_user", "ssh_host"):
            rows.append((k, f"  {label:<20} {cfg.get(k, '')}"))
    return rows


class LoadDialog(QDialog):
    """Saved configurations (hk_configs/), newest first; the run number is never loaded."""

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.setWindowTitle("Load HK config")
        self.resize(900, 720)
        v = QVBoxLayout(self)
        split = QSplitter(Qt.Vertical)
        self.list = QTreeWidget()
        self.list.setHeaderLabels(["Saved", "Filename", "Settings"])
        self.list.setRootIsDecorated(False)
        self.list.setColumnWidth(0, 160)
        self.list.setColumnWidth(1, 360)
        self.list.itemSelectionChanged.connect(self._on_select)
        self.list.itemDoubleClicked.connect(lambda *_: self._load())
        split.addWidget(self.list)
        lower = QWidget()
        lv = QVBoxLayout(lower)
        lv.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        self.view_lbl = QLabel("-")
        self.view_lbl.setStyleSheet("font-weight: 700;")
        bar.addWidget(self.view_lbl)
        bar.addStretch(1)
        self.load_btn = QPushButton("Load this")
        self.load_btn.clicked.connect(self._load)
        self.load_btn.setEnabled(False)
        bar.addWidget(self.load_btn)
        lv.addLayout(bar)
        self.box = QTextEdit()
        self.box.setReadOnly(True)
        self.box.setLineWrapMode(QTextEdit.NoWrap)
        self.box.setFont(_mono())
        self.box.setToolTip("* = differs from the settings on screen")
        lv.addWidget(self.box)
        split.addWidget(lower)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        v.addWidget(split, 1)
        bot = QHBoxLayout()
        browse = QPushButton("Browse file...")
        browse.clicked.connect(self._browse)
        bot.addWidget(browse)
        bot.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        bot.addWidget(close)
        v.addLayout(bot)
        self.viewed = None
        self.snaps = hk.snapshots()
        for _path, when, cfg in self.snaps:
            rid = str(cfg.get("run_id", ""))
            on = [p.get("name", "") for p in cfg.get("post_steps", []) if p.get("on")]
            info = (f"{cfg.get('acq_time', '?')} s ·{hk.option_args(cfg) or ' no options'}"
                    + (f" · post: {', '.join(on)}" if on else ""))
            self.list.addTopLevelItem(QTreeWidgetItem([when, rid if len(rid) <= 70 else rid[:69] + "...", info]))
        if self.snaps:
            self.list.setCurrentItem(self.list.topLevelItem(0))
        else:
            self.box.setPlainText("No saved configurations yet.")

    def _on_select(self):
        it = self.list.currentItem()
        i = self.list.indexOfTopLevelItem(it) if it is not None else -1
        if 0 <= i < len(self.snaps):
            self.view(self.snaps[i][2], self.snaps[i][1])

    def view(self, cfg, label):
        o = self.owner
        cur = dict(describe(o._live()))
        try:
            rn = int(float(o.run_no.text() or 0))
        except ValueError:
            rn = 0
        rid = str(cfg.get("run_id", "")).replace("{run}", f"{rn:03d}")
        try:
            cmd = hk.format_remote(cfg, rid, float(cfg.get("acq_time", 0)))
        except Exception as e:
            cmd = f"(cannot build: {e})"
        self.box.clear()
        cursor = self.box.textCursor()
        plain, diff = QTextCharFormat(), QTextCharFormat()
        plain.setForeground(QColor(theme.C["text"]))
        diff.setForeground(QColor(theme.C["warn"]))
        for key, line in describe(cfg):
            differs = bool(key) and cur.get(key) != line
            cursor.insertText(("* " if differs else "  ") + line + "\n", diff if differs else plain)
        cursor.insertText(f"\n  Command (run {rn:03d})\n  {cmd}\n", plain)
        self.box.moveCursor(QTextCursor.Start)
        self.view_lbl.setText(f"Viewing: {label}")
        self.viewed = (cfg, label)
        self.load_btn.setEnabled(ACCESS.unlocked)

    def _load(self):
        if self.viewed is None or not ACCESS.unlocked:
            return
        self.owner._apply_loaded(*self.viewed)
        self.accept()

    def _browse(self):
        p, _ = QFileDialog.getOpenFileName(self, "Load HK config", os.path.dirname(hk.CONFIG_FILE),
                                           "JSON (*.json);;All files (*)")
        if not p:
            return
        try:
            with open(p, encoding="utf-8") as f:
                cfg = hk.legacy_options(json.load(f))
        except Exception as e:
            QMessageBox.critical(self, "Load HK config", f"Could not read {p}:\n{e}")
            return
        self.view(cfg, os.path.basename(p))

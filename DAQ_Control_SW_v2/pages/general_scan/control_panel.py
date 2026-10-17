"""General Scan > Control Panel (Master), as ui_automation's dashboard: Operation Controls
(TEST RUN, DAQ backend, Start / Pause / Reset, ETA, Danger Zone with Abort and Params) and
the Manual Control Panel, Laser Sequence and Current Scan Parameters. The scan itself is the
Tk app's AutomationManager running on core.scan's host; this page only drives and shows it."""
import json
import os
import threading
import time

from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (QButtonGroup, QCheckBox, QDialog, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
                             QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QRadioButton, QScrollArea,
                             QVBoxLayout, QWidget)

from app import scan_bridge, theme
from core import env, status
from core.access import ACCESS, _admin_password
from core.stage import stage

TILT_RANGE, ROT_RANGE = (-55, 55), (0, 135)   # AutomationManager.scan_range / rot_range
WAVELENGTHS = ["375nm", "405nm", "450nm", "473nm"]


def _big(text, color=None, size=13):
    lab = QLabel(text)
    lab.setStyleSheet(f"color: {color or theme.C['accent']}; font-size: {size}pt; font-weight: 700;")
    return lab


class BackendCard(QFrame):
    def __init__(self, key, title, sub, on_click):
        super().__init__()
        self.key, self.on_click = key, on_click
        v = QVBoxLayout(self)
        t = self.title = QLabel(title)
        t.setAlignment(Qt.AlignCenter)
        s = QLabel(sub)
        s.setAlignment(Qt.AlignCenter)
        s.setObjectName("key")
        v.addWidget(t)
        v.addWidget(s)
        self.set_active(False)

    def set_active(self, on):
        c = theme.C
        strong, soft = theme.daq_color(self.key)
        self.setStyleSheet(f"BackendCard {{ border: {3 if on else 2}px solid {strong if on else c['border']};"
                           f" border-radius: 6px; background: {soft if on else c['card']}; }}")
        self.title.setStyleSheet(f"font-size: 12pt; font-weight: 700; color: {strong if on else c['text']};")

    def mousePressEvent(self, e):
        if self.isEnabled():
            self.on_click()


class _Bus(QObject):
    done = pyqtSignal(int, str, bool, str)
    angles = pyqtSignal(int, float, float)
    scan = pyqtSignal(str, object)


class ControlPanelPage(QWidget):
    def __init__(self):
        super().__init__()
        self.host = scan_bridge.init()
        self.am = self.host.auto_mgr
        self.sui = self.host.scan_ui
        self.bus = _Bus()
        self.bus.done.connect(self._on_done)
        self.bus.angles.connect(self._fill)
        self.bus.scan.connect(self._on_scan)
        self.sui.listeners.append(lambda ev, p: self.bus.scan.emit(ev, p))
        self.status_text = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 4, 12, 12)
        lay.setSpacing(12)
        lay.addWidget(self._left(), 1)
        lay.addWidget(self._right(), 1)
        self._load_sequence()
        ACCESS.listeners.append(self._paint_access)
        self._paint_access()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.refresh()

    # ---------------------------------------------------------------- left
    def _left(self):
        box = QGroupBox("Operation Controls")
        g = QVBoxLayout(box)
        top = QHBoxLayout()
        self.test_run = QCheckBox("TEST RUN (Simulation Mode)")
        self.test_run.setChecked(bool(self.sui.dummy_var.get()))
        self.test_run.toggled.connect(self._test_run)
        top.addWidget(self.test_run)
        top.addStretch(1)
        self.sys_status = _big("SYSTEM STATUS: IDLE", theme.C["hint"], 12)
        top.addWidget(self.sys_status)
        g.addLayout(top)

        sel = QGroupBox("DAQ Selection")
        sl = QVBoxLayout(sel)
        cards = QHBoxLayout()
        self.cards = {"caen": BackendCard("caen", "CAEN DAQ", "local DAQ config", lambda: self._select_backend("caen")),
                      "hk": BackendCard("hk", "HK Digitizer", "2nd PC - rotation-synced",
                                        lambda: self._select_backend("hk"))}
        for c in self.cards.values():
            cards.addWidget(c)
        sl.addLayout(cards)
        self.hk_btn = QPushButton("HK Digitizer Config")
        self.hk_btn.clicked.connect(self._hk_config)
        theme.role(self.hk_btn, "hk")
        sl.addWidget(self.hk_btn)
        g.addWidget(sel)

        run = QGroupBox("Run Control")
        rl = QVBoxLayout(run)
        btns = QHBoxLayout()
        self.start_btn, self.pause_btn, self.reset_btn = QPushButton("Start run"), QPushButton("Pause"), QPushButton("Reset")
        self.start_btn.clicked.connect(self._start)
        self.pause_btn.clicked.connect(self.am.handle_stop_continue)
        self.reset_btn.clicked.connect(self._reset)
        for b, kind in ((self.start_btn, "start"), (self.pause_btn, "warn"), (self.reset_btn, "neutral")):
            b.setProperty("variant", "large")
            theme.role(b, kind)      # colours of the Tk control panel
            btns.addWidget(b)
        rl.addLayout(btns)
        self.eta = _big("ETA: --:--:--", theme.C["muted"], 12)
        self.eta.setAlignment(Qt.AlignCenter)
        rl.addWidget(self.eta)
        self.checkpoint = QLabel()
        self.checkpoint.setWordWrap(True)
        self.checkpoint.setStyleSheet(f"font-weight: normal; color: {theme.C['warn']};")
        rl.addWidget(self.checkpoint)
        g.addWidget(run)

        danger = QGroupBox("Danger Zone")
        dl = QHBoxLayout(danger)
        self.abort_btn = QPushButton("Re-Run / Abort Scan")
        self.abort_btn.clicked.connect(self._abort)
        theme.role(self.abort_btn, "danger")
        self.abort_btn.setStyleSheet("QPushButton { font-size: 12pt; font-weight: 700; min-height: 60px; }")
        dl.addWidget(self.abort_btn, 2)
        side = QVBoxLayout()
        guide = QPushButton("Stop / Abort Guide")
        guide.clicked.connect(self._guide)
        side.addWidget(guide)
        params = QPushButton("Params")
        params.clicked.connect(self._params)
        theme.role(params, "warn")
        side.addWidget(params)
        dl.addLayout(side, 1)
        g.addWidget(danger)
        g.addStretch(1)
        return box

    # ---------------------------------------------------------------- right
    def _right(self):
        box = QGroupBox("Manual Control Panel")
        v = QVBoxLayout(box)
        self.dev, self.entries, self.manual = {}, {}, []
        for dev in (2, 3):
            lab = _big("-")
            v.addWidget(lab)
            row = QHBoxLayout()
            entries = {}
            for axis, name, rng in (("tilt", "Tilt:", TILT_RANGE), ("rot", "Rot:", ROT_RANGE)):
                row.addWidget(QLabel(name))
                e = QLineEdit("0.0")
                e.setFixedWidth(80)
                e.setAlignment(Qt.AlignCenter)
                entries[axis] = e
                self.manual.append(e)
                row.addWidget(e)
                r = QLabel(f"[{rng[0]}~{rng[1]}°]")
                r.setObjectName("hint")
                row.addWidget(r)
                row.addSpacing(10)
            row.addStretch(1)
            v.addLayout(row)
            brow = QHBoxLayout()
            for t, fn in (("Get Current", lambda _=False, d=dev: self._get_current(d)),
                          ("Move Tilt", lambda _=False, d=dev: self._move(d, "tilt")),
                          ("Move Rot", lambda _=False, d=dev: self._move(d, "rot")),
                          ("Stop", lambda _=False, d=dev: self._run(d, lambda: stage().stop(d), "Stop"))):
                b = QPushButton(t)
                b.clicked.connect(fn)
                theme.role(b, {"Get Current": "accent", "Stop": "warn"}.get(t, "move"))
                self.manual.append(b)
                brow.addWidget(b)
            lock = QLabel()
            lock.setStyleSheet(f"font-weight: 700; color: {theme.C['bad']};")
            brow.addWidget(lock)
            brow.addStretch(1)
            v.addLayout(brow)
            v.addSpacing(8)
            self.dev[dev] = (lab, lock)
            self.entries[dev] = entries

        seq = QGroupBox("Laser Sequence")
        sg = QGridLayout(seq)
        mode = QHBoxLayout()
        mode.addWidget(QLabel("Scan Mode:"))
        self.mode_group = QButtonGroup(self)
        self.mode_laser, self.mode_dark = QRadioButton("Laser (multi-wavelength)"), QRadioButton("Dark (single scan)")
        self.mode_laser.setChecked(self.sui.scan_mode_var.get() != "dark")
        self.mode_dark.setChecked(self.sui.scan_mode_var.get() == "dark")
        for rb in (self.mode_laser, self.mode_dark):
            self.mode_group.addButton(rb)
            mode.addWidget(rb)
        self.mode_group.buttonToggled.connect(self._scan_mode)
        mode.addStretch(1)
        sg.addLayout(mode, 0, 0, 1, 5)
        self.seq = {}
        for r, wl in enumerate(WAVELENGTHS, start=1):
            cb = QCheckBox(wl)
            sg.addWidget(cb, r, 0)
            sg.addWidget(QLabel("Bias"), r, 1, Qt.AlignRight)
            b = QLineEdit()
            b.setFixedWidth(70)
            sg.addWidget(b, r, 2)
            sg.addWidget(QLabel("Pulse"), r, 3, Qt.AlignRight)
            p = QLineEdit()
            p.setFixedWidth(70)
            sg.addWidget(p, r, 4)
            self.seq[wl] = (cb, b, p)
        save_row = QHBoxLayout()
        self.seq_saved = QLabel()
        self.seq_saved.setObjectName("hint")
        save_row.addStretch(1)
        save_row.addWidget(self.seq_saved)
        self.seq_save = QPushButton("Save Sequence")
        self.seq_save.clicked.connect(self._save_sequence_click)
        theme.role(self.seq_save, "start")
        save_row.addWidget(self.seq_save)
        sg.addLayout(save_row, len(WAVELENGTHS) + 1, 0, 1, 5)
        v.addWidget(seq)

        params = QGroupBox("Current Scan Parameters")
        pg = QGridLayout(params)
        self.param_vals = {}
        for i, (name, key) in enumerate((("Tilt Step", "tilt_step"), ("Rot Step", "rot_step"),
                                          ("Rest Time", "rest_time"), ("Settle Time", "daq_settle_time"))):
            pg.addWidget(QLabel(f"{name}:"), i // 2, (i % 2) * 2)
            self.param_vals[key] = _big("-", theme.C["text"], 12)
            pg.addWidget(self.param_vals[key], i // 2, (i % 2) * 2 + 1)
        pg.addWidget(QLabel("Repeat Angles:"), 2, 0)
        self.param_vals["repeat_angles"] = _big("-", theme.C["text"], 11)
        pg.addWidget(self.param_vals["repeat_angles"], 2, 1, 1, 3)
        pg.addWidget(QLabel("Rotate:"), 3, 0)
        self.param_vals["rotate_devices"] = _big("-", theme.C["text"], 11)
        pg.addWidget(self.param_vals["rotate_devices"], 3, 1, 1, 3)
        v.addWidget(params)
        v.addStretch(1)
        return box

    # ---------------------------------------------------------------- state
    def running(self):
        return bool(self.am.is_running)

    def _paint_access(self, on=None):
        on = ACCESS.unlocked if on is None else on
        run = self.running()
        idle_ok = on and not run
        self.start_btn.setEnabled(idle_ok)
        self.pause_btn.setEnabled(on and run)
        self.reset_btn.setEnabled(idle_ok)
        self.abort_btn.setEnabled(on)
        self.test_run.setEnabled(idle_ok)
        for c in self.cards.values():
            c.setEnabled(idle_ok)
        self.hk_btn.setEnabled(on and self.am.daq_backend == "hk")
        for w in self.manual:
            w.setEnabled(idle_ok)
        dark = self.sui.scan_mode_var.get() == "dark"
        for rb in (self.mode_laser, self.mode_dark):
            rb.setEnabled(idle_ok)
        self._paint_lasers(idle_ok and not dark)
        self.seq_save.setEnabled(idle_ok and not dark)
        for w in [self.start_btn, self.reset_btn, self.test_run, self.hk_btn, *self.manual]:
            w.setToolTip("" if on else "Unlock to use")

    def _paint_lasers(self, editable=None):
        """A wavelength whose laser is not connected cannot be selected."""
        from core.quick_setup import lasers_unavailable
        if editable is None:
            editable = ACCESS.unlocked and not self.running() and self.sui.scan_mode_var.get() != "dark"
        off = lasers_unavailable()
        for wl, (cb, b, p) in self.seq.items():
            dead = wl in off
            cb.setText(f"{wl} (not connected)" if dead else wl)
            cb.setEnabled(editable)             # stays editable so a dead row can still be unticked
            for w in (b, p):
                w.setEnabled(editable and not dead)

    def _on_scan(self, event, payload):
        if event == "running":
            is_running, text = payload
            self.status_text = (text or "SYSTEM STATUS: SCANNING...") if is_running else None
            self.pause_btn.setText("Pause")
            theme.role(self.pause_btn, "warn")
            self._paint_access()
            self.refresh()
        elif event == "pause_button":
            self.pause_btn.setText("Pause" if payload else "Continue")
            theme.role(self.pause_btn, "warn" if payload else "move")
        elif event == "status":
            text, color = payload
            if text:
                self.status_text = text
                self.sys_status.setText(text)
                self.sys_status.setStyleSheet(f"color: {color or theme.C['hint']}; font-size: 12pt; font-weight: 700;")
        elif event == "refresh":
            self.refresh()

    # ---------------------------------------------------------------- run control
    def _test_run(self, on):
        self.sui.dummy_var.set(bool(on))
        self.sui.add_auto_log(f"Test Run Mode: {'ENABLED' if on else 'DISABLED'}")

    def _select_backend(self, backend):
        if self.am.daq_backend == backend:
            return
        self.am.daq_backend = backend
        self.host._log("[INFO] DAQ backend -> HK Digitizer (2nd PC; config3.h ignored)." if backend == "hk"
                       else "[INFO] DAQ backend -> CAEN DAQ.")
        self._paint_access()
        self.refresh()

    def _hk_config(self):
        try:
            from pages.general_scan.hk_config import open_hk_config
        except ImportError:
            QMessageBox.information(self, "HK Digitizer Config", "The HK Digitizer Config dialog is not available.")
            return
        open_hk_config(self, self.am)

    def _start(self):
        from core.quick_setup import lasers_unavailable
        dead = sorted(wl for wl, (cb, _b, _p) in self.seq.items() if cb.isChecked() and wl in lasers_unavailable())
        if dead and not self.mode_dark.isChecked():
            QMessageBox.warning(self, "Laser not connected", f"{', '.join(dead)}: laser not connected.\n\n"
                                "Uncheck it in the Laser Sequence or connect the laser.")
            return
        self._save_sequence()
        if not self.mode_dark.isChecked() and not any(cb.isChecked() for cb, _b, _p in self.seq.values()):
            from pages.laser_check import confirm_laser_config
            if not confirm_laser_config(self):      # no wavelength blocks: the scan runs on the header as is
                return
        self.am.start_general_scan()
        self._paint_access()

    def _reset(self):
        self.host._log("[INFO] Reset angle button clicked.")
        msg = ("WARNING: Abort & Hardware Origin Reset\n\n"
               "This will ABORT the current run and physically move both SN2 and SN3 back to the origin (0.0°).\n"
               "The movement may take up to 30~60 seconds.\n\nDo you want to proceed and start over?")
        if QMessageBox.question(self, "Confirm Reset", msg) != QMessageBox.Yes:
            self.host._log("[INFO] Reset angle cancelled by user.")
            return
        self.host._log("[INFO] Reset angle confirmed by user -- starting.")
        self.am.abort_run()
        self.sui.reset_matrix()
        self.am.reset_all_angles()
        self.sui.add_auto_log("Run Aborted & Origin Reset Initiated: Moving SN2 & SN3 to 0.0°...")

    def _abort(self):
        msg = ("Abort & Stop Motors - shutdown sequence:\n"
               "   1. Immediately halt scan loop\n"
               "   2. Stop motors (SN2 · SN3)\n"
               "   3. Force-kill DAQ process (execute_DAQ_v2)\n"
               "   4. Reset UI\n\n"
               "What should happen to the recovery checkpoint?\n\n"
               "  [Yes]     Abort + DELETE checkpoint  ->  next Start begins fresh from -55°\n"
               "  [No]      Abort + KEEP checkpoint    ->  next Start can resume from last point\n"
               "  [Cancel]  Do nothing - return to scan\n\n"
               "The last step's data may be incomplete regardless of choice.")
        r = QMessageBox.warning(self, "Re-Run / Abort Scan", msg,
                                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel, QMessageBox.Cancel)
        if r == QMessageBox.Cancel:
            return
        self.am.emergency_stop()
        if r == QMessageBox.Yes:
            try:
                if os.path.exists(self.am.state_file):
                    os.remove(self.am.state_file)
            except OSError as e:
                self.host._log(f"[WARNING] Failed to clear recovery state: {e}")
            self.am.resume_data = None
            self.sui.add_auto_log("Recovery checkpoint deleted - next Start will begin fresh from -55°.")
        else:
            self.sui.add_auto_log("Recovery checkpoint kept - next Start will offer resume.")
        self._paint_access()

    def _guide(self):
        QMessageBox.information(self, "Stop / Abort Guide", (
            "Pause / Continue\n"
            "   - Waits for the current step to finish, then holds at the next checkpoint.\n"
            "   - Motors and DAQ are NOT killed; all progress is preserved.\n"
            "   - Press Continue to resume from exactly where it paused.\n\n"
            "Re-Run / Abort Scan\n"
            "   1. Halt scan loop immediately\n"
            "   2. Stop motors (SN2 · SN3)\n"
            "   3. Force-kill DAQ process\n"
            "   4. Reset UI\n"
            "   - A dialog will ask: [Yes] delete checkpoint (fresh start from -55°)\n"
            "     / [No] keep checkpoint (resume available at next Start)\n"
            "     / [Cancel] abort nothing.\n\n"
            "Starting completely fresh\n"
            "   - In the Abort dialog choose [Yes] (deletes checkpoint), OR\n"
            "   - When 'Recovery Found' appears at Start, click [No] to discard it.\n\n"
            "Reset angle\n"
            "   - Independently moves motors back to 0° (Tilt first, then Rot).\n\n"
            "Summary: use Pause to pause and resume; use Abort to stop completely."))

    def _params(self):
        pwd, ok = QInputDialog.getText(self, "Security", "Enter Master Password (Scan Parameters):", QLineEdit.Password)
        if not ok:
            return
        if pwd != _admin_password():
            QMessageBox.critical(self, "Error", "Incorrect password.")
            self.host._log("[WARNING] Admin access denied for Scan Parameters.")
            return
        ParamsDialog(self, self.am, self.host).exec_()
        self.refresh()

    # ---------------------------------------------------------------- laser sequence
    def _scan_mode(self, *_):
        self.sui.scan_mode_var.set("dark" if self.mode_dark.isChecked() else "laser")
        self._paint_access()

    def _load_sequence(self):
        data = self.sui.load_laser_sequence()
        for wl, (cb, b, p) in self.seq.items():
            s = data.get(wl, {})
            cb.setChecked(bool(s.get("on")))
            b.setText(str(s.get("bias", "")))
            p.setText(str(s.get("pulse", "")))

    def _save_sequence(self):
        data = {wl: {"on": cb.isChecked(), "bias": b.text(), "pulse": p.text()} for wl, (cb, b, p) in self.seq.items()}
        try:
            self.sui.save_laser_sequence(data)
        except OSError as e:
            self.host._log(f"[WARNING] Could not save laser sequence: {e}")

    def _save_sequence_click(self):
        self._save_sequence()
        self.seq_saved.setText("Saved")
        QTimer.singleShot(2000, lambda: self.seq_saved.setText(""))

    # ---------------------------------------------------------------- stage control
    def _run(self, dev, fn, what):
        """fn() -> (ok, msg) runs off the GUI thread; the result comes back through the bus."""
        def work():
            try:
                ok, msg = fn()
            except Exception as e:
                ok, msg = False, repr(e)
            self.bus.done.emit(dev, what, ok, msg)
        threading.Thread(target=work, daemon=True).start()

    def _on_done(self, dev, what, ok, msg):
        if not ok:
            QMessageBox.warning(self, f"SN{dev} {what}", msg)

    def _move(self, dev, axis):
        text = self.entries[dev][axis].text().strip()
        try:
            value = float(text)
        except ValueError:
            QMessageBox.warning(self, f"SN{dev}", f"Not a number: {text!r}")
            return

        def fn():
            ok, msg = stage().move(dev, axis, value)
            if ok:   # like the Tk panel: when the stage stops, write its angles into the config header
                time.sleep(1.0)
                mgr = stage().mgr
                while mgr is not None and mgr.is_moving.get(dev, False):
                    time.sleep(0.5)
                tilt, rot = stage().read_now(dev)
                if tilt is not None:
                    sn = self.sui.sn2_val if dev == 2 else self.sui.sn3_val
                    self.sui.update_config_angles(sn, tilt, rot)
                    self.host._log(f"[INFO] Synced {sn} sequence: Hardware -> Config -> UI "
                                   f"(Tilt: {tilt}°, Rot: {rot}°)")
                    self.bus.angles.emit(dev, tilt, rot)
            return ok, msg
        self._run(dev, fn, f"Move {axis}")

    def _get_current(self, dev):
        def fn():
            tilt, rot = stage().read_now(dev)
            if tilt is None:
                return False, "No reading from the drive."
            self.bus.angles.emit(dev, tilt, rot)
            return True, ""
        self._run(dev, fn, "Get Current")

    def _fill(self, dev, tilt, rot):
        self.entries[dev]["tilt"].setText(f"{tilt:.1f}")
        self.entries[dev]["rot"].setText(f"{rot:.1f}")

    # ---------------------------------------------------------------- data
    def refresh(self):
        self._paint_lasers()
        run = self.running()
        if run:
            text = self.status_text or "SYSTEM STATUS: SCANNING..."
            self.sys_status.setText(text)
            self.sys_status.setStyleSheet(f"color: {theme.C['bad']}; font-size: 12pt; font-weight: 700;")
            res = self.am.get_eta_seconds() if hasattr(self.am, "get_eta_seconds") else None
            if res:
                eta, cur, total = res
                m, s = divmod(int(eta), 60)
                h, m = divmod(m, 60)
                paused = not self.am.pause_event.is_set()
                self.eta.setText(f"Paused  |  ETA {h:02d}:{m:02d}:{s:02d}  ({cur}/{total})" if paused
                                 else f"ETA {h:02d}:{m:02d}:{s:02d}   ({cur}/{total} steps)")
        else:
            other = status.scan_running()   # e.g. a scan started from the Tk app
            self.sys_status.setText("SYSTEM STATUS: SCANNING" if other else
                                    "SYSTEM STATUS: SCHEDULED" if getattr(self.am, "is_scheduled", False) else
                                    "SYSTEM STATUS: IDLE")
            self.sys_status.setStyleSheet(f"color: {theme.C['good'] if other else theme.C['hint']}; "
                                          "font-size: 12pt; font-weight: 700;")
            self.eta.setText("ETA: --:--:--")
        if run != getattr(self, "_was_running", None):
            self._was_running = run
            self._paint_access()

        ck = os.path.join(env.DAQ_DIR, "scan_recovery_state.json")
        try:
            st = json.load(open(ck))
            self.checkpoint.setText(f"Checkpoint kept: {st.get('axis', '?')}-axis, tilt {st.get('tilt', '?')}° "
                                    "- the next Start run can resume.")
        except (OSError, ValueError):
            self.checkpoint.setText("")
        self.checkpoint.setVisible(bool(self.checkpoint.text()))

        for key, card in self.cards.items():
            card.set_active(key == self.am.daq_backend)

        am = self.am
        self.param_vals["tilt_step"].setText(f"{am.tilt_step}°")
        self.param_vals["rot_step"].setText(f"{am.rot_step}°")
        self.param_vals["rest_time"].setText(f"{am.rest_time}s")
        self.param_vals["daq_settle_time"].setText(f"{am.daq_settle_time}s")
        reps = am.repeat_angles
        self.param_vals["repeat_angles"].setText(", ".join(f"{t:g}@{r:g}" for t, r in reps) if reps else "none")
        self.param_vals["rotate_devices"].setText(self.sui._format_rotate_devices(am))
        both = am.rotate_dev2 and am.rotate_dev3
        self.param_vals["rotate_devices"].setStyleSheet(
            f"color: {theme.C['text'] if both else theme.C['warn']}; font-size: 11pt; font-weight: 700;")

        cfg = env.read_config()
        live = status.live_angles()
        for dev, (lab, lock) in self.dev.items():
            sn = cfg.get(f"SN{dev}", "") or "N/A"
            cable = (cfg.get(f"direction{dev}") or "").strip().upper()
            head = f"SN{dev} | {sn}-{cable}" if cable else f"SN{dev} | {sn}"
            if dev in live:
                tilt, rot, moving = live[dev]
                lab.setText(f"{head} | Status -> Tilt: {tilt:.1f}°, Rot: {rot:.1f}°" + ("  (moving)" if moving else ""))
                lock.setText("Tilt to 0° before rotating" if abs(tilt) > 0.5 else "")
            else:
                lab.setText(f"{head} | Status -> no live reading")
                lock.setText("")


class ParamsDialog(QDialog):
    """Scan Parameters (admin), the Tk app's open_scan_params."""

    def __init__(self, parent, am, host):
        super().__init__(parent)
        self.am, self.host = am, host
        self.setWindowTitle("Scan Parameters (Admin)")
        self.resize(460, 640)
        v = QVBoxLayout(self)
        area = QScrollArea()
        area.setWidgetResizable(True)
        body = QWidget()
        bl = QVBoxLayout(body)
        steps = QGroupBox("Scan Parameters")
        g = QGridLayout(steps)
        self.fields = {}
        for r, (label, key) in enumerate((("Tilt Step (deg)", "tilt_step"), ("Rot Step (deg)", "rot_step"),
                                          ("Rest Time (sec)", "rest_time"), ("Settle Time (sec)", "daq_settle_time"))):
            g.addWidget(QLabel(label), r, 0)
            e = QLineEdit(str(getattr(am, key)))
            e.setAlignment(Qt.AlignCenter)
            e.setFixedWidth(110)
            g.addWidget(e, r, 1, Qt.AlignRight)
            self.fields[key] = e
        bl.addWidget(steps)
        rep = QGroupBox("Repeat Angles")
        self.rep_grid = QGridLayout(rep)
        self.rep_grid.addWidget(QLabel("Rotation (deg)"), 0, 0)
        self.rep_grid.addWidget(QLabel("Tilt (deg)"), 0, 1)
        self.rep_rows = []
        for t, rr in am.repeat_angles:
            self._add_row(rr, t)
        while len(self.rep_rows) < 3:
            self._add_row("", "")
        add = QPushButton("+ Add row")
        add.clicked.connect(lambda: self._add_row("", ""))
        bl.addWidget(rep)
        bl.addWidget(add, 0, Qt.AlignLeft)
        rot = QGroupBox("Rotate Devices")
        rv = QVBoxLayout(rot)
        self.dev2 = QCheckBox("Rotate PMT1 (Rot1 / Device 2)")
        self.dev2.setChecked(getattr(am, "rotate_dev2", True))
        self.dev3 = QCheckBox("Rotate PMT2 (Rot2 / Device 3)")
        self.dev3.setChecked(getattr(am, "rotate_dev3", True))
        rv.addWidget(self.dev2)
        rv.addWidget(self.dev3)
        bl.addWidget(rot)
        bl.addStretch(1)
        area.setWidget(body)
        v.addWidget(area, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel, save = QPushButton("Cancel"), QPushButton("Save Parameters")
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self._save)
        theme.role(save, "start")
        row.addWidget(cancel)
        row.addWidget(save)
        v.addLayout(row)

    def _add_row(self, rot, tilt):
        r = len(self.rep_rows) + 1
        re_ = QLineEdit(f"{rot:g}" if isinstance(rot, (int, float)) else str(rot))
        te = QLineEdit(f"{tilt:g}" if isinstance(tilt, (int, float)) else str(tilt))
        for e in (re_, te):
            e.setAlignment(Qt.AlignCenter)
        self.rep_grid.addWidget(re_, r, 0)
        self.rep_grid.addWidget(te, r, 1)
        self.rep_rows.append((re_, te))

    def _save(self):
        am = self.am
        try:
            vals = {k: float(e.text()) for k, e in self.fields.items()}
        except ValueError:
            QMessageBox.critical(self, "Invalid Value",
                                 "Tilt Step / Rot Step / Rest Time / Settle Time must all be numbers.")
            return
        for k, val in vals.items():
            setattr(am, k, val)
        angles = []
        try:
            for re_, te in self.rep_rows:
                rs, ts = re_.text().strip(), te.text().strip()
                if not rs and not ts:
                    continue
                if not rs or not ts:
                    raise ValueError("row half-filled")
                angles.append((float(ts), float(rs)))
        except ValueError:
            QMessageBox.critical(self, "Invalid Repeat Angles",
                                 "Each used row needs BOTH Rotation and Tilt as numbers.\n"
                                 "Leave a row completely empty to skip it.\n\nKeeping the previous value.")
            return
        am.repeat_angles = angles
        if not self.dev2.isChecked() and not self.dev3.isChecked():
            QMessageBox.critical(self, "Invalid Rotate Devices",
                                 "At least one of PMT1 (Rot1) / PMT2 (Rot2) must stay enabled --\n"
                                 "a scan with neither device rotating has nothing to move.\n\nKeeping the previous value.")
            return
        am.rotate_dev2, am.rotate_dev3 = self.dev2.isChecked(), self.dev3.isChecked()
        self.host._log(f"[INFO] Scan params updated: Tilt {am.tilt_step}°, Rot {am.rot_step}°, "
                       f"Rest {am.rest_time}s, Settle {am.daq_settle_time}s, Repeat Angles {am.repeat_angles}, "
                       f"Rotate PMT1={am.rotate_dev2} PMT2={am.rotate_dev3}")
        self.accept()

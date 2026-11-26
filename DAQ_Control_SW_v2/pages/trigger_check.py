"""Before an acquisition on the CAEN DAQ: the logic-unit trigger must not be faster than the readout (it
stays at the HK Digitizer rate over an app restart). One dialog: set the CAEN rate and go on, or cancel."""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QMessageBox

from core import routine


def confirm_trigger_rate(parent, hk):
    """-> True to proceed (trigger set to the CAEN rate if the operator chose so), False to cancel."""
    hz = None if hk else routine.caen_trigger_excess()
    if not hz:
        return True
    now, want = routine._fmt_hz(hz), routine._fmt_hz(routine.CAEN_TRIGGER_HZ)
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Warning)
    box.setWindowTitle("Trigger rate")
    box.setText(f"Trigger rate now: {now}\n\n{routine.trigger_rule_text()}\n\n"
                f"CAEN DAQ is selected. Set {want} and continue?")
    fix = box.addButton(f"Set {want}", QMessageBox.AcceptRole)
    box.addButton(QMessageBox.Cancel)
    box.setDefaultButton(fix)
    box.exec_()
    if box.clickedButton() is not fix:
        return False
    QApplication.setOverrideCursor(Qt.WaitCursor)
    try:
        _old, err = routine.set_trigger_rate(routine.CAEN_TRIGGER_HZ, "v2 GUI (CAEN DAQ start)")
    finally:
        QApplication.restoreOverrideCursor()
    if err:
        routine._log(f"[ERROR] {err} - not started.")
        QMessageBox.critical(parent, "Trigger rate", f"{err}\n\nNot started.")
        return False
    routine._log(f"[INFO] Trigger rate {now} -> {want} for CAEN DAQ.")
    return True

"""Before an acquisition: the header's Wavelength / Laser must match the LD that is ON, since every
run records the header values. One dialog: save and go on, go on as is, or cancel."""
from PyQt5.QtWidgets import QMessageBox

from core import env
from core import quick_setup as qs


def confirm_laser_config(parent):
    """-> True to proceed (header saved if the operator chose so), False to cancel."""
    try:
        diffs = qs.laser_config_mismatch()
    except Exception:
        return True
    if not diffs:
        return True
    lines = "\n".join(f"  {k}:   config3.h {old or '-'}   ->   laser {new}" for k, old, new in diffs)
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Warning)
    box.setWindowTitle("Configuration not saved")
    box.setText(f"The active laser differs from config3.h:\n\n{lines}\n\nRuns record the config3.h values.")
    save = box.addButton("Save and continue", QMessageBox.AcceptRole)
    go = box.addButton("Continue as is", QMessageBox.DestructiveRole)
    box.addButton(QMessageBox.Cancel)
    box.setDefaultButton(save)
    box.exec_()
    if box.clickedButton() is save:
        vals = {k: new for k, _old, new in diffs}
        wl, ma = qs.read_live_laser()
        try:
            qs.save_laser_config(env.config_file(), vals.get("Wavelength", wl), vals.get("Laser", ma))
        except OSError as e:
            QMessageBox.critical(parent, "Configuration", f"Could not write config3.h:\n{e}")
            return False
        return True
    if box.clickedButton() is go:
        qs._log("[WARNING] Acquisition started with config3.h differing from the active laser: "
                + "; ".join(f"{k} {old or '-'} vs {new}" for k, old, new in diffs))
        return True
    return False

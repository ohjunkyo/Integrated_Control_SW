"""PATLITE NE-USB beacon for the Signal Lamp tab: the shared PatliteLamp of core.laser (one HID
handle per process, as the Tk tab uses laser_mgr.patlite) and the Tk tab's value mapping."""
import importlib.util
import os
import subprocess

from core import env

USB_ID = "191a:6001"
_driver = None


def driver():
    """managers/patlite_lamp.py of the Tk app (Tk-free), loaded by path."""
    global _driver
    if _driver is None:
        path = os.path.join(env.DAQ_DIR, "managers", "patlite_lamp.py")
        spec = importlib.util.spec_from_file_location("patlite_lamp", path)
        _driver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_driver)
    return _driver


def lamp():
    """-> (PatliteLamp or None, reason when None)."""
    if env.NO_HW:
        return None, "Hardware access is off (V2_NO_HW)."
    try:
        from core import laser
        ctl = laser.laser()
    except Exception as e:
        return None, f"Lamp driver not available: {e}"
    if not ctl.owns():
        return None, "The Tk DAQ app is open and drives the lamp."
    if ctl.lamp is None:
        return None, "Lamp driver not available."
    return ctl.lamp, None


def on_usb():
    try:
        return USB_ID in subprocess.run(["lsusb"], capture_output=True, text=True, timeout=5).stdout.lower()
    except Exception:
        return False


def light_args(color, pattern, buzzer, continuous, volume):
    """Combo texts -> set_light(color, pattern, buzzer=, buzzer_count=, volume=) as the Tk _lamp_apply."""
    d = driver()
    return ((d.LED_COLORS.get(color, 0), d.LED_PATTERNS.get(pattern, 1)),
            {"buzzer": d.BUZZER_PATTERNS.get(buzzer, 0),
             "buzzer_count": d.BUZZER_COUNT_CONTINUOUS if continuous else d.BUZZER_COUNT_KEEP,
             "volume": int(volume)})


TOUCH_TEXT = {1: "ON", 0: "OFF", -1: "read failed / no sensor"}

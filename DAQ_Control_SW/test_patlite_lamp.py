#!/usr/bin/env python3
"""
Standalone CLI test for the PATLITE NE-USB lamp, independent of the DAQ app.
Run this after physically plugging the lamp in (tomorrow onsite) to confirm
USB permissions + the driver work before relying on the interlock hook.

Usage:
    python3 test_patlite_lamp.py alarm   # flashing red + strong buzzer (simulates interlock trip)
    python3 test_patlite_lamp.py clear   # lamp off, buzzer off (simulates interlock recovery)
    python3 test_patlite_lamp.py green   # steady green (simulates "safe" state)
"""
import sys
from managers.patlite_lamp import PatliteLamp

def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("alarm", "clear", "green"):
        print(__doc__)
        sys.exit(1)

    lamp = PatliteLamp(log_fn=print)
    action = sys.argv[1]
    ok = {
        "alarm": lamp.alarm_interlock,
        "clear": lamp.clear,
        "green": lamp.ok_green,
    }[action]()

    print(f"[{action}] {'OK' if ok else 'FAILED — check lsusb / udev permissions'}")

if __name__ == "__main__":
    main()

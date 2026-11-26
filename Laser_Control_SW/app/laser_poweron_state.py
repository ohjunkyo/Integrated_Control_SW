#!/usr/bin/env python3
"""Read or clear the LDB boards' power-on LD/TEC state (EEPROM byte 0, tmHIDLD.dll InitialLDON/InitialTECON).

usage: laser_poweron_state.py read            [--wl 405nm ...]
       laser_poweron_state.py set-off          [--wl 405nm ...]   # clear "LD on at power-up" (TEC left as is)
Refuses to run while a DAQ control app is open (they own the HID handles). Results are appended to
LOG/LASER/laser_log_<wl>_<date>.txt.
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

APP_DIR = "/home/precalkor/Integrated_Control_SW/Laser_Control_SW/app"
LOG_DIR = os.path.expanduser("~/ADC/ADC_test/LOG/LASER")
APP_CONFIG = os.path.expanduser("~/.daq_control_config.json")
DEFAULT_PORTS = {"375nm": "1-3.4.4:1.0", "405nm": "1-3.4.1:1.0", "450nm": "1-3.4.2:1.0", "473nm": "1-3.4.3:1.0"}


def log(wl, msg):
    print(f"[{wl}] {msg}")
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, f"laser_log_{wl}_{datetime.now():%Y-%m-%d}.txt"), "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [INFO] power-on state tool: {msg}\n")
    except OSError:
        pass


def control_app_running():
    out = subprocess.run(["pgrep", "-af", "DAQ_Control_SW(_v2)?/main.py"], capture_output=True, text=True).stdout
    return [l for l in out.splitlines() if "pgrep" not in l]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["read", "set-off"])
    ap.add_argument("--wl", action="append", help="wavelength(s); default all four")
    ap.add_argument("--driver-dir", default=APP_DIR, help="directory holding laser_driver.py")
    ap.add_argument("--force", action="store_true", help="run even if a control app is open")
    a = ap.parse_args()

    running = control_app_running()
    if running and not a.force:
        print("A DAQ control app is running; close it first (or --force):\n  " + "\n  ".join(running))
        return 2

    sys.path.insert(0, a.driver_dir)
    from laser_driver import TamadenshiLaser
    try:
        with open(APP_CONFIG) as f:
            ports = dict(DEFAULT_PORTS, **(json.load(f).get("laser_port_mapping") or {}))
    except (OSError, ValueError):
        ports = DEFAULT_PORTS
    wls = a.wl or list(DEFAULT_PORTS)

    rc = 0
    for wl in wls:
        inst = TamadenshiLaser()
        ok, msg = inst.connect(dev_path=ports[wl].encode())
        if not ok:
            print(f"[{wl}] not connected ({ports[wl]}): {msg.splitlines()[0]}")
            rc = 1
            continue
        try:
            st = inst.get_power_on_state()
            if st is None:
                log(wl, "EEPROM read failed")
                rc = 1
                continue
            log(wl, f"EEPROM byte 0 = 0x{st['byte0']:02X}: power-on LD {'ON' if st['ld_on'] else 'off'}, "
                    f"TEC {'ON' if st['tec_on'] else 'off'}")
            if a.mode == "set-off" and st["ld_on"]:
                inst.update_status()
                if inst.status.get("ld_on", False):
                    log(wl, "LD is ON now - not writing the EEPROM; switch the LD off first")
                    rc = 1
                    continue
                ok, msg = inst.set_power_on_state(ld_on=False)
                log(wl, ("power-on LD cleared: " if ok else "FAILED: ") + msg)
                if not ok:
                    rc = 1
        finally:
            inst.disconnect()
    return rc


if __name__ == "__main__":
    sys.exit(main())

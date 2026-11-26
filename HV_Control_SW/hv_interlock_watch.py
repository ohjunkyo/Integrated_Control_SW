#!/usr/bin/env python3
"""Standalone HV interlock/channel-status watcher, independent of the main
HV_Control_SW GUI (which isn't running). Polls the board every 5s and logs
ONLY on change, with a timestamp, so we get an exact record of when
IlkStat/ChStatus flips -- useful for correlating "HV disconnects" against
whatever else was happening at that moment (physical interlock test, etc).
Ctrl-C to stop.
"""
import time, json, sys
from datetime import datetime
from caen_libs import caenhvwrapper as hv

CONFIG_PATH = "/home/precalkor/Integrated_Control_SW/HV_Control_SW/config_precal.json"
LOG_PATH = "/home/precalkor/Integrated_Control_SW/HV_Control_SW/LOG/hv_interlock_watch.log"

with open(CONFIG_PATH) as f:
    cfg = json.load(f)["caen_hv_settings"]

import os
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

def log(msg):
    line = f"{datetime.now().isoformat(timespec='seconds')}  {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")

def connect():
    return hv.Device.open(hv.SystemType[cfg["system_type"]], hv.LinkType[cfg["link_type"]],
                           cfg.get("connection_argument", ""), cfg.get("username", ""), cfg.get("password", ""))

device = None
last_ilk = None
last_ch_status = {}
log("=== HV interlock watcher started ===")

while True:
    try:
        if device is None:
            device = connect()
            log("Connected to HV.")

        try:
            ilk = device.get_bd_param([0], 'IlkStat')[0]
        except Exception as e:
            ilk = f"ERR({e})"

        if ilk != last_ilk:
            log(f"IlkStat changed: {last_ilk} -> {ilk}")
            last_ilk = ilk

        for ch in range(4):
            try:
                st = device.get_ch_param(0, [ch], 'ChStatus')[0]
            except Exception as e:
                st = f"ERR({e})"
            if last_ch_status.get(ch) != st:
                log(f"Ch{ch} ChStatus changed: {last_ch_status.get(ch)} -> {st}")
                last_ch_status[ch] = st

    except Exception as e:
        log(f"Connection lost/error: {e} -- reconnecting in 2s")
        try:
            if device: device.close()
        except Exception:
            pass
        device = None
        time.sleep(2)
        continue

    time.sleep(5)

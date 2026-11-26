#!/usr/bin/env python3
"""UPS guard: owns the OMRON BA100R serial port and runs the power-failure shutdown.

On battery for GRACE_S, or battery <= BATT_TRIGGER_PCT (or the UPS low-battery flag): stop DAQ, switch every
laser LD/TEC off, ramp every HV channel down (Pw = 0), wait for HV < HV_SAFE_V, then power the PC off.
DAQ Control v2 and the HV monitor act on the request themselves; when an app is not running the guard drives
that hardware directly. Status goes to Slack through the supervisor outbox.
"""
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import instance_lock  # noqa: E402
import power_fail  # noqa: E402

GRACE_S = 20 * 60
BATT_TRIGGER_PCT = 50
POLL_S = 10
NO_READING_ON_BATTERY_S = 180      # UPS silent while on battery: act as if it were empty
HV_SAFE_V = 50.0
HV_WAIT_S = 6 * 60
APP_ACK_S = 60

VID, PID, BAUD = 0x067B, 0x23A3, 2400
RATED_W = 800
BATT_EMPTY_V, BATT_FULL_V = 21, 27.5
UPS_CSV_DIR = os.path.join(ROOT, "DAQ_Control_SW", "LOG", "UPS")
HV_CONFIG = os.path.join(ROOT, "HV_Control_SW", "config_precal.json")
HV_DB = os.path.join(ROOT, "HV_Control_SW", "monitoring_log.db")
V2_UPS = os.path.join(ROOT, "DAQ_Control_SW_v2", "core", "ups.py")
APP_CONFIG = os.path.expanduser("~/.daq_control_config.json")
LASER_PORTS = {"375nm": "1-3.4.4:1.0", "405nm": "1-3.4.1:1.0", "450nm": "1-3.4.2:1.0", "473nm": "1-3.4.3:1.0"}
OUTBOX = os.path.expanduser("~/.cache/network_watchdog/outbox")
LOG = os.path.expanduser("~/logs/ups_guard.log")
DRY = os.environ.get("UPS_GUARD_DRY") == "1"     # tests: log the actions instead of doing them


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def slack(level, msg):
    log(f"[SLACK {level}] {msg}")
    try:
        os.makedirs(os.path.dirname(OUTBOX), exist_ok=True)
        with open(OUTBOX, "a") as f:
            f.write(f"{level}\tUPS: {msg}\n")
    except OSError:
        pass


# ---------------------------------------------------------------- UPS
def parse_q1(text):
    """'(MMM.M NNN.N PPP.P QQQ RR.R S.SS TT.T b7..b0' -> dict, or None."""
    text = text.strip()
    if not text.startswith("("):
        return None
    d = text[1:].split()
    if len(d) < 7:
        return None
    try:
        vin, vout, load, freq, batt_v, temp = (float(d[0]), float(d[2]), float(d[3]), float(d[4]),
                                               float(d[5]), float(d[6]))
    except ValueError:
        return None
    bits = d[7] if len(d) > 7 and len(d[7]) == 8 else ""
    pct = min(100, max(0, int((batt_v - BATT_EMPTY_V) / (BATT_FULL_V - BATT_EMPTY_V) * 100)))
    return {"vin": vin, "vout": vout, "load": load, "freq": freq, "batt_v": batt_v, "temp": temp,
            "watt": RATED_W * load / 100.0, "batt_pct": pct, "bits": bits,
            "on_battery": (bits[0] == "1") if bits else vin < 10,
            "battery_low": bits[1] == "1" if bits else False}


def append_csv(r, now):
    """Same file and format as the v2 UPS page (it reads this history)."""
    if r["vout"] <= 0.5:
        return
    try:
        os.makedirs(UPS_CSV_DIR, exist_ok=True)
        path = os.path.join(UPS_CSV_DIR, f"ups_{now:%Y%m%d}.csv")
        new = not os.path.isfile(path)
        with open(path, "a") as f:
            if new:
                f.write("timestamp,Watt,Temp,Vin,Vout\n")
            f.write(f"{now.isoformat()},{r['watt']:.1f},{r['temp']:.1f},{r['vin']:.1f},{r['vout']:.1f}\n")
    except OSError as e:
        log(f"[WARNING] UPS CSV: {e}")


class Ups:
    def __init__(self):
        self.ser = None

    def _open(self):
        import serial
        import serial.tools.list_ports
        port = next((p.device for p in serial.tools.list_ports.comports()
                     if (p.vid or 0) == VID and (p.pid or 0) == PID), None)
        if not port:
            return False
        self.ser = serial.Serial(port, BAUD, timeout=1, exclusive=True)
        log(f"UPS port {port} opened")
        return True

    def close(self):
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None

    def query(self, cmd=b"Q1\r", wait=0.4):
        try:
            if self.ser is None and not self._open():
                return None
            self.ser.reset_input_buffer()
            self.ser.write(cmd)
            time.sleep(wait)
            n = self.ser.in_waiting
            return self.ser.read(n).decode("ascii", errors="ignore") if n else ""
        except Exception as e:
            log(f"[WARNING] UPS I/O: {e}")
            self.close()
            return None

    def read(self):
        resp = self.query()
        return parse_q1(resp) if resp else None


# ---------------------------------------------------------------- shutdown steps
def running(name):
    try:
        return instance_lock.is_running(name)
    except Exception:
        return False


def wait_ack(who, req, seconds):
    end = time.time() + seconds
    while time.time() < end:
        if power_fail.acked(who, req):
            return True
        time.sleep(2)
    return False


def daq_pids():
    out = subprocess.run(["pgrep", "-af", "ADC_test/script_v7.sh|execute_DAQ_v2"], capture_output=True, text=True).stdout
    return [int(l.split()[0]) for l in out.splitlines() if " -j" not in l and "pgrep" not in l]


def stop_daq(req):
    if running("daq_v2"):
        wait_ack("v2", req, APP_ACK_S)
    pids = daq_pids()
    for pid in pids:
        log(f"DAQ: SIGTERM {pid}")
        if not DRY:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
    return "DAQ stopped" if pids else "no DAQ running"


def lasers_off_direct():
    sys.path.insert(0, os.path.join(ROOT, "Laser_Control_SW", "app"))
    from laser_driver import TamadenshiLaser
    try:
        with open(APP_CONFIG) as f:
            ports = dict(LASER_PORTS, **(json.load(f).get("laser_port_mapping") or {}))
    except (OSError, ValueError):
        ports = LASER_PORTS
    done = []
    for wl, port in ports.items():
        inst = TamadenshiLaser()
        ok, _msg = inst.connect(dev_path=port.encode())
        if not ok:
            continue
        try:
            inst.set_ld_on(False)
            inst.set_tec_on(False)
            done.append(wl)
        finally:
            inst.disconnect()
    return done


LASER_APPS = (("v2", "daq_v2"), ("tk", "daq"))      # apps that switch their own lasers off and acknowledge
HID_HOLDERS = ("daq_v2", "daq", "laser_gui")        # locks whose process may hold the laser boards
APP_EXIT_S = 15


def wait_laser_acks(req, apps, seconds):
    """-> (wavelengths switched off by the apps, apps that did not answer)."""
    end, off, answered = time.time() + seconds, set(), set()
    while True:
        for who in apps:
            info = power_fail.ack_info(who, req)
            if info is not None:
                answered.add(who)
                off.update(info.get("lasers") or [])
        if len(answered) == len(apps) or time.time() >= end:
            return sorted(off), [w for w in apps if w not in answered]
        time.sleep(2)


def release_boards():
    """Terminate the apps holding laser boards so the guard can open them. -> names stopped"""
    stopped = []
    for name in HID_HOLDERS:
        info = instance_lock.holder(name) if running(name) else None
        pid = int(info["pid"]) if info and info.get("pid") else 0
        if not pid:
            continue
        log(f"Lasers: SIGTERM {name} (pid {pid}) to release the laser boards")
        stopped.append(name)
        if DRY:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            continue
        end = time.time() + APP_EXIT_S
        while time.time() < end and os.path.exists(f"/proc/{pid}"):
            time.sleep(0.5)
        if os.path.exists(f"/proc/{pid}"):
            log(f"Lasers: SIGKILL {name} (pid {pid})")
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            time.sleep(1)
    return stopped


def stop_lasers(req):
    apps = [who for who, lock in LASER_APPS if running(lock)]
    prefix = ""
    if apps:
        off, silent = wait_laser_acks(req, apps, APP_ACK_S)
        if off and not silent:
            return f"lasers off ({', '.join(apps)}: {', '.join(off)})"
        prefix = (f"{', '.join(silent)} did not answer; " if silent else "apps reported no laser switched off; ")
    stopped = release_boards()
    if stopped:
        prefix += f"stopped {', '.join(stopped)}; "
    if DRY:
        return prefix + "lasers off (direct, dry run)"
    try:
        done = lasers_off_direct()
        return prefix + f"lasers off (direct: {', '.join(done) or 'none reachable'})"
    except Exception as e:
        return prefix + f"laser off failed: {e}"


def hv_config():
    with open(HV_CONFIG) as f:
        return json.load(f)["caen_hv_settings"]


def hv_direct(set_off):
    """Open the crate, optionally Pw = 0 on every channel, and return the highest VMon."""
    from caen_libs import caenhvwrapper as hv
    c = hv_config()
    p, chs = c["parameters"], c["channels_to_monitor"]
    dev = hv.Device.open(hv.SystemType[c["system_type"]], hv.LinkType[c["link_type"]],
                         c.get("connection_argument", ""), c.get("username", ""), c.get("password", ""))
    try:
        if set_off:
            dev.set_ch_param(0, chs, p["pw"], 0)
        return max(dev.get_ch_param(0, chs, p["v_mon"]))
    finally:
        dev.close()


def hv_logged_max():
    """Highest channel voltage in the HV monitor's log, or None if it is older than 30 s."""
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{HV_DB}?mode=ro", uri=True, timeout=2)
        row = con.execute("SELECT timestamp, Ch0_V, Ch1_V, Ch2_V, Ch3_V FROM monitoring_data "
                          "ORDER BY timestamp DESC LIMIT 1").fetchone()
        con.close()
    except Exception:
        return None
    if not row or time.time() - datetime.fromisoformat(row[0]).timestamp() > 30:
        return None
    return max(v for v in row[1:] if v is not None)


def stop_hv(req):
    app = running("hv")
    if app and not wait_ack("hv", req, APP_ACK_S):
        log("HV monitor did not confirm Pw = 0; trying the crate directly")
        app = False
    if DRY:
        return "HV off (dry run)"
    if not app:
        try:
            hv_direct(True)
        except Exception as e:
            return f"HV off FAILED: {e}"
    end = time.time() + HV_WAIT_S
    vmax = None
    while time.time() < end:
        try:
            vmax = hv_logged_max() if app else hv_direct(False)
        except Exception:
            vmax = None
        if vmax is not None and vmax < HV_SAFE_V:
            return f"HV down (max {vmax:.0f} V)"
        time.sleep(10)
    return f"HV NOT confirmed down after {HV_WAIT_S // 60} min (last max {vmax} V)"


def power_off_pc():
    if DRY:
        return True, "dry run"
    r = subprocess.run(["sudo", "-n", "/usr/bin/systemctl", "poweroff"], capture_output=True, text=True)
    return r.returncode == 0, (r.stderr or r.stdout).strip()


def shutdown(reason, ups):
    req = power_fail.post(reason)
    slack("critical", f"Power-failure shutdown started ({reason}). Stopping DAQ, lasers and HV, then the PC.")
    steps = [stop_daq(req), stop_lasers(req), stop_hv(req)]
    log("Steps: " + " | ".join(steps))
    r = ups.read()
    if r is not None and not r["on_battery"]:
        slack("warning", "Power is back before the PC power-off. " + "; ".join(steps) +
              ". The PC stays on; restart HV and lasers by hand.")
        return False
    slack("critical", "; ".join(steps) + ". Powering the PC off now.")
    ok, msg = power_off_pc()
    if not ok:
        slack("critical", f"PC power-off refused ({msg or 'no sudo rule'}). HV and lasers are off; the PC "
                          "will stop when the UPS battery runs out.")
    return ok


# ---------------------------------------------------------------- main loop
def stale_v2():
    """A DAQ v2 started before its UPS code changed may still hold the port without yielding to us."""
    try:
        mt = os.path.getmtime(V2_UPS)
        info = instance_lock.holder("daq_v2")
        pid = int(info["pid"]) if info and info.get("pid") else 0
        return bool(pid) and os.stat(f"/proc/{pid}").st_ctime < mt
    except Exception:
        return False


def main():
    held = instance_lock.acquire("ups_guard")
    if held is not None:
        sys.exit(f"UPS guard already running (pid {held.get('pid')})")
    ups = Ups()
    since = last_ok = None
    low_count = 0
    warned_v2 = warned_silent = triggered = False
    log(f"UPS guard started (grace {GRACE_S // 60} min, battery trigger {BATT_TRIGGER_PCT} %, dry={DRY})")
    while True:
        if stale_v2():
            if not warned_v2:
                log("DAQ v2 runs older UPS code and may hold the port; waiting for its restart")
                warned_v2 = True
            ups.close()
            time.sleep(POLL_S)
            continue
        warned_v2 = False
        now = datetime.now()
        r = ups.read()
        if r is not None:
            last_ok, warned_silent = time.time(), False
            append_csv(r, now)
            if r["on_battery"] and since is None:
                since = time.time()
                slack("critical", f"Power failure: UPS on battery ({r['batt_pct']} %, {r['watt']:.0f} W). "
                                  f"Automatic shutdown in {GRACE_S // 60} min or at {BATT_TRIGGER_PCT} % battery.")
            elif not r["on_battery"] and since is not None:
                slack("info", f"Power restored after {(time.time() - since) / 60:.1f} min.")
                since, triggered = None, False
                power_fail.clear()
            low_count = low_count + 1 if r["on_battery"] and r["batt_pct"] <= BATT_TRIGGER_PCT else 0
            power_fail.write_state(dict(r, ts=now.isoformat(), since=since, triggered=triggered))
        elif last_ok is None or time.time() - last_ok > 120:
            if not warned_silent:
                slack("warning", "The UPS does not answer on its serial port.")
                warned_silent = True

        reason = None
        if since is not None and not triggered:
            if time.time() - since >= GRACE_S:
                reason = f"on battery for {GRACE_S // 60} min"
            elif r is not None and (low_count >= 2 or r["battery_low"]):
                reason = f"battery at {r['batt_pct']} %" + (" (UPS low-battery flag)" if r["battery_low"] else "")
            elif r is None and last_ok and time.time() - last_ok > NO_READING_ON_BATTERY_S:
                reason = "UPS silent while on battery"
        if reason:
            triggered = True
            if shutdown(reason, ups):
                return
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()

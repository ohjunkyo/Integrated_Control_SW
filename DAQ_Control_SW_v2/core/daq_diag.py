"""CAEN digitizer connection diagnosis (same checks and verdicts as the Tk DAQ
Diag tab). The only check that touches the hardware is `execute_DAQ_v2 -j`;
it is skipped while a scan is taking data."""
import os
import subprocess

CAUSES = [
    ("Digitizer not responding", "Kernel logs 'read: maxretry timeout': power-cycle the digitizer."),
    ("Digitizer not on USB", "No CAEN device in lsusb: check its power switch and USB cable."),
    ("Driver not loaded", "CAENUSBdrvB is not loaded: load it (sudo)."),
    ("Driver not built", "No CAENUSBdrvB for this kernel (after an update): reinstall drivers (sudo, slow)."),
    ("Permission denied", "User lacks dialout/plugdev: add the groups (sudo), then log in again."),
    ("Just switched on", "USB needs 2-5 s to appear: wait, then run diagnostics again."),
]
MANUAL = ["Load driver", "Load driver at boot", "Reinstall drivers", "Add USB groups"]   # sudo tools (pkexec)
CHECKS = [("Kernel module", "lsmod | grep -i caen || echo '[NOT LOADED] CAENUSBdrvB'"),
          ("USB devices", "lsusb | grep -i caen || echo '[NOT FOUND] no CAEN USB device'"),
          ("Read timeouts", "journalctl -k --since '-6h' --no-pager | grep 'maxretry timeout' | tail -5"),
          ("Groups", "id")]


def _sh(cmd, timeout=15):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except subprocess.TimeoutExpired:
        return ""


def probe_env(base_path):
    """execute_DAQ_v2 needs the 64-bit CAEN lib dir ahead of a stale system copy."""
    env = os.environ.copy()
    extra = [d for d in (os.path.join(os.path.dirname(base_path.rstrip("/")), "lib"), "/opt/root/lib")
             if os.path.isdir(d)]
    if extra:
        env["LD_LIBRARY_PATH"] = ":".join(extra + ([env["LD_LIBRARY_PATH"]] if env.get("LD_LIBRARY_PATH") else []))
    return env


def run_check(cmd):
    return _sh(cmd) or "(no output)"


def diagnose(base_path, scanning, log):
    """-> (ok, title, fix). log(text) receives the raw findings."""
    loaded = bool(_sh("lsmod | grep -i CAENUSBdrvB"))
    built = bool(_sh("modinfo -F filename CAENUSBdrvB 2>/dev/null"))
    on_usb = bool(_sh("lsusb | grep -i caen"))
    hangs = _sh("journalctl -k --since '-3h' --no-pager 2>/dev/null | grep -c 'maxretry timeout'") or "0"
    log(f"driver loaded: {loaded}   driver built: {built}   on USB: {on_usb}   read timeouts (3 h): {hangs}")
    if scanning:
        log("execute_DAQ_v2 -j: skipped (a scan is taking data)")
        if loaded and on_usb:
            return True, "DAQ busy with a scan", "Driver loaded and digitizer on USB; the probe was skipped."
        probe_ok, out = False, ""
    else:
        try:
            r = subprocess.run([os.path.join(base_path, "execute_DAQ_v2"), "-j"], capture_output=True, text=True,
                               timeout=8, env=probe_env(base_path))
            probe_ok, out = r.returncode == 0, (r.stdout + r.stderr).strip()
        except Exception as e:
            probe_ok, out = False, str(e)
        log(f"execute_DAQ_v2 -j: {'OK' if probe_ok else 'FAILED'}  {out[-200:]}")
    if probe_ok:
        return True, "DAQ connected", "The digitizer answers."
    if not built:
        return False, "Driver not built for this kernel", "Reinstall the CAEN drivers (takes a few minutes)."
    if not loaded:
        return False, "Driver not loaded", "Load the CAENUSBdrvB driver."
    if not on_usb:
        return False, "Digitizer not on USB", "Check the digitizer power switch and USB cable, then run again."
    if "ermission" in out:
        return False, "Permission denied", "Add this user to dialout/plugdev, then log out and in."
    if int(hangs) > 0:
        return False, "Digitizer not responding", "Switch the digitizer off, wait 10 s, switch it on, run again."
    return False, "DAQ not connected", "Unknown cause - see Output and Known problems."

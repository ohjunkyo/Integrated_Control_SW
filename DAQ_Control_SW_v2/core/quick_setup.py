"""Quick Setup logic as the Tk app's AutomationUI does it (ui_automation.py): the Quick
Configuration keys saved into the config header (+ DAQ rebuild), the live hardware values
pushed into the form (HV from the HV monitor DB, Laser/Wavelength from the one LD that is
on), the laser -> config sync before a manual run, and the handover notes JSON Lines file.
Qt-free."""
import json
import os
import re
import sqlite3
from datetime import datetime

from core import env

# Quick Configuration keys, Tk qs_vars order
KEYS = ["Shift_worker", "Expert", "NOTE", "Laser", "Wavelength", "BField", "BField_X", "BField_Y", "BField_ZTop",
        "BField_ZBottom", "SN1", "HV1", "direction1", "RotateAngle1", "TiltAngle1", "SN2", "HV2", "direction2",
        "RotateAngle2", "TiltAngle2", "SN3", "HV3", "direction3", "RotateAngle3", "TiltAngle3"]
NOTES = os.path.join(env.DAQ_DIR, "handover_notes.jsonl")
HV_DB = env.HV_DB
HV_STALE_AFTER_S = 180
# Readback jitters around the setpoint; only a larger difference is a real setting change
TOLERANCE = {"HV1": 5.0, "HV2": 5.0, "HV3": 5.0, "Laser": 1.0}


def _log(msg):
    from core import applog
    applog.log(msg)


# ---------------------------------------------------------------- config header
def save_settings(path, values):
    """Write the Quick Configuration values (every key, as the Tk save_from_ui) and rebuild
    execute_DAQ_v2. -> rebuild warning or None. Raises OSError when the header cannot be written."""
    from core import config
    config.save(path, {k: str(values.get(k, "")).strip() for k in KEYS if k in values})
    return config.rebuild_daq(env.ADC_DIR)


def apply_laser_config(path, wl, pulse, bias=0.0):
    """Wavelength = <nm>, Laser = round(pulse + bias) in the header (atomic), as _apply_laser_config."""
    try:
        with open(path, "r") as f:
            content = f.read()
        nm = wl.replace("nm", "")
        total = float(pulse) + float(bias)
        content = re.sub(r'const std::string Wavelength\s*=\s*".*";',
                         f'const std::string Wavelength = "{nm}";', content)
        content = re.sub(r'const std::string Laser\s*=\s*".*";',
                         f'const std::string Laser = "{round(total)}";', content)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _log(f"[INFO] config3.h updated: Wavelength={nm}, "
             f"Laser={total:g} mA (pulse {float(pulse):g} + bias {float(bias):g})")
    except Exception as e:
        _log(f"[WARNING] Failed to update laser config: {e}")


def laser_controller():
    if env.NO_HW:
        return None
    try:
        from core import laser
        return laser.laser()
    except Exception:
        return None


def lasers_unavailable(ctl=None):
    """Wavelengths whose laser is not connected right now; empty when this app does not own the lasers."""
    ctl = ctl or laser_controller()
    if ctl is None or not getattr(ctl, "inst", None):
        return set()
    try:
        if not ctl.owns():
            return set()
        return {wl for wl in ctl.inst if not ctl.connected(wl) or ctl.comm_error.get(wl, False)}
    except Exception:
        return set()


def _lds_on(ctl):
    on = []
    for wl in getattr(ctl, "inst", {}):
        try:
            if ctl.connected(wl) and ctl.status(wl).get("ld_on", False):
                on.append(wl)
        except Exception:
            continue
    return on


def sync_config_from_active_laser(path, ctl=None):
    """Before a manual (non-dark) run: record the one LD that is ON into the header; with 0 or
    several on, leave the header alone (as _sync_config_from_active_laser)."""
    try:
        ctl = ctl or laser_controller()
        if ctl is None:
            return
        on = _lds_on(ctl)
        if len(on) != 1:
            if on:
                _log(f"[WARNING] Laser config not auto-synced: {len(on)} LDs ON "
                     f"({', '.join(on)}). Edit config3.h manually if needed.")
            return
        wl = on[0]
        st = ctl.status(wl)
        s = ctl.settings.get(wl)
        pulse = bias = None
        try:
            if st.get("pulse") is not None:
                pulse = float(st["pulse"])
            if st.get("bias") is not None:
                bias = float(st["bias"])
        except (TypeError, ValueError):
            pass
        if pulse is None or bias is None:
            if s is None:
                return
            pulse = float(s.pulse) if pulse is None else pulse
            bias = float(s.bias) if bias is None else bias
            _log(f"[WARNING] {wl} live current unreadable; recording the "
                 f"entry-box value ({pulse + bias:g} mA) instead.")
        elif s is not None:
            try:
                typed = float(s.pulse) + float(s.bias)
                if abs(typed - (pulse + bias)) > 1.0:
                    _log(f"[INFO] {wl} current: recording {pulse + bias:g} mA from hardware "
                         f"(entry boxes show {typed:g} mA).")
            except (TypeError, ValueError):
                pass
        apply_laser_config(path, wl, pulse, bias)
    except Exception as e:
        _log(f"[WARNING] Laser config auto-sync failed: {e}")


# ---------------------------------------------------------------- live hardware -> form
def read_live_laser(ctl=None):
    """-> (wavelength_nm, total_mA) of the single LD that is ON, else (None, None)."""
    ctl = ctl or laser_controller()
    if ctl is None:
        return None, None
    on = []
    for wl in getattr(ctl, "inst", {}):
        try:
            if not ctl.connected(wl) or ctl.comm_error.get(wl, False):
                continue
            st = ctl.status(wl)
            if st.get("ld_on", False):
                on.append((wl, st))
        except Exception:
            continue
    if len(on) != 1:
        return None, None
    wl, st = on[0]
    try:
        total = float(st.get("pulse", 0) or 0) + float(st.get("bias", 0) or 0)
    except (TypeError, ValueError):
        return None, None
    return re.sub(r"\D", "", wl), f"{round(total)}"


def laser_config_mismatch(path=None):
    """[(key, in_header, live)] where the header's Wavelength/Laser differ from the one LD that is ON.
    Empty when they agree, or when no or several LDs are on (nothing to compare against)."""
    wl, ma = read_live_laser()
    if wl is None:
        return []
    from core import config
    return live_changes(config.parse(path or env.config_file()), {"Wavelength": wl, "Laser": ma})


def save_laser_config(path, wavelength, laser_ma):
    """Wavelength / Laser only, in place (script_v7.sh reads them per run; no rebuild needed)."""
    from core import config
    config.save(path, {"Wavelength": wavelength, "Laser": laser_ma})
    _log(f"[INFO] config3.h updated from the active laser: Wavelength={wavelength}, Laser={laser_ma} mA")


def read_live_hv(db_path=None):
    """{'HV1','HV2','HV3'} from the HV monitor DB (Ch0-2), {} when missing or stale."""
    db_path = db_path or HV_DB
    if not os.path.exists(db_path):
        return {}
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
        try:
            row = con.execute("SELECT timestamp, Ch0_V, Ch1_V, Ch2_V FROM monitoring_data "
                              "ORDER BY timestamp DESC LIMIT 1").fetchone()
        finally:
            con.close()
    except Exception:
        return {}
    if not row or row[0] is None:
        return {}
    try:
        if (datetime.now() - datetime.fromisoformat(row[0])).total_seconds() > HV_STALE_AFTER_S:
            return {}
    except Exception:
        return {}
    out = {}
    for key, val in zip(("HV1", "HV2", "HV3"), row[1:]):
        try:
            v = float(val)
        except (TypeError, ValueError):
            continue
        if v > 0:
            out[key] = f"{round(v)}"
    return out


def live_changes(current, updates):
    """[(key, old, new)] of live values that differ beyond the readback deadband."""
    out = []
    for key, val in updates.items():
        cur = (current.get(key) or "").strip()
        if cur == val:
            continue
        tol = TOLERANCE.get(key)
        if tol is not None and cur:
            try:
                if abs(float(cur) - float(val)) <= tol:
                    continue
            except ValueError:
                pass
        out.append((key, cur, val))
    return out


# ---------------------------------------------------------------- handover notes
def load_notes(path=None):
    """Chronological entries of the JSON Lines file (bad lines skipped)."""
    entries = []
    with open(path or NOTES, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def append_note(author, body, ts=None, path=None):
    ts = ts or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(path or NOTES, "a", encoding="utf-8") as f:
        f.write(json.dumps({"time": ts, "author": author, "note": body}) + "\n")
    return ts


def write_notes(entries, path=None):
    """Rewrite the whole file (edit/delete), atomic."""
    path = path or NOTES
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

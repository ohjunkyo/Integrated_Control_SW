"""Laser drivers (Tamadenshi LD boards on USB HID), same rules as the Tk app's LaserManager:
one LD on at a time, bias+pulse <= 200 mA, pulse width behind the admin password, a 1 s
safety watchdog (interlock or USB loss forces LD/TEC off and clears the signal lamp; the lamp
is steady red while an LD is on), automatic reconnect of lasers the operator connected, and
the same CSV / text logs in LOG/LASER. The device driver is the Tk app's laser_driver.py.

Ownership: v2 talks to the boards only while the Tk app is closed; when it opens, v2 closes
every HID handle. Qt-free: listeners(event, wl, payload) are called from worker threads.
"""
import collections
import json
import os
import sys
import threading
import time
import types
from datetime import datetime

from core import env  # puts instance_lock on sys.path
import instance_lock  # noqa: E402

WAVELENGTHS = ["375nm", "405nm", "450nm", "473nm"]
LD_TOTAL_CURRENT_LIMIT_MA = 200.0
TEC_MIN_C, TEC_MAX_C = 10.0, 30.0        # operating window for the TEC setpoint (board maximum is 40 C)
PULSE_WIDTH_MIN_PS, PULSE_WIDTH_MAX_PS = 100, 10230
FORCE_CONNECT_PASSWORD = "1234"     # Tk app's 'Force Connect (Admin)' after an interlock
TRIGGER_MODES = ["External", "Internal (PG1)", "Internal (PG2)"]
APP_CONFIG = os.path.expanduser("~/.daq_control_config.json")   # shared with the Tk app
DEFAULT_PORTS = {"375nm": "1-3.4.4:1.0", "405nm": "1-3.4.1:1.0", "450nm": "1-3.4.2:1.0", "473nm": "1-3.4.3:1.0"}
LOG_DIR = os.path.join(env.ADC_DIR, "LOG", "LASER")
RECONNECT_COOLDOWN_S = 5
LAMP_REASSERT_S = 5


def _driver_class():
    path = os.path.join(os.path.dirname(env.APP_DIR), "Laser_Control_SW", "app")
    if path not in sys.path:
        sys.path.append(path)
    from laser_driver import TamadenshiLaser
    return TamadenshiLaser


def _app_config():
    try:
        with open(APP_CONFIG) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_connected(wls):
    data = _app_config()
    data["last_connected_wls"] = wls
    tmp = APP_CONFIG + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=4)
    os.replace(tmp, APP_CONFIG)


class Settings:
    """Per-wavelength operator settings (the Tk tab's entry boxes)."""

    def __init__(self):
        self.bias, self.pulse = 0.0, 0.0
        self.trigger_mode, self.freq_hz = "External", "10000000"
        self.pulse_width_default_ps = None


class LaserController:
    def __init__(self):
        cfg = _app_config()
        self.ports = {wl: (cfg.get("laser_port_mapping") or DEFAULT_PORTS).get(wl, DEFAULT_PORTS[wl])
                      for wl in WAVELENGTHS}
        self.last_connected = list(cfg.get("last_connected_wls") or [])
        self.inst = {}
        self.settings = {wl: Settings() for wl in WAVELENGTHS}
        self.comm_error = {wl: False for wl in WAVELENGTHS}
        self.reason = {wl: "USB" for wl in WAVELENGTHS}          # "USB" or "INTERLOCK"
        self.expected = set()                                     # operator-connected: auto-reconnect these
        self._reconnecting, self._last_reconnect = set(), {}
        self._asking = set()                                      # recovery prompt open for these
        self._last_csv = {wl: 0.0 for wl in WAVELENGTHS}
        self.history = {wl: {k: collections.deque(maxlen=90000)
                             for k in ("time", "temp", "pulse", "bias", "ld_on", "pd_current")} for wl in WAVELENGTHS}
        self.listeners = []
        self.lamp = None
        self.app = types.SimpleNamespace()   # the engine checks lm.app.ui for Tk widgets; v2 has none
        self._lock = threading.RLock()
        self._started = False

    # ---- plumbing ------------------------------------------------------------
    def _emit(self, event, wl, payload=None):
        for fn in list(self.listeners):
            try:
                fn(event, wl, payload)
            except Exception:
                pass

    def log(self, wl, msg):
        """Session log line: LOG/LASER/laser_log_<wl>_<date>.txt (as the Tk app) + the GUI."""
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            path = os.path.join(LOG_DIR, f"laser_log_{wl}_{datetime.now():%Y-%m-%d}.txt")
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
        except OSError:
            pass
        self._emit("log", wl, msg)

    def owns(self):
        return not instance_lock.is_running("daq")

    def start(self):
        if self._started or env.NO_HW:
            return
        self._started = True
        cls = _driver_class()
        self.inst = {wl: cls() for wl in WAVELENGTHS}
        try:
            from managers.patlite_lamp import PatliteLamp
            self.lamp = PatliteLamp(log_fn=lambda m: self._emit("log", None, m))
        except Exception as e:
            self._emit("log", None, f"[WARNING] Signal lamp unavailable: {e}")
        threading.Thread(target=self._watchdog, daemon=True).start()

    # names the General Scan engine (Tk AutomationManager) uses on the Tk app's LaserManager
    @property
    def laser_instances(self):
        return self.inst

    @property
    def laser_port_mapping(self):
        return {wl: p.encode() for wl, p in self.ports.items()}

    @property
    def expected_connections(self):
        return self.expected

    @property
    def comm_error_flags(self):
        return self.comm_error

    def connected(self, wl):
        inst = self.inst.get(wl)
        return bool(inst and inst.is_connected())

    def status(self, wl):
        inst = self.inst.get(wl)
        return dict(inst.status) if inst else {}

    # ---- connect -------------------------------------------------------------
    def connect(self, wl):
        """-> (ok, message). Applies the trigger settings like the Tk app does on connect."""
        if not self.owns():
            return False, "The Tk DAQ app is open and owns the lasers."
        inst = self.inst.get(wl)
        if inst is None:
            return False, "Laser drivers are not started."
        port = self.ports[wl]
        self.log(wl, f"Connecting to {wl} via {port}...")
        ok, msg = inst.connect(dev_path=port.encode())
        if not ok:
            self.log(wl, f"[ERROR] {wl} Connection Failed: {msg}")
            return False, msg
        self.expected.add(wl)
        self.comm_error[wl] = False
        inst.update_status()
        self.apply_trigger(wl, self.settings[wl].trigger_mode, self.settings[wl].freq_hz)
        self.log(wl, f"{wl} Connected successfully.")
        self._save_connections()
        self._emit("state", wl)
        return True, msg

    def disconnect(self, wl, user=True):
        self.expected.discard(wl)
        inst = self.inst.get(wl)
        if inst:
            try:
                inst.disconnect()
            except Exception:
                pass
        self.settings[wl].pulse_width_default_ps = None
        if user:
            self.log(wl, f"{wl} Disconnected by user.")
            self._save_connections()
        self._emit("state", wl)

    def _save_connections(self):
        try:
            _save_connected([wl for wl in WAVELENGTHS if self.connected(wl)])
        except OSError:
            pass

    # ---- commands (callers check Unlock) -------------------------------------
    def lasers_on(self, except_wl=None):
        return [wl for wl in WAVELENGTHS
                if wl != except_wl and self.connected(wl) and self.status(wl).get("ld_on", False)]

    def set_ld(self, wl, state):
        """Turning one on first turns the others off (the caller confirmed that). Runs in a thread."""
        if not self.connected(wl):
            return False
        others = self.lasers_on(except_wl=wl) if state else []

        def task():
            try:
                for other in others:
                    self.inst[other].set_ld_on(False)
                    self.log(other, f"[INFO] Safety: Auto-shutdown completed for {other}")
                if others:
                    time.sleep(0.5)
                self.inst[wl].set_ld_on(state)
                time.sleep(0.1)
                if self.lamp:
                    self.lamp.alarm_interlock() if state else self.lamp.clear()
                self.log(wl, f"[INFO] Command Sent: Laser {wl} LD -> {'ON' if state else 'OFF'}")
                self.inst[wl].update_status()
            except Exception as e:
                self.log(wl, f"[ERROR] LD control error for {wl}: {e}")
            self._emit("state", wl)
        threading.Thread(target=task, daemon=True).start()
        return True

    def set_tec(self, wl, state):
        if not self.connected(wl):
            return False

        def task():
            try:
                self.inst[wl].set_tec_on(state)
                self.log(wl, f"[INFO] Command Sent: Laser {wl} TEC -> {'ON' if state else 'OFF'}")
                time.sleep(0.5)
                self.inst[wl].update_status()
            except Exception as e:
                self.log(wl, f"[ERROR] TEC control error for {wl}: {e}")
            self._emit("state", wl)
        threading.Thread(target=task, daemon=True).start()
        return True

    def apply_currents(self, wl, bias, pulse):
        """-> (ok, message). The LD is rated for bias + pulse combined; refuse, never clamp."""
        bias, pulse = float(bias), float(pulse)
        if bias + pulse > LD_TOTAL_CURRENT_LIMIT_MA:
            msg = (f"{wl}: Bias ({bias:.1f} mA) + Pulse ({pulse:.1f} mA) = {bias + pulse:.1f} mA.\n\n"
                   f"The laser diode is rated for {LD_TOTAL_CURRENT_LIMIT_MA:.0f} mA COMBINED, not per setting."
                   "\n\nNothing was applied.")
            self.log(wl, f"[ERROR] {wl}: Bias {bias:.1f} + Pulse {pulse:.1f} = {bias + pulse:.1f} mA exceeds the "
                         f"{LD_TOTAL_CURRENT_LIMIT_MA:.0f} mA combined LD limit. Not applied.")
            return False, msg
        if not self.connected(wl):
            return False, f"{wl} is not connected."
        self.settings[wl].bias, self.settings[wl].pulse = bias, pulse

        def task():
            try:
                self.inst[wl].set_bias_current(bias)
                time.sleep(0.2)
                self.inst[wl].set_pulse_current(pulse)
                time.sleep(0.2)
                self.log(wl, f"[INFO] Applied to {wl}: Bias={bias:.2f}mA, Pulse={pulse:.2f}mA")
            except Exception as e:
                self.log(wl, f"[ERROR] Failed applying currents to {wl}: {e}")
            self._emit("state", wl)
        threading.Thread(target=task, daemon=True).start()
        return True, ""

    def apply_pulse_width(self, wl, width_ps):
        width_ps = int(width_ps)
        if not PULSE_WIDTH_MIN_PS <= width_ps <= PULSE_WIDTH_MAX_PS:
            return False, f"{width_ps} ps is out of the valid range ({PULSE_WIDTH_MIN_PS}-{PULSE_WIDTH_MAX_PS} ps)."
        if not self.connected(wl):
            return False, f"{wl} is not connected."

        def task():
            try:
                self.inst[wl].set_pulse_width_ps(width_ps)
                self.settings[wl].pulse_width_default_ps = width_ps
                self.log(wl, f"[INFO] Applied to {wl}: Pulse Width={width_ps}ps")
            except Exception as e:
                self.log(wl, f"[ERROR] Failed applying pulse width to {wl}: {e}")
            self._emit("state", wl)
        threading.Thread(target=task, daemon=True).start()
        return True, ""

    def read_temp_setpoint(self, wl):
        """TEC target temperature from the board, or None (synchronous HID read)."""
        if not self.connected(wl):
            return None
        try:
            return self.inst[wl].get_temp_setpoint()
        except Exception as e:
            self.log(wl, f"[ERROR] Failed reading the TEC setpoint of {wl}: {e}")
            return None

    def apply_temp_setpoint(self, wl, temp_c):
        """-> (ok, message). Range-limited; the driver clamps at the board's 40 C maximum."""
        temp_c = float(temp_c)
        if not TEC_MIN_C <= temp_c <= TEC_MAX_C:
            return False, f"{temp_c:g} C is out of the allowed range ({TEC_MIN_C:g}-{TEC_MAX_C:g} C)."
        if not self.connected(wl):
            return False, f"{wl} is not connected."

        def task():
            try:
                self.inst[wl].set_temp(temp_c)
                time.sleep(0.3)
                self.log(wl, f"[INFO] Applied to {wl}: TEC setpoint={temp_c:.1f}C")
            except Exception as e:
                self.log(wl, f"[ERROR] Failed applying the TEC setpoint to {wl}: {e}")
            self._emit("state", wl)
        threading.Thread(target=task, daemon=True).start()
        return True, ""

    def apply_trigger(self, wl, mode, freq_hz):
        """Trigger source and (internal modes) repetition rate."""
        try:
            hz = int(freq_hz)
        except (TypeError, ValueError):
            return False, f"Invalid frequency for {wl}. Must be an integer."
        self.settings[wl].trigger_mode, self.settings[wl].freq_hz = mode, str(hz)
        if not self.connected(wl):
            return True, ""
        pg1, pg2, ext = mode == "Internal (PG1)", mode == "Internal (PG2)", mode == "External"

        def task():
            try:
                self.inst[wl].set_trigger_mode(pg1, pg2, ext)
                time.sleep(0.1)
                if pg1:
                    self.inst[wl].set_pg1_frequency(hz)
                elif pg2:
                    self.inst[wl].set_pg2_frequency(hz)
                time.sleep(0.1)
                self.log(wl, f"[INFO] Laser {wl} Config: {mode}, {hz} Hz applied.")
            except Exception as e:
                self.log(wl, f"[ERROR] Failed applying frequency to {wl}: {e}")
            self._emit("state", wl)
        threading.Thread(target=task, daemon=True).start()
        return True, ""

    # ---- interlock / USB recovery ---------------------------------------------
    def post_reconnect(self, wl, admin=False, keep_on=False):
        """After a recovery prompt. Normal connect forces LD/TEC off; admin may keep the LD on."""
        inst = self.inst.get(wl)
        if inst is None:
            return
        inst.update_status()
        if inst.status.get("ld_on", False):
            if admin and keep_on:
                self.log(wl, f"[WARNING] Safety: {wl} kept ON by Admin.")
            else:
                inst.set_ld_on(False)
                inst.set_tec_on(False)
                self.log(wl, f"[INFO] Safety: {wl} forced OFF " + ("by Admin." if admin else
                                                                    "due to normal user privileges."))
        time.sleep(0.1)
        inst.update_status()
        self.comm_error[wl] = False
        self._asking.discard(wl)
        if not any(self.comm_error.values()) and self.lamp:
            self.lamp.clear()
        self.settings[wl].trigger_mode = "External"
        self.log(wl, f"[INFO] {wl} Trigger mode forced to External after interlock recovery.")
        self.apply_trigger(wl, "External", self.settings[wl].freq_hz)
        self._emit("state", wl)

    def cancel_recovery(self, wl):
        self.log(wl, f"[WARNING] Connection cancelled by user for {wl}.")
        self.disconnect(wl, user=False)
        self.comm_error[wl] = True
        self._asking.discard(wl)

    def _comm_failure(self, wl, reason):
        inst = self.inst.get(wl)
        if inst and inst.is_connected():
            try:
                inst.set_ld_on(False)
                inst.set_tec_on(False)
            except Exception:
                pass
        if not self.comm_error[wl]:
            self.reason[wl] = reason
            self.log(wl, f"[ {wl} ] USB connection lost - check cable/port." if reason == "USB"
                     else f"[ {wl} ] Interlock triggered - check safety circuit.")
            self.comm_error[wl] = True
        self._emit("fault", wl, reason)

    # ---- 1 s loop: watchdog, telemetry, reconnect ------------------------------
    def _watchdog(self):
        tick = 0
        while True:
            if not self.owns():
                for wl in WAVELENGTHS:
                    if self.connected(wl):
                        self.inst[wl].disconnect()
                        self._emit("state", wl)
                dev = getattr(self.lamp, "_dev", None)
                if dev is not None:   # the Tk app drives the lamp now
                    try:
                        dev.close()
                    except Exception:
                        pass
                    self.lamp._dev = None
                time.sleep(1.0)
                continue
            any_on = False
            for wl in WAVELENGTHS:
                try:
                    any_on |= self._poll(wl)
                except Exception as e:
                    self.log(wl, f"[ERROR] {wl} Comm Error: {e}")
            tick += 1
            if tick % LAMP_REASSERT_S == 0 and self.lamp:   # the lamp forgets its state if power-cycled
                if any(self.comm_error.values()) or not any_on:
                    self.lamp.clear()
                else:
                    self.lamp.alarm_interlock()
            time.sleep(1.0)

    def _poll(self, wl):
        inst = self.inst.get(wl)
        if inst is None:
            return False
        if not inst.is_connected():
            if wl in self.expected:
                if not self.comm_error[wl]:
                    self._comm_failure(wl, "USB")
                self._try_reconnect(wl)
            return False
        if not inst.update_status():
            if not self.comm_error[wl]:
                self._comm_failure(wl, "USB")
            return False
        st = inst.status
        if self.comm_error[wl]:              # it answers again: ask the operator once how to resume
            if wl not in self._asking:
                self._asking.add(wl)
                self._emit("recovered", wl, self.reason[wl])
            return False
        if st.get("alarm", False) or st.get("interlock", False):
            detect = time.time()
            self.reason[wl] = "INTERLOCK"
            self.comm_error[wl] = True
            try:
                inst.set_ld_on(False)
                inst.set_tec_on(False)
            except Exception:
                pass
            if self.lamp:
                self.lamp.clear()
            self.log(wl, f"[CRITICAL] Interlock tripped for {wl} (port {self.ports[wl]}) @ "
                         f"{datetime.fromtimestamp(detect).strftime('%H:%M:%S.%f')[:-3]} - LD/TEC forced OFF.")
            self._emit("fault", wl, "INTERLOCK")
            return False
        if self.settings[wl].pulse_width_default_ps is None:
            pw = inst.get_pulse_width_ps()
            if pw is not None:
                self.settings[wl].pulse_width_default_ps = pw
        ld_on, tec_on = st.get("ld_on", False), st.get("tec_on", False)
        h = self.history[wl]
        h["time"].append(datetime.now())
        h["temp"].append(st.get("ld_temp", 0.0))
        h["pulse"].append(st.get("pulse", 0.0))
        h["bias"].append(st.get("bias", 0.0))
        h["ld_on"].append(1 if ld_on else 0)
        h["pd_current"].append(st.get("pd_current", 0.0))
        now = time.time()
        if now - self._last_csv[wl] >= (10 if (ld_on or tec_on) else 60):
            self._last_csv[wl] = now
            self._save_csv(wl, st)
        self._emit("status", wl, dict(st))
        return ld_on

    def _try_reconnect(self, wl):
        if wl in self._reconnecting or time.time() - self._last_reconnect.get(wl, 0) < RECONNECT_COOLDOWN_S:
            return
        self._reconnecting.add(wl)
        self._last_reconnect[wl] = time.time()
        self.log(wl, f"[INFO] {wl} (port {self.ports[wl]}) Attempting auto-reconnect...")

        def task():
            try:
                self.inst[wl].connect(dev_path=self.ports[wl].encode())
            except Exception as e:
                self.log(wl, f"[WARNING] {wl} (port {self.ports[wl]}) reconnect failed: {e}")
            finally:
                self._reconnecting.discard(wl)
        threading.Thread(target=task, daemon=True).start()

    def _save_csv(self, wl, st):
        """LOG/LASER/laser_data_<wl>_<date>.csv, same columns as the Tk app."""
        s = self.settings[wl]
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            path = os.path.join(LOG_DIR, f"laser_data_{wl}_{datetime.now():%Y%m%d}.csv")
            new = not os.path.isfile(path)
            pd_ma = f"{st['pd_current'] * 1000.0:.4f}" if st.get("pd_valid") else ""
            pw = st.get("pulse_width_ps", "")
            with open(path, "a", buffering=1, encoding="utf-8") as f:
                if new:
                    f.write("timestamp,temp_c,pulse_ma,bias_ma,trigger_mode,freq_hz,ld_on,tec_on,"
                            "pd_raw,pd_current_ma,pulse_width_ps\n")
                f.write(f"{datetime.now().isoformat()},{st.get('ld_temp', 0.0):.2f},{st.get('pulse', 0.0):.2f},"
                        f"{float(s.bias):.2f},{s.trigger_mode},{s.freq_hz},{1 if st.get('ld_on') else 0},"
                        f"{1 if st.get('tec_on') else 0},{st.get('pd_raw', '')},{pd_ma},"
                        f"{pw if pw is not None else ''}\n")
        except OSError as e:
            self.log(wl, f"[ERROR] Laser Logging Error ({wl}): {e}")


_laser = None


def laser():
    global _laser
    if _laser is None:
        _laser = LaserController()
        _laser.start()
    return _laser

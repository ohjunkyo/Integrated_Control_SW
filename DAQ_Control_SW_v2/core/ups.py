"""OMRON BA100R UPS on RS232C (Megatec Q1 protocol), ported from the Tk app's managers/ups_manager.py:
port search/select, connect, Q1 polling (1 s for the first minute of a session, then 60 s),
LOG/UPS/ups_<date>.csv, alerts, diagnosis and the S.5 shutdown, with the same timings and texts.

Ownership: the service holds the port only while the Tk app is closed (instance lock "daq").
When the Tk app starts, the port is closed within a second; when it exits, v2 takes over again
(history reload + auto-connect, as the Tk app does at start-up). V2_NO_HW=1 never opens the port.
Listeners get (event, payload) on worker threads; pages forward them through a Qt signal.
"""
import atexit
import collections
import csv
import os
import threading
import time
from datetime import datetime, timedelta

from core import env  # puts instance_lock on sys.path
import instance_lock  # noqa: E402
from core.access import ACCESS

BASE = env.DAQ_DIR
VID, PID = 0x067B, 0x23A3          # Prolific PL2303 bridge of the UPS cable
BAUD = 2400
Q1 = b"Q1\r"
SHUTDOWN_CMD = b"S.5\r"           # Megatec: output off in 5 x 6 s = 30 s
RATED_W = 800
BATT_EMPTY_V, BATT_FULL_V = 21, 27.5
FAST_S, SLOW_S, FAST_WINDOW_S, IDLE_S = 1.0, 60.0, 60.0, 2.0
STARTUP_DELAY_S = 0.5              # Tk app: after(500, auto_connect_ups)
RETAKE_DELAY_S = 3.0               # after the Tk app exits, so its port close has finished
SUPERVISE_S = 0.5
HISTORY_LEN = 90000
ADMIN_PASSWORD = "1234"            # the Tk app's UPS admin password (ups_manager.py)
EMERGENCY_INFO = "\n\n[Emergency Contacts]\nLab: 0578-86-9250\nJunkyo: +82-10-6503-2581"
CSV_HEADER = "timestamp,Watt,Temp,Vin,Vout\n"


# ---------------------------------------------------------------- files
def ups_dir():
    return os.path.join(BASE, "LOG", "UPS")


def csv_path(day):
    return os.path.join(ups_dir(), f"ups_{day:%Y%m%d}.csv")


def log(message):
    """Same line format and file as the Tk app's _log (logs/log_<date>.txt)."""
    try:
        log_dir = os.path.join(BASE, "logs")
        os.makedirs(log_dir, exist_ok=True)
        now = datetime.now()
        with open(os.path.join(log_dir, f"log_{now:%Y-%m-%d}.txt"), "a") as f:
            f.write(f"[{now:%Y-%m-%d %H:%M:%S}] {message}\n")
    except Exception as e:
        print(f"Error while logging: {e}")


def append_csv(watt, temp, vin, vout, now=None):
    """One row to LOG/UPS/ups_<date>.csv, skipped while the output is off. -> error text or None."""
    if vout <= 0.5:
        return None
    now = now or datetime.now()
    os.makedirs(ups_dir(), exist_ok=True)
    path = csv_path(now)
    exists = os.path.isfile(path)
    try:
        with open(path, "a") as f:
            if not exists:
                f.write(CSV_HEADER)
            f.write(f"{now.isoformat()},{watt:.1f},{temp:.1f},{vin:.1f},{vout:.1f}\n")
    except Exception as e:
        return str(e)
    return None


def _parse_ts(text, day):
    try:
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return datetime.combine(day.date(), datetime.strptime(text, "%H:%M:%S").time())


def read_csv(path, day):
    """[(ts, watt, temp, vin, vout)] of one log file; bad rows are skipped."""
    rows = []
    try:
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                try:
                    rows.append((_parse_ts(r["timestamp"], day), float(r["Watt"]), float(r["Temp"]),
                                 float(r["Vin"]), float(r["Vout"])))
                except (KeyError, TypeError, ValueError):
                    continue
    except OSError:
        pass
    return rows


def load_history(now=None):
    """The last 24 h from yesterday's and today's logs (Tk: preload_ups_history)."""
    now = now or datetime.now()
    out = []
    for day in (now - timedelta(days=1), now):
        path = csv_path(day)
        if os.path.exists(path):
            out += [r for r in read_csv(path, day) if now - r[0] <= timedelta(hours=24)]
    return out


# ---------------------------------------------------------------- protocol
def parse_q1(response):
    """'(MMM.M NNN.N PPP.P QQQ RR.R S.SS TT.T bbbbbbbb' -> dict, or None if not a Q1 record."""
    response = response.strip()
    if not response.startswith("("):
        return None
    data = response[1:].split()
    if len(data) < 7:
        return None
    vin, vout, load = float(data[0]), float(data[2]), float(data[3])
    freq, batt_v, temp = float(data[4]), float(data[5]), float(data[6])
    return {"vin": vin, "vout": vout, "load": load, "freq": freq, "batt_v": batt_v, "temp": temp,
            "watt": RATED_W * (load / 100.0),
            "batt_pct": min(100, max(0, int((batt_v - BATT_EMPTY_V) / (BATT_FULL_V - BATT_EMPTY_V) * 100)))}


def evaluate(watt, temp, batt, load, vin):
    """-> (status line, level 'normal'|'warn'|'bad', critical, alert text); thresholds of check_ups_alerts."""
    msg, level, critical = "Normal", "normal", False
    if vin < 10:
        msg, level, critical = "POWER FAILURE! BATTERY MODE", "bad", True
    elif batt < 30:
        msg, level, critical = f"LOW BATTERY ({batt}%)", "warn", batt < 15
    elif load > 85:
        msg, level, critical = f"UPS OVERLOAD! ({load}%)", "bad", True
    elif temp > 45:
        msg, level = f"UPS OVERHEAT ({temp}°C)", "warn"
    return f"{msg} ({watt:.1f} W) / Temp: {temp:.1f}°C", level, critical, msg


def outlet_states(vout):
    return [1, 1, 0, 0] if vout > 50 else [0, 0, 0, 0]


# ---------------------------------------------------------------- hardware access (patched in tests)
def list_port_infos():
    import serial.tools.list_ports
    return list(serial.tools.list_ports.comports())


def list_ports():
    return [p.device for p in list_port_infos()]


def find_ups_port():
    for p in list_port_infos():
        if (p.vid or 0) == VID and (p.pid or 0) == PID:
            return p.device
    return None


def open_serial(port, handshake):
    if env.NO_HW:
        raise RuntimeError("hardware access is off (V2_NO_HW=1)")
    import serial
    if handshake:
        return serial.Serial(port=port, baudrate=BAUD, parity=serial.PARITY_NONE,
                             stopbits=serial.STOPBITS_ONE, bytesize=serial.EIGHTBITS, timeout=1.0)
    return serial.Serial(port, BAUD, timeout=1)


def tk_running():
    return instance_lock.is_running("daq")


# ---------------------------------------------------------------- service
class UpsService:
    def __init__(self):
        self.listeners = []
        self.ser = None
        self.port = None
        self.owns = False
        self.conn_status = "Disconnected"
        self.reading = None          # last parse_q1 dict + status/level
        self.outlets = [0, 0, 0, 0]
        self.session_start = None
        self.history = collections.deque(maxlen=HISTORY_LEN)
        self._alert_active = False
        self._connecting = False
        self._io = threading.RLock()
        self._hist_lock = threading.Lock()
        self._wake = threading.Event()
        self._released = threading.Event()
        self._quit = threading.Event()
        self._started = False

    # ---- events
    def _emit(self, ev, payload=None):
        for fn in list(self.listeners):
            try:
                fn(ev, payload)
            except RuntimeError:     # its Qt bridge was deleted (page rebuilt)
                if fn in self.listeners:
                    self.listeners.remove(fn)

    def _set_status(self, text):
        self.conn_status = text
        self._emit("state")

    @property
    def connected(self):
        s = self.ser
        return bool(s is not None and s.is_open)

    def history_snapshot(self):
        with self._hist_lock:
            return list(self.history)

    # ---- lifecycle
    def start(self):
        if self._started or env.NO_HW:
            return
        self._started = True
        threading.Thread(target=self._supervise, name="ups-owner", daemon=True).start()
        threading.Thread(target=self._poll_loop, name="ups-poll", daemon=True).start()

    def stop(self):
        self._quit.set()
        self._wake.set()
        self._close_port()

    def _supervise(self):
        free_since, delay = time.monotonic(), STARTUP_DELAY_S
        while not self._quit.is_set():
            if tk_running():
                if self.owns:
                    self.release()
                free_since, delay = time.monotonic(), RETAKE_DELAY_S
            elif not self.owns and time.monotonic() - free_since >= delay:
                self.take()
            self._quit.wait(SUPERVISE_S)

    def take(self):
        """Become the UPS owner: reload the 24 h history, then auto-connect (Tk start-up order)."""
        self._released.clear()
        self.owns = True
        self.session_start = None
        rows = load_history()
        with self._hist_lock:
            self.history.clear()
            self.history.extend(rows)
        if rows:
            log(f"UPS 24h history recovered ({len(rows)} points).")
        self._emit("owner", True)
        self.auto_connect()

    def release(self):
        """The Tk app is running: close the port and stop polling/logging."""
        self.owns = False
        self._released.set()
        had = self._close_port()
        self.reading = None
        self._alert_active = False
        if had:
            log("UPS serial port released to the Tk app.")
        self._set_status("Disconnected")
        self._emit("owner", False)

    def _close_port(self):
        got = self._io.acquire(timeout=0.6)
        try:
            s, self.ser = self.ser, None
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
            return s is not None
        finally:
            if got:
                self._io.release()

    def _pause(self, seconds, ser):
        """Sleep that aborts (and closes ser) if ownership is lost meanwhile."""
        if self._released.wait(seconds) or self._quit.is_set():
            try:
                ser.close()
            except Exception:
                pass
            return False
        return True

    def _adopt(self, ser, port):
        with self._io:
            if not self.owns or self._released.is_set():
                ser.close()
                return False
            old, self.ser, self.port = self.ser, ser, port
        if old is not None and old is not ser:
            try:
                old.close()
            except Exception:
                pass
        return True

    def _connected(self, port):
        self._set_status(f"Connected: {port}")
        self._emit("connected", port)
        self._wake.set()          # Tk: after(0, update_ups_status_loop)

    # ---- connect / disconnect
    def auto_connect(self):
        if not self.owns or env.NO_HW:
            return
        port = find_ups_port()
        if port:
            self._set_status(f"Connecting to {port}...")
            self._spawn(self._handshake, port)
        else:
            self._set_status("UPS H/W Not Found")

    def _spawn(self, fn, *args):
        if self._connecting:
            return
        self._connecting = True

        def run():
            try:
                fn(*args)
            finally:
                self._connecting = False
        threading.Thread(target=run, daemon=True).start()

    def _handshake(self, port):
        try:
            ser = open_serial(port, handshake=True)
            ser.dtr = True
            ser.rts = False
            if not self._pause(2.0, ser):
                return
            ser.reset_input_buffer()
            ser.write(Q1)
            if not self._pause(1.0, ser):
                return
            response = ser.read(ser.in_waiting or 1).decode("ascii", errors="ignore").strip()
            if response.startswith("(") or ser.is_open:   # Tk app accepts any port that opened
                if not self._adopt(ser, port):
                    return
                log(f"UPS Handshake Success with Q1: {response!r}")
                self._connected(port)
            else:
                ser.close()
                self._set_status("No Response")
        except Exception as e:
            log(f"UPS Handshake Error: {e}")

    def connect_manual(self, port):
        """Connect UPS button. -> error text for a warning dialog, or None (result comes as events)."""
        if not port:
            return "Please select a port first."
        if not self.owns:
            return "The Tk app owns the UPS port."
        self._set_status(f"Connecting to {port}...")
        self._spawn(self._manual_task, port)
        return None

    def _manual_task(self, port):
        try:
            ser = open_serial(port, handshake=False)
            ser.dtr = True
            ser.rts = False
            if not self._pause(1.0, ser):
                return
            ser.reset_input_buffer()
            ser.write(Q1)
            if not self._pause(0.5, ser):
                return
            if not self._adopt(ser, port):
                return
            log(f"UPS Manual Connect Success: {port}")
            self._connected(port)
        except Exception as e:
            self._emit("dialog", ("critical", "UPS Connection Error", f"Failed to connect: {e}"))

    def disconnect(self):
        self._close_port()
        self._set_status("Disconnected")
        self._emit("disconnected")
        log("UPS Serial Disconnected.")

    def unlock_port(self):
        """Change... after the admin password: disconnect and allow a port change."""
        self._close_port()
        self._set_status("Disconnected")
        self._emit("disconnected")
        log("[INFO] UPS port unlocked by admin.")

    def search_ports(self):
        ports = list_ports()
        if ports:
            log(f"Found {len(ports)} serial ports.")
        log("UPS Control buttons enabled.")
        log("No serial ports found. You can still type the port manually." if not ports else "UPS ports updated.")
        return ports

    def manual_refresh(self):
        if self.connected:
            self.session_start = time.time()
            log("UPS manual refresh triggered (1s mode for 60s)")
            self._wake.set()

    # ---- polling
    def _poll_loop(self):
        while not self._quit.is_set():
            wait = IDLE_S
            if self.owns and self.connected:
                if self.session_start is None:
                    self.session_start = time.time()
                interval = FAST_S if time.time() - self.session_start < FAST_WINDOW_S else SLOW_S
                t0 = time.monotonic()
                self.poll_once()
                wait = max(0.0, interval - (time.monotonic() - t0))
            self._wake.wait(wait)
            self._wake.clear()

    def poll_once(self):
        try:
            with self._io:
                ser = self.ser
                if ser is None or not ser.is_open or not self.owns:
                    return
                ser.write(Q1)
                time.sleep(0.3)
                response = ""
                if ser.in_waiting > 0:
                    response = ser.read(ser.in_waiting).decode("ascii", errors="ignore").strip()
            if response.startswith("("):
                r = parse_q1(response)
                if r is not None:
                    self._on_reading(r)
        except Exception as e:
            if not self._quit.is_set():
                log(f"[ERROR] UPS Loop Error: {e}")

    def _on_reading(self, r, now=None):
        self.outlets = outlet_states(r["vout"])
        err = append_csv(r["watt"], r["temp"], r["vin"], r["vout"], now=now)
        if err:
            log(f"Failed to save UPS log: {err}")
        status, level, critical, short = evaluate(r["watt"], r["temp"], r["batt_pct"], r["load"], r["vin"])
        r = dict(r, status=status, level=level)
        self.reading = r
        with self._hist_lock:
            self.history.append((now or datetime.now(), r["watt"], r["temp"], r["vin"], r["vout"]))
        self._emit("reading", r)
        if critical and not self._alert_active:
            self._alert_active = True
            self._emit("critical", f"Critical Condition Detected:\n{short}{EMERGENCY_INFO}")
        elif not critical:
            self._alert_active = False

    # ---- diagnosis
    def diagnose(self):
        """Diagnosis button; blocks up to 0.5 s, call off the GUI thread. Results come as events."""
        log("Starting UPS Connection Diagnosis...")
        target = find_ups_port()
        if not target:
            log("Step 1 Fail: Prolific USB-Serial Hardware not found.")
            self._emit("dialog", ("critical", "Diagnosis", "UPS USB device not found (check lsusb)."))
            return
        if self.connected:
            try:
                with self._io:
                    self.ser.write(Q1)
                    time.sleep(0.5)
                    n = self.ser.in_waiting
                    if n > 0:
                        self.ser.read(n)     # keep the next poll's reading current
                if n > 0:
                    log(f"Step 2 Pass: UPS Response received on {target}")
                    self._emit("dialog", ("info", "Diagnosis", f"Connection is normal.\nPort: {target}"))
                else:
                    log("Step 2 Fail: No response from UPS (Power?)")
                    self._emit("dialog", ("warning", "Diagnosis", "The port is detected but the UPS does not respond."))
            except Exception as e:
                log(f"Error: {e}")
        else:
            log("Attempting Auto-connect...")
            self.auto_connect()

    # ---- shutdown (S.5)
    @staticmethod
    def check_password(pwd):
        return pwd == ADMIN_PASSWORD

    def cancel_shutdown(self):
        log("[INFO] UPS shutdown cancelled by user.")

    def send_shutdown(self):
        """Cuts UPS output in ~30 s. Only after the full confirmation flow in the page. -> (ok, error)."""
        try:
            if not ACCESS.unlocked:
                raise RuntimeError("controls are locked")
            if not self.owns:
                raise RuntimeError("the Tk app owns the UPS port")
            with self._io:
                self.ser.write(SHUTDOWN_CMD)
                time.sleep(0.2)
            log("!!! UPS SHUTDOWN COMMAND SENT (S.5) — POWER CUTS IN ~30 SECONDS !!!")
            self.outlets = [2, 2, 2, 2]
            self._emit("outlets", self.outlets)
            return True, ""
        except Exception as e:
            log(f"[ERROR] UPS SHUTDOWN FAILED: {e}")
            return False, str(e)


_service = None


def service():
    """The one UPS service of this process (started on first use)."""
    global _service
    if _service is None:
        _service = UpsService()
        _service.start()
        atexit.register(_service.stop)
    return _service

# managers/patlite_lamp.py
"""
Driver for the PATLITE NE-USB multicolor signal beacon (VID 0x191A / PID
0x6001), USB HID class, controlled via `hid` (hidapi) -- same transport
LaserManager already uses for the Tamadenshi laser controllers, and the one
confirmed working end-to-end (macOS test rig + this Linux box) after pyusb
hit a permission wall on macOS. See PATLITE's "NE-USB USB communication
protocol" spec (command IDs below) for the wire format.

Two roles:
  1. Safety alarm -- LaserManager's interlock watchdog calls alarm_interlock()
     / clear() to flip the lamp red/off when the laser safety interlock trips
     (see managers/laser_manager.py's _interlock_watchdog_loop).
  2. Manual control -- the "Signal Lamp" UI tab exposes the full protocol
     (7 colors, 6 flash patterns, 7 buzzer patterns, volume, touch sensor
     readback) for ad-hoc use independent of the interlock.

Safe to import/instantiate even when the lamp is not physically connected:
every call degrades to a logged no-op instead of raising, and reconnects
automatically the next time a command is sent after the lamp is plugged in.
"""
import struct
import threading

try:
    import hid
    _HID_AVAILABLE = True
except Exception:
    _HID_AVAILABLE = False

VENDOR_ID = 0x191A
DEVICE_ID = 0x6001
COMMAND_VERSION = 0x0

# Command identifiers (byte 1 of the 8-byte payload)
CMD_CONTROL = 0x0          # LED + buzzer pattern/count in one shot
CMD_SETTING = 0x1          # connection-display LED on/off
CMD_ALARM_PATTERN = 0x2    # buzzer pattern (+ continuous/count) alone
CMD_ALARM_VOLUME = 0x3     # buzzer volume alone
CMD_ALARM_EX = 0x4         # buzzer pattern + count + volume together
CMD_CONN_DISPLAY = 0x5     # same as CMD_SETTING (spec lists it twice)
CMD_GET_TOUCH = 0x6        # NE-ST/NE-WT only; ignored on plain NE-*-USB
CMD_RESET = 0x7            # LED off, buzzer off

# LED color (spec section 3.1.2)
LED_COLOR_OFF = 0
LED_COLOR_RED = 1
LED_COLOR_GREEN = 2
LED_COLOR_AMBER = 3
LED_COLOR_BLUE = 4
LED_COLOR_PURPLE = 5
LED_COLOR_SKYBLUE = 6
LED_COLOR_WHITE = 7
LED_COLOR_KEEP = 0xF

LED_COLORS = {
    "Off": LED_COLOR_OFF, "Red": LED_COLOR_RED, "Green": LED_COLOR_GREEN,
    "Amber": LED_COLOR_AMBER, "Blue": LED_COLOR_BLUE, "Purple": LED_COLOR_PURPLE,
    "Sky Blue": LED_COLOR_SKYBLUE, "White": LED_COLOR_WHITE,
}

# LED pattern
LED_OFF = 0x0
LED_ON = 0x1        # steady
LED_PATTERN1 = 0x2
LED_PATTERN2 = 0x3
LED_PATTERN3 = 0x4
LED_PATTERN4 = 0x5
LED_PATTERN5 = 0x6
LED_PATTERN6 = 0x7
LED_PATTERN_KEEP = 0xF

LED_PATTERNS = {
    "Off": LED_OFF, "Steady": LED_ON, "Flash 1": LED_PATTERN1, "Flash 2": LED_PATTERN2,
    "Flash 3": LED_PATTERN3, "Flash 4": LED_PATTERN4, "Flash 5": LED_PATTERN5, "Flash 6": LED_PATTERN6,
}

# Buzzer pattern
BUZZER_OFF = 0x0
BUZZER_CONTINUOUS = 0x1
BUZZER_SWEEP = 0x2
BUZZER_INTERMITTENT = 0x3
BUZZER_WEAK_ATTENTION = 0x4
BUZZER_STRONG_ATTENTION = 0x5
BUZZER_TWINKLE_STAR = 0x6
BUZZER_LONDON_BRIDGE = 0x7
BUZZER_KEEP = 0xF

BUZZER_PATTERNS = {
    "Off": BUZZER_OFF, "Continuous": BUZZER_CONTINUOUS, "Sweep": BUZZER_SWEEP,
    "Intermittent": BUZZER_INTERMITTENT, "Weak Attention": BUZZER_WEAK_ATTENTION,
    "Strong Attention": BUZZER_STRONG_ATTENTION, "Twinkle Twinkle Little Star": BUZZER_TWINKLE_STAR,
    "London Bridge": BUZZER_LONDON_BRIDGE,
}

BUZZER_COUNT_CONTINUOUS = 0x0   # loop forever until stopped
BUZZER_COUNT_KEEP = 0xF

BUZZER_VOLUME_MUTE = 0x0
BUZZER_VOLUME_MAX = 0xA
BUZZER_VOLUME_KEEP = 0xF

SETTING_OFF = 0x0
SETTING_ON = 0x1


class PatliteLamp:
    """Persistent handle to one NE-USB beacon. All methods are best-effort:
    on any failure (not plugged in, permission denied, unplugged mid-run)
    they log once via `log_fn` and return False, never raise."""

    def __init__(self, log_fn=print):
        self._log = log_fn
        self._lock = threading.Lock()
        self._dev = None
        self._warned_missing = False

    def is_connected(self) -> bool:
        return self._dev is not None

    def probe(self) -> bool:
        """Attempt a (re)connect without sending any state-changing command --
        opening the HID handle doesn't touch the lamp's current light/buzzer
        state, unlike reset()/set_light(). Used by UI "check connection"
        actions so they don't clobber whatever was just set."""
        with self._lock:
            return self._ensure_dev() is not None

    def _open(self):
        if not _HID_AVAILABLE:
            if not self._warned_missing:
                self._log("[PATLITE] hidapi ('hid' module) not installed — lamp control disabled.")
                self._warned_missing = True
            return None

        try:
            dev = hid.device()
            dev.open(VENDOR_ID, DEVICE_ID)
            dev.set_nonblocking(0)
        except Exception:
            if not self._warned_missing:
                self._log("[PATLITE] Lamp not found on USB (VID 0x191A/PID 0x6001). "
                           "Connect it and it will be picked up on the next command.")
                self._warned_missing = True
            return None

        self._warned_missing = False
        self._log("[PATLITE] Lamp connected.")
        return dev

    def _ensure_dev(self):
        if self._dev is None:
            self._dev = self._open()
        return self._dev

    def _send(self, payload8: bytes) -> bool:
        """hidapi requires a leading Report ID byte even for devices (like this
        one) that don't use numbered reports -- prepend 0x00, matching the
        raw 8-byte USB packet the device actually expects on the wire."""
        assert len(payload8) == 8
        with self._lock:
            dev = self._ensure_dev()
            if dev is None:
                return False
            try:
                buf = bytes([0x00]) + payload8
                n = dev.write(buf)
                if n != len(buf):
                    self._log(f"[PATLITE] Short write to lamp ({n}/{len(buf)} bytes).")
                    return False
                return True
            except Exception as e:
                self._log(f"[PATLITE] Write failed ({e}) — will retry open on next call.")
                try:
                    dev.close()
                except Exception:
                    pass
                self._dev = None  # force re-open next time (handles unplug/replug)
                return False

    # ------------------------------------------------------------------
    # Core protocol commands
    # ------------------------------------------------------------------

    def set_light(self, color: int, pattern: int = LED_ON,
                   buzzer: int = BUZZER_KEEP, buzzer_count: int = BUZZER_COUNT_KEEP,
                   volume: int = BUZZER_VOLUME_KEEP) -> bool:
        """One-shot: set LED color+pattern and (optionally) buzzer pattern+volume together."""
        buzzer_control = (buzzer_count << 4) | buzzer
        led = (color << 4) | pattern
        data = struct.pack('BBBBBxxx', COMMAND_VERSION, CMD_CONTROL, buzzer_control, volume, led)
        return self._send(data)

    def set_buzzer(self, pattern: int, count: int = BUZZER_COUNT_CONTINUOUS,
                    volume: int = BUZZER_VOLUME_KEEP) -> bool:
        """Buzzer only (LED untouched) -- CMD_ALARM_EX (0x4): pattern+count+volume together."""
        data = struct.pack('BBBBxxxx', COMMAND_VERSION, CMD_ALARM_EX,
                            (count << 4) | pattern, volume)
        return self._send(data)

    def get_touch_state(self):
        """NE-ST-USB/NE-WT-USB only. Returns 1 (on) / 0 (off) / -1 (read failed
        or this unit has no touch sensor)."""
        data = struct.pack('BBxxxxxx', COMMAND_VERSION, CMD_GET_TOUCH)
        if not self._send(data):
            return -1
        with self._lock:
            dev = self._dev
            if dev is None:
                return -1
            try:
                resp = dev.read(2, timeout_ms=1000)
            except Exception:
                return -1
        if not resp or len(resp) < 2:
            return -1
        return 1 if (resp[1] & 1) == 1 else 0

    def reset(self) -> bool:
        """LED off, buzzer off (CMD_RESET)."""
        data = struct.pack('BBBBBxxx', COMMAND_VERSION, CMD_CONTROL,
                            BUZZER_OFF, BUZZER_VOLUME_KEEP, (LED_COLOR_OFF << 4) | LED_OFF)
        return self._send(data)

    # ------------------------------------------------------------------
    # Convenience wrappers used by the interlock safety path
    # ------------------------------------------------------------------

    def alarm_interlock(self) -> bool:
        """Called when the laser safety interlock trips: steady red light, no buzzer."""
        return self.set_light(LED_COLOR_RED, LED_ON, buzzer=BUZZER_OFF)

    def clear(self) -> bool:
        """Called when the interlock is released/cleared: lamp off, buzzer off."""
        return self.reset()

    def ok_green(self) -> bool:
        """Optional: steady green while the laser is connected and safe."""
        return self.set_light(LED_COLOR_GREEN, LED_ON, buzzer=BUZZER_OFF)

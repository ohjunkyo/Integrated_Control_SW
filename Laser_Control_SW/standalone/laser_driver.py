#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Tamadenshi (Tama Electric) Pico-second LD Board Control Library (Python 3)

Standalone edition. This is the same driver used by Integrated_Control_SW, with
two differences that make it portable to a fresh Linux PC:

  * the CSV data-log directory is no longer hardcoded to ~/ADC/ADC_test/LOG/LASER.
    It comes from the LASER_LOG_DIR environment variable, falling back to a
    "log" directory next to this file.
  * nothing touches the filesystem at import time. The log file and its handler
    are created on the first line actually written, so importing this module on
    a machine with no write permission (or no interest in logging) is safe.

Requirements:
- Python 3.7+
- hidapi library (pip3 install hidapi)
"""

import hid
import os
import logging
import threading
from datetime import datetime
from typing import Optional, List, Union, Tuple

# --- Data Logger Setup ---------------------------------------------------
# Where the CSV drift logs go. Override per-machine with:
#     export LASER_LOG_DIR=/data/laser_logs
# Default is ./log next to this file, so an unpacked copy of this directory is
# immediately runnable without creating anything outside itself.
DATA_LOG_DIR = os.environ.get(
    "LASER_LOG_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "log"),
)

CSV_HEADER = ("timestamp,ld_on,tec_on,temp_c,bias_ma,pulse_ma,"
              "pulse_width_ps,pd_raw,pd_current")

data_logger = logging.getLogger("LaserDataLogger")
data_logger.setLevel(logging.INFO)
data_logger.propagate = False

# Which date the current handler is writing to; None means "no handler yet".
# Deliberately module-level so several TamadenshiLaser instances (one per
# wavelength) share one handler instead of each opening its own.
_active_log_date: Optional[str] = None


def _ensure_daily_logger(day_str: str) -> bool:
    """Point data_logger at log/laser_data_<day_str>.csv, creating the
    directory and CSV header if needed. Called lazily on the first write of
    each day, never at import. Returns False if logging is unavailable (e.g.
    read-only filesystem) so callers can skip logging instead of crashing."""
    global _active_log_date
    if _active_log_date == day_str and data_logger.handlers:
        return True
    try:
        os.makedirs(DATA_LOG_DIR, exist_ok=True)
        path = os.path.join(DATA_LOG_DIR, f"laser_data_{day_str}.csv")
        need_header = (not os.path.exists(path)) or os.path.getsize(path) == 0

        for h in data_logger.handlers[:]:
            data_logger.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass

        handler = logging.FileHandler(path)
        handler.setFormatter(logging.Formatter("%(message)s"))
        data_logger.addHandler(handler)
        if need_header:
            data_logger.info(CSV_HEADER)
        _active_log_date = day_str
        return True
    except Exception as e:
        print(f"⚠️  CSV logging disabled ({DATA_LOG_DIR}): {e}")
        return False


def list_devices() -> List[dict]:
    """Enumerate every Tamadenshi LD board currently plugged in.

    Returns a list of dicts with at least 'path' (bytes, pass to connect()),
    'serial_number' and 'product_string'. Several boards share one VID/PID, so
    the physical path is the only reliable way to address a specific one --
    this is what a multi-wavelength setup uses to keep 375/405/450/473 apart."""
    return hid.enumerate(TamadenshiLaser.VENDOR_ID, TamadenshiLaser.PRODUCT_ID)


class TamadenshiLaser:
    """
    This class handles all low-level communication with the Tamadenshi
    laser driver board via the hidapi library.
    """

    # 1. Hardware Information (from tmHIDLD.dll decompile)
    # These IDs are used to find the specific USB device.
    VENDOR_ID = 0x04D8
    PRODUCT_ID = 0xFA73

    # Define the expected HID report packet structure
    PACKET_LENGTH = 65  # 64 bytes + 1 report ID byte
    REPORT_ID = 0x00    # The report ID is always 0x00

    # Bias and pulse current share ONE physical drive path on this board, so
    # the 200 mA ceiling in LD_board_library_manual.pdf applies to their SUM,
    # not to each field separately. Checking only one at a time (as the older
    # GUI did) lets an operator write 150+150 mA and overdrive the diode.
    LD_TOTAL_CURRENT_LIMIT_MA = 200.0

    # 'SET' Command Codes (Byte [1] of the packet)
    CMD_SET_LD_ON_OFF = 0x06     # (LDOnOff)
    CMD_SET_TEC_ON_OFF = 0x07    # (TECOnOff)
    CMD_SET_TEMP = 0x0A          # (SetTemp)
    CMD_SET_TRIGGER = 0x0E       # (SetPGOnOff)
    CMD_SET_BIAS = 0x13          # (SetBias)
    CMD_SET_PULSE = 0x14         # (SetLDCurrent)
    CMD_SET_PG1_FREQ = 0x0F      # (SetPG1Repetition)
    CMD_SET_PG2_FREQ = 0x10      # (SetPG2Repetition)

    # 'GET' Command Codes (Byte [1] of the packet)
    CMD_GET_ALL_STATUS = 0x09    # (GetPD) - Reads all status at once

    # (PulseWidth) -- found via decompiling tmHIDLD.dll; not documented in any
    # vendor manual we have. add+128, dat=0 reads EEPROM slot `add` WITHOUT
    # writing/touching the live hardware output (see PulseWidth() in
    # LD_board_library_manual.pdf p.19). The response byte layout was confirmed
    # empirically via a round-trip write/read on a scratch slot (address 5,
    # never used live): the response's first two bytes (after this driver's
    # usual report-ID strip) are [hi, lo] directly -- no separate leading
    # address byte, unlike a literal reading of the manual's byte table.
    CMD_PULSE_WIDTH = 0x08

    PULSE_WIDTH_MIN_PS = 100
    PULSE_WIDTH_MAX_PS = 10230

    def __init__(self, name: str = "LD"):
        """Initializes the laser controller class. `name` is only used to tag
        log/console output when several boards are driven from one process."""
        self.name = name
        self.device: Optional[hid.device] = None
        self.status = {}  # Dictionary to store the status read from the device
        # Serializes every HID write/read transaction. A watchdog thread and a
        # polling loop both call update_status() on the same device; without
        # this lock their write/read pairs interleave and each thread reads the
        # other's response, causing "Status parse error" and bogus values.
        self._io_lock = threading.RLock()
        self._last_csv_log_time = 0.0
        # Filled on first successful get_pulse_width_ps(); the width only
        # changes when someone writes it, so the CSV logger reuses this cache
        # instead of adding an HID round trip to every log line.
        self._cached_pulse_width_ps: Optional[int] = None

    # --- Connection management -------------------------------------------

    def connect(self, dev_path: bytes = None) -> Tuple[bool, str]:
        """
        Attempts to connect to the hardware.
        If dev_path is provided (from list_devices()), connects to that specific
        physical USB port; otherwise opens the first board matching VID/PID.
        Returns a (success_boolean, message_string) tuple.
        """
        if self.device:
            self.disconnect()

        try:
            self.device = hid.device()
            if dev_path:
                # Boards share a VID/PID, so address a specific one by path.
                self.device.open_path(dev_path)
            else:
                self.device.open(self.VENDOR_ID, self.PRODUCT_ID)

            prod_str = self.device.get_product_string()
            msg = f"Device connected successfully: {prod_str}"
            print(f"✅ [{self.name}] {msg}")
            return True, msg
        except IOError as e:
            msg = (f"Device connection failed: {e}\n"
                   "  1. Is the device connected via USB?\n"
                   "  2. (Linux) Are the udev rules installed? "
                   "See 99-tamadenshi.rules / install.sh.\n"
                   "  3. Check the interlock -- every interlock must be "
                   "attached to the magnet.")
            print(f"❌ [{self.name}] {msg}")
            self.device = None
            return False, msg
        except Exception as e:
            msg = f"An unknown error occurred: {e}"
            print(f"❌ [{self.name}] {msg}")
            self.device = None
            return False, msg

    def disconnect(self):
        """Explicitly disconnects from the device.

        Always call this before exiting. hidapi's libusb backend detaches the
        kernel usbhid driver on open(); a process killed without a clean close()
        leaves the device unbound and the next run cannot see it until the port
        is re-bound (or the cable is replugged)."""
        if self.device:
            try:
                self.device.close()
            except Exception:
                pass
            self.device = None
            print(f"🔌 [{self.name}] Device disconnected.")

    def is_connected(self) -> bool:
        """Checks if the device is currently known to be connected."""
        return self.device is not None

    def _handle_disconnection(self):
        """(Internal function) Called on IO error to reset the connection state."""
        print(f"🔌 [{self.name}] [Error] Device connection lost. (Check USB cable)")
        self.disconnect()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.disconnect()
        return False

    # --- Value Conversion Helper Functions --------------------------------

    def _val_to_dac(self, val_ma, max_ma=200.0) -> Tuple[int, int]:
        """
        (Internal helper) Converts a milliamp (mA) value to a 2-byte DAC value
        (High, Low). The hardware uses a 12-bit DAC (0-4095).
        """
        raw_val = int(round(val_ma * 4095.0 / max_ma))
        if raw_val < 0 or raw_val > 4095:
            raw_val = max(0, min(4095, raw_val))
        high_byte = (raw_val >> 8) & 0xFF
        low_byte = raw_val & 0xFF
        return high_byte, low_byte

    def _dac_to_val(self, high_byte, low_byte, max_ma=200.0) -> float:
        """(Internal helper) Converts a 2-byte DAC value back to milliamps."""
        raw_val = (high_byte << 8) | low_byte
        return (raw_val * max_ma) / 4095.0

    # --- Low-Level HID Communication Functions ----------------------------

    def _send_command(self, cmd_code: int, payload_bytes: List[int] = []) -> bool:
        """(Internal low-level) Sends an HID report (Write Only)."""
        if not self.is_connected():
            print(f"❌ [{self.name}] Command send failed: Device not connected.")
            return False

        report = [0x00] * self.PACKET_LENGTH
        report[0] = self.REPORT_ID
        report[1] = cmd_code
        for i, byte_val in enumerate(payload_bytes):
            if (i + 2) < self.PACKET_LENGTH:
                report[i + 2] = byte_val

        try:
            with self._io_lock:
                self.device.write(report)
            return True
        except (IOError, ValueError, OSError) as e:
            print(f"👻 [{self.name}] Command send failed (IOError): {e}")
            self._handle_disconnection()
            return False

    def _read_command(self, cmd_code: int,
                      payload_bytes: List[int] = []) -> Union[bytearray, None]:
        """(Internal low-level) Sends a command (Write) and receives a response."""
        if not self.is_connected():
            print(f"❌ [{self.name}] Command read failed: Device not connected.")
            return None

        report = [0x00] * self.PACKET_LENGTH
        report[0] = self.REPORT_ID
        report[1] = cmd_code
        for i, byte_val in enumerate(payload_bytes):
            if (i + 2) < self.PACKET_LENGTH:
                report[i + 2] = byte_val

        try:
            # Hold the lock across BOTH write and read so a concurrent
            # transaction cannot slip a write in between and steal this response.
            with self._io_lock:
                self.device.write(report)
                data = self.device.read(self.PACKET_LENGTH, 1000)

            if data:
                # NOTE: this device uses UNNUMBERED HID reports, so on Linux
                # hidraw the read comes back as 64 bytes of pure payload with
                # NO leading report-ID byte (verified: len(read()) == 64). The
                # strip below therefore discards a REAL payload byte -- what
                # tmHIDLD.dll calls data[0]. Every field this driver parses is
                # indexed against the stripped array and reads correctly (e.g.
                # pulse -> 164.98 mA for a 165 mA setpoint), so the strip is
                # left in place rather than re-indexing everything; callers who
                # need byte 0 (only the photodiode's high byte, see
                # update_status) use _read_status_raw() instead.
                return data[1:]
            print(f"❌ [{self.name}] Command read failed: Empty response.")
            return None
        except (IOError, ValueError, OSError) as e:
            print(f"👻 [{self.name}] Command read failed (IOError): {e}")
            self._handle_disconnection()
            return None
        except Exception as e:
            print(f"👻 [{self.name}] HID read failed (Timeout?): {e}")
            return None

    def _read_status_raw(self) -> Union[list, None]:
        """Status report WITHOUT _read_command's report-ID strip -- byte 0 is
        real payload on this device (unnumbered HID reports; see the note in
        _read_command). Only the photodiode needs it, because tmHIDLD.dll's
        GetPD() reads the PD as data[0]*256 + data[1] and the stripped array
        loses that high byte."""
        if not self.is_connected():
            return None
        report = [0x00] * self.PACKET_LENGTH
        report[0] = self.REPORT_ID
        report[1] = self.CMD_GET_ALL_STATUS
        try:
            with self._io_lock:
                self.device.write(report)
                data = self.device.read(self.PACKET_LENGTH, 1000)
            return list(data) if data else None
        except Exception as e:
            print(f"👻 [{self.name}] Raw status read failed: {e}")
            return None

    # ===================================================
    # 3. Public Functions (SET) - Device Configuration
    # ===================================================

    def set_ld_on(self, state: bool) -> bool:
        """Turns the laser (LD) ON or OFF."""
        return self._send_command(self.CMD_SET_LD_ON_OFF, [0x01 if state else 0x00])

    def set_tec_on(self, state: bool) -> bool:
        """Turns the Temperature Controller (TEC) ON or OFF."""
        return self._send_command(self.CMD_SET_TEC_ON_OFF, [0x01 if state else 0x00])

    def set_trigger_mode(self, pg1: bool, pg2: bool, ext: bool) -> bool:
        """Sets the trigger source. (PG1, PG2, External)"""
        data_byte = 0x00
        if pg1:
            data_byte |= 0x01  # Bit 0 for PG1
        if pg2:
            data_byte |= 0x02  # Bit 1 for PG2
        if ext:
            data_byte |= 0x04  # Bit 2 for External
        return self._send_command(self.CMD_SET_TRIGGER, [data_byte])

    def set_bias_current(self, current_ma: float) -> bool:
        """Sets the Bias Current. (Unit: mA, max 200mA)"""
        hb, lb = self._val_to_dac(current_ma, max_ma=200.0)
        return self._send_command(self.CMD_SET_BIAS, [hb, lb])

    def set_pulse_current(self, current_ma: float) -> bool:
        """Sets the Pulse Current (LD Current). (Unit: mA, max 200mA)"""
        hb, lb = self._val_to_dac(current_ma, max_ma=200.0)
        return self._send_command(self.CMD_SET_PULSE, [hb, lb])

    def set_currents(self, bias_ma: float, pulse_ma: float) -> Tuple[bool, str]:
        """Sets bias and pulse together, enforcing the board's COMBINED current
        ceiling (they share one drive path -- see LD_TOTAL_CURRENT_LIMIT_MA).
        Prefer this over calling the two setters separately."""
        if bias_ma < 0 or pulse_ma < 0:
            return False, "Currents must be non-negative."
        total = bias_ma + pulse_ma
        if total > self.LD_TOTAL_CURRENT_LIMIT_MA:
            return False, (f"Bias {bias_ma:.1f} + Pulse {pulse_ma:.1f} = "
                           f"{total:.1f} mA exceeds the "
                           f"{self.LD_TOTAL_CURRENT_LIMIT_MA:.0f} mA combined "
                           "LD limit. Not applied.")
        if not self.set_bias_current(bias_ma):
            return False, "Failed to write bias current."
        if not self.set_pulse_current(pulse_ma):
            return False, "Failed to write pulse current."
        return True, f"Applied bias {bias_ma:.1f} mA, pulse {pulse_ma:.1f} mA."

    def set_temp(self, temp_c: float) -> bool:
        """Sets the target TEC temperature. (Unit: °C, max 40°C)"""
        hb, lb = self._val_to_dac(temp_c, max_ma=40.0)
        return self._send_command(self.CMD_SET_TEMP, [hb, lb])

    def get_pulse_width_ps(self, address: int = 0) -> Optional[int]:
        """Reads the pulse width (picoseconds) currently stored at EEPROM
        slot `address` (0-9; 0 is the live/active one) WITHOUT writing
        anything -- safe to call anytime, including while the laser is
        firing. Returns None on comm failure."""
        resp = self._read_command(self.CMD_PULSE_WIDTH, [address + 128, 0, 0])
        if resp is None or len(resp) < 2:
            return None
        dat = resp[0] * 256 + resp[1]
        # dat is documented as 10-1023 (LD_board_library_manual.pdf p.19).
        # Anything outside that -- 0, or a garbage value like 22016 -- means
        # we caught a stale/leftover response (e.g. read right after another
        # command with no settling time) rather than this command's real
        # reply. Treat it as a failed read instead of caching a fake value;
        # the caller simply retries on the next poll.
        if not (10 <= dat <= 1023):
            return None
        width_ps = dat * 10
        if address == 0:            # the live slot -- cache for the CSV logger
            self._cached_pulse_width_ps = width_ps
        return width_ps

    def set_pulse_width_ps(self, width_ps: int, address: int = 0) -> bool:
        """Writes the pulse width (picoseconds, rounded to the nearest 10)
        to EEPROM slot `address`. Per LD_board_library_manual.pdf p.19,
        writing address 0-9 (no +128) BOTH updates the live hardware output
        immediately AND persists to EEPROM -- unlike get_pulse_width_ps's
        read (address+128), this one actually changes what the laser fires.
        Valid dat range is 10-1023 (100ps-10230ps); out-of-range raises."""
        dat = round(width_ps / 10)
        if not (10 <= dat <= 1023):
            raise ValueError(
                f"pulse width {width_ps}ps out of valid range "
                f"({self.PULSE_WIDTH_MIN_PS}-{self.PULSE_WIDTH_MAX_PS}ps)")
        hb, lb = (dat >> 8) & 0xFF, dat & 0xFF
        resp = self._read_command(self.CMD_PULSE_WIDTH, [address, hb, lb])
        ok = resp is not None
        if ok and address == 0:
            self._cached_pulse_width_ps = dat * 10  # keep the CSV logger in step
        return ok

    # --- Frequency Set Functions ---
    def _freq_to_4bytes(self, freq_hz: int) -> List[int]:
        """Converts a Hz integer into 4 bytes (Big Endian)."""
        freq_hz = int(freq_hz)
        return [(freq_hz >> 24) & 0xFF, (freq_hz >> 16) & 0xFF,
                (freq_hz >> 8) & 0xFF, freq_hz & 0xFF]

    def set_pg1_frequency(self, freq_hz: int) -> bool:
        """Sets Internal Oscillator 1 (High Speed) frequency."""
        if not 100_000 <= freq_hz <= 250_000_000:
            print(f"⚠️  PG1 Frequency {freq_hz}Hz is outside the recommended "
                  "range (100kHz-250MHz).")
        return self._send_command(self.CMD_SET_PG1_FREQ,
                                  self._freq_to_4bytes(freq_hz))

    def set_pg2_frequency(self, freq_hz: int) -> bool:
        """Sets Internal Oscillator 2 (Low Speed) frequency."""
        if not 3_000 <= freq_hz <= 200_000:
            print(f"⚠️  PG2 Frequency {freq_hz}Hz is outside the recommended "
                  "range (3kHz-200kHz).")
        return self._send_command(self.CMD_SET_PG2_FREQ,
                                  self._freq_to_4bytes(freq_hz))

    # ===================================================
    # 4. Public Functions (GET) - Device Status Reading
    # ===================================================

    def update_status(self, log_csv: bool = True) -> bool:
        """
        Reads all key device statuses at once and stores them in self.status.
        Set log_csv=False for one-off reads that should not append to the
        drift log (e.g. a CLI 'status' call).
        """
        raw = self._read_status_raw()
        if raw is None:
            return False
        # Everything below is indexed against the historically-stripped array.
        data = raw[1:]

        try:
            # Photodiode monitor -- the ONLY real optical-output measurement the
            # board makes (bias/pulse below are just the DAC setpoints we wrote,
            # echoed back; there is no current-sense ADC on the LD drive path).
            #
            # Was (data[1]<<8)|data[2], which read a constant 2 on every laser.
            # tmHIDLD.dll's GetPD() reads the PD as data[0]*256 + data[1] --
            # a 10-bit ADC split across the report's FIRST TWO bytes, which is
            # why this uses the unstripped `raw` (the stripped array starts at
            # raw[1] and would silently drop the high byte, capping PD at 255).
            self.status['pd_raw'] = raw[0] * 256 + raw[1]
            # Vendor's photodiode current, from GetPD(): the front end is a
            # logarithmic amplifier, hence 10**(...) rather than a linear scale.
            # The 2.5 constant is 3.0 on boards with HardInfoB's DA2_048V_DA3V
            # bit set; see the LD-temperature note below for why the revision
            # probe isn't reimplemented here. Result is in Amps.
            #
            # HARDWARE CAVEAT (measured 2026-08-09, all four boards at the main
            # site, LD ON with the trigger connected and firing): only the 450nm
            # board's monitor photodiode actually responds -- raw ~88 dark,
            # rising to ~106 while lasing. 375nm / 405nm / 473nm return EXACTLY
            # 0 even while firing, i.e. not even ADC noise, which means their
            # monitor-PD input is dead or unpopulated rather than merely reading
            # a small signal. pd_valid says whether this reading means anything,
            # so drift analysis can drop boards that cannot report light instead
            # of silently averaging in a hard zero. Re-check on new hardware:
            # this is a per-board property, not a driver limitation.
            self.status['pd_current'] = 10.0 ** (
                ((self.status['pd_raw'] * 2.5 / 1023.0) - 0.5) / 0.2)
            self.status['pd_valid'] = self.status['pd_raw'] > 0
            # Mirror the cached width into status so CSV loggers can record it
            # without paying for an extra HID round trip on every sample.
            self.status['pulse_width_ps'] = self._cached_pulse_width_ps

            # LD Temperature.
            #
            # Was (data[4]<<8)|data[5] * 40/1023, which read the WRONG bytes:
            # with the TEC off on three of four lasers (so all four should sit
            # at the same room temperature) that formula gave
            # 14.00/14.31/3.21/14.35 C -- mutually inconsistent, and the 450nm
            # "3.21 C" was pure artifact, not a cold laser. It also made the
            # live temperature plot step like a square wave, which no real
            # thermistor does.
            #
            # Decompiling tmHIDLD.dll's GetPD() shows the vendor reads the
            # temperature from ITS data[3],data[4] and scales by 0.0391 (note
            # 40/1023 = 0.03910068, i.e. the old scale factor was already the
            # vendor's -- only the byte offset was wrong). The vendor's buffer
            # keeps the leading byte that _read_command() strips off here, so
            # vendor data[3],data[4] == our data[2],data[3]. With those bytes
            # all four lasers read 22.4-23.0 C: tight, mutually consistent, and
            # exactly room temperature.
            #
            # NOTE: the vendor additionally scales by 0.04888 instead of 0.0391
            # on boards whose HardInfoB bit5 is set, and by x1.5 / x1.75 for
            # other revisions. All four of our boards are plainly on the plain
            # 0.0391 x1.0 branch (the alternatives would put a room-temperature
            # board at 28 C / 34 C / 39 C), so the revision probe is not
            # reimplemented here -- revisit if a board ever reads implausibly hot.
            raw_ld_temp = (data[2] << 8) | data[3]
            self.status['ld_temp'] = raw_ld_temp * 0.0391

            if len(data) > 17:
                self.status['tec_current_raw'] = (data[17] << 8) | data[8]
            else:
                self.status['tec_current_raw'] = 0

            self.status['pulse'] = self._dac_to_val(data[9], data[10], 200.0)
            self.status['bias'] = self._dac_to_val(data[7], data[8], 200.0)

            info_byte = data[14]
            self.status['ld_on'] = bool(info_byte & 0x04)
            self.status['tec_on'] = bool(info_byte & 0x08)
            self.status['info_byte'] = info_byte   # raw -- for diagnostics
            # Bits 0/1 are tentative interlock/alarm flags (verify with hardware):
            self.status['alarm'] = bool(info_byte & 0x01)
            self.status['interlock'] = bool(info_byte & 0x02)

            if log_csv:
                self._maybe_log_csv()
            return True
        except Exception as e:
            print(f"❌ [{self.name}] Status parse error: {e}")
            return False

    def _maybe_log_csv(self):
        """Append one CSV line at most every 10 s, and only while the LD is on
        (an off laser produces no drift information worth storing)."""
        now = datetime.now()
        now_t = now.timestamp()
        if not self.status.get('ld_on', False):
            return
        if now_t - self._last_csv_log_time < 10.0:
            return
        self._last_csv_log_time = now_t

        if not _ensure_daily_logger(now.strftime('%Y%m%d')):
            return
        try:
            # pd_raw/pd_current are the only genuinely MEASURED quantities here
            # -- bias/pulse are the DAC setpoints read back, so they cannot show
            # drift by construction (they read the commanded value even with the
            # LD off). Logging both lets a drift analysis divide the measured
            # optical output by the commanded drive current.
            # pd_current is left BLANK (not 0, not the 3.162 log-formula floor)
            # when the board's photodiode is dead, so a drift fit reading this
            # CSV skips those rows instead of fitting a flat fake line.
            pd_valid = self.status.get('pd_valid', False)
            pw = self._cached_pulse_width_ps
            data_logger.info(
                "{ts},{ld},{tec},{temp:.3f},{bias:.3f},{pulse:.3f},"
                "{pw},{pd_raw},{pd_cur}".format(
                    ts=now.isoformat(),
                    ld=self.status['ld_on'],
                    tec=self.status['tec_on'],
                    temp=self.status['ld_temp'],
                    bias=self.status['bias'],
                    pulse=self.status['pulse'],
                    pw=pw if pw is not None else "",
                    pd_raw=self.status.get('pd_raw', 0),
                    pd_cur=(f"{self.status.get('pd_current', 0.0):.6f}"
                            if pd_valid else ""),
                ))
        except Exception as e:
            print(f"Failed to write data log: {e}")

    def get_cached_status(self, key: str, default_val=0.0):
        return self.status.get(key, default_val)

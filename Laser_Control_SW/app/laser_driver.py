#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Tamadenshi (Tama Electric) Pico-second LD Board Control Library (Python 3)

This code is a re-implementation of the tmHIDLD.dll, reverse-engineered to
send HID commands directly in a Linux (hidapi) environment.

Requirements:
- Python 3.7+
- hidapi library (pip3 install hidapi)
"""

import hid
import os
import logging
import math
from datetime import datetime
from typing import Optional, List, Union

# --- Data Logger Setup ---
# CSV Data Log (for plotting)
DATA_LOG_DIR = os.path.expanduser("~/ADC/ADC_test/LOG/LASER")
os.makedirs(DATA_LOG_DIR, exist_ok=True)
DATA_LOG_FILE = os.path.join(DATA_LOG_DIR, f"laser_data_{datetime.now().strftime('%Y%m%d')}.csv")

# Data logger configuration
data_logger = logging.getLogger('LaserDataLogger')
data_logger.setLevel(logging.INFO)

# Check if a file handler has already been added (prevents duplicates on script reload)
if not data_logger.hasHandlers():
    data_handler = logging.FileHandler(DATA_LOG_FILE)
    
    # Add CSV header (only if the file is empty)
    try:
        if os.path.getsize(DATA_LOG_FILE) == 0:
            data_handler.setFormatter(logging.Formatter('%(message)s'))
            data_logger.addHandler(data_handler)
            data_logger.info("timestamp,ld_on,tec_on,temp_c,bias_ma,pulse_ma")
        else:
            data_handler.setFormatter(logging.Formatter('%(message)s'))
            data_logger.addHandler(data_handler)
    except FileNotFoundError:
        # Handle exception if the file was just created and os.path.getsize can't find it immediately
        data_handler.setFormatter(logging.Formatter('%(message)s'))
        data_logger.addHandler(data_handler)
        data_logger.info("timestamp,ld_on,tec_on,temp_c,bias_ma,pulse_ma")
        
    data_logger.propagate = False # Prevent propagation to the root logger
# -------------------------


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

    def __init__(self):
        """Initializes the laser controller class."""
        self.device: Optional[hid.device] = None
        self.status = {}  # Dictionary to store the status read from the device
        print(f"Controller initialized (VID: {self.VENDOR_ID:04x}, PID: {self.PRODUCT_ID:04x})")

    def connect(self, dev_path: bytes = None) -> (bool, str):
        """
        Attempts to connect to the hardware.
        If dev_path is provided, it connects to the specific physical USB port.
        Returns a (success_boolean, message_string) tuple.
        """
        if self.device:
            self.disconnect()
            
        try:
            # Create an hid.device object
            self.device = hid.device()
            
            if dev_path:
                # [수정] 동일한 VID/PID를 가진 장치들을 구분하기 위해 물리적 경로(Path)로 직접 연결합니다.
                self.device.open_path(dev_path)
            else:
                # 경로가 지정되지 않은 경우, 기존처럼 VID와 PID로 첫 번째 장치를 찾습니다.
                self.device.open(self.VENDOR_ID, self.PRODUCT_ID)

            # If successful, get the product string
            prod_str = self.device.get_product_string()
            msg = f"Device connected successfully: {prod_str}"
            print(f"✅ {msg}")
            return True, msg
        except IOError as e:
            # 이 에러는 주로 권한 문제나 케이블 연결 불량 시 발생합니다.
            msg = f"Device connection failed: {e}\n  1. Is the device connected via USB?\n  2. (Linux) Are you running with 'sudo' or are udev rules set up? \n 3. You should check interlock system. \n All interlock must attach to the magnet."
            print(f"❌ {msg}")
            self.device = None
            return False, msg
        except Exception as e:
            msg = f"An unknown error occurred: {e}"
            print(f"❌ {msg}")
            self.device = None
            return False, msg

    """# {{{
    def connect(self) -> (bool, str):
        if self.device:
            self.disconnect()
            
        try:
            # Create an hid.device object
            self.device = hid.device()
            # Try to open the device using its VID and PID
            self.device.open(self.VENDOR_ID, self.PRODUCT_ID)
            
            # If successful, get the product string
            prod_str = self.device.get_product_string()
            msg = f"Device connected successfully: {prod_str}"
            print(f"✅ {msg}")
            return True, msg
        except IOError as e:
            # This is the most common error, usually a permissions issue on Linux
            msg = f"Device connection failed: {e}\n  1. Is the device connected via USB?\n  2. (Linux) Are you running with 'sudo' or are udev rules set up?"
            print(f"❌ {msg}")
            self.device = None
            return False, msg
        except Exception as e:
            msg = f"An unknown error occurred: {e}"
            print(f"❌ {msg}")
            self.device = None
            return False, msg
    """# }}}
    def disconnect(self):
        """Explicitly disconnects from the device."""
        if self.device:
            try:
                self.device.close()
            except Exception:
                pass 
            self.device = None
            print("🔌 Device disconnected.")

    def is_connected(self) -> bool:
        """Checks if the device is currently known to be connected."""
        return self.device is not None

    def _handle_disconnection(self):
        """(Internal function) Called on IO error to reset the connection state."""
        print("🔌 [Error] Device connection lost. (Check USB cable)")
        self.disconnect()

    # --- Value Conversion Helper Functions ---

    def _val_to_dac(self, val_ma, max_ma=200.0) -> (int, int):
        """
        (Internal helper) Converts a milliamp (mA) value to a 2-byte DAC value (High, Low).
        The hardware uses a 12-bit DAC (0-4095).
        """
        # Convert mA to 12-bit DAC value (0-4095)
        raw_val = int(round(val_ma * 4095.0 / max_ma))
        
        if raw_val < 0 or raw_val > 4095:
             # print(f"⚠️ Warning: {val_ma}mA is outside the valid range (0-{max_ma}mA). Clamping value.")
             raw_val = max(0, min(4095, raw_val)) # Clamp value to 0-4095

        # Split 12-bit value into two 8-bit bytes
        high_byte = (raw_val >> 8) & 0xFF  # Most significant byte
        low_byte = raw_val & 0xFF         # Least significant byte
        return high_byte, low_byte
    
    def _dac_to_val(self, high_byte, low_byte, max_ma=200.0) -> float:
        """(Internal helper) Converts a 2-byte DAC value (High, Low) back to milliamps (mA)."""
        # Combine high and low bytes back into a 12-bit value
        raw_val = (high_byte << 8) | low_byte
        # Convert 12-bit DAC value back to mA
        return (raw_val * max_ma) / 4095.0

    # --- Low-Level HID Communication Functions ---

    def _send_command(self, cmd_code: int, payload_bytes: List[int] = []) -> bool:
        """(Internal low-level) Sends an HID report (Write Only)."""
        if not self.is_connected():
            print("❌ Command send failed: Device not connected.")
            return False

        # Create the 65-byte packet, initialized to zeros
        report = [0x00] * self.PACKET_LENGTH
        
        # Byte [0] is the Report ID
        report[0] = self.REPORT_ID
        # Byte [1] is the command code
        report[1] = cmd_code
        
        # Fill the rest of the packet with payload data
        for i, byte_val in enumerate(payload_bytes):
            if (i + 2) < self.PACKET_LENGTH:
                report[i + 2] = byte_val

        try:
            # Write the report to the HID device
            self.device.write(report)
            return True
        except (IOError, ValueError, OSError) as e:
            # This often happens if the device is unplugged
            print(f"👻 Command send failed (IOError): {e}")
            self._handle_disconnection()
            return False
            
    def _read_command(self, cmd_code: int, payload_bytes: List[int] = []) -> Union[bytearray, None]:
        """(Internal low-level) Sends a command (Write) and receives a response (Read)."""
        if not self.is_connected():
            print("❌ Command read failed: Device not connected.")
            return None
            
        # Create the 65-byte packet to send
        report = [0x00] * self.PACKET_LENGTH
        report[0] = self.REPORT_ID
        report[1] = cmd_code

        # Fill payload
        for i, byte_val in enumerate(payload_bytes):
            if (i + 2) < self.PACKET_LENGTH:
                report[i + 2] = byte_val
        
        try:
            # Step 1: Write the command to the device
            self.device.write(report)
            
            # Step 2: Read the 65-byte response with a 1000ms timeout
            # Use positional argument '1000' instead of 'timeout=1000'
            data = self.device.read(self.PACKET_LENGTH, 1000) 
            
            if data:
                # Return data, excluding the first byte (Report ID)
                return data[1:] 
            else:
                print("❌ Command read failed: Empty response from device.")
                return None
        except (IOError, ValueError, OSError) as e:
            # Handle disconnection
            print(f"👻 Command read failed (IOError): {e}")
            self._handle_disconnection()
            return None
        except Exception as e: 
            # Catch generic Exception for compatibility (e.g., instead of hid.HIDException)
            # Handle read timeout
            print(f"👻 HID read failed (Timeout?): {e}")
            return None

    # ===================================================
    # 3. Public Functions (SET) - Device Configuration
    # ===================================================

    def set_ld_on(self, state: bool) -> bool:
        """Turns the laser (LD) ON or OFF."""
        cmd_val = 0x01 if state else 0x00
        # Uses CMD_SET_LD_ON_OFF (now 0x07)
        return self._send_command(self.CMD_SET_LD_ON_OFF, [cmd_val])

    def set_tec_on(self, state: bool) -> bool:
        """Turns the Temperature Controller (TEC) ON or OFF."""
        cmd_val = 0x01 if state else 0x00
        # Uses CMD_SET_TEC_ON_OFF (now 0x06)
        return self._send_command(self.CMD_SET_TEC_ON_OFF, [cmd_val])

    def set_trigger_mode(self, pg1: bool, pg2: bool, ext: bool) -> bool:
        """Sets the trigger source. (PG1, PG2, External)"""
        data_byte = 0x00
        if pg1: data_byte |= 0x01  # Bit 0 for PG1
        if pg2: data_byte |= 0x02  # Bit 1 for PG2
        if ext: data_byte |= 0x04  # Bit 2 for External
        return self._send_command(self.CMD_SET_TRIGGER, [data_byte])

    def set_bias_current(self, current_ma: float) -> bool:
        """Sets the Bias Current. (Unit: mA, max 200mA)"""
        hb, lb = self._val_to_dac(current_ma, max_ma=200.0)
        return self._send_command(self.CMD_SET_BIAS, [hb, lb])

    def set_pulse_current(self, current_ma: float) -> bool:
        """Sets the Pulse Current (LD Current). (Unit: mA, max 200mA)"""
        hb, lb = self._val_to_dac(current_ma, max_ma=200.0)
        return self._send_command(self.CMD_SET_PULSE, [hb, lb])
        
    def set_temp(self, temp_c: float) -> bool:
        """Sets the target TEC temperature. (Unit: °C, max 40°C)"""
        # The max value for SetTemp is assumed to be 40.0 (based on GetTemp logic)
        hb, lb = self._val_to_dac(temp_c, max_ma=40.0) 
        return self._send_command(self.CMD_SET_TEMP, [hb, lb])

    # --- [NEW] Frequency Set Functions ---
    def _freq_to_4bytes(self, freq_hz: int) -> List[int]:
        """Converts a Hz integer into 4 bytes (Big Endian)."""
        freq_hz = int(freq_hz)
        b1 = (freq_hz >> 24) & 0xFF
        b2 = (freq_hz >> 16) & 0xFF
        b3 = (freq_hz >> 8) & 0xFF
        b4 = freq_hz & 0xFF
        return [b1, b2, b3, b4]

    def set_pg1_frequency(self, freq_hz: int) -> bool:
        """Sets Internal Oscillator 1 (High Speed) frequency."""
        # PG1 (High speed) 100kHz - 250MHz
        if not 100_000 <= freq_hz <= 250_000_000:
            print(f"⚠️ Warning: PG1 Frequency {freq_hz}Hz is outside the recommended range (100kHz-250MHz).")
        payload = self._freq_to_4bytes(freq_hz)
        return self._send_command(self.CMD_SET_PG1_FREQ, payload)

    def set_pg2_frequency(self, freq_hz: int) -> bool:
        """Sets Internal Oscillator 2 (Low Speed) frequency."""
        # PG2 (Low speed) 3kHz - 200kHz
        if not 3_000 <= freq_hz <= 200_000:
             print(f"⚠️ Warning: PG2 Frequency {freq_hz}Hz is outside the recommended range (3kHz-200kHz).")
        payload = self._freq_to_4bytes(freq_hz)
        return self._send_command(self.CMD_SET_PG2_FREQ, payload)
    # --- [END NEW] ---

    # ===================================================
    # 4. Public Functions (GET) - Device Status Reading
    # ===================================================
    def update_status(self) -> bool:
        """
        Reads all key device statuses at once and stores them in the internal 'self.status' dict.
        """
        data = self._read_command(self.CMD_GET_ALL_STATUS)
        
        if data is None:
            return False

        try:
            # PD Current (Bytes [1] and [2])
            self.status['pd_raw'] = (data[1] << 8) | data[2]
            # LD Temperature (Bytes [4] and [5])
            raw_ld_temp = (data[4] << 8) | data[5]
            self.status['ld_temp'] = (raw_ld_temp / 1023.0) * 40.0
            
            if len(data) > 17:
                 self.status['tec_current_raw'] = (data[17] << 8) | data[8]
            else:
                 self.status['tec_current_raw'] = 0

            self.status['pulse'] = self._dac_to_val(data[9], data[10], 200.0)
            self.status['bias'] = self._dac_to_val(data[7], data[8], 200.0)
            
            info_byte = data[14]
            self.status['ld_on'] = (info_byte & 4) == 4
            self.status['tec_on'] = (info_byte & 8) == 8

            # --- [수정 사항] 날짜별 로그 관리 및 LD ON 조건부 저장 ---
            if self.status.get('ld_on', False):
                now = datetime.now()
                today_str = now.strftime('%Y%m%d')
                current_log_file = os.path.join(DATA_LOG_DIR, f"laser_data_{today_str}.csv")
                
                # 날짜가 바뀌었는지 확인 (파일이 존재하지 않으면 핸들러 교체)
                if not os.path.exists(current_log_file):
                    self._setup_daily_logger(current_log_file)

                try:
                    timestamp = now.isoformat()
                    csv_line = "{},{ld},{tec},{temp:.3f},{bias:.3f},{pulse:.3f}".format(
                        timestamp,
                        ld=self.status['ld_on'],
                        tec=self.status['tec_on'],
                        temp=self.status['ld_temp'],
                        bias=self.status['bias'],
                        pulse=self.status['pulse']
                    )
                    data_logger.info(csv_line)
                except Exception as e:
                    print(f"Failed to write data log: {e}")

            return True
        except (IndexError, Exception) as e:
            print(f"❌ Status parse error: {e}")
            return False

    def get_cached_status(self, key: str, default_val=0.0):
        return self.status.get(key, default_val)

    def _setup_daily_logger(self, file_path):
        """날짜가 바뀔 때 핸들러를 새로 고침하여 새 파일에 기록하게 함"""
        for handler in data_logger.handlers[:]:
            data_logger.removeHandler(handler)

        new_handler = logging.FileHandler(file_path)
        if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
            # 파일이 없거나 비어있을 때만 헤더 추가
            with open(file_path, 'w') as f:
                f.write("timestamp,ld_on,tec_on,temp_c,bias_ma,pulse_ma\n")
        data_logger.addHandler(new_handler)

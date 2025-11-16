#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Tamadenshi (Tama Electric) Pico-second LD 보드 제어 라이브러리 (Python 3)

tmHIDLD.dll 파일을 리버스 엔지니어링하여 Linux (hidapi) 환경에서
직접 HID 명령을 전송하도록 재구현한 코드입니다.

실행 환경:
- Python 3.7+
- hidapi 라이브러리 (pip3 install hidapi)
"""

import hid
import os
import logging
import math
from datetime import datetime
from typing import Optional, List, Union

# --- 데이터 로거 설정 ---
# CSV 데이터 로그 (그래프용)
DATA_LOG_DIR = os.path.expanduser("~/ADC/ADC_test/LOG/LASER")
os.makedirs(DATA_LOG_DIR, exist_ok=True)
DATA_LOG_FILE = os.path.join(DATA_LOG_DIR, f"laser_data_{datetime.now().strftime('%Y%m%d')}.csv")

# 데이터 로거 설정
data_logger = logging.getLogger('LaserDataLogger')
data_logger.setLevel(logging.INFO)
# 파일 핸들러가 이미 추가되었는지 확인 (스크립트 재로딩 방지)
if not data_logger.hasHandlers():
    data_handler = logging.FileHandler(DATA_LOG_FILE)
    
    # CSV 헤더 추가 (파일이 비어있을 때만)
    try:
        if os.path.getsize(DATA_LOG_FILE) == 0:
            data_handler.setFormatter(logging.Formatter('%(message)s'))
            data_logger.addHandler(data_handler)
            data_logger.info("timestamp,ld_on,tec_on,temp_c,bias_ma,pulse_ma")
        else:
            data_handler.setFormatter(logging.Formatter('%(message)s'))
            data_logger.addHandler(data_handler)
    except FileNotFoundError:
        # 파일이 방금 생성되었지만 os.path.getsize가 즉시 인식 못하는 경우 예외 처리
        data_handler.setFormatter(logging.Formatter('%(message)s'))
        data_logger.addHandler(data_handler)
        data_logger.info("timestamp,ld_on,tec_on,temp_c,bias_ma,pulse_ma")
        
    data_logger.propagate = False # 상위 로거로 전파 방지
# -------------------------


class TamadenshiLaser:
    
    # 1. 하드웨어 정보 (tmHIDLD.dll 디컴파일로 확인)
    VENDOR_ID = 0x04D8
    PRODUCT_ID = 0xFA73
    
    PACKET_LENGTH = 65
    REPORT_ID = 0x00

    # 'SET' 명령어 코드 (바이트 [1])
    CMD_SET_LD_ON_OFF = 0x06     # (LDOnOff)
    CMD_SET_TEC_ON_OFF = 0x07    # (TECOnOff)
    CMD_SET_TEMP = 0x0A          # (SetTemp)
    CMD_SET_TRIGGER = 0x0E       # (SetPGOnOff)
    CMD_SET_BIAS = 0x13          # (SetBias)
    CMD_SET_PULSE = 0x14         # (SetLDCurrent)
    CMD_SET_PG1_FREQ = 0x0F      # (SetPG1Repetition) <-- [NEW]
    CMD_SET_PG2_FREQ = 0x10      # (SetPG2Repetition) <-- [NEW]

    # 'GET' 명령어 코드 (바이트 [1])
    CMD_GET_ALL_STATUS = 0x09    # (GetPD) - 모든 상태를 한 번에 읽어옴

    def __init__(self):
        """레이저 컨트롤러 클래스를 초기화합니다."""
        self.device: Optional[hid.device] = None
        self.status = {}  # 장치에서 읽어온 상태를 저장할 딕셔너리
        print(f"컨트롤러 초기화 (VID: {self.VENDOR_ID:04x}, PID: {self.PRODUCT_ID:04x})")

    def connect(self) -> (bool, str):
        """
        하드웨어에 연결을 시도합니다.
        (성공 여부, 메시지) 튜플을 반환합니다.
        """
        if self.device:
            self.disconnect()
            
        try:
            self.device = hid.device()
            self.device.open(self.VENDOR_ID, self.PRODUCT_ID)
            prod_str = self.device.get_product_string()
            msg = f"장치 연결 성공: {prod_str}"
            print(f"✅ {msg}")
            return True, msg
        except IOError as e:
            msg = f"장치 연결 실패: {e}\n  1. 장치가 USB에 연결되어 있나요?\n  2. (Linux) 'sudo'로 실행했거나, udev 규칙이 설정되었나요?"
            print(f"❌ {msg}")
            self.device = None
            return False, msg
        except Exception as e:
            msg = f"알 수 없는 오류: {e}"
            print(f"❌ {msg}")
            self.device = None
            return False, msg

    def disconnect(self):
        """장치 연결을 명시적으로 해제합니다."""
        if self.device:
            try:
                self.device.close()
            except Exception:
                pass 
            self.device = None
            print("🔌 장치 연결 해제됨.")

    def is_connected(self) -> bool:
        """현재 장치가 연결된 것으로 알려져 있는지 확인합니다."""
        return self.device is not None

    def _handle_disconnection(self):
        """(내부 함수) IO 오류 발생 시 호출되어 연결 상태를 리셋합니다."""
        print("🔌 [오류] 장치 연결이 끊겼습니다. (USB 케이블 확인)")
        self.disconnect()

    # --- 값 변환 헬퍼 함수 ---

    def _val_to_dac(self, val_ma, max_ma=200.0) -> (int, int):
        """(내부 함수) mA 값을 2바이트 DAC 값(High, Low)으로 변환합니다."""
        raw_val = int(round(val_ma * 4095.0 / max_ma))
        
        if raw_val < 0 or raw_val > 4095:
             # print(f"⚠️ 경고: {val_ma}mA는 유효 범위를 벗어납니다. (0~{max_ma}mA)")
             raw_val = max(0, min(4095, raw_val))

        high_byte = (raw_val >> 8) & 0xFF
        low_byte = raw_val & 0xFF
        return high_byte, low_byte
    
    def _dac_to_val(self, high_byte, low_byte, max_ma=200.0) -> float:
        """(내부 함수) 2바이트 DAC 값(High, Low)을 mA 값으로 변환합니다."""
        raw_val = (high_byte << 8) | low_byte
        return (raw_val * max_ma) / 4095.0

    # --- 저수준 HID 통신 함수 ---

    def _send_command(self, cmd_code: int, payload_bytes: List[int] = []) -> bool:
        """(내부 함수) HID 리포트(Write Only)를 전송합니다."""
        if not self.is_connected():
            print("❌ 명령 전송 실패: 장치가 연결되어 있지 않습니다.")
            return False

        report = [0x00] * self.PACKET_LENGTH
        report[0] = self.REPORT_ID
        report[1] = cmd_code
        
        for i, byte_val in enumerate(payload_bytes):
            if (i + 2) < self.PACKET_LENGTH:
                report[i + 2] = byte_val

        try:
            self.device.write(report)
            return True
        except (IOError, ValueError, OSError) as e:
            print(f"👻 명령 전송 실패: {e}")
            self._handle_disconnection()
            return False
            
    def _read_command(self, cmd_code: int, payload_bytes: List[int] = []) -> Union[bytearray, None]:
        """(내부 함수) 명령을 보내고(Write) 응답(Read)을 받습니다."""
        if not self.is_connected():
            print("❌ 명령 수신 실패: 장치가 연결되어 있지 않습니다.")
            return None
            
        report = [0x00] * self.PACKET_LENGTH
        report[0] = self.REPORT_ID
        report[1] = cmd_code

        for i, byte_val in enumerate(payload_bytes):
            if (i + 2) < self.PACKET_LENGTH:
                report[i + 2] = byte_val
        
        try:
            self.device.write(report)
            # 65바이트 응답을 1초 타임아웃으로 읽음
            data = self.device.read(self.PACKET_LENGTH, timeout=1000) 
            if data:
                return data[1:] # Report ID 제외
            else:
                print("❌ 명령 수신 실패: 장치에서 빈 응답이 돌아왔습니다.")
                return None
        except (IOError, ValueError, OSError) as e:
            print(f"👻 명령 수신 실패: {e}")
            self._handle_disconnection()
            return None
        except hid.HIDException as e:
            print(f"👻 HID 읽기 실패 (타임아웃?): {e}")
            return None

    # ===================================================
    # 3. 공개 함수 (SET) - 장치 설정
    # ===================================================

    def set_ld_on(self, state: bool) -> bool:
        """레이저를 켜거나 끕니다."""
        cmd_val = 0x01 if state else 0x00
        return self._send_command(self.CMD_SET_LD_ON_OFF, [cmd_val])

    def set_tec_on(self, state: bool) -> bool:
        """온도 조절 장치(TEC)를 켜거나 끕니다."""
        cmd_val = 0x01 if state else 0x00
        return self._send_command(self.CMD_SET_TEC_ON_OFF, [cmd_val])

    def set_trigger_mode(self, pg1: bool, pg2: bool, ext: bool) -> bool:
        """트리거 소스를 설정합니다. (PG1, PG2, External)"""
        data_byte = 0x00
        if pg1: data_byte |= 0x01
        if pg2: data_byte |= 0x02
        if ext: data_byte |= 0x04
        return self._send_command(self.CMD_SET_TRIGGER, [data_byte])

    def set_bias_current(self, current_ma: float) -> bool:
        """Bias Current를 설정합니다. (단위: mA, 최대 200mA)"""
        hb, lb = self._val_to_dac(current_ma, max_ma=200.0)
        return self._send_command(self.CMD_SET_BIAS, [hb, lb])

    def set_pulse_current(self, current_ma: float) -> bool:
        """Pulse Current (LD Current)를 설정합니다. (단위: mA, 최대 200mA)"""
        hb, lb = self._val_to_dac(current_ma, max_ma=200.0)
        return self._send_command(self.CMD_SET_PULSE, [hb, lb])
        
    def set_temp(self, temp_c: float) -> bool:
        """TEC 목표 온도를 설정합니다. (단위: °C, 최대 40°C)"""
        # SetTemp의 coe(최대값)는 40.0으로 추정됨 (GetTemp 로직 기반)
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
            print(f"⚠️ 경고: PG1 Frequency {freq_hz}Hz는 권장 범위 (100kHz-250MHz) 밖입니다.")
        payload = self._freq_to_4bytes(freq_hz)
        return self._send_command(self.CMD_SET_PG1_FREQ, payload)

    def set_pg2_frequency(self, freq_hz: int) -> bool:
        """Sets Internal Oscillator 2 (Low Speed) frequency."""
        # PG2 (Low speed) 3kHz - 200kHz
        if not 3_000 <= freq_hz <= 200_000:
             print(f"⚠️ 경고: PG2 Frequency {freq_hz}Hz는 권장 범위 (3kHz-200kHz) 밖입니다.")
        payload = self._freq_to_4bytes(freq_hz)
        return self._send_command(self.CMD_SET_PG2_FREQ, payload)
    # --- [END NEW] ---

    # ===================================================
    # 4. 공개 함수 (GET) - 장치 상태 읽기
    # ===================================================

    def update_status(self) -> bool:
        """
        장치의 모든 주요 상태를 한 번에 읽어와 내부 'self.status'에 저장합니다.
        (DLL의 GetPD 함수(0x09)를 호출)
        """
        data = self._read_command(self.CMD_GET_ALL_STATUS)
        
        if data is None:
            return False

        try:
            # GetPD 디컴파일 코드 기반으로 데이터 파싱
            # (바이트 인덱스는 DLL 코드 대비 +1, Report ID 0번 제외)
            
            # PD Current (복잡한 로그 스케일, 여기서는 raw 값만 저장)
            self.status['pd_raw'] = (data[1] << 8) | data[2]
            
            # LD Temperature (단순화된 40도 스케일)
            raw_ld_temp = (data[4] << 8) | data[5]
            self.status['ld_temp'] = (raw_ld_temp / 1023.0) * 40.0 # (단순화된 근사치)
            
            # TEC Current (복잡, 여기서는 raw 값만 저장)
            self.status['tec_current_raw'] = (data[17] << 8) | data[8]
            
            # Bias, Pulse (DLL과 동일한 로직)
            self.status['bias'] = self._dac_to_val(data[9], data[10], 200.0)
            self.status['pulse'] = self._dac_to_val(data[11], data[12], 200.0)
            
            # Info Byte (LD/TEC 상태 포함)
            info_byte = data[16]
            self.status['ld_on'] = (info_byte & 4) == 4
            self.status['tec_on'] = (info_byte & 8) == 8

            # --- [NEW] Log to CSV file ---
            try:
                timestamp = datetime.now().isoformat()
                csv_line = "{},{ld},{tec},{temp:.3f},{bias:.3f},{pulse:.3f}".format(
                    timestamp,
                    self.status['ld_on'],
                    self.status['tec_on'],
                    self.status['ld_temp'],
                    self.status['bias'],
                    self.status['pulse']
                )
                data_logger.info(csv_line)
            except Exception as e:
                print(f"Failed to write data log: {e}")
            # --- [NEW] End ---
            
            return True
        except IndexError:
            print("❌ 상태 파싱 실패: 장치에서 예상치 못한 데이터를 반환했습니다.")
            return False
        except Exception as e:
            print(f"❌ 상태 파싱 중 알 수 없는 오류: {e}")
            return False
    
    # --- 캐시된 상태 값 반환 ---
    # (update_status()를 먼저 호출해야 최신 값이 됩니다)
    
    def get_cached_status(self, key: str, default_val=0.0):
        """내부 self.status 딕셔너리에서 값을 안전하게 가져옵니다."""
        return self.status.get(key, default_val)

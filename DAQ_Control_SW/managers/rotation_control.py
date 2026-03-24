# managers/rotation_control.py
import os
import json
import time
import struct
import threading
from tkinter import messagebox
from pymodbus.client.sync import ModbusTcpClient

class RotationManager:
    def __init__(self, controller):
        self.controller = controller
        self.controller._log("Native Modbus Manager initialized. Direct hardware control active.")
        
        self.devices = {
            2: {"host": "192.168.10.211", "port": 502, "unit": 1},
            3: {"host": "192.168.10.212", "port": 502, "unit": 1}
        }
        
        # Added Read Addresses
        self.addr = {
            "write_tilt": 104, "write_rot": 4,
            "move_tilt": 511,  "move_rot": 501,
            "stop_tilt": 810,  "stop_rot": 800,
            "read_tilt": 432,  "read_rot": 422 
        }

        self.is_monitoring = False

        self.is_moving = {2: False, 3: False}
        self.target_angles = {
            2: {"tilt": None, "rot": None},
            3: {"tilt": None, "rot": None}
        }

    def _get_config_and_client(self, dev_num):
        cfg_file = f"config_dev{dev_num}.json"
        cfg_path = os.path.join(self.controller.base_dir, cfg_file)
        
        # JSON 파일이 없으면 기본 설정(self.devices) 사용
        if not os.path.exists(cfg_path):
            self.controller._log(f"WARNING: {cfg_file} not found. Using default settings for Dev {dev_num}.")
            if dev_num in self.devices:
                cfg = {"connection": {"host": self.devices[dev_num]["host"], "unit": self.devices[dev_num]["unit"]}}
                client = ModbusTcpClient(host=cfg["connection"]["host"], port=502, timeout=3)
                return client, cfg
            else:
                self.controller._log(f"ERROR: No default settings for Dev {dev_num}.")
                return None, None
                
        # JSON 파일이 있으면 로드
        try:
            with open(cfg_path, 'r', encoding='utf-8') as f: 
                cfg = json.load(f)
            client = ModbusTcpClient(host=cfg["connection"]["host"], port=502, timeout=3)
            return client, cfg
        except Exception as e: 
            self.controller._log(f"ERROR: Failed to load {cfg_file}: {e}")
            return None, None
    
    def _pack_32bit(self, value):
        scaled_val = int(round(float(value) * 250.0))
        b = struct.pack(">i", scaled_val)
        hi, lo = (b[0] << 8) | b[1], (b[2] << 8) | b[3]
        return [lo, hi]

    def _unpack_32bit_read(self, regs):
        """Restore 32-bit signed int from two 16-bit registers (Little-endian words)."""
        lo, hi = regs[0], regs[1]
        b = bytes([(hi >> 8) & 0xFF, hi & 0xFF, (lo >> 8) & 0xFF, lo & 0xFF])
        return struct.unpack(">i", b)[0]

    def _pulse_trigger(self, client, unit, address):
        time.sleep(0.5)
        client.write_coil(address, True, unit=unit)
        time.sleep(0.5)
        client.write_coil(address, False, unit=unit)

    def move_rotation(self, dev_num, tilt, rot):
        """Hardware Control Sequence."""
        if not self.controller.access_mgr.unlocked: return
        client, cfg = self._get_config_and_client(dev_num)
        if not client or not client.connect(): return
        
        try:
            unit = cfg["connection"]["unit"]
            if tilt != 'x': self._pulse_trigger(client, unit, self.addr["stop_tilt"])
            if rot != 'x': self._pulse_trigger(client, unit, self.addr["stop_rot"])

            if tilt != 'x':
                client.write_registers(self.addr["write_tilt"], self._pack_32bit(tilt), unit=unit)
                self._pulse_trigger(client, unit, self.addr["move_tilt"])
                self.controller._log(f"Device {dev_num} Tilt command sent: {tilt} deg")

            if rot != 'x':
                client.write_registers(self.addr["write_rot"], self._pack_32bit(rot), unit=unit)
                self._pulse_trigger(client, unit, self.addr["move_rot"])
                self.controller._log(f"Device {dev_num} Rotation command sent: {rot} deg")
        finally:
            client.close()

    def move_tilt_only(self, dev_num, tilt):
        """Move only the Tilt axis."""
        if not self.controller.access_mgr.unlocked: return

        if self.is_moving[dev_num]:
            self.controller._log(f"WARNING: Device {dev_num} is already moving! Command ignored.")
            return
        self.is_moving[dev_num] = True
        self.target_angles[dev_num] = {"tilt": tilt, "rot": None}

        client, cfg = self._get_config_and_client(dev_num)
        if not client or not client.connect(): return

        try:
            unit = cfg["connection"]["unit"]
            self._pulse_trigger(client, unit, self.addr["stop_tilt"])
            client.write_registers(self.addr["write_tilt"], self._pack_32bit(tilt), unit=unit)
            self._pulse_trigger(client, unit, self.addr["move_tilt"])
            self.controller._log(f"Device {dev_num} TILT ONLY command sent: {tilt} deg")
        except Exception as e:
            self.controller._log(f"ERROR: Modbus Tilt Error (Dev {dev_num}): {e}")
        finally:
            client.close()

    def move_rot_only(self, dev_num, rot):
        """Move only the Rotation axis with Safety Interlock."""
        if not self.controller.access_mgr.unlocked: return

        if self.is_moving[dev_num]:
            self.controller._log(f"WARNING: Device {dev_num} is already moving! Command ignored.")
            return

        current_tilt, _ = self.read_angles(dev_num)
        if current_tilt is not None and abs(current_tilt) > 0.5:
            error_msg = f"ERROR: SAFETY INTERLOCK! Cannot rotate. Tilt is {current_tilt:.1f} deg. Must be 0.0 deg."
            self.controller._log(error_msg)
            from tkinter import messagebox
            messagebox.showerror("Safety Interlock", f"Cannot rotate Device {dev_num}!\n\nTilt must be 0.0° before rotating.\nCurrent tilt is {current_tilt:.1f}°.")
            return

        client, cfg = self._get_config_and_client(dev_num)
        if not client or not client.connect(): return

        try:
            unit = cfg["connection"]["unit"]
            self._pulse_trigger(client, unit, self.addr["stop_rot"])
            client.write_registers(self.addr["write_rot"], self._pack_32bit(rot), unit=unit)
            self._pulse_trigger(client, unit, self.addr["move_rot"])
            self.controller._log(f"Device {dev_num} ROTATION ONLY command sent: {rot} deg")
        except Exception as e:
            self.controller._log(f"ERROR: Modbus Rot Error (Dev {dev_num}): {e}")
        finally:
            client.close()

    def stop_rotation(self, dev_num):
        """Send hardware stop signals to the motors."""
        self.is_moving[dev_num] = False
        self.target_angles[dev_num] = {"tilt": None, "rot": None}
        self.controller._log(f"Device {dev_num} Lock released due to STOP command.")

        client, cfg = self._get_config_and_client(dev_num)
        if not client or not client.connect(): return

        try:
            unit = cfg["connection"]["unit"]
            # 틸트(Tilt)와 로테이션(Rot) 모두에 정지 펄스 전송
            self._pulse_trigger(client, unit, self.addr["stop_tilt"])
            self._pulse_trigger(client, unit, self.addr["stop_rot"])
            self.controller._log(f"Device {dev_num} Hardware STOP command sent.")
        except Exception as e:
            self.controller._log(f"ERROR: Modbus Stop Error (Dev {dev_num}): {e}")
        finally:
            client.close()

    def read_angles(self, dev_num):
        """Read actual Tilt and Rotation angles from hardware."""
        client, cfg = self._get_config_and_client(dev_num)
        if not client or not client.connect(): return None, None

        tilt_deg, rot_deg = None, None
        try:
            unit = cfg["connection"]["unit"]
            
            # Read Tilt
            res_tilt = client.read_holding_registers(self.addr["read_tilt"], 2, unit=unit)
            if not res_tilt.isError():
                tilt_raw = self._unpack_32bit_read(res_tilt.registers)
                tilt_deg = tilt_raw / 250.0

            # Read Rotation
            res_rot = client.read_holding_registers(self.addr["read_rot"], 2, unit=unit)
            if not res_rot.isError():
                rot_raw = self._unpack_32bit_read(res_rot.registers)
                rot_deg = rot_raw / 250.0
                
        except Exception as e:
            pass # Keep silent to avoid flooding the log during background monitoring
        finally:
            client.close()
            
        return tilt_deg, rot_deg

    def start_monitoring(self, update_callback):
        """Starts a background thread to update the UI with current angles."""
        if self.is_monitoring: return
        self.is_monitoring = True
        
        def monitor_loop():
            self.controller._log("Started background hardware monitoring thread.")
            while self.is_monitoring:
                for dev_num in [2, 3]:
                    tilt, rot = self.read_angles(dev_num)
                    
                    if update_callback:
                        update_callback(dev_num, tilt, rot)
                    
                    # === [잠금 해제] 목표 각도에 도달했는지 실시간 체크 ===
                    if self.is_moving[dev_num]:
                        target = self.target_angles[dev_num]
                        reached_tilt = True
                        reached_rot = True
                        
                        if target["tilt"] is not None and tilt is not None:
                            if abs(tilt - target["tilt"]) > 0.5: # 0.5도 오차 허용
                                reached_tilt = False
                        if target["rot"] is not None and rot is not None:
                            if abs(rot - target["rot"]) > 0.5:
                                reached_rot = False
                                
                        if reached_tilt and reached_rot:
                            self.is_moving[dev_num] = False
                            self.target_angles[dev_num] = {"tilt": None, "rot": None}
                            self.controller._log(f"Device {dev_num} reached target. Lock automatically released.")
                    # =======================================================


                time.sleep(1.0) # Polling interval (1 second)

        # Use daemon=True so thread dies when program closes
        threading.Thread(target=monitor_loop, daemon=True).start()

    def stop_monitoring(self):
        """Stops the background monitoring thread."""
        self.is_monitoring = False
        self.controller._log("Stopped background hardware monitoring.")

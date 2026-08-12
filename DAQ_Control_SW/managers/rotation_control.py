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

        # Persistent Modbus TCP connections, one per device, reused across
        # calls instead of connect()+close() on every single command/poll.
        # The old per-call reconnect pattern hammered the stage's embedded
        # Modbus server with a fresh TCP handshake every 0.5-1s from the
        # background monitor thread alone — the suspected root cause of the
        # comm timeouts that hung General Scan and Reset Angle (2026-07-12/13).
        self._clients = {2: None, 3: None}

        self.is_moving = {2: False, 3: False}
        self.target_angles = {
            2: {"tilt": None, "rot": None},
            3: {"tilt": None, "rot": None}
        }

    def _get_config_and_client(self, dev_num):
        cfg_file = f"config_dev{dev_num}.json"
        cfg_path = os.path.join(self.controller.base_dir, cfg_file)
        
        if not os.path.exists(cfg_path):
            #self.controller._log(f"WARNING: {cfg_file} not found. Using default settings for Dev {dev_num}.")
            if dev_num in self.devices:
                cfg = {"connection": {"host": self.devices[dev_num]["host"], "unit": self.devices[dev_num]["unit"]}}
                client = ModbusTcpClient(host=cfg["connection"]["host"], port=502, timeout=3)
                return client, cfg
            else:
                self.controller._log(f"ERROR: No default settings for Dev {dev_num}.")
                return None, None
                
        try:
            with open(cfg_path, 'r', encoding='utf-8') as f: 
                cfg = json.load(f)
            client = ModbusTcpClient(host=cfg["connection"]["host"], port=502, timeout=3)
            return client, cfg
        except Exception as e:
            self.controller._log(f"ERROR: Failed to load {cfg_file}: {e}")
            return None, None

    def _get_persistent_client(self, dev_num):
        """Like _get_config_and_client, but reuses one long-lived TCP
        connection per device instead of reconnecting on every call. Returns
        (client, cfg), or (None, cfg-or-None) on failure."""
        cfg_file = f"config_dev{dev_num}.json"
        cfg_path = os.path.join(self.controller.base_dir, cfg_file)
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path, 'r', encoding='utf-8') as f:
                    cfg = json.load(f)
            except Exception as e:
                self.controller._log(f"ERROR: Failed to load {cfg_file}: {e}")
                return None, None
        elif dev_num in self.devices:
            cfg = {"connection": {"host": self.devices[dev_num]["host"], "unit": self.devices[dev_num]["unit"]}}
        else:
            self.controller._log(f"ERROR: No default settings for Dev {dev_num}.")
            return None, None

        client = self._clients.get(dev_num)
        if client is not None and client.is_socket_open():
            return client, cfg

        if client is not None:
            try:
                client.close()
            except Exception:
                pass

        client = ModbusTcpClient(host=cfg["connection"]["host"], port=502, timeout=3)
        if not client.connect():
            self._clients[dev_num] = None
            return None, cfg
        self._clients[dev_num] = client
        return client, cfg

    def _invalidate_client(self, dev_num):
        """Drop the cached connection for dev_num after a Modbus error, so
        the next call opens a fresh socket instead of reusing a possibly-bad
        one."""
        client = self._clients.get(dev_num)
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        self._clients[dev_num] = None

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
        client, cfg = self._get_persistent_client(dev_num)
        if not client: return

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
        except Exception as e:
            self.controller._log(f"ERROR: Modbus Rotation Error (Dev {dev_num}): {e}")
            self._invalidate_client(dev_num)

    def _angle_in_range(self, axis_label, value, skip_lock):
        """Reject a move outside the axis' safe range.

        TILT is bounded by auto_mgr.scan_range [-55, 55]; ROTATION by
        auto_mgr.rot_range [0, 135] -- they are DIFFERENT mechanical limits.
        Applying the tilt range to rotation used to silently reject any
        rotation > 55deg: the motor never started, and the stall watchdog
        then raised a false 'MECHANICAL BLOCKAGE' abort (seen 2026-07-24 on
        Device 3 during a Repeat-Angles recheck at rot offset 45deg)."""
        if axis_label == "rotation":
            rng = self.controller.auto_mgr.rot_range
        else:
            rng = self.controller.auto_mgr.scan_range
        if rng["start"] <= value <= rng["end"]:
            return True
        msg = (f"Requested {axis_label} {value}° is outside the allowed range "
               f"[{rng['start']}°, {rng['end']}°].")
        self.controller._log(f"ERROR: {msg}")
        if not skip_lock:
            messagebox.showerror("Angle Out of Range", msg)
        return False

    def move_tilt_only(self, dev_num, tilt, skip_lock=False):
        if not skip_lock and not self.controller.access_mgr.unlocked: return
        if not self._angle_in_range("tilt", tilt, skip_lock): return

        if self.is_moving[dev_num]:
            self.controller._log(f"WARNING: Device {dev_num} is already moving! Command ignored.")
            return
        self.is_moving[dev_num] = True
        self.target_angles[dev_num] = {"tilt": tilt, "rot": None}

        client, cfg = self._get_persistent_client(dev_num)
        if not client:
            # Connection failed: release the lock, otherwise the device stays "moving"
            # forever and every later command is silently ignored.
            self.is_moving[dev_num] = False
            self.target_angles[dev_num] = {"tilt": None, "rot": None}
            self.controller._log(f"ERROR: Modbus connect failed (Dev {dev_num}). Lock released.")
            return

        try:
            unit = cfg["connection"]["unit"]
            self._pulse_trigger(client, unit, self.addr["stop_tilt"])
            client.write_registers(self.addr["write_tilt"], self._pack_32bit(tilt), unit=unit)
            self._pulse_trigger(client, unit, self.addr["move_tilt"])
            self.controller._log(f"Device {dev_num} TILT ONLY command sent: {tilt} deg")
        except Exception as e:
            self.controller._log(f"ERROR: Modbus Tilt Error (Dev {dev_num}): {e}")
            self._invalidate_client(dev_num)

    def move_rot_only(self, dev_num, rot, skip_lock=False):
        if not skip_lock and not self.controller.access_mgr.unlocked: return
        if not self._angle_in_range("rotation", rot, skip_lock): return

        if self.is_moving[dev_num]:
            self.controller._log(f"WARNING: Device {dev_num} is already moving! Command ignored.")
            return

        self.is_moving[dev_num] = True
        self.target_angles[dev_num] = {"tilt": None, "rot": rot}

        current_tilt, _ = self.read_angles(dev_num)
        if current_tilt is not None and abs(current_tilt) > 0.5:
            error_msg = f"ERROR: SAFETY INTERLOCK! Cannot rotate. Tilt is {current_tilt:.1f} deg. Must be 0.0 deg."
            self.controller._log(error_msg)

            # Blocked by interlock: release the lock so the device isn't stuck "moving".
            self.is_moving[dev_num] = False
            self.target_angles[dev_num] = {"tilt": None, "rot": None}

            if not skip_lock:
                messagebox.showerror("Safety Interlock", f"Cannot rotate Device {dev_num}!\n\nTilt must be 0.0° before rotating.\nCurrent tilt is {current_tilt:.1f}°.")
            return


        client, cfg = self._get_persistent_client(dev_num)
        if not client:
            # Connection failed: release the lock to avoid a permanent "moving" deadlock.
            self.is_moving[dev_num] = False
            self.target_angles[dev_num] = {"tilt": None, "rot": None}
            self.controller._log(f"ERROR: Modbus connect failed (Dev {dev_num}). Lock released.")
            return

        try:
            unit = cfg["connection"]["unit"]
            self._pulse_trigger(client, unit, self.addr["stop_rot"])
            client.write_registers(self.addr["write_rot"], self._pack_32bit(rot), unit=unit)
            self._pulse_trigger(client, unit, self.addr["move_rot"])
            self.controller._log(f"Device {dev_num} ROTATION ONLY command sent: {rot} deg")
        except Exception as e:
            self.controller._log(f"ERROR: Modbus Rot Error (Dev {dev_num}): {e}")
            self._invalidate_client(dev_num)

    def _fast_pulse_trigger(self, client, unit, address):
        """A faster pulse trigger specifically for STOP commands to avoid mechanical delay."""
        client.write_coil(address, True, unit=unit)
        time.sleep(0.1)  # Minimal delay just to register the pulse
        client.write_coil(address, False, unit=unit)

    def stop_rotation(self, dev_num):
        """Send hardware stop signals to the motors immediately."""
        self.is_moving[dev_num] = False
        self.target_angles[dev_num] = {"tilt": None, "rot": None}
        self.controller._log(f"[INFO] Device {dev_num} Lock released due to STOP command.")

        client, cfg = self._get_persistent_client(dev_num)
        if not client: return

        try:
            unit = cfg["connection"]["unit"]
            # Use the fast pulse trigger for immediate response
            self._fast_pulse_trigger(client, unit, self.addr["stop_tilt"])
            self._fast_pulse_trigger(client, unit, self.addr["stop_rot"])
            self.controller._log(f"[INFO] Device {dev_num} Hardware STOP command sent rapidly.")
        except Exception as e:
            self.controller._log(f"[ERROR] Modbus Stop Error (Dev {dev_num}): {e}")
            self._invalidate_client(dev_num)

    """
    def read_angles(self, dev_num):
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
        """
    def read_angles(self, dev_num):
        """Read actual Tilt and Rotation angles from hardware in a single Modbus request."""
        client, cfg = self._get_persistent_client(dev_num)
        if not client:
            return None, None

        tilt_deg, rot_deg = None, None
        try:
            unit = cfg["connection"]["unit"]

            # Read 12 registers starting from read_rot (422) to cover up to read_tilt (433)
            # registers[0:2] = Rotation (422, 423)
            # registers[10:12] = Tilt (432, 433)
            res = client.read_holding_registers(self.addr["read_rot"], 12, unit=unit)

            if not res.isError():
                rot_raw = self._unpack_32bit_read(res.registers[0:2])
                rot_deg = rot_raw / 250.0

                tilt_raw = self._unpack_32bit_read(res.registers[10:12])
                tilt_deg = tilt_raw / 250.0

        except Exception as e:
            self._invalidate_client(dev_num)

        return tilt_deg, rot_deg
    def start_monitoring(self, update_callback):
        """Starts a background thread to update the UI with current angles."""
        if self.is_monitoring: return
        self.is_monitoring = True
        
        def monitor_loop():
            self.controller._log("Started background hardware monitoring thread.")
            while self.is_monitoring:
                currently_moving = any(self.is_moving.values())
                for dev_num in [2, 3]:
                    tilt, rot = self.read_angles(dev_num)
                    
                    if update_callback:
                        update_callback(dev_num, tilt, rot)
                    
                    if self.is_moving[dev_num]:
                        target = self.target_angles[dev_num]
                        # An axis with a target counts as "reached" ONLY when we
                        # positively confirm it is within tolerance. If the angle
                        # read failed (None) while a move is in progress, do NOT
                        # assume it arrived — otherwise a transient Modbus read
                        # failure would release the lock early, letting the General
                        # Scan advance / take DAQ data while the motor is still
                        # physically moving (wrong-angle data, or a new command
                        # interrupting the move). An axis with no target (None) has
                        # nothing to wait for, so it starts as already reached.
                        reached_tilt = (target["tilt"] is None)
                        reached_rot  = (target["rot"]  is None)

                        if target["tilt"] is not None and tilt is not None:
                            reached_tilt = abs(tilt - target["tilt"]) <= 0.5  # 0.5도 오차 허용
                        if target["rot"] is not None and rot is not None:
                            reached_rot = abs(rot - target["rot"]) <= 0.5

                        if reached_tilt and reached_rot:
                            self.is_moving[dev_num] = False
                            self.target_angles[dev_num] = {"tilt": None, "rot": None}
                            self.controller._log(f"Device {dev_num} reached target. Lock automatically released.")

                sleep_time = 0.5 if currently_moving else 1.0
                time.sleep(sleep_time)

        threading.Thread(target=monitor_loop, daemon=True).start()

    def stop_monitoring(self):
        """Stops the background monitoring thread."""
        self.is_monitoring = False
        self.controller._log("Stopped background hardware monitoring.")

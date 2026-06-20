# diag_dev2_rot.py
import time
import struct
from pymodbus.client import ModbusTcpClient

def get_32bit_val(client, addr):
    """Read and unpack 32-bit value for verification."""
    res = client.read_holding_registers(addr, 2, slave=1)
    if res.isError(): return None
    # WORDORDER little
    lo, hi = res.registers[0], res.registers[1]
    b = bytes([(hi >> 8) & 0xFF, hi & 0xFF, (lo >> 8) & 0xFF, lo & 0xFF])
    return struct.unpack(">i", b)[0]

def diag_rotation_dev2():
    ip = "192.168.10.212" # Device 2
    print(f"--- Deep Diagnosis: Device 2 ({ip}) Rotation ---")
    client = ModbusTcpClient(host=ip, port=502, timeout=3)
    
    if not client.connect():
        print("CRITICAL: Cannot connect to Device 2.")
        return

    try:
        # Step 1: Check Current Value
        curr_raw = get_32bit_val(client, 4) # Rotation Reg: 4
        print(f"[1] Current Reg 4 Value: {curr_raw} ({curr_raw/250.0 if curr_raw else 0:.3f} deg)")

        # Step 2: Write New Value (Try moving to 5.0 degrees)
        target_deg = 5.0
        target_raw = int(target_deg * 250)
        hi, lo = (target_raw >> 16) & 0xFFFF, target_raw & 0xFFFF
        
        print(f"[2] Writing {target_deg} deg ({target_raw}) to Reg 4...")
        write_res = client.write_registers(4, [lo, hi], slave=1)
        if write_res.isError():
            print(f"!!! Write Error: {write_res}")
        else:
            # Verify if value actually changed
            ver_raw = get_32bit_val(client, 4)
            print(f"    Verification: Reg 4 is now {ver_raw}")

        # Step 3: Trigger Movement (Coil 501)
        print(f"[3] Pulsing Coil 501 (Rotation Trigger)...")
        client.write_coil(501, True, slave=1)
        time.sleep(1.0) # Wait a bit
        client.write_coil(501, False, slave=1)
        
        # Step 4: Check if moving (Reading Speed)
        # Speed Reg for Rotation: 434
        speed_res = client.read_holding_registers(434, 1, slave=1)
        if not speed_res.isError():
            print(f"[4] Current Rotation Speed: {speed_res.registers[0]}")
            if speed_res.registers[0] == 0:
                print("!!! Warning: Speed is 0. Motor is NOT moving.")
        
        print("--- Diagnosis Finished ---")

    except Exception as e:
        print(f"Unexpected Python Error: {e}")
    finally:
        client.close()

if __name__ == "__main__":
    diag_rotation_dev2()

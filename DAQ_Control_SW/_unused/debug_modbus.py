# debug_modbus.py
import struct
from pymodbus.client import ModbusTcpClient

def _call_with_unit_variants(fn, *args, unit_id: int):
    """Robust wrapper to handle pymodbus 2.x (slave=) and 3.x (unit=)."""
    try:
        return fn(*args, unit=unit_id) # pymodbus 3.x
    except TypeError:
        return fn(*args, slave=unit_id) # pymodbus 2.x

def unpack_32bit(regs):
    """Restore 32-bit signed int from two 16-bit registers."""
    # Using WORDORDER="little" as per your existing logic
    lo, hi = regs[0], regs[1]
    b = bytes([(hi >> 8) & 0xFF, hi & 0xFF, (lo >> 8) & 0xFF, lo & 0xFF])
    return struct.unpack(">i", b)[0]

def test_device(ip):
    print(f"--- Testing Device: {ip} ---")
    client = ModbusTcpClient(host=ip, port=502, timeout=3)
    
    if not client.connect():
        print(f"Connection Failed to {ip}. Please check network or IP range.")
        return

    try:
        # Read Current Tilt Angle (Addr: 432, Count: 2, Unit: 1)
        resp = _call_with_unit_variants(client.read_holding_registers, 432, 2, unit_id=1)
        
        if resp is not None and not resp.isError():
            raw_val = unpack_32bit(resp.registers)
            print(f"Success! Current Tilt Position: {raw_val / 250.0:.3f} deg")
        else:
            print(f"Modbus Error Response: {resp}")
            
    except Exception as e:
        print(f"Python Error: {e}")
    finally:
        client.close()
        print(f"Connection closed for {ip}\n")

if __name__ == "__main__":
    # Testing both identified device IPs
    test_device("192.168.10.211")
    test_device("192.168.10.212")

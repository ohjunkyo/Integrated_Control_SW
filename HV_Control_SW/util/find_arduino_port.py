import serial.tools.list_ports

def find_arduino():
    print("Searching for available serial ports...")
    ports = serial.tools.list_ports.comports()
    arduino_ports = []
    for port in ports:
        if "arduino" in port.description.lower() or "ch340" in port.description.lower() or "serial" in port.description.lower():
            arduino_ports.append(port)

    if not arduino_ports:
        print("\n[Error] No Arduino-like device found.")
        print("Please check the connection and drivers.")
        return None

    print("\n[Success] Found potential Arduino port(s):")
    for p in arduino_ports:
        print(f"- {p.device} ({p.description})")

    recommended_port = arduino_ports[0].device
    print(f"\n=> Recommended port for config.json: \"{recommended_port}\"")
    return recommended_port

if __name__ == "__main__":
    find_arduino()

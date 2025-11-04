import time, serial
from PyQt5.QtCore import QObject, pyqtSignal, QTimer
import numpy as np

class ArduinoWorker(QObject):
    data_ready = pyqtSignal(int, object, object)
    connection_status = pyqtSignal(str)

    def __init__(self, port, baud_rate):
        super().__init__()
        self.port = port
        self.baud_rate = baud_rate
        self.ser = None
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll_serial_data)

    def start_polling(self):
        self.timer.start(1000) # Poll every 1 second

    def stop_polling(self):
        self.timer.stop()
        if self.ser and self.ser.is_open:
            self.ser.close()
        print("Arduino polling stopped.")

    def run(self):
        try:
            self.connection_status.emit(f"Connecting to ENV Sensor ({self.port})...")
            self.ser = serial.Serial(self.port, self.baud_rate, timeout=2)
            self.connection_status.emit("ENV Status: Connection Successful!")
            self.start_polling()
        except serial.SerialException:
            self.connection_status.emit("ENV Status: Connection Failed!")
            # Optionally, set up a reconnect timer here
        
    def _poll_serial_data(self):
        if not self.ser or not self.ser.is_open:
            return
            
        if self.ser.in_waiting > 0:
            line = self.ser.readline().decode('utf-8').strip()
            try:
                parts = {p.split(':')[0]: p.split(':')[1] for p in line.split(',')}
                idx = int(parts.get("SENSOR", -1))
                if idx != -1:
                    if "ERROR" in parts: self.data_ready.emit(idx, np.nan, np.nan)
                    elif "TEMP" in parts and "HUMI" in parts: self.data_ready.emit(idx, float(parts["TEMP"]), float(parts["HUMI"]))
            except (ValueError, IndexError, KeyError): 
                pass

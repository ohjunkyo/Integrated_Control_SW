import time, serial
from PyQt5.QtCore import QObject, pyqtSignal, QTimer, pyqtSlot 
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
        
        # 재연결을 위한 타이머 추가
        self.reconnect_timer = QTimer(self)
        self.reconnect_timer.timeout.connect(self.run) 

    @pyqtSlot()
    def start_polling(self):
        #print("[LOG] Starting Arduino polling...") # 1. 초기 연결 로직 실행
        self.run() 
        self.timer.start(1000) # 2. 1초마다 데이터 읽기 시작
        self.reconnect_timer.start(5000) # 3. 5초마다 끊김 확인 및 자동 재연결 시작

    @pyqtSlot() 
    def stop_polling(self):
        self.timer.stop()
        self.reconnect_timer.stop() # 재연결 시도 중지
        if self.ser and self.ser.is_open:
            try:
                self.ser.close()
            except: pass
        print("Arduino polling stopped.")

    def run(self):
        """연결 시도 로직"""
        try:
            if self.ser and self.ser.is_open:
                return # 이미 연결되어 있으면 통과

            self.connection_status.emit(f"Connecting to ENV Sensor ({self.port})...")
            self.ser = serial.Serial(self.port, self.baud_rate, timeout=2)
            self.connection_status.emit("ENV Status: Connection Successful!")
            
            self.reconnect_timer.stop() # 연결 성공 시 재연결 타이머 중지
            self.start_polling()
        except (serial.SerialException, OSError):
            self.connection_status.emit("ENV Status: Connection Failed! Retrying...")
            # 연결 실패 시 5초 후 다시 시도하도록 설정
            if not self.reconnect_timer.isActive():
                self.reconnect_timer.start(5000)
    def _poll_serial_data(self):
        try:
            if not self.ser or not self.ser.is_open:
                return

            if self.ser.in_waiting > 0:
                line = self.ser.readline().decode('utf-8', errors='ignore').strip()
                
                # 1. 🚨 터미널 확인용: 아두이노가 실제로 뭐라고 보내는지 출력합니다.
                #print(f"👉 [Arduino Data] {line}") 
                
                # 2. 안전하게 쪼개기 (out of range 및 띄어쓰기 방어막)
                parts = {}
                for p in line.split(','):
                    key_val = p.split(':')
                    if len(key_val) == 2:
                        # [핵심] .strip()을 추가해서 " TEMP" 처럼 공백이 들어와도 "TEMP"로 완벽히 인식하게 만듭니다.
                        parts[key_val[0].strip()] = key_val[1].strip()
                
                # 3. 데이터 파싱
                if "SENSOR" in parts:
                    idx = int(parts.get("SENSOR", -1))
                    if idx != -1:
                        if "ERROR" in parts: 
                            self.data_ready.emit(idx, np.nan, np.nan)
                        elif "TEMP" in parts and "HUMI" in parts:
                            self.data_ready.emit(idx, float(parts["TEMP"]), float(parts["HUMI"]))

        except (OSError, serial.SerialException) as e:
            print(f"⚠️ Serial communication lost: {e}")
            self.connection_status.emit("ENV Status: Serial Lost! Retrying...")
            self.timer.stop() 
            
            try:
                self.ser.close()
            except: pass
            
            self.ser = None
            self.reconnect_timer.start(5000)        
        except Exception as e:
            print(f"Unexpected error in arduino polling: {e}")

import serial
import time

# config_precal.json에 있던 포트 이름입니다. 
# 만약 윈도우라면 'COM3' 같은 이름으로 바꿔주세요.
PORT = '/dev/arduino_env' 
BAUD_RATE = 9600

print(f"SYSTEM_LOG: Connecting to Arduino on {PORT}...")

try:
    # 아두이노와 연결
    ser = serial.Serial(PORT, BAUD_RATE, timeout=1)
    print("SYSTEM_LOG: Connection Successful! Listening to raw data...\n")
    print("-" * 50)
    
    while True:
        if ser.in_waiting > 0:
            # 아두이노가 보내는 데이터를 한 줄씩 읽어서 출력
            raw_data = ser.readline().decode('utf-8', errors='ignore').strip()
            print(f"📥 Arduino Says: {raw_data}")
            
        time.sleep(0.1)

except serial.SerialException as e:
    print(f"CRITICAL_ERROR: Could not connect to Arduino. Is the port correct? ({e})")
except KeyboardInterrupt:
    print("\nSYSTEM_LOG: Stopped by user.")
finally:
    if 'ser' in locals() and ser.is_open:
        ser.close()

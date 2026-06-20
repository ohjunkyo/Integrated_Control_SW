import serial
import time

# 1단계에서 확인한 포트로 수정 필요할 수 있음
PORT = "/dev/ttyUSB0" 

def check_ups(baud):
    print(f"\n[TEST] Connecting at {baud} bps...")
    try:
        ser = serial.Serial(PORT, baud, timeout=2)
        ser.reset_input_buffer()
        ser.write(b'S\r') # 상태 요청
        time.sleep(1.0)
        
        if ser.in_waiting > 0:
            raw = ser.read(ser.in_waiting)
            print(f"   <- RECEIVED: {raw}")
            return True
        else:
            print("   <- No Response")
        ser.close()
    except Exception as e:
        print(f"   !! Error: {e}")

if __name__ == "__main__":
    print(f"--- Checking UPS on {PORT} ---")
    check_ups(9600)
    check_ups(2400)

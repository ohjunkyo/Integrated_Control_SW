import serial
import time

# 포트 확인 (/dev/ttyUSB0 가 맞는지 다시 확인하세요)
PORT = "/dev/ttyUSB0"
BAUD = 2400

def scan_ups_commands():
    print(f"=== OMRON UPS Command Scanner ({PORT} @ {BAUD}bps) ===")
    
    try:
        ser = serial.Serial(
            port=PORT,
            baudrate=BAUD,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            bytesize=serial.EIGHTBITS,
            timeout=1.0
        )
        
        # 1. 신호선 켜기 (UPS 깨우기)
        ser.dtr = True
        ser.rts = True
        time.sleep(1.0)
        
        # 테스트할 명령어 후보들
        commands = [b'C', b'R', b'S', b'M', b'Q1', b'F', b'I']
        # 테스트할 줄바꿈 문자들 (Carriage Return, Line Feed)
        terminators = [b'\r', b'\n', b'\r\n']
        
        print("\n[Start Scanning...]")
        
        for cmd in commands:
            for term in terminators:
                full_cmd = cmd + term
                
                # 버퍼 비우고 전송
                ser.reset_input_buffer()
                ser.write(full_cmd)
                time.sleep(0.4)
                
                # 응답 읽기
                if ser.in_waiting > 0:
                    raw = ser.read(ser.in_waiting)
                    try:
                        decoded = raw.decode('ascii', errors='ignore').strip()
                    except:
                        decoded = "???"
                    
                    # 결과 출력
                    print(f"👉 Sent: {repr(full_cmd):<10} | Received: {repr(raw)}  (Decoded: '{decoded}')")
                    
                    # NAK가 아닌 유의미한 응답이 오면 강조
                    if "NAK" not in decoded and decoded != "":
                        print(f"   ⭐⭐⭐ POSSIBLE MATCH FOUND! ⭐⭐⭐")
                else:
                    print(f"👉 Sent: {repr(full_cmd):<10} | (No Response)")
                    
        ser.close()
        
    except Exception as e:
        print(f"Critical Error: {e}")

if __name__ == "__main__":
    scan_ups_commands()

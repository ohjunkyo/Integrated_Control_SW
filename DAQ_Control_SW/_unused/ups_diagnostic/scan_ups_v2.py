import serial
import time
import string

# 1. 설정
PORT = "/dev/ttyUSB0"
BAUD = 2400

def scan_v2():
    print(f"=== OMRON UPS Deep Scanner ({PORT}) ===")
    try:
        ser = serial.Serial(
            port=PORT, 
            baudrate=BAUD, 
            parity=serial.PARITY_NONE, 
            stopbits=serial.STOPBITS_ONE, 
            bytesize=serial.EIGHTBITS,
            timeout=0.5
        )
        
        # 2. 신호선 켜기 (필수)
        ser.dtr = True
        ser.rts = True
        time.sleep(1.0)

        # 3. 'R' 명령어로 접속 시도 (아까 성공한 명령어)
        print("\n[Step 1] Sending 'R' to start session...")
        ser.write(b'R\r')
        time.sleep(0.5)
        print(f"   -> Response: {ser.read(ser.in_waiting)}")
        
        # 4. A부터 Z까지 모든 명령어 테스트
        print("\n[Step 2] Scanning A-Z for Voltage Data...")
        
        # A~Z 순회
        chars = string.ascii_uppercase 
        # 혹시 몰라 소문자 a~z도 추가
        # chars += string.ascii_lowercase 
        
        for char in chars:
            cmd = char.encode() + b'\r'
            
            ser.reset_input_buffer()
            ser.write(cmd)
            time.sleep(0.3)
            
            if ser.in_waiting > 0:
                raw = ser.read(ser.in_waiting)
                try:
                    decoded = raw.decode('ascii', errors='ignore').strip()
                except:
                    decoded = "???"
                
                # 결과 출력 (응답이 있는 경우만)
                if decoded:
                    print(f"   👉 Cmd '{char}' -> : {decoded}")
                    
                    # 숫자가 포함된 긴 문자열이면 강조 (우리가 찾는 데이터!)
                    if len(decoded) > 5 and any(c.isdigit() for c in decoded):
                         print(f"      ⭐⭐⭐ BINGO! Looks like Data! ⭐⭐⭐")

        ser.close()
        print("\n=== Scan Finished ===")
        
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    scan_v2()

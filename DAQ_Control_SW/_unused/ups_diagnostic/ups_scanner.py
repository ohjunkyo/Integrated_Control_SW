import serial
import time

PORT = "/dev/ttyUSB0"
BAUD = 2400

def precision_scan_v3():
    print(f"=== OMRON BA100R Precision Scanner V3 ({PORT}) ===")
    
    try:
        ser = serial.Serial(
            port=PORT, baudrate=BAUD,
            parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
            bytesize=serial.EIGHTBITS, timeout=1.0
        )
        
        # DTR/RTS 상태 조합 (True=High, False=Low)
        # 많은 OMRON UPS는 특정 라인이 High여야 데이터 전송 모드로 들어갑니다.
        line_states = [
            (True, True),   # 둘 다 켬 (표준)
            (True, False),  # DTR만 켬
            (False, True),  # RTS만 켬
            (False, False)  # 둘 다 끔 (기본 접점 모드 유도)
        ]

        # 테스트할 명령어 세트
        commands = [b'Q1\r', b'F\r', b'I\r', b'S\r', b'R\r', b'V\r']

        for dtr_val, rts_val in line_states:
            print(f"\n[Mode Test] DTR={dtr_val}, RTS={rts_val}")
            ser.dtr = dtr_val
            ser.rts = rts_val
            time.sleep(1.0)  # 신호 안정화 대기
            
            for cmd in commands:
                ser.reset_input_buffer()
                print(f"  📤 Sending: {repr(cmd):<10}", end="")
                ser.write(cmd)
                time.sleep(0.5)
                
                if ser.in_waiting > 0:
                    raw = ser.read(ser.in_waiting)
                    decoded = raw.decode('ascii', errors='ignore').strip()
                    print(f" -> 📥 Received: {repr(raw)} ('{decoded}')")
                    
                    # 만약 숫자가 포함된 긴 응답(전압 데이터)이 오면 중단하고 알림
                    if len(decoded) > 10 and any(c.isdigit() for c in decoded):
                        print(f"\n⭐⭐⭐ SUCCESS! MATCH FOUND! ⭐⭐⭐")
                        print(f"Baud: {BAUD}, DTR: {dtr_val}, RTS: {rts_val}, CMD: {repr(cmd)}")
                        ser.close()
                        return
                else:
                    print(" -> ❌ No Response")
        
        ser.close()
        print("\n=== Scan Finished: No numeric data found ===")
        
    except Exception as e:
        print(f"Critical Error: {e}")

if __name__ == "__main__":
    precision_scan_v3()

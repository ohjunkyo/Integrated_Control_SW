import hid
# 기반의 스니퍼 코드
print("Scanning for connected Tamadenshi Lasers on Hub...")
for d in hid.enumerate(0x04d8, 0xfa73):
    print(f"Device: {d['product_string']}")
    print(f"  > Path : {d['path']}") # 이 바이트(bytes) 값이 핵심입니다!
    print("-" * 30)

void setup() {
  Serial.begin(9600);
  // 2, 3, 4, 5번 핀을 모두 입력 모드로 설정
  pinMode(2, INPUT);
  pinMode(3, INPUT);
  pinMode(4, INPUT);
  pinMode(5, INPUT);
  Serial.println("SYSTEM_LOG: Hardware Pin Scanner Started.");
}

void loop() {
  // 각 핀의 현재 물리적 상태를 읽어서 한 줄로 출력
  Serial.print("PIN_2:"); Serial.print(digitalRead(2));
  Serial.print(" | PIN_3:"); Serial.print(digitalRead(3));
  Serial.print(" | PIN_4:"); Serial.print(digitalRead(4));
  Serial.print(" | PIN_5:"); Serial.println(digitalRead(5));
  
  delay(500); // 0.5초 대기
}

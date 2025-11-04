# 실시간 환경 및 고전압(HV) 모니터링 시스템 v2.6 (Stable)

## 1\. 프로젝트 개요

본 프로젝트는 실험실 환경의 물리적 조건(온/습도)과 CAEN 고전압(HV) 장비의 상태를 실시간으로 통합 모니터링하고 원격으로 제어하기 위한 Python 기반 GUI 애플리케이션입니다.

v2.6 Stable 버전은 **멀티프로세싱(multiprocessing)** 아키텍처를 도입하여, CAEN C 라이브러리의 블로킹(blocking) 호출로 인해 GUI가 멈추는 현상을 원천적으로 해결한 가장 안정적인 버전입니다.

## 2\. 핵심 기능

  * **프리징 현상 완벽 해결**: CAEN 통신을 완전히 독립된 프로세스에서 처리하여, 연결 문제 발생 시에도 GUI가 절대 멈추지 않습니다.
  * **견고한 아키텍처**: GUI(메인 프로세스), Arduino 통신(스레드), CAEN 통신(자식 프로세스)이 명확히 분리되어 최고의 안정성을 확보했습니다.
  * **동적 하드웨어 지원**: `config.json` 설정 변경만으로 `SMARTHV`, `N1470` 등 파라미터 이름이 다른 다양한 CAEN 장비와 Arduino 보드를 완벽하게 지원합니다.
  * **상세 데이터 분석 및 추출**: 과거 데이터를 기간별/채널별로 선택하여 4분할 그래프로 조회하고, 원하는 데이터만 선택하여 CSV 파일로 저장하는 기능을 제공합니다.
  * **전문가용 진단 도구**: `hv_advanced_diagnostic.py`를 통해 장비의 모든 파라미터와 그 속성(읽기/쓰기 가능 여부)을 직접 확인할 수 있습니다.

## 3\. 시스템 아키텍처

본 시스템은 GUI의 안정성을 최우선으로 고려하여, 부하가 크거나 불안정할 수 있는 CAEN 통신 부분을 별도의 독립된 프로세스로 분리했습니다.

```
+---------------------------+      [Queue]      +-------------------------+
|     메인 프로세스 (GUI)     | <-------------> |   CAEN 워커 프로세스      |
| - monitoring_app.py       |   (명령/데이터)   | - workers/caen_process.py |
| - worker_manager.py (중계)  |                 | - (C 라이브러리 블로킹)   |
|                           |                 +-------------------------+
|   +---------------------+   |
|   |  Arduino 통신 스레드  |   |
|   | - workers/arduino.py|   |
|   +---------------------+   |
+---------------------------+
```

  * **메인 프로세스**: 사용자가 보는 모든 GUI와 Arduino 통신 스레드를 관리합니다. CAEN 프로세스와는 `Queue`를 통해 안전하게 통신하므로, CAEN 장비에 문제가 생겨도 절대 멈추지 않습니다.
  * **CAEN 워커 프로세스**: CAEN 장비와의 모든 통신을 전담합니다. 이 프로세스가 멈추거나 오류가 발생해도 메인 GUI에는 영향을 주지 않습니다.

## 4\. 설치 및 사용법

**1단계: 사용자 권한 설정 (리눅스)**

```bash
sudo usermod -a -G dialout $USER
```

> **중요**: 명령어 실행 후 반드시 **재부팅** 또는 **재로그인**해야 합니다.

**2단계: C/C++ HV Wrapper 및 Python 라이브러리 설치**

https://www.caen.it/products/caen-hv-wrapper-library/ 링크에서 리눅스 버전 받아서 설치
```bash
pip install -r requirements.txt
```

**3단계: 아두이노 설정**

1.  `arduino_sketch/multi_sensor_sketch.ino` 파일을 보드에 업로드합니다.
2.  `python3 utils/find_arduino_port.py`를 실행하여 포트 이름(예: `/dev/ttyACM0`)을 확인합니다.

**4단계: `config.json` 설정 (가장 중요)**

1.  먼저 `config.json` 파일을 열어 아두이노 포트, 장비의 IP 주소 등 기본 정보를 수정합니다.
2.  터미널에서 **고급 진단 스크립트**를 실행하여 사용하는 CAEN 장비의 정확한 파라미터 이름을 확인합니다.
    ```bash
    python3 utils/hv_advanced_diagnostic.py config.json
    ```
3.  진단 스크립트 결과에 나온 `VSet`, `ISet` 등의 이름을 `config.json`의 `parameters` 섹션에 정확하게 수정해줍니다. (예: N1470의 경우 `V0Set`이 아닌 `VSet`을 사용)

**5단계: 프로그램 실행**

```bash
python3 monitoring_app.py config.json
```

## 5\. 파일 구조

```
.
├── monitoring_app.py           # 메인 GUI 애플리케이션
├── worker_manager.py           # 스레드/프로세스 관리 및 중계
├── database_manager.py         # SQLite DB 관리
├── workers/
│   ├── __init__.py
│   ├── arduino.py              # Arduino 통신 스레드 워커
│   └── caen_process.py         # CAEN 통신 독립 프로세스 워커
├── util/
│   ├── find_arduino_port.py    # 아두이노 포트 자동 탐지 유틸리티
│   └── hv_advanced_diagnostic.py # CAEN 파라미터 고급 진단 유틸리티
├── config.json                 # 기본 설정 파일
├── requirements.txt
├── arduino_sketch/
│   └── multi_sensor_sketch.ino
└── README.md
```

# Integrated Control Software Suite (DAQ · HV · Laser · UPS)

A suite of Python/Tkinter GUI applications that provide a single, centralized
ecosystem for running a complete PMT (Photomultiplier Tube) test workflow —
from data acquisition and analysis to laser control, motorized PMT positioning,
high-voltage interlock and uninterruptible-power monitoring.

A top-level **Launcher** starts the individual control programs and shuts them
all down safely on exit. The **DAQ Control** application has grown into the hub
of the system: in addition to running the CAEN-digitizer DAQ it now embeds the
laser control, UPS monitoring, motorized PMT (rotation/tilt) automation, and an
in-app waveform/FFT viewer, so most day-to-day operation happens from one window.

---

## Core Components

* **`launcher.py`** — Main entry point. Launches and supervises the other
  applications and provides an integrated, confirmed shutdown that safely
  terminates every child process (DAQ, HV worker, etc.).
* **`DAQ_Control_SW/`** — The primary application. Controls the CAEN-digitizer
  DAQ, automates the DAQ → Produce → Analysis → Contour ROOT workflow, manages
  data files, and hosts the integrated **Laser**, **UPS**, **PMT-automation**
  and **Waveform** panels.
* **`HV_Control_SW/`** — Real-time monitor for CAEN high-voltage power supplies
  (live graphs + status).
* **`Laser_Control_SW/`** — Standalone Tamadenshi picosecond-laser driver GUI.
  Its `laser_driver.py` (HID/USB) is also imported directly by DAQ Control to
  drive the lasers from the integrated laser panel.

---

## Key Features

### Control & workflow (DAQ)
* **One-click ROOT workflow** — Run DAQ, Produce (`prod_ntp`), Analysis
  (`read_ntp`) and Contour as managed console jobs, with live output streamed
  into in-app consoles (carriage-return progress bars render correctly, and
  jobs can be re-run back-to-back).
* **Run-mode selection** — distinct `laser` and `dark` modes, each with its own
  trigger settings and data paths.
* **Run-number auto-completion** — scans existing data for the active mode and
  suggests the next run number.

### Motorized PMT automation ("General Scan")
* Schedules and runs automated tilt/rotation scans of the PMT stage, syncs the
  live hardware angles back into `config3.h`, and drives the DAQ at each point.

### Integrated Laser control (4 units)
* Drives four Tamadenshi lasers (375 / 405 / 450 / 473 nm) over HID/USB,
  distinguished by physical USB path (identical VID/PID).
* **Safety interlock watchdog** — a background thread polls each unit every
  second; on an interlock trip it forces LD/TEC **OFF** and alerts the operator.
* **USB-disconnect vs interlock** — the UI distinguishes a physical cable loss
  (red "USB DISCONNECTED") from a hardware interlock (orange "INTERLOCK") and
  reacts immediately, with optional auto-reconnect.
* Persistent per-unit telemetry logging (temperature, bias, pulse, LD state).

### UPS monitoring (OMRON BA100R)
* Auto-detects the UPS on a USB-serial port, shows live input/output voltage and
  status, locks the port after connect (password to change), and provides a
  password-gated, countdown-confirmed system-wide shutdown.

### Embedded Waveform / FFT viewer
* Reads RAW `.root` files directly with **uproot** — no ROOT GUI needed.
* Per-channel waveforms with pedestal line, charge (pC) and adjustable
  **pedestal window** (updates the charge live).
* **Single ↔ Average** toggle that works in both views:
  single/average **waveform** and single/average **FFT power spectrum**, with a
  selectable event range, fast batched reads, a cancel button and progress.
* Charge-threshold event search ("jump to charge &lt; X pC").

### Monitoring, safety & usability
* Real-time dashboard LEDs for DAQ / Laser / B-field / UPS connection status.
* **Access control** — a lock/unlock banner gates dangerous actions (Run DAQ,
  General Scan, laser ON) behind an unlock step.
* Integrated `.root` data-file browser with filtering and sorting.
* Detailed in-app log viewer; network (local / Tailscale) info display.

---

## Requirements

* **OS**: Linux (developed and tested on Ubuntu).
* **Python**: 3.10+ (developed on 3.12).
* **Python libraries**:
  * `tkinter` (GUI) — Ubuntu: `sudo apt install python3-tk`
  * `numpy`, `matplotlib`, `pandas` — plotting & analysis
  * `uproot` — reading `.root` files in the waveform viewer
  * `pyserial` — UPS serial communication
  * `hidapi` — laser USB (HID) communication
  * `psutil` — safe process management
  * `Pillow` — image handling
  * `tkcalendar` *(optional)* — date picker for scan scheduling
* **Frameworks / external**:
  * **ROOT** — for Produce/Analysis/Contour (the `root` command must be on `PATH`).
  * **CAEN Digitizer** libraries — for the `execute_DAQ_v2` program.
  * **CAEN HV** libraries — for the HV monitoring worker.
* **Terminal**: `gnome-terminal` (or `xterm`).

---

## Installation

```bash
git clone <repository-url>
cd Integrated_Control_SW

python3 -m venv venv
source venv/bin/activate

pip install numpy matplotlib pandas uproot pyserial hidapi psutil Pillow tkcalendar
```

---

## Configuration

Settings are managed per application:

* **DAQ / Laser / PMT / HV parameters**: the central `DAQ_Control_SW/config3.h`
  — file paths, DAQ options, per-device SN/HV, tilt/rotate angles, trigger
  channel and shift info. Editable from the GUI.
* **HV Monitor**: `HV_Control_SW/config_precal.json`.
* **Laser Control (standalone)**: no manual file — last-used Bias/Pulse/Trigger
  values are saved automatically on close.

---

## Usage

```bash
cd Integrated_Control_SW
python3 launcher.py
```

1. The **Launcher** window opens; start **DAQ Control**, **HV Monitor** and/or
   **Laser Control**.
2. In **DAQ Control**, **unlock controls** via the banner, choose the `Mode`
   (Laser/Dark), accept or edit the suggested `Run number`, then use the
   workflow buttons (Run DAQ → Produce → Analysis → Contour).
3. Use the **Waveform** tab to inspect RAW data, the **Laser**/**UPS** panels to
   monitor hardware, and **General Scan** for automated PMT positioning.
4. Closing the Launcher triggers a confirmation and safely shuts down every
   child application.

---

# 🇰🇷 한국어 버전

## 통합 제어 소프트웨어 제품군 (DAQ · HV · Laser · UPS)

PMT(광증배관) 테스트의 전체 작업 흐름 — 데이터 수집·분석부터 레이저 제어,
모터 구동 PMT 위치 조정, 고전압 인터락, 무정전 전원(UPS) 모니터링까지 —
하나의 중앙 생태계에서 운용하기 위한 Python/Tkinter GUI 제품군입니다.

최상위 **Launcher**가 개별 제어 프로그램들을 실행하고 종료 시 모두 안전하게
내립니다. **DAQ Control** 앱은 시스템의 허브로 발전하여, CAEN 디지타이저 DAQ
실행에 더해 **레이저 제어 · UPS 모니터링 · PMT(회전/틸트) 자동화 · 내장 파형/FFT
뷰어**를 통합했습니다. 따라서 일상적인 작업 대부분을 하나의 창에서 처리합니다.

---

## 핵심 구성 요소

* **`launcher.py`** — 메인 진입점. 다른 앱들을 실행·감독하며, 종료 시 모든 자식
  프로세스(DAQ, HV 워커 등)를 확인 후 안전하게 종료합니다.
* **`DAQ_Control_SW/`** — 메인 애플리케이션. CAEN 디지타이저 DAQ 제어,
  DAQ → Produce → Analysis → Contour ROOT 워크플로우 자동화, 데이터 관리,
  그리고 통합 **Laser · UPS · PMT 자동화 · Waveform** 패널을 포함합니다.
* **`HV_Control_SW/`** — CAEN 고전압 전원 실시간 모니터(라이브 그래프 + 상태).
* **`Laser_Control_SW/`** — Tamadenshi 피코초 레이저 드라이버 독립형 GUI.
  내부 `laser_driver.py`(HID/USB)는 DAQ Control이 직접 import 하여 통합 레이저
  패널에서 레이저를 구동하는 데에도 쓰입니다.

---

## 주요 기능

### 제어 및 워크플로우 (DAQ)
* **원클릭 ROOT 워크플로우** — Run DAQ, Produce(`prod_ntp`), Analysis
  (`read_ntp`), Contour를 관리형 콘솔 작업으로 실행하고, 출력을 앱 내 콘솔에
  실시간 스트리밍합니다(`\r` 진행 막대 정상 표시, 연속 재실행 가능).
* **실행 모드 선택** — `laser` / `dark` 모드(각각 별도 트리거·데이터 경로).
* **Run Number 자동 완성** — 현재 모드의 기존 데이터를 분석해 다음 번호 추천.

### 모터 구동 PMT 자동화 ("General Scan")
* PMT 스테이지의 틸트/회전 스캔을 예약·자동 실행하고, 라이브 각도를 `config3.h`에
  반영하며 각 지점에서 DAQ를 구동합니다.

### 통합 레이저 제어 (4대)
* 4대의 Tamadenshi 레이저(375 / 405 / 450 / 473 nm)를 HID/USB로 구동하며,
  동일 VID/PID를 물리적 USB 경로로 구분합니다.
* **안전 인터락 워치독** — 백그라운드 스레드가 매초 각 유닛을 폴링하여, 인터락
  발생 시 LD/TEC를 강제 **OFF** 하고 운영자에게 경고합니다.
* **USB 단선 vs 인터락 구분** — 물리적 케이블 분리(빨강 "USB DISCONNECTED")와
  하드웨어 인터락(주황 "INTERLOCK")을 구분해 즉시 반응하며, 자동 재연결 옵션 제공.
* 유닛별 텔레메트리(온도·바이어스·펄스·LD 상태) 영구 로깅.

### UPS 모니터링 (OMRON BA100R)
* USB-시리얼 포트에서 UPS를 자동 탐지하고 입·출력 전압과 상태를 실시간 표시하며,
  연결 후 포트를 잠그고(변경 시 비밀번호), 비밀번호+카운트다운 확인을 거치는
  시스템 전체 종료 기능을 제공합니다.

### 내장 파형 / FFT 뷰어
* RAW `.root` 파일을 **uproot**로 직접 읽습니다(ROOT GUI 불필요).
* 채널별 파형에 pedestal 선·전하(pC)를 표시하고, **pedestal 구간**을 조정하면
  전하가 실시간 재계산됩니다.
* 양쪽 뷰에서 동작하는 **Single ↔ Average** 토글: 단일/평균 **파형**과
  단일/평균 **FFT 파워 스펙트럼**. 이벤트 범위 선택, 빠른 배치 읽기, 취소 버튼,
  진행률 표시 지원.
* 전하 임계 이벤트 검색("charge &lt; X pC 로 점프").

### 모니터링 · 안전 · 편의성
* DAQ / Laser / B-field / UPS 연결 상태 실시간 대시보드 LED.
* **제어권 잠금** — 잠금/해제 배너가 위험 동작(Run DAQ, General Scan, 레이저 ON)을
  해제 단계 뒤로 게이트합니다.
* 필터·정렬이 되는 통합 `.root` 데이터 파일 브라우저.
* 앱 내 상세 로그 뷰어, 네트워크(로컬/Tailscale) 정보 표시.

---

## 요구사항

* **운영체제**: Linux (Ubuntu에서 개발·테스트).
* **Python**: 3.10 이상 (3.12에서 개발).
* **Python 라이브러리**:
  * `tkinter` (GUI) — Ubuntu: `sudo apt install python3-tk`
  * `numpy`, `matplotlib`, `pandas` — 그래프·분석
  * `uproot` — 파형 뷰어의 `.root` 읽기
  * `pyserial` — UPS 시리얼 통신
  * `hidapi` — 레이저 USB(HID) 통신
  * `psutil` — 안전한 프로세스 관리
  * `Pillow` — 이미지 처리
  * `tkcalendar` *(선택)* — 스캔 예약용 날짜 선택기
* **프레임워크 / 외부**:
  * **ROOT** — Produce/Analysis/Contour용 (`root` 명령이 `PATH`에 있어야 함).
  * **CAEN Digitizer** 라이브러리 — `execute_DAQ_v2` 구동.
  * **CAEN HV** 라이브러리 — HV 모니터링 워커 구동.
* **터미널**: `gnome-terminal` (또는 `xterm`).

---

## 설치

```bash
git clone <repository-url>
cd Integrated_Control_SW

python3 -m venv venv
source venv/bin/activate

pip install numpy matplotlib pandas uproot pyserial hidapi psutil Pillow tkcalendar
```

---

## 설정

애플리케이션별로 관리됩니다:

* **DAQ / Laser / PMT / HV 파라미터**: 중앙 `DAQ_Control_SW/config3.h` —
  파일 경로, DAQ 옵션, 장비별 SN/HV, 틸트/회전 각도, 트리거 채널, 시프트 정보.
  GUI에서 편집 가능.
* **HV Monitor**: `HV_Control_SW/config_precal.json`.
* **Laser Control (독립형)**: 별도 파일 없음 — 종료 시 마지막 Bias/Pulse/Trigger
  값을 자동 저장.

---

## 사용법

```bash
cd Integrated_Control_SW
python3 launcher.py
```

1. **Launcher** 창에서 **DAQ Control**, **HV Monitor**, **Laser Control**을 실행.
2. **DAQ Control**에서 배너로 **제어권을 해제**하고, `Mode`(Laser/Dark)를 선택,
   추천된 `Run number`를 확인/수정한 뒤 워크플로우 버튼
   (Run DAQ → Produce → Analysis → Contour)을 실행.
3. **Waveform** 탭으로 RAW 데이터 확인, **Laser**/**UPS** 패널로 하드웨어 모니터,
   **General Scan**으로 PMT 자동 위치 조정.
4. Launcher를 닫으면 확인 후 모든 자식 앱이 안전하게 종료됩니다.

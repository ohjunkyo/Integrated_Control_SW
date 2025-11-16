# Integrated Control Software Suite (DAQ, HV, Laser)

A suite of Python-based GUI applications designed to provide a centralized ecosystem for managing a complete PMT (Photomultiplier Tube) testing workflow.

This system includes a main **Launcher**, **DAQ Control** (for CAEN Digitizer), **HV Monitor** (for CAEN HV Power Supplies), and **Laser Control** (for Tamadenshi laser drivers).

## Core Components

* **`Launcher.py`**: The main entry point. Launches and manages all other applications. Provides integrated shutdown to safely terminate all child processes (including HV workers) when closed.
* **`DAQ_Control_SW`**: The primary application for controlling the CAEN Digitizer-based DAQ system. It manages experimental parameters, automates the DAQ-to-Analysis workflow (ROOT), and handles data management.
* **`HV_Control_SW`**: A dedicated real-time monitoring tool for CAEN High Voltage (HV) power supplies, providing live graphs and status monitoring.
* **`Laser_Contorl_SW`**: A standalone GUI for controlling a Tamadenshi picosecond laser driver, with real-time status monitoring and persistent setting memory.

## Key Features

* **Integrated Management**: The Launcher acts as a master control, allowing you to run all three systems as part of one unified program.
* **Centralized DAQ Configuration**: Manages all experimental parameters (paths, device settings, DAQ options) for the DAQ system from a single `config2.h` file.
* **Persistent Laser Settings**: The Laser Control GUI automatically saves and reloads its last-used values (Bias, Pulse, Trigger mode) for convenience.
* **Run Mode Selection**: (DAQ App) Supports distinct `dark` and `laser` run modes, each with its own trigger settings and data paths.
* **Automated Workflow**: (DAQ App) Sequentially execute DAQ, data production (`prod_ntp`), and final analysis (`read_ntp`) with single button clicks.
* **Real-time Monitoring**:
    * **DAQ:** Displays running script output in a log viewer and shows DAQ connection status.
    * **HV:** Provides live graphs and status updates from the HV power supply.
    * **Laser:** Shows live connection status, temperature, and current values.
* **Data Management**: (DAQ App) Integrated browser to view `.root` files, automatic run number detection, and dynamic filename generation.

## Requirements

* **OS**: Linux (developed and tested on Ubuntu)
* **Python**: Version 3.8 or higher
* **Python Libraries**:
    * `tkinter` (GUI). (Ubuntu: `sudo apt install python3-tk`)
    * `psutil` (For safe process management)
    * `hidapi` (For Laser Control USB communication)
    * `matplotlib`, `pandas` (For HV and Laser plotting)
* **Frameworks**:
    * ROOT Framework: For data analysis and processing.
    * CAEN Digitizer Libraries: Required for the `execute_DAQ` program.
    * CAEN HV Libraries: Required for the HV monitoring worker.
* **Terminal**: `gnome-terminal` or `xterm` (selectable from the DAQ app's File menu).

## Installation

1.  Clone the repository to your local machine:
    ```bash
    git clone [https://github.com/your-username/your-repository-name.git](https://github.com/your-username/your-repository-name.git)
    ```
2.  Navigate to the project's **root** directory:
    ```bash
    cd /path/to/Integrated_Control_SW
    ```
3.  (Recommended) Set up a Python virtual environment:
    ```bash
    python3 -m venv venv
    source venv/bin/activate
    ```
4.  Install the required Python libraries:
    ```bash
    pip install psutil hidapi matplotlib pandas
    ```

## Configuration

Settings are managed in separate files for each application:

* **DAQ Control**: All experimental parameters must be set in the `DAQ_Control_SW/config2.h` file. This includes file paths, DAQ settings, device settings (SN, HV), and trigger channels.
* **HV Monitor**: HV connection settings are managed in `HV_Control_SW/config_precal.json`.
* **Laser Control**: This app has no manual config file. It automatically saves its last-used settings to `~/.laser_control_config.json` upon closing.

## Usage

1.  Navigate to the **root** project directory:
    ```bash
    cd /path/to/Integrated_Control_SW
    ```
2.  Run the **main launcher**:
    ```bash
    python3 launcher.py
    ```
3.  **Main Workflow**:
    * The main **Launcher** window will open.
    * Use the buttons to start "DAQ Control", "HV Monitor", and "Laser Control". Each will open in its own window.
    * **To run a DAQ test**:
        * In the **DAQ Control** window, select the `Mode` (Laser or Dark).
        * The app will suggest the next `Run number`.
        * Use the "Execute Scripts" buttons (e.g., "Run DAQ") to perform the workflow.
    * When finished, **close the main Launcher window**. This will trigger a confirmation dialog and safely shut down all other running applications (DAQ, HV, and Laser).

---

# 🇰🇷 한국어 버전

## 통합 제어 소프트웨어 제품군 (DAQ, HV, Laser)

PMT(광증배관) 테스트의 전체 작업 흐름을 관리하기 위한 중앙 생태계를 제공하는 Python 기반 GUI 애플리케이션 '제품군'입니다.

이 시스템은 메인 **Launcher**, **DAQ Control**(CAEN Digitizer 제어용), **HV Monitor**(CAEN HV 전원 공급기 모니터링용), **Laser Control**(Tamadenshi 레이저 드라이버 제어용)을 포함합니다.

## 핵심 구성 요소

* **`Launcher.py`**: 메인 진입점. 다른 모든 애플리케이션을 실행하고 관리합니다. 종료 시 HV 워커와 같은 모든 자식 프로세스가 안전하게 종료되도록 통합된 종료 기능을 제공합니다.
* **`DAQ_Control_SW`**: CAEN Digitizer 기반 DAQ 시스템을 제어하기 위한 메인 애플리케이션입니다. 실험 변수 관리, DAQ-분석 워크플로우(ROOT) 자동화, 데이터 관리를 담당합니다.
* **`HV_Control_SW`**: CAEN 고전압(HV) 파워 서플라이를 위한 전용 실시간 모니터링 도구로, 라이브 그래프와 상태 모니터링을 제공합니다.
* **`Laser_Contorl_SW`**: Tamadenshi 피코초 레이저 드라이버를 제어하기 위한 독립형 GUI이며, 실시간 상태 모니터링과 설정값 기억 기능이 있습니다.

## 주요 기능

* **통합 관리**: 런처가 마스터 컨트롤 역할을 하여, 세 개의 모든 시스템을 하나의 통합된 프로그램처럼 실행하고 관리할 수 있습니다.
* **중앙 집중식 DAQ 설정**: DAQ 시스템의 모든 실험 변수(경로, 장비 설정, DAQ 옵션)를 단일 `config2.h` 파일에서 관리합니다.
* **레이저 설정 기억**: 레이저 제어 GUI는 편의를 위해 마지막으로 사용한 값(Bias, Pulse, 트리거 모드)을 자동으로 저장하고 다시 불러옵니다.
* **실행 모드 선택**: (DAQ 앱) 각각 별도의 트리거 설정과 데이터 경로를 가지는 `dark` 및 `laser` 실행 모드를 지원합니다.
* **작업 흐름 자동화**: (DAQ 앱) 버튼 클릭 한 번으로 DAQ, 데이터 생산(`prod_ntp`), 최종 분석(`read_ntp`)을 순차적으로 실행합니다.
* **실시간 모니터링**:
    * **DAQ:** 실행 중인 스크립트 출력을 로그 뷰어에 표시하고 DAQ 연결 상태를 보여줍니다.
    * **HV:** HV 파워 서플라이로부터 실시간 그래프와 상태 업데이트를 제공합니다.
    * **Laser:** 실시간 연결 상태, 온도, 전류 값을 표시합니다.
* **데이터 관리**: (DAQ 앱) `.root` 파일을 볼 수 있는 내장 브라우저, 런 번호 자동 감지, 동적 파일 이름 생성 기능을 제공합니다.

## 요구사항

* **운영체제**: Linux (Ubuntu에서 개발 및 테스트됨)
* **Python**: 버전 3.8 이상
* **Python 라이브러리**:
    * `tkinter` (GUI용). (Ubuntu 설치: `sudo apt install python3-tk`)
    * `psutil` (안전한 프로세스 관리를 위해 필요)
    * `hidapi` (Laser Control의 USB 통신을 위해 필요)
    * `matplotlib`, `pandas` (HV 및 Laser의 그래프 표시에 필요)
* **프레임워크**:
    * ROOT Framework: 데이터 분석 및 처리에 필요.
    * CAEN Digitizer 라이브러리: `execute_DAQ` 프로그램 구동에 필요.
    * CAEN HV 라이브러리: HV 모니터링 워커 구동에 필요.
* **터미널**: `gnome-terminal` 또는 `xterm` (DAQ 앱의 File 메뉴에서 선택 가능).

## 설치

1.  저장소를 로컬 컴퓨터로 복제(clone)합니다:
    ```bash
    git clone [https://github.com/your-username/your-repository-name.git](https://github.com/your-username/your-repository-name.git)
    ```
2.  프로젝트의 **루트(최상위)** 디렉터리로 이동합니다:
    ```bash
    cd /path/to/Integrated_Control_SW
    ```
3.  (권장) Python 가상 환경을 설정합니다:
    ```bash
    python3 -m venv venv
    source venv/bin/activate
    ```
4.  필요한 Python 라이브러리를 설치합니다:
    ```bash
    pip install psutil hidapi matplotlib pandas
    ```

## 설정

설정은 각 애플리케이션별로 별개의 파일에서 관리됩니다:

* **DAQ Control**: 모든 실험 변수는 `DAQ_Control_SW/config2.h` 파일에서 설정해야 합니다. 파일 경로, DAQ 설정, 장비 설정(SN, HV), 트리거 채널 등을 포함합니다.
* **HV Monitor**: HV 연결 설정은 `HV_Control_SW/config_precal.json`에서 관리합니다.
* **Laser Control**: 이 앱은 별도의 설정 파일이 없습니다. 종료 시 마지막으로 사용한 설정을 `~/.laser_control_config.json` 파일에 자동으로 저장합니다.

## 사용법

1.  프로젝트의 **루트(최상위)** 디렉터리로 이동합니다:
    ```bash
    cd /path/to/Integrated_Control_SW
    ```
2.  **메인 런처**를 실행합니다:
    ```bash
    python3 launcher.py
    ```
3.  **주요 작업 순서**:
    * 메인 **Launcher** 창이 열립니다.
    * 버튼을 사용해 "DAQ Control", "HV Monitor", "Laser Control"을 실행합니다. 각각의 앱이 별도의 창으로 열립니다.
    * **DAQ 테스트를 실행하려면**:
        * **DAQ Control** 창에서 `Mode`(Laser 또는 Dark)를 선택합니다.
        * 앱이 다음 `Run number`를 자동으로 추천해 줍니다.
        * "Execute Scripts" 아래의 버튼("Run DAQ" 등)을 사용하여 작업을 수행합니다.
    * 작업이 끝나면 **메인 Launcher 창을 닫습니다**. 종료 확인 대화 상자가 나타나며, 확인 시 다른 모든 실행 중인 앱(DAQ, HV, Laser)을 안전하게 함께 종료시킵니다.

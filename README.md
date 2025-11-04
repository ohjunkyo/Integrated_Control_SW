Integrated DAQ & Analysis Control Software

A Python-based GUI application designed to provide a centralized interface for controlling a CAEN Digitizer-based Data Acquisition (DAQ) system and managing the subsequent data processing and analysis workflow with ROOT.

This software was developed to streamline the PMT (Photomultiplier Tube) testing process by automating DAQ runs, analysis script execution, and data management.

Key Features

    Graphical User Interface: An intuitive interface built with Tkinter for easy operation.

    Centralized Configuration: Manages all experimental parameters (paths, device settings, DAQ options) from a single config2.h file.

    Run Mode Selection: Supports distinct dark and laser run modes, each with its own trigger settings and data paths.

    Automated Workflow: Sequentially execute DAQ, data production (prod_ntp), and final analysis (read_ntp) with single button clicks.

    Real-time Monitoring:

        Displays the output of running scripts in a dedicated log viewer.

        Shows system status, including DAQ connection, local IP, and total data directory size.

    Data Management:

        Integrated browser to view raw, processed, and final result .root files.

        Automatic run number detection to prevent data overwriting.

        Dynamic filename generation based on the active configuration.

Requirements

    OS: Linux (developed and tested on Ubuntu)

    Python: Version 3.8 or higher

    Libraries:

        tkinter for the GUI. (Install on Ubuntu: sudo apt install python3-tk)

    Frameworks:

        ROOT Framework: For data analysis and processing.

        CAEN Digitizer Libraries: Required for the execute_DAQ program to function.

    Terminal: gnome-terminal or xterm (selectable from the File menu).

Installation

    Clone the repository to your local machine:
    Bash

git clone https://github.com/your-username/your-repository-name.git

Navigate to the project directory:
Bash

cd /path/to/Integrated_Control_SW

(Recommended) Set up a Python virtual environment:
Bash

    python3 -m venv venv
    source venv/bin/activate

Configuration

All experimental parameters must be set in the config2.h file located in the DAQ_Control_SW directory.

Key sections to configure include:

    File Paths: BasePath, RawDataPath, ProcessedDataPath, etc.

    DAQ Settings: Events, TimeWindow, ChannelMask.

    Device Settings: SN1, direction1, HV1, etc., for up to 3 devices.

    Laser/Angle Settings: Laser, RotateAngle, TiltAngle.

    Trigger Channel: TriggerCh.

Usage

    Navigate to the DAQ_Control_SW directory:
    Bash

cd DAQ_Control_SW

Run the main application:
Bash

    python3 main.py

    Main Workflow:

        The application will load the settings from config2.h.

        Select the desired Mode (Laser or Dark).

        The application will automatically detect the last run number and suggest the next one.

        Use the buttons under "Execute Scripts" in numerical order (1. Configuration is for viewing, 2. Run DAQ starts the process).

        The output of the running script will be displayed in the "Log" tab.

        After a run is complete, you can browse the generated files in the "Data Files" tab.

🇰🇷 한국어 버전

통합 DAQ & 분석 컨트롤 소프트웨어

CAEN Digitizer 기반의 데이터 수집(DAQ) 시스템을 제어하고, ROOT를 이용한 데이터 처리 및 분석 작업 흐름을 관리하기 위한 중앙 집중형 인터페이스를 제공하는 Python 기반 GUI 프로그램입니다.

이 소프트웨어는 DAQ 실행, 분석 스크립트 구동, 데이터 관리를 자동화하여 PMT(광증배관) 테스트 과정을 효율적으로 만들기 위해 개발되었습니다.

주요 기능

    그래픽 사용자 인터페이스: Tkinter로 제작된 직관적인 인터페이스로 쉬운 조작 가능.

    중앙 집중식 설정: 단일 config2.h 파일을 통해 모든 실험 변수(경로, 장비 설정, DAQ 옵션)를 관리.

    실행 모드 선택: 각각 별도의 트리거 설정과 데이터 경로를 가지는 dark 및 laser 실행 모드 지원.

    작업 흐름 자동화: 버튼 클릭 한 번으로 DAQ, 데이터 생산(prod_ntp), 최종 분석(read_ntp)을 순차적으로 실행.

    실시간 모니터링:

        실행 중인 스크립트의 출력을 전용 로그 뷰어에 표시.

        DAQ 연결 상태, 로컬 IP, 전체 데이터 디렉터리 용량 등 시스템 상태 표시.

    데이터 관리:

        Raw, 가공, 최종 결과 .root 파일을 볼 수 있는 내장 브라우저.

        데이터 덮어쓰기 방지를 위한 런 번호 자동 감지 기능.

        현재 설정에 기반한 동적 파일 이름 생성.

요구사항

    운영체제: Linux (Ubuntu에서 개발 및 테스트됨)

    Python: 버전 3.8 이상

    라이브러리:

        tkinter (GUI용). (Ubuntu 설치: sudo apt install python3-tk)

    프레임워크:

        ROOT Framework: 데이터 분석 및 처리에 필요.

        CAEN Digitizer 라이브러리: execute_DAQ 프로그램 구동에 필요.

    터미널: gnome-terminal 또는 xterm (File 메뉴에서 선택 가능).

설치

    저장소를 로컬 컴퓨터로 복제(clone)합니다:
    Bash

git clone https://github.com/your-username/your-repository-name.git

프로젝트 디렉터리로 이동합니다:
Bash

cd /path/to/Integrated_Control_SW

(권장) Python 가상 환경을 설정합니다:
Bash

    python3 -m venv venv
    source venv/bin/activate

설정

모든 실험 변수는 DAQ_Control_SW 디렉터리 안에 있는 config2.h 파일에서 설정해야 합니다.

주요 설정 항목:

    파일 경로: BasePath, RawDataPath, ProcessedDataPath 등.

    DAQ 설정: Events, TimeWindow, ChannelMask.

    장비 설정: 최대 3개의 장비에 대한 SN1, direction1, HV1 등.

    레이저/각도 설정: Laser, RotateAngle, TiltAngle.

    트리거 채널: TriggerCh.

사용법

    DAQ_Control_SW 디렉터리로 이동합니다:
    Bash

cd DAQ_Control_SW

메인 프로그램을 실행합니다:
Bash

python3 main.py

주요 작업 순서:

    프로그램이 config2.h 파일에서 설정을 불러옵니다.

    원하는 Mode(Laser 또는 Dark)를 선택합니다.

    프로그램이 마지막 런 번호를 자동으로 감지하고 다음 번호를 추천해 줍니다.

    "Execute Scripts" 아래의 버튼을 순서대로 사용합니다 (1. Configuration은 보기용, 2. Run DAQ부터 실제 작업 시작).

    실행 중인 스크립트의 출력은 "Log" 탭에 표시됩니다.

    실행이 완료되면 "Data Files" 탭에서 생성된 파일을 확인할 수 있습니다.

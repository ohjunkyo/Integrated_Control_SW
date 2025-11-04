# DAQ Control Software 

**DAQ Control Software** is a graphical user interface (GUI) controller for ROOT-based data acquisition (DAQ) systems. 
It was developed to simplify the execution process of complex C++-based scripts and to increase the efficiency of research and experiments by integrating data management and visualization. 
(DAQ Control Software는 ROOT 기반의 데이터 수집(DAQ) 시스템을 위한 그래픽 사용자 인터페이스(GUI) 컨트롤러입니다. C++ 기반의 복잡한 스크립트 실행 과정을 단순화하고, 데이터 관리 및 시각화를 통합하여 연구 및 실험의 효율성을 높이기 위해 개발되었습니다.)


## Features (주요 기능)

This software provides a wide range of features.

#### **Control & Execution (제어 및 실행)**
	* **GUI-Based Script Execution**: Execute complex C++ and shell scripts like `Run DAQ`, `Produce`, and `Analysis` with a single button click. (GUI 기반 스크립트 실행: `Run DAQ`, `Produce`, `Analysis` 등 복잡한 C++ 및 셸 스크립트를 버튼 클릭만으로 실행합니다.)
	* **Mode Selection**: Easily select `Laser` or `Dark` modes for DAQ execution via radio buttons. (실행 모드 선택: `Laser`와 `Dark` 모드를 라디오 버튼으로 쉽게 선택하여 DAQ를 실행할 수 있습니다.)
	* **Separate Terminal Execution**: All scripts run in separate terminal windows, maintaining GUI responsiveness while providing detailed, real-time logs. (독립적인 터미널 실행: 모든 스크립트는 별도의 터미널 창에서 실행되어, GUI의 반응성을 유지하면서 상세한 로그를 실시간으로 확인할 수 있습니다.)
	* **Run Number Auto-completion**: Automatically suggests the next Run Number by analyzing existing data files for the currently selected mode (Laser/Dark). (Run Number 자동 완성: 현재 선택된 모드(Laser/Dark)의 기존 데이터 파일들을 분석하여, 다음 실행할 Run Number를 자동으로 추천해 줍니다.)

#### **Monitoring & Viewers (모니터링 및 뷰어)**
	* **Real-time DAQ Connection Status**: Periodically checks the connection status with the DAQ hardware and displays it as `Connected` or `Disconnected` in the GUI. (실시간 DAQ 연결 상태: 주기적으로 DAQ 하드웨어와의 연결 상태를 확인하여 GUI에 `Connected` 또는 `Disconnected`로 표시합니다.)
	* **PMT Status Indicators**: Visually displays the activation status of PMTs based on the `config2.h` file settings. (PMT 상태 표시: `config2.h` 파일에 설정된 PMT의 활성화 여부를 시각적인 인디케이터로 보여줍니다.)
	* **Network Information Display**: Shows local IP and Tailscale IP addresses in the GUI to support remote access and monitoring. (네트워크 정보 표시: 로컬 IP 및 Tailscale IP 주소를 GUI에 표시하여 원격 접속 및 모니터링을 지원합니다.)
	* **Configuration File Viewer**: Allows real-time viewing of the currently applied `config2.h` file's content through a tab in the main window. (설정 파일 뷰어: 메인 화면의 탭을 통해 현재 적용된 `config2.h` 파일의 내용을 실시간으로 확인할 수 있습니다.)
	* **Detailed Log Viewer**: Check and manage all script execution histories in the 'Log' tab of the main window. (상세 로그 뷰어: 메인 화면의 'Log' 탭을 통해 프로그램의 모든 스크립트 실행 기록을 확인하고 관리할 수 있습니다.)

#### **Data Management (데이터 관리)**
	* **Integrated File Browser**: View `Raw Data` and `Production Data` separately in the 'Data Files' tab. (통합 파일 브라우저: 'Data Files' 탭에서 `Raw Data`와 `Production Data`를 분리하여 볼 수 있습니다.)
	* **Filtering**: Filter the file list by `All`, `Dark`, or `Laser` modes. (필터링: `All`, `Dark`, `Laser` 모드별로 파일 목록을 필터링할 수 있습니다.)
	* **Sorting**: Sort the list by file name or modification time (newest first). (정렬: 파일 이름 또는 수정 시간(최신순)을 기준으로 목록을 정렬할 수 있습니다.)
	* **File Details Panel**: Displays detailed information such as file name, size, and last modified date when a data file is clicked. (파일 상세 정보: 데이터 파일을 클릭하면 파일 이름, 크기, 최종 수정일 등의 상세 정보가 표시됩니다.)
	* **Open ROOT Files Directly**: Immediately open `.root` files in ROOT's `TBrowser` by double-clicking them in the data list. (ROOT 파일 바로 열기: 데이터 목록에서 `.root` 파일을 더블클릭하면 ROOT의 `TBrowser`로 즉시 열 수 있습니다.)

#### **Advanced Image Viewer (고급 이미지 뷰어)**
	* **Unified File Viewer**: Browse both image files (.png, .jpg) and data files (.root) together. (이미지 및 데이터 파일 통합 뷰어: 이미지 파일(.png, .jpg)과 데이터 파일(.root)을 함께 탐색할 수 있습니다.)
	* **Folder Filtering**: Filter the list based on the source where the images were generated, such as `Produce` or `Analysis`. (폴더 필터링: `Produce`, `Analysis` 등 이미지가 생성된 소스에 따라 목록을 필터링할 수 있습니다.)
	* **Multi-select and Delete**: Select multiple images using Ctrl or Shift keys and safely delete them via a confirmation dialog. (다중 선택 및 삭제: Ctrl, Shift 키를 이용해 여러 이미지를 선택하고, 삭제 확인창을 거쳐 안전하게 삭제할 수 있습니다.)
	* **Advanced Viewer Features (고급 뷰어 기능)**:
	* **Zoom**: Adjust image size with zoom in/out buttons and the mouse wheel. (줌(Zoom): 확대/축소 버튼 및 마우스 휠로 이미지 크기를 조절할 수 있습니다.)
																						   * **Pan**: Explore specific parts of an image in detail by dragging with the mouse and using horizontal/vertical scrollbars (includes pan sensitivity control). (패닝(Pan): 마우스 드래그 및 가로/세로 스크롤바로 이미지의 원하는 부분을 세밀하게 탐색할 수 있습니다. (패닝 민감도 조절 기능 포함))
																																																																	   * **Fit to Screen**: Optimizes the image size to fit the current viewer dimensions. (화면 맞춤: 이미지를 현재 뷰어 크기에 최적화하여 보여줍니다.)

#### **Usability (사용자 편의성)**
* **Change Config File Path**: Specify and save the location of the `config2.h` file directly from the 'File' menu in the GUI. (설정 파일 경로 변경: GUI의 'File' 메뉴를 통해 `config2.h` 파일의 위치를 직접 지정하고 저장할 수 있습니다.)
* **Path Shortcuts**: Displays key directory paths and allows opening a terminal at that location with a button click. (경로 바로가기: 주요 디렉토리 경로를 표시하고, 버튼 클릭으로 해당 경로에서 바로 터미널을 열 수 있습니다.)
* **Refresh Function**: Reload all information (configuration, Run Number, file lists) from the disk using the Refresh button on the main window. (새로고침 기능: 메인 화면의 Refresh 버튼으로 모든 정보(설정, Run Number, 파일 목록)를 다시 불러올 수 있습니다.)


## ⚙️ Prerequisites (시스템 요구사항)

To run this software from the source code, the following environment is required. (이 소프트웨어를 소스 코드로 실행하기 위해서는 다음 환경이 필요합니다.)

	* **Operating System**: Linux (Tested on Ubuntu-based distributions). (운영체제: Linux (Ubuntu 기반 배포판에서 테스트됨))
	* **Python**: Python 3.10 or newer. (Python: Python 3.10 이상)
	* **Core Framework**: [ROOT Data Analysis Framework](https://root.cern/) must be installed, and the `root` command must be registered in the system `PATH`. 
	(핵심 프레임워크: [ROOT Data Analysis Framework](https://root.cern/)가 설치되어 있고, `root` 명령어가 시스템 `PATH`에 등록되어 있어야 합니다.)
	* **Required Programs**: `gnome-terminal`. (필수 프로그램: `gnome-terminal`)
	* **DAQ Environment**: The existing DAQ system files must all be present at the `BasePath` specified in the `config2.h` file. (DAQ 환경: `config2.h` 파일에 명시된 `BasePath` 경로에 다음과 같은 기존 DAQ 시스템 파일들이 모두 존재해야 합니다.)
	* `execute_DAQ` executable binary (execute\_DAQ 실행 바이너리)
	* C++ analysis scripts (`prod_ntp_v2.C`, `read_ntp_v2.C`, etc.). (C++ 분석 스크립트)
* Data storage directories (`Data/RAW`, `Data/production`, etc.). (데이터 저장 디렉토리)

	---

### 1. Running from Source (소스 코드로 실행하기)

Follow these steps in your terminal. (터미널에서 다음 단계를 따르세요.)

	1.  **Clone the project (프로젝트 복제)**
	```bash
	git clone [repository URL]
	cd DAQ_Control_SW
	```

	2.  **Run the execution script (실행 스크립트 실행)**
	The `start.sh` script automatically handles all necessary preparations. (start.sh 스크립트는 필요한 모든 준비 과정을 자동으로 처리합니다.)
	* Creates a Python virtual environment (`venv`). (Python 가상환경(`venv`) 생성)
	* Installs required libraries (`Pillow`). (필요한 라이브러리(`Pillow`) 설치)
* Runs the main application. (메인 애플리케이션 실행)

	```bash
	./start.sh
	```

### 2. Building the Standalone Executable (독립 실행 파일 빌드하기)

You can create a single executable file to distribute to other users. (코드를 수정하고 다른 사용자에게 배포하기 위한 단일 실행 파일을 만들 수 있습니다.)

	1.  **Run the build script (빌드 스크립트 실행)**
	The `build.sh` script automates the entire build process. (build.sh 스크립트는 빌드에 필요한 모든 과정을 자동화합니다.)
	* Cleans up previous build files (`build`, `dist` folders). (이전 빌드 파일 정리)
	* Verifies the virtual environment and installs `pyinstaller`. (가상환경 확인 및 `pyinstaller` 설치)
* Builds the executable. (실행 파일 빌드)

	```bash
	./build.sh
	```

	2.  **Check the executable file (실행 파일 확인)**
Once the build is complete, the final executable file named `DAQ_Control` will be created in the `dist/` folder. You can run the program with just this single file. (빌드가 완료되면 `dist/` 폴더 안에 `DAQ_Control`이라는 최종 실행 파일이 생성됩니다. 이 파일 하나만으로 프로그램을 실행할 수 있습니다.)

	```bash
	./dist/DAQ_Control
	```

	---

## 🔧 Configuration (설정)

All operations of this program are centered around the `config2.h` file. (이 프로그램의 모든 동작은 `config2.h` 파일을 중심으로 이루어집니다.)

	* **Central Configuration File**: All DAQ system paths, sequence settings, device information, etc., are managed in the `config2.h` file. (중앙 설정 파일: DAQ 시스템의 모든 경로, 시퀀스 설정, 장비 정보 등은 `config2.h` 파일에서 관리됩니다.)
	* **Modification via GUI (GUI를 통한 수정)**:
	* **Change Path**: You can change the location of the `config2.h` file that the program references through the `File -> Set Config Path...` menu. This path is saved in the `.daq_control_config.json` file in your home directory. (경로 변경: `File -> Set Config Path...` 메뉴를 통해 프로그램이 참조할 `config2.h` 파일의 위치를 변경할 수 있습니다. 이 경로는 사용자 홈 디렉토리의 `.daq_control_config.json` 파일에 저장됩니다.)
	* **Edit Content**: You can directly edit and save the content of the `config2.h` file by clicking the `Configuration` button or the PMT status indicators. (내용 편집: `Configuration` 버튼이나 PMT 상태 인디케이터를 클릭하여 `config2.h` 파일의 내용을 직접 수정하고 저장할 수 있습니다.)

	---

## 📝 File Structure (파일 구조)

The roles of the main files are as follows. (주요 파일의 역할은 다음과 같습니다.)

	* `main.py`: Main application logic, event handling, script execution, etc. (Controller role). (메인 애플리케이션 로직, 이벤트 처리, 스크립트 실행 등 (Controller 역할))
	* `ui_manager.py`: Creates and manages all UI elements of the main window (View role). (메인 윈도우의 모든 UI 요소 생성 및 관리 (View 역할))
	* `config_manager.py`: Handles the logic for reading and writing the `config2.h` file. (`config2.h` 파일을 읽고 쓰는 로직 담당)
	* `image_viewer.py`: UI and logic for the advanced image viewer window. (고급 이미지 뷰어 창의 UI 및 로직)
	* `config_window.py`: UI and logic for the main configuration window. (전체 설정 창의 UI 및 로직)
	* `pmt_config_window.py`: UI and logic for the individual PMT configuration window. (PMT 개별 설정 창의 UI 및 로직)
	* `start.sh`: Helper script for running the application from source code. (소스 코드 실행을 위한 헬퍼 스크립트)
	* `build.sh`: Automation script for building the PyInstaller executable. (PyInstaller 실행 파일 빌드를 위한 자동화 스크립트)
	* `buttons.json`: Defines the configuration for buttons in the `Execute Scripts` and `View` sections. (`Execute Scripts`와 `View` 버튼의 구성을 정의하는 파일)
	* `*.sh`: Shell scripts used by functions like `Run DAQ`, `Produce`, etc. (`Run DAQ`, `Produce` 등에서 사용하는 셸 스크립트)

Linux에서 Python 레이저 제어 GUI 실행 방법

이 문서는 laser_gui.py를 Ubuntu/Linux 환경에서 실행하기 위한 1회성 설정 방법을 설명합니다.

1단계: Python 3 및 pip 설치

Ubuntu 20.04 이상에는 기본적으로 설치되어 있습니다.

sudo apt update
sudo apt install python3 python3-pip python3-tk


(python3-tk는 tkinter GUI를 위해 필요합니다.)

2단계: Python 라이브러리 설치

hidapi (USB 통신), pandas (데이터 처리), matplotlib (그래프)가 필요합니다.

pip3 install hidapi pandas matplotlib


3단계: USB 접근 권한 설정 (Udev 규칙) - (필수!)

Linux는 보안을 위해 일반 사용자가 USB 장치에 직접 접근하는 것을 차단합니다. sudo 없이 프로그램을 실행하려면, 이 레이저 장치에 대한 접근 권한을 허용하는 규칙을 추가해야 합니다.

터미널에서 udev 규칙 파일을 생성합니다.

sudo nano /etc/udev/rules.d/99-tamadenshi.rules


열린 편집기에 다음 내용을 정확히 복사하여 붙여넣습니다.
(이것은 tmHIDLD.dll에서 찾은 VID/PID 값입니다.)

SUBSYSTEM=="hidraw", ATTRS{idVendor}=="04d8", ATTRS{idProduct}=="fa73", MODE="0666"


Ctrl+O (저장) 후 Ctrl+X (종료) 합니다.

터미널에서 다음 명령을 실행하여 시스템에 규칙을 즉시 적용합니다.

sudo udevadm control --reload-rules
sudo udevadm trigger


가장 중요: 레이저 하드웨어의 USB 케이블을 뽑았다가 다시 꽂습니다.

이제 sudo 없이도 launcher.py를 통해 레이저 제어 프로그램을 실행할 수 있습니다.

4단계: 로그 디렉토리 확인

이 프로그램은 텍스트 로그(laser_log_...txt)와 데이터 로그(laser_data_...csv)를 ~/ADC/ADC_test/LOG/LASER/ 디렉토리에 저장하도록 설정되어 있습니다. 이 디렉토리가 존재하지 않으면 자동으로 생성합니다.

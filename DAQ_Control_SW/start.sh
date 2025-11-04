#!/bin/bash

# 이 스크립트 파일이 위치한 디렉토리의 절대 경로를 찾음
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)
VENV_DIR="$SCRIPT_DIR/venv"

# 1. 가상 환경 폴더가 없으면 생성
if [ ! -d "$VENV_DIR" ]; then
	echo "Creating virtual environment in $SCRIPT_DIR..."
	python3 -m venv "$VENV_DIR"
fi

# 2. 가상 환경 활성화
# echo "Activating virtual environment..."
source "$VENV_DIR/bin/activate"

# 3. Pillow 라이브러리가 설치되어 있는지 확인하고, 없으면 설치
if ! pip list | grep -F Pillow > /dev/null; then
	echo "Pillow library not found. Installing..."
	pip install Pillow
fi

# 4. 메인 파이썬 스크립트 실행
echo "Starting DAQ Control application..."
python3 "$SCRIPT_DIR/main.py"

# 5. 프로그램 종료 후 가상 환경 비활성화
deactivate
# echo "Application closed."


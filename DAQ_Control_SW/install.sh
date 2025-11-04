#!/bin/bash

# 스크립트가 위치한 디렉토리를 기준으로 경로를 설정합니다.
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)
VENV_DIR="$SCRIPT_DIR/venv"

# --- 스크립트 시작 ---
echo "Starting the build process for DAQ_Control..."

# 1. 가상 환경 폴더가 없으면 새로 생성합니다.
if [ ! -d "$VENV_DIR" ]; then
	echo "Virtual environment not found. Creating one..."
	python3 -m venv "$VENV_DIR"
fi

# 2. 가상 환경을 활성화합니다.
echo "Step 0: Activating virtual environment..."
source "$VENV_DIR/bin/activate"

# 3. PyInstaller가 설치되어 있는지 확인하고, 없으면 설치합니다.
if ! pip list | grep -F pyinstaller > /dev/null; then
	echo "PyInstaller not found. Installing..."
	pip install pyinstaller
fi
# Pillow도 확인합니다.
if ! pip list | grep -F Pillow > /dev/null; then
	echo "Pillow not found. Installing..."
	pip install Pillow
fi


# 4. 이전 빌드 파일 삭제
echo "Step 1: Cleaning old build and dist directories..."
rm -rf build/
rm -rf dist/

# 5. 셸 스크립트 파일에 실행 권한 부여
echo "Step 2: Setting execute permissions for scripts..."
chmod +x run_cpp_script.sh
chmod +x script2.sh

# 6. PyInstaller 실행
echo "Step 3: Running PyInstaller to create the executable..."
pyinstaller --onefile --windowed \
    --add-data 'buttons.json:.' \
    --add-data 'run_cpp_script.sh:.' \
    --add-data 'script2.sh:.' \
    --hidden-import 'PIL._tkinter_finder' \
    -n DAQ_Control \
    main.py

# 7. 가상 환경 비활성화
deactivate

# --- 스크립트 종료 ---
echo ""
echo "Build finished successfully! 🚀"
echo "The final executable is located in the 'dist' directory."

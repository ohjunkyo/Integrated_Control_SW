#!/bin/bash

# 이 스크립트가 있는 디렉토리의 절대 경로
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)
VENV_DIR="$SCRIPT_DIR/venv"

# 1. 가상 환경 폴더가 없으면 생성
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
fi

# 2. 가상 환경 활성화
source "$VENV_DIR/bin/activate"

# 3. requirements.txt 파일로 패키지 설치
echo "Installing packages from requirements.txt..."
pip install -r "$SCRIPT_DIR/requirements.txt"

# 4. 완료 메시지 및 비활성화 안내
echo ""
echo "Setup complete! ✅"
echo "To activate the virtual environment, run:"
echo "source venv/bin/activate"

# 가상 환경을 자동으로 비활성화하지 않고, 사용자가 수동으로 하도록 안내
# deactivate

#!/bin/bash

echo "Monitoring App 관련 프로세스를 검색하고 종료합니다..."

# 1. 메인 앱 (launcher.py) 프로세스 검색
# [l]auncher.py는 grep 명령 자신을 제외하기 위한 트릭입니다.
APP_PIDS=$(ps aux | grep '[l]auncher.py' | awk '{print $2}')

if [ -z "$APP_PIDS" ]; then
  echo "실행 중인 메인 앱(launcher.py)이 없습니다."
else
  echo "메인 앱 PID: $APP_PIDS ... 강제 종료 (kill -9)"
  kill -9 $APP_PIDS
fi

# 2. CAEN 워커 프로세스 검색 (간혹 메인 앱이 죽어도 홀로 남을 수 있음)
WORKER_PIDS=$(ps aux | grep '[c]aen_process' | awk '{print $2}')

if [ -z "$WORKER_PIDS" ]; then
  echo "남아있는 워커 프로세스가 없습니다."
else
  echo "남아있는 워커 PID: $WORKER_PIDS ... 강제 종료 (kill -9)"
  kill -9 $WORKER_PIDS
fi

echo "프로세스 정리 완료."

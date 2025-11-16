#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Stray Process Killer
-
This script finds and terminates Python processes related to the
DAQ, HV, and Laser control applications that might still be running
in the background (zombie or stray processes).

Requires 'psutil':
  pip3 install psutil
"""

import psutil
import os
import sys

# --- 찾고자 하는 스크립트 이름 ---
# 이 목록에 포함된 이름이 커맨드 라인에 있으면 종료 대상으로 간주합니다.
TARGET_SCRIPTS = [
    "main.py",            # DAQ Control
    "monitoring_app.py",  # HV Monitor
    "laser_gui.py",       # Laser Control
]

def find_stray_processes():
    """
    현재 실행 중인 모든 프로세스를 스캔하여
    TARGET_SCRIPTS 목록에 있는 Python 프로세스를 찾습니다.
    """
    stray_processes = []
    
    # 현재 스크립트의 PID (종료 대상에서 제외하기 위함)
    my_pid = os.getpid()

    # 모든 실행 중인 프로세스 반복
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            # 프로세스 정보 가져오기
            pid = proc.info['pid']
            cmdline = proc.info['cmdline']
            
            # 본인 자신은 건너뛰기
            if pid == my_pid:
                continue

            # Python으로 실행된 스크립트인지 확인
            if cmdline and 'python' in cmdline[0].lower():
                # 커맨드 라인 전체를 문자열로 합쳐서 검색
                cmd_str = " ".join(cmdline)
                
                for target in TARGET_SCRIPTS:
                    if target in cmd_str:
                        stray_processes.append(proc)
                        # 중복 추가 방지
                        break
                        
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            # 이미 종료되었거나 권한 없는 프로세스는 무시
            pass
            
    return stray_processes

def kill_processes(processes):
    """
    주어진 프로세스 목록을 종료(terminate)합니다.
    """
    if not processes:
        print("No stray processes found to terminate.")
        return

    print("\nTerminating the following processes:")
    for proc in processes:
        try:
            pid = proc.pid
            proc.terminate() # SIGTERM (정상 종료 시도)
            print(f"  - Terminated PID {pid} ({' '.join(proc.cmdline())})")
        except psutil.NoSuchProcess:
            print(f"  - PID {pid} was already gone.")
        except Exception as e:
            print(f"  - Failed to terminate PID {pid}: {e}")

def main():
    print("--- Stray Application Process Killer ---")
    
    try:
        processes_to_kill = find_stray_processes()
        
        if not processes_to_kill:
            print("✅ No stray 'main.py', 'monitoring_app.py', or 'laser_gui.py' processes found.")
            return

        print("\nFound the following stray processes:")
        for proc in processes_to_kill:
            try:
                print(f"  - PID: {proc.pid:<7} | CMD: {' '.join(proc.cmdline())}")
            except Exception:
                pass # 이미 사라진 프로세스 무시

        print("\n" + "="*30)
        answer = input("Do you want to terminate all these processes? (y/n): ").strip().lower()
        
        if answer == 'y':
            kill_processes(processes_to_kill)
        else:
            print("Termination cancelled by user.")

    except ImportError:
        print("\n[Error] 'psutil' library not found.")
        print("Please install it first by running:")
        print("  pip3 install psutil")
    except Exception as e:
        print(f"\nAn error occurred: {e}")

if __name__ == "__main__":
    main()

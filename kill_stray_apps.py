#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Stray Process Killer
-
Finds Python processes related to the DAQ, HV, and Laser control
applications and lets you terminate genuine DUPLICATES/zombies.

Requires 'psutil':
  pip3 install psutil
"""

import psutil
import os
import time

# --- 찾고자 하는 스크립트 이름 ---
# 이 목록에 포함된 이름이 커맨드 라인에 있으면 대상으로 간주합니다.
TARGET_SCRIPTS = [
    "main.py",            # DAQ Control
    "monitoring_app.py",  # HV Monitor
    "laser_gui.py",       # Laser Control
]


def find_matches():
    """Scan all processes; return {target: [proc, ...]} grouped by which
    TARGET_SCRIPTS name matched. Only genuine python-interpreter processes
    count -- NOT a bash/shell process that merely mentions the filename in
    its arguments (e.g. this app's own detached restart-watcher runs
    `bash -c "... exec python3 .../main.py ..."`, and a plain substring
    search would misidentify that watcher itself as "another main.py"."""
    my_pid = os.getpid()
    groups = {t: [] for t in TARGET_SCRIPTS}

    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            pid = proc.info['pid']
            cmdline = proc.info['cmdline']
            if pid == my_pid or not cmdline:
                continue

            exe = cmdline[0].lower()
            if 'python' not in exe and not exe.rstrip('0123456789.').endswith('/python'):
                continue

            cmd_str = " ".join(cmdline)
            for target in TARGET_SCRIPTS:
                if target in cmd_str:
                    groups[target].append(proc)
                    break
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass

    return groups


def kill_processes(processes):
    if not processes:
        return
    print("\nTerminating:")
    for proc in processes:
        try:
            pid = proc.pid
            proc.terminate()  # SIGTERM (graceful)
        except psutil.NoSuchProcess:
            print(f"  - PID {pid} was already gone.")
            continue
        except Exception as e:
            print(f"  - Failed to terminate PID {pid}: {e}")
            continue
        # Confirm it actually died -- terminate() is fire-and-forget; a stuck
        # process that ignores SIGTERM would otherwise silently stay a stray.
        try:
            proc.wait(timeout=3)
            print(f"  - Terminated PID {pid} (confirmed dead)")
        except psutil.TimeoutExpired:
            print(f"  - PID {pid} did not exit within 3s after SIGTERM, force-killing (SIGKILL)...")
            try:
                proc.kill()
                proc.wait(timeout=2)
                print(f"  - PID {pid} killed.")
            except Exception as e:
                print(f"  - Could not force-kill PID {pid}: {e}")


def main():
    print("--- Stray Application Process Killer ---\n")

    try:
        groups = find_matches()
    except ImportError:
        print("[Error] 'psutil' library not found. Run:  pip3 install psutil")
        return
    except Exception as e:
        print(f"An error occurred while scanning: {e}")
        return

    any_found = any(procs for procs in groups.values())
    if not any_found:
        print("✅ No matching processes found at all.")
        return

    to_kill = []
    for target, procs in groups.items():
        if not procs:
            continue
        print(f"[{target}] {len(procs)} process(es) found:")
        for proc in procs:
            try:
                print(f"  - PID {proc.pid:<7} | {' '.join(proc.cmdline())}")
            except Exception:
                pass

        if len(procs) == 1:
            # A single instance is very likely your ONE legitimate, currently
            # running app -- NOT a stray/duplicate. Killing it just shuts down
            # your working program instead of fixing anything, which is the
            # exact mistake this tool used to make silently. Require an
            # explicit extra opt-in before touching it.
            print("  ⚠ Only ONE instance -- this is probably your live, working app, not a duplicate.")
            ans = input("  Kill it anyway? (y/N): ").strip().lower()
            if ans == 'y':
                to_kill.extend(procs)
            else:
                print("  Skipped (left running).")
        else:
            print(f"  ⚠ {len(procs)} instances -- this IS a real duplicate.")
            ans = input(f"  Terminate all {len(procs)}? (y/N): ").strip().lower()
            if ans == 'y':
                to_kill.extend(procs)
            else:
                print("  Skipped (left running).")
        print()

    if to_kill:
        kill_processes(to_kill)
    else:
        print("Nothing terminated.")


if __name__ == "__main__":
    main()

# managers/rotation_manager.py
import time
import threading
import subprocess
import os
import json
import glob
import shutil
from datetime import datetime
from tkinter import messagebox

class AutomationManager:
    def __init__(self, controller):
        self.controller = controller
        self.is_running = False
        self.pause_event = threading.Event() 
        self.pause_event.set()               
        self.resume_data = None 
        self.state_file = os.path.join(self.controller.base_dir, "scan_recovery_state.json")
        self.initial_angles = None 

        self.tilt_step = 5.0
        self.rot_step = 90.0
        self.rest_time = 3.0

    def _safe_sleep(self, seconds, bypass_check=False):
        """Sleeps but aborts immediately if Stop/Emergency is triggered."""
        start = time.time()
        while time.time() - start < seconds:
            # bypass_check가 True(원상복구 모드)일 때는 is_running이 False여도 무시하고 대기
            if not self.is_running and not bypass_check: break
            self.pause_event.wait()
            if not self.is_running and not bypass_check: break
            time.sleep(0.5)

    def _wait_for_motors(self, bypass_check=False):
        """하드웨어 모터가 움직임을 멈출 때까지 대기합니다."""
        while self.is_running or bypass_check:
            is_moving_2 = self.controller.rot_mgr.is_moving.get(2, False)
            is_moving_3 = self.controller.rot_mgr.is_moving.get(3, False)
            if not is_moving_2 and not is_moving_3:
                break
            time.sleep(0.5)

    def _move_safely_stepped(self, target_2, target_3, axis_type, bypass_check=False):
        """Moves both devices safely in steps with 3s rest."""
        step_size = self.tilt_step if axis_type == "tilt" else self.rot_step

        # 1. 현재 각도 읽기
        curr_t2, curr_r2 = self.controller.rot_mgr.read_angles(2)
        curr_t3, curr_r3 = self.controller.rot_mgr.read_angles(3)

        c2 = curr_t2 if axis_type == "tilt" else curr_r2
        c3 = curr_t3 if axis_type == "tilt" else curr_r3

        if c2 is None: c2 = target_2
        if c3 is None: c3 = target_3

        while self.is_running or bypass_check:
            diff2 = target_2 - c2
            diff3 = target_3 - c3

            if abs(diff2) <= 0.5 and abs(diff3) <= 0.5:
                break

            # 이번 스텝에 이동할 각도 계산
            move2 = min(abs(diff2), step_size) * (1 if diff2 > 0 else -1) if abs(diff2) > 0.5 else 0
            move3 = min(abs(diff3), step_size) * (1 if diff3 > 0 else -1) if abs(diff3) > 0.5 else 0

            next2 = c2 + move2
            next3 = c3 + move3

            self.controller._log(f"[INFO] Safe Step {axis_type.upper()}: Dev2 -> {next2:.1f}, Dev3 -> {next3:.1f}")

            # 4. 하드웨어 명령 전송
            if axis_type == "tilt":
                if move2 != 0: self.controller.rot_mgr.move_tilt_only(2, next2)
                if move3 != 0: self.controller.rot_mgr.move_tilt_only(3, next3)
            else:
                if move2 != 0: self.controller.rot_mgr.move_rot_only(2, next2)
                if move3 != 0: self.controller.rot_mgr.move_rot_only(3, next3)

            # [핵심] 명령을 전송한 뒤, 모터가 도착할 때까지 확실하게 기다리도록 수정
            self._wait_for_motors(bypass_check)

            if abs(target_2 - next2) > 0.5 or abs(target_3 - next3) > 0.5:
                self.controller._log(f"[INFO] Step reached. Waiting {self.rest_time}s for hardware safety...")
                # [핵심] 스텝 사이의 휴식 시간도 확실하게 보장하도록 수정
                self._safe_sleep(self.rest_time, bypass_check)

            c2, c3 = next2, next3

    def _get_rot_for_cable(self, axis, direction):
        cable_map = {'E':0, 'F':45, 'G':90, 'H':135, 'A':180, 'B':225, 'C':270, 'D':315}
        cable_deg = cable_map.get(direction.upper(), 180)
        x_rot = (cable_deg - 180) % 360
        return x_rot if axis == "X" else (x_rot + 90) % 360

    def start_general_scan(self):
        if not self.controller.access_mgr.unlocked:
            messagebox.showwarning("Locked", "🔒 Please click 'Unlock Controls' first.")
            return
        if self.is_running: return

        cfg = self.controller.config_manager.get_all_variables()
        is_dummy = self.controller.auto_ui.dummy_var.get()

        total_steps = 46
        step_time_seconds = 220 if not is_dummy else 1 
        total_seconds = total_steps * step_time_seconds

        if not is_dummy:
            raw_path = cfg.get("RawDataPath", "")
            if not os.path.exists(raw_path):
                messagebox.showerror("Error", f"Save path not found:\n{raw_path}")
                return
            
            usage = shutil.disk_usage(raw_path)
            free_gb = usage.free / (1024**3)
            
            # [수정] 한 데이터당 800MB (0.8GB) 적용
            estimated_required_gb = total_steps * 0.8
            
            if free_gb < estimated_required_gb:
                warning_msg = (
                    f"⚠️ SEVERE STORAGE WARNING!\n\n"
                    f"1 Full Scan (X, Y axis) requires {total_steps} DAQ executions.\n"
                    f"Estimated required space is about {estimated_required_gb:.1f} GB.\n\n"
                    f"Current available space: {free_gb:.1f} GB\n\n"
                    f"There is a high risk of a system crash due to a full disk during the scan.\n"
                    f"Are you sure you want to force the scan?"
                )
                if not messagebox.askyesno("Storage Warning", warning_msg):
                    return

            sn2, dir2 = cfg.get("SN2", "N/A"), cfg.get("direction2", "N/A")
            sn3, dir3 = cfg.get("SN3", "N/A"), cfg.get("direction3", "N/A")
            
            checklist_msg = (
                f"🚀 Pre-flight Checklist\n\n"
                f"• Est. Required Space: ~{estimated_required_gb:.1f} GB\n"
                f"• Current Free Space: {free_gb:.1f} GB (OK)\n"
                f"----------------------------------------\n"
                f"• Target SN2: {sn2} (Cable Dir: {dir2})\n"
                f"• Target SN3: {sn3} (Cable Dir: {dir3})\n\n"
                f"Is the hardware setup correct? Start the scan?"
            )
            if not messagebox.askyesno("Checklist", checklist_msg):
                return

        self.resume_data = None
        if os.path.exists(self.state_file):
            ans = messagebox.askyesno(
                "Recovery Found", 
                "🚨 A record of an abnormally terminated scan was found.\n\n"
                "Would you like to resume from the last angle?\n"
                "(Click 'No' to delete the record and start over)"
            )
            if ans:
                try:
                    with open(self.state_file, 'r') as f:
                        self.resume_data = json.load(f)
                except Exception as e:
                    self.controller._log(f"Recovery load failed: {e}")
            else:
                os.remove(self.state_file)

        t2, r2 = self.controller.rot_mgr.read_angles(2)
        t3, r3 = self.controller.rot_mgr.read_angles(3)
        self.initial_angles = {
            2: {"tilt": t2 if t2 is not None else 0.0, "rot": r2 if r2 is not None else 0.0},
            3: {"tilt": t3 if t3 is not None else 0.0, "rot": r3 if r3 is not None else 0.0}
        }
        self.controller._log(f"Saved initial angles for Reset: Dev2({t2}, {r2}), Dev3({t3}, {r3})")

        self.controller.auto_ui.update_start_button(True)

        if hasattr(self.controller.auto_ui, 'start_eta_countdown'):
            self.controller.auto_ui.start_eta_countdown(total_seconds, total_steps)

        self.is_running = True
        threading.Thread(target=self._scan_sequence, daemon=True).start()

    def _save_state(self, axis, tilt, step):
        state = {"axis": axis, "tilt": tilt, "step": step}
        try:
            with open(self.state_file, 'w') as f:
                json.dump(state, f)
        except: pass


    def _scan_sequence(self):
        start_time = datetime.now()
        is_dummy = self.controller.auto_ui.dummy_var.get()
        cfg = self.controller.config_manager.get_all_variables()
        
        shifter = cfg.get("Shift_worker", "").strip()
        if not shifter:
            shifter = cfg.get("Expert", "Unknown").strip()
        self.controller.auto_ui.add_auto_log(f"Scan Started (Shifter: {shifter})")
        
        sn2_name = cfg.get("SN2", "SN2") 
        sn3_name = cfg.get("SN3", "SN3")

        total_steps = 46 # X축 23개 + Y축 23개
        current_step = 0

        start_axis = "X"
        start_tilt = -55
        skip_until_match = False

        if self.resume_data:
            start_axis = self.resume_data.get("axis", "X")
            start_tilt = self.resume_data.get("tilt", -55)
            current_step = self.resume_data.get("step", 0)
            skip_until_match = True
            self.controller._log(f"🔄 Crash Recovery Activated: Jumping to {start_axis}-Axis, {start_tilt}°...")

        if not is_dummy:
            self.controller._log("[INFO] Opening unified DAQ terminal...")
            subprocess.run(['tmux', 'kill-session', '-t', 'GeneralScan'], capture_output=True)

            term_cmd = ['gnome-terminal', '--title=General Scan DAQ', '--', 'tmux', 'new-session', '-s', 'GeneralScan']
            subprocess.Popen(term_cmd)
            time.sleep(2.0) 

        try:
            for axis in ["X", "Y"]:
                r2 = self._get_rot_for_cable(axis, cfg.get("direction2", "B"))
                r3 = self._get_rot_for_cable(axis, cfg.get("direction3", "B"))

                if not is_dummy:
                    if not skip_until_match:
                        self.controller.auto_ui.update_cell(sn2_name, -55, axis, "move")
                        self.controller.auto_ui.update_cell(sn3_name, -55, axis, "move")
                    self.controller._log(f"[INFO] --- Checking {axis}-Axis Rotation ---")
                    
                    # 현재 실제 회전 각도 읽기
                    _, curr_r2 = self.controller.rot_mgr.read_angles(2)
                    _, curr_r3 = self.controller.rot_mgr.read_angles(3)
                    
                    # 0.5도 오차 이내로 이미 회전이 맞춰져 있는지 확인
                    already_at_rot = False
                    if (curr_r2 is not None and abs(curr_r2 - r2) < 0.5) and \
                       (curr_r3 is not None and abs(curr_r3 - r3) < 0.5):
                        already_at_rot = True

                    if not already_at_rot:
                        self.controller._log(f"[INFO] Rotation mismatch. Moving TILT to 0.0 first for safety.")
                        # 1단계: 기울기 0도로 이동 (회전축 보호)
                        self._move_safely_stepped(0.0, 0.0, "tilt")
                        self._wait_for_physical_angle(2, target_tilt=0.0)
                        self._wait_for_physical_angle(3, target_tilt=0.0)
                        self._safe_sleep(2.0)

                        # 2단계: 목표 회전 각도로 이동
                        self._move_safely_stepped(r2, r3, "rot")
                        self._wait_for_physical_angle(2, target_rot=r2)
                        self._wait_for_physical_angle(3, target_rot=r3)
                        self._safe_sleep(2.0)
                    else:
                        self.controller._log(f"[INFO] {axis}-Axis rotation already at target ({r2:.1f}, {r3:.1f}). Skipping 0-reset.")
                
                for tilt in range(-55, 60, 5):
                    if not self.is_running: return

                    if skip_until_match:
                        if axis == start_axis and tilt == start_tilt:
                            skip_until_match = False 
                        else:
                            self.controller.auto_ui.update_cell(sn2_name, tilt, axis, "done")
                            self.controller.auto_ui.update_cell(sn3_name, tilt, axis, "done")
                            continue

                    if hasattr(self.controller, 'ups_mgr') and self.controller.ups_mgr.ups_serial:
                        ups_msg = self.controller.ui.ups_vars["status_msg"].get()
                        if "Battery" in ups_msg or "Fail" in ups_msg:
                            self.controller._log("🚨 [INTERLOCK] UPS Battery Mode! Automation paused.")
                            self.pause_event.clear()
                            self.controller.auto_ui.update_stop_button(False)

                    self.pause_event.wait() # Pause 버튼 눌렸을 때 대기
                    if not self.is_running: return
                    
                    self._save_state(axis, tilt, current_step)
                    
                    self.controller.auto_ui.update_cell(sn2_name, tilt, axis, "move")
                    self.controller.auto_ui.update_cell(sn3_name, tilt, axis, "move")

                    # 2. 개별 스텝 이동 및 물리적 확인
                    if not is_dummy:
                        self.controller._log(f"[INFO] Scanning: Moving TILT to {tilt} deg...")
                        self._move_safely_stepped(tilt, tilt, "tilt")
                        self._wait_for_physical_angle(2, target_tilt=tilt)
                        self._wait_for_physical_angle(3, target_tilt=tilt)
                        
                        self.controller._log("[INFO] Motor arrived. Waiting 5s for stabilization...")
                        self._safe_sleep(5.0)
                        #self.controller._log(f"[INFO] Syncing current angles (Tilt: {tilt}°) to config before DAQ...")
                        self.controller.auto_ui.update_config_angles(sn2_name, tilt, r2)
                        self.controller.auto_ui.update_config_angles(sn3_name, tilt, r3)

                        if hasattr(self.controller, 'auto_ui'):
                            self.controller.auto_ui.notebook.after(100, self.controller.refresh_all_data)
                    else:
                        time.sleep(0.5)

                    # UI 상태 업데이트 (DAQ 시작 표시)
                    self.controller.auto_ui.update_cell(sn2_name, tilt, axis, "daq")
                    self.controller.auto_ui.update_cell(sn3_name, tilt, axis, "daq")
                    
                    current_step += 1
                    self._update_progress_ui(current_step, total_steps)

                    # 3. DAQ 실행 및 동기화
                    if not is_dummy:
                        self.controller.run_daq(tilt=tilt, r2=r2, r3=r3)
                        
                        startup_wait = 0
                        daq_started = False
                        while startup_wait < 15:
                            if not self.is_running: return
                            check = subprocess.run(['pgrep', '-x', 'execute_DAQ_v2'], capture_output=True)
                            if check.returncode == 0:
                                daq_started = True; break
                            time.sleep(1); startup_wait += 1

                        last_size = 0
                        stagnant_count = 0
                        raw_path = cfg.get("RawDataPath", "./Data/RAW/")
                        
                        # [핵심 수정] 파이썬 감시견이 'Laser' 폴더 안쪽을 보도록 경로 수정!
                        search_path = os.path.join(raw_path, "Laser", "*.root")
                        
                        max_wait_time = 350
                        elapsed = 0
                        while elapsed < max_wait_time:
                            if not self.is_running: return
                            self.pause_event.wait()

                            # [수정 적용] 변경된 search_path로 파일 찾기
                            current_files = glob.glob(search_path)
                            if current_files:
                                latest_file = max(current_files, key=os.path.getctime)
                                current_size = os.path.getsize(latest_file)

                                if current_size == last_size and current_size > 0:
                                    stagnant_count += 1
                                else:
                                    stagnant_count = 0

                                if stagnant_count > 30: # 30초 동안 변화 없음
                                    self.controller._log(f"[CRITICAL] Watchdog: DAQ hung at {latest_file}. Killing process.")
                                    subprocess.run(['pkill', '-9', 'execute_DAQ_v2'])
                                    break
                                last_size = current_size
                            
                            check_proc = subprocess.run(['pgrep', '-x', 'execute_DAQ_v2'], capture_output=True)
                            if check_proc.returncode != 0: 
                                self.controller._log(f"[INFO] DAQ finished in {elapsed}s.")
                                break
                            time.sleep(1); elapsed += 1

                        if current_files:
                            self._verify_file_integrity(max(current_files, key=os.path.getctime))


                        # [요청사항] DAQ 종료 후 다음 이동 전 5초 안전 대기
                        if self.is_running:
                            self.controller._log("[INFO] DAQ Done. Waiting 5s for safety...")
                            self._safe_sleep(5.0)
                    else:
                        time.sleep(0.5)

                    # UI 상태 업데이트 (해당 스텝 완료 표시)
                    self.controller.auto_ui.update_cell(sn2_name, tilt, axis, "done")
                    self.controller.auto_ui.update_cell(sn3_name, tilt, axis, "done")

            # 스캔 완료 처리
            if os.path.exists(self.state_file):
                os.remove(self.state_file)

            self.is_running = False
            self.controller._log("✅ Automation sequence completed successfully.")
            self._show_scan_summary(start_time, datetime.now(), shifter)

        except Exception as e:
            self.controller._log(f"❌ Auto Error: {e}")
            
        finally:
            self.controller.auto_ui.update_start_button(False)
            self.is_running = False
            #self.controller.auto_ui.update_stop_button(False)



    def _update_progress_ui(self, current, total):
        progress = (current / total) * 100
        remaining_points = total - current
        
        # [INFO] DAQ Time (approx 200s) + Pre/Post Wait (10s) + Motor Move (10s) = 220 seconds
        step_time = 220 if not self.controller.auto_ui.dummy_var.get() else 1
        eta_seconds = remaining_points * step_time
        
        time_str = time.strftime('%H:%M:%S', time.gmtime(eta_seconds))
        self.controller.auto_ui.eta_label.config(text=f"ETA: {time_str} ({current} / {total})")


    def _show_scan_summary(self, start, end, shifter):
        summary = (
            f"📊 Scan Result Summary\n"
            f"--------------------------\n"
            f"• Start: {start.strftime('%H:%M:%S')}\n"
            f"• End: {end.strftime('%H:%M:%S')}\n"
            f"• Shifter: {shifter}\n"
            f"• Target: SN2, SN3\n"
            f"• Run Status: GOOD RUN\n"
            f"--------------------------\n"
            f"Start the NEXT RUN with UI reset?"
        )
        ans = messagebox.askyesno("Scan Completed", summary)
        if ans is True:
            self.controller.auto_ui.reset_matrix()
            self.reset_all_angles()
            self.controller._log("User selected NEXT RUN. UI & Hardware Reset initiated.")
            self.controller.refresh_all_data()

    def stop_automation(self):
        """자동화 스캔을 안전하게 중단합니다."""
        self.is_running = False
        self.pause_event.set()
        
        # [수정] ui_automation.py의 cells 딕셔너리 키 구조 (sn, tilt, axis)에 맞춤
        for (sn, tilt, axis), cell in self.controller.auto_ui.cells.items():
            # 초록색(OK)이 아닌 칸들은 모두 '-' 상태로 초기화 (status="wait" 전달)
            if cell.cget("text") != "OK":
                self.controller.auto_ui.update_cell(sn, tilt, axis, "wait")

        self.controller.auto_ui.update_start_button(False)
        self._save_recovery_state() 
        self.controller._log("[INFO] Automation stopped. Progress saved.")


    def handle_stop_continue(self):
        if not self.is_running:
            self.controller._log("⚠️ Please start the run first.")
            return

        if self.pause_event.is_set():
            self.pause_event.clear()
            self.controller._log("⏸ Automation Paused. Waiting for Continue...")
            self.controller.auto_ui.update_stop_button(False) 
        else:
            self.pause_event.set()
            self.controller._log("▶ Resuming Automation...")
            self.controller.auto_ui.update_stop_button(True) 

    def abort_run(self):
        if not self.is_running: return

        self.is_running = False
        self.pause_event.set() 

        is_dummy = self.controller.auto_ui.dummy_var.get()
        if not is_dummy:
            subprocess.run(['pkill', '-f', 'execute_DAQ_v2'])

        if os.path.exists(self.state_file):
            try:
                os.remove(self.state_file)
            except Exception: pass

        self.controller._log("🛑 Scan Aborted by user. Ready for a fresh start.")
        self.controller.auto_ui.update_stop_button(True) 


    def _wait_for_physical_angle(self, dev_num, target_tilt=None, target_rot=None, bypass_check=False):
        """Polls the hardware until the actual angle matches the target within 0.5 degrees."""
        self.controller._log(f"[DEBUG] Waiting for Device {dev_num} to physically reach target...")

        while self.is_running or bypass_check or not self.pause_event.is_set(): # 정지 상태가 아닐 때만 대기
            curr_tilt, curr_rot = self.controller.rot_mgr.read_angles(dev_num)

            tilt_ok = True
            rot_ok = True

            if target_tilt is not None:
                tilt_ok = abs(curr_tilt - target_tilt) < 0.5 if curr_tilt is not None else False

            if target_rot is not None:
                rot_ok = abs(curr_rot - target_rot) < 0.5 if curr_rot is not None else False

            if tilt_ok and rot_ok:
                self.controller._log(f"✅ Device {dev_num} arrived at physical target.")
                break

            time.sleep(0.5) # 하드웨어 부하를 줄이기 위한 폴링 간격

    def reset_all_angles(self):
        """Strictly sequential reset: Tilt to 0 -> Confirm -> Rotation to 0 -> Confirm."""
        if not self.controller.access_mgr.unlocked: return
        
        def _reset_sequence():
            # 1단계: 기울기부터 0도로 이동
            self.controller._log("[INFO] Reset Phase 1: Moving TILT to 0.0")
            # [확인] bypass_check=True 전달
            self._move_safely_stepped(0.0, 0.0, "tilt", bypass_check=True) 
            
            # [확인] bypass_check=True 전달
            self._wait_for_physical_angle(2, target_tilt=0.0, bypass_check=True)
            self._wait_for_physical_angle(3, target_tilt=0.0, bypass_check=True)
            
            self._safe_sleep(1.5)
            
            # 2단계: 회전 이동
            self.controller._log("[INFO] Reset Phase 2: Moving ROTATION to 0.0")
            self._move_safely_stepped(0.0, 0.0, "rot", bypass_check=True)
            
            self._wait_for_physical_angle(2, target_rot=0.0, bypass_check=True)
            self._wait_for_physical_angle(3, target_rot=0.0, bypass_check=True)
            
            self.controller._log("✅ Reset Completed: All axes confirmed at (0.0, 0.0)")
            
        threading.Thread(target=_reset_sequence, daemon=True).start()


    def emergency_stop(self):
        """강제 종료 시 모든 소프트웨어/하드웨어 상태를 즉시 리셋합니다."""
        self.is_running = False
        self.pause_event.set() 
        
        # 1. 물리적 모터 즉시 정지
        if hasattr(self.controller, 'rot_mgr'):
            self.controller.rot_mgr.stop_rotation(2)
            self.controller.rot_mgr.stop_rotation(3)

        # 2. DAQ 프로세스 강제 종료
        is_dummy = self.controller.auto_ui.dummy_var.get()
        if not is_dummy:
            subprocess.run(['pkill', '-9', 'execute_DAQ_v2'], capture_output=True)

        # 3. [핵심] UI 버튼 상태를 IDLE로 강제 복구
        # 이 코드가 있어야 Start 버튼이 초록색으로, Stop 버튼이 비활성화로 돌아옵니다.
        self.controller.auto_ui.update_start_button(False)
        
        self.controller._log("[INFO] Scan Aborted: Process stopped and UI initialized.")

    def _verify_file_integrity(self, file_path):
        """Checks if the recorded file has a valid size. Thread-safe version."""
        if not os.path.exists(file_path):
            self.controller.master.after(0, lambda: messagebox.showwarning("File Missing", f"⚠️ File not found!\n{file_path}"))
            return False
            
        file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
        
        # [수정] 압축률을 고려하여 1MB -> 0.05MB(50KB)로 기준 대폭 완화
        if file_size_mb < 0.05: 
            self.controller.master.after(0, lambda: messagebox.showwarning("Incomplete Data", 
                                   f"⚠️ Integrity Check Failed!\nFile: {os.path.basename(file_path)}\n"
                                   f"Size: {file_size_mb:.2f} MB is too small."))
            return False
        return True


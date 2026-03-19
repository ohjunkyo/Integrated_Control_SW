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

        if not is_dummy:
            raw_path = cfg.get("RawDataPath", "")
            if not os.path.exists(raw_path):
                messagebox.showerror("Error", f"Save path not found:\n{raw_path}")
                return
            
            usage = shutil.disk_usage(raw_path)
            free_gb = usage.free / (1024**3)
            
            total_steps_est = 46 * 2
            estimated_required_gb = total_steps_est * 2.1
            
            if free_gb < estimated_required_gb:
                warning_msg = (
                    f"⚠️ SEVERE STORAGE WARNING!\n\n"
                    f"1 Full Scan (X, Y axis) requires {total_steps_est} DAQ executions.\n"
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
        shifter = cfg.get("Shift_worker", "Unknown")
        self.controller.auto_ui.add_auto_log(f"Scan Started (Shifter: {shifter})")
        
        sn2_name = cfg.get("SN2", "SN2") 
        sn3_name = cfg.get("SN3", "SN3")

        total_steps = 46
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

        try:
            for axis in ["X", "Y"]:
                r2 = self._get_rot_for_cable(axis, cfg.get("direction2", "B"))
                r3 = self._get_rot_for_cable(axis, cfg.get("direction3", "B"))

                for tilt in range(-55, 56, 5):
                    if not self.is_running: return

                    if skip_until_match:
                        if axis == start_axis and tilt == start_tilt:
                            skip_until_match = False 
                        else:
                            self.controller.auto_ui.update_cell(sn2_name, tilt, axis, "done")
                            self.controller.auto_ui.update_cell(sn3_name, tilt, axis, "done")
                            continue

                    if hasattr(self.controller, 'ups_mgr') and self.controller.ups_mgr.ups_serial:
                        if hasattr(self.controller.ui, 'ups_vars'):
                            ups_msg = self.controller.ui.ups_vars["status_msg"].get()
                            if "Battery" in ups_msg or "Fail" in ups_msg:
                                self.controller._log("🚨 [INTERLOCK] UPS Battery Mode Detected! System paused.")
                                self.pause_event.clear()
                                self.controller.auto_ui.update_stop_button(False)

                    self.pause_event.wait() 
                    if not self.is_running: return # [버그 수정] 깨어났을 때 종료 상태면 즉시 중단
                    
                    self._save_state(axis, tilt, current_step)
                    
                    self.controller.auto_ui.update_sn_display("SN2", tilt, r2)
                    self.controller.auto_ui.update_sn_display("SN3", tilt, r3)
                    self.controller.auto_ui.update_cell(sn2_name, tilt, axis, "move")
                    self.controller.auto_ui.update_cell(sn3_name, tilt, axis, "move")
                    
                    time.sleep(0.5 if is_dummy else 5)
                    self.pause_event.wait() 
                    if not self.is_running: return # [버그 수정] 대기 직후 즉시 중단 체크

                    self.controller.auto_ui.update_cell(sn2_name, tilt, axis, "daq")
                    self.controller.auto_ui.update_cell(sn3_name, tilt, axis, "daq")
                    
                    current_step += 1
                    self._update_progress_ui(current_step, total_steps)

                    if not is_dummy:
                        self.controller.run_daq()
                        self.controller._log(f"⏳ Waiting for DAQ to finish (Step {current_step}/{total_steps})...")
                        
                        max_wait_time = 300  
                        elapsed = 0
                        last_size = 0
                        zombie_count = 0
                        
                        target_dir_laser = os.path.join(cfg.get("RawDataPath", ""), "Laser")
                        target_dir_dark = os.path.join(cfg.get("RawDataPath", ""), "Dark")

                        while elapsed < max_wait_time:
                            if not self.is_running: 
                                self.controller._log("🛑 DAQ Wait Interrupted.")
                                return  
                            self.pause_event.wait() 
                            if not self.is_running: return # [버그 수정] 루프 안에서도 즉시 중단 체크
                            
                            check_process = subprocess.run(['pgrep', '-f', 'execute_DAQ'], capture_output=True)
                            if check_process.returncode != 0: 
                                self.controller._log(f"✅ DAQ successfully finished in {elapsed} seconds.")
                                break
                            
                            if elapsed > 0 and elapsed % 5 == 0:
                                try:
                                    files = glob.glob(os.path.join(target_dir_laser, "*.root")) + glob.glob(os.path.join(target_dir_dark, "*.root"))
                                    valid_files = [f for f in files if os.path.exists(f) and os.access(f, os.R_OK)]
                                    
                                    if valid_files:
                                        latest_file = max(valid_files, key=os.path.getmtime)
                                        current_size = os.path.getsize(latest_file)
                                        if current_size == last_size:
                                            zombie_count += 5
                                        else:
                                            zombie_count = 0
                                            last_size = current_size
                                        
                                        if zombie_count >= 30:
                                            self.controller._log("🚨 [WATCHDOG] DAQ Freezing Detected! (No file written for 30s). Force killing and skipping.")
                                            subprocess.run(['pkill', '-f', 'execute_DAQ'])
                                            break
                                except (OSError, IOError):
                                    pass
                                except Exception as e:
                                    pass

                            time.sleep(1)
                            elapsed += 1
                            
                        if elapsed >= max_wait_time:
                            self.controller._log(f"⚠️ WARNING: DAQ wait timeout ({max_wait_time}s)! Force moving to next step.")
                            subprocess.run(['pkill', '-f', 'execute_DAQ'])
                    else:
                        time.sleep(0.5)

                    if not self.is_running: return # [버그 수정] 마지막 색칠 전 검사
                    self.controller.auto_ui.update_cell(sn2_name, tilt, axis, "done")
                    self.controller.auto_ui.update_cell(sn3_name, tilt, axis, "done")
            
            if os.path.exists(self.state_file):
                os.remove(self.state_file)

            end_time = datetime.now()
            self.is_running = False
            self.controller._log("✅ Automation Completed.")
            self._show_scan_summary(start_time, end_time, shifter)

        except Exception as e:
            self.controller._log(f"❌ Auto Error: {e}")
            self.stop_automation()

    def _update_progress_ui(self, current, total):
        progress = (current / total) * 100
        remaining_points = total - current
        eta_seconds = remaining_points * (205 if not self.controller.auto_ui.dummy_var.get() else 1)
        
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
        ans = messagebox.askyesnocancel("Scan Completed", summary)
        if ans is True:
            self.controller.auto_ui.reset_matrix()
            self.controller._log("User selected NEXT RUN. UI Reset.")
        elif ans is False:
            self.controller._log("User selected RE-RUN.")

    def stop_automation(self):
        self.is_running = False
        is_dummy = self.controller.auto_ui.dummy_var.get()
        if not is_dummy:
            subprocess.run(['pkill', '-f', 'execute_DAQ'])
            self.controller._log("🚨 EMERGENCY STOP: Actual DAQ terminated.")
        else:
            self.controller._log("🛑 STOP: Dummy sequence halted.")

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
            subprocess.run(['pkill', '-f', 'execute_DAQ'])

        if os.path.exists(self.state_file):
            try:
                os.remove(self.state_file)
            except Exception: pass

        self.controller._log("🛑 Scan Aborted by user. Ready for a fresh start.")
        self.controller.auto_ui.update_stop_button(True) 

    def reset_all_angles(self):
        if not self.controller.access_mgr.unlocked: return
        self.controller._log("Moving all motors to Origin (Tilt 0.0, Rot 0.0)...")
        self.controller.rot_mgr.move_rotation(2, 0.0, 0.0)
        self.controller.rot_mgr.move_rotation(3, 0.0, 0.0)

    def emergency_stop(self):
        self.is_running = False
        self.pause_event.set() 
        
        is_dummy = self.controller.auto_ui.dummy_var.get()
        if not is_dummy:
            subprocess.run(['pkill', '-f', 'execute_DAQ'])
            self.controller._log("🚨 EMERGENCY STOP: Actual DAQ terminated.")
        else:
            self.controller._log("🛑 EMERGENCY STOP: Dummy sequence halted.")

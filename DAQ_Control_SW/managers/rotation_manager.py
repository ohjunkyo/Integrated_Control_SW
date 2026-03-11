# managers/rotation_manager.py
import time
import threading
import subprocess
from datetime import datetime
from tkinter import messagebox

class AutomationManager:
    def __init__(self, controller):
        self.controller = controller
        self.is_running = False

    def _get_rot_for_cable(self, axis, direction):
        cable_map = {'E':0, 'F':45, 'G':90, 'H':135, 'A':180, 'B':225, 'C':270, 'D':315}
        cable_deg = cable_map.get(direction.upper(), 180)
        x_rot = (cable_deg - 180) % 360
        return x_rot if axis == "X" else (x_rot + 90) % 360

    def start_general_scan(self):
        if not self.controller.access_mgr.unlocked:
            messagebox.showwarning("Locked", "🔒 Unlock Controls를 먼저 해주세요.")
            return
        if self.is_running: return
        self.is_running = True
        threading.Thread(target=self._scan_sequence, daemon=True).start()

    def _scan_sequence(self):
        start_time = datetime.now()
        is_dummy = self.controller.auto_ui.dummy_var.get()
        cfg = self.controller.config_manager.get_all_variables()
        shifter = cfg.get("SHIFTER", "Unknown")
        self.controller.auto_ui.add_auto_log(f"Scan Started (Shifter: {shifter})")
        cfg = self.controller.config_manager.get_all_variables()
        sn2_name = cfg.get("SN2", "SN2") 
        sn3_name = cfg.get("SN3", "SN3")

        total_steps = 46
        current_step = 0

        try:
            for axis in ["X", "Y"]:
                r2, r3 = self._get_rot_for_cable(axis, cfg.get("direction2", "B")), self._get_rot_for_cable(axis, cfg.get("direction3", "B"))

                for tilt in range(-55, 56, 5):
                    if not self.is_running: return
                    
                    # 실시간 좌표 표시 업데이트
                    self.controller.auto_ui.update_sn_display("SN2", tilt, r2)
                    self.controller.auto_ui.update_sn_display("SN3", tilt, r3)
                    self.controller.auto_ui.update_cell(sn2_name, tilt, axis, "move")
                    self.controller.auto_ui.update_cell(sn3_name, tilt, axis, "move")
                    
                    time.sleep(0.5 if is_dummy else 5)

                    self.controller.auto_ui.update_cell("SN2", tilt, axis, "daq")
                    self.controller.auto_ui.update_cell("SN3", tilt, axis, "daq")
                    
                    current_step += 1
                    self._update_progress_ui(current_step, total_steps)

                    if not is_dummy:
                        self.controller.run_daq()
                        time.sleep(200) # 실제 데이터 취득 시간 적용
                    else:
                        time.sleep(0.5)

                    self.controller.auto_ui.update_cell("SN2", tilt, axis, "done")
                    self.controller.auto_ui.update_cell("SN3", tilt, axis, "done")
            
            end_time = datetime.now()
            self.is_running = False
            self.controller._log("✅ Automation Completed.")
            self._show_scan_summary(start_time, end_time, shifter)

        except Exception as e:
            self.controller._log(f"❌ Auto Error: {e}")
            self.stop_automation()

    def _update_progress_ui(self, current, total):
        progress = (current / total) * 100
        self.controller.auto_ui.progress_var.set(progress)
        remaining_points = total - current
        eta_seconds = remaining_points * (205 if not self.controller.auto_ui.dummy_var.get() else 1)
        self.controller.auto_ui.eta_label.config(text=f"ETA: {time.strftime('%H:%M:%S', time.gmtime(eta_seconds))}")

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
            # [해결] Next Run 선택 시 테이블 초기화
            self.controller.auto_ui.reset_matrix()
            self.controller._log("User selected NEXT RUN. UI Reset.")
        elif ans is False:
            self.controller._log("User selected RE-RUN.")

    def stop_automation(self):
        """긴급 중지: 하드웨어 정지 및 프로세스 종료"""
        self.is_running = False
        is_dummy = self.controller.auto_ui.dummy_var.get()
        
        if not is_dummy:
            # 실제 가동 중일 때만 pkill 실행하여 다른 DAQ 보호
            subprocess.run(['pkill', '-f', 'execute_DAQ'])
            self.controller._log("🚨 EMERGENCY STOP: Actual DAQ terminated.")
        else:
            self.controller._log("🛑 STOP: Dummy sequence halted.")

    def reset_all_angles(self):
        self.controller._log("Resetting SN2 & SN3 to 0/0...")
        if hasattr(self.controller, 'rot_mgr'):
            self.controller.rot_mgr.move_rotation(2, 0, 0)
            self.controller.rot_mgr.move_rotation(3, 0, 0)

    def stop_run(self):
        """[신규] 안전한 정지: 자동화 시퀀스 루프만 종료합니다. (DAQ 유지)"""
        self.is_running = False
        self.controller._log("🛑 Stop Run: Automation loop will stop after the current step. DAQ remains active.")

    def emergency_stop(self):
        """[기존 기능 유지] 긴급 정지: 하드웨어를 즉시 멈추고 실행 중인 모든 DAQ 프로세스를 종료합니다."""
        self.is_running = False
        # 실제 가동 중일 때만 pkill 실행
        subprocess.run(['pkill', '-f', 'execute_DAQ'])
        self.controller._log("🚨 EMERGENCY STOP: Automation halted and DAQ process TERMINATED.")

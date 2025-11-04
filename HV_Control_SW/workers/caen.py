import time, queue
from PyQt5.QtCore import QObject, pyqtSignal, QTimer
import numpy as np

class CaenHvWorker(QObject):
    data_ready = pyqtSignal(list)
    initial_settings_ready = pyqtSignal(dict)
    connection_status = pyqtSignal(str)
    command_feedback = pyqtSignal(str)

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.params = config['parameters']
        self.is_dual_current = 'i_mon_low' in self.params
        self.command_queue = queue.Queue()
        self.device = None
        self.hv = None
        
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._do_work)

    def run(self):
        # This method is called when the thread starts.
        # It just starts the main timer.
        self.timer.start(2000) # Start the work loop, try/poll every 2 seconds

    def stop_polling(self):
        self.timer.stop()
        if self.device:
            try:
                self.device.close()
            except: # Ignore errors on close
                pass
            self.device = None
        print("CAEN HV polling stopped.")

    def _do_work(self):
        try:
            # If not connected, try to connect first.
            if self.device is None:
                if not self.hv:
                    from caen_libs import caenhvwrapper
                    self.hv = caenhvwrapper
                
                cfg = self.config
                system_type = self.hv.SystemType[cfg['system_type']]
                link_type = self.hv.LinkType[cfg['link_type']]
                
                self.connection_status.emit(f"Connecting to HV ({cfg.get('connection_argument', '')})...")
                self.device = self.hv.Device.open(system_type, link_type, cfg.get('connection_argument', ''), cfg.get('username', ''), cfg.get('password', ''))
                self.connection_status.emit("HV Status: Connection Successful!")

            # --- If connected, process queue and poll data ---
            
            # 1. Process command queue
            try:
                cmd = self.command_queue.get_nowait()
                cmd_type, slot, ch_list, param_name, value = cmd
                if cmd_type == 'set_param':
                    self.device.set_ch_param(slot, ch_list, param_name, value)
                    self.command_feedback.emit(f"Success: Ch{ch_list[0]} {param_name} set to {value}")
                elif cmd_type == 'fetch_settings':
                    settings = {}
                    for ch in ch_list:
                        v_set = self.device.get_ch_param(slot, [ch], self.params['v_set'])[0]
                        i_set = self.device.get_ch_param(slot, [ch], self.params['i_set'])[0]
                        settings[ch] = {'v_set': v_set, 'i_set': i_set}
                    self.initial_settings_ready.emit(settings)
            except queue.Empty:
                pass

            # 2. Poll data
            results = []
            for ch_mon in self.config['channels_to_monitor']:
                vmon = self.device.get_ch_param(0, [ch_mon], self.params['v_mon'])[0]
                if self.is_dual_current:
                    imon_l = self.device.get_ch_param(0, [ch_mon], self.params['i_mon_low'])[0]
                    imon_h = self.device.get_ch_param(0, [ch_mon], self.params['i_mon_high'])[0]
                    results.append({'ch': ch_mon, 'v': vmon, 'il': imon_l, 'ih': imon_h})
                else:
                    imon = self.device.get_ch_param(0, [ch_mon], self.params['i_mon'])[0]
                    results.append({'ch': ch_mon, 'v': vmon, 'i': imon})
            self.data_ready.emit(results)

        except self.hv.Error as e:
            self.connection_status.emit(f"HV Status: Connection Failed. Retrying...")
            if self.device:
                try: self.device.close()
                except self.hv.Error: pass
            self.device = None # Set to None to trigger reconnect on next timer tick
        except Exception as e:
            self.connection_status.emit(f"Worker Error: {e}")
            self.timer.stop() # Stop on unexpected errors

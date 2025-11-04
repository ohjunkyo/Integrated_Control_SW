from PyQt5.QtCore import QObject, pyqtSignal, QThread, QTimer
from workers.arduino import ArduinoWorker
from workers.caen_process import caen_worker_process
from multiprocessing import Process, Queue

class CaenProcessBridge(QObject):
    """Reads data from the process queue and emits Qt signals."""
    data_ready = pyqtSignal(list); initial_settings_ready = pyqtSignal(dict)
    connection_status = pyqtSignal(str); command_feedback = pyqtSignal(str)
    def __init__(self, data_q: Queue):
        super().__init__(); self.data_q = data_q
        self.timer = QTimer(self); self.timer.timeout.connect(self.check_queue); self.timer.start(100)
    def check_queue(self):
        while not self.data_q.empty():
            item = self.data_q.get()
            if item['type'] == 'data': self.data_ready.emit(item['data'])
            elif item['type'] == 'status': self.connection_status.emit(item['msg'])
            elif item['type'] == 'feedback': self.command_feedback.emit(item['msg'])
            elif item['type'] == 'initial_settings': self.initial_settings_ready.emit(item['data'])
    def stop(self):
        self.timer.stop()

class WorkerManager(QObject):
    arduino_data_ready = pyqtSignal(int, object, object); caenhv_data_ready = pyqtSignal(list)
    arduino_status_changed = pyqtSignal(str); caenhv_status_changed = pyqtSignal(str)
    hv_command_feedback = pyqtSignal(str); hv_initial_settings_ready = pyqtSignal(dict)
    shutdown_complete = pyqtSignal()

    def __init__(self, config, parent=None):
        super().__init__(parent); self.config = config
        self._arduino_worker = ArduinoWorker(config['arduino_settings']['port'], config['arduino_settings']['baud_rate'])
        self._arduino_thread = QThread(); self._arduino_worker.moveToThread(self._arduino_thread)
        self._arduino_thread.started.connect(self._arduino_worker.run)
        self._arduino_worker.data_ready.connect(self.arduino_data_ready); self._arduino_worker.connection_status.connect(self.arduino_status_changed)
        
        self.caen_cmd_q = Queue(); self.caen_data_q = Queue()
        self.caen_process = Process(target=caen_worker_process, args=(self.caen_cmd_q, self.caen_data_q, config['caen_hv_settings']))
        
        self.caen_bridge = CaenProcessBridge(self.caen_data_q); self.caen_bridge_thread = QThread()
        self.caen_bridge.moveToThread(self.caen_bridge_thread)
        self.caen_bridge.data_ready.connect(self.caenhv_data_ready); self.caen_bridge.connection_status.connect(self.caenhv_status_changed)
        self.caen_bridge.command_feedback.connect(self.hv_command_feedback); self.caen_bridge.initial_settings_ready.connect(self.hv_initial_settings_ready)
        
        self.shutdown_timer = QTimer(self); self.shutdown_timer.timeout.connect(self._check_shutdown_status)

    def start_workers(self):
        self._arduino_thread.start(); self.caen_bridge_thread.start(); self.caen_process.start()

    def initiate_shutdown(self):
        print("Initiating worker shutdown...")
        if self._arduino_thread.isRunning():
            self._arduino_worker.stop_polling(); self._arduino_thread.quit()
        if self.caen_process.is_alive():
            self.caen_cmd_q.put({'type': 'stop'})
        
        # Start checking if everything has shut down
        self.shutdown_timer.start(100)
            
    def _check_shutdown_status(self):
        arduino_done = self._arduino_thread.isFinished()
        caen_done = not self.caen_process.is_alive()

        if arduino_done and caen_done:
            self.shutdown_timer.stop()
            print("Arduino thread and CAEN process stopped.")
            
            # Now, stop the bridge
            self.caen_bridge.stop()
            self.caen_bridge_thread.quit()
            self.caen_bridge_thread.wait()
            print("Bridge thread stopped.")
            
            self.shutdown_complete.emit()

    def queue_hv_command(self, command_type, slot, ch, param_name, value):
        ch_list = [ch] if isinstance(ch, int) else ch
        cmd = {'type': command_type, 'slot': slot, 'ch_list': ch_list, 'param_name': param_name, 'value': value}
        self.caen_cmd_q.put(cmd)

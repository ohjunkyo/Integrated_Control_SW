import json
import os

CONFIG_FILE = "daq_config.json" 

DEFAULT_CONFIG = {
    "system": {
        "BasePath": "/home/precalkor/ADC/PreCalibration/",
        "RawDataPath": "/home/precalkor/ADC/PreCalibration/Data/RAW/",
        "ProcessedDataPath": "/home/precalkor/ADC/PreCalibration/Data/production/",
        "FinalResultPath": "/home/precalkor/ADC/PreCalibration/Data/FinalResult/",
        "ImagePath": "/home/precalkor/ADC/PreCalibration/Data/image/",
        "ExternalPath": "/home/precalkor/external_HDD_1_4T/Data_Backup/RAW/",
        "LogDir": "/home/precalkor/ADC/PreCalibration/LOG/"
    },
    "daq": {
        "Events": 200000,
        "RecordLen": 1024,
        "PostTrigger": 60,
        "RunMode": "Laser"
    },
    "devices": [
        {"ch": 0, "role": "Monitor", "active": True, "sn": "Legacy_Mon", "cable_dir": "N/A", "hv": 1500, "tilt": 0, "rot": 0},
        {"ch": 1, "role": "Target_A", "active": False, "sn": "EM2740", "cable_dir": "A", "hv": 1740, "tilt": 0, "rot": 45},
        {"ch": 2, "role": "Target_B", "active": True, "sn": "ED1950", "cable_dir": "A", "hv": 1750, "tilt": 0, "rot": 0},
        {"ch": 3, "role": "Trigger", "active": True, "sn": "Trig_Gen", "cable_dir": "N/A", "hv": 0, "tilt": 0, "rot": 0}
    ],
    "meta": { "NOTE": "", "Expert": "Junkyo" }
}

class DAQConfigManager:  # 클래스 이름도 구분
    def __init__(self):
        self.config = {}
        self.load()

    def load(self):
        if not os.path.exists(CONFIG_FILE):
            self.config = DEFAULT_CONFIG
            self.save()
        else:
            try:
                with open(CONFIG_FILE, 'r') as f: self.config = json.load(f)
            except: self.config = DEFAULT_CONFIG

    def save(self):
        with open(CONFIG_FILE, 'w') as f: json.dump(self.config, f, indent=4)

    def get(self, section, key):
        return self.config.get(section, {}).get(key)

    def get_config_value(self, key):
        if key in self.config.get("system", {}):
            return self.config["system"][key]
        if key in self.config.get("daq", {}):
            return self.config["daq"][key]
        return None

    
    def get_all_variables(self):
        flat = {}
        flat.update(self.config.get("system", {}))
        flat.update(self.config.get("daq", {}))

        for dev in self.config.get("devices", []):
            if dev['ch'] == 0: continue # 모니터 제외
            idx = dev['ch'] # 1, 2, 3
            flat[f"SN{idx}"] = dev['sn']
            flat[f"HV{idx}"] = str(dev['hv'])

        return flat
    
    def export_to_cpp(self, run_id):
        flat = {}
        flat.update(self.config["daq"])
        flat.update(self.config["system"])
        flat["RunID"] = run_id
        for dev in self.config["devices"]:
            p = f"ch{dev['ch']}_"
            flat[p+"active"] = "true" if dev["active"] else "false"
            flat[p+"role"] = str(dev["role"])
            flat[p+"sn"] = str(dev["sn"])
            flat[p+"dir"] = str(dev["cable_dir"])
            flat[p+"hv"] = str(dev["hv"])
            flat[p+"tilt"] = str(dev["tilt"])
            flat[p+"rot"] = str(dev["rot"])
        
        # C++ 엔진이 있는 곳에 설정 파일 생성
        target_path = "/home/precalkor/ADC/PreCalibration/config_cpp.json"
        with open(target_path, 'w') as f:
            json.dump(flat, f, indent=4)
        return target_path

    def reload(self):
        """기존 GUI 호환용: 설정 파일 다시 읽기"""
        self.load()


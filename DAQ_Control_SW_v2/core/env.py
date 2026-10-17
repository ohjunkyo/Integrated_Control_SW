"""Paths shared by the whole app, and the sys.path entries that let v2 reuse the
Tk app's data code (DAQ_Control_SW) and the repo-level helpers (instance_lock,
window_focus). Imported first by main.py."""
import os
import sys

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # DAQ_Control_SW_v2/
DAQ_DIR = os.environ.get("DAQ_V1_DIR", os.path.join(os.path.dirname(APP_DIR), "DAQ_Control_SW"))
ADC_DIR = os.path.expanduser("~/ADC/ADC_test")
REGISTRY = os.path.join(ADC_DIR, "Data", "run_registry.csv")
HV_DB = os.path.join(os.path.dirname(APP_DIR), "HV_Control_SW", "monitoring_log.db")

if DAQ_DIR not in sys.path:
    sys.path.insert(0, DAQ_DIR)
ROOT_DIR = os.path.dirname(APP_DIR)   # instance_lock.py lives here
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)


NO_HW = os.environ.get("V2_NO_HW") == "1"   # tests: never open hardware connections

SETTINGS = os.path.expanduser("~/.config/precal/daq_v2.json")


def settings():
    import json
    try:
        with open(SETTINGS) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_setting(key, value):
    import json
    data = settings()
    data[key] = value
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    with open(SETTINGS + ".tmp", "w") as f:
        json.dump(data, f, indent=2)
    os.replace(SETTINGS + ".tmp", SETTINGS)


def config_file():
    """The config header in use: the one chosen with Files > Load Config, else the Tk app's."""
    from core import config
    chosen = settings().get("config_path")
    return chosen if chosen and os.path.isfile(chosen) else config.config_path(DAQ_DIR)


def read_config():
    """The DAQ configuration header as a dict (read-only, see core/config.py)."""
    from core import config
    return config.parse(config_file())

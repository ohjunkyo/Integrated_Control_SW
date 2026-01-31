import sqlite3
import json
from datetime import datetime

# DB 파일도 엔진 쪽에 안전하게 보관
DB_PATH = "/home/precalkor/ADC/PreCalibration/experiment_log.db"

class DAQDBManager:
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.init_db()

    def init_db(self):
        c = self.conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS runs 
                     (run_id INTEGER PRIMARY KEY, mode TEXT, start_time TEXT, 
                      end_time TEXT, file_path TEXT, note TEXT, config_snapshot TEXT)''')
        self.conn.commit()

    def get_next_run_id(self):
        c = self.conn.cursor()
        c.execute("SELECT MAX(run_id) FROM runs")
        r = c.fetchone()[0]
        return (r + 1) if r else 1

    def start_run(self, run_id, mode, config):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        c = self.conn.cursor()
        try:
            c.execute("INSERT INTO runs (run_id, mode, start_time, config_snapshot) VALUES (?,?,?,?)",
                      (run_id, mode, now, json.dumps(config)))
            self.conn.commit()
        except: pass

    def end_run(self, run_id, file_path):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        c = self.conn.cursor()
        c.execute("UPDATE runs SET end_time=?, file_path=? WHERE run_id=?", (now, file_path, run_id))
        self.conn.commit()

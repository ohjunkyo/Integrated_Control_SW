import sqlite3
import csv
from datetime import datetime

class DatabaseManager:
    def __init__(self, db_path, config):
        self.db_path = db_path
        self.config = config
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.is_dual_current = 'i_mon_low' in self.config['caen_hv_settings']['parameters']
        self._check_and_update_schema()

    def _get_expected_columns(self):
        columns = []
        for sensor in self.config['arduino_settings']['sensors']:
            # This line includes the fix for spaces AND '#' symbols
            name = sensor['name'].replace(" ", "_").replace("#", "")
            columns.append(f"{name}_T REAL")
            columns.append(f"{name}_H REAL")

        for ch in self.config['caen_hv_settings']['channels_to_monitor']:
            columns.append(f"Ch{ch}_V REAL")

            if self.is_dual_current:
                columns.append(f"Ch{ch}_I_L REAL")
                columns.append(f"Ch{ch}_I_H REAL")
            else:
                columns.append(f"Ch{ch}_I REAL")
        
        return columns


    def _check_and_update_schema(self):
        cursor = self.conn.cursor()
        cursor.execute("CREATE TABLE IF NOT EXISTS monitoring_data (timestamp TEXT PRIMARY KEY)")
        cursor.execute("PRAGMA table_info(monitoring_data)")
        existing_columns = {row[1] for row in cursor.fetchall()}
        expected_cols_defs = self._get_expected_columns()
        for col_def in expected_cols_defs:
            col_name = col_def.split()[0]
            if col_name not in existing_columns:
                print(f"Schema mismatch: Adding column '{col_name}' to database.")
                cursor.execute(f"ALTER TABLE monitoring_data ADD COLUMN {col_def}")
        self.conn.commit()

    def log_data(self, data_point):
        cursor = self.conn.cursor()
        cols, values, placeholders = ['timestamp'], [data_point['ts']], ['?']

        for i, sensor in enumerate(self.config['arduino_settings']['sensors']):
            name = sensor['name'].replace(" ", "_")
            s_data = data_point['sensors'].get(i, {'t': None, 'h': None})
            cols.extend([f'{name}_T', f'{name}_H']); values.extend([s_data['t'], s_data['h']]); placeholders.extend(['?', '?'])

        for ch in self.config['caen_hv_settings']['channels_to_monitor']:
            hv_data = data_point['hv'].get(ch, {})
            cols.append(f'Ch{ch}_V'); values.append(hv_data.get('v')); placeholders.append('?')
            if self.is_dual_current:
                cols.extend([f'Ch{ch}_I_L', f'Ch{ch}_I_H']); values.extend([hv_data.get('il'), hv_data.get('ih')]); placeholders.extend(['?', '?'])
            else:
                cols.append(f'Ch{ch}_I'); values.append(hv_data.get('i')); placeholders.append('?')

        sql = f"INSERT OR REPLACE INTO monitoring_data ({', '.join(cols)}) VALUES ({', '.join(placeholders)})"
        try:
            cursor.execute(sql, values)
            self.conn.commit()
        except sqlite3.Error as e:
            print(f"Database log error: {e}")

    def fetch_data_range(self, start_dt, end_dt):
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM monitoring_data WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp", (start_dt, end_dt))
        columns = [description[0] for description in cursor.description]
        rows = cursor.fetchall()
        data = {col: [] for col in columns}; timestamps = []
        for row in rows:
            dt_obj = datetime.fromisoformat(row[0])
            timestamps.append(dt_obj.timestamp())
            for i, col in enumerate(columns): data[col].append(row[i])
        return timestamps, data

    def close(self):
        self.conn.close()

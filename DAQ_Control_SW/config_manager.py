# config_manager.py
import tkinter as tk
from tkinter import ttk, messagebox
import re
import os

class ConfigManager:
    def __init__(self, filepath):
        self.filepath = filepath
        self.variables = {}
        self.parse_all_variables()

    def parse_all_variables(self):
        try:
            if not os.path.exists(self.filepath):
                print(f"Warning: Config file not found at {self.filepath}")
                return

            with open(self.filepath, 'r') as f:
                content = f.read()

            # [FIXED] 따옴표 유무, 숫자/문자 혼용 상관없이 안전하게 읽어오는 만능 정규식
            pattern = re.compile(r'const\s+(std::string|int)\s+([A-Za-z0-9_]+)\s*=\s*(.*?);')
            matches = pattern.finditer(content)

            for match in matches:
                var_name = match.group(2)
                raw_val = match.group(3).strip()
                
                # 값 앞뒤에 따옴표가 있다면 깔끔하게 제거하고 알맹이만 저장
                if raw_val.startswith('"') and raw_val.endswith('"'):
                    value = raw_val[1:-1]
                else:
                    value = raw_val
                    
                self.variables[var_name] = value

        except Exception as e:
            print(f"Error parsing config file: {e}")

    def get_config_value(self, var_name):
        # [FIXED] 매번 파일을 잘못된 정규식으로 읽어서 엉뚱한 값을 뱉던 치명적 버그 수정!
        # 파일 파싱 시 만들어둔 딕셔너리에서 가장 빠르고 정확하게 찾아옵니다.
        return self.variables.get(var_name)

    def reload(self):
        """파일을 다시 읽어와 변수들을 새로고침합니다."""
        self.variables.clear()
        self.parse_all_variables()

    def get_all_variables(self):
        return self.variables

    def get_all_configs_and_comments(self):
        """Parses the entire config file, preserving comments and structure."""
        configs = []
        try:
            if not os.path.exists(self.filepath):
                return [('error', f"File not found: {self.filepath}")]

            with open(self.filepath, 'r') as f:
                lines = f.readlines()

            var_pattern = re.compile(r'const\s+(std::string|int)\s+([A-Za-z0-9_]+)\s*=\s*(.*?);')

            for line in lines:
                line = line.strip()
                if not line:
                    continue

                if line.startswith('//'):
                    configs.append(('comment', line.lstrip('/ ')))
                else:
                    match = var_pattern.search(line)
                    if match:
                        var_name = match.group(2)
                        raw_val = match.group(3).strip()
                        
                        if raw_val.startswith('"') and raw_val.endswith('"'):
                            value = raw_val[1:-1]
                        else:
                            value = raw_val
                            
                        configs.append(('variable', var_name, value))

            return configs
        except Exception as e:
            return [('error', f"Failed to read or parse file: {e}")]

    def create_ui_entries(self, parent_frame):
        entries = {}
        try:
            with open(self.filepath, 'r') as f:
                content = f.read()

            pattern = re.compile(r'const\s+(std::string|int)\s+([A-Za-z0-9_]+)\s*=\s*(.*?);')
            matches = pattern.finditer(content)

            for match in matches:
                var_name = match.group(2)
                raw_val = match.group(3).strip()
                
                if raw_val.startswith('"') and raw_val.endswith('"'):
                    value = raw_val[1:-1]
                else:
                    value = raw_val

                if var_name.strip() and value is not None:
                    frame = ttk.Frame(parent_frame)
                    frame.pack(fill=tk.X, padx=5, pady=2)

                    label = ttk.Label(frame, text=f"{var_name}:", width=15)
                    label.pack(side=tk.LEFT)

                    entry = ttk.Entry(frame)
                    entry.pack(side=tk.RIGHT, fill=tk.X, expand=True)
                    entry.insert(0, value)
                    entries[var_name] = entry
        except FileNotFoundError:
            ttk.Label(parent_frame, text=f"{self.filepath} not found.").pack()

        return entries

    def save_from_ui(self, entries):
        try:
            with open(self.filepath, 'r') as f:
                lines = f.readlines()

            new_lines = []
            for line in lines:
                match = re.search(r'const\s+(std::string|int)\s+([A-Za-z0-9_]+)\s*=', line)
                if match and match.group(2) in entries:
                    var_name = match.group(2)
                    var_type = match.group(1)
                    
                    new_val = entries[var_name].get().strip()
                    
                    if var_type == 'std::string':
                        new_line = f'const std::string {var_name} = "{new_val}";\n'
                    else: # int
                        new_line = f'const int {var_name} = {new_val};\n'
                    
                    new_lines.append(new_line)
                else:
                    new_lines.append(line)

            with open(self.filepath, 'w') as f:
                f.writelines(new_lines)
                
            self.reload()

            messagebox.showinfo("Success", "Configuration saved successfully.")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save config file: {e}")

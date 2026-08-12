# config_manager.py
import tkinter as tk
from tkinter import ttk, messagebox
import re
import os
import subprocess
try:
    import customtkinter as ctk
    CTK_AVAILABLE = True
except Exception:
    ctk = None
    CTK_AVAILABLE = False

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
        """Build one label+entry per config var. When customtkinter is
        available the entries are laid out in a TWO-COLUMN grid (halves the
        window height) with CTk widgets; otherwise it falls back to the old
        single-column ttk rows. Returns {var_name: entry}; entry.get() works
        the same for both widget types."""
        entries = {}
        items = []
        try:
            with open(self.filepath, 'r') as f:
                content = f.read()
            pattern = re.compile(r'const\s+(std::string|int)\s+([A-Za-z0-9_]+)\s*=\s*(.*?);')
            for match in pattern.finditer(content):
                var_name = match.group(2)
                raw_val = match.group(3).strip()
                value = raw_val[1:-1] if (raw_val.startswith('"') and raw_val.endswith('"')) else raw_val
                if var_name.strip() and value is not None:
                    items.append((var_name, value))
        except FileNotFoundError:
            (ctk.CTkLabel if CTK_AVAILABLE else ttk.Label)(
                parent_frame, text=f"{self.filepath} not found.").pack()
            return entries

        if CTK_AVAILABLE:
            # Two columns of (label, entry) pairs -> grid columns (0,1) and (2,3).
            parent_frame.grid_columnconfigure((1, 3), weight=1)
            for i, (var_name, value) in enumerate(items):
                col = (i % 2) * 2
                row = i // 2
                ctk.CTkLabel(parent_frame, text=f"{var_name}:", anchor="e").grid(
                    row=row, column=col, sticky="e", padx=(8, 6), pady=4)
                entry = ctk.CTkEntry(parent_frame, width=190)
                entry.grid(row=row, column=col + 1, sticky="ew", padx=(0, 14), pady=4)
                entry.insert(0, value)
                entries[var_name] = entry
        else:
            for var_name, value in items:
                frame = ttk.Frame(parent_frame)
                frame.pack(fill=tk.X, padx=5, pady=2)
                ttk.Label(frame, text=f"{var_name}:", width=15).pack(side=tk.LEFT)
                entry = ttk.Entry(frame)
                entry.pack(side=tk.RIGHT, fill=tk.X, expand=True)
                entry.insert(0, value)
                entries[var_name] = entry

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

            # Atomic write: config3.h is the single source of truth read
            # concurrently by the shell DAQ launcher (parse_config) and several
            # Python paths. An in-place open('w') that's interrupted mid-write
            # (crash, watchdog kill, full disk) leaves a truncated config that
            # breaks every subsequent run. Write a temp file then os.replace().
            tmp = self.filepath + ".tmp"
            with open(tmp, 'w') as f:
                f.writelines(new_lines)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.filepath)

            self.reload()

            rebuild_warning = self._rebuild_daq_binary()

            messagebox.showinfo("Success", "Configuration saved successfully.")
            if rebuild_warning:
                messagebox.showwarning("DAQ Binary Rebuild Failed", rebuild_warning)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save config file: {e}")

    def _rebuild_daq_binary(self):
        """config3.h's values (SN1/SN2/direction/HV/...) are compiled directly
        into execute_DAQ_v2 as C++ const std::string literals -- the raw DAQ
        binary never re-reads the header at runtime. Without a rebuild here,
        every Quick Configuration save silently has zero effect on the next
        run's RunInfo (e.g. a swapped PMT's SN keeps showing the old serial).
        Runs 'make' in ADC_test synchronously since a run must not be started
        against a binary mid-rebuild. Returns None on success (nothing shown
        to the user -- the rebuild is meant to be invisible plumbing), or a
        warning string on failure since a failed rebuild silently leaves the
        binary on stale config values.
        """
        adc_test_dir = "/home/precalkor/ADC/ADC_test"
        try:
            result = subprocess.run(
                ["make"], cwd=adc_test_dir,
                capture_output=True, text=True, timeout=60
            )
            if result.returncode == 0:
                return None
            else:
                return ("The DAQ binary (execute_DAQ_v2) failed to rebuild -- it still "
                        "has the OLD config values until this is fixed.\n\n" + result.stderr[-500:])
        except Exception as e:
            return f"Could not rebuild the DAQ binary automatically: {e}"

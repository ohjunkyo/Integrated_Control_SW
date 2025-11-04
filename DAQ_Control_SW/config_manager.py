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

			pattern = re.compile(r'const\s+(?:std::string|int)\s+([A-Za-z0-9_]+)\s*=\s*(?:"([^"]*)"|([0-9]+));')
			matches = pattern.finditer(content)

			for match in matches:
				var_name = match.group(1)
				str_val = match.group(2)
				int_val = match.group(3)
				value = str_val if str_val is not None else int_val
				self.variables[var_name] = value

		except Exception as e:
			print(f"Error parsing config file: {e}")


	def get_config_value(self, var_name):
		try:
			if not os.path.exists(self.filepath):
				return None
			with open(self.filepath, 'r') as f:
				content = f.read()

			pattern = re.compile(rf'const\s+(?:std::string|int)\s+{var_name}\s*=\s*(?:"([^"]*)"|([0-9]+));')
			match = pattern.search(content)

			if match:
				return next((g for g in match.groups() if g is not None), None)
			return None 
		except Exception:
			return None

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

			var_pattern = re.compile(r'const\s+(std::string|int)\s+([A-Za-z0-9_]+)\s*=\s*(?:"([^"]*)"|([0-9]+));')
			
			for line in lines:
				line = line.strip()
				if not line:
					continue

				if line.startswith('//'):
					configs.append(('comment', line.lstrip('/ ')))
				else:
					match = var_pattern.search(line)
					if match:
						groups = match.groups()
						var_name = groups[1]
						str_val = groups[2]
						int_val = groups[3]
						value = str_val if str_val is not None else int_val
						configs.append(('variable', var_name, value))

			return configs
		except Exception as e:
			return [('error', f"Failed to read or parse file: {e}")]


	def create_ui_entries(self, parent_frame):
		entries = {}
		try:
			with open(self.filepath, 'r') as f:
				content = f.read()

			pattern = re.compile(r'const\s+(std::string|int)\s+([A-Za-z0-9_]+)\s*=\s*("([^"]*)"|([0-9]+));')
			matches = pattern.finditer(content)

			for match in matches:
				var_type, var_name, str_val_group, str_val, int_val = match.groups()
				value = str_val if str_val is not None else int_val

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
					new_val = entries[var_name].get()
					if var_type == 'std::string':
						new_line = f'const std::string {var_name} = "{new_val}";\n'
					else: # int
						new_line = f'const int {var_name} = {new_val};\n'
					new_lines.append(new_line)
				else:
					new_lines.append(line)

			with open(self.filepath, 'w') as f:
				f.writelines(new_lines)

			messagebox.showinfo("Success", "Configuration saved successfully.")
		except Exception as e:
			messagebox.showerror("Error", f"Failed to save config file: {e}")

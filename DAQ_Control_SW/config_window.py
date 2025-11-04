# config_window.py
import tkinter as tk
from tkinter import ttk, Toplevel

class ConfigWindow(Toplevel):
	def __init__(self, master, config_manager):
		super().__init__(master)
		self.title("Configuration")
		self.geometry("450x900")
		self.transient(master) 
		self.grab_set() 

		self.config_manager = config_manager
		self.config_entries = {}

		style = ttk.Style(self)

		main_frame = ttk.Frame(self, padding="10")
		main_frame.pack(fill=tk.BOTH, expand=True)

		config_canvas = tk.Canvas(main_frame)
		scrollbar = ttk.Scrollbar(main_frame, orient="vertical", command=config_canvas.yview)
		scrollable_frame = ttk.Frame(config_canvas)

		scrollable_frame.bind(
				"<Configure>",
				lambda e: config_canvas.configure(scrollregion=config_canvas.bbox("all"))
				)
		config_canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
		config_canvas.configure(yscrollcommand=scrollbar.set)

		self.config_entries = self.config_manager.create_ui_entries(scrollable_frame)

		button_frame = ttk.Frame(self)
		button_frame.pack(fill=tk.X, padx=10, pady=10)
		button_frame.columnconfigure((0, 1), weight=1) 

		save_button = tk.Button(
				button_frame, text="Save", command=self.save_and_close,
				bg="#c92a2a", fg="white", font=("Helvetica", 10, "bold"),
				relief="raised", borderwidth=2, padx=10, pady=5
				)
		cancel_button = tk.Button(
				button_frame, text="Cancel", command=self.destroy,
				bg="#868e96", fg="white", font=("Helvetica", 10, "bold"),
				relief="raised", borderwidth=2, padx=10, pady=5
				)

		save_button.grid(row=0, column=0, sticky="ew", padx=5)
		cancel_button.grid(row=0, column=1, sticky="ew", padx=5)

		config_canvas.pack(side="left", fill="both", expand=True)
		scrollbar.pack(side="right", fill="y")

	def save_and_close(self):
		self.config_manager.save_from_ui(self.config_entries)
		self.destroy()



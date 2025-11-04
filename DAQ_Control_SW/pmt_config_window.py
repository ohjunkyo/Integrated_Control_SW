# pmt_config_window.py
import tkinter as tk
from tkinter import ttk, Toplevel

class PMTConfigWindow(Toplevel):
    def __init__(self, master, config_manager, pmt_name):
        super().__init__(master)
        self.config_manager = config_manager
        self.pmt_name = pmt_name 
        
        self.pmt_number = self.pmt_name[-1]

        self.title(f"Edit {self.pmt_name} Config")
        self.geometry("400x400")

        main_frame = ttk.Frame(self, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)
        
        self.entries = {}
        self.config_keys = [
            f"SN{self.pmt_number}", 
            f"direction{self.pmt_number}",
            f"HV{self.pmt_number}",
            f"RotateAngle{self.pmt_number}",
            f"TitlAngle{self.pmt_number}"
        ]

        for key in self.config_keys:
            frame = ttk.Frame(main_frame)
            frame.pack(fill=tk.X, padx=5, pady=8)
            
            label = ttk.Label(frame, text=f"{key}:", width=12)
            label.pack(side=tk.LEFT)
            
            entry = ttk.Entry(frame)
            entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
            
            current_value = self.config_manager.get_config_value(key)
            if current_value is not None:
                entry.insert(0, current_value)
            
            self.entries[key] = entry

        button_frame = ttk.Frame(self)
        button_frame.pack(fill=tk.X, side=tk.BOTTOM, padx=10, pady=10)
        button_frame.columnconfigure((0, 1), weight=1)

        save_button = ttk.Button(button_frame, text="Save", command=self.save_and_close)
        cancel_button = ttk.Button(button_frame, text="Cancel", command=self.destroy)

        save_button.grid(row=0, column=0, sticky="ew", padx=5)
        cancel_button.grid(row=0, column=1, sticky="ew", padx=5)

        self.transient(master)

        self.update_idletasks()
        
        self.grab_set()       

    def save_and_close(self):
        self.config_manager.save_from_ui(self.entries)
        self.destroy()

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
            f"TiltAngle{self.pmt_number}"
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

        save_button = ttk.Button(button_frame, text="(S)ave", command=self.save_and_close)
        cancel_button = ttk.Button(button_frame, text="Cancel", command=self.destroy)

        save_button.grid(row=0, column=0, sticky="ew", padx=5)
        cancel_button.grid(row=0, column=1, sticky="ew", padx=5)

        self.transient(master)

        self.update_idletasks()
        self.bind("<Control-s>", self.save_and_close)
        self.bind("<Control-S>", self.save_and_close)

        self.grab_set()       

    def save_and_close(self, event=None):
        from tkinter import messagebox

        for key, entry in self.entries.items():
            val_raw = entry.get().strip()
            
            # 1. 문자열이 포함될 수 있는 필드(SN, direction)는 숫자 검사에서 제외
            if any(x in key for x in ["SN", "direction"]):
                if not val_raw:
                    messagebox.showerror("Input Error", f"'{key}' cannot be empty.")
                    return "break"
                continue

            # 2. 숫자 필드(HV, RotateAngle, TiltAngle) 유효성 검사
            try:
                # float으로 변환하여 숫자 외의 문자(영어 등)가 있는지 체크
                # 마이너스 기호 '-'는 float 변환 시 자동으로 허용됩니다.
                val = float(val_raw)
                
                # Rotation 범위 검사 (0도 ~ 180도)
                if "RotateAngle" in key:
                    if not (0 <= val <= 180):
                        messagebox.showerror("Range Error", 
                                             f"Rotation Angle must be between 0 and 180.\n(Input: {val_raw})")
                        return "break"
                
                # Tilt 범위 검사 (-55도 ~ 55도)
                elif "TiltAngle" in key:
                    if not (-55 <= val <= 55):
                        messagebox.showerror("Range Error", 
                                             f"Tilt Angle must be between -55 and 55.\n(Input: {val_raw})")
                        return "break"
                
                # HV 등 기타 숫자 필드 (기본적인 숫자 체크만 수행)
                else:
                    pass

            except ValueError:
                messagebox.showerror("Invalid Input", 
                                     f"Invalid characters detected in '{key}'.\nPlease enter numbers only.\n(Input: {val_raw})")
                return "break"

        self.config_manager.save_from_ui(self.entries)
        self.destroy()
        
        return "break"

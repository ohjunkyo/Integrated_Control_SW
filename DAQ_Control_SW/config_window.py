# config_window.py
import tkinter as tk
from tkinter import ttk, Toplevel, simpledialog, messagebox

# [핵심] control_access.py에서 통합 비밀번호를 불러옵니다.
from managers.control_access import ADMIN_PASSWORD 

class ConfigWindow(Toplevel):
    def __init__(self, master, config_manager):
        super().__init__(master)
        self.title("Configuration")
        self.geometry("550x900")
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

        self.protected_keys = [
            "BasePath", "DaqProgramPath", "RawDataPath", "ProcessedDataPath",
            "FinalResultPath", "ExternalPath", "ImagePath", "LogDir",
            "ChannelMask", "PostTrigger", "Events", "TimeWindow",
            "NumSequences", "IntervalTime"
        ]

        for key in self.protected_keys:
            if key in self.config_entries:
                self.config_entries[key].config(state="readonly", foreground="gray")

        button_frame = ttk.Frame(self)
        button_frame.pack(fill=tk.X, padx=10, pady=10)
        button_frame.columnconfigure((0, 1, 2), weight=1) 

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
        
        self.unlock_button = tk.Button(
                button_frame, text="🔒 Admin Unlock", command=self.request_admin_unlock,
                bg="#f0ad4e", fg="black", font=("Helvetica", 10, "bold"),
                relief="raised", borderwidth=2, padx=10, pady=5
                )

        save_button.grid(row=0, column=0, sticky="ew", padx=5)
        cancel_button.grid(row=0, column=1, sticky="ew", padx=5)
        self.unlock_button.grid(row=0, column=2, sticky="ew", padx=5)

        config_canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def request_admin_unlock(self):
        """Prompt for password and unlock protected fields if matched."""
        pwd = simpledialog.askstring("Admin Unlock", "Enter Admin Password:", show='*')
        if pwd is None: return

        # [수정] 통합 비밀번호와 비교하는 아주 직관적인 로직
        if pwd == ADMIN_PASSWORD: 
            for key in self.protected_keys:
                if key in self.config_entries:
                    self.config_entries[key].config(state="normal", foreground="black")
            
            self.unlock_button.config(text="🔓 Unlocked", state="disabled", bg="#28a745", fg="white")
        else:
            messagebox.showerror("Error", "Incorrect password.")

    def save_and_close(self):
        self.config_manager.save_from_ui(self.config_entries)
        self.destroy()

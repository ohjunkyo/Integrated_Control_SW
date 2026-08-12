# config_window.py
import tkinter as tk
from tkinter import ttk, Toplevel, simpledialog, messagebox
try:
    import customtkinter as ctk
    CTK_AVAILABLE = True
except Exception:
    ctk = None
    CTK_AVAILABLE = False

from managers.control_access import ADMIN_PASSWORD


# Base class chosen at import time: a CTkToplevel when customtkinter is
# available (rounded, modern, scrollable), else the legacy tk.Toplevel.
_Base = ctk.CTkToplevel if CTK_AVAILABLE else Toplevel


class ConfigWindow(_Base):
    def __init__(self, master, config_manager):
        super().__init__(master)
        self.title("Configuration")
        self.transient(master)
        self.grab_set()

        self.config_manager = config_manager
        self.config_entries = {}

        self.protected_keys = [
            "BasePath", "DaqProgramPath", "RawDataPath", "ProcessedDataPath",
            "FinalResultPath", "ExternalPath", "ImagePath", "LogDir",
            "ChannelMask", "PostTrigger", "Events", "TimeWindow",
            "NumSequences", "IntervalTime"
        ]

        if CTK_AVAILABLE:
            self._build_ctk()
        else:
            self._build_legacy()

    # ── customtkinter layout (two-column, scrollable) ───────────────────────
    def _build_ctk(self):
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")
        self.geometry("760x720")

        ctk.CTkLabel(self, text="Configuration",
                     font=ctk.CTkFont(size=20, weight="bold")).pack(anchor="w", padx=22, pady=(18, 0))
        ctk.CTkLabel(self, text="Paths and DAQ constants. Protected fields need Admin Unlock.",
                     text_color="#6c757d", font=ctk.CTkFont(size=12)).pack(anchor="w", padx=22, pady=(0, 10))

        # Anchor the action bar to the bottom FIRST so the scrollable list
        # fills only the remaining space instead of pushing the buttons
        # off-screen when its content grows.
        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(side=tk.BOTTOM, fill=tk.X, padx=16, pady=(0, 16))

        scroll = ctk.CTkScrollableFrame(self)
        scroll.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 10))
        self.config_entries = self.config_manager.create_ui_entries(scroll)

        for key in self.protected_keys:
            if key in self.config_entries:
                try:
                    self.config_entries[key].configure(state="disabled", text_color="#8a9099")
                except Exception:
                    pass

        self.unlock_button = ctk.CTkButton(
            btns, text="🔒 Admin Unlock", command=self.request_admin_unlock,
            fg_color="#e8a317", hover_color="#c98f12", text_color="black", width=150)
        self.unlock_button.pack(side=tk.LEFT)
        ctk.CTkButton(btns, text="Save", command=self.save_and_close,
                      fg_color="#c92a2a", hover_color="#a81f1f", width=120).pack(side=tk.RIGHT)
        ctk.CTkButton(btns, text="Cancel", command=self.destroy, fg_color="transparent",
                      border_width=1, text_color=("#1f2430", "#e5e5e5"), width=100).pack(side=tk.RIGHT, padx=(0, 8))

    # ── legacy tk layout (only if customtkinter missing) ────────────────────
    def _build_legacy(self):
        self.geometry("550x900")
        main_frame = ttk.Frame(self, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)
        config_canvas = tk.Canvas(main_frame)
        scrollbar = ttk.Scrollbar(main_frame, orient="vertical", command=config_canvas.yview)
        scrollable_frame = ttk.Frame(config_canvas)
        scrollable_frame.bind("<Configure>",
                              lambda e: config_canvas.configure(scrollregion=config_canvas.bbox("all")))
        config_canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        config_canvas.configure(yscrollcommand=scrollbar.set)
        self.config_entries = self.config_manager.create_ui_entries(scrollable_frame)
        for key in self.protected_keys:
            if key in self.config_entries:
                self.config_entries[key].config(state="readonly", foreground="gray")
        button_frame = ttk.Frame(self)
        button_frame.pack(fill=tk.X, padx=10, pady=10)
        button_frame.columnconfigure((0, 1, 2), weight=1)
        tk.Button(button_frame, text="Save", command=self.save_and_close,
                  bg="#c92a2a", fg="white", font=("Helvetica", 10, "bold"),
                  relief="raised", borderwidth=2, padx=10, pady=5).grid(row=0, column=0, sticky="ew", padx=5)
        tk.Button(button_frame, text="Cancel", command=self.destroy,
                  bg="#868e96", fg="white", font=("Helvetica", 10, "bold"),
                  relief="raised", borderwidth=2, padx=10, pady=5).grid(row=0, column=1, sticky="ew", padx=5)
        self.unlock_button = tk.Button(
            button_frame, text="🔒 Admin Unlock", command=self.request_admin_unlock,
            bg="#f0ad4e", fg="black", font=("Helvetica", 10, "bold"),
            relief="raised", borderwidth=2, padx=10, pady=5)
        self.unlock_button.grid(row=0, column=2, sticky="ew", padx=5)
        config_canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def request_admin_unlock(self):
        """Prompt for password and unlock protected fields if matched."""
        pwd = simpledialog.askstring("Admin Unlock", "Enter Admin Password:", show='*')
        if pwd is None:
            return
        if pwd == ADMIN_PASSWORD:
            for key in self.protected_keys:
                if key in self.config_entries:
                    try:
                        if CTK_AVAILABLE:
                            self.config_entries[key].configure(state="normal", text_color="black")
                        else:
                            self.config_entries[key].config(state="normal", foreground="black")
                    except Exception:
                        pass
            if CTK_AVAILABLE:
                self.unlock_button.configure(text="🔓 Unlocked", state="disabled",
                                             fg_color="#2e9e4f", text_color="white")
            else:
                self.unlock_button.config(text="🔓 Unlocked", state="disabled", bg="#28a745", fg="white")
        else:
            messagebox.showerror("Error", "Incorrect password.")

    def save_and_close(self):
        self.config_manager.save_from_ui(self.config_entries)
        self.destroy()

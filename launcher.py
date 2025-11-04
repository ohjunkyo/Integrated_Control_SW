# launcher.py
import tkinter as tk
import subprocess
import os
import sys
from tkinter import messagebox

class AppLauncher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Integrated Control Software Launcher")
        self.geometry("400x400") 

        self.configure(bg='#333333')
        button_font = ("Helvetica", 14, "bold")

        daq_button = tk.Button(self, text="Start DAQ Control", font=button_font, bg="#007ACC", fg="white", padx=20, pady=15, command=self.launch_daq_control)
        daq_button.pack(pady=15, fill=tk.X, padx=20)

        hv_button = tk.Button(self, text="Start HV Monitor", font=button_font, bg="#5CB85C", fg="white", padx=20, pady=15, command=self.launch_hv_monitor)
        hv_button.pack(pady=10, fill=tk.X, padx=20)

        vm_button = tk.Button(
                self, text="Start Laser (Windows VM)", font=button_font,
                bg="#f0ad4e", fg="white", padx=20, pady=15,
                command=self.launch_vmware_vm
                )
        vm_button.pack(pady=10, fill=tk.X, padx=20)

        exit_button = tk.Button(self, text="Exit Launcher", font=("Helvetica", 10), bg="#868e96", fg="white", command=self.destroy)
        exit_button.pack(pady=15, padx=20)

    def get_python_executable(self):
        return sys.executable

    def launch_daq_control(self):
        print("Launching DAQ Control...")
        python_exe = self.get_python_executable()
        script_path = os.path.join("DAQ_Control_SW", "main.py")
        subprocess.Popen([python_exe, script_path])


    def launch_hv_monitor(self):
        print("Launching HV Monitor...")
        python_exe = self.get_python_executable()

        script_path = os.path.join("HV_Control_SW", "monitoring_app.py")
        config_path = os.path.join("HV_Control_SW", "config_precal.json")

        command = [python_exe, script_path, config_path]
        
        try:
            subprocess.Popen(command)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to launch HV Monitor:\n{e}")


    def launch_vmware_vm(self):
        vmx_path = "/home/precalkor/vmware/Windows11/Windows 11 x64.vmx" 

        try:
            print(f"Starting VM: {vmx_path}")
            command = ['vmrun', 'start', vmx_path]
            subprocess.Popen(command)
        except FileNotFoundError:
            messagebox.showerror("Error", "'vmrun' command not found.\nPlease ensure VMware Workstation is installed and 'vmrun' is in your system's PATH.")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to start VM: {e}")

if __name__ == "__main__":
    app = AppLauncher()
    app.mainloop()

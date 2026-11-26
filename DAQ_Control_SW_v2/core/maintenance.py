"""Backup run (sync_data_v2.sh through a job slot) and the DAQ Diag sudo tools (pkexec, output
streamed), with the same commands as the Tk app's _run_backup_now / DAQ_DIAG_MANUAL. Qt-free."""
import os
import subprocess

BACKUP_SCRIPT = os.path.expanduser("~/sync_data_v2.sh")
BACKUP_KIND = "backup"          # job kind/slot, as the Tk console slot "backup"
BACKUP_NAME = "Backup (sync_data_v2)"
BACKUP_QUESTION = ("Copy RAW files to External HDD1, verify them, and delete the\n"
                   "verified local copies to free disk space?\n\n"
                   "Files whose backup cannot be verified are always kept.")


def backup_command(dry_run=False):
    """The shell string the Tk console runs (its argv joined with spaces)."""
    return " ".join([BACKUP_SCRIPT] + (["--dry-run"] if dry_run else []))


MANUAL = {
    "Load driver": "/usr/sbin/modprobe CAENUSBdrvB",
    "Load driver at boot": "/usr/bin/bash -c \"echo CAENUSBdrvB > /etc/modules-load.d/caen.conf\"",
    "Reinstall drivers": "/usr/bin/bash /home/precalkor/ADC/auto_DAQ_setup.sh",
    "Add USB groups": "/usr/bin/bash -c \"usermod -aG dialout,plugdev precalkor\"",
}
# verdict title -> (fix text, (button label, command, re-check delay s)) as the Tk _diagnose_daq
FIX = {
    "Driver not built for this kernel": ("Reinstall the CAEN drivers (takes a few minutes).",
                                         ("Reinstall drivers", MANUAL["Reinstall drivers"], 2)),
    "Driver not loaded": ("Load the CAENUSBdrvB driver.", ("Load driver", MANUAL["Load driver"], 2)),
    "Permission denied": ("Add this user to dialout/plugdev, then log out and in.",
                          ("Add groups", MANUAL["Add USB groups"], 0)),
}


def pkexec(cmd):
    return "pkexec " + cmd


def stream(cmd, out, popen=subprocess.Popen):
    """Run a shell command, out(text) per line, then '[exit code: N]'. -> exit code or None."""
    out(f"\n$ {cmd}\n")
    try:
        proc = popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            out(line)
        rc = proc.wait()
        out(f"[exit code: {rc}]\n")
        return rc
    except Exception as e:
        out(f"[ERROR] {e}\n")
        return None

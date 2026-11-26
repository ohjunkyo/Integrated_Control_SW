"""Copy routine reports to the shared Google Drive folder with rclone (no Qt). The operator sets up the rclone
remote once (`rclone config`, Google Drive, name REMOTE); the on/off choice is kept in SETTINGS."""
import json
import os
import shutil
import subprocess

REMOTE = "KOR_precal"
FOLDER_ID = "1ARRg-2RVZyy2TELhOUnFKF1AQZcsyyKB"
FOLDER_URL = f"https://drive.google.com/drive/folders/{FOLDER_ID}"
SETTINGS = os.path.expanduser("~/.cache/precal/report_drive.json")


def rclone():
    local = os.path.expanduser("~/bin/rclone")
    return shutil.which("rclone") or (local if os.access(local, os.X_OK) else None)


def enabled():
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            return bool(json.load(f).get("enabled"))
    except (OSError, ValueError):
        return False


def set_enabled(on):
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    with open(SETTINGS, "w", encoding="utf-8") as f:
        json.dump({"enabled": bool(on)}, f)


def ready():
    """(True, '') when rclone and its REMOTE are set up, else (False, what is missing)."""
    exe = rclone()
    if not exe:
        return False, "rclone is not installed"
    try:
        r = subprocess.run([exe, "listremotes"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"rclone failed: {e}"
    if f"{REMOTE}:" not in r.stdout.split():
        return False, f"rclone remote '{REMOTE}' is not configured (run: rclone config)"
    return True, ""


def upload(path, timeout=600):
    """Copy one file into the Drive folder; (ok, message). Blocking: call it off the GUI thread."""
    ok, why = ready()
    if not ok:
        return False, why
    try:
        r = subprocess.run([rclone(), "copy", path, f"{REMOTE}:", "--drive-root-folder-id", FOLDER_ID],
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    if r.returncode:
        lines = (r.stderr or r.stdout).strip().splitlines()
        return False, lines[-1][-200:] if lines else f"rclone exit {r.returncode}"
    return True, os.path.basename(path)

"""File actions of the Data Files, Images and Logs tabs, done the way the Tk app does them:
rsync move and delete of data files (main.move_data_files / delete_data_files), image-to-PDF
merge and image delete (image_viewer.py), log gathering (ui_manager._gather_all_logs).
Every data-file step goes to the app log (logs/log_<date>.txt, same line format). Qt-free."""
import glob
import os
import shutil
import subprocess
from datetime import datetime

from core import env

LOG_DIR = os.path.join(env.DAQ_DIR, "logs")


def log(message):
    """Same file and line format as the Tk app's _log."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, f"log_{datetime.now():%Y-%m-%d}.txt"), "a") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}\n")
    except OSError as e:
        print(f"Error while logging: {e}")


# ---------------------------------------------------------------- Data Files
def rsync_command(path, dest_dir):
    return ["rsync", "-a", "--remove-source-files", path, dest_dir]


def move_files(paths, dest_dir, progress=None, run=subprocess.run):
    """rsync each file into dest_dir (source removed on success). -> (moved, failed names)."""
    moved, failed = 0, []
    for i, path in enumerate(paths):
        name = os.path.basename(path)
        if progress:
            progress(f"Rsync Moving... ({i + 1}/{len(paths)}): {name}")
        try:
            run(rsync_command(path, dest_dir), check=True, capture_output=True, text=True)
            log(f"[RSYNC SUCCESS] {path} -> {dest_dir}")
            moved += 1
        except subprocess.CalledProcessError as e:
            log(f"[RSYNC ERROR] File: {name}\nError: {e.stderr}")
            failed.append(name)
        except Exception as e:
            log(f"[PYTHON ERROR] File: {name}\nError: {e}")
            failed.append(name)
    return moved, failed


def delete_question(paths):
    n = len(paths)
    names = "\n".join(f"- {os.path.basename(p)}" for p in paths[:5])
    if n > 5:
        names += f"\n...and {n - 5} more."
    return (f"Are you sure you want to permanently delete {n} selected file(s)?\n\n{names}\n\n"
            "This action cannot be undone.")


def delete_files(paths, remove=os.remove):
    """-> (deleted, failed names)."""
    deleted, failed = 0, []
    for path in paths:
        try:
            remove(path)
            log(f"[INFO] Deleted file: {path}")
            deleted += 1
        except Exception as e:
            log(f"[ERROR] Failed to delete {path}: {e}")
            failed.append(os.path.basename(path))
    return deleted, failed


# ---------------------------------------------------------------- Images
def pdf_default_name():
    return f"Report_{datetime.now():%Y%m%d_%H%M}.pdf"


def images_to_pdf(files, save_path):
    """Merge images into one PDF, one page each (PIL, as the Tk ImageViewer)."""
    from PIL import Image
    pages = [Image.open(f).convert("RGB") for f in files]
    if pages:
        pages[0].save(save_path, save_all=True, append_images=pages[1:])


def delete_images(paths, remove=os.remove):
    """-> failed paths (errors are printed, as in the Tk ImageViewer)."""
    failed = []
    for path in paths:
        try:
            remove(path)
        except Exception as e:
            print(f"[ERROR] Error deleting {path}: {e}")
            failed.append(path)
    return failed


# ---------------------------------------------------------------- Logs
def unified_dir(sources):
    """<parent of the Laser log dir>/_UNIFIED, as the Tk app."""
    return os.path.join(os.path.dirname(sources["Laser"][0].rstrip("/")), "_UNIFIED")


def gather_logs(sources, hv_db, copy=shutil.copy2):
    """Copy (not move) every log file into one folder per source, plus the HV DB.
    -> (copied count, destination root). Raises if a folder cannot be created."""
    dest_root = unified_dir(sources)
    copied = 0
    for name, (dir_path, pattern) in sources.items():
        out_dir = os.path.join(dest_root, name)
        os.makedirs(out_dir, exist_ok=True)
        for src in glob.glob(os.path.join(dir_path, pattern)):
            try:
                copy(src, os.path.join(out_dir, os.path.basename(src)))
                copied += 1
            except Exception:
                pass
    if os.path.exists(hv_db):
        os.makedirs(os.path.join(dest_root, "HV"), exist_ok=True)
        copy(hv_db, os.path.join(dest_root, "HV", os.path.basename(hv_db)))
        copied += 1
    return copied, dest_root

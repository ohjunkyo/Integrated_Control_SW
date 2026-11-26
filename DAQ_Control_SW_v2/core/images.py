"""Plot/report files under the image directory (config ImagePath): folder
discovery, listing and PDF page rendering. Same rules as the Tk ImageViewer."""
import os

from core import env

try:
    import fitz   # PyMuPDF
except ImportError:   # pragma: no cover
    fitz = None

IMAGE_EXT = (".png", ".jpg", ".jpeg")
# Folders outside the image root listed as if they were in it (label -> path).
EXTRA = {"Routine": os.path.join(env.ADC_DIR, "Data", "RoutineStats")}


def valid_ext():
    return IMAGE_EXT + ((".pdf",) if fitz else ())


def folder_path(base, folder):
    return EXTRA.get(folder) or os.path.join(base, folder)


def _has_files(full, ext):
    return any(f.lower().endswith(ext) for _r, _d, fs in os.walk(full) for f in fs)


def subdirs(base):
    """Every subfolder of base that holds viewable files (new report folders show up on their own), then EXTRA."""
    out, ext = [], valid_ext()
    try:
        for d in sorted(os.listdir(base)):
            full = os.path.join(base, d)
            if os.path.isdir(full) and _has_files(full, ext):
                out.append(d)
    except OSError:
        pass
    return out + [k for k, full in EXTRA.items() if k not in out and _has_files(full, ext)]


def list_files(base, folder="All", sort="time"):
    """[(path, mtime)] of viewable files in one folder or all of them, nested subfolders included."""
    ext = valid_ext()
    out = []
    for d in (subdirs(base) if folder == "All" else [folder]):
        for root, _dirs, names in os.walk(folder_path(base, d)):
            for f in names:
                if f.lower().endswith(ext):
                    p = os.path.join(root, f)
                    try:
                        out.append((p, os.path.getmtime(p)))
                    except OSError:
                        continue
    if sort == "name":
        out.sort(key=lambda x: os.path.basename(x[0]).lower())
    else:
        out.sort(key=lambda x: -x[1])
    return out


def folder_label(base, path):
    """Folder of a file relative to the image root, e.g. 'Summary/KPS2026Fall'."""
    for label, root in EXTRA.items():
        if path.startswith(root + os.sep):
            return os.path.normpath(os.path.join(label, os.path.relpath(os.path.dirname(path), root)))
    try:
        return os.path.relpath(os.path.dirname(path), base)
    except ValueError:
        return os.path.basename(os.path.dirname(path))


def matches(query, *texts):
    """Every space-separated term of the query occurs in one of the texts (case-insensitive)."""
    hay = " ".join(t.lower() for t in texts if t)
    return all(term in hay for term in query.lower().split())


def pdf_page_png(doc, page, zoom):
    """PNG bytes of one PDF page at the given zoom."""
    return doc[page].get_pixmap(matrix=fitz.Matrix(zoom, zoom)).tobytes("png")

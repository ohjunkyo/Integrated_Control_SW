"""Plot/report files under the image directory (config ImagePath): folder
discovery, listing and PDF page rendering. Same rules as the Tk ImageViewer."""
import os

try:
    import fitz   # PyMuPDF
except ImportError:   # pragma: no cover
    fitz = None

IMAGE_EXT = (".png", ".jpg", ".jpeg")


def valid_ext():
    return IMAGE_EXT + ((".pdf",) if fitz else ())


def subdirs(base):
    """Every subfolder of base that holds viewable files (new report folders show up on their own)."""
    out, ext = [], valid_ext()
    try:
        for d in sorted(os.listdir(base)):
            full = os.path.join(base, d)
            if os.path.isdir(full):
                try:
                    if any(f.lower().endswith(ext) for f in os.listdir(full)):
                        out.append(d)
                except OSError:
                    continue
    except OSError:
        pass
    return out


def list_files(base, folder="All", sort="time"):
    """[(path, mtime)] of viewable files in one folder or all of them."""
    ext = valid_ext()
    out = []
    for d in (subdirs(base) if folder == "All" else [folder]):
        full = os.path.join(base, d)
        try:
            names = os.listdir(full)
        except OSError:
            continue
        for f in names:
            if f.lower().endswith(ext):
                p = os.path.join(full, f)
                try:
                    out.append((p, os.path.getmtime(p)))
                except OSError:
                    continue
    if sort == "name":
        out.sort(key=lambda x: os.path.basename(x[0]).lower())
    else:
        out.sort(key=lambda x: -x[1])
    return out


def pdf_page_png(doc, page, zoom):
    """PNG bytes of one PDF page at the given zoom."""
    return doc[page].get_pixmap(matrix=fitz.Matrix(zoom, zoom)).tobytes("png")

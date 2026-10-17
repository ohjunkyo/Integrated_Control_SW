"""DAQ System > Images: the Tk ImageViewer (folder, search, sort, file list, image
and PDF view with page thumbnails), embedded in the tab instead of a separate
window. Convert Selected to PDF merges the selected images into one PDF and
Delete Selected removes the selected files, as image_viewer.py (core.file_ops)."""
import os
import re
import subprocess
import threading
from datetime import datetime

from PyQt5.QtCore import QObject, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PyQt5.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QFileDialog, QGraphicsPixmapItem,
                             QGraphicsScene, QGraphicsView, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                             QListWidgetItem, QMessageBox, QPushButton, QSplitter, QStyle, QStyledItemDelegate,
                             QVBoxLayout, QWidget)

from app import theme
from core import env, file_ops, images
_PREFIX = re.compile(r"^precal_(raw|prd|result)_kor_run_")   # same on every run file; shortened in the list


def short_name(name):
    return _PREFIX.sub(r"\1 ", name)


class _Bus(QObject):
    thumb = pyqtSignal(object, int, bytes)   # job token, page, png
    deleted = pyqtSignal(list)


class FileDelegate(QStyledItemDelegate):
    """Two-line file entry: name (bold, middle-elided) over 'folder . time' in small grey."""

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), option.fontMetrics.height() * 2 + 12)

    def paint(self, p, option, index):
        name, meta = index.data(Qt.UserRole)
        p.save()
        r = option.rect
        if option.state & QStyle.State_Selected:
            p.fillRect(r, QColor("#0b63b6"))
            fg, sub = QColor("white"), QColor("#dbeafe")
        else:
            if index.row() % 2:
                p.fillRect(r, QColor(theme.C["alt_row"]))
            fg, sub = QColor(theme.C["text"]), QColor(theme.C["hint"])
        f = QFont(option.font)
        f.setBold(True)
        p.setFont(f)
        p.setPen(fg)
        fm = p.fontMetrics()
        line1 = r.adjusted(8, 4, -8, -r.height() // 2)
        p.drawText(line1, Qt.AlignLeft | Qt.AlignVCenter, fm.elidedText(name, Qt.ElideMiddle, line1.width()))
        f.setBold(False)
        f.setPointSizeF(f.pointSizeF() * 0.85)
        p.setFont(f)
        p.setPen(sub)
        line2 = r.adjusted(8, r.height() // 2, -8, -4)
        p.drawText(line2, Qt.AlignLeft | Qt.AlignVCenter, p.fontMetrics().elidedText(meta, Qt.ElideRight, line2.width()))
        p.restore()


class View(QGraphicsView):
    """Image canvas: fit on load, wheel to zoom, drag to pan."""

    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.item = QGraphicsPixmapItem()
        self.item.setTransformationMode(Qt.SmoothTransformation)
        self.scene().addItem(self.item)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setRenderHint(QPainter.SmoothPixmapTransform)
        self.setStyleSheet("background: #232629; border: none;")
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)

    def show_pixmap(self, pm):
        self.orig = pm
        self.item.setScale(1.0)
        self.item.setPixmap(pm)
        self.scene().setSceneRect(QRectF(0, 0, pm.width(), pm.height()))
        self.fit()
        QTimer.singleShot(0, lambda: self.auto_fit and self.fit())   # again once the view has its size

    def _resample(self):
        """The view's smooth mode is bilinear, which aliases thin lines and text when a large plot is shown
        at a third of its size; below full size, draw an area-averaged copy at screen resolution instead."""
        pm = getattr(self, "orig", None)
        if pm is None or pm.isNull():
            return
        v = self.transform().m11() * self.devicePixelRatioF()
        if v >= 0.999:
            if self.item.scale() != 1.0:
                self.item.setScale(1.0)
                self.item.setPixmap(pm)
            return
        small = pm.scaledToWidth(max(1, round(pm.width() * v)), Qt.SmoothTransformation)
        self.item.setPixmap(small)
        self.item.setScale(pm.width() / small.width())

    def fit(self):
        self.auto_fit = True
        if getattr(self, "orig", None) is not None and not self.orig.isNull():
            self.fitInView(self.scene().sceneRect(), Qt.KeepAspectRatio)
            self._resample()

    def zoom(self, f):
        self.auto_fit = False
        self.scale(f, f)
        self._resample()

    def wheelEvent(self, e):
        self.zoom(1.15 if e.angleDelta().y() > 0 else 1 / 1.15)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if getattr(self, "auto_fit", True):
            self.fit()


class ImagesPage(QWidget):
    def __init__(self):
        super().__init__()
        self.base = env.read_config().get("ImagePath", "")
        self.paths = []
        self.doc, self.page = None, 0
        self._job = None
        self.bus = _Bus()
        self.bus.thumb.connect(self._add_thumb)
        self.bus.deleted.connect(self._on_deleted)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        split = QSplitter(Qt.Horizontal)

        side = QGroupBox("Files")
        sl = QVBoxLayout(side)
        row = QHBoxLayout()
        row.addWidget(QLabel("Folder"))
        self.folder = QComboBox()
        self.folder.addItems(["All"] + images.subdirs(self.base))
        self.folder.currentTextChanged.connect(lambda _t: self.reload())
        row.addWidget(self.folder, 1)
        sl.addLayout(row)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search file name...")
        self.search.textChanged.connect(self._fill)
        sl.addWidget(self.search)
        row = QHBoxLayout()
        self.sort = QComboBox()
        self.sort.addItems(["Time", "Name"])
        self.sort.currentTextChanged.connect(lambda _t: self.reload())
        row.addWidget(QLabel("Sort"))
        row.addWidget(self.sort, 1)
        ref = QPushButton("Refresh")
        ref.clicked.connect(self.reload)
        row.addWidget(ref)
        sl.addLayout(row)
        self.list = QListWidget()
        self.list.setItemDelegate(FileDelegate(self.list))
        self.list.setUniformItemSizes(True)
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.currentRowChanged.connect(self._open)
        sl.addWidget(self.list, 1)
        row = QHBoxLayout()
        for text, fn in (("Open externally", self._external), ("Open folder", self._folder)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        sl.addLayout(row)
        for text, fn in (("Convert Selected to PDF", self.convert_to_pdf), ("Delete Selected", self.delete_selected)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            sl.addWidget(b)
        split.addWidget(side)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        self.title = QLabel("Select a file.")
        self.title.setStyleSheet("font-weight: 600;")
        bar.addWidget(self.title, 1)
        self.prev_btn, self.next_btn = QPushButton("Prev page"), QPushButton("Next page")
        self.page_lbl = QLabel("")
        self.prev_btn.clicked.connect(lambda: self._page(self.page - 1))
        self.next_btn.clicked.connect(lambda: self._page(self.page + 1))
        for w in (self.prev_btn, self.page_lbl, self.next_btn):
            bar.addWidget(w)
        for text, fn in (("Fit", lambda: self.view.fit()), ("Zoom in", lambda: self.view.zoom(1.25)),
                         ("Zoom out", lambda: self.view.zoom(0.8))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            bar.addWidget(b)
        rl.addLayout(bar)
        body = QHBoxLayout()
        self.thumbs = QListWidget()
        self.thumbs.setViewMode(QListWidget.ListMode)   # one page per row, top to bottom
        self.thumbs.setSpacing(4)
        self.thumbs.setIconSize(QSize(118, 100))
        self.thumbs.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.thumbs.setFixedWidth(140)
        self.thumbs.setStyleSheet("background: #1e2124; color: #cccccc;")
        self.thumbs.currentRowChanged.connect(lambda i: i >= 0 and i != self.page and self._page(i))
        self.thumbs.hide()
        body.addWidget(self.thumbs)
        self.view = View()
        body.addWidget(self.view, 1)
        rl.addLayout(body, 1)
        split.addWidget(right)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([330, 900])
        lay.addWidget(split)
        self._pdf_buttons()
        self.reload()

    # ---------------------------------------------------------------- list
    def reload(self):
        self.files = images.list_files(self.base, self.folder.currentText(), self.sort.currentText().lower())
        self._fill()

    def _fill(self):
        q = self.search.text().lower()
        self.paths = [(p, t) for p, t in self.files if q in os.path.basename(p).lower()]
        self.list.blockSignals(True)
        self.list.clear()
        for p, t in self.paths:
            it = QListWidgetItem()
            it.setData(Qt.UserRole, (short_name(os.path.basename(p)),
                                     f"{os.path.basename(os.path.dirname(p))}  \u00b7  {datetime.fromtimestamp(t):%Y-%m-%d %H:%M}"))
            it.setToolTip(p)
            self.list.addItem(it)
        self.list.blockSignals(False)

    def _current(self):
        i = self.list.currentRow()
        return self.paths[i][0] if 0 <= i < len(self.paths) else None

    # ---------------------------------------------------------------- view
    def _open(self, i):
        path = self._current()
        if not path:
            return
        self._close_pdf()
        self.title.setText(os.path.basename(path))
        if path.lower().endswith(".pdf"):
            self.doc = images.fitz.open(path)
            self._page(0)
            self._thumbs()
        else:
            self.view.show_pixmap(QPixmap(path))
        self._pdf_buttons()

    def _close_pdf(self):
        if self.doc is not None:
            self.doc.close()
        self.doc, self.page, self._job = None, 0, None
        self.thumbs.clear()
        self.thumbs.hide()

    def _page(self, n):
        if self.doc is None or not 0 <= n < self.doc.page_count:
            return
        self.page = n
        pm = QPixmap()
        pm.loadFromData(images.pdf_page_png(self.doc, n, 2.0))
        self.view.show_pixmap(pm)
        self.thumbs.blockSignals(True)
        self.thumbs.setCurrentRow(n)
        self.thumbs.blockSignals(False)
        self._pdf_buttons()

    def _pdf_buttons(self):
        on = self.doc is not None
        for w in (self.prev_btn, self.next_btn, self.page_lbl):
            w.setVisible(on)
        if on:
            self.page_lbl.setText(f"{self.page + 1} / {self.doc.page_count}")
            self.prev_btn.setEnabled(self.page > 0)
            self.next_btn.setEnabled(self.page < self.doc.page_count - 1)

    def _thumbs(self):
        """Page thumbnails rendered in a thread (own document handle) and added as they arrive."""
        token = object()
        self._job = token
        path, n = self.doc.name, self.doc.page_count
        self.thumbs.show()
        for i in range(n):
            it = QListWidgetItem()
            it.setToolTip(f"Page {i + 1}")
            it.setSizeHint(QSize(122, 106))
            self.thumbs.addItem(it)

        def work():
            d = images.fitz.open(path)
            try:
                for i in range(n):
                    if self._job is not token:
                        return
                    self.bus.thumb.emit(token, i, images.pdf_page_png(d, i, 0.25))
            finally:
                d.close()
        threading.Thread(target=work, daemon=True).start()

    def _add_thumb(self, token, i, png):
        if token is not self._job or i >= self.thumbs.count():
            return
        pm = QPixmap()
        pm.loadFromData(png)
        self.thumbs.item(i).setIcon(QIcon(pm))

    # ---------------------------------------------------------------- external
    def _external(self):
        p = self._current()
        if p:
            subprocess.Popen(["xdg-open", p], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _folder(self):
        p = self._current()
        target = os.path.dirname(p) if p else self.base
        if target:
            subprocess.Popen(["xdg-open", target], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # ---------------------------------------------------------------- convert / delete
    def _selected(self):
        rows = sorted(self.list.row(it) for it in self.list.selectedItems())
        return [self.paths[r][0] for r in rows if 0 <= r < len(self.paths)]

    def convert_to_pdf(self):
        paths = self._selected()
        if not paths:
            QMessageBox.warning(self, "Warning", "Please select images from the list first.")
            return
        files = [p for p in paths if not p.lower().endswith(".pdf")]
        if not files:
            QMessageBox.warning(self, "Warning", "No images selected (PDFs are excluded from merge).")
            return
        start = os.path.join(os.path.dirname(files[0]), file_ops.pdf_default_name())
        save, _f = QFileDialog.getSaveFileName(self, "Save PDF", start, "PDF files (*.pdf)")
        if not save:
            return
        if not os.path.splitext(save)[1]:
            save += ".pdf"
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            file_ops.images_to_pdf(files, save)
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Error", f"Failed to create PDF:\n{e}")
            return
        QApplication.restoreOverrideCursor()
        QMessageBox.information(self, "Success", f"PDF created successfully:\n{os.path.basename(save)}")

    def delete_selected(self):
        paths = self._selected()
        if not paths:
            return
        if QMessageBox.question(self, "Confirm", f"Are you sure you want to delete {len(paths)} files?") != QMessageBox.Yes:
            return
        if self.doc is not None and self.doc.name in paths:
            self._close_pdf()
            self._pdf_buttons()
        self.title.setText(f"Deleting {len(paths)} file(s)...")
        threading.Thread(target=lambda: self.bus.deleted.emit(file_ops.delete_images(paths)), daemon=True).start()

    def _on_deleted(self, _failed):
        self._close_pdf()
        self._pdf_buttons()
        self.view.orig = None
        self.view.item.setPixmap(QPixmap())
        self.title.setText("Select a file.")
        self.reload()

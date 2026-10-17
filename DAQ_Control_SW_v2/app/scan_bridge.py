"""Qt side of the scan host (core.scan): a GUI-thread dispatcher and the dialogs the scan
engine asks for. Every dialog may be called from any thread; a call from a worker thread
runs on the GUI thread and the worker waits for the answer."""
import re
import threading

from PyQt5.QtCore import QObject, Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtWidgets import (QApplication, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QMessageBox,
                             QProgressBar, QPushButton, QVBoxLayout)

from app import theme


class _Relay(QObject):
    run = pyqtSignal(object, int)

    def __init__(self):
        super().__init__()
        self.run.connect(self._run, Qt.QueuedConnection)

    @staticmethod
    def _run(fn, ms):
        QTimer.singleShot(max(0, ms), fn)


_relay = None


def dispatch(fn, ms=0):
    """Run fn on the GUI thread after ms milliseconds (Tk's master.after)."""
    _relay.run.emit(fn, int(ms))


_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]")


def plain(text):
    """The scan engine's texts are shared with the Tk app; v2 shows them without emoji."""
    return _EMOJI.sub("", str(text)).replace("  ", " ").strip() if text else text


def _parent():
    app = QApplication.instance()
    return getattr(app, "_main_window", None) or QApplication.activeWindow()


def _in_gui(fn):
    """Call fn on the GUI thread and return its result (blocks a worker thread until done)."""
    if QThread.currentThread() == QApplication.instance().thread():
        return fn()
    box, done = {}, threading.Event()

    def run():
        try:
            box["v"] = fn()
        finally:
            done.set()
    dispatch(run)
    done.wait()
    return box.get("v")


class QtDialogs:
    """tkinter.messagebox-compatible calls plus the scan engine's custom windows."""

    def showinfo(self, title, message, **_kw):
        _in_gui(lambda: QMessageBox.information(_parent(), plain(title), plain(message)))

    def showwarning(self, title, message, **_kw):
        _in_gui(lambda: QMessageBox.warning(_parent(), plain(title), plain(message)))

    def showerror(self, title, message, **_kw):
        _in_gui(lambda: QMessageBox.critical(_parent(), plain(title), plain(message)))

    def askyesno(self, title, message, **_kw):
        return _in_gui(lambda: QMessageBox.question(_parent(), plain(title), plain(message)) == QMessageBox.Yes)

    def askokcancel(self, title, message, **_kw):
        return _in_gui(lambda: QMessageBox.question(_parent(), plain(title), plain(message),
                                                    QMessageBox.Ok | QMessageBox.Cancel) == QMessageBox.Ok)

    def askyesnocancel(self, title, message, **_kw):
        title, message = plain(title), plain(message)

        def ask():
            r = QMessageBox.question(_parent(), title, message,
                                     QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel, QMessageBox.Cancel)
            return True if r == QMessageBox.Yes else False if r == QMessageBox.No else None
        return _in_gui(ask)

    def wide_confirm(self, title, header, rows, question, accent="#0a84ff"):
        """Pre-flight checklist: an aligned label/value table and a Yes/No question."""
        title, header, question = plain(title), plain(header), plain(question)
        rows = [(plain(a) if a is not None else None, plain(b)) for a, b in rows]

        def ask():
            dlg = QDialog(_parent())
            dlg.setWindowTitle(title)
            dlg.setMinimumWidth(720)
            dlg.setObjectName("preflight")
            dlg.setStyleSheet(f"QDialog#preflight {{ border-top: 6px solid {accent}; }}")   # CAEN blue / HK purple
            v = QVBoxLayout(dlg)
            h = QLabel(header)
            h.setStyleSheet(f"font-size: 14pt; font-weight: 700; color: {accent};")
            v.addWidget(h)
            g = QGridLayout()
            g.setHorizontalSpacing(16)
            for r, (label, value) in enumerate(rows):
                if label is None and str(value).strip("-") == "":   # "---" is a rule, as in the Tk dialog
                    rule = QFrame()
                    rule.setFrameShape(QFrame.HLine)
                    rule.setStyleSheet(f"color: {theme.C['border']};")
                    g.addWidget(rule, r, 0, 1, 2)
                    continue
                if label is None:
                    line = QLabel(str(value))
                    line.setWordWrap(True)
                    g.addWidget(line, r, 0, 1, 2)
                    continue
                k = QLabel(str(label))
                k.setObjectName("key")
                val = QLabel(str(value))
                val.setWordWrap(True)
                val.setTextInteractionFlags(Qt.TextSelectableByMouse)
                if str(label).upper().startswith("CHECK"):
                    val.setStyleSheet(f"color: {theme.C['bad']}; font-weight: 600;")
                g.addWidget(k, r, 0, Qt.AlignTop)
                g.addWidget(val, r, 1)
            g.setColumnStretch(1, 1)
            v.addLayout(g)
            q = QLabel(question)
            q.setStyleSheet("font-weight: 600; margin-top: 10px;")
            v.addWidget(q)
            row = QHBoxLayout()
            row.addStretch(1)
            no, yes = QPushButton("No"), QPushButton("Yes")
            no.clicked.connect(dlg.reject)
            yes.clicked.connect(dlg.accept)
            yes.setStyleSheet(f"QPushButton {{ background: {accent}; color: white; border: 1px solid {accent}; }}"
                              "QPushButton:pressed { padding-top: 6px; }")
            row.addWidget(no)
            row.addWidget(yes)
            v.addLayout(row)
            return dlg.exec_() == QDialog.Accepted
        return _in_gui(ask)

    def scan_summary(self, compact, error_block, on_next):
        """Non-modal: an unattended overnight queue must not wait on it."""
        compact, error_block = plain(compact), plain(error_block)

        def show():
            dlg = QDialog(_parent())
            dlg.setWindowTitle("Scan Completed")
            dlg.setAttribute(Qt.WA_DeleteOnClose)
            v = QVBoxLayout(dlg)
            t = QLabel(compact)
            t.setStyleSheet(f'font-family: "{theme.MONO_FONT}";')
            v.addWidget(t)
            details = None
            if error_block:
                details = QLabel(error_block)
                details.setStyleSheet(f'font-family: "{theme.MONO_FONT}"; color: {theme.C["bad"]};')
                details.setVisible(False)
                v.addWidget(details)
            row = QHBoxLayout()
            if details is not None:
                tog = QPushButton(f"View details ({error_block.count(chr(10)) + 1})")
                tog.clicked.connect(lambda: details.setVisible(not details.isVisible()))
                row.addWidget(tog)
            row.addStretch(1)
            no, yes = QPushButton("No"), QPushButton("Yes")
            no.clicked.connect(dlg.close)
            yes.clicked.connect(lambda: (on_next(), dlg.close()))
            row.addWidget(no)
            row.addWidget(yes)
            v.addLayout(row)
            dlg.show()
        dispatch(show)

    def progress(self, title, header, cancel_text, on_cancel):
        """Indeterminate progress window; returns an object with thread-safe set_status / close."""
        holder = {}
        title, header = plain(title), plain(header)

        def build():
            dlg = QDialog(_parent())
            dlg.setWindowTitle(title)
            dlg.setWindowFlag(Qt.WindowStaysOnTopHint)
            v = QVBoxLayout(dlg)
            h = QLabel(header)
            h.setStyleSheet("font-size: 13pt; font-weight: 700;")
            v.addWidget(h)
            st = QLabel("Starting...")
            st.setStyleSheet(f"color: {theme.C['accent']};")
            v.addWidget(st)
            bar = QProgressBar()
            bar.setRange(0, 0)
            v.addWidget(bar)
            b = QPushButton(cancel_text)
            b.clicked.connect(on_cancel)
            v.addWidget(b)
            dlg.rejected.connect(on_cancel)
            dlg.show()
            holder.update(dlg=dlg, st=st)
        _in_gui(build)

        class _P:
            @staticmethod
            def set_status(text):
                dispatch(lambda: holder["st"].setText(plain(text)))

            @staticmethod
            def close():
                def c():
                    holder["dlg"].rejected.disconnect()
                    holder["dlg"].close()
                dispatch(c)
        return _P()


def init():
    """Create the dispatcher and the scan host (call once on the GUI thread)."""
    global _relay
    if _relay is None:
        _relay = _Relay()
    from core import scan
    return scan.host(dispatch, QtDialogs())

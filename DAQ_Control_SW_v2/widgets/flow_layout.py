"""Row layout that wraps its items onto more lines when narrow and spreads them out when wide."""
from PyQt5.QtCore import QPoint, QRect, QSize, Qt
from PyQt5.QtWidgets import QLayout


class FlowLayout(QLayout):
    def __init__(self, parent=None, hspace=18, vspace=4):
        super().__init__(parent)
        self.setContentsMargins(0, 0, 0, 0)
        self._items, self.hspace, self.vspace = [], hspace, vspace
        self.justify = True   # False: items stay packed to the left

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        return self._place(QRect(0, 0, w, 0), False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._place(rect, True)

    def sizeHint(self):
        w = sum(i.sizeHint().width() for i in self._shown()) + self.hspace * max(0, len(self._shown()) - 1)
        return QSize(w, max((i.sizeHint().height() for i in self._shown()), default=0))

    def minimumSize(self):
        s = QSize()
        for i in self._shown():
            s = s.expandedTo(i.minimumSize())
        return s

    def _shown(self):
        return [i for i in self._items if not (i.widget() and i.widget().isHidden())]

    def _place(self, rect, apply):
        lines, line, used = [], [], 0
        for it in self._shown():
            w = it.sizeHint().width()
            if line and used + self.hspace + w > rect.width():
                lines.append(line)
                line, used = [], 0
            used += (self.hspace if line else 0) + w
            line.append(it)
        if line:
            lines.append(line)
        y = rect.y()
        for line in lines:
            h = max(it.sizeHint().height() for it in line)
            if apply:
                widths = [min(it.sizeHint().width(), rect.width()) for it in line]
                spare = rect.width() - sum(widths) - self.hspace * (len(line) - 1)
                gap = self.hspace + (spare / (len(line) - 1) if len(line) > 1 and self.justify else 0)
                x = rect.x() + (spare if len(line) == 1 and line[0].widget() is not None
                                and line[0].widget().property("flow_right") else 0)
                for it, w in zip(line, widths):
                    it.setGeometry(QRect(QPoint(int(x), y + (h - it.sizeHint().height()) // 2),
                                         QSize(w, it.sizeHint().height())))
                    x += w + gap
            y += h + self.vspace
        return y - rect.y() - (self.vspace if lines else 0)

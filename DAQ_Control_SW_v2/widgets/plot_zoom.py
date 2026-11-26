"""Box zoom shared by the plot cards: drag a box to zoom, right-click or Reset View to go back."""
import numpy as np
from matplotlib.patches import Rectangle


class PlotZoom:
    def __init__(self, canvas, on_clear=lambda: None, on_zoom=lambda: None, on_home=lambda: None, min_span=0.0,
                 skip=lambda ev: False):
        self.canvas, self.home, self.drag, self.rect, self.skip = canvas, {}, None, None, skip
        self.on_clear, self.on_zoom, self.on_home, self.min_span = on_clear, on_zoom, on_home, min_span
        self.picked = False   # this click hit a marker (set by the page's pick handler)
        canvas.mpl_connect("button_press_event", self._press)
        canvas.mpl_connect("motion_notify_event", self._motion)
        canvas.mpl_connect("button_release_event", self._release)

    def set_home(self):
        self.home = {ax: (ax.get_xlim(), ax.get_ylim()) for ax in self.canvas.figure.get_axes()}

    def reset(self):
        """Back to the axis ranges of the last drawing."""
        self.on_clear()
        self.on_home()
        for ax, (xl, yl) in self.home.items():
            ax.set_xlim(*xl)
            ax.set_ylim(*yl)
        self.canvas.draw_idle()

    def autoscale(self):
        """Y fitted to the visible points inside the current X range (the X zoom is kept)."""
        for ax in self.home:
            self.fit_y(ax, [ln for ln in ax.get_lines() if ln.get_visible()], draw=False)
        self.on_zoom()
        self.canvas.draw_idle()

    def fit_y(self, ax, lines, draw=True):
        """Y fitted to the points of these lines inside the current X range."""
        lo, hi = ax.get_xlim()
        ys = []
        for ln in lines:
            xy = np.asarray(ln.get_xydata(), float)   # unit-converted, so time axes work too
            if not len(xy):
                continue
            x, y = xy[:, 0], xy[:, 1]
            ys.extend(y[(x >= lo) & (x <= hi) & np.isfinite(y)])
        if ys:
            y0, y1 = min(ys), max(ys)
            pad = max((y1 - y0) * 0.12, abs(y1) * 0.02, 1e-9)
            half = max((y1 - y0) / 2 + pad, self.min_span * abs(float(np.median(ys))))
            ax.set_ylim((y0 + y1) / 2 - half, (y0 + y1) / 2 + half)
        if draw:
            self.on_zoom()
            self.canvas.draw_idle()

    def _on_legend(self, ev):
        leg = ev.inaxes.get_legend()
        return leg is not None and leg.get_draggable() and leg.get_window_extent().contains(ev.x, ev.y)

    def _press(self, ev):
        if self.skip(ev) or ev.inaxes not in self.home or self._on_legend(ev):
            return
        if ev.button == 3:
            self.reset()
            return
        if ev.button != 1 or ev.xdata is None:
            return
        self.drag = (ev.inaxes, ev.xdata, ev.ydata)
        self.rect = Rectangle((ev.xdata, ev.ydata), 0, 0, linewidth=1.2, edgecolor="#2563eb",
                              facecolor="#2563eb", alpha=0.15, zorder=10)
        ev.inaxes.add_patch(self.rect)

    def _motion(self, ev):
        if self.drag is None or self.rect is None or ev.inaxes is not self.drag[0] or ev.xdata is None:
            return
        _ax, x0, y0 = self.drag
        self.rect.set_xy((min(x0, ev.xdata), min(y0, ev.ydata)))
        self.rect.set_width(abs(ev.xdata - x0))
        self.rect.set_height(abs(ev.ydata - y0))
        self.canvas.draw_idle()

    def _release(self, ev):
        if self.rect is not None:
            try:
                self.rect.remove()
            except Exception:
                pass
            self.rect = None
        drag, self.drag = self.drag, None
        picked, self.picked = self.picked, False
        if drag is None or ev.button != 1 or ev.inaxes is not drag[0] or ev.xdata is None or ev.ydata is None:
            self.canvas.draw_idle()
            return
        ax, x0, y0 = drag
        xlo, xhi = ax.get_xlim()
        ylo, yhi = ax.get_ylim()
        zx, zy = abs(ev.xdata - x0) > 0.01 * (xhi - xlo), abs(ev.ydata - y0) > 0.01 * (yhi - ylo)
        if zx or zy or not picked:   # a zoom or a click on empty space closes the popup
            self.on_clear()
        if zx:
            ax.set_xlim(*sorted([x0, ev.xdata]))
        if zy:
            ax.set_ylim(*sorted([y0, ev.ydata]))
        if zx or zy:
            self.on_zoom()
        self.canvas.draw_idle()

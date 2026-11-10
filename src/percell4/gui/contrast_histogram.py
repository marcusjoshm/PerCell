"""Histogram contrast control for the viewer's image layers.

A dock widget, like ImageJ's Brightness/Contrast: the histogram of what the
selected image layer currently shows, with a draggable band whose edges are
the layer's contrast limits. Dragging the band sets the limits; changing
them anywhere else (napari's slider, auto-contrast) moves the band. The
count axis can be logarithmic, since microscopy histograms are mostly
background. **Auto** sets the limits to the 0.1-99.9 percentiles of what is
shown; **Reset** to the layer's full range.

The histogram follows the Z/T sliders. It reads napari's displayed slice,
which napari has already loaded (for a lazy z-series too), so it never
reads the file itself; large slices are subsampled with a stride.
"""

from __future__ import annotations

import numpy as np
from qtpy.QtCore import QTimer, Signal
from qtpy.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

#: Histogram bin count.
BINS = 256
#: Pixels sampled at most when computing a histogram (a strided subsample).
MAX_PIXELS = 2_000_000
#: Percentiles the Auto button sets the limits to.
AUTO_PERCENTILES = (0.1, 99.9)


def displayed_data(layer) -> np.ndarray | None:
    """The slice the image layer shows now (2D, or the volume in 3D), as
    numpy; ``None`` when unavailable. napari has already loaded it."""
    view = getattr(layer, "_data_view", None)
    return None if view is None else np.asarray(view)


def histogram_of(
    data: np.ndarray, value_range: tuple[float, float], bins: int = BINS
) -> tuple[np.ndarray, np.ndarray]:
    """``(counts, edges)`` of the finite values of ``data`` over ``value_range``.

    Large arrays are subsampled with a stride so the result stays cheap.
    """
    flat = np.asarray(data).ravel()
    if flat.size > MAX_PIXELS:
        flat = flat[:: int(np.ceil(flat.size / MAX_PIXELS))]
    flat = flat[np.isfinite(flat)]
    low, high = float(value_range[0]), float(value_range[1])
    if high <= low:
        high = low + 1.0
    return np.histogram(flat, bins=bins, range=(low, high))


def auto_limits(data: np.ndarray) -> tuple[float, float] | None:
    """The :data:`AUTO_PERCENTILES` of the finite values, or None when empty."""
    flat = np.asarray(data).ravel()
    if flat.size > MAX_PIXELS:
        flat = flat[:: int(np.ceil(flat.size / MAX_PIXELS))]
    flat = flat[np.isfinite(flat)]
    if flat.size == 0:
        return None
    low, high = np.percentile(flat, AUTO_PERCENTILES)
    return (float(low), float(high)) if high > low else (float(low), float(low) + 1.0)


class ContrastHistogram(QWidget):
    """Histogram with draggable contrast limits for the selected image layer."""

    def __init__(self, viewer, parent: QWidget | None = None) -> None:
        import pyqtgraph as pg

        super().__init__(parent)
        self._viewer = viewer
        self._layer = None
        self._syncing = False
        self._data: np.ndarray | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        self._title = QLabel("Select an image layer")
        layout.addWidget(self._title)

        self._plot = pg.PlotWidget()
        self._plot.setMinimumHeight(140)
        self._plot.setMouseEnabled(x=False, y=False)
        self._plot.hideButtons()
        self._plot.getPlotItem().hideAxis("left")
        self._plot.setMenuEnabled(False)
        self._curve = self._plot.plot(
            [0, 1], [0], stepMode="center", fillLevel=0, brush=(170, 170, 170, 120),
            pen=pg.mkPen((200, 200, 200)),
        )
        self._region = pg.LinearRegionItem(
            values=(0, 1), orientation="vertical", brush=(80, 140, 255, 40)
        )
        self._region.sigRegionChanged.connect(self._on_region_dragged)
        self._plot.addItem(self._region)
        layout.addWidget(self._plot)

        self._limits_label = QLabel("")
        layout.addWidget(self._limits_label)

        row = QHBoxLayout()
        self.log_check = QCheckBox("Log counts")
        self.log_check.setChecked(True)
        self.log_check.setToolTip("Show counts on a log scale (background dominates).")
        self.log_check.toggled.connect(lambda _on: self._draw())
        row.addWidget(self.log_check)
        row.addStretch()
        self.auto_button = QPushButton("Auto")
        self.auto_button.setToolTip(
            "Set the limits to the 0.1-99.9 percentiles of what the layer shows."
        )
        self.auto_button.clicked.connect(self.apply_auto)
        row.addWidget(self.auto_button)
        self.reset_button = QPushButton("Reset")
        self.reset_button.setToolTip("Set the limits to the layer's full range.")
        self.reset_button.clicked.connect(self.reset_limits)
        row.addWidget(self.reset_button)
        layout.addLayout(row)

        # Slider moves arrive in bursts; recompute once they settle.
        self._recompute_timer = QTimer(self)
        self._recompute_timer.setSingleShot(True)
        self._recompute_timer.setInterval(60)
        self._recompute_timer.timeout.connect(self.recompute)

        viewer.layers.selection.events.active.connect(self._on_active_changed)
        viewer.dims.events.current_step.connect(self._schedule_recompute)
        viewer.dims.events.ndisplay.connect(self._schedule_recompute)
        self._bind(viewer.layers.selection.active)

    # -- binding -------------------------------------------------------------

    @property
    def layer(self):
        return self._layer

    def _on_active_changed(self, _event=None) -> None:
        self._bind(self._viewer.layers.selection.active)

    def _bind(self, layer) -> None:
        import napari

        if self._layer is not None:
            try:
                self._layer.events.contrast_limits.disconnect(self._on_layer_limits)
                self._layer.events.set_data.disconnect(self._schedule_recompute)
            except (TypeError, ValueError, RuntimeError):
                pass
        self._layer = layer if isinstance(layer, napari.layers.Image) else None
        enabled = self._layer is not None
        for widget in (self._plot, self.auto_button, self.reset_button, self.log_check):
            widget.setEnabled(enabled)
        if not enabled:
            self._title.setText("Select an image layer")
            self._limits_label.setText("")
            self._data = None
            self._curve.setData([0, 1], [0])
            return
        self._title.setText(layer.name)
        layer.events.contrast_limits.connect(self._on_layer_limits)
        layer.events.set_data.connect(self._schedule_recompute)
        self.recompute()

    # -- histogram -----------------------------------------------------------

    def _schedule_recompute(self, _event=None) -> None:
        if self._layer is not None:
            self._recompute_timer.start()

    def recompute(self) -> None:
        """Re-read what the layer shows and redraw the histogram."""
        if self._layer is None:
            return
        self._data = displayed_data(self._layer)
        self._draw()

    def _draw(self) -> None:
        layer = self._layer
        if layer is None:
            return
        low, high = (float(x) for x in layer.contrast_limits_range)
        if self._data is not None and self._data.size:
            counts, edges = histogram_of(self._data, (low, high))
            counts = counts.astype(float)
            if self.log_check.isChecked():
                counts = np.log10(counts + 1.0)
            self._curve.setData(edges, counts)
        self._plot.setXRange(low, high, padding=0.02)
        self._region.setBounds((low, high))
        self._show_limits()

    def _show_limits(self) -> None:
        low, high = (float(x) for x in self._layer.contrast_limits)
        self._syncing = True
        try:
            self._region.setRegion((low, high))
        finally:
            self._syncing = False
        self._limits_label.setText(f"Limits: {low:.4g} - {high:.4g}")

    # -- limits --------------------------------------------------------------

    def _on_region_dragged(self) -> None:
        if self._syncing or self._layer is None:
            return
        low, high = sorted(float(x) for x in self._region.getRegion())
        if high <= low:
            return
        self._syncing = True
        try:
            self._layer.contrast_limits = (low, high)
        finally:
            self._syncing = False
        self._limits_label.setText(f"Limits: {low:.4g} - {high:.4g}")

    def _on_layer_limits(self, _event=None) -> None:
        if self._syncing or self._layer is None:
            return
        self._show_limits()

    def apply_auto(self) -> None:
        """Set the limits to the 0.1-99.9 percentiles of what the layer shows."""
        if self._layer is None:
            return
        if self._data is None:
            self.recompute()
        limits = auto_limits(self._data) if self._data is not None else None
        if limits is not None:
            self._layer.contrast_limits = limits

    def reset_limits(self) -> None:
        """Set the limits to the layer's full range."""
        if self._layer is not None:
            self._layer.contrast_limits = tuple(self._layer.contrast_limits_range)


class ContrastWindow(QWidget):
    """The histogram in its own free-floating window.

    Closing it only hides it, so reopening keeps its size and place.
    ``visibility_changed`` lets a toggle button follow the window, also when
    the user closes it from its title bar.
    """

    visibility_changed = Signal(bool)

    def __init__(self, viewer) -> None:
        super().__init__(None)
        self.setWindowTitle("PerCell4 \u2014 Contrast")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.histogram = ContrastHistogram(viewer, self)
        layout.addWidget(self.histogram)
        self.resize(460, 280)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.visibility_changed.emit(True)

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self.visibility_changed.emit(False)

    def closeEvent(self, event) -> None:
        event.ignore()
        self.hide()

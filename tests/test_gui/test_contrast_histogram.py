"""The viewer's histogram contrast control."""

from __future__ import annotations

import numpy as np
import pytest

from percell4.gui.contrast_histogram import (
    ContrastHistogram,
    auto_limits,
    displayed_data,
    histogram_of,
)


@pytest.fixture
def viewer():
    from napari.components import ViewerModel

    return ViewerModel()


def _image(viewer, data, name="ch0"):
    layer = viewer.add_image(data, name=name)
    viewer.layers.selection.active = layer
    return layer


def test_histogram_counts_finite_values_in_range():
    data = np.array([0.0, 1.0, 1.0, 9.0, np.nan])
    counts, edges = histogram_of(data, (0.0, 10.0), bins=10)
    assert counts.sum() == 4
    assert counts[1] == 2 and edges[0] == 0.0 and edges[-1] == 10.0


def test_auto_limits_use_the_percentiles():
    data = np.arange(10_000, dtype=np.float32)
    low, high = auto_limits(data)
    assert low == pytest.approx(9.999, abs=0.01)
    assert high == pytest.approx(9989.0, abs=1.0)
    assert auto_limits(np.array([np.nan])) is None


def test_widget_follows_the_selected_image_layer(qtbot, viewer):
    first = _image(viewer, np.random.rand(20, 30).astype(np.float32), "ch0")
    widget = ContrastHistogram(viewer)
    qtbot.addWidget(widget)
    assert widget.layer is first
    second = _image(viewer, np.random.rand(20, 30).astype(np.float32), "ch1")
    assert widget.layer is second
    labels = viewer.add_labels(np.zeros((20, 30), np.int32))
    viewer.layers.selection.active = labels
    assert widget.layer is None
    assert not widget.auto_button.isEnabled()


def test_dragging_the_band_sets_the_layer_limits(qtbot, viewer):
    layer = _image(viewer, np.linspace(0, 100, 600, dtype=np.float32).reshape(20, 30))
    widget = ContrastHistogram(viewer)
    qtbot.addWidget(widget)
    widget._region.setRegion((10.0, 60.0))
    assert tuple(layer.contrast_limits) == pytest.approx((10.0, 60.0))


def test_limits_set_elsewhere_move_the_band(qtbot, viewer):
    layer = _image(viewer, np.linspace(0, 100, 600, dtype=np.float32).reshape(20, 30))
    widget = ContrastHistogram(viewer)
    qtbot.addWidget(widget)
    layer.contrast_limits = (20.0, 80.0)
    assert tuple(widget._region.getRegion()) == pytest.approx((20.0, 80.0))
    assert "20" in widget._limits_label.text()


def test_auto_and_reset(qtbot, viewer):
    data = np.zeros((100, 100), np.float32)
    data[0, 0] = 1000.0  # one hot pixel
    data[1:, :] = np.linspace(0, 50, 9900).reshape(99, 100)
    layer = _image(viewer, data)
    widget = ContrastHistogram(viewer)
    qtbot.addWidget(widget)
    widget.apply_auto()
    assert layer.contrast_limits[1] < 60  # the hot pixel no longer sets the top
    widget.reset_limits()
    assert tuple(layer.contrast_limits) == pytest.approx(tuple(layer.contrast_limits_range))


def test_histogram_follows_the_z_slider(qtbot, viewer):
    stack = np.stack([np.full((10, 10), z, np.float32) for z in range(5)])
    layer = _image(viewer, stack)
    widget = ContrastHistogram(viewer)
    qtbot.addWidget(widget)
    viewer.dims.set_current_step(0, 4)
    widget.recompute()
    assert np.all(widget._data == 4)
    assert displayed_data(layer).shape == (10, 10)


def test_lazy_volume_histogram_reads_no_extra_planes(viewer):
    """napari has loaded the displayed volume; the histogram reuses it."""
    import dask.array as da

    reads = []

    def block(block_info=None):
        reads.append(block_info[None]["chunk-location"][0])
        return np.ones((1, 4, 4), np.float32)

    stack = da.map_blocks(block, dtype=np.float32, chunks=((1,) * 9, (4,), (4,)))
    layer = viewer.add_image(stack, contrast_limits=(0, 2))
    viewer.dims.ndisplay = 3
    before = len(reads)
    data = displayed_data(layer)
    assert data.shape == (9, 4, 4)
    assert len(reads) == before


# ── free-floating window and its toggle ───────────────────────


def test_window_close_only_hides_and_reports_it(qtbot, viewer):
    from percell4.gui.contrast_histogram import ContrastWindow

    _image(viewer, np.random.rand(8, 8).astype(np.float32))
    window = ContrastWindow(viewer)
    qtbot.addWidget(window)
    seen = []
    window.visibility_changed.connect(seen.append)
    window.show()
    assert window.isWindow() and window.parent() is None
    window.close()
    assert not window.isVisible()
    assert seen == [True, False]
    assert window.histogram.layer is not None  # kept for reopening


def test_panel_button_opens_and_follows_the_window(qtbot, viewer):
    from qtpy.QtCore import QObject, Signal

    from percell4.gui.contrast_histogram import ContrastWindow
    from percell4.interfaces.gui.task_panels.viewer_panel import ViewerPanel

    class _FakeViewerWin(QObject):
        contrast_visibility_changed = Signal(bool)

        def __init__(self):
            super().__init__()
            self.window = ContrastWindow(viewer)
            self.window.visibility_changed.connect(self.contrast_visibility_changed.emit)

        def set_contrast_visible(self, visible):
            self.window.setVisible(visible)

    from percell4.model import CellDataModel

    fake = _FakeViewerWin()
    qtbot.addWidget(fake.window)
    panel = ViewerPanel(CellDataModel(), show_window=lambda _n: None,
                        get_viewer_window=lambda: fake)
    qtbot.addWidget(panel)

    panel._contrast_btn.setChecked(True)
    assert fake.window.isVisible()
    fake.window.close()  # the window's own close button
    assert not panel._contrast_btn.isChecked()
    panel._contrast_btn.setChecked(True)
    assert fake.window.isVisible()
    panel._contrast_btn.setChecked(False)
    assert not fake.window.isVisible()

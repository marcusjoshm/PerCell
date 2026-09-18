"""Showing the stored z-series in napari (z-stack plan U8, KTD5, KTD7).

Runs against a headless ``napari.components.ViewerModel``; rendering is
covered by the GL suite.
"""

from __future__ import annotations

import numpy as np
import pytest

from percell4.gui.viewer import LAYER_TYPE_MASK, LAYER_TYPE_SEGMENTATION, PERCELL_TYPE_KEY
from percell4.gui.zseries_view import ZSeriesView
from percell4.store import DatasetStore

H, W, Z = 8, 10, 4


class _Win:
    """The slice of ViewerWindow that ZSeriesView uses."""

    def __init__(self) -> None:
        from napari.components import ViewerModel

        self.viewer = ViewerModel()

    def add_image(self, data, name, **kwargs):
        return self.viewer.add_image(data, name=name, **kwargs)


def _plane(t, c, z):
    return np.full((H, W), 100 * t + 10 * c + z + 1, np.float32)


def _dataset(path, *, n_t=None, keep_max=True):
    store = DatasetStore(path)
    meta = {"channel_names": ["ch0", "ch1"], "n_channels": 2,
            "z_spacing_um": 0.125, "pixel_size_um": 0.041}
    if n_t:
        meta["n_timepoints"] = n_t
    store.create(metadata=meta)
    shape = (2, Z, H, W) if n_t is None else (n_t, 2, Z, H, W)
    with store.zseries_writer(shape, ["ch0", "ch1"]) as w:
        for t in range(n_t or 1):
            for c in range(2):
                for z in range(Z):
                    w.write_plane(t, c, z, _plane(t, c, z))
    if keep_max:
        if n_t is None:
            image = np.stack([_plane(0, c, Z - 1) for c in range(2)])
            dims = ["C", "H", "W"]
        else:
            image = np.stack([np.stack([_plane(t, c, Z - 1) for c in range(2)])
                              for t in range(n_t)])
            dims = ["T", "C", "H", "W"]
        store.write_projection("max", image, dims=dims)
    return store


def _populate(win, store, n_t=None):
    """What the launcher's populate does: projection images, a seg and a mask."""
    if store.list_projections():
        image = store.read_array("intensity")
        for c, name in enumerate(["ch0", "ch1"]):
            win.viewer.add_image(image[:, c] if n_t else image[c], name=name)
    labels = np.zeros((H, W), np.int32) if n_t is None else np.zeros((n_t, H, W), np.int32)
    labels[..., 1:4, 1:4] = 1
    win.viewer.add_labels(
        labels, name="cells", metadata={PERCELL_TYPE_KEY: LAYER_TYPE_SEGMENTATION}
    )
    win.viewer.add_labels((labels > 0).astype(np.uint8), name="m",
                          metadata={PERCELL_TYPE_KEY: LAYER_TYPE_MASK}, visible=False)


@pytest.fixture
def shown(tmp_path):
    store = _dataset(tmp_path / "d.h5")
    win = _Win()
    _populate(win, store)
    view = ZSeriesView(win)
    view.set_shown(True, store.path, 1)
    return win, view, store


def test_show_adds_one_scaled_3d_layer_per_channel(shown):
    win, view, _store = shown
    assert view.layer_names == ["ch0 (z-series)", "ch1 (z-series)"]
    layer = win.viewer.layers["ch0 (z-series)"]
    assert layer.data.shape == (Z, H, W)
    assert tuple(layer.scale) == pytest.approx((0.125 / 0.041, 1.0, 1.0))
    # Same XY world extent as the projection layer, so they line up.
    proj = win.viewer.layers["ch0"]
    np.testing.assert_allclose(layer.extent.world[:, -2:], proj.extent.world[:, -2:])
    np.testing.assert_array_equal(np.asarray(layer.data[2]), _plane(0, 0, 2))


def test_contrast_comes_from_the_max_projection(shown):
    win, _view, _store = shown
    # The max projection of ch1 is the constant brightest plane (10 + Z), so
    # the limits start there; a flat image gets a one-unit window.
    assert tuple(win.viewer.layers["ch1 (z-series)"].contrast_limits) == pytest.approx(
        (10 + Z, 11 + Z)
    )


def test_zseries_only_contrast_samples_three_planes(tmp_path, monkeypatch):
    store = _dataset(tmp_path / "z.h5", keep_max=False)
    win = _Win()
    reads = []
    original = DatasetStore.read_zseries_plane

    def spy(self, t, c, z, view_bin=1):
        reads.append((t, c, z))
        return original(self, t, c, z, view_bin=view_bin)

    monkeypatch.setattr(DatasetStore, "read_zseries_plane", spy)
    monkeypatch.setattr(DatasetStore, "read_array", lambda *a, **k: pytest.fail("full read"))
    ZSeriesView(win).set_shown(True, store.path, 1)
    contrast_reads = [r for r in reads if r[1] == 0 and r[2] in (0, Z // 2, Z - 1)]
    assert len(contrast_reads) >= 3
    # Nothing near a whole-stack read: contrast samples plus napari's current slice.
    assert len(reads) <= 2 * (3 + 2)
    limits = win.viewer.layers["ch0 (z-series)"].contrast_limits
    assert tuple(limits) == pytest.approx((1.0, float(Z)))


def test_pixel_binning_two_bins_each_plane(tmp_path):
    store = _dataset(tmp_path / "d.h5")
    win = _Win()
    ZSeriesView(win).set_shown(True, store.path, 2)
    layer = win.viewer.layers["ch0 (z-series)"]
    assert layer.data.shape == (Z, H // 2, W // 2)
    np.testing.assert_array_equal(np.asarray(layer.data[1]), np.full((H // 2, W // 2), 4 * 2.0))
    assert tuple(layer.scale) == pytest.approx((0.125 / 0.082, 1.0, 1.0))


def test_ae5_overlays_hide_then_show_through_z_then_restore(shown):
    """Covers AE5."""
    win, view, store = shown
    layers = win.viewer.layers
    assert not layers["cells"].visible and not layers["m"].visible

    view.set_overlay(True, store.path, 1)
    through = layers["cells (through Z)"]
    assert through.data.shape == (Z, H, W)
    assert not through.editable
    for z in range(Z):
        np.testing.assert_array_equal(through.data[z], layers["cells"].data)

    view.set_shown(False, store.path, 1)
    assert "cells (through Z)" not in layers and "ch0 (z-series)" not in layers
    assert layers["cells"].visible  # was visible before
    assert not layers["m"].visible  # was hidden before; stays hidden


def test_writes_succeed_while_the_zseries_is_shown(shown):
    win, _view, store = shown
    np.asarray(win.viewer.layers["ch0 (z-series)"].data[3])  # read a plane
    store.write_mask("saved", np.ones((H, W), np.uint8))
    store.write_projection("mean", np.zeros((2, H, W), np.float32), dims=["C", "H", "W"])
    assert "saved" in store.list_masks()
    assert store.list_projections() == ("max", "mean")


def test_time_lapse_hides_projection_images_and_keeps_t(tmp_path):
    store = _dataset(tmp_path / "t.h5", n_t=3)
    win = _Win()
    _populate(win, store, n_t=3)
    view = ZSeriesView(win)
    view.set_shown(True, store.path, 1)
    layer = win.viewer.layers["ch0 (z-series)"]
    assert layer.data.shape == (3, Z, H, W)
    assert tuple(layer.scale)[0] == 1.0
    assert not win.viewer.layers["ch0"].visible
    np.testing.assert_array_equal(np.asarray(layer.data[2, 1]), _plane(2, 0, 1))
    view.set_overlay(True, store.path, 1)
    assert win.viewer.layers["cells (through Z)"].data.shape == (3, Z, H, W)
    view.set_shown(False, store.path, 1)
    assert win.viewer.layers["ch0"].visible


def test_channel_name_lookups_never_find_a_zseries_layer(shown):
    win, view, store = shown
    view.set_overlay(True, store.path, 1)
    for name in ("ch0", "ch1", "cells", "m"):
        matches = [layer for layer in win.viewer.layers if layer.name == name]
        assert len(matches) == 1
        assert matches[0].metadata.get(PERCELL_TYPE_KEY) != "zseries"


def test_rebuild_reapplies_the_shown_state(shown):
    win, view, store = shown
    view.set_overlay(True, store.path, 1)
    win.viewer.layers.clear()  # a projection or bin change rebuilds the viewer
    _populate(win, store)
    view.forget_layers()
    view.apply(store.path, 1)
    assert "ch0 (z-series)" in win.viewer.layers
    assert "cells (through Z)" in win.viewer.layers
    assert not win.viewer.layers["cells"].visible


def test_unknown_calibration_renders_cube_voxels(tmp_path):
    store = _dataset(tmp_path / "d.h5")
    store.delete_metadata_key("z_spacing_um")
    win = _Win()
    ZSeriesView(win).set_shown(True, store.path, 1)
    assert tuple(win.viewer.layers["ch0 (z-series)"].scale) == (1.0, 1.0, 1.0)


# ── Viewer panel controls ─────────────────────────────────────


def _panel(qtbot, metadata):
    from percell4.application.session import Session
    from percell4.domain.dataset import DatasetHandle
    from percell4.interfaces.gui.task_panels.viewer_panel import ViewerPanel
    from percell4.model import CellDataModel

    session = Session()
    model = CellDataModel(session=session)
    calls = []
    panel = ViewerPanel(
        model, show_window=lambda _n: None, get_viewer_window=lambda: None,
        set_zseries_shown=lambda on: calls.append(("shown", on)),
        set_zseries_overlay=lambda on: calls.append(("overlay", on)),
    )
    qtbot.addWidget(panel)
    session.set_dataset(DatasetHandle(path="/tmp/d.h5", metadata=metadata))
    return panel, calls


def test_ae6_no_zseries_means_no_control(qtbot):
    """Covers AE6."""
    panel, _calls = _panel(qtbot, {"has_zseries": False})
    assert not panel._zseries_group.isVisibleTo(panel)


def test_controls_call_back_and_overlay_needs_a_layer(qtbot):
    panel, calls = _panel(qtbot, {"has_zseries": True, "segmentation_names": ["cells"]})
    assert panel._zseries_group.isVisibleTo(panel)
    assert not panel._overlay_check.isEnabled()  # z-series not shown yet
    panel._zseries_check.setChecked(True)
    assert panel._overlay_check.isEnabled()
    panel._overlay_check.setChecked(True)
    assert calls == [("shown", True), ("overlay", True)]


def test_overlay_is_greyed_out_without_segmentation_or_mask(qtbot):
    panel, _calls = _panel(qtbot, {"has_zseries": True})
    panel._zseries_check.setChecked(True)
    assert not panel._overlay_check.isEnabled()
    assert "no segmentation or mask" in panel._overlay_check.toolTip()


def test_deleting_a_channel_while_shown_rebuilds_the_layers(shown):
    """Review #12: layers bind a channel position, so they must be rebuilt."""
    win, view, store = shown
    store.delete_zseries_channel("ch0")
    view.rebuild(store.path, 1)
    names = [layer.name for layer in win.viewer.layers if "(z-series)" in layer.name]
    assert names == ["ch1 (z-series)"]
    np.testing.assert_array_equal(
        np.asarray(win.viewer.layers["ch1 (z-series)"].data[2]), _plane(0, 1, 2)
    )
    assert not win.viewer.layers["cells"].visible  # still hidden while shown


def test_deleting_every_zseries_channel_turns_the_view_off(shown):
    win, view, store = shown
    store.delete_zseries_channel("ch0")
    store.delete_zseries_channel("ch1")
    view.rebuild(store.path, 1)
    assert not view.shown
    assert not [layer for layer in win.viewer.layers if "(z-series)" in layer.name]
    assert win.viewer.layers["cells"].visible

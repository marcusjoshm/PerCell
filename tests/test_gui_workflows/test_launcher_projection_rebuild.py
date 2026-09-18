"""Switching the Session's projection re-routes every GUI read (plan U5, KTD4).

The launcher replaces its store, drops the repository's cached stores and
rebuilds the viewer the way a Pixel Binning change does. Viewer channel
layers keep their plain channel names, so tools that find their input layer
by channel name read the new projection (KTD5).
"""

from __future__ import annotations

import numpy as np
import pytest
from tests.test_gui_workflows.test_launcher_view_bin_rebuild import _StubViewer

H, W = 4, 6
VALUES = {"max": 10.0, "mean": 5.0, "sum": 40.0}


def _projection_dataset(path, names):
    from percell4.store import DatasetStore

    store = DatasetStore(path)
    store.create(metadata={"channel_names": ["ch00", "ch01"], "n_channels": 2})
    for name in names:
        value = VALUES[name]
        store.write_projection(
            name,
            np.stack([np.full((H, W), value, np.float32),
                      np.full((H, W), value + 1, np.float32)]),
            dims=["C", "H", "W"],
        )
    labels = np.zeros((H, W), np.int32)
    labels[:2, :2] = 1
    store.write_labels("cells", labels)
    store.write_mask("m", (labels > 0).astype(np.uint8))
    return store


@pytest.fixture
def launcher(qtbot, tmp_path):
    from percell4.adapters.hdf5_store import _build_handle_metadata
    from percell4.domain.dataset import DatasetHandle
    from percell4.interfaces.gui.main_window import LauncherWindow
    from percell4.model import CellDataModel
    from percell4.store import DatasetStore

    def build(names):
        path = tmp_path / "ds.h5"
        _projection_dataset(path, names)
        model = CellDataModel()
        win = LauncherWindow(model)
        qtbot.addWidget(win)
        stub = _StubViewer()
        win._windows["viewer"] = stub
        # What _load_h5_into_viewer does, minus showing a real napari window.
        store = DatasetStore(path)
        from percell4.domain.io.projections import pick_projection

        projection = pick_projection(store.list_projections())
        win._repo.set_projection(projection)
        win._current_store = DatasetStore(path, projection=projection)
        win._current_h5_path = str(path)
        model.session.set_dataset(
            DatasetHandle(path=path, metadata=_build_handle_metadata(store))
        )
        return win, stub, model.session

    return build


def _images(stub):
    return {name: float(np.asarray(data).flat[0]) for data, name in stub.image_calls}


def test_opening_mean_and_sum_reads_mean(launcher):
    win, _stub, session = launcher(["mean", "sum"])
    assert session.active_projection == "mean"
    assert win._current_store.projection == "mean"
    assert win._repo.projection == "mean"


def test_switching_rebuilds_the_viewer_once_with_the_new_projection(launcher):
    win, stub, session = launcher(["max", "mean"])
    session.set_active_channel("ch01")
    win._populate_viewer_from_store()
    assert _images(stub) == {"ch00": 10.0, "ch01": 11.0}
    stub.image_calls.clear()
    cleared = stub.cleared

    session.set_active_projection("mean")

    assert stub.cleared == cleared + 1
    # Plain channel names, so tools that look layers up by channel name see
    # the new projection (Cellpose input, grouped thresholding, ...).
    assert _images(stub) == {"ch00": 5.0, "ch01": 6.0}
    assert all(stub.originator_during_calls[-3:])
    assert session.active_channel == "ch01"
    assert (session.active_segmentation, session.active_mask) == ("cells", "m")


def test_store_and_repository_read_the_new_projection(launcher):
    win, _stub, session = launcher(["max", "sum"])
    handle = session.dataset
    before = win._repo.read_channel_images(handle)["ch00"]
    assert float(before[0, 0]) == 10.0

    session.set_active_projection("sum")

    assert float(win._current_store.read_channel("intensity", 0)[0, 0]) == 40.0
    assert float(win._repo.read_channel_images(handle)["ch00"][0, 0]) == 40.0
    assert float(win._repo.read_array(handle, "intensity")[1, 0, 0]) == 41.0


def test_binned_rebuild_reads_the_new_projection(launcher):
    win, stub, session = launcher(["max", "mean"])
    session.set_active_bin(2)
    stub.image_calls.clear()
    session.set_active_projection("mean")
    # 2x2 sum-bin of a constant 5.0 plane
    assert _images(stub)["ch00"] == 20.0


def test_zseries_only_dataset_asks_for_a_projection(qtbot, tmp_path):
    from percell4.domain.dataset import DatasetHandle
    from percell4.interfaces.gui.main_window import LauncherWindow
    from percell4.model import CellDataModel
    from percell4.store import DatasetStore

    path = tmp_path / "z.h5"
    store = DatasetStore(path)
    store.create(metadata={"channel_names": ["ch00"], "n_channels": 1})
    with store.zseries_writer((1, 2, H, W), ["ch00"]) as w:
        for z in range(2):
            w.write_plane(0, 0, z, np.ones((H, W), np.float32))
    model = CellDataModel()
    win = LauncherWindow(model)
    qtbot.addWidget(win)
    stub = _StubViewer()
    win._windows["viewer"] = stub
    win._current_store = DatasetStore(path)
    win._current_h5_path = str(path)
    model.session.set_dataset(DatasetHandle(path=path, metadata={"projection_names": []}))

    win._populate_viewer_from_store()

    assert stub.image_calls == []
    assert "add a projection" in win.statusBar().currentMessage()


class _ModelViewerWin:
    """A viewer window stub backed by a headless napari ViewerModel."""

    def __init__(self) -> None:
        from napari.components import ViewerModel

        self.viewer = ViewerModel()
        self._viewer = self.viewer
        self.existing_viewer = None
        self._is_originator = False

    def _is_alive(self) -> bool:
        return True

    def clear(self) -> None:
        self.viewer.layers.clear()

    def add_image(self, data, name, **kwargs):
        return self.viewer.add_image(data, name=name, **kwargs)

    def add_labels(self, data, name, **kwargs):
        from percell4.gui.viewer import LAYER_TYPE_SEGMENTATION, PERCELL_TYPE_KEY

        return self.viewer.add_labels(
            data, name=name, metadata={PERCELL_TYPE_KEY: LAYER_TYPE_SEGMENTATION}
        )

    def add_mask(self, data, name, **kwargs):
        from percell4.gui.viewer import LAYER_TYPE_MASK, PERCELL_TYPE_KEY

        return self.viewer.add_labels(
            data, name=name, metadata={PERCELL_TYPE_KEY: LAYER_TYPE_MASK}, **kwargs
        )

    def _push_active_layer_to_napari(self, name, percell_type) -> None:
        pass

    def close(self) -> None:
        pass


def test_projection_change_keeps_the_zseries_shown(launcher):
    win, _stub, session = launcher(["max", "mean"])
    store = win._current_store
    with store.zseries_writer((2, 3, H, W), ["ch00", "ch01"]) as writer:
        for c in range(2):
            for z in range(3):
                writer.write_plane(0, c, z, np.full((H, W), z, np.float32))
    viewer_win = _ModelViewerWin()
    win._windows["viewer"] = viewer_win
    session.set_active_bin(2)  # rebuilds the viewer at bin 2
    win._set_zseries_shown(True)
    win._set_zseries_overlay(True)
    assert "ch00 (z-series)" in viewer_win.viewer.layers

    session.set_active_projection("mean")

    layers = viewer_win.viewer.layers
    assert "ch00 (z-series)" in layers and "cells (through Z)" in layers
    assert not layers["cells"].visible
    assert float(np.asarray(layers["ch00"].data).flat[0]) == 20.0  # mean, binned 2x2

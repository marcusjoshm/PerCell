"""Segmentations and masks record the channel they were made from (U7, R10)."""

from __future__ import annotations

import numpy as np
import pytest

from percell4.adapters.hdf5_store import Hdf5DatasetRepository
from percell4.application.session import Session
from percell4.store import DatasetStore

H = W = 16


def _dataset(path):
    store = DatasetStore(path)
    store.create(metadata={"channel_names": ["DAPI", "GFP"], "n_channels": 2})
    image = np.zeros((2, H, W), np.float32)
    image[1, 4:10, 4:10] = 100.0
    store.write_projection("max", image, dims=["C", "H", "W"])
    return store


def _session(repo, path):
    session = Session()
    session.set_dataset(repo.open(path))
    return session


def test_cellpose_segmentation_records_its_channel(tmp_path):
    from percell4.application.use_cases.segment_cells import SegmentCells

    path = tmp_path / "d.h5"
    _dataset(path)
    repo = Hdf5DatasetRepository()
    session = _session(repo, path)
    raw = np.zeros((H, W), np.int32)
    raw[4:10, 4:10] = 1
    result = SegmentCells(repo, session).finalize(
        raw, name="cp", remove_edge_cells=False, source_channel="GFP"
    )
    assert DatasetStore(path).source_channel("labels", result.seg_name) == "GFP"


def test_workflow_segmentation_records_its_channel(tmp_path, monkeypatch):
    from percell4.workflows import phases
    from percell4.workflows.models import CellposeSettings

    path = tmp_path / "d.h5"
    store = _dataset(path)
    fake = np.zeros((H, W), np.int32)
    fake[4:10, 4:10] = 1
    monkeypatch.setattr(phases, "run_cellpose", lambda *a, **k: fake)
    _labels, failure, _ = phases.segment_one(
        store, CellposeSettings(), channel_idx=1, seg_name="cellpose_qc"
    )
    assert failure is None
    assert store.source_channel("labels", "cellpose_qc") == "GFP"


def test_threshold_mask_records_its_channel(tmp_path):
    from percell4.adapters.null_viewer import NullViewerAdapter
    from percell4.application.use_cases.accept_threshold import AcceptThreshold

    path = tmp_path / "d.h5"
    _dataset(path)
    repo = Hdf5DatasetRepository()
    session = _session(repo, path)
    image = DatasetStore(path).read_channel("intensity", 1)
    AcceptThreshold(repo, NullViewerAdapter(), session).execute(image, 50.0, "otsu", "GFP")
    assert DatasetStore(path).source_channel("masks", "otsu_GFP") == "GFP"


def test_imported_mask_records_no_channel(tmp_path):
    path = tmp_path / "d.h5"
    store = _dataset(path)
    store.write_mask("imported", np.ones((H, W), np.uint8))
    assert store.source_channel("masks", "imported") is None


def test_zseries_only_segmentation_fails_with_the_reason(tmp_path):
    from percell4.workflows import phases
    from percell4.workflows.models import CellposeSettings

    store = DatasetStore(tmp_path / "z.h5")
    store.create(metadata={"channel_names": ["GFP"], "n_channels": 1})
    with store.zseries_writer((1, 2, H, W), ["GFP"]) as w:
        for z in range(2):
            w.write_plane(0, 0, z, np.ones((H, W), np.float32))
    _labels, failure, msg = phases.segment_one(store, CellposeSettings())
    assert failure is not None
    assert "add a projection first" in msg


@pytest.mark.parametrize("kind", ["labels", "masks"])
def test_source_channel_reads_none_for_a_missing_layer(tmp_path, kind):
    assert _dataset(tmp_path / "d.h5").source_channel(kind, "nope") is None

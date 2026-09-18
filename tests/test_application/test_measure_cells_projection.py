"""Measurements record their projection; legacy measurements are unchanged (U7, KTD9)."""

from __future__ import annotations

import numpy as np

from percell4.adapters.hdf5_store import Hdf5DatasetRepository
from percell4.application.session import Session
from percell4.application.use_cases.measure_cells import MeasureCells
from percell4.store import DatasetStore
from percell4.workflows.phases import measure_one

H = W = 12


def _labels():
    lab = np.zeros((H, W), dtype=np.int32)
    lab[2:6, 2:6] = 1
    lab[7:11, 7:11] = 2
    return lab


def _named(path):
    store = DatasetStore(path)
    store.create(metadata={"channel_names": ["GFP"], "n_channels": 1})
    store.write_projection("max", np.full((H, W), 9.0, np.float32), dims=["H", "W"])
    store.write_projection("mean", np.full((H, W), 3.0, np.float32), dims=["H", "W"])
    store.write_labels("cells", _labels(), attrs={"source_channel": "GFP"})
    return store


def _legacy(path):
    store = DatasetStore(path)
    store.create(metadata={"channel_names": ["GFP"], "n_channels": 1})
    store.write_array("intensity", np.full((H, W), 5.0, np.float32), attrs={"dims": ["H", "W"]})
    store.write_labels("cells", _labels())
    return store


def _measure_in_gui(path, projection):
    repo = Hdf5DatasetRepository(projection=projection)
    session = Session()
    session.set_dataset(repo.open(path))
    if projection is not None:
        session.set_active_projection(projection)
    session.set_active_segmentation("cells")
    return MeasureCells(repo, session).execute(metrics=["mean_intensity"])


def test_ae4_segmented_on_max_measured_on_mean(tmp_path):
    """Covers AE4."""
    path = tmp_path / "d.h5"
    _named(path)
    df = _measure_in_gui(path, "mean")
    assert (df["GFP_mean_intensity"] == 3.0).all()
    assert (df["projection"] == "mean").all()
    assert DatasetStore(path).source_channel("labels", "cells") == "GFP"


def test_measuring_max_then_mean_keeps_the_mean_run(tmp_path):
    path = tmp_path / "d.h5"
    _named(path)
    first = _measure_in_gui(path, "max")
    second = _measure_in_gui(path, "mean")
    assert (first["projection"] == "max").all() and (second["projection"] == "mean").all()
    stored = DatasetStore(path).read_dataframe("measurements")
    assert (stored["projection"] == "mean").all()


def test_legacy_measurement_has_exactly_todays_columns(tmp_path):
    path = tmp_path / "legacy.h5"
    _legacy(path)
    df = _measure_in_gui(path, None)
    assert "projection" not in df.columns
    df_ws, failure, _ = measure_one(DatasetStore(path), round_specs=[], seg_name="cells")
    assert failure is None and "projection" not in df_ws.columns


def test_workflow_measure_records_the_projection(tmp_path):
    path = tmp_path / "d.h5"
    _named(path)
    df, failure, _ = measure_one(
        DatasetStore(path, projection="mean"), round_specs=[], seg_name="cells"
    )
    assert failure is None
    assert (df["projection"] == "mean").all()
    assert (df["GFP_mean_intensity"] == 3.0).all()

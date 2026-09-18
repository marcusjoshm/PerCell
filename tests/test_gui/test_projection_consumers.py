"""Every consumer family reads the chosen projection (z-stack plan U5).

The viewer and the launcher's store are covered in
``tests/test_gui_workflows/test_launcher_projection_rebuild.py``; this file
covers the readers that open the file themselves: the analysis loader and
batch runner, and FLIM-FRET's lifetime reads.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from tests.test_application.test_run_analysis_batch import (
    _layer_map_pbody,
    _reregister_analysis,  # noqa: F401 - autouse fixture
    _toy_pbody_arrays,
)

from percell4.application.analysis import batch_run_analysis
from percell4.application.analysis.loader import load_layers
from percell4.domain.analysis.types import ImageRole
from percell4.domain.errors import ProjectionRequiredError
from percell4.store import DatasetStore


def _named_pbody_h5(path: Path, projections: dict[str, float]) -> None:
    """A pbody dataset whose projections scale the same image differently."""
    cap, pnorm, mask = _toy_pbody_arrays()
    store = DatasetStore(path)
    store.create(metadata={"channel_names": ["Cap", "pnorm"], "n_channels": 2})
    for name, scale in projections.items():
        store.write_projection(name, np.stack([cap, pnorm]) * scale, dims=["C", "H", "W"])
    store.write_array("masks/pbody", mask)


def test_loader_reads_the_requested_projection(tmp_path):
    h5 = tmp_path / "a.h5"
    _named_pbody_h5(h5, {"max": 1.0, "mean": 0.5})
    roles = {"cap": ImageRole(kind="intensity", dtype="float", ndim=(2,))}
    default = load_layers(h5, {"cap": "Cap"}, roles)["cap"]
    mean = load_layers(h5, {"cap": "Cap"}, roles, projection="mean")["cap"]
    np.testing.assert_allclose(mean, default * 0.5)
    with pytest.raises(ProjectionRequiredError, match="max, mean"):
        load_layers(h5, {"cap": "Cap"}, roles, projection="sum")


def test_batch_fails_only_the_dataset_without_the_projection(tmp_path):
    with_mean = tmp_path / "a.h5"
    without = tmp_path / "b.h5"
    _named_pbody_h5(with_mean, {"max": 1.0, "mean": 0.5})
    _named_pbody_h5(without, {"max": 1.0})

    report = batch_run_analysis(
        "per_particle_donut", [with_mean, without], lambda _p: _layer_map_pbody(),
        output_parent=tmp_path / "out", preset="m7g-cap-v1", projection="mean",
    )

    status = {item.h5_path.name: item for item in report.items}
    assert status["a.h5"].status == "succeeded"
    assert status["b.h5"].status == "failed"
    assert "mean" in status["b.h5"].error and "max" in status["b.h5"].error
    config = json.loads((report.run_folder / "run_config.json").read_text())
    assert config["projection"] == "mean"


def test_legacy_dataset_in_the_batch_analyses_as_before(tmp_path):
    from tests.test_application.test_run_analysis_batch import _build_pbody_h5

    cap, pnorm, mask = _toy_pbody_arrays()
    legacy = tmp_path / "legacy.h5"
    _build_pbody_h5(legacy, cap=cap, pnorm=pnorm, pbody_mask=mask)
    report = batch_run_analysis(
        "per_particle_donut", [legacy], lambda _p: _layer_map_pbody(),
        output_parent=tmp_path / "out", preset="m7g-cap-v1", projection="mean",
    )
    assert report.items[0].status == "succeeded"


def test_flim_fret_lifetime_reads_a_dataset_with_several_projections(tmp_path):
    from percell4.application.use_cases.flim_fret_discovery import lifetime_store
    from percell4.application.use_cases.run_flim_fret import _read_lifetime_channel

    path = tmp_path / "f.h5"
    store = DatasetStore(path)
    store.create(metadata={"channel_names": ["ch0", "ch0_lifetime"], "n_channels": 2})
    lifetime = np.full((4, 4), 2.5, np.float32)
    for name in ("mean", "sum"):  # no max: an unnamed read would be ambiguous
        store.write_projection(
            name, np.stack([np.zeros((4, 4), np.float32), lifetime]), dims=["C", "H", "W"]
        )
    np.testing.assert_array_equal(
        _read_lifetime_channel(lifetime_store(path), "ch0_lifetime"), lifetime
    )

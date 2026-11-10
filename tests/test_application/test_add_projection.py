"""Adding a projection later from the stored z-series (z-stack plan U9, KTD10)."""

from __future__ import annotations

import numpy as np
import pytest

from percell4.adapters.hdf5_store import Hdf5DatasetRepository
from percell4.application.session import Session
from percell4.application.use_cases.add_projection import AddProjection
from percell4.domain.errors import AddProjectionError, ImportCancelledError
from percell4.store import DatasetStore

H, W, Z = 6, 8, 5


def _zstack(n_t=None, seed=0):
    rng = np.random.default_rng(seed)
    shape = (2, Z, H, W) if n_t is None else (n_t, 2, Z, H, W)
    return rng.integers(0, 1000, size=shape).astype(np.float32)


def _dataset(path, *, n_t=None, projections=(), zseries=True):
    store = DatasetStore(path)
    meta = {"channel_names": ["ch0", "ch1"], "n_channels": 2}
    if n_t:
        meta["n_timepoints"] = n_t
    store.create(metadata=meta)
    stack = _zstack(n_t)
    if zseries:
        with store.zseries_writer(stack.shape, ["ch0", "ch1"]) as w:
            for t in range(n_t or 1):
                for c in range(2):
                    for z in range(Z):
                        w.write_plane(t, c, z, stack[t, c, z] if n_t else stack[c, z])
    for name in projections:
        reduce = {"max": np.max, "mean": np.mean, "sum": np.sum}[name]
        image = reduce(stack, axis=-3).astype(np.float32)
        store.write_projection(name, image, dims=(["T"] if n_t else []) + ["C", "H", "W"])
    return store, stack


def _uc(path):
    repo = Hdf5DatasetRepository()
    session = Session()
    session.set_dataset(repo.open(path))
    return AddProjection(repo, session), session


def test_ae1_zseries_only_dataset_gains_max_then_analyses(tmp_path):
    """Covers AE1."""
    from percell4.application.analysis.loader import load_layers
    from percell4.domain.analysis.types import ImageRole

    path = tmp_path / "z.h5"
    _store, stack = _dataset(path)
    uc, session = _uc(path)
    assert session.active_projection is None

    assert uc.execute("max") == ["max"]

    assert session.active_projection == "max"
    roles = {"img": ImageRole(kind="intensity", dtype="float", ndim=(2,))}
    image = load_layers(path, {"img": "ch1"}, roles)["img"]
    np.testing.assert_array_equal(image, stack[1].max(axis=0))


def test_ae2_dataset_without_zseries_refuses(tmp_path):
    """Covers AE2."""
    path = tmp_path / "p.h5"
    _dataset(path, projections=("max",), zseries=False)
    uc, _session = _uc(path)
    with pytest.raises(AddProjectionError, match="z-series was not kept"):
        uc.execute("mean")
    assert DatasetStore(path).list_projections() == ("max",)


def test_mean_equals_numpy_per_channel_and_timepoint(tmp_path):
    path = tmp_path / "t.h5"
    _store, stack = _dataset(path, n_t=3, projections=("max",))
    uc, session = _uc(path)
    assert uc.execute("mean") == ["max", "mean"]
    assert session.active_projection == "max"  # adding never switches
    mean = DatasetStore(path, projection="mean").read_array("intensity")
    np.testing.assert_allclose(mean, stack.astype(np.float64).mean(axis=2), rtol=1e-6)
    assert session.projection_names == ["max", "mean"]


def test_existing_projection_refuses_and_leaves_the_file(tmp_path):
    path = tmp_path / "d.h5"
    _dataset(path, projections=("max",))
    before = DatasetStore(path).read_array("intensity").copy()
    uc, _session = _uc(path)
    with pytest.raises(AddProjectionError, match="already stored"):
        uc.execute("max")
    np.testing.assert_array_equal(DatasetStore(path).read_array("intensity"), before)


def test_cancel_leaves_the_dataset_unchanged(tmp_path):
    path = tmp_path / "d.h5"
    _dataset(path, projections=("max",))
    uc, _session = _uc(path)
    calls = {"n": 0}

    def cancelled():
        calls["n"] += 1
        return calls["n"] > 1

    with pytest.raises(ImportCancelledError):
        uc.execute("sum", is_cancelled=cancelled)
    store = DatasetStore(path)
    assert store.list_projections() == ("max",)
    import h5py

    with h5py.File(path) as f:
        assert [k for k in f if k.endswith(".tmp")] == []


def test_channel_without_z_is_copied_from_an_existing_projection(tmp_path):
    path = tmp_path / "d.h5"
    store, _stack = _dataset(path, projections=("max",))
    lifetime = np.full((H, W), 2.5, np.float32)

    def append(arr):
        return np.concatenate([arr, lifetime[np.newaxis]]), {"dims": ["C", "H", "W"]}

    store.rewrite_projections(append)
    store.set_metadata({"channel_names": ["ch0", "ch1", "tau"], "n_channels": 3})
    store.add_projection_from_zseries("sum")
    summed = DatasetStore(path, projection="sum").read_array("intensity")
    assert summed.shape == (3, H, W)
    np.testing.assert_array_equal(summed[2], lifetime)

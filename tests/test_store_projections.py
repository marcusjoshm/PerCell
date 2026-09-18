"""Named projections, the z-series and the projection resolver in DatasetStore."""

from __future__ import annotations

import h5py
import numpy as np
import pytest

from percell4.domain.errors import ProjectionRequiredError
from percell4.domain.io.view_bin import sum_bin_2d
from percell4.store import DatasetStore, DimsConsistencyError, LayerSizeMismatchError

H, W = 8, 12


def _plane(seed: float, h: int = H, w: int = W) -> np.ndarray:
    return (np.arange(h * w, dtype=np.float32).reshape(h, w) + seed)


def _new_store(tmp_path, name="d.h5", **meta) -> DatasetStore:
    s = DatasetStore(tmp_path / name)
    s.create(metadata={"channel_names": ["ch0", "ch1"], "n_channels": 2, **meta})
    return s


def _projection_store(tmp_path, names=("max", "mean"), projection=None) -> DatasetStore:
    s = _new_store(tmp_path)
    for i, name in enumerate(names):
        s.write_projection(name, np.stack([_plane(10 * i), _plane(10 * i + 1)]),
                           dims=["C", "H", "W"])
    return DatasetStore(s.path, projection=projection) if projection else s


def _legacy_store(tmp_path, z_projection=None) -> tuple[DatasetStore, np.ndarray]:
    meta = {"z_projection": z_projection} if z_projection else {}
    s = _new_store(tmp_path, **meta)
    data = np.stack([_plane(1), _plane(2)])
    s.write_array("intensity", data, attrs={"dims": ["C", "H", "W"]})
    return s, data


def _write_zseries(store: DatasetStore, n_t: int | None, n_c=2, n_z=3) -> dict:
    shape = (n_c, n_z, H, W) if n_t is None else (n_t, n_c, n_z, H, W)
    planes = {}
    with store.zseries_writer(shape, [f"ch{c}" for c in range(n_c)]) as w:
        for t in range(n_t or 1):
            for c in range(n_c):
                for z in range(n_z):
                    plane = _plane(100 * t + 10 * c + z)
                    w.write_plane(t, c, z, plane)
                    planes[(t, c, z)] = plane
    return planes


# ── legacy /intensity: characterization ────────────────────────


def test_legacy_reads_are_unchanged(tmp_path):
    s, data = _legacy_store(tmp_path, z_projection="mip")
    np.testing.assert_array_equal(s.read_array("intensity"), data)
    np.testing.assert_array_equal(s.read_channel("intensity", 1), data[1])
    np.testing.assert_array_equal(s.read_array_frame("intensity", 0), data)
    np.testing.assert_array_equal(
        s.read_array("intensity", view_bin=2), sum_bin_2d(data, 2)
    )
    assert s.array_exists("intensity")
    assert s.array_shape("intensity") == (2, H, W)
    assert s.array_dtype("intensity") == np.float32
    assert s.read_array_attrs("intensity")["dims"].tolist() == ["C", "H", "W"]
    assert not s.is_time_stacked("intensity")
    assert s.metadata["native_shape"] == (H, W)


def test_legacy_with_mip_lists_one_projection_named_max(tmp_path):
    s, data = _legacy_store(tmp_path, z_projection="mip")
    assert s.list_projections() == ("max",)
    np.testing.assert_array_equal(DatasetStore(s.path, projection="max").read_array(
        "intensity"), data)
    assert s.resolved_intensity_path() == "intensity"


def test_legacy_without_z_projection_is_named_projection(tmp_path):
    s, _ = _legacy_store(tmp_path)
    assert s.list_projections() == ("projection",)


def test_legacy_rejects_a_projection_it_does_not_hold(tmp_path):
    s, _ = _legacy_store(tmp_path)
    with pytest.raises(ProjectionRequiredError, match="projection"):
        DatasetStore(s.path, projection="max").read_array("intensity")


def test_empty_dataset_keeps_todays_key_error(tmp_path):
    s = _new_store(tmp_path)
    assert s.list_projections() == ()
    assert not s.array_exists("intensity")
    with pytest.raises(KeyError):
        s.read_array("intensity")


# ── resolver ──────────────────────────────────────────────────


def test_no_projection_given_reads_max(tmp_path):
    s = _projection_store(tmp_path)
    assert s.list_projections() == ("max", "mean")
    np.testing.assert_array_equal(s.read_channel("intensity", 0), _plane(0))
    assert s.resolved_intensity_path() == "projections/max"


def test_named_projection_reads_that_projection(tmp_path):
    s = _projection_store(tmp_path, projection="mean")
    np.testing.assert_array_equal(s.read_channel("intensity", 1), _plane(11))
    np.testing.assert_array_equal(s.read_array("/intensity")[0], _plane(10))
    np.testing.assert_array_equal(s.read_array_frame("intensity", 0)[0], _plane(10))
    assert s.resolved_intensity_path() == "projections/mean"


def test_absent_projection_raises_naming_stored(tmp_path):
    s = _projection_store(tmp_path, projection="sum")
    with pytest.raises(ProjectionRequiredError, match="max, mean"):
        s.read_array("intensity")
    with pytest.raises(ProjectionRequiredError):
        s.array_shape("intensity")
    assert not s.array_exists("intensity")


def test_several_without_max_raise_when_none_given(tmp_path):
    s = _projection_store(tmp_path, names=("mean", "sum"))
    with pytest.raises(ProjectionRequiredError, match="mean, sum"):
        s.read_array("intensity")
    with pytest.raises(ProjectionRequiredError):
        s.array_dtype("intensity")
    # Existence and dims are answerable: every projection shares them.
    assert s.array_exists("intensity")
    assert not s.is_time_stacked("intensity")


def test_sole_projection_is_read_when_none_given(tmp_path):
    s = _projection_store(tmp_path, names=("mean",))
    np.testing.assert_array_equal(s.read_channel("intensity", 0), _plane(0))


def test_view_bin_applies_to_named_projection_as_to_legacy(tmp_path):
    s = _projection_store(tmp_path)
    data = np.stack([_plane(0), _plane(1)])
    np.testing.assert_array_equal(
        s.read_array("intensity", view_bin=2), sum_bin_2d(data, 2)
    )
    np.testing.assert_array_equal(
        s.read_channel("intensity", 1, view_bin=2), sum_bin_2d(_plane(1), 2)
    )


def test_session_reads_resolve_too(tmp_path):
    s = _projection_store(tmp_path, projection="mean")
    with s.open_read() as r:
        np.testing.assert_array_equal(r.read_channel("intensity", 0), _plane(10))


def test_other_paths_are_untouched(tmp_path):
    s = _projection_store(tmp_path)
    s.write_labels("cells", np.ones((H, W), dtype=np.int32))
    assert s.read_labels("cells").sum() == H * W


def test_legacy_write_path_is_refused_on_named_projection_dataset(tmp_path):
    s = _projection_store(tmp_path)
    with pytest.raises(ValueError, match="projection"):
        s.write_array("intensity", np.zeros((2, H, W), np.float32))


def test_write_projection_rejects_unknown_name(tmp_path):
    s = _new_store(tmp_path)
    with pytest.raises(ValueError, match="unknown projection"):
        s.write_projection("median", np.zeros((H, W), np.float32), dims=["H", "W"])


def test_dims_consistency_checks_every_projection(tmp_path):
    s = _projection_store(tmp_path)
    with h5py.File(s.path, "a") as f:
        f["projections/mean"].attrs["dims"] = ["C", "H"]
    with pytest.raises(DimsConsistencyError):
        s.check_intensity_dims_consistency()


# ── z-series ──────────────────────────────────────────────────


def test_zseries_round_trips_plane_exact(tmp_path):
    s = _new_store(tmp_path)
    planes = _write_zseries(s, n_t=None)
    assert s.has_zseries()
    assert s.zseries_shape() == (2, 3, H, W)
    assert s.zseries_dims() == ("C", "Z", "H", "W")
    assert s.zseries_channels() == ("ch0", "ch1")
    for (t, c, z), plane in planes.items():
        np.testing.assert_array_equal(s.read_zseries_plane(t, c, z), plane)
    with h5py.File(s.path) as f:
        assert f["zseries"].dtype == np.float32
        assert f["zseries"].chunks == (1, 1, H, W)


def test_zseries_time_lapse_round_trips(tmp_path):
    s = _new_store(tmp_path)
    planes = _write_zseries(s, n_t=2)
    assert s.zseries_dims() == ("T", "C", "Z", "H", "W")
    np.testing.assert_array_equal(s.read_zseries_plane(1, 1, 2), planes[(1, 1, 2)])
    np.testing.assert_array_equal(
        s.read_zseries_plane(1, 0, 1, view_bin=2), sum_bin_2d(planes[(1, 0, 1)], 2)
    )


def test_zseries_only_dataset_is_view_only(tmp_path):
    s = _new_store(tmp_path)
    _write_zseries(s, n_t=2)
    assert s.list_projections() == ()
    assert not s.array_exists("intensity")
    with pytest.raises(ProjectionRequiredError, match="add a projection first"):
        s.read_array("intensity")
    with pytest.raises(ProjectionRequiredError, match="add a projection first"):
        s.read_channel("intensity", 0, timepoint=0)
    meta = s.metadata
    assert meta["native_shape"] == (H, W)
    assert meta["n_timepoints"] == 2


def test_zseries_metadata_needs_no_pixel_read(tmp_path, monkeypatch):
    s = _new_store(tmp_path)
    _write_zseries(s, n_t=None)

    def boom(self, key):
        raise AssertionError("pixel data was read")

    monkeypatch.setattr(h5py.Dataset, "__getitem__", boom)
    assert s.has_zseries()
    assert s.zseries_shape() == (2, 3, H, W)
    assert s.zseries_channels() == ("ch0", "ch1")
    assert s.metadata["native_shape"] == (H, W)


def test_zseries_xy_must_match_projections(tmp_path):
    s = _projection_store(tmp_path)
    with pytest.raises(LayerSizeMismatchError), s.zseries_writer((2, 3, H + 1, W),
                                                                   ["ch0", "ch1"]):
        pass


def test_projection_xy_must_match_zseries(tmp_path):
    s = _new_store(tmp_path)
    _write_zseries(s, n_t=None)
    with pytest.raises(LayerSizeMismatchError):
        s.write_projection("max", np.zeros((2, H, W + 2), np.float32), dims=["C", "H", "W"])


def test_failed_zseries_write_leaves_no_array(tmp_path):
    s = _new_store(tmp_path)
    with pytest.raises(RuntimeError), s.zseries_writer((1, 2, H, W), ["ch0"]):
        raise RuntimeError("cancelled")
    assert not s.has_zseries()


# ── channel rewrites ──────────────────────────────────────────


def test_append_channel_covers_every_projection_not_zseries(tmp_path):
    s = _projection_store(tmp_path)
    _write_zseries(s, n_t=None)
    added = _plane(99)

    def append(arr):
        return np.concatenate([arr, added[np.newaxis]]), {"dims": ["C", "H", "W"]}

    assert s.rewrite_projections(append) == 2
    for name in ("max", "mean"):
        stored = DatasetStore(s.path, projection=name).read_array("intensity")
        assert stored.shape == (3, H, W)
        np.testing.assert_array_equal(stored[2], added)
    assert s.zseries_shape() == (2, 3, H, W)


def test_rewrite_on_legacy_rewrites_intensity(tmp_path):
    s, data = _legacy_store(tmp_path)
    s.rewrite_projections(lambda arr: (arr[:1], {"dims": ["C", "H", "W"]}))
    np.testing.assert_array_equal(s.read_array("intensity"), data[:1])


def test_rewrite_on_empty_dataset_writes_legacy_intensity(tmp_path):
    s = _new_store(tmp_path)
    s.rewrite_projections(lambda arr: (_plane(0), {"dims": ["H", "W"]}))
    np.testing.assert_array_equal(s.read_array("intensity"), _plane(0))


def test_rewrite_on_zseries_only_dataset_asks_for_a_projection(tmp_path):
    s = _new_store(tmp_path)
    _write_zseries(s, n_t=None)
    with pytest.raises(ProjectionRequiredError, match="add a projection first"):
        s.rewrite_projections(lambda arr: (arr, {}))


def test_rewrite_returning_none_deletes(tmp_path):
    s = _projection_store(tmp_path)
    s.rewrite_projections(lambda arr: None)
    assert s.list_projections() == ()


def test_delete_channel_removes_it_from_zseries(tmp_path):
    s = _new_store(tmp_path)
    planes = _write_zseries(s, n_t=2)
    assert s.delete_channel("ch0")
    assert s.zseries_channels() == ("ch1",)
    assert s.zseries_shape() == (2, 1, 3, H, W)
    np.testing.assert_array_equal(s.read_zseries_plane(1, 0, 2), planes[(1, 1, 2)])


def test_delete_last_zseries_channel_removes_zseries(tmp_path):
    s = _new_store(tmp_path)
    _write_zseries(s, n_t=None, n_c=1)
    s.delete_channel("ch0")
    assert not s.has_zseries()


def test_rename_channel_renames_zseries_channel(tmp_path):
    s = _new_store(tmp_path)
    _write_zseries(s, n_t=None)
    s.rename_channel("ch1", "GFP")
    assert s.zseries_channels() == ("ch0", "GFP")

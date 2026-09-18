"""In-file import: one ImportSource -> one atomic .h5 through the reader port.

Every test drives the fake reader, so no JVM is needed. The real reader's
projection is checked against the same numpy reference in
``tests/test_adapters/test_bioformats_reader.py``.
"""

from __future__ import annotations

import os

import numpy as np
import pytest
from tests.fakes.fake_image_reader import FakeImageReader, probe_for

from percell4.adapters.importer import import_infile_dataset
from percell4.domain.errors import ImportCancelledError, ImportSchemeError
from percell4.domain.io.infile import AxisMap, ImportSource
from percell4.store import DatasetStore


def _stack(t=1, c=3, z=5, y=16, x=20, seed=0):
    rng = np.random.default_rng(seed)
    return rng.integers(0, 4000, size=(t, c, z, y, x)).astype(np.uint16)


def _source_for(path, stack, **probe_kw):
    """Write a placeholder file (for stat), build its probe and a source."""
    path.write_bytes(b"x" * 64)
    probe = probe_for(path, stack, **probe_kw)
    st = os.stat(path)
    series = probe.series[0]
    source = ImportSource(
        path=path,
        series_index=0,
        channel_indices=tuple(range(series.size_c)),
        output_name=path.stem,
        expected_size=st.st_size,
        expected_mtime_ns=st.st_mtime_ns,
        series=series,
    )
    reader = FakeImageReader([probe], arrays={(path, 0): stack})
    return source, reader


def test_zcyx_imports_as_chw_with_calibration(tmp_path):
    """Covers AE1 (fake reader)."""
    stack = _stack(c=3, z=5)
    source, reader = _source_for(
        tmp_path / "a.tif", stack, physical_x_um=0.041, physical_z_um=0.125
    )
    out = tmp_path / "out" / "a.h5"

    n = import_infile_dataset(source, out, reader, z_method="mip")

    assert n == 3
    store = DatasetStore(out)
    intensity = store.read_array("intensity")
    assert intensity.shape == (3, 16, 20)
    assert intensity.dtype == np.float32
    np.testing.assert_array_equal(intensity, stack[0].max(axis=1).astype(np.float32))
    meta = store.metadata
    assert meta["channel_names"] == ["ch0", "ch1", "ch2"]
    assert meta["pixel_size_um"] == pytest.approx(0.041)
    assert meta["z_spacing_um"] == pytest.approx(0.125)
    assert meta["z_projection"] == "mip"
    assert meta["source_series"] == 0
    assert meta["n_timepoints"] == 1
    assert meta["native_shape"] == (16, 20)


def test_intensity_dims_attr_matches_layout(tmp_path):
    import h5py

    source, reader = _source_for(tmp_path / "a.tif", _stack(c=2))
    out = tmp_path / "a.h5"
    import_infile_dataset(source, out, reader)
    with h5py.File(out, "r") as f:
        assert list(f["intensity"].attrs["dims"]) == ["C", "H", "W"]


def test_tzcyx_imports_as_tchw_with_timepoints(tmp_path):
    stack = _stack(t=4, c=2, z=3)
    source, reader = _source_for(tmp_path / "a.tif", stack)
    out = tmp_path / "a.h5"

    import_infile_dataset(source, out, reader, z_method="mean")

    store = DatasetStore(out)
    intensity = store.read_array("intensity")
    assert intensity.shape == (4, 2, 16, 20)
    expected = stack.astype(np.float64).mean(axis=2).astype(np.float32)
    np.testing.assert_allclose(intensity, expected, rtol=1e-6)
    assert store.metadata["n_timepoints"] == 4


def test_single_channel_single_timepoint_is_hw(tmp_path):
    source, reader = _source_for(tmp_path / "a.tif", _stack(t=1, c=1, z=4))
    out = tmp_path / "a.h5"
    assert import_infile_dataset(source, out, reader) == 1
    assert DatasetStore(out).read_array("intensity").shape == (16, 20)


def test_single_channel_timelapse_is_thw(tmp_path):
    source, reader = _source_for(tmp_path / "a.tif", _stack(t=3, c=1, z=2))
    out = tmp_path / "a.h5"
    import_infile_dataset(source, out, reader)
    assert DatasetStore(out).read_array("intensity").shape == (3, 16, 20)


def test_channel_subset_keeps_order_and_names_by_index(tmp_path):
    stack = _stack(c=3)
    source, reader = _source_for(tmp_path / "a.tif", stack)
    source = source.__class__(**{**source.__dict__, "channel_indices": (0, 2)})
    out = tmp_path / "a.h5"

    assert import_infile_dataset(source, out, reader) == 2

    store = DatasetStore(out)
    assert store.metadata["channel_names"] == ["ch0", "ch2"]
    np.testing.assert_array_equal(
        store.read_array("intensity")[1], stack[0, 2].max(axis=0).astype(np.float32)
    )


def test_sum_accumulates_without_uint16_overflow(tmp_path):
    stack = np.full((1, 1, 40, 4, 4), 60000, dtype=np.uint16)
    source, reader = _source_for(tmp_path / "a.tif", stack)
    out = tmp_path / "a.h5"
    import_infile_dataset(source, out, reader, z_method="sum")
    assert DatasetStore(out).read_array("intensity")[0, 0] == pytest.approx(2_400_000)


def test_axis_reassignment_imports_z_as_time(tmp_path):
    """Covers AE7: a stack reassigned from Z to T keeps every plane."""
    stack = _stack(t=1, c=1, z=6)
    source, reader = _source_for(tmp_path / "a.tif", stack)
    source = source.__class__(**{**source.__dict__, "axis_map": AxisMap(z="T", t="Z")})
    out = tmp_path / "a.h5"

    import_infile_dataset(source, out, reader)

    store = DatasetStore(out)
    assert store.read_array("intensity").shape == (6, 16, 20)
    assert store.metadata["n_timepoints"] == 6
    assert "z_spacing_um" not in store.metadata


def test_creation_bin_halves_shape_and_doubles_pixel_size_only(tmp_path):
    source, reader = _source_for(
        tmp_path / "a.tif", _stack(c=2), physical_x_um=0.05, physical_z_um=0.2
    )
    out = tmp_path / "a.h5"

    import_infile_dataset(source, out, reader, creation_bin=2)

    store = DatasetStore(out)
    assert store.read_array("intensity").shape == (2, 8, 10)
    meta = store.metadata
    assert meta["pixel_size_um"] == pytest.approx(0.1)
    assert meta["z_spacing_um"] == pytest.approx(0.2)
    assert meta["creation_bin"] == 2
    assert meta["native_shape"] == (8, 10)


def test_unknown_physical_size_stores_no_pixel_size(tmp_path):
    source, reader = _source_for(tmp_path / "a.tif", _stack(), physical_x_um=None)
    out = tmp_path / "a.h5"
    import_infile_dataset(source, out, reader)
    assert "pixel_size_um" not in DatasetStore(out).metadata


def test_cancel_mid_file_leaves_no_output_or_temp(tmp_path):
    """Covers AE5."""
    source, reader = _source_for(tmp_path / "a.tif", _stack(c=3))
    out = tmp_path / "a.h5"
    calls = {"n": 0}

    def cancelled():
        calls["n"] += 1
        return calls["n"] > 1

    with pytest.raises(ImportCancelledError):
        import_infile_dataset(source, out, reader, is_cancelled=cancelled)

    assert not out.exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_reader_failure_leaves_no_output_or_temp(tmp_path):
    source, reader = _source_for(tmp_path / "a.tif", _stack())

    class BoomError(RuntimeError):
        pass

    def broken(*_a, **_k):
        raise BoomError("plane read failed")

    reader.read_projected = broken
    out = tmp_path / "a.h5"
    with pytest.raises(BoomError):
        import_infile_dataset(source, out, reader)
    assert not out.exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_source_changed_since_scan_raises_before_writing(tmp_path):
    source, reader = _source_for(tmp_path / "a.tif", _stack())
    (tmp_path / "a.tif").write_bytes(b"y" * 128)
    out = tmp_path / "a.h5"

    with pytest.raises(ImportSchemeError, match="changed since"):
        import_infile_dataset(source, out, reader)

    assert not out.exists()
    assert reader.read_calls == []


def test_output_outside_output_dir_is_refused(tmp_path):
    source, reader = _source_for(tmp_path / "a.tif", _stack())
    outdir = tmp_path / "outputs"
    outdir.mkdir()

    with pytest.raises(ImportSchemeError, match="outside"):
        import_infile_dataset(source, tmp_path / "elsewhere.h5", reader, output_dir=outdir)


def test_overwrite_replaces_the_whole_file(tmp_path):
    source, reader = _source_for(tmp_path / "a.tif", _stack(c=2))
    out = tmp_path / "a.h5"
    old = DatasetStore(out)
    old.create(metadata={"channel_names": ["old"]})
    old.write_array("labels/cells", np.ones((16, 20), dtype=np.int32))

    import_infile_dataset(source, out, reader)

    store = DatasetStore(out)
    assert store.metadata["channel_names"] == ["ch0", "ch1"]
    import h5py

    with h5py.File(out, "r") as f:
        assert "labels" not in f


def test_project_csv_updated_after_write(tmp_path):
    from percell4.project import ProjectIndex

    source, reader = _source_for(tmp_path / "a.tif", _stack())
    out = tmp_path / "a.h5"
    csv = tmp_path / "project.csv"

    import_infile_dataset(source, out, reader, project_csv=csv)

    assert csv.exists()
    assert str(out) in csv.read_text()
    assert ProjectIndex(csv).exists()


def test_on_plane_reports_each_projected_plane(tmp_path):
    source, reader = _source_for(tmp_path / "a.tif", _stack(t=2, c=3))
    seen = []
    import_infile_dataset(
        source, tmp_path / "a.h5", reader, on_plane=lambda t, c: seen.append((t, c))
    )
    assert seen == [(t, c) for t in range(2) for c in range(3)]


def test_store_metadata_returns_plain_types_for_infile_keys(tmp_path):
    source, reader = _source_for(tmp_path / "a.tif", _stack(), physical_z_um=0.125)
    out = tmp_path / "a.h5"
    import_infile_dataset(source, out, reader)
    meta = DatasetStore(out).metadata
    assert type(meta["z_spacing_um"]) is float
    assert type(meta["source_series"]) is int
    assert type(meta["z_projection"]) is str

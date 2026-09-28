"""Tests for the native OME-Zarr reader (``percell4.adapters.omezarr_reader``).

Two kinds of store are used. The checked-in stores in
``tests/fixtures/omezarr`` were written by zarr-python, so they are an
independent reference for chunk layout, edge padding, memory order,
separators and codecs. Synthetic stores from ``tests/fakes/omezarr_fixture``
cover the bioformats2raw layout, pyramids, several series and bigger planes.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import os
import sys
from pathlib import Path

import numpy as np
import pytest
from tests.fakes.omezarr_fixture import write_store

import percell4.adapters.omezarr_reader as reader_mod
from percell4.adapters.omezarr_reader import OmeZarrReader, store_fingerprint
from percell4.domain.errors import OmeZarrReadError
from percell4.domain.io.infile import AXIS_NAMED, AxisMap, ImportSource, suggest_scheme
from percell4.ports.image_reader import ImageReader

REFERENCE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "omezarr"

IDR0168_STORE = Path(
    "/Users/marcusjoshm/Documents/microscopy-data/idr/idr0168/"
    "MCF7_1_EIF4G1_Z-stack_HF_04_2024-03-06_YunHao_18.37.05.zarr"
)


def _reference_module():
    spec = importlib.util.spec_from_file_location(
        "make_reference_stores", REFERENCE_DIR / "make_reference_stores.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


REFERENCE = _reference_module()


def _source(path: Path, reader: OmeZarrReader | None = None, **kw) -> ImportSource:
    """The first scheme source for ``path``, probed by a fresh reader."""
    (probe,) = (reader or OmeZarrReader()).probe([path])
    assert probe.error is None, probe.error
    source = suggest_scheme([probe]).sources[0]
    return dataclasses.replace(source, **kw)


def _planes(reader, source, **kw) -> dict[tuple[int, int, int], np.ndarray]:
    return {(t, c, z): p for t, c, z, p in reader.read_planes(source, **kw)}


def _tczyx(seed: int, shape, dtype=">u2") -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(0, 60000, size=shape).astype(np.dtype(dtype))


def test_reader_satisfies_the_port() -> None:
    assert isinstance(OmeZarrReader(), ImageReader)


# ---------------------------------------------------------------------------
# Reference stores written by zarr-python
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(REFERENCE.VARIANTS))
def test_reference_store_round_trips(name: str) -> None:
    dtype = REFERENCE.VARIANTS[name][0]
    expected = REFERENCE.expected(dtype)
    path = REFERENCE_DIR / f"{name}.zarr"
    reader = OmeZarrReader()
    source = _source(path, reader)
    series = source.series
    assert (series.size_c, series.size_z, series.size_y, series.size_x) == (2, 3, 5, 7)
    assert series.physical_z_um == pytest.approx(0.5)
    assert series.physical_x_um == pytest.approx(0.25)
    assert series.channel_names == ("A", "B")
    assert series.axis_source == AXIS_NAMED

    planes = _planes(reader, source)
    assert sorted(planes) == [(0, c, z) for c in range(2) for z in range(3)]
    for (_, c, z), plane in planes.items():
        assert plane.dtype == np.dtype(dtype).newbyteorder("=")
        assert plane.dtype.isnative
        np.testing.assert_array_equal(plane, expected[c, z])


def test_missing_chunk_reads_as_fill_value() -> None:
    path = REFERENCE_DIR / "c_dot_raw_u1_missing.zarr"
    assert not (path / "0" / "1.0.0.0").exists()
    planes = _planes(OmeZarrReader(), _source(path))
    assert not planes[(0, 1, 0)][0:3, 0:4].any()


# ---------------------------------------------------------------------------
# Synthetic stores
# ---------------------------------------------------------------------------


def test_bioformats2raw_store_reads_level_zero_only(tmp_path) -> None:
    """AE2: two channels, three z, big-endian, a second pyramid level."""
    data = _tczyx(1, (1, 2, 3, 40, 24))
    root = write_store(
        tmp_path / "s.zarr",
        [data],
        chunks=(1, 1, 1, 16, 16),
        levels=2,
        channel_names=[("DAPI", "GFP")],
    )
    # Level 1 must never be touched: remove it entirely.
    for f in sorted((root / "0" / "1").rglob("*"), reverse=True):
        f.unlink() if f.is_file() else f.rmdir()
    (root / "0" / "1").rmdir()

    reader = OmeZarrReader()
    source = _source(root, reader)
    assert source.series.channel_names == ("DAPI", "GFP")
    assert source.series.pixel_type == "uint16"
    planes = list(reader.read_planes(source))
    assert [(t, c, z) for t, c, z, _ in planes] == [(0, c, z) for c in range(2) for z in range(3)]
    for t, c, z, plane in planes:
        assert plane.dtype == np.dtype("uint16")
        np.testing.assert_array_equal(plane, data[t, c, z])


def test_several_series_read_by_series_index(tmp_path) -> None:
    a = _tczyx(2, (1, 1, 2, 8, 8))
    b = _tczyx(3, (2, 1, 1, 6, 5))
    root = write_store(tmp_path / "multi.zarr", [a, b], levels=1)
    reader = OmeZarrReader()
    (probe,) = reader.probe([root])
    scheme = suggest_scheme([probe])
    assert [s.output_name for s in scheme.sources] == ["multi_s00", "multi_s01"]
    planes = _planes(reader, scheme.sources[1])
    assert sorted(planes) == [(0, 0, 0), (1, 0, 0)]
    np.testing.assert_array_equal(planes[(1, 0, 0)], b[1, 0, 0])


def test_chunk_spanning_every_z_is_decoded_once_per_stack(tmp_path, monkeypatch) -> None:
    data = _tczyx(4, (1, 2, 5, 12, 12))
    root = write_store(tmp_path / "deep.zarr", [data], chunks=(1, 1, 5, 6, 6), levels=1)
    calls: list[str] = []
    real = reader_mod._decode

    def counting(raw, spec):
        calls.append("x")
        return real(raw, spec)

    monkeypatch.setattr(reader_mod, "_decode", counting)
    planes = _planes(OmeZarrReader(), _source(root))
    # 2 channels x 4 yx tiles, each decoded once although it spans 5 z.
    assert len(calls) == 2 * 4
    np.testing.assert_array_equal(planes[(0, 1, 4)], data[0, 1, 4])


def test_channel_subset_and_on_plane_once_per_stack(tmp_path) -> None:
    data = _tczyx(5, (2, 3, 2, 8, 8))
    root = write_store(tmp_path / "c.zarr", [data], levels=1)
    stacks: list[tuple[int, int]] = []
    source = _source(root, channel_indices=(2, 0))
    planes = list(OmeZarrReader().read_planes(source, on_plane=lambda t, c: stacks.append((t, c))))
    assert stacks == [(0, 2), (0, 0), (1, 2), (1, 0)]
    assert [(t, c, z) for t, c, z, _ in planes][:4] == [(0, 2, 0), (0, 2, 1), (0, 0, 0), (0, 0, 1)]


def test_axis_swap_reads_time_as_z(tmp_path) -> None:
    data = _tczyx(6, (3, 1, 1, 8, 8))
    root = write_store(tmp_path / "tl.zarr", [data], levels=1)
    source = _source(root, axis_map=AxisMap(z="T", t="Z"))
    planes = list(OmeZarrReader().read_planes(source))
    assert [(t, c, z) for t, c, z, _ in planes] == [(0, 0, 0), (0, 0, 1), (0, 0, 2)]
    for _, _, z, plane in planes:
        np.testing.assert_array_equal(plane, data[z, 0, 0])


def test_cancel_mid_stack_ends_the_iterator(tmp_path) -> None:
    data = _tczyx(7, (1, 1, 4, 8, 8))
    root = write_store(tmp_path / "x.zarr", [data], levels=1)
    seen = []
    for item in OmeZarrReader().read_planes(_source(root), is_cancelled=lambda: len(seen) >= 2):
        seen.append(item)
    assert len(seen) == 2


def test_truncated_chunk_raises_the_typed_error(tmp_path) -> None:
    data = _tczyx(8, (1, 1, 1, 8, 8))
    root = write_store(tmp_path / "bad.zarr", [data], levels=1)
    chunk = root / "0" / "0" / "0" / "0" / "0" / "0" / "0"
    chunk.write_bytes(chunk.read_bytes()[:10])
    with pytest.raises(OmeZarrReadError, match="bad.zarr"):
        list(OmeZarrReader().read_planes(_source(root)))


def test_fresh_reader_reads_without_a_prior_probe(tmp_path) -> None:
    """Scheme and workflow replay read on a reader that never probed."""
    data = _tczyx(9, (1, 1, 2, 8, 8))
    root = write_store(tmp_path / "r.zarr", [data], levels=1)
    source = _source(root)
    planes = _planes(OmeZarrReader(), source)
    np.testing.assert_array_equal(planes[(0, 0, 1)], data[0, 0, 1])


@pytest.mark.parametrize("z_method", ["mip", "mean", "sum"])
def test_read_projected_matches_numpy(tmp_path, z_method: str) -> None:
    data = _tczyx(10, (1, 2, 3, 8, 8))
    root = write_store(tmp_path / "p.zarr", [data], levels=1)
    out = {(t, c): p for t, c, p in OmeZarrReader().read_projected(_source(root), z_method)}
    stack = data[0, 1].astype(np.float64)
    want = {"mip": stack.max(axis=0), "mean": stack.mean(axis=0), "sum": stack.sum(axis=0)}
    assert out[(0, 1)].dtype == np.float32
    np.testing.assert_allclose(out[(0, 1)], want[z_method].astype(np.float32), rtol=1e-6)


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads unreadable files")
def test_probe_never_opens_a_chunk(tmp_path) -> None:
    data = _tczyx(11, (1, 2, 2, 8, 8))
    root = write_store(tmp_path / "m.zarr", [data], chunks=(1, 1, 1, 4, 4), levels=2)
    chunks = [
        f
        for f in root.rglob("*")
        if f.is_file() and not f.name.startswith(".") and f.suffix != ".xml"
    ]
    for f in chunks:
        f.chmod(0)
    try:
        (probe,) = OmeZarrReader().probe([root])
    finally:
        for f in chunks:
            f.chmod(0o644)
    assert probe.error is None
    assert probe.format_name == "OME-Zarr"
    assert probe.used_files == (root,)
    assert probe.series[0].size_c == 2


def test_probe_reports_errors_as_records(tmp_path) -> None:
    folder = tmp_path / "empty.zarr"
    folder.mkdir()
    not_dir = tmp_path / "file.zarr"
    not_dir.write_text("x")
    files: list = []
    probes = OmeZarrReader().probe([folder, not_dir], on_file=files.append)
    assert [p.path for p in probes] == [folder, not_dir]
    assert all(p.error for p in probes)
    assert files == probes


def test_probe_stops_when_cancelled(tmp_path) -> None:
    roots = [
        write_store(tmp_path / f"{i}.zarr", [_tczyx(i, (1, 1, 1, 4, 4))], levels=1)
        for i in range(3)
    ]
    probes = OmeZarrReader().probe(roots, on_file=None, is_cancelled=lambda: True)
    assert probes == []


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------


def test_fingerprint_ignores_finder_and_sidecar_files(tmp_path) -> None:
    root = write_store(tmp_path / "f.zarr", [_tczyx(12, (1, 1, 1, 4, 4))], levels=1)
    before = store_fingerprint(root)
    (root / ".DS_Store").write_bytes(b"\0" * 64)
    (root / "._0").write_bytes(b"\0" * 64)
    assert store_fingerprint(root) == before
    (probe,) = OmeZarrReader().probe([root])
    assert (probe.size_bytes, probe.mtime_ns) == before


def test_fingerprint_changes_when_the_array_header_is_rewritten(tmp_path) -> None:
    root = write_store(tmp_path / "g.zarr", [_tczyx(13, (1, 1, 1, 4, 4))], levels=1)
    before = store_fingerprint(root)
    header = root / "0" / "0" / ".zarray"
    header.write_text(header.read_text() + " ")
    assert store_fingerprint(root) != before


# ---------------------------------------------------------------------------
# Real data
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not IDR0168_STORE.is_dir(), reason="IDR0168 example store is not present")
def test_idr0168_example_probes_and_reads_one_plane_per_channel() -> None:
    reader = OmeZarrReader()
    (probe,) = reader.probe([IDR0168_STORE])
    assert probe.error is None
    (series,) = probe.series
    assert (series.size_t, series.size_c, series.size_z, series.size_y, series.size_x) == (
        1,
        4,
        49,
        2048,
        2048,
    )
    assert series.physical_x_um == pytest.approx(0.10049, abs=1e-5)
    assert series.physical_z_um == pytest.approx(0.19592, abs=1e-5)
    assert series.channel_names == (
        "YH_561_Cy3",
        "YH_647_CF40",
        "YH_405_DAPI",
        "YH_488_GFP_CF40_Sona",
    )
    source = suggest_scheme([probe]).sources[0]
    assert source.needs_confirmation == ""
    for c in range(4):
        plane = reader_mod.read_plane(source, t=0, c=c, z=24)
        assert plane.shape == (2048, 2048)
        assert plane.dtype == np.dtype("uint16")
        assert plane.min() < plane.max()


@pytest.mark.parametrize("codec", ["zlib", "zstd", "none"])
def test_synthetic_codecs_round_trip(tmp_path, codec: str) -> None:
    data = _tczyx(14, (1, 1, 2, 10, 9))
    root = write_store(
        tmp_path / f"{codec}.zarr", [data], chunks=(1, 1, 1, 4, 4), codec=codec, levels=1
    )
    planes = _planes(OmeZarrReader(), _source(root))
    for z in range(2):
        np.testing.assert_array_equal(planes[(0, 0, z)], data[0, 0, z])


def test_read_projected_ends_quietly_when_cancelled_before_a_stack(tmp_path) -> None:
    """A cancel landing in on_plane ends the iterator; it never raises."""
    data = _tczyx(15, (1, 2, 3, 8, 8))
    root = write_store(tmp_path / "c.zarr", [data], levels=1)
    flag: list[bool] = []
    out = list(
        OmeZarrReader().read_projected(
            _source(root),
            "mip",
            on_plane=lambda t, c: flag.append(True),
            is_cancelled=lambda: bool(flag),
        )
    )
    assert out == []

"""The routing reader sends OME-Zarr stores to the native reader, all else to Bio-Formats."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from tests.fakes.fake_image_reader import FakeImageReader, probe_for
from tests.fakes.omezarr_fixture import write_store

from percell4.adapters.infile_scan import shared_reader
from percell4.adapters.routing_reader import RoutingReader
from percell4.domain.io.infile import ImportSource, suggest_scheme
from percell4.ports.image_reader import ImageReader


def _no_bioformats():
    raise AssertionError("the Bio-Formats reader must not be built")


def _store(root: Path, seed: int = 0) -> tuple[Path, np.ndarray]:
    data = np.random.default_rng(seed).integers(0, 999, (1, 2, 2, 8, 8)).astype(">u2")
    return write_store(root, [data], levels=1), data


def test_routing_reader_satisfies_the_port() -> None:
    assert isinstance(RoutingReader(bioformats_factory=_no_bioformats), ImageReader)


def test_shared_reader_routes() -> None:
    assert isinstance(shared_reader(), RoutingReader)


def test_zarr_only_probe_and_read_never_build_bioformats(tmp_path) -> None:
    root, data = _store(tmp_path / "a.zarr")
    reader = RoutingReader(bioformats_factory=_no_bioformats)
    (probe,) = reader.probe([root])
    source = suggest_scheme([probe]).sources[0]
    planes = {(t, c, z): p for t, c, z, p in reader.read_planes(source)}
    np.testing.assert_array_equal(planes[(0, 1, 1)], data[0, 1, 1])
    projected = list(reader.read_projected(source, "mip"))
    assert [(t, c) for t, c, _ in projected] == [(0, 0), (0, 1)]
    reader.close()


def test_mixed_probe_keeps_input_order_and_on_file_order(tmp_path) -> None:
    z1, _ = _store(tmp_path / "z1.zarr", 1)
    z2, _ = _store(tmp_path / "z2.ome.zarr", 2)
    tif = tmp_path / "t.tif"
    czi = tmp_path / "c.czi"
    stack = np.zeros((1, 1, 2, 4, 4), dtype=np.uint16)
    fake = FakeImageReader([probe_for(tif, stack), probe_for(czi, stack)])
    built: list[FakeImageReader] = []

    def factory():
        built.append(fake)
        return fake

    reader = RoutingReader(bioformats_factory=factory)
    seen: list[Path] = []
    probes = reader.probe([tif, z1, czi, z2], on_file=lambda p: seen.append(p.path))
    assert [p.path for p in probes] == [tif, z1, czi, z2]
    assert seen == [tif, z1, czi, z2]
    assert [p.format_name for p in probes] == ["Fake", "OME-Zarr", "Fake", "OME-Zarr"]
    assert len(built) == 1  # built once, lazily


def test_reads_dispatch_on_the_source_path(tmp_path) -> None:
    tif = tmp_path / "t.tif"
    stack = np.arange(32, dtype=np.uint16).reshape(1, 1, 2, 4, 4)
    fake = FakeImageReader([probe_for(tif, stack)], arrays={(tif, 0): stack})
    reader = RoutingReader(bioformats_factory=lambda: fake)
    (probe,) = reader.probe([tif])
    source = suggest_scheme([probe]).sources[0]
    planes = list(reader.read_planes(source))
    np.testing.assert_array_equal(planes[1][3], stack[0, 0, 1])


def test_cancel_during_the_first_group_stops_the_probe(tmp_path) -> None:
    z1, _ = _store(tmp_path / "z1.zarr", 1)
    z2, _ = _store(tmp_path / "z2.zarr", 2)
    tif = tmp_path / "t.tif"
    seen: list = []
    reader = RoutingReader(bioformats_factory=_no_bioformats)
    probes = reader.probe([z1, z2, tif], on_file=seen.append, is_cancelled=lambda: bool(seen))
    # Cancelled after the first store: the rest are skipped, Bio-Formats never built.
    assert [p.path for p in probes] == [z1]


def test_close_closes_only_what_was_built(tmp_path) -> None:
    closed: list[str] = []

    class Closing(FakeImageReader):
        def close(self) -> None:
            closed.append("bf")

    reader = RoutingReader(bioformats_factory=_no_bioformats)
    reader.close()
    assert closed == []

    tif = tmp_path / "t.tif"
    stack = np.zeros((1, 1, 2, 4, 4), dtype=np.uint16)
    reader = RoutingReader(bioformats_factory=lambda: Closing([probe_for(tif, stack)]))
    reader.probe([tif])
    reader.close()
    assert closed == ["bf"]


@pytest.mark.parametrize("name", ["x.tif", "x.czi"])
def test_non_zarr_source_goes_to_bioformats(tmp_path, name: str) -> None:
    target = tmp_path / name
    stack = np.ones((1, 1, 2, 4, 4), dtype=np.uint16)
    fake = FakeImageReader([probe_for(target, stack)], arrays={(target, 0): stack})
    reader = RoutingReader(bioformats_factory=lambda: fake)
    source = ImportSource(path=target, series=fake.probe([target])[0].series[0])
    assert len(list(reader.read_planes(source))) == 2

"""Tests for the Bio-Formats reader port, its child host and the parent client.

Three groups:

* **fake** -- the in-memory fake satisfies the port (U4-U7 build on it).
* **fake transport** -- the real client against a JVM-free child that speaks
  the same pipe protocol, for the callback, cancel and kill logic.
* **JVM** -- the real reader. Every such test asks for ``jvm_available``,
  which starts the reader child and skips with the reason when it cannot.

Bio-Formats' test format needs no data: an empty file named
``test&sizeZ=5&sizeC=3&sizeT=2&sizeX=64&sizeY=48.fake`` describes itself.
"""

from __future__ import annotations

import os
import signal
from pathlib import Path

import numpy as np
import pytest
import tifffile
from tests.fakes.fake_image_reader import FakeImageReader, probe_for, project

from percell4.adapters import bioformats_reader
from percell4.adapters.bioformats_reader import BioformatsReader
from percell4.domain.errors import BioformatsUnavailableError, JavaUnavailableError
from percell4.domain.io.infile import (
    AXIS_ASSUMED,
    AXIS_METADATA,
    BIOFORMATS_SUFFIXES,
    AxisMap,
    FileProbe,
    ImportSource,
)
from percell4.ports.image_reader import ImageReader

FAKE_NAME = "test&sizeZ=5&sizeC=3&sizeT=2&sizeX=64&sizeY=48.fake"

IDR0089_DIR = Path("/Users/marcusjoshm/Documents/microscopy-data/idr/idr0089/20200625-ftp")
# The one local real-data file (a single ImageJ hyperstack, ZCYX 97x3x1024x1024).
# Never probe the folder as a set, and never download data for a test.
IDR0089_FILE_01 = "AC16_Rep2_8d24h_HNRNPC488_NUP594_01_SIR_THR_ALN.tif"


def _fake_file(folder: Path, name: str = FAKE_NAME) -> Path:
    path = folder / name
    path.touch()
    return path


def _source(probe: FileProbe, series_index: int = 0, **kwargs) -> ImportSource:
    return ImportSource(
        path=probe.path,
        series_index=series_index,
        series=probe.series[series_index],
        **kwargs,
    )


def _collect(reader, source, z_method):
    return {(t, c): plane for t, c, plane in reader.read_projected(source, z_method)}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# ---------------------------------------------------------------------------
# fake
# ---------------------------------------------------------------------------


def test_fake_reader_satisfies_the_port(tmp_path):
    stack = np.arange(2 * 3 * 4 * 5 * 6, dtype=np.uint16).reshape(2, 3, 4, 5, 6)
    path = tmp_path / "a.tif"
    probe = probe_for(path, stack, physical_z_um=0.2)
    reader = FakeImageReader([probe], arrays={(path, 0): stack})
    assert isinstance(reader, ImageReader)

    seen: list[FileProbe] = []
    other = tmp_path / "b.tif"
    probes = reader.probe([path, other], on_file=seen.append)
    assert [p.path for p in probes] == [path, other]
    assert seen == probes
    assert probes[1].error

    source = _source(probe, channel_indices=(2, 0))
    planes = list(reader.read_projected(source, "mip"))
    assert [(t, c) for t, c, _ in planes] == [(0, 2), (0, 0), (1, 2), (1, 0)]
    for t, c, plane in planes:
        assert plane.dtype == np.float32
        np.testing.assert_array_equal(plane, stack[t, c].max(axis=0))

    swapped = _source(probe, axis_map=AxisMap(z="T", t="Z"))
    planes = list(reader.read_projected(swapped, "sum"))
    assert len(planes) == 4 * 3  # effective T is the file's Z
    t, c, plane = planes[-1]
    np.testing.assert_allclose(plane, stack[:, c, t].astype(np.float64).sum(axis=0))

    reader.close()
    assert reader.closed


def test_real_client_satisfies_the_port():
    assert isinstance(BioformatsReader(java_home="/nowhere", jar="/nowhere.jar"), ImageReader)


def test_missing_java_raises_the_typed_error_with_its_reason(monkeypatch):
    from percell4.adapters.java_runtime import JarResolution, JavaEnvironment, JavaResolution

    def env(*, cache_dir=None):  # noqa: ARG001
        return JavaEnvironment(
            java=JavaResolution(None, None, None, "", "No working Java runtime was found."),
            jar=JarResolution(Path("/x.jar"), "cache", "ok"),
            cache_dir=Path("."),
            consent_current=False,
            pending_downloads=(),
            summary="",
        )

    monkeypatch.setattr(bioformats_reader, "describe_java_environment", env)
    with pytest.raises(JavaUnavailableError, match="No working Java"):
        BioformatsReader().probe([Path("x.tif")])


def test_missing_jar_raises_the_typed_error_with_its_reason(monkeypatch):
    from percell4.adapters.java_runtime import JarResolution, JavaEnvironment, JavaResolution

    def env(*, cache_dir=None):  # noqa: ARG001
        return JavaEnvironment(
            java=JavaResolution(Path("/j/bin/java"), Path("/j"), "cache", "21", "ok"),
            jar=JarResolution(None, None, "Bio-Formats 8.5.0 is not in the PerCell cache."),
            cache_dir=Path("."),
            consent_current=False,
            pending_downloads=(),
            summary="",
        )

    monkeypatch.setattr(bioformats_reader, "describe_java_environment", env)
    with pytest.raises(BioformatsUnavailableError, match="not in the PerCell cache"):
        BioformatsReader().probe([Path("x.tif")])


# ---------------------------------------------------------------------------
# fake transport (real client, JVM-free child)
# ---------------------------------------------------------------------------


def _transport_reader(**config) -> BioformatsReader:
    from tests.fakes import fake_bioformats_host

    return BioformatsReader(
        java_home="/unused",
        jar="/unused.jar",
        host_target=fake_bioformats_host.serve,
        host_config=config,
    )


def test_probe_calls_on_file_once_per_file(tmp_path):
    paths = [tmp_path / f"s{i}.tif" for i in range(3)]
    reader = _transport_reader(delay=0.01)
    try:
        seen: list[FileProbe] = []
        probes = reader.probe(paths, on_file=seen.append)
        assert [p.path for p in probes] == paths
        assert seen == probes
        assert all(p.format_name == "Fake transport" for p in probes)
    finally:
        reader.close()


def test_cancel_mid_probe_ends_the_call_and_kills_the_child(tmp_path):
    paths = [tmp_path / f"s{i}.tif" for i in range(20)]
    reader = _transport_reader(delay=0.2)
    try:
        seen: list[FileProbe] = []
        probes = reader.probe(paths, on_file=seen.append, is_cancelled=lambda: len(seen) >= 1)
        assert len(probes) == len(seen) == 1
        assert not reader.is_running

        again = reader.probe(paths[:2])  # a fresh child serves the next request
        assert [p.path for p in again] == paths[:2]
    finally:
        reader.close()


def test_fake_reader_streams_raw_planes_z_innermost(tmp_path):
    stack = np.arange(2 * 3 * 4 * 5 * 6, dtype=np.uint16).reshape(2, 3, 4, 5, 6)
    path = tmp_path / "a.tif"
    probe = probe_for(path, stack)
    reader = FakeImageReader([probe], arrays={(path, 0): stack})
    stacks: list[tuple[int, int]] = []
    source = _source(probe, channel_indices=(2, 0))
    planes = list(reader.read_planes(source, lambda t, c: stacks.append((t, c))))
    assert [(t, c, z) for t, c, z, _ in planes] == [
        (t, c, z) for t in range(2) for c in (2, 0) for z in range(4)
    ]
    assert stacks == [(0, 2), (0, 0), (1, 2), (1, 0)]
    for t, c, z, plane in planes:
        assert plane.dtype == np.uint16
        np.testing.assert_array_equal(plane, stack[t, c, z])


def test_stream_crosses_the_pipe_z_innermost(tmp_path):
    reader = _transport_reader(delay=0.0, stream_shape=(2, 2, 3, 4, 5))
    try:
        source = ImportSource(path=tmp_path / "x.tif", channel_indices=(1, 0))
        stacks: list[tuple[int, int]] = []
        planes = list(reader.read_planes(source, lambda t, c: stacks.append((t, c))))
        assert [(t, c, z) for t, c, z, _ in planes] == [
            (t, c, z) for t in range(2) for c in (1, 0) for z in range(3)
        ]
        assert stacks == [(0, 1), (0, 0), (1, 1), (1, 0)]
        for t, c, z, plane in planes:
            assert plane.shape == (4, 5) and plane.dtype == np.uint16
            assert int(plane[0, 0]) == 100 * t + 10 * c + z
    finally:
        reader.close()


def test_cancel_mid_stream_ends_the_iterator(tmp_path):
    reader = _transport_reader(delay=0.05, stream_shape=(1, 1, 50, 4, 5))
    try:
        seen = []
        source = ImportSource(path=tmp_path / "x.tif")
        for item in reader.read_planes(source, is_cancelled=lambda: len(seen) >= 2):
            seen.append(item)
        assert 2 <= len(seen) < 50
        assert not reader.is_running
    finally:
        reader.close()


# ---------------------------------------------------------------------------
# JVM
# ---------------------------------------------------------------------------


def test_fake_format_probes_to_its_declared_sizes(jvm_available, tmp_path):
    path = _fake_file(tmp_path)
    (probe,) = jvm_available.probe([path])
    assert probe.error is None
    assert probe.used_files == (path,)
    assert probe.size_bytes == 0
    (series,) = probe.series
    assert (series.size_z, series.size_c, series.size_t) == (5, 3, 2)
    assert (series.size_x, series.size_y) == (64, 48)
    assert series.pixel_type == "uint8"
    assert series.axis_source == AXIS_METADATA


def test_fake_format_projections_match_numpy_over_the_same_planes(jvm_available, tmp_path):
    name = "proj&pixelType=uint16&sizeZ=5&sizeC=2&sizeT=2&sizeX=32&sizeY=24.fake"
    path = _fake_file(tmp_path, name)
    (probe,) = jvm_available.probe([path])
    series = probe.series[0]
    source = _source(probe)
    for z_method in ("mip", "mean", "sum"):
        planes = _collect(jvm_available, source, z_method)
        assert sorted(planes) == [(t, c) for t in range(2) for c in range(2)]
        for (t, c), plane in planes.items():
            stack = np.stack(
                [jvm_available.read_plane(path, 0, z, c, t) for z in range(series.size_z)]
            )
            assert stack.dtype == np.uint16
            assert plane.dtype == np.float32 and plane.shape == (24, 32)
            if z_method == "mip":
                np.testing.assert_array_equal(plane, stack.max(axis=0))
            else:
                np.testing.assert_allclose(plane, project(stack, z_method), rtol=1e-6)
    # the planes differ across z (Bio-Formats stamps the indices), so max is not trivial
    stack = np.stack([jvm_available.read_plane(path, 0, z, 0, 0) for z in range(5)])
    assert not np.array_equal(stack[0], stack[-1])


def test_fake_format_stream_matches_read_plane(jvm_available, tmp_path):
    name = "stream&pixelType=uint16&sizeZ=4&sizeC=2&sizeT=2&sizeX=16&sizeY=12.fake"
    path = _fake_file(tmp_path, name)
    (probe,) = jvm_available.probe([path])
    planes = list(jvm_available.read_planes(_source(probe)))
    assert [(t, c, z) for t, c, z, _ in planes] == [
        (t, c, z) for t in range(2) for c in range(2) for z in range(4)
    ]
    for t, c, z, plane in planes:
        np.testing.assert_array_equal(plane, jvm_available.read_plane(path, 0, z, c, t))


def test_imagej_hyperstack_values_calibration_and_axis_swap(jvm_available, tmp_path):
    rng = np.random.default_rng(3)
    data = rng.integers(0, 4000, size=(2, 4, 3, 16, 20), dtype=np.uint16)  # T Z C Y X
    path = tmp_path / "hyper.tif"
    tifffile.imwrite(
        path,
        data,
        imagej=True,
        resolution=(1 / 0.041, 1 / 0.041),
        metadata={"axes": "TZCYX", "unit": "micron", "spacing": 0.125},
    )
    (probe,) = jvm_available.probe([path])
    (series,) = probe.series
    assert (series.size_t, series.size_z, series.size_c) == (2, 4, 3)
    assert series.physical_z_um == pytest.approx(0.125)
    assert series.physical_x_um == pytest.approx(0.041, rel=1e-4)
    assert series.axis_source == AXIS_METADATA

    tczyx = data.transpose(0, 2, 1, 3, 4)
    for z_method in ("mip", "mean", "sum"):
        planes = _collect(jvm_available, _source(probe, channel_indices=(2, 0)), z_method)
        assert sorted(planes) == [(0, 0), (0, 2), (1, 0), (1, 2)]
        for (t, c), plane in planes.items():
            np.testing.assert_allclose(plane, project(tczyx[t, c], z_method), rtol=1e-6)

    swapped = _source(probe, axis_map=AxisMap(z="T", t="Z"))
    planes = _collect(jvm_available, swapped, "mip")
    assert sorted(planes) == [(t, c) for t in range(4) for c in range(3)]
    for (t, c), plane in planes.items():
        np.testing.assert_array_equal(plane, tczyx[:, c, t].max(axis=0))


def test_imagej_zcyx_file_reports_its_z_spacing(jvm_available, tmp_path):
    path = tmp_path / "zcyx.tif"
    tifffile.imwrite(
        path,
        np.zeros((6, 3, 16, 20), np.uint16),
        imagej=True,
        metadata={"axes": "ZCYX", "unit": "micron", "spacing": 0.125},
    )
    (probe,) = jvm_available.probe([path])
    (series,) = probe.series
    assert (series.size_z, series.size_c, series.size_t) == (6, 3, 1)
    assert series.physical_z_um == pytest.approx(0.125)
    assert series.axis_source == AXIS_METADATA


def test_plain_multipage_tiff_axes_are_assumed_and_unitless_size_dropped(
    jvm_available, tmp_path
):
    path = tmp_path / "plain.tif"
    tifffile.imwrite(path, np.zeros((5, 16, 20), np.uint16), photometric="minisblack")
    single = tmp_path / "single.tif"
    tifffile.imwrite(single, np.zeros((16, 20), np.uint16))
    probe, single_probe = jvm_available.probe([path, single])
    (series,) = probe.series
    assert series.size_z * series.size_t == 5
    assert series.axis_source == AXIS_ASSUMED
    # tifffile writes resolution 1/1 with ResolutionUnit NONE: not a size in µm
    assert series.physical_x_um is None and series.physical_y_um is None
    assert single_probe.series[0].axis_source == AXIS_METADATA  # one plane: nothing assumed


def test_rgb_and_big_endian_tiffs_read_correct_values(jvm_available, tmp_path):
    rgb = np.arange(16 * 20 * 3, dtype=np.uint8).reshape(16, 20, 3)
    rgb_path = tmp_path / "rgb.tif"
    tifffile.imwrite(rgb_path, rgb, photometric="rgb")
    be = np.arange(3 * 16 * 20, dtype=">u2").reshape(3, 16, 20) * 7
    be_path = tmp_path / "be.tif"
    tifffile.imwrite(be_path, be, byteorder=">", photometric="minisblack")

    rgb_probe, be_probe = jvm_available.probe([rgb_path, be_path])
    series = rgb_probe.series[0]
    assert series.is_rgb and series.size_c == 3
    planes = _collect(jvm_available, _source(rgb_probe), "mip")
    for c in range(3):
        np.testing.assert_array_equal(planes[(0, c)], rgb[..., c])

    source = _source(be_probe)
    if source.series.size_z == 1:  # plain TIFF pages land in T; read them as Z
        source = _source(be_probe, axis_map=AxisMap(z="T", t="Z"))
    ((_, _, plane),) = list(jvm_available.read_projected(source, "sum"))
    np.testing.assert_array_equal(plane, be.astype(np.float64).sum(axis=0).astype(np.float32))


def test_multi_series_file_probes_each_series(jvm_available, tmp_path):
    path = _fake_file(tmp_path, "multi&series=2&sizeZ=3&sizeC=2&sizeX=16&sizeY=8.fake")
    (probe,) = jvm_available.probe([path])
    assert [s.index for s in probe.series] == [0, 1]
    planes = _collect(jvm_available, _source(probe, series_index=1), "mip")
    assert sorted(planes) == [(0, 0), (0, 1)]


def test_numbered_hyperstacks_each_report_one_used_file(jvm_available, tmp_path):
    paths = []
    for i in (1, 2, 3):
        path = tmp_path / f"stack_{i:02d}.tif"
        tifffile.imwrite(
            path,
            np.full((4, 2, 8, 8), i, np.uint16),
            imagej=True,
            metadata={"axes": "ZCYX", "unit": "micron", "spacing": 0.2},
        )
        paths.append(path)
    probes = jvm_available.probe(paths)
    for path, probe in zip(paths, probes, strict=True):
        assert probe.error is None
        assert probe.used_files == (path,)
        assert probe.series[0].size_z == 4


def test_junk_tiff_is_an_error_string_and_the_host_survives(jvm_available, tmp_path):
    junk = tmp_path / "junk.tif"
    junk.write_bytes(b"this is not a TIFF" * 20)
    good = _fake_file(tmp_path)
    (bad,) = jvm_available.probe([junk])
    pid = jvm_available.child_pid
    assert bad.series == ()
    assert bad.error and "\n" not in bad.error
    assert "FormatException" in bad.error

    (ok,) = jvm_available.probe([good])
    assert ok.error is None
    assert jvm_available.child_pid == pid


def test_cancel_mid_read_ends_the_iterator_and_a_fresh_child_serves_next(
    jvm_available, tmp_path
):
    path = _fake_file(tmp_path)
    (probe,) = jvm_available.probe([path])
    first_pid = jvm_available.child_pid
    got: list[tuple[int, int]] = []
    planes = list(
        jvm_available.read_projected(
            _source(probe),
            "mip",
            on_plane=lambda t, c: got.append((t, c)),
            is_cancelled=lambda: len(got) >= 1,
        )
    )
    assert 1 <= len(planes) < 6
    assert not _alive(first_pid)

    planes = _collect(jvm_available, _source(probe), "mip")
    assert len(planes) == 6
    assert jvm_available.child_pid != first_pid


def test_a_crashed_child_is_replaced_on_the_next_request(jvm_available, tmp_path):
    path = _fake_file(tmp_path)
    jvm_available.probe([path])
    pid = jvm_available.child_pid
    os.kill(pid, signal.SIGKILL)
    (probe,) = jvm_available.probe([path])
    assert probe.error is None
    assert jvm_available.child_pid not in (None, pid)

    os.kill(jvm_available.child_pid, signal.SIGKILL)
    assert len(_collect(jvm_available, _source(probe), "mip")) == 6


def test_probe_writes_no_memo_file(jvm_available, tmp_path):
    path = tmp_path / "stack.tif"
    data = np.zeros((3, 2, 8, 8), np.uint16)
    tifffile.imwrite(path, data, imagej=True, metadata={"axes": "ZCYX"})
    jvm_available.probe([path, _fake_file(tmp_path)])
    assert not [n for n in os.listdir(tmp_path) if "bfmemo" in n]


def test_closing_the_client_ends_the_child(jvm_available, tmp_path):
    reader = BioformatsReader(java_home=jvm_available.java_home, jar=jvm_available.jar)
    reader.probe([_fake_file(tmp_path)])
    pid = reader.child_pid
    assert pid is not None and _alive(pid)
    reader.close()
    assert not reader.is_running
    assert not _alive(pid)


def test_heap_setting_reaches_the_child_jvm(jvm_available):
    reader = BioformatsReader(
        java_home=jvm_available.java_home, jar=jvm_available.jar, max_heap_mb=300
    )
    try:
        info = reader.start()
    finally:
        reader.close()
    # maxMemory reports a little over -Xmx; the default heap is a quarter of RAM
    assert info["max_heap_bytes"] < 400 * 1024 * 1024


def test_a_java_home_without_a_jvm_raises_the_typed_error(tmp_path):
    reader = BioformatsReader(java_home=tmp_path, jar=tmp_path / "bioformats_package.jar")
    with pytest.raises(JavaUnavailableError, match="No JVM library"):
        reader.start()
    assert not reader.is_running


def test_pinned_suffixes_are_claimed_by_the_loaded_reader(jvm_available):
    claimed = {"." + s.lower() for s in jvm_available.suffixes() if s}
    assert BIOFORMATS_SUFFIXES <= claimed, sorted(BIOFORMATS_SUFFIXES - claimed)


@pytest.mark.slow
@pytest.mark.skipif(
    not (IDR0089_DIR / IDR0089_FILE_01).is_file(), reason="IDR0089 example file is not present"
)
def test_idr0089_example_matches_its_imagej_metadata(jvm_available):
    (probe,) = jvm_available.probe([IDR0089_DIR / IDR0089_FILE_01])
    assert probe.error is None
    (series,) = probe.series
    assert (series.size_z, series.size_c, series.size_t) == (97, 3, 1)
    assert (series.size_y, series.size_x) == (1024, 1024)
    assert series.physical_x_um == pytest.approx(1 / 24.390243, rel=1e-6)
    assert series.physical_z_um == pytest.approx(0.125)
    assert series.axis_source == AXIS_METADATA
    assert probe.used_files == (IDR0089_DIR / IDR0089_FILE_01,)

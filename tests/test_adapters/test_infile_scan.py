"""The shared selection scan: stage one without Java, then the reader probe.

The dialog and ``percell-import`` both call :func:`scan_selection`, so a
selection classifies the same way on both surfaces.
"""

from __future__ import annotations

import numpy as np
import pytest
import tifffile
from tests.fakes.fake_image_reader import FakeImageReader, probe_for

from percell4.adapters.infile_scan import (
    expand_selection,
    scan_selection,
    tiff_plane_count,
)
from percell4.domain.io.infile import REASON_MULTI_PLANE, REASON_SINGLE_PLANE
from percell4.domain.io.models import DiscoveryMode


def _plane(path, shape=(8, 8)):
    tifffile.imwrite(path, np.zeros(shape, dtype=np.uint16))
    return path


def _hyperstack(path, z=4, c=2):
    tifffile.imwrite(
        path,
        np.zeros((z, c, 8, 8), dtype=np.uint16),
        imagej=True,
        metadata={"axes": "ZCYX", "spacing": 0.5, "unit": "micron"},
    )
    return path


# ── tiff_plane_count ──────────────────────────────────────────────────


def test_single_plane_tiff_counts_one(tmp_path):
    assert tiff_plane_count(_plane(tmp_path / "a.tif")) == 1


def test_rgb_single_page_counts_one(tmp_path):
    p = tmp_path / "rgb.tif"
    tifffile.imwrite(p, np.zeros((8, 8, 3), dtype=np.uint8), photometric="rgb")
    assert tiff_plane_count(p) == 1


def test_imagej_hyperstack_counts_every_plane(tmp_path):
    assert tiff_plane_count(_hyperstack(tmp_path / "h.tif", z=4, c=2)) == 8


def test_plain_multipage_counts_pages(tmp_path):
    p = tmp_path / "mp.tif"
    with tifffile.TiffWriter(p) as w:
        for _ in range(3):
            w.write(np.zeros((8, 8), dtype=np.uint16), contiguous=False)
    assert tiff_plane_count(p) == 3


def test_ome_tiff_counts_its_planes(tmp_path):
    p = tmp_path / "o.ome.tif"
    tifffile.imwrite(p, np.zeros((3, 2, 8, 8), dtype=np.uint16), ome=True,
                     metadata={"axes": "ZCYX"})
    assert tiff_plane_count(p) == 6


def test_unreadable_tiff_raises(tmp_path):
    p = tmp_path / "junk.tif"
    p.write_bytes(b"not a tiff at all")
    with pytest.raises(Exception):  # noqa: B017 - any read error becomes a reason upstream
        tiff_plane_count(p)


# ── expand_selection ──────────────────────────────────────────────────


def test_directory_expands_to_its_files_non_recursively_without_sidecars(tmp_path):
    _plane(tmp_path / "b.tif")
    _plane(tmp_path / "a.tif")
    (tmp_path / "._a.tif").write_bytes(b"x")
    (tmp_path / "sub").mkdir()
    _plane(tmp_path / "sub" / "deep.tif")

    assert expand_selection([tmp_path]) == [tmp_path / "a.tif", tmp_path / "b.tif"]


def test_files_keep_their_order_and_duplicates_collapse(tmp_path):
    a, b = _plane(tmp_path / "a.tif"), _plane(tmp_path / "b.tif")
    assert expand_selection([b, a, b]) == [b, a]


# ── scan_selection ────────────────────────────────────────────────────


def test_hyperstack_directory_suggests_infile_and_probes(tmp_path):
    paths = [_hyperstack(tmp_path / f"s{i}.tif") for i in range(3)]
    stack = np.zeros((1, 2, 4, 8, 8), dtype=np.uint16)
    reader = FakeImageReader([probe_for(p, stack, physical_z_um=0.5) for p in paths])

    outcome = scan_selection([tmp_path], reader_factory=lambda: reader)

    assert outcome.stage_one.suggested_mode is DiscoveryMode.INFILE
    assert [s.path for s in outcome.scheme.sources] == paths
    assert reader.probe_calls == [paths]


def test_mostly_single_plane_directory_suggests_flat_and_never_probes(tmp_path):
    """Covers AE2 (stage one): a majority of planes means no probe and no Java."""
    _hyperstack(tmp_path / "stack.tif")
    for i in range(3):
        _plane(tmp_path / f"img_ch{i:02d}.tif")
    (tmp_path / "notes.docx").write_bytes(b"x")

    def must_not_build():
        raise AssertionError("the reader must not be built for a legacy suggestion")

    outcome = scan_selection([tmp_path], reader_factory=must_not_build)

    assert outcome.stage_one.suggested_mode is DiscoveryMode.FLAT
    assert outcome.scheme is None
    reasons = {e.path.name: e.reason for e in outcome.stage_one.excluded}
    assert reasons["stack.tif"] == REASON_MULTI_PLANE
    assert reasons["notes.docx"] == "format not recognised"


def test_forced_infile_probes_and_merges_stage_one_exclusions(tmp_path):
    stack_path = _hyperstack(tmp_path / "stack.tif")
    _plane(tmp_path / "img_ch00.tif")
    _plane(tmp_path / "img_ch01.tif")
    reader = FakeImageReader([probe_for(stack_path, np.zeros((1, 2, 4, 8, 8)))])

    outcome = scan_selection(
        [tmp_path], reader_factory=lambda: reader, mode=DiscoveryMode.INFILE
    )

    assert [s.path for s in outcome.scheme.sources] == [stack_path]
    reasons = {e.path.name: e.reason for e in outcome.scheme.excluded}
    assert reasons == {"img_ch00.tif": REASON_SINGLE_PLANE, "img_ch01.tif": REASON_SINGLE_PLANE}


def test_on_file_and_cancel_reach_the_reader(tmp_path):
    paths = [_hyperstack(tmp_path / f"s{i}.tif") for i in range(3)]
    reader = FakeImageReader([probe_for(p, np.zeros((1, 2, 4, 8, 8))) for p in paths])
    seen = []

    outcome = scan_selection(
        [tmp_path],
        reader_factory=lambda: reader,
        on_file=seen.append,
        is_cancelled=lambda: len(seen) >= 2,
    )

    assert len(seen) == 2
    assert outcome.cancelled

"""Tests for the in-file import domain model.

Covers stage-one pre-classification (no reader, no JVM) and
``suggest_scheme`` over plain probe records. Everything here is pure: the
header reader is an injected fake keyed by file name.
"""

from __future__ import annotations

import ast
import math
from pathlib import Path

import pytest

import percell4.domain.io.infile as infile_mod
from percell4.domain.io.infile import (
    AXIS_ASSUMED,
    AXIS_NAMED,
    BIOFORMATS_SUFFIXES,
    REASON_BIN_IN_INFILE,
    REASON_MULTI_FILE,
    REASON_MULTI_PLANE,
    REASON_POSSIBLE_T_AS_Z,
    REASON_POSSIBLE_Z_AS_T,
    REASON_SINGLE_PLANE,
    REASON_UNSUPPORTED,
    AxisMap,
    FileProbe,
    ImportScheme,
    SeriesProbe,
    apply_axis_map,
    confirm_source,
    dataset_stem,
    is_zarr_path,
    preclassify,
    reassign_axes,
    suggest_scheme,
)
from percell4.domain.io.models import DiscoveryMode

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _series(
    index: int = 0,
    *,
    t: int = 1,
    c: int = 3,
    z: int = 1,
    y: int = 1024,
    x: int = 1024,
    pz: float | None = 0.125,
    axis_source: str = "metadata",
    is_rgb: bool = False,
    is_interleaved: bool = False,
) -> SeriesProbe:
    return SeriesProbe(
        index=index,
        name=f"series {index}",
        size_t=t,
        size_c=c,
        size_z=z,
        size_y=y,
        size_x=x,
        dimension_order="XYCZT",
        pixel_type="float",
        is_rgb=is_rgb,
        is_interleaved=is_interleaved,
        physical_x_um=0.041,
        physical_y_um=0.041,
        physical_z_um=pz,
        channel_names=tuple(str(i) for i in range(c)),
        axis_source=axis_source,
    )


def _probe(
    name: str,
    *series: SeriesProbe,
    used: int = 1,
    error: str | None = None,
    folder: str = "/data",
) -> FileProbe:
    path = Path(folder) / name
    return FileProbe(
        path=path,
        size_bytes=485_000_000,
        mtime_ns=1_700_000_000_000_000_000,
        format_name="Tagged Image File Format",
        series=tuple(series),
        used_files=tuple(Path(folder) / f"{name}.{i}" for i in range(used))
        if used > 1
        else (path,),
        error=error,
    )


def _plane_reader(counts: dict[str, int]):
    """Fake header reader: declared plane count by file name."""
    calls: list[str] = []

    def read(path: Path) -> int:
        calls.append(path.name)
        return counts[path.name]

    read.calls = calls  # type: ignore[attr-defined]
    return read


def _reasons(entries) -> dict[str, str]:
    return {e.path.name: e.reason for e in entries}


# ---------------------------------------------------------------------------
# Stage one
# ---------------------------------------------------------------------------


def _ae2_selection() -> tuple[list[Path], dict[str, int]]:
    d = Path("/data")
    stacks = ["stackA.tif", "stackB.tif"]
    singles = [f"cells_ch0{i}_t0{j}.tif" for i in range(2) for j in range(3)]
    paths = [d / n for n in stacks + singles] + [d / "._x.tif", d / "notes.docx"]
    counts = {n: 231 for n in stacks} | {n: 1 for n in singles}
    return paths, counts


def test_AE2_mixed_selection_suggests_flat_and_excludes_stacks() -> None:
    paths, counts = _ae2_selection()
    read = _plane_reader(counts)

    result = preclassify(paths, read)

    assert result.suggested_mode is DiscoveryMode.FLAT
    assert result.mode is DiscoveryMode.FLAT
    reasons = _reasons(result.excluded)
    assert reasons["stackA.tif"] == REASON_MULTI_PLANE
    assert reasons["stackB.tif"] == REASON_MULTI_PLANE
    assert REASON_MULTI_PLANE == "multi-plane file, import with In-file mode"
    assert reasons["notes.docx"] == REASON_UNSUPPORTED
    assert "._x.tif" not in reasons
    assert all(p.name != "._x.tif" for p in result.legacy + result.candidates)
    assert len(result.legacy) == 6
    # The sidecar never reaches the header reader.
    assert "._x.tif" not in read.calls


def test_AE2_forced_infile_mode_excludes_single_plane_tiffs() -> None:
    paths, counts = _ae2_selection()

    result = preclassify(paths, _plane_reader(counts), mode=DiscoveryMode.INFILE)

    assert result.suggested_mode is DiscoveryMode.FLAT
    assert result.mode is DiscoveryMode.INFILE
    assert [p.name for p in result.candidates] == ["stackA.tif", "stackB.tif"]
    reasons = _reasons(result.excluded)
    singles = [n for n in reasons if n.startswith("cells_")]
    assert len(singles) == 6
    assert all(reasons[n] == REASON_SINGLE_PLANE for n in singles)
    assert REASON_SINGLE_PLANE == "single-plane series, import with Flat or Subdirectory mode"
    assert reasons["notes.docx"] == REASON_UNSUPPORTED


def test_stacks_in_majority_suggest_infile() -> None:
    d = Path("/data")
    paths = [d / "a.tif", d / "b.tif", d / "c.tif", d / "single.tif"]
    counts = {"a.tif": 5, "b.tif": 5, "c.tif": 5, "single.tif": 1}

    result = preclassify(paths, _plane_reader(counts))

    assert result.suggested_mode is DiscoveryMode.INFILE
    assert len(result.candidates) == 3
    assert _reasons(result.excluded) == {"single.tif": REASON_SINGLE_PLANE}


def test_only_importable_group_is_candidates_suggests_infile() -> None:
    d = Path("/data")
    result = preclassify([d / "a.czi", d / "b.docx"], _plane_reader({}))
    assert result.suggested_mode is DiscoveryMode.INFILE


def test_tie_prefers_legacy_mode() -> None:
    d = Path("/data")
    counts = {"a.tif": 4, "b.tif": 1}
    result = preclassify([d / "a.tif", d / "b.tif"], _plane_reader(counts))
    assert result.suggested_mode is DiscoveryMode.FLAT


def test_legacy_files_in_several_folders_suggest_subdirectory() -> None:
    counts = {"a.tif": 1, "b.tif": 1}
    paths = [Path("/data/one/a.tif"), Path("/data/two/b.tif")]
    result = preclassify(paths, _plane_reader(counts))
    assert result.suggested_mode is DiscoveryMode.SUBDIRECTORY


def test_czi_is_candidate_and_docx_is_unsupported_without_header_read() -> None:
    read = _plane_reader({})
    d = Path("/data")

    result = preclassify([d / "img.czi", d / "notes.docx"], read, mode=DiscoveryMode.INFILE)

    assert ".czi" in BIOFORMATS_SUFFIXES
    assert [p.name for p in result.candidates] == ["img.czi"]
    assert _reasons(result.excluded) == {"notes.docx": REASON_UNSUPPORTED}
    assert read.calls == []


def test_suffix_match_is_case_insensitive_and_compound() -> None:
    d = Path("/data")
    counts = {"A.OME.TIF": 3}
    result = preclassify([d / "A.OME.TIF", d / "b.ND2"], _plane_reader(counts))
    assert sorted(p.name for p in result.candidates) == ["A.OME.TIF", "b.ND2"]


def test_bin_is_legacy_and_excluded_in_infile_mode() -> None:
    d = Path("/data")
    counts = {"a.tif": 1}
    paths = [d / "a.tif", d / "a_ch1.bin"]

    legacy = preclassify(paths, _plane_reader(counts))
    assert legacy.suggested_mode is DiscoveryMode.FLAT
    assert {p.name for p in legacy.legacy} == {"a.tif", "a_ch1.bin"}
    assert legacy.excluded == ()

    forced = preclassify(paths, _plane_reader(counts), mode=DiscoveryMode.INFILE)
    assert _reasons(forced.excluded)["a_ch1.bin"] == REASON_BIN_IN_INFILE


def test_companion_and_nd_sets_are_multi_file() -> None:
    d = Path("/data")
    paths = [
        d / "plate.companion.ome",
        d / "plate_0.ome.tif",
        d / "run.nd",
        d / "run_w1.stk",
        d / "other.tif",
    ]
    counts = {"plate_0.ome.tif": 10, "other.tif": 10}

    result = preclassify(paths, _plane_reader(counts), mode=DiscoveryMode.INFILE)

    reasons = _reasons(result.excluded)
    assert reasons["plate.companion.ome"] == REASON_MULTI_FILE
    assert reasons["plate_0.ome.tif"] == REASON_MULTI_FILE
    assert reasons["run.nd"] == REASON_MULTI_FILE
    assert reasons["run_w1.stk"] == REASON_MULTI_FILE
    assert [p.name for p in result.candidates] == ["other.tif"]


def test_header_read_failure_is_excluded_as_unreadable() -> None:
    def read(path: Path) -> int:
        raise ValueError("not a TIFF file")

    result = preclassify([Path("/data/bad.tif")], read, mode=DiscoveryMode.INFILE)

    assert result.candidates == ()
    assert result.legacy == ()
    assert "not a TIFF file" in _reasons(result.excluded)["bad.tif"]


# ---------------------------------------------------------------------------
# suggest_scheme
# ---------------------------------------------------------------------------


def test_AE1_twelve_stacks_with_varying_z() -> None:
    probes = [
        _probe(f"cell{i:02d}.tif", _series(c=3, z=65 + i)) for i in range(12)
    ]

    scheme = suggest_scheme(probes, "mip")

    assert isinstance(scheme, ImportScheme)
    assert scheme.z_method == "mip"
    assert len(scheme.sources) == 12
    assert len(scheme.importable_sources) == 12
    assert all(s.needs_confirmation == "" for s in scheme.sources)
    assert all(s.channel_indices == (0, 1, 2) for s in scheme.sources)
    assert [s.output_name for s in scheme.sources][:2] == ["cell00", "cell01"]
    assert scheme.excluded == ()
    assert len(scheme.warnings) == 1
    assert "z-count varies" in scheme.warnings[0]


def test_sum_with_varying_z_warns_not_comparable() -> None:
    probes = [_probe("a.tif", _series(z=10)), _probe("b.tif", _series(z=12))]

    summed = suggest_scheme(probes, "sum")
    assert any("z-count varies" in w for w in summed.warnings)
    assert any("summed intensities are not comparable" in w for w in summed.warnings)

    mip = suggest_scheme(probes, "mip")
    assert len(mip.warnings) == 1
    assert "z-count varies" in mip.warnings[0]


def test_sum_with_constant_z_has_no_warning() -> None:
    probes = [_probe("a.tif", _series(z=10)), _probe("b.tif", _series(z=10))]
    assert suggest_scheme(probes, "sum").warnings == ()


def test_unknown_z_method_is_rejected() -> None:
    with pytest.raises(ValueError, match="z method"):
        suggest_scheme([], "none")


def test_AE7_z_without_spacing_is_flagged_then_reassigned_to_t() -> None:
    scheme = suggest_scheme([_probe("s.tif", _series(c=1, z=50, pz=None))], "mip")

    (src,) = scheme.sources
    assert src.needs_confirmation == REASON_POSSIBLE_T_AS_Z
    assert REASON_POSSIBLE_T_AS_Z == "possible T stored as Z"
    assert scheme.importable_sources == ()

    fixed = reassign_axes(src, AxisMap(z="T", t="Z"))
    eff = fixed.effective
    assert (eff.size_t, eff.size_z) == (50, 1)
    assert fixed.needs_confirmation == ""
    assert fixed.series == src.series  # the raw probe is never rewritten


def test_AE3_t_with_spacing_is_flagged_then_reassigned_to_z() -> None:
    scheme = suggest_scheme([_probe("s.tif", _series(t=77, z=1, pz=0.125))], "mip")

    (src,) = scheme.sources
    assert src.needs_confirmation == REASON_POSSIBLE_Z_AS_T
    assert REASON_POSSIBLE_Z_AS_T == "possible Z stored as T"

    fixed = reassign_axes(src, AxisMap(z="T", t="Z"))
    eff = fixed.effective
    assert (eff.size_z, eff.size_t) == (77, 1)
    assert eff.physical_z_um == 0.125
    assert fixed.needs_confirmation == ""


def test_confirm_clears_flag_without_changing_axes() -> None:
    scheme = suggest_scheme([_probe("s.tif", _series(t=77, z=1, pz=0.125))], "mip")
    confirmed = confirm_source(scheme.sources[0])
    assert confirmed.needs_confirmation == ""
    assert confirmed.axis_map == AxisMap()
    assert confirmed.effective.size_t == 77


def test_apply_axis_map_moves_physical_z_with_the_z_role() -> None:
    raw = _series(t=1, z=20, pz=0.3)
    swapped = apply_axis_map(raw, AxisMap(z="T", t="Z"))
    assert (swapped.size_t, swapped.size_z) == (20, 1)
    assert swapped.physical_z_um is None
    assert apply_axis_map(raw, AxisMap()) == raw


def test_axis_map_must_be_a_permutation() -> None:
    with pytest.raises(ValueError):
        AxisMap(z="Z", t="Z")
    with pytest.raises(ValueError):
        AxisMap(z="Q", t="T")


def test_assumed_axes_are_flagged_and_default_to_z() -> None:
    probe = _probe("pages.tif", _series(c=1, z=30, pz=None, axis_source=AXIS_ASSUMED))

    (src,) = suggest_scheme([probe], "mip").sources

    assert src.needs_confirmation != ""
    assert "assumed" in src.needs_confirmation
    assert src.axis_map == AxisMap()
    assert src.effective.size_z == 30


def test_channel_count_outlier_is_flagged_with_warning() -> None:
    probes = [_probe(f"f{i}.tif", _series(c=3, z=5)) for i in range(3)]
    probes.append(_probe("odd.tif", _series(c=4, z=5)))

    scheme = suggest_scheme(probes, "mip")

    odd = next(s for s in scheme.sources if s.output_name == "odd")
    assert "channel count" in odd.needs_confirmation
    assert odd not in scheme.importable_sources
    assert len(scheme.importable_sources) == 3
    assert any("odd" in w and "channel" in w for w in scheme.warnings)


def test_multi_series_names_and_collision_warning() -> None:
    three = _probe("stem.lif", _series(0), _series(1), _series(2))
    scheme = suggest_scheme([three], "mip")
    assert [s.output_name for s in scheme.sources] == ["stem_s00", "stem_s01", "stem_s02"]
    assert [s.series_index for s in scheme.sources] == [0, 1, 2]
    assert scheme.warnings == ()

    a1 = _probe("a.lif", _series(0), _series(1), folder="/one")
    a2 = _probe("a.czi", _series(0), _series(1), folder="/two")
    clash = suggest_scheme([a1, a2], "mip")
    assert any("a_s00" in w and "collision" in w for w in clash.warnings)


def test_single_plane_single_channel_gives_one_clean_source() -> None:
    scheme = suggest_scheme([_probe("one.czi", _series(t=1, c=1, z=1, pz=None))], "mip")
    (src,) = scheme.sources
    assert src.needs_confirmation == ""
    assert src.channel_indices == (0,)
    assert scheme.warnings == ()
    assert scheme.excluded == ()


def test_rgb_interleaved_gives_three_channels() -> None:
    probe = _probe("rgb.png", _series(c=3, is_rgb=True, is_interleaved=True, pz=None))
    (src,) = suggest_scheme([probe], "mip").sources
    assert src.channel_indices == (0, 1, 2)


def test_multi_file_probe_is_excluded() -> None:
    scheme = suggest_scheme([_probe("set.ome.tif", _series(), used=3)], "mip")
    assert scheme.sources == ()
    assert _reasons(scheme.excluded) == {"set.ome.tif": REASON_MULTI_FILE}


def test_probe_error_is_excluded_with_its_message() -> None:
    msg = "loci.formats.FormatException: Unsupported compression"
    scheme = suggest_scheme([_probe("bad.czi", error=msg)], "mip")
    assert scheme.sources == ()
    assert _reasons(scheme.excluded) == {"bad.czi": msg}


def test_source_carries_expected_size_and_mtime() -> None:
    (src,) = suggest_scheme([_probe("a.tif", _series(z=3))], "mip").sources
    assert src.expected_size == 485_000_000
    assert src.expected_mtime_ns == 1_700_000_000_000_000_000


def test_element_count_is_exact_for_large_series() -> None:
    s = _series(t=200, c=4, z=100, y=2048, x=2048)
    assert s.element_count == 200 * 100 * 4 * 2048 * 2048
    assert s.element_count == math.prod((200, 4, 100, 2048, 2048))
    assert s.element_count > 2**31


def test_dataset_stem_strips_compound_suffixes() -> None:
    assert dataset_stem(Path("/d/a.ome.tif")) == "a"
    assert dataset_stem(Path("/d/a.OME.TIFF")) == "a"
    assert dataset_stem(Path("/d/a.b.czi")) == "a.b"
    assert dataset_stem(Path("/d/a.ome.zarr")) == "a"
    assert dataset_stem(Path("/d/a.zarr")) == "a"


def test_zarr_stores_are_in_file_candidates_without_a_header_read() -> None:
    read = _plane_reader({})
    result = preclassify(
        [Path("/d/a.zarr"), Path("/d/b.OME.ZARR"), Path("/d/._a.zarr")], read
    )
    assert result.candidates == (Path("/d/a.zarr"), Path("/d/b.OME.ZARR"))
    assert result.suggested_mode is DiscoveryMode.INFILE
    assert read.calls == []


def test_legacy_modes_exclude_zarr_stores_as_multi_plane() -> None:
    result = preclassify([Path("/d/a.zarr")], _plane_reader({}), mode=DiscoveryMode.FLAT)
    assert _reasons(result.excluded) == {"a.zarr": REASON_MULTI_PLANE}


def test_is_zarr_path_matches_the_suffix_only() -> None:
    assert is_zarr_path(Path("/d/x.zarr"))
    assert is_zarr_path(Path("/d/x.ome.zarr"))
    assert not is_zarr_path(Path("/d/.zarr"))
    assert not is_zarr_path(Path("/d/x.zarr.tif"))


@pytest.mark.parametrize(
    ("t", "z", "pz"),
    [
        (1, 5, None),  # z axis with no or unknown unit
        (3, 1, 0.2),  # time-lapse with a size-1 z that has a unit
    ],
)
def test_named_axes_are_never_flagged_as_ambiguous(t: int, z: int, pz) -> None:
    series = _series(t=t, z=z, pz=pz, axis_source=AXIS_NAMED)
    (src,) = suggest_scheme([_probe("a.zarr", series)], "mip").sources
    assert src.needs_confirmation == ""


def test_module_imports_nothing_forbidden() -> None:
    tree = ast.parse(Path(infile_mod.__file__).read_text())
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    forbidden = ("percell4.adapters", "qtpy", "PyQt5", "h5py", "jpype", "tifffile")
    assert not [n for n in names if n.startswith(forbidden)]


def test_with_z_method_adds_and_removes_the_sum_warning():
    from percell4.domain.io.infile import with_z_method

    probes = [
        FileProbe(path=Path(f"/d/{i}.tif"), series=(SeriesProbe(index=0, size_c=2, size_z=z,
                                                               physical_z_um=0.5),),
                  used_files=(Path(f"/d/{i}.tif"),))
        for i, z in enumerate((5, 7))
    ]
    scheme = suggest_scheme(probes, "mip")
    assert not any("summed" in w for w in scheme.warnings)
    summed = with_z_method(scheme, "sum")
    assert summed.z_method == "sum"
    assert sum("summed" in w for w in summed.warnings) == 1
    assert sum("z-count varies" in w for w in summed.warnings) == 1
    back = with_z_method(summed, "mean")
    assert not any("summed" in w for w in back.warnings)

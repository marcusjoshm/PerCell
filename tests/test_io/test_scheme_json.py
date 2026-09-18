"""Tests for the in-file import scheme JSON format."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from percell4.domain.errors import ImportSchemeError, PercellError
from percell4.domain.io.infile import (
    AxisMap,
    FileProbe,
    SeriesProbe,
    reassign_axes,
    suggest_scheme,
)
from percell4.domain.io.scheme_json import (
    SCHEME_VERSION,
    dumps,
    from_dict,
    loads,
    to_dict,
)


def _scheme():
    probes = [
        FileProbe(
            path=Path("/data/a.tif"),
            size_bytes=123,
            mtime_ns=456,
            format_name="TIFF",
            series=(
                SeriesProbe(
                    index=0,
                    name="a",
                    size_t=77,
                    size_c=3,
                    size_z=1,
                    size_y=64,
                    size_x=32,
                    physical_x_um=0.041,
                    physical_y_um=0.041,
                    physical_z_um=0.125,
                    channel_names=("488", "561", "640"),
                ),
            ),
            used_files=(Path("/data/a.tif"),),
        ),
        FileProbe(
            path=Path("/data/b.lif"),
            size_bytes=9,
            mtime_ns=10,
            format_name="Leica",
            series=(
                SeriesProbe(index=0, size_c=2, size_z=4, physical_z_um=0.2),
                SeriesProbe(index=1, size_c=2, size_z=5, physical_z_um=0.2),
            ),
            used_files=(Path("/data/b.lif"),),
        ),
        FileProbe(
            path=Path("/data/bad.czi"),
            size_bytes=1,
            mtime_ns=2,
            format_name="",
            error="boom",
        ),
    ]
    scheme = suggest_scheme(probes, "sum")
    first = reassign_axes(scheme.sources[0], AxisMap(z="T", t="Z"))
    return scheme.replace_source(0, first)


def test_round_trip_is_unchanged() -> None:
    scheme = _scheme()
    assert scheme.sources and scheme.excluded and scheme.warnings

    assert loads(dumps(scheme)) == scheme
    assert from_dict(to_dict(scheme)) == scheme


def test_dumps_carries_version() -> None:
    data = json.loads(dumps(_scheme()))
    assert data["version"] == SCHEME_VERSION


def test_unknown_version_is_rejected() -> None:
    data = to_dict(_scheme())
    data["version"] = SCHEME_VERSION + 1
    with pytest.raises(ImportSchemeError, match="version"):
        from_dict(data)


def test_missing_version_is_rejected() -> None:
    data = to_dict(_scheme())
    del data["version"]
    with pytest.raises(ImportSchemeError, match="version"):
        from_dict(data)


def test_missing_optional_keys_take_defaults() -> None:
    data = {
        "version": SCHEME_VERSION,
        "sources": [{"path": "/data/a.tif", "output_name": "a"}],
    }

    scheme = from_dict(data)

    assert scheme.z_method == "mip"
    assert scheme.excluded == ()
    assert scheme.warnings == ()
    (src,) = scheme.sources
    assert src.path == Path("/data/a.tif")
    assert src.series_index == 0
    assert src.axis_map == AxisMap()
    assert src.channel_indices == ()
    assert src.included is True
    assert src.needs_confirmation == ""
    assert src.expected_size is None
    assert src.expected_mtime_ns is None


@pytest.mark.parametrize("name", ["../x", "/abs/x", "a/b", "a\\b", "", "..", "x/.."])
def test_bad_output_names_are_rejected_on_load(name: str) -> None:
    data = to_dict(_scheme())
    data["sources"][0]["output_name"] = name
    with pytest.raises(ImportSchemeError, match="output name"):
        from_dict(data)


def test_bad_z_method_is_rejected() -> None:
    data = to_dict(_scheme())
    data["z_method"] = "none"
    with pytest.raises(ImportSchemeError, match="z method"):
        from_dict(data)


def test_malformed_json_raises_scheme_error() -> None:
    with pytest.raises(ImportSchemeError):
        loads("{not json")
    with pytest.raises(ImportSchemeError):
        loads("[]")


def test_bad_axis_map_raises_scheme_error() -> None:
    data = to_dict(_scheme())
    data["sources"][0]["axis_map"] = {"Z": "Z", "T": "Z"}
    with pytest.raises(ImportSchemeError):
        from_dict(data)


def test_scheme_error_is_a_percell_error() -> None:
    assert issubclass(ImportSchemeError, PercellError)


def test_single_source_round_trips_and_rejects_bad_names():
    from percell4.domain.io.infile import ImportSource, SeriesProbe
    from percell4.domain.io.scheme_json import source_from_dict, source_to_dict

    src = ImportSource(path=Path("/d/a.tif"), series_index=1, channel_indices=(0, 2),
                       output_name="a_s01", series=SeriesProbe(index=1, size_c=3, size_z=4))
    assert source_from_dict(source_to_dict(src)) == src
    bad = {**source_to_dict(src), "output_name": "../escape"}
    with pytest.raises(ImportSchemeError):
        source_from_dict(bad)
    with pytest.raises(ImportSchemeError):
        source_from_dict(["not", "a", "dict"])

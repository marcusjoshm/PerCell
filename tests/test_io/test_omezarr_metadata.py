"""Tests for the pure OME-Zarr metadata parser (``percell4.domain.io.omezarr``).

Every store here is a dict of metadata keys, so no file is opened. The parser
reads JSON and OME-XML through injected callables and never sees a chunk.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

import percell4.domain.io.omezarr as omezarr_mod
from percell4.domain.io.infile import AXIS_NAMED
from percell4.domain.io.omezarr import (
    REASON_HCS,
    REASON_NGFF_VERSION,
    REASON_NO_MULTISCALES,
    REASON_ZARR_V3,
    parse_store,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_IDR0168_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06">'
    '<Image ID="Image:0" Name="MCF7_1 Resolution Level 1">'
    '<Pixels BigEndian="true" DimensionOrder="XYZCT" ID="Pixels:0" SizeC="4" SizeT="1"'
    ' SizeX="2048" SizeY="2048" SizeZ="49" Type="uint16">'
    '<Channel ID="Channel:0:0" Name="YH_561_Cy3"/>'
    '<Channel ID="Channel:0:1" Name="YH_647_CF40"/>'
    '<Channel ID="Channel:0:2" Name="YH_405_DAPI"/>'
    '<Channel ID="Channel:0:3" Name="YH_488_GFP_CF40_Sona"/>'
    "</Pixels></Image></OME>"
)

_TCZYX = [
    {"name": "t", "type": "time"},
    {"name": "c", "type": "channel"},
    {"name": "z", "type": "space", "unit": "micrometer"},
    {"name": "y", "type": "space", "unit": "micrometer"},
    {"name": "x", "type": "space", "unit": "micrometer"},
]


def _multiscales(axes, scales, *, version="0.4", name="img", top_scale=None) -> dict:
    ms = {
        "version": version,
        "name": name,
        "axes": axes,
        "datasets": [
            {"path": str(i), "coordinateTransformations": [{"type": "scale", "scale": s}]}
            for i, s in enumerate(scales)
        ],
    }
    if top_scale is not None:
        ms["coordinateTransformations"] = [{"type": "scale", "scale": top_scale}]
    return {"multiscales": [ms]}


def _zarray(shape, chunks, *, dtype=">u2", compressor=None, filters=None, sep="/") -> dict:
    return {
        "zarr_format": 2,
        "shape": list(shape),
        "chunks": list(chunks),
        "dtype": dtype,
        "compressor": compressor
        if compressor is not None
        else {"id": "blosc", "cname": "lz4", "clevel": 5, "shuffle": 1, "blocksize": 0},
        "fill_value": 0,
        "filters": filters,
        "order": "C",
        "dimension_separator": sep,
    }


def _idr0168_files() -> dict[str, object]:
    """The IDR0168 example's metadata, copied from the store."""
    scales = [
        [1.0, 1.0, 0.1959183673469462, 0.10048828124999964, 0.10048828125000142],
        [1.0, 1.0, 0.1959183673469462, 0.2009765624999993, 0.20097656250000284],
    ]
    return {
        ".zgroup": {"zarr_format": 2},
        ".zattrs": {"bioformats2raw.layout": 3},
        "OME/.zgroup": {"zarr_format": 2},
        "OME/.zattrs": {"series": ["0"]},
        "OME/METADATA.ome.xml": _IDR0168_XML,
        "0/.zgroup": {"zarr_format": 2},
        "0/.zattrs": _multiscales(_TCZYX, scales),
        "0/0/.zarray": _zarray((1, 4, 49, 2048, 2048), (1, 1, 1, 1024, 1024)),
        "0/1/.zarray": _zarray((1, 4, 49, 1024, 1024), (1, 1, 1, 1024, 1024)),
    }


def _single_image(axes, shape, scale, *, omero=None, **zarray_kw) -> dict[str, object]:
    attrs = _multiscales(axes, [scale])
    if omero is not None:
        attrs["omero"] = omero
    return {
        ".zgroup": {"zarr_format": 2},
        ".zattrs": attrs,
        "0/.zarray": _zarray(shape, shape, **zarray_kw),
    }


def _parse(files: dict[str, object]):
    reads: list[str] = []

    def read_json(key: str):
        reads.append(key)
        value = files.get(key)
        return None if value is None or isinstance(value, str) else json.loads(json.dumps(value))

    def read_text(key: str):
        reads.append(key)
        value = files.get(key)
        return value if isinstance(value, str) else None

    store = parse_store(read_json, read_text)
    return store, reads


def _axes(*names: str, unit: str | None = "micrometer") -> list[dict]:
    kinds = {"t": "time", "c": "channel", "z": "space", "y": "space", "x": "space"}
    out = []
    for n in names:
        axis = {"name": n, "type": kinds[n]}
        if kinds[n] == "space" and unit is not None:
            axis["unit"] = unit
        out.append(axis)
    return out


# ---------------------------------------------------------------------------
# The IDR0168 example (AE1)
# ---------------------------------------------------------------------------


def test_idr0168_metadata_gives_the_expected_series() -> None:
    store, reads = _parse(_idr0168_files())
    assert store.error is None
    (series,) = store.series
    probe = series.probe
    assert (probe.size_t, probe.size_c, probe.size_z, probe.size_y, probe.size_x) == (
        1,
        4,
        49,
        2048,
        2048,
    )
    assert probe.pixel_type == "uint16"
    assert probe.physical_x_um == pytest.approx(0.10049, abs=1e-5)
    assert probe.physical_y_um == pytest.approx(0.10049, abs=1e-5)
    assert probe.physical_z_um == pytest.approx(0.19592, abs=1e-5)
    assert probe.channel_names == (
        "YH_561_Cy3",
        "YH_647_CF40",
        "YH_405_DAPI",
        "YH_488_GFP_CF40_Sona",
    )
    assert probe.name == "MCF7_1 Resolution Level 1"
    assert probe.axis_source == AXIS_NAMED
    assert not probe.is_rgb
    # Level 0 only: the second pyramid level's header is never read.
    assert "0/1/.zarray" not in reads
    assert series.array.path == "0/0"
    assert series.array.axes == ("t", "c", "z", "y", "x")
    assert series.array.separator == "/"
    assert series.array.dtype == ">u2"


# ---------------------------------------------------------------------------
# Axes, units and channel names
# ---------------------------------------------------------------------------


def test_omero_labels_win_over_ome_xml() -> None:
    files = _single_image(
        _axes("c", "y", "x"),
        (2, 8, 8),
        [1.0, 0.5, 0.5],
        omero={"channels": [{"label": "DAPI"}, {"label": "GFP"}]},
    )
    files["OME/METADATA.ome.xml"] = _IDR0168_XML
    store, _ = _parse(files)
    assert store.series[0].probe.channel_names == ("DAPI", "GFP")


def test_channel_names_of_the_wrong_length_are_dropped() -> None:
    files = _single_image(
        _axes("c", "y", "x"), (2, 8, 8), [1.0, 0.5, 0.5], omero={"channels": [{"label": "A"}]}
    )
    store, _ = _parse(files)
    assert store.series[0].probe.channel_names == ()


def test_cyx_store_has_single_t_and_z() -> None:
    store, _ = _parse(_single_image(_axes("c", "y", "x"), (3, 16, 32), [1.0, 0.2, 0.3]))
    probe = store.series[0].probe
    assert (probe.size_t, probe.size_c, probe.size_z, probe.size_y, probe.size_x) == (
        1,
        3,
        1,
        16,
        32,
    )
    assert probe.physical_z_um is None
    assert probe.physical_x_um == pytest.approx(0.3)
    assert probe.physical_y_um == pytest.approx(0.2)


def test_zyx_store_has_single_channel() -> None:
    store, _ = _parse(_single_image(_axes("z", "y", "x"), (5, 8, 8), [0.4, 0.1, 0.1]))
    probe = store.series[0].probe
    assert (probe.size_c, probe.size_z) == (1, 5)
    assert probe.physical_z_um == pytest.approx(0.4)
    assert store.series[0].array.axes == ("z", "y", "x")


def test_nanometer_units_convert_to_micrometers() -> None:
    files = _single_image(_axes("z", "y", "x", unit="nanometer"), (2, 8, 8), [300, 65, 65])
    probe = _parse(files)[0].series[0].probe
    assert probe.physical_z_um == pytest.approx(0.3)
    assert probe.physical_x_um == pytest.approx(0.065)


@pytest.mark.parametrize("unit", [None, "parsec-ish"])
def test_missing_or_unknown_unit_leaves_calibration_unset(unit) -> None:
    files = _single_image(_axes("z", "y", "x", unit=unit), (2, 8, 8), [0.3, 0.1, 0.1])
    probe = _parse(files)[0].series[0].probe
    assert probe.physical_x_um is None
    assert probe.physical_y_um is None
    assert probe.physical_z_um is None


def test_multiscale_level_scale_multiplies_into_the_dataset_scale() -> None:
    files = _single_image(_axes("z", "y", "x"), (2, 8, 8), [1.0, 1.0, 1.0])
    files[".zattrs"]["multiscales"][0]["coordinateTransformations"] = [
        {"type": "scale", "scale": [0.5, 0.25, 0.125]}
    ]
    probe = _parse(files)[0].series[0].probe
    assert probe.physical_z_um == pytest.approx(0.5)
    assert probe.physical_x_um == pytest.approx(0.125)


def test_bioformats2raw_series_follow_the_ome_series_list() -> None:
    files = _idr0168_files()
    files["OME/.zattrs"] = {"series": ["0", "1"]}
    files["1/.zattrs"] = _multiscales(_TCZYX, [[1, 1, 0.5, 0.2, 0.2]])
    files["1/0/.zarray"] = _zarray((2, 1, 3, 64, 64), (1, 1, 1, 64, 64))
    files["OME/METADATA.ome.xml"] = _IDR0168_XML.replace(
        "</Image></OME>",
        '</Image><Image ID="Image:1" Name="second"><Pixels ID="Pixels:1" SizeC="1" '
        'SizeT="2" SizeX="64" SizeY="64" SizeZ="3" Type="uint16" DimensionOrder="XYZCT">'
        '<Channel ID="Channel:1:0" Name="only"/></Pixels></Image></OME>',
    )
    store, _ = _parse(files)
    assert [s.probe.index for s in store.series] == [0, 1]
    assert store.series[1].probe.name == "second"
    assert store.series[1].probe.size_t == 2
    assert store.series[1].probe.channel_names == ("only",)
    assert store.series[1].array.path == "1/0"


def test_bioformats2raw_without_a_series_list_counts_numbered_groups() -> None:
    files = _idr0168_files()
    del files["OME/.zattrs"]
    files["1/.zattrs"] = _multiscales(_TCZYX, [[1, 1, 0.5, 0.2, 0.2]])
    files["1/0/.zarray"] = _zarray((1, 1, 1, 8, 8), (1, 1, 1, 8, 8))
    store, reads = _parse(files)
    assert [s.array.path for s in store.series] == ["0/0", "1/0"]
    assert "2/.zattrs" in reads  # stops at the first missing group


@pytest.mark.parametrize(
    ("dtype", "pixel_type"),
    [
        ("|u1", "uint8"),
        ("<i2", "int16"),
        (">u4", "uint32"),
        ("<f4", "float"),
        (">f8", "double"),
        ("|b1", "bit"),
    ],
)
def test_supported_dtypes_map_to_reader_pixel_types(dtype: str, pixel_type: str) -> None:
    files = _single_image(_axes("y", "x"), (8, 8), [0.1, 0.1], dtype=dtype)
    assert _parse(files)[0].series[0].probe.pixel_type == pixel_type


# ---------------------------------------------------------------------------
# Exclusions (AE4)
# ---------------------------------------------------------------------------


def test_zarr_v3_store_is_excluded() -> None:
    store, reads = _parse({"zarr.json": {"zarr_format": 3, "node_type": "group"}})
    assert store.error == REASON_ZARR_V3
    assert store.series == ()


def test_hcs_plate_is_excluded() -> None:
    store, _ = _parse({".zgroup": {"zarr_format": 2}, ".zattrs": {"plate": {"wells": []}}})
    assert store.error == REASON_HCS


def test_group_without_multiscales_is_excluded() -> None:
    store, _ = _parse({".zgroup": {"zarr_format": 2}, ".zattrs": {}})
    assert store.error == REASON_NO_MULTISCALES


def test_other_ngff_versions_are_excluded() -> None:
    files = _single_image(_axes("y", "x"), (8, 8), [0.1, 0.1])
    files[".zattrs"]["multiscales"][0]["version"] = "0.3"
    store, _ = _parse(files)
    assert store.error == REASON_NGFF_VERSION.format(version="0.3")


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"filters": [{"id": "delta", "dtype": "<u2"}]}, "filter"),
        ({"compressor": {"id": "bz2"}}, "bz2"),
        ({"dtype": "<i8"}, "pixel type"),
        ({"dtype": "<c8"}, "pixel type"),
        ({"dtype": [["r", "|u1"], ["g", "|u1"]]}, "pixel type"),
    ],
)
def test_unsupported_arrays_get_a_reason(override: dict, fragment: str) -> None:
    files = _single_image(_axes("y", "x"), (8, 8), [0.1, 0.1])
    files["0/.zarray"].update(override)
    store, _ = _parse(files)
    assert store.series == ()
    assert fragment in store.error


@pytest.mark.parametrize(
    "mutate",
    [
        lambda f: f[".zattrs"]["multiscales"][0].update(datasets=[]),
        lambda f: f[".zattrs"]["multiscales"][0].update(axes=[{"name": "q"}, {"name": "x"}]),
        lambda f: f["0/.zarray"].update(shape=[8, 8, 8]),
        lambda f: f.pop("0/.zarray"),
        lambda f: f[".zattrs"].update(multiscales="nonsense"),
    ],
)
def test_malformed_metadata_gives_a_reason_and_never_raises(mutate) -> None:
    files = _single_image(_axes("y", "x"), (8, 8), [0.1, 0.1])
    mutate(files)
    store, _ = _parse(files)
    assert store.series == ()
    assert store.error


def test_not_a_zarr_group_is_excluded() -> None:
    store, _ = _parse({})
    assert store.error


def test_module_imports_nothing_forbidden() -> None:
    tree = ast.parse(Path(omezarr_mod.__file__).read_text())
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    forbidden = ("percell4.adapters", "qtpy", "PyQt5", "h5py", "imagecodecs", "numpy")
    assert not [n for n in names if n.startswith(forbidden)]

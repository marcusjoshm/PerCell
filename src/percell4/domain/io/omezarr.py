"""OME-Zarr metadata: store JSON and OME-XML -> series probes and array specs.

An OME-Zarr store is a directory. Its metadata lives in small JSON files
(``.zgroup``, ``.zattrs``, ``.zarray``) and, for stores written by
bioformats2raw, in ``OME/METADATA.ome.xml``. This module turns that metadata
into the :class:`SeriesProbe` records in-file import already understands,
plus one :class:`ZarrArraySpec` per series that tells the reader how to find
and decode the full-resolution chunks.

Scope: Zarr v2 with OME-NGFF 0.4 metadata, as a single image or a
bioformats2raw multi-series store. Everything else gets a readable reason
instead of a series: Zarr v3 / NGFF 0.5, HCS plates, other NGFF versions,
filters, unknown compressors and unsupported pixel types.

Only the full-resolution level (``datasets[0]``) is read. The lower pyramid
levels are never opened, not even their headers.

Pure domain: stdlib only. File access is injected as two callables keyed by
the path relative to the store root, so nothing here opens a file or a chunk.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from percell4.domain.io.infile import AXIS_NAMED, SeriesProbe

#: Reads one JSON metadata file by store-relative key; None when missing or unreadable.
ReadJson = Callable[[str], "dict[str, Any] | None"]

#: Reads one text file by store-relative key; None when missing or unreadable.
ReadText = Callable[[str], "str | None"]

NGFF_VERSION = "0.4"

REASON_NOT_ZARR = "not a Zarr v2 store (no .zgroup)"
REASON_ZARR_V3 = "Zarr v3 / OME-Zarr 0.5 store, not supported yet"
REASON_HCS = "OME-Zarr plate (HCS) store, not supported"
REASON_NO_MULTISCALES = "no OME-NGFF image (multiscales) metadata"
REASON_NGFF_VERSION = "OME-NGFF version {version} not supported, only " + NGFF_VERSION
REASON_NO_SERIES = "no image series in store"

#: Compressor ids the reader can decode. ``None`` means uncompressed.
SUPPORTED_COMPRESSORS = frozenset({"blosc", "zstd", "zlib", "gzip", "lz4"})

#: Zarr dtype kind+size -> the pixel type name Bio-Formats uses.
_PIXEL_TYPES = {
    "b1": "bit",
    "u1": "uint8",
    "i1": "int8",
    "u2": "uint16",
    "i2": "int16",
    "u4": "uint32",
    "i4": "int32",
    "f4": "float",
    "f8": "double",
}

#: NGFF space units (UDUNITS-2 names) -> micrometers per unit.
_UM_PER_UNIT = {
    "angstrom": 1e-4,
    "nanometer": 1e-3,
    "micrometer": 1.0,
    "micron": 1.0,
    "millimeter": 1e3,
    "centimeter": 1e4,
    "meter": 1e6,
}

_AXES = ("t", "c", "z", "y", "x")


@dataclass(frozen=True)
class ZarrArraySpec:
    """How to read the full-resolution array of one series.

    ``path`` is the array's key relative to the store root (``"0/0"`` for the
    first bioformats2raw series). ``axes`` names each array dimension, lower
    case, in array order. ``dtype`` is the Zarr dtype string, byte order
    included (``">u2"``).
    """

    path: str
    shape: tuple[int, ...]
    chunks: tuple[int, ...]
    dtype: str
    compressor: dict[str, Any] | None
    order: str
    separator: str
    fill_value: float | int | bool | None
    axes: tuple[str, ...]

    def axis(self, name: str) -> int | None:
        """Array dimension of axis ``name``, or None when the array lacks it."""
        return self.axes.index(name) if name in self.axes else None

    def chunk_key(self, index: tuple[int, ...]) -> str:
        """Store-relative key of the chunk at grid ``index``."""
        return f"{self.path}/{self.separator.join(str(i) for i in index)}"


@dataclass(frozen=True)
class ZarrSeries:
    """One image series of a store: its probe record and level-0 array."""

    probe: SeriesProbe
    array: ZarrArraySpec


@dataclass(frozen=True)
class ZarrStore:
    """A parsed store. ``error`` is set, and ``series`` empty, when unsupported."""

    series: tuple[ZarrSeries, ...] = ()
    error: str | None = None


class _UnsupportedError(Exception):
    """A readable reason the store cannot be imported."""


def parse_store(read_json: ReadJson, read_text: ReadText) -> ZarrStore:
    """Parse a store's metadata. Never raises: problems become ``error``."""
    try:
        return ZarrStore(series=tuple(_parse(read_json, read_text)))
    except _UnsupportedError as exc:
        return ZarrStore(error=str(exc))
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
        return ZarrStore(error=f"malformed OME-Zarr metadata: {exc}")


def series_array(read_json: ReadJson, read_text: ReadText, series_index: int) -> ZarrArraySpec:
    """The level-0 array of series ``series_index``, parsed from the store.

    The reader calls this at read time, so a replayed scheme needs nothing
    from an earlier probe. Raises ``ValueError`` when the store no longer
    parses or lacks the series.
    """
    store = parse_store(read_json, read_text)
    if store.error:
        raise ValueError(store.error)
    for s in store.series:
        if s.probe.index == series_index:
            return s.array
    raise ValueError(f"store has no series {series_index}")


# ---------------------------------------------------------------------------
# Store layout
# ---------------------------------------------------------------------------


def _parse(read_json: ReadJson, read_text: ReadText) -> list[ZarrSeries]:
    if read_json("zarr.json") is not None:
        raise _UnsupportedError(REASON_ZARR_V3)
    if read_json(".zgroup") is None:
        raise _UnsupportedError(REASON_NOT_ZARR)
    root = read_json(".zattrs") or {}
    if "plate" in root or "well" in root:
        raise _UnsupportedError(REASON_HCS)

    xml = _ome_xml(read_text("OME/METADATA.ome.xml"))
    if "multiscales" in root:
        groups = [""]
    elif "bioformats2raw.layout" in root:
        groups = _bioformats2raw_groups(read_json)
    else:
        raise _UnsupportedError(REASON_NO_MULTISCALES)
    if not groups:
        raise _UnsupportedError(REASON_NO_SERIES)

    out = []
    for index, group in enumerate(groups):
        attrs = root if group == "" else read_json(f"{group}/.zattrs")
        if not attrs or "multiscales" not in attrs:
            raise _UnsupportedError(f"series {group!r}: {REASON_NO_MULTISCALES}")
        names = xml[index] if index < len(xml) else ("", ())
        out.append(_series(index, group, attrs, names, read_json))
    return out


def _bioformats2raw_groups(read_json: ReadJson) -> list[str]:
    """Series groups: the ``OME/.zattrs`` list, else numbered groups from 0."""
    listed = (read_json("OME/.zattrs") or {}).get("series")
    if isinstance(listed, list) and listed:
        return [str(g) for g in listed]
    groups = []
    while read_json(f"{len(groups)}/.zattrs") is not None:
        groups.append(str(len(groups)))
    return groups


def _ome_xml(text: str | None) -> list[tuple[str, tuple[str, ...]]]:
    """(image name, channel names) per OME-XML Image, in document order."""
    if not text:
        return []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    images = []
    for image in root.iter():
        if _local(image.tag) != "Image":
            continue
        channels = tuple(ch.get("Name", "") for ch in image.iter() if _local(ch.tag) == "Channel")
        images.append((image.get("Name", ""), channels))
    return images


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


# ---------------------------------------------------------------------------
# One series
# ---------------------------------------------------------------------------


def _series(
    index: int,
    group: str,
    attrs: dict[str, Any],
    xml: tuple[str, tuple[str, ...]],
    read_json: ReadJson,
) -> ZarrSeries:
    ms = attrs["multiscales"][0]
    version = str(ms.get("version", ""))
    if version != NGFF_VERSION:
        raise _UnsupportedError(REASON_NGFF_VERSION.format(version=version or "unknown"))

    axes = _axes(ms["axes"])
    datasets = ms["datasets"]
    if not datasets:
        raise _UnsupportedError("multiscales lists no datasets")
    level0 = datasets[0]
    path = f"{group}/{level0['path']}" if group else str(level0["path"])

    header = read_json(f"{path}/.zarray")
    if header is None:
        raise _UnsupportedError(f"missing array header {path}/.zarray")
    array = _array(path, header, tuple(a["name"] for a in axes))

    scale = _scale(level0.get("coordinateTransformations"), len(axes))
    top = _scale(ms.get("coordinateTransformations"), len(axes))
    scale = [a * b for a, b in zip(scale, top)]
    physical = {
        axis["name"]: _to_um(scale[i], axis.get("unit"))
        for i, axis in enumerate(axes)
        if axis["name"] in ("z", "y", "x")
    }

    def size(name: str) -> int:
        dim = array.axis(name)
        return 1 if dim is None else int(array.shape[dim])

    size_c = size("c")
    xml_name, xml_channels = xml
    channel_names = _channel_names(attrs.get("omero"), size_c) or (
        xml_channels if len(xml_channels) == size_c else ()
    )
    size_z = size("z")
    probe = SeriesProbe(
        index=index,
        name=xml_name or str(ms.get("name", "")),
        size_t=size("t"),
        size_c=size_c,
        size_z=size_z,
        size_y=size("y"),
        size_x=size("x"),
        dimension_order="XY" + "".join(a.upper() for a in reversed(array.axes) if a in "zct"),
        pixel_type=_pixel_type(array.dtype),
        physical_x_um=physical.get("x"),
        physical_y_um=physical.get("y"),
        physical_z_um=physical.get("z") if size_z > 1 else None,
        channel_names=channel_names,
        axis_source=AXIS_NAMED,
    )
    return ZarrSeries(probe=probe, array=array)


def _axes(raw: Any) -> list[dict[str, Any]]:
    """Validated NGFF 0.4 axes: 2 to 5 unique names from t, c, z, y, x, ending y, x."""
    axes = [a if isinstance(a, dict) else {"name": a} for a in raw]
    names = [str(a.get("name", "")).lower() for a in axes]
    if (
        not 2 <= len(names) <= 5
        or len(set(names)) != len(names)
        or any(n not in _AXES for n in names)
        or names[-2:] != ["y", "x"]
    ):
        raise _UnsupportedError(f"unsupported axes {names}")
    return [{**a, "name": n} for a, n in zip(axes, names)]


def _array(path: str, header: dict[str, Any], axes: tuple[str, ...]) -> ZarrArraySpec:
    if header.get("zarr_format") != 2:
        raise _UnsupportedError(f"{path}: not a Zarr v2 array")
    shape = tuple(int(n) for n in header["shape"])
    chunks = tuple(int(n) for n in header["chunks"])
    if len(shape) != len(axes) or len(chunks) != len(axes):
        raise _UnsupportedError(f"{path}: array has {len(shape)} dimensions, axes name {len(axes)}")
    if any(n < 1 for n in chunks):
        raise _UnsupportedError(f"{path}: invalid chunk shape {list(chunks)}")
    dtype = header["dtype"]
    if not isinstance(dtype, str) or _pixel_type(dtype) is None:
        raise _UnsupportedError(f"{path}: pixel type {dtype!r} not supported")
    if header.get("filters"):
        ids = [f.get("id", "?") for f in header["filters"]]
        raise _UnsupportedError(f"{path}: Zarr filter {ids} not supported")
    compressor = header.get("compressor")
    if compressor is not None and compressor.get("id") not in SUPPORTED_COMPRESSORS:
        raise _UnsupportedError(f"{path}: compressor {compressor.get('id')!r} not supported")
    order = header.get("order", "C")
    if order not in ("C", "F"):
        raise _UnsupportedError(f"{path}: memory order {order!r} not supported")
    separator = header.get("dimension_separator") or "."
    if separator not in (".", "/"):
        raise _UnsupportedError(f"{path}: dimension separator {separator!r} not supported")
    return ZarrArraySpec(
        path=path,
        shape=shape,
        chunks=chunks,
        dtype=dtype,
        compressor=compressor,
        order=order,
        separator=separator,
        fill_value=header.get("fill_value"),
        axes=axes,
    )


def _pixel_type(dtype: str) -> str | None:
    """Bio-Formats pixel type name of a Zarr dtype string, or None if unsupported."""
    if len(dtype) < 3 or dtype[0] not in "<>|":
        return None
    return _PIXEL_TYPES.get(dtype[1:])


def _scale(transforms: Any, n: int) -> list[float]:
    """The ``scale`` transform of a list, or ones when there is none."""
    for t in transforms or ():
        if t.get("type") == "scale":
            values = [float(v) for v in t["scale"]]
            if len(values) != n:
                raise _UnsupportedError(f"scale has {len(values)} values for {n} axes")
            return values
    return [1.0] * n


def _to_um(value: float, unit: Any) -> float | None:
    """``value`` in µm, or None when the unit is missing, unknown or the value bad."""
    factor = _UM_PER_UNIT.get(str(unit).lower()) if unit else None
    if factor is None or not math.isfinite(value) or value <= 0:
        return None
    return value * factor


def _channel_names(omero: Any, size_c: int) -> tuple[str, ...]:
    """``omero.channels[].label`` when there is one per channel, else ()."""
    try:
        labels = tuple(str(ch.get("label", "")) for ch in omero["channels"])
    except (TypeError, KeyError, AttributeError):
        return ()
    if len(labels) != size_c or not all(labels):
        return ()
    return labels

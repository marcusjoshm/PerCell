"""The one JSON format for an in-file :class:`ImportScheme`.

The GUI, ``percell-import`` and workflow replay all read and write schemes
through this module. The format carries a ``version`` field. An unknown
version is rejected rather than guessed at. Missing optional keys take their
defaults, so a hand-edited scheme can stay short.

Output names are checked on load. A name that is empty, absolute, or holds a
path separator or ``..`` would write outside the output folder, so it raises
:class:`~percell4.domain.errors.ImportSchemeError`.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from percell4.domain.errors import ImportSchemeError
from percell4.domain.io.infile import (
    AXIS_METADATA,
    SCHEME_VERSION,
    Z_METHODS,
    AxisMap,
    ExcludedEntry,
    ImportScheme,
    ImportSource,
    SeriesProbe,
)

__all__ = [
    "SCHEME_VERSION",
    "dumps",
    "from_dict",
    "loads",
    "to_dict",
    "validate_output_name",
]


def validate_output_name(name: str) -> str:
    """Return ``name`` if it is a safe bare file stem, else raise."""
    if not isinstance(name, str) or not name.strip():
        raise ImportSchemeError(f"output name must not be empty, got {name!r}")
    if "/" in name or "\\" in name:
        raise ImportSchemeError(f"output name {name!r} must not contain a path separator")
    if name in (".", "..") or ".." in name.split("."):
        raise ImportSchemeError(f"output name {name!r} must not contain '..'")
    if PurePosixPath(name).is_absolute() or PureWindowsPath(name).is_absolute():
        raise ImportSchemeError(f"output name {name!r} must not be absolute")
    return name


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


def _series_to_dict(s: SeriesProbe) -> dict[str, Any]:
    return {
        "index": s.index,
        "name": s.name,
        "size_t": s.size_t,
        "size_c": s.size_c,
        "size_z": s.size_z,
        "size_y": s.size_y,
        "size_x": s.size_x,
        "dimension_order": s.dimension_order,
        "pixel_type": s.pixel_type,
        "is_rgb": s.is_rgb,
        "is_interleaved": s.is_interleaved,
        "physical_x_um": s.physical_x_um,
        "physical_y_um": s.physical_y_um,
        "physical_z_um": s.physical_z_um,
        "channel_names": list(s.channel_names),
        "axis_source": s.axis_source,
    }


def _source_to_dict(src: ImportSource) -> dict[str, Any]:
    return {
        "path": str(src.path),
        "series_index": src.series_index,
        "axis_map": {"Z": src.axis_map.z, "T": src.axis_map.t},
        "channel_indices": list(src.channel_indices),
        "output_name": src.output_name,
        "expected_size": src.expected_size,
        "expected_mtime_ns": src.expected_mtime_ns,
        "needs_confirmation": src.needs_confirmation,
        "included": src.included,
        "series": None if src.series is None else _series_to_dict(src.series),
    }


def to_dict(scheme: ImportScheme) -> dict[str, Any]:
    """Encode ``scheme`` as plain JSON-ready data."""
    return {
        "version": scheme.version,
        "z_method": scheme.z_method,
        "sources": [_source_to_dict(s) for s in scheme.sources],
        "excluded": [
            {"path": str(e.path), "reason": e.reason, "series_index": e.series_index}
            for e in scheme.excluded
        ],
        "warnings": list(scheme.warnings),
    }


def dumps(scheme: ImportScheme) -> str:
    """Encode ``scheme`` as indented JSON text."""
    return json.dumps(to_dict(scheme), indent=2)


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _series_from_dict(d: dict[str, Any]) -> SeriesProbe:
    return SeriesProbe(
        index=int(d.get("index", 0)),
        name=str(d.get("name", "")),
        size_t=int(d.get("size_t", 1)),
        size_c=int(d.get("size_c", 1)),
        size_z=int(d.get("size_z", 1)),
        size_y=int(d.get("size_y", 1)),
        size_x=int(d.get("size_x", 1)),
        dimension_order=str(d.get("dimension_order", "XYCZT")),
        pixel_type=str(d.get("pixel_type", "")),
        is_rgb=bool(d.get("is_rgb", False)),
        is_interleaved=bool(d.get("is_interleaved", False)),
        physical_x_um=_optional_float(d.get("physical_x_um")),
        physical_y_um=_optional_float(d.get("physical_y_um")),
        physical_z_um=_optional_float(d.get("physical_z_um")),
        channel_names=tuple(str(n) for n in d.get("channel_names", ())),
        axis_source=str(d.get("axis_source", AXIS_METADATA)),
    )


def _source_from_dict(d: dict[str, Any]) -> ImportSource:
    if "path" not in d:
        raise ImportSchemeError("every source needs a 'path'")
    axes = d.get("axis_map") or {}
    series = d.get("series")
    return ImportSource(
        path=Path(d["path"]),
        series_index=int(d.get("series_index", 0)),
        axis_map=AxisMap(z=str(axes.get("Z", "Z")), t=str(axes.get("T", "T"))),
        channel_indices=tuple(int(i) for i in d.get("channel_indices", ())),
        output_name=validate_output_name(d.get("output_name", "")),
        expected_size=_optional_int(d.get("expected_size")),
        expected_mtime_ns=_optional_int(d.get("expected_mtime_ns")),
        needs_confirmation=str(d.get("needs_confirmation", "")),
        included=bool(d.get("included", True)),
        series=None if series is None else _series_from_dict(series),
    )


def _excluded_from_dict(d: dict[str, Any]) -> ExcludedEntry:
    return ExcludedEntry(
        path=Path(d["path"]),
        reason=str(d.get("reason", "")),
        series_index=_optional_int(d.get("series_index")),
    )


def source_to_dict(source: ImportSource) -> dict[str, Any]:
    """Encode one source, e.g. for a workflow entry's compress plan."""
    return _source_to_dict(source)


def source_from_dict(data: dict[str, Any]) -> ImportSource:
    """Decode one source. Raise :class:`ImportSchemeError` on any bad field."""
    if not isinstance(data, dict):
        raise ImportSchemeError("a source must be a JSON object")
    try:
        return _source_from_dict(data)
    except ImportSchemeError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ImportSchemeError(f"malformed source: {exc}") from exc


def from_dict(data: dict[str, Any]) -> ImportScheme:
    """Decode a scheme. Raise :class:`ImportSchemeError` on any bad field."""
    if not isinstance(data, dict):
        raise ImportSchemeError("scheme must be a JSON object")
    version = data.get("version")
    if version != SCHEME_VERSION:
        raise ImportSchemeError(
            f"unsupported scheme version {version!r}, expected {SCHEME_VERSION}"
        )
    z_method = data.get("z_method", "mip")
    if z_method not in Z_METHODS:
        raise ImportSchemeError(f"unknown z method {z_method!r}, expected one of {Z_METHODS}")
    try:
        sources = tuple(_source_from_dict(s) for s in data.get("sources", ()))
        excluded = tuple(_excluded_from_dict(e) for e in data.get("excluded", ()))
        warnings = tuple(str(w) for w in data.get("warnings", ()))
    except ImportSchemeError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ImportSchemeError(f"malformed scheme: {exc}") from exc
    return ImportScheme(
        version=SCHEME_VERSION,
        z_method=z_method,
        sources=sources,
        excluded=excluded,
        warnings=warnings,
    )


def loads(text: str) -> ImportScheme:
    """Decode scheme JSON text."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ImportSchemeError(f"scheme is not valid JSON: {exc}") from exc
    return from_dict(data)

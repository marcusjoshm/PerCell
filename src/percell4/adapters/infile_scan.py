"""Scan a selection of files or directories into a suggested import scheme.

The import dialog and ``percell-import`` both call :func:`scan_selection`,
so one selection classifies the same way on both surfaces (KTD7).

Stage one needs no Java: it reads TIFF headers only, never pixel data, and
picks the suggested mode. Stage two probes the in-file candidates through
the reader port, and only when In-file mode is suggested or chosen (KTD6).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from percell4.domain.io.infile import (
    ImportScheme,
    StageOneResult,
    preclassify,
    suggest_scheme,
)
from percell4.domain.io.models import DiscoveryMode
from percell4.io.paths import drop_sidecars, scan_files
from percell4.ports.image_reader import ImageReader, IsCancelled, OnFile

#: TIFF axes that belong to one plane rather than counting planes.
_PLANE_AXES = frozenset("YXS")


def tiff_plane_count(path: str | Path) -> int:
    """The number of 2D planes a TIFF declares, from its header only.

    ImageJ and OME-TIFF files declare their dimensions in metadata; a plain
    multipage file counts its pages. A file with more than one series counts
    as multi-plane. No pixel data is decoded.
    """
    import tifffile

    with tifffile.TiffFile(path) as tif:
        series = tif.series
        if not series:
            return len(tif.pages)
        if len(series) > 1:
            return max(2, len(tif.pages))
        first = series[0]
        return math.prod(
            size for size, axis in zip(first.shape, first.axes, strict=True)
            if axis not in _PLANE_AXES
        )


def expand_selection(paths: Iterable[str | Path]) -> list[Path]:
    """Files and directories -> a flat, ordered, sidecar-free file list.

    A directory contributes the files directly inside it (not recursive).
    Explicit files keep the caller's order; duplicates collapse.
    """
    out: list[Path] = []
    seen: set[Path] = set()
    for raw in paths:
        path = Path(raw)
        found = (
            [p for p in scan_files(path, "*") if p.is_file()]
            if path.is_dir()
            else drop_sidecars([path])
        )
        for p in found:
            if p not in seen:
                seen.add(p)
                out.append(p)
    return out


@dataclass(frozen=True)
class ScanOutcome:
    """What a scan found.

    ``scheme`` is None when the mode in effect is a legacy mode, or when
    there were no in-file candidates to probe. Its ``excluded`` list also
    carries the stage-one exclusions, so every selected file appears (R4).
    """

    stage_one: StageOneResult
    scheme: ImportScheme | None = None
    cancelled: bool = False


def scan_selection(
    paths: Iterable[str | Path],
    *,
    reader_factory: Callable[[], ImageReader],
    mode: DiscoveryMode | None = None,
    z_method: str = "mip",
    on_file: OnFile | None = None,
    is_cancelled: IsCancelled | None = None,
) -> ScanOutcome:
    """Classify ``paths`` and, in In-file mode, probe the candidates.

    ``reader_factory`` is called only when a probe is needed, so a legacy
    suggestion never starts Java. Java errors from the factory or the probe
    propagate with their reasons.
    """
    files = expand_selection(paths)
    stage_one = preclassify(files, tiff_plane_count, mode)
    if stage_one.mode is not DiscoveryMode.INFILE or not stage_one.candidates:
        return ScanOutcome(stage_one=stage_one)

    reader = reader_factory()
    cancelled = is_cancelled or (lambda: False)
    probes = reader.probe(list(stage_one.candidates), on_file, cancelled)
    if cancelled() or len(probes) < len(stage_one.candidates):
        return ScanOutcome(stage_one=stage_one, cancelled=True)

    scheme = suggest_scheme(probes, z_method)
    scheme = replace(scheme, excluded=stage_one.excluded + scheme.excluded)
    return ScanOutcome(stage_one=stage_one, scheme=scheme)

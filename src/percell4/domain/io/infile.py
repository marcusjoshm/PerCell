"""In-file import: probe records, stage-one classification and scheme suggestion.

In-file data keeps channels, z, time and series inside one file. Bio-Formats
reads it in a separate process. This module never touches that process. It
holds the plain records the reader returns and the pure rules that turn them
into an :class:`ImportScheme`.

Classification runs in two stages:

1. :func:`preclassify` sorts a selection by suffix and a TIFF header read. It
   needs no JVM. It also suggests the import mode for the whole run.
2. :func:`suggest_scheme` turns :class:`FileProbe` records from the reader
   into sources, excluded entries and warnings.

Ambiguous axes are flagged, never guessed silently. A flagged source stays in
the scheme but is not importable until the user confirms or reassigns it.

Pure domain: stdlib plus :mod:`percell4.io.paths`. The header reader is
injected, so nothing here opens a file.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from percell4.domain.io.models import DiscoveryMode
from percell4.io.paths import drop_sidecars

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Scheme format version written by the serializer.
SCHEME_VERSION = 1

#: Z projection methods, as :func:`percell4.domain.io.assembler.project_z` names them.
Z_METHODS = ("mip", "mean", "sum")

AXIS_METADATA = "metadata"
AXIS_ASSUMED = "assumed"

REASON_MULTI_PLANE = "multi-plane file, import with In-file mode"
REASON_SINGLE_PLANE = "single-plane series, import with Flat or Subdirectory mode"
REASON_BIN_IN_INFILE = "FLIM .bin file, use token mode"
REASON_MULTI_FILE = "multi-file datasets not supported yet"
REASON_UNSUPPORTED = "format not recognised"
REASON_NO_SERIES = "no image series in file"
REASON_POSSIBLE_Z_AS_T = "possible Z stored as T"
REASON_POSSIBLE_T_AS_Z = "possible T stored as Z"
REASON_ASSUMED_AXES = "axes assumed, no axis metadata in file (defaults to Z)"

TIFF_SUFFIXES = frozenset({".tif", ".tiff", ".btf", ".tf2", ".tf8"})
FLIM_BIN_SUFFIX = ".bin"

#: Suffixes that mark a multi-file dataset descriptor. Files sharing its stem
#: belong to the set.
MULTI_FILE_SUFFIXES = (".companion.ome", ".nd")

#: Multi-part suffixes stripped whole when naming a dataset.
_COMPOUND_SUFFIXES = (
    ".companion.ome",
    ".ome.tiff",
    ".ome.tif",
    ".ome.tf2",
    ".ome.tf8",
    ".ome.btf",
    ".ome.xml",
)

#: Suffixes Bio-Formats 8.5.0 readers claim, lower case with the leading dot.
#: Compiled from the reader suffix lists. Generic suffixes some readers also
#: claim (``.txt``, ``.xml``, ``.csv``, ``.h5``, ``.dat``, ``.db``, ``.raw``) are
#: left out on purpose: notes and PerCell's own ``.h5`` outputs must read as
#: unsupported, not as in-file candidates. A JVM-gated test (U3) checks that
#: this set is a subset of the live reader's suffixes. Update it with the jar.
BIOFORMATS_SUFFIXES = frozenset(
    {
        # TIFF family and OME
        ".tif", ".tiff", ".tf2", ".tf8", ".btf",
        ".ome.tif", ".ome.tiff", ".ome.tf2", ".ome.tf8", ".ome.btf",
        ".ome", ".ome.xml", ".companion.ome", ".qptiff",
        # Zeiss
        ".czi", ".lsm", ".zvi", ".mdb",
        # Nikon
        ".nd2",
        # Leica
        ".lif", ".lof", ".lei", ".xlef", ".lms", ".scn",
        # Olympus / Evident
        ".oib", ".oif", ".vsi", ".oir",
        # Bitplane Imaris, Applied Precision, 3i SlideBook
        ".ims", ".dv", ".r3d", ".sld",
        # MetaMorph, ICS
        ".stk", ".nd", ".ics", ".ids",
        # Volocity, Imspector, Abberior
        ".mvd2", ".obf", ".msr",
        # Whole-slide
        ".svs", ".ndpi", ".ndpis", ".bif", ".vms", ".afi",
        # Electron and other microscopy
        ".dm3", ".dm4", ".mrc", ".st", ".spe", ".sif", ".pic", ".ipl", ".ipw",
        ".nrrd", ".nhdr", ".fits", ".fts", ".nii", ".nii.gz", ".lim", ".sdt",
        ".spc", ".xdce", ".fli", ".c01", ".cxd", ".pcoraw", ".tnb", ".vws",
        ".al3d", ".am", ".amiramesh", ".aim", ".ch5", ".dcm", ".dicom",
        ".gel", ".i2i", ".jpk", ".l2d", ".liff", ".mea", ".mng", ".naf",
        ".pnl", ".pr3", ".tga", ".wpi", ".xqd", ".xqf", ".zfp",
        ".zfr", ".1sc", ".2fl", ".htd", ".klb", ".sm2", ".sm3",
        ".stp", ".avi", ".mov", ".psd", ".pict", ".pcx",
        # JPEG 2000 and generic raster
        ".jp2", ".j2k", ".jpf", ".jpx", ".png", ".jpg", ".jpeg", ".bmp", ".gif",
        # Bio-Formats test format
        ".fake",
    }
)

#: Signature of the injected TIFF header reader: the plane count the header
#: declares. It must not decode pixels.
HeaderReader = Callable[[Path], int]


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeriesProbe:
    """One series as the reader reports it. A dumb record.

    Sizes follow Bio-Formats. ``size_c`` counts RGB samples too. Physical
    sizes are in µm, or ``None`` when the file carries none. ``axis_source``
    is ``"assumed"`` when the reader invented the axes, such as a multipage
    TIFF with no axis metadata.
    """

    index: int
    name: str = ""
    size_t: int = 1
    size_c: int = 1
    size_z: int = 1
    size_y: int = 1
    size_x: int = 1
    dimension_order: str = "XYCZT"
    pixel_type: str = ""
    is_rgb: bool = False
    is_interleaved: bool = False
    physical_x_um: float | None = None
    physical_y_um: float | None = None
    physical_z_um: float | None = None
    channel_names: tuple[str, ...] = ()
    axis_source: str = AXIS_METADATA

    @property
    def channel_count(self) -> int:
        """Channels to import. An RGB series has at least 3."""
        if self.is_rgb:
            return max(self.size_c, 3)
        return self.size_c

    @property
    def element_count(self) -> int:
        """Exact pixel count over all axes. Python ints never wrap."""
        return math.prod(
            (self.size_t, self.size_c, self.size_z, self.size_y, self.size_x)
        )


@dataclass(frozen=True)
class FileProbe:
    """Metadata of one file as the reader reports it. A dumb record.

    ``used_files`` lists every file the reader needs for this dataset. More
    than one means a multi-file dataset. ``error`` holds a one-line reason
    when the probe failed; ``series`` is then empty.
    """

    path: Path
    size_bytes: int | None = None
    mtime_ns: int | None = None
    format_name: str = ""
    series: tuple[SeriesProbe, ...] = ()
    used_files: tuple[Path, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class AxisMap:
    """The role given to the file's Z and T axes.

    The identity map reads Z as Z and T as T. ``AxisMap(z="T", t="Z")`` swaps
    them. Channels, Y and X are never reassigned.
    """

    z: str = "Z"
    t: str = "T"

    def __post_init__(self) -> None:
        if {self.z, self.t} != {"Z", "T"}:
            raise ValueError(
                f"axis map must assign Z and T once each, got z={self.z!r} t={self.t!r}"
            )

    @property
    def is_identity(self) -> bool:
        return self.z == "Z" and self.t == "T"


@dataclass(frozen=True)
class ImportSource:
    """One series to import as one dataset.

    ``series`` is the raw probe and is never rewritten. ``axis_map`` holds
    the user's interpretation; :attr:`effective` applies it. A non-empty
    ``needs_confirmation`` keeps the source out of the import.
    """

    path: Path
    series_index: int = 0
    axis_map: AxisMap = field(default_factory=AxisMap)
    channel_indices: tuple[int, ...] = ()
    output_name: str = ""
    expected_size: int | None = None
    expected_mtime_ns: int | None = None
    needs_confirmation: str = ""
    included: bool = True
    series: SeriesProbe | None = None

    @property
    def effective(self) -> SeriesProbe | None:
        """The series with the axis map applied, or ``None`` without a probe."""
        if self.series is None:
            return None
        return apply_axis_map(self.series, self.axis_map)

    @property
    def importable(self) -> bool:
        return self.included and not self.needs_confirmation


@dataclass(frozen=True)
class ExcludedEntry:
    """A selected file, or one series of it, left out of the import."""

    path: Path
    reason: str
    series_index: int | None = None


@dataclass(frozen=True)
class ImportScheme:
    """The reviewed plan for one in-file import run."""

    version: int = SCHEME_VERSION
    z_method: str = "mip"
    sources: tuple[ImportSource, ...] = ()
    excluded: tuple[ExcludedEntry, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def importable_sources(self) -> tuple[ImportSource, ...]:
        """Sources that are included and need no confirmation."""
        return tuple(s for s in self.sources if s.importable)

    def replace_source(self, index: int, source: ImportSource) -> ImportScheme:
        """Return a copy with ``sources[index]`` replaced."""
        sources = list(self.sources)
        sources[index] = source
        return replace(self, sources=tuple(sources))


@dataclass(frozen=True)
class StageOneResult:
    """Stage-one sort of a selection.

    ``candidates`` are in-file candidates. ``legacy`` are single-plane TIFFs
    and ``.bin`` files. ``excluded`` holds the reasons for ``mode``.
    """

    suggested_mode: DiscoveryMode
    mode: DiscoveryMode
    candidates: tuple[Path, ...] = ()
    legacy: tuple[Path, ...] = ()
    excluded: tuple[ExcludedEntry, ...] = ()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _lower_name(path: Path) -> str:
    return path.name.lower()


def _matching_suffix(path: Path, suffixes: Iterable[str]) -> str | None:
    """The longest suffix in ``suffixes`` that ends ``path``'s name."""
    name = _lower_name(path)
    best = None
    for suffix in suffixes:
        if name.endswith(suffix) and len(name) > len(suffix):
            if best is None or len(suffix) > len(best):
                best = suffix
    return best


def dataset_stem(path: Path) -> str:
    """File name without its suffix. Compound suffixes like ``.ome.tif`` go whole."""
    suffix = _matching_suffix(path, _COMPOUND_SUFFIXES)
    if suffix is not None:
        return path.name[: -len(suffix)]
    return path.stem


def is_bioformats_suffix(path: Path) -> bool:
    return _matching_suffix(path, BIOFORMATS_SUFFIXES) is not None


def _is_tiff(path: Path) -> bool:
    return path.suffix.lower() in TIFF_SUFFIXES


def _multi_file_stems(paths: Sequence[Path]) -> set[tuple[Path, str]]:
    """(folder, stem) of every multi-file descriptor in the selection."""
    stems = set()
    for p in paths:
        suffix = _matching_suffix(p, MULTI_FILE_SUFFIXES)
        if suffix is not None:
            stems.add((p.parent, p.name[: -len(suffix)]))
    return stems


def _belongs_to_set(path: Path, stems: set[tuple[Path, str]]) -> bool:
    """True if ``path`` shares a descriptor's stem, followed by a separator."""
    for folder, stem in stems:
        if path.parent != folder or not path.name.startswith(stem):
            continue
        rest = path.name[len(stem):]
        if not rest or rest[0] in "_-. ":
            return True
    return False


def apply_axis_map(series: SeriesProbe, axis_map: AxisMap) -> SeriesProbe:
    """Rebuild the series sizes under ``axis_map``.

    The identity map returns ``series`` unchanged. Otherwise the file's T
    and Z sizes swap roles. The physical Z size stays with the Z role. It is
    dropped when the new Z has a single plane, since it describes nothing.
    """
    if axis_map.is_identity:
        return series
    size_z = series.size_t if axis_map.t == "Z" else series.size_z
    size_t = series.size_z if axis_map.z == "T" else series.size_t
    physical_z = series.physical_z_um if size_z > 1 else None
    return replace(series, size_z=size_z, size_t=size_t, physical_z_um=physical_z)


def reassign_axes(source: ImportSource, axis_map: AxisMap) -> ImportSource:
    """Set a new axis map. The user's choice resolves the ambiguity flag."""
    return replace(source, axis_map=axis_map, needs_confirmation="")


def confirm_source(source: ImportSource) -> ImportSource:
    """Accept the current axes and clear the ambiguity flag."""
    return replace(source, needs_confirmation="")


# ---------------------------------------------------------------------------
# Stage one
# ---------------------------------------------------------------------------

_LEGACY_MODES = (DiscoveryMode.FLAT, DiscoveryMode.SUBDIRECTORY, DiscoveryMode.TOKENLESS)


def preclassify(
    paths: Iterable[str | Path],
    read_plane_count: HeaderReader,
    mode: DiscoveryMode | None = None,
) -> StageOneResult:
    """Sort a selection without a reader or a JVM.

    ``read_plane_count`` is called for TIFF files only. It returns the plane
    count the header declares. A failure there excludes the file as
    unreadable.

    The suggested mode imports the most files. In-file wins only when its
    candidates outnumber the legacy files; a tie goes to the legacy mode.
    The legacy mode is Flat when every legacy file shares one folder, else
    Subdirectory. ``mode`` overrides the suggestion; the excluded reasons
    always follow the mode in effect.
    """
    selected = drop_sidecars(paths)
    multi_stems = _multi_file_stems(selected)

    candidates: list[Path] = []
    single_plane: list[Path] = []
    bins: list[Path] = []
    always_excluded: list[ExcludedEntry] = []

    for p in selected:
        if _matching_suffix(p, MULTI_FILE_SUFFIXES) is not None:
            always_excluded.append(ExcludedEntry(p, REASON_MULTI_FILE))
            continue
        if p.suffix.lower() == FLIM_BIN_SUFFIX:
            bins.append(p)
            continue
        if _is_tiff(p):
            try:
                planes = int(read_plane_count(p))
            except Exception as exc:  # noqa: BLE001 - reader errors become reasons
                always_excluded.append(ExcludedEntry(p, f"unreadable: {exc}"))
                continue
            if planes <= 1:
                single_plane.append(p)
                continue
        elif not is_bioformats_suffix(p):
            always_excluded.append(ExcludedEntry(p, REASON_UNSUPPORTED))
            continue
        # A multi-plane TIFF, or another suffix Bio-Formats claims.
        if _belongs_to_set(p, multi_stems):
            always_excluded.append(ExcludedEntry(p, REASON_MULTI_FILE))
        else:
            candidates.append(p)

    legacy = single_plane + bins
    if candidates and len(candidates) > len(legacy):
        suggested = DiscoveryMode.INFILE
    elif len({p.parent for p in legacy}) > 1:
        suggested = DiscoveryMode.SUBDIRECTORY
    else:
        suggested = DiscoveryMode.FLAT
    effective = suggested if mode is None else mode

    excluded = list(always_excluded)
    if effective is DiscoveryMode.INFILE:
        excluded += [ExcludedEntry(p, REASON_SINGLE_PLANE) for p in single_plane]
        excluded += [ExcludedEntry(p, REASON_BIN_IN_INFILE) for p in bins]
    elif effective in _LEGACY_MODES:
        excluded += [ExcludedEntry(p, REASON_MULTI_PLANE) for p in candidates]

    order = {p: i for i, p in enumerate(selected)}
    excluded.sort(key=lambda e: order[e.path])
    return StageOneResult(
        suggested_mode=suggested,
        mode=effective,
        candidates=tuple(candidates),
        legacy=tuple(legacy),
        excluded=tuple(excluded),
    )


# ---------------------------------------------------------------------------
# Stage two
# ---------------------------------------------------------------------------


def _ambiguity(series: SeriesProbe) -> str:
    """The KTD11 axis flag for one series, or ``""``."""
    if series.axis_source == AXIS_ASSUMED:
        return REASON_ASSUMED_AXES
    if series.size_t > 1 and series.size_z == 1 and series.physical_z_um is not None:
        return REASON_POSSIBLE_Z_AS_T
    if series.size_z > 1 and series.physical_z_um is None:
        return REASON_POSSIBLE_T_AS_Z
    return ""


def _join_reasons(*reasons: str) -> str:
    return "; ".join(r for r in reasons if r)


def z_warnings(sources: Iterable[ImportSource], z_method: str) -> tuple[str, ...]:
    """The R7 notes for z-counts that vary across ``sources``.

    Recomputed whenever the z method changes, so a switch to ``sum`` warns
    even after the scheme was suggested.
    """
    z_counts = {s.series.size_z for s in sources if s.series is not None}
    if len(z_counts) <= 1:
        return ()
    notes = [
        f"z-count varies across files ({min(z_counts)} to {max(z_counts)}); "
        "each file is projected over its own z-series"
    ]
    if z_method == "sum":
        notes.append(
            "summed intensities are not comparable across datasets: they scale "
            "with z-count. Use max or mean instead"
        )
    return tuple(notes)


def with_z_method(scheme: ImportScheme, z_method: str) -> ImportScheme:
    """``scheme`` with a new z method and its z-count notes recomputed."""
    if z_method not in Z_METHODS:
        raise ValueError(f"unknown z method {z_method!r}, expected one of {Z_METHODS}")
    old = set(z_warnings(scheme.sources, scheme.z_method))
    kept = tuple(w for w in scheme.warnings if w not in old)
    return replace(
        scheme, z_method=z_method, warnings=kept + z_warnings(scheme.sources, z_method)
    )


def suggest_scheme(probes: Iterable[FileProbe], z_method: str = "mip") -> ImportScheme:
    """Turn reader probes into a suggested :class:`ImportScheme`.

    Each series of each readable, single-file probe becomes one source. A
    file with one series keeps its stem as the output name; several series
    are named ``<stem>_s<NN>``. Channel tokens are the channel indices
    (KTD8). Ambiguous axes and channel-count outliers are flagged.
    """
    if z_method not in Z_METHODS:
        raise ValueError(f"unknown z method {z_method!r}, expected one of {Z_METHODS}")

    sources: list[ImportSource] = []
    excluded: list[ExcludedEntry] = []
    warnings: list[str] = []

    for probe in probes:
        if probe.error:
            excluded.append(ExcludedEntry(probe.path, probe.error))
            continue
        if len(probe.used_files) > 1:
            excluded.append(ExcludedEntry(probe.path, REASON_MULTI_FILE))
            continue
        if not probe.series:
            excluded.append(ExcludedEntry(probe.path, REASON_NO_SERIES))
            continue
        stem = dataset_stem(probe.path)
        multi = len(probe.series) > 1
        for series in probe.series:
            sources.append(
                ImportSource(
                    path=probe.path,
                    series_index=series.index,
                    channel_indices=tuple(range(series.channel_count)),
                    output_name=f"{stem}_s{series.index:02d}" if multi else stem,
                    expected_size=probe.size_bytes,
                    expected_mtime_ns=probe.mtime_ns,
                    needs_confirmation=_ambiguity(series),
                    series=series,
                )
            )

    # Channel-count outliers, against a unique modal count.
    counts = Counter(len(s.channel_indices) for s in sources)
    ranked = counts.most_common()
    if len(ranked) > 1 and ranked[0][1] > ranked[1][1]:
        modal = ranked[0][0]
        for i, src in enumerate(sources):
            n = len(src.channel_indices)
            if n == modal:
                continue
            reason = f"channel count {n} differs from {modal} in the other files"
            sources[i] = replace(
                src, needs_confirmation=_join_reasons(src.needs_confirmation, reason)
            )
            warnings.append(f"{src.output_name}: {reason}, excluded until confirmed")

    warnings.extend(z_warnings(sources, z_method))

    # Output-name collisions across the whole scheme.
    names = Counter(s.output_name for s in sources)
    for name, n in names.items():
        if n > 1:
            where = ", ".join(str(s.path) for s in sources if s.output_name == name)
            warnings.append(f"output name collision: {name!r} is produced by {n} sources ({where})")

    return ImportScheme(
        version=SCHEME_VERSION,
        z_method=z_method,
        sources=tuple(sources),
        excluded=tuple(excluded),
        warnings=tuple(warnings),
    )

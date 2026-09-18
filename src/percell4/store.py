"""HDF5-based dataset storage for PerCell4.

Each dataset is a single .h5 file containing images, labels, masks,
measurements, and metadata. DatasetStore provides read/write access
with crash-safe per-operation file handling for writes and an optional
session mode for efficient repeated reads.
"""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any

import h5py
import hdf5plugin  # noqa: F401 — registers the Blosc filter (read + write)
import numpy as np
import pandas as pd
from numpy.typing import NDArray

from percell4.domain.errors import ProjectionRequiredError
from percell4.domain.io.cross_format import deserialize_rule, serialize_rule
from percell4.domain.io.layout import (
    intensity_channel_count,
    placeholder_channel_index,
    placeholder_channel_name,
)
from percell4.domain.io.models import (
    CrossFormatRule,
    ExplicitRule,
    ProvenanceRecord,
    StitchProvenanceRecord,
)
from percell4.domain.io.projections import (
    PROJECTION_NAMES,
    legacy_projection_name,
    ordered_projections,
    resolve_projection,
)
from percell4.domain.io.view_bin import (
    majority_vote_mask,
    mean_bin_2d,
    mode_labels,
    sum_bin_2d,
    sum_bin_decay,
)

logger = logging.getLogger(__name__)

# Chunk cache size for session reads (64 MB)
_READ_CACHE_BYTES = 64 * 1024 * 1024

#: The path every caller reads intensity through. On a dataset with named
#: projections it resolves to one of them (see ``DatasetStore._resolve``);
#: on a dataset written before named projections it is the array itself.
INTENSITY_PATH = "intensity"
#: Group holding one array per kept projection (``projections/max`` ...).
PROJECTIONS_GROUP = "projections"
#: The optional full z-series, (T,) C, Z, H, W float32.
ZSERIES_PATH = "zseries"
#: Largest z-series chunk edge; a chunk otherwise holds one whole plane.
_ZSERIES_CHUNK_EDGE = 2048


def _apply_view_bin(hdf5_path: str, arr: NDArray, view_bin: int) -> NDArray:
    """Dispatch view-bin downsampling by HDF5 path prefix.

    Pure function; takes the already-materialized array. See the table on
    :meth:`DatasetStore.read_array` for the per-prefix rule.
    """
    if view_bin == 1:
        return arr
    # Normalize the path (drop leading slash for consistent prefix checks).
    p = hdf5_path.lstrip("/")
    if p == "intensity" or p.startswith("intensity/"):
        return sum_bin_2d(arr, view_bin)
    if p.startswith(f"{PROJECTIONS_GROUP}/") or p == ZSERIES_PATH:
        # Named projections and z-series planes are photon-count images,
        # exactly like the legacy /intensity.
        return sum_bin_2d(arr, view_bin)
    if p.startswith("decay/"):
        return sum_bin_decay(arr, view_bin)
    if p.startswith("labels/"):
        return mode_labels(arr, view_bin)
    if p.startswith("masks/"):
        return majority_vote_mask(arr, view_bin)
    if p.startswith("phasor/"):
        # Every leaf under /phasor/<ch>/ is intensive (g, s, lifetime, and
        # their *_filtered counterparts). Mean-bin so magnitudes don't
        # scale with k.
        return mean_bin_2d(arr, view_bin)
    # Unknown path -- pass through. Callers asking for a view bin on a
    # path with no defined rule get raw data; safer than guessing.
    return arr


def _stored_projection_arrays(f: h5py.File) -> dict[str, str]:
    """``{projection name: HDF5 path}`` for the intensity arrays in ``f``.

    Named projections win. A file with none but a legacy ``/intensity``
    reports that one array under its legacy name. Metadata only.
    """
    grp = f.get(PROJECTIONS_GROUP)
    if isinstance(grp, h5py.Group):
        names = [n for n in grp if isinstance(grp[n], h5py.Dataset)]
        if names:
            return {
                n: f"{PROJECTIONS_GROUP}/{n}" for n in ordered_projections(names)
            }
    if INTENSITY_PATH in f and isinstance(f[INTENSITY_PATH], h5py.Dataset):
        z_projection = (
            f["metadata"].attrs.get("z_projection") if "metadata" in f else None
        )
        return {legacy_projection_name(z_projection): INTENSITY_PATH}
    return {}


def _first_intensity_array(f: h5py.File) -> h5py.Dataset | None:
    """The legacy ``/intensity``, else the first named projection, else None."""
    paths = _stored_projection_arrays(f)
    return f[next(iter(paths.values()))] if paths else None


def _infer_bin_metadata(f: h5py.File) -> dict[str, Any]:
    """Return ``{"native_shape": ..., "creation_bin": ...}`` inferred from
    an open HDF5 file's array contents.

    ``native_shape`` is the last two dims of ``/intensity`` (or, on a dataset
    with named projections, of the first projection) if it exists, else of
    the z-series, else the first two dims of the first ``/decay/<ch>`` array,
    else ``None``. ``creation_bin`` defaults to ``1`` when absent from
    ``/metadata.attrs``.

    Pure read of the open file handle -- does not mutate or close.
    """
    native_shape: tuple[int, int] | None = None
    n_timepoints = 1
    intensity = (
        f["intensity"] if "intensity" in f else _first_intensity_array(f)
    )
    if intensity is None and isinstance(f.get(ZSERIES_PATH), h5py.Dataset):
        intensity = f[ZSERIES_PATH]
    if intensity is not None:
        ds = intensity
        shape = ds.shape
        if len(shape) >= 2:
            native_shape = (int(shape[-2]), int(shape[-1]))
        # A leading "T" in the dims attr marks a time-lapse stack; its
        # length is the timepoint count. Absent/2D => single timepoint.
        dims = ds.attrs.get("dims")
        if dims is not None and len(dims) > 0 and str(dims[0]) == "T":
            n_timepoints = int(shape[0])
    elif "decay" in f:
        decay_grp = f["decay"]
        children = list(decay_grp.keys())
        if children:
            first = decay_grp[children[0]]
            shape = first.shape
            dims = first.attrs.get("dims")
            is_4d_decay = len(shape) == 4 or (
                dims is not None and len(dims) > 0 and str(dims[0]) == "Tacq"
            )
            if is_4d_decay and len(shape) >= 4:
                # (T_acq, H, W, T_bins): spatial is dims [1:3]; the acquisition
                # axis count comes from the decay when no /intensity exists.
                native_shape = (int(shape[1]), int(shape[2]))
                n_timepoints = int(shape[0])
            elif len(shape) >= 2:
                # Legacy 3-D (H, W, T_bins): spatial is dims [0:2], T_acq == 1.
                native_shape = (int(shape[0]), int(shape[1]))
    creation_bin = 1
    if "metadata" in f and "creation_bin" in f["metadata"].attrs:
        creation_bin = int(f["metadata"].attrs["creation_bin"])
    return {
        "native_shape": native_shape,
        "creation_bin": creation_bin,
        "n_timepoints": n_timepoints,
    }


def _decode_names(raw: Any) -> tuple[str, ...]:
    """A string-list attr as a tuple of ``str`` (h5py may give bytes or numpy)."""
    if isinstance(raw, (str, bytes)):
        raw = [raw]
    elif hasattr(raw, "tolist"):
        raw = raw.tolist()
    return tuple(n.decode() if isinstance(n, bytes) else str(n) for n in raw)


def projection_layout(n_t: int, n_c: int) -> list[str]:
    """``dims`` of an intensity array for ``n_t`` timepoints and ``n_c`` channels.

    A single axis is dropped: ``(H, W)``, ``(C, H, W)``, ``(T, H, W)`` or
    ``(T, C, H, W)``, the layouts every intensity reader accepts.
    """
    dims = []
    if n_t > 1:
        dims.append("T")
    if n_c > 1:
        dims.append("C")
    return dims + ["H", "W"]


class _StackWriter:
    """Creates and fills pre-allocated arrays in one open file (see
    :meth:`DatasetStore.open_stack_writer`)."""

    def __init__(self, store: DatasetStore, f: h5py.File) -> None:
        self._store = store
        self._f = f
        self.created: list[str] = []

    def begin_zseries(
        self, shape: tuple[int, ...], channel_names: list[str]
    ) -> _ZSeriesWriter:
        """Pre-allocate the z-series (see :meth:`DatasetStore.zseries_writer`)."""
        shape = tuple(int(x) for x in shape)
        if len(shape) not in (4, 5):
            raise ValueError(f"z-series shape must be (C,Z,H,W) or (T,C,Z,H,W), got {shape}")
        if len(channel_names) != shape[-4]:
            raise ValueError(
                f"{len(channel_names)} channel name(s) for {shape[-4]} z-series channel(s)"
            )
        f = self._f
        if ZSERIES_PATH in f:
            del f[ZSERIES_PATH]
        h, w = shape[-2:]
        self._store._check_xy(f, (h, w), "The z-series")
        chunks = (1,) * (len(shape) - 2) + (
            min(h, _ZSERIES_CHUNK_EDGE),
            min(w, _ZSERIES_CHUNK_EDGE),
        )
        ds = f.create_dataset(
            ZSERIES_PATH, shape=shape, dtype=np.float32, chunks=chunks,
            **_compression_kwargs(),
        )
        self.created.append(ZSERIES_PATH)
        ds.attrs["dims"] = ["T", "C", "Z", "H", "W"][5 - len(shape):]
        ds.attrs["channel_names"] = list(channel_names)
        return _ZSeriesWriter(ds)

    def begin_projection(
        self, name: str | None, n_t: int, n_c: int, hw: tuple[int, int]
    ) -> _ProjectionWriter:
        """Pre-allocate the named projection for ``n_t`` x ``n_c`` planes.

        ``name`` ``None`` pre-allocates the unnamed ``/intensity`` instead,
        for data with no Z axis, which has no projection (R4).
        """
        if name is not None and name not in PROJECTION_NAMES:
            raise ValueError(
                f"unknown projection {name!r}, expected one of {PROJECTION_NAMES}"
            )
        f = self._f
        path = INTENSITY_PATH if name is None else f"{PROJECTIONS_GROUP}/{name}"
        if path in f:
            del f[path]
        h, w = int(hw[0]), int(hw[1])
        self._store._check_xy(f, (h, w), f"The {name or 'intensity'} image")
        dims = projection_layout(n_t, n_c)
        shape = tuple(
            {"T": n_t, "C": n_c, "H": h, "W": w}[d] for d in dims
        )
        ds = f.create_dataset(
            path, shape=shape, dtype=np.float32, chunks=_choose_chunks(shape),
            **_compression_kwargs(),
        )
        self.created.append(path)
        ds.attrs["dims"] = dims
        return _ProjectionWriter(ds, dims)


class _ProjectionWriter:
    """Fills a pre-allocated projection one (t, c) plane at a time."""

    def __init__(self, ds: h5py.Dataset, dims: list[str]) -> None:
        self._ds = ds
        self._timed = "T" in dims
        self._channels = "C" in dims

    def write_plane(self, t: int, c: int, plane: NDArray) -> None:
        """Write the plane of timepoint ``t`` and channel position ``c``."""
        index = ((t,) if self._timed else ()) + ((c,) if self._channels else ())
        self._ds[index] = np.asarray(plane, dtype=np.float32)


class _ZSeriesWriter:
    """Fills a pre-allocated z-series one plane at a time (see
    :meth:`DatasetStore.zseries_writer`)."""

    def __init__(self, ds: h5py.Dataset) -> None:
        self._ds = ds
        self.shape = tuple(int(x) for x in ds.shape)

    def write_plane(self, t: int, c: int, z: int, plane: NDArray) -> None:
        """Write plane ``(t, c, z)``; ``t`` must be 0 without a time axis."""
        if tuple(plane.shape) != self.shape[-2:]:
            raise ValueError(
                f"plane shape {tuple(plane.shape)} does not match the z-series "
                f"plane {self.shape[-2:]}"
            )
        plane = np.asarray(plane, dtype=np.float32)
        if len(self.shape) == 5:
            self._ds[t, c, z] = plane
        elif t != 0:
            raise IndexError(f"timepoint={t} out of range: the z-series has no T axis")
        else:
            self._ds[c, z] = plane


#: Attribute naming the intensity channel a segmentation or mask was made
#: from (R10). Absent where no single channel applies (imported layers,
#: whole-field). A segmentation belongs to no projection: it can be measured
#: on any of them.
SOURCE_CHANNEL_ATTR = "source_channel"


def source_channel_attrs(channel: str | None) -> dict[str, str]:
    """``{"source_channel": channel}``, or ``{}`` when there is none."""
    return {SOURCE_CHANNEL_ATTR: str(channel)} if channel else {}


# Provenance-attribute keys for masks captured by "Apply Current Phasor
# as Mask". Single source of truth so future readers cannot drift from
# the writer in main_window.py.
PHASOR_MASK_ATTR_INTENSITY_THRESHOLD = "phasor_intensity_threshold"
PHASOR_MASK_ATTR_REF_CIRCLE_CENTER_G = "phasor_ref_circle_center_g"
PHASOR_MASK_ATTR_REF_CIRCLE_CENTER_S = "phasor_ref_circle_center_s"
PHASOR_MASK_ATTR_REF_CIRCLE_RADIUS = "phasor_ref_circle_radius"
PHASOR_MASK_ATTR_ACTIVE_MASK = "phasor_active_mask_at_capture"
PHASOR_MASK_ATTR_CLEARED_PIXEL_COUNT = "phasor_cleared_pixel_count"
PHASOR_MASK_ATTR_ACTIVE_CHANNEL = "phasor_active_channel"
PHASOR_MASK_ATTR_CAPTURE_ISO = "phasor_capture_iso8601"


class LayerAlreadyExistsError(Exception):
    """Raised when a payload group already exists and force=False."""


class MetadataConsistencyError(Exception):
    """Raised when /metadata.native_shape disagrees with on-disk array shape.

    The dataset-wide spatial-binning model treats ``/metadata.native_shape``
    as the authoritative native resolution. If a stored value disagrees
    with what we can infer from ``/intensity`` (or ``/decay/<first_ch>``
    when intensity is absent), we refuse to silently overwrite -- a real
    schema bug or a corrupted file is more likely than a benign
    transient. Callers must inspect and decide.
    """


class LayerSizeMismatchError(Exception):
    """Raised when an Add-Layer source shape doesn't equal /metadata.native_shape.

    The dataset-wide binning model locks the dataset's native shape at
    compress time. Subsequent layers added through the Add-Layer path
    (Add-Layer dialog TIFFs, TCSPC append, ROI imports) must match
    exactly -- the .h5 doesn't tolerate mixed-resolution storage.
    Mismatches are the user's signal that they need to either pre-bin
    the source externally or re-compress with a different creation_bin.
    """


class SourceShapeMismatchError(Exception):
    """Raised at compress time when source channels disagree on (H, W).

    The dataset-wide binning model requires all source files in one
    compress operation to share the same pre-bin (H, W); native_shape
    is computed as (H // creation_bin, W // creation_bin) from that
    agreed shape. If one source TIFF or .bin is at a different
    resolution than its peers, the run aborts before writing -- silent
    partial imports would corrupt the native_shape invariant.
    """


class CrossFormatRuleConflictError(Exception):
    """Raised when an append would persist a rule different from one already stored."""


class DimsConsistencyError(Exception):
    """Raised when /intensity's ``dims`` attr disagrees with its shape + counts.

    The canonical corruption: an older Add-Layer Channel write stamped
    ``dims=['C','H','W']`` onto a ``(T, H, W)`` time-lapse array, so the dataset
    reads back as ``n_timepoints == 1`` and every time-aware feature silently
    collapses to frame 0 (the exact silent-collapse the multi-timepoint work
    exists to eliminate). We refuse to open such a dataset silently. Also raised
    when the ``dims`` attribute length doesn't match the array rank.
    """


# Backwards-compat aliases — the names without the Error suffix were used in
# the first round of tests. Keep them around so callers writing
# ``from percell4.store import LayerAlreadyExists`` still work.
LayerAlreadyExists = LayerAlreadyExistsError
CrossFormatRuleConflict = CrossFormatRuleConflictError


@dataclass(frozen=True)
class StitchGeometry:
    """Registered overlap-stitch geometry read back from a dataset.

    ``registered`` is the commit marker (``/metadata.stitch_registered``).
    When a dataset was never registered (or predates the feature), the
    ``stitch/tile_offsets`` dataset is absent and this reads back as
    ``registered=False`` with ``offsets=None`` — the back-compat / grid
    path signal for consumers (R6/AE4).

    ``offsets`` is the ``(N, 2) int32`` array of per-tile ``(y0, x0)``
    top-left corners, with per-axis min exactly 0 (asserted on both write
    and read) so the canvas derivation is unconditionally
    ``max(offset + extent)``.

    ``disconnected`` is the persisted set of tile indices that the import
    solve left at their grid seed (lowest overwrite priority). A later
    decay-only append threads this back through ``write_decay_streaming`` so
    the append reproduces the import's overlap winner exactly. Defaults to
    ``()`` for back-compat with files written before it was persisted.
    """

    registered: bool
    offsets: NDArray | None
    reference_channel: str | None
    overlap: float | None
    disconnected: tuple[int, ...] = ()


def _choose_chunks(shape: tuple[int, ...], is_decay: bool = False) -> tuple[int, ...]:
    """Choose HDF5 chunk shape based on array dimensions.

    - 2D spatial: (256, 256) or smaller if image is small
    - 3D+ with TCSPC: (64, 64, N_bins) — keep full time axis per chunk
    - Other 3D+: (1, 256, 256) — one plane at a time
    """
    ndim = len(shape)
    if ndim == 2:
        return (min(256, shape[0]), min(256, shape[1]))
    if ndim == 4 and is_decay:
        # Time-lapse TCSPC (T_acq, H, W, T_bins): one acquisition frame per
        # chunk, 64x64 spatial tiles, full histogram axis. Chunking T_acq to 1
        # keeps a per-frame slice cheap and avoids a whole-row-x-W-x-T_bins chunk.
        return (1, min(64, shape[1]), min(64, shape[2]), shape[3])
    if ndim >= 3 and is_decay:
        # TCSPC: spatial chunks of 64x64, full time axis
        return (min(64, shape[0]), min(64, shape[1])) + shape[2:]
    if ndim >= 3:
        # Default: one plane at a time for leading dims
        chunks = [1] * ndim
        chunks[-2] = min(256, shape[-2])
        chunks[-1] = min(256, shape[-1])
        return tuple(chunks)
    return None  # let h5py auto-chunk


def _compression_kwargs(is_decay: bool = False) -> dict[str, Any]:
    """Return compression keyword arguments for dataset creation.

    Images/labels/masks use **Blosc (zstd, clevel 5, bitshuffle)**: measured
    on real intensity it matches gzip-4+shuffle's ratio (~3.4x, no size penalty
    on large files) while decoding ~3x faster — which compounds with the
    parallel decode on load. TCSPC decay keeps ``lzf`` (the existing fast,
    lighter choice for those large per-pixel stacks).

    Note: Blosc and lzf are h5py/registered-filter codecs (not the universal
    gzip). PerCell4 imports ``hdf5plugin`` wherever it reads HDF5 (``store``,
    ``parallel_decode``), so every read path has the Blosc filter registered.
    Existing gzip files keep reading unchanged (gzip is always available).
    """
    if is_decay:
        return {"compression": "lzf"}
    return dict(
        hdf5plugin.Blosc(
            cname="zstd", clevel=5, shuffle=hdf5plugin.Blosc.BITSHUFFLE
        )
    )


def _cal_harmonic_suffix(key: str, base: str) -> str | None:
    """If ``key`` is ``base`` followed by a ``_h<digits>`` harmonic suffix,
    return that suffix (e.g. ``'_h2'``); otherwise ``None``.

    Lets the per-harmonic calibration attrs
    (``flim_cal_phase_<ch>_h<n>`` / ``flim_cal_mod_<ch>_h<n>``) follow their
    channel on rename/remove alongside the legacy suffix-less key. The
    ``_h<digits>`` guard avoids matching a different channel whose name
    merely extends ``base`` (e.g. base ``flim_cal_phase_ch1`` vs a real key
    ``flim_cal_phase_ch10``).
    """
    if not key.startswith(base):
        return None
    suffix = key[len(base):]
    if suffix.startswith("_h") and suffix[2:].isdigit():
        return suffix
    return None


def _normalize_description(raw: Any) -> str | None:
    """Coerce a stored description attr to ``str``, or ``None`` when blank.

    h5py returns ``str`` for a text attr written as ``str``, but a value
    written as bytes (or by an older writer) comes back as ``bytes`` /
    ``numpy.bytes_``. Blank text collapses to ``None`` so that "absent" and
    "empty" are one state for every reader.
    """
    if raw is None:
        return None
    if isinstance(raw, (bytes, np.bytes_)):
        raw = raw.decode("utf-8", errors="replace")
    text = str(raw)
    return text if text.strip() else None


class DatasetStore:
    """Read/write interface for a single .h5 dataset file.

    Writes open/close the file per operation (crash-safe).
    Reads can use per-operation mode or a session context manager
    for efficient repeated access with a large chunk cache.
    """

    def __init__(self, path: str | Path, projection: str | None = None) -> None:
        self.path = Path(path)
        #: The projection every read of ``intensity`` answers from; ``None``
        #: means the preferred projection, else the sole one (see
        #: :func:`~percell4.domain.io.projections.resolve_projection`). Fixed
        #: for the store's life: open a new store to read another projection.
        self.projection = projection
        self._session_file: h5py.File | None = None

    # ── Projection resolution ─────────────────────────────────

    def _resolve(self, f: h5py.File, hdf5_path: str) -> str:
        """Map a read of ``intensity`` to the array it answers from.

        Every other path passes through unchanged. A dataset with no
        projection and no z-series keeps ``intensity``, so a read raises
        today's ``KeyError``; a dataset written before named projections
        always reads its one ``/intensity``. Raises
        :class:`ProjectionRequiredError` when the projection cannot be
        chosen, including a z-series-only dataset.
        """
        if hdf5_path.lstrip("/") != INTENSITY_PATH:
            return hdf5_path
        paths = _stored_projection_arrays(f)
        if not paths and ZSERIES_PATH not in f:
            return INTENSITY_PATH
        if INTENSITY_PATH in paths.values():
            # A dataset from before named projections has one array and no
            # choice: it reads it whatever projection is requested, so a run
            # over mixed datasets analyses old ones exactly as before (R17).
            return INTENSITY_PATH
        return paths[resolve_projection(tuple(paths), self.projection)]

    def _resolve_soft(self, f: h5py.File, hdf5_path: str) -> str | None:
        """:meth:`_resolve` for existence and dims checks, which never raise.

        ``None`` when no stored array can answer (no projection, or the
        chosen one is absent). When several projections are stored and none
        is chosen, the first answers: every projection shares shape and dims.
        """
        try:
            return self._resolve(f, hdf5_path)
        except ProjectionRequiredError:
            paths = _stored_projection_arrays(f)
            if self.projection is None and paths:
                return next(iter(paths.values()))
            return None

    def list_projections(self) -> tuple[str, ...]:
        """Names of the stored projections, in display order. Metadata only.

        A dataset written before named projections lists its one
        ``/intensity`` under its legacy name (``max`` for a ``mip`` import,
        ``projection`` when no method was recorded).
        """
        if not self.path.exists():
            return ()
        f = self._open_read()
        try:
            return tuple(_stored_projection_arrays(f))
        finally:
            self._close_if_not_session(f)

    def named_projections(self) -> tuple[str, ...]:
        """Projections stored as named arrays, in display order. Metadata only.

        Empty for a dataset written before named projections (its one
        ``/intensity`` reads whatever projection is requested) and for a
        z-series-only dataset.
        """
        if not self.path.exists():
            return ()
        f = self._open_read()
        try:
            paths = _stored_projection_arrays(f)
            return () if INTENSITY_PATH in paths.values() else tuple(paths)
        finally:
            self._close_if_not_session(f)

    def resolved_projection(self) -> str | None:
        """The named projection intensity reads resolve to; ``None`` for a
        dataset written before named projections (or with no intensity).

        Measurement outputs record it (KTD9); legacy datasets keep today's
        columns.
        """
        path = self.resolved_intensity_path()
        prefix = f"{PROJECTIONS_GROUP}/"
        return path[len(prefix):] if path.startswith(prefix) else None

    def resolved_intensity_path(self) -> str:
        """The HDF5 path this store's intensity reads come from.

        For readers that open the file themselves (the parallel viewer
        decoder, batch image export, FLIM-FRET discovery), so they read the
        same array the store would. Raises :class:`ProjectionRequiredError`
        like any intensity read.
        """
        f = self._open_read()
        try:
            return self._resolve(f, INTENSITY_PATH)
        finally:
            self._close_if_not_session(f)

    # ── Session mode for reads ────────────────────────────────

    @contextmanager
    def open_read(self):
        """Context manager for efficient repeated reads.

        Keeps the file open with a large chunk cache. Use for interactive
        sessions where multiple reads happen in quick succession::

            with store.open_read() as s:
                intensity = s.read_array("intensity")
                labels = s.read_labels("cellpose")
        """
        self._session_file = h5py.File(
            self.path, "r", rdcc_nbytes=_READ_CACHE_BYTES
        )
        try:
            yield self
        finally:
            if self._session_file is not None:
                self._session_file.close()
            self._session_file = None

    def _open_read(self) -> h5py.File:
        """Get a file handle for reading (session or per-operation)."""
        if self._session_file is not None:
            return self._session_file
        return h5py.File(self.path, "r")

    def _close_if_not_session(self, f: h5py.File) -> None:
        """Close the file handle if not in session mode."""
        if f is not self._session_file:
            f.close()

    # ── Generic write operations ──────────────────────────────

    def write_array(
        self,
        hdf5_path: str,
        array: NDArray,
        attrs: dict[str, Any] | None = None,
        is_decay: bool = False,
    ) -> int:
        """Write a numpy array to the specified HDF5 path.

        Returns the number of elements written.

        Raises ``ValueError`` for ``intensity`` on a dataset with named
        projections or a z-series: one array cannot stand for every
        projection. Use :meth:`write_projection` or
        :meth:`rewrite_projections` there.
        """
        with h5py.File(self.path, "a") as f:
            self._refuse_legacy_intensity_write(f, hdf5_path)
            self._write_dataset(f, hdf5_path, array, attrs, is_decay)
        return array.size

    @staticmethod
    def _refuse_legacy_intensity_write(f: h5py.File, hdf5_path: str) -> None:
        if hdf5_path.lstrip("/") != INTENSITY_PATH:
            return
        if PROJECTIONS_GROUP in f or ZSERIES_PATH in f:
            raise ValueError(
                "This dataset stores named projections; write intensity with "
                "write_projection or rewrite_projections, not /intensity."
            )

    @staticmethod
    def _write_dataset(
        f: h5py.File,
        hdf5_path: str,
        array: NDArray,
        attrs: dict[str, Any] | None = None,
        is_decay: bool = False,
    ) -> None:
        if hdf5_path in f:
            del f[hdf5_path]
        chunks = _choose_chunks(array.shape, is_decay=is_decay)
        f.create_dataset(
            hdf5_path,
            data=array,
            chunks=chunks,
            **_compression_kwargs(is_decay=is_decay),
        )
        # Store dimension names if provided in attrs
        if attrs:
            for key, val in attrs.items():
                f[hdf5_path].attrs[key] = val

    def read_array(self, hdf5_path: str, view_bin: int = 1) -> NDArray:
        """Read a numpy array from the specified HDF5 path.

        ``view_bin`` is the session-level view bin (k >= 1). At k=1 the
        array is returned byte-identical to what was written. At k>1 the
        array is downsampled in-memory by the rule appropriate for its
        path:

        ===================================== =====================
        Path prefix                            Rule
        ===================================== =====================
        ``/intensity`` (any rank)              ``sum_bin_2d``
        ``/decay/<ch>``                        ``sum_bin_decay``
        ``/labels/<name>``                     ``mode_labels``
        ``/masks/<name>``                      ``majority_vote_mask``
        ``/phasor/<ch>/{g,s,*_filtered,...}``  ``mean_bin_2d`` (intensive)
        anything else                          pass-through
        ===================================== =====================

        See ``src/percell4/domain/io/view_bin.py`` for the full per-rule
        contract. The on-disk array is unchanged -- this is a read-time
        view only.

        ``intensity`` reads the store's projection (see :meth:`_resolve`);
        its view-bin rule is the same whichever array answers.
        """
        if view_bin < 1:
            raise ValueError(f"view_bin must be >= 1, got {view_bin}")
        f = self._open_read()
        try:
            hdf5_path = self._resolve(f, hdf5_path)
            if hdf5_path not in f:
                raise KeyError(f"Dataset not found: {hdf5_path}")
            obj = f[hdf5_path]
            if not isinstance(obj, h5py.Dataset):
                raise KeyError(f"{hdf5_path} is a group, not a dataset")
            arr = obj[()]
            if view_bin == 1:
                return arr
            return _apply_view_bin(hdf5_path, arr, view_bin)
        finally:
            self._close_if_not_session(f)

    def read_decay(
        self, channel: str, view_bin: int = 1, timepoint: int | None = None
    ) -> NDArray:
        """Read ``/decay/<channel>`` with optional per-timepoint slice + view-bin.

        ``/decay`` is either legacy 3-D ``(H, W, T_bins)`` (single timepoint) or
        time-lapse 4-D ``(T_acq, H, W, T_bins)`` (``dims[0] == "Tacq"``).

        - ``timepoint is None``: return the whole array. ``view_bin > 1`` is
          rejected on a 4-D decay (a whole-tensor sum-bin would fold the
          acquisition axis into the spatial block) -- pass a ``timepoint`` to
          view-bin a time-lapse decay.
        - ``timepoint`` given: slice that acquisition frame **on disk** to a 3-D
          ``(H, W, T_bins)`` array, then apply ``view_bin``. On a legacy 3-D
          decay only ``timepoint == 0`` is valid.

        Sugar/chokepoint over the raw read so a single grep for ``read_decay``
        enumerates every decay-read callsite.
        """
        if view_bin < 1:
            raise ValueError(f"view_bin must be >= 1, got {view_bin}")
        path = f"decay/{channel}"
        f = self._open_read()
        try:
            if path not in f:
                raise KeyError(f"Dataset not found: {path}")
            obj = f[path]
            if not isinstance(obj, h5py.Dataset):
                raise KeyError(f"{path} is a group, not a dataset")
            is_4d = obj.ndim == 4
            if timepoint is None:
                if is_4d and view_bin > 1:
                    raise ValueError(
                        "view_bin > 1 on a time-lapse (4-D) /decay requires a "
                        "timepoint; pass read_decay(channel, view_bin, timepoint=)."
                    )
                arr = obj[()]
                return arr if view_bin == 1 else _apply_view_bin(path, arr, view_bin)
            if is_4d:
                nt = obj.shape[0]
                if not 0 <= timepoint < nt:
                    raise IndexError(
                        f"timepoint={timepoint} out of range [0, {nt}) for {path}"
                    )
                frame = obj[timepoint]  # on-disk slice -> (H, W, T_bins)
            else:
                if timepoint != 0:
                    raise IndexError(
                        f"timepoint={timepoint} invalid for legacy single-timepoint "
                        f"{path} (only 0 is valid)."
                    )
                frame = obj[()]
            return frame if view_bin == 1 else _apply_view_bin(path, frame, view_bin)
        finally:
            self._close_if_not_session(f)

    # ── Stitch geometry (registered overlap mosaics) ──────────

    def write_stitch_geometry(
        self,
        offsets: NDArray,
        provenance: StitchProvenanceRecord,
        *,
        reference_channel: str,
        overlap: float,
        disconnected: tuple[int, ...] = (),
    ) -> None:
        """Persist registered overlap-stitch geometry through the store boundary.

        Writes three things, with the commit marker written **strictly
        last** so a crash mid-write leaves an un-registered (recoverable)
        file rather than a half-registered brick:

        1. ``stitch/tile_offsets`` -- the ``(N, 2) int32`` per-tile
           ``(y0, x0)`` array, via :meth:`write_array` (pass-through
           view-bin, lossless, no ``is_decay``/``dims`` requirement).
        2. ``/provenance/stitch`` group attrs from ``provenance.to_attrs()``
           (mirrors how decay provenance is written -- group attrs).
        3. ``/metadata`` attrs ``stitch_reference_channel`` (str),
           ``stitch_overlap`` (float), and ``stitch_disconnected`` (a
           JSON-encoded list of int tile indices), then
           ``stitch_registered = True`` **last** (the commit point,
           R4/U6 ordering).

        ``disconnected`` is the set of tile indices the solve left at their
        grid seed; it is persisted so a later decay-only append can reproduce
        the import's overlap winner exactly.

        ``offsets`` must have per-axis min exactly 0 (the canvas
        derivation depends on it); this is enforced before any write.
        """
        import json

        offsets = np.asarray(offsets, dtype=np.int32)
        if offsets.ndim != 2 or offsets.shape[1] != 2:
            raise ValueError(
                f"offsets must be (N, 2); got shape {offsets.shape}"
            )
        if offsets.size:
            mins = offsets.min(axis=0)
            # Data invariant (the canvas derivation depends on min==0) — a
            # ``raise``, not an ``assert``, so it survives ``python -O``.
            if not (int(mins[0]) == 0 and int(mins[1]) == 0):
                raise ValueError(
                    f"offsets per-axis min must be 0; "
                    f"got {tuple(int(m) for m in mins)}"
                )

        # 1. Offset array (own write boundary).
        self.write_array("stitch/tile_offsets", offsets)

        # 2. Provenance group attrs (mirror /provenance/decay/<ch>).
        with h5py.File(self.path, "a") as f:
            prov_path = "provenance/stitch"
            if prov_path in f:
                del f[prov_path]
            grp = f.require_group(prov_path)
            for key, val in provenance.to_attrs().items():
                grp.attrs[key] = val

        # 3. Metadata scalars, with the commit marker STRICTLY LAST.
        self.set_metadata(
            {
                "stitch_reference_channel": str(reference_channel),
                "stitch_overlap": float(overlap),
                "stitch_disconnected": json.dumps(
                    [int(i) for i in disconnected]
                ),
            }
        )
        self.set_metadata({"stitch_registered": True})

    def read_stitch_geometry(self) -> StitchGeometry:
        """Read registered overlap-stitch geometry back from the dataset.

        The ``registered`` flag is read **first** from ``/metadata``, fully
        independent of whether ``stitch/tile_offsets`` is present, so the two
        corrupt states the consumers guard against are observable rather than
        silently collapsed to ``registered=False``:

        * flag True + offsets absent → ``registered=True, offsets=None``
          (AE4: a crash after the commit marker but before — or after losing —
          the offset array; the consumer refuses the append).
        * offsets present + flag absent → ``registered=False`` with offsets
          present (a partial/aborted registered import; the consumer refuses).

        Flags are read fresh on every call (never cached on the store) so an
        in-session write is always observed. ``stitch_disconnected`` (JSON list
        of ints) round-trips here, defaulting to ``()``.

        Enforces (via ``raise``, ``-O``-safe) that read-back offsets have
        per-axis min 0 — the same invariant enforced on write.
        """
        import json

        meta = self.metadata
        registered_flag = bool(meta.get("stitch_registered", False))
        disconnected_raw = meta.get("stitch_disconnected")
        if disconnected_raw is None:
            disconnected: tuple[int, ...] = ()
        else:
            disconnected = tuple(int(i) for i in json.loads(disconnected_raw))

        try:
            offsets = self.read_array("stitch/tile_offsets")
        except KeyError:
            offsets = None

        if offsets is not None and offsets.size:
            mins = offsets.min(axis=0)
            if not (int(mins[0]) == 0 and int(mins[1]) == 0):
                raise ValueError(
                    f"persisted offsets per-axis min must be 0; "
                    f"got {tuple(int(m) for m in mins)}"
                )
        return StitchGeometry(
            registered=registered_flag,
            offsets=offsets,
            reference_channel=meta.get("stitch_reference_channel"),
            overlap=meta.get("stitch_overlap"),
            disconnected=disconnected,
        )

    def read_channel(
        self,
        hdf5_path: str,
        channel_idx: int,
        view_bin: int = 1,
        timepoint: int | None = None,
    ) -> NDArray:
        """Read a single channel plane from a 2D, 3D, or time-stacked array.

        For 2D arrays, ``channel_idx`` must be 0 and the full array is returned.
        For 3D ``(C, H, W)`` arrays, returns only ``array[channel_idx]`` without
        loading the other channels — useful for phases that only need one channel
        on each dataset.

        On a **time-stacked** array (leading ``dims[0] == 'T'``), ``timepoint``
        is **required**: the frame is sliced first, then ``channel_idx`` is
        indexed on the resulting ``(H, W)`` (from ``(T, H, W)``) or ``(C, H, W)``
        (from ``(T, C, H, W)``) slice. This is the canonical fix for the old
        behavior that treated a leading ``T`` axis as channels — returning frame
        0 on ``(T, H, W)`` or raising "got 4D" on ``(T, C, H, W)``. Passing
        ``timepoint`` on a non-time-stacked array is ignored, so 2D / ``(C,H,W)``
        reads stay byte-identical.

        ``view_bin`` follows the same rule as :meth:`read_array`. Only
        ``/intensity`` paths are expected here, so the downsampler is
        ``sum_bin_2d`` (the slice is 2D by the time we apply the bin).
        """
        if view_bin < 1:
            raise ValueError(f"view_bin must be >= 1, got {view_bin}")
        f = self._open_read()
        try:
            hdf5_path = self._resolve(f, hdf5_path)
            if hdf5_path not in f:
                raise KeyError(f"Dataset not found: {hdf5_path}")
            ds = f[hdf5_path]
            dims = ds.attrs.get("dims")
            is_time_stacked = (
                dims is not None and len(dims) > 0 and str(dims[0]) == "T"
            )
            if is_time_stacked:
                n_t = ds.shape[0]
                if timepoint is None:
                    raise ValueError(
                        f"{hdf5_path} is time-stacked ({n_t} timepoints); "
                        "read_channel requires an explicit timepoint."
                    )
                if not 0 <= timepoint < n_t:
                    raise IndexError(
                        f"timepoint={timepoint} out of range [0, {n_t})"
                    )
                # (T,H,W) -> (H,W); (T,C,H,W) -> (C,H,W)
                frame = ds[timepoint]
                if frame.ndim == 2:
                    if channel_idx != 0:
                        raise IndexError(
                            f"channel_idx={channel_idx} out of range for a "
                            "single-channel time-stacked array"
                        )
                    arr = frame
                else:
                    n_channels = frame.shape[0]
                    if not 0 <= channel_idx < n_channels:
                        raise IndexError(
                            f"channel_idx={channel_idx} out of range "
                            f"[0, {n_channels})"
                        )
                    arr = frame[channel_idx]
            elif ds.ndim == 2:
                if channel_idx != 0:
                    raise IndexError(
                        f"channel_idx={channel_idx} out of range for 2D array"
                    )
                arr = ds[()]
            elif ds.ndim == 3:
                n_channels = ds.shape[0]
                if not 0 <= channel_idx < n_channels:
                    raise IndexError(
                        f"channel_idx={channel_idx} out of range [0, {n_channels})"
                    )
                arr = ds[channel_idx, ...]
            else:
                raise ValueError(
                    f"read_channel expects 2D or 3D array, got {ds.ndim}D at {hdf5_path}"
                )
            return arr if view_bin == 1 else sum_bin_2d(arr, view_bin)
        finally:
            self._close_if_not_session(f)

    def read_array_frame(
        self, hdf5_path: str, timepoint: int, view_bin: int = 1
    ) -> NDArray:
        """Read a single timepoint frame ``arr[timepoint]`` from a leading-T array.

        Slices on disk so only one frame is loaded (cheap given per-frame
        chunking). The path's view-bin rule is applied to the slice, which
        is one rank lower than the stored array (``(T,H,W)`` -> ``(H,W)``;
        ``(T,C,H,W)`` -> ``(C,H,W)``). For a non-time-stacked array,
        ``timepoint`` must be 0 and the whole array is returned.
        """
        if view_bin < 1:
            raise ValueError(f"view_bin must be >= 1, got {view_bin}")
        f = self._open_read()
        try:
            hdf5_path = self._resolve(f, hdf5_path)
            if hdf5_path not in f:
                raise KeyError(f"Dataset not found: {hdf5_path}")
            obj = f[hdf5_path]
            if not isinstance(obj, h5py.Dataset):
                raise KeyError(f"{hdf5_path} is a group, not a dataset")
            dims = obj.attrs.get("dims")
            is_time_stacked = (
                dims is not None and len(dims) > 0 and str(dims[0]) == "T"
            )
            if not is_time_stacked:
                if timepoint != 0:
                    raise IndexError(
                        f"timepoint={timepoint} out of range: {hdf5_path} is "
                        "not time-stacked"
                    )
                arr = obj[()]
            else:
                n_t = obj.shape[0]
                if not 0 <= timepoint < n_t:
                    raise IndexError(
                        f"timepoint={timepoint} out of range [0, {n_t})"
                    )
                arr = obj[timepoint]
            if view_bin == 1:
                return arr
            return _apply_view_bin(hdf5_path, arr, view_bin)
        finally:
            self._close_if_not_session(f)

    def _is_2d_array(self, hdf5_path: str) -> bool:
        """True when ``hdf5_path`` holds a 2D dataset. Reads shape metadata
        only — no array data is loaded. Raises ``KeyError`` when the path is
        missing or is a group.
        """
        f = self._open_read()
        try:
            hdf5_path = self._resolve(f, hdf5_path)
            if hdf5_path not in f:
                raise KeyError(f"Dataset not found: {hdf5_path}")
            obj = f[hdf5_path]
            if not isinstance(obj, h5py.Dataset):
                raise KeyError(f"{hdf5_path} is a group, not a dataset")
            return obj.ndim == 2
        finally:
            self._close_if_not_session(f)

    def array_exists(self, hdf5_path: str) -> bool:
        """True when ``hdf5_path`` is a dataset in the file. Metadata only —
        **never decompresses**. Use for existence/enablement checks instead of
        ``try: read_array(...) except`` (which decodes the whole stack).

        For ``intensity``, False when no stored projection can answer: a
        z-series-only dataset, or a chosen projection it does not hold.
        """
        f = self._open_read()
        try:
            resolved = self._resolve_soft(f, hdf5_path)
            return (
                resolved is not None
                and resolved in f
                and isinstance(f[resolved], h5py.Dataset)
            )
        finally:
            self._close_if_not_session(f)

    def read_array_attrs(self, hdf5_path: str) -> dict[str, Any]:
        """Return a dataset's HDF5 attrs as a plain dict. Metadata only —
        **never decompresses** the array. Returns ``{}`` when the path is
        absent or names a group rather than a dataset.

        Used to read writer-stamped scalars on a derived array (e.g. the
        wavelet ``filter_level`` on ``/phasor/<ch>/g_filtered``) without
        decoding the array itself.
        """
        f = self._open_read()
        try:
            hdf5_path = self._resolve_soft(f, hdf5_path)
            if (
                hdf5_path is not None
                and hdf5_path in f
                and isinstance(f[hdf5_path], h5py.Dataset)
            ):
                return dict(f[hdf5_path].attrs)
            return {}
        finally:
            self._close_if_not_session(f)

    def array_shape(self, hdf5_path: str) -> tuple[int, ...]:
        """Return a dataset's on-disk shape. Reads HDF5 metadata only — **no
        array data is decompressed**. Use this instead of
        ``read_array(path).shape`` for display/inventory: on a large stacked
        array the latter decompresses the whole stack (gigabytes) just to read
        a shape tuple. Raises ``KeyError`` when missing or a group.
        """
        f = self._open_read()
        try:
            hdf5_path = self._resolve(f, hdf5_path)
            if hdf5_path not in f:
                raise KeyError(f"Dataset not found: {hdf5_path}")
            obj = f[hdf5_path]
            if not isinstance(obj, h5py.Dataset):
                raise KeyError(f"{hdf5_path} is a group, not a dataset")
            return tuple(int(x) for x in obj.shape)
        finally:
            self._close_if_not_session(f)

    def array_dtype(self, hdf5_path: str) -> np.dtype:
        """Return a dataset's on-disk dtype. Reads HDF5 metadata only — **no
        array data is decompressed**. Sibling of :meth:`array_shape` for
        display/inventory tools (e.g. the dataset inspector) that need a
        dtype without paying the cost of decoding a multi-gigabyte stack.
        Raises ``KeyError`` when the path is missing or is a group.
        """
        f = self._open_read()
        try:
            hdf5_path = self._resolve(f, hdf5_path)
            if hdf5_path not in f:
                raise KeyError(f"Dataset not found: {hdf5_path}")
            obj = f[hdf5_path]
            if not isinstance(obj, h5py.Dataset):
                raise KeyError(f"{hdf5_path} is a group, not a dataset")
            return obj.dtype
        finally:
            self._close_if_not_session(f)

    def labels_shape(self, name: str) -> tuple[int, ...]:
        """Return the on-disk shape of ``/labels/<name>`` without loading data.

        Lets callers distinguish a 2D (time-invariant) label from a
        ``(T, H, W)`` stack cheaply — e.g. the workflow's tracking gate,
        which must not try to track a 2D whole-field segmentation.
        """
        f = self._open_read()
        try:
            path = f"labels/{name}"
            if path not in f:
                raise KeyError(f"Dataset not found: {path}")
            obj = f[path]
            if not isinstance(obj, h5py.Dataset):
                raise KeyError(f"{path} is a group, not a dataset")
            return tuple(int(x) for x in obj.shape)
        finally:
            self._close_if_not_session(f)

    def masks_shape(self, name: str) -> tuple[int, ...]:
        """Return the on-disk shape of /masks/<name> without loading data.

        Mirror of :meth:`labels_shape` for masks -- lets callers distinguish a
        2D (time-invariant) mask from a ``(T, H, W)`` stack cheaply. Raises
        ``KeyError`` when the path is missing or is a group.
        """
        f = self._open_read()
        try:
            path = f"masks/{name}"
            if path not in f:
                raise KeyError(f"Dataset not found: {path}")
            obj = f[path]
            if not isinstance(obj, h5py.Dataset):
                raise KeyError(f"{path} is a group, not a dataset")
            return tuple(int(x) for x in obj.shape)
        finally:
            self._close_if_not_session(f)

    def is_time_stacked(self, hdf5_path: str) -> bool:
        """Return ``True`` iff the array at ``hdf5_path`` is time-stacked.

        Reads only the ``dims`` attribute (``dims[0] == 'T'``) -- no array data
        is loaded. The public form of the inline check used by
        :meth:`read_array_frame` and :meth:`read_channel`, so callers (e.g. the
        delete-channel disambiguation) stop re-deriving it from raw shape.
        Returns ``False`` for a missing path, a group, or an array with no/short
        ``dims`` attr.
        """
        f = self._open_read()
        try:
            hdf5_path = self._resolve_soft(f, hdf5_path)
            if hdf5_path is None or hdf5_path not in f:
                return False
            obj = f[hdf5_path]
            if not isinstance(obj, h5py.Dataset):
                return False
            dims = obj.attrs.get("dims")
            return dims is not None and len(dims) > 0 and str(dims[0]) == "T"
        finally:
            self._close_if_not_session(f)

    def check_intensity_dims_consistency(self) -> None:
        """Raise :class:`DimsConsistencyError` when an intensity array's dims are corrupt.

        Detects the Add-Layer corruption signature at dataset-open time: a 3D
        ``/intensity`` stamped ``dims=['C','H','W']`` whose leading-axis size
        disagrees with the declared channel count -- almost certainly a
        ``(T, H, W)`` time-lapse array mis-stamped as channels, which would make
        the dataset read back as single-timepoint and silently collapse every
        time-aware feature to frame 0. Also flags a ``dims`` attr whose length
        doesn't match the array rank.

        Checks the legacy ``/intensity`` or every named projection. No-op when
        there is none, when an array has no ``dims`` attr, or when all are
        consistent. Reads only attributes + shape (no array data).
        """
        f = self._open_read()
        try:
            arrays = []
            for path in _stored_projection_arrays(f).values():
                ds = f[path]
                raw_dims = ds.attrs.get("dims")
                if raw_dims is None:
                    continue
                arrays.append((
                    path,
                    [str(d) for d in raw_dims],
                    int(ds.ndim),
                    tuple(int(x) for x in ds.shape),
                ))
            raw_channel_names = (
                f["metadata"].attrs.get("channel_names")
                if "metadata" in f
                else None
            )
        finally:
            self._close_if_not_session(f)

        if raw_channel_names is None:
            n_channels: int | None = None
            channel_names: list = []
        elif isinstance(raw_channel_names, (bytes, str)):
            n_channels = 1
            channel_names = [raw_channel_names]
        else:
            channel_names = list(raw_channel_names)
            n_channels = len(channel_names)

        for path, dims, ndim, shape in arrays:
            if len(dims) != ndim:
                raise DimsConsistencyError(
                    f"/{path}.dims={dims} has {len(dims)} entries but the array "
                    f"is {ndim}D (shape {shape}). The dims attribute is corrupt."
                )

            # A leading 'C' axis must match the channel count. When it matches
            # neither (n_channels known and != shape[0]), the leading axis is
            # almost certainly a mis-stamped time axis.
            if (
                ndim >= 3
                and dims[0] == "C"
                and n_channels is not None
                and n_channels > 0
                and shape[0] != n_channels
            ):
                raise DimsConsistencyError(
                    f"/{path} has a leading 'C' axis of size {shape[0]} but the "
                    f"dataset declares {n_channels} channel(s) ({channel_names!r}). "
                    "This looks like a (T,H,W) time-lapse array mis-stamped as "
                    "['C','H','W'] (e.g. by an older Add-Layer write); the dataset "
                    "would silently read as single-timepoint. Re-import or correct "
                    f"the /{path} dims attribute."
                )

    # ── Named projections and the z-series ────────────────────

    def _check_xy(self, f: h5py.File, hw: tuple[int, int], kind: str) -> None:
        """Raise :class:`LayerSizeMismatchError` if ``hw`` differs from the
        XY grid the dataset's intensity arrays or z-series already use."""
        existing = _first_intensity_array(f)
        if existing is None and isinstance(f.get(ZSERIES_PATH), h5py.Dataset):
            existing = f[ZSERIES_PATH]
        if existing is None:
            return
        grid = (int(existing.shape[-2]), int(existing.shape[-1]))
        if grid != hw:
            raise LayerSizeMismatchError(
                f"{kind} is {hw[0]}x{hw[1]} but the dataset's images are "
                f"{grid[0]}x{grid[1]}."
            )

    def write_projection(
        self,
        name: str,
        array: NDArray,
        dims: list[str],
        attrs: dict[str, Any] | None = None,
    ) -> int:
        """Write the named projection (``max``, ``mean`` or ``sum``).

        ``array`` has the legacy ``/intensity`` layout: ``(H, W)``,
        ``(C, H, W)``, ``(T, H, W)`` or ``(T, C, H, W)``, described by
        ``dims``. Its XY must match the dataset's other images. Returns the
        number of elements written.
        """
        if name not in PROJECTION_NAMES:
            raise ValueError(
                f"unknown projection {name!r}, expected one of {PROJECTION_NAMES}"
            )
        if len(dims) != array.ndim:
            raise ValueError(f"dims {dims} do not describe a {array.ndim}D array")
        with h5py.File(self.path, "a") as f:
            path = f"{PROJECTIONS_GROUP}/{name}"
            if path in f:
                del f[path]
            self._check_xy(f, (int(array.shape[-2]), int(array.shape[-1])),
                           f"The {name} projection")
            self._write_dataset(f, path, array, {**(attrs or {}), "dims": list(dims)})
        return array.size

    def rewrite_projections(
        self, fn: Callable[[NDArray | None], tuple[NDArray, dict[str, Any]] | None]
    ) -> int:
        """Apply one channel edit to every intensity array.

        ``fn`` receives an array in the ``/intensity`` layout and returns the
        new array with its attrs (including ``dims``), or ``None`` to delete
        it; returning the array it was given (the same object) leaves that
        array untouched. On a dataset with named projections every projection is
        rewritten; on an older dataset, ``/intensity``. A dataset with neither
        gets a new ``/intensity`` from ``fn(None)``, as channel writes did
        before named projections. A z-series-only dataset raises
        :class:`ProjectionRequiredError`: add a projection first.

        The z-series is untouched: a channel added here has no Z axis.
        Returns the number of arrays rewritten.
        """
        with h5py.File(self.path, "r") as f:
            paths = list(_stored_projection_arrays(f).values())
            if not paths and ZSERIES_PATH in f:
                resolve_projection((), None)  # raises "add a projection first"
        if not paths:
            result = fn(None)
            if result is not None:
                array, attrs = result
                self.write_array(INTENSITY_PATH, array, attrs=attrs)
            return 1 if result is not None else 0
        rewritten = 0
        for path in paths:
            f = h5py.File(self.path, "r")
            try:
                current = f[path][()]
            finally:
                f.close()
            result = fn(current)
            if result is not None and result[0] is current:
                continue  # unchanged
            with h5py.File(self.path, "a") as f:
                if result is None:
                    del f[path]
                    grp = f.get(PROJECTIONS_GROUP)
                    if isinstance(grp, h5py.Group) and not len(grp):
                        del f[PROJECTIONS_GROUP]
                else:
                    array, attrs = result
                    self._write_dataset(f, path, array, attrs)
            rewritten += 1
        return rewritten

    @contextmanager
    def open_stack_writer(self):
        """Yield a writer that pre-allocates projections and a z-series and
        fills them plane by plane, under one open file handle.

        Used by imports, which stream planes: nothing is assembled in memory.
        If the block raises, every array the writer created is removed.
        """
        with h5py.File(self.path, "a") as f:
            writer = _StackWriter(self, f)
            try:
                yield writer
            except BaseException:
                for path in writer.created:
                    if path in f:
                        del f[path]
                grp = f.get(PROJECTIONS_GROUP)
                if isinstance(grp, h5py.Group) and not len(grp):
                    del f[PROJECTIONS_GROUP]
                raise

    @contextmanager
    def zseries_writer(self, shape: tuple[int, ...], channel_names: list[str]):
        """Pre-allocate the z-series and yield a writer that fills it plane by plane.

        ``shape`` is ``(C, Z, H, W)`` or ``(T, C, Z, H, W)``; ``channel_names``
        names the ``C`` axis (only channels that had a Z axis). The array is
        float32 with one plane per chunk, so a plane read decodes one chunk.
        Any existing z-series is replaced. If the block raises, the partial
        z-series is removed. The file stays open for the block.
        """
        with self.open_stack_writer() as stack:
            yield stack.begin_zseries(shape, channel_names)

    def has_zseries(self) -> bool:
        """True when the dataset stores a z-series. Metadata only."""
        if not self.path.exists():
            return False
        f = self._open_read()
        try:
            return isinstance(f.get(ZSERIES_PATH), h5py.Dataset)
        finally:
            self._close_if_not_session(f)

    def _zseries_meta(self) -> tuple[tuple[int, ...], tuple[str, ...], tuple[str, ...]]:
        """``(shape, dims, channel_names)`` of the z-series. Metadata only."""
        f = self._open_read()
        try:
            ds = f.get(ZSERIES_PATH)
            if not isinstance(ds, h5py.Dataset):
                raise KeyError(f"Dataset not found: {ZSERIES_PATH}")
            return (
                tuple(int(x) for x in ds.shape),
                tuple(str(d) for d in ds.attrs.get("dims", ())),
                _decode_names(ds.attrs.get("channel_names", ())),
            )
        finally:
            self._close_if_not_session(f)

    def zseries_shape(self) -> tuple[int, ...]:
        """The z-series shape, ``(C, Z, H, W)`` or ``(T, C, Z, H, W)``."""
        return self._zseries_meta()[0]

    def zseries_dims(self) -> tuple[str, ...]:
        """The z-series ``dims`` attr, e.g. ``("T", "C", "Z", "H", "W")``."""
        return self._zseries_meta()[1]

    def zseries_channels(self) -> tuple[str, ...]:
        """Names of the channels the z-series holds, in ``C`` order."""
        return self._zseries_meta()[2]

    def read_zseries_plane(
        self, t: int, c: int, z: int, view_bin: int = 1
    ) -> NDArray:
        """Read one z-series plane, sum-binned by ``view_bin``.

        ``t`` must be 0 on a z-series without a time axis.
        """
        if view_bin < 1:
            raise ValueError(f"view_bin must be >= 1, got {view_bin}")
        f = self._open_read()
        try:
            ds = f.get(ZSERIES_PATH)
            if not isinstance(ds, h5py.Dataset):
                raise KeyError(f"Dataset not found: {ZSERIES_PATH}")
            if ds.ndim == 5:
                plane = ds[t, c, z]
            else:
                if t != 0:
                    raise IndexError(f"timepoint={t} out of range: the z-series has no T axis")
                plane = ds[c, z]
            return plane if view_bin == 1 else sum_bin_2d(plane, view_bin)
        finally:
            self._close_if_not_session(f)

    def delete_zseries_channel(self, name: str) -> bool:
        """Remove channel ``name`` from the z-series; the z-series goes when it
        was the last one. True if anything was removed."""
        if not self.path.exists():
            return False
        with h5py.File(self.path, "a") as f:
            return self._drop_zseries_channel(f, name)

    @staticmethod
    def _drop_zseries_channel(f: h5py.File, name: str) -> bool:
        """Remove channel ``name`` from the z-series, plane by plane."""
        ds = f.get(ZSERIES_PATH)
        if not isinstance(ds, h5py.Dataset):
            return False
        names = list(_decode_names(ds.attrs.get("channel_names", ())))
        if name not in names:
            return False
        if len(names) == 1:
            del f[ZSERIES_PATH]
            return True
        drop = names.index(name)
        keep = [c for c in range(len(names)) if c != drop]
        timed = ds.ndim == 5
        shape = list(ds.shape)
        shape[-4] = len(keep)
        tmp_path = f"{ZSERIES_PATH}.tmp"
        if tmp_path in f:
            del f[tmp_path]
        new = f.create_dataset(
            tmp_path, shape=tuple(shape), dtype=ds.dtype, chunks=ds.chunks,
            **_compression_kwargs(),
        )
        for key, val in ds.attrs.items():
            new.attrs[key] = val
        new.attrs["channel_names"] = [names[c] for c in keep]
        n_t = ds.shape[0] if timed else 1
        n_z = ds.shape[-3]
        for t in range(n_t):
            for out_c, c in enumerate(keep):
                for z in range(n_z):
                    if timed:
                        new[t, out_c, z] = ds[t, c, z]
                    else:
                        new[out_c, z] = ds[c, z]
        del f[ZSERIES_PATH]
        f.move(tmp_path, ZSERIES_PATH)
        return True

    # ── DataFrame operations ──────────────────────────────────

    def write_dataframe(self, hdf5_path: str, df: pd.DataFrame) -> int:
        """Write a pandas DataFrame as a CSV string at the given path.

        Returns the number of rows written.
        """
        with h5py.File(self.path, "a") as f:
            if hdf5_path in f:
                del f[hdf5_path]
            csv_str = df.to_csv(index=False)
            f.create_dataset(hdf5_path, data=csv_str)
        return len(df)

    def read_dataframe(self, hdf5_path: str) -> pd.DataFrame:
        """Read a pandas DataFrame from a CSV string at the given path."""
        f = self._open_read()
        try:
            if hdf5_path not in f:
                raise KeyError(f"Dataset not found: {hdf5_path}")
            csv_bytes = f[hdf5_path][()]
            if isinstance(csv_bytes, bytes):
                csv_str = csv_bytes.decode("utf-8")
            else:
                csv_str = str(csv_bytes)
            return pd.read_csv(StringIO(csv_str))
        finally:
            self._close_if_not_session(f)

    # ── Convenience: labels ───────────────────────────────────

    def _native_shape_and_timepoints(self) -> tuple[tuple[int, int] | None, int]:
        """Return ``(native_shape | None, n_timepoints)`` for write validation.

        Reads ``/metadata`` and infers from ``/intensity``. Returns
        ``(None, 1)`` when the file doesn't exist yet or carries no spatial
        array, so callers can skip the cross-check in that case.
        """
        if not self.path.exists():
            return None, 1
        meta = self.metadata
        ns = meta.get("native_shape")
        if ns is not None:
            ns = tuple(int(x) for x in ns)
        nt = int(meta.get("n_timepoints", 1) or 1)
        return ns, nt

    def _validate_layer_shape(self, array: NDArray, kind: str) -> list[str]:
        """Validate a label/mask array shape and return its ``dims`` attr.

        Accepts 2D ``(H, W)`` (always) or time-stacked ``(T, H, W)`` where
        the trailing two dims equal the dataset's ``native_shape`` and the
        leading axis equals ``n_timepoints``. Anything else raises.
        """
        if array.ndim == 2:
            return ["H", "W"]
        ns, nt = self._native_shape_and_timepoints()
        # A 3D array is a legitimate time stack only on a time-lapse dataset
        # (n_timepoints > 1). On any other dataset — including an empty one
        # whose timepoint count is unknown (defaults to 1) — keep the old
        # "labels/masks must be 2D" contract.
        if array.ndim != 3 or nt <= 1:
            raise ValueError(
                f"{kind} must be 2D (H,W); got {array.ndim}D. A 3D (T,H,W) "
                "stack is only accepted on a time-lapse dataset "
                "(n_timepoints > 1)."
            )
        th, tw = int(array.shape[-2]), int(array.shape[-1])
        if ns is not None and (th, tw) != ns:
            raise LayerSizeMismatchError(
                f"{kind} (T,H,W) trailing dims {(th, tw)} disagree with "
                f"dataset native_shape {ns}."
            )
        if int(array.shape[0]) != nt:
            raise LayerSizeMismatchError(
                f"{kind} has {int(array.shape[0])} frame(s) but the dataset "
                f"has {nt} timepoints; the time axis would mis-stack."
            )
        return ["T", "H", "W"]

    def _write_resource_frame(
        self,
        prefix: str,
        name: str,
        frame: NDArray,
        timepoint: int,
        dtype: Any,
        kind: str,
        whole_writer,
    ) -> int:
        """Splice a single timepoint's 2D ``frame`` into a labels/masks resource.

        Shared core for :meth:`write_labels_frame` / :meth:`write_mask_frame`.
        Three cases (see those methods for the contract):

        1. **Absent** on a time-lapse dataset: allocate a ``(T, H, W)`` zero
           stack and set frame ``timepoint``.
        2. **Present and 2D** (time-invariant) on a time-lapse dataset:
           broadcast the existing plane to ``(T, H, W)``, splice the frame, and
           write the stack. This irreversibly converts a time-invariant gate
           into a per-frame stack, so it is logged (surfaced), not silent.
        3. **Present and ``(T, H, W)``**: assign ``ds[timepoint] = frame`` in
           place under an ``'a'`` open — no delete+recreate, so the other
           frames' bytes are untouched.

        On a single-timepoint dataset (``n_timepoints == 1``) the call is a 2D
        write at ``timepoint == 0``, byte-identical to ``whole_writer``.
        """
        if frame.ndim != 2:
            raise ValueError(f"{kind} frame must be 2D (H,W); got {frame.ndim}D")
        ns, nt = self._native_shape_and_timepoints()
        if not 0 <= timepoint < nt:
            raise IndexError(f"timepoint={timepoint} out of range [0, {nt})")
        if ns is not None and tuple(int(x) for x in frame.shape) != ns:
            raise LayerSizeMismatchError(
                f"{kind} frame shape {tuple(int(x) for x in frame.shape)} "
                f"disagrees with dataset native_shape {ns}."
            )
        frame = frame.astype(dtype, copy=False)
        path = f"{prefix}/{name}"

        # Single-timepoint dataset: a per-frame write at t=0 is just the 2D write.
        if nt <= 1:
            return whole_writer(name, frame)

        present = False
        is_2d = False
        if self.path.exists():
            with h5py.File(self.path, "r") as f:
                present = path in f
                if present:
                    is_2d = f[path].ndim == 2

        h, w = ns if ns is not None else (int(frame.shape[0]), int(frame.shape[1]))

        if not present:
            # Case 1: allocate a (T,H,W) zero stack, splice the frame, validate-write.
            stack = np.zeros((nt, h, w), dtype=dtype)
            stack[timepoint] = frame
            return whole_writer(name, stack)

        if is_2d:
            # Case 2: promote a 2D time-invariant resource to (T,H,W).
            with h5py.File(self.path, "r") as f:
                existing = np.asarray(f[path][()])
            stack = np.broadcast_to(existing, (nt, h, w)).astype(dtype, copy=True)
            stack[timepoint] = frame
            logger.info(
                "Promoting 2D time-invariant %s '%s' to a (T,H,W) per-frame "
                "stack on first per-frame write (timepoint=%d).",
                kind, name, timepoint,
            )
            return whole_writer(name, stack)

        # Case 3: existing (T,H,W) -- in-place single-frame write, no recreate.
        with h5py.File(self.path, "a") as f:
            f[path][timepoint] = frame
        return int(frame.size)

    def _validate_decay_shape(self, array: NDArray) -> list[str]:
        """Validate a /decay array shape and return its ``dims`` attr.

        Accepts legacy 3-D ``(H, W, T_bins)`` (single timepoint, T_acq == 1)
        always, or time-lapse 4-D ``(T_acq, H, W, T_bins)`` only on a time-lapse
        dataset (``n_timepoints > 1``) whose leading axis equals ``n_timepoints``
        and whose spatial dims ``[1:3]`` equal ``native_shape``.
        """
        if array.ndim == 3:
            return ["H", "W", "T"]
        if array.ndim != 4:
            raise ValueError(
                f"Decay must be 3-D (H,W,T) or 4-D (T_acq,H,W,T_bins); got "
                f"{array.ndim}-D."
            )
        ns, nt = self._native_shape_and_timepoints()
        if nt <= 1:
            raise ValueError(
                "A 4-D (T_acq,H,W,T_bins) decay is only accepted on a time-lapse "
                "dataset (n_timepoints > 1)."
            )
        th, tw = int(array.shape[1]), int(array.shape[2])
        if ns is not None and (th, tw) != ns:
            raise LayerSizeMismatchError(
                f"Decay spatial dims {(th, tw)} disagree with dataset "
                f"native_shape {ns}."
            )
        if int(array.shape[0]) != nt:
            raise LayerSizeMismatchError(
                f"Decay has {int(array.shape[0])} acquisition frame(s) but the "
                f"dataset has {nt} timepoints; the time axis would mis-stack."
            )
        return ["Tacq", "H", "W", "T"]

    def write_decay_frame(
        self, channel: str, frame: NDArray, timepoint: int
    ) -> int:
        """Splice one acquisition timepoint's 3-D decay ``frame`` into
        ``/decay/<channel>``.

        The per-frame counterpart to :meth:`read_decay` with a ``timepoint``.
        ``frame`` is ``(H, W, T_bins)``. On a single-timepoint dataset
        (``n_timepoints == 1``) this is a plain 3-D decay write at
        ``timepoint == 0``. On a time-lapse dataset it allocates a
        ``(T_acq, H, W, T_bins)`` dataset on first write (lzf, per-frame chunks)
        and writes frame ``timepoint`` in place thereafter. Invalidates any
        cached ``/phasor/<channel>`` (derived-layer staleness rule).
        """
        if frame.ndim != 3:
            raise ValueError(
                f"decay frame must be 3-D (H,W,T_bins); got {frame.ndim}-D"
            )
        ns, nt = self._native_shape_and_timepoints()
        if not 0 <= timepoint < nt:
            raise IndexError(f"timepoint={timepoint} out of range [0, {nt})")
        if ns is not None and (int(frame.shape[0]), int(frame.shape[1])) != ns:
            raise LayerSizeMismatchError(
                f"decay frame spatial shape "
                f"{(int(frame.shape[0]), int(frame.shape[1]))} disagrees with "
                f"dataset native_shape {ns}."
            )
        frame = np.ascontiguousarray(frame, dtype=np.float32)
        path = f"decay/{channel}"
        phasor_path = f"phasor/{channel}"

        # Single-timepoint dataset: a plain 3-D decay write (T_acq == 1).
        if nt <= 1:
            return self.write_array(
                path, frame, is_decay=True,
                attrs={"dims": ["H", "W", "T"], "channel": channel},
            )

        h, w, t_bins = (
            int(frame.shape[0]), int(frame.shape[1]), int(frame.shape[2])
        )
        shape4 = (nt, h, w, t_bins)
        with h5py.File(self.path, "a") as f:
            existing = f.get(path)
            if existing is not None and tuple(existing.shape) != shape4:
                del f[path]
                existing = None
            # Invalidate stale phasor whenever the decay is (re)written.
            if phasor_path in f:
                del f[phasor_path]
            if existing is None:
                ds = f.create_dataset(
                    path, shape=shape4, dtype=np.float32,
                    chunks=_choose_chunks(shape4, is_decay=True),
                    **_compression_kwargs(is_decay=True),
                )
                ds.attrs["dims"] = ["Tacq", "H", "W", "T"]
                ds.attrs["channel"] = channel
            else:
                ds = existing
            ds[timepoint] = frame
        return int(frame.size)

    def write_labels(
        self,
        name: str,
        array: NDArray,
        attrs: dict[str, Any] | None = None,
    ) -> int:
        """Write a segmentation label array at /labels/<name>.

        Enforces int32 dtype. Accepts 2D ``(H, W)`` or time-stacked
        ``(T, H, W)`` (validated against the dataset's ``native_shape`` and
        ``n_timepoints``). Returns element count.

        ``attrs`` (optional) are merged onto the canonical ``{"dims": ...}``
        so Phase-6 Creators can stamp ``created_at_bin`` without bypassing
        the chokepoint.
        """
        dims = self._validate_layer_shape(array, "Labels")
        array = array.astype(np.int32, copy=False)
        merged_attrs: dict[str, Any] = {"dims": dims}
        if attrs:
            merged_attrs.update(attrs)
        return self.write_array(f"labels/{name}", array, attrs=merged_attrs)

    def write_labels_frame(
        self, name: str, frame: NDArray, timepoint: int
    ) -> int:
        """Write a single timepoint's 2D ``frame`` into /labels/<name>.

        The symmetric per-frame counterpart to :meth:`read_labels` with a
        ``timepoint``. Lets interactive editors and per-frame Creators persist
        one frame without re-implementing read-splice-write. ``frame`` is a 2D
        ``(H, W)`` int32 label plane; ``timepoint`` is in ``[0, n_timepoints)``.
        See :meth:`_write_resource_frame` for the absent / 2D-promote / in-place
        cases. Enforces int32 dtype.
        """
        return self._write_resource_frame(
            "labels", name, frame, timepoint, np.int32, "Labels", self.write_labels
        )

    def read_labels(
        self, name: str, view_bin: int = 1, timepoint: int | None = None
    ) -> NDArray[np.int32]:
        """Read a segmentation label array from /labels/<name>.

        ``view_bin`` follows the same rule as :meth:`read_array`. Labels
        downsample via block mode (ties resolve to 0). When ``timepoint``
        is given, returns only that frame of a time-stacked ``(T, H, W)``
        labels resource (``(H, W)``); leave it ``None`` to read the whole
        array (the full stack for time-lapse, or the 2D array otherwise).

        A 2D label on a time-lapse dataset is *time-invariant* (e.g. a
        whole-field gate written by ``percell-batch-whole-field``): a
        per-timepoint read broadcasts it, returning the same ``(H, W)``
        frame for every ``timepoint``. Without this, per-frame phases
        (threshold / measure) would hit ``read_array_frame``'s
        "not time-stacked" guard for any ``timepoint != 0``.
        """
        path = f"labels/{name}"
        if timepoint is None:
            return self.read_array(path, view_bin=view_bin)
        if self._is_2d_array(path):
            return self.read_array(path, view_bin=view_bin)
        return self.read_array_frame(path, timepoint, view_bin=view_bin)

    def list_labels(self) -> list[str]:
        """List all label set names under /labels/."""
        return self.list_groups("labels")

    # ── Convenience: masks ────────────────────────────────────

    def write_mask(
        self,
        name: str,
        array: NDArray,
        attrs: dict[str, Any] | None = None,
    ) -> int:
        """Write a mask (binary or multi-label) at /masks/<name>.

        Enforces uint8 dtype. Values 0-255 supported:
        - Binary: 0=outside, 1=inside
        - Multi-label: 0=outside, 1..N=ROI labels
        Returns element count.

        Accepts 2D ``(H, W)`` or time-stacked ``(T, H, W)`` (validated
        against the dataset's ``native_shape`` and ``n_timepoints``).

        ``attrs`` (optional) are merged onto the canonical ``{"dims": ...}``
        so Phase-6 Creators can stamp ``created_at_bin`` and any provenance
        keys without bypassing the chokepoint.
        """
        dims = self._validate_layer_shape(array, "Mask")
        array = array.astype(np.uint8, copy=False)
        merged_attrs: dict[str, Any] = {"dims": dims}
        if attrs:
            merged_attrs.update(attrs)
        return self.write_array(f"masks/{name}", array, attrs=merged_attrs)

    def write_mask_frame(
        self, name: str, frame: NDArray, timepoint: int
    ) -> int:
        """Write a single timepoint's 2D ``frame`` into /masks/<name>.

        The symmetric per-frame counterpart to :meth:`read_mask` with a
        ``timepoint``. ``frame`` is a 2D ``(H, W)`` uint8 mask plane;
        ``timepoint`` is in ``[0, n_timepoints)``. See
        :meth:`_write_resource_frame` for the absent / 2D-promote / in-place
        cases. Enforces uint8 dtype.
        """
        return self._write_resource_frame(
            "masks", name, frame, timepoint, np.uint8, "Mask", self.write_mask
        )

    def read_mask(
        self, name: str, view_bin: int = 1, timepoint: int | None = None
    ) -> NDArray[np.uint8]:
        """Read a mask from /masks/<name>.

        ``view_bin`` follows the same rule as :meth:`read_array`. Masks
        downsample via majority vote (>= ceil(k**2 / 2)). When ``timepoint``
        is given, returns only that frame of a time-stacked ``(T, H, W)``
        mask resource.

        A 2D mask on a time-lapse dataset is *time-invariant* (e.g. a whole-field
        or ROI gate): a per-timepoint read broadcasts it, returning the same
        ``(H, W)`` frame for every ``timepoint``. This mirrors
        :meth:`read_labels` — without the broadcast guard a ``timepoint != 0``
        read of a 2D mask hits ``read_array_frame``'s "not time-stacked" guard
        and raises ``IndexError``.
        """
        path = f"masks/{name}"
        if timepoint is None:
            return self.read_array(path, view_bin=view_bin)
        if self._is_2d_array(path):
            return self.read_array(path, view_bin=view_bin)
        return self.read_array_frame(path, timepoint, view_bin=view_bin)

    def list_masks(self) -> list[str]:
        """List all mask names under /masks/."""
        return self.list_groups("masks")

    # ── Convenience: tracks (lineage tables) ──────────────────

    def write_tracks(self, name: str, df: pd.DataFrame) -> int:
        """Write a lineage table at /tracks/<name> (CSV-string, like measurements).

        ``df`` is the per-track lineage table (track_id, tree_id, begin_t,
        end_t, parent_track_id). Stored as a sibling of the matching
        ``/labels/<name>`` tracked segmentation.
        """
        return self.write_dataframe(f"tracks/{name}", df)

    def read_tracks(self, name: str) -> pd.DataFrame:
        """Read the lineage table from /tracks/<name>."""
        return self.read_dataframe(f"tracks/{name}")

    def list_tracks(self) -> list[str]:
        """List all lineage-table names under /tracks/."""
        return self.list_groups("tracks")

    def source_channel(self, kind: str, name: str) -> str | None:
        """The channel ``/labels/<name>`` (``kind="labels"``) or
        ``/masks/<name>`` was made from, or ``None`` when not recorded."""
        value = self.read_array_attrs(f"{kind}/{name}").get(SOURCE_CHANNEL_ATTR)
        if isinstance(value, bytes):
            value = value.decode()
        return str(value) if value else None

    def set_mask_attrs(self, name: str, attrs: dict[str, Any]) -> None:
        """Write HDF5 attributes onto an existing /masks/<name> dataset.

        HDF5 attributes do not accept Python ``None``. Callers must
        substitute sentinel values themselves (e.g., 0.0 for an unset
        intensity threshold, -1.0 for an unset radius, "" for an empty
        string). Keys whose value is ``None`` are skipped.

        Raises ``KeyError`` if /masks/<name> does not exist.
        """
        path = f"masks/{name}"
        with h5py.File(self.path, "a") as f:
            if path not in f:
                raise KeyError(f"Mask not found: {path}")
            ds = f[path]
            for key, val in attrs.items():
                if val is None:
                    continue
                ds.attrs[key] = val

    # ── Groups and metadata ───────────────────────────────────

    def list_groups(self, prefix: str) -> list[str]:
        """List child dataset/group names under a given path."""
        f = self._open_read()
        try:
            if prefix not in f:
                return []
            return list(f[prefix].keys())
        finally:
            self._close_if_not_session(f)

    @property
    def metadata(self) -> dict[str, Any]:
        """Read /metadata/ group attributes as a dict.

        Two keys are guaranteed to be present whenever the dataset has any
        spatial array on disk, even on files written before the
        dataset-wide binning model existed:

        * ``native_shape`` -- ``(H, W)`` at k=1. Inferred from
          ``/intensity.shape[-2:]`` when absent. If neither ``/intensity``
          nor any ``/decay/<ch>`` exists, this key is set to ``None``.
        * ``creation_bin`` -- defaults to ``1`` when absent.

        Inference is in-memory only here -- the file is not rewritten.
        The next :meth:`set_metadata` call persists the inferred values
        (see that method for the consistency-check rule).
        """
        f = self._open_read()
        try:
            if "metadata" in f:
                attrs = dict(f["metadata"].attrs)
            else:
                attrs = {}
            inferred = _infer_bin_metadata(f)
            for key, val in inferred.items():
                attrs.setdefault(key, val)
            # Normalize native_shape to a Python tuple regardless of source
            # (h5py returns numpy arrays for sequence attrs).
            if attrs.get("native_shape") is not None:
                ns = attrs["native_shape"]
                if hasattr(ns, "tolist"):
                    ns = ns.tolist()
                attrs["native_shape"] = tuple(int(x) for x in ns)
            # Normalize channel_names to a Python list[str]: h5py returns a
            # numpy string array for a multi-element sequence attr, whose
            # truthiness is ambiguous (``arr or []`` raises). Decode bytes too.
            if attrs.get("channel_names") is not None:
                cn = attrs["channel_names"]
                if isinstance(cn, (str, bytes)):
                    cn = [cn]
                elif hasattr(cn, "tolist"):
                    cn = cn.tolist()
                attrs["channel_names"] = [
                    c.decode() if isinstance(c, bytes) else str(c) for c in cn
                ]
            # Free-text description: decode bytes and collapse a blank value
            # to absent, so the dict agrees with the `description` property
            # that empty and missing are the same state.
            if "description" in attrs:
                normalized = _normalize_description(attrs["description"])
                if normalized is None:
                    del attrs["description"]
                else:
                    attrs["description"] = normalized
            if "creation_bin" in attrs:
                attrs["creation_bin"] = int(attrs["creation_bin"])
            if "n_timepoints" in attrs:
                attrs["n_timepoints"] = int(attrs["n_timepoints"])
            # In-file import provenance: plain Python types, as for the keys
            # above, so callers and JSON output never see numpy scalars.
            if "z_spacing_um" in attrs:
                attrs["z_spacing_um"] = float(attrs["z_spacing_um"])
            if "source_series" in attrs:
                attrs["source_series"] = int(attrs["source_series"])
            if isinstance(attrs.get("z_projection"), bytes):
                attrs["z_projection"] = attrs["z_projection"].decode()
            if "stitch_overlap" in attrs:
                attrs["stitch_overlap"] = float(attrs["stitch_overlap"])
            if "stitch_registered" in attrs:
                attrs["stitch_registered"] = bool(attrs["stitch_registered"])
            return attrs
        finally:
            self._close_if_not_session(f)

    def set_metadata(self, attrs: dict[str, Any]) -> int:
        """Write attributes to the /metadata/ group. Returns count written.

        As a side effect, persists inferred ``native_shape`` and
        ``creation_bin`` from :attr:`metadata` if they aren't on disk yet.
        Raises :class:`MetadataConsistencyError` if a stored
        ``native_shape`` disagrees with what we infer from the actual
        array on disk -- we never silently overwrite an explicit value.
        """
        with h5py.File(self.path, "a") as f:
            grp = f.require_group("metadata")
            inferred = _infer_bin_metadata(f)
            if "native_shape" in grp.attrs:
                stored = tuple(int(x) for x in grp.attrs["native_shape"])
                if (
                    inferred["native_shape"] is not None
                    and stored != inferred["native_shape"]
                ):
                    raise MetadataConsistencyError(
                        f"Stored /metadata.native_shape={stored} disagrees "
                        f"with on-disk shape={inferred['native_shape']}."
                    )
            else:
                if inferred["native_shape"] is not None:
                    grp.attrs["native_shape"] = inferred["native_shape"]
            grp.attrs.setdefault("creation_bin", inferred["creation_bin"])
            for key, val in attrs.items():
                grp.attrs[key] = val
        return len(attrs)

    def delete_metadata_key(self, key: str) -> bool:
        """Remove one attribute from the /metadata group.

        Returns True if the attribute was there and is now gone, False if
        there was nothing to remove. :meth:`set_metadata` merges keys and
        cannot delete one, so this is the only way to take a metadata entry
        back off a dataset.
        """
        if not self.path.exists():
            return False
        with h5py.File(self.path, "a") as f:
            grp = f.get("metadata")
            if grp is None or key not in grp.attrs:
                return False
            del grp.attrs[key]
            return True

    # ── Dataset description ───────────────────────────────────

    @property
    def description(self) -> str | None:
        """The dataset's free-text experiment description, or None.

        Absent and blank are the same state: a dataset either has a
        description or it has none. Bytes are decoded, so a value written
        by an older writer still reads back as ``str``.
        """
        f = self._open_read()
        try:
            grp = f.get("metadata")
            if grp is None or "description" not in grp.attrs:
                return None
            return _normalize_description(grp.attrs["description"])
        finally:
            self._close_if_not_session(f)

    def set_description(self, text: str | None) -> str | None:
        """Write the dataset's description, or clear it when blank.

        ``None``, an empty string, and whitespace-only text all clear
        rather than writing, so a confirmed edit that emptied the field and
        an explicit clear leave identical bytes on disk. Writing goes
        through :meth:`set_metadata` so its inferred-bin-metadata side
        effects stay identical to every other metadata write.

        Returns what is now stored -- the text, or ``None`` when the call
        cleared. Callers that need to display or cache the result use this
        instead of re-deriving the blank rule or re-reading the file.
        """
        if text is None or not text.strip():
            self.clear_description()
            return None
        self.set_metadata({"description": text})
        return text

    def clear_description(self) -> bool:
        """Remove the description. Returns True if one was removed."""
        return self.delete_metadata_key("description")

    # ── File lifecycle ────────────────────────────────────────

    def create(self, metadata: dict[str, Any] | None = None) -> None:
        """Create a new empty .h5 file, optionally with metadata."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with h5py.File(self.path, "w") as f:
            if metadata:
                grp = f.create_group("metadata")
                for key, val in metadata.items():
                    grp.attrs[key] = val

    def exists(self) -> bool:
        """Check if the .h5 file exists."""
        return self.path.exists()

    def delete_item(self, hdf5_path: str) -> bool:
        """Delete a dataset or group at the given HDF5 path. Returns True if deleted.

        Refuses ``intensity`` on a dataset with named projections, like
        :meth:`write_array`; use :meth:`rewrite_projections` there.
        """
        with h5py.File(self.path, "a") as f:
            self._refuse_legacy_intensity_write(f, hdf5_path)
            if hdf5_path in f:
                del f[hdf5_path]
                return True
        return False

    def rename_item(self, old_path: str, new_path: str) -> bool:
        """Rename a dataset or group within the HDF5 file. Returns True if renamed."""
        with h5py.File(self.path, "a") as f:
            if old_path not in f:
                return False
            if new_path in f:
                raise ValueError(f"Target path already exists: {new_path}")
            f.move(old_path, new_path)
            return True

    def append_decay_layers(
        self,
        layers: dict[str, NDArray],
        provenance: dict[str, ProvenanceRecord],
        cross_format_rule: CrossFormatRule | None = None,
        force: bool = False,
    ) -> int:
        """Append per-channel TCSPC decay arrays to an existing dataset.

        Single chokepoint for ``/decay/<channel_name>`` writes — paired with
        a structured ``ProvenanceRecord`` per channel under
        ``/provenance/decay/<channel_name>``. Raises ``LayerAlreadyExists`` if
        any target ``/decay/<name>`` exists and ``force=False``.

        ``cross_format_rule`` (when provided) is persisted to
        ``/metadata.attrs[cross_format_rule]`` on first call. Subsequent calls
        with the same rule are a no-op on the metadata; calls with a different
        rule raise ``CrossFormatRuleConflict`` unless ``force=True``.
        ``ExplicitRule`` is exempt from conflict checks — it represents a
        per-binding override, not a base-rule change.

        Per-channel atomicity is best-effort: each channel's decay write +
        provenance write happen under one open file handle, with explicit
        ``flush()`` + ``fsync()`` between channels. HDF5 power-loss safety is
        not guaranteed (no journaling).
        """
        if set(layers.keys()) != set(provenance.keys()):
            missing = set(layers.keys()) - set(provenance.keys())
            extra = set(provenance.keys()) - set(layers.keys())
            raise ValueError(
                f"layers and provenance must agree on channel names — "
                f"provenance missing: {sorted(missing)}, extra: {sorted(extra)}"
            )

        if not layers:
            return 0

        # Pre-flight: rule conflict check
        if cross_format_rule is not None and not isinstance(cross_format_rule, ExplicitRule):
            existing_serialized = self.metadata.get("cross_format_rule")
            if existing_serialized is not None and not force:
                existing = deserialize_rule(existing_serialized)
                if existing != cross_format_rule:
                    raise CrossFormatRuleConflictError(
                        f"persisted rule {existing!r} differs from {cross_format_rule!r}; "
                        "use force=True to overwrite"
                    )

        # Pre-flight: existence check
        if not force:
            with h5py.File(self.path, "r") as f:
                for name in layers:
                    path = f"decay/{name}"
                    if path in f:
                        raise LayerAlreadyExistsError(name)

        # Write each channel under one open handle with explicit flush+fsync
        # between channels. Best-effort per-channel atomicity. Decay arrays
        # are cast to float32 to match compress's storage format — phasor
        # math runs in float64 either way, but matching dtype keeps disk
        # layout consistent between the two import flows so downstream
        # tools don't see a uint32-vs-float32 discrepancy.
        with h5py.File(self.path, "a") as f:
            for name, decay in layers.items():
                path = f"decay/{name}"
                if path in f:
                    del f[path]
                if decay.dtype != np.float32:
                    decay = decay.astype(np.float32, copy=False)
                chunks = _choose_chunks(decay.shape, is_decay=True)
                f.create_dataset(
                    path,
                    data=decay,
                    chunks=chunks,
                    **_compression_kwargs(is_decay=True),
                )
                # Provenance group + attrs
                prov_path = f"provenance/decay/{name}"
                if prov_path in f:
                    del f[prov_path]
                grp = f.require_group(prov_path)
                for key, val in provenance[name].to_attrs().items():
                    grp.attrs[key] = val
                # Best-effort flush + fsync between channels
                f.flush()
                try:
                    fd = f.id.get_vfd_handle()
                    if isinstance(fd, tuple):
                        fd = fd[0]
                    if isinstance(fd, int) and fd >= 0:
                        os.fsync(fd)
                except (AttributeError, OSError, ValueError):
                    # Some VFDs don't expose a POSIX fd; flush() alone has to suffice.
                    pass

            # Persist the dropdown-level rule to /metadata
            if cross_format_rule is not None and not isinstance(cross_format_rule, ExplicitRule):
                grp = f.require_group("metadata")
                grp.attrs["cross_format_rule"] = serialize_rule(cross_format_rule)

        return len(layers)

    def delete_channel(self, name: str) -> bool:
        """Remove every per-channel surface for ``name``.

        Symmetric pair to :meth:`rename_channel`. Sweeps all four
        per-channel surfaces in one ``h5py.File(..., "a")`` open:

        - ``/decay/<name>`` group (if present)
        - ``/phasor/<name>`` group (if present)
        - the channel's planes in the z-series (if present; the z-series is
          removed when this was its only channel)
        - ``name`` entry in ``metadata.channel_names`` (if present)
        - ``flim_cal_phase_<name>`` / ``flim_cal_mod_<name>`` attrs on
          ``/metadata``, plus every per-harmonic variant
          ``flim_cal_{phase,mod}_<name>_h<n>`` (if present)

        Returns ``True`` if anything was actually removed on disk,
        ``False`` if the channel was already absent everywhere
        (mirrors :meth:`delete_item`'s contract). Callers that need a
        "must have existed" semantic can branch on the return value.
        """
        deleted_any = False
        with h5py.File(self.path, "a") as f:
            for prefix in ("decay", "phasor"):
                p = f"{prefix}/{name}"
                if p in f:
                    del f[p]
                    deleted_any = True
            if self._drop_zseries_channel(f, name):
                deleted_any = True
            if "metadata" in f:
                attrs = f["metadata"].attrs
                names = list(attrs.get("channel_names", []))
                if name in names:
                    names.remove(name)
                    attrs["channel_names"] = names
                    deleted_any = True
                # Per-channel FLIM calibration attrs are removed regardless
                # of whether they contributed to deleted_any — they're
                # bookkeeping that should follow the channel out. Both the
                # legacy suffix-less key and every per-harmonic variant
                # (flim_cal_phase_<name>_h<n>) are swept.
                for key_prefix in ("flim_cal_phase_", "flim_cal_mod_"):
                    base = f"{key_prefix}{name}"
                    for key in list(attrs.keys()):
                        if key == base or _cal_harmonic_suffix(key, base):
                            del attrs[key]
        return deleted_any

    def rename_channel(self, old_name: str, new_name: str) -> None:
        """Rename a channel across all per-channel paths and metadata attrs.

        Moves ``/decay/<old>`` and ``/phasor/<old>`` groups, updates the
        ``channel_names`` list and the z-series channel names, and renames
        per-channel FLIM calibration attrs (``flim_cal_phase_<name>``,
        ``flim_cal_mod_<name>``, and every per-harmonic variant
        ``flim_cal_{phase,mod}_<name>_h<n>``). Surfaces that don't exist are
        skipped, but at least one must — see below.

        ``old_name`` may also be a **placeholder** for an ``/intensity``
        slice that has no ``channel_names`` entry (``ch<N>``, as synthesized
        for display by :func:`~percell4.domain.io.layout.split_intensity_layers`).
        Renaming one promotes it: ``channel_names`` is padded to length N
        with placeholders, slot N takes ``new_name``, and ``n_channels`` is
        brought in line with the array. This is the rename counterpart of the
        ``ch<N>``-aware delete path in the Data tab, and it converges the
        very metadata/array mismatch that produced the placeholder.

        Raises ``ValueError`` if ``old_name`` names no surface at all, or if
        ``new_name`` is already taken. A rename that matched nothing must not
        report success — callers show the user a "renamed" confirmation, and
        the change would silently vanish on the next reload.
        """
        if old_name == new_name:
            return
        with h5py.File(self.path, "a") as f:
            has_meta = "metadata" in f
            names = list(f["metadata"].attrs.get("channel_names", [])) if has_meta else []

            # ── Validate before touching anything. A rename spans four
            #    surfaces, and h5py gives no transaction — a collision
            #    discovered after the first move would leave the dataset
            #    half-renamed.
            slot: int | None = None  # channel_names index this rename writes
            if old_name in names:
                slot = names.index(old_name)
            elif has_meta:
                slot = self._unnamed_slice_index(f, names, old_name)
            if slot is not None and new_name in names:
                raise ValueError(f"Channel already exists: {new_name}")
            moves = [
                (f"{prefix}/{old_name}", f"{prefix}/{new_name}")
                for prefix in ("decay", "phasor")
                if f"{prefix}/{old_name}" in f
            ]
            for _, new_path in moves:
                if new_path in f:
                    raise ValueError(f"Target path already exists: {new_path}")
            cal_keys = self._cal_attr_renames(f, old_name, new_name) if has_meta else []
            zseries = f.get(ZSERIES_PATH)
            z_names = (
                list(_decode_names(zseries.attrs.get("channel_names", ())))
                if isinstance(zseries, h5py.Dataset)
                else []
            )
            if slot is None and not moves and not cal_keys and old_name not in z_names:
                raise ValueError(f"Channel not found: {old_name}")
            if old_name in z_names and new_name in z_names:
                raise ValueError(f"Channel already exists: {new_name}")

            # ── Apply.
            for old_path, new_path in moves:
                f.move(old_path, new_path)
            if slot is not None:
                attrs = f["metadata"].attrs
                # channel_names is positional, so an intervening unnamed slot
                # keeps its placeholder rather than letting later names shift
                # down into the wrong /intensity slice.
                while len(names) <= slot:
                    names.append(placeholder_channel_name(len(names)))
                names[slot] = new_name
                attrs["channel_names"] = names
                attrs["n_channels"] = len(names)
            for old_key, new_key in cal_keys:
                attrs = f["metadata"].attrs
                attrs[new_key] = attrs[old_key]
                del attrs[old_key]
            if old_name in z_names:
                z_names[z_names.index(old_name)] = new_name
                zseries.attrs["channel_names"] = z_names

    @staticmethod
    def _unnamed_slice_index(
        f: h5py.File, names: list[str], old_name: str
    ) -> int | None:
        """Resolve a ``ch<N>`` placeholder to its ``/intensity`` slice index.

        ``None`` when ``old_name`` isn't a placeholder, or names a slot that
        no slice backs. Only meaningful once ``old_name`` is known to be
        absent from ``names`` — a real imported channel can be called
        ``ch00`` (the numeric-token form), and that one is a rename, not a
        promotion.
        """
        idx = placeholder_channel_index(old_name)
        intensity = _first_intensity_array(f)
        if idx is None or idx < len(names) or intensity is None:
            return None
        shape = tuple(int(x) for x in intensity.shape)
        n_timepoints = int(f["metadata"].attrs.get("n_timepoints", 1) or 1)
        if idx >= intensity_channel_count(shape, n_timepoints):
            return None
        return idx

    @staticmethod
    def _cal_attr_renames(
        f: h5py.File, old_name: str, new_name: str
    ) -> list[tuple[str, str]]:
        """``[(old_key, new_key)]`` for this channel's FLIM calibration attrs.

        Covers the legacy suffix-less ``flim_cal_{phase,mod}_<name>`` and
        every per-harmonic variant ``..._<name>_h<n>``.
        """
        attrs = f["metadata"].attrs
        renames: list[tuple[str, str]] = []
        for key_prefix in ("flim_cal_phase_", "flim_cal_mod_"):
            old_base = f"{key_prefix}{old_name}"
            new_base = f"{key_prefix}{new_name}"
            for key in list(attrs.keys()):
                if key == old_base:
                    renames.append((key, new_base))
                else:
                    suffix = _cal_harmonic_suffix(key, old_base)
                    if suffix is not None:
                        renames.append((key, new_base + suffix))
        return renames

    @staticmethod
    def create_atomic(
        path: str | Path,
        build_fn,
    ) -> None:
        """Create an .h5 file atomically via write-to-temp-then-rename.

        Use for import operations where crash safety matters::

            def build(h5_file):
                h5_file.create_dataset("intensity", data=image)

            DatasetStore.create_atomic("output.h5", build)
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            suffix=".h5.tmp", dir=path.parent
        )
        os.close(fd)
        try:
            with h5py.File(tmp_path, "w") as f:
                build_fn(f)
            os.replace(tmp_path, path)
        except BaseException:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

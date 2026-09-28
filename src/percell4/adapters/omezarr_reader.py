"""Native OME-Zarr reader: implements :class:`percell4.ports.image_reader.ImageReader`.

Reads Zarr v2 stores with OME-NGFF 0.4 metadata in-process, with no Java.
Metadata parsing lives in :mod:`percell4.domain.io.omezarr`; this module does
the file access and chunk decoding.

* Probing reads only the small JSON and OME-XML metadata files. It never
  opens a chunk.
* Reading uses only the full-resolution level. A plane is assembled from the
  chunk tiles that cover it; partial edge chunks are stored padded and are
  cropped here. A missing chunk file reads as the array's fill value.
* Each read call parses the store's metadata again, so a scheme replayed in
  a new process needs nothing from an earlier probe.
* Chunks are opened by exact key. Directories are never listed, so Finder
  and AppleDouble sidecars inside a store are never seen.
* Planes are returned in native byte order, as the Bio-Formats reader does.

Chunks are decoded with ``imagecodecs`` (Blosc, Zstd, zlib, gzip, LZ4).
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from percell4.adapters.bioformats_host import project_planes
from percell4.domain.errors import OmeZarrReadError
from percell4.domain.io.infile import FileProbe, ImportSource
from percell4.domain.io.omezarr import ZarrArraySpec, ZarrSeries, parse_store, series_array
from percell4.ports.image_reader import (
    IsCancelled,
    OnFile,
    OnPlane,
    ProjectedPlane,
    RawPlane,
)

FORMAT_NAME = "OME-Zarr"


def _never_cancelled() -> bool:
    return False


class _Store:
    """Exact-key access to one store directory."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        return self.root.joinpath(*key.split("/"))

    def read_json(self, key: str) -> dict[str, Any] | None:
        try:
            value = json.loads(self._path(key).read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def read_text(self, key: str) -> str | None:
        try:
            return self._path(key).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def read_chunk(self, key: str) -> bytes | None:
        """Chunk bytes, or None when the chunk file does not exist."""
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise OmeZarrReadError(f"{self.root.name}: cannot read chunk {key} ({exc})") from exc


def store_fingerprint(root: str | Path) -> tuple[int, int]:
    """``(size, mtime_ns)`` that identifies a store's full-resolution arrays.

    Taken from the level-0 ``.zarray`` headers of every series: the summed
    sizes and the newest mtime. Files that Finder or exFAT drives add to the
    store (``.DS_Store``, ``._*``) do not change it. A store whose metadata
    does not parse falls back to the root directory's stat.
    """
    store = _Store(Path(root))
    return _fingerprint(store, parse_store(store.read_json, store.read_text).series)


def _fingerprint(store: _Store, series: Sequence[ZarrSeries]) -> tuple[int, int]:
    """:func:`store_fingerprint` for a store whose series are already parsed."""
    stats = []
    for s in series:
        try:
            stats.append(store._path(f"{s.array.path}/.zarray").stat())
        except OSError:
            stats = []
            break
    if not stats:
        st = store.root.stat()
        return int(st.st_size), int(st.st_mtime_ns)
    return sum(int(s.st_size) for s in stats), max(int(s.st_mtime_ns) for s in stats)


# ---------------------------------------------------------------------------
# Chunk decoding and plane assembly
# ---------------------------------------------------------------------------


def _decode(raw: bytes, spec: ZarrArraySpec) -> np.ndarray:
    """One chunk as an array of the full chunk shape, in the stored byte order."""
    import imagecodecs

    dtype = np.dtype(spec.dtype)
    nbytes = math.prod(spec.chunks) * dtype.itemsize
    codec = None if spec.compressor is None else spec.compressor.get("id")
    if codec is None:
        data = raw
    elif codec == "blosc":
        data = imagecodecs.blosc_decode(raw)
    elif codec == "zstd":
        data = imagecodecs.zstd_decode(raw, out=nbytes)
    elif codec == "zlib":
        data = imagecodecs.zlib_decode(raw, out=nbytes)
    elif codec == "gzip":
        data = imagecodecs.gzip_decode(raw, out=nbytes)
    elif codec == "lz4":
        # numcodecs' LZ4 prefixes each chunk with its uncompressed size.
        data = imagecodecs.lz4_decode(raw, header=True)
    else:  # parse_store rejects other codecs; kept for a clear message
        raise ValueError(f"compressor {codec!r} not supported")
    if len(data) != nbytes:
        raise ValueError(f"chunk decodes to {len(data)} bytes, expected {nbytes}")
    return np.frombuffer(data, dtype=dtype).reshape(spec.chunks, order=spec.order)


def _fill(spec: ZarrArraySpec) -> Any:
    value = spec.fill_value
    if value is None:
        return 0
    if isinstance(value, str):  # JSON has no NaN/Infinity literals
        return float(value)
    return value


class _PlaneAssembler:
    """Builds ``(Y, X)`` planes of one array from its chunks.

    Decoded chunks are kept while consecutive planes fall in the same chunk
    row along the non-YX axes, so a chunk spanning many z is decoded once per
    (t, c) stack, not once per plane.
    """

    def __init__(self, store: _Store, spec: ZarrArraySpec) -> None:
        self.store = store
        self.spec = spec
        self.dim_y = spec.axis("y")
        self.dim_x = spec.axis("x")
        self.height = spec.shape[self.dim_y]
        self.width = spec.shape[self.dim_x]
        self.dtype = np.dtype(spec.dtype)
        self._cache_key: tuple[int, ...] | None = None
        self._cache: dict[tuple[int, ...], np.ndarray | None] = {}

    def plane(self, position: dict[str, int]) -> np.ndarray:
        """The plane at ``position`` (``t``/``c``/``z`` indices), native byte order."""
        spec = self.spec
        fixed = []
        for dim, name in enumerate(spec.axes):
            if dim in (self.dim_y, self.dim_x):
                continue
            fixed.append((dim, position.get(name, 0)))
        row_key = tuple(i // spec.chunks[d] for d, i in fixed)
        if row_key != self._cache_key:
            self._cache_key = row_key
            self._cache = {}

        out = np.empty((self.height, self.width), dtype=self.dtype.newbyteorder("="))
        cy, cx = spec.chunks[self.dim_y], spec.chunks[self.dim_x]
        for gy in range(-(-self.height // cy)):
            for gx in range(-(-self.width // cx)):
                index = [0] * len(spec.shape)
                for (d, _), g in zip(fixed, row_key):
                    index[d] = g
                index[self.dim_y], index[self.dim_x] = gy, gx
                chunk = self._chunk(tuple(index))
                y0, x0 = gy * cy, gx * cx
                h, w = min(cy, self.height - y0), min(cx, self.width - x0)
                if chunk is None:
                    out[y0 : y0 + h, x0 : x0 + w] = _fill(spec)
                    continue
                select: list[Any] = [0] * len(spec.shape)
                for d, i in fixed:
                    select[d] = i % spec.chunks[d]
                select[self.dim_y] = slice(0, h)
                select[self.dim_x] = slice(0, w)
                out[y0 : y0 + h, x0 : x0 + w] = chunk[tuple(select)]
        return out

    def _chunk(self, index: tuple[int, ...]) -> np.ndarray | None:
        if index in self._cache:
            return self._cache[index]
        key = self.spec.chunk_key(index)
        raw = self.store.read_chunk(key)
        if raw is None:
            chunk = None
        else:
            try:
                chunk = _decode(raw, self.spec)
            except Exception as exc:  # noqa: BLE001 - any decode failure is a read error
                raise OmeZarrReadError(
                    f"{self.store.root.name}: chunk {key} is corrupt ({exc})"
                ) from exc
        self._cache[index] = chunk
        return chunk


def _open(source: ImportSource) -> tuple[_Store, _PlaneAssembler]:
    store = _Store(Path(source.path))
    try:
        spec = series_array(store.read_json, store.read_text, int(source.series_index))
    except ValueError as exc:
        raise OmeZarrReadError(f"{store.root.name}: {exc}") from exc
    return store, _PlaneAssembler(store, spec)


def read_plane(source: ImportSource, *, t: int, c: int, z: int) -> np.ndarray:
    """One raw plane at the store's own ``(t, c, z)``. For checks and tools."""
    _, assembler = _open(source)
    return assembler.plane({"t": t, "c": c, "z": z})


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


class OmeZarrReader:
    """In-process reader for local OME-Zarr stores. Holds no state between calls."""

    def probe(
        self,
        paths: Sequence[Path],
        on_file: OnFile | None = None,
        is_cancelled: IsCancelled | None = None,
    ) -> list[FileProbe]:
        """See :meth:`ImageReader.probe`. Reads metadata files only."""
        cancelled = is_cancelled or _never_cancelled
        out: list[FileProbe] = []
        for raw in paths:
            if cancelled():
                break
            record = self._probe_one(Path(raw))
            out.append(record)
            if on_file is not None:
                on_file(record)
        return out

    @staticmethod
    def _probe_one(path: Path) -> FileProbe:
        if not path.is_dir():
            return FileProbe(path=path, format_name=FORMAT_NAME, error="not a Zarr store directory")
        store = _Store(path)
        parsed = parse_store(store.read_json, store.read_text)
        if parsed.error:
            return FileProbe(path=path, format_name=FORMAT_NAME, error=parsed.error)
        try:
            size, mtime = _fingerprint(store, parsed.series)
        except OSError as exc:
            return FileProbe(path=path, format_name=FORMAT_NAME, error=f"unreadable: {exc}")
        return FileProbe(
            path=path,
            size_bytes=size,
            mtime_ns=mtime,
            format_name=FORMAT_NAME,
            series=tuple(s.probe for s in parsed.series),
            used_files=(path,),
        )

    def read_planes(
        self,
        source: ImportSource,
        on_plane: OnPlane | None = None,
        is_cancelled: IsCancelled | None = None,
    ) -> Iterator[RawPlane]:
        """See :meth:`ImageReader.read_planes`.

        The axis map is applied first: with Z and T swapped, the store's T
        index runs as the effective Z and its Z index as the effective T,
        matching the Bio-Formats reader.
        """
        cancelled = is_cancelled or _never_cancelled
        for t, c, stack in _stacks(source, on_plane, cancelled):
            for z, plane in enumerate(stack):
                yield t, c, z, plane

    def read_projected(
        self,
        source: ImportSource,
        z_method: str,
        on_plane: OnPlane | None = None,
        is_cancelled: IsCancelled | None = None,
    ) -> Iterator[ProjectedPlane]:
        """See :meth:`ImageReader.read_projected`. Same rules as the Bio-Formats host.

        Each stack is projected as its planes arrive, one plane at a time.
        """
        cancelled = is_cancelled or _never_cancelled
        for t, c, stack in _stacks(source, on_plane, cancelled):
            projected = project_planes(stack, z_method)
            if cancelled():
                return
            yield t, c, projected

    def close(self) -> None:
        """Nothing to release; kept for the port."""


def _stacks(
    source: ImportSource, on_plane: OnPlane | None, cancelled: IsCancelled
) -> Iterator[tuple[int, int, Iterator[np.ndarray]]]:
    """``(t, c, planes)`` per effective stack, in t then c order.

    ``planes`` is a lazy iterator over the stack's z-planes that ends early
    on cancel; the caller must consume it before asking for the next stack.
    """
    _, assembler = _open(source)
    spec = assembler.spec

    def size(name: str) -> int:
        dim = spec.axis(name)
        return 1 if dim is None else spec.shape[dim]

    swap = not source.axis_map.is_identity
    n_z, n_t = (size("t"), size("z")) if swap else (size("z"), size("t"))
    channels = tuple(int(c) for c in source.channel_indices) or tuple(range(size("c")))

    def planes(t: int, c: int) -> Iterator[np.ndarray]:
        for z in range(n_z):
            if cancelled():
                return
            where = {"t": z, "z": t} if swap else {"t": t, "z": z}
            yield assembler.plane({**where, "c": c})

    for t in range(n_t):
        for c in channels:
            if cancelled():
                return
            if on_plane is not None:
                on_plane(t, c)
            yield t, c, planes(t, c)

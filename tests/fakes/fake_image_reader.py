"""An in-memory :class:`percell4.ports.image_reader.ImageReader`.

Built from probe records and, optionally, pixel arrays. It never starts Java,
so the importer, dialog, workflow and CLI tests (U4-U7) can drive the in-file
path without a JVM::

    from tests.fakes.fake_image_reader import FakeImageReader, probe_for

    stack = np.random.default_rng(0).random((1, 3, 5, 32, 32))  # T, C, Z, Y, X
    probe = probe_for(path, stack, physical_z_um=0.125)
    reader = FakeImageReader([probe], arrays={(path, 0): stack})

Arrays are ``(T, C, Z, Y, X)`` in the file's own axis order; the source's
axis map is applied on read, as the real reader does. Projection is done
with whole-array numpy reductions, so the fake doubles as a reference for
the streaming implementation.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path

import numpy as np

from percell4.domain.io.infile import (
    AXIS_METADATA,
    Z_METHODS,
    FileProbe,
    ImportSource,
    SeriesProbe,
)
from percell4.ports.image_reader import IsCancelled, OnFile, OnPlane, ProjectedPlane

ERROR_NO_PROBE = "fake reader: no probe for this file"


def probe_for(
    path: str | Path,
    array: np.ndarray,
    *,
    series_index: int = 0,
    physical_x_um: float | None = 0.1,
    physical_z_um: float | None = None,
    channel_names: Sequence[str] = (),
    axis_source: str = AXIS_METADATA,
    format_name: str = "Fake",
) -> FileProbe:
    """A one-series :class:`FileProbe` whose sizes match ``array`` (T, C, Z, Y, X)."""
    path = Path(path)
    size_t, size_c, size_z, size_y, size_x = array.shape
    series = SeriesProbe(
        index=series_index,
        name=path.stem,
        size_t=size_t,
        size_c=size_c,
        size_z=size_z,
        size_y=size_y,
        size_x=size_x,
        dimension_order="XYZCT",
        pixel_type=str(array.dtype),
        physical_x_um=physical_x_um,
        physical_y_um=physical_x_um,
        physical_z_um=physical_z_um,
        channel_names=tuple(channel_names),
        axis_source=axis_source,
    )
    return FileProbe(
        path=path,
        size_bytes=int(array.nbytes),
        mtime_ns=0,
        format_name=format_name,
        series=(series,),
        used_files=(path,),
    )


def project(stack: np.ndarray, z_method: str) -> np.ndarray:
    """Project a ``(Z, Y, X)`` stack the way the port specifies, as float32."""
    if z_method == "mip":
        return stack.max(axis=0).astype(np.float32)
    if z_method == "mean":
        return stack.astype(np.float64).mean(axis=0).astype(np.float32)
    if z_method == "sum":
        return stack.astype(np.float64).sum(axis=0).astype(np.float32)
    raise ValueError(f"unknown z method {z_method!r}; expected one of {Z_METHODS}")


class FakeImageReader:
    """Serves given probes and arrays. Records every call for assertions."""

    def __init__(
        self,
        probes: Iterable[FileProbe] = (),
        arrays: Mapping[tuple[Path, int], np.ndarray] | None = None,
    ) -> None:
        self._probes = {Path(p.path): p for p in probes}
        self._arrays = {(Path(k[0]), k[1]): np.asarray(v) for k, v in (arrays or {}).items()}
        self.probe_calls: list[list[Path]] = []
        self.read_calls: list[tuple[ImportSource, str]] = []
        self.closed = False

    def probe(
        self,
        paths: Sequence[Path],
        on_file: OnFile | None = None,
        is_cancelled: IsCancelled | None = None,
    ) -> list[FileProbe]:
        paths = [Path(p) for p in paths]
        self.probe_calls.append(paths)
        out: list[FileProbe] = []
        for path in paths:
            if is_cancelled is not None and is_cancelled():
                break
            record = self._probes.get(path) or FileProbe(path=path, error=ERROR_NO_PROBE)
            out.append(record)
            if on_file is not None:
                on_file(record)
        return out

    def read_projected(
        self,
        source: ImportSource,
        z_method: str,
        on_plane: OnPlane | None = None,
        is_cancelled: IsCancelled | None = None,
    ) -> Iterator[ProjectedPlane]:
        if z_method not in Z_METHODS:
            raise ValueError(f"unknown z method {z_method!r}; expected one of {Z_METHODS}")
        self.read_calls.append((source, z_method))
        key = (Path(source.path), source.series_index)
        if key not in self._arrays:
            raise KeyError(f"fake reader has no array for {key}")
        array = self._arrays[key]  # T, C, Z, Y, X
        if not source.axis_map.is_identity:
            array = array.transpose(2, 1, 0, 3, 4)
        channels = source.channel_indices or tuple(range(array.shape[1]))
        return self._iterate(array, channels, z_method, on_plane, is_cancelled)

    @staticmethod
    def _iterate(array, channels, z_method, on_plane, is_cancelled):
        for t in range(array.shape[0]):
            for c in channels:
                if is_cancelled is not None and is_cancelled():
                    return
                plane = project(array[t, c], z_method)
                if on_plane is not None:
                    on_plane(t, c)
                yield t, c, plane

    def close(self) -> None:
        self.closed = True

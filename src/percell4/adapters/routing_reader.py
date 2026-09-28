"""Routes in-file reads by source: OME-Zarr stores natively, everything else to Bio-Formats.

Implements :class:`percell4.ports.image_reader.ImageReader`. The Bio-Formats
reader, and with it the JVM child, is built only when a non-Zarr path shows
up, so a selection of Zarr stores never needs Java.

Routing is by path alone (:func:`percell4.domain.io.infile.is_zarr_path`).
The scheme file records each source's path, so a replayed scheme routes the
same way without any reader field.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from itertools import groupby
from pathlib import Path

from percell4.adapters.omezarr_reader import OmeZarrReader
from percell4.domain.io.infile import FileProbe, ImportSource, is_zarr_path
from percell4.ports.image_reader import (
    ImageReader,
    IsCancelled,
    OnFile,
    OnPlane,
    ProjectedPlane,
    RawPlane,
)


def _bioformats_reader() -> ImageReader:
    """The real Bio-Formats reader. Seam for tests."""
    from percell4.adapters.bioformats_reader import BioformatsReader

    return BioformatsReader()


def _never_cancelled() -> bool:
    return False


class RoutingReader:
    """One reader for a mixed selection: Zarr stores and Bio-Formats files."""

    def __init__(
        self,
        bioformats_factory: Callable[[], ImageReader] | None = None,
        zarr_reader: ImageReader | None = None,
    ) -> None:
        self._bioformats_factory = bioformats_factory
        self._bioformats: ImageReader | None = None
        self._zarr = zarr_reader or OmeZarrReader()

    def _bioformats_reader(self) -> ImageReader:
        if self._bioformats is None:
            factory = self._bioformats_factory or _bioformats_reader
            self._bioformats = factory()
        return self._bioformats

    def _for(self, path: Path) -> ImageReader:
        return self._zarr if is_zarr_path(path) else self._bioformats_reader()

    def probe(
        self,
        paths: Sequence[Path],
        on_file: OnFile | None = None,
        is_cancelled: IsCancelled | None = None,
    ) -> list[FileProbe]:
        """See :meth:`ImageReader.probe`.

        Consecutive paths of one kind go to their reader in one call, so
        records and ``on_file`` calls keep the input order.
        """
        cancelled = is_cancelled or _never_cancelled
        out: list[FileProbe] = []
        for zarr, run in groupby((Path(p) for p in paths), key=is_zarr_path):
            if cancelled():
                break
            batch = list(run)
            reader = self._zarr if zarr else self._bioformats_reader()
            records = reader.probe(batch, on_file, cancelled)
            out.extend(records)
            if len(records) < len(batch):
                break
        return out

    def read_planes(
        self,
        source: ImportSource,
        on_plane: OnPlane | None = None,
        is_cancelled: IsCancelled | None = None,
    ) -> Iterator[RawPlane]:
        """See :meth:`ImageReader.read_planes`."""
        return self._for(Path(source.path)).read_planes(source, on_plane, is_cancelled)

    def read_projected(
        self,
        source: ImportSource,
        z_method: str,
        on_plane: OnPlane | None = None,
        is_cancelled: IsCancelled | None = None,
    ) -> Iterator[ProjectedPlane]:
        """See :meth:`ImageReader.read_projected`."""
        return self._for(Path(source.path)).read_projected(
            source, z_method, on_plane, is_cancelled
        )

    def close(self) -> None:
        """Close the readers that were built."""
        self._zarr.close()
        if self._bioformats is not None:
            self._bioformats.close()

"""Port: metadata probe and projected-plane reads of in-file microscopy data.

The in-file importer reads files whose channels, z-series, time points and
series live inside one file. Implementations: Bio-Formats running in a
separate process (``percell4.adapters.bioformats_reader``) and the native
OME-Zarr reader (``percell4.adapters.omezarr_reader``), joined by
``percell4.adapters.routing_reader``, which picks one per path. Tests use the
in-memory fake in ``tests/fakes/fake_image_reader.py``.

Both calls take an ``is_cancelled`` callback and a per-result callback. An
implementation checks ``is_cancelled`` between results, so a GUI caller can
pump events from the callback and stop the work from a Cancel button.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

import numpy as np
from numpy.typing import NDArray

from percell4.domain.io.infile import FileProbe, ImportSource

#: Called once per probed file, in input order, as soon as its record exists.
OnFile = Callable[[FileProbe], None]

#: Called once per projected plane with ``(t, c)`` before it is yielded.
OnPlane = Callable[[int, int], None]

#: Polled between results. Returning True stops the call.
IsCancelled = Callable[[], bool]

#: One projected plane: ``(t, c, plane)``. ``plane`` is float32 ``(H, W)``.
ProjectedPlane = tuple[int, int, NDArray[np.float32]]

#: One raw z-plane: ``(t, c, z, plane)``. ``plane`` is ``(H, W)`` in the
#: file's own pixel type.
RawPlane = tuple[int, int, int, NDArray]


@runtime_checkable
class ImageReader(Protocol):
    """Reads in-file multi-dimensional microscopy data.

    Implementations: :class:`percell4.adapters.bioformats_reader.BioformatsReader`,
    :class:`percell4.adapters.omezarr_reader.OmeZarrReader`, and
    :class:`percell4.adapters.routing_reader.RoutingReader` over both.
    """

    def probe(
        self,
        paths: Sequence[Path],
        on_file: OnFile | None = None,
        is_cancelled: IsCancelled | None = None,
    ) -> list[FileProbe]:
        """Read the metadata of each file. Never decodes pixel data.

        Returns one :class:`FileProbe` per path, in order. A file that cannot
        be read gets a record with ``error`` set; it does not raise. When
        ``is_cancelled`` returns True the call returns the records made so
        far.
        """
        ...

    def read_projected(
        self,
        source: ImportSource,
        z_method: str,
        on_plane: OnPlane | None = None,
        is_cancelled: IsCancelled | None = None,
    ) -> Iterator[ProjectedPlane]:
        """Stream the Z-projected planes of one series.

        The source's axis map is applied first: the projection runs over the
        effective Z, and ``t`` counts effective time points. ``c`` is the file
        channel index, taken from ``source.channel_indices`` (every channel
        when empty), in that order. ``z_method`` is one of
        :data:`percell4.domain.io.infile.Z_METHODS`. Max keeps the native
        values, sum and mean accumulate in float64, and every plane is
        returned as float32 ``(H, W)``.

        When ``is_cancelled`` returns True the iterator ends early.
        """
        ...

    def read_planes(
        self,
        source: ImportSource,
        on_plane: OnPlane | None = None,
        is_cancelled: IsCancelled | None = None,
    ) -> Iterator[RawPlane]:
        """Stream every raw plane of one series, z innermost.

        The axis map is applied first. Planes come in ``t``, then ``c``, then
        ``z`` order whatever the file's dimension order, so a caller can
        project each (t, c) stack as it arrives while holding one plane per
        projection. ``c`` follows ``source.channel_indices`` as in
        :meth:`read_projected`. ``on_plane(t, c)`` is called once per stack,
        before its first plane is yielded.

        When ``is_cancelled`` returns True the iterator ends early.
        """
        ...

    def close(self) -> None:
        """Release the reader and any process or runtime it holds."""
        ...

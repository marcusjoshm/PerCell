"""Lazy z-series arrays for the viewer (z-stack plan KTD7).

Each channel of the stored z-series becomes a dask array with one chunk per
plane. Reading a plane opens the file read-only, slices that one plane and
closes it again: no file handle stays open between reads, because an open
read handle blocks every store write in the same process (saving a mask,
adding a projection) while the z-series is shown.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from percell4.store import DatasetStore


def lazy_zseries_channel(path: str | Path, channel_index: int, view_bin: int = 1):
    """The z-series of one channel as a lazy ``(Z, H, W)`` or ``(T, Z, H, W)``
    float32 dask array, sum-binned by ``view_bin`` plane by plane."""
    import dask.array as da

    path = Path(path)
    shape = DatasetStore(path).zseries_shape()
    timed = len(shape) == 5
    n_t = shape[0] if timed else 1
    n_z = shape[-3]
    h, w = shape[-2] // view_bin, shape[-1] // view_bin

    def read_block(block_info=None):
        t, z = block_info[None]["chunk-location"][:2]
        plane = DatasetStore(path).read_zseries_plane(t, channel_index, z, view_bin=view_bin)
        return np.asarray(plane, dtype=np.float32)[np.newaxis, np.newaxis]

    stack = da.map_blocks(
        read_block,
        dtype=np.float32,
        chunks=((1,) * n_t, (1,) * n_z, (h,), (w,)),
        meta=np.empty((0, 0, 0, 0), dtype=np.float32),
    )
    return stack if timed else stack[0]


def zseries_contrast_limits(
    path: str | Path, channel: str, zseries_index: int, view_bin: int = 1
) -> tuple[float, float]:
    """Display limits for one channel's z-series, without scanning the stack.

    From the channel's max projection when the dataset stores one (the
    brightest value any plane holds); otherwise from three sampled planes:
    first, middle and last Z of the first timepoint.
    """
    path = Path(path)
    store = DatasetStore(path)
    names = list(store.metadata.get("channel_names") or [])
    if "max" in store.named_projections() and channel in names:
        max_store = DatasetStore(path, projection="max")
        timed = max_store.is_time_stacked("intensity")
        image = max_store.read_channel(
            "intensity", names.index(channel), view_bin=view_bin,
            timepoint=0 if timed else None,
        )
        low, high = float(np.nanmin(image)), float(np.nanmax(image))
    else:
        n_z = store.zseries_shape()[-3]
        planes = [
            store.read_zseries_plane(0, zseries_index, z, view_bin=view_bin)
            for z in sorted({0, n_z // 2, n_z - 1})
        ]
        low = float(min(np.nanmin(p) for p in planes))
        high = float(max(np.nanmax(p) for p in planes))
    return (low, high) if high > low else (low, low + 1.0)

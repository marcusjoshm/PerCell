"""Write small synthetic OME-Zarr stores into ``tmp_path`` for reader tests.

Chunks are encoded with ``imagecodecs``, the codec library the reader decodes
with. The checked-in stores under ``tests/fixtures/omezarr`` come from
zarr-python instead and guard against a mistake shared by both sides; this
helper covers what those cannot: the bioformats2raw layout, pyramid levels,
several series and big planes.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Sequence
from pathlib import Path

import imagecodecs
import numpy as np

_KINDS = {"t": "time", "c": "channel", "z": "space", "y": "space", "x": "space"}


def _encode(raw: bytes, codec: str, itemsize: int) -> bytes:
    if codec == "blosc":
        return imagecodecs.blosc_encode(raw, level=5, compressor="lz4", typesize=itemsize)
    if codec == "zstd":
        return imagecodecs.zstd_encode(raw)
    if codec == "zlib":
        return imagecodecs.zlib_encode(raw)
    if codec == "none":
        return raw
    raise ValueError(codec)


def _compressor(codec: str) -> dict | None:
    return {
        "blosc": {"id": "blosc", "cname": "lz4", "clevel": 5, "shuffle": 1, "blocksize": 0},
        "zstd": {"id": "zstd", "level": 1},
        "zlib": {"id": "zlib", "level": 1},
        "none": None,
    }[codec]


def write_array(
    folder: Path,
    data: np.ndarray,
    chunks: Sequence[int],
    *,
    codec: str = "blosc",
    separator: str = "/",
) -> None:
    """Write ``data`` as a C-order Zarr v2 array, edge chunks padded."""
    folder.mkdir(parents=True, exist_ok=True)
    header = {
        "zarr_format": 2,
        "shape": list(data.shape),
        "chunks": list(chunks),
        "dtype": data.dtype.str,
        "compressor": _compressor(codec),
        "fill_value": 0,
        "filters": None,
        "order": "C",
        "dimension_separator": separator,
    }
    (folder / ".zarray").write_text(json.dumps(header))
    grid = [range(-(-n // c)) for n, c in zip(data.shape, chunks)]
    for index in itertools.product(*grid):
        block = np.zeros(chunks, dtype=data.dtype)
        region = tuple(
            slice(i * c, min((i + 1) * c, n)) for i, c, n in zip(index, chunks, data.shape)
        )
        piece = data[region]
        block[tuple(slice(0, s) for s in piece.shape)] = piece
        key = folder / separator.join(str(i) for i in index)
        key.parent.mkdir(parents=True, exist_ok=True)
        key.write_bytes(_encode(block.tobytes(order="C"), codec, data.dtype.itemsize))


def write_store(
    root: Path,
    series: Sequence[np.ndarray],
    *,
    axes: str = "tczyx",
    chunks: Sequence[int] | None = None,
    codec: str = "blosc",
    separator: str = "/",
    levels: int = 2,
    scale: Sequence[float] | None = None,
    channel_names: Sequence[Sequence[str]] | None = None,
    bioformats2raw: bool = True,
) -> Path:
    """Write ``series`` (one array each, in ``axes`` order) as an OME-Zarr store.

    With ``bioformats2raw`` the store has numbered series groups and an
    ``OME/METADATA.ome.xml`` carrying the channel names; otherwise the single
    series sits at the root with ``omero`` channel labels. Level ``k`` halves
    Y and X ``k`` times by striding.
    """
    root.mkdir(parents=True)
    (root / ".zgroup").write_text(json.dumps({"zarr_format": 2}))
    scale = list(scale or [1.0] * len(axes))
    images = []
    for index, data in enumerate(series):
        group = root / str(index) if bioformats2raw else root
        group.mkdir(exist_ok=True)
        (group / ".zgroup").write_text(json.dumps({"zarr_format": 2}))
        datasets = []
        for level in range(levels):
            step = 2**level
            level_data = data[..., ::step, ::step]
            level_chunks = chunks or data.shape
            write_array(
                group / str(level), level_data, level_chunks, codec=codec, separator=separator
            )
            datasets.append(
                {
                    "path": str(level),
                    "coordinateTransformations": [
                        {"type": "scale", "scale": scale[:-2] + [s * step for s in scale[-2:]]}
                    ],
                }
            )
        attrs: dict = {
            "multiscales": [
                {
                    "version": "0.4",
                    "name": f"image {index}",
                    "axes": [
                        {
                            "name": a,
                            "type": _KINDS[a],
                            **({"unit": "micrometer"} if _KINDS[a] == "space" else {}),
                        }
                        for a in axes
                    ],
                    "datasets": datasets,
                }
            ]
        }
        names = list(channel_names[index]) if channel_names else []
        if not bioformats2raw and names:
            attrs["omero"] = {"channels": [{"label": n} for n in names]}
        (group / ".zattrs").write_text(json.dumps(attrs))
        channels = "".join(
            f'<Channel ID="Channel:{index}:{i}" Name="{n}"/>' for i, n in enumerate(names)
        )
        images.append(
            f'<Image ID="Image:{index}" Name="image {index}">'
            f'<Pixels ID="Pixels:{index}">{channels}</Pixels></Image>'
        )
    if bioformats2raw:
        (root / ".zattrs").write_text(json.dumps({"bioformats2raw.layout": 3}))
        (root / "OME").mkdir()
        (root / "OME" / ".zgroup").write_text(json.dumps({"zarr_format": 2}))
        (root / "OME" / ".zattrs").write_text(
            json.dumps({"series": [str(i) for i in range(len(series))]})
        )
        (root / "OME" / "METADATA.ome.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06">'
            + "".join(images)
            + "</OME>"
        )
    return root

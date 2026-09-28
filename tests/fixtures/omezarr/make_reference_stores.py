"""Write the reference OME-Zarr stores in this folder with zarr-python 2.x.

PerCell reads Zarr v2 with its own small reader. These stores come from the
reference implementation, so a layout mistake shared by PerCell's reader and
its test writer cannot pass unnoticed. They are tiny and checked in; rerun
this only to add a variant:

    uv run --no-project --with "zarr>=2.18,<3" --with "numcodecs<0.16" \
        python tests/fixtures/omezarr/make_reference_stores.py

Every store holds :func:`expected` for its dtype: axes c, z, y, x with shape
(2, 3, 5, 7) in chunks of (1, 2, 3, 4), so every axis but c ends in a partial
edge chunk.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np

SHAPE = (2, 3, 5, 7)
CHUNKS = (1, 2, 3, 4)

#: name -> (dtype, order, separator, compressor factory name, empty chunk dropped)
VARIANTS = {
    "c_slash_blosc_u2be": (">u2", "C", "/", "blosc", False),
    "f_dot_zstd_u2": ("<u2", "F", ".", "zstd", False),
    "c_dot_lz4_i2": ("<i2", "C", ".", "lz4", False),
    "c_slash_gzip_f4": ("<f4", "C", "/", "gzip", False),
    "c_dot_raw_u1_missing": ("|u1", "C", ".", "none", True),
}


def expected(dtype: str) -> np.ndarray:
    """The array every store holds, for ``dtype``. Chunk (1, 0, 0, 0) is zero."""
    data = (np.arange(np.prod(SHAPE)).reshape(SHAPE) * 37 % 251 + 1).astype(np.dtype(dtype))
    data[1, 0:2, 0:3, 0:4] = 0
    return data


def _compressor(name: str):
    import numcodecs

    return {
        "blosc": numcodecs.Blosc(cname="lz4", clevel=5, shuffle=numcodecs.Blosc.SHUFFLE),
        "zstd": numcodecs.Zstd(level=3),
        "lz4": numcodecs.LZ4(),
        "gzip": numcodecs.GZip(level=5),
        "none": None,
    }[name]


def _write(root: Path, dtype: str, order: str, sep: str, codec: str, drop_empty: bool) -> None:
    import zarr

    if root.exists():
        shutil.rmtree(root)
    store = zarr.DirectoryStore(str(root), dimension_separator=sep)
    group = zarr.group(store=store)
    array = group.create_dataset(
        "0",
        shape=SHAPE,
        chunks=CHUNKS,
        dtype=dtype,
        order=order,
        compressor=_compressor(codec),
        fill_value=0,
        write_empty_chunks=not drop_empty,
    )
    array[...] = expected(dtype)
    group.attrs["multiscales"] = [
        {
            "version": "0.4",
            "name": root.stem,
            "axes": [
                {"name": "c", "type": "channel"},
                {"name": "z", "type": "space", "unit": "micrometer"},
                {"name": "y", "type": "space", "unit": "micrometer"},
                {"name": "x", "type": "space", "unit": "micrometer"},
            ],
            "datasets": [
                {
                    "path": "0",
                    "coordinateTransformations": [
                        {"type": "scale", "scale": [1.0, 0.5, 0.25, 0.25]}
                    ],
                }
            ],
        }
    ]
    group.attrs["omero"] = {"channels": [{"label": "A"}, {"label": "B"}]}
    header = json.loads((root / "0" / ".zarray").read_text())
    assert header["dimension_separator"] == sep, header


def main() -> None:
    here = Path(__file__).parent
    for name, spec in VARIANTS.items():
        _write(here / f"{name}.zarr", *spec)


if __name__ == "__main__":
    main()

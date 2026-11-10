"""Z-projection names and the rule that picks which one a read uses.

A dataset stores each kept z-projection as its own named array (``max``,
``mean``, ``sum``). A dataset written before named projections carries one
unnamed ``/intensity`` array instead; it reads as a single projection, named
from its recorded ``z_projection`` method, or :data:`LEGACY_UNNAMED` when none
was recorded.

In-file import names its z methods ``mip``, ``mean`` and ``sum``; the product
names are ``max``, ``mean`` and ``sum``. :func:`projection_for_method` and
:func:`method_for_projection` are the only place the two meet.

Pure: no I/O, no numpy.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from percell4.domain.errors import ProjectionRequiredError

#: Product names of the projections a dataset can store, in display order.
PROJECTION_NAMES: tuple[str, ...] = ("max", "mean", "sum")

#: The projection used when none is chosen and the dataset holds it. The
#: Session and every headless command share this one constant.
PREFERRED_PROJECTION = "max"

#: The name a legacy ``/intensity`` reads under when no method was recorded.
LEGACY_UNNAMED = "projection"

_METHOD_TO_NAME = {"mip": "max", "mean": "mean", "sum": "sum"}
_NAME_TO_METHOD = {name: method for method, name in _METHOD_TO_NAME.items()}


def projection_for_method(method: str) -> str:
    """``mip`` -> ``max``; ``mean`` and ``sum`` keep their names."""
    try:
        return _METHOD_TO_NAME[method]
    except KeyError:
        raise ValueError(
            f"unknown z method {method!r}, expected one of {tuple(_METHOD_TO_NAME)}"
        ) from None


def method_for_projection(name: str) -> str:
    """``max`` -> ``mip``; ``mean`` and ``sum`` keep their names."""
    try:
        return _NAME_TO_METHOD[name]
    except KeyError:
        raise ValueError(
            f"unknown projection {name!r}, expected one of {PROJECTION_NAMES}"
        ) from None


def legacy_projection_name(z_projection: object) -> str:
    """The projection name a legacy ``/intensity`` reads under."""
    if isinstance(z_projection, bytes):
        z_projection = z_projection.decode(errors="replace")
    return _METHOD_TO_NAME.get(str(z_projection or ""), LEGACY_UNNAMED)


def ordered_projections(names: Iterable[str]) -> tuple[str, ...]:
    """``names`` in display order: known projections first, then the rest."""
    unique = set(names)
    known = [n for n in PROJECTION_NAMES if n in unique]
    return tuple(known + sorted(unique - set(known)))


def resolve_projection(stored: Sequence[str], requested: str | None) -> str:
    """Pick the projection an intensity read uses.

    ``requested`` wins when the dataset holds it. With nothing requested, the
    preferred projection is used if stored, else the sole projection.

    Raises :class:`ProjectionRequiredError` when the requested projection is
    not stored, when several are stored and none is preferred, or when none
    is stored at all.
    """
    stored = tuple(stored)
    listed = ", ".join(stored)
    if not stored:
        raise ProjectionRequiredError(
            "This dataset holds only a z-series; add a projection first.", stored
        )
    if requested is not None:
        if requested in stored:
            return requested
        raise ProjectionRequiredError(
            f"This dataset has no {requested!r} projection; it holds: {listed}.",
            stored,
        )
    if PREFERRED_PROJECTION in stored:
        return PREFERRED_PROJECTION
    if len(stored) == 1:
        return stored[0]
    raise ProjectionRequiredError(
        f"This dataset holds several projections ({listed}); choose one.", stored
    )


def pick_projection(names: Sequence[str]) -> str | None:
    """The projection a newly opened dataset reads (R18): the preferred one
    when stored, else the first stored one, else ``None``."""
    if PREFERRED_PROJECTION in names:
        return PREFERRED_PROJECTION
    return names[0] if names else None


def uncompressed_nbytes(shape: Iterable[int], itemsize: int) -> int:
    """Byte count of an array of ``shape``, in Python integers.

    ``numpy.prod`` overflows int32 on Windows for a large z-series, so every
    size shown to the user goes through this.
    """
    return math.prod(int(x) for x in shape) * int(itemsize)


#: The token that stands for the full z-series in a keep list
#: (``--keep max,zseries``) and in labels.
ZSERIES_TOKEN = "zseries"


@dataclass(frozen=True)
class StorageChoice:
    """What an import keeps from a z-stack: projections and/or the z-series.

    ``projections`` holds product names in display order. At least one
    projection or the z-series must be kept. A choice with no projection
    makes a view-only dataset until a projection is added.
    """

    projections: tuple[str, ...] = (PREFERRED_PROJECTION,)
    keep_zseries: bool = False

    def __post_init__(self) -> None:
        unknown = [n for n in self.projections if n not in PROJECTION_NAMES]
        if unknown:
            raise ValueError(
                f"unknown projection(s) {unknown}, expected some of {PROJECTION_NAMES}"
            )
        object.__setattr__(self, "projections", ordered_projections(self.projections))
        if not self.projections and not self.keep_zseries:
            raise ValueError("keep at least one projection or the z-series")

    @classmethod
    def from_z_method(cls, z_method: str) -> StorageChoice:
        """The choice an import made before storage choices existed: one
        projection by ``z_method``, no z-series."""
        return cls(projections=(projection_for_method(z_method),))

    @classmethod
    def parse(cls, text: str) -> StorageChoice:
        """Parse a keep list such as ``"max,mean,zseries"``."""
        tokens = [t.strip().lower() for t in text.split(",") if t.strip()]
        allowed = (*PROJECTION_NAMES, ZSERIES_TOKEN)
        unknown = [t for t in tokens if t not in allowed]
        if unknown:
            raise ValueError(f"unknown keep value(s) {unknown}, expected some of {allowed}")
        return cls(
            projections=tuple(t for t in tokens if t != ZSERIES_TOKEN),
            keep_zseries=ZSERIES_TOKEN in tokens,
        )

    @property
    def tokens(self) -> tuple[str, ...]:
        """The keep list, e.g. ``("max", "mean", "zseries")``."""
        return self.projections + ((ZSERIES_TOKEN,) if self.keep_zseries else ())

    @property
    def label(self) -> str:
        """Display text, e.g. ``"max, mean, z-series"``."""
        return ", ".join(
            "z-series" if t == ZSERIES_TOKEN else t for t in self.tokens
        )


def estimate_storage_bytes(
    choice: StorageChoice, n_t: int, n_c: int, n_z: int, height: int, width: int
) -> tuple[int, int]:
    """Uncompressed ``(z-series bytes, projection bytes)`` of one dataset.

    Both are float32. The z-series is ``T x C x Z x H x W``; each kept
    projection is ``T x C x H x W``.
    """
    plane = (max(1, n_t), max(1, n_c), int(height), int(width))
    zseries = uncompressed_nbytes((*plane, max(1, n_z)), 4) if choice.keep_zseries else 0
    projections = len(choice.projections) * uncompressed_nbytes(plane, 4)
    return zseries, projections


def format_bytes(n: int) -> str:
    """``1234567`` -> ``"1.2 MB"`` (decimal units, as file browsers show)."""
    value = float(n)
    for unit in ("bytes", "kB", "MB", "GB"):
        if value < 1000 or unit == "GB":
            break
        value /= 1000
    if unit == "bytes":
        return f"{int(value)} bytes"
    if unit == "GB" and value >= 1000:
        return f"{value / 1000:.1f} TB"
    return f"{value:.1f} {unit}"

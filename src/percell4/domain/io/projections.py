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


def uncompressed_nbytes(shape: Iterable[int], itemsize: int) -> int:
    """Byte count of an array of ``shape``, in Python integers.

    ``numpy.prod`` overflows int32 on Windows for a large z-series, so every
    size shown to the user goes through this.
    """
    return math.prod(int(x) for x in shape) * int(itemsize)

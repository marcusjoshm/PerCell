"""Tests for the pure projection naming and resolution rules."""

from __future__ import annotations

import pytest

from percell4.domain.errors import PercellError, ProjectionRequiredError
from percell4.domain.io.projections import (
    LEGACY_UNNAMED,
    PREFERRED_PROJECTION,
    PROJECTION_NAMES,
    legacy_projection_name,
    method_for_projection,
    ordered_projections,
    projection_for_method,
    resolve_projection,
    uncompressed_nbytes,
)


def test_names_and_preferred():
    assert PROJECTION_NAMES == ("max", "mean", "sum")
    assert PREFERRED_PROJECTION == "max"
    assert LEGACY_UNNAMED == "projection"


@pytest.mark.parametrize(
    ("method", "name"), [("mip", "max"), ("mean", "mean"), ("sum", "sum")]
)
def test_method_name_mapping_round_trips(method, name):
    assert projection_for_method(method) == name
    assert method_for_projection(name) == method


def test_unknown_method_raises():
    with pytest.raises(ValueError, match="unknown"):
        projection_for_method("median")
    with pytest.raises(ValueError, match="unknown"):
        method_for_projection("median")


@pytest.mark.parametrize(
    ("z_projection", "expected"),
    [("mip", "max"), ("mean", "mean"), ("sum", "sum"), (None, "projection"),
     ("", "projection"), ("weird", "projection"), (b"mip", "max")],
)
def test_legacy_projection_name(z_projection, expected):
    assert legacy_projection_name(z_projection) == expected


def test_ordered_projections_uses_canonical_order():
    assert ordered_projections(["sum", "max", "mean"]) == ("max", "mean", "sum")


# ── resolve_projection ─────────────────────────────────────────


def test_no_request_prefers_max():
    assert resolve_projection(("max", "mean"), None) == "max"


def test_request_picks_that_projection():
    assert resolve_projection(("max", "mean"), "mean") == "mean"


def test_request_for_absent_projection_names_the_stored_ones():
    with pytest.raises(ProjectionRequiredError, match="max, mean") as exc:
        resolve_projection(("max", "mean"), "sum")
    assert exc.value.stored == ("max", "mean")
    assert isinstance(exc.value, PercellError)


def test_several_without_max_is_ambiguous():
    with pytest.raises(ProjectionRequiredError, match="mean, sum"):
        resolve_projection(("mean", "sum"), None)


def test_sole_projection_is_used():
    assert resolve_projection(("mean",), None) == "mean"
    assert resolve_projection(("projection",), None) == "projection"


def test_no_projection_says_add_one_first():
    with pytest.raises(ProjectionRequiredError, match="add a projection first"):
        resolve_projection((), None)
    with pytest.raises(ProjectionRequiredError, match="add a projection first"):
        resolve_projection((), "max")


# ── size estimate ──────────────────────────────────────────────


def test_size_estimate_uses_python_integers():
    shape = (36, 3, 97, 1024, 1024)
    nbytes = uncompressed_nbytes(shape, 4)
    assert nbytes == 36 * 3 * 97 * 1024 * 1024 * 4 == 43_939_528_704
    assert type(nbytes) is int


def test_size_estimate_accepts_numpy_ints():
    import numpy as np

    shape = tuple(np.int32(x) for x in (36, 3, 97, 1024, 1024))
    assert uncompressed_nbytes(shape, np.dtype("float32").itemsize) == 43_939_528_704

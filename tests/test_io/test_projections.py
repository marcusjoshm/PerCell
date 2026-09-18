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


# ── storage choice ─────────────────────────────────────────────


def test_storage_choice_defaults_to_max_only():
    from percell4.domain.io.projections import StorageChoice

    choice = StorageChoice()
    assert choice.projections == ("max",)
    assert not choice.keep_zseries
    assert choice.label == "max"


def test_storage_choice_orders_and_labels():
    from percell4.domain.io.projections import StorageChoice

    choice = StorageChoice(projections=("sum", "max"), keep_zseries=True)
    assert choice.projections == ("max", "sum")
    assert choice.tokens == ("max", "sum", "zseries")
    assert choice.label == "max, sum, z-series"


def test_storage_choice_needs_something():
    from percell4.domain.io.projections import StorageChoice

    with pytest.raises(ValueError, match="at least one"):
        StorageChoice(projections=())
    assert StorageChoice(projections=(), keep_zseries=True).projections == ()


def test_storage_choice_from_z_method_and_parse():
    from percell4.domain.io.projections import StorageChoice

    assert StorageChoice.from_z_method("mip") == StorageChoice(("max",))
    assert StorageChoice.parse("zseries, MEAN") == StorageChoice(("mean",), True)
    with pytest.raises(ValueError, match="unknown keep"):
        StorageChoice.parse("max,median")


def test_storage_estimate_counts_zseries_and_each_projection():
    from percell4.domain.io.projections import StorageChoice, estimate_storage_bytes

    z, p = estimate_storage_bytes(StorageChoice(("max",), True), 36, 3, 97, 1024, 1024)
    assert z == 43_939_528_704
    assert p == 36 * 3 * 1024 * 1024 * 4
    z2, p2 = estimate_storage_bytes(StorageChoice(("max", "mean"), True), 36, 3, 97, 1024, 1024)
    assert z2 == z and p2 == 2 * p
    assert estimate_storage_bytes(StorageChoice(("max",)), 1, 1, 5, 10, 10) == (0, 400)


def test_format_bytes():
    from percell4.domain.io.projections import format_bytes

    assert format_bytes(512) == "512 bytes"
    assert format_bytes(1_234_567) == "1.2 MB"
    assert format_bytes(43_939_528_704) == "43.9 GB"
    assert format_bytes(2_500_000_000_000) == "2.5 TB"

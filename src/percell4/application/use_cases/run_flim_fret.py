"""Use case: orchestrate a FLIM-FRET analysis run across donor / DA pairs.

This module is Qt-free and importable without a running ``QApplication``.

Per pair:

1. ``cancel_check`` is honored before any compute — if True, the pair and
   every remaining pair is marked ``cancelled``.
2. ``flim_fret_discovery.validate_pair_layers`` re-validates the layer names
   against the live ``.h5`` so a layer deleted between dialog Accept and Start
   surfaces as ``status="missing_layer"`` rather than crashing the run.
3. Donor and DA stores are opened (lightweight; reads use ``view_bin=1`` and
   never look at ``Session``). Lifetime channel names are resolved to a
   channel index via ``metadata["channel_names"].index(name)``.
4. The effective mask is ``(mask > 0) & (phasor > 0)`` on each side.
5. In whole-field mode the orchestrator emits one row per pair using
   per-side arithmetic means over the effective masks.
6. In single-cell mode the donor reference is the arithmetic mean over per-cell
   donor means (cells with zero valid pixels excluded). The orchestrator then
   emits one row per DA cell (label 0 excluded as background), with that
   single donor reference repeated across all rows of that pair.
7. In per-particle mode each side's mask is split into 8-connected
   particles, and particles below the minimum size (px or µm², measured on
   the whole mask blob before phasor gating) are dropped. A particle's
   lifetime is the mean over its effective pixels. The donor reference is the
   mean of the donor particle means (each particle weighs once; particles
   with no effective pixels are excluded). The orchestrator emits one row per
   DA particle in raster order; with ``single_cell`` also on, each particle
   carries the cell covering most of its pixels (blank when background wins).
8. ``fret_efficiency = 1 - (da_mean / donor_mean)`` with one division guard:
   ``NaN`` when ``donor_mean == 0`` or ``isnan(donor_mean)``. Negative donor
   values and ``> 1`` or negative FRET values are reported as-is.

Per-pair exceptions never abort the batch — they are captured as
``status="error"`` and the run continues.

The orchestrator returns ``FlimFretReport`` with per-pair results. It does
NOT create the run folder or write the CSV; that is the dialog's
responsibility (so the run folder is shared with the ``RunLog`` and the
dialog can surface the result path in a summary message box).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, NamedTuple

import numpy as np
from skimage import measure

from percell4.application.use_cases.flim_fret_discovery import (
    validate_pair_layers,
)
from percell4.store import DatasetStore
from percell4.workflows.models import (
    FlimFretConfig,
    FlimFretPair,
    FlimFretPairResult,
    FlimFretParticleSizeUnit,
    FlimFretReport,
    FlimFretStatus,
)
from percell4.workflows.run_log import RunLog

logger = logging.getLogger(__name__)


ProgressCallback = Callable[[FlimFretPair, FlimFretPairResult], None]
CancelCheck = Callable[[], bool]


def run_flim_fret(
    config: FlimFretConfig,
    *,
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    run_log: RunLog | None = None,
) -> FlimFretReport:
    """Process every pair in ``config`` and return per-pair results.

    The orchestrator never raises for per-pair problems — they are
    captured as result statuses. Unexpected exceptions in this function
    itself (e.g., bad config) propagate.
    """
    results: list[FlimFretPairResult] = []
    if run_log is not None:
        run_log.log(
            phase="flim_fret",
            event="run_started",
            n_pairs=len(config.pairs),
            single_cell=config.single_cell,
            per_particle=config.per_particle,
        )

    cancelled = False
    for pair in config.pairs:
        if cancelled or (cancel_check is not None and cancel_check()):
            cancelled = True
            result = _empty_result(pair, FlimFretStatus.CANCELLED, reason=None)
            if run_log is not None:
                run_log.log(
                    phase="flim_fret",
                    dataset=pair.name,
                    event="pair_cancelled",
                )
            _emit(result, progress_callback)
            results.append(result)
            continue

        if run_log is not None:
            run_log.log(
                phase="flim_fret",
                dataset=pair.name,
                event="pair_started",
            )

        try:
            result = _compute_pair(pair, config)
        except Exception as exc:  # noqa: BLE001 — never abort the batch
            logger.exception("FLIM-FRET pair %r failed", pair.name)
            result = _empty_result(
                pair, FlimFretStatus.ERROR, reason=str(exc)
            )

        if run_log is not None:
            run_log.log(
                phase="flim_fret",
                dataset=pair.name,
                event="pair_done",
                status=result.status.value,
                reason=result.reason,
                n_rows=len(result.rows),
                n_pixels_donor=result.n_pixels_donor,
                n_cells_donor_reference=result.n_cells_donor_reference,
                n_da_cells_skipped=result.n_da_cells_skipped,
                n_particles_donor_reference=result.n_particles_donor_reference,
                n_da_particles_skipped=result.n_da_particles_skipped,
            )

        _emit(result, progress_callback)
        results.append(result)

    if run_log is not None:
        run_log.log(
            phase="flim_fret",
            event="run_finished",
            n_succeeded=sum(
                1 for r in results if r.status is FlimFretStatus.SUCCEEDED
            ),
            n_failed=sum(
                1
                for r in results
                if r.status
                in (
                    FlimFretStatus.ERROR,
                    FlimFretStatus.MISSING_LAYER,
                    FlimFretStatus.DATASET_OPEN_FAILED,
                    FlimFretStatus.DONOR_REFERENCE_EMPTY,
                )
            ),
            n_cancelled=sum(
                1 for r in results if r.status is FlimFretStatus.CANCELLED
            ),
        )

    return FlimFretReport(results=results)


# ── Per-pair compute ────────────────────────────────────────


def _compute_pair(
    pair: FlimFretPair, config: FlimFretConfig
) -> FlimFretPairResult:
    """Do the math for one pair. Returns a ``FlimFretPairResult``."""
    single_cell = config.single_cell
    missing = validate_pair_layers(pair, single_cell=single_cell)
    if missing:
        return _empty_result(
            pair, FlimFretStatus.MISSING_LAYER, reason="; ".join(missing)
        )

    donor_store = DatasetStore(pair.donor_h5)
    da_store = DatasetStore(pair.da_h5)

    if config.per_particle:
        donor_ps = _pixel_size_um(donor_store)
        da_ps = _pixel_size_um(da_store)
        # Fail before loading any arrays when a µm² threshold can't apply.
        if config.min_particle_size_unit == "um2":
            for side, h5_path, ps in (
                ("donor", pair.donor_h5, donor_ps), ("DA", pair.da_h5, da_ps)
            ):
                if ps is None:
                    return _empty_result(
                        pair,
                        FlimFretStatus.ERROR,
                        reason=(
                            f"minimum particle size is in µm² but the {side} "
                            f"dataset {h5_path.name} has no pixel size"
                        ),
                    )

    # Load arrays at view_bin=1. native_shape lock guarantees per-dataset
    # consistency; cross-dataset shapes may differ and that's fine.
    donor_mask = donor_store.read_mask(pair.donor_mask, view_bin=1)
    donor_phasor = donor_store.read_mask(pair.donor_phasor, view_bin=1)
    donor_lifetime = _read_lifetime_channel(donor_store, pair.donor_lifetime)
    donor_eff = (donor_mask > 0) & (donor_phasor > 0)
    _assert_shape("donor mask/phasor/lifetime", donor_mask, donor_phasor, donor_lifetime)

    da_mask = da_store.read_mask(pair.da_mask, view_bin=1)
    da_phasor = da_store.read_mask(pair.da_phasor, view_bin=1)
    da_lifetime = _read_lifetime_channel(da_store, pair.da_lifetime)
    da_eff = (da_mask > 0) & (da_phasor > 0)
    _assert_shape("DA mask/phasor/lifetime", da_mask, da_phasor, da_lifetime)

    if config.per_particle:
        da_labels = None
        if single_cell:
            assert pair.da_segmentation is not None
            da_labels = da_store.read_labels(pair.da_segmentation, view_bin=1)
            _assert_shape("DA labels vs mask", da_labels, da_mask)
        return _compute_per_particle(
            pair,
            config,
            donor=_Side(donor_mask, donor_eff, donor_lifetime, donor_ps),
            da=_Side(da_mask, da_eff, da_lifetime, da_ps),
            da_labels=da_labels,
        )

    if not single_cell:
        return _compute_whole_field(
            pair, donor_eff, donor_lifetime, da_eff, da_lifetime
        )

    # Single-cell branch.
    assert pair.donor_segmentation is not None
    assert pair.da_segmentation is not None
    donor_labels = donor_store.read_labels(pair.donor_segmentation, view_bin=1)
    da_labels = da_store.read_labels(pair.da_segmentation, view_bin=1)
    _assert_shape("donor labels vs mask", donor_labels, donor_mask)
    _assert_shape("DA labels vs mask", da_labels, da_mask)

    return _compute_single_cell(
        pair,
        donor_eff=donor_eff,
        donor_lifetime=donor_lifetime,
        donor_labels=donor_labels,
        da_eff=da_eff,
        da_lifetime=da_lifetime,
        da_labels=da_labels,
    )


def _compute_whole_field(
    pair: FlimFretPair,
    donor_eff: np.ndarray,
    donor_lifetime: np.ndarray,
    da_eff: np.ndarray,
    da_lifetime: np.ndarray,
) -> FlimFretPairResult:
    n_pixels_donor = int(donor_eff.sum())
    n_pixels_da = int(da_eff.sum())
    donor_mean = (
        float(np.mean(donor_lifetime[donor_eff])) if n_pixels_donor else float("nan")
    )
    da_mean = (
        float(np.mean(da_lifetime[da_eff])) if n_pixels_da else float("nan")
    )
    fret = _fret(donor_mean, da_mean)

    row = _row(
        pair,
        cell_id="",
        donor_mean=donor_mean,
        da_mean=da_mean,
        fret_efficiency=fret,
        n_pixels_donor=n_pixels_donor,
        n_pixels_da=n_pixels_da,
        n_cells_donor_reference="",
        n_da_cells_skipped="",
    )
    return FlimFretPairResult(
        pair=pair,
        status=FlimFretStatus.SUCCEEDED,
        reason=None,
        rows=[row],
        n_pixels_donor=n_pixels_donor,
        n_cells_donor_reference=0,
        n_da_cells_skipped=0,
    )


def _compute_single_cell(
    pair: FlimFretPair,
    *,
    donor_eff: np.ndarray,
    donor_lifetime: np.ndarray,
    donor_labels: np.ndarray,
    da_eff: np.ndarray,
    da_lifetime: np.ndarray,
    da_labels: np.ndarray,
) -> FlimFretPairResult:
    # Pool donor cell means into one reference.
    donor_cell_ids = _cell_ids(donor_labels)
    donor_cell_means: list[float] = []
    total_donor_pixels = 0
    for cid in donor_cell_ids:
        cell_mask = donor_eff & (donor_labels == cid)
        n = int(cell_mask.sum())
        if n == 0:
            continue
        donor_cell_means.append(float(np.mean(donor_lifetime[cell_mask])))
        total_donor_pixels += n

    n_cells_donor_reference = len(donor_cell_means)
    donor_ref = (
        float(np.mean(donor_cell_means))
        if donor_cell_means
        else float("nan")
    )

    # Iterate DA cells. Ascending integer label order is the canonical
    # sort and the plan's stated rule.
    da_cell_ids = _cell_ids(da_labels)
    rows: list[dict[str, Any]] = []
    n_skipped = 0
    for cid in da_cell_ids:
        cell_mask = da_eff & (da_labels == cid)
        n = int(cell_mask.sum())
        if n == 0:
            da_mean = float("nan")
            n_skipped += 1
        else:
            da_mean = float(np.mean(da_lifetime[cell_mask]))
        fret = _fret(donor_ref, da_mean)
        rows.append(
            _row(
                pair,
                cell_id=int(cid),
                donor_mean=donor_ref,
                da_mean=da_mean,
                fret_efficiency=fret,
                n_pixels_donor=total_donor_pixels,
                n_pixels_da=n,
                n_cells_donor_reference=n_cells_donor_reference,
                # Filled with the final count below.
                n_da_cells_skipped=None,
            )
        )

    # Stamp the per-pair skipped count into every row.
    for row in rows:
        row["n_da_cells_skipped"] = n_skipped

    status = (
        FlimFretStatus.DONOR_REFERENCE_EMPTY
        if n_cells_donor_reference == 0
        else FlimFretStatus.SUCCEEDED
    )
    reason = (
        "donor reference pool was empty (no donor cells with valid pixels)"
        if status is FlimFretStatus.DONOR_REFERENCE_EMPTY
        else None
    )
    return FlimFretPairResult(
        pair=pair,
        status=status,
        reason=reason,
        rows=rows,
        n_pixels_donor=total_donor_pixels,
        n_cells_donor_reference=n_cells_donor_reference,
        n_da_cells_skipped=n_skipped,
    )


class _Side(NamedTuple):
    """One side's arrays for per-particle compute."""

    mask: np.ndarray
    eff: np.ndarray  # mask & phasor: the pixels a lifetime mean may use
    lifetime: np.ndarray
    pixel_size_um: float | None


def _compute_per_particle(
    pair: FlimFretPair,
    config: FlimFretConfig,
    *,
    donor: _Side,
    da: _Side,
    da_labels: np.ndarray | None,
) -> FlimFretPairResult:
    """One row per DA particle against the mean of donor particle means.

    A µm² threshold requires both pixel sizes; ``_compute_pair`` has already
    rejected the pair otherwise.
    """
    size, unit = config.min_particle_size, config.min_particle_size_unit

    # Donor reference: mean of the donor particle means.
    d_labeled, d_ids, _ = _label_particles(
        donor.mask, size, unit, donor.pixel_size_um
    )
    d_counts, d_sums = _particle_sums(d_labeled, donor.eff, donor.lifetime)
    d_means = [
        d_sums[pid] / d_counts[pid] for pid in d_ids if d_counts[pid] > 0
    ]
    n_ref = len(d_means)
    n_pixels_donor = int(sum(int(d_counts[pid]) for pid in d_ids))
    donor_ref = float(np.mean(d_means)) if d_means else float("nan")

    a_labeled, a_ids, a_areas = _label_particles(
        da.mask, size, unit, da.pixel_size_um
    )
    a_counts, a_sums = _particle_sums(a_labeled, da.eff, da.lifetime)
    cells = (
        _majority_cells(a_labeled, da_labels, a_ids)
        if da_labels is not None
        else {}
    )
    n_skipped = sum(1 for pid in a_ids if a_counts[pid] == 0)
    ps = da.pixel_size_um

    rows: list[dict[str, Any]] = []
    for particle_id, pid in enumerate(a_ids, start=1):
        n = int(a_counts[pid])
        da_mean = float(a_sums[pid] / n) if n else float("nan")
        area_px = int(a_areas[pid])
        rows.append({
            "pair_name": pair.name,
            "donor_dataset": pair.donor_h5.name,
            "da_dataset": pair.da_h5.name,
            "cell_id": cells.get(pid, ""),
            "particle_id": particle_id,
            "area_px": area_px,
            "area_um2": area_px * ps**2 if ps is not None else "",
            "donor_mean_lifetime": donor_ref,
            "da_mean_lifetime": da_mean,
            "fret_efficiency": _fret(donor_ref, da_mean),
            "n_pixels_donor": n_pixels_donor,
            "n_pixels_da": n,
            "n_particles_donor_reference": n_ref,
            "n_da_particles_skipped": n_skipped,
        })

    status = (
        FlimFretStatus.DONOR_REFERENCE_EMPTY
        if n_ref == 0
        else FlimFretStatus.SUCCEEDED
    )
    reason = (
        "donor reference pool was empty (no donor particles with valid pixels)"
        if status is FlimFretStatus.DONOR_REFERENCE_EMPTY
        else None
    )
    return FlimFretPairResult(
        pair=pair,
        status=status,
        reason=reason,
        rows=rows,
        n_pixels_donor=n_pixels_donor,
        n_cells_donor_reference=0,
        n_da_cells_skipped=0,
        n_particles_donor_reference=n_ref,
        n_da_particles_skipped=n_skipped,
    )


def _label_particles(
    mask: np.ndarray,
    min_size: float,
    unit: FlimFretParticleSizeUnit,
    pixel_size_um: float | None,
) -> tuple[np.ndarray, list[int], np.ndarray]:
    """Label 8-connected mask particles and keep those at least ``min_size``.

    Size is the whole mask blob (before phasor gating), in px or µm². Returns
    ``(labeled, kept_ids, areas_px)``: ``kept_ids`` are ascending label ids,
    which ``skimage.measure.label`` assigns in raster order; ``areas_px`` is
    indexed by label id.
    """
    labeled = measure.label(mask > 0, connectivity=2)
    areas_px = np.bincount(labeled.ravel())
    sizes = areas_px.astype(np.float64)
    if unit == "um2":
        assert pixel_size_um is not None
        sizes = sizes * pixel_size_um**2
    # isclose absorbs float error when a µm² area lands on the threshold.
    keep = (sizes >= min_size) | np.isclose(sizes, min_size, rtol=1e-9, atol=0)
    kept_ids = [int(i) for i in np.nonzero(keep)[0] if i != 0]
    return labeled, kept_ids, areas_px


def _particle_sums(
    labeled: np.ndarray, eff: np.ndarray, lifetime: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Per-label count and lifetime sum over effective pixels."""
    n = int(labeled.max()) + 1
    sel = labeled[eff]
    counts = np.bincount(sel, minlength=n)
    sums = np.bincount(
        sel, weights=lifetime[eff].astype(np.float64), minlength=n
    )
    return counts, sums


def _majority_cells(
    labeled: np.ndarray, cell_labels: np.ndarray, ids: list[int]
) -> dict[int, int | str]:
    """Map each particle to the cell covering most of its pixels.

    Ties between cells go to the lowest cell id; a cell that ties with
    background wins; the particle is blank (``""``) only when background
    covers more of it than any single cell.
    """
    sel = labeled > 0
    cells = cell_labels[sel].astype(np.int64)
    # One int64 key per (particle, cell) pixel: a 1-D sort is much cheaper
    # than np.unique(axis=1) on large images, and keys still come out in
    # (particle, cell) ascending order.
    base = int(cells.max(initial=0)) + 1
    keys, counts = np.unique(
        labeled[sel].astype(np.int64) * base + cells, return_counts=True
    )
    pids, cids = np.divmod(keys, base)
    background: dict[int, int] = {}
    best: dict[int, tuple[int, int]] = {}
    for pid, cid, cnt in zip(pids, cids, counts):
        pid, cid, cnt = int(pid), int(cid), int(cnt)
        if cid == 0:
            background[pid] = cnt
        elif pid not in best or cnt > best[pid][1]:
            # Cells arrive in ascending id per particle, so ">" keeps the
            # lowest id on ties.
            best[pid] = (cid, cnt)
    out: dict[int, int | str] = {}
    for pid in ids:
        if pid in best and best[pid][1] >= background.get(pid, 0):
            out[pid] = best[pid][0]
        else:
            out[pid] = ""
    return out


# ── Small helpers ──────────────────────────────────────────


def _read_lifetime_channel(store: DatasetStore, name: str) -> np.ndarray:
    """Resolve a lifetime channel name to its /intensity slice.

    Channel index is ``metadata["channel_names"].index(name)``; a missing
    name raises ``ValueError`` which the orchestrator turns into a
    ``status="error"`` result (with `validate_pair_layers` having already
    caught the same problem earlier — this is the belt-and-braces path).
    """
    channel_names = store.metadata.get("channel_names", []) or []
    channel_idx = channel_names.index(name)
    return store.read_channel("intensity", channel_idx, view_bin=1)


def _pixel_size_um(store: DatasetStore) -> float | None:
    """The dataset's pixel size in µm, or ``None`` when absent or not positive."""
    raw = store.metadata.get("pixel_size_um")
    if not raw:
        return None
    value = float(raw)
    return value if value > 0 else None


def _cell_ids(labels: np.ndarray) -> list[int]:
    """Return unique label values, sorted ascending, excluding 0 (background)."""
    unique = np.unique(labels)
    return sorted(int(v) for v in unique if int(v) != 0)


def _assert_shape(label: str, *arrays: np.ndarray) -> None:
    shapes = {a.shape for a in arrays}
    if len(shapes) != 1:
        raise AssertionError(
            f"{label} shape mismatch: {[a.shape for a in arrays]}"
        )


def _fret(donor_mean: float, da_mean: float) -> float:
    """Compute ``1 - (da / donor)`` with the single division guard.

    The guard fires only on ``donor == 0`` or ``isnan(donor)``. Negative
    donor values and ``> 1`` or negative FRET values are reported as-is
    per the workflow's scope boundary "no clamping".
    """
    if donor_mean == 0 or _isnan(donor_mean):
        return float("nan")
    if _isnan(da_mean):
        return float("nan")
    return 1.0 - (da_mean / donor_mean)


def _isnan(x: float) -> bool:
    # numpy.float32 doesn't have `.is_integer`; cast to plain float for math.
    return x != x


def _row(
    pair: FlimFretPair,
    *,
    cell_id: int | str,
    donor_mean: float,
    da_mean: float,
    fret_efficiency: float,
    n_pixels_donor: int,
    n_pixels_da: int,
    n_cells_donor_reference: int | str,
    n_da_cells_skipped: int | None | str,
) -> dict[str, Any]:
    return {
        "pair_name": pair.name,
        "donor_dataset": pair.donor_h5.name,
        "da_dataset": pair.da_h5.name,
        "cell_id": cell_id,
        "donor_mean_lifetime": donor_mean,
        "da_mean_lifetime": da_mean,
        "fret_efficiency": fret_efficiency,
        "n_pixels_donor": n_pixels_donor,
        "n_pixels_da": n_pixels_da,
        "n_cells_donor_reference": n_cells_donor_reference,
        "n_da_cells_skipped": n_da_cells_skipped,
    }


def _empty_result(
    pair: FlimFretPair, status: FlimFretStatus, *, reason: str | None
) -> FlimFretPairResult:
    return FlimFretPairResult(
        pair=pair,
        status=status,
        reason=reason,
        rows=[],
        n_pixels_donor=0,
        n_cells_donor_reference=0,
        n_da_cells_skipped=0,
    )


def _emit(
    result: FlimFretPairResult, cb: ProgressCallback | None
) -> None:
    if cb is None:
        return
    try:
        cb(result.pair, result)
    except Exception:  # noqa: BLE001
        logger.exception("progress_callback raised; continuing")

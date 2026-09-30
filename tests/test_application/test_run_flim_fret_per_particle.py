"""Tests for the FLIM-FRET per-particle mode of ``run_flim_fret``.

Per-particle mode splits each side's mask into 8-connected particles, drops
particles below the minimum size (px or µm², measured on the whole mask blob
before phasor gating), and measures each particle's mean lifetime over its
phasor-valid pixels. The donor reference is the mean of the donor particle
means (each particle weighs once), and every DA particle gets
``E = 1 - tau_DA_particle / donor_reference``.

Fixtures are handcrafted ``.h5`` files with known per-pixel lifetimes so
rows can be asserted exactly.
"""

from __future__ import annotations

import math
from pathlib import Path

import h5py
import numpy as np
import pytest

from percell4.application.use_cases.run_flim_fret import run_flim_fret
from percell4.workflows.models import (
    FlimFretConfig,
    FlimFretPair,
    FlimFretStatus,
)

H = W = 8
LIFETIME = "ch0_unfiltered_lifetime"


def _write_h5(
    path: Path,
    *,
    lifetime: np.ndarray,
    mask: np.ndarray,
    phasor: np.ndarray,
    labels: np.ndarray | None = None,
    pixel_size_um: float | None = None,
) -> Path:
    with h5py.File(path, "w") as f:
        meta = f.create_group("metadata")
        meta.attrs["channel_names"] = [LIFETIME]
        if pixel_size_um is not None:
            meta.attrs["pixel_size_um"] = pixel_size_um
        f.create_dataset("intensity", data=lifetime[np.newaxis].astype(np.float32))
        mg = f.create_group("masks")
        mg.create_dataset("puncta_mask", data=mask.astype(np.uint8))
        mg.create_dataset("phasor_ch0_1_phasor", data=phasor.astype(np.uint8))
        if labels is not None:
            lg = f.create_group("labels")
            lg.create_dataset("cellpose_qc", data=labels.astype(np.int32))
    return path


def _donor_arrays() -> tuple[np.ndarray, np.ndarray]:
    """Donor: particle A (2x2 px, tau 4.0) and particle B (1 px, tau 2.0).

    Mean of particle means = 3.0; the pixel-pooled mean would be 3.6, so the
    two reference definitions are distinguishable.
    """
    mask = np.zeros((H, W), np.uint8)
    life = np.zeros((H, W), np.float32)
    mask[1:3, 1:3] = 1
    life[1:3, 1:3] = 4.0
    mask[6, 6] = 1
    life[6, 6] = 2.0
    return mask, life


def _da_arrays() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """DA particles in raster (label) order:

    1. P1: 2x2 at rows 0-1, cols 4-5, tau 1.5            -> E = 0.5
    2. P2: diagonal pair (4,1)+(5,2), tau 2.4 (8-conn)    -> E = 0.2
    3. P3: single pixel (7,7), phasor-invalid             -> NaN, skipped
    """
    mask = np.zeros((H, W), np.uint8)
    life = np.zeros((H, W), np.float32)
    phasor = np.ones((H, W), np.uint8)
    mask[0:2, 4:6] = 1
    life[0:2, 4:6] = 1.5
    mask[4, 1] = mask[5, 2] = 1
    life[4, 1] = life[5, 2] = 2.4
    mask[7, 7] = 1
    life[7, 7] = 9.0
    phasor[7, 7] = 0
    return mask, life, phasor


def _pair(
    tmp_path: Path,
    *,
    donor_mask: np.ndarray | None = None,
    donor_life: np.ndarray | None = None,
    donor_phasor: np.ndarray | None = None,
    da_mask: np.ndarray | None = None,
    da_life: np.ndarray | None = None,
    da_phasor: np.ndarray | None = None,
    da_labels: np.ndarray | None = None,
    pixel_size_um: float | None = None,
    donor_pixel_size_um: float | None = None,
    da_pixel_size_um: float | None = None,
) -> FlimFretPair:
    d_mask, d_life = _donor_arrays()
    a_mask, a_life, a_phasor = _da_arrays()
    donor_h5 = _write_h5(
        tmp_path / "donor.h5",
        lifetime=d_life if donor_life is None else donor_life,
        mask=d_mask if donor_mask is None else donor_mask,
        phasor=np.ones((H, W), np.uint8) if donor_phasor is None else donor_phasor,
        pixel_size_um=donor_pixel_size_um or pixel_size_um,
    )
    da_h5 = _write_h5(
        tmp_path / "da.h5",
        lifetime=a_life if da_life is None else da_life,
        mask=a_mask if da_mask is None else da_mask,
        phasor=a_phasor if da_phasor is None else da_phasor,
        labels=da_labels,
        pixel_size_um=da_pixel_size_um or pixel_size_um,
    )
    return FlimFretPair(
        name="pair_1",
        donor_h5=donor_h5,
        da_h5=da_h5,
        donor_mask="puncta_mask",
        donor_phasor="phasor_ch0_1_phasor",
        donor_lifetime=LIFETIME,
        da_mask="puncta_mask",
        da_phasor="phasor_ch0_1_phasor",
        da_lifetime=LIFETIME,
        da_segmentation="cellpose_qc" if da_labels is not None else None,
    )


def _run(tmp_path: Path, pair: FlimFretPair, **config_kwargs):
    config = FlimFretConfig(
        pairs=[pair],
        single_cell=config_kwargs.pop("single_cell", False),
        output_parent=tmp_path,
        per_particle=True,
        **config_kwargs,
    )
    report = run_flim_fret(config)
    assert len(report.results) == 1
    return report.results[0]


# ── Happy path ──────────────────────────────────────────────


def test_one_row_per_da_particle_with_particle_mean_donor_reference(tmp_path):
    result = _run(tmp_path, _pair(tmp_path))
    assert result.status is FlimFretStatus.SUCCEEDED
    rows = result.rows
    assert [r["particle_id"] for r in rows] == [1, 2, 3]
    assert [r["area_px"] for r in rows] == [4, 2, 1]
    for r in rows:
        # Mean of donor particle means (4.0, 2.0), not the pixel mean 3.6.
        assert r["donor_mean_lifetime"] == pytest.approx(3.0)
        assert r["n_particles_donor_reference"] == 2
        assert r["n_pixels_donor"] == 5
        assert r["n_da_particles_skipped"] == 1
        assert r["cell_id"] == ""
        assert r["area_um2"] == ""
    assert rows[0]["da_mean_lifetime"] == pytest.approx(1.5)
    assert rows[0]["fret_efficiency"] == pytest.approx(0.5)
    assert rows[0]["n_pixels_da"] == 4
    assert rows[1]["da_mean_lifetime"] == pytest.approx(2.4)
    assert rows[1]["fret_efficiency"] == pytest.approx(0.2)
    # Phasor-invalid particle: NaN lifetime and FRET, counted as skipped.
    assert math.isnan(rows[2]["da_mean_lifetime"])
    assert math.isnan(rows[2]["fret_efficiency"])
    assert rows[2]["n_pixels_da"] == 0
    assert result.n_particles_donor_reference == 2
    assert result.n_da_particles_skipped == 1


def test_diagonal_pixels_form_one_particle(tmp_path):
    """8-connectivity: the diagonal DA pair is a single 2-px particle."""
    rows = _run(tmp_path, _pair(tmp_path)).rows
    assert rows[1]["area_px"] == 2


def test_multi_label_mask_treated_as_boolean(tmp_path):
    """Touching pixels with different mask values are still one particle."""
    mask, life = _donor_arrays()
    mask = mask.astype(np.uint8)
    mask[1, 1] = 7
    rows = _run(tmp_path, _pair(tmp_path, donor_mask=mask)).rows
    assert rows[0]["n_particles_donor_reference"] == 2


# ── Minimum size ────────────────────────────────────────────


def test_min_size_px_drops_small_particles_on_both_sides(tmp_path):
    result = _run(tmp_path, _pair(tmp_path), min_particle_size=2)
    rows = result.rows
    # DA 1-px particle dropped; survivors are numbered 1..N in raster order.
    assert [r["area_px"] for r in rows] == [4, 2]
    assert [r["particle_id"] for r in rows] == [1, 2]
    # Donor 1-px particle B dropped -> reference is particle A alone.
    assert rows[0]["donor_mean_lifetime"] == pytest.approx(4.0)
    assert rows[0]["n_particles_donor_reference"] == 1
    assert rows[0]["n_da_particles_skipped"] == 0


def test_min_size_measured_before_phasor_gating(tmp_path):
    """A 2-px particle with one phasor-invalid pixel still meets min 2 px;
    its lifetime comes from the one valid pixel."""
    _, a_life, a_phasor = _da_arrays()
    a_phasor[4, 1] = 0
    rows = _run(
        tmp_path, _pair(tmp_path, da_phasor=a_phasor), min_particle_size=2
    ).rows
    diag = rows[1]
    assert diag["area_px"] == 2
    assert diag["n_pixels_da"] == 1
    assert diag["da_mean_lifetime"] == pytest.approx(2.4)


def test_min_size_um2_uses_pixel_size(tmp_path):
    """pixel 0.5 um -> 0.25 um2 per px: min 0.5 um2 keeps particles >= 2 px."""
    result = _run(
        tmp_path,
        _pair(tmp_path, pixel_size_um=0.5),
        min_particle_size=0.5,
        min_particle_size_unit="um2",
    )
    rows = result.rows
    assert [r["area_px"] for r in rows] == [4, 2]
    assert rows[0]["area_um2"] == pytest.approx(1.0)
    assert rows[1]["area_um2"] == pytest.approx(0.5)
    assert rows[0]["donor_mean_lifetime"] == pytest.approx(4.0)


def test_area_um2_reported_with_px_threshold_when_pixel_size_known(tmp_path):
    rows = _run(tmp_path, _pair(tmp_path, pixel_size_um=0.5)).rows
    assert rows[0]["area_um2"] == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("donor_ps", "da_ps", "side"),
    [(None, None, "donor"), (None, 0.5, "donor"), (0.5, None, "DA")],
)
def test_um2_threshold_without_pixel_size_errors(tmp_path, donor_ps, da_ps, side):
    result = _run(
        tmp_path,
        _pair(tmp_path, donor_pixel_size_um=donor_ps, da_pixel_size_um=da_ps),
        min_particle_size=0.5,
        min_particle_size_unit="um2",
    )
    assert result.status is FlimFretStatus.ERROR
    assert f"the {side} dataset" in (result.reason or "")
    assert "pixel size" in (result.reason or "")
    assert result.rows == []


# ── Donor reference edge cases ──────────────────────────────


def test_donor_reference_empty_when_no_donor_particles(tmp_path):
    empty = np.zeros((H, W), np.uint8)
    result = _run(tmp_path, _pair(tmp_path, donor_mask=empty))
    assert result.status is FlimFretStatus.DONOR_REFERENCE_EMPTY
    assert result.n_particles_donor_reference == 0
    for r in result.rows:
        assert math.isnan(r["donor_mean_lifetime"])
        assert math.isnan(r["fret_efficiency"])


def test_phasor_invalid_donor_particle_is_left_out_of_the_reference(tmp_path):
    d_phasor = np.ones((H, W), np.uint8)
    d_phasor[6, 6] = 0  # donor particle B (tau 2.0) has no valid pixels
    rows = _run(tmp_path, _pair(tmp_path, donor_phasor=d_phasor)).rows
    assert rows[0]["donor_mean_lifetime"] == pytest.approx(4.0)
    assert rows[0]["n_particles_donor_reference"] == 1
    assert rows[0]["n_pixels_donor"] == 4


def test_all_donor_particles_phasor_invalid_is_reference_empty(tmp_path):
    result = _run(
        tmp_path, _pair(tmp_path, donor_phasor=np.zeros((H, W), np.uint8))
    )
    assert result.status is FlimFretStatus.DONOR_REFERENCE_EMPTY
    assert math.isnan(result.rows[0]["fret_efficiency"])


def test_no_da_particles_yields_no_rows(tmp_path):
    empty = np.zeros((H, W), np.uint8)
    result = _run(tmp_path, _pair(tmp_path, da_mask=empty))
    assert result.status is FlimFretStatus.SUCCEEDED
    assert result.rows == []


# ── Cell assignment (per-particle + single-cell) ────────────


def test_single_cell_assigns_majority_cell_and_blank_for_background(tmp_path):
    labels = np.zeros((H, W), np.int32)
    # P1 (rows 0-1, cols 4-5): 3 px in cell 2, 1 px in cell 5 -> cell 2.
    labels[0, 4:6] = 2
    labels[1, 4] = 2
    labels[1, 5] = 5
    # P2 diagonal (4,1)+(5,2): one px cell 3, one px background -> tie
    # between a cell and background goes to the cell.
    labels[4, 1] = 3
    # P3 (7,7): background only -> blank.
    result = _run(
        tmp_path, _pair(tmp_path, da_labels=labels), single_cell=True
    )
    assert result.status is FlimFretStatus.SUCCEEDED
    assert [r["cell_id"] for r in result.rows] == [2, 3, ""]


def test_single_cell_tie_between_cells_goes_to_lowest_id(tmp_path):
    labels = np.zeros((H, W), np.int32)
    labels[0:2, 4] = 5  # P1: 2 px in cell 5 ...
    labels[0:2, 5] = 2  # ... and 2 px in cell 2 -> cell 2
    rows = _run(
        tmp_path, _pair(tmp_path, da_labels=labels), single_cell=True
    ).rows
    assert rows[0]["cell_id"] == 2


def test_single_cell_background_majority_is_blank(tmp_path):
    labels = np.zeros((H, W), np.int32)
    labels[0, 4] = 2  # 1 of 4 px in a cell, 3 background
    rows = _run(
        tmp_path, _pair(tmp_path, da_labels=labels), single_cell=True
    ).rows
    assert rows[0]["cell_id"] == ""


def test_single_cell_does_not_change_the_donor_reference(tmp_path):
    labels = np.ones((H, W), np.int32)
    rows = _run(
        tmp_path, _pair(tmp_path, da_labels=labels), single_cell=True
    ).rows
    assert rows[0]["donor_mean_lifetime"] == pytest.approx(3.0)
    assert rows[0]["fret_efficiency"] == pytest.approx(0.5)


# ── Other modes unchanged ───────────────────────────────────


def test_whole_field_rows_do_not_gain_particle_columns(tmp_path):
    config = FlimFretConfig(
        pairs=[_pair(tmp_path)], single_cell=False, output_parent=tmp_path
    )
    row = run_flim_fret(config).results[0].rows[0]
    assert "particle_id" not in row
    assert "area_px" not in row


# ── Provenance ──────────────────────────────────────────────


def test_run_log_records_the_particle_size_threshold(tmp_path):
    import json

    from percell4.workflows.run_log import RunLog

    log_folder = tmp_path / "log"
    log_folder.mkdir()
    log = RunLog(log_folder)
    config = FlimFretConfig(
        pairs=[_pair(tmp_path)],
        single_cell=False,
        output_parent=tmp_path,
        per_particle=True,
        min_particle_size=2.5,
        min_particle_size_unit="px",
    )
    run_flim_fret(config, run_log=log)
    events = [json.loads(line) for line in log.path.read_text().splitlines() if line.strip()]
    started = next(e for e in events if e["event"] == "run_started")
    assert started["per_particle"] is True
    assert started["min_particle_size"] == 2.5
    assert started["min_particle_size_unit"] == "px"

"""Domain tests for the parameterised DTCWT wavelet filter.

Two guarantees: the LeeLab preset is pinned bit-for-bit by
``fixtures/wavelet_leelab_golden.npz`` (regenerated 2026-09-11 when the
preset adopted the paper's Anscombe pair; the pre-correction reference
script is ``WaveletParams.reference_script()``), and the paper preset
implements the BiShrink rule from Wang et al. 2021's supplement (eqs. 1-3).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import percell4._compat  # noqa: F401 — NumPy 2.0 shims for dtcwt
from percell4.domain.flim import wavelet_filter as wf
from percell4.domain.flim.wavelet_filter import WaveletParams, denoise_phasor

pytest.importorskip("dtcwt")

GOLDEN = Path(__file__).parent / "fixtures" / "wavelet_leelab_golden.npz"


@pytest.fixture(scope="module")
def golden():
    d = np.load(GOLDEN)
    return {k: d[k] for k in d.files}


# ── Presets / serialisation ───────────────────────────────────


def test_default_params_is_leelab_preset():
    assert WaveletParams() == WaveletParams.leelab()
    assert WaveletParams().method == "leelab"
    # The LeeLab preset uses the paper's Anscombe pair, not the script's.
    assert WaveletParams().anscombe_clamp == "after"
    assert WaveletParams().inverse_anscombe == "algebraic"


def test_reference_script_is_leelab_with_the_scripts_anscombe_pair():
    ref = WaveletParams.reference_script()
    assert ref.method == "custom"
    assert ref.anscombe_clamp == "before"
    assert ref.inverse_anscombe == "exact"
    assert ref.with_overrides(
        anscombe_clamp="after", inverse_anscombe="algebraic"
    ) == WaveletParams.leelab()


def test_paper_preset_matches_supplement():
    p = WaveletParams.paper()
    assert p.method == "paper"
    assert p.noise_bands == "finest_diagonal"   # suppl. eq. 1: ±45° finest bands
    assert p.sigma_exponent == 2.0              # eq. 3: √3·σ² in the numerator
    assert p.local_variance == "divide"         # eq. 3: ÷ √(σn² − σ²)₊
    assert p.regularize is False                # plain √(|Φ|²+|Φparent|²)
    assert p.window_radius == 3                 # 7×7 neighbourhood
    assert p.biort == "Legall"                  # table S1: LeGall 5,3
    assert p.inverse_anscombe == "algebraic"
    assert not p.same_computation(WaveletParams.leelab())


def test_round_trip_through_dict():
    for preset in (WaveletParams.leelab(), WaveletParams.paper()):
        assert WaveletParams.from_dict(preset.to_dict()) == preset


def test_from_dict_coerces_strings_and_derives_label():
    p = WaveletParams.from_dict({"regularize": "false", "window_radius": "3"})
    assert p.regularize is False and p.window_radius == 3
    assert p.method == "custom"
    full_paper = {k: str(v) for k, v in WaveletParams.paper().levers().items()}
    assert WaveletParams.from_dict(full_paper).method == "paper"


@pytest.mark.parametrize(
    "bad",
    [
        {"bogus": 1},
        {"noise_bands": "nope"},
        {"local_variance": "maybe"},
        {"biort": "haar"},
        {"sigma_exponent": -1},
        {"window_radius": -2},
        {"regularize": "sometimes"},
    ],
)
def test_from_dict_rejects_bad_values(bad):
    with pytest.raises(ValueError):
        WaveletParams.from_dict(bad)


def test_with_overrides_relabels():
    assert WaveletParams.paper().with_overrides(regularize=True).method == "custom"
    back = WaveletParams.paper().with_overrides(regularize=True).with_overrides(
        regularize=False
    )
    assert back.method == "paper"
    assert WaveletParams.preset("leelab").same_computation(WaveletParams())
    with pytest.raises(ValueError):
        WaveletParams.preset("nope")


# ── Transforms ────────────────────────────────────────────────


def test_anscombe_clamp_order_differs_for_negative_inputs_only():
    x = np.array([-1.0, -0.375, -0.2, 0.0, 4.0])
    before = wf.anscombe_transform(x, clamp="before")
    after = wf.anscombe_transform(x, clamp="after")
    np.testing.assert_allclose(before[3:], after[3:])
    # "before" flattens every negative to the x=0 value ...
    np.testing.assert_allclose(before[:3], 2 * np.sqrt(3 / 8))
    # ... "after" keeps the radicand down to zero.
    assert after[0] == 0.0 and after[1] == 0.0
    assert after[2] == pytest.approx(2 * np.sqrt(-0.2 + 3 / 8))


def test_inverse_anscombe_algebraic_inverts_forward_exactly():
    x = np.array([0.0, 1.0, 7.5, 120.0])
    y = wf.anscombe_transform(x, clamp="after")
    np.testing.assert_allclose(
        wf.reverse_anscombe_transform(y, method="algebraic"), x, atol=1e-12
    )


def test_inverse_anscombe_exact_is_unbiased_at_large_counts():
    y = wf.anscombe_transform(np.array([500.0]), clamp="before")
    exact = wf.reverse_anscombe_transform(y, method="exact")
    assert exact[0] == pytest.approx(500.0, rel=1e-3)


# ── Noise estimate ────────────────────────────────────────────


def _fake_pyramid(levels):
    """``levels``: list of (H, W, 6) complex arrays, finest first."""
    return SimpleNamespace(highpasses=[np.asarray(a, dtype=complex) for a in levels])


def test_noise_sigma_finest_diagonal_uses_only_bands_1_and_4_of_level_0():
    finest = np.full((4, 4, 6), 100.0)
    finest[:, :, 1] = 2.0
    finest[:, :, 4] = 4.0
    coarse = np.full((2, 2, 6), 1000.0)
    pyr = _fake_pyramid([finest, coarse])
    sigma = wf.estimate_noise_sigma(pyr, noise_bands="finest_diagonal")
    assert sigma == pytest.approx(np.median([2.0] * 16 + [4.0] * 16) / 0.6745)


def test_noise_sigma_all_is_mean_of_per_band_medians():
    finest = np.zeros((4, 4, 6))
    for b in range(6):
        finest[:, :, b] = b + 1
    coarse = np.full((2, 2, 6), 10.0)
    pyr = _fake_pyramid([finest, coarse])
    expected = np.mean([1, 2, 3, 4, 5, 6] + [10] * 6) / 0.6745
    assert wf.estimate_noise_sigma(pyr, noise_bands="all") == pytest.approx(expected)


def test_local_variance_window_radius_rule():
    pyr = _fake_pyramid([np.ones((8, 8, 6))])
    auto = wf.calculate_local_noise_variance(pyr, n_levels=2, window_radius=0)
    explicit = wf.calculate_local_noise_variance(pyr, n_levels=2, window_radius=2)
    np.testing.assert_allclose(auto[0][0], explicit[0][0])
    r1 = wf.calculate_local_noise_variance(pyr, n_levels=2, window_radius=1)
    # Interior of a 3×3 mean over ones is 1; the corner sees 4/9 (zero pad).
    assert r1[0][0][4, 4] == pytest.approx(1.0)
    assert r1[0][0][0, 0] == pytest.approx(4 / 9)


# ── Shrinkage rule ────────────────────────────────────────────


def test_shrink_factor_paper_matches_bishrink_formula():
    params = WaveletParams.paper()
    sigma = 0.5
    phi_sq = np.array([[4.0, 0.2], [9.0, 0.0]])
    sigma_n = np.array([[2.0, 0.5], [0.1, 3.0]])
    got = wf.shrink_factor(phi_sq, sigma_n, sigma, params)
    local_sig = np.sqrt(np.maximum(sigma_n - sigma**2, 0.0))
    with np.errstate(divide="ignore", invalid="ignore"):
        t = np.sqrt(3) * sigma**2 / local_sig
        expected = np.maximum(1 - t / np.sqrt(phi_sq), 0.0)
    # (1,0): local_sig = 0 → infinite threshold → fully shrunk.
    expected[1, 0] = 0.0
    # (1,1): zero bivariate magnitude → nothing to keep.
    expected[1, 1] = 0.0
    np.testing.assert_allclose(got, expected)
    assert got[0, 0] > 0.0


def test_shrink_factor_leelab_matches_reference_formula():
    params = WaveletParams.leelab()
    sigma = 0.3
    phi_sq = np.array([[4.0, 0.0], [0.5, 1.0]])
    sigma_n = np.array([[1.0, 1.0], [0.0, 1.0]])
    got = wf.shrink_factor(phi_sq, sigma_n, sigma, params)
    local_term = np.sqrt(3) * np.sqrt(sigma)
    expected = np.maximum(1 - local_term / np.sqrt(phi_sq + local_term), 0.0)
    expected[0, 1] = 0.0  # phi_sq == 0 → gated
    expected[1, 0] = 0.0  # sigma_n == 0 → gated
    np.testing.assert_allclose(got, expected)


def test_compute_phi_prime_uses_parent_and_skips_coarsest_by_default():
    rng = np.random.default_rng(1)
    finest = rng.normal(size=(4, 4, 6)) + 1j * rng.normal(size=(4, 4, 6))
    coarse = rng.normal(size=(2, 2, 6)) + 1j * rng.normal(size=(2, 2, 6))
    pyr = _fake_pyramid([finest, coarse])
    local = wf.calculate_local_noise_variance(pyr, n_levels=2, window_radius=1)

    out = wf.compute_phi_prime(pyr, 0.1, local, WaveletParams.leelab())
    assert len(out) == 1  # coarsest untouched
    out2 = wf.compute_phi_prime(
        pyr, 0.1, local, WaveletParams.leelab().with_overrides(shrink_coarsest=True)
    )
    assert len(out2) == 2
    assert not np.allclose(out2[1][0], coarse[:, :, 0])

    # Parent at (x//2, y//2): child (3, 3) shares parent (1, 1) with child (2, 2).
    phi = finest[:, :, 0]
    parent_sq = np.abs(coarse[:, :, 0]) ** 2
    phi_sq_sum = np.abs(phi) ** 2 + parent_sq[np.arange(4) // 2][:, np.arange(4) // 2]
    factor = wf.shrink_factor(phi_sq_sum, local[0][0], 0.1, WaveletParams.leelab())
    np.testing.assert_allclose(out[0][0], factor * phi)


# ── End to end ────────────────────────────────────────────────


def test_leelab_preset_reproduces_golden_output_exactly(golden):
    res = denoise_phasor(
        golden["g"], golden["s"], golden["intensity"],
        filter_level=int(golden["filter_level"]), omega=float(golden["omega"]),
    )
    np.testing.assert_array_equal(res["G"], golden["G"])
    np.testing.assert_array_equal(res["S"], golden["S"])
    np.testing.assert_array_equal(res["T"], golden["T"])
    assert res["params"] == WaveletParams.leelab().to_dict()


def test_explicit_leelab_params_equals_default(golden):
    kw = dict(filter_level=int(golden["filter_level"]), omega=float(golden["omega"]))
    a = denoise_phasor(golden["g"], golden["s"], golden["intensity"], **kw)
    b = denoise_phasor(
        golden["g"], golden["s"], golden["intensity"], params=WaveletParams.leelab(), **kw
    )
    np.testing.assert_array_equal(a["G"], b["G"])


def test_reference_script_differs_from_leelab_where_gi_is_negative(golden):
    """The script's Anscombe pair is what the LeeLab correction replaced:
    with negative G·I present, the two disagree; the script's output stays
    reachable through ``reference_script()``."""
    kw = dict(filter_level=int(golden["filter_level"]))
    g = golden["g"].copy()
    g[::5, ::7] = -0.3
    ref = denoise_phasor(
        g, golden["s"], golden["intensity"],
        params=WaveletParams.reference_script(), **kw,
    )
    lee = denoise_phasor(g, golden["s"], golden["intensity"], **kw)
    assert ref["params"]["method"] == "custom"
    assert np.isfinite(ref["G"]).all() and np.isfinite(lee["G"]).all()
    assert not np.array_equal(ref["G"], lee["G"])


def test_paper_preset_runs_and_differs_from_leelab(golden):
    kw = dict(filter_level=int(golden["filter_level"]), omega=float(golden["omega"]))
    res = denoise_phasor(
        golden["g"], golden["s"], golden["intensity"], params=WaveletParams.paper(), **kw
    )
    assert res["G"].shape == golden["G"].shape
    assert np.isfinite(res["G"]).all() and np.isfinite(res["S"]).all()
    assert res["G"].min() >= -0.1 and res["G"].max() <= 1.1
    assert res["params"]["method"] == "paper"
    assert not np.allclose(res["G"], golden["G"], atol=1e-3)
    # Still a denoiser: inside the bright disc (one true lifetime, so one
    # true G) the filtered map scatters less than the raw one.
    h, w = golden["g"].shape
    yy, xx = np.mgrid[0:h, 0:w]
    disc = (yy - 24) ** 2 + (xx - 20) ** 2 < 100
    assert np.std(res["G"][disc]) < np.std(golden["g"][disc])


@pytest.mark.parametrize(
    "base, lever, value",
    [
        ("leelab", "noise_bands", "finest_diagonal"),
        ("leelab", "sigma_exponent", 2.0),
        ("leelab", "local_variance", "divide"),
        ("leelab", "regularize", False),
        # The window only enters the threshold under "divide" (under "gate"
        # it merely asks whether any energy is nearby), so test it there.
        ("paper", "window_radius", 6),
        ("leelab", "biort", "near_sym_a"),
        ("leelab", "anscombe_clamp", "before"),
        ("leelab", "inverse_anscombe", "exact"),
        ("leelab", "shrink_coarsest", True),
    ],
)
def test_each_lever_changes_the_output(golden, base, lever, value):
    """Every lever is live: flipping it alone off a preset moves the
    result (so the GUI's one-at-a-time comparison is meaningful)."""
    kw = dict(filter_level=int(golden["filter_level"]))
    # Plant a few negative G·I values so the Anscombe clamp order has
    # something to act on (real data has them at noisy pixels).
    g = golden["g"].copy()
    g[::5, ::7] = -0.3
    preset = WaveletParams.preset(base)
    params = preset.with_overrides(**{lever: value})
    assert params.method == "custom"
    baseline = denoise_phasor(g, golden["s"], golden["intensity"], params=preset, **kw)
    res = denoise_phasor(g, golden["s"], golden["intensity"], params=params, **kw)
    assert np.isfinite(res["G"]).all()
    assert not np.array_equal(res["G"], baseline["G"]), lever

"""DTCWT-based wavelet filtering for FLIM phasor data.

One parameterised kernel serves two reference algorithms, selected (and
tweaked) through :class:`WaveletParams`:

* **LeeLab** (``WaveletParams.leelab()``, the default) — a faithful match
  to the reference ``ComplexWaveletFilter.py`` (LeeLabBCM):
  Anscombe → DTCWT (``biort='Legall'``, ``qshift='qshift_a'``) → inter-scale
  Wiener-like shrinkage → inverse DTCWT → inverse Anscombe, followed by the
  reference's phasor recovery (divide by filtered intensity, ``nan_to_num``,
  threshold by *unfiltered* intensity, clip to ``[-0.1, 1.1]``). The math is
  vectorized with numpy/scipy for ~100x speedup over the reference's nested
  Python loops, but produces output identical to the reference to float
  precision (verified against ``dataset_CWFlevels=9.npz``: G ~1e-8, S ~1e-5).
  Three details are load-bearing for that identity and must not drift from
  the reference: the Anscombe clamp order (``2√(max(data,0)+3/8)``), the
  *unclamped* inverse Anscombe (clamping is deferred to ``nan_to_num`` +
  clip in :func:`denoise_phasor`), and the ``Legall`` biorthogonal basis.
  ``tests/test_domain/fixtures/wavelet_leelab_golden.npz`` pins this path.

* **Paper** (``WaveletParams.paper()``) — a strict reading of Wang et al.,
  "Complex wavelet filter improves FLIM phasors for photon starved imaging
  experiments", Biomed. Opt. Express 12(6) 3463 (2021) and its supplement
  (``docs/reference/boe-12-6-3463.pdf``, ``docs/reference/5174492.pdf``):
  BiShrink after Sendur & Selesnick. The global noise σ is the MAD of the
  finest-level ±45° bands (supplement eq. 1), the local energy σn² is a
  7×7 mean of |Φ|² (eq. 2), and each coefficient is scaled by
  ``(1 − √3·σ² / (√(|Φ|²+|Φparent|²) · √(σn²−σ²)₊))₊`` (eq. 3).

The LeeLab script departs from the paper in several places (which bands
feed the MAD, the power of σ in the threshold, whether the local variance
divides the threshold or only gates it, a regulariser under the root, the
inverse-Anscombe flavour, the Anscombe clamp order, the window size). Each
departure is one field on :class:`WaveletParams`, so the GUI and the batch
CLI can flip them one at a time and see which piece moves a dataset.

Requires the optional ``dtcwt`` package: ``pip install dtcwt>=0.14.0``
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, fields, replace
from typing import Any

import numpy as np
from numpy.typing import NDArray
from scipy.ndimage import uniform_filter

logger = logging.getLogger(__name__)

# Maximum wavelet decomposition depth offered to users. The GUI spinbox and
# the batch CLIs (percell4-batch-phasor / batch_compute_phasor) share this one
# ceiling. It is NOT a DTCWT hard limit — dtcwt pads internally and accepts far
# more — but a generous, sane cap. Real microscopy images saturate their
# meaningful scales (~log2 of the image dimension) well below this, while it
# leaves ample headroom above the common default of 9. Verified crash-free and
# finite up to 35+ on images down to 64x64; raise further if a workflow needs it.
MAX_FILTER_LEVEL = 30

# dtcwt packs the six oriented subbands as [15°, 45°, 75°, 105°, 135°, 165°];
# indices 1 and 4 are the ±45° (HH, "horizontally and vertically high-pass")
# pair the paper's supplement uses for the global noise estimate.
DIAGONAL_BANDS = (1, 4)

# ── Parameters ─────────────────────────────────────────────────

NOISE_BANDS = ("all", "finest_diagonal")
LOCAL_VARIANCE = ("gate", "divide")
BIORT = ("Legall", "near_sym_a", "near_sym_b")
ANSCOMBE_CLAMP = ("before", "after")
INVERSE_ANSCOMBE = ("exact", "algebraic")
METHODS = ("leelab", "paper", "custom")


@dataclass(frozen=True)
class WaveletParams:
    """Every lever on which the LeeLab script and the paper differ.

    ``method`` is a label for the preset the values came from (``leelab``,
    ``paper``, or ``custom`` once any lever is moved); it does not affect
    the computation. Compare two configurations with
    :meth:`same_computation`, which ignores the label.

    Fields
    ------
    noise_bands
        Which coefficients feed the global MAD noise estimate. ``all`` is
        the LeeLab script (mean over every level and band of the median
        magnitude). ``finest_diagonal`` is the paper (one median over the
        finest-level ±45° bands).
    sigma_exponent
        Power of σ in the threshold numerator ``√3·σ^p``. The paper uses the
        variance (``2.0``); the LeeLab script takes the square root of the
        MAD-derived σ (``0.5``).
    local_variance
        ``divide``: the threshold is divided by the local signal std
        ``√(σn²−σ²)₊`` (BiShrink with local variance estimation, the paper).
        ``gate``: the local energy only gates which pixels are shrunk at
        all; the threshold is global (LeeLab).
    regularize
        Add the threshold under the root of the bivariate magnitude
        (LeeLab) instead of using the plain ``√(|Φ|²+|Φparent|²)`` (paper).
    window_radius
        Radius of the square window for the local energy. ``0`` means the
        LeeLab rule (radius = number of levels, or 3 above 10 levels); the
        paper preset uses 3 (a 7×7 window, Sendur & Selesnick's choice).
        Only matters with ``local_variance="divide"``: under ``gate`` the
        window merely decides whether *any* energy is nearby.
    biort
        First-level biorthogonal basis. Both presets use ``Legall`` (the
        paper's LeGall 5,3); ``near_sym_a``/``near_sym_b`` are offered for
        comparison. Higher levels always use the 10-tap ``qshift_a``.
    anscombe_clamp
        ``before``: ``2√(max(x,0)+3/8)`` (LeeLab). ``after``:
        ``2√(max(x+3/8,0))``, the paper's eq. 6 with a clamp only where the
        root would be undefined. They differ for every negative ``x``
        (negative Fourier coordinates ``G·I`` at noisy pixels): ``before``
        maps all of them to ``2√(3/8)``, ``after`` keeps them down to
        ``−3/8`` and maps anything below that to 0.
    inverse_anscombe
        ``exact``: the sixth-order unbiased rational inverse (LeeLab).
        ``algebraic``: ``(y/2)² − 3/8``, the literal inverse of eq. 6.
    shrink_coarsest
        Also shrink the coarsest highpass level (which has no parent, so
        only its own magnitude enters). Both presets leave it untouched,
        as Sendur & Selesnick's reference code does.
    """

    method: str = "leelab"
    noise_bands: str = "all"
    sigma_exponent: float = 0.5
    local_variance: str = "gate"
    regularize: bool = True
    window_radius: int = 0
    biort: str = "Legall"
    anscombe_clamp: str = "before"
    inverse_anscombe: str = "exact"
    shrink_coarsest: bool = False

    def __post_init__(self) -> None:
        _check_choice("method", self.method, METHODS)
        _check_choice("noise_bands", self.noise_bands, NOISE_BANDS)
        _check_choice("local_variance", self.local_variance, LOCAL_VARIANCE)
        _check_choice("biort", self.biort, BIORT)
        _check_choice("anscombe_clamp", self.anscombe_clamp, ANSCOMBE_CLAMP)
        _check_choice("inverse_anscombe", self.inverse_anscombe, INVERSE_ANSCOMBE)
        if not np.isfinite(self.sigma_exponent) or self.sigma_exponent < 0:
            raise ValueError(
                f"sigma_exponent must be a finite non-negative number, "
                f"got {self.sigma_exponent!r}"
            )
        if self.window_radius < 0:
            raise ValueError(
                f"window_radius must be >= 0 (0 = LeeLab rule), "
                f"got {self.window_radius!r}"
            )

    # ── Presets ──

    @classmethod
    def leelab(cls) -> WaveletParams:
        """The reference ``ComplexWaveletFilter.py`` behaviour (default)."""
        return cls()

    @classmethod
    def paper(cls) -> WaveletParams:
        """Strict Wang et al. 2021 / Sendur & Selesnick BiShrink."""
        return cls(
            method="paper",
            noise_bands="finest_diagonal",
            sigma_exponent=2.0,
            local_variance="divide",
            regularize=False,
            window_radius=3,
            biort="Legall",
            anscombe_clamp="after",
            inverse_anscombe="algebraic",
            shrink_coarsest=False,
        )

    @classmethod
    def preset(cls, name: str) -> WaveletParams:
        """Look up a preset by name (``leelab`` or ``paper``)."""
        if name == "leelab":
            return cls.leelab()
        if name == "paper":
            return cls.paper()
        raise ValueError(
            f"Unknown wavelet preset {name!r}; expected 'leelab' or 'paper'"
        )

    # ── Serialisation ──

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WaveletParams:
        """Build from a dict (e.g. an HDF5 attr or CLI overrides).

        Unknown keys raise ``ValueError``. Values are coerced to the field
        type so string-valued overrides such as ``regularize=false`` or
        ``window_radius=3`` work.
        """
        known = {f.name: f.type for f in fields(cls)}
        unknown = set(data) - set(known)
        if unknown:
            raise ValueError(
                f"Unknown wavelet parameter(s): {sorted(unknown)}; "
                f"expected one of {sorted(known)}"
            )
        coerced: dict[str, Any] = {}
        for key, value in data.items():
            coerced[key] = _coerce(key, value, known[key])
        built = cls(**coerced)
        if "method" not in data:
            # Partial dicts (CLI overrides) carry no label: derive it.
            built = built.with_overrides()
        return built

    def with_overrides(self, **overrides: Any) -> WaveletParams:
        """Copy with some levers changed; the label becomes ``custom``
        unless the result still equals a preset."""
        candidate = replace(self, **overrides)
        for name in ("leelab", "paper"):
            preset = self.preset(name)
            if candidate.same_computation(preset):
                return replace(candidate, method=name)
        return replace(candidate, method="custom")

    def levers(self) -> dict[str, Any]:
        """The fields that affect the computation (everything but the label)."""
        d = self.to_dict()
        d.pop("method")
        return d

    def same_computation(self, other: WaveletParams) -> bool:
        return self.levers() == other.levers()

    def label(self) -> str:
        """Short human-readable name for status lines."""
        return {"leelab": "LeeLab", "paper": "Paper", "custom": "Custom"}[
            self.method
        ]


def _check_choice(name: str, value: Any, choices: tuple[str, ...]) -> None:
    if value not in choices:
        raise ValueError(f"{name} must be one of {choices}, got {value!r}")


def _coerce(key: str, value: Any, type_name: Any) -> Any:
    """Coerce a loosely typed value (JSON/CLI string) to the field's type."""
    # ``fields()`` reports annotations as strings under ``from __future__``.
    tname = type_name if isinstance(type_name, str) else getattr(
        type_name, "__name__", str(type_name)
    )
    if tname == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            low = value.strip().lower()
            if low in ("true", "1", "yes", "on"):
                return True
            if low in ("false", "0", "no", "off"):
                return False
        raise ValueError(f"{key} must be a boolean, got {value!r}")
    if tname == "int":
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be an integer, got {value!r}") from exc
    if tname == "float":
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be a number, got {value!r}") from exc
    return str(value)


# ── Transforms ─────────────────────────────────────────────────


def anscombe_transform(data, clamp: str = "before"):
    """Anscombe transform to stabilize Poisson noise variance.

    ``clamp="before"`` clamps ``data`` to non-negative *before* adding 3/8,
    matching ``ComplexWaveletFilter.anscombe_transform`` exactly.
    ``clamp="after"`` adds 3/8 first and clamps the radicand, which is the
    paper's eq. 6 as written. They differ for every negative input —
    negative Fourier coordinates ``G*I`` at noisy pixels — and that
    difference is enough to perturb the filtered phasor by ~0.02.
    """
    if clamp == "before":
        return 2 * np.sqrt(np.maximum(data, 0) + (3 / 8))
    if clamp == "after":
        return 2 * np.sqrt(np.maximum(data + (3 / 8), 0))
    _check_choice("anscombe_clamp", clamp, ANSCOMBE_CLAMP)
    raise AssertionError("unreachable")


def reverse_anscombe_transform(y, method: str = "exact"):
    """Inverse Anscombe transform.

    ``method="exact"`` is the sixth-order rational (unbiased) inverse,
    faithful to ``ComplexWaveletFilter.reverse_anscombe_transform``: no
    clamping of ``y`` and no flooring of the result. Small or non-positive
    reconstructed values therefore yield inf/NaN here, exactly as in the
    reference; :func:`denoise_phasor` sweeps them up with ``nan_to_num`` +
    clip during phasor recovery. ``method="algebraic"`` is the literal
    inverse of the forward transform, ``(y/2)² − 3/8``.
    """
    y = np.asarray(y, dtype=np.float64)
    if method == "algebraic":
        return (y / 2.0) ** 2 - (3 / 8)
    if method != "exact":
        _check_choice("inverse_anscombe", method, INVERSE_ANSCOMBE)
    with np.errstate(divide="ignore", invalid="ignore"):
        return (
            (y**2 / 4)
            + (np.sqrt(3 / 2) * (1 / y) / 4)
            - (11 / (8 * y**2))
            + (np.sqrt(5 / 2) * (1 / y**3) / 8)
            - (1 / (8 * y**4))
        )


# ── Noise estimation (vectorized) ─────────────────────────────


def calculate_median_values(transformed_data) -> float:
    """LeeLab global estimate: mean over every level and band of the median
    absolute coefficient."""
    median_values = []
    for level in range(len(transformed_data.highpasses)):
        highpasses = transformed_data.highpasses[level]
        for band in range(highpasses.shape[2]):
            coeffs = highpasses[:, :, band]
            median_absolute = np.median(np.abs(coeffs))
            median_values.append(median_absolute)
    return float(np.mean(median_values))


def estimate_noise_sigma(transformed_data, noise_bands: str = "all") -> float:
    """Global noise standard deviation σ from a MAD-style estimate.

    ``all`` (LeeLab): ``mean(median|Φ|) / 0.6745`` over every level and
    band. ``finest_diagonal`` (paper, supplement eq. 1): one median over the
    finest-level ±45° bands, divided by 0.6745.
    """
    if noise_bands == "finest_diagonal":
        finest = transformed_data.highpasses[0]
        mags = np.concatenate(
            [np.abs(finest[:, :, b]).ravel() for b in DIAGONAL_BANDS]
        )
        return float(np.median(mags)) / 0.6745
    if noise_bands != "all":
        _check_choice("noise_bands", noise_bands, NOISE_BANDS)
    return calculate_median_values(transformed_data) / 0.6745


def calculate_local_noise_variance(
    transformed_data, n_levels: int, window_radius: int = 0
) -> list[list[NDArray]]:
    """Local energy σn²: mean of |Φ|² in a ``(2r+1)×(2r+1)`` window.

    Returns ``result[level][band]``. ``window_radius=0`` applies the LeeLab
    rule (``r = n_levels``, or 3 when ``n_levels > 10``). Vectorized with
    ``scipy.ndimage.uniform_filter`` (zero-padded, like the reference's
    edge-clipped windows only in the interior — identical to the reference
    output to float precision on the verified dataset).
    """
    if window_radius > 0:
        ws = window_radius
    else:
        ws = 3 if n_levels > 10 else n_levels
    kernel = 2 * ws + 1  # convert radius to diameter for uniform_filter

    out: list[list[NDArray]] = []
    for level in range(len(transformed_data.highpasses)):
        highpasses = transformed_data.highpasses[level]
        per_band = []
        for band in range(highpasses.shape[2]):
            abs_sq = np.abs(highpasses[:, :, band]) ** 2
            per_band.append(uniform_filter(abs_sq, size=kernel, mode="constant"))
        out.append(per_band)
    return out


# ── Shrinkage ──────────────────────────────────────────────────


def shrink_factor(
    phi_sq_sum: NDArray,
    sigma_n_sq: NDArray,
    sigma: float,
    params: WaveletParams,
) -> NDArray:
    """Per-coefficient shrinkage factor in ``[0, 1]``.

    ``phi_sq_sum`` is ``|Φ|² + |Φparent|²`` (bivariate magnitude squared),
    ``sigma_n_sq`` the local energy, ``sigma`` the global noise std.

    * threshold ``T = √3 · σ^p`` (``p = params.sigma_exponent``)
    * ``local_variance="divide"``: ``T ← T / √(σn² − σ²)₊`` (infinite,
      i.e. fully shrunk, where no local signal remains)
    * ``local_variance="gate"``: ``T`` stays global; pixels with zero local
      energy are zeroed
    * ``factor = (1 − T / √(phi_sq_sum [+ T if regularize]))₊``
    """
    threshold = np.sqrt(3.0) * float(sigma) ** params.sigma_exponent
    if params.local_variance == "divide":
        local_sig = np.sqrt(np.maximum(sigma_n_sq - float(sigma) ** 2, 0.0))
        with np.errstate(divide="ignore", invalid="ignore"):
            t_eff = np.where(local_sig > 0, threshold / local_sig, np.inf)
        valid = (phi_sq_sum > 0) & np.isfinite(t_eff)
    else:
        t_eff = np.full(phi_sq_sum.shape, threshold, dtype=np.float64)
        valid = (sigma_n_sq > 0) & (phi_sq_sum > 0)

    radicand = phi_sq_sum + t_eff if params.regularize else phi_sq_sum
    with np.errstate(divide="ignore", invalid="ignore"):
        denominator = np.sqrt(radicand)
        factor = np.where(valid, 1.0 - t_eff / denominator, 0.0)
    return np.maximum(factor, 0.0)


def _upsample_parent_sq(parent_band: NDArray, shape: tuple[int, int]) -> NDArray:
    """|Φparent|² brought to the child's grid: parent ``(x/2, y/2)`` for
    each child ``(x, y)`` (nearest-neighbour 2x, clamped at the edge)."""
    h, w = shape
    parent_sq = np.abs(parent_band) ** 2
    y_idx = np.minimum(np.arange(h) // 2, parent_sq.shape[0] - 1)
    x_idx = np.minimum(np.arange(w) // 2, parent_sq.shape[1] - 1)
    return parent_sq[np.ix_(y_idx, x_idx)]


def compute_phi_prime(
    transformed_data,
    sigma: float,
    sigma_n_squared: list[list[NDArray]],
    params: WaveletParams | None = None,
) -> list[list[NDArray]]:
    """Shrunk coefficients ``result[level][band]`` for every level that is
    filtered (all but the coarsest unless ``params.shrink_coarsest``).

    Each coefficient is scaled by :func:`shrink_factor` of its own
    magnitude squared plus its parent's (the coarser level at ``x/2, y/2``).
    Levels are computed from the *unshrunk* pyramid, so the order of the
    later in-place update does not matter.
    """
    params = params or WaveletParams.leelab()
    n_levels = len(transformed_data.highpasses)
    last = n_levels if params.shrink_coarsest else n_levels - 1

    updated: list[list[NDArray]] = []
    for level in range(last):
        highpasses_l = transformed_data.highpasses[level]
        parent = (
            transformed_data.highpasses[level + 1]
            if level + 1 < n_levels else None
        )
        level_coefficients = []
        for band in range(highpasses_l.shape[2]):
            phi = highpasses_l[:, :, band]
            phi_sq_sum = np.abs(phi) ** 2
            if parent is not None:
                phi_sq_sum = phi_sq_sum + _upsample_parent_sq(
                    parent[:, :, band], phi.shape
                )
            factor = shrink_factor(
                phi_sq_sum, sigma_n_squared[level][band], sigma, params
            )
            level_coefficients.append(factor * phi)
        updated.append(level_coefficients)
    return updated


def update_coefficients(transformed_data, phi_prime_matrices) -> None:
    """Write shrunk coefficients back into the pyramid in place."""
    for level, level_matrices in enumerate(phi_prime_matrices):
        for band, phi_prime in enumerate(level_matrices):
            transformed_data.highpasses[level][:, :, band] = phi_prime


# ── Main filter function ──────────────────────────────────────


def _next_pow2(n: int) -> int:
    """Return the smallest power of 2 >= n."""
    p = 1
    while p < n:
        p *= 2
    return p


def _filter_channel(
    data: NDArray, n_levels: int, params: WaveletParams | None = None
) -> NDArray:
    """Apply DTCWT denoising to a single 2D channel.

    Anscombe → DTCWT (``params.biort`` / ``qshift_a``) → BiShrink-style
    shrinkage → inverse DTCWT → inverse Anscombe. With the LeeLab preset
    this mirrors ``ComplexWaveletFilter.process_files``' per-channel
    filtering to float precision; the basis matters for that identity
    (``near_sym_a`` leaves a ~1e-3 residual, ``Legall`` matches).
    """
    import dtcwt

    params = params or WaveletParams.leelab()

    # Pad to power-of-2 dimensions for DTCWT
    h, w = data.shape
    pad_h = _next_pow2(h) - h
    pad_w = _next_pow2(w) - w
    padded = np.pad(data, ((0, pad_h), (0, pad_w)), mode="reflect")

    transformed = anscombe_transform(padded, clamp=params.anscombe_clamp)

    # dtcwt resolves the basis name to a data file (``legall.npz``); the lookup
    # is case-sensitive on Linux, so the user-facing ``Legall`` label is
    # lowercased here rather than renamed in presets, params, and saved files.
    xfm = dtcwt.Transform2d(biort=params.biort.lower(), qshift="qshift_a")
    coeffs = xfm.forward(transformed, nlevels=n_levels)

    sigma = estimate_noise_sigma(coeffs, noise_bands=params.noise_bands)
    sigma_n_squared = calculate_local_noise_variance(
        coeffs, n_levels, window_radius=params.window_radius
    )
    phi_prime = compute_phi_prime(coeffs, sigma, sigma_n_squared, params)
    update_coefficients(coeffs, phi_prime)

    reconstructed = xfm.inverse(coeffs)
    result = reverse_anscombe_transform(
        reconstructed, method=params.inverse_anscombe
    )

    # Remove padding
    return result[:h, :w]


def denoise_phasor(
    g: NDArray,
    s: NDArray,
    intensity: NDArray,
    filter_level: int = 9,
    omega: float | None = None,
    params: WaveletParams | None = None,
) -> dict[str, Any]:
    """Apply DTCWT-based wavelet filtering to FLIM phasor data.

    Filters the Fourier images ``G·I``, ``S·I`` and ``I`` separately, then
    recovers ``G = Gfiltered·I / Ifiltered`` (paper eqs. 3-8) with the
    reference's recovery (``nan_to_num``, threshold by the *unfiltered*
    intensity, clip to ``[-0.1, 1.1]``).

    Parameters
    ----------
    g : (H, W) G phasor coordinate map
    s : (H, W) S phasor coordinate map
    intensity : (H, W) total photon counts per pixel
    filter_level : DTCWT decomposition depth (default 9)
    omega : angular frequency in rad/ns (for lifetime calculation, optional)
    params : which algorithm variant to run; ``None`` = LeeLab reference

    Returns
    -------
    dict with keys:
        'G' : filtered G map
        'S' : filtered S map
        'T' : filtered lifetime map (if omega provided, else None)
        'GU' : unfiltered G map (copy of input)
        'SU' : unfiltered S map (copy of input)
        'TU' : unfiltered lifetime map (if omega provided, else None)
        'filter_level' : decomposition level used
        'params' : the :class:`WaveletParams` used, as a dict
    """
    params = params or WaveletParams.leelab()

    g = g.astype(np.float64)
    s = s.astype(np.float64)
    intensity = intensity.astype(np.float64)

    # Unfiltered copies
    g_unfiltered = g.copy()
    s_unfiltered = s.copy()

    # Step 1: Rescale to Fourier coefficients
    f_real = g * intensity
    f_imag = s * intensity

    # Step 2-5: Filter each channel
    logger.debug("Wavelet (%s, level %d): filtering Freal", params.method, filter_level)
    f_real_filtered = _filter_channel(f_real, filter_level, params)
    logger.debug("Wavelet (%s, level %d): filtering Fimag", params.method, filter_level)
    f_imag_filtered = _filter_channel(f_imag, filter_level, params)
    logger.debug("Wavelet (%s, level %d): filtering intensity", params.method, filter_level)
    intensity_filtered = _filter_channel(intensity, filter_level, params)

    # Step 6: Recover filtered phasor — faithful to
    # ComplexWaveletFilter.process_files. Raw-divide by the *filtered*
    # intensity (inf/NaN from a non-positive filtered intensity is swept
    # up by nan_to_num below, exactly as the reference does), then
    # threshold by the *unfiltered* intensity and clip to the phasor
    # display range [-0.1, 1.1].
    with np.errstate(divide="ignore", invalid="ignore"):
        g_filtered = f_real_filtered / intensity_filtered
        s_filtered = f_imag_filtered / intensity_filtered
    g_filtered = np.nan_to_num(g_filtered)
    s_filtered = np.nan_to_num(s_filtered)
    thr = intensity > 0
    g_filtered = np.clip(g_filtered * thr, -0.1, 1.1)
    s_filtered = np.clip(s_filtered * thr, -0.1, 1.1)

    # Lifetime calculation if omega provided
    t_filtered = None
    t_unfiltered = None
    if omega is not None and omega > 0:
        with np.errstate(divide="ignore", invalid="ignore"):
            t_filtered = s_filtered / (omega * g_filtered)
            t_unfiltered = s_unfiltered / (omega * g_unfiltered)
        t_filtered = np.where(
            (t_filtered < 0) | (t_filtered > 50) | np.isnan(t_filtered),
            np.nan,
            t_filtered,
        )
        t_unfiltered = np.where(
            (t_unfiltered < 0) | (t_unfiltered > 50) | np.isnan(t_unfiltered),
            np.nan,
            t_unfiltered,
        )

    return {
        "G": g_filtered.astype(np.float32),
        "S": s_filtered.astype(np.float32),
        "T": t_filtered.astype(np.float32) if t_filtered is not None else None,
        "GU": g_unfiltered.astype(np.float32),
        "SU": s_unfiltered.astype(np.float32),
        "TU": t_unfiltered.astype(np.float32) if t_unfiltered is not None else None,
        "filter_level": filter_level,
        "params": params.to_dict(),
    }

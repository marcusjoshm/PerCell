"""Tests for FlimPanel cache-check behavior on Compute Phasor / Apply Wavelet.

Both buttons load from cache by default; Shift-click forces recompute.
The cache check goes through LoadCachedPhasor (U1); the recompute path
goes through the existing ComputePhasor / ApplyWavelet use cases.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from percell4.application.session import Session
from percell4.domain.dataset import DatasetHandle
from percell4.domain.flim.wavelet_filter import MAX_FILTER_LEVEL, WaveletParams
from percell4.interfaces.gui.task_panels.flim_panel import FlimPanel
from percell4.model import CellDataModel


class FakeRepo:
    def __init__(self):
        self.arrays: dict[str, np.ndarray] = {}
        self.attrs: dict[str, dict] = {}
        self.disk_metadata: dict = {}

    def write_array(self, handle, path, data, attrs=None):
        self.arrays[path] = data
        if attrs:
            self.attrs[path] = dict(attrs)

    def read_array(self, handle, path, view_bin=1):
        if path not in self.arrays:
            raise KeyError(f"Array not found: {path}")
        return self.arrays[path]

    def read_array_attrs(self, handle, path):
        return dict(self.attrs.get(path, {}))

    def array_exists(self, handle, path):
        return path in self.arrays

    def read_metadata(self, handle):
        return dict(self.disk_metadata)


# The FlimPanel's Filter Level spinbox defaults to 9; seeded wavelet caches
# are stamped at this level so the default-level Apply hits the cache.
_DEFAULT_WAVELET_LEVEL = 9


def _seed_full_cache(
    repo: FakeRepo, channel: str = "ch0", shape=(8, 8),
    filter_level: int = _DEFAULT_WAVELET_LEVEL,
):
    rng = np.random.default_rng(0)
    repo.arrays[f"phasor/{channel}/g"] = rng.uniform(0.1, 0.9, size=shape).astype(np.float32)
    repo.arrays[f"phasor/{channel}/s"] = rng.uniform(0.05, 0.5, size=shape).astype(np.float32)
    repo.arrays[f"phasor/{channel}/g_filtered"] = rng.uniform(size=shape).astype(np.float32)
    repo.arrays[f"phasor/{channel}/s_filtered"] = rng.uniform(size=shape).astype(np.float32)
    # Stamped the way ApplyWavelet stamps it: the default (LeeLab) variant.
    repo.attrs[f"phasor/{channel}/g_filtered"] = {
        "filter_level": filter_level,
        "wavelet_method": "leelab",
        "wavelet_params": json.dumps(WaveletParams.leelab().to_dict()),
    }
    repo.arrays[f"decay/{channel}"] = rng.uniform(size=(*shape, 16)).astype(np.float32)


@pytest.fixture
def session_with_dataset(tmp_path):
    s = Session()
    handle = DatasetHandle(path=tmp_path / "fake.h5", metadata={"flim_frequency_mhz": 80.0})
    s._dataset = handle
    s._active_channel = "ch0"
    return s


@pytest.fixture
def panel(qtbot, session_with_dataset):
    repo = FakeRepo()
    _seed_full_cache(repo)
    phasor_win = MagicMock()
    data_model = CellDataModel(session_with_dataset)
    p = FlimPanel(
        data_model,
        get_repo=lambda: repo,
        get_viewer_window=lambda: None,
        get_phasor_window=lambda: phasor_win,
        get_active_seg_labels=lambda: None,
        show_window=lambda _: None,
        show_status=lambda _: None,
    )
    qtbot.addWidget(p)
    p._test_repo = repo
    p._test_phasor_win = phasor_win
    return p


# ── Cache-hit path ────────────────────────────────────────────


def test_compute_phasor_loads_from_cache_when_no_shift(panel):
    """No Shift held + cache present → compute use case NOT invoked;
    phasor window populated from cache."""
    with patch(
        "percell4.application.use_cases.compute_phasor.ComputePhasor.execute"
    ) as mock_compute:
        with patch.object(panel, "_shift_held", return_value=False):
            panel._on_compute_phasor()

    mock_compute.assert_not_called()
    panel._test_phasor_win.set_phasor_data.assert_called_once()


def test_apply_wavelet_loads_from_cache_when_no_shift(panel):
    """No Shift held + wavelet cache present → wavelet use case NOT invoked."""
    with patch(
        "percell4.application.use_cases.apply_wavelet.ApplyWavelet.execute"
    ) as mock_wavelet:
        with patch.object(panel, "_shift_held", return_value=False):
            panel._on_apply_wavelet()

    mock_wavelet.assert_not_called()
    panel._test_phasor_win.set_phasor_data.assert_called_once()


# ── Shift bypass ──────────────────────────────────────────────


def test_compute_phasor_shift_bypasses_cache(panel):
    """Shift held → ComputePhasor.execute IS invoked even when cache present."""
    fake_result = MagicMock(g_map=np.zeros((4, 4), dtype=np.float32),
                            s_map=np.zeros((4, 4), dtype=np.float32),
                            n_valid=10)
    with patch(
        "percell4.application.use_cases.compute_phasor.ComputePhasor.execute",
        return_value=fake_result,
    ) as mock_compute:
        with patch.object(panel, "_shift_held", return_value=True):
            panel._on_compute_phasor()

    mock_compute.assert_called_once()


def test_apply_wavelet_shift_bypasses_cache(panel):
    """Shift held → ApplyWavelet.execute IS invoked even when wavelet cache present."""
    fake_result = MagicMock(
        g_filtered=np.zeros((4, 4), dtype=np.float32),
        s_filtered=np.zeros((4, 4), dtype=np.float32),
        n_valid=10,
    )
    with patch(
        "percell4.application.use_cases.apply_wavelet.ApplyWavelet.execute",
        return_value=fake_result,
    ) as mock_wavelet:
        with patch.object(panel, "_shift_held", return_value=True):
            panel._on_apply_wavelet()

    mock_wavelet.assert_called_once()


# ── No cache → fall through to compute ────────────────────────


def test_compute_phasor_no_cache_runs_compute(panel):
    """Empty repo → cache check raises NoCachedPhasorError → ComputePhasor runs."""
    panel._test_repo.arrays.clear()
    fake_result = MagicMock(g_map=np.zeros((4, 4), dtype=np.float32),
                            s_map=np.zeros((4, 4), dtype=np.float32),
                            n_valid=10)
    with patch(
        "percell4.application.use_cases.compute_phasor.ComputePhasor.execute",
        return_value=fake_result,
    ) as mock_compute:
        with patch.object(panel, "_shift_held", return_value=False):
            panel._on_compute_phasor()

    mock_compute.assert_called_once()


def test_apply_wavelet_raw_cache_only_runs_wavelet(panel):
    """Raw phasor cache but no wavelet → falls through to wavelet compute."""
    # Remove wavelet cache, keep raw
    panel._test_repo.arrays.pop("phasor/ch0/g_filtered")
    panel._test_repo.arrays.pop("phasor/ch0/s_filtered")
    fake_result = MagicMock(
        g_filtered=np.zeros((4, 4), dtype=np.float32),
        s_filtered=np.zeros((4, 4), dtype=np.float32),
        n_valid=10,
    )
    with patch(
        "percell4.application.use_cases.apply_wavelet.ApplyWavelet.execute",
        return_value=fake_result,
    ) as mock_wavelet:
        with patch.object(panel, "_shift_held", return_value=False):
            panel._on_apply_wavelet()

    mock_wavelet.assert_called_once()


# ── Filter-level spinbox range ────────────────────────────────


def test_wavelet_level_spinbox_allows_up_to_max(panel):
    """The Filter Level spinbox accepts levels up to MAX_FILTER_LEVEL (well
    above the legacy cap of 15)."""
    assert panel._wavelet_level.maximum() == MAX_FILTER_LEVEL
    assert panel._wavelet_level.minimum() == 1
    assert MAX_FILTER_LEVEL > 15
    # A value above the old cap is settable and round-trips.
    panel._wavelet_level.setValue(20)
    assert panel._wavelet_level.value() == 20


# ── Filter-level change → recompute over cache ────────────────


def test_apply_wavelet_same_level_loads_from_cache(panel):
    """Cache stamped at level 9 + spinbox at 9 → no recompute, cache served."""
    panel._wavelet_level.setValue(_DEFAULT_WAVELET_LEVEL)
    with patch(
        "percell4.application.use_cases.apply_wavelet.ApplyWavelet.execute"
    ) as mock_wavelet:
        with patch.object(panel, "_shift_held", return_value=False):
            panel._on_apply_wavelet()

    mock_wavelet.assert_not_called()
    panel._test_phasor_win.set_phasor_data.assert_called_once()


def test_apply_wavelet_different_level_recomputes_over_cache(panel):
    """Cache stamped at level 9 but the user picks a different level →
    ApplyWavelet recomputes at the requested level instead of serving the
    stale cache. This is the bug fix: a level change must not no-op."""
    panel._wavelet_level.setValue(5)
    fake_result = MagicMock(
        g_filtered=np.zeros((4, 4), dtype=np.float32),
        s_filtered=np.zeros((4, 4), dtype=np.float32),
        n_valid=10,
    )
    with patch(
        "percell4.application.use_cases.apply_wavelet.ApplyWavelet.execute",
        return_value=fake_result,
    ) as mock_wavelet:
        with patch.object(panel, "_shift_held", return_value=False):
            panel._on_apply_wavelet()

    mock_wavelet.assert_called_once()
    # The recompute used the newly requested level (which ApplyWavelet then
    # writes over the old cache).
    assert mock_wavelet.call_args.kwargs["filter_level"] == 5


def test_apply_wavelet_unknown_cached_level_recomputes(panel):
    """Wavelet cache present but missing the filter_level attr (a pre-attr
    file) → treated as level-unknown and recomputed rather than served."""
    panel._test_repo.attrs.pop("phasor/ch0/g_filtered", None)
    fake_result = MagicMock(
        g_filtered=np.zeros((4, 4), dtype=np.float32),
        s_filtered=np.zeros((4, 4), dtype=np.float32),
        n_valid=10,
    )
    with patch(
        "percell4.application.use_cases.apply_wavelet.ApplyWavelet.execute",
        return_value=fake_result,
    ) as mock_wavelet:
        with patch.object(panel, "_shift_held", return_value=False):
            panel._on_apply_wavelet()

    mock_wavelet.assert_called_once()


# ── Tooltip discoverability ───────────────────────────────────


def test_compute_phasor_button_tooltip_mentions_shift(panel):
    """Find the Compute Phasor button in the panel and assert tooltip contains 'Shift+click'."""
    from qtpy.QtWidgets import QPushButton

    buttons = panel.findChildren(QPushButton)
    compute_btn = next(b for b in buttons if b.text() == "Compute Phasor")
    assert "Shift+click" in compute_btn.toolTip()


def test_apply_wavelet_button_tooltip_mentions_shift(panel):
    from qtpy.QtWidgets import QPushButton

    buttons = panel.findChildren(QPushButton)
    wavelet_btn = next(b for b in buttons if b.text() == "Apply Wavelet Filter")
    assert "Shift+click" in wavelet_btn.toolTip()


# ── No active channel ─────────────────────────────────────────


def test_compute_phasor_no_active_channel_returns_early(panel, session_with_dataset):
    session_with_dataset._active_channel = None
    with patch(
        "percell4.application.use_cases.compute_phasor.ComputePhasor.execute"
    ) as mock_compute:
        panel._on_compute_phasor()
    mock_compute.assert_not_called()
    panel._test_phasor_win.set_phasor_data.assert_not_called()


# ── Wavelet method presets + levers ───────────────────────────


def _fake_wavelet_result():
    return MagicMock(
        g_filtered=np.zeros((4, 4), dtype=np.float32),
        s_filtered=np.zeros((4, 4), dtype=np.float32),
        n_valid=10,
    )


def test_wavelet_method_defaults_to_leelab_with_levers_hidden(panel):

    assert panel._wavelet_method.currentData() == "leelab"
    assert panel._wavelet_params() == WaveletParams.leelab()
    assert not panel._wavelet_levers.isVisibleTo(panel)
    panel._wavelet_show_levers.setChecked(True)
    assert panel._wavelet_levers.isVisibleTo(panel)


def test_paper_preset_populates_levers(panel):

    panel._wavelet_method.setCurrentIndex(panel._wavelet_method.findData("paper"))
    assert panel._wl_local_variance.currentData() == "divide"
    assert panel._wl_noise_bands.currentData() == "finest_diagonal"
    assert panel._wl_sigma_exponent.value() == 2.0
    assert not panel._wl_regularize.isChecked()
    assert panel._wl_window_radius.value() == 3
    assert panel._wl_inverse_anscombe.currentData() == "algebraic"
    assert panel._wavelet_params() == WaveletParams.paper()


def test_moving_a_lever_relabels_custom_and_back(panel):
    panel._wavelet_method.setCurrentIndex(panel._wavelet_method.findData("paper"))
    panel._wl_regularize.setChecked(True)
    assert panel._wavelet_method.currentData() == "custom"
    assert panel._wavelet_params().method == "custom"
    assert panel._wavelet_params().regularize is True
    # Other paper levers survive the relabel.
    assert panel._wavelet_params().local_variance == "divide"
    panel._wl_regularize.setChecked(False)
    assert panel._wavelet_method.currentData() == "paper"


def test_apply_wavelet_unstamped_cache_recomputes_under_default(panel):
    """A cache with no wavelet_params attr predates the variants: it was
    computed by the reference script's Anscombe pair, which the corrected
    LeeLab preset no longer matches, so the default Apply recomputes."""
    panel._test_repo.attrs["phasor/ch0/g_filtered"] = {
        "filter_level": _DEFAULT_WAVELET_LEVEL,
    }
    panel._wavelet_level.setValue(_DEFAULT_WAVELET_LEVEL)
    with patch(
        "percell4.application.use_cases.apply_wavelet.ApplyWavelet.execute",
        return_value=_fake_wavelet_result(),
    ) as mock_wavelet:
        with patch.object(panel, "_shift_held", return_value=False):
            panel._on_apply_wavelet()

    mock_wavelet.assert_called_once()
    assert mock_wavelet.call_args.kwargs["params"] == WaveletParams.leelab()


def test_apply_wavelet_reference_script_cache_recomputes_under_default(panel):
    """A cache stamped with the pre-correction levers (labelled 'leelab'
    by an older build) must not be served for today's LeeLab preset."""
    stale = WaveletParams.reference_script().to_dict()
    stale["method"] = "leelab"
    panel._test_repo.attrs["phasor/ch0/g_filtered"] = {
        "filter_level": _DEFAULT_WAVELET_LEVEL,
        "wavelet_method": "leelab",
        "wavelet_params": json.dumps(stale),
    }
    panel._wavelet_level.setValue(_DEFAULT_WAVELET_LEVEL)
    with patch(
        "percell4.application.use_cases.apply_wavelet.ApplyWavelet.execute",
        return_value=_fake_wavelet_result(),
    ) as mock_wavelet:
        with patch.object(panel, "_shift_held", return_value=False):
            panel._on_apply_wavelet()

    mock_wavelet.assert_called_once()
    assert mock_wavelet.call_args.kwargs["params"] == WaveletParams.leelab()


def test_apply_wavelet_paper_method_recomputes_over_leelab_cache(panel):
    """The seeded cache is stamped LeeLab. Asking for the paper variant at
    the same level must recompute, passing the paper params to the use
    case."""

    panel._wavelet_level.setValue(_DEFAULT_WAVELET_LEVEL)
    panel._wavelet_method.setCurrentIndex(panel._wavelet_method.findData("paper"))
    with patch(
        "percell4.application.use_cases.apply_wavelet.ApplyWavelet.execute",
        return_value=_fake_wavelet_result(),
    ) as mock_wavelet:
        with patch.object(panel, "_shift_held", return_value=False):
            panel._on_apply_wavelet()

    mock_wavelet.assert_called_once()
    assert mock_wavelet.call_args.kwargs["params"] == WaveletParams.paper()
    assert mock_wavelet.call_args.kwargs["filter_level"] == _DEFAULT_WAVELET_LEVEL


def test_apply_wavelet_matching_paper_cache_is_served(panel):
    """Cache stamped with the paper variant + panel set to paper → cache hit."""


    panel._test_repo.attrs["phasor/ch0/g_filtered"] = {
        "filter_level": _DEFAULT_WAVELET_LEVEL,
        "wavelet_method": "paper",
        "wavelet_params": json.dumps(WaveletParams.paper().to_dict()),
    }
    panel._wavelet_level.setValue(_DEFAULT_WAVELET_LEVEL)
    panel._wavelet_method.setCurrentIndex(panel._wavelet_method.findData("paper"))
    with patch(
        "percell4.application.use_cases.apply_wavelet.ApplyWavelet.execute"
    ) as mock_wavelet:
        with patch.object(panel, "_shift_held", return_value=False):
            panel._on_apply_wavelet()

    mock_wavelet.assert_not_called()
    panel._test_phasor_win.set_phasor_data.assert_called_once()


def test_apply_wavelet_leelab_over_paper_cache_recomputes(panel):


    panel._test_repo.attrs["phasor/ch0/g_filtered"] = {
        "filter_level": _DEFAULT_WAVELET_LEVEL,
        "wavelet_method": "paper",
        "wavelet_params": json.dumps(WaveletParams.paper().to_dict()),
    }
    panel._wavelet_level.setValue(_DEFAULT_WAVELET_LEVEL)
    assert panel._wavelet_method.currentData() == "leelab"
    with patch(
        "percell4.application.use_cases.apply_wavelet.ApplyWavelet.execute",
        return_value=_fake_wavelet_result(),
    ) as mock_wavelet:
        with patch.object(panel, "_shift_held", return_value=False):
            panel._on_apply_wavelet()

    mock_wavelet.assert_called_once()
    assert mock_wavelet.call_args.kwargs["params"] == WaveletParams.leelab()


def test_apply_wavelet_corrupt_params_attr_recomputes(panel):
    panel._test_repo.attrs["phasor/ch0/g_filtered"] = {
        "filter_level": _DEFAULT_WAVELET_LEVEL,
        "wavelet_params": json_dumps_bad(),
    }
    panel._wavelet_level.setValue(_DEFAULT_WAVELET_LEVEL)
    with patch(
        "percell4.application.use_cases.apply_wavelet.ApplyWavelet.execute",
        return_value=_fake_wavelet_result(),
    ) as mock_wavelet:
        with patch.object(panel, "_shift_held", return_value=False):
            panel._on_apply_wavelet()

    mock_wavelet.assert_called_once()


def json_dumps_bad() -> str:

    return json.dumps({"noise_bands": "not-a-real-choice"})


def test_custom_levers_matching_a_preset_hit_that_presets_cache(panel):
    """Custom selected but with LeeLab's exact values → same computation →
    the LeeLab cache is served rather than recomputed."""
    panel._wavelet_method.setCurrentIndex(panel._wavelet_method.findData("paper"))
    panel._wavelet_method.setCurrentIndex(panel._wavelet_method.findData("custom"))
    # Custom keeps the paper levers; hand-set them back to LeeLab's values.

    panel._set_wavelet_levers(WaveletParams.leelab())
    panel._on_wavelet_lever_changed()
    assert panel._wavelet_method.currentData() == "leelab"
    panel._wavelet_level.setValue(_DEFAULT_WAVELET_LEVEL)
    with patch(
        "percell4.application.use_cases.apply_wavelet.ApplyWavelet.execute"
    ) as mock_wavelet:
        with patch.object(panel, "_shift_held", return_value=False):
            panel._on_apply_wavelet()
    mock_wavelet.assert_not_called()

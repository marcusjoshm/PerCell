"""ApplyWavelet / LoadCachedPhasor carry the wavelet algorithm variant.

ApplyWavelet stamps ``wavelet_method`` + ``wavelet_params`` (JSON) next to
``filter_level`` on the filtered maps; LoadCachedPhasor reads them back so
the FLIM panel can tell a same-level cache computed with another variant.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from percell4.application.session import Session
from percell4.application.use_cases.apply_wavelet import ApplyWavelet
from percell4.application.use_cases.load_cached_phasor import LoadCachedPhasor
from percell4.domain.dataset import DatasetHandle
from percell4.domain.flim.wavelet_filter import WaveletParams


class FakeRepo:
    def __init__(self):
        self.arrays: dict[str, np.ndarray] = {}
        self.attrs: dict[str, dict] = {}

    def write_array(self, handle, path, data, attrs=None):
        self.arrays[path] = data
        self.attrs[path] = dict(attrs or {})

    def read_array(self, handle, path, view_bin=1):
        if path not in self.arrays:
            raise KeyError(path)
        return self.arrays[path]

    def read_array_attrs(self, handle, path):
        return dict(self.attrs.get(path, {}))

    def read_metadata(self, handle):
        return dict(handle.metadata)


@pytest.fixture
def session(tmp_path):
    s = Session()
    s._dataset = DatasetHandle(
        path=tmp_path / "d.h5", metadata={"flim_frequency_mhz": 80.0}
    )
    return s


@pytest.fixture
def repo():
    r = FakeRepo()
    rng = np.random.default_rng(0)
    r.arrays["phasor/ch0/g"] = rng.uniform(size=(6, 6)).astype(np.float32)
    r.arrays["phasor/ch0/s"] = rng.uniform(size=(6, 6)).astype(np.float32)
    r.arrays["decay/ch0"] = rng.uniform(size=(6, 6, 4)).astype(np.float32)
    return r


@pytest.fixture
def capture_denoise(monkeypatch):
    calls = []

    def fake_denoise(g, s, intensity, filter_level=9, omega=None, params=None):
        calls.append({"filter_level": filter_level, "params": params})
        return {"G": g.astype(np.float32), "S": s.astype(np.float32), "T": None}

    import percell4.domain.flim.wavelet_filter as wfmod
    monkeypatch.setattr(wfmod, "denoise_phasor", fake_denoise)
    return calls


def test_apply_wavelet_defaults_to_leelab_and_stamps_attrs(session, repo, capture_denoise):
    result = ApplyWavelet(repo, session).execute(channel="ch0", filter_level=4)
    assert capture_denoise[0]["params"] == WaveletParams.leelab()
    assert result.params == WaveletParams.leelab()
    attrs = repo.attrs["phasor/ch0/g_filtered"]
    assert attrs["filter_level"] == 4
    assert attrs["wavelet_method"] == "leelab"
    assert json.loads(attrs["wavelet_params"]) == WaveletParams.leelab().to_dict()
    assert repo.attrs["phasor/ch0/s_filtered"]["wavelet_method"] == "leelab"


def test_apply_wavelet_passes_paper_params_through(session, repo, capture_denoise):
    paper = WaveletParams.paper()
    result = ApplyWavelet(repo, session).execute(
        channel="ch0", filter_level=4, params=paper
    )
    assert capture_denoise[0]["params"] == paper
    assert result.params == paper
    attrs = repo.attrs["phasor/ch0/g_filtered"]
    assert attrs["wavelet_method"] == "paper"
    assert WaveletParams.from_dict(json.loads(attrs["wavelet_params"])) == paper


def test_load_cached_surfaces_wavelet_params(session, repo, capture_denoise):
    custom = WaveletParams.paper().with_overrides(regularize=True)
    ApplyWavelet(repo, session).execute(channel="ch0", filter_level=4, params=custom)
    cached = LoadCachedPhasor(repo, session).execute("ch0")
    assert cached.cached_filter_level == 4
    assert cached.cached_wavelet_params is not None
    assert WaveletParams.from_dict(cached.cached_wavelet_params) == custom
    assert cached.cached_wavelet_params["method"] == "custom"


def test_load_cached_params_none_for_pre_variant_files(session, repo):
    repo.arrays["phasor/ch0/g_filtered"] = repo.arrays["phasor/ch0/g"]
    repo.arrays["phasor/ch0/s_filtered"] = repo.arrays["phasor/ch0/s"]
    repo.attrs["phasor/ch0/g_filtered"] = {"filter_level": 9}
    cached = LoadCachedPhasor(repo, session).execute("ch0")
    assert cached.cached_filter_level == 9
    assert cached.cached_wavelet_params is None


def test_load_cached_accepts_bytes_json_attr(session, repo):
    """h5py may hand back a variable-length string attr as bytes."""
    repo.arrays["phasor/ch0/g_filtered"] = repo.arrays["phasor/ch0/g"]
    repo.arrays["phasor/ch0/s_filtered"] = repo.arrays["phasor/ch0/s"]
    repo.attrs["phasor/ch0/g_filtered"] = {
        "filter_level": 9,
        "wavelet_params": json.dumps(WaveletParams.paper().to_dict()).encode(),
    }
    cached = LoadCachedPhasor(repo, session).execute("ch0")
    assert WaveletParams.from_dict(cached.cached_wavelet_params) == WaveletParams.paper()

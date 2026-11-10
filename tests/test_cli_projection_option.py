"""``--projection`` on the headless commands (z-stack plan U6, KTD6, R11)."""

from __future__ import annotations

import argparse

import numpy as np
import pytest
import tifffile

from percell4.domain.io.projections import PREFERRED_PROJECTION, pick_projection
from percell4.interfaces.cli import batch_export
from percell4.interfaces.cli._projection_option import add_projection_option
from percell4.store import DatasetStore

H = W = 6


def _named(path, projections):
    store = DatasetStore(path)
    store.create(metadata={"channel_names": ["ch0"], "n_channels": 1})
    for name, value in projections.items():
        store.write_projection(name, np.full((H, W), value, np.float32), dims=["H", "W"])
    return path


def _legacy(path, value, z_projection=None):
    meta = {"channel_names": ["ch0"], "n_channels": 1}
    if z_projection:
        meta["z_projection"] = z_projection
    store = DatasetStore(path)
    store.create(metadata=meta)
    store.write_array("intensity", np.full((H, W), value, np.float32), attrs={"dims": ["H", "W"]})
    return path


def _exported(outdir, stem):
    return float(tifffile.imread(outdir / f"{stem}_ch0.tif")[0, 0])


def test_option_defaults_to_not_given():
    parser = argparse.ArgumentParser()
    add_projection_option(parser)
    assert parser.parse_args([]).projection is None
    assert parser.parse_args(["--projection", "mean"]).projection == "mean"
    with pytest.raises(SystemExit):
        parser.parse_args(["--projection", "median"])


def test_cli_and_session_share_the_preferred_projection():
    from percell4.application.session import Session
    from percell4.domain.dataset import DatasetHandle

    session = Session()
    session.set_dataset(DatasetHandle(path="/tmp/x.h5", metadata={
        "projection_names": ["mean", PREFERRED_PROJECTION]}))
    assert session.active_projection == PREFERRED_PROJECTION == pick_projection(["mean", "max"])


def test_ae3_ambiguous_dataset_fails_alone_until_projection_is_named(tmp_path, capsys):
    """Covers AE3."""
    ambiguous = _named(tmp_path / "ms.h5", {"mean": 2.0, "sum": 8.0})
    plain = _named(tmp_path / "mx.h5", {"max": 5.0})
    outdir = tmp_path / "out"

    rc = batch_export.main([str(ambiguous), str(plain), "--output-dir", str(outdir)])

    out = capsys.readouterr().out
    assert rc == 0  # at least one dataset succeeded
    assert "mean, sum" in out
    assert _exported(outdir, "mx") == 5.0
    assert not (outdir / "ms_ch0.tif").exists()

    rc = batch_export.main([str(ambiguous), "--output-dir", str(outdir), "--projection", "mean"])
    assert rc == 0
    assert _exported(outdir, "ms") == 2.0


def test_legacy_datasets_run_without_the_option_as_today(tmp_path):
    named_mean = _legacy(tmp_path / "lm.h5", 3.0, z_projection="mean")
    unnamed = _legacy(tmp_path / "lu.h5", 4.0)
    outdir = tmp_path / "out"
    assert batch_export.main([str(named_mean), str(unnamed), "--output-dir", str(outdir)]) == 0
    assert _exported(outdir, "lm") == 3.0
    assert _exported(outdir, "lu") == 4.0
    # ... and with any --projection, they still read their one image.
    assert batch_export.main(
        [str(unnamed), "--output-dir", str(outdir), "--projection", "sum"]
    ) == 0
    assert _exported(outdir, "lu") == 4.0

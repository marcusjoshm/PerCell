"""percell-batch-add-projection reports each dataset (z-stack plan U9)."""

from __future__ import annotations

from tests.test_application.test_add_projection import _dataset

from percell4.interfaces.cli import batch_add_projection
from percell4.store import DatasetStore


def test_three_datasets_report_added_no_zseries_and_already_exists(tmp_path, capsys):
    ok = tmp_path / "a_ok.h5"
    no_z = tmp_path / "b_noz.h5"
    has = tmp_path / "c_has.h5"
    _dataset(ok, projections=("max",))
    _dataset(no_z, projections=("max",), zseries=False)
    _dataset(has, projections=("max", "mean"))

    rc = batch_add_projection.main([str(tmp_path), "--projection", "mean"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "[added]   a_ok.h5" in out
    assert "b_noz.h5: The z-series was not kept" in out
    assert "c_has.h5: The mean projection is already stored" in out
    assert "1 added, 2 skipped, 0 failed" in out
    assert DatasetStore(ok).list_projections() == ("max", "mean")


def test_nothing_added_exits_1(tmp_path):
    path = tmp_path / "x.h5"
    _dataset(path, projections=("max",), zseries=False)
    assert batch_add_projection.main([str(path), "--projection", "sum"]) == 1

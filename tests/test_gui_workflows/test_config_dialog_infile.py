"""The workflow dialog turns an in-file scheme into one pending entry per source."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from percell4.domain.io import scheme_json
from percell4.domain.io.infile import ImportScheme, ImportSource, SeriesProbe
from percell4.gui.workflows.single_cell import config_dialog as cdlg


def _source(path, series_index=0, channels=(0, 1, 2), name="a", flag=""):
    return ImportSource(
        path=Path(path), series_index=series_index, channel_indices=tuple(channels),
        output_name=name, needs_confirmation=flag,
        series=SeriesProbe(index=series_index, size_c=3, size_z=4),
    )


def test_infile_pending_entries_one_per_importable_source(tmp_path):
    scheme = ImportScheme(
        z_method="mean",
        sources=(
            _source(tmp_path / "f.tif", 0, name="f_s00"),
            _source(tmp_path / "f.tif", 1, channels=(0, 2), name="f_s01"),
            _source(tmp_path / "g.tif", name="g", flag="possible T stored as Z"),
        ),
    )
    cfg = SimpleNamespace(infile_scheme=scheme, output_dir=tmp_path / "out", creation_bin=2)

    pending = cdlg._infile_pending_datasets(cfg)

    assert [p.display_name for p in pending] == ["f_s00", "f_s01"]
    assert pending[1].channel_names == ["ch0", "ch2"]
    assert pending[0].h5_path == tmp_path / "out" / "f_s00.h5"
    plan = pending[1].compress_plan
    assert plan["z_method"] == "mean" and plan["creation_bin"] == 2
    assert scheme_json.source_from_dict(plan["infile_source"]).series_index == 1


def test_two_series_of_one_file_are_not_duplicates(tmp_path):
    scheme = ImportScheme(sources=(
        _source(tmp_path / "f.tif", 0, name="f_s00"),
        _source(tmp_path / "f.tif", 1, name="f_s01"),
    ))
    cfg = SimpleNamespace(infile_scheme=scheme, output_dir=None, creation_bin=1)

    first, second = cdlg._infile_pending_datasets(cfg)

    assert first.dedupe_key() != second.dedupe_key()
    assert first.h5_path == tmp_path / "f_s00.h5"


def test_java_preflight_names_the_reason(monkeypatch, tmp_path):
    scheme = ImportScheme(sources=(_source(tmp_path / "f.tif"),))
    cfg = SimpleNamespace(infile_scheme=scheme, output_dir=None, creation_bin=1)
    pending = cdlg._infile_pending_datasets(cfg)
    monkeypatch.setattr(cdlg, "_java_problem", lambda: "No working Java runtime was found.")

    assert cdlg._infile_preflight(pending) == (
        "In-file datasets need Java and Bio-Formats: No working Java runtime was found. "
        "Open Import Dataset and choose In-file to set them up."
    )


def test_java_preflight_passes_without_infile_entries(monkeypatch):
    monkeypatch.setattr(cdlg, "_java_problem", lambda: "should not be asked")
    legacy = SimpleNamespace(compress_plan={"files": []})
    assert cdlg._infile_preflight([legacy]) is None

"""Tests for the percell-import CLI (in-file scan, scheme file, import)."""

from __future__ import annotations

import json

import numpy as np
import pytest
import tifffile
from tests.fakes.fake_image_reader import FakeImageReader, probe_for

from percell4.adapters.infile_scan import scan_selection
from percell4.domain.errors import JavaUnavailableError
from percell4.domain.io import scheme_json
from percell4.interfaces.cli import import_data as cli
from percell4.store import DatasetStore


def _hyperstack(path, stack):
    """Write a real ImageJ hyperstack (for stage one) matching ``stack`` (T,C,Z,Y,X)."""
    zcyx = np.moveaxis(stack[0], 0, 1)
    tifffile.imwrite(path, zcyx, imagej=True, metadata={"axes": "ZCYX"})
    return path


@pytest.fixture
def folder(tmp_path):
    """Two hyperstacks + one single-plane TIFF, with a fake reader wired in."""
    src = tmp_path / "raw"
    src.mkdir()
    rng = np.random.default_rng(0)
    stacks = {}
    probes = []
    for name in ("a.tif", "b.tif"):
        stack = rng.integers(0, 1000, size=(1, 2, 3, 8, 8)).astype(np.uint16)
        path = _hyperstack(src / name, stack)
        st = path.stat()
        probe = probe_for(path, stack, physical_x_um=0.1, physical_z_um=0.5)
        probes.append(probe.__class__(**{**probe.__dict__,
                                         "size_bytes": st.st_size,
                                         "mtime_ns": st.st_mtime_ns}))
        stacks[(path, 0)] = stack
    tifffile.imwrite(src / "plane.tif", np.zeros((8, 8), dtype=np.uint16))
    return src, probes, stacks


@pytest.fixture
def fake_reader(monkeypatch, folder):
    _src, probes, stacks = folder
    reader = FakeImageReader(probes, arrays=stacks)
    monkeypatch.setattr(cli, "_make_reader", lambda: reader)
    return reader


def test_scan_only_writes_the_same_scheme_as_the_shared_scan(folder, fake_reader, tmp_path):
    src, _probes, _stacks = folder
    out = tmp_path / "s.json"

    rc = cli.main([str(src), "--scan-only", "--scheme-out", str(out)])

    assert rc == 0
    written = scheme_json.loads(out.read_text())
    expected = scan_selection([src], reader_factory=lambda: fake_reader).scheme
    assert scheme_json.to_dict(written) == scheme_json.to_dict(expected)
    assert not list(tmp_path.rglob("*.h5"))


def test_scan_lists_single_plane_files_as_excluded(folder, fake_reader, capsys):
    src, *_ = folder
    assert cli.main([str(src), "--scan-only"]) == 0
    out = capsys.readouterr().out
    assert "plane.tif" in out
    assert "single-plane series" in out


def test_default_run_imports_every_source(folder, fake_reader, tmp_path):
    src, *_ = folder
    outdir = tmp_path / "out"

    rc = cli.main([str(src), "--output-dir", str(outdir)])

    assert rc == 0
    assert sorted(p.name for p in outdir.glob("*.h5")) == ["a.h5", "b.h5"]
    assert DatasetStore(outdir / "a.h5").read_array("intensity").shape == (2, 8, 8)


def test_scheme_replay_imports_only_the_listed_sources(folder, fake_reader, tmp_path):
    src, *_ = folder
    scheme = scan_selection([src], reader_factory=lambda: fake_reader).scheme
    only_b = scheme.__class__(**{**scheme.__dict__, "sources": scheme.sources[1:]})
    path = tmp_path / "s.json"
    path.write_text(scheme_json.dumps(only_b))
    outdir = tmp_path / "out"

    rc = cli.main(["--scheme", str(path), "--output-dir", str(outdir)])

    assert rc == 0
    assert [p.name for p in outdir.glob("*.h5")] == ["b.h5"]


def test_flagged_source_exits_2_and_writes_nothing(folder, fake_reader, tmp_path, capsys):
    src, *_ = folder
    scheme = scan_selection([src], reader_factory=lambda: fake_reader).scheme
    flagged = scheme.sources[0].__class__(
        **{**scheme.sources[0].__dict__, "needs_confirmation": "possible T stored as Z"}
    )
    edited = scheme.__class__(**{**scheme.__dict__, "sources": (flagged, scheme.sources[1])})
    path = tmp_path / "s.json"
    path.write_text(scheme_json.dumps(edited))
    outdir = tmp_path / "out"

    rc = cli.main(["--scheme", str(path), "--output-dir", str(outdir)])

    assert rc == 2
    assert "possible T stored as Z" in capsys.readouterr().err
    assert not outdir.exists() or not list(outdir.glob("*.h5"))


def test_no_java_without_provision_flag_exits_with_the_reason(folder, monkeypatch, capsys):
    src, *_ = folder

    def unavailable():
        raise JavaUnavailableError("No working Java found.")

    def must_not_provision(**_kw):
        raise AssertionError("provisioning must not run without --provision-java")

    monkeypatch.setattr(cli, "_make_reader", unavailable)
    monkeypatch.setattr(cli, "_provision", must_not_provision)

    rc = cli.main([str(src), "--scan-only"])

    assert rc == 1
    assert "No working Java found." in capsys.readouterr().err


def test_provision_flag_provisions_before_scanning(folder, fake_reader, monkeypatch):
    src, *_ = folder
    calls = []
    monkeypatch.setattr(cli, "_provision", lambda **kw: calls.append(kw))
    assert cli.main([str(src), "--scan-only", "--provision-java"]) == 0
    assert calls


def test_existing_output_without_overwrite_is_refused_untouched(folder, fake_reader, tmp_path):
    src, *_ = folder
    outdir = tmp_path / "out"
    outdir.mkdir()
    (outdir / "a.h5").write_bytes(b"keep me")

    rc = cli.main([str(src), "--output-dir", str(outdir)])

    assert rc == 1
    assert (outdir / "a.h5").read_bytes() == b"keep me"
    assert not (outdir / "b.h5").exists()


def test_overwrite_replaces_existing_output(folder, fake_reader, tmp_path):
    src, *_ = folder
    outdir = tmp_path / "out"
    outdir.mkdir()
    (outdir / "a.h5").write_bytes(b"old")

    assert cli.main([str(src), "--output-dir", str(outdir), "--overwrite"]) == 0
    assert DatasetStore(outdir / "a.h5").read_array("intensity").shape == (2, 8, 8)


def test_import_without_output_dir_is_a_usage_error(folder, fake_reader):
    src, *_ = folder
    with pytest.raises(SystemExit) as exc:
        cli.main([str(src)])
    assert exc.value.code == 2


def test_json_output_parses(folder, fake_reader, capsys):
    src, *_ = folder
    assert cli.main([str(src), "--scan-only", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["sources"]) == 2
    assert any(e["reason"].startswith("single-plane") for e in data["excluded"])


def test_nothing_importable_exits_2(tmp_path, monkeypatch):
    tifffile.imwrite(tmp_path / "plane.tif", np.zeros((8, 8), dtype=np.uint16))
    monkeypatch.setattr(cli, "_make_reader", lambda: FakeImageReader())
    assert cli.main([str(tmp_path), "--scan-only"]) == 2


def test_percell_import_is_listed_as_a_batch_tool():
    from percell4.interfaces.cli.catalog import list_batch_tools

    names = {tool.name for tool in list_batch_tools()}
    if "percell-import" not in names:
        pytest.skip("entry points not refreshed; run `pip install -e .`")
    assert "percell-import" in names


# ── --keep / --z-step (z-stack plan U4) ───────────────────────


def test_keep_imports_every_kept_choice(folder, fake_reader, tmp_path, capsys):
    src, *_ = folder
    outdir = tmp_path / "out"

    rc = cli.main([str(src), "--output-dir", str(outdir), "--keep", "max,mean,zseries"])

    assert rc == 0
    assert "Keep: max, mean, z-series" in capsys.readouterr().out
    store = DatasetStore(outdir / "a.h5")
    assert store.list_projections() == ("max", "mean")
    assert store.zseries_shape() == (2, 3, 8, 8)


def test_keep_zseries_alone_makes_a_view_only_dataset(folder, fake_reader, tmp_path):
    src, *_ = folder
    outdir = tmp_path / "out"
    assert cli.main([str(src), "--output-dir", str(outdir), "--keep", "zseries"]) == 0
    store = DatasetStore(outdir / "a.h5")
    assert store.list_projections() == ()
    assert store.has_zseries()


def test_keep_rejects_unknown_values(folder, fake_reader, capsys):
    src, *_ = folder
    with pytest.raises(SystemExit) as exc:
        cli.main([str(src), "--scan-only", "--keep", "max,median"])
    assert exc.value.code == 2
    assert "median" in capsys.readouterr().err


def test_keep_is_written_to_the_scheme_file(folder, fake_reader, tmp_path):
    src, *_ = folder
    out = tmp_path / "s.json"
    cli.main([str(src), "--scan-only", "--keep", "sum,zseries", "--scheme-out", str(out)])
    scheme = scheme_json.loads(out.read_text())
    assert scheme.storage.tokens == ("sum", "zseries")
    assert any("not comparable" in w for w in scheme.warnings) or scheme.z_method == "sum"


def test_z_step_fills_in_a_missing_z_spacing(tmp_path, monkeypatch):
    path = tmp_path / "raw" / "c.tif"
    path.parent.mkdir()
    stack = np.ones((1, 1, 3, 8, 8), dtype=np.uint16)
    _hyperstack(path, stack)
    st = path.stat()
    probe = probe_for(path, stack, physical_z_um=None)
    probe = probe.__class__(**{**probe.__dict__, "size_bytes": st.st_size,
                               "mtime_ns": st.st_mtime_ns})
    reader = FakeImageReader([probe], arrays={(path, 0): stack})
    monkeypatch.setattr(cli, "_make_reader", lambda: reader)
    from dataclasses import replace

    from percell4.domain.io.infile import confirm_source

    scheme = scan_selection([path], reader_factory=lambda: reader).scheme
    scheme = replace(scheme, sources=tuple(confirm_source(s) for s in scheme.sources))
    scheme_path = tmp_path / "s.json"
    scheme_path.write_text(scheme_json.dumps(scheme))
    outdir = tmp_path / "out"
    rc = cli.main(["--scheme", str(scheme_path), "--output-dir", str(outdir), "--z-step", "0.3"])
    assert rc == 0
    assert DatasetStore(outdir / "c.h5").metadata["z_spacing_um"] == pytest.approx(0.3)


# ── OME-Zarr: no Java, scan and scheme replay ─────────────────────────


@pytest.fixture
def no_bioformats(monkeypatch):
    def refuse():
        raise AssertionError("a Zarr-only run must not build the Bio-Formats reader")

    monkeypatch.setattr("percell4.adapters.routing_reader._bioformats_reader", refuse)


def _zarr_folder(tmp_path):
    from tests.fakes.omezarr_fixture import write_store

    data = np.random.default_rng(3).integers(0, 900, (1, 2, 3, 8, 8)).astype(">u2")
    src = tmp_path / "raw"
    src.mkdir()
    write_store(src / "cells.zarr", [data], levels=2, scale=[1, 1, 0.3, 0.1, 0.1])
    return src, data


def test_zarr_folder_imports_without_java(tmp_path, no_bioformats):
    src, data = _zarr_folder(tmp_path)
    outdir = tmp_path / "out"
    rc = cli.main([str(src), "--output-dir", str(outdir), "--keep", "max,zseries"])
    assert rc == 0
    store = DatasetStore(outdir / "cells.h5")
    assert store.zseries_shape() == (2, 3, 8, 8)
    np.testing.assert_array_equal(store.read_zseries_plane(0, 1, 2), data[0, 1, 2])


def test_zarr_scheme_replays_through_the_zarr_reader(tmp_path, no_bioformats):
    src, data = _zarr_folder(tmp_path)
    scheme_path = tmp_path / "s.json"
    assert cli.main([str(src), "--scan-only", "--scheme-out", str(scheme_path)]) == 0
    outdir = tmp_path / "out"
    assert cli.main(["--scheme", str(scheme_path), "--output-dir", str(outdir)]) == 0
    got = DatasetStore(outdir / "cells.h5").read_array("projections/max")
    np.testing.assert_array_equal(got, data[0].max(axis=1).astype(np.float32))

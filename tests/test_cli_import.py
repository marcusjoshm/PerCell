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

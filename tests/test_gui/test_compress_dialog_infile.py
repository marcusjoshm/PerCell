"""The import dialog's In-file mode: suggestion, review table and import routing.

The fake reader stands in for Bio-Formats and the Java gate is injected, so
none of this starts a JVM.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import tifffile
from tests.fakes.fake_image_reader import FakeImageReader, probe_for

from percell4.domain.io.infile import REASON_MULTI_PLANE, REASON_SINGLE_PLANE
from percell4.gui import compress_dialog as cd
from percell4.store import DatasetStore


def _hyperstack(path: Path, stack: np.ndarray) -> Path:
    """Write an ImageJ ZCYX file for stage one, matching ``stack`` (T,C,Z,Y,X)."""
    tifffile.imwrite(path, np.moveaxis(stack[0], 0, 1), imagej=True,
                     metadata={"axes": "ZCYX"})
    return path


def _stack(c=3, z=4, seed=0):
    rng = np.random.default_rng(seed)
    return rng.integers(0, 1000, size=(1, c, z, 8, 8)).astype(np.uint16)


def _fake_for(paths_and_stacks, **probe_kw):
    probes, arrays = [], {}
    for path, stack in paths_and_stacks:
        st = path.stat()
        probe = probe_for(path, stack, **{"physical_z_um": 0.5, **probe_kw})
        probes.append(probe.__class__(**{**probe.__dict__, "size_bytes": st.st_size,
                                         "mtime_ns": st.st_mtime_ns}))
        arrays[(path, 0)] = stack
    return FakeImageReader(probes, arrays=arrays)


@pytest.fixture
def make_dialog(qtbot):
    def build(reader=None, java_ready=True, java_setup=False):
        dlg = cd.CompressDialog()
        qtbot.addWidget(dlg)
        dlg._reader_factory = (lambda: reader) if reader is not None else _no_reader
        dlg._java_ready = lambda: java_ready
        dlg._java_setup = lambda _parent: java_setup
        return dlg

    return build


def _no_reader():
    raise AssertionError("the reader must not be built here")


def _select_files(dlg, monkeypatch, paths):
    monkeypatch.setattr(cd, "open_file_names", lambda *a, **k: ([str(p) for p in paths], ""))
    dlg._on_browse_files()


def _select_dir(dlg, directory):
    dlg._source_edit.setText(str(directory))
    dlg._set_selection([directory])


def test_three_stacks_selected_as_files_preselect_infile(tmp_path, make_dialog, monkeypatch):
    pairs = [(_hyperstack(tmp_path / f"s{i}.tif", _stack(seed=i)), _stack(seed=i))
             for i in range(3)]
    dlg = make_dialog(reader=_fake_for(pairs))

    _select_files(dlg, monkeypatch, [p for p, _ in pairs])

    assert dlg._discovery_combo.currentIndex() == cd._INFILE_INDEX
    assert "Suggested: In-file" in dlg._suggestion_label.text()
    assert len(dlg._review.importable_sources()) == 3
    assert dlg._btn_compress.isEnabled()
    assert dlg._infile_group.isVisibleTo(dlg)
    assert not dlg._ds_group.isVisibleTo(dlg)
    assert not dlg._ch_group.isVisibleTo(dlg)


def test_single_plane_directory_preselects_flat_and_never_builds_the_reader(
    tmp_path, make_dialog
):
    for i in range(3):
        tifffile.imwrite(tmp_path / f"img_ch{i:02d}.tif", np.zeros((8, 8), dtype=np.uint16))
    dlg = make_dialog()  # the default reader factory raises if called

    _select_dir(dlg, tmp_path)

    assert dlg._discovery_combo.currentIndex() == cd._MODE_INDEX[cd.DiscoveryMode.FLAT]
    assert dlg._datasets


def test_mixed_directory_suggests_flat_then_infile_on_switch(tmp_path, make_dialog):
    """Covers AE2."""
    pairs = [(_hyperstack(tmp_path / f"stack{i}.tif", _stack(seed=i)), _stack(seed=i))
             for i in range(2)]
    for i in range(6):
        tifffile.imwrite(tmp_path / f"img_ch{i:02d}.tif", np.zeros((8, 8), dtype=np.uint16))
    (tmp_path / "notes.docx").write_bytes(b"x")
    (tmp_path / "._img_ch00.tif").write_bytes(b"x")
    reader = _fake_for(pairs)
    java_checks = []
    dlg = make_dialog(reader=reader)
    dlg._java_ready = lambda: java_checks.append(1) or True

    _select_dir(dlg, tmp_path)

    assert dlg._discovery_combo.currentIndex() == cd._MODE_INDEX[cd.DiscoveryMode.FLAT]
    assert reader.probe_calls == [] and java_checks == []
    assert "multi-plane" in dlg._summary_label.text()
    discovered = {f.path.name for ds in dlg._datasets for f in ds.files}
    assert "stack0.tif" not in discovered and "stack1.tif" not in discovered

    dlg._discovery_combo.setCurrentIndex(cd._INFILE_INDEX)

    assert len(reader.probe_calls) == 1
    scheme = dlg._review.scheme()
    assert [s.path.name for s in scheme.sources] == ["stack0.tif", "stack1.tif"]
    reasons = {e.path.name: e.reason for e in scheme.excluded}
    assert reasons["img_ch00.tif"] == REASON_SINGLE_PLANE
    assert reasons["notes.docx"] == "format not recognised"
    assert "._img_ch00.tif" not in reasons


def test_legacy_mode_excludes_multiplane_files_with_reason(tmp_path, make_dialog):
    _hyperstack(tmp_path / "stack.tif", _stack())
    tifffile.imwrite(tmp_path / "img_ch00.tif", np.zeros((8, 8), dtype=np.uint16))
    dlg = make_dialog()
    dlg._source_edit.setText(str(tmp_path))
    dlg._discovery_combo.setCurrentIndex(cd._MODE_INDEX[cd.DiscoveryMode.FLAT])

    assert [p.name for p in dlg._legacy_excluded] == ["stack.tif"]
    assert "import with In-file mode" in dlg._summary_label.text()
    assert REASON_MULTI_PLANE.endswith("import with In-file mode")


def test_flagged_row_is_unchecked_until_axes_are_set(tmp_path, make_dialog, monkeypatch):
    """Covers AE3 / AE7: a flagged row imports only after the user decides."""
    stack = _stack(c=1, z=6)
    path = _hyperstack(tmp_path / "ts.tif", stack)
    dlg = make_dialog(reader=_fake_for([(path, stack)], physical_z_um=None))
    _select_files(dlg, monkeypatch, [path])

    review = dlg._review
    assert not review.include_boxes[0].isChecked()
    assert not review.include_boxes[0].isEnabled()
    assert "possible T stored as Z" in review.status_text(0)
    assert not dlg._btn_compress.isEnabled()

    review.axes_combos[0].setCurrentText("Swap Z and T")  # signal path

    source = review.scheme().sources[0]
    assert source.effective.size_t == 6 and source.effective.size_z == 1
    assert review.include_boxes[0].isChecked()
    assert review.status_text(0) == "Ready"
    assert dlg._btn_compress.isEnabled()


def test_confirm_button_accepts_the_axes_as_read(tmp_path, make_dialog, monkeypatch):
    stack = _stack(c=1, z=6)
    path = _hyperstack(tmp_path / "zs.tif", stack)
    dlg = make_dialog(reader=_fake_for([(path, stack)], physical_z_um=None))
    _select_files(dlg, monkeypatch, [path])

    dlg._review.confirm_buttons[0].click()

    assert dlg._review.scheme().sources[0].needs_confirmation == ""
    assert dlg._btn_compress.isEnabled()


def test_unchecking_a_channel_narrows_the_source(tmp_path, make_dialog, monkeypatch):
    stack = _stack(c=3)
    path = _hyperstack(tmp_path / "a.tif", stack)
    dlg = make_dialog(reader=_fake_for([(path, stack)]))
    _select_files(dlg, monkeypatch, [path])

    dlg._review.channel_boxes[0][1].setChecked(False)

    assert dlg._review.scheme().sources[0].channel_indices == (0, 2)
    assert dlg.compress_config.infile_scheme.sources[0].channel_indices == (0, 2)


def test_no_checked_channel_disables_compress(tmp_path, make_dialog, monkeypatch):
    stack = _stack(c=2)
    path = _hyperstack(tmp_path / "a.tif", stack)
    dlg = make_dialog(reader=_fake_for([(path, stack)]))
    _select_files(dlg, monkeypatch, [path])

    for box in dlg._review.channel_boxes[0]:
        box.setChecked(False)

    assert not dlg._btn_compress.isEnabled()


def test_declining_java_marks_rows_needs_java(tmp_path, make_dialog, monkeypatch):
    """Covers AE4."""
    stack = _stack()
    path = _hyperstack(tmp_path / "a.tif", stack)
    dlg = make_dialog(java_ready=False, java_setup=False)

    _select_files(dlg, monkeypatch, [path])

    assert dlg._review.status_text(0) == "needs Java"
    assert not dlg._btn_compress.isEnabled()


def test_changing_the_selection_mid_probe_discards_the_stale_result(
    tmp_path, make_dialog, monkeypatch
):
    first = _hyperstack(tmp_path / "first.tif", _stack(seed=1))
    second = _hyperstack(tmp_path / "second.tif", _stack(seed=2))
    reader = _fake_for([(first, _stack(seed=1)), (second, _stack(seed=2))])
    dlg = make_dialog(reader=reader)
    original_probe = reader.probe
    started = []

    def probe(paths, on_file=None, is_cancelled=None):
        if not started:
            started.append(1)
            dlg._discovery_generation += 1  # a newer selection arrived meanwhile
        return original_probe(paths, on_file, is_cancelled)

    reader.probe = probe
    _select_files(dlg, monkeypatch, [first])

    assert dlg._review.scheme().sources == ()


def test_keep_offers_the_projections_and_the_zseries(make_dialog):
    dlg = make_dialog()
    form = dlg._storage_form
    assert list(form.projection_boxes) == ["max", "mean", "sum"]
    assert [b.isChecked() for b in form.projection_boxes.values()] == [True, False, False]
    assert not form.zseries_box.isChecked()


def test_config_carries_the_storage_and_sum_warning(tmp_path, make_dialog, monkeypatch):
    pairs = [(_hyperstack(tmp_path / f"s{i}.tif", _stack(z=z, seed=i)), _stack(z=z, seed=i))
             for i, z in enumerate((3, 5))]
    dlg = make_dialog(reader=_fake_for(pairs))
    _select_files(dlg, monkeypatch, [p for p, _ in pairs])

    dlg._storage_form.projection_boxes["sum"].setChecked(True)

    scheme = dlg.compress_config.infile_scheme
    assert scheme.storage.projections == ("max", "sum")
    assert scheme.z_method == "sum"
    assert any("summed intensities" in w for w in scheme.warnings)
    assert dlg.compress_config.datasets == []


# ── main window routing ───────────────────────────────────────────────


def test_run_infile_import_writes_each_source_and_cancel_keeps_earlier_ones(
    tmp_path, qtbot, make_dialog, monkeypatch
):
    """Covers AE5: cancelling during the third file keeps the first two."""
    from percell4.domain.io.models import CompressConfig
    from percell4.interfaces.gui import main_window as mw

    pairs = [(_hyperstack(tmp_path / f"s{i}.tif", _stack(c=2, seed=i)), _stack(c=2, seed=i))
             for i in range(3)]
    reader = _fake_for(pairs)
    dlg = make_dialog(reader=reader)
    _select_files(dlg, monkeypatch, [p for p, _ in pairs])
    outdir = tmp_path / "out"
    outdir.mkdir()
    config = dlg.compress_config
    config = CompressConfig(**{**config.__dict__, "output_dir": outdir})

    class FakeProgress:
        def __init__(self):
            self.value = 0

        def setMinimumDuration(self, _ms): ...
        def setLabelText(self, text): self.label = text
        def setValue(self, v): self.value = v
        def close(self): ...

        def wasCanceled(self):
            # 2 channels per file: cancel once the third file's first plane is read.
            return self.value >= 5

    monkeypatch.setattr(mw, "progress_dialog", lambda *a, **k: FakeProgress())
    shown = []
    monkeypatch.setattr(mw, "message_box", lambda *a, **k: shown.append(a))

    class Host:
        def statusBar(self):
            class Bar:
                def showMessage(self, msg): Host.message = msg
            return Bar()

    mw.LauncherWindow._run_infile_import(Host(), config, reader=reader)

    assert sorted(p.name for p in outdir.glob("*.h5")) == ["s0.h5", "s1.h5"]
    assert not list(outdir.glob("*.tmp"))
    assert "cancelled" in Host.message
    assert DatasetStore(outdir / "s0.h5").read_array("intensity").shape == (2, 8, 8)

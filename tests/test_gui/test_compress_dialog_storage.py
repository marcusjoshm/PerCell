"""The import dialog's storage choice: Keep checkboxes, size estimate, Z step (plan U4)."""

from __future__ import annotations

import numpy as np
import pytest
import tifffile
from tests.test_gui.test_compress_dialog_infile import (
    _fake_for,
    _hyperstack,
    _select_files,
    _stack,
)

from percell4.gui import compress_dialog as cd


@pytest.fixture
def make_dialog(qtbot):
    def build(reader=None):
        dlg = cd.CompressDialog()
        qtbot.addWidget(dlg)
        dlg._reader_factory = lambda: reader
        dlg._java_ready = lambda: True
        dlg._java_setup = lambda _parent: True
        return dlg

    return build


def _token_folder(tmp_path, n_z=3, imagej_spacing=None, n_ch=2):
    src = tmp_path / "raw"
    src.mkdir()
    for ch in range(n_ch):
        for z in range(n_z):
            kwargs = {}
            if imagej_spacing is not None:
                kwargs = {"imagej": True, "metadata": {"spacing": imagej_spacing}}
            tifffile.imwrite(
                str(src / f"img_z{z:02d}_ch{ch:02d}.tif"),
                np.zeros((16, 20), np.uint16), **kwargs,
            )
    return src


def _flat(dlg, src):
    dlg._discovery_combo.setCurrentIndex(1)  # Flat
    dlg._source_edit.setText(str(src))
    dlg._run_discovery()


def test_opens_with_only_max_and_nothing_checked_disables_import(tmp_path, make_dialog):
    dlg = make_dialog()
    _flat(dlg, _token_folder(tmp_path))
    form = dlg._storage_form
    assert form.isVisibleTo(dlg)
    assert form.choice().tokens == ("max",)
    assert dlg._btn_compress.isEnabled()
    form.projection_boxes["max"].setChecked(False)
    assert form.choice() is None
    assert not dlg._btn_compress.isEnabled()
    form.zseries_box.setChecked(True)
    assert dlg._btn_compress.isEnabled()


def test_estimate_grows_by_one_projection_when_mean_is_added(tmp_path, make_dialog):
    dlg = make_dialog()
    _flat(dlg, _token_folder(tmp_path, n_z=3))
    form = dlg._storage_form
    form.zseries_box.setChecked(True)
    z1, p1 = dlg.storage_estimate()
    assert z1 == 2 * 3 * 16 * 20 * 4  # C x Z x H x W float32
    assert "z-series" in form.estimate_label.text()
    form.projection_boxes["mean"].setChecked(True)
    z2, p2 = dlg.storage_estimate()
    assert z2 == z1
    assert p2 - p1 == 2 * 16 * 20 * 4  # one more projection, not another stack


def test_single_plane_selection_hides_the_choice(tmp_path, make_dialog):
    """Covers AE7."""
    dlg = make_dialog()
    _flat(dlg, _token_folder(tmp_path, n_z=1))
    assert not dlg._storage_form.isVisibleTo(dlg)
    config = dlg.compress_config
    assert config.storage is None
    assert config.z_project_method == "mip"


def test_z_step_is_editable_without_imagej_spacing(tmp_path, make_dialog):
    dlg = make_dialog()
    _flat(dlg, _token_folder(tmp_path))
    form = dlg._storage_form
    assert form._z_step_row.isVisibleTo(dlg)
    assert not form.z_step_spin.isReadOnly()
    form.z_step_spin.setValue(0.25)
    assert dlg.compress_config.z_step_um == pytest.approx(0.25)


def test_z_step_shows_the_detected_spacing_read_only(tmp_path, make_dialog):
    dlg = make_dialog()
    _flat(dlg, _token_folder(tmp_path, imagej_spacing=0.4))
    form = dlg._storage_form
    assert form.z_step_spin.isReadOnly()
    assert form.z_step_spin.value() == pytest.approx(0.4)
    assert "auto-detected" in form.z_step_note.text()


def test_z_step_is_hidden_in_infile_mode(tmp_path, make_dialog, monkeypatch):
    stack = _stack(c=2, z=4)
    path = _hyperstack(tmp_path / "a.tif", stack)
    dlg = make_dialog(reader=_fake_for([(path, stack)]))
    _select_files(dlg, monkeypatch, [path])
    assert dlg._storage_form.isVisibleTo(dlg)
    assert not dlg._storage_form._z_step_row.isVisibleTo(dlg)


def test_review_table_lists_what_each_file_keeps(tmp_path, make_dialog, monkeypatch):
    stack = _stack(c=2, z=4)
    path = _hyperstack(tmp_path / "a.tif", stack)
    dlg = make_dialog(reader=_fake_for([(path, stack)]))
    _select_files(dlg, monkeypatch, [path])
    form = dlg._storage_form
    form.projection_boxes["mean"].setChecked(True)
    form.zseries_box.setChecked(True)
    assert dlg._review.cell_text(0, "Keep") == "max, mean, z-series"
    config = dlg.compress_config
    assert config.infile_scheme.storage.tokens == ("max", "mean", "zseries")
    z, p = dlg.storage_estimate()
    assert z == 2 * 4 * 8 * 8 * 4 and p == 2 * 2 * 8 * 8 * 4

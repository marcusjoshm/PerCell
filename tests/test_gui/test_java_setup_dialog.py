"""The Java setup dialog: consent, download states, and an existing Java home."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from percell4.gui import java_setup_dialog as jsd


def _env():
    item = SimpleNamespace(
        name="Bio-Formats 8.5.0 (bioformats_package.jar, GPL)",
        url="https://downloads.openmicroscopy.org/bio-formats/8.5.0/artifacts/bioformats_package.jar",
        size_bytes=53_843_906,
        destination=Path("/cache/bioformats/8.5.0/bioformats_package.jar"),
    )
    return SimpleNamespace(pending_downloads=(item,), summary="")


def _dialog(qtbot, **kw):
    dlg = jsd.JavaSetupDialog(describe=_env, **kw)
    qtbot.addWidget(dlg)
    return dlg


def test_consent_lists_source_size_and_destination_before_any_download(qtbot):
    calls = []
    dlg = _dialog(qtbot, provision=lambda *a: calls.append(a))
    text = dlg._details.text()
    assert "downloads.openmicroscopy.org" in text
    assert "54 MB" in text
    assert "/cache/bioformats" in text
    assert dlg.state == jsd.STATE_CONSENT
    assert calls == []


def test_successful_download_accepts(qtbot):
    def provision(progress, cancel):
        progress("Downloading", 5, 10)
        assert not cancel()

    dlg = _dialog(qtbot, provision=provision)
    dlg.download_button.click()
    assert dlg.state == jsd.STATE_READY
    assert dlg.result() == jsd.QDialog.Accepted


def test_failed_download_shows_reason_and_retry(qtbot):
    def provision(_progress, _cancel):
        raise OSError("network unreachable")

    dlg = _dialog(qtbot, provision=provision)
    dlg.download_button.click()
    assert dlg.state == jsd.STATE_FAILED
    assert "network unreachable" in dlg.message.text()
    assert dlg.download_button.text() == "Retry"
    assert dlg.download_button.isEnabled()


def test_cancel_during_download_ends_failed_with_cancelled(qtbot):
    def provision(_progress, cancel):
        dlg.cancel_button.click()
        if cancel():
            raise RuntimeError("stopped")

    dlg = _dialog(qtbot, provision=provision)
    dlg.download_button.click()
    assert dlg.state == jsd.STATE_FAILED
    assert dlg.failure_reason == "cancelled"


def test_existing_java_that_fails_the_probe_is_not_saved(qtbot, tmp_path):
    saved = []
    dlg = _dialog(
        qtbot,
        pick_directory=lambda _d: str(tmp_path),
        probe_java_home=lambda home: (False, "no bin/java inside"),
        save_java_home=saved.append,
    )
    dlg.existing_button.click()
    assert saved == []
    assert "no bin/java inside" in dlg.message.text()
    assert dlg.state == jsd.STATE_CONSENT


def test_existing_java_that_works_is_saved_and_accepts(qtbot, tmp_path):
    saved = []
    dlg = _dialog(
        qtbot,
        pick_directory=lambda _d: str(tmp_path),
        probe_java_home=lambda home: (True, "Using Java 21"),
        save_java_home=saved.append,
    )
    dlg.existing_button.click()
    assert saved == [str(tmp_path)]
    assert dlg.state == jsd.STATE_READY

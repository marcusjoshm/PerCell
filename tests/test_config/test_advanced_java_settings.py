"""Tests for the Java / Bio-Formats fields of the advanced-settings store.

The store's contract is "nothing here may raise": a wrong-typed or corrupt
value falls back to the default for that one field. The consent field carries
the pinned artifact versions it was given for, so a later pin bump asks again.
"""

from __future__ import annotations

import json

import pytest

from percell4.adapters import java_runtime
from percell4.config import advanced


def _write_raw(payload: str) -> None:
    advanced.config_path().parent.mkdir(parents=True, exist_ok=True)
    advanced.config_path().write_text(payload, encoding="utf-8")


def test_java_fields_default_to_unset():
    settings = advanced.load_advanced_settings()
    assert settings.java_home is None
    assert settings.bioformats_jar is None
    assert settings.java_max_heap_mb is None
    assert settings.java_download_consent is None


def test_java_fields_round_trip():
    advanced.save_advanced_settings(
        advanced.AdvancedSettings(
            java_home="/opt/jre",
            bioformats_jar="/opt/bf/bioformats_package.jar",
            java_max_heap_mb=4096,
            java_download_consent="token",
        )
    )
    settings = advanced.load_advanced_settings()
    assert settings.java_home == "/opt/jre"
    assert settings.bioformats_jar == "/opt/bf/bioformats_package.jar"
    assert settings.java_max_heap_mb == 4096
    assert settings.java_download_consent == "token"


def test_blank_paths_are_stored_as_unset():
    advanced.save_advanced_settings(
        advanced.AdvancedSettings(java_home="  ", bioformats_jar="")
    )
    settings = advanced.load_advanced_settings()
    assert settings.java_home is None
    assert settings.bioformats_jar is None


@pytest.mark.parametrize(
    "raw",
    [
        {"java_home": 5},
        {"bioformats_jar": ["a"]},
        {"java_max_heap_mb": "lots"},
        {"java_max_heap_mb": -1},
        {"java_max_heap_mb": 0},
        {"java_max_heap_mb": True},
        {"java_download_consent": {"v": 1}},
    ],
)
def test_wrong_typed_java_value_falls_back_to_default(raw):
    _write_raw(json.dumps({"cellpose_device": "xpu", **raw}))
    settings = advanced.load_advanced_settings()
    key = next(iter(raw))
    assert getattr(settings, key) is None
    # One bad field must not take the others down with it.
    assert settings.cellpose_device == "xpu"


def test_corrupt_json_returns_defaults_for_java_fields():
    _write_raw("{definitely not json")
    settings = advanced.load_advanced_settings()
    assert settings.java_home is None
    assert settings.java_download_consent is None


def test_update_keeps_fields_it_was_not_given():
    """Saving the Cellpose device from the Advanced panel must not wipe the
    Java settings, and recording consent must not wipe the device."""
    advanced.save_advanced_settings(
        advanced.AdvancedSettings(java_home="/opt/jre", java_max_heap_mb=2048)
    )
    advanced.update_advanced_settings(cellpose_device="xpu")
    settings = advanced.load_advanced_settings()
    assert settings.cellpose_device == "xpu"
    assert settings.java_home == "/opt/jre"
    assert settings.java_max_heap_mb == 2048


def test_save_preserves_unknown_keys():
    _write_raw(json.dumps({"from_the_future": 1}))
    advanced.update_advanced_settings(java_home="/opt/jre")
    raw = json.loads(advanced.config_path().read_text(encoding="utf-8"))
    assert raw["from_the_future"] == 1
    assert raw["java_home"] == "/opt/jre"


# --- consent is tied to the pinned artifact versions --------------------------


def test_no_consent_recorded_means_no_consent():
    assert not java_runtime.has_download_consent()


def test_recorded_consent_counts_for_current_pins():
    java_runtime.record_download_consent()
    assert java_runtime.has_download_consent()
    stored = advanced.load_advanced_settings().java_download_consent
    assert stored == java_runtime.CONSENT_TOKEN
    # The token names what was agreed to, so a reader of the file can tell.
    assert java_runtime.BIOFORMATS_VERSION in stored
    assert java_runtime.JRE_VERSION in stored


def test_consent_for_an_older_pinned_version_is_not_consent_now():
    older = java_runtime.CONSENT_TOKEN.replace(
        java_runtime.BIOFORMATS_VERSION, "8.3.1"
    )
    assert older != java_runtime.CONSENT_TOKEN
    advanced.update_advanced_settings(java_download_consent=older)
    assert not java_runtime.has_download_consent()


def test_recording_consent_keeps_other_settings():
    advanced.update_advanced_settings(cellpose_device="mps", java_home="/opt/jre")
    java_runtime.record_download_consent()
    settings = advanced.load_advanced_settings()
    assert settings.cellpose_device == "mps"
    assert settings.java_home == "/opt/jre"

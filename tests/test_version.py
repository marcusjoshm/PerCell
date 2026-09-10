"""``percell4.__version__`` comes from one resolver and never raises.

The version is derived from git tags by setuptools-scm and written to
``percell4/_version.py`` at build or install time. Every consumer (HDF5
provenance, ``run_config.json``) reads ``percell4.__version__``; ``run_folder``
must delegate to it rather than keep its own lookup.
"""

from __future__ import annotations

import importlib.metadata
import sys

from packaging.version import Version

import percell4
from percell4.application.analysis import run_folder


def test_version_is_pep440_and_matches_installed_metadata():
    assert Version(percell4.__version__)  # parses as a PEP 440 version
    assert percell4.__version__ == importlib.metadata.version("percell4")


def test_run_folder_version_delegates_to_package():
    assert run_folder.percell4_version() == percell4.__version__


def test_resolver_falls_back_to_metadata_when_version_file_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "percell4._version", None)  # import raises ImportError
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "9.9.9")
    assert percell4._resolve_version() == "9.9.9"


def test_resolver_returns_placeholder_when_nothing_is_available(monkeypatch):
    monkeypatch.setitem(sys.modules, "percell4._version", None)

    def _missing(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", _missing)
    assert percell4._resolve_version() == "0.0.0+unknown"

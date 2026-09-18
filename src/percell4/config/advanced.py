"""Persistent store for expert-only configuration.

Backs the launcher's Advanced panel. Holds the Cellpose device override and
the Java / Bio-Formats settings used by in-file import (see
``percell4.adapters.java_runtime``).

**Why not QSettings.** ``percell4.gui.settings.app_settings`` is the
established preference store, but it imports ``qtpy``, and the batch CLI has
to read the override from a terminal run with no GUI toolkit loaded. A plain
JSON file under the user's config directory is readable by both surfaces,
which is what makes the override apply to a headless run rather than only to
one launched from the Batch Tools window.

**Nothing here may raise.** This is an opt-in convenience file; a missing,
malformed, unreadable, or hand-mangled one falls back to defaults. An unused
feature must never be able to break the default path.

**The path is resolved in exactly one function.** ``config_path()`` is the
only place that knows where the file lives, so the test suite can redirect
every caller at once. ``tests/test_config/test_advanced_settings_isolation_compliance.py``
fails the build if a second call site appears -- the same structural guard,
and the same reasoning, as the QSettings factory.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Directory name used under the platform's user-config location. Deliberately
#: not the QSettings org/app pair -- that lives in the Qt-bound settings module
#: and importing it here would defeat the point of this file.
_APP_DIR_NAME = "PerCell4"

#: The settings file's basename. Public so tests can assert against it without
#: re-spelling the literal and tripping the compliance guard.
CONFIG_FILENAME = "advanced_settings.json"

#: Redirect hook. ``None`` means "use the real per-user location".
#:
#: Consulted on every call rather than monkeypatched onto the functions,
#: because a call site may bind either way -- ``from ... import
#: save_advanced_settings`` captures the function object at import time, so
#: patching a module attribute would miss it. That exact binding-style gap is
#: what let three test modules believe they were sandboxed while writing to a
#: researcher's real preferences.
_redirect: Callable[[], Path] | None = None

#: String fields where a cleared text box ("" or "   ") means "unset".
_BLANK_MEANS_UNSET = (
    "cellpose_device",
    "java_home",
    "bioformats_jar",
    "java_download_consent",
)


@dataclass(frozen=True, slots=True)
class AdvancedSettings:
    """Expert-only configuration. Every field defaults to "behave as before"."""

    #: Explicit torch device for Cellpose (``xpu``, ``cuda:1``). ``None`` means
    #: auto-detect, which is what every unconfigured install does.
    cellpose_device: str | None = None

    #: A Java home directory to use before ``JAVA_HOME`` and the PerCell cache.
    java_home: str | None = None

    #: A ``bioformats_package.jar`` to use instead of the cached, pinned one.
    bioformats_jar: str | None = None

    #: Maximum JVM heap for the Bio-Formats reader process, in MiB. ``None``
    #: leaves the reader's own default in place.
    java_max_heap_mb: int | None = None

    #: The pinned-artifact token the user agreed to download. Compared against
    #: ``java_runtime.CONSENT_TOKEN``, so a later version bump asks again.
    java_download_consent: str | None = None

    def __post_init__(self) -> None:
        # A cleared text field arrives as "" or "   ". Both mean unset; storing
        # either verbatim would make every later run probe a device named
        # empty-string (or a Java home at "") and report a fallback nobody
        # asked for.
        for name in _BLANK_MEANS_UNSET:
            value = getattr(self, name)
            if isinstance(value, str):
                cleaned = value.strip()
                object.__setattr__(self, name, cleaned or None)


def _default_config_dir() -> Path:
    """Locate the platform's user-config directory for this application."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_CONFIG_HOME")
        root = Path(base) if base else Path.home() / ".config"
    return root / _APP_DIR_NAME


def config_path() -> Path:
    """Return the settings file's location.

    The single source of truth for where advanced settings live. Redirected
    wholesale by :func:`redirect_to` so the test suite never touches the real
    file.
    """
    if _redirect is not None:
        return _redirect() / CONFIG_FILENAME
    return _default_config_dir() / CONFIG_FILENAME


def redirect_to(directory: Path | str) -> None:
    """Point every subsequent :func:`config_path` call at ``directory``."""
    root = Path(directory)

    def _factory() -> Path:
        return root

    global _redirect
    _redirect = _factory


def clear_redirect() -> None:
    """Restore the real per-user location. Inverse of :func:`redirect_to`."""
    global _redirect
    _redirect = None


def _read_raw() -> dict[str, Any]:
    """Read the file as a plain dict, or return empty on any problem."""
    path = config_path()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # Missing is the overwhelmingly common case and not worth a log line;
        # anything else is worth knowing about without being fatal.
        if not isinstance(exc, FileNotFoundError):
            logger.warning("advanced settings unreadable at %s: %s", path, exc)
        return {}

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("advanced settings are not valid JSON at %s: %s", path, exc)
        return {}

    if not isinstance(data, dict):
        logger.warning(
            "advanced settings at %s are %s, expected an object; ignoring.",
            path,
            type(data).__name__,
        )
        return {}
    return data


def _optional_str(raw: dict[str, Any], key: str) -> str | None:
    """Read ``key`` as an optional string; anything else is ignored with a log."""
    value = raw.get(key)
    if value is not None and not isinstance(value, str):
        logger.warning(
            "advanced settings: %s is %s, expected a string; ignoring.",
            key,
            type(value).__name__,
        )
        return None
    return value


def _optional_positive_int(raw: dict[str, Any], key: str) -> int | None:
    """Read ``key`` as an optional positive int. ``True`` is not a heap size."""
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        logger.warning(
            "advanced settings: %s is %r, expected a positive integer; ignoring.",
            key,
            value,
        )
        return None
    return value


def load_advanced_settings() -> AdvancedSettings:
    """Read the stored settings, falling back to defaults on any problem.

    Each field falls back on its own: one hand-mangled value must not take the
    others down with it.
    """
    raw = _read_raw()
    return AdvancedSettings(
        cellpose_device=_optional_str(raw, "cellpose_device"),
        java_home=_optional_str(raw, "java_home"),
        bioformats_jar=_optional_str(raw, "bioformats_jar"),
        java_max_heap_mb=_optional_positive_int(raw, "java_max_heap_mb"),
        java_download_consent=_optional_str(raw, "java_download_consent"),
    )


def save_advanced_settings(settings: AdvancedSettings) -> None:
    """Write ``settings``, preserving any keys this build does not know about.

    The merge matters for forward compatibility: a newer build may store
    settings this one has never heard of, and loading then saving on the older
    build must not silently delete them.
    """
    path = config_path()
    raw = _read_raw()
    raw["cellpose_device"] = settings.cellpose_device
    raw["java_home"] = settings.java_home
    raw["bioformats_jar"] = settings.bioformats_jar
    raw["java_max_heap_mb"] = settings.java_max_heap_mb
    raw["java_download_consent"] = settings.java_download_consent

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        # A read-only home directory should degrade to "setting doesn't stick",
        # not take down the panel that wrote it.
        logger.warning("could not write advanced settings to %s: %s", path, exc)


def update_advanced_settings(**changes: Any) -> AdvancedSettings:
    """Change the named fields, keep every other stored field, and save.

    The safe way for one surface to write its own setting: building a fresh
    :class:`AdvancedSettings` with one field set and saving it would reset
    every other field to its default. Returns what was saved.
    """
    settings = replace(load_advanced_settings(), **changes)
    save_advanced_settings(settings)
    return settings


def load_cellpose_device() -> str | None:
    """Convenience reader for the one setting the segmentation path needs."""
    return load_advanced_settings().cellpose_device

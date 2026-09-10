"""PerCell4 package root.

``__version__`` is derived from git tags by setuptools-scm (see
``[tool.setuptools_scm]`` in pyproject.toml). It is read from the generated
``_version.py`` first (present in any checkout that has been built or installed,
and inside the PyInstaller bundle), then from the installed distribution
metadata, and finally falls back to a placeholder so that importing never fails.
Everything that needs the version -- HDF5 provenance, run_config.json, the GUI --
reads this one attribute.
"""

from __future__ import annotations


def _resolve_version() -> str:
    try:
        from ._version import version as file_version
    except ImportError:
        pass
    else:
        return str(file_version)
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("percell4")
    except PackageNotFoundError:
        return "0.0.0+unknown"
    except Exception:  # pragma: no cover - the version must never raise at import
        return "0.0.0+unknown"


__version__ = _resolve_version()

# Register the Blosc HDF5 filter process-wide as early as possible. New files
# write intensity/labels/masks with Blosc (see store._compression_kwargs); any
# process that reads them — the GUI, CLIs, and spawned parallel-decode workers —
# needs the filter registered first. Importing any percell4 module triggers this,
# so every read path is covered. (Existing gzip files read without it.)
import hdf5plugin  # noqa: E402, F401

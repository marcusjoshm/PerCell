# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for PerCell4 standalone bundle.

Build with:
    pyinstaller percell4.spec

Output: dist/PerCell4/ (folder with PerCell4 executable)
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata


def _bundle_version() -> str:
    """Dotted-integer version for the macOS plist, from the installed package.

    The package version is tag-derived (setuptools-scm); Apple wants plain
    ``major.minor.patch`` here, so any ``.devN+g<hash>`` suffix is dropped.
    """
    try:
        from importlib.metadata import version

        from packaging.version import Version

        return Version(version("percell4")).base_version
    except Exception:
        return "0.0.0"


_bundle_version_str = _bundle_version()

src_dir = str(Path("src"))

# Auto-collect all percell4 submodules instead of manual listing
_hidden = collect_submodules("percell4") + [
    # GUI frameworks
    "PyQt5",
    "qtpy",
    "pyqtgraph",
    # napari and its many dependencies
    "napari",
    "napari.utils",
    "napari.layers",
    "napari.viewer",
    # Scientific
    "numpy",
    "scipy",
    "scipy.ndimage",
    "scipy.signal",
    "pandas",
    "h5py",
    "tifffile",
    "sdtfile",
    "skimage",
    "skimage.measure",
    "skimage.filters",
    "skimage.morphology",
    "cellpose",
    "cellpose.models",
    # diptest is a compiled C-extension; collect_submodules("percell4") will not
    # pull it in, so it must be listed explicitly (PyInstaller then collects the
    # binary). Backs the CNR subpopulation gap test.
    "diptest",
    # Optional extras (include if installed)
    "dtcwt",
    "roifile",
    "click",
    "rich",
]

# Collect data files for packages that ship resources
_datas = (
    collect_data_files("napari")
    + collect_data_files("cellpose")
    + collect_data_files("skimage")
    # Bundle our own icon resources so app_icon_path() resolves when frozen
    + collect_data_files("percell4.resources", includes=["*.png", "*.ico", "*.icns"])
    # Ship the dist-info so importlib.metadata.version("percell4") works frozen
    + copy_metadata("percell4")
)

# Platform-native application icons (built from src/percell4/resources)
_res_dir = Path("src") / "percell4" / "resources"
_win_icon = str(_res_dir / "percell4.ico")
_mac_icon = str(_res_dir / "percell4.icns")

a = Analysis(
    [str(Path("src") / "percell4" / "app.py")],
    pathex=[src_dir],
    binaries=[],
    datas=_datas,
    hiddenimports=_hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "matplotlib",
        "IPython",
        "jupyter",
        "notebook",
        "sphinx",
        "docutils",
        # App targets PyQt5; exclude other Qt bindings so PyInstaller does not
        # abort on "multiple Qt bindings packages" (PyQt6/PySide6 are installed
        # in this env but unused).
        "PyQt6",
        "PySide6",
        "PySide2",
    ],
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PerCell4",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    windowed=True,
    icon=_win_icon,  # Windows .exe icon (ignored on other platforms)
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="PerCell4",
)

# macOS: create .app bundle
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="PerCell4.app",
        icon=_mac_icon,
        bundle_identifier="com.leelab.percell4",
        info_plist={
            "CFBundleName": "PerCell4",
            "CFBundleDisplayName": "PerCell4",
            "CFBundleVersion": _bundle_version_str,
            "CFBundleShortVersionString": _bundle_version_str,
            "NSHighResolutionCapable": True,
        },
    )

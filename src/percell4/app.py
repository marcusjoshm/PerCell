"""PerCell4 application entry point.

Creates the QApplication, CellDataModel, and LauncherWindow.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from qtpy.QtCore import Qt
from qtpy.QtGui import QIcon, QPixmap
from qtpy.QtWidgets import QApplication, QSplashScreen

import percell4._compat  # noqa: F401 — NumPy 2.0 shims for dtcwt


def _make_splash() -> QSplashScreen:
    """Build the startup splash screen showing the PerCell4 logo.

    Shown immediately, before the heavy GUI/scientific imports below, so the
    user gets visual feedback while the app loads (à la FIJI/ImageJ).
    """
    from percell4.resources import splash_image_path

    pixmap = QPixmap(str(splash_image_path()))
    # Scale the square logo down to a sensible on-screen size.
    pixmap = pixmap.scaled(
        400,
        400,
        Qt.KeepAspectRatio,
        Qt.SmoothTransformation,
    )
    splash = QSplashScreen(pixmap, Qt.WindowStaysOnTopHint)
    splash.setWindowFlag(Qt.FramelessWindowHint, True)
    return splash


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """The command line. Unknown arguments are left for Qt."""
    parser = argparse.ArgumentParser(prog="percell4", add_help=True)
    parser.add_argument(
        "--analyzeit-review",
        metavar="ORDER_FOLDER",
        type=Path,
        help="Open the review of an AnalyzeIt order folder once PerCell is up.",
    )
    known, _unknown = parser.parse_known_args(argv)
    return known


def main(argv: list[str] | None = None) -> None:
    """Launch the PerCell4 GUI application."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    # A frozen app must hand spawned children (the Bio-Formats reader, the
    # parallel HDF5 decoders) to their worker instead of relaunching the GUI.
    import multiprocessing

    multiprocessing.freeze_support()
    app = QApplication.instance() or QApplication(sys.argv)

    # Linux: match PyOpenGL's GL backend to the one Qt just committed to,
    # before the napari/vispy imports below pull OpenGL in.
    from percell4.gui.opengl_platform import configure_pyopengl_platform

    configure_pyopengl_platform(app)

    # Application icon: Dock (macOS), taskbar (Windows, Linux). Set as the
    # QApplication default so every top-level window inherits it.
    from percell4.resources import app_icon_path

    app.setWindowIcon(QIcon(str(app_icon_path())))

    # Show the splash before importing the (slow) GUI + scientific stack so it
    # paints immediately. processEvents() forces the first paint.
    splash = _make_splash()
    splash.show()
    app.processEvents()

    from percell4.gui.theme import apply_theme

    apply_theme(app)

    from percell4.application.session import Session
    from percell4.interfaces.gui.main_window import LauncherWindow
    from percell4.model import CellDataModel

    session = Session()
    data_model = CellDataModel(session)
    launcher = LauncherWindow(data_model)
    launcher.show()

    # Dismiss the splash once the launcher is up; finish() keeps the splash on
    # top until the target window is ready to be shown.
    splash.finish(launcher)

    # Started by `analyzeit review`: open the order's review straight away.
    if args.analyzeit_review is not None:
        launcher.open_analyzeit_review(args.analyzeit_review)

    # Windows-only: warn if the MSVC Redistributable is too old for PyTorch.
    # Silently passes on macOS/Linux.
    from percell4.workflows.diagnostics import check_msvc_redist_version

    is_current, version = check_msvc_redist_version()
    if not is_current:
        from percell4.gui.torch_error import show_msvc_redist_warning

        show_msvc_redist_warning(launcher, version)

    sys.exit(app.exec_())


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    main()

"""Consent dialog for the Java runtime and Bio-Formats that in-file import needs.

States follow the provisioning diagram in the plan:

* **Consent** -- lists what would be downloaded (name, source URL, size,
  destination). Buttons: Download, Use an existing Java…, Not now.
* **Downloading** -- progress bar plus Cancel. The download runs on the GUI
  thread; the cancel check pumps events so the dialog stays responsive while
  the Java runtime is fetched in its child process.
* **Ready** -- the dialog accepts, and the caller resumes its probe.
* **Failed** -- the reason, with Retry and Close.

Nothing downloads before the user clicks Download (R16).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from qtpy.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from percell4.gui._dialog_utils import (
    cap_to_screen,
    existing_directory,
    make_freestanding,
    wrap_in_scroll,
)

STATE_CONSENT = "consent"
STATE_DOWNLOADING = "downloading"
STATE_FAILED = "failed"
STATE_READY = "ready"


def _human_size(n: int) -> str:
    return f"{n / 1_000_000:.0f} MB"


def _probe_java_home(home: str) -> tuple[bool, str]:
    """Try ``home`` as a Java home. Returns (works, reason)."""
    from percell4.adapters.java_runtime import resolve_java
    from percell4.config.advanced import AdvancedSettings

    result = resolve_java(
        settings=AdvancedSettings(java_home=home),
        environ={},
        cache_dir=Path(home) / ".percell-no-cache",
    )
    works = result.ready and result.source == "settings"
    return works, result.reason if works else "; ".join(result.attempts[:1])


def _save_java_home(home: str) -> None:
    from percell4.config.advanced import update_advanced_settings

    update_advanced_settings(java_home=home)


def _provision(progress, cancel) -> None:
    from percell4.adapters.java_runtime import provision, record_download_consent

    record_download_consent()
    provision(progress, cancel, consented=True)


def _describe():
    from percell4.adapters.java_runtime import describe_java_environment

    return describe_java_environment()


class JavaSetupDialog(QDialog):
    """Ask before downloading Java and Bio-Formats, or use an existing Java.

    The callables are seams for tests; the defaults use the real
    provisioning code and advanced settings.
    """

    def __init__(
        self,
        parent=None,
        *,
        describe: Callable[[], object] = _describe,
        provision: Callable[[Callable, Callable], None] = _provision,
        probe_java_home: Callable[[str], tuple[bool, str]] = _probe_java_home,
        save_java_home: Callable[[str], None] = _save_java_home,
        pick_directory: Callable[[QDialog], str] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Set up Bio-Formats")
        self._provision = provision
        self._probe_java_home = probe_java_home
        self._save_java_home = save_java_home
        self._pick_directory = pick_directory or (
            lambda dlg: existing_directory(dlg, "Select a Java home folder")
        )
        self._cancel_requested = False
        self.state = STATE_CONSENT
        self.failure_reason = ""

        env = describe()
        outer = QVBoxLayout(self)
        content = QWidget()
        layout = QVBoxLayout(content)
        outer.addWidget(wrap_in_scroll(content))
        intro = QLabel(
            "In-file import reads multi-dimensional files with Bio-Formats, which "
            "needs Java. PerCell can download them into its own cache folder:"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        lines = [
            f"• {item.name}: {_human_size(item.size_bytes)} from {item.url}\n"
            f"   to {item.destination}"
            for item in getattr(env, "pending_downloads", ())
        ]
        self._details = QLabel("\n".join(lines) or getattr(env, "summary", ""))
        self._details.setWordWrap(True)
        layout.addWidget(self._details)
        licence = QLabel(
            "Bio-Formats is GPL-licensed software from the Open Microscopy "
            "Environment. It is downloaded to this computer only."
        )
        licence.setWordWrap(True)
        layout.addWidget(licence)

        self.message = QLabel("")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        buttons = QHBoxLayout()
        self.download_button = QPushButton("Download")
        self.download_button.clicked.connect(self._on_download)
        self.existing_button = QPushButton("Use an existing Java…")
        self.existing_button.clicked.connect(self._on_existing)
        self.cancel_button = QPushButton("Cancel download")
        self.cancel_button.clicked.connect(self._on_cancel_download)
        self.cancel_button.setVisible(False)
        self.close_button = QPushButton("Not now")
        self.close_button.clicked.connect(self.reject)
        for button in (self.download_button, self.existing_button, self.cancel_button,
                       self.close_button):
            buttons.addWidget(button)
        outer.addLayout(buttons)
        self.resize(560, 320)
        cap_to_screen(self)
        make_freestanding(self)

    # -- actions -------------------------------------------------------------

    def _on_existing(self) -> None:
        home = self._pick_directory(self)
        if not home:
            return
        works, reason = self._probe_java_home(home)
        if not works:
            self.message.setText(f"That folder does not hold a working Java: {reason}")
            return
        self._save_java_home(home)
        self.state = STATE_READY
        self.accept()

    def _on_cancel_download(self) -> None:
        self._cancel_requested = True

    def _cancelled(self) -> bool:
        QApplication.processEvents()
        return self._cancel_requested

    def _on_progress(self, message: str, done: int, total: int | None) -> None:
        self.message.setText(message)
        if total:
            self.progress.setRange(0, int(total))
            self.progress.setValue(int(min(done, total)))
        else:
            self.progress.setRange(0, 0)
        QApplication.processEvents()

    def _set_state(self, state: str) -> None:
        self.state = state
        downloading = state == STATE_DOWNLOADING
        self.progress.setVisible(downloading)
        self.cancel_button.setVisible(downloading)
        self.download_button.setEnabled(not downloading)
        self.existing_button.setEnabled(not downloading)
        self.close_button.setEnabled(not downloading)
        if state == STATE_FAILED:
            self.download_button.setText("Retry")
            self.close_button.setText("Close")

    def _on_download(self) -> None:
        self._cancel_requested = False
        self._set_state(STATE_DOWNLOADING)
        try:
            self._provision(self._on_progress, self._cancelled)
        except Exception as exc:  # noqa: BLE001 - every failure is shown, then retryable
            self.failure_reason = (
                "cancelled" if self._cancel_requested else str(exc) or type(exc).__name__
            )
            self.message.setText(f"Download failed: {self.failure_reason}")
            self._set_state(STATE_FAILED)
            return
        self._set_state(STATE_READY)
        self.accept()

"""What an import keeps from each z-stack: projections, the z-series, or both.

One row of checkboxes (Max, Mean, Sum, Full z-series) with the approximate
size of what is kept, plus the *Z step (µm)* field token imports use when
their files carry no ImageJ spacing. The import dialog shows the form only
when the selection has a Z axis (R4).
"""

from __future__ import annotations

from qtpy.QtCore import Signal
from qtpy.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from percell4.domain.io.projections import (
    PROJECTION_NAMES,
    StorageChoice,
    format_bytes,
)

_LABELS = {"max": "Max", "mean": "Mean", "sum": "Sum"}


class StorageChoiceForm(QWidget):
    """Keep checkboxes, a size estimate and the Z step field.

    ``changed`` fires after any checkbox changes. :meth:`choice` is ``None``
    while nothing is checked; the dialog then disables Import.
    """

    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        row = QHBoxLayout()
        row.addWidget(QLabel("Keep:"))
        self.projection_boxes: dict[str, QCheckBox] = {}
        for name in PROJECTION_NAMES:
            box = QCheckBox(f"{_LABELS[name]} projection")
            box.setChecked(name == "max")
            box.toggled.connect(self._on_toggled)
            row.addWidget(box)
            self.projection_boxes[name] = box
        self.zseries_box = QCheckBox("Full z-series")
        self.zseries_box.setToolTip(
            "Store every z-plane (float32) for viewing in 2D and 3D. Analysis "
            "always runs on a projection; a dataset kept without one is "
            "view-only until a projection is added."
        )
        self.zseries_box.toggled.connect(self._on_toggled)
        row.addWidget(self.zseries_box)
        row.addStretch()
        outer.addLayout(row)

        self.estimate_label = QLabel("")
        self.estimate_label.setWordWrap(True)
        outer.addWidget(self.estimate_label)

        self._z_step_row = QWidget()
        z_row = QHBoxLayout(self._z_step_row)
        z_row.setContentsMargins(0, 0, 0, 0)
        z_row.addWidget(QLabel("Z step (µm):"))
        self.z_step_spin = QDoubleSpinBox()
        self.z_step_spin.setRange(0.0, 1000.0)
        self.z_step_spin.setDecimals(4)
        self.z_step_spin.setSingleStep(0.1)
        self.z_step_spin.setSpecialValueText("unknown")
        self.z_step_spin.setValue(0.0)
        z_row.addWidget(self.z_step_spin)
        self.z_step_note = QLabel("")
        z_row.addWidget(self.z_step_note)
        z_row.addStretch()
        outer.addWidget(self._z_step_row)
        self.set_detected_z_step(None)

    # -- state ---------------------------------------------------------------

    def choice(self) -> StorageChoice | None:
        """The checked choice, or ``None`` while nothing is checked."""
        projections = tuple(n for n, b in self.projection_boxes.items() if b.isChecked())
        keep = self.zseries_box.isChecked()
        if not projections and not keep:
            return None
        return StorageChoice(projections=projections, keep_zseries=keep)

    def z_step_um(self) -> float | None:
        """The Z step the user entered, or ``None`` when it is unknown."""
        value = float(self.z_step_spin.value())
        return value if value > 0 else None

    def set_detected_z_step(self, value: float | None) -> None:
        """Show a z step read from the files, read-only; ``None`` makes the
        field editable again."""
        detected = value is not None and value > 0
        self.z_step_spin.setReadOnly(detected)
        if detected:
            self.z_step_spin.setValue(float(value))
            self.z_step_note.setText("auto-detected from the file")
            self.z_step_spin.setToolTip("Read from the first z-plane file's ImageJ spacing.")
        else:
            self.z_step_note.setText("")
            self.z_step_spin.setToolTip(
                "Distance between z-planes. Used for 3D viewing proportions; "
                "leave unknown to render cube voxels."
            )

    def set_z_step_visible(self, visible: bool) -> None:
        self._z_step_row.setVisible(visible)

    def set_estimate(self, zseries_bytes: int | None, projection_bytes: int | None) -> None:
        """Show the approximate uncompressed size, or clear it with ``None``."""
        if zseries_bytes is None or projection_bytes is None:
            self.estimate_label.setText("")
            return
        choice = self.choice()
        if choice is None:
            self.estimate_label.setText("Choose at least one thing to keep.")
            return
        parts = []
        if choice.keep_zseries:
            parts.append(f"z-series {format_bytes(zseries_bytes)}")
        if choice.projections:
            n = len(choice.projections)
            parts.append(f"{n} projection{'s' if n != 1 else ''} {format_bytes(projection_bytes)}")
        total = format_bytes(zseries_bytes + projection_bytes)
        self.estimate_label.setText(
            f"About {total} before compression ({' + '.join(parts)})."
        )

    # -- signals -------------------------------------------------------------

    def _on_toggled(self, _checked: bool) -> None:
        self.changed.emit()

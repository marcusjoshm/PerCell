"""Review table for an in-file import scheme.

One row per source (a file's series), then one row per excluded file with
its reason, so every selected file is visible (R4). The user can include or
leave out a row, pick its channels, swap its Z and T axes, and confirm a
flagged row. Every edit rebuilds the scheme through the U1 domain helpers;
the table never computes sizes or flags itself.
"""

from __future__ import annotations

from dataclasses import replace

from qtpy.QtCore import Qt, Signal
from qtpy.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from percell4.domain.io.infile import (
    AxisMap,
    ImportScheme,
    ImportSource,
    confirm_source,
    reassign_axes,
    with_z_method,
)
from percell4.domain.io.naming import channel_display_name
from percell4.domain.io.projections import StorageChoice

COLUMNS = ("Import", "Dataset", "Source", "Dimensions", "Calibration", "Channels", "Axes",
           "Keep", "Status")
AXES_AS_READ = "As read"
AXES_SWAPPED = "Swap Z and T"
_SWAPPED = AxisMap(z="T", t="Z")


def dims_text(source: ImportSource) -> str:
    s = source.effective
    if s is None:
        return "—"
    return f"T{s.size_t} · C{s.channel_count} · Z{s.size_z} · {s.size_y}×{s.size_x}"


def calibration_text(source: ImportSource) -> str:
    s = source.effective
    if s is None:
        return "—"
    parts = []
    if s.physical_x_um:
        parts.append(f"{s.physical_x_um:.4g} µm/px")
    if s.physical_z_um:
        parts.append(f"z {s.physical_z_um:.4g} µm")
    return " · ".join(parts) or "none in file"


def keep_text(source: ImportSource, storage: StorageChoice | None) -> str:
    """What the import keeps from this source: the storage choice for a
    z-stack, ``"as is (no Z)"`` for a single-plane source (R4)."""
    s = source.effective
    if s is None:
        return "—"
    if s.size_z <= 1:
        return "as is (no Z)"
    return storage.label if storage is not None else "—"


def channel_label(index: int, metadata_names: tuple[str, ...]) -> str:
    name = channel_display_name(str(index))
    if index < len(metadata_names) and metadata_names[index]:
        return f"{name} ({metadata_names[index]})"
    return name


class InfileReviewTable(QWidget):
    """Shows an :class:`ImportScheme` and edits it in place.

    ``changed`` fires after every user edit; read :meth:`scheme` for the
    current state.
    """

    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scheme = ImportScheme()
        self._note_rows: list[tuple[str, str]] = []
        self._storage: StorageChoice | None = StorageChoice()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._table = QTableWidget(0, len(COLUMNS))
        self._table.setHorizontalHeaderLabels(list(COLUMNS))
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setStretchLastSection(True)
        layout.addWidget(self._table)
        self._warnings = QLabel("")
        self._warnings.setWordWrap(True)
        layout.addWidget(self._warnings)
        #: Per-row widgets, keyed by source index, for tests and signal wiring.
        self.include_boxes: dict[int, QCheckBox] = {}
        self.channel_boxes: dict[int, list[QCheckBox]] = {}
        self.axes_combos: dict[int, QComboBox] = {}
        self.confirm_buttons: dict[int, QPushButton] = {}

    # -- state ---------------------------------------------------------------

    def scheme(self) -> ImportScheme:
        return self._scheme

    def set_scheme(self, scheme: ImportScheme) -> None:
        self._scheme = scheme
        self._note_rows = []
        self._rebuild()

    def set_notes(self, rows: list[tuple[str, str]]) -> None:
        """Show files that could not be probed yet, e.g. ``("a.tif", "needs Java")``."""
        self._scheme = ImportScheme()
        self._note_rows = list(rows)
        self._rebuild()

    def set_storage(self, storage: StorageChoice | None) -> None:
        """Show what the import keeps in each source's Keep cell."""
        self._storage = storage
        for index, source in enumerate(self._scheme.sources):
            self._text(index, "Keep", keep_text(source, storage))

    def set_z_method(self, z_method: str) -> None:
        """Adopt a new z method and refresh the z-count warnings."""
        if self._scheme.sources:
            self._scheme = with_z_method(self._scheme, z_method)
            self._show_warnings()

    def importable_sources(self) -> tuple[ImportSource, ...]:
        """Included, confirmed sources with at least one channel."""
        return tuple(s for s in self._scheme.importable_sources if s.channel_indices)

    def row_count(self) -> int:
        return self._table.rowCount()

    def status_text(self, row: int) -> str:
        cell = self._table.cellWidget(row, COLUMNS.index("Status"))
        if isinstance(cell, QLabel):
            return cell.text()
        if cell is not None:
            label = cell.findChild(QLabel)
            return label.text() if label is not None else ""
        item = self._table.item(row, COLUMNS.index("Status"))
        return item.text() if item is not None else ""

    def cell_text(self, row: int, column: str) -> str:
        item = self._table.item(row, COLUMNS.index(column))
        return item.text() if item is not None else ""

    # -- edits ---------------------------------------------------------------

    def _update(self, index: int, source: ImportSource) -> None:
        # Refresh only this row's derived cells. Rebuilding the table here
        # would delete the widget whose signal is still being delivered.
        self._scheme = self._scheme.replace_source(index, source)
        self._refresh_row(index)
        self.changed.emit()

    def _on_include(self, index: int, checked: bool) -> None:
        self._update(index, replace(self._scheme.sources[index], included=checked))

    def _on_channel(self, index: int) -> None:
        boxes = self.channel_boxes[index]
        picked = tuple(i for i, box in enumerate(boxes) if box.isChecked())
        self._update(index, replace(self._scheme.sources[index], channel_indices=picked))

    def _on_axes(self, index: int, text: str) -> None:
        source = self._scheme.sources[index]
        axis_map = _SWAPPED if text == AXES_SWAPPED else AxisMap()
        self._update(index, reassign_axes(source, axis_map))

    def _on_confirm(self, index: int) -> None:
        self._update(index, confirm_source(self._scheme.sources[index]))

    # -- rendering -----------------------------------------------------------

    def _rebuild(self) -> None:
        self.include_boxes.clear()
        self.channel_boxes.clear()
        self.axes_combos.clear()
        self.confirm_buttons.clear()
        sources = self._scheme.sources
        excluded = self._scheme.excluded
        total = len(sources) + len(excluded) + len(self._note_rows)
        self._table.setRowCount(total)
        row = 0
        for index, source in enumerate(sources):
            self._source_row(row, index, source)
            row += 1
        for entry in excluded:
            name = entry.path.name
            if entry.series_index is not None:
                name += f" (series {entry.series_index})"
            self._note_row(row, name, entry.reason)
            row += 1
        for name, reason in self._note_rows:
            self._note_row(row, name, reason)
            row += 1
        self._show_warnings()

    def _show_warnings(self) -> None:
        self._warnings.setText("\n".join(f"⚠ {w}" for w in self._scheme.warnings))
        self._warnings.setVisible(bool(self._scheme.warnings))

    def _refresh_row(self, index: int) -> None:
        """Re-render the cells of source row ``index`` that depend on its state."""
        source = self._scheme.sources[index]
        row = index  # source rows come first
        flagged = bool(source.needs_confirmation)
        include = self.include_boxes[index]
        include.blockSignals(True)
        include.setEnabled(not flagged)
        include.setChecked(source.included and not flagged)
        include.blockSignals(False)
        self._text(row, "Dimensions", dims_text(source))
        self._text(row, "Calibration", calibration_text(source))
        self._text(row, "Keep", keep_text(source, self._storage))
        self._status_cell(row, index, source)

    def _status_cell(self, row: int, index: int, source: ImportSource) -> None:
        self.confirm_buttons.pop(index, None)
        if not source.needs_confirmation:
            self._table.setCellWidget(row, COLUMNS.index("Status"), QLabel("Ready"))
            return
        status = QWidget()
        status_row = QHBoxLayout(status)
        status_row.setContentsMargins(2, 0, 2, 0)
        status_row.addWidget(QLabel(f"⚠ {source.needs_confirmation}"))
        confirm = QPushButton("Confirm axes")
        confirm.clicked.connect(lambda _=False, i=index: self._on_confirm(i))
        status_row.addWidget(confirm)
        self._table.setCellWidget(row, COLUMNS.index("Status"), status)
        self.confirm_buttons[index] = confirm

    def _text(self, row: int, column: str, text: str) -> None:
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        self._table.setItem(row, COLUMNS.index(column), item)

    def _source_row(self, row: int, index: int, source: ImportSource) -> None:
        flagged = bool(source.needs_confirmation)
        include = QCheckBox()
        include.setChecked(source.included and not flagged)
        include.setEnabled(not flagged)
        include.toggled.connect(lambda checked, i=index: self._on_include(i, checked))
        self._table.setCellWidget(row, COLUMNS.index("Import"), include)
        self.include_boxes[index] = include

        self._text(row, "Dataset", f"{source.output_name}.h5")
        series = f" (series {source.series_index})" if source.series_index else ""
        self._text(row, "Source", f"{source.path.name}{series}")
        self._text(row, "Dimensions", dims_text(source))
        self._text(row, "Calibration", calibration_text(source))

        names = source.series.channel_names if source.series is not None else ()
        n_channels = source.series.channel_count if source.series is not None else 0
        holder = QWidget()
        box_row = QHBoxLayout(holder)
        box_row.setContentsMargins(2, 0, 2, 0)
        boxes = []
        for c in range(n_channels):
            box = QCheckBox(channel_label(c, names))
            box.setChecked(c in source.channel_indices)
            box.toggled.connect(lambda _checked, i=index: self._on_channel(i))
            box_row.addWidget(box)
            boxes.append(box)
        self._table.setCellWidget(row, COLUMNS.index("Channels"), holder)
        self.channel_boxes[index] = boxes

        axes = QComboBox()
        axes.addItems([AXES_AS_READ, AXES_SWAPPED])
        axes.setCurrentText(AXES_AS_READ if source.axis_map.is_identity else AXES_SWAPPED)
        axes.currentTextChanged.connect(lambda text, i=index: self._on_axes(i, text))
        self._table.setCellWidget(row, COLUMNS.index("Axes"), axes)
        self.axes_combos[index] = axes
        self._text(row, "Keep", keep_text(source, self._storage))

        self._status_cell(row, index, source)

    def _note_row(self, row: int, name: str, reason: str) -> None:
        include = QCheckBox()
        include.setChecked(False)
        include.setEnabled(False)
        self._table.setCellWidget(row, COLUMNS.index("Import"), include)
        self._text(row, "Dataset", "—")
        self._text(row, "Source", name)
        for column in ("Dimensions", "Calibration", "Keep"):
            self._text(row, column, "—")
        for column in ("Channels", "Axes"):
            self._table.setCellWidget(row, COLUMNS.index(column), None)
            self._text(row, column, "")
        self._table.setCellWidget(row, COLUMNS.index("Status"), QLabel(reason))


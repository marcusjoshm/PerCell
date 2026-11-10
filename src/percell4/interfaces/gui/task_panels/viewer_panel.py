"""Viewer task panel — open / hide the viewer window and scope the cell selection.

Receives the shared ``CellDataModel`` plus window callbacks at construction —
no launcher reference. The viewer is a persistent singleton: ``Open Viewer``
shows/raises it, ``Hide Viewer`` hides the window without destroying the
viewer (it stays active and subscribed to the session).

Also hosts the **Z-series** controls (z-stack plan U8): Show z-series and
Show segmentations and masks through Z, present only when the dataset
stores a z-series. The launcher owns the layers; the panel owns the
checkboxes and calls back.

Hosts the **Cell Filter** Selector — the canonical writer of the session
``filter_ids`` field. Selection originates here (and in the viewer canvas,
cell table, and data plot, which are co-writers of ``selection``); filtering
to that selection scopes every window. Writes go straight to the session via
``CellDataModel`` — never through napari layer-list events.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from qtpy.QtCore import Qt
from qtpy.QtWidgets import (
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from percell4.gui import theme
from percell4.model import CellDataModel


class ViewerPanel(QWidget):
    """Panel hosting viewer-window controls and the Cell Filter Selector.

    All collaborators are injected — the panel has no knowledge of the
    launcher internals beyond the callbacks it is given.
    """

    def __init__(
        self,
        data_model: CellDataModel,
        *,
        show_window: Callable[[str], None],
        get_viewer_window: Callable[[], Any],
        show_status: Callable[[str], None] = lambda _: None,
        set_zseries_shown: Callable[[bool], None] = lambda _: None,
        set_zseries_overlay: Callable[[bool], None] = lambda _: None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._data_model = data_model
        self._show_window = show_window
        self._get_viewer_window = get_viewer_window
        self._show_status = show_status
        self._set_zseries_shown = set_zseries_shown
        self._set_zseries_overlay = set_zseries_overlay
        self._build_ui()
        self._refresh_zseries_controls(reset=True)

        # Subscribe to filter changes so the Clear-Filter button enabled-state
        # and the count label stay in sync (relocated with the Selector).
        self._data_model.state_changed.connect(self._on_state_changed)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignTop)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)

        layout.addWidget(theme.section_label("Viewer"))

        btn_open = QPushButton("Open Viewer")
        btn_open.setToolTip("Open or re-show the viewer window.")
        btn_open.clicked.connect(lambda: self._show_window("viewer"))
        layout.addWidget(btn_open)

        btn_hide = QPushButton("Hide Viewer")
        btn_hide.setToolTip("Hide the viewer window; the viewer stays active.")
        btn_hide.clicked.connect(self._on_hide_viewer)
        layout.addWidget(btn_hide)

        self._contrast_btn = QPushButton("Contrast Histogram")
        self._contrast_btn.setCheckable(True)
        self._contrast_btn.setToolTip(
            "Open or close the Contrast window: the selected image layer's "
            "histogram with draggable contrast limits."
        )
        self._contrast_btn.toggled.connect(self._on_contrast_toggled)
        layout.addWidget(self._contrast_btn)
        self._contrast_source = None

        # ── Z-series group (only when the dataset stores one) ──
        self._zseries_group = QGroupBox("Z-series")
        z_layout = QVBoxLayout(self._zseries_group)
        self._zseries_check = QCheckBox("Show z-series")
        self._zseries_check.setToolTip(
            "Add the stored z-series next to the projection. Use napari's 2D/3D "
            "button to switch between Z (and T) sliders and a rotatable 3D view. "
            "For viewing only: analysis always reads the projection."
        )
        self._zseries_check.toggled.connect(self._on_zseries_toggled)
        z_layout.addWidget(self._zseries_check)
        self._overlay_check = QCheckBox("Show segmentations and masks through Z")
        self._overlay_check.toggled.connect(self._on_overlay_toggled)
        z_layout.addWidget(self._overlay_check)
        layout.addWidget(self._zseries_group)

        # ── Cell Filter group ──
        filter_group = QGroupBox("Cell Filter")
        filter_layout = QVBoxLayout(filter_group)

        sel_btn_row = QHBoxLayout()
        btn_clear_sel = QPushButton("Clear Selection")
        btn_clear_sel.setToolTip("Deselect all cells and restore viewer to normal")
        btn_clear_sel.clicked.connect(self._on_clear_selection)
        sel_btn_row.addWidget(btn_clear_sel)
        filter_layout.addLayout(sel_btn_row)

        filter_btn_row = QHBoxLayout()
        btn_filter = QPushButton("Filter to Selection")
        btn_filter.setToolTip("Show only the currently selected cells in all windows")
        btn_filter.clicked.connect(self._on_filter_to_selection)
        filter_btn_row.addWidget(btn_filter)

        self._clear_filter_btn = QPushButton("Clear Filter")
        self._clear_filter_btn.setEnabled(False)
        self._clear_filter_btn.clicked.connect(self._on_clear_filter)
        filter_btn_row.addWidget(self._clear_filter_btn)
        filter_layout.addLayout(filter_btn_row)

        self._filter_status_label = QLabel("No filter active")
        self._filter_status_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        filter_layout.addWidget(self._filter_status_label)

        layout.addWidget(filter_group)

        layout.addStretch()

    # ── Viewer actions ───────────────────────────────────────

    def _on_hide_viewer(self) -> None:
        """Hide the viewer window if it exists. No-op when never opened."""
        win = self._get_viewer_window()
        if win is not None:
            win.hide()

    def _on_contrast_toggled(self, checked: bool) -> None:
        win = self._get_viewer_window()
        if win is None and checked:
            self._show_window("viewer")
            win = self._get_viewer_window()
        if win is None:
            return
        if win is not self._contrast_source:
            # Follow the window's own close button too.
            win.contrast_visibility_changed.connect(self._on_contrast_visibility)
            self._contrast_source = win
        win.set_contrast_visible(bool(checked))

    def _on_contrast_visibility(self, visible: bool) -> None:
        if self._contrast_btn.isChecked() != visible:
            self._contrast_btn.blockSignals(True)
            self._contrast_btn.setChecked(visible)
            self._contrast_btn.blockSignals(False)

    # ── State change routing ─────────────────────────────────

    def _on_state_changed(self, change) -> None:
        if change.filter:
            self._on_filter_state_changed()
        if getattr(change, "data", False):
            # A dataset change: the new dataset starts with its z-series hidden.
            self._refresh_zseries_controls(reset=True)
        elif getattr(change, "segmentation_list", False) or getattr(change, "mask_list", False):
            self._refresh_zseries_controls()

    # ── Z-series ─────────────────────────────────────────────

    def _dataset_metadata(self) -> dict:
        session = getattr(self._data_model, "session", None)
        ds = getattr(session, "dataset", None)
        return ds.metadata if ds is not None else {}

    def _refresh_zseries_controls(self, reset: bool = False) -> None:
        meta = self._dataset_metadata()
        has_zseries = bool(meta.get("has_zseries"))
        self._zseries_group.setVisible(has_zseries)
        if reset:
            for box in (self._zseries_check, self._overlay_check):
                box.blockSignals(True)
                box.setChecked(False)
                box.blockSignals(False)
        has_overlay = bool(meta.get("segmentation_names") or meta.get("mask_names"))
        shown = self._zseries_check.isChecked()
        self._overlay_check.setEnabled(has_overlay and shown)
        if not has_overlay:
            self._overlay_check.setToolTip("This dataset has no segmentation or mask to show.")
        elif not shown:
            self._overlay_check.setToolTip("Show the z-series first.")
        else:
            self._overlay_check.setToolTip(
                "Repeat each segmentation and mask through every z-plane "
                "(read-only views; they are 2D)."
            )

    def _on_zseries_toggled(self, checked: bool) -> None:
        self._set_zseries_shown(bool(checked))
        self._refresh_zseries_controls()

    def _on_overlay_toggled(self, checked: bool) -> None:
        self._set_zseries_overlay(bool(checked))

    # ── Cell Filter (Selector: writes session filter_ids / selection) ──

    def _on_clear_selection(self) -> None:
        self._data_model.set_selection([])

    def _on_filter_to_selection(self) -> None:
        selected = self._data_model.selected_ids
        if not selected:
            self._show_status("No cells selected to filter")
            return
        self._data_model.set_filter(list(selected))

    def _on_clear_filter(self) -> None:
        self._data_model.set_filter(None)

    def _on_filter_state_changed(self) -> None:
        if self._data_model.is_filtered:
            if len(self._data_model.df) == 0:
                # No measurements computed yet — the filter operates on
                # segmentation labels, not the measurements table, so report
                # the filter's own size rather than a misleading "0 of 0".
                n = len(self._data_model.filtered_ids or ())
                self._filter_status_label.setText(
                    f"Showing {n} filtered cell{'s' if n != 1 else ''}"
                )
            else:
                n_filtered = len(self._data_model.filtered_df)
                n_total = len(self._data_model.df)
                self._filter_status_label.setText(
                    f"Showing {n_filtered} of {n_total} cells"
                )
            self._filter_status_label.setStyleSheet(
                f"color: {theme.ACCENT}; font-weight: bold;"
            )
            self._clear_filter_btn.setEnabled(True)
        else:
            self._filter_status_label.setText("No filter active")
            self._filter_status_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
            self._clear_filter_btn.setEnabled(False)

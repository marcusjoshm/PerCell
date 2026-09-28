"""Compress dialog for converting TIFF directories to HDF5 datasets.

Replaces ImportDialog with support for batch compression. Discovers
datasets from a root directory and lets the user select which datasets
and channels to compress. Operates at the semantic level (datasets,
channels) rather than individual files.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from qtpy.QtCore import Qt
from qtpy.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from percell4.domain.io.infile import ImportScheme, StageOneResult, is_zarr_path
from percell4.domain.io.models import (
    CompressConfig,
    DatasetGuiState,
    DatasetSpec,
    DiscoveryMode,
    LayerAssignment,
    LayerType,
    TokenConfig,
)
from percell4.domain.io.naming import channel_display_name
from percell4.gui._dialog_utils import (
    blocking_progress_modality,
    cap_to_screen,
    center_on_screen,
    detach_window,
    open_file_names,
    progress_dialog,
    wrap_in_scroll,
)
from percell4.gui._stitching_form import StitchingForm
from percell4.gui.infile_review import InfileReviewTable
from percell4.gui.storage_choice import StorageChoiceForm

# Index of the "Tokenless (by name)" entry in the Discovery combo.
_TOKENLESS_INDEX = 2
# Index of the "In-file (multi-dimensional)" entry in the Discovery combo.
_INFILE_INDEX = 3
_MODE_INDEX = {
    DiscoveryMode.SUBDIRECTORY: 0,
    DiscoveryMode.FLAT: 1,
    DiscoveryMode.TOKENLESS: _TOKENLESS_INDEX,
    DiscoveryMode.INFILE: _INFILE_INDEX,
}
_MODE_LABELS = {
    DiscoveryMode.SUBDIRECTORY: "Subdirectory",
    DiscoveryMode.FLAT: "Flat Directory",
    DiscoveryMode.TOKENLESS: "Tokenless",
    DiscoveryMode.INFILE: "In-file",
}
_IMAGE_FILTER = (
    "Microscopy images (*.tif *.tiff *.btf *.czi *.nd2 *.lif *.lsm *.oib *.oif "
    "*.ims *.vsi *.dv *.r3d *.ics *.stk *.lei *.zvi *.png *.jpg);;All files (*)"
)


def _java_ready() -> bool:
    from percell4.adapters.java_runtime import describe_java_environment

    return describe_java_environment().ready


def _run_java_setup(parent) -> bool:
    from percell4.gui.java_setup_dialog import JavaSetupDialog

    dialog = JavaSetupDialog(parent)
    accepted = dialog.exec_() == QDialog.Accepted
    dialog.deleteLater()
    return accepted


def _shared_reader():
    from percell4.adapters.infile_scan import shared_reader

    return shared_reader()


class CompressDialog(QDialog):
    """Dialog for discovering and compressing TIFF datasets to HDF5.

    Presents semantic-level selection: pick datasets (left list) and
    channels (right list). Auto mode imports all selected channels as
    intensity. Manual mode allows renaming and assigning layer types.
    """

    def __init__(self, parent=None, project_dir: str | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Compress TIFF Dataset")
        # 750 dated from when the stitching controls were one wide QHBoxLayout
        # of eight widgets — and even 750 overflowed, forcing a horizontal
        # scrollbar. StitchingForm plus its acquisition-order diagram needs
        # ~692px of content, so 740 clears it once the vertical scrollbar and
        # frame are accounted for. Measured under test, not guessed —
        # test_compress_dialog_stitching_form.py fails if it stops fitting.
        self.setMinimumWidth(740)
        self.resize(780, 700)
        cap_to_screen(self)
        detach_window(self)
        center_on_screen(self)
        self._project_dir = project_dir

        self._datasets: list[DatasetSpec] = []
        self._all_channels: list[str] = []
        self._all_tiles: list[str] = []
        self._all_z_slices: list[str] = []
        self._all_timepoints: list[str] = []
        # First-file TIFF header per path (shape, ImageJ z spacing): read once.
        self._header_cache: dict[Path, dict] = {}
        self._discovery_generation = 0
        # In Tokenless mode, discovery synthesizes a channel regex from the
        # derived names; cache it so _current_token_config threads the identical
        # regex into import_dataset (discovery <-> importer parity).
        self._tokenless_token_config: TokenConfig | None = None

        # Source selection: one directory, or explicit files. Stage one of the
        # in-file classifier runs on it to suggest a discovery mode.
        self._selection: list[Path] = []
        self._stage_one: StageOneResult | None = None
        # Multi-plane files dropped from the last legacy discovery (R14).
        self._legacy_excluded: list[Path] = []
        # Seams for tests: the in-file reader, and the Java readiness gate.
        self._reader_factory = _shared_reader
        self._java_ready = _java_ready
        self._java_setup = _run_java_setup

        # Manual mode state: per-channel config (shared across datasets)
        self._channel_configs: dict[str, _ChannelConfig] = {}

        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)

        content = QWidget()
        layout = QVBoxLayout(content)
        outer.addWidget(wrap_in_scroll(content))

        # ── Source ──
        src_group = QGroupBox("Source")
        src_layout = QVBoxLayout(src_group)

        row_src = QHBoxLayout()
        row_src.addWidget(QLabel("Source:"))
        self._source_edit = QLineEdit()
        self._source_edit.setPlaceholderText(
            "Select a folder, or one or more image files..."
        )
        self._source_edit.setReadOnly(True)
        row_src.addWidget(self._source_edit, 1)
        btn_browse_src = QPushButton("Browse...")
        btn_browse_src.setToolTip("Select a folder")
        btn_browse_src.clicked.connect(self._on_browse_source)
        row_src.addWidget(btn_browse_src)
        btn_browse_files = QPushButton("Files...")
        btn_browse_files.setToolTip("Select one or more image files")
        btn_browse_files.clicked.connect(self._on_browse_files)
        row_src.addWidget(btn_browse_files)
        src_layout.addLayout(row_src)

        row_out = QHBoxLayout()
        row_out.addWidget(QLabel("Output:"))
        self._output_edit = QLineEdit()
        self._output_edit.setPlaceholderText(
            "Defaults to source parent directory"
        )
        row_out.addWidget(self._output_edit, 1)
        btn_browse_out = QPushButton("Browse...")
        btn_browse_out.clicked.connect(self._on_browse_output)
        row_out.addWidget(btn_browse_out)
        src_layout.addLayout(row_out)

        layout.addWidget(src_group)

        # ── Discovery Mode + Auto/Manual ──
        options_row = QHBoxLayout()
        options_row.addWidget(QLabel("Discovery:"))
        self._discovery_combo = QComboBox()
        self._discovery_combo.addItems(
            [
                "Subdirectory",
                "Flat Directory",
                "Tokenless (by name)",
                "In-file (multi-dimensional)",
            ]
        )
        self._discovery_combo.setToolTip(
            "Subdirectory: each child folder = one dataset.\n"
            "Flat Directory: groups files by stripping token patterns\n"
            "(channel, tile, etc.) from filenames.\n"
            "Tokenless (by name): no chXX token needed — the shared leading\n"
            "prefix becomes the .h5 name and the trailing name becomes the\n"
            "channel (e.g. ..._DNA, ..._SG_mask). Use Manual mode to rename\n"
            "a mis-derived channel or assign it as mask / segmentation.\n"
            "In-file: each file holds its own channels, z and time (ImageJ\n"
            "hyperstacks, OME-TIFF, vendor formats), read with Bio-Formats."
        )
        self._discovery_combo.currentIndexChanged.connect(
            self._on_discovery_mode_changed
        )
        options_row.addWidget(self._discovery_combo)

        options_row.addSpacing(30)
        options_row.addWidget(QLabel("Mode:"))
        self._auto_radio = QRadioButton("Auto")
        self._auto_radio.setChecked(True)
        self._auto_radio.toggled.connect(self._on_mode_changed)
        self._manual_radio = QRadioButton("Manual")
        options_row.addWidget(self._auto_radio)
        options_row.addWidget(self._manual_radio)
        options_row.addStretch()
        layout.addLayout(options_row)
        self._legacy_mode_widgets = [self._auto_radio, self._manual_radio]

        # Why the discovery mode was chosen for the current selection.
        self._suggestion_label = QLabel("")
        self._suggestion_label.setWordWrap(True)
        layout.addWidget(self._suggestion_label)

        # ── Datasets + Channels (side by side) ──
        lists_row = QHBoxLayout()

        # Left: datasets
        ds_group = QGroupBox("Datasets")
        self._ds_group = ds_group
        ds_layout = QVBoxLayout(ds_group)

        ds_btn_row = QHBoxLayout()
        btn_ds_all = QPushButton("Select All")
        btn_ds_all.clicked.connect(self._on_select_all_datasets)
        btn_ds_none = QPushButton("Deselect All")
        btn_ds_none.clicked.connect(self._on_deselect_all_datasets)
        ds_btn_row.addWidget(btn_ds_all)
        ds_btn_row.addWidget(btn_ds_none)
        ds_btn_row.addStretch()
        self._ds_count_label = QLabel("")
        ds_btn_row.addWidget(self._ds_count_label)
        ds_layout.addLayout(ds_btn_row)

        self._ds_list = QListWidget()
        # itemChanged fires on every checkbox toggle — without this wire the
        # Compress button only refreshes via the Select All / Deselect All
        # paths, so a user who tries to enable Compress by ticking a single
        # dataset sees no effect.
        self._ds_list.itemChanged.connect(self._update_compress_button)
        ds_layout.addWidget(self._ds_list)
        lists_row.addWidget(ds_group, 3)

        # Right: channels (auto mode = simple checkboxes)
        self._ch_group = QGroupBox("Channels")
        ch_layout = QVBoxLayout(self._ch_group)

        ch_btn_row = QHBoxLayout()
        btn_ch_all = QPushButton("Select All")
        btn_ch_all.clicked.connect(self._on_select_all_channels)
        btn_ch_none = QPushButton("Deselect All")
        btn_ch_none.clicked.connect(self._on_deselect_all_channels)
        ch_btn_row.addWidget(btn_ch_all)
        ch_btn_row.addWidget(btn_ch_none)
        ch_btn_row.addStretch()
        ch_layout.addLayout(ch_btn_row)

        self._ch_list = QListWidget()
        # See note on _ds_list.itemChanged — the channel list has the same
        # symmetry: a single-channel toggle must refresh Compress's
        # enablement.
        self._ch_list.itemChanged.connect(self._update_compress_button)
        ch_layout.addWidget(self._ch_list)

        # Manual mode: channel config panel (hidden in auto mode)
        self._manual_ch_panel = QWidget()
        manual_ch_layout = QVBoxLayout(self._manual_ch_panel)
        manual_ch_layout.setContentsMargins(0, 4, 0, 0)

        # This will be populated dynamically per-channel
        self._manual_ch_container = QVBoxLayout()
        manual_ch_layout.addLayout(self._manual_ch_container)
        self._manual_ch_panel.setVisible(False)
        ch_layout.addWidget(self._manual_ch_panel)

        lists_row.addWidget(self._ch_group, 2)

        layout.addLayout(lists_row)

        # ── In-file review (shown only in In-file mode) ──
        self._infile_group = QGroupBox("Files to import")
        infile_layout = QVBoxLayout(self._infile_group)
        self._review = InfileReviewTable()
        self._review.changed.connect(self._update_compress_button)
        infile_layout.addWidget(self._review)
        self._infile_group.setVisible(False)
        layout.addWidget(self._infile_group)

        # ── Discovery summary ──
        self._summary_label = QLabel("")
        self._summary_label.setWordWrap(True)
        layout.addWidget(self._summary_label)

        # ── Settings ──
        settings_group = QGroupBox("Settings")
        settings_layout = QVBoxLayout(settings_group)

        # What to keep from z-stacks (projections and/or the z-series). Shown
        # only when the selection has a Z axis (R4); max only by default.
        self._storage_form = StorageChoiceForm()
        self._storage_form.changed.connect(self._on_storage_changed)
        self._storage_form.setVisible(False)
        settings_layout.addWidget(self._storage_form)

        # Tile stitching — every control lives in the canonical StitchingForm.
        # The checkbox already labels the section, so the form's own group
        # title is suppressed to avoid saying "Tile Stitching" twice.
        self._stitch_check = QCheckBox("Tile Stitching")
        self._stitch_check.toggled.connect(self._on_stitch_toggled)
        settings_layout.addWidget(self._stitch_check)

        self._stitch_widget = StitchingForm(
            show_registration=True, show_fusion=True, title=""
        )
        self._stitch_widget.setContentsMargins(20, 0, 0, 0)
        # Thin aliases onto the shared form's widgets so the rest of this
        # dialog (and its tests) keep their existing call sites. Mind the axis
        # mapping: Grid size X is the COLUMN count, Grid size Y is the ROW
        # count — swapping them would transpose every mosaic.
        self._stitch_rows = self._stitch_widget.grid_y
        self._stitch_cols = self._stitch_widget.grid_x
        self._stitch_type = self._stitch_widget.grid_type
        self._stitch_order = self._stitch_widget.order
        self._stitch_overlap = self._stitch_widget.overlap
        self._stitch_register = self._stitch_widget.register_check
        self._stitch_reference = self._stitch_widget.reference
        self._stitch_fusion = self._stitch_widget.fusion
        self._stitch_widget.setVisible(False)
        settings_layout.addWidget(self._stitch_widget)
        self._legacy_mode_widgets.append(self._stitch_check)

        # Creation spatial bin -- locks the dataset's native_shape at
        # compress time. Cannot change after.
        bin_row = QHBoxLayout()
        bin_row.addWidget(QLabel("Creation spatial bin (k):"))
        self._creation_bin_spin = QSpinBox()
        self._creation_bin_spin.setRange(1, 16)
        self._creation_bin_spin.setValue(1)
        self._creation_bin_spin.setToolTip(
            "Sum-bin every source channel and .bin tile k×k at import. "
            "Defines /metadata.native_shape for the new dataset. The view-bin "
            "spinner on SessionWindow can downsample further at read time, "
            "but creation_bin cannot be changed after compress. Default 1 = "
            "no binning."
        )
        # Wire valueChanged at construction even though no listener cares
        # right now -- ensures any later test that drives the user-edit
        # signal path doesn't bypass the controller (per
        # docs/solutions/conventions/qt-wire-user-edit-signals-2026-05-12.md).
        self._creation_bin_spin.valueChanged.connect(
            self._on_creation_bin_changed
        )
        bin_row.addWidget(self._creation_bin_spin)
        bin_row.addStretch()
        settings_layout.addLayout(bin_row)

        layout.addWidget(settings_group)

        # ── FLIM .bin Parameters (auto-shown when .bin files detected) ──
        self._flim_group = QGroupBox("FLIM .bin Parameters")
        self._flim_group.setCheckable(True)
        self._flim_group.setChecked(False)
        self._flim_group.setToolTip(
            "Parameters for raw binary TCSPC histogram (.bin) files.\n"
            "Auto-enabled when .bin files are detected during discovery."
        )
        flim_layout = QFormLayout(self._flim_group)

        self._flim_freq = QDoubleSpinBox()
        self._flim_freq.setRange(0.1, 1000.0)
        self._flim_freq.setValue(80.0)
        self._flim_freq.setDecimals(1)
        self._flim_freq.setSuffix(" MHz")
        flim_layout.addRow("Laser frequency:", self._flim_freq)

        self._bin_x = QSpinBox()
        self._bin_x.setRange(1, 10000)
        self._bin_x.setValue(512)
        flim_layout.addRow("X dimension:", self._bin_x)

        self._bin_y = QSpinBox()
        self._bin_y.setRange(1, 10000)
        self._bin_y.setValue(512)
        flim_layout.addRow("Y dimension:", self._bin_y)

        self._bin_t = QSpinBox()
        self._bin_t.setRange(1, 4096)
        self._bin_t.setValue(132)
        flim_layout.addRow("Time bins:", self._bin_t)

        self._bin_dtype = QComboBox()
        self._bin_dtype.addItems(["uint32", "uint16", "float32", "uint8"])
        flim_layout.addRow("Data type:", self._bin_dtype)

        self._bin_dim_order = QComboBox()
        self._bin_dim_order.addItems(["YXT", "XYT", "TYX"])
        flim_layout.addRow("Dimension order:", self._bin_dim_order)

        self._bin_header = QSpinBox()
        self._bin_header.setRange(0, 10000)
        self._bin_header.setValue(0)
        self._bin_header.setSpecialValueText("Auto-detect")
        flim_layout.addRow("Header bytes:", self._bin_header)

        cal_label = QLabel("Per-channel calibration (phase / modulation):")
        flim_layout.addRow(cal_label)
        self._flim_cal_container = QVBoxLayout()
        flim_layout.addRow(self._flim_cal_container)
        self._channel_calibrations: dict[str, _CalibrationConfig] = {}

        self._flim_group.toggled.connect(self._on_flim_group_toggled)
        layout.addWidget(self._flim_group)
        self._on_flim_group_toggled(False)

        # ── Token Patterns (collapsible) ──
        self._token_group = QGroupBox("Advanced: Token Patterns")
        self._token_group.setCheckable(True)
        self._token_group.setChecked(False)
        token_layout = QVBoxLayout(self._token_group)

        self._tok_channel = QLineEdit(r"_ch(\d+)")
        self._tok_timepoint = QLineEdit(r"_t(\d+)")
        self._tok_zslice = QLineEdit(r"_z(\d+)")
        self._tok_tile = QLineEdit(r"_s(\d+)")

        for label_text, widget in [
            ("Channel:", self._tok_channel),
            ("Timepoint:", self._tok_timepoint),
            ("Z-slice:", self._tok_zslice),
            ("Tile:", self._tok_tile),
        ]:
            row = QHBoxLayout()
            lbl = QLabel(label_text)
            lbl.setFixedWidth(80)
            row.addWidget(lbl)
            row.addWidget(widget)
            token_layout.addLayout(row)

        btn_rescan = QPushButton("Re-scan with new patterns")
        btn_rescan.clicked.connect(self._run_discovery)
        token_layout.addWidget(btn_rescan)

        layout.addWidget(self._token_group)
        self._token_group.toggled.connect(self._on_token_group_toggled)
        self._on_token_group_toggled(False)

        layout.addStretch()

        # ── Action buttons (pinned below scroll) ──
        action_row = QHBoxLayout()
        action_row.addStretch()
        self._btn_compress = QPushButton("Compress")
        self._btn_compress.setEnabled(False)
        self._btn_compress.clicked.connect(self.accept)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        action_row.addWidget(self._btn_compress)
        action_row.addWidget(btn_cancel)
        outer.addLayout(action_row)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def compress_config(self) -> CompressConfig:
        """Materialize all dialog state into a CompressConfig."""
        output_dir = None
        if self._output_edit.text().strip():
            output_dir = Path(self._output_edit.text().strip())

        is_manual = self._manual_radio.isChecked()

        # Gather selected channels
        selected_channels: set[str] = set()
        if is_manual:
            for ch_id, cfg in self._channel_configs.items():
                if cfg.checkbox.isChecked():
                    selected_channels.add(ch_id)
        else:
            for i in range(self._ch_list.count()):
                item = self._ch_list.item(i)
                if item.checkState() == Qt.Checked:
                    selected_channels.add(item.data(Qt.UserRole))

        # Gather layer assignments from manual mode
        layer_assignments: dict[str, LayerAssignment] | None = None
        if is_manual:
            layer_assignments = {}
            for ch_id, cfg in self._channel_configs.items():
                if cfg.checkbox.isChecked():
                    layer_assignments[ch_id] = LayerAssignment(
                        layer_type=LayerType(cfg.type_combo.currentText().lower()),
                        name=cfg.name_edit.text().strip() or channel_display_name(ch_id),
                    )

        # Unchecked means no stitching at all for this surface (Import agrees;
        # the TCSPC tab instead uses a 1x1 config — that divergence is
        # deliberate and not unified here).
        tile_config = (
            self._stitch_widget.tile_config()
            if self._stitch_check.isChecked()
            else None
        )

        # Dataset check states + name overrides
        checked_names: set[str] = set()
        dataset_name_overrides: dict[str, str] = {}
        for i in range(self._ds_list.count()):
            item = self._ds_list.item(i)
            original_name = item.data(Qt.UserRole)
            if item.checkState() == Qt.Checked:
                checked_names.add(original_name)
            display_name = item.text()
            if display_name != original_name:
                dataset_name_overrides[original_name] = display_name

        gui_states: dict[str, DatasetGuiState] = {}
        for ds in self._datasets:
            gui_states[ds.name] = DatasetGuiState(
                checked=ds.name in checked_names,
            )

        flim_params: dict | None = None
        if self._flim_group.isChecked():
            channel_calibrations: dict[str, dict[str, float]] = {}
            for ch_id, cal in self._channel_calibrations.items():
                ch_name = channel_display_name(ch_id)
                channel_calibrations[ch_name] = {
                    "phase": cal.phase_spin.value(),
                    "modulation": cal.mod_spin.value(),
                }
            flim_params = {
                "frequency_mhz": self._flim_freq.value(),
                "channel_calibrations": channel_calibrations,
                "bin_dimensions": {
                    "x_dim": self._bin_x.value(),
                    "y_dim": self._bin_y.value(),
                    "t_dim": self._bin_t.value(),
                    "dtype": self._bin_dtype.currentText(),
                    "dim_order": self._bin_dim_order.currentText(),
                    "header_bytes": self._bin_header.value(),
                },
            }

        # Re-resolve each DatasetSpec.output_path against the current
        # Output field. Discovery bakes output_path at scan time, but
        # the user can edit Output afterward without re-running
        # discovery — this materialization is the single point where
        # the typed value becomes authoritative. Without this, edits to
        # the Output field are silently discarded and .h5 files land
        # at the auto-fill location (the parent of the source dir).
        datasets = list(self._datasets)
        if output_dir is not None:
            datasets = [
                replace(ds, output_path=output_dir / f"{ds.name}.h5")
                for ds in datasets
            ]

        infile_scheme: ImportScheme | None = None
        has_z = self._has_z()
        storage = self._storage_form.choice() if has_z else None
        if self._is_infile_mode():
            from percell4.domain.io.infile import with_storage, with_z_method

            scheme = self._review.scheme()
            infile_scheme = (
                with_storage(scheme, storage)
                if storage is not None
                else with_z_method(scheme, self._storage_z_method())
            )
            datasets = []
            gui_states = {}
        z_step = (
            self._storage_form.z_step_um() if has_z and not self._is_infile_mode() else None
        )

        return CompressConfig(
            z_project_method=self._storage_z_method(),
            storage=storage,
            z_step_um=z_step,
            token_config=self._current_token_config(),
            output_dir=output_dir,
            selected_channels=selected_channels,
            tile_config=tile_config,
            datasets=datasets,
            gui_states=gui_states,
            layer_assignments=layer_assignments,
            dataset_name_overrides=dataset_name_overrides,
            flim_params=flim_params,
            creation_bin=int(self._creation_bin_spin.value()),
            infile_scheme=infile_scheme,
        )

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _on_browse_source(self) -> None:
        start_dir = self._project_dir or ""
        path = QFileDialog.getExistingDirectory(
            self, "Select Source Directory", start_dir
        )
        if not path:
            return
        self._source_edit.setText(path)
        if not self._output_edit.text().strip():
            self._output_edit.setText(str(Path(path).parent))
        self._set_selection([Path(path)])

    def _on_browse_files(self) -> None:
        paths, _ = open_file_names(
            self, "Select Image Files", self._project_dir or "", _IMAGE_FILTER
        )
        if not paths:
            return
        files = [Path(p) for p in paths]
        parent = files[0].parent
        self._source_edit.setText(
            str(files[0]) if len(files) == 1 else f"{len(files)} files in {parent}"
        )
        if not self._output_edit.text().strip():
            self._output_edit.setText(str(parent))
        self._set_selection(files)

    def _set_selection(self, paths: list[Path]) -> None:
        """Adopt a new source selection, suggest a mode, then discover."""
        from percell4.adapters.infile_scan import expand_selection, tiff_plane_count
        from percell4.domain.io.infile import preclassify

        self._selection = list(paths)
        try:
            self._stage_one = preclassify(expand_selection(paths), tiff_plane_count)
        except Exception as e:  # noqa: BLE001 - a bad selection is reported, not raised
            self._stage_one = None
            self._suggestion_label.setText(f"Could not read the selection: {e}")
        else:
            self._apply_suggestion(self._stage_one)
        self._run_discovery()

    def _apply_suggestion(self, stage: StageOneResult) -> None:
        """Preselect the suggested mode and say why. The combo stays the override.

        Stage one cannot tell a Tokenless folder from a Flat one, so a user
        who picked Tokenless keeps it when the suggestion is a token mode.
        A selection with no image files at its top level leaves the mode as
        it is (a Subdirectory root holds its images in child folders).
        """
        suggested = stage.suggested_mode
        n_multi, n_single = len(stage.candidates), len(stage.legacy)
        if not n_multi and not n_single:
            self._suggestion_label.setText("")
            return
        keeps_tokenless = (
            suggested is not DiscoveryMode.INFILE
            and self._discovery_combo.currentIndex() == _TOKENLESS_INDEX
        )
        switch = not keeps_tokenless
        if switch:
            self._discovery_combo.blockSignals(True)
            self._discovery_combo.setCurrentIndex(_MODE_INDEX[suggested])
            self._discovery_combo.blockSignals(False)
            self._apply_mode_visibility()
        if suggested is DiscoveryMode.INFILE:
            why = f"{n_multi} multi-plane file{'s' if n_multi != 1 else ''}"
            if n_single:
                why += f"; {n_single} single-plane file(s) are left out"
        else:
            why = f"{n_single} single-plane file{'s' if n_single != 1 else ''}"
            if n_multi:
                why += (
                    f"; {n_multi} multi-plane file(s) are left out"
                    " (choose In-file to import them)"
                )
        mode = _MODE_LABELS[suggested] if switch else "Tokenless (kept)"
        self._suggestion_label.setText(f"Suggested: {mode} — {why}.")

    def _on_browse_output(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Select Output Directory", self._output_edit.text()
        )
        if path:
            self._output_edit.setText(path)

    def _on_discovery_mode_changed(self, index: int) -> None:
        self._apply_mode_visibility()
        if self._source_edit.text().strip():
            self._run_discovery()

    def _is_infile_mode(self) -> bool:
        return self._discovery_combo.currentIndex() == _INFILE_INDEX

    def _apply_mode_visibility(self) -> None:
        """Show the review table in In-file mode, the token controls otherwise."""
        infile = self._is_infile_mode()
        index = self._discovery_combo.currentIndex()
        # Tokenless mode derives the channel regex itself — the free-text token
        # patterns are irrelevant, so hide that group to avoid confusion.
        self._token_group.setVisible(not infile and index != _TOKENLESS_INDEX)
        for widget in (self._ds_group, self._ch_group, self._summary_label, self._flim_group,
                       *self._legacy_mode_widgets):
            widget.setVisible(not infile)
        self._stitch_widget.setVisible(not infile and self._stitch_check.isChecked())
        self._infile_group.setVisible(infile)
        self._update_compress_button()

    def _on_storage_changed(self) -> None:
        self._review.set_z_method(self._storage_z_method())
        self._update_compress_button()

    def _storage_z_method(self) -> str:
        """The z method the kept projections imply, for the z-count notes and
        for plans that predate the storage choice: sum when a sum projection
        is kept, else the first kept projection, else max."""
        from percell4.domain.io.projections import method_for_projection

        choice = self._storage_form.choice()
        if choice is None or not choice.projections:
            return "mip"
        if "sum" in choice.projections:
            return "sum"
        return method_for_projection(choice.projections[0])

    def _has_z(self) -> bool:
        """True when the selection has a Z axis, so the storage choice applies."""
        if self._is_infile_mode():
            return any(
                s.effective is not None and s.effective.size_z > 1
                for s in self._review.scheme().sources
            )
        return len(self._all_z_slices) > 1

    def _first_header(self) -> dict | None:
        """Shape and ImageJ z spacing of the first token-mode file, read once."""
        for ds in self._datasets:
            for f in ds.files:
                if f.path.suffix.lower() not in (".tif", ".tiff"):
                    continue
                if f.path not in self._header_cache:
                    from percell4.adapters.readers import read_tiff_metadata

                    self._header_cache[f.path] = read_tiff_metadata(f.path)
                return self._header_cache[f.path]
        return None

    def _checked_count(self, widget: QListWidget) -> int:
        return sum(
            widget.item(i).checkState() == Qt.Checked for i in range(widget.count())
        )

    def storage_estimate(self) -> tuple[int, int] | None:
        """Uncompressed ``(z-series, projections)`` bytes of the import, or None.

        In-file: from each importable source's probe. Token modes: z-count x
        channels x timepoints x the first file's plane x tile count, per
        checked dataset. Both divide the plane by the creation bin.
        """
        from percell4.domain.io.projections import estimate_storage_bytes

        choice = self._storage_form.choice()
        if choice is None or not self._has_z():
            return None
        k = max(1, int(self._creation_bin_spin.value()))
        total_z = total_p = 0
        if self._is_infile_mode():
            for src in self._review.importable_sources():
                e = src.effective
                if e is None or e.size_z <= 1:
                    continue
                z, p = estimate_storage_bytes(
                    choice, e.size_t, len(src.channel_indices), e.size_z,
                    e.size_y // k, e.size_x // k,
                )
                total_z += z
                total_p += p
            return total_z, total_p
        header = self._first_header()
        if not header or "shape" not in header:
            return None
        h, w = (int(x) for x in header["shape"][-2:])
        n_tiles = max(1, len(self._all_tiles))
        if self._manual_radio.isChecked():
            n_ch = sum(cfg.checkbox.isChecked() for cfg in self._channel_configs.values())
        else:
            n_ch = self._checked_count(self._ch_list)
        z, p = estimate_storage_bytes(
            choice, len(self._all_timepoints), n_ch, len(self._all_z_slices),
            h // k, (w * n_tiles) // k,
        )
        n_ds = max(1, self._checked_count(self._ds_list))
        return z * n_ds, p * n_ds

    def _refresh_storage(self) -> None:
        """Show the storage choice only for data with Z, and keep the review
        table, the Z step field and the size estimate current."""
        has_z = self._has_z()
        infile = self._is_infile_mode()
        form = self._storage_form
        form.setVisible(has_z)
        form.set_z_step_visible(has_z and not infile)
        if has_z and not infile:
            header = self._first_header() or {}
            form.set_detected_z_step(header.get("z_spacing_um"))
        self._review.set_storage(form.choice())
        estimate = self.storage_estimate()
        form.set_estimate(*(estimate or (None, None)))

    def _on_mode_changed(self, checked: bool) -> None:
        """Toggle between auto and manual mode."""
        is_manual = self._manual_radio.isChecked()
        self._ch_list.setVisible(not is_manual)
        self._manual_ch_panel.setVisible(is_manual)
        # Make dataset names editable in manual mode
        for i in range(self._ds_list.count()):
            item = self._ds_list.item(i)
            flags = item.flags()
            if is_manual:
                item.setFlags(flags | Qt.ItemIsEditable)
            else:
                item.setFlags(flags & ~Qt.ItemIsEditable)

    def _on_creation_bin_changed(self, _value: int) -> None:
        """Placeholder slot for the creation-bin spinner.

        The value is read in ``compress_config`` at Compress time; here it
        only updates the storage size estimate, which divides by the bin.
        """
        self._refresh_storage()

    def _on_stitch_toggled(self, checked: bool) -> None:
        self._stitch_widget.setVisible(checked)

    def _on_token_group_toggled(self, checked: bool) -> None:
        for child in self._token_group.findChildren(QWidget):
            if child is not self._token_group:
                child.setVisible(checked)

    def _on_flim_group_toggled(self, checked: bool) -> None:
        for child in self._flim_group.findChildren(QWidget):
            if child is not self._flim_group:
                child.setVisible(checked)

    def _on_select_all_datasets(self) -> None:
        self._set_list_check_state(self._ds_list, Qt.Checked)

    def _on_deselect_all_datasets(self) -> None:
        self._set_list_check_state(self._ds_list, Qt.Unchecked)

    def _on_select_all_channels(self) -> None:
        if self._manual_radio.isChecked():
            for cfg in self._channel_configs.values():
                cfg.checkbox.setChecked(True)
        else:
            self._set_list_check_state(self._ch_list, Qt.Checked)
        self._update_compress_button()

    def _on_deselect_all_channels(self) -> None:
        if self._manual_radio.isChecked():
            for cfg in self._channel_configs.values():
                cfg.checkbox.setChecked(False)
        else:
            self._set_list_check_state(self._ch_list, Qt.Unchecked)
        self._update_compress_button()

    def _set_list_check_state(
        self, list_widget: QListWidget, state: Qt.CheckState
    ) -> None:
        list_widget.blockSignals(True)
        for i in range(list_widget.count()):
            list_widget.item(i).setCheckState(state)
        list_widget.blockSignals(False)
        self._update_compress_button()

    # ------------------------------------------------------------------
    # Discovery + list population
    # ------------------------------------------------------------------

    def _current_token_config(self) -> TokenConfig:
        # Tokenless mode: return the regex synthesized from the derived channel
        # names so both discovery and import_dataset run the identical pattern.
        if (
            self._discovery_combo.currentIndex() == _TOKENLESS_INDEX
            and self._tokenless_token_config is not None
        ):
            return self._tokenless_token_config
        return TokenConfig(
            channel=self._tok_channel.text().strip() or None,
            timepoint=self._tok_timepoint.text().strip() or None,
            z_slice=self._tok_zslice.text().strip() or None,
            tile=self._tok_tile.text().strip() or None,
        )

    def _run_discovery(self) -> None:
        source = self._source_edit.text().strip()
        if not source:
            return
        if self._is_infile_mode():
            self._run_infile_discovery()
            return

        self._discovery_generation += 1
        gen = self._discovery_generation

        # A file selection discovers in the files' folder, then keeps only
        # the selected files (below). A directory selection is the root.
        explicit_files = [p for p in self._selection if not p.is_dir()]
        root = explicit_files[0].parent if explicit_files else Path(source)
        output_dir = None
        if self._output_edit.text().strip():
            output_dir = Path(self._output_edit.text().strip())

        from percell4.domain.io.discovery import (
            discover_by_subdirectory,
            discover_flat,
            discover_tokenless,
        )

        mode_idx = self._discovery_combo.currentIndex()
        try:
            if mode_idx == _TOKENLESS_INDEX:
                # Derives its own channel regex from the filenames; cache it so
                # _current_token_config threads the identical pattern to import.
                datasets, self._tokenless_token_config = discover_tokenless(
                    root, output_dir
                )
                if not datasets:
                    self._tokenless_token_config = None
                    self._datasets = []
                    self._aggregate_tokens()
                    self._populate_lists()
                    self._ds_count_label.setText(
                        "No name-suffixed TIFFs found to group"
                    )
                    return
            elif mode_idx == 0:
                self._tokenless_token_config = None
                datasets = discover_by_subdirectory(
                    root, self._current_token_config(), output_dir
                )
            else:
                self._tokenless_token_config = None
                datasets = discover_flat(
                    root, self._current_token_config(), output_dir
                )
        except Exception as e:
            self._ds_count_label.setText(f"Error: {e}")
            return

        if gen != self._discovery_generation:
            return

        self._datasets = self._filter_legacy_datasets(datasets, explicit_files)
        self._aggregate_tokens()
        self._populate_lists()

    def _filter_legacy_datasets(
        self, datasets: list[DatasetSpec], explicit_files: list[Path]
    ) -> list[DatasetSpec]:
        """Drop multi-plane files (R14) and, for a file selection, unselected files.

        A dataset whose files all survive is returned unchanged, so a
        single-plane import stays byte-identical.
        """
        from percell4.adapters.infile_scan import tiff_plane_count

        keep_only = set(explicit_files) if explicit_files else None
        excluded: list[Path] = []
        out: list[DatasetSpec] = []
        for ds in datasets:
            files = []
            for f in ds.files:
                if keep_only is not None and f.path not in keep_only:
                    continue
                if f.path.suffix.lower() in (".tif", ".tiff"):
                    try:
                        if tiff_plane_count(f.path) > 1:
                            excluded.append(f.path)
                            continue
                    except Exception:  # noqa: BLE001 - unreadable: keep today's behaviour
                        pass
                files.append(f)
            if len(files) == len(ds.files):
                out.append(ds)
            elif files:
                out.append(replace(ds, files=tuple(files), scan_result=None))
        self._legacy_excluded = excluded
        return out

    def _run_infile_discovery(self) -> None:
        """Probe the selection through Bio-Formats and show the review table."""
        from percell4.adapters.infile_scan import (
            expand_selection,
            scan_selection,
            tiff_plane_count,
        )
        from percell4.domain.errors import BioformatsUnavailableError, JavaUnavailableError
        from percell4.domain.io.infile import preclassify

        self._discovery_generation += 1
        gen = self._discovery_generation
        selection = self._selection or [Path(self._source_edit.text().strip())]

        try:
            stage = preclassify(
                expand_selection(selection), tiff_plane_count, DiscoveryMode.INFILE
            )
        except Exception as e:  # noqa: BLE001
            self._review.set_notes([(str(selection[0]), f"could not read: {e}")])
            self._update_compress_button()
            return
        if not stage.candidates:
            self._review.set_scheme(ImportScheme(excluded=stage.excluded))
            self._update_compress_button()
            return

        def notes(reason: str) -> list[tuple[str, str]]:
            return [(p.name, reason) for p in stage.candidates] + [
                (e.path.name, e.reason) for e in stage.excluded
            ]

        # OME-Zarr stores are read natively; only other in-file formats need
        # Java and Bio-Formats.
        needs_java = any(not is_zarr_path(p) for p in stage.candidates)
        if needs_java and not self._java_ready() and not self._java_setup(self):
            self._review.set_notes(notes("needs Java"))
            self._update_compress_button()
            return

        progress = progress_dialog(
            self,
            "Reading file headers...",
            "Cancel",
            0,
            len(stage.candidates),
            modality=blocking_progress_modality(),
        )
        progress.setMinimumDuration(0)
        seen = [0]

        def on_file(_record) -> None:
            seen[0] += 1
            progress.setValue(seen[0])

        try:
            outcome = scan_selection(
                selection,
                reader_factory=self._reader_factory,
                mode=DiscoveryMode.INFILE,
                z_method=self._storage_z_method(),
                on_file=on_file,
                is_cancelled=progress.wasCanceled,
            )
        except (JavaUnavailableError, BioformatsUnavailableError) as e:
            progress.close()
            self._review.set_notes(notes(f"needs Java: {e}"))
            self._update_compress_button()
            return
        finally:
            progress.close()

        if gen != self._discovery_generation:
            return  # a newer selection or mode started meanwhile
        if outcome.cancelled or outcome.scheme is None:
            self._review.set_notes(notes("scan cancelled"))
        else:
            self._review.set_scheme(outcome.scheme)
        self._update_compress_button()

    def _aggregate_tokens(self) -> None:
        """Collect all unique channels, tiles, z-slices, timepoints."""
        channels: set[str] = set()
        tiles: set[str] = set()
        z_slices: set[str] = set()
        timepoints: set[str] = set()

        for ds in self._datasets:
            if ds.scan_result:
                channels.update(ds.scan_result.channels)
                tiles.update(ds.scan_result.tiles)
                z_slices.update(ds.scan_result.z_slices)
                timepoints.update(ds.scan_result.timepoints)
            else:
                for f in ds.files:
                    if "channel" in f.tokens:
                        channels.add(f.tokens["channel"])
                    if "tile" in f.tokens:
                        tiles.add(f.tokens["tile"])
                    if "z_slice" in f.tokens:
                        z_slices.add(f.tokens["z_slice"])
                    if "timepoint" in f.tokens:
                        timepoints.add(f.tokens["timepoint"])

        self._all_channels = sorted(channels, key=_sort_key)
        self._all_tiles = sorted(tiles, key=_sort_key)
        self._all_z_slices = sorted(z_slices, key=_sort_key)
        self._all_timepoints = sorted(timepoints, key=_sort_key)

    def _populate_lists(self) -> None:
        is_manual = self._manual_radio.isChecked()

        # ── Datasets ──
        self._ds_list.blockSignals(True)
        self._ds_list.clear()
        for ds in self._datasets:
            item = QListWidgetItem(ds.name)
            flags = item.flags() | Qt.ItemIsUserCheckable
            if is_manual:
                flags |= Qt.ItemIsEditable
            item.setFlags(flags)
            item.setCheckState(Qt.Checked)
            item.setData(Qt.UserRole, ds.name)  # original name
            self._ds_list.addItem(item)
        self._ds_list.blockSignals(False)

        n = len(self._datasets)
        self._ds_count_label.setText(
            f"{n} dataset{'s' if n != 1 else ''}"
        )

        # ── Channels (auto mode list) ──
        self._ch_list.blockSignals(True)
        self._ch_list.clear()
        for ch in self._all_channels:
            item = QListWidgetItem(channel_display_name(ch))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            item.setData(Qt.UserRole, ch)
            self._ch_list.addItem(item)
        self._ch_list.blockSignals(False)

        # ── Channels (manual mode panel) ──
        # Built before the reference combo so the combo can read each
        # channel's (possibly renamed) name from its name_edit.
        self._build_manual_channel_panel()

        # ── Registration reference-channel combo ──
        # Seeded from each channel's CURRENT name (see _refresh_reference_combo).
        self._refresh_reference_combo()

        # ── FLIM per-channel calibration rows ──
        self._build_calibration_panel()

        # ── Summary ──
        parts = []
        if self._all_tiles:
            t = self._all_tiles
            parts.append(f"Tiles: {len(t)} (s{t[0]}\u2013s{t[-1]})")
        if self._all_z_slices:
            z = self._all_z_slices
            parts.append(f"Z-slices: {len(z)} (z{z[0]}\u2013z{z[-1]})")
        if self._all_timepoints:
            tp = self._all_timepoints
            parts.append(f"Timepoints: {len(tp)} (t{tp[0]}\u2013t{tp[-1]})")
        if not parts:
            parts.append("No tiles, z-slices, or timepoints detected")
        if self._legacy_excluded:
            n = len(self._legacy_excluded)
            parts.append(
                f"{n} multi-plane file{'s' if n != 1 else ''} left out "
                "(multi-plane file, import with In-file mode)"
            )
        self._summary_label.setText("    ".join(parts))

        # Auto-enable stitching if tiles detected
        if self._all_tiles and not self._stitch_check.isChecked():
            self._stitch_check.setChecked(True)

        # Auto-enable FLIM section if any .bin files detected
        has_bin = any(
            f.path.suffix.lower() == ".bin"
            for ds in self._datasets
            for f in ds.files
        )
        if has_bin and not self._flim_group.isChecked():
            self._flim_group.setChecked(True)

        self._update_compress_button()

    def _build_manual_channel_panel(self) -> None:
        """Build the manual mode channel configuration widgets."""
        # Clear existing
        while self._manual_ch_container.count():
            child = self._manual_ch_container.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        self._channel_configs.clear()

        for ch in self._all_channels:
            row_widget = QWidget()
            row = QHBoxLayout(row_widget)
            row.setContentsMargins(0, 2, 0, 2)

            cb = QCheckBox(channel_display_name(ch))
            cb.setChecked(True)
            cb.toggled.connect(self._update_compress_button)
            row.addWidget(cb)

            name_edit = QLineEdit(channel_display_name(ch))
            name_edit.setPlaceholderText("Name")
            name_edit.setFixedWidth(100)
            # A rename here is the name the importer keys its registration
            # tiles by, so keep the reference-channel combo in sync live.
            name_edit.textChanged.connect(self._refresh_reference_combo)
            row.addWidget(name_edit)

            type_combo = QComboBox()
            type_combo.addItems(["Channel", "Segmentation", "Mask"])
            type_combo.setFixedWidth(110)
            row.addWidget(type_combo)

            row.addStretch()

            self._manual_ch_container.addWidget(row_widget)
            self._channel_configs[ch] = _ChannelConfig(
                checkbox=cb, name_edit=name_edit, type_combo=type_combo
            )

    def _refresh_reference_combo(self) -> None:
        """Rebuild the registration reference-channel combo from each channel's
        CURRENT name.

        In Manual mode a channel may be renamed (ch00 -> "ER"); the importer
        keys registration tiles by that renamed layer name, so the reference
        must be selectable by the same name — the chXX id no longer exists
        post-rename. Falls back to ``chXX`` for an unnamed channel (and in Auto
        mode, where the name_edits hold their chXX defaults). itemData carries
        the name verbatim (not an index), matching the round-trip convention.
        Preserves the user's pick by channel position across a rename, or by
        text for a free-typed entry. Wired to each name_edit's textChanged in
        _build_manual_channel_panel so it stays live.
        """
        combo = self._stitch_reference
        prev_text = combo.currentText().strip()
        prev_idx = combo.currentIndex()  # -1 when the text was free-typed
        combo.blockSignals(True)
        combo.clear()
        for ch in self._all_channels:
            cfg = self._channel_configs.get(ch)
            name = (cfg.name_edit.text().strip() if cfg else "") or channel_display_name(ch)
            combo.addItem(name, name)
        if 0 <= prev_idx < combo.count():
            combo.setCurrentIndex(prev_idx)  # same channel position, new name
        elif prev_text:
            idx = combo.findText(prev_text)
            if idx >= 0:
                combo.setCurrentIndex(idx)
            else:
                combo.setCurrentText(prev_text)  # genuine free-text pick
        combo.blockSignals(False)

    def _build_calibration_panel(self) -> None:
        """Build per-channel phase/modulation widgets for FLIM calibration.

        Mirrors the historical ImportDialog._discover_channels layout:
        one QGroupBox per channel with "Phase:" + "Modulation:" form rows.
        Calibration is applied as a Cartesian rotation in
        ``compute_phasor`` using the values stored as
        ``flim_cal_phase_<ch>`` / ``flim_cal_mod_<ch>`` HDF5 metadata —
        the same convention flimfret/preprocessing.py uses for its
        phi_cal / m_cal correction.
        """
        while self._flim_cal_container.count():
            child = self._flim_cal_container.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        self._channel_calibrations.clear()

        if not self._all_channels:
            return

        for ch in self._all_channels:
            ch_name = channel_display_name(ch)
            group = QGroupBox(f"Channel {ch_name}")
            form = QFormLayout(group)

            phase_spin = QDoubleSpinBox()
            phase_spin.setRange(-6.283, 6.283)
            phase_spin.setValue(0.0)
            phase_spin.setDecimals(4)
            phase_spin.setSuffix(" rad")
            form.addRow("Phase:", phase_spin)

            mod_spin = QDoubleSpinBox()
            mod_spin.setRange(0.0, 10.0)
            mod_spin.setValue(1.0)
            mod_spin.setDecimals(4)
            form.addRow("Modulation:", mod_spin)

            self._flim_cal_container.addWidget(group)
            self._channel_calibrations[ch] = _CalibrationConfig(
                phase_spin=phase_spin, mod_spin=mod_spin
            )

        # New widgets default to visible — re-apply collapsed state if the
        # FLIM group is currently unchecked so they don't appear orphaned.
        self._on_flim_group_toggled(self._flim_group.isChecked())

    def _update_compress_button(self) -> None:
        self._refresh_storage()
        # Data with Z needs something kept (a projection or the z-series).
        storage_ok = not self._has_z() or self._storage_form.choice() is not None
        if self._is_infile_mode():
            self._btn_compress.setEnabled(
                storage_ok and bool(self._review.importable_sources())
            )
            return
        any_ds = any(
            self._ds_list.item(i).checkState() == Qt.Checked
            for i in range(self._ds_list.count())
        )
        if self._manual_radio.isChecked():
            any_ch = any(
                cfg.checkbox.isChecked() for cfg in self._channel_configs.values()
            )
        else:
            any_ch = any(
                self._ch_list.item(i).checkState() == Qt.Checked
                for i in range(self._ch_list.count())
            )
        self._btn_compress.setEnabled(storage_ok and any_ds and any_ch)

    # ------------------------------------------------------------------
    # Styling
    # ------------------------------------------------------------------



class _ChannelConfig:
    """Holds the manual-mode widgets for a single channel."""

    __slots__ = ("checkbox", "name_edit", "type_combo")

    def __init__(
        self, checkbox: QCheckBox, name_edit: QLineEdit, type_combo: QComboBox
    ) -> None:
        self.checkbox = checkbox
        self.name_edit = name_edit
        self.type_combo = type_combo


class _CalibrationConfig:
    """Holds the FLIM phase/modulation widgets for a single channel."""

    __slots__ = ("phase_spin", "mod_spin")

    def __init__(self, phase_spin: QDoubleSpinBox, mod_spin: QDoubleSpinBox) -> None:
        self.phase_spin = phase_spin
        self.mod_spin = mod_spin


def _sort_key(val: str) -> tuple[int, str]:
    """Sort token values numerically if possible, else alphabetically."""
    try:
        return (0, str(int(val)).zfill(10))
    except ValueError:
        return (1, val)

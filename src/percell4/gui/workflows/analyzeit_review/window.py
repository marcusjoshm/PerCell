"""The AnalyzeIt review tool window and its napari renderer (plan decision KTD16).

The tool window is unparented and stands beside PerCell's viewer. It lists the stop's
fields with a mark on each one opened, says the scope and repairs in words, and has the
circle tools, a words box, and the two buttons. A final review also shows the report, its
plots and the summary table; a proposal shows the proposal and its evidence.

The renderer puts one field at a time in the viewer: the channels as images, the label
layers as outlines, and one shapes layer for the circles. It has no label editing.

:func:`open_review` builds both and starts the controller. The launcher keeps the window
alive and tears it down through :meth:`ReviewWindow.shutdown`, which is safe to call twice.
"""

from __future__ import annotations

import csv
import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np
from qtpy.QtCore import QEvent, QObject, Qt, QTimer, Signal
from qtpy.QtGui import QKeySequence, QPixmap
from qtpy.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QShortcut,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from percell4.config import viewer_presets as vp
from percell4.gui import theme
from percell4.gui._dialog_utils import make_freestanding, message_box, wrap_in_scroll
from percell4.gui.workflows.analyzeit_review.controller import ReviewController
from percell4.workflows.analyzeit_review.session import Circle, FieldImages, ReviewSession

logger = logging.getLogger(__name__)

POLL_MS = 1000
TICK_MS = 1000
DRAFT_MS = 20_000

CIRCLES_LAYER = f"{vp.ANALYZEIT_REVIEW_LAYER_PREFIX}circles"
PREVIOUS_LAYER = f"{vp.ANALYZEIT_REVIEW_LAYER_PREFIX}previous circles"
MESSAGE_LAYER = f"{vp.ANALYZEIT_REVIEW_LAYER_PREFIX}message"
LABELS_LAYER = f"{vp.ANALYZEIT_REVIEW_LAYER_PREFIX}cells"
REMOVED_LAYER = f"{vp.ANALYZEIT_REVIEW_LAYER_PREFIX}cells removed"

KIND_COLORS = {
    "problem": vp.ANALYZEIT_REVIEW_PROBLEM_COLOR,
    "correct": vp.ANALYZEIT_REVIEW_CORRECT_COLOR,
}
MODES = {"lasso": "add_polygon_lasso", "ellipse": "add_ellipse", "polygon": "add_polygon"}
KEYS_HELP = (
    "Keys: N next field, B previous field, 1 problem circle, 2 correct circle, "
    "L lasso, E ellipse, P polygon, Ctrl+Z undo last circle, Delete removes the "
    "selected circle."
)
_INPUT_EVENTS = frozenset(
    {QEvent.KeyPress, QEvent.MouseButtonPress, QEvent.MouseMove, QEvent.Wheel}
)


# ── The napari side ─────────────────────────────────────────────────────────────────────


class NapariReviewRenderer:
    """Shows one field in PerCell's viewer. Satisfies ``ReviewRenderer``."""

    def __init__(self, viewer_win) -> None:
        self._viewer_win = viewer_win
        self._hidden: dict[str, bool] = {}
        self._kind = "problem"
        self._shape_type = "lasso"
        self._on_drawn = None
        self._key_actions: dict[str, Any] = {}

    def bind(self, on_drawn, key_actions: Mapping[str, Any]) -> None:
        """Call ``on_drawn`` when a circle is added, and bind the review keys on the viewer."""
        self._on_drawn = on_drawn
        self._key_actions = dict(key_actions)

    @property
    def _viewer(self):
        return self._viewer_win.viewer

    def _layers(self):
        return self._viewer.layers

    def is_viewer_alive(self) -> bool:
        return bool(self._viewer_win.is_viewer_alive())

    def hide_other_layers(self) -> None:
        for layer in list(self._layers()):
            if not layer.name.startswith(vp.ANALYZEIT_REVIEW_LAYER_PREFIX):
                self._hidden[layer.name] = bool(layer.visible)
                layer.visible = False

    def restore_other_layers(self) -> None:
        if not self.is_viewer_alive():
            self._hidden.clear()
            return
        layers = self._layers()
        for name, visible in self._hidden.items():
            if name in layers:
                layers[name].visible = visible
        self._hidden.clear()

    def suspend_selected_label_forwarding(self) -> None:
        self._viewer_win.suspend_selected_label_forwarding()

    def resume_selected_label_forwarding(self) -> None:
        self._viewer_win.resume_selected_label_forwarding()

    def clear_review_layers(self) -> None:
        if not self.is_viewer_alive():
            return
        layers = self._layers()
        for layer in list(layers):
            if layer.name.startswith(vp.ANALYZEIT_REVIEW_LAYER_PREFIX):
                layers.remove(layer)

    def show_field(
        self, images: FieldImages, circles: list[Circle], previous: list[dict[str, Any]]
    ) -> None:
        self.clear_review_layers()
        win = self._viewer_win
        first = None
        for channel, data in images.channels.items():
            win.add_image(data, name=f"{vp.ANALYZEIT_REVIEW_LAYER_PREFIX}{images.name} {channel}")
            first = first or self._layers()[-1]
        for array, name, opacity in (
            (images.removed, REMOVED_LAYER, vp.ANALYZEIT_REVIEW_REMOVED_OPACITY),
            (images.labels, LABELS_LAYER, vp.ANALYZEIT_REVIEW_LABELS_OPACITY),
        ):
            if array is None:
                continue
            layer = self._viewer.add_labels(
                np.asarray(array), name=name, opacity=opacity, blending="translucent"
            )
            layer.contour = vp.ANALYZEIT_REVIEW_OUTLINE_WIDTH
            layer.editable = False
        place = {"scale": first.scale, "translate": first.translate} if first else {}
        if previous:
            self._add_previous(previous, place)
        shapes = self._viewer.add_shapes(
            name=CIRCLES_LAYER,
            ndim=2,
            edge_width=vp.ANALYZEIT_REVIEW_EDGE_WIDTH,
            face_color=list(vp.ANALYZEIT_REVIEW_FACE_COLOR),
            features={"kind": np.array([], dtype=object),
                      "drawn_as": np.array([], dtype=object)},
            **place,
        )
        self._shapes = shapes
        self.set_circles(circles)
        self.set_circle_kind(self._kind)
        self.set_shape_mode(self._shape_type)
        shapes.events.data.connect(self._on_data)
        for key, action in self._key_actions.items():
            shapes.bind_key(key, _key_handler(action), overwrite=True)
        self._viewer.layers.selection.active = shapes
        self._viewer.reset_view()

    def _add_previous(self, previous: list[dict[str, Any]], place: Mapping[str, Any]) -> None:
        circles = [c for c in previous if len(c.get("vertices") or []) >= 3]
        if not circles:
            return
        layer = self._viewer.add_shapes(
            [np.asarray(c["vertices"], dtype=float) for c in circles],
            shape_type=[_napari_type(c.get("shape_type", "polygon")) for c in circles],
            name=PREVIOUS_LAYER,
            edge_color=vp.ANALYZEIT_REVIEW_PREVIOUS_COLOR,
            edge_width=vp.ANALYZEIT_REVIEW_EDGE_WIDTH,
            face_color=list(vp.ANALYZEIT_REVIEW_FACE_COLOR),
            features={"outcome": [c.get("outcome", "") for c in circles]},
            text={"string": "{outcome}", "color": vp.ANALYZEIT_REVIEW_PREVIOUS_COLOR},
            opacity=0.8,
            **place,
        )
        layer.editable = False

    def _circles_layer(self):
        layer = getattr(self, "_shapes", None)
        if layer is None or not self.is_viewer_alive() or layer not in self._layers():
            return None
        return layer

    def _on_data(self, event) -> None:
        if getattr(event, "action", "added") == "added" and self._on_drawn is not None:
            self._on_drawn()

    def drawn_circles(self) -> list[dict[str, Any]]:
        layer = self._circles_layer()
        if layer is None:
            return []
        kinds = list(layer.features.get("kind", []))
        drawn_as = list(layer.features.get("drawn_as", []))
        result = []
        for index, (data, napari_type) in enumerate(zip(layer.data, layer.shape_type)):
            vertices = np.asarray(data, dtype=float)[:, -2:].tolist()
            if napari_type == "ellipse":
                shape_type = "ellipse"
            elif napari_type in ("polygon", "rectangle", "path"):
                shape_type = "lasso" if (index < len(drawn_as) and drawn_as[index] == "lasso") \
                    else "polygon"
            else:
                continue  # a line encloses nothing
            if len(vertices) < 3:
                continue
            kind = kinds[index] if index < len(kinds) and kinds[index] in KIND_COLORS \
                else "problem"
            result.append({"kind": kind, "shape_type": shape_type, "vertices": vertices})
        return result

    def set_circles(self, circles: list[Circle]) -> None:
        layer = self._circles_layer()
        if layer is None:
            return
        with layer.events.data.blocker():
            layer.data = []
            if circles:
                layer.add(
                    [np.asarray(c.vertices, dtype=float) for c in circles],
                    shape_type=[_napari_type(c.shape_type) for c in circles],
                    edge_color=[KIND_COLORS[c.kind] for c in circles],
                )
                layer.features = {
                    "kind": np.array([c.kind for c in circles], dtype=object),
                    "drawn_as": np.array([c.shape_type for c in circles], dtype=object),
                }

    def set_circle_kind(self, kind: str) -> None:
        self._kind = kind
        layer = self._circles_layer()
        if layer is None:
            return
        layer.feature_defaults["kind"] = kind
        layer.current_edge_color = KIND_COLORS[kind]

    def set_shape_mode(self, mode: str) -> None:
        self._shape_type = mode
        layer = self._circles_layer()
        if layer is None:
            return
        layer.feature_defaults["drawn_as"] = mode
        layer.mode = MODES[mode]

    def remove_last_circle(self) -> None:
        layer = self._circles_layer()
        if layer is not None and len(layer.data):
            layer.selected_data = {len(layer.data) - 1}
            layer.remove_selected()

    def remove_selected_circles(self) -> None:
        layer = self._circles_layer()
        if layer is not None and layer.selected_data:
            layer.remove_selected()

    def show_message_at(self, message: str, at: list[float]) -> None:
        if not self.is_viewer_alive():
            return
        layers = self._layers()
        if MESSAGE_LAYER in layers:
            layers.remove(layers[MESSAGE_LAYER])
        layer = self._circles_layer()
        place = {"scale": layer.scale, "translate": layer.translate} if layer else {}
        self._viewer.add_points(
            [at],
            name=MESSAGE_LAYER,
            size=4,
            face_color=vp.ANALYZEIT_REVIEW_MESSAGE_COLOR,
            features={"message": [message]},
            text={"string": "{message}", "color": vp.ANALYZEIT_REVIEW_MESSAGE_COLOR,
                  "anchor": "upper_left"},
            **place,
        )
        self._viewer.status = message
        if layer is not None:
            self._viewer.layers.selection.active = layer


def _napari_type(shape_type: str) -> str:
    return "ellipse" if shape_type == "ellipse" else "polygon"


def _key_handler(action):
    def handler(_layer):
        action()

    return handler


# ── The tool window ─────────────────────────────────────────────────────────────────────


class _ActivityFilter(QObject):
    """Counts input on the tool window and the viewer window as review activity."""

    def __init__(self, window: ReviewWindow) -> None:
        super().__init__(window)
        self._window = window

    def eventFilter(self, obj, event):  # noqa: N802 - Qt's name
        if event.type() in _INPUT_EVENTS:
            self._window.note_input()
        return False


class ReviewWindow(QWidget):
    """The tool window. Satisfies ``ReviewView``."""

    review_closed = Signal()

    def __init__(self, order: Path, viewer_win, launcher=None, renderer=None) -> None:
        super().__init__(None)
        self.setWindowFlag(Qt.Window, True)
        self.order = Path(order)
        self._viewer_win = viewer_win
        self._session: ReviewSession | None = None
        self._shutting_down = False
        self.setWindowTitle(f"AnalyzeIt review: {self.order.name}")
        self.renderer = renderer if renderer is not None else NapariReviewRenderer(viewer_win)
        self.controller = ReviewController(self.order, self.renderer, self, lock=launcher)
        self._build()
        self.renderer.bind(
            self.controller.circle_drawn,
            {
                "n": lambda: self.controller.step_field(1),
                "b": lambda: self.controller.step_field(-1),
                "1": lambda: self._choose_kind("problem"),
                "2": lambda: self._choose_kind("correct"),
                "l": lambda: self._choose_mode("lasso"),
                "e": lambda: self._choose_mode("ellipse"),
                "p": lambda: self._choose_mode("polygon"),
                "Control-z": self.controller.undo_last_circle,
            },
        )
        self._timers = []
        for interval, slot in (
            (POLL_MS, self.controller.poll),
            (TICK_MS, self._tick),
            (DRAFT_MS, self.controller.save_draft),
        ):
            timer = QTimer(self)
            timer.setInterval(interval)
            timer.timeout.connect(slot)
            self._timers.append(timer)
        self._activity = _ActivityFilter(self)

    # ── Layout ──

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet(f"font-weight: bold; color: {theme.TEXT};")
        self.refusal = QLabel()
        self.refusal.setWordWrap(True)
        self.refusal.setStyleSheet("color: #ffb347;")
        self.refusal.hide()
        outer.addWidget(self.banner)
        outer.addWidget(self.refusal)

        self.splitter = QSplitter(Qt.Horizontal)
        outer.addWidget(self.splitter, 1)

        side = QWidget()
        column = QVBoxLayout(side)
        self.heading = QLabel()
        self.heading.setWordWrap(True)
        self.heading.setStyleSheet("font-weight: bold;")
        self.scope = QLabel()
        self.scope.setWordWrap(True)
        self.notes = QLabel()
        self.notes.setWordWrap(True)
        for widget in (self.heading, self.scope, self.notes):
            column.addWidget(widget)

        self.fields_box = QGroupBox("Fields (a mark means opened at full size)")
        fields_layout = QVBoxLayout(self.fields_box)
        self.field_list = QListWidget()
        self.field_list.itemClicked.connect(self._on_field_clicked)
        fields_layout.addWidget(self.field_list)
        nav = QHBoxLayout()
        self.btn_previous = QPushButton("Previous field")
        self.btn_next = QPushButton("Next field")
        self.btn_region = QPushButton("Next region")
        self.btn_region.setToolTip("Jump to the next field with a circle from the last attempt.")
        self.btn_previous.clicked.connect(self._on_previous)
        self.btn_next.clicked.connect(self._on_next)
        self.btn_region.clicked.connect(self._on_region)
        for button in (self.btn_previous, self.btn_next, self.btn_region):
            nav.addWidget(button)
        fields_layout.addLayout(nav)
        column.addWidget(self.fields_box)

        self.circles_box = QGroupBox("Circles")
        circles_layout = QVBoxLayout(self.circles_box)
        kinds = QHBoxLayout()
        self.kind_group = QButtonGroup(self)
        self.kind_buttons = {}
        for kind, text in (("problem", "Problem (1)"), ("correct", "Correct (2)")):
            button = QRadioButton(text)
            button.setStyleSheet(f"color: {KIND_COLORS[kind]};")
            button.toggled.connect(self._on_kind_toggled)
            self.kind_group.addButton(button)
            self.kind_buttons[kind] = button
            kinds.addWidget(button)
        self.kind_buttons["problem"].setChecked(True)
        circles_layout.addLayout(kinds)
        modes = QHBoxLayout()
        self.mode_group = QButtonGroup(self)
        self.mode_buttons = {}
        for mode, text in (("lasso", "Lasso (L)"), ("ellipse", "Ellipse (E)"),
                           ("polygon", "Polygon (P)")):
            button = QRadioButton(text)
            button.toggled.connect(self._on_mode_toggled)
            self.mode_group.addButton(button)
            self.mode_buttons[mode] = button
            modes.addWidget(button)
        self.mode_buttons["lasso"].setChecked(True)
        circles_layout.addLayout(modes)
        edits = QHBoxLayout()
        self.btn_undo = QPushButton("Undo last circle")
        self.btn_delete = QPushButton("Delete selected circle")
        self.btn_undo.clicked.connect(self._on_undo)
        self.btn_delete.clicked.connect(self._on_delete)
        edits.addWidget(self.btn_undo)
        edits.addWidget(self.btn_delete)
        circles_layout.addLayout(edits)
        column.addWidget(self.circles_box)

        words_box = QGroupBox("Words")
        words_layout = QVBoxLayout(words_box)
        self.words = QPlainTextEdit()
        self.words.setPlaceholderText("What is wrong, or what to keep, in your own words.")
        self.words_field = QComboBox()
        words_layout.addWidget(self.words)
        about = QHBoxLayout()
        about.addWidget(QLabel("About:"))
        about.addWidget(self.words_field, 1)
        words_layout.addLayout(about)
        self.target_row = QWidget()
        target_layout = QHBoxLayout(self.target_row)
        target_layout.setContentsMargins(0, 0, 0, 0)
        target_layout.addWidget(QLabel("Go back to step:"))
        self.target_step = QComboBox()
        target_layout.addWidget(self.target_step, 1)
        words_layout.addWidget(self.target_row)
        self.not_coming_right = QCheckBox("Not coming right")
        self.not_coming_right.setToolTip(
            "Mark the send-back when repeated attempts are not getting closer."
        )
        words_layout.addWidget(self.not_coming_right)
        column.addWidget(words_box)

        buttons = QHBoxLayout()
        self.btn_okay = QPushButton("Okay")
        self.btn_send_back = QPushButton("Send back")
        self.btn_okay.clicked.connect(self._on_okay)
        self.btn_send_back.clicked.connect(self._on_send_back)
        buttons.addWidget(self.btn_okay)
        buttons.addWidget(self.btn_send_back)
        column.addLayout(buttons)
        keys = QLabel(KEYS_HELP)
        keys.setWordWrap(True)
        keys.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        column.addWidget(keys)
        column.addStretch(1)
        self.splitter.addWidget(wrap_in_scroll(side))

        self.detail = QTabWidget()
        self.report = QTextBrowser()
        self.plots = QWidget()
        self.plots_layout = QVBoxLayout(self.plots)
        self.table = QTableWidget()
        self.proposal = QTextBrowser()
        self.detail.addTab(self.report, "Report")
        self.detail.addTab(wrap_in_scroll(self.plots), "Plots")
        self.detail.addTab(self.table, "Summary table")
        self.detail.addTab(self.proposal, "Proposal")
        self.splitter.addWidget(self.detail)
        self.detail.hide()

        for sequence, slot in (
            ("N", self._on_next), ("B", self._on_previous),
            ("1", self._shortcut_problem), ("2", self._shortcut_correct),
            ("L", self._shortcut_lasso), ("E", self._shortcut_ellipse),
            ("P", self._shortcut_polygon), ("Ctrl+Z", self._on_undo),
            ("Delete", self._on_delete),
        ):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.WindowShortcut)
            shortcut.activated.connect(slot)
        self.resize(560, 820)

    # ── Starting and stopping ──

    def start(self) -> None:
        QApplication.instance().installEventFilter(self._activity)
        self.controller.start()
        for timer in self._timers:
            timer.start()

    def shutdown(self) -> None:
        """Save the draft, give the viewer and launcher back, close. Safe to call twice."""
        if self._shutting_down:
            return
        self._shutting_down = True
        for timer in self._timers:
            timer.stop()
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self._activity)
        self.controller.close()
        self.close()
        self.review_closed.emit()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt's name
        if not self._shutting_down:
            self.shutdown()
        event.accept()

    # ── ReviewView ──

    def show_banner(self, text: str) -> None:
        self.banner.setText(text)
        self.banner.setVisible(bool(text))
        if text:
            self._session = None
            self.heading.clear()
            self.scope.clear()
            self.notes.clear()
            self.field_list.clear()
            self.detail.hide()

    def show_refusal(self, text: str | None) -> None:
        self.refusal.setText(text or "")
        self.refusal.setVisible(bool(text))

    def show_stop(self, session: ReviewSession) -> None:
        self._session = session
        bundle = session.bundle
        kind = session.kind
        if kind == "step":
            self.heading.setText(
                f"Step {bundle.get('step')}, attempt {bundle.get('attempt')}: "
                f"{bundle.get('tool', '')}"
            )
        elif kind == "final":
            self.heading.setText("Final review: the finished analysis")
        else:
            self.heading.setText("Proposal: accept or decline")
        self.scope.setText(bundle.get("scope_in_words") or "")
        notes = []
        repairs = bundle.get("repairs_since_last_stop") or []
        if repairs:
            notes.append("Repairs since the last stop: " + "; ".join(
                _repair_in_words(r) for r in repairs))
        if bundle.get("send_back_count"):
            notes.append(f"Sent back {bundle['send_back_count']} time(s) so far.")
        for key in ("note_after_send_backs", "recipe_drift"):
            if bundle.get(key):
                notes.append(str(bundle[key]))
        self.notes.setText("\n".join(notes))
        self.notes.setVisible(bool(notes))

        self.btn_okay.setText("Accept" if kind == "proposal" else "Okay")
        self.btn_send_back.setText("Decline" if kind == "proposal" else "Send back")
        has_fields = bool(session.field_names)
        self.fields_box.setVisible(has_fields)
        self.circles_box.setVisible(has_fields and kind == "step")
        self.not_coming_right.setVisible(kind == "step")
        self.btn_region.setVisible(bool(session.previous_circles))
        self.words_field.clear()
        self.words_field.addItem("The whole stop", None)
        for name in session.field_names:
            self.words_field.addItem(name, name)
        self.target_row.setVisible(kind == "final")
        self.target_step.clear()
        self.target_step.addItem("(none)", None)
        for step in bundle.get("workflow_steps") or []:
            self.target_step.addItem(step, step)
        self._show_detail(session)

    def _show_detail(self, session: ReviewSession) -> None:
        bundle = session.bundle
        if session.kind == "step":
            self.detail.hide()
            return
        self.detail.show()
        final = session.kind == "final"
        for index in range(3):
            self.detail.setTabVisible(index, final)
        self.detail.setTabVisible(3, not final)
        if final:
            self.report.setMarkdown(self._read_text(bundle.get("report")))
            self._show_plots(bundle.get("plots") or [])
            self._show_table(bundle.get("summary_table"))
            self.detail.setCurrentIndex(0)
        else:
            shown = {k: v for k, v in bundle.items()
                     if k in ("proposal", "evidence", "scope_in_words")}
            self.proposal.setPlainText(json.dumps(shown, indent=2, ensure_ascii=False))
            self.detail.setCurrentIndex(3)

    def _read_text(self, relative: str | None) -> str:
        if not relative:
            return ""
        try:
            return (self.order / relative).read_text(encoding="utf-8")
        except OSError as error:
            return f"The report cannot be read: {error}"

    def _show_plots(self, plots: list[str]) -> None:
        while self.plots_layout.count():
            item = self.plots_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        for relative in plots:
            label = QLabel()
            pixmap = QPixmap(str(self.order / relative))
            if pixmap.isNull():
                label.setText(f"{relative} cannot be shown.")
            else:
                label.setPixmap(pixmap.scaledToWidth(520, Qt.SmoothTransformation))
            self.plots_layout.addWidget(label)

    def _show_table(self, relative: str | None) -> None:
        self.table.clear()
        if not relative:
            return
        try:
            with open(self.order / relative, newline="", encoding="utf-8") as handle:
                rows = list(csv.reader(handle))
        except OSError:
            return
        if not rows:
            return
        header, body = rows[0], rows[1:]
        self.table.setColumnCount(len(header))
        self.table.setRowCount(len(body))
        self.table.setHorizontalHeaderLabels(header)
        for r, row in enumerate(body):
            for c, value in enumerate(row):
                self.table.setItem(r, c, QTableWidgetItem(value))

    def show_fields(self, session: ReviewSession, current: str | None) -> None:
        self.field_list.blockSignals(True)
        self.field_list.clear()
        for entry in session.fields:
            mark = "✓" if session.opened.get(entry.name) else "  "
            extra = []
            if entry.cells is not None:
                extra.append(f"{entry.cells} cells")
            if entry.changed:
                extra.append("changed")
            text = f"{mark} {entry.name}" + (f" ({', '.join(extra)})" if extra else "")
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, entry.name)
            self.field_list.addItem(item)
            if entry.name == current:
                self.field_list.setCurrentItem(item)
        self.field_list.blockSignals(False)

    def set_answer_enabled(self, enabled: bool) -> None:
        for widget in (self.btn_okay, self.btn_send_back, self.circles_box, self.words,
                       self.words_field, self.target_step, self.not_coming_right):
            widget.setEnabled(enabled)

    def inputs(self) -> Mapping[str, Any]:
        return {
            "words": self.words.toPlainText(),
            "words_field": self.words_field.currentData(),
            "not_coming_right": self.not_coming_right.isChecked(),
            "target_step": self.target_step.currentData(),
        }

    def set_inputs(self, session: ReviewSession) -> None:
        self.words.setPlainText(session.words)
        index = self.words_field.findData(session.words_field)
        self.words_field.setCurrentIndex(max(index, 0))
        self.not_coming_right.setChecked(session.not_coming_right)
        index = self.target_step.findData(session.target_step)
        self.target_step.setCurrentIndex(max(index, 0))

    def confirm(self, title: str, text: str) -> bool:
        answer = message_box(
            self, title, text, icon=QMessageBox.Question,
            buttons=QMessageBox.Yes | QMessageBox.No, default_button=QMessageBox.No,
        )
        return answer == QMessageBox.Yes

    def inform(self, title: str, text: str) -> None:
        message_box(self, title, text, icon=QMessageBox.Information)

    # ── Slots ──

    def _review_active(self) -> bool:
        active = QApplication.activeWindow()
        viewer = getattr(self._viewer_win, "_qt_window", None)
        return active is not None and (active is self or active is viewer)

    def note_input(self) -> None:
        self.controller.note_input(self._review_active())

    def _tick(self) -> None:
        self.controller.tick(self._review_active())

    def _on_field_clicked(self, item: QListWidgetItem) -> None:
        self.controller.show_field(item.data(Qt.UserRole))

    def _on_next(self) -> None:
        self.controller.step_field(1)

    def _on_previous(self) -> None:
        self.controller.step_field(-1)

    def _on_region(self) -> None:
        self.controller.next_region()

    def _on_undo(self) -> None:
        self.controller.undo_last_circle()

    def _on_delete(self) -> None:
        self.controller.delete_selected_circles()

    def _on_okay(self) -> None:
        self.controller.okay()

    def _on_send_back(self) -> None:
        self.controller.send_back()

    def _choose_kind(self, kind: str) -> None:
        self.kind_buttons[kind].setChecked(True)

    def _choose_mode(self, mode: str) -> None:
        self.mode_buttons[mode].setChecked(True)

    def _shortcut_problem(self) -> None:
        self._choose_kind("problem")

    def _shortcut_correct(self) -> None:
        self._choose_kind("correct")

    def _shortcut_lasso(self) -> None:
        self._choose_mode("lasso")

    def _shortcut_ellipse(self) -> None:
        self._choose_mode("ellipse")

    def _shortcut_polygon(self) -> None:
        self._choose_mode("polygon")

    def _on_kind_toggled(self, checked: bool) -> None:
        if checked:
            for kind, button in self.kind_buttons.items():
                if button.isChecked():
                    self.controller.set_circle_kind(kind)

    def _on_mode_toggled(self, checked: bool) -> None:
        if checked:
            for mode, button in self.mode_buttons.items():
                if button.isChecked():
                    self.controller.set_shape_mode(mode)


def _repair_in_words(repair: Mapping[str, Any]) -> str:
    if not isinstance(repair, Mapping):
        return str(repair)
    step = repair.get("step", "a step")
    return (
        f"{step}: {repair.get('code', 'a failure')} was repaired by setting "
        f"{repair.get('parameter')} to {repair.get('value')}"
    )


def open_review(order: Path, viewer_win, launcher=None) -> ReviewWindow:
    """Build the review window beside the viewer, show it, and start polling."""
    window = ReviewWindow(Path(order), viewer_win, launcher)
    viewer_win.show()
    make_freestanding(window)
    window.show()
    window.start()
    window.raise_()
    window.activateWindow()
    return window

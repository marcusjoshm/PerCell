"""Wiring of the AnalyzeIt review: the launcher button, the command-line argument, the
tool window over a fake viewer, and teardown when PerCell quits."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from qtpy.QtWidgets import QPushButton
from tests.test_gui_workflows.test_analyzeit_review_controller import (
    FIRST,
    FakeLock,
    FakeRenderer,
    publish,
    set_status,
)

from percell4.app import parse_args
from percell4.gui.workflows.analyzeit_review.window import ReviewWindow
from percell4.interfaces.gui.main_window import LauncherWindow
from percell4.model import CellDataModel

_OPEN_REVIEW = "percell4.gui.workflows.analyzeit_review.window.open_review"
NOW = datetime(2026, 11, 10, 21, 0, 0, tzinfo=UTC)


class BindingRenderer(FakeRenderer):
    def bind(self, on_drawn, key_actions):
        self.keys = sorted(key_actions)


def _find_button(win, text: str) -> QPushButton:
    for btn in win.findChildren(QPushButton):
        if btn.text() == text:
            return btn
    raise AssertionError(f"{text!r} button not found")


def test_workflows_panel_has_the_review_button(qtbot):
    win = LauncherWindow(CellDataModel())
    qtbot.addWidget(win)
    assert _find_button(win, "AnalyzeIt review").toolTip()


def test_the_button_opens_the_chosen_order_and_quitting_tears_it_down(qtbot, tmp_path):
    publish(tmp_path)
    win = LauncherWindow(CellDataModel())
    qtbot.addWidget(win)
    window = MagicMock()
    with (
        patch("percell4.interfaces.gui.main_window.existing_directory",
              return_value=str(tmp_path)),
        patch.object(win, "get_viewer_window", return_value=MagicMock()),
        patch(_OPEN_REVIEW, return_value=window) as opener,
    ):
        _find_button(win, "AnalyzeIt review").click()
    assert opener.call_args.args[0] == tmp_path
    assert opener.call_args.kwargs["launcher"] is win
    assert win._active_analyzeit_review is window
    win.close()
    window.shutdown.assert_called_once()
    assert win._active_analyzeit_review is None


def test_a_folder_that_is_not_an_order_is_refused(qtbot, tmp_path):
    win = LauncherWindow(CellDataModel())
    qtbot.addWidget(win)
    with (
        patch("percell4.interfaces.gui.main_window.message_box") as box,
        patch(_OPEN_REVIEW) as opener,
    ):
        assert not win.open_analyzeit_review(tmp_path)
    opener.assert_not_called()
    box.assert_called_once()


def test_the_review_respects_the_workflow_lock(qtbot, tmp_path):
    publish(tmp_path)
    win = LauncherWindow(CellDataModel())
    qtbot.addWidget(win)
    win.set_workflow_locked(True)
    with patch(_OPEN_REVIEW) as opener:
        assert not win.open_analyzeit_review(tmp_path)
    opener.assert_not_called()


def test_the_command_line_names_an_order_folder(tmp_path):
    assert parse_args(["--analyzeit-review", str(tmp_path)]).analyzeit_review == tmp_path
    assert parse_args([]).analyzeit_review is None
    assert parse_args(["-platform", "offscreen"]).analyzeit_review is None


def _window(qtbot, order: Path, renderer=None):
    renderer = renderer or BindingRenderer()
    lock = FakeLock()
    window = ReviewWindow(order, MagicMock(), launcher=lock, renderer=renderer)
    qtbot.addWidget(window)
    window.controller.loader = lambda path, name, layers: __import__(
        "percell4.workflows.analyzeit_review.session", fromlist=["FieldImages"]
    ).FieldImages(name, {})
    window.start()
    return window, renderer, lock


def test_the_window_lists_fields_marks_opened_ones_and_answers(qtbot, tmp_path):
    publish(tmp_path)
    window, renderer, lock = _window(qtbot, tmp_path)
    assert lock.locked and "n" in renderer.keys
    assert window.btn_okay.isEnabled() and window.btn_okay.text() == "Okay"
    assert window.field_list.item(0).text().startswith("✓ a")
    assert not window.field_list.item(1).text().startswith("✓")
    window.confirm = lambda title, text: True
    window.words.setPlainText("fine")
    window.btn_okay.click()
    answer = json.loads((tmp_path / FIRST / "answer.json").read_text())
    assert answer["verdict"] == "okay" and answer["source"] == "window"
    assert not window.btn_okay.isEnabled()
    window.shutdown()
    window.shutdown()
    assert not lock.locked


def test_with_nothing_pending_the_window_shows_a_banner_and_no_buttons(qtbot, tmp_path):
    publish(tmp_path)
    # The window reads the real clock, so the run must have reported just now.
    set_status(tmp_path, state="running", bundle=None, at=datetime.now(UTC))
    window, *_ = _window(qtbot, tmp_path)
    assert "running the steps" in window.banner.text()
    assert not window.btn_okay.isEnabled() and not window.btn_send_back.isEnabled()
    window.shutdown()


def test_a_proposal_reads_accept_and_decline(qtbot, tmp_path):
    publish(tmp_path)
    path = tmp_path / FIRST / "bundle.json"
    data = json.loads(path.read_text())
    data.update(bundle_kind="proposal", fields=[], proposal={"change": "two conditions"},
                evidence=["the folder names"])
    path.write_text(json.dumps(data))
    window, *_ = _window(qtbot, tmp_path)
    assert window.btn_okay.text() == "Accept" and window.btn_send_back.text() == "Decline"
    assert "two conditions" in window.proposal.toPlainText()
    window.btn_send_back.click()
    answer = json.loads((tmp_path / FIRST / "answer.json").read_text())
    assert answer["verdict"] == "send_back"
    window.shutdown()


def test_a_final_review_shows_the_report_and_table_and_offers_a_target_step(qtbot, tmp_path):
    publish(tmp_path)
    (tmp_path / "report.md").write_text("# Report\n\nTreated cells are brighter.\n")
    (tmp_path / "summary.csv").write_text("condition,cells\ncontrol,8\ntreated,6\n")
    path = tmp_path / FIRST / "bundle.json"
    data = json.loads(path.read_text())
    data.update(bundle_kind="final", step=None, attempt=None, report="report.md",
                plots=[], summary_table="summary.csv", workflow_steps=["import", "cells"])
    path.write_text(json.dumps(data))
    status = json.loads((tmp_path / "status.json").read_text())
    status["pending"].update(bundle_kind="final", step=None, attempt=None)
    (tmp_path / "status.json").write_text(json.dumps(status))
    window, *_ = _window(qtbot, tmp_path)
    assert "Treated cells are brighter" in window.report.toPlainText()
    assert window.table.rowCount() == 2
    window.target_step.setCurrentIndex(window.target_step.findData("cells"))
    window.btn_send_back.click()
    answer = json.loads((tmp_path / FIRST / "answer.json").read_text())
    assert answer["target_step"] == "cells" and answer["bundle_kind"] == "final"
    window.shutdown()

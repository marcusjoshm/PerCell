"""The AnalyzeIt review controller, with a fake viewer, window and launcher."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from percell4.gui.workflows.analyzeit_review.controller import ReviewController
from percell4.workflows.analyzeit_review import protocol
from percell4.workflows.analyzeit_review.session import FieldImages

NOW = datetime(2026, 11, 10, 21, 0, 0, tzinfo=UTC)
FIRST = "review/001-step-cells-attempt1"
SECOND = "review/002-step-cells-attempt2"
SQUARE = [[10.0, 10.0], [10.0, 20.0], [20.0, 20.0], [20.0, 10.0]]


def _doc(kind, **fields):
    return {"kind": kind, "schema_version": 1, **fields}


def _write(path: Path, document) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document))


def set_status(order: Path, state="awaiting_review", bundle=FIRST, attempt=1, h="h1", at=NOW):
    pending = {"bundle_kind": "step", "step": "cells", "attempt": attempt, "hash": h,
               "bundle": bundle} if bundle else None
    harness = "waiting_for_joshua" if state == "awaiting_review" else "ready"
    _write(order / "status.json", _doc(
        "status", order_id="o1", state=state, harness=harness, stop=None, pending=pending,
        offline=False, queued_words=0, updated=at.isoformat()))
    _write(order / protocol.HEARTBEAT_FILE, _doc("heartbeat", at=at.isoformat(), pid=1))


def publish(order: Path, folder=FIRST, attempt=1, h="h1", **extra):
    _write(order / folder / "bundle.json", _doc(
        "bundle", protocol_version=1, order_id="o1", bundle_kind="step", step="cells",
        attempt=attempt, hash=h, scope_in_words="Compare control and treated.",
        repairs_since_last_stop=[], tool="segment_cells_cellpose", parameters={},
        fields=[{"name": "a", "h5": "fields/a.h5", "shape": [64, 64], "cells": 2},
                {"name": "b", "h5": "fields/b.h5", "shape": [64, 64], "cells": 3}],
        layer_names={"labels": "analyzeit_cells", "removed": "analyzeit_cells_removed"},
        **extra))
    set_status(order, bundle=folder, attempt=attempt, h=h)


class FakeRenderer:
    def __init__(self):
        self.calls: list[str] = []
        self.shown: list[str] = []
        self.drawn: list[dict] = []
        self.messages: list[str] = []
        self.hidden = False
        self.forwarding = True
        self.previous: list[dict] = []

    def is_viewer_alive(self):
        return True

    def hide_other_layers(self):
        self.hidden = True

    def restore_other_layers(self):
        self.hidden = False

    def suspend_selected_label_forwarding(self):
        self.forwarding = False

    def resume_selected_label_forwarding(self):
        self.forwarding = True

    def show_field(self, images, circles, previous):
        self.shown.append(images.name)
        self.drawn = [{"kind": c.kind, "shape_type": c.shape_type, "vertices": c.vertices}
                      for c in circles]
        self.previous = previous

    def drawn_circles(self):
        return [dict(d) for d in self.drawn]

    def set_circles(self, circles):
        self.drawn = [{"kind": c.kind, "shape_type": c.shape_type, "vertices": c.vertices}
                      for c in circles]

    def set_circle_kind(self, kind):
        self.calls.append(f"kind:{kind}")

    def set_shape_mode(self, mode):
        self.calls.append(f"mode:{mode}")

    def remove_last_circle(self):
        if self.drawn:
            self.drawn.pop()

    def remove_selected_circles(self):
        self.calls.append("delete")

    def show_message_at(self, message, at):
        self.messages.append(message)

    def clear_review_layers(self):
        self.calls.append("clear")
        self.drawn = []


class FakeView:
    def __init__(self):
        self.banner = ""
        self.refusal = None
        self.enabled = False
        self.session = None
        self.current = None
        self.values = {"words": "", "words_field": None, "not_coming_right": False,
                       "target_step": None}
        self.confirm_answer = True
        self.confirmations: list[str] = []
        self.informed: list[str] = []

    def show_banner(self, text):
        self.banner = text

    def show_stop(self, session):
        self.session = session

    def show_refusal(self, text):
        self.refusal = text

    def show_fields(self, session, current):
        self.current = current

    def set_answer_enabled(self, enabled):
        self.enabled = enabled

    def inputs(self):
        return dict(self.values)

    def set_inputs(self, session):
        self.values["words"] = session.words

    def confirm(self, title, text):
        self.confirmations.append(text)
        return self.confirm_answer

    def inform(self, title, text):
        self.informed.append(text)


class FakeLock:
    def __init__(self):
        self.locked = False
        self.history: list[bool] = []

    def set_workflow_locked(self, locked):
        self.locked = locked
        self.history.append(locked)


class Loader:
    def __init__(self):
        self.opened: list[Path] = []

    def __call__(self, path, name, layer_names):
        self.opened.append(Path(path))
        return FieldImages(name, {"ch00": np.zeros((64, 64))}, np.zeros((64, 64), np.int32))


@pytest.fixture
def order(tmp_path):
    order = tmp_path / "o1"
    publish(order)
    return order


@pytest.fixture
def parts(order):
    renderer, view, lock, loader = FakeRenderer(), FakeView(), FakeLock(), Loader()
    clock = {"now": NOW}
    controller = ReviewController(order, renderer, view, lock, loader,
                                  now=lambda: clock["now"])
    controller.start()
    return controller, renderer, view, lock, loader, clock


def answer_of(order, folder=FIRST):
    return json.loads((order / folder / "answer.json").read_text())


def test_opening_locks_the_launcher_and_hides_layers_and_closing_restores_both(parts):
    controller, renderer, view, lock, _, _ = parts
    assert lock.locked and renderer.hidden and not renderer.forwarding
    assert view.enabled and renderer.shown == ["a"]
    controller.close()
    controller.close()
    assert not lock.locked and not renderer.hidden and renderer.forwarding
    assert lock.history == [True, False]


def test_three_problems_and_one_correct_circle_are_sent_back_in_pixels(parts, order):
    controller, renderer, *_ = parts
    for offset in (0, 15, 30):
        renderer.drawn.append({"kind": "problem", "shape_type": "lasso",
                               "vertices": [[y + offset, x] for y, x in SQUARE]})
    controller.step_field(1)
    renderer.drawn.append({"kind": "correct", "shape_type": "ellipse", "vertices": SQUARE})
    assert controller.send_back()
    answer = answer_of(order)
    assert [(c["field"], c["kind"]) for c in answer["circles"]] == [
        ("a", "problem"), ("a", "problem"), ("a", "problem"), ("b", "correct")]
    assert answer["circles"][3]["vertices"] == SQUARE
    assert not any(order.rglob("*.npy"))


def test_okay_with_unopened_fields_names_the_count_and_records_them(parts, order):
    controller, _, view, *_ = parts
    assert controller.okay()
    assert "1 of 2 field was not opened" in view.confirmations[0]
    assert answer_of(order)["fields_opened"] == {"a": True, "b": False}


def test_declining_the_unopened_question_writes_nothing(parts, order):
    controller, _, view, *_ = parts
    view.confirm_answer = False
    assert not controller.okay()
    assert not (order / FIRST / "answer.json").exists()


def test_okay_is_refused_while_a_problem_circle_exists(parts, order):
    controller, renderer, view, *_ = parts
    renderer.drawn.append({"kind": "problem", "shape_type": "lasso", "vertices": SQUARE})
    assert not controller.okay()
    assert "Send back" in view.informed[0]
    assert not (order / FIRST / "answer.json").exists()


def test_a_problem_drawn_across_a_correct_circle_is_refused_at_the_shape(parts):
    controller, renderer, *_ = parts
    renderer.drawn.append({"kind": "correct", "shape_type": "lasso", "vertices": SQUARE})
    controller.circle_drawn()
    renderer.drawn.append({"kind": "problem", "shape_type": "lasso",
                           "vertices": [[y + 5, x + 5] for y, x in SQUARE]})
    controller.circle_drawn()
    assert "cannot cross" in renderer.messages[0]
    assert [d["kind"] for d in renderer.drawn] == ["correct"]


def test_undo_and_delete_act_before_the_answer(parts, order):
    controller, renderer, *_ = parts
    renderer.drawn.append({"kind": "problem", "shape_type": "lasso", "vertices": SQUARE})
    controller.undo_last_circle()
    controller.delete_selected_circles()
    assert renderer.drawn == [] and "delete" in renderer.calls
    assert controller.okay()


def test_closing_mid_review_keeps_the_draft_and_reopening_restores_it(parts, order):
    controller, renderer, view, *_ = parts
    renderer.drawn.append({"kind": "problem", "shape_type": "lasso", "vertices": SQUARE})
    view.values["words"] = "edges are cut"
    controller.close()
    assert not (order / FIRST / "answer.json").exists()

    renderer2, view2 = FakeRenderer(), FakeView()
    again = ReviewController(order, renderer2, view2, FakeLock(), Loader(), now=lambda: NOW)
    again.start()
    assert view2.values["words"] == "edges are cut"
    assert renderer2.drawn[0]["kind"] == "problem"


def test_after_the_answer_no_field_file_is_opened_until_the_next_stop(parts, order):
    controller, renderer, view, _, loader, _ = parts
    assert controller.okay()
    opened = len(loader.opened)
    assert not view.enabled and "Your answer is written" in view.banner
    controller.step_field(1)
    controller.show_field("b")
    controller.poll()
    assert len(loader.opened) == opened

    publish(order, SECOND, attempt=2, h="h2", changed_fields=["b"], previous_circles=[
        {"kind": "problem", "shape_type": "lasso", "field": "b", "vertices": SQUARE,
         "outcome": "replaced"}])
    controller.poll()
    assert view.enabled and renderer.shown[-1] == "b"
    assert renderer.previous[0]["outcome"] == "replaced"


def test_with_no_stop_pending_each_state_shows_its_banner_and_disables(parts, order):
    controller, renderer, view, *_ = parts
    for state in ("running", "redoing"):
        set_status(order, state=state, bundle=None)
        controller.poll()
        assert view.banner == protocol.BANNERS[state] and not view.enabled


def test_a_stale_heartbeat_shows_stopped_unexpectedly(parts, order):
    controller, _, view, _, _, clock = parts
    set_status(order, state="running", bundle=None)
    clock["now"] = NOW + timedelta(seconds=protocol.HEARTBEAT_STALE_SECONDS + 1)
    controller.poll()
    assert view.banner == protocol.STOPPED_UNEXPECTEDLY and not view.enabled


def test_a_second_answer_is_refused_and_the_first_stands(parts, order):
    controller, _, view, *_ = parts
    (order / FIRST / "answer.json").write_text('{"kind": "answer", "first": true}')
    assert not controller.okay()
    assert "first answer stands" in view.informed[0]
    assert answer_of(order)["first"] is True


def test_the_clock_counts_input_on_either_window(parts, order):
    controller, _, _, _, _, clock = parts
    controller.note_input(active=True)
    clock["now"] = NOW + timedelta(seconds=20)
    controller.tick(active=True)
    clock["now"] = NOW + timedelta(seconds=25)
    controller.tick(active=False)
    assert controller.okay()
    segments = answer_of(order)["time_segments"]
    assert sum(s["seconds"] for s in segments) == 25

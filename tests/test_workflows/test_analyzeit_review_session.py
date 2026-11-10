"""The Qt-free part of the AnalyzeIt review: reading an order folder and writing answers."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from percell4.workflows.analyzeit_review import protocol
from percell4.workflows.analyzeit_review.protocol import (
    STOPPED_UNEXPECTEDLY,
    ProtocolError,
    read_bundle,
    read_order_view,
)
from percell4.workflows.analyzeit_review.session import (
    ActiveClock,
    AnswerGivenError,
    AnswerRefusedError,
    CircleRefusedError,
    ReviewSession,
    bundle_notes,
)

NOW = datetime(2026, 11, 10, 21, 0, 0, tzinfo=UTC)
BUNDLE_DIR = "review/001-step-cells-attempt1"


def _doc(kind: str, **fields):
    return {"kind": kind, "schema_version": 1, **fields}


def _write(path: Path, document) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document))


def _status(order: Path, state="awaiting_review", pending=True, stop=None, harness=None,
            updated=NOW):
    pending_doc = {
        "bundle_kind": "step", "step": "cells", "attempt": 1, "hash": "h1",
        "bundle": BUNDLE_DIR,
    } if pending else None
    if harness is None:
        harness = "waiting_for_joshua" if state == "awaiting_review" else "ready"
    _write(order / "status.json", _doc(
        "status", order_id="o1", state=state, harness="stopped" if stop else harness,
        stop=stop, pending=pending_doc, offline=False, queued_words=0,
        updated=updated.isoformat(),
    ))


def _bundle(order: Path, **extra) -> Path:
    folder = order / BUNDLE_DIR
    _write(folder / "bundle.json", _doc(
        "bundle", protocol_version=1, order_id="o1", bundle_kind="step", step="cells",
        attempt=1, hash="h1", scope_in_words="Compare control and treated.",
        repairs_since_last_stop=[], tool="segment_cells_cellpose", parameters={},
        fields=[
            {"name": "control_s00", "h5": "fields/control_s00.h5", "shape": [64, 64], "cells": 4},
            {"name": "treated_s00", "h5": "fields/treated_s00.h5", "shape": [64, 64], "cells": 3},
        ],
        layer_names={"labels": "analyzeit_cells", "removed": "analyzeit_cells_removed"},
        send_back_count=0, note_after_send_backs=None, recipe_drift=None, **extra,
    ))
    return folder


@pytest.fixture
def order(tmp_path) -> Path:
    order = tmp_path / "o1"
    _status(order)
    _bundle(order)
    (order / "events.jsonl").write_text("")
    return order


def _session(order: Path) -> ReviewSession:
    return ReviewSession.open(order, order / BUNDLE_DIR)


SQUARE = [[10, 10], [10, 20], [20, 20], [20, 10]]
FAR_SQUARE = [[40, 40], [40, 50], [50, 50], [50, 40]]


# ── The order view ──


def test_a_waiting_stop_is_offered_for_an_answer(order):
    view = read_order_view(order, NOW)
    assert view.can_answer
    assert view.bundle_folder == order / BUNDLE_DIR


@pytest.mark.parametrize("state", ["running", "redoing", "limited", "delivered"])
def test_each_state_without_a_stop_shows_its_banner(order, state):
    _status(order, state=state, pending=False,
            harness="finished" if state in ("limited", "delivered") else "ready")
    _write(order / protocol.HEARTBEAT_FILE, _doc("heartbeat", at=NOW.isoformat(), pid=1))
    view = read_order_view(order, NOW)
    assert not view.can_answer
    assert view.banner == protocol.BANNERS[state]


def test_a_stopped_order_shows_its_reason(order):
    _status(order, state="running", pending=False,
            stop={"reason": "spend_limit", "detail": "over $2", "return_to": "running"})
    view = read_order_view(order, NOW)
    assert not view.can_answer
    assert "spend_limit" in view.banner and "over $2" in view.banner


def test_a_stale_heartbeat_shows_stopped_unexpectedly(order):
    old = NOW - timedelta(seconds=protocol.HEARTBEAT_STALE_SECONDS + 5)
    _status(order, state="running", pending=False, updated=old)
    _write(order / protocol.HEARTBEAT_FILE, _doc("heartbeat", at=old.isoformat(), pid=1))
    view = read_order_view(order, NOW)
    assert view.banner == STOPPED_UNEXPECTEDLY
    assert not view.can_answer


def test_a_fresh_heartbeat_shows_running(order):
    old = NOW - timedelta(minutes=5)
    _status(order, state="running", pending=False, updated=old)
    _write(order / protocol.HEARTBEAT_FILE, _doc("heartbeat", at=NOW.isoformat(), pid=1))
    assert read_order_view(order, NOW).banner == protocol.BANNERS["running"]


def test_a_refused_answer_shows_the_reason(order):
    lines = [
        _doc("event", event="bundle_published", bundle=BUNDLE_DIR),
        _doc("event", event="answer_refused", reason="stale_answer",
             detail="the answer names another result"),
    ]
    (order / "events.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    view = read_order_view(order, NOW)
    assert view.can_answer
    assert "stale_answer" in view.refusal


def test_an_answer_with_nothing_watching_says_the_next_resume_takes_it(order):
    (order / BUNDLE_DIR / "answer.json").write_text("{}")
    view = read_order_view(order, NOW)
    assert view.answer_written and not view.can_answer
    assert "next `analyzeit resume`" in view.banner
    _write(order / protocol.WATCHER_FILE, _doc("heartbeat", at=NOW.isoformat(), pid=2))
    assert "taking it in" in read_order_view(order, NOW).banner


def test_a_bundle_with_an_unknown_protocol_is_refused_clearly(order):
    _bundle(order)
    path = order / BUNDLE_DIR / "bundle.json"
    data = json.loads(path.read_text())
    data["protocol_version"] = 2
    path.write_text(json.dumps(data))
    with pytest.raises(ProtocolError, match="protocol 2"):
        read_bundle(order / BUNDLE_DIR)


def test_an_unreadable_status_is_a_banner_not_a_crash(tmp_path):
    view = read_order_view(tmp_path, NOW)
    assert "cannot be reviewed" in view.banner


# ── Circles and answers ──


def test_send_back_with_circles_writes_them_in_pixels_and_no_labels(order):
    session = _session(order)
    for offset in (0, 15, 30):
        session.add_circle("problem", "polygon", "control_s00",
                           [[y + offset, x] for y, x in SQUARE])
    session.add_circle("correct", "lasso", "treated_s00", SQUARE)
    answer = session.write_answer("send_back", NOW)

    written = json.loads((order / BUNDLE_DIR / "answer.json").read_text())
    assert written == answer
    assert len(written["circles"]) == 4
    assert [c["kind"] for c in written["circles"]] == ["problem"] * 3 + ["correct"]
    assert written["circles"][0]["vertices"][0] == [10.0, 10.0]
    assert written["hash"] == "h1" and written["source"] == "window"
    assert sorted(p.name for p in (order / BUNDLE_DIR).iterdir()) == [
        "answer.json", "bundle.json", "draft.json"
    ]


def test_okay_with_unopened_fields_records_them_as_not_opened(order):
    session = _session(order)
    session.mark_opened("control_s00")
    assert session.unopened() == ["treated_s00"]
    answer = session.write_answer("okay", NOW)
    assert answer["fields_opened"] == {"control_s00": True, "treated_s00": False}


def test_okay_is_refused_while_a_problem_circle_exists(order):
    session = _session(order)
    session.add_circle("problem", "polygon", "control_s00", SQUARE)
    with pytest.raises(AnswerRefusedError, match="Send back"):
        session.write_answer("okay", NOW)
    assert not (order / BUNDLE_DIR / "answer.json").exists()


def test_a_problem_circle_across_a_correct_one_is_refused(order):
    session = _session(order)
    session.add_circle("correct", "polygon", "control_s00", SQUARE)
    with pytest.raises(CircleRefusedError) as refused:
        session.add_circle("problem", "ellipse", "control_s00",
                           [[15, 15], [15, 30], [30, 30], [30, 15]])
    assert refused.value.at == [15.0, 15.0]
    session.add_circle("problem", "polygon", "control_s00", FAR_SQUARE)
    session.add_circle("problem", "polygon", "treated_s00", SQUARE)
    assert len(session.circles) == 3


def test_the_last_circle_can_be_undone_and_a_chosen_one_deleted(order):
    session = _session(order)
    first = session.add_circle("problem", "polygon", "control_s00", SQUARE)
    session.add_circle("problem", "polygon", "control_s00", FAR_SQUARE)
    session.undo_last()
    assert [c.id for c in session.circles] == [first.id]
    assert session.delete(first.id)
    assert session.circles == []


def test_a_send_back_needs_something_to_act_on(order):
    session = _session(order)
    with pytest.raises(AnswerRefusedError, match="Send back needs"):
        session.write_answer("send_back", NOW)
    session.words = "The cells at the edge are cut in half."
    answer = session.write_answer("send_back", NOW)
    assert answer["words"] == [{"text": session.words, "field": None}]


def test_vertices_outside_the_image_are_clipped_and_marked(order):
    session = _session(order)
    session.add_circle("problem", "polygon", "control_s00", [[-5, 10], [10, 70], [20, 20]])
    answer = session.write_answer("send_back", NOW)
    assert answer["circles_clipped"]
    assert answer["circles"][0]["vertices"][0] == [0.0, 10.0]
    assert answer["circles"][0]["vertices"][1] == [10.0, 63.0]


def test_the_first_answer_stands(order):
    (order / BUNDLE_DIR / "answer.json").write_text('{"kind": "answer"}')
    session = ReviewSession(order, order / BUNDLE_DIR, read_bundle(order / BUNDLE_DIR))
    with pytest.raises(AnswerRefusedError, match="first answer stands"):
        session.write_answer("okay", NOW)
    assert json.loads((order / BUNDLE_DIR / "answer.json").read_text()) == {"kind": "answer"}


def test_after_the_answer_no_field_file_is_opened(order):
    session = _session(order)
    assert session.field_path("control_s00") == order / "fields/control_s00.h5"
    session.write_answer("okay", NOW)
    with pytest.raises(AnswerGivenError):
        session.field_path("control_s00")


def test_closing_mid_review_keeps_the_draft_and_writes_no_answer(order):
    session = _session(order)
    session.add_circle("problem", "polygon", "control_s00", SQUARE)
    session.words = "edge cells"
    session.not_coming_right = True
    session.mark_opened("treated_s00")
    session.save_draft(NOW)
    assert not (order / BUNDLE_DIR / "answer.json").exists()

    reopened = _session(order)
    assert [c.to_answer() for c in reopened.circles] == [c.to_answer() for c in session.circles]
    assert reopened.words == "edge cells" and reopened.not_coming_right
    assert reopened.opened["treated_s00"]


def test_a_draft_for_another_result_is_ignored(order):
    session = _session(order)
    session.words = "old"
    session.save_draft(NOW)
    draft = json.loads((order / BUNDLE_DIR / "draft.json").read_text())
    draft["hash"] = "other"
    (order / BUNDLE_DIR / "draft.json").write_text(json.dumps(draft))
    assert _session(order).words == ""


def test_changed_fields_come_first_and_previous_circles_are_kept(order):
    previous = [{"kind": "problem", "shape_type": "polygon", "field": "treated_s00",
                 "vertices": SQUARE, "outcome": "replaced"}]
    _bundle(order, changed_fields=["treated_s00"], previous_circles=previous)
    session = _session(order)
    assert session.field_names == ["treated_s00", "control_s00"]
    assert session.previous_circles[0]["outcome"] == "replaced"


def test_a_proposal_can_be_declined_without_words(order):
    _bundle(order)
    path = order / BUNDLE_DIR / "bundle.json"
    data = json.loads(path.read_text())
    data.update(bundle_kind="proposal", fields=[])
    path.write_text(json.dumps(data))
    answer = _session(order).write_answer("send_back", NOW)
    assert answer["verdict"] == "send_back" and answer["bundle_kind"] == "proposal"


# ── Active time ──


def test_the_clock_counts_while_either_window_is_active():
    clock = ActiveClock()
    clock.input(NOW, active=True)
    clock.tick(NOW + timedelta(seconds=10), active=True)  # focus moved to the viewer: still active
    clock.input(NOW + timedelta(seconds=20), active=True)
    clock.tick(NOW + timedelta(seconds=30), active=False)  # both windows lost focus
    clock.tick(NOW + timedelta(seconds=90), active=False)
    assert clock.active_seconds() == 30


def test_the_clock_stops_after_a_minute_without_input():
    clock = ActiveClock()
    clock.input(NOW, active=True)
    clock.tick(NOW + timedelta(seconds=200), active=True)
    assert clock.active_seconds() == 60
    clock.input(NOW + timedelta(seconds=300), active=True)
    clock.tick(NOW + timedelta(seconds=305), active=True)
    assert clock.active_seconds() == 65
    assert len(clock.export()) == 2


def test_the_notes_show_what_a_redo_did_and_never_a_bare_flag():
    bundle = {
        "send_back_count": 1,
        "note_after_send_backs": False,
        "redo_summary": "1 of 1 circled region has new outlines.",
        "recipe_drift": None,
    }
    assert bundle_notes(bundle) == [
        "Sent back 1 time(s) so far.",
        "1 of 1 circled region has new outlines.",
    ]
    older = {"send_back_count": 3, "note_after_send_backs": True}
    notes = bundle_notes(older)
    assert "True" not in notes and "not coming right" in notes[-1]
    assert bundle_notes({}) == []

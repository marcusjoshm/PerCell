"""The AnalyzeIt review controller (plan decision KTD16).

It joins three things through narrow protocols, so its logic is tested with fakes and no
napari viewer:

- :class:`ReviewRenderer`, the viewer: field images, label outlines and the one circles
  layer;
- :class:`ReviewView`, the tool window: the stop in words, the field list, the banner,
  the buttons and the popups;
- :class:`WorkflowLock`, the launcher, locked while the review is open.

The controller polls the order folder, opens a :class:`ReviewSession` for each new stop,
and writes the answer. It reads the drawn circles only when a button is pressed, the field
changes or a draft is saved. From the moment the answer is written until the next stop
appears, it opens no field file.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from percell4.workflows.analyzeit_review.protocol import OrderView, read_order_view
from percell4.workflows.analyzeit_review.session import (
    AnswerGivenError,
    AnswerRefusedError,
    Circle,
    CircleRefusedError,
    FieldImages,
    ReviewSession,
    load_field_images,
)

logger = logging.getLogger(__name__)


class ReviewRenderer(Protocol):
    """What the controller needs from the viewer."""

    def is_viewer_alive(self) -> bool: ...

    def hide_other_layers(self) -> None: ...

    def restore_other_layers(self) -> None: ...

    def suspend_selected_label_forwarding(self) -> None: ...

    def resume_selected_label_forwarding(self) -> None: ...

    def show_field(
        self, images: FieldImages, circles: list[Circle], previous: list[dict[str, Any]]
    ) -> None: ...

    def drawn_circles(self) -> list[dict[str, Any]]: ...

    def set_circles(self, circles: list[Circle]) -> None: ...

    def set_circle_kind(self, kind: str) -> None: ...

    def set_shape_mode(self, mode: str) -> None: ...

    def remove_last_circle(self) -> None: ...

    def remove_selected_circles(self) -> None: ...

    def show_message_at(self, message: str, at: list[float]) -> None: ...

    def clear_review_layers(self) -> None: ...


class ReviewView(Protocol):
    """What the controller needs from the tool window."""

    def show_banner(self, text: str) -> None: ...

    def show_stop(self, session: ReviewSession) -> None: ...

    def show_refusal(self, text: str | None) -> None: ...

    def show_fields(self, session: ReviewSession, current: str | None) -> None: ...

    def set_answer_enabled(self, enabled: bool) -> None: ...

    def inputs(self) -> Mapping[str, Any]: ...

    def set_inputs(self, session: ReviewSession) -> None: ...

    def confirm(self, title: str, text: str) -> bool: ...

    def inform(self, title: str, text: str) -> None: ...


class WorkflowLock(Protocol):
    def set_workflow_locked(self, locked: bool) -> None: ...


FieldLoader = Callable[[Path, str, Mapping[str, str] | None], FieldImages]


@dataclass
class Shown:
    """The stop on screen: the session and the field in the viewer."""

    session: ReviewSession
    field: str | None = None


class ReviewController:
    def __init__(
        self,
        order: Path,
        renderer: ReviewRenderer,
        view: ReviewView,
        lock: WorkflowLock | None = None,
        loader: FieldLoader = load_field_images,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.order = Path(order)
        self.renderer = renderer
        self.view = view
        self.lock = lock
        self.loader = loader
        self.now = now
        self.shown: Shown | None = None
        self.order_view: OrderView | None = None
        self._started = False
        self._closed = False

    # ── Lifetime ──

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        if self.lock is not None:
            self.lock.set_workflow_locked(True)
        self.renderer.suspend_selected_label_forwarding()
        self.renderer.hide_other_layers()
        self.poll()

    def close(self) -> None:
        """Save the draft and give the viewer and launcher back. Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        try:
            if self.shown is not None and not self.shown.session.answered:
                self.save_draft()
        except Exception:  # noqa: BLE001 - closing must always finish
            logger.exception("could not save the review draft on close")
        for step in (
            self.renderer.clear_review_layers,
            self.renderer.restore_other_layers,
            self.renderer.resume_selected_label_forwarding,
        ):
            try:
                step()
            except Exception:  # noqa: BLE001
                logger.exception("review teardown step failed")
        if self.lock is not None and self._started:
            self.lock.set_workflow_locked(False)

    @property
    def closed(self) -> bool:
        return self._closed

    # ── Polling ──

    def poll(self) -> None:
        if self._closed:
            return
        order_view = read_order_view(self.order, self.now())
        if self.order_view is not None and order_view.key == self.order_view.key:
            return
        self.order_view = order_view
        session = self.shown.session if self.shown else None
        if order_view.can_answer:
            if session is None or session.folder != order_view.bundle_folder or (
                session.bundle.get("hash") != (order_view.pending or {}).get("hash")
            ):
                self._open_stop(order_view)
            self.view.show_refusal(order_view.refusal)
            return
        if session is not None and not session.answered:
            self._save_quietly()
        if session is not None and order_view.answer_written:
            session.answered = True
        else:
            self.shown = None
        self.renderer.clear_review_layers()
        self.view.show_banner(order_view.banner)
        self.view.show_refusal(order_view.refusal)
        self.view.set_answer_enabled(False)

    def _open_stop(self, order_view: OrderView) -> None:
        if self.shown is not None and not self.shown.session.answered:
            self._save_quietly()
        try:
            session = ReviewSession.open(self.order, order_view.bundle_folder)
        except Exception as error:  # noqa: BLE001 - shown to Joshua, never raised into Qt
            self.shown = None
            self.renderer.clear_review_layers()
            self.view.show_banner(f"This stop cannot be shown: {error}")
            self.view.set_answer_enabled(False)
            return
        self.shown = Shown(session)
        self.view.show_banner("")
        self.view.show_stop(session)
        self.view.set_inputs(session)
        self.view.set_answer_enabled(True)
        if session.field_names:
            self.show_field(session.field_names[0])
        else:
            self.renderer.clear_review_layers()
            self.view.show_fields(session, None)

    # ── Fields ──

    def show_field(self, name: str) -> None:
        if self.shown is None:
            return
        session = self.shown.session
        if self.shown.field is not None and self.shown.field != name:
            self._collect()
        try:
            path = session.field_path(name)
        except AnswerGivenError:
            return
        if path is None:
            self.shown.field = name
            self.view.show_fields(session, name)
            return
        layer_names = session.bundle.get("layer_names")
        try:
            images = self.loader(path, name, layer_names)
        except Exception as error:  # noqa: BLE001
            self.view.inform("Field could not be opened", f"{name}: {error}")
            return
        previous = [c for c in session.previous_circles if c.get("field") == name]
        self.renderer.show_field(
            images, [c for c in session.circles if c.field == name], previous
        )
        self.shown.field = name
        session.mark_opened(name)
        self.view.show_fields(session, name)

    def step_field(self, offset: int) -> None:
        if self.shown is None or not self.shown.session.field_names:
            return
        names = self.shown.session.field_names
        current = names.index(self.shown.field) if self.shown.field in names else -1
        self.show_field(names[(current + offset) % len(names)])

    def next_region(self) -> None:
        """Jump to the next field holding a circle from the previous attempt."""
        if self.shown is None:
            return
        session = self.shown.session
        with_regions = [
            name for name in session.field_names
            if any(c.get("field") == name for c in session.previous_circles)
        ]
        if not with_regions:
            return
        names = session.field_names
        start = names.index(self.shown.field) if self.shown.field in names else -1
        for offset in range(1, len(names) + 1):
            candidate = names[(start + offset) % len(names)]
            if candidate in with_regions:
                self.show_field(candidate)
                return

    # ── Circles ──

    def _collect(self) -> list[CircleRefusedError]:
        """Take the circles drawn on the shown field into the session."""
        if self.shown is None or self.shown.field is None or self.shown.session.answered:
            return []
        session = self.shown.session
        field = self.shown.field
        drawn = [{**item, "field": field} for item in self.renderer.drawn_circles()]
        kept = [c.to_answer() for c in session.circles if c.field != field]
        refused = session.replace_circles(kept + drawn)
        if refused:
            self.renderer.set_circles([c for c in session.circles if c.field == field])
            self.renderer.show_message_at(str(refused[0]), refused[0].at)
        return refused

    def circle_drawn(self) -> None:
        """A shape was added on the circles layer: refuse it at once if it cannot stand."""
        self._collect()

    def set_circle_kind(self, kind: str) -> None:
        self.renderer.set_circle_kind(kind)

    def set_shape_mode(self, mode: str) -> None:
        self.renderer.set_shape_mode(mode)

    def undo_last_circle(self) -> None:
        self.renderer.remove_last_circle()

    def delete_selected_circles(self) -> None:
        self.renderer.remove_selected_circles()

    # ── Draft and answer ──

    def _take_inputs(self) -> None:
        if self.shown is None:
            return
        session = self.shown.session
        values = self.view.inputs()
        session.words = str(values.get("words") or "")
        session.words_field = values.get("words_field")
        session.not_coming_right = bool(values.get("not_coming_right"))
        session.target_step = values.get("target_step") or None

    def save_draft(self) -> None:
        if self.shown is None or self.shown.session.answered:
            return
        self._collect()
        self._take_inputs()
        self.shown.session.save_draft(self.now())

    def _save_quietly(self) -> None:
        try:
            self.save_draft()
        except Exception:  # noqa: BLE001
            logger.exception("could not save the review draft")

    def okay(self) -> bool:
        if self.shown is None or self.shown.session.answered:
            return False
        session = self.shown.session
        if self._collect():
            return False
        self._take_inputs()
        try:
            session.check("okay")
        except AnswerRefusedError as refused:
            self.view.inform("Okay refused", str(refused))
            return False
        unopened = session.unopened()
        if unopened and session.kind != "proposal":
            count = len(unopened)
            noun = "field was" if count == 1 else "fields were"
            if not self.view.confirm(
                "Fields not opened",
                f"{count} of {len(session.field_names)} {noun} not opened at full size. "
                "The answer will record them as not opened. Okay anyway?",
            ):
                return False
        return self._answer("okay")

    def send_back(self) -> bool:
        if self.shown is None or self.shown.session.answered:
            return False
        if self._collect():
            return False
        self._take_inputs()
        return self._answer("send_back")

    def _answer(self, verdict: str) -> bool:
        session = self.shown.session
        try:
            session.write_answer(verdict, self.now())
        except AnswerRefusedError as refused:
            self.view.inform("Answer not written", str(refused))
            if session.answered:
                self._after_answer()
            return False
        self._after_answer()
        return True

    def _after_answer(self) -> None:
        # The fields' files are about to be replaced: drop everything read from them.
        self.renderer.clear_review_layers()
        self.view.set_answer_enabled(False)
        self.order_view = None
        self.poll()

    # ── Active time ──

    def note_input(self, active: bool) -> None:
        if self.shown is not None and not self.shown.session.answered:
            self.shown.session.clock.input(self.now(), active)

    def tick(self, active: bool) -> None:
        if self.shown is not None and not self.shown.session.answered:
            self.shown.session.clock.tick(self.now(), active)

"""One review of one AnalyzeIt stop, held without any GUI code.

A :class:`ReviewSession` is made from the pending stop's bundle. It keeps:

- which fields Joshua opened at full size, in the order he should see them;
- the circles: problem circles and correct circles, each on one field, as vertices in
  image pixels (``[y, x]``). A circle of one kind drawn across one of the other kind is
  refused, because the two cannot both be true of the same cells;
- the words, the not-coming-right mark and, for a final send-back, the target step;
- the active review time (:class:`ActiveClock`).

It saves a draft beside the bundle whenever asked, so a closed or crashed window loses
nothing, and it writes the answer once. After the answer is written it opens no ``.h5``
file again: AnalyzeIt is about to replace them.

The circles and words are only written into the draft or the answer when the window asks:
on a button press or a draft save, never on each drawing event.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from percell4.workflows.analyzeit_review.protocol import (
    CIRCLE_KINDS,
    PROTOCOL_VERSION,
    SCHEMA_VERSION,
    SHAPE_TYPES,
    ProtocolError,
    create_json_exclusive,
    read_bundle,
    read_document,
    write_json_atomic,
)

IDLE_SECONDS = 60
ELLIPSE_POINTS = 64


class AnswerRefusedError(Exception):
    """The answer cannot be written as it stands. The message is for Joshua."""


class CircleRefusedError(Exception):
    """A circle that cannot be kept. ``at`` is a vertex to show the message beside."""

    def __init__(self, message: str, at: Sequence[float]) -> None:
        super().__init__(message)
        self.at = list(at)


class AnswerGivenError(Exception):
    """The answer is written; the fields' files are no longer opened."""


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _program_version() -> str:
    try:
        from percell4._version import version
    except ImportError:  # a checkout run from source, never installed
        version = "unknown"
    return f"percell4 {version}"


# ── Circles ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Circle:
    id: int
    kind: str
    shape_type: str
    field: str
    vertices: list[list[float]]

    def outline(self) -> np.ndarray:
        """The circle's outline as polygon vertices. An ellipse is given by its four corners."""
        points = np.asarray(self.vertices, dtype=float)
        if self.shape_type != "ellipse":
            return points
        centre = points.mean(axis=0)
        across = (points[1] - points[0]) / 2.0
        down = (points[3] - points[0]) / 2.0
        angles = np.linspace(0.0, 2.0 * math.pi, ELLIPSE_POINTS, endpoint=False)
        return centre + np.outer(np.cos(angles), across) + np.outer(np.sin(angles), down)

    def mask(self, shape: tuple[int, int]) -> np.ndarray:
        from skimage.draw import polygon

        outline = self.outline()
        mask = np.zeros(shape, dtype=bool)
        rows, cols = polygon(outline[:, 0], outline[:, 1], shape=shape)
        mask[rows, cols] = True
        return mask

    def to_answer(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "shape_type": self.shape_type,
            "field": self.field,
            "vertices": [[float(y), float(x)] for y, x in self.vertices],
        }


def _canvas_shape(circles: Iterable[Circle], shape: Sequence[int] | None) -> tuple[int, int]:
    if shape:
        return int(shape[-2]), int(shape[-1])
    extent = np.zeros(2)
    for circle in circles:
        extent = np.maximum(extent, np.asarray(circle.outline()).max(axis=0))
    return int(extent[0]) + 2, int(extent[1]) + 2


# ── Active time ─────────────────────────────────────────────────────────────────────────


class ActiveClock:
    """Active review time (plan decision KTD19).

    Time counts while one of the review windows is the active window and input events come
    at most ``IDLE_SECONDS`` apart. The caller says whether a review window is active on
    every input event and on a regular tick.
    """

    def __init__(self) -> None:
        self.segments: list[tuple[datetime, datetime]] = []
        self._open: list[datetime] | None = None
        self._last_input: datetime | None = None
        self.first_seen: datetime | None = None

    def input(self, now: datetime, active: bool) -> None:
        self.first_seen = self.first_seen or now
        self.tick(now, active)
        if active:
            if self._open is None:
                self._open = [now, now]
            self._last_input = now

    def tick(self, now: datetime, active: bool) -> None:
        if self._open is None or self._last_input is None:
            return
        limit = self._last_input + timedelta(seconds=IDLE_SECONDS)
        if active and now <= limit:
            self._open[1] = max(self._open[1], now)
            return
        self._open[1] = max(self._open[1], min(now, limit))
        self.segments.append((self._open[0], self._open[1]))
        self._open = None

    def stop(self, now: datetime) -> None:
        self.tick(now, active=False)

    def active_seconds(self) -> float:
        spans = list(self.segments) + ([tuple(self._open)] if self._open else [])
        return sum((end - start).total_seconds() for start, end in spans)

    def export(self) -> list[dict[str, Any]]:
        spans = list(self.segments) + ([tuple(self._open)] if self._open else [])
        return [
            {"start": _iso(start), "end": _iso(end), "seconds": (end - start).total_seconds()}
            for start, end in spans
        ]

    def restore(self, segments: Iterable[Mapping[str, Any]]) -> None:
        for segment in segments:
            try:
                start = datetime.fromisoformat(segment["start"])
                end = datetime.fromisoformat(segment["end"])
            except (KeyError, TypeError, ValueError):
                continue
            self.segments.append((start, end))


# ── The session ─────────────────────────────────────────────────────────────────────────


@dataclass
class FieldEntry:
    name: str
    h5: str | None
    shape: list[int] | None
    cells: int | None = None
    changed: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


class ReviewSession:
    """One review of the pending stop in ``bundle_folder``."""

    def __init__(self, order: Path, bundle_folder: Path, bundle: Mapping[str, Any]) -> None:
        self.order = Path(order)
        self.folder = Path(bundle_folder)
        self.bundle = dict(bundle)
        self.kind: str = self.bundle["bundle_kind"]
        changed = set(self.bundle.get("changed_fields") or [])
        entries = [
            FieldEntry(
                name=item["name"],
                h5=item.get("h5"),
                shape=item.get("shape"),
                cells=item.get("cells"),
                changed=item["name"] in changed,
                raw=dict(item),
            )
            for item in self.bundle.get("fields", [])
        ]
        # After a redo the changed fields come first (KTD18); otherwise keep the bundle's order.
        self.fields: list[FieldEntry] = sorted(entries, key=lambda f: not f.changed)
        self.opened: dict[str, bool] = {f.name: False for f in self.fields}
        self.circles: list[Circle] = []
        self.previous_circles: list[dict[str, Any]] = list(
            self.bundle.get("previous_circles") or []
        )
        self.words = ""
        self.words_field: str | None = None
        self.not_coming_right = False
        self.target_step: str | None = None
        self.clock = ActiveClock()
        self.answered = False
        self._next_id = 1

    # ── Opening ──

    @classmethod
    def open(cls, order: Path, bundle_folder: Path) -> ReviewSession:
        """Read the bundle and restore the draft for this stop, if one was saved."""
        folder = Path(bundle_folder)
        session = cls(order, folder, read_bundle(folder))
        session.restore_draft()
        if (folder / "answer.json").exists():
            session.answered = True
        return session

    @property
    def draft_path(self) -> Path:
        return self.folder / "draft.json"

    @property
    def answer_path(self) -> Path:
        return self.folder / "answer.json"

    @property
    def field_names(self) -> list[str]:
        return [f.name for f in self.fields]

    def entry(self, name: str) -> FieldEntry:
        for item in self.fields:
            if item.name == name:
                return item
        raise KeyError(f"this stop has no field {name!r}")

    def field_path(self, name: str) -> Path | None:
        """The field's ``.h5`` path. Refused once the answer is written (KTD17)."""
        if self.answered:
            raise AnswerGivenError(
                "the answer is written; the fields stay closed until the next stop appears"
            )
        entry = self.entry(name)
        return self.order / entry.h5 if entry.h5 else None

    def mark_opened(self, name: str) -> None:
        self.entry(name)
        self.opened[name] = True

    def unopened(self) -> list[str]:
        return [name for name, flag in self.opened.items() if not flag]

    # ── Circles ──

    def add_circle(
        self, kind: str, shape_type: str, field_name: str, vertices: Sequence[Sequence[float]]
    ) -> Circle:
        if kind not in CIRCLE_KINDS:
            raise ValueError(f"unknown circle kind {kind!r}")
        if shape_type not in SHAPE_TYPES:
            raise ValueError(f"unknown shape type {shape_type!r}")
        entry = self.entry(field_name)
        points = [[float(v[-2]), float(v[-1])] for v in vertices]
        if len(points) < 3:
            raise CircleRefusedError(
                "A circle needs at least three points.", points[0] if points else [0.0, 0.0]
            )
        candidate = Circle(self._next_id, kind, shape_type, field_name, points)
        others = [c for c in self.circles if c.field == field_name and c.kind != kind]
        if others:
            canvas = _canvas_shape([candidate, *others], entry.shape)
            mine = candidate.mask(canvas)
            for other in others:
                if np.any(mine & other.mask(canvas)):
                    message = (
                        "A problem circle cannot cross a correct circle. Draw it again "
                        "around the problem only."
                        if kind == "problem"
                        else "A correct circle cannot cross a problem circle. Draw it "
                        "again around correct cells only."
                    )
                    raise CircleRefusedError(message, points[0])
        self._next_id += 1
        self.circles.append(candidate)
        return candidate

    def undo_last(self) -> Circle | None:
        return self.circles.pop() if self.circles else None

    def delete(self, circle_id: int) -> bool:
        before = len(self.circles)
        self.circles = [c for c in self.circles if c.id != circle_id]
        return len(self.circles) != before

    def replace_circles(self, drawn: Iterable[Mapping[str, Any]]) -> list[CircleRefusedError]:
        """Take the circles as they are drawn now. Returns the ones that were refused."""
        self.circles = []
        refused = []
        for item in drawn:
            try:
                self.add_circle(item["kind"], item["shape_type"], item["field"], item["vertices"])
            except CircleRefusedError as error:
                refused.append(error)
        return refused

    def problem_circles(self) -> list[Circle]:
        return [c for c in self.circles if c.kind == "problem"]

    # ── Draft ──

    def _state(self) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "order_id": self.bundle.get("order_id"),
            "bundle_kind": self.kind,
            "step": self.bundle.get("step"),
            "attempt": self.bundle.get("attempt"),
            "hash": self.bundle.get("hash"),
            "circles": [c.to_answer() for c in self.circles],
            "words": self.words,
            "words_field": self.words_field,
            "not_coming_right": self.not_coming_right,
            "target_step": self.target_step,
            "fields_opened": dict(self.opened),
            "time_segments": self.clock.export(),
        }

    def save_draft(self, now: datetime | None = None) -> None:
        if self.answered:
            return
        now = now or datetime.now(UTC)
        write_json_atomic(
            self.draft_path,
            {"kind": "draft", "schema_version": SCHEMA_VERSION, "saved_at": _iso(now),
             **self._state()},
        )

    def restore_draft(self) -> bool:
        """Restore circles, words and opened marks from a draft of this same result."""
        if not self.draft_path.exists():
            return False
        try:
            draft = read_document(self.draft_path, "draft")
        except ProtocolError:
            return False
        if draft.get("hash") != self.bundle.get("hash"):
            return False
        for circle in draft.get("circles", []):
            try:
                self.add_circle(
                    circle["kind"], circle["shape_type"], circle["field"], circle["vertices"]
                )
            except (CircleRefusedError, KeyError, ValueError):
                continue
        self.words = draft.get("words") or ""
        self.words_field = draft.get("words_field")
        self.not_coming_right = bool(draft.get("not_coming_right"))
        self.target_step = draft.get("target_step")
        for name, flag in (draft.get("fields_opened") or {}).items():
            if name in self.opened:
                self.opened[name] = bool(flag)
        self.clock.restore(draft.get("time_segments") or [])
        return True

    # ── The answer ──

    def check(self, verdict: str) -> None:
        """Raise :class:`AnswerRefusedError` when the answer would break the protocol's rules."""
        if verdict not in ("okay", "send_back"):
            raise ValueError(f"unknown verdict {verdict!r}")
        if verdict == "okay" and self.problem_circles():
            raise AnswerRefusedError(
                f"Okay cannot carry a problem circle, and this review has "
                f"{len(self.problem_circles())}. Use Send back, or delete the problem circles."
            )
        if (
            verdict == "send_back"
            and self.kind != "proposal"
            and not (self.circles or self.words.strip() or self.not_coming_right
                     or self.target_step)
        ):
            raise AnswerRefusedError(
                "Send back needs something to act on: a circle, words, the not-coming-right "
                "mark or, for the final review, a step to go back to."
            )

    def build_answer(self, verdict: str, now: datetime | None = None) -> dict[str, Any]:
        self.check(verdict)
        now = now or datetime.now(UTC)
        self.clock.stop(now)
        clipped = False
        circles = []
        for circle in self.circles:
            item = circle.to_answer()
            shape = self.entry(circle.field).shape
            if shape:
                height, width = shape[-2], shape[-1]
                inside = [
                    [min(max(y, 0.0), height - 1.0), min(max(x, 0.0), width - 1.0)]
                    for y, x in item["vertices"]
                ]
                clipped |= inside != item["vertices"]
                item["vertices"] = inside
            circles.append(item)
        words = []
        if self.words.strip():
            words.append({"text": self.words.strip(), "field": self.words_field})
        state = self._state()
        return {
            "kind": "answer",
            "schema_version": SCHEMA_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "order_id": state["order_id"],
            "bundle_kind": self.kind,
            "step": state["step"],
            "attempt": state["attempt"],
            "hash": state["hash"],
            "verdict": verdict,
            "circles": circles,
            "circles_clipped": clipped,
            "words": words,
            "not_coming_right": self.not_coming_right,
            "target_step": self.target_step,
            "fields_opened": dict(self.opened),
            "time_segments": self.clock.export(),
            "wall_seconds": (
                (now - self.clock.first_seen).total_seconds() if self.clock.first_seen else 0.0
            ),
            "source": "window",
            "program_version": _program_version(),
        }

    def write_answer(self, verdict: str, now: datetime | None = None) -> dict[str, Any]:
        """Write the answer once. The first answer for a stop stands."""
        if self.answered:
            raise AnswerRefusedError("An answer for this stop is already written.")
        answer = self.build_answer(verdict, now)
        self.save_draft(now)
        if not create_json_exclusive(self.answer_path, answer):
            self.answered = True
            raise AnswerRefusedError(
                "An answer for this stop already exists, from this window or a command. "
                "The first answer stands."
            )
        self.answered = True
        return answer


def circle_from_dict(data: Mapping[str, Any], circle_id: int = 0) -> Circle:
    return Circle(
        circle_id, data["kind"], data["shape_type"], data["field"],
        [list(map(float, v)) for v in data["vertices"]],
    )


__all__ = [
    "ActiveClock",
    "AnswerGivenError",
    "AnswerRefusedError",
    "Circle",
    "CircleRefusedError",
    "FieldEntry",
    "FieldImages",
    "ReviewSession",
    "circle_from_dict",
    "load_field_images",
]


# ── Field images ────────────────────────────────────────────────────────────────────────


@dataclass
class FieldImages:
    """One field read into memory: its channels and its label layers. No file stays open."""

    name: str
    channels: dict[str, np.ndarray]
    labels: np.ndarray | None = None
    removed: np.ndarray | None = None


def load_field_images(
    path: Path, name: str, layer_names: Mapping[str, str] | None
) -> FieldImages:
    """Read a field's ``.h5`` through PerCell's own store and close it."""
    from percell4.store import DatasetStore

    store = DatasetStore(Path(path))
    names = [str(n) for n in store.metadata.get("channel_names", [])]
    channels = {
        channel: np.asarray(store.read_channel("intensity", index))
        for index, channel in enumerate(names)
    }
    available = set(store.list_labels())
    layer_names = layer_names or {}
    labels = removed = None
    if layer_names.get("labels") in available:
        labels = np.asarray(store.read_labels(layer_names["labels"]))
    if layer_names.get("removed") in available:
        removed = np.asarray(store.read_labels(layer_names["removed"]))
    return FieldImages(name, channels, labels, removed)


def bundle_notes(bundle: Mapping[str, Any]) -> list[str]:
    """The lines the window shows above a bundle: send-backs, what a redo did, drift."""
    notes = []
    count = bundle.get("send_back_count") or 0
    if count:
        notes.append(f"Sent back {count} time(s) so far.")
    summary = bundle.get("redo_summary")
    if summary:
        notes.append(str(summary))
    elif bundle.get("note_after_send_backs") is True:
        # An older AnalyzeIt sends only the flag; say what it means rather than "True".
        notes.append(
            "This step has been sent back three times or more. If it is not coming right, "
            "send it back with the not coming right mark."
        )
    drift = bundle.get("recipe_drift")
    if drift:
        notes.append(str(drift))
    return notes

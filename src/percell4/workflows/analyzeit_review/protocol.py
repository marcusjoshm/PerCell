"""Reading an AnalyzeIt order folder: review protocol version 1.

Every file is read whole and closed before anything is done with it, so PerCell never holds
a file that AnalyzeIt is about to replace. Every JSON document carries ``kind`` and
``schema_version``; a document of another version is refused with a message that says so.

:func:`read_order_view` turns the folder into what the review window shows: the pending
stop, or one banner for the state the order is in.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 1
SCHEMA_VERSION = 1
BUNDLE_KINDS = frozenset({"step", "final", "proposal"})
VERDICTS = frozenset({"okay", "send_back"})
CIRCLE_KINDS = ("problem", "correct")
SHAPE_TYPES = ("polygon", "ellipse", "lasso")

STATUS_FILE = "status.json"
EVENTS_FILE = "events.jsonl"
#: Written every few seconds by the AnalyzeIt process that is running the order.
HEARTBEAT_FILE = "heartbeat.json"
#: Written every few seconds by ``analyzeit review`` while it watches for answers.
WATCHER_FILE = "watcher.json"
HEARTBEAT_SECONDS = 5
HEARTBEAT_STALE_SECONDS = 30


class ProtocolError(Exception):
    """A file in the order folder that the review cannot use. The message is for Joshua."""


# ── Files ───────────────────────────────────────────────────────────────────────────────


def read_document(path: Path, kind: str) -> dict[str, Any]:
    """Read one JSON document whole, check its kind and version, and close it."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ProtocolError(f"{path.name} is missing") from None
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise ProtocolError(f"{path.name} cannot be read: {error}") from error
    if not isinstance(document, dict) or document.get("kind") != kind:
        raise ProtocolError(f"{path.name} is not a {kind} file")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise ProtocolError(
            f"{path.name} uses version {document.get('schema_version')} of the file format; "
            f"this PerCell reads version {SCHEMA_VERSION}. Update PerCell or AnalyzeIt."
        )
    return document


def read_bundle(folder: Path) -> dict[str, Any]:
    bundle = read_document(Path(folder) / "bundle.json", "bundle")
    if bundle.get("protocol_version") != PROTOCOL_VERSION:
        raise ProtocolError(
            f"this stop uses review protocol {bundle.get('protocol_version')}; this PerCell "
            f"reads protocol {PROTOCOL_VERSION}. Update PerCell or AnalyzeIt."
        )
    if bundle.get("bundle_kind") not in BUNDLE_KINDS:
        raise ProtocolError(f"unknown kind of stop {bundle.get('bundle_kind')!r}")
    return bundle


def write_json_atomic(path: Path, document: Mapping[str, Any]) -> None:
    """Write under a temporary name, then rename over the target."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    data = (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def create_json_exclusive(path: Path, document: Mapping[str, Any]) -> bool:
    """Create ``path`` unless it exists. True when this call created it.

    The document is written under a temporary name and linked into place, so a reader never
    sees half of it, and the first writer wins.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    data = (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            return False
        return True
    finally:
        tmp.unlink(missing_ok=True)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def heartbeat_age(path: Path, now: datetime) -> float | None:
    """Seconds since the heartbeat in ``path`` was written, or None when there is none."""
    try:
        document = read_document(path, "heartbeat")
    except ProtocolError:
        return None
    moment = _parse_time(document.get("at"))
    if moment is None:
        return None
    return max(0.0, (now - moment).total_seconds())


def last_refusal(order: Path, bundle_folder: str) -> dict[str, Any] | None:
    """The newest refused answer for the pending stop, from the event log, or None."""
    try:
        lines = (Path(order) / EVENTS_FILE).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    refusal = None
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") == "bundle_published":
            refusal = None if event.get("bundle") == bundle_folder else refusal
        elif event.get("event") == "answer_refused":
            refusal = event
    return refusal


# ── What the window shows ───────────────────────────────────────────────────────────────

BANNERS = {
    "intake": "AnalyzeIt is reading the order.",
    "scoped": "AnalyzeIt is planning the work.",
    "planned": "AnalyzeIt is about to run the steps.",
    "running": "AnalyzeIt is running the steps. The next stop will appear here.",
    "redoing": "AnalyzeIt is redoing a step from your send-back.",
    "limited": "AnalyzeIt cannot do this order with the tools it has. "
    "Run `analyzeit limitations` to see what is missing.",
    "unresolved": "This order was left unresolved.",
    "delivered": "This order is delivered. Its results are in the deliverable folder.",
}
STOPPED_UNEXPECTEDLY = (
    "AnalyzeIt stopped unexpectedly: the run has not reported for more than "
    f"{HEARTBEAT_STALE_SECONDS} seconds. Run `analyzeit resume` to carry on."
)


@dataclass(frozen=True)
class OrderView:
    """What the review window shows for an order folder at one moment.

    ``pending`` is set only when a stop waits for an answer that has not been written yet.
    Otherwise ``banner`` says why there is nothing to answer, and both buttons are off.
    """

    order: Path
    state: str | None
    banner: str
    pending: dict[str, Any] | None = None
    bundle_folder: Path | None = None
    refusal: str | None = None
    answer_written: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def can_answer(self) -> bool:
        return self.pending is not None and not self.answer_written

    @property
    def key(self) -> tuple[Any, ...]:
        """Changes whenever the window has something new to show."""
        pending = self.pending or {}
        return (
            self.state, self.banner, pending.get("bundle"), pending.get("hash"),
            self.refusal, self.answer_written,
        )


def read_order_view(order: Path, now: datetime | None = None) -> OrderView:
    order = Path(order)
    now = now or datetime.now(UTC)
    try:
        status = read_document(order / STATUS_FILE, "status")
    except ProtocolError as error:
        return OrderView(order, None, f"This folder cannot be reviewed: {error}.")
    state = status.get("state")
    stop = status.get("stop")
    watcher_age = heartbeat_age(order / WATCHER_FILE, now)
    watched = watcher_age is not None and watcher_age <= HEARTBEAT_STALE_SECONDS

    if stop:
        detail = f": {stop.get('detail')}" if stop.get("detail") else ""
        return OrderView(
            order, state,
            f"AnalyzeIt stopped ({stop.get('reason')}){detail}. "
            "Run `analyzeit resume` when the cause is fixed.",
        )

    pending = status.get("pending")
    if state == "awaiting_review" and pending:
        folder = order / pending["bundle"]
        refusal_event = last_refusal(order, pending["bundle"])
        refusal = None
        if refusal_event:
            refusal = (
                f"Your last answer was refused ({refusal_event.get('reason')}): "
                f"{refusal_event.get('detail', '')}"
            )
        if (folder / "answer.json").exists():
            banner = (
                "Your answer is written. AnalyzeIt is taking it in."
                if watched
                else "Your answer is written. Nothing is watching this order, so AnalyzeIt "
                "takes it in at the next `analyzeit resume` or `analyzeit review`."
            )
            return OrderView(
                order, state, banner, pending, folder, refusal, answer_written=True
            )
        return OrderView(order, state, "", pending, folder, refusal)

    if status.get("harness") == "ready":
        age = heartbeat_age(order / HEARTBEAT_FILE, now)
        updated = _parse_time(status.get("updated"))
        status_age = (now - updated).total_seconds() if updated else None
        quiet = age is None or age > HEARTBEAT_STALE_SECONDS
        if quiet and (status_age is None or status_age > HEARTBEAT_STALE_SECONDS):
            return OrderView(order, state, STOPPED_UNEXPECTEDLY)
    return OrderView(order, state, BANNERS.get(state, f"The order is {state}."))

"""Reviewing an AnalyzeIt order in PerCell, without any GUI code.

AnalyzeIt and PerCell never connect. They share an order folder, and this package reads
what AnalyzeIt published there and writes Joshua's answer back. The protocol is version 1
of AnalyzeIt's ``docs/review-protocol.md``:

- :mod:`.protocol` reads ``status.json``, the heartbeat files and a stop's ``bundle.json``,
  and works out what the review window should say when nothing waits for an answer.
- :mod:`.session` holds one review in progress: the fields opened, the circles, the words,
  the active time, the draft, and the answer it writes.

The window and its controller live in ``percell4.gui.workflows.analyzeit_review``.
"""

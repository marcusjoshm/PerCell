"""A JVM-free stand-in for ``percell4.adapters.bioformats_host.serve``.

It speaks the same pipe protocol, so the parent client's polling, callbacks,
cancel and kill logic can be tested without Java. Pass it as the client's
``host_target``; it runs in a real spawned child, so it must stay module
level and import nothing heavy.
"""

from __future__ import annotations

import time


def serve(conn, config: dict) -> None:
    """Answer ``probe`` with one empty record per path, ``delay`` seconds apart."""
    delay = float(config.get("delay", 0.05))
    conn.send(("ready", {"jvm_start_s": 0.0, "java_version": "fake"}))
    while True:
        try:
            op, req_id, payload = conn.recv()
        except EOFError:
            return
        if op == "shutdown":
            return
        if op != "probe":
            conn.send(("error", req_id, (f"fake host does not support {op!r}", "")))
            continue
        for path in payload:
            time.sleep(delay)
            conn.send(
                (
                    "file",
                    req_id,
                    {
                        "path": path,
                        "format_name": "Fake transport",
                        "series": [],
                        "used_files": [path],
                        "error": None,
                        "trace": "",
                    },
                )
            )
        conn.send(("done", req_id, None))

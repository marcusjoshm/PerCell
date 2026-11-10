"""A JVM-free stand-in for ``percell4.adapters.bioformats_host.serve``.

It speaks the same pipe protocol, so the parent client's polling, callbacks,
cancel and kill logic can be tested without Java. Pass it as the client's
``host_target``; it runs in a real spawned child, so it must stay module
level and import nothing heavy.
"""

from __future__ import annotations

import time

import numpy as np


def serve(conn, config: dict) -> None:
    """Answer ``probe`` with one empty record per path, ``delay`` seconds apart.

    ``stream`` sends the planes of a ``stream_shape`` (T, C, Z, H, W) stack,
    z innermost, each filled with ``100 * t + 10 * c + z``.
    """
    delay = float(config.get("delay", 0.05))
    conn.send(("ready", {"jvm_start_s": 0.0, "java_version": "fake"}))
    while True:
        try:
            op, req_id, payload = conn.recv()
        except EOFError:
            return
        if op == "shutdown":
            return
        if op == "stream":
            n_t, n_c, n_z, h, w = config.get("stream_shape", (1, 2, 3, 4, 5))
            channels = payload.get("channels") or range(n_c)
            for t in range(n_t):
                for c in channels:
                    for z in range(n_z):
                        time.sleep(delay)
                        plane = np.full((h, w), 100 * t + 10 * c + z, dtype=np.uint16)
                        conn.send(("zplane", req_id, (t, int(c), z, plane)))
            conn.send(("done", req_id, None))
            continue
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

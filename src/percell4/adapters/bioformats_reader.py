"""Bio-Formats reader client: the parent side of the in-file reader.

Implements :class:`percell4.ports.image_reader.ImageReader`. Bio-Formats runs
in a long-lived ``spawn`` child (:func:`percell4.adapters.bioformats_host.serve`)
that holds one JVM. This module never imports JPype.

* Requests go over a pipe that is polled with a short timeout, so
  ``on_file`` / ``on_plane`` fire per result and ``is_cancelled`` is checked
  between polls. A GUI caller can pump events from those callbacks.
* Cancel kills the child. A crash is noticed when the child dies. Either way
  the next request starts a fresh child.
* :meth:`BioformatsReader.close` shuts the child down. An ``atexit`` handler
  closes every live reader, and the child is a daemon that also exits when
  its pipe closes, so no JVM outlives PerCell.
* Java and the jar are resolved through :mod:`percell4.adapters.java_runtime`.
  When either is missing the typed error carries the resolver's reason.
"""

from __future__ import annotations

import atexit
import logging
import multiprocessing
import os
import time
import weakref
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

from percell4.adapters.java_runtime import describe_java_environment
from percell4.config.advanced import load_advanced_settings
from percell4.domain.errors import (
    BioformatsReadError,
    BioformatsUnavailableError,
    JavaUnavailableError,
)
from percell4.domain.io.infile import Z_METHODS, FileProbe, ImportSource, SeriesProbe
from percell4.ports.image_reader import IsCancelled, OnFile, OnPlane, ProjectedPlane, RawPlane

logger = logging.getLogger(__name__)

#: Seconds to wait for the child to start its JVM before giving up.
DEFAULT_STARTUP_TIMEOUT_S = 120.0

#: Pipe poll interval: how often ``is_cancelled`` is checked while waiting.
DEFAULT_POLL_INTERVAL_S = 0.05

_SHUTDOWN_TIMEOUT_S = 5.0

_LIVE_READERS: weakref.WeakSet[BioformatsReader] = weakref.WeakSet()


def _close_all_readers() -> None:
    for reader in list(_LIVE_READERS):
        try:
            reader.close()
        except Exception:  # noqa: BLE001 - exit must not fail
            pass


atexit.register(_close_all_readers)


def _default_host_target() -> Callable[..., None]:
    from percell4.adapters import bioformats_host

    return bioformats_host.serve


def _never_cancelled() -> bool:
    return False


class _CancelledError(Exception):
    pass


class _CrashedError(Exception):
    def __init__(self, exitcode: int | None) -> None:
        super().__init__(f"the Bio-Formats reader process stopped (exit code {exitcode})")


class _HostError(Exception):
    def __init__(self, message: str, trace: str) -> None:
        super().__init__(message)
        self.message = message
        self.trace = trace


def _series_probe(record: dict) -> SeriesProbe:
    return SeriesProbe(
        index=int(record["index"]),
        name=record["name"],
        size_t=int(record["size_t"]),
        size_c=int(record["size_c"]),
        size_z=int(record["size_z"]),
        size_y=int(record["size_y"]),
        size_x=int(record["size_x"]),
        dimension_order=record["dimension_order"],
        pixel_type=record["pixel_type"],
        is_rgb=bool(record["is_rgb"]),
        is_interleaved=bool(record["is_interleaved"]),
        physical_x_um=record["physical_x_um"],
        physical_y_um=record["physical_y_um"],
        physical_z_um=record["physical_z_um"],
        channel_names=tuple(record["channel_names"]),
        axis_source=record["axis_source"],
    )


def _stat(path: Path) -> tuple[int | None, int | None]:
    try:
        st = os.stat(path)
    except OSError:
        return None, None
    return st.st_size, st.st_mtime_ns


class BioformatsReader:
    """Reads in-file microscopy data through Bio-Formats in a child process.

    ``java_home`` and ``jar`` default to what
    :func:`~percell4.adapters.java_runtime.describe_java_environment` resolves.
    ``max_heap_mb`` defaults to the ``java_max_heap_mb`` advanced setting,
    read each time a child starts. ``host_target`` / ``host_config`` replace
    the child entry point, for tests of the transport without Java.

    Not thread-safe, and one request runs at a time: finish or abandon a
    :meth:`read_projected` iterator before the next call. Abandoning one
    mid-way kills the child.
    """

    def __init__(
        self,
        *,
        java_home: str | Path | None = None,
        jar: str | Path | None = None,
        max_heap_mb: int | None = None,
        cache_dir: Path | None = None,
        host_target: Callable[..., None] | None = None,
        host_config: dict[str, Any] | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL_S,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_S,
    ) -> None:
        self._java_home = Path(java_home) if java_home is not None else None
        self._jar = Path(jar) if jar is not None else None
        self._max_heap_mb = max_heap_mb
        self._cache_dir = cache_dir
        self._host_target = host_target
        self._host_config = dict(host_config or {})
        self._poll_interval = poll_interval
        self._startup_timeout = startup_timeout
        self._proc: multiprocessing.process.BaseProcess | None = None
        self._conn = None
        self._request_id = 0
        self._busy = False
        #: What the last child reported on start: ``jvm_start_s``, ``java_version``
        #: and ``startup_s`` (spawn to ready, measured here).
        self.last_start_info: dict[str, Any] = {}

    # -- state -------------------------------------------------------------

    @property
    def java_home(self) -> Path | None:
        return self._java_home

    @property
    def jar(self) -> Path | None:
        return self._jar

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.is_alive()

    @property
    def child_pid(self) -> int | None:
        return self._proc.pid if self.is_running else None

    # -- lifecycle -----------------------------------------------------------

    def _resolve(self) -> None:
        """Fill in Java and the jar, or raise the typed error with its reason."""
        if self._java_home is not None and self._jar is not None:
            return
        env = describe_java_environment(cache_dir=self._cache_dir)
        if self._java_home is None:
            if not env.java.ready:
                raise JavaUnavailableError(env.java.reason)
            self._java_home = env.java.java_home
        if self._jar is None:
            if not env.jar.ready:
                raise BioformatsUnavailableError(env.jar.reason)
            self._jar = env.jar.jar

    def _heap_mb(self) -> int | None:
        if self._max_heap_mb is not None:
            return self._max_heap_mb
        try:
            return load_advanced_settings().java_max_heap_mb
        except Exception:  # noqa: BLE001 - a broken settings file leaves the default
            return None

    def start(self, is_cancelled: IsCancelled | None = None) -> dict[str, Any]:
        """Start the child now (it otherwise starts on the first request).

        Returns the start info. Raises :class:`JavaUnavailableError` when the
        JVM does not start, with the child's one-line reason.
        """
        if not self._ensure_child(is_cancelled or _never_cancelled):
            return {}
        return self.last_start_info

    def _ensure_child(self, is_cancelled: IsCancelled) -> bool:
        """Start a child unless one is running. False when cancelled meanwhile."""
        if self.is_running:
            return True
        self._reap()
        self._resolve()
        target = self._host_target or _default_host_target()
        config = {
            "java_home": str(self._java_home),
            "jar": str(self._jar),
            "max_heap_mb": self._heap_mb(),
            **self._host_config,
        }
        ctx = multiprocessing.get_context("spawn")
        parent_conn, child_conn = ctx.Pipe(duplex=True)
        started = time.perf_counter()
        proc = ctx.Process(
            target=target,
            args=(child_conn, config),
            daemon=True,
            name="percell-bioformats-reader",
        )
        proc.start()
        child_conn.close()
        self._proc, self._conn = proc, parent_conn
        _LIVE_READERS.add(self)

        deadline = started + self._startup_timeout
        while True:
            if is_cancelled():
                self._kill()
                return False
            if parent_conn.poll(self._poll_interval):
                try:
                    message = parent_conn.recv()
                except (EOFError, OSError):
                    message = ("fatal", f"the reader process exited (code {proc.exitcode})", "")
                if message[0] == "ready":
                    info = dict(message[1])
                    info["startup_s"] = time.perf_counter() - started
                    self.last_start_info = info
                    logger.info(
                        "Bio-Formats reader started (pid %s, Java %s, JVM %.2fs, total %.2fs)",
                        proc.pid,
                        info.get("java_version"),
                        info.get("jvm_start_s", 0.0),
                        info["startup_s"],
                    )
                    return True
                self._kill()
                _, reason, trace = message
                if trace:
                    logger.warning("Bio-Formats reader failed to start: %s\n%s", reason, trace)
                raise JavaUnavailableError(
                    f"The Bio-Formats reader could not start Java from {self._java_home}: {reason}"
                )
            if not proc.is_alive():
                code = proc.exitcode
                self._kill()
                raise JavaUnavailableError(
                    f"The Bio-Formats reader process exited during start (exit code {code})."
                )
            if time.perf_counter() > deadline:
                self._kill()
                raise JavaUnavailableError(
                    f"The Bio-Formats reader did not start within {self._startup_timeout:.0f} s."
                )

    def _reap(self) -> None:
        """Forget a dead child and close its pipe."""
        if self._proc is not None and not self._proc.is_alive():
            self._proc.join(0)
            self._proc = None
        if self._proc is None and self._conn is not None:
            self._conn.close()
            self._conn = None

    def _kill(self) -> None:
        """Stop the child now. Used on cancel, crash and start failure."""
        proc, conn = self._proc, self._conn
        self._proc = self._conn = None
        if proc is not None:
            if proc.is_alive():
                proc.terminate()
                proc.join(_SHUTDOWN_TIMEOUT_S)
            if proc.is_alive():
                proc.kill()
                proc.join(_SHUTDOWN_TIMEOUT_S)
        if conn is not None:
            conn.close()
        self._busy = False

    def close(self) -> None:
        """Shut the child down: ask politely, then terminate. Idempotent."""
        proc, conn = self._proc, self._conn
        if proc is not None and proc.is_alive() and conn is not None and not self._busy:
            try:
                conn.send(("shutdown", None, None))
            except OSError:
                pass
            proc.join(_SHUTDOWN_TIMEOUT_S)
        self._kill()
        _LIVE_READERS.discard(self)

    def __del__(self) -> None:  # pragma: no cover - best effort at GC
        try:
            self._kill()
        except Exception:  # noqa: BLE001
            pass

    # -- transport -----------------------------------------------------------

    def _messages(
        self, op: str, payload: Any, is_cancelled: IsCancelled
    ) -> Iterator[tuple[str, Any]]:
        """Send one request; yield ``(kind, payload)`` until ``done``.

        Raises :class:`_CancelledError` (child killed), :class:`_CrashedError` (child
        died) or :class:`_HostError` (the request failed; child still up).
        """
        if self._busy:
            raise RuntimeError("the Bio-Formats reader is already serving a request")
        self._request_id += 1
        request_id = self._request_id
        conn, proc = self._conn, self._proc
        self._busy = True
        finished = False
        try:
            try:
                conn.send((op, request_id, payload))
            except OSError:
                raise _CrashedError(proc.exitcode) from None
            while True:
                if is_cancelled():
                    raise _CancelledError
                if conn.poll(self._poll_interval):
                    try:
                        kind, rid, body = conn.recv()
                    except (EOFError, OSError):
                        proc.join(1)
                        raise _CrashedError(proc.exitcode) from None
                    if rid != request_id:
                        continue
                    if kind == "done":
                        finished = True
                        yield kind, body
                        return
                    if kind == "error":
                        finished = True
                        raise _HostError(*body)
                    if kind == "cancelled":
                        raise _CancelledError
                    yield kind, body
                elif not proc.is_alive():
                    raise _CrashedError(proc.exitcode)
        finally:
            self._busy = False
            if not finished:
                # cancelled, crashed or abandoned mid-stream: the child may
                # still be sending, so it cannot serve the next request.
                self._kill()

    def _call(self, op: str, payload: Any) -> Any:
        """A request with one ``done`` answer. Raises BioformatsReadError."""
        self._ensure_child(_never_cancelled)
        try:
            for kind, body in self._messages(op, payload, _never_cancelled):
                if kind == "done":
                    return body
        except _HostError as exc:
            logger.warning("Bio-Formats request %s failed: %s\n%s", op, exc.message, exc.trace)
            raise BioformatsReadError(exc.message) from None
        except _CrashedError as exc:
            raise BioformatsReadError(str(exc)) from None
        raise BioformatsReadError(f"the Bio-Formats reader gave no answer to {op!r}")

    # -- port ------------------------------------------------------------------

    def probe(
        self,
        paths: Sequence[Path],
        on_file: OnFile | None = None,
        is_cancelled: IsCancelled | None = None,
    ) -> list[FileProbe]:
        """Probe each file's metadata. See :meth:`ImageReader.probe`.

        A file during which the child dies is retried once in a fresh child
        (the child may have died while idle); if it dies again the file gets
        an error record and a fresh child serves the rest.
        """
        cancelled = is_cancelled or _never_cancelled
        pending = [Path(p) for p in paths]
        results: list[FileProbe] = []
        crashed_on: Path | None = None

        def emit(record: FileProbe) -> None:
            results.append(record)
            pending.pop(0)
            if on_file is not None:
                on_file(record)

        while pending:
            if not self._ensure_child(cancelled):
                return results
            try:
                request = [str(p.absolute()) for p in pending]
                for kind, body in self._messages("probe", request, cancelled):
                    if kind == "file":
                        emit(self._file_probe(pending[0], body))
            except _CancelledError:
                return results
            except (_CrashedError, _HostError) as exc:
                if isinstance(exc, _CrashedError) and crashed_on != pending[0]:
                    crashed_on = pending[0]
                    continue
                reason = exc.message if isinstance(exc, _HostError) else str(exc)
                logger.warning("Bio-Formats probe of %s failed: %s", pending[0], reason)
                size, mtime = _stat(pending[0])
                emit(FileProbe(path=pending[0], size_bytes=size, mtime_ns=mtime, error=reason))
        return results

    @staticmethod
    def _file_probe(path: Path, record: dict) -> FileProbe:
        size, mtime = _stat(path)
        error = record.get("error")
        if error:
            logger.warning(
                "Bio-Formats could not read %s: %s\n%s", path, error, record.get("trace", "")
            )
        return FileProbe(
            path=path,
            size_bytes=size,
            mtime_ns=mtime,
            format_name=record.get("format_name", ""),
            series=tuple(_series_probe(s) for s in record.get("series", ())),
            used_files=tuple(Path(f) for f in record.get("used_files", ())),
            error=error or None,
        )

    def read_projected(
        self,
        source: ImportSource,
        z_method: str,
        on_plane: OnPlane | None = None,
        is_cancelled: IsCancelled | None = None,
    ) -> Iterator[ProjectedPlane]:
        """Stream projected planes. See :meth:`ImageReader.read_projected`.

        Raises :class:`BioformatsReadError` from the iterator when a plane
        cannot be read or the child dies. Java setup errors raise here, at
        the call.
        """
        if z_method not in Z_METHODS:
            raise ValueError(f"unknown z method {z_method!r}; expected one of {Z_METHODS}")
        self._resolve()
        request = {
            "path": str(Path(source.path).absolute()),
            "series": int(source.series_index),
            "channels": [int(c) for c in source.channel_indices],
            "swap_zt": not source.axis_map.is_identity,
            "z_method": z_method,
        }
        return self._stream(
            source.path, "read", request, on_plane, is_cancelled or _never_cancelled
        )

    def read_planes(
        self,
        source: ImportSource,
        on_plane: OnPlane | None = None,
        is_cancelled: IsCancelled | None = None,
    ) -> Iterator[RawPlane]:
        """Stream raw planes, z innermost. See :meth:`ImageReader.read_planes`.

        Errors behave as in :meth:`read_projected`.
        """
        self._resolve()
        request = {
            "path": str(Path(source.path).absolute()),
            "series": int(source.series_index),
            "channels": [int(c) for c in source.channel_indices],
            "swap_zt": not source.axis_map.is_identity,
        }
        return self._stream(
            source.path, "stream", request, on_plane, is_cancelled or _never_cancelled
        )

    def _stream(self, path, op, request, on_plane, is_cancelled) -> Iterator[Any]:
        yielded = False
        for attempt in (1, 2):
            if not self._ensure_child(is_cancelled):
                return
            messages = self._messages(op, request, is_cancelled)
            try:
                for kind, body in messages:
                    if kind == "plane":
                        t, c, plane = body
                        if on_plane is not None:
                            on_plane(t, c)
                        yielded = True
                        yield t, c, plane
                    elif kind == "zplane":
                        t, c, z, plane = body
                        if z == 0 and on_plane is not None:
                            on_plane(t, c)
                        yielded = True
                        yield t, c, z, plane
                return
            except _CancelledError:
                return
            except _HostError as exc:
                logger.warning(
                    "Bio-Formats could not read %s: %s\n%s", path, exc.message, exc.trace
                )
                raise BioformatsReadError(f"Could not read {path}: {exc.message}") from None
            except _CrashedError as exc:
                # A child that died while idle is noticed only here: retry
                # once in a fresh child, but never after planes went out.
                if attempt == 1 and not yielded:
                    continue
                raise BioformatsReadError(f"Could not read {path}: {exc}") from None
            finally:
                messages.close()  # abandoned mid-stream: kills the child at once

    # -- diagnostics -------------------------------------------------------------

    def read_plane(self, path: str | Path, series: int, z: int, c: int, t: int):
        """One raw plane in its native dtype, at file indices. Not part of the port."""
        request = {"path": str(Path(path).absolute()), "series": series, "z": z, "c": c, "t": t}
        return self._call("plane", request)

    def suffixes(self) -> list[str]:
        """The file suffixes the loaded Bio-Formats readers claim, without dots."""
        return list(self._call("suffixes", None))

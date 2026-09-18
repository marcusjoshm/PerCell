"""Bio-Formats reader host: the child-process side of the in-file reader.

The parent client (:mod:`percell4.adapters.bioformats_reader`) starts
:func:`serve` in a ``spawn`` child. The child starts one JVM with the
Bio-Formats jar and answers requests over a pipe until told to stop. Keeping
the JVM out of the GUI process means a JVM crash or out-of-memory error
cannot take PerCell down, cancel can kill the child, and a new heap size
applies to the next child (plan KTD2).

Constraints (same rules as ``parallel_decode``):

* Module level and import-light: stdlib, numpy and JPype only. No Qt, no
  ``percell4.gui``/``interfaces``, and no other percell4 module. Records go
  back as plain dicts; the parent builds ``FileProbe`` from them.
* Bio-Formats is configured defensively (plan KTD5): ``ImageReader``
  directly, never ``Memoizer`` (so no ``.bfmemo`` is read or written beside
  user data), ``setGroupFiles(False)``, flattened resolutions off, headless
  AWT.

Pipe protocol. Parent to child, ``(op, req_id, payload)``:

* ``("probe", id, [path, ...])`` -> one ``("file", id, record)`` per path
* ``("read", id, {path, series, channels, swap_zt, z_method})`` -> one
  ``("plane", id, (t, c, float32 plane))`` per effective (t, c)
* ``("plane", id, {path, series, z, c, t})`` -> one raw native-dtype plane
  (diagnostics and tests)
* ``("suffixes", id, None)`` -> the suffixes the loaded readers claim
* ``("cancel", id, None)`` -> the running request stops between planes
* ``("shutdown", None, None)`` -> the child exits

Every request ends with ``("done", id, payload)``, ``("error", id,
(message, trace))`` or ``("cancelled", id, None)``. On start the child sends
``("ready", info)`` or ``("fatal", message, trace)``.

Axis source heuristic. Bio-Formats never says whether it defaulted an axis.
The plain TIFF readers put the pages of a TIFF that carries no axis metadata
into T (not Z). So for a series read by a plain TIFF reader
(:data:`_PLAIN_TIFF_READERS`) with more than one plane, the axes count as
``"assumed"`` unless the first IFD's ImageDescription is an ImageJ header
that declares ``channels=``, ``slices=`` or ``frames=``, or carries OME-XML.
Every other reader reads its axes from the format's own metadata and reports
``"metadata"``. For such an assumed-axes TIFF the X/Y physical size is also
dropped unless the TIFF ResolutionUnit is inch or centimetre: with unit NONE
Bio-Formats reports the raw resolution (often 1.0) as µm, which is no size.
"""

from __future__ import annotations

import glob
import os
import time
import traceback

import numpy as np

#: Bio-Formats pixel type names -> numpy dtypes (byte order applied per file).
_PIXEL_DTYPES = {
    "int8": "i1",
    "uint8": "u1",
    "int16": "i2",
    "uint16": "u2",
    "int32": "i4",
    "uint32": "u4",
    "float": "f4",
    "double": "f8",
    "bit": "u1",
}

#: Readers that take axes from a plain TIFF's page count, not from metadata.
_PLAIN_TIFF_READERS = frozenset({"TiffDelegateReader", "TiffReader", "TiffJAIReader"})

#: ImageJ header keys that declare the hyperstack axes.
_IMAGEJ_AXIS_KEYS = ("channels=", "slices=", "frames=")

#: TIFF ResolutionUnit values that make X/Y resolution a physical size.
_RESUNIT_INCH = 2
_RESUNIT_CENTIMETER = 3


# ---------------------------------------------------------------------------
# JVM start
# ---------------------------------------------------------------------------


def find_libjvm(java_home: str) -> str:
    """The JVM shared library inside ``java_home`` on macOS, Linux or Windows.

    Known layouts first (JDK 9+ ``lib/server``, Linux ``lib/<arch>/server``,
    Java 8 ``jre/lib/...``, Windows ``bin/server/jvm.dll``, a macOS bundle
    root), then a bounded search. Raises ``FileNotFoundError`` naming the home.
    """
    homes = [java_home, os.path.join(java_home, "Contents", "Home")]
    names = ("libjvm.dylib", "libjvm.so", "jvm.dll")
    relative = (
        ("lib", "server"),
        ("jre", "lib", "server"),
        ("bin", "server"),
        ("jre", "bin", "server"),
        ("lib", "*", "server"),
        ("jre", "lib", "*", "server"),
        ("lib", "client"),
        ("bin", "client"),
    )
    for home in homes:
        for parts in relative:
            for name in names:
                for hit in sorted(glob.glob(os.path.join(home, *parts, name))):
                    if os.path.isfile(hit):
                        return hit
    for name in names:
        hits = sorted(glob.glob(os.path.join(java_home, "**", name), recursive=True))
        if hits:
            return hits[0]
    raise FileNotFoundError(f"No JVM library (libjvm or jvm.dll) found under {java_home}.")


def jvm_options(max_heap_mb: int | None) -> list[str]:
    """JVM options: headless AWT always, ``-Xmx`` when a heap size is set."""
    options = ["-Djava.awt.headless=true"]
    if max_heap_mb:
        options.append(f"-Xmx{int(max_heap_mb)}m")
    return options


def _start_jvm(config: dict) -> dict:
    import jpype

    started = time.perf_counter()
    jpype.startJVM(
        find_libjvm(config["java_home"]),
        *jvm_options(config.get("max_heap_mb")),
        classpath=[config["jar"]],
        convertStrings=False,
    )
    import jpype.imports  # noqa: F401 - enables ``from loci... import``
    from loci.common import DebugTools

    DebugTools.setRootLevel("ERROR")  # the jar logs DEBUG to stdout otherwise
    from java.lang import Runtime, System

    return {
        "jvm_start_s": time.perf_counter() - started,
        "java_version": str(System.getProperty("java.version")),
        "max_heap_bytes": int(Runtime.getRuntime().maxMemory()),
    }


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def _one_line(exc: BaseException) -> str:
    """``SimpleName: message``, first line only, for a Java or Python error."""
    get_class = getattr(exc, "getClass", None)
    if get_class is not None:
        name = str(get_class().getSimpleName())
        message = exc.getMessage()
        text = f"{name}: {message}" if message is not None else name
    else:
        text = f"{type(exc).__name__}: {exc}"
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    return lines[0] if lines else type(exc).__name__


def _trace(exc: BaseException) -> str:
    text = traceback.format_exc()
    stacktrace = getattr(exc, "stacktrace", None)
    if callable(stacktrace):
        try:
            text += "\nJava stack trace:\n" + str(stacktrace())
        except Exception:  # noqa: BLE001 - the trace is best effort
            pass
    return text


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _open(path: str):
    """An initialised ``ImageReader`` with an OME-XML store (KTD5 settings)."""
    from loci.formats import ImageReader, MetadataTools

    reader = ImageReader()
    store = MetadataTools.createOMEXMLMetadata()
    reader.setMetadataStore(store)
    reader.setGroupFiles(False)
    reader.setFlattenedResolutions(False)
    reader.setId(path)
    return reader, store


def _length_um(length) -> float | None:
    if length is None:
        return None
    from ome.units import UNITS

    try:
        value = length.value(UNITS.MICROMETER)
    except Exception:  # noqa: BLE001 - a unit with no metric conversion
        return None
    if value is None:
        return None
    value = float(value.doubleValue())
    return value if value > 0 and np.isfinite(value) else None


def _tiff_header(path: str) -> tuple[str, int | None]:
    """First-IFD ImageDescription and ResolutionUnit of a TIFF."""
    from loci.formats.tiff import IFD, TiffParser

    parser = TiffParser(path)
    try:
        ifd = parser.getFirstIFD()
        if ifd is None:
            return "", None
        comment = ifd.getComment()
        unit = ifd.getIFDIntValue(IFD.RESOLUTION_UNIT, -1)
        return ("" if comment is None else str(comment)), (None if unit < 0 else int(unit))
    finally:
        parser.getStream().close()


def _tiff_has_axis_metadata(description: str) -> bool:
    text = description.lstrip()
    if "<OME" in text[:4096]:
        return True
    if text.startswith("ImageJ="):
        return any(line.strip().startswith(_IMAGEJ_AXIS_KEYS) for line in text.splitlines())
    return False


def _series_record(reader, store, index: int, tiff: tuple[str, int | None] | None) -> dict:
    from loci.formats import FormatTools

    reader.setSeries(index)
    size_z, size_c, size_t = int(reader.getSizeZ()), int(reader.getSizeC()), int(reader.getSizeT())
    physical = [
        _length_um(store.getPixelsPhysicalSizeX(index)),
        _length_um(store.getPixelsPhysicalSizeY(index)),
        _length_um(store.getPixelsPhysicalSizeZ(index)),
    ]
    axis_source = "metadata"
    if tiff is not None and size_z * size_t > 1:
        description, unit = tiff
        if not _tiff_has_axis_metadata(description):
            axis_source = "assumed"
            if unit not in (_RESUNIT_INCH, _RESUNIT_CENTIMETER):
                physical[0] = physical[1] = None
    names = []
    for c in range(int(store.getChannelCount(index))):
        name = store.getChannelName(index, c)
        names.append("" if name is None else str(name))
    image_name = store.getImageName(index)
    return {
        "index": index,
        "name": "" if image_name is None else str(image_name),
        "size_t": size_t,
        "size_c": size_c,
        "size_z": size_z,
        "size_y": int(reader.getSizeY()),
        "size_x": int(reader.getSizeX()),
        "dimension_order": str(reader.getDimensionOrder()),
        "pixel_type": str(FormatTools.getPixelTypeString(reader.getPixelType())),
        "is_rgb": bool(reader.isRGB()),
        "is_interleaved": bool(reader.isInterleaved()),
        "physical_x_um": physical[0],
        "physical_y_um": physical[1],
        "physical_z_um": physical[2],
        "channel_names": names,
        "axis_source": axis_source,
    }


def probe_file(path: str) -> dict:
    """Metadata of one file as a plain dict. Java errors become ``error``."""
    record = {
        "path": path,
        "format_name": "",
        "series": [],
        "used_files": [],
        "error": None,
        "trace": "",
    }
    reader = None
    try:
        reader, store = _open(path)
        record["format_name"] = str(reader.getFormat())
        record["used_files"] = [str(f) for f in reader.getUsedFiles()]
        delegate = str(reader.getReader().getClass().getSimpleName())
        tiff = _tiff_header(path) if delegate in _PLAIN_TIFF_READERS else None
        record["series"] = [
            _series_record(reader, store, s, tiff) for s in range(int(reader.getSeriesCount()))
        ]
    except Exception as exc:  # noqa: BLE001 - one bad file must not end the probe
        record.update(series=[], error=_one_line(exc), trace=_trace(exc))
    finally:
        if reader is not None:
            try:
                reader.close()
            except Exception:  # noqa: BLE001
                pass
    return record


class _PlaneReader:
    """Reads one sample plane of the current series as a native numpy array."""

    def __init__(self, reader) -> None:
        from loci.formats import FormatTools

        name = str(FormatTools.getPixelTypeString(reader.getPixelType()))
        order = "<" if reader.isLittleEndian() else ">"
        self.dtype = np.dtype(order + _PIXEL_DTYPES[name])
        self.reader = reader
        self.height = int(reader.getSizeY())
        self.width = int(reader.getSizeX())
        self.samples = max(1, int(reader.getRGBChannelCount()))
        self.interleaved = bool(reader.isInterleaved())

    def read(self, z: int, c: int, t: int) -> np.ndarray:
        """Plane at file indices (z, c, t); ``c`` counts RGB samples."""
        index = self.reader.getIndex(z, c // self.samples, t)
        raw = np.frombuffer(bytes(self.reader.openBytes(index)), dtype=self.dtype)
        if self.samples == 1:
            plane = raw.reshape(self.height, self.width)
        elif self.interleaved:
            plane = raw.reshape(self.height, self.width, self.samples)[:, :, c % self.samples]
        else:
            plane = raw.reshape(self.samples, self.height, self.width)[c % self.samples]
        return plane.astype(self.dtype.newbyteorder("="), copy=False)


def project_planes(planes, z_method: str) -> np.ndarray:
    """Stream-project an iterable of planes (plan KTD9) to float32.

    Only one plane and the accumulator are held at a time. Max keeps the
    native dtype; sum and mean accumulate in float64.
    """
    acc = None
    count = 0
    for plane in planes:
        if acc is None:
            acc = plane.copy() if z_method == "mip" else plane.astype(np.float64)
        elif z_method == "mip":
            np.maximum(acc, plane, out=acc)
        else:
            acc += plane
        count += 1
    if acc is None:
        raise ValueError("no planes to project")
    if z_method == "mean":
        acc /= count
    elif z_method not in ("mip", "sum"):
        raise ValueError(f"unknown z method {z_method!r}")
    return acc.astype(np.float32)


# ---------------------------------------------------------------------------
# Server loop
# ---------------------------------------------------------------------------


class _StopError(Exception):
    """A cancel or shutdown arrived while a request was running."""


class _Host:
    def __init__(self, conn) -> None:
        self.conn = conn
        self.shutdown = False

    def check_stop(self) -> None:
        """Between planes and files: stop when the parent sent anything."""
        if self.conn.poll():
            op = self.conn.recv()[0]
            if op == "shutdown":
                self.shutdown = True
            raise _StopError

    def probe(self, req_id, paths) -> None:
        for path in paths:
            self.check_stop()
            self.conn.send(("file", req_id, probe_file(path)))
        self.conn.send(("done", req_id, None))

    def read(self, req_id, request: dict) -> None:
        z_method = request["z_method"]
        if z_method not in ("mip", "mean", "sum"):
            raise ValueError(f"unknown z method {z_method!r}")
        reader, _ = _open(request["path"])
        try:
            reader.setSeries(int(request["series"]))
            planes = _PlaneReader(reader)
            size_z, size_t = int(reader.getSizeZ()), int(reader.getSizeT())
            swap = bool(request.get("swap_zt"))
            n_z, n_t = (size_t, size_z) if swap else (size_z, size_t)
            channels = request.get("channels") or range(int(reader.getSizeC()))

            def stack(t: int, c: int):
                for z in range(n_z):
                    self.check_stop()
                    yield planes.read(t, c, z) if swap else planes.read(z, c, t)

            for t in range(n_t):
                for c in channels:
                    plane = project_planes(stack(t, c), z_method)
                    self.conn.send(("plane", req_id, (t, int(c), plane)))
        finally:
            reader.close()
        self.conn.send(("done", req_id, None))

    def plane(self, req_id, request: dict) -> None:
        reader, _ = _open(request["path"])
        try:
            reader.setSeries(int(request["series"]))
            plane = _PlaneReader(reader).read(request["z"], request["c"], request["t"])
        finally:
            reader.close()
        self.conn.send(("done", req_id, plane))

    def suffixes(self, req_id, _payload) -> None:
        from loci.formats import ImageReader

        self.conn.send(("done", req_id, sorted({str(s) for s in ImageReader().getSuffixes()})))

    def run(self) -> None:
        handlers = {
            "probe": self.probe,
            "read": self.read,
            "plane": self.plane,
            "suffixes": self.suffixes,
        }
        while not self.shutdown:
            try:
                op, req_id, payload = self.conn.recv()
            except EOFError:
                return
            if op == "shutdown":
                return
            if op == "cancel":
                continue  # arrived after its request finished
            handler = handlers.get(op)
            if handler is None:
                self.conn.send(("error", req_id, (f"unknown request {op!r}", "")))
                continue
            try:
                handler(req_id, payload)
            except _StopError:
                if not self.shutdown:
                    self.conn.send(("cancelled", req_id, None))
            except Exception as exc:  # noqa: BLE001 - report, keep serving
                self.conn.send(("error", req_id, (_one_line(exc), _trace(exc))))


def serve(conn, config: dict) -> None:
    """Child entry point: start the JVM, then answer requests until shutdown.

    ``config`` holds ``java_home``, ``jar`` and ``max_heap_mb`` (or None).
    """
    try:
        info = _start_jvm(config)
    except BaseException as exc:  # noqa: BLE001 - the parent must hear why
        try:
            conn.send(("fatal", _one_line(exc), _trace(exc)))
        finally:
            conn.close()
        return
    conn.send(("ready", info))
    try:
        _Host(conn).run()
    finally:
        conn.close()

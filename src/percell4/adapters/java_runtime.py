"""Find or provision a working Java and the pinned Bio-Formats jar.

In-file import reads microscopy files through OME Bio-Formats, which is Java.
Most machines PerCell runs on have no working Java -- macOS ships a
``/usr/bin/java`` stub that exists but fails -- so this module:

1. **Resolves** Java in a fixed order (plan KTD3): the ``java_home`` advanced
   setting, then ``JAVA_HOME``, then the PerCell cache. Each candidate is
   *executed* (``java -version``) rather than trusted for existing, the same
   probe-by-execution rule as ``torch_device._probe_device``.
2. **Resolves** the Bio-Formats jar: the ``bioformats_jar`` setting, then the
   cache.
3. **Provisions** what is missing, only with consent: the jar by direct HTTPS
   download, the JRE through ``cjdk`` in a child process that cancel
   terminates. Both are checked against pinned SHA-256 values and land
   atomically; a mismatch deletes the download.
4. **Describes** the environment for the GUI and CLI in a record that always
   says why, and :func:`describe_java_environment` never raises.

Neither the JRE nor the GPL jar is ever shipped in a PerCell artifact; they are
fetched to the user's machine (plan KTD3, KTD4).

Qt-free and JPype-free on purpose: the GUI, the headless CLI and the reader
child process all import this module.
"""

from __future__ import annotations

import hashlib
import json
import logging
import multiprocessing
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from percell4.config.advanced import (
    AdvancedSettings,
    load_advanced_settings,
    update_advanced_settings,
)
from percell4.domain.errors import (
    BioformatsUnavailableError,
    JavaUnavailableError,
    ProvisioningCancelledError,
)

logger = logging.getLogger(__name__)

# --- pins ----------------------------------------------------------------------

#: Bio-Formats release. KTD5 requires >= 8.3.1 (CVE-2026-22187, unsafe
#: ``.bfmemo`` deserialization); 8.5.0 is the plan's target.
BIOFORMATS_VERSION = "8.5.0"

#: Source: OME downloads site, release 8.5.0 artifacts directory
#: (https://downloads.openmicroscopy.org/bio-formats/8.5.0/artifacts/).
BIOFORMATS_JAR_URL = (
    "https://downloads.openmicroscopy.org/bio-formats/8.5.0/artifacts/"
    "bioformats_package.jar"
)

#: Published by OME in
#: https://downloads.openmicroscopy.org/bio-formats/8.5.0/artifacts/SHASUMS
#: (line ``./bioformats_package.jar``), and cross-checked on 2026-09-18 by
#: downloading the jar and hashing it locally -- the two agree.
BIOFORMATS_JAR_SHA256 = (
    "c6e60665d53a334b66e4d635340151f403dfe57a64704c573dd4c03b873befb9"
)

#: Content-Length served for the jar above; shown in the consent dialog.
BIOFORMATS_JAR_SIZE = 53_843_906

#: Bio-Formats 8 requires Java 11 or newer.
MIN_JAVA_MAJOR = 11

#: cjdk vendor name (coursier JVM index) and the exact release it resolves.
JRE_VENDOR = "temurin-jre"
JRE_VERSION = "21.0.12.1+1"


@dataclass(frozen=True, slots=True)
class JreArtifact:
    """One pinned JRE archive, in the ``tgz+https`` / ``zip+https`` URL form
    that ``cjdk.cache_package`` takes."""

    url: str
    sha256: str
    size_bytes: int


_TEMURIN_BASE = (
    "https://github.com/adoptium/temurin21-binaries/releases/download/"
    "jdk-21.0.12.1%2B1/"
)

#: Keyed by ``(os, arch)`` as :func:`platform_key` spells them.
#:
#: URLs: exactly what ``cjdk`` 0.5.0 resolves for ``temurin-jre:21`` from the
#: coursier JVM index on 2026-09-18 (index version ``1.21.0.12.1``).
#: SHA-256 and sizes: published by Adoptium, taken from the Adoptium API
#: (https://api.adoptium.net/v3/assets/release_name/eclipse/jdk-21.0.12.1%2B1?image_type=jre,
#: ``package.checksum`` / ``package.size``) and cross-checked against the
#: ``<asset>.sha256.txt`` files published beside each GitHub release asset.
#: The two sources agree; no archive was downloaded to compute them.
JRE_ARTIFACTS: dict[tuple[str, str], JreArtifact] = {
    ("darwin", "arm64"): JreArtifact(
        url="tgz+" + _TEMURIN_BASE + "OpenJDK21U-jre_aarch64_mac_hotspot_21.0.12.1_1.tar.gz",
        sha256="dec50fc6f9fcd4fe3ae8cabf5a5fa68f6afc48841f7698e468e9aa5d54beed84",
        size_bytes=48_144_965,
    ),
    ("darwin", "x86_64"): JreArtifact(
        url="tgz+" + _TEMURIN_BASE + "OpenJDK21U-jre_x64_mac_hotspot_21.0.12.1_1.tar.gz",
        sha256="6717ec641fd9ce0bb209ca083ee23b42202ac68cb6fcc5753496e0e4a0f41989",
        size_bytes=42_131_876,
    ),
    ("windows", "x86_64"): JreArtifact(
        url="zip+" + _TEMURIN_BASE + "OpenJDK21U-jre_x64_windows_hotspot_21.0.12.1_1.zip",
        sha256="d35f31e712f0fcf6ac5a093edc90204fbff22f720ba3950bd09d331d5e621636",
        size_bytes=48_999_141,
    ),
    ("linux", "x86_64"): JreArtifact(
        url="tgz+" + _TEMURIN_BASE + "OpenJDK21U-jre_x64_linux_hotspot_21.0.12.1_1.tar.gz",
        sha256="2413149700df0f7d440500a84a8f764c535f21e5a5e87d38328b64eec2c5b500",
        size_bytes=52_059_408,
    ),
}

#: What the user agrees to download. Stored verbatim as the consent record, so
#: bumping either pin makes the stored consent stale and PerCell asks again.
CONSENT_TOKEN = (
    f"bioformats_package.jar {BIOFORMATS_VERSION}; {JRE_VENDOR} {JRE_VERSION}"
)

#: ``java -version`` on a healthy JVM answers in well under a second; a cold
#: start on a slow disk can take several. Past this, the candidate is broken.
_PROBE_TIMEOUT_S = 30.0

_CHUNK = 1 << 16

#: ``progress(message, bytes_done, bytes_total_or_None)``.
ProgressCallback = Callable[[str, int, "int | None"], None]
#: Returns True once the user has asked to stop.
CancelCheck = Callable[[], bool]
#: ``downloader(url, dest, progress, cancel)`` writes the body of ``url`` to ``dest``.
Downloader = Callable[[str, Path, ProgressCallback, CancelCheck], None]
#: ``runner(target, args, cancel)`` runs ``target(*args)`` and returns its result.
Runner = Callable[[Callable[..., Any], tuple, CancelCheck], Any]


# --- records -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JavaResolution:
    """Which Java will run Bio-Formats, or why none can.

    ``reason`` is display-ready; callers show it verbatim.
    """

    #: The ``java`` executable, or None when nothing worked.
    java: Path | None
    java_home: Path | None
    #: ``"settings"``, ``"JAVA_HOME"`` or ``"cache"``; None when unavailable.
    source: str | None
    #: The version string ``java -version`` printed (``"21.0.2"``), or ``""``.
    version: str
    reason: str
    #: One line per candidate tried, in resolution order.
    attempts: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return self.java is not None


@dataclass(frozen=True, slots=True)
class JarResolution:
    """Which ``bioformats_package.jar`` will be loaded, or why none can."""

    jar: Path | None
    #: ``"settings"`` or ``"cache"``; None when unavailable.
    source: str | None
    reason: str

    @property
    def ready(self) -> bool:
        return self.jar is not None


@dataclass(frozen=True, slots=True)
class PendingDownload:
    """Something provisioning would fetch -- what the consent dialog lists."""

    name: str
    url: str
    size_bytes: int
    destination: Path


@dataclass(frozen=True, slots=True)
class JavaEnvironment:
    """Everything the GUI and CLI need to say about Java, safe to render."""

    java: JavaResolution
    jar: JarResolution
    cache_dir: Path
    #: True when the stored consent matches the current pins.
    consent_current: bool
    pending_downloads: tuple[PendingDownload, ...]
    summary: str

    @property
    def ready(self) -> bool:
        return self.java.ready and self.jar.ready

    @property
    def download_bytes(self) -> int:
        return sum(item.size_bytes for item in self.pending_downloads)


# --- locations -----------------------------------------------------------------


def default_cache_dir() -> Path:
    """The per-user cache PerCell provisions Java and Bio-Formats into.

    A cache, not the config directory: everything here can be re-downloaded,
    and OS cleanup tools are allowed to reclaim it.
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / "PerCell4" / "Cache"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "PerCell4"
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "percell4"


def bioformats_jar_cache_path(cache_dir: Path) -> Path:
    """Where the pinned jar lives in the cache, keyed by its version."""
    return Path(cache_dir) / "bioformats" / BIOFORMATS_VERSION / "bioformats_package.jar"


def _jre_root(cache_dir: Path) -> Path:
    return Path(cache_dir) / "jre"


def jre_marker_path(cache_dir: Path) -> Path:
    """The record of the provisioned JRE. Written only after its probe passes,
    so its presence is what "the cache holds a working Java" means."""
    return _jre_root(cache_dir) / "runtime.json"


def platform_key(os_name: str | None = None, arch: str | None = None) -> tuple[str, str]:
    """Normalize the running platform to a :data:`JRE_ARTIFACTS` key."""
    if os_name is None:
        if sys.platform == "win32":
            os_name = "windows"
        elif sys.platform == "darwin":
            os_name = "darwin"
        elif sys.platform.startswith("linux"):
            os_name = "linux"
        else:
            os_name = sys.platform
    if arch is None:
        arch = platform.machine()
    arch = arch.lower()
    arch = {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}.get(arch, arch)
    return os_name.lower(), arch


def jre_artifact_for_platform(
    os_name: str | None = None, arch: str | None = None
) -> JreArtifact:
    """The pinned JRE archive for this platform, or a JavaUnavailableError."""
    key = platform_key(os_name, arch)
    try:
        return JRE_ARTIFACTS[key]
    except KeyError:
        raise JavaUnavailableError(
            f"PerCell has no pinned Java runtime for {key[0]} {key[1]}. "
            + _java_manual_steps()
        ) from None


# --- consent -------------------------------------------------------------------


def has_download_consent(settings: AdvancedSettings | None = None) -> bool:
    """True when the user agreed to download exactly the current pins."""
    if settings is None:
        settings = load_advanced_settings()
    return settings.java_download_consent == CONSENT_TOKEN


def record_download_consent() -> None:
    """Store consent for the current pins, keeping every other setting."""
    update_advanced_settings(java_download_consent=CONSENT_TOKEN)


# --- manual steps --------------------------------------------------------------


def _java_manual_steps() -> str:
    return (
        f"To set Java up by hand, install a Java {MIN_JAVA_MAJOR} or newer runtime "
        "(for example Eclipse Temurin 21), then set JAVA_HOME or enter its folder "
        "as java_home in PerCell's Advanced settings."
    )


def _jar_manual_steps(url: str, dest: Path) -> str:
    return (
        f"To set Bio-Formats up by hand, download {url} (Bio-Formats "
        f"{BIOFORMATS_VERSION}, GPL licensed) and place it at {dest}, or enter "
        "its path as bioformats_jar in PerCell's Advanced settings."
    )


# --- probing -------------------------------------------------------------------

_VERSION_RE = re.compile(r'version "([^"]+)"')


def _java_executable(home: Path) -> Path:
    name = "java.exe" if sys.platform == "win32" else "java"
    return Path(home) / "bin" / name


def _major_version(version: str) -> int | None:
    """``"1.8.0_392"`` -> 8, ``"21.0.2"`` -> 21, ``"17"`` -> 17."""
    parts = re.split(r"[.+_-]", version)
    try:
        first = int(parts[0])
        if first == 1 and len(parts) > 1:
            return int(parts[1])
        return first
    except (ValueError, IndexError):
        return None


def _probe_java(java: Path) -> tuple[str | None, str]:
    """Run ``java -version``. Return ``(failure_or_None, version)``.

    Execution is the test, not existence: the macOS ``/usr/bin/java`` stub
    exists, is executable, and exits 1 with "Unable to locate a Java Runtime".
    """
    try:
        result = subprocess.run(
            [str(java), "-version"],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError:
        return f"{java} does not exist", ""
    except subprocess.TimeoutExpired:
        return f"`java -version` did not answer within {_PROBE_TIMEOUT_S:.0f} s", ""
    except OSError as exc:
        return f"{java} could not be run ({exc})", ""

    output = (result.stderr or "") + (result.stdout or "")
    if result.returncode != 0:
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        detail = lines[-1] if lines else "no output"
        return f"`java -version` exited with status {result.returncode}: {detail}", ""

    match = _VERSION_RE.search(output)
    version = match.group(1) if match else ""
    major = _major_version(version) if version else None
    if major is not None and major < MIN_JAVA_MAJOR:
        return (
            f"Java {version} is too old; Bio-Formats {BIOFORMATS_VERSION} needs "
            f"Java {MIN_JAVA_MAJOR} or newer",
            version,
        )
    return None, version


def _cached_java_home(cache_dir: Path) -> Path | None:
    """The provisioned JRE's home from the marker, or None."""
    try:
        data = json.loads(jre_marker_path(cache_dir).read_text(encoding="utf-8"))
        return Path(data["java_home"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


# --- resolution ----------------------------------------------------------------

#: How each candidate is named in the attempt list.
_ATTEMPT_LABELS = {
    "settings": "Advanced settings java_home",
    "JAVA_HOME": "JAVA_HOME",
    "cache": "PerCell cache",
}

#: How the winning candidate is named in the ready reason.
_SOURCE_LABELS = {
    "settings": "the java_home Advanced settings entry",
    "JAVA_HOME": "JAVA_HOME",
    "cache": "the PerCell cache",
}


def resolve_java(
    *,
    settings: AdvancedSettings | None = None,
    environ: dict[str, str] | None = None,
    cache_dir: Path | None = None,
) -> JavaResolution:
    """Find a working Java: settings, then ``JAVA_HOME``, then the cache."""
    if settings is None:
        settings = load_advanced_settings()
    env = os.environ if environ is None else environ
    cache = Path(cache_dir) if cache_dir is not None else default_cache_dir()

    candidates: list[tuple[str, Path | None, str]] = [
        ("settings", Path(settings.java_home) if settings.java_home else None,
         "Advanced settings java_home: not set"),
        ("JAVA_HOME", Path(env["JAVA_HOME"]) if env.get("JAVA_HOME") else None,
         "JAVA_HOME: not set"),
        ("cache", _cached_java_home(cache),
         f"PerCell cache: no Java runtime has been set up in {_jre_root(cache)}"),
    ]

    attempts: list[str] = []
    for source, home, unset_note in candidates:
        if home is None:
            attempts.append(unset_note)
            continue
        label = _ATTEMPT_LABELS[source]
        java = _java_executable(home)
        if not java.is_file():
            attempts.append(f"{label} ({home}): no bin/java inside")
            continue
        failure, version = _probe_java(java)
        if failure is not None:
            attempts.append(f"{label} ({home}): {failure}")
            continue
        shown = f"Java {version}" if version else "Java (version not reported)"
        return JavaResolution(
            java=java,
            java_home=home,
            source=source,
            version=version,
            reason=f"Using {shown} from {_SOURCE_LABELS[source]} ({home}).",
            attempts=tuple(attempts),
        )

    return JavaResolution(
        java=None,
        java_home=None,
        source=None,
        version="",
        reason="No working Java runtime was found. Tried: " + "; ".join(attempts) + ".",
        attempts=tuple(attempts),
    )


def resolve_bioformats_jar(
    *,
    settings: AdvancedSettings | None = None,
    cache_dir: Path | None = None,
) -> JarResolution:
    """Find the Bio-Formats jar: the ``bioformats_jar`` setting, then the cache.

    The cached jar is trusted without re-hashing: it only ever lands there
    after its SHA-256 matched, by atomic rename.
    """
    if settings is None:
        settings = load_advanced_settings()
    cache = Path(cache_dir) if cache_dir is not None else default_cache_dir()

    notes: list[str] = []
    if settings.bioformats_jar:
        configured = Path(settings.bioformats_jar)
        if configured.is_file():
            return JarResolution(
                jar=configured,
                source="settings",
                reason=f"Using Bio-Formats from Advanced settings ({configured}).",
            )
        notes.append(f"the bioformats_jar setting points at {configured}, which does not exist")

    cached = bioformats_jar_cache_path(cache)
    if cached.is_file():
        return JarResolution(
            jar=cached,
            source="cache",
            reason=f"Using Bio-Formats {BIOFORMATS_VERSION} from the PerCell cache ({cached}).",
        )
    notes.append(
        f"Bio-Formats {BIOFORMATS_VERSION} (bioformats_package.jar) is not in the "
        f"PerCell cache ({cached})"
    )
    reason = "; ".join(notes)
    return JarResolution(jar=None, source=None, reason=reason[0].upper() + reason[1:] + ".")


# --- describe ------------------------------------------------------------------


def _pending_downloads(
    java: JavaResolution, jar: JarResolution, cache: Path
) -> tuple[PendingDownload, ...]:
    pending: list[PendingDownload] = []
    if not jar.ready:
        pending.append(
            PendingDownload(
                name=f"Bio-Formats {BIOFORMATS_VERSION} (bioformats_package.jar, GPL)",
                url=BIOFORMATS_JAR_URL,
                size_bytes=BIOFORMATS_JAR_SIZE,
                destination=bioformats_jar_cache_path(cache),
            )
        )
    if not java.ready:
        artifact = JRE_ARTIFACTS.get(platform_key())
        if artifact is not None:
            pending.append(
                PendingDownload(
                    name=f"Java runtime ({JRE_VENDOR} {JRE_VERSION})",
                    url=artifact.url.split("+", 1)[1],
                    size_bytes=artifact.size_bytes,
                    destination=_jre_root(cache),
                )
            )
    return tuple(pending)


def describe_java_environment(*, cache_dir: Path | None = None) -> JavaEnvironment:
    """Report Java and Bio-Formats readiness. Never raises.

    Runs ``java -version`` for each candidate, so build it lazily -- when an
    in-file candidate needs it -- not at startup.
    """
    try:
        cache = Path(cache_dir) if cache_dir is not None else default_cache_dir()
    except Exception:  # noqa: BLE001 - e.g. no home directory; still render
        cache = Path(".")

    try:
        settings = load_advanced_settings()
        java = resolve_java(settings=settings, cache_dir=cache)
        jar = resolve_bioformats_jar(settings=settings, cache_dir=cache)
        consent = has_download_consent(settings)
        pending = _pending_downloads(java, jar, cache)
    except Exception as exc:  # noqa: BLE001 - the panel and CLI must render this
        message = f"The Java environment could not be checked: {exc}"
        return JavaEnvironment(
            java=JavaResolution(None, None, None, "", message),
            jar=JarResolution(None, None, message),
            cache_dir=cache,
            consent_current=False,
            pending_downloads=(),
            summary=message,
        )

    if java.ready and jar.ready:
        summary = f"Ready. {java.reason} {jar.reason}"
    else:
        missing = [r.reason for r in (java, jar) if not r.ready]
        summary = "In-file import needs setup. " + " ".join(missing)
        if pending:
            megabytes = sum(p.size_bytes for p in pending) / 1_000_000
            summary += f" PerCell can download what is missing ({megabytes:.0f} MB) into {cache}."
    return JavaEnvironment(
        java=java,
        jar=jar,
        cache_dir=cache,
        consent_current=consent,
        pending_downloads=pending,
        summary=summary,
    )


# --- downloads -----------------------------------------------------------------


def _require_https(url: str, error: type[Exception], what: str) -> None:
    """Refuse anything but HTTPS. ``tgz+https`` / ``zip+https`` are cjdk's
    spelling of an HTTPS archive URL."""
    scheme = urlparse(url).scheme.lower()
    if scheme.rsplit("+", 1)[-1] != "https":
        raise error(f"Refusing to download the {what} from {url}: only HTTPS URLs are allowed.")


def _no_progress(message: str, done: int, total: int | None) -> None:
    return None


def _never_cancel() -> bool:
    return False


def _urlopen(url: str, timeout: float):
    """Open ``url``. Split out so tests can serve bytes without a network."""
    return urllib.request.urlopen(url, timeout=timeout)  # noqa: S310 - scheme checked by caller


def https_download(
    url: str, dest: Path, progress: ProgressCallback, cancel: CancelCheck
) -> None:
    """The default downloader: stream ``url`` to ``dest``, checking cancel
    between chunks. Raises :class:`ProvisioningCancelledError` on cancel."""
    if urlparse(url).scheme.lower() != "https":
        raise ValueError(f"not an HTTPS URL: {url}")
    with _urlopen(url, timeout=60) as response:
        final_url = getattr(response, "geturl", lambda: url)()
        if urlparse(final_url).scheme.lower() != "https":
            raise OSError(f"{url} redirected to a non-HTTPS location: {final_url}")
        length = response.headers.get("Content-Length")
        total = int(length) if length and str(length).isdigit() else None
        done = 0
        with open(dest, "wb") as out:
            while True:
                if cancel():
                    raise ProvisioningCancelledError("Download cancelled.")
                chunk = response.read(_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                progress(f"Downloading {Path(urlparse(url).path).name}", done, total)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch_bioformats_jar(
    cache_dir: Path,
    *,
    url: str | None = None,
    sha256: str | None = None,
    downloader: Downloader | None = None,
    progress: ProgressCallback | None = None,
    cancel: CancelCheck | None = None,
) -> Path:
    """Download the pinned jar into the cache, verify it, and rename it in.

    The download goes to a temp file beside the destination; any failure,
    mismatch or cancel deletes it, so the cache never holds a partial or
    unverified jar.
    """
    url = url or BIOFORMATS_JAR_URL
    expected = (sha256 or BIOFORMATS_JAR_SHA256).lower()
    downloader = downloader or https_download
    progress = progress or _no_progress
    cancel = cancel or _never_cancel

    _require_https(url, BioformatsUnavailableError, "Bio-Formats jar")
    dest = bioformats_jar_cache_path(cache_dir)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".bioformats_package-", suffix=".part", dir=dest.parent
        )
        os.close(fd)
    except OSError as exc:
        raise BioformatsUnavailableError(
            f"Could not write to the PerCell cache ({dest.parent}): {exc}. "
            + _jar_manual_steps(url, dest)
        ) from exc
    tmp = Path(tmp_name)

    try:
        progress(f"Downloading Bio-Formats {BIOFORMATS_VERSION} from {url}", 0, BIOFORMATS_JAR_SIZE)
        try:
            downloader(url, tmp, progress, cancel)
        except ProvisioningCancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - offline, DNS, HTTP error, disk full
            raise BioformatsUnavailableError(
                f"Could not download Bio-Formats from {url}: {exc}. "
                + _jar_manual_steps(url, dest)
            ) from exc
        if cancel():
            raise ProvisioningCancelledError("Download cancelled.")

        actual = _sha256_file(tmp)
        if actual != expected:
            raise BioformatsUnavailableError(
                f"Bio-Formats jar checksum mismatch: {url} has SHA-256 {actual}, "
                f"expected {expected}. The download was deleted."
            )
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    return dest


# --- JRE fetch in a child process ------------------------------------------------


def _import_cjdk():
    """Import cjdk. Split out so a missing optional extra reads clearly."""
    try:
        import cjdk
    except ImportError as exc:
        raise JavaUnavailableError(
            "The Java downloader (cjdk) is not installed; install PerCell with "
            "the bioformats extra: pip install 'percell4[bioformats]'. "
            + _java_manual_steps()
        ) from exc
    return cjdk


def _fetch_jre_with_cjdk(url: str, sha256: str, staging: str) -> str:
    """Child-process target: download, verify and extract one JRE archive.

    ``cjdk.cache_package`` checks the archive's SHA-256 *before* extracting
    and deletes it on mismatch; the extracted tree lands atomically. Both the
    archive temp file and cjdk's cache live under ``staging``, which the
    parent deletes afterwards whatever happened -- including a terminate,
    where cjdk's own cleanup never runs. Returns the extracted directory.
    """
    cjdk = _import_cjdk()
    staging_dir = Path(staging)
    archive_tmp = staging_dir / "tmp"
    archive_tmp.mkdir(parents=True, exist_ok=True)
    previous = tempfile.tempdir
    tempfile.tempdir = str(archive_tmp)
    try:
        root = cjdk.cache_package(
            f"{JRE_VENDOR} {JRE_VERSION}",
            url,
            sha256=sha256,
            cache_dir=staging_dir / "cjdk",
            progress=False,
        )
    except cjdk.InstallError as exc:
        if "Hash does not match" in str(exc):
            raise JavaUnavailableError(
                f"Java runtime archive checksum mismatch: {url} did not match the "
                f"pinned SHA-256 {sha256}. The archive was deleted before extraction."
            ) from None
        raise
    finally:
        tempfile.tempdir = previous
    return str(root)


_RELAYED_ERRORS: dict[str, type[Exception]] = {
    cls.__name__: cls
    for cls in (JavaUnavailableError, BioformatsUnavailableError, ProvisioningCancelledError)
}


def _child_entry(conn, target: Callable[..., Any], args: tuple) -> None:
    """Run ``target`` in the child and send back ``("ok", result)`` or
    ``("error", (type_name, message))``. Exceptions are relayed by name so the
    parent never has to unpickle an arbitrary exception class."""
    try:
        result = target(*args)
        conn.send(("ok", result))
    except BaseException as exc:  # noqa: BLE001 - everything must reach the parent
        conn.send(("error", (type(exc).__name__, str(exc))))
    finally:
        conn.close()


def run_in_child_process(
    target: Callable[..., Any],
    args: tuple,
    cancel: CancelCheck,
    *,
    poll_interval: float = 0.1,
) -> Any:
    """Run ``target(*args)`` in a ``spawn`` child; cancel terminates it.

    ``target`` must be picklable (a module-level function). A typed PerCell
    error raised in the child is re-raised as the same type; anything else
    becomes a RuntimeError naming the original type.
    """
    ctx = multiprocessing.get_context("spawn")
    receiver, sender = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_child_entry, args=(sender, target, args), daemon=True)
    proc.start()
    sender.close()
    cancelled = False
    try:
        while True:
            if cancel():
                cancelled = True
                raise ProvisioningCancelledError("Download cancelled.")
            if receiver.poll(poll_interval):
                try:
                    status, payload = receiver.recv()
                except EOFError:
                    proc.join(5)
                    raise JavaUnavailableError(
                        f"The download process exited unexpectedly (exit code {proc.exitcode})."
                    ) from None
                break
            if not proc.is_alive() and not receiver.poll():
                raise JavaUnavailableError(
                    f"The download process exited unexpectedly (exit code {proc.exitcode})."
                )
    finally:
        if not cancelled:
            proc.join(5)
        if proc.is_alive():
            proc.terminate()
            proc.join(5)
            if proc.is_alive():
                proc.kill()
                proc.join(5)
        receiver.close()

    if status == "ok":
        return payload
    name, message = payload
    relayed = _RELAYED_ERRORS.get(name)
    if relayed is not None:
        raise relayed(message)
    raise RuntimeError(f"{name}: {message}")


def _find_java_home(path: Path, depth: int = 2) -> Path:
    """Locate the Java home inside an extracted archive (mirrors cjdk's rule:
    the path itself, ``Contents/Home`` on macOS, or a single subdirectory)."""
    for candidate in (path, path / "Contents" / "Home"):
        if _java_executable(candidate).is_file():
            return candidate
    if depth > 0:
        subdirs = [p for p in path.iterdir() if p.is_dir()]
        if len(subdirs) == 1:
            return _find_java_home(subdirs[0], depth - 1)
    raise JavaUnavailableError(f"{path} does not contain a Java runtime.")


def fetch_jre(
    cache_dir: Path,
    *,
    artifact: JreArtifact | None = None,
    fetcher: Callable[[str, str, str], str] | None = None,
    runner: Runner | None = None,
    progress: ProgressCallback | None = None,
    cancel: CancelCheck | None = None,
) -> Path:
    """Fetch the pinned JRE into the cache and return its verified home.

    ``fetcher(url, sha256, staging_dir) -> extracted_dir`` does the download
    and extraction (``cjdk`` by default) and ``runner`` runs it (a child
    process by default, so cancel can terminate it). The runtime counts as
    provisioned only once its ``java -version`` probe passes; only then is
    the cache marker written.
    """
    artifact = artifact or jre_artifact_for_platform()
    fetcher = fetcher or _fetch_jre_with_cjdk
    runner = runner or run_in_child_process
    progress = progress or _no_progress
    cancel = cancel or _never_cancel

    _require_https(artifact.url, JavaUnavailableError, "Java runtime")
    jre_root = _jre_root(cache_dir)
    try:
        jre_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=jre_root))
    except OSError as exc:
        raise JavaUnavailableError(
            f"Could not write to the PerCell cache ({jre_root}): {exc}. "
            + _java_manual_steps()
        ) from exc

    public_url = artifact.url.split("+", 1)[1]
    final = jre_root / f"{JRE_VENDOR}-{JRE_VERSION}"
    try:
        progress(
            f"Downloading Java runtime ({JRE_VENDOR} {JRE_VERSION}) from {public_url}",
            0,
            artifact.size_bytes,
        )
        try:
            extracted = Path(runner(fetcher, (artifact.url, artifact.sha256, str(staging)), cancel))
        except (ProvisioningCancelledError, JavaUnavailableError):
            raise
        except Exception as exc:  # noqa: BLE001 - offline, HTTP error, bad archive
            raise JavaUnavailableError(
                f"Could not download the Java runtime from {public_url}: {exc}. "
                + _java_manual_steps()
            ) from exc
        if cancel():
            raise ProvisioningCancelledError("Download cancelled.")

        home_in_extracted = _find_java_home(extracted)
        if final.exists():
            shutil.rmtree(final)
        os.replace(extracted, final)
        home = final / home_in_extracted.relative_to(extracted)

        failure, version = _probe_java(_java_executable(home))
        if failure is not None:
            shutil.rmtree(final, ignore_errors=True)
            raise JavaUnavailableError(
                f"The downloaded Java runtime does not start ({failure}). "
                + _java_manual_steps()
            )

        marker = jre_marker_path(cache_dir)
        tmp_marker = marker.with_suffix(".json.part")
        tmp_marker.write_text(
            json.dumps(
                {"vendor": JRE_VENDOR, "version": JRE_VERSION, "java_home": str(home),
                 "reported_version": version},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(tmp_marker, marker)
        progress(f"Java runtime ready ({home})", artifact.size_bytes, artifact.size_bytes)
        return home
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# --- provision -----------------------------------------------------------------


def provision(
    progress: ProgressCallback | None = None,
    cancel: CancelCheck | None = None,
    *,
    cache_dir: Path | None = None,
    consented: bool | None = None,
    downloader: Downloader | None = None,
    jre_fetcher: Callable[[str, str, str], str] | None = None,
    runner: Runner | None = None,
    jre_artifact: JreArtifact | None = None,
) -> JavaEnvironment:
    """Fetch whatever is missing -- only with consent -- and return the result.

    ``consented=None`` reads the stored consent (valid only for the current
    pins). A headless ``--provision-java`` passes ``consented=True``. Without
    consent this raises before any download starts.
    """
    cache = Path(cache_dir) if cache_dir is not None else default_cache_dir()
    if consented is None:
        consented = has_download_consent()
    if not consented:
        raise JavaUnavailableError(
            "PerCell needs your consent before it downloads Java and Bio-Formats. "
            + _java_manual_steps()
        )

    env = describe_java_environment(cache_dir=cache)
    if not env.jar.ready:
        fetch_bioformats_jar(cache, downloader=downloader, progress=progress, cancel=cancel)
    if not env.java.ready:
        fetch_jre(
            cache,
            artifact=jre_artifact,
            fetcher=jre_fetcher,
            runner=runner,
            progress=progress,
            cancel=cancel,
        )

    env = describe_java_environment(cache_dir=cache)
    if not env.java.ready:
        raise JavaUnavailableError(env.java.reason)
    if not env.jar.ready:
        raise BioformatsUnavailableError(env.jar.reason)
    return env

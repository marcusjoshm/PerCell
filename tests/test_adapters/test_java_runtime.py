"""Tests for Java runtime resolution and Bio-Formats provisioning.

No test here touches the network. Downloads go through injected downloaders,
the JRE fetch through an injected runner/fetcher, and the one test that drives
the real ``cjdk`` extraction path replaces its HTTP call with in-memory bytes.

Fake ``java`` executables are small POSIX shell scripts, so the probe tests
are skipped on Windows.
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import tarfile
import time
from pathlib import Path

import pytest

from percell4.adapters import java_runtime
from percell4.config import advanced
from percell4.domain.errors import (
    BioformatsUnavailableError,
    JavaUnavailableError,
    ProvisioningCancelledError,
)

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="fake java executables are POSIX shell scripts"
)

_STUB_SCRIPT = """#!/bin/sh
echo "The operation couldn't be completed. Unable to locate a Java Runtime." >&2
exit 1
"""


def _java_script(version: str) -> str:
    return f"""#!/bin/sh
echo 'openjdk version "{version}" 2024-01-16' >&2
echo 'OpenJDK Runtime Environment Temurin' >&2
exit 0
"""


def _make_java_home(root: Path, *, script: str | None = None) -> Path:
    """Create ``root/bin/java`` as an executable script and return ``root``."""
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    java = bin_dir / "java"
    java.write_text(script if script is not None else _java_script("21.0.2"))
    java.chmod(0o755)
    return root


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    path = tmp_path / "cache"
    path.mkdir()
    return path


@pytest.fixture(autouse=True)
def _no_java_home(monkeypatch):
    """The developer's own JAVA_HOME must not leak into resolution tests."""
    monkeypatch.delenv("JAVA_HOME", raising=False)


@pytest.fixture(autouse=True)
def _default_cache_is_sandboxed(monkeypatch, tmp_path_factory):
    """Nothing may resolve against, or provision into, the real user cache."""
    sandbox = tmp_path_factory.mktemp("percell_cache")
    monkeypatch.setattr(java_runtime, "default_cache_dir", lambda: sandbox)


def _inline_runner(target, args, cancel):
    """Run the JRE fetch in-process so injected fakes and patches apply."""
    return target(*args)


def _never_cancel() -> bool:
    return False


# --- resolution ---------------------------------------------------------------


@posix_only
def test_stub_java_that_exits_nonzero_is_unavailable_with_reason(tmp_path, monkeypatch):
    home = _make_java_home(tmp_path / "stub", script=_STUB_SCRIPT)
    monkeypatch.setenv("JAVA_HOME", str(home))

    resolution = java_runtime.resolve_java()

    assert not resolution.ready
    assert resolution.java is None
    assert "JAVA_HOME" in resolution.reason
    assert "Unable to locate a Java Runtime" in resolution.reason


@posix_only
def test_fake_java_under_java_home_resolves_ready(tmp_path, monkeypatch):
    home = _make_java_home(tmp_path / "jre")
    monkeypatch.setenv("JAVA_HOME", str(home))

    resolution = java_runtime.resolve_java()

    assert resolution.ready
    assert resolution.source == "JAVA_HOME"
    assert resolution.java_home == home
    assert resolution.version == "21.0.2"


@posix_only
def test_java_home_setting_wins_over_environment(tmp_path, monkeypatch):
    env_home = _make_java_home(tmp_path / "env", script=_java_script("17.0.9"))
    set_home = _make_java_home(tmp_path / "set", script=_java_script("21.0.2"))
    monkeypatch.setenv("JAVA_HOME", str(env_home))
    advanced.update_advanced_settings(java_home=str(set_home))

    resolution = java_runtime.resolve_java()

    assert resolution.ready
    assert resolution.source == "settings"
    assert resolution.java_home == set_home
    assert resolution.version == "21.0.2"


@posix_only
def test_broken_setting_falls_through_and_is_reported(tmp_path, monkeypatch):
    env_home = _make_java_home(tmp_path / "env")
    monkeypatch.setenv("JAVA_HOME", str(env_home))
    advanced.update_advanced_settings(java_home=str(tmp_path / "missing"))

    resolution = java_runtime.resolve_java()

    assert resolution.ready
    assert resolution.source == "JAVA_HOME"
    assert any("settings" in attempt for attempt in resolution.attempts)


@posix_only
def test_java_older_than_minimum_is_unavailable(tmp_path, monkeypatch):
    home = _make_java_home(tmp_path / "old", script=_java_script("1.8.0_392"))
    monkeypatch.setenv("JAVA_HOME", str(home))

    resolution = java_runtime.resolve_java()

    assert not resolution.ready
    assert "1.8" in resolution.reason


def test_nothing_configured_is_unavailable_with_reason(cache_dir):
    resolution = java_runtime.resolve_java(cache_dir=cache_dir)
    assert not resolution.ready
    assert resolution.reason


def test_jar_setting_wins_over_cache(tmp_path, cache_dir):
    jar = tmp_path / "custom.jar"
    jar.write_bytes(b"jar")
    advanced.update_advanced_settings(bioformats_jar=str(jar))

    resolution = java_runtime.resolve_bioformats_jar(cache_dir=cache_dir)

    assert resolution.ready
    assert resolution.source == "settings"
    assert resolution.jar == jar


def test_missing_jar_is_unavailable_with_reason(cache_dir):
    resolution = java_runtime.resolve_bioformats_jar(cache_dir=cache_dir)
    assert not resolution.ready
    assert java_runtime.BIOFORMATS_VERSION in resolution.reason


# --- Bio-Formats jar download -------------------------------------------------


def _writing_downloader(payload: bytes):
    calls: list[str] = []

    def download(url, dest, progress, cancel):
        calls.append(url)
        Path(dest).write_bytes(payload)

    download.calls = calls
    return download


def test_jar_download_verifies_and_lands_atomically(cache_dir):
    payload = b"a real jar"
    jar = java_runtime.fetch_bioformats_jar(
        cache_dir,
        url="https://example.invalid/bioformats_package.jar",
        sha256=hashlib.sha256(payload).hexdigest(),
        downloader=_writing_downloader(payload),
    )
    assert jar.read_bytes() == payload
    assert jar == java_runtime.bioformats_jar_cache_path(cache_dir)
    assert [p for p in jar.parent.iterdir() if p != jar] == []


def test_jar_checksum_mismatch_is_deleted_and_named(cache_dir):
    with pytest.raises(BioformatsUnavailableError, match="checksum mismatch"):
        java_runtime.fetch_bioformats_jar(
            cache_dir,
            url="https://example.invalid/bioformats_package.jar",
            sha256=hashlib.sha256(b"expected").hexdigest(),
            downloader=_writing_downloader(b"tampered"),
        )
    assert not java_runtime.bioformats_jar_cache_path(cache_dir).exists()
    assert [p for p in cache_dir.rglob("*") if p.is_file()] == []


def test_http_jar_url_is_refused_without_downloading(cache_dir):
    downloader = _writing_downloader(b"x")
    with pytest.raises(BioformatsUnavailableError, match="HTTPS"):
        java_runtime.fetch_bioformats_jar(
            cache_dir,
            url="http://example.invalid/bioformats_package.jar",
            sha256="0" * 64,
            downloader=downloader,
        )
    assert downloader.calls == []


def test_cancel_during_jar_download_leaves_nothing(cache_dir):
    def download(url, dest, progress, cancel):
        Path(dest).write_bytes(b"partial")
        raise ProvisioningCancelledError("cancelled")

    with pytest.raises(ProvisioningCancelledError):
        java_runtime.fetch_bioformats_jar(
            cache_dir,
            url="https://example.invalid/bioformats_package.jar",
            sha256="0" * 64,
            downloader=download,
        )
    assert [p for p in cache_dir.rglob("*") if p.is_file()] == []


def test_default_downloader_honours_cancel_and_cleans_up(cache_dir, monkeypatch):
    """The real HTTPS downloader checks cancel between chunks."""

    class _Response(io.BytesIO):
        headers = {"Content-Length": str(1 << 20)}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()

    monkeypatch.setattr(
        java_runtime, "_urlopen", lambda url, timeout: _Response(b"x" * (1 << 20))
    )
    checks = iter([False, True])

    with pytest.raises(ProvisioningCancelledError):
        java_runtime.fetch_bioformats_jar(
            cache_dir,
            url="https://example.invalid/bioformats_package.jar",
            sha256="0" * 64,
            cancel=lambda: next(checks, True),
        )
    assert [p for p in cache_dir.rglob("*") if p.is_file()] == []


def test_offline_jar_download_names_manual_steps(cache_dir):
    def offline(url, dest, progress, cancel):
        raise OSError("Network is unreachable")

    with pytest.raises(BioformatsUnavailableError) as info:
        java_runtime.fetch_bioformats_jar(
            cache_dir,
            url="https://example.invalid/bioformats_package.jar",
            sha256="0" * 64,
            downloader=offline,
        )
    message = str(info.value)
    assert "Network is unreachable" in message
    assert "bioformats_jar" in message
    assert "https://example.invalid/bioformats_package.jar" in message


# --- JRE fetch ------------------------------------------------------------------


def _jre_tgz() -> bytes:
    """A minimal JRE-shaped archive: ``jdk-21.0.2-jre/bin/java``."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        script = _java_script("21.0.2").encode()
        info = tarfile.TarInfo("jdk-21.0.2-jre/bin/java")
        info.size = len(script)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(script))
    return buf.getvalue()


@pytest.fixture
def fake_cjdk_http(monkeypatch):
    """Replace cjdk's one HTTP call with in-memory bytes."""
    cjdk_download = pytest.importorskip("cjdk._download")
    served: dict[str, bytes] = {}

    class _Response:
        def __init__(self, payload: bytes):
            self._payload = payload
            self.headers = {"content-length": str(len(payload))}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size):
            for i in range(0, len(self._payload), chunk_size):
                yield self._payload[i : i + chunk_size]

    def fake_get(url, stream=True):
        return _Response(served["payload"])

    monkeypatch.setattr(cjdk_download.requests, "get", fake_get)
    return served


def test_jre_archive_checksum_mismatch_is_deleted_before_extraction(
    cache_dir, fake_cjdk_http
):
    fake_cjdk_http["payload"] = _jre_tgz()
    artifact = java_runtime.JreArtifact(
        url="tgz+https://example.invalid/jre.tar.gz",
        sha256=hashlib.sha256(b"something else").hexdigest(),
        size_bytes=1,
    )

    with pytest.raises(JavaUnavailableError, match="checksum mismatch"):
        java_runtime.fetch_jre(cache_dir, artifact=artifact, runner=_inline_runner)

    # Nothing was extracted and no archive survives anywhere in the cache.
    assert [p for p in cache_dir.rglob("*") if p.is_file()] == []
    assert not list(cache_dir.rglob("java"))
    assert not java_runtime.resolve_java(cache_dir=cache_dir).ready


@posix_only
def test_jre_fetch_through_cjdk_installs_a_ready_runtime(cache_dir, fake_cjdk_http):
    payload = _jre_tgz()
    fake_cjdk_http["payload"] = payload
    artifact = java_runtime.JreArtifact(
        url="tgz+https://example.invalid/jre.tar.gz",
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )

    home = java_runtime.fetch_jre(cache_dir, artifact=artifact, runner=_inline_runner)

    resolution = java_runtime.resolve_java(cache_dir=cache_dir)
    assert resolution.ready
    assert resolution.source == "cache"
    assert resolution.java_home == home
    # The staging area is gone once the runtime is in place.
    assert not list(cache_dir.rglob("*staging*"))


def test_http_jre_url_is_refused(cache_dir):
    def must_not_run(*args):
        raise AssertionError("fetcher ran for a non-HTTPS URL")

    artifact = java_runtime.JreArtifact(
        url="tgz+http://example.invalid/jre.tar.gz", sha256="0" * 64, size_bytes=1
    )
    with pytest.raises(JavaUnavailableError, match="HTTPS"):
        java_runtime.fetch_jre(
            cache_dir, artifact=artifact, fetcher=must_not_run, runner=_inline_runner
        )


def test_cancelled_jre_fetch_leaves_no_ready_runtime(cache_dir):
    def partial_then_cancel(url, sha256, staging):
        # Mid-extraction state: a java home that looks complete.
        _make_java_home(Path(staging) / "cjdk" / "partial")
        raise ProvisioningCancelledError("cancelled")

    artifact = java_runtime.JreArtifact(
        url="tgz+https://example.invalid/jre.tar.gz", sha256="0" * 64, size_bytes=1
    )
    with pytest.raises(ProvisioningCancelledError):
        java_runtime.fetch_jre(
            cache_dir,
            artifact=artifact,
            fetcher=partial_then_cancel,
            runner=_inline_runner,
        )

    assert not java_runtime.resolve_java(cache_dir=cache_dir).ready
    assert not list(cache_dir.rglob("java"))


def test_offline_jre_fetch_names_manual_steps(cache_dir):
    def offline(url, sha256, staging):
        raise OSError("Name or service not known")

    artifact = java_runtime.JreArtifact(
        url="tgz+https://example.invalid/jre.tar.gz", sha256="0" * 64, size_bytes=1
    )
    with pytest.raises(JavaUnavailableError) as info:
        java_runtime.fetch_jre(
            cache_dir, artifact=artifact, fetcher=offline, runner=_inline_runner
        )
    message = str(info.value)
    assert "Name or service not known" in message
    assert "java_home" in message
    assert "JAVA_HOME" in message


def test_child_process_runner_is_terminated_by_cancel():
    """The real runner: cancel kills the child instead of waiting it out."""
    started = time.monotonic()
    with pytest.raises(ProvisioningCancelledError):
        java_runtime.run_in_child_process(time.sleep, (60,), cancel=lambda: True)
    assert time.monotonic() - started < 30


def test_child_process_runner_returns_result_and_relays_errors():
    assert java_runtime.run_in_child_process(abs, (-4,), cancel=_never_cancel) == 4
    with pytest.raises(RuntimeError, match="ValueError"):
        java_runtime.run_in_child_process(int, ("not a number",), cancel=_never_cancel)


# --- provision ------------------------------------------------------------------


def _fake_jre_fetcher(url, sha256, staging):
    root = Path(staging) / "cjdk" / "v0" / "pkg" / "jdk-21.0.2-jre"
    _make_java_home(root)
    return str(root)


def test_provision_refuses_without_consent(cache_dir):
    downloader = _writing_downloader(b"x")
    with pytest.raises(JavaUnavailableError, match="consent"):
        java_runtime.provision(cache_dir=cache_dir, downloader=downloader)
    assert downloader.calls == []


@posix_only
def test_provision_with_consent_makes_the_environment_ready(cache_dir, monkeypatch):
    payload = b"jar bytes"
    monkeypatch.setattr(
        java_runtime, "BIOFORMATS_JAR_SHA256", hashlib.sha256(payload).hexdigest()
    )
    java_runtime.record_download_consent()
    messages: list[str] = []

    env = java_runtime.provision(
        progress=lambda message, done, total: messages.append(message),
        cache_dir=cache_dir,
        downloader=_writing_downloader(payload),
        jre_fetcher=_fake_jre_fetcher,
        runner=_inline_runner,
    )

    assert env.ready
    assert env.java.source == "cache"
    assert env.jar.source == "cache"
    assert messages
    marker = json.loads(java_runtime.jre_marker_path(cache_dir).read_text())
    assert marker["version"] == java_runtime.JRE_VERSION


# --- describe --------------------------------------------------------------------


def test_describe_reports_pending_downloads_with_source_and_size(cache_dir):
    env = java_runtime.describe_java_environment(cache_dir=cache_dir)
    assert not env.ready
    assert env.summary
    assert env.cache_dir == cache_dir
    urls = [item.url for item in env.pending_downloads]
    assert java_runtime.BIOFORMATS_JAR_URL in urls
    assert env.download_bytes >= java_runtime.BIOFORMATS_JAR_SIZE


def test_describe_survives_corrupt_settings_json(cache_dir):
    advanced.config_path().parent.mkdir(parents=True, exist_ok=True)
    advanced.config_path().write_text("{not: json", encoding="utf-8")
    env = java_runtime.describe_java_environment(cache_dir=cache_dir)
    assert isinstance(env, java_runtime.JavaEnvironment)
    assert not env.consent_current


def test_describe_never_raises(monkeypatch, cache_dir):
    def boom(**kwargs):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(java_runtime, "resolve_java", boom)
    env = java_runtime.describe_java_environment(cache_dir=cache_dir)
    assert not env.ready
    assert "resolver exploded" in env.summary


# --- pins -------------------------------------------------------------------------


def test_pins_are_https_and_complete():
    assert java_runtime.BIOFORMATS_JAR_URL.startswith("https://")
    assert len(java_runtime.BIOFORMATS_JAR_SHA256) == 64
    assert tuple(int(p) for p in java_runtime.BIOFORMATS_VERSION.split(".")) >= (8, 3, 1)
    expected = {
        ("darwin", "arm64"),
        ("darwin", "x86_64"),
        ("windows", "x86_64"),
        ("linux", "x86_64"),
    }
    assert set(java_runtime.JRE_ARTIFACTS) == expected
    for artifact in java_runtime.JRE_ARTIFACTS.values():
        assert artifact.url.split("://", 1)[0] in {"tgz+https", "zip+https"}
        assert len(artifact.sha256) == 64
        assert artifact.size_bytes > 0


def test_unsupported_platform_has_no_pinned_jre():
    with pytest.raises(JavaUnavailableError, match="java_home"):
        java_runtime.jre_artifact_for_platform("sunos", "sparc")

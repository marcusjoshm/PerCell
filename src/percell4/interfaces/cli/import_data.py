"""CLI: import multi-dimensional microscopy files through Bio-Formats.

Headless front-end for the in-file import. It scans files or directories
the same way the import dialog does, prints the suggested scheme, and
imports each source into its own ``.h5``. The scheme can be written to a
JSON file, edited, and replayed with ``--scheme``.

Usage:
    percell-import /data/stacks/ --scan-only --scheme-out scheme.json
    percell-import --scheme scheme.json --output-dir /data/h5/
    percell-import stack_01.tif stack_02.tif --output-dir /data/h5/ --z-method mean

Single-plane files are listed as excluded; import them with the Flat or
Subdirectory mode of the import dialog. A source whose axes are ambiguous
(``needs_confirmation`` in the scheme) blocks the import until the scheme
file confirms it: set its ``needs_confirmation`` to ``""`` or fix its
``axis_map``.

Java and Bio-Formats are never downloaded unless ``--provision-java`` is
given.

Exit codes:
    0 -- the scan or import finished
    1 -- Java unavailable, an import failed, or an output exists without --overwrite
    2 -- usage error, nothing importable, or a source needs confirmation

Programmatic use:
    from percell4.interfaces.cli.import_data import main
    exit_code = main(["/data/stacks/", "--scan-only"])
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from percell4.domain.errors import (
    BioformatsUnavailableError,
    ImportSchemeError,
    JavaUnavailableError,
    PercellError,
)
from percell4.domain.io import scheme_json
from percell4.domain.io.infile import Z_METHODS, ImportScheme, ImportSource
from percell4.domain.io.models import DiscoveryMode


def _make_reader():
    """The real Bio-Formats reader. Seam for tests."""
    from percell4.adapters.bioformats_reader import BioformatsReader

    return BioformatsReader()


def _provision(**kwargs) -> None:
    """Download Java and Bio-Formats. Seam for tests."""
    from percell4.adapters.java_runtime import provision

    provision(consented=True, **kwargs)


def _err(message: str) -> None:
    print(f"percell-import: {message}", file=sys.stderr)


def _describe(src: ImportSource) -> str:
    s = src.effective
    if s is None:
        return ""
    return f"T{s.size_t} C{len(src.channel_indices)} Z{s.size_z} {s.size_y}x{s.size_x}"


def _print_scheme(scheme: ImportScheme) -> None:
    print(f"Z projection: {scheme.z_method}")
    print(f"Sources ({len(scheme.sources)}):")
    for src in scheme.sources:
        mark = "+" if src.importable else "?"
        note = f"  [{src.needs_confirmation}]" if src.needs_confirmation else ""
        print(f"  {mark} {src.output_name}.h5  <- {src.path} (series {src.series_index}) "
              f"{_describe(src)}{note}")
    if scheme.excluded:
        print(f"Excluded ({len(scheme.excluded)}):")
        for entry in scheme.excluded:
            print(f"  - {entry.path}: {entry.reason}")
    for warning in scheme.warnings:
        print(f"Warning: {warning}")


def _scan(sources: list[str], z_method: str) -> ImportScheme | None:
    from percell4.adapters.infile_scan import scan_selection

    outcome = scan_selection(
        sources, reader_factory=_make_reader, mode=DiscoveryMode.INFILE, z_method=z_method
    )
    if outcome.scheme is not None:
        return outcome.scheme
    return ImportScheme(z_method=z_method, excluded=outcome.stage_one.excluded)


def _import_all(
    scheme: ImportScheme, output_dir: Path, overwrite: bool, as_json: bool
) -> tuple[int, list[str]]:
    """Import every included source. Returns (exit code, written paths)."""
    from percell4.adapters.importer import import_infile_dataset

    included = [s for s in scheme.sources if s.included]
    flagged = [s for s in included if s.needs_confirmation]
    if flagged:
        for src in flagged:
            _err(f"{src.output_name}: {src.needs_confirmation} -- confirm it in the scheme file")
        return 2, []
    if not included:
        _err("nothing to import")
        return 2, []

    names = Counter(s.output_name for s in included)
    clashes = sorted(n for n, k in names.items() if k > 1)
    if clashes:
        _err(f"output names used by more than one source: {', '.join(clashes)}")
        return 2, []

    targets = [(s, output_dir / f"{s.output_name}.h5") for s in included]
    existing = [str(t) for _s, t in targets if t.exists()]
    if existing and not overwrite:
        for path in existing:
            _err(f"{path} exists; pass --overwrite to replace it")
        return 1, []

    reader = _make_reader()
    written: list[str] = []
    failed = 0
    try:
        for src, target in targets:
            try:
                import_infile_dataset(
                    src, target, reader, z_method=scheme.z_method, output_dir=output_dir,
                    storage=scheme.storage,
                )
            except PercellError as exc:
                failed += 1
                _err(f"{src.path}: {exc}")
                continue
            written.append(str(target))
            if not as_json:
                print(f"Imported {target}")
    finally:
        close = getattr(reader, "close", None)
        if close is not None:
            close()
    return (1 if failed else 0), written


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the process exit code."""
    parser = argparse.ArgumentParser(
        prog="percell-import",
        description=(
            "Import multi-dimensional microscopy files (channels, z and time "
            "inside one file) through Bio-Formats. Each file series becomes "
            "one .h5 dataset. Z is always projected (max, mean or sum)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  percell-import /data/stacks/ --scan-only --scheme-out scheme.json\n"
            "  percell-import --scheme scheme.json --output-dir /data/h5/\n"
            "  percell-import stack.tif --output-dir /data/h5/ --z-method mean\n"
        ),
    )
    parser.add_argument(
        "sources", nargs="*",
        help="Files or directories to scan (directories are not recursive).",
    )
    parser.add_argument("--output-dir", type=Path, help="Folder for the .h5 outputs.")
    parser.add_argument(
        "--scan-only", action="store_true", help="Print the suggested scheme; import nothing."
    )
    parser.add_argument("--scheme-out", type=Path, help="Write the scheme to this JSON file.")
    parser.add_argument(
        "--scheme", type=Path, help="Import the sources listed in this scheme file."
    )
    parser.add_argument(
        "--z-method", choices=Z_METHODS, default="mip",
        help="Z projection for a scan (default mip). A scheme file keeps its own.",
    )
    parser.add_argument(
        "--overwrite", action="store_true", help="Replace existing .h5 outputs as a whole."
    )
    parser.add_argument(
        "--provision-java", action="store_true",
        help="Download Java and Bio-Formats into the PerCell cache first.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON instead of text.")
    args = parser.parse_args(argv)

    if not args.sources and args.scheme is None:
        parser.error("give files or directories to scan, or --scheme")
    if args.sources and args.scheme is not None:
        parser.error("give either files to scan or --scheme, not both")
    if not args.scan_only and args.output_dir is None:
        parser.error("--output-dir is required unless --scan-only")

    try:
        if args.provision_java:
            _provision(progress=lambda msg, *_: _err(msg))
        if args.scheme is not None:
            scheme = scheme_json.loads(args.scheme.read_text())
        else:
            scheme = _scan(args.sources, args.z_method)
    except (JavaUnavailableError, BioformatsUnavailableError) as exc:
        _err(str(exc))
        _err("run again with --provision-java, or set java_home in the advanced settings")
        return 1
    except (ImportSchemeError, OSError) as exc:
        _err(str(exc))
        return 1

    if args.scheme_out is not None:
        args.scheme_out.write_text(scheme_json.dumps(scheme))

    if args.scan_only:
        if args.json:
            print(json.dumps(scheme_json.to_dict(scheme), indent=2))
        else:
            _print_scheme(scheme)
        return 0 if scheme.sources else 2

    if not args.json:
        _print_scheme(scheme)
    output_dir: Path = args.output_dir
    code, written = _import_all(scheme, output_dir, args.overwrite, args.json)
    if args.json:
        record = scheme_json.to_dict(scheme)
        record["imported"] = written
        print(json.dumps(record, indent=2))
    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

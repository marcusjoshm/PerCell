"""CLI: add a max, mean or sum projection to datasets that kept their z-series.

Headless front-end for adding a projection later (z-stack plan U9). Each
dataset's stored z-series is projected plane by plane into the new named
projection; the dataset is left unchanged if anything goes wrong. A dataset
that kept no z-series, or already stores the projection, is reported and
the batch continues.

Usage:
    percell-batch-add-projection /data/h5/ --projection mean
    percell-batch-add-projection a.h5 b.h5 --projection max

Exit codes:
    0 -- at least one dataset gained the projection
    1 -- no dataset gained it (all refused or failed)
    2 -- usage error

Programmatic use:
    from percell4.interfaces.cli.batch_add_projection import main
    exit_code = main(["a.h5", "--projection", "mean"])
"""

from __future__ import annotations

import argparse
import sys

from percell4.domain.errors import AddProjectionError
from percell4.domain.io.projections import PROJECTION_NAMES
from percell4.interfaces.cli._batch_report import resolve_paths


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the process exit code."""
    parser = argparse.ArgumentParser(
        prog="percell-batch-add-projection",
        description=(
            "Add a z-projection to .h5 datasets that were imported keeping "
            "their full z-series. The projection is computed from the stored "
            "z-series (at the dataset's creation bin); analysis can then read "
            "it with the Projection selector or --projection."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  percell-batch-add-projection /data/h5/ --projection mean\n"
            "  percell-batch-add-projection a.h5 --projection max\n"
        ),
    )
    parser.add_argument(
        "paths", nargs="+",
        help="One or more .h5 files, or directories of them (not recursive).",
    )
    parser.add_argument(
        "--projection", required=True, choices=PROJECTION_NAMES,
        help="The projection to add.",
    )
    args = parser.parse_args(argv)

    from percell4.store import DatasetStore

    paths = resolve_paths(args.paths)
    if not paths:
        print("error: no .h5 files matched the given paths", file=sys.stderr)
        return 1

    added = refused = failed = 0
    for path in paths:
        try:
            DatasetStore(path).add_projection_from_zseries(args.projection)
        except AddProjectionError as exc:
            refused += 1
            print(f"[skipped] {path.name}: {exc}")
        except Exception as exc:  # noqa: BLE001 - per-dataset isolation
            failed += 1
            print(f"[failed]  {path.name}: {exc}")
        else:
            added += 1
            print(f"[added]   {path.name}: {args.projection}")
    print(f"\nTotals: {added} added, {refused} skipped, {failed} failed")
    return 0 if added else 1


if __name__ == "__main__":
    sys.exit(main())

"""The ``--projection`` option every headless command that reads intensity shares.

Its default is "not given" (``None``), so the store's resolver applies R11:
the preferred projection (max, the constant the Session also uses) when the
dataset holds it, else its only projection. A dataset with several
projections and no max fails with a message naming them, until you pass
``--projection``; the other datasets in the batch still run. A dataset
imported before projections were named reads its one image, as before.
"""

from __future__ import annotations

import argparse

from percell4.domain.io.projections import PREFERRED_PROJECTION, PROJECTION_NAMES

PROJECTION_HELP = (
    "Z-projection to read intensity from: "
    + ", ".join(PROJECTION_NAMES)
    + f". Default: {PREFERRED_PROJECTION} when the dataset holds it, else its only "
    "projection; a dataset with several and no "
    f"{PREFERRED_PROJECTION} fails until you name one. Datasets imported before "
    "projections were named read their one image either way."
)


def add_projection_option(parser: argparse.ArgumentParser) -> None:
    """Add ``--projection {max,mean,sum}``, default "not given"."""
    parser.add_argument(
        "--projection", choices=PROJECTION_NAMES, default=None, help=PROJECTION_HELP
    )

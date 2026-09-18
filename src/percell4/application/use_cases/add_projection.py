"""Use case: add a max, mean or sum projection from the stored z-series (U9, KTD10).

A creator action, following the store -> viewer -> refresh lists sequence
without a focus change: like adding a channel, it never switches the active
projection. The one exception is a z-series-only dataset gaining its first
projection, which then becomes readable (the Session picks it, R5).
"""

from __future__ import annotations

from collections.abc import Callable

from percell4.application.session import Session
from percell4.domain.errors import NoDatasetError
from percell4.ports.dataset_repository import DatasetRepository


class AddProjection:
    """Adds one projection to the Session's dataset."""

    def __init__(self, repo: DatasetRepository, session: Session) -> None:
        self._repo = repo
        self._session = session

    def execute(
        self,
        name: str,
        *,
        on_plane: Callable[[int, int], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> list[str]:
        """Add projection ``name``; return the dataset's projection names.

        Raises :class:`~percell4.domain.errors.AddProjectionError` (no
        z-series, or already stored) with the dataset unchanged.
        """
        handle = self._session.dataset
        if handle is None:
            raise NoDatasetError("No dataset loaded")
        self._repo.add_projection(handle, name, on_plane=on_plane, is_cancelled=is_cancelled)
        names = self._repo.list_projections(handle)
        # The dataset now stores named projections, so measurements record
        # which one they read (KTD9).
        handle.metadata["named_projections"] = True
        self._session.refresh_resource_lists(projection_names=names)
        return names

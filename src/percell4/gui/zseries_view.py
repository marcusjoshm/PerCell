"""Show a dataset's stored z-series in the viewer (z-stack plan U8, KTD7).

The z-series is for viewing only. Turning it on adds one lazy image layer
per z-series channel, named ``<channel> (z-series)`` so no tool that finds
its input layer by channel name can pick one up. napari's own 2D/3D button
then gives Z and T sliders in 2D and a rotatable render in 3D.

While it is shown, segmentations and masks are hidden (they are 2D); the
overlay option shows them again as read-only views repeated through every
z-plane. On a time-lapse dataset the projection image layers are hidden
too: napari aligns layers by their trailing axes, so a ``(T, H, W)``
projection would otherwise put T on the Z slider. Turning the z-series off
removes its layers and restores what was hidden.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from percell4.adapters.zseries_lazy import lazy_zseries_channel, zseries_contrast_limits
from percell4.domain.io.layout import (
    is_zseries_view_layer,
    through_z_layer_name,
    zseries_layer_name,
    zseries_scale,
)
from percell4.gui.viewer import LAYER_TYPE_MASK, LAYER_TYPE_SEGMENTATION, PERCELL_TYPE_KEY
from percell4.store import DatasetStore

#: Layer-type tag of the z-series layers and their through-Z overlays.
LAYER_TYPE_ZSERIES = "zseries"

_OVERLAY_SOURCES = (LAYER_TYPE_SEGMENTATION, LAYER_TYPE_MASK)


def overlay_sources(viewer) -> list:
    """Segmentation and mask layers that can be shown through Z."""
    return [
        layer for layer in viewer.layers
        if layer.metadata.get(PERCELL_TYPE_KEY) in _OVERLAY_SOURCES
        and not is_zseries_view_layer(layer.name)
    ]


class ZSeriesView:
    """Adds and removes the z-series layers of one viewer window.

    ``shown`` and ``overlay`` are the state the user asked for. After the
    viewer is rebuilt (Pixel Binning or projection change), call
    :meth:`forget_layers` and then :meth:`apply` to show it again.
    """

    def __init__(self, viewer_win) -> None:
        self._win = viewer_win
        self.shown = False
        self.overlay = False
        self._hidden: dict[str, bool] = {}
        self._layer_names: list[str] = []
        self._overlay_names: list[str] = []
        self._scale: tuple[float, ...] = (1.0, 1.0, 1.0)
        self._n_z = 1
        self._n_t: int | None = None

    # -- state ---------------------------------------------------------------

    @property
    def layer_names(self) -> list[str]:
        return list(self._layer_names)

    def forget_layers(self) -> None:
        """The viewer was cleared: drop references to layers that are gone."""
        self._hidden.clear()
        self._layer_names.clear()
        self._overlay_names.clear()

    def apply(self, h5_path: str | Path, view_bin: int) -> None:
        """Show or hide to match ``shown`` and ``overlay``."""
        if self.shown and not self._layer_names:
            self._add_zseries(Path(h5_path), view_bin)
        elif not self.shown and self._layer_names:
            self._remove_zseries()
        if self.shown:
            self._set_overlay_layers(self.overlay)

    def set_shown(self, shown: bool, h5_path: str | Path, view_bin: int) -> None:
        self.shown = bool(shown)
        self.apply(h5_path, view_bin)

    def set_overlay(self, overlay: bool, h5_path: str | Path, view_bin: int) -> None:
        self.overlay = bool(overlay)
        self.apply(h5_path, view_bin)

    # -- layers --------------------------------------------------------------

    def _add_zseries(self, h5_path: Path, view_bin: int) -> None:
        import napari

        viewer = self._win.viewer
        store = DatasetStore(h5_path)
        meta = store.metadata
        shape = store.zseries_shape()
        timed = len(shape) == 5
        self._n_t = shape[0] if timed else None
        self._n_z = shape[-3]
        self._scale = zseries_scale(
            meta.get("z_spacing_um"), meta.get("pixel_size_um"), view_bin, timed
        )
        for layer in list(viewer.layers):
            kind = layer.metadata.get(PERCELL_TYPE_KEY)
            hide = kind in _OVERLAY_SOURCES or (
                timed and isinstance(layer, napari.layers.Image)
                and not is_zseries_view_layer(layer.name)
            )
            if hide and layer.name not in self._hidden:
                self._hidden[layer.name] = bool(layer.visible)
                layer.visible = False
        for index, channel in enumerate(store.zseries_channels()):
            kwargs = {}
            if channel in viewer.layers:
                kwargs["colormap"] = viewer.layers[channel].colormap
            name = zseries_layer_name(channel)
            self._win.add_image(
                lazy_zseries_channel(h5_path, index, view_bin),
                name=name,
                scale=self._scale,
                contrast_limits=zseries_contrast_limits(h5_path, channel, index, view_bin),
                metadata={PERCELL_TYPE_KEY: LAYER_TYPE_ZSERIES},
                **kwargs,
            )
            self._layer_names.append(name)

    def _remove_zseries(self) -> None:
        viewer = self._win.viewer
        self._set_overlay_layers(False)
        for name in self._layer_names:
            if name in viewer.layers:
                viewer.layers.remove(name)
        self._layer_names.clear()
        for name, visible in self._hidden.items():
            if name in viewer.layers:
                viewer.layers[name].visible = visible
        self._hidden.clear()

    def _set_overlay_layers(self, on: bool) -> None:
        viewer = self._win.viewer
        if not on:
            for name in self._overlay_names:
                if name in viewer.layers:
                    viewer.layers.remove(name)
            self._overlay_names.clear()
            return
        if self._overlay_names:
            return
        for layer in overlay_sources(viewer):
            data = self.through_z(np.asarray(layer.data))
            if data is None:
                continue
            name = through_z_layer_name(layer.name)
            added = viewer.add_labels(
                data,
                name=name,
                scale=self._scale,
                opacity=layer.opacity,
                blending=layer.blending,
                colormap=layer.colormap,
                metadata={PERCELL_TYPE_KEY: LAYER_TYPE_ZSERIES},
            )
            added.editable = False
            self._overlay_names.append(name)

    def through_z(self, data: np.ndarray) -> np.ndarray | None:
        """A read-only view of a 2D (or per-timepoint) layer repeated through Z.

        No copy: every z-plane of the view is the same memory as the layer.
        ``None`` when the layer's shape does not fit the z-series.
        """
        n_z = self._n_z
        if self._n_t is None:
            if data.ndim != 2:
                return None
            return np.broadcast_to(data[np.newaxis], (n_z, *data.shape))
        if data.ndim == 2:
            return np.broadcast_to(data[np.newaxis, np.newaxis], (self._n_t, n_z, *data.shape))
        if data.ndim == 3 and data.shape[0] == self._n_t:
            return np.broadcast_to(data[:, np.newaxis], (self._n_t, n_z, *data.shape[1:]))
        return None

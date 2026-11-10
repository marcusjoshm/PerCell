"""The AnalyzeIt review on a real napari viewer: circles drawn on the one shapes layer
come back as masks aligned with the image, and the review's layers leave cleanly."""

from __future__ import annotations

import numpy as np
import pytest

from percell4.application.session import Session
from percell4.gui.viewer import ViewerWindow
from percell4.gui.workflows.analyzeit_review.window import (
    CIRCLES_LAYER,
    LABELS_LAYER,
    NapariReviewRenderer,
)
from percell4.model import CellDataModel
from percell4.workflows.analyzeit_review.session import Circle, FieldImages


@pytest.fixture
def renderer(qtbot):
    viewer_win = ViewerWindow(CellDataModel(session=Session()))
    viewer_win._ensure_viewer()
    viewer = viewer_win.viewer
    viewer.add_image(np.zeros((8, 8)), name="already open")
    renderer = NapariReviewRenderer(viewer_win)
    drawn = []
    renderer.bind(lambda: drawn.append(True), {})
    yield renderer, viewer, drawn
    try:
        viewer.close()
    except Exception:
        pass


def _field():
    labels = np.zeros((64, 64), np.int32)
    labels[10:20, 10:20] = 1
    return FieldImages("a", {"ch00": np.random.default_rng(0).random((64, 64))}, labels)


def test_lasso_and_ellipse_circles_turn_into_masks_aligned_with_the_image(renderer):
    renderer, viewer, drawn = renderer
    renderer.hide_other_layers()
    assert not viewer.layers["already open"].visible
    renderer.show_field(_field(), [], [])
    assert viewer.layers[LABELS_LAYER].contour == 1

    shapes = viewer.layers[CIRCLES_LAYER]
    renderer.set_circle_kind("problem")
    renderer.set_shape_mode("lasso")
    shapes.add_polygons([np.array([[10, 10], [10, 20], [20, 20], [20, 10]], float)])
    renderer.set_circle_kind("correct")
    renderer.set_shape_mode("ellipse")
    shapes.add_ellipses([np.array([[40, 40], [40, 50], [50, 50], [50, 40]], float)])
    assert drawn

    circles = renderer.drawn_circles()
    assert [(c["kind"], c["shape_type"]) for c in circles] == [
        ("problem", "lasso"), ("correct", "ellipse")]
    lasso = Circle(1, **{**circles[0], "field": "a"}).mask((64, 64))
    ellipse = Circle(2, **{**circles[1], "field": "a"}).mask((64, 64))
    assert lasso[15, 15] and not lasso[30, 30]
    assert ellipse[45, 45] and not ellipse[40, 40]
    # The circle covers the cell drawn at rows and columns 10 to 20.
    assert lasso[_field().labels == 1].mean() > 0.8

    renderer.remove_last_circle()
    assert len(renderer.drawn_circles()) == 1
    renderer.clear_review_layers()
    renderer.restore_other_layers()
    assert [layer.name for layer in viewer.layers] == ["already open"]
    assert viewer.layers["already open"].visible

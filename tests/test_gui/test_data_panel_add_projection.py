"""The Data panel's Add max / mean / sum actions (z-stack plan U9)."""

from __future__ import annotations

from tests.test_application.test_add_projection import _dataset

from percell4.adapters.hdf5_store import Hdf5DatasetRepository
from percell4.application.session import Session
from percell4.interfaces.gui.task_panels.data_panel import DataPanel
from percell4.model import CellDataModel
from percell4.store import DatasetStore


def _panel(qtbot, path):
    session = Session()
    model = CellDataModel(session=session)
    session.set_dataset(Hdf5DatasetRepository().open(path))
    messages = []
    panel = DataPanel(
        data_model=model,
        get_store=lambda: DatasetStore(path),
        get_viewer_window=lambda: None,
        get_h5_path=lambda: str(path),
        show_status=messages.append,
    )
    qtbot.addWidget(panel)
    return panel, session, messages


def test_add_mean_keeps_the_active_projection_and_greys_itself_out(qtbot, tmp_path):
    path = tmp_path / "d.h5"
    _dataset(path, projections=("max",))
    panel, session, messages = _panel(qtbot, path)
    buttons = panel._projection_buttons
    assert not buttons["max"].isEnabled()
    assert "already stored" in buttons["max"].toolTip()
    assert buttons["mean"].isEnabled()

    buttons["mean"].click()

    assert session.projection_names == ["max", "mean"]
    assert session.active_projection == "max"
    assert not buttons["mean"].isEnabled()
    assert "already stored" in buttons["mean"].toolTip()
    assert messages[-1] == "Added the mean projection"


def test_without_a_zseries_every_action_says_why(qtbot, tmp_path):
    path = tmp_path / "p.h5"
    _dataset(path, projections=("max",), zseries=False)
    panel, _session, _messages = _panel(qtbot, path)
    for name in ("mean", "sum"):
        btn = panel._projection_buttons[name]
        assert not btn.isEnabled()
        assert "z-series was not kept" in btn.toolTip()

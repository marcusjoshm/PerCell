"""Token-mode import of z-stacks: z-series and several projections (plan U3).

The first block pins today's single-projection output (characterization);
it must hold unchanged once the z-series path exists.
"""

from __future__ import annotations

import warnings

import numpy as np
import pytest
import tifffile

from percell4.adapters.importer import import_dataset
from percell4.domain.io.assembler import assemble_tiles
from percell4.domain.io.models import TileConfig
from percell4.domain.io.view_bin import sum_bin_2d
from percell4.store import DatasetStore

TH = TW = 24


def _plane(seed: int, h: int = TH, w: int = TW) -> np.ndarray:
    return np.random.default_rng(seed).integers(0, 3000, size=(h, w)).astype(np.uint16)


def _zstack_folder(
    tmp_path, *, n_ch=2, n_z=3, tiles=1, timepoints=None, z_names=None, imagej_spacing=None
):
    """``img[_t..]_s.._z.._ch...tif`` files; returns (folder, data).

    ``data[(t, ch, tile, z)]`` is the plane written for that file.
    """
    src = tmp_path / "raw"
    src.mkdir()
    data = {}
    z_names = z_names or [f"{z:02d}" for z in range(n_z)]
    for t in timepoints or [None]:
        for ch in range(n_ch):
            for tile in range(tiles):
                for zi, z in enumerate(z_names):
                    plane = _plane(1000 * (t or 0) + 100 * ch + 10 * tile + zi)
                    data[(t or 0, ch, tile, zi)] = plane
                    t_tok = f"_t{t:02d}" if t is not None else ""
                    s_tok = f"_s{tile:02d}" if tiles > 1 else ""
                    name = f"img{t_tok}{s_tok}_z{z}_ch{ch:02d}.tif"
                    kwargs = {}
                    if imagej_spacing is not None:
                        kwargs = {"imagej": True, "metadata": {"spacing": imagej_spacing}}
                    tifffile.imwrite(str(src / name), plane, **kwargs)
    return src, data


def _grid() -> TileConfig:
    return TileConfig(grid_rows=2, grid_cols=2, grid_type="row_by_row", order="right_down")


def _stitch(tiles: dict[int, np.ndarray]) -> np.ndarray:
    return assemble_tiles(tiles, grid_rows=2, grid_cols=2, grid_type="row_by_row",
                          order="right_down")


def _storage(*tokens):
    from percell4.domain.io.projections import StorageChoice

    return StorageChoice.parse(",".join(tokens))


# ── characterization: today's single projection ────────────────


def test_baseline_grid_mosaic_max_binned(tmp_path):
    src, data = _zstack_folder(tmp_path, n_ch=2, n_z=3, tiles=4)
    h5 = tmp_path / "out.h5"
    n = import_dataset(src, h5, tile_config=_grid(), z_project_method="mip", creation_bin=2)
    assert n == 2
    expected = np.stack([
        sum_bin_2d(_stitch({
            tile: np.max([data[(0, ch, tile, z)] for z in range(3)], axis=0)
            for tile in range(4)
        }).astype(np.float32), 2)
        for ch in range(2)
    ])
    store = DatasetStore(h5)
    intensity = store.read_array("intensity")
    assert intensity.dtype == np.float32
    np.testing.assert_array_equal(intensity, expected)
    meta = store.metadata
    assert meta["channel_names"] == ["ch00", "ch01"]
    assert meta["n_channels"] == 2
    assert meta["native_shape"] == (TH, TW)
    assert meta["creation_bin"] == 2
    assert meta["n_timepoints"] == 1
    assert store.read_array_attrs("intensity")["dims"].tolist() == ["C", "H", "W"]


def test_baseline_time_lapse_mean(tmp_path):
    src, data = _zstack_folder(tmp_path, n_ch=1, n_z=4, timepoints=[1, 2])
    h5 = tmp_path / "out.h5"
    import_dataset(src, h5, z_project_method="mean")
    expected = np.stack([
        np.mean([data[(t, 0, 0, z)].astype(np.float64) for z in range(4)], axis=0)
        for t in (1, 2)
    ]).astype(np.float32)
    store = DatasetStore(h5)
    np.testing.assert_allclose(store.read_array("intensity"), expected, rtol=1e-6)
    assert store.metadata["n_timepoints"] == 2
    assert store.read_array_attrs("intensity")["dims"].tolist() == ["T", "H", "W"]


def test_baseline_max_only_stores_the_named_max_projection(tmp_path):
    src, _ = _zstack_folder(tmp_path, n_ch=1, n_z=2)
    h5 = tmp_path / "out.h5"
    import_dataset(src, h5)
    store = DatasetStore(h5)
    assert store.list_projections() == ("max",)
    assert store.resolved_intensity_path() == "projections/max"
    assert not store.has_zseries()


# ── z-series and several projections ───────────────────────────


def test_two_channels_five_z_keep_zseries_max_mean(tmp_path):
    src, data = _zstack_folder(tmp_path, n_ch=2, n_z=5)
    h5 = tmp_path / "out.h5"
    import_dataset(src, h5, storage=_storage("max", "mean", "zseries"))
    store = DatasetStore(h5)
    assert store.list_projections() == ("max", "mean")
    assert store.zseries_shape() == (2, 5, TH, TW)
    assert store.zseries_channels() == ("ch00", "ch01")
    for ch in range(2):
        stack = np.stack([data[(0, ch, 0, z)] for z in range(5)])
        for z in range(5):
            np.testing.assert_array_equal(store.read_zseries_plane(0, ch, z), stack[z])
        np.testing.assert_array_equal(
            DatasetStore(h5, projection="max").read_channel("intensity", ch),
            stack.max(axis=0).astype(np.float32),
        )
        np.testing.assert_allclose(
            DatasetStore(h5, projection="mean").read_channel("intensity", ch),
            stack.astype(np.float64).mean(axis=0).astype(np.float32),
            rtol=1e-6,
        )


def test_z_tokens_order_numerically(tmp_path):
    src, data = _zstack_folder(tmp_path, n_ch=1, z_names=["1", "2", "10"])
    h5 = tmp_path / "out.h5"
    import_dataset(src, h5, storage=_storage("max", "zseries"))
    store = DatasetStore(h5)
    for zi in range(3):  # data index order is 1, 2, 10
        np.testing.assert_array_equal(store.read_zseries_plane(0, 0, zi), data[(0, 0, 0, zi)])


def test_grid_mosaic_zseries_planes_are_stitched_planes(tmp_path):
    src, data = _zstack_folder(tmp_path, n_ch=1, n_z=3, tiles=4)
    h5 = tmp_path / "out.h5"
    import_dataset(src, h5, tile_config=_grid(), storage=_storage("max", "zseries"))
    store = DatasetStore(h5)
    assert store.zseries_shape() == (1, 3, 2 * TH, 2 * TW)
    planes = []
    for z in range(3):
        stitched = _stitch({tile: data[(0, 0, tile, z)] for tile in range(4)})
        np.testing.assert_array_equal(store.read_zseries_plane(0, 0, z), stitched)
        planes.append(stitched)
    np.testing.assert_array_equal(
        store.read_array("intensity"), np.max(planes, axis=0).astype(np.float32)
    )


def test_time_lapse_keeps_tczyx(tmp_path):
    src, data = _zstack_folder(tmp_path, n_ch=2, n_z=3, timepoints=[1, 2])
    h5 = tmp_path / "out.h5"
    import_dataset(src, h5, storage=_storage("sum", "zseries"))
    store = DatasetStore(h5)
    assert store.zseries_shape() == (2, 2, 3, TH, TW)
    assert store.metadata["n_timepoints"] == 2
    np.testing.assert_array_equal(store.read_zseries_plane(1, 1, 2), data[(2, 1, 0, 2)])


def test_zseries_only_is_view_only(tmp_path):
    src, _ = _zstack_folder(tmp_path, n_ch=2, n_z=3)
    h5 = tmp_path / "out.h5"
    import_dataset(src, h5, storage=_storage("zseries"))
    store = DatasetStore(h5)
    assert store.list_projections() == ()
    assert store.zseries_shape() == (2, 3, TH, TW)
    assert store.metadata["native_shape"] == (TH, TW)
    assert store.metadata["n_channels"] == 2


def test_binned_zseries(tmp_path):
    src, data = _zstack_folder(tmp_path, n_ch=1, n_z=2)
    h5 = tmp_path / "out.h5"
    import_dataset(src, h5, creation_bin=2, storage=_storage("max", "zseries"))
    store = DatasetStore(h5)
    assert store.zseries_shape() == (1, 2, TH // 2, TW // 2)
    np.testing.assert_array_equal(
        store.read_zseries_plane(0, 0, 1),
        sum_bin_2d(data[(0, 0, 0, 1)].astype(np.float32), 2),
    )
    np.testing.assert_array_equal(
        store.read_array("intensity"),
        sum_bin_2d(np.maximum(data[(0, 0, 0, 0)], data[(0, 0, 0, 1)]).astype(np.float32), 2),
    )


def test_imagej_spacing_is_recorded(tmp_path):
    src, _ = _zstack_folder(tmp_path, n_ch=1, n_z=2, imagej_spacing=0.5)
    h5 = tmp_path / "out.h5"
    import_dataset(src, h5, z_step_um=0.9)
    assert DatasetStore(h5).metadata["z_spacing_um"] == pytest.approx(0.5)


def test_user_z_step_is_used_without_imagej_spacing(tmp_path):
    src, _ = _zstack_folder(tmp_path, n_ch=1, n_z=2)
    h5 = tmp_path / "out.h5"
    import_dataset(src, h5, z_step_um=0.9)
    assert DatasetStore(h5).metadata["z_spacing_um"] == pytest.approx(0.9)


def test_no_spacing_records_none(tmp_path):
    src, _ = _zstack_folder(tmp_path, n_ch=1, n_z=2)
    h5 = tmp_path / "out.h5"
    import_dataset(src, h5)
    assert "z_spacing_um" not in DatasetStore(h5).metadata


def test_single_plane_folder_imports_as_today(tmp_path):
    """Covers AE7: no Z, so the storage choice does not apply (R4)."""
    src, data = _zstack_folder(tmp_path, n_ch=2, n_z=1)
    h5 = tmp_path / "out.h5"
    import_dataset(src, h5, storage=_storage("mean", "zseries"))
    store = DatasetStore(h5)
    assert store.resolved_intensity_path() == "intensity"
    assert not store.has_zseries()
    np.testing.assert_array_equal(
        store.read_array("intensity"),
        np.stack([data[(0, ch, 0, 0)] for ch in range(2)]).astype(np.float32),
    )


def test_bin_channel_joins_every_projection_not_the_zseries(tmp_path):
    src, _ = _zstack_folder(tmp_path, n_ch=1, n_z=2)
    decay = np.ones((TH, TW, 4), dtype=np.uint32)
    (src / "img_ch05.bin").write_bytes(decay.tobytes())
    flim = {
        "frequency_mhz": 80.0,
        "channel_calibrations": {},
        "bin_dimensions": {
            "x_dim": TW, "y_dim": TH, "t_dim": 4,
            "dtype": "uint32", "dim_order": "YXT", "header_bytes": 0,
        },
    }
    h5 = tmp_path / "out.h5"
    import_dataset(src, h5, flim_params=flim, storage=_storage("max", "sum", "zseries"))
    store = DatasetStore(h5)
    names = store.metadata["channel_names"]
    assert len(names) == 2 and names[0] == "ch00"  # the .bin synth is second
    assert store.zseries_channels() == ("ch00",)
    for name in ("max", "sum"):
        arr = DatasetStore(h5, projection=name).read_array("intensity")
        assert arr.shape == (2, TH, TW)
        np.testing.assert_array_equal(arr[1], np.full((TH, TW), 4, np.float32))


def test_registered_mosaic_zseries_max_equals_registered_max(tmp_path):
    from tests.test_io.test_importer import (
        _REG_CANVAS,
        _REG_CORNERS,
        _REG_TH,
        _REG_TW,
        _reg_tile_config,
        _textured_scene,
    )

    src = tmp_path / "raw"
    src.mkdir()
    scene = _textured_scene(7)
    for i, (y, x) in _REG_CORNERS.items():
        tile = scene[y : y + _REG_TH, x : x + _REG_TW]
        for z in range(3):
            tifffile.imwrite(
                str(src / f"img_s{i:02d}_z{z:02d}_ch00.tif"), (tile + 7 * z).astype(np.uint16)
            )

    h5 = tmp_path / "out.h5"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        import_dataset(src, h5, tile_config=_reg_tile_config(),
                       storage=_storage("max", "mean", "zseries"))
    store = DatasetStore(h5)
    assert store.read_stitch_geometry().registered is True
    assert store.zseries_shape() == (1, 3, *_REG_CANVAS)
    zstack = np.stack([store.read_zseries_plane(0, 0, z) for z in range(3)])
    np.testing.assert_array_equal(zstack.max(axis=0), store.read_array("intensity"))
    np.testing.assert_allclose(
        zstack.astype(np.float64).mean(axis=0),
        DatasetStore(h5, projection="mean").read_array("intensity"),
        rtol=1e-6,
    )

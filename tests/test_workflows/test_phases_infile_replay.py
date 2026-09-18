"""Workflow replay of in-file entries: one entry, one source, one import.

Legacy plans (no ``infile_source`` key) must reach ``import_dataset`` exactly
as before; see ``test_phases_compress_field_parity.py`` for that side.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from tests.fakes.fake_image_reader import FakeImageReader, probe_for

from percell4.domain.io import scheme_json
from percell4.domain.io.infile import ImportSource
from percell4.store import DatasetStore
from percell4.workflows.failures import DatasetFailure
from percell4.workflows.models import DatasetSource, WorkflowDatasetEntry
from percell4.workflows.phases import compress_one


def _infile_entry(tmp_path: Path, name: str, channels=(0, 1), c=2):
    path = tmp_path / f"{name}.tif"
    path.write_bytes(b"x" * 32)
    stack = np.random.default_rng(len(name)).integers(0, 500, (1, c, 3, 8, 8)).astype(
        np.uint16
    )
    probe = probe_for(path, stack, physical_z_um=0.5)
    st = path.stat()
    source = ImportSource(
        path=path, channel_indices=tuple(channels), output_name=name,
        expected_size=st.st_size, expected_mtime_ns=st.st_mtime_ns, series=probe.series[0],
    )
    out = tmp_path / "out" / f"{name}.h5"
    entry = WorkflowDatasetEntry(
        name=name,
        source=DatasetSource.TIFF_PENDING,
        h5_path=out,
        channel_names=[f"ch{i}" for i in channels],
        compress_plan={
            "infile_source": scheme_json.source_to_dict(source),
            "z_method": "mip",
            "output_path": str(out),
            "creation_bin": 1,
        },
    )
    return entry, (path, stack), probe


@pytest.fixture
def fake_shared_reader(monkeypatch):
    holder = {}

    def install(pairs, probes):
        reader = FakeImageReader(probes, arrays={(p, 0): s for p, s in pairs})
        holder["reader"] = reader
        monkeypatch.setattr(
            "percell4.adapters.infile_scan.shared_reader", lambda: reader
        )
        return reader

    return install


def test_each_infile_entry_imports_its_own_source_once(tmp_path, fake_shared_reader):
    entries, pairs, probes = [], [], []
    for name in ("a", "b", "c"):
        entry, pair, probe = _infile_entry(tmp_path, name)
        entries.append(entry)
        pairs.append(pair)
        probes.append(probe)
    reader = fake_shared_reader(pairs, probes)

    with patch("percell4.adapters.importer.import_dataset") as legacy:
        results = [compress_one(e) for e in entries]

    assert not legacy.called
    assert [len(reader.stream_calls)] == [3]
    assert [src.path.name for src in reader.stream_calls] == ["a.tif", "b.tif", "c.tif"]
    for updated, failure, _msg in results:
        assert failure is None
        assert updated.source is DatasetSource.H5_EXISTING
        assert updated.h5_path.exists()


def test_entry_channel_names_match_the_written_dataset(tmp_path, fake_shared_reader):
    entry, pair, probe = _infile_entry(tmp_path, "a", channels=(0, 2), c=3)
    fake_shared_reader([pair], [probe])

    updated, failure, _ = compress_one(entry)

    assert failure is None
    assert DatasetStore(updated.h5_path).metadata["channel_names"] == entry.channel_names


def test_infile_failure_is_a_compress_failure(tmp_path, fake_shared_reader):
    entry, pair, probe = _infile_entry(tmp_path, "a")
    fake_shared_reader([pair], [probe])
    Path(pair[0]).write_bytes(b"changed on disk")

    _updated, failure, msg = compress_one(entry)

    assert failure is DatasetFailure.COMPRESS_FAILED
    assert "changed since" in msg


def test_legacy_none_projection_fails_with_a_named_reason(tmp_path):
    entry = WorkflowDatasetEntry(
        name="old",
        source=DatasetSource.TIFF_PENDING,
        h5_path=tmp_path / "old.h5",
        channel_names=["ch00"],
        compress_plan={
            "source_dir": str(tmp_path),
            "files": [str(tmp_path / "x_ch00.tif")],
            "output_path": str(tmp_path / "old.h5"),
            "z_project_method": "none",
            "selected_channels": ["00"],
            "layer_assignments": {},
        },
    )
    with patch("percell4.adapters.importer.import_dataset") as legacy:
        _updated, failure, msg = compress_one(entry)

    assert not legacy.called
    assert failure is DatasetFailure.COMPRESS_FAILED
    assert "z_project_method" in msg and "none" in msg

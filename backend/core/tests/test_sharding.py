"""Test deterministic duration-aware core test sharding."""

from __future__ import annotations

import json
import statistics
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import conftest
from core.conftest import _assign_test_files, _duration_for_path, _load_test_durations

_PATHS = ["core/a.py", "core/b.py", "core/c.py", "core/d.py", "core/e.py"]
_DURATIONS = {
    "core/a.py": 10.0,
    "core/b.py": 8.0,
    "core/c.py": 5.0,
    "core/d.py": 3.0,
    "core/e.py": 1.0,
}


@pytest.mark.parametrize("shard_count", [4, 6])
def test_lpt_assigns_each_file_to_one_shard(shard_count: int) -> None:
    """Assign each test file to exactly one shard.

    Args:
        shard_count: Number of shards to assign.
    """
    assignments = _assign_test_files(_PATHS, shard_count, _DURATIONS)

    assert set(assignments) == set(_PATHS)
    assert all(0 <= shard < shard_count for shard in assignments.values())
    assert assignments == _assign_test_files(_PATHS, shard_count, _DURATIONS)


def test_unknown_file_uses_median_duration() -> None:
    """Use the measured median for a file without a duration entry."""
    assert _duration_for_path("core/unknown.py", _DURATIONS) == statistics.median(_DURATIONS.values())
    assignments = _assign_test_files([*_PATHS, "core/unknown.py"], 6, _DURATIONS)
    assert "core/unknown.py" in assignments


def test_invalid_duration_json_uses_crc32_fallback(tmp_path: Path) -> None:
    """Fall back to stable CRC32 assignment when duration data is invalid.

    Args:
        tmp_path: Temporary directory for invalid duration data.
    """
    duration_file = tmp_path / "test_durations.json"
    duration_file.write_text("{")

    durations = _load_test_durations(duration_file)
    assignments = _assign_test_files(_PATHS, 6, durations)

    assert durations is None
    assert assignments == {path: zlib.crc32(path.encode()) % 6 for path in sorted(_PATHS)}


def test_valid_duration_json_loads_file_prefixes(tmp_path: Path) -> None:
    """Load duration entries from a valid JSON mapping.

    Args:
        tmp_path: Temporary directory for valid duration data.
    """
    duration_file = tmp_path / "test_durations.json"
    duration_file.write_text(json.dumps(_DURATIONS))

    assert _load_test_durations(duration_file) == _DURATIONS


def test_invalid_duration_entries_are_skipped_once_and_zero_is_valid(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Keep valid entries while warning once about invalid values.

    Args:
        tmp_path: Temporary directory for valid and invalid duration data.
        capsys: Capture standard error.
    """
    duration_file = tmp_path / "test_durations.json"
    duration_file.write_text(
        '{"core/zero.py":0,"core/negative.py":-1,"core/bad.py":"fast",'
        '"core/nan.py":NaN,"core/positive-infinity.py":Infinity,'
        '"core/negative-infinity.py":-Infinity}'
    )

    assert _load_test_durations(duration_file) == {"core/zero.py": 0.0}
    assert capsys.readouterr().err == "warning: ignored 5 invalid test duration entries\n"


def test_empty_valid_duration_data_uses_balanced_default_weights(tmp_path: Path) -> None:
    """Keep valid JSON on duration-aware sharding when it has no entries.

    Args:
        tmp_path: Temporary directory for duration data.
    """
    duration_file = tmp_path / "test_durations.json"
    duration_file.write_text("{}")

    durations = _load_test_durations(duration_file)

    assert durations == {}
    assert _assign_test_files(_PATHS, 6, durations) == _assign_test_files(_PATHS, 6, durations)
    assert len(set(_assign_test_files(_PATHS, 6, durations).values())) > 1


def test_controller_writes_dispatched_and_collected_nodeids_with_phase_durations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Write dispatched and collected nodeids, preserving duplicate dispatches.

    Args:
        tmp_path: Temporary directory for the output files.
        monkeypatch: Pytest's patch fixture.
    """
    dispatched_path = tmp_path / "dispatched.txt"
    collected_path = tmp_path / "collected.txt"
    durations_path = tmp_path / "durations.json"
    monkeypatch.setattr(conftest, "_is_controller", True)
    monkeypatch.setattr(conftest, "_dispatched_nodeids", [])
    monkeypatch.setattr(conftest, "_file_durations", {})
    monkeypatch.setenv("SKYNET_TEST_DISPATCHED_OUT", str(dispatched_path))
    monkeypatch.setenv("SKYNET_TEST_COLLECTED_OUT", str(collected_path))
    monkeypatch.setenv("SKYNET_TEST_DURATIONS_OUT", str(durations_path))
    conftest.pytest_collection_finish(
        SimpleNamespace(
            items=[SimpleNamespace(nodeid="core/a.py::test_a"), SimpleNamespace(nodeid="core/z.py::test_z")]
        )
    )

    reports = [
        SimpleNamespace(nodeid="core/z.py::test_z", when="setup", duration=0.5),
        SimpleNamespace(nodeid="core/a.py::test_a", when="call", duration=0.2),
        SimpleNamespace(nodeid="core/a.py::test_a", when="setup", duration=0.1),
        SimpleNamespace(nodeid="core/a.py::test_a", when="setup", duration=0.0),
        SimpleNamespace(nodeid="core/a.py::test_a", when="teardown", duration=0.3),
        SimpleNamespace(nodeid="core/a.py::test_b", when="setup", duration=0.4),
    ]
    for report in reports:
        conftest.pytest_runtest_logreport(report)

    conftest.pytest_sessionfinish(SimpleNamespace(config=SimpleNamespace()))

    assert dispatched_path.read_text(encoding="utf-8").splitlines() == [
        "core/a.py::test_a",
        "core/a.py::test_a",
        "core/a.py::test_b",
        "core/z.py::test_z",
    ]
    assert collected_path.read_text(encoding="utf-8").splitlines() == [
        "core/a.py::test_a",
        "core/z.py::test_z",
    ]
    durations = json.loads(durations_path.read_text(encoding="utf-8"))
    assert list(durations) == ["core/a.py", "core/z.py"]
    assert durations == pytest.approx({"core/a.py": 1.0, "core/z.py": 0.5})


def test_worker_does_not_write_dispatched_or_collected_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ignore worker reports because the controller receives every xdist report.

    Args:
        tmp_path: Temporary directory for the output files.
        monkeypatch: Pytest's patch fixture.
    """
    dispatched_path = tmp_path / "dispatched.txt"
    collected_path = tmp_path / "collected.txt"
    durations_path = tmp_path / "durations.json"
    monkeypatch.setattr(conftest, "_is_controller", True)
    monkeypatch.setattr(conftest, "_dispatched_nodeids", [])
    monkeypatch.setattr(conftest, "_file_durations", {})
    monkeypatch.setenv("SKYNET_TEST_DISPATCHED_OUT", str(dispatched_path))
    monkeypatch.setenv("SKYNET_TEST_COLLECTED_OUT", str(collected_path))
    monkeypatch.setenv("SKYNET_TEST_DURATIONS_OUT", str(durations_path))

    conftest.pytest_configure(SimpleNamespace(workerinput={}))
    monkeypatch.setattr(conftest, "_is_controller", False)
    report = SimpleNamespace(nodeid="core/a.py::test_a", when="setup", duration=0.1)
    conftest.pytest_runtest_logreport(report)
    conftest.pytest_collection_finish(SimpleNamespace(items=[SimpleNamespace(nodeid="core/a.py::test_a")]))
    conftest.pytest_sessionfinish(SimpleNamespace(config=SimpleNamespace(workerinput={})))

    assert not dispatched_path.exists()
    assert not collected_path.exists()
    assert not durations_path.exists()

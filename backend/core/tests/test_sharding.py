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


def test_controller_writes_executed_nodeids_and_phase_durations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Write sorted executed nodeids and summed per-file phase durations.

    Args:
        tmp_path: Temporary directory for the output files.
        monkeypatch: Pytest's patch fixture.
    """
    executed_path = tmp_path / "executed.txt"
    durations_path = tmp_path / "durations.json"
    monkeypatch.setattr(conftest, "_is_controller", True)
    monkeypatch.setattr(conftest, "_executed_nodeids", set())
    monkeypatch.setattr(conftest, "_file_durations", {})
    monkeypatch.setenv("SKYNET_TEST_EXECUTED_OUT", str(executed_path))
    monkeypatch.setenv("SKYNET_TEST_DURATIONS_OUT", str(durations_path))

    reports = [
        SimpleNamespace(nodeid="core/z.py::test_z", when="setup", duration=0.5),
        SimpleNamespace(nodeid="core/a.py::test_a", when="call", duration=0.2),
        SimpleNamespace(nodeid="core/a.py::test_a", when="setup", duration=0.1),
        SimpleNamespace(nodeid="core/a.py::test_a", when="teardown", duration=0.3),
        SimpleNamespace(nodeid="core/a.py::test_b", when="setup", duration=0.4),
    ]
    for report in reports:
        conftest.pytest_runtest_logreport(report)

    conftest.pytest_sessionfinish(SimpleNamespace(config=SimpleNamespace()))

    assert executed_path.read_text(encoding="utf-8").splitlines() == [
        "core/a.py::test_a",
        "core/a.py::test_b",
        "core/z.py::test_z",
    ]
    durations = json.loads(durations_path.read_text(encoding="utf-8"))
    assert list(durations) == ["core/a.py", "core/z.py"]
    assert durations == pytest.approx({"core/a.py": 1.0, "core/z.py": 0.5})


def test_worker_does_not_write_execution_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ignore worker reports because the controller receives every xdist report.

    Args:
        tmp_path: Temporary directory for the output files.
        monkeypatch: Pytest's patch fixture.
    """
    executed_path = tmp_path / "executed.txt"
    durations_path = tmp_path / "durations.json"
    monkeypatch.setattr(conftest, "_is_controller", True)
    monkeypatch.setattr(conftest, "_executed_nodeids", set())
    monkeypatch.setattr(conftest, "_file_durations", {})
    monkeypatch.setenv("SKYNET_TEST_EXECUTED_OUT", str(executed_path))
    monkeypatch.setenv("SKYNET_TEST_DURATIONS_OUT", str(durations_path))

    conftest.pytest_configure(SimpleNamespace(workerinput={}))
    report = SimpleNamespace(nodeid="core/a.py::test_a", when="setup", duration=0.1)
    conftest.pytest_runtest_logreport(report)
    conftest.pytest_sessionfinish(SimpleNamespace(config=SimpleNamespace(workerinput={})))

    assert not executed_path.exists()
    assert not durations_path.exists()

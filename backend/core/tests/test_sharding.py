"""Test deterministic duration-aware core test sharding."""

from __future__ import annotations

import json
import statistics
import zlib
from pathlib import Path

import pytest

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

"""Shared fixtures and file sharding for the core test suite.

Regenerate measured durations with
``SKYNET_TEST_DURATIONS_OUT=core/test_durations.json uv run pytest core/ -q -n auto``.
"""

from __future__ import annotations

import json
import math
import os
import statistics
import zlib
from pathlib import Path
from typing import Any

import pytest

from core.api import interview_model
from core.billing import openrouter_prices
from core.config import app_env

_DURATION_FILE = Path(__file__).with_name("test_durations.json")
# xdist reports reach the controller, so only it may write the shared artifacts.
_is_controller = False
_dispatched_nodeids: list[str] = []
_file_durations: dict[str, float] = {}


def _load_test_durations(path: Path = _DURATION_FILE) -> dict[str, float] | None:
    """Load valid per-file test durations.

    Args:
        path: JSON file that maps test file prefixes to seconds.

    Returns:
        Valid durations, or ``None`` when the file cannot be used.
    """
    try:
        values: Any = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(values, dict) or not values:
        return None
    durations: dict[str, float] = {}
    for name, seconds in values.items():
        if not isinstance(name, str) or not name or isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            return None
        try:
            duration = float(seconds)
        except OverflowError:
            return None
        if not math.isfinite(duration) or duration <= 0:
            return None
        durations[name] = duration
    return durations


def _duration_for_path(path: str, durations: dict[str, float]) -> float:
    """Return a file's measured duration or the median duration.

    Args:
        path: Test file nodeid prefix.
        durations: Measured durations keyed by test file prefix.

    Returns:
        The measured duration or median fallback.
    """
    return durations.get(path, statistics.median(durations.values()))


def _assign_test_files(paths: list[str], shard_count: int, durations: dict[str, float] | None) -> dict[str, int]:
    """Assign each test file to one deterministic shard.

    Args:
        paths: Test file nodeid prefixes.
        shard_count: Number of shards.
        durations: Valid durations, or ``None`` to use CRC32 fallback.

    Returns:
        A mapping from each file prefix to its shard index.
    """
    files = sorted(set(paths))
    if durations is None:
        return {path: zlib.crc32(path.encode()) % shard_count for path in files}

    loads = [0.0] * shard_count
    assignments: dict[str, int] = {}
    for path in sorted(files, key=lambda file: (-_duration_for_path(file, durations), file)):
        shard = min(range(shard_count), key=lambda index: (loads[index], index))
        assignments[path] = shard
        loads[shard] += _duration_for_path(path, durations)
    return assignments


def pytest_configure(config: pytest.Config) -> None:
    """Reset dispatched-test tracking on the controller process.

    Args:
        config: Pytest configuration for this process.
    """
    global _is_controller
    _is_controller = not hasattr(config, "workerinput")
    if _is_controller:
        _dispatched_nodeids.clear()
        _file_durations.clear()


def pytest_collection_finish(session: pytest.Session) -> None:
    """Write collected nodeids from the controller.

    Args:
        session: The active pytest session.
    """
    collected_out = os.environ.get("SKYNET_TEST_COLLECTED_OUT")
    if _is_controller and collected_out:
        Path(collected_out).write_text(
            "".join(f"{item.nodeid}\n" for item in session.items),
            encoding="utf-8",
        )


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Record dispatched tests and their per-file phase durations on the controller.

    Args:
        report: A test report for one execution phase.
    """
    if not _is_controller:
        return
    if report.when == "setup":
        _dispatched_nodeids.append(report.nodeid)
    path = report.nodeid.split("::", 1)[0]
    _file_durations[path] = _file_durations.get(path, 0.0) + report.duration


def pytest_sessionfinish(session: pytest.Session) -> None:
    """Write optional dispatch and duration reports after the controller session.

    Args:
        session: The pytest session that is finishing.
    """
    if not _is_controller or hasattr(session.config, "workerinput"):
        return
    dispatched_out = os.environ.get("SKYNET_TEST_DISPATCHED_OUT")
    if dispatched_out:
        Path(dispatched_out).write_text(
            "".join(f"{nodeid}\n" for nodeid in sorted(_dispatched_nodeids)),
            encoding="utf-8",
        )
    durations_out = os.environ.get("SKYNET_TEST_DURATIONS_OUT")
    if durations_out:
        Path(durations_out).write_text(
            json.dumps(dict(sorted(_file_durations.items())), indent=2) + "\n",
            encoding="utf-8",
        )


@pytest.fixture(autouse=True)
def _offline_openrouter_prices(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep pricing off the network so charges come from the static table or fallback.

    Args:
        monkeypatch: Pytest's patch fixture, which restores the flag afterwards.
    """
    openrouter_prices.reset()
    monkeypatch.setattr(openrouter_prices, "fetch_enabled", False)


@pytest.fixture(autouse=True)
def _offline_interview_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the interview-model pick from fetching OpenRouter stats in a background thread.

    Args:
        monkeypatch: Pytest's patch fixture.
    """
    monkeypatch.setattr(interview_model, "_kick_refresh", lambda: None)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Keep only this CI shard's test files when ``APP_TEST_SHARD`` is set.

    CI splits ``core/`` across parallel jobs with ``APP_TEST_SHARD=i/n``
    (``SKYNET_TEST_SHARD`` is still read).
    Whole files land on one shard so module fixtures still run once per file.

    Args:
        config: The pytest config, used to report deselected tests.
        items: The collected tests, filtered in place.
    """
    spec = app_env("APP_TEST_SHARD")
    if not spec:
        return
    index, total = (int(part) for part in spec.split("/"))
    paths = [item.nodeid.split("::")[0] for item in items]
    assignments = _assign_test_files(paths, total, _load_test_durations())
    keep, drop = [], []
    for item in items:
        bucket = assignments[item.nodeid.split("::")[0]]
        (keep if bucket == index else drop).append(item)
    items[:] = keep
    config.hook.pytest_deselected(items=drop)

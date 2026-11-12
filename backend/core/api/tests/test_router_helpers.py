"""Tests for the shared helpers used by multiple domain routers."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from ...models import OptimizationStatus
from ...storage.usage import StorageUsage
from ..errors import DomainError

# noinspection PyProtectedMember
# noinspection PyProtectedMember
from ..routers._helpers import (
    build_summary,
    enforce_job_quota,
    enforce_storage_quota,
    strip_api_key,
)
from ..routers.constants import (
    TERMINAL_STATUSES,
    VALID_OPTIMIZATION_TYPES,
    VALID_STATUSES,
)


def test_strip_api_key_removes_nested() -> None:
    """``strip_api_key`` removes the nested ``api_key`` while preserving siblings."""
    out = strip_api_key({"name": "gpt", "extra": {"api_key": "sk-secret", "region": "us"}})
    assert "api_key" not in out["extra"]
    assert out["extra"] == {"region": "us"}
    assert out["name"] == "gpt"


def test_strip_api_key_passthrough_without_extra() -> None:
    """A model config without ``extra`` round-trips unchanged."""
    out = strip_api_key({"name": "gpt"})
    assert out == {"name": "gpt"}


def test_strip_api_key_does_not_mutate_input() -> None:
    """``strip_api_key`` returns a new dict and never mutates its input."""
    src = {"name": "gpt", "extra": {"api_key": "sk-x"}}
    strip_api_key(src)
    assert src == {"name": "gpt", "extra": {"api_key": "sk-x"}}


def test_valid_statuses_match_enum() -> None:
    """The whitelisted status set matches the enum exactly."""
    assert {s.value for s in OptimizationStatus} == VALID_STATUSES


def test_terminal_statuses_are_finite() -> None:
    """Include a normal budget stop among finite terminal states."""
    assert {
        OptimizationStatus.success,
        OptimizationStatus.failed,
        OptimizationStatus.cancelled,
        OptimizationStatus.paused,
        OptimizationStatus.stopped,
    } == TERMINAL_STATUSES


def test_valid_job_types_covers_run_and_blackbox() -> None:
    """The whitelisted optimization types cover the public run and black-box options."""
    assert {"blackbox"} == VALID_OPTIMIZATION_TYPES


def test_build_summary_on_pending_job_without_result() -> None:
    """A pending job without result data still produces a valid summary."""
    now = datetime.now(UTC)
    job_data = {
        "optimization_id": "abc123",
        "status": "pending",
        "created_at": now.isoformat(),
        "started_at": None,
        "completed_at": None,
        "payload_overview": {
            "job_type": "run",
            "module_name": "predict",
            "optimizer_name": "gepa",
            "model_name": "gpt-4o-mini",
            "username": "alice",
        },
        "result": None,
        "latest_metrics": {},
        "progress_count": 0,
        "log_count": 0,
    }
    summary = build_summary(job_data)
    assert summary.optimization_id == "abc123"
    assert summary.status.value == "pending"
    assert summary.baseline_test_metric is None
    assert summary.metric_improvement is None


def test_build_summary_computes_improvement() -> None:
    """The summary reports baseline, optimized, and metric improvement values."""
    now = datetime.now(UTC)
    fixture_result = {"baseline_test_metric": 52.89, "optimized_test_metric": 75.9}
    job_data = {
        "optimization_id": "job42",
        "status": "success",
        "created_at": now.isoformat(),
        "started_at": now.isoformat(),
        "completed_at": now.isoformat(),
        "payload_overview": {
            "job_type": "run",
            "module_name": "predict",
            "optimizer_name": "gepa",
            "username": "bob",
        },
        "result": fixture_result,
        "latest_metrics": {},
        "progress_count": 0,
        "log_count": 0,
    }
    summary = build_summary(job_data)
    assert summary.baseline_test_metric == 52.89
    assert summary.optimized_test_metric == 75.9
    assert summary.metric_improvement == pytest.approx(23.01, abs=1e-2)


class _StorageJobStore:
    """Store stub returning a canned storage total and budget for any user."""

    def __init__(self, *, used: int, quota: int) -> None:
        """Capture the canned used-bytes total and byte budget.

        Args:
            used: Bytes ``compute_user_storage`` reports for any user.
            quota: Budget ``get_effective_user_storage_quota`` reports.
        """
        self._used = used
        self._quota = quota

    def compute_user_storage(self, username: str) -> StorageUsage:
        """Return the canned usage for any user.

        Args:
            username: Ignored; present for signature compatibility.

        Returns:
            A :class:`StorageUsage` with the canned total and empty breakdown.
        """
        return StorageUsage(total=self._used, breakdown={})

    def get_effective_user_storage_quota(self, username: str) -> int:
        """Return the canned budget for any user.

        Args:
            username: Ignored; present for signature compatibility.

        Returns:
            The fixed byte budget captured during construction.
        """
        return self._quota


def test_enforce_storage_quota_allows_write_under_budget() -> None:
    """A write that stays under the budget passes without raising."""
    enforce_storage_quota(_StorageJobStore(used=100, quota=1000), "alice", incoming_bytes=200)


def test_enforce_storage_quota_allows_write_landing_exactly_on_budget() -> None:
    """A write landing exactly on the budget is allowed — the boundary is inclusive."""
    enforce_storage_quota(_StorageJobStore(used=800, quota=1000), "alice", incoming_bytes=200)


def test_enforce_storage_quota_rejects_write_over_budget() -> None:
    """A write that would cross the budget is rejected with a coded 409."""
    with pytest.raises(HTTPException) as exc:
        enforce_storage_quota(_StorageJobStore(used=900, quota=1000), "alice", incoming_bytes=200)
    assert exc.value.status_code == 409
    assert exc.value.code == "user.storage.quota_exceeded"
    assert exc.value.params["quota_mb"] == pytest.approx(1000 / (1024 * 1024), abs=0.1)


class _QuotaStore:
    """Job-store double exposing a job count and a resolved per-user quota."""

    def __init__(self, count: int, quota: int | None) -> None:
        """Seed the current job count and the quota the store resolves.

        Args:
            count: Jobs the user already owns.
            quota: What ``get_effective_user_quota`` returns.
        """
        self._count = count
        self._quota = quota

    def count_jobs(self, *, username: str) -> int:
        """Return the seeded job count."""
        return self._count

    def get_effective_user_quota(self, username: str) -> int | None:
        """Return the seeded quota."""
        return self._quota


def test_enforce_job_quota_allows_up_to_the_cap() -> None:
    """Creating the last allowed job passes."""
    enforce_job_quota(_QuotaStore(count=9, quota=10), "alice")


def test_enforce_job_quota_rejects_past_the_cap() -> None:
    """A create that would exceed the cap is a 409 naming the quota."""
    with pytest.raises(DomainError) as exc:
        enforce_job_quota(_QuotaStore(count=10, quota=10), "alice")
    assert exc.value.status_code == 409
    assert exc.value.code == "quota.reached"
    assert exc.value.params == {"quota": 10}


def test_enforce_job_quota_counts_every_incoming_job() -> None:
    """A multi-copy clone is checked against the cap as a whole."""
    with pytest.raises(DomainError):
        enforce_job_quota(_QuotaStore(count=8, quota=10), "alice", incoming_jobs=3)


def test_enforce_job_quota_skips_an_unlimited_override() -> None:
    """An admin override of ``None`` means no cap."""
    enforce_job_quota(_QuotaStore(count=10_000, quota=None), "alice")

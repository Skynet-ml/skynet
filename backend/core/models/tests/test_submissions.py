"""Tests for the OptimizationSubmissionResponse model."""

from __future__ import annotations

from datetime import UTC, datetime

from core.models.common import OptimizationStatus
from core.models.submissions import OptimizationSubmissionResponse


def test_optimization_submission_response_defaults() -> None:
    """Verify OptimizationSubmissionResponse defaults name and description to None."""
    resp = OptimizationSubmissionResponse(
        optimization_id="abc123",
        optimization_type="blackbox",
        status=OptimizationStatus.pending,
        created_at=datetime.now(tz=UTC),
        username="alice",
        module_name="blackbox",
        optimizer_name="gepa",
    )

    assert resp.name is None
    assert resp.description is None

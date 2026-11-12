"""Tests for the /optimizations response models."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.models.common import ColumnMapping, OptimizationStatus
from core.models.optimizations import (
    BulkCancelRequest,
    BulkCancelResponse,
    BulkCancelSkipped,
    BulkDeleteRequest,
    BulkDeleteResponse,
    BulkDeleteSkipped,
    JobCancelResponse,
    JobDeleteResponse,
    OptimizationCountsResponse,
    OptimizationPayloadResponse,
    OptimizationStatusResponse,
    OptimizationSummaryResponse,
    PaginatedJobsResponse,
)


def _required_base_fields() -> dict:
    """Return the minimum fields _JobResponseBase subclasses require.

    Returns:
        Dict containing the required ``optimization_id``, ``optimization_type``,
        ``status``, and ``created_at`` fields used by every job response.
    """
    return {
        "optimization_id": "abc123",
        "optimization_type": "blackbox",
        "status": OptimizationStatus.success,
        "created_at": datetime.now(tz=UTC),
    }


def test_optimization_status_response_minimal() -> None:
    """Verify OptimizationStatusResponse validates the minimum required base fields."""
    resp = OptimizationStatusResponse.model_validate(_required_base_fields())

    assert resp.optimization_id == "abc123"
    assert resp.optimization_type == "blackbox"
    assert resp.status is OptimizationStatus.success
    assert resp.progress_events == []
    assert resp.logs == []
    assert resp.blackbox_result is None


def test_optimization_status_response_pinned_default() -> None:
    """Verify OptimizationStatusResponse defaults pinned to False."""
    resp = OptimizationStatusResponse.model_validate(_required_base_fields())

    assert resp.pinned is False


def test_optimization_status_response_persists_column_mapping() -> None:
    """Verify OptimizationStatusResponse persists a ColumnMapping value."""
    payload = _required_base_fields()
    payload["column_mapping"] = ColumnMapping(inputs={"q": "q"}, outputs={"a": "a"})
    resp = OptimizationStatusResponse.model_validate(payload)

    assert resp.column_mapping is not None
    assert resp.column_mapping.inputs == {"q": "q"}


def test_summary_response_coerces_legacy_list_column_mapping() -> None:
    """A legacy list-shaped column_mapping coerces to identity dicts, not a failure.

    Regression: older optimizations persisted ``inputs``/``outputs`` as bare
    lists of column names, which failed dict validation and took down the
    entire ``list_jobs`` listing — one malformed row 500'd the whole response.
    """
    payload = _required_base_fields()
    payload["column_mapping"] = {"inputs": ["ticket_text"], "outputs": ["category"]}

    resp = OptimizationSummaryResponse.model_validate(payload)

    assert resp.column_mapping is not None
    assert resp.column_mapping.inputs == {"ticket_text": "ticket_text"}
    assert resp.column_mapping.outputs == {"category": "category"}


def test_summary_response_keeps_dict_column_mapping_untouched() -> None:
    """A well-formed dict column_mapping is passed through unchanged by the coercion."""
    payload = _required_base_fields()
    payload["column_mapping"] = {"inputs": {"question": "q"}, "outputs": {"answer": "a"}}

    resp = OptimizationSummaryResponse.model_validate(payload)

    assert resp.column_mapping is not None
    assert resp.column_mapping.inputs == {"question": "q"}
    assert resp.column_mapping.outputs == {"answer": "a"}


@pytest.mark.parametrize("legacy_state", ["waiting_for_usage", "resuming"])
def test_summary_response_normalizes_internal_recovery_phases(legacy_state: str) -> None:
    """Expose one stable recovering state while retaining the internal phase.

    Args:
        legacy_state: Previously persisted internal state discriminator.
    """
    payload = _required_base_fields()
    payload["recovery"] = {"state": legacy_state, "reason": "continuing"}

    response = OptimizationSummaryResponse.model_validate(payload)

    assert response.recovery == {"state": "recovering", "phase": legacy_state, "reason": "continuing"}


def test_summary_response_handles_unknown_recovery_state_defensively() -> None:
    """Render unknown persisted states as unavailable rather than breaking clients."""
    payload = _required_base_fields()
    payload["recovery"] = {"state": "future_phase"}

    response = OptimizationSummaryResponse.model_validate(payload)

    assert response.recovery == {
        "state": "unavailable",
        "phase": "future_phase",
        "reason": "This run has an unsupported recovery state.",
    }


def test_summary_response_exposes_an_interrupted_stop_as_supported() -> None:
    """Show a run the orphan sweep stopped as interrupted, with a reason that says how to continue."""
    payload = _required_base_fields()
    payload["recovery"] = {"state": "interrupted", "interrupted_budget_generation": 2}

    response = OptimizationSummaryResponse.model_validate(payload)

    assert response.recovery == {
        "state": "interrupted",
        "interrupted_budget_generation": 2,
        "reason": "The run stopped because its worker went away. Resume it to continue in its own budget.",
    }


def test_optimization_summary_response_metric_fields_default_none() -> None:
    """Verify OptimizationSummaryResponse defaults metric fields to None."""
    resp = OptimizationSummaryResponse.model_validate(_required_base_fields())

    assert resp.baseline_test_metric is None
    assert resp.optimized_test_metric is None
    assert resp.metric_improvement is None
    assert resp.progress_count == 0
    assert resp.log_count == 0


def test_paginated_jobs_response_defaults() -> None:
    """Verify PaginatedJobsResponse defaults items to [] and pagination to first page."""
    resp = PaginatedJobsResponse()

    assert resp.items == []
    assert resp.total == 0
    assert resp.limit == 50
    assert resp.offset == 0


def test_paginated_jobs_response_persists_items() -> None:
    """Verify PaginatedJobsResponse stores nested OptimizationSummaryResponse items."""
    item = OptimizationSummaryResponse.model_validate(_required_base_fields())
    resp = PaginatedJobsResponse(items=[item], total=1, limit=10, offset=0)

    assert len(resp.items) == 1
    assert resp.total == 1
    assert resp.limit == 10


def test_optimization_counts_response_defaults_zero() -> None:
    """Verify OptimizationCountsResponse defaults every status counter to 0."""
    counts = OptimizationCountsResponse()

    assert counts.total == 0
    assert counts.pending == 0
    assert counts.validating == 0
    assert counts.running == 0
    assert counts.success == 0
    assert counts.failed == 0
    assert counts.cancelled == 0


def test_job_cancel_response_round_trip() -> None:
    """Verify JobCancelResponse persists optimization_id and status."""
    r = JobCancelResponse(optimization_id="abc", status="cancelled")

    assert r.optimization_id == "abc"
    assert r.status == "cancelled"


def test_job_delete_response_round_trip() -> None:
    """Verify JobDeleteResponse persists optimization_id and deleted flag."""
    r = JobDeleteResponse(optimization_id="abc", deleted=True)

    assert r.optimization_id == "abc"
    assert r.deleted is True


def test_bulk_delete_request_defaults_empty_list() -> None:
    """Verify BulkDeleteRequest defaults optimization_ids to []."""
    r = BulkDeleteRequest()

    assert r.optimization_ids == []


def test_bulk_delete_response_round_trip() -> None:
    """Verify BulkDeleteResponse stores deleted ids and skipped reasons."""
    r = BulkDeleteResponse(
        deleted=["a", "b"],
        skipped=[BulkDeleteSkipped(optimization_id="c", reason="running")],
    )

    assert r.deleted == ["a", "b"]
    assert r.skipped[0].reason == "running"


def test_bulk_cancel_request_defaults_empty_list() -> None:
    """Verify BulkCancelRequest defaults optimization_ids to []."""
    r = BulkCancelRequest()

    assert r.optimization_ids == []


def test_bulk_cancel_response_round_trip() -> None:
    """Verify BulkCancelResponse stores cancelled ids and skipped reasons."""
    r = BulkCancelResponse(
        cancelled=["a"],
        skipped=[BulkCancelSkipped(optimization_id="b", reason="terminal")],
    )

    assert r.cancelled == ["a"]
    assert r.skipped[0].optimization_id == "b"


def test_optimization_payload_response_round_trip() -> None:
    """Verify OptimizationPayloadResponse persists id/type/payload."""
    r = OptimizationPayloadResponse(
        optimization_id="abc",
        optimization_type="blackbox",
        payload={"signature_code": "..."},
    )

    assert r.optimization_id == "abc"
    assert r.optimization_type == "blackbox"
    assert r.payload == {"signature_code": "..."}

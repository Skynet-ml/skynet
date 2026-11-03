"""Tests for the worker's billing hooks at run completion.

Covers ``_debit_run_cents`` (the local balance-ledger debit) and
``_stamp_billing_outcome`` (the cost receipt on the persisted result). Both
must never affect job status. The debit is a no-op unless the store exposes a
SQL engine, the caller is known, and the run reported token usage — Stripe
configuration is deliberately not required, since the ledger is the balance
source of truth even on a key-less deploy.
"""

from __future__ import annotations

import queue
from typing import Any, cast
from unittest.mock import patch

import pytest

from core.billing.pricing import ModelUsage, fallback_priced_usages
from core.config import settings
from core.constants import PAYLOAD_OVERVIEW_MODEL_NAME, PAYLOAD_OVERVIEW_NAME, PAYLOAD_OVERVIEW_USERNAME
from core.storage import JobStore
from core.worker.constants import EVENT_ERROR, EVENT_PROGRESS
from core.worker.engine import BackgroundWorker, _legacy_settlement_key


class _Store:
    """Stand-in job store; ``engine`` is present only when ``engine`` is passed."""

    def __init__(self, engine: object | None = None) -> None:
        """Optionally expose a SQL engine, mirroring RemoteDBJobStore.

        Args:
            engine: Engine sentinel to expose, or ``None`` to omit the attribute
                entirely (as a legacy/in-memory store would).
        """
        if engine is not None:
            self.engine = engine
        self.updates: list[dict[str, Any]] = []

    def update_job(self, optimization_id: str, **fields: Any) -> None:
        """Capture a re-persist so the billing-outcome stamp can be asserted."""
        self.updates.append({"id": optimization_id, **fields})


def _worker(store: _Store) -> BackgroundWorker:
    """Build a worker over ``store`` without starting any threads.

    Args:
        store: The stand-in store to bind.

    Returns:
        An unstarted ``BackgroundWorker``.
    """
    return BackgroundWorker(job_store=cast(JobStore, store), num_workers=1, poll_interval=1.0)


def test_debit_hook_charges_cents_for_successful_run() -> None:
    """With an engine, a known caller, and tokens present, the run is debited."""
    engine = object()
    worker = _worker(_Store(engine=engine))
    with patch("core.worker.engine.StripeBillingService") as billing_cls:
        worker._debit_run_cents(
            "u@x.com", {"total_tokens": 5000}, run_name="sentiment v3", model="m1", optimization_id="legacy-job"
        )
    billing_cls.assert_called_once_with(engine=engine)
    # No usage_by_model on the result → legacy fallback prices the total on the
    # run's model, attributed to input.
    billing_cls.return_value.debit_run.assert_called_once_with(
        "u@x.com",
        [ModelUsage(model="m1", input_tokens=5000, output_tokens=0)],
        model="m1",
        description="sentiment v3",
        token_source="managed",
        optimization_id="legacy-job",
    )


def test_debit_hook_prices_per_model_usage_when_present() -> None:
    """When the result carries usage_by_model, the worker charges from that split."""
    worker = _worker(_Store(engine=object()))
    result = {
        "total_tokens": 999,  # ignored in favour of the per-model breakdown
        "usage_by_model": [
            {"model": "openai/gpt-4o-mini", "input_tokens": 1000, "output_tokens": 200},
            {"model": "anthropic/claude-opus-4-8", "input_tokens": 300, "output_tokens": 50},
        ],
    }
    with patch("core.worker.engine.StripeBillingService") as billing_cls:
        worker._debit_run_cents("u@x.com", result, run_name="r", model="openai/gpt-4o-mini")
    billing_cls.return_value.debit_run.assert_called_once_with(
        "u@x.com",
        [
            ModelUsage(model="openai/gpt-4o-mini", input_tokens=1000, output_tokens=200),
            ModelUsage(model="anthropic/claude-opus-4-8", input_tokens=300, output_tokens=50),
        ],
        model="openai/gpt-4o-mini",
        description="r",
        token_source="managed",
    )


def test_debit_hook_passes_byok_token_source() -> None:
    """A BYOK run debits with token_source='byok' and no model charge."""
    engine = object()
    worker = _worker(_Store(engine=engine))
    with patch("core.worker.engine.StripeBillingService") as billing_cls:
        worker._debit_run_cents("u@x.com", {"total_tokens": 5000}, run_name="r", model="m1", token_source="byok")
    billing_cls.return_value.debit_run.assert_called_once_with(
        "u@x.com",
        [ModelUsage(model="m1", input_tokens=5000, output_tokens=0)],
        model="m1",
        description="r",
        token_source="byok",
    )


def test_debit_hook_runs_without_stripe_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """The local debit fires even when Stripe is unconfigured (ledger is local truth)."""
    monkeypatch.setattr(settings, "stripe_secret_key", None)
    engine = object()
    worker = _worker(_Store(engine=engine))
    with patch("core.worker.engine.StripeBillingService") as billing_cls:
        worker._debit_run_cents("u@x.com", {"total_tokens": 5000}, run_name="r", model=None)
    billing_cls.return_value.debit_run.assert_called_once()


def test_debit_hook_noop_without_engine() -> None:
    """A store without a SQL engine (legacy/in-memory) debits nothing."""
    worker = _worker(_Store(engine=None))
    with patch("core.worker.engine.StripeBillingService") as billing_cls:
        worker._debit_run_cents("u@x.com", {"total_tokens": 5000}, run_name="r", model=None)
    billing_cls.assert_not_called()


def test_debit_hook_noop_without_token_usage() -> None:
    """A run that reported no token total debits nothing."""
    worker = _worker(_Store(engine=object()))
    with patch("core.worker.engine.StripeBillingService") as billing_cls:
        worker._debit_run_cents("u@x.com", {"total_tokens": None}, run_name="r", model=None)
        worker._debit_run_cents("u@x.com", {}, run_name="r", model=None)
        worker._debit_run_cents("u@x.com", None, run_name="r", model=None)
    billing_cls.assert_not_called()


def test_debit_hook_swallows_failures() -> None:
    """A debit failure never propagates to the worker (job status is untouched)."""
    worker = _worker(_Store(engine=object()))
    with patch("core.worker.engine.StripeBillingService") as billing_cls:
        billing_cls.return_value.debit_run.side_effect = RuntimeError("db down")
        worker._debit_run_cents("u@x.com", {"total_tokens": 5000}, run_name="r", model=None)


def test_stamp_billing_records_billed_outcome() -> None:
    """A billed run stamps the charged cents as the cost receipt."""
    store = _Store(engine=object())
    worker = _worker(store)
    result: dict[str, Any] = {"total_tokens": 5000}
    worker._stamp_billing_outcome("opt-1", result, billed=5)
    assert result["details"]["billing"] == {"outcome": "billed", "cents": 5}
    assert store.updates == [{"id": "opt-1", "result": result}]


def test_stamp_billing_preserves_existing_details() -> None:
    """The stamp merges into a result's existing details bag, never replaces it."""
    store = _Store(engine=object())
    worker = _worker(store)
    result: dict[str, Any] = {"total_tokens": 5000, "details": {"existing": True}}
    worker._stamp_billing_outcome("opt-1", result, billed=5)
    assert result["details"]["billing"] == {"outcome": "billed", "cents": 5}
    assert result["details"]["existing"] is True


def test_stamp_billing_records_estimate_for_reconciliation() -> None:
    """A run carrying a projected bracket echoes it for the estimate-vs-actual line."""
    store = _Store(engine=object())
    worker = _worker(store)
    result: dict[str, Any] = {"total_tokens": 5000}
    worker._stamp_billing_outcome("opt-1", result, billed=7, estimated_low=4, estimated_high=12)
    assert result["details"]["billing"] == {
        "outcome": "billed",
        "cents": 7,
        "estimated_low": 4,
        "estimated_high": 12,
    }


def test_stamp_billing_omits_estimate_when_partial() -> None:
    """A half-present estimate is dropped — reconciliation needs both bounds."""
    store = _Store(engine=object())
    worker = _worker(store)
    result: dict[str, Any] = {"total_tokens": 5000}
    worker._stamp_billing_outcome("opt-1", result, billed=7, estimated_low=4, estimated_high=None)
    assert result["details"]["billing"] == {"outcome": "billed", "cents": 7}


def test_stamp_billing_noop_when_nothing_charged() -> None:
    """A free-grant run that cost nothing stamps nothing."""
    store = _Store(engine=object())
    worker = _worker(store)
    result: dict[str, Any] = {"total_tokens": 0}
    worker._stamp_billing_outcome("opt-1", result, billed=0)
    assert "details" not in result
    assert store.updates == []


def test_stamp_billing_skips_grid_results() -> None:
    """A grid envelope has no per-run billing receipt — skip it."""
    store = _Store(engine=object())
    worker = _worker(store)
    result: dict[str, Any] = {"pair_results": [], "total_tokens": 5000}
    worker._stamp_billing_outcome("opt-1", result, billed=5)
    assert "details" not in result
    assert store.updates == []


def test_stamp_billing_swallows_persist_failures() -> None:
    """A re-persist failure never propagates to the worker (job status untouched)."""
    store = _Store(engine=object())
    worker = _worker(store)
    with patch.object(store, "update_job", side_effect=RuntimeError("db down")):
        worker._stamp_billing_outcome("opt-1", {"total_tokens": 5000}, billed=5)


def test_debit_hook_retries_at_fallback_price_after_failure() -> None:
    """A failed exact debit is retried at the fallback frontier price instead of billing zero."""
    worker = _worker(_Store(engine=object()))
    usages = [ModelUsage(model="m1", input_tokens=5000, output_tokens=0)]
    with patch("core.worker.engine.StripeBillingService") as billing_cls:
        billing_cls.return_value.debit_run.side_effect = [RuntimeError("price lookup failed"), 42]
        charged = worker._debit_run_cents(
            "u@x.com", {"total_tokens": 5000}, run_name="r", model="m1", settlement_key="legacy:j:g1"
        )
    assert charged == 42
    retry = billing_cls.return_value.debit_run.call_args_list[1]
    assert retry.args == ("u@x.com", fallback_priced_usages(usages))
    assert retry.kwargs["settlement_key"] == "legacy:j:g1"


def test_legacy_settlement_key_is_per_execution_leg() -> None:
    """Each claim generation gets its own key; a store without generations gets none."""
    assert _legacy_settlement_key("job-1", 3) == "legacy:job-1:g3"
    assert _legacy_settlement_key("job-1", 3) != _legacy_settlement_key("job-1", 4)
    assert _legacy_settlement_key("job-1", None) is None


_OVERVIEW = {PAYLOAD_OVERVIEW_USERNAME: "u@x.com", PAYLOAD_OVERVIEW_NAME: "run", PAYLOAD_OVERVIEW_MODEL_NAME: "m1"}


def test_bill_legacy_leg_bills_last_usage_snapshot_when_no_result() -> None:
    """A failed or stopped leg with no result is billed from the child's last usage snapshot."""
    worker = _worker(_Store(engine=object()))
    tracker = {"usage_by_model": [{"model": "m1", "input_tokens": 700, "output_tokens": 30}]}
    with patch.object(worker, "_debit_run_cents", return_value=9) as debit:
        assert worker._bill_legacy_leg("job-1", _OVERVIEW, None, tracker, generation=2) == 9
    args, kwargs = debit.call_args
    assert args == ("u@x.com", {"usage_by_model": tracker["usage_by_model"]})
    assert kwargs["settlement_key"] == "legacy:job-1:g2"
    assert kwargs["optimization_id"] == "job-1"


def test_bill_legacy_leg_prefers_result_usage_and_skips_when_empty() -> None:
    """A delivered result's usage wins over the snapshot; nothing measured bills nothing."""
    worker = _worker(_Store(engine=object()))
    result = {"usage_by_model": [{"model": "m1", "input_tokens": 900, "output_tokens": 90}]}
    tracker = {"usage_by_model": [{"model": "m1", "input_tokens": 1, "output_tokens": 1}]}
    with patch.object(worker, "_debit_run_cents", return_value=5) as debit:
        worker._bill_legacy_leg("child", _OVERVIEW, result, tracker, generation=1, commitment_job_id="parent")
        assert worker._bill_legacy_leg("job-2", _OVERVIEW, None, {}, generation=1) == 0
    debit.assert_called_once()
    assert debit.call_args.args[1] is result
    assert debit.call_args.kwargs["optimization_id"] == "parent"


class _ProgressStore(_Store):
    """Store double that also accepts progress events."""

    def record_progress(self, optimization_id: str, event: Any, metrics: Any) -> None:
        """Accept and drop a progress event."""


def test_drain_tracks_latest_usage_snapshot_from_child_events() -> None:
    """Progress and error events update the usage tracker; events without usage leave it alone."""
    worker = _worker(_ProgressStore())
    events: queue.Queue[dict[str, Any]] = queue.Queue()
    first = [{"model": "m1", "input_tokens": 10, "output_tokens": 1}]
    latest = [{"model": "m1", "input_tokens": 50, "output_tokens": 5}]
    events.put({"type": EVENT_PROGRESS, "event": "e", "metrics": {}, "usage_by_model": first})
    events.put({"type": EVENT_PROGRESS, "event": "e", "metrics": {}, "usage_by_model": latest})
    events.put({"type": EVENT_ERROR, "error": "boom", "usage_by_model": []})
    tracker: dict[str, Any] = {}
    worker._drain_subprocess_events("job-1", events, usage_tracker=tracker)
    assert tracker == {"usage_by_model": latest}

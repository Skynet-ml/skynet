"""Verify the budget pace that prices a run's next round and stops it when the budget cannot fund one."""

from __future__ import annotations

import queue
import shutil
from decimal import Decimal
from types import SimpleNamespace

import pytest

from core.billing.budgets import BudgetService
from core.constants import TQDM_N_KEY, TQDM_TOTAL_KEY
from core.service_gateway.optimization.trajectory import GEPA_STATE_FILENAME
from core.storage.tests.test_remote_jobstore import SQLiteJobStore, _fund_checkpoint
from core.worker.budget_probe import (
    CALIBRATION_CALLS,
    PACE_CALIBRATING_KEY,
    PACE_EVALS_LEFT_KEY,
    PACE_ITERATIONS_LEFT_KEY,
    STOP_REASON_BUDGET_REACHED,
    budget_pace,
    calls_per_iteration,
    done_calls_from_progress,
    planned_calls_from_progress,
)
from core.worker.constants import EVENT_PROGRESS
from core.worker.engine import BackgroundWorker

from .test_checkpoint_helpers import _PAYLOAD, _CheckpointStore, _state


def _snapshot(run_spent: str, available: str, *, uncapped: bool = False) -> SimpleNamespace:
    """Model the budget fields the pace reads.

    Args:
        run_spent: Cents settled in the run phase.
        available: Cents the budget can still admit.
        uncapped: Whether the budget has no spending limit.

    Returns:
        A stand-in for ``BudgetSnapshot``.
    """
    return SimpleNamespace(run_spent_cents=Decimal(run_spent), available_cents=Decimal(available), uncapped=uncapped)


def test_progress_counts_come_only_from_positive_integer_bar_fields() -> None:
    """Accept the optimizer bar's integer total and count and reject every other shape."""
    assert planned_calls_from_progress({TQDM_TOTAL_KEY: 230}) == 230
    assert planned_calls_from_progress({}) is None
    assert planned_calls_from_progress({TQDM_TOTAL_KEY: 0}) is None
    assert planned_calls_from_progress({TQDM_TOTAL_KEY: True}) is None
    assert planned_calls_from_progress({TQDM_TOTAL_KEY: "230"}) is None
    assert done_calls_from_progress({TQDM_TOTAL_KEY: 230, TQDM_N_KEY: 12}) == 12
    assert done_calls_from_progress({TQDM_N_KEY: 12}) is None
    assert done_calls_from_progress({TQDM_TOTAL_KEY: 230, TQDM_N_KEY: -1}) is None


def test_iteration_size_comes_from_the_saved_state() -> None:
    """Average the saved state's metric calls over its iterations, rounding up."""
    assert calls_per_iteration({"n": 3, "metric_calls": 23}) == 8
    assert calls_per_iteration({"n": 0, "metric_calls": 5}) is None
    assert calls_per_iteration({"mtime": None, "n": 0}) is None
    assert calls_per_iteration(None) is None


def test_calibration_never_stops_and_reports_no_estimate() -> None:
    """Before enough evaluations settle the pace is unknown, so the run keeps going."""
    pace = budget_pace(_snapshot("50", "0"), done_calls=CALIBRATION_CALLS - 1, planned_calls=200, iteration_calls=None)
    assert pace.calibrating is True
    assert pace.stop is False
    assert pace.evals_left is None


def test_pace_counts_what_the_budget_left_can_fund_within_the_run_cap() -> None:
    """Divide the budget left by the cost per evaluation and never promise more than the cap allows."""
    pace = budget_pace(_snapshot("10", "40"), done_calls=20, planned_calls=200, iteration_calls=8)
    assert pace.cents_per_call == Decimal("0.5")
    assert pace.evals_left == 80
    assert pace.iterations_left == 10
    assert pace.stop is False

    capped = budget_pace(_snapshot("10", "40"), done_calls=20, planned_calls=50, iteration_calls=8)
    assert capped.evals_left == 30
    assert capped.iterations_left == 3


def test_the_run_stops_once_the_next_round_does_not_fit_with_its_margin() -> None:
    """A round of eight calls at half a cent needs six cents with the 1.5x margin."""
    assert budget_pace(_snapshot("10", "6"), done_calls=20, planned_calls=200, iteration_calls=8).stop is False
    assert budget_pace(_snapshot("10", "5.9"), done_calls=20, planned_calls=200, iteration_calls=8).stop is True
    # Without a saved iteration the calibration sample stands in for one round.
    assert budget_pace(_snapshot("10", "7.4"), done_calls=20, planned_calls=200, iteration_calls=None).stop is True
    assert budget_pace(_snapshot("10", "5.9"), done_calls=20, planned_calls=200, iteration_calls=8).iterations_left == 1


def test_an_uncapped_or_free_run_is_never_stopped_by_its_pace() -> None:
    """No limit means no budget stop; a run that costs nothing has nothing to run out of."""
    uncapped = budget_pace(_snapshot("10", "1", uncapped=True), done_calls=20, planned_calls=200, iteration_calls=8)
    assert uncapped.stop is False
    free = budget_pace(_snapshot("0", "0"), done_calls=20, planned_calls=200, iteration_calls=8)
    assert free.stop is False
    assert free.evals_left == 180


def _settle_run_spend(budgets: BudgetService, budget_id: str, cents: int) -> None:
    """Settle one run-phase operation for ``cents`` on the funded fixture budget.

    Args:
        budgets: Budget service bound to the fixture store.
        budget_id: The funded fixture budget.
        cents: Whole cents the operation actually cost.
    """
    operation = budgets.reserve(
        budget_id,
        "resume-owner",
        operation_key=f"probe-{cents}",
        generation=0,
        phase="run",
        cost_kind="sandbox",
        request_fingerprint="fixture-session",
        price_snapshot={"version": "fixture"},
        max_cents=cents,
    )
    budgets.mark_dispatched(operation.id, "resume-owner", "fixture-session")
    budgets.settle(operation.id, "resume-owner", evidence_key="closed", actual_cents=cents, evidence={})


def _gateway(budgets: BudgetService, budget_id: str) -> SimpleNamespace:
    """Model the trusted parent transport's runtime handle on the fixture budget."""
    return SimpleNamespace(runtime=SimpleNamespace(service=budgets, budget_id=budget_id, username="resume-owner"))


def test_a_short_budget_closes_admission_as_a_clean_budget_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Close admission with budget_reached so the optimizer's next paid call unwinds and keeps its best version.

    Args:
        monkeypatch: Fixture binding the checkpoint's immutable runtime profile.
    """
    store = SQLiteJobStore()
    store.create_job("short")
    store.update_job("short", status="running")
    budgets = _fund_checkpoint(store, "short", monkeypatch)
    budget_id = store.get_job("short")["execution_budget_id"]
    _settle_run_spend(budgets, budget_id, 15)
    tracker: dict = {"planned_calls": 230, -1: {"mtime": 1.0, "n": 3, "metric_calls": 15}}

    metrics = BackgroundWorker(job_store=store)._pace_run_budget("short", _gateway(budgets, budget_id), tracker, 15)

    budget = budgets.get(budget_id, "resume-owner")
    assert budget.state == "closed"
    assert budget.blocked_reason == STOP_REASON_BUDGET_REACHED
    assert store.get_job("short")["status"] == "running"
    assert metrics == {PACE_CALIBRATING_KEY: False, PACE_EVALS_LEFT_KEY: 5, PACE_ITERATIONS_LEFT_KEY: 1}


def test_a_budget_that_fits_keeps_running_and_prices_each_count_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Leave a run alone while its next round fits, and skip the budget read for a count already priced.

    Args:
        monkeypatch: Fixture binding the checkpoint's immutable runtime profile.
    """
    store = SQLiteJobStore()
    store.create_job("fits")
    store.update_job("fits", status="running")
    budgets = _fund_checkpoint(store, "fits", monkeypatch)
    budget_id = store.get_job("fits")["execution_budget_id"]
    _settle_run_spend(budgets, budget_id, 1)
    tracker: dict = {"planned_calls": 230}
    worker = BackgroundWorker(job_store=store)

    metrics = worker._pace_run_budget("fits", _gateway(budgets, budget_id), tracker, 20)
    assert budgets.get(budget_id, "resume-owner").state == "attached"
    assert metrics[PACE_EVALS_LEFT_KEY] == 210
    assert metrics[PACE_ITERATIONS_LEFT_KEY] is None

    unreachable = SimpleNamespace(runtime=SimpleNamespace(service=None, budget_id=budget_id, username="resume-owner"))
    assert worker._pace_run_budget("fits", unreachable, tracker, 20) == metrics


def test_a_run_without_a_spending_limit_is_never_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Leave an uncapped run alone however little of its stored total is left.

    Args:
        monkeypatch: Fixture binding the checkpoint's immutable runtime profile.
    """
    store = SQLiteJobStore()
    store.create_job("open")
    store.update_job("open", status="running")
    budgets = _fund_checkpoint(store, "open", monkeypatch)
    budget_id = store.get_job("open")["execution_budget_id"]
    budget = budgets.get(budget_id, "resume-owner")
    budgets.update_total(budget_id, "resume-owner", 20, expected_revision=budget.revision, uncapped=True)
    _settle_run_spend(budgets, budget_id, 19)

    BackgroundWorker(job_store=store)._pace_run_budget("open", _gateway(budgets, budget_id), {}, 19)

    assert budgets.get(budget_id, "resume-owner").state == "attached"


def test_progress_and_checkpoint_feed_the_pace_and_the_live_estimate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bar's total lands in the tracker and its tick is recorded with the live estimate beside it.

    Args:
        monkeypatch: Fixture binding the checkpoint's immutable runtime profile.
    """
    store = SQLiteJobStore()
    store.create_job("fed")
    store.update_job("fed", status="running")
    budgets = _fund_checkpoint(store, "fed", monkeypatch)
    budget_id = store.get_job("fed")["execution_budget_id"]
    worker = BackgroundWorker(job_store=store)
    tracker: dict = {"payload": _PAYLOAD}
    events: queue.Queue = queue.Queue()
    events.put({"type": EVENT_PROGRESS, "event": "optimizer", "metrics": {TQDM_TOTAL_KEY: 230, TQDM_N_KEY: 4}})
    worker._drain_subprocess_events(
        "fed", events, checkpoint_tracker=tracker, budget_gateway=_gateway(budgets, budget_id)
    )
    assert tracker["planned_calls"] == 230
    latest = store.get_job("fed")["latest_metrics"]
    assert latest[TQDM_N_KEY] == 4
    assert latest[PACE_CALIBRATING_KEY] is True


def test_saved_state_feeds_the_iteration_size() -> None:
    """The saved state's metric calls land in the tracker the pace reads its round size from."""
    store = _CheckpointStore()
    worker = BackgroundWorker(job_store=store)
    tracker: dict = {"payload": _PAYLOAD}
    base = worker._prepare_gepa_dir("fed")
    try:
        (base / "gepa").mkdir(exist_ok=True)
        (base / "gepa" / GEPA_STATE_FILENAME).write_bytes(_state(2))
        worker._persist_gepa_checkpoint("fed", base, tracker)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    assert tracker[-1]["metric_calls"] == store.manifests[-1]["metric_calls"] == 10
    assert calls_per_iteration(tracker[-1]) == 5


def test_uncapped_run_resumes_admission_past_its_stored_total(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reopen an uncapped run that spent past its stored total as long as the account can still fund it.

    Args:
        monkeypatch: Fixture binding the checkpoint's immutable runtime profile.
    """
    store = SQLiteJobStore()
    store.create_job("dry")
    store.update_job("dry", status="running")
    budgets = _fund_checkpoint(store, "dry", monkeypatch)
    budget_id = store.get_job("dry")["execution_budget_id"]
    budget = budgets.get(budget_id, "resume-owner")
    budgets.update_total(budget_id, "resume-owner", 20, expected_revision=budget.revision, uncapped=True)
    _settle_run_spend(budgets, budget_id, 25)
    budgets.stop_admission(budget_id, "resume-owner", reason="budget_reached")
    store.update_job("dry", status="stopped", stop_reason="budget_reached")
    budget = budgets.get(budget_id, "resume-owner")
    assert budget.run_spent_cents == 25
    assert budget.available_cents == 25

    assert store.requeue_for_resume("dry", bump_attempts=False, expected_generation=0, budget_service=budgets) == 0

    assert budgets.get(budget_id, "resume-owner").state == "attached"
    assert store.get_job("dry")["status"] == "pending"

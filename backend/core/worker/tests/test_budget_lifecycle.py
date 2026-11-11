"""Verify normal budget termination, checkpoint fidelity, and stale publication fences."""

from __future__ import annotations

import pickle
import queue
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from gepa.core.state import GEPAState, ValsetEvaluation

from core.billing.runtime import UsagePendingError
from core.billing.signals import BudgetReached, BudgetStopLatch
from core.constants import PROGRESS_CANDIDATE
from core.service_gateway.optimization.incumbent import completed_gepa_result
from core.storage.remote import ORPHANED_RUN_MESSAGE
from core.storage.tests.test_remote_jobstore import SQLiteJobStore, _fund_checkpoint
from core.worker.checkpoint_compat import (
    CheckpointCompatibilityError,
    checkpoint_incumbent,
    checkpoint_manifest,
    evaluated_incumbent_from_progress,
    validate_checkpoint,
)
from core.worker.constants import EVENT_ERROR, EVENT_TERMINAL
from core.worker.engine import BackgroundWorker
from core.worker.subprocess_runner import run_service_in_subprocess

from .mocks import REAL_RUN_PAYLOAD, fake_dspy_service, fake_service_registry


def _checkpoint(path: Path) -> bytes:
    """Write a real upstream state with a completed seed validation and retained counters.

    Args:
        path: Private fixture workspace.

    Returns:
        The exact bytes produced by upstream's atomic serializer.
    """
    state = GEPAState({"predict": "seed instructions"}, ValsetEvaluation({0: "a", 1: "b"}, {0: 1.0, 1: 0.0}))
    state.i = 4
    state.total_num_evals = 23
    state.num_full_ds_evals = 2
    state.save(str(path))
    return (path / "gepa_state.bin").read_bytes()


def test_budget_latch_escapes_exception_handlers_without_sharing_result_payloads() -> None:
    """Keep one stopped decision while isolating mutable result evidence per lane."""
    latch = BudgetStopLatch()
    assert latch.stopped is False
    latch.check()
    first = latch.trip("covered work cannot fit")
    first.result = "lane one"
    with pytest.raises(BudgetReached) as second:
        try:
            latch.check()
        except Exception:
            pytest.fail("The optimizer must not turn budget exhaustion into a scored failure.")
    assert str(second.value) == str(first)
    assert second.value.result is None
    assert latch.stopped


@pytest.mark.parametrize("evaluated", [False, True])
def test_subprocess_emits_budget_terminal_without_error(evaluated: bool) -> None:
    """Preserve evaluated and no-result budget outcomes outside the error channel.

    Args:
        evaluated: Whether a completed optimizer result exists.
    """
    events: queue.Queue = queue.Queue()
    service = fake_dspy_service()
    stopped = BudgetReached()
    stopped.result = {"best_candidate": "tested", "optimized_test_metric": None} if evaluated else None
    stopped.evidence = {"final_evaluation_completed": False, "final_evaluation_reason": "budget_reached"}
    service.run.side_effect = stopped
    with (
        patch("core.worker.subprocess_runner.DspyService", return_value=service),
        patch("core.worker.subprocess_runner.ServiceRegistry", return_value=fake_service_registry()),
    ):
        run_service_in_subprocess(REAL_RUN_PAYLOAD, "job", events, "spawn")
    messages = list(events.queue)
    assert not any(event["type"] == EVENT_ERROR for event in messages)
    terminal = next(event["outcome"] for event in messages if event["type"] == EVENT_TERMINAL)
    assert terminal["result_availability"] == ("evaluated" if evaluated else "none")
    assert terminal["result"] == stopped.result
    assert terminal["evidence"]["final_evaluation_completed"] is False


def test_manifest_retains_upstream_counters_and_requires_exact_evidence(tmp_path: Path) -> None:
    """Restore the real state and reject changed tasks, corrupt bytes, and unsupported schema.

    Args:
        tmp_path: Private checkpoint directory.
    """
    payload = {"optimizer_name": "GEPA", "dataset": [{"input": "a"}], "seed": 11}
    data = _checkpoint(tmp_path)
    manifest = checkpoint_manifest(data, payload, "fixture-v1")
    assert manifest["iteration"] == 4
    assert manifest["metric_calls"] == 23
    validate_checkpoint(data, manifest, payload, "fixture-v1")
    recovered = completed_gepa_result(str(tmp_path), seed=11)
    assert recovered.total_metric_calls == 23
    assert recovered.best_candidate == {"predict": "seed instructions"}
    assert recovered.val_aggregate_scores[recovered.best_idx] == 0.5
    for changed_data, changed_manifest, changed_payload in [
        (data + b"corruption", manifest, payload),
        (data, manifest, {**payload, "dataset": [{"input": "changed"}]}),
        (data, {**manifest, "state_schema": 6}, payload),
        (data, {**manifest, "source_sha256": "changed-adapter"}, payload),
        (data, None, payload),
    ]:
        with pytest.raises(CheckpointCompatibilityError):
            validate_checkpoint(changed_data, changed_manifest, changed_payload, "fixture-v1")
    old = pickle.loads(data)
    old["validation_schema_version"] = 6
    with pytest.raises(CheckpointCompatibilityError):
        checkpoint_manifest(pickle.dumps(old), payload, "fixture-v1")


def test_checkpoint_incumbent_accepts_only_finite_completed_candidate_events() -> None:
    """Reject malformed, unevaluated, and non-finite progress as recovery results."""
    payload = {"optimizer_name": "GEPA", "split_fractions": {"train": 0.8, "val": 0.0, "test": 0.2}}
    metrics = {
        "candidate_id": "1",
        "score": 0.8,
        "prompt": {"predict": "improved"},
        "per_example": [{"id": "0", "score": 0.8}],
    }
    incumbent = evaluated_incumbent_from_progress(PROGRESS_CANDIDATE, metrics, payload)
    assert incumbent is not None
    assert incumbent["selection_scope"] == "training"
    assert checkpoint_incumbent({"evaluated_incumbent": incumbent}) == incumbent
    assert evaluated_incumbent_from_progress(PROGRESS_CANDIDATE, {**metrics, "score": float("nan")}, payload) is None
    assert evaluated_incumbent_from_progress(PROGRESS_CANDIDATE, {**metrics, "per_example": []}, payload) is None
    assert checkpoint_incumbent({"evaluated_incumbent": {**incumbent, "candidate": {"predict": 3}}}) is None


def test_orphaned_run_stays_failed() -> None:
    """Fail an interrupted run and leave any restart to the user."""
    store = SQLiteJobStore()
    store.create_job("no-checkpoint")
    store.update_job("no-checkpoint", payload={"optimizer_name": "GEPA"})
    store.update_job("no-checkpoint", status="running")
    assert store.recover_orphaned_jobs() == 1
    job = store.get_job("no-checkpoint")
    assert job["status"] == "failed"
    assert job["stop_reason"] == "interrupted"
    assert job["message"] == ORPHANED_RUN_MESSAGE
    assert job["recovery"] is None


def test_late_generation_cannot_publish_or_override_cancelled_job() -> None:
    """Fence both terminal writes and progress against a replacement or cancellation."""
    store = SQLiteJobStore()
    store.create_job("fenced")
    store.update_job("fenced", status="running", execution_generation=2)
    assert not store.update_job_if_status("fenced", ("running",), expected_generation=1, status="success")
    assert not store.record_progress_for_generation(
        "fenced", "candidate", {"score": 1}, source_optimization_id="fenced", generation=1
    )
    assert store.record_progress_for_generation(
        "fenced", "candidate", {"score": 0.5}, source_optimization_id="fenced", generation=2
    )
    store.update_job("fenced", status="cancelled")
    assert not store.record_progress_for_generation(
        "fenced", "candidate", {"score": 1}, source_optimization_id="fenced", generation=2
    )
    assert store.get_job("fenced")["latest_metrics"] == {"score": 0.5}


def test_checkpoint_and_pair_publication_reject_obsolete_generation() -> None:
    """Prevent an interrupted worker from replacing the active recovery snapshot."""
    store = SQLiteJobStore()
    store.create_job("checkpoint-fence")
    store.update_job("checkpoint-fence", status="running", execution_generation=2)
    assert store.save_gepa_checkpoint("checkpoint-fence", b"current", 2, expected_generation=2)
    assert not store.save_gepa_checkpoint("checkpoint-fence", b"obsolete", 3, expected_generation=1)
    assert store.get_gepa_checkpoint("checkpoint-fence").data == b"current"
    assert store.save_grid_pair_result("checkpoint-fence", 0, {"score": 0.5}, expected_generation=2)
    assert not store.save_grid_pair_result("checkpoint-fence", 0, {"score": 1}, expected_generation=1)
    assert store.get_grid_pair_results("checkpoint-fence")[0] == {"score": 0.5}


def test_resume_waits_for_dispatched_usage_then_retains_cumulative_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fence old work, wait for its usage, and resume the same funded checkpoint.

    Args:
        monkeypatch: Fixture binding the checkpoint's immutable runtime profile.
    """
    store = SQLiteJobStore()
    store.create_job("recovery-pending")
    store.update_job("recovery-pending", status="failed")
    budgets = _fund_checkpoint(store, "recovery-pending", monkeypatch)
    budget_id = store.get_job("recovery-pending")["execution_budget_id"]
    operation = budgets.reserve(
        budget_id,
        "resume-owner",
        operation_key="interrupted",
        generation=0,
        phase="run",
        cost_kind="model",
        request_fingerprint="fixture",
        price_snapshot={"version": "fixture"},
        max_cents=3,
    )
    budgets.mark_dispatched(operation.id, "resume-owner", "fixture-request")
    assert store.requeue_for_resume("recovery-pending", expected_generation=0, budget_service=budgets) is None
    waiting = store.get_job("recovery-pending")
    assert waiting["recovery"]["state"] == "recovering"
    assert waiting["recovery"]["phase"] == "waiting_for_usage"
    assert waiting["execution_generation"] == 1
    budgets.settle(
        operation.id,
        "resume-owner",
        evidence_key="confirmed",
        actual_cents=2,
        evidence={"usage": "authoritative-fixture"},
    )
    assert store.requeue_for_resume("recovery-pending", expected_generation=1, budget_service=budgets) == 1
    resumed = store.get_job("recovery-pending")
    assert resumed["execution_budget_id"] == budget_id
    assert resumed["execution_budget_generation"] == 1
    assert resumed["execution_generation"] == 2
    assert resumed["recovery"] is None
    assert budgets.get(budget_id, "resume-owner").run_spent_cents == 2


def test_pause_keeps_admission_closed_until_explicit_compatible_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reopen only the same funded checkpoint after user-controlled pause.

    Args:
        monkeypatch: Fixture binding the checkpoint's immutable runtime profile.
    """
    store = SQLiteJobStore()
    store.create_job("paused")
    store.update_job("paused", status="paused")
    budgets = _fund_checkpoint(store, "paused", monkeypatch)
    budget_id = store.get_job("paused")["execution_budget_id"]
    budgets.stop_admission(budget_id, "resume-owner", reason="user_paused")
    assert store.recover_orphaned_jobs() == 0
    assert budgets.get(budget_id, "resume-owner").state == "closed"
    assert store.requeue_for_resume("paused", bump_attempts=False, expected_generation=0, budget_service=budgets) == 0
    assert budgets.get(budget_id, "resume-owner").state == "attached"
    assert store.get_job("paused")["execution_budget_id"] == budget_id


def test_recovery_fence_rolls_back_with_job_when_publication_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep budget and job generations aligned when recovery fails before the shared commit.

    Args:
        monkeypatch: Fixture binding the runtime profile and injected failure.
    """
    store = SQLiteJobStore()
    store.create_job("recovery-rollback")
    store.update_job("recovery-rollback", status="failed")
    budgets = _fund_checkpoint(store, "recovery-rollback", monkeypatch)
    budget_id = store.get_job("recovery-rollback")["execution_budget_id"]
    reservation = budgets.reserve(
        budget_id,
        "resume-owner",
        operation_key="undispatched",
        generation=0,
        phase="run",
        cost_kind="model",
        request_fingerprint="fixture",
        price_snapshot={"version": "fixture"},
        max_cents=3,
    )
    fence = budgets.fence_generation

    def fail_after_fence(*args, **kwargs):
        """Interrupt between updating budget state and publishing the job's generation."""
        fence(*args, **kwargs)
        raise RuntimeError("simulated publication failure")

    monkeypatch.setattr(budgets, "fence_generation", fail_after_fence)
    with pytest.raises(RuntimeError, match="simulated publication failure"):
        store.requeue_for_resume("recovery-rollback", budget_service=budgets)
    job = store.get_job("recovery-rollback")
    budget = budgets.get(budget_id, "resume-owner")
    assert job["execution_generation"] == 0
    assert job["execution_budget_generation"] == budget.generation == 0
    assert job["recovery"] is None
    assert budgets.get_operation(reservation.id, "resume-owner").state == "reserved"
    assert budget.reserved_cents == 3


def test_checkpoint_metadata_inspection_never_executes_pickle_payload(tmp_path: Path) -> None:
    """Inspect schema and counters without executing a guest-authored reducer."""
    marker = tmp_path / "must-not-exist"

    class GuestObject:
        """Represent an arbitrary object the untrusted checkpoint can contain."""

        def __reduce__(self):
            """Encode a safe test-only marker write that inspection must never execute."""
            return Path.write_text, (marker, "executed")

    state = {"validation_schema_version": 7, "i": 2, "total_num_evals": 5, "adapter_state": GuestObject()}
    manifest = checkpoint_manifest(pickle.dumps(state), {"optimizer_name": "GEPA"}, "fixture")
    assert manifest["iteration"] == 2
    assert not marker.exists()


@pytest.mark.parametrize("pending", [False, True])
def test_runtime_settlement_precedes_terminal_and_preserves_pending_result(
    pending: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep usage uncertainty separate from an already completed optimizer result.

    Args:
        pending: Whether provider usage remains unresolved after teardown.
        monkeypatch: Fixture binding the checkpoint's immutable runtime profile.
    """
    store = SQLiteJobStore()
    store.create_job("settling")
    store.update_job("settling", status="running")
    budgets = _fund_checkpoint(store, "settling", monkeypatch)
    budget_id = store.get_job("settling")["execution_budget_id"]
    operation = budgets.reserve(
        budget_id,
        "resume-owner",
        operation_key="runtime",
        generation=0,
        phase="run",
        cost_kind="sandbox",
        request_fingerprint="fixed-session",
        price_snapshot={"version": "fixture"},
        max_cents=3,
    )
    budgets.mark_dispatched(operation.id, "resume-owner", "fixture-session")

    class Gateway:
        """Model a parent runtime whose provider cleanup finishes after optimization."""

        runtime = SimpleNamespace(service=budgets, budget_id=budget_id, username="resume-owner")

        def close(self) -> None:
            """Require settlement to run before any terminal publication."""
            assert store.get_job("settling")["status"] == "running"
            if pending:
                raise UsagePendingError("Provider has not published final usage.")
            budgets.settle(
                operation.id, "resume-owner", evidence_key="closed", actual_cents=1, evidence={"runtime": "closed"}
            )

    snapshot, error = BackgroundWorker(job_store=store)._close_budget_gateway("settling", Gateway(), 0)
    assert error is None
    assert snapshot["pending_operations"] == int(pending)
    assert snapshot["run_spent_cents"] == ("0" if pending else "1")
    assert "username" not in snapshot
    assert "account_available_cents" not in snapshot
    assert store.get_job("settling")["terminal_evidence"]["execution_budget"] == snapshot


def test_failed_job_closes_budget_and_releases_only_unstarted_holds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bill metered usage, return unstarted holds, and still settle pending sandbox usage (SKY-529).

    Args:
        monkeypatch: Fixture binding the checkpoint's immutable runtime profile.
    """
    store = SQLiteJobStore()
    store.create_job("failed-run")
    budgets = _fund_checkpoint(store, "failed-run", monkeypatch)
    budget_id = store.get_job("failed-run")["execution_budget_id"]

    def reserve(key: str, cost_kind: str) -> str:
        """Reserve one three-cent run operation and return its id."""
        return budgets.reserve(
            budget_id,
            "resume-owner",
            operation_key=key,
            generation=0,
            phase="run",
            cost_kind=cost_kind,
            request_fingerprint=key,
            price_snapshot={"version": "fixture"},
            max_cents=3,
        ).id

    metered = reserve("metered", "model")
    budgets.mark_dispatched(metered, "resume-owner", "metered-request")
    budgets.settle(metered, "resume-owner", evidence_key="metered", actual_cents=2, evidence={"usage": "fixture"})
    unstarted = reserve("unstarted", "model")
    sandbox = reserve("sandbox", "sandbox")
    budgets.mark_dispatched(sandbox, "resume-owner", "sandbox-request")
    budgets.mark_pending(sandbox, "resume-owner")
    before = budgets.get(budget_id, "resume-owner")
    assert before.reserved_cents == 6

    closed = budgets.close_for_failed_job(budget_id, "resume-owner", expected_generation=0)

    assert closed.state == "closed"
    assert closed.blocked_reason == "job_failed"
    assert closed.run_spent_cents == 2
    assert closed.reserved_cents == 3
    assert closed.account_available_cents == before.account_available_cents + 3
    assert budgets.get_operation(unstarted, "resume-owner").state == "released"
    assert budgets.get_operation(sandbox, "resume-owner").state == "pending"

    budgets.settle(sandbox, "resume-owner", evidence_key="sandbox", actual_cents=1, evidence={"usage": "fixture"})
    settled = budgets.get(budget_id, "resume-owner")
    assert settled.run_spent_cents == 3
    assert settled.reserved_cents == 0
    assert settled.wallet_reserved_cents == 0
    assert settled.account_available_cents == 50 - 3

    reopened = budgets.resume_admission(budget_id, "resume-owner", expected_generation=0)
    assert reopened.state == "attached"
    assert reopened.blocked_reason is None


def test_stale_failed_close_keeps_the_resumed_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure close from a fenced epoch leaves a manual resume's admission and holds alone (SKY-529).

    Args:
        monkeypatch: Fixture binding the checkpoint's immutable runtime profile.
    """
    store = SQLiteJobStore()
    store.create_job("resumed-run")
    budgets = _fund_checkpoint(store, "resumed-run", monkeypatch)
    budget_id = store.get_job("resumed-run")["execution_budget_id"]
    fenced = budgets.fence_generation(budget_id, "resume-owner", expected_generation=0)
    budgets.stop_admission(budget_id, "resume-owner", reason="resume")
    budgets.resume_admission(budget_id, "resume-owner", expected_generation=fenced.generation)
    new_work = budgets.reserve(
        budget_id,
        "resume-owner",
        operation_key="new-work",
        generation=fenced.generation,
        phase="run",
        cost_kind="model",
        request_fingerprint="new-work",
        price_snapshot={"version": "fixture"},
        max_cents=3,
    )

    stale = budgets.close_for_failed_job(budget_id, "resume-owner", expected_generation=0)

    assert stale.state == "attached"
    assert stale.blocked_reason is None
    assert stale.reserved_cents == 3
    assert budgets.get_operation(new_work.id, "resume-owner").state == "reserved"


def test_failed_close_keeps_an_earlier_stop_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run that stopped at its budget limit keeps that reason when it then fails (SKY-529).

    Args:
        monkeypatch: Fixture binding the checkpoint's immutable runtime profile.
    """
    store = SQLiteJobStore()
    store.create_job("limited-run")
    budgets = _fund_checkpoint(store, "limited-run", monkeypatch)
    budget_id = store.get_job("limited-run")["execution_budget_id"]
    budgets.stop_admission(budget_id, "resume-owner", reason="budget_reached")

    closed = budgets.close_for_failed_job(budget_id, "resume-owner", expected_generation=0)

    assert closed.state == "closed"
    assert closed.blocked_reason == "budget_reached"

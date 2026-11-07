"""Tests for the hardening job's pod-safe claim, lease, budget hold and commit pin (SKY-369)."""

from __future__ import annotations

import tarfile
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from core.api.errors import DomainError
from core.billing.budgets import BudgetService
from core.billing.operation_pricing import ChargePolicy, operation_quote
from core.billing.runtime import PaidResult, generation_operation_key
from core.config import settings
from core.models.hardening import HardeningJob, HardeningJobModel
from core.service_gateway.optimization.blackbox.hardening import new_job
from core.storage.models import Base, BillingCustomerModel
from core.worker import hardening_runner
from core.worker.hardening_job import HardeningWorkspace
from core.worker.hardening_runner import (
    OPEN_STATES,
    HardeningBudget,
    HardeningClaimLostError,
    HardeningMeter,
    RepoHardeningRunner,
    _renew_until,
    claim_hardening_job,
    close_ended_hardening_holds,
    open_hardening_runner,
    operation_key,
    process_next_hardening_job,
    renew_hardening_lease,
    store_hardening_result,
)

_USER = "alice"
_TARGET = {
    "kind": "repo",
    "repo": {"repository": "octo/bot", "branch": "main", "editable_paths": ["bot"], "secrets": []},
}
_SCORER = {"metric_code": "def score(): return 1\n"}
# A placeholder managed key, so a pass gets past the start checks.
_KEYED = settings.model_copy(update={"openrouter_api_key": SecretStr("test-managed-key")})
_END_STATUSES = ["done", "clean_stop", "budget_stop", "failed", "preflight_failed", "rejected", "stale"]


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Create a private job and wallet database for each test.

    Args:
        tmp_path: Pytest temporary folder.

    Yields:
        The engine.
    """
    database = create_engine(f"sqlite:///{tmp_path / 'hardening.db'}", connect_args={"timeout": 10})
    Base.metadata.create_all(database)
    with Session(database) as session:
        session.add(BillingCustomerModel(username=_USER, stripe_customer_id="cus_alice", balance_cents=10_000))
        session.commit()
    yield database
    database.dispose()


def _budget(engine: Engine) -> str:
    """Create the caller's execution budget.

    Args:
        engine: The test engine.

    Returns:
        The budget id.
    """
    return BudgetService(engine=engine).create(_USER, 500, idempotency_key="harden").id


def _seed(
    engine: Engine,
    *,
    binding_id: str | None,
    place: str = "cloud",
    status: str = "preflight",
    scorer: dict[str, Any] | None = None,
    commit: str | None = None,
) -> HardeningJob:
    """Store one queued job.

    Args:
        engine: The test engine.
        binding_id: The job's budget.
        place: Where the job runs.
        status: The stored status.
        scorer: The job's scorer; the default metric when None.
        commit: A pinned commit, if any.

    Returns:
        The stored job.
    """
    job = new_job(attacker_model="m-attack", judge_model="m-judge", place=place, rounds=2, round_cap_usd=1.0)  # type: ignore[arg-type]
    job.status = status  # type: ignore[assignment]
    target = {"kind": "repo", "repo": dict(_TARGET["repo"])}  # type: ignore[call-overload]
    if commit:
        target["repo"]["commit"] = commit
    job.target = target
    job.binding_id = binding_id
    job.scorer = _SCORER if scorer is None else (scorer or None)
    with Session(engine) as session:
        session.add(
            HardeningJobModel(
                id=job.id, username=_USER, draft_id=f"draft-{job.id}", status=status, job=job.model_dump(mode="json")
            )
        )
        session.commit()
    return job


@pytest.fixture
def holds(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every hold id the worker's budget returns.

    Args:
        monkeypatch: Pytest monkeypatch.

    Returns:
        The hold ids, in call order.
    """
    seen: list[str] = []
    reserve = HardeningBudget.reserve

    def spy(self: HardeningBudget, job: HardeningJob, hold_usd: float, *, current: str | None = None) -> str:
        """Reserve as the worker does and record the hold.

        Args:
            self: The budget handle.
            job: The job.
            hold_usd: The dollars to hold.
            current: The job's stored hold.

        Returns:
            The open hold id.
        """
        seen.append(reserve(self, job, hold_usd, current=current))
        return seen[-1]

    monkeypatch.setattr(HardeningBudget, "reserve", spy)
    return seen


def _row(engine: Engine, job_id: str) -> HardeningJobModel:
    """Read one job row.

    Args:
        engine: The test engine.
        job_id: The job.

    Returns:
        The row, detached.
    """
    with Session(engine, expire_on_commit=False) as session:
        row = session.get(HardeningJobModel, job_id)
        assert row is not None
        session.expunge(row)
        return row


def _spend(service: BudgetService, budget_id: str, operation_id: str, usd: str) -> None:
    """Charge one managed model call to the job's hold through the meter.

    Args:
        service: The budget service.
        budget_id: The job's budget.
        operation_id: The job's hold.
        usd: The provider cost of the call.
    """
    meter = HardeningMeter(
        service,
        operation_id=operation_id,
        username=_USER,
        budget_id=budget_id,
        generation=service.get(budget_id, _USER).generation,
        phase="setup",
    )
    policy = ChargePolicy("managed_model")
    quote = operation_quote({"call": usd}, Decimal("0.05"), policy, {"fixture": True})
    meter.execute(
        quote,
        policy,
        lambda: PaidResult(value="ok", provider_usd=Decimal(usd), evidence={"fixture": True}),
        operation_key="call",
        cost_kind="model",
    )


def _process(engine: Engine, run: Any, *, pod: str = "pod-a", local_mode: bool = False) -> bool:
    """Run one worker pass with a fake job driver.

    Args:
        engine: The test engine.
        run: The fake driver.
        pod: The pod name.
        local_mode: Whether the worker is local.

    Returns:
        Whether a job was claimed.
    """
    return process_next_hardening_job(
        engine, pod=pod, lease_seconds=60, local_mode=local_mode, settings=_KEYED, run=run
    )


def test_only_one_pod_wins_a_claim_race(engine: Engine) -> None:
    """Eight pods race for one queued job; exactly one claims it."""
    job = _seed(engine, binding_id=None)
    barrier = Barrier(8)

    def claim(index: int) -> str | None:
        """Claim as one pod.

        Args:
            index: The pod number.

        Returns:
            The claim token when it won.
        """
        barrier.wait()
        won = claim_hardening_job(engine, pod=f"pod-{index}", lease_seconds=60, place="cloud")
        return won[4] if won else None

    with ThreadPoolExecutor(max_workers=8) as pool:
        winners = [pod for pod in pool.map(claim, range(8)) if pod]

    assert len(winners) == 1
    assert winners[0].startswith("pod-")
    assert _row(engine, job.id).claimed_by == winners[0]


def test_a_held_lease_blocks_and_an_expired_lease_frees_the_job(engine: Engine) -> None:
    """A second pod waits while the lease holds and takes the job after it lapses; the first pod's result is dropped."""
    job = _seed(engine, binding_id=None)
    first = claim_hardening_job(engine, pod="pod-a", lease_seconds=60, place="cloud")
    assert first is not None
    assert claim_hardening_job(engine, pod="pod-b", lease_seconds=60, place="cloud") is None

    with Session(engine) as session:
        row = session.get(HardeningJobModel, job.id)
        assert row is not None
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()

    second = claim_hardening_job(engine, pod="pod-b", lease_seconds=60, place="cloud")
    assert second is not None
    assert second[4].startswith("pod-b:")
    assert _row(engine, job.id).claimed_by == second[4]
    assert not store_hardening_result(engine, job.id, job, claim=first[4], operation_id=None)
    assert _row(engine, job.id).claimed_by == second[4]


def test_claim_skips_other_places_and_waiting_jobs(engine: Engine) -> None:
    """A cloud worker never takes a local job, and no worker takes a job that waits for the user."""
    _seed(engine, binding_id=None, place="local")
    _seed(engine, binding_id=None, status="waiting_review")
    assert claim_hardening_job(engine, pod="pod-a", lease_seconds=60, place="cloud") is None


@pytest.mark.parametrize("status", _END_STATUSES)
def test_each_end_status_settles_the_spend_then_releases_the_rest(
    engine: Engine, holds: list[str], status: str
) -> None:
    """At every end status the hold settles the actual spend for good and holds nothing more."""
    budget_id = _budget(engine)
    job = _seed(engine, binding_id=budget_id)
    service = BudgetService(engine=engine)

    def run(job: HardeningJob, **kwargs: Any) -> HardeningJob:
        """Hold, spend one call, and end.

        Args:
            job: The job.
            **kwargs: The driver arguments.

        Returns:
            The ended job.
        """
        kwargs["reserve"](job, 2.0)
        _spend(service, budget_id, holds[-1], "0.02")
        ended = job.model_copy(deep=True)
        ended.status = status  # type: ignore[assignment]
        return ended

    assert _process(engine, run)

    operation = service.get_operation(holds[-1], _USER)
    assert operation.state not in OPEN_STATES
    assert operation.actual_cents > 0
    assert service.get(budget_id, _USER).reserved_cents == 0
    row = _row(engine, job.id)
    assert row.status == status
    assert row.budget_operation_id is None
    assert row.claimed_by is None
    assert row.lease_expires_at is None


def test_a_job_that_spent_nothing_only_releases(engine: Engine, holds: list[str]) -> None:
    """A hold that never dispatched is released, with no charge."""
    budget_id = _budget(engine)
    _seed(engine, binding_id=budget_id)
    service = BudgetService(engine=engine)

    def run(job: HardeningJob, **kwargs: Any) -> HardeningJob:
        """Hold and fail before paid work.

        Args:
            job: The job.
            **kwargs: The driver arguments.

        Returns:
            The failed job.
        """
        kwargs["reserve"](job, 2.0)
        ended = job.model_copy(deep=True)
        ended.status = "preflight_failed"
        return ended

    assert _process(engine, run)

    operation = service.get_operation(holds[-1], _USER)
    assert operation.state == "released"
    assert operation.actual_cents == 0
    assert service.get(budget_id, _USER).reserved_cents == 0


def test_a_waiting_job_keeps_its_hold_but_no_lease(engine: Engine, holds: list[str]) -> None:
    """A job that waits for the user keeps its open hold for the next pass and holds no claim."""
    budget_id = _budget(engine)
    job = _seed(engine, binding_id=budget_id)
    service = BudgetService(engine=engine)
    claims: list[str | None] = []

    def run(job: HardeningJob, **kwargs: Any) -> HardeningJob:
        """Hold and wait for review.

        Args:
            job: The job.
            **kwargs: The driver arguments.

        Returns:
            The waiting job.
        """
        kwargs["reserve"](job, 2.0)
        claims.append(_row(engine, job.id).claimed_by)
        waiting = job.model_copy(deep=True)
        waiting.status = "waiting_review"
        return waiting

    assert _process(engine, run)

    row = _row(engine, job.id)
    assert len(claims) == 1
    assert (claims[0] or "").startswith("pod-a:")
    assert row.status == "waiting_review"
    assert row.claimed_by is None
    assert row.lease_expires_at is None
    assert row.budget_operation_id == holds[-1]
    assert service.get_operation(holds[-1], _USER).state in OPEN_STATES
    assert not _process(engine, run)


def test_the_next_pass_reuses_the_open_hold(engine: Engine, holds: list[str]) -> None:
    """A second pass on the same job reserves nothing new."""
    budget_id = _budget(engine)
    job = _seed(engine, binding_id=budget_id)

    def run(job: HardeningJob, **kwargs: Any) -> HardeningJob:
        """Hold, and stay queued for another pass.

        Args:
            job: The job.
            **kwargs: The driver arguments.

        Returns:
            The job, still queued.
        """
        kwargs["reserve"](job, 2.0)
        return job

    assert _process(engine, run)
    assert _process(engine, run)
    assert len(holds) == 2
    assert holds[0] == holds[1] == _row(engine, job.id).budget_operation_id


def test_a_retried_job_takes_the_next_attempt_of_its_key(engine: Engine) -> None:
    """After a hold closes, the next reserve opens a new attempt under the same key."""
    budget_id = _budget(engine)
    job = _seed(engine, binding_id=budget_id)
    service = BudgetService(engine=engine)
    budget = HardeningBudget(service, username=_USER, budget_id=budget_id)

    first = budget.reserve(job, 2.0)
    assert budget.reserve(job, 2.0, current=first) == first
    budget.finish(first)
    second = budget.reserve(job, 2.0, current=first)

    assert second != first
    snapshot = service.get_operation(second, _USER)
    assert snapshot.state in OPEN_STATES
    assert snapshot.attempt == 1
    assert snapshot.operation_key == operation_key(job.id)


def test_the_meter_settles_each_call_cumulatively(engine: Engine) -> None:
    """Two calls leave the hold dispatched with both costs, and the final settle keeps that sum."""
    budget_id = _budget(engine)
    job = _seed(engine, binding_id=budget_id)
    service = BudgetService(engine=engine)
    budget = HardeningBudget(service, username=_USER, budget_id=budget_id)
    held = budget.reserve(job, 2.0)

    _spend(service, budget_id, held, "0.01")
    once = service.get_operation(held, _USER).actual_cents
    _spend(service, budget_id, held, "0.01")
    twice = service.get_operation(held, _USER)

    assert twice.actual_cents == 2 * once
    assert twice.state in OPEN_STATES
    budget.finish(held)
    final = service.get_operation(held, _USER)
    assert final.state not in OPEN_STATES
    assert final.actual_cents == twice.actual_cents


def test_a_job_without_a_scorer_fails_without_a_pass(engine: Engine) -> None:
    """A job with no scorer ends as failed with hardening.no_scorer, and the driver never runs."""
    job = _seed(engine, binding_id=_budget(engine), scorer={})
    calls: list[str] = []

    assert _process(engine, lambda job, **kwargs: calls.append(job.id))

    row = _row(engine, job.id)
    assert calls == []
    assert row.status == "failed"
    assert row.job["error"] == "hardening.no_scorer"
    assert row.claimed_by is None


def test_a_raising_pass_fails_the_job_and_closes_its_hold(engine: Engine) -> None:
    """A driver error ends the job as failed with a stable key, and the hold closes."""
    budget_id = _budget(engine)
    job = _seed(engine, binding_id=budget_id)
    service = BudgetService(engine=engine)

    def run(job: HardeningJob, **kwargs: Any) -> HardeningJob:
        """Hold, then raise.

        Args:
            job: The job.
            **kwargs: The driver arguments.

        Raises:
            KeyError: Always.
        """
        kwargs["reserve"](job, 2.0)
        raise KeyError("boom")

    assert _process(engine, run)

    row = _row(engine, job.id)
    assert row.status == "failed"
    assert row.job["error"] == "hardening.runner_failed"
    assert row.budget_operation_id is None
    assert service.get(budget_id, _USER).reserved_cents == 0


def test_the_first_pass_pins_the_commit_for_later_passes(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """The commit the first pass staged goes into the target, and the next pass stages that commit."""
    job = _seed(engine, binding_id=_budget(engine), place="local")
    payloads: list[dict[str, Any]] = []

    @contextmanager
    def fake_runner(workspace: HardeningWorkspace, job: HardeningJob, **kwargs: Any) -> Iterator[object]:
        """Report the staged commit as the real runner does.

        Args:
            workspace: The pass's trees.
            job: The job.
            **kwargs: The runner arguments.

        Yields:
            A stand-in runner.
        """
        kwargs["on_commit"](workspace.commit)
        yield object()

    monkeypatch.setattr(hardening_runner, "open_hardening_runner", fake_runner)

    def run(job: HardeningJob, **kwargs: Any) -> HardeningJob:
        """Open the runner on a staged tree at a new commit.

        Args:
            job: The job.
            **kwargs: The driver arguments.

        Returns:
            The job, still queued.
        """
        payloads.append(kwargs["payload"])
        workspace = HardeningWorkspace(
            agent_archive=Path("a"),
            trusted_root=Path("t"),
            exploits_path=Path("e"),
            reference=None,
            commit="c0ffee",
            applied_fixes=(),
            secrets={},
        )
        with kwargs["runner_factory"](workspace, job):
            pass
        return job

    assert _process(engine, run, local_mode=True)
    assert _row(engine, job.id).job["target"]["repo"]["commit"] == "c0ffee"
    assert _process(engine, run, local_mode=True)
    assert "commit" not in payloads[0]["target"]["repo"]
    assert payloads[1]["target"]["repo"]["commit"] == "c0ffee"


def test_a_pinned_commit_is_never_moved(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """A target that already names a commit keeps it."""
    job = _seed(engine, binding_id=_budget(engine), place="local", commit="abc123")

    @contextmanager
    def fake_runner(workspace: HardeningWorkspace, job: HardeningJob, **kwargs: Any) -> Iterator[object]:
        """Report another commit.

        Args:
            workspace: The pass's trees.
            job: The job.
            **kwargs: The runner arguments.

        Yields:
            A stand-in runner.
        """
        kwargs["on_commit"]("other")
        yield object()

    monkeypatch.setattr(hardening_runner, "open_hardening_runner", fake_runner)

    def run(job: HardeningJob, **kwargs: Any) -> HardeningJob:
        """Open the runner once.

        Args:
            job: The job.
            **kwargs: The driver arguments.

        Returns:
            The job.
        """
        with kwargs["runner_factory"](None, job):
            pass
        return job

    assert _process(engine, run, local_mode=True)
    assert _row(engine, job.id).job["target"]["repo"]["commit"] == "abc123"


def _diff(before: str, after: str) -> str:
    """Write a git diff that changes the metric file's one line.

    Args:
        before: The old line.
        after: The new line.

    Returns:
        The diff.
    """
    return (
        "diff --git a/eval/metric.py b/eval/metric.py\n"
        "--- a/eval/metric.py\n+++ b/eval/metric.py\n"
        f"@@ -1 +1 @@\n-{before}\n+{after}\n"
    )


def test_the_runner_scores_with_the_approved_fixes_applied(tmp_path: Path) -> None:
    """Earlier rounds' fixes, then the fix under test, change the tree the scorer opens."""
    trusted = tmp_path / "trusted"
    (trusted / "eval").mkdir(parents=True)
    (trusted / "eval" / "metric.py").write_text("v1\n")
    first = _diff("v1", "v2")
    second = _diff("v2", "v3")
    workspace = HardeningWorkspace(
        agent_archive=tmp_path / "agent.tgz",
        trusted_root=trusted,
        exploits_path=trusted / "eval" / "exploits",
        reference=None,
        commit="c0ffee",
        applied_fixes=(first,),
        secrets={},
    )
    seen: list[str] = []

    class FakeScorer:
        """Reads the metric text the scorer's tree holds."""

        def __init__(self, archive: Path, fixes: tuple[str, ...]) -> None:
            """Read the packed metric.

            Args:
                archive: The packed tree.
                fixes: The metric fixes packed into the tree.
            """
            with tarfile.open(archive) as tree:
                handle = tree.extractfile("eval/metric.py")
                assert handle is not None
                seen.append(handle.read().decode())

        def score(self, patch: str) -> dict[str, float]:
            """Return a fixed score.

            Args:
                patch: The solution.

            Returns:
                The score.
            """
            return {"score": 1.0}

        def close(self) -> None:
            """Close nothing."""

    job = new_job(attacker_model="m", judge_model="m", place="local")
    runner = RepoHardeningRunner(
        workspace,
        job,
        gateway=None,
        meter=None,
        open_scorer=FakeScorer,
        managed_key="k",  # type: ignore[arg-type]
    )
    try:
        assert runner.fixes == [first]
        runner._score(None, "")
        runner._score(second, "")
        runner._score(None, "")
    finally:
        runner.close()

    assert seen == ["v2\n", "v3\n"]


def _fixed_tree(tmp_path: Path, applied: tuple[str, ...]) -> RepoHardeningRunner:
    """Create a runner whose trusted tree holds a one-line metric.

    Args:
        tmp_path: Pytest temporary folder.
        applied: The approved fixes of earlier rounds.

    Returns:
        The runner, with a scorer that always returns 1.
    """
    trusted = tmp_path / "trusted"
    (trusted / "eval").mkdir(parents=True)
    (trusted / "eval" / "metric.py").write_text("v1\n")
    workspace = HardeningWorkspace(
        agent_archive=tmp_path / "agent.tgz",
        trusted_root=trusted,
        exploits_path=trusted / "eval" / "exploits",
        reference=None,
        commit="c0ffee",
        applied_fixes=applied,
        secrets={},
    )
    scorer = SimpleNamespace(score=lambda patch: {"score": 1.0}, close=lambda: None)
    return RepoHardeningRunner(
        workspace,
        new_job(attacker_model="m", judge_model="m", place="local"),
        gateway=None,
        meter=None,
        open_scorer=lambda archive, fixes: scorer,
        managed_key="k",  # type: ignore[arg-type]
    )


def test_a_drafted_fix_that_does_not_apply_scores_negative_infinity(tmp_path: Path) -> None:
    """A drafted fix that does not apply scores -inf, so its checks fail and the job waits for the user."""
    runner = _fixed_tree(tmp_path, ())
    try:
        assert runner._score(_diff("other", "v2"), "") == float("-inf")
        assert runner._score(None, "") == 1.0
    finally:
        runner.close()


def test_an_approved_fix_that_does_not_apply_still_raises(tmp_path: Path) -> None:
    """When the approved fixes alone do not apply, the base score raises and the pass fails."""
    runner = _fixed_tree(tmp_path, (_diff("other", "v2"),))
    try:
        with pytest.raises(RuntimeError):
            runner._score(None, "")
    finally:
        runner.close()


def _meter(
    service: BudgetService, budget_id: str, operation_id: str, lost: threading.Event | None = None
) -> HardeningMeter:
    """Create a meter on the job's hold.

    Args:
        service: The budget service.
        budget_id: The job's budget.
        operation_id: The job's hold.
        lost: The claim-lost signal, if any.

    Returns:
        The meter.
    """
    return HardeningMeter(
        service,
        operation_id=operation_id,
        lost=lost,
        username=_USER,
        budget_id=budget_id,
        generation=service.get(budget_id, _USER).generation,
        phase="setup",
    )


def _quote(key: str) -> tuple[Any, ChargePolicy]:
    """Quote one managed model call with a 5 cent maximum.

    Args:
        key: Makes the quote's request distinct.

    Returns:
        The quote and its policy.
    """
    policy = ChargePolicy("managed_model")
    return operation_quote({"call": key}, Decimal("0.05"), policy, {"fixture": True}), policy


def test_a_failed_dispatch_keeps_the_call_maximum_in_the_hold(engine: Engine) -> None:
    """A transport error after dispatch charges the call's maximum as unconfirmed, re-raises, and the close keeps it."""
    budget_id = _budget(engine)
    job = _seed(engine, binding_id=budget_id)
    service = BudgetService(engine=engine)
    budget = HardeningBudget(service, username=_USER, budget_id=budget_id)
    held = budget.reserve(job, 2.0)
    quote, policy = _quote("timeout")

    def dispatch() -> PaidResult[str]:
        """Fail as a timed-out transport does.

        Raises:
            TimeoutError: Always.
        """
        raise TimeoutError

    with pytest.raises(TimeoutError):
        _meter(service, budget_id, held).execute(quote, policy, dispatch, operation_key="call", cost_kind="model")

    assert service.get_operation(held, _USER).actual_cents == quote.maximum.total
    budget.finish(held)
    final = service.get_operation(held, _USER)
    assert final.state not in OPEN_STATES
    assert final.actual_cents == quote.maximum.total


def test_a_spent_hold_sets_exhausted_before_it_refuses_a_call(engine: Engine) -> None:
    """A call whose maximum passes the hold sets exhausted, raises, and never dispatches."""
    budget_id = _budget(engine)
    job = _seed(engine, binding_id=budget_id)
    service = BudgetService(engine=engine)
    held = HardeningBudget(service, username=_USER, budget_id=budget_id).reserve(job, 0.01)
    exhausted = threading.Event()
    meter = HardeningMeter(
        service,
        operation_id=held,
        username=_USER,
        budget_id=budget_id,
        generation=service.get(budget_id, _USER).generation,
        phase="setup",
        exhausted=exhausted,
    )
    quote, policy = _quote("over")
    calls: list[str] = []

    with pytest.raises(hardening_runner.HardeningBudgetExceededError):
        meter.execute(quote, policy, lambda: calls.append("x"), operation_key="call", cost_kind="model")

    assert exhausted.is_set()
    assert calls == []


def test_a_pass_that_fails_on_a_spent_hold_ends_as_budget_stop(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """When the meter ran out of hold, the core's failed result becomes budget_stop with no error."""
    job = _seed(engine, binding_id=_budget(engine))

    def fake_runner(*args: Any, exhausted: threading.Event, **kwargs: Any) -> None:
        """Signal a spent hold as the meter does.

        Args:
            *args: The runner arguments.
            exhausted: The job's spent-hold signal.
            **kwargs: The runner options.
        """
        exhausted.set()

    def run(job: HardeningJob, **kwargs: Any) -> HardeningJob:
        """Open the runner, then fail as the core does on a raised call.

        Args:
            job: The job.
            **kwargs: The driver arguments.

        Returns:
            The failed job.
        """
        kwargs["runner_factory"](None, job)
        ended = job.model_copy(deep=True)
        ended.status = "failed"
        ended.error = "The job's hold cannot cover another model call."
        return ended

    monkeypatch.setattr(hardening_runner, "open_hardening_runner", fake_runner)
    assert _process(engine, run)

    row = _row(engine, job.id)
    assert row.status == "budget_stop"
    assert row.job["error"] is None


def test_parallel_calls_on_one_meter_add_both_costs(engine: Engine) -> None:
    """Two gateway threads that settle at the same time both land in the hold."""
    budget_id = _budget(engine)
    job = _seed(engine, binding_id=budget_id)
    service = BudgetService(engine=engine)
    held = HardeningBudget(service, username=_USER, budget_id=budget_id).reserve(job, 2.0)
    meter = _meter(service, budget_id, held)
    barrier = Barrier(2)

    def call(usd: str) -> str:
        """Run one call that waits for the other before it returns.

        Args:
            usd: The call's provider cost.

        Returns:
            The call's value.
        """
        quote, policy = _quote(usd)

        def dispatch() -> PaidResult[str]:
            """Return after both calls are in flight.

            Returns:
                The paid result.
            """
            barrier.wait()
            return PaidResult(value="ok", provider_usd=Decimal(usd), evidence={"usd": usd})

        return meter.execute(quote, policy, dispatch, operation_key=usd, cost_kind="model")

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(call, ["0.01", "0.02"])) == ["ok", "ok"]

    policy = ChargePolicy("managed_model")
    expected = policy.convert(Decimal("0.01")).total + policy.convert(Decimal("0.02")).total
    assert service.get_operation(held, _USER).actual_cents == expected


def test_a_lost_claim_stops_the_meter(engine: Engine) -> None:
    """After another pod takes the job, the meter refuses every call before dispatch."""
    budget_id = _budget(engine)
    job = _seed(engine, binding_id=budget_id)
    service = BudgetService(engine=engine)
    held = HardeningBudget(service, username=_USER, budget_id=budget_id).reserve(job, 2.0)
    lost = threading.Event()
    lost.set()
    quote, policy = _quote("lost")
    calls: list[str] = []

    with pytest.raises(HardeningClaimLostError):
        _meter(service, budget_id, held, lost).execute(
            quote, policy, lambda: calls.append("x"), operation_key="call", cost_kind="model"
        )

    assert calls == []
    assert service.get_operation(held, _USER).actual_cents == 0


def test_a_renewal_with_a_stale_claim_sets_lost(engine: Engine) -> None:
    """The renewer of a stale claim stops and sets lost; a live claim renews and calls on_renew."""
    job = _seed(engine, binding_id=None)
    live = claim_hardening_job(engine, pod="pod-a", lease_seconds=60, place="cloud")
    assert live is not None
    assert not renew_hardening_lease(engine, job.id, claim="pod-a:stale", lease_seconds=60)

    stop, lost = threading.Event(), threading.Event()
    _renew_until(engine, job.id, "pod-a:stale", 0.03, stop, lost, lambda: None)
    assert lost.is_set()

    renewals: list[int] = []
    stop, lost = threading.Event(), threading.Event()

    def on_renew() -> None:
        """Count a renewal and stop after two."""
        renewals.append(1)
        if len(renewals) == 2:
            stop.set()

    _renew_until(engine, job.id, live[4], 0.03, stop, lost, on_renew)
    assert len(renewals) == 2
    assert not lost.is_set()


def test_a_same_pod_reclaim_fences_the_older_claim(engine: Engine) -> None:
    """When a lease lapses and the same pod claims again, the older pass of that pod cannot store."""
    job = _seed(engine, binding_id=None)
    first = claim_hardening_job(engine, pod="pod-a", lease_seconds=60, place="cloud")
    assert first is not None
    with Session(engine) as session:
        row = session.get(HardeningJobModel, job.id)
        assert row is not None
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    second = claim_hardening_job(engine, pod="pod-a", lease_seconds=60, place="cloud")
    assert second is not None
    assert second[4] != first[4]

    assert not store_hardening_result(engine, job.id, job, claim=first[4], operation_id=None)
    assert not renew_hardening_lease(engine, job.id, claim=first[4], lease_seconds=60)
    assert store_hardening_result(engine, job.id, job, claim=second[4], operation_id=None)


def test_reserve_uses_the_generation_key_after_a_fence(engine: Engine) -> None:
    """After the budget's generation moves, a new hold takes the generation-scoped key and opens."""
    budget_id = _budget(engine)
    job = _seed(engine, binding_id=budget_id)
    service = BudgetService(engine=engine)
    budget = HardeningBudget(service, username=_USER, budget_id=budget_id)
    first = budget.reserve(job, 2.0)
    budget.finish(first)
    fenced = service.fence_generation(budget_id, _USER, expected_generation=service.get(budget_id, _USER).generation)

    second = budget.reserve(job, 2.0, current=first)

    snapshot = service.get_operation(second, _USER)
    assert snapshot.state in OPEN_STATES
    assert snapshot.generation == fenced.generation
    assert snapshot.operation_key == generation_operation_key(operation_key(job.id), fenced.generation)


def test_the_sweep_closes_a_hold_that_an_earlier_close_left_open(
    engine: Engine, holds: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ended job whose close failed keeps its hold id; the sweep closes the hold and clears the id."""
    budget_id = _budget(engine)
    job = _seed(engine, binding_id=budget_id)
    service = BudgetService(engine=engine)
    held = holds

    def run(job: HardeningJob, **kwargs: Any) -> HardeningJob:
        """Hold, spend, and end.

        Args:
            job: The job.
            **kwargs: The driver arguments.

        Returns:
            The ended job.
        """
        kwargs["reserve"](job, 2.0)
        _spend(service, budget_id, held[-1], "0.02")
        ended = job.model_copy(deep=True)
        ended.status = "done"
        return ended

    def broken_finish(self: HardeningBudget, operation_id: str) -> None:
        """Fail as a database error does.

        Args:
            self: The budget handle.
            operation_id: The hold.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("database away")

    real_finish = HardeningBudget.finish
    monkeypatch.setattr(HardeningBudget, "finish", broken_finish)
    assert _process(engine, run)
    row = _row(engine, job.id)
    assert row.status == "done"
    assert row.budget_operation_id == held[-1]
    assert service.get(budget_id, _USER).reserved_cents > 0

    monkeypatch.setattr(HardeningBudget, "finish", real_finish)
    assert close_ended_hardening_holds(engine) == 1

    assert _row(engine, job.id).budget_operation_id is None
    assert service.get_operation(held[-1], _USER).state not in OPEN_STATES
    assert service.get(budget_id, _USER).reserved_cents == 0
    assert close_ended_hardening_holds(engine) == 0


@pytest.mark.parametrize(
    ("scorer", "keyed", "error"),
    [
        ({"kind": "remote", "url": "https://score.example", "metric_code": "x"}, True, "hardening.no_python_metric"),
        ({"kind": "python"}, True, "hardening.no_python_metric"),
        (None, False, "hardening.no_managed_models"),
        (
            {**_SCORER, "model": {"name": "openai/gpt-4o-mini", "token_source": "byok", "extra": {}}},
            True,
            "hardening.byok_missing",
        ),
    ],
)
def test_a_job_that_cannot_start_ends_with_a_stable_key(
    engine: Engine, scorer: dict[str, Any] | None, keyed: bool, error: str
) -> None:
    """A remote or metric-less scorer, or a deployment without managed models, ends the job before any hold."""
    budget_id = _budget(engine)
    job = _seed(engine, binding_id=budget_id, scorer=scorer)
    calls: list[str] = []

    assert process_next_hardening_job(
        engine,
        pod="pod-a",
        lease_seconds=60,
        local_mode=False,
        settings=_KEYED if keyed else settings.model_copy(update={"openrouter_api_key": None}),
        run=lambda job, **kwargs: calls.append(job.id),
    )

    row = _row(engine, job.id)
    assert calls == []
    assert row.status == "failed"
    assert row.job["error"] == error
    assert BudgetService(engine=engine).get(budget_id, _USER).reserved_cents == 0


@pytest.mark.parametrize(
    ("refusal", "error"),
    [
        (None, "hardening.byok_missing"),
        (DomainError("billing.byok_not_configured", status=503), "hardening.byok_missing"),
        (DomainError("billing.byok_model_not_served", status=400), "hardening.byok_not_served"),
    ],
)
def test_a_byok_scorer_without_a_usable_key_ends_with_a_stable_key(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, refusal: DomainError | None, error: str
) -> None:
    """A BYOK scorer with no saved key, no vault, or a key Skynet cannot route, ends before any hold."""
    budget_id = _budget(engine)
    job = _seed(
        engine,
        binding_id=budget_id,
        scorer={**_SCORER, "model": {"name": "openai/gpt-4o-mini", "token_source": "byok", "extra": {}}},
    )
    calls: list[str] = []

    def resolve_connection(username: str, provider: str) -> None:
        """Find no key, or refuse as the vault or the route check does.

        Args:
            username: The owner.
            provider: The provider slug.

        Raises:
            DomainError: The case's refusal, when it has one.
        """
        if refusal is not None:
            raise refusal

    monkeypatch.setattr(
        hardening_runner, "ProviderKeyVault", lambda **kwargs: SimpleNamespace(resolve_connection=resolve_connection)
    )

    assert _process(engine, lambda job, **kwargs: calls.append(job.id))

    row = _row(engine, job.id)
    assert calls == []
    assert row.status == "failed"
    assert row.job["error"] == error
    assert BudgetService(engine=engine).get(budget_id, _USER).reserved_cents == 0


def test_the_runner_resolves_a_byok_key_and_keys_each_scorer_box(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A BYOK scorer model gets its saved key at run time, and each scorer box of a pass has its own key."""
    budget_id = _budget(engine)
    job = _seed(
        engine,
        binding_id=budget_id,
        scorer={**_SCORER, "model": {"name": "openai/gpt-4o-mini", "token_source": "byok", "extra": {}}},
    )
    protected: list[dict[str, Any]] = []
    keys: list[str] = []

    class FakeGateway:
        """Records the payload the runner protects."""

        def __init__(self, meter: HardeningMeter) -> None:
            """Keep the meter.

            Args:
                meter: The job's meter.
            """
            self.meter = meter

        def protect_payload(self, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            """Record and return the payload.

            Args:
                payload: The scorer payload.
                **kwargs: The protect arguments.

            Returns:
                The payload.
            """
            protected.append(payload)
            return payload

        def parent_sandbox_runtime(self) -> None:
            """Return no runtime."""

        def close(self) -> None:
            """Close nothing."""

    class FakeWorkspace:
        """Records the box's operation key."""

        def __init__(self, *, spec: Any, **kwargs: Any) -> None:
            """Record the key.

            Args:
                spec: The sandbox spec.
                **kwargs: The workspace arguments.
            """
            keys.append(spec.operation_key)

    class FakeScorer:
        """A scorer that prepares nothing."""

        def __init__(self, workspace: FakeWorkspace, *args: Any, **kwargs: Any) -> None:
            """Keep nothing.

            Args:
                workspace: The box.
                *args: The scorer arguments.
                **kwargs: The scorer options.
            """

        def prepare(self) -> None:
            """Prepare nothing."""

        def close(self) -> None:
            """Close nothing."""

    class FakeVault:
        """Returns one saved OpenAI connection."""

        def resolve_connection(self, username: str, provider: str) -> SimpleNamespace | None:
            """Return the saved connection.

            Args:
                username: The owner.
                provider: The provider slug.

            Returns:
                The connection for OpenAI, else None.
            """
            if provider != "openai":
                return None
            return SimpleNamespace(secret="saved-user-key", api_base=None, params={})

    monkeypatch.setattr(hardening_runner, "ModelGateway", FakeGateway)
    monkeypatch.setattr(
        hardening_runner, "bind_protected_sandbox", lambda *a, **k: {"lifetime_seconds": 60, "image": "i"}
    )
    monkeypatch.setattr(hardening_runner, "_repo_model_gateway", lambda model: None)
    monkeypatch.setattr(hardening_runner, "RepoWorkspace", FakeWorkspace)
    monkeypatch.setattr(hardening_runner, "RepoScorer", FakeScorer)
    workspace = HardeningWorkspace(
        agent_archive=tmp_path / "a.tgz",
        trusted_root=tmp_path,
        exploits_path=tmp_path / "e",
        reference=None,
        commit="c0ffee",
        applied_fixes=(),
        secrets={},
    )

    with open_hardening_runner(
        workspace,
        job,
        service=BudgetService(engine=engine),
        username=_USER,
        budget_id=budget_id,
        operation_id=None,
        settings=_KEYED,
        claim="pod-a:one",
        vault=FakeVault(),  # type: ignore[arg-type]
    ) as runner:
        runner.open_scorer(tmp_path / "a.tgz", ())
        runner.open_scorer(tmp_path / "a.tgz", ("fix",))

    assert protected[0]["scorer"]["model"]["extra"]["api_key"] == "saved-user-key"
    assert job.scorer is not None
    assert "api_key" not in job.scorer["model"]["extra"]
    assert len(keys) == 2
    assert keys[0] != keys[1]
    assert all(key.startswith(f"repo-scorer:{operation_key(job.id)}:") for key in keys)

    with (
        pytest.raises(ValueError, match="vault"),
        open_hardening_runner(
            workspace,
            job,
            service=BudgetService(engine=engine),
            username=_USER,
            budget_id=budget_id,
            operation_id=None,
            settings=_KEYED,
            claim="pod-a:one",
        ),
    ):
        pass

"""The metric-hardening loop: attack, judge, user review, metric fix and exploit suite.

An attacker model tries to score high without solving the task. A judge labels
each claim. The user confirms the real exploits, the agent drafts a metric fix,
three automatic checks run on it, and the user approves it. Approved rounds add
their exploits to the suite at ``.optimize/exploits/<id>/{patch.diff, why.md}``.

Every function here is pure state logic over a :class:`HardeningJob`. The
caller passes the run/sandbox runner, the exploits path and the reference, so
unit tests use fakes and the worker passes the real ones. Each function returns
a new job and leaves its input unchanged.
"""

from __future__ import annotations

import json
import math
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from ....constants import HARDENING_DEFAULT_ROUND_CAP_USD, HARDENING_DEFAULT_ROUNDS, HARDENING_UNSURE_CONFIDENCE
from ....models.hardening import (
    HardeningExploit,
    HardeningFix,
    HardeningFixChecks,
    HardeningJob,
    HardeningPlace,
    HardeningRound,
    HardeningView,
)
from ....notifications.catalog import HARDENING_DONE_EVENT, HARDENING_REVIEW_EVENT

RETRYABLE_STATUSES: frozenset[str] = frozenset({"failed", "preflight_failed", "stale", "rejected"})
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"preflight_failed", "clean_stop", "budget_stop", "done", "rejected", "failed", "stale"}
)


class HardeningStateError(Exception):
    """The job is not in a state that allows the requested step."""


class ExploitNotFoundError(Exception):
    """The current round has no exploit with the given id."""


class FixChecksFailedError(Exception):
    """A fix with a failed automatic check cannot be approved."""


@dataclass(frozen=True)
class Candidate:
    """One attacker claim before the judge labels it."""

    kind: Literal["case", "method"]
    patch_diff: str
    why_md: str


@dataclass(frozen=True)
class Verdict:
    """The judge's label for one candidate."""

    label: Literal["exploit", "valid"]
    confidence: float
    reason: str


class HardeningRunner(Protocol):
    """The run/sandbox side of the loop. The worker gives the real one; tests give fakes."""

    def preflight(self, *, view: HardeningView) -> list[str]:
        """Check that the metric and the repository can be attacked.

        Args:
            view: What the attacker may see.

        Returns:
            The failure codes; empty when the job can start.
        """

    def attack(
        self,
        *,
        round_index: int,
        view: HardeningView,
        model: str,
        exploits_path: Path,
        budget_usd: float | None,
    ) -> tuple[list[Candidate], float]:
        """Run one attack round.

        Args:
            round_index: The 1-based round.
            view: What the attacker may see.
            model: The attacker model.
            exploits_path: The suite folder, so the attacker can skip known exploits.
            budget_usd: The dollars the round may still spend, or None when local.

        Returns:
            The candidates and the dollars spent.
        """

    def judge(self, *, candidate: Candidate, model: str, budget_usd: float | None) -> tuple[Any, float]:
        """Label one candidate.

        Args:
            candidate: The attacker's claim.
            model: The judge model.
            budget_usd: The dollars the round may still spend, or None when local.

        Returns:
            The raw judge output for :func:`parse_verdict`, and the dollars spent.
        """

    def draft_fix(
        self,
        *,
        exploits: list[HardeningExploit],
        model: str,
        exploits_path: Path,
        budget_usd: float | None,
    ) -> tuple[str, float]:
        """Draft a metric fix for the confirmed exploits.

        Args:
            exploits: The exploits the user confirmed in this round.
            model: The model that drafts the fix.
            exploits_path: The suite folder.
            budget_usd: The dollars the round may still spend, or None when local.

        Returns:
            The fix as a unified diff, and the dollars spent.
        """

    def score(self, *, fix_diff: str | None, exploit: HardeningExploit | None) -> float:
        """Score the base solution, or an exploit, on the metric.

        Args:
            fix_diff: A metric fix to apply first, or None for the current metric.
            exploit: The exploit to replay, or None for the base solution.

        Returns:
            The metric score.
        """

    def reference_score(self, *, fix_diff: str | None, reference: Path) -> float:
        """Score the reference solution on the metric.

        Args:
            fix_diff: A metric fix to apply first, or None for the current metric.
            reference: The ``[metric] reference`` solution.

        Returns:
            The metric score.
        """

    def apply_fix(self, *, fix_diff: str) -> None:
        """Apply an approved fix to the metric for good.

        Args:
            fix_diff: The approved fix.
        """


def parse_verdict(raw: Any) -> Verdict:
    """Read the judge output as a verdict.

    SKY-348 ships the shared classification schema. When it lands, this
    function becomes a thin call to its parse function; the loop only calls this.

    Args:
        raw: A mapping, or JSON text of one, with ``label``, ``confidence`` and ``reason``.

    Returns:
        The verdict.

    Raises:
        ValueError: When the output does not match the schema.
    """
    data = json.loads(raw) if isinstance(raw, str | bytes) else raw
    if not isinstance(data, Mapping):
        # One error type for every schema mismatch, so the loop handles them the same way.
        raise ValueError("judge output is not an object")  # noqa: TRY004
    label = data.get("label")
    if label not in ("exploit", "valid"):
        raise ValueError(f"judge label {label!r} is not exploit or valid")
    confidence = data.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, int | float) or not 0.0 <= confidence <= 1.0:
        raise ValueError(f"judge confidence {confidence!r} is not a number from 0 to 1")
    reason = data.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("judge reason is missing")
    return Verdict(label=label, confidence=float(confidence), reason=reason.strip())


def exploit_passes(exploit_score: float, base_score: float) -> bool:
    """Apply the suite pass rule: a fixed metric scores an exploit no higher than the base.

    Args:
        exploit_score: The exploit's score on the fixed metric.
        base_score: The base solution's score on the fixed metric.

    Returns:
        True when the exploit no longer pays.
    """
    return exploit_score <= base_score


def new_job(
    *,
    attacker_model: str,
    judge_model: str,
    place: HardeningPlace,
    rounds: int = HARDENING_DEFAULT_ROUNDS,
    round_cap_usd: float = HARDENING_DEFAULT_ROUND_CAP_USD,
    view: HardeningView = "black_box",
    job_id: str | None = None,
) -> HardeningJob:
    """Build a job that waits for its preflight.

    Args:
        attacker_model: The attacker model.
        judge_model: The judge model.
        place: Where the job runs.
        rounds: The most attack rounds.
        round_cap_usd: The dollars each round may spend.
        view: What the attacker may see.
        job_id: The id to use; a new one when None.

    Returns:
        The job.
    """
    job = HardeningJob(
        id=job_id or str(uuid.uuid4()),
        status="preflight",
        round=0,
        rounds=rounds,
        round_cap_usd=round_cap_usd,
        reserved_usd=0.0,
        spent_usd=0.0,
        place=place,
        attacker_model=attacker_model,
        judge_model=judge_model,
        view=view,
    )
    _reserve(job)
    return job


def advance(
    job: HardeningJob,
    *,
    runner: HardeningRunner,
    exploits_path: Path,
    reference: Path | None,
) -> HardeningJob:
    """Do one step of a job that is in preflight or running.

    A runner error fails the job with its message; :func:`retry` resumes it.

    Args:
        job: The job.
        runner: The run/sandbox runner.
        exploits_path: The suite folder.
        reference: The optional ``[metric] reference`` solution.

    Returns:
        The job after the step.

    Raises:
        HardeningStateError: When the job waits for the user or has ended.
    """
    if job.status not in ("preflight", "running"):
        raise HardeningStateError(job.status)
    job = job.model_copy(deep=True)
    try:
        if job.status == "preflight":
            _preflight(job, runner)
        else:
            _step(job, runner, exploits_path, reference)
    except Exception as exc:
        job.status = "failed"
        job.error = str(exc) or type(exc).__name__
        _release(job)
    return job


def run_until_wait(
    job: HardeningJob,
    *,
    runner: HardeningRunner,
    exploits_path: Path,
    reference: Path | None,
) -> HardeningJob:
    """Advance a job until it waits for the user or ends.

    Args:
        job: The job.
        runner: The run/sandbox runner.
        exploits_path: The suite folder.
        reference: The optional ``[metric] reference`` solution.

    Returns:
        The job in a waiting or final state.
    """
    while job.status in ("preflight", "running"):
        job = advance(job, runner=runner, exploits_path=exploits_path, reference=reference)
    return job


def record_verdict(job: HardeningJob, exploit_id: str, *, confirmed: bool) -> HardeningJob:
    """Store the user's verdict on one exploit of the current round.

    When every exploit that needs review has a verdict, the job goes on to the
    fix if the user confirmed any, or stops clean if the user rejected all.

    Args:
        job: The job.
        exploit_id: The exploit.
        confirmed: Whether the user confirms it is a real exploit.

    Returns:
        The job after the verdict.

    Raises:
        HardeningStateError: When the job does not wait for review.
        ExploitNotFoundError: When the current round has no such exploit.
    """
    if job.status != "waiting_review" or not job.rounds_detail:
        raise HardeningStateError(job.status)
    job = job.model_copy(deep=True)
    current = job.rounds_detail[-1]
    exploit = next((e for e in current.exploits if e.id == exploit_id), None)
    if exploit is None:
        raise ExploitNotFoundError(exploit_id)
    exploit.user_verdict = "confirmed" if confirmed else "rejected"
    if any(e.user_verdict is None for e in current.exploits if _needs_review(e)):
        return job
    if any(e.user_verdict == "confirmed" for e in current.exploits):
        current.status = "running"
        job.status = "running"
    else:
        current.status = "clean"
        job.status = "clean_stop"
        _release(job)
    return job


def decide_fix(job: HardeningJob, *, approve: bool) -> HardeningJob:
    """Store the user's decision on the drafted fix.

    Args:
        job: The job.
        approve: Whether the user approves the fix.

    Returns:
        The job after the decision: running when approved, rejected otherwise.

    Raises:
        HardeningStateError: When the job has no fix that waits for a decision.
        FixChecksFailedError: When the user approves a fix that failed a check.
    """
    if job.status != "waiting_fix" or not job.rounds_detail or job.rounds_detail[-1].fix is None:
        raise HardeningStateError(job.status)
    fix = job.rounds_detail[-1].fix
    checks = (fix.checks.suite_replay, fix.checks.base_score_unchanged, fix.checks.reference_not_lower)
    if approve and "fail" in checks:
        raise FixChecksFailedError(job.id)
    job = job.model_copy(deep=True)
    current = job.rounds_detail[-1]
    assert current.fix is not None
    if approve:
        current.fix.user_decision = "approved"
        current.status = "approved"
        job.status = "running"
    else:
        current.fix.user_decision = "rejected"
        current.status = "rejected"
        job.status = "rejected"
        _release(job)
    return job


def retry(job: HardeningJob) -> HardeningJob:
    """Resume a failed, stale or rejected job, or run a failed preflight again.

    A rejected fix goes back to drafting for the same round.

    Args:
        job: The job.

    Returns:
        The job, ready for :func:`advance`.

    Raises:
        HardeningStateError: When the job cannot be retried.
    """
    if job.status not in RETRYABLE_STATUSES:
        raise HardeningStateError(job.status)
    job = job.model_copy(deep=True)
    job.error = None
    if job.status == "preflight_failed":
        job.status = "preflight"
        job.preflight_failures = []
    else:
        job.status = "running"
        if job.rounds_detail and job.rounds_detail[-1].status == "rejected":
            job.rounds_detail[-1].status = "running"
            job.rounds_detail[-1].fix = None
    _reserve(job)
    return job


def notification_event(job: HardeningJob) -> str | None:
    """Return the notification event the job's status sends, if any.

    Args:
        job: The job.

    Returns:
        "Review needed" while the user must act, "Hardening done" when the job
        ends well, or None.
    """
    if job.status in ("waiting_review", "waiting_fix"):
        return HARDENING_REVIEW_EVENT
    if job.status in ("clean_stop", "budget_stop", "done"):
        return HARDENING_DONE_EVENT
    return None


def _preflight(job: HardeningJob, runner: HardeningRunner) -> None:
    """Run the preflight and move the job to running or preflight_failed.

    Args:
        job: The job, changed in place.
        runner: The run/sandbox runner.
    """
    failures = list(runner.preflight(view=job.view))
    job.preflight_failures = failures
    if failures:
        job.status = "preflight_failed"
        _release(job)
    else:
        job.status = "running"


def _step(job: HardeningJob, runner: HardeningRunner, exploits_path: Path, reference: Path | None) -> None:
    """Do the next step of a running job.

    Args:
        job: The job, changed in place.
        runner: The run/sandbox runner.
        exploits_path: The suite folder.
        reference: The optional reference solution.
    """
    current = job.rounds_detail[-1] if job.rounds_detail else None
    if current is not None and current.status == "approved":
        _apply_fix(job, current, runner, exploits_path)
    elif current is not None and current.status == "running":
        _draft_fix(job, current, runner, exploits_path, reference)
    elif job.round >= job.rounds:
        job.status = "done"
        _release(job)
    else:
        _attack_round(job, runner, exploits_path)


def _attack_round(job: HardeningJob, runner: HardeningRunner, exploits_path: Path) -> None:
    """Run the next attack round and judge its candidates.

    Args:
        job: The job, changed in place.
        runner: The run/sandbox runner.
        exploits_path: The suite folder.
    """
    round_index = job.round + 1
    budget = _round_budget(job, round_index)
    if budget is not None and budget <= 0:
        _budget_stop(job)
        return
    candidates, cost = runner.attack(
        round_index=round_index,
        view=job.view,
        model=job.attacker_model,
        exploits_path=exploits_path,
        budget_usd=budget,
    )
    _spend(job, cost)
    job.round = round_index
    current = HardeningRound(index=round_index, status="running")
    job.rounds_detail.append(current)
    for candidate in candidates:
        budget = _round_budget(job, round_index)
        if budget is not None and budget <= 0:
            current.status = "stopped"
            _budget_stop(job)
            return
        raw, cost = runner.judge(candidate=candidate, model=job.judge_model, budget_usd=budget)
        _spend(job, cost)
        verdict = parse_verdict(raw)
        current.exploits.append(
            HardeningExploit(
                id=uuid.uuid4().hex,
                kind=candidate.kind,
                label=verdict.label,
                confidence=verdict.confidence,
                reason=verdict.reason,
                unsure=verdict.confidence < HARDENING_UNSURE_CONFIDENCE,
                patch_diff=candidate.patch_diff,
                why_md=candidate.why_md,
            )
        )
    if any(_needs_review(e) for e in current.exploits):
        current.status = "waiting_review"
        job.status = "waiting_review"
    else:
        current.status = "clean"
        job.status = "clean_stop"
        _release(job)


def _draft_fix(
    job: HardeningJob,
    current: HardeningRound,
    runner: HardeningRunner,
    exploits_path: Path,
    reference: Path | None,
) -> None:
    """Draft a fix for the round's confirmed exploits and run the three checks.

    Args:
        job: The job, changed in place.
        current: The current round.
        runner: The run/sandbox runner.
        exploits_path: The suite folder.
        reference: The optional reference solution.
    """
    budget = _round_budget(job, current.index)
    if budget is not None and budget <= 0:
        current.status = "stopped"
        _budget_stop(job)
        return
    confirmed = _confirmed(current)
    diff, cost = runner.draft_fix(
        exploits=confirmed, model=job.attacker_model, exploits_path=exploits_path, budget_usd=budget
    )
    _spend(job, cost)
    base_before = runner.score(fix_diff=None, exploit=None)
    base_after = runner.score(fix_diff=diff, exploit=None)
    suite = [e for r in job.rounds_detail if r.status == "fixed" for e in _confirmed(r)] + confirmed
    suite_holds = all(exploit_passes(runner.score(fix_diff=diff, exploit=e), base_after) for e in suite)
    checks = HardeningFixChecks(
        suite_replay=_result(suite_holds),
        base_score_unchanged=_result(math.isclose(base_before, base_after, rel_tol=1e-9, abs_tol=1e-9)),
    )
    if reference is not None:
        reference_before = runner.reference_score(fix_diff=None, reference=reference)
        reference_after = runner.reference_score(fix_diff=diff, reference=reference)
        checks.reference_not_lower = _result(
            reference_after >= reference_before or math.isclose(reference_after, reference_before, abs_tol=1e-9)
        )
    current.fix = HardeningFix(diff=diff, checks=checks)
    current.status = "waiting_fix"
    job.status = "waiting_fix"


def _apply_fix(job: HardeningJob, current: HardeningRound, runner: HardeningRunner, exploits_path: Path) -> None:
    """Apply the approved fix and add the round's confirmed exploits to the suite.

    Args:
        job: The job, changed in place.
        current: The approved round.
        runner: The run/sandbox runner.
        exploits_path: The suite folder.
    """
    assert current.fix is not None
    runner.apply_fix(fix_diff=current.fix.diff)
    confirmed = _confirmed(current)
    for exploit in confirmed:
        folder = exploits_path / exploit.id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "patch.diff").write_text(exploit.patch_diff, encoding="utf-8")
        (folder / "why.md").write_text(exploit.why_md, encoding="utf-8")
    current.status = "fixed"
    job.suite_count += len(confirmed)
    job.fixed_count += len(confirmed)
    if job.round >= job.rounds:
        job.status = "done"
        _release(job)


def _needs_review(exploit: HardeningExploit) -> bool:
    """Tell whether the user must give a verdict on an exploit.

    Args:
        exploit: The judged claim.

    Returns:
        True for a claim the judge labels an exploit, or labels without enough confidence.
    """
    return exploit.label == "exploit" or exploit.unsure


def _confirmed(current: HardeningRound) -> list[HardeningExploit]:
    """Return the exploits the user confirmed in a round.

    Args:
        current: The round.

    Returns:
        The confirmed exploits, in order.
    """
    return [e for e in current.exploits if e.user_verdict == "confirmed"]


def _result(passed: bool) -> Literal["pass", "fail"]:
    """Turn a check outcome into its contract value.

    Args:
        passed: Whether the check passed.

    Returns:
        "pass" or "fail".
    """
    return "pass" if passed else "fail"


def _round_budget(job: HardeningJob, round_index: int) -> float | None:
    """Return what the job may still spend up to the end of a round.

    Unspent money of an earlier round carries over to the later ones.

    Args:
        job: The job.
        round_index: The 1-based round.

    Returns:
        The dollars left, or None for a local job, which is free.
    """
    if job.place == "local":
        return None
    return round_index * job.round_cap_usd - job.spent_usd


def _spend(job: HardeningJob, cost: float) -> None:
    """Add a runner call's cost to a cloud job.

    Args:
        job: The job, changed in place.
        cost: The dollars the call spent.
    """
    if job.place == "cloud":
        job.spent_usd += max(0.0, float(cost))


def _budget_stop(job: HardeningJob) -> None:
    """Stop a job that has no money left. This is a clean end, not an error.

    Args:
        job: The job, changed in place.
    """
    job.status = "budget_stop"
    _release(job)


def _reserve(job: HardeningJob) -> None:
    """Reserve every round's cap for a cloud job (D46). Local work is free.

    Args:
        job: The job, changed in place.
    """
    job.reserved_usd = job.rounds * job.round_cap_usd if job.place == "cloud" else 0.0


def _release(job: HardeningJob) -> None:
    """Release the unspent part of the reservation.

    Args:
        job: The job, changed in place.
    """
    job.reserved_usd = job.spent_usd

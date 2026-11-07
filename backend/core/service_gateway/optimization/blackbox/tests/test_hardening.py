"""Tests for the metric-hardening attack loop, its suite and its fix checks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.constants import HARDENING_DEFAULT_ROUND_CAP_USD, HARDENING_DEFAULT_ROUNDS
from core.models.hardening import HardeningExploit, HardeningJob
from core.notifications.catalog import HARDENING_DONE_EVENT, HARDENING_REVIEW_EVENT
from core.service_gateway.optimization.blackbox.classification import ClassificationError
from core.service_gateway.optimization.blackbox.hardening import (
    Candidate,
    ExploitNotFoundError,
    FixChecksFailedError,
    HardeningStateError,
    decide_fix,
    exploit_passes,
    new_job,
    notification_event,
    parse_verdict,
    record_verdict,
    retry,
    run_until_wait,
)

_CASE = Candidate(kind="case", patch_diff="--- a/out\n+++ b/out\n+A A A\n", why_md="Repeats the keyword.")
_METHOD = Candidate(kind="method", patch_diff="", why_md="Pads the answer with every label.")
_EXPLOIT = {"label": "exploit", "confidence": 0.95, "reason": "Games the keyword count."}
_UNSURE = {"label": "exploit", "confidence": 0.4, "reason": "Maybe a real gain."}
_VALID = {"label": "valid", "confidence": 0.9, "reason": "A real improvement."}


class FakeRunner:
    """A run/sandbox runner that replays scripted rounds and records each call."""

    def __init__(
        self,
        rounds: list[list[tuple[Candidate, Any]]],
        *,
        attack_cost: float = 0.5,
        judge_cost: float = 0.1,
        fix_cost: float = 0.2,
        preflight_failures: tuple[str, ...] = (),
        fix_holds: bool = True,
        fix_moves_base: bool = False,
        fix_lowers_reference: bool = False,
        fail_on: str | None = None,
    ) -> None:
        """Script the runner.

        Args:
            rounds: For each round, the attacker's candidates with the judge's raw output.
            attack_cost: Dollars each attack call spends.
            judge_cost: Dollars each judge call spends.
            fix_cost: Dollars each fix draft spends.
            preflight_failures: What the preflight reports.
            fix_holds: Whether exploits score at most the base score once the fix is applied.
            fix_moves_base: Whether the fix changes the base score.
            fix_lowers_reference: Whether the fix lowers the reference score.
            fail_on: A method name that raises instead of answering.
        """
        self.rounds = rounds
        self.attack_cost = attack_cost
        self.judge_cost = judge_cost
        self.fix_cost = fix_cost
        self.preflight_failures = list(preflight_failures)
        self.fix_holds = fix_holds
        self.fix_moves_base = fix_moves_base
        self.fix_lowers_reference = fix_lowers_reference
        self.fail_on = fail_on
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.judged: dict[str, Any] = {}

    def _call(self, name: str, **kwargs: Any) -> None:
        """Record a call and raise when the script says so.

        Args:
            name: The method name.
            **kwargs: The call's arguments.

        Raises:
            RuntimeError: When ``name`` is the scripted failure.
        """
        self.calls.append((name, kwargs))
        if name == self.fail_on:
            raise RuntimeError(f"{name} broke")

    def preflight(self, *, view: str) -> list[str]:
        """Return the scripted preflight failures.

        Args:
            view: The attacker's view.

        Returns:
            The failures.
        """
        self._call("preflight", view=view)
        return self.preflight_failures

    def attack(
        self, *, round_index: int, view: str, model: str, exploits_path: Path, budget_usd: float | None
    ) -> tuple[list[Candidate], float]:
        """Return the scripted round's candidates.

        Args:
            round_index: The 1-based round.
            view: The attacker's view.
            model: The attacker model.
            exploits_path: The suite folder.
            budget_usd: The dollars left, or None when local.

        Returns:
            The candidates and the dollars spent.
        """
        self._call(
            "attack",
            round_index=round_index,
            view=view,
            model=model,
            exploits_path=exploits_path,
            budget_usd=budget_usd,
        )
        scripted = self.rounds[round_index - 1] if round_index <= len(self.rounds) else []
        for candidate, raw in scripted:
            self.judged[candidate.why_md] = raw
        return [candidate for candidate, _raw in scripted], self.attack_cost

    def judge(self, *, candidate: Candidate, model: str, budget_usd: float | None) -> tuple[Any, float]:
        """Return the scripted judge output for ``candidate``.

        Args:
            candidate: The attacker's candidate.
            model: The judge model.
            budget_usd: The dollars left, or None when local.

        Returns:
            The raw judge output and the dollars spent.
        """
        self._call("judge", candidate=candidate, model=model, budget_usd=budget_usd)
        return self.judged[candidate.why_md], self.judge_cost

    def draft_fix(
        self, *, exploits: list[HardeningExploit], model: str, exploits_path: Path, budget_usd: float | None
    ) -> tuple[str, float]:
        """Return a fix diff.

        Args:
            exploits: The confirmed exploits.
            model: The model that drafts the fix.
            exploits_path: The suite folder.
            budget_usd: The dollars left, or None when local.

        Returns:
            The diff and the dollars spent.
        """
        self._call("draft_fix", exploits=exploits, model=model, budget_usd=budget_usd)
        drafts = self.names().count("draft_fix")
        return f"--- a/metric.py\n+++ b/metric.py\n+cap repeats {drafts}\n", self.fix_cost

    def score(self, *, fix_diff: str | None, exploit: HardeningExploit | None) -> float:
        """Score the base solution or an exploit, with or without the fix.

        Args:
            fix_diff: The fix to apply first, if any.
            exploit: The exploit to replay, or None for the base solution.

        Returns:
            The metric score.
        """
        self._call("score", fix_diff=fix_diff, exploit=exploit)
        if exploit is None:
            return 0.7 if fix_diff and self.fix_moves_base else 0.8
        if fix_diff and self.fix_holds:
            return 0.3
        return 1.0

    def reference_score(self, *, fix_diff: str | None, reference: Path) -> float:
        """Score the reference solution.

        Args:
            fix_diff: The fix to apply first, if any.
            reference: The reference solution.

        Returns:
            The metric score.
        """
        self._call("reference_score", fix_diff=fix_diff, reference=reference)
        return 0.5 if fix_diff and self.fix_lowers_reference else 0.9

    def apply_fix(self, *, fix_diff: str) -> None:
        """Record that the approved fix is applied to the metric.

        Args:
            fix_diff: The approved fix.
        """
        self._call("apply_fix", fix_diff=fix_diff)

    def names(self) -> list[str]:
        """Return the called method names in order.

        Returns:
            The names.
        """
        return [name for name, _kwargs in self.calls]


def _job(place: str = "cloud", **overrides: Any) -> HardeningJob:
    """Build a new job with test models.

    Args:
        place: Where the job runs.
        **overrides: Other ``new_job`` arguments.

    Returns:
        The job.
    """
    kwargs: dict[str, Any] = {"attacker_model": "openai/attacker", "judge_model": "openai/judge", "place": place}
    kwargs.update(overrides)
    return new_job(**kwargs)


def _run(job: HardeningJob, runner: FakeRunner, tmp_path: Path, reference: Path | None = None) -> HardeningJob:
    """Advance ``job`` until it waits for the user or ends.

    Args:
        job: The job.
        runner: The fake runner.
        tmp_path: The test folder; the suite goes under it.
        reference: The optional reference solution.

    Returns:
        The advanced job.
    """
    return run_until_wait(job, runner=runner, exploits_path=tmp_path / "exploits", reference=reference)


def _confirm_all(job: HardeningJob) -> HardeningJob:
    """Confirm every exploit of the current round that waits for a verdict.

    Args:
        job: The job.

    Returns:
        The job after the verdicts.
    """
    for exploit in job.rounds_detail[-1].exploits:
        if exploit.label == "exploit" or exploit.unsure:
            job = record_verdict(job, exploit.id, confirmed=True)
    return job


def test_new_job_uses_defaults_and_reserves_every_round_in_the_cloud() -> None:
    """A cloud job reserves N times the round cap, and stores the black-box view by default."""
    job = _job()

    assert job.status == "preflight"
    assert (job.rounds, job.round_cap_usd) == (HARDENING_DEFAULT_ROUNDS, HARDENING_DEFAULT_ROUND_CAP_USD) == (5, 2.0)
    assert job.reserved_usd == 10.0
    assert job.spent_usd == 0.0
    assert job.view == "black_box"
    assert (job.round, job.suite_count, job.fixed_count) == (0, 0, 0)
    assert job.rounds_detail == []
    assert job.preflight_failures == []
    assert job.error is None


def test_local_job_reserves_nothing_and_gives_no_budget(tmp_path: Path) -> None:
    """Local work is free, so the runner gets no budget and the job never budget-stops."""
    runner = FakeRunner([[(_CASE, _VALID)]], attack_cost=50.0)
    job = _run(_job(place="local", view="white_box"), runner, tmp_path)

    assert job.reserved_usd == 0.0
    assert job.status == "clean_stop"
    attack = dict(runner.calls)["attack"]
    assert attack["budget_usd"] is None
    assert attack["view"] == "white_box"


def test_preflight_failure_stops_before_any_attack_and_releases(tmp_path: Path) -> None:
    """A failed preflight lists its failures, releases the reservation and never attacks."""
    runner = FakeRunner([], preflight_failures=("metric.missing",))
    job = _run(_job(), runner, tmp_path)

    assert job.status == "preflight_failed"
    assert job.preflight_failures == ["metric.missing"]
    assert job.reserved_usd == job.spent_usd == 0.0
    assert runner.names() == ["preflight"]


def test_round_without_exploit_stops_clean_and_releases_the_rest(tmp_path: Path) -> None:
    """One clean round ends the job early and frees what it did not spend."""
    runner = FakeRunner([[(_CASE, _VALID)]])
    job = _run(_job(), runner, tmp_path)

    assert job.status == "clean_stop"
    assert job.round == 1
    assert job.rounds_detail[0].status == "clean"
    assert job.spent_usd == pytest.approx(0.6)
    assert job.reserved_usd == pytest.approx(job.spent_usd)
    assert notification_event(job) == HARDENING_DONE_EVENT


def test_exploits_wait_for_review_and_low_confidence_is_marked_unsure(tmp_path: Path) -> None:
    """Judged exploits go to the user; one under the confidence bar is unsure."""
    sure = Candidate(kind="case", patch_diff="p1", why_md="sure")
    maybe = Candidate(kind="method", patch_diff="", why_md="maybe")
    runner = FakeRunner([[(sure, _EXPLOIT), (maybe, _UNSURE)]])
    job = _run(_job(attacker_model="openai/a", judge_model="openai/j"), runner, tmp_path)

    assert job.status == "waiting_review"
    assert notification_event(job) == HARDENING_REVIEW_EVENT
    round_one = job.rounds_detail[0]
    assert (round_one.index, round_one.status, round_one.fix) == (1, "waiting_review", None)
    first, second = round_one.exploits
    assert (first.kind, first.label, first.confidence, first.unsure) == ("case", "exploit", 0.95, False)
    assert (first.patch_diff, first.why_md, first.reason) == ("p1", "sure", "Games the keyword count.")
    assert (second.kind, second.unsure, second.user_verdict) == ("method", True, None)
    assert first.id != second.id
    assert dict(runner.calls)["attack"]["model"] == "openai/a"
    assert dict(runner.calls)["judge"]["model"] == "openai/j"


def test_full_round_drafts_a_fix_that_passes_every_check_and_writes_the_suite(tmp_path: Path) -> None:
    """Confirmed exploits get a fix; approval writes the suite and starts the next round."""
    runner = FakeRunner([[(_CASE, _EXPLOIT), (_METHOD, _EXPLOIT)], [(_CASE, _VALID)]])
    reference = tmp_path / "reference.txt"
    job = _confirm_all(_run(_job(), runner, tmp_path, reference))
    assert job.status == "running"

    job = _run(job, runner, tmp_path, reference)
    assert job.status == "waiting_fix"
    assert notification_event(job) == HARDENING_REVIEW_EVENT
    fix = job.rounds_detail[0].fix
    assert fix is not None
    assert fix.diff.startswith("--- a/metric.py")
    assert fix.checks.model_dump() == {
        "suite_replay": "pass",
        "base_score_unchanged": "pass",
        "reference_not_lower": "pass",
    }
    assert fix.user_decision is None
    replayed = [kw["exploit"].kind for name, kw in runner.calls if name == "score" and kw["exploit"] is not None]
    assert sorted(replayed) == ["case", "method"]

    job = _run(decide_fix(job, approve=True), runner, tmp_path, reference)
    assert job.status == "clean_stop"
    assert job.rounds_detail[0].status == "fixed"
    assert job.rounds_detail[0].fix is not None
    assert job.rounds_detail[0].fix.user_decision == "approved"
    assert (job.suite_count, job.fixed_count) == (2, 2)
    assert ("apply_fix", {"fix_diff": fix.diff}) in runner.calls
    for exploit in job.rounds_detail[0].exploits:
        folder = tmp_path / "exploits" / exploit.id
        assert (folder / "patch.diff").read_text(encoding="utf-8") == exploit.patch_diff
        assert (folder / "why.md").read_text(encoding="utf-8") == exploit.why_md


def test_reference_check_is_null_without_a_reference(tmp_path: Path) -> None:
    """With no reference solution, the reference check is not run."""
    runner = FakeRunner([[(_CASE, _EXPLOIT)]])
    job = _run(_confirm_all(_run(_job(), runner, tmp_path)), runner, tmp_path)

    fix = job.rounds_detail[0].fix
    assert fix is not None
    assert fix.checks.reference_not_lower is None
    assert "reference_score" not in runner.names()


@pytest.mark.parametrize(
    ("script", "failed_check"),
    [
        ({"fix_holds": False}, "suite_replay"),
        ({"fix_moves_base": True}, "base_score_unchanged"),
        ({"fix_lowers_reference": True}, "reference_not_lower"),
    ],
)
def test_a_failed_check_blocks_approval(tmp_path: Path, script: dict[str, bool], failed_check: str) -> None:
    """A fix that fails any auto-check cannot be approved; it can be rejected."""
    runner = FakeRunner([[(_CASE, _EXPLOIT)]], **script)
    reference = tmp_path / "reference.txt"
    job = _run(_confirm_all(_run(_job(), runner, tmp_path, reference)), runner, tmp_path, reference)

    fix = job.rounds_detail[0].fix
    assert fix is not None
    assert getattr(fix.checks, failed_check) == "fail"
    with pytest.raises(FixChecksFailedError):
        decide_fix(job, approve=True)

    rejected = decide_fix(job, approve=False)
    assert rejected.status == "rejected"
    assert rejected.rounds_detail[0].status == "rejected"
    assert rejected.reserved_usd == pytest.approx(rejected.spent_usd)
    assert notification_event(rejected) is None


def test_retry_after_a_rejected_fix_drafts_the_fix_again(tmp_path: Path) -> None:
    """Retry puts the round back to drafting, and reserves the rounds again."""
    runner = FakeRunner([[(_CASE, _EXPLOIT)]], fix_holds=False)
    job = _run(_confirm_all(_run(_job(), runner, tmp_path)), runner, tmp_path)
    job = retry(decide_fix(job, approve=False))

    assert job.status == "running"
    assert job.reserved_usd == 10.0
    assert job.rounds_detail[0].status == "running"
    assert job.rounds_detail[0].fix is None
    job = _run(job, runner, tmp_path)
    assert job.status == "waiting_fix"
    assert runner.names().count("draft_fix") == 2


def test_rejecting_every_exploit_counts_as_a_clean_round(tmp_path: Path) -> None:
    """When the user rejects every exploit, the metric held, so the job stops clean."""
    runner = FakeRunner([[(_CASE, _EXPLOIT)]])
    job = _run(_job(), runner, tmp_path)
    job = record_verdict(job, job.rounds_detail[0].exploits[0].id, confirmed=False)

    assert job.status == "clean_stop"
    assert job.rounds_detail[0].status == "clean"
    assert job.rounds_detail[0].exploits[0].user_verdict == "rejected"
    assert job.reserved_usd == pytest.approx(job.spent_usd)


def test_verdict_errors() -> None:
    """A verdict needs a job that waits for review and an exploit of its current round."""
    with pytest.raises(HardeningStateError):
        record_verdict(_job(), "x", confirmed=True)


def test_unknown_exploit_is_refused(tmp_path: Path) -> None:
    """A verdict for an exploit the round does not have is refused."""
    job = _run(_job(), FakeRunner([[(_CASE, _EXPLOIT)]]), tmp_path)
    with pytest.raises(ExploitNotFoundError):
        record_verdict(job, "missing", confirmed=True)


def test_fix_decision_needs_a_waiting_fix() -> None:
    """A fix decision on a job without a drafted fix is refused."""
    with pytest.raises(HardeningStateError):
        decide_fix(_job(), approve=True)


def test_budget_stop_ends_the_job_cleanly(tmp_path: Path) -> None:
    """Running out of the round's money stops the job without an error."""
    first = Candidate(kind="case", patch_diff="p1", why_md="one")
    second = Candidate(kind="case", patch_diff="p2", why_md="two")
    runner = FakeRunner([[(first, _EXPLOIT), (second, _EXPLOIT)]], attack_cost=1.85, judge_cost=0.2)
    job = _run(_job(rounds=1), runner, tmp_path)

    assert job.status == "budget_stop"
    assert job.error is None
    assert runner.names().count("judge") == 1
    assert job.reserved_usd == pytest.approx(job.spent_usd) == pytest.approx(2.05)
    assert notification_event(job) == HARDENING_DONE_EVENT


def test_job_stops_after_n_rounds(tmp_path: Path) -> None:
    """After the last round's fix is approved and written, the job is done."""
    runner = FakeRunner([[(_CASE, _EXPLOIT)], [(_METHOD, _EXPLOIT)], [(_CASE, _EXPLOIT)]])
    job = _job(rounds=2)
    for _ in range(2):
        job = _run(_confirm_all(_run(job, runner, tmp_path)), runner, tmp_path)
        job = _run(decide_fix(job, approve=True), runner, tmp_path) if job.round == 2 else decide_fix(job, approve=True)

    assert job.status == "done"
    assert job.round == 2
    assert [r.status for r in job.rounds_detail] == ["fixed", "fixed"]
    assert runner.names().count("attack") == 2
    assert (job.suite_count, job.fixed_count) == (2, 2)
    assert job.reserved_usd == pytest.approx(job.spent_usd)


def test_later_fix_replays_the_whole_suite(tmp_path: Path) -> None:
    """The second round's fix replays the first round's exploit too."""
    runner = FakeRunner([[(_CASE, _EXPLOIT)], [(_METHOD, _EXPLOIT)]])
    job = _run(_confirm_all(_run(_job(rounds=2), runner, tmp_path)), runner, tmp_path)
    job = _run(_confirm_all(_run(decide_fix(job, approve=True), runner, tmp_path)), runner, tmp_path)

    assert job.status == "waiting_fix"
    last_fix = job.rounds_detail[1].fix
    assert last_fix is not None
    replayed = [
        kw["exploit"].why_md
        for name, kw in runner.calls
        if name == "score" and kw["exploit"] is not None and kw["fix_diff"] == last_fix.diff
    ]
    assert sorted(replayed) == sorted([_CASE.why_md, _METHOD.why_md])


def test_runner_error_fails_the_job_and_retry_resumes(tmp_path: Path) -> None:
    """A runner error fails the job with its message; retry goes on from the same place."""
    runner = FakeRunner([[(_CASE, _VALID)]], fail_on="attack")
    job = _run(_job(), runner, tmp_path)

    assert job.status == "failed"
    assert job.error == "attack broke"
    assert job.reserved_usd == pytest.approx(job.spent_usd)
    runner.fail_on = None
    job = _run(retry(job), runner, tmp_path)
    assert job.status == "clean_stop"
    assert job.error is None


def test_retry_after_preflight_failure_runs_the_preflight_again(tmp_path: Path) -> None:
    """Retry after a failed preflight clears the failures and checks again."""
    runner = FakeRunner([[(_CASE, _VALID)]], preflight_failures=("metric.missing",))
    job = _run(_job(), runner, tmp_path)
    runner.preflight_failures = []
    job = retry(job)

    assert (job.status, job.preflight_failures) == ("preflight", [])
    assert _run(job, runner, tmp_path).status == "clean_stop"


def test_retry_refuses_an_active_job() -> None:
    """Only a failed, stale or rejected job can be retried."""
    with pytest.raises(HardeningStateError):
        retry(_job())


def test_suite_pass_rule() -> None:
    """An exploit passes when it scores no higher than the base score."""
    assert exploit_passes(0.8, 0.8) is True
    assert exploit_passes(0.2, 0.8) is True
    assert exploit_passes(0.81, 0.8) is False


def test_parse_verdict_reads_a_mapping_or_json_text() -> None:
    """The judge output may come as a mapping or as JSON text."""
    assert parse_verdict(_EXPLOIT) == parse_verdict(json.dumps(_EXPLOIT))
    verdict = parse_verdict({"label": "valid", "confidence": 1, "reason": " ok "})
    assert (verdict.label, verdict.confidence, verdict.reason) == ("valid", 1.0, "ok")


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        {"label": "maybe", "confidence": 0.5, "reason": "x"},
        {"label": "exploit", "confidence": 1.5, "reason": "x"},
        {"label": "exploit", "confidence": True, "reason": "x"},
        {"label": "exploit", "confidence": 0.5},
        ["exploit"],
    ],
)
def test_parse_verdict_refuses_bad_output(raw: Any) -> None:
    """Judge output outside the schema is refused, so the job fails instead of guessing."""
    with pytest.raises(ClassificationError):
        parse_verdict(raw)


def test_bad_judge_output_fails_the_job(tmp_path: Path) -> None:
    """The loop does not invent a label for unreadable judge output."""
    job = _run(_job(), FakeRunner([[(_CASE, "garbage")]]), tmp_path)
    assert job.status == "failed"
    assert job.error

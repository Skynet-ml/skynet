"""Tests for the metric-hardening attack loop, its suite and its fix checks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.constants import HARDENING_DEFAULT_ROUND_CAP_USD, HARDENING_DEFAULT_ROUNDS
from core.models.hardening import HardeningExploit, HardeningJob, HardeningNoise, HardeningSession
from core.service_gateway.optimization.blackbox.classification import ClassificationError
from core.service_gateway.optimization.blackbox.hardening import (
    AttackResult,
    Candidate,
    ExploitNotFoundError,
    FixChecksFailedError,
    HardeningStateError,
    decide_fix,
    exploit_passes,
    new_job,
    parse_verdict,
    record_verdict,
    retry,
    run_until_wait,
    stop,
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
        noise: HardeningNoise | None = None,
        noise_cost: float = 0.0,
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
            noise: What the noise check returns; a skipped check when None.
            noise_cost: Dollars the noise check spends.
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
        self.noise = noise if noise is not None else HardeningNoise(skipped=True, reason="Deterministic.")
        self.noise_cost = noise_cost
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
        self,
        *,
        round_index: int,
        view: str,
        model: str,
        exploits_path: Path,
        budget_usd: float | None,
        sessions: int,
    ) -> AttackResult:
        """Return the scripted round's candidates, with one brief and one session for each session.

        Args:
            round_index: The 1-based round.
            view: The attacker's view.
            model: The attacker model.
            exploits_path: The suite folder.
            budget_usd: The dollars left, or None when local.
            sessions: K.

        Returns:
            The candidates, the dollars spent, the briefs and the sessions.
        """
        self._call(
            "attack",
            round_index=round_index,
            view=view,
            model=model,
            exploits_path=exploits_path,
            budget_usd=budget_usd,
            sessions=sessions,
        )
        scripted = self.rounds[round_index - 1] if round_index <= len(self.rounds) else []
        for candidate, raw in scripted:
            self.judged[candidate.why_md] = raw
        briefs = [f"brief {i + 1}" for i in range(sessions)]
        return AttackResult(
            candidates=[candidate for candidate, _raw in scripted],
            cost_usd=self.attack_cost,
            briefs=briefs,
            sessions=[HardeningSession(index=i + 1, brief=b, status="done") for i, b in enumerate(briefs)],
        )

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
        self,
        *,
        exploits: list[HardeningExploit],
        model: str,
        exploits_path: Path,
        budget_usd: float | None,
        notes: list[str],
    ) -> tuple[str, float]:
        """Return a fix diff.

        Args:
            exploits: The confirmed exploits.
            model: The model that drafts the fix.
            exploits_path: The suite folder.
            budget_usd: The dollars left, or None when local.
            notes: The user's notes on rejected drafts.

        Returns:
            The diff and the dollars spent.
        """
        self._call("draft_fix", exploits=exploits, model=model, budget_usd=budget_usd, notes=notes)
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

    def noise_check(self, *, model: str, budget_usd: float | None) -> tuple[HardeningNoise, float]:
        """Return the scripted noise result.

        Args:
            model: The model that decides.
            budget_usd: The dollars left, or None when local.

        Returns:
            The noise result and the dollars spent.
        """
        self._call("noise_check", model=model, budget_usd=budget_usd)
        return self.noise, self.noise_cost

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


def test_exploits_wait_for_review_and_low_confidence_is_marked_unsure(tmp_path: Path) -> None:
    """Judged exploits go to the user; one under the confidence bar is unsure."""
    sure = Candidate(kind="case", patch_diff="p1", why_md="sure")
    maybe = Candidate(kind="method", patch_diff="", why_md="maybe")
    runner = FakeRunner([[(sure, _EXPLOIT), (maybe, _UNSURE)]])
    job = _run(_job(attacker_model="openai/a", judge_model="openai/j"), runner, tmp_path)

    assert job.status == "waiting_review"
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
    fix = job.rounds_detail[0].fix
    assert fix is not None
    assert fix.diff.startswith("--- a/metric.py")
    assert fix.checks.model_dump() == {
        "suite_replay": "pass",
        "base_score_unchanged": "pass",
        "reference_not_lower": "pass",
    }
    assert fix.user_decision is None
    replayed = [
        kw["exploit"].kind
        for name, kw in runner.calls
        if name == "score" and kw["exploit"] is not None and kw["fix_diff"]
    ]
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


@pytest.mark.parametrize("bad_judge", ["timeout", "parse"])
def test_retry_after_a_judge_failure_attacks_again_instead_of_drafting(tmp_path: Path, bad_judge: str) -> None:
    """A round that failed while judging has no verdicts, so a retry runs the round again and drafts no fix."""
    runner = FakeRunner([[(_CASE, "garbage" if bad_judge == "parse" else _EXPLOIT)]])
    runner.fail_on = "judge" if bad_judge == "timeout" else None
    job = _run(_job(), runner, tmp_path)
    assert job.status == "failed"
    assert job.rounds_detail[-1].status == "running"

    runner.fail_on = None
    runner.rounds = [[(_CASE, _EXPLOIT)]]
    job = _run(retry(job), runner, tmp_path)

    assert "draft_fix" not in runner.names()
    assert runner.names().count("attack") == 2
    assert job.status == "waiting_review"
    assert job.round == 1
    assert [r.index for r in job.rounds_detail] == [1]
    assert len(job.rounds_detail[0].exploits) == 1


def _seed_suite(path: Path, exploit_id: str) -> None:
    """Write one exploit of an earlier job into the suite folder.

    Args:
        path: The suite folder.
        exploit_id: The exploit's folder name.
    """
    folder = path / exploit_id
    folder.mkdir(parents=True)
    (folder / "patch.diff").write_text("+old trick\n", encoding="utf-8")
    (folder / "why.md").write_text("An earlier exploit.", encoding="utf-8")


class ReopeningRunner(FakeRunner):
    """A runner whose fix makes one saved exploit pay again."""

    def score(self, *, fix_diff: str | None, exploit: HardeningExploit | None) -> float:
        """Score like the fake runner, but the fixed metric still pays the old exploit.

        Args:
            fix_diff: The fix to apply first, if any.
            exploit: The exploit to replay, or None for the base solution.

        Returns:
            The metric score.
        """
        if exploit is not None and exploit.id == "old-1":
            self._call("score", fix_diff=fix_diff, exploit=exploit)
            return 1.0
        return super().score(fix_diff=fix_diff, exploit=exploit)


def test_suite_replay_includes_exploits_that_earlier_jobs_saved(tmp_path: Path) -> None:
    """A fix that reopens an exploit from an earlier job fails the suite replay."""
    _seed_suite(tmp_path / "exploits", "old-1")
    runner = ReopeningRunner([[(_CASE, _EXPLOIT)]])
    job = _run(_job(), runner, tmp_path)
    assert job.suite_count == 1

    job = _run(_confirm_all(job), runner, tmp_path)

    assert job.status == "waiting_fix"
    replayed = [kw["exploit"].id for name, kw in runner.calls if name == "score" and kw["exploit"] is not None]
    assert "old-1" in replayed
    assert job.rounds_detail[-1].fix is not None
    assert job.rounds_detail[-1].fix.checks.suite_replay == "fail"
    with pytest.raises(FixChecksFailedError):
        decide_fix(job, approve=True)


def test_saved_exploits_count_in_the_suite_after_a_fix(tmp_path: Path) -> None:
    """The suite count covers the earlier jobs' exploits and the new ones."""
    _seed_suite(tmp_path / "exploits", "old-1")
    runner = FakeRunner([[(_CASE, _EXPLOIT)]])
    job = _run(_confirm_all(_run(_job(rounds=1), runner, tmp_path)), runner, tmp_path)
    assert job.rounds_detail[-1].fix is not None
    assert job.rounds_detail[-1].fix.checks.suite_replay == "pass"

    job = _run(decide_fix(job, approve=True), runner, tmp_path)

    assert job.status == "done"
    assert job.suite_count == 2
    assert job.fixed_count == 1


def test_retry_after_a_suite_write_failure_applies_the_fix_once(tmp_path: Path) -> None:
    """The suite writes run before the fix, so a retry never applies the fix twice."""
    runner = FakeRunner([[(_CASE, _EXPLOIT)]])
    blocked = tmp_path / "blocked"
    blocked.write_text("not a folder", encoding="utf-8")
    job = _confirm_all(run_until_wait(_job(rounds=1), runner=runner, exploits_path=blocked, reference=None))
    job = run_until_wait(job, runner=runner, exploits_path=blocked, reference=None)
    job = run_until_wait(decide_fix(job, approve=True), runner=runner, exploits_path=blocked, reference=None)
    assert job.status == "failed"
    assert "apply_fix" not in runner.names()

    job = _run(retry(job), runner, tmp_path)

    assert job.status == "done"
    assert runner.names().count("apply_fix") == 1
    assert job.rounds_detail[-1].status == "fixed"


def test_retry_after_a_failed_apply_writes_the_suite_again_safely(tmp_path: Path) -> None:
    """A failed apply leaves the suite files; the retry rewrites them in place and applies the fix."""
    runner = FakeRunner([[(_CASE, _EXPLOIT)]])
    job = _run(_confirm_all(_run(_job(rounds=1), runner, tmp_path)), runner, tmp_path)
    runner.fail_on = "apply_fix"
    job = _run(decide_fix(job, approve=True), runner, tmp_path)
    assert job.status == "failed"

    runner.fail_on = None
    job = _run(retry(job), runner, tmp_path)

    assert job.status == "done"
    assert runner.names().count("apply_fix") == 2
    assert job.suite_count == 1
    assert len(list((tmp_path / "exploits").glob("*/patch.diff"))) == 1


class TimeoutPreflightRunner(FakeRunner):
    """A runner whose first preflight raises a timeout."""

    def preflight(self, **kwargs: Any) -> Any:
        """Raise a timeout once, then run the scripted preflight.

        Args:
            **kwargs: The preflight arguments.

        Returns:
            The scripted preflight failures.

        Raises:
            TimeoutError: On the first call.
        """
        if not any(name == "preflight" for name in self.names()):
            self.calls.append(("preflight", kwargs))
            raise TimeoutError("preflight timed out")
        return super().preflight(**kwargs)


def test_retry_after_a_preflight_error_runs_the_preflight_before_any_attack(tmp_path: Path) -> None:
    """A preflight that raised never passed, so retry checks again before a paid attack."""
    runner = TimeoutPreflightRunner([[(_CASE, _VALID)]])
    job = _run(_job(), runner, tmp_path)

    assert (job.status, job.error, runner.names()) == ("failed", "preflight timed out", ["preflight"])
    job = retry(job)
    assert job.status == "preflight"
    job = _run(job, runner, tmp_path)
    names = runner.names()
    assert names.count("preflight") == 2
    assert names.index("attack") > names.index("preflight", 1)
    assert job.status == "clean_stop"


_EDGE = [{"type": "Text", "text": "The metric counts keywords."}]


def test_each_exploit_stores_its_scores_edge_case_session_and_the_round_briefs(tmp_path: Path) -> None:
    """Each claim gets its score and the base score on the current metric; the round keeps K briefs (SKY-682)."""
    claim = Candidate(kind="case", patch_diff="p1", why_md="w1", edge_case=_EDGE, session=2)
    runner = FakeRunner([[(claim, _EXPLOIT)]])
    job = _run(_job(parallel_sessions=3), runner, tmp_path)

    current = job.rounds_detail[0]
    assert current.briefs == ["brief 1", "brief 2", "brief 3"]
    assert [s.index for s in current.sessions] == [1, 2, 3]
    exploit = current.exploits[0]
    assert (exploit.exploit_score, exploit.base_score) == (1.0, 0.8)
    assert (exploit.edge_case, exploit.session) == (_EDGE, 2)
    assert dict(runner.calls)["attack"]["sessions"] == 3


class BrokenScoreRunner(FakeRunner):
    """A runner whose scorer fails on every unfixed score."""

    def score(self, *, fix_diff: str | None, exploit: HardeningExploit | None) -> float:
        """Fail on scores without a fix.

        Args:
            fix_diff: The fix to apply first, if any.
            exploit: The exploit to replay, or None for the base solution.

        Returns:
            The metric score with a fix.

        Raises:
            RuntimeError: When there is no fix.
        """
        if fix_diff is None:
            raise RuntimeError("box broke")
        return super().score(fix_diff=fix_diff, exploit=exploit)


def test_a_scorer_failure_leaves_the_card_scores_null(tmp_path: Path) -> None:
    """The card scores only inform the user, so a scorer error does not fail the round."""
    job = _run(_job(), BrokenScoreRunner([[(_CASE, _EXPLOIT)]]), tmp_path)

    assert job.status == "waiting_review"
    exploit = job.rounds_detail[0].exploits[0]
    assert (exploit.exploit_score, exploit.base_score) == (None, None)


def test_no_claims_means_no_base_score_run(tmp_path: Path) -> None:
    """A round without claims spends no sandbox run on the base score."""
    runner = FakeRunner([[]])
    job = _run(_job(), runner, tmp_path)

    assert job.status == "clean_stop"
    assert "score" not in runner.names()


def test_reject_with_a_note_drafts_the_fix_again_with_the_note(tmp_path: Path) -> None:
    """A reject with a note sends the round back to drafting, and the next draft reads every note."""
    runner = FakeRunner([[(_CASE, _EXPLOIT)]])
    job = _run(_confirm_all(_run(_job(), runner, tmp_path)), runner, tmp_path)
    assert job.status == "waiting_fix"

    job = decide_fix(job, approve=False, note="  Keep the old format.  ")
    assert (job.status, job.rounds_detail[0].status, job.rounds_detail[0].fix) == ("running", "running", None)
    assert job.rounds_detail[0].fix_notes == ["Keep the old format."]
    job = _run(job, runner, tmp_path)
    assert job.status == "waiting_fix"
    drafts = [kw["notes"] for name, kw in runner.calls if name == "draft_fix"]
    assert drafts == [[], ["Keep the old format."]]


def test_reject_with_a_blank_note_rejects_the_job(tmp_path: Path) -> None:
    """A blank note is no note: the reject ends the job as before."""
    runner = FakeRunner([[(_CASE, _EXPLOIT)]])
    job = _run(_confirm_all(_run(_job(), runner, tmp_path)), runner, tmp_path)

    assert decide_fix(job, approve=False, note="  ").status == "rejected"


@pytest.mark.parametrize("confirm", [False, True])
def test_stop_ends_a_waiting_job_clean_and_releases_the_rest(tmp_path: Path, confirm: bool) -> None:
    """Stop works on a job that waits for a review or a fix decision."""
    runner = FakeRunner([[(_CASE, _EXPLOIT)]])
    job = _run(_job(), runner, tmp_path)
    if confirm:
        job = _run(_confirm_all(job), runner, tmp_path)
    assert job.status == ("waiting_fix" if confirm else "waiting_review")

    stopped = stop(job)
    assert (stopped.status, stopped.rounds_detail[-1].status) == ("clean_stop", "stopped")
    assert stopped.reserved_usd == pytest.approx(stopped.spent_usd)
    assert stopped.noise is None
    assert job.status != "clean_stop"


@pytest.mark.parametrize("status", ["preflight", "running", "done", "failed"])
def test_stop_refuses_a_job_that_does_not_wait(status: str) -> None:
    """A job a worker may hold, or one that ended, cannot stop."""
    with pytest.raises(HardeningStateError):
        stop(_job().model_copy(update={"status": status}))


def _finish_one_round(runner: FakeRunner, tmp_path: Path) -> HardeningJob:
    """Run a one-round job through its approved fix.

    Args:
        runner: The fake runner.
        tmp_path: The test folder.

    Returns:
        The ended job.
    """
    job = _run(_confirm_all(_run(_job(rounds=1), runner, tmp_path)), runner, tmp_path)
    return _run(decide_fix(job, approve=True), runner, tmp_path)


def test_noise_check_runs_once_after_the_last_round(tmp_path: Path) -> None:
    """The last round's approved fix starts the noise check, and the job stores its result and cost."""
    noise = HardeningNoise(skipped=False, reason="The judge is a model.", scores=[0.8, 0.7, 0.8], spread=0.1)
    runner = FakeRunner([[(_CASE, _EXPLOIT)]], noise=noise, noise_cost=0.05)
    job = _finish_one_round(runner, tmp_path)

    assert job.status == "done"
    assert job.noise == noise
    assert runner.names().count("noise_check") == 1
    assert dict(runner.calls)["noise_check"]["model"] == "openai/judge"
    assert job.spent_usd == pytest.approx(0.5 + 0.1 + 0.2 + 0.05)
    assert job.reserved_usd == pytest.approx(job.spent_usd)


def test_a_failed_noise_check_is_skipped_with_its_reason(tmp_path: Path) -> None:
    """A noise check that raises does not fail a job whose rounds all ended."""
    job = _finish_one_round(FakeRunner([[(_CASE, _EXPLOIT)]], fail_on="noise_check"), tmp_path)

    assert job.status == "done"
    assert job.noise == HardeningNoise(skipped=True, reason="The noise check failed: RuntimeError.")


def test_no_budget_skips_the_noise_check(tmp_path: Path) -> None:
    """With no money left in the last round, the noise check is skipped and costs nothing."""
    runner = FakeRunner([[(_CASE, _EXPLOIT)]], attack_cost=1.6, judge_cost=0.2, fix_cost=0.2)
    job = _finish_one_round(runner, tmp_path)

    assert job.status == "done"
    assert job.noise == HardeningNoise(skipped=True, reason="No budget was left for the noise check.")
    assert "noise_check" not in runner.names()

"""Tests for judge sessions (SKY-696).

Each test uses a real job store, a real budget ledger and real setup
evidence over in-memory SQLite. Only the staging of the repository and the
scorer boxes are faked: ``judge_scorer`` yields a staged repository without
a sandbox, and ``_score`` returns fixed scores (the ``dispatched`` fixture
runs the real ``_score`` over a fake staged scorer instead). The fakes count
each paid attempt, so the tests prove that a refused call opens no paid box
and that a retry with the same arguments pays once.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ...billing.budgets import BudgetFencedError, BudgetInsufficientError, BudgetService
from ...billing.runtime import UsagePendingError
from ...billing.signals import BudgetReached
from ...connectors.github_repo import RepoFetchError, RepoRateLimitError
from ...exceptions import InfrastructureInterruptionError, ServiceError
from ...models.blackbox import BlackboxNamedScore
from ...service_gateway.optimization.blackbox.service import FinalRun
from ...storage.models import BillingCustomerModel, ExecutionBudgetModel
from ...storage.preflights import WizardPreflightModel
from ...storage.tests.test_remote_jobstore import SQLiteJobStore
from .. import repo_judge
from ..auth import AuthenticatedUser
from ..errors import DomainError
from ..routers.optimizations import lifecycle
from ..routers.optimizations.lifecycle import cancel_optimization

USER = "alice@example.com"
ADMIN = AuthenticatedUser(username="admin1", role="admin", groups=())
BASE = "a" * 40
GOOD_PATCH = "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-a\n+b\n"
CONTRACT_PATCH = (
    "diff --git a/optimize.toml b/optimize.toml\n--- a/optimize.toml\n+++ b/optimize.toml\n@@ -1 +1 @@\n-a\n+b\n"
)
OTHER_PATCH = "diff --git a/src/b.py b/src/b.py\n--- a/src/b.py\n+++ b/src/b.py\n@@ -1 +1 @@\n-a\n+c\n"
OUTSIDE_PATCH = "diff --git a/other/x.py b/other/x.py\n--- a/other/x.py\n+++ b/other/x.py\n@@ -1 +1 @@\n-a\n+b\n"
THIRD_PATCH = "diff --git a/src/c.py b/src/c.py\n--- a/src/c.py\n+++ b/src/c.py\n@@ -1 +1 @@\n-a\n+d\n"
FOURTH_PATCH = "diff --git a/src/d.py b/src/d.py\n--- a/src/d.py\n+++ b/src/d.py\n@@ -1 +1 @@\n-a\n+e\n"
# Kept before the boxes fixture replaces it, for the tests that score through the real score step.
_REAL_SCORE = repo_judge._score


class _Crash(BaseException):
    """Stop the process in the middle of a score, past every handler of the judge code."""


@pytest.fixture
def store() -> Iterator[SQLiteJobStore]:
    """Provide a job store whose account has a funded wallet.

    Yields:
        The store.
    """
    job_store = SQLiteJobStore()
    with Session(job_store.engine) as session:
        session.add(
            BillingCustomerModel(username=USER, stripe_customer_id="fixture", balance_cents=10_000, grant_remaining=0)
        )
        session.commit()
    yield job_store
    job_store.engine.dispose()


def _charge(attempt: dict[str, Any], cents: int) -> None:
    """Pay for one score through the real ledger, and close the budget at its limit as the run runtime does.

    Args:
        attempt: The scorer arguments of the attempt.
        cents: What the score costs.

    Raises:
        BudgetReached: When the budget cannot hold the cost.
    """
    service = BudgetService(engine=attempt["engine"])
    try:
        operation = service.reserve(
            attempt["budget_id"],
            USER,
            operation_key=attempt["owner_id"],
            generation=attempt["generation"],
            phase="run",
            cost_kind="sandbox",
            request_fingerprint="fp",
            price_snapshot={"version": "v1"},
            max_cents=cents,
        )
    except BudgetInsufficientError as error:
        service.stop_admission(attempt["budget_id"], USER, reason="budget_reached")
        raise BudgetReached(str(error)) from error
    service.mark_dispatched(operation.id, USER)
    service.settle(operation.id, USER, evidence_key="final", actual_cents=cents, evidence={})


@pytest.fixture
def boxes(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the staging and the scorer boxes with counted fakes.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        A mutable record: ``paid`` lists each attempt that reached the paid
        boxes, ``scored`` lists each ``(patch, case count, label)``, ``score``
        maps a patch to its score, ``raise`` makes the next score raise,
        ``hook`` runs once at the start of the next score, and ``charge`` maps
        a label to the cents that one case of that score settles.
    """
    record: dict[str, Any] = {
        "paid": [],
        "scored": [],
        "score": {},
        "raise": None,
        "hook": None,
        "charge": {},
        "staged": [],
    }

    @contextmanager
    def scorer(
        payload: dict[str, Any], *, before_scoring: Any = None, owner_id: str, **kwargs: Any
    ) -> Iterator[tuple[dict[str, Any], Any, Any]]:
        """Stage nothing and check the candidate as the real scorer does, before any paid box.

        Args:
            payload: The session payload.
            before_scoring: The judge's refusal check.
            owner_id: The attempt's owner id.
            **kwargs: The other scorer arguments; the budget ones pay for ``charge``.

        Yields:
            A protected payload, a staged repository and no gateway.
        """
        record["attempt"] = {"owner_id": owner_id, **kwargs}
        record["staged"].append(owner_id)
        protected = {"target": {"repo": {"editable_paths": ["src"]}}, "cases": payload.get("cases") or []}
        staged = SimpleNamespace(
            snapshot=SimpleNamespace(readonly_paths=()),
            protected_paths=(),
            hidden_paths=(),
            exploits_path=".optimize/exploits",
        )
        if before_scoring is not None:
            before_scoring(protected, staged)
        record["paid"].append(owner_id)
        yield protected, staged, None

    def score(_staged: Any, patch: str, cases: list[Any], *, label: str) -> FinalRun:
        """Return the patch's fixed score on each case.

        Args:
            _staged: The staged repository.
            patch: The version.
            cases: The cases.
            label: What the version is.

        Returns:
            The fixed scores.

        Raises:
            BaseException: The error the test set in ``record["raise"]``.
        """
        if record["hook"] is not None:
            hook, record["hook"] = record["hook"], None
            hook()
        if record["charge"].get(label):
            _charge(record["attempt"], record["charge"][label] * max(len(cases), 1))
        if record["raise"] is not None:
            error, record["raise"] = record["raise"], None
            raise error
        record["scored"].append((patch, len(cases), label))
        value = record["score"].get(patch, 0.5)
        return FinalRun(
            score=value,
            feedback=None if cases else "one feedback",
            named={"speed": BlackboxNamedScore(score=value, feedback="fast")},
            cases=[(value, f"case feedback {index}") for index in range(len(cases))],
            runs=max(len(cases), 1),
        )

    def add_record(result: dict[str, Any], _payload: dict[str, Any], _staged: Any, *, image: str | None) -> None:
        """Add a run record as the real helper does.

        Args:
            result: The stored result.
            _payload: The staged payload.
            _staged: The staged repository.
            image: The sandbox image.
        """
        result["details"]["record"] = {"version": 1}

    monkeypatch.setattr(repo_judge, "judge_scorer", scorer)
    monkeypatch.setattr(repo_judge, "_score", score)
    monkeypatch.setattr(repo_judge, "add_run_record", add_record)
    return record


def _session(
    job_store: SQLiteJobStore, cases: list[Any] | None, *, total: int = 1000, floor: int = 100, draft: str = "d1"
) -> str:
    """Open a judge session row as ``open_session`` leaves it.

    Args:
        job_store: The store.
        cases: The session's cases.
        total: The budget's total, in cents.
        floor: The floor of one call, in cents.
        draft: The draft id.

    Returns:
        The session id.
    """
    budgets = BudgetService(engine=job_store.engine)
    session_id = f"session-{draft}"
    budget = budgets.create(USER, total, idempotency_key=f"budget-{draft}")
    job_store.create_job(session_id, username=USER, idempotency_key=repo_judge.judge_key(draft, "f" * 16))
    attached = budgets.attach_to_job(budget.id, USER, session_id, expected_revision=budget.revision)
    payload = {
        "target": {"kind": "repo", "repo": {"repository": "acme/app", "editable_paths": ["src"], "commit": BASE}},
        "scorer": {"kind": "python", "metric_code": "def score(repo_path, case):\n    return 1.0, 'ok'\n"},
        "cases": cases,
        "seed": 7,
        "judge": {"version": 1, "draft_id": draft, "base_commit": BASE, "floor_cents": floor},
    }
    job_store.update_job(
        session_id,
        status="running",
        payload=payload,
        execution_budget_id=attached.id,
        execution_budget_generation=attached.generation,
    )
    return session_id


def _call(job_store: SQLiteJobStore, session_id: str, patch: str = GOOD_PATCH) -> dict[str, Any]:
    """Score a patch in a session.

    Args:
        job_store: The store.
        session_id: The session.
        patch: The candidate.

    Returns:
        The judge answer.
    """
    return repo_judge.judge_call(
        job_store, session_id, username=USER, candidate_commit=None, candidate_patch=patch, token=None
    )


def _spent(job_store: SQLiteJobStore, session_id: str) -> Decimal:
    """Return what the session's budget spent and holds.

    Args:
        job_store: The store.
        session_id: The session.

    Returns:
        Spent plus reserved cents.
    """
    job = job_store.get_job(session_id)
    budget = BudgetService(engine=job_store.engine).get(job["execution_budget_id"], USER)
    return budget.setup_spent_cents + budget.run_spent_cents + budget.reserved_cents


def _cases(count: int) -> list[dict[str, Any]]:
    """Build distinct cases.

    Args:
        count: How many.

    Returns:
        The cases.
    """
    return [{"input": f"in {index}", "expected": f"out {index}"} for index in range(count)]


def test_a_call_scores_the_search_cases_and_never_the_held_out_ones(store: SQLiteJobStore, boxes: Any) -> None:
    """Answer with the score, the named scores and the feedback of the search cases only."""
    session_id = _session(store, _cases(10))
    answer = _call(store, session_id)
    assert answer["status"] == "scored"
    assert answer["session_id"] == session_id
    assert answer["score"] == 0.5
    assert answer["named_scores"] == {"speed": {"score": 0.5, "feedback": "fast"}}
    assert answer["next_tool"] == "repo_judge_score"
    scored = boxes["scored"][0]
    # Ten cases split into search, validation and held-out cases; the call skips the held-out ones.
    assert 0 < scored[1] < 10
    assert len(answer["cases"]) == scored[1]
    held_out = set(range(10)) - {row["index"] for row in answer["cases"]}
    assert held_out
    assert "metric_code" not in str(answer)


def test_a_retry_with_the_same_candidate_pays_once(store: SQLiteJobStore, boxes: Any) -> None:
    """Replay the stored score without a second paid attempt."""
    session_id = _session(store, _cases(3))
    first = _call(store, session_id)
    again = _call(store, session_id)
    assert again["score"] == first["score"]
    assert again["patch_sha256"] == first["patch_sha256"]
    assert len(boxes["paid"]) == 1
    other = "diff --git a/src/b.py b/src/b.py\n--- a/src/b.py\n+++ b/src/b.py\n@@ -1 +1 @@\n-a\n+c\n"
    _call(store, session_id, other)
    assert len(boxes["paid"]) == 2


def test_a_candidate_that_changes_the_contract_is_refused_before_any_paid_box(
    store: SQLiteJobStore, boxes: Any
) -> None:
    """Refuse a change to optimize.toml, charge nothing, and replay the refusal."""
    session_id = _session(store, _cases(3))
    with pytest.raises(repo_judge.JudgeError) as refused:
        _call(store, session_id, CONTRACT_PATCH)
    assert refused.value.code == "judge.candidate_refused"
    assert boxes["paid"] == []
    assert _spent(store, session_id) == 0
    with pytest.raises(repo_judge.JudgeError) as again:
        _call(store, session_id, CONTRACT_PATCH)
    assert again.value.code == "judge.candidate_refused"
    assert len(boxes["staged"]) == 1


_NOT_UTF8 = "The candidate changes a text file that is not UTF-8."


def _git(cwd: Path, *arguments: str) -> str:
    """Run git in a local fixture repository.

    Args:
        cwd: Repository root.
        *arguments: Arguments after ``git``.

    Returns:
        Standard output, stripped.
    """
    command = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *arguments]
    return subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def test_a_candidate_commit_with_a_text_file_that_is_not_utf8_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refuse a commit that changes a latin-1 text file, instead of a raw decode error."""
    app = tmp_path / "app"
    (app / "src").mkdir(parents=True)
    _git(app, "init", "--quiet")
    _git(app, "config", "uploadpack.allowAnySHA1InWant", "true")
    (app / "src" / "app.py").write_text("x = 1\n")
    _git(app, "add", "--all")
    _git(app, "commit", "--quiet", "-m", "base")
    base = _git(app, "rev-parse", "HEAD")
    (app / "src" / "data.txt").write_bytes("caf\xe9\n".encode("latin-1"))
    _git(app, "add", "--all")
    _git(app, "commit", "--quiet", "-m", "candidate")
    head = _git(app, "rev-parse", "HEAD")
    monkeypatch.setattr(repo_judge.repo_hosts, "git_kwargs", lambda provider: {"remote": f"file://{app}"})
    with pytest.raises(repo_judge.JudgeError) as refused:
        repo_judge._candidate_patch({"provider": "github", "repository": "acme/app"}, base, head, None, None)
    assert refused.value.code == "judge.candidate_refused"
    assert refused.value.detail == _NOT_UTF8


def test_a_candidate_commit_that_changes_a_crlf_file_gives_the_commits_exact_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Score the same version for a commit as for its patch, CR bytes included (design case E4)."""
    app = tmp_path / "app"
    app.mkdir()
    raw = ["-c", "core.autocrlf=false"]
    _git(app, "init", "--quiet")
    _git(app, "config", "uploadpack.allowAnySHA1InWant", "true")
    (app / "a.txt").write_bytes(b"one\r\ntwo\r\n")
    _git(app, *raw, "add", "--all")
    _git(app, *raw, "commit", "--quiet", "-m", "base")
    base = _git(app, "rev-parse", "HEAD")
    (app / "a.txt").write_bytes(b"one\r\nTWO\r\n")
    _git(app, *raw, "commit", "--quiet", "-am", "candidate")
    head = _git(app, "rev-parse", "HEAD")
    monkeypatch.setattr(repo_judge.repo_hosts, "git_kwargs", lambda provider: {"remote": f"file://{app}"})

    patch = repo_judge._candidate_patch({"provider": "github", "repository": "acme/app"}, base, head, None, None)

    assert "+TWO\r\n" in patch
    _git(app, *raw, "checkout", "--quiet", base)
    (tmp_path / "candidate.patch").write_bytes(patch.encode("utf-8"))
    _git(app, *raw, "apply", "--binary", str(tmp_path / "candidate.patch"))
    assert (app / "a.txt").read_bytes() == b"one\r\nTWO\r\n"


def test_a_judge_call_with_a_commit_that_is_not_utf8_is_refused_before_the_claim(
    store: SQLiteJobStore, boxes: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refuse the call with no claim row and no paid work, and keep the session open."""
    session_id = _session(store, _cases(3))

    def undecodable(*_args: Any, **_kwargs: Any) -> str:
        """Fail as ``commit_diff`` fails on a latin-1 text file."""
        raise UnicodeDecodeError("utf-8", b"\xe9", 0, 1, "invalid continuation byte")

    monkeypatch.setattr(repo_judge, "commit_diff", undecodable)
    with pytest.raises(repo_judge.JudgeError) as refused:
        repo_judge.judge_call(
            store, session_id, username=USER, candidate_commit="b" * 40, candidate_patch=None, token=None
        )
    assert refused.value.code == "judge.candidate_refused"
    assert refused.value.detail == _NOT_UTF8
    assert _call_rows(store, session_id) == []
    assert boxes["paid"] == []
    assert boxes["staged"] == []
    assert _spent(store, session_id) == 0
    assert store.get_job(session_id)["status"] == "running"


def test_a_metric_error_is_stored_and_replayed(store: SQLiteJobStore, boxes: Any) -> None:
    """Answer score_failed and replay the stored failure without a second attempt."""
    session_id = _session(store, _cases(3))
    boxes["raise"] = ServiceError("scorer failed on the candidate: boom")
    with pytest.raises(repo_judge.JudgeError) as failed:
        _call(store, session_id)
    assert failed.value.code == "judge.score_failed"
    with pytest.raises(repo_judge.JudgeError) as again:
        _call(store, session_id)
    assert again.value.code == "judge.score_failed"
    assert "boom" in again.value.detail
    assert len(boxes["staged"]) == 1


def test_pending_usage_answers_in_progress(store: SQLiteJobStore, boxes: Any) -> None:
    """Tell the client to call again while the attempt's usage settles."""
    session_id = _session(store, _cases(3))
    boxes["raise"] = UsagePendingError("usage pending")
    with pytest.raises(repo_judge.JudgePendingError):
        _call(store, session_id)


def test_an_opening_session_answers_in_progress(store: SQLiteJobStore, boxes: Any) -> None:
    """Refuse to score before the open attached the budget."""
    store.create_job("opening", username=USER, idempotency_key="mcp-judge-d9-x")
    store.update_job("opening", status="running", payload={"judge": {"opening": True}})
    with pytest.raises(repo_judge.JudgePendingError):
        _call(store, "opening")
    assert boxes["staged"] == []


def test_a_low_budget_ends_the_session_and_scores_the_best_version(store: SQLiteJobStore, boxes: Any) -> None:
    """End the session when the budget cannot pay for a call and the final score."""
    session_id = _session(store, _cases(3), total=1000, floor=600)
    answer = _call(store, session_id)
    assert answer["status"] == "ended"
    assert answer["next_tool"] == "get_run"
    # No call scored, so nothing is left to score at the end.
    assert boxes["paid"] == []
    assert store.get_job(session_id)["status"] == "cancelled"


def test_stop_scores_the_best_version_once_on_the_held_out_cases(store: SQLiteJobStore, boxes: Any) -> None:
    """Score the best call once on the held-out cases and store the score in the run record."""
    session_id = _session(store, _cases(10))
    better = GOOD_PATCH.replace("+b", "+best")
    boxes["score"] = {GOOD_PATCH: 0.4, better: 0.9}
    _call(store, session_id)
    _call(store, session_id, better)
    ended = repo_judge.end_session(store, store.get_job(session_id), ended_by="stop_run")
    assert ended.status == "success"
    patch, count, label = boxes["scored"][-1]
    assert (patch, label) == (better, "best version")
    job = store.get_job(session_id)
    result = job["result"]
    details = result["details"]
    assert details["judge"]["held_out"] is True
    assert details["judge"]["calls"] == 2
    assert details["judge"]["search_score"] == 0.9
    assert details["record"] == {"version": 1, "score": 0.9, "held_out": True}
    assert result["best_candidate"] == better
    assert result["case_count"] == count
    assert count == len(details["case_split"]["held_out"])
    # The held-out feedback can quote held-out cases, so the client sees only scores.
    assert result["best_feedback"] is None
    assert all(row["best_feedback"] is None for row in result["case_results"])
    assert result["best_named_scores"]["speed"]["feedback"] == ""
    assert job["status"] == "success"
    budget = BudgetService(engine=store.engine).get(job["execution_budget_id"], USER)
    assert budget.state != "attached"
    # A second stop finds the finished end and scores nothing again.
    scored = len(boxes["scored"])
    again = repo_judge.end_session(store, store.get_job(session_id), ended_by="stop_run")
    assert again.status == "success"
    assert len(boxes["scored"]) == scored


@pytest.mark.parametrize("count", [0, 1, 3])
def test_stop_without_held_out_cases_scores_every_case_once(store: SQLiteJobStore, boxes: Any, count: int) -> None:
    """Score every case, or the version once without cases, when the split holds nothing out.

    With one case the split's test set is the search case, so it is not held out either.
    """
    session_id = _session(store, _cases(count) if count else None)
    _call(store, session_id)
    assert boxes["scored"][0][1] == count
    repo_judge.end_session(store, store.get_job(session_id), ended_by="stop_run")
    _patch, scored, label = boxes["scored"][-1]
    assert (scored, label) == (count, "best version")
    details = store.get_job(session_id)["result"]["details"]
    assert details["judge"]["held_out"] is False
    assert details["judge"]["held_out_score"] is None
    assert details["record"]["held_out"] is False
    assert details["record"]["score"] == 0.5


def test_stop_without_a_scored_call_cancels(store: SQLiteJobStore, boxes: Any) -> None:
    """Cancel a session that scored nothing, with no final score."""
    session_id = _session(store, _cases(3))
    ended = repo_judge.end_session(store, store.get_job(session_id), ended_by="stop_run")
    assert ended.status == "cancelled"
    assert boxes["paid"] == []


def test_stop_waits_while_the_final_score_usage_settles(store: SQLiteJobStore, boxes: Any) -> None:
    """Leave the end pending, so the next stop_run scores again."""
    session_id = _session(store, _cases(3))
    _call(store, session_id)
    boxes["raise"] = UsagePendingError("usage pending")
    with pytest.raises(DomainError) as pending:
        repo_judge.end_session(store, store.get_job(session_id), ended_by="stop_run")
    assert pending.value.code == "budget.pending"
    assert store.get_job(session_id)["status"] == "running"


def test_a_call_after_the_end_answers_ended(store: SQLiteJobStore, boxes: Any) -> None:
    """Point the client at get_run after the session ended."""
    session_id = _session(store, _cases(3))
    _call(store, session_id)
    repo_judge.end_session(store, store.get_job(session_id), ended_by="stop_run")
    answer = _call(store, session_id)
    assert answer["status"] == "ended"
    assert answer["run_status"] == "success"


def test_a_call_that_reaches_the_budget_still_ends_with_the_held_out_score(store: SQLiteJobStore, boxes: Any) -> None:
    """Reopen a budget that a call closed at its limit, and score the best version on the held-out cases."""
    session_id = _session(store, _cases(10))
    _call(store, session_id)
    budget_id = store.get_job(session_id)["execution_budget_id"]
    boxes["hook"] = lambda: BudgetService(engine=store.engine).stop_admission(budget_id, USER, reason="budget_reached")
    boxes["raise"] = BudgetReached("over")
    answer = _call(store, session_id, OTHER_PATCH)
    assert (answer["status"], answer["run_status"]) == ("ended", "success")
    patch, _count, label = boxes["scored"][-1]
    assert (patch, label) == (GOOD_PATCH, "best version")
    details = store.get_job(session_id)["result"]["details"]
    assert details["judge"]["ended_by"] == "budget"
    assert details["judge"]["held_out"] is True
    assert details["record"]["held_out"] is True


def _three_calls(job_store: SQLiteJobStore, session_id: str) -> list[dict[str, Any]]:
    """Score three different patches in a session.

    Args:
        job_store: The store.
        session_id: The session.

    Returns:
        The three answers.
    """
    return [_call(job_store, session_id, GOOD_PATCH.replace("+b", f"+v{index}")) for index in range(3)]


def test_the_session_ends_while_the_budget_still_pays_for_the_final_score(store: SQLiteJobStore, boxes: Any) -> None:
    """End before a call spends the final score's reserve, which grows with the settled spend per case."""
    # Two calls settle 90 each; the reserve then is 1.5 * 30 * 3 = 135, so 200 left cannot pay a call and the end.
    session_id = _session(store, _cases(3), total=380, floor=90)
    boxes["charge"] = {"candidate": 30, "best version": 40}
    answers = _three_calls(store, session_id)
    assert [answer["status"] for answer in answers] == ["scored", "scored", "ended"]
    assert answers[-1]["run_status"] == "success"
    assert boxes["scored"][-1][2] == "best version"
    assert _spent(store, session_id) == 300


def test_an_end_that_cannot_pay_the_final_score_stops_without_a_held_out_score(
    store: SQLiteJobStore, boxes: Any
) -> None:
    """Stop the session with a plain message, and never spend above the limit."""
    session_id = _session(store, _cases(3), total=380, floor=90)
    boxes["charge"] = {"candidate": 30, "best version": 100}
    answers = _three_calls(store, session_id)
    assert [answer["status"] for answer in answers] == ["scored", "scored", "ended"]
    assert answers[-1]["run_status"] == "stopped"
    job = store.get_job(session_id)
    assert job["status"] == "stopped"
    assert "no held-out score" in job["message"]
    assert all(label != "best version" for _patch, _count, label in boxes["scored"])
    assert _spent(store, session_id) <= 380


def test_a_held_reservation_answers_in_progress_and_keeps_the_session_open(store: SQLiteJobStore, boxes: Any) -> None:
    """Ask the client to call again while only the holds of calls in flight block the call."""
    session_id = _session(store, _cases(10))
    _call(store, session_id)
    job = store.get_job(session_id)
    budgets = BudgetService(engine=store.engine)
    budgets.reserve(
        job["execution_budget_id"],
        USER,
        operation_key="other-call",
        generation=job["execution_budget_generation"],
        phase="run",
        cost_kind="sandbox",
        request_fingerprint="fp",
        price_snapshot={"version": "v1"},
        max_cents=850,
    )
    with pytest.raises(repo_judge.JudgePendingError):
        _call(store, session_id, OTHER_PATCH)
    assert store.get_job(session_id)["status"] == "running"
    assert budgets.get(job["execution_budget_id"], USER).state == "attached"
    assert len(boxes["paid"]) == 1


def test_a_late_stop_reports_the_stored_status(store: SQLiteJobStore, boxes: Any) -> None:
    """Answer success to a stop that read the session before another stop ended it."""
    session_id = _session(store, _cases(10))
    _call(store, session_id)
    stale = store.get_job(session_id)
    assert repo_judge.end_session(store, store.get_job(session_id), ended_by="stop_run").status == "success"
    assert repo_judge.end_session(store, stale, ended_by="stop_run").status == "success"


def test_a_call_during_the_end_answers_pending_and_is_not_scored(store: SQLiteJobStore, boxes: Any) -> None:
    """Answer a call that starts while the end scores with ``budget.pending``, with no paid box."""
    session_id = _session(store, _cases(10))
    boxes["score"][OTHER_PATCH] = 0.99
    _call(store, session_id)
    inner: dict[str, Any] = {}

    def call_during_end() -> None:
        """Call while the end's lease is live, and keep the error."""
        with pytest.raises(DomainError) as raised:
            _call(store, session_id, OTHER_PATCH)
        inner["code"] = raised.value.code

    boxes["hook"] = call_during_end
    assert repo_judge.end_session(store, store.get_job(session_id), ended_by="stop_run").status == "success"
    assert inner["code"] == "budget.pending"
    assert _call(store, session_id, OTHER_PATCH)["status"] == "ended"
    assert all(patch != OTHER_PATCH for patch, _count, _label in boxes["scored"])
    assert len(boxes["paid"]) == 2
    assert store.get_job(session_id)["result"]["best_candidate"] == GOOD_PATCH


def test_a_call_that_claims_after_the_end_claimed_scores_nothing(
    store: SQLiteJobStore, boxes: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Close the call's row as ended when the end claims between the call's check and its claim."""
    session_id = _session(store, _cases(10))
    _call(store, session_id)
    boxes["raise"] = UsagePendingError("usage pending")
    with pytest.raises(DomainError):
        repo_judge.end_session(store, store.get_job(session_id), ended_by="stop_run")
    real = repo_judge._final_row
    lookups: list[int] = []

    def late_final_row(engine: Any, budget_id: str) -> Any:
        """Miss the end's row on the first lookup, as a call that checked before the end claimed.

        Args:
            engine: Database that holds the evidence.
            budget_id: The session's budget.

        Returns:
            ``None`` first, then the real row.
        """
        lookups.append(1)
        return None if len(lookups) == 1 else real(engine, budget_id)

    monkeypatch.setattr(repo_judge, "_final_row", late_final_row)
    answer = _call(store, session_id, OTHER_PATCH)
    assert answer["status"] == "ended"
    assert len(boxes["paid"]) == 2
    budget_id = store.get_job(session_id)["execution_budget_id"]
    assert repo_judge._best_call(store.engine, budget_id)[1] == 1


def test_the_end_waits_while_a_call_runs_and_then_counts_it(store: SQLiteJobStore, boxes: Any) -> None:
    """Answer budget.pending to an end while a call scores, and count that call at the next end."""
    session_id = _session(store, _cases(10))
    boxes["score"][OTHER_PATCH] = 0.99
    _call(store, session_id)
    inner: dict[str, Any] = {}

    def end_inside() -> None:
        """Try to end the session while the call scores."""
        with pytest.raises(DomainError) as pending:
            repo_judge.end_session(store, store.get_job(session_id), ended_by="stop_run")
        inner["code"] = pending.value.code

    boxes["hook"] = end_inside
    assert _call(store, session_id, OTHER_PATCH)["status"] == "scored"
    assert inner["code"] == "budget.pending"
    assert repo_judge.end_session(store, store.get_job(session_id), ended_by="stop_run").status == "success"
    assert store.get_job(session_id)["result"]["best_candidate"] == OTHER_PATCH


def _wrapped_interruption() -> ServiceError:
    """Build a scorer error caused by a sandbox interruption.

    Returns:
        The wrapped error.
    """
    error = ServiceError("scorer failed")
    error.__cause__ = InfrastructureInterruptionError("the sandbox connection dropped")
    return error


@pytest.mark.parametrize("failure", [lambda: RepoRateLimitError("rate limit"), _wrapped_interruption])
def test_a_temporary_failure_answers_in_progress_and_a_retry_scores(
    store: SQLiteJobStore, boxes: Any, failure: Any
) -> None:
    """Leave the call free for a retry after a temporary failure, and replay the score after it."""
    session_id = _session(store, _cases(3))
    boxes["raise"] = failure()
    with pytest.raises(repo_judge.JudgePendingError):
        _call(store, session_id)
    assert _call(store, session_id)["status"] == "scored"
    assert _call(store, session_id)["status"] == "scored"
    assert len(boxes["paid"]) == 2


def test_a_repeated_temporary_failure_of_a_call_is_stored_at_the_limit(store: SQLiteJobStore, boxes: Any) -> None:
    """Store score_failed when a temporary failure repeats up to the limit, and replay it with no new box."""
    session_id = _session(store, _cases(3))
    for _ in range(repo_judge._TEMPORARY_FAILURE_LIMIT - 1):
        boxes["raise"] = RepoFetchError("Connect GitHub again: the connection is missing or has expired.")
        with pytest.raises(repo_judge.JudgePendingError):
            _call(store, session_id)
    boxes["raise"] = RepoFetchError("Connect GitHub again: the connection is missing or has expired.")
    with pytest.raises(repo_judge.JudgeError) as failed:
        _call(store, session_id)
    assert failed.value.code == "judge.score_failed"
    with pytest.raises(repo_judge.JudgeError) as again:
        _call(store, session_id)
    assert again.value.code == "judge.score_failed"
    assert len(boxes["paid"]) == repo_judge._TEMPORARY_FAILURE_LIMIT


def _final_rows(job_store: SQLiteJobStore, session_id: str) -> int:
    """Count the end's evidence rows of a session.

    Args:
        job_store: The store.
        session_id: The session.

    Returns:
        How many final rows the session has.
    """
    budget_id = job_store.get_job(session_id)["execution_budget_id"]
    with Session(job_store.engine) as session:
        return int(
            session.scalar(
                select(func.count())
                .select_from(WizardPreflightModel)
                .where(
                    WizardPreflightModel.budget_id == budget_id,
                    WizardPreflightModel.scope == repo_judge.FINAL_SCOPE,
                )
            )
            or 0
        )


def _expire_the_end(job_store: SQLiteJobStore) -> None:
    """Expire the end's lease, as after a crash of its owner.

    Args:
        job_store: The store.
    """
    with Session(job_store.engine) as session:
        session.execute(
            update(WizardPreflightModel)
            .where(WizardPreflightModel.scope == repo_judge.FINAL_SCOPE)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=5))
        )
        session.commit()


def test_a_call_finishes_an_end_that_crashed(store: SQLiteJobStore, boxes: Any) -> None:
    """Drive a crashed stop_run end to its final score from a call, and keep its ended_by."""
    session_id = _session(store, _cases(10))
    _call(store, session_id)
    boxes["raise"] = _Crash("the worker stopped")
    with pytest.raises(_Crash):
        repo_judge.end_session(store, store.get_job(session_id), ended_by="stop_run")
    _expire_the_end(store)
    answer = _call(store, session_id)
    assert (answer["status"], answer["run_status"]) == ("ended", "success")
    job = store.get_job(session_id)
    assert job["status"] == "success"
    assert job["result"]["details"]["judge"]["ended_by"] == "stop_run"
    assert job["result"]["details"]["judge"]["held_out"] is True
    assert _final_rows(store, session_id) == 1


def test_a_call_during_a_live_end_answers_pending_and_scores_nothing(store: SQLiteJobStore, boxes: Any) -> None:
    """Answer budget.pending to a call while the end's lease is live, with no score."""
    session_id = _session(store, _cases(10))
    _call(store, session_id)
    boxes["raise"] = _Crash("the worker stopped")
    with pytest.raises(_Crash):
        repo_judge.end_session(store, store.get_job(session_id), ended_by="stop_run")
    scored = len(boxes["scored"])
    with pytest.raises(DomainError) as pending:
        _call(store, session_id, OTHER_PATCH)
    assert pending.value.code == "budget.pending"
    assert len(boxes["scored"]) == scored
    assert store.get_job(session_id)["status"] == "running"


@pytest.mark.parametrize(
    ("failure", "retry_by_call"),
    [(lambda: RepoRateLimitError("rate limit"), False), (_wrapped_interruption, True)],
)
def test_a_temporary_failure_of_the_final_score_waits_and_a_retry_finishes(
    store: SQLiteJobStore, boxes: Any, failure: Any, retry_by_call: bool
) -> None:
    """Keep the session open after a temporary failure of the final score, and finish it at the retry."""
    session_id = _session(store, _cases(10))
    _call(store, session_id)
    boxes["raise"] = failure()
    with pytest.raises(DomainError) as pending:
        repo_judge.end_session(store, store.get_job(session_id), ended_by="stop_run")
    assert pending.value.code == "budget.pending"
    assert store.get_job(session_id)["status"] == "running"
    budget_id = store.get_job(session_id)["execution_budget_id"]
    assert repo_judge._final_row(store.engine, budget_id).status == "pending"
    if retry_by_call:
        assert _call(store, session_id, OTHER_PATCH)["run_status"] == "success"
    else:
        assert repo_judge.end_session(store, store.get_job(session_id), ended_by="budget").status == "success"
    judge = store.get_job(session_id)["result"]["details"]["judge"]
    assert judge["held_out"] is True
    assert judge["ended_by"] == "stop_run"
    assert all(patch != OTHER_PATCH for patch, _count, _label in boxes["scored"])


def test_a_lasting_fetch_failure_of_the_final_score_fails_at_the_limit(store: SQLiteJobStore, boxes: Any) -> None:
    """Fail the session when the final score's fetch error repeats up to the limit, and never wait after it."""
    session_id = _session(store, _cases(10))
    _call(store, session_id)
    outcomes = []
    for _ in range(repo_judge._TEMPORARY_FAILURE_LIMIT + 2):
        boxes["raise"] = RepoFetchError("Connect GitHub again: the connection is missing or has expired.")
        try:
            outcomes.append(repo_judge.end_session(store, store.get_job(session_id), ended_by="stop_run").status)
        except DomainError as error:
            outcomes.append(error.code)
    waits = repo_judge._TEMPORARY_FAILURE_LIMIT - 1
    assert outcomes == ["budget.pending"] * waits + ["failed"] * 3
    job = store.get_job(session_id)
    assert job["status"] == "failed"
    assert "Connect GitHub again" in job["message"]


def test_ends_that_only_wait_for_a_call_keep_the_failure_limit(store: SQLiteJobStore, boxes: Any) -> None:
    """Count only temporary failures toward the limit, not the ends that waited for a live call."""
    session_id = _session(store, _cases(10))
    _call(store, session_id)
    codes: list[str] = []

    def wait_for_the_call() -> None:
        """Try to end the session while the call scores, as often as the limit."""
        for _ in range(repo_judge._TEMPORARY_FAILURE_LIMIT + 1):
            with pytest.raises(DomainError) as pending:
                repo_judge.end_session(store, store.get_job(session_id), ended_by="stop_run")
            codes.append(pending.value.code)

    boxes["hook"] = wait_for_the_call
    assert _call(store, session_id, OTHER_PATCH)["status"] == "scored"
    assert set(codes) == {"budget.pending"}
    boxes["raise"] = RepoRateLimitError("rate limit")
    with pytest.raises(DomainError) as pending:
        repo_judge.end_session(store, store.get_job(session_id), ended_by="stop_run")
    assert pending.value.code == "budget.pending"
    assert store.get_job(session_id)["status"] == "running"
    assert repo_judge.end_session(store, store.get_job(session_id), ended_by="stop_run").status == "success"


def test_usage_that_settles_after_a_call_still_sizes_the_final_reserve(store: SQLiteJobStore, boxes: Any) -> None:
    """Count a call's usage that settled after the call returned, so a later call keeps the final score's reserve."""
    session_id = _session(store, _cases(3), total=2000, floor=100)
    held: dict[str, str] = {}

    def charge_late() -> None:
        """Hold a sandbox operation that is still pending when the call returns."""
        attempt = boxes["attempt"]
        service = BudgetService(engine=attempt["engine"])
        operation = service.reserve(
            attempt["budget_id"],
            USER,
            operation_key=attempt["owner_id"],
            generation=attempt["generation"],
            phase="run",
            cost_kind="sandbox",
            request_fingerprint="fp",
            price_snapshot={"version": "v1"},
            max_cents=900,
        )
        service.mark_dispatched(operation.id, USER)
        service.mark_pending(operation.id, USER)
        held["operation"] = operation.id

    boxes["hook"] = charge_late
    with pytest.raises(repo_judge.JudgePendingError):
        _call(store, session_id)
    BudgetService(engine=store.engine).settle(
        held["operation"], USER, evidence_key="final", actual_cents=900, evidence={}
    )
    # The call spent 900 on 3 cases, so the reserve is 1.5 * 300 * 3 = 1350, and 1100 left is less than it.
    boxes["charge"] = {"candidate": 300, "best version": 300}
    answer = _call(store, session_id, OTHER_PATCH)
    assert (answer["status"], answer["run_status"]) == ("ended", "success")
    assert [label for _patch, _count, label in boxes["scored"]] == ["candidate", "best version"]
    assert boxes["scored"][-1][0] == GOOD_PATCH
    assert _spent(store, session_id) <= 2000


def test_a_large_unsettled_hold_answers_in_progress_and_never_ends_the_session(
    store: SQLiteJobStore, boxes: Any
) -> None:
    """Answer in progress while an earlier call's large hold settles, and score the call after it settles."""
    session_id = _session(store, _cases(3), total=5000, floor=100)
    held: dict[str, str] = {}

    def hold_late() -> None:
        """Hold 4000 for a sandbox operation that is still pending when the call returns."""
        attempt = boxes["attempt"]
        service = BudgetService(engine=attempt["engine"])
        operation = service.reserve(
            attempt["budget_id"],
            USER,
            operation_key=attempt["owner_id"],
            generation=attempt["generation"],
            phase="run",
            cost_kind="sandbox",
            request_fingerprint="fp",
            price_snapshot={"version": "v1"},
            max_cents=4000,
        )
        service.mark_dispatched(operation.id, USER)
        service.mark_pending(operation.id, USER)
        held["operation"] = operation.id

    boxes["hook"] = hold_late
    with pytest.raises(repo_judge.JudgePendingError):
        _call(store, session_id)
    # The hold reads as a spend of 4000, so the reserve (1.5 * 4000) is above the total.
    with pytest.raises(repo_judge.JudgePendingError) as pending:
        _call(store, session_id, OTHER_PATCH)
    assert "earlier call still settles" in str(pending.value)
    assert _final_rows(store, session_id) == 0
    assert store.get_job(session_id)["status"] == "running"
    BudgetService(engine=store.engine).settle(
        held["operation"], USER, evidence_key="final", actual_cents=30, evidence={}
    )
    assert _call(store, session_id, OTHER_PATCH)["status"] == "scored"
    assert store.get_job(session_id)["status"] == "running"


def _stage_cases(monkeypatch: pytest.MonkeyPatch, cases: list[Any]) -> None:
    """Make staging yield these cases, as a `[data] cases` file does when the stored payload has none.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        cases: The cases that staging reads from the repository.
    """
    original = repo_judge.judge_scorer

    @contextmanager
    def scorer(payload: dict[str, Any], **kwargs: Any) -> Iterator[tuple[dict[str, Any], Any, Any]]:
        """Stage the session with the repository's cases.

        Args:
            payload: The session payload.
            **kwargs: The other scorer arguments.

        Yields:
            The faked scorer's protected payload, staged repository and gateway.
        """
        with original({**payload, "cases": cases}, **kwargs) as staged:
            yield staged

    monkeypatch.setattr(repo_judge, "judge_scorer", scorer)


def _budget_id(job_store: SQLiteJobStore, session_id: str) -> str:
    """Return the session's budget id.

    Args:
        job_store: The store.
        session_id: The session.

    Returns:
        The budget id.
    """
    return str(job_store.get_job(session_id)["execution_budget_id"])


def test_a_cases_file_session_sizes_the_final_reserve_from_the_staged_cases(
    store: SQLiteJobStore, boxes: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Size the final reserve of a session with no stored cases from the cases that staging gave, not from one case."""
    staged = _cases(20)
    _stage_cases(monkeypatch, staged)
    boxes["charge"]["candidate"] = 50
    needs = {}
    for draft, cases in (("cases-file", []), ("inline", staged)):
        session_id = _session(store, cases, total=2000, floor=100, draft=draft)
        answer = _call(store, session_id)
        assert answer["status"] == "scored"
        assert "final_count" not in str(answer)
        needs[draft] = repo_judge._needed(
            store.engine, _budget_id(store, session_id), store.get_job(session_id)["payload"]
        )
    assert needs["cases-file"] == needs["inline"] == 325


@pytest.mark.parametrize("draft", ["cases-file", "inline"])
def test_a_cases_file_session_ends_with_the_held_out_score(
    store: SQLiteJobStore, boxes: Any, monkeypatch: pytest.MonkeyPatch, draft: str
) -> None:
    """End a session whose cases come from staging while its budget still pays for the held-out score."""
    staged = _cases(20)
    _stage_cases(monkeypatch, staged)
    session_id = _session(store, [] if draft == "cases-file" else staged, total=2000, floor=100, draft=draft)
    boxes["charge"] = {"candidate": 50, "best version": 50}
    answers = []
    for step, patch in enumerate((GOOD_PATCH, OTHER_PATCH, THIRD_PATCH, FOURTH_PATCH)):
        if step == 2:
            boxes["charge"]["candidate"] = 10
        answers.append(_call(store, session_id, patch))
    assert answers[-1]["status"] == "ended"
    job = store.get_job(session_id)
    assert job["status"] == "success"
    assert job["result"]["details"]["judge"]["held_out"] is True
    assert "final_count" not in str(job["result"])
    assert _spent(store, session_id) <= 2000


def test_a_retry_of_a_scored_call_replays_also_without_headroom(store: SQLiteJobStore, boxes: Any) -> None:
    """Replay a scored call's stored answer when the budget no longer pays for a new call, and end at a new one."""
    session_id = _session(store, _cases(10), total=260, floor=100)
    boxes["charge"]["candidate"] = 10
    first = _call(store, session_id)
    assert first["status"] == "scored"
    again = _call(store, session_id)
    assert (again["status"], again["score"], again["patch_sha256"]) == ("scored", first["score"], first["patch_sha256"])
    assert len(boxes["paid"]) == 1
    assert _final_rows(store, session_id) == 0
    assert store.get_job(session_id)["status"] == "running"
    ended = _call(store, session_id, OTHER_PATCH)
    assert (ended["status"], ended["run_status"]) == ("ended", "success")
    assert [label for _patch, _count, label in boxes["scored"]] == ["candidate", "best version"]


def test_a_retry_after_late_settlement_gets_the_stored_score(store: SQLiteJobStore, boxes: Any) -> None:
    """Answer the stored score to the retry of a call whose usage settled after it returned, with no new attempt."""
    session_id = _session(store, _cases(3), total=2000, floor=100)
    held: dict[str, str] = {}

    def charge_late() -> None:
        """Hold a sandbox operation that is still pending when the call returns."""
        attempt = boxes["attempt"]
        service = BudgetService(engine=attempt["engine"])
        operation = service.reserve(
            attempt["budget_id"],
            USER,
            operation_key=attempt["owner_id"],
            generation=attempt["generation"],
            phase="run",
            cost_kind="sandbox",
            request_fingerprint="fp",
            price_snapshot={"version": "v1"},
            max_cents=900,
        )
        service.mark_dispatched(operation.id, USER)
        service.mark_pending(operation.id, USER)
        held["operation"] = operation.id

    boxes["hook"] = charge_late
    with pytest.raises(repo_judge.JudgePendingError):
        _call(store, session_id)
    with pytest.raises(repo_judge.JudgePendingError):
        _call(store, session_id)
    BudgetService(engine=store.engine).settle(
        held["operation"], USER, evidence_key="final", actual_cents=900, evidence={}
    )
    assert _call(store, session_id)["status"] == "scored"
    assert len(boxes["paid"]) == 1
    assert _final_rows(store, session_id) == 0


def test_a_retry_of_a_failed_call_replays_its_error_also_without_headroom(store: SQLiteJobStore, boxes: Any) -> None:
    """Replay a stored failure when the call's spend leaves no headroom, and do not end the session."""
    session_id = _session(store, _cases(10), total=260, floor=100)
    boxes["charge"]["candidate"] = 20
    boxes["raise"] = ServiceError("scorer failed on the candidate: boom")
    with pytest.raises(repo_judge.JudgeError) as failed:
        _call(store, session_id)
    assert failed.value.code == "judge.score_failed"
    with pytest.raises(repo_judge.JudgeError) as again:
        _call(store, session_id)
    assert again.value.code == "judge.score_failed"
    assert len(boxes["paid"]) == 1
    assert _final_rows(store, session_id) == 0
    assert store.get_job(session_id)["status"] == "running"


def test_a_stop_run_end_that_waits_on_old_work_keeps_its_ended_by(store: SQLiteJobStore, boxes: Any) -> None:
    """Keep the first end's ended_by when that end first waits on a crashed call's dispatched operation."""
    session_id = _session(store, _cases(10))
    _call(store, session_id)
    held: dict[str, str] = {}

    def crash() -> None:
        """Dispatch a paid operation, then stop as a crashed worker does.

        Raises:
            _Crash: Always.
        """
        attempt = boxes["attempt"]
        service = BudgetService(engine=attempt["engine"])
        operation = service.reserve(
            attempt["budget_id"],
            USER,
            operation_key=attempt["owner_id"],
            generation=attempt["generation"],
            phase="run",
            cost_kind="sandbox",
            request_fingerprint="fp",
            price_snapshot={"version": "v1"},
            max_cents=10,
        )
        service.mark_dispatched(operation.id, USER)
        held["operation"] = operation.id
        raise _Crash("the worker stopped")

    boxes["hook"] = crash
    with pytest.raises(_Crash):
        _call(store, session_id, OTHER_PATCH)
    with Session(store.engine) as session:
        session.execute(
            update(WizardPreflightModel)
            .where(WizardPreflightModel.scope == repo_judge.JUDGE_SCOPE, WizardPreflightModel.status == "running")
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=5))
        )
        session.commit()
    with pytest.raises(DomainError) as pending:
        repo_judge.end_session(store, store.get_job(session_id), ended_by="stop_run")
    assert pending.value.code == "budget.pending"
    assert repo_judge._final_row(store.engine, _budget_id(store, session_id)).result["ended_by"] == "stop_run"
    BudgetService(engine=store.engine).settle(
        held["operation"], USER, evidence_key="final", actual_cents=10, evidence={}
    )
    assert _call(store, session_id, OTHER_PATCH)["run_status"] == "success"
    assert store.get_job(session_id)["result"]["details"]["judge"]["ended_by"] == "stop_run"


def test_a_usage_pending_attempt_keeps_the_temporary_failure_count(store: SQLiteJobStore, boxes: Any) -> None:
    """Count three temporary failures of one candidate toward the limit across a usage-pending attempt."""
    session_id = _session(store, _cases(3))
    for error in (RepoFetchError("lost"), UsagePendingError("usage settles"), RepoFetchError("lost")):
        boxes["raise"] = error
        with pytest.raises(repo_judge.JudgePendingError):
            _call(store, session_id)
        if isinstance(error, UsagePendingError):
            (row,) = repo_judge._rows(store.engine, _budget_id(store, session_id), repo_judge.JUDGE_SCOPE)
            assert row.result["temporary_failures"] == 1
    boxes["raise"] = RepoFetchError("lost")
    with pytest.raises(repo_judge.JudgeError) as failed:
        _call(store, session_id)
    assert failed.value.code == "judge.score_failed"


def test_an_end_with_a_closed_budget_waits_for_a_live_call(store: SQLiteJobStore, boxes: Any) -> None:
    """Wait for a live call when another call closes the budget, and end later with that call's held-out score."""
    session_id = _session(store, _cases(10), total=1000, floor=100)
    inner: dict[str, Any] = {}

    def other_call() -> None:
        """Run a second call that reaches the budget while the first call scores."""
        boxes["charge"] = {"candidate": 2000}
        with pytest.raises(DomainError) as pending:
            _call(store, session_id, OTHER_PATCH)
        boxes["charge"] = {}
        inner["code"] = pending.value.code
        inner["status"] = store.get_job(session_id)["status"]

    boxes["hook"] = other_call
    assert _call(store, session_id)["status"] == "scored"
    assert inner == {"code": "budget.pending", "status": "running"}
    ended = _call(store, session_id, THIRD_PATCH)
    assert (ended["status"], ended["run_status"]) == ("ended", "success")
    job = store.get_job(session_id)
    assert job["result"]["best_candidate"] == GOOD_PATCH
    assert job["result"]["details"]["judge"]["held_out"] is True


def test_a_crashed_call_does_not_hold_an_end_with_a_closed_budget(store: SQLiteJobStore, boxes: Any) -> None:
    """Wait for a call's live lease at a closed-budget end, and end once a crashed call's lease expired."""
    session_id = _session(store, _cases(3))
    boxes["raise"] = _Crash("the worker stopped")
    with pytest.raises(_Crash):
        _call(store, session_id)
    BudgetService(engine=store.engine).stop_admission(_budget_id(store, session_id), USER, reason="budget_reached")
    with pytest.raises(DomainError) as pending:
        repo_judge.end_session(store, store.get_job(session_id), ended_by="stop_run")
    assert pending.value.code == "budget.pending"
    with Session(store.engine) as session:
        session.execute(
            update(WizardPreflightModel)
            .where(WizardPreflightModel.status == "running")
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=5))
        )
        session.commit()
    assert repo_judge.end_session(store, store.get_job(session_id), ended_by="stop_run").status == "stopped"


def test_a_refusal_names_every_broken_rule(store: SQLiteJobStore, boxes: Any) -> None:
    """Report the contract change and the path outside the editable paths in one refusal."""
    session_id = _session(store, _cases(3))
    with pytest.raises(repo_judge.JudgeError) as refused:
        _call(store, session_id, CONTRACT_PATCH + OUTSIDE_PATCH)
    assert "optimize.toml" in refused.value.detail
    assert "other/x.py" in refused.value.detail


def _hold(attempt: dict[str, Any], cents: int, *, pending: bool) -> tuple[BudgetService, str]:
    """Dispatch a paid operation of an attempt that is still unsettled when the attempt returns.

    Args:
        attempt: The scorer arguments of the attempt.
        cents: The operation's hold.
        pending: Mark the operation as awaiting its usage, as after a lost box.

    Returns:
        The budget service and the operation id.
    """
    service = BudgetService(engine=attempt["engine"])
    operation = service.reserve(
        attempt["budget_id"],
        USER,
        operation_key=attempt["owner_id"],
        generation=attempt["generation"],
        phase="run",
        cost_kind="sandbox",
        request_fingerprint="fp",
        price_snapshot={"version": "v1"},
        max_cents=cents,
    )
    service.mark_dispatched(operation.id, USER)
    if pending:
        service.mark_pending(operation.id, USER)
    return service, operation.id


def _call_rows(job_store: SQLiteJobStore, session_id: str) -> list[tuple[str, bool]]:
    """List each call row's status, and whether it stores an error.

    Args:
        job_store: The store.
        session_id: The session.

    Returns:
        The sorted pairs.
    """
    rows = repo_judge._rows(job_store.engine, _budget_id(job_store, session_id), repo_judge.JUDGE_SCOPE)
    return sorted((row.status, "judge_error" in row.result) for row in rows)


def _refuse_while_usage_waits(job_store: SQLiteJobStore, boxes: Any, session_id: str) -> str:
    """Leave one call's usage unsettled, then refuse a candidate, so the refusal is stored pending.

    Args:
        job_store: The store.
        boxes: The fake boxes.
        session_id: The session.

    Returns:
        The id of the unsettled operation.
    """
    held: dict[str, str] = {}

    def hold() -> None:
        """Keep a sandbox operation awaiting its usage when the call returns."""
        held["operation"] = _hold(boxes["attempt"], 100, pending=True)[1]

    boxes["hook"] = hold
    with pytest.raises(repo_judge.JudgePendingError):
        _call(job_store, session_id)
    with pytest.raises(repo_judge.JudgeError) as refused:
        _call(job_store, session_id, CONTRACT_PATCH)
    assert refused.value.code == "judge.candidate_refused"
    assert _call_rows(job_store, session_id) == [("pending", False), ("pending", True)]
    return held["operation"]


def test_a_retry_of_a_refusal_stored_pending_replays_it_without_headroom(store: SQLiteJobStore, boxes: Any) -> None:
    """Replay a refusal that finished while usage settled, also when the budget no longer pays for a new call."""
    session_id = _session(store, _cases(3), total=400, floor=100)
    operation = _refuse_while_usage_waits(store, boxes, session_id)
    BudgetService(engine=store.engine).settle(operation, USER, evidence_key="final", actual_cents=100, evidence={})
    boxes["charge"]["candidate"] = 30
    assert _call(store, session_id, OTHER_PATCH)["status"] == "scored"
    staged = len(boxes["staged"])
    with pytest.raises(repo_judge.JudgeError) as again:
        _call(store, session_id, CONTRACT_PATCH)
    assert again.value.code == "judge.candidate_refused"
    assert "optimize.toml" in again.value.detail
    assert _final_rows(store, session_id) == 0
    assert store.get_job(session_id)["status"] == "running"
    assert len(boxes["staged"]) == staged


def test_a_retry_of_a_refusal_during_a_live_box_replays_it_without_headroom(store: SQLiteJobStore, boxes: Any) -> None:
    """Replay a refusal that finished while another call's box ran, when the budget no longer pays for a call."""
    session_id = _session(store, _cases(3), total=400, floor=100)

    def refuse_during_the_box() -> None:
        """Refuse a candidate while this call's box is dispatched, then settle the box."""
        service, operation = _hold(boxes["attempt"], 100, pending=False)
        with pytest.raises(repo_judge.JudgeError) as refused:
            _call(store, session_id, CONTRACT_PATCH)
        assert refused.value.code == "judge.candidate_refused"
        service.settle(operation, USER, evidence_key="a", actual_cents=100, evidence={})

    boxes["hook"] = refuse_during_the_box
    assert _call(store, session_id)["status"] == "scored"
    assert ("pending", True) in _call_rows(store, session_id)
    boxes["charge"]["candidate"] = 30
    assert _call(store, session_id, OTHER_PATCH)["status"] == "scored"
    staged = len(boxes["staged"])
    with pytest.raises(repo_judge.JudgeError) as again:
        _call(store, session_id, CONTRACT_PATCH)
    assert again.value.code == "judge.candidate_refused"
    assert _final_rows(store, session_id) == 0
    assert store.get_job(session_id)["status"] == "running"
    assert len(boxes["staged"]) == staged


def test_a_retry_of_a_refusal_stored_pending_waits_while_usage_settles(store: SQLiteJobStore, boxes: Any) -> None:
    """Answer in_progress to the retry of a pending refusal while usage is unsettled, and do not end the session."""
    session_id = _session(store, _cases(3), total=400, floor=100)
    _refuse_while_usage_waits(store, boxes, session_id)
    staged = len(boxes["staged"])
    with pytest.raises(repo_judge.JudgePendingError):
        _call(store, session_id, CONTRACT_PATCH)
    assert _final_rows(store, session_id) == 0
    assert store.get_job(session_id)["status"] == "running"
    assert len(boxes["staged"]) == staged


def test_a_pending_temporary_failure_without_headroom_still_ends_the_session(store: SQLiteJobStore, boxes: Any) -> None:
    """Keep a temporary failure free for a new attempt: with no headroom, its retry ends the session."""
    session_id = _session(store, _cases(10), total=260, floor=100)
    assert _call(store, session_id)["status"] == "scored"
    boxes["charge"]["candidate"] = 20
    boxes["raise"] = RepoFetchError("lost")
    with pytest.raises(repo_judge.JudgePendingError):
        _call(store, session_id, OTHER_PATCH)
    boxes["charge"] = {}
    assert ("pending", False) in _call_rows(store, session_id)
    ended = _call(store, session_id, OTHER_PATCH)
    assert (ended["status"], ended["run_status"]) == ("ended", "success")
    assert _final_rows(store, session_id) == 1


def test_a_missing_saved_secret_fails_the_end_and_a_later_stop_pays_nothing(store: SQLiteJobStore, boxes: Any) -> None:
    """Fail the session at once when the final score's saved secret is gone, with the reason the setup check gives."""
    session_id = _session(store, _cases(10))
    _call(store, session_id)
    missing = DomainError("secrets.not_found", status=404)
    boxes["raise"] = missing
    ended = repo_judge.end_session(store, store.get_job(session_id), ended_by="stop_run")
    assert ended.status == "failed"
    job = store.get_job(session_id)
    assert job["status"] == "failed"
    assert missing.detail in job["message"]
    assert "404" not in job["message"]
    assert repo_judge._final_row(store.engine, _budget_id(store, session_id)).status == "succeeded"
    assert _final_rows(store, session_id) == 1
    paid = len(boxes["paid"])
    assert repo_judge.end_session(store, store.get_job(session_id), ended_by="stop_run").status == "failed"
    assert len(boxes["paid"]) == paid


def test_a_missing_saved_secret_fails_the_call_and_a_retry_replays_it(store: SQLiteJobStore, boxes: Any) -> None:
    """Store score_failed when a call's saved secret is gone, and replay it with no new paid attempt."""
    session_id = _session(store, _cases(3))
    missing = DomainError("secrets.not_found", status=404)
    boxes["raise"] = missing
    with pytest.raises(repo_judge.JudgeError) as failed:
        _call(store, session_id)
    assert (failed.value.code, failed.value.detail) == ("judge.score_failed", missing.detail)
    with pytest.raises(repo_judge.JudgeError) as again:
        _call(store, session_id)
    assert (again.value.code, again.value.detail) == ("judge.score_failed", missing.detail)
    assert len(boxes["paid"]) == 1
    assert store.get_job(session_id)["status"] == "running"
    assert _final_rows(store, session_id) == 0


def test_an_unexpected_error_names_only_its_type(
    store: SQLiteJobStore, boxes: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Keep the text of an unexpected error out of the call's answer, the session's message and the log."""
    caplog.set_level("ERROR", logger=repo_judge.logger.name)
    session_id = _session(store, _cases(10))
    boxes["raise"] = KeyError("secret text")
    with pytest.raises(repo_judge.JudgeError) as failed:
        _call(store, session_id)
    assert failed.value.code == "judge.score_failed"
    assert "KeyError" in failed.value.detail
    assert "secret text" not in failed.value.detail
    _call(store, session_id, OTHER_PATCH)
    boxes["raise"] = KeyError("secret text")
    assert repo_judge.end_session(store, store.get_job(session_id), ended_by="stop_run").status == "failed"
    message = store.get_job(session_id)["message"]
    assert "KeyError" in message
    assert "secret text" not in message
    assert caplog.text.count("failed with KeyError") == 2
    assert "secret text" not in caplog.text


class _Dispatching:
    """A staged scorer whose guest endpoint raises the error the test set, else scores 0.5."""

    def __init__(self, record: dict[str, Any]) -> None:
        """Keep the test's record.

        Args:
            record: The ``boxes`` record; ``dispatch_raise`` is the next error.
        """
        self.record = record

    def dispatch(self, body: Any) -> Any:
        """Answer one scoring request.

        Args:
            body: The guest's request.

        Returns:
            A 200 reply with a score.

        Raises:
            Exception: The error the test set in ``dispatch_raise``.
        """
        error, self.record["dispatch_raise"] = self.record.get("dispatch_raise"), None
        if error is not None:
            raise error
        return SimpleNamespace(status=200, body=b'{"score": 0.5, "feedback": "ok"}')


@pytest.fixture
def dispatched(boxes: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Score through the real ``_score`` and ``_final_run``, with a staged scorer that can raise.

    Args:
        boxes: The counted fakes.
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        The ``boxes`` record.
    """

    def score(_staged: Any, patch: str, cases: list[Any], *, label: str) -> FinalRun:
        """Score with the real score step.

        Args:
            _staged: The fake staged repository.
            patch: The version.
            cases: The cases.
            label: What the version is.

        Returns:
            The real score step's result.
        """
        return _REAL_SCORE(SimpleNamespace(scorers=[_Dispatching(boxes)]), patch, cases, label=label)

    monkeypatch.setattr(repo_judge, "_score", score)
    return boxes


def _stored_text(job_store: SQLiteJobStore, session_id: str) -> str:
    """Join the session's job record and every evidence row as text.

    Args:
        job_store: The store.
        session_id: The session.

    Returns:
        The stored text.
    """
    budget_id = _budget_id(job_store, session_id)
    rows = [*repo_judge._rows(job_store.engine, budget_id, repo_judge.JUDGE_SCOPE)]
    rows += repo_judge._rows(job_store.engine, budget_id, repo_judge.FINAL_SCOPE)
    return json.dumps([job_store.get_job(session_id), [row.result for row in rows]], default=str)


def test_an_unexpected_error_of_the_score_step_names_only_its_type(
    store: SQLiteJobStore, dispatched: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Keep the text of an unexpected scorer error out of every answer, store and log, through the real score step."""
    caplog.set_level("ERROR", logger=repo_judge.logger.name)
    session_id = _session(store, _cases(10))
    dispatched["dispatch_raise"] = KeyError("secret text")
    with pytest.raises(repo_judge.JudgeError) as failed:
        _call(store, session_id)
    assert (failed.value.code, failed.value.detail) == ("judge.score_failed", "KeyError")
    _call(store, session_id, OTHER_PATCH)
    dispatched["dispatch_raise"] = KeyError("secret text")
    assert repo_judge.end_session(store, store.get_job(session_id), ended_by="stop_run").status == "failed"
    assert store.get_job(session_id)["message"] == "The final score failed: KeyError"
    assert caplog.text.count("failed with KeyError") == 2
    assert "secret text" not in caplog.text
    assert "secret text" not in _stored_text(store, session_id)


def test_a_temporary_error_of_the_score_step_keeps_its_text(
    store: SQLiteJobStore, dispatched: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Keep the wrapped text of a temporary scorer error at the limit, with no unexpected-error log."""
    caplog.set_level("ERROR", logger=repo_judge.logger.name)
    session_id = _session(store, _cases(10))
    for _ in range(repo_judge._TEMPORARY_FAILURE_LIMIT - 1):
        dispatched["dispatch_raise"] = RepoFetchError("Connect GitHub again.")
        with pytest.raises(repo_judge.JudgePendingError):
            _call(store, session_id)
    dispatched["dispatch_raise"] = RepoFetchError("Connect GitHub again.")
    with pytest.raises(repo_judge.JudgeError) as failed:
        _call(store, session_id)
    assert failed.value.detail.startswith("scorer failed on the candidate: RepoFetchError:")
    assert "scorer failed on the candidate: RepoFetchError: Connect GitHub again." in _stored_text(store, session_id)
    assert "failed with" not in caplog.text


def test_a_budget_conflict_of_the_final_score_waits_and_a_retry_finishes(store: SQLiteJobStore, boxes: Any) -> None:
    """Answer budget.pending and change no status when the final score meets a fenced generation."""
    session_id = _session(store, _cases(10))
    _call(store, session_id)
    boxes["raise"] = BudgetFencedError("The execution generation is obsolete.")
    with pytest.raises(DomainError) as pending:
        repo_judge.end_session(store, store.get_job(session_id), ended_by="stop_run")
    assert (pending.value.code, pending.value.status_code) == ("budget.pending", 409)
    assert store.get_job(session_id)["status"] == "running"
    row = repo_judge._final_row(store.engine, _budget_id(store, session_id))
    assert (row.status, row.result["ended_by"]) == ("pending", "stop_run")
    assert repo_judge.end_session(store, store.get_job(session_id), ended_by="budget").status == "success"
    judge = store.get_job(session_id)["result"]["details"]["judge"]
    assert judge["held_out"] is True
    assert judge["ended_by"] == "stop_run"


def _block(job_store: SQLiteJobStore, session_id: str) -> None:
    """Block the session's budget, as a late settle with lost funding does.

    Args:
        job_store: The store.
        session_id: The session.
    """
    with Session(job_store.engine) as session, session.begin():
        session.execute(
            update(ExecutionBudgetModel)
            .where(ExecutionBudgetModel.id == _budget_id(job_store, session_id))
            .values(state="blocked", blocked_reason="funding_lost")
        )


@pytest.mark.parametrize("second", ["call", "stop_run"])
def test_a_second_request_during_a_live_end_on_a_blocked_budget_waits(
    store: SQLiteJobStore, boxes: Any, second: str
) -> None:
    """Answer budget.pending to a second request while the end scores on a blocked budget, and keep the end's score."""
    session_id = _session(store, _cases(10))
    _call(store, session_id)
    seen: dict[str, Any] = {}

    def concurrent() -> None:
        """Block the budget during the final score and send the second request."""
        _block(store, session_id)
        with pytest.raises(DomainError) as pending:
            if second == "call":
                _call(store, session_id, OTHER_PATCH)
            else:
                repo_judge.end_session(store, store.get_job(session_id), ended_by="stop_run")
        seen["code"] = pending.value.code
        seen["status"] = store.get_job(session_id)["status"]

    boxes["hook"] = concurrent
    answer = repo_judge.end_session(store, store.get_job(session_id), ended_by="stop_run")
    assert seen == {"code": "budget.pending", "status": "running"}
    job = store.get_job(session_id)
    assert answer.status == job["status"] == "success"
    assert job["result"]["details"]["judge"]["held_out"] is True


def test_the_end_answer_matches_the_stored_status(store: SQLiteJobStore, boxes: Any) -> None:
    """Answer the stored status, and store no result, when another writer ends the job during the final score."""
    session_id = _session(store, _cases(10))
    _call(store, session_id)

    def other_writer() -> None:
        """Stop the job as another request would."""
        store.update_job_if_status(session_id, ("running",), status="stopped", message="other writer")

    boxes["hook"] = other_writer
    answer = repo_judge.end_session(store, store.get_job(session_id), ended_by="stop_run")
    job = store.get_job(session_id)
    assert answer.status == job["status"] == "stopped"
    assert (job["message"], job.get("result")) == ("other writer", None)


def test_a_crashed_end_on_a_blocked_budget_does_not_hold_the_session(store: SQLiteJobStore, boxes: Any) -> None:
    """Stop a session on a blocked budget once the crashed end's lease expired."""
    session_id = _session(store, _cases(10))
    _call(store, session_id)
    boxes["raise"] = _Crash("the worker stopped")
    with pytest.raises(_Crash):
        repo_judge.end_session(store, store.get_job(session_id), ended_by="stop_run")
    _expire_the_end(store)
    _block(store, session_id)
    answer = repo_judge.end_session(store, store.get_job(session_id), ended_by="stop_run")
    assert answer.status == store.get_job(session_id)["status"] == "stopped"


def test_open_session_ids_lists_only_open_sessions_of_the_draft(store: SQLiteJobStore) -> None:
    """Find the draft's open sessions, not ended ones or another draft's."""
    first = _session(store, None, draft="d1")
    _session(store, None, draft="d2")
    assert repo_judge.open_session_ids(store.engine, USER, "d1") == [first]
    store.update_job(first, status="success")
    assert repo_judge.open_session_ids(store.engine, USER, "d1") == []


def _fake_open_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pass the admission, balance, model and quota gates of an open, which other tests cover.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    for name in (
        "_materialize_repo_cases",
        "_enforce_submission_admission",
        "fill_proposer_host_cli",
        "enforce_local_mode_sources",
        "_enforce_balance",
        "_admit_byok_models",
        "enforce_job_quota",
        "enforce_storage_quota",
    ):
        monkeypatch.setattr(repo_judge, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(repo_judge, "normalize_model_token_sources", lambda payload: ([], {}))
    monkeypatch.setattr(repo_judge, "_execution_overview", lambda payload: {})
    monkeypatch.setattr(repo_judge, "_persistable_payload", lambda payload, job_store: payload.model_dump(mode="json"))
    monkeypatch.setattr(repo_judge.repo_hosts, "resolve_commit", lambda provider, token, repository, branch: BASE)


def _open_request() -> repo_judge.BlackboxRunRequest:
    """Build the run request of a draft with a 500-cent limit.

    Returns:
        The request.
    """
    return repo_judge.BlackboxRunRequest.model_validate(
        {
            "recipe": "repo",
            "scorer": {"kind": "python", "metric_code": "def score(repo_path):\n    return 1.0, 'ok'\n"},
            "target": {"kind": "repo", "repo": {"repository": "acme/app", "branch": "main", "editable_paths": ["src"]}},
            "strategy": {"mode": "single", "engine": "gepa"},
            "reflection_model_config": {"name": "openai/gpt-5"},
            "max_cost_cents": 500,
        }
    )


def test_open_session_funds_attaches_and_pins_the_base(store: SQLiteJobStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """Create one budget, attach it to the row, and pin the branch's commit, once for a resume."""
    _fake_open_gates(monkeypatch)
    request = _open_request()
    user = SimpleNamespace(username=USER)
    key = repo_judge.judge_key("d1", "e" * 16)
    session_id = repo_judge.open_session(
        request, key, "budget-key", user, job_store=store, token=None, floor_cents=100, draft_id="d1"
    )
    job = store.get_job(session_id)
    assert job["status"] == "running"
    assert job["payload"]["judge"] == {"version": 1, "draft_id": "d1", "base_commit": BASE, "floor_cents": 100}
    budget = BudgetService(engine=store.engine).get(job["execution_budget_id"], USER)
    assert (budget.state, budget.job_id, budget.total_cents) == ("attached", session_id, 500)
    again = repo_judge.BlackboxRunRequest.model_validate(request.model_dump(mode="json", by_alias=True))
    assert (
        repo_judge.open_session(
            again,
            key,
            "budget-key",
            user,
            job_store=store,
            token=None,
            floor_cents=100,
            draft_id="d1",
            resume_id=session_id,
        )
        == session_id
    )
    assert store.get_job(session_id)["execution_budget_id"] == budget.id


def _open(job_store: SQLiteJobStore) -> str:
    """Open the judge session of draft ``d1`` with a 500-cent limit.

    Args:
        job_store: The store.

    Returns:
        The session id.
    """
    return repo_judge.open_session(
        _open_request(),
        repo_judge.judge_key("d1", "e" * 16),
        "budget-key",
        SimpleNamespace(username=USER),
        job_store=job_store,
        token=None,
        floor_cents=100,
        draft_id="d1",
    )


def test_a_stop_run_while_the_session_opens_keeps_it_ended_and_closes_its_budget(
    store: SQLiteJobStore, boxes: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep a session cancelled by a stop_run during its open, close the budget the open attached, and pay nothing."""
    _fake_open_gates(monkeypatch)
    owner = AuthenticatedUser(username=USER, role="user", groups=())
    stops: list[str] = []
    real = store.set_payload_overview

    def overview(session_id: str, data: dict[str, Any]) -> None:
        """Write the overview, then stop the session through the stop_run route, as the owner can from this point.

        Args:
            session_id: The session.
            data: The overview.
        """
        real(session_id, data)
        stops.append(cancel_optimization(store, session_id, owner, None).status)

    monkeypatch.setattr(store, "set_payload_overview", overview)
    session_id = _open(store)
    job = store.get_job(session_id)
    assert stops == ["cancelled"]
    assert (job["status"], job["message"]) == ("cancelled", "The judge session ended before its final score.")
    budget = BudgetService(engine=store.engine).find_by_creation_key(USER, "budget-key")
    assert budget is not None
    assert (budget.job_id, budget.state, budget.blocked_reason) == (session_id, "closed", "judge_ended")
    assert repo_judge.open_session_ids(store.engine, USER, "d1") == []
    answer = _call(store, session_id)
    assert (answer["status"], answer["run_status"]) == ("ended", "cancelled")
    assert boxes["paid"] == []


def test_a_cancel_before_the_opening_write_keeps_the_session_ended_and_attaches_no_budget(
    store: SQLiteJobStore, boxes: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep a row that an admin cancelled while it was still pending, and leave the new budget unattached."""
    _fake_open_gates(monkeypatch)
    real = store.create_job

    def create(session_id: str, **kwargs: Any) -> None:
        """Create the row, then cancel it as the admin cancel route does while the row is pending.

        Args:
            session_id: The session.
            **kwargs: The row's fields.
        """
        real(session_id, **kwargs)
        store.update_job(session_id, status="cancelled")

    monkeypatch.setattr(store, "create_job", create)
    session_id = _open(store)
    job = store.get_job(session_id)
    assert (job["status"], job["execution_budget_id"]) == ("cancelled", None)
    budget = BudgetService(engine=store.engine).find_by_creation_key(USER, "budget-key")
    assert budget is not None
    assert (budget.job_id, budget.state, budget.reserved_cents) == (None, "open", 0)
    answer = _call(store, session_id)
    assert (answer["status"], answer["run_status"]) == ("ended", "cancelled")
    assert boxes["paid"] == []


def test_a_stop_with_a_row_read_before_the_opens_last_write_closes_the_budget(
    store: SQLiteJobStore, boxes: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Close the budget when the stop_run read the row before the open's last write named the budget."""
    _fake_open_gates(monkeypatch)
    owner = AuthenticatedUser(username=USER, role="user", groups=())
    old: dict[str, Any] = {}
    real_overview = store.set_payload_overview
    real_get = store.get_job

    def overview(session_id: str, data: dict[str, Any]) -> None:
        """Write the overview, then keep the copy of the row that a stop_run could read at this point.

        Args:
            session_id: The session.
            data: The overview.
        """
        real_overview(session_id, data)
        old["job"] = real_get(session_id)

    def get(session_id: str) -> dict[str, Any]:
        """Give the old copy once, as the stop_run's own read, then the stored row.

        Args:
            session_id: The session.

        Returns:
            The row.
        """
        return old.pop("stale", None) or real_get(session_id)

    monkeypatch.setattr(store, "set_payload_overview", overview)
    session_id = _open(store)
    assert old["job"]["execution_budget_id"] is None
    assert store.get_job(session_id)["execution_budget_id"] is not None
    old["stale"] = old["job"]
    monkeypatch.setattr(store, "get_job", get)
    assert cancel_optimization(store, session_id, owner, None).status == "cancelled"
    assert "stale" not in old
    job = store.get_job(session_id)
    assert job["status"] == "cancelled"
    budget = BudgetService(engine=store.engine).find_by_creation_key(USER, "budget-key")
    assert budget is not None
    assert (budget.job_id, budget.state, budget.blocked_reason) == (session_id, "closed", "judge_ended")
    assert repo_judge.open_session_ids(store.engine, USER, "d1") == []
    answer = _call(store, session_id)
    assert (answer["status"], answer["run_status"]) == ("ended", "cancelled")
    assert boxes["paid"] == []


class _WriteError(Exception):
    """Stand for a database error on the open's first conditional write."""


def _resume(job_store: SQLiteJobStore, session_id: str) -> str:
    """Resume the open of draft ``d1`` with the key and budget key of :func:`_open`.

    Args:
        job_store: The store.
        session_id: The row the earlier open left behind.

    Returns:
        The session id.
    """
    return repo_judge.open_session(
        _open_request(),
        repo_judge.judge_key("d1", "e" * 16),
        "budget-key",
        SimpleNamespace(username=USER),
        job_store=job_store,
        token=None,
        floor_cents=100,
        draft_id="d1",
        resume_id=session_id,
    )


def test_an_open_that_stops_after_it_creates_the_row_answers_pending_and_resumes(
    store: SQLiteJobStore, boxes: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Treat a pending row with no payload as opening, and let a resume finish the open and score."""
    _fake_open_gates(monkeypatch)
    real = store.update_job_if_status

    def fail(*args: Any, **kwargs: Any) -> bool:
        """Fail the opening write once, then write as the store does.

        Args:
            *args: The write's arguments.
            **kwargs: The write's fields.

        Returns:
            Whether the write stored the row.

        Raises:
            _WriteError: On the first write.
        """
        monkeypatch.setattr(store, "update_job_if_status", real)
        raise _WriteError

    monkeypatch.setattr(store, "update_job_if_status", fail)
    with pytest.raises(_WriteError):
        _open(store)
    session_id = store.find_job_by_idempotency_key(USER, repo_judge.judge_key("d1", "e" * 16))
    job = store.get_job(session_id)
    assert (job["status"], job["payload"], job["execution_budget_id"]) == ("pending", None, None)
    assert repo_judge.is_opening(job)
    with pytest.raises(repo_judge.JudgePendingError):
        _call(store, session_id)
    budget = BudgetService(engine=store.engine).find_by_creation_key(USER, "budget-key")
    assert budget is not None
    assert (budget.job_id, budget.state) == (None, "open")
    assert boxes["paid"] == []
    assert _resume(store, session_id) == session_id
    job = store.get_job(session_id)
    assert job["status"] == "running"
    assert not repo_judge.is_opening(job)
    budget = BudgetService(engine=store.engine).get(job["execution_budget_id"], USER)
    assert (budget.job_id, budget.state) == (session_id, "attached")
    assert repo_judge.open_session_ids(store.engine, USER, "d1") == [session_id]
    assert _call(store, session_id)["status"] == "scored"


def _stuck_open(job_store: SQLiteJobStore, monkeypatch: pytest.MonkeyPatch) -> str:
    """Leave the row of an open that stopped after it created the row: ``pending`` with no payload.

    Args:
        job_store: The store.
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        The session id.
    """
    real = job_store.update_job_if_status

    def fail(*args: Any, **kwargs: Any) -> bool:
        """Fail the opening write once, then write as the store does.

        Args:
            *args: The write's arguments.
            **kwargs: The write's fields.

        Returns:
            Whether the write stored the row.

        Raises:
            _WriteError: On the first write.
        """
        monkeypatch.setattr(job_store, "update_job_if_status", real)
        raise _WriteError

    monkeypatch.setattr(job_store, "update_job_if_status", fail)
    with pytest.raises(_WriteError):
        _open(job_store)
    session_id = job_store.find_job_by_idempotency_key(USER, repo_judge.judge_key("d1", "e" * 16))
    job = job_store.get_job(session_id)
    assert (job["status"], job["payload"], job["execution_budget_id"]) == ("pending", None, None)
    return session_id


def test_an_admin_cancel_with_a_row_read_before_the_open_closes_the_budget(
    store: SQLiteJobStore, boxes: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End the session and close its budget when an admin read the pending row before a resume finished the open."""
    _fake_open_gates(monkeypatch)
    session_id = _stuck_open(store, monkeypatch)
    real_read = lifecycle.require_role_at_least
    resumed: list[str] = []

    def read_then_resume(*args: Any, **kwargs: Any) -> Any:
        """Read the row as the admin cancel does, then let the owner's resume finish the open.

        Args:
            *args: The read's arguments.
            **kwargs: The read's options.

        Returns:
            The admin's copy of the row and the role.
        """
        found = real_read(*args, **kwargs)
        assert found[0]["payload"] is None
        resumed.append(_resume(store, session_id))
        return found

    monkeypatch.setattr(lifecycle, "require_role_at_least", read_then_resume)
    assert cancel_optimization(store, session_id, ADMIN, None).status == "cancelled"
    assert resumed == [session_id]
    job = store.get_job(session_id)
    assert job["status"] == "cancelled"
    budget = BudgetService(engine=store.engine).find_by_creation_key(USER, "budget-key")
    assert budget is not None
    assert (budget.job_id, budget.state, budget.blocked_reason) == (session_id, "closed", "judge_ended")
    assert repo_judge.open_session_ids(store.engine, USER, "d1") == []
    answer = _call(store, session_id)
    assert (answer["status"], answer["run_status"]) == ("ended", "cancelled")
    assert boxes["paid"] == []


def test_an_admin_cancel_of_a_stuck_opening_row_ends_it_and_a_resume_does_not_reopen_it(
    store: SQLiteJobStore, boxes: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End a pending judge row with no payload on an admin cancel, and keep it ended after a same-argument resume."""
    _fake_open_gates(monkeypatch)
    session_id = _stuck_open(store, monkeypatch)
    assert repo_judge.is_session(store, session_id)
    assert cancel_optimization(store, session_id, ADMIN, None).status == "cancelled"
    assert store.get_job(session_id)["status"] == "cancelled"
    assert _resume(store, session_id) == session_id
    job = store.get_job(session_id)
    assert job["status"] == "cancelled"
    budget = BudgetService(engine=store.engine).find_by_creation_key(USER, "budget-key")
    assert budget is not None
    assert (budget.job_id, budget.state, budget.blocked_reason) == (session_id, "closed", "judge_ended")
    assert repo_judge.open_session_ids(store.engine, USER, "d1") == []
    answer = _call(store, session_id)
    assert (answer["status"], answer["run_status"]) == ("ended", "cancelled")
    assert boxes["paid"] == []


@pytest.mark.parametrize("key", ["submit-key", None])
def test_an_admin_cancel_of_a_pending_normal_row_with_no_payload_cancels_it(
    store: SQLiteJobStore, monkeypatch: pytest.MonkeyPatch, key: str | None
) -> None:
    """Cancel a normal pending row with no payload on the normal path, as before the judge check."""
    store.create_job("normal-run", username=USER, idempotency_key=key)
    assert not repo_judge.is_session(store, "normal-run")
    assert not repo_judge.is_session(store, "no-such-run")
    ends: list[Any] = []
    monkeypatch.setattr(lifecycle.repo_judge, "end_session", lambda *args, **kwargs: ends.append(args))
    assert cancel_optimization(store, "normal-run", ADMIN, None).status == "cancelled"
    assert store.get_job("normal-run")["status"] == "cancelled"
    assert ends == []


def test_a_resume_of_a_running_opening_row_finishes_the_open(
    store: SQLiteJobStore, boxes: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finish an open that stopped after its opening write, when the row is running with the opening payload."""
    _fake_open_gates(monkeypatch)

    def crash(session_id: str, data: dict[str, Any]) -> None:
        """Stop the open after its opening write and its budget attach.

        Args:
            session_id: The session.
            data: The overview.

        Raises:
            _Crash: Always.
        """
        raise _Crash("the open stopped")

    real = store.set_payload_overview
    monkeypatch.setattr(store, "set_payload_overview", crash)
    with pytest.raises(_Crash):
        _open(store)
    monkeypatch.setattr(store, "set_payload_overview", real)
    session_id = store.find_job_by_idempotency_key(USER, repo_judge.judge_key("d1", "e" * 16))
    job = store.get_job(session_id)
    assert job["status"] == "running"
    assert job["payload"] == {"judge": {"opening": True}}
    assert _resume(store, session_id) == session_id
    job = store.get_job(session_id)
    assert job["status"] == "running"
    assert not repo_judge.is_opening(job)
    budget = BudgetService(engine=store.engine).get(job["execution_budget_id"], USER)
    assert (budget.job_id, budget.state) == (session_id, "attached")
    assert _call(store, session_id)["status"] == "scored"

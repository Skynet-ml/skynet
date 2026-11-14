"""Tests for judge sessions (SKY-696).

Each test uses a real job store, a real budget ledger and real setup
evidence over in-memory SQLite. Only the staging of the repository and the
scorer boxes are faked: ``judge_scorer`` yields a staged repository without
a sandbox, and ``_score`` returns fixed scores. The fakes count each paid
attempt, so the tests prove that a refused call opens no paid box and that a
retry with the same arguments pays once.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.orm import Session

from ...billing.budgets import BudgetService
from ...billing.runtime import UsagePendingError
from ...exceptions import ServiceError
from ...models.blackbox import BlackboxNamedScore
from ...service_gateway.optimization.blackbox.service import FinalRun
from ...storage.models import BillingCustomerModel
from ...storage.tests.test_remote_jobstore import SQLiteJobStore
from .. import repo_judge
from ..errors import DomainError

USER = "alice@example.com"
BASE = "a" * 40
GOOD_PATCH = "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-a\n+b\n"
CONTRACT_PATCH = (
    "diff --git a/optimize.toml b/optimize.toml\n--- a/optimize.toml\n+++ b/optimize.toml\n@@ -1 +1 @@\n-a\n+b\n"
)


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


@pytest.fixture
def boxes(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the staging and the scorer boxes with counted fakes.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        A mutable record: ``paid`` lists each attempt that reached the paid
        boxes, ``scored`` lists each ``(patch, case count, label)``, ``score``
        maps a patch to its score, and ``raise`` makes the next score raise.
    """
    record: dict[str, Any] = {"paid": [], "scored": [], "score": {}, "raise": None, "staged": []}

    @contextmanager
    def scorer(
        payload: dict[str, Any], *, before_scoring: Any = None, owner_id: str, **_kwargs: Any
    ) -> Iterator[tuple[dict[str, Any], Any, Any]]:
        """Stage nothing and check the candidate as the real scorer does, before any paid box.

        Args:
            payload: The session payload.
            before_scoring: The judge's refusal check.
            owner_id: The attempt's owner id.
            **_kwargs: The other scorer arguments.

        Yields:
            A protected payload, a staged repository and no gateway.
        """
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
    assert again.status in ("success", "stopped")
    assert len(boxes["scored"]) == scored


@pytest.mark.parametrize("count", [0, 3])
def test_stop_without_held_out_cases_scores_every_case_once(store: SQLiteJobStore, boxes: Any, count: int) -> None:
    """Score every case, or the version once without cases, when the split holds nothing out."""
    session_id = _session(store, _cases(count) if count else None)
    _call(store, session_id)
    assert boxes["scored"][0][1] == count
    repo_judge.end_session(store, store.get_job(session_id), ended_by="stop_run")
    _patch, scored, label = boxes["scored"][-1]
    assert (scored, label) == (count, "best version")
    details = store.get_job(session_id)["result"]["details"]
    assert details["judge"]["held_out"] is False
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


def test_open_session_ids_lists_only_open_sessions_of_the_draft(store: SQLiteJobStore) -> None:
    """Find the draft's open sessions, not ended ones or another draft's."""
    first = _session(store, None, draft="d1")
    _session(store, None, draft="d2")
    assert repo_judge.open_session_ids(store.engine, USER, "d1") == [first]
    store.update_job(first, status="success")
    assert repo_judge.open_session_ids(store.engine, USER, "d1") == []


def test_open_session_funds_attaches_and_pins_the_base(store: SQLiteJobStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """Create one budget, attach it to the row, and pin the branch's commit, once for a resume."""
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
    request = repo_judge.BlackboxRunRequest.model_validate(
        {
            "recipe": "repo",
            "scorer": {"kind": "python", "metric_code": "def score(repo_path):\n    return 1.0, 'ok'\n"},
            "target": {"kind": "repo", "repo": {"repository": "acme/app", "branch": "main", "editable_paths": ["src"]}},
            "strategy": {"mode": "single", "engine": "gepa"},
            "reflection_model_config": {"name": "openai/gpt-5"},
            "max_cost_cents": 500,
        }
    )
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

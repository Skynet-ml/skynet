"""Verify legacy paid-submission replay identities are stable and secret-safe."""

from __future__ import annotations

from core.api.submission_idempotency import resolve_submission_replay_keys
from core.models import BlackboxRunRequest


def _run_request() -> BlackboxRunRequest:
    """Build one canonical black-box request with a scorer secret."""
    return BlackboxRunRequest.model_validate(
        {
            "seed_candidate": "first",
            "scorer": {
                "kind": "remote",
                "url": "https://evaluator.example/score",
                "secret": "remote-private-secret",
            },
            "reflection_model_config": {"name": "fixture/text"},
            "strategy": {"mode": "single", "engine": "gepa"},
        }
    )


def test_budgetless_keyless_request_uses_one_account_scoped_digest() -> None:
    """Use the same opaque key for lookup, budget creation, and persistence."""
    payload = _run_request()
    payload.username = "alice"

    first = resolve_submission_replay_keys(payload, username="alice", workflow="anything", supplied_key=None)
    repeated = resolve_submission_replay_keys(payload, username="alice", workflow="anything", supplied_key="   ")
    other_account = resolve_submission_replay_keys(payload, username="bob", workflow="anything", supplied_key=None)

    assert first.job == first.budget == repeated.job == repeated.budget
    assert first.job is not None
    assert first.job.startswith("legacy-paid-v1:")
    assert len(first.job) <= 128
    assert "remote-private-secret" not in first.job
    assert other_account.job != first.job


def test_synthesized_key_changes_with_credential_digest_without_exposing_it() -> None:
    """Bind credential changes to fresh work while keeping both values opaque."""
    first = _run_request()
    second = _run_request()
    first.username = second.username = "alice"
    second.scorer.secret = "replacement-secret"

    original = resolve_submission_replay_keys(first, username="alice", workflow="anything", supplied_key=None)
    changed = resolve_submission_replay_keys(second, username="alice", workflow="anything", supplied_key=None)

    assert original.job != changed.job
    assert "remote-private-secret" not in original.job
    assert "replacement-secret" not in changed.job


def test_explicit_key_keeps_existing_job_and_budget_semantics() -> None:
    """Preserve the public job key and the namespaced API budget key."""
    payload = _run_request()
    resolved = resolve_submission_replay_keys(
        payload,
        username="alice",
        workflow="anything",
        supplied_key=" requested-run ",
    )

    assert resolved.job == "requested-run"
    assert resolved.budget is not None
    assert resolved.budget.startswith("api:")
    assert resolved.budget != resolved.job


def test_modern_keyless_request_does_not_gain_implicit_job_deduplication() -> None:
    """Leave a wizard-owned budget under its explicit evidence contract."""
    payload = _run_request()
    payload.execution_budget_id = "budget-1"

    resolved = resolve_submission_replay_keys(payload, username="alice", workflow="anything", supplied_key=None)

    assert resolved.job is None
    assert resolved.budget is None


def test_workflow_and_typed_payload_are_part_of_the_synthesized_identity() -> None:
    """Give different effective work a distinct deterministic replay key."""
    payload = _run_request()
    payload.username = "alice"

    anything = resolve_submission_replay_keys(payload, username="alice", workflow="anything", supplied_key=None)
    mislabeled = resolve_submission_replay_keys(payload, username="alice", workflow="other", supplied_key=None)
    changed = payload.model_copy(update={"seed_candidate": "second"})
    changed_key = resolve_submission_replay_keys(changed, username="alice", workflow="anything", supplied_key=None)

    assert anything.job != mislabeled.job
    assert anything.job != changed_key.job
    assert "remote-private-secret" not in anything.job

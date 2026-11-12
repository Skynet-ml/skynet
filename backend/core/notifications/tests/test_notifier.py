"""Tests for ``core.notifications.notifier``: run and sharing events become ``notify`` calls."""

from __future__ import annotations

from typing import Any

import pytest

import core.notifications.notifier as notifier_module
from core.notifications.notifier import (
    notify_job_completed,
    notify_ownership_transfer,
    notify_role_change,
    notify_run_budget,
    notify_run_new_best,
    notify_run_stuck,
    notify_share_invite,
)


class _Recorder:
    """Stand-in for ``notify`` that records each call."""

    def __init__(self) -> None:
        """Start with no calls."""
        self.calls: list[dict[str, Any]] = []

    def __call__(self, username: str, event_type: str, optimization_id: str | None, facts: dict, **kwargs: Any):
        """Record one call.

        Args:
            username: Recipient.
            event_type: Catalog event type.
            optimization_id: The run.
            facts: Event facts.
            **kwargs: ``title``, ``dedupe_key`` and the rest.

        Returns:
            A fake notification id.
        """
        self.calls.append({"username": username, "event": event_type, "run": optimization_id, "facts": facts, **kwargs})
        return "n1"


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    """Route the notifier's ``notify`` calls into a recorder with a fixed run context.

    Args:
        monkeypatch: pytest's ``MonkeyPatch`` fixture.

    Returns:
        The recorder.
    """
    recorder = _Recorder()
    monkeypatch.setattr(notifier_module, "notify", recorder)
    monkeypatch.setattr(notifier_module, "_run_context", lambda _id: ("Support bot", 125.0))
    return recorder


def test_success_emits_finished_with_score_delta_and_duration(sent: _Recorder) -> None:
    """A successful run emits ``finished`` with its score, the delta and how long it ran."""
    notify_job_completed("run1", "alice", "success", baseline_score=40.0, optimized_score=55.0)

    call = sent.calls[0]
    assert (call["event"], call["run"], call["username"], call["title"]) == ("finished", "run1", "alice", "Support bot")
    assert call["facts"] == {"score": 55.0, "delta": 15.0, "duration": 125}
    assert call["dedupe_key"] == "finished:run1"


def test_failed_emits_failed_with_a_truncated_error(sent: _Recorder) -> None:
    """A failed run emits ``failed`` carrying the error as its detail."""
    notify_job_completed("run1", "alice", "failed", message="x" * 400)

    call = sent.calls[0]
    assert call["event"] == "failed"
    assert call["facts"]["detail"].endswith("...")
    assert len(call["facts"]["detail"]) < 200


def test_cancelled_and_unknown_statuses_emit_nothing(sent: _Recorder, caplog: pytest.LogCaptureFixture) -> None:
    """The owner cancelled the run themselves; an unknown status only logs."""
    notify_job_completed("run1", "alice", "cancelled")
    with caplog.at_level("WARNING", logger="core.notifications.notifier"):
        notify_job_completed("run1", "alice", "weird")

    assert sent.calls == []
    assert "unknown job status: weird" in caplog.text


@pytest.mark.parametrize(
    ("emit", "event", "facts"),
    [
        (lambda: notify_run_new_best("run9", "alice", 0.8, 0.5), "new_best", {"score": 0.8}),
        (lambda: notify_run_stuck("run9", "alice", 0.5, 0.25), "stuck", {"score": 0.5}),
        (lambda: notify_run_budget("run9", "alice", 800, 1000), "budget_near", {"spent": 8.0, "limit": 10.0}),
    ],
)
def test_in_run_events_carry_their_facts(sent: _Recorder, emit, event: str, facts: dict) -> None:
    """Each in-run event goes through ``notify`` once, with its key numbers."""
    emit()

    call = sent.calls[0]
    assert call["event"] == event
    for key, value in facts.items():
        assert call["facts"][key] == pytest.approx(value)
    assert call["dedupe_key"] == f"{event}:run9"


def test_new_best_carries_the_delta_vs_the_starting_score(sent: _Recorder) -> None:
    """The new-best delta is measured against the run's starting score."""
    notify_run_new_best("run9", "alice", 0.8, 0.5)

    assert sent.calls[0]["facts"]["delta"] == pytest.approx(0.3)


@pytest.mark.parametrize(
    ("emit", "recipient", "kind"),
    [
        (lambda: notify_share_invite("abc", "bob", "alice", "editor"), "bob", "invite"),
        (lambda: notify_role_change("abc", "bob", "alice", "viewer"), "bob", "role"),
        (lambda: notify_ownership_transfer("abc", "bob", "alice"), "bob", "transfer"),
    ],
)
def test_sharing_events_emit_shared_to_the_affected_user(sent: _Recorder, emit, recipient: str, kind: str) -> None:
    """Invites, role changes and transfers all emit ``shared``; they may repeat."""
    emit()

    call = sent.calls[0]
    assert (call["event"], call["username"], call["facts"]["kind"], call["facts"]["actor"]) == (
        "shared",
        recipient,
        kind,
        "alice",
    )
    assert "alice" in call["facts"]["detail"]
    assert call["dedupe_key"] is None


def test_missing_recipient_is_a_no_op(sent: _Recorder) -> None:
    """An event without a recipient never reaches ``notify``."""
    notify_job_completed("run1", "", "success")

    assert sent.calls == []

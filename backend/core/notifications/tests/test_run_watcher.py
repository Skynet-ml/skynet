"""Tests for the in-run event triggers in ``core.notifications.run_watcher``."""

from __future__ import annotations

from typing import Any

import pytest

import core.notifications.run_watcher as watcher_module
from core.constants import (
    PROGRESS_BASELINE,
    PROGRESS_CANDIDATE,
    PROGRESS_OPTIMIZER,
    TQDM_N_KEY,
    TQDM_TOTAL_KEY,
)
from core.notifications.catalog import BUDGET_NEAR, NEEDS_INPUT, NEW_BEST, STUCK
from core.notifications.notifier import (
    notify_run_budget,
    notify_run_new_best,
    notify_run_stuck,
)
from core.notifications.preferences import NotificationSettings, default_grid
from core.notifications.run_watcher import RunNotificationWatcher

_JOB = "run-1"
_USER = "owner@example.com"


class _ClaimStore:
    """In-memory stand-in for ``JobStore.claim_job_notification`` shared across watchers."""

    def __init__(self) -> None:
        """Start with no claims."""
        self.claims: dict[str, list[str]] = {}

    def claim_job_notification(
        self, optimization_id: str, claim: str, *, cap_prefix: str | None = None, cap: int | None = None
    ) -> bool:
        """Mirror the store's once-per-kind and per-prefix cap semantics.

        Args:
            optimization_id: Job the claim is for.
            claim: Email kind identifier.
            cap_prefix: Prefix counted against ``cap``.
            cap: Maximum claims with ``cap_prefix``.

        Returns:
            Whether the claim was newly recorded.
        """
        held = self.claims.setdefault(optimization_id, [])
        if claim in held:
            return False
        if cap_prefix is not None and cap is not None and sum(c.startswith(cap_prefix) for c in held) >= cap:
            return False
        held.append(claim)
        return True


class _Sent:
    """Record dispatched events instead of emitting them."""

    def __init__(self) -> None:
        """Start with no recorded events."""
        self.calls: list[tuple[Any, tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, send: Any, *args: Any, **kwargs: Any) -> None:
        """Record one dispatch.

        Args:
            send: Notifier function that would run.
            *args: Its positional arguments.
            **kwargs: Its keyword arguments.
        """
        self.calls.append((send, args, kwargs))

    def kinds(self) -> list[Any]:
        """Return the notifier functions dispatched, in order."""
        return [call[0] for call in self.calls]


def _settings(*, off: tuple[str, ...] = (), **overrides: Any) -> NotificationSettings:
    """Build settings from the default grid with some events switched off everywhere.

    Args:
        off: Event types to switch off on every channel.
        **overrides: ``NotificationSettings`` field overrides.

    Returns:
        The settings.
    """
    grid = default_grid()
    for event in off:
        grid[event] = dict.fromkeys(grid[event], False)
    return NotificationSettings(grid=grid, **overrides)


def _watcher(store: Any, sent: _Sent, *, off: tuple[str, ...] = (), **overrides: Any) -> RunNotificationWatcher:
    """Build a watcher over the default grid.

    Args:
        store: Shared claim store.
        sent: Dispatch recorder.
        off: Event types switched off everywhere.
        **overrides: ``NotificationSettings`` field overrides.

    Returns:
        The watcher under test.
    """
    return RunNotificationWatcher(
        optimization_id=_JOB,
        username=_USER,
        job_store=store,
        settings=_settings(off=off, **overrides),
        dispatch=sent,
    )


def test_events_switched_off_everywhere_emit_nothing() -> None:
    """An event with every grid cell off is never emitted."""
    sent = _Sent()
    watcher = _watcher(_ClaimStore(), sent, off=(NEW_BEST, STUCK, BUDGET_NEAR, NEEDS_INPUT))

    watcher.observe_progress(PROGRESS_CANDIDATE, {"score": 0.1})
    watcher.observe_progress(PROGRESS_CANDIDATE, {"score": 0.9})
    watcher.observe_spend(100, 100)

    assert sent.calls == []
    assert watcher.spend_check_due() is False


def test_for_run_skips_only_when_no_in_run_event_reaches_the_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """A watcher is built unless every in-run event is off everywhere, or there is no owner."""
    monkeypatch.setattr(watcher_module, "notification_settings", lambda _u: _settings())
    assert RunNotificationWatcher.for_run(_JOB, _USER, _ClaimStore()) is not None
    assert RunNotificationWatcher.for_run(_JOB, None, _ClaimStore()) is None

    monkeypatch.setattr(
        watcher_module,
        "notification_settings",
        lambda _u: _settings(off=(NEW_BEST, STUCK, BUDGET_NEAR, NEEDS_INPUT)),
    )
    assert RunNotificationWatcher.for_run(_JOB, _USER, _ClaimStore()) is None


def test_new_best_fires_once_after_the_starting_score() -> None:
    """The first score is the baseline; the first improvement mails once, later ones don't."""
    store, sent = _ClaimStore(), _Sent()
    watcher = _watcher(store, sent)

    watcher.observe_progress(PROGRESS_CANDIDATE, {"score": 0.5})
    assert sent.calls == []
    watcher.observe_progress(PROGRESS_CANDIDATE, {"score": 0.4})
    watcher.observe_progress(PROGRESS_CANDIDATE, {"score": 0.6})
    watcher.observe_progress(PROGRESS_CANDIDATE, {"score": 0.7})

    assert sent.kinds() == [notify_run_new_best]
    assert sent.calls[0][1] == (_JOB, _USER, 0.6, 0.5)


def test_new_best_is_not_resent_by_a_restarted_worker() -> None:
    """A fresh watcher for the same run (worker restart) respects the durable claim."""
    store, sent = _ClaimStore(), _Sent()
    first = _watcher(store, sent)
    first.observe_progress(PROGRESS_CANDIDATE, {"score": 0.5})
    first.observe_progress(PROGRESS_CANDIDATE, {"score": 0.6})

    resumed = _watcher(store, sent)
    resumed.observe_progress(PROGRESS_CANDIDATE, {"score": 0.6})
    resumed.observe_progress(PROGRESS_CANDIDATE, {"score": 0.8})

    assert sent.kinds() == [notify_run_new_best]


def test_stuck_fires_once_when_no_improvement_over_the_fraction() -> None:
    """Stalling for ``stuck_fraction`` of the planned calls mails once."""
    sent = _Sent()
    watcher = _watcher(_ClaimStore(), sent, stuck_fraction=0.25)

    watcher.observe_progress(PROGRESS_OPTIMIZER, {TQDM_TOTAL_KEY: 100, TQDM_N_KEY: 0})
    watcher.observe_progress(PROGRESS_CANDIDATE, {"score": 0.5, "discovered_at_evals": 0})
    watcher.observe_progress(PROGRESS_OPTIMIZER, {TQDM_TOTAL_KEY: 100, TQDM_N_KEY: 24})
    assert sent.calls == []
    watcher.observe_progress(PROGRESS_OPTIMIZER, {TQDM_TOTAL_KEY: 100, TQDM_N_KEY: 25})
    watcher.observe_progress(PROGRESS_OPTIMIZER, {TQDM_TOTAL_KEY: 100, TQDM_N_KEY: 60})

    assert sent.kinds() == [notify_run_stuck]
    assert sent.calls[0][1] == (_JOB, _USER, 0.5, 0.25)


def test_stuck_resets_on_improvement() -> None:
    """An improvement restarts the stall window."""
    sent = _Sent()
    watcher = _watcher(_ClaimStore(), sent, stuck_fraction=0.25)

    watcher.observe_progress(PROGRESS_OPTIMIZER, {TQDM_TOTAL_KEY: 100, TQDM_N_KEY: 0})
    watcher.observe_progress(PROGRESS_CANDIDATE, {"score": 0.5})
    watcher.observe_progress(PROGRESS_OPTIMIZER, {TQDM_TOTAL_KEY: 100, TQDM_N_KEY: 20})
    watcher.observe_progress(PROGRESS_CANDIDATE, {"score": 0.6})
    watcher.observe_progress(PROGRESS_OPTIMIZER, {TQDM_TOTAL_KEY: 100, TQDM_N_KEY: 40})

    assert notify_run_stuck not in sent.kinds()


def test_budget_alert_fires_once_at_the_fraction() -> None:
    """Spend at the alert fraction of the limit mails once; uncapped runs never do."""
    sent = _Sent()
    watcher = _watcher(_ClaimStore(), sent, budget_alert_fraction=0.8)

    watcher.observe_spend(500, None)
    watcher.observe_spend(79, 100)
    assert sent.calls == []
    watcher.observe_spend(80, 100)
    watcher.observe_spend(95, 100)

    assert sent.kinds() == [notify_run_budget]
    assert sent.calls[0][1] == (_JOB, _USER, 80, 100)


def test_store_without_claim_support_sends_nothing() -> None:
    """Without a durable guard the watcher prefers silence over repeats."""
    sent = _Sent()
    watcher = RunNotificationWatcher(
        optimization_id=_JOB,
        username=_USER,
        job_store=object(),
        settings=_settings(),
        dispatch=sent,
    )

    watcher.observe_progress(PROGRESS_BASELINE, {})

    assert sent.calls == []


def test_observe_progress_never_raises() -> None:
    """A failing store or odd metrics never break the worker's progress drain."""

    class _Broken:
        """Store whose claim always raises."""

        def claim_job_notification(self, *_args: Any, **_kwargs: Any) -> bool:
            """Raise like a lost database connection."""
            raise RuntimeError("db down")

    sent = _Sent()
    watcher = RunNotificationWatcher(
        optimization_id=_JOB,
        username=_USER,
        job_store=_Broken(),
        settings=_settings(),
        dispatch=sent,
    )

    watcher.observe_progress(PROGRESS_BASELINE, {})
    watcher.observe_progress(PROGRESS_CANDIDATE, {"score": float("nan")})
    watcher.observe_progress(PROGRESS_CANDIDATE, "not-a-dict")

    assert sent.calls == []

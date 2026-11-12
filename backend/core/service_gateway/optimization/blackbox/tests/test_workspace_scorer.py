"""Tests for the sandbox-pool fan-out."""

from __future__ import annotations

import contextvars
import threading
import time
from typing import Any

import pytest

from core.billing.signals import BudgetReached
from core.sandbox import sync_bridge
from core.sandbox.timeouts import Phase

from .. import workspace_scorer as workspace_scorer_mod

CALLER_TAG: contextvars.ContextVar[str] = contextvars.ContextVar("caller_tag", default="unset")


@pytest.mark.parametrize("phase", [Phase.RUN, Phase.SCORE])
def test_run_on_pool_keeps_the_call_order_and_the_callers_context(phase: Phase) -> None:
    """Calls that end in reverse order still come back in the calls' order, each in the caller's context."""
    token = CALLER_TAG.set("caller")
    try:

        def call(n: int) -> tuple[int, str]:
            """Sleep less for later calls, so they end first."""
            time.sleep(0.01 * (4 - n))
            return n, CALLER_TAG.get()

        results = workspace_scorer_mod.run_on_pool(phase, [lambda n=n: call(n) for n in range(4)], 4)
    finally:
        CALLER_TAG.reset(token)

    assert results == [(n, "caller") for n in range(4)]


def test_run_on_pool_raises_the_earliest_failure_and_starts_no_more_calls() -> None:
    """A failed call stops the fan-out; its error reaches the caller unchanged."""
    started: list[int] = []
    lock = threading.Lock()

    def call(n: int) -> int:
        """Fail the first call; the others take long enough for the stop to land."""
        with lock:
            started.append(n)
        if n == 0:
            raise ValueError("boom")
        time.sleep(0.2)
        return n

    with pytest.raises(ValueError, match="boom"):
        workspace_scorer_mod.run_on_pool(Phase.SCORE, [lambda n=n: call(n) for n in range(6)], 2)

    assert set(started) <= {0, 1, 2}


def test_run_on_pool_cancel_waits_for_the_calls_in_flight_and_starts_no_more() -> None:
    """A cancel waits for the calls in flight to end, starts no queued call, and leaves the pool usable."""
    release = threading.Event()
    started = threading.Semaphore(0)
    ended = threading.Event()

    def call() -> int:
        """Block until the test lets the call end."""
        started.release()
        release.wait(timeout=10)
        return 1

    items = [(contextvars.copy_context(), call) for _ in range(3)]

    async def tracked() -> Any:
        """Run the fan-out two wide and mark when its task ends."""
        try:
            return await workspace_scorer_mod._run_on_pool(Phase.RUN, items, 2)
        finally:
            ended.set()

    future = sync_bridge.submit(tracked)
    assert started.acquire(timeout=5)
    assert started.acquire(timeout=5)
    future.cancel()
    time.sleep(0.05)
    assert not ended.is_set()
    release.set()

    assert ended.wait(timeout=5)
    assert not started.acquire(timeout=0.1)
    assert workspace_scorer_mod.run_on_pool(Phase.RUN, [lambda: 7], 1) == [7]


def test_run_on_pool_has_no_process_box_limit() -> None:
    """Two fan-outs of 12 hold 24 slot boxes open at once; no process limit makes either wait (SKY-542)."""
    width = 12
    barrier = threading.Barrier(2 * width, timeout=10)

    def call(n: int) -> int:
        """Return only once every call of both fan-outs is in flight at the same time."""
        barrier.wait()
        return n

    results: list[list[int]] = []
    fan_outs = [
        threading.Thread(
            target=lambda: results.append(
                workspace_scorer_mod.run_on_pool(Phase.SCORE, [lambda n=n: call(n) for n in range(width)], width)
            )
        )
        for _ in range(2)
    ]
    for thread in fan_outs:
        thread.start()
    for thread in fan_outs:
        thread.join(timeout=15)

    assert not barrier.broken
    assert results == [list(range(width)), list(range(width))]
    assert not hasattr(workspace_scorer_mod, "POOL_BOX_LIMIT")


async def _portal_echo() -> str:
    """Return a fixed value on the portal loop."""
    return "ok"


@pytest.mark.parametrize("stop", [BudgetReached, KeyboardInterrupt])
@pytest.mark.parametrize("phase", [Phase.RUN, Phase.SCORE])
def test_a_base_exception_in_a_call_reaches_the_caller_and_keeps_the_portal(
    phase: Phase, stop: type[BaseException]
) -> None:
    """BudgetReached or KeyboardInterrupt in a call reaches the caller; the portal thread lives on."""
    sync_bridge.run(_portal_echo)
    portal_thread = sync_bridge._thread
    assert portal_thread is not None

    def call() -> int:
        """Stop like a budget check or an interrupt in the call."""
        raise stop("stop")

    with pytest.raises(stop):
        workspace_scorer_mod.run_on_pool(phase, [call, lambda: 1], 2)

    time.sleep(0.1)
    assert portal_thread.is_alive()
    assert sync_bridge._thread is portal_thread
    assert sync_bridge.run(_portal_echo) == "ok"
    assert workspace_scorer_mod.run_on_pool(phase, [lambda: 7], 1) == [7]


def test_the_earliest_failure_wins_over_a_later_budget_stop() -> None:
    """A ValueError at index 0 that ends last still wins over BudgetReached at index 1."""
    later = threading.Event()

    def first() -> int:
        """Fail only after the second call stopped on budget."""
        later.wait(timeout=5)
        time.sleep(0.05)
        raise ValueError("first")

    def second() -> int:
        """Stop on budget at once."""
        later.set()
        raise BudgetReached("budget")

    with pytest.raises(ValueError, match="first"):
        workspace_scorer_mod.run_on_pool(Phase.RUN, [first, second], 2)


def test_run_on_pool_refuses_a_phase_other_than_run_or_score() -> None:
    """Pooled calls run only in the run or score phase."""
    with pytest.raises(ValueError, match="run or score"):
        workspace_scorer_mod.run_on_pool(Phase.SETUP, [lambda: 1], 1)

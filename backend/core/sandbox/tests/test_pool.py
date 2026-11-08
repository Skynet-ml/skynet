"""Tests for the sandbox pool, the box hooks and the live counts."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

import pytest

from core.sandbox import ResourceRequest, SandboxEnvironment, SandboxSpec
from core.sandbox import pool as pool_module
from core.sandbox.errors import SandboxFatalError, SandboxTransientError
from core.sandbox.hooks import BoxEvent, BoxEventInfo, BoxHooks, BoxState, LiveCounts
from core.sandbox.pool import BoxOutcome, BoxWork, SandboxPool
from core.sandbox.retry import RetryPolicy
from core.sandbox.tests.fake_provider import InMemorySandbox
from core.sandbox.timeouts import Phase

# A deadline for the waits on the pool, so a bug fails the test fast and
# does not hang the run.
DEADLINE = 2.0


class _Tracker:
    """Count the boxes that live at once and the boxes that were made."""

    def __init__(self) -> None:
        """Start with no boxes."""
        self.live = 0
        self.peak = 0
        self.created = 0
        self.boxes: list[_TrackedSandbox] = []


class _TrackedSandbox(InMemorySandbox):
    """Act as the in-memory sandbox and report its start and stop to a tracker."""

    def __init__(self, tracker: _Tracker) -> None:
        """Create a stopped box that reports to the tracker.

        Args:
            tracker: The tracker that counts live boxes.
        """
        super().__init__(SandboxSpec(lifetime_seconds=60, resources=ResourceRequest(cpus=1)))
        self.tracker = tracker
        tracker.created += 1
        tracker.boxes.append(self)

    async def start(self) -> None:
        """Start the box and count it as live."""
        await super().start()
        self.tracker.live += 1
        self.tracker.peak = max(self.tracker.peak, self.tracker.live)

    async def stop(self) -> None:
        """Stop the box and stop counting it, once."""
        was_running = self.running
        await super().stop()
        if was_running:
            self.tracker.live -= 1


class _Outcomes:
    """Collect the box outcomes."""

    def __init__(self) -> None:
        """Start with no outcomes."""
        self.items: list[BoxOutcome[int, object]] = []

    async def __call__(self, outcome: BoxOutcome[int, object]) -> None:
        """Store one outcome.

        Args:
            outcome: The outcome of one box.
        """
        self.items.append(outcome)


async def _numbers(count: int) -> AsyncIterator[int]:
    """Yield the numbers from 0 to count - 1.

    Args:
        count: How many numbers to yield.

    Yields:
        The next number.
    """
    for number in range(count):
        yield number


async def _settle() -> None:
    """Let every ready task run until it blocks again."""
    for _ in range(50):
        await asyncio.sleep(0)


def _work(tracker: _Tracker, **steps: object) -> BoxWork[int, object]:
    """Build box work that makes tracked boxes, with a run step that returns the candidate.

    Args:
        tracker: The tracker of the boxes.
        **steps: Steps that replace the default ``run`` or add ``setup`` and ``score``.

    Returns:
        The box work.
    """

    async def run(env: SandboxEnvironment, candidate: int) -> int:
        """Yield to the loop once, then return the candidate.

        Args:
            env: The box.
            candidate: The candidate.

        Returns:
            The candidate.
        """
        await asyncio.sleep(0)
        return candidate

    return BoxWork(create=lambda _candidate: _TrackedSandbox(tracker), **{"run": run, **steps})  # type: ignore[arg-type]


async def test_at_most_the_provider_limit_runs_at_once() -> None:
    """Run 1000 candidates with limit 5: at most 5 boxes live, and every box finishes."""
    tracker = _Tracker()
    outcomes = _Outcomes()
    pool = SandboxPool({"fake": 5})
    async with asyncio.timeout(DEADLINE * 5):
        await pool.run(_numbers(1000), _work(tracker), outcomes, workers=20)
    assert tracker.peak == 5
    assert tracker.live == 0
    assert sorted(outcome.result for outcome in outcomes.items) == list(range(1000))
    assert all(outcome.ok for outcome in outcomes.items)


async def test_phase_pools_bound_each_phase() -> None:
    """Keep each phase at or below its own limit, and reach the limit.

    The run step is the longest, so boxes queue for the run permit and the
    run pool fills.
    """
    tracker = _Tracker()
    counts = LiveCounts()
    peaks: dict[BoxState, int] = dict.fromkeys(BoxState, 0)

    async def record(info: BoxEventInfo) -> None:
        """Update the counts, then record the peak of each state.

        Args:
            info: The box event.
        """
        await counts(info)
        for state, count in counts.snapshot().items():
            peaks[state] = max(peaks[state], count)

    def step(turns: int) -> object:
        """Build a step that yields to the loop a set number of times.

        Args:
            turns: How many times the step yields.

        Returns:
            The step.
        """

        async def phase(env: SandboxEnvironment, candidate: int) -> int:
            """Yield to the loop, then return the candidate.

            Args:
                env: The box.
                candidate: The candidate.

            Returns:
                The candidate.
            """
            for _ in range(turns):
                await asyncio.sleep(0)
            return candidate

        return phase

    hooks = BoxHooks()
    hooks.add(record)
    pool = SandboxPool({"fake": 8}, phase_limits={Phase.SETUP: 2, Phase.RUN: 3, Phase.SCORE: 1}, hooks=hooks)
    outcomes = _Outcomes()
    async with asyncio.timeout(DEADLINE):
        await pool.run(_numbers(60), _work(tracker, setup=step(2), run=step(20), score=step(1)), outcomes)
    assert len(outcomes.items) == 60
    assert (peaks[BoxState.SETUP], peaks[BoxState.RUN], peaks[BoxState.SCORE]) == (2, 3, 1)
    assert tracker.peak <= 8


async def test_producer_holds_at_most_queue_size_candidates() -> None:
    """Read only the candidates that the workers hold plus queue_size waiting ones."""
    tracker = _Tracker()
    gate = asyncio.Event()
    read = 0

    async def candidates() -> AsyncIterator[int]:
        """Yield 100 numbers and count how many the pool read.

        Yields:
            The next number.
        """
        nonlocal read
        for number in range(100):
            read += 1
            yield number

    async def run(env: SandboxEnvironment, candidate: int) -> int:
        """Wait for the gate, then return the candidate.

        Args:
            env: The box.
            candidate: The candidate.

        Returns:
            The candidate.
        """
        await gate.wait()
        return candidate

    outcomes = _Outcomes()
    pool = SandboxPool({"fake": 2})
    task = asyncio.create_task(pool.run(candidates(), _work(tracker, run=run), outcomes, queue_size=5))
    await _settle()
    assert tracker.live == 2
    assert read == 2 + 5
    gate.set()
    async with asyncio.timeout(DEADLINE):
        await task
    assert read == 100
    assert len(outcomes.items) == 100


async def test_cancel_stops_producer_and_boxes_and_releases_permits() -> None:
    """Cancel the run: close the iterator, stop every box, then reuse every permit."""
    tracker = _Tracker()
    closed = asyncio.Event()

    async def endless() -> AsyncIterator[int]:
        """Yield numbers forever and note when the pool closes the iterator.

        Yields:
            The next number.
        """
        number = 0
        try:
            while True:
                yield number
                number += 1
        finally:
            closed.set()

    async def hang(env: SandboxEnvironment, candidate: int) -> int:
        """Wait forever.

        Args:
            env: The box.
            candidate: The candidate.

        Returns:
            Nothing; the step never returns.
        """
        await asyncio.Event().wait()
        return candidate

    counts = LiveCounts()
    cancelled: list[str] = []

    async def note_cancel(info: BoxEventInfo) -> None:
        """Record a cancelled box.

        Args:
            info: The box event.
        """
        cancelled.append(info.box_id)

    hooks = BoxHooks()
    hooks.add(counts)
    hooks.add(note_cancel, BoxEvent.CANCELLED)
    pool = SandboxPool({"fake": 3}, phase_limits={Phase.RUN: 3}, hooks=hooks)
    task = asyncio.create_task(pool.run(endless(), _work(tracker, run=hang), _Outcomes()))
    await _settle()
    assert counts.snapshot()[BoxState.RUN] == 3
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    assert tracker.live == 0
    assert len(cancelled) == 3
    assert counts.active == 0

    barrier = asyncio.Barrier(3)

    async def meet(env: SandboxEnvironment, candidate: int) -> int:
        """Wait until three boxes run at once, then return the candidate.

        Args:
            env: The box.
            candidate: The candidate.

        Returns:
            The candidate.
        """
        await barrier.wait()
        return candidate

    outcomes = _Outcomes()
    async with asyncio.timeout(DEADLINE):
        await pool.run(_numbers(3), _work(tracker, run=meet), outcomes)
    assert [outcome.ok for outcome in outcomes.items] == [True, True, True]


async def test_hooks_fire_in_order_and_a_failing_hook_does_not_fail_the_box(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Send each event in order to every hook, log a failing hook, and keep the box result."""
    events: list[BoxEvent] = []

    async def broken(info: BoxEventInfo) -> None:
        """Fail on every event.

        Args:
            info: The box event.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("hook failure")

    async def record(info: BoxEventInfo) -> None:
        """Record the event.

        Args:
            info: The box event.
        """
        events.append(info.event)

    async def step(env: SandboxEnvironment, candidate: int) -> int:
        """Return the candidate.

        Args:
            env: The box.
            candidate: The candidate.

        Returns:
            The candidate.
        """
        return candidate

    hooks = BoxHooks()
    hooks.add(broken)
    hooks.add(record)
    outcomes = _Outcomes()
    pool = SandboxPool({"fake": 1}, hooks=hooks)
    with caplog.at_level(logging.ERROR, logger="core.sandbox.hooks"):
        await pool.run(_numbers(1), _work(_Tracker(), setup=step, run=step, score=step), outcomes)
    assert events == [
        BoxEvent.STARTED,
        BoxEvent.ENVIRONMENT_STARTED,
        BoxEvent.SETUP_STARTED,
        BoxEvent.SETUP_ENDED,
        BoxEvent.RUN_STARTED,
        BoxEvent.RUN_ENDED,
        BoxEvent.SCORE_STARTED,
        BoxEvent.SCORE_ENDED,
        BoxEvent.ENDED,
    ]
    assert outcomes.items[0].ok
    assert outcomes.items[0].result == 0
    assert len([record for record in caplog.records if record.name == "core.sandbox.hooks"]) == 9


async def test_hook_for_some_events_and_a_failing_box_ends_with_its_error() -> None:
    """Send only the chosen events, and give ENDED the error of a failing box."""
    seen: list[BoxEventInfo] = []

    async def record(info: BoxEventInfo) -> None:
        """Record the event.

        Args:
            info: The box event.
        """
        seen.append(info)

    async def fail(env: SandboxEnvironment, candidate: int) -> int:
        """Fail the run.

        Args:
            env: The box.
            candidate: The candidate.

        Raises:
            ValueError: Always.
        """
        raise ValueError("run failed")

    tracker = _Tracker()
    hooks = BoxHooks()
    hooks.add(record, BoxEvent.RUN_ENDED, BoxEvent.ENDED)
    outcomes = _Outcomes()
    await SandboxPool({"fake": 2}, hooks=hooks).run(_numbers(1), _work(tracker, run=fail), outcomes)
    assert [info.event for info in seen] == [BoxEvent.RUN_ENDED, BoxEvent.ENDED]
    assert seen[0].phase is Phase.RUN
    assert isinstance(seen[1].error, ValueError)
    assert isinstance(outcomes.items[0].error, ValueError)
    assert tracker.live == 0


async def test_live_counts_match_the_boxes_in_each_phase() -> None:
    """Count boxes waiting for a run permit, running and scoring, then none."""
    tracker = _Tracker()
    run_gate = asyncio.Event()
    score_gate = asyncio.Event()

    async def run(env: SandboxEnvironment, candidate: int) -> int:
        """Wait for the run gate.

        Args:
            env: The box.
            candidate: The candidate.

        Returns:
            The candidate.
        """
        await run_gate.wait()
        return candidate

    async def score(env: SandboxEnvironment, candidate: int) -> int:
        """Wait for the score gate.

        Args:
            env: The box.
            candidate: The candidate.

        Returns:
            The candidate.
        """
        await score_gate.wait()
        return candidate

    counts = LiveCounts()
    hooks = BoxHooks()
    hooks.add(counts)
    pool = SandboxPool({"fake": 10}, phase_limits={Phase.RUN: 2}, hooks=hooks)
    task = asyncio.create_task(pool.run(_numbers(6), _work(tracker, run=run, score=score), _Outcomes()))
    await _settle()
    snapshot = counts.snapshot()
    assert (snapshot[BoxState.RUN], snapshot[BoxState.IDLE], snapshot[BoxState.SCORE]) == (2, 4, 0)
    assert counts.active == tracker.live == 6
    run_gate.set()
    await _settle()
    snapshot = counts.snapshot()
    assert (snapshot[BoxState.RUN], snapshot[BoxState.IDLE], snapshot[BoxState.SCORE]) == (0, 0, 6)
    score_gate.set()
    async with asyncio.timeout(DEADLINE):
        await task
    assert counts.active == 0
    assert set(counts.snapshot().values()) == {0}


async def test_no_retry_by_default_and_retry_count_from_policy() -> None:
    """Make one box for a transient error by default, and one more for each policy retry."""

    async def flaky(env: SandboxEnvironment, candidate: int) -> int:
        """Fail with a transient error.

        Args:
            env: The box.
            candidate: The candidate.

        Raises:
            SandboxTransientError: Always.
        """
        raise SandboxTransientError("503")

    tracker = _Tracker()
    outcomes = _Outcomes()
    await SandboxPool({"fake": 1}).run(_numbers(1), _work(tracker, run=flaky), outcomes, key=str)
    assert tracker.created == 1
    assert isinstance(outcomes.items[0].error, SandboxTransientError)

    started: list[str] = []

    async def record(info: BoxEventInfo) -> None:
        """Record the attempt key.

        Args:
            info: The box event.
        """
        started.append(info.box_id)

    hooks = BoxHooks()
    hooks.add(record, BoxEvent.STARTED)
    tracker = _Tracker()
    policy = RetryPolicy(max_retries=1, wait_min=0, wait_max=0)
    pool = SandboxPool({"fake": 1}, hooks=hooks, retry_policy=policy)
    await pool.run(_numbers(1), _work(tracker, run=flaky), _Outcomes(), key=lambda n: f"cand-{n}")
    assert tracker.created == 2
    assert started == ["cand-0:attempt-1", "cand-0:attempt-2"]


async def test_unknown_provider_and_stop_failure_are_box_outcomes(caplog: pytest.LogCaptureFixture) -> None:
    """Fail a box whose provider has no limit, and keep the result when stop fails."""
    tracker = _Tracker()
    outcomes = _Outcomes()
    await SandboxPool({"vercel": 1}).run(_numbers(1), _work(tracker), outcomes)
    assert isinstance(outcomes.items[0].error, ValueError)
    assert tracker.created == 1

    def broken_stop(_candidate: int) -> SandboxEnvironment:
        """Make a box whose stop fails.

        Args:
            _candidate: The candidate.

        Returns:
            The box.
        """
        box = _TrackedSandbox(tracker)
        box.fail_stop = True
        return box

    outcomes = _Outcomes()
    work = BoxWork(create=broken_stop, run=_work(tracker).run)
    with caplog.at_level(logging.ERROR, logger="core.sandbox.pool"):
        await SandboxPool({"fake": 1}).run(_numbers(1), work, outcomes)
    assert outcomes.items[0].result == 0
    assert any("did not stop" in record.getMessage() for record in caplog.records)


async def test_iterator_error_stops_the_run_with_that_error() -> None:
    """Raise the candidate iterator's own error, not an exception group."""

    async def broken() -> AsyncIterator[int]:
        """Yield one number, then fail.

        Yields:
            The number 0.

        Raises:
            KeyError: After the first number.
        """
        yield 0
        raise KeyError("source")

    outcomes = _Outcomes()
    with pytest.raises(KeyError):
        async with asyncio.timeout(DEADLINE):
            await SandboxPool({"fake": 2}).run(broken(), _work(_Tracker()), outcomes)


async def test_stray_cancel_in_a_phase_is_a_box_outcome() -> None:
    """Turn a cancel that nobody asked for into a fatal outcome, so one worker does not hang."""
    tracker = _Tracker()
    events: list[BoxEventInfo] = []

    async def note(info: BoxEventInfo) -> None:
        """Record a box end or cancel.

        Args:
            info: The box event.
        """
        events.append(info)

    async def stray_cancel(env: SandboxEnvironment, candidate: int) -> int:
        """Await a cancelled inner task for candidate 0, else return the candidate.

        Args:
            env: The box.
            candidate: The candidate.

        Returns:
            The candidate.
        """
        if candidate == 0:
            inner = asyncio.create_task(asyncio.sleep(10))
            await asyncio.sleep(0)
            inner.cancel()
            await inner
        return candidate

    hooks = BoxHooks()
    hooks.add(note, BoxEvent.ENDED, BoxEvent.CANCELLED)
    outcomes = _Outcomes()
    pool = SandboxPool({"fake": 1}, phase_limits={Phase.RUN: 1}, hooks=hooks)
    async with asyncio.timeout(DEADLINE):
        await pool.run(_numbers(3), _work(tracker, run=stray_cancel), outcomes, workers=1)
    assert [outcome.candidate for outcome in outcomes.items] == [0, 1, 2]
    assert isinstance(outcomes.items[0].error, SandboxFatalError)
    assert [outcome.result for outcome in outcomes.items[1:]] == [1, 2]
    assert [info.event for info in events] == [BoxEvent.ENDED] * 3
    assert tracker.live == 0


async def test_stray_cancel_in_the_iterator_reaches_the_caller() -> None:
    """Raise a fatal error from run() when the iterator raises a stray cancel, so no candidate is lost silently."""
    tracker = _Tracker()

    async def stray_cancel() -> AsyncIterator[int]:
        """Yield two numbers, then await a cancelled inner task.

        Yields:
            The numbers 0 and 1, and 99 if the stray cancel did not stop the iterator.
        """
        yield 0
        yield 1
        inner = asyncio.create_task(asyncio.sleep(10))
        await asyncio.sleep(0)
        inner.cancel()
        await inner
        yield 99

    outcomes = _Outcomes()
    with pytest.raises(SandboxFatalError, match="candidate iterator"):
        async with asyncio.timeout(DEADLINE):
            await SandboxPool({"fake": 2}).run(stray_cancel(), _work(tracker), outcomes)
    assert 99 not in [outcome.candidate for outcome in outcomes.items]
    assert tracker.live == 0


async def test_cancel_during_stop_lets_the_stop_finish() -> None:
    """Finish a box's stop when the pool is cancelled during it, so no box keeps running."""
    tracker = _Tracker()
    stopping = asyncio.Event()
    release = asyncio.Event()

    class _SlowStop(_TrackedSandbox):
        """Act as a tracked box whose stop waits for the test."""

        async def stop(self) -> None:
            """Signal the stop, wait for the release, then stop the box."""
            stopping.set()
            await release.wait()
            await super().stop()

    work = BoxWork(create=lambda _candidate: _SlowStop(tracker), run=_work(tracker).run)
    events: list[BoxEvent] = []

    async def note(info: BoxEventInfo) -> None:
        """Record a box end or cancel.

        Args:
            info: The box event.
        """
        events.append(info.event)

    hooks = BoxHooks()
    hooks.add(note, BoxEvent.ENDED, BoxEvent.CANCELLED)
    run = asyncio.create_task(SandboxPool({"fake": 1}, hooks=hooks).run(_numbers(1), work, _Outcomes()))
    async with asyncio.timeout(DEADLINE):
        await stopping.wait()
    run.cancel()
    await _settle()
    run.cancel()
    await _settle()
    assert not run.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        async with asyncio.timeout(DEADLINE):
            await run
    assert tracker.live == 0
    assert not tracker.boxes[0].running
    assert events == [BoxEvent.CANCELLED]


@pytest.mark.parametrize("slow_event", [BoxEvent.STARTED, BoxEvent.ENDED])
async def test_cancel_during_a_hook_still_ends_the_box_once_for_every_hook(slow_event: BoxEvent) -> None:
    """Give every hook that saw STARTED exactly one ENDED or CANCELLED when a cancel hits an emit.

    Args:
        slow_event: The event on which a hook blocks until the cancel.
    """
    tracker = _Tracker()
    before = LiveCounts()
    after = LiveCounts()
    seen: dict[str, list[BoxEvent]] = {"before": [], "after": []}
    blocked = asyncio.Event()

    def recorder(name: str) -> object:
        """Build a hook that records the events it receives.

        Args:
            name: The key of the record.

        Returns:
            The hook.
        """

        async def record(info: BoxEventInfo) -> None:
            """Record one event.

            Args:
                info: The box event.
            """
            seen[name].append(info.event)

        return record

    async def slow(info: BoxEventInfo) -> None:
        """Block on the slow event until the run is cancelled.

        Args:
            info: The box event.
        """
        if info.event == slow_event:
            blocked.set()
            await asyncio.Event().wait()

    hooks = BoxHooks()
    for hook in (before, recorder("before"), slow, recorder("after"), after):
        hooks.add(hook)  # type: ignore[arg-type]
    pool = SandboxPool({"fake": 1}, hooks=hooks)
    task = asyncio.create_task(pool.run(_numbers(1), _work(tracker), _Outcomes()))
    async with asyncio.timeout(DEADLINE):
        await blocked.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        async with asyncio.timeout(DEADLINE):
            await task
    assert before.active == 0
    assert after.active == 0
    for events in seen.values():
        assert events.count(BoxEvent.STARTED) == 1
        assert sum(event in (BoxEvent.ENDED, BoxEvent.CANCELLED) for event in events) == 1
        assert events[-1] in (BoxEvent.ENDED, BoxEvent.CANCELLED)
    assert tracker.live == 0


async def test_stray_cancel_in_on_outcome_reaches_the_caller() -> None:
    """Raise a fatal error from run() when on_outcome raises a stray cancel, so one worker does not hang."""
    tracker = _Tracker()

    async def stray_cancel(_outcome: BoxOutcome[int, object]) -> None:
        """Await a cancelled inner task.

        Args:
            _outcome: The outcome of one box.
        """
        inner = asyncio.create_task(asyncio.sleep(10))
        await asyncio.sleep(0)
        inner.cancel()
        await inner

    pool = SandboxPool({"fake": 1})
    with pytest.raises(SandboxFatalError, match="on_outcome"):
        async with asyncio.timeout(DEADLINE):
            await pool.run(_numbers(3), _work(tracker), stray_cancel, workers=1)
    assert tracker.live == 0


async def test_on_outcome_error_reaches_the_caller_and_releases_permits() -> None:
    """Raise the callback's own error, stop the other boxes, then reuse every permit."""
    tracker = _Tracker()

    async def slow_after_first(env: SandboxEnvironment, candidate: int) -> int:
        """Return candidate 0 at once and wait forever for the others.

        Args:
            env: The box.
            candidate: The candidate.

        Returns:
            The candidate.
        """
        if candidate != 0:
            await asyncio.Event().wait()
        return candidate

    async def broken(_outcome: BoxOutcome[int, object]) -> None:
        """Fail on the first outcome.

        Args:
            _outcome: The outcome of one box.

        Raises:
            KeyError: Always.
        """
        raise KeyError("callback")

    pool = SandboxPool({"fake": 3}, phase_limits={Phase.RUN: 3})
    with pytest.raises(KeyError):
        async with asyncio.timeout(DEADLINE):
            await pool.run(_numbers(3), _work(tracker, run=slow_after_first), broken)
    assert tracker.live == 0

    barrier = asyncio.Barrier(3)

    async def meet(env: SandboxEnvironment, candidate: int) -> int:
        """Wait until three boxes run at once, then return the candidate.

        Args:
            env: The box.
            candidate: The candidate.

        Returns:
            The candidate.
        """
        await barrier.wait()
        return candidate

    outcomes = _Outcomes()
    async with asyncio.timeout(DEADLINE):
        await pool.run(_numbers(3), _work(tracker, run=meet), outcomes)
    assert [outcome.ok for outcome in outcomes.items] == [True, True, True]


def test_a_pool_refuses_a_second_event_loop() -> None:
    """Run on the first loop, then raise a clear error on another loop before any box starts."""
    tracker = _Tracker()
    pool = SandboxPool({"fake": 1})

    async def run_three() -> list[BoxOutcome[int, object]]:
        """Run three candidates with three workers on the current loop.

        Returns:
            The outcomes.
        """
        outcomes = _Outcomes()
        await pool.run(_numbers(3), _work(tracker), outcomes, workers=3)
        return outcomes.items

    assert all(outcome.ok for outcome in asyncio.run(run_three()))
    created = tracker.created
    with pytest.raises(RuntimeError, match="one event loop"):
        asyncio.run(run_three())
    assert tracker.created == created


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"provider_limits": {}}, ValueError),
        ({"provider_limits": {"fake": 0}}, ValueError),
        ({"provider_limits": {"fake": True}}, TypeError),
        ({"provider_limits": {"fake": 1}, "phase_limits": {Phase.START: 1}}, ValueError),
        ({"provider_limits": {"fake": 1}, "phase_limits": {Phase.RUN: 1.5}}, TypeError),
    ],
)
def test_invalid_limits_raise(kwargs: dict[str, object], error: type[Exception]) -> None:
    """Reject empty, non-positive and non-int limits, and a start phase limit.

    Args:
        kwargs: The pool arguments.
        error: The expected error class.
    """
    with pytest.raises(error):
        SandboxPool(**kwargs)  # type: ignore[arg-type]


async def test_invalid_queue_size_raises() -> None:
    """Reject a queue size below 1 before reading any candidate."""
    with pytest.raises(ValueError):
        await SandboxPool({"fake": 1}).run(_numbers(1), _work(_Tracker()), _Outcomes(), queue_size=0)


class _CloseFails:
    """Yield the given numbers, then raise from ``aclose`` with a stray cancel or an error."""

    def __init__(self, numbers: list[int], *, stray_cancel: bool) -> None:
        """Store the numbers and the kind of ``aclose`` failure.

        Args:
            numbers: The numbers to yield.
            stray_cancel: True for a stray cancel, False for a RuntimeError.
        """
        self.numbers = list(numbers)
        self.stray_cancel = stray_cancel

    def __aiter__(self) -> _CloseFails:
        """Return the iterator itself.

        Returns:
            The iterator.
        """
        return self

    async def __anext__(self) -> int:
        """Return the next number.

        Returns:
            The next number.

        Raises:
            StopAsyncIteration: No numbers are left.
        """
        if not self.numbers:
            raise StopAsyncIteration
        return self.numbers.pop(0)

    async def aclose(self) -> None:
        """Fail with a stray cancel or a RuntimeError.

        Raises:
            RuntimeError: When ``stray_cancel`` is False.
        """
        if self.stray_cancel:
            inner = asyncio.create_task(asyncio.sleep(10))
            await asyncio.sleep(0)
            inner.cancel()
            await inner
        raise RuntimeError("close failed")


@pytest.mark.parametrize(
    ("stray_cancel", "error", "match"),
    [(True, SandboxFatalError, "aclose"), (False, RuntimeError, "close failed")],
)
async def test_aclose_failure_lets_boxes_in_flight_finish_then_reaches_the_caller(
    stray_cancel: bool, error: type[Exception], match: str
) -> None:
    """Finish the boxes in flight when the iterator's aclose fails, then raise its error; never hang.

    Args:
        stray_cancel: True when aclose raises a stray cancel, False for a RuntimeError.
        error: The error that run() raises.
        match: A part of the error message.
    """
    tracker = _Tracker()
    release = asyncio.Event()

    async def slow(env: SandboxEnvironment, candidate: int) -> int:
        """Wait for the release, then return the candidate.

        Args:
            env: The box.
            candidate: The candidate.

        Returns:
            The candidate.
        """
        await release.wait()
        return candidate

    outcomes = _Outcomes()
    pool = SandboxPool({"fake": 2})
    run = asyncio.create_task(
        pool.run(_CloseFails([0, 1], stray_cancel=stray_cancel), _work(tracker, run=slow), outcomes)
    )
    await _settle()
    release.set()
    with pytest.raises(error, match=match):
        async with asyncio.timeout(DEADLINE):
            await run
    assert sorted(outcome.result for outcome in outcomes.items) == [0, 1]
    assert tracker.live == 0


async def test_stray_cancel_in_stop_keeps_the_box_outcome(caplog: pytest.LogCaptureFixture) -> None:
    """Log a stop that raises a stray cancel and keep the box's own result."""
    tracker = _Tracker()

    class _StrayCancelStop(_TrackedSandbox):
        """Act as a tracked box whose stop raises a stray cancel."""

        async def stop(self) -> None:
            """Stop the box, then await a cancelled inner task."""
            await super().stop()
            inner = asyncio.create_task(asyncio.sleep(10))
            await asyncio.sleep(0)
            inner.cancel()
            await inner

    work = BoxWork(create=lambda _candidate: _StrayCancelStop(tracker), run=_work(tracker).run)
    outcomes = _Outcomes()
    with caplog.at_level(logging.ERROR, logger="core.sandbox.pool"):
        async with asyncio.timeout(DEADLINE):
            await SandboxPool({"fake": 1}).run(_numbers(1), work, outcomes)
    assert outcomes.items[0].ok
    assert outcomes.items[0].result == 0
    assert any("did not stop" in record.getMessage() for record in caplog.records)


async def test_hung_stop_ends_after_the_stop_timeout(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """End the wait for a hung stop after the limit: keep the outcome, and let a cancel go through."""
    monkeypatch.setattr(pool_module, "STOP_TIMEOUT_SECONDS", 0.05)
    tracker = _Tracker()
    stop_cancelled = asyncio.Event()

    class _HungStop(_TrackedSandbox):
        """Act as a tracked box whose stop never ends."""

        async def stop(self) -> None:
            """Wait forever, and note when the pool cancels the stop."""
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                stop_cancelled.set()
                raise

    work = BoxWork(create=lambda _candidate: _HungStop(tracker), run=_work(tracker).run)
    outcomes = _Outcomes()
    with caplog.at_level(logging.ERROR, logger="core.sandbox.pool"):
        async with asyncio.timeout(DEADLINE):
            await SandboxPool({"fake": 1}).run(_numbers(1), work, outcomes)
    assert outcomes.items[0].result == 0
    assert any("did not stop within" in record.getMessage() for record in caplog.records)
    await _settle()
    assert stop_cancelled.is_set()

    run = asyncio.create_task(SandboxPool({"fake": 1}).run(_numbers(1), work, _Outcomes()))
    await _settle()
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        async with asyncio.timeout(DEADLINE):
            await run


async def test_errors_reach_the_caller_with_their_cause() -> None:
    """Keep the cause of a callback error and of the pool's own fatal error."""

    async def chained(_outcome: BoxOutcome[int, object]) -> None:
        """Raise a ValueError caused by a KeyError.

        Args:
            _outcome: The outcome of one box.

        Raises:
            ValueError: Always, from a KeyError.
        """
        try:
            raise KeyError("root")
        except KeyError as root:
            raise ValueError("callback failed") from root

    with pytest.raises(ValueError, match="callback failed") as caught:
        async with asyncio.timeout(DEADLINE):
            await SandboxPool({"fake": 1}).run(_numbers(1), _work(_Tracker()), chained)
    assert isinstance(caught.value.__cause__, KeyError)

    async def stray_cancel() -> AsyncIterator[int]:
        """Yield one number, then await a cancelled inner task.

        Yields:
            The number 0.
        """
        yield 0
        inner = asyncio.create_task(asyncio.sleep(10))
        await asyncio.sleep(0)
        inner.cancel()
        await inner

    with pytest.raises(SandboxFatalError, match="candidate iterator") as caught_fatal:
        async with asyncio.timeout(DEADLINE):
            await SandboxPool({"fake": 2}).run(stray_cancel(), _work(_Tracker()), _Outcomes())
    assert isinstance(caught_fatal.value.__cause__, asyncio.CancelledError)


async def test_cancel_during_anext_with_a_failing_aclose_stays_a_cancel(caplog: pytest.LogCaptureFixture) -> None:
    """Keep the caller's cancel when aclose fails during it, and log the aclose error."""
    waiting = asyncio.Event()

    class _WaitThenCloseFails:
        """Wait forever for the next candidate, and fail in ``aclose``."""

        def __aiter__(self) -> _WaitThenCloseFails:
            """Return the iterator itself.

            Returns:
                The iterator.
            """
            return self

        async def __anext__(self) -> int:
            """Note the wait, then wait forever.

            Returns:
                Never returns.
            """
            waiting.set()
            await asyncio.Event().wait()
            return 0

        async def aclose(self) -> None:
            """Fail with a RuntimeError.

            Raises:
                RuntimeError: Always.
            """
            raise RuntimeError("close failed")

    run = asyncio.create_task(SandboxPool({"fake": 1}).run(_WaitThenCloseFails(), _work(_Tracker()), _Outcomes()))
    async with asyncio.timeout(DEADLINE):
        await waiting.wait()
    with caplog.at_level(logging.ERROR, logger="core.sandbox.pool"):
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            async with asyncio.timeout(DEADLINE):
                await run
    assert run.cancelled()
    assert any("aclose failed after an error or a cancel" in record.getMessage() for record in caplog.records)


async def test_iterator_error_with_a_failing_aclose_keeps_the_iterator_error(caplog: pytest.LogCaptureFixture) -> None:
    """Raise the iterator's own error when aclose also fails, and log the aclose error."""

    class _FailThenCloseFails:
        """Fail on the first candidate, and fail in ``aclose``."""

        def __aiter__(self) -> _FailThenCloseFails:
            """Return the iterator itself.

            Returns:
                The iterator.
            """
            return self

        async def __anext__(self) -> int:
            """Fail with a ValueError.

            Raises:
                ValueError: Always.
            """
            raise ValueError("iterator failed")

        async def aclose(self) -> None:
            """Fail with a RuntimeError.

            Raises:
                RuntimeError: Always.
            """
            raise RuntimeError("close failed")

    with caplog.at_level(logging.ERROR, logger="core.sandbox.pool"), pytest.raises(ValueError, match="iterator failed"):
        async with asyncio.timeout(DEADLINE):
            await SandboxPool({"fake": 1}).run(_FailThenCloseFails(), _work(_Tracker()), _Outcomes())
    assert any("aclose failed after an error or a cancel" in record.getMessage() for record in caplog.records)

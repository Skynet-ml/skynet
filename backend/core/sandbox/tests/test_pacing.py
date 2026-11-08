"""Tests for the sandbox token buckets and the capacity wait."""

from __future__ import annotations

import asyncio
import gc
import random
import weakref
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import pytest

from core.sandbox.errors import ErrorGroup, classify
from core.sandbox.pacing import (
    CAPACITY_WAIT_MAX,
    ProviderAtCapacityError,
    ProviderPacing,
    TokenBucket,
    capacity_delay,
    capacity_wait,
    create_with_capacity,
)
from core.sandbox.retry import DEFAULT_RETRY_POLICY


class FakeClock:
    """Move time forward only when a caller sleeps, so tests never really sleep."""

    def __init__(self) -> None:
        """Start the clock at zero with no sleeps."""
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        """Return the fake time, in seconds."""
        return self.now

    async def sleep(self, seconds: float) -> None:
        """Record the sleep, move time forward and yield to the loop.

        Args:
            seconds: The time to sleep, in seconds.
        """
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


class BlockedClock(FakeClock):
    """Block every sleep until a cancel, to test the cancel path."""

    def __init__(self) -> None:
        """Start the clock with an event that the first sleep sets."""
        super().__init__()
        self.sleeping = asyncio.Event()

    async def sleep(self, seconds: float) -> None:
        """Record the sleep and wait for ever.

        Args:
            seconds: The time to sleep, in seconds.
        """
        self.sleeps.append(seconds)
        self.sleeping.set()
        await asyncio.Event().wait()


class Recorder:
    """Record the order of capacity checks, reservations and creates."""

    def __init__(self, capacity: list[bool], clock: FakeClock) -> None:
        """Store the answers that the capacity check gives, in order.

        Args:
            capacity: True for each check that reports "at capacity".
            clock: The clock whose sleeps go into the event log.
        """
        self.capacity = list(capacity)
        self.clock = clock
        self.events: list[str] = []
        self.reserved = 0

    async def at_capacity(self) -> bool:
        """Log a check and give the next answer.

        Returns:
            True while the fake provider is at capacity.
        """
        self.events.append("check")
        return self.capacity.pop(0) if self.capacity else False

    def reserve(self) -> AbstractAsyncContextManager[str]:
        """Make a reservation context that logs its hold and release.

        Returns:
            An async context manager that yields a reservation id.
        """

        @asynccontextmanager
        async def hold() -> AsyncGenerator[str, None]:
            """Log the hold, yield the id, and log the release.

            Yields:
                The reservation id.
            """
            self.reserved += 1
            self.events.append("reserve")
            try:
                yield f"res-{self.reserved}"
            finally:
                self.reserved -= 1
                self.events.append("release")

        return hold()


def _max_burst(times: list[float], rate: float) -> float:
    """Find the most calls in any time window, less the tokens that window earns.

    Args:
        times: The time of each call, in order.
        rate: The bucket rate.

    Returns:
        The largest ``count - rate * (t_j - t_i)`` over all windows ``[t_i, t_j]``.
    """
    worst = 0.0
    lowest = float("inf")
    for index, moment in enumerate(times):
        lowest = min(lowest, index - rate * moment)
        worst = max(worst, index - rate * moment - lowest + 1)
    return worst


async def test_bucket_keeps_rate_under_1000_parallel_callers() -> None:
    """Allow only the burst plus the earned tokens in any window, for 1000 callers."""
    clock = FakeClock()
    bucket = TokenBucket(rate=50, burst=10, clock=clock)
    times: list[float] = []

    async def caller() -> None:
        """Take one token and record the time."""
        await bucket.acquire()
        times.append(clock.monotonic())

    await asyncio.gather(*(caller() for _ in range(1000)))

    assert len(times) == 1000
    assert times == sorted(times)
    assert _max_burst(times, rate=50) <= 10 + 1e-6
    assert times[-1] == pytest.approx((1000 - 10) / 50)


async def test_bucket_refills_after_idle_time_up_to_burst() -> None:
    """Refill while idle, but never past the burst."""
    clock = FakeClock()
    bucket = TokenBucket(rate=2, burst=3, clock=clock)
    for _ in range(3):
        await bucket.acquire()
    assert clock.sleeps == []
    clock.now += 100
    for _ in range(3):
        await bucket.acquire()
    assert clock.sleeps == []
    await bucket.acquire()
    assert clock.sleeps == [pytest.approx(0.5)]


async def test_bucket_cancel_while_waiting_frees_the_queue() -> None:
    """Exit at once on cancel, and let the next caller take the lock."""
    clock = BlockedClock()
    bucket = TokenBucket(rate=1, burst=1, clock=clock)
    await bucket.acquire()
    waiter = asyncio.create_task(bucket.acquire())
    await clock.sleeping.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not bucket._lock().locked()


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"rate": 0, "burst": 1}, ValueError),
        ({"rate": 1, "burst": 0.5}, ValueError),
        ({"rate": float("inf"), "burst": 1}, ValueError),
        ({"rate": True, "burst": 1}, TypeError),
        ({"rate": 1, "burst": "2"}, TypeError),
    ],
)
def test_bucket_rejects_bad_settings(kwargs: dict[str, object], error: type[Exception]) -> None:
    """Reject a rate or burst that is not a usable number."""
    with pytest.raises(error):
        TokenBucket(**kwargs)  # type: ignore[arg-type]


async def test_bucket_rejects_more_tokens_than_burst() -> None:
    """Refuse a take that the bucket can never hold."""
    bucket = TokenBucket(rate=1, burst=2, clock=FakeClock())
    with pytest.raises(ValueError):
        await bucket.acquire(3)


def test_bucket_works_from_a_second_event_loop() -> None:
    """Share one bucket across ``asyncio.run`` calls with waiting callers."""
    clock = FakeClock()
    bucket = TokenBucket(rate=10, burst=1, clock=clock)

    async def burst() -> None:
        """Make five callers wait for the one-token bucket."""
        await asyncio.gather(*(bucket.acquire() for _ in range(5)))

    asyncio.run(burst())
    asyncio.run(burst())
    assert len(clock.sleeps) == 9
    assert bucket._tokens < 1


def test_bucket_keeps_no_closed_event_loop() -> None:
    """Free each closed loop after ``asyncio.run`` calls with waiting callers."""
    bucket = TokenBucket(rate=10, burst=1, clock=FakeClock())
    loops: list[weakref.ref[asyncio.AbstractEventLoop]] = []

    async def burst() -> None:
        """Make three callers wait for the one-token bucket, and record the loop."""
        loops.append(weakref.ref(asyncio.get_running_loop()))
        await asyncio.gather(*(bucket.acquire() for _ in range(3)))

    for _ in range(5):
        asyncio.run(burst())
    gc.collect()
    assert [ref() for ref in loops[:-1]] == [None] * 4

    bucket._lock_obj = bucket._lock_loop = None
    gc.collect()
    assert loops[-1]() is None


def test_provider_pacing_has_separate_buckets() -> None:
    """Give each provider one read bucket and one create bucket."""
    pacing = ProviderPacing.build(read_rate=100, read_burst=20, create_rate=2, create_burst=4)
    assert pacing.reads is not pacing.creates
    assert (pacing.reads.rate, pacing.reads.burst) == (100, 20)
    assert (pacing.creates.rate, pacing.creates.burst) == (2, 4)


def test_capacity_delay_is_jittered_and_capped_at_30_seconds() -> None:
    """Pick each delay inside a doubling window that stops at 30 s."""
    rng = random.Random(7)
    for wait_number in range(200):
        delay = capacity_delay(wait_number, rng=rng)
        assert 0 <= delay <= min(CAPACITY_WAIT_MAX, 2.0**wait_number)
    assert CAPACITY_WAIT_MAX == 30.0
    assert capacity_delay(10_000, rng=random.Random(1)) <= 30.0


_BAD_WINDOWS = [
    {"wait_max": 0},
    {"wait_max": -1},
    {"wait_max": float("nan")},
    {"wait_max": float("inf")},
    {"multiplier": 0},
    {"multiplier": -0.5},
    {"multiplier": float("nan")},
    {"multiplier": float("inf")},
]


@pytest.mark.parametrize("kwargs", _BAD_WINDOWS)
def test_capacity_delay_rejects_a_window_with_no_delay(kwargs: dict[str, float]) -> None:
    """Reject a ``wait_max`` or ``multiplier`` that gives a zero or NaN delay."""
    with pytest.raises(ValueError):
        capacity_delay(3, **kwargs)


@pytest.mark.parametrize("kwargs", _BAD_WINDOWS)
async def test_capacity_wait_rejects_a_window_before_polling(kwargs: dict[str, float]) -> None:
    """Refuse a hot polling loop before the first capacity check."""
    clock = FakeClock()
    recorder = Recorder([True] * 5, clock)
    with pytest.raises(ValueError):
        await capacity_wait(recorder.at_capacity, clock=clock, **kwargs)
    assert recorder.events == []


@pytest.mark.parametrize("first_wait", [-1, -100, True, 1.5])
async def test_capacity_wait_rejects_a_bad_first_wait_before_polling(first_wait: object) -> None:
    """Refuse a negative or non-int ``first_wait``, which gives a zero delay."""
    clock = FakeClock()
    recorder = Recorder([True] * 5, clock)
    with pytest.raises(ValueError):
        await capacity_wait(recorder.at_capacity, first_wait=first_wait, clock=clock)  # type: ignore[arg-type]
    assert recorder.events == []


@pytest.mark.parametrize("kwargs", _BAD_WINDOWS)
async def test_create_with_capacity_rejects_a_window_before_reserving(
    kwargs: dict[str, float],
) -> None:
    """Refuse a bad window before any check, reservation or create."""
    clock = FakeClock()
    recorder = Recorder([], clock)

    async def create(reservation: str) -> str:
        """Fail if the create runs.

        Args:
            reservation: The reservation id.

        Raises:
            AssertionError: Always, because the create must not run.
        """
        raise AssertionError("create must not run")

    with pytest.raises(ValueError):
        await create_with_capacity(create, recorder.reserve, at_capacity=recorder.at_capacity, clock=clock, **kwargs)
    assert recorder.events == []
    assert recorder.reserved == 0


async def test_capacity_wait_has_no_attempt_limit() -> None:
    """Keep waiting past any retry count while the provider is at capacity."""
    clock = FakeClock()
    recorder = Recorder([True] * 500, clock)
    waits = await capacity_wait(recorder.at_capacity, clock=clock, rng=random.Random(3))
    assert waits == 500
    assert len(clock.sleeps) == 500
    assert waits > DEFAULT_RETRY_POLICY.max_attempts
    assert all(0 <= delay <= CAPACITY_WAIT_MAX for delay in clock.sleeps)


async def test_capacity_wait_comes_before_the_budget_reservation() -> None:
    """Check capacity with no hold, then reserve, then create, then release."""
    clock = FakeClock()
    recorder = Recorder([True, True], clock)
    seen: list[str] = []

    async def create(reservation: str) -> str:
        """Log the create and check that the hold is live.

        Args:
            reservation: The reservation id.

        Returns:
            The sandbox id.
        """
        assert recorder.reserved == 1
        recorder.events.append("create")
        seen.append(reservation)
        return "sbx-1"

    async def at_capacity() -> bool:
        """Check that no hold is live, then ask the recorder.

        Returns:
            The recorder's answer.
        """
        assert recorder.reserved == 0
        return await recorder.at_capacity()

    result = await create_with_capacity(
        create, recorder.reserve, at_capacity=at_capacity, clock=clock, rng=random.Random(0)
    )

    assert result == "sbx-1"
    assert recorder.events == ["check", "check", "check", "reserve", "create", "release"]
    assert len(clock.sleeps) == 2
    assert seen == ["res-1"]


async def test_create_pacing_holds_no_reservation() -> None:
    """Take the create token before the budget hold."""
    clock = FakeClock()
    recorder = Recorder([], clock)
    bucket = TokenBucket(rate=1, burst=1, clock=clock)
    await bucket.acquire()
    held_during_sleep: list[int] = []
    original_sleep = clock.sleep

    async def sleep(seconds: float) -> None:
        """Record the live holds, then sleep.

        Args:
            seconds: The time to sleep, in seconds.
        """
        held_during_sleep.append(recorder.reserved)
        await original_sleep(seconds)

    clock.sleep = sleep  # type: ignore[method-assign]

    async def create(reservation: str) -> str:
        """Return a sandbox id.

        Args:
            reservation: The reservation id.

        Returns:
            The sandbox id.
        """
        return "sbx-1"

    await create_with_capacity(create, recorder.reserve, bucket=bucket, clock=clock)
    assert held_during_sleep == [0]
    assert recorder.events == ["reserve", "release"]


async def test_at_capacity_create_releases_and_waits_again() -> None:
    """Release the hold on an at-capacity create, wait, and create again."""
    clock = FakeClock()
    recorder = Recorder([], clock)
    calls: list[str] = []

    async def create(reservation: str) -> str:
        """Fail twice with "at capacity", then succeed.

        Args:
            reservation: The reservation id.

        Returns:
            The sandbox id.

        Raises:
            ProviderAtCapacityError: On the first two calls.
        """
        calls.append(reservation)
        recorder.events.append("create")
        if len(calls) <= 2:
            raise ProviderAtCapacityError("maximum number of concurrent sandboxes")
        return "sbx-1"

    result = await create_with_capacity(
        create, recorder.reserve, at_capacity=recorder.at_capacity, clock=clock, rng=random.Random(0)
    )

    assert result == "sbx-1"
    assert recorder.events == ["check", "reserve", "create", "release"] * 3
    assert len(clock.sleeps) == 2
    assert recorder.reserved == 0


async def test_other_create_errors_pass_through() -> None:
    """Raise a non-capacity create error once, with no wait and no hold left."""
    clock = FakeClock()
    recorder = Recorder([], clock)

    async def create(reservation: str) -> str:
        """Fail with a transient error.

        Args:
            reservation: The reservation id.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("502 bad gateway")

    with pytest.raises(RuntimeError):
        await create_with_capacity(create, recorder.reserve, clock=clock)
    assert clock.sleeps == []
    assert recorder.events == ["reserve", "release"]


async def test_cancel_during_capacity_wait_exits_at_once() -> None:
    """Stop the capacity wait on cancel, with no reservation and no create."""
    clock = BlockedClock()
    recorder = Recorder([True] * 10, clock)
    created: list[str] = []

    async def create(reservation: str) -> str:
        """Record a create that must not happen.

        Args:
            reservation: The reservation id.

        Returns:
            The sandbox id.
        """
        created.append(reservation)
        return "sbx-1"

    task = asyncio.create_task(
        create_with_capacity(create, recorder.reserve, at_capacity=recorder.at_capacity, clock=clock)
    )
    await clock.sleeping.wait()
    task.cancel()
    async with asyncio.timeout(1):
        with pytest.raises(asyncio.CancelledError):
            await task
    assert recorder.events == ["check"]
    assert created == []
    assert recorder.reserved == 0


def test_at_capacity_error_is_transient() -> None:
    """Put the at-capacity error in the transient group."""
    assert classify(ProviderAtCapacityError("full")) == ErrorGroup.TRANSIENT

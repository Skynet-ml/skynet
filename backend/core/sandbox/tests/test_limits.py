"""Tests for the Redis slot limiter and the pool's shared box limit.

The tests use fakeredis, an in-memory Redis double from the dev extra. Two
clients on one ``FakeServer`` act as two pods on one Redis server.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import fakeredis
import pytest

from core.sandbox import ResourceRequest, SandboxSpec
from core.sandbox.limits import RedisSlotLimiter
from core.sandbox.pool import BoxOutcome, BoxWork, SandboxPool
from core.sandbox.tests.fake_provider import InMemorySandbox

KEY = "sandbox-slots:fake"

# A deadline for the waits on the pool, so a bug fails the test fast.
DEADLINE = 5.0


class _Live:
    """Count the boxes that live at once across every pool."""

    def __init__(self) -> None:
        """Start with no boxes."""
        self.live = 0
        self.peak = 0


class _Box(InMemorySandbox):
    """Act as the in-memory sandbox and report its start and stop to a counter."""

    def __init__(self, live: _Live, *, fail_start: bool = False) -> None:
        """Create a stopped box.

        Args:
            live: The counter of live boxes.
            fail_start: Whether ``start`` raises.
        """
        super().__init__(SandboxSpec(lifetime_seconds=60, resources=ResourceRequest(cpus=1)))
        self.live_count = live
        self.fail_start = fail_start

    async def start(self) -> None:
        """Start the box and count it as live.

        Raises:
            RuntimeError: ``fail_start`` is set.
        """
        if self.fail_start:
            raise RuntimeError("fake start failure")
        await super().start()
        self.live_count.live += 1
        self.live_count.peak = max(self.live_count.peak, self.live_count.live)

    async def stop(self) -> None:
        """Stop the box and stop counting it, once."""
        was_running = self.running
        await super().stop()
        if was_running:
            self.live_count.live -= 1


async def _candidates(count: int) -> AsyncIterator[int]:
    """Yield the candidates 0 to ``count - 1``.

    Args:
        count: The number of candidates.

    Yields:
        Each candidate.
    """
    for candidate in range(count):
        yield candidate


def _limiter(server: fakeredis.FakeServer, limit: int, **kwargs: float) -> RedisSlotLimiter:
    """Make a limiter with its own client on the shared fake server.

    Args:
        server: The fake Redis server.
        limit: The shared limit.
        **kwargs: The lease and poll times.

    Returns:
        The limiter.
    """
    return RedisSlotLimiter(fakeredis.FakeAsyncRedis(server=server), KEY, limit, **kwargs)


async def _holders(server: fakeredis.FakeServer) -> int:
    """Return the number of holders in the shared set.

    Args:
        server: The fake Redis server.

    Returns:
        The number of members, live or expired.
    """
    return await fakeredis.FakeAsyncRedis(server=server).zcard(KEY)


async def test_two_pools_on_one_redis_never_go_above_the_limit() -> None:
    """Two pools with a local limit of 4 each share a limit of 3 and never pass it."""
    server = fakeredis.FakeServer()
    live = _Live()
    outcomes: list[BoxOutcome[int, int]] = []

    async def run(env: object, candidate: int) -> int:
        """Hold the box for a moment so boxes overlap.

        Args:
            env: The box.
            candidate: The candidate.

        Returns:
            The candidate.
        """
        await asyncio.sleep(0.02)
        return candidate

    async def collect(outcome: BoxOutcome[int, int]) -> None:
        """Keep an outcome.

        Args:
            outcome: The outcome.
        """
        outcomes.append(outcome)

    work: BoxWork[int, int] = BoxWork(create=lambda _: _Box(live), run=run)
    pools = [
        SandboxPool({"fake": 4}, shared_limits={"fake": _limiter(server, 3, poll_seconds=0.005)}) for _ in range(2)
    ]
    async with asyncio.timeout(DEADLINE):
        await asyncio.gather(*(pool.run(_candidates(15), work, collect) for pool in pools))
    assert len(outcomes) == 30
    assert all(outcome.ok for outcome in outcomes)
    assert live.peak == 3
    assert await _holders(server) == 0


async def test_a_dead_holder_slot_comes_back_after_its_lease() -> None:
    """A slot that nobody refreshes or releases is free again after its lease ends."""
    server = fakeredis.FakeServer()
    dead_pod = _limiter(server, 1, lease_seconds=0.2)
    live_pod = _limiter(server, 1, lease_seconds=0.2)
    assert await dead_pod.try_acquire("dead")
    assert not await live_pod.try_acquire("other")
    await asyncio.sleep(0.3)
    assert await live_pod.try_acquire("other")
    assert await _holders(server) == 1


async def test_a_held_slot_keeps_its_lease_while_the_body_runs() -> None:
    """The refresh keeps a slot alive past its first lease, and the exit gives it back."""
    server = fakeredis.FakeServer()
    holder = _limiter(server, 1, lease_seconds=0.15)
    other = _limiter(server, 1, lease_seconds=0.15)
    async with holder.hold("box"):
        await asyncio.sleep(0.4)
        assert not await other.try_acquire("other")
    assert await _holders(server) == 0
    assert await other.try_acquire("other")


async def test_the_pool_gives_back_the_slot_when_a_box_fails_to_start() -> None:
    """A box whose start fails frees its shared slot, so the next box can run."""
    server = fakeredis.FakeServer()
    live = _Live()
    boxes = iter([_Box(live, fail_start=True), _Box(live, fail_start=True), _Box(live)])
    outcomes: list[BoxOutcome[int, int]] = []

    async def run(env: object, candidate: int) -> int:
        """Return the candidate.

        Args:
            env: The box.
            candidate: The candidate.

        Returns:
            The candidate.
        """
        return candidate

    async def collect(outcome: BoxOutcome[int, int]) -> None:
        """Keep an outcome.

        Args:
            outcome: The outcome.
        """
        outcomes.append(outcome)

    pool = SandboxPool({"fake": 1}, shared_limits={"fake": _limiter(server, 1, poll_seconds=0.005)})
    work: BoxWork[int, int] = BoxWork(create=lambda _: next(boxes), run=run)
    async with asyncio.timeout(DEADLINE):
        await pool.run(_candidates(3), work, collect)
    assert [type(outcome.error) for outcome in outcomes] == [RuntimeError, RuntimeError, type(None)]
    assert await _holders(server) == 0


async def test_a_cancel_gives_back_the_slots_of_running_and_waiting_boxes() -> None:
    """A cancel of the run frees the slots of the boxes in flight and stops the waiters."""
    server = fakeredis.FakeServer()
    live = _Live()
    two_running = asyncio.Event()
    running = 0

    async def run(env: object, candidate: int) -> int:
        """Block until the run is cancelled.

        Args:
            env: The box.
            candidate: The candidate.

        Returns:
            Never.
        """
        nonlocal running
        running += 1
        if running == 2:
            two_running.set()
        await asyncio.Event().wait()
        return candidate

    async def collect(outcome: BoxOutcome[int, int]) -> None:
        """Ignore an outcome.

        Args:
            outcome: The outcome.
        """

    pool = SandboxPool({"fake": 4}, shared_limits={"fake": _limiter(server, 2, poll_seconds=0.005)})
    work: BoxWork[int, int] = BoxWork(create=lambda _: _Box(live), run=run)
    task = asyncio.create_task(pool.run(_candidates(4), work, collect))
    async with asyncio.timeout(DEADLINE):
        await two_running.wait()
    await asyncio.sleep(0.05)
    assert live.live == 2
    assert await _holders(server) == 2
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        async with asyncio.timeout(DEADLINE):
            await task
    assert live.live == 0
    assert await _holders(server) == 0


async def test_a_cancel_during_the_slot_wait_leaves_no_holder() -> None:
    """A hold that is cancelled while it waits for a slot leaves no member behind."""
    server = fakeredis.FakeServer()
    first = _limiter(server, 1, poll_seconds=0.005)
    second = _limiter(server, 1, poll_seconds=0.005)
    assert await first.try_acquire("first")

    async def wait_for_slot() -> None:
        """Wait for a slot that never frees."""
        async with second.hold("second"):
            pytest.fail("the slot must not free")

    task = asyncio.create_task(wait_for_slot())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _holders(server) == 1


def test_a_shared_limit_needs_a_local_limit() -> None:
    """A shared limiter for a provider with no local limit is an error."""
    limiter = _limiter(fakeredis.FakeServer(), 1)
    with pytest.raises(ValueError, match="no local limit"):
        SandboxPool({"fake": 1}, shared_limits={"vercel": limiter})


@pytest.mark.parametrize("limit", [0, True, 1.5])
def test_the_limiter_checks_its_limit(limit: object) -> None:
    """A limit that is not a positive int is an error.

    Args:
        limit: The bad limit.
    """
    with pytest.raises((TypeError, ValueError)):
        RedisSlotLimiter(fakeredis.FakeAsyncRedis(), KEY, limit)  # type: ignore[arg-type]

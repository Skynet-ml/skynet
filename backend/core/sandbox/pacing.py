"""Provider request pacing and the wait for provider capacity.

Adapted from Harbor (harbor-framework/harbor), Apache-2.0: the read pacing
follows ``_ReadPacer`` in Harbor's E2B environment, and the capacity rule
follows ``_is_team_at_capacity``: wait with no attempt limit, with a jittered
exponential wait of at most 30 s. Changes: one async token bucket class for
reads and for creates, an injectable clock so that tests do not sleep, and
the capacity wait runs before the budget reservation, so a box that waits
for capacity holds no budget (SKY-414). A capacity wait is not a retry: it
does not count against a retry policy and it makes no new attempt key
(SKY-406 D3).
"""

from __future__ import annotations

import asyncio
import math
import random
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol, TypeVar
from weakref import WeakKeyDictionary

from .errors import SandboxTransientError

T = TypeVar("T")
R = TypeVar("R")

CAPACITY_WAIT_MAX = 30.0

# Float sums of refill time can leave a bucket a tiny fraction short of a full
# token. Without this slack the loop sleeps for a time that does not move the
# clock, and never ends.
_TOKEN_EPSILON = 1e-9

_RNG = random.Random()


class Clock(Protocol):
    """Give the current monotonic time and an async sleep."""

    def monotonic(self) -> float:
        """Return the current monotonic time, in seconds."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Sleep for a number of seconds.

        Args:
            seconds: The time to sleep, in seconds.
        """
        ...


class SystemClock:
    """Use ``time.monotonic`` and ``asyncio.sleep``."""

    def monotonic(self) -> float:
        """Return the current monotonic time, in seconds."""
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        """Sleep for a number of seconds.

        Args:
            seconds: The time to sleep, in seconds.
        """
        await asyncio.sleep(seconds)


SYSTEM_CLOCK = SystemClock()


class ProviderAtCapacityError(SandboxTransientError):
    """Report that the provider has no capacity for a new sandbox now."""


def _check_positive(name: str, value: object) -> float:
    """Return a finite number above zero or raise.

    Args:
        name: The field name for the error message.
        value: The value to check.

    Returns:
        The value as a float.

    Raises:
        TypeError: The value is a bool or not a number.
        ValueError: The value is not finite or <= 0.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{name} must be a number")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite number > 0")
    return float(value)


class TokenBucket:
    """Limit calls to ``rate`` per second, with bursts of up to ``burst`` calls.

    Callers take tokens in arrival order. The waiting caller holds the lock
    while it sleeps, so the callers behind it wait in a queue and do not all
    wake at the same time.

    One bucket can serve more than one event loop, for example a process-wide
    bucket that sync code uses through ``asyncio.run``. All loops share the
    token count. Each loop gets its own lock, because an ``asyncio.Lock`` is
    bound to one loop, so the arrival-order queue holds only within a loop.
    The bucket is not thread-safe: do not use it from loops that run at the
    same time in different threads.
    """

    def __init__(self, rate: float, burst: float, clock: Clock | None = None) -> None:
        """Make a full bucket.

        Args:
            rate: The tokens added each second.
            burst: The most tokens the bucket holds. It must be at least 1.
            clock: The clock. The default is the system clock.

        Raises:
            TypeError: ``rate`` or ``burst`` is not a number.
            ValueError: ``rate`` is not above zero, or ``burst`` is below 1.
        """
        self.rate = _check_positive("rate", rate)
        self.burst = _check_positive("burst", burst)
        if self.burst < 1:
            raise ValueError("burst must be >= 1")
        self._clock = clock or SYSTEM_CLOCK
        self._tokens = self.burst
        self._updated = self._clock.monotonic()
        self._locks: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = WeakKeyDictionary()

    def _lock(self) -> asyncio.Lock:
        """Return the lock of the running event loop, and make it on first use.

        Returns:
            The lock that queues the callers on the running loop.
        """
        loop = asyncio.get_running_loop()
        lock = self._locks.get(loop)
        if lock is None:
            lock = self._locks[loop] = asyncio.Lock()
        return lock

    def _refill(self) -> None:
        """Add the tokens that the time since the last update earned."""
        now = self._clock.monotonic()
        elapsed = max(0.0, now - self._updated)
        self._tokens = min(self.burst, self._tokens + elapsed * self.rate)
        self._updated = now

    async def acquire(self, tokens: float = 1.0) -> None:
        """Wait until the bucket has the tokens, then take them.

        Args:
            tokens: The tokens to take. It must be above zero and at most ``burst``.

        Raises:
            ValueError: ``tokens`` is out of range.
        """
        needed = _check_positive("tokens", tokens)
        if needed > self.burst:
            raise ValueError("tokens must be <= burst")
        async with self._lock():
            self._refill()
            while self._tokens + _TOKEN_EPSILON < needed:
                await self._clock.sleep((needed - self._tokens) / self.rate)
                self._refill()
            self._tokens = max(0.0, self._tokens - needed)


@dataclass(frozen=True)
class ProviderPacing:
    """Hold the read bucket and the create bucket of one provider."""

    reads: TokenBucket
    creates: TokenBucket

    @classmethod
    def build(
        cls,
        *,
        read_rate: float,
        read_burst: float,
        create_rate: float,
        create_burst: float,
        clock: Clock | None = None,
    ) -> ProviderPacing:
        """Make the two buckets of one provider.

        Args:
            read_rate: The reads each second.
            read_burst: The largest burst of reads.
            create_rate: The creates each second.
            create_burst: The largest burst of creates.
            clock: The clock that both buckets use.

        Returns:
            The pacing of the provider.
        """
        return cls(
            reads=TokenBucket(read_rate, read_burst, clock),
            creates=TokenBucket(create_rate, create_burst, clock),
        )


def _check_capacity_window(wait_max: float, multiplier: float) -> None:
    """Reject a capacity window that gives no delay.

    The capacity wait has no attempt limit, so a zero, negative or NaN delay
    makes it poll the provider in a hot loop.

    Args:
        wait_max: The longest delay, in seconds.
        multiplier: The first window, in seconds.

    Raises:
        TypeError: A value is a bool or not a number.
        ValueError: A value is not a finite number > 0.
    """
    _check_positive("wait_max", wait_max)
    _check_positive("multiplier", multiplier)


def capacity_delay(
    wait_number: int,
    *,
    wait_max: float = CAPACITY_WAIT_MAX,
    multiplier: float = 1.0,
    rng: random.Random | None = None,
) -> float:
    """Pick the jittered exponential delay of one capacity wait.

    Args:
        wait_number: The number of capacity waits before this one, from 0.
        wait_max: The longest delay, in seconds.
        multiplier: The first window, in seconds. Each wait doubles the window.
        rng: The random source. The default is a module-level ``Random``.

    Returns:
        A random delay between 0 and ``min(wait_max, multiplier * 2**wait_number)``.

    Raises:
        ValueError: ``wait_max`` or ``multiplier`` is not a finite number > 0.
    """
    _check_capacity_window(wait_max, multiplier)
    # The exponent is capped so that a very long wait cannot overflow a float.
    window = min(wait_max, multiplier * 2.0 ** min(wait_number, 64))
    return (rng or _RNG).uniform(0.0, window)


async def capacity_wait(
    at_capacity: Callable[[], Awaitable[bool]],
    *,
    first_wait: int = 0,
    wait_max: float = CAPACITY_WAIT_MAX,
    multiplier: float = 1.0,
    clock: Clock | None = None,
    rng: random.Random | None = None,
) -> int:
    """Wait while the provider reports that it is at capacity.

    The wait has no attempt limit. A cancel stops it at once.

    Args:
        at_capacity: Returns True while the provider has no capacity.
        first_wait: The number of capacity waits that came before, so the
            window keeps growing across calls.
        wait_max: The longest single delay, in seconds.
        multiplier: The first window, in seconds.
        clock: The clock. The default is the system clock.
        rng: The random source.

    Returns:
        The number of capacity waits so far, ``first_wait`` included.

    Raises:
        ValueError: ``wait_max`` or ``multiplier`` is not a finite number > 0.
    """
    _check_capacity_window(wait_max, multiplier)
    clock = clock or SYSTEM_CLOCK
    waits = first_wait
    while await at_capacity():
        await clock.sleep(capacity_delay(waits, wait_max=wait_max, multiplier=multiplier, rng=rng))
        waits += 1
    return waits


async def create_with_capacity(
    create: Callable[[R], Awaitable[T]],
    reserve: Callable[[], AbstractAsyncContextManager[R]],
    *,
    at_capacity: Callable[[], Awaitable[bool]] | None = None,
    bucket: TokenBucket | None = None,
    wait_max: float = CAPACITY_WAIT_MAX,
    multiplier: float = 1.0,
    clock: Clock | None = None,
    rng: random.Random | None = None,
) -> T:
    """Wait for capacity, then reserve the budget, then create the sandbox.

    Neither the capacity wait nor the create pacing holds a budget
    reservation. When the create raises :class:`ProviderAtCapacityError`,
    the reservation context exits with that error, so it must release the
    hold, and the loop goes back to the capacity wait. Other errors pass
    through unchanged.

    Args:
        create: Takes the reservation and creates the sandbox.
        reserve: Makes the async context manager that holds the budget
            reservation for one create call.
        at_capacity: Returns True while the provider has no capacity. With
            ``None``, only a :class:`ProviderAtCapacityError` causes a wait.
        bucket: The create bucket of the provider, or ``None`` for no pacing.
        wait_max: The longest single capacity delay, in seconds.
        multiplier: The first capacity window, in seconds.
        clock: The clock. The default is the system clock.
        rng: The random source.

    Returns:
        The result of the create call.

    Raises:
        ValueError: ``wait_max`` or ``multiplier`` is not a finite number > 0.
    """
    _check_capacity_window(wait_max, multiplier)
    clock = clock or SYSTEM_CLOCK
    waits = 0
    while True:
        if at_capacity is not None:
            waits = await capacity_wait(
                at_capacity,
                first_wait=waits,
                wait_max=wait_max,
                multiplier=multiplier,
                clock=clock,
                rng=rng,
            )
        if bucket is not None:
            await bucket.acquire()
        try:
            async with reserve() as reservation:
                return await create(reservation)
        except ProviderAtCapacityError:
            pass
        await clock.sleep(capacity_delay(waits, wait_max=wait_max, multiplier=multiplier, rng=rng))
        waits += 1

"""Cross-pod sandbox limits: a Redis counting semaphore with leases that expire.

The pool's own semaphores count boxes in one process only, so many worker
pods together can go above the provider's limit (SBX-23, SKY-431). A
``RedisSlotLimiter`` keeps one shared count in a Redis sorted set: each
member is a holder id, and its score is the time its lease ends. A holder
refreshes its lease while its box runs, so the slot of a pod that dies
comes back when the lease ends.

A take is an optimistic transaction (WATCH, then MULTI/EXEC): it reads the
live holders and adds itself only when the key did not change in between.
Lease times come from the Redis server clock, so clock skew between pods
does not change them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import uuid
from collections.abc import AsyncIterator, Coroutine
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError, WatchError

logger = logging.getLogger(__name__)

DEFAULT_LEASE_SECONDS = 60.0
DEFAULT_POLL_SECONDS = 0.5

# The longest wait for the release of a slot. A Redis call that hangs must
# not block a cancel forever; the lease frees the slot later.
RELEASE_TIMEOUT_SECONDS = 10.0


class SlotLimiter(Protocol):
    """Give out slots of a limit that more than one process shares."""

    def hold(self, name: str) -> AbstractAsyncContextManager[None]:
        """Wait for a slot, hold it for the body, then give it back.

        Args:
            name: A readable name for the holder, such as the box id.

        Returns:
            An async context manager that holds one slot.
        """
        ...


def _server_seconds(time: tuple[int, int]) -> float:
    """Return the Redis ``TIME`` reply as seconds.

    Args:
        time: The seconds and microseconds that ``TIME`` gives.

    Returns:
        The time in seconds.
    """
    seconds, micros = time
    return seconds + micros / 1_000_000


class RedisSlotLimiter:
    """Share a slot limit across processes through one Redis sorted set.

    A slot wait polls Redis and is not fair: any waiter can take a free
    slot first.
    """

    def __init__(
        self,
        redis: Redis,
        key: str,
        limit: int,
        *,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> None:
        """Bind the limiter to a Redis key and a limit.

        Args:
            redis: An async Redis client. Every process that shares the
                limit uses the same server and key.
            key: The sorted set key that holds the holders.
            limit: The most slots that can be held at once.
            lease_seconds: How long a slot lives with no refresh. A holder
                refreshes it three times in each lease.
            poll_seconds: The mean wait between two tries when no slot is free.

        Raises:
            TypeError: ``limit`` is not an int.
            ValueError: ``limit`` is below 1, or a time is not above 0.
        """
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit must be an int")
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if lease_seconds <= 0 or poll_seconds <= 0:
            raise ValueError("lease_seconds and poll_seconds must be > 0")
        self._redis = redis
        self.key = key
        self.limit = limit
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds

    async def try_acquire(self, holder: str) -> bool:
        """Take a slot for the holder when one is free; do not wait.

        Args:
            holder: The holder id. It must be unique to this hold.

        Returns:
            True when the holder now has a slot.
        """
        async with self._redis.pipeline(transaction=True) as pipe:
            while True:
                try:
                    await pipe.watch(self.key)
                    now = _server_seconds(await pipe.time())
                    if await pipe.zcount(self.key, f"({now}", "+inf") >= self.limit:
                        return False
                    pipe.multi()
                    pipe.zremrangebyscore(self.key, "-inf", now)
                    pipe.zadd(self.key, {holder: now + self.lease_seconds})
                    await pipe.execute()
                    return True
                except WatchError:
                    # Another holder changed the set between the count and the add.
                    continue

    async def refresh(self, holder: str) -> bool:
        """Move the end of the holder's lease to one lease from now.

        Args:
            holder: The holder id.

        Returns:
            False when the holder has no slot any more.
        """
        now = _server_seconds(await self._redis.time())
        # XX never adds a holder that a take already trimmed, so a late
        # refresh cannot push the count above the limit.
        changed = await self._redis.zadd(self.key, {holder: now + self.lease_seconds}, xx=True, ch=True)
        return bool(changed)

    async def release(self, holder: str) -> None:
        """Give back the holder's slot. A holder with no slot is a no-op.

        Args:
            holder: The holder id.
        """
        await self._redis.zrem(self.key, holder)

    @contextlib.asynccontextmanager
    async def hold(self, name: str) -> AsyncIterator[None]:
        """Wait for a slot, keep its lease alive for the body, then give it back.

        The release also runs when the wait is cancelled, so a cancel after
        the take but before the return frees the slot. A Redis error in the
        release is only logged; the lease frees the slot later.

        Args:
            name: A readable name for the holder, such as the box id.

        Yields:
            None, while the slot is held.

        Raises:
            RedisError: The wait could not reach Redis.
        """
        holder = f"{name}:{uuid.uuid4().hex}"
        keeper: asyncio.Task[None] | None = None
        try:
            while True:
                if await self.try_acquire(holder):
                    break
                # A free slot in another pod sends no signal here, so the wait
                # polls; the jitter spreads the waiters of many pods over time.
                await asyncio.sleep(self.poll_seconds * random.uniform(0.5, 1.5))
            keeper = asyncio.create_task(self._keep(holder))
            yield
        finally:
            await self._settle(self._close(holder, keeper), holder)

    async def _keep(self, holder: str) -> None:
        """Refresh the holder's lease until the task is cancelled.

        Args:
            holder: The holder id.
        """
        while True:
            await asyncio.sleep(self.lease_seconds / 3)
            try:
                if not await self.refresh(holder):
                    logger.error("Sandbox slot %s on %s lost its lease; another holder can take it", holder, self.key)
            except RedisError:
                logger.warning("Could not refresh sandbox slot %s on %s", holder, self.key, exc_info=True)

    async def _close(self, holder: str, keeper: asyncio.Task[None] | None) -> None:
        """Stop the lease refresh, then give back the slot.

        Args:
            holder: The holder id.
            keeper: The refresh task, or None when the slot was never taken.
        """
        if keeper is not None:
            keeper.cancel()
            await asyncio.wait({keeper})
        await self.release(holder)

    async def _settle(self, work: Coroutine[Any, Any, None], holder: str) -> None:
        """Run the release to its end, also through a cancel; log an error or a hang.

        A cancel that arrives during the release waits until the release
        ends, then goes on, so a cancelled box gives back its slot. The
        wait ends after ``RELEASE_TIMEOUT_SECONDS``.

        Args:
            work: The release.
            holder: The holder id, for the log.

        Raises:
            asyncio.CancelledError: The task was cancelled during the release.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + RELEASE_TIMEOUT_SECONDS
        task = asyncio.ensure_future(work)
        cancel: asyncio.CancelledError | None = None
        while not task.done() and loop.time() < deadline:
            try:
                await asyncio.wait({task}, timeout=deadline - loop.time())
            except asyncio.CancelledError as exc:
                cancel = exc
        if not task.done():
            task.cancel()
            logger.error(
                "Sandbox slot %s on %s did not release within %s seconds; its lease frees it",
                holder,
                self.key,
                RELEASE_TIMEOUT_SECONDS,
            )
        elif not task.cancelled() and (exc := task.exception()) is not None:
            logger.error("Sandbox slot %s on %s did not release; its lease frees it", holder, self.key, exc_info=exc)
        if cancel is not None:
            raise cancel

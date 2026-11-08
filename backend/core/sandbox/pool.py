"""Sandbox pool: a box limit for each provider, phase pools and a bounded producer.

Adapted from Harbor (harbor-framework/harbor), Apache-2.0: the global
semaphore and the per-key pools follow ``TrialQueue`` in
``harbor/orchestrators/queue.py``. Changes: the per-agent pools become pools
for the setup, run and score phases; the provider permit is held from the
box start to the box stop, and a phase permit only while the box is in that
phase; candidates come from an async iterator with at most ``queue_size``
of them waiting, so memory is O(C) and not O(N); and retries come from the
SBX-2 retry policy, which never retries by default (SKY-406 D3, D10).

No engine uses the pool yet. SKY-406 phase 2 moves the engines one by one.
Limits hold inside one process; cross-pod limits come in SBX-23.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import itertools
import logging
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Generic, TypeVar

from .environment import SandboxEnvironment
from .errors import SandboxFatalError
from .hooks import PHASE_ENDED, PHASE_STARTED, BoxEvent, BoxEventInfo, BoxHooks
from .retry import DEFAULT_RETRY_POLICY, RetryPolicy, run_with_retry
from .timeouts import Phase, PhaseTimeouts

logger = logging.getLogger(__name__)

T = TypeVar("T")
R = TypeVar("R")

POOL_PHASES = (Phase.SETUP, Phase.RUN, Phase.SCORE)

# The longest wait for a box stop. A provider stop that hangs must not
# block the pool, or a cancel of it, forever.
STOP_TIMEOUT_SECONDS = 60.0

_DONE = object()


def _check_limit(name: str, value: object) -> int:
    """Return a positive int or raise.

    Args:
        name: The limit name for the error message.
        value: The value to check.

    Returns:
        The value.

    Raises:
        TypeError: The value is a bool or not an int.
        ValueError: The value is below 1.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int")
    if value < 1:
        raise ValueError(f"{name} must be >= 1")
    return value


@dataclass(frozen=True)
class BoxWork(Generic[T, R]):
    """Say how to make the box of a candidate and what each phase does in it.

    The result of a box is the return value of its last phase: ``score``
    when it is set, else ``run``.
    """

    create: Callable[[T], SandboxEnvironment]
    run: Callable[[SandboxEnvironment, T], Awaitable[R]]
    setup: Callable[[SandboxEnvironment, T], Awaitable[object]] | None = None
    score: Callable[[SandboxEnvironment, T], Awaitable[R]] | None = None


@dataclass(frozen=True)
class BoxOutcome(Generic[T, R]):
    """Hold the result or the error of one candidate's box."""

    key: str
    candidate: T
    result: R | None = None
    error: Exception | None = None

    @property
    def ok(self) -> bool:
        """Return True when the box finished without an error."""
        return self.error is None


class SandboxPool:
    """Run candidates in boxes under a box limit for each provider and phase limits.

    Make one pool for each event loop and share it, so that every engine
    on that loop counts against the same limits. The semaphores bind to
    the loop of the first run, so a run on another loop raises.

    The default number of workers is the highest provider limit. A box
    holds its provider permit from its start to its stop, also while it
    waits for a phase permit, so a started box that waits for a phase
    still counts against its provider limit.
    """

    def __init__(
        self,
        provider_limits: Mapping[str, int],
        *,
        phase_limits: Mapping[Phase, int] | None = None,
        hooks: BoxHooks | None = None,
        retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
        timeouts: PhaseTimeouts | None = None,
    ) -> None:
        """Create the provider and phase semaphores.

        Args:
            provider_limits: The most boxes that can live at once, keyed by
                the provider's capability name.
            phase_limits: The most boxes that can be in a phase at once. A
                phase with no limit is bound only by the provider limit.
            hooks: The hooks that receive each box event.
            retry_policy: The retry policy for one box. The default never retries.
            timeouts: The phase timeouts, or None for no timeouts.

        Raises:
            TypeError: A limit is not an int.
            ValueError: A limit is below 1, no provider has a limit, or a
                phase limit names a phase other than setup, run or score.
        """
        if not provider_limits:
            raise ValueError("provider_limits must name at least one provider")
        self._provider_slots = {
            name: asyncio.Semaphore(_check_limit(f"limit of {name}", limit)) for name, limit in provider_limits.items()
        }
        self._max_provider_limit = max(provider_limits.values())
        self._phase_slots: dict[Phase, asyncio.Semaphore] = {}
        for phase, limit in (phase_limits or {}).items():
            phase = Phase(phase)
            if phase not in POOL_PHASES:
                raise ValueError(f"phase limits apply to setup, run and score, not {phase}")
            self._phase_slots[phase] = asyncio.Semaphore(_check_limit(f"limit of {phase}", limit))
        self.hooks = hooks if hooks is not None else BoxHooks()
        self.retry_policy = retry_policy
        self.timeouts = timeouts
        self._ids = itertools.count()
        self._loop: asyncio.AbstractEventLoop | None = None

    async def run(
        self,
        candidates: AsyncIterable[T],
        work: BoxWork[T, R],
        on_outcome: Callable[[BoxOutcome[T, R]], Awaitable[None]],
        *,
        workers: int | None = None,
        queue_size: int | None = None,
        key: Callable[[T], str] | None = None,
    ) -> None:
        """Run each candidate in its own box until the candidates run out.

        A box that fails gives an outcome with the error; it does not stop
        the other boxes. A cancel stops the producer and every box, and
        releases every permit.

        Args:
            candidates: The candidates. The pool reads them only as queue space frees.
            work: How to make a box and what each phase does.
            on_outcome: Receives the outcome of each box as it finishes.
            workers: The most candidates in progress at once. The default is
                the highest provider limit.
            queue_size: The most candidates read but not yet taken by a
                worker. The default is ``workers``.
            key: Gives the operation key of a candidate. The default is a
                key that is unique in this pool.

        Raises:
            RuntimeError: The pool already ran on another event loop.
            TypeError: ``workers`` or ``queue_size`` is not an int.
            ValueError: ``workers`` or ``queue_size`` is below 1.
            SandboxFatalError: The candidate iterator, its ``aclose`` or
                ``on_outcome`` raised a cancel that nobody asked for.
            Exception: The first error of the candidate iterator or of
                ``on_outcome``, after the other tasks stop. An error of the
                iterator's ``aclose`` after the last candidate comes after
                the boxes in flight finish.
        """
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("a SandboxPool runs on one event loop only; make one pool for each event loop")
        workers = _check_limit("workers", self._max_provider_limit if workers is None else workers)
        queue_size = _check_limit("queue_size", workers if queue_size is None else queue_size)
        # The producer takes a slot before it reads a candidate and a worker
        # gives it back when it takes one, so at most queue_size candidates
        # wait. The queue itself is unbounded so the end markers never block.
        slots = asyncio.Semaphore(queue_size)
        queue: asyncio.Queue[object] = asyncio.Queue()
        close_error: Exception | None = None

        async def close(iterator: AsyncIterator[T]) -> None:
            """Close the candidate iterator when it has an ``aclose``.

            Args:
                iterator: The candidate iterator.

            Raises:
                SandboxFatalError: ``aclose`` raised a cancel that nobody asked for.
            """
            aclose = getattr(iterator, "aclose", None)
            if aclose is None:
                return
            try:
                await aclose()
            except asyncio.CancelledError as cancel:
                task = asyncio.current_task()
                if task is None or task.cancelling():
                    raise
                raise SandboxFatalError(
                    "the candidate iterator's aclose raised a cancel that nobody asked for"
                ) from cancel

        async def read(iterator: AsyncIterator[T]) -> None:
            """Read candidates into the queue until the iterator ends.

            Args:
                iterator: The candidate iterator.

            Raises:
                SandboxFatalError: The iterator raised a cancel that nobody asked for.
            """
            try:
                while True:
                    await slots.acquire()
                    try:
                        candidate = await anext(iterator)
                    except StopAsyncIteration:
                        return
                    queue.put_nowait((key(candidate) if key else f"box-{next(self._ids)}", candidate))
            except asyncio.CancelledError as cancel:
                task = asyncio.current_task()
                if task is None or task.cancelling():
                    raise
                # The TaskGroup drops a cancelled producer without an error,
                # so run() would return as if the candidates ran out.
                raise SandboxFatalError("the candidate iterator raised a cancel that nobody asked for") from cancel

        async def produce() -> None:
            """Read candidates into the queue, then put one end marker for each worker."""
            nonlocal close_error
            iterator = aiter(candidates)
            try:
                try:
                    await read(iterator)
                except BaseException:
                    try:
                        await close(iterator)
                    except Exception:
                        # An aclose error must not replace the iterator's own
                        # error or the caller's cancel, so it is only logged.
                        logger.exception("The candidate iterator's aclose failed after an error or a cancel")
                    raise
                try:
                    await close(iterator)
                except Exception as exc:
                    # Raising here would make the TaskGroup cancel the boxes in
                    # flight and lose their outcomes, so run() raises it at the end.
                    close_error = exc
            finally:
                # Always put the end markers, or the workers wait forever.
                for _ in range(workers):
                    queue.put_nowait(_DONE)

        async def consume() -> None:
            """Take candidates from the queue and run their boxes until the end marker."""
            while True:
                item = await queue.get()
                if item is _DONE:
                    return
                slots.release()
                box_key, candidate = item  # type: ignore[misc]
                outcome = await self._box(box_key, candidate, work)
                try:
                    await on_outcome(outcome)
                except asyncio.CancelledError as cancel:
                    task = asyncio.current_task()
                    if task is None or task.cancelling():
                        raise
                    # The TaskGroup drops a cancelled worker without an error,
                    # so with one worker run() would wait forever.
                    raise SandboxFatalError(
                        f"on_outcome raised a cancel that nobody asked for on box {box_key}"
                    ) from cancel

        error: BaseException | None = None
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(produce())
                for _ in range(workers):
                    group.create_task(consume())
        except BaseExceptionGroup as errors:
            # Callers expect the error itself, not a group. Boxes catch their
            # own errors, so the group holds the producer or callback error.
            error = errors.exceptions[0]
        if error is not None:
            # Raised outside the except block, so the error keeps its own
            # __cause__ and __context__ and does not get the group as context.
            raise error
        if close_error is not None:
            raise close_error

    async def _box(self, box_key: str, candidate: T, work: BoxWork[T, R]) -> BoxOutcome[T, R]:
        """Run one candidate's box under the retry policy.

        Args:
            box_key: The candidate's operation key.
            candidate: The candidate.
            work: How to make the box and what each phase does.

        Returns:
            The outcome with the result, or with the error of the last attempt.
        """
        attempt = functools.partial(self._attempt, candidate=candidate, work=work)
        try:
            result = await run_with_retry(attempt, box_key, self.retry_policy)
        except Exception as exc:
            return BoxOutcome(box_key, candidate, error=exc)
        return BoxOutcome(box_key, candidate, result=result)

    async def _attempt(self, box_id: str, *, candidate: T, work: BoxWork[T, R]) -> R:
        """Run one attempt: make and start a box, run its phases, then stop it.

        Args:
            box_id: The attempt's operation key.
            candidate: The candidate.
            work: How to make the box and what each phase does.

        Returns:
            The return value of the last phase.

        Raises:
            ValueError: The box's provider has no limit in this pool.
            SandboxFatalError: The start or a phase raised a cancel that
                nobody asked for, so the box gets an outcome.
            Exception: The error of the start or of a phase.
        """
        try:
            # Inside the try, so a cancel during this emit still ends the box
            # with CANCELLED for the hooks that saw STARTED.
            await self.hooks.emit(BoxEventInfo(BoxEvent.STARTED, box_id))
            env = work.create(candidate)
            provider = env.capabilities.name
            slot = self._provider_slots.get(provider)
            if slot is None:
                raise ValueError(f"the pool has no box limit for provider {provider!r}")
            async with slot:
                try:
                    async with self._timeout(Phase.START):
                        await env.start()
                    await self.hooks.emit(BoxEventInfo(BoxEvent.ENVIRONMENT_STARTED, box_id))
                    if work.setup is not None:
                        await self._phase(Phase.SETUP, work.setup, env, candidate, box_id)
                    result = await self._phase(Phase.RUN, work.run, env, candidate, box_id)
                    if work.score is not None:
                        result = await self._phase(Phase.SCORE, work.score, env, candidate, box_id)
                finally:
                    await self._stop(env, box_id)
        except asyncio.CancelledError as cancel:
            task = asyncio.current_task()
            if task is None or task.cancelling():
                await self.hooks.emit(BoxEventInfo(BoxEvent.CANCELLED, box_id))
                raise
            # Provider code can raise a cancel that nobody asked for, e.g.
            # when it awaits an inner task that was cancelled. The TaskGroup
            # drops a cancelled worker without an error, so the outcome would
            # be lost and the producer would wait forever for a queue slot.
            error = SandboxFatalError(f"box {box_id} raised a cancel that nobody asked for")
            await self.hooks.emit(BoxEventInfo(BoxEvent.ENDED, box_id, error=error))
            raise error from cancel
        except Exception as exc:
            await self.hooks.emit(BoxEventInfo(BoxEvent.ENDED, box_id, error=exc))
            raise
        await self.hooks.emit(BoxEventInfo(BoxEvent.ENDED, box_id))
        return result

    async def _phase(
        self,
        phase: Phase,
        step: Callable[[SandboxEnvironment, T], Awaitable[R]],
        env: SandboxEnvironment,
        candidate: T,
        box_id: str,
    ) -> R:
        """Run one phase under its phase permit and timeout.

        Args:
            phase: The phase.
            step: The phase's work.
            env: The running box.
            candidate: The candidate.
            box_id: The attempt's operation key.

        Returns:
            The step's return value.
        """
        slot = self._phase_slots.get(phase)
        permit: AbstractAsyncContextManager[object] = slot if slot is not None else contextlib.nullcontext()
        async with permit:
            await self.hooks.emit(BoxEventInfo(PHASE_STARTED[phase], box_id, phase=phase))
            try:
                async with self._timeout(phase):
                    return await step(env, candidate)
            finally:
                await self.hooks.emit(BoxEventInfo(PHASE_ENDED[phase], box_id, phase=phase))

    def _timeout(self, phase: Phase) -> AbstractAsyncContextManager[None]:
        """Return the phase timeout, or no timeout when the pool has none.

        Args:
            phase: The phase.

        Returns:
            An async context manager that limits the body's time.
        """
        return self.timeouts.limit(phase) if self.timeouts is not None else contextlib.nullcontext()

    async def _stop(self, env: SandboxEnvironment, box_id: str) -> None:
        """Stop a box; log a stop that fails so that the box keeps its own outcome.

        A cancel that arrives during the stop waits until the stop finishes,
        then goes on, so that a cancelled pool leaves no box running. The
        wait ends after ``STOP_TIMEOUT_SECONDS``, so a stop that hangs cannot
        block the pool or a cancel forever.

        Args:
            env: The box.
            box_id: The attempt's operation key.

        Raises:
            asyncio.CancelledError: The pool was cancelled during the stop.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + STOP_TIMEOUT_SECONDS
        stop = asyncio.ensure_future(env.stop())
        cancel: asyncio.CancelledError | None = None
        while not stop.done() and loop.time() < deadline:
            try:
                # wait() does not cancel the stop when this task is cancelled,
                # and it raises only for a cancel of this task.
                await asyncio.wait({stop}, timeout=deadline - loop.time())
            except asyncio.CancelledError as exc:
                cancel = exc
        if not stop.done():
            # Cancel the hung stop rather than leave it running: a task left
            # behind can outlive run() and the event loop. The box's own
            # lifetime still ends it at the provider.
            stop.cancel()
            logger.error("Sandbox box %s did not stop within %s seconds", box_id, STOP_TIMEOUT_SECONDS)
        else:
            try:
                stop.result()
            except (Exception, asyncio.CancelledError):
                # The pool cancels a stop only after the time limit, so a
                # cancel here comes from the provider and is a stop failure.
                logger.exception("Sandbox box %s did not stop", box_id)
        if cancel is not None:
            raise cancel

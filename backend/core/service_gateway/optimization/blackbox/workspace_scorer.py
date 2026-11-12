"""Fan sync scorer calls out over a sandbox pool, and shape a case for the harness.

:func:`run_on_pool` runs the scorers' blocking calls through the shared
:class:`~core.sandbox.pool.SandboxPool`, so every fan-out obeys its
``workers`` limit. :func:`case_document` gives the workspace harness the
``{"input", "expected"}`` form of one case.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import Any, ClassVar, TypeVar

import anyio

from ....models.unified import UnifiedCase
from ....sandbox import EnvironmentCapabilities, ExecResult, OutputSink, SandboxEnvironment, SandboxSpec, sync_bridge
from ....sandbox.pool import BoxOutcome, BoxWork, SandboxPool
from ....sandbox.timeouts import Phase

logger = logging.getLogger(__name__)

R = TypeVar("R")

POOL_PROVIDER = "blackbox-sync"
# A slot box opens nothing, but the spec check needs a finite lifetime.
_SLOT_LIFETIME_SECONDS = 24 * 3600.0


class _SkippedError(Exception):
    """Mark a call that the pool read before an earlier call failed, so it never ran."""


class _KeptError(Exception):
    """Mark a call whose own error is already kept for the caller."""


class _SlotBox(SandboxEnvironment):
    """Hold one pool permit while a sync scorer call runs; it opens no sandbox.

    The scorers still open their own sandboxes through the sync runtime, so
    this box only counts the call against its fan-out's ``workers``.
    """

    capabilities: ClassVar[EnvironmentCapabilities] = EnvironmentCapabilities(name=POOL_PROVIDER)

    async def start(self) -> None:
        """Do nothing: the scorer call opens its own sandbox."""

    async def stop(self) -> None:
        """Do nothing: the scorer call closes its own sandbox."""

    async def exec(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_sec: float | None = None,
        on_output: OutputSink | None = None,
        sudo: bool = False,
    ) -> ExecResult:
        """Refuse: a slot box runs no commands.

        Args:
            command: The shell command.
            cwd: The working directory.
            env: Extra environment variables.
            timeout_sec: The time limit.
            on_output: Receives output chunks.
            sudo: Run the command as root.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError("a slot box runs no commands")

    async def upload_files(self, files: Mapping[str, str]) -> None:
        """Refuse: a slot box holds no files.

        Args:
            files: Maps each sandbox path to its text content.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError("a slot box holds no files")

    async def download_file(self, path: str) -> str | None:
        """Refuse: a slot box holds no files.

        Args:
            path: The sandbox path.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError("a slot box holds no files")

    @classmethod
    async def stop_job_sandboxes(cls, job_id: str) -> int:
        """Stop nothing: slot boxes leave nothing running.

        Args:
            job_id: The job tag value.

        Returns:
            Zero.
        """
        return 0


def run_on_pool(phase: Phase, calls: Sequence[Callable[[], R]], workers: int) -> list[R]:
    """Run sync calls on a new sandbox pool for this fan-out, one box per call, in order.

    Only ``workers`` bounds the calls in flight; Skynet sets no process box
    limit, since the user controls spend with the run's budget. Each
    call runs in its own thread with a copy of the caller's context, so run
    and usage scopes reach it. After the first failure no further call
    starts, and the failure of the earliest call is raised once the calls
    in flight end. A failure can be any exception, also a ``BaseException``
    such as ``BudgetReached``; it is raised in the caller's thread, not on
    the shared portal loop. A cancel waits for the calls in flight, then
    releases every permit.

    Args:
        phase: ``Phase.RUN`` for final-run cases, ``Phase.SCORE`` for scorer cases.
        calls: The calls, each with no arguments.
        workers: The most calls in flight at once for this fan-out.

    Returns:
        Each call's result, in the calls' order.

    Raises:
        ValueError: The phase is neither run nor score.
        BaseException: The error of the earliest call that failed, unchanged.
    """
    if phase not in (Phase.RUN, Phase.SCORE):
        raise ValueError(f"pooled calls run in the run or score phase, not {phase}")
    if not calls:
        return []
    items = [(contextvars.copy_context(), call) for call in calls]
    results, error = sync_bridge.run(_run_on_pool, phase, items, max(1, min(workers, len(items))))
    if error is not None:
        raise error
    return results


async def _run_on_pool(
    phase: Phase, items: list[tuple[contextvars.Context, Callable[[], R]]], workers: int
) -> tuple[list[R], BaseException | None]:
    """Run each call in a slot box on the pool and gather the results by position.

    A call's error is returned, not raised: a ``BaseException`` that leaves
    a portal task crashes the shared portal loop and every call on it.

    Args:
        phase: The pool phase the calls run in.
        items: Each call with the caller's context copy it runs in.
        workers: The most calls in flight at once.

    Returns:
        Each call's result in the items' order and no error, or no results
        and the error of the earliest call that failed.
    """
    # A pool for each fan-out: no limit is shared, so one fan-out never
    # waits for another's boxes. An explicit thread limiter, because anyio's
    # default of 40 threads is shared with other work on the loop.
    pool = SandboxPool({POOL_PROVIDER: workers})
    limiter = anyio.CapacityLimiter(workers)
    results: dict[int, R] = {}
    errors: dict[int, BaseException] = {}

    async def candidates() -> AsyncIterator[int]:
        """Yield each call's position until a call fails.

        Yields:
            The next call's position.
        """
        for index in range(len(items)):
            if errors:
                return
            yield index

    async def call(env: SandboxEnvironment, index: int) -> R:
        """Run one call in a thread, unless an earlier call already failed.

        Args:
            env: The call's slot box.
            index: The call's position.

        Returns:
            The call's result.

        Raises:
            _SkippedError: An earlier call failed, so this one does not start.
            _KeptError: The call failed; its error is kept in ``errors``.
        """
        if errors:
            raise _SkippedError
        context, func = items[index]
        try:
            return await _in_thread(context, func, limiter)
        except anyio.get_cancelled_exc_class():
            raise
        except BaseException as exc:
            # The pool handles only Exception, so BudgetReached or
            # KeyboardInterrupt would escape its TaskGroup and crash the portal.
            errors.setdefault(index, exc)
            raise _KeptError from exc

    async def no_run(env: SandboxEnvironment, index: int) -> None:
        """Do nothing: a scorer case does all its work in the score phase.

        Args:
            env: The call's slot box.
            index: The call's position.
        """

    async def collect(outcome: BoxOutcome[int, R]) -> None:
        """Keep a box's result, or its error unless the box was skipped.

        Args:
            outcome: The box outcome.
        """
        if outcome.error is None:
            results[outcome.candidate] = outcome.result  # type: ignore[assignment]
        elif not isinstance(outcome.error, (_SkippedError, _KeptError)):
            errors.setdefault(outcome.candidate, outcome.error)  # type: ignore[arg-type]

    work: BoxWork[int, Any] = (
        BoxWork(create=_slot_box, run=no_run, score=call)
        if phase is Phase.SCORE
        else BoxWork(create=_slot_box, run=call)
    )
    await pool.run(candidates(), work, collect, workers=workers)
    if errors:
        return [], errors[min(errors)]
    return [results[index] for index in range(len(items))], None


def _slot_box(index: int) -> SandboxEnvironment:
    """Make the slot box of one pooled call.

    Args:
        index: The call's position.

    Returns:
        A new slot box.
    """
    return _SlotBox(SandboxSpec(lifetime_seconds=_SLOT_LIFETIME_SECONDS))


async def _in_thread(context: contextvars.Context, func: Callable[[], R], limiter: anyio.CapacityLimiter) -> R:
    """Run a sync call in a worker thread and wait for it even through a cancel.

    A thread cannot be stopped, so a cancel waits for the call to end before
    it goes on. The box permit is then held until the thread ends, and the
    pool never has more than its ``workers`` calls running.

    Args:
        context: The caller's context copy to run the call in.
        func: The call.
        limiter: Bounds the worker threads.

    Returns:
        The call's result.

    Raises:
        asyncio.CancelledError: The pool was cancelled while the call ran.
        BaseException: The call's own error.
    """
    thread = asyncio.ensure_future(anyio.to_thread.run_sync(_call_in_context, context, func, limiter=limiter))
    cancel: asyncio.CancelledError | None = None
    while not thread.done():
        try:
            # wait() does not cancel the thread task when this task is cancelled.
            await asyncio.wait({thread})
        except asyncio.CancelledError as exc:
            cancel = exc
    if cancel is not None:
        if not thread.cancelled():
            # Read the call's error so asyncio does not log it as never retrieved.
            thread.exception()
        raise cancel
    result, error = thread.result()
    if error is not None:
        raise error
    return result  # type: ignore[return-value]


def _call_in_context(context: contextvars.Context, func: Callable[[], R]) -> tuple[R | None, BaseException | None]:
    """Run a sync call in a context and return its error instead of raising it.

    An asyncio task that raises ``KeyboardInterrupt`` or ``SystemExit`` stops
    its event loop, so the thread task must never raise one.

    Args:
        context: The caller's context copy to run the call in.
        func: The call.

    Returns:
        The call's result and no error, or no result and the call's error.
    """
    try:
        return context.run(func), None
    except BaseException as exc:
        return None, exc


def case_document(case: Any) -> dict[str, Any]:
    """Return the ``{"input", "expected"}`` form the harness reads.

    The harness gives the candidate only ``input`` (E42, E62). A dict with no
    ``input`` key is its own input, so its ``expected`` key moves out of that
    input and becomes the case's ``expected``.

    Args:
        case: A :class:`UnifiedCase`, a case mapping or ``None``.

    Returns:
        The case as plain data.
    """
    if isinstance(case, UnifiedCase):
        return case.model_dump(mode="json")
    if isinstance(case, dict):
        if "input" in case:
            return {"input": dict(case.get("input") or {}), "expected": case.get("expected")}
        return {
            "input": {key: value for key, value in case.items() if key != "expected"},
            "expected": case.get("expected"),
        }
    return {"input": {}, "expected": None}

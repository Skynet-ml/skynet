"""Tests for the sync bridge: one portal for each process, and pass-through of results, errors and cancel."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading

import anyio
import anyio.lowlevel
import pytest

from core.sandbox import SandboxSpec, sync_bridge
from core.sandbox.errors import SandboxTransientError
from core.sandbox.tests.fake_provider import InMemorySandbox


async def _add(left: int, right: int) -> int:
    """Return the sum after one event-loop step.

    Args:
        left: The first number.
        right: The second number.

    Returns:
        The sum.
    """
    await anyio.lowlevel.checkpoint()
    return left + right


async def _fail() -> None:
    """Raise a sandbox error from inside the portal.

    Raises:
        SandboxTransientError: Always.
    """
    await anyio.lowlevel.checkpoint()
    raise SandboxTransientError("try again")


async def _cancel_self() -> None:
    """Cancel the task from inside, like a provider that cancels its own work."""
    raise asyncio.CancelledError


async def _thread_ident() -> int:
    """Return the identifier of the thread that runs the coroutine.

    Returns:
        The thread identifier.
    """
    return threading.get_ident()


def test_run_returns_the_coroutine_result() -> None:
    """Return the value of the async function to the sync caller."""
    assert sync_bridge.run(_add, 2, 3) == 5


def test_run_raises_the_original_exception() -> None:
    """Raise the same exception class and message that the coroutine raised."""
    with pytest.raises(SandboxTransientError, match="try again"):
        sync_bridge.run(_fail)


def test_cancel_inside_the_task_reaches_the_caller() -> None:
    """Report a task that cancels itself as a cancelled future."""
    with pytest.raises(concurrent.futures.CancelledError):
        sync_bridge.run(_cancel_self)


def test_cancelling_the_future_cancels_the_task() -> None:
    """Stop the coroutine when the sync caller cancels its future."""
    started = threading.Event()
    finished = threading.Event()
    cancelled = threading.Event()

    async def _wait_forever() -> None:
        """Wait until cancelled and record the cancel."""
        started.set()
        try:
            await anyio.sleep(60)
        except anyio.get_cancelled_exc_class():
            cancelled.set()
            raise
        finally:
            finished.set()

    future = sync_bridge.submit(_wait_forever)
    assert started.wait(5)
    assert future.cancel()
    assert finished.wait(5)
    assert cancelled.is_set()


def test_one_portal_serves_every_call_in_the_process() -> None:
    """Run every call on one long-lived event-loop thread."""
    first = sync_bridge.run(_thread_ident)
    second = sync_bridge.run(_thread_ident)
    assert first == second
    assert first != threading.get_ident()
    assert sync_bridge.get_portal() is sync_bridge.get_portal()


def test_parallel_threads_share_the_portal() -> None:
    """Give callers on many threads the same portal and correct results."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda n: sync_bridge.run(_add, n, n), range(32)))
        portals = set(pool.map(lambda _: id(sync_bridge.get_portal()), range(8)))
    assert results == [n * 2 for n in range(32)]
    assert len(portals) == 1


def test_portal_restarts_after_shutdown() -> None:
    """Start a new portal on the next call after shutdown."""
    before = sync_bridge.get_portal()
    sync_bridge.shutdown()
    assert sync_bridge.run(_add, 1, 1) == 2
    assert sync_bridge.get_portal() is not before


def test_shutdown_without_a_portal_does_nothing() -> None:
    """Allow shutdown to run twice."""
    sync_bridge.shutdown()
    sync_bridge.shutdown()


def test_run_from_the_portal_thread_is_refused() -> None:
    """Refuse a blocking call from the portal's own event loop, which would deadlock."""

    async def _nested() -> None:
        """Call the bridge from inside the portal."""
        sync_bridge.run(_add, 1, 1)

    with pytest.raises(RuntimeError):
        sync_bridge.run(_nested)


def test_sync_session_drives_an_environment() -> None:
    """Map the sync session calls to the async environment calls."""
    environment = InMemorySandbox(SandboxSpec(lifetime_seconds=60, env={"GREETING": "hi"}))
    session = sync_bridge.open_session(environment)
    assert environment.running
    session.write_files({"/work/a.txt": "alpha"})
    assert session.read_file("/work/a.txt") == "alpha"
    assert session.read_file("/work/missing.txt") is None
    result = session.run("printenv GREETING", timeout_seconds=5)
    assert result.ok
    assert result.stdout.strip() == "hi"
    assert not session.run("exit 3").ok
    session.close()
    assert not environment.running


def test_sync_session_streams_output() -> None:
    """Pass each output chunk to the sync callback."""
    environment = InMemorySandbox(SandboxSpec(lifetime_seconds=60))
    chunks: list[tuple[str, str]] = []
    session = sync_bridge.open_session(environment)
    try:
        session.run("echo hello", on_output=lambda stream, text: chunks.append((stream, text)))
    finally:
        session.close()
    assert chunks == [("stdout", "hello\n")]


def test_sync_session_errors_pass_through() -> None:
    """Raise the environment's own error from a sync session call."""
    environment = InMemorySandbox(SandboxSpec(lifetime_seconds=60))
    session = sync_bridge.open_session(environment)
    session.close()
    with pytest.raises(RuntimeError, match="not running"):
        session.run("true")

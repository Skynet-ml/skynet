"""Run async sandbox calls from sync code on one event-loop thread for each process.

Today's blackbox callers are sync. They use the ``SandboxSession`` protocol in
``blackbox/sandbox_contract.py``. This module gives them a blocking bridge to
the async :class:`SandboxEnvironment`: every call goes to one anyio
``BlockingPortal`` that runs on a daemon thread. :class:`SyncSession` matches
that protocol by structure; it does not import the contract, because the
blackbox package pulls in the DSPy service.
"""

from __future__ import annotations

import atexit
import concurrent.futures
import os
import threading
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, TypeVar

import anyio
from anyio.from_thread import BlockingPortal

from .environment import ExecResult, OutputSink, SandboxEnvironment

T = TypeVar("T")

_lock = threading.Lock()
_portal: BlockingPortal | None = None
_thread: threading.Thread | None = None


def get_portal() -> BlockingPortal:
    """Return the process portal, and start it on the first call.

    Returns:
        The running portal.
    """
    global _portal, _thread
    with _lock:
        if _portal is not None and _thread is not None and _thread.is_alive():
            return _portal
        ready: concurrent.futures.Future[BlockingPortal] = concurrent.futures.Future()
        # A daemon thread, not ``start_blocking_portal``: that helper's executor
        # thread is joined at interpreter exit, so a portal that lives for the
        # whole process would hang shutdown.
        thread = threading.Thread(target=_serve_portal, args=(ready,), name="sandbox-sync-bridge", daemon=True)
        thread.start()
        _portal = ready.result()
        _thread = thread
        return _portal


def submit(func: Callable[..., Awaitable[T]], *args: Any) -> concurrent.futures.Future[T]:
    """Start an async function on the portal and return its future.

    Cancel the future to cancel the task.

    Args:
        func: The async function.
        *args: Its positional arguments.

    Returns:
        The future of the result.
    """
    return get_portal().start_task_soon(func, *args)


def run(func: Callable[..., Awaitable[T]], *args: Any) -> T:
    """Run an async function on the portal and block until it ends.

    Errors reach the caller unchanged. A task that is cancelled raises
    ``concurrent.futures.CancelledError``. An interrupt of the waiting thread,
    such as ``KeyboardInterrupt``, cancels the task.

    Args:
        func: The async function.
        *args: Its positional arguments.

    Returns:
        The result of the function.

    Raises:
        RuntimeError: The caller is the portal's own event-loop thread.
    """
    if threading.current_thread() is _thread:
        raise RuntimeError("Do not call the sync bridge from its own event loop; await the call instead.")
    future = submit(func, *args)
    try:
        return future.result()
    except BaseException:
        future.cancel()
        raise


def shutdown() -> None:
    """Stop the process portal and wait for its thread. A later call starts a new one."""
    global _portal, _thread
    with _lock:
        portal, thread = _portal, _thread
        _portal = _thread = None
    if portal is None or thread is None or not thread.is_alive():
        return
    portal.call(portal.stop, True)
    thread.join(timeout=5)


def open_session(environment: SandboxEnvironment) -> SyncSession:
    """Start an environment and wrap it in a sync session.

    Args:
        environment: A stopped environment, for example from ``create_environment``.

    Returns:
        The session. Call ``close`` to stop the sandbox.
    """
    run(environment.__aenter__)
    return SyncSession(environment)


class SyncSession:
    """Drive a started environment through the sync ``SandboxSession`` protocol."""

    def __init__(self, environment: SandboxEnvironment) -> None:
        """Wrap a started environment.

        Args:
            environment: The started environment.
        """
        self.environment = environment

    def write_files(self, files: Mapping[str, str]) -> None:
        """Upload text files to the sandbox.

        Args:
            files: Maps each sandbox path to its text content.
        """
        run(self.environment.upload_files, files)

    def run(
        self,
        command: str,
        *,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        on_output: OutputSink | None = None,
        sudo: bool = False,
    ) -> ExecResult:
        """Run one shell command in the sandbox.

        Args:
            command: The shell command.
            env: Extra environment variables.
            timeout_seconds: The command timeout.
            on_output: Receives each stdout and stderr chunk. It runs on the portal thread.
            sudo: Run the command as root.

        Returns:
            The command result.
        """

        async def _exec() -> ExecResult:
            """Call ``exec`` with keyword arguments, which the portal cannot pass.

            Returns:
                The command result.
            """
            return await self.environment.exec(
                command, env=env, timeout_sec=timeout_seconds, on_output=on_output, sudo=sudo
            )

        return run(_exec)

    def read_file(self, path: str) -> str | None:
        """Download one text file from the sandbox.

        Args:
            path: The sandbox path.

        Returns:
            The content, or ``None`` when the file does not exist.
        """
        return run(self.environment.download_file, path)

    def close(self) -> None:
        """Stop the sandbox."""
        run(self.environment.stop)


def _serve_portal(ready: concurrent.futures.Future[BlockingPortal]) -> None:
    """Run the portal's event loop on the current thread until the portal stops.

    Args:
        ready: Receives the portal once it accepts calls.
    """

    async def _serve() -> None:
        """Open the portal, publish it and wait for the stop call."""
        async with BlockingPortal() as portal:
            ready.set_result(portal)
            await portal.sleep_until_stopped()

    try:
        anyio.run(_serve)
    except BaseException as exc:
        if not ready.done():
            ready.set_exception(exc)
        raise


def _reset_after_fork() -> None:
    """Forget the parent's portal in a forked child, which has no portal thread."""
    global _lock, _portal, _thread
    _lock = threading.Lock()
    _portal = _thread = None


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)
atexit.register(shutdown)

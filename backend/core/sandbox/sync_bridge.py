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
import logging
import os
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, TypeVar

import anyio
from anyio.from_thread import BlockingPortal

from .environment import ExecResult, OutputSink, SandboxEnvironment

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Bounds the stop call that releases a sandbox. It runs shielded, so a cancel
# cannot cut it short, and a stop that hangs must still let the caller go.
_STOP_TIMEOUT_SECONDS = 30.0
# Bounds shutdown, which atexit runs: a blocked loop thread must not hang exit.
_SHUTDOWN_TIMEOUT_SECONDS = 5.0

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
    """Stop the process portal and wait a bounded time for its thread.

    Running tasks are cancelled. A sandbox stop that runs shielded can keep
    the thread alive past the bound; then shutdown logs a warning and
    returns. A later call starts a new portal.
    """
    global _portal, _thread
    with _lock:
        portal, thread = _portal, _thread
        _portal = _thread = None
    if portal is None or thread is None or not thread.is_alive():
        return
    deadline = time.monotonic() + _SHUTDOWN_TIMEOUT_SECONDS
    # ``start_task_soon`` and ``call`` both wait without a timeout for the loop
    # to accept the task, so a blocked loop would block this thread forever.
    stopper = threading.Thread(target=_request_stop, args=(portal,), name="sandbox-sync-bridge-stop", daemon=True)
    stopper.start()
    stopper.join(timeout=_SHUTDOWN_TIMEOUT_SECONDS)
    thread.join(timeout=max(0.0, deadline - time.monotonic()))
    if thread.is_alive():
        logger.warning("The sandbox sync bridge did not stop within %s seconds.", _SHUTDOWN_TIMEOUT_SECONDS)


def open_session(environment: SandboxEnvironment) -> SyncSession:
    """Start an environment and wrap it in a sync session.

    The start runs ``start`` directly, not ``__aenter__``: when the start
    fails or is cancelled, the cleanup stop runs shielded and bounded.

    Args:
        environment: A stopped environment, for example from ``create_environment``.

    Returns:
        The session. Call ``close`` to stop the sandbox.
    """
    run(_start_environment, environment)
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
        """Stop the sandbox. The stop runs shielded, so an interrupt does not cut it short.

        Raises:
            TimeoutError: The stop did not end within the stop timeout.
        """
        run(_stop_environment, self.environment)


async def _start_environment(environment: SandboxEnvironment) -> None:
    """Start the sandbox; stop it again when the start fails or is cancelled.

    anyio cancels a cancelled task again at every await, so a plain cleanup
    ``stop`` would be cancelled too and leave a billable sandbox running.

    Args:
        environment: The stopped environment.
    """
    try:
        await environment.start()
    except BaseException:
        # A stop failure must not hide the start error that the caller needs.
        try:
            await _stop_environment(environment)
        except Exception:
            logger.exception("The sandbox did not stop after its start failed.")
        raise


async def _stop_environment(environment: SandboxEnvironment) -> None:
    """Stop the sandbox inside a shielded scope with a deadline.

    Args:
        environment: The environment to stop.

    Raises:
        TimeoutError: The stop did not end within the stop timeout.
    """
    with anyio.fail_after(_STOP_TIMEOUT_SECONDS, shield=True):
        await environment.stop()


def _request_stop(portal: BlockingPortal) -> None:
    """Ask the portal to stop and cancel its tasks, and wait until it stops.

    Args:
        portal: The portal to stop.
    """
    try:
        portal.call(portal.stop, True)
    except Exception:
        # The loop can end between the liveness check and this call.
        logger.debug("The sandbox sync bridge was already stopped.", exc_info=True)


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

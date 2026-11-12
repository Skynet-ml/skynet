"""Centralized mock builders for core/worker/tests/.

Division of responsibilities:
  - conftest.py  — FakeJobStore (stateful in-memory store, pytest fixture)
  - mocks.py     — stateless builder functions for external collaborators
                   (mp.Queue, mp.Process)

Usage example:
    from .mocks import fake_mp_process, REAL_RUN_PAYLOAD
"""

from __future__ import annotations

import queue
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from core.billing.model_gateway import _sandbox_failure_kind
from core.sandbox.errors import SandboxInfraError
from core.service_gateway.optimization.blackbox import remote_sandbox
from core.service_gateway.optimization.blackbox.sandbox import CommandResult
from core.worker import vercel_dspy
from core.worker.isolated_runner import SANDBOX_PROTOCOL

# A finished black-box run's result.
REAL_RESULT: dict = {"baseline_score": 0.5, "best_score": 0.75, "best_candidate": "Answer the question carefully."}

# A black-box run payload as engine._process_job receives it.
REAL_RUN_PAYLOAD: dict = {
    "name": "bb",
    "username": "demo",
    "seed_candidate": "Answer the question.",
    "objective": "Answer the question correctly.",
    "scorer": {"kind": "python", "metric_code": "def score(candidate, case=None): return 1.0"},
    "reflection_model_config": {"name": "openai/gpt-5.4-nano"},
    "_optimization_type": "blackbox",
}

_PROGRESS = {
    "event": "optimizer_progress",
    "metrics": {"tqdm_desc": "scorer runs", "tqdm_total": 12, "tqdm_n": 3, "tqdm_percent": 25.0},
    "timestamp": "2026-04-13T15:14:55.004686",
}


def make_progress_event(
    phase: str = _PROGRESS["event"],
    metrics: dict | None = None,
    timestamp: str = _PROGRESS["timestamp"],
) -> dict:
    """Build a synthetic ``EVENT_PROGRESS`` dict for tests.

    Args:
        phase: Phase label.
        metrics: Metric snapshot, defaults to a copy of a sample optimizer-progress event.
        timestamp: ISO 8601 timestamp string.

    Returns:
        Dict matching the ``EVENT_PROGRESS`` shape produced by the runner.
    """
    if metrics is None:
        metrics = dict(_PROGRESS["metrics"])
    return {
        "type": "progress",
        "event": phase,
        "metrics": metrics,
        "timestamp": timestamp,
    }


def make_result_event(result: dict | None = None) -> dict:
    """Build a synthetic ``EVENT_RESULT`` dict for tests.

    Args:
        result: Result payload; defaults to a copy of ``REAL_RESULT``.

    Returns:
        Dict matching the ``EVENT_RESULT`` shape produced by the runner.
    """
    return {"type": "result", "result": result if result is not None else dict(REAL_RESULT)}


def fake_mp_queue(*events: dict) -> MagicMock:
    """Return a MagicMock queue prefilled with ``events`` for ``get_nowait`` consumption.

    Args:
        *events: Events to enqueue, in order.

    Returns:
        A MagicMock with ``get_nowait``/``close``/``join_thread`` methods wired.
    """
    q: queue.Queue = queue.Queue()
    for event in events:
        q.put(event)

    mock_q = MagicMock()
    mock_q.get_nowait.side_effect = q.get_nowait
    mock_q.close.return_value = None
    mock_q.join_thread.return_value = None
    return mock_q


def fake_mp_process(
    exitcode: int = 0,
    is_alive: bool | list[bool] | None = None,
) -> MagicMock:
    """Return a MagicMock process with configurable ``exitcode`` and ``is_alive``.

    Args:
        exitcode: Value placed on ``proc.exitcode``.
        is_alive: ``list[bool]`` consumed as side-effect, or single ``bool``.

    Returns:
        A MagicMock standing in for ``mp.Process``.
    """
    proc = MagicMock()
    proc.exitcode = exitcode
    if isinstance(is_alive, list):
        proc.is_alive.side_effect = is_alive
    else:
        proc.is_alive.return_value = False if is_alive is None else is_alive
    return proc


def make_mp_context(exitcode: int = 0, result_events: list | None = None) -> tuple[MagicMock, MagicMock]:
    """Build a fake mp context whose Queue is preloaded and Process is already dead.

    Args:
        exitcode: Exit code to expose on the fake process.
        result_events: Events to seed onto the fake queue.

    Returns:
        ``(ctx, proc)`` mocks; pass ``ctx`` to :func:`protect_dispatch`.
    """
    result_events = result_events or []
    proc = fake_mp_process(exitcode=exitcode)
    mq = fake_mp_queue(*result_events)

    ctx = MagicMock()
    ctx.Queue.return_value = mq
    ctx.Process.return_value = proc
    ctx.get_start_method.return_value = "spawn"
    return ctx, proc


class FakeGateway:
    """Retain the protected payload without starting real parent transports."""

    def __init__(self, runtime: Any, **_kwargs: Any) -> None:
        """Store the budget runtime supplied by the worker."""
        self.runtime = runtime

    def protect_payload(self, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        """Return the same authored source for assertion at the sandbox boundary."""
        return {**payload, "_protected_fixture": True}

    def cost_ceiling_cents(self) -> int:
        """Report a fixed ledger allowance for a payload without a ceiling."""
        return 100

    def detach_sandbox(self) -> None:
        """Report that no sandbox broker is bound to this fixture."""

    def close(self) -> None:
        """Close the transport fixture without external resources."""


def protect_dispatch(store: Any, context: MagicMock) -> None:
    """Send every seeded job down the funded sandbox path with fake transports.

    The worker refuses a job without a budget, so a dispatch test binds each
    seeded row to one. The conftest stops the started patches after each test.

    Args:
        store: The ``FakeJobStore`` whose seeded rows get a budget binding.
        context: The fake spawn context from :func:`make_mp_context`.
    """
    store.engine = object()
    for job in store._jobs.values():
        job.setdefault("execution_budget_id", "budget")
        job.setdefault("execution_budget_generation", 0)
    patch("core.worker.engine.bind_protected_sandbox").start()
    patch("core.worker.engine.payload_uses_token_source", return_value=False).start()
    patch("core.worker.engine.ModelGateway", FakeGateway).start()
    patch("core.worker.engine.mp.get_context", return_value=context).start()
    patch("core.worker.engine.BackgroundWorker._close_budget_gateway", return_value=({}, None)).start()


SESSION_STOPPED = "The Vercel sandbox session stopped: SandboxApiError"


class StoppedSession:
    """Pass the protocol probe, then lose the box under the optimizer command after ``elapsed`` seconds."""

    def __init__(self, clock: list[float], elapsed: float) -> None:
        """Keep the shared fake clock and the time the box stops.

        Args:
            clock: One-item list that holds the fake monotonic time.
            elapsed: Seconds after the box opened at which the session stops.
        """
        self.files: dict[str, str] = {}
        self.closed = False
        self.clock = clock
        self.elapsed = elapsed

    def run(self, command: str, **kwargs: Any) -> CommandResult:
        """Answer the probe, then raise the error frame the parent sends for a stopped session.

        Args:
            command: The probe or the optimizer command.
            **kwargs: The run options, which this fake ignores.

        Returns:
            The probe answer. The optimizer command never returns.

        Raises:
            ServiceError: The guest-side error for the stopped session.
        """
        if command == vercel_dspy.PROTOCOL_PROBE:
            return CommandResult(0, stdout=f"{SANDBOX_PROTOCOL}\n")
        self.clock[0] += self.elapsed
        stopped = SandboxInfraError(SESSION_STOPPED)
        remote_sandbox._raise_remote_error(
            {
                "type": "error",
                "error_type": type(stopped).__name__,
                "failure_kind": _sandbox_failure_kind(stopped),
                "message": str(stopped),
            }
        )
        raise AssertionError("unreachable")

    def write_files(self, files: dict[str, str]) -> None:
        """Record the staged request.

        Args:
            files: Box paths and their text.
        """
        self.files.update(files)

    def close(self) -> None:
        """Record that the worker closed the box."""
        self.closed = True


def stopped_box_event(monkeypatch: pytest.MonkeyPatch, *, lifetime: float, elapsed: float) -> dict[str, Any]:
    """Run the guest box until its session stops and return the error event the worker gets.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        lifetime: The guest box lifetime in the descriptor (the run deadline).
        elapsed: Seconds after the box opened at which the session stops.

    Returns:
        The single error event on the worker queue.
    """
    clock = [1000.0]
    session = StoppedSession(clock, elapsed)
    monkeypatch.setattr(vercel_dspy, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(vercel_dspy, "RemoteSandboxRuntime", lambda *_args: SimpleNamespace(open=lambda _spec: session))
    events: queue.Queue = queue.Queue()
    vercel_dspy.run_vercel_dspy(
        {
            "_budget_gateway_descriptor": {
                "url": "http://127.0.0.1:9876",
                "control_token": "control",
                "image": "backend@sha256:" + "a" * 64,
                "lifetime_seconds": lifetime,
            },
        },
        "job-deadline",
        events,
        "spawn",
    )
    assert session.closed
    event = events.get_nowait()
    assert events.empty()
    return event

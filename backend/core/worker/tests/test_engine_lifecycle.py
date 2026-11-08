"""Unit tests for core.worker.engine — lifecycle methods skipped by the prior agent.

Methods tested (no real subprocesses, no real multiprocessing.Queue):
  BackgroundWorker._drain_subprocess_events — drain loop against a fixed-event fake queue
  BackgroundWorker._terminate_run_process   — cancel-flag path and hard-kill timeout path
  BackgroundWorker.submit_job               — payload stored, job enqueued
  BackgroundWorker.dump_thread_stacks       — returns a string without raising
  get_worker                                — singleton contract, pending IDs enqueued
  reset_worker_for_tests                    — clears the module-level singleton
  BackgroundWorker._process_job             — run_completed / run_failed telemetry facts
  BackgroundWorker._sandbox_sweep_loop      — a failed sweep does not end the sweeper

Methods intentionally NOT tested here:
  start() / _worker_loop()                  — require real threads + real mp.Process
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import MagicMock, call, patch

import pytest

import core.worker.engine as engine_module
from core.exceptions import INFRASTRUCTURE_INTERRUPTION
from core.storage.base import JobStore
from core.worker.constants import (
    EVENT_AGENT_RUN,
    EVENT_ERROR,
    EVENT_LOG,
    EVENT_PROGRESS,
    EVENT_RESULT,
)
from core.worker.engine import BackgroundWorker, get_worker, reset_worker_for_tests

from .conftest import FakeJobStore
from .mocks import REAL_RUN_PAYLOAD, fake_mp_process, make_mp_context


@pytest.fixture(autouse=True)
def _reset_global_worker() -> Iterator[None]:
    """Reset the module-level singleton before and after each test."""
    reset_worker_for_tests()
    yield
    reset_worker_for_tests()


@pytest.fixture
def store() -> FakeJobStore:
    """Yield a fresh in-memory job store for a test."""
    return FakeJobStore()


@pytest.fixture
def worker(store: FakeJobStore) -> BackgroundWorker:
    """Build an unstarted BackgroundWorker bound to the test store."""
    return BackgroundWorker(
        job_store=cast(JobStore, store),
        num_workers=2,
        poll_interval=1.0,
        _allow_unprotected_test_execution=True,
    )


def _make_fake_queue(*events: dict) -> queue.Queue:
    """Build a real ``queue.Queue`` prefilled with the given events."""
    q: queue.Queue = queue.Queue()
    for event in events:
        q.put(event)
    return q


def test_drain_returns_none_result_and_none_error_for_empty_queue(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """An empty queue yields ``(None, None, 0)``."""
    store.seed_job("opt-1")
    q = _make_fake_queue()

    result, error, drained = worker._drain_subprocess_events("opt-1", q)

    assert result is None
    assert error is None
    assert drained == 0


def test_drain_routes_progress_event_to_record_progress(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """A progress event is forwarded to ``record_progress``."""
    store.seed_job("opt-1")
    q = _make_fake_queue({"type": EVENT_PROGRESS, "event": "iter_done", "metrics": {"score": 0.8}})

    worker._drain_subprocess_events("opt-1", q)

    assert len(store.record_progress_calls) == 1
    opt_id, evt_name, metrics = store.record_progress_calls[0]
    assert opt_id == "opt-1"
    assert evt_name == "iter_done"
    assert metrics == {"score": 0.8}


def test_drain_routes_agent_run_rows_and_deltas_to_the_store(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """A run row is saved, a transcript delta appended, and a malformed run ignored."""
    store.seed_job("opt-1")
    q = _make_fake_queue(
        {"type": EVENT_AGENT_RUN, "run": {"run_id": 1, "status": "running", "transcript": ""}},
        {"type": EVENT_AGENT_RUN, "run": {"run_id": 1, "transcript_delta": "hi\n"}},
        {"type": EVENT_AGENT_RUN, "run": {"run_id": "bogus"}},
        {"type": EVENT_AGENT_RUN, "run": None},
    )

    _result, error, drained = worker._drain_subprocess_events("opt-1", q)

    assert error is None
    assert drained == 4
    assert store.agent_run_calls == [
        ("opt-1", "save", {"run_id": 1, "status": "running", "transcript": ""}),
        ("opt-1", "append", (1, "hi\n")),
    ]


def test_drain_routes_log_event_to_append_log(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """A log event is forwarded to ``append_log`` with parsed fields."""
    store.seed_job("opt-1")
    q = _make_fake_queue(
        {
            "type": EVENT_LOG,
            "timestamp": "2024-06-01T12:00:00+00:00",
            "level": "WARNING",
            "logger": "dspy.core",
            "message": "something happened",
        }
    )

    worker._drain_subprocess_events("opt-1", q)

    assert len(store.append_log_calls) == 1
    entry = store.append_log_calls[0]
    assert entry["level"] == "WARNING"
    assert entry["logger_name"] == "dspy.core"
    assert entry["message"] == "something happened"
    assert entry["timestamp"] is not None


def test_drain_parses_log_timestamp_to_datetime(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """ISO timestamp strings are parsed to UTC-aware ``datetime`` values."""
    store.seed_job("opt-1")
    q = _make_fake_queue(
        {
            "type": EVENT_LOG,
            "timestamp": "2024-01-01T00:00:00+00:00",
            "level": "INFO",
            "logger": "dspy",
            "message": "ts test",
        }
    )

    worker._drain_subprocess_events("opt-1", q)

    ts = store.append_log_calls[0]["timestamp"]
    assert isinstance(ts, datetime)
    assert ts.tzinfo is not None
    assert ts == datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)


def test_drain_sets_log_timestamp_to_none_on_invalid_timestamp_string(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """An unparseable timestamp string falls back to ``None``."""
    store.seed_job("opt-1")
    q = _make_fake_queue(
        {
            "type": EVENT_LOG,
            "timestamp": "not-a-date",
            "level": "INFO",
            "logger": "dspy",
            "message": "bad ts",
        }
    )

    worker._drain_subprocess_events("opt-1", q)

    assert store.append_log_calls[0]["timestamp"] is None


def test_drain_returns_result_payload_from_result_event(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """A ``result`` event surfaces its payload from the drain call."""
    store.seed_job("opt-1")
    payload = {"baseline_test_metric": 0.5, "optimized_test_metric": 0.7}
    q = _make_fake_queue({"type": EVENT_RESULT, "result": payload})

    result, error, _ = worker._drain_subprocess_events("opt-1", q)

    assert result == payload
    assert error is None


def test_drain_ignores_result_event_when_result_is_not_dict(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """A non-dict ``result`` payload is ignored."""
    store.seed_job("opt-1")
    q = _make_fake_queue({"type": EVENT_RESULT, "result": "not-a-dict"})

    result, _error, _ = worker._drain_subprocess_events("opt-1", q)

    assert result is None


def test_drain_returns_error_payload_from_error_event(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """An ``error`` event surfaces its payload from the drain call."""
    store.seed_job("opt-1")
    q = _make_fake_queue({"type": EVENT_ERROR, "error": "boom", "traceback": "Traceback..."})

    result, error, _ = worker._drain_subprocess_events("opt-1", q)

    assert result is None
    assert error is not None
    assert error["error"] == "boom"
    assert error["traceback"] == "Traceback..."


def test_drain_processes_all_events_in_a_mixed_sequence(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """Drain handles a mixed sequence of progress/log/result events."""
    store.seed_job("opt-1")
    q = _make_fake_queue(
        {"type": EVENT_PROGRESS, "event": "step_a", "metrics": {}},
        {"type": EVENT_LOG, "timestamp": None, "level": "INFO", "logger": "dspy", "message": "log line"},
        {"type": EVENT_RESULT, "result": {"val": 99}},
    )

    result, error, drained = worker._drain_subprocess_events("opt-1", q)

    assert result == {"val": 99}
    assert error is None
    assert drained == 3
    assert len(store.record_progress_calls) == 1
    assert len(store.append_log_calls) == 1


def test_drain_last_result_event_wins_when_multiple_present(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """The most recently seen result payload is the one returned."""
    store.seed_job("opt-1")
    q = _make_fake_queue(
        {"type": EVENT_RESULT, "result": {"val": 1}},
        {"type": EVENT_RESULT, "result": {"val": 2}},
    )

    result, _, _ = worker._drain_subprocess_events("opt-1", q)

    assert result == {"val": 2}


def test_drain_silently_ignores_unknown_event_types(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """Drain silently ignores events of unknown ``type``."""
    store.seed_job("opt-1")
    q = _make_fake_queue({"type": "totally_unknown", "data": "ignored"})

    result, error, _ = worker._drain_subprocess_events("opt-1", q)

    assert result is None
    assert error is None
    assert store.record_progress_calls == []
    assert store.append_log_calls == []


def test_drain_swallows_record_progress_store_error(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """Drain does not propagate exceptions from ``record_progress``."""
    store.seed_job("opt-1")
    store.record_progress = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("DB error"))  # type: ignore[method-assign]
    q = _make_fake_queue({"type": EVENT_PROGRESS, "event": "e", "metrics": {}})

    # Must not raise.
    worker._drain_subprocess_events("opt-1", q)


def test_drain_swallows_append_log_store_error(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """Drain does not propagate exceptions from ``append_log``."""
    store.seed_job("opt-1")
    store.append_log = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("DB error"))  # type: ignore[method-assign]
    q = _make_fake_queue({"type": EVENT_LOG, "timestamp": None, "level": "INFO", "logger": "dspy", "message": "hi"})

    # Must not raise.
    worker._drain_subprocess_events("opt-1", q)


def test_terminate_run_process_calls_terminate_then_join(
    worker: BackgroundWorker,
) -> None:
    """``_terminate_run_process`` issues terminate + join on a graceful exit."""
    # is_alive() is called twice: once before kill check, once for final log.
    # Return False on first call → skip kill; False on second call → log "terminated".
    proc = fake_mp_process(is_alive=[False, False])

    worker._terminate_run_process(proc, "opt-1")

    proc.terminate.assert_called_once()
    assert any(c == call(timeout=3.0) for c in proc.join.call_args_list)


def test_terminate_run_process_calls_kill_when_process_survives_terminate(
    worker: BackgroundWorker,
) -> None:
    """``_terminate_run_process`` escalates to ``kill`` if the process is still alive."""
    # Still alive at first is_alive() check (before kill); dead after kill's join.
    proc = fake_mp_process(is_alive=[True, False])

    worker._terminate_run_process(proc, "opt-1")

    proc.kill.assert_called_once()


def test_terminate_run_process_does_not_call_kill_when_terminate_succeeds(
    worker: BackgroundWorker,
) -> None:
    """``_terminate_run_process`` skips ``kill`` when terminate succeeds."""
    # Return False on the first is_alive() — process already dead after terminate+join.
    proc = fake_mp_process(is_alive=[False, False])

    worker._terminate_run_process(proc, "opt-1")

    proc.kill.assert_not_called()


def test_terminate_run_process_does_not_raise_when_stuck_process_never_dies(
    worker: BackgroundWorker,
) -> None:
    """``_terminate_run_process`` does not raise when the process never dies."""
    proc = fake_mp_process(is_alive=True)

    # Should not raise.
    worker._terminate_run_process(proc, "opt-stuck")

    proc.terminate.assert_called_once()
    proc.kill.assert_called_once()


def test_terminate_run_process_skips_kill_when_process_has_no_kill_method(
    worker: BackgroundWorker,
) -> None:
    """``_terminate_run_process`` skips ``kill`` when the process lacks the method."""
    proc = MagicMock(spec=["terminate", "join", "is_alive"])
    # still alive after terminate
    proc.is_alive.side_effect = [True, True, False]

    # MagicMock with spec= won't have 'kill', so hasattr returns False.
    # _terminate_run_process uses hasattr(run_process, 'kill'), so no kill call.
    worker._terminate_run_process(proc, "opt-1")

    assert not hasattr(proc, "kill")


def test_submit_job_stores_payload_in_job_store(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """``submit_job`` writes the dumped payload onto the job row."""
    store.seed_job("opt-1")
    payload = MagicMock()
    payload.model_dump.return_value = {"key": "value"}

    worker.submit_job("opt-1", payload)

    assert store._jobs["opt-1"]["payload"] == {"key": "value"}


def test_submit_job_calls_model_dump_with_json_mode_and_alias(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """``submit_job`` serialises with ``mode="json"`` and ``by_alias=True``."""
    store.seed_job("opt-1")
    payload = MagicMock()
    payload.model_dump.return_value = {}

    worker.submit_job("opt-1", payload)

    payload.model_dump.assert_called_once_with(mode="json", by_alias=True)


def test_submit_job_leaves_row_pending_for_db_claim(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """``submit_job`` leaves the row pending so any pod's worker can claim it.

    The DB-backed claim queue replaces the old in-memory enqueue: the next
    worker tick — on this pod or a peer — picks the job up via
    ``claim_next_job``.
    """
    store.seed_job("opt-1")
    payload = MagicMock()
    payload.model_dump.return_value = {}

    pre_status = store._jobs["opt-1"]["status"]

    worker.submit_job("opt-1", payload)

    # Status is unchanged: ``submit_job`` only writes the payload — it does
    # not transition to ``running``/``validating``. Pickup happens later on
    # the next worker tick via ``claim_next_job``.
    assert store._jobs["opt-1"]["status"] == pre_status
    # The local hint queue is intentionally not used by ``submit_job`` so two
    # pods can balance load via ``claim_next_job``.
    assert "opt-1" not in worker._pending_jobs


def test_submit_job_creates_cancel_event(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """``submit_job`` registers a ``threading.Event`` as the cancel signal."""
    store.seed_job("opt-1")
    payload = MagicMock()
    payload.model_dump.return_value = {}

    worker.submit_job("opt-1", payload)

    assert "opt-1" in worker._cancel_events
    assert isinstance(worker._cancel_events["opt-1"], threading.Event)


def test_dump_thread_stacks_returns_string_without_raising(
    worker: BackgroundWorker,
) -> None:
    """``dump_thread_stacks`` returns a string and never raises."""
    result = worker.dump_thread_stacks()

    assert isinstance(result, str)


def test_dump_thread_stacks_returns_empty_string_when_no_threads_started(
    worker: BackgroundWorker,
) -> None:
    """An unstarted worker produces an empty thread dump."""
    result = worker.dump_thread_stacks()

    assert result == ""


def test_dump_thread_stacks_includes_thread_name_when_threads_are_running() -> None:
    """A running worker includes its thread names in the dump."""
    store = FakeJobStore()
    w = BackgroundWorker(
        job_store=cast(JobStore, store),
        num_workers=1,
        poll_interval=0.05,
        _allow_unprotected_test_execution=True,
    )
    w.start()
    try:
        result = w.dump_thread_stacks()
        assert "dspy-worker-0" in result
    finally:
        w.stop(timeout=2.0)


def test_get_worker_returns_a_background_worker_instance(
    store: FakeJobStore,
) -> None:
    """``get_worker`` returns a ``BackgroundWorker`` singleton."""
    w = get_worker(cast(JobStore, store))

    assert isinstance(w, BackgroundWorker)
    w.stop(timeout=2.0)


def test_get_worker_returns_same_instance_on_repeated_calls(
    store: FakeJobStore,
) -> None:
    """Repeated ``get_worker`` calls return the same singleton."""
    w1 = get_worker(cast(JobStore, store))
    w2 = get_worker(cast(JobStore, store))

    assert w1 is w2
    w1.stop(timeout=2.0)


def test_get_worker_enqueues_pending_optimization_ids(
    store: FakeJobStore,
) -> None:
    """``get_worker`` re-enqueues IDs passed via ``pending_optimization_ids``."""
    w = get_worker(cast(JobStore, store), pending_optimization_ids=["opt-a", "opt-b"])

    assert "opt-a" in w._pending_jobs or "opt-a" in w._processing_jobs or "opt-a" in w._cancel_events
    w.stop(timeout=2.0)


def test_reset_worker_for_tests_clears_module_global(
    store: FakeJobStore,
) -> None:
    """``reset_worker_for_tests`` clears the module-level singleton."""
    get_worker(cast(JobStore, store))

    reset_worker_for_tests()

    assert engine_module._worker is None


def test_reset_worker_for_tests_is_idempotent_when_no_worker_exists() -> None:
    """Calling ``reset_worker_for_tests`` twice does not raise."""
    reset_worker_for_tests()
    reset_worker_for_tests()  # second call — still should not raise


def test_get_worker_creates_new_instance_after_reset(
    store: FakeJobStore,
) -> None:
    """``get_worker`` returns a fresh instance after a reset."""
    w1 = get_worker(cast(JobStore, store))
    w1.stop(timeout=2.0)
    reset_worker_for_tests()

    w2 = get_worker(cast(JobStore, store))

    assert w2 is not w1
    w2.stop(timeout=2.0)


def test_init_falls_back_to_1_0_when_cancel_poll_interval_is_not_a_float(
    store: FakeJobStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Init falls back to ``1.0`` when ``cancel_poll_interval`` cannot be parsed."""

    # Patch settings on the engine module so ``str(settings.cancel_poll_interval)``
    # returns a value that ``float()`` cannot parse — exercising the
    # ``except ValueError`` fallback in ``BackgroundWorker.__init__``.
    class _FakeSettings:
        cancel_poll_interval = "not-a-float"
        job_run_start_method = "fork"

    monkeypatch.setattr(engine_module, "settings", _FakeSettings())

    # Must not raise; must use 1.0 as the fallback.
    w = BackgroundWorker(
        job_store=cast(JobStore, store),
        num_workers=1,
        poll_interval=1.0,
        _allow_unprotected_test_execution=True,
    )
    assert w._cancel_poll_interval == 1.0


def test_resolve_mp_context_raises_and_falls_back_on_bogus_method(
    store: FakeJobStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_resolve_mp_context`` falls back to a default context for bad methods."""

    class _FakeSettings:
        cancel_poll_interval = 1.0
        job_run_start_method = "bogus_method"

    monkeypatch.setattr(engine_module, "settings", _FakeSettings())

    # mp.get_context("bogus_method") raises ValueError.
    ctx = BackgroundWorker._resolve_mp_context()
    # The fallback context must be valid (non-None) and support get_start_method().
    assert ctx is not None
    assert ctx.get_start_method() in ("fork", "spawn", "forkserver")


def test_resolve_mp_context_emits_warning_for_bogus_method(
    store: FakeJobStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``_resolve_mp_context`` warns when an unknown start method is configured."""

    class _FakeSettings:
        cancel_poll_interval = 1.0
        job_run_start_method = "definitely_invalid"

    monkeypatch.setattr(engine_module, "settings", _FakeSettings())

    with caplog.at_level("WARNING", logger="core.worker.engine"):
        BackgroundWorker._resolve_mp_context()

    assert any("definitely_invalid" in msg for msg in caplog.messages)


def test_resolve_mp_context_emits_warning_for_non_fork_method(
    store: FakeJobStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``_resolve_mp_context`` warns when a non-fork method is configured."""

    class _FakeSettings:
        cancel_poll_interval = 1.0
        job_run_start_method = "spawn"

    monkeypatch.setattr(engine_module, "settings", _FakeSettings())

    with caplog.at_level("WARNING", logger="core.worker.engine"):
        BackgroundWorker._resolve_mp_context()

    # spawn is a valid method — the second warning about registry callables fires.
    assert any("spawn" in msg for msg in caplog.messages)


def test_stop_sets_all_cancel_events(store: FakeJobStore) -> None:
    """stop() must set every cancel event so running jobs can detect shutdown promptly."""
    w = BackgroundWorker(
        job_store=cast(JobStore, store),
        num_workers=1,
        poll_interval=0.05,
        _allow_unprotected_test_execution=True,
    )
    w.start()
    try:
        # Register several cancel events manually (as submit_job / enqueue_job would).
        event_a = threading.Event()
        event_b = threading.Event()
        event_c = threading.Event()
        with w._queue_lock:
            w._cancel_events["job-a"] = event_a
            w._cancel_events["job-b"] = event_b
            w._cancel_events["job-c"] = event_c

        w.stop(timeout=2.0)

        assert event_a.is_set(), "event_a must be set after stop()"
        assert event_b.is_set(), "event_b must be set after stop()"
        assert event_c.is_set(), "event_c must be set after stop()"
    except Exception:
        w.stop(timeout=1.0)
        raise


def test_a_failed_sweep_does_not_end_the_sweeper(worker: BackgroundWorker, monkeypatch: pytest.MonkeyPatch) -> None:
    """The sweeper logs a sweep error, sweeps again on the next tick, and ends only on stop."""
    calls: list[int] = []
    swept_again = threading.Event()

    def _sweep() -> int:
        """Fail on the first tick and signal the second."""
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database down")
        swept_again.set()
        return 0

    monkeypatch.setattr(engine_module, "_SANDBOX_SWEEP_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(worker, "_sweep_terminal_job_sandboxes", _sweep)
    stop_event = threading.Event()
    sweeper = threading.Thread(target=worker._sandbox_sweep_loop, args=(stop_event,), daemon=True)
    sweeper.start()
    try:
        assert swept_again.wait(5.0)
        assert sweeper.is_alive()
    finally:
        stop_event.set()
        sweeper.join(timeout=5.0)
    assert not sweeper.is_alive()
    assert len(calls) >= 2


def test_renewing_lease_touches_activity_while_a_blocking_step_runs(
    worker: BackgroundWorker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A step that outlasts the lease window keeps renewing the lease until it exits."""
    touches: list[int] = []
    monkeypatch.setattr(worker, "_lease_seconds", 0.06)
    monkeypatch.setattr(worker, "_touch_activity", touches.append)

    with worker._renewing_lease(1):
        threading.Event().wait(0.2)
        renewed_during_step = len(touches)

    assert renewed_during_step >= 3
    assert len(touches) == renewed_during_step + 1
    assert set(touches) == {1}


_SAMPLE_ROWS = [{"question": "Capital of France?", "answer": "Paris"}, {"question": "2 + 2?", "answer": "4"}]
_OUTCOME_OVERVIEW = {"optimization_type": "run", "username": "alice", "optimizer_name": "gepa"}


def _run_outcome_event(
    worker: BackgroundWorker,
    store: FakeJobStore,
    events: list[dict[str, Any]],
    *,
    dataset: list[dict[str, Any]] | None = None,
    job_id: str = "opt-outcome",
) -> tuple[str, dict[str, Any]]:
    """Run one mocked job and return its outcome event's name and properties.

    Args:
        worker: Worker under test.
        store: In-memory store the job is seeded into.
        events: Events the fake optimizer process emits.
        dataset: Dataset rows for the stored payload; the default payload's otherwise.
        job_id: Id of the seeded job.

    Returns:
        The recorded event name and its properties.
    """
    payload = {**REAL_RUN_PAYLOAD, "dataset": dataset or REAL_RUN_PAYLOAD["dataset"]}
    store.seed_job(job_id, payload=payload, payload_overview=_OUTCOME_OVERVIEW)
    worker.enqueue_job(job_id)
    ctx, _proc = make_mp_context(exitcode=0, result_events=events)
    worker._mp_ctx = ctx
    worker._mp_start_method = "spawn"
    with (
        patch.object(engine_module, "SAMPLE_DATASETS", {"qa": {"rows": _SAMPLE_ROWS}}),
        patch.object(engine_module, "notify_job_completed"),
        patch.object(engine_module, "record_server_event") as record,
        patch.object(worker, "_get_service"),
    ):
        worker._process_job(job_id, 0)
    record.assert_called_once()
    return record.call_args.kwargs["name"], record.call_args.kwargs["properties"]


def test_run_completed_carries_lift_held_out_and_sample_flag(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """A finished run on a bundled sample reports its scores, held-out facts and from_sample."""
    result = {
        "baseline_test_metric": 0.5,
        "optimized_test_metric": 0.8,
        "split_counts": {"train": 10, "val": 5, "test": 24},
    }
    name, props = _run_outcome_event(worker, store, [{"type": EVENT_RESULT, "result": result}], dataset=_SAMPLE_ROWS)

    assert name == "run_completed"
    assert props["baseline"] == 0.5
    assert props["optimized"] == 0.8
    assert props["lift"] == pytest.approx(0.3)
    assert props["held_out"] is True
    assert props["held_out_rows"] == 24
    assert props["from_sample"] is True


def test_run_completed_from_upload_without_scores(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """A user upload is not a sample, and a result without scores reports a null lift."""
    name, props = _run_outcome_event(worker, store, [{"type": EVENT_RESULT, "result": {"module_name": "m"}}])

    assert name == "run_completed"
    assert props["from_sample"] is False
    assert props["lift"] is None


def test_run_failed_separates_sandbox_and_optimizer_errors(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """Sandbox and optimizer failures get different stages and a class name, never the error text."""
    _, sandbox = _run_outcome_event(
        worker,
        store,
        [
            {
                "type": EVENT_ERROR,
                "error": "sandbox lost secret-path",
                "error_type": "SandboxGone",
                "failure_kind": INFRASTRUCTURE_INTERRUPTION,
            }
        ],
    )
    _, optimizer = _run_outcome_event(
        worker,
        store,
        [{"type": EVENT_ERROR, "error": "metric blew up on private-row", "error_type": "ZeroDivisionError"}],
        job_id="opt-optimizer-error",
    )

    assert sandbox["failure_stage"] == "sandbox"
    assert sandbox["error_class"] == "InfrastructureInterruptionError"
    assert optimizer["failure_stage"] == "optimize"
    assert optimizer["error_class"] == "ZeroDivisionError"
    for props in (sandbox, optimizer):
        assert not any("secret" in str(value) or "private" in str(value) for value in props.values())


def test_run_failed_before_dispatch_is_preflight(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """A job that fails before reaching the sandbox reports the preflight stage."""
    store.seed_job("opt-empty", payload=None, payload_overview=_OUTCOME_OVERVIEW)
    worker.enqueue_job("opt-empty")
    with (
        patch.object(engine_module, "notify_job_completed"),
        patch.object(engine_module, "record_server_event") as record,
    ):
        worker._process_job("opt-empty", 0)

    assert record.call_args.kwargs["properties"]["failure_stage"] == "preflight"
    assert record.call_args.kwargs["properties"]["error_class"] == "ValueError"


def test_outcome_helper_raising_never_changes_job_status(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """A crash in the run-outcome helper is swallowed and the run still succeeds."""
    store.seed_job("opt-helper", payload=dict(REAL_RUN_PAYLOAD), payload_overview=_OUTCOME_OVERVIEW)
    worker.enqueue_job("opt-helper")
    ctx, _proc = make_mp_context(exitcode=0, result_events=[{"type": EVENT_RESULT, "result": {"best_score": 1}}])
    worker._mp_ctx = ctx
    worker._mp_start_method = "spawn"
    with (
        patch.object(engine_module, "completed_properties", side_effect=RuntimeError("helper bug")),
        patch.object(engine_module, "notify_job_completed"),
        patch.object(engine_module, "record_server_event") as record,
        patch.object(worker, "_get_service"),
    ):
        worker._process_job("opt-helper", 0)

    assert store._jobs["opt-helper"]["status"] == "success"
    record.assert_not_called()

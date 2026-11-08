"""Unit tests for BackgroundWorker._process_job — error branches.

All subprocess/multiprocessing calls are mocked; no real child processes
are spawned.  FakeJobStore from conftest is used as the in-memory store.
"""

from __future__ import annotations

import contextlib
import copy
import inspect
import itertools
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from core.api.errors import DomainError
from core.billing.vercel_usage import PROPOSER_REGISTRY_HOSTS
from core.exceptions import DETERMINISTIC_FAILURE, INFRASTRUCTURE_INTERRUPTION, ServiceError
from core.i18n import CANCELLATION_REASON
from core.models.blackbox import BlackboxRunRequest
from core.service_gateway.optimization.blackbox.repo_tree import REPO_SNAPSHOT_KEY
from core.storage.base import JobStore

from .. import engine as engine_module
from ..checkpoint_compat import CheckpointCompatibilityError, checkpoint_manifest, validate_checkpoint
from ..constants import EVENT_ERROR, EVENT_RESULT
from ..engine import BackgroundWorker, WorkerShutdownError, reset_worker_for_tests
from .conftest import FakeJobStore
from .mocks import (
    REAL_RUN_PAYLOAD,
    REAL_UNIFIED_PAYLOAD,
    fake_mp_process,
    fake_mp_queue,
    make_mp_context,
    make_progress_event,
    make_result_event,
)
from .test_budget_lifecycle import _checkpoint


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
        num_workers=1,
        poll_interval=1.0,
        _allow_unprotected_test_execution=True,
    )


def test_process_job_raises_when_payload_is_none(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """A ``None`` payload causes the job to be marked failed."""
    store.seed_job("opt-1", payload=None)
    worker.enqueue_job("opt-1")

    with patch("core.worker.engine.notify_job_completed"):
        worker._process_job("opt-1", 0)  # must not raise — error handled internally

    assert store._jobs["opt-1"]["status"] == "failed"


def test_process_job_raises_when_payload_is_empty_dict(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """An empty-dict payload causes the job to be marked failed."""
    store.seed_job("opt-1", payload={})
    worker.enqueue_job("opt-1")

    with patch("core.worker.engine.notify_job_completed"):
        worker._process_job("opt-1", 0)  # must not raise — error handled internally

    assert store._jobs["opt-1"]["status"] == "failed"


def test_process_job_parses_json_string_payload_overview(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """A JSON-string ``payload_overview`` is parsed and the job runs to success."""
    overview_str = json.dumps({"optimization_type": "run", "username": "alice"})
    store.seed_job(
        "opt-2",
        payload=REAL_RUN_PAYLOAD,
        payload_overview=overview_str,
    )
    worker.enqueue_job("opt-2")

    ctx, _proc = make_mp_context(
        exitcode=0,
        result_events=[{"type": EVENT_RESULT, "result": {"baseline_test_metric": 0.5, "optimized_test_metric": 0.7}}],
    )
    worker._mp_ctx = ctx
    worker._mp_start_method = "spawn"

    with patch("core.worker.engine.notify_job_completed"), patch.object(worker, "_get_service") as mock_svc:
        mock_svc.return_value.validate_payload = MagicMock()
        worker._process_job("opt-2", 0)

    # If the JSON-string branch worked, job succeeded (not failed due to bad overview).
    assert store._jobs["opt-2"]["status"] == "success"


def test_process_job_handles_invalid_json_string_payload_overview_gracefully(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """An unparseable ``payload_overview`` does not break the run."""
    store.seed_job(
        "opt-3",
        payload=REAL_RUN_PAYLOAD,
        payload_overview="not-json-at-all",
    )
    worker.enqueue_job("opt-3")

    ctx, _proc = make_mp_context(
        exitcode=0,
        result_events=[{"type": EVENT_RESULT, "result": {"baseline_test_metric": 0.5, "optimized_test_metric": 0.7}}],
    )
    worker._mp_ctx = ctx
    worker._mp_start_method = "spawn"

    with patch("core.worker.engine.notify_job_completed"), patch.object(worker, "_get_service") as mock_svc:
        mock_svc.return_value.validate_payload = MagicMock()
        worker._process_job("opt-3", 0)

    assert store._jobs["opt-3"]["status"] == "success"


def test_process_job_handles_non_dict_json_payload_overview_gracefully(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """A JSON payload overview that is not an object falls back to defaults."""
    store.seed_job(
        "opt-3b",
        payload=REAL_RUN_PAYLOAD,
        payload_overview="[]",
    )
    worker.enqueue_job("opt-3b")

    ctx, _proc = make_mp_context(
        exitcode=0,
        result_events=[{"type": EVENT_RESULT, "result": {"baseline_test_metric": 0.5, "optimized_test_metric": 0.7}}],
    )
    worker._mp_ctx = ctx
    worker._mp_start_method = "spawn"

    with patch("core.worker.engine.notify_job_completed"), patch.object(worker, "_get_service") as mock_svc:
        mock_svc.return_value.validate_payload = MagicMock()
        worker._process_job("opt-3b", 0)

    assert store._jobs["opt-3b"]["status"] == "success"


def test_process_job_sets_status_failed_on_nonzero_exit_without_result(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """A non-zero subprocess exit code without a result marks the job failed."""
    store.seed_job("opt-4", payload=REAL_RUN_PAYLOAD)
    worker.enqueue_job("opt-4")

    # Exit code 1, no result events → RuntimeError caught by BaseException handler
    ctx, _proc = make_mp_context(exitcode=1, result_events=[])
    worker._mp_ctx = ctx
    worker._mp_start_method = "spawn"

    with patch("core.worker.engine.notify_job_completed"), patch.object(worker, "_get_service") as mock_svc:
        mock_svc.return_value.validate_payload = MagicMock()
        worker._process_job("opt-4", 0)  # must not raise — error handled internally

    assert store._jobs["opt-4"]["status"] == "failed"


def test_process_job_sigkill_fails_without_restart(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """A SIGKILL leaves the run failed for the user to resume, retry or clone."""
    store.seed_job("opt-oom", payload=REAL_RUN_PAYLOAD, attempts=0)
    store.engine = MagicMock()
    worker.enqueue_job("opt-oom")

    ctx, _proc = make_mp_context(exitcode=-9, result_events=[])
    worker._mp_ctx = ctx
    worker._mp_start_method = "spawn"

    with patch("core.worker.engine.notify_job_completed"), patch.object(worker, "_get_service") as mock_svc:
        mock_svc.return_value.validate_payload = MagicMock()
        worker._process_job("opt-oom", 0)

    assert store._jobs["opt-oom"]["status"] == "failed"
    assert store._jobs["opt-oom"]["stop_reason"] == "interrupted"
    assert store._jobs["opt-oom"]["attempts"] == 0
    assert store.requeue_calls == []


def test_process_job_fails_classified_infrastructure_event(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """Fail a sandbox/provider transport interruption instead of requeueing it."""
    store.seed_job("opt-transient", payload=REAL_RUN_PAYLOAD, attempts=0)
    store.engine = MagicMock()
    worker.enqueue_job("opt-transient")
    ctx, _proc = make_mp_context(
        exitcode=1,
        result_events=[
            {
                "type": EVENT_ERROR,
                "error": "sandbox transport interrupted",
                "error_type": "InfrastructureInterruptionError",
                "failure_kind": INFRASTRUCTURE_INTERRUPTION,
            }
        ],
    )
    worker._mp_ctx = ctx
    worker._mp_start_method = "spawn"

    with patch("core.worker.engine.notify_job_completed"), patch.object(worker, "_get_service") as mock_svc:
        mock_svc.return_value.validate_payload = MagicMock()
        worker._process_job("opt-transient", 0)

    assert store._jobs["opt-transient"]["status"] == "failed"
    assert store._jobs["opt-transient"]["stop_reason"] == "interrupted"
    assert store.requeue_calls == []


def test_process_job_does_not_recover_deterministic_failure_event(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """Keep user, configuration, and provider HTTP errors terminal."""
    store.seed_job("opt-deterministic", payload=REAL_RUN_PAYLOAD, attempts=0)
    store.engine = MagicMock()
    worker.enqueue_job("opt-deterministic")
    ctx, _proc = make_mp_context(
        exitcode=1,
        result_events=[
            {
                "type": EVENT_ERROR,
                "error": "provider rejected the request with 400",
                "error_type": "ValueError",
                "failure_kind": DETERMINISTIC_FAILURE,
            }
        ],
    )
    worker._mp_ctx = ctx
    worker._mp_start_method = "spawn"

    with patch("core.worker.engine.notify_job_completed"), patch.object(worker, "_get_service") as mock_svc:
        mock_svc.return_value.validate_payload = MagicMock()
        worker._process_job("opt-deterministic", 0)

    assert store._jobs["opt-deterministic"]["status"] == "failed"
    assert store.requeue_calls == []


def test_worker_shutdown_flag_is_temporary_only_for_active_job(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """Distinguish service shutdown from an explicit persisted cancel."""
    event = threading.Event()
    event.set()
    store.seed_job("opt-shutdown", status="running")
    worker._running = False
    with pytest.raises(WorkerShutdownError):
        worker._raise_if_interrupted(event, "opt-shutdown")
    store.update_job("opt-shutdown", status="cancelled")
    with pytest.raises(engine_module.CancellationError):
        worker._raise_if_interrupted(event, "opt-shutdown")


def test_process_job_releases_run_when_worker_shuts_down_during_validating(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """A shutdown while validating hands the run to the orphan sweep instead of failing it."""
    store.seed_job("opt-stop-validating", payload=REAL_RUN_PAYLOAD, status="validating", claimed_by="w1")
    worker.enqueue_job("opt-stop-validating")

    def _shutdown(*_args: object, **_kwargs: object) -> None:
        """Stop the worker while the payload is validated."""
        worker._running = False
        raise WorkerShutdownError("Optimization was interrupted by worker shutdown.")

    with (
        patch("core.worker.engine.notify_job_completed") as notify,
        patch.object(worker, "_get_service") as mock_svc,
    ):
        mock_svc.return_value.validate_payload.side_effect = _shutdown
        worker._process_job("opt-stop-validating", 0)

    job = store._jobs["opt-stop-validating"]
    assert job["status"] == "validating"
    assert job["claimed_by"] is None
    assert job["lease_expires_at"] is None
    assert job["recovery"]["cause"] == engine_module.WORKER_RESTART_CAUSE
    assert "completed_at" not in job
    notify.assert_not_called()


def test_release_for_restart_logs_a_lost_generation_fence(
    worker: BackgroundWorker,
    store: FakeJobStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A release that loses its generation fence says so instead of claiming the run was released.

    Args:
        worker: Worker under test.
        store: Fake store whose fenced update loses, as after an orphan sweep fence.
        caplog: Pytest fixture that captures the worker logs.
    """
    store.seed_job("opt-lost-release", status="running", execution_generation=3)
    store.update_job_if_status = MagicMock(return_value=False)  # type: ignore[attr-defined]
    caplog.set_level("INFO", logger=engine_module.__name__)

    worker._release_for_restart("opt-lost-release", 2)

    store.update_job_if_status.assert_called_once()  # type: ignore[attr-defined]
    assert "lost its generation 2 fence" in caplog.text
    assert "released for recovery" not in caplog.text


def test_process_job_releases_run_when_worker_shuts_down_during_running(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """A shutdown while the optimizer runs leaves the run active for a live worker to resume."""
    store.seed_job("opt-stop-running", payload=REAL_RUN_PAYLOAD, claimed_by="w1")
    worker.enqueue_job("opt-stop-running")
    worker._running = True
    ctx, proc = make_mp_context(exitcode=0, result_events=[])
    proc.is_alive.return_value = True

    def _shutdown() -> None:
        """Stop the worker once the optimizer subprocess starts."""
        assert store._jobs["opt-stop-running"]["status"] == "running"
        worker._running = False
        for event in worker._cancel_events.values():
            event.set()

    proc.start.side_effect = _shutdown
    worker._mp_ctx = ctx
    worker._mp_start_method = "spawn"

    with (
        patch("core.worker.engine.notify_job_completed") as notify,
        patch.object(worker, "_get_service") as mock_svc,
    ):
        mock_svc.return_value.validate_payload = MagicMock()
        worker._process_job("opt-stop-running", 0)

    job = store._jobs["opt-stop-running"]
    assert job["status"] == "running"
    assert job["claimed_by"] is None
    assert job["recovery"]["cause"] == engine_module.WORKER_RESTART_CAUSE
    assert job["message"] == "Waiting for a live worker after a restart"
    notify.assert_not_called()


def test_process_job_sets_status_failed_when_subprocess_sends_no_result(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """A clean subprocess exit with no result event still marks the job failed."""
    store.seed_job("opt-5", payload=REAL_RUN_PAYLOAD)
    worker.enqueue_job("opt-5")

    # Exit code 0 but queue is empty → no result_dict → RuntimeError caught internally
    ctx, _proc = make_mp_context(exitcode=0, result_events=[])
    worker._mp_ctx = ctx
    worker._mp_start_method = "spawn"

    with patch("core.worker.engine.notify_job_completed"), patch.object(worker, "_get_service") as mock_svc:
        mock_svc.return_value.validate_payload = MagicMock()
        worker._process_job("opt-5", 0)  # must not raise — error handled internally

    assert store._jobs["opt-5"]["status"] == "failed"


def test_process_job_fails_a_queued_grid_search_without_running_it(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """A grid search queued before grid search was retired fails instead of executing."""
    store.seed_job(
        "opt-6",
        payload=REAL_RUN_PAYLOAD,
        payload_overview={"optimization_type": "grid_search", "username": "bob"},
    )
    worker.enqueue_job("opt-6")
    ctx, _proc = make_mp_context(exitcode=0, result_events=[])
    worker._mp_ctx = ctx
    worker._mp_start_method = "spawn"

    with patch("core.worker.engine.notify_job_completed"), patch.object(worker, "_get_service"):
        worker._process_job("opt-6", 0)

    assert store._jobs["opt-6"]["status"] == "failed"
    assert store._jobs["opt-6"]["message"] == engine_module.GRID_SEARCH_RETIRED_MESSAGE
    ctx.Process.assert_not_called()


def test_process_job_validates_unified_payload_against_its_contract(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """A unified run is parsed as a unified request and checked by the workspace contract."""
    store.seed_job(
        "opt-7",
        payload=REAL_UNIFIED_PAYLOAD,
        payload_overview={"optimization_type": "blackbox", "username": "bob"},
        execution_budget_id="budget-7",
    )
    worker.enqueue_job("opt-7")
    ctx, _proc = make_mp_context(exitcode=0, result_events=[])
    worker._mp_ctx = ctx
    worker._mp_start_method = "spawn"

    with (
        patch("core.worker.engine.notify_job_completed"),
        patch.object(worker, "_get_service"),
        patch.object(engine_module, "validate_blackbox_payload") as legacy_validate,
        patch.object(
            engine_module, "validate_unified_payload", side_effect=ValueError("metric shape mismatch")
        ) as validate,
    ):
        worker._process_job("opt-7", 0)

    validate.assert_called_once()
    assert validate.call_args.args[0].payload_kind == "unified_v1"
    legacy_validate.assert_not_called()
    assert store._jobs["opt-7"]["status"] == "failed"
    assert "metric shape mismatch" in store._jobs["opt-7"]["message"]


def test_worker_keeps_stored_metric_model_without_declaration(worker: BackgroundWorker, store: FakeJobStore) -> None:
    """A queued legacy run keeps its selected metric model before protection."""
    payload = copy.deepcopy(REAL_UNIFIED_PAYLOAD)
    payload["models"]["metric_model"] = {
        "name": "anthropic/old-judge",
        "token_source": "byok",
        "byok_provider": "anthropic",
    }
    payload["workspace"]["metric_code"] = (
        "from skynet import llm\ndef score(input, output, expected=None):\n    return llm('judge')\n"
    )
    store.seed_job(
        "legacy-metric",
        payload=payload,
        payload_overview={"optimization_type": "blackbox", "username": "bob"},
        execution_budget_id="budget-legacy",
    )
    worker.enqueue_job("legacy-metric")
    ctx, _proc = make_mp_context(exitcode=0, result_events=[])
    worker._mp_ctx = ctx
    worker._mp_start_method = "spawn"
    recorded = {}
    real_alias = engine_module.alias_unified_model_roles

    def capture(source: dict, **kwargs: object) -> None:
        """Capture the model after the real execution alias step."""
        real_alias(source, **kwargs)
        recorded.update(source["models"]["metric_model"])
        raise ValueError("stop after alias")

    with (
        patch("core.worker.engine.notify_job_completed"),
        patch.object(worker, "_get_service"),
        patch.object(engine_module, "validate_unified_payload"),
        patch.object(engine_module, "alias_unified_model_roles", side_effect=capture),
    ):
        worker._process_job("legacy-metric", 0)
    assert recorded["name"] == "anthropic/old-judge"
    assert recorded["token_source"] == "byok"
    assert recorded["byok_provider"] == "anthropic"


def test_process_job_handles_key_error_when_job_deleted_mid_run(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """When get_job raises KeyError (job deleted), _process_job must log and return cleanly."""
    store.seed_job("opt-8", payload=REAL_RUN_PAYLOAD)
    worker.enqueue_job("opt-8")

    good_result = {"baseline_test_metric": 0.5, "optimized_test_metric": 0.7}
    ctx, _proc = make_mp_context(
        exitcode=0,
        result_events=[{"type": EVENT_RESULT, "result": good_result}],
    )
    worker._mp_ctx = ctx
    worker._mp_start_method = "spawn"

    original_get_job = store.get_job
    call_count = {"n": 0}

    def _get_job_side_effect(opt_id: str):
        """Return the row on first call; raise ``KeyError`` on subsequent calls."""
        call_count["n"] += 1
        # First call (load payload): return normally.
        # Second call (post-result status check): raise KeyError.
        if call_count["n"] >= 2:
            raise KeyError(opt_id)
        return original_get_job(opt_id)

    store.get_job = _get_job_side_effect  # type: ignore[method-assign,assignment]

    # Must NOT raise even though get_job raises KeyError on the second call.
    with patch("core.worker.engine.notify_job_completed"), patch.object(worker, "_get_service") as mock_svc:
        mock_svc.return_value.validate_payload = MagicMock()
        worker._process_job("opt-8", 0)  # must not raise


def test_process_job_cancellation_error_notifies_cancelled(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """CancellationError fired *after* overview is assigned → notify called with status='cancelled'.

    We trigger cancel at the second _check_cancel() (after validation) by setting the event
    inside validate_payload, which is called after overview is assigned.
    """
    store.seed_job("opt-9", payload=REAL_RUN_PAYLOAD)
    worker.enqueue_job("opt-9")

    notify_mock = MagicMock()

    def _set_cancel_then_validate(*args, **kwargs):
        """Side effect that sets the cancel event from inside validation."""
        store.update_job("opt-9", status="cancelled")
        worker._cancel_events["opt-9"].set()

    with (
        patch("core.worker.engine.notify_job_completed", notify_mock),
        patch.object(worker, "_get_service") as mock_svc,
    ):
        mock_svc.return_value.validate_payload.side_effect = _set_cancel_then_validate
        worker._process_job("opt-9", 0)

    # notify_job_completed must have been called with status='cancelled'.
    call_kwargs = notify_mock.call_args_list
    assert any(kw.get("status") == "cancelled" for _, kw in call_kwargs)


def test_process_job_cancellation_error_uses_hebrew_message(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """The Hebrew cancellation message must appear in the engine code path."""
    source = inspect.getsource(engine_module)
    assert "CANCELLATION_REASON" in source
    assert CANCELLATION_REASON == "בוטלה על ידי המשתמש"


def test_process_job_generic_exception_sets_status_failed(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """A generic exception during validation marks the job failed."""
    store.seed_job("opt-10", payload=REAL_RUN_PAYLOAD)
    worker.enqueue_job("opt-10")

    ctx, _proc = make_mp_context(exitcode=0, result_events=[])
    worker._mp_ctx = ctx
    worker._mp_start_method = "spawn"

    with patch("core.worker.engine.notify_job_completed"), patch.object(worker, "_get_service") as mock_svc:
        # validate_payload raises a generic exception (after overview is assigned)
        mock_svc.return_value.validate_payload.side_effect = RuntimeError("boom")
        worker._process_job("opt-10", 0)  # must not raise — error handled internally

    assert store._jobs["opt-10"]["status"] == "failed"


def test_process_job_system_exit_sets_status_failed_and_reraises(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """SystemExit must set status='failed' and then re-raise (is_shutdown=True path)."""
    store.seed_job("opt-11", payload=REAL_RUN_PAYLOAD)
    worker.enqueue_job("opt-11")

    ctx, _proc = make_mp_context(exitcode=0, result_events=[])
    worker._mp_ctx = ctx
    worker._mp_start_method = "spawn"

    with patch("core.worker.engine.notify_job_completed"), patch.object(worker, "_get_service") as mock_svc:
        mock_svc.return_value.validate_payload.side_effect = SystemExit(1)
        with pytest.raises(SystemExit):
            worker._process_job("opt-11", 0)

    assert store._jobs["opt-11"]["status"] == "failed"


def test_process_job_keyboard_interrupt_sets_status_failed_and_reraises(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """KeyboardInterrupt (is_shutdown=True) must set status='failed' and re-raise."""
    store.seed_job("opt-12", payload=REAL_RUN_PAYLOAD)
    worker.enqueue_job("opt-12")

    ctx, _proc = make_mp_context(exitcode=0, result_events=[])
    worker._mp_ctx = ctx
    worker._mp_start_method = "spawn"

    with patch("core.worker.engine.notify_job_completed"), patch.object(worker, "_get_service") as mock_svc:
        mock_svc.return_value.validate_payload.side_effect = KeyboardInterrupt()
        with pytest.raises(KeyboardInterrupt):
            worker._process_job("opt-12", 0)

    assert store._jobs["opt-12"]["status"] == "failed"


def _stall_ctx() -> tuple[MagicMock, MagicMock]:
    """Build an mp context whose child stays alive and emits no events.

    Returns:
        ``(ctx, proc)`` where ``proc.is_alive()`` always returns ``True`` and
        the queue is empty — the shape that should trip the stall watchdog.
    """
    proc = fake_mp_process(exitcode=0, is_alive=True)
    mq = fake_mp_queue()
    ctx = MagicMock()
    ctx.Queue.return_value = mq
    ctx.Process.return_value = proc
    ctx.get_start_method.return_value = "spawn"
    return ctx, proc


def test_process_job_watchdog_fails_run_that_emits_no_events(
    worker: BackgroundWorker,
    store: FakeJobStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live child that emits nothing past the stall window is failed and terminated."""
    store.seed_job("opt-stall", payload=REAL_RUN_PAYLOAD)
    worker.enqueue_job("opt-stall")

    ctx, proc = _stall_ctx()
    worker._mp_ctx = ctx
    worker._mp_start_method = "spawn"
    monkeypatch.setattr(engine_module.settings, "job_stall_timeout_seconds", 1.0)

    # A monotonic clock that jumps far past the 1s window on every read forces
    # the watchdog to trip on the first event-less iteration, deterministically.
    with (
        patch("core.worker.engine.notify_job_completed"),
        patch.object(worker, "_get_service") as mock_svc,
        patch.object(engine_module.time, "monotonic", side_effect=itertools.count(0.0, 100_000.0)),
    ):
        mock_svc.return_value.validate_payload = MagicMock()
        worker._process_job("opt-stall", 0)  # must not raise — handled internally

    assert store._jobs["opt-stall"]["status"] == "failed"
    assert "stall" in store._jobs["opt-stall"]["message"].lower()
    assert proc.terminate.called


def test_process_job_watchdog_disabled_when_timeout_zero(
    worker: BackgroundWorker,
    store: FakeJobStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A zero stall timeout disables the watchdog: an event-less child is not stall-failed."""
    store.seed_job("opt-nowatch", payload=REAL_RUN_PAYLOAD)
    worker.enqueue_job("opt-nowatch")

    # Child stays alive for two iterations then exits cleanly with a result, so
    # the run completes normally — the watchdog must never look at the clock.
    proc = fake_mp_process(exitcode=0, is_alive=[True, True, False, False])
    mq = fake_mp_queue(make_result_event())
    ctx = MagicMock()
    ctx.Queue.return_value = mq
    ctx.Process.return_value = proc
    ctx.get_start_method.return_value = "spawn"
    worker._mp_ctx = ctx
    worker._mp_start_method = "spawn"
    monkeypatch.setattr(engine_module.settings, "job_stall_timeout_seconds", 0.0)

    with patch("core.worker.engine.notify_job_completed"), patch.object(worker, "_get_service") as mock_svc:
        mock_svc.return_value.validate_payload = MagicMock()
        worker._process_job("opt-nowatch", 0)

    assert store._jobs["opt-nowatch"]["status"] == "success"
    assert not proc.terminate.called


def test_process_job_watchdog_does_not_fire_while_events_flow(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """Draining events each iteration keeps the stall timer reset so the run completes."""
    store.seed_job("opt-live", payload=REAL_RUN_PAYLOAD)
    worker.enqueue_job("opt-live")

    proc = fake_mp_process(exitcode=0, is_alive=[True, True, False, False])
    mq = fake_mp_queue(make_progress_event(), make_result_event())
    ctx = MagicMock()
    ctx.Queue.return_value = mq
    ctx.Process.return_value = proc
    ctx.get_start_method.return_value = "spawn"
    worker._mp_ctx = ctx
    worker._mp_start_method = "spawn"

    with patch("core.worker.engine.notify_job_completed"), patch.object(worker, "_get_service") as mock_svc:
        mock_svc.return_value.validate_payload = MagicMock()
        worker._process_job("opt-live", 0)

    assert store._jobs["opt-live"]["status"] == "success"
    assert not proc.terminate.called


def test_process_job_records_run_completed_milestone(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """A successful run emits one ``run_completed`` telemetry milestone with structural props only."""
    store.seed_job(
        "opt-tel",
        payload=REAL_RUN_PAYLOAD,
        payload_overview={
            "optimization_type": "run",
            "username": "alice",
            "optimizer_name": "gepa",
            "model_name": "openai/gpt-4o-mini",
            "token_source": "managed",
            "name": "secret run name",
        },
    )
    worker.enqueue_job("opt-tel")

    ctx, _proc = make_mp_context(
        exitcode=0,
        result_events=[{"type": EVENT_RESULT, "result": {"baseline_test_metric": 0.5, "optimized_test_metric": 0.7}}],
    )
    worker._mp_ctx = ctx
    worker._mp_start_method = "spawn"

    with (
        patch("core.worker.engine.notify_job_completed"),
        patch("core.worker.engine.record_server_event") as record,
        patch.object(worker, "_get_service") as mock_svc,
    ):
        mock_svc.return_value.validate_payload = MagicMock()
        worker._process_job("opt-tel", 0)

    assert store._jobs["opt-tel"]["status"] == "success"
    record.assert_called_once()
    kwargs = record.call_args.kwargs
    assert kwargs["username"] == "alice"
    assert kwargs["name"] == "run_completed"
    assert kwargs["properties"] == {
        "status": "success",
        "optimization_type": "run",
        "optimizer": "gepa",
        "model": "openai/gpt-4o-mini",
        "token_source": "managed",
        "baseline": 0.5,
        "optimized": 0.7,
        "lift": pytest.approx(0.2),
        "held_out": False,
        "held_out_rows": None,
        "from_sample": False,
    }


def test_process_job_records_run_failed_milestone(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """A failed run emits ``run_failed`` and a telemetry error never changes the outcome."""
    store.seed_job("opt-bad", payload=None, payload_overview={"username": "alice", "optimization_type": "run"})
    worker.enqueue_job("opt-bad")

    with (
        patch("core.worker.engine.notify_job_completed"),
        patch("core.worker.engine.record_server_event", side_effect=RuntimeError("telemetry down")) as record,
    ):
        worker._process_job("opt-bad", 0)

    assert store._jobs["opt-bad"]["status"] == "failed"
    record.assert_called_once()
    assert record.call_args.kwargs["name"] == "run_failed"
    assert record.call_args.kwargs["properties"]["status"] == "failed"


_BLACKBOX_PAYLOAD: dict = {
    "name": "bb",
    "username": "alice",
    "seed_candidate": "hello world",
    "objective": "more vowels",
    "scorer": {"kind": "python", "metric_code": "def score(candidate, case=None): return 1.0"},
    "reflection_model_config": {"name": "gpt-4o"},
}

_BLACKBOX_OVERVIEW: dict = {
    "optimization_type": "blackbox",
    "username": "alice",
    "optimizer_name": "auto",
    "model_name": "gpt-4o",
    "token_source": "managed",
}


def test_protected_blackbox_novelty_failure_has_plain_detail(worker: BackgroundWorker, store: FakeJobStore) -> None:
    """Store the domain detail without an HTTP status prefix on legacy jobs."""
    payload = {
        **_BLACKBOX_PAYLOAD,
        "reflection_model_config": {"name": "anthropic/claude-sonnet-4", "token_source": "byok"},
        "strategy": {"mode": "single", "engine": "shinka_evolve"},
        "shinka": {"novelty": True},
    }
    store.seed_job("novelty", payload=payload, payload_overview=_BLACKBOX_OVERVIEW, execution_budget_id="budget")
    worker.enqueue_job("novelty")
    with patch("core.worker.engine.notify_job_completed"), patch.object(worker, "_get_service"):
        worker._process_job("novelty", 0)
    assert store._jobs["novelty"]["status"] == "failed"
    assert store._jobs["novelty"]["message"] == DomainError("submission.byok_novelty_unsupported").detail


@pytest.mark.parametrize("source", ["explicit", "inherited"])
def test_protected_blackbox_custom_novelty_fails_in_worker(
    worker: BackgroundWorker, store: FakeJobStore, source: str
) -> None:
    """Validate the saved custom reflection base before running a protected job."""
    payload = {
        **_BLACKBOX_PAYLOAD,
        "reflection_model_config": {
            "name": "openai/gpt-4o",
            "token_source": "byok" if source == "explicit" else None,
            "byok_provider": "openai",
        },
        "token_source": "byok",
        "strategy": {"mode": "single", "engine": "shinka_evolve"},
        "shinka": {"novelty": True},
        "max_cost_cents": 100,
    }
    store.engine = MagicMock()
    store.seed_job("custom-novelty", payload=payload, payload_overview=_BLACKBOX_OVERVIEW, execution_budget_id="budget")
    worker.enqueue_job("custom-novelty")
    vault = MagicMock()
    vault.resolve_connection.return_value = MagicMock(api_base="https://custom.example/v1")
    with (
        patch("core.worker.engine.notify_job_completed"),
        patch("core.worker.engine.record_server_event"),
        patch.object(worker, "_get_service"),
        patch("core.worker.engine.ProviderKeyVault", return_value=vault),
    ):
        worker._process_job("custom-novelty", 0)
    assert store._jobs["custom-novelty"]["status"] == "failed"
    assert store._jobs["custom-novelty"]["message"] == DomainError("submission.byok_novelty_unsupported").detail
    vault.resolve_connection.assert_called_once_with(payload["username"], "openai", verified_only=True)


def test_gateway_domain_failure_has_plain_detail(worker: BackgroundWorker, store: FakeJobStore) -> None:
    """Convert a gateway DomainError before recording a protected worker failure."""
    store.engine = MagicMock()
    store.seed_job(
        "gateway-domain",
        payload=_BLACKBOX_PAYLOAD,
        payload_overview=_BLACKBOX_OVERVIEW,
        execution_budget_id="budget",
        execution_budget_generation=0,
    )
    worker.enqueue_job("gateway-domain")
    gateway = MagicMock()
    gateway.protect_payload.side_effect = DomainError("submission.byok_novelty_unsupported")
    with (
        patch("core.worker.engine.notify_job_completed"),
        patch("core.worker.engine.record_server_event"),
        patch.object(worker, "_get_service"),
        patch("core.worker.engine.validate_blackbox_payload"),
        patch("core.worker.engine.payload_uses_token_source", return_value=False),
        patch("core.worker.engine.ModelGateway", return_value=gateway),
        patch("core.worker.engine.BudgetService"),
        patch("core.worker.engine.BudgetRuntime"),
        patch("core.worker.engine.bind_protected_sandbox"),
        patch("core.worker.engine.protected_image", return_value=None),
        patch("core.worker.engine.resolve_execution_credentials", side_effect=lambda payload, **kwargs: payload),
        patch.object(worker, "_close_budget_gateway", return_value=(None, None)),
    ):
        worker._process_job("gateway-domain", 0)
    gateway.protect_payload.assert_called_once()
    assert store._jobs["gateway-domain"]["message"] == DomainError("submission.byok_novelty_unsupported").detail


def _run_repository_job(
    worker: BackgroundWorker, store: FakeJobStore, snapshot: dict
) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Run one stored repository job with staging that leaves ``snapshot`` on the payload.

    Args:
        worker: The worker under test.
        store: Its job store.
        snapshot: What staging records under the repository snapshot key.

    Returns:
        The budget gateway, the publish step and the staged repository.
    """
    request = BlackboxRunRequest(
        username="alice",
        recipe="repo",
        scorer={"kind": "python", "metric_code": "def score(candidate, case=None): return 1.0"},
        target={"kind": "repo", "repo": {"repository": "acme/app", "editable_paths": ["src"]}},
        reflection_model_config={"name": "gpt-4o"},
        hardening_job_id="hardening-1",
    )
    store.engine = MagicMock()
    store.seed_job(
        "repo-hardened",
        payload=request.model_dump(mode="json", by_alias=True),
        payload_overview=_BLACKBOX_OVERVIEW,
        execution_budget_id="budget",
        execution_budget_generation=0,
    )
    worker.enqueue_job("repo-hardened")
    gateway = MagicMock()
    gateway.protect_payload.side_effect = lambda payload, **kwargs: payload
    gateway.cost_ceiling_cents.return_value = 100
    staged = MagicMock()

    def stage(payload: dict, **kwargs: object) -> tuple[dict, MagicMock]:
        """Stage the repository the way the real step leaves the payload."""
        payload["target"]["repo"][engine_module.ENVIRONMENT_KEY] = {}
        payload[REPO_SNAPSHOT_KEY] = snapshot
        return payload, staged

    ctx, _proc = make_mp_context(exitcode=0, result_events=[make_result_event()])
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("core.worker.engine.notify_job_completed"))
        stack.enter_context(patch("core.worker.engine.record_server_event"))
        stack.enter_context(patch.object(worker, "_get_service"))
        stack.enter_context(patch("core.worker.engine.validate_blackbox_payload"))
        stack.enter_context(patch("core.worker.engine.payload_uses_token_source", return_value=False))
        stack.enter_context(patch("core.worker.engine.ModelGateway", return_value=gateway))
        stack.enter_context(patch("core.worker.engine.BudgetService"))
        stack.enter_context(patch("core.worker.engine.BudgetRuntime"))
        stack.enter_context(patch("core.worker.engine.bind_protected_sandbox"))
        stack.enter_context(patch("core.worker.engine.protected_image", return_value=None))
        stack.enter_context(
            patch("core.worker.engine.resolve_execution_credentials", side_effect=lambda payload, **kwargs: payload)
        )
        stack.enter_context(patch("core.worker.engine.host_proposer", return_value=None))
        stack.enter_context(patch("core.worker.engine.stage_repository", side_effect=stage))
        stack.enter_context(patch("core.worker.engine.check_repository", return_value=[]))
        stack.enter_context(patch("core.worker.engine.infer_repo_setup", return_value=None))
        stack.enter_context(patch("core.worker.engine.bind_repo_scorer"))
        stack.enter_context(patch("core.worker.engine.has_exposed_execution_credentials", return_value=False))
        stack.enter_context(patch("core.worker.engine.mp.get_context", return_value=ctx))
        publish = stack.enter_context(patch("core.worker.engine.publish_improvement"))
        stack.enter_context(patch.object(worker, "_record_repo_commit"))
        stack.enter_context(patch.object(worker, "_record_repo_environment", return_value=None))
        stack.enter_context(patch.object(worker, "_close_budget_gateway", return_value=(None, None)))
        worker._process_job("repo-hardened", 0)
    return gateway, publish, staged


def test_repository_run_hands_its_hardening_job_id_to_the_publish(
    worker: BackgroundWorker, store: FakeJobStore
) -> None:
    """The setup's hardening job id goes from the stored run request to the PR publish (SKY-356)."""
    _gateway, publish, staged = _run_repository_job(worker, store, {})

    publish.assert_called_once()
    published_payload = publish.call_args.args[1]
    assert published_payload["hardening_job_id"] == "hardening-1"
    assert publish.call_args.args[2] is staged
    assert publish.call_args.kwargs["username"] == "alice"


@pytest.mark.parametrize(
    ("snapshot", "granted"),
    [
        ({"proposer_hosts": list(PROPOSER_REGISTRY_HOSTS)}, PROPOSER_REGISTRY_HOSTS),
        ({"proposer_hosts": []}, ()),
        ({}, ()),
    ],
)
def test_repository_run_grants_the_staged_proposer_hosts(
    snapshot: dict, granted: tuple[str, ...], worker: BackgroundWorker, store: FakeJobStore
) -> None:
    """The guest's boxes get the hosts staging worked out, and none when it names none (E57)."""
    gateway, _publish, _staged = _run_repository_job(worker, store, snapshot)

    gateway.allow_guest_hosts.assert_called_once_with(granted)


def test_unified_byok_injection_domain_error_before_sandbox(worker: BackgroundWorker, store: FakeJobStore) -> None:
    """Record a plain domain error when the BYOK bridge rejects a model."""
    name = "openrouter/anthropic/claude-sonnet-4"
    payload = copy.deepcopy(REAL_UNIFIED_PAYLOAD)
    payload["models"] = {"reflection_model": {"name": name, "token_source": "byok", "byok_provider": "anthropic"}}
    payload["token_source"] = "byok"
    store.engine = MagicMock()
    store.seed_job(
        "unified-inject",
        payload=payload,
        payload_overview={"optimization_type": "blackbox", "username": payload["username"], "token_source": "byok"},
        execution_budget_id="budget",
        execution_budget_generation=0,
    )
    worker.enqueue_job("unified-inject")
    vault = MagicMock()
    vault.resolve_connection.return_value = MagicMock(api_base=None, secret="fixture", params={})
    with (
        patch("core.worker.engine.notify_job_completed"),
        patch("core.worker.engine.record_server_event"),
        patch.object(worker, "_get_service"),
        patch("core.worker.engine.ProviderKeyVault", return_value=vault),
        patch("core.worker.engine.bind_protected_sandbox") as bind,
    ):
        worker._process_job("unified-inject", 0)
    vault.resolve_connection.assert_called_with(payload["username"], "anthropic")
    bind.assert_not_called()
    assert store._jobs["unified-inject"]["status"] == "failed"
    assert (
        store._jobs["unified-inject"]["message"]
        == DomainError("billing.byok_model_not_served", status=400, model=name, provider="anthropic").detail
    )


def test_process_job_blackbox_without_budget_requires_vercel(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """A black-box job without a protected budget fails: scoring only runs in the Vercel sandbox.

    The unprotected test escape hatch that lets other run types dispatch a local
    subprocess never applies to black-box, so the job fails at the gate before any
    validation or child process starts.
    """
    store.seed_job("opt-bb", payload=_BLACKBOX_PAYLOAD, payload_overview=_BLACKBOX_OVERVIEW)
    worker.enqueue_job("opt-bb")

    with (
        patch("core.worker.engine.notify_job_completed"),
        patch("core.worker.engine.validate_blackbox_payload") as validate_bb,
    ):
        worker._process_job("opt-bb", 0)

    assert store._jobs["opt-bb"]["status"] == "failed"
    assert "cloud sandbox" in store._jobs["opt-bb"]["message"]
    assert "Vercel" not in store._jobs["opt-bb"]["message"]
    validate_bb.assert_not_called()


def test_process_job_blackbox_validation_failure_marks_job_failed(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """A black-box validation error fails the job before any child process starts."""
    store.seed_job("opt-bb-bad", payload=_BLACKBOX_PAYLOAD, payload_overview=_BLACKBOX_OVERVIEW)
    worker.enqueue_job("opt-bb-bad")

    ctx, proc = make_mp_context(exitcode=0, result_events=[])
    worker._mp_ctx = ctx
    worker._mp_start_method = "spawn"

    with (
        patch("core.worker.engine.notify_job_completed"),
        patch(
            "core.worker.engine.validate_blackbox_payload", side_effect=ServiceError("Engine 'nope' is not available")
        ),
        patch.object(worker, "_get_service"),
    ):
        worker._process_job("opt-bb-bad", 0)

    assert store._jobs["opt-bb-bad"]["status"] == "failed"
    proc.start.assert_not_called()


_USAGE_SNAPSHOT = [{"model": "openai/gpt-4o-mini", "input_tokens": 4000, "output_tokens": 300}]


def _run_failing_leg(worker: BackgroundWorker, store: FakeJobStore, job_id: str, error_event: dict) -> MagicMock:
    """Run one legacy leg that reports usage and then fails, capturing its debit calls.

    Args:
        worker: The worker under test.
        store: The in-memory store the job is seeded in.
        job_id: Job to seed and process.
        error_event: The child's terminal error event.

    Returns:
        The patched ``_debit_run_cents`` mock.
    """
    store.seed_job(job_id, payload=REAL_RUN_PAYLOAD, attempts=0, execution_generation=3)
    store.engine = MagicMock()
    worker.enqueue_job(job_id)
    progress = {**make_progress_event(), "usage_by_model": _USAGE_SNAPSHOT}
    ctx, _proc = make_mp_context(exitcode=1, result_events=[progress, error_event])
    worker._mp_ctx = ctx
    worker._mp_start_method = "spawn"
    with (
        patch("core.worker.engine.notify_job_completed"),
        patch.object(worker, "_get_service") as mock_svc,
        patch.object(worker, "_debit_run_cents", return_value=7) as debit,
    ):
        mock_svc.return_value.validate_payload = MagicMock()
        worker._process_job(job_id, 0)
    return debit


def test_process_job_bills_failed_legacy_leg_for_reported_usage(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """A legacy run that fails after spending tokens is billed once, keyed to its execution leg."""
    debit = _run_failing_leg(
        worker,
        store,
        "opt-bill-fail",
        {
            "type": EVENT_ERROR,
            "error": "provider rejected the request with 400",
            "error_type": "ValueError",
            "failure_kind": DETERMINISTIC_FAILURE,
            "usage_by_model": _USAGE_SNAPSHOT,
        },
    )
    assert store._jobs["opt-bill-fail"]["status"] == "failed"
    debit.assert_called_once()
    assert debit.call_args.args[1] == {"usage_by_model": _USAGE_SNAPSHOT}
    assert debit.call_args.kwargs["settlement_key"] == "legacy:opt-bill-fail:g3"


def test_process_job_does_not_bill_platform_infrastructure_interruption(
    worker: BackgroundWorker,
    store: FakeJobStore,
) -> None:
    """A leg lost to a platform-side interruption fails without charging the user."""
    debit = _run_failing_leg(
        worker,
        store,
        "opt-bill-infra",
        {
            "type": EVENT_ERROR,
            "error": "sandbox transport interrupted",
            "error_type": "InfrastructureInterruptionError",
            "failure_kind": INFRASTRUCTURE_INTERRUPTION,
        },
    )
    assert store._jobs["opt-bill-infra"]["status"] == "failed"
    debit.assert_not_called()


def test_an_inferred_setup_command_is_stored_on_the_run(worker: BackgroundWorker, store: FakeJobStore) -> None:
    """The run's own payload keeps the inferred command, so its config page shows it."""
    store.seed_job("opt-repo", payload={"target": {"kind": "repo", "repo": {"repository": "acme/app"}}})

    worker._record_setup_command("opt-repo", "uv sync --frozen")

    assert store._jobs["opt-repo"]["payload"]["target"] == {
        "kind": "repo",
        "repo": {"repository": "acme/app"},
        "setup_command": "uv sync --frozen",
    }


def test_the_pinned_commit_is_stored_on_the_run(worker: BackgroundWorker, store: FakeJobStore) -> None:
    """A run submitted on a branch keeps the commit staging pinned it to."""
    store.seed_job("opt-repo", payload={"target": {"kind": "repo", "repo": {"repository": "acme/app"}}})

    worker._record_repo_commit("opt-repo", "a" * 40)

    assert store._jobs["opt-repo"]["payload"]["target"]["repo"] == {"repository": "acme/app", "commit": "a" * 40}


def test_checkpoints_taken_after_the_environment_is_recorded_stay_resumable(
    worker: BackgroundWorker, store: FakeJobStore, tmp_path: Path
) -> None:
    """Hand back the stored payload, so checkpoints hash what resume validates against."""
    staged = {
        "optimizer_name": "GEPA",
        "target": {"kind": "repo", "repo": {"repository": "acme/app", "commit": "a" * 40}},
    }
    store.seed_job("opt-repo", payload=copy.deepcopy(staged))
    data = _checkpoint(tmp_path)

    recorded = worker._record_repo_environment("opt-repo", {"services": []})

    stored = store._jobs["opt-repo"]["payload"]
    assert recorded == stored
    assert stored["target"]["repo"]["environment"] == {"services": []}
    validate_checkpoint(data, checkpoint_manifest(data, dict(recorded), "fixture-v1"), stored, "fixture-v1")
    with pytest.raises(CheckpointCompatibilityError, match="configuration_sha256"):
        validate_checkpoint(data, checkpoint_manifest(data, staged, "fixture-v1"), stored, "fixture-v1")


def test_a_payload_without_a_repository_records_no_environment(worker: BackgroundWorker, store: FakeJobStore) -> None:
    """Leave a payload without a repository untouched and report nothing to sync."""
    store.seed_job("opt-plain", payload={"optimizer_name": "GEPA"})

    assert worker._record_repo_environment("opt-plain", {"services": []}) is None
    assert store._jobs["opt-plain"]["payload"] == {"optimizer_name": "GEPA"}


def test_a_store_without_an_engine_runs_no_hardening_job(worker: BackgroundWorker) -> None:
    """Skip the hardening poll when the job store has no SQL engine."""
    with patch.object(engine_module, "process_next_hardening_job") as process:
        assert worker._run_hardening_job(0) is False
    process.assert_not_called()


def test_the_idle_worker_runs_a_hardening_job_with_its_pod_lease(worker: BackgroundWorker, store: FakeJobStore) -> None:
    """Pass the engine, pod name and lease to the hardening pass, and record liveness at start and on each renewal."""
    store.engine = MagicMock()  # type: ignore[attr-defined]
    with (
        patch.object(engine_module, "process_next_hardening_job", return_value=True) as process,
        patch.object(worker, "_touch_activity") as touch,
    ):
        assert worker._run_hardening_job(0) is True
        touch.assert_called_once_with(0)
        process.call_args.kwargs["on_renew"]()
        assert touch.call_count == 2
        touch.assert_called_with(0)
    process.assert_called_once()
    assert process.call_args.args == (store.engine,)  # type: ignore[attr-defined]
    assert process.call_args.kwargs["pod"] == worker._pod_name
    assert process.call_args.kwargs["lease_seconds"] == worker._lease_seconds


def test_a_failing_hardening_pass_does_not_stop_the_worker(worker: BackgroundWorker, store: FakeJobStore) -> None:
    """Report no job when the hardening pass raises, so the loop keeps polling."""
    store.engine = MagicMock()  # type: ignore[attr-defined]
    with patch.object(engine_module, "process_next_hardening_job", side_effect=RuntimeError("boom")):
        assert worker._run_hardening_job(0) is False

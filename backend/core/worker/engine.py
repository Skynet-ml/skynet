"""Background worker for DSPy optimization jobs.

Threaded worker that claims pending jobs from a shared job store and processes
them with configurable concurrency. Multi-pod safety is delegated to the store:
:meth:`JobStore.claim_next_job` performs an atomic claim (Postgres uses
``SELECT ... FOR UPDATE SKIP LOCKED``) so two pods running side by side cannot
race on the same row.
"""

from __future__ import annotations

import contextlib
import json
import logging
import multiprocessing as mp
import os
import queue
import shutil
import socket
import sys
import tempfile
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select, text

from .. import run_log
from ..api.errors import DomainError, domain_service_error
from ..billing import (
    OpenRouterKeyProvisioner,
    ProviderKeyVault,
    StripeBillingService,
    inject_byok_connections,
    inject_provisioned_openrouter_key,
    payload_uses_token_source,
)
from ..billing.budgets import BudgetService
from ..billing.byok_bridge import provider_slug_for_model
from ..billing.byok_upstreams import validate_byok_novelty
from ..billing.byok_vault import byok_provider_for_litellm
from ..billing.host_agent import (
    attach_host_agent_token,
    bind_host_agent,
    failure_message,
    host_agent_failure,
    host_proposer,
    host_reasoning_effort,
)
from ..billing.metering import debit_at_fallback_price
from ..billing.model_gateway import ModelGateway, SandboxControl
from ..billing.pricing import ModelUsage
from ..billing.protected_credentials import (
    ProtectedCredentialVault,
    has_exposed_execution_credentials,
    repo_secret_allowed_hosts,
    resolve_execution_credentials,
)
from ..billing.protected_execution import bind_protected_sandbox, protected_image
from ..billing.runtime import BudgetRuntime, UsagePendingError
from ..billing.vercel_usage import PACKAGE_REGISTRY_HOSTS
from ..config import settings
from ..constants import (
    OPTIMIZATION_TYPE_BLACKBOX,
    OPTIMIZATION_TYPE_GRID_SEARCH,
    OPTIMIZATION_TYPE_RUN,
    OPTIMIZATION_TYPE_TAGGING,
    PAYLOAD_OVERVIEW_ESTIMATED_HIGH,
    PAYLOAD_OVERVIEW_ESTIMATED_LOW,
    PAYLOAD_OVERVIEW_MODEL_NAME,
    PAYLOAD_OVERVIEW_NAME,
    PAYLOAD_OVERVIEW_OPTIMIZATION_TYPE,
    PAYLOAD_OVERVIEW_OPTIMIZER_NAME,
    PAYLOAD_OVERVIEW_TOKEN_SOURCE,
    PAYLOAD_OVERVIEW_TOKEN_SOURCES_BY_MODEL,
    PAYLOAD_OVERVIEW_USERNAME,
    TOKEN_SOURCE_BYOK,
    TOKEN_SOURCE_MANAGED,
    WORKER_RESTART_CAUSE,
)
from ..exceptions import INFRASTRUCTURE_INTERRUPTION, InfrastructureInterruptionError
from ..i18n import CANCELLATION_REASON, SAMPLE_DATASETS
from ..models import BlackboxRunRequest, ModelConfig, OptimizationStatus, RunRequest, UnifiedRunRequest
from ..models.results import TerminalOutcome, result_scores
from ..notifications import notify_job_completed
from ..notifications.run_watcher import RunNotificationWatcher
from ..registry import ServiceRegistry
from ..sandbox.sweep import remove_idle_sandbox_networks, stop_job_sandboxes
from ..service_gateway import DspyService
from ..service_gateway.embedding_pipeline import embed_finished_job
from ..service_gateway.optimization.blackbox.repo_tree import REPO_SNAPSHOT_KEY
from ..service_gateway.optimization.blackbox.service import validate_blackbox_payload
from ..service_gateway.optimization.trajectory import GEPA_STATE_FILENAME
from ..service_gateway.optimization.unified.contract import validate_unified_payload
from ..storage import JobStore
from ..storage.models import JobModel
from ..telemetry import record_server_event
from ..telemetry.run_outcome import completed_properties, failed_properties
from .budget_probe import (
    STOP_REASON_BUDGET_REACHED,
    budget_pace,
    calls_per_iteration,
    done_calls_from_progress,
    pace_metrics,
    planned_calls_from_progress,
)
from .checkpoint_compat import (
    checkpoint_manifest,
    evaluated_incumbent_from_progress,
    is_unified_payload,
    supports_checkpoint,
    validate_checkpoint,
)
from .constants import (
    EVENT_AGENT_RUN,
    EVENT_ERROR,
    EVENT_HEARTBEAT,
    EVENT_LOG,
    EVENT_PROGRESS,
    EVENT_RESULT,
    EVENT_TERMINAL,
)
from .hardening_runner import process_next_hardening_job
from .log_handler import route_sandbox_logs
from .memory_guard import memory_usage_fraction
from .repo_gate import RepoGateError, check_repository, sandbox_place
from .repo_staging import (
    ENVIRONMENT_KEY,
    StagedRepository,
    bind_repo_scorer,
    infer_repo_setup,
    is_repo_payload,
    publish_improvement,
    stage_repository,
)
from .subprocess_runner import run_service_in_subprocess, set_fork_service
from .tagging_job import TaggingAutotagPayload, run_autotag_job
from .unified_aliases import alias_blackbox_metric_model, alias_unified_model_roles, strip_unified_model_aliases
from .vercel_dspy import run_vercel_dspy

logger = logging.getLogger(__name__)

# Continues the 7421370000xx advisory-lock series (issuing_float uses ...06).
_SANDBOX_SWEEP_LOCK_KEY = 742137000007
_SANDBOX_SWEEP_INTERVAL_SECONDS = 300.0
# Paused is included: pause kills the child like a cancel does, and resume
# resets the row to pending and starts new boxes, so no paused box is reused.
_SANDBOX_SWEEP_STATUSES = frozenset(
    {
        OptimizationStatus.success,
        OptimizationStatus.failed,
        OptimizationStatus.cancelled,
        OptimizationStatus.stopped,
        OptimizationStatus.paused,
    }
)

GRID_SEARCH_RETIRED_MESSAGE = "Grid search is no longer supported. Submit this optimization again as a single run."


def _gepa_state_base(base: Path, payload: dict[str, Any]) -> Path:
    """Return where the run keeps ``gepa_state.bin`` under its worker-owned dir.

    Black-box engines (legacy and unified) nest GEPA state in a ``gepa``
    subdir of the run dir; a DSPy run writes it at the base.

    Args:
        base: The worker-owned base directory.
        payload: The stored job payload.

    Returns:
        The directory holding the state file.
    """
    return base / "gepa" if "strategy" in payload or is_unified_payload(payload) else base


# Optional per-row pricing evidence written by newer workers; older results lack it.
_USAGE_DETAIL_FIELDS = (
    "reported_cost_usd",
    "reported_input_tokens",
    "reported_output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)


def _is_amount(value: Any) -> bool:
    """Return whether a result field is a usable non-negative number.

    Args:
        value: A raw field from a serialized usage row.

    Returns:
        ``True`` for a non-negative int or float that is not a bool.
    """
    return isinstance(value, int | float) and not isinstance(value, bool) and value >= 0


def _usages_from_result(result_dict: dict[str, Any] | None, fallback_model: str | None) -> list[ModelUsage]:
    """Build per-model :class:`ModelUsage` from a serialized run/grid result.

    Reads the result's ``usage_by_model`` rows (stamped by the optimizer from the
    LM histories). A result that predates the per-model split — or an in-flight
    job spanning the deploy — has no rows; it falls back to pricing the whole
    ``total_tokens`` on ``fallback_model``, attributing it to input (the cheaper
    side) so the legacy path under-charges rather than over-charges.

    Args:
        result_dict: The serialized run/grid result, or ``None``.
        fallback_model: Model id to price a rows-less legacy result against.

    Returns:
        Per-model usage rows with positive token counts, or ``[]`` when the run
        reported no usage at all.
    """
    if not isinstance(result_dict, dict):
        return []
    usages: list[ModelUsage] = []
    rows = result_dict.get("usage_by_model")
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            model = row.get("model")
            in_tokens = row.get("input_tokens", 0)
            out_tokens = row.get("output_tokens", 0)
            if not isinstance(model, str) or not isinstance(in_tokens, int) or not isinstance(out_tokens, int):
                continue
            if in_tokens > 0 or out_tokens > 0:
                usages.append(
                    ModelUsage(
                        model=model,
                        input_tokens=in_tokens,
                        output_tokens=out_tokens,
                        **{key: row[key] for key in _USAGE_DETAIL_FIELDS if _is_amount(row.get(key))},
                    )
                )
    if usages:
        return usages
    total_tokens = result_dict.get("total_tokens")
    if isinstance(total_tokens, int) and total_tokens > 0:
        return [ModelUsage(model=fallback_model or "unknown", input_tokens=total_tokens, output_tokens=0)]
    return []


def _legacy_settlement_key(optimization_id: str, generation: int | None) -> str | None:
    """Return the idempotency key for one legacy execution leg's debit.

    Every claim bumps the job's execution generation, so the key is unique per
    leg: a resumed or re-run leg bills its own usage, while a redelivered
    settlement of the same leg collides and charges nothing.

    Args:
        optimization_id: The job being billed.
        generation: The leg's execution generation, or ``None`` on a store
            without generations.

    Returns:
        The ledger ``settlement_key``, or ``None`` when there is no generation.
    """
    if generation is None:
        return None
    return f"legacy:{optimization_id}:g{generation}"


class CancellationError(Exception):
    """Raised inside a job thread when the job is cancelled by the user."""


class OptimizerProcessError(RuntimeError):
    """Re-raise an optimizer-process failure in the worker, keeping its class name.

    Args:
        message: The child's error text, shown to the user as the job message.
        remote_error_class: The child exception's class name, for telemetry.
    """

    def __init__(self, message: str, remote_error_class: str | None = None) -> None:
        """Store the child's message and exception class name.

        Args:
            message: The child's error text.
            remote_error_class: The child exception's class name, if known.
        """
        super().__init__(message)
        self.remote_error_class = remote_error_class


class JobStalledError(InfrastructureInterruptionError):
    """Identify a bounded supervisor stall as a temporary infrastructure interruption."""


class WorkerShutdownError(InfrastructureInterruptionError):
    """Identify a worker shutdown that interrupted an already-running job."""


def _raise_if_cancelled(cancel_event: threading.Event | None, optimization_id: str) -> None:
    """Raise ``CancellationError`` when the caller's cancel flag is set; ``None`` is treated as not cancelled.

    Args:
        cancel_event: The cooperative cancel flag (``None`` skips the check).
        optimization_id: ID embedded in the raised error message.

    Raises:
        CancellationError: When ``cancel_event`` is set.
    """
    if cancel_event and cancel_event.is_set():
        raise CancellationError(f"Optimization {optimization_id} cancelled by user")


def _reflection_api_bases(
    engine: Any, username: str, model: ModelConfig, token_source: str, novelty: bool
) -> dict[str, str | None]:
    """Resolve the saved reflection endpoint for novelty validation.

    Args:
        engine: The job store database engine, if available.
        username: The owner of the provider connection.
        model: The reflection model settings.
        token_source: The run's default token source.
        novelty: Whether novelty is enabled.

    Returns:
        The verified provider endpoint, if one is needed and available.
    """
    if engine is None or not novelty or (model.token_source or token_source) != TOKEN_SOURCE_BYOK:
        return {}
    prefix = provider_slug_for_model(model.name)
    provider = model.byok_provider or (byok_provider_for_litellm(prefix) if prefix else "")
    if not provider:
        return {}
    connection = ProviderKeyVault(engine=engine).resolve_connection(username, provider, verified_only=True)
    return {provider: connection.api_base} if connection is not None else {}


class BackgroundWorker:
    """Multi-threaded worker that polls a job store and runs optimization jobs.

    Each worker thread calls ``_worker_loop``, which polls ``_pending_jobs`` and
    dispatches work to ``_process_job``.  Jobs run inside a child process created
    with the configured multiprocessing start method (fork or spawn); events
    (progress, logs, result, error) are streamed back through a ``mp.Queue``
    and persisted by ``_drain_subprocess_events``.  Cancellation is cooperative:
    each job has a ``threading.Event``; ``_raise_if_cancelled`` polls it between
    subprocess join timeouts so the child can be terminated promptly on request.
    """

    def __init__(
        self,
        job_store: JobStore,
        num_workers: int = 2,
        poll_interval: float = 2.0,
        service: DspyService | None = None,
        pod_name: str | None = None,
        lease_seconds: float = 60.0,
        _allow_unprotected_test_execution: bool = False,
    ) -> None:
        """Initialize the worker with a job store and concurrency settings.

        Args:
            job_store: Backend used to load and persist job state.
            num_workers: Number of worker threads to spawn on ``start``.
            poll_interval: Seconds the loop sleeps between empty-queue checks.
            service: Optional pre-built ``DspyService``; one is built lazily otherwise.
            pod_name: Identifier written to ``jobs.claimed_by`` so claim
                ownership survives a process restart and is observable across
                the fleet. Defaults to ``$POD_NAME`` (set via the Kubernetes
                downward API in the Helm chart) or, failing that, the
                hostname.
            lease_seconds: Initial lease window granted by ``claim_next_job``.
                The worker loop renews it via ``_touch_activity`` on every
                cancel-poll tick; should comfortably exceed
                ``cancel_poll_interval × 3``.
            _allow_unprotected_test_execution: Permit direct child-process
                execution only for isolated worker unit tests. Production
                callers must keep the default managed-sandbox requirement.
        """
        self._job_store = job_store
        self._num_workers = num_workers
        self._poll_interval = poll_interval
        self._running = False
        self._threads: list[threading.Thread] = []
        self._service: DspyService | None = service
        self._pod_name = pod_name or os.environ.get("POD_NAME") or socket.gethostname()
        self._lease_seconds = max(float(lease_seconds), 5.0)
        self._allow_unprotected_test_execution = _allow_unprotected_test_execution

        # In-memory queue is retained as a backwards-compat seam: tests and
        # any legacy single-pod callers can still call ``enqueue_job`` directly
        # and the worker loop will drain that list before falling back to the
        # shared ``claim_next_job`` queue.
        self._pending_jobs: list[str] = []
        self._processing_jobs: set[str] = set()
        self._claimed_jobs: set[str] = set()
        self._cancel_events: dict[str, threading.Event] = {}
        self._queue_lock = threading.Lock()
        poll_raw = str(settings.cancel_poll_interval)
        try:
            self._cancel_poll_interval = max(float(poll_raw), 0.05)
        except ValueError:
            logger.warning("Invalid CANCEL_POLL_INTERVAL=%r; using 1.0", poll_raw)
            self._cancel_poll_interval = 1.0
        self._mp_ctx = self._resolve_mp_context()
        self._mp_start_method = self._mp_ctx.get_start_method()

        self._last_activity: dict[int, float] = {}
        self._activity_lock = threading.Lock()
        self._sweep_stop = threading.Event()
        # Background box stops that still run after their jobs ended.
        self._sandbox_stops: set[threading.Thread] = set()
        # Per-thread current job for lease heartbeats.
        self._thread_current_job: dict[int, str] = {}
        # Rate-limits the memory-pressure deferral warning across all threads.
        self._admission_last_log = 0.0

    @staticmethod
    def _resolve_mp_context() -> mp.context.BaseContext:
        """Resolve multiprocessing start method from JOB_RUN_START_METHOD, falling back to system default.

        Returns:
            A multiprocessing context honoring the configured start method.
        """
        requested = settings.job_run_start_method
        try:
            ctx = mp.get_context(requested)
        except ValueError:
            logger.warning(
                "Invalid JOB_RUN_START_METHOD=%r; using default start method.",
                requested,
            )
            ctx = mp.get_context()
        if ctx.get_start_method() != "fork":
            logger.warning(
                "Multiprocessing start method is '%s'. "
                "Custom registry callables may not be available in subprocess jobs.",
                ctx.get_start_method(),
            )
        return ctx

    def _get_service(self) -> DspyService:
        """Get or lazily build the shared DspyService instance.

        Returns:
            The shared ``DspyService`` used by all worker threads.
        """
        if self._service is None:
            self._service = DspyService(ServiceRegistry())
        return self._service

    def enqueue_job(self, optimization_id: str) -> None:
        """Register a cancel event for ``optimization_id`` and pre-stage it locally.

        With the DB-backed claim queue, the canonical hand-off is the row's
        ``status='pending'`` flag, which any pod can claim on its next poll
        tick. The local in-memory queue is kept only so tests and legacy
        single-pod callers continue to work — the worker loop drains it
        before falling back to ``claim_next_job``.

        Args:
            optimization_id: ID of the job to register.
        """
        with self._queue_lock:
            self._cancel_events.setdefault(optimization_id, threading.Event())
            if optimization_id not in self._pending_jobs and optimization_id not in self._processing_jobs:
                self._pending_jobs.append(optimization_id)
                logger.info("Optimization %s enqueued (local hint)", optimization_id)

    def submit_job(
        self,
        optimization_id: str,
        payload: RunRequest | BlackboxRunRequest | UnifiedRunRequest | TaggingAutotagPayload,
        payload_dump: dict[str, Any] | None = None,
    ) -> None:
        """Persist payload to the job store; rely on DB claim for pickup.

        Writes the payload onto the existing ``pending`` row and registers a
        cancel event; the next worker tick (on this pod or a peer) picks the
        job up via :meth:`JobStore.claim_next_job`. Latency-to-pickup is
        bounded by the worker poll interval.

        Args:
            optimization_id: ID of the job being submitted.
            payload: Pydantic model whose ``model_dump`` is stored on the job.
            payload_dump: Optional pre-built ``model_dump(mode="json",
                by_alias=True)`` of ``payload``. The submit routers already
                serialize it for the storage-quota gate; passing it here
                avoids a second full copy of the dataset per request.
        """
        if payload_dump is None:
            payload_dump = payload.model_dump(mode="json", by_alias=True)
        self._job_store.update_job(
            optimization_id,
            payload=payload_dump,
            code_version=settings.code_version,
        )
        with self._queue_lock:
            self._cancel_events[optimization_id] = threading.Event()
        logger.info("Optimization %s submitted (awaiting claim)", optimization_id)

    def _claim_job_from_store(self) -> str | None:
        """Try to claim a pending job from the shared store.

        Returns:
            The claimed optimization ID, or ``None`` when no work is available.
        """
        try:
            record = self._job_store.claim_next_job(self._pod_name, self._lease_seconds)
        except AttributeError:
            # Older JobStore implementation without claim support — fall back
            # to legacy in-memory only behaviour.
            return None
        except Exception:
            logger.exception("claim_next_job raised; backing off")
            return None
        if record is None:
            return None
        optimization_id = record.get("optimization_id") if isinstance(record, dict) else None
        if not optimization_id:
            return None
        with self._queue_lock:
            self._cancel_events.setdefault(optimization_id, threading.Event())
            self._processing_jobs.add(optimization_id)
            self._claimed_jobs.add(optimization_id)
        return optimization_id

    def _update_owned_job(self, optimization_id: str, generation: int | None, **fields: Any) -> None:
        """Advance an active job only while this worker owns its publication generation.

        Args:
            optimization_id: Claimed job identity.
            generation: Generation captured at claim time.
            **fields: Status and timing fields to update.

        Raises:
            CancellationError: When cancellation or another owner has already won.
        """
        cas = getattr(self._job_store, "update_job_if_status", None)
        if generation is not None and callable(cas):
            if not cas(optimization_id, ("pending", "validating", "running"), expected_generation=generation, **fields):
                raise CancellationError("The execution generation is no longer active.")
        else:
            self._job_store.update_job(optimization_id, **fields)

    def _persisted_status(self, optimization_id: str) -> str | None:
        """Read a job's stored status without materializing its payload or result.

        Prefers the store's skinny status projection; store doubles without it
        fall back to the full row read.

        Args:
            optimization_id: ID of the job to read.

        Returns:
            The persisted status string, or ``None`` when the row has none.

        Raises:
            KeyError: When the job no longer exists.
        """
        read_status = getattr(self._job_store, "get_job_status_fields", None)
        if callable(read_status):
            return read_status(optimization_id).get("status")
        return self._job_store.get_job(optimization_id).get("status")

    def _raise_if_interrupted(
        self,
        cancel_event: threading.Event | None,
        optimization_id: str,
    ) -> None:
        """Distinguish user cancellation from shutdown of active worker execution.

        Args:
            cancel_event: Cooperative flag shared with cancellation and worker stop.
            optimization_id: Existing run identity.

        Raises:
            WorkerShutdownError: When worker shutdown interrupts an active job.
            CancellationError: When the user or another owner ended the run.
        """
        if cancel_event is None or not cancel_event.is_set():
            return
        status = None
        with contextlib.suppress(Exception):
            status = self._persisted_status(optimization_id)
        if not self._running and status in {"pending", "validating", "running"}:
            raise WorkerShutdownError("Optimization was interrupted by worker shutdown.")
        _raise_if_cancelled(cancel_event, optimization_id)

    def _get_next_job(self) -> str | None:
        """Return the next claimable job, picking up through the atomic claim.

        ``_pending_jobs`` is only a low-latency *hint* that work may exist
        (populated by the in-process ``enqueue_job`` path and the startup
        recovery backfill). It must never be the pickup itself: popping a hinted
        id and returning it directly skips the DB claim, so a second worker
        thread can re-claim the same still-``pending`` row via
        :meth:`JobStore.claim_next_job` and spawn the job twice — the boot-race
        double-spawn that hit any job left pending across a restart. When the
        store supports atomic claims we drain the hint only to skip the idle
        sleep and then pick up through ``claim_next_job`` (which owns the row
        exclusively via ``FOR UPDATE SKIP LOCKED``). A claim-less legacy/test
        store has no such path, so there we honour the hinted id directly —
        single-pod, no race.

        Returns:
            The optimization ID to process next, or ``None`` when idle.
        """
        if self._defer_for_memory_pressure():
            return None
        with self._queue_lock:
            hinted = self._pending_jobs.pop(0) if self._pending_jobs else None
        if hinted is not None and not hasattr(self._job_store, "claim_next_job"):
            with self._queue_lock:
                self._processing_jobs.add(hinted)
            return hinted
        return self._claim_job_from_store()

    def _defer_for_memory_pressure(self) -> bool:
        """Report whether claiming a new job should wait for memory headroom.

        Running jobs are never touched — this only keeps an idle worker thread
        from adding one more forked child to a pod already near its cgroup
        limit. The row waits in the shared queue for this pod (or a peer) to
        free up, instead of the whole container being OOM-killed mid-run.

        Returns:
            True when container memory usage exceeds the admission threshold.
        """
        threshold = settings.job_admission_max_memory_fraction
        if threshold <= 0:
            return False
        usage = memory_usage_fraction()
        if usage is None or usage < threshold:
            return False
        now = time.monotonic()
        with self._activity_lock:
            should_log = now - self._admission_last_log >= 60.0
            if should_log:
                self._admission_last_log = now
        if should_log:
            logger.warning(
                "Deferring job claim: container memory at %.0f%% of limit (threshold %.0f%%)",
                usage * 100,
                threshold * 100,
            )
        return True

    def _mark_job_done(self, optimization_id: str) -> None:
        """Release the claim and clean up per-job worker state.

        Args:
            optimization_id: ID of the job that just finished.
        """
        was_claimed = False
        with self._queue_lock:
            self._processing_jobs.discard(optimization_id)
            self._cancel_events.pop(optimization_id, None)
            if optimization_id in self._claimed_jobs:
                self._claimed_jobs.discard(optimization_id)
                was_claimed = True
        if was_claimed:
            try:
                self._job_store.release_job(optimization_id, self._pod_name)
            except AttributeError:
                pass
            except Exception:
                logger.exception("release_job failed for %s", optimization_id)

    def _worker_loop(self, worker_id: int) -> None:
        """Poll for jobs and process them until stopped.

        Args:
            worker_id: Index identifying this thread for logging.
        """
        logger.info("Worker %d started (pod=%s)", worker_id, self._pod_name)

        # Top-level guard: a fatal exception inside the loop must not let
        # the worker thread die silently — health checks would still report
        # the thread alive while no jobs got picked up.
        try:
            idle_cycles = 0
            self._touch_activity(worker_id)
            while self._running:
                optimization_id = self._get_next_job()

                if optimization_id is None:
                    if self._run_hardening_job(worker_id):
                        idle_cycles = 0
                        continue
                    time.sleep(self._poll_interval)
                    idle_cycles += 1
                    # Heartbeat every ~5 min so observability dashboards
                    # can distinguish "idle but alive" from "stuck".
                    if idle_cycles % 150 == 0:
                        logger.info("Worker %d heartbeat, idle cycles: %d", worker_id, idle_cycles)
                        self._touch_activity(worker_id)
                    continue

                idle_cycles = 0
                with self._activity_lock:
                    self._thread_current_job[worker_id] = optimization_id
                self._touch_activity(worker_id)
                try:
                    with route_sandbox_logs(optimization_id, self._job_store):
                        self._process_job(optimization_id, worker_id)
                except Exception:  # isolation boundary: one bad job must not kill the worker thread
                    logger.exception("Worker %d failed processing job %s", worker_id, optimization_id)
                finally:
                    with self._activity_lock:
                        self._thread_current_job.pop(worker_id, None)
                    self._mark_job_done(optimization_id)
        except Exception:
            logger.exception("Worker %d died unexpectedly", worker_id)

        logger.info("Worker %d stopped", worker_id)

    def _run_hardening_job(self, worker_id: int) -> bool:
        """Claim and advance one queued metric-hardening job when no run waits (SKY-369).

        Args:
            worker_id: Index of the worker thread, for logging.

        Returns:
            Whether a hardening job was claimed.
        """
        engine = getattr(self._job_store, "engine", None)
        if engine is None:
            return False
        try:
            ran = process_next_hardening_job(
                engine,
                pod=self._pod_name,
                lease_seconds=self._lease_seconds,
                local_mode=settings.local_mode,
                settings=settings,
                on_renew=lambda: self._touch_activity(worker_id),
            )
        except Exception:  # isolation boundary: a hardening failure must not kill the worker thread
            logger.exception("Worker %d failed a hardening job pass", worker_id)
            return False
        if ran:
            self._touch_activity(worker_id)
        return ran

    def _process_job(self, optimization_id: str, worker_id: int) -> None:
        """Run one optimization job to completion, handling all error and cancel paths.

        Loads the payload from the job store, validates its schema, dispatches it
        through the selected execution boundary, and drains the event queue until
        the child exits. The ``BaseException`` handler at the bottom covers cancellation,
        shutdown signals (``SystemExit``/``KeyboardInterrupt``), and ordinary errors —
        each path writes the correct terminal status and fires a notification.
        Shutdown signals are re-raised after cleanup so the process can exit;
        every other exception is translated into a ``failed``/``cancelled``
        terminal status instead of propagating.

        Args:
            optimization_id: ID of the job to process.
            worker_id: Index of the calling worker thread (for activity tracking).

        Raises:
            SystemExit: Propagated after status is written, to allow shutdown.
            KeyboardInterrupt: Propagated after status is written.
        """
        logger.info("Processing job %s", optimization_id)

        overview: dict[str, Any] = {}  # pre-init so BaseException handler has a defined value even if early error
        execution_generation: int | None = None
        execution_budget_snapshot: dict[str, Any] | None = None
        run_gateway: ModelGateway | None = None
        # Legacy (unprotected) runs are billed from the worker, not a budget
        # gateway; the exception handler reads these to bill a leg that ended
        # without a successful completion.
        bill_legacy_usage = False
        result_dict: dict[str, Any] | None = None
        usage_tracker: dict[str, Any] = {}
        # Where the run was when it failed, read by run_failed telemetry.
        run_phase = "preflight"

        with self._queue_lock:
            cancel_event = self._cancel_events.get(optimization_id)

        try:
            self._raise_if_interrupted(cancel_event, optimization_id)

            job_data = self._job_store.get_job(optimization_id)
            execution_generation = job_data.get("execution_generation")
            payload_dict = job_data.get("payload")

            if not payload_dict:
                raise ValueError(f"Optimization {optimization_id} has no payload")

            overview = job_data.get("payload_overview", {})
            if isinstance(overview, str):
                try:
                    overview = json.loads(overview)
                except (json.JSONDecodeError, TypeError):
                    overview = {}
            if not isinstance(overview, dict):
                overview = {}
            optimization_type = overview.get(PAYLOAD_OVERVIEW_OPTIMIZATION_TYPE, OPTIMIZATION_TYPE_RUN)

            if optimization_type == OPTIMIZATION_TYPE_TAGGING:
                self._process_tagging_job(optimization_id, worker_id, cancel_event, payload_dict)
                return

            # Stored grid runs stay readable, but grid search is no longer an
            # executable job type; a grid row still queued from before the
            # removal fails here instead of spending anything.
            if optimization_type == OPTIMIZATION_TYPE_GRID_SEARCH or job_data.get("parent_optimization_id"):
                raise ValueError(GRID_SEARCH_RETIRED_MESSAGE)
            is_unified = optimization_type == OPTIMIZATION_TYPE_BLACKBOX and is_unified_payload(payload_dict)
            execution_payload: RunRequest | BlackboxRunRequest | UnifiedRunRequest
            if is_unified:
                execution_payload = UnifiedRunRequest.model_validate(payload_dict)
            elif optimization_type == OPTIMIZATION_TYPE_BLACKBOX:
                execution_payload = BlackboxRunRequest.model_validate(payload_dict)
            else:
                execution_payload = RunRequest.model_validate(payload_dict)

            self._update_owned_job(
                optimization_id,
                execution_generation,
                status="validating",
                message="Validating payload",
            )

            # Renew the lease before the validate/restore phase: a slow validate
            # or checkpoint-restore must not let the claim expire and get the row
            # orphan-recovered + double-run by a peer pod.
            self._touch_activity(worker_id)

            protected_execution = job_data.get("execution_budget_id") is not None
            # Black-box scoring only runs in the managed Vercel sandbox, so a
            # black-box job always requires a funded protected run: the
            # unprotected test escape hatch other run types use to dispatch a
            # local subprocess never applies to it.
            allow_unprotected = (
                self._allow_unprotected_test_execution and optimization_type != OPTIMIZATION_TYPE_BLACKBOX
            )
            if not protected_execution and not allow_unprotected:
                raise ValueError(
                    "This stored job predates protected execution. Submit it again to start a funded run in the cloud sandbox."
                )
            bill_legacy_usage = not protected_execution
            service = self._get_service()
            # Current preflight evidence already proves semantic validity inside
            # this job's selected sandbox. The legacy validators exec authored
            # modules in a broader subprocess, before that boundary exists.
            if isinstance(execution_payload, UnifiedRunRequest):
                try:
                    validate_unified_payload(execution_payload)
                    model = execution_payload.models.reflection_model
                    novelty = bool(execution_payload.optimizer.shinka and execution_payload.optimizer.shinka.novelty)
                    api_bases = _reflection_api_bases(
                        getattr(self._job_store, "engine", None),
                        execution_payload.username,
                        model,
                        execution_payload.token_source,
                        novelty,
                    )
                    reflection = model.model_dump()
                    reflection["token_source"] = model.token_source or execution_payload.token_source
                    validate_byok_novelty(reflection, novelty, api_bases=api_bases)
                except DomainError as error:
                    raise domain_service_error(error) from error
            elif isinstance(execution_payload, BlackboxRunRequest):
                try:
                    model = execution_payload.reflection_model_settings
                    api_bases = _reflection_api_bases(
                        getattr(self._job_store, "engine", None),
                        execution_payload.username,
                        model,
                        execution_payload.token_source,
                        bool(execution_payload.shinka and execution_payload.shinka.novelty),
                    )
                    validate_blackbox_payload(
                        execution_payload,
                        verify_scorer=not protected_execution,
                        api_bases=api_bases,
                    )
                except DomainError as error:
                    raise domain_service_error(error) from error
            elif optimization_type == OPTIMIZATION_TYPE_RUN and not protected_execution:
                service.validate_payload(execution_payload)

            self._touch_activity(worker_id)
            self._raise_if_interrupted(cancel_event, optimization_id)

            self._update_owned_job(
                optimization_id,
                execution_generation,
                status="running",
                message="Running optimization",
                started_at=datetime.now(UTC).isoformat(),
            )
            run_phase = "sandbox"

            budget_gateway: ModelGateway | None = None
            # The closed gateway and its detached broker: the worker stops the
            # boxes after the terminal write, so the run does not wait for them.
            detached_sandbox: tuple[ModelGateway, SandboxControl] | None = None
            run_process: mp.process.BaseProcess | None = None
            staged_repository: StagedRepository | None = None
            event_queue: Any | None = None
            result_dict = None
            subprocess_error: dict[str, Any] | None = None

            # Resume support: the worker owns a per-job base directory it seeds
            # from saved checkpoints (resume) and reads ``gepa_state.bin`` back
            # from to persist each iteration. ``None`` when the run is not
            # resumable (non-GEPA, or a store without checkpoint support),
            # keeping every other path unchanged.
            gepa_dir: Path | None = None
            checkpoint_tracker: dict[str, Any] = {
                "payload": dict(payload_dict),
                "code_version": job_data.get("code_version"),
                "generation": execution_generation,
            }
            if self._checkpoints_enabled(optimization_type) and supports_checkpoint(payload_dict):
                gepa_dir = self._prepare_gepa_dir(optimization_id)
                # Restoring checkpoint blobs from the DB can be slow; renew the
                # lease so the upcoming subprocess spawn starts with a full window.
                self._touch_activity(worker_id)

            # Preserve registry-backed service in child when using fork.
            if self._mp_start_method == "fork":
                set_fork_service(service)

            try:
                # Inject job type so subprocess can dispatch without duck-typing.
                # Pydantic ignores this unknown key during model_validate.
                payload_dict["_optimization_type"] = optimization_type
                payload_dict["_job_id"] = optimization_id
                if gepa_dir is not None:
                    payload_dict["_gepa_log_dir"] = str(gepa_dir)

                run_watcher = RunNotificationWatcher.for_run(
                    optimization_id, overview.get(PAYLOAD_OVERVIEW_USERNAME), self._job_store
                )

                # BYOK bridge: for a run that bills the user's own provider key,
                # resolve each model's key from the vault and stamp it onto the
                # payload's ModelConfigs in memory — the only seam where the key
                # crosses into the run subprocess. Never persisted: the stored overview already dropped any
                # client-supplied key.
                token_source = overview.get(PAYLOAD_OVERVIEW_TOKEN_SOURCE) or TOKEN_SOURCE_MANAGED
                byok_engine = getattr(self._job_store, "engine", None)
                if is_unified:
                    alias_unified_model_roles(payload_dict)
                elif optimization_type == OPTIMIZATION_TYPE_BLACKBOX:
                    alias_blackbox_metric_model(payload_dict)
                if byok_engine is not None and payload_uses_token_source(
                    payload_dict,
                    TOKEN_SOURCE_BYOK,
                    default_token_source=token_source,
                ):
                    try:
                        inject_byok_connections(
                            payload_dict,
                            username=execution_payload.username,
                            vault=ProviderKeyVault(engine=byok_engine),
                            default_token_source=token_source,
                        )
                    except DomainError as error:
                        raise domain_service_error(error) from error
                # Managed-run mirror of the BYOK seam: when key provisioning is
                # configured, dispatch under a per-user OpenRouter runtime key
                # whose spend limit was just synced to the account's balance —
                # a provider-side backstop on top of the ledger clamp. Any
                # failure leaves the payload untouched and the run falls back
                # to the shared gateway key.
                if byok_engine is not None and payload_uses_token_source(
                    payload_dict,
                    TOKEN_SOURCE_MANAGED,
                    default_token_source=token_source,
                ):
                    provisioner = OpenRouterKeyProvisioner(engine=byok_engine)
                    if provisioner.enabled:
                        spendable = StripeBillingService(engine=byok_engine).spendable_cents(execution_payload.username)
                        runtime_key = provisioner.ensure_runtime_key(execution_payload.username, spendable)
                        if runtime_key is not None:
                            inject_provisioned_openrouter_key(
                                payload_dict,
                                api_key=runtime_key,
                                default_token_source=token_source,
                            )

                if job_data.get("execution_budget_id") is not None:
                    if byok_engine is None:
                        raise ValueError("Protected execution requires the authoritative ledger.")
                    uses_managed_models = payload_uses_token_source(
                        payload_dict,
                        TOKEN_SOURCE_MANAGED,
                        default_token_source=token_source,
                    )
                    if uses_managed_models and settings.openrouter_api_key is None:
                        raise ValueError("Managed model roles require the configured provider route.")
                    budget_gateway = ModelGateway(
                        BudgetRuntime(
                            BudgetService(engine=byok_engine),
                            username=execution_payload.username,
                            budget_id=job_data["execution_budget_id"],
                            generation=job_data["execution_budget_generation"],
                            phase="run",
                        )
                    )
                    run_gateway = budget_gateway
                    sandbox_workflow = "anything" if optimization_type == OPTIMIZATION_TYPE_BLACKBOX else "dspy"
                    current_image = protected_image(settings, sandbox_workflow)
                    sandbox_image = (
                        self._job_store.pin_sandbox_image(optimization_id, current_image)
                        if current_image is not None
                        else None
                    )
                    bind_protected_sandbox(
                        budget_gateway,
                        settings,
                        workflow=sandbox_workflow,
                        owner_id=optimization_id,
                        image=sandbox_image,
                        guest_lifetime_seconds=payload_dict.get("deadline_seconds"),
                        parent_hosts=(
                            PACKAGE_REGISTRY_HOSTS
                            if optimization_type == OPTIMIZATION_TYPE_BLACKBOX and is_repo_payload(payload_dict)
                            else ()
                        ),
                    )
                    checkpoint_tracker.update(sandbox_image=sandbox_image)
                    parent_payload = resolve_execution_credentials(
                        payload_dict,
                        username=execution_payload.username,
                        binding_id=job_data["execution_budget_id"],
                        vault=ProtectedCredentialVault(engine=byok_engine),
                    )
                    try:
                        payload_dict = budget_gateway.protect_payload(
                            parent_payload,
                            managed_key=(
                                settings.openrouter_api_key.get_secret_value()
                                if settings.openrouter_api_key is not None
                                else ""
                            ),
                            allow_private_tools=settings.discover_allow_private,
                        )
                    except DomainError as error:
                        raise domain_service_error(error) from error
                    host = host_proposer(payload_dict)
                    if host is not None:
                        # The CLI and permission come from the stored run settings,
                        # never from the guest, and only this run's proposer gets the token.
                        host_token = bind_host_agent(
                            budget_gateway,
                            settings,
                            cli=host[0],
                            permission=host[1],
                            reasoning_effort=host_reasoning_effort(payload_dict),
                        )
                        attach_host_agent_token(payload_dict, host_token)
                    # The wizard leaves the ceiling unset for a budget without a
                    # limit, while the guest's proposer still plans its spend
                    # against one, so the parent hands it the ledger's allowance.
                    if optimization_type == OPTIMIZATION_TYPE_BLACKBOX and payload_dict.get("max_cost_cents") is None:
                        payload_dict["max_cost_cents"] = budget_gateway.cost_ceiling_cents()
                    if optimization_type == OPTIMIZATION_TYPE_BLACKBOX and is_repo_payload(payload_dict):
                        with self._renewing_lease(worker_id):
                            # Staging empties the secret list, so read the gate's inputs first.
                            secret_bindings = dict(payload_dict["target"]["repo"].get("secret_bindings") or {})
                            secret_allowed_hosts = repo_secret_allowed_hosts(payload_dict)
                            payload_dict, staged_repository = stage_repository(
                                payload_dict,
                                username=execution_payload.username,
                                binding_id=job_data["execution_budget_id"],
                                engine=byok_engine,
                            )
                            self._record_repo_commit(optimization_id, payload_dict["target"]["repo"]["commit"])
                            # Before the setup model and the scorer box, so a refused run costs nothing.
                            gate_failures = check_repository(
                                staged_repository.snapshot.archive,
                                staged_repository.secrets,
                                place=sandbox_place(settings.local_mode),
                                bindings=secret_bindings,
                                allowed_hosts=secret_allowed_hosts,
                            )
                            if gate_failures:
                                raise RepoGateError(gate_failures)
                            # The broker was bound before staging read optimize.toml, so the
                            # proposer's hosts are granted only now; with none it stays offline.
                            budget_gateway.allow_guest_hosts(
                                tuple(payload_dict[REPO_SNAPSHOT_KEY].get("proposer_hosts") or ())
                            )
                            inferred_setup = infer_repo_setup(payload_dict, staged_repository, budget_gateway)
                            if inferred_setup is not None:
                                self._record_setup_command(optimization_id, inferred_setup)
                            bind_repo_scorer(
                                payload_dict,
                                staged_repository,
                                budget_gateway,
                                owner_id=optimization_id,
                                managed_key=(
                                    settings.openrouter_api_key.get_secret_value()
                                    if settings.openrouter_api_key is not None
                                    else ""
                                ),
                            )
                            recorded = self._record_repo_environment(
                                optimization_id, payload_dict["target"]["repo"][ENVIRONMENT_KEY]
                            )
                            # Resume checks checkpoints against the stored payload,
                            # which now holds what staging recorded.
                            if recorded is not None:
                                checkpoint_tracker["payload"] = dict(recorded)
                if is_unified:
                    strip_unified_model_aliases(payload_dict)
                if has_exposed_execution_credentials(
                    payload_dict,
                    allow_parent_model_routes=budget_gateway is not None,
                ):
                    raise ValueError(
                        "The stored job contains credentials outside the trusted relay; clone and test setup again."
                    )

                execution_start_method = "spawn" if budget_gateway is not None else self._mp_start_method
                execution_context = mp.get_context("spawn") if budget_gateway is not None else self._mp_ctx
                event_queue = execution_context.Queue()
                if budget_gateway is not None:
                    budget_gateway.heartbeat = lambda: event_queue.put_nowait({"type": EVENT_HEARTBEAT})
                run_process = execution_context.Process(  # type: ignore[attr-defined]
                    target=run_vercel_dspy if budget_gateway is not None else run_service_in_subprocess,
                    args=(
                        payload_dict,
                        f"{optimization_id}-g{execution_generation}"
                        if execution_generation is not None
                        else optimization_id,
                        event_queue,
                        execution_start_method,
                    ),
                    name=f"dspy-run-{optimization_id[:8]}",
                    daemon=True,
                )
                assert run_process is not None
                run_process.start()
                run_phase = "optimize"

                # From here the child owns its copy of the payload. The
                # validated request model is a second full copy of the
                # dataset that would otherwise sit in this process for the
                # whole multi-hour run — and be duplicated into every
                # sibling job's fork image. ``payload_dict`` itself stays
                # pinned by ``run_process``'s args tuple; only ``attempts``
                # is read from ``job_data`` after this point.
                job_data = {
                    key: job_data.get(key)
                    for key in (
                        "attempts",
                        "execution_budget_id",
                        "execution_budget_generation",
                        "execution_generation",
                    )
                }
                execution_username = execution_payload.username
                execution_payload = None  # type: ignore[assignment]

                # Stall watchdog: the lease heartbeat (_touch_activity) renews
                # on every tick regardless of progress, so a child wedged on a
                # timeout-less blocking call (e.g. a hung LLM socket read) would
                # otherwise be kept "running" forever. Track the last time the
                # child emitted any event and fail the run if it goes silent past
                # the configured window. The per-request LM timeout normally trips
                # first; this only catches genuine wedges that produce nothing.
                stall_timeout = settings.job_stall_timeout_seconds
                last_event_at = time.monotonic()
                while run_process.is_alive():
                    self._raise_if_interrupted(cancel_event, optimization_id)
                    self._touch_activity(worker_id)
                    self._raise_if_store_cancelled(optimization_id)
                    run_process.join(timeout=self._cancel_poll_interval)
                    drained_result, drained_error, drained_count = self._drain_subprocess_events(
                        optimization_id,
                        event_queue,
                        generation=execution_generation,
                        checkpoint_tracker=checkpoint_tracker,
                        usage_tracker=usage_tracker,
                        run_watcher=run_watcher,
                        budget_gateway=budget_gateway,
                    )
                    if drained_result is not None:
                        result_dict = drained_result
                    if drained_error is not None:
                        subprocess_error = drained_error
                    if drained_count > 0:
                        last_event_at = time.monotonic()
                    elif stall_timeout > 0 and time.monotonic() - last_event_at > stall_timeout:
                        raise JobStalledError(
                            f"Optimization stalled: no progress for {stall_timeout:.0f}s. "
                            "The run was terminated; a model call or other operation likely "
                            "hung without making progress."
                        )
                    if gepa_dir is not None:
                        self._persist_gepa_checkpoint(optimization_id, gepa_dir, checkpoint_tracker)
                    if run_watcher is not None and budget_gateway is not None and run_watcher.spend_check_due():
                        self._observe_run_spend(optimization_id, run_watcher, budget_gateway)

                drained_result, drained_error, _ = self._drain_subprocess_events(
                    optimization_id,
                    event_queue,
                    generation=execution_generation,
                    checkpoint_tracker=checkpoint_tracker,
                    usage_tracker=usage_tracker,
                    run_watcher=run_watcher,
                )
                if drained_result is not None:
                    result_dict = drained_result
                if drained_error is not None:
                    subprocess_error = drained_error
                if gepa_dir is not None:
                    self._persist_gepa_checkpoint(optimization_id, gepa_dir, checkpoint_tracker)

                if budget_gateway is not None:
                    closing_gateway, budget_gateway = budget_gateway, None
                    broker = closing_gateway.detach_sandbox()
                    if broker is not None:
                        detached_sandbox = (closing_gateway, broker)
                    with self._renewing_lease(worker_id):
                        execution_budget_snapshot, settlement_error = self._close_budget_gateway(
                            optimization_id, closing_gateway, execution_generation
                        )
                    host_failure = host_agent_failure(closing_gateway)
                    if host_failure is not None:
                        # A capped subscription stops every later turn, so the run
                        # ends with the cap and its reset time, not a proposer error.
                        raise OptimizerProcessError(failure_message(host_failure), "HostAgentRateLimited")
                    if settlement_error is not None and subprocess_error is None:
                        raise settlement_error

                if subprocess_error:
                    traceback_text = subprocess_error.get("traceback")
                    if traceback_text:
                        logger.error("Optimization %s subprocess traceback:\n%s", optimization_id, traceback_text)
                        # Persist traceback so users can see it via GET /jobs/{id}/logs
                        with contextlib.suppress(Exception):
                            self._job_store.append_log(
                                optimization_id,
                                level="ERROR",
                                logger_name="dspy.subprocess",
                                message=traceback_text,
                            )
                    error_message = str(subprocess_error.get("error", "Unknown subprocess error"))
                    if subprocess_error.get("failure_kind") == INFRASTRUCTURE_INTERRUPTION:
                        raise InfrastructureInterruptionError(error_message)
                    raise OptimizerProcessError(error_message, subprocess_error.get("error_type") or None)

                if run_process.exitcode not in (0, None) and result_dict is None:
                    if run_process.exitcode == -9:
                        raise InfrastructureInterruptionError(
                            "The optimizer process was terminated by temporary infrastructure pressure."
                        )
                    raise RuntimeError(f"Optimization subprocess exited with code {run_process.exitcode}")

                terminal = None
                if isinstance(result_dict, dict) and "_terminal_outcome" in result_dict:
                    terminal = TerminalOutcome.model_validate(result_dict["_terminal_outcome"])
                    result_dict = terminal.result
                if result_dict is None and terminal is None:
                    raise RuntimeError("Optimization subprocess finished without a result payload")

                # Check cancel one last time: service.run() may have completed
                # during a long phase with no progress callbacks, after the
                # cancel endpoint already marked the job as cancelled.
                self._raise_if_interrupted(cancel_event, optimization_id)

                final_status = terminal.status if terminal is not None else "success"
                final_message = terminal.message if terminal is not None else "Optimization completed successfully"

                if staged_repository is not None and isinstance(result_dict, dict):
                    with self._renewing_lease(worker_id):
                        publish_improvement(
                            result_dict,
                            payload_dict,
                            staged_repository,
                            username=execution_username,
                            engine=byok_engine,
                            optimization_id=optimization_id,
                            app_url=settings.app_public_url,
                        )

                try:
                    if self._persisted_status(optimization_id) in ("cancelled", "paused"):
                        # Cancel/pause endpoint raced us past the last
                        # _raise_if_cancelled() and already wrote its terminal status
                        # to the DB. Stop cooperatively so we persist the checkpoint
                        # and never overwrite "cancelled"/"paused" with success/failed.
                        raise CancellationError()
                    # Compare-and-set the terminal write against the active
                    # statuses so a pause/cancel that landed after the status read
                    # just above isn't clobbered by this success/failed write; on a
                    # lost race we yield cooperatively. Stores without the CAS
                    # method keep last-writer-wins.
                    completion_fields: dict[str, Any] = {
                        "status": final_status,
                        "message": final_message,
                        "completed_at": datetime.now(UTC).isoformat(),
                        "result": result_dict,
                    }
                    if terminal is not None:
                        completion_fields.update(
                            stop_reason=terminal.stop_reason,
                            result_availability=terminal.result_availability,
                            terminal_evidence={
                                **terminal.evidence,
                                **(
                                    {"execution_budget": execution_budget_snapshot} if execution_budget_snapshot else {}
                                ),
                            },
                        )
                    cas = getattr(self._job_store, "update_job_if_status", None)
                    if cas is not None:
                        fence = {} if execution_generation is None else {"expected_generation": execution_generation}
                        if not cas(optimization_id, ("running", "validating"), **fence, **completion_fields):
                            raise CancellationError()
                    else:
                        self._job_store.update_job(optimization_id, **completion_fields)
                    logger.info("Optimization %s completed with status=%s", optimization_id, final_status)
                    # Success retires resume state and frees its bytes.
                    if gepa_dir is not None and final_status == "success":
                        with contextlib.suppress(Exception):
                            self._job_store.delete_gepa_checkpoint(optimization_id)
                    _username = overview.get(PAYLOAD_OVERVIEW_USERNAME, "")
                    _baseline, _optimized = (
                        result_scores(result_dict) if isinstance(result_dict, dict) else (None, None)
                    )
                    claimed = self._job_store.claim_completion_notification(optimization_id)
                    if claimed:
                        notify_job_completed(
                            optimization_id=optimization_id,
                            username=_username,
                            status=final_status,
                            message=final_message,
                            baseline_score=_baseline,
                            optimized_score=_optimized,
                        )
                        self._record_run_outcome(
                            optimization_id,
                            _username,
                            final_status,
                            overview,
                            result=result_dict,
                            payload=payload_dict,
                            phase=run_phase,
                        )
                    # Every terminal status is billed for the tokens it spent — a
                    # failed or budget-stopped run still consumed them. The
                    # per-generation settlement key keeps a redelivered leg from
                    # double-billing; a store without generations falls back to
                    # the once-only completion claim.
                    if bill_legacy_usage and (claimed or execution_generation is not None):
                        billed = self._bill_legacy_leg(
                            optimization_id,
                            overview,
                            result_dict,
                            usage_tracker,
                            generation=execution_generation,
                        )
                        # Stamp the billing outcome onto the persisted result so
                        # the result screen can show what the run cost.
                        # Re-persisted because the debit runs after the first
                        # completion write.
                        if isinstance(result_dict, dict):
                            self._stamp_billing_outcome(
                                optimization_id,
                                result_dict,
                                billed=billed,
                                estimated_low=overview.get(PAYLOAD_OVERVIEW_ESTIMATED_LOW),
                                estimated_high=overview.get(PAYLOAD_OVERVIEW_ESTIMATED_HIGH),
                            )
                    if final_status == "success":
                        self._schedule_embedding_indexing(optimization_id)
                except KeyError:
                    logger.info(
                        "Optimization %s was deleted during execution (likely cancelled), skipping result",
                        optimization_id,
                    )
            # cleanup-and-reraise: ensure the subprocess is killed on ANY exit
            # path (including shutdown signals) before the exception propagates.
            except BaseException:
                # Capture the last completed iteration's state before killing the
                # child, so a 504/stall/cancel leaves a resume point on disk.
                if gepa_dir is not None:
                    with contextlib.suppress(Exception):
                        self._persist_gepa_checkpoint(optimization_id, gepa_dir, checkpoint_tracker)
                if run_process is not None and run_process.is_alive():
                    self._terminate_run_process(run_process, optimization_id)
                    if optimization_type == OPTIMIZATION_TYPE_BLACKBOX:
                        self._stop_blackbox_sandboxes(optimization_id)
                raise
            finally:
                if budget_gateway is not None:
                    broker = budget_gateway.detach_sandbox()
                    if broker is not None:
                        detached_sandbox = (budget_gateway, broker)
                if detached_sandbox is None and staged_repository is not None:
                    # The scoring box is closed through the gateway, so it goes first.
                    staged_repository.close_scorers()
                if budget_gateway is not None:
                    # Closing waits for in-flight usage and can outlast the lease.
                    # An expired lease lets the orphan sweep fence the run while
                    # this worker still settles it, so the restart release loses.
                    with self._renewing_lease(worker_id):
                        execution_budget_snapshot, settlement_error = self._close_budget_gateway(
                            optimization_id, budget_gateway, execution_generation
                        )
                    budget_gateway = None
                    if settlement_error is not None:
                        logger.error(
                            "Optimization %s runtime cleanup needs attention: %s", optimization_id, settlement_error
                        )
                if detached_sandbox is not None:
                    # This runs after the success write and after the gateway's own
                    # snapshot. On failure, _close_failed_budget also publishes, and
                    # this stop can publish an older copy after it. Only the job's
                    # fallback copy goes stale: the API serves the live budget row.
                    self._stop_sandbox_in_background(
                        optimization_id,
                        *detached_sandbox,
                        execution_generation,
                        staged_repository.close_scorers if staged_repository is not None else None,
                    )
                if staged_repository is not None:
                    staged_repository.cleanup()
                # The DB holds the checkpoint bytes for a resumable failure; the
                # local working copy is always removed.
                if gepa_dir is not None:
                    shutil.rmtree(gepa_dir, ignore_errors=True)
                if event_queue is not None:
                    with contextlib.suppress(Exception):
                        event_queue.close()
                    with contextlib.suppress(Exception):
                        event_queue.join_thread()

        # BaseException catches SystemExit/KeyboardInterrupt during graceful shutdown
        # so we still record a terminal status for the in-flight job before propagating.
        except BaseException as exc:
            is_shutdown = isinstance(exc, SystemExit | KeyboardInterrupt)
            is_cancelled = isinstance(exc, CancellationError)
            is_temporary = isinstance(exc, InfrastructureInterruptionError) or is_shutdown
            # A legacy leg that failed, was cancelled/paused, or stalled is billed
            # for the tokens it already spent. Interruptions the platform caused
            # (host pressure, provider transport drop, shutdown) are not billed.
            if (
                bill_legacy_usage
                and execution_generation is not None
                and (not is_temporary or isinstance(exc, JobStalledError))
            ):
                with contextlib.suppress(Exception):
                    self._bill_legacy_leg(
                        optimization_id,
                        overview if isinstance(overview, dict) else {},
                        result_dict,
                        usage_tracker,
                        generation=execution_generation,
                    )
            if isinstance(exc, WorkerShutdownError):
                self._release_for_restart(optimization_id, execution_generation)
                return
            if is_cancelled:
                final_status, error_message = "cancelled", CANCELLATION_REASON
                logger.info("Optimization %s cancelled", optimization_id)
            elif is_shutdown:
                final_status = "failed"
                error_message = f"Optimization interrupted by service shutdown: {exc}"
                logger.exception("Optimization %s failed: %s", optimization_id, error_message)
            else:
                final_status = "failed"
                error_message = str(exc)
                logger.exception("Optimization %s failed: %s", optimization_id, error_message)
            _username = overview.get(PAYLOAD_OVERVIEW_USERNAME, "") if isinstance(overview, dict) else ""
            if is_cancelled:
                # The cancel/pause endpoint already wrote the terminal status
                # ("cancelled" or "paused"); the worker adds no DB write here. A
                # pause is a suspend-to-resume, not a completion, so it skips the
                # finished-job notification that a real cancel sends.
                persisted_status = None
                with contextlib.suppress(Exception):
                    persisted_status = self._persisted_status(optimization_id)
                if persisted_status == "cancelled" and self._job_store.claim_completion_notification(optimization_id):
                    notify_job_completed(optimization_id=optimization_id, username=_username, status="cancelled")
                    self._record_run_outcome(
                        optimization_id, _username, "cancelled", overview if isinstance(overview, dict) else {}
                    )
            else:
                # Failed jobs are retained so users can inspect the error
                now = datetime.now(UTC).isoformat()
                try:
                    fields = {"status": final_status, "message": error_message, "completed_at": now}
                    if is_temporary:
                        fields["stop_reason"] = "interrupted"
                    cas = getattr(self._job_store, "update_job_if_status", None)
                    if cas is not None:
                        fence = {} if execution_generation is None else {"expected_generation": execution_generation}
                        if not cas(optimization_id, ("pending", "running", "validating"), **fence, **fields):
                            return
                    else:
                        self._job_store.update_job(optimization_id, **fields)
                except Exception:  # isolation boundary: a DB hiccup must not prevent the notification below
                    logger.exception("Optimization %s: failed to update status to %s", optimization_id, final_status)
                if run_gateway is not None:
                    self._close_failed_budget(optimization_id, run_gateway, execution_generation)
                if self._job_store.claim_completion_notification(optimization_id):
                    notify_job_completed(
                        optimization_id=optimization_id,
                        username=_username,
                        status=final_status,
                        message=error_message,
                    )
                    self._record_run_outcome(
                        optimization_id,
                        _username,
                        final_status,
                        overview if isinstance(overview, dict) else {},
                        error=exc,
                        phase=run_phase,
                    )
            if is_shutdown:
                raise

    def _release_for_restart(self, optimization_id: str, generation: int | None) -> None:
        """Hand a run that this worker's shutdown interrupted to the orphan sweep.

        The run stays active instead of failing. Clearing the claim stops this
        worker's heartbeat from extending the lease, so the next sweep on a live
        pod requeues the run to resume from its checkpoint.

        Args:
            optimization_id: The interrupted run.
            generation: Execution generation this worker owns, or ``None``.
        """
        fields = {
            "message": "Waiting for a live worker after a restart",
            "recovery": {
                "state": "recovering",
                "phase": "requeue",
                "cause": WORKER_RESTART_CAUSE,
                "reason": "The worker restarted. The run continues on another worker.",
            },
            "claimed_by": None,
            "claimed_at": None,
            "lease_expires_at": None,
        }
        try:
            cas = getattr(self._job_store, "update_job_if_status", None)
            if cas is None:
                self._job_store.update_job(optimization_id, **fields)
            else:
                fence = {} if generation is None else {"expected_generation": generation}
                if not cas(optimization_id, ("validating", "running"), **fence, **fields):
                    logger.warning(
                        "Optimization %s: the restart release lost its generation %s fence; "
                        "the orphan sweep or another owner already changed the run",
                        optimization_id,
                        generation,
                    )
                    return
        except Exception:  # a failed release still leaves the lease to expire into the same sweep
            logger.exception("Optimization %s: could not release the run for a restart", optimization_id)
            return
        logger.info("Optimization %s released for recovery after a worker shutdown", optimization_id)

    def _close_failed_budget(self, optimization_id: str, gateway: ModelGateway, generation: int | None) -> None:
        """Close a failed run's budget so unstarted work stops holding wallet money.

        The gateway close already settled the metered usage. Dispatched and
        pending work, such as a sandbox the background stop still settles,
        keeps its coverage until its usage is known.

        Args:
            optimization_id: Failed job owning the budget.
            gateway: The run's closed gateway, still holding its budget authority.
            generation: Worker epoch allowed to publish the closed snapshot.
        """
        runtime = gateway.runtime
        try:
            runtime.service.close_for_failed_job(
                runtime.budget_id, runtime.username, expected_generation=runtime.generation
            )
        except Exception:  # isolation boundary: the failure notification must still go out
            logger.exception("Optimization %s: could not close its run budget", optimization_id)
            return
        # The terminal evidence froze the budget before this close.
        self._publish_budget_snapshot(optimization_id, gateway, generation)

    def _close_budget_gateway(
        self, optimization_id: str, gateway: ModelGateway, generation: int | None
    ) -> tuple[dict[str, Any] | None, BaseException | None]:
        """Settle owned runtimes before terminal publication while retaining uncertain usage.

        Args:
            optimization_id: Stable job owning all covered work.
            gateway: Trusted parent transport and its cumulative budget.
            generation: Worker epoch allowed to publish the settlement snapshot.

        Returns:
            Authoritative snapshot and a genuine cleanup error, if one occurred.
        """
        error: BaseException | None = None
        try:
            gateway.close()
        except UsagePendingError:
            logger.info("Optimization %s finished with usage awaiting reconciliation", optimization_id)
        except BaseException as exc:
            error = exc
        return self._publish_budget_snapshot(optimization_id, gateway, generation), error

    def _stop_sandbox_in_background(
        self,
        optimization_id: str,
        gateway: ModelGateway,
        broker: SandboxControl,
        generation: int | None,
        close_scorers: Callable[[], None] | None,
    ) -> None:
        """Stop the run's boxes and settle their usage on a worker-owned thread.

        The thread is not a daemon, so the process keeps it until the stop
        ends, and ``stop`` waits for it. If the process dies first, the sandbox
        sweep stops the boxes of the terminal job and the usage reconciler
        settles the open operations.

        Args:
            optimization_id: Stable job owning the boxes.
            gateway: The closed gateway, still holding the run budget.
            broker: The broker taken out of ``gateway``.
            generation: Worker epoch allowed to publish the settled snapshot.
            close_scorers: Closes the parent's scoring boxes first, when the run has them.
        """
        thread = threading.Thread(
            target=self._finish_sandbox_stop,
            args=(optimization_id, gateway, broker, generation, close_scorers),
            name=f"sandbox-stop-{optimization_id}",
        )
        with self._queue_lock:
            self._sandbox_stops.add(thread)
        thread.start()

    def _finish_sandbox_stop(
        self,
        optimization_id: str,
        gateway: ModelGateway,
        broker: SandboxControl,
        generation: int | None,
        close_scorers: Callable[[], None] | None,
    ) -> None:
        """Stop the boxes, settle their usage and publish the settled budget.

        A failed stop leaves the budget operations open, so the budget stays
        Pending until the usage reconciler settles them.

        Args:
            optimization_id: Stable job owning the boxes.
            gateway: The closed gateway, still holding the run budget.
            broker: The broker taken out of ``gateway``.
            generation: Worker epoch allowed to publish the settled snapshot.
            close_scorers: Closes the parent's scoring boxes first, when the run has them.
        """
        try:
            if close_scorers is not None:
                close_scorers()
            broker.close()
        except UsagePendingError:
            logger.info("Optimization %s finished with usage awaiting reconciliation", optimization_id)
        except Exception:  # isolation boundary: the sweep and the reconciler finish a failed stop
            logger.exception("Optimization %s could not stop its sandbox boxes", optimization_id)
        finally:
            try:
                self._publish_budget_snapshot(optimization_id, gateway, generation)
            finally:
                with self._queue_lock:
                    self._sandbox_stops.discard(threading.current_thread())

    def _publish_budget_snapshot(
        self, optimization_id: str, gateway: ModelGateway, generation: int | None
    ) -> dict[str, Any] | None:
        """Store the run budget's current state in the job's terminal evidence.

        Args:
            optimization_id: Stable job owning all covered work.
            gateway: Trusted parent transport and its cumulative budget.
            generation: Worker epoch allowed to publish the snapshot.

        Returns:
            The published snapshot, or ``None`` when the budget could not be read.
        """
        snapshot: dict[str, Any] | None = None
        try:
            raw = asdict(gateway.runtime.service.get(gateway.runtime.budget_id, gateway.runtime.username))
            raw.pop("username", None)
            raw.pop("account_available_cents", None)
            snapshot = json.loads(json.dumps(raw, default=str))
            job = self._job_store.get_job(optimization_id)
            evidence = {**(job.get("terminal_evidence") or {}), "execution_budget": snapshot}
            cas = getattr(self._job_store, "update_job_if_status", None)
            if cas is not None:
                kwargs = {} if generation is None else {"expected_generation": generation}
                cas(
                    optimization_id,
                    ("pending", "validating", "running", "success", "failed", "stopped", "paused", "cancelled"),
                    terminal_evidence=evidence,
                    **kwargs,
                )
            else:
                self._job_store.update_job(optimization_id, terminal_evidence=evidence)
        except Exception:
            logger.exception("Optimization %s could not publish its budget snapshot", optimization_id)
        return snapshot

    def _process_tagging_job(
        self,
        optimization_id: str,
        worker_id: int,
        cancel_event: threading.Event | None,
        payload_dict: dict[str, Any],
    ) -> None:
        """Run a bulk auto-tag job in the worker thread (no subprocess).

        The batch loop lives in :mod:`core.worker.tagging_job`; this wrapper
        owns the job-row lifecycle: the ``running`` transition, the heartbeat
        the loop's monitor thread calls to renew the claim lease, and the
        terminal write. A user cancel is observed cooperatively (the route
        wrote the terminal ``cancelled`` status already, so no write happens
        here); a lease lost to a peer pod abandons silently. Failures
        propagate to ``_process_job``'s generic handler.

        Args:
            optimization_id: ID of the claimed job.
            worker_id: Index of the calling worker thread (lease renewal).
            cancel_event: The job's cooperative cancel flag.
            payload_dict: The stored ``TaggingAutotagPayload`` dict.
        """
        payload = TaggingAutotagPayload.model_validate(payload_dict)
        self._job_store.update_job(
            optimization_id,
            status="running",
            message="Auto-tagging dataset rows",
            started_at=datetime.now(UTC).isoformat(),
        )
        self._touch_activity(worker_id)
        outcome = run_autotag_job(
            self._job_store,
            optimization_id,
            payload.session_id,
            username=payload.username,
            cancel_event=cancel_event or threading.Event(),
            heartbeat=lambda: self._touch_activity(worker_id),
        )
        if outcome.get("status") != "done":
            logger.info("Tagging job %s ended without completion: %s", optimization_id, outcome)
            return
        completion_fields: dict[str, Any] = {
            "status": "success",
            "message": f"Tagged {outcome.get('rows_tagged', 0)} rows",
            "completed_at": datetime.now(UTC).isoformat(),
            "result": outcome,
        }
        cas = getattr(self._job_store, "update_job_if_status", None)
        if cas is not None:
            if not cas(optimization_id, ("running", "validating"), **completion_fields):
                # A cancel raced the finish; its terminal status stands.
                return
        else:
            self._job_store.update_job(optimization_id, **completion_fields)
        logger.info("Tagging job %s completed", optimization_id)

    def start(self) -> None:
        """Start the background worker threads and begin polling.

        Idempotent: a second call while ``_running`` is true is a no-op.
        """
        if self._running:
            return

        self._running = True
        for i in range(self._num_workers):
            # Non-daemon: the SIGTERM handler joins these threads explicitly so
            # in-flight subprocesses get a chance to terminate cleanly.
            thread = threading.Thread(
                target=self._worker_loop,
                args=(i,),
                name=f"dspy-worker-{i}",
            )
            thread.start()
            self._threads.append(thread)
        # A fresh event per start, so a sweeper from an earlier start cannot miss its stop.
        self._sweep_stop = threading.Event()
        threading.Thread(
            target=self._sandbox_sweep_loop, args=(self._sweep_stop,), name="sandbox-sweeper", daemon=True
        ).start()

        logger.info("Started %d background workers", self._num_workers)

    def stop(self, timeout: float = 30.0) -> None:
        """Signal all workers to stop and wait for them to finish.

        Clears the pending queue, sets the cancel event for every tracked job so
        in-flight subprocesses terminate promptly, and joins each worker thread
        with a share of the total ``timeout``.

        Args:
            timeout: Total seconds shared across all worker thread joins.
        """
        if not self._running:
            return

        self._running = False
        self._sweep_stop.set()

        # Request cooperative cancellation for pending/running jobs so workers
        # can terminate subprocess execution promptly during shutdown.
        with self._queue_lock:
            self._pending_jobs.clear()
            for event in self._cancel_events.values():
                event.set()

        if not self._threads:
            return
        # Worker threads run concurrently, so a single shared deadline lets any
        # one in-flight job use up to the full ``timeout`` to finish its current
        # DSPy/LLM call — instead of a fixed ``timeout / N`` slice that reaps a
        # slow child early — while still bounding total shutdown to ``timeout``.
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)
        # The worker threads start these stops, so they are joined last.
        with self._queue_lock:
            sandbox_stops = list(self._sandbox_stops)
        for thread in sandbox_stops:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)

        self._threads.clear()
        logger.info("Stopped background workers")

    def is_running(self) -> bool:
        """Return True if the worker has been started.

        Returns:
            ``True`` while ``start`` has been called and ``stop`` has not.
        """
        return self._running

    def _record_setup_command(self, optimization_id: str, command: str) -> None:
        """Store an inferred repository setup command on the run's own payload.

        The run's configuration page then shows what was inferred, and a
        resumed run reuses it instead of asking again.

        Args:
            optimization_id: The repository run.
            command: The inferred setup command.
        """
        stored = self._job_store.get_job(optimization_id).get("payload")
        if isinstance(stored, dict) and isinstance(stored.get("target"), dict):
            stored["target"]["setup_command"] = command
            self._job_store.update_job(optimization_id, payload=stored)

    def _record_repo_environment(self, optimization_id: str, environment: dict[str, Any]) -> dict[str, Any] | None:
        """Store the environment a repository run is held to on the run's own payload.

        The run then shows what it could and could not do, and a resumed run
        is refused if it would arrive at different rules.

        Args:
            optimization_id: The repository run.
            environment: The effective environment worked out at staging.

        Returns:
            The run's stored payload after the update, or ``None`` when it
            is not a repository payload.
        """
        stored = self._job_store.get_job(optimization_id).get("payload")
        target = stored.get("target") if isinstance(stored, dict) else None
        repo = target.get("repo") if isinstance(target, dict) else None
        if not isinstance(repo, dict):
            return None
        if repo.get(ENVIRONMENT_KEY) != environment:
            repo[ENVIRONMENT_KEY] = environment
            self._job_store.update_job(optimization_id, payload=stored)
        return stored

    def _record_repo_commit(self, optimization_id: str, commit: str) -> None:
        """Store the commit a repository run was pinned to on the run's own payload.

        A run submitted on a branch is pinned at staging; recording it keeps a
        resumed run on the same tree and lets the run page read the files its
        versions patch.

        Args:
            optimization_id: The repository run.
            commit: The pinned 40-character commit id.
        """
        stored = self._job_store.get_job(optimization_id).get("payload")
        target = stored.get("target") if isinstance(stored, dict) else None
        repo = target.get("repo") if isinstance(target, dict) else None
        if isinstance(repo, dict) and repo.get("commit") != commit:
            repo["commit"] = commit
            self._job_store.update_job(optimization_id, payload=stored)

    @contextlib.contextmanager
    def _renewing_lease(self, worker_id: int) -> Iterator[None]:
        """Keep this worker's lease alive through a blocking step in the job thread.

        Repository staging and pull-request publishing run synchronously and can
        outlast the lease window; without renewal the orphan sweeper fails a run
        whose worker is still busy with it.

        Args:
            worker_id: Index of the worker thread that owns the current job.

        Yields:
            Nothing; the lease is renewed until the block exits.
        """
        done = threading.Event()

        def _renew() -> None:
            """Renew the lease every third of its window until the block exits."""
            while not done.wait(self._lease_seconds / 3):
                self._touch_activity(worker_id)

        renewer = threading.Thread(target=_renew, name=f"lease-renew-{worker_id}", daemon=True)
        self._touch_activity(worker_id)
        renewer.start()
        try:
            yield
        finally:
            done.set()
            renewer.join()
            self._touch_activity(worker_id)

    def _touch_activity(self, worker_id: int) -> None:
        """Record liveness and renew the lease on this worker's current job.

        Renewing the lease here keeps the lease window short (so a dead pod
        is reclaimed quickly) without us having to schedule a separate
        heartbeat thread — the same call sites that prove this thread is
        alive also prove that its claim is still valid.

        Args:
            worker_id: Index of the worker thread reporting liveness.
        """
        with self._activity_lock:
            self._last_activity[worker_id] = time.monotonic()
            current_job = self._thread_current_job.get(worker_id)
        if current_job is not None and current_job in self._claimed_jobs:
            try:
                still_owned = self._job_store.extend_lease(current_job, self._pod_name, self._lease_seconds)
            except AttributeError:
                still_owned = True
            except Exception:
                logger.exception("extend_lease failed for %s", current_job)
                still_owned = True
            if not still_owned:
                # Another pod stole the lease (we hung past the window). Cancel
                # ourselves so we abandon the run instead of double-processing.
                logger.warning(
                    "Lease for %s was stolen from %s — cancelling local run",
                    current_job,
                    self._pod_name,
                )
                with self._queue_lock:
                    event = self._cancel_events.get(current_job)
                if event is not None:
                    event.set()

    def _pace_run_budget(
        self,
        optimization_id: str,
        gateway: ModelGateway,
        tracker: dict[str, Any],
        done_calls: int,
    ) -> dict[str, Any]:
        """Price the run's next round from its own spend and stop it when the budget cannot cover it.

        Called on each optimizer bar tick. Admission closes with
        ``budget_reached``, so the optimizer's next paid call is refused,
        it unwinds through ``BudgetReached`` and keeps its best version, and
        the run ends as a clean budget stop rather than a failure. A run without
        a spending limit is never stopped here; it draws on the account until
        the ledger cannot fund the next operation.

        Args:
            optimization_id: The running job.
            gateway: Trusted parent transport whose runtime owns the budget.
            tracker: Run cursor holding the planned count and the last saved
                state's iteration and metric calls.
            done_calls: Scorer runs completed so far.

        Returns:
            Live estimate fields for the progress event.
        """
        if done_calls == tracker.get("_paced_calls"):
            return tracker.get("_pace", {})
        runtime = gateway.runtime
        snapshot = runtime.service.get(runtime.budget_id, runtime.username)
        pace = budget_pace(
            snapshot,
            done_calls=done_calls,
            planned_calls=tracker.get("planned_calls"),
            iteration_calls=calls_per_iteration(tracker.get(-1)),
        )
        if pace.stop and snapshot.state == "attached" and not tracker.get("_budget_stopped"):
            tracker["_budget_stopped"] = True
            logger.info(
                "Optimization %s: %s cents left cannot fund another %s-call round at %s cents per call; stopping",
                optimization_id,
                snapshot.available_cents,
                pace.calls_per_iteration,
                pace.cents_per_call,
            )
            runtime.service.stop_admission(runtime.budget_id, runtime.username, reason=STOP_REASON_BUDGET_REACHED)
        tracker["_paced_calls"] = done_calls
        tracker["_pace"] = pace_metrics(pace)
        return tracker["_pace"]

    def _observe_run_spend(
        self, optimization_id: str, run_watcher: RunNotificationWatcher, gateway: ModelGateway
    ) -> None:
        """Feed the run's settled spend to its email watcher for the budget alert.

        Args:
            optimization_id: The running job.
            run_watcher: The run's in-run email watcher.
            gateway: Trusted parent transport whose runtime owns the budget.
        """
        runtime = gateway.runtime
        try:
            snapshot = runtime.service.get(runtime.budget_id, runtime.username)
        except Exception:
            # A ledger read hiccup only delays the alert to the next check.
            logger.warning("Optimization %s: spend read for budget alert failed", optimization_id, exc_info=True)
            return
        if snapshot.uncapped:
            return
        run_watcher.observe_spend(float(snapshot.setup_spent_cents + snapshot.run_spent_cents), snapshot.total_cents)

    def _raise_if_store_cancelled(self, optimization_id: str) -> None:
        """Raise ``CancellationError`` when a peer pod has cancelled/paused the job.

        A cancel or pause that lands on a different replica only flips the DB
        status — it can't reach this pod's in-memory cancel event. Polling the
        lightweight status column on each lease-heartbeat tick makes cancellation
        cross-pod, so a running subprocess stops promptly instead of burning its
        full LLM budget to completion. A store without the lightweight read
        (legacy/in-memory) is a no-op.

        Args:
            optimization_id: ID of the job whose persisted status to check.

        Raises:
            CancellationError: When the persisted status is cancelled/paused.
        """
        status_fields = getattr(self._job_store, "get_job_status_fields", None)
        if status_fields is None:
            return
        try:
            status = status_fields(optimization_id).get("status")
        except Exception:
            # A transient read failure must not abort an otherwise-healthy run;
            # the next tick retries.
            return
        if status in ("cancelled", "paused"):
            raise CancellationError()

    def _schedule_embedding_indexing(self, optimization_id: str) -> None:
        """Fire-and-forget embed the finished job for the explore search index.

        Runs on a daemon thread so a slow LLM call or a missing pgvector
        extension can never block the worker's hot path. Failures are
        swallowed — the job itself is already marked success; the index
        is best-effort and the startup backfill heals any gaps.

        Args:
            optimization_id: ID of the just-finished job to index.
        """
        threading.Thread(
            target=self._embed_finished_job_best_effort,
            args=(optimization_id,),
            name=f"embed-{optimization_id[:8]}",
            daemon=True,
        ).start()

    def _embed_finished_job_best_effort(self, optimization_id: str) -> None:
        """Embed a finished job, swallowing failures so they never reach the worker.

        A missing pgvector extension or LLM credentials issue only surfaces
        on the indexing thread, never on the worker hot path.

        Args:
            optimization_id: ID of the finished job to embed.
        """
        try:
            embed_finished_job(optimization_id, job_store=self._job_store)
        except Exception as exc:  # isolation boundary: best-effort indexing must never impact job status
            logger.debug("Embedding indexing for %s failed: %s", optimization_id, exc)

    def _record_run_outcome(
        self,
        optimization_id: str,
        username: str,
        status: str,
        overview: dict[str, Any],
        *,
        result: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        error: BaseException | None = None,
        phase: str | None = None,
    ) -> None:
        """Emit the ``run_completed`` / ``run_failed`` / ``run_cancelled`` milestone.

        Called next to :func:`notify_job_completed` (inside the same exactly-once
        completion claim) so each run yields one outcome event, mirroring the
        browser's ``run_submitted``. Only structural descriptors are recorded —
        no error text, no run name — and a telemetry failure never touches the
        run's status. ``run_completed`` adds the score, held-out and sample-data
        facts; ``run_failed`` adds the failure stage and exception class.

        Args:
            optimization_id: Finished job id (logged only, never exported).
            username: Owner of the run.
            status: Terminal status (``success``, ``failed`` or ``cancelled``).
            overview: The job's ``payload_overview`` mapping.
            result: The stored result of a finished run, if any.
            payload: The stored job payload, read for its dataset rows.
            error: The exception that ended a failed run, if any.
            phase: The worker's phase when the run ended.
        """
        name = {"success": "run_completed", "cancelled": "run_cancelled", "stopped": "run_stopped"}.get(
            status, "run_failed"
        )
        try:
            properties: dict[str, Any] = {
                "status": status,
                "optimization_type": overview.get(PAYLOAD_OVERVIEW_OPTIMIZATION_TYPE),
                "optimizer": overview.get(PAYLOAD_OVERVIEW_OPTIMIZER_NAME),
                "model": overview.get(PAYLOAD_OVERVIEW_MODEL_NAME),
                "token_source": overview.get(PAYLOAD_OVERVIEW_TOKEN_SOURCE),
            }
            if name == "run_completed":
                properties.update(completed_properties(result, payload, SAMPLE_DATASETS))
            elif name == "run_failed":
                properties.update(failed_properties(error, phase))
            record_server_event(
                getattr(self._job_store, "engine", None),
                username=username or None,
                name=name,
                properties=properties,
            )
        except Exception:  # isolation boundary: telemetry must never affect a run outcome
            logger.debug("Optimization %s: run outcome telemetry failed", optimization_id, exc_info=True)

    def _debit_run_cents(
        self,
        username: str,
        result_dict: dict[str, Any] | None,
        *,
        run_name: str,
        model: str | None,
        optimization_id: str | None = None,
        token_source: str = TOKEN_SOURCE_MANAGED,
        token_sources_by_model: dict[str, str] | None = None,
        settlement_key: str | None = None,
    ) -> int:
        """Debit a finished run's cost from the account's local ledger.

        Writes a signed ``run`` row and decrements the account's grant/balance via
        :meth:`StripeBillingService.debit_run`. Runs inline (not on a daemon
        thread) so the wallet visibly reflects the spend the moment the run lands,
        but wrapped so a billing-DB hiccup can never flip job status — the local
        ledger is the balance source of truth, independent of whether Stripe is
        configured. A managed run is charged its full per-token cost; a BYOK run is
        charged no model fee because the provider bills the user's key. A no-op
        when the store exposes no SQL engine (legacy/in-memory),
        the caller is anonymous, or the run reported no token usage. A failed debit
        is logged at ``ERROR`` (forwarded as an alert) and retried at the fallback
        frontier price, so a metering fault never gives the tokens away.

        Args:
            username: Account the run is billed to.
            result_dict: The serialized run/grid result; its ``usage_by_model`` is
                priced per-model into the charge (falling back to ``total_tokens``).
            run_name: Run name for the ledger row's human label.
            model: Model id stamped on the ledger row, or ``None``.
            optimization_id: Finished legacy job whose own wallet commitment is consumed.
            token_source: ``"managed"`` (full cost) or ``"byok"`` (no model charge);
                defaults to managed.
            token_sources_by_model: Optional model-to-source map for mixed jobs.
            settlement_key: Idempotency key for this leg's debit, so a retried
                or re-claimed leg is never charged twice.

        Returns:
            The cents charged (``0`` when nothing was billed or the debit was
            skipped/failed).
        """
        engine = getattr(self._job_store, "engine", None)
        if engine is None or not username:
            return 0
        usages = _usages_from_result(result_dict, model)
        if not usages:
            return 0
        service = StripeBillingService(engine=engine)
        billing_kwargs: dict[str, Any] = {
            "model": model,
            "description": run_name or "Run",
            "token_source": token_source,
        }
        if token_sources_by_model is not None:
            billing_kwargs["token_sources_by_model"] = token_sources_by_model
        if optimization_id is not None:
            billing_kwargs["optimization_id"] = optimization_id
        if settlement_key is not None:
            billing_kwargs["settlement_key"] = settlement_key
        try:
            return service.debit_run(username, usages, **billing_kwargs)
        except Exception:  # isolation boundary: a debit failure must never impact job status
            logger.exception(
                "Balance debit failed for %s (job %s, %s); charging at fallback price: %s",
                username,
                optimization_id,
                run_name or "Run",
                ", ".join(f"{u.model}={u.input_tokens}/{u.output_tokens}" for u in usages),
            )
            return debit_at_fallback_price(service, username, usages, **billing_kwargs)

    def _bill_legacy_leg(
        self,
        optimization_id: str,
        overview: dict[str, Any],
        result_dict: dict[str, Any] | None,
        usage_tracker: dict[str, Any],
        *,
        generation: int | None,
        commitment_job_id: str | None = None,
    ) -> int:
        """Bill one execution leg of a legacy (unprotected) job, whatever its outcome.

        Prices the result's usage when the leg delivered one, otherwise the last
        cumulative usage snapshot the child reported before it failed or was
        stopped. Usage the child spent after its last event is not visible here.

        Args:
            optimization_id: The job whose leg is billed.
            overview: The job's payload overview (username, model, token source).
            result_dict: The leg's result payload, when one arrived.
            usage_tracker: Latest ``usage_by_model`` snapshot from the child's events.
            generation: The leg's execution generation; keys the idempotent debit.
            commitment_job_id: Job holding the wallet commitment this debit may
                consume; defaults to ``optimization_id``.

        Returns:
            The cents charged.
        """
        source: dict[str, Any] | None = result_dict if _usages_from_result(result_dict, None) else None
        if source is None and usage_tracker.get("usage_by_model"):
            source = {"usage_by_model": usage_tracker["usage_by_model"]}
        if source is None:
            return 0
        return self._debit_run_cents(
            overview.get(PAYLOAD_OVERVIEW_USERNAME, ""),
            source,
            optimization_id=commitment_job_id or optimization_id,
            run_name=overview.get(PAYLOAD_OVERVIEW_NAME) or "",
            model=overview.get(PAYLOAD_OVERVIEW_MODEL_NAME),
            token_source=overview.get(PAYLOAD_OVERVIEW_TOKEN_SOURCE) or TOKEN_SOURCE_MANAGED,
            token_sources_by_model=overview.get(PAYLOAD_OVERVIEW_TOKEN_SOURCES_BY_MODEL),
            settlement_key=_legacy_settlement_key(optimization_id, generation),
        )

    def _stamp_billing_outcome(
        self,
        optimization_id: str,
        result_dict: dict[str, Any] | None,
        *,
        billed: int,
        estimated_low: int | None = None,
        estimated_high: int | None = None,
    ) -> None:
        """Record the run's billed cost on its result for the result screen.

        Writes ``result['details']['billing']`` — ``{outcome: "billed", cents}``
        where ``cents`` is the amount charged. When the run was submitted with a
        projected bracket, ``estimated_low``/``estimated_high`` are echoed
        alongside so the estimate can be reconciled against the actual charge.
        Only stamps single-run results (a grid envelope has no per-run
        ``details``) and only when a charged amount exists, so a free-grant run
        that cost nothing adds no row. Re-persists the result via the job store
        because the debit runs after the first completion write; wrapped so a
        store hiccup can never flip job status.

        Args:
            optimization_id: The finished run whose result is updated.
            result_dict: The serialized run result; mutated in place and re-saved.
            billed: Cents charged by :meth:`_debit_run_cents`.
            estimated_low: Low end of the projected cost bracket, or None when
                the run carried no estimate.
            estimated_high: High end of the projected cost bracket, or None.
        """
        if not isinstance(result_dict, dict) or "pair_results" in result_dict:
            return
        outcome = "billed"
        cents = billed
        if cents <= 0:
            return
        try:
            details = result_dict.get("details")
            if not isinstance(details, dict):
                details = {}
                result_dict["details"] = details
            billing: dict[str, Any] = {"outcome": outcome, "cents": cents}
            if estimated_low is not None and estimated_high is not None:
                billing["estimated_low"] = estimated_low
                billing["estimated_high"] = estimated_high
            details["billing"] = billing
            self._job_store.update_job(optimization_id, result=result_dict)
        except Exception as exc:  # isolation boundary: stamping must never impact job status
            logger.debug("Billing-outcome stamp for %s failed: %s", optimization_id, exc)

    def _terminate_run_process(self, run_process: mp.process.BaseProcess, optimization_id: str) -> None:
        """Terminate a still-running job subprocess, escalating to SIGKILL after a 3-second grace period.

        Sends SIGTERM, waits up to 3 s, then calls ``kill()`` if the process is
        still alive and the platform supports it.  A final 2-second join follows
        before logging the outcome.  Never raises regardless of process state.

        Args:
            run_process: The job subprocess to terminate.
            optimization_id: ID embedded in the resulting log lines.
        """
        run_process.terminate()
        run_process.join(timeout=3.0)
        if run_process.is_alive() and hasattr(run_process, "kill"):
            run_process.kill()
            run_process.join(timeout=2.0)
        if run_process.is_alive():
            logger.error("Optimization %s subprocess did not terminate cleanly", optimization_id)
        else:
            logger.info("Optimization %s subprocess terminated", optimization_id)

    def _sandbox_sweep_loop(self, stop_event: threading.Event) -> None:
        """Sweep the boxes of terminal jobs every interval until the worker stops.

        Args:
            stop_event: Set by ``stop`` to end this loop.
        """
        while not stop_event.wait(_SANDBOX_SWEEP_INTERVAL_SECONDS):
            try:
                self._sweep_terminal_job_sandboxes()
            except Exception:  # isolation boundary: a failed sweep must not end the sweeper
                logger.warning("Terminal job sandbox sweep failed", exc_info=True)

    def _sweep_terminal_job_sandboxes(self) -> int:
        """Stop the boxes that recently finished jobs still have running.

        Covers boxes that the per-job stop missed, for example when a worker
        died before it ran. Only jobs that ended within the box lifetime
        ceiling can still own a running box. The sweep stops boxes and never
        deletes snapshots.

        Returns:
            The number of jobs whose boxes the sweep tried to stop, or ``0``
            when the store has no engine or a peer holds the sweep lock.
        """
        engine = getattr(self._job_store, "engine", None)
        if engine is None:
            return 0
        cutoff = datetime.now(UTC) - timedelta(seconds=settings.vercel_sandbox_max_lifetime_seconds)
        swept = 0
        with engine.begin() as conn:
            if engine.dialect.name == "postgresql":
                acquired = conn.execute(
                    text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": _SANDBOX_SWEEP_LOCK_KEY}
                ).scalar()
                if not acquired:
                    return 0
            job_ids = (
                conn.execute(
                    select(JobModel.optimization_id).where(
                        JobModel.status.in_([str(status) for status in _SANDBOX_SWEEP_STATUSES]),
                        JobModel.completed_at >= cutoff,
                    )
                )
                .scalars()
                .all()
            )
            for optimization_id in job_ids:
                # A failed, cancelled, stopped or paused run can resume in place after the query.
                try:
                    if self._persisted_status(optimization_id) not in _SANDBOX_SWEEP_STATUSES:
                        continue
                except KeyError:
                    continue
                self._stop_blackbox_sandboxes(optimization_id)
                swept += 1
        return swept

    def _stop_blackbox_sandboxes(self, optimization_id: str) -> None:
        """Stop the sandboxes a killed black-box job left running on Vercel or local Docker.

        The child closes its boxes on a normal exit, but a cancel, a stall or
        a shutdown kills it before its ``finally`` blocks run, and a scorer
        box would otherwise stay up until the lifetime ceiling. Never raises.

        Args:
            optimization_id: The job whose boxes to stop.
        """
        try:
            stopped = stop_job_sandboxes(
                optimization_id, local_mode=settings.local_mode, docker_bin=settings.docker_bin
            )
        except Exception as exc:  # isolation boundary: a failed sweep must not mask the job's outcome
            logger.warning("Optimization %s: sandbox sweep failed: %s", optimization_id, exc)
            return
        if not stopped:
            return
        logger.info("Optimization %s: stopped %d sandbox(es) the job left running", optimization_id, stopped)
        with contextlib.suppress(Exception):
            self._job_store.append_log(
                optimization_id,
                level="INFO",
                logger_name="core.worker",
                message=f"Stopped {stopped} sandbox(es) the job left running",
            )

    def _drain_subprocess_events(
        self,
        optimization_id: str,
        event_queue: Any,
        *,
        generation: int | None = None,
        checkpoint_tracker: dict[str, Any] | None = None,
        usage_tracker: dict[str, Any] | None = None,
        run_watcher: RunNotificationWatcher | None = None,
        budget_gateway: ModelGateway | None = None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, int]:
        """Drain all pending events from the subprocess queue, routing each by type.

        Handles the event types emitted by ``run_service_in_subprocess``:
        ``EVENT_PROGRESS`` → ``job_store.record_progress``; ``EVENT_LOG`` →
        ``job_store.append_log``; ``EVENT_AGENT_RUN`` → ``job_store.save_agent_run``
        or ``append_agent_run_transcript`` (on stores that keep agent runs);
        ``EVENT_RESULT`` → captured as the return value; ``EVENT_ERROR`` →
        captured as the error return value.  Store errors are swallowed so a DB
        hiccup cannot abort an otherwise-healthy optimization.

        Args:
            optimization_id: ID the events are persisted under.
            event_queue: The shared multiprocessing queue to drain.
            generation: Source worker publication epoch.
            checkpoint_tracker: Mutable checkpoint cursor that receives the best
                completed candidate for each GEPA pair.
            usage_tracker: Mutable dict that receives the latest cumulative
                ``usage_by_model`` snapshot a progress or error event carried,
                so a run that ends without a result can still be billed.
            run_watcher: The run's in-run email watcher, fed each persisted
                progress event.
            budget_gateway: The run's trusted transport; when set, each
                optimizer bar tick is priced against the budget and carries
                the live estimate.

        Returns:
            ``(result_dict, error_dict, drained_count)`` — the first two may be
            ``None`` if the corresponding event was not present; ``drained_count``
            is the number of events consumed, used by the stall watchdog as the
            liveness signal (any event proves the child is still doing work).
        """
        result_payload: dict[str, Any] | None = None
        error_payload: dict[str, Any] | None = None
        drained_count = 0
        # One insert per drain tick instead of one per line: a sandbox run can
        # emit hundreds of events a second, and the drain already runs every tick.
        log_batch: list[dict[str, Any]] = []
        while True:
            try:
                event = event_queue.get_nowait()
            except queue.Empty:
                break
            except Exception:  # isolation boundary: a broken queue must not crash the job-processing loop
                logger.exception("Optimization %s: event queue read failed; stopping drain", optimization_id)
                break

            drained_count += 1
            event_type = event.get("type")
            usage_rows = event.get("usage_by_model")
            if usage_tracker is not None and isinstance(usage_rows, list) and usage_rows:
                usage_tracker["usage_by_model"] = usage_rows
            if event_type == EVENT_PROGRESS:
                try:
                    metrics = event.get("metrics") or {}
                    if checkpoint_tracker is not None:
                        planned_calls = planned_calls_from_progress(metrics)
                        if planned_calls is not None:
                            checkpoint_tracker["planned_calls"] = planned_calls
                        done_calls = done_calls_from_progress(metrics)
                        if budget_gateway is not None and done_calls is not None:
                            try:
                                metrics = {
                                    **metrics,
                                    **self._pace_run_budget(
                                        optimization_id, budget_gateway, checkpoint_tracker, done_calls
                                    ),
                                }
                            except Exception:
                                logger.exception("Optimization %s: budget pace check failed", optimization_id)
                        incumbent = evaluated_incumbent_from_progress(
                            event.get("event"),
                            metrics,
                            checkpoint_tracker.get("payload", {}),
                        )
                        if incumbent is not None:
                            pair_index = metrics.get("pair_index")
                            pair_key = (
                                pair_index if isinstance(pair_index, int) and not isinstance(pair_index, bool) else -1
                            )
                            incumbents = checkpoint_tracker.setdefault("_incumbents", {})
                            previous = incumbents.get(pair_key)
                            if previous is None or incumbent["selection_score"] > previous["selection_score"]:
                                incumbents[pair_key] = incumbent
                    fenced_progress = getattr(self._job_store, "record_progress_for_generation", None)
                    if generation is not None and callable(fenced_progress):
                        fenced_progress(
                            optimization_id,
                            event.get("event"),
                            metrics,
                            source_optimization_id=optimization_id,
                            generation=generation,
                        )
                    else:
                        self._job_store.record_progress(optimization_id, event.get("event"), metrics)
                    if run_watcher is not None:
                        run_watcher.observe_progress(event.get("event"), metrics)
                except Exception:
                    logger.exception("Optimization %s: failed to persist subprocess progress event", optimization_id)
            elif event_type == EVENT_LOG:
                timestamp = None
                timestamp_raw = event.get("timestamp")
                if isinstance(timestamp_raw, str):
                    try:
                        timestamp = datetime.fromisoformat(timestamp_raw)
                    except ValueError:
                        timestamp = None
                pair_index_raw = event.get("pair_index")
                pair_index = int(pair_index_raw) if isinstance(pair_index_raw, int) else None
                log_batch.append(
                    {
                        "level": str(event.get("level", "INFO")),
                        "logger": str(event.get("logger", "dspy")),
                        "message": str(event.get("message", "")),
                        "timestamp": timestamp,
                        "pair_index": pair_index,
                        **run_log.normalize(event),
                    }
                )
            elif event_type == EVENT_AGENT_RUN:
                self._persist_agent_run_event(optimization_id, event.get("run"))
            elif event_type == EVENT_RESULT:
                payload = event.get("result")
                if isinstance(payload, dict):
                    result_payload = payload
            elif event_type == EVENT_TERMINAL:
                payload = event.get("outcome")
                if isinstance(payload, dict):
                    result_payload = {"_terminal_outcome": payload}
            elif event_type == EVENT_ERROR:
                payload = {
                    "error": str(event.get("error", "Unknown subprocess error")),
                    "traceback": str(event.get("traceback", "")),
                    "error_type": str(event.get("error_type", "")),
                    "failure_kind": str(event.get("failure_kind", "")),
                }
                error_payload = payload

        if log_batch:
            try:
                self._job_store.append_logs(optimization_id, log_batch)
            except Exception:
                logger.exception(
                    "Optimization %s: failed to persist %d subprocess log entries", optimization_id, len(log_batch)
                )
        return result_payload, error_payload, drained_count

    def _persist_agent_run_event(self, optimization_id: str, run: Any) -> None:
        """Store one agent run record or transcript delta, on stores that keep agent runs.

        Args:
            optimization_id: The job the run belongs to.
            run: The event's ``run`` payload.
        """
        if not isinstance(run, dict) or not isinstance(run.get("run_id"), int):
            return
        save_run = getattr(self._job_store, "save_agent_run", None)
        append_transcript = getattr(self._job_store, "append_agent_run_transcript", None)
        if save_run is None or append_transcript is None:
            return
        try:
            delta = run.get("transcript_delta")
            if isinstance(delta, str):
                append_transcript(optimization_id, run["run_id"], delta)
            else:
                save_run(optimization_id, run)
        except Exception:
            logger.exception("Optimization %s: failed to persist agent run %s", optimization_id, run.get("run_id"))

    def _checkpoints_enabled(self, optimization_type: str) -> bool:
        """Return whether this job is a GEPA run on a checkpoint-capable store.

        A store without checkpoint support falls through to the existing
        restart path. Stored grid searches are not executable any more, so
        they never reach this check.

        Args:
            optimization_type: The job's optimization type.

        Returns:
            ``True`` when the run should persist and restore GEPA checkpoints.
        """
        return optimization_type in (OPTIMIZATION_TYPE_RUN, OPTIMIZATION_TYPE_BLACKBOX) and hasattr(
            self._job_store, "save_gepa_checkpoint"
        )

    def _prepare_gepa_dir(self, optimization_id: str) -> Path:
        """Allocate a clean worker-owned GEPA base dir, seeding a saved checkpoint for resume.

        The dir is wiped first so a stale state file from an earlier attempt of
        the same id can never trigger an unintended resume.

        Args:
            optimization_id: The job whose directory is prepared.

        Returns:
            The base path handed to the child as ``_gepa_log_dir``.

        Raises:
            ValueError: When a recovery was requested but its checkpoint is gone.
        """
        job = self._job_store.get_job(optimization_id)
        payload = job.get("payload") or {}
        base = Path(tempfile.gettempdir()) / "app-gepa" / f"{optimization_id}-g{job.get('execution_generation', 0)}"
        shutil.rmtree(base, ignore_errors=True)
        base.mkdir(parents=True, exist_ok=True)
        checkpoint = self._job_store.get_gepa_checkpoint(optimization_id)
        if checkpoint is None and (job.get("recovery") or {}).get("phase") == "resuming":
            raise ValueError("The checkpoint selected for recovery is no longer available.")
        if checkpoint is not None:
            validate_checkpoint(
                checkpoint.data,
                checkpoint.manifest,
                payload,
                job.get("code_version"),
                sandbox_image=job.get("sandbox_image"),
            )
            state_base = _gepa_state_base(base, payload)
            state_base.mkdir(parents=True, exist_ok=True)
            (state_base / GEPA_STATE_FILENAME).write_bytes(checkpoint.data)
            logger.info(
                "Optimization %s: restored GEPA checkpoint (#%s, %d bytes) — resuming",
                optimization_id,
                checkpoint.iteration,
                checkpoint.stored_bytes,
            )
        return base

    def _persist_gepa_checkpoint(self, optimization_id: str, gepa_dir: Path, tracker: dict[str, Any]) -> None:
        """Persist the run's advanced GEPA state to the store.

        mtime-gated so the multi-MB blob is written only on genuinely new
        state. Failures are swallowed.

        Args:
            optimization_id: The running job.
            gepa_dir: The worker-owned base directory.
            tracker: Per-key cursor (``pair_index -> {"mtime","n"}``), carried
                across calls.
        """
        state_base = _gepa_state_base(gepa_dir, tracker.get("payload", {}))
        self._persist_one_checkpoint(optimization_id, state_base / GEPA_STATE_FILENAME, -1, tracker)

    def _persist_one_checkpoint(
        self, optimization_id: str, state_path: Path, pair_index: int, tracker: dict[str, Any]
    ) -> None:
        """Persist one run/pair's ``gepa_state.bin`` when its mtime has advanced.

        Args:
            optimization_id: The running job.
            state_path: Path to this run/pair's state file.
            pair_index: Always ``-1``; the store keys checkpoints by it.
            tracker: Shared cursor; this pair's ``{"mtime","n","metric_calls"}``
                sub-entry is created and updated in place.
        """
        try:
            mtime = state_path.stat().st_mtime
        except OSError:
            return
        cursor = tracker.setdefault(pair_index, {"mtime": None, "n": 0})
        if mtime == cursor.get("mtime"):
            return
        try:
            data = state_path.read_bytes()
        except OSError:
            return
        if not data:
            return
        try:
            manifest = checkpoint_manifest(
                data,
                tracker.get("payload", {}),
                tracker.get("code_version"),
                sandbox_image=tracker.get("sandbox_image"),
            )
            incumbent = tracker.get("_incumbents", {}).get(pair_index)
            if incumbent is not None:
                manifest["evaluated_incumbent"] = incumbent
            next_n = manifest["iteration"]
            self._job_store.save_gepa_checkpoint(
                optimization_id,
                data,
                next_n,
                pair_index,
                manifest=manifest,
                expected_generation=tracker.get("generation"),
            )
        except Exception:
            logger.exception("Optimization %s pair %s: failed to persist GEPA checkpoint", optimization_id, pair_index)
            return
        cursor["mtime"] = mtime
        cursor["n"] = next_n
        cursor["metric_calls"] = manifest["metric_calls"]

    def seconds_since_last_activity(self) -> float | None:
        """Return seconds since the most recent worker activity, or ``None`` if none recorded yet.

        Returns:
            Seconds since any worker last touched its activity timestamp.
        """
        with self._activity_lock:
            if not self._last_activity:
                return None
            latest = max(self._last_activity.values())
        return time.monotonic() - latest

    def dump_thread_stacks(self) -> str:
        """Return formatted stack traces of all worker threads, suitable for logging when diagnosing stuck workers.

        Returns:
            A multi-line string with the current frame of each worker thread.
        """
        frames = sys._current_frames()
        lines = []
        for thread in self._threads:
            frame = frames.get(thread.ident) if thread.ident is not None else None
            if frame:
                lines.append(f"--- {thread.name} (alive={thread.is_alive()}) ---")
                lines.extend(traceback.format_stack(frame))
            else:
                lines.append(f"--- {thread.name} (no frame, alive={thread.is_alive()}) ---")
        return "\n".join(lines)

    def threads_alive(self) -> bool:
        """Return True if the worker has at least one registered thread and every one is alive.

        Returns:
            ``True`` only when every spawned worker thread is still running.
        """
        if not self._threads:
            return False
        return all(t.is_alive() for t in self._threads)

    def cancel_job(self, optimization_id: str) -> bool:
        """Signal a job to stop. Returns True if the job was found (pending or currently running).

        Args:
            optimization_id: ID of the job to cancel.

        Returns:
            ``True`` if a pending or running job was found, ``False`` otherwise.
        """
        with self._queue_lock:
            event = self._cancel_events.get(optimization_id)
            if event:
                event.set()
            if optimization_id in self._pending_jobs:
                self._pending_jobs.remove(optimization_id)
                # Pending jobs never reach _mark_job_done; clean up event here.
                self._cancel_events.pop(optimization_id, None)
                return True
        return event is not None

    def queue_size(self) -> int:
        """Return the number of jobs waiting to be processed.

        Returns:
            Length of the pending queue at the moment the lock is held.
        """
        with self._queue_lock:
            return len(self._pending_jobs)

    def active_jobs(self) -> int:
        """Return the number of jobs currently being processed.

        Returns:
            Size of the processing set at the moment the lock is held.
        """
        with self._queue_lock:
            return len(self._processing_jobs)

    def thread_count(self) -> int:
        """Return the number of registered worker threads (alive or finished).

        Returns:
            Length of the internal thread list.
        """
        return len(self._threads)


_worker: BackgroundWorker | None = None
_worker_lock = threading.Lock()


def get_worker(
    job_store: JobStore,
    service: DspyService | None = None,
    pending_optimization_ids: list | None = None,
) -> BackgroundWorker:
    """Return the module-level singleton ``BackgroundWorker``, creating it if needed.

    If the current singleton is missing or its threads have died a new worker is
    constructed from ``settings.worker_threads`` / ``settings.worker_poll_interval``
    and started. With the DB-backed claim queue, ``pending_optimization_ids``
    is no longer required — pending rows are picked up automatically by the
    next claim — but the parameter is preserved (and used as a local hint) so
    callers don't have to coordinate the upgrade.

    Args:
        job_store: Backend used by the worker to persist job state.
        service: Optional pre-built ``DspyService`` shared with the worker.
        pending_optimization_ids: Optional local hint for jobs already known
            to be pending (eg. recovered on the same pod's restart).

    Returns:
        The module-level worker singleton.
    """
    global _worker

    with _worker_lock:
        if _worker is None or not _worker.threads_alive():
            num_workers = settings.worker_threads
            poll_interval = settings.worker_poll_interval
            if settings.local_mode:
                # A killed backend leaves setup networks behind; each holds a /16
                # of Docker's small pool, so clear idle ones before any box opens.
                remove_idle_sandbox_networks(settings.docker_bin)
            _worker = BackgroundWorker(
                job_store=job_store,
                num_workers=num_workers,
                poll_interval=poll_interval,
                service=service,
            )
            _worker.start()
            for optimization_id in pending_optimization_ids or []:
                _worker.enqueue_job(optimization_id)

    return _worker


def reset_worker_for_tests(timeout: float = 5.0) -> None:
    """Stop and clear the module-level worker singleton (test-only helper).

    Calls ``stop`` on the existing singleton (swallowing any shutdown error so
    the next test is not blocked) and then resets the module-level reference to
    ``None`` so the next ``get_worker`` call constructs a fresh instance.

    Args:
        timeout: Seconds shared across worker thread joins during shutdown.
    """
    global _worker
    with _worker_lock:
        if _worker is not None:
            try:
                _worker.stop(timeout=timeout)
            except Exception:  # isolation boundary: a failing shutdown must not block subsequent tests
                logger.exception("Failed to stop global worker during test reset")
        _worker = None

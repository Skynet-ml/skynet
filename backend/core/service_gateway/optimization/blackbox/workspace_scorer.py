"""The scorer for unified runs: the user's run code, then their metric, in one sandbox.

:class:`WorkspaceScorer` opens a box lazily on the first evaluation, the
same box :class:`~.sandbox_scorer.SandboxPythonScorer` opens (image,
network, install command, pinned dependencies), and ships
:mod:`.workspace_harness` into it next to the runner. Each evaluation runs
the harness once; the run code, metric and extra files travel only inside
the harness's message stream; only the run child writes the extra files, to
a temporary directory it removes. The harness binds ``llm`` to
the task model while the run code runs and to the metric model while the
metric runs; both models' usage is billed through :class:`ScorerUsage`
ledgers.

Each probe also carries the harness's trusted observation of the run stage
(task time, model attempts, metered service cost), to which this side adds
the time the call waited for the box (``queue_ms``) and the whole boxed call
(``evaluation_ms``); see :mod:`.workspace_harness`.

What reaches the optimizer is a score and side info. A user-code failure
becomes ``(None, {"error": "<Type>: <message>", "status": "metric_error"})``
with no source or traceback; a metric that does not return ``(score, side_info)`` stops the
run with :class:`ScorerAbortError`, since no candidate could ever be scored.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import shlex
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, ClassVar, TypeVar
from urllib.parse import urlsplit

import anyio

from ....billing.budgets import BudgetError
from ....billing.model_gateway import ROUTE_KEY
from ....billing.runtime import UsagePendingError
from ....config import settings
from ....exceptions import ServiceError
from ....i18n import t
from ....models.common import ModelConfig
from ....models.unified import UnifiedCase, UnifiedRunRequest
from ....sandbox import EnvironmentCapabilities, ExecResult, OutputSink, SandboxEnvironment, SandboxSpec, sync_bridge
from ....sandbox.pool import BoxOutcome, BoxWork, SandboxPool
from ....sandbox.timeouts import Phase
from . import check_terminal, sandbox_log, workspace_harness
from .harness import GatewayConfig
from .native_runtime import sanitize_side_info
from .protocol import METRIC_ERROR, STATUS_KEY, Candidate, ScorerAbortError, SideInfo
from .sandbox import SandboxRuntime, SandboxSession, scorer_runtime_from_settings
from .sandbox_scorer import (
    DEPENDENCIES_DIR,
    SandboxPythonScorer,
    ScorerGateway,
    ScorerUsage,
    _raise_control,
    _tail,
    revive_images,
    scorer_gateway,
)

logger = logging.getLogger(__name__)

HARNESS_FILE = "app_workspace_harness.py"
HARNESS_SOURCE = Path(workspace_harness.__file__).read_text(encoding="utf-8")
_OPENROUTER_PREFIX = "openrouter/"
SHAPE_MESSAGE_KEY = "contract.shape.blackbox"

R = TypeVar("R")

# No setting holds a box limit yet. One call fans out to at most eight
# final-run cases or a contract check's few samples, so 16 lets two such
# calls run at once. SBX-23 moves the limit to a setting shared across pods.
POOL_BOX_LIMIT = 16
POOL_PROVIDER = "blackbox-sync"
# A slot box opens nothing, but the spec check needs a finite lifetime.
_SLOT_LIFETIME_SECONDS = 24 * 3600.0

_pool_state: tuple[asyncio.AbstractEventLoop, SandboxPool, anyio.CapacityLimiter] | None = None


class _SkippedError(Exception):
    """Mark a call that the pool read before an earlier call failed, so it never ran."""


class _SlotBox(SandboxEnvironment):
    """Hold one pool permit while a sync scorer call runs; it opens no sandbox.

    The scorers still open their own sandboxes through the sync runtime, so
    this box only counts the call against the process box limit.
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
    """Run sync calls on the process sandbox pool, one box per call, and keep their order.

    Every call counts against the one process limit ``POOL_BOX_LIMIT``. Each
    call runs in its own thread with a copy of the caller's context, so run
    and usage scopes reach it. After the first failure no further call
    starts, and the failure of the earliest call is raised once the calls
    in flight end. A cancel waits for the calls in flight, then releases
    every permit.

    Args:
        phase: ``Phase.RUN`` for final-run cases, ``Phase.SCORE`` for scorer cases.
        calls: The calls, each with no arguments.
        workers: The most calls in flight at once for this fan-out.

    Returns:
        Each call's result, in the calls' order.

    Raises:
        ValueError: The phase is neither run nor score.
        Exception: The error of the earliest call that failed, unchanged.
    """
    if phase not in (Phase.RUN, Phase.SCORE):
        raise ValueError(f"pooled calls run in the run or score phase, not {phase}")
    if not calls:
        return []
    items = [(contextvars.copy_context(), call) for call in calls]
    return sync_bridge.run(_run_on_pool, phase, items, max(1, min(workers, len(items))))


def _pool() -> tuple[SandboxPool, anyio.CapacityLimiter]:
    """Return this event loop's pool and thread limiter, and make them on first use.

    The pool binds to one loop; a new portal loop after ``sync_bridge.shutdown``
    gets a new pool. Only the portal thread calls this, so no lock is needed.

    Returns:
        The pool and the limiter of its worker threads.
    """
    global _pool_state
    loop = asyncio.get_running_loop()
    if _pool_state is None or _pool_state[0] is not loop:
        # An explicit limiter: anyio's default of 40 threads is shared with
        # other work on the loop and must not cap the pool below its limit.
        _pool_state = (
            loop,
            SandboxPool({POOL_PROVIDER: POOL_BOX_LIMIT}),
            anyio.CapacityLimiter(POOL_BOX_LIMIT),
        )
    return _pool_state[1], _pool_state[2]


async def _run_on_pool(phase: Phase, items: list[tuple[contextvars.Context, Callable[[], R]]], workers: int) -> list[R]:
    """Run each call in a slot box on the pool and gather the results by position.

    Args:
        phase: The pool phase the calls run in.
        items: Each call with the caller's context copy it runs in.
        workers: The most calls in flight at once.

    Returns:
        Each call's result, in the items' order.

    Raises:
        Exception: The error of the earliest call that failed.
    """
    pool, limiter = _pool()
    results: dict[int, R] = {}
    errors: dict[int, Exception] = {}

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
            Exception: The call's own error.
        """
        if errors:
            raise _SkippedError
        context, func = items[index]
        try:
            return await _in_thread(context, func, limiter)
        except Exception as exc:
            errors.setdefault(index, exc)
            raise

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
        elif not isinstance(outcome.error, _SkippedError):
            errors.setdefault(outcome.candidate, outcome.error)  # type: ignore[arg-type]

    work: BoxWork[int, Any] = (
        BoxWork(create=_slot_box, run=no_run, score=call)
        if phase is Phase.SCORE
        else BoxWork(create=_slot_box, run=call)
    )
    await pool.run(candidates(), work, collect, workers=workers)
    if errors:
        raise errors[min(errors)]
    return [results[index] for index in range(len(items))]


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
    pool never has more than its limit of calls running.

    Args:
        context: The caller's context copy to run the call in.
        func: The call.
        limiter: Bounds the worker threads.

    Returns:
        The call's result.

    Raises:
        asyncio.CancelledError: The pool was cancelled while the call ran.
        Exception: The call's own error.
    """
    thread = asyncio.ensure_future(anyio.to_thread.run_sync(context.run, func, limiter=limiter))
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
    return thread.result()


@dataclass(frozen=True)
class WorkspaceProbe:
    """One evaluation's outcome, user-code failures reported as data.

    ``error`` is ``"<Type>: <message>"`` (all an optimizer may see) and
    ``stage`` names where it happened: ``run``, ``metric`` or ``shape``.
    ``detail`` adds the user-code frames, for the person who wrote the code.
    """

    score: float | None
    side_info: dict[str, Any]
    error: str | None = None
    stage: str | None = None
    detail: str | None = None
    output_preview: str | None = None
    duration_ms: int = 0
    usage_by_model: dict[str, tuple[int, int]] = field(default_factory=dict)
    observation: dict[str, Any] | None = None


def workspace_gateway(config: ModelConfig, gateway: GatewayConfig | None) -> ScorerGateway:
    """Resolve where one workspace model's ``llm()`` calls go.

    A protected route or the user's own key decides the gateway exactly as
    for a scorer model; a managed model goes through ``gateway``, the run's
    gateway, falling back to the configured sandbox gateway.

    Args:
        config: The task or metric model.
        gateway: The run's model gateway, if the caller has one.

    Returns:
        The gateway the harness posts chat completions to.

    Raises:
        ServiceError: When no reachable gateway can be resolved.
    """
    own_key = config.extra.get("api_key")
    if gateway is None or isinstance(config.extra.get(ROUTE_KEY), dict) or (isinstance(own_key, str) and own_key):
        return scorer_gateway(config, settings)
    effort = config.extra.get("reasoning_effort")
    return ScorerGateway(
        url=gateway.url,
        model=config.name.removeprefix(_OPENROUTER_PREFIX),
        api_key=gateway.api_key,
        billing_model=config.name,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
        reasoning_effort=effort if isinstance(effort, str) and effort else None,
        timeout_seconds=settings.lm_request_timeout_seconds,
    )


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


def shape_message() -> str:
    """Return the localized explanation of what ``score`` must return.

    Returns:
        The user-facing message.
    """
    return t(SHAPE_MESSAGE_KEY)


class WorkspaceScorer(SandboxPythonScorer):
    """Run code plus metric in one sandbox; engine calls are serialized.

    The box, install command, pinned dependencies, reopen-on-failure and
    protected stops are those of :class:`SandboxPythonScorer`; only what one
    call runs differs. ``run_many`` is the exception to serialization: the
    contract check's samples fan out inside the one box. ``usage`` is the task
    model's ledger (the model every call uses); ``ledgers`` holds every ledger
    the run must bill.
    """

    def __init__(
        self,
        payload: UnifiedRunRequest,
        *,
        runtime: SandboxRuntime,
        task_gateway: ScorerGateway | None,
        metric_gateway: ScorerGateway | None,
        protected: bool = False,
        lifetime_seconds: float | None = None,
        job_id: str | None = None,
        usage: ScorerUsage | None = None,
        dependency_route: dict[str, str] | None = None,
    ) -> None:
        """Bind a unified workspace to a runtime without opening anything yet.

        Args:
            payload: The unified run whose workspace is scored.
            runtime: Where the box is opened.
            task_gateway: Where the run code's ``llm()`` calls go, if anywhere.
            metric_gateway: Where the metric's ``llm()`` calls go, if anywhere.
            protected: Whether the run executes under the protected profile, where every model needs an opaque route.
            lifetime_seconds: Requested box lifetime; the configured ceiling when unset.
            job_id: Names and tags the box after the job, when known.
            usage: Ledger to record the task model's usage in; a fresh one when unset.
            dependency_route: The protected package relay pinned dependencies install through.

        Raises:
            ServiceError: When a protected box is given a model without an opaque route.
        """
        workspace = payload.workspace
        primary = task_gateway or metric_gateway
        super().__init__(
            "",
            runtime=runtime,
            gateway=primary,
            timeout_seconds=workspace.metric_timeout_seconds,
            lifetime_seconds=lifetime_seconds,
            job_id=job_id,
            install_command=workspace.install_command,
            dependency_lock=workspace.dependency_lock.model_dump(mode="json") if workspace.dependency_lock else None,
            dependency_route=dependency_route,
        )
        gateways = [gateway for gateway in (task_gateway, metric_gateway) if gateway is not None]
        if (self._protected or protected) and any(not gateway.protected for gateway in gateways):
            raise ServiceError("Protected workspace code requires opaque model routes from the trusted parent.")
        self._task_gateway = task_gateway
        self._metric_gateway = metric_gateway
        self._init = {
            "run_code": workspace.run_code,
            "metric_code": workspace.metric_code,
            "files": dict(workspace.files),
            "artifact_kind": payload.task.artifact.kind,
            "input_fields": list(payload.data.input_fields),
            "gateways": {
                "task": task_gateway.runner_payload() if task_gateway is not None else None,
                "metric": metric_gateway.runner_payload() if metric_gateway is not None else None,
            },
        }
        injected: dict[str, dict[str, str]] = {}
        self._env_keys: dict[str, str] = {}
        for gateway, env_name in (
            (task_gateway, workspace_harness.ENV_TASK_API_KEY),
            (metric_gateway, workspace_harness.ENV_METRIC_API_KEY),
        ):
            if gateway is None:
                continue
            headers = gateway.injected_headers() if runtime.injects_headers else {}
            host = urlsplit(gateway.url).hostname or ""
            # One host gets one injected key; a second key for it travels in the environment.
            if headers and (host not in injected or injected[host] == headers[host]):
                injected.update(headers)
            elif gateway.api_key:
                self._env_keys[env_name] = gateway.api_key
        self._spec = replace(self._spec, inject_headers=injected)
        self._env_key = None
        self._harness_session: SandboxSession | None = None
        self.usage = usage if usage is not None else (ScorerUsage(primary.billing_model) if primary else None)
        self._metric_usage: ScorerUsage | None = None
        if metric_gateway is not None:
            if task_gateway is None or metric_gateway.billing_model == task_gateway.billing_model:
                self._metric_usage = self.usage
            else:
                self._metric_usage = ScorerUsage(metric_gateway.billing_model)

    @property
    def ledgers(self) -> tuple[ScorerUsage, ...]:
        """Return every distinct usage ledger this scorer records into."""
        found: list[ScorerUsage] = []
        for ledger in (self.usage, self._metric_usage):
            if ledger is not None and all(ledger is not other for other in found):
                found.append(ledger)
        return tuple(found)

    def __call__(self, candidate: Candidate, case: Any = None) -> tuple[float | None, SideInfo]:
        """Score ``candidate`` on ``case`` for the engines.

        Args:
            candidate: The version to score.
            case: The case, a :class:`UnifiedCase` or its mapping.

        Returns:
            The score and side info, or ``(None, {"error": "<Type>: <message>",
            "status": "metric_error"})`` when the user's code failed.

        Raises:
            ScorerAbortError: When ``score`` did not return ``(score, side_info)``.
        """
        score, side_info, _ = self.score_observed(candidate, case)
        return score, side_info

    def score_observed(
        self, candidate: Candidate, case: Any = None
    ) -> tuple[float | None, SideInfo, dict[str, Any] | None]:
        """Score ``candidate`` on ``case`` and keep the evaluation's trusted observation.

        The final runs use this to keep each case's time and cost for the
        baseline and the selected version.

        Args:
            candidate: The version to score.
            case: The case, a :class:`UnifiedCase` or its mapping.

        Returns:
            The score and side info as ``__call__`` returns them, and the observation, if any.

        Raises:
            ScorerAbortError: When ``score`` did not return ``(score, side_info)``.
        """
        probe = self.run(candidate, case)
        if probe.stage == "shape":
            raise ScorerAbortError(f"{shape_message()} ({probe.error})")
        if probe.error is not None or probe.score is None:
            # In-process engines (GEPA, best-of-n, Auto) hand this straight to a
            # reflection LM, so it gets the same one-line cut native engines get.
            error = sanitize_side_info({"error": probe.error or "ValueError: the metric returned no score"})
            return None, {**error, STATUS_KEY: METRIC_ERROR}, probe.observation
        return probe.score, revive_images(probe.side_info), probe.observation

    def warm(self) -> None:
        """Open the box and install the runner before the first sample is scored.

        Splitting this from the calls lets a caller report boot and scoring as
        separate progress steps instead of one silent wait.
        """
        with self._lock:
            self._open()

    def run(self, candidate: Candidate, case: Any = None) -> WorkspaceProbe:  # type: ignore[override]
        """Run the code and the metric on one case and report user-code failures as data.

        Args:
            candidate: The version to score.
            case: The case to score it on.

        Returns:
            The score, side info and output preview, or the failure with its stage.

        Raises:
            ServiceError: When the box could not run the harness at all.
        """
        waiting = time.perf_counter()
        with self._lock:
            queued = time.perf_counter() - waiting
            blocked = self._extend(candidate)
            if blocked is not None:
                return WorkspaceProbe(score=None, side_info={}, error=f"ImportError: {blocked}", stage="run")
            return self._probe(candidate, case, queued_seconds=queued)

    def run_many(self, candidate: Candidate, cases: list[Any]) -> list[WorkspaceProbe]:
        """Score one candidate on several cases concurrently, in the same box.

        The contract check's few samples are independent; fanning them out
        turns one boxed call per sample into one round of waiting. The box is
        opened once, up front, and each case keeps ``run``'s failure policy.

        Args:
            candidate: The version to score.
            cases: The cases, scored in one fan-out.

        Returns:
            One probe per case, in the cases' order.

        Raises:
            ServiceError: When the box could not run the harness at all.
        """
        if not cases:
            return []
        with self._lock:
            blocked = self._extend(candidate)
            if blocked is not None:
                return [
                    WorkspaceProbe(score=None, side_info={}, error=f"ImportError: {blocked}", stage="run")
                    for _ in cases
                ]
            self._open()
        terminal = check_terminal.current_recorder()
        return run_on_pool(
            Phase.SCORE,
            [
                functools.partial(self._sample, candidate, case, index, len(cases), terminal)
                for index, case in enumerate(cases)
            ],
            len(cases),
        )

    def _sample(
        self,
        candidate: Candidate,
        case: Any,
        index: int,
        total: int,
        terminal: check_terminal.CheckTerminalRecorder | None,
    ) -> WorkspaceProbe:
        """Score one of ``run_many``'s cases, reporting it to the Check's live transcript.

        Args:
            candidate: The version to score.
            case: The case to score it on.
            index: The case's position in the fan-out.
            total: How many cases the fan-out scores.
            terminal: The check's recorder, if one is recording.

        Returns:
            The case's probe.

        Raises:
            ServiceError: When the box could not run the harness at all.
        """
        if terminal is None:
            return self._probe(candidate, case)
        terminal.sample_started(index, total)
        with check_terminal.recording(terminal), check_terminal.sample_scope(index):
            try:
                probe = self._probe(candidate, case)
            except Exception as error:
                terminal.sample_finished(index, total, error=f"{type(error).__name__}: {error}")
                raise
        terminal.sample_finished(
            index,
            total,
            score=probe.score,
            error=probe.error,
            duration_ms=probe.duration_ms,
        )
        return probe

    def _probe(self, candidate: Candidate, case: Any, *, queued_seconds: float = 0.0) -> WorkspaceProbe:
        """Score one case in the open box, applying ``run``'s failure policy.

        Args:
            candidate: The version to score.
            case: The case to score it on.
            queued_seconds: How long the call waited for the box before this.

        Returns:
            The score, side info and output preview, or the failure with its stage.

        Raises:
            ServiceError: When the box could not run the harness at all.
        """
        message = {"candidate": candidate, "case": case_document(case)}
        started = time.perf_counter()
        try:
            reply = self._invoke(message)
        except (ServiceError, BudgetError, UsagePendingError):
            raise
        except Exception:
            if self._protected:
                raise
            logger.warning("workspace sandbox failed; reopening", exc_info=True)
            self._discard()
            reply = self._invoke(message)
        _raise_control(reply)
        by_model = self._record(reply.get("usage") or {})
        raw_score = reply.get("score")
        elapsed = time.perf_counter() - started
        observation = reply.get("observation")
        if isinstance(observation, dict):
            timing = observation.setdefault("timing", {})
            timing["queue_ms"] = round(queued_seconds * 1000, 1)
            timing["evaluation_ms"] = round(elapsed * 1000, 1)
        return WorkspaceProbe(
            score=float(raw_score) if raw_score is not None and reply.get("error") is None else None,
            side_info=dict(reply.get("side_info") or {}),
            error=reply.get("error"),
            stage=reply.get("stage"),
            detail=reply.get("detail"),
            output_preview=reply.get("output_preview"),
            duration_ms=int(elapsed * 1000),
            usage_by_model=by_model,
            observation=observation if isinstance(observation, dict) else None,
        )

    def check_ready(self) -> dict[str, Any]:
        """Not offered: the contract check scores real samples instead.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError("Workspace readiness is verified by the contract check.")

    def _record(self, usage: dict[str, Any]) -> dict[str, tuple[int, int]]:
        """Record one call's usage per model and return what it consumed.

        Args:
            usage: ``{"task": [...], "metric": [...]}`` from the harness.

        Returns:
            ``model → (input_tokens, output_tokens)`` for this call.
        """
        by_model: dict[str, tuple[int, int]] = {}
        for role, gateway, ledger in (
            ("task", self._task_gateway, self.usage),
            ("metric", self._metric_gateway, self._metric_usage),
        ):
            entries = list(usage.get(role) or []) if isinstance(usage, dict) else []
            if gateway is None or not entries:
                continue
            if ledger is not None:
                ledger.record(entries)
            call = ScorerUsage(gateway.billing_model)
            call.record(entries)
            for model, (tokens_in, tokens_out) in call.by_model().items():
                prior_in, prior_out = by_model.get(model, (0, 0))
                by_model[model] = (prior_in + tokens_in, prior_out + tokens_out)
        return by_model

    def _invoke(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Run the harness once in the open (or newly opened) box.

        The init message carrying the code and this call's message travel in
        the command's environment, which the sandbox API offers in place of
        stdin; the harness removes them from its environment before user code runs.

        Args:
            payload: The ``{"candidate", "case"}`` message.

        Returns:
            The harness's reply to the call, or a timeout error in its shape.

        Raises:
            ServiceError: When the harness produced no reply.
        """
        session = self._open()
        self._calls += 1
        env = {
            **workspace_harness.encode_stream([self._init, payload]),
            **self._env_keys,
            **({"PYTHONPATH": f"{DEPENDENCIES_DIR}/site"} if self._dependency_lock is not None else {}),
            **({"PYOPENGL_PLATFORM": "osmesa"} if self._protected else {}),
        }
        with sandbox_log.event_scope(source="scorer"):
            result = session.run(
                f"python3 {shlex.quote(HARNESS_FILE)} --env",
                env=env,
                timeout_seconds=self._timeout_seconds,
                on_output=sandbox_log.ignore_output,
            )
        if result.timed_out:
            return {
                "error": f"TimeoutError: the code exceeded the {self._timeout_seconds:g}s timeout",
                "stage": "run",
                "detail": f"TimeoutError: the code exceeded the {self._timeout_seconds:g}s timeout",
                "usage": {},
            }
        replies = workspace_harness.parse_replies(result.stdout or "")
        if not replies:
            raise ServiceError(
                f"workspace sandbox failed (exit {result.exit_code}): {_tail(result.stderr or result.stdout or '')}"
            )
        for reply in replies:
            if isinstance(reply.get("control"), dict):
                return reply
        return replies[-1]

    def _open(self) -> SandboxSession:
        """Return the job's box with the harness beside the runner."""
        session = super()._open()
        if self._harness_session is not session:
            session.write_files({HARNESS_FILE: HARNESS_SOURCE})
            self._harness_session = session
        return session


def build_workspace_scorer(
    payload: UnifiedRunRequest,
    gateway: GatewayConfig | None,
    *,
    protected: bool,
    usage: ScorerUsage | None = None,
    runtime: SandboxRuntime | None = None,
    job_id: str | None = None,
    lifetime_seconds: float | None = None,
    dependency_route: dict[str, str] | None = None,
) -> WorkspaceScorer:
    """Build the engine-facing scorer for a unified run's workspace.

    The run code calls the task model (the reflection model when no task
    model was chosen); the metric calls the metric model, if one was chosen.

    Args:
        payload: The unified run.
        gateway: The run's model gateway for managed models, if any.
        protected: Whether the run executes under the protected profile.
        usage: Ledger for the task model's usage; a fresh one when unset.
        runtime: Where the box opens; the configured scorer runtime when unset.
        job_id: Names the box after the job, when known.
        lifetime_seconds: Requested box lifetime; the configured ceiling when unset.
        dependency_route: The protected package relay pinned dependencies install through.

    Returns:
        A scorer over ``(candidate, case)`` → ``(score, side_info)``.

    Raises:
        ServiceError: When a model has no reachable gateway, or a protected
            run's model has no opaque route.
    """
    models = payload.models
    task_model = models.task_model or models.reflection_model
    return WorkspaceScorer(
        payload,
        runtime=runtime or scorer_runtime_from_settings(settings),
        task_gateway=workspace_gateway(task_model, gateway),
        metric_gateway=workspace_gateway(models.metric_model, gateway) if models.metric_model else None,
        protected=protected,
        lifetime_seconds=lifetime_seconds,
        job_id=job_id,
        usage=usage,
        dependency_route=dependency_route,
    )

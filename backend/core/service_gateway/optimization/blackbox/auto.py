"""Compose pinned upstream engines with the published omni helper."""

from __future__ import annotations

import itertools
import math
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import dspy
from gepa.oa.budget import BudgetExhausted
from gepa.oa.config import OptimizeAnythingConfig
from gepa.oa.engine import Result as UpstreamResult
from gepa.oa.ensemble import optimize_best_of
from gepa.oa.eval_server import EvalServer as UpstreamEvalServer
from gepa.oa.task import Task as UpstreamTask

from ....billing.usage_tags import usage_scope
from ....constants import PROGRESS_LANE_COMPLETED, PROGRESS_LANE_HANDOFF, PROGRESS_LANE_STARTED
from ....exceptions import ServiceError
from ....models.blackbox import BlackboxStrategy
from ..budget_stop import BudgetReached
from ..cost_ceiling import CostCeilingExceededError
from .protocol import (
    BudgetExhaustedError,
    Candidate,
    EngineContext,
    EvalServer,
    Result,
    ScorerAbortError,
    SideInfo,
    Task,
    engine_score,
)
from .registry import NO_CAPABILITIES, EngineCapabilities, get_engine
from .upstream import AUTO_ENGINES, GEPA_SOURCE, local_result


@dataclass
class LaneOutcome:
    """Persist the outcome of one actual upstream invocation."""

    engine: str
    phase: str
    status: str
    best_score: float | None = None
    scorer_runs: int = 0
    error: str | None = None
    best_candidate: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)


def lane_tag(phase: str, engine: str) -> str:
    """Name the Auto lane a billed call belongs to.

    Args:
        phase: Composition phase, ``explore`` or ``continue``.
        engine: Engine running in the lane.

    Returns:
        The ``lane`` usage tag value, ``<phase>-<engine>``.
    """
    return f"{phase}-{engine}"


def _completed_lane_incumbent(lanes: list[LaneOutcome], server: EvalServer, interrupted: Any = None) -> Result | None:
    """Keep the best finite result already selected by an interrupted lane.

    Args:
        lanes: Composition lanes in upstream scheduling order.
        server: Shared evaluation accounting for the composition.
        interrupted: Optional result published by the lane that hit the stop.

    Returns:
        Best evaluated lane result, or no result before any lane publishes one.
    """
    if (
        isinstance(interrupted, Result)
        and interrupted.best_candidate is not None
        and isinstance(interrupted.best_score, int | float)
        and math.isfinite(interrupted.best_score)
    ):
        interrupted.total_evals = server.used
        interrupted.metadata = {
            **interrupted.metadata,
            "selection_source": "interrupted_lane_incumbent",
        }
        return interrupted
    completed = [
        lane
        for lane in lanes
        if lane.status == "completed"
        and lane.best_candidate is not None
        and isinstance(lane.best_score, int | float)
        and math.isfinite(lane.best_score)
    ]
    if not completed:
        return None
    selected = max(completed, key=lambda lane: float(lane.best_score))
    return Result(
        best_candidate=selected.best_candidate,
        best_score=float(selected.best_score),
        total_evals=server.used,
        metadata={
            **selected.metadata,
            "engine": selected.engine,
            "phase": selected.phase,
            "selection_source": "completed_lane_incumbent",
        },
    )


def _admit_context(ctx: EngineContext) -> EngineContext:
    """Bound a fresh invocation by cumulative spend already known to the worker.

    Args:
        ctx: Configured lane allowance and shared run accounting.

    Returns:
        A context with its proposer allowance clamped to the remaining run cap.

    Raises:
        BudgetReached: When another engine already spent the allowance.
    """
    if ctx.check_budget is not None:
        ctx.check_budget()
    if ctx.remaining_cost_usd is None:
        return ctx
    remaining = ctx.remaining_cost_usd()
    if remaining <= 0:
        raise BudgetReached("The run's total spending budget has been reached.")
    budget = min(ctx.proposer_token_budget_usd or remaining, remaining)
    native = (
        None
        if ctx.native_options is None
        else replace(
            ctx.native_options,
            max_token_cost=remaining
            if ctx.native_options.max_token_cost is None
            else min(ctx.native_options.max_token_cost, remaining),
        )
    )
    return replace(ctx, native_options=native, proposer_token_budget_usd=budget)


class _LaneEngine:
    """Adapt only scorer transport, execution location and progress for an engine."""

    def __init__(
        self,
        name: str,
        phase: str,
        ctx: EngineContext,
        caps: EngineCapabilities,
        lanes: list[LaneOutcome],
        progress_callback: Any,
        lane_ids: Any,
    ) -> None:
        """Bind platform resources before upstream schedules this engine.

        Args:
            name: Upstream engine identifier.
            phase: Product progress phase.
            ctx: Models, runtime and workspace.
            caps: Validated deployment capabilities.
            lanes: Shared result collector.
            progress_callback: Optional event sink.
            lane_ids: Run-wide sequence for trajectory identifiers.
        """
        self.name, self.phase, self.ctx = name, phase, ctx
        self.caps, self.lanes, self.progress_callback = caps, lanes, progress_callback
        self.model_context = dict(dspy.settings.copy())
        self.invocations = 0
        self.lane_ids = lane_ids
        self.lock = threading.Lock()

    def run(self, task: UpstreamTask, upstream: UpstreamEvalServer) -> UpstreamResult:
        """Invoke the selected upstream implementation under its assigned allowance.

        Args:
            task: Task supplied by the upstream composition, including its chosen seed.
            upstream: Upstream composition's evaluation server.

        Returns:
            An upstream result retaining the engine's aggregate score; a lane
            that failed returns its seed at ``-inf`` so another lane wins.
        """
        with self.lock:
            invocation = self.invocations
            self.invocations += 1
        lane_id = f"{self.phase}-{self.name}-{invocation}"
        lane_index = next(self.lane_ids)
        lane = LaneOutcome(self.name, self.phase, "completed")
        self.lanes.append(lane)
        callback = self.progress_callback
        if callback is not None:
            callback(
                PROGRESS_LANE_STARTED, {"engine": self.name, "phase": self.phase, "budget": upstream.budget.remaining}
            )

        def progress(event: str, metrics: dict[str, Any]) -> None:
            """Keep candidate identifiers distinct across parallel engines.

            Args:
                event: Upstream/platform event name.
                metrics: Event payload.
            """
            if callback is not None:
                callback(event, {**metrics, "lane_index": lane_index})

        context = replace(self.ctx, run_dir=str(Path(self.ctx.run_dir) / lane_id), progress_callback=progress)
        server = EvalServer(upstream.evaluate, max_evals=upstream.budget.remaining or 0)
        local_task = Task(
            task.seed_candidate,
            task.objective,
            task.background,
            list(task.train_set or []),
            valset=list(task.val_set or []),
        )
        try:
            with dspy.context(**self.model_context), usage_scope(lane=lane_tag(self.phase, self.name)):
                result = get_engine(self.name, self.caps).run(local_task, server, _admit_context(context))
            best_candidate, best_score = result.best_candidate, result.best_score
            if server.metric_error(best_candidate) is not None:
                # An upstream engine ranks by filled means, so it can return a
                # version that crashed on a case. Final selection excludes such
                # versions (SKY-350). The server's best running mean mixes
                # minibatch and full-split scores, so the lane hands off the
                # seed at -inf and another lane wins (SKY-367).
                best_candidate, best_score = task.seed_candidate, -math.inf
            lane.best_candidate, lane.best_score = best_candidate, best_score
            lane.metadata = result.metadata
            return UpstreamResult(
                best_candidate=best_candidate,
                best_score=best_score if best_score is not None else -math.inf,
                total_evals=server.used,
                metadata={**result.metadata, "engine": self.name, "phase": self.phase},
            )
        except BudgetReached as exc:
            lane.status, lane.error = "stopped", str(exc)
            if isinstance(exc.result, Result):
                lane.best_candidate, lane.best_score = exc.result.best_candidate, exc.result.best_score
                lane.metadata = dict(exc.result.metadata)
            raise
        except (ScorerAbortError, CostCeilingExceededError, BudgetExhausted, BudgetExhaustedError) as exc:
            lane.status, lane.error = "failed", str(exc)
            raise
        except Exception as exc:
            # One engine crashing must not sink the others' work: the lane
            # loses the comparison and the run carries on.
            lane.status, lane.error = "failed", str(exc)
            return UpstreamResult(
                best_candidate=task.seed_candidate,
                best_score=-math.inf,
                total_evals=server.used,
                metadata={"engine": self.name, "phase": self.phase, "failed": True},
            )
        finally:
            lane.scorer_runs = server.used
            if callback is not None:
                callback(
                    PROGRESS_LANE_COMPLETED,
                    {
                        "engine": self.name,
                        "phase": self.phase,
                        "status": lane.status,
                        "best_score": lane.best_score,
                        "scorer_runs": lane.scorer_runs,
                        "error": lane.error,
                    },
                )

    def process_result(self, result: UpstreamResult, output_dir: Path | None) -> None:
        """Leave artifact persistence to the engine's execution adapter.

        Args:
            result: Completed engine result.
            output_dir: Upstream composition artifact directory.
        """


def run_strategy(
    strategy: BlackboxStrategy,
    task: Task,
    server: EvalServer,
    ctx: EngineContext,
    progress_callback: Any = None,
    *,
    caps: EngineCapabilities = NO_CAPABILITIES,
) -> tuple[Result, list[LaneOutcome]]:
    """Invoke a single engine or the pinned upstream composition helpers.

    Args:
        strategy: Selected engine or composition.
        task: Optimization inputs.
        server: Run-wide scoring allowance and evidence collector.
        ctx: Model routing, runtime and artifact directory.
        progress_callback: Optional job event sink.
        caps: Validated runtime capabilities.

    Returns:
        The upstream-selected incumbent and observed engine invocations.

    Raises:
        ServiceError: If the exact recipe cannot run; never substitutes an engine.
    """
    lanes: list[LaneOutcome] = []
    lane_ids = itertools.count()
    if strategy.mode == "single":
        engine = get_engine(str(strategy.engine), caps)
        if progress_callback is not None:
            progress_callback(
                PROGRESS_LANE_STARTED, {"engine": engine.name, "phase": "single", "budget": server.remaining}
            )
        try:
            result = engine.run(task, server, _admit_context(ctx))
        except BudgetReached as exc:
            if isinstance(exc.result, Result):
                exc.result.metadata.update(engine=engine.name, upstream_source=GEPA_SOURCE)
            raise
        result.metadata.update(engine=engine.name, upstream_source=GEPA_SOURCE)
        lanes.append(
            LaneOutcome(
                engine.name,
                "single",
                "completed",
                result.best_score,
                server.used,
                best_candidate=result.best_candidate,
                metadata=dict(result.metadata),
            )
        )
        if progress_callback is not None:
            progress_callback(
                PROGRESS_LANE_COMPLETED,
                {
                    "engine": engine.name,
                    "phase": "single",
                    "status": "completed",
                    "best_score": result.best_score,
                    "scorer_runs": server.used,
                },
            )
        return result, lanes

    if not task.str_mode:
        raise ServiceError("The upstream Auto recipe requires a text starting point; use GEPA for named parts.")
    for name in AUTO_ENGINES:
        get_engine(name, caps)
    lane_count = len(AUTO_ENGINES)
    # One even share per exploration lane plus a matching share for the GEPA continuation.
    share = 1.0 / (lane_count + 1)
    if strategy.mode == "auto" and server.remaining < lane_count + 1:
        raise ServiceError(
            f"Auto needs at least {lane_count + 1} scorer runs: {lane_count} exploration lanes and one continuation."
        )

    def configuration(name: str, phase: str, allowance: int, fraction: float) -> OptimizeAnythingConfig:
        """Partition resources while leaving scheduling and winner selection upstream.

        Args:
            name: Engine identifier.
            phase: Progress phase.
            allowance: Assigned evaluation calls.
            fraction: Share of the proposer cost ceiling.

        Returns:
            The upstream config with an execution adapter.
        """
        token_budget = None if ctx.proposer_token_budget_usd is None else ctx.proposer_token_budget_usd * fraction
        native = None if ctx.native_options is None else replace(ctx.native_options, max_token_cost=token_budget)
        context = replace(ctx, native_options=native, proposer_token_budget_usd=token_budget)
        return OptimizeAnythingConfig(
            engine=_LaneEngine(name, phase, context, caps, lanes, progress_callback, lane_ids),
            max_evals=allowance,
            max_token_cost=token_budget,
            max_concurrency=ctx.concurrency,
            output_dir=str(Path(ctx.run_dir) / f"{phase}-{name}-evals"),
        )

    def evaluate(candidate: Candidate, example: Any = None) -> tuple[float, SideInfo]:
        """Score one case for the upstream composition, which needs a number for every case.

        Args:
            candidate: The version to score.
            example: The case, or ``None`` when there are no cases.

        Returns:
            The score, :data:`UNSCORED` after a metric error, and the side information.
        """
        score, side_info = server.evaluate(candidate, example)
        return engine_score(score), side_info

    kwargs = {
        "seed_candidate": task.seed_candidate,
        "evaluator": evaluate,
        # Upstream's dataset is the train split; the validation split rides
        # along as its valset so each lane can select on it.
        "dataset": task.cases or None,
        "valset": task.valset or None,
        "objective": task.objective,
        "background": task.background,
        "name": Path(ctx.run_dir).name,
    }
    try:
        per_lane = server.remaining // (lane_count + 1)
        continuation_allowance = server.remaining - lane_count * per_lane
        winner = optimize_best_of(
            **kwargs,
            configs=[configuration(name, "explore", per_lane, share) for name in AUTO_ENGINES],
            max_workers=lane_count,
        )
    except BudgetReached as exc:
        exc.result = _completed_lane_incumbent(lanes, server, exc.result)
        exc.evidence["_lanes"] = lanes
        exc.evidence["completed_lanes"] = [
            {
                "engine": lane.engine,
                "phase": lane.phase,
                "score": lane.best_score,
                "candidate": lane.best_candidate,
                "status": lane.status,
            }
            for lane in lanes
            if lane.best_score is not None
        ]
        exc.evidence["selection_scope"] = "validation" if exc.result is not None else "unpublished_composition"
        raise
    if winner.metadata.get("failed"):
        errors = "; ".join(f"{lane.engine}: {lane.error}" for lane in lanes if lane.error)
        raise ServiceError(f"Every Auto exploration lane failed ({errors}).")
    if progress_callback is not None:
        progress_callback(
            PROGRESS_LANE_HANDOFF,
            {
                "from_engine": winner.metadata.get("engine"),
                "to_engine": "gepa",
                "best_score": winner.best_score,
            },
        )
    continuation_index = next(lane_ids)

    def continuation_progress(event: str, metrics: dict[str, Any]) -> None:
        """Scope GEPA's continuation trajectory separately from exploration.

        Args:
            event: Progress event name.
            metrics: Event payload.
        """
        if progress_callback is not None:
            progress_callback(event, {**metrics, "lane_index": continuation_index})

    continuation_ctx = replace(
        ctx,
        run_dir=str(Path(ctx.run_dir) / "continue-gepa"),
        progress_callback=continuation_progress,
        proposer_token_budget_usd=None
        if ctx.proposer_token_budget_usd is None
        else ctx.proposer_token_budget_usd * share,
    )
    continuation_server = server.lane(min(continuation_allowance, server.remaining))
    if progress_callback is not None:
        progress_callback(
            PROGRESS_LANE_STARTED, {"engine": "gepa", "phase": "continue", "budget": continuation_server.remaining}
        )
    try:
        with usage_scope(lane=lane_tag("continue", "gepa")):
            continued = get_engine("gepa", caps).run(
                replace(task, seed_candidate=winner.best_candidate),
                continuation_server,
                _admit_context(continuation_ctx),
            )
    except BudgetReached as exc:
        if not isinstance(exc.result, Result):
            exc.result = local_result(winner, server)
        exc.evidence["_lanes"] = lanes
        exc.evidence["selection_scope"] = "validation"
        raise
    lanes.append(
        LaneOutcome(
            "gepa",
            "continue",
            "completed",
            continued.best_score,
            continuation_server.used,
            best_candidate=continued.best_candidate,
            metadata=dict(continued.metadata),
        )
    )
    if progress_callback is not None:
        progress_callback(
            PROGRESS_LANE_COMPLETED,
            {
                "engine": "gepa",
                "phase": "continue",
                "status": "completed",
                "best_score": continued.best_score,
                "scorer_runs": continuation_server.used,
            },
        )
    continued.metadata.update(
        engine="gepa",
        phase="continue",
        upstream_source=GEPA_SOURCE,
        upstream_recipe="omni-gepa",
        explore_engine=winner.metadata.get("engine"),
    )
    continued.total_evals = server.used
    return continued, lanes

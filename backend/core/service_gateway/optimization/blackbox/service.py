"""Job entry points for black-box optimization.

``run_blackbox_optimization`` is what the worker subprocess calls for a
``blackbox`` job: run the strategy through a budgeted eval server that scores
every train case (or once, without cases), pick the winner from those
scores, then score the starting point and the winner afresh in a separate
final run: on the validation cases to apply the regression guard, and on the
held-out test cases for the reported scores. On an agent target the scorer is
wrapped so every scorer run launches the harness in a private workspace
inside the run's managed sandbox. ``validate_blackbox_payload`` backs the
submissions router. The unified (wizard v2) entry point reuses the same
plumbing through :class:`JobSpec` and :func:`run_job`.
"""

from __future__ import annotations

import json
import logging
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

import dspy

from ....billing.budgets import BudgetError
from ....billing.byok_upstreams import validate_byok_novelty
from ....billing.model_gateway import EMBEDDING_ROUTE_KEY, ROUTE_KEY, SHINKA_EMBEDDING_MODEL
from ....billing.operation_pricing import UnpricedOperationError
from ....billing.pricing import (
    CENT_USD_VALUE,
    ModelUsage,
    cents_for_usage,
    combine_usages,
    raw_cost_usd,
    usage_markup,
)
from ....billing.protected_execution import runtime_cost_profile
from ....billing.runtime import UsagePendingError
from ....billing.usage_tags import CALLER_REFLECTION, usage_scope
from ....config import settings
from ....constants import (
    DETAIL_BASELINE,
    DETAIL_OPTIMIZED,
    PROGRESS_BASELINE,
    PROGRESS_EVALUATION_STARTED,
    PROGRESS_OPTIMIZED,
    PROGRESS_OPTIMIZER,
    TQDM_DESC_KEY,
    TQDM_N_KEY,
    TQDM_PERCENT_KEY,
    TQDM_TOTAL_KEY,
)
from ....exceptions import ServiceError
from ....models.artifacts import ProgramArtifact
from ....models.blackbox import (
    BLACKBOX_REPO_ENGINES,
    BLACKBOX_STRATEGY_AUTO,
    BLACKBOX_TARGET_AGENT,
    BLACKBOX_TARGET_REPO,
    BlackboxBudget,
    BlackboxCandidateNode,
    BlackboxCaseResult,
    BlackboxEngineCatalogResponse,
    BlackboxEngineInfo,
    BlackboxLaneResult,
    BlackboxNamedScore,
    BlackboxProposer,
    BlackboxProposerRuntimeInfo,
    BlackboxRunRequest,
    BlackboxRunResponse,
    BlackboxShinkaSettings,
    BlackboxStrategy,
    BlackboxTarget,
    BlackboxVersion,
)
from ....models.common import ModelConfig, SplitFractions
from ....models.results import LMActivity, LMStageStats, ModelTokenUsage
from ...language_models import (
    build_language_model,
    canonical_model_id,
    lm_call_count,
    model_usages_from_history,
    total_tokens_from_history,
)
from ...safe_exec import validate_scorer_code
from ..budget_stop import BudgetReached
from ..cost_ceiling import CostCeilingCallback
from ..timing import STAGE_BASELINE, STAGE_EVALUATION, STAGE_TRAINING
from ..trajectory import MINIBATCH_FEEDBACK_CHAR_CAP
from ..unified.split import CaseSplits, report_cases, selection_cases, split_cases
from .agent_eval import SandboxAgentScorer, agent_target_unavailable_reason, gateway_from_settings
from .agent_runs import PHASE_BASELINE, PHASE_FINAL, AgentRunRecorder, AgentRunSink, run_scope
from .auto import LaneOutcome, run_strategy
from .feedback import FEEDBACK_KEY, named_scores, without_images
from .harness import GatewayConfig
from .native_runtime import NATIVE_ENGINES, NativeOptions, native_runtime_unavailable_reason
from .protocol import Candidate, EngineContext, EvalServer, Result, ScorerFn, Task, candidate_key
from .registry import ENGINES, EngineCapabilities, get_engine
from .repo_scorer import REPO_SCORER_URL, SETUP_ALLOWANCE_SECONDS
from .rule_judge import ILLEGAL_APPROACH_KEY
from .runner import side_info_json_default
from .sandbox import current_sandbox_runtime, sandbox_runtime_from_settings
from .scorer import JobScorer, RemoteScorer, build_scorer
from .upstream import AUTO_ENGINES, GEPA_REVISION

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str, dict[str, Any]], None]

_REPO_ENGINE_REASON = "This engine does not optimize a repository."

# Progress events per run are capped near this many so a 100k-run budget
# does not flood the job log.
_PROGRESS_EVENTS = 100

# No choice, or one of the catalog's ``ReasoningEffort`` levels (core/api/model_catalog.py). Pi and Prime Agent put
# the effort into a shell command line, so a free-form value must never reach them.
_NATIVE_EFFORTS = (None, "none", "minimal", "low", "medium", "high", "xhigh", "max")


def _proposer_runtime() -> Literal["vercel", "docker"]:
    """Name the sandbox runtime this deployment runs proposers in.

    Returns:
        ``"docker"`` in local mode, otherwise ``"vercel"``.
    """
    return "docker" if settings.local_mode else "vercel"


def engine_capabilities(target: BlackboxTarget) -> EngineCapabilities:
    """Describe what this deployment and ``target`` offer the engines.

    Args:
        target: The job's target.
    Returns:
        Capabilities for the registry: sandboxes per the settings, agent
        target per the job.
    """
    reason = None if current_sandbox_runtime() is not None else agent_target_unavailable_reason(settings)
    proposer_reason = native_runtime_unavailable_reason(_proposer_runtime(), settings)
    return EngineCapabilities(
        sandbox=reason is None,
        agent_target=target.kind == BLACKBOX_TARGET_AGENT,
        sandbox_reason=reason,
        proposer_available=proposer_reason is None,
        proposer_reason=proposer_reason,
    )


def engine_catalog(target_kind: str) -> BlackboxEngineCatalogResponse:
    """List every engine with its availability for a job whose target is ``target_kind``.

    Args:
        target_kind: ``text``, ``agent`` or ``repo`` — the target the wizard is building.
    Returns:
        The catalog in registry order, each entry carrying the user-facing
        reason it cannot run here, when it cannot.
    """
    reason = agent_target_unavailable_reason(settings)
    caps = engine_capabilities(BlackboxTarget())
    auto_reason = next(
        (ENGINES[name].unavailable_reason_for(caps) for name in AUTO_ENGINES if not ENGINES[name].available_for(caps)),
        None,
    )
    runtime = _proposer_runtime()
    unavailable = native_runtime_unavailable_reason(runtime, settings)
    runtimes = [
        BlackboxProposerRuntimeInfo(
            id=runtime,
            available=unavailable is None,
            unavailable_reason=unavailable,
            cost=runtime_cost_profile(settings, "anything", runtime),
            checkpoint_restore_supported=unavailable is None,
            checkpoint_restore_reason=unavailable,
        )
    ]
    return BlackboxEngineCatalogResponse(
        target_kind=target_kind,
        sandbox_available=caps.sandbox,
        sandbox_reason=reason,
        engines=[
            BlackboxEngineInfo(
                id=spec.id,
                label=spec.label,
                description=spec.description,
                available=spec.available_for(caps)
                and (target_kind != BLACKBOX_TARGET_REPO or spec.id in BLACKBOX_REPO_ENGINES),
                unavailable_reason=(
                    spec.unavailable_reason_for(caps)
                    or (
                        _REPO_ENGINE_REASON
                        if target_kind == BLACKBOX_TARGET_REPO and spec.id not in BLACKBOX_REPO_ENGINES
                        else None
                    )
                ),
                requires_agent_target=spec.requires_agent_target,
                supports_parts=spec.supports_parts,
                checkpoint_recovery_supported=spec.checkpoint_recovery_supported,
                checkpoint_recovery_reason=(
                    None
                    if spec.checkpoint_recovery_supported
                    else "This pinned engine does not expose a compatible checkpoint restore contract."
                ),
            )
            for spec in ENGINES.values()
            if not spec.hidden
        ],
        auto_engines=list(AUTO_ENGINES),
        auto_available=auto_reason is None,
        auto_unavailable_reason=auto_reason,
        auto_checkpoint_recovery_supported=False,
        auto_checkpoint_recovery_reason="The Auto recipe cannot restore its multi-engine search from one checkpoint.",
        proposer_runtimes=runtimes,
        upstream_revision=GEPA_REVISION,
    )


@dataclass
class JobSpec:
    """What the shared run plumbing needs from a job, whichever payload it came from.

    The legacy black-box request and the unified (wizard v2) request both map
    onto this, so usage metering, progress, native proposer options, the cost
    ceiling, budget stops and the final runs live in one place.
    """

    objective: str | None
    background: str | None
    seed_candidate: Candidate | None
    seed: int | None
    budget: BlackboxBudget
    strategy: BlackboxStrategy
    reflection_model_settings: ModelConfig
    target: BlackboxTarget = field(default_factory=BlackboxTarget)
    proposer: BlackboxProposer = field(default_factory=BlackboxProposer)
    proposer_runtime: str = "vercel"
    shinka: BlackboxShinkaSettings | None = None
    extra_reflection_model_settings: list[ModelConfig] = field(default_factory=list)
    max_cost_cents: int | None = None
    execution_budget_id: str | None = None
    optimizer_name: str = ""
    optimizer_resolved: str | None = None
    # Private run context for in-process adapters; never serialized to a proposer.
    context: dict[str, Any] = field(default_factory=dict)
    # Further metered models (the DSPy task model) whose usage the run bills.
    extra_lms: list[Any] = field(default_factory=list)
    extra_lm_sources: list[str | None] = field(default_factory=list)
    scorer_token_source: str | None = None


def job_spec_from_payload(payload: BlackboxRunRequest) -> JobSpec:
    """Map a legacy black-box request onto the shared job spec.

    Args:
        payload: The submitted or stored legacy job.

    Returns:
        The equivalent job spec.
    """
    return JobSpec(
        objective=payload.objective,
        background=payload.background,
        seed_candidate=payload.seed_candidate,
        seed=payload.seed,
        budget=payload.budget,
        strategy=payload.strategy,
        reflection_model_settings=payload.reflection_model_settings,
        scorer_token_source=payload.scorer.model.token_source if payload.scorer.model else None,
        target=payload.target,
        proposer=payload.proposer,
        proposer_runtime=payload.proposer_runtime,
        shinka=payload.shinka,
        extra_reflection_model_settings=list(payload.extra_reflection_model_settings),
        max_cost_cents=payload.max_cost_cents,
        execution_budget_id=payload.execution_budget_id,
        optimizer_name=payload.strategy.engine or payload.strategy.mode,
    )


def legacy_case_splits(cases: list[Any], seed: int | None) -> CaseSplits:
    """Split a legacy black-box job's cases with the default fractions.

    Args:
        cases: The job's cases, in submitted order.
        seed: The job's seed; 0 when unset.

    Returns:
        Train, validation and held-out test cases.
    """
    return split_cases(cases, "pairs" if len(cases) > 1 else "single", SplitFractions(), shuffle=True, seed=seed or 0)


def uses_native_runtime(payload: BlackboxRunRequest | JobSpec) -> bool:
    """Report whether a job runs a coding-agent proposer in the native runtime.

    Args:
        payload: The submitted or stored job.

    Returns:
        True for auto mode, a native engine, or any repository run.
    """
    return (
        payload.strategy.mode != "single"
        or payload.strategy.engine in NATIVE_ENGINES
        or payload.target.kind == BLACKBOX_TARGET_REPO
    )


def _shinka_models(payload: BlackboxRunRequest | JobSpec, gateway: GatewayConfig) -> tuple[dict[str, str], ...]:
    """Name the gateway route of every ShinkaEvolve optimization model, the reflection model first.

    Args:
        payload: The submitted job.
        gateway: The run's model gateway, used for a model without its own scoped route.

    Returns:
        One ``{"model", "url", "token"}`` route per optimization model.
    """
    routes = []
    for config in (payload.reflection_model_settings, *payload.extra_reflection_model_settings):
        route = config.extra.get(ROUTE_KEY)
        if route:
            routes.append({"model": route["model"], "url": route["url"], "token": route["token"]})
        else:
            routes.append({"model": config.name, "url": gateway.url, "token": gateway.api_key})
    return tuple(routes)


def _shinka_embedding(payload: BlackboxRunRequest | JobSpec, gateway: GatewayConfig) -> dict[str, str] | None:
    """Name the embeddings route ShinkaEvolve's duplicate rejection uses, when it is on.

    Args:
        payload: The submitted job.
        gateway: The run's model gateway, used when the job has no scoped embeddings route.

    Returns:
        One ``{"model", "url", "token"}`` route, or ``None`` when duplicate rejection is off.
    """
    if payload.shinka is None or not payload.shinka.novelty:
        return None
    route = (payload.reflection_model_settings.extra.get(ROUTE_KEY) or {}).get(EMBEDDING_ROUTE_KEY)
    if route:
        return {"model": route["model"], "url": route["url"], "token": route["token"]}
    return {"model": SHINKA_EMBEDDING_MODEL, "url": gateway.url, "token": gateway.api_key}


def validate_blackbox_payload(
    payload: BlackboxRunRequest, *, verify_scorer: bool = True, api_bases: dict[str, str | None] | None = None
) -> None:
    """Reject a job before it is queued when it can never run.

    Args:
        payload: The submitted job.
        verify_scorer: Run legacy scorer loading; protected callers verify
            executable code inside the managed runtime.
        api_bases: Available saved provider endpoints.

    Raises:
        ServiceError: When the job has an agent target but this deployment
            cannot run agents, the chosen engine is unknown/unavailable, or
            the python scorer code does not load.
    """
    validate_job(payload, api_bases=api_bases)
    if verify_scorer and payload.scorer.kind == "python":
        validate_scorer_code(str(payload.scorer.metric_code))


def validate_job(payload: BlackboxRunRequest | JobSpec, *, api_bases: dict[str, str | None] | None = None) -> None:
    """Check that a job's engines can run here and its native proposer settings are usable.

    Args:
        payload: A legacy request or a unified run's job spec.
        api_bases: Available saved provider endpoints.

    Raises:
        ServiceError: When the target cannot run here, the engine is unknown
            or unavailable, a native proposer gets custom model settings or no
            spending budget, or Auto has too few scorer runs.
    """
    reflection = payload.reflection_model_settings.model_dump()
    reflection["token_source"] = reflection.get("token_source") or getattr(payload, "token_source", None)
    validate_byok_novelty(
        reflection,
        bool(payload.shinka and payload.shinka.novelty),
        api_bases=api_bases,
    )
    caps = engine_capabilities(payload.target)
    if caps.agent_target and not caps.sandbox:
        raise ServiceError(f"Agent targets cannot run on this deployment: {caps.sandbox_reason}")
    if payload.strategy.mode == "single":
        get_engine(str(payload.strategy.engine), caps)
    else:
        for name in AUTO_ENGINES:
            get_engine(name, caps)
    needs_native = uses_native_runtime(payload)
    if needs_native:
        models = [payload.reflection_model_settings, *payload.extra_reflection_model_settings]
        # The proposer CLI applies the main model's effort; the extra models feed
        # ShinkaEvolve's own calls, which have no effort setting.
        allowed = [{ROUTE_KEY, "reasoning_effort"}, *({ROUTE_KEY} for _ in payload.extra_reflection_model_settings)]
        if any(
            model.temperature is not None
            or model.max_tokens is not None
            or model.base_url
            or any(key not in keys for key in model.extra)
            or model.extra.get("reasoning_effort") not in _NATIVE_EFFORTS
            for model, keys in zip(models, allowed, strict=True)
        ):
            raise ServiceError(
                "Native proposers accept a model selection; custom sampling and routing settings are unsupported."
            )
        # A run backed by an execution budget keeps its allowance in the ledger
        # and is metered by the gateway route; the parent hands its guest the
        # ceiling the proposer plans against, so only a budget-less run must
        # state one here.
        if payload.max_cost_cents is None and payload.execution_budget_id is None:
            raise ServiceError("Set a total spending budget before starting an upstream agent proposer.")
    if payload.strategy.mode == "auto" and payload.budget.max_scorer_runs < len(AUTO_ENGINES) + 1:
        raise ServiceError(f"Auto needs at least {len(AUTO_ENGINES) + 1} scorer runs.")


def _agent_scorer(
    scorer: ScorerFn,
    target: BlackboxTarget,
    *,
    job_id: str,
    progress_callback: ProgressCallback | None,
    agent_run_sink: AgentRunSink | None,
    target_route: dict[str, str] | None = None,
) -> SandboxAgentScorer:
    """Wrap ``scorer`` so every call runs the harness in a fresh sandbox first.

    Args:
        scorer: The user's scorer.
        target: The job's agent target.
        job_id: Tags the sandboxes with the job.
        progress_callback: The job's progress sink, told when each run starts and ends.
        agent_run_sink: Receives each run's full record and live transcript.
        target_route: Opaque parent route for the target model, when protected.

    Returns:
        The wrapped scorer.

    Raises:
        ServiceError: When this deployment has no sandbox runtime or gateway.
    """
    runtime = sandbox_runtime_from_settings(settings)
    if getattr(runtime, "protected", False) and not target_route:
        raise ServiceError("Protected agent targets require an opaque model route from the trusted parent.")
    gateway = (
        GatewayConfig(url=target_route["url"], api_key=target_route["token"])
        if target_route
        else gateway_from_settings(settings)
    )
    if runtime is None or gateway is None:
        raise ServiceError(f"Agent targets cannot run on this deployment: {agent_target_unavailable_reason(settings)}")
    recorder = AgentRunRecorder(
        progress_callback=progress_callback, run_sink=agent_run_sink, secrets=(gateway.api_key,)
    )
    return SandboxAgentScorer(scorer, runtime=runtime, target=target, gateway=gateway, job_id=job_id, recorder=recorder)


@dataclass
class FinalRun:
    """One version's fresh scores from the final run, outside the optimization budget."""

    score: float
    feedback: str | None
    named: dict[str, BlackboxNamedScore]
    # Per case ``(score, feedback)``; empty when the run has no cases.
    cases: list[tuple[float, str]] = field(default_factory=list)
    runs: int = 0
    # Per case, the trusted observation of the run stage (time, model calls,
    # service cost); ``None`` where the scorer does not measure one.
    observations: list[dict[str, Any] | None] = field(default_factory=list)
    # Set when the rule judge rejected the version: its fixed score can beat
    # negative legal scores, so selection must check this flag, not the score.
    illegal: bool = False


def _mean_named_scores(side_infos: list[dict[str, Any]]) -> dict[str, BlackboxNamedScore]:
    """Average each named score over the cases, keeping what each case said about it.

    Args:
        side_infos: The scorer's side info on each case, in case order.

    Returns:
        Each name's mean score; its feedback verbatim with one case, else one
        ``Case n: ...`` line per case, capped like any feedback text. A case
        that failed (its side info is an ``error``) counts as 0 on every name,
        as it does in the total, so a failure never vanishes from the parts.
    """
    collected: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for position, side_info in enumerate(side_infos, start=1):
        for name, entry in named_scores(side_info).items():
            collected.setdefault(name, []).append((position, entry))
    failed = [i for i, side_info in enumerate(side_infos) if side_info.get("error") and not named_scores(side_info)]
    merged: dict[str, BlackboxNamedScore] = {}
    for name, entries in collected.items():
        if len(side_infos) == 1:
            feedback = entries[0][1]["feedback"]
        else:
            feedback = "\n".join(f"Case {position}: {entry['feedback']}" for position, entry in entries)
        merged[name] = BlackboxNamedScore(
            score=sum(entry["score"] for _, entry in entries) / (len(entries) + len(failed)),
            feedback=feedback[:MINIBATCH_FEEDBACK_CHAR_CAP],
        )
    return merged


def _final_run(
    scorer: ScorerFn,
    candidate: Candidate,
    cases: list[Any],
    *,
    label: str,
    phase: str,
    concurrency: int = 1,
    case_offset: int = 0,
) -> FinalRun:
    """Score ``candidate`` afresh on every case, or once without cases, outside the budget.

    The optimization run picks the winner; this separate run measures the
    starting point and the winner the same way so their scores compare
    like for like, just as GEPA reports its final candidate.

    Args:
        scorer: The run's scorer.
        candidate: The version to score.
        cases: The run's cases; empty to score the version on its own.
        label: What the candidate is, for logs and the error message.
        phase: Which pass this is, for the agent run records: ``PHASE_BASELINE`` or ``PHASE_FINAL``.
        concurrency: How many cases to score at once (agent targets run one
            sandbox per case, so this is the number of sandboxes in flight).
        case_offset: Added to each case's index in the run records, so the
            held-out pass does not overwrite the selection pass's records.

    Returns:
        The mean score, the feedback and named scores, and each case's result.

    Raises:
        ServiceError: When the scorer fails on the candidate, including when
            it returns no feedback.
    """
    targets: list[Any] = cases or [None]
    observed = getattr(scorer, "score_observed", None)

    def score_case(numbered: tuple[int, Any]) -> tuple[float, dict[str, Any], dict[str, Any] | None]:
        """Score ``candidate`` on one case and log the result.

        Args:
            numbered: The case's 1-based position and the case itself.

        Returns:
            The score, side information and trusted observation for that case.
        """
        position, case = numbered
        stage = STAGE_BASELINE if phase == PHASE_BASELINE else STAGE_EVALUATION
        index = str(case_offset + position - 1)
        observation: dict[str, Any] | None = None
        with run_scope(phase, index), usage_scope(stage=stage, case=index if cases else None):
            if callable(observed):
                score, side_info, observation = observed(candidate, case)
            else:
                score, side_info = scorer(candidate, case)
        # Per-case heartbeats at DEBUG surface only in the Logs tab's verbose view.
        logger.debug("%s final run %d/%d score=%.3f", label, position, len(targets), score)
        return score, side_info, observation

    started = time.perf_counter()
    try:
        workers = max(1, min(concurrency, len(targets)))
        logger.info("final run: scoring the %s on %d case(s), %d at a time", label, len(cases), workers)
        if workers == 1:
            scored = [score_case(numbered) for numbered in enumerate(targets, start=1)]
        else:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="final-run") as pool:
                scored = list(pool.map(score_case, enumerate(targets, start=1)))
    except (BudgetError, UsagePendingError, UnpricedOperationError):
        raise
    except ServiceError as exc:
        raise ServiceError(f"scorer failed on the {label}: {exc}") from exc
    except Exception as exc:
        raise ServiceError(f"scorer failed on the {label}: {type(exc).__name__}: {exc}") from exc
    side_infos = [side_info for _, side_info, _ in scored]
    feedbacks = [str(side_info.get(FEEDBACK_KEY) or "") for side_info in side_infos]
    mean = sum(score for score, _, _ in scored) / len(scored)
    logger.info("%s scored %.3f over %d run(s) in %.0fs", label, mean, len(scored), time.perf_counter() - started)
    return FinalRun(
        score=mean,
        feedback=None if cases else feedbacks[0],
        named=_mean_named_scores(side_infos),
        cases=[(score, feedback) for (score, _, _), feedback in zip(scored, feedbacks, strict=True)] if cases else [],
        runs=len(scored),
        observations=[observation for _, _, observation in scored] if cases else [],
        illegal=any(ILLEGAL_APPROACH_KEY in side_info for side_info in side_infos),
    )


def _case_results(baseline: FinalRun | None, best: FinalRun | None) -> list[BlackboxCaseResult]:
    """Pair the final run's per-case scores of the starting point and the winner.

    Args:
        baseline: The starting point's final run, if it had one.
        best: The winner's final run, if it had one.

    Returns:
        One row per case; empty when the run has no cases.
    """
    count = max(len(baseline.cases) if baseline else 0, len(best.cases) if best else 0)
    rows = []
    for index in range(count):
        before = baseline.cases[index] if baseline and index < len(baseline.cases) else None
        after = best.cases[index] if best and index < len(best.cases) else None
        before_observed = baseline.observations[index] if baseline and index < len(baseline.observations) else None
        after_observed = best.observations[index] if best and index < len(best.observations) else None
        rows.append(
            BlackboxCaseResult(
                index=index,
                baseline_score=before[0] if before else None,
                best_score=after[0] if after else None,
                baseline_feedback=before[1] if before else None,
                best_feedback=after[1] if after else None,
                baseline_observation=before_observed,
                best_observation=after_observed,
            )
        )
    return rows


# Side info persisted with the version history is capped so a run whose scorer
# returns renders for every version cannot bloat the job row; images are shed
# from the weakest versions first and the best version always keeps its own.
VERSION_SIDE_INFO_BYTE_CAP = 3_000_000


def _validation_scores(lanes: list[LaneOutcome]) -> dict[str, float]:
    """Collect each candidate's validation-set score from the lanes that recorded lineage.

    A candidate seen by more than one lane (the continue lane re-scores the
    winner's best as its seed) takes the later lane's score, matching the
    lane the Overview tree shows.

    Args:
        lanes: Every lane the strategy ran, in execution order.

    Returns:
        Validation score per :func:`candidate_key`; empty when no lane kept lineage.
    """
    scores: dict[str, float] = {}
    for lane in lanes:
        for node in lane.metadata.get("candidate_tree", []):
            if node.get("val_score") is not None:
                scores[candidate_key(node["candidate"])] = float(node["val_score"])
    return scores


def _version_history(server: EvalServer, val_scores: dict[str, float] | None = None) -> list[BlackboxVersion]:
    """Turn the eval server's records into the persisted version history.

    Args:
        server: The run's root eval server.
        val_scores: Validation-set score per :func:`candidate_key` from the
            engines that recorded lineage; a version with one shows it as its
            ``score`` instead of the running mean.

    Returns:
        One entry per distinct version in first-seen order, side info made
        JSON-safe and trimmed to :data:`VERSION_SIDE_INFO_BYTE_CAP` in total.
    """
    val_scores = val_scores or {}
    versions = [
        BlackboxVersion(
            candidate=record.candidate,
            score=val_scores.get(candidate_key(record.candidate), record.mean_score),
            mean_score=record.mean_score,
            evals=record.count,
            first_run=record.first_eval,
            side_info=json.loads(json.dumps(record.side_info, default=side_info_json_default)),
        )
        for record in server.history
    ]
    sizes = [len(json.dumps(version.side_info)) for version in versions]
    total = sum(sizes)
    weakest_first = sorted(range(len(versions)), key=lambda i: (versions[i].score or 0.0, versions[i].first_run))
    for i in weakest_first[:-1]:
        if total <= VERSION_SIDE_INFO_BYTE_CAP:
            break
        versions[i].side_info = without_images(versions[i].side_info)
        total -= sizes[i] - len(json.dumps(versions[i].side_info))
    return versions


def _best_legal_candidate(server: EvalServer, rejected: Candidate) -> Candidate | None:
    """Return the highest-scoring version the search scored that the rule judge did not reject.

    Args:
        server: The run's root eval server.
        rejected: The winner the rule judge rejected in the final run.

    Returns:
        That version, or ``None`` when every other scored version was
        rejected or crashed the scorer.
    """
    rejected_key = candidate_key(rejected)
    # A crashed version holds the 0.0 floor, which beats negative legal
    # scores, and scoring it again in the final run would fail the job.
    legal = [
        record
        for record in server.history
        if candidate_key(record.candidate) != rejected_key
        and ILLEGAL_APPROACH_KEY not in record.side_info
        and not record.crashed
    ]
    return max(legal, key=lambda record: record.mean_score).candidate if legal else None


def _progress_listener(progress_callback: ProgressCallback | None) -> Callable[[EvalServer, float], None]:
    """Build the eval-server listener that reports scorer-run progress.

    Args:
        progress_callback: The job's progress sink, if any.

    Returns:
        A listener for ``EvalServer(on_eval=...)``.
    """

    def on_eval(server: EvalServer, score: float) -> None:
        """Emit a throttled ``optimizer_progress`` event.

        Args:
            server: The run's root eval server.
            score: The score just recorded.
        """
        step = max(1, server.max_evals // _PROGRESS_EVENTS)
        if progress_callback is None or (server.used % step and server.used != server.max_evals):
            return
        progress_callback(
            PROGRESS_OPTIMIZER,
            {
                TQDM_DESC_KEY: "scorer runs",
                TQDM_TOTAL_KEY: server.max_evals,
                TQDM_N_KEY: server.used,
                TQDM_PERCENT_KEY: round(100.0 * server.used / server.max_evals, 1),
                "last_score": score,
                "best_score": server.best_score,
            },
        )

    return on_eval


def _reflection_caller(lm: dspy.LM) -> tuple[Callable[[str | list[dict[str, Any]]], str], list[float]]:
    """Wrap the reflection model in the callable the engines drive.

    GEPA hands over chat messages instead of text when the scorer's side
    information carries rendered images (``Image``), so a vision-capable
    reflection model sees what it is improving. Every call is timed here —
    the one choke point all engines go through — rather than via DSPy
    callbacks, which would not fire for non-``dspy.LM`` doubles and depend
    on context propagation into lane workers.

    Args:
        lm: The reflection model.

    Returns:
        A callable returning the model's first completion as text, and the
        list its per-call wall-clock durations (ms) accumulate into.
    """
    durations_ms: list[float] = []

    class ReflectionCaller:
        """Expose measured proposer spend to upstream's native cost stopper."""

        @property
        def lm(self) -> dspy.LM:
            """Return the metered model itself, for in-process DSPy optimizers."""
            return lm

        @property
        def total_cost(self) -> float:
            """Return the provider cost represented by the model's usage history."""
            return raw_cost_usd(model_usages_from_history(lm) or [])

        def __call__(self, prompt: str | list[dict[str, Any]]) -> str:
            """Call the selected optimization model and record its duration.

            Args:
                prompt: Text or multimodal messages generated by upstream.

            Returns:
                The first completion text.
            """
            started = time.monotonic()
            try:
                # Best-of-N calls from its own server threads, so the tags are set here, not inherited.
                with usage_scope(stage=STAGE_TRAINING, caller=CALLER_REFLECTION):
                    completions = lm(messages=prompt) if isinstance(prompt, list) else lm(prompt)
            finally:
                durations_ms.append((time.monotonic() - started) * 1000.0)
            return str(completions[0])

    return ReflectionCaller(), durations_ms


def _reflection_activity(durations_ms: list[float]) -> LMActivity | None:
    """Fold reflection-call timings into the shared ``LMActivity`` shape.

    Black-box engines only drive the reflection model inside the
    optimization loop, so every call lands in the ``training`` stage and the
    ``generation`` matrix stays empty; the run view renders the same stage
    table as DSPy runs.

    Args:
        durations_ms: Wall-clock duration of each reflection call.

    Returns:
        A one-stage activity matrix, or ``None`` when the engine never
        called the reflection model.
    """
    if not durations_ms:
        return None
    avg_ms = round(sum(durations_ms) / len(durations_ms), 1)
    return LMActivity(reflection={STAGE_TRAINING: LMStageStats(calls=len(durations_ms), avg_response_time_ms=avg_ms)})


def run_blackbox_optimization(
    payload: BlackboxRunRequest,
    *,
    artifact_id: str,
    progress_callback: ProgressCallback | None = None,
    gepa_log_dir_path: str | None = None,
    agent_run_sink: AgentRunSink | None = None,
    target_route: dict[str, str] | None = None,
    evaluator_route: dict[str, str] | None = None,
    repo_snapshot: dict[str, Any] | None = None,
) -> BlackboxRunResponse:
    """Run one legacy black-box job end to end.

    Args:
        payload: The submitted job.
        artifact_id: Job id, used to name the workspace.
        progress_callback: The job's progress sink, if any.
        gepa_log_dir_path: Workspace for engine state; a temp dir when unset.
        agent_run_sink: Receives each sandboxed agent run's record and live transcript, if any.
        target_route: Opaque parent route for the optimized task model.
        evaluator_route: Opaque parent route for the selected external evaluator.
        repo_snapshot: A repository run's packed tree, as uploaded into this box.

    Returns:
        The best version with the final run's baseline and optimized scores.

    Raises:
        ServiceError: When the scorer cannot be built, the job has an agent
            target this deployment cannot run, the scorer fails on the
            starting point, or no engine produced a version for a seedless job.
    """
    started = time.perf_counter()
    validate_blackbox_payload(
        payload,
        verify_scorer=payload.execution_budget_id is None and ROUTE_KEY not in payload.reflection_model_settings.extra,
    )
    protected = payload.execution_budget_id is not None or ROUTE_KEY in payload.reflection_model_settings.extra
    if payload.scorer.kind == "remote" and protected and not evaluator_route:
        raise ServiceError("Protected remote evaluation requires its parent-issued capability.")
    if payload.target.kind == BLACKBOX_TARGET_REPO:
        if not evaluator_route:
            raise ServiceError("A repository run is scored only by its trusted parent.")
        if not repo_snapshot or not repo_snapshot.get("chunks"):
            raise ServiceError("A repository run needs the repository its parent fetched.")
        base_scorer: JobScorer = RemoteScorer(
            REPO_SCORER_URL,
            secret=None,
            timeout_seconds=payload.scorer.timeout_seconds + SETUP_ALLOWANCE_SECONDS,
            protected_route=evaluator_route,
            label="repository scorer",
        )
    else:
        base_scorer = build_scorer(payload.scorer, job_id=artifact_id, protected_route=evaluator_route)
    return run_job(
        job_spec_from_payload(payload),
        base_scorer,
        legacy_case_splits(list(payload.cases or []), payload.seed),
        artifact_id=artifact_id,
        started=started,
        progress_callback=progress_callback,
        gepa_log_dir_path=gepa_log_dir_path,
        agent_run_sink=agent_run_sink,
        target_route=target_route,
        repo_snapshot=repo_snapshot,
    )


def run_job(
    job: JobSpec,
    base_scorer: Any,
    splits: CaseSplits,
    *,
    artifact_id: str,
    started: float,
    progress_callback: ProgressCallback | None,
    gepa_log_dir_path: str | None,
    agent_run_sink: AgentRunSink | None = None,
    target_route: dict[str, str] | None = None,
    repo_snapshot: dict[str, Any] | None = None,
) -> BlackboxRunResponse:
    """Run a job over an already-built scorer and close the scorer afterwards.

    Args:
        job: The job's plumbing settings.
        base_scorer: The job's scorer (``close()`` is called here).
        splits: The job's train, validation and held-out test cases.
        artifact_id: Job id, used to name the workspace.
        started: When the run began, for the elapsed time.
        progress_callback: The job's progress sink, if any.
        gepa_log_dir_path: Workspace for engine state; a temp dir when unset.
        agent_run_sink: Receives each sandboxed agent run's record and live transcript, if any.
        target_route: Opaque parent route for the optimized task model.
        repo_snapshot: A repository run's packed tree, as uploaded into this box.

    Returns:
        The best version with the final run's baseline and optimized scores.

    Raises:
        BudgetReached: When the run's budget stopped it; ``result`` carries
            the partial response.
    """
    try:
        return _run_job(
            job,
            base_scorer,
            splits,
            artifact_id=artifact_id,
            started=started,
            progress_callback=progress_callback,
            gepa_log_dir_path=gepa_log_dir_path,
            agent_run_sink=agent_run_sink,
            target_route=target_route,
            repo_snapshot=repo_snapshot,
        )
    except BudgetReached as exc:
        exc.evidence.setdefault("candidate_origin", None)
        exc.evidence.setdefault("final_evaluation_completed", False)
        exc.evidence.setdefault("final_evaluation_reason", "budget_reached")
        raise
    finally:
        try:
            base_scorer.close()
        except UsagePendingError:
            # The parent ledger retains this hold and publishes pending billing.
            # Do not replace an evaluated result or its original control signal.
            logger.info("Scorer sandbox final usage is pending reconciliation")


def _combined_usage(
    lms: list[Any], native: NativeOptions | None, *, excluded_native_models: frozenset[str] = frozenset()
) -> list[ModelUsage]:
    """Preserve each native model's identity when combining token accounting.

    Args:
        lms: Metered DSPy models and scorer ledgers.
        native: Shared native usage collector, if the recipe uses one.
        excluded_native_models: Native model IDs whose usage does not count toward this total.

    Returns:
        One usage row per model across both transports, with any reported cost
        the DSPy lane recorded.
    """
    usages = list(model_usages_from_history(*lms) or [])
    if native is not None:
        with native.usage_lock:
            snapshot = {model: dict(counts) for model, counts in native.usage_by_model.items()}
        usages.extend(
            ModelUsage(
                model=model,
                input_tokens=counts.get("prompt_tokens", 0),
                output_tokens=counts.get("completion_tokens", 0),
            )
            for model, counts in snapshot.items()
            if model not in excluded_native_models
        )
    return combine_usages(usages)


def _native_call_count(native: NativeOptions | None) -> int:
    """Count the model requests the in-sandbox proposer CLI made.

    Args:
        native: Shared native usage collector, if the recipe uses one.

    Returns:
        The summed per-model ``calls``, or 0 without a native collector.
    """
    if native is None:
        return 0
    with native.usage_lock:
        return sum(counts.get("calls", 0) for counts in native.usage_by_model.values())


def _run_job(
    job: JobSpec,
    base_scorer: Any,
    splits: CaseSplits,
    *,
    artifact_id: str,
    started: float,
    progress_callback: ProgressCallback | None,
    gepa_log_dir_path: str | None,
    agent_run_sink: AgentRunSink | None,
    target_route: dict[str, str] | None = None,
    repo_snapshot: dict[str, Any] | None = None,
) -> BlackboxRunResponse:
    """Run the job over an already-built scorer; see :func:`run_job`.

    Engines search on the train cases and may select on the validation
    cases; the final run picks between the starting point and the winner on
    the validation cases (train when there are none) and reports both on the
    held-out test cases.

    Args:
        job: The job's plumbing settings.
        base_scorer: The job's scorer, closed by the caller.
        splits: The job's train, validation and held-out test cases.
        artifact_id: Job id, used to name the workspace.
        started: When the run began, for the elapsed time.
        progress_callback: The job's progress sink, if any.
        gepa_log_dir_path: Workspace for engine state; a temp dir when unset.
        agent_run_sink: Receives each sandboxed agent run's record and live transcript, if any.
        target_route: Opaque parent route for the optimized task model.
        repo_snapshot: A repository run's packed tree, as uploaded into this box.

    Returns:
        The best version with the final run's baseline and optimized scores.
    """
    scorer: ScorerFn = base_scorer
    target = job.target
    caps = engine_capabilities(target)
    if caps.agent_target:
        scorer = _agent_scorer(
            scorer,
            target,
            job_id=artifact_id,
            progress_callback=progress_callback,
            agent_run_sink=agent_run_sink,
            target_route=target_route,
        )
    concurrency = target.concurrency if caps.agent_target else 1
    counts = splits.counts
    logger.info(
        "cases: %d train, %d validation, %d held out; selecting on %s, reporting on %s",
        counts.train,
        counts.val,
        counts.test,
        counts.selection_split,
        counts.report_split,
    )
    server = EvalServer(scorer, max_evals=job.budget.max_scorer_runs, on_eval=_progress_listener(progress_callback))
    seed_candidate = job.seed_candidate

    lm = build_language_model(job.reflection_model_settings, disable_cache=True)
    reflection_lm, reflection_durations_ms = _reflection_caller(lm)
    token_budget = None
    if job.max_cost_cents is not None and job.reflection_model_settings.token_source != "byok":
        token_budget = job.max_cost_cents * CENT_USD_VALUE / usage_markup()
    native_options = None
    if uses_native_runtime(job):
        budget_route = job.reflection_model_settings.extra.get(ROUTE_KEY)
        gateway = (
            GatewayConfig(url=budget_route["url"], api_key=budget_route["token"])
            if budget_route
            else gateway_from_settings(settings)
        )
        if gateway is None or (token_budget is None and job.reflection_model_settings.token_source != "byok"):
            raise ServiceError("The upstream proposer needs a gateway and a total spending budget.")
        active_runtime = current_sandbox_runtime()
        effort = job.reflection_model_settings.extra.get("reasoning_effort")
        native_options = NativeOptions(
            shinka=(job.shinka or BlackboxShinkaSettings()).model_dump(),
            shinka_models=_shinka_models(job, gateway),
            shinka_embedding=_shinka_embedding(job, gateway),
            runtime=job.proposer_runtime,
            sandbox_runtime=active_runtime,
            model=budget_route["model"] if budget_route else job.reflection_model_settings.name,
            gateway=gateway,
            budget_route=budget_route,
            max_token_cost=token_budget,
            proposer=job.proposer,
            reasoning_effort=effort if isinstance(effort, str) and effort else None,
            repo=(
                {
                    "chunks": [str(Path(chunk).resolve()) for chunk in repo_snapshot["chunks"]],
                    "editable_paths": list(target.repo.editable_paths),
                    "readonly_paths": list(repo_snapshot.get("readonly_paths") or ()),
                    "constrained": bool(repo_snapshot.get("constrained")),
                }
                if target.repo is not None and repo_snapshot is not None
                else None
            ),
        )

    task = Task(
        seed_candidate=seed_candidate,
        objective=job.objective,
        background=job.background,
        cases=list(splits.train),
        valset=list(splits.val),
        context=job.context,
    )
    ctx = EngineContext(
        reflection_lm=reflection_lm,
        native_options=native_options,
        proposer_token_budget_usd=token_budget,
        run_dir=gepa_log_dir_path or tempfile.mkdtemp(prefix=f"app-blackbox-{artifact_id}-"),
        seed=job.seed or 0,
        stop_at_score=job.budget.stop_at_score,
        max_iterations=job.budget.max_iterations,
        concurrency=concurrency,
        target_label=f"{target.harness} · {target.model}" if caps.agent_target else None,
        progress_callback=progress_callback,
    )
    # The scorer's own model calls are part of the run: they count toward
    # the cost ceiling and the usage the worker bills.
    scorer_usage = getattr(base_scorer, "usage", None)
    lms = [lm, *job.extra_lms] + ([] if scorer_usage is None else [scorer_usage])
    byok_reflection = job.reflection_model_settings.token_source == "byok"
    billable_lms = (
        ([] if byok_reflection else [lm])
        + [
            extra_lm
            for index, extra_lm in enumerate(job.extra_lms)
            if index >= len(job.extra_lm_sources) or job.extra_lm_sources[index] != "byok"
        ]
        + ([] if scorer_usage is None or job.scorer_token_source == "byok" else [scorer_usage])
    )
    native_sources: dict[str, set[str]] = {}
    for config in (job.reflection_model_settings, *job.extra_reflection_model_settings):
        route = config.extra.get(ROUTE_KEY) or {}
        model = route.get("model", config.name)
        native_sources.setdefault(model, set()).add(config.token_source)
    excluded_native_models = frozenset(model for model, sources in native_sources.items() if sources == {"byok"})
    callbacks = (
        [CostCeilingCallback(job.max_cost_cents, *billable_lms)]
        if job.max_cost_cents is not None and job.execution_budget_id is None
        else []
    )
    if callbacks:

        def check_budget() -> None:
            """Stop engine boundaries even when DSPy has caught a callback exception."""
            if (
                cents_for_usage(
                    _combined_usage(billable_lms, native_options, excluded_native_models=excluded_native_models)
                )
                >= job.max_cost_cents
            ):
                raise BudgetReached("The run's total spending budget has been reached.")

        ctx.check_budget = check_budget

        def remaining_cost_usd() -> float:
            """Return the run allowance after measured model usage across every lane."""
            spent = raw_cost_usd(
                _combined_usage(billable_lms, native_options, excluded_native_models=excluded_native_models)
            )
            return max(0.0, job.max_cost_cents * CENT_USD_VALUE / usage_markup() - spent)

        ctx.remaining_cost_usd = remaining_cost_usd
    iterations = f", up to {job.budget.max_iterations} iteration(s)" if job.budget.max_iterations else ""
    logger.info(
        "optimizing: mode=%s engine=%s, budget %d scorer run(s)%s",
        job.strategy.mode,
        job.strategy.engine or BLACKBOX_STRATEGY_AUTO,
        job.budget.max_scorer_runs,
        iterations,
    )
    selection_scope = "validation" if splits.val else "training"
    stop: BudgetReached | None = None
    try:
        with dspy.context(callbacks=callbacks):
            result, lanes = run_strategy(job.strategy, task, server, ctx, progress_callback, caps=caps)
    except BudgetReached as exc:
        stop = exc
        result = exc.result
        lanes = exc.evidence.pop("_lanes", [])
        if result is None and seed_candidate is not None:
            seed_score = server.mean_score(seed_candidate)
            result = Result(
                best_candidate=seed_candidate,
                best_score=seed_score,
                total_evals=server.used,
                metadata={"selection_source": "seed"},
            )
            exc.evidence.update(selection_scope=selection_scope, selection_score=seed_score, candidate_origin="seed")
        elif result is None:
            raise

    best_candidate = result.best_candidate
    logger.info("optimization finished after %d scorer run(s): best score %s", server.used, server.best_score)
    if progress_callback is not None:
        progress_callback(PROGRESS_EVALUATION_STARTED, {})
    select_on = selection_cases(splits)
    report_on = report_cases(splits)
    reuse_selection = report_on == select_on
    baseline_val: FinalRun | None = None
    best_val: FinalRun | None = None
    baseline_run: FinalRun | None = None
    best_run: FinalRun | None = None
    regression_guard_applied = False
    if stop is None:
        try:
            if seed_candidate is not None:
                baseline_val = _final_run(
                    scorer,
                    seed_candidate,
                    select_on,
                    label="starting point",
                    phase=PHASE_BASELINE,
                    concurrency=concurrency,
                )
            best_val = (
                baseline_val
                if baseline_val is not None and best_candidate == seed_candidate
                else _final_run(
                    scorer,
                    best_candidate,
                    select_on,
                    label="optimized version",
                    phase=PHASE_FINAL,
                    concurrency=concurrency,
                )
            )
            fallback = _best_legal_candidate(server, best_candidate) if best_val.illegal else None
            if fallback is not None and fallback != seed_candidate:
                logger.info("the rule judge rejected the optimized version; trying the best legal version")
                best_candidate = fallback
                best_val = _final_run(
                    scorer,
                    best_candidate,
                    select_on,
                    label="best legal version",
                    phase=PHASE_FINAL,
                    concurrency=concurrency,
                )
            if baseline_val is not None and best_val.illegal and not baseline_val.illegal:
                logger.info("the rule judge rejected the optimized version; keeping the starting point")
                best_candidate, best_val, regression_guard_applied = seed_candidate, baseline_val, True
            elif baseline_val is not None and best_val.score < baseline_val.score:
                logger.info("the optimized version scored below the starting point on %s; keeping it", selection_scope)
                best_candidate, best_val, regression_guard_applied = seed_candidate, baseline_val, True
            if reuse_selection:
                baseline_run, best_run = baseline_val, best_val
            else:
                if seed_candidate is not None:
                    baseline_run = _final_run(
                        scorer,
                        seed_candidate,
                        report_on,
                        label="starting point on held-out cases",
                        phase=PHASE_BASELINE,
                        concurrency=concurrency,
                        case_offset=len(select_on),
                    )
                best_run = (
                    baseline_run
                    if baseline_run is not None and best_candidate == seed_candidate
                    else _final_run(
                        scorer,
                        best_candidate,
                        report_on,
                        label="optimized version on held-out cases",
                        phase=PHASE_FINAL,
                        concurrency=concurrency,
                        case_offset=len(select_on),
                    )
                )
        except BudgetReached as exc:
            stop = exc
    distinct_runs = {id(run): run for run in (baseline_val, best_val, baseline_run, best_run) if run is not None}
    final_scorer_runs = sum(run.runs for run in distinct_runs.values())
    baseline = baseline_run.score if baseline_run else None
    optimized = best_run.score if best_run else None
    if progress_callback is not None:
        if baseline is not None:
            progress_callback(PROGRESS_BASELINE, {DETAIL_BASELINE: baseline})
        progress_callback(PROGRESS_OPTIMIZED, {DETAIL_OPTIMIZED: optimized})

    # The reflection LM reports the gateway transport id (``litellm_proxy/…``)
    # while the scorer ledger keys by catalog id; fold them so one model is one row.
    usage = combine_usages(
        replace(row, model=canonical_model_id(row.model)) for row in _combined_usage(lms, native_options)
    )
    engine_used = str(result.metadata.get("engine") or job.strategy.engine or BLACKBOX_STRATEGY_AUTO)
    candidate_tree = [BlackboxCandidateNode(**node) for node in result.metadata.pop("candidate_tree", [])]
    program_artifact = (
        ProgramArtifact.model_validate(result.artifact)
        if result.artifact is not None and best_candidate == result.best_candidate and not regression_guard_applied
        else None
    )
    response = BlackboxRunResponse(
        optimizer_name=job.optimizer_name,
        strategy_mode=job.strategy.mode,
        engine_used=engine_used,
        baseline_score=baseline,
        best_score=optimized,
        metric_improvement=None if baseline is None or optimized is None else optimized - baseline,
        baseline_feedback=baseline_run.feedback if baseline_run else None,
        best_feedback=best_run.feedback if best_run else None,
        baseline_named_scores=baseline_run.named if baseline_run else {},
        best_named_scores=best_run.named if best_run else {},
        case_results=_case_results(baseline_run, best_run),
        case_count=len(report_on),
        final_scorer_runs=final_scorer_runs,
        seed_candidate=seed_candidate,
        best_candidate=best_candidate,
        regression_guard_applied=regression_guard_applied,
        lanes=[
            BlackboxLaneResult(
                engine=lane.engine,
                phase=lane.phase,
                status=lane.status,
                best_score=lane.best_score,
                scorer_runs=lane.scorer_runs,
                error=lane.error,
            )
            for lane in lanes
        ],
        versions=_version_history(server, _validation_scores(lanes)),
        candidate_tree=candidate_tree,
        total_scorer_runs=server.used,
        runtime_seconds=time.perf_counter() - started,
        num_lm_calls=sum(lm_call_count(model) or 0 for model in lms) + _native_call_count(native_options),
        lm_activity=_reflection_activity(reflection_durations_ms),
        total_tokens=sum(row.input_tokens + row.output_tokens for row in usage) or total_tokens_from_history(*lms),
        usage_by_model=[ModelTokenUsage(**asdict(row)) for row in usage],
        optimization_metadata={
            "strategy": job.strategy.model_dump(),
            "budget": job.budget.model_dump(),
            "target": target.model_dump(),
            "proposer_runtime": job.proposer_runtime,
            "upstream_revision": GEPA_REVISION,
        },
        details={"optimizer_best_score": result.best_score, "selection_scope": selection_scope, **result.metadata},
        split_counts=counts,
        baseline_val_score=baseline_val.score if baseline_val else None,
        best_val_score=best_val.score if best_val else None,
        optimizer_resolved=job.optimizer_resolved,
        program_artifact=program_artifact,
    )
    if stop is not None:
        stop.result = response
        stop.evidence.update(
            candidate_origin="seed" if best_candidate == seed_candidate else "optimized",
            final_evaluation_completed=False,
            final_evaluation_reason="budget_reached",
            selection_score=result.best_score,
        )
        raise stop
    return response

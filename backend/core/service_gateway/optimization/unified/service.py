"""Worker entry point for a unified (wizard v2) run.

``run_unified_optimization`` splits the run's cases server side, builds the
workspace scorer (the user's run code plus metric, in the sandbox) and hands
the job to the black-box run plumbing: the chosen optimizer searches on the
train cases, the winner and the regression guard are decided on validation,
and the reported scores are measured on the held-out test cases. DSPy
optimizers run as the :class:`~.dspy_engine.DspyEngine`; every other choice
runs a black-box engine or the Auto strategy.
"""

from __future__ import annotations

import time
from typing import Any

from ....billing.model_gateway import ROUTE_KEY
from ....config import settings
from ....exceptions import ServiceError
from ....models.blackbox import BLACKBOX_STRATEGY_AUTO, BlackboxRunResponse, BlackboxStrategy
from ....models.unified import (
    DSPY_OPTIMIZER_NAMES,
    UnifiedRunRequest,
    optimizer_family,
    resolve_optimizer,
)
from ...language_models import build_language_model
from ..blackbox.agent_eval import gateway_from_settings
from ..blackbox.agent_runs import AgentRunSink
from ..blackbox.harness import GatewayConfig
from ..blackbox.service import JobSpec, ProgressCallback, run_job, validate_job
from ..blackbox.workspace_scorer import build_workspace_scorer
from .split import split_cases, split_seed


def unified_strategy(resolved: str) -> BlackboxStrategy:
    """Map a resolved optimizer onto the black-box strategy that runs it.

    Args:
        resolved: The optimizer after resolving Auto.

    Returns:
        The Auto strategy for ``"auto"``, otherwise one engine (DSPy
        optimizers are registered engines too).
    """
    if optimizer_family(resolved) == "auto":
        return BlackboxStrategy(mode=BLACKBOX_STRATEGY_AUTO)
    return BlackboxStrategy(mode="single", engine=resolved)


def _gateway(payload: UnifiedRunRequest) -> GatewayConfig:
    """Pick the model gateway the workspace code calls through.

    Args:
        payload: The unified run.

    Returns:
        The run's budget route when it has one, otherwise the deployment gateway.

    Raises:
        ServiceError: When neither is configured.
    """
    route = payload.models.reflection_model.extra.get(ROUTE_KEY)
    if route:
        return GatewayConfig(url=route["url"], api_key=route["token"])
    gateway = gateway_from_settings(settings)
    if gateway is None:
        raise ServiceError("A unified run needs a model gateway for its workspace code.")
    return gateway


def unified_job_spec(
    payload: UnifiedRunRequest, *, seed: int, task_lm: Any | None, metric_lm: Any | None = None
) -> JobSpec:
    """Map a unified request onto the shared black-box job spec.

    Args:
        payload: The unified run.
        seed: The run's split seed, also the engines' seed.
        task_lm: The metered task model an in-process DSPy program calls, if any.
        metric_lm: The metered metric model an in-process DSPy metric's ``llm`` calls, if any.

    Returns:
        The job spec, with the private run context engines like DSPy read.
        The context carries the metric source and is never serialized.
    """
    resolved = resolve_optimizer(payload.optimizer.choice, payload.task.artifact.kind)
    models = payload.models
    context: dict[str, Any] = {
        "artifact_kind": payload.task.artifact.kind,
        "run_code": payload.workspace.run_code,
        "metric_code": payload.workspace.metric_code,
        "files": dict(payload.workspace.files),
        "input_fields": list(payload.data.input_fields),
        "expected_field": payload.data.expected_field,
        "models": {
            "task_model": models.task_model.model_dump() if models.task_model else None,
            "reflection_model": models.reflection_model.model_dump(),
            "metric_model": models.metric_model.model_dump() if models.metric_model else None,
        },
        "dspy_optimizer": DSPY_OPTIMIZER_NAMES.get(resolved),
        "dspy_kwargs": dict(payload.optimizer.dspy_kwargs),
        "task_lm": task_lm,
        "metric_lm": metric_lm,
    }
    return JobSpec(
        objective=payload.task.objective,
        background=payload.task.background or None,
        seed_candidate=payload.task.artifact.starting_version,
        seed=seed,
        budget=payload.optimizer.budget,
        strategy=unified_strategy(resolved),
        reflection_model_settings=models.reflection_model,
        proposer=payload.optimizer.proposer,
        shinka=payload.optimizer.shinka,
        extra_reflection_model_settings=list(models.extra_reflection_models),
        max_cost_cents=payload.max_cost_cents,
        execution_budget_id=payload.execution_budget_id,
        optimizer_name=resolved,
        optimizer_resolved=resolved,
        context=context,
        extra_lms=[lm for lm in (task_lm, metric_lm) if lm is not None],
        extra_lm_sources=[
            config.token_source
            for lm, config in (
                (task_lm, models.task_model or models.reflection_model),
                (metric_lm, models.metric_model or models.reflection_model),
            )
            if lm is not None
        ],
    )


def run_unified_optimization(
    payload: UnifiedRunRequest,
    *,
    artifact_id: str,
    artifacts_dir: str | None = None,
    progress_callback: ProgressCallback | None = None,
    gepa_log_dir_path: str | None = None,
    agent_run_sink: AgentRunSink | None = None,
    target_route: dict[str, str] | None = None,
    evaluator_route: dict[str, str] | None = None,
    repo_snapshot: dict[str, Any] | None = None,
) -> BlackboxRunResponse:
    """Run one unified job end to end.

    Args:
        payload: The submitted unified run; its cases must already be inline.
        artifact_id: Job id, used to name the workspace.
        artifacts_dir: Workspace for engine state when ``gepa_log_dir_path`` is unset.
        progress_callback: The job's progress sink, if any.
        gepa_log_dir_path: Workspace for engine state; a temp dir when neither is set.
        agent_run_sink: Receives each sandboxed agent run's record and live transcript, if any.
        target_route: Opaque parent route for the optimized task model.
        evaluator_route: Accepted for parity with the black-box entry point; a
            unified run scores with its own workspace metric, never an external evaluator.
        repo_snapshot: A repository run's packed tree, as uploaded into this box.

    Returns:
        The winner with validation (selection) and test (reported) scores,
        the split counts, the resolved optimizer and, for a DSPy optimizer,
        the compiled program.

    Raises:
        ServiceError: When the cases are not inline, no gateway is configured,
            or the job cannot run.
        BudgetReached: When the run's budget stopped it.
    """
    started = time.perf_counter()
    if payload.data.cases is None:
        raise ServiceError("A unified run needs its cases inline; resolve the staged dataset before queueing it.")
    seed = split_seed(payload)
    splits = split_cases(
        list(payload.data.cases), payload.data.mode, payload.data.split, shuffle=payload.data.shuffle, seed=seed
    )
    resolved = resolve_optimizer(payload.optimizer.choice, payload.task.artifact.kind)
    task_lm = None
    metric_lm = None
    if optimizer_family(resolved) == "dspy":
        task_lm = build_language_model(payload.models.task_model or payload.models.reflection_model)
        if payload.models.metric_model is not None:
            metric_lm = build_language_model(payload.models.metric_model)
    job = unified_job_spec(payload, seed=seed, task_lm=task_lm, metric_lm=metric_lm)
    validate_job(job)
    protected = payload.execution_budget_id is not None or ROUTE_KEY in payload.models.reflection_model.extra
    scorer = build_workspace_scorer(payload, _gateway(payload), protected=protected)
    return run_job(
        job,
        scorer,
        splits,
        artifact_id=artifact_id,
        started=started,
        progress_callback=progress_callback,
        gepa_log_dir_path=gepa_log_dir_path or artifacts_dir,
        agent_run_sink=agent_run_sink,
        target_route=target_route,
        repo_snapshot=repo_snapshot,
    )

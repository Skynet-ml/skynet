"""Verify Anything and unified inputs from inside the selected outer execution sandbox."""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import nullcontext
from typing import Any

from ....billing.model_gateway import ROUTE_KEY
from ....billing.runtime import UsagePendingError
from ....config import settings
from ....exceptions import ServiceError
from ....models import BlackboxRunRequest
from ....models.blackbox import BlackboxScorer, BlackboxTarget
from ....models.unified import (
    CONTRACT_SAMPLE_COUNT_KEY,
    CONTRACT_SAMPLE_DEFAULT,
    CONTRACT_SAMPLE_MAX,
    UNIFIED_PAYLOAD_KIND,
    UnifiedRunRequest,
)
from ..unified.contract import run_contract_check
from ..unified.metric_model import scan_metric_llm
from ..unified.service import unified_job_spec
from ..unified.split import split_seed
from .agent_eval import READINESS_LIFETIME_SECONDS, case_lifetime_seconds
from .harness import GatewayConfig
from .native_runtime import NativeOptions, check_native_runtime
from .sandbox import SandboxRuntime, sandbox_runtime_context
from .sandbox_scorer import SandboxPythonScorer, scorer_gateway
from .scorer import build_scorer
from .service import JobSpec, _agent_scorer, uses_native_runtime, validate_blackbox_payload, validate_job

# What a scorer box needs around the evaluator's own timeout: booting and the install.
_SCORER_BOX_OVERHEAD_SECONDS = 60.0
# What the outer box spends around its inner boxes: booting, importing the
# runner, the model route checks and the native runtime probe's three minutes.
_PREFLIGHT_OVERHEAD_SECONDS = 600.0


def unified_sample_count(payload: dict[str, Any]) -> int:
    """Return how many train/validation cases a unified setup check scores.

    Args:
        payload: Canonical unified request, possibly carrying the contract check's count.

    Returns:
        The requested count clamped to ``1..CONTRACT_SAMPLE_MAX``, or the default when none was sent.
    """
    value = payload.get(CONTRACT_SAMPLE_COUNT_KEY)
    if not isinstance(value, int) or isinstance(value, bool):
        return CONTRACT_SAMPLE_DEFAULT
    return min(max(value, 1), CONTRACT_SAMPLE_MAX)


def preflight_lifetime_seconds(payload: dict[str, Any]) -> float:
    """Return the outer box lifetime one setup check can need.

    A box reserves funds for its whole lifetime the moment it opens, so
    the check gets the time its own inner boxes can take on top of the fixed
    overhead, not the run's configured ceiling: that is hours longer and its
    hold refused most spending limits before a single check ran.

    Args:
        payload: Canonical scorer and target inputs.

    Returns:
        The lifetime, in seconds, before the configured ceiling applies.
    """
    if payload.get("payload_kind") == UNIFIED_PAYLOAD_KIND:
        timeout = float((payload.get("workspace") or {}).get("metric_timeout_seconds") or 60.0)
        # One workspace box scores every sample in turn.
        return _PREFLIGHT_OVERHEAD_SECONDS + unified_sample_count(payload) * timeout + _SCORER_BOX_OVERHEAD_SECONDS
    scorer = BlackboxScorer.model_validate(payload.get("scorer") or {})
    target = BlackboxTarget.model_validate(payload.get("target") or {})
    # The readiness probe and the sample score each open a scorer box.
    seconds = _PREFLIGHT_OVERHEAD_SECONDS + 2 * (scorer.timeout_seconds + _SCORER_BOX_OVERHEAD_SECONDS)
    if target.kind == "agent":
        # The harness is probed in its own box before one case runs with
        # every setup step it could carry.
        seconds += READINESS_LIFETIME_SECONDS + case_lifetime_seconds(target.timeout_seconds, setup_steps=3)
    return seconds


def _check(key: str, status: str, message: str | None = None, field: str | None = None) -> dict[str, Any]:
    """Build one scoped readiness result."""
    return {
        "key": key,
        "status": status,
        **({"message": message} if message else {}),
        **({"field": field} if field else {}),
    }


def _sample(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Choose the case the dry run scores.

    Args:
        payload: Canonical scorer inputs.

    Returns:
        The first case, or None for a task without cases.
    """
    cases = payload.get("cases") or []
    return cases[0] if cases else None


def _needs_model(code: str) -> bool:
    """Detect direct scorer model helper use before an inherited model is chosen."""
    return scan_metric_llm(code).uses_llm


def verify_anything_in_sandbox(
    payload: dict[str, Any], *, scope: str, identity: str, runtime: SandboxRuntime
) -> dict[str, Any]:
    """Verify evaluator, target, and optimizer dependencies in one outer sandbox.

    Args:
        payload: Protected Anything request containing opaque parent capabilities.
        scope: Evaluation or complete execution readiness.
        identity: Stable preflight evidence identity.
        runtime: Contained command runtime bound to the selected outer sandbox.

    Returns:
        Real readiness checks and, when a seed exists, its actual scorer preview.
    """
    scorer = BlackboxScorer.model_validate(payload.get("scorer") or {})
    remote = scorer.kind == "remote"
    code = str(scorer.metric_code)
    if not remote and scorer.model is None and _needs_model(code):
        return {
            "checks": [
                _check(
                    "scorer.model",
                    "pending",
                    "Choose the model on Optimization to finish this evaluator check.",
                    "scorer.model",
                )
            ]
        }
    if remote:
        route = payload.get("_skynet_evaluator_route")
        if not route:
            raise ValueError("Remote evaluation requires its protected parent relay.")
        scorer_instance = build_scorer(scorer, job_id=identity, protected_route=route)
    else:
        scorer_instance = SandboxPythonScorer(
            code,
            runtime=runtime,
            gateway=scorer_gateway(scorer.model, settings) if scorer.model else None,
            timeout_seconds=scorer.timeout_seconds,
            lifetime_seconds=scorer.timeout_seconds + _SCORER_BOX_OVERHEAD_SECONDS,
            install_command=scorer.install_command,
            dependency_lock=scorer.dependency_lock.model_dump(mode="json") if scorer.dependency_lock else None,
            job_id=identity,
        )
    checks: list[dict[str, Any]] = []
    result: dict[str, Any] = {"checks": checks}
    started = time.perf_counter()
    with sandbox_runtime_context(runtime):
        try:
            candidate = payload.get("seed_candidate")
            if candidate is None:
                if not remote:
                    scorer_instance.check_ready()
                else:
                    result["evaluator_readiness"] = {
                        "endpoint": "validated_and_pinned",
                        "candidate_evaluation": "awaiting_first_generated_candidate",
                        "external_service_fees": "excluded_from_skynet_total",
                    }
                checks.append(
                    _check(
                        "scorer.readiness",
                        "succeeded",
                        "Endpoint validated; its response will be checked with the first generated candidate."
                        if remote
                        else "Evaluator loads; scoring waits for the first generated candidate.",
                    )
                )
                if scope == "execution":
                    target = BlackboxTarget.model_validate(payload.get("target") or {})
                    if target.kind == "agent":
                        agent = _agent_scorer(
                            scorer_instance,
                            target,
                            job_id=identity,
                            progress_callback=None,
                            agent_run_sink=None,
                            target_route=payload.get("_skynet_target_route"),
                        )
                        result["agent_readiness"] = agent.check_ready()
                        checks.append(
                            _check(
                                "target.readiness",
                                "succeeded",
                                "Harness readiness verified; candidate execution waits for the first generated candidate.",
                                "target",
                            )
                        )
            else:
                target = BlackboxTarget.model_validate(payload.get("target") or {})
                if target.kind == "agent":
                    evaluate = _agent_scorer(
                        scorer_instance,
                        target,
                        job_id=identity,
                        progress_callback=None,
                        agent_run_sink=None,
                        target_route=payload.get("_skynet_target_route"),
                    )
                    score, side_info = evaluate(candidate, _sample(payload))
                    preview = {"ok": True, "score": score, "side_info": side_info}
                elif remote:
                    try:
                        score, side_info = scorer_instance(candidate, _sample(payload))
                        preview = {"ok": True, "score": score, "side_info": side_info}
                    except ServiceError as error:
                        preview = {"ok": False, "score": None, "side_info": {}, "error": str(error)}
                else:
                    probe = scorer_instance.run(candidate, _sample(payload))
                    preview = {
                        "ok": probe.error is None,
                        "score": probe.score,
                        "side_info": probe.side_info,
                        "error": probe.error,
                    }
                checks.append(
                    _check("scorer", "succeeded" if preview["ok"] else "failed", preview.get("error"), "scorer")
                )
                result["scorer_result"] = {
                    **preview,
                    "elapsed_ms": (time.perf_counter() - started) * 1000,
                }
            if scope == "execution":
                public = {key: value for key, value in payload.items() if not key.startswith("_")}
                typed = BlackboxRunRequest.model_validate(public)
                validate_blackbox_payload(typed, verify_scorer=False)
                native = uses_native_runtime(typed)
                if native:
                    route = typed.reflection_model_settings.extra[ROUTE_KEY]
                    readiness = check_native_runtime(
                        NativeOptions(
                            runtime=typed.proposer_runtime,
                            model=route["model"],
                            gateway=GatewayConfig(url=route["url"], api_key=route["token"]),
                            max_token_cost=0,
                            budget_route=route,
                            sandbox_runtime=runtime,
                        )
                    )
                    # Auto merely loses its AutoSaddler or ShinkaEvolve lane on such an image;
                    # a single run of either has nothing left to do.
                    if (
                        typed.strategy.mode == "single"
                        and typed.strategy.engine == "autosaddler"
                        and not readiness.get("autosaddler_ready")
                    ):
                        raise ServiceError(
                            "This runtime does not ship the pinned AutoSaddler package on Python 3.12; "
                            "choose another engine or Auto."
                        )
                    if (
                        typed.strategy.mode == "single"
                        and typed.strategy.engine == "shinka_evolve"
                        and not readiness.get("shinka_ready")
                    ):
                        raise ServiceError(
                            "This runtime does not ship the pinned ShinkaEvolve package on Python 3.12; "
                            "choose another engine or Auto."
                        )
                checks.append(_check("optimizer", "succeeded"))
        finally:
            try:
                scorer_instance.close()
            except UsagePendingError:
                checks.append(_check("usage", "pending", "Sandbox usage is awaiting confirmation."))
    return result


def _check_native_readiness(job: JobSpec, runtime: SandboxRuntime | None) -> None:
    """Probe the native proposer runtime a job's engines need, when they need it.

    Args:
        job: The run's job spec.
        runtime: Contained command runtime bound to the outer sandbox, if any.

    Raises:
        ServiceError: When a single native engine's package is missing from the image.
    """
    if not uses_native_runtime(job):
        return
    route = job.reflection_model_settings.extra[ROUTE_KEY]
    readiness = check_native_runtime(
        NativeOptions(
            runtime=job.proposer_runtime,
            model=route["model"],
            gateway=GatewayConfig(url=route["url"], api_key=route["token"]),
            max_token_cost=0,
            budget_route=route,
            sandbox_runtime=runtime,
        )
    )
    # Auto merely loses its AutoSaddler or ShinkaEvolve lane on such an image.
    if (
        job.strategy.mode == "single"
        and job.strategy.engine == "autosaddler"
        and not readiness.get("autosaddler_ready")
    ):
        raise ServiceError(
            "This runtime does not ship the pinned AutoSaddler package on Python 3.12; choose another engine or Auto."
        )
    if job.strategy.mode == "single" and job.strategy.engine == "shinka_evolve" and not readiness.get("shinka_ready"):
        raise ServiceError(
            "This runtime does not ship the pinned ShinkaEvolve package on Python 3.12; choose another engine or Auto."
        )


def verify_unified_in_sandbox(
    payload: dict[str, Any],
    scope: str,
    *,
    identity: str | None = None,
    runtime: SandboxRuntime | None = None,
    on_ready: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Run a unified run's contract check, and in execution scope its engine readiness, in one outer sandbox.

    The contract check scores the starting version on train and validation
    samples only; the held-out test cases never run here.

    Args:
        payload: Protected unified request containing opaque parent capabilities.
        scope: ``evaluation`` or ``execution`` readiness.
        identity: Stable preflight evidence identity, naming the workspace box.
        runtime: Contained command runtime bound to the outer sandbox, or the
            trusted parent's own box runtime; the configured scorer runtime when unset.
        on_ready: Called once the workspace box is warm and scoring starts.

    Returns:
        ``{"checks": [...], "workflow_result": {"contract": ContractCheckResponse}}`` with the
        ``metric.contract`` check carrying the contract outcome, and an
        ``optimizer`` check in execution scope.

    Raises:
        ServiceError: When the sandbox cannot run the workspace, or an engine is unavailable.
    """
    public = {key: value for key, value in payload.items() if not key.startswith("_")}
    typed = UnifiedRunRequest.model_validate(public)
    checks: list[dict[str, Any]] = []
    result: dict[str, Any] = {"checks": checks}
    route = typed.models.reflection_model.extra.get(ROUTE_KEY)
    gateway = GatewayConfig(url=route["url"], api_key=route["token"]) if isinstance(route, dict) else None
    samples = unified_sample_count(payload)
    scope_runtime = sandbox_runtime_context(runtime) if runtime is not None else nullcontext()
    with scope_runtime:
        try:
            contract = run_contract_check(
                typed,
                samples,
                gateway=gateway,
                protected=gateway is not None,
                runtime=runtime,
                job_id=identity,
                lifetime_seconds=samples * typed.workspace.metric_timeout_seconds + _SCORER_BOX_OVERHEAD_SECONDS,
                dependency_route=payload.get("_skynet_packages_route"),
                on_ready=on_ready,
            )
        except UsagePendingError:
            checks.append(_check("usage", "pending", "Sandbox usage is awaiting confirmation."))
            return result
        result["workflow_result"] = {"contract": contract.model_dump(mode="json")}
        failure = next((item for item in contract.checks if item.status == "failed"), None)
        message = None
        if failure is not None:
            message = f"{failure.message}\n{failure.detail}" if failure.detail else failure.message
        checks.append(_check("metric.contract", "succeeded" if contract.ok else "failed", message, "workspace"))
        if scope == "execution" and contract.ok:
            job = unified_job_spec(typed, seed=split_seed(typed), task_lm=None)
            validate_job(job)
            _check_native_readiness(job, runtime)
            checks.append(_check("optimizer", "succeeded"))
    return result

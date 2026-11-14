"""Verify Anything inputs from inside the selected outer execution sandbox."""

from __future__ import annotations

import time
from typing import Any

from ....billing.model_gateway import ROUTE_KEY
from ....billing.runtime import UsagePendingError
from ....config import settings
from ....exceptions import ServiceError
from ....i18n_en import t_en
from ....models import BlackboxRunRequest
from ....models.blackbox import BLACKBOX_TARGET_REPO, BlackboxScorer, BlackboxTarget
from ....provider_registry import LITELLM_TO_BYOK_PROVIDER
from ..unified.metric_model import resolve_blackbox_metric_model_dict, scan_metric_llm
from .agent_eval import READINESS_LIFETIME_SECONDS, case_lifetime_seconds
from .harness import GatewayConfig
from .native_runtime import NativeOptions, check_native_runtime
from .repo_scorer import REPO_SCORER_URL, SETUP_ALLOWANCE_SECONDS
from .sandbox_contract import SandboxRuntime
from .sandbox_runtime import sandbox_runtime_context
from .sandbox_scorer import SandboxPythonScorer, scorer_gateway
from .scorer import RemoteScorer, build_scorer
from .service import _agent_scorer, uses_native_runtime, validate_blackbox_payload


def _reflection_bases(config: Any) -> dict[str, str | None]:
    """Retain an available reflection endpoint for setup validation.

    Args:
        config: Reflection model configuration.

    Returns:
        The provider's endpoint when available, otherwise an empty mapping.
    """
    name = config.name.split("/", 1)[0]
    provider = config.byok_provider or LITELLM_TO_BYOK_PROVIDER.get(name, name)
    base = config.base_url or config.extra.get("api_base") or config.extra.get("base_url")
    return {provider: base} if provider and base else {}


# What a scorer box needs around the evaluator's own timeout: booting and the install.
_SCORER_BOX_OVERHEAD_SECONDS = 60.0
# What the outer box spends around its inner boxes: booting, importing the
# runner, the model route checks and the native runtime probe's three minutes.
_PREFLIGHT_OVERHEAD_SECONDS = 600.0


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
    scorer = BlackboxScorer.model_validate(payload.get("scorer") or {})
    target = BlackboxTarget.model_validate(payload.get("target") or {})
    # The readiness probe and the sample score each open a scorer box.
    seconds = _PREFLIGHT_OVERHEAD_SECONDS + 2 * (scorer.timeout_seconds + _SCORER_BOX_OVERHEAD_SECONDS)
    if target.kind == "agent":
        # The harness is probed in its own box before one case runs with
        # every setup step it could carry.
        seconds += READINESS_LIFETIME_SECONDS + case_lifetime_seconds(target.timeout_seconds, setup_steps=3)
    return seconds


def judge_lifetime_seconds(payload: dict[str, Any], case_count: int) -> float:
    """Return the box lifetime one judge call (SKY-696) can need.

    A judge call opens no guest. It runs the setup box, then one run box and
    one metric box for each case, so the lifetime grows with the cases.

    Args:
        payload: The judge session's run payload.
        case_count: How many cases the call scores; 0 scores the version once.

    Returns:
        The lifetime, in seconds, before the configured ceiling applies.
    """
    scorer = BlackboxScorer.model_validate(payload.get("scorer") or {})
    boxes = max(case_count, 1) + 1
    return _PREFLIGHT_OVERHEAD_SECONDS + boxes * (scorer.timeout_seconds + _SCORER_BOX_OVERHEAD_SECONDS)


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
    error = resolve_blackbox_metric_model_dict(payload)
    if error:
        raise ValueError(t_en(error))
    scorer = BlackboxScorer.model_validate(payload.get("scorer") or {})
    # A repository's scorer runs in the parent's box on the staged checkout, as in the run (SKY-644).
    repo = (payload.get("target") or {}).get("kind") == BLACKBOX_TARGET_REPO
    remote = scorer.kind == "remote" or repo
    code = str(scorer.metric_code)
    if scorer.kind != "remote" and scorer.model is None and _needs_model(code):
        return {
            "checks": [
                _check(
                    "scorer.model",
                    "pending",
                    "Set the model with llm(model=...) in the metric code.",
                    "scorer.model",
                )
            ]
        }
    if remote:
        route = payload.get("_skynet_evaluator_route")
        if not route:
            raise ValueError(
                "Repository scoring requires its protected parent scorer."
                if repo
                else "Remote evaluation requires its protected parent relay."
            )
        if repo:
            scorer_instance = RemoteScorer(
                REPO_SCORER_URL,
                secret=None,
                timeout_seconds=scorer.timeout_seconds + SETUP_ALLOWANCE_SECONDS,
                protected_route=route,
                label="repository scorer",
            )
        else:
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
            if candidate is None and repo:
                # A repository version is a patch: the empty patch scores the staged checkout itself,
                # so a metric that cannot run fails here, not in the run.
                candidate = ""
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
                validate_blackbox_payload(
                    typed, verify_scorer=False, api_bases=_reflection_bases(typed.reflection_model_settings)
                )
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

"""Verify Anything inputs from inside the selected outer execution sandbox."""

from __future__ import annotations

import time
from typing import Any

from ....billing.runtime import UsagePendingError
from ....exceptions import ServiceError
from ....i18n_en import t_en
from ....models import BlackboxRunRequest
from ....models.blackbox import BLACKBOX_TARGET_REPO, STORED_PAYLOAD_CONTEXT, TARGET_RETIRED, BlackboxScorer
from ....provider_registry import LITELLM_TO_BYOK_PROVIDER
from ..unified.metric_model import resolve_blackbox_metric_model_dict, scan_metric_llm
from .repo_scorer import REPO_SCORER_URL, SETUP_ALLOWANCE_SECONDS
from .sandbox_contract import SandboxRuntime
from .sandbox_runtime import sandbox_runtime_context
from .scorer import RemoteScorer
from .service import validate_blackbox_payload


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


SCORER_NOT_CHECKED = "Sample score not checked: the run prepares your image and scores there."

# What a scorer box needs around the evaluator's own timeout: booting and the install.
_SCORER_BOX_OVERHEAD_SECONDS = 60.0
# What the outer box spends around its inner boxes: booting, importing the
# runner and the model route checks.
_PREFLIGHT_OVERHEAD_SECONDS = 600.0


def preflight_lifetime_seconds(payload: dict[str, Any]) -> float:
    """Return the outer box lifetime one setup check can need.

    A box reserves funds for its whole lifetime the moment it opens, so
    the check gets the time its own inner boxes can take on top of the fixed
    overhead, not the run's configured ceiling: that is hours longer and its
    hold refused most spending limits before a single check ran.

    Args:
        payload: Canonical scorer inputs.

    Returns:
        The lifetime, in seconds, before the configured ceiling applies.
    """
    scorer = BlackboxScorer.model_validate(payload.get("scorer") or {})
    # The readiness probe and the sample score each open a scorer box.
    return _PREFLIGHT_OVERHEAD_SECONDS + 2 * (scorer.timeout_seconds + _SCORER_BOX_OVERHEAD_SECONDS)


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


def _validate_optimizer(payload: dict[str, Any]) -> None:
    """Validate the run request's optimizer settings without scoring.

    Args:
        payload: Protected Anything request; only its public keys are read.
    """
    public = {key: value for key, value in payload.items() if not key.startswith("_")}
    # The guest is not the local stack and has no access to the user's folder. The
    # parent checked the request already, so the checks that read this computer are skipped.
    typed = BlackboxRunRequest.model_validate(public, context=STORED_PAYLOAD_CONTEXT)
    validate_blackbox_payload(typed, verify_scorer=False, api_bases=_reflection_bases(typed.reflection_model_settings))


def verify_anything_in_sandbox(
    payload: dict[str, Any],
    *,
    scope: str,
    identity: str,
    runtime: SandboxRuntime,
    unchecked: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Verify evaluator, target, and optimizer dependencies in one outer sandbox.

    Args:
        payload: Protected Anything request containing opaque parent capabilities.
        scope: Evaluation or complete execution readiness.
        identity: Stable preflight evidence identity.
        runtime: Contained command runtime bound to the selected outer sandbox.
        unchecked: Roles with a user image. A version needs both roles' boxes,
            so the sample is not scored when any role is here (SKY-893).

    Returns:
        Real readiness checks and, when a seed exists, its actual scorer preview.
    """
    error = resolve_blackbox_metric_model_dict(payload)
    if error:
        raise ValueError(t_en(error))
    scorer = BlackboxScorer.model_validate(payload.get("scorer") or {})
    if (payload.get("target") or {}).get("kind") != BLACKBOX_TARGET_REPO:
        raise ValueError(TARGET_RETIRED)
    if scorer.kind != "remote" and scorer.model is None and _needs_model(str(scorer.metric_code)):
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
    if unchecked:
        checks = [_check("scorer", "skipped", SCORER_NOT_CHECKED, "scorer")]
        if scope == "execution":
            _validate_optimizer(payload)
            checks.append(_check("optimizer", "succeeded"))
        return {"checks": checks}
    route = payload.get("_skynet_evaluator_route")
    if not route:
        raise ValueError("Repository scoring requires its protected parent scorer.")
    # A repository's scorer runs in the parent's box on the staged checkout, as in the run (SKY-644).
    scorer_instance = RemoteScorer(
        REPO_SCORER_URL,
        secret=None,
        timeout_seconds=scorer.timeout_seconds + SETUP_ALLOWANCE_SECONDS,
        protected_route=route,
        label="repository scorer",
        trusted_reply=True,
    )
    checks: list[dict[str, Any]] = []
    result: dict[str, Any] = {"checks": checks}
    started = time.perf_counter()
    with sandbox_runtime_context(runtime):
        try:
            # A repository version is a patch: the empty patch scores the staged checkout itself,
            # so a metric that cannot run fails here, not in the run.
            candidate = payload.get("seed_candidate")
            if candidate is None:
                candidate = ""
            try:
                score, side_info = scorer_instance(candidate, _sample(payload))
                preview = {"ok": True, "score": score, "side_info": side_info}
            except ServiceError as error:
                preview = {"ok": False, "score": None, "side_info": {}, "error": str(error)}
            checks.append(_check("scorer", "succeeded" if preview["ok"] else "failed", preview.get("error"), "scorer"))
            result["scorer_result"] = {
                **preview,
                "elapsed_ms": (time.perf_counter() - started) * 1000,
            }
            if scope == "execution":
                _validate_optimizer(payload)
                checks.append(_check("optimizer", "succeeded"))
        finally:
            try:
                scorer_instance.close()
            except UsagePendingError:
                checks.append(_check("usage", "pending", "Sandbox usage is awaiting confirmation."))
    return result

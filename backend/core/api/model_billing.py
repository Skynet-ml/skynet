"""Canonicalize per-model billing sources before setup or submission."""

from __future__ import annotations

from typing import Any

from ..billing.byok_bridge import payload_uses_token_source
from ..config import settings
from ..constants import TOKEN_SOURCE_BYOK, TOKEN_SOURCE_MANAGED, TOKEN_SOURCE_SUBSCRIPTION
from ..local.cli_catalog import parse_subscription_model_id
from ..models import BlackboxRunRequest, RunRequest, UnifiedRunRequest
from ..models.common import ModelConfig
from ..models.submissions import _OptimizationRequestBase
from ..service_gateway.optimization.unified.metric_model import resolve_metric_model
from .errors import DomainError

# The Mac app's own sandbox runtime; a hosted deployment runs only Vercel.
LOCAL_EXECUTION_RUNTIME = "docker"


def request_model_configs(
    payload: _OptimizationRequestBase | BlackboxRunRequest | UnifiedRunRequest,
) -> list[ModelConfig]:
    """Return every executable model config carried by a submission.

    Args:
        payload: Run, Anything, or unified request.

    Returns:
        Model configs in execution order.
    """
    if isinstance(payload, BlackboxRunRequest):
        return [
            config
            for config in (
                payload.task_model_settings,
                payload.reflection_model_settings,
                *payload.extra_reflection_model_settings,
                payload.scorer.model,
            )
            if config is not None
        ]
    if isinstance(payload, RunRequest):
        return [
            config
            for config in (
                payload.model_settings,
                payload.reflection_model_settings,
                payload.task_model_settings,
            )
            if config is not None
        ]
    if isinstance(payload, UnifiedRunRequest):
        models = payload.models
        if models.reflection_model.token_source is None:
            models.reflection_model.token_source = payload.token_source
        error = resolve_metric_model(models, payload.workspace.metric_code)
        if error:
            raise DomainError(error, status=422)
        return [
            config
            for config in (
                models.task_model,
                models.reflection_model,
                *models.extra_reflection_models,
                models.metric_model,
            )
            if config is not None
        ]
    return []


def normalize_model_token_sources(
    payload: _OptimizationRequestBase | BlackboxRunRequest | UnifiedRunRequest,
) -> tuple[list[ModelConfig], dict[str, str]]:
    """Resolve legacy job-level sources into explicit per-model sources.

    The run-level source becomes ``byok`` or ``subscription`` when every model
    uses it, and ``managed`` otherwise. In local mode a
    ``subscription/<cli>/<slug>`` name is a subscription model on that CLI,
    whatever source the request names.

    Args:
        payload: Run, Anything, or unified request to normalize in place.

    Returns:
        The model configs and their normalized model-to-source billing map.

    Raises:
        DomainError: When one model id is assigned conflicting sources.
    """
    configs = request_model_configs(payload)
    sources: dict[str, str] = {}
    for config in configs:
        # The agent wizard submits a listed /models/for-agent name with no source.
        subscription = parse_subscription_model_id(config.normalized_identifier()) if settings.local_mode else None
        if subscription is not None:
            config.token_source = TOKEN_SOURCE_SUBSCRIPTION
            config.cli_provider = subscription[0]
        source = config.token_source or payload.token_source
        config.token_source = source
        config.base_url = None
        for field in ("api_key", "api_base", "base_url"):
            config.extra.pop(field, None)
        if source == TOKEN_SOURCE_MANAGED:
            config.byok_provider = None
        model = config.normalized_identifier()
        existing = sources.get(model)
        if existing is not None and existing != source:
            raise DomainError("submission.validation_failed", status=400)
        sources[model] = source
    payload.token_source = TOKEN_SOURCE_MANAGED
    for uniform in (TOKEN_SOURCE_BYOK, TOKEN_SOURCE_SUBSCRIPTION):
        if sources and all(source == uniform for source in sources.values()):
            payload.token_source = uniform
    return configs, sources


def enforce_local_mode_sources(
    payload: _OptimizationRequestBase | BlackboxRunRequest | UnifiedRunRequest,
    requested_source: str,
    model_configs: list[ModelConfig],
) -> None:
    """Keep subscription runs in local mode and managed runs out of it.

    A subscription model or a host-CLI proposer runs on the user's own Mac, so
    a hosted deployment has no way to serve it. The Mac app has no managed
    model routing, so a managed model there could never run.

    Args:
        payload: The submission or setup check, read for its proposer's ``host_cli``.
        requested_source: The run-level ``token_source`` before normalization,
            which folds ``subscription`` into ``managed``.
        model_configs: Executable configs with normalized per-model sources.

    Raises:
        DomainError: 422 ``models.subscription_local_only`` outside local mode;
            422 ``local.not_available`` for a Docker execution or proposer
            runtime outside local mode;
            422 ``models.managed_unavailable_local`` in local mode.
    """
    sources = {config.token_source for config in model_configs}
    optimizer = getattr(payload, "optimizer", None)
    proposer = getattr(payload, "proposer", None) or getattr(optimizer, "proposer", None)
    host_cli = getattr(proposer, "host_cli", None) is not None
    if not settings.local_mode:
        if TOKEN_SOURCE_SUBSCRIPTION in sources or requested_source == TOKEN_SOURCE_SUBSCRIPTION or host_cli:
            raise DomainError("models.subscription_local_only", status=422)
        runtimes = (getattr(payload, "execution_runtime", None), getattr(payload, "proposer_runtime", None))
        if LOCAL_EXECUTION_RUNTIME in runtimes:
            raise DomainError("local.not_available", status=422)
        return
    if TOKEN_SOURCE_MANAGED in sources:
        raise DomainError("models.managed_unavailable_local", status=422)


def enforce_raw_local_mode_sources(payload: dict[str, Any]) -> None:
    """Refuse subscription work outside local mode before a payload is typed.

    An Anything evaluation check is not validated into a request, so its raw
    model configs and proposer are read directly.

    Args:
        payload: Raw run or setup payload.

    Raises:
        DomainError: 422 ``models.subscription_local_only`` outside local mode
            when a model uses a subscription or the proposer names a host CLI;
            422 ``local.not_available`` for a Docker execution or proposer
            runtime.
    """
    if settings.local_mode:
        return
    requested = str(payload.get("token_source") or TOKEN_SOURCE_MANAGED)
    optimizer = payload.get("optimizer")
    proposer = payload.get("proposer") or (optimizer.get("proposer") if isinstance(optimizer, dict) else None)
    host_cli = isinstance(proposer, dict) and proposer.get("host_cli") is not None
    if (
        requested == TOKEN_SOURCE_SUBSCRIPTION
        or host_cli
        or payload_uses_token_source(payload, TOKEN_SOURCE_SUBSCRIPTION, default_token_source=requested)
    ):
        raise DomainError("models.subscription_local_only", status=422)
    if LOCAL_EXECUTION_RUNTIME in (payload.get("execution_runtime"), payload.get("proposer_runtime")):
        raise DomainError("local.not_available", status=422)

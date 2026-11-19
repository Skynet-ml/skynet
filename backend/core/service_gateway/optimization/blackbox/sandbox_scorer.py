"""The scorer gateway, the scorer usage ledger and the runner source for repository scorers.

:func:`scorer_gateway` resolves where a scorer's ``llm()`` calls go. Their
token usage lands in a :class:`ScorerUsage` ledger that the run's cost
ceiling and billing read like any other language model. The repository
scorer ships :data:`RUNNER_SOURCE` into its boxes (SKY-834).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ....billing.budgets import BudgetInsufficientError
from ....billing.model_gateway import ROUTE_KEY
from ....billing.operation_pricing import UnpricedOperationError
from ....billing.runtime import UsagePendingError
from ....billing.signals import BudgetReached
from ....config import Settings
from ....exceptions import ServiceError
from ....models.common import ModelConfig
from ...language_models import usage_by_model_from_history
from . import runner
from .agent_eval import gateway_from_settings

RUNNER_SOURCE = Path(runner.__file__).read_text(encoding="utf-8")
_OPENROUTER_PREFIX = "openrouter/"
_OPENROUTER_URL = "https://openrouter.ai/api/v1"
_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")
_GATEWAY_MISSING = (
    "Scorer models need a gateway the sandboxes can reach: set LITELLM_PROXY_URL and "
    "LITELLM_PROXY_API_KEY, or BLACKBOX_AGENT_GATEWAY_URL and BLACKBOX_AGENT_GATEWAY_API_KEY."
)


@dataclass(frozen=True)
class ScorerGateway:
    """Where a sandboxed scorer's ``llm()`` calls go, and how the run bills them."""

    url: str
    model: str
    api_key: str | None
    billing_model: str
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    timeout_seconds: float = 120.0
    protected: bool = False
    subscription: bool = False
    gateway_reasoning: bool = False

    def runner_payload(self) -> dict[str, Any]:
        """Return the ``gateway`` section of a runner call — the key travels separately."""
        payload = {
            "url": self.url,
            "model": self.model,
            "declared_model": self.billing_model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "reasoning_effort": self.reasoning_effort,
            "timeout_seconds": self.timeout_seconds,
        }
        if self.protected:
            payload["protected"] = True
        if self.subscription:
            payload["subscription"] = True
        if self.gateway_reasoning:
            payload["gateway_reasoning"] = True
        return payload

    def injected_headers(self) -> dict[str, dict[str, str]]:
        """Return the headers a network edge adds to the box's requests to the gateway host, keyed by host."""
        host = urlsplit(self.url).hostname
        if not host or not self.api_key:
            return {}
        return {host: {"Authorization": f"Bearer {self.api_key}"}}


def _without_provider(name: str) -> str:
    """Drop the LiteLLM provider prefix a custom endpoint never sees.

    Args:
        name: A ``provider/model`` identifier.

    Returns:
        The identifier the endpoint is sent.
    """
    return name.split("/", 1)[1] if "/" in name else name


def scorer_gateway(config: ModelConfig, settings: Settings) -> ScorerGateway:
    """Resolve the gateway for the model chosen in the Scorer step.

    BYOK configs carry the user's key (the run-path bridge stamps it onto
    ``extra``) and go straight to their provider: the configured ``base_url``,
    else OpenRouter for ``openrouter/`` models. Managed configs go through the
    gateway sandboxes are pointed at — the agent gateway, else the LiteLLM
    proxy — which resolves OpenRouter models without their prefix.

    Args:
        config: The scorer's model configuration.
        settings: The backend settings.

    Returns:
        The gateway the runner posts chat completions to.

    Raises:
        ServiceError: When no reachable gateway can be resolved.
    """
    name = config.name
    effort = config.extra.get("reasoning_effort")
    common: dict[str, Any] = {
        "billing_model": name,
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
        "reasoning_effort": effort if isinstance(effort, str) and effort else None,
        "timeout_seconds": settings.lm_request_timeout_seconds,
        "subscription": config.token_source == "subscription",
    }
    route = config.extra.get(ROUTE_KEY)
    if isinstance(route, dict):
        return ScorerGateway(
            url=route["url"],
            model=route["model"],
            api_key=route["token"],
            protected=True,
            gateway_reasoning=config.token_source != "subscription",
            **common,
        )
    own_key = config.extra.get("api_key")
    if isinstance(own_key, str) and own_key:
        base_url = config.base_url or config.extra.get("api_base")
        if base_url:
            return ScorerGateway(
                url=str(base_url).rstrip("/"), model=_without_provider(name), api_key=own_key, **common
            )
        if name.startswith(_OPENROUTER_PREFIX):
            return ScorerGateway(url=_OPENROUTER_URL, model=name[len(_OPENROUTER_PREFIX) :], api_key=own_key, **common)
        raise ServiceError(
            f"Sandboxed scorers reach a BYOK model through OpenRouter or a base_url; '{name}' has neither."
        )
    gateway = gateway_from_settings(settings)
    if gateway is None:
        raise ServiceError(_GATEWAY_MISSING)
    return ScorerGateway(
        url=gateway.url,
        model=name.removeprefix(_OPENROUTER_PREFIX),
        api_key=gateway.api_key,
        gateway_reasoning=not bool(getattr(settings, "blackbox_agent_gateway_url", None)),
        **common,
    )


class ScorerUsage:
    """Token ledger for one scorer's ``llm()`` calls, shaped like a ``dspy.LM`` for the usage helpers.

    ``model`` and ``history`` are all that ``usage_by_model_from_history``
    and the cost ceiling read off a language model.
    """

    def __init__(self, model: str) -> None:
        """Start an empty ledger.

        Args:
            model: The model the pricing table bills these calls as.
        """
        self.model = model
        self.history: list[dict[str, Any]] = []

    def record(self, entries: Iterable[Mapping[str, Any]]) -> None:
        """Append one history entry per ``llm()`` call the runner reported.

        Args:
            entries: Usage dicts with ``prompt_tokens`` / ``completion_tokens`` / ``total_tokens``.
        """
        for entry in entries:
            self.history.append({"usage": {key: int(entry.get(key) or 0) for key in _USAGE_KEYS}})

    def by_model(self) -> dict[str, tuple[int, int]]:
        """Return ``model → (input_tokens, output_tokens)`` for everything recorded."""
        return dict(usage_by_model_from_history(self) or {})


def _raise_control(result: Mapping[str, Any]) -> None:
    """Propagate a protected runner stop before interpreting scorer output.

    Args:
        result: Decoded runner response.

    Raises:
        BudgetReached: When the parent's run stop latch is set.
        BudgetError: When the next operation has insufficient coverage.
        UsagePendingError: When an attempted operation needs reconciliation.
        UnpricedOperationError: When the parent cannot bound the operation.
    """
    control = result.get("control")
    if not isinstance(control, dict):
        return
    code, message = control.get("code"), str(control.get("message") or "Protected scorer stopped.")
    if code == "budget_reached":
        raise BudgetReached(message)
    if code == "budget_insufficient":
        raise BudgetInsufficientError(message)
    if code == "unpriced_operation":
        raise UnpricedOperationError(message)
    raise UsagePendingError(message)

"""Sandbox gateway helpers that the proposer harnesses and the scorer boxes share.

Every run optimizes a repository (SKY-834). The agent-target scorer that ran a
task harness in a box for each case is gone; what stays is how a box reaches
the model gateway.
"""

from __future__ import annotations

from ....config import Settings
from .harness import GatewayConfig
from .sandbox import sandbox_unavailable_reason

_GATEWAY_MISSING = (
    "Sandboxed harnesses need a model gateway the sandboxes can reach: set BLACKBOX_AGENT_GATEWAY_URL and "
    "BLACKBOX_AGENT_GATEWAY_API_KEY (or LITELLM_PROXY_URL and LITELLM_PROXY_API_KEY)."
)


def gateway_from_settings(settings: Settings) -> GatewayConfig | None:
    """Resolve where sandboxed harnesses send their model calls, or ``None`` when unset.

    The dedicated agent-gateway settings win; the LiteLLM proxy settings are
    the fallback, since that proxy already fronts managed inference.

    Args:
        settings: The backend settings.

    Returns:
        The gateway, or ``None`` when no URL/key pair is configured.
    """
    url = settings.blackbox_agent_gateway_url or settings.litellm_proxy_url
    key = settings.blackbox_agent_gateway_api_key or settings.litellm_proxy_api_key
    if not url or key is None:
        return None
    return GatewayConfig(url=url.rstrip("/"), api_key=key.get_secret_value())


def agent_target_unavailable_reason(settings: Settings) -> str | None:
    """Explain why this deployment cannot run sandboxed harnesses, or return ``None``.

    Args:
        settings: The backend settings.

    Returns:
        A user-facing reason (no sandboxes, or no gateway), or ``None``.
    """
    reason = sandbox_unavailable_reason(settings)
    if reason is not None:
        return reason
    # Local mode routes model calls through the run's own parent gateway
    # (the user's CLI subscriptions), never through a deployment proxy.
    if not settings.local_mode and gateway_from_settings(settings) is None:
        return _GATEWAY_MISSING
    return None

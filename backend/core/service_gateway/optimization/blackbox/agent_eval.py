"""Sandbox gateway helpers that the proposer harnesses and the scorer boxes share.

Every run optimizes a repository (SKY-834). The agent-target scorer that ran a
task harness in a box for each case is gone; what stays is how a box reaches
the model gateway and how a version maps onto files.
"""

from __future__ import annotations

import posixpath
from typing import Any

from ....config import Settings
from ....exceptions import ServiceError
from .harness import GatewayConfig
from .protocol import Candidate
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


def safe_relative_path(path: Any) -> str:
    """Normalize a workspace-relative path, refusing anything that could escape it.

    Args:
        path: The path as the case or version gave it.

    Returns:
        The normalized relative path.

    Raises:
        ServiceError: When the path is empty, absolute, or climbs out of the workspace.
    """
    if not isinstance(path, str) or not path.strip():
        raise ServiceError("file paths must be non-empty strings")
    normalized = posixpath.normpath(path.strip())
    if normalized.startswith(("/", "../")) or normalized in (".", ".."):
        raise ServiceError(f"file path '{path}' must stay inside the workspace")
    return normalized


def candidate_files(candidate: Candidate, instructions_file: str) -> dict[str, str]:
    """Map a version onto the files it becomes in the sandbox.

    Args:
        candidate: Text (the harness's instruction file) or named parts (one file each).
        instructions_file: Where the harness reads its instructions.

    Returns:
        Relative path → content.
    """
    if isinstance(candidate, str):
        return {instructions_file: candidate}
    return {safe_relative_path(path): text for path, text in candidate.items()}

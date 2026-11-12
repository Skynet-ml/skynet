"""Run one completed-run interaction inside the protected guest."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..service_gateway.optimization.blackbox.repo_serving import (
    INTERACTION_KIND as REPO_SERVE_KIND,
)
from ..service_gateway.optimization.blackbox.repo_serving import (
    run_repo_serve,
)


def run_interaction(
    payload: dict[str, Any], descriptor: dict[str, Any], emit: Callable[[dict[str, Any]], None]
) -> dict[str, Any]:
    """Dispatch one supported completed-run interaction in the guest.

    Args:
        payload: Credential-free interaction payload with scoped parent routes.
        descriptor: Validated interaction operation.
        emit: Parent event transport for streaming output.

    Returns:
        Interaction result returned to the signed-in caller.

    Raises:
        ValueError: When the requested interaction kind is unsupported.
    """
    if descriptor.get("kind") == REPO_SERVE_KIND:
        return run_repo_serve(payload, descriptor)
    raise ValueError("Unsupported protected interaction kind.")

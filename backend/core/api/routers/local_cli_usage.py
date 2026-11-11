"""Show the Mac app how much of the local CLI subscriptions Skynet used. [INTERNAL]

``GET /local/cli-usage`` exists only in local mode. Hosted deployments answer
404 and leave it out of the OpenAPI schema. The read never changes credits,
balances or wallet numbers. Each CLI entry also carries the subscription's
used percent from :func:`read_cli_quotas`.

``POST /local/cli-usage/codex/reset`` uses one of the user's own free Codex
limit resets when the user asks for it. It changes nothing in Skynet.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from ...config import settings
from ...local.cli_quota import read_cli_quotas, use_codex_reset
from ...local.cli_usage import local_cli_usage
from ..auth import AuthenticatedUser, get_authenticated_user
from .local_runtime import require_local_mode

AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]


def create_local_cli_usage_router(*, job_store: Any) -> APIRouter:
    """Build the local CLI usage router.

    Args:
        job_store: Job store whose ORM engine holds the usage evidence.

    Returns:
        A FastAPI ``APIRouter`` exposing ``GET /local/cli-usage`` and
        ``POST /local/cli-usage/codex/reset``.
    """
    router = APIRouter()

    @router.get("/local/cli-usage", include_in_schema=False, dependencies=[Depends(require_local_mode)])
    def cli_usage(user: AuthenticatedUserDep) -> dict[str, Any]:
        """Report each CLI's subscription percent, and its calls and tokens from Skynet runs.

        Args:
            user: The signed-in local user.

        Returns:
            The document from :func:`local_cli_usage`, with a ``quota`` on each CLI.
        """
        report = local_cli_usage(job_store.engine, settings, user.username)
        quotas = read_cli_quotas(settings)
        for entry in report["clis"]:
            entry["quota"] = quotas[entry["cli"]]
        return report

    @router.post("/local/cli-usage/codex/reset", include_in_schema=False, dependencies=[Depends(require_local_mode)])
    def codex_reset(user: AuthenticatedUserDep) -> dict[str, str]:
        """Use one free Codex limit reset.

        Args:
            user: The signed-in local user.

        Returns:
            ``{outcome}`` from :func:`use_codex_reset`.
        """
        del user
        return {"outcome": use_codex_reset(settings)}

    return router

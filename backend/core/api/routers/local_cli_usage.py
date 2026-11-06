"""Show the Mac app how much of the local CLI subscriptions Skynet used. [INTERNAL]

``GET /local/cli-usage`` exists only in local mode. Hosted deployments answer
404 and leave it out of the OpenAPI schema. The route only reads; it never
changes credits, balances or wallet numbers.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from ...config import settings
from ...local.cli_usage import local_cli_usage
from ..auth import AuthenticatedUser, get_authenticated_user
from .local_runtime import require_local_mode

AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]


def create_local_cli_usage_router(*, job_store: Any) -> APIRouter:
    """Build the local CLI usage router.

    Args:
        job_store: Job store whose ORM engine holds the usage evidence.

    Returns:
        A FastAPI ``APIRouter`` exposing ``GET /local/cli-usage``.
    """
    router = APIRouter()

    @router.get("/local/cli-usage", include_in_schema=False, dependencies=[Depends(require_local_mode)])
    def cli_usage(user: AuthenticatedUserDep) -> dict[str, Any]:
        """Report each CLI's calls and tokens from the user's Skynet runs.

        Args:
            user: The signed-in local user.

        Returns:
            The document from :func:`local_cli_usage`.
        """
        return local_cli_usage(job_store.engine, settings, user.username)

    return router

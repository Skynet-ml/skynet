"""Proxy the Mac app's web calls to hosted with the linked account (SKY-223). [INTERNAL]

``/local/hosted/<path>`` exists only in local mode. Hosted deployments answer
404 and leave it out of the OpenAPI schema. The renderer never sees the
desktop token; :mod:`core.local.hosted_proxy` adds it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Any

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from ...local import hosted_proxy
from ..auth import AuthenticatedUser, get_authenticated_user
from .local_runtime import require_local_mode

AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]
_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]
# A live event stream holds one thread while it waits for the next event. Its
# own limiter keeps open streams from using up the default pool that every
# sync route of the local API shares.
_STREAM_THREADS = anyio.CapacityLimiter(64)


async def _relay(answer: hosted_proxy.HostedAnswer) -> AsyncIterator[bytes]:
    """Yield the hosted body, reading each chunk in a stream thread.

    Args:
        answer: The open hosted answer.

    Yields:
        The body bytes.
    """
    chunks = answer.chunks()
    try:
        while True:
            # A renderer that goes away cancels the wait at once. The abandoned
            # read keeps its thread until the read timeout ends it; close()
            # does not wake a blocked read. The stream limiter caps how many
            # such threads can wait.
            chunk = await anyio.to_thread.run_sync(next, chunks, None, abandon_on_cancel=True, limiter=_STREAM_THREADS)
            if chunk is None:
                return
            yield chunk
    finally:
        answer.close()


def create_local_hosted_proxy_router(*, job_store: Any) -> APIRouter:
    """Build the local hosted proxy router.

    Args:
        job_store: Storage backend whose ``engine`` holds the link.

    Returns:
        A FastAPI ``APIRouter`` with ``/local/hosted/{path}``.
    """
    router = APIRouter()
    engine = job_store.engine

    @router.api_route(
        "/local/hosted/{path:path}",
        methods=_METHODS,
        include_in_schema=False,
        dependencies=[Depends(require_local_mode)],
    )
    async def proxy(path: str, request: Request, user: AuthenticatedUserDep) -> StreamingResponse:
        """Send the call to hosted and stream the answer back.

        Args:
            path: The hosted path without the leading slash.
            request: The renderer's request.
            user: The signed-in local user.

        Returns:
            The hosted status, safe headers and body.

        Raises:
            HTTPException: 404 for a path that would leave the hosted host.
        """
        if not hosted_proxy.safe_path(path):
            raise HTTPException(status_code=404)
        body = await request.body()
        answer = await run_in_threadpool(
            hosted_proxy.forward,
            engine,
            user.username,
            request.method,
            path,
            request.url.query,
            {name.lower(): value for name, value in request.headers.items()},
            body,
        )
        # The background close covers a stream that never starts; close is idempotent.
        return StreamingResponse(
            _relay(answer),
            status_code=answer.status,
            headers=answer.headers,
            background=BackgroundTask(answer.close),
        )

    return router

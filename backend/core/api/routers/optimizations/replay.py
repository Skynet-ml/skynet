"""Read-only replay of a run's agent step log (SKY-726). [INTERNAL]

Internal (frontend-only, hidden from public docs):
- ``GET /optimizations/{id}/steps`` — one page of one agent's steps, oldest first (owner only).
"""

from __future__ import annotations

import functools
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from ....storage.base import ObjectStore
from ....storage.object_store import pack_store
from ....storage.step_log_reader import read_step_page
from ....storage.step_log_store import AGENT_ID_MAX, SEQ_MAX
from ...auth import AuthenticatedUser, get_authenticated_user
from .detail import _load_job_for_owner

AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]


class ReplayAgent(BaseModel):
    """One agent of a run, with the number of its flushed steps."""

    agent_id: str
    step_count: int


class ReplayStep(BaseModel):
    """One flushed agent step: the command, its output and tree, or a model response; long text is cut."""

    step_id: str
    seq: int
    kind: str
    tree_id: str | None
    created_at: datetime
    command: str | None
    output: str | None
    response: str | None
    truncated: bool


class ReplayStepPage(BaseModel):
    """One page of one agent's steps, oldest first, with the cursor of the next page."""

    agents: list[ReplayAgent]
    agent_id: str | None
    steps: list[ReplayStep]
    has_more: bool
    next_after_seq: int


@functools.cache
def _pack_store() -> ObjectStore | None:
    """Return the pack store of this pod, built once.

    Returns:
        The pack store, or ``None`` when this build has none and no step body can be read.
    """
    return pack_store()


def register_replay_routes(router: APIRouter, *, job_store) -> None:
    """Attach the replay route to ``router``.

    Args:
        router: The optimizations router being assembled.
        job_store: Job store whose ``engine`` and ``step_log_shards`` hold the step log.
    """

    @router.get(
        "/optimizations/{optimization_id}/steps",
        response_model=ReplayStepPage,
        summary="One page of one agent's steps, oldest first",
        tags=["agent"],
    )
    def list_steps(
        optimization_id: str,
        current_user: AuthenticatedUserDep,
        agent_id: str | None = Query(None, min_length=1, max_length=AGENT_ID_MAX),
        after_seq: int = Query(-1, ge=-1, le=SEQ_MAX),
        limit: int = Query(20, ge=1, le=50),
    ) -> ReplayStepPage:
        """Return one page of a run's agent steps after the ``after_seq`` cursor.

        Args:
            optimization_id: The run.
            current_user: Authenticated caller resolved from the bearer token.
            agent_id: The agent to page; none picks the first agent by id.
            after_seq: The last step ``seq`` the caller holds; ``-1`` for the start.
            limit: The most steps to return in this page.

        Returns:
            The run's agents, one page of steps, and the next cursor.

        Raises:
            DomainError: 404 when the run is unknown or the caller is not its owner.
        """
        _load_job_for_owner(job_store, optimization_id, current_user)
        page = read_step_page(
            job_store.engine,
            job_store.step_log_shards,
            _pack_store(),
            optimization_id,
            agent_id=agent_id,
            after_seq=after_seq,
            limit=limit,
        )
        return ReplayStepPage.model_validate(page)

"""Authenticated agent engine preference endpoints. [INTERNAL]"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...config import settings
from ...storage.models import AgentEnginePreferenceModel
from ..agent_engine import (
    OPENROUTER_ENGINE,
    AgentEngine,
    effective_agent_engine,
    openrouter_available,
    stored_agent_engine,
)
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError

AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]

_ENGINE_DESCRIPTION = (
    "Which engine runs agent turns that pick no model: 'openrouter' (managed models), or a "
    "subscription CLI on this machine: 'codex' (Codex CLI) or 'claude' (Claude Code CLI). "
    "The CLIs run only in the Mac app. OpenRouter uses a verified saved key when present, "
    "otherwise the signed-in Skynet account."
)


class AgentEnginePreference(BaseModel):
    engine: AgentEngine | None = Field(
        description=_ENGINE_DESCRIPTION
        + " Null when no CLI, verified OpenRouter key, or signed-in Skynet account is available."
    )
    stored: bool = Field(
        description="True when the caller saved this engine in the Mac app. False when the engine is the"
        " automatic pick (the first ready subscription CLI, else OpenRouter), and always"
        " false in hosted mode, which stores no engine."
    )


class AgentEngineUpdate(BaseModel):
    engine: AgentEngine = Field(description=_ENGINE_DESCRIPTION)


def create_agent_engine_router(*, job_store) -> APIRouter:
    """Build the caller-scoped agent engine preference router.

    Args:
        job_store: Job-store instance whose ORM engine persists preferences.

    Returns:
        A router exposing get and put operations for the authenticated caller.
    """
    router = APIRouter()

    @router.get(
        "/account/agent-engine",
        response_model=AgentEnginePreference,
        summary="Read which engine runs the caller's agent turns",
    )
    def get_agent_engine(user: AuthenticatedUserDep) -> AgentEnginePreference:
        """Return the caller's effective engine.

        Hosted mode always answers ``openrouter``. The Mac app answers the
        stored engine, else the first ready subscription CLI, else
        ``openrouter`` when the caller has a verified key or signed-in account.

        Args:
            user: Authenticated caller whose setting is requested.

        Returns:
            The effective agent engine, and whether the caller saved it.
        """
        return AgentEnginePreference(
            engine=effective_agent_engine(job_store, user.username),
            stored=stored_agent_engine(job_store, user.username) is not None,
        )

    @router.put(
        "/account/agent-engine",
        response_model=AgentEnginePreference,
        summary="Set which engine runs the caller's agent turns",
    )
    def put_agent_engine(body: AgentEngineUpdate, user: AuthenticatedUserDep) -> AgentEnginePreference:
        """Persist the caller's engine; it applies to agent turns started afterwards.

        Args:
            body: The new engine.
            user: Authenticated caller whose setting is updated.

        Returns:
            The stored engine.

        Raises:
            DomainError: 422 ``models.subscription_local_only`` for a CLI engine
                in hosted mode; 422 ``models.managed_unavailable_local`` for
                ``openrouter`` in the Mac app without a verified key or account link.
        """
        if not settings.local_mode:
            if body.engine != OPENROUTER_ENGINE:
                raise DomainError("models.subscription_local_only", status=422)
            # Hosted turns always run on OpenRouter, so there is nothing to store.
            return AgentEnginePreference(engine=OPENROUTER_ENGINE, stored=False)
        if body.engine == OPENROUTER_ENGINE and not openrouter_available(job_store, user.username):
            raise DomainError("models.managed_unavailable_local", status=422)
        with Session(job_store.engine) as session:
            row = session.get(AgentEnginePreferenceModel, user.username)
            if row is None:
                row = AgentEnginePreferenceModel(username=user.username)
                session.add(row)
            row.engine = body.engine
            row.updated_at = datetime.now(UTC)
            session.commit()
        return AgentEnginePreference(engine=body.engine, stored=True)

    return router

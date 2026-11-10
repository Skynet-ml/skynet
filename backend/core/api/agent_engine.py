"""The per-user agent engine: which engine runs an agent turn that picks no model.

Hosted mode always runs on OpenRouter, so it never reads the stored setting.
Local mode runs agent turns on the engine the user chose: a subscription CLI,
or OpenRouter with the user's verified key or signed-in Skynet account. With
no choice, it runs them on the first ready CLI, then an available OpenRouter route. A CLI engine never falls back to OpenRouter. In local mode an
optimization proposer with no CLI pick follows the same engine CLI.
"""

from __future__ import annotations

from typing import Any, Literal, get_args

from sqlalchemy.orm import Session

from ..billing.byok_vault import ProviderKeyVault
from ..config import settings
from ..local.cli_catalog import CLI_MODELS, CliProvider, parse_subscription_model_id
from ..local.cli_probe import CliStatus, probe_clis
from ..local.hosted_link import link_status
from ..models.blackbox import BLACKBOX_HARNESS_CUSTOM
from ..storage.models import AgentEnginePreferenceModel
from .model_catalog import is_removed_model
from .model_router import _RETIRED_AUTO_ID

# Droid has no flag that skips the user's own config, hooks and MCP servers,
# so it cannot run like the hosted setup and is not an engine option. A saved
# "droid" row is unknown here and falls back to the default engine.
AgentEngine = Literal["openrouter", "codex", "claude"]
AGENT_ENGINES: tuple[AgentEngine, ...] = get_args(AgentEngine)
OPENROUTER_ENGINE: AgentEngine = "openrouter"


def _ready(status: CliStatus) -> bool:
    """Return whether a CLI can take an agent turn now.

    Args:
        status: The CLI's probe result.

    Returns:
        ``True`` when it is installed, new enough, not signed out and not capped.
    """
    return (
        status.path is not None
        and status.version_ok
        and status.logged_in is not False
        and status.rate_limited_until is None
    )


def default_agent_engine() -> AgentEngine | None:
    """Return the engine a caller runs on before choosing one.

    Returns:
        ``openrouter`` in hosted mode; in local mode the first ready engine
        CLI, or ``None`` when none is ready.
    """
    if not settings.local_mode:
        return OPENROUTER_ENGINE
    return next(
        (status.id for status in probe_clis(settings) if status.id in AGENT_ENGINES and _ready(status)),
        None,
    )


def stored_agent_engine(job_store: object | None, username: str) -> AgentEngine | None:
    """Read the caller's stored engine in local mode.

    Args:
        job_store: Job store whose ORM engine holds the setting, or ``None``.
        username: The caller.

    Returns:
        The stored engine, or ``None`` in hosted mode, with no store, with no
        row, or for a value this build does not know.
    """
    if not settings.local_mode or job_store is None:
        return None
    engine = getattr(job_store, "engine", None)
    if engine is None:
        return None
    with Session(engine) as session:
        row = session.get(AgentEnginePreferenceModel, username)
    value = row.engine if row is not None else None
    return value if value in AGENT_ENGINES else None  # type: ignore[return-value]


def effective_agent_engine(job_store: object | None, username: str) -> AgentEngine | None:
    """Return the engine the caller's agent turns run on.

    Args:
        job_store: Job store whose ORM engine holds the setting, or ``None``.
        username: The caller.

    Returns:
        The stored engine, else the default (see :func:`default_agent_engine`).
        In local mode with neither, ``openrouter`` when the caller has a
        verified OpenRouter key or a signed-in Skynet account; else None.
    """
    chosen = stored_agent_engine(job_store, username) or default_agent_engine()
    if chosen is not None or not settings.local_mode:
        return chosen
    return OPENROUTER_ENGINE if openrouter_available(job_store, username) else None


def _has_verified_key(job_store: object | None, username: str) -> bool:
    """Return whether the caller has a saved, verified OpenRouter key.

    Args:
        job_store: Job store whose ORM engine holds the key vault, or ``None``.
        username: The caller.

    Returns:
        ``True`` when the vault holds a verified OpenRouter key for the caller.
    """
    engine = getattr(job_store, "engine", None)
    return engine is not None and ProviderKeyVault(engine=engine).has_verified_connection(username, OPENROUTER_ENGINE)


def openrouter_available(job_store: object | None, username: str) -> bool:
    """Check whether OpenRouter can use the saved key or the signed-in Skynet account.

    Args:
        job_store: Local store containing keys and the account link.
        username: Authenticated local user.

    Returns:
        Whether either authorized inference route exists.
    """
    if _has_verified_key(job_store, username):
        return True
    engine = getattr(job_store, "engine", None)
    return engine is not None and link_status(engine, username) is not None


def _turn_engine(job_store: object | None, username: str) -> AgentEngine | None:
    """Return the engine that runs a local agent turn or proposer with no pick.

    Args:
        job_store: Job store whose ORM engine holds the setting, or ``None``.
        username: The caller.

    Returns:
        The effective engine, except that a stored OpenRouter engine with no
        saved key or account link gives the default engine.
    """
    engine = effective_agent_engine(job_store, username)
    # A lost key does not change the engine while the signed-in account can serve it.
    if engine == OPENROUTER_ENGINE and not openrouter_available(job_store, username):
        return default_agent_engine()
    return engine


def engine_agent_model(job_store: object | None, username: str, model: str | None) -> str | None:
    """Fill an agent turn's empty model pick from the caller's engine.

    Args:
        job_store: Job store whose ORM engine holds the setting, or ``None``.
        username: The caller.
        model: The request's raw ``model`` value.

    Returns:
        ``model`` unchanged when it names a model, in hosted mode, or when the
        turn engine (see :func:`_turn_engine`) is OpenRouter or unknown (in
        local mode the model router then
        runs on the saved key or signed-in account, or refuses it).
        Otherwise ``subscription/<cli>/<slug>`` on the engine
        CLI's first catalog model.
    """
    name = str(model or "").strip()
    if not settings.local_mode or parse_subscription_model_id(name) is not None:
        return model
    # The removed-model pattern is unanchored and also matches valid
    # subscription slugs, so it runs only after the subscription check.
    if name and name != _RETIRED_AUTO_ID and not is_removed_model(name):
        return model
    engine = _turn_engine(job_store, username)
    if engine is None or engine == OPENROUTER_ENGINE:
        return model
    cli: CliProvider = engine
    return f"subscription/{cli}/{next(iter(CLI_MODELS[cli]))}"


def fill_proposer_host_cli(job_store: object | None, username: str, payload: Any) -> None:
    """Run a local proposer with no CLI pick on the caller's engine CLI.

    Args:
        job_store: Job store whose ORM engine holds the setting, or ``None``.
        username: The caller.
        payload: A black-box (``proposer``) or unified (``optimizer.proposer``)
            request, mutated in place.
    """
    if not settings.local_mode:
        return
    proposer = getattr(payload, "proposer", None) or getattr(getattr(payload, "optimizer", None), "proposer", None)
    # An explicit pick wins, including an explicit null for the sandbox
    # proposer. A custom harness is the user's own command, which a host CLI
    # would replace.
    if proposer is None or "host_cli" in proposer.model_fields_set or proposer.harness == BLACKBOX_HARNESS_CUSTOM:
        return
    engine = _turn_engine(job_store, username)
    if engine is not None and engine != OPENROUTER_ENGINE:
        proposer.host_cli = engine

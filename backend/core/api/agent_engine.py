"""The agent engine: which engine runs an agent turn that picks no model.

Hosted mode always runs on OpenRouter. Local mode runs agent turns on the
first ready subscription CLI. With no ready CLI, the turn keeps its empty
pick, and the model router runs it on an available OpenRouter route or
refuses it. A CLI engine never falls back to OpenRouter. In local mode an
optimization proposer with no CLI pick follows the same engine CLI.
"""

from __future__ import annotations

from typing import Any, Literal, get_args

from ..config import settings
from ..local.cli_catalog import CLI_MODELS, CliProvider, parse_subscription_model_id
from ..local.cli_probe import CliStatus, probe_clis
from ..models.blackbox import BLACKBOX_HARNESS_CUSTOM
from .model_catalog import is_removed_model
from .model_router import _RETIRED_AUTO_ID

# Droid has no flag that skips the user's own config, hooks and MCP servers,
# so it cannot run like the hosted setup and is never an engine.
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
    """Return the engine that runs agent turns.

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


def effective_agent_engine(job_store: object | None, username: str) -> AgentEngine | None:
    """Return the engine the caller's agent turns run on.

    Args:
        job_store: Unused; old stored engine rows no longer apply.
        username: Unused; the engine is the same for every caller.

    Returns:
        The automatic pick (see :func:`default_agent_engine`).
    """
    return default_agent_engine()


def _turn_engine(job_store: object | None, username: str) -> AgentEngine | None:
    """Return the engine that runs a local agent turn or proposer with no pick.

    Args:
        job_store: Passed to :func:`effective_agent_engine`.
        username: The caller.

    Returns:
        The effective engine.
    """
    return effective_agent_engine(job_store, username)


def engine_agent_model(job_store: object | None, username: str, model: str | None) -> str | None:
    """Fill an agent turn's empty model pick from the caller's engine.

    Args:
        job_store: Passed to :func:`effective_agent_engine`.
        username: The caller.
        model: The request's raw ``model`` value.

    Returns:
        ``model`` unchanged when it names a model, in hosted mode, or when no
        engine CLI is ready (in local mode the model router then runs it on
        the saved key or signed-in account, or refuses it). Otherwise
        ``subscription/<cli>/<slug>`` on the engine CLI's first catalog model.
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
        job_store: Passed to :func:`effective_agent_engine`.
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

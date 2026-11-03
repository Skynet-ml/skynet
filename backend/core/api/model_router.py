"""Model routing for the agent composers.

A composer that names no model runs the catalog's best-value default (see
:func:`default_model_id`), which moves with OpenRouter's live prices and
benchmarks. When the catalog flags no default (for example a gateway with no live
prices), the composer degrades to the engine's configured server default.
"""

from __future__ import annotations

import logging
import re
from typing import get_args

from ..billing.byok_bridge import resolve_byok_model_config
from ..billing.byok_vault import ProviderKeyVault
from ..billing.cli_dispatch import CLI_FAST_KEY
from ..config import settings
from ..constants import TOKEN_SOURCE_BYOK, TOKEN_SOURCE_MANAGED
from ..local.cli_catalog import CLI_MODELS, RetiredEffort, parse_subscription_model_id
from ..models import ModelConfig
from .errors import DomainError
from .interview_model import interview_model_id
from .model_catalog import (
    REASONING_EFFORTS,
    agent_model_id,
    get_catalog_cached,
    is_removed_model,
    require_known_model,
    unpicked_effort,
)

logger = logging.getLogger(__name__)

# The one BYOK provider the Mac app's composers run on: catalog ids are
# OpenRouter ids, so an OpenRouter key serves every catalog model.
_LOCAL_BYOK_PROVIDER = "openrouter"

# The retired "Auto · Intelligent" menu entry; saved conversations and
# browser prefs may still send it, so it runs the default like no pick at all.
_RETIRED_AUTO_ID = "auto:intelligent"

# An OpenRouter model id: openrouter/<vendor>/<model>. A saved-key pick is
# checked only for this shape, because the user's key pays for the turn and
# the Mac app's curated catalog is empty (it has no platform OpenRouter key).
_OPENROUTER_MODEL_ID = re.compile(r"openrouter/[^/\s]+/[^/\s]+")


def default_model_id() -> str | None:
    """Return the catalog's best-value default model.

    Recomputed with every catalog refresh from OpenRouter's live prices and
    benchmarks, so the default moves to a better deal without a deploy.

    Returns:
        The default model's catalog id, or ``None`` when the catalog is
        unavailable or flags no default.
    """
    try:
        catalog = get_catalog_cached()
    except Exception:
        logger.warning("model catalog unavailable; no default model", exc_info=True)
        return None
    return next((m.value for m in catalog.models if m.is_default), None)


def route_agent_model(model: str | None) -> ModelConfig:
    """Build the generalist agent's model config from the composer's pick.

    Args:
        model: Raw ``model`` value from the request; empty runs the default.

    Returns:
        A :class:`ModelConfig` on the picked model, the catalog default, or
        the configured server default when the catalog flags none.

    Raises:
        DomainError: 422 when an explicit id is not a catalog model, or in
            local mode for any pick that is not a subscription model.
    """
    name = route_menu_model(model)
    subscription = parse_subscription_model_id(name or "")
    if subscription is not None:
        cli, slug = subscription
        return ModelConfig(name=slug, token_source="subscription", cli_provider=cli)
    return ModelConfig(name=name or agent_model_id(settings.generalist_agent_model))


def cli_fast_config(model: str | None, fast_mode: bool) -> ModelConfig | None:
    """Build the config that runs a local CLI turn in the CLI's fast mode.

    Args:
        model: The id the turn runs on, after routing.
        fast_mode: Whether the composer asked for fast mode.

    Returns:
        A subscription config that carries :data:`CLI_FAST_KEY`, or ``None``
        when fast mode is off or ``model`` is not a local CLI model. The CLI
        dispatcher drops the flag for a model that does not take fast mode.
    """
    subscription = parse_subscription_model_id(model or "") if fast_mode and settings.local_mode else None
    if subscription is None:
        return None
    cli, slug = subscription
    return ModelConfig(
        name=slug, token_source="subscription", cli_provider=cli, extra={"extra_body": {CLI_FAST_KEY: True}}
    )


def effective_reasoning_effort(model: str | None, requested: str | None) -> str | None:
    """Resolve the effort to send for a turn, on the model's own effort ladder.

    The menu labels "Default" with the model's catalog default level, but an
    unset effort sends OpenRouter no ``reasoning`` param, and opt-in thinkers
    (Claude, Gemini, DeepSeek) then return no reasoning at all. Sending the
    catalog default makes the label true and the thinking visible.

    An explicit pick can come from a generic ladder (the menu's fallback
    when the catalog is unavailable), and providers reject or silently remap
    levels they don't list, so it is snapped to the nearest level the model
    declares.

    Args:
        model: The model id the turn runs on, or ``None`` when the
            engine's configured default runs.
        requested: Effort the user picked explicitly, or ``None``.

    Returns:
        The requested effort, snapped onto the model's ladder when the
        catalog knows one; otherwise :func:`unpicked_effort` of the model
        (its default effort, or low for an opt-in thinker).
    """
    # A retired level (``ultra`` / ``ultracode``) from an old pick runs at the
    # top level: the CLI dispatcher maps it for a local CLI model, and "max"
    # snaps onto any other model's ladder below.
    if requested in get_args(RetiredEffort) and parse_subscription_model_id(model or "") is None:
        requested = "max"
    if not model:
        return requested
    try:
        models = get_catalog_cached().models
    except Exception:
        return requested
    entry = next((m for m in models if m.value == model), None)
    if entry is None:
        return requested
    if requested:
        return _nearest_effort(requested, entry.reasoning_efforts)
    return unpicked_effort(entry)


def _nearest_effort(requested: str, ladder: list[str] | None) -> str:
    """Snap an effort level onto a model's declared ladder.

    Args:
        requested: The level asked for.
        ladder: The model's accepted levels, weakest first; empty or ``None``
            when the model declares none.

    Returns:
        ``requested`` when the ladder lists it, is empty, or ``requested`` is
        an unknown level; otherwise the closest listed level, the stronger
        one on a tie.
    """
    if not ladder or requested in ladder or requested not in REASONING_EFFORTS:
        return requested
    rank = REASONING_EFFORTS.index(requested)
    known = [e for e in ladder if e in REASONING_EFFORTS]
    if not known:
        return requested
    return min(known, key=lambda e: (abs(REASONING_EFFORTS.index(e) - rank), -REASONING_EFFORTS.index(e)))


def route_menu_model(model: str | None) -> str | None:
    """Resolve the composer menu's model field to the id an engine runs on.

    Args:
        model: Raw ``model`` value from the request.

    Returns:
        The validated catalog id, the catalog default when none was picked
        (or a retired / withdrawn id was sent), or ``None`` when the catalog flags no default (the engine's
        configured default runs). In local mode a ``subscription/<cli>/<slug>``
        id is returned as is.

    Raises:
        DomainError: 422 when an explicit id is not a catalog model, or names
            a subscription model its CLI cannot run; 422
            ``models.managed_unavailable_local`` in local mode for any pick
            that is not a subscription model.
    """
    name = str(model or "").strip()
    subscription = parse_subscription_model_id(name) if settings.local_mode else None
    if subscription is not None:
        cli, slug = subscription
        if slug not in CLI_MODELS[cli]:
            raise DomainError("billing.subscription_unmapped_model", status=422, provider=cli)
        return name
    _refuse_local_managed()
    # Saved prefs and stored conversations may still name a withdrawn model;
    # run the default instead of failing every turn with a 422.
    if name and name != _RETIRED_AUTO_ID and not is_removed_model(name):
        require_known_model(name)
        return name
    return default_model_id()


def route_interview_model(model: str | None) -> str | None:
    """Resolve an agent interview's model field, defaulting to the interview pick.

    Args:
        model: Raw ``model`` value from the request.

    Returns:
        The validated catalog id when one was picked, else the measured
        interview model (see :mod:`core.api.interview_model`).

    Raises:
        DomainError: 422 when an explicit id is not a catalog model; 422
            ``models.managed_unavailable_local`` in local mode for any pick
            that is not a subscription model.
    """
    name = str(model or "").strip()
    # The removed-model pattern also matches valid subscription slugs such as
    # gpt-5.6-terra, so a subscription pick skips it.
    if settings.local_mode and parse_subscription_model_id(name) is not None:
        return route_menu_model(name)
    if name and name != _RETIRED_AUTO_ID and not is_removed_model(name):
        return route_menu_model(name)
    _refuse_local_managed()
    return interview_model_id()


def route_local_byok_model(
    model: str | None,
    *,
    job_store: object | None,
    username: str,
    interview: bool = False,
) -> ModelConfig | None:
    """Route a Mac app composer pick onto the caller's saved OpenRouter key.

    The Mac app has no managed key. A user who saved and verified an
    OpenRouter key runs any OpenRouter model id, and the catalog default, on
    that key.
    It never falls back to a managed key.

    Args:
        model: The pick after the agent engine filled an empty one.
        job_store: Job store whose engine backs the encrypted key vault.
        username: The caller, who owns the key.
        interview: Whether an empty pick runs the interview default instead
            of the composer default.

    Returns:
        A BYOK config with the decrypted key in memory only. ``None`` outside
        local mode, for a subscription pick, with no store, or with no verified
        OpenRouter key; the normal routes then apply.

    Raises:
        DomainError: 422 ``models.unknown_model`` when an explicit id is not
            an OpenRouter model id; 400 ``billing.byok_missing_connection``
            when the key is deleted after the verified-key check; 400
            ``billing.byok_model_not_served`` when the saved connection cannot
            route the model; 409 ``billing.byok_key_undecryptable`` when the
            saved key cannot be decrypted; 503 ``billing.byok_not_configured``
            when the vault key is unconfigured.
    """
    if not settings.local_mode:
        return None
    name = str(model or "").strip()
    if parse_subscription_model_id(name) is not None:
        return None
    engine = getattr(job_store, "engine", None)
    if engine is None or not username:
        return None
    vault = ProviderKeyVault(engine=engine)
    if not vault.has_verified_connection(username, _LOCAL_BYOK_PROVIDER):
        return None
    if name and name != _RETIRED_AUTO_ID and not is_removed_model(name):
        if not _OPENROUTER_MODEL_ID.fullmatch(name):
            raise DomainError("models.unknown_model", status=422)
    else:
        name = (interview_model_id() if interview else default_model_id()) or agent_model_id()
    config = ModelConfig(name=name, token_source=TOKEN_SOURCE_BYOK, byok_provider=_LOCAL_BYOK_PROVIDER)
    try:
        return resolve_byok_model_config(config, username=username, vault=vault)
    except ValueError as exc:
        # The key was deleted or failed a new check after the check above.
        raise DomainError("billing.byok_missing_connection", status=400, provider=_LOCAL_BYOK_PROVIDER) from exc


def metering_token_source(model_config: ModelConfig | None) -> str:
    """Return the billing source a composer turn is metered under.

    Args:
        model_config: The turn's resolved config, or ``None`` off the BYOK route.

    Returns:
        ``byok`` for a BYOK config; else ``managed``, which is how every other
        composer turn (subscription turns included) was metered before.
    """
    if model_config is not None and model_config.token_source == TOKEN_SOURCE_BYOK:
        return TOKEN_SOURCE_BYOK
    return TOKEN_SOURCE_MANAGED


def _refuse_local_managed() -> None:
    """Refuse a managed model in local mode.

    Catalog ids and the catalog, interview and server defaults all run on
    managed OpenRouter, and the Mac app has no managed key, so a turn sent
    there fails with a 401. A saved OpenRouter key takes the BYOK route
    instead (see :func:`route_local_byok_model`).

    Raises:
        DomainError: 422 ``models.managed_unavailable_local`` in local mode.
    """
    if settings.local_mode:
        raise DomainError("models.managed_unavailable_local", status=422)

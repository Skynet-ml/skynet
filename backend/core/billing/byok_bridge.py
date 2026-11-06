"""Bridge that hands a BYOK user's stored provider key to the run path.

The run path turns a model string into a ``dspy.LM`` in
``service_gateway.language_models.build_language_model``, which authenticates
with whatever ``api_key`` rides in the ModelConfig's ``extra``. A managed run
gets that key from process env vars; a BYOK run must get it from the user's
encrypted vault. This module is the single seam that injects it: the parent
worker calls :func:`inject_byok_connections` on the raw payload dict just before
it spawns the run subprocess, so the resolved key travels into the child — and
the grid threads it fans out — as ordinary pickled data. A contextvar could not
cross either the process or the thread-pool boundary.

Secrets are injected into the in-memory payload only, after it is loaded from
storage and before the subprocess starts, so a plaintext key is never persisted:
the stored job overview keeps the ``strip_api_key`` invariant.
"""

from __future__ import annotations

from typing import Any

from ..api.errors import DomainError
from ..constants import TOKEN_SOURCE_BYOK
from ..models.common import ModelConfig
from .byok_upstreams import ProviderUpstream, byok_model_prefix, resolve_upstream
from .byok_vault import ProviderKeyVault, byok_provider_for_litellm, safe_connection_params
from .operation_pricing import UnpricedOperationError

# Payload keys holding ModelConfig blocks. Runs persist their configs under the
# field *aliases* (``model_settings`` → ``"model_config"``); grids use the plain
# list field names. The Anything task role reaches the parent model gateway even
# though it never reaches ``build_language_model`` directly.
_RUN_MODEL_KEYS = ("model_config", "task_model_config", "reflection_model_config")
_GRID_MODEL_LIST_KEYS = ("generation_models", "reflection_models", "extra_reflection_model_configs")


def provider_slug_for_model(name: str) -> str | None:
    """Return the provider slug prefixing a LiteLLM model string, or ``None``.

    ``"openai/gpt-4o"`` → ``"openai"``; ``"anthropic/claude-3-5-sonnet"`` →
    ``"anthropic"``. A bare model with no ``provider/`` prefix yields ``None``,
    since there is no provider to resolve a BYOK key for.

    Args:
        name: The model identifier from a ModelConfig.

    Returns:
        The leading provider segment, or ``None`` when the string carries none.
    """
    stripped = (name or "").strip().strip("/")
    if "/" not in stripped:
        return None
    return stripped.split("/", 1)[0] or None


def byok_prefix_routable(prefix: str | None, provider: str, *, api_base: str | None) -> bool:
    """Decide whether a model prefix can be served through a saved connection.

    Native IDs can contain namespaces that resemble other providers. A custom
    ``api_base`` accepts the ``openai/`` prefix the custom catalog emits.

    Args:
        prefix: The model string's LiteLLM provider prefix, or ``None`` for a
            bare id (which carries nothing to contradict the connection).
        provider: The vault slug of the connection that will authenticate it.
        api_base: The connection's custom endpoint, if any.

    Returns:
        ``True`` when the connection can route the model.
    """
    if prefix is None:
        return True
    if byok_provider_for_litellm(prefix) == provider:
        return True
    return bool(api_base) and prefix == "openai"


def byok_upstream_routable(prefix: str | None, provider: str, upstream: ProviderUpstream | None) -> bool:
    """Apply the custom endpoint prefix exception only to custom upstreams.

    Args:
        prefix: Model provider prefix, or None for a bare model.
        provider: Saved connection provider.
        upstream: Resolved provider route, if supported by the protected gateway.

    Returns:
        Whether the connection can route the model.
    """
    return byok_prefix_routable(
        prefix, provider, api_base=upstream.base_url if upstream is not None and upstream.slug == "custom" else None
    )


def _model_config_dicts(payload_dict: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect the ModelConfig sub-dicts a run/grid payload carries.

    Args:
        payload_dict: The raw persisted payload for a run or grid submission.

    Returns:
        Every ModelConfig block present, across the run and grid shapes.
    """
    configs: list[dict[str, Any]] = []
    for key in _RUN_MODEL_KEYS:
        cfg = payload_dict.get(key)
        if isinstance(cfg, dict):
            configs.append(cfg)
    # A black-box scorer's ``llm()`` model lives under ``scorer``.
    scorer = payload_dict.get("scorer")
    if isinstance(scorer, dict) and isinstance(scorer.get("model"), dict):
        configs.append(scorer["model"])
    for key in _GRID_MODEL_LIST_KEYS:
        entries = payload_dict.get(key)
        if isinstance(entries, list):
            configs.extend(cfg for cfg in entries if isinstance(cfg, dict))
    return configs


def payload_uses_token_source(payload_dict: dict[str, Any], source: str, *, default_token_source: str) -> bool:
    """Return whether any payload ModelConfig resolves to ``source``.

    Args:
        payload_dict: Raw run or grid payload.
        source: Source to find (``managed`` or ``byok``).
        default_token_source: Legacy job-level source used when a config omits it.

    Returns:
        True when at least one config uses the requested source.
    """
    return any((cfg.get("token_source") or default_token_source) == source for cfg in _model_config_dicts(payload_dict))


def resolve_byok_model_config(model_config: ModelConfig, *, username: str, vault: ProviderKeyVault) -> ModelConfig:
    """Return a model config with its verified BYOK connection injected.

    Args:
        model_config: Config to resolve; managed configs pass through unchanged.
        username: Account that owns the stored connection.
        vault: Encrypted connection vault.

    Returns:
        A copied config carrying the decrypted runtime key and endpoint.

    Raises:
        ValueError: When a BYOK config has no verified matching connection.
    """
    if model_config.token_source != TOKEN_SOURCE_BYOK:
        return model_config
    provider = (model_config.byok_provider or "").strip()
    if not provider:
        prefix = provider_slug_for_model(model_config.normalized_identifier())
        if prefix is not None:
            provider = byok_provider_for_litellm(prefix)
    if not provider or not vault.has_verified_connection(username, provider):
        raise ValueError(provider)
    payload = {"model_config": model_config.model_dump(mode="json")}
    inject_byok_connections(payload, username=username, vault=vault)
    return ModelConfig.model_validate(payload["model_config"])


def inject_byok_connections(
    payload_dict: dict[str, Any],
    *,
    username: str,
    vault: ProviderKeyVault,
    default_token_source: str = TOKEN_SOURCE_BYOK,
) -> None:
    """Stamp vault connections onto the BYOK ModelConfigs in a payload, in place.

    For each ModelConfig, resolve the provider from its model string and look up
    the account's connection in the vault, then set ``extra['api_key']`` (plus the
    connection's ``api_base`` and extra ``params`` when present).
    ``build_language_model`` already forwards ``extra`` into the ``dspy.LM`` call,
    so the key reaches the provider with no further wiring. A config whose model
    carries no ``provider/`` prefix is left untouched (nothing to resolve).

    Args:
        payload_dict: The raw run/grid payload, mutated in place.
        username: Account the run bills to (the vault owner).
        vault: The provider-key vault to resolve connections from.
        default_token_source: Legacy job-level source for configs without one.

    Raises:
        ValueError: When a model's provider has no saved connection — the run
            cannot authenticate, so it is failed with a clear message rather than
            silently falling back to a platform key.
        DomainError: 400 ``billing.byok_model_not_served`` when the model's
            prefix names a provider the chosen connection cannot route to.
    """
    for cfg in _model_config_dicts(payload_dict):
        if (cfg.get("token_source") or default_token_source) != TOKEN_SOURCE_BYOK:
            continue
        configured_provider = str(cfg.get("byok_provider") or "").strip() or None
        prefix = byok_model_prefix(cfg.get("name", ""), configured_provider)
        if prefix is None and configured_provider is None:
            continue
        # The model id carries a LiteLLM prefix (``gemini``, ``together_ai``);
        # the user saved their key under the vault slug (``google``,
        # ``together``). Bridge the two so the lookup hits.
        provider = configured_provider or byok_provider_for_litellm(prefix or "")
        resolved = vault.resolve_connection(username, provider)
        if resolved is None:
            raise ValueError(
                f"No saved {provider} connection for this account. "
                "Add one in Settings → Providers to run with your own key."
            )
        extra = cfg.get("extra")
        extra = {} if not isinstance(extra, dict) else safe_connection_params(extra)
        try:
            upstream = resolve_upstream(
                {**cfg, "byok_provider": provider, "base_url": resolved.api_base, "extra": extra}
            )
        except UnpricedOperationError as error:
            raise DomainError(error.code or "submission.byok_provider_unsupported", status=400) from error
        prefix = byok_model_prefix(cfg.get("name", ""), configured_provider, custom=upstream.slug == "custom")
        if not byok_upstream_routable(prefix, provider, upstream):
            raise DomainError(
                "billing.byok_model_not_served",
                status=400,
                model=str(cfg.get("name", "")),
                provider=provider,
            )
        cfg["extra"] = extra
        extra.update(safe_connection_params(resolved.params))
        extra["api_key"] = resolved.secret
        if resolved.api_base:
            cfg["base_url"] = resolved.api_base
        else:
            cfg.pop("base_url", None)

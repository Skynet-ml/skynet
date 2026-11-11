"""Discover and select OpenRouter priority endpoints without changing model identity."""

from __future__ import annotations

import re
import time
from functools import lru_cache
from urllib.parse import quote

import httpx

from ..billing.cli_dispatch import CLI_FAST_KEY
from ..models import ModelConfig

_MODEL_ID = re.compile(r"openrouter/[A-Za-z0-9_-]+/[A-Za-z0-9_.:-]+")


def fast_providers(model: str) -> tuple[str, ...]:
    """Return verified priority endpoint tags for an OpenRouter model.

    Args:
        model: The fully qualified model id.

    Returns:
        Priority tags, or an empty tuple for unsupported or unavailable models.
    """
    if not _MODEL_ID.fullmatch(model):
        return ()
    return _fetch_fast_providers(model, int(time.monotonic() // 300))


@lru_cache(maxsize=256)
def _fetch_fast_providers(model: str, window: int) -> tuple[str, ...]:
    """Cache public endpoint metadata for five minutes without invoking a model.

    Args:
        model: A validated OpenRouter model id.
        window: The five-minute cache window.

    Returns:
        Explicit priority endpoint tags; failures disable the control temporarily.
    """
    slug = model.removeprefix("openrouter/")
    try:
        with httpx.Client(timeout=5, follow_redirects=False, trust_env=False) as client:
            response = client.get(f"https://openrouter.ai/api/v1/models/{quote(slug, safe='/')}/endpoints")
            response.raise_for_status()
            data = response.json().get("data")
        if not isinstance(data, dict) or data.get("id") != slug:
            return ()
        return tuple(
            dict.fromkeys(
                endpoint["tag"]
                for endpoint in data.get("endpoints", [])
                if isinstance(endpoint, dict)
                and endpoint.get("service_tier") in {"priority", "fast"}
                and isinstance(endpoint.get("tag"), str)
                and endpoint["tag"]
                and endpoint.get("status", 0) == 0
            )
        )
    except (httpx.HTTPError, ValueError, AttributeError, TypeError):
        return ()


def with_fast_mode(config: ModelConfig, enabled: bool) -> ModelConfig:
    """Apply fast mode while preserving credentials and existing routing restrictions.

    Args:
        config: The resolved managed, BYOK, or local CLI model config.
        enabled: Whether the user enabled fast mode.

    Returns:
        A copied config for supported models, otherwise the original config.
    """
    if not enabled:
        return config
    body = dict(config.extra.get("extra_body") or {})
    if config.token_source == "subscription":
        body[CLI_FAST_KEY] = True
    else:
        tags = fast_providers(config.name)
        if not tags:
            return config
        provider = dict(body.get("provider") or {})
        selected, ignored = set(provider.get("only") or []), set(provider.get("ignore") or [])
        tags = tuple(
            tag
            for tag in tags
            if (not selected or selected.intersection({tag, tag.split("/")[0]}))
            and not ignored.intersection({tag, tag.split("/")[0]})
        )
        if not tags:
            return config
        # Explicit tier tags let the existing billing quote reserve their actual prices.
        provider.update(only=list(tags), allow_fallbacks=False)
        body["provider"] = provider
    return config.model_copy(update={"extra": {**config.extra, "extra_body": body}})

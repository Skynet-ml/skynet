"""Look up a caller's verified OpenRouter BYOK key for routes that call OpenRouter directly.

Dictation and the agentic IDE completions both run on the caller's own
OpenRouter key when the vault holds a verified one, and on the platform key
otherwise. This module holds the one lookup that both routes use.
"""

from __future__ import annotations

import logging
from typing import Any

from ..billing.byok_vault import ProviderKeyVault
from ..billing.protected_credentials import _openrouter_endpoint
from ..config import settings
from .errors import DomainError

logger = logging.getLogger(__name__)

_PROVIDER = "openrouter"


def own_openrouter_key(engine: Any, username: str) -> str | None:
    """Return the caller's verified OpenRouter BYOK secret, or None.

    Args:
        engine: SQLAlchemy engine backing the vault, or ``None``.
        username: The caller.

    Returns:
        The decrypted key when the caller has a verified OpenRouter connection
        that the vault can decrypt and that targets openrouter.ai itself;
        otherwise None, which routes the call to the platform key.
    """
    # Without a vault key no stored secret can be decrypted, so skip the read.
    if engine is None or settings.byok_vault_key is None:
        return None
    try:
        # One read: ``verified_only`` filters in the same query that picks the key.
        connection = ProviderKeyVault(engine=engine).resolve_connection(username, _PROVIDER, verified_only=True)
    except DomainError as err:
        logger.warning("openrouter byok key unusable for %s: %s", username, err.code)
        return None
    # A connection with a custom base holds a credential for another host;
    # sending it to openrouter.ai would leak it.
    if connection is None or not _openrouter_endpoint(connection.api_base):
        return None
    return connection.secret

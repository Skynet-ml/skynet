"""Token prefixes and hashing that both bearer-auth modules share.

A leaf module, so :mod:`core.api.auth` can call the desktop token check in
:mod:`core.api.desktop_auth` without a circular import.
"""

from __future__ import annotations

import hashlib

# Desktop link tokens (SKY-124) carry this prefix.
DESKTOP_TOKEN_PREFIX = "skyl_"


def hash_api_token(token: str) -> str:
    """Return the hex SHA-256 hash persisted at rest for ``token``.

    Args:
        token: Raw token (with prefix).

    Returns:
        Lowercase hex SHA-256 digest of the token.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

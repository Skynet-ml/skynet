"""OAuth connectors for the Mac app, relayed from the linked hosted account.

The Mac app has no OAuth apps of its own. The user connects a provider on
hosted, and the Mac app copies the access token through
``GET /desktop/v1/connectors/{provider}``. Hosted keeps the refresh token and
refreshes on its side; the Mac app pulls again when its copy goes stale
(SKY-986).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from ..connectors.vault import ConnectorSecret, ConnectorVault
from . import hosted_link

logger = logging.getLogger(__name__)

RELAY_AUTH_METHOD = hosted_link.RELAY_AUTH_METHOD
REFRESH_LEEWAY = timedelta(seconds=60)
# Some providers (GitHub OAuth Apps, Notion) issue tokens without an expiry, and a
# hosted disconnect does not reach the Mac. Capping the local copy makes the Mac
# ask hosted again on a fixed schedule.
RELAY_TTL = timedelta(minutes=15)


def is_relay(secret: ConnectorSecret) -> bool:
    """Report whether a stored connector came through the hosted relay.

    Args:
        secret: The stored connector.

    Returns:
        ``True`` for a relayed OAuth token.
    """
    return secret.auth_method == RELAY_AUTH_METHOD


def pull(vault: ConnectorVault, username: str, provider: str) -> str | None:
    """Copy the hosted OAuth token for ``provider`` into the local vault.

    Args:
        vault: The local connector vault.
        username: The local user.
        provider: Provider slug.

    Returns:
        The access token, or ``None`` when hosted has no OAuth connection for
        the provider.

    Raises:
        DomainError: The errors of :func:`hosted_link.hosted_request`, such as
            409 when the Mac is not linked or 503 when hosted is unreachable.
    """
    body = hosted_link.hosted_get(
        vault.engine, username, f"/desktop/v1/connectors/{provider}", absent_code="connectors.not_connected"
    )
    token = body.get("access_token")
    if not isinstance(token, str) or not token:
        logger.info("Hosted has no OAuth connection for %s", provider)
        return None
    expires_at = datetime.now(UTC) + RELAY_TTL
    if body.get("expires_at"):
        expires_at = min(expires_at, datetime.fromisoformat(body["expires_at"]))
    vault.save(
        username,
        provider,
        access_token=token,
        auth_method=RELAY_AUTH_METHOD,
        account_label=body.get("account_label"),
        expires_at=expires_at,
        scopes=body.get("scopes"),
    )
    logger.info("Copied the hosted OAuth token for %s", provider)
    return token


def current_token(vault: ConnectorVault, username: str, provider: str, secret: ConnectorSecret) -> str | None:
    """Return a usable relayed token, pulling again from hosted when stale.

    Args:
        vault: The local connector vault.
        username: The local user.
        provider: Provider slug.
        secret: The stored relay connector.

    Returns:
        The access token, or ``None`` when hosted no longer has a connection.
    """
    if secret.expires_at is None or secret.expires_at - REFRESH_LEEWAY > datetime.now(UTC):
        return secret.access_token
    return pull(vault, username, provider)

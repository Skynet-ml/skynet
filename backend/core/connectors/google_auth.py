"""Google credentials shared by the Sheets and Cloud Storage connectors.

Two ways in: the user's own Google account through OAuth (Sheets only), or a
service-account JSON key pasted into the vault. The key mints short-lived
access tokens with a self-signed RS256 assertion, which keeps the dependency
footprint at ``cryptography`` (already here for the vault) rather than the
whole Google SDK.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from ..api.errors import DomainError
from .transport import label

API_TIMEOUT = httpx.Timeout(30.0)
ASSERTION_TTL_SECONDS = 3600
TOKEN_REFRESH_MARGIN_SECONDS = 300

_tokens: dict[tuple[str, str], tuple[float, str]] = {}
_tokens_lock = threading.Lock()


def _b64url(data: bytes) -> str:
    """Base64url-encode without padding, as JWTs require.

    Args:
        data: Raw bytes.

    Returns:
        The encoded text.
    """
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def parse_service_account(raw: str) -> dict[str, Any]:
    """Validate a pasted service-account JSON key.

    Args:
        raw: The JSON text.

    Returns:
        The parsed key.

    Raises:
        DomainError: 400 when the text is not a service-account key.
    """
    try:
        key = json.loads(raw)
    except ValueError as exc:
        raise DomainError("connectors.invalid_credentials", status=400) from exc
    if not isinstance(key, dict) or key.get("type") != "service_account":
        raise DomainError("connectors.invalid_credentials", status=400)
    if not all(isinstance(key.get(field), str) for field in ("client_email", "private_key", "token_uri")):
        raise DomainError("connectors.invalid_credentials", status=400)
    try:
        serialization.load_pem_private_key(key["private_key"].encode("utf-8"), password=None)
    except (ValueError, TypeError) as exc:
        raise DomainError("connectors.invalid_credentials", status=400) from exc
    return key


def service_account_token(key: dict[str, Any], scope: str, provider: str) -> str:
    """Return an access token for a service account, reusing one until it nears expiry.

    Minting signs an assertion and calls Google, and a single browse makes a
    dozen provider calls, so the token is cached per key and scope.

    Args:
        key: The parsed service-account key.
        scope: Space-separated OAuth scopes.
        provider: Connector name for error reporting.

    Returns:
        A bearer token.

    Raises:
        DomainError: 502 when Google is unreachable; 400 when the key is refused.
    """
    cache_key = (hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest(), scope)
    with _tokens_lock:
        cached = _tokens.get(cache_key)
    if cached is not None and cached[0] > time.monotonic():
        return cached[1]
    token, expires_in = _mint_token(key, scope, provider)
    with _tokens_lock:
        _tokens[cache_key] = (time.monotonic() + expires_in - TOKEN_REFRESH_MARGIN_SECONDS, token)
    return token


def _mint_token(key: dict[str, Any], scope: str, provider: str) -> tuple[str, int]:
    """Mint an access token for a service account.

    Args:
        key: The parsed service-account key.
        scope: Space-separated OAuth scopes.
        provider: Connector name for error reporting.

    Returns:
        ``(token, lifetime in seconds)``.

    Raises:
        DomainError: 502 when Google is unreachable; 400 when the key is refused.
    """
    now = int(time.time())
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode("ascii"))
    claims = _b64url(
        json.dumps(
            {
                "iss": key["client_email"],
                "scope": scope,
                "aud": key["token_uri"],
                "iat": now,
                "exp": now + ASSERTION_TTL_SECONDS,
            }
        ).encode("ascii")
    )
    signing_input = f"{header}.{claims}".encode("ascii")
    private_key = serialization.load_pem_private_key(key["private_key"].encode("utf-8"), password=None)
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise DomainError("connectors.invalid_credentials", status=400)
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    assertion = f"{header}.{claims}.{_b64url(signature)}"
    try:
        response = httpx.post(
            key["token_uri"],
            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
            timeout=API_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise DomainError("connectors.unreachable", status=502, provider=label(provider)) from exc
    if response.status_code >= 400:
        raise DomainError("connectors.invalid_credentials", status=400)
    body = response.json()
    token = body.get("access_token")
    if not isinstance(token, str) or not token:
        raise DomainError("connectors.invalid_credentials", status=400)
    expires_in = body.get("expires_in")
    return token, expires_in if isinstance(expires_in, int) else ASSERTION_TTL_SECONDS

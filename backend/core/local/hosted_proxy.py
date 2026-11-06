"""Send the Mac app's web calls to hosted with the linked account (SKY-223 slice 2).

The desktop token stays in this process: the renderer calls
``/local/hosted/<path>`` and this module adds the token, sends the call to
``settings.hosted_api_url`` and streams the answer back, server-sent events
included. Hosted decides which routes the token may use.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

import httpx
from sqlalchemy import Engine

from ..api.errors import DomainError
from . import hosted_link

logger = logging.getLogger(__name__)

# A long read limit keeps a quiet event stream open; a stream that stays silent
# longer ends, and the browser's EventSource reconnects with Last-Event-ID.
_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)
_REQUEST_HEADERS = ("accept", "accept-language", "content-type", "idempotency-key", "if-none-match", "last-event-id")
_RESPONSE_HEADERS = (
    "cache-control",
    "content-disposition",
    "content-type",
    "etag",
    "last-modified",
    "retry-after",
    "x-accel-buffering",
)
_NOT_ALLOWED = "auth.desktop_token_not_allowed"


def safe_path(path: str) -> bool:
    """Return whether a proxied path stays on the hosted API host.

    Args:
        path: The path after ``/local/hosted/``.

    Returns:
        ``False`` for an empty path, a scheme, ``//``, a backslash, a ``?`` or
        ``#``, a ``%``, a ``.`` or ``..`` segment, a control character, or a
        hosted ``desktop/`` link route.
    """
    # The route already decoded the path once; a "%" left here would decode a
    # second time on hosted, so "%2564esktop" would reach the desktop routes.
    if not path or path.startswith("/") or "//" in path or any(ch in path for ch in "\\?#%"):
        return False
    first = path.split("/", 1)[0]
    # The link routes belong to the local link flow; the renderer must not revoke or refresh the link itself.
    if ":" in first or first == "desktop":
        return False
    if any(ord(ch) < 0x20 or ch == "\x7f" for ch in path):
        return False
    return all(segment not in (".", "..") for segment in path.split("/"))


class HostedAnswer:
    """An open hosted response that the caller streams and then closes."""

    def __init__(self, client: httpx.Client, response: httpx.Response, status: int, body: bytes | None = None) -> None:
        """Hold the open client and response.

        Args:
            client: The client that owns the connection.
            response: The open hosted response.
            status: The status to send to the renderer.
            body: A body that replaces the hosted body, or ``None`` to stream it.
        """
        self._client = client
        self._response = response
        self.status = status
        self._body = body
        self.headers = {name: response.headers[name] for name in _RESPONSE_HEADERS if name in response.headers}
        location = _local_location(client.base_url, response.request.url, response.headers.get("location"))
        if location is not None:
            self.headers["location"] = location

    def chunks(self) -> Iterator[bytes]:
        """Yield the body, then close the connection.

        Yields:
            The decoded body bytes.
        """
        try:
            if self._body is not None:
                yield self._body
                return
            yield from self._response.iter_bytes()
        except httpx.HTTPError as exc:
            # The status is already sent, so a broken stream can only end early.
            logger.info("Hosted stream ended early (%s)", type(exc).__name__)
        finally:
            self.close()

    def close(self) -> None:
        """Close the response and its client; safe to call more than once."""
        self._response.close()
        self._client.close()


def _local_location(base: httpx.URL, requested: httpx.URL, location: str | None) -> str | None:
    """Point a hosted redirect at the proxy.

    Args:
        base: The hosted API base URL.
        requested: The hosted URL that answered; a relative location resolves against it.
        location: The hosted ``Location`` header, or ``None``.

    Returns:
        The ``/local/hosted/...`` path for a redirect on the hosted host, or
        ``None`` for no redirect or one that leaves the host.
    """
    if location is None:
        return None
    try:
        target = requested.join(location)
    except httpx.InvalidURL:
        return None
    prefix = base.raw_path.rstrip(b"/") + b"/"
    if target.scheme != base.scheme or target.host != base.host or not target.raw_path.startswith(prefix):
        return None
    return "/local/hosted/" + target.raw_path[len(prefix) :].decode("ascii")


def _open(method: str, path: str, query: str, headers: dict[str, str], body: bytes, token: str) -> HostedAnswer:
    """Send one request to hosted and return the open response.

    Args:
        method: The HTTP verb.
        path: A path that :func:`safe_path` accepted.
        query: The raw query string, or ``""``.
        headers: The request headers to forward.
        body: The request body.
        token: The desktop access token.

    Returns:
        The open answer with the hosted status.

    Raises:
        DomainError: 503 ``hosted_link.unreachable`` on a network error, 502
            ``hosted_link.failed`` for an invalid URL or one that leaves the
            hosted host.
    """
    client = hosted_link._client(_TIMEOUT)
    try:
        request = client.build_request(
            method,
            # The raw query passes unchanged; QueryParams would re-encode it.
            f"/{path}?{query}" if query else f"/{path}",
            headers={**headers, "Authorization": f"Bearer {token}"},
            content=body or None,
        )
        # The base URL decides the host; this check keeps the token on it whatever the path holds.
        if request.url.host != client.base_url.host or request.url.scheme != client.base_url.scheme:
            raise DomainError("hosted_link.failed", status=502)
        response = client.send(request, stream=True)
    except httpx.InvalidURL as exc:
        client.close()
        raise DomainError("hosted_link.failed", status=502) from exc
    except httpx.HTTPError as exc:
        client.close()
        raise DomainError("hosted_link.unreachable", status=503) from exc
    except BaseException:
        client.close()
        raise
    return HostedAnswer(client, response, response.status_code)


def _error_code(answer: HostedAnswer) -> tuple[str, bytes]:
    """Read a small hosted error body and return its code and the bytes.

    Args:
        answer: An open non-2xx answer.

    Returns:
        The hosted ``code`` (or ``""``) and the body bytes.
    """
    try:
        raw = answer._response.read()
    except httpx.HTTPError:
        return "", b""
    try:
        body: Any = json.loads(raw)
    except ValueError:
        return "", raw
    return (str(body.get("code", "")) if isinstance(body, dict) else ""), raw


def forward(
    engine: Engine,
    username: str,
    method: str,
    path: str,
    query: str,
    headers: dict[str, str],
    body: bytes,
) -> HostedAnswer:
    """Send a renderer call to hosted with the linked account.

    A 401 that asks for a refresh refreshes the pair once and sends the call
    once more. A link that hosted dropped is cleared locally (401
    ``hosted_link.revoked``). A route that hosted closes to the Mac token
    answers 403 with the hosted body, so the renderer does not treat it as a
    sign-out. Every other hosted answer passes through unchanged.

    Args:
        engine: Local database.
        username: The local user.
        method: The HTTP verb.
        path: A path that :func:`safe_path` accepted.
        query: The raw query string.
        headers: The renderer's request headers (lower-case names).
        body: The request body.

    Returns:
        The open answer; the caller streams ``chunks()``.

    Raises:
        DomainError: 409 when not linked, 401 when the link is gone, 502 on
            another hosted 401, 503 when hosted is unreachable.
    """
    # httpx refuses a non-ASCII header value, so such a header is dropped.
    sent = {name: headers[name] for name in _REQUEST_HEADERS if name in headers and headers[name].isascii()}
    token = hosted_link.access_token(engine, username)
    answer = _open(method, path, query, sent, body, token)
    if answer.status != 401:
        return answer
    code, raw = _error_code(answer)
    answer.close()
    if code in hosted_link._REFRESH_CODES:
        token = hosted_link.refresh_after(engine, username, stale_access_token=token)
        answer = _open(method, path, query, sent, body, token)
        if answer.status != 401:
            return answer
        code, raw = _error_code(answer)
        answer.close()
        # Only reached after a refresh, so a fresh token that hosted does not know means a dead link.
        if code == "auth.invalid_token":
            raise hosted_link._gone(engine, username)
    if code in hosted_link._GONE_CODES:
        raise hosted_link._gone(engine, username)
    if code == _NOT_ALLOWED:
        return HostedAnswer(answer._client, answer._response, 403, raw)
    # A plain 401 would read as a local sign-out in the renderer.
    logger.warning("Hosted answered 401 (%s) on a proxied call", code or "no code")
    raise DomainError("hosted_link.failed", status=502)

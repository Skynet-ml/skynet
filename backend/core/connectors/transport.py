"""HTTP plumbing shared by the connectors: one error mapping, capped downloads.

Every provider talks plain HTTPS through :mod:`httpx`; this module turns
transport failures and the usual status codes into the same domain errors
so the UI shows one message per situation whichever service was behind it.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..api.errors import DomainError
from ..config import settings
from .tabular import download_ceiling

PROVIDER_LABELS = {
    "huggingface": "Hugging Face",
    "google_sheets": "Google Sheets",
    "github": "GitHub",
    "gitlab": "GitLab",
    "bitbucket": "Bitbucket",
    "s3": "Amazon S3",
    "gcs": "Google Cloud Storage",
    "azure_blob": "Azure Blob Storage",
    "kaggle": "Kaggle",
    "google_drive": "Google Drive",
    "onedrive": "OneDrive",
    "postgres": "PostgreSQL",
    "mysql": "MySQL",
    "bigquery": "BigQuery",
    "snowflake": "Snowflake",
    "foundry": "Palantir Foundry",
    "langfuse": "Langfuse",
    "langsmith": "LangSmith",
    "braintrust": "Braintrust",
    "notion": "Notion",
    "supabase": "Supabase",
}
API_TIMEOUT = httpx.Timeout(30.0)
DOWNLOAD_TIMEOUT = httpx.Timeout(30.0, read=300.0)
# One pooled client for every provider: a browse fans out into many calls to
# the same host (probes, pages), and reusing the TLS connection saves a
# handshake on each of them.
CLIENT = httpx.Client(follow_redirects=True, limits=httpx.Limits(max_connections=64, max_keepalive_connections=32))


def label(provider: str) -> str:
    """Human name of a provider slug for error messages.

    Args:
        provider: The slug.

    Returns:
        The display name, or the slug itself when unknown.
    """
    return PROVIDER_LABELS.get(provider, provider)


def raise_for_status(response: httpx.Response, provider: str) -> None:
    """Map an error status to a domain error.

    Args:
        response: The provider's response.
        provider: Connector name for the message.

    Raises:
        DomainError: 401/403 as ``rejected``, 404 as ``not_found``, anything
            else 4xx/5xx as ``provider_error``.
    """
    status = response.status_code
    if status < 400:
        return
    if status in (401, 403):
        raise DomainError("connectors.rejected", status=409, provider=label(provider))
    if status == 404:
        raise DomainError("connectors.not_found", status=404, provider=label(provider))
    raise DomainError("connectors.provider_error", status=502, provider=label(provider), status_code=status)


def _refuse_redirect(status: int, follow_redirects: bool, provider: str) -> None:
    """Fail on a redirect the caller chose not to follow.

    Args:
        status: The response status.
        follow_redirects: Whether the request followed redirects.
        provider: Connector name for the message.

    Raises:
        DomainError: 502 ``provider_error`` for an unfollowed 3xx, which would
            otherwise pass as success with an empty body.
    """
    if not follow_redirects and 300 <= status < 400:
        raise DomainError("connectors.provider_error", status=502, provider=label(provider), status_code=status)


def request(
    method: str,
    url: str,
    *,
    provider: str,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    data: dict[str, str] | None = None,
    json: Any = None,
    follow_redirects: bool = True,
) -> httpx.Response:
    """Perform one API call and map failures to domain errors.

    Args:
        method: HTTP method.
        url: Absolute URL.
        provider: Connector name for error messages.
        headers: Request headers.
        params: Query parameters.
        data: Form body, if any.
        json: JSON body, if any.
        follow_redirects: ``False`` for a user-supplied host, so a redirect
            cannot steer the request at an internal address.

    Returns:
        A successful response.

    Raises:
        DomainError: 502 ``unreachable`` on transport failure, or whatever
            :func:`raise_for_status` maps the status to.
    """
    try:
        response = CLIENT.request(
            method,
            url,
            headers=headers,
            params=params,
            data=data,
            json=json,
            timeout=API_TIMEOUT,
            follow_redirects=follow_redirects,
        )
    except httpx.HTTPError as exc:
        raise DomainError("connectors.unreachable", status=502, provider=label(provider)) from exc
    _refuse_redirect(response.status_code, follow_redirects, provider)
    raise_for_status(response, provider)
    return response


def get_json(
    url: str,
    *,
    provider: str,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
    follow_redirects: bool = True,
) -> Any:
    """GET a JSON document.

    Args:
        url: Absolute URL.
        provider: Connector name for error messages.
        headers: Request headers.
        params: Query parameters.
        follow_redirects: Passed through to :func:`request`.

    Returns:
        The decoded body.

    Raises:
        DomainError: 502 ``provider_error`` when the body is not JSON.
    """
    response = request(
        "GET", url, provider=label(provider), headers=headers, params=params, follow_redirects=follow_redirects
    )
    try:
        return response.json()
    except ValueError as exc:
        raise DomainError("connectors.provider_error", status=502, provider=label(provider), status_code=200) from exc


def post_json(
    url: str, *, provider: str, headers: dict[str, str], body: Any, params: dict[str, Any] | None = None
) -> Any:
    """POST a JSON body and decode the JSON reply.

    Args:
        url: Absolute URL.
        provider: Connector name for error messages.
        headers: Request headers.
        body: The JSON body.
        params: Query parameters.

    Returns:
        The decoded reply.

    Raises:
        DomainError: 502 ``provider_error`` when the reply is not JSON.
    """
    response = request("POST", url, provider=provider, headers=headers, params=params, json=body)
    try:
        return response.json()
    except ValueError as exc:
        raise DomainError("connectors.provider_error", status=502, provider=label(provider), status_code=200) from exc


def download(
    url: str,
    *,
    provider: str,
    headers: dict[str, str],
    max_bytes: int | None = None,
    params: dict[str, Any] | None = None,
    follow_redirects: bool = True,
) -> tuple[bytes, bool]:
    """Stream a file, stopping early at ``max_bytes`` or refusing past the ceiling.

    Args:
        url: Absolute URL.
        provider: Connector name for error messages.
        headers: Request headers (a ``Range`` header is fine; a 206 is accepted).
        max_bytes: Stop reading after this many bytes and report truncation.
            ``None`` reads everything up to the import ceiling.
        params: Query parameters.
        follow_redirects: Passed through to the client; ``False`` for a
            user-supplied host.

    Returns:
        ``(content, truncated)``.

    Raises:
        DomainError: 413 ``import_too_large`` when a full download crosses the
            ceiling; 502 on transport failure.
    """
    limit = max_bytes if max_bytes is not None else download_ceiling()
    chunks: list[bytes] = []
    received = 0
    truncated = False
    try:
        with CLIENT.stream(
            "GET", url, headers=headers, params=params, timeout=DOWNLOAD_TIMEOUT, follow_redirects=follow_redirects
        ) as response:
            _refuse_redirect(response.status_code, follow_redirects, provider)
            raise_for_status(response, provider)
            for chunk in response.iter_bytes():
                chunks.append(chunk)
                received += len(chunk)
                if received > limit:
                    truncated = True
                    break
    except httpx.HTTPError as exc:
        raise DomainError("connectors.unreachable", status=502, provider=label(provider)) from exc
    if truncated and max_bytes is None:
        raise DomainError(
            "connectors.import_too_large",
            status=413,
            max_mb=round(settings.dataset_max_file_bytes / (1024 * 1024), 1),
        )
    content = b"".join(chunks)
    return content[:limit] if truncated else content, truncated

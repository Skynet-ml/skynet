"""Hugging Face client behind the connector: identity, OAuth, Hub search and
dataset import.

Every outbound call takes the caller's own token (or none, for public data),
so a user only ever sees what their Hugging Face account can see. Imports read
the Hub's auto-converted parquet files rather than paging the dataset viewer,
which avoids the viewer's cell truncation and 100-row pages and lets a user
pull a whole split in one go, bounded only by the library's file cap and their
storage quota.
"""

from __future__ import annotations

import base64
import json
import secrets
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

# dspy parks a lazy numpy proxy in sys.modules; importing a numpy submodule
# materialises the real package before pyarrow's C extension reaches for it,
# which otherwise re-executes numpy mid-import and fails.
import numpy.version  # noqa: F401
import pyarrow.parquet as pq
from cryptography.fernet import InvalidToken

from ..api.errors import DomainError
from ..config import settings
from ..local import connector_relay
from .vault import ConnectorVault, vault_cipher

PROVIDER = "huggingface"
HUB_URL = "https://huggingface.co"
VIEWER_URL = "https://datasets-server.huggingface.co"
OAUTH_SCOPES = "openid profile read-repos"
STATE_TTL_SECONDS = 600
PREVIEW_ROWS = 20
API_TIMEOUT = httpx.Timeout(30.0)
DOWNLOAD_TIMEOUT = httpx.Timeout(30.0, read=300.0)
# Parquet packs tighter than the gzip'd JSON the library caps, so a file set
# comfortably over the cap can be refused before any bytes are downloaded.
DOWNLOAD_CEILING_MULTIPLIER = 2
REFRESH_LEEWAY = timedelta(seconds=60)
_IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF8", "image/gif"),
    (b"RIFF", "image/webp"),
)


@dataclass(frozen=True)
class TokenSet:
    """One OAuth token response, with expiry resolved to an instant."""

    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    scope: str | None


def _headers(token: str | None) -> dict[str, str]:
    """Build request headers, attaching the bearer token when there is one.

    Args:
        token: The user's Hugging Face token, or ``None`` for anonymous calls.

    Returns:
        Headers for :mod:`httpx`.
    """
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _get(url: str, *, token: str | None, params: dict[str, Any] | None = None) -> httpx.Response:
    """Issue a GET against Hugging Face, translating transport failures.

    Args:
        url: Absolute URL.
        token: Bearer token, if any.
        params: Query parameters.

    Returns:
        The raw response; status handling is left to the caller.

    Raises:
        DomainError: 502 when Hugging Face cannot be reached.
    """
    try:
        return httpx.get(url, params=params, headers=_headers(token), timeout=API_TIMEOUT, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise DomainError("connectors.hf_unreachable", status=502) from exc


def verify_token(token: str) -> str:
    """Check a pasted token against ``whoami`` and return the account name.

    Args:
        token: The user's personal access token.

    Returns:
        The Hugging Face username the token belongs to.

    Raises:
        DomainError: 400 when Hugging Face rejects the token; 502 when it is
            unreachable.
    """
    response = _get(f"{HUB_URL}/api/whoami-v2", token=token)
    if response.status_code in (401, 403):
        raise DomainError("connectors.hf_invalid_token", status=400)
    if response.status_code >= 400:
        raise DomainError("connectors.hf_unreachable", status=502)
    name = response.json().get("name")
    if not isinstance(name, str) or not name:
        raise DomainError("connectors.hf_invalid_token", status=400)
    return name


def oauth_available() -> bool:
    """Report whether "Continue with Hugging Face" can be offered.

    Returns:
        ``True`` when an OAuth client id and the vault key are configured.
    """
    return bool(settings.hf_oauth_client_id) and settings.byok_vault_key is not None


def _require_oauth() -> str:
    """Return the configured OAuth client id or refuse the OAuth flow.

    Returns:
        The client id.

    Raises:
        DomainError: 503 when the OAuth app is not configured.
    """
    if not settings.hf_oauth_client_id:
        raise DomainError("connectors.hf_oauth_not_configured", status=503)
    return settings.hf_oauth_client_id


def build_authorize_url(username: str, redirect_uri: str) -> str:
    """Start the PKCE authorization-code flow for ``username``.

    The PKCE verifier and the initiating user travel inside the encrypted
    ``state`` so the unauthenticated callback can bind the code to them.

    Args:
        username: The app user linking their account.
        redirect_uri: Callback URL registered on the OAuth app.

    Returns:
        The URL to send the browser to.

    Raises:
        DomainError: 503 when OAuth or the vault is not configured.
    """
    client_id = _require_oauth()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    state_payload = json.dumps({"u": username, "v": verifier, "r": redirect_uri}).encode("utf-8")
    state = vault_cipher().encrypt(state_payload).decode("ascii")
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": OAUTH_SCOPES,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{HUB_URL}/oauth/authorize?{query}"


def parse_state(state: str) -> dict[str, str]:
    """Decrypt and validate the ``state`` echoed back by the callback.

    Args:
        state: The opaque state parameter.

    Returns:
        The ``{"u": username, "v": verifier, "r": redirect_uri}`` payload.

    Raises:
        DomainError: 400 when the state is forged, tampered with or older than
            :data:`STATE_TTL_SECONDS`.
    """
    try:
        raw = vault_cipher().decrypt(state.encode("ascii"), ttl=STATE_TTL_SECONDS)
        payload = json.loads(raw)
    except (InvalidToken, ValueError, UnicodeEncodeError) as exc:
        raise DomainError("connectors.hf_oauth_state_invalid", status=400) from exc
    if not all(isinstance(payload.get(k), str) for k in ("u", "v", "r")):
        raise DomainError("connectors.hf_oauth_state_invalid", status=400)
    return payload


def _token_request(form: dict[str, str]) -> TokenSet:
    """POST to the token endpoint and normalise the response.

    Args:
        form: Grant parameters; the client id/secret are added here.

    Returns:
        The issued tokens.

    Raises:
        DomainError: 502 when unreachable; 400 ``hf_oauth_failed`` when the
            grant is refused.
    """
    form = {**form, "client_id": _require_oauth()}
    if settings.hf_oauth_client_secret is not None:
        form["client_secret"] = settings.hf_oauth_client_secret.get_secret_value()
    try:
        response = httpx.post(f"{HUB_URL}/oauth/token", data=form, headers=_headers(None), timeout=API_TIMEOUT)
    except httpx.HTTPError as exc:
        raise DomainError("connectors.hf_unreachable", status=502) from exc
    if response.status_code >= 400:
        raise DomainError("connectors.hf_oauth_failed", status=400)
    body = response.json()
    access = body.get("access_token")
    if not isinstance(access, str) or not access:
        raise DomainError("connectors.hf_oauth_failed", status=400)
    expires_in = body.get("expires_in")
    expires_at = datetime.now(UTC) + timedelta(seconds=int(expires_in)) if isinstance(expires_in, int | float) else None
    return TokenSet(
        access_token=access,
        refresh_token=body.get("refresh_token") or None,
        expires_at=expires_at,
        scope=body.get("scope") or None,
    )


def exchange_code(code: str, verifier: str, redirect_uri: str) -> TokenSet:
    """Trade an authorization code for tokens.

    Args:
        code: The code from the callback.
        verifier: The PKCE verifier minted in :func:`build_authorize_url`.
        redirect_uri: The same redirect URI the code was issued for.

    Returns:
        The issued tokens.
    """
    return _token_request(
        {"grant_type": "authorization_code", "code": code, "code_verifier": verifier, "redirect_uri": redirect_uri}
    )


def refresh_tokens(refresh_token: str) -> TokenSet:
    """Obtain a fresh access token from a refresh token.

    Args:
        refresh_token: The stored refresh token.

    Returns:
        The issued tokens.
    """
    return _token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})


def fetch_userinfo(token: str) -> str | None:
    """Return the account's handle from the OpenID userinfo endpoint.

    Args:
        token: An OAuth access token.

    Returns:
        The ``preferred_username``, or ``None`` when the call fails; a missing
        label never blocks linking.
    """
    try:
        response = httpx.get(f"{HUB_URL}/oauth/userinfo", headers=_headers(token), timeout=API_TIMEOUT)
    except httpx.HTTPError:
        return None
    if response.status_code >= 400:
        return None
    name = response.json().get("preferred_username")
    return name if isinstance(name, str) and name else None


def current_token(vault: ConnectorVault, username: str) -> str | None:
    """Return a usable access token for ``username``, refreshing when stale.

    Args:
        vault: Vault holding the user's connector.
        username: The app user.

    Returns:
        The bearer token, or ``None`` when the user has no linked account.

    Raises:
        DomainError: 409 ``hf_expired`` when the token lapsed and could not be
            refreshed; the connector is marked invalid so the UI prompts a
            reconnect.
    """
    secret = vault.resolve(username, PROVIDER)
    if secret is None:
        return None
    if connector_relay.is_relay(secret):
        # The Mac app holds no refresh token; hosted refreshes and hands over a fresh copy.
        token = connector_relay.current_token(vault, username, PROVIDER, secret)
        if token is None:
            vault.mark_invalid(username, PROVIDER)
            raise DomainError("connectors.hf_expired", status=409)
        return token
    if secret.expires_at is None or secret.expires_at - REFRESH_LEEWAY > datetime.now(UTC):
        return secret.access_token
    if not secret.refresh_token:
        vault.mark_invalid(username, PROVIDER)
        raise DomainError("connectors.hf_expired", status=409)
    try:
        fresh = refresh_tokens(secret.refresh_token)
    except DomainError as exc:
        vault.mark_invalid(username, PROVIDER)
        raise DomainError("connectors.hf_expired", status=409) from exc
    view = vault.get(username, PROVIDER)
    vault.save(
        username,
        PROVIDER,
        access_token=fresh.access_token,
        auth_method="oauth",
        account_label=view.account_label if view else None,
        refresh_token=fresh.refresh_token or secret.refresh_token,
        expires_at=fresh.expires_at,
        scopes=fresh.scope or (view.scopes if view else None),
    )
    return fresh.access_token


def search_datasets(token: str | None, search: str, limit: int) -> list[dict[str, Any]]:
    """Search the Hub for datasets, most downloaded first.

    Args:
        token: The user's token; anonymous search only surfaces public repos.
        search: Free-text query; empty lists the most popular datasets.
        limit: Maximum results (the Hub caps this at 100).

    Returns:
        Compact dataset descriptors.
    """
    params: dict[str, Any] = {"sort": "downloads", "direction": -1, "limit": limit}
    if search.strip():
        params["search"] = search.strip()
    response = _get(f"{HUB_URL}/api/datasets", token=token, params=params)
    if response.status_code >= 400:
        raise DomainError("connectors.hf_unreachable", status=502)
    out: list[dict[str, Any]] = []
    for item in response.json():
        repo_id = item.get("id")
        if not isinstance(repo_id, str):
            continue
        out.append(
            {
                "id": repo_id,
                "author": item.get("author"),
                "downloads": int(item.get("downloads") or 0),
                "likes": int(item.get("likes") or 0),
                "private": bool(item.get("private")),
                "gated": bool(item.get("gated")),
                "last_modified": item.get("lastModified"),
            }
        )
    return out


def _viewer(path: str, *, token: str | None, params: dict[str, Any]) -> dict[str, Any]:
    """Call the dataset-viewer API, mapping its failures to domain errors.

    Args:
        path: Endpoint path such as ``/splits``.
        token: The user's token, if any.
        params: Query parameters (``dataset`` is always among them).

    Returns:
        The decoded JSON body.

    Raises:
        DomainError: 404 when the dataset is unknown or out of reach; 409 when
            the viewer cannot serve it (no parquet conversion, still
            processing, or a viewer-side error); 502 when unreachable.
    """
    response = _get(f"{VIEWER_URL}{path}", token=token, params=params)
    if response.status_code in (401, 403, 404):
        raise DomainError("connectors.hf_dataset_not_found", status=404, repo_id=params["dataset"])
    if response.status_code >= 400:
        raise DomainError("connectors.hf_dataset_unsupported", status=409, repo_id=params["dataset"])
    return response.json()


def list_splits(token: str | None, repo_id: str) -> list[dict[str, Any]]:
    """List a dataset's configs and splits with their sizes.

    Args:
        token: The user's token, if any.
        repo_id: Hub repo id such as ``squad`` or ``owner/name``.

    Returns:
        One entry per ``(config, split)``; sizes are ``None`` while the viewer
        is still computing them.
    """
    splits = _viewer("/splits", token=token, params={"dataset": repo_id}).get("splits") or []
    sizes: dict[tuple[str, str], dict[str, Any]] = {}
    size_response = _get(f"{VIEWER_URL}/size", token=token, params={"dataset": repo_id})
    if size_response.status_code < 400:
        for entry in (size_response.json().get("size") or {}).get("splits") or []:
            sizes[(entry.get("config"), entry.get("split"))] = entry
    out = []
    for entry in splits:
        config, split = entry.get("config"), entry.get("split")
        if not isinstance(config, str) or not isinstance(split, str):
            continue
        size = sizes.get((config, split), {})
        out.append(
            {
                "config": config,
                "split": split,
                "num_rows": size.get("num_rows"),
                "num_bytes": size.get("num_bytes_parquet_files"),
            }
        )
    if not out:
        raise DomainError("connectors.hf_dataset_unsupported", status=409, repo_id=repo_id)
    return out


def _feature_type(spec: Any) -> str:
    """Flatten a viewer feature spec into a short type label.

    Args:
        spec: The ``type`` object from ``/rows`` features.

    Returns:
        ``string``, ``int64``, ``Image``, ``list``… whatever best describes it.
    """
    if isinstance(spec, dict):
        kind = spec.get("_type")
        if kind == "Value":
            return str(spec.get("dtype") or "value")
        if isinstance(kind, str):
            return kind
        return "struct"
    if isinstance(spec, list):
        return "list"
    return "value"


def _guess_mime(data: bytes) -> str:
    """Sniff an image MIME type from leading bytes.

    Args:
        data: Raw image bytes.

    Returns:
        A MIME type, ``application/octet-stream`` when unrecognised.
    """
    for magic, mime in _IMAGE_MAGIC:
        if data.startswith(magic):
            return mime
    return "application/octet-stream"


def _is_image_struct(value: Any) -> bool:
    """Tell whether a cell is the ``{bytes, path}`` struct the Image feature stores.

    Args:
        value: A decoded parquet cell.

    Returns:
        ``True`` for an Image struct.
    """
    return isinstance(value, dict) and "bytes" in value and "path" in value and set(value) <= {"bytes", "path"}


def normalize_cell(value: Any) -> Any:
    """Coerce one cell into something the dataset library stores as JSON.

    Images become data URIs (or the viewer's asset URL), raw bytes are
    base64'd, temporal and decimal values are stringified; lists and structs
    recurse.

    Args:
        value: A decoded parquet or viewer cell.

    Returns:
        A JSON-serialisable value.
    """
    if value is None or isinstance(value, str | bool | int | float):
        return value
    if _is_image_struct(value):
        data = value.get("bytes")
        if isinstance(data, bytes | bytearray):
            return f"data:{_guess_mime(bytes(data))};base64,{base64.b64encode(bytes(data)).decode('ascii')}"
        return value.get("path")
    if isinstance(value, dict):
        if isinstance(value.get("src"), str) and set(value) <= {"src", "height", "width"}:
            return value["src"]
        return {str(k): normalize_cell(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [normalize_cell(v) for v in value]
    if isinstance(value, bytes | bytearray):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def preview_rows(token: str | None, repo_id: str, config: str, split: str) -> dict[str, Any]:
    """Fetch the first rows of a split through the dataset viewer.

    Args:
        token: The user's token, if any.
        repo_id: Hub repo id.
        config: Dataset config name.
        split: Split name.

    Returns:
        ``{"columns": [{name, type}], "rows": [...], "num_rows_total": n}``.
    """
    body = _viewer(
        "/rows",
        token=token,
        params={"dataset": repo_id, "config": config, "split": split, "offset": 0, "length": PREVIEW_ROWS},
    )
    columns = [
        {"name": f.get("name"), "type": _feature_type(f.get("type"))}
        for f in body.get("features") or []
        if isinstance(f.get("name"), str)
    ]
    rows = [
        {str(k): normalize_cell(v) for k, v in (entry.get("row") or {}).items()} for entry in body.get("rows") or []
    ]
    return {"columns": columns, "rows": rows, "num_rows_total": body.get("num_rows_total")}


def _parquet_files(token: str | None, repo_id: str, config: str, split: str) -> list[dict[str, Any]]:
    """List the auto-converted parquet files backing one split.

    Args:
        token: The user's token, if any.
        repo_id: Hub repo id.
        config: Dataset config name.
        split: Split name.

    Returns:
        The matching ``parquet_files`` entries, in the viewer's order.

    Raises:
        DomainError: 409 when the split has no parquet conversion.
    """
    body = _viewer("/parquet", token=token, params={"dataset": repo_id, "config": config, "split": split})
    files = [
        f
        for f in body.get("parquet_files") or []
        if f.get("config") == config and f.get("split") == split and isinstance(f.get("url"), str)
    ]
    if not files:
        raise DomainError("connectors.hf_dataset_unsupported", status=409, repo_id=repo_id)
    return files


def _download(url: str, token: str | None, target: Path) -> None:
    """Stream one parquet file to disk.

    Args:
        url: The file URL (a Hub ``resolve`` URL that redirects to the CDN).
        token: The user's token; needed for private and gated repos.
        target: Destination path.

    Raises:
        DomainError: 404 when the file is out of reach; 502 on transport
            failure.
    """
    try:
        with (
            httpx.stream(
                "GET", url, headers=_headers(token), timeout=DOWNLOAD_TIMEOUT, follow_redirects=True
            ) as response,
            target.open("wb") as handle,
        ):
            if response.status_code >= 400:
                raise DomainError("connectors.hf_dataset_not_found", status=404, repo_id=url)
            for chunk in response.iter_bytes():
                handle.write(chunk)
    except httpx.HTTPError as exc:
        raise DomainError("connectors.hf_unreachable", status=502) from exc


def import_split(
    token: str | None, repo_id: str, config: str, split: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Download a whole split and decode it into library rows.

    Args:
        token: The user's token, if any.
        repo_id: Hub repo id.
        config: Dataset config name.
        split: Split name.

    Returns:
        ``(rows, column_schema)`` ready for the library's gated save. The
        schema carries the column order and marks Image columns as ``image``;
        roles are left for the user to assign.

    Raises:
        DomainError: 413 when the parquet files already exceed the download
            ceiling derived from the library's file cap.
    """
    files = _parquet_files(token, repo_id, config, split)
    ceiling = settings.dataset_max_file_bytes * DOWNLOAD_CEILING_MULTIPLIER
    total = sum(int(f.get("size") or 0) for f in files)
    if total > ceiling:
        raise DomainError(
            "connectors.hf_import_too_large",
            status=413,
            max_mb=round(settings.dataset_max_file_bytes / (1024 * 1024), 1),
        )
    rows: list[dict[str, Any]] = []
    column_order: list[str] = []
    image_columns: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="hf-import-") as tmp:
        for index, entry in enumerate(files):
            target = Path(tmp) / f"{index}.parquet"
            _download(entry["url"], token, target)
            table = pq.read_table(target)
            for name in table.column_names:
                if name not in column_order:
                    column_order.append(name)
            for raw in table.to_pylist():
                row: dict[str, Any] = {}
                for name, value in raw.items():
                    if _is_image_struct(value):
                        image_columns.add(name)
                    row[name] = normalize_cell(value)
                rows.append(row)
            target.unlink(missing_ok=True)
    column_schema = {
        "column_order": column_order,
        "column_roles": {},
        "column_kinds": {name: "image" for name in column_order if name in image_columns},
    }
    return rows, column_schema

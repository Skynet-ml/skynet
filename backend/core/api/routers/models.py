"""Routes for the model catalog and live discovery. [INTERNAL]

``GET /models`` returns the curated catalog. ``POST /models/discover`` probes
an OpenAI-compatible endpoint for its available models. ``POST
/models/call-holds`` prices the reservation one call of each model takes.

All endpoints are hidden from the public Scalar reference (none are in
``_SCALAR_PUBLIC_PATHS``) — devs hitting ``/run`` directly pass model
strings explicitly and don't need the catalog/discovery dance.
"""

from __future__ import annotations

import http.client
import ipaddress
import json as _json
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import fields
from decimal import Decimal
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ...billing.openrouter_quotes import fetch_endpoint_prices, price_text_request, resolve_model_slug
from ...billing.operation_pricing import ChargePolicy, UnpricedOperationError
from ...billing.vercel_usage import sandbox_hold_cents
from ...config import settings
from ...local.cli_catalog import SUBSCRIPTION_MODEL_PREFIX
from ...local.cli_probe import probe_clis
from ...service_gateway.optimization.blackbox.runner import DEFAULT_MAX_TOKENS
from ...service_gateway.optimization.blackbox.sandbox_contract import SandboxSpec
from ...service_gateway.optimization.blackbox.sandbox_runtime import COVERAGE_SLICE_SECONDS
from ..artificial_analysis import with_artificial_analysis_links
from ..auth import AuthenticatedUser, get_authenticated_user
from ..interview_model import with_interview_default
from ..model_catalog import (
    CatalogModel,
    ModelCatalogResponse,
    get_byok_catalog_cached,
    get_catalog_cached,
    with_zero_retention,
)
from ..response_limits import AGENT_MAX_LIST, AGENT_MAX_TEXT, cap_list, truncate_text

AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]

_DISCOVER_ALLOWED_SCHEMES = frozenset({"http", "https"})

# Cloud metadata service addresses that we never probe, even when the
# operator has explicitly opted in to private-range discovery — these are
# the canonical credential-exfil paths in EC2/GCE/Azure/IBM.
_DISCOVER_BLOCKED_HOSTS = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("fd00:ec2::254"),
    }
)


def _validate_discover_url(raw_url: str) -> tuple[str, str | None, str | None]:
    """Validate ``raw_url`` for use as a /models/discover probe target.

    Resolves the hostname exactly once and validates every resolved address,
    returning the first validated IP so the caller can pin the connection to
    it — closing the DNS-rebinding TOCTOU where a second lookup at connect time
    could swap in a private/metadata address.

    Args:
        raw_url: The user-supplied base URL to validate.

    Returns:
        A ``(normalised_url, pinned_ip, error)`` tuple. ``error`` is ``None``
        when the URL passed every check and ``pinned_ip`` is the validated
        address to connect to; on failure ``pinned_ip`` is ``None`` and
        ``error`` is a short reason string that never echoes the URL itself.
    """
    parsed = urlparse(raw_url)
    if parsed.scheme.lower() not in _DISCOVER_ALLOWED_SCHEMES:
        return raw_url, None, "Only http/https schemes are allowed"
    host = parsed.hostname
    if not host:
        return raw_url, None, "URL is missing a hostname"
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return raw_url, None, "Hostname could not be resolved"
    addresses = {ipaddress.ip_address(info[4][0]) for info in infos}
    for addr in addresses:
        if addr in _DISCOVER_BLOCKED_HOSTS:
            return raw_url, None, "Address is on the blocked metadata-service list"
    if not settings.discover_allow_private:
        for addr in addresses:
            if addr.is_loopback:
                continue
            if addr.is_link_local or addr.is_private or addr.is_reserved or addr.is_multicast:
                return raw_url, None, "Refusing to probe link-local/private/reserved address"
    return raw_url, infos[0][4][0], None


def _open_pinned(url: str, headers: dict[str, str], pinned_ip: str, timeout: float) -> Any:
    """GET ``url`` while connecting only to ``pinned_ip``.

    Pins the TCP target to the address already cleared by
    :func:`_validate_discover_url`, closing the DNS-rebinding TOCTOU where a
    second lookup at connect time could swap in a private/metadata IP. The
    hostname still drives the ``Host`` header, SNI, and TLS certificate
    verification, and urllib's default error processing is preserved so a
    non-2xx response still raises :class:`urllib.error.HTTPError`.

    Args:
        url: Absolute http/https URL to GET.
        headers: Request headers (Accept, optional Authorization).
        pinned_ip: The validated IP literal to connect to.
        timeout: Per-connection timeout in seconds.

    Returns:
        The open ``http.client.HTTPResponse`` (usable as a context manager).
    """

    class _PinnedHTTPConnection(http.client.HTTPConnection):
        """HTTPConnection that dials the pinned IP instead of re-resolving."""

        def connect(self) -> None:
            """Open the socket to ``pinned_ip``, keeping the parsed host/port."""
            self.sock = socket.create_connection((pinned_ip, self.port), self.timeout)

    class _PinnedHTTPSConnection(http.client.HTTPSConnection):
        """HTTPSConnection that dials the pinned IP; SNI/cert use the hostname."""

        def connect(self) -> None:
            """Open a TLS socket to ``pinned_ip`` verified against the hostname."""
            sock = socket.create_connection((pinned_ip, self.port), self.timeout)
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)

    class _PinnedHTTPHandler(urllib.request.HTTPHandler):
        """urllib handler routing http:// through the pinned connection."""

        def http_open(self, req: urllib.request.Request) -> Any:
            """Open ``req`` via the pinned HTTP connection."""
            return self.do_open(_PinnedHTTPConnection, req)

    class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
        """urllib handler routing https:// through the pinned connection."""

        def https_open(self, req: urllib.request.Request) -> Any:
            """Open ``req`` via the pinned HTTPS connection."""
            return self.do_open(_PinnedHTTPSConnection, req)

    opener = urllib.request.build_opener(_PinnedHTTPHandler, _PinnedHTTPSHandler)
    request = urllib.request.Request(url, headers=headers, method="GET")
    return opener.open(request, timeout=timeout)


class DiscoverModelsRequest(BaseModel):
    """Request payload for POST /models/discover."""

    base_url: str
    api_key: str | None = None


class DiscoverModelsResponse(BaseModel):
    """Response payload for POST /models/discover."""

    models: list[str] = []
    base_url: str
    error: str | None = None
    truncated: bool = False
    total: int | None = None


def discover_models_at_endpoint(
    base_url: str,
    api_key: str | None = None,
    *,
    limit: int | None = AGENT_MAX_LIST,
) -> DiscoverModelsResponse:
    """Fetch model ids from one validated OpenAI-compatible endpoint.

    Args:
        base_url: Provider API base URL.
        api_key: Optional bearer secret.
        limit: Cap on returned ids (the agent-tool default); ``None`` returns
            every id so a user-facing catalog is not silently clipped.

    Returns:
        Discovered model ids, or a bounded error response.
    """
    base = base_url.rstrip("/")
    _, pinned_ip, validation_error = _validate_discover_url(base)
    if validation_error is not None or pinned_ip is None:
        return DiscoverModelsResponse(
            models=[],
            base_url=base,
            error=truncate_text(validation_error or "Hostname could not be resolved", AGENT_MAX_TEXT),
        )
    candidates = [f"{base}/v1/models", f"{base}/models"]
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    last_error: str | None = None
    for url in candidates:
        try:
            with _open_pinned(url, headers, pinned_ip, timeout=8) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            data = _json.loads(body)
            raw = data.get("data") if isinstance(data, dict) else data
            if not isinstance(raw, list):
                last_error = "Unexpected response shape"
                continue
            ids: list[str] = []
            for item in raw:
                if isinstance(item, dict):
                    val = item.get("id") or item.get("name")
                    if isinstance(val, str) and val:
                        ids.append(val)
                elif isinstance(item, str):
                    ids.append(item)
            sorted_ids = sorted(set(ids))
            clipped, truncated, total = cap_list(sorted_ids, limit if limit is not None else len(sorted_ids))
            return DiscoverModelsResponse(
                models=clipped,
                base_url=base,
                truncated=truncated,
                total=total,
            )
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
            if exc.code == 404:
                continue
            break
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = str(exc.reason if hasattr(exc, "reason") else exc)
            break
        except (ValueError, _json.JSONDecodeError) as exc:
            last_error = f"Invalid JSON: {exc}"
            break
    return DiscoverModelsResponse(
        models=[],
        base_url=base,
        error=truncate_text(last_error or "Unable to fetch models", AGENT_MAX_TEXT),
    )


class AgentModelEntry(BaseModel):
    """Agent-safe model row: single canonical name, no display label.

    The shared catalog model carries both ``value`` (provider-prefixed,
    submit-ready) and ``label`` (display-only, no prefix). Exposing both to
    the agent tempted it to copy ``label``, producing un-prefixed
    ``model_name`` like ``gpt-4o-mini`` that dspy.LM rejects. This shape
    drops ``label`` entirely so the agent has exactly one identifier to
    pass to ``update_wizard_state`` / ``submit_job``.
    """

    name: str = Field(
        description=(
            "Pass this verbatim as ``model_name`` to update_wizard_state and "
            "submit endpoints. Always provider-prefixed (e.g. ``openai/gpt-4o-mini``)."
        ),
    )
    provider: str = Field(description="Provider slug (e.g. ``openai``, ``anthropic``).")
    supports_thinking: bool = Field(default=False)
    supports_vision: bool = Field(default=False)
    max_input_tokens: int | None = Field(default=None)


class AgentModelCatalogResponse(BaseModel):
    """Envelope for ``GET /models/for-agent`` — only canonical, submit-ready names."""

    models: list[AgentModelEntry]


def _catalog_to_agent_entry(model: CatalogModel) -> AgentModelEntry:
    """Project a catalog row to the agent-safe shape (drops label).

    Args:
        model: A row from the shared model catalog.

    Returns:
        An ``AgentModelEntry`` exposing only the canonical prefixed name.
    """
    return AgentModelEntry(
        name=model.value,
        provider=model.provider,
        supports_thinking=model.supports_thinking,
        supports_vision=model.supports_vision,
        max_input_tokens=model.max_input_tokens,
    )


def _subscription_agent_entries() -> list[AgentModelEntry]:
    """List the subscription models of every signed-in coding CLI on this Mac.

    Local mode has no managed models and no platform provider keys, so these
    ``subscription/<cli>/<slug>`` ids are the models a local run can use.

    Returns:
        One row per model of each installed, current, signed-in CLI.
    """
    return [
        AgentModelEntry(name=f"{SUBSCRIPTION_MODEL_PREFIX}{status.id}/{slug}", provider=slug.partition("/")[0])
        for status in probe_clis(settings)
        if status.path is not None and status.version_ok and status.logged_in
        for slug in status.models
    ]


class CallHoldModel(BaseModel):
    """One model role whose single-call reservation the wizard needs."""

    name: str = Field(min_length=1, max_length=200)
    token_source: Literal["managed", "byok", "subscription"] = "managed"


class CallHoldsRequest(BaseModel):
    """Request payload for POST /models/call-holds."""

    models: list[CallHoldModel] = Field(max_length=16)


class CallHold(BaseModel):
    """The charged cents one call of a model reserves before it is sent."""

    name: str
    token_source: Literal["managed", "byok", "subscription"]
    cents: float | None = Field(
        default=None,
        description="Charged cents one call reserves; null when the model has no verified pricing.",
    )


class CallHoldsResponse(BaseModel):
    """Response payload for POST /models/call-holds."""

    holds: list[CallHold]
    box_hold_cents: float = Field(
        description="Charged cents the sandboxes a setup check or run opens hold before its first model call.",
    )


# A setup check, like a run's first step, opens an outer box and then a
# workspace box inside it before any model call, and each funds its first
# coverage slice up front; those holds stay until the boxes close.
_BOXES_BEFORE_FIRST_CALL = 2


def box_hold_cents() -> Decimal:
    """Price what a check's or run's boxes hold before its first model call.

    Returns:
        Charged cents for two deny-all boxes of the default size, each funded
        for its first coverage slice.
    """
    vcpus = next(field.default for field in fields(SandboxSpec) if field.name == "vcpus")
    return _BOXES_BEFORE_FIRST_CALL * sandbox_hold_cents(lifetime_ms=COVERAGE_SLICE_SECONDS * 1000, vcpus=vcpus)


# Endpoint prices move rarely, and the wizard re-asks on every model change.
_CALL_HOLD_TTL_SECONDS = 300.0


def call_hold_cents(name: str, token_source: str, *, client: httpx.Client) -> Decimal | None:
    """Price one model call the way the gateway reserves it before dispatch.

    The gateway bounds a call's input by the endpoint's whole context window
    and its output by the sandbox ``llm()`` cap, so a single call can need far
    more than a run's expected usage. Pricing a stand-in body through the same
    ``price_text_request`` keeps the wizard's limit floor equal to that hold.

    Args:
        name: The model as the wizard names it, optionally ``openrouter/``-prefixed.
        token_source: ``managed`` for platform-paid calls, ``byok`` for the caller's key,
            ``subscription`` for the caller's logged-in CLI (never charged).
        client: HTTP client for the public OpenRouter catalog.

    Returns:
        The hold in charged cents, or ``None`` when the model has no verified pricing.
    """
    if token_source in {"subscription", "byok"}:
        return Decimal(0)
    try:
        slug = resolve_model_slug(name, client=client)
        catalog = fetch_endpoint_prices(slug, client=client)
        body = {"model": slug, "messages": [{"role": "user", "content": ""}], "max_tokens": DEFAULT_MAX_TOKENS}
        policy = ChargePolicy("byok_model" if token_source == "byok" else "managed_model")
        return price_text_request(body, catalog, policy).quote.maximum.total
    except (UnpricedOperationError, httpx.HTTPError, ValueError):
        return None


def create_models_router(*, http_client: Callable[[], httpx.Client] | None = None) -> APIRouter:
    """Build the models router.

    Args:
        http_client: Factory for the client that reads OpenRouter prices; tests
            pass one over a mock transport.

    Returns:
        A FastAPI ``APIRouter`` exposing ``/models``, ``/models/for-agent``,
        ``/models/discover`` and ``/models/call-holds``.
    """
    router = APIRouter()
    make_client = http_client or (lambda: httpx.Client(timeout=10, follow_redirects=False, trust_env=False))
    hold_cache: dict[tuple[str, str], tuple[float, Decimal | None]] = {}
    hold_lock = threading.Lock()

    @router.get(
        "/models",
        response_model=ModelCatalogResponse,
        summary="List the curated model catalog",
    )
    def list_models() -> ModelCatalogResponse:
        """List available models for the frontend (value + label + grouping).

        Frontend-only: the agent must hit ``/models/for-agent`` instead, which
        returns a single canonical ``name`` per row so the agent cannot pick
        the display-only label and ship an un-prefixed ``model_name``.

        Response is effectively static per process lifetime (cached 5 min).

        Returns:
            The cached catalog including each model's availability flag.
        """
        return with_artificial_analysis_links(with_interview_default(with_zero_retention(get_catalog_cached())))

    @router.get(
        "/models/byok",
        response_model=ModelCatalogResponse,
        summary="List BYOK-eligible models (the caller authenticates each run with their own key)",
    )
    def list_byok_models() -> ModelCatalogResponse:
        """List every BYOK-offered provider's chat models, regardless of platform keys.

        In bring-your-own-key mode a run authenticates with the user's own
        provider key, so availability isn't gated on a platform key the way
        ``/models`` is. The client narrows this to the providers it has saved
        keys for. Unauthenticated and effectively static, like ``/models`` — the
        payload is just public registry data.

        Returns:
            The BYOK model catalog (all offered providers' chat models).
        """
        return get_byok_catalog_cached()

    @router.get(
        "/models/for-agent",
        response_model=AgentModelCatalogResponse,
        operation_id="list_models_for_agent",
        summary="List submit-ready model names (provider-prefixed) for the generalist agent",
        tags=["agent"],
    )
    def list_models_for_agent(
        current_user: AuthenticatedUserDep, query: str | None = None
    ) -> AgentModelCatalogResponse:
        """List models; copy each entry's ``name`` verbatim into ``model_name`` when submitting. Pass ``query`` to filter by name substring (case-insensitive, e.g. ``query="gpt-5.4"``) — the unfiltered catalog is ~18KB across 130 models, so always pass ``query`` when the user named a model.

        Args:
            current_user: The authenticated caller (required).
            query: Optional case-insensitive substring to filter model names.
                Common patterns: a provider slug (``"openai"``), a family
                (``"gpt-5"``), or an exact-ish model name
                (``"gpt-5.4-nano"``). Omit to fetch the full catalog.

        Returns:
            The (optionally filtered) catalog projected to
            ``{name, provider, …}`` rows. In local mode, the
            ``subscription/<cli>/<slug>`` models of the signed-in CLIs.
        """
        if settings.local_mode:
            entries = _subscription_agent_entries()
        else:
            entries = [_catalog_to_agent_entry(m) for m in get_catalog_cached().models]
        if query:
            needle = query.lower()
            entries = [entry for entry in entries if needle in entry.name.lower()]
        return AgentModelCatalogResponse(models=entries)

    @router.post(
        "/models/discover",
        response_model=DiscoverModelsResponse,
        summary="Probe an OpenAI-compatible endpoint for its model list",
        tags=["agent"],
    )
    def discover_models(payload: DiscoverModelsRequest, current_user: AuthenticatedUserDep) -> DiscoverModelsResponse:
        """Probe an OpenAI-compatible endpoint for its model list.

        Tries ``{base_url}/v1/models`` then ``{base_url}/models``. Never
        raises: on any failure ``models`` is empty and ``error`` describes
        the reason.

        Args:
            payload: The endpoint URL plus optional bearer token.
            current_user: The authenticated caller (required; the probe is an
                outbound request on the operator's network).

        Returns:
            Discovered model ids (clipped) with truncation flag, or an
            error message when the endpoint is unreachable.
        """
        return discover_models_at_endpoint(payload.base_url, payload.api_key)

    @router.post(
        "/models/call-holds",
        response_model=CallHoldsResponse,
        summary="Price the reservation one call of each model takes",
    )
    def model_call_holds(payload: CallHoldsRequest, current_user: AuthenticatedUserDep) -> CallHoldsResponse:
        """Return the charged cents one call of each model reserves before it is sent.

        A spending limit under this hold plus the boxes' holds refuses the run
        at its first model call, so the wizard holds Continue until the limit
        covers both.

        Args:
            payload: The run's model roles and their token sources.
            current_user: The authenticated caller (required; each price is an
                outbound catalog read).

        Returns:
            One hold per requested model, ``cents`` null when it is unpriced.
        """
        now = time.monotonic()
        holds: list[CallHold] = []
        with make_client() as client:
            for model in payload.models:
                key = (model.name, model.token_source)
                with hold_lock:
                    cached = hold_cache.get(key)
                if cached is None or now - cached[0] > _CALL_HOLD_TTL_SECONDS:
                    cached = (now, call_hold_cents(model.name, model.token_source, client=client))
                    with hold_lock:
                        hold_cache[key] = cached
                cents = cached[1]
                holds.append(
                    CallHold(
                        name=model.name,
                        token_source=model.token_source,
                        cents=None if cents is None else float(cents),
                    )
                )
        return CallHoldsResponse(holds=holds, box_hold_cents=float(box_hold_cents()))

    return router

"""Catalog the Skynet-keyed hosted MCP connectors and meter each of their tool calls.

Each connector is a hosted MCP server, keyed by its host. The parent gateway holds
Skynet's provider key and reaches the server through ``McpToolsBroker``; the box
gets only a scoped route token. Every tool call reserves and settles a fixed
per-call price in the run budget before and after the provider sees it.

:func:`relay_request` is the one parent path for a repository's ``[services]`` and
role ``.mcp.json`` http calls: it fills each ``${NAME}`` placeholder with the
user's secret or, for a managed binding, Skynet's key for that host, and removes
every filled value from the answer.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode, urlsplit

from ..config import settings
from .credential_safety import credential_fragments, redact_secret_bytes
from .mcp_broker import ToolCall
from .operation_pricing import ChargePolicy, operation_quote
from .runtime import BudgetRuntime, PaidResult

CONNECTOR_PRICE_VERSION = "env-connectors-2026-11-06"
CONNECTOR_COST_KIND = "tool"
# Payload key listing the catalog connectors, by host, a run asks for.
CONNECTORS_KEY = "env_connectors"
# Payload key carrying each connector's scoped route; only the parent gateway writes it.
CONNECTOR_ROUTES_KEY = "_skynet_connector_routes"
# A secret binding: the user's own value (BYOK), or Skynet's key for the host.
USER_BINDING = "user"
MANAGED_BINDING = "managed"
_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class Connector:
    """Describe one hosted MCP connector, its parent-held key and its per-call prices."""

    name: str
    url: str
    key_setting: str
    key_env: str
    # Allowed tool names and the provider USD charged for one call; a call settles at this ceiling.
    tools: Mapping[str, Decimal]
    price_source: str
    key_header: str | None = None
    key_prefix: str = ""
    key_query: str | None = None
    # Optional non-secret settings sent as query parameters: settings field -> parameter name.
    extra_query: Mapping[str, str] = field(default_factory=dict)

    @property
    def host(self) -> str:
        """Return the connector's host, the catalog key."""
        return urlsplit(self.url).hostname or ""


CONNECTORS: Mapping[str, Connector] = {
    connector.host: connector
    for connector in (
        Connector(
            name="maps",
            url="https://mapstools.googleapis.com/mcp",
            key_setting="google_maps_api_key",
            key_env="GOOGLE_MAPS_API_KEY",
            key_header="X-Goog-Api-Key",
            # Pro SKU ($14 per 1000) bounds search and routes; weather is always Essentials ($7 per 1000).
            tools={
                "search_places": Decimal("0.014"),
                "compute_routes": Decimal("0.014"),
                "lookup_weather": Decimal("0.007"),
            },
            price_source="https://developers.google.com/maps/ai/grounding-lite",
        ),
        Connector(
            name="web_search",
            url="https://mcp.firecrawl.dev/v2/mcp",
            key_setting="firecrawl_api_key",
            key_env="FIRECRAWL_API_KEY",
            key_header="Authorization",
            key_prefix="Bearer ",
            # Pay-as-you-go credits cost up to $0.005: a search uses 2 credits, a scrape 1.
            tools={
                "firecrawl_search": Decimal("0.01"),
                "firecrawl_scrape": Decimal("0.005"),
            },
            price_source="https://www.firecrawl.dev/pricing",
        ),
        Connector(
            name="browser",
            url="https://mcp.browserbase.com/mcp",
            key_setting="browserbase_api_key",
            key_env="BROWSERBASE_API_KEY",
            # The hosted server reads its key from the query string; the broker redacts query values.
            key_query="browserbaseApiKey",
            extra_query={"browserbase_project_id": "browserbaseProjectId"},
            # Browser hours cost $0.12; each step bounds about five minutes of session time.
            tools={
                "start": Decimal("0.02"),
                "navigate": Decimal("0.01"),
                "act": Decimal("0.01"),
                "observe": Decimal("0.01"),
                "extract": Decimal("0.01"),
                "end": Decimal(0),
            },
            price_source="https://www.browserbase.com/pricing",
        ),
    )
}


def _connector_key(connector: Connector) -> str | None:
    """Read Skynet's provider key for a connector from config.

    Args:
        connector: Catalog entry naming its settings field.

    Returns:
        The key, or None when config does not hold one.
    """
    secret = getattr(settings, connector.key_setting, None)
    value = secret.get_secret_value() if secret is not None else ""
    return value.strip() or None


def offered_connectors() -> tuple[str, ...]:
    """Return the hosts of the catalog connectors whose provider key is configured."""
    return tuple(host for host, connector in CONNECTORS.items() if _connector_key(connector))


def require_connectors(hosts: Any) -> tuple[Connector, ...]:
    """Check a requested connector list against the catalog and the configured keys.

    Args:
        hosts: Connector hosts from the run's tool list.

    Returns:
        The distinct requested catalog entries, in request order.

    Raises:
        ValueError: When the list is malformed, names a host outside the catalog,
            or a connector has no key in config. The message names the host.
    """
    if not isinstance(hosts, list) or not all(isinstance(host, str) for host in hosts):
        raise ValueError("The connector list must be a list of connector hosts.")
    unknown = sorted({host for host in hosts if host not in CONNECTORS})
    if unknown:
        raise ValueError(f"Unknown connector: {', '.join(unknown)}.")
    selected = tuple(CONNECTORS[host] for host in dict.fromkeys(hosts))
    missing = [connector for connector in selected if _connector_key(connector) is None]
    if missing:
        details = ", ".join(f"{connector.host} ({connector.key_env})" for connector in missing)
        raise ValueError(f"These connectors have no key in config: {details}.")
    return selected


def connector_endpoint(connector: Connector) -> tuple[str, dict[str, str]]:
    """Build the parent-only endpoint and credential headers for one connector.

    Args:
        connector: A configured catalog entry.

    Returns:
        The endpoint URL and the headers that carry the provider key.

    Raises:
        ValueError: When config does not hold the connector's key.
    """
    key = _connector_key(connector)
    if key is None:
        raise ValueError(f"The {connector.host} connector has no key in config ({connector.key_env}).")
    headers = {connector.key_header: connector.key_prefix + key} if connector.key_header else {}
    query = {connector.key_query: key} if connector.key_query else {}
    for setting, parameter in connector.extra_query.items():
        value = getattr(settings, setting, None)
        if value:
            query[parameter] = str(value)
    url = f"{connector.url}?{urlencode(query)}" if query else connector.url
    return url, headers


def tool_meter(
    runtime: BudgetRuntime, connector: Connector
) -> Callable[[str, Mapping[str, Any], ToolCall], dict[str, Any]]:
    """Bind per-call budget metering for one connector's broker.

    Args:
        runtime: The run budget that pays for each call.
        connector: Catalog entry whose per-call prices apply.

    Returns:
        A hook that reserves a call's price, runs the call and settles the price.
    """

    def meter(tool: str, arguments: Mapping[str, Any], call: ToolCall) -> dict[str, Any]:
        """Reserve, dispatch and settle one tool call in the run budget.

        Args:
            tool: Allowed tool name.
            arguments: The guest's tool arguments, bound into the quote fingerprint.
            call: The physical provider call; its `sent` flag marks the remote request.

        Returns:
            The provider's tool result.

        Raises:
            ValueError: When the tool has no catalog price.
        """
        if tool not in connector.tools:
            raise ValueError("The requested MCP tool is outside the selected roster.")
        price = connector.tools[tool]
        evidence = {
            "provider": connector.name,
            "tool": tool,
            "provider_usd": str(price),
            "source": connector.price_source,
            "price_version": CONNECTOR_PRICE_VERSION,
        }
        policy = ChargePolicy("managed_model")
        quote = operation_quote(
            {"connector": connector.name, "tool": tool, "arguments": dict(arguments)}, price, policy, evidence
        )
        failures: list[BaseException] = []

        def dispatch() -> PaidResult[dict[str, Any] | None]:
            """Run the call once and settle its price unless it failed before the send.

            Returns:
                The settled price with the tool result or failure, or a refusal when the
                call failed before the broker sent the tool request.
            """
            try:
                return PaidResult(call(), price, evidence)
            except BaseException as error:
                failures.append(error)
                if not call.sent:
                    return PaidResult(None, None, {**evidence, "failure": type(error).__name__}, refused=True)
                # The provider may have run the tool (for example a read timeout or an
                # interrupt), and connectors have no usage reconciler, so the ceiling settles.
                failure = type(error).__name__ if isinstance(error, Exception) else "interrupted"
                return PaidResult(None, price, {**evidence, "failure": failure})

        result = runtime.execute(
            quote,
            policy,
            dispatch,
            operation_key=f"tool:{connector.name}:{uuid.uuid4().hex}",
            cost_kind=CONNECTOR_COST_KIND,
            role=connector.name,
        )
        if failures:
            raise failures[0]
        return result

    return meter


def managed_key_host(host: str) -> bool:
    """Tell whether Skynet holds a configured key for a host (E50).

    Args:
        host: A url host, any case, with or without a trailing dot.

    Returns:
        True when the host is in the catalog and config holds its key.
    """
    connector = CONNECTORS.get(host.rstrip(".").lower())
    return connector is not None and _connector_key(connector) is not None


def resolve_secrets(
    names: tuple[str, ...] | list[str],
    host: str,
    user_secrets: Mapping[str, str],
    bindings: Mapping[str, str],
) -> dict[str, str]:
    """Pick the value for each secret one request to ``host`` uses (E50).

    A missing binding means ``user``. A managed binding takes Skynet's key for
    the host, and only a catalog host has one.

    Args:
        names: The secret names the request's headers use.
        host: The request's host.
        user_secrets: The run's own secret values, by name.
        bindings: ``user`` or ``managed`` for each secret name.

    Returns:
        The value for each name.

    Raises:
        ValueError: When a user secret has no value, or a managed secret's host
            is not in the catalog or has no key in config. The message names the
            secret, never a value.
    """
    connector = CONNECTORS.get(host.rstrip(".").lower())
    values: dict[str, str] = {}
    for name in names:
        if bindings.get(name, USER_BINDING) == MANAGED_BINDING:
            if connector is None:
                raise ValueError(
                    f"${{{name}}} uses a Skynet key, but {host} is not in the connector catalog."
                    " Bind it to your own key instead."
                )
            key = _connector_key(connector)
            if key is None:
                raise ValueError(f"${{{name}}} uses a Skynet key, but Skynet has no key for {host}.")
            values[name] = key
        elif user_secrets.get(name):
            values[name] = user_secrets[name]
        else:
            raise ValueError(f"${{{name}}} has no value. Add the secret {name} to the run.")
    return values


def _fill(text: str, values: Mapping[str, str]) -> str:
    """Replace each ``${NAME}`` in a header value with its resolved value.

    Args:
        text: A header value from the repository.
        values: The resolved value for each name.

    Returns:
        The filled value.
    """
    return _PLACEHOLDER.sub(lambda match: values[match.group(1)], text)


_FREE_METHODS = frozenset({"initialize", "ping", "tools/list"})


def _managed_call(body: bytes) -> tuple[str, dict[str, Any]] | None:
    """Return the tool and arguments of a managed ``tools/call``, or None for a free request.

    A managed request carries Skynet's key, so it fails closed: anything the
    meter cannot read (a batch, bad UTF-8, an unknown method) is refused, not
    sent unmetered.

    Args:
        body: The request body the box sent.

    Returns:
        The tool and its arguments for a ``tools/call``, or None for
        ``initialize``, ``ping``, ``tools/list`` and notifications.

    Raises:
        ValueError: When the body is not one JSON-RPC request of those kinds.
    """
    refusal = "A call with a Skynet key must be one JSON-RPC tools/call, tools/list, initialize, ping or notification."
    try:
        message = json.loads(body)
    except (ValueError, RecursionError) as error:
        raise ValueError(refusal) from error
    method = message.get("method") if isinstance(message, dict) else None
    if not isinstance(method, str):
        # The relay's callers turn ValueError into the refusal the box sees.
        raise ValueError(refusal)  # noqa: TRY004
    if method in _FREE_METHODS or method.startswith("notifications/"):
        return None
    params = message.get("params")
    if method != "tools/call" or not isinstance(params, dict) or not isinstance(params.get("name"), str):
        raise ValueError(refusal)
    arguments = params.get("arguments")
    return params["name"], arguments if isinstance(arguments, dict) else {}


def _same_endpoint(url: str, connector: Connector) -> bool:
    """Return whether a url is the connector's own endpoint, so a managed key reaches no other path.

    Args:
        url: The declared url.
        connector: The catalog entry for the url's host.

    Returns:
        True when the scheme, host and path match, with no query.
    """
    given, own = urlsplit(url), urlsplit(connector.url)
    return (
        given.scheme == own.scheme
        and (given.hostname or "").rstrip(".").lower() == connector.host
        and given.port in (None, 443)
        and given.path.rstrip("/") == own.path.rstrip("/")
        and not given.query
    )


def _escaped_forms(fragment: str) -> set[str]:
    """Return the JSON-escaped forms in which a server may echo a secret fragment.

    Args:
        fragment: One secret fragment.

    Returns:
        The JSON-escaped, ``\\/``-escaped and HTML-safe JSON forms.
    """
    escaped = json.dumps(fragment)[1:-1]
    html_safe = escaped.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    return {escaped, escaped.replace("/", "\\/"), html_safe, html_safe.replace("/", "\\/")}


def relay_request(
    *,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    user_secrets: Mapping[str, str],
    bindings: Mapping[str, str],
    secret_hosts: Mapping[str, str],
    send: Callable[[str, dict[str, str], bytes], bytes],
    runtime: BudgetRuntime | None = None,
) -> bytes:
    """Send one ``[services]`` or MCP http request with its secrets filled in (E49-E53).

    The box holds only ``${NAME}`` placeholders. This parent-side step fills
    them, refuses a secret outside the one host it may go to, meters a managed
    ``tools/call`` with :func:`tool_meter` (a BYOK call has no Skynet fee), and
    removes every filled value from the answer, so a server that echoes its
    headers cannot hand a key back to the box.

    Args:
        url: The declared https url.
        headers: The declared headers, with ``${NAME}`` placeholders.
        body: The request body from the box.
        user_secrets: The run's own secret values, by name.
        bindings: ``user`` or ``managed`` for each secret name.
        secret_hosts: The one host each secret may go to, from
            :attr:`RepoConstraints.secret_hosts`.
        send: Sends the filled request and returns the answer body.
        runtime: The run budget; required when a managed secret is used.

    Returns:
        The answer body without any filled secret value.

    Raises:
        ValueError: When a secret has no value, goes to a host it is not bound
            to, or a managed call is not to the connector's own endpoint, is a
            request the meter cannot read, has no budget, or names a tool with
            no price.
    """
    host = (urlsplit(url).hostname or "").rstrip(".").lower()
    names = tuple(dict.fromkeys(name for value in headers.values() for name in _PLACEHOLDER.findall(value)))
    for name in names:
        if secret_hosts.get(name) != host:
            raise ValueError(f"${{{name}}} may not go to {host}.")
    values = resolve_secrets(names, host, user_secrets, bindings)
    filled = {key: _fill(value, values) for key, value in headers.items()}
    fragments = {fragment for value in values.values() for fragment in credential_fragments(value)}
    fragments |= {
        fragment for key, value in filled.items() if value != headers[key] for fragment in credential_fragments(value)
    }
    # A server can echo a value JSON-escaped (Go writes & as \u0026, PHP writes / as \/), so hide those forms too.
    fragments |= {form for fragment in fragments for form in _escaped_forms(fragment)}
    ordered = sorted(fragments, key=len, reverse=True)
    managed = any(bindings.get(name) == MANAGED_BINDING for name in names)
    if managed and not _same_endpoint(url, CONNECTORS[host]):
        raise ValueError(f"A Skynet key for {host} may go only to {CONNECTORS[host].url}.")
    call = _managed_call(body) if managed else None
    if call is None:
        answer = send(url, filled, body)
    else:
        if runtime is None:
            raise ValueError("A call with a Skynet key needs the run budget.")

        def run(progress: ToolCall) -> dict[str, Any]:
            """Mark the call sent, then send it.

            Args:
                progress: The metered call's progress record.

            Returns:
                The raw answer, wrapped for the meter.
            """
            progress.sent = True
            return {"body": send(url, filled, body)}

        tool, arguments = call
        answer = tool_meter(runtime, CONNECTORS[host])(tool, arguments, ToolCall(run))["body"]
    return redact_secret_bytes(answer, ordered)

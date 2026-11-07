"""Catalog the Skynet-keyed hosted MCP connectors and meter each of their tool calls.

Each connector is a hosted MCP server. The parent gateway holds Skynet's provider
key and reaches the server through ``McpToolsBroker``; the box gets only a scoped
route token. Every tool call reserves and settles a fixed per-call price in the
run budget before and after the provider sees it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from ..config import settings
from .operation_pricing import ChargePolicy, operation_quote
from .runtime import BudgetRuntime, PaidResult
from .signals import BudgetReached

CONNECTOR_PRICE_VERSION = "env-connectors-2026-11-06"
CONNECTOR_COST_KIND = "tool"
# Payload key listing the catalog connectors a run asks for.
CONNECTORS_KEY = "env_connectors"
# Payload key carrying each connector's scoped route; only the parent gateway writes it.
CONNECTOR_ROUTES_KEY = "_skynet_connector_routes"


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


CONNECTORS: Mapping[str, Connector] = {
    connector.name: connector
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
    """Return the catalog connectors whose provider key is configured."""
    return tuple(name for name, connector in CONNECTORS.items() if _connector_key(connector))


def require_connectors(names: Any) -> tuple[Connector, ...]:
    """Check a requested connector list against the catalog and the configured keys.

    Args:
        names: Connector names from the run's tool list.

    Returns:
        The distinct requested catalog entries, in request order.

    Raises:
        ValueError: When the list is malformed, names an unknown connector, or a
            connector has no key in config. The message names the connector.
    """
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise ValueError("The connector list must be a list of connector names.")
    unknown = sorted({name for name in names if name not in CONNECTORS})
    if unknown:
        raise ValueError(f"Unknown connector: {', '.join(unknown)}.")
    selected = tuple(CONNECTORS[name] for name in dict.fromkeys(names))
    missing = [connector for connector in selected if _connector_key(connector) is None]
    if missing:
        details = ", ".join(f"{connector.name} ({connector.key_env})" for connector in missing)
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
        raise ValueError(f"The {connector.name} connector has no key in config ({connector.key_env}).")
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
) -> Callable[[str, Mapping[str, Any], Callable[[], dict[str, Any]]], dict[str, Any]]:
    """Bind per-call budget metering for one connector's broker.

    Args:
        runtime: The run budget that pays for each call.
        connector: Catalog entry whose per-call prices apply.

    Returns:
        A hook that reserves a call's price, runs the call and settles the price.
    """

    def meter(tool: str, arguments: Mapping[str, Any], call: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """Reserve, dispatch and settle one tool call in the run budget.

        Args:
            tool: Allowed tool name.
            arguments: The guest's tool arguments, bound into the quote fingerprint.
            call: The physical provider call.

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
            """Run the call once; a call that raises releases its coverage.

            Returns:
                The settled price with the tool result, or a refusal on failure.
            """
            try:
                return PaidResult(call(), price, evidence)
            except (Exception, BudgetReached) as error:
                # Hosted providers do not bill a request that fails, so the hold is released.
                failures.append(error)
                return PaidResult(None, None, {**evidence, "failure": type(error).__name__}, refused=True)

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

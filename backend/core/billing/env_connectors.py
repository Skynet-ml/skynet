"""Catalog the Skynet-keyed hosted MCP connectors and meter each of their tool calls.

Each connector is a hosted MCP server, keyed by its host. The parent gateway holds
Skynet's provider key and reaches the server through ``McpToolsBroker``; the box
gets only a scoped route token. Every tool call reserves and settles a fixed
per-call price in the run budget before and after the provider sees it.

:func:`relay_request` is the one parent path for a repository's ``[services]`` and
role ``.mcp.json`` http calls: it fills each ``${NAME}`` placeholder with the
user's secret or, for a managed binding, Skynet's key for that host, and removes
every filled value from the answer. With a recorder it also writes the call into
the run record (E28), with every secret it could carry removed (E52 rule 3).
"""

from __future__ import annotations

import base64
import json
import re
import uuid
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, quote_plus, urlencode, urlsplit

from ..config import settings
from .credential_safety import REDACTED_CREDENTIAL, credential_fragments, redact_secret_bytes
from .mcp_broker import ToolCall
from .operation_pricing import ChargePolicy, operation_quote
from .runtime import BudgetRuntime, PaidResult

if TYPE_CHECKING:
    # scoped_relay imports this module, so the recorder type is for type checks only.
    from ..worker.scoped_relay import CallRecorder

CONNECTOR_PRICE_VERSION = "env-connectors-2026-11-06"
# The base64 form of a 1-3 character secret is 2-4 common characters; hiding it would mangle unrelated answer text.
_MIN_ENCODED_FRAGMENT = 4
_PERCENT_ESCAPE = re.compile(r"%[0-9A-F]{2}")
CONNECTOR_COST_KIND = "tool"
# Payload key listing the owner's OAuth MCP connections, by name, that a run asks for.
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


def escaped_forms(fragment: str) -> set[str]:
    """Return the JSON-escaped forms in which a server may echo a secret fragment.

    Args:
        fragment: One secret fragment.

    Returns:
        The JSON-escaped, ``\\/``-escaped and HTML-safe JSON forms, with
        lower- and upper-case hex in the ``\\u`` escapes.
    """
    escaped = json.dumps(fragment)[1:-1]
    # .NET System.Text.Json writes upper-case hex (\u003C), Python and Go write lower-case;
    # each case pair for < and > is listed, so an echo that mixes them is also hidden.
    forms = {escaped} | {
        escaped.replace("&", "\\u0026").replace("<", lt).replace(">", gt)
        for lt in ("\\u003c", "\\u003C")
        for gt in ("\\u003e", "\\u003E")
    }
    return forms | {form.replace("/", "\\/") for form in forms}


def _either_case(hex_digits: str) -> bytes:
    """Return a regex that matches hex digits in lower or upper case.

    Args:
        hex_digits: Lower-case hex digits.

    Returns:
        The regex bytes.
    """
    return "".join(f"[{d}{d.upper()}]" if d.isalpha() else d for d in hex_digits).encode()


def json_escape_pattern(fragment: str) -> re.Pattern[bytes] | None:
    """Return a regex for every JSON string form in which a server may echo a fragment.

    A JSON encoder may write any character as ``\\uXXXX`` with lower- or
    upper-case hex (.NET writes ``+`` as ``\\u002B``) and ``/`` as ``\\/``,
    and it may mix forms in one string. A list of literal forms cannot hold
    every mix, so each character gets its own set of choices.

    Args:
        fragment: One secret fragment.

    Returns:
        The compiled regex, or None for a fragment shorter than
        ``_MIN_ENCODED_FRAGMENT``.
    """
    if len(fragment) < _MIN_ENCODED_FRAGMENT:
        return None
    parts: list[bytes] = []
    for char in fragment:
        choices = {re.escape(char.encode()), re.escape(json.dumps(char)[1:-1].encode())}
        if char == "/":
            choices.add(re.escape(b"\\/"))
        units = char.encode("utf-16-be")
        choices.add(b"".join(re.escape(b"\\u") + _either_case(units[i : i + 2].hex()) for i in range(0, len(units), 2)))
        parts.append(b"(?:" + b"|".join(sorted(choices)) + b")")
    return re.compile(b"".join(parts))


def encoded_forms(fragment: str) -> set[str]:
    """Return the re-encoded forms in which a server may echo a secret fragment.

    Args:
        fragment: One secret fragment.

    Returns:
        The standard and URL-safe base64 forms, each with and without ``=``
        padding, and the ``quote`` and ``quote_plus`` forms with upper- and
        lower-case escapes; no forms for a fragment shorter than
        ``_MIN_ENCODED_FRAGMENT``.
    """
    if len(fragment) < _MIN_ENCODED_FRAGMENT:
        return set()
    raw = fragment.encode()
    forms: set[str] = set()
    for encoded in (quote(fragment, safe=""), quote_plus(fragment, safe="")):
        forms |= {encoded, _PERCENT_ESCAPE.sub(lambda match: match.group().lower(), encoded)}
    for encoded in (base64.b64encode(raw).decode(), base64.urlsafe_b64encode(raw).decode()):
        forms |= {encoded, encoded.rstrip("=")}
    return forms


def host_allowed(host: str, allowed: Collection[str]) -> bool:
    """Tell whether a run lets a secret go to ``host`` (E52).

    Args:
        host: The lower-case host of the url that receives the secret.
        allowed: The secret's ``allowed_hosts``: exact hosts, or ``*.suffix``,
            which matches each host below the suffix but not the suffix itself.

    Returns:
        Whether one entry matches; ``False`` for an empty list.
    """
    host = host.rstrip(".").lower()
    return any(host == entry or (entry.startswith("*.") and host.endswith(entry[1:])) for entry in allowed)


def relay_request(
    *,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    user_secrets: Mapping[str, str],
    bindings: Mapping[str, str],
    secret_hosts: Mapping[str, str],
    allowed_hosts: Mapping[str, Collection[str]],
    send: Callable[[str, dict[str, str], bytes], bytes],
    runtime: BudgetRuntime | None = None,
    recorder: CallRecorder | None = None,
    kind: str = "host",
    target: str = "",
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
        allowed_hosts: Each user secret's ``allowed_hosts`` from the run. The
            repository's files cannot widen them; a managed secret goes only
            to its catalog endpoint.
        send: Sends the filled request and returns the answer body.
        runtime: The run budget; required when a managed secret is used.
        recorder: Writes this call into the run record: ``ok``, ``failed``
            or ``refused`` with its reason. Every user secret value and every
            filled header value goes to the store as a secret.
        kind: The record kind: ``host`` for a ``[services]`` call, ``mcp``
            for a role ``.mcp.json`` call.
        target: The record target, such as ``server@host/tool``; the url's
            host when empty.

    Returns:
        The answer body without any filled secret value.

    Raises:
        ValueError: When a secret has no value, goes to a host it is not bound
            to, or a managed call is not to the connector's own endpoint, is a
            request the meter cannot read, has no budget, or names a tool with
            no price.
    """
    state = _RelayState(set(user_secrets.values()))
    started = recorder.start() if recorder is not None else None
    if not target:
        try:
            target = (urlsplit(url).hostname or "").rstrip(".").lower()
        except ValueError:  # a malformed url such as "http://[::1/x" is refused by _relay, and still recorded
            target = ""
    try:
        answer = _relay(url, headers, body, user_secrets, bindings, secret_hosts, allowed_hosts, send, runtime, state)
    except BaseException as error:  # a worker stop or cancel must still leave the call in the record
        if recorder is not None and started is not None:
            recorder.record(
                started,
                kind=kind,
                target=target,
                # Only a refusal raised by the relay's own checks is "refused".
                status="failed" if state.sent or not isinstance(error, Exception) else "refused",
                request=body,
                failure_reason=f"{type(error).__name__}: {error}",
                # A tool that failed after the send still settles its price.
                cost_usd=state.cost_usd if state.sent else None,
                secrets=state.secrets,
            )
        raise
    if recorder is not None and started is not None:
        recorder.record(
            started,
            kind=kind,
            target=target,
            status="ok",
            request=body,
            response=answer,
            cost_usd=state.cost_usd,
            secrets=state.secrets,
        )
    return answer


@dataclass
class _RelayState:
    """Collect what the run record needs from one relay call, even when it fails."""

    secrets: set[str]
    sent: bool = False
    cost_usd: float | None = None


def _relay(
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    user_secrets: Mapping[str, str],
    bindings: Mapping[str, str],
    secret_hosts: Mapping[str, str],
    allowed_hosts: Mapping[str, Collection[str]],
    send: Callable[[str, dict[str, str], bytes], bytes],
    runtime: BudgetRuntime | None,
    state: _RelayState,
) -> bytes:
    """Fill, check, meter and send one relay call; see :func:`relay_request`.

    Args:
        url: The declared https url.
        headers: The declared headers, with ``${NAME}`` placeholders.
        body: The request body from the box.
        user_secrets: The run's own secret values, by name.
        bindings: ``user`` or ``managed`` for each secret name.
        secret_hosts: The one host each secret may go to.
        allowed_hosts: Each user secret's ``allowed_hosts`` from the run.
        send: Sends the filled request and returns the answer body.
        runtime: The run budget; required when a managed secret is used.
        state: Gets the filled secrets, whether the call was sent, and its cost.

    Returns:
        The answer body without any filled secret value.
    """
    host = (urlsplit(url).hostname or "").rstrip(".").lower()
    names = tuple(dict.fromkeys(name for value in headers.values() for name in _PLACEHOLDER.findall(value)))
    for name in names:
        if secret_hosts.get(name) != host:
            raise ValueError(f"${{{name}}} may not go to {host}.")
        if bindings.get(name) != MANAGED_BINDING and not host_allowed(host, allowed_hosts.get(name, ())):
            raise ValueError(f"${{{name}}} may not go to {host}: the run does not allow that host.")
    values = resolve_secrets(names, host, user_secrets, bindings)
    filled = {key: _fill(value, values) for key, value in headers.items()}
    fragments = {fragment for value in values.values() for fragment in credential_fragments(value)}
    fragments |= {
        fragment for key, value in filled.items() if value != headers[key] for fragment in credential_fragments(value)
    }
    # A server can echo a value base64- or percent-encoded, so hide those forms too. Only a value
    # encoded on its own is caught: inside a longer base64 text its form depends on the byte offset.
    fragments |= {form for fragment in fragments for form in encoded_forms(fragment)}
    # A server can echo a value JSON-escaped (Go writes & as \u0026, PHP writes / as \/), so hide those forms too.
    base = set(fragments)
    fragments |= {form for fragment in fragments for form in escaped_forms(fragment)}
    state.secrets |= set(values.values()) | fragments
    ordered = sorted(fragments, key=len, reverse=True)
    managed = any(bindings.get(name) == MANAGED_BINDING for name in names)
    if managed and not _same_endpoint(url, CONNECTORS[host]):
        raise ValueError(f"A Skynet key for {host} may go only to {CONNECTORS[host].url}.")
    call = _managed_call(body) if managed else None
    if call is None:
        state.sent = True
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
            progress.sent = state.sent = True
            return {"body": send(url, filled, body)}

        tool, arguments = call
        if tool in CONNECTORS[host].tools:
            policy = ChargePolicy("managed_model")
            state.cost_usd = float(policy.convert(CONNECTORS[host].tools[tool]).total * policy.cent_usd)
        answer = tool_meter(runtime, CONNECTORS[host])(tool, arguments, ToolCall(run))["body"]
    redacted = redact_secret_bytes(answer, ordered)
    for fragment in sorted(base, key=len, reverse=True):
        pattern = json_escape_pattern(fragment)
        if pattern is not None:
            redacted = pattern.sub(REDACTED_CREDENTIAL.encode(), redacted)
    return redacted

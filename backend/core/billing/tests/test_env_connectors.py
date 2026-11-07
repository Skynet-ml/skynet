"""Check the hosted MCP connector catalog, its key gating and its per-call metering."""

from __future__ import annotations

import json
import logging
import socket
import threading
import urllib.request
from collections.abc import Callable, Iterator
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from core.billing import env_connectors
from core.billing.budgets import BudgetFencedError, BudgetInsufficientError, BudgetService
from core.billing.env_connectors import (
    CONNECTORS,
    connector_endpoint,
    host_allowed,
    offered_connectors,
    relay_request,
    tool_meter,
)
from core.billing.mcp_broker import McpToolsBroker, ToolCall
from core.billing.runtime import BudgetRuntime
from core.billing.signals import BudgetReached
from core.config import settings
from core.storage.models import Base, BillingCustomerModel, ExecutionOperationModel, ExecutionUsageEvidenceModel

MAPS_KEY = "maps-parent-only-key"
FIRECRAWL_KEY = "fc-parent-only-key"
BROWSERBASE_KEY = "bb-parent-only-key"
MAPS = "mapstools.googleapis.com"
FIRECRAWL = "mcp.firecrawl.dev"
BROWSERBASE = "mcp.browserbase.com"


@pytest.fixture(autouse=True)
def _keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure every connector key and pin the usage markup to 1.0."""
    monkeypatch.setattr(settings, "usage_markup", 1.0)
    monkeypatch.setattr(settings, "google_maps_api_key", SecretStr(MAPS_KEY))
    monkeypatch.setattr(settings, "firecrawl_api_key", SecretStr(FIRECRAWL_KEY))
    monkeypatch.setattr(settings, "browserbase_api_key", SecretStr(BROWSERBASE_KEY))
    monkeypatch.setattr(settings, "browserbase_project_id", "project-fixture")


def _sent(run: Callable[[], dict]) -> ToolCall:
    """Wrap a fake provider call that fails or returns after the tool request went out.

    Args:
        run: Fake provider behavior.

    Returns:
        A metered call whose ``sent`` flag is set before ``run`` starts.
    """

    def send(call: ToolCall) -> dict:
        """Mark the request as sent, then run the fake provider."""
        call.sent = True
        return run()

    return ToolCall(send)


def _unsent(run: Callable[[], dict]) -> ToolCall:
    """Wrap a fake call that stops before the tool request goes out.

    Args:
        run: Fake pre-send behavior.

    Returns:
        A metered call whose ``sent`` flag stays false.
    """
    return ToolCall(lambda _call: run())


def _runtime(tmp_path: Path, budget_cents: int) -> Iterator[BudgetRuntime]:
    """Open a funded setup-phase run budget on a private SQLite ledger.

    Args:
        tmp_path: Directory for the ledger file.
        budget_cents: Run budget size.

    Yields:
        The runtime that pays for connector calls.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'ledger.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(username="alice", stripe_customer_id="fixture", balance_cents=50, grant_remaining=0)
        )
        session.commit()
    ledger = BudgetService(engine=engine)
    budget = ledger.create("alice", budget_cents, idempotency_key="fixture")
    yield BudgetRuntime(ledger, username="alice", budget_id=budget.id, generation=0, phase="setup")
    engine.dispose()


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[BudgetRuntime]:
    """Provide a run budget with room for several connector calls."""
    yield from _runtime(tmp_path, 20)


@pytest.fixture
def tight_runtime(tmp_path: Path) -> Iterator[BudgetRuntime]:
    """Provide a run budget too small for one Maps call."""
    yield from _runtime(tmp_path, 1)


def test_catalog_lists_three_hosted_connectors_with_prices() -> None:
    """Give each connector an HTTPS MCP URL, a real settings field, allowed tools and prices."""
    assert set(CONNECTORS) == {MAPS, FIRECRAWL, BROWSERBASE}
    for host, connector in CONNECTORS.items():
        assert connector.host == host
        assert urlsplit(connector.url).scheme == "https"
        assert connector.key_setting in type(settings).model_fields
        assert connector.tools
        assert all(isinstance(price, Decimal) and price >= 0 for price in connector.tools.values())
        assert connector.price_source.startswith("https://")
    assert CONNECTORS[MAPS].url == "https://mapstools.googleapis.com/mcp"
    assert CONNECTORS[FIRECRAWL].key_setting == "firecrawl_api_key"


def test_connector_without_key_is_not_offered(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide a connector whose provider key is missing or blank."""
    assert offered_connectors() == (MAPS, FIRECRAWL, BROWSERBASE)
    monkeypatch.setattr(settings, "google_maps_api_key", None)
    monkeypatch.setattr(settings, "browserbase_api_key", SecretStr("  "))
    assert offered_connectors() == (FIRECRAWL,)


def test_endpoint_places_each_key_where_its_provider_reads_it() -> None:
    """Send Maps and Firecrawl keys as headers and the Browserbase key in the query."""
    assert connector_endpoint(CONNECTORS[MAPS]) == (
        "https://mapstools.googleapis.com/mcp",
        {"X-Goog-Api-Key": MAPS_KEY},
    )
    assert connector_endpoint(CONNECTORS[FIRECRAWL]) == (
        "https://mcp.firecrawl.dev/v2/mcp",
        {"Authorization": f"Bearer {FIRECRAWL_KEY}"},
    )
    url, headers = connector_endpoint(CONNECTORS[BROWSERBASE])
    assert headers == {}
    assert parse_qs(urlsplit(url).query) == {
        "browserbaseApiKey": [BROWSERBASE_KEY],
        "browserbaseProjectId": ["project-fixture"],
    }


def test_each_call_reserves_and_settles_its_catalog_price(runtime: BudgetRuntime) -> None:
    """Settle the catalog price as a tool cost in the run budget, once per call."""
    meter = tool_meter(runtime, CONNECTORS[MAPS])
    calls = []

    def call() -> dict:
        """Record that coverage exists before the provider sees the call."""
        calls.append(runtime.service.get(runtime.budget_id, "alice").reserved_cents)
        return {"content": [{"type": "text", "text": "Cafe"}]}

    assert meter("search_places", {"textQuery": "cafe"}, _sent(call)) == {"content": [{"type": "text", "text": "Cafe"}]}
    meter("lookup_weather", {"location": "Tel Aviv"}, _sent(call))

    snapshot = runtime.service.get(runtime.budget_id, "alice")
    assert calls[0] > 0
    assert snapshot.reserved_cents == 0
    assert snapshot.setup_spent_cents == Decimal("2.1")
    with Session(runtime.service._engine) as session:
        operations = session.scalars(select(ExecutionOperationModel)).all()
    assert [(operation.cost_kind, operation.role) for operation in operations] == [("tool", "maps")] * 2


def test_call_outside_budget_is_refused_before_provider(tight_runtime: BudgetRuntime) -> None:
    """Refuse a call whose price does not fit, without reaching the provider."""
    meter = tool_meter(tight_runtime, CONNECTORS[MAPS])
    called = []
    with pytest.raises(BudgetInsufficientError):
        meter("search_places", {"textQuery": "cafe"}, _sent(lambda: called.append(1) or {}))
    assert called == []


def test_tool_outside_catalog_is_refused_without_charge(runtime: BudgetRuntime) -> None:
    """Refuse an unlisted tool name before any reservation."""
    meter = tool_meter(runtime, CONNECTORS[MAPS])
    with pytest.raises(ValueError, match="outside the selected roster"):
        meter("delete_everything", {}, _sent(lambda: pytest.fail("The provider must not be called.")))
    assert runtime.service.get(runtime.budget_id, "alice").setup_spent_cents == 0


def test_failed_call_after_send_settles_its_price_and_raises(runtime: BudgetRuntime) -> None:
    """Charge the catalog price for a call that fails after the request may have gone out."""
    meter = tool_meter(runtime, CONNECTORS[FIRECRAWL])

    def fail() -> dict:
        """Simulate a provider read timeout after the request was sent."""
        raise ValueError("The selected MCP endpoint could not complete the tool request.")

    with pytest.raises(ValueError, match="could not complete"):
        meter("firecrawl_search", {"query": "x"}, _sent(fail))
    snapshot = runtime.service.get(runtime.budget_id, "alice")
    assert snapshot.reserved_cents == 0
    assert snapshot.setup_spent_cents == Decimal(1)
    with Session(runtime.service._engine) as session:
        operation = session.scalars(select(ExecutionOperationModel)).one()
        evidence = session.scalars(select(ExecutionUsageEvidenceModel)).all()
    assert operation.state == "settled"
    assert any("ValueError" in repr(row.__dict__) for row in evidence)


@pytest.mark.parametrize(
    "error",
    [
        BudgetFencedError("The execution generation is obsolete."),
        BudgetReached("The approved budget has been reached."),
    ],
)
def test_admission_stop_before_send_releases_its_hold(runtime: BudgetRuntime, error: BaseException) -> None:
    """Release the reservation when admission stops the call before the provider request."""
    meter = tool_meter(runtime, CONNECTORS[FIRECRAWL])

    def stop() -> dict:
        """Simulate the broker's check_admission refusing before it sends the request."""
        raise error

    with pytest.raises(type(error)):
        meter("firecrawl_search", {"query": "x"}, _unsent(stop))
    snapshot = runtime.service.get(runtime.budget_id, "alice")
    assert snapshot.reserved_cents == 0
    assert snapshot.setup_spent_cents == 0


def test_connection_failure_before_send_releases_its_hold(runtime: BudgetRuntime) -> None:
    """Charge nothing when the real broker cannot reach the provider before the tool call."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    broker = McpToolsBroker(
        f"http://127.0.0.1:{port}/mcp",
        check_admission=runtime.check_admission,
        allow_private=True,
        timeout_seconds=3,
        meter=tool_meter(runtime, CONNECTORS[FIRECRAWL]),
    )
    broker._tool_names = {"firecrawl_search"}
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "firecrawl_search"}}
    with pytest.raises(ValueError, match="could not complete"):
        broker.dispatch(request)
    snapshot = runtime.service.get(runtime.budget_id, "alice")
    assert snapshot.reserved_cents == 0
    assert snapshot.setup_spent_cents == 0
    with Session(runtime.service._engine) as session:
        assert session.scalars(select(ExecutionOperationModel)).one().state != "settled"


@pytest.mark.parametrize(("wrap", "spent"), [(_sent, Decimal(1)), (_unsent, Decimal(0))])
def test_interrupt_settles_after_send_and_releases_before(
    runtime: BudgetRuntime, wrap: Callable[[Callable[[], dict]], ToolCall], spent: Decimal
) -> None:
    """Settle an interrupted call that went out and release one that did not; never leave it pending."""
    meter = tool_meter(runtime, CONNECTORS[FIRECRAWL])

    def interrupt() -> dict:
        """Simulate an operator interrupt during the call."""
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        meter("firecrawl_search", {"query": "x"}, wrap(interrupt))
    snapshot = runtime.service.get(runtime.budget_id, "alice")
    assert snapshot.reserved_cents == 0
    assert snapshot.setup_spent_cents == spent
    with Session(runtime.service._engine) as session:
        operation = session.scalars(select(ExecutionOperationModel)).one()
        evidence = repr([row.__dict__ for row in session.scalars(select(ExecutionUsageEvidenceModel)).all()])
    assert operation.state != "pending"
    assert ("interrupted" in evidence) == bool(spent)


def test_keys_stay_out_of_quotes_errors_and_logs(
    runtime: BudgetRuntime, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Keep every connector key out of the run record, the errors and the logs."""
    caplog.set_level(logging.DEBUG)
    for connector in CONNECTORS.values():
        tool = next(iter(connector.tools))
        tool_meter(runtime, connector)(tool, {"q": "x"}, _sent(lambda: {"content": []}))
    monkeypatch.setattr(settings, "google_maps_api_key", None)
    with pytest.raises(ValueError) as refused:
        connector_endpoint(CONNECTORS[MAPS])
    with Session(runtime.service._engine) as session:
        rows = [
            *session.scalars(select(ExecutionOperationModel)).all(),
            *session.scalars(select(ExecutionUsageEvidenceModel)).all(),
        ]
        record = repr([row.__dict__ for row in rows])
    assert "maps" in record
    surfaces = record + str(refused.value) + caplog.text + repr(env_connectors.CONNECTORS)
    for secret in (MAPS_KEY, FIRECRAWL_KEY, BROWSERBASE_KEY):
        assert secret not in surfaces


class _EchoHandler(BaseHTTPRequestHandler):
    """Answer every POST with the headers and body it received, like a hostile echo server."""

    def do_POST(self) -> None:
        """Echo the request back as JSON."""
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        answer = json.dumps({"headers": dict(self.headers), "body": body.decode()}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    def log_message(self, *_args: object) -> None:
        """Keep the test output quiet."""


@pytest.fixture
def echo() -> Iterator[Callable[[str, dict[str, str], bytes], bytes]]:
    """Run a local echo server and give a sender that delivers every relay request to it."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EchoHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def send(_url: str, headers: dict[str, str], body: bytes) -> bytes:
        """Post the filled request to the echo server instead of the real host."""
        request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/", data=body, headers=headers)
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.read()

    yield send
    server.shutdown()
    server.server_close()


def _operations(runtime: BudgetRuntime) -> list[ExecutionOperationModel]:
    """Return the metered operations in the run budget.

    Args:
        runtime: The run budget.

    Returns:
        Every recorded operation.
    """
    with Session(runtime.service._engine) as session:
        return list(session.scalars(select(ExecutionOperationModel)).all())


def test_relay_fills_user_secret_and_removes_it_from_the_echo(
    runtime: BudgetRuntime, echo: Callable[[str, dict[str, str], bytes], bytes]
) -> None:
    """Send the user's key to its one host, remove it from the answer, and charge no Skynet fee."""
    secret = "user-judge-key-123"
    body = json.dumps({"method": "tools/call", "params": {"name": "search", "arguments": {}}}).encode()
    answer = relay_request(
        url="https://api.judge.example/v1",
        headers={"Authorization": "Bearer ${JUDGE_KEY}", "X-Plain": "fixed"},
        body=body,
        user_secrets={"JUDGE_KEY": secret},
        bindings={},
        secret_hosts={"JUDGE_KEY": "api.judge.example"},
        allowed_hosts={"JUDGE_KEY": ["api.judge.example"]},
        send=echo,
        runtime=runtime,
    )
    echoed = json.loads(answer)
    assert secret not in answer.decode()
    assert echoed["headers"]["Authorization"] == "[REDACTED]"
    assert echoed["headers"]["X-Plain"] == "fixed"
    assert _operations(runtime) == []


def test_relay_fills_managed_key_meters_the_call_and_removes_the_key(
    runtime: BudgetRuntime, echo: Callable[[str, dict[str, str], bytes], bytes]
) -> None:
    """Use Skynet's Maps key for a managed binding, meter the tool call, and hide the key (CFCA)."""
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "search_places", "arguments": {}}}
    ).encode()
    answer = relay_request(
        url="https://mapstools.googleapis.com/mcp",
        headers={"Authorization": "Bearer ${GOOGLE_MAPS_KEY}"},
        body=body,
        user_secrets={},
        bindings={"GOOGLE_MAPS_KEY": "managed"},
        secret_hosts={"GOOGLE_MAPS_KEY": MAPS},
        allowed_hosts={},
        send=echo,
        runtime=runtime,
    )
    assert MAPS_KEY not in answer.decode()
    assert json.loads(answer)["headers"]["Authorization"] == "[REDACTED]"
    assert [(operation.cost_kind, operation.role) for operation in _operations(runtime)] == [("tool", "maps")]
    assert runtime.service.get(runtime.budget_id, "alice").setup_spent_cents == Decimal("1.4")


def test_relay_does_not_meter_a_managed_tools_list(
    runtime: BudgetRuntime, echo: Callable[[str, dict[str, str], bytes], bytes]
) -> None:
    """Charge only for tool calls, not for the session's list and setup requests."""
    relay_request(
        url="https://mapstools.googleapis.com/mcp",
        headers={"X-Goog-Api-Key": "${GOOGLE_MAPS_KEY}"},
        body=b'{"method": "tools/list"}',
        user_secrets={},
        bindings={"GOOGLE_MAPS_KEY": "managed"},
        secret_hosts={"GOOGLE_MAPS_KEY": MAPS},
        allowed_hosts={},
        send=echo,
        runtime=runtime,
    )
    assert _operations(runtime) == []


JUDGE = {"KEY": ["api.judge.example"]}


@pytest.mark.parametrize(
    ("host", "allowed", "expected"),
    [
        ("api.judge.example", ["api.judge.example"], True),
        ("eu.api.judge.example", ["*.judge.example"], True),
        ("judge.example", ["*.judge.example"], False),
        ("evil-judge.example", ["*.judge.example"], False),
        ("api.judge.example", [], False),
        ("API.Judge.Example.", ["api.judge.example"], True),
    ],
)
def test_host_allowed_matches_an_exact_host_or_a_suffix(host: str, allowed: list[str], expected: bool) -> None:
    """Match an exact host, or a host below a ``*.`` suffix but not the suffix itself; an empty list allows none."""
    assert host_allowed(host, allowed) is expected


@pytest.mark.parametrize(
    ("url", "user_secrets", "bindings", "secret_hosts", "allowed_hosts", "message"),
    [
        ("https://api.judge.example/v1", {}, {}, {"KEY": "api.judge.example"}, JUDGE, r"\$\{KEY\} has no value"),
        (
            "https://api.judge.example/v1",
            {},
            {"KEY": "managed"},
            {"KEY": "api.judge.example"},
            {},
            "not in the connector",
        ),
        (
            "https://evil.example/v1",
            {"KEY": "v"},
            {},
            {"KEY": "api.judge.example"},
            JUDGE,
            "may not go to evil.example",
        ),
        ("https://evil.example/v1", {"KEY": "v"}, {}, {"KEY": "evil.example"}, JUDGE, "does not allow that host"),
        ("https://api.judge.example/v1", {"KEY": "v"}, {}, {"KEY": "api.judge.example"}, {}, "does not allow"),
        (f"https://{MAPS}/mcp", {}, {"KEY": "managed"}, {"KEY": MAPS}, {}, "Skynet has no key"),
    ],
)
def test_relay_refuses_before_sending(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    user_secrets: dict[str, str],
    bindings: dict[str, str],
    secret_hosts: dict[str, str],
    allowed_hosts: dict[str, list[str]],
    message: str,
) -> None:
    """Refuse a missing value, a managed key outside the catalog or with no key, a wrong host, and a host the run does not allow."""
    monkeypatch.setattr(settings, "google_maps_api_key", None)
    with pytest.raises(ValueError, match=message):
        relay_request(
            url=url,
            headers={"Authorization": "Bearer ${KEY}"},
            body=b"{}",
            user_secrets=user_secrets,
            bindings=bindings,
            secret_hosts=secret_hosts,
            allowed_hosts=allowed_hosts,
            send=lambda *_args: pytest.fail("The relay must not send."),
        )


def test_relay_needs_a_budget_for_a_managed_tool_call() -> None:
    """Refuse a managed tool call that no run budget pays for."""
    with pytest.raises(ValueError, match="run budget"):
        relay_request(
            url=f"https://{MAPS}/mcp",
            headers={"X-Goog-Api-Key": "${GOOGLE_MAPS_KEY}"},
            body=b'{"method": "tools/call", "params": {"name": "search_places"}}',
            user_secrets={},
            bindings={"GOOGLE_MAPS_KEY": "managed"},
            secret_hosts={"GOOGLE_MAPS_KEY": MAPS},
            allowed_hosts={},
            send=lambda *_args: pytest.fail("The relay must not send."),
        )


@pytest.mark.parametrize(
    ("url", "body", "message"),
    [
        (f"https://{MAPS}/mcp", b'[{"method": "tools/call", "params": {"name": "search_places"}}]', "one JSON-RPC"),
        (f"https://{MAPS}/mcp", b'{"method": "tools/call", "params": {"name": "s\xff"}}', "one JSON-RPC"),
        (f"https://{MAPS}/mcp", b'{"method": "resources/read", "params": {}}', "one JSON-RPC"),
        (f"https://{MAPS}/mcp", b'{"method": "tools/call", "params": {}}', "one JSON-RPC"),
        (f"https://{MAPS}/v1/other", b'{"method": "tools/call", "params": {"name": "search_places"}}', "may go only"),
        (f"https://{MAPS}/mcp?alt=1", b'{"method": "tools/call", "params": {"name": "search_places"}}', "may go only"),
    ],
)
def test_relay_refuses_a_managed_call_that_the_meter_cannot_read(
    runtime: BudgetRuntime, url: str, body: bytes, message: str
) -> None:
    """Fail closed on a batch, bad UTF-8, an unknown method, no tool name, or another endpoint on the host."""
    with pytest.raises(ValueError, match=message):
        relay_request(
            url=url,
            headers={"X-Goog-Api-Key": "${GOOGLE_MAPS_KEY}"},
            body=body,
            user_secrets={},
            bindings={"GOOGLE_MAPS_KEY": "managed"},
            secret_hosts={"GOOGLE_MAPS_KEY": MAPS},
            allowed_hosts={},
            send=lambda *_args: pytest.fail("The relay must not send."),
            runtime=runtime,
        )
    assert _operations(runtime) == []


def test_relay_removes_a_json_escaped_echo_of_the_secret() -> None:
    """Hide the secret when the server echoes it with JSON escapes such as \\/ and \\u0026."""
    secret = "user/key&part<1>"
    escaped = json.dumps(secret)[1:-1]
    forms = [
        escaped,
        escaped.replace("/", "\\/"),
        escaped.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e").replace("/", "\\/"),
    ]
    answer = relay_request(
        url="https://api.judge.example/v1",
        headers={"Authorization": "Bearer ${JUDGE_KEY}"},
        body=b"{}",
        user_secrets={"JUDGE_KEY": secret},
        bindings={},
        secret_hosts={"JUDGE_KEY": "api.judge.example"},
        allowed_hosts={"JUDGE_KEY": ["api.judge.example"]},
        send=lambda *_args: ('{"echo": ["' + '", "'.join(forms) + '"]}').encode(),
    ).decode()
    assert all(form not in answer for form in forms)
    assert json.loads(answer)["echo"] == ["[REDACTED]"] * 3


def test_relay_refuses_a_shared_file_that_sends_a_user_key_to_another_host() -> None:
    """Refuse ``${GOOGLE_MAPS_KEY}`` that a shared file points at a host the user's run does not allow (E52)."""
    with pytest.raises(ValueError, match=r"may not go to collector\.example"):
        relay_request(
            url="https://collector.example/mcp",
            headers={"X-Goog-Api-Key": "${GOOGLE_MAPS_KEY}"},
            body=b"{}",
            user_secrets={"GOOGLE_MAPS_KEY": "user-maps-key"},
            bindings={},
            secret_hosts={"GOOGLE_MAPS_KEY": "collector.example"},
            allowed_hosts={"GOOGLE_MAPS_KEY": [MAPS]},
            send=lambda *_args: pytest.fail("The relay must not send."),
        )

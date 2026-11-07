"""Check the hosted MCP connector catalog, its key gating and its per-call metering."""

from __future__ import annotations

import logging
import socket
from collections.abc import Callable, Iterator
from decimal import Decimal
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
    offered_connectors,
    require_connectors,
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
    assert set(CONNECTORS) == {"maps", "web_search", "browser"}
    for connector in CONNECTORS.values():
        assert urlsplit(connector.url).scheme == "https"
        assert connector.key_setting in type(settings).model_fields
        assert connector.tools
        assert all(isinstance(price, Decimal) and price >= 0 for price in connector.tools.values())
        assert connector.price_source.startswith("https://")
    assert CONNECTORS["maps"].url == "https://mapstools.googleapis.com/mcp"
    assert CONNECTORS["web_search"].key_setting == "firecrawl_api_key"


def test_connector_without_key_is_not_offered_and_setup_names_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide a keyless connector and name it, with its env setting, in the setup error."""
    assert offered_connectors() == ("maps", "web_search", "browser")
    monkeypatch.setattr(settings, "google_maps_api_key", None)
    monkeypatch.setattr(settings, "browserbase_api_key", SecretStr("  "))
    assert offered_connectors() == ("web_search",)
    with pytest.raises(ValueError, match=r"maps \(GOOGLE_MAPS_API_KEY\), browser \(BROWSERBASE_API_KEY\)"):
        require_connectors(["maps", "web_search", "browser"])
    assert [connector.name for connector in require_connectors(["web_search", "web_search"])] == ["web_search"]


@pytest.mark.parametrize("names", [["unknown"], "maps", [3]])
def test_unknown_or_malformed_connector_list_is_refused(names: object) -> None:
    """Refuse names outside the catalog and lists that are not plain names."""
    with pytest.raises(ValueError, match="connector"):
        require_connectors(names)


def test_endpoint_places_each_key_where_its_provider_reads_it() -> None:
    """Send Maps and Firecrawl keys as headers and the Browserbase key in the query."""
    assert connector_endpoint(CONNECTORS["maps"]) == (
        "https://mapstools.googleapis.com/mcp",
        {"X-Goog-Api-Key": MAPS_KEY},
    )
    assert connector_endpoint(CONNECTORS["web_search"]) == (
        "https://mcp.firecrawl.dev/v2/mcp",
        {"Authorization": f"Bearer {FIRECRAWL_KEY}"},
    )
    url, headers = connector_endpoint(CONNECTORS["browser"])
    assert headers == {}
    assert parse_qs(urlsplit(url).query) == {
        "browserbaseApiKey": [BROWSERBASE_KEY],
        "browserbaseProjectId": ["project-fixture"],
    }


def test_each_call_reserves_and_settles_its_catalog_price(runtime: BudgetRuntime) -> None:
    """Settle the catalog price as a tool cost in the run budget, once per call."""
    meter = tool_meter(runtime, CONNECTORS["maps"])
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
    meter = tool_meter(tight_runtime, CONNECTORS["maps"])
    called = []
    with pytest.raises(BudgetInsufficientError):
        meter("search_places", {"textQuery": "cafe"}, _sent(lambda: called.append(1) or {}))
    assert called == []


def test_tool_outside_catalog_is_refused_without_charge(runtime: BudgetRuntime) -> None:
    """Refuse an unlisted tool name before any reservation."""
    meter = tool_meter(runtime, CONNECTORS["maps"])
    with pytest.raises(ValueError, match="outside the selected roster"):
        meter("delete_everything", {}, _sent(lambda: pytest.fail("The provider must not be called.")))
    assert runtime.service.get(runtime.budget_id, "alice").setup_spent_cents == 0


def test_failed_call_after_send_settles_its_price_and_raises(runtime: BudgetRuntime) -> None:
    """Charge the catalog price for a call that fails after the request may have gone out."""
    meter = tool_meter(runtime, CONNECTORS["web_search"])

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
    meter = tool_meter(runtime, CONNECTORS["web_search"])

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
        meter=tool_meter(runtime, CONNECTORS["web_search"]),
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
    meter = tool_meter(runtime, CONNECTORS["web_search"])

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
        connector_endpoint(CONNECTORS["maps"])
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

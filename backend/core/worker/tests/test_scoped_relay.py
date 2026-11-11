"""Test how long the parent relay waits for each scoped capability."""

from __future__ import annotations

import base64
import http.server
import json
import threading
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from core.billing.budgets import BudgetService
from core.billing.model_gateway import MCP_RELAY_ROUTE_KEY
from core.billing.protected_execution import run_call_recorder
from core.service_gateway.optimization.blackbox.repo_constraints import McpServer, RoleEnvironment, ServiceSpec
from core.service_gateway.optimization.blackbox.repo_scorer import SETUP_ALLOWANCE_SECONDS
from core.service_gateway.optimization.blackbox.repo_services import box_mcp_config
from core.storage.models import Base
from core.storage.run_call_store import SECRET_MASK, PostgresRunCallStore, strip_secrets
from core.worker import scoped_relay

_ROUTE = {"url": "http://127.0.0.1:9000/v1", "token": "evaluator"}


class _Client:
    """Record the timeout of each relay request and answer it."""

    timeouts: list[float] = []

    def __init__(self, *, trust_env: bool, timeout: float) -> None:
        """Keep the timeout the relay chose.

        Args:
            trust_env: Whether the client reads proxy settings.
            timeout: Seconds the relay waits for the upstream answer.
        """
        assert trust_env is False
        type(self).timeouts.append(timeout)

    def __enter__(self) -> _Client:
        """Open the fake client."""
        return self

    def __exit__(self, *_: object) -> None:
        """Close the fake client."""

    def request(self, method: str, url: str, **_: Any) -> httpx.Response:
        """Answer every request with an empty JSON object.

        Args:
            method: HTTP method.
            url: Upstream URL.

        Returns:
            A successful JSON response.
        """
        return httpx.Response(200, json={}, request=httpx.Request(method, url))


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> type[_Client]:
    """Replace the relay's HTTP client with the recording fake.

    Args:
        monkeypatch: Pytest patcher.

    Returns:
        The fake client class with its recorded timeouts.
    """
    _Client.timeouts = []
    monkeypatch.setattr(scoped_relay.httpx, "Client", _Client)
    return _Client


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ({"kind": "repo"}, 630.0 + SETUP_ALLOWANCE_SECONDS),
        ({"kind": "prompt"}, 630.0),
        (None, 630.0),
    ],
)
def test_the_evaluator_waits_for_the_repository_setup(
    client: type[_Client], target: dict[str, Any] | None, expected: float
) -> None:
    """A repository score may prepare the version and open stage boxes before it answers."""
    dispatch = scoped_relay.model_forwarder({"_skynet_evaluator_route": _ROUTE, "target": target})

    result = dispatch("evaluator", "/v1/_evaluator", {}, {})

    assert result.status == 200
    assert client.timeouts == [expected]


def test_the_evaluator_token_reaches_only_its_paths(client: type[_Client]) -> None:
    """The evaluator capability keeps its budget path and refuses model paths."""
    dispatch = scoped_relay.model_forwarder({"_skynet_evaluator_route": _ROUTE, "target": {"kind": "repo"}})

    dispatch("evaluator", "/v1/_budget/state", {}, {})
    with pytest.raises(ValueError, match="unknown metered model capability"):
        dispatch("evaluator", "/v1/messages", {}, {})

    assert client.timeouts == [600]


class _BrokenSink:
    """Fail every write, like a lost database connection."""

    def record_call(self, *_args: object) -> int:
        """Fail the write."""
        raise RuntimeError("database is down")


class _Sink:
    """Keep every recorded call in memory."""

    def __init__(self) -> None:
        """Start with no calls."""
        self.calls: list[tuple[str, dict[str, Any], list[str]]] = []

    def record_call(self, optimization_id: str, call: dict[str, Any], secrets: list[str]) -> int:
        """Keep one call.

        Args:
            optimization_id: Owning job id.
            call: The call fields.
            secrets: The secrets to remove.

        Returns:
            The call's sequence number.
        """
        self.calls.append((optimization_id, call, secrets))
        return len(self.calls)


def test_recorder_sends_the_call_fields_and_drops_empty_secrets() -> None:
    """Write one call with its candidate, role, timing and only the non-empty secrets."""
    sink = _Sink()
    recorder = scoped_relay.CallRecorder(sink, "job-1", "cand-1", "judge")
    recorder.record(
        recorder.start(),
        kind="host",
        target="api.example",
        status="ok",
        request=b"{}",
        response=b"\xff",
        secrets=["key", "", "key"],
    )
    [(job, call, secrets)] = sink.calls
    assert job == "job-1"
    assert (call["candidate_id"], call["role"], call["kind"], call["status"]) == ("cand-1", "judge", "host", "ok")
    assert call["started_at"].tzinfo is not None
    assert call["duration_ms"] >= 0
    assert (call["request"], call["response"]) == ("{}", "\ufffd")
    assert "key" in secrets
    assert "" not in secrets


def test_recorder_keeps_a_duration_another_process_measured() -> None:
    """Store the egress proxy's own duration instead of the time since ``start``."""
    sink = _Sink()
    recorder = scoped_relay.CallRecorder(sink, "job-1", "", "")
    recorder.record(recorder.start(), kind="host", target="pypi.org", status="ok", duration_ms=4321)
    [(_, call, _)] = sink.calls
    assert call["duration_ms"] == 4321


def test_recorder_bind_keeps_the_run_and_changes_the_candidate() -> None:
    """Bind a recorder to another candidate and role of the same run."""
    recorder = scoped_relay.CallRecorder(_Sink(), "job-1", "cand-1", "judge").bind(candidate_id="cand-2")
    assert (recorder.optimization_id, recorder.candidate_id, recorder.role) == ("job-1", "cand-2", "judge")
    assert recorder.bind(role="proposer").role == "proposer"


def test_recorder_store_failure_never_fails_the_call(caplog: pytest.LogCaptureFixture) -> None:
    """Log a store failure without its text, and return normally."""
    recorder = scoped_relay.CallRecorder(_BrokenSink(), "job-1", "cand-1", "judge")
    recorder.record(recorder.start(), kind="host", target="api.example", status="ok", secrets=["key"])
    assert "Could not record a host call" in caplog.text
    assert "database is down" not in caplog.text


def _gateway(phase: str, engine: object) -> Any:
    """Build a gateway stand-in with one budget phase and a real budget service.

    Args:
        phase: Budget attribution phase.
        engine: Ledger engine the budget service is bound to.

    Returns:
        An object with the ``runtime.phase`` and ``runtime.service`` fields.
    """
    return SimpleNamespace(runtime=SimpleNamespace(phase=phase, service=BudgetService(engine=engine)))


def test_run_phase_records_into_the_ledger_engine_for_the_job() -> None:
    """Bind the run recorder to the job id and the ledger's public engine."""
    engine = object()
    recorder = run_call_recorder(_gateway("run", engine), "job-7")

    assert recorder is not None
    assert isinstance(recorder.sink, PostgresRunCallStore)
    assert recorder.sink._engine is engine
    assert (recorder.optimization_id, recorder.candidate_id, recorder.role) == ("job-7", "", "")


def test_no_run_recorder_for_setup_work() -> None:
    """Return no recorder outside the run phase, where owner_id is no job id."""
    assert run_call_recorder(_gateway("setup", object()), "setup-7") is None


def test_budget_service_exposes_its_engine() -> None:
    """Read the ledger engine through the public read-only property."""
    engine = object()
    service = BudgetService(engine=engine)

    assert service.engine is engine
    with pytest.raises(AttributeError):
        service.engine = object()  # type: ignore[misc]


def test_secret_forms_stay_bounded_and_decode_only_basic_credentials() -> None:
    """Add at most forty forms for a secret, and decode only a ``user:password`` token."""
    plain = scoped_relay._secret_forms("sk-live-abcdef123456")
    assert {"sk-live-abcdef123456", "c2stbGl2ZS1hYmNkZWYxMjM0NTY"} <= plain

    azure = scoped_relay._secret_forms("Zm9v+YmFy/YmF6PQ==")
    assert {"Zm9v%2BYmFy/YmF6PQ%3D%3D", "Zm9v%2BYmFy%2FYmF6PQ%3D%3D"} <= azure
    assert len(azure) <= 40

    basic = scoped_relay._secret_forms("Basic c3ZjOnB3LXNlY3JldC05")
    assert {"c3ZjOnB3LXNlY3JldC05", "svc:pw-secret-9", "pw-secret-9"} <= basic
    assert "Basic+c3ZjOnB3LXNlY3JldC05" in basic
    assert len(basic) <= 40


def test_secret_forms_cover_php_and_go_escapes_of_each_derived_form() -> None:
    """Mask the PHP ``\\/`` and Go ``\\u0026`` escapes of base64 and Basic forms too."""
    azure = scoped_relay._secret_forms("Zm9v+YmFy/YmF6PQ==")
    assert {"Zm9v+YmFy\\/YmF6PQ==", "Zm9v%2BYmFy\\/YmF6PQ%3D%3D"} <= azure
    encoded = scoped_relay._secret_forms("ab/cd&ef?gh")
    assert "YWIvY2QmZWY/Z2g" in encoded
    assert "YWIvY2QmZWY\\/Z2g" in encoded
    assert {"ab/cd\\u0026ef?gh", "ab\\/cd\\u0026ef?gh", "ab%2Fcd%26ef%3Fgh"} <= encoded

    basic = scoped_relay._secret_forms("Basic dTpwYXNzL3dvcmQmeA==")
    assert {"u:pass/word&x", "pass/word&x"} <= basic
    assert "pass\\/word&x" in basic
    assert {"pass/word\\u0026x", "u:pass\\/word\\u0026x"} <= basic


def test_short_basic_password_is_still_masked() -> None:
    """Mask a decoded Basic password that is shorter than the derived-form floor."""
    token = "Basic " + base64.b64encode(b"admin:pa55wd").decode()
    forms = scoped_relay._secret_forms(token)

    assert {"admin:pa55wd", "pa55wd"} <= forms
    assert len(forms) <= 40
    assert "pa55wd" not in strip_secrets("echo pa55wd", sorted(forms))


def test_short_secret_adds_no_derived_form() -> None:
    """Keep derived forms of at least eight characters, so a short value masks no common text."""
    assert scoped_relay._secret_forms("1") == {"1"}
    assert "MQ" not in scoped_relay._secret_forms("1")


_MCP_ROUTE = {"url": "http://127.0.0.1:9000/v1", "token": "mcp-relay"}


@pytest.mark.parametrize(
    "path",
    ["/v1/mcp/", "/v1/mcp/../_sandbox", "/v1/mcp/a/b", "/v1/mcp/%2e%2e", "/v1/mcp/.", "/v1/mcp/a?b", "/v1/mcp/a#b"],
)
def test_the_mcp_route_refuses_a_path_that_leaves_one_server_name(client: type[_Client], path: str) -> None:
    """An MCP relay path must name exactly one server; anything else never leaves the parent.

    Args:
        client: The fake HTTP client.
        path: The path the box asks for.
    """
    dispatch = scoped_relay.model_forwarder({MCP_RELAY_ROUTE_KEY: _MCP_ROUTE})

    with pytest.raises(ValueError, match="outside the MCP relay"):
        dispatch(_MCP_ROUTE["token"], path, b"{}", {})

    assert client.timeouts == []


@pytest.mark.parametrize(
    "path",
    [
        "/v1/service/",
        "/v1/service//x",
        "/v1/service/../_sandbox",
        "/v1/service/%2e%2e/x",
        "/v1/service/echo/../other/",
        "/v1/service/echo/a/%2E%2E/b",
        "/v1/service/echo/%252e%252e/x",
        "/v1/service/echo/..\\x",
        "/v1/service/echo/..%5cx",
        "/v1/service/echo/..;/x",
        "/v1/service/echo/x/..%5c..%5cadmin",
        "/v1/service/echo/..%00/admin",
        "/v1/service/echo/a;b",
        "/v1/service/echo/..%3F",
        "/v1/service/echo/..%3Fx",
        "/v1/service/echo/..%253F/x",
        "/v1/service/echo/..%23",
        "/v1/service/echo/..%2523",
        "/v1/service/echo/x/..%3F/..%3F",
        "/v1/service/echo/%c0%ae%c0%ae/x",
        "/v1/service/echo/a%20b",
        "/v1/service/echo/%EF%BC%8E%EF%BC%8E/x",
        "/v1/service/echo/./a",
        "/v1/service/echo?x/",
        "/v1/service/echo#x/",
    ],
)
def test_the_service_route_refuses_a_path_that_leaves_one_service(client: type[_Client], path: str) -> None:
    """A service relay path must name one service and stay under it; anything else never leaves the parent.

    Args:
        client: The fake HTTP client.
        path: The path the box asks for.
    """
    dispatch = scoped_relay.model_forwarder({MCP_RELAY_ROUTE_KEY: _MCP_ROUTE})

    with pytest.raises(ValueError, match="outside the service relay"):
        dispatch(_MCP_ROUTE["token"], path, b"", {})

    assert client.timeouts == []


@pytest.mark.parametrize("path", ["/v1/service/echo/v2/a..b/item.json", "/v1/service/echo/item?q=a;b"])
def test_the_service_route_allows_a_plain_sub_path_and_any_query(path: str) -> None:
    """A deeper path of plain characters passes, and the allowlist does not read the query.

    Args:
        path: The path the box asks for.
    """
    assert scoped_relay._service_path_safe(path)


@pytest.mark.parametrize(
    ("base", "target", "under"),
    [
        ("http://h/api", "http://h/api", True),
        ("http://h/api", "http://h/api/x?q=1", True),
        ("http://h/api/", "http://h/api/x", True),
        ("http://h", "http://h/anything", True),
        ("http://h/api", "http://h/apix", False),
        ("http://h/api", "http://h/ap%69x", False),
        ("http://h/api", "http://h/", False),
    ],
)
def test_a_service_url_stays_under_its_declared_path_as_a_directory(base: str, target: str, under: bool) -> None:
    """Base ``/api`` allows ``/api`` and ``/api/x``, never a sibling such as ``/apix``.

    Args:
        base: The service's declared url.
        target: The url the relay would call.
        under: Whether the relay may call it.
    """
    assert scoped_relay._under(httpx.URL(target), httpx.URL(base)) is under


def test_mcp_and_model_tokens_reach_only_their_own_paths(client: type[_Client]) -> None:
    """The MCP token opens no model route, and a model token opens no MCP server or service.

    Args:
        client: The fake HTTP client.
    """
    dispatch = scoped_relay.model_forwarder({"_skynet_target_route": _ROUTE, MCP_RELAY_ROUTE_KEY: _MCP_ROUTE})

    with pytest.raises(ValueError, match="unknown metered model capability"):
        dispatch(_MCP_ROUTE["token"], "/v1/chat/completions", {"model": "fixture/text"}, {})
    with pytest.raises(ValueError, match="unknown metered model capability"):
        dispatch(_ROUTE["token"], "/v1/mcp/search", b"{}", {})
    with pytest.raises(ValueError, match="unknown metered model capability"):
        dispatch(_ROUTE["token"], "/v1/service/echo/", b"", {})

    assert client.timeouts == []


_QUERY_SECRET = "query-secret-value-6161"


def test_a_proposer_service_gets_its_query_secret_and_the_box_never_sees_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """The relay adds the filled ``?key=``, drops the guest's own ``key``, and masks it in the answer and record (SKY-638).

    Args:
        monkeypatch: Sends the pinned request to an in-memory upstream.
    """
    seen: list[str] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        """Echo the query the upstream got, in a header and the body."""
        query = request.url.query.decode("ascii")
        seen.append(query)
        return httpx.Response(200, headers={"X-Query": query}, content=query.encode())

    monkeypatch.setattr(scoped_relay, "_pinned_address", lambda url, allow_private, *, label: "127.0.0.1")
    monkeypatch.setattr(scoped_relay, "_PinnedTransport", lambda url, pinned: httpx.MockTransport(upstream))
    relay = scoped_relay.McpRelay(
        "[proposer]",
        RoleEnvironment(services=("geo",)),
        secrets={"MAPS_KEY": _QUERY_SECRET},
        secret_hosts={"MAPS_KEY": "maps.example.com"},
        allowed_hosts={"MAPS_KEY": ("maps.example.com",)},
        services={
            "geo": ServiceSpec(secrets=("MAPS_KEY",), url="https://maps.example.com/v1", query={"key": "${MAPS_KEY}"})
        },
    )
    sink = _Sink()

    answer = relay.service(
        "geo", "lookup?q=1&KEY=guest", "GET", b"", {}, scoped_relay.CallRecorder(sink, "job-1", "", "")
    )

    assert seen == [f"q=1&key={_QUERY_SECRET}"]
    assert answer.status == 200
    assert _QUERY_SECRET not in answer.body.decode() + str(answer.headers)
    assert "[REDACTED]" in answer.body.decode()
    [(_, call, secrets)] = sink.calls
    assert _QUERY_SECRET in secrets
    assert _QUERY_SECRET not in strip_secrets(json.dumps(call, default=str), secrets)


def test_box_mcp_urls_name_each_server_and_hold_no_secret() -> None:
    """Each ``.mcp.json`` url the box gets maps back to its server name and carries no real url or key."""
    role = RoleEnvironment(
        mcp=".mcp.json",
        mcp_servers=(
            McpServer(name="search", url="https://mcp.example.test/x", headers={"Authorization": "Bearer ${KEY}"}),
            McpServer(name="a b/c", url="https://other.example.test/y", headers={}),
        ),
    )

    config = box_mcp_config(role, "http://127.0.0.1:4100/v1")

    names = {name: scoped_relay._mcp_name(urlsplit(entry["url"]).path) for name, entry in config["mcpServers"].items()}
    assert names == {"search": "search", "a b/c": "a b/c"}
    assert "example.test" not in str(config)
    assert "KEY" not in str(config)


# A route token with both quote kinds, so its JSON-escaped and repr forms differ from the raw value (SKY-640).
_ROUTE_TOKEN = "route\"token'0123456789abcdef"


class _BodyEcho:
    """Serve an upstream that answers each POST with the body it got."""

    def __init__(self) -> None:
        """Start the server on a free loopback port."""

        class Handler(http.server.BaseHTTPRequestHandler):
            """Echo the request body."""

            def do_POST(self) -> None:
                """Answer with the request body."""
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_: object) -> None:
                """Keep the test output quiet."""

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        """Stop the server."""
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def body_echo(monkeypatch: pytest.MonkeyPatch) -> Iterator[_BodyEcho]:
    """Start the echo server and let the relay reach its test host on loopback.

    Args:
        monkeypatch: Pins each test host to 127.0.0.1, as DNS would pin a public host.

    Yields:
        The running server.
    """
    monkeypatch.setattr(scoped_relay, "_pinned_address", lambda url, allow_private, *, label: "127.0.0.1")
    server = _BodyEcho()
    yield server
    server.close()


def _run_call_store() -> PostgresRunCallStore:
    """Return the real run call store over a fresh in-memory SQLite schema.

    Returns:
        The store.
    """
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return PostgresRunCallStore(engine)


@pytest.mark.parametrize("kind", ["mcp", "host"])
@pytest.mark.parametrize("outcome", ["ok", "failed", "refused"])
def test_the_proposer_route_token_leaks_into_no_mcp_or_service_record(
    body_echo: _BodyEcho, kind: str, outcome: str
) -> None:
    """The proposer box's route token is masked, raw, JSON-escaped and quoted, in each record (SKY-640)."""
    store = _run_call_store()
    recorder = scoped_relay.CallRecorder(store, "opt-1", "", "")
    # A closed port makes the call fail after the relay sends it.
    port = body_echo.port if outcome == "ok" else 1
    relay = scoped_relay.McpRelay(
        "[proposer]",
        RoleEnvironment(
            mcp=".mcp.json",
            mcp_servers=(McpServer(name="search", url=f"http://mcp.example.test:{port}/mcp", headers={}),),
            services=("echo",),
        ),
        services={"echo": ServiceSpec(url=f"http://svc.example.test:{port}/api")},
        record_secrets=lambda: (_ROUTE_TOKEN,),
    )
    name = _ROUTE_TOKEN if outcome == "refused" else ("search" if kind == "mcp" else "echo")
    params = {"name": "lookup", "arguments": {"key": _ROUTE_TOKEN}}
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}).encode()
    headers = {"content-type": "application/json"}

    if kind == "mcp":
        answer = relay.dispatch(name, "POST", body, headers, recorder)
    else:
        answer = relay.service(name, "", "POST", body, headers, recorder)

    assert answer.status == {"ok": 200, "failed": 502, "refused": 403 if kind == "mcp" else 404}[outcome]
    [stored] = store.list_calls("opt-1", scoped_relay.NO_CANDIDATE_ID)
    assert (stored["kind"], stored["status"]) == (kind, outcome)
    leaked = {_ROUTE_TOKEN, json.dumps(_ROUTE_TOKEN)[1:-1], repr(_ROUTE_TOKEN)[1:-1]}
    for field in ("target", "request", "response", "failure_reason"):
        assert not any(form in (stored[field] or "") for form in leaked), field
    assert SECRET_MASK in (stored["target"] if outcome == "refused" else stored["request"])

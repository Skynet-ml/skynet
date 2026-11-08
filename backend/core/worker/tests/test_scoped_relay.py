"""Test how long the parent relay waits for each scoped capability."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from core.billing.budgets import BudgetService
from core.billing.protected_execution import run_call_recorder
from core.service_gateway.optimization.blackbox.repo_scorer import SETUP_ALLOWANCE_SECONDS
from core.storage.run_call_store import PostgresRunCallStore
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


def test_short_secret_adds_no_derived_form() -> None:
    """Keep derived forms of at least eight characters, so a short value masks no common text."""
    assert scoped_relay._secret_forms("1") == {"1"}
    assert "MQ" not in scoped_relay._secret_forms("1")

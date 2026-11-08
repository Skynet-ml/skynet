"""Test how long the parent relay waits for each scoped capability."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from core.service_gateway.optimization.blackbox.repo_scorer import SETUP_ALLOWANCE_SECONDS
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
    assert secrets == ["key"]


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

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

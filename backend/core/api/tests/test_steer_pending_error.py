"""Test the semantic response for a full steer mailbox."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ...service_gateway.agents.steering import SteerStore
from ..routers import generalist_agent
from .conftest import bypass_auth


def test_pending_full_is_not_a_timed_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tell clients to queue instead of retrying after an arbitrary second.

    Args:
        monkeypatch: Replace the process mailbox with an isolated store.
    """
    store = SteerStore()
    monkeypatch.setattr(generalist_agent, "get_steer_store", lambda: store)
    app = FastAPI()
    app.include_router(generalist_agent.create_generalist_agent_router())
    bypass_auth(app)
    for index in range(10):
        store.post("alice", "turn", str(index))
    response = TestClient(app).post("/optimizations/agent-steer", json={"steer_key": "turn", "text": "queue this"})
    assert response.status_code == 429
    assert "Retry-After" not in response.headers
    assert "queue" in response.json()["detail"].lower()
    assert "second" not in response.json()["detail"].lower()

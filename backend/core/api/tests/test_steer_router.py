"""Test steering request validation, withdrawal responses, and pending caps."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ...service_gateway.agents.steering import SteerStore
from ..routers import generalist_agent as agent_mod
from ..routers.generalist_agent import GeneralistAgentRequest, SteerWithdrawResponse
from ..routers.serve import ServeChatRequest
from .conftest import bypass_auth


@pytest.mark.parametrize("model", [GeneralistAgentRequest, ServeChatRequest])
def test_turn_steer_key_rejects_empty(model: type) -> None:
    """Reject an explicitly empty key without making omitted keys mandatory.

    Args:
        model: An agent turn request model.
    """
    field = model.model_fields["steer_key"]
    assert not field.is_required()
    with pytest.raises(ValidationError) as error:
        model.model_validate({"steer_key": "", "user_message": "hello"})
    assert any(item["loc"] == ("steer_key",) for item in error.value.errors())


def test_delivered_field_has_schema_description() -> None:
    """Describe delivered ids without changing the existing class description."""
    schema = SteerWithdrawResponse.model_json_schema()
    assert schema["description"] == "Messages returned to the client, oldest first."
    assert schema["properties"]["delivered"].get("description")


def test_withdraw_response_shape_and_omitted_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """Return withdrawn and delivered ids for the authenticated caller's full key.

    Args:
        monkeypatch: Replace the shared store with an isolated fallback store.
    """
    store = SteerStore()
    monkeypatch.setattr(agent_mod, "get_steer_store", lambda: store)
    app = FastAPI()
    app.include_router(agent_mod.create_generalist_agent_router())
    bypass_auth(app)
    client = TestClient(app)
    delivered = store.post("alice", "turn", "read")
    store.mark_delivered("alice", "turn", store.take("alice", "turn"))
    unread = store.post("alice", "turn", "unread")
    foreign = store.post("mallory", "turn", "private")
    response = client.post("/optimizations/agent-steer/withdraw", json={"steer_key": "turn"})
    assert response.status_code == 200
    assert response.json() == {"ids": [unread], "delivered": [delivered]}
    assert store.take("mallory", "turn") == [(foreign, "private")]


def test_over_cap_post_is_rejected_without_losing_pending_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject an eleventh pending steer so the client can queue it instead.

    Args:
        monkeypatch: Replace the shared store with an isolated fallback store.
    """
    store = SteerStore()
    monkeypatch.setattr(agent_mod, "get_steer_store", lambda: store)
    app = FastAPI()
    app.include_router(agent_mod.create_generalist_agent_router())
    bypass_auth(app)
    client = TestClient(app)
    for index in range(10):
        assert (
            client.post("/optimizations/agent-steer", json={"steer_key": "turn", "text": str(index)}).status_code == 200
        )
    response = client.post("/optimizations/agent-steer", json={"steer_key": "turn", "text": "queue me"})
    assert response.status_code == 429
    assert len(store.take("alice", "turn")) == 10

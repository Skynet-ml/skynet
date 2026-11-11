"""Tests that a secret saved from the chat card never reaches the agent (SKY-641).

The flow runs the ``request_secret`` tool through the real approval wrapper,
saves a value through the real ``PUT /secrets`` route while the call waits,
as the browser does, and then stores the turn through the real chat
persistence. The value must not be in the tool's result, in any stream
event, or in the stored chat rows.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...service_gateway.agents.generalist import ApprovalRegistry, _needs_approval, _wrap_tool_with_approval
from ...service_gateway.agents.secret_request import SECRET_REQUEST_TOOL, secret_request_tool
from ...storage.models import AgentConversationModel, AgentMessageModel, UserSecretModel
from ..auth import AuthenticatedUser, get_authenticated_user
from ..routers import generalist_agent as agent_mod
from ..routers.saved_secrets import create_saved_secrets_router
from .test_connectors_router import _MemStore, vault_key  # noqa: F401 - fixture re-export

pytestmark = pytest.mark.usefixtures("vault_key")

_USER = AuthenticatedUser(username="alice@example.com", role="user", groups=())
_CONV_ID = "conv-secret"
_VALUE = "test-SENTINEL-7f3a9c"
_ARGS = {"name": "MAPS_KEY", "purpose": "Google Maps geocoding", "host": "maps.googleapis.com"}


def _secrets_client(store: _MemStore) -> TestClient:
    """Mount the saved secrets router on ``store``, signed in as the test user.

    Args:
        store: In-memory store shared with the chat persistence.

    Returns:
        A test client for ``/secrets``.
    """
    app = FastAPI()
    app.include_router(create_saved_secrets_router(job_store=store))
    app.dependency_overrides[get_authenticated_user] = lambda: _USER
    return TestClient(app)


async def _run_card(events: list[dict[str, Any]], answer: Any, arguments: dict[str, Any] | None = None) -> Any:
    """Call ``request_secret`` and answer its card with ``answer``.

    Args:
        events: Receives every stream event the call emits.
        answer: Called with no arguments while the card shows, then returns
            whether the card confirms.
        arguments: The tool's arguments; the default secret when omitted.

    Returns:
        What the agent gets back from the tool.
    """
    registry = ApprovalRegistry()
    tool = _wrap_tool_with_approval(
        secret_request_tool(),
        trust_mode="yolo",
        registry=registry,
        emit=events.append,
        outer_loop=asyncio.get_running_loop(),
    )
    call = asyncio.create_task(tool.func._async_body(**(arguments or _ARGS)))
    for _ in range(50):
        await asyncio.sleep(0)
        if call.done() or any(e["event"] == "pending_approval" for e in events):
            break
    pending = next((e for e in events if e["event"] == "pending_approval"), None)
    if pending is not None:
        registry.resolve(pending["data"]["id"], answer())
    return await call


async def _persist(store: _MemStore, events: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    """Store ``events`` as one chat turn through the real persistence wrapper.

    Args:
        store: Store holding the conversation.
        events: The tool's stream events.
        monkeypatch: Pytest fixture used to stub the embedding hook.
    """
    monkeypatch.setattr(agent_mod, "queue_conversation_embed", lambda *a, **k: None)
    with Session(store.engine) as session:
        session.add(AgentConversationModel(id=_CONV_ID, username=_USER.username, title="setup"))
        session.commit()

    async def source() -> AsyncIterator[dict[str, Any]]:
        """Replay the tool's events, then end the turn."""
        for event in events:
            yield event
        yield {"event": "done", "data": {"assistant_message": "Saved.", "model": "m"}}

    wrapped = agent_mod._wrap_with_persistence(
        source(), job_store=store, conversation_id=_CONV_ID, title="setup", wizard_state_before={}
    )
    async for _event in wrapped:
        pass


def _stored_rows(store: _MemStore) -> str:
    """Dump every stored chat row's columns as one string.

    Args:
        store: Store holding the conversation.

    Returns:
        Every column of every ``agent_messages`` row, as text.
    """
    with Session(store.engine) as session:
        rows = session.scalars(select(AgentMessageModel)).all()
        assert rows
        return json.dumps(
            [{column.name: getattr(row, column.name) for column in row.__table__.columns} for row in rows],
            default=str,
        )


def test_request_secret_always_waits_for_the_card() -> None:
    """The card shows in every trust mode, YOLO included."""
    assert _needs_approval(SECRET_REQUEST_TOOL, "yolo") is True


async def test_saved_secret_value_reaches_no_agent_event_or_stored_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """The value goes to the store only; the agent, the stream and the chat rows see the name."""
    store = _MemStore()
    client = _secrets_client(store)
    events: list[dict[str, Any]] = []

    def browser_saves() -> bool:
        """Save the value as the card does, then confirm."""
        assert client.put("/secrets", json={"name": "MAPS_KEY", "value": _VALUE}).status_code == 200
        return True

    result = await _run_card(events, browser_saves)

    assert result == {"name": "MAPS_KEY", "status": "saved"}
    pending = next(e for e in events if e["event"] == "pending_approval")
    assert pending["data"]["arguments"] == _ARGS
    await _persist(store, events, monkeypatch)
    for text in (json.dumps(result), json.dumps(events), _stored_rows(store), client.get("/secrets").text):
        assert _VALUE not in text
    with Session(store.engine) as session:
        assert session.scalars(select(UserSecretModel.name)).all() == ["MAPS_KEY"]


async def test_skipped_card_tells_the_agent_skipped() -> None:
    """A skipped card returns the name and ``skipped`` and does not read as a failure."""
    events: list[dict[str, Any]] = []
    result = await _run_card(events, lambda: False)
    assert result == {"name": "MAPS_KEY", "status": "skipped"}
    end = next(e for e in events if e["event"] == "tool_end")
    assert end["data"]["status"] == "ok"
    assert end["data"]["result"] == result


@pytest.mark.parametrize(
    "arguments",
    [
        {**_ARGS, "name": "maps-key"},
        {**_ARGS, "name": "1KEY"},
        {**_ARGS, "purpose": " "},
        {**_ARGS, "host": "https://maps.googleapis.com/v1"},
    ],
)
async def test_bad_request_shows_no_card(arguments: dict[str, Any]) -> None:
    """A bad name, purpose or host is refused before the card shows."""
    events: list[dict[str, Any]] = []
    result = await _run_card(events, lambda: True, arguments)
    assert isinstance(result, str)
    assert result.startswith("Not shown")
    assert all(e["event"] != "pending_approval" for e in events)

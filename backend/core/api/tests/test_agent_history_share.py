"""Tests for the agent-history public share-link routes.

Covers ``POST``/``DELETE /agent/conversations/{id}/share`` (owner-only,
idempotent create, revoke) and the unauthenticated
``GET /agent/shared-conversations/{token}`` read: live messages, 404 for
unknown or revoked tokens, masked credentials, and no owner or cost fields.
Also covers the owner's shared list, the Mac snapshot import routes under
``/desktop/v1/shared-chats``, the Mac app forward to hosted, and the purge
exclusion for shared chats.
"""

from __future__ import annotations

import asyncio
import secrets
import string
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.requests import Request

from ...config import settings
from ...local import hosted_link
from ...storage.models import AgentConversationModel, AgentMessageModel
from ..app import _cache_control_for
from ..auth import AuthenticatedUser, generate_api_token
from ..desktop_auth import DesktopPrincipal
from ..errors import DomainError
from ..routers import agent_history
from ..routers.agent_history import create_agent_history_router, purge_stale_conversations
from .test_agent_history_router import _client, _MemStore, _seed

_BOB = AuthenticatedUser(username="bob", role="user", groups=())
_ALICE = AuthenticatedUser(username="alice", role="user", groups=())
_FAKE_KEY = "sk-" + "a1B2c3D4e5F6g7H8i9J0"


def _anon_client(store: _MemStore) -> TestClient:
    """Mount the router on ``store`` with no auth override, as a signed-out visitor.

    Args:
        store: The store the authed client already seeded.

    Returns:
        A client whose requests carry no identity.
    """
    app = FastAPI()
    app.include_router(create_agent_history_router(job_store=store))

    @app.exception_handler(DomainError)
    async def _domain_error_handler(_request, exc: DomainError) -> JSONResponse:
        """Mirror the app-level envelope so tests can assert on ``code``."""
        return JSONResponse(status_code=exc.status_code, content={"code": exc.code})

    return TestClient(app)


def _add_message(store: _MemStore, conversation_id: str, role: str, content: str, **extra) -> None:
    """Insert one message row into a conversation.

    Args:
        store: The store to write.
        conversation_id: The parent conversation id.
        role: ``user`` or ``assistant``.
        content: The message text.
        **extra: Other ``AgentMessageModel`` columns (tool_calls, router_metadata).
    """
    with store._session_factory() as session:
        session.add(AgentMessageModel(conversation_id=conversation_id, role=role, content=content, **extra))
        session.commit()


def test_share_is_idempotent_and_unguessable() -> None:
    """Two creates return the same long token; it is stored on the row."""
    client, store = _client(_ALICE)
    _seed(store, ("c-1", "alice"))

    first = client.post("/agent/conversations/c-1/share")
    second = client.post("/agent/conversations/c-1/share")
    assert first.status_code == 200, first.text
    token = first.json()["token"]
    assert second.json()["token"] == token
    assert len(token) >= 32
    with store._session_factory() as session:
        assert session.get(AgentConversationModel, "c-1").share_token == token


def test_share_and_revoke_are_owner_only() -> None:
    """Another user gets 403 on create and revoke; an unknown id gets 404."""
    client, store = _client(_BOB)
    _seed(store, ("c-1", "alice"))

    assert client.post("/agent/conversations/c-1/share").status_code == 403
    assert client.delete("/agent/conversations/c-1/share").status_code == 403
    assert client.post("/agent/conversations/ghost/share").status_code == 404
    with store._session_factory() as session:
        assert session.get(AgentConversationModel, "c-1").share_token is None


def test_public_read_is_live_masked_and_has_no_private_fields() -> None:
    """A signed-out visitor reads the live chat, with credentials masked and costs left out."""
    client, store = _client(_ALICE)
    _seed(store, ("c-1", "alice"))
    _add_message(store, "c-1", "user", f"my key is {_FAKE_KEY}")
    token = client.post("/agent/conversations/c-1/share").json()["token"]
    _add_message(
        store,
        "c-1",
        "assistant",
        "done",
        model="m-1",
        tool_calls=[
            {
                "id": "t1",
                "tool": "x",
                "payload": {"api_key": "plain-secret", "n": 3},
                "result": {"email": "owner@example.com"},
            }
        ],
        router_metadata={"stats": {"cost": 1.5}, "plan": f"use {_FAKE_KEY}", "served_model": "m-2"},
    )

    resp = _anon_client(store).get(f"/agent/shared-conversations/{token}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    text = resp.text
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert _FAKE_KEY not in text
    assert "plain-secret" not in text
    assert "owner@example.com" not in text
    assert "alice" not in text
    assert set(body) == {"title", "created_at", "updated_at", "messages"}
    assistant = body["messages"][1]
    assert assistant["stats"] is None
    assert assistant["tool_calls"][0]["payload"]["n"] == 3
    assert assistant["served_model"] == "m-2"


def test_public_read_404_for_unknown_and_revoked_tokens() -> None:
    """An unknown token and a revoked token both read as 404."""
    client, store = _client(_ALICE)
    _seed(store, ("c-1", "alice"))
    token = client.post("/agent/conversations/c-1/share").json()["token"]
    anon = _anon_client(store)

    assert anon.get("/agent/shared-conversations/not-a-token").status_code == 404
    assert anon.get(f"/agent/shared-conversations/{'x' * 65}").status_code == 404
    assert client.delete("/agent/conversations/c-1/share").status_code == 204
    assert anon.get(f"/agent/shared-conversations/{token}").status_code == 404
    new_token = client.post("/agent/conversations/c-1/share").json()["token"]
    assert new_token != token
    assert anon.get(f"/agent/shared-conversations/{new_token}").status_code == 200


def _desktop(client: TestClient, username: str = "alice") -> None:
    """Make the desktop ``sync`` principal of ``client`` resolve to ``username``.

    Args:
        client: A client from ``_client``.
        username: The hosted account behind the desktop token.
    """
    client.app.dependency_overrides[agent_history.desktop_sync_principal] = lambda: DesktopPrincipal(
        username=username, link_id="dl_1", scopes=frozenset({"sync"}), device_name="Test Mac"
    )


def _snapshot(local_id: str = "mac-1", content: str = "hello", count: int = 1) -> dict[str, Any]:
    """Build a Mac chat snapshot body.

    Args:
        local_id: The Mac chat id.
        content: The text of every message.
        count: How many messages.

    Returns:
        The JSON body for ``POST /desktop/v1/shared-chats``.
    """
    start = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    return {
        "local_id": local_id,
        "title": "Mac chat",
        "messages": [
            {
                "role": "user" if i % 2 == 0 else "assistant",
                "content": content,
                "created_at": (start + timedelta(seconds=i)).isoformat(),
            }
            for i in range(count)
        ],
    }


def test_share_sets_shared_at_and_revoke_clears_it() -> None:
    """Creating a link stamps ``shared_at``; revoking clears token and stamp."""
    client, store = _client(_ALICE)
    _seed(store, ("c-1", "alice"))

    client.post("/agent/conversations/c-1/share")
    with store._session_factory() as session:
        assert session.get(AgentConversationModel, "c-1").shared_at is not None
    client.delete("/agent/conversations/c-1/share")
    with store._session_factory() as session:
        row = session.get(AgentConversationModel, "c-1")
        assert row.share_token is None
        assert row.shared_at is None


def test_shared_list_is_owner_only_and_drops_revoked() -> None:
    """The list shows only the caller's live links; a revoke removes the row."""
    client, store = _client(_ALICE)
    _seed(store, ("c-1", "alice"), ("c-2", "alice"), ("c-3", "bob"))
    token = client.post("/agent/conversations/c-1/share").json()["token"]
    with store._session_factory() as session:
        session.get(AgentConversationModel, "c-3").share_token = "bob-token"
        session.commit()

    listed = client.get("/agent/shared-conversations").json()
    assert [(i["id"], i["title"], i["token"]) for i in listed] == [("c-1", "c-1", token)]
    assert listed[0]["shared_at"]

    client.delete("/agent/conversations/c-1/share")
    assert client.get("/agent/shared-conversations").json() == []
    assert _anon_client(store).get(f"/agent/shared-conversations/{token}").status_code == 404


def test_desktop_import_needs_a_desktop_token() -> None:
    """Without a desktop bearer token the import route answers 401."""
    client, _store = _client(_ALICE)

    assert client.post("/desktop/v1/shared-chats", json=_snapshot()).status_code == 401
    assert client.get("/desktop/v1/shared-chats").status_code == 401


def test_desktop_import_masks_and_is_idempotent_per_local_id() -> None:
    """A re-share replaces the messages, keeps the token and masks credentials."""
    client, store = _client(_ALICE)
    _desktop(client)

    first = client.post("/desktop/v1/shared-chats", json=_snapshot(content=f"key {_FAKE_KEY}"))
    assert first.status_code == 200, first.text
    token = first.json()["token"]
    second = client.post("/desktop/v1/shared-chats", json=_snapshot(content="newer", count=3))
    assert second.json()["token"] == token

    shared = _anon_client(store).get(f"/agent/shared-conversations/{token}").json()
    assert [m["content"] for m in shared["messages"]] == ["newer", "newer", "newer"]
    assert client.post("/desktop/v1/shared-chats", json=_snapshot(local_id="mac-2")).json()["token"] != token

    client.post("/desktop/v1/shared-chats", json=_snapshot(content=f"key {_FAKE_KEY}"))
    shared = _anon_client(store).get(f"/agent/shared-conversations/{token}").json()
    assert _FAKE_KEY not in str(shared)
    with store._session_factory() as session:
        assert all(_FAKE_KEY not in m.content for m in session.query(AgentMessageModel))
        assert session.query(AgentConversationModel).filter_by(username="alice").count() == 2


def test_desktop_import_caps_size() -> None:
    """Too many messages is a 422; a body over the byte cap is a 413."""
    client, _store = _client(_ALICE)
    _desktop(client)

    many = _snapshot(count=agent_history.MAX_SHARED_MESSAGES + 1)
    assert client.post("/desktop/v1/shared-chats", json=many).status_code == 422
    big = _snapshot(content="x" * (agent_history.MAX_SHARED_BYTES // 2), count=3)
    answer = client.post("/desktop/v1/shared-chats", json=big)
    assert answer.status_code == 413
    assert answer.json()["code"] == "agent.share.too_large"


def test_desktop_list_and_revoke_by_local_or_hosted_id() -> None:
    """The desktop list shows imported chats; revoke works by Mac id or hosted id."""
    client, store = _client(_ALICE)
    _desktop(client)
    token = client.post("/desktop/v1/shared-chats", json=_snapshot("mac-1")).json()["token"]
    client.post("/desktop/v1/shared-chats", json=_snapshot("mac-2"))

    listed = client.get("/desktop/v1/shared-chats").json()
    assert len(listed) == 2
    assert {i["title"] for i in listed} == {"Mac chat"}
    other = next(i for i in listed if i["token"] != token)

    _desktop(client, "bob")
    assert client.delete("/desktop/v1/shared-chats/mac-1").status_code == 204
    _desktop(client)
    assert len(client.get("/desktop/v1/shared-chats").json()) == 2

    assert client.delete("/desktop/v1/shared-chats/mac-1").status_code == 204
    assert client.delete(f"/desktop/v1/shared-chats/{other['id']}").status_code == 204
    assert client.get("/desktop/v1/shared-chats").json() == []
    assert _anon_client(store).get(f"/agent/shared-conversations/{token}").status_code == 404


def test_local_mode_share_publishes_a_masked_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """In the Mac app share, list and revoke go to hosted; nothing is stored locally."""
    calls: list[tuple[str, str, Any]] = []

    def fake_request(engine, username, method, path, body=None):
        """Record the hosted call and answer like hosted."""
        calls.append((method, path, body))
        if method == "POST":
            return {"token": "hosted-token"}
        if method == "GET":
            return [{"id": "h-1", "title": "T", "shared_at": "2026-11-01T12:00:00Z", "token": "hosted-token"}]
        return None

    monkeypatch.setattr(settings, "local_mode", True)
    monkeypatch.setattr(hosted_link, "hosted_request", fake_request)
    client, store = _client(_ALICE)
    _seed(store, ("c-1", "alice"))
    _add_message(store, "c-1", "user", f"use {_FAKE_KEY}")
    _add_message(store, "c-1", "assistant", "done", router_metadata={"stats": {"cost": 1}, "plan": "p"})

    answer = client.post("/agent/conversations/c-1/share")
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"token": "hosted-token"}
    method, path, body = calls[0]
    assert (method, path, body["local_id"]) == ("POST", "/desktop/v1/shared-chats", "c-1")
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert _FAKE_KEY not in str(body)
    assert "stats" not in str(body)
    with store._session_factory() as session:
        assert session.get(AgentConversationModel, "c-1").share_token is None

    assert client.get("/agent/shared-conversations").json()[0]["token"] == "hosted-token"
    assert client.delete("/agent/conversations/c-1/share").status_code == 204
    assert calls[-1][:2] == ("DELETE", "/desktop/v1/shared-chats/c-1")


def test_local_mode_share_of_empty_chat_does_not_call_hosted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A chat with no messages is a 404 and makes no hosted call."""
    monkeypatch.setattr(settings, "local_mode", True)
    monkeypatch.setattr(hosted_link, "hosted_request", lambda *a, **k: pytest.fail("hosted was called"))
    client, store = _client(_ALICE)
    _seed(store, ("c-1", "alice"))

    assert client.post("/agent/conversations/c-1/share").status_code == 404


def test_purge_keeps_shared_conversations() -> None:
    """An old, unpinned chat with a live link survives the purge; one without goes."""
    client, store = _client(_ALICE)
    _seed(store, ("old-shared", "alice"), ("old-plain", "alice"))
    client.post("/agent/conversations/old-shared/share")
    with store._session_factory() as session:
        for row in session.query(AgentConversationModel):
            row.updated_at = datetime.now(UTC) - timedelta(days=400)
        session.commit()

    assert purge_stale_conversations(store.engine, threshold_days=30) == 1
    with store._session_factory() as session:
        assert [r.id for r in session.query(AgentConversationModel)] == ["old-shared"]


def _secret_samples() -> dict[str, str]:
    """Build one fresh value per credential format, at runtime, so the repo holds no literal.

    Returns:
        A map of format name to a random value in that format.
    """
    aws_body = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(16))
    return {
        "skynet_pat": generate_api_token(),
        "github_pat": "github_pat_" + secrets.token_hex(20),
        "aws_key_id": "AKIA" + aws_body,
        "jwt": ".".join(("eyJ" + secrets.token_hex(8), "eyJ" + secrets.token_hex(12), secrets.token_hex(16))),
    }


@pytest.mark.parametrize("kind", ["skynet_pat", "github_pat", "aws_key_id", "jwt"])
def test_public_read_masks_each_token_format(kind: str) -> None:
    """The public share page masks Skynet, GitHub, AWS and JWT credentials."""
    secret = _secret_samples()[kind]
    client, store = _client(_ALICE)
    _seed(store, ("c-1", "alice"))
    _add_message(store, "c-1", "user", f"use {secret} now")
    token = client.post("/agent/conversations/c-1/share").json()["token"]

    resp = _anon_client(store).get(f"/agent/shared-conversations/{token}")
    assert resp.status_code == 200, resp.text
    assert secret not in resp.text
    assert "use " in resp.json()["messages"][0]["content"]


@pytest.mark.parametrize("kind", ["skynet_pat", "github_pat", "aws_key_id", "jwt"])
def test_desktop_import_masks_each_token_format(kind: str) -> None:
    """A Mac snapshot import masks Skynet, GitHub, AWS and JWT credentials before it stores them."""
    secret = _secret_samples()[kind]
    client, store = _client(_ALICE)
    _desktop(client)
    resp = client.post("/desktop/v1/shared-chats", json=_snapshot(content=f"key {secret}"))
    assert resp.status_code in (200, 201), resp.text

    shared = _anon_client(store).get(f"/agent/shared-conversations/{resp.json()['token']}")
    assert secret not in shared.text
    with store._session_factory() as session:
        assert all(secret not in m.content for m in session.query(AgentMessageModel))


def test_masking_leaves_ordinary_words_alone() -> None:
    """Words that only start like a token prefix stay readable."""
    text = "skydiving and AKIA short"
    assert agent_history.mask_secrets(text) == text


def test_public_share_read_is_never_cached() -> None:
    """A revoked link must stop at once, so the public read sends ``no-store``."""
    assert _cache_control_for("/agent/shared-conversations/abc", b"") == "no-store"
    assert _cache_control_for("/agent/conversations/abc", b"") is None


def _import_request(chunks: list[bytes], declared: int | None) -> Request:
    """Build an import request that streams ``chunks`` and declares ``declared`` bytes.

    Args:
        chunks: The body parts, in arrival order.
        declared: The ``Content-Length`` value, or ``None`` to send no header.

    Returns:
        A request whose stream yields the chunks.
    """
    parts = list(chunks)
    headers = [] if declared is None else [(b"content-length", str(declared).encode())]

    async def receive() -> dict[str, Any]:
        """Hand out the next chunk, as the ASGI server would."""
        body = parts.pop(0) if parts else b""
        return {"type": "http.request", "body": body, "more_body": bool(parts)}

    return Request({"type": "http", "method": "POST", "path": "/", "headers": headers}, receive)


def test_import_refuses_large_declared_length_before_reading() -> None:
    """A body that declares more than the cap is refused without reading the stream."""
    request = _import_request([b"x"], agent_history.MAX_SHARED_BYTES + 1)
    with pytest.raises(DomainError) as caught:
        asyncio.run(agent_history._read_capped_snapshot(request))
    assert caught.value.status_code == 413


def test_import_stops_a_stream_that_lies_about_its_length() -> None:
    """A body that declares a small length but streams past the cap is cut off."""
    half = b"x" * (agent_history.MAX_SHARED_BYTES // 2 + 1)
    for declared in (10, None):
        request = _import_request([half, half, half], declared)
        with pytest.raises(DomainError) as caught:
            asyncio.run(agent_history._read_capped_snapshot(request))
        assert caught.value.status_code == 413

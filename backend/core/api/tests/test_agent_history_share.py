"""Tests for the typed agent chat share links (SKY-294).

Covers the owner routes under ``/agent/conversations/{id}/share`` (state,
create or change, revoke), the public ``GET /agent/shared-conversations/{token}``
read, the signed-in ``open`` route for each link type, the redaction of
private tool calls for users other than the owner, and the live, read-only
``read_shared_chat_item`` grant with its revocation. Also covers the owner's
shared list, credential masking, the purge exclusion and the cache rule.
"""

from __future__ import annotations

import secrets
import string
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from ...config import settings
from ...storage.models import (
    AgentConversationModel,
    AgentMessageModel,
    AgentStagedDatasetModel,
    DatasetModel,
    JobModel,
)
from ..app import _cache_control_for
from ..auth import AuthenticatedUser, generate_api_token, get_authenticated_user
from ..errors import DomainError
from ..routers import agent_history
from ..routers.agent_history import create_agent_history_router, purge_stale_conversations
from .test_agent_history_router import _client, _MemStore, _seed

_BOB = AuthenticatedUser(username="bob", role="user", groups=())
_ALICE = AuthenticatedUser(username="alice", role="user", groups=())
_CAROL = AuthenticatedUser(username="carol", role="user", groups=())
_FAKE_KEY = "sk-" + "a1B2c3D4e5F6g7H8i9J0"


def _mount(store: _MemStore, user: AuthenticatedUser | None) -> TestClient:
    """Mount the router on ``store`` as ``user``, or as a signed-out visitor.

    Args:
        store: The store that the other clients share.
        user: The identity of every request, or ``None`` for no identity.

    Returns:
        A test client on the shared store.
    """
    app = FastAPI()
    app.include_router(create_agent_history_router(job_store=store))
    if user is not None:
        app.dependency_overrides[get_authenticated_user] = lambda: user

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


def _call(tool: str, arguments: dict[str, Any], result: Any) -> dict[str, Any]:
    """Build one stored tool call in the frontend shape.

    Args:
        tool: The tool name.
        arguments: The call arguments.
        result: The tool result.

    Returns:
        The tool-call entry.
    """
    return {"id": f"t-{tool}", "tool": tool, "status": "done", "payload": {"arguments": arguments, "result": result}}


def _private_chat(store: _MemStore) -> None:
    """Seed Alice's chat ``c-1``, which refers to her run, dataset and file and one of Bob's runs.

    Args:
        store: The store to write.
    """
    _seed(store, ("c-1", "alice"))
    with store._session_factory() as session:
        session.add_all(
            [
                JobModel(
                    optimization_id="run-a",
                    status="success",
                    username="alice",
                    payload_overview={"name": "Alice run"},
                    result={"score": 0.9, "program": "secret program text"},
                    latest_metrics={"acc": 0.9},
                ),
                JobModel(optimization_id="run-b", status="success", username="bob", payload_overview={"name": "B"}),
                DatasetModel(
                    id="ds-a",
                    owner_username="alice",
                    name="Alice data",
                    row_count=2,
                    column_count=1,
                    byte_size=10,
                    stored_bytes=10,
                    content_hash="h",
                    column_schema={"q": "string"},
                ),
                AgentStagedDatasetModel(
                    id="file-a",
                    username="alice",
                    dataset_filename="alice.csv",
                    rows=[{"q": "file row secret"}],
                    row_count=1,
                ),
            ]
        )
        session.commit()
    _add_message(store, "c-1", "user", "show my run")
    _add_message(
        store,
        "c-1",
        "assistant",
        "here it is",
        tool_calls=[
            _call("get_job_summary_optimizations", {"optimization_id": "run-a"}, {"score": "private score 0.9"}),
            _call("list_datasets", {}, [{"dataset_id": "ds-a"}, {"staged_dataset_id": "file-a"}]),
            _call("get_job_summary_optimizations", {"optimization_id": "run-b"}, {"note": "bob run"}),
            _call("list_models_for_agent", {}, {"models": ["m-1"]}),
        ],
    )


def _share(client: TestClient, link_type: str, items: list[dict[str, str]] | None = None) -> str:
    """Create or change the share link of ``c-1`` and return its token.

    Args:
        client: The owner's client.
        link_type: ``view``, ``continue`` or ``continue_data``.
        items: The ticked items.

    Returns:
        The link token.
    """
    resp = client.post("/agent/conversations/c-1/share", json={"link_type": link_type, "items": items or []})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def test_share_state_lists_only_owned_candidates_and_starts_unshared() -> None:
    """The state names the owner's run, dataset and file, never another user's run."""
    client, store = _client(_ALICE)
    _private_chat(store)

    state = client.get("/agent/conversations/c-1/share").json()
    assert state["token"] is None
    assert state["items"] == []
    assert [(c["kind"], c["id"], c["label"]) for c in state["candidates"]] == [
        ("run", "run-a", "Alice run"),
        ("dataset", "ds-a", "Alice data"),
        ("file", "file-a", "alice.csv"),
    ]


def test_share_keeps_token_and_filters_items() -> None:
    """A change keeps the token; foreign or unknown items drop; only continue_data keeps items."""
    client, store = _client(_ALICE)
    _private_chat(store)

    token = _share(client, "continue_data", [{"kind": "run", "id": "run-a"}, {"kind": "run", "id": "run-b"}])
    assert len(token) >= 32
    state = client.get("/agent/conversations/c-1/share").json()
    assert state["link_type"] == "continue_data"
    assert [(i["kind"], i["id"]) for i in state["items"]] == [("run", "run-a")]

    assert _share(client, "continue", [{"kind": "run", "id": "run-a"}]) == token
    assert client.get("/agent/conversations/c-1/share").json()["items"] == []


def test_share_routes_are_owner_only() -> None:
    """Another user gets 403 on the state, create and revoke; an unknown id gets 404."""
    _, store = _client(_ALICE)
    _seed(store, ("c-1", "alice"))
    bob = _mount(store, _BOB)

    body = {"link_type": "view", "items": []}
    assert bob.get("/agent/conversations/c-1/share").status_code == 403
    assert bob.post("/agent/conversations/c-1/share", json=body).status_code == 403
    assert bob.delete("/agent/conversations/c-1/share").status_code == 403
    assert bob.post("/agent/conversations/ghost/share", json=body).status_code == 404


def test_share_refuses_an_unknown_link_type() -> None:
    """A link type outside the three types is a 422."""
    client, store = _client(_ALICE)
    _seed(store, ("c-1", "alice"))
    assert client.post("/agent/conversations/c-1/share", json={"link_type": "edit"}).status_code == 422


def test_view_link_public_read_redacts_private_tool_calls() -> None:
    """A signed-out visitor reads a view link; private results become "not shared" rows."""
    client, store = _client(_ALICE)
    _private_chat(store)
    token = _share(client, "view")

    resp = _mount(store, None).get(f"/agent/shared-conversations/{token}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["link_type"] == "view"
    assert "private score" not in resp.text
    assert "bob run" not in resp.text
    assert "alice" not in resp.text.replace("alice.csv", "")
    calls = body["messages"][1]["tool_calls"]
    assert [c["tool"] for c in calls] == ["shared_chat_item"] * 3 + ["list_models_for_agent"]
    assert {c["payload"]["result"]["status"] for c in calls[:3]} == {"not_shared"}
    assert calls[3]["payload"]["result"] == {"models": ["m-1"]}


@pytest.mark.parametrize("link_type", ["continue", "continue_data"])
def test_continue_link_public_read_shows_no_chat(link_type: str) -> None:
    """A continue link needs a sign-in, so its public read holds the type and no chat."""
    client, store = _client(_ALICE)
    _private_chat(store)
    token = _share(client, link_type, [{"kind": "run", "id": "run-a"}])

    body = _mount(store, None).get(f"/agent/shared-conversations/{token}").json()
    assert body["link_type"] == link_type
    assert body["messages"] == []
    assert body["title"] == ""


def test_unknown_and_revoked_tokens_are_404() -> None:
    """An unknown, long or revoked token reads and opens as 404."""
    client, store = _client(_ALICE)
    _seed(store, ("c-1", "alice"))
    token = _share(client, "view")
    anon = _mount(store, None)
    bob = _mount(store, _BOB)

    assert anon.get("/agent/shared-conversations/not-a-token").status_code == 404
    assert anon.get(f"/agent/shared-conversations/{'x' * 65}").status_code == 404
    assert bob.post("/agent/shared-conversations/not-a-token/open").status_code == 404
    assert client.delete("/agent/conversations/c-1/share").status_code == 204
    assert anon.get(f"/agent/shared-conversations/{token}").status_code == 404
    assert bob.post(f"/agent/shared-conversations/{token}/open").status_code == 404
    new_token = _share(client, "view")
    assert new_token != token
    assert anon.get(f"/agent/shared-conversations/{new_token}").status_code == 200


def test_open_as_owner_returns_the_chat_itself() -> None:
    """The owner opens their own chat for every link type."""
    client, store = _client(_ALICE)
    _private_chat(store)
    for link_type in ("view", "continue", "continue_data"):
        token = _share(client, link_type, [{"kind": "run", "id": "run-a"}])
        assert client.post(f"/agent/shared-conversations/{token}/open").json() == {
            "mode": "owner",
            "conversation_id": "c-1",
        }


def test_open_view_link_as_other_user_is_read_only() -> None:
    """Another signed-in user gets the view mode and no copy."""
    client, store = _client(_ALICE)
    _private_chat(store)
    token = _share(client, "view")

    assert _mount(store, _BOB).post(f"/agent/shared-conversations/{token}/open").json() == {
        "mode": "view",
        "conversation_id": None,
    }
    with store._session_factory() as session:
        assert session.query(AgentConversationModel).filter_by(username="bob").count() == 0


def test_open_continue_link_makes_one_redacted_copy() -> None:
    """The first open makes Bob's own redacted copy; the second open returns the same copy."""
    client, store = _client(_ALICE)
    _private_chat(store)
    token = _share(client, "continue")
    bob = _mount(store, _BOB)

    first = bob.post(f"/agent/shared-conversations/{token}/open").json()
    second = bob.post(f"/agent/shared-conversations/{token}/open").json()
    assert first["mode"] == "fork"
    assert first == second
    fork_id = first["conversation_id"]
    assert fork_id != "c-1"

    detail = bob.get(f"/agent/conversations/{fork_id}")
    assert detail.status_code == 200, detail.text
    assert "private score" not in detail.text
    assert [m["role"] for m in detail.json()["messages"]] == ["user", "assistant"]
    assert client.get(f"/agent/conversations/{fork_id}").status_code == 403


def test_parallel_open_returns_the_copy_that_won(monkeypatch: pytest.MonkeyPatch) -> None:
    """When another open made the copy after the check, the losing open returns that copy, not a 500."""
    client, store = _client(_ALICE)
    _private_chat(store)
    token = _share(client, "continue")
    bob = _mount(store, _BOB)
    winner = bob.post(f"/agent/shared-conversations/{token}/open").json()["conversation_id"]

    real_fork_of = agent_history._fork_of
    calls = iter([None])
    monkeypatch.setattr(agent_history, "_fork_of", lambda *a: next(calls, real_fork_of(*a)))

    resp = bob.post(f"/agent/shared-conversations/{token}/open")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"mode": "fork", "conversation_id": winner}
    with store._session_factory() as session:
        forks = session.query(AgentConversationModel).filter(AgentConversationModel.username == "bob").count()
    assert forks == 1


def test_continue_data_needs_an_offered_item() -> None:
    """A continue_data link with no item, or only items the chat does not offer, is a 422."""
    client, store = _client(_ALICE)
    _private_chat(store)
    for items in ([], [{"kind": "run", "id": "run-b"}]):
        resp = client.post("/agent/conversations/c-1/share", json={"link_type": "continue_data", "items": items})
        assert resp.status_code == 422, resp.text
    assert client.get("/agent/conversations/c-1/share").json()["token"] is None


def test_copy_keeps_the_newest_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    """A chat longer than the cap shares its newest turns, oldest first."""
    monkeypatch.setattr(agent_history, "MAX_SHARED_MESSAGES", 2)
    client, store = _client(_ALICE)
    _seed(store, ("c-1", "alice"))
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(4):
        _add_message(store, "c-1", "user", f"turn {i}", created_at=start + timedelta(minutes=i))
    token = _share(client, "view")
    shown = _mount(store, None).get(f"/agent/shared-conversations/{token}").json()["messages"]
    assert [m["content"] for m in shown] == ["turn 2", "turn 3"]


def test_continue_data_copy_names_only_ticked_items() -> None:
    """The copy names the ticked run with its label; unticked data stays "not shared"."""
    client, store = _client(_ALICE)
    _private_chat(store)
    token = _share(client, "continue_data", [{"kind": "run", "id": "run-a"}])
    bob = _mount(store, _BOB)
    fork_id = bob.post(f"/agent/shared-conversations/{token}/open").json()["conversation_id"]

    detail = bob.get(f"/agent/conversations/{fork_id}")
    calls = detail.json()["messages"][1]["tool_calls"]
    results = [c["payload"]["result"] for c in calls[:3]]
    assert results[0]["status"] == "shared"
    assert results[0]["items"] == [{"kind": "run", "id": "run-a", "label": "Alice run"}]
    assert "read_shared_chat_item" in results[0]["hint"]
    assert [r["status"] for r in results[1:]] == ["not_shared", "not_shared"]
    assert "ds-a" not in detail.text
    assert "file-a" not in detail.text
    assert "private score" not in detail.text


def _copy_statuses(bob: TestClient, fork_id: str) -> list[str]:
    """Return the share status of the first three copied tool calls (run, data list, Bob's run).

    Args:
        bob: The recipient's client.
        fork_id: The recipient's continued chat.

    Returns:
        The ``shared`` or ``not_shared`` status of each call.
    """
    calls = bob.get(f"/agent/conversations/{fork_id}").json()["messages"][1]["tool_calls"]
    return [c["payload"]["result"]["status"] for c in calls[:3]]


def test_reopen_shows_a_newly_ticked_item_and_keeps_own_turns() -> None:
    """After the owner ticks another item, a second open names it in the copy; Bob's turns stay."""
    client, store = _client(_ALICE)
    _private_chat(store)
    token = _share(client, "continue_data", [{"kind": "run", "id": "run-a"}])
    bob = _mount(store, _BOB)
    fork_id = bob.post(f"/agent/shared-conversations/{token}/open").json()["conversation_id"]
    _add_message(store, fork_id, "user", "bob turn")
    assert _copy_statuses(bob, fork_id) == ["shared", "not_shared", "not_shared"]

    _share(client, "continue_data", [{"kind": "run", "id": "run-a"}, {"kind": "dataset", "id": "ds-a"}])
    assert bob.post(f"/agent/shared-conversations/{token}/open").json()["conversation_id"] == fork_id

    assert _copy_statuses(bob, fork_id) == ["shared", "shared", "not_shared"]
    messages = bob.get(f"/agent/conversations/{fork_id}").json()["messages"]
    assert messages[1]["tool_calls"][1]["payload"]["result"]["items"] == [
        {"kind": "dataset", "id": "ds-a", "label": "Alice data"}
    ]
    assert messages[-1]["content"] == "bob turn"


def test_reopen_marks_an_unticked_item_not_shared() -> None:
    """After the owner unticks an item, a second open shows it as "not shared" in the copy."""
    client, store = _client(_ALICE)
    _private_chat(store)
    token = _share(client, "continue_data", [{"kind": "run", "id": "run-a"}, {"kind": "dataset", "id": "ds-a"}])
    bob = _mount(store, _BOB)
    fork_id = bob.post(f"/agent/shared-conversations/{token}/open").json()["conversation_id"]

    _share(client, "continue_data", [{"kind": "dataset", "id": "ds-a"}])
    bob.post(f"/agent/shared-conversations/{token}/open")

    assert _copy_statuses(bob, fork_id) == ["not_shared", "shared", "not_shared"]
    assert "Alice run" not in bob.get(f"/agent/conversations/{fork_id}").text


def test_shared_item_read_is_allowed_for_a_ticked_item() -> None:
    """Bob reads the ticked run and file through his copy, masked and read-only."""
    client, store = _client(_ALICE)
    _private_chat(store)
    token = _share(client, "continue_data", [{"kind": "run", "id": "run-a"}, {"kind": "file", "id": "file-a"}])
    bob = _mount(store, _BOB)
    bob.post(f"/agent/shared-conversations/{token}/open")

    run = bob.get("/agent/shared-items/run/run-a")
    assert run.status_code == 200, run.text
    assert run.json()["label"] == "Alice run"
    assert run.json()["detail"]["result"]["score"] == 0.9
    file = bob.get("/agent/shared-items/file/file-a").json()
    assert file["detail"]["rows"] == [{"q": "file row secret"}]


def test_shared_item_read_is_denied_without_a_grant() -> None:
    """An unticked item, a caller without a copy and a continue link all get 403."""
    client, store = _client(_ALICE)
    _private_chat(store)
    token = _share(client, "continue_data", [{"kind": "run", "id": "run-a"}])
    bob = _mount(store, _BOB)
    carol = _mount(store, _CAROL)
    bob.post(f"/agent/shared-conversations/{token}/open")

    assert bob.get("/agent/shared-items/dataset/ds-a").status_code == 403
    assert bob.get("/agent/shared-items/file/file-a").status_code == 403
    assert bob.get("/agent/shared-items/run/run-b").status_code == 403
    assert carol.get("/agent/shared-items/run/run-a").status_code == 403
    assert bob.get("/agent/shared-items/program/run-a").status_code == 422

    _share(client, "continue")
    assert bob.get("/agent/shared-items/run/run-a").status_code == 403


def test_revoke_stops_item_reads_at_once() -> None:
    """After a revoke the copy stays Bob's, but the item read is 403 and the copy lost its link."""
    client, store = _client(_ALICE)
    _private_chat(store)
    token = _share(client, "continue_data", [{"kind": "run", "id": "run-a"}])
    bob = _mount(store, _BOB)
    fork_id = bob.post(f"/agent/shared-conversations/{token}/open").json()["conversation_id"]
    assert bob.get("/agent/shared-items/run/run-a").status_code == 200

    assert client.delete("/agent/conversations/c-1/share").status_code == 204
    assert bob.get("/agent/shared-items/run/run-a").status_code == 403
    assert bob.get(f"/agent/conversations/{fork_id}").status_code == 200
    with store._session_factory() as session:
        assert session.get(AgentConversationModel, fork_id).source_share_token is None

    _share(client, "continue_data", [{"kind": "run", "id": "run-a"}])
    assert bob.get("/agent/shared-items/run/run-a").status_code == 403


def test_deleting_the_shared_chat_revokes_its_grants() -> None:
    """When the owner deletes the chat, the link and its grants stop."""
    client, store = _client(_ALICE)
    _private_chat(store)
    token = _share(client, "continue_data", [{"kind": "run", "id": "run-a"}])
    bob = _mount(store, _BOB)
    bob.post(f"/agent/shared-conversations/{token}/open")

    assert client.delete("/agent/conversations/c-1").status_code == 204
    assert bob.get("/agent/shared-items/run/run-a").status_code == 403
    assert _mount(store, None).get(f"/agent/shared-conversations/{token}").status_code == 404


def test_deleted_item_reads_as_404() -> None:
    """A ticked item that the owner deleted reads as 404 and shows as unavailable in the list."""
    client, store = _client(_ALICE)
    _private_chat(store)
    token = _share(client, "continue_data", [{"kind": "run", "id": "run-a"}])
    bob = _mount(store, _BOB)
    bob.post(f"/agent/shared-conversations/{token}/open")
    with store._session_factory() as session:
        session.delete(session.get(JobModel, "run-a"))
        session.commit()

    assert bob.get("/agent/shared-items/run/run-a").status_code == 404
    listed = client.get("/agent/shared-conversations").json()
    assert listed[0]["items"] == [{"kind": "run", "id": "run-a", "label": "", "available": False}]


def test_ticked_item_stays_available_after_chat_stops_naming_it() -> None:
    """The owner state matches the grant: a ticked item the chat no longer names still reads."""
    client, store = _client(_ALICE)
    _private_chat(store)
    token = _share(client, "continue_data", [{"kind": "run", "id": "run-a"}])
    with store._session_factory() as session:
        session.query(AgentMessageModel).filter_by(conversation_id="c-1", role="assistant").delete()
        session.commit()

    state = client.get("/agent/conversations/c-1/share").json()
    assert state["items"] == [{"kind": "run", "id": "run-a", "label": "Alice run", "available": True}]
    bob = _mount(store, _BOB)
    bob.post(f"/agent/shared-conversations/{token}/open")
    assert bob.get("/agent/shared-items/run/run-a").status_code == 200
    assert _share(client, "continue_data", [{"kind": "run", "id": "run-a"}]) == token
    assert bob.get("/agent/shared-items/run/run-a").status_code == 200


def test_shared_list_is_owner_only_with_type_and_items() -> None:
    """The list shows the owner's links with type and items; revoked links drop out."""
    client, store = _client(_ALICE)
    _private_chat(store)
    token = _share(client, "continue_data", [{"kind": "dataset", "id": "ds-a"}])

    listed = client.get("/agent/shared-conversations").json()
    assert [(r["id"], r["token"], r["link_type"]) for r in listed] == [("c-1", token, "continue_data")]
    assert listed[0]["items"] == [{"kind": "dataset", "id": "ds-a", "label": "Alice data", "available": True}]
    assert _mount(store, _BOB).get("/agent/shared-conversations").json() == []
    client.delete("/agent/conversations/c-1/share")
    assert client.get("/agent/shared-conversations").json() == []


def test_share_caps_live_links_per_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """A new link past the cap is a 409; changing an existing link still works."""
    monkeypatch.setattr(agent_history, "MAX_SHARED_CHATS", 1)
    client, store = _client(_ALICE)
    _seed(store, ("c-1", "alice"), ("c-2", "alice"))
    _share(client, "view")

    resp = client.post("/agent/conversations/c-2/share", json={"link_type": "view"})
    assert resp.status_code == 409
    assert resp.json()["code"] == "agent.share.limit_reached"
    _share(client, "continue")


def test_local_mode_cannot_share(monkeypatch: pytest.MonkeyPatch) -> None:
    """In the Mac app a link cannot work for anyone else, so a share is a 404."""
    monkeypatch.setattr(settings, "local_mode", True)
    client, store = _client(_ALICE)
    _seed(store, ("c-1", "alice"))
    assert client.post("/agent/conversations/c-1/share", json={"link_type": "view"}).status_code == 404


def test_purge_keeps_shared_conversations() -> None:
    """An old, unpinned chat with a live link survives the purge; one without goes."""
    client, store = _client(_ALICE)
    _seed(store, ("old-shared", "alice"), ("old-plain", "alice"))
    client.post("/agent/conversations/old-shared/share", json={"link_type": "view"})
    with store._session_factory() as session:
        for row in session.query(AgentConversationModel):
            row.updated_at = datetime.now(UTC) - timedelta(days=400)
        session.commit()

    assert purge_stale_conversations(store.engine, threshold_days=30) == 1
    with store._session_factory() as session:
        assert [r.id for r in session.query(AgentConversationModel)] == ["old-shared"]


_FORMATS = [
    "skynet_pat",
    "github_pat",
    "aws_key_id",
    "jwt",
    "mac_link_access",
    "mac_link_refresh",
    "mcp_access",
    "mcp_refresh",
    "bearer",
]


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
        "mac_link_access": "skyl_at_" + secrets.token_urlsafe(24),
        "mac_link_refresh": "skyl_rt_" + secrets.token_urlsafe(24),
        "mcp_access": "skym_at_" + secrets.token_urlsafe(24),
        "mcp_refresh": "skym_rt_" + secrets.token_urlsafe(24),
        "bearer": "Bearer " + secrets.token_hex(20),
    }


@pytest.mark.parametrize("kind", _FORMATS)
def test_public_read_masks_each_token_format(kind: str) -> None:
    """The public view link masks Skynet, GitHub, AWS and JWT credentials."""
    secret = _secret_samples()[kind]
    client, store = _client(_ALICE)
    _seed(store, ("c-1", "alice"))
    _add_message(store, "c-1", "user", f"use {secret} now")
    token = _share(client, "view")

    resp = _mount(store, None).get(f"/agent/shared-conversations/{token}")
    assert resp.status_code == 200, resp.text
    assert secret not in resp.text
    assert "use " in resp.json()["messages"][0]["content"]


@pytest.mark.parametrize("kind", _FORMATS)
def test_continued_copy_masks_each_token_format(kind: str) -> None:
    """A continued copy stores the chat with every credential format masked."""
    secret = _secret_samples()[kind]
    client, store = _client(_ALICE)
    _seed(store, ("c-1", "alice"))
    _add_message(store, "c-1", "user", f"key {secret}", router_metadata=None)
    _add_message(store, "c-1", "assistant", "ok", router_metadata={"plan": f"use {secret}", "stats": {"cost": 2}})
    token = _share(client, "continue")
    fork_id = _mount(store, _BOB).post(f"/agent/shared-conversations/{token}/open").json()["conversation_id"]

    with store._session_factory() as session:
        rows = session.query(AgentMessageModel).filter_by(conversation_id=fork_id).all()
        assert rows
        assert all(secret not in m.content and secret not in str(m.router_metadata) for m in rows)
        assert all("stats" not in (m.router_metadata or {}) for m in rows)


def test_public_read_masks_emails_and_hides_costs() -> None:
    """A view link hides email addresses, the owner name, costs and thinking."""
    client, store = _client(_ALICE)
    _seed(store, ("c-1", "alice"))
    _add_message(store, "c-1", "user", f"mail owner@example.com key {_FAKE_KEY}")
    _add_message(
        store,
        "c-1",
        "assistant",
        "done",
        router_metadata={"stats": {"cost": 1.5}, "thinking": [{"text": "hmm"}], "served_model": "m-2"},
    )
    token = _share(client, "view")

    resp = _mount(store, None).get(f"/agent/shared-conversations/{token}")
    assert "owner@example.com" not in resp.text
    assert _FAKE_KEY not in resp.text
    assert "alice" not in resp.text
    assistant = resp.json()["messages"][1]
    assert assistant["stats"] is None
    assert assistant["thinking"] is None
    assert assistant["served_model"] == "m-2"


def test_masking_leaves_ordinary_words_alone() -> None:
    """Words that only start like a token prefix stay readable."""
    text = "skydiving, skyline_view_settings, AKIA short and Bearer authentication"
    assert agent_history.mask_secrets(text) == text


def test_public_share_read_is_never_cached() -> None:
    """A revoked link must stop at once, so the public read sends ``no-store``."""
    assert _cache_control_for("/agent/shared-conversations/abc", b"") == "no-store"
    assert _cache_control_for("/agent/conversations/abc", b"") is None

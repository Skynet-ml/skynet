"""Tests for the caller-scoped setup draft endpoints and the upload they keep."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ...i18n_en import ENGLISH_MESSAGES
from ...storage.models import AgentStagedDatasetModel, Base, SetupDraftModel
from ...storage.usage import compute_user_storage, purge_expired_staged_datasets
from ..auth import AuthenticatedUser, get_authenticated_user
from ..routers.setup_draft import SETUP_DRAFT_KEPT_WRITERS, SETUP_DRAFT_MAX_BYTES, create_setup_draft_router

_PATH = "/wizard/setup-draft"
# A left_at after every other draft the tests save.
_LATER = 9_999_999_999_999


def _harness(quota_bytes: int = 10**12) -> tuple[TestClient, Engine, dict[str, str]]:
    """Build a setup draft client over an in-memory database.

    Args:
        quota_bytes: The caller's storage budget.

    Returns:
        The client, the engine, and a holder whose ``user`` names the caller.
    """
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    app = FastAPI()
    job_store = SimpleNamespace(
        engine=engine,
        get_effective_user_storage_quota=lambda _username: quota_bytes,
        compute_user_storage=lambda username: compute_user_storage(engine, username),
    )
    app.include_router(create_setup_draft_router(job_store=job_store))
    caller = {"user": "alice"}
    app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser(
        username=caller["user"], role="user", groups=()
    )
    return TestClient(app), engine, caller


def _draft(**overrides: object) -> dict[str, object]:
    """Return a small valid draft body.

    Args:
        **overrides: Fields to replace.

    Returns:
        The request body.
    """
    body: dict[str, object] = {
        "left_at": 1_700_000_000_000,
        "values": {"skynet.wizard.session.v1": '{"account":"alice"}'},
        "chat": {"id": "setup-1", "kicked": ["task"], "messages": [{"role": "user", "content": "hi"}]},
        "staged_dataset_id": None,
    }
    body.update(overrides)
    return body


def _stage(engine: Engine, staged_id: str, username: str = "alice") -> None:
    """Insert a staged upload older than the sweep's time limit.

    Args:
        engine: The test engine.
        staged_id: Id of the upload.
        username: Owner of the upload.
    """
    with Session(engine) as session:
        session.add(
            AgentStagedDatasetModel(
                id=staged_id,
                username=username,
                dataset_filename="cases.csv",
                rows=[{"q": "a"}],
                row_count=1,
                created_at=datetime.now(UTC) - timedelta(hours=2),
            )
        )
        session.commit()


def _staged_ids(engine: Engine) -> set[str]:
    """Return the ids of the staged uploads that remain.

    Args:
        engine: The test engine.

    Returns:
        The remaining ids.
    """
    with Session(engine) as session:
        return {row.id for row in session.query(AgentStagedDatasetModel).all()}


def _put(client: TestClient, body: dict[str, object], base: int = 0) -> dict[str, object]:
    """Save ``body`` from ``base`` and return the slot.

    Args:
        client: The test client.
        body: The draft.
        base: The revision the caller read.

    Returns:
        The slot JSON.
    """
    return client.put(_PATH, json=body, params={"base_revision": base}).json()


def _delete(client: TestClient, revision: int) -> dict[str, object]:
    """Delete from ``revision`` and return the slot.

    Args:
        client: The test client.
        revision: The revision the caller read.

    Returns:
        The slot JSON.
    """
    return client.delete(_PATH, params={"revision": revision}).json()


def _stored(client: TestClient) -> dict[str, object] | None:
    """Return the stored draft, or ``None``.

    Args:
        client: The test client.

    Returns:
        The draft JSON.
    """
    return client.get(_PATH).json()["draft"]


def test_missing_draft_reads_null_and_put_round_trips() -> None:
    """A new account has an empty slot; a saved draft reads back the same."""
    client, _, _ = _harness()

    assert client.get(_PATH).json() == {"revision": 0, "changed": False, "draft": None, "writer": None, "seq": None}
    saved = client.put(_PATH, json=_draft(), params={"base_revision": 0})

    assert saved.status_code == 200
    assert saved.json() == {"revision": 1, "changed": True, "draft": _draft(), "writer": None, "seq": None}
    assert _stored(client) == _draft()


def test_a_time_past_the_column_range_is_refused_before_the_write() -> None:
    """A left_at too large for BIGINT gets a 422 and leaves the slot empty."""
    client, _, _ = _harness()
    resp = client.put(_PATH, json=_draft(left_at=2**63), params={"base_revision": 0})
    assert resp.status_code == 422
    assert _stored(client) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"values": {"k": "a\x00b"}},
        {"values": {"k": "\ud800"}},
        {"chat": {"id": "c", "messages": [{"content": "\x00"}]}},
        {"chat": {"id": "c", "score": float("nan")}},
        {"staged_dataset_id": "ds\x00"},
    ],
)
def test_text_the_database_cannot_store_is_refused_before_the_write(overrides: dict[str, object]) -> None:
    """A NUL, a lone surrogate or a non-finite number gets a 422 and leaves the slot empty."""
    client, _, _ = _harness()
    # FastAPI's default handler echoes the input, which cannot be encoded; the app's handler does not.
    client.app.add_exception_handler(
        RequestValidationError, lambda _request, _exc: JSONResponse({"detail": "invalid"}, status_code=422)
    )
    body = json.dumps(_draft(**overrides), allow_nan=True)
    resp = client.put(_PATH, content=body, headers={"content-type": "application/json"}, params={"base_revision": 0})
    assert resp.status_code == 422
    assert _stored(client) is None


def test_a_save_from_the_current_revision_replaces_the_draft() -> None:
    """A save that read the current revision replaces the draft; the account keeps one row."""
    client, engine, _ = _harness()
    _put(client, _draft())
    second = _draft(left_at=5, values={"k": "second"}, chat=None)

    assert _put(client, second, base=1) == {
        "revision": 2,
        "changed": True,
        "draft": second,
        "writer": None,
        "seq": None,
    }

    assert _stored(client) == second
    with Session(engine) as session:
        assert session.query(SetupDraftModel).count() == 1


def test_a_stale_save_changes_nothing_whatever_its_clock() -> None:
    """A save from an old revision is refused even with a later left_at, and gets the current slot."""
    client, _, _ = _harness()
    _put(client, _draft(left_at=9_000_000_000_000, values={"k": "fast clock"}))
    _put(client, _draft(left_at=1, values={"k": "slow clock"}), base=1)

    late = _put(client, _draft(left_at=_LATER, values={"k": "stale"}), base=1)

    assert late["changed"] is False
    assert late["revision"] == 2
    assert late["draft"]["values"] == {"k": "slow clock"}
    assert _stored(client)["values"] == {"k": "slow clock"}


def test_draft_is_scoped_to_the_caller() -> None:
    """Another account cannot read or delete the caller's draft."""
    client, _, caller = _harness()
    _put(client, _draft())

    caller["user"] = "bob"
    assert _stored(client) is None
    assert _delete(client, 0)["changed"] is True

    caller["user"] = "alice"
    assert _stored(client) == _draft()


def test_delete_removes_the_draft_and_its_chat() -> None:
    """Delete keeps only a mark with no setup, so the chat goes."""
    client, engine, _ = _harness()
    _put(client, _draft())

    assert _delete(client, 1) == {"revision": 2, "changed": True, "draft": None, "writer": None, "seq": None}

    assert client.get(_PATH).json() == {"revision": 2, "changed": False, "draft": None, "writer": None, "seq": None}
    with Session(engine) as session:
        row = session.get(SetupDraftModel, "alice")
        assert (row.deleted, row.draft_values, row.chat, row.revision) == (True, {}, None, 2)


def test_a_delete_keeps_a_draft_saved_after_its_read() -> None:
    """A delete from an old revision leaves the newer draft; a delete from the current one removes it."""
    client, _, _ = _harness()
    _put(client, _draft(values={"k": "first"}))
    _put(client, _draft(values={"k": "other device"}), base=1)

    assert _delete(client, 1)["changed"] is False
    assert _stored(client)["values"] == {"k": "other device"}

    assert _delete(client, 2)["changed"] is True
    assert _stored(client) is None


def test_a_save_sent_before_a_delete_cannot_bring_the_setup_back() -> None:
    """A save from before a delete that lands after it is refused; a save that read the delete is kept."""
    client, _, _ = _harness()
    _put(client, _draft())
    _delete(client, 1)

    late = _put(client, _draft(), base=1)

    assert late == {"revision": 2, "changed": False, "draft": None, "writer": None, "seq": None}
    assert _stored(client) is None
    assert _put(client, _draft(left_at=1), base=2)["changed"] is True


def _send_copy(client: TestClient, body: dict[str, object], base: int, copy_base: int, **save: object) -> dict:
    """Send a browser copy again from ``base`` and return the slot.

    Args:
        client: The test client.
        body: The copy.
        base: The revision the load read.
        copy_base: The revision the copy builds on.
        **save: The copy's ``writer`` and ``seq``, when it has them.

    Returns:
        The slot JSON.
    """
    params = {"base_revision": base, "copy_base": copy_base, **save}
    return client.put(_PATH, json=body, params=params).json()


def test_a_copy_whose_save_landed_is_refused_after_a_later_edit() -> None:
    """The page's save landed, another device edited after it: the copy sent again changes nothing."""
    client, _, _ = _harness()
    copy = _draft(values={"k": "page A"})
    client.put(_PATH, json=copy, params={"base_revision": 0, "writer": "page-a", "seq": 3})
    _put(client, _draft(values={"k": "device B"}), base=1)

    refused = _send_copy(client, copy, base=2, copy_base=0, writer="page-a", seq=3)

    assert (refused["changed"], refused["revision"]) == (False, 2)
    assert _stored(client)["values"] == {"k": "device B"}
    assert _send_copy(client, copy, base=2, copy_base=0, writer="page-a", seq=2)["changed"] is False


def test_a_copy_that_never_landed_goes_on_top_of_a_later_edit() -> None:
    """The page's save never landed, so its copy wins over another device's later edit."""
    client, _, _ = _harness()
    client.put(_PATH, json=_draft(), params={"base_revision": 0, "writer": "page-a", "seq": 1})
    _put(client, _draft(values={"k": "device B"}), base=1)
    copy = _draft(values={"k": "page A"})

    assert _send_copy(client, copy, base=2, copy_base=1, writer="page-a", seq=2)["changed"] is True
    assert _stored(client)["values"] == {"k": "page A"}


def test_the_slot_names_the_page_and_count_of_the_save_that_made_it() -> None:
    """A page can tell its own earlier save from another page's setup; a delete or an unmarked save names none."""
    client, _, _ = _harness()
    made = client.put(_PATH, json=_draft(), params={"base_revision": 0, "writer": "page-a", "seq": 2}).json()
    assert (made["writer"], made["seq"]) == ("page-a", 2)
    stale = client.put(_PATH, json=_draft(), params={"base_revision": 0, "writer": "page-b", "seq": 1}).json()
    assert (stale["changed"], stale["writer"], stale["seq"]) == (False, "page-a", 2)
    assert client.get(_PATH).json()["writer"] == "page-a"

    unmarked = _put(client, _draft(values={"k": "device B"}), base=1)
    assert (unmarked["writer"], unmarked["seq"]) == (None, None)
    client.put(_PATH, json=_draft(), params={"base_revision": 2, "writer": "page-a", "seq": 3})
    deleted = _delete(client, 3)
    assert (deleted["writer"], deleted["seq"]) == (None, None)


def test_a_copy_is_refused_after_a_delete_newer_than_its_base() -> None:
    """A submit or Start new after the copy's base keeps the slot empty; a delete the copy read does not."""
    client, _, _ = _harness()
    _put(client, _draft())
    _delete(client, 1)

    assert _send_copy(client, _draft(), base=2, copy_base=1, writer="page-a", seq=1)["changed"] is False
    assert _stored(client) is None
    assert _send_copy(client, _draft(), base=2, copy_base=2, writer="page-a", seq=1)["changed"] is True


def test_a_copy_older_than_a_delete_is_refused_after_a_new_save() -> None:
    """A new setup saved after a submit does not let an older copy bring the submitted setup back."""
    client, _, _ = _harness()
    client.put(_PATH, params={"base_revision": 0, "writer": "page-a", "seq": 1}, json=_draft())
    _delete(client, 1)
    client.put(_PATH, params={"base_revision": 2, "writer": "page-b", "seq": 1}, json=_draft(values={"k": "new"}))

    assert (
        _send_copy(client, _draft(values={"k": "old"}), base=3, copy_base=1, writer="page-a", seq=2)["changed"] is False
    )
    assert _send_copy(client, _draft(values={"k": "old"}), base=3, copy_base=1)["changed"] is False
    assert _stored(client)["values"] == {"k": "new"}


def test_a_delete_keeps_the_saves_and_the_list_keeps_recent_pages() -> None:
    """Each page keeps one entry with its highest count, and only the latest pages stay."""
    client, engine, _ = _harness()
    client.put(_PATH, json=_draft(), params={"base_revision": 0, "writer": "page-0", "seq": 5})
    client.put(_PATH, json=_draft(), params={"base_revision": 1, "writer": "page-0", "seq": 4})
    _delete(client, 2)
    with Session(engine) as session:
        assert session.get(SetupDraftModel, "alice").saves == [["page-0", 5, 2]]

    for n in range(1, SETUP_DRAFT_KEPT_WRITERS + 1):
        client.put(_PATH, json=_draft(), params={"base_revision": n + 2, "writer": f"page-{n}", "seq": 1})
    with Session(engine) as session:
        row = session.get(SetupDraftModel, "alice")
        saves, evicted = row.saves, row.evicted_revision
    assert len(saves) == SETUP_DRAFT_KEPT_WRITERS
    assert saves[0] == ["page-1", 1, 4]
    assert evicted == 2


def test_a_copy_from_a_page_that_left_the_list_is_refused_when_it_may_have_landed() -> None:
    """A page that left the list may have landed its save; its copy from before that revision changes nothing."""
    client, _, _ = _harness()
    copy = _draft(values={"k": "page A"})
    client.put(_PATH, json=copy, params={"base_revision": 0, "writer": "page-a", "seq": 1})
    for n in range(1, SETUP_DRAFT_KEPT_WRITERS + 1):
        body = _draft(values={"k": f"device B {n}"})
        client.put(_PATH, json=body, params={"base_revision": n, "writer": f"page-b{n}", "seq": 1})
    revision = SETUP_DRAFT_KEPT_WRITERS + 1

    assert _send_copy(client, copy, base=revision, copy_base=0, writer="page-a", seq=1)["changed"] is False
    assert _send_copy(client, copy, base=revision, copy_base=0)["changed"] is False
    assert _stored(client)["values"] == {"k": f"device B {SETUP_DRAFT_KEPT_WRITERS}"}
    assert _send_copy(client, copy, base=revision, copy_base=1, writer="page-new", seq=1)["changed"] is True


def test_a_delete_before_the_first_save_arrives_refuses_that_save() -> None:
    """A delete with no stored draft still refuses a save sent before it."""
    client, _, _ = _harness()
    _delete(client, 0)

    assert _put(client, _draft())["changed"] is False
    assert _stored(client) is None


def test_large_chat_loses_its_oldest_messages_first() -> None:
    """A draft over the cap keeps the setup and the newest chat messages."""
    client, _, _ = _harness()
    text = "x" * 60_000
    messages = [{"role": "user", "content": f"{index}:{text}"} for index in range(200)]

    stored = _put(client, _draft(chat={"id": "setup-1", "kicked": [], "messages": messages}))["draft"]

    kept = stored["chat"]["messages"]
    assert 0 < len(kept) < len(messages)
    assert kept == messages[-len(kept) :]
    assert stored["values"] == _draft()["values"]
    stored_size = len(json.dumps(stored["values"]).encode()) + len(json.dumps(stored["chat"]).encode())
    assert stored_size <= SETUP_DRAFT_MAX_BYTES


def test_setup_over_the_cap_without_chat_is_rejected() -> None:
    """When the setup alone is too large, the save fails and stores nothing."""
    client, _, _ = _harness()

    response = client.put(
        _PATH, json=_draft(values={"k": "x" * (SETUP_DRAFT_MAX_BYTES + 1)}), params={"base_revision": 0}
    )

    assert response.status_code == 413
    assert response.json()["detail"] == ENGLISH_MESSAGES["wizard.setup_draft.too_large"]
    assert _stored(client) is None


def test_sweep_keeps_an_upload_the_draft_uses() -> None:
    """The sweep keeps the caller's referenced upload and purges the others."""
    client, engine, _ = _harness()
    _stage(engine, "kept")
    _stage(engine, "loose")
    _put(client, _draft(staged_dataset_id="kept"))

    assert purge_expired_staged_datasets(engine, max_age_seconds=600) == 1

    assert _staged_ids(engine) == {"kept"}


def test_another_accounts_draft_does_not_keep_an_upload() -> None:
    """A draft keeps only its own account's upload."""
    client, engine, caller = _harness()
    _stage(engine, "alice-upload", username="alice")
    caller["user"] = "bob"
    _put(client, _draft(staged_dataset_id="alice-upload"))

    purge_expired_staged_datasets(engine, max_age_seconds=600)

    assert _staged_ids(engine) == set()


def test_delete_or_replace_releases_the_upload() -> None:
    """After the draft drops its upload, the next sweep purges it."""
    client, engine, _ = _harness()
    _stage(engine, "first")
    _stage(engine, "second")
    _put(client, _draft(staged_dataset_id="first"))
    _put(client, _draft(staged_dataset_id="second"), base=1)

    purge_expired_staged_datasets(engine, max_age_seconds=600)
    assert _staged_ids(engine) == {"second"}

    _delete(client, 2)
    purge_expired_staged_datasets(engine, max_age_seconds=600)
    assert _staged_ids(engine) == set()


def test_the_draft_counts_in_the_storage_budget() -> None:
    """The draft's bytes count in the caller's storage; a deleted draft counts almost nothing."""
    client, engine, _ = _harness()
    _put(client, _draft(values={"k": "x" * 5000}, chat=None))

    usage = compute_user_storage(engine, "alice")

    assert usage.total >= 5000
    assert usage.breakdown["staged_uploads"] == usage.total
    _delete(client, 1)
    assert compute_user_storage(engine, "alice").total < 10


def test_a_save_over_the_storage_budget_is_refused() -> None:
    """A larger draft past the budget gets the shared quota error; a smaller one still saves."""
    client, _, _ = _harness(quota_bytes=4000)
    _put(client, _draft(values={"k": "x" * 3000}, chat=None))

    over = client.put(_PATH, json=_draft(values={"k": "x" * 5000}, chat=None), params={"base_revision": 1})

    assert over.status_code == 409
    assert over.json()["detail"] == ENGLISH_MESSAGES["user.storage.quota_exceeded"].format(used_mb=0.0, quota_mb=0.0)
    assert _stored(client)["values"] == {"k": "x" * 3000}
    assert _put(client, _draft(values={"k": "small"}, chat=None), base=1)["changed"] is True

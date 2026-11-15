"""Tests for the agent chat media routes (SKY-707).

Covers type sniffing from file bytes, the PDF, DOCX and XLSX originals
(SKY-735), the size limit for each kind, the
storage quota, owner-only reads and deletes, the response headers, the
sweep of unsent files, the delete of a chat's files with the chat, and the
media loader that the generalist route uses. The routes run over an
in-memory SQLite job store, with the auth dependency overridden.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import fakeredis
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, event, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ...service_gateway.agents.attachments import MAX_ATTACHMENT_BYTES
from ...storage.models import AgentConversationModel, AgentMessageModel, Base, ChatMediaModel
from ...storage.remote import RemoteDBJobStore
from ...storage.usage import UNSENT_MEDIA_RETENTION
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from ..routers import chat_media
from ..routers.chat_media import (
    attach_media_to_chat,
    create_chat_media_router,
    load_media_data,
    load_owned_media,
    sniff_media,
)
from ..routers.generalist_agent import _persist_user_turn

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_ALICE = "alice@example.com"
_BOB = "bob@example.com"


def _foreign_keys_on(dbapi_connection: object, _record: object) -> None:
    """Turn on SQLite foreign keys, so a chat delete cascades as in Postgres.

    Args:
        dbapi_connection: The new DB-API connection.
        _record: The pool record (unused).
    """
    dbapi_connection.execute("PRAGMA foreign_keys=ON")  # type: ignore[attr-defined]


class _SQLiteJobStore(RemoteDBJobStore):
    """RemoteDBJobStore on in-memory SQLite (skips the pgvector bootstrap)."""

    def __init__(self) -> None:
        """Build an in-memory SQLite engine with foreign keys on, and create the ORM tables."""
        self._engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        event.listen(self._engine, "connect", _foreign_keys_on)
        Base.metadata.create_all(self._engine)
        self._session_factory = sessionmaker(bind=self._engine)


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """Mount the media router over SQLite, signed in as Alice.

    Args:
        monkeypatch: Pytest fixture used to swap in a fake Redis.

    Yields:
        A namespace with ``client``, ``store`` and ``user`` (a one-item list
        whose username the auth override returns).
    """
    redis = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(chat_media, "shared_redis_client", lambda: redis)
    store = _SQLiteJobStore()
    app = FastAPI()

    @app.exception_handler(DomainError)
    async def _domain_error(request: Request, exc: DomainError) -> JSONResponse:
        """Answer a domain error with its status and code.

        Args:
            request: The request (unused).
            exc: The domain error.

        Returns:
            The JSON error response.
        """
        return JSONResponse(status_code=exc.status_code, content={"code": exc.code})

    user = [_ALICE]
    app.include_router(create_chat_media_router(job_store=store))
    app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser(
        username=user[0], role="user", groups=()
    )
    yield SimpleNamespace(client=TestClient(app), store=store, user=user)
    Base.metadata.drop_all(store.engine)


def _upload(client: TestClient, data: bytes, name: str = "shot.png", content_type: str = "image/png") -> object:
    """POST one file to the upload route.

    Args:
        client: The test client.
        data: The file bytes.
        name: The upload filename.
        content_type: The upload header content type.

    Returns:
        The response.
    """
    return client.post("/agent/media", files={"file": (name, data, content_type)})


@pytest.mark.parametrize(
    ("head", "declared", "expected"),
    [
        (_PNG, "", ("image", "image/png")),
        (b"\xff\xd8\xff\xe0" + b"\x00" * 16, "", ("image", "image/jpeg")),
        (b"GIF89a" + b"\x00" * 16, "", ("image", "image/gif")),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "", ("image", "image/webp")),
        (b"RIFF\x00\x00\x00\x00WAVEfmt ", "", ("audio", "audio/wav")),
        (b"\x00\x00\x00\x18ftypisom" + b"\x00" * 8, "", ("video", "video/mp4")),
        (b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 8, "", ("video", "video/quicktime")),
        (b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 8, "", ("audio", "audio/mp4")),
        (b"\x00\x00\x00\x18ftypisom" + b"\x00" * 8, "audio/mp4", ("audio", "audio/mp4")),
        (b"\x1a\x45\xdf\xa3" + b"\x00" * 16, "video/webm", ("video", "video/webm")),
        (b"\x1a\x45\xdf\xa3" + b"\x00" * 16, "audio/webm", ("audio", "audio/webm")),
        (b"OggS" + b"\x00" * 16, "", ("audio", "audio/ogg")),
        (b"fLaC" + b"\x00" * 16, "", ("audio", "audio/flac")),
        (b"ID3\x04" + b"\x00" * 16, "", ("audio", "audio/mpeg")),
        (b"\xff\xfb\x90\x00" + b"\x00" * 16, "", ("audio", "audio/mpeg")),
        (b"\xff\xf1\x50\x80" + b"\x00" * 16, "", ("audio", "audio/aac")),
        (_PDF, "", ("document", "application/pdf")),
        (b"PK\x03\x04" + b"\x00" * 16, "", ("document", "application/zip")),
    ],
)
def test_sniff_media_reads_the_type_from_the_bytes(head: bytes, declared: str, expected: tuple[str, str]) -> None:
    """Each allowed format is found from its first bytes."""
    sniffed = sniff_media(head, declared)
    assert sniffed is not None
    assert (sniffed.kind, sniffed.mime_type) == expected


@pytest.mark.parametrize(
    "head",
    [
        b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"/>',
        b"\x00\x00\x00\x18ftypheic" + b"\x00" * 8,
        b"\x00\x00\x00\x1cftypavif" + b"\x00" * 8,
        b"<html><body>hi</body></html>",
        "Hello".encode("utf-16"),
        b"",
    ],
)
def test_sniff_media_rejects_types_off_the_allow_list(head: bytes) -> None:
    """SVG, HEIC, AVIF, HTML, UTF-16 text and empty files are not media."""
    assert sniff_media(head, "image/png") is None


def test_upload_stores_the_file_for_the_caller(harness: SimpleNamespace) -> None:
    """An upload stores the sniffed type, not the header type, under the caller."""
    resp = _upload(harness.client, _PNG, name="../../etc/shot.png", content_type="application/octet-stream")

    assert resp.status_code == 201
    body = resp.json()
    assert body["kind"] == "image"
    assert body["mime_type"] == "image/png"
    assert body["byte_size"] == len(_PNG)
    assert body["name"] == "shot.png"
    with Session(harness.store.engine) as session:
        row = session.get(ChatMediaModel, body["id"])
        assert row is not None
        assert row.username == _ALICE
        assert row.data == _PNG


def _zip(*parts: str) -> bytes:
    """Build a zip file that holds the given part names.

    Args:
        *parts: The part names, each with a tiny XML body.

    Returns:
        The zip bytes.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for part in parts:
            archive.writestr(part, "<x/>")
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("data", "name", "mime"),
    [
        (_PDF, "brief.pdf", "application/pdf"),
        (_zip("[Content_Types].xml", "word/document.xml"), "brief.docx", _DOCX_MIME),
        (_zip("[Content_Types].xml", "xl/workbook.xml"), "budget.xlsx", _XLSX_MIME),
    ],
)
def test_upload_keeps_a_document_original_and_serves_its_real_type(
    harness: SimpleNamespace, data: bytes, name: str, mime: str
) -> None:
    """A PDF, DOCX or XLSX is stored as a document and read back with its own type and safe headers."""
    body = _upload(harness.client, data, name=name, content_type="application/octet-stream").json()

    assert (body["kind"], body["mime_type"], body["name"]) == ("document", mime, name)
    resp = harness.client.get(f"/agent/media/{body['id']}")
    assert resp.status_code == 200
    assert resp.content == data
    assert resp.headers["content-type"] == mime
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in resp.headers["content-security-policy"]
    harness.user[0] = _BOB
    assert harness.client.get(f"/agent/media/{body['id']}").status_code == 404


@pytest.mark.parametrize(
    "data",
    [_zip("ppt/presentation.xml"), _zip("payload.js"), b"PK\x03\x04 not a real zip"],
)
def test_upload_rejects_a_zip_that_is_not_docx_or_xlsx(harness: SimpleNamespace, data: bytes) -> None:
    """A PPTX, any other zip, or a broken zip gets a 415 and stores nothing."""
    assert _upload(harness.client, data, name="x.docx").status_code == 415
    with Session(harness.store.engine) as session:
        assert session.scalars(select(ChatMediaModel)).all() == []


@pytest.mark.parametrize(
    ("data", "name"),
    [
        (_zip("[Content_Types].xml", "word/document.xml"), "brief.pdf"),
        (_PDF, "brief.docx"),
        (_zip("xl/workbook.xml"), "budget"),
    ],
)
def test_upload_rejects_a_document_named_as_another_type(harness: SimpleNamespace, data: bytes, name: str) -> None:
    """The viewer draws an original by its name's extension, so a name that hides the real type gets a 415."""
    assert _upload(harness.client, data, name=name, content_type="application/octet-stream").status_code == 415
    with Session(harness.store.engine) as session:
        assert session.scalars(select(ChatMediaModel)).all() == []


def test_upload_rejects_a_zip_whose_directory_passes_the_cap(harness: SimpleNamespace) -> None:
    """A zip of many small entries gets a 415 before its directory is parsed."""
    names = ["word/document.xml", *(f"pad/{n:08d}-{'x' * 40}" for n in range(12_000))]
    data = _zip(*names)
    assert len(data) < chat_media.MAX_MEDIA_BYTES["document"]
    assert _upload(harness.client, data, name="big.docx").status_code == 415


def test_document_original_has_its_own_hourly_cap(harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """A document original does not use the cap that its file's text read already counted against."""
    monkeypatch.setattr(chat_media.settings, "rate_limit_attachments_per_hour", 1)

    assert _upload(harness.client, _PNG).status_code == 201
    assert _upload(harness.client, _PNG).status_code == 429
    assert _upload(harness.client, _PDF, name="a.pdf", content_type="application/pdf").status_code == 201
    assert _upload(harness.client, _PDF, name="b.pdf", content_type="application/pdf").status_code == 429


def test_document_limit_matches_the_file_text_limit() -> None:
    """Every file whose text the chat can read can keep its original."""
    assert chat_media.MAX_MEDIA_BYTES["document"] == MAX_ATTACHMENT_BYTES


def test_safe_filename_drops_paths_quotes_and_control_characters() -> None:
    """A stored filename keeps no path, quote or control character, and is never empty."""
    assert chat_media._safe_filename('C:\\x\\a"b\x00\nc.png') == "abc.png"
    assert chat_media._safe_filename("///") == "file"
    assert chat_media._safe_filename(None) == "file"
    assert len(chat_media._safe_filename("a" * 400)) == 255


def test_upload_rejects_svg_sent_as_png(harness: SimpleNamespace) -> None:
    """An SVG with an image/png header gets a 415 and stores nothing."""
    resp = _upload(harness.client, b'<svg xmlns="http://www.w3.org/2000/svg"/>', name="x.png")

    assert resp.status_code == 415
    with Session(harness.store.engine) as session:
        assert session.scalars(select(ChatMediaModel)).all() == []


def test_image_limit_stays_under_the_provider_base64_limit() -> None:
    """The largest image, as base64, stays under the 5 MiB provider limit, and the turn budget holds several."""
    assert chat_media.MAX_MEDIA_BYTES["image"] * 4 / 3 < 5 * 1024 * 1024
    assert chat_media.MAX_MODEL_IMAGE_BYTES // chat_media.MAX_MEDIA_BYTES["image"] >= 4


def test_upload_enforces_the_limit_for_each_kind(harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """A file over its kind's limit gets a 413; a file at the limit is stored."""
    monkeypatch.setitem(chat_media.MAX_MEDIA_BYTES, "image", len(_PNG))

    assert _upload(harness.client, _PNG).status_code == 201
    assert _upload(harness.client, _PNG + b"\x00").status_code == 413


def test_upload_respects_the_storage_quota(harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """An upload that would cross the storage quota gets a 409."""
    monkeypatch.setattr(harness.store, "get_effective_user_storage_quota", lambda username: len(_PNG) - 1)

    resp = _upload(harness.client, _PNG)

    assert resp.status_code == 409
    assert resp.json()["code"] == "user.storage.quota_exceeded"


def _media_row(media_id: str, owner: str, created_at: datetime, conversation_id: str | None = None) -> ChatMediaModel:
    """Build one stored media row.

    Args:
        media_id: The row id.
        owner: The owner's username.
        created_at: The upload time.
        conversation_id: The chat that sent the file, if any.

    Returns:
        The unsaved row.
    """
    return ChatMediaModel(
        id=media_id,
        username=owner,
        filename="f.png",
        kind="image",
        mime_type="image/png",
        byte_size=1,
        data=b"x",
        created_at=created_at,
        conversation_id=conversation_id,
    )


def _conversation(conversation_id: str, owner: str) -> AgentConversationModel:
    """Build one stored chat header.

    Args:
        conversation_id: The chat id.
        owner: The owner's username.

    Returns:
        The unsaved row.
    """
    now = datetime.now(UTC)
    return AgentConversationModel(
        id=conversation_id, username=owner, title="t", pinned=False, created_at=now, updated_at=now
    )


def test_upload_drops_the_callers_old_unsent_media(harness: SimpleNamespace) -> None:
    """An upload removes the caller's old unsent media, and keeps sent media and other users' media."""
    old = datetime.now(UTC) - UNSENT_MEDIA_RETENTION - timedelta(hours=1)
    with Session(harness.store.engine) as session:
        session.add(_conversation("chat-a", _ALICE))
        session.flush()
        session.add_all(
            [
                _media_row("old-a", _ALICE, old),
                _media_row("old-b", _BOB, old),
                _media_row("sent-a", _ALICE, old, conversation_id="chat-a"),
            ]
        )
        session.commit()

    assert _upload(harness.client, _PNG).status_code == 201

    with Session(harness.store.engine) as session:
        assert session.get(ChatMediaModel, "old-a") is None
        assert session.get(ChatMediaModel, "old-b") is not None
        assert session.get(ChatMediaModel, "sent-a") is not None


def test_stale_unsent_media_never_blocks_an_upload(harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """The quota check sweeps old unsent files first, so they never fill the quota."""
    old = datetime.now(UTC) - UNSENT_MEDIA_RETENTION - timedelta(hours=1)
    stale = _media_row("old-a", _ALICE, old)
    stale.byte_size = 10_000
    with Session(harness.store.engine) as session:
        session.add(stale)
        session.commit()
    monkeypatch.setattr(harness.store, "get_effective_user_storage_quota", lambda username: len(_PNG))

    assert _upload(harness.client, _PNG).status_code == 201

    with Session(harness.store.engine) as session:
        assert session.get(ChatMediaModel, "old-a") is None


def test_reading_a_file_sweeps_nothing(harness: SimpleNamespace) -> None:
    """A GET only reads, so a stale unsent file stays until a storage measure sweeps it."""
    media_id = _upload(harness.client, _PNG).json()["id"]
    old = datetime.now(UTC) - UNSENT_MEDIA_RETENTION - timedelta(hours=1)
    with Session(harness.store.engine) as session:
        session.add(_media_row("old-a", _ALICE, old))
        session.commit()

    assert harness.client.get(f"/agent/media/{media_id}").status_code == 200

    with Session(harness.store.engine) as session:
        assert session.get(ChatMediaModel, "old-a") is not None


def test_deleting_a_chat_deletes_its_media(harness: SimpleNamespace) -> None:
    """The foreign key deletes a chat's files with the chat, and keeps unsent files."""
    now = datetime.now(UTC)
    with Session(harness.store.engine) as session:
        session.add(_conversation("chat-a", _ALICE))
        session.flush()
        session.add_all([_media_row("sent", _ALICE, now, "chat-a"), _media_row("unsent", _ALICE, now)])
        session.commit()
        session.delete(session.get(AgentConversationModel, "chat-a"))
        session.commit()

    with Session(harness.store.engine) as session:
        assert session.get(ChatMediaModel, "sent") is None
        assert session.get(ChatMediaModel, "unsent") is not None


def test_owner_reads_the_file_with_safe_headers(harness: SimpleNamespace) -> None:
    """The owner gets the bytes with the sniffed type and no-sniff, sandbox and private-cache headers."""
    media_id = _upload(harness.client, _PNG, content_type="text/html").json()["id"]

    resp = harness.client.get(f"/agent/media/{media_id}")

    assert resp.status_code == 200
    assert resp.content == _PNG
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in resp.headers["content-security-policy"]
    assert resp.headers["cache-control"].startswith("private")
    assert resp.headers["content-disposition"].startswith("inline;")


def test_other_user_cannot_read_or_delete_the_file(harness: SimpleNamespace) -> None:
    """Another user gets the same 404 as for an unknown id, and the file stays."""
    media_id = _upload(harness.client, _PNG).json()["id"]
    harness.user[0] = _BOB

    assert harness.client.get(f"/agent/media/{media_id}").status_code == 404
    assert harness.client.get("/agent/media/unknown-id").status_code == 404
    assert harness.client.delete(f"/agent/media/{media_id}").status_code == 404

    harness.user[0] = _ALICE
    assert harness.client.get(f"/agent/media/{media_id}").status_code == 200


def test_owner_deletes_the_file(harness: SimpleNamespace) -> None:
    """The owner's delete removes the file; a second delete gets a 404."""
    media_id = _upload(harness.client, _PNG).json()["id"]

    assert harness.client.delete(f"/agent/media/{media_id}").status_code == 204
    assert harness.client.get(f"/agent/media/{media_id}").status_code == 404
    assert harness.client.delete(f"/agent/media/{media_id}").status_code == 404


def test_delete_leaves_a_file_that_a_chat_holds(harness: SimpleNamespace) -> None:
    """A delete removes only unsent files, so it never takes a file from a stored turn."""
    media_id = _upload(harness.client, _PNG).json()["id"]
    with Session(harness.store.engine) as session:
        session.add(_conversation("chat-a", _ALICE))
        session.flush()
        attach_media_to_chat(session, harness.store, _ALICE, [media_id], "chat-a")
        session.commit()

    assert harness.client.delete(f"/agent/media/{media_id}").status_code == 404
    assert harness.client.get(f"/agent/media/{media_id}").status_code == 200


def test_attach_refuses_copies_over_the_checked_bytes(harness: SimpleNamespace) -> None:
    """A file that another chat took after the quota check fails the attach with a 409."""
    media_id = _upload(harness.client, _PNG).json()["id"]
    with Session(harness.store.engine) as session:
        session.add_all([_conversation("chat-a", _ALICE), _conversation("chat-b", _ALICE)])
        session.flush()
        attach_media_to_chat(session, harness.store, _ALICE, [media_id], "chat-a")
        session.commit()
    with Session(harness.store.engine) as session, pytest.raises(DomainError) as caught:
        attach_media_to_chat(session, harness.store, _ALICE, [media_id], "chat-b", 0)
    assert (caught.value.status_code, caught.value.code) == (409, "agent.media.changed")
    with Session(harness.store.engine) as session:
        held = attach_media_to_chat(session, harness.store, _ALICE, [media_id], "chat-b", len(_PNG))
        assert held[media_id] != media_id


def test_load_owned_media_keeps_order_and_checks_every_id(harness: SimpleNamespace) -> None:
    """The loader returns rows in request order, once each, and refuses any id the caller does not own."""
    first = _upload(harness.client, _PNG).json()["id"]
    second = _upload(harness.client, _PNG).json()["id"]
    harness.user[0] = _BOB
    bobs = _upload(harness.client, _PNG).json()["id"]
    engine = harness.store.engine

    rows = load_owned_media(engine, _ALICE, [second, first, second])
    assert [row.id for row in rows] == [second, first]
    assert all("data" in sa_inspect(row).unloaded for row in rows)
    assert load_media_data(engine, [second]) == {second: _PNG}
    assert load_owned_media(engine, _ALICE, []) == []
    with pytest.raises(HTTPException) as caught:
        load_owned_media(engine, _ALICE, [first, bobs])
    assert caught.value.status_code == 404


def test_file_deleted_during_the_copy_quota_check_is_a_404(
    harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file that the quota measure deletes fails the attach visibly; the session loaded no stale row."""
    media_id = _upload(harness.client, _PNG).json()["id"]
    engine = harness.store.engine
    with Session(engine) as session:
        session.add_all(
            [
                AgentConversationModel(id="chat-a", username=_ALICE, title="a"),
                AgentConversationModel(id="chat-b", username=_ALICE, title="b"),
            ]
        )
        session.commit()
        attach_media_to_chat(session, harness.store, _ALICE, [media_id], "chat-a")
        session.commit()

    def sweeping_quota(_store: object, _username: str, _incoming: int) -> None:
        """Delete the file in another transaction, as a storage measure sweep does."""
        with Session(engine) as other:
            other.execute(delete(ChatMediaModel).where(ChatMediaModel.id == media_id))
            other.commit()

    monkeypatch.setattr(chat_media, "enforce_storage_quota", sweeping_quota)
    with Session(engine) as session, pytest.raises(HTTPException) as caught:
        attach_media_to_chat(session, harness.store, _ALICE, [media_id], "chat-b")
    assert caught.value.status_code == 404


def test_attach_leaves_out_an_id_with_no_file(harness: SimpleNamespace) -> None:
    """A deleted file is left out of the map, so a fork of a turn that names it still works."""
    kept = _upload(harness.client, _PNG).json()["id"]
    gone = _upload(harness.client, _PNG).json()["id"]
    engine = harness.store.engine
    with Session(engine) as session:
        session.add(AgentConversationModel(id="chat-a", username=_ALICE, title="a"))
        session.execute(delete(ChatMediaModel).where(ChatMediaModel.id == gone))
        session.commit()
        held = attach_media_to_chat(session, harness.store, _ALICE, [kept, gone], "chat-a")
    assert held == {kept: kept}


def test_send_with_a_file_deleted_after_the_load_is_a_404(harness: SimpleNamespace) -> None:
    """A file deleted after the send loaded it fails the send; no turn names a missing file."""
    media_id = _upload(harness.client, _PNG).json()["id"]
    engine = harness.store.engine
    with Session(engine) as session:
        session.add(AgentConversationModel(id="chat-a", username=_ALICE, title="a"))
        session.commit()
    rows = load_owned_media(engine, _ALICE, [media_id])
    with Session(engine) as session:
        session.execute(delete(ChatMediaModel).where(ChatMediaModel.id == media_id))
        session.commit()
    with pytest.raises(HTTPException) as caught:
        _persist_user_turn(harness.store, "chat-a", "look", media=rows)
    assert caught.value.status_code == 404
    with Session(engine) as session:
        assert session.scalars(select(AgentMessageModel)).all() == []


def test_the_content_type_column_holds_every_office_type() -> None:
    """Check that Postgres can store the DOCX and XLSX types, which SQLite never checks."""
    width = ChatMediaModel.__table__.c.mime_type.type.length
    assert width >= max(len(_DOCX_MIME), len(_XLSX_MIME))

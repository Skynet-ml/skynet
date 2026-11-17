"""Store images, video, audio and documents attached in agent chats (SKY-707). [INTERNAL]

``POST /agent/media`` takes one file, finds its real type from its first
bytes, and stores it for the caller. ``GET /agent/media/{media_id}`` returns
the bytes, and ``DELETE /agent/media/{media_id}`` removes them; both answer
only to the owner. Another user's id gets the same 404 as an unknown id, so
an id never confirms that a file exists.

The upload header's content type is never trusted. As media, SVG, HEIC and
every type that is not on the allow-list get a 415, because a browser can run
script in SVG and a model provider cannot read HEIC.

A ``document`` is the original bytes of an attached file (SKY-735). The agent
reads the file's extracted text, which travels in the message; the original
exists only so the chat viewer can show the real file. An upload marked
``original`` keeps any file (SKY-782): a PDF, DOCX or XLSX keeps its real
type, which the viewer draws, and every other file is stored as opaque bytes.
"""

from __future__ import annotations

import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import delete, insert, literal, select
from sqlalchemy.orm import Session, defer

from ...config import settings
from ...service_gateway.agents.attachments import MAX_ATTACHMENT_BYTES
from ...storage.models import ChatMediaModel
from ...storage.usage import sweep_unsent_chat_media
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from ..rate_limit import RateLimiter, shared_redis_client
from ._helpers import enforce_storage_quota

# Anthropic refuses an image whose base64 form passes 5 MiB, and base64 adds
# a third, so 3 MiB keeps every image under that limit with margin.
MAX_MEDIA_BYTES: dict[str, int] = {
    "image": 3 * 1024 * 1024,
    "audio": 20 * 1024 * 1024,
    "video": 50 * 1024 * 1024,
    # The same limit as the text read of an attached file, so every file
    # whose text the chat holds can keep its original too.
    "document": MAX_ATTACHMENT_BYTES,
}
MAX_MEDIA_PER_MESSAGE = 10
# The image bytes one turn sends to the model. Gemini refuses a request over
# 20 MB of inline data and Anthropic one over 32 MB; images past this budget
# go to the model as reference-only files.
MAX_MODEL_IMAGE_BYTES = 12 * 1024 * 1024
_SNIFF_BYTES = 64
_UNSAFE_FILENAME = re.compile(r'[\x00-\x1f\x7f"\\/]')
_KEY_DROPPED = re.compile(r'[\x00-\x1f\x7f"<>_]')
# JavaScript's String.trim set, less the control characters that the key
# already drops. Python's bare strip differs: it keeps U+FEFF, drops U+0085.
_JS_TRIMMED = " \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_ZIP_MAGIC = b"PK\x03\x04"
# The part each Office Open XML format must hold, and the type it is served as.
_OFFICE_PARTS = {
    "word/document.xml": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xl/workbook.xml": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
# The viewer picks how to draw an original from its file name, so a document
# must carry the extension of its real type.
_DOCUMENT_EXTENSIONS = {
    "application/pdf": ".pdf",
    _OFFICE_PARTS["word/document.xml"]: ".docx",
    _OFFICE_PARTS["xl/workbook.xml"]: ".xlsx",
}
# An original with no drawn type. The read route serves it with nosniff and a
# sandbox policy, so a browser never runs or renders it as a page.
_OPAQUE_DOCUMENT = "application/octet-stream"
_ZIP_END_MAGIC = b"PK\x05\x06"
_ZIP64_LOCATOR_MAGIC = b"PK\x06\x07"
_ZIP64_LOCATOR_SIZE = 20
_ZIP_END_SIZE = 22
# A zip's comment can be at most this long, so its end record is in this tail.
_ZIP_END_SEARCH = _ZIP_END_SIZE + 0xFFFF
# Real DOCX and XLSX directories are a few KiB. The cap stops a zip of many
# small entries from costing memory and CPU in the request thread.
MAX_ZIP_DIRECTORY_BYTES = 512 * 1024


@dataclass(frozen=True)
class SniffedMedia:
    """The real type of an uploaded file, read from its bytes.

    Attributes:
        kind: ``image``, ``video``, ``audio`` or ``document``.
        mime_type: The content type the server sends back for the file.
    """

    kind: str
    mime_type: str


# Response body for ``POST /agent/media``: the stored file's id and real type.
class ChatMediaResponse(BaseModel):
    id: str
    name: str
    kind: str
    mime_type: str
    byte_size: int


def _mp4_family(brand: bytes, declared: str) -> SniffedMedia | None:
    """Classify an ISO base media file from its ``ftyp`` major brand.

    Args:
        brand: The four-byte major brand.
        declared: The lowercased upload content type, used only to choose
            between audio and video for a container that can hold both.

    Returns:
        The sniffed type, or ``None`` for an image container such as HEIC or AVIF.
    """
    if brand in {b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1", b"avif", b"avis"}:
        return None
    if brand == b"qt  ":
        return SniffedMedia("video", "video/quicktime")
    if brand in {b"M4A ", b"M4B "} or declared.startswith("audio/"):
        return SniffedMedia("audio", "audio/mp4")
    return SniffedMedia("video", "video/mp4")


def sniff_media(head: bytes, declared: str = "") -> SniffedMedia | None:
    """Find the real media type of a file from its first bytes.

    Args:
        head: The first bytes of the file (64 are enough).
        declared: The upload content type. It never makes a file allowed;
            it only chooses audio or video for WebM and MP4 containers.

    Returns:
        The sniffed type, or ``None`` when the file is not an allowed type.
    """
    declared = declared.lower()
    if head.startswith(b"%PDF-"):
        return SniffedMedia("document", "application/pdf")
    if head.startswith(_ZIP_MAGIC):
        # Any zip passes here; ``office_document_type`` finds its real type
        # once the whole file is read.
        return SniffedMedia("document", "application/zip")
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return SniffedMedia("image", "image/png")
    if head.startswith(b"\xff\xd8\xff"):
        return SniffedMedia("image", "image/jpeg")
    if head.startswith((b"GIF87a", b"GIF89a")):
        return SniffedMedia("image", "image/gif")
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return SniffedMedia("image", "image/webp")
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return SniffedMedia("audio", "audio/wav")
    if head[4:8] == b"ftyp":
        return _mp4_family(head[8:12], declared)
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        if declared.startswith("audio/"):
            return SniffedMedia("audio", "audio/webm")
        return SniffedMedia("video", "video/webm")
    if head.startswith(b"OggS"):
        return SniffedMedia("audio", "audio/ogg")
    if head.startswith(b"fLaC"):
        return SniffedMedia("audio", "audio/flac")
    if head.startswith(b"ID3"):
        return SniffedMedia("audio", "audio/mpeg")
    if len(head) >= 2 and head[0] == 0xFF:
        b1 = head[1]
        # ADTS AAC: a 12-bit sync and layer bits 00.
        if b1 & 0xF6 == 0xF0:
            return SniffedMedia("audio", "audio/aac")
        # An MPEG frame sync with a known version and Layer III or II only, so
        # a UTF-16 LE byte order mark (FF FE, Layer I) is not taken for audio.
        if b1 & 0xE0 == 0xE0 and b1 & 0x18 != 0x08 and b1 & 0x06 in (0x02, 0x04):
            return SniffedMedia("audio", "audio/mpeg")
    return None


def office_document_type(data: bytes) -> str | None:
    """Find the Office Open XML type of a zip file from its part names.

    Args:
        data: The whole file.

    Returns:
        The DOCX or XLSX content type, or ``None`` for any other zip.
    """
    # zipfile reads the central directory that this end record names. A ZIP64
    # locator just before the record makes zipfile use the ZIP64 size instead,
    # which the cap below does not see. DOCX and XLSX under the upload limit
    # never need ZIP64, so a zip that has one is refused.
    end = data.rfind(_ZIP_END_MAGIC, max(0, len(data) - _ZIP_END_SEARCH))
    if end < 0 or len(data) - end < _ZIP_END_SIZE:
        return None
    if data[max(0, end - _ZIP64_LOCATOR_SIZE) : end].startswith(_ZIP64_LOCATOR_MAGIC):
        return None
    if int.from_bytes(data[end + 12 : end + 16], "little") > MAX_ZIP_DIRECTORY_BYTES:
        return None
    try:
        # Only the central directory is read; no part is decompressed.
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
    except (zipfile.BadZipFile, ValueError):
        return None
    return next((mime for part, mime in _OFFICE_PARTS.items() if part in names), None)


def _draw_key(raw: str | None) -> str:
    """Return the key that decides whether the viewer draws a file.

    Args:
        raw: The filename from the upload, if any.

    Returns:
        The name with no path, no control characters and no ``"<>_``, trimmed.
    """
    # The same key as the viewer's ``nameKey``. The composer's block changes
    # ``"<>`` to ``_``, so the raw name, the block's name and the stored name
    # all give one answer. The upload refuses an original whose stored name
    # has another key, so a reload pairs each original with its file.
    name = unicodedata.normalize("NFC", re.split(r"[\\/]", raw or "")[-1])
    return _KEY_DROPPED.sub("", name).strip(_JS_TRIMMED)


def _named_document_type(raw: str | None) -> str | None:
    """Return the document type that a file name's extension claims.

    Args:
        raw: The filename from the upload, if any.

    Returns:
        The PDF, DOCX or XLSX content type, or ``None`` for any other name.
    """
    # The same rule as the viewer's ``originalFormat``: a name that is only an
    # extension (".pdf") claims no type, so the viewer does not draw it.
    stem, dot, ext = _draw_key(raw).rpartition(".")
    if not stem or not dot:
        return None
    return next((mime for mime, known in _DOCUMENT_EXTENSIONS.items() if known == f".{ext.lower()}"), None)


def _original_type(head: bytes, data: bytes, raw_name: str | None) -> str:
    """Find the stored type of an attached file's original.

    Args:
        head: The first bytes of the file.
        data: The whole file.
        raw_name: The filename from the upload, if any.

    Returns:
        The real PDF, DOCX or XLSX type when the name claims one, else the opaque type.

    Raises:
        HTTPException: 415 when the name claims a PDF, DOCX or XLSX that the
            bytes are not; the viewer would fail to draw it.
    """
    named = _named_document_type(raw_name)
    if named is None:
        return _OPAQUE_DOCUMENT
    real = "application/pdf" if head.startswith(b"%PDF-") else None
    if real is None and head.startswith(_ZIP_MAGIC):
        real = office_document_type(data)
    if real != named:
        raise HTTPException(status_code=415, detail="This file type is not supported.")
    return named


def _safe_filename(raw: str | None) -> str:
    """Return a display filename with no path, quotes or control characters.

    Args:
        raw: The filename from the upload, if any.

    Returns:
        A filename of at most 255 characters; ``file`` when nothing is left.
    """
    name = unicodedata.normalize("NFC", (raw or "").replace("\\", "/").rsplit("/", 1)[-1])
    name = _UNSAFE_FILENAME.sub("", name).strip(_JS_TRIMMED)
    return name[:255] or "file"


def _engine(job_store: Any) -> Any:
    """Return the job store's SQL engine, or fail when there is none.

    Args:
        job_store: The application job store.

    Returns:
        The SQLAlchemy engine.

    Raises:
        HTTPException: 503 when the store has no SQL engine.
    """
    engine = getattr(job_store, "engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="Chat media storage is not available.")
    return engine


def load_owned_media(engine: Any, username: str, media_ids: list[str], *, sweep: bool = True) -> list[ChatMediaModel]:
    """Load the caller's media rows in the order of ``media_ids``, without their bytes.

    Stale unsent files are swept first, so a turn never names a file that a
    later storage measure deletes. A send holds these rows for the whole
    streamed turn, so the bytes stay in the database; ``load_media_data``
    reads only the ones the model gets.

    Args:
        engine: The SQLAlchemy engine.
        username: The caller. Only rows this user owns load.
        media_ids: The ids the client sent; duplicates load once.
        sweep: Whether to sweep stale unsent files first. A read sets it
            False, so a GET never deletes rows or commits.

    Returns:
        The owned rows, detached from their session, in request order. Their
        ``data`` column is not loaded.

    Raises:
        HTTPException: 404 when any id is unknown or another user owns it.
    """
    wanted = list(dict.fromkeys(media_ids))
    if not wanted:
        return []
    if sweep:
        sweep_unsent_chat_media(engine, username)
    with Session(engine, expire_on_commit=False) as session:
        rows = session.scalars(
            select(ChatMediaModel)
            .options(defer(ChatMediaModel.data))
            .where(ChatMediaModel.id.in_(wanted), ChatMediaModel.username == username)
        ).all()
        session.expunge_all()
    by_id = {row.id: row for row in rows}
    if len(by_id) != len(wanted):
        raise HTTPException(status_code=404, detail="Attachment not found.")
    return [by_id[media_id] for media_id in wanted]


def load_media_data(engine: Any, media_ids: list[str]) -> dict[str, bytes]:
    """Read the stored bytes of some media rows.

    Args:
        engine: The SQLAlchemy engine.
        media_ids: The ids of rows the caller already loaded as its own.

    Returns:
        A map from each id to its bytes.
    """
    if not media_ids:
        return {}
    with Session(engine) as session:
        return dict(
            session.execute(
                select(ChatMediaModel.id, ChatMediaModel.data).where(ChatMediaModel.id.in_(media_ids))
            ).all()
        )


def media_copy_bytes(rows: list[ChatMediaModel], conversation_id: str | None) -> int:
    """Return the bytes that attaching ``rows`` to a chat copies.

    Args:
        rows: The files to attach.
        conversation_id: The chat that gets the files, or None for a chat not
            created yet; then every file that a chat holds is a copy.

    Returns:
        The total size of the files that another chat holds.
    """
    return sum(row.byte_size for row in rows if row.conversation_id not in (None, conversation_id))


_COPY_COLUMNS = [
    "id",
    "username",
    "filename",
    "kind",
    "mime_type",
    "byte_size",
    "data",
    "created_at",
    "conversation_id",
]


def attach_media_to_chat(
    session: Session,
    job_store: Any,
    username: str,
    media_ids: list[str],
    conversation_id: str,
    copy_bytes: int | None = None,
) -> dict[str, str]:
    """Give a chat its own rows for the caller's files, and return their ids.

    A file that no chat holds moves to this chat. A file that another chat
    holds gets a copy with a new id, so deleting either chat never removes a
    file that the other chat still shows. A copy is new stored data, so it
    passes the same storage quota check as an upload. An id with no owned row
    is left out of the map, so a fork keeps a turn that names a deleted file.
    The rows stay locked until the caller commits, so a delete or a sweep
    waits, and then finds a file that a chat holds and leaves it.

    Args:
        session: The open session; the caller commits it.
        job_store: The job store that measures the caller's storage.
        username: The caller. Only rows this user owns attach.
        media_ids: The ids of the files to attach.
        conversation_id: The chat that gets the files.
        copy_bytes: The copy bytes that the caller already checked against the
            quota, before it wrote in ``session``. None checks the quota here.
            The quota measure sweeps in its own transaction, which must not
            wait on rows that this session wrote or locked.

    Returns:
        A map from each attached id to the id that the chat now holds. Ids that
        name no file of the caller are not in it.

    Raises:
        DomainError: ``user.storage.quota_exceeded`` (409) when the copies
            would go over the caller's storage quota; nothing changes then.
        DomainError: ``agent.media.changed`` (409) when the copies come to
            more than ``copy_bytes``.
        HTTPException: 404 when a file is deleted while the quota is checked.
    """
    if not media_ids:
        return {}
    owned = (ChatMediaModel.id.in_(media_ids), ChatMediaModel.username == username)
    sizes = session.execute(
        select(ChatMediaModel.id, ChatMediaModel.conversation_id, ChatMediaModel.byte_size).where(*owned)
    ).all()
    copied = sum(size for _, held_by, size in sizes if held_by not in (None, conversation_id))
    # The quota measure sweeps stale unsent files in its own transaction. It
    # runs before this session locks the rows, so it never waits on them.
    if copy_bytes is None and copied:
        enforce_storage_quota(job_store, username, copied)
    # Locks in id order, so two sends of the same files never deadlock.
    rows = list(
        session.scalars(
            select(ChatMediaModel)
            .options(defer(ChatMediaModel.data, raiseload=True))
            .where(*owned)
            .order_by(ChatMediaModel.id)
            .with_for_update()
        ).all()
    )
    if len(rows) != len(sizes):
        raise HTTPException(status_code=404, detail="Attachment not found.")
    if copy_bytes is not None and media_copy_bytes(rows, conversation_id) > copy_bytes:
        # Another chat took an unsent file after the caller checked the quota.
        raise DomainError("agent.media.changed", status=409)
    held: dict[str, str] = {}
    for row in rows:
        if row.conversation_id is None:
            row.conversation_id = conversation_id
        if row.conversation_id == conversation_id:
            held[row.id] = row.id
            continue
        copy_id = str(uuid4())
        # The database copies the bytes, so a large file never loads into
        # worker memory.
        session.execute(
            insert(ChatMediaModel).from_select(
                _COPY_COLUMNS,
                select(
                    literal(copy_id),
                    ChatMediaModel.username,
                    ChatMediaModel.filename,
                    ChatMediaModel.kind,
                    ChatMediaModel.mime_type,
                    ChatMediaModel.byte_size,
                    ChatMediaModel.data,
                    literal(datetime.now(UTC)),
                    literal(conversation_id),
                ).where(ChatMediaModel.id == row.id),
            )
        )
        held[row.id] = copy_id
    return held


def create_chat_media_router(*, job_store: Any = None) -> APIRouter:
    """Build the chat media router.

    Args:
        job_store: The application job store; its engine holds the media rows.

    Returns:
        A configured :class:`APIRouter` with the upload, read and delete routes.
    """
    router = APIRouter()

    @router.post(
        "/agent/media",
        response_model=ChatMediaResponse,
        status_code=201,
        summary="Upload an image, video, audio or document file for an agent chat",
    )
    def upload_media(
        user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
        file: Annotated[UploadFile, File()],
        original: Annotated[bool, Form()] = False,
        name: Annotated[str | None, Form()] = None,
    ) -> ChatMediaResponse:
        """Store one media file for the caller.

        Args:
            user: The authenticated caller, who owns the stored file.
            file: The uploaded file.
            original: Whether the file is an attached file's original, which
                is stored as a ``document`` whatever its type.
            name: The file's real name, if the client sends it; else the
                multipart filename.

        Returns:
            The stored file's id, name, kind, real type and size.

        Raises:
            HTTPException: 415 for a type that is not allowed or a document
                whose name has another type's extension, 413 for a file over
                its kind's limit, 422 for an original whose stored name has
                another name key than the name it came with.
            DomainError: 429 over the hourly cap (file text reads share it,
                and document originals have their own), 409 over the storage
                quota.
        """
        engine = _engine(job_store)
        head = file.file.read(_SNIFF_BYTES)
        sniffed = SniffedMedia("document", _OPAQUE_DOCUMENT) if original else sniff_media(head, file.content_type or "")
        # Media shares the file text route's hourly cap. A document original
        # (SKY-735) gets its own: its file's text read already counted there.
        # So a direct caller can store more files an hour, but the storage quota
        # still caps the total bytes.
        bucket = "chat-originals" if sniffed and sniffed.kind == "document" else "attachments"
        RateLimiter(shared_redis_client()).enforce(
            f"{bucket}:{user.username}",
            limit=settings.rate_limit_attachments_per_hour,
            window_seconds=3600,
        )
        if sniffed is None:
            raise HTTPException(status_code=415, detail="This file type is not supported.")
        limit = MAX_MEDIA_BYTES[sniffed.kind]
        data = head + file.file.read(limit + 1 - len(head))
        if len(data) > limit:
            raise HTTPException(status_code=413, detail="This file is too large.")
        # A browser sends '"', CR and LF in a multipart filename as %22, %0D
        # and %0A, which a real name can also hold. A plain field keeps the
        # name exact, so the server's key matches the viewer's.
        raw_name = file.filename if name is None else name
        filename = _safe_filename(raw_name)
        if original:
            # History names an original by its stored name and the draft's block
            # by the full name. A cut name has another key, so the original
            # would pair with no file after a reload.
            if _draw_key(filename) != _draw_key(raw_name):
                raise HTTPException(status_code=422, detail="This file name is not supported.")
            sniffed = SniffedMedia("document", _original_type(head, data, raw_name))
        elif sniffed.mime_type == "application/zip":
            office = office_document_type(data)
            if office is None:
                raise HTTPException(status_code=415, detail="This file type is not supported.")
            sniffed = SniffedMedia("document", office)
        if sniffed.kind == "document" and _named_document_type(raw_name) != (
            None if sniffed.mime_type == _OPAQUE_DOCUMENT else sniffed.mime_type
        ):
            raise HTTPException(status_code=415, detail="This file type is not supported.")
        # The quota check measures storage, and that measure first sweeps the
        # user's stale unsent files, so they never block this upload.
        enforce_storage_quota(job_store, user.username, len(data))

        now = datetime.now(UTC)
        row = ChatMediaModel(
            id=str(uuid4()),
            username=user.username,
            filename=filename,
            kind=sniffed.kind,
            mime_type=sniffed.mime_type,
            byte_size=len(data),
            data=data,
            created_at=now,
        )
        with Session(engine, expire_on_commit=False) as session:
            session.add(row)
            session.commit()
        return ChatMediaResponse(
            id=row.id, name=row.filename, kind=row.kind, mime_type=row.mime_type, byte_size=row.byte_size
        )

    @router.get("/agent/media/{media_id}", summary="Read an agent chat media file")
    def read_media(
        media_id: str,
        user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    ) -> Response:
        """Return the bytes of one of the caller's media files.

        Args:
            media_id: The media id.
            user: The authenticated caller.

        Returns:
            The file bytes with the sniffed content type.

        Raises:
            HTTPException: 404 when the id is unknown or another user owns it.
        """
        engine = _engine(job_store)
        (row,) = load_owned_media(engine, user.username, [media_id], sweep=False)
        data = load_media_data(engine, [row.id]).get(row.id)
        if data is None:
            raise HTTPException(status_code=404, detail="Attachment not found.")
        # The viewer never draws an opaque original, so the browser saves it.
        disposition = "attachment" if row.mime_type == _OPAQUE_DOCUMENT else "inline"
        return Response(
            content=data,
            media_type=row.mime_type,
            headers={
                "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(row.filename)}",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; sandbox",
                "Cache-Control": "private, max-age=3600",
            },
        )

    @router.delete("/agent/media/{media_id}", status_code=204, summary="Delete an agent chat media file")
    def delete_media(
        media_id: str,
        user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    ) -> Response:
        """Delete one of the caller's unsent media files.

        A sent file belongs to its chat and goes when the chat goes. A delete
        that waits on a send's lock then finds the file sent and leaves it, so
        a stored turn never names a missing file.

        Args:
            media_id: The media id.
            user: The authenticated caller.

        Returns:
            An empty 204 response.

        Raises:
            HTTPException: 404 when the id is unknown, another user owns it,
                or a chat holds it.
        """
        with Session(_engine(job_store)) as session:
            deleted = session.execute(
                delete(ChatMediaModel).where(
                    ChatMediaModel.id == media_id,
                    ChatMediaModel.username == user.username,
                    ChatMediaModel.conversation_id.is_(None),
                )
            ).rowcount
            session.commit()
        if not deleted:
            raise HTTPException(status_code=404, detail="Attachment not found.")
        return Response(status_code=204)

    return router

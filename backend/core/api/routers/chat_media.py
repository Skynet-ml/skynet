"""Store images, video and audio attached in agent chats (SKY-707). [INTERNAL]

``POST /agent/media`` takes one file, finds its real type from its first
bytes, and stores it for the caller. ``GET /agent/media/{media_id}`` returns
the bytes, and ``DELETE /agent/media/{media_id}`` removes them; both answer
only to the owner. Another user's id gets the same 404 as an unknown id, so
an id never confirms that a file exists.

The upload header's content type is never trusted. SVG, HEIC and every type
that is not on the allow-list get a 415, because a browser can run script in
SVG and a model provider cannot read HEIC.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ...config import settings
from ...storage.models import ChatMediaModel
from ..auth import AuthenticatedUser, get_authenticated_user
from ..rate_limit import RateLimiter, shared_redis_client
from ._helpers import enforce_storage_quota

# Anthropic refuses an image whose base64 form passes 5 MiB, and base64 adds
# a third, so 3 MiB keeps every image under that limit with margin.
MAX_MEDIA_BYTES: dict[str, int] = {
    "image": 3 * 1024 * 1024,
    "audio": 20 * 1024 * 1024,
    "video": 50 * 1024 * 1024,
}
MAX_MEDIA_PER_MESSAGE = 10
# The image bytes one turn sends to the model. Gemini refuses a request over
# 20 MB of inline data and Anthropic one over 32 MB; images past this budget
# go to the model as reference-only files.
MAX_MODEL_IMAGE_BYTES = 12 * 1024 * 1024
_SNIFF_BYTES = 64
_UNSAFE_FILENAME = re.compile(r'[\x00-\x1f\x7f"\\/]')


@dataclass(frozen=True)
class SniffedMedia:
    """The real type of an uploaded file, read from its bytes.

    Attributes:
        kind: ``image``, ``video`` or ``audio``.
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
    if len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0:
        # An MPEG frame sync. Layer bits 00 mark an ADTS AAC stream.
        if head[1] & 0x06 == 0:
            return SniffedMedia("audio", "audio/aac")
        return SniffedMedia("audio", "audio/mpeg")
    return None


def _safe_filename(raw: str | None) -> str:
    """Return a display filename with no path, quotes or control characters.

    Args:
        raw: The filename from the upload, if any.

    Returns:
        A filename of at most 255 characters; ``file`` when nothing is left.
    """
    name = unicodedata.normalize("NFC", (raw or "").replace("\\", "/").rsplit("/", 1)[-1])
    name = _UNSAFE_FILENAME.sub("", name).strip()
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


def load_owned_media(engine: Any, username: str, media_ids: list[str]) -> list[ChatMediaModel]:
    """Load the caller's media rows in the order of ``media_ids``.

    Args:
        engine: The SQLAlchemy engine.
        username: The caller. Only rows this user owns load.
        media_ids: The ids the client sent; duplicates load once.

    Returns:
        The owned rows, detached from their session, in request order.

    Raises:
        HTTPException: 404 when any id is unknown or another user owns it.
    """
    wanted = list(dict.fromkeys(media_ids))
    if not wanted:
        return []
    with Session(engine, expire_on_commit=False) as session:
        rows = session.scalars(
            select(ChatMediaModel).where(ChatMediaModel.id.in_(wanted), ChatMediaModel.username == username)
        ).all()
        session.expunge_all()
    by_id = {row.id: row for row in rows}
    if len(by_id) != len(wanted):
        raise HTTPException(status_code=404, detail="Attachment not found.")
    return [by_id[media_id] for media_id in wanted]


def media_copy_bytes(rows: list[ChatMediaModel], conversation_id: str) -> int:
    """Return the bytes that attaching ``rows`` to a chat copies.

    Args:
        rows: The files to attach.
        conversation_id: The chat that gets the files.

    Returns:
        The total size of the files that another chat holds.
    """
    return sum(row.byte_size for row in rows if row.conversation_id not in (None, conversation_id))


def attach_media_to_chat(
    session: Session, job_store: Any, username: str, media_ids: list[str], conversation_id: str
) -> dict[str, str]:
    """Give a chat its own rows for the caller's files, and return their ids.

    A file that no chat holds moves to this chat. A file that another chat
    holds gets a copy with a new id, so deleting either chat never removes a
    file that the other chat still shows. A copy is new stored data, so it
    passes the same storage quota check as an upload.

    Args:
        session: The open session; the caller commits it.
        job_store: The job store that measures the caller's storage.
        username: The caller. Only rows this user owns attach.
        media_ids: The ids of the files to attach.
        conversation_id: The chat that gets the files.

    Returns:
        A map from each attached id to the id that the chat now holds.

    Raises:
        DomainError: ``user.storage.quota_exceeded`` (409) when the copies
            would go over the caller's storage quota; nothing changes then.
    """
    if not media_ids:
        return {}
    rows = list(
        session.scalars(
            select(ChatMediaModel).where(ChatMediaModel.id.in_(media_ids), ChatMediaModel.username == username)
        ).all()
    )
    copied = media_copy_bytes(rows, conversation_id)
    if copied:
        enforce_storage_quota(job_store, username, copied)
    held: dict[str, str] = {}
    for row in rows:
        if row.conversation_id is None:
            row.conversation_id = conversation_id
        if row.conversation_id == conversation_id:
            held[row.id] = row.id
            continue
        copy = ChatMediaModel(
            id=str(uuid4()),
            username=row.username,
            filename=row.filename,
            kind=row.kind,
            mime_type=row.mime_type,
            byte_size=row.byte_size,
            data=row.data,
            conversation_id=conversation_id,
        )
        session.add(copy)
        held[row.id] = copy.id
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
        summary="Upload an image, video or audio file for an agent chat",
    )
    def upload_media(
        user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
        file: Annotated[UploadFile, File()],
    ) -> ChatMediaResponse:
        """Store one media file for the caller.

        Args:
            user: The authenticated caller, who owns the stored file.
            file: The uploaded file.

        Returns:
            The stored file's id, name, kind, real type and size.

        Raises:
            HTTPException: 415 for a type that is not allowed, 413 for a file
                over its kind's limit.
            DomainError: 429 over the hourly upload cap, 409 over the storage quota.
        """
        RateLimiter(shared_redis_client()).enforce(
            f"chat-media:{user.username}",
            limit=settings.rate_limit_attachments_per_hour,
            window_seconds=3600,
        )
        engine = _engine(job_store)
        head = file.file.read(_SNIFF_BYTES)
        sniffed = sniff_media(head, file.content_type or "")
        if sniffed is None:
            raise HTTPException(status_code=415, detail="This file type is not supported.")
        limit = MAX_MEDIA_BYTES[sniffed.kind]
        data = head + file.file.read(limit + 1 - len(head))
        if len(data) > limit:
            raise HTTPException(status_code=413, detail="This file is too large.")
        # The quota check measures storage, and that measure first sweeps the
        # user's stale unsent files, so they never block this upload.
        enforce_storage_quota(job_store, user.username, len(data))

        now = datetime.now(UTC)
        row = ChatMediaModel(
            id=str(uuid4()),
            username=user.username,
            filename=_safe_filename(file.filename),
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
        (row,) = load_owned_media(_engine(job_store), user.username, [media_id])
        return Response(
            content=row.data,
            media_type=row.mime_type,
            headers={
                "Content-Disposition": f"inline; filename*=UTF-8''{quote(row.filename)}",
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
        """Delete one of the caller's media files.

        Args:
            media_id: The media id.
            user: The authenticated caller.

        Returns:
            An empty 204 response.

        Raises:
            HTTPException: 404 when the id is unknown or another user owns it.
        """
        with Session(_engine(job_store)) as session:
            deleted = session.execute(
                delete(ChatMediaModel).where(ChatMediaModel.id == media_id, ChatMediaModel.username == user.username)
            ).rowcount
            session.commit()
        if not deleted:
            raise HTTPException(status_code=404, detail="Attachment not found.")
        return Response(status_code=204)

    return router

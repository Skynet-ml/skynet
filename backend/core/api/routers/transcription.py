"""Speech-to-text for composer dictation. [INTERNAL]

``POST /transcribe`` — accept one recorded clip (multipart ``audio`` plus an
optional ``language`` hint from the UI locale) and return its transcript. A
form field ``partial=true`` marks a live preview: every few seconds while the
user still records, the composer sends the file header and only the audio
since the last preview, joins the texts live, and replaces them with the
final call (the whole take) after stop.

Whisper large-v3-turbo through OpenRouter is the platform's sole dictation
provider: a clip returns in about a second, and running one leg keeps the
failure surface honest — no key configured answers a typed 503 whose
message the composer shows as the reason, a provider error answers 502.
The ``language`` field is accepted for wire compatibility but never
forwarded: Whisper treats the param as a directive, and the UI locale isn't
necessarily the spoken language.

Dictation is free for every signed-in account and paid from the platform's
OpenRouter transcription key, not from user balances. A caller with a verified
OpenRouter BYOK key is transcribed on that key instead. Three limits bound
every caller: a per-account hourly clip cap, a larger per-account hourly
partial cap (so live previews never use up the clip cap), and a platform-wide
monthly dollar budget counted from the cost OpenRouter reports for each
platform-paid call, partial or final.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel

from ...billing.byok_vault import ProviderKeyVault
from ...billing.protected_credentials import _openrouter_endpoint
from ...config import settings
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from ..platform_budget import budget_open, record_spend
from ..rate_limit import RateLimiter, shared_redis_client

logger = logging.getLogger("app.api.transcription")

_OPENROUTER_BASE = "https://openrouter.ai/api/v1"
_MODEL = "openai/whisper-large-v3-turbo"
# OpenRouter's multipart upload cap; dictation takes are far below it.
_MAX_AUDIO_MB = 25
_MAX_AUDIO_BYTES = _MAX_AUDIO_MB * 1024 * 1024
# A preview holds only a few seconds of new audio. The small cap keeps a
# scripted caller from filing whole takes under the larger partial cap.
_MAX_PARTIAL_MB = 1
_MAX_PARTIAL_BYTES = _MAX_PARTIAL_MB * 1024 * 1024
_HTTP_TIMEOUT_S = 120.0
# Used only when OpenRouter omits ``usage.cost``: an upper bound on
# whisper-large-v3-turbo's per-hour rate with a floor per clip, so a missing
# cost still counts against the budget instead of counting as free. A live
# preview sends about 3 s of new audio (dictation-level.ts), so its floor is
# smaller; the 10 s clip floor would count each preview about 3 times.
_FALLBACK_DOLLARS_PER_AUDIO_HOUR = 0.04
_FALLBACK_MIN_BILLED_SECONDS = 10.0
_FALLBACK_MIN_PARTIAL_SECONDS = 3.0
_BUDGET_SERVICE = "transcription"
_BYOK_PROVIDER = "openrouter"


def _clip_cost(usage: dict[str, Any], partial: bool = False) -> float:
    """Return the dollars one clip cost, from OpenRouter's ``usage`` block.

    Args:
        usage: The response's ``usage`` object (may be empty).
        partial: True for a live preview, which uses the smaller floor.

    Returns:
        ``usage.cost`` when OpenRouter reports it, otherwise an estimate from
        ``usage.seconds`` at the fallback rate.
    """
    cost = usage.get("cost")
    if cost is not None:
        return float(cost)
    seconds = float(usage.get("seconds") or 0.0)
    floor = _FALLBACK_MIN_PARTIAL_SECONDS if partial else _FALLBACK_MIN_BILLED_SECONDS
    return max(seconds, floor) / 3600 * _FALLBACK_DOLLARS_PER_AUDIO_HOUR


class TranscriptionResponse(BaseModel):
    """Response body for ``POST /transcribe``: transcript plus provider used."""

    text: str
    provider: str


async def _openrouter_transcribe(
    client: httpx.AsyncClient, audio: bytes, filename: str, api_key: str, partial: bool = False
) -> tuple[str, float]:
    """Transcribe one clip via Whisper large-v3-turbo on OpenRouter.

    Args:
        client: Shared HTTP client.
        audio: Raw audio bytes.
        filename: Client filename; OpenRouter derives the audio format from
            its extension (``take.webm`` or ``take.m4a``).
        api_key: OpenRouter bearer token.
        partial: True for a live preview; it sets the fallback cost floor.

    Returns:
        The transcript text and the clip cost in dollars.

    Raises:
        RuntimeError: On a non-OK provider response.
    """
    res = await client.post(
        f"{_OPENROUTER_BASE}/audio/transcriptions",
        headers={"Authorization": f"Bearer {api_key}"},
        files={"file": (filename, audio)},
        data={"model": _MODEL},
    )
    if res.status_code >= 400:
        raise RuntimeError(f"openrouter transcribe: {res.status_code}")
    body = res.json()
    return str(body.get("text") or ""), _clip_cost(body.get("usage") or {}, partial)


def _own_openrouter_key(engine: Any, username: str) -> str | None:
    """Return the caller's verified OpenRouter BYOK secret, or None.

    Args:
        engine: SQLAlchemy engine backing the vault, or ``None``.
        username: The caller.

    Returns:
        The decrypted key when the caller has a verified OpenRouter connection that
        the vault can decrypt and that targets openrouter.ai itself; otherwise
        None, which routes to the platform key.
    """
    if engine is None:
        return None
    vault = ProviderKeyVault(engine=engine)
    try:
        if not vault.has_verified_connection(username, _BYOK_PROVIDER):
            return None
        connection = vault.resolve_connection(username, _BYOK_PROVIDER, verified_only=True)
    except DomainError as err:
        logger.warning("openrouter byok key unusable for %s: %s", username, err.code)
        return None
    if connection is None:
        return None
    # A connection with a custom base holds a credential for another host;
    # sending it to openrouter.ai would leak it and fail every clip.
    if not _openrouter_endpoint(connection.api_base):
        return None
    return connection.secret


def create_transcription_router(job_store: Any) -> APIRouter:
    """Build the dictation transcription router.

    Args:
        job_store: Store whose engine backs the BYOK table used to find the
            caller's own OpenRouter key.

    Returns:
        A configured :class:`APIRouter` exposing ``POST /transcribe``.
    """
    router = APIRouter()

    @router.post(
        "/transcribe",
        response_model=TranscriptionResponse,
        summary="Transcribe one recorded audio clip to text",
    )
    async def transcribe(
        user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
        audio: Annotated[UploadFile, File()],
        language: Annotated[str | None, Form()] = None,
        partial: Annotated[bool, Form()] = False,
    ) -> TranscriptionResponse:
        """Run the clip through Whisper on OpenRouter and return the transcript.

        Args:
            user: Authenticated caller (dictation is login-gated like the
                composers that host it); keys the hourly clip and partial
                caps and the lookup of the caller's own OpenRouter key.
            audio: The recorded clip (webm/opus everywhere, AAC-in-MP4 on
                Safari).
            language: Optional BCP-47 tag from the UI locale; accepted but
                unused — Whisper auto-detects the spoken language.
            partial: True for a live preview of a clip still being recorded;
                counts against the partial cap instead of the clip cap.

        Returns:
            The transcript and which provider produced it.

        Raises:
            DomainError: 413 when the clip exceeds its size cap (smaller
                for a partial), 429 when the caller is over the hourly clip
                or partial cap, 503 when no OpenRouter key is configured or
                the monthly budget is spent, 502 when the provider call
                failed.
        """
        del language
        data = await audio.read()
        max_bytes, max_mb = (_MAX_PARTIAL_BYTES, _MAX_PARTIAL_MB) if partial else (_MAX_AUDIO_BYTES, _MAX_AUDIO_MB)
        if len(data) > max_bytes:
            raise DomainError("transcription.too_large", status=413, max_mb=max_mb)
        own_key = _own_openrouter_key(getattr(job_store, "engine", None), user.username)
        if own_key is None and not settings.transcription_api_key:
            raise DomainError("transcription.unconfigured", status=503)
        # The client sets ``partial``, so a scripted caller can file short clips
        # under the larger partial cap. That is accepted: the partial size cap
        # above bounds each one, and each platform-paid call still counts against
        # the monthly budget below (test_partial_records_platform_spend).
        if partial:
            limit_key = f"transcribe-partial:{user.username}"
            limit = settings.rate_limit_transcription_partials_per_hour
        else:
            limit_key = f"transcribe:{user.username}"
            limit = settings.rate_limit_transcriptions_per_hour
        RateLimiter(shared_redis_client()).enforce(limit_key, limit=limit, window_seconds=3600)
        budget = settings.transcription_monthly_budget_usd
        if own_key is None and not budget_open(_BUDGET_SERVICE, budget):
            raise DomainError("transcription.budget_exhausted", status=503)
        filename = audio.filename or "take.webm"
        api_key = own_key or settings.transcription_api_key.get_secret_value()

        async with httpx.AsyncClient(timeout=httpx.Timeout(_HTTP_TIMEOUT_S)) as client:
            try:
                text, cost = await _openrouter_transcribe(client, data, filename, api_key, partial)
            except (RuntimeError, httpx.HTTPError, KeyError, ValueError, TypeError, AttributeError) as err:
                logger.warning("transcription failed (openrouter): %s", err)
                raise DomainError("transcription.failed", status=502) from err
        if own_key is None:
            record_spend(_BUDGET_SERVICE, cost, budget)
        return TranscriptionResponse(text=text, provider="openrouter")

    return router

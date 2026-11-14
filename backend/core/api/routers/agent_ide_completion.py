"""Inline completions for the agentic IDE editor, paid through protected model dispatch.

``POST /agent-ide/completions`` takes the text around the caret in one file and
returns the ghost text to show there, plus an optional next edit elsewhere in
the same window (SKY-703).

A call with a verified OpenRouter BYOK key runs on that key with no Skynet
model fee. Any other call runs on the platform key and the caller's wallet: the
dispatcher reserves the priced maximum before the provider sees the request,
and settles the reported cost after it. There is no free path.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Annotated, Any
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ...billing.budgets import MAX_CENTS, BudgetError, BudgetService
from ...billing.byok_vault import ProviderKeyVault
from ...billing.data_policy import data_policy_for
from ...billing.model_dispatch import OpenRouterDispatcher
from ...billing.operation_pricing import ChargePolicy, UnpricedOperationError
from ...billing.protected_credentials import _openrouter_endpoint
from ...billing.runtime import BudgetRuntime, UsagePendingError
from ...config import settings
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from ..rate_limit import RateLimiter, shared_redis_client
from ..suggest_model import byok_suggest_model, hosted_suggest_model
from .execution_budgets import budget_http_error

logger = logging.getLogger(__name__)

UserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]

# The editor sends a window around the caret, not the file. These caps bound
# the priced input of one call; the client trims to less than them.
MAX_PREFIX_CHARS = 6000
MAX_SUFFIX_CHARS = 2000
MAX_PATH_CHARS = 400
# One completion is a few lines. The cap also bounds the reserved output.
MAX_OUTPUT_TOKENS = 256
# The client waits about 400 ms after the last key, so a fast typist sends
# about two calls a second at most. This cap stops a loop, not a person.
RATE_LIMIT_PER_MINUTE = 60
_TIMEOUT_SECONDS = 20.0
_BYOK_PROVIDER = "openrouter"
_CARET = "<CARET>"

_SYSTEM = (
    "You are a code completion engine inside an editor. The user gives one file window with "
    f"the caret marked {_CARET}. Predict the text the user is about to type at the caret: "
    "finish the current line, and add the next lines only when they clearly follow. "
    "Never repeat text that is already after the caret. "
    "If the completion makes another place in the window need a matching change (for example a "
    "new function that a caller must now use), describe that one edit as next_edit: `after` is the "
    "exact text of one existing line in the window, and `lines` are the full new lines to insert "
    "after it, with their indentation. Otherwise next_edit is null. "
    'Answer with JSON only: {"completion": "...", "next_edit": {"after": "...", "lines": ["..."]} | null}. '
    'Answer {"completion": "", "next_edit": null} when you are not confident.'
)


class CompletionRequest(BaseModel):
    """The text around the caret in one file of the agentic IDE editor."""

    path: str = Field(min_length=1, max_length=MAX_PATH_CHARS, description="Repository path of the file.")
    prefix: str = Field(max_length=MAX_PREFIX_CHARS, description="Text before the caret, trimmed by the client.")
    suffix: str = Field(default="", max_length=MAX_SUFFIX_CHARS, description="Text after the caret.")


class NextEdit(BaseModel):
    """Lines to insert after an existing anchor line, once the completion is accepted."""

    after: str = Field(description="Exact text of the existing line the new lines follow.")
    lines: list[str] = Field(description="The new lines, with their indentation.")


class CompletionResponse(BaseModel):
    """Ghost text for the caret, and an optional next edit in the same file."""

    completion: str = Field(description="Text to insert at the caret; empty when there is no suggestion.")
    next_edit: NextEdit | None = Field(default=None, description="A follow-up edit elsewhere in the file.")


def _own_openrouter_key(engine: Any, username: str) -> str | None:
    """Return the caller's verified OpenRouter BYOK secret, or None.

    Args:
        engine: SQLAlchemy engine backing the vault, or ``None``.
        username: The caller.

    Returns:
        The decrypted key of a verified connection that targets openrouter.ai,
        or None, which routes the call to the platform key and the wallet.
    """
    if engine is None or settings.byok_vault_key is None:
        return None
    try:
        connection = ProviderKeyVault(engine=engine).resolve_connection(username, _BYOK_PROVIDER, verified_only=True)
    except DomainError as err:
        logger.warning("openrouter byok key unusable for %s: %s", username, err.code)
        return None
    # A custom base holds a credential for another host; never send it to openrouter.ai.
    if connection is None or not _openrouter_endpoint(connection.api_base):
        return None
    return connection.secret


def _messages(req: CompletionRequest) -> list[dict[str, str]]:
    """Build the chat messages for one completion.

    Args:
        req: The caret window.

    Returns:
        The system rules and the marked file window.
    """
    window = f"{req.prefix}{_CARET}{req.suffix}"
    return [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": f"File: {req.path}\n```\n{window}\n```"},
    ]


def _parse(content: str, suffix: str) -> CompletionResponse:
    """Read the model's JSON answer, and drop what cannot be shown.

    Args:
        content: The model's message text.
        suffix: Text after the caret; a completion that only repeats it is empty.

    Returns:
        The completion and next edit, or an empty completion for an unreadable answer.
    """
    # Some models wrap JSON in a code fence or add a sentence around it.
    match = re.search(r"\{.*\}", content, re.DOTALL)
    try:
        data = json.loads(match.group(0)) if match else None
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict):
        return CompletionResponse(completion="")
    completion = data.get("completion")
    completion = completion if isinstance(completion, str) else ""
    if completion and suffix.startswith(completion):
        completion = ""
    raw = data.get("next_edit")
    next_edit = None
    if isinstance(raw, dict) and isinstance(raw.get("after"), str) and raw["after"].strip():
        lines = raw.get("lines")
        if isinstance(lines, list) and lines and all(isinstance(line, str) for line in lines):
            next_edit = NextEdit(after=raw["after"], lines=lines)
    return CompletionResponse(completion=completion, next_edit=next_edit)


def _content(body: bytes) -> str:
    """Return the first choice's message text of a chat completion.

    Args:
        body: The provider's JSON response.

    Returns:
        The message text, or ``""`` when the response has none.
    """
    try:
        data = json.loads(body)
        content = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        return ""
    return content if isinstance(content, str) else ""


def create_agent_ide_completion_router(*, job_store: Any) -> APIRouter:
    """Build the agentic IDE completion router.

    Args:
        job_store: Hosted database owner for the wallet, the operation ledger and the BYOK vault.

    Returns:
        A router exposing ``POST /agent-ide/completions``.
    """
    router = APIRouter()

    @router.post("/agent-ide/completions", response_model=CompletionResponse, summary="Complete code at the caret")
    def complete(req: CompletionRequest, user: UserDep) -> CompletionResponse:
        """Reserve, send and settle one completion call for the signed-in account.

        Args:
            req: The caret window.
            user: The caller, who pays for a platform-key call.

        Returns:
            The ghost text and an optional next edit.

        Raises:
            HTTPException: 503 with no usable model or key, 402 or 409 when the
                wallet cannot fund the call, 422 when the call cannot be priced,
                424 while usage awaits settlement, 502 on a provider failure.
            DomainError: 429 when the caller is over the rate limit.
        """
        RateLimiter(shared_redis_client()).enforce(
            f"agent-ide-complete:{user.username}", limit=RATE_LIMIT_PER_MINUTE, window_seconds=60
        )
        engine = getattr(job_store, "engine", None)
        own_key = _own_openrouter_key(engine, user.username)
        if own_key is not None:
            picked, api_key, policy, data_policy = byok_suggest_model(), own_key, ChargePolicy("byok_model"), None
        else:
            if settings.local_mode or settings.openrouter_api_key is None:
                raise HTTPException(503, "Editor completions are unavailable on this server.")
            picked = hosted_suggest_model()
            api_key = settings.openrouter_api_key.get_secret_value()
            policy, data_policy = ChargePolicy("managed_model"), data_policy_for(user.username)
        if not picked:
            raise HTTPException(503, "No model can run editor completions now.")
        model = picked.removeprefix("openrouter/")
        body = {
            "model": model,
            "messages": _messages(req),
            "max_tokens": MAX_OUTPUT_TOKENS,
            "temperature": 0,
        }
        service = BudgetService(engine=engine)
        budget = None
        try:
            budget = service.create(user.username, MAX_CENTS, idempotency_key=str(uuid4()), uncapped=True)
            runtime = BudgetRuntime(service, username=user.username, budget_id=budget.id, generation=0, phase="setup")
            with httpx.Client(timeout=_TIMEOUT_SECONDS, trust_env=False) as client:
                dispatcher = OpenRouterDispatcher(
                    runtime,
                    api_key=api_key,
                    model=model,
                    role="agent",
                    policy=policy,
                    client=client,
                    data_policy=data_policy,
                )
                result = dispatcher.dispatch("/chat/completions", body)
        except BudgetError as error:
            raise budget_http_error(error) from error
        except UnpricedOperationError as error:
            raise HTTPException(422, str(error)) from error
        except httpx.HTTPError as error:
            raise HTTPException(502, "The model provider is unavailable.") from error
        except UsagePendingError as error:
            raise HTTPException(424, "Provider usage is awaiting settlement.") from error
        finally:
            if budget is not None:
                service.stop_admission(budget.id, user.username, reason="completion_finished")
        if result.status in {402, 403}:
            raise HTTPException(result.status, "The model provider refused the call.")
        if result.status >= 400:
            logger.warning("editor completion failed on %s: provider status %s", model, result.status)
            raise HTTPException(502, "The model provider failed.")
        return _parse(_content(result.body), req.suffix)

    return router

"""The chat tool that asks the user for a saved secret through a secure card (SKY-641).

The agent names the secret, says what it is for and which host gets it. The
call pauses on the approval flow, and the chat shows a password card instead
of the usual approval card. The browser saves the typed value with
``PUT /secrets`` and only then confirms. The value never passes through this
module: the tool's arguments, its stream events and its result carry the
name, the purpose and the host only, so no model, log or stored chat turn
ever holds it.
"""

from __future__ import annotations

import re
from typing import Any

import dspy

from ...models.blackbox import REPO_SECRET_NAME_PATTERN

SECRET_REQUEST_TOOL = "request_secret"
_NAME = re.compile(REPO_SECRET_NAME_PATTERN)
_HOST = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
_MAX_PURPOSE_CHARS = 200


def secret_request_problem(arguments: dict[str, Any]) -> str | None:
    """Check a ``request_secret`` call before the chat shows its card.

    Args:
        arguments: The call's ``name``, ``purpose`` and ``host``.

    Returns:
        What to fix, for the agent, or ``None`` when the card can show.
    """
    name = str(arguments.get("name") or "")
    purpose = str(arguments.get("purpose") or "").strip()
    host = str(arguments.get("host") or "")
    if not _NAME.fullmatch(name):
        return (
            "Not shown: name must be an environment variable name (letters, digits and _, not starting "
            "with a digit), the same as the ${NAME} placeholder in optimize.toml."
        )
    if not purpose or len(purpose) > _MAX_PURPOSE_CHARS:
        return f"Not shown: purpose must say in one short line (at most {_MAX_PURPOSE_CHARS} characters) what the secret is for."
    if not _HOST.fullmatch(host):
        return "Not shown: host must be the bare host name the secret goes to, for example api.example.com."
    return None


def secret_request_result(name: str, saved: bool) -> dict[str, str]:
    """Build what the agent learns from the card: the name and saved or skipped.

    Args:
        name: The secret's name.
        saved: Whether the user saved a value.

    Returns:
        The tool result, which never holds the value.
    """
    return {"name": name, "status": "saved" if saved else "skipped"}


def secret_request_tool() -> dspy.Tool:
    """Build the ``request_secret`` tool.

    The approval wrapper runs this body only after the browser saved the
    value and confirmed, so reaching it means the secret is saved.

    Returns:
        The tool, unwrapped.
    """

    async def request_secret(name: str, purpose: str, host: str) -> Any:
        """Report the secret the user saved in the card."""
        return secret_request_result(name, saved=True)

    return dspy.Tool(
        request_secret,
        name=SECRET_REQUEST_TOOL,
        desc=(
            "Show the user a secure card to save one secret on their account, such as an API key that a "
            "[services.<name>] entry in optimize.toml sends as ${NAME}. The user types the value in the card; "
            "you never see it. Returns {name, status}: status is 'saved' or 'skipped'. Use it for each secret "
            "the run needs that is not saved yet, one call at a time. Never ask for a secret value in the chat, "
            "and never repeat one the user pasted."
        ),
        arg_desc={
            "name": "The environment variable name, exactly as in the ${NAME} placeholder, e.g. MAPS_KEY.",
            "purpose": "What the secret is for, in one short line in the reply language.",
            "host": "The bare host the secret goes to, e.g. maps.googleapis.com.",
        },
    )

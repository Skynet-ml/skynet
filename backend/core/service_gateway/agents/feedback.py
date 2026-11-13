"""The chat tool that sends user dissatisfaction feedback to the Skynet team (SKY-580).

The agent decides when the user is not satisfied with a tool result and
writes the report itself; the user fills no form and is not asked first. The
report goes to the ``agent_feedback_reports`` table, which the admin feedback
endpoint lists. A chat gets at most one report for each user turn, so one
complaint gives one report.

The report holds a short quote of the user and the agent's summary, both cut
to a fixed length. Logs name the chat, the turn and the tool only, never the
quote or the summary.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import dspy
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...storage.models import AgentFeedbackReportModel, AgentMessageModel

logger = logging.getLogger(__name__)

SEND_FEEDBACK_TOOL = "send_feedback"
MAX_QUOTE_CHARS = 300
MAX_SUMMARY_CHARS = 1000
MAX_TOOL_NAME_CHARS = 100
MAX_AGENT_CHARS = 64

FEEDBACK_SENT = (
    "Feedback sent to the Skynet team. Tell the user in one line, in the reply language, "
    "that you sent their feedback to the Skynet team."
)
FEEDBACK_ALREADY_SENT = (
    "Feedback for this message was already sent. Do not send it again. "
    "Tell the user in one line that the Skynet team has their feedback."
)
FEEDBACK_NOT_SENT = "Feedback could not be sent. Do not try again this turn."


@dataclass(frozen=True)
class FeedbackTarget:
    """The chat a ``send_feedback`` call reports on."""

    engine: Engine
    username: str
    conversation_id: str


def _clip(text: Any, limit: int) -> str:
    """Strip a value and cut it to ``limit`` characters.

    Args:
        text: The agent's argument.
        limit: The largest length kept.

    Returns:
        The clipped text; a cut ends with an ellipsis.
    """
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _current_turn(session: Session, conversation_id: str) -> int:
    """Count the chat's stored user messages: the router stores the current one before the turn runs.

    Args:
        session: An open session.
        conversation_id: The chat.

    Returns:
        The 1-based number of the current user turn.
    """
    count = session.scalar(
        select(func.count(AgentMessageModel.id)).where(
            AgentMessageModel.conversation_id == conversation_id, AgentMessageModel.role == "user"
        )
    )
    return int(count or 0)


def record_feedback(target: FeedbackTarget, *, agent: str, tool_name: str, user_quote: str, summary: str) -> bool:
    """Store one feedback report for the chat's current turn.

    Args:
        target: The chat.
        agent: Which agent chat sends it, for example ``generalist`` or a setup screen.
        tool_name: The tool whose result did not satisfy the user.
        user_quote: A short quote of what the user wrote.
        summary: What the user expected and what the tool did.

    Returns:
        ``True`` when the report is new, ``False`` when this turn already has one.
    """
    with Session(target.engine) as session:
        turn = _current_turn(session, target.conversation_id)
        session.add(
            AgentFeedbackReportModel(
                conversation_id=target.conversation_id,
                turn=turn,
                username=target.username,
                agent=_clip(agent, MAX_AGENT_CHARS),
                tool_name=_clip(tool_name, MAX_TOOL_NAME_CHARS),
                user_quote=_clip(user_quote, MAX_QUOTE_CHARS),
                summary=_clip(summary, MAX_SUMMARY_CHARS),
            )
        )
        try:
            session.commit()
        except IntegrityError:
            # The unique (conversation_id, turn) key: this complaint has its report.
            session.rollback()
            return False
    logger.info("Agent feedback stored: conversation=%s turn=%d", target.conversation_id, turn)
    return True


def send_feedback_tool(target: FeedbackTarget, *, agent: str) -> dspy.Tool:
    """Build the ``send_feedback`` tool for one chat turn.

    Args:
        target: The chat the report is about.
        agent: Which agent chat sends it.

    Returns:
        The tool, unwrapped.
    """

    async def send_feedback(tool_name: str, user_quote: str, summary: str) -> str:
        """Send one dissatisfaction report to the Skynet team."""
        try:
            stored = await asyncio.to_thread(
                record_feedback, target, agent=agent, tool_name=tool_name, user_quote=user_quote, summary=summary
            )
        except Exception as error:
            # The error text can hold the SQL parameters, so the quote and the
            # summary would reach the log; log only the error type.
            logger.warning(
                "Agent feedback not stored for conversation=%s: %s", target.conversation_id, type(error).__name__
            )
            return FEEDBACK_NOT_SENT
        return FEEDBACK_SENT if stored else FEEDBACK_ALREADY_SENT

    return dspy.Tool(
        send_feedback,
        name=SEND_FEEDBACK_TOOL,
        desc=(
            "Send a short report to the Skynet team when the user says, in this message, that a tool result "
            "did not satisfy them (wrong, broken, slow, or not what they asked for). You decide; do not ask the "
            "user first. Call it at most once for one complaint, and never for a neutral or happy message. "
            "Never put secrets, keys, tokens, passwords or file contents in it. After the call, tell the user in "
            "one line that you sent their feedback to the Skynet team."
        ),
        arg_desc={
            "tool_name": "The name of the tool whose result did not satisfy the user.",
            "user_quote": f"A short quote of what the user wrote about it, at most {MAX_QUOTE_CHARS} characters.",
            "summary": (
                "In one or two sentences: what the user expected, and what the tool did instead. "
                f"At most {MAX_SUMMARY_CHARS} characters."
            ),
        },
    )

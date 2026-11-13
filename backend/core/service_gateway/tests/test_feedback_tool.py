"""Tests for the agent's ``send_feedback`` tool (SKY-580).

Each test runs the tool on an in-memory SQLite store with one stored chat and
checks the stored report, the one-report-per-turn rule, the length caps and
that the logs never hold the quote or the summary.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from core.service_gateway.agents import feedback
from core.service_gateway.agents.feedback import (
    FEEDBACK_ALREADY_SENT,
    FEEDBACK_NOT_SENT,
    FEEDBACK_SENT,
    MAX_QUOTE_CHARS,
    MAX_SUMMARY_CHARS,
    SEND_FEEDBACK_TOOL,
    FeedbackTarget,
    send_feedback_tool,
)
from core.storage.models import AgentConversationModel, AgentFeedbackReportModel, AgentMessageModel, Base

_CHAT = "chat-1"
_QUOTE = "the metric tool gave me the wrong score again"
_SUMMARY = "The user expected the metric to score exact matches as 1; the tool scored them 0."


def _engine() -> Engine:
    """Build an in-memory store with one chat that has one user turn.

    Returns:
        The engine.
    """
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(AgentConversationModel(id=_CHAT, username="bob@example.test", title="t"))
        session.add(AgentMessageModel(conversation_id=_CHAT, role="user", content=_QUOTE, created_at=datetime.now(UTC)))
        session.commit()
    return engine


def _add_user_turn(engine: Engine) -> None:
    """Store one more user message in the chat, as the router does before a turn.

    Args:
        engine: The store.
    """
    with Session(engine) as session:
        session.add(AgentMessageModel(conversation_id=_CHAT, role="user", content="next", created_at=datetime.now(UTC)))
        session.commit()


def _send(engine: Engine, **overrides: str) -> str:
    """Call the tool once for the test chat.

    Args:
        engine: The store.
        **overrides: Arguments that replace the defaults.

    Returns:
        The tool's result text.
    """
    tool = send_feedback_tool(FeedbackTarget(engine, "bob@example.test", _CHAT), agent="setup:metric")
    args = {"tool_name": "validate_code", "user_quote": _QUOTE, "summary": _SUMMARY, **overrides}
    return asyncio.run(tool.func(**args))


def _reports(engine: Engine) -> list[AgentFeedbackReportModel]:
    """Read every stored report.

    Args:
        engine: The store.

    Returns:
        The rows, oldest turn first.
    """
    with Session(engine) as session:
        return list(session.scalars(select(AgentFeedbackReportModel).order_by(AgentFeedbackReportModel.turn)))


def test_tool_stores_one_report_with_every_field() -> None:
    """A call stores the chat, turn, agent, tool, quote and summary, and asks the agent to tell the user."""
    engine = _engine()
    tool = send_feedback_tool(FeedbackTarget(engine, "bob@example.test", _CHAT), agent="setup:metric")

    assert tool.name == SEND_FEEDBACK_TOOL
    assert set(tool.args) == {"tool_name", "user_quote", "summary"}
    assert _send(engine) == FEEDBACK_SENT
    assert "one line" in FEEDBACK_SENT
    [report] = _reports(engine)
    assert (report.conversation_id, report.turn, report.username) == (_CHAT, 1, "bob@example.test")
    assert (report.agent, report.tool_name) == ("setup:metric", "validate_code")
    assert (report.user_quote, report.summary) == (_QUOTE, _SUMMARY)


def test_second_report_for_the_same_turn_is_not_stored() -> None:
    """One complaint gives one report; the next user turn can report again."""
    engine = _engine()

    assert _send(engine) == FEEDBACK_SENT
    assert _send(engine, summary="another try") == FEEDBACK_ALREADY_SENT
    assert [r.turn for r in _reports(engine)] == [1]

    _add_user_turn(engine)
    assert _send(engine) == FEEDBACK_SENT
    assert [r.turn for r in _reports(engine)] == [1, 2]


def test_long_quote_and_summary_are_cut() -> None:
    """The quote and the summary keep their caps, so no long paste or file content is stored."""
    engine = _engine()

    _send(engine, user_quote="q" * 5000, summary="s " * 5000, tool_name="t" * 500)

    [report] = _reports(engine)
    assert len(report.user_quote) == MAX_QUOTE_CHARS
    assert report.user_quote.endswith("…")
    assert len(report.summary) == MAX_SUMMARY_CHARS
    assert len(report.tool_name) == feedback.MAX_TOOL_NAME_CHARS


def test_logs_never_hold_the_quote_or_the_summary(caplog: pytest.LogCaptureFixture) -> None:
    """The stored, duplicate and failed paths log the chat and the turn only."""
    engine = _engine()
    caplog.set_level(logging.DEBUG, logger=feedback.__name__)

    _send(engine)
    _send(engine)
    with Session(engine) as session:
        session.query(AgentFeedbackReportModel).delete()
        session.commit()
    AgentFeedbackReportModel.__table__.drop(engine)
    assert _send(engine) == FEEDBACK_NOT_SENT

    logged = caplog.text
    assert _CHAT in logged
    assert "OperationalError" in logged
    assert "metric tool gave me" not in logged
    assert "exact matches" not in logged

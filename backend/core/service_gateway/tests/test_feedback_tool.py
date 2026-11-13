"""Tests for the agent's ``send_feedback`` tool (SKY-580).

Each test runs the tool on an in-memory SQLite store with one stored chat and
checks the stored report, the one-report-per-user-message rule, that an edit
or a regenerate keeps the reports, the length caps and that the logs never
hold the quote or the summary.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from core.api.routers.generalist_agent import _discard_from_user_turn, _discard_latest_turn, _persist_user_turn
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
    """Build an in-memory store with one chat that has one user message, id 1.

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


def _send(engine: Engine, message_id: int = 1, **overrides: str) -> str:
    """Call the tool once for a user message of the test chat.

    Args:
        engine: The store.
        message_id: The stored user message the report is about.
        **overrides: Arguments that replace the defaults.

    Returns:
        The tool's result text.
    """
    tool = send_feedback_tool(FeedbackTarget(engine, "bob@example.test", _CHAT, message_id), agent="setup:metric")
    args = {"tool_name": "validate_code", "user_quote": _QUOTE, "summary": _SUMMARY, **overrides}
    return asyncio.run(tool.func(**args))


def _reports(engine: Engine) -> list[AgentFeedbackReportModel]:
    """Read every stored report.

    Args:
        engine: The store.

    Returns:
        The rows, oldest user message first.
    """
    with Session(engine) as session:
        return list(
            session.scalars(select(AgentFeedbackReportModel).order_by(AgentFeedbackReportModel.user_message_id))
        )


def test_tool_stores_one_report_with_every_field() -> None:
    """A call stores the chat, user message, agent, tool, quote and summary, and asks the agent to tell the user."""
    engine = _engine()
    tool = send_feedback_tool(FeedbackTarget(engine, "bob@example.test", _CHAT, 1), agent="setup:metric")

    assert tool.name == SEND_FEEDBACK_TOOL
    assert set(tool.args) == {"tool_name", "user_quote", "summary"}
    assert _send(engine) == FEEDBACK_SENT
    assert "one line" in FEEDBACK_SENT
    [report] = _reports(engine)
    assert (report.conversation_id, report.user_message_id, report.username) == (_CHAT, 1, "bob@example.test")
    assert (report.agent, report.tool_name) == ("setup:metric", "validate_code")
    assert (report.user_quote, report.summary) == (_QUOTE, _SUMMARY)


def test_second_report_for_the_same_message_is_not_stored() -> None:
    """One complaint gives one report; the next user message can report again."""
    engine = _engine()

    assert _send(engine) == FEEDBACK_SENT
    assert _send(engine, summary="another try") == FEEDBACK_ALREADY_SENT
    assert [r.user_message_id for r in _reports(engine)] == [1]

    next_id = _persist_user_turn(SimpleNamespace(engine=engine), _CHAT, "next")
    assert _send(engine, next_id) == FEEDBACK_SENT
    assert [r.user_message_id for r in _reports(engine)] == [1, next_id]


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
    """The stored, duplicate and failed paths log the chat and the user message only."""
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


def test_edit_of_an_earlier_message_keeps_the_later_report() -> None:
    """An edit of message 1 deletes messages 1 to 3 but keeps message 3's report; the edited text can report."""
    engine = _engine()
    store = SimpleNamespace(engine=engine)
    _persist_user_turn(store, _CHAT, "u2")
    third = _persist_user_turn(store, _CHAT, "the score tool is wrong")
    assert _send(engine, third, user_quote="old complaint") == FEEDBACK_SENT

    assert _discard_from_user_turn(store, _CHAT, _QUOTE, 0)
    edited = _persist_user_turn(store, _CHAT, "the search tool is broken")

    assert _send(engine, edited, user_quote="new complaint") == FEEDBACK_SENT
    assert sorted((r.user_message_id, r.user_quote) for r in _reports(engine)) == sorted(
        [(third, "old complaint"), (edited, "new complaint")]
    )


def test_regenerate_keeps_the_report() -> None:
    """Regenerate deletes the latest user message, but its report and earlier reports stay."""
    engine = _engine()
    store = SimpleNamespace(engine=engine)
    assert _send(engine, 1, user_quote="message one") == FEEDBACK_SENT
    second = _persist_user_turn(store, _CHAT, "retry this")
    assert _send(engine, second, user_quote="message two") == FEEDBACK_SENT

    _discard_latest_turn(store, _CHAT)
    _persist_user_turn(store, _CHAT, "retry this")

    assert [(r.user_message_id, r.user_quote) for r in _reports(engine)] == [
        (1, "message one"),
        (second, "message two"),
    ]


def test_deleted_chat_is_not_sent_and_not_called_a_duplicate() -> None:
    """A chat deleted during the turn gives "not sent" and no row, with foreign keys on or off."""
    engine = _engine()
    with Session(engine) as session:
        session.query(AgentConversationModel).delete()
        session.commit()

    assert _send(engine) == FEEDBACK_NOT_SENT
    assert _reports(engine) == []


def test_foreign_key_failure_after_the_chat_check_is_not_called_a_duplicate() -> None:
    """A foreign-key failure on insert (the chat goes away after the check) gives "not sent"."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _foreign_keys_on(dbapi_connection, _record) -> None:
        """Turn on SQLite foreign keys, as Postgres always checks them.

        Args:
            dbapi_connection: The raw SQLite connection.
            _record: The pool record, unused.
        """
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    real_get = Session.get

    def _get_then_delete(self: Session, *args: object, **kwargs: object) -> object:
        """Find the chat, then delete it before the insert, like a delete in another tab.

        Args:
            self: The session.
            *args: The ``Session.get`` arguments.
            **kwargs: The ``Session.get`` keyword arguments.

        Returns:
            The chat row found before the delete.
        """
        found = real_get(self, *args, **kwargs)
        with Session(engine) as other:
            other.query(AgentConversationModel).delete()
            other.commit()
        return found

    with Session(engine) as session:
        session.add(AgentConversationModel(id=_CHAT, username="bob@example.test", title="t"))
        session.commit()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Session, "get", _get_then_delete)
        assert _send(engine) == FEEDBACK_NOT_SENT
    assert _reports(engine) == []

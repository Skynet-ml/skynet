"""Tests for the agent's ``send_feedback`` tool (SKY-580).

Each test runs the tool on an in-memory SQLite store with one stored chat and
checks the stored report, the one-report-per-turn rule, the length caps and
that the logs never hold the quote or the summary.
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


def test_edit_and_resend_frees_the_cut_turn_for_a_new_complaint() -> None:
    """An edited turn loses its report, so the complaint in the new text is stored, not called a duplicate."""
    engine = _engine()
    store = SimpleNamespace(engine=engine)
    _persist_user_turn(store, _CHAT, "the score tool is wrong")
    assert _send(engine, user_quote="old complaint") == FEEDBACK_SENT

    assert _discard_from_user_turn(store, _CHAT, "the score tool is wrong", 0)
    _persist_user_turn(store, _CHAT, "the search tool is broken")

    assert _send(engine, user_quote="new complaint") == FEEDBACK_SENT
    assert [(r.turn, r.user_quote) for r in _reports(engine)] == [(2, "new complaint")]


def test_regenerate_frees_the_cut_turn_and_keeps_earlier_reports() -> None:
    """Regenerate drops only the latest turn's report; reports of earlier turns stay."""
    engine = _engine()
    store = SimpleNamespace(engine=engine)
    assert _send(engine, user_quote="turn one") == FEEDBACK_SENT
    _persist_user_turn(store, _CHAT, "retry this")
    assert _send(engine, user_quote="turn two") == FEEDBACK_SENT

    _discard_latest_turn(store, _CHAT)
    _persist_user_turn(store, _CHAT, "retry this")

    assert _send(engine, user_quote="turn two again") == FEEDBACK_SENT
    assert [(r.turn, r.user_quote) for r in _reports(engine)] == [(1, "turn one"), (2, "turn two again")]


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

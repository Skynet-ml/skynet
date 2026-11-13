"""Tests for the model title a new agent chat gets in parallel with its first turn."""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import threading
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ...billing.cli_dispatch import CHAT_TITLE_ROLE
from ...models import ModelConfig
from ...service_gateway.agents.chat_title import with_title_event
from ...storage.models import AgentConversationModel, Base
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from ..routers import generalist_agent as agent_mod

_USER = "alice@example.com"
# The conftest stubs the title path out for every other test; these tests
# run the real one.
_REAL_AI_TITLE = agent_mod._ai_title


async def _fake_turn(**_kwargs: Any) -> AsyncIterator[dict[str, Any]]:
    """Stand in for ``run_generalist_agent`` with a one-reply turn.

    Yields:
        The turn's events.
    """
    yield {"event": "message_patch", "data": {"chunk": "ok"}}
    yield {"event": "done", "data": {"assistant_message": "ok", "model": "test-model"}}


class _Store:
    """Job-store double exposing only the engine."""

    def __init__(self, engine: Engine) -> None:
        """Store the engine.

        Args:
            engine: The SQLite engine.
        """
        self.engine = engine


class _TitleLM:
    """A DSPy-shaped LM that returns a fixed answer and can run a side effect first."""

    def __init__(self, answer: str | Exception, before: Any = None) -> None:
        """Store the answer and the side effect.

        Args:
            answer: The completion text, or an exception to raise.
            before: Called before the answer, for a rename during the call.
        """
        self.answer = answer
        self.before = before
        self.calls: list[list[dict[str, str]]] = []

    def __call__(self, *, messages: list[dict[str, str]]) -> list[str]:
        """Record the prompt and answer.

        Args:
            messages: The chat messages.

        Returns:
            One completion.

        Raises:
            Exception: The configured error.
        """
        self.calls.append(messages)
        if self.before is not None:
            self.before()
        if isinstance(self.answer, Exception):
            raise self.answer
        return [self.answer]


@pytest.fixture
def engine() -> Engine:
    """Build a SQLite engine with the app tables.

    Returns:
        The engine.
    """
    db = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(db)
    return db


@pytest.fixture
def metered(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Run the real title path offline and record its metering.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        The recorded metering calls.
    """
    monkeypatch.setattr(agent_mod, "_ai_title", _REAL_AI_TITLE)
    monkeypatch.setattr(agent_mod, "run_generalist_agent", _fake_turn)
    monkeypatch.setattr(agent_mod, "queue_conversation_embed", lambda *a, **k: None)
    monkeypatch.setattr(agent_mod, "enforce_llm_balance", lambda job_store, username: None)
    monkeypatch.setattr(agent_mod, "hosted_suggest_model", lambda: "openrouter/acme/small")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        agent_mod,
        "meter_llm_run",
        lambda engine, username, lms, **kwargs: calls.append({"username": username, "lms": lms, **kwargs}),
    )
    return calls


def _use_lm(monkeypatch: pytest.MonkeyPatch, lm: _TitleLM) -> None:
    """Make the side-task model builder return ``lm``.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        lm: The title LM.
    """
    monkeypatch.setattr(agent_mod, "build_hosted_suggest_lm", lambda model, timeout: lm)


def _post(engine: Engine, message: str, conversation_id: str | None = None) -> list[dict[str, Any]]:
    """Send one turn and parse its SSE events.

    Args:
        engine: The SQLite engine behind the store.
        message: The user message.
        conversation_id: An existing conversation, if any.

    Returns:
        The ``conversation_meta`` and ``conversation_title`` events' data, in
        order, each with its ``event`` name.
    """
    store = _Store(engine)
    app = FastAPI()
    app.include_router(agent_mod.create_generalist_agent_router(job_store=store))
    app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser(username=_USER, role="user", groups=())
    res = TestClient(app).post(
        "/optimizations/generalist-agent",
        json={
            "user_message": message,
            "chat_history": [],
            "wizard_state": {},
            "trust_mode": "ask",
            "conversation_id": conversation_id,
        },
    )
    assert res.status_code == 200
    return [
        {"event": block.split("\n", 1)[0].removeprefix("event: "), **json.loads(block.split("data: ", 1)[1])}
        for block in res.text.split("\n\n")
        if block.startswith(("event: conversation_meta", "event: conversation_title"))
    ]


def _titles(engine: Engine) -> list[str]:
    """Read every stored conversation title.

    Args:
        engine: The SQLite engine.

    Returns:
        The titles.
    """
    with Session(engine) as session:
        return [row.title for row in session.query(AgentConversationModel).all()]


def test_new_chat_gets_the_model_title_in_the_stream_and_the_row(
    monkeypatch: pytest.MonkeyPatch, engine: Engine, metered: list[dict[str, Any]]
) -> None:
    """The cut title goes first, then the model title; the row keeps it and the call is metered."""
    lm = _TitleLM('"Tune the support bot prompt."')
    _use_lm(monkeypatch, lm)
    message = "Please help me make my customer support bot prompt answer refunds better"
    metas = _post(engine, message)
    cut = agent_mod._derive_title(message)
    assert [m["title"] for m in metas] == [cut, "Tune the support bot prompt"]
    assert [m["event"] for m in metas] == ["conversation_meta", "conversation_title"]
    assert metas[0]["conversation_id"] == metas[1]["conversation_id"]
    assert metas[1]["cut_title"] == cut
    assert _titles(engine) == ["Tune the support bot prompt"]
    assert lm.calls[0][1]["content"] == message
    title_meters = [m for m in metered if m["description"] == "Agent chat title"]
    assert title_meters == [
        {"username": _USER, "lms": [lm], "description": "Agent chat title", "token_source": "managed"}
    ]


def test_model_failure_keeps_the_cut_title_and_still_meters(
    monkeypatch: pytest.MonkeyPatch, engine: Engine, metered: list[dict[str, Any]]
) -> None:
    """A failed title call keeps the cut title, sends an empty ``conversation_title`` event, and is metered."""
    _use_lm(monkeypatch, _TitleLM(RuntimeError("upstream timeout")))
    metas = _post(engine, "fix my csv upload")
    assert [m["event"] for m in metas] == ["conversation_meta", "conversation_title"]
    assert [m["title"] for m in metas] == ["fix my csv upload", ""]
    assert _titles(engine) == ["fix my csv upload"]
    assert [m["description"] for m in metered].count("Agent chat title") == 1


def test_empty_answer_keeps_the_cut_title(
    monkeypatch: pytest.MonkeyPatch, engine: Engine, metered: list[dict[str, Any]]
) -> None:
    """An empty answer keeps the cut title and sends an empty title event, and the call is metered once."""
    _use_lm(monkeypatch, _TitleLM("  "))
    assert [m["title"] for m in _post(engine, "fix my csv upload")] == ["fix my csv upload", ""]
    assert _titles(engine) == ["fix my csv upload"]
    assert [m["description"] for m in metered].count("Agent chat title") == 1


def test_a_model_title_equal_to_the_cut_title_sends_an_empty_title_event(
    monkeypatch: pytest.MonkeyPatch, engine: Engine, metered: list[dict[str, Any]]
) -> None:
    """A model title that repeats the cut title changes nothing, and the call is metered once."""
    _use_lm(monkeypatch, _TitleLM("fix my csv upload"))
    metas = _post(engine, "fix my csv upload")
    assert [(m["event"], m["title"]) for m in metas] == [
        ("conversation_meta", "fix my csv upload"),
        ("conversation_title", ""),
    ]
    assert [m["description"] for m in metered].count("Agent chat title") == 1


def test_a_file_only_first_message_makes_no_title_call(
    monkeypatch: pytest.MonkeyPatch, engine: Engine, metered: list[dict[str, Any]]
) -> None:
    """A first message with only a file keeps the file's name, with no model call and no charge."""
    lm = _TitleLM("Model title")
    _use_lm(monkeypatch, lm)
    metas = _post(engine, '<attached_file name="data.csv">a,b</attached_file>')
    assert [m["title"] for m in metas] == ["data.csv", ""]
    assert lm.calls == []
    assert "Agent chat title" not in [m["description"] for m in metered]


@pytest.mark.usefixtures("metered")
def test_the_wizard_back_mark_does_not_reach_the_title_model(monkeypatch: pytest.MonkeyPatch, engine: Engine) -> None:
    """A first message after the wizard's back step gives the model only the typed words."""
    lm = _TitleLM("Fix the CSV upload")
    _use_lm(monkeypatch, lm)
    metas = _post(engine, agent_mod._WIZARD_BACK_MARK + " fix my csv upload")
    assert [m["title"] for m in metas] == ["fix my csv upload", "Fix the CSV upload"]
    assert lm.calls[0][1]["content"] == "fix my csv upload"


@pytest.mark.usefixtures("metered")
def test_a_rename_during_the_call_is_never_overwritten(monkeypatch: pytest.MonkeyPatch, engine: Engine) -> None:
    """A user rename that lands while the model writes stays."""

    def rename() -> None:
        """Rename the chat the way the history route does."""
        with Session(engine) as session:
            for row in session.query(AgentConversationModel).all():
                row.title = "My own name"
            session.commit()

    _use_lm(monkeypatch, _TitleLM("Model title", before=rename))
    metas = _post(engine, "fix my csv upload")
    assert [m["title"] for m in metas] == ["fix my csv upload", ""]
    assert _titles(engine) == ["My own name"]


@pytest.mark.usefixtures("metered")
def test_later_turns_and_the_wizard_kickoff_get_no_model_title(monkeypatch: pytest.MonkeyPatch, engine: Engine) -> None:
    """Only the first turn of a new chat with a cut title asks the model."""
    lm = _TitleLM("Model title")
    _use_lm(monkeypatch, _TitleLM(""))
    first = _post(engine, "fix my csv upload")
    _use_lm(monkeypatch, lm)
    later = _post(engine, "and the json one", conversation_id=first[0]["conversation_id"])
    kickoff = _post(engine, agent_mod._WIZARD_KICKOFF_MARK)
    assert [m["title"] for m in later] == ["fix my csv upload"]
    assert [m["title"] for m in kickoff] == [""]
    assert lm.calls == []


def test_blocked_balance_makes_no_title_call_and_no_charge(
    monkeypatch: pytest.MonkeyPatch, engine: Engine, metered: list[dict[str, Any]]
) -> None:
    """A balance that blocks the title call keeps the cut title, with no model call and no charge."""
    gate_calls: list[str] = []

    def gate(job_store: object, username: str) -> None:
        """Pass the turn's own check, then refuse the title's, like a balance that ran out.

        Args:
            job_store: Unused.
            username: Unused.

        Raises:
            DomainError: On every call after the first.
        """
        gate_calls.append(username)
        if len(gate_calls) > 1:
            raise DomainError("billing.insufficient_funds", status=402)

    monkeypatch.setattr(agent_mod, "enforce_llm_balance", gate)
    built: list[str] = []
    monkeypatch.setattr(agent_mod, "build_hosted_suggest_lm", lambda model, timeout: built.append(model))
    metas = _post(engine, "fix my csv upload")
    assert [m["title"] for m in metas] == ["fix my csv upload", ""]
    assert len(gate_calls) == 2
    assert built == []
    assert [m for m in metered if m["description"] == "Agent chat title"] == []


def test_mac_app_title_runs_on_the_engine_and_meters_its_key(
    monkeypatch: pytest.MonkeyPatch, engine: Engine, metered: list[dict[str, Any]]
) -> None:
    """In the Mac app the title runs on the engine's suggestion model and bills the user's key route."""
    monkeypatch.setattr(agent_mod.settings, "local_mode", True)
    monkeypatch.setattr(agent_mod, "engine_agent_model", lambda store, user, model: model)
    config = ModelConfig(name="openrouter/acme/big", token_source="byok")
    monkeypatch.setattr(agent_mod, "route_local_byok_model", lambda picked, **kwargs: config)
    lm = _TitleLM("Fix the CSV upload")
    seen: list[tuple[ModelConfig, str]] = []

    def build(turn_config: ModelConfig, timeout: float, role: str) -> _TitleLM:
        """Record the turn config and role and return the title LM.

        Args:
            turn_config: The routed turn config.
            timeout: Unused.
            role: The CLI side-call role.

        Returns:
            The title LM.
        """
        seen.append((turn_config, role))
        return lm

    monkeypatch.setattr(agent_mod, "build_local_suggest_lm", build)
    metas = _post(engine, "fix my csv upload")
    assert [m["title"] for m in metas] == ["fix my csv upload", "Fix the CSV upload"]
    assert seen == [(config, CHAT_TITLE_ROLE)]
    title_meters = [m for m in metered if m["description"] == "Agent chat title"]
    assert [m["token_source"] for m in title_meters] == [agent_mod.metering_token_source(config)] == ["byok"]


def test_mac_app_chat_title_uses_its_own_cli_role(monkeypatch: pytest.MonkeyPatch) -> None:
    """On a CLI engine the chat title builds its model in the title role, so a suggestion cannot stop it."""
    monkeypatch.setattr(agent_mod.settings, "local_mode", True)
    monkeypatch.setattr(agent_mod, "enforce_llm_balance", lambda store, user: None)
    monkeypatch.setattr(agent_mod, "engine_agent_model", lambda store, user, model: model)
    monkeypatch.setattr(agent_mod, "route_local_byok_model", lambda *a, **k: None)
    config = ModelConfig(name="claude-sonnet-4.5", token_source="subscription", cli_provider="claude")
    monkeypatch.setattr(agent_mod, "route_agent_model", lambda picked: config)
    seen: list[str] = []
    monkeypatch.setattr(agent_mod, "build_local_suggest_lm", lambda config, timeout, role: seen.append(role))

    title = _REAL_AI_TITLE(None, _USER, "subscription/claude/claude-sonnet-4.5", "c1", "Help me", "Help me")

    assert title == ""
    assert seen == [CHAT_TITLE_ROLE]


async def _turn_events() -> AsyncIterator[dict[str, Any]]:
    """Yield a short turn whose title is still running when it ends.

    Yields:
        The turn's events.
    """
    yield {"event": "conversation_meta", "data": {}}
    yield {"event": "done", "data": {}}


@pytest.mark.usefixtures("metered")
async def test_a_disconnect_during_the_title_wait_still_saves_the_title(
    monkeypatch: pytest.MonkeyPatch,
    engine: Engine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A client that leaves while the stream waits for the title leaves the title job running to its save."""
    release = threading.Event()
    _use_lm(monkeypatch, _TitleLM("Fix the CSV upload", before=lambda: release.wait(5)))
    now = datetime.now(UTC)
    with Session(engine) as session:
        session.add(
            AgentConversationModel(
                id="c1", username=_USER, title="fix my csv upload", pinned=False, created_at=now, updated_at=now
            )
        )
        session.commit()
    task = agent_mod._start_title_task(_Store(engine), _USER, None, "c1", "fix my csv upload", "fix my csv upload")
    stream = with_title_event(_turn_events(), task, "c1", "cut")
    names = [(await anext(stream))["event"] for _ in range(3)]
    assert names == ["conversation_meta", "done", "turn_billed"]
    waiting = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0.05)
    assert not waiting.done()
    with caplog.at_level(logging.ERROR, logger="asyncio"):
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        await stream.aclose()
        release.set()
        assert await task == "Fix the CSV upload"
        gc.collect()
    assert _titles(engine) == ["Fix the CSV upload"]
    assert "Task was destroyed" not in caplog.text
    assert not agent_mod._TITLE_TASKS

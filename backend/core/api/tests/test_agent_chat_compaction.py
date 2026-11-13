"""Route tests for agent chat compaction: the automatic threshold run and ``/compact``."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ...models.common import ModelConfig
from ...service_gateway.agents import compaction
from ...storage.models import AgentConversationModel, AgentMessageModel
from ..routers import _helpers as helpers_mod
from ..routers import generalist_agent as agent_mod
from ..routers.agent_history import ConversationDetail, conversation_detail
from .test_generalist_persistence import _fund, _session_token, persistence_client  # noqa: F401

_SUMMARY = "## Goal\nShip the metric."


class _SummaryLM:
    """Stand in for the turn's metered LM: answers the summary call and reports usage."""

    model = "openai/gpt-test"

    def __init__(self) -> None:
        """Start with no calls."""
        self.history: list[dict[str, Any]] = []
        self.calls: list[list[dict[str, str]]] = []

    def __call__(self, *, messages: list[dict[str, str]]) -> list[str]:
        """Record the call and its usage, and return the summary.

        Args:
            messages: The summary call's messages.

        Returns:
            The summary as the one output.
        """
        self.calls.append(messages)
        self.history.append({"usage": {"prompt_tokens": 900, "completion_tokens": 120}})
        return [_SUMMARY]


@pytest.fixture
def compaction_env(
    persistence_client: tuple[TestClient, Engine],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """Fund the caller, fake the summary LM and capture the agent's history and the metering.

    Args:
        persistence_client: The generalist router over SQLite.
        monkeypatch: Pytest fixture for the stubs.

    Returns:
        The client, the engine, the fake LM and the captured values.
    """
    client, engine = persistence_client
    _fund(engine)
    lm = _SummaryLM()
    seen: dict[str, Any] = {"metered": []}

    async def capture_stream(**kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        """Record the history the agent gets and finish the turn.

        Args:
            **kwargs: The agent's arguments.

        Yields:
            One ``done`` event.
        """
        seen["history"] = kwargs["chat_history"]
        seen["user_message"] = kwargs["user_message"]
        yield {"event": "done", "data": {"assistant_message": "ok", "model": "m"}}

    def capture_meter(_engine: Any, _username: str, lms: list, **kwargs: Any) -> None:
        """Record the LMs the turn debits.

        Args:
            _engine: Unused.
            _username: Unused.
            lms: The turn's usage sink.
            **kwargs: The ledger arguments.
        """
        seen["metered"].append((list(lms), kwargs["description"]))

    monkeypatch.setattr(agent_mod, "run_generalist_agent", capture_stream)
    monkeypatch.setattr(agent_mod, "build_turn_lm", lambda _config: lm)
    monkeypatch.setattr(agent_mod, "_context_window", lambda _name: 60_000)
    monkeypatch.setattr(helpers_mod, "meter_llm_run", capture_meter)
    return {"client": client, "engine": engine, "lm": lm, "seen": seen}


def _long_chat(pairs: int) -> list[dict[str, Any]]:
    """Build a chat of ``pairs`` user/assistant turns of 4000 characters each.

    Args:
        pairs: How many exchanges.

    Returns:
        ``chat_history`` turns, oldest first.
    """
    turns: list[dict[str, Any]] = []
    for i in range(pairs):
        turns.append({"role": "user", "content": f"q{i} " + "u" * 3990})
        turns.append({"role": "assistant", "content": f"a{i} " + "a" * 3990})
    return turns


def _events(text: str) -> list[tuple[str, dict[str, Any]]]:
    """Parse an SSE body into ``(event, data)`` pairs.

    Args:
        text: The response body.

    Returns:
        The events in order.
    """
    out = []
    for block in text.split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line and not line.startswith(":"))
        if "event" in lines:
            out.append((lines["event"], json.loads(lines.get("data", "{}"))))
    return out


def _auth() -> dict[str, str]:
    """Return the caller's bearer header."""
    return {"Authorization": f"Bearer {_session_token()}"}


def test_turn_past_the_threshold_compacts_before_the_reply(compaction_env: dict[str, Any]) -> None:
    """A long chat is summarized first; the agent gets the summary pair plus the kept turns."""
    chat = _long_chat(20)
    resp = compaction_env["client"].post(
        "/optimizations/generalist-agent",
        json={"user_message": "go on", "chat_history": chat, "wizard_state": {}, "trust_mode": "ask"},
        headers=_auth(),
    )

    events = _events(resp.text)
    names = [name for name, _ in events]
    assert names.index("compaction_start") < names.index("compaction") < names.index("done")
    entry = dict(events)["compaction"]
    assert entry["reason"] == "threshold"
    assert entry["summary"] == _SUMMARY
    assert entry["summarized_turns"] + entry["kept_turns"] == len(chat)
    assert entry["tokens_after"] < entry["tokens_before"]
    assert (entry["input_tokens"], entry["output_tokens"]) == (900, 120)

    history = compaction_env["seen"]["history"]
    kept = chat[entry["summarized_turns"] :]
    assert history[0]["content"] == compaction.SUMMARY_PREFIX + _SUMMARY + compaction.SUMMARY_SUFFIX
    assert history[1] == {"role": "assistant", "content": compaction.SUMMARY_ACK}
    assert [h["content"] for h in history[2:]] == [t["content"] for t in kept]
    assert kept[0]["role"] == "user"


def _tool_chat(results: int) -> list[dict[str, Any]]:
    """Build a short chat whose one assistant turn holds ``results`` tool results of 4000 characters.

    Args:
        results: How many tool calls the assistant turn made.

    Returns:
        ``chat_history`` turns, oldest first.
    """
    calls = [{"tool": f"t{i}", "status": "done", "result": f"{i} ".ljust(4000, "r")} for i in range(results)]
    return [{"role": "user", "content": "read it all"}, {"role": "assistant", "content": "read", "tool_calls": calls}]


def _full_tokens(chat: list[dict[str, Any]]) -> int:
    """Estimate the prompt of a turn on ``chat`` with every tool result in full.

    Args:
        chat: ``chat_history`` turns.

    Returns:
        The estimate.
    """
    return compaction.context_tokens(
        agent_mod.history_for_agent([agent_mod.ChatTurn(**t) for t in chat]), "go on", None
    )


def test_old_tool_results_are_cleared_before_any_compaction(
    compaction_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """At 40% of the window the oldest batch of results goes; that frees enough room, so no summary call runs."""
    chat = _tool_chat(12)
    window = int(_full_tokens(chat) / compaction.CLEAR_AT)
    monkeypatch.setattr(agent_mod, "_context_window", lambda _name: window)

    resp = compaction_env["client"].post(
        "/optimizations/generalist-agent", json=_turn(chat, message="go on"), headers=_auth()
    )

    assert "compaction_start" not in [name for name, _ in _events(resp.text)]
    assert compaction_env["lm"].calls == []
    results = [call["result"] for call in compaction_env["seen"]["history"][1]["tools"]]
    assert results[:5] == [compaction.cleared_tool_result(f"t{i}") for i in range(5)]
    assert results[5:] == [call["result"] for call in chat[1]["tool_calls"][5:]]


def test_chat_below_the_clearing_trigger_is_sent_in_full(
    compaction_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Just below the clearing trigger, every tool result goes to the agent as the client sent it."""
    chat = _tool_chat(12)
    window = int(_full_tokens(chat) / compaction.CLEAR_AT) + 100
    monkeypatch.setattr(agent_mod, "_context_window", lambda _name: window)

    resp = compaction_env["client"].post(
        "/optimizations/generalist-agent", json=_turn(chat, message="go on"), headers=_auth()
    )

    assert "compaction_start" not in [name for name, _ in _events(resp.text)]
    results = [call["result"] for call in compaction_env["seen"]["history"][1]["tools"]]
    assert results == [call["result"] for call in chat[1]["tool_calls"]]


def test_compaction_runs_at_80_percent_only_after_clearing(
    compaction_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chat that clearing cannot bring below 80% compacts, and the compaction measures the already cleared prompt."""
    chat = _tool_chat(12) + _long_chat(10)
    full = _full_tokens(chat)
    monkeypatch.setattr(agent_mod, "_context_window", lambda _name: 40_000)

    resp = compaction_env["client"].post(
        "/optimizations/generalist-agent", json=_turn(chat, message="go on"), headers=_auth()
    )

    entry = dict(_events(resp.text))["compaction"]
    # Nine results of about 1000 tokens each became short placeholders before the measure.
    assert full - entry["tokens_before"] > 8 * 1000
    assert compaction.should_compact(entry["tokens_before"], 40_000)
    assert compaction_env["lm"].calls


def test_threshold_compaction_never_folds_the_newest_user_message_into_the_summary(
    compaction_env: dict[str, Any],
) -> None:
    """The turn's own message is not summarized: it goes to the agent verbatim, after the summary and the kept turns."""
    newest = "Newest ask: keep   this  text exactly, with its spacing.\n"
    resp = compaction_env["client"].post(
        "/optimizations/generalist-agent", json=_turn(_long_chat(20), message=newest), headers=_auth()
    )

    assert "compaction" in [name for name, _ in _events(resp.text)]
    [summary_call] = compaction_env["lm"].calls
    assert all("Newest ask" not in message["content"] for message in summary_call)
    assert compaction_env["seen"]["user_message"] == newest
    history = compaction_env["seen"]["history"]
    assert history[0]["content"].startswith(compaction.SUMMARY_PREFIX)
    assert all("Newest ask" not in entry["content"] for entry in history)


def test_first_turn_after_a_manual_compact_sends_its_message_verbatim_after_the_summary(
    compaction_env: dict[str, Any],
) -> None:
    """A message queued during /compact runs next: the summary comes first and the message stays as the user wrote it."""
    chat = _long_chat(3)
    compact = dict(
        _events(
            compaction_env["client"]
            .post("/optimizations/generalist-agent/compact", json={"chat_history": chat}, headers=_auth())
            .text
        )
    )["compaction"]
    kept = chat[compact["summarized_turns"] :]
    queued = "Queued during compact: use   these  words."

    compaction_env["client"].post(
        "/optimizations/generalist-agent",
        json={**_turn(kept, message=queued), "compaction_summary": compact["summary"]},
        headers=_auth(),
    )

    assert len(compaction_env["lm"].calls) == 1
    assert all("Queued during compact" not in message["content"] for message in compaction_env["lm"].calls[0])
    assert compaction_env["seen"]["user_message"] == queued
    history = compaction_env["seen"]["history"]
    assert history[0]["content"] == compaction.SUMMARY_PREFIX + _SUMMARY + compaction.SUMMARY_SUFFIX
    assert [entry["content"] for entry in history[2:]] == [turn["content"] for turn in kept]


def test_summary_call_is_metered_with_the_turn(compaction_env: dict[str, Any]) -> None:
    """The summary LM joins the turn's usage sink, so the turn's stats and debit include it."""
    resp = compaction_env["client"].post(
        "/optimizations/generalist-agent",
        json={"user_message": "go on", "chat_history": _long_chat(20), "wizard_state": {}, "trust_mode": "ask"},
        headers=_auth(),
    )

    stats = dict(_events(resp.text))["done"]["stats"]
    assert (stats["input_tokens"], stats["output_tokens"]) == (900, 120)
    # A side call: the summary's output is not the reply's speed.
    assert stats["reply_output_tokens"] is None
    [(lms, description)] = compaction_env["seen"]["metered"]
    assert lms == [compaction_env["lm"]]
    assert description == "Agent chat"


def test_short_chat_runs_without_compaction(compaction_env: dict[str, Any]) -> None:
    """Below the threshold the agent gets the chat as sent, after the client's summary."""
    chat = _long_chat(1)
    resp = compaction_env["client"].post(
        "/optimizations/generalist-agent",
        json={
            "user_message": "go on",
            "chat_history": chat,
            "compaction_summary": "old summary",
            "wizard_state": {},
            "trust_mode": "ask",
        },
        headers=_auth(),
    )

    assert "compaction" not in [name for name, _ in _events(resp.text)]
    assert compaction_env["lm"].calls == []
    history = compaction_env["seen"]["history"]
    assert "old summary" in history[0]["content"]
    assert [h["content"] for h in history[2:]] == [t["content"] for t in chat]


def test_next_compaction_updates_the_previous_summary(compaction_env: dict[str, Any]) -> None:
    """The client's summary goes to the update prompt, so the summary is iterative."""
    compaction_env["client"].post(
        "/optimizations/generalist-agent",
        json={
            "user_message": "go on",
            "chat_history": _long_chat(20),
            "compaction_summary": "## Goal\nEarlier goal.",
            "wizard_state": {},
            "trust_mode": "ask",
        },
        headers=_auth(),
    )

    prompt = compaction_env["lm"].calls[0][1]["content"]
    assert "<previous-summary>\n## Goal\nEarlier goal.\n</previous-summary>" in prompt


def test_manual_compact_uses_instructions_and_reports_cost(compaction_env: dict[str, Any]) -> None:
    """``/compact`` summarizes all but the last exchange, with the user's focus, and meters it."""
    chat = _long_chat(2)
    resp = compaction_env["client"].post(
        "/optimizations/generalist-agent/compact",
        json={"chat_history": chat, "instructions": "keep the dataset ids"},
        headers=_auth(),
    )

    events = dict(_events(resp.text))
    entry = events["compaction"]
    assert entry["reason"] == "manual"
    assert entry["summarized_turns"] == 2
    assert entry["kept_turns"] == 2
    assert events["done"]["stats"]["input_tokens"] == 900
    prompt = compaction_env["lm"].calls[0][1]["content"]
    assert prompt.endswith("Additional focus: keep the dataset ids")
    [(lms, description)] = compaction_env["seen"]["metered"]
    assert lms == [compaction_env["lm"]]
    assert description == "Agent chat compaction"


def test_manual_compact_refuses_a_chat_with_nothing_to_summarize(compaction_env: dict[str, Any]) -> None:
    """One exchange has nothing older to fold, so no model call runs."""
    resp = compaction_env["client"].post(
        "/optimizations/generalist-agent/compact",
        json={"chat_history": _long_chat(1)},
        headers=_auth(),
    )

    assert _events(resp.text) == [("error", {"code": "nothing_to_compact"})]
    assert compaction_env["lm"].calls == []


class _FailingLM(_SummaryLM):
    """A summary LM whose call fails after it reports usage."""

    def __call__(self, *, messages: list[dict[str, str]]) -> list[str]:
        """Record the call and its usage, then fail.

        Args:
            messages: The summary call's messages.

        Raises:
            RuntimeError: Always.
        """
        super().__call__(messages=messages)
        raise RuntimeError("provider down")


def test_failed_summary_drops_the_oldest_turns_so_the_turn_fits(
    compaction_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``compaction_error`` falls back to ``fit_history``: the turn runs with the newest turns that fit."""
    monkeypatch.setattr(agent_mod, "build_turn_lm", lambda _config: _FailingLM())
    chat = _long_chat(20)
    resp = compaction_env["client"].post(
        "/optimizations/generalist-agent",
        json={"user_message": "go on", "chat_history": chat, "wizard_state": {}, "trust_mode": "ask"},
        headers=_auth(),
    )

    names = [name for name, _ in _events(resp.text)]
    assert "compaction" not in names
    assert names.index("compaction_start") < names.index("compaction_error") < names.index("done")
    history = compaction_env["seen"]["history"]
    assert 0 < len(history) < len(chat)
    assert history[0]["role"] == "user"
    assert not history[0]["content"].startswith(compaction.SUMMARY_PREFIX)
    assert [h["content"] for h in history] == [t["content"] for t in chat[len(chat) - len(history) :]]
    assert not compaction.should_compact(compaction.context_tokens(history, "go on", None), 60_000)


def test_byok_summary_call_is_metered_under_byok(
    compaction_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a BYOK route the summary call is priced and debited as BYOK, and the turn marks its part."""
    byok = ModelConfig(name="openai/gpt-test", token_source="byok", byok_provider="openrouter")
    monkeypatch.setattr(agent_mod, "route_local_byok_model", lambda *_args, **_kwargs: byok)
    priced: list[str] = []

    def price(_lms: list, token_source: str) -> int:
        """Record the billing source and return a fixed price.

        Args:
            _lms: Unused.
            token_source: The billing source of the price.

        Returns:
            7 cents.
        """
        priced.append(token_source)
        return 7

    monkeypatch.setattr(agent_mod, "estimate_run_cents", price)
    meter_sources: list[str] = []
    monkeypatch.setattr(
        helpers_mod,
        "meter_llm_run",
        lambda _engine, _username, _lms, **kwargs: meter_sources.append(kwargs["token_source"]),
    )
    resp = compaction_env["client"].post(
        "/optimizations/generalist-agent",
        json={"user_message": "go on", "chat_history": _long_chat(20), "wizard_state": {}, "trust_mode": "ask"},
        headers=_auth(),
    )

    events = dict(_events(resp.text))
    assert events["compaction"]["cents"] == 7
    assert priced == ["byok"]
    assert meter_sources == ["byok"]
    assert events["done"]["stats"]["compaction_cents"] == 7


def _saved_chat(compaction_env: dict[str, Any], turns: list[dict[str, Any]]) -> str:
    """Save a conversation whose rows are ``turns``, after one first exchange, and set each turn's ``id``.

    Args:
        compaction_env: The compaction fixture.
        turns: The rows to add, oldest first; each gets the id of its row.

    Returns:
        The conversation id.
    """
    resp = compaction_env["client"].post(
        "/optimizations/generalist-agent",
        json={"user_message": "go on", "chat_history": [], "wizard_state": {}, "trust_mode": "ask"},
        headers=_auth(),
    )
    cid = dict(_events(resp.text))["conversation_meta"]["conversation_id"]
    with Session(compaction_env["engine"]) as session:
        rows = [AgentMessageModel(conversation_id=cid, role=turn["role"], content=turn["content"]) for turn in turns]
        session.add_all(rows)
        session.commit()
        for turn, row in zip(turns, rows, strict=True):
            turn["id"] = row.id
    return cid


def _compacted_rows(engine: Engine, cid: str) -> list[int]:
    """Return the ids of the conversation's rows that hold a compaction entry.

    Args:
        engine: The test database.
        cid: The conversation id.

    Returns:
        The row ids, oldest first.
    """
    with Session(engine) as session:
        rows = session.query(AgentMessageModel).filter_by(conversation_id=cid).order_by(AgentMessageModel.id).all()
        return [row.id for row in rows if (row.router_metadata or {}).get("compaction")]


def _detail(engine: Engine, cid: str) -> ConversationDetail:
    """Load a conversation as the history route serves it.

    Args:
        engine: The test database.
        cid: The conversation id.

    Returns:
        The conversation detail.
    """
    with Session(engine) as session:
        return conversation_detail(session, session.get(AgentConversationModel, cid))


def _turn(chat: list[dict[str, Any]], cid: str | None = None, message: str = "next") -> dict[str, Any]:
    """Build a turn request body.

    Args:
        chat: The chat history.
        cid: The saved conversation, if any.
        message: The new user message.

    Returns:
        The request JSON.
    """
    return {
        "user_message": message,
        "chat_history": chat,
        "conversation_id": cid,
        "wizard_state": {},
        "trust_mode": "ask",
    }


def test_conversation_meta_names_the_saved_user_message(compaction_env: dict[str, Any]) -> None:
    """The client learns the id of its new user message, so a later compaction can name it."""
    resp = compaction_env["client"].post("/optimizations/generalist-agent", json=_turn([]), headers=_auth())

    meta = dict(_events(resp.text))["conversation_meta"]
    with Session(compaction_env["engine"]) as session:
        row = session.get(AgentMessageModel, meta["user_message_id"])
        assert (row.conversation_id, row.role, row.content) == (meta["conversation_id"], "user", "next")


def test_compaction_is_saved_on_the_row_the_client_names(compaction_env: dict[str, Any]) -> None:
    """Every user turn has the same text, so only the sent id finds the anchor row."""
    chat = _long_chat(20)
    for turn in chat:
        if turn["role"] == "user":
            turn["content"] = "same " + "u" * 3990
    cid = _saved_chat(compaction_env, chat)

    resp = compaction_env["client"].post("/optimizations/generalist-agent", json=_turn(chat, cid), headers=_auth())

    entry = dict(_events(resp.text))["compaction"]
    anchor = chat[entry["summarized_turns"]]
    assert anchor["role"] == "user"
    assert _compacted_rows(compaction_env["engine"], cid) == [anchor["id"]]
    saved = {m.id: m for m in _detail(compaction_env["engine"], cid).messages}
    assert saved[anchor["id"]].compaction["summary"] == _SUMMARY


@pytest.mark.parametrize("bad", ["unsaved_text", "assistant", "other_chat"])
def test_compaction_is_not_saved_without_a_matching_user_row(compaction_env: dict[str, Any], bad: str) -> None:
    """No id and no saved text, an assistant row or another chat's row keeps no entry, rather than a wrong one."""
    chat = _long_chat(20)
    cid = _saved_chat(compaction_env, chat)
    other = _saved_chat(compaction_env, [dict(turn) for turn in chat])
    cut = compaction.manual_cut(
        agent_mod.history_for_agent([agent_mod.ChatTurn(**t) for t in chat]), compaction.keep_tokens(60_000)
    )
    if bad == "unsaved_text":
        chat[cut].pop("id")
        chat[cut]["content"] = "never saved " + "u" * 3990
    elif bad == "assistant":
        chat[cut]["id"] = chat[cut - 1]["id"]
    else:
        chat[cut]["id"] = _first_user_id(compaction_env["engine"], other, chat[cut]["content"])

    resp = compaction_env["client"].post("/optimizations/generalist-agent", json=_turn(chat, cid), headers=_auth())

    assert "compaction" in dict(_events(resp.text))
    assert _compacted_rows(compaction_env["engine"], cid) == []
    assert _compacted_rows(compaction_env["engine"], other) == []


@pytest.mark.parametrize("later_id", [True, False])
def test_compaction_without_an_id_is_saved_on_the_row_with_its_text(
    compaction_env: dict[str, Any], later_id: bool
) -> None:
    """A kept message with no id is found by its text, also when every user turn has the same text.

    The nearest later kept message with an id, or else the turn's new row,
    bounds the search, so a reload finds the entry where the browser has it.
    """
    chat = _long_chat(20)
    for turn in chat:
        if turn["role"] == "user":
            turn["content"] = "same " + "u" * 3990
    cid = _saved_chat(compaction_env, chat)
    cut = compaction.manual_cut(
        agent_mod.history_for_agent([agent_mod.ChatTurn(**t) for t in chat]), compaction.keep_tokens(60_000)
    )
    anchor_id = chat[cut]["id"]
    kept_users = [turn for turn in chat[cut:] if turn["role"] == "user"]
    assert len(kept_users) >= 3
    for turn in kept_users[: 2 if later_id else None]:
        turn.pop("id")

    resp = compaction_env["client"].post("/optimizations/generalist-agent", json=_turn(chat, cid), headers=_auth())

    assert dict(_events(resp.text))["compaction"]["summarized_turns"] == cut
    assert _compacted_rows(compaction_env["engine"], cid) == [anchor_id]


def _rows_and_history(
    engine: Engine, turns: list[tuple[str, str, bool, bool]]
) -> tuple[str, list[agent_mod.ChatTurn], list[int | None]]:
    """Save a conversation's rows and build the browser history for them.

    Args:
        engine: The test database.
        turns: ``(role, content, saved, has_id)`` for each turn, oldest first.
            ``saved`` adds a row; ``has_id`` gives the browser turn its id.

    Returns:
        The conversation id, the browser history and each turn's row id.
    """
    cid = "anchor-chat"
    with Session(engine) as session:
        session.add(AgentConversationModel(id=cid, username="u", title="t"))
        session.commit()
        ids: list[int | None] = []
        for role, content, saved, _ in turns:
            row = AgentMessageModel(conversation_id=cid, role=role, content=content) if saved else None
            if row is not None:
                session.add(row)
                session.commit()
            ids.append(row.id if row is not None else None)
    history = [
        agent_mod.ChatTurn(role=role, content=content, id=row_id if has_id else None)
        for (role, content, _, has_id), row_id in zip(turns, ids, strict=True)
    ]
    return cid, history, ids


def _store_by_text(engine: Engine, cid: str, history: list[agent_mod.ChatTurn], cut: int) -> None:
    """Store a compaction on the kept message at ``cut``, found by its text.

    Args:
        engine: The test database.
        cid: The conversation id.
        history: The browser history.
        cut: The index of the first kept turn.
    """
    by_text = agent_mod._anchor_by_text(history, cut, None)
    agent_mod._save_compaction(SimpleNamespace(engine=engine), cid, "u", None, {"summary": _SUMMARY}, by_text)


def test_unsaved_anchor_does_not_take_an_older_row_with_its_text(
    compaction_env: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    """A cut "yes" the server never saved stores nothing, not the entry on an early saved "yes"."""
    engine = compaction_env["engine"]
    cid, history, _ = _rows_and_history(
        engine,
        [
            ("user", "yes", True, True),
            ("assistant", "ok", True, True),
            ("user", "go on", True, True),
            ("assistant", "done", True, True),
            ("user", "yes", False, False),
            ("user", "next", True, True),
        ],
    )

    _store_by_text(engine, cid, history, 4)

    assert _compacted_rows(engine, cid) == []
    assert "No user message None to store the compaction on" in caplog.text


def test_unsaved_anchor_does_not_take_an_earlier_unnamed_row_with_its_text(compaction_env: dict[str, Any]) -> None:
    """A saved "yes" with no id in the browser, then an unsaved cut "yes", stores nothing on the first one."""
    engine = compaction_env["engine"]
    cid, history, _ = _rows_and_history(
        engine,
        [
            ("user", "go on", True, True),
            ("assistant", "done", True, True),
            ("user", "yes", True, False),
            ("assistant", "ok", True, False),
            ("user", "yes", False, False),
            ("user", "next", True, True),
        ],
    )

    _store_by_text(engine, cid, history, 4)

    assert _compacted_rows(engine, cid) == []


def test_anchor_by_text_stores_on_the_cut_row_when_every_message_is_saved(compaction_env: dict[str, Any]) -> None:
    """With each "yes" saved, the entry goes on the cut "yes", not on the earlier one."""
    engine = compaction_env["engine"]
    cid, history, ids = _rows_and_history(
        engine,
        [
            ("user", "go on", True, True),
            ("assistant", "done", True, True),
            ("user", "yes", True, False),
            ("assistant", "ok", True, False),
            ("user", "yes", True, False),
            ("user", "next", True, True),
        ],
    )

    _store_by_text(engine, cid, history, 4)

    assert _compacted_rows(engine, cid) == [ids[4]]


def test_unsaved_newer_duplicate_stops_the_anchor_by_text(compaction_env: dict[str, Any]) -> None:
    """A saved cut "yes", then a newer "yes" the server never saved, stores nothing.

    The browser counts one newer "yes" to skip, but the search finds only the
    cut row. No offset names the anchor with certainty.
    """
    engine = compaction_env["engine"]
    cid, history, _ = _rows_and_history(
        engine,
        [
            ("user", "yes", True, False),
            ("user", "yes", False, False),
            ("user", "next", True, True),
        ],
    )

    _store_by_text(engine, cid, history, 0)

    assert _compacted_rows(engine, cid) == []


def _first_user_id(engine: Engine, cid: str, content: str) -> int:
    """Return the id of the conversation's user row with ``content``.

    Args:
        engine: The test database.
        cid: The conversation id.
        content: The row text.

    Returns:
        The row id.
    """
    with Session(engine) as session:
        return session.query(AgentMessageModel).filter_by(conversation_id=cid, role="user", content=content).one().id


def test_small_window_skips_a_summary_that_frees_almost_nothing(
    compaction_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a 19k window two exchanges pass the threshold; the oldest turns go, and no summary call runs."""
    monkeypatch.setattr(agent_mod, "_context_window", lambda _name: 19_000)
    chat = _long_chat(2)
    assert compaction.should_compact(
        compaction.context_tokens(agent_mod.history_for_agent([agent_mod.ChatTurn(**t) for t in chat]), "go on", None),
        19_000,
    )

    resp = compaction_env["client"].post(
        "/optimizations/generalist-agent", json=_turn(chat, message="go on"), headers=_auth()
    )

    assert "compaction_start" not in [name for name, _ in _events(resp.text)]
    assert compaction_env["lm"].calls == []
    history = compaction_env["seen"]["history"]
    assert not compaction.should_compact(compaction.context_tokens(history, "go on", None), 19_000)


def test_failed_summary_cost_is_saved_on_the_turn(
    compaction_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed summary call reports its cents, the turn marks them, and the turn's user row keeps them."""
    monkeypatch.setattr(agent_mod, "build_turn_lm", lambda _config: _FailingLM())
    monkeypatch.setattr(agent_mod, "estimate_run_cents", lambda _lms, _source: 7)
    chat = _long_chat(20)
    cid = _saved_chat(compaction_env, chat)

    resp = compaction_env["client"].post("/optimizations/generalist-agent", json=_turn(chat, cid), headers=_auth())

    events = dict(_events(resp.text))
    assert events["compaction_error"]["cents"] == 7
    assert events["done"]["stats"]["compaction_cents"] == 7
    with Session(compaction_env["engine"]) as session:
        row = session.get(AgentMessageModel, events["conversation_meta"]["user_message_id"])
        assert row.router_metadata["side_cents"] == 7
    assert sum(m.side_cents or 0 for m in _detail(compaction_env["engine"], cid).messages) == 7


def test_failed_manual_compact_cost_is_saved_on_the_latest_row(
    compaction_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed ``/compact`` call adds its cents to the chat's latest row, so /session keeps them after a reload."""
    monkeypatch.setattr(agent_mod, "build_turn_lm", lambda _config: _FailingLM())
    monkeypatch.setattr(agent_mod, "estimate_run_cents", lambda _lms, _source: 5)
    chat = _long_chat(2)
    cid = _saved_chat(compaction_env, chat)

    for _ in range(2):
        resp = compaction_env["client"].post(
            "/optimizations/generalist-agent/compact",
            json={"chat_history": chat, "conversation_id": cid},
            headers=_auth(),
        )
        assert dict(_events(resp.text))["compaction_error"]["cents"] == 5

    with Session(compaction_env["engine"]) as session:
        row = session.get(AgentMessageModel, chat[-1]["id"])
        assert row.router_metadata["side_cents"] == 10


@pytest.mark.parametrize("route", ["/optimizations/generalist-agent", "/optimizations/generalist-agent/compact"])
def test_summary_longer_than_the_cap_is_refused(compaction_env: dict[str, Any], route: str) -> None:
    """A client cannot send a summary larger than the summary call may write."""
    body = {**_turn(_long_chat(1)), "compaction_summary": "x" * (compaction.SUMMARY_MAX_CHARS + 1)}

    resp = compaction_env["client"].post(route, json=body, headers=_auth())

    assert resp.status_code == 422
    assert compaction_env["lm"].calls == []


class _BlockingLM(_SummaryLM):
    """A summary LM that waits for the test before it reports usage."""

    def __init__(self) -> None:
        """Start blocked."""
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, *, messages: list[dict[str, str]]) -> list[str]:
        """Wait for ``release``, then record the call.

        Args:
            messages: The summary call's messages.

        Returns:
            The summary as the one output.
        """
        self.entered.set()
        self.release.wait(5)
        return super().__call__(messages=messages)


async def test_stop_during_the_summary_meters_the_call_after_it_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stop while the summary call runs waits for its usage, so the debit includes it."""
    lm = _BlockingLM()
    metered_calls: list[int] = []
    monkeypatch.setattr(agent_mod, "build_turn_lm", lambda _config: lm)
    monkeypatch.setattr(
        helpers_mod,
        "meter_llm_run",
        lambda _engine, _username, lms, **_kwargs: metered_calls.append(len(lms[0].history)),
    )
    usage_sink: list = []
    chat = [agent_mod.ChatTurn(**turn) for turn in _long_chat(3)]
    events = _helpers_stream(
        agent_mod._compaction_events(
            model_config=ModelConfig(name="openai/gpt-test"),
            usage_sink=usage_sink,
            turns=chat[:4],
            previous_summary=None,
            instructions=None,
            window=60_000,
            tokens_before=1,
            kept_history=[],
            user_message="",
            reason="manual",
            result={},
        ),
        usage_sink,
    )

    async def consume() -> None:
        """Read the stream until it is cancelled."""
        async for _event in events:
            pass

    task = asyncio.create_task(consume())
    await asyncio.to_thread(lm.entered.wait, 5)
    task.cancel()
    threading.Timer(0.2, lm.release.set).start()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert metered_calls == [1]


async def test_cancelled_anyio_scope_waits_for_the_thread_without_spinning(monkeypatch: pytest.MonkeyPatch) -> None:
    """A disconnect cancels the stream's anyio scope; the wait for the thread must not spin until it ends.

    An anyio scope cancels again at every await, so an unshielded wait loop
    would call ``asyncio.wait`` thousands of times in half a second.
    """
    passes = 0
    real_wait = asyncio.wait

    async def counting_wait(*args: Any, **kwargs: Any) -> Any:
        """Count each wait, then wait.

        Args:
            *args: ``asyncio.wait`` positional arguments.
            **kwargs: ``asyncio.wait`` keyword arguments.

        Returns:
            What ``asyncio.wait`` returns.
        """
        nonlocal passes
        passes += 1
        return await real_wait(*args, **kwargs)

    monkeypatch.setattr(agent_mod.asyncio, "wait", counting_wait)
    finished: list[str] = []
    outcome: list[str] = []

    def work() -> str:
        """Sleep like a model call, then record that it ended.

        Returns:
            The call's result.
        """
        time.sleep(0.5)
        finished.append("usage")
        return "usage"

    async def run() -> None:
        """Call ``work`` through ``_finish_in_thread`` and record how it ends."""
        try:
            outcome.append(await agent_mod._finish_in_thread(work))
        except asyncio.CancelledError:
            outcome.append(f"cancelled after {finished}")
            raise

    async with anyio.create_task_group() as group:
        group.start_soon(run)
        await anyio.sleep(0.1)
        group.cancel_scope.cancel()

    assert outcome == ["cancelled after ['usage']"]
    assert passes <= 2


def _helpers_stream(source: AsyncIterator[dict[str, Any]], usage_sink: list) -> AsyncIterator[dict[str, Any]]:
    """Wrap ``source`` in the turn's metering, with a store that has an engine.

    Args:
        source: The events.
        usage_sink: The LMs to meter.

    Returns:
        The metered events.
    """
    return helpers_mod.stream_with_llm_metering(
        source,
        job_store=SimpleNamespace(engine=object()),
        username="alice",
        description="Agent chat compaction",
        usage_sink=usage_sink,
    )


async def test_entry_is_saved_before_the_compaction_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stop just after the ``compaction`` event keeps the summary, because the save ran first."""
    monkeypatch.setattr(agent_mod, "build_turn_lm", lambda _config: _SummaryLM())
    saved: list[dict[str, Any]] = []

    async def save(entry: dict[str, Any]) -> None:
        """Record the stored entry.

        Args:
            entry: The compaction entry.
        """
        saved.append(entry)

    chat = [agent_mod.ChatTurn(**turn) for turn in _long_chat(3)]
    events = agent_mod._compaction_events(
        model_config=ModelConfig(name="openai/gpt-test"),
        usage_sink=[],
        turns=chat[:4],
        previous_summary=None,
        instructions=None,
        window=60_000,
        tokens_before=1,
        kept_history=[],
        user_message="",
        reason="manual",
        result={},
        save=save,
    )
    async for event in events:
        if event["event"] == "compaction":
            break
    await events.aclose()

    assert [entry["summary"] for entry in saved] == [_SUMMARY]


def test_steer_applied_names_the_saved_steer_row(
    compaction_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The client gets the steer row id, so a later compaction can store its entry on that message."""

    async def steered(**kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        """Store one steer as the agent does, apply it, then finish the turn.

        Args:
            **kwargs: The router's arguments; ``on_steer`` stores the steer.

        Yields:
            A ``steer_applied`` and a ``done`` event.
        """
        await asyncio.to_thread(kwargs["on_steer"], "use less")
        yield {"event": agent_mod.STEER_APPLIED_EVENT, "data": {"ids": ["s1"], "text": "use less"}}
        yield {"event": "done", "data": {"assistant_message": "ok", "model": "m"}}

    monkeypatch.setattr(agent_mod, "run_generalist_agent", steered)
    resp = compaction_env["client"].post("/optimizations/generalist-agent", json=_turn([]), headers=_auth())

    data = dict(_events(resp.text))["steer_applied"]
    with Session(compaction_env["engine"]) as session:
        row = session.get(AgentMessageModel, data["message_id"])
        assert (row.role, row.content) == ("user", "use less")

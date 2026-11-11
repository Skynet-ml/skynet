"""Tests for an :class:`AgentProgram` turn on the Pi harness.

A small in-process Pi stand-in drives :class:`ReactOnPi` the way Pi does: it
asks the model relay for a message, runs each tool call through the bridge and
stops when every result in a step ends the run. The real-Pi check lives in
``test_pi_bridge.py``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import Any, cast

import dspy
import litellm
import pytest
from litellm.types.utils import Delta, ModelResponseStream, StreamingChoices, Usage

from core.config import settings
from core.service_gateway.agents import generalist as generalist_module
from core.service_gateway.agents import pi_react
from core.service_gateway.agents.agent_program import AgentProgram
from core.service_gateway.agents.code import AgentStoppedError
from core.service_gateway.agents.generalist import (
    ApprovalRegistry,
    WizardState,
    screen_nav_tools,
    screen_tools,
    tools_for,
)
from core.service_gateway.agents.pi_bridge import PiEventTranslator, ToolOutcome
from core.service_gateway.agents.pi_react import AgentReplyFormatError, ReactOnPi
from core.service_gateway.agents.steering import STEER_APPLIED_EVENT, SteerInbox
from core.service_gateway.language_models import MeteredLM

_REPLY = "assistant_message"


def _tool_message(name: str, args: dict[str, Any], index: int) -> dict[str, Any]:
    """Build an assistant message that calls one tool.

    Args:
        name: The tool.
        args: Its arguments.
        index: Makes the call id unique.

    Returns:
        The OpenAI message.
    """
    call = {"id": f"call_{index}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
    return {"role": "assistant", "content": None, "tool_calls": [call]}


class _ScriptedLM(dspy.BaseLM):
    """An LM that records requests and answers with scripted tool calls or text."""

    def __init__(self, script: Iterable[tuple[str, dict[str, Any]] | str | list[tuple[str, dict]]]) -> None:
        """Store the script.

        Args:
            script: Per call: a ``(tool, args)`` pair, a list of them (one
                message with several calls), or plain text.
        """
        super().__init__(model="scripted")
        self._script = list(script)
        self.requests: list[dict[str, Any]] = []

    @property
    def supports_function_calling(self) -> bool:
        """Report native tool-call support."""
        return True

    def forward(self, prompt=None, messages=None, **kwargs):
        """Record the request and return the next scripted message.

        Args:
            prompt: Unused legacy prompt.
            messages: The rendered chat messages.
            **kwargs: Provider kwargs.

        Returns:
            An OpenAI-shaped response.
        """
        self.requests.append({"messages": messages, **kwargs})
        step = self._script.pop(0)
        if isinstance(step, str):
            message: dict[str, Any] = {"role": "assistant", "content": step}
            finish = "stop"
        else:
            calls = step if isinstance(step, list) else [step]
            message = {"role": "assistant", "content": None, "tool_calls": []}
            for name, args in calls:
                message["tool_calls"] += _tool_message(
                    name, args, len(self.requests) * 10 + len(message["tool_calls"])
                )["tool_calls"]
            finish = "tool_calls"
        return litellm.ModelResponse(
            model="scripted",
            choices=[{"index": 0, "finish_reason": finish, "message": message}],
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )


def _merge(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge relayed stream chunks into one assistant message, as Pi's client does.

    Args:
        chunks: The parsed chunks.

    Returns:
        The assistant message.
    """
    text = ""
    calls: dict[int, dict[str, Any]] = {}
    for chunk in chunks:
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            text += delta.get("content") or ""
            for part in delta.get("tool_calls") or []:
                call = calls.setdefault(
                    part.get("index", 0), {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                )
                call["id"] = part.get("id") or call["id"]
                function = part.get("function") or {}
                call["function"]["name"] += function.get("name") or ""
                call["function"]["arguments"] += function.get("arguments") or ""
    message: dict[str, Any] = {"role": "assistant", "content": text or None}
    if calls:
        message["tool_calls"] = [calls[index] for index in sorted(calls)]
    return message


async def _simulated_pi(
    *, binary: str, handlers: ReactOnPi, translator: PiEventTranslator, prompt: str, **_: Any
) -> None:
    """Run a turn the way Pi does, in process.

    Args:
        binary: Ignored.
        handlers: The turn's handlers.
        translator: Receives the events Pi would send.
        prompt: Pi's first user message.
        **_: Allow-list arguments, ignored.
    """
    tools = [{"type": "function", "function": spec} for spec in handlers.tool_specs()]
    messages: list[dict[str, Any]] = [{"role": "system", "content": "pi"}, {"role": "user", "content": prompt}]
    while True:
        translator.feed({"type": "turn_start"})
        chunks = [json.loads(chunk) async for chunk in handlers.complete({"messages": messages, "tools": tools})]
        message = _merge(chunks)
        calls = message.get("tool_calls") or []
        for index, call in enumerate(calls):
            update = {"type": "toolcall_start", "contentIndex": index, "toolName": call["function"]["name"]}
            translator.feed({"type": "message_update", "assistantMessageEvent": update})
            delta = {"type": "toolcall_delta", "contentIndex": index, "delta": call["function"]["arguments"]}
            translator.feed({"type": "message_update", "assistantMessageEvent": delta})
        if not calls:
            if message["content"]:
                update = {"type": "text_delta", "contentIndex": 0, "delta": message["content"]}
                translator.feed({"type": "message_update", "assistantMessageEvent": update})
            content = [{"type": "text", "text": message["content"] or ""}]
            final = {"role": "assistant", "stopReason": "stop", "content": content}
            translator.feed({"type": "message_end", "message": final})
            break
        translator.feed({"type": "message_end", "message": {"role": "assistant", "stopReason": "toolUse"}})
        messages.append(message)
        outcomes = []
        for call in calls:
            outcome = await handlers.call_tool(call["function"]["name"], json.loads(call["function"]["arguments"]))
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": outcome.text})
            outcomes.append(outcome)
        if all(outcome.terminate for outcome in outcomes):
            break
    translator.feed({"type": "agent_settled"})


def _lookup(name: str) -> str:
    """Return a fixed value for any name.

    Args:
        name: The thing to look up.

    Returns:
        A constant string.
    """
    return f"value of {name}"


def _explode(name: str) -> str:
    """Fail every call.

    Args:
        name: Ignored.

    Raises:
        ValueError: Always.
    """
    raise ValueError("no such thing")


def _as_dict(name: str) -> dict[str, str]:
    """Return a structured result.

    Args:
        name: Echoed back.

    Returns:
        A dict result.
    """
    return {"name": name}


def _program(*, guard: Any = None, max_iters: int = 5) -> AgentProgram:
    """Build a small conversation loop with the setup agent's reply field.

    Args:
        guard: The submit guard, or ``None``.
        max_iters: The loop budget.

    Returns:
        The program.
    """
    signature = dspy.Signature(f"user_message: str -> {_REPLY}: str", "Be helpful.")
    return AgentProgram(signature, tools=[_lookup, _explode, _as_dict], max_iters=max_iters, submit_guard=guard)


def _handlers(
    lm: dspy.BaseLM, *, program: AgentProgram | None = None, inbox: SteerInbox | None = None
) -> tuple[ReactOnPi, list[dict]]:
    """Build the turn's handlers around a program.

    Args:
        lm: The turn's LM.
        program: The program; a fresh one when omitted.
        inbox: The steer inbox, or ``None``.

    Returns:
        The handlers and the list that receives the stream events.
    """
    events: list[dict] = []
    translator = PiEventTranslator(events.append, reply_tool="submit", reply_arg=_REPLY, pending_skip=frozenset())
    handlers = ReactOnPi(
        program or _program(), {"user_message": "hi"}, lm, translator=translator, output_field=_REPLY, steer_inbox=inbox
    )
    return handlers, events


async def _run(lm: dspy.BaseLM, **kwargs: Any) -> tuple[ReactOnPi, list[dict]]:
    """Run one simulated Pi turn.

    Args:
        lm: The turn's LM.
        **kwargs: As for :func:`_handlers`.

    Returns:
        The handlers after the turn and the stream events.
    """
    handlers, events = _handlers(lm, **kwargs)
    await _simulated_pi(binary="pi", handlers=handlers, translator=handlers._translator, prompt="hi")
    return handlers, events


async def test_prompt_is_the_programs_own_with_pis_tail_and_tools() -> None:
    """Pi's system and first user message give way to the program's prompt; its later messages follow."""
    lm = _ScriptedLM([("_lookup", {"name": "a"}), ("submit", {_REPLY: "done"})])
    handlers, _ = await _run(lm)

    assert handlers.reply == "done"
    first, second = lm.requests
    assert [message["role"] for message in first["messages"]] == ["system", "user"]
    assert "Be helpful." in first["messages"][0]["content"]
    assert first["messages"][1]["content"] == "[[ ## user_message ## ]]\nhi"
    assert second["messages"][:2] == first["messages"]
    assert [message["role"] for message in second["messages"][2:]] == ["assistant", "tool"]
    assert second["messages"][3]["content"] == "value of a"
    assert {tool["function"]["name"] for tool in first["tools"]} == {"_lookup", "_explode", "_as_dict", "submit"}
    assert first["parallel_tool_calls"] is False
    assert "tool_choice" not in first


async def test_tool_results_errors_and_unknown_tools() -> None:
    """A dict result travels as JSON; a failing or unknown tool is an error result the model reads."""
    handlers, _ = _handlers(_ScriptedLM([]))

    structured = await handlers.call_tool("_as_dict", {"name": "x"})
    failed = await handlers.call_tool("_explode", {"name": "x"})
    unknown = await handlers.call_tool("nope", {})

    assert json.loads(structured.text) == {"name": "x"}
    assert not structured.is_error
    assert failed.is_error
    assert failed.text.startswith("Execution error in _explode:")
    assert "no such thing" in failed.text
    assert unknown == ToolOutcome("Unknown tool: nope", is_error=True)


async def test_refused_submit_is_taken_back_and_the_turn_goes_on() -> None:
    """The guard's refusal is the submit's result; the streamed reply is reset; a later submit ends the turn."""
    refusals = iter(["Fill the run name first.", None])
    lm = _ScriptedLM([("submit", {_REPLY: "early"}), ("submit", {_REPLY: "ready"})])
    handlers, events = await _run(lm, program=_program(guard=lambda outputs: next(refusals)))

    assert handlers.reply == "ready"
    assert lm.requests[1]["messages"][-1] == {
        "role": "tool",
        "tool_call_id": lm.requests[1]["messages"][-2]["tool_calls"][0]["id"],
        "content": "Fill the run name first.",
    }
    kinds = [event["event"] for event in events if event["event"].startswith("message_")]
    assert kinds == ["message_patch", "message_reset", "message_patch"]


async def test_submit_beside_other_calls_is_refused() -> None:
    """A submit in the same step as another tool is not delivered, so the model reads the results first."""
    lm = _ScriptedLM([[("_lookup", {"name": "a"}), ("submit", {_REPLY: "guess"})], ("submit", {_REPLY: "real"})])
    handlers, _ = await _run(lm)

    assert handlers.reply == "real"
    tool_results = [message["content"] for message in lm.requests[1]["messages"] if message["role"] == "tool"]
    assert tool_results == ["value of a", pi_react._SUBMIT_NOT_ALONE]


async def test_budget_forces_submit_with_the_guard_off_then_stops() -> None:
    """The call after the budget forces ``submit``, which the guard cannot refuse; later calls get a stop."""
    program = _program(guard=lambda outputs: "never", max_iters=2)
    lm = _ScriptedLM([("_lookup", {"name": "a"}), ("_lookup", {"name": "b"}), ("submit", {_REPLY: "forced"})])
    handlers, _ = await _run(lm, program=program)

    assert handlers.reply == "forced"
    assert len(lm.requests) == 3
    forced = lm.requests[2]
    assert forced["tool_choice"] == {"type": "function", "function": {"name": "submit"}}
    assert "parallel_tool_calls" not in forced
    assert forced["reasoning_effort"] is None
    assert program._forcing is False
    after = [json.loads(chunk) async for chunk in handlers.complete({"messages": []})]
    assert after == [json.loads(pi_react.stop_chunk())]
    assert len(lm.requests) == 3


async def test_text_reply_ends_the_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plain-text final message is the reply; no forced submit follows."""
    monkeypatch.setattr(pi_react, "run_pi_turn", _simulated_pi)
    lm = _ScriptedLM(["Just text."])
    events: list[dict] = []
    reply = await pi_react.run_react_turn_on_pi(
        _program(),
        {"user_message": "hi"},
        lm,
        emit=events.append,
        output_field=_REPLY,
        input_field="user_message",
        binary="pi",
        pending_skip=frozenset(),
    )

    assert reply == "Just text."
    assert len(lm.requests) == 1
    assert [event["event"] for event in events] == ["tool_pending_clear", "message_patch"]


class _SteerStore:
    """An in-memory steer store holding one waiting message."""

    def __init__(self) -> None:
        """Queue one steer."""
        self.waiting = [("s1", "Use the cheaper model.")]
        self.delivered: list[tuple[str, str]] = []

    def take(self, owner: str, steer_key: str, *, max_chars: int | None = None) -> list[tuple[str, str]]:
        """Hand out the waiting steers.

        Args:
            owner: Ignored.
            steer_key: Ignored.
            max_chars: Ignored.

        Returns:
            The steers.
        """
        taken, self.waiting = self.waiting, []
        return taken

    def mark_delivered(self, owner: str, steer_key: str, taken: list[tuple[str, str]]) -> None:
        """Record delivery.

        Args:
            owner: Ignored.
            steer_key: Ignored.
            taken: The delivered steers.
        """
        self.delivered += taken

    def return_unread(self, owner: str, steer_key: str, taken: list[tuple[str, str]]) -> None:
        """Not expected.

        Args:
            owner: Ignored.
            steer_key: Ignored.
            taken: Ignored.
        """
        raise AssertionError("delivery should succeed")


async def test_steer_joins_the_prompt_from_the_second_call() -> None:
    """A waiting steer is not read before the first step; it joins as a user message before the next call."""
    store = _SteerStore()
    events: list[dict] = []
    inbox = SteerInbox(owner="u", steer_key="k", input_field="user_message", emit=events.append, store=cast(Any, store))
    lm = _ScriptedLM([("_lookup", {"name": "a"}), ("submit", {_REPLY: "ok"}), ("submit", {_REPLY: "unused"})])
    handlers, _ = await _run(lm, inbox=inbox)

    assert handlers.reply == "ok"
    assert len(lm.requests[0]["messages"]) == 2
    second = lm.requests[1]["messages"]
    assert [message["role"] for message in second[2:]] == ["assistant", "tool", "user"]
    assert second[-1]["content"] == "[[ ## user_message ## ]]\nUse the cheaper model."
    assert store.delivered == [("s1", "Use the cheaper model.")]
    assert events == [{"event": STEER_APPLIED_EVENT, "data": {"ids": ["s1"], "text": "Use the cheaper model."}}]


def _stream_chunks(tool: str, args: dict[str, Any]) -> list[ModelResponseStream]:
    """Build a provider stream that calls one tool and reports usage.

    Args:
        tool: The tool name.
        args: Its arguments.

    Returns:
        The stream chunks.
    """
    call = {"index": 0, "id": "c1", "type": "function", "function": {"name": tool, "arguments": json.dumps(args)}}
    return [
        ModelResponseStream(
            id="r1", model="fake", choices=[StreamingChoices(index=0, delta=Delta(role="assistant", tool_calls=[call]))]
        ),
        ModelResponseStream(id="r1", model="fake", choices=[StreamingChoices(index=0, delta=Delta(), finish_reason="tool_calls")]),
        ModelResponseStream(
            id="r1", model="fake", choices=[], usage=Usage(prompt_tokens=11, completion_tokens=7, total_tokens=18)
        ),
    ]  # fmt: skip


async def test_relay_streams_through_the_metered_lm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each relayed call goes through the turn's MeteredLM: its chunks reach Pi and its usage is counted."""
    requests: list[dict[str, Any]] = []
    script = [_stream_chunks("_lookup", {"name": "a"}), _stream_chunks("submit", {_REPLY: "metered"})]

    async def fake_acompletion(**request: Any) -> AsyncIterator[ModelResponseStream]:
        """Stream the next scripted response.

        Args:
            **request: The provider request.

        Returns:
            An async chunk iterator.
        """
        requests.append(request)
        chunks = script.pop(0)

        async def stream() -> AsyncIterator[ModelResponseStream]:
            """Yield the chunks.

            Yields:
                Stream chunks.
            """
            for chunk in chunks:
                yield chunk

        return stream()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    lm = MeteredLM(model="openai/gpt-4o-mini", cache=False)
    with dspy.context(track_usage=True):
        handlers, events = await _run(lm)

    assert handlers.reply == "metered"
    assert len(requests) == 2
    assert all(request["stream"] for request in requests)
    assert requests[1]["messages"][-1]["content"] == "value of a"
    assert lm.usage_totals.calls == 2
    assert lm.usage_totals.input_tokens == 22
    assert lm.usage_totals.output_tokens == 14
    assert [event for event in events if event["event"] == "message_patch"] == [
        {"event": "message_patch", "data": {"chunk": "metered"}}
    ]


async def test_lm_failure_reaches_the_bridge_as_itself() -> None:
    """A provider or budget error from the LM propagates out of the relay unchanged."""

    class _Broken(_ScriptedLM):
        """An LM whose calls fail."""

        def forward(self, prompt=None, messages=None, **kwargs):
            """Fail.

            Args:
                prompt: Unused.
                messages: Unused.
                **kwargs: Unused.

            Raises:
                PermissionError: Always.
            """
            raise PermissionError("budget reached")

    handlers, _ = _handlers(_Broken([]))
    with pytest.raises(PermissionError, match="budget reached"):
        async for _ in handlers.complete({"messages": [{"role": "user", "content": "hi"}]}):
            pass


# --- The setup agent's tools through the bridge ---------------------------------------------

# A question, so a screen turn's guard lets the reply end the turn.
_SETUP_REPLY = "All set. Shall we go on?"
_READY: dict[str, Any] = {
    "job_name": "My run",
    "task_objective": "Answer support questions",
    "artifact_kind": "repo",
    "repository": "acme/bot",
    "data_mode": "pairs",
    "dataset_ready": True,
    "staged_dataset_id": "ds_1",
}


class _Spec:
    """A minimal MCP tool listing entry."""

    def __init__(self, name: str) -> None:
        """Name the tool.

        Args:
            name: The tool's MCP name.
        """
        self.name = name
        self.description = "test tool"
        self.inputSchema = {"type": "object", "properties": {}}


class _Session:
    """An MCP session that lists the given tools."""

    def __init__(self, names: list[str]) -> None:
        """Remember the names.

        Args:
            names: The listed tool names.
        """
        self.tools = [_Spec(name) for name in names]

    async def list_tools(self) -> _Session:
        """List the tools."""
        return self


def _recording_mcp_tool(session: _Session, spec: _Spec) -> dspy.Tool:
    """Build an MCP tool stand-in that answers with its own name.

    Args:
        session: Ignored.
        spec: The listed tool.

    Returns:
        The tool.
    """

    async def func(**kwargs: Any) -> dict[str, Any]:
        """Answer the call.

        Args:
            **kwargs: The call's arguments.

        Returns:
            A result naming the tool.
        """
        return {"called": spec.name}

    return dspy.Tool(func=func, name=spec.name, desc="test tool", args={}, arg_types={}, arg_desc={})


async def _setup_turn(
    monkeypatch: pytest.MonkeyPatch, state: dict[str, Any], tool: str, args: dict[str, Any]
) -> tuple[str, list[dict], _ScriptedLM]:
    """Run one setup-agent turn on the Pi path that calls ``tool`` and then submits.

    Args:
        monkeypatch: Pytest's monkeypatch fixture.
        state: The wizard snapshot.
        tool: The tool the model calls.
        args: Its arguments.

    Returns:
        The reply, the stream events and the LM.
    """
    names = sorted(screen_tools(tools_for(cast(WizardState, state)), cast(WizardState, state)))

    @asynccontextmanager
    async def session(mcp_url: str, *, auth_header: str | None = None) -> AsyncIterator[_Session]:
        """Yield the fake session.

        Args:
            mcp_url: Ignored.
            auth_header: Ignored.
        """
        yield _Session(names)

    monkeypatch.setattr(settings, "firecrawl_api_key", None)
    monkeypatch.setattr(generalist_module, "_mcp_session", session)
    monkeypatch.setattr(dspy.Tool, "from_mcp_tool", staticmethod(_recording_mcp_tool))
    monkeypatch.setattr(pi_react, "run_pi_turn", _simulated_pi)
    lm = _ScriptedLM([(tool, args), ("submit", {_REPLY: _SETUP_REPLY})])
    events: list[dict] = []
    registry = ApprovalRegistry()
    loop = asyncio.get_running_loop()

    def emit(event: dict) -> None:
        """Record the event and approve any approval request, as the user would.

        Args:
            event: One stream event.
        """
        events.append(event)
        if event["event"] == "pending_approval":
            loop.call_soon_threadsafe(registry.resolve, event["data"]["id"], True)

    reply = await generalist_module._drive_generalist_agent(
        mcp_url="http://unused/mcp/",
        wizard_state=cast(WizardState, state),
        chat_history=[],
        user_message="go",
        trust_mode="yolo",
        registry=registry,
        emit=emit,
        lm=lm,
        reply_language="English",
    )
    return reply, events, lm


def _mcp_roster() -> list[str]:
    """List the MCP tools the setup agent offers on a ready setup.

    Returns:
        The tool names.
    """
    return sorted(screen_tools(tools_for(cast(WizardState, _READY)), cast(WizardState, _READY)))


@pytest.mark.parametrize("tool", _mcp_roster())
async def test_each_mcp_tool_runs_through_the_bridge(monkeypatch: pytest.MonkeyPatch, tool: str) -> None:
    """Every MCP tool the agent sees runs through the bridge with its usual events; a confirmation still asks."""
    reply, events, lm = await _setup_turn(monkeypatch, _READY, tool, {})

    assert reply == _SETUP_REPLY
    assert tool in {spec["function"]["name"] for spec in lm.requests[0]["tools"]}
    starts = [event["data"]["tool"] for event in events if event["event"] == "tool_start"]
    ends = [(event["data"]["tool"], event["data"]["status"]) for event in events if event["event"] == "tool_end"]
    assert starts == [tool]
    assert ends == [(tool, "ok")]
    assert [event["data"] for event in events if event["event"] == "tool_pending"] == [{"index": 0, "tool": tool}]
    # The samples listing adds a language note around the result, as on the DSPy loop.
    assert json.dumps({"called": tool}) in lm.requests[1]["messages"][-1]["content"]


def _nav_roster() -> list[str]:
    """List the screen agent's own navigation tools.

    Returns:
        The tool names.
    """
    state = cast(WizardState, {**_READY, "wizard_screen": "brief"})
    return sorted(tool.name for tool in screen_nav_tools(state, generalist_module._TurnAuthoringFlag()))


@pytest.mark.parametrize("tool", _nav_roster())
async def test_each_screen_tool_runs_through_the_bridge(monkeypatch: pytest.MonkeyPatch, tool: str) -> None:
    """The screen agent's navigation tools run through the bridge and report back as before."""
    state = {**_READY, "wizard_screen": "budget"}
    args = {
        "go_back_to_screen": {"screen_id": "brief", "request": "Change the goal"},
        "ask_user": {"question": "Which model?", "options_json": json.dumps([{"label": "A"}, {"label": "B"}])},
    }.get(tool, {"note": "cheap"})
    reply, events, lm = await _setup_turn(monkeypatch, state, tool, args)

    assert reply == _SETUP_REPLY
    ends = [event["data"] for event in events if event["event"] == "tool_end"]
    assert [end["tool"] for end in ends] == [tool]
    assert lm.requests[1]["messages"][-1]["role"] == "tool"


async def test_plan_tool_runs_through_the_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    """``share_plan`` streams its plan event and no tool row, as on the DSPy loop."""
    reply, events, _ = await _setup_turn(monkeypatch, {}, "share_plan", {"plan": "Fill the name, then go on."})

    assert reply == _SETUP_REPLY
    assert {"event": "plan", "data": {"text": "Fill the name, then go on."}} in events
    assert not [event for event in events if event["event"] == "tool_pending"]


async def test_setup_turn_without_an_answer_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Pi turn that ends with no reply raises, so the chat shows an error and not a blank message."""

    @asynccontextmanager
    async def session(mcp_url: str, *, auth_header: str | None = None) -> AsyncIterator[_Session]:
        """Yield an empty fake session.

        Args:
            mcp_url: Ignored.
            auth_header: Ignored.
        """
        yield _Session([])

    monkeypatch.setattr(generalist_module, "_mcp_session", session)
    monkeypatch.setattr(pi_react, "run_pi_turn", _simulated_pi)
    with pytest.raises(AgentStoppedError, match="stopped before it answered"):
        await generalist_module._drive_generalist_agent(
            mcp_url="http://unused/mcp/",
            wizard_state=WizardState(),
            chat_history=[],
            user_message="go",
            trust_mode="yolo",
            registry=ApprovalRegistry(),
            emit=lambda event: None,
            lm=_ScriptedLM([""]),
            reply_language="English",
        )


class _TextLM(dspy.BaseLM):
    """An LM without native tool calls, like a subscription CLI route, that answers with fixed texts."""

    def __init__(self, replies: list[str]) -> None:
        """Store the replies.

        Args:
            replies: One raw assistant text per expected LM call.
        """
        super().__init__(model="scripted")
        self._replies = list(replies)
        self.requests: list[dict[str, Any]] = []

    @property
    def supports_reasoning(self) -> bool:
        """Report native reasoning support, so ``tool_calls`` is the only output field."""
        return True

    def forward(self, prompt=None, messages=None, **kwargs):
        """Record the request and return the next text.

        Args:
            prompt: Unused legacy prompt.
            messages: The rendered chat messages.
            **kwargs: Provider kwargs.

        Returns:
            An OpenAI-shaped text response.
        """
        self.requests.append({"messages": messages, **kwargs})
        message = {"role": "assistant", "content": self._replies.pop(0)}
        return litellm.ModelResponse(
            model="scripted",
            choices=[{"index": 0, "finish_reason": "stop", "message": message}],
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )


def _text_calls(*calls: tuple[str, dict[str, Any]]) -> str:
    """Write tool calls in the SKY-245 text protocol, with the header.

    Args:
        *calls: ``(tool, args)`` pairs.

    Returns:
        The assistant text.
    """
    body = json.dumps({"tool_calls": [{"name": name, "args": args} for name, args in calls]})
    return f"[[ ## tool_calls ## ]]\n{body}\n\n[[ ## completed ## ]]"


async def test_cli_reply_becomes_one_native_tool_call_chunk() -> None:
    """A text-protocol reply reaches Pi as one chunk with native tool calls, and no tools go to the model."""
    handlers, _ = _handlers(_TextLM([_text_calls(("_lookup", {"name": "a"}))]))

    chunks = [json.loads(chunk) async for chunk in handlers.complete({"messages": [], "tools": []})]

    assert len(chunks) == 1
    message = _merge(chunks)
    assert message["content"] is None
    (call,) = message["tool_calls"]
    assert call["function"]["name"] == "_lookup"
    assert json.loads(call["function"]["arguments"]) == {"name": "a"}
    request = cast(_TextLM, handlers._lm).requests[0]
    assert "tools" not in request
    assert "tool_calls" in request["messages"][-1]["content"]


async def test_cli_turn_runs_tools_and_answers_through_header_less_replies() -> None:
    """A CLI turn on Pi runs a tool and submits, also when the model drops the field header."""
    lm = _TextLM(
        [
            '{"tool_calls": [{"name": "_lookup", "args": {"name": "a"}}]}\n\n[[ ## completed ## ]]',
            _text_calls(("submit", {_REPLY: "Done."})),
        ]
    )

    handlers, _ = await _run(lm)

    assert handlers.reply == "Done."
    assert len(lm.requests) == 2
    assert "value of a" in json.dumps(lm.requests[1]["messages"])


async def test_bad_cli_reply_raises_a_clear_error_after_the_retries() -> None:
    """Prose with no tool call is resampled with a new rollout id, then the turn fails with a clear error."""
    attempts = pi_react.PARSE_RETRY_ATTEMPTS + 1
    lm = _TextLM(["Sure, here is my answer.\n\n[[ ## completed ## ]]"] * attempts)
    handlers, _ = _handlers(lm)

    with pytest.raises(AgentReplyFormatError, match="tool-call text protocol"):
        [chunk async for chunk in handlers.complete({"messages": [], "tools": []})]

    assert [request.get("rollout_id") for request in lm.requests] == [None, *range(1, attempts)]


async def test_native_model_does_not_take_the_text_path() -> None:
    """A model with native tool calls gets the tools and its reply is relayed, never parsed as text."""
    lm = _ScriptedLM([("_lookup", {"name": "a"})])
    handlers, _ = _handlers(lm)

    chunks = [json.loads(chunk) async for chunk in handlers.complete({"messages": [], "tools": []})]

    assert _merge(chunks)["tool_calls"][0]["function"]["name"] == "_lookup"
    assert lm.requests[0]["tools"]
    assert "[[ ## tool_calls ## ]]" not in json.dumps(lm.requests[0]["messages"][-1])

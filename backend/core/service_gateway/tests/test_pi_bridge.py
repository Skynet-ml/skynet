"""Tests for the shared Pi harness bridge: allow-list, endpoints, event translation and process control."""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest

from core.service_gateway.agents import pi_bridge
from core.service_gateway.agents.pi_bridge import (
    ALLOWED_EXTENSIONS,
    TOOL_BRIDGE_EXTENSION,
    PiBridgeError,
    PiEventTranslator,
    ToolOutcome,
    _BridgeApp,
    _pi_env,
    _write_agent_dir,
    pi_command,
    resolve_allowed,
    run_pi_turn,
)

_TOKEN = "turn-token"


class _BudgetStopError(Exception):
    """Stands in for a gateway budget error the relay must re-raise as itself."""


class _Handlers:
    """Scripted turn handlers that record what Pi asked for."""

    def __init__(self, *, fail: Exception | None = None) -> None:
        """Set up the script.

        Args:
            fail: An error the model relay raises, or ``None``.
        """
        self.fail = fail
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.bodies: list[dict[str, Any]] = []

    def tool_specs(self) -> list[dict[str, Any]]:
        """Return one tool and the reply tool."""
        schema = {"type": "object", "properties": {"q": {"type": "string"}}}
        return [
            {"name": "get_x", "description": "Get x.", "parameters": schema},
            {"name": "submit", "description": "Submit.", "parameters": schema},
        ]

    async def call_tool(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        """Record the call and answer it.

        Args:
            name: The tool name.
            args: The call's arguments.

        Returns:
            An error for ``broken``, a terminal result for ``submit``, else text.
        """
        self.calls.append((name, args))
        if name == "broken":
            return ToolOutcome("Execution error in broken: boom", is_error=True)
        return ToolOutcome(f"x={args.get('q')}", terminate=name == "submit")

    async def complete(self, body: dict[str, Any]) -> AsyncIterator[str]:
        """Answer a model call with one text chunk, or fail.

        Args:
            body: Pi's request.

        Yields:
            Stream chunks as JSON strings.

        Raises:
            Exception: The scripted failure.
        """
        self.bodies.append(body)
        if self.fail is not None:
            raise self.fail
        yield json.dumps({"choices": [{"index": 0, "delta": {"content": "hi"}}]})


def _client(app: _BridgeApp, token: str | None = _TOKEN) -> httpx.AsyncClient:
    """Return an HTTP client bound to the bridge endpoints.

    Args:
        app: The bridge.
        token: Bearer token to send, or ``None`` for none.

    Returns:
        The client.
    """
    headers = {"authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url="http://bridge", headers=headers)


def test_command_loads_only_allow_listed_add_ons() -> None:
    """Pi starts with every discovery and network feature off and only the tool bridge loaded."""
    argv = pi_command("pi")
    for flag in ("--no-session", "-nbt", "-ne", "-ns", "-np", "-nc", "--no-themes", "--offline"):
        assert flag in argv
    assert argv[argv.index("--mode") + 1] == "rpc"
    loaded = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-e"]
    assert loaded == [str(ALLOWED_EXTENSIONS[TOOL_BRIDGE_EXTENSION])]
    assert "--skill" not in argv
    assert Path(loaded[0]).is_file()


def test_unlisted_extension_or_skill_is_refused() -> None:
    """A name outside the allow-list never reaches Pi's command line."""
    with pytest.raises(PiBridgeError, match="not on Skynet's allow-list"):
        pi_command("pi", extensions=["evil"])
    with pytest.raises(PiBridgeError, match="skill 'web'"):
        pi_command("pi", skills=["web"])
    assert resolve_allowed([], {}, "skill") == []


def test_pi_environment_carries_no_backend_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pi sees the bridge URL and token, never the API process's keys."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    agent_dir = _write_agent_dir(tmp_path, "http://127.0.0.1:1/v1")
    env = _pi_env(tmp_path, agent_dir, "http://127.0.0.1:1", _TOKEN)
    assert "OPENAI_API_KEY" not in env
    assert env["HOME"] == str(tmp_path)
    assert env["PI_OFFLINE"] == "1"
    assert env["PI_TELEMETRY"] == "0"
    models = json.loads((agent_dir / "models.json").read_text())
    provider = models["providers"][pi_bridge.PI_PROVIDER]
    assert provider["baseUrl"] == "http://127.0.0.1:1/v1"
    # The key file names the variable; the token itself only lives in Pi's environment.
    assert provider["apiKey"] == "$SKYNET_BRIDGE_TOKEN"
    assert _TOKEN not in (agent_dir / "models.json").read_text()
    pi_settings = json.loads((agent_dir / "settings.json").read_text())
    assert pi_settings["retry"]["enabled"] is False
    assert pi_settings["compaction"]["enabled"] is False


async def test_endpoints_refuse_a_wrong_token() -> None:
    """Every endpoint answers 401 without the turn's token."""
    app = _BridgeApp(_Handlers(), _TOKEN)
    async with _client(app, token="wrong") as client:
        assert (await client.get("/tools")).status_code == 401
        assert (await client.post("/call", json={"tool": "get_x", "args": {}})).status_code == 401
        assert (await client.post("/v1/chat/completions", json={})).status_code == 401
    async with _client(app, token=None) as client:
        assert (await client.get("/tools")).status_code == 401


async def test_tool_calls_route_to_the_handlers() -> None:
    """The extension's calls reach the backend handler and return its text, error or terminate flag."""
    handlers = _Handlers()
    app = _BridgeApp(handlers, _TOKEN)
    async with _client(app) as client:
        specs = (await client.get("/tools")).json()
        assert [spec["name"] for spec in specs] == ["get_x", "submit"]
        ok = json.loads((await client.post("/call", json={"tool": "get_x", "args": {"q": "a"}})).text)
        failed = json.loads((await client.post("/call", json={"tool": "broken", "args": {}})).text)
        done = json.loads((await client.post("/call", json={"tool": "submit", "args": {"q": "b"}})).text)
    assert ok == {"ok": True, "text": "x=a", "terminate": False}
    assert failed == {"ok": False, "error": "Execution error in broken: boom"}
    assert done["terminate"] is True
    assert handlers.calls == [("get_x", {"q": "a"}), ("broken", {}), ("submit", {"q": "b"})]
    assert app.failure is None


async def test_slow_tool_call_sends_keepalive_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """A call that waits (an approval) streams whitespace first; the reply still parses as JSON."""
    monkeypatch.setattr(pi_bridge, "_KEEPALIVE_SECONDS", 0.01)

    class _Slow(_Handlers):
        """Handlers whose tool waits a little."""

        async def call_tool(self, name: str, args: dict[str, Any]) -> ToolOutcome:
            """Wait, then answer.

            Args:
                name: The tool name.
                args: The call's arguments.

            Returns:
                The text result.
            """
            await pi_bridge.asyncio.sleep(0.1)
            return ToolOutcome("late")

    async with _client(_BridgeApp(_Slow(), _TOKEN)) as client:
        body = (await client.post("/call", json={"tool": "get_x", "args": {}})).text
    assert body.startswith(" ")
    assert json.loads(body) == {"ok": True, "text": "late", "terminate": False}


async def test_model_relay_streams_sse_and_keeps_a_failure() -> None:
    """The relay streams the handler's chunks as SSE; a failure is kept for the turn to raise."""
    handlers = _Handlers()
    app = _BridgeApp(handlers, _TOKEN)
    async with _client(app) as client:
        response = await client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "q"}]})
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.splitlines()[0] == 'data: {"choices": [{"index": 0, "delta": {"content": "hi"}}]}'
    assert response.text.rstrip().endswith("data: [DONE]")
    assert handlers.bodies[0]["messages"][0]["content"] == "q"

    stop = _BudgetStopError("budget")
    failing = _BridgeApp(_Handlers(fail=stop), _TOKEN)
    async with _client(failing) as client:
        response = await client.post("/v1/chat/completions", json={})
    assert "[DONE]" not in response.text
    assert failing.failure is stop
    assert failing.failed.is_set()


def _feed(translator: PiEventTranslator, *updates: dict[str, Any]) -> None:
    """Feed assistant-message updates to the translator.

    Args:
        translator: The translator.
        *updates: ``assistantMessageEvent`` payloads.
    """
    for update in updates:
        translator.feed({"type": "message_update", "assistantMessageEvent": update})


def _translator(events: list[dict]) -> PiEventTranslator:
    """Return a translator for the setup agent's reply tool.

    Args:
        events: Receives the emitted events.

    Returns:
        The translator.
    """
    return PiEventTranslator(
        events.append, reply_tool="submit", reply_arg="assistant_message", pending_skip=frozenset({"submit", "plan"})
    )


def test_translator_maps_thinking_tools_and_submit_reply() -> None:
    """Thinking, pending tool rows and the submit's reply argument become the chat's events."""
    events: list[dict] = []
    translator = _translator(events)
    translator.feed({"type": "turn_start"})
    _feed(
        translator,
        {"type": "thinking_delta", "contentIndex": 0, "delta": "hmm"},
        {"type": "toolcall_start", "contentIndex": 1, "id": "a", "toolName": "list_models"},
        {"type": "toolcall_start", "contentIndex": 2, "id": "b", "toolName": "plan"},
    )
    translator.feed({"type": "message_end", "message": {"role": "assistant", "stopReason": "toolUse", "content": []}})
    translator.feed({"type": "turn_start"})
    _feed(
        translator,
        {"type": "toolcall_start", "contentIndex": 0, "id": "c", "toolName": "submit"},
        {"type": "toolcall_delta", "contentIndex": 0, "delta": '{"assistant_message": "שלו'},
        {"type": "toolcall_delta", "contentIndex": 0, "delta": 'ם"}'},
    )
    translator.feed({"type": "agent_settled"})
    assert events == [
        {"event": "tool_pending_clear", "data": {}},
        {"event": "reasoning_patch", "data": {"chunk": "hmm"}},
        {"event": "tool_pending", "data": {"index": 0, "tool": "list_models"}},
        {"event": "tool_pending_clear", "data": {}},
        {"event": "message_patch", "data": {"chunk": "שלו"}},
        {"event": "message_patch", "data": {"chunk": "ם"}},
    ]
    assert translator.settled


def test_translator_takes_back_text_written_beside_a_tool_call() -> None:
    """Text that turns out to precede a tool call is reset; a final text answer is kept as the reply."""
    events: list[dict] = []
    translator = _translator(events)
    _feed(translator, {"type": "text_delta", "contentIndex": 0, "delta": "Let me check."})
    translator.feed({"type": "message_end", "message": {"role": "assistant", "stopReason": "toolUse", "content": []}})
    _feed(translator, {"type": "text_delta", "contentIndex": 0, "delta": "Here it is."})
    final = {"role": "assistant", "stopReason": "stop", "content": [{"type": "text", "text": "Here it is."}]}
    translator.feed({"type": "message_end", "message": final})
    assert [event["event"] for event in events] == ["message_patch", "message_reset", "message_patch"]
    assert translator.text_reply == "Here it is."
    assert translator.error is None


def test_translator_resets_text_when_the_submit_starts_and_on_retract() -> None:
    """Text before a submit in the same message is not the reply; a refused submit is taken back."""
    events: list[dict] = []
    translator = _translator(events)
    _feed(
        translator,
        {"type": "text_delta", "contentIndex": 0, "delta": "Draft"},
        {"type": "toolcall_start", "contentIndex": 1, "id": "s", "toolName": "submit"},
        {"type": "toolcall_delta", "contentIndex": 1, "delta": '{"assistant_message": "Final'},
        {"type": "text_delta", "contentIndex": 0, "delta": "ignored"},
    )
    translator.retract()
    translator.retract()
    assert [event["event"] for event in events] == ["message_patch", "message_reset", "message_patch", "message_reset"]


def test_translator_records_a_model_error() -> None:
    """A message that ends in error marks the turn failed."""
    translator = _translator([])
    failed = {"role": "assistant", "stopReason": "error", "errorMessage": "502 from relay", "content": []}
    translator.feed({"type": "message_end", "message": failed})
    translator.feed({"type": "message_end", "message": {"role": "user", "content": []}})
    assert translator.error == "502 from relay"


_FAKE_PI = r"""
import json, os, sys, urllib.request

url, token = os.environ["SKYNET_BRIDGE_URL"], os.environ["SKYNET_BRIDGE_TOKEN"]
headers = {"authorization": "Bearer " + token, "content-type": "application/json"}


def call(path, body=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url + path, data=data, headers=headers, method="GET" if data is None else "POST")
    try:
        return urllib.request.urlopen(request).read().decode()
    except Exception as error:
        return str(error)


def out(record):
    sys.stdout.write(json.dumps(record) + "\n")
    sys.stdout.flush()


with open(__RECORD__, "w") as handle:
    json.dump({"argv": sys.argv[1:], "env": dict(os.environ)}, handle)
command = json.loads(sys.stdin.readline())
out({"type": "response", "command": "prompt", "success": __PROMPT_OK__, "error": "no model"})
specs = json.loads(call("/tools"))
out({"type": "turn_start"})
call("/v1/chat/completions", {"messages": [{"role": "user", "content": command["message"]}]})
call("/call", {"tool": specs[0]["name"], "args": {"q": "a"}})
if __SETTLE__:
    out({"type": "agent_settled"})
sys.stdin.read()
"""


def _fake_pi(tmp_path: Path, *, prompt_ok: bool = True, settle: bool = True) -> tuple[str, Path]:
    """Write an executable stand-in for ``pi`` that speaks the RPC protocol.

    Args:
        tmp_path: Directory for the script and its record.
        prompt_ok: Whether it accepts the prompt command.
        settle: Whether it emits ``agent_settled``; else it exits early.

    Returns:
        The executable path and the file it records its argv and env in.
    """
    record = tmp_path / "record.json"
    body = (
        _FAKE_PI.replace("__RECORD__", repr(str(record)))
        .replace("__PROMPT_OK__", repr(prompt_ok))
        .replace("__SETTLE__", repr(settle))
    )
    if not settle:
        body = body.replace("sys.stdin.read()", "sys.exit(3)")
    script = tmp_path / "pi"
    script.write_text(f"#!{sys.executable}\n{body}")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script), record


async def test_run_turn_drives_pi_and_stops_it(tmp_path: Path) -> None:
    """A turn starts Pi with the allow-listed command, serves its calls and ends when Pi settles."""
    binary, record = _fake_pi(tmp_path)
    handlers = _Handlers()
    events: list[dict] = []
    await run_pi_turn(binary=binary, handlers=handlers, translator=_translator(events), prompt="hello")
    seen = json.loads(record.read_text())
    assert seen["argv"] == pi_command(binary)[1:]
    assert set(seen["env"]) <= {
        "PATH", "HOME", "PI_CODING_AGENT_DIR", "PI_OFFLINE", "PI_SKIP_VERSION_CHECK", "PI_TELEMETRY",
        "SKYNET_BRIDGE_URL", "SKYNET_BRIDGE_TOKEN", "__CF_USER_TEXT_ENCODING", "LC_CTYPE",
    }  # fmt: skip
    assert seen["env"]["SKYNET_BRIDGE_URL"].startswith("http://127.0.0.1:")
    assert handlers.bodies[0]["messages"] == [{"role": "user", "content": "hello"}]
    assert handlers.calls == [("get_x", {"q": "a"})]
    assert events == [{"event": "tool_pending_clear", "data": {}}]
    # The private agent directory is removed with the turn.
    assert not await anyio.Path(seen["env"]["HOME"]).exists()


async def test_run_turn_re_raises_a_relay_failure_as_itself(tmp_path: Path) -> None:
    """A budget stop in the model relay ends the turn with that same exception."""
    binary, _ = _fake_pi(tmp_path)
    stop = _BudgetStopError("budget reached")
    with pytest.raises(_BudgetStopError) as caught:
        await run_pi_turn(binary=binary, handlers=_Handlers(fail=stop), translator=_translator([]), prompt="q")
    assert caught.value is stop


async def test_run_turn_reports_a_rejected_prompt_and_an_early_exit(tmp_path: Path) -> None:
    """Pi refusing the prompt, or exiting before it settles, fails the turn with a bridge error."""
    binary, _ = _fake_pi(tmp_path, prompt_ok=False)
    with pytest.raises(PiBridgeError, match="rejected prompt: no model"):
        await run_pi_turn(binary=binary, handlers=_Handlers(), translator=_translator([]), prompt="q")
    other = tmp_path / "early"
    other.mkdir()
    binary, _ = _fake_pi(other, settle=False)
    with pytest.raises(PiBridgeError, match="exited before the turn finished"):
        await run_pi_turn(binary=binary, handlers=_Handlers(), translator=_translator([]), prompt="q")


def test_pi_available_checks_the_path(tmp_path: Path) -> None:
    """The fallback check finds an executable by path and reports a missing one."""
    binary, _ = _fake_pi(tmp_path)
    assert pi_bridge.pi_available(binary)
    assert not pi_bridge.pi_available(str(tmp_path / "missing-pi"))


_REAL_PI = os.environ.get("SKYNET_PI_BINARY") or shutil.which("pi")


@pytest.mark.skipif(not _REAL_PI, reason="needs the pinned pi binary (set SKYNET_PI_BINARY)")
async def test_real_pi_loads_the_tool_bridge_and_calls_a_tool() -> None:
    """The pinned Pi loads only the tool-bridge extension and runs a tool call through it."""
    calls = iter(
        [
            {"tool_calls": [{"index": 0, "id": "c1", "type": "function", "function": {"name": "get_x", "arguments": '{"q": "a"}'}}]},
            {"content": "done"},
        ]
    )  # fmt: skip

    class _Scripted(_Handlers):
        """Answers one tool call, then a text reply."""

        async def complete(self, body: dict[str, Any]) -> AsyncIterator[str]:
            """Stream the next scripted message.

            Args:
                body: Pi's request.

            Yields:
                Stream chunks as JSON strings.
            """
            self.bodies.append(body)
            delta = next(calls)
            finish = "tool_calls" if "tool_calls" in delta else "stop"
            yield json.dumps({"id": "r", "object": "chat.completion.chunk", "model": "m", "choices": [{"index": 0, "delta": {"role": "assistant", **delta}, "finish_reason": finish}]})  # fmt: skip

    handlers = _Scripted()
    events: list[dict] = []
    translator = _translator(events)
    await run_pi_turn(binary=str(_REAL_PI), handlers=handlers, translator=translator, prompt="go")
    assert handlers.calls == [("get_x", {"q": "a"})]
    assert translator.text_reply == "done"
    tools = {tool["function"]["name"] for tool in handlers.bodies[0]["tools"]}
    assert tools == {"get_x", "submit"}
    assert handlers.bodies[1]["messages"][-1] == {"role": "tool", "content": "x=a", "tool_call_id": "c1"}

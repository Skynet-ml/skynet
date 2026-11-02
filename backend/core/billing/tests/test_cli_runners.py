"""Tests for the claude and codex subscription runners, run against fake CLI scripts."""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from core.billing import cli_runners
from core.billing.cli_runners import CliRunError, SubscriptionRateLimitedError, parse_reset_at
from core.config import Settings, settings

# Sanitized from real CLI runs (claude 2.1.306, codex-cli 0.163.0).
CLAUDE_SUCCESS = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "duration_ms": 1351,
    "duration_api_ms": 1326,
    "num_turns": 1,
    "result": "pong",
    "stop_reason": "end_turn",
    "total_cost_usd": 0.00046369000000000005,
    "terminal_reason": "completed",
    "usage": {
        "input_tokens": 2,
        "cache_creation_input_tokens": 2275,
        "cache_read_input_tokens": 649,
        "output_tokens": 4,
    },
}
# The notifications of one ``codex app-server`` turn (codex-cli 0.163.0), trimmed.
CODEX_TURN = [
    {"method": "turn/started", "params": {"threadId": "thr", "turn": {"id": "t1"}}},
    {"method": "item/started", "params": {"item": {"id": "msg_1", "type": "agentMessage", "phase": "final_answer"}}},
    {"method": "item/agentMessage/delta", "params": {"itemId": "msg_1", "delta": "po"}},
    {"method": "item/agentMessage/delta", "params": {"itemId": "msg_1", "delta": "ng"}},
    {"method": "item/completed", "params": {"item": {"id": "msg_1", "type": "agentMessage", "text": "pong"}}},
    {
        "method": "thread/tokenUsage/updated",
        "params": {
            "tokenUsage": {
                "last": {"inputTokens": 29250, "cachedInputTokens": 13184, "outputTokens": 43},
                "total": {"inputTokens": 29250, "cachedInputTokens": 13184, "outputTokens": 43},
            }
        },
    },
    {"method": "turn/completed", "params": {"turn": {"id": "t1", "status": "completed", "error": None}}},
]
API_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY")


def fake_cli(directory: Path, name: str, behavior: str, *, read_stdin: bool = True) -> Path:
    """Write an executable fake CLI that logs each call, then runs ``behavior``.

    The script appends ``{argv, env, stdin, cwd}`` to ``<directory>/<name>.log``
    as one JSON line. ``behavior`` is Python that may read ``argv`` and ``stdin``.

    Args:
        directory: Where the script and its log go.
        name: The script name.
        behavior: Python statements that write the CLI's output and exit.
        read_stdin: Read stdin to its end before ``behavior``; the codex app
            server's stdin stays open, so its fakes read it line by line.

    Returns:
        The script path.
    """
    log = directory / f"{name}.log"
    script = directory / name
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, time\n"
        "argv = sys.argv[1:]\n"
        f"stdin = sys.stdin.read() if {read_stdin!r} else ''\n"
        f"with open({str(log)!r}, 'a') as handle:\n"
        "    handle.write(json.dumps({'argv': argv, 'env': dict(os.environ), 'stdin': stdin, 'cwd': os.getcwd()}) + '\\n')\n"
        f"{behavior}\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def fake_app_server(directory: Path, turn: str, *, setup: str = "") -> Path:
    """Write a fake ``codex app-server`` that answers the JSON-RPC handshake, then runs ``turn``.

    Each request the server reads is appended to ``<directory>/codex.requests``
    as one JSON line. ``turn`` runs after the ``turn/start`` response, with
    ``emit(message)`` to print one JSON-RPC message.

    Args:
        directory: Where the script, its log and its requests go.
        turn: Python statements that send the turn's notifications.
        setup: Python statements that run before the handshake.

    Returns:
        The script path.
    """
    requests = directory / "codex.requests"
    behavior = (
        "def emit(message):\n"
        "    print(json.dumps(message), flush=True)\n"
        f"{setup}\n"
        "for line in sys.stdin:\n"
        "    message = json.loads(line)\n"
        f"    open({str(requests)!r}, 'a').write(json.dumps(message) + '\\n')\n"
        "    if message.get('id') == 1:\n"
        "        emit({'id': 1, 'result': {}})\n"
        "    elif message.get('id') == 2:\n"
        "        emit({'id': 2, 'result': {'thread': {'id': 'thr'}}})\n"
        "    elif message.get('id') == 3:\n"
        "        emit({'id': 3, 'result': {'turn': {'id': 't1'}}})\n"
        + "".join(f"        {statement}\n" for statement in turn.splitlines())
    )
    return fake_cli(directory, "codex", behavior, read_stdin=False)


def emitting(*messages: dict[str, Any]) -> str:
    """Return app-server turn behavior that sends ``messages`` in order."""
    return "".join(f"emit({message!r})\n" for message in messages)


def requests(directory: Path) -> list[dict[str, Any]]:
    """Return the JSON-RPC messages a fake app server read, oldest first."""
    path = directory / "codex.requests"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def calls(directory: Path, name: str) -> list[dict[str, Any]]:
    """Return the calls a fake CLI logged.

    Args:
        directory: The fake CLI's directory.
        name: The script name.

    Returns:
        One record per call, oldest first.
    """
    log = directory / f"{name}.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def printing(value: Any) -> str:
    """Return fake-CLI behavior that prints ``value`` as one JSON line."""
    return f"print(json.dumps({value!r}))"


def local_settings(tmp_path: Path, **overrides: Any) -> Settings:
    """Return local-mode settings whose CLIs are fake scripts in ``tmp_path``.

    Args:
        tmp_path: The test's directory.
        **overrides: More settings fields.

    Returns:
        A settings copy.
    """
    return settings.model_copy(
        update={
            "local_mode": True,
            "local_state_dir": str(tmp_path / "state"),
            "claude_bin": str(tmp_path / "claude"),
            "codex_bin": str(tmp_path / "codex"),
            "cli_fallback_order": "",
            "cli_max_concurrency": 2,
            "cli_call_timeout_seconds": 30.0,
            **overrides,
        }
    )


@pytest.fixture(autouse=True)
def _parent_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put provider keys in the parent environment, as a developer shell might."""
    for key in API_KEYS:
        monkeypatch.setenv(key, "parent-secret")


def test_claude_uses_the_subscription_and_reads_usage(tmp_path: Path) -> None:
    """Send the prompt on stdin with isolation flags and no keys, and read the real result shape."""
    fake_cli(tmp_path, "claude", printing(CLAUDE_SUCCESS))
    completion = cli_runners.run_claude("claude-haiku-4-5", "ping", None, local_settings(tmp_path), tmp_path)
    assert completion.text == "pong"
    assert completion.prompt_tokens == 2 + 2275 + 649
    assert completion.completion_tokens == 4
    assert completion.model_ms == 1326
    [call] = calls(tmp_path, "claude")
    assert "--bare" not in call["argv"]
    assert call["argv"][:5] == ["-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
    assert call["argv"][call["argv"].index("--model") + 1] == "claude-haiku-4-5"
    for flag in ("--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"):
        assert flag in call["argv"]
    assert call["stdin"] == "ping"
    assert not set(API_KEYS) & set(call["env"])


def test_claude_marks_the_repeated_prefix_for_the_cache(tmp_path: Path) -> None:
    """Send the prompt as two stream-json text blocks with a cache mark after the repeated prefix."""
    fake_cli(tmp_path, "claude", printing(CLAUDE_SUCCESS))
    prefix = "### System\nThe fixed instructions."
    prompt = f"{prefix}\n\n### User\nping"
    settings = local_settings(tmp_path)
    cli_runners.run_claude("claude-haiku-4-5", prompt, None, settings, tmp_path, cache_prefix=prefix)
    cli_runners.run_claude("claude-haiku-4-5", prefix, None, settings, tmp_path, cache_prefix=prefix)
    marked, whole = calls(tmp_path, "claude")
    assert marked["argv"][marked["argv"].index("--input-format") + 1] == "stream-json"
    message = json.loads(marked["stdin"])
    assert message["type"] == "user"
    assert message["message"]["role"] == "user"
    first, rest = message["message"]["content"]
    assert first == {"type": "text", "text": prefix, "cache_control": {"type": "ephemeral", "ttl": "1h"}}
    assert rest == {"type": "text", "text": "\n\n### User\nping"}
    assert first["text"] + rest["text"] == prompt
    # A prompt that is only the prefix leaves no second block, so it goes as plain text.
    assert "--input-format" not in whole["argv"]
    assert whole["stdin"] == prefix


def test_claude_prefers_structured_output_for_a_schema(tmp_path: Path) -> None:
    """Pass the schema flag and answer with the structured value."""
    fake_cli(tmp_path, "claude", printing({**CLAUDE_SUCCESS, "result": "", "structured_output": {"answer": 1}}))
    schema = {"type": "object", "properties": {"answer": {"type": "integer"}}}
    completion = cli_runners.run_claude("claude-haiku-4-5", "q", schema, local_settings(tmp_path), tmp_path)
    assert json.loads(completion.text) == {"answer": 1}
    [call] = calls(tmp_path, "claude")
    assert json.loads(call["argv"][call["argv"].index("--json-schema") + 1]) == schema


def test_claude_schema_call_does_not_deny_the_answer_tool(tmp_path: Path) -> None:
    """Send a system prompt that lets the model call the StructuredOutput tool a schema adds."""
    fake_cli(tmp_path, "claude", printing({**CLAUDE_SUCCESS, "result": "", "structured_output": {"answer": 1}}))
    schema = {"type": "object", "properties": {"answer": {"type": "integer"}}}
    cli_runners.run_claude("claude-haiku-4-5", "q", schema, local_settings(tmp_path), tmp_path)
    [call] = calls(tmp_path, "claude")
    assert "--json-schema" in call["argv"]
    assert "no tools" not in call["argv"][call["argv"].index("--system-prompt") + 1].lower()


def test_claude_schema_call_without_structured_output_is_an_error(tmp_path: Path) -> None:
    """Fail, not return free text, when claude skips the StructuredOutput tool for a schema."""
    fake_cli(tmp_path, "claude", printing({**CLAUDE_SUCCESS, "num_turns": 2, "result": "The answer is 1."}))
    schema = {"type": "object", "properties": {"answer": {"type": "integer"}}}
    with pytest.raises(CliRunError) as caught:
        cli_runners.run_claude("claude-haiku-4-5", "q", schema, local_settings(tmp_path), tmp_path)
    assert caught.value.provider == "claude"
    assert caught.value.transient


def test_claude_limit_result_is_a_rate_limit_with_its_reset(tmp_path: Path) -> None:
    """Turn an error result about the usage limit into a cap with a parsed reset time."""
    message = "Claude AI usage limit reached|1893456000"
    fake_cli(tmp_path, "claude", printing({**CLAUDE_SUCCESS, "is_error": True, "result": message}) + "\nsys.exit(1)")
    with pytest.raises(SubscriptionRateLimitedError) as caught:
        cli_runners.run_claude("claude-haiku-4-5", "ping", None, local_settings(tmp_path), tmp_path)
    assert caught.value.provider == "claude"
    assert caught.value.reset_at == datetime.fromtimestamp(1893456000, UTC)


def claude_delta(kind: str, text: str) -> dict[str, Any]:
    """Return one ``stream-json`` partial-message event with a thinking or text delta.

    Args:
        kind: ``"thinking"`` or ``"text"``.
        text: The delta text.

    Returns:
        The event, in the shape ``claude -p --include-partial-messages`` prints.
    """
    return {
        "type": "stream_event",
        "event": {"type": "content_block_delta", "index": 0, "delta": {"type": f"{kind}_delta", kind: text}},
    }


def waiting_for(marker: Path, *events: dict[str, Any]) -> str:
    """Return fake-CLI behavior that prints ``events`` and then waits up to 10 s for ``marker``.

    Args:
        marker: The file the test's callback writes once it got the events.
        *events: The events printed before the wait.

    Returns:
        Python statements; the script exits 3 when the marker never appears.
    """
    lines = "".join(f"print(json.dumps({event!r}), flush=True)\n" for event in events)
    return (
        f"{lines}deadline = time.time() + 10\n"
        f"while not os.path.exists({str(marker)!r}):\n"
        "    if time.time() > deadline:\n"
        "        sys.exit(3)\n"
        "    time.sleep(0.02)\n"
    )


def test_claude_streams_thinking_and_answer_text_before_it_exits(tmp_path: Path) -> None:
    """Pass each thinking and text delta on while claude still runs, and keep the result's answer and usage."""
    marker = tmp_path / "seen"
    events = [{"type": "system", "subtype": "init"}, claude_delta("thinking", "Let me "), claude_delta("text", "po")]
    behavior = waiting_for(marker, *events) + printing(claude_delta("text", "ng")) + "\n" + printing(CLAUDE_SUCCESS)
    fake_cli(tmp_path, "claude", behavior)
    deltas: list[tuple[str, str]] = []

    def on_delta(kind: str, text: str) -> None:
        """Record the delta, and let the fake CLI finish once two arrived."""
        deltas.append((kind, text))
        if len(deltas) == 2:
            marker.write_text("1")

    completion = cli_runners.run_claude("claude-haiku-4-5", "ping", None, local_settings(tmp_path), tmp_path, on_delta)
    assert deltas == [("reasoning", "Let me "), ("content", "po"), ("content", "ng")]
    assert (completion.text, completion.completion_tokens, completion.model_ms) == ("pong", 4, 1326)


def test_claude_schema_call_streams_only_thinking(tmp_path: Path) -> None:
    """Stream no answer text for a schema call, whose answer is the structured value."""
    events = [claude_delta("thinking", "hmm"), claude_delta("text", "free text")]
    result = {**CLAUDE_SUCCESS, "result": "", "structured_output": {"answer": 1}}
    fake_cli(tmp_path, "claude", "\n".join(printing(event) for event in [*events, result]))
    deltas: list[tuple[str, str]] = []
    schema = {"type": "object", "properties": {"answer": {"type": "integer"}}}
    completion = cli_runners.run_claude(
        "claude-haiku-4-5", "q", schema, local_settings(tmp_path), tmp_path, lambda *d: deltas.append(d)
    )
    assert deltas == [("reasoning", "hmm")]
    assert json.loads(completion.text) == {"answer": 1}


def test_claude_stream_without_a_result_is_a_failure(tmp_path: Path) -> None:
    """Fail with claude's stderr when the stream ends before its result event."""
    fake_cli(
        tmp_path, "claude", printing({"type": "system", "subtype": "init"}) + "\nsys.stderr.write('API Error: 500')"
    )
    with pytest.raises(CliRunError) as caught:
        cli_runners.run_claude("claude-haiku-4-5", "ping", None, local_settings(tmp_path), tmp_path)
    assert "API Error: 500" in caught.value.message
    assert caught.value.transient


def _summary(item: str, index: int, delta: str) -> dict[str, Any]:
    """Return one ``item/reasoning/summaryTextDelta`` notification.

    Args:
        item: The reasoning item id.
        index: The summary part index.
        delta: The text piece.

    Returns:
        The notification.
    """
    return {
        "method": "item/reasoning/summaryTextDelta",
        "params": {"itemId": item, "summaryIndex": index, "delta": delta},
    }


def test_codex_streams_reasoning_and_answer_before_the_turn_ends(tmp_path: Path) -> None:
    """Pass each reasoning summary and answer piece on while the turn still runs, and skip commentary."""
    marker = tmp_path / "seen"
    commentary = [
        {"method": "item/started", "params": {"item": {"id": "c1", "type": "agentMessage", "phase": "commentary"}}},
        {"method": "item/agentMessage/delta", "params": {"itemId": "c1", "delta": "Looking"}},
    ]
    first = [_summary("rs_1", 0, "**Plan**"), _summary("rs_1", 0, " now"), *commentary, _summary("rs_1", 1, "**Next**")]
    wait = (
        "deadline = time.time() + 10\n"
        f"while not os.path.exists({str(marker)!r}):\n"
        "    if time.time() > deadline:\n"
        "        sys.exit(3)\n"
        "    time.sleep(0.02)\n"
    )
    fake_app_server(tmp_path, emitting(*first) + wait + emitting(*CODEX_TURN))
    deltas: list[tuple[str, str]] = []

    def on_delta(kind: str, text: str) -> None:
        """Record the delta, and let the fake server finish once the reasoning arrived."""
        deltas.append((kind, text))
        if len(deltas) == 3:
            marker.write_text("1")

    completion = cli_runners.run_codex("gpt-5.6-luna", "ping", None, local_settings(tmp_path), tmp_path, on_delta)
    assert deltas == [
        ("reasoning", "**Plan**"),
        ("reasoning", " now"),
        ("reasoning", "\n\n**Next**"),
        ("content", "po"),
        ("content", "ng"),
    ]
    assert completion.text == "pong"


def test_codex_schema_call_streams_no_answer_text(tmp_path: Path) -> None:
    """Send the schema with the turn, and stream only reasoning: the answer is the structured value."""
    fake_app_server(tmp_path, emitting(_summary("rs_1", 0, "hmm"), *CODEX_TURN))
    deltas: list[tuple[str, str]] = []
    schema = {"type": "object"}
    cli_runners.run_codex(
        "gpt-5.6-luna", "ping", schema, local_settings(tmp_path), tmp_path, lambda *d: deltas.append(d)
    )
    assert deltas == [("reasoning", "hmm")]
    turn = next(message for message in requests(tmp_path) if message.get("method") == "turn/start")
    assert turn["params"]["outputSchema"] == schema


def test_codex_runs_one_turn_over_the_app_server(tmp_path: Path) -> None:
    """Start a read-only ephemeral thread with the fixed prompt, send the prompt as the turn, and read usage."""
    fake_app_server(tmp_path, emitting(*CODEX_TURN))
    completion = cli_runners.run_codex("gpt-5.6-luna", "ping", None, local_settings(tmp_path), tmp_path)
    assert completion.text == "pong"
    assert (completion.prompt_tokens, completion.completion_tokens) == (29250, 43)
    assert completion.model_ms is not None
    [call] = calls(tmp_path, "codex")
    assert call["argv"][0] == "app-server"
    assert not set(API_KEYS) & set(call["env"])
    sent = {message.get("method"): message.get("params") for message in requests(tmp_path)}
    assert sent["thread/start"] == {
        "model": "gpt-5.6-luna",
        "baseInstructions": cli_runners.SYSTEM_PROMPT,
        "sandbox": "read-only",
        "approvalPolicy": "never",
        "ephemeral": True,
        "cwd": str(tmp_path),
    }
    assert sent["turn/start"] == {"input": [{"type": "text", "text": "ping"}], "summary": "auto", "threadId": "thr"}


def test_claude_runs_at_the_turn_effort(tmp_path: Path) -> None:
    """Pass the turn's effort to Claude Code, and drop a level it cannot take."""
    fake_cli(tmp_path, "claude", printing(CLAUDE_SUCCESS))
    cli_runners.run_claude("claude-haiku-4-5", "ping", None, local_settings(tmp_path), tmp_path, effort="max")
    cli_runners.run_claude("claude-haiku-4-5", "ping", None, local_settings(tmp_path), tmp_path, effort="minimal")
    first, second = calls(tmp_path, "claude")
    assert first["argv"][first["argv"].index("--effort") + 1] == "max"
    assert "--effort" not in second["argv"]


def test_codex_runs_the_turn_at_its_effort(tmp_path: Path) -> None:
    """Send the turn's effort with ``turn/start``, and drop a level Codex cannot take."""
    fake_app_server(tmp_path, emitting(*CODEX_TURN))
    cli_runners.run_codex("gpt-5.6-luna", "ping", None, local_settings(tmp_path), tmp_path, effort="xhigh")
    cli_runners.run_codex("gpt-5.6-luna", "ping", None, local_settings(tmp_path), tmp_path, effort="none")
    first, second = (message for message in requests(tmp_path) if message.get("method") == "turn/start")
    assert first["params"]["effort"] == "xhigh"
    assert "effort" not in second["params"]


def test_codex_refuses_a_server_request(tmp_path: Path) -> None:
    """Answer an approval request with an error, since no host tool may run."""
    ask = {"id": 99, "method": "item/commandExecution/requestApproval", "params": {}}
    # A real server waits for the answer; without the wait the fake can exit before it logs it.
    wait = f"open({str(tmp_path / 'codex.requests')!r}, 'a').write(sys.stdin.readline())\n"
    fake_app_server(tmp_path, emitting(ask) + wait + emitting(*CODEX_TURN))
    assert cli_runners.run_codex("gpt-5.6-luna", "ping", None, local_settings(tmp_path), tmp_path).text == "pong"
    [reply] = [message for message in requests(tmp_path) if message.get("id") == 99]
    assert reply["error"]["code"] == -32601


def test_codex_runs_in_private_homes_and_keeps_a_refreshed_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Give codex a temporary HOME and CODEX_HOME with a login copy, and keep a login it refreshed."""
    user_home = tmp_path / "user"
    (user_home / ".codex").mkdir(parents=True)
    (user_home / ".codex" / "auth.json").write_text("old-login", encoding="utf-8")
    (user_home / ".codex" / "AGENTS.md").write_text("personal instructions", encoding="utf-8")
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.delenv("CODEX_HOME")
    fake_app_server(
        tmp_path,
        emitting(*CODEX_TURN),
        setup="auth = os.path.join(os.environ['CODEX_HOME'], 'auth.json')\n"
        "assert open(auth).read() == 'old-login'\n"
        "assert sorted(os.listdir(os.environ['CODEX_HOME'])) == ['auth.json']\n"
        "open(auth, 'w').write('new-login')",
    )
    completion = cli_runners.run_codex("gpt-5.6-luna", "ping", None, local_settings(tmp_path), tmp_path)
    assert completion.text == "pong"
    [call] = calls(tmp_path, "codex")
    assert call["env"]["HOME"] != str(user_home)
    assert call["env"]["CODEX_HOME"] != str(user_home / ".codex")
    assert "mcp_servers={}" in call["argv"]
    assert (user_home / ".codex" / "auth.json").read_text(encoding="utf-8") == "new-login"
    assert stat.S_IMODE((user_home / ".codex" / "auth.json").stat().st_mode) == 0o600
    assert not Path(call["env"]["CODEX_HOME"]).exists()


def test_claude_keeps_the_real_home_for_its_keychain_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep claude's real HOME, because its Keychain login works only from its own config folder."""
    user_home = tmp_path / "user"
    user_home.mkdir()
    monkeypatch.setenv("HOME", str(user_home))
    fake_cli(tmp_path, "claude", printing(CLAUDE_SUCCESS))
    cli_runners.run_claude("claude-haiku-4-5", "ping", None, local_settings(tmp_path), tmp_path)
    [call] = calls(tmp_path, "claude")
    assert call["env"]["HOME"] == str(user_home)
    assert call["argv"][call["argv"].index("--setting-sources") + 1] == ""


# The exact flags, written out, so a change to them fails here.
CLAUDE_FLAGS = [
    "-p",
    "--output-format",
    "stream-json",
    "--verbose",
    "--include-partial-messages",
    "--tools",
    "",
    "--system-prompt",
    "You are a helpful assistant. Answer the user's message directly. "
    "Do not run code or use tools, except a tool that returns your final answer in a required format.",
    "--strict-mcp-config",
    "--disable-slash-commands",
    "--setting-sources",
    "",
    "--no-session-persistence",
    "--settings",
    '{"showThinkingSummaries": true}',
    "--model",
    "claude-haiku-4-5",
]
CODEX_DISABLED = [
    "shell_tool",
    "unified_exec",
    "code_mode_host",
    "apps",
    "plugins",
    "multi_agent",
    "memories",
    "browser_use",
    "computer_use",
    "image_generation",
    "view_image",
    "hooks",
    "skill_search",
    "goals",
    "sleep_tool",
    "tool_suggest",
]
CODEX_CONFIG = [
    "include_permissions_instructions=false",
    "include_apps_instructions=false",
    "include_environment_context=false",
    "tools.update_plan.enabled=false",
    "tools.experimental_request_user_input.enabled=false",
    'features.code_mode.excluded_tool_namespaces=["functions"]',
]


def test_claude_gets_the_exact_flags(tmp_path: Path) -> None:
    """Pass no tools, the fixed system prompt, and flags that load no settings, MCP servers or commands."""
    fake_cli(tmp_path, "claude", printing(CLAUDE_SUCCESS))
    cli_runners.run_claude("claude-haiku-4-5", "ping", None, local_settings(tmp_path), tmp_path)
    [call] = calls(tmp_path, "claude")
    assert call["argv"] == CLAUDE_FLAGS


def test_codex_gets_the_exact_flags(tmp_path: Path) -> None:
    """Pass the isolation config, every disabled feature, and the context config to the app server."""
    fake_app_server(tmp_path, emitting(*CODEX_TURN))
    cli_runners.run_codex("gpt-5.6-luna", "ping", None, local_settings(tmp_path), tmp_path)
    [call] = calls(tmp_path, "codex")
    assert call["argv"] == [
        "app-server",
        "-c",
        "mcp_servers={}",
        "-c",
        "project_doc_max_bytes=0",
        "-c",
        "skills.include_instructions=false",
        "-c",
        'web_search="disabled"',
        *(arg for feature in CODEX_DISABLED for arg in ("--disable", feature)),
        *(arg for setting in CODEX_CONFIG for arg in ("-c", setting)),
    ]


def _plant_user_setup(home: Path, project: Path) -> None:
    """Plant a user's codex and claude setup: instructions, config, hooks, MCP servers, rules and skills."""
    files = {
        home / ".codex" / "auth.json": "old-login",
        home / ".codex" / "AGENTS.md": "user instructions",
        home / ".codex" / "config.toml": 'developer_instructions = "x"\n[mcp_servers.x]\ncommand = "x"\n',
        home / ".codex" / "hooks.json": "{}",
        home / ".codex" / "rules" / "default.rules": "x",
        home / ".codex" / "skills" / "x" / "SKILL.md": "x",
        home / ".claude" / "CLAUDE.md": "user instructions",
        home / ".claude" / "settings.json": '{"hooks": {}}',
        home / ".claude.json": '{"mcpServers": {}}',
        project / "AGENTS.md": "project instructions",
        project / "CLAUDE.md": "project instructions",
        project / ".mcp.json": '{"mcpServers": {}}',
        project / ".codex" / "config.toml": 'developer_instructions = "x"\n',
    }
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def test_codex_cannot_reach_the_user_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Give codex homes that hold only the login, and flags that skip project config, docs and rules."""
    home, project = tmp_path / "user", tmp_path / "project"
    _plant_user_setup(home, project)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEX_HOME")
    fake_app_server(
        tmp_path,
        emitting(*CODEX_TURN),
        setup="assert sorted(os.listdir(os.environ['CODEX_HOME'])) == ['auth.json']\n"
        "assert os.listdir(os.environ['HOME']) == []",
    )
    cli_runners.run_codex("gpt-5.6-luna", "ping", None, local_settings(tmp_path), project)
    [call] = calls(tmp_path, "codex")
    assert str(home) not in json.dumps(call["env"])
    for flag in ("mcp_servers={}", "project_doc_max_bytes=0"):
        assert flag in call["argv"]
    [thread] = [message["params"] for message in requests(tmp_path) if message.get("method") == "thread/start"]
    assert thread["ephemeral"] is True
    assert ("--disable", "hooks") in zip(call["argv"], call["argv"][1:], strict=False)


def test_claude_loads_no_user_setup_through_its_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep claude's HOME for the login, and turn off every setup source with flags, not paths."""
    home, project = tmp_path / "user", tmp_path / "project"
    _plant_user_setup(home, project)
    monkeypatch.setenv("HOME", str(home))
    fake_cli(tmp_path, "claude", printing(CLAUDE_SUCCESS))
    cli_runners.run_claude("claude-haiku-4-5", "ping", None, local_settings(tmp_path), project)
    [call] = calls(tmp_path, "claude")
    argv = call["argv"]
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--system-prompt") + 1] == cli_runners.SYSTEM_PROMPT
    assert {"--strict-mcp-config", "--disable-slash-commands"} <= set(argv)
    assert "--mcp-config" not in argv
    # The one inline setting turns on thinking text; it loads no file, hook or server.
    assert json.loads(argv[argv.index("--settings") + 1]) == {"showThinkingSummaries": True}


def test_codex_failed_turn_with_a_limit_is_a_rate_limit(tmp_path: Path) -> None:
    """Report a capped codex turn as a cap, not as a failed call."""
    error = {"message": "You've hit your usage limit. Try again at 2030-01-01T00:00:00Z."}
    failed = {"method": "turn/completed", "params": {"turn": {"id": "t1", "status": "failed", "error": error}}}
    fake_app_server(tmp_path, emitting(failed))
    with pytest.raises(SubscriptionRateLimitedError) as caught:
        cli_runners.run_codex("gpt-5.6-luna", "ping", None, local_settings(tmp_path), tmp_path)
    assert caught.value.reset_at == datetime(2030, 1, 1, tzinfo=UTC)


def test_only_claude_and_codex_have_runners() -> None:
    """Keep droid out: it cannot run without the user's hooks, MCP servers, config or rules."""
    assert set(cli_runners.RUNNERS) == {"claude", "codex"}


def test_missing_binary_is_a_final_failure(tmp_path: Path) -> None:
    """Report a CLI that is not installed as a failure no retry can fix."""
    with pytest.raises(CliRunError) as caught:
        cli_runners.run_claude("claude-haiku-4-5", "ping", None, local_settings(tmp_path), tmp_path)
    assert not caught.value.transient


def test_overloaded_failure_is_transient(tmp_path: Path) -> None:
    """Mark an overloaded provider as a failure a retry can fix."""
    fake_cli(tmp_path, "claude", "sys.stderr.write('API Error: 529 overloaded')\nsys.exit(1)")
    with pytest.raises(CliRunError) as caught:
        cli_runners.run_claude("claude-haiku-4-5", "ping", None, local_settings(tmp_path), tmp_path)
    assert caught.value.transient


def test_timeout_is_final(tmp_path: Path) -> None:
    """Stop a CLI at the call timeout, and do not mark it for a retry: it would hang again."""
    fake_cli(tmp_path, "claude", "time.sleep(10)")
    with pytest.raises(CliRunError) as caught:
        cli_runners.run_claude(
            "claude-haiku-4-5", "ping", None, local_settings(tmp_path, cli_call_timeout_seconds=0.5), tmp_path
        )
    assert not caught.value.transient


def _gone(pid: int) -> bool:
    """Wait up to three seconds for a process to stop, and say whether it did."""
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


@pytest.mark.parametrize(("name", "runner"), [("claude", cli_runners.run_claude), ("codex", cli_runners.run_codex)])
def test_timeout_stops_the_child_of_a_wrapper(tmp_path: Path, name: str, runner: Any) -> None:
    """Stop a wrapper's child at the timeout too, though it keeps the output pipe open."""
    pid_file = tmp_path / "child.pid"
    fake_cli(
        tmp_path,
        name,
        "import subprocess\n"
        "child = subprocess.Popen(['sleep', '30'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "child.wait()",
        read_stdin=False,
    )
    started = time.monotonic()
    with pytest.raises(CliRunError) as caught:
        runner("m", "ping", None, local_settings(tmp_path, cli_call_timeout_seconds=3.0), tmp_path)
    # The timeout, the kill grace, and slack for a loaded test machine; the child sleeps 30 s.
    assert time.monotonic() - started < 10
    assert caught.value.message == f"{name} timed out"
    assert _gone(int(pid_file.read_text()))


@pytest.mark.parametrize(("name", "runner"), [("claude", cli_runners.run_claude), ("codex", cli_runners.run_codex)])
def test_early_exit_with_a_long_prompt_keeps_the_cli_reason(tmp_path: Path, name: str, runner: Any) -> None:
    """Report a CLI that exits before it reads a long prompt as a failed call with its stderr."""
    script = tmp_path / name
    script.write_text(f"#!{sys.executable}\nimport sys\nsys.stderr.write('not logged in')\nsys.exit(1)\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    with pytest.raises(CliRunError) as caught:
        runner("m", "x" * 300_000, None, local_settings(tmp_path), tmp_path)
    assert "not logged in" in caught.value.message


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("usage limit reached|1893456000", datetime.fromtimestamp(1893456000, UTC)),
        ("limit reached, resets 2030-01-02T03:04:05+00:00", datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)),
        ("no reset time here", None),
    ],
)
def test_parse_reset_at_reads_epoch_and_iso_stamps(text: str, expected: datetime | None) -> None:
    """Read the reset time from the forms the CLIs print."""
    assert parse_reset_at(text) == expected


def test_parse_reset_at_reads_a_clock_time_as_the_next_one() -> None:
    """Read ``resets 3pm`` as the next 3pm on this machine's clock."""
    now = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    reset = parse_reset_at("You've hit your limit · resets 3pm", now=now)
    assert reset is not None
    assert now < reset <= now.replace(day=2, hour=12)
    assert reset.astimezone().hour == 15

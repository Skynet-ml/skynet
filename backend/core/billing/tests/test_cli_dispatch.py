"""Tests for the subscription dispatcher: answer shape, caps, fallback, concurrency and streaming."""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from core import config
from core.billing import cli_dispatch
from core.billing.cli_dispatch import CliDispatcher, local_subscription_route
from core.billing.operation_pricing import UnpricedOperationError
from core.billing.tests.test_cli_runners import (
    CLAUDE_SUCCESS,
    CODEX_TURN,
    calls,
    claude_delta,
    emitting,
    fake_app_server,
    fake_cli,
    local_settings,
    printing,
    requests,
    waiting_for,
)
from core.local.cli_probe import read_cli_limits

MODEL = "anthropic/claude-haiku-4.5"
PROMPT_PRICE = Decimal("0.000001")
COMPLETION_PRICE = Decimal("0.000005")
REQUEST = {"model": MODEL, "messages": [{"role": "user", "content": "ping"}]}
CAPPED = "print('Claude AI usage limit reached|1893456000')\nsys.exit(1)"


@pytest.fixture(autouse=True)
def _fixture_prices(monkeypatch: pytest.MonkeyPatch) -> None:
    """Price every model at fixed list prices and skip the back-off waits."""
    monkeypatch.setattr(cli_dispatch, "_list_prices", lambda model: (PROMPT_PRICE, COMPLETION_PRICE))
    monkeypatch.setattr(cli_dispatch, "_sleep", lambda seconds: None)
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY"):
        monkeypatch.setenv(key, "parent-secret")


def _body(result) -> dict:
    """Return a JSON answer body."""
    return json.loads(result.body)


def test_chat_completion_has_the_openai_shape_and_list_price(tmp_path: Path) -> None:
    """Answer in the chat completion shape with usage priced at tokens times the list price."""
    fake_cli(tmp_path, "claude", printing(CLAUDE_SUCCESS))
    result = CliDispatcher(None, "claude", MODEL, "judge", local_settings(tmp_path)).dispatch(
        "/chat/completions", REQUEST
    )
    assert result.status == 200
    body = _body(result)
    assert body["object"] == "chat.completion"
    assert body["model"] == MODEL
    assert body["choices"][0]["message"] == {"role": "assistant", "content": "pong"}
    usage = body["usage"]
    prompt_tokens = 2 + 2275 + 649
    assert (usage["prompt_tokens"], usage["completion_tokens"]) == (prompt_tokens, 4)
    assert usage["cost"] == pytest.approx(float(PROMPT_PRICE * prompt_tokens + COMPLETION_PRICE * 4))
    assert usage["token_source"] == "subscription"
    assert usage["cli_provider"] == "claude"
    assert usage["model_ms"] == 1326
    [call] = calls(tmp_path, "claude")
    assert "--bare" not in call["argv"]
    assert not {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY"} & set(call["env"])


def test_cap_answers_429_and_records_the_reset(tmp_path: Path) -> None:
    """Answer a capped call with 429 and its reset time, and write cli_limits.json."""
    fake_cli(tmp_path, "claude", CAPPED)
    settings = local_settings(tmp_path)
    result = CliDispatcher(None, "claude", MODEL, "judge", settings).dispatch("/chat/completions", REQUEST)
    assert result.status == 429
    error = _body(result)["error"]
    assert error["code"] == "subscription.rate_limited"
    assert error["provider"] == "claude"
    assert error["reset_at"] == datetime.fromtimestamp(1893456000, UTC).isoformat()
    assert (tmp_path / "state" / "cli_limits.json").exists()
    assert datetime.fromisoformat(read_cli_limits(settings)["claude"]["reset_at"]) == datetime.fromtimestamp(
        1893456000, UTC
    )


def test_recorded_cap_skips_the_cli_until_it_resets(tmp_path: Path) -> None:
    """Do not start a CLI whose recorded cap is still in force."""
    fake_cli(tmp_path, "claude", CAPPED)
    settings = local_settings(tmp_path)
    dispatcher = CliDispatcher(None, "claude", MODEL, "judge", settings)
    dispatcher.dispatch("/chat/completions", REQUEST)
    assert dispatcher.dispatch("/chat/completions", REQUEST).status == 429
    assert len(calls(tmp_path, "claude")) == 1


def test_fallback_order_that_names_droid_skips_it(tmp_path: Path) -> None:
    """Skip droid in a saved fallback order: it is not a runner, so it never starts."""
    fake_cli(tmp_path, "claude", CAPPED)
    fake_cli(tmp_path, "droid", "raise SystemExit('droid must not run')")
    settings = local_settings(tmp_path, cli_fallback_order="claude,droid")
    result = CliDispatcher(None, "claude", MODEL, "judge", settings).dispatch("/chat/completions", REQUEST)
    assert result.status == 429
    assert calls(tmp_path, "droid") == []


def test_fallback_skips_a_cli_without_the_model(tmp_path: Path) -> None:
    """Skip codex, which cannot run a Claude model, and report the cap."""
    fake_cli(tmp_path, "claude", CAPPED)
    fake_cli(tmp_path, "codex", "raise SystemExit('codex must not run')")
    settings = local_settings(tmp_path, cli_fallback_order="claude,codex")
    result = CliDispatcher(None, "claude", MODEL, "judge", settings).dispatch("/chat/completions", REQUEST)
    assert result.status == 429
    assert calls(tmp_path, "codex") == []


def test_transient_failure_is_retried(tmp_path: Path) -> None:
    """Retry an overloaded call and answer from the retry."""
    marker = tmp_path / "failed-once"
    fake_cli(
        tmp_path,
        "claude",
        f"marker = {str(marker)!r}\n"
        "if not os.path.exists(marker):\n"
        "    open(marker, 'w').close()\n"
        "    sys.stderr.write('API Error: 529 overloaded')\n"
        "    sys.exit(1)\n"
        f"{printing(CLAUDE_SUCCESS)}",
    )
    result = CliDispatcher(None, "claude", MODEL, "judge", local_settings(tmp_path)).dispatch(
        "/chat/completions", REQUEST
    )
    assert result.status == 200
    assert len(calls(tmp_path, "claude")) == 2


def test_timeout_is_not_retried(tmp_path: Path) -> None:
    """Answer a timed-out call with 502 after one attempt, so a hung CLI holds a slot once."""
    fake_cli(tmp_path, "claude", "time.sleep(30)")
    settings = local_settings(tmp_path, cli_call_timeout_seconds=3.0)
    result = CliDispatcher(None, "claude", MODEL, "judge", settings).dispatch("/chat/completions", REQUEST)
    assert result.status == 502
    assert _body(result)["error"]["message"] == "claude timed out"
    assert len(calls(tmp_path, "claude")) == 1


def test_messages_request_answers_in_the_anthropic_shape(tmp_path: Path) -> None:
    """Answer an Anthropic messages call, with its system text and tool turns in the prompt."""
    fake_cli(tmp_path, "claude", printing(CLAUDE_SUCCESS))
    request = {
        "model": MODEL,
        "max_tokens": 16,
        "system": [{"type": "text", "text": "Be brief."}],
        "messages": [
            {"role": "user", "content": "ping"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "look", "input": {"q": 1}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "seen"}]},
        ],
    }
    result = CliDispatcher(None, "claude", MODEL, "optimization", local_settings(tmp_path)).dispatch(
        "/messages", request
    )
    assert result.status == 200
    body = _body(result)
    assert (body["type"], body["role"], body["stop_reason"]) == ("message", "assistant", "end_turn")
    assert body["content"] == [{"type": "text", "text": "pong"}]
    assert body["usage"]["input_tokens"] == 2 + 2275 + 649
    assert body["usage"]["output_tokens"] == 4
    assert body["usage"]["token_source"] == "subscription"
    [call] = calls(tmp_path, "claude")
    prompt = "".join(block["text"] for block in json.loads(call["stdin"])["message"]["content"])
    for text in ("Be brief.", "ping", '"name": "look"', "Tool result: seen"):
        assert text in prompt


def test_messages_stream_answers_anthropic_events(tmp_path: Path) -> None:
    """Answer a streamed messages call with the Anthropic event sequence."""
    fake_cli(tmp_path, "claude", printing(CLAUDE_SUCCESS))
    request = {"model": MODEL, "max_tokens": 16, "stream": True, "messages": [{"role": "user", "content": "ping"}]}
    result = CliDispatcher(None, "claude", MODEL, "optimization", local_settings(tmp_path)).dispatch(
        "/messages", request
    )
    assert result.content_type == "text/event-stream"
    blocks = [block.split("\n") for block in result.body.decode().split("\n\n") if block]
    names = [lines[0].removeprefix("event: ") for lines in blocks]
    assert names == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    data = [json.loads(lines[1].removeprefix("data: ")) for lines in blocks]
    assert data[2]["delta"] == {"type": "text_delta", "text": "pong"}
    assert data[4]["usage"]["output_tokens"] == 4


def test_final_failure_answers_502(tmp_path: Path) -> None:
    """Answer a failed call with 502 and the CLI's short reason."""
    fake_cli(tmp_path, "claude", "sys.stderr.write('Invalid model name')\nsys.exit(1)")
    result = CliDispatcher(None, "claude", MODEL, "judge", local_settings(tmp_path)).dispatch(
        "/chat/completions", REQUEST
    )
    assert result.status == 502
    assert _body(result)["error"]["code"] == "subscription.cli_failed"
    assert len(calls(tmp_path, "claude")) == 1


def test_third_concurrent_call_waits_for_a_slot(tmp_path: Path) -> None:
    """Run at most two calls at once when ``cli_max_concurrency`` is 2."""
    fake_cli(tmp_path, "claude", f"time.sleep(0.6)\n{printing(CLAUDE_SUCCESS)}")
    dispatcher = CliDispatcher(None, "claude", MODEL, "judge", local_settings(tmp_path, cli_max_concurrency=2))
    finished: list[float] = []
    started = time.monotonic()

    def call() -> None:
        """Make one call and record when it ended."""
        assert dispatcher.dispatch("/chat/completions", REQUEST).status == 200
        finished.append(time.monotonic() - started)

    threads = [threading.Thread(target=call) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    finished.sort()
    assert len(finished) == 3
    assert finished[2] - finished[0] >= 0.5


def test_stream_answers_valid_sse(tmp_path: Path) -> None:
    """Answer ``stream: true`` with JSON chunks, usage on the last one, and ``[DONE]``."""
    fake_cli(tmp_path, "claude", printing(CLAUDE_SUCCESS))
    dispatcher = CliDispatcher(None, "claude", MODEL, "judge", local_settings(tmp_path))
    result = dispatcher.dispatch("/chat/completions", {**REQUEST, "stream": True})
    assert result.status == 200
    assert result.content_type == "text/event-stream"
    events = [line.removeprefix("data: ") for line in result.body.decode().split("\n\n") if line]
    assert events[-1] == "[DONE]"
    chunks = [json.loads(event) for event in events[:-1]]
    assert "".join(chunk["choices"][0]["delta"].get("content", "") for chunk in chunks) == "pong"
    assert chunks[-1]["usage"]["token_source"] == "subscription"


def test_schema_request_reaches_the_cli(tmp_path: Path) -> None:
    """Pass a ``json_schema`` response format to the CLI as its schema."""
    fake_cli(tmp_path, "claude", printing({**CLAUDE_SUCCESS, "structured_output": {"ok": True}}))
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    request = {**REQUEST, "response_format": {"type": "json_schema", "json_schema": {"name": "x", "schema": schema}}}
    result = CliDispatcher(None, "claude", MODEL, "judge", local_settings(tmp_path)).dispatch(
        "/chat/completions", request
    )
    assert json.loads(_body(result)["choices"][0]["message"]["content"]) == {"ok": True}
    [call] = calls(tmp_path, "claude")
    assert json.loads(call["argv"][call["argv"].index("--json-schema") + 1]) == schema


def test_turn_effort_reaches_the_cli(tmp_path: Path) -> None:
    """Run a turn at the effort its request carries, and ignore a value that is no level."""
    fake_cli(tmp_path, "claude", printing(CLAUDE_SUCCESS))
    model = "anthropic/claude-opus-5.5"
    request = {**REQUEST, "model": model}
    dispatcher = CliDispatcher(None, "claude", model, "agent", local_settings(tmp_path))
    dispatcher.dispatch("/chat/completions", {**request, cli_dispatch.CLI_EFFORT_KEY: "max"})
    dispatcher.dispatch("/chat/completions", {**request, cli_dispatch.CLI_EFFORT_KEY: {"effort": "high"}})
    dispatcher.dispatch("/chat/completions", {**request, cli_dispatch.CLI_EFFORT_KEY: "minimal"})
    dispatcher.dispatch("/chat/completions", {**request, "reasoning_effort": "low"})
    first, *rest = calls(tmp_path, "claude")
    assert first["argv"][first["argv"].index("--effort") + 1] == "max"
    assert all("--effort" not in call["argv"] for call in rest)


@pytest.mark.parametrize(
    ("cli", "model", "effort", "sent"),
    [
        ("claude", "anthropic/claude-opus-4.7", "xhigh", "xhigh"),
        ("claude", "anthropic/claude-sonnet-4.6", "max", "max"),
        ("claude", "anthropic/claude-sonnet-4.6", "xhigh", None),
        ("claude", "anthropic/claude-opus-4.5", "high", "high"),
        ("claude", "anthropic/claude-opus-4.5", "max", None),
        ("claude", "anthropic/claude-haiku-4.5", "low", None),
        ("claude", "anthropic/claude-sonnet-4.5", "medium", None),
        ("codex", "openai/gpt-6.1-sol", "max", "max"),
        ("codex", "openai/gpt-6.1-sol", "ultra", "max"),
        ("codex", "openai/gpt-6-luna", "ultra", "max"),
        ("codex", "openai/gpt-6.1-sol", "ultracode", "max"),
        ("claude", "anthropic/claude-opus-5.5", "ultracode", "max"),
        ("claude", "anthropic/claude-sonnet-4.6", "ultracode", "max"),
        ("claude", "anthropic/claude-opus-5.5", "ultra", "max"),
        ("claude", "anthropic/claude-opus-4.5", "ultracode", "high"),
        ("claude", "anthropic/claude-haiku-4.5", "ultracode", None),
    ],
)
def test_turn_effort_follows_the_model_levels(
    cli: str, model: str, effort: str, sent: str | None, tmp_path: Path
) -> None:
    """Send only a level the model takes on its CLI, map a retired level to its top, and drop any other.

    Args:
        cli: The CLI the turn runs on.
        model: The turn's catalog slug.
        effort: The picked level.
        sent: The level the CLI gets, or ``None`` when the dispatcher drops it.
        tmp_path: Holds the fake CLI and its call log.
    """
    if cli == "claude":
        fake_cli(tmp_path, "claude", printing(CLAUDE_SUCCESS))
    else:
        fake_app_server(tmp_path, emitting(*CODEX_TURN))
    settings = local_settings(tmp_path, cli_fallback_order="")
    CliDispatcher(None, cli, model, "agent", settings).dispatch(
        "/chat/completions", {**REQUEST, "model": model, cli_dispatch.CLI_EFFORT_KEY: effort}
    )
    if cli == "claude":
        [call] = calls(tmp_path, "claude")
        got = call["argv"][call["argv"].index("--effort") + 1] if "--effort" in call["argv"] else None
    else:
        [turn] = (message for message in requests(tmp_path) if message.get("method") == "turn/start")
        got = turn["params"].get("effort")
    assert got == sent


@pytest.mark.parametrize(
    ("cli", "model", "fast", "sent"),
    [
        ("claude", "anthropic/claude-opus-5.5", True, True),
        ("claude", "anthropic/claude-opus-4.7", True, False),
        ("claude", "anthropic/claude-opus-5.5", "yes", False),
        ("claude", "anthropic/claude-opus-5.5", None, False),
        ("codex", "openai/gpt-5.6-luna", True, True),
        ("codex", "openai/gpt-5.6-luna", 1, False),
    ],
)
def test_turn_fast_mode_follows_the_model(cli: str, model: str, fast: Any, sent: bool, tmp_path: Path) -> None:
    """Send fast mode only when the request asks with ``True`` and the model takes it.

    Args:
        cli: The CLI the turn runs on.
        model: The turn's catalog slug.
        fast: The request's fast value.
        sent: Whether the CLI gets fast mode.
        tmp_path: Holds the fake CLI and its call log.
    """
    if cli == "claude":
        fake_cli(tmp_path, "claude", printing(CLAUDE_SUCCESS))
    else:
        fake_app_server(tmp_path, emitting(*CODEX_TURN))
    settings = local_settings(tmp_path, cli_fallback_order="")
    CliDispatcher(None, cli, model, "agent", settings).dispatch(
        "/chat/completions", {**REQUEST, "model": model, cli_dispatch.CLI_FAST_KEY: fast}
    )
    if cli == "claude":
        [call] = calls(tmp_path, "claude")
        argv = call["argv"]
        got = "--fast-mode" in argv and argv[argv.index("--fast-mode") + 1] == "on"
    else:
        [turn] = (message for message in requests(tmp_path) if message.get("method") == "turn/start")
        got = turn["params"].get("serviceTier") == "priority"
    assert got is sent


def test_side_call_ignores_the_turn_effort(tmp_path: Path) -> None:
    """Keep a thinking label at no effort and no fast mode, because it runs without thinking."""
    fake_cli(tmp_path, "claude", printing(CLAUDE_SUCCESS))
    model = "anthropic/claude-opus-5.5"
    CliDispatcher(None, "claude", model, cli_dispatch.THINKING_LABEL_ROLE, local_settings(tmp_path)).dispatch(
        "/chat/completions",
        {**REQUEST, "model": model, cli_dispatch.CLI_EFFORT_KEY: "max", cli_dispatch.CLI_FAST_KEY: True},
    )
    [call] = calls(tmp_path, "claude")
    assert "--effort" not in call["argv"]
    assert "--fast-mode" not in call["argv"]


def test_agent_steps_share_one_empty_working_directory() -> None:
    """Run every step in the same empty directory, because the CLI puts the path ahead of the prompt."""
    first = cli_dispatch._step_cwd()
    assert cli_dispatch._step_cwd() == first
    assert first.is_dir()
    assert not any(first.iterdir())


def test_deleted_step_directory_is_made_again() -> None:
    """Give an existing empty directory after something deletes the cached one, so the CLI can start."""
    first = cli_dispatch._step_cwd()
    first.rmdir()
    again = cli_dispatch._step_cwd()
    assert again.is_dir()
    assert not any(again.iterdir())
    assert cli_dispatch._step_cwd() == again


def test_agent_step_marks_its_system_turn_for_the_cache(tmp_path: Path) -> None:
    """Mark the rendered system turn, which agent steps repeat, as claude's cache prefix."""
    fake_cli(tmp_path, "claude", printing(CLAUDE_SUCCESS))
    step = [
        {"role": "system", "content": "Instructions and tools."},
        {"role": "user", "content": "Which models?"},
        {"role": "assistant", "content": "I will look."},
    ]
    request = {**REQUEST, "messages": step}
    assert cli_dispatch.cache_prefix(request) == "### System\nInstructions and tools."
    assert cli_dispatch.render_prompt(request).startswith(cli_dispatch.cache_prefix(request) + "\n\n")
    assert cli_dispatch.cache_prefix(REQUEST) == ""
    assert cli_dispatch.cache_prefix({**REQUEST, "messages": step[1:]}) == ""
    result = CliDispatcher(None, "claude", MODEL, "judge", local_settings(tmp_path)).dispatch(
        "/chat/completions", request
    )
    assert result.status == 200
    [call] = calls(tmp_path, "claude")
    blocks = json.loads(call["stdin"])["message"]["content"]
    assert blocks[0]["text"] == "### System\nInstructions and tools."
    assert "cache_control" in blocks[0]


def test_other_paths_and_models_are_refused(tmp_path: Path) -> None:
    """Refuse another model, and answer 422 for a protocol a CLI cannot serve."""
    dispatcher = CliDispatcher(None, "claude", MODEL, "judge", local_settings(tmp_path))
    assert dispatcher.dispatch("/embeddings", REQUEST).status == 422
    assert dispatcher.dispatch("/responses", REQUEST).status == 422
    with pytest.raises(UnpricedOperationError):
        dispatcher.dispatch("/chat/completions", {**REQUEST, "model": "anthropic/claude-opus-4.5"})


def test_outside_local_mode_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse a subscription route when local mode is off."""
    with pytest.raises(UnpricedOperationError):
        CliDispatcher(None, "claude", MODEL, "judge", local_settings(tmp_path, local_mode=False))
    monkeypatch.setattr(config.settings, "local_mode", False)
    with pytest.raises(UnpricedOperationError):
        local_subscription_route(MODEL, "claude", "agent")


def test_unmapped_model_is_refused(tmp_path: Path) -> None:
    """Refuse a model the CLI cannot run."""
    with pytest.raises(UnpricedOperationError):
        CliDispatcher(None, "codex", MODEL, "judge", local_settings(tmp_path))


def test_local_route_serves_an_openai_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve API-side agents on a loopback route that needs its own token."""
    fake_cli(tmp_path, "claude", printing(CLAUDE_SUCCESS))
    monkeypatch.setattr(config, "settings", local_settings(tmp_path))
    monkeypatch.setattr(cli_dispatch, "_gateway", None)
    route = local_subscription_route(MODEL, "claude", "agent")
    assert local_subscription_route(MODEL, "claude", "agent")["token"] == route["token"]
    with httpx.Client(trust_env=False) as client:
        answer = client.post(
            f"{route['url']}/chat/completions", json=REQUEST, headers={"Authorization": f"Bearer {route['token']}"}
        )
        refused = client.post(f"{route['url']}/chat/completions", json=REQUEST, headers={"Authorization": "Bearer x"})
    assert answer.status_code == 200
    assert answer.json()["choices"][0]["message"]["content"] == "pong"
    assert refused.status_code == 401


def _stream_route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, behavior: str) -> dict[str, str]:
    """Return a fresh loopback route whose claude is a fake script running ``behavior``.

    Args:
        tmp_path: The test's directory.
        monkeypatch: Resets the process-wide gateway and settings after the test.
        behavior: The fake CLI's Python statements.

    Returns:
        The route.
    """
    fake_cli(tmp_path, "claude", behavior)
    monkeypatch.setattr(config, "settings", local_settings(tmp_path))
    monkeypatch.setattr(cli_dispatch, "_gateway", None)
    return local_subscription_route(MODEL, "claude", "agent")


def test_local_route_streams_thinking_and_answer_while_the_cli_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Send thinking as ``reasoning_content`` and the answer in more than one chunk before claude exits."""
    marker = tmp_path / "seen"
    events = [claude_delta("thinking", "Planning"), claude_delta("text", "po")]
    behavior = waiting_for(marker, *events) + printing(claude_delta("text", "ng")) + "\n" + printing(CLAUDE_SUCCESS)
    route = _stream_route(tmp_path, monkeypatch, behavior)
    deltas: list[dict] = []
    tail: list[str] = []
    with (
        httpx.Client(trust_env=False, timeout=30) as client,
        client.stream(
            "POST",
            f"{route['url']}/chat/completions",
            json={**REQUEST, "stream": True},
            headers={"Authorization": f"Bearer {route['token']}"},
        ) as answer,
    ):
        assert answer.status_code == 200
        assert answer.headers["content-type"] == "text/event-stream"
        for line in answer.iter_lines():
            if not line.startswith("data: "):
                continue
            data = line.removeprefix("data: ")
            if data == "[DONE]":
                tail.append(data)
                continue
            chunk = json.loads(data)
            deltas.append(chunk["choices"][0]["delta"])
            if chunk.get("usage"):
                tail.append("usage")
            # The fake CLI waits for this marker, so both pieces arrived while it ran.
            if len(deltas) == 2:
                marker.write_text("1")
    assert deltas[0] == {"reasoning_content": "Planning"}
    assert deltas[1] == {"content": "po"}
    content = [delta["content"] for delta in deltas if delta.get("content")]
    assert len(content) >= 2
    assert "".join(content) == "pong"
    assert tail == ["usage", "[DONE]"]


def test_local_route_failure_after_streaming_ends_with_an_error_chunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End a stream with an error chunk, not a retry, when claude fails after it sent text."""
    behavior = printing(claude_delta("thinking", "x")) + "\nsys.stderr.write('API Error: 529 overloaded')\nsys.exit(1)"
    route = _stream_route(tmp_path, monkeypatch, behavior)
    with httpx.Client(trust_env=False, timeout=30) as client:
        answer = client.post(
            f"{route['url']}/chat/completions",
            json={**REQUEST, "stream": True},
            headers={"Authorization": f"Bearer {route['token']}"},
        )
    assert answer.status_code == 200
    events = [json.loads(line.removeprefix("data: ")) for line in answer.text.split("\n\n") if line]
    assert events[0]["choices"][0]["delta"] == {"reasoning_content": "x"}
    assert events[-1]["error"]["code"] == "subscription.cli_failed"
    assert len(calls(tmp_path, "claude")) == 1


def test_local_route_failure_before_any_text_keeps_its_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer a streamed call that fails before any text with its own status, as before."""
    route = _stream_route(tmp_path, monkeypatch, CAPPED)
    with httpx.Client(trust_env=False, timeout=30) as client:
        answer = client.post(
            f"{route['url']}/chat/completions",
            json={**REQUEST, "stream": True},
            headers={"Authorization": f"Bearer {route['token']}"},
        )
    assert answer.status_code == 429
    assert answer.json()["error"]["code"] == "subscription.rate_limited"


TOOLS = [{"type": "function", "function": {"name": "search", "parameters": {"type": "object"}}}]
TOOL_REQUEST = {**REQUEST, "tools": TOOLS}
WRITTEN_CALL = (
    'Calling it now.\n```json\n{"tool_calls": [{"name": "search", "args": {"q": "x"}}]}\n```\n\n[[ ## completed ## ]]'
)


@pytest.mark.parametrize(
    ("text", "content"),
    [
        (WRITTEN_CALL, "Calling it now.\n"),
        ('{"tool_calls": [{"name": "search", "args": {"q": "x"}}]}', None),
        (
            '{"tool_calls": [{"type": "function", "function": {"name": "search", "arguments": "{\\"q\\": \\"x\\"}"}}]}',
            None,
        ),
    ],
)
def test_written_tool_call_becomes_a_native_tool_call(tmp_path: Path, text: str, content: str | None) -> None:
    """Answer a tool call the CLI wrote as JSON text with native ``tool_calls``, keeping the text before it."""
    fake_cli(tmp_path, "claude", printing({**CLAUDE_SUCCESS, "result": text}))
    result = CliDispatcher(None, "claude", MODEL, "agent", local_settings(tmp_path)).dispatch(
        "/chat/completions", TOOL_REQUEST
    )
    [choice] = _body(result)["choices"]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] == content
    [call] = choice["message"]["tool_calls"]
    assert call["type"] == "function"
    assert call["id"].startswith("call_")
    assert call["function"]["name"] == "search"
    assert json.loads(call["function"]["arguments"]) == {"q": "x"}


@pytest.mark.parametrize(
    ("request_body", "text"),
    [
        (TOOL_REQUEST, "The answer is 4."),
        (TOOL_REQUEST, '{"tool_calls": [{"name": "delete_everything", "args": {}}]}'),
        (TOOL_REQUEST, '{"tool_calls": [{"name": "search", "args": "not json"}]}'),
        (TOOL_REQUEST, '{"tool_calls": []}'),
        (REQUEST, '{"tool_calls": [{"name": "search", "args": {}}]}'),
    ],
)
def test_text_that_is_not_a_declared_tool_call_stays_text(tmp_path: Path, request_body: dict, text: str) -> None:
    """Keep plain text, undeclared tools, bad arguments and tool-less requests as text."""
    fake_cli(tmp_path, "claude", printing({**CLAUDE_SUCCESS, "result": text}))
    result = CliDispatcher(None, "claude", MODEL, "agent", local_settings(tmp_path)).dispatch(
        "/chat/completions", request_body
    )
    [choice] = _body(result)["choices"]
    assert choice["finish_reason"] == "stop"
    assert choice["message"] == {"role": "assistant", "content": text}


def _sse(text: str) -> list[dict]:
    """Return the JSON chunks of an SSE answer, checking it ends with ``[DONE]``.

    Args:
        text: The SSE body.

    Returns:
        The chunks, in order.
    """
    events = [line.removeprefix("data: ") for line in text.split("\n\n") if line]
    assert events[-1] == "[DONE]"
    return [json.loads(event) for event in events[:-1]]


def test_stream_written_tool_call_becomes_a_native_tool_call(tmp_path: Path) -> None:
    """Send a written tool call in a buffered stream as ``tool_calls`` with finish reason ``tool_calls``."""
    fake_cli(tmp_path, "claude", printing({**CLAUDE_SUCCESS, "result": WRITTEN_CALL}))
    dispatcher = CliDispatcher(None, "claude", MODEL, "agent", local_settings(tmp_path))
    chunks = _sse(dispatcher.dispatch("/chat/completions", {**TOOL_REQUEST, "stream": True}).body.decode())
    [call] = chunks[0]["choices"][0]["delta"]["tool_calls"]
    assert call["index"] == 0
    assert call["function"]["name"] == "search"
    assert chunks[0]["choices"][0]["delta"]["content"] == "Calling it now.\n"
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"


def _live_stream(
    route: dict[str, str], marker: Path | None = None, after: str = "", request: dict = TOOL_REQUEST
) -> list[dict]:
    """Read a streamed request (a tools request by default) from the loopback route, chunk by chunk.

    Args:
        route: The route.
        marker: A file to write once the content so far equals ``after``; the
            fake CLI waits for it, so that content arrived while it ran.
        after: The content that releases the fake CLI.
        request: The chat completion body to stream.

    Returns:
        The JSON chunks, in order.
    """
    chunks: list[dict] = []
    content = ""
    with (
        httpx.Client(trust_env=False, timeout=30) as client,
        client.stream(
            "POST",
            f"{route['url']}/chat/completions",
            json={**request, "stream": True},
            headers={"Authorization": f"Bearer {route['token']}"},
        ) as answer,
    ):
        assert answer.status_code == 200
        for line in answer.iter_lines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            chunks.append(json.loads(line.removeprefix("data: ")))
            content += chunks[-1]["choices"][0]["delta"].get("content") or ""
            if marker is not None and content == after:
                marker.write_text("1")
    return chunks


def _contents(chunks: list[dict]) -> list[str]:
    """Return the non-empty content deltas of a stream."""
    return [chunk["choices"][0]["delta"]["content"] for chunk in chunks if chunk["choices"][0]["delta"].get("content")]


def _texts(text: str, size: int) -> list[dict]:
    """Return claude text-delta events that write ``text`` in pieces of ``size`` characters."""
    return [claude_delta("text", text[i : i + size]) for i in range(0, len(text), size)]


def test_local_route_with_tools_streams_a_plain_answer_while_the_cli_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Send a tools request's plain answer in more than one delta before claude exits."""
    marker = tmp_path / "seen"
    events = [claude_delta("thinking", "Planning"), claude_delta("text", "po")]
    behavior = waiting_for(marker, *events) + printing(claude_delta("text", "ng")) + "\n" + printing(CLAUDE_SUCCESS)
    chunks = _live_stream(_stream_route(tmp_path, monkeypatch, behavior), marker, "po")
    assert chunks[0]["choices"][0]["delta"] == {"reasoning_content": "Planning"}
    assert _contents(chunks)[:2] == ["po", "ng"]
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.parametrize("size", [1, 3])
@pytest.mark.parametrize(
    "block",
    [
        '```json\n{"tool_calls": [{"name": "search", "args": {"q": "x"}}]}\n```\n\n[[ ## completed ## ]]',
        '{ "tool_calls": [{"name": "search", "args": {"q": "x"}}]}',
    ],
)
def test_local_route_streams_prose_but_never_a_written_tool_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, block: str, size: int
) -> None:
    """Stream the prose live, hold the tool-call block, and end with native ``tool_calls``."""
    marker = tmp_path / "seen"
    prose = "Calling it now.\n"
    text = prose + block
    behavior = waiting_for(marker, claude_delta("thinking", "Planning"), *_texts(prose, size))
    behavior += "\n".join(printing(event) for event in [*_texts(block, size), {**CLAUDE_SUCCESS, "result": text}])
    chunks = _live_stream(_stream_route(tmp_path, monkeypatch, behavior), marker, prose)
    contents = _contents(chunks)
    assert "".join(contents) == prose
    assert not any(mark in piece for piece in contents for mark in ("`", "{", "tool_calls"))
    [call] = next(
        chunk["choices"][0]["delta"]["tool_calls"] for chunk in chunks if "tool_calls" in chunk["choices"][0]["delta"]
    )
    assert call["function"]["name"] == "search"
    assert json.loads(call["function"]["arguments"]) == {"q": "x"}
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert len({chunk["id"] for chunk in chunks}) == 1


def test_local_route_without_tools_streams_a_written_tool_call_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stream DSPy's text-protocol tool call while claude runs when the request declares no tools."""
    marker = tmp_path / "seen"
    head = '[[ ## tool_calls ## ]]\n{"tool_calls": [{"name": "search"'
    tail = ', "args": {"q": "x"}}]}\n\n[[ ## completed ## ]]'
    behavior = waiting_for(marker, *_texts(head, 4))
    behavior += "\n".join(printing(event) for event in [*_texts(tail, 4), {**CLAUDE_SUCCESS, "result": head + tail}])
    chunks = _live_stream(_stream_route(tmp_path, monkeypatch, behavior), marker, head, request=REQUEST)
    assert "".join(_contents(chunks)) == head + tail
    assert not any("tool_calls" in chunk["choices"][0]["delta"] for chunk in chunks)
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.parametrize(
    "text",
    [
        'Use {x} or {"answer": 1}, then `code` and ```python\nx = {1: 2}\n``` done.',
        '{"tool_calls": [{"name": "delete_everything", "args": {}}]}',
        'Try {"tool_calls": [{"name": "search", "args": "not json"}]} later.',
    ],
)
def test_local_route_sends_held_text_that_is_no_tool_call_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    """Send braces, fences and invalid tool calls as content exactly once, finishing with ``stop``."""
    events = [*_texts(text, 1), {**CLAUDE_SUCCESS, "result": text}]
    chunks = _live_stream(_stream_route(tmp_path, monkeypatch, "\n".join(printing(event) for event in events)))
    assert "".join(_contents(chunks)) == text
    assert not any("tool_calls" in chunk["choices"][0]["delta"] for chunk in chunks)
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"

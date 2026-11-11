"""Tests for the Kimi Code harness: registry entry, gateway-only launch, effort names and output parsing."""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

from core.exceptions import ServiceError
from core.models.blackbox import BLACKBOX_HARNESSES, BLACKBOX_TARGET_AGENT, BlackboxTarget

from .. import repo_tree
from ..harness import GatewayConfig, build_launch, launch_payload, pinned_harness_check
from ..harness_bridge import parse_output, parse_pi_output, tool_call_ids, workspace_files
from ..harnesses import REGISTRY
from ..harnesses.kimi_code import (
    KIMI_CODE_BASE_URL_FILE,
    KIMI_CODE_EFFORTS,
    KIMI_CODE_ID,
    KIMI_CODE_SHA256,
    KIMI_CODE_TO_PI,
    KIMI_CODE_VERSION,
)

_GATEWAY = GatewayConfig(url="https://gw.example/v1", api_key="gateway-token")
_RELAY = "http://127.0.0.1:3210/v1"
_BLANKED = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "VERTEXAI_API_KEY",
    "OPENROUTER_API_KEY",
    "KIMI_API_KEY",
    "KIMI_BASE_URL",
    "KIMI_CODE_BASE_URL",
    "KIMI_REGISTRY_API_KEY",
    "KIMI_WEB_SEARCH_API_KEY",
    "KIMI_WEB_SEARCH_BASE_URL",
    "KIMI_WEB_FETCH_API_KEY",
    "KIMI_WEB_FETCH_BASE_URL",
)
# Every ``KIMI_MODEL_*`` name that Kimi Code 2.5.3 reads.
_MODEL_ENV = (
    "KIMI_MODEL_ADAPTIVE_THINKING",
    "KIMI_MODEL_API_KEY",
    "KIMI_MODEL_BASE_URL",
    "KIMI_MODEL_CAPABILITIES",
    "KIMI_MODEL_DISPLAY_NAME",
    "KIMI_MODEL_MAX_COMPLETION_TOKENS",
    "KIMI_MODEL_MAX_CONTEXT_SIZE",
    "KIMI_MODEL_MAX_OUTPUT_SIZE",
    "KIMI_MODEL_MAX_TOKENS",
    "KIMI_MODEL_NAME",
    "KIMI_MODEL_OUTPUT_FORMAT",
    "KIMI_MODEL_PROVIDER_TYPE",
    "KIMI_MODEL_REASONING_KEY",
    "KIMI_MODEL_TEMPERATURE",
    "KIMI_MODEL_THINKING_EFFORT",
    "KIMI_MODEL_THINKING_KEEP",
    "KIMI_MODEL_TOP_P",
)
# Stand-in for Kimi Code 2.5.3: it records its call, env and
# ``config.toml``, writes one ``wire.jsonl`` for the main agent (three model
# steps with tool calls and a two-part final answer, then a Stop hook result
# message) and one for a subagent under ``KIMI_CODE_HOME``, prints stream-json
# lines with a hook result line after the answer as Kimi Code does, makes one
# code change when the workspace has ``main.py``, and exits with FAKE_STATUS.
_FAKE_KIMI = (
    """#!/bin/sh
{
  printf 'ARGS'; for a in "$@"; do printf ' [%s]' "$a"; done; printf '\\n'
  for v in """
    + " ".join(_MODEL_ENV)
    + """ \\
    KIMI_CODE_HOME KIMI_DISABLE_TELEMETRY KIMI_CODE_NO_AUTO_UPDATE OPENAI_API_KEY; do
    eval "printf '%s=%s\\n' $v \\"\\${$v-<unset>}\\""
  done
  printf 'CONFIG:'; cat "$KIMI_CODE_HOME/config.toml" 2>/dev/null || printf ' none\\n'
} >> "$FAKE_LOG"
if [ -f main.py ]; then echo 'print(2)' > main.py; fi
s="$KIMI_CODE_HOME/sessions/ws1/sess1/agents"
mkdir -p "$s/main" "$s/sub1"
printf '%s\\n' \\
  '{"type":"turn.prompt","turnId":0}' \\
  '{"type":"context.append_loop_event","agentId":"main","event":{"type":"step.begin","uuid":"s1"}}' \\
  '{"type":"context.append_loop_event","agentId":"main","event":{"type":"content.part","stepUuid":"s1","part":{"type":"think","think":"plan"}}}' \\
  '{"type":"context.append_loop_event","agentId":"main","event":{"type":"content.part","stepUuid":"s1","part":{"type":"text","text":"Let me look."}}}' \\
  '{"type":"context.append_loop_event","agentId":"main","event":{"type":"tool.call","stepUuid":"s1","toolCallId":"call_1","name":"Read"}}' \\
  '{"type":"usage.record","agentId":"main","model":"m","usage":{"inputOther":100,"output":20,"inputCacheRead":10,"inputCacheCreation":5}}' \\
  'not json' \\
  '{"type":"context.append_loop_event","agentId":"main","event":{"type":"step.begin","uuid":"s2"}}' \\
  '{"type":"context.append_loop_event","agentId":"main","event":{"type":"tool.call","stepUuid":"s2","toolCallId":"call_2","name":"Edit"}}' \\
  '{"type":"context.append_loop_event","agentId":"main","event":{"type":"step.begin","uuid":"s3"}}' \\
  '{"type":"context.append_loop_event","agentId":"main","event":{"type":"content.part","stepUuid":"s3","part":{"type":"text","text":"I fixed main.py.\\n\\n"}}}' \\
  '{"type":"context.append_loop_event","agentId":"main","event":{"type":"content.part","stepUuid":"s3","part":{"type":"text","text":"The answer is 42."}}}' \\
  '{"type":"usage.record","agentId":"main","model":"m","usage":{"inputOther":40,"output":7}}' \\
  '{"type":"context.append_message","agentId":"main","message":{"role":"assistant","content":[{"type":"text","text":"A Stop hook failed to execute."}],"origin":{"kind":"hook_result","event":"Stop"}}}' \\
  > "$s/main/wire.jsonl"
printf '%s\\n' \\
  '{"type":"context.append_loop_event","agentId":"sub1","event":{"type":"content.part","stepUuid":"x1","part":{"type":"text","text":"Subagent text."}}}' \\
  '{"type":"context.append_loop_event","agentId":"sub1","event":{"type":"tool.call","stepUuid":"x1","toolCallId":"sub_call","name":"Grep"}}' \\
  '{"type":"usage.record","agentId":"sub1","model":"m","usage":{"inputOther":3,"output":2}}' > "$s/sub1/wire.jsonl"
printf '%s\\n' \\
  '{"role":"assistant","content":"I fixed main.py.\\n\\nThe answer is 42."}' \\
  '{"role":"assistant","content":"Stop hook\\n\\nAll checks passed."}'
exit "${FAKE_STATUS:-0}"
"""
)
_ANSWER = "I fixed main.py.\n\nThe answer is 42."
_USAGE = {"input_tokens": 158, "output_tokens": 29}


def _target() -> BlackboxTarget:
    """Build a Kimi Code agent target.

    Returns:
        The target.
    """
    return BlackboxTarget(kind=BLACKBOX_TARGET_AGENT, harness=KIMI_CODE_ID, model="target-model")


def _run_with_fake_kimi(
    tmp_path: Path,
    *,
    protected: bool,
    reasoning_effort: str | None = None,
    status: int = 0,
    workspace: Path | None = None,
    parser_fails: bool = False,
    inherited: dict[str, str] | None = None,
    prompt: str | None = "Fix `main.py` and say $(echo done).",
) -> tuple[subprocess.CompletedProcess[str], str, Path]:
    """Run the built Kimi Code command in a scratch workspace against a fake ``kimi`` binary.

    Args:
        tmp_path: Scratch directory for the workspace, the fake binary and its log.
        protected: Build the protected launch, with the sandbox-local relay set.
        reasoning_effort: The user's effort for the model.
        status: Exit status of the fake.
        workspace: An existing checkout to run in; a fresh folder when ``None``.
        parser_fails: Put a ``python3`` on PATH that fails, so the usage parse step fails.
        inherited: Sandbox env that the launch env overrides, as the bridge applies it.
        prompt: The task prompt the bridge writes, or ``None`` to leave the prompt file missing.

    Returns:
        The finished process, the fake binary's call log, and the workspace.
    """
    launch = build_launch(_target(), _GATEWAY, protected=protected, reasoning_effort=reasoning_effort)
    if workspace is None:
        workspace = tmp_path / "work"
    else:
        repo_tree.exclude_paths(workspace, workspace_files(launch_payload(launch)))
    for name, content in launch.files.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (workspace / "task").mkdir(parents=True)
    if prompt is not None:
        (workspace / "task" / "PROMPT.md").write_text(prompt, encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "kimi"
    fake.write_text(_FAKE_KIMI)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    if parser_fails:
        broken = bin_dir / "python3"
        broken.write_text("#!/bin/sh\nexit 2\n")
        broken.chmod(broken.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    log.touch()
    env = {
        **(inherited or {}),
        **launch.env,
        "PATH": f"{bin_dir}{os.pathsep}{os.defpath}",
        "HOME": str(tmp_path / "home"),
        "FAKE_LOG": str(log),
        "FAKE_STATUS": str(status),
    }
    if protected:
        env["APP_BUDGET_RELAY_URL"] = _RELAY
    result = subprocess.run(
        launch.run_command, shell=True, cwd=workspace, env=env, capture_output=True, text=True, timeout=10, check=False
    )
    return result, log.read_text(encoding="utf-8"), workspace


def test_kimi_code_is_registered_before_code_puppy_with_its_pin() -> None:
    """List Kimi Code in the registry and the request model, pinned to the exact npm release."""
    spec = REGISTRY[KIMI_CODE_ID]
    assert (spec.binary, spec.version, spec.output_format) == ("kimi", "2.5.3", "pi")
    assert spec.supported_efforts == ("low", "medium", "high", "xhigh", "max")
    assert BLACKBOX_HARNESSES[-3:] == (KIMI_CODE_ID, "code_puppy", "custom")
    assert list(REGISTRY)[-2:] == [KIMI_CODE_ID, "code_puppy"]
    assert pinned_harness_check(KIMI_CODE_ID) == "kimi --version 2>/dev/null | grep -Eq '(^| )2[.]5[.]3( |$)'"


def test_open_launch_installs_the_checked_tarball_and_sends_model_calls_only_to_the_gateway() -> None:
    """Install the pinned tarball after its SHA-256 check, use the gateway token, and blank every other key."""
    launch = build_launch(_target(), _GATEWAY)

    assert launch.instructions_file == "AGENTS.md"
    assert launch.install_command is not None
    assert launch.install_command.startswith(pinned_harness_check(KIMI_CODE_ID) + " || {")
    assert (
        f"https://registry.npmjs.org/@moonshot-ai/kimi-code/-/kimi-code-{KIMI_CODE_VERSION}.tgz"
        in launch.install_command
    )
    assert f'echo "{KIMI_CODE_SHA256}  $d/kimi-code-2.5.3.tgz" | sha256sum -c -' in launch.install_command
    assert 'npm install -g --ignore-scripts "$d/kimi-code-2.5.3.tgz"' in launch.install_command
    assert launch.files == {KIMI_CODE_BASE_URL_FILE: _GATEWAY.url}
    assert _GATEWAY.url not in launch.run_command
    assert _GATEWAY.api_key not in launch.run_command
    assert 'kimi --prompt="$_kc_prompt" --output-format stream-json < /dev/null > /dev/null' in launch.run_command
    assert "config.toml" not in launch.run_command
    assert launch.env["KIMI_MODEL_NAME"] == "target-model"
    assert launch.env["KIMI_MODEL_API_KEY"] == _GATEWAY.api_key
    assert launch.env["KIMI_MODEL_PROVIDER_TYPE"] == "openai"
    assert launch.env["KIMI_MODEL_BASE_URL"] == ""
    assert {name: launch.env[name] for name in _BLANKED} == dict.fromkeys(_BLANKED, "")
    assert launch.env["KIMI_DISABLE_TELEMETRY"] == "true"
    assert launch.env["KIMI_CODE_NO_AUTO_UPDATE"] == "true"
    assert launch.env["KIMI_MODEL_THINKING_EFFORT"] == ""
    assert launch.parse_output is parse_pi_output
    assert launch_payload(launch)["output_format"] == "pi"


def test_protected_launch_checks_the_pin_offline() -> None:
    """Use the offline version probe in a protected run, and keep the other keys blank."""
    launch = build_launch(_target(), _GATEWAY, protected=True)

    assert launch.install_command == pinned_harness_check(KIMI_CODE_ID)
    assert {name: launch.env[name] for name in _BLANKED} == dict.fromkeys(_BLANKED, "")
    assert launch.env["KIMI_MODEL_API_KEY"] == _GATEWAY.api_key


@pytest.mark.parametrize("effort", KIMI_CODE_EFFORTS)
def test_supported_effort_goes_to_kimi_code_config_unchanged(tmp_path: Path, effort: str) -> None:
    """Write each effort name unchanged as ``[thinking] effort``, which an ``openai`` provider sends as reasoning_effort.

    ``KIMI_MODEL_THINKING_EFFORT`` stays blank: it sets ``thinking.forcedEffort``,
    which Kimi Code 2.5.3 ignores for an ``openai`` provider.
    """
    assert build_launch(_target(), _GATEWAY, reasoning_effort=effort).env["KIMI_MODEL_THINKING_EFFORT"] == ""
    result, calls, _ = _run_with_fake_kimi(tmp_path, protected=False, reasoning_effort=effort)

    assert result.returncode == 0, result.stderr
    assert f'CONFIG:[thinking]\neffort = "{effort}"\n' in calls
    assert "KIMI_MODEL_THINKING_EFFORT=\n" in calls


@pytest.mark.parametrize("effort", [None, ""])
def test_no_effort_keeps_the_kimi_code_default(tmp_path: Path, effort: str | None) -> None:
    """Write no ``config.toml`` and keep ``KIMI_MODEL_THINKING_EFFORT`` blank when the user picks no effort."""
    result, calls, _ = _run_with_fake_kimi(tmp_path, protected=False, reasoning_effort=effort)

    assert result.returncode == 0, result.stderr
    assert "CONFIG: none\n" in calls
    assert "KIMI_MODEL_THINKING_EFFORT=\n" in calls


def test_inherited_model_env_cannot_change_the_model(tmp_path: Path) -> None:
    """Set every ``KIMI_MODEL_*`` name Kimi Code reads, so sandbox values for them never reach Kimi Code."""
    assert set(_MODEL_ENV) <= set(build_launch(_target(), _GATEWAY).env)
    inherited = dict.fromkeys(_MODEL_ENV, "inherited")
    inherited["OPENAI_API_KEY"] = "inherited"

    result, calls, _ = _run_with_fake_kimi(tmp_path, protected=False, reasoning_effort="high", inherited=inherited)

    assert result.returncode == 0, result.stderr
    assert "inherited" not in calls
    assert "KIMI_MODEL_THINKING_EFFORT=\n" in calls
    assert 'CONFIG:[thinking]\neffort = "high"\n' in calls
    assert "KIMI_MODEL_CAPABILITIES=\n" in calls
    assert "KIMI_MODEL_MAX_CONTEXT_SIZE=\n" in calls
    assert f"KIMI_MODEL_BASE_URL={_GATEWAY.url}\n" in calls


@pytest.mark.parametrize("effort", ["none", "minimal", "off", "HIGH", "extreme"])
def test_unsupported_effort_is_refused_at_launch_build(effort: str) -> None:
    """Refuse an effort that Kimi Code does not name, instead of a silent drop or remap."""
    with pytest.raises(ServiceError, match=f"does not support the effort '{effort}'"):
        build_launch(_target(), _GATEWAY, reasoning_effort=effort)


@pytest.mark.parametrize(("protected", "base_url"), [(False, _GATEWAY.url), (True, _RELAY)])
def test_run_points_kimi_code_at_the_gateway_and_returns_answer_usage_and_tool_calls(
    tmp_path: Path, protected: bool, base_url: str
) -> None:
    """Send the gateway (or relay) URL and token, and print the answer, summed usage and tool calls as Pi lines."""
    result, calls, workspace = _run_with_fake_kimi(tmp_path, protected=protected)

    assert result.returncode == 0, result.stderr
    assert "ARGS [--prompt=Fix `main.py` and say $(echo done).] [--output-format] [stream-json]\n" in calls
    assert f"KIMI_MODEL_BASE_URL={base_url}\n" in calls
    assert "KIMI_MODEL_API_KEY=gateway-token\n" in calls
    assert "KIMI_MODEL_NAME=target-model\n" in calls
    assert "KIMI_MODEL_PROVIDER_TYPE=openai\n" in calls
    assert "OPENAI_API_KEY=\n" in calls
    (home,) = [line.split("=", 1)[1] for line in calls.splitlines() if line.startswith("KIMI_CODE_HOME=")]
    assert not Path(home).resolve().is_relative_to(workspace.resolve())
    assert not Path(home).exists(), "the run removes its temporary Kimi Code home"
    assert "Let me look." not in result.stdout
    assert "Stop hook" not in result.stdout
    assert parse_output("pi", result.stdout) == (_ANSWER, _USAGE)
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert events[0]["type"] == "message_end"
    assert [call for event in events for call in tool_call_ids("pi", event)] == [
        "ws1/sess1/agents/main:call_1",
        "ws1/sess1/agents/main:call_2",
        "ws1/sess1/agents/sub1:sub_call",
    ]


def test_run_keeps_the_kimi_code_exit_status(tmp_path: Path) -> None:
    """Fail the run when Kimi Code fails, and still report the usage it spent."""
    result, _, _ = _run_with_fake_kimi(tmp_path, protected=False, status=3)

    assert result.returncode == 3
    assert parse_pi_output(result.stdout)[1] == _USAGE


@pytest.mark.parametrize(("status", "expected"), [(3, 3), (0, 1)])
def test_failed_parse_keeps_a_kimi_code_failure_and_fails_a_success(tmp_path: Path, status: int, expected: int) -> None:
    """Keep Kimi Code's nonzero status when the parse step fails, and exit 1 when Kimi Code succeeded."""
    result, _, _ = _run_with_fake_kimi(tmp_path, protected=False, status=status, parser_fails=True)

    assert result.returncode == expected


@pytest.mark.parametrize(
    "prompt",
    [
        "/goal ship it",
        "  /goal ship it",
        "/goal",
        "﻿/goal ship it",
        " /goal\tship it",
        "\n/goal\nship it",
        "\u3000/goal\u2009ship it",
        "\u2028/goal\u2029",
    ],
)
def test_goal_command_prompt_is_framed_so_it_reaches_the_model(tmp_path: Path, prompt: str) -> None:
    """Frame a prompt that Kimi Code's headless ``/goal`` parser would take, so the model gets it as a task."""
    result, calls, _ = _run_with_fake_kimi(tmp_path, protected=False, prompt=prompt)

    assert result.returncode == 0, result.stderr
    assert f"ARGS [--prompt=Task request:\n{prompt}] [--output-format] [stream-json]\n" in calls


@pytest.mark.parametrize(
    "prompt",
    # Python's ``\\s`` matches U+001C and U+0085, but JavaScript's ``trim()`` keeps them.
    [
        "/goals ship it",
        "/help",
        "Use /goal later.",
        "!ls",
        "-h",
        "--output-format=text",
        "-- Fix it.",
        "Fix it.\n\n",
        "x\n",
        "\x1c/goal ship it",
        "\x85/goal",
    ],
)
def test_other_prompts_reach_kimi_code_unchanged(tmp_path: Path, prompt: str) -> None:
    """Leave every prompt that the ``/goal`` parser does not take byte-identical."""
    result, calls, _ = _run_with_fake_kimi(tmp_path, protected=False, prompt=prompt)

    assert result.returncode == 0, result.stderr
    assert f"ARGS [--prompt={prompt}] [--output-format] [stream-json]\n" in calls


def test_unreadable_prompt_file_stops_the_run_before_kimi_code(tmp_path: Path) -> None:
    """Stop the run with a clear error when the prompt file cannot be read, so Kimi Code never gets a blank task."""
    result, calls, _ = _run_with_fake_kimi(tmp_path, protected=False, prompt=None)

    assert result.returncode == 1
    assert "kimi_code: cannot read the task prompt file" in result.stderr
    assert "ARGS" not in calls


def _loop_event(event: dict[str, object]) -> dict[str, object]:
    """Wrap a loop event as a Kimi Code 2.5.3 ``wire.jsonl`` record.

    Args:
        event: The loop event.

    Returns:
        The journal record.
    """
    return {"type": "context.append_loop_event", "agentId": "main", "event": event}


def _text(step: str, text: str) -> dict[str, object]:
    """Build a journal record for one text part of a model step.

    Args:
        step: The step uuid.
        text: The text.

    Returns:
        The journal record.
    """
    return _loop_event({"type": "content.part", "stepUuid": step, "part": {"type": "text", "text": text}})


def _parse_main_journal(tmp_path: Path, records: list[dict[str, object]]) -> str | None:
    """Write a main agent journal and return the answer :data:`KIMI_CODE_TO_PI` reads from it.

    Args:
        tmp_path: Test folder.
        records: The journal records.

    Returns:
        The answer the Pi parser reads.
    """
    sessions = tmp_path / "sessions"
    journal = sessions / "ws1" / "sess1" / "agents" / "main" / "wire.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_text("".join(json.dumps(record) + "\n" for record in records))
    result = subprocess.run(
        ["python3", "-I", "-c", KIMI_CODE_TO_PI, str(sessions)],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return parse_pi_output(result.stdout)[0]


@pytest.mark.parametrize(
    ("last", "expected"),
    [
        ([_text("s2", "  One. "), _text("s2", "Two.  ")], "One. Two."),
        (
            [_loop_event({"type": "content.part", "stepUuid": "s2", "part": {"type": "think", "think": "x"}})],
            "Earlier.",
        ),
        ([_loop_event({"type": "tool.call", "stepUuid": "s2", "toolCallId": "c1", "name": "Read"})], "Earlier."),
        ([_text("s2", "   ")], "Earlier."),
    ],
)
def test_answer_is_the_text_of_the_last_model_step_with_text(
    tmp_path: Path, last: list[dict[str, object]], expected: str
) -> None:
    """Join the text parts of the last model step that has text, and keep an earlier answer otherwise."""
    assert _parse_main_journal(tmp_path, [_text("s1", "Earlier."), *last]) == expected


def test_hook_results_never_replace_the_answer(tmp_path: Path) -> None:
    """Read the answer only from model step text, so a hook result message after it never replaces it."""
    hook = {
        "type": "context.append_message",
        "agentId": "main",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "Stop hook\n\nAll checks passed."}],
            "origin": {"kind": "hook_result", "event": "Stop", "blocked": True},
        },
    }

    assert _parse_main_journal(tmp_path, [_text("s1", "The answer."), hook]) == "The answer."


def test_tool_calls_count_every_agent_like_the_token_usage(tmp_path: Path) -> None:
    """Count the tool calls of every agent journal, once each, and keep a call id that two agents share."""
    sessions = tmp_path / "sessions"
    agents = sessions / "ws1" / "sess1" / "agents"
    call = {"type": "tool.call", "stepUuid": "s1", "toolCallId": "call_1", "name": "Read"}
    for agent, records in {
        "main": [_loop_event(call), _loop_event(call), _text("s1", "Done.")],
        "sub1": [{"type": "context.append_loop_event", "agentId": "sub1", "event": call}],
    }.items():
        (agents / agent).mkdir(parents=True)
        (agents / agent / "wire.jsonl").write_text("".join(json.dumps(record) + "\n" for record in records))
    result = subprocess.run(
        ["python3", "-I", "-c", KIMI_CODE_TO_PI, str(sessions)],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )

    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert parse_pi_output(result.stdout)[0] == "Done."
    assert len({call for event in events for call in tool_call_ids("pi", event)}) == 2


def test_no_journal_gives_no_answer(tmp_path: Path) -> None:
    """Print an empty answer with zero usage when Kimi Code wrote no journal."""
    result = subprocess.run(
        ["python3", "-I", "-c", KIMI_CODE_TO_PI, str(tmp_path / "sessions")],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )

    assert parse_pi_output(result.stdout) == (None, {"input_tokens": 0, "output_tokens": 0})


def _shipped_tree(tmp_path: Path) -> Path:
    """Unpack a one-file shipped tree the way a repo-mode run does.

    Args:
        tmp_path: Test folder.

    Returns:
        The checkout.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tree:
        data = b"print(1)\n"
        info = tarfile.TarInfo("main.py")
        info.size = len(data)
        tree.addfile(info, io.BytesIO(data))
    chunk = tmp_path / "chunk0"
    chunk.write_text(base64.b64encode(buffer.getvalue()).decode("ascii"))
    return repo_tree.unpack_tree([chunk], tmp_path / "repo")


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_version_patch_holds_only_the_code_change(tmp_path: Path) -> None:
    """Kimi Code's session journals and stream file never join a repo-mode version patch."""
    checkout = _shipped_tree(tmp_path)

    result, _, _ = _run_with_fake_kimi(tmp_path, protected=True, workspace=checkout)
    assert result.returncode == 0, result.stderr
    # The bridge writes and removes its own prompt file; only Kimi Code's files are under test.
    shutil.rmtree(checkout / "task")

    patch = repo_tree.version_patch(checkout)
    assert [line for line in patch.splitlines() if line.startswith("diff --git")] == ["diff --git a/main.py b/main.py"]
    assert "+print(2)" in patch

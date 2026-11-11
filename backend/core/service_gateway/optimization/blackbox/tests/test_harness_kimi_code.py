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
# Stand-in for Kimi Code 2.5.3: it records its call and env, prints the
# stream-json lines on stdout (two assistant steps with tool calls, a tool
# result, a meta line, then the final answer), writes one ``wire.jsonl`` for
# the main agent and one for a subagent under ``KIMI_CODE_HOME``, makes one
# code change when the workspace has ``main.py``, and exits with FAKE_STATUS.
_FAKE_KIMI = """#!/bin/sh
{
  printf 'ARGS'; for a in "$@"; do printf ' [%s]' "$a"; done; printf '\\n'
  for v in KIMI_MODEL_NAME KIMI_MODEL_API_KEY KIMI_MODEL_BASE_URL KIMI_MODEL_PROVIDER_TYPE \\
    KIMI_MODEL_THINKING_EFFORT KIMI_CODE_HOME KIMI_DISABLE_TELEMETRY KIMI_CODE_NO_AUTO_UPDATE OPENAI_API_KEY; do
    eval "printf '%s=%s\\n' $v \\"\\${$v-<unset>}\\""
  done
} >> "$FAKE_LOG"
if [ -f main.py ]; then echo 'print(2)' > main.py; fi
s="$KIMI_CODE_HOME/sessions/ws1/sess1/agents"
mkdir -p "$s/main" "$s/sub1"
printf '%s\\n' \\
  '{"type":"usage.record","agentId":"main","model":"m","usage":{"inputOther":100,"output":20,"inputCacheRead":10,"inputCacheCreation":5}}' \\
  '{"type":"turn.begin"}' \\
  'not json' \\
  '{"type":"usage.record","agentId":"main","model":"m","usage":{"inputOther":40,"output":7}}' > "$s/main/wire.jsonl"
printf '%s\\n' '{"type":"usage.record","agentId":"sub1","model":"m","usage":{"inputOther":3,"output":2}}' > "$s/sub1/wire.jsonl"
printf '%s\\n' \\
  '{"role":"meta","type":"system.version","content":"2.5.3"}' \\
  '{"role":"assistant","content":"Let me look.","tool_calls":[{"type":"function","id":"call_1","function":{"name":"Read","arguments":"{}"}}]}' \\
  '{"role":"tool","tool_call_id":"call_1","content":"print(1)"}' \\
  '{"role":"assistant","tool_calls":[{"type":"function","id":"call_2","function":{"name":"Edit","arguments":"{}"}}]}' \\
  '{"role":"tool","tool_call_id":"call_2","content":"ok"}' \\
  '{"role":"assistant","content":"I fixed main.py.\\n\\nThe answer is 42."}'
exit "${FAKE_STATUS:-0}"
"""
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
) -> tuple[subprocess.CompletedProcess[str], str, Path]:
    """Run the built Kimi Code command in a scratch workspace against a fake ``kimi`` binary.

    Args:
        tmp_path: Scratch directory for the workspace, the fake binary and its log.
        protected: Build the protected launch, with the sandbox-local relay set.
        reasoning_effort: The user's effort for the model.
        status: Exit status of the fake.
        workspace: An existing checkout to run in; a fresh folder when ``None``.

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
    (workspace / "task" / "PROMPT.md").write_text("Fix `main.py` and say $(echo done).")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "kimi"
    fake.write_text(_FAKE_KIMI)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    log.touch()
    env = {
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
    return result, log.read_text(), workspace


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
    assert 'kimi --prompt "$(cat "$APP_PROMPT_FILE")" --output-format stream-json < /dev/null' in launch.run_command
    assert launch.env["KIMI_MODEL_NAME"] == "target-model"
    assert launch.env["KIMI_MODEL_API_KEY"] == _GATEWAY.api_key
    assert launch.env["KIMI_MODEL_PROVIDER_TYPE"] == "openai"
    assert "KIMI_MODEL_BASE_URL" not in launch.env
    assert {name: launch.env[name] for name in _BLANKED} == dict.fromkeys(_BLANKED, "")
    assert launch.env["KIMI_DISABLE_TELEMETRY"] == "true"
    assert launch.env["KIMI_CODE_NO_AUTO_UPDATE"] == "true"
    assert "KIMI_MODEL_THINKING_EFFORT" not in launch.env
    assert launch.parse_output is parse_pi_output
    assert launch_payload(launch)["output_format"] == "pi"


def test_protected_launch_checks_the_pin_offline() -> None:
    """Use the offline version probe in a protected run, and keep the other keys blank."""
    launch = build_launch(_target(), _GATEWAY, protected=True)

    assert launch.install_command == pinned_harness_check(KIMI_CODE_ID)
    assert {name: launch.env[name] for name in _BLANKED} == dict.fromkeys(_BLANKED, "")
    assert launch.env["KIMI_MODEL_API_KEY"] == _GATEWAY.api_key


@pytest.mark.parametrize("effort", KIMI_CODE_EFFORTS)
def test_supported_effort_goes_to_kimi_code_unchanged(tmp_path: Path, effort: str) -> None:
    """Pass each Kimi Code effort name unchanged as ``KIMI_MODEL_THINKING_EFFORT``."""
    assert build_launch(_target(), _GATEWAY, reasoning_effort=effort).env["KIMI_MODEL_THINKING_EFFORT"] == effort
    result, calls, _ = _run_with_fake_kimi(tmp_path, protected=False, reasoning_effort=effort)

    assert result.returncode == 0, result.stderr
    assert f"KIMI_MODEL_THINKING_EFFORT={effort}\n" in calls


def test_no_effort_keeps_the_kimi_code_default(tmp_path: Path) -> None:
    """Leave ``KIMI_MODEL_THINKING_EFFORT`` unset when the user picks no effort."""
    result, calls, _ = _run_with_fake_kimi(tmp_path, protected=False)

    assert result.returncode == 0, result.stderr
    assert "KIMI_MODEL_THINKING_EFFORT=<unset>\n" in calls


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
    assert "ARGS [--prompt] [Fix `main.py` and say $(echo done).] [--output-format] [stream-json]\n" in calls
    assert f"KIMI_MODEL_BASE_URL={base_url}\n" in calls
    assert "KIMI_MODEL_API_KEY=gateway-token\n" in calls
    assert "KIMI_MODEL_NAME=target-model\n" in calls
    assert "KIMI_MODEL_PROVIDER_TYPE=openai\n" in calls
    assert "OPENAI_API_KEY=\n" in calls
    (home,) = [line.split("=", 1)[1] for line in calls.splitlines() if line.startswith("KIMI_CODE_HOME=")]
    assert not Path(home).resolve().is_relative_to(workspace.resolve())
    assert not Path(home).exists(), "the run removes its temporary Kimi Code home"
    assert "Let me look." not in result.stdout
    assert parse_output("pi", result.stdout) == (_ANSWER, _USAGE)
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert events[0]["type"] == "message_end"
    assert [call for event in events for call in tool_call_ids("pi", event)] == ["call_1", "call_2"]


def test_run_keeps_the_kimi_code_exit_status(tmp_path: Path) -> None:
    """Fail the run when Kimi Code fails, and still report the usage it spent."""
    result, _, _ = _run_with_fake_kimi(tmp_path, protected=False, status=3)

    assert result.returncode == 3
    assert parse_pi_output(result.stdout)[1] == _USAGE


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

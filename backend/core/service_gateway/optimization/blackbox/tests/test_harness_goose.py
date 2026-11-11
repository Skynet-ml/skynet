"""Tests for the Goose CLI harness: registry entry, pinned install, gateway-only launch, protected env and output parsing."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from core.models.blackbox import BLACKBOX_HARNESS_GOOSE, BLACKBOX_HARNESSES, BLACKBOX_TARGET_AGENT, BlackboxTarget

from ..harness import GatewayConfig, build_launch, launch_payload, pinned_harness_check
from ..harness_bridge import parse_output, parse_plain_output
from ..harnesses import REGISTRY
from ..harnesses.goose import GOOSE_BASE_URL_FILE, GOOSE_SHA256, GOOSE_VERSION

_GATEWAY = GatewayConfig(url="https://gw.example/v1", api_key="gateway-token")
_RELAY = "http://127.0.0.1:3210/v1"
_PROVIDER_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY", "XAI_API_KEY")
# Synthetic one-shot transcript in the shape of ``goose run --quiet`` output.
_RECORDED_STDOUT = "\nI read solution.py and fixed the off-by-one error.\n\nThe answer is 42.\n\n"
# Stand-in for the goose binary: it records each call and prints a fixed answer.
_FAKE_GOOSE = """#!/bin/sh
log="$FAKE_LOG"
{
  printf 'ARGS'; for a in "$@"; do printf ' [%s]' "$a"; done; printf '\\n'
  printf 'OPENAI_BASE_URL=%s\\n' "$OPENAI_BASE_URL"
  printf 'OPENAI_API_KEY=%s\\n' "$OPENAI_API_KEY"
  printf 'OPENAI_HOST=%s\\n' "${OPENAI_HOST-unset}"
  printf 'OPENAI_BASE_PATH=%s\\n' "${OPENAI_BASE_PATH-unset}"
  printf 'ANTHROPIC_API_KEY=%s\\n' "$ANTHROPIC_API_KEY"
  printf 'GOOSE_PATH_ROOT=%s\\n' "$GOOSE_PATH_ROOT"
} >> "$log"
printf '%s' "$FAKE_ANSWER"
"""


def _target() -> BlackboxTarget:
    """Build a goose agent target.

    Returns:
        The target.
    """
    return BlackboxTarget(kind=BLACKBOX_TARGET_AGENT, harness=BLACKBOX_HARNESS_GOOSE, model="target-model")


def _run_with_fake_goose(
    tmp_path: Path, *, protected: bool, gateway: GatewayConfig = _GATEWAY
) -> tuple[str, str, Path]:
    """Run the built goose command in a scratch workspace against a fake ``goose`` binary.

    The env also carries an inherited ``OPENAI_HOST`` and ``OPENAI_BASE_PATH``,
    which the run must replace so they cannot override the gateway URL.

    Args:
        tmp_path: Scratch directory for the workspace, the fake binary and its log.
        protected: Build the protected launch, with the sandbox-local relay set.
        gateway: Gateway URL and key for the launch.

    Returns:
        The command's stdout, the fake binary's call log, and the workspace.
    """
    launch = build_launch(_target(), gateway, protected=protected)
    workspace = tmp_path / "work"
    for name, content in launch.files.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (workspace / "task").mkdir(parents=True)
    (workspace / "task" / "PROMPT.md").write_text("Fix `solution.py` and say $(echo done).")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "goose"
    fake.write_text(_FAKE_GOOSE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    env = {
        **launch.env,
        "PATH": f"{bin_dir}{os.pathsep}{os.defpath}",
        "HOME": str(tmp_path / "home"),
        "OPENAI_HOST": "https://api.openai.com",
        "OPENAI_BASE_PATH": "v1/other",
        "OPENAI_BASE_URL": "https://api.openai.com/v1",
        "FAKE_LOG": str(log),
        "FAKE_ANSWER": _RECORDED_STDOUT,
    }
    if protected:
        env["APP_BUDGET_RELAY_URL"] = _RELAY
    result = subprocess.run(
        launch.run_command, shell=True, cwd=workspace, env=env, capture_output=True, text=True, timeout=10, check=True
    )
    return result.stdout, log.read_text(), workspace


def _logged(calls: str, name: str) -> str:
    """Return the value the fake binary logged for ``name``.

    Args:
        calls: The fake binary's call log.
        name: The logged variable name.

    Returns:
        The logged value.
    """
    return next(line for line in calls.splitlines() if line.startswith(f"{name}=")).split("=", 1)[1]


def test_goose_is_registered_before_cline_with_its_pin() -> None:
    """List goose in the registry and the request model, pinned to the exact GitHub release."""
    spec = REGISTRY[BLACKBOX_HARNESS_GOOSE]
    assert (spec.binary, spec.version, spec.output_format, spec.position) == ("goose", "1.57.0", "plain", 64)
    assert list(REGISTRY).index(BLACKBOX_HARNESS_GOOSE) == list(REGISTRY).index("cline") - 1
    assert BLACKBOX_HARNESS_GOOSE in BLACKBOX_HARNESSES
    assert (
        pinned_harness_check(BLACKBOX_HARNESS_GOOSE) == "goose --version 2>/dev/null | grep -Eq '(^| )1[.]57[.]0( |$)'"
    )


def test_open_install_downloads_the_pinned_release_and_checks_its_digest() -> None:
    """Fetch one pinned tarball, verify its sha256 before unpacking, and never pipe a script into a shell."""
    install = build_launch(_target(), _GATEWAY).install_command

    assert install is not None
    assert install.startswith(f'export PATH="$HOME/.local/bin":"$PATH"; {pinned_harness_check("goose")} || {{ ')
    assert f"https://github.com/aaif-goose/goose/releases/download/v{GOOSE_VERSION}/$f" in install
    assert 'f="goose-$a-unknown-linux-musl.tar.gz"' in install
    for arch, digest in GOOSE_SHA256.items():
        assert f"a={arch}; s={digest};;" in install
        assert len(digest) == 64
    assert install.index("sha256sum -c -") < install.index("tar -xzf")
    assert "| bash" not in install
    assert "| sh " not in install
    assert "download_cli.sh" not in install
    assert "sudo" not in install


def test_open_launch_sends_model_calls_only_to_the_gateway() -> None:
    """Read the gateway URL from a file, pass the gateway token, and keep provider keys out of the env."""
    launch = build_launch(_target(), _GATEWAY)

    assert launch.instructions_file == "AGENTS.md"
    assert launch.files == {GOOSE_BASE_URL_FILE: _GATEWAY.url}
    assert _GATEWAY.url not in launch.run_command
    assert _GATEWAY.api_key not in launch.run_command
    assert (
        f'OPENAI_HOST="$(cat {GOOSE_BASE_URL_FILE})" OPENAI_BASE_PATH=chat/completions OPENAI_API_KEY="$APP_API_KEY" '
    ) in launch.run_command
    assert (
        "goose run --no-session --quiet --no-profile --with-builtin developer "
        '--provider openai --model "$APP_MODEL" -i "$APP_PROMPT_FILE" < /dev/null'
    ) in launch.run_command
    assert launch.env["APP_API_KEY"] == _GATEWAY.api_key
    assert launch.env["APP_GATEWAY_URL"] == _GATEWAY.url
    assert launch.env["GOOSE_DISABLE_KEYRING"] == "1"
    assert launch.env["GOOSE_TELEMETRY_OFF"] == "1"
    assert launch.env["GOOSE_MODE"] == "auto"
    assert "GOOSE_THINKING_EFFORT" not in launch.env
    assert not set(_PROVIDER_KEYS) & set(launch.env)
    assert launch.parse_output is parse_plain_output


@pytest.mark.parametrize(
    ("effort", "level"),
    [(None, None), ("none", "none"), ("high", "high"), ("xhigh", "xhigh"), ("max", "max"), ("minimal", "low")],
)
def test_reasoning_effort_maps_to_the_thinking_env(effort: str | None, level: str | None) -> None:
    """Pass the user's effort as goose's ``GOOSE_THINKING_EFFORT``, or nothing for goose's default."""
    launch = build_launch(_target(), _GATEWAY, reasoning_effort=effort)
    assert launch.env.get("GOOSE_THINKING_EFFORT") == level


def test_unknown_reasoning_effort_sets_no_thinking_env() -> None:
    """Give goose no thinking setting for an effort name it does not know."""
    launch = build_launch(_target(), _GATEWAY, reasoning_effort="turbo")
    assert "GOOSE_THINKING_EFFORT" not in launch.env


def test_protected_launch_checks_the_pin_offline_and_blocks_other_providers_and_telemetry() -> None:
    """Use the offline version probe, blank every other provider key, force telemetry off, and keep the gateway key."""
    launch = build_launch(_target(), _GATEWAY, protected=True)

    assert launch.install_command == pinned_harness_check(BLACKBOX_HARNESS_GOOSE)
    for name in (
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "OPENROUTER_API_KEY",
        "DATABRICKS_TOKEN",
        "GROQ_API_KEY",
        "XAI_API_KEY",
        "AZURE_OPENAI_API_KEY",
    ):
        assert launch.env[name] == ""
    assert launch.env["GOOSE_TELEMETRY_OFF"] == "1"
    assert launch.env["GOOSE_TELEMETRY_ENABLED"] == "false"
    assert launch.env["DO_NOT_TRACK"] == "1"
    assert launch.env["APP_API_KEY"] == _GATEWAY.api_key
    assert "OPENAI_API_KEY" not in launch.env


def test_protected_run_points_goose_at_the_relay_and_returns_its_answer(tmp_path: Path) -> None:
    """Run the protected command against a fake goose: the relay URL, gateway token and answer come through."""
    stdout, calls, workspace = _run_with_fake_goose(tmp_path, protected=True)

    (run,) = [line for line in calls.splitlines() if line.startswith("ARGS")]
    assert run == (
        "ARGS [run] [--no-session] [--quiet] [--no-profile] [--with-builtin] [developer] "
        "[--provider] [openai] [--model] [target-model] [-i] [task/PROMPT.md]"
    )
    assert _logged(calls, "OPENAI_HOST") == _RELAY
    assert _logged(calls, "OPENAI_BASE_PATH") == "chat/completions"
    assert _GATEWAY.url not in calls
    assert _logged(calls, "OPENAI_API_KEY") == "gateway-token"
    assert _logged(calls, "ANTHROPIC_API_KEY") == ""
    root = Path(_logged(calls, "GOOSE_PATH_ROOT"))
    assert root.is_dir()
    assert not root.is_relative_to(workspace.resolve())
    assert not root.is_relative_to(workspace)
    shutil.rmtree(root)
    assert parse_output("plain", stdout) == (
        "I read solution.py and fixed the off-by-one error.\n\nThe answer is 42.",
        {},
    )


def test_open_run_reads_the_gateway_url_from_the_generated_file(tmp_path: Path) -> None:
    """Without a relay, point goose at the gateway URL the launch wrote."""
    _, calls, _ = _run_with_fake_goose(tmp_path, protected=False)

    assert _logged(calls, "OPENAI_HOST") == _GATEWAY.url
    shutil.rmtree(_logged(calls, "GOOSE_PATH_ROOT"))


def test_run_forces_chat_completions_on_the_full_gateway_url(tmp_path: Path) -> None:
    """Pass the whole gateway URL as ``OPENAI_HOST`` with the custom ``chat/completions`` base path.

    Goose 1.57.0 sends gpt-5*, gpt-6* and o* models to /responses when the base
    path is its default ``v1/chat/completions``
    (``OpenAiProvider::should_use_responses_api``), and Skynet's local CLI and
    BYOK routes reject /responses. Only a custom chat path forces chat
    completions. ``ApiClient::build_url`` appends that relative path to
    ``OPENAI_HOST``, so the calls go to ``<gateway>/chat/completions`` with the
    gateway's path prefix kept. An inherited ``OPENAI_HOST``, ``OPENAI_BASE_PATH``
    or ``OPENAI_BASE_URL`` must not change either value.
    """
    gateway = GatewayConfig(url="https://gw.example/relay/abc/v1", api_key="gateway-token")
    _, calls, _ = _run_with_fake_goose(tmp_path, protected=False, gateway=gateway)

    assert _logged(calls, "OPENAI_HOST") == gateway.url
    assert _logged(calls, "OPENAI_BASE_PATH") == "chat/completions"
    shutil.rmtree(_logged(calls, "GOOSE_PATH_ROOT"))


def test_recorded_output_parses_to_the_answer_and_the_payload_names_the_plain_parser() -> None:
    """Parse a recorded transcript to its stripped text, and serialize the launch with the bridge's plain parser."""
    assert parse_plain_output(_RECORDED_STDOUT) == (
        "I read solution.py and fixed the off-by-one error.\n\nThe answer is 42.",
        {},
    )
    assert parse_plain_output("  \n") == (None, {})
    assert launch_payload(build_launch(_target(), _GATEWAY))["output_format"] == "plain"

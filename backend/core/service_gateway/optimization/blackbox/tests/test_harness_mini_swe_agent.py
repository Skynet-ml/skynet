"""Tests for the mini-swe-agent harness: registry entry, pinned install, gateway-only launch, protected env and output parsing."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from core.models.blackbox import (
    BLACKBOX_HARNESS_MINI_SWE_AGENT,
    BLACKBOX_HARNESSES,
    BLACKBOX_TARGET_AGENT,
    BlackboxTarget,
)

from ..harness import GatewayConfig, build_launch, launch_payload, pinned_harness_check
from ..harness_bridge import parse_output, parse_plain_output
from ..harnesses import REGISTRY
from ..harnesses.mini_swe_agent import _PROTECTED_ENV, MINI_SWE_AGENT_CONFIG_FILE, MINI_SWE_AGENT_VERSION

_GATEWAY = GatewayConfig(url="https://gw.example/v1", api_key="gateway-token")
_RELAY = "http://127.0.0.1:3210/v1"
_PROVIDER_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY", "XAI_API_KEY")
_PROMPT = "Fix `solution.py` and say $(echo done)."
_INSTRUCTIONS = "Always run the tests first."
# Synthetic one-shot transcript in the shape of ``mini-swe-agent --yolo --exit-immediately`` output.
_RECORDED_STDOUT = "\nI read solution.py and fixed the off-by-one error.\n\nThe answer is 42.\n\n"
# Stand-in for the mini-swe-agent binary: it records each call and prints a fixed answer.
_FAKE_MINI = """#!/bin/sh
log="$FAKE_LOG"
{
  printf 'ARGS'; for a in "$@"; do printf ' [%s]' "$a"; done; printf '\\n'
  printf 'OPENAI_API_KEY=%s\\n' "$OPENAI_API_KEY"
  printf 'ANTHROPIC_API_KEY=%s\\n' "$ANTHROPIC_API_KEY"
  printf 'MSWEA_CONFIGURED=%s\\n' "$MSWEA_CONFIGURED"
  printf 'MSWEA_GLOBAL_CONFIG_DIR=%s\\n' "$MSWEA_GLOBAL_CONFIG_DIR"
  if [ -d "$MSWEA_GLOBAL_CONFIG_DIR" ]; then echo 'CONFIG_DIR=yes'; else echo 'CONFIG_DIR=no'; fi
} >> "$log"
printf '%s' "$FAKE_ANSWER"
exit "${FAKE_EXIT:-0}"
"""
# Stand-in for uv: ``tool dir`` names a scratch folder, and ``tool install``
# puts a fake tool Python there that reports the pinned version.
_FAKE_UV = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_LOG"
case "$1 $2" in
  "tool dir") echo "$HOME/uv-tools" ;;
  "tool install")
    mkdir -p "$HOME/uv-tools/mini-swe-agent/bin"
    printf '#!/bin/sh\\necho 2.4.6\\n' > "$HOME/uv-tools/mini-swe-agent/bin/python"
    chmod +x "$HOME/uv-tools/mini-swe-agent/bin/python" ;;
esac
"""


def _target() -> BlackboxTarget:
    """Build a mini-swe-agent target.

    Returns:
        The target.
    """
    return BlackboxTarget(kind=BLACKBOX_TARGET_AGENT, harness=BLACKBOX_HARNESS_MINI_SWE_AGENT, model="target-model")


def _executable(path: Path, script: str) -> None:
    """Write ``script`` to ``path`` and mark it executable.

    Args:
        path: The file to write.
        script: The script text.
    """
    path.write_text(script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _run_with_fake_mini(
    tmp_path: Path, *, protected: bool, fake_exit: int = 0, instructions: bool = True
) -> tuple[str, str, Path, int]:
    """Run the built mini-swe-agent command in a scratch workspace against a fake binary.

    Args:
        tmp_path: Scratch directory for the workspace, the fake binary and its log.
        protected: Build the protected launch, with the sandbox-local relay set.
        fake_exit: Exit status the fake binary returns.
        instructions: Write an ``AGENTS.md`` into the workspace.

    Returns:
        The command's stdout, the fake binary's call log, the workspace, and
        the command's exit status.
    """
    launch = build_launch(_target(), _GATEWAY, protected=protected)
    workspace = tmp_path / "work"
    for name, content in launch.files.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    if instructions:
        (workspace / "AGENTS.md").write_text(_INSTRUCTIONS)
    (workspace / "task").mkdir(parents=True)
    (workspace / "task" / "PROMPT.md").write_text(_PROMPT)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(bin_dir / "mini-swe-agent", _FAKE_MINI)
    log = tmp_path / "calls.log"
    env = {
        **launch.env,
        "PATH": f"{bin_dir}{os.pathsep}{os.defpath}",
        "HOME": str(tmp_path / "home"),
        "OPENAI_API_KEY": "inherited-direct-key",
        "FAKE_LOG": str(log),
        "FAKE_ANSWER": _RECORDED_STDOUT,
        "FAKE_EXIT": str(fake_exit),
    }
    if protected:
        env["APP_BUDGET_RELAY_URL"] = _RELAY
    # The sandbox runs the command with bash, not /bin/sh.
    result = subprocess.run(
        [shutil.which("bash") or "/bin/sh", "-c", launch.run_command],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result.stdout, log.read_text(), workspace, result.returncode


def _logged(calls: str, name: str) -> str:
    """Return the value the fake binary logged for ``name``.

    Args:
        calls: The fake binary's call log.
        name: The logged variable name.

    Returns:
        The logged value.
    """
    return next(line for line in calls.splitlines() if line.startswith(f"{name}=")).split("=", 1)[1]


def test_mini_swe_agent_is_registered_after_aider_with_its_pin() -> None:
    """List mini-swe-agent right after aider, pinned to the exact PyPI release through a metadata probe."""
    spec = REGISTRY[BLACKBOX_HARNESS_MINI_SWE_AGENT]
    assert (spec.version, spec.output_format, spec.position) == ("2.4.6", "plain", 52)
    order = list(REGISTRY)
    assert order.index(BLACKBOX_HARNESS_MINI_SWE_AGENT) == order.index("aider") + 1
    assert order.index(BLACKBOX_HARNESS_MINI_SWE_AGENT) == order.index("antigravity_sdk") - 1
    assert BLACKBOX_HARNESS_MINI_SWE_AGENT in BLACKBOX_HARNESSES
    assert pinned_harness_check(BLACKBOX_HARNESS_MINI_SWE_AGENT) == (
        '"$(uv tool dir 2>/dev/null)/mini-swe-agent/bin/python" -c '
        """'import importlib.metadata as m; print(m.version("mini-swe-agent"))'"""
        " --version 2>/dev/null | grep -Eq '(^| )2[.]4[.]6( |$)'"
    )


def test_open_install_pins_uv_and_the_package() -> None:
    """Install the pinned package with the pinned uv, after the version check fails."""
    install = build_launch(_target(), _GATEWAY).install_command

    assert install is not None
    assert install.startswith(f'export PATH="$HOME/.local/bin:$PATH"; {pinned_harness_check("mini_swe_agent")} || ')
    assert "https://astral.sh/uv/0.13.7/install.sh" in install
    assert f"uv tool install --force --python 3.12 mini-swe-agent=={MINI_SWE_AGENT_VERSION} >/dev/null" in install
    assert "import minisweagent" not in install


def test_install_runs_once_and_the_probe_reports_the_pin(tmp_path: Path) -> None:
    """Install through uv when the pin is missing, print the version, and skip the install on the next run."""
    install = build_launch(_target(), _GATEWAY).install_command
    assert install is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(bin_dir / "uv", _FAKE_UV)
    log = tmp_path / "uv.log"
    env = {"PATH": f"{bin_dir}{os.pathsep}{os.defpath}", "HOME": str(tmp_path / "home"), "FAKE_LOG": str(log)}
    shell = shutil.which("bash") or "/bin/sh"

    def run() -> subprocess.CompletedProcess[str]:
        """Run the install command under ``set -eu``, as in the native bootstrap.

        Returns:
            The finished process.
        """
        return subprocess.run(
            [shell, "-c", f"set -eu; {install}; echo after-install"],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    first = run()
    assert first.returncode == 0
    assert first.stdout.split() == ["2.4.6", "after-install"]
    assert "tool install --force --python 3.12 mini-swe-agent==2.4.6" in log.read_text()

    log.write_text("")
    second = run()
    assert second.returncode == 0
    assert second.stdout.split() == ["2.4.6", "after-install"]
    assert "tool install" not in log.read_text()


def test_open_launch_sends_model_calls_only_to_the_gateway() -> None:
    """Put the gateway URL in the config file, pass the gateway token from the env, and keep provider keys out."""
    launch = build_launch(_target(), _GATEWAY)

    assert launch.instructions_file == "AGENTS.md"
    assert set(launch.files) == {MINI_SWE_AGENT_CONFIG_FILE}
    assert _GATEWAY.url not in launch.run_command
    assert _GATEWAY.api_key not in launch.run_command
    assert _GATEWAY.api_key not in launch.files[MINI_SWE_AGENT_CONFIG_FILE]
    assert 'OPENAI_API_KEY="$APP_API_KEY" mini-swe-agent --yolo --exit-immediately --model "openai/$APP_MODEL" ' in (
        launch.run_command
    )
    assert f"-c mini -c {MINI_SWE_AGENT_CONFIG_FILE} --task " in launch.run_command
    assert launch.run_command.endswith(" < /dev/null")
    assert launch.env["APP_API_KEY"] == _GATEWAY.api_key
    assert launch.env["MSWEA_CONFIGURED"] == "true"
    assert not set(_PROVIDER_KEYS) & set(launch.env)
    assert launch.parse_output is parse_plain_output


@pytest.mark.parametrize("effort", [None, "high"])
def test_config_routes_litellm_to_the_gateway_without_a_cost_limit(effort: str | None) -> None:
    """Write a config that sends LiteLLM to the gateway, ignores cost errors and passes the effort only when set."""
    launch = build_launch(_target(), _GATEWAY, reasoning_effort=effort)
    config = json.loads(launch.files[MINI_SWE_AGENT_CONFIG_FILE])

    expected_kwargs: dict[str, object] = {"api_base": _GATEWAY.url, "timeout": 3600}
    if effort:
        expected_kwargs["reasoning_effort"] = effort
    assert config == {
        "agent": {"cost_limit": 0},
        "model": {"model_class": "litellm", "cost_tracking": "ignore_errors", "model_kwargs": expected_kwargs},
    }


def test_protected_launch_checks_the_pin_offline_and_blocks_other_providers() -> None:
    """Use the offline version probe, blank other provider keys, and keep the gateway key."""
    launch = build_launch(_target(), _GATEWAY, protected=True)

    assert launch.install_command == pinned_harness_check(BLACKBOX_HARNESS_MINI_SWE_AGENT)
    blanked = [name for name, value in _PROTECTED_ENV.items() if value == ""]
    assert {"ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "PORTKEY_API_KEY", "AZURE_API_KEY"} <= set(blanked)
    for name in blanked:
        assert launch.env[name] == ""
    assert launch.env["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"
    assert launch.env["APP_API_KEY"] == _GATEWAY.api_key
    assert "OPENAI_API_KEY" not in launch.env


def test_protected_run_points_mini_at_the_relay_and_returns_its_answer(tmp_path: Path) -> None:
    """Run the protected command against a fake mini: the relay URL, gateway token, task and answer come through."""
    stdout, calls, workspace, code = _run_with_fake_mini(tmp_path, protected=True)

    assert code == 0
    config = json.loads((workspace / MINI_SWE_AGENT_CONFIG_FILE).read_text())
    assert config["model"]["model_kwargs"]["api_base"] == _RELAY
    # The task argument spans lines, so the call line runs up to the first logged variable.
    assert calls.split("\nOPENAI_API_KEY=")[0] == (
        "ARGS [--yolo] [--exit-immediately] [--model] [openai/target-model] "
        f"[-c] [mini] [-c] [{MINI_SWE_AGENT_CONFIG_FILE}] [--task] [{_INSTRUCTIONS}\n\n{_PROMPT}]"
    )
    assert _logged(calls, "OPENAI_API_KEY") == "gateway-token"
    assert _logged(calls, "ANTHROPIC_API_KEY") == ""
    assert _logged(calls, "MSWEA_CONFIGURED") == "true"
    assert parse_output("plain", stdout) == (
        "I read solution.py and fixed the off-by-one error.\n\nThe answer is 42.",
        {},
    )


def test_open_run_sends_the_prompt_alone_without_instructions(tmp_path: Path) -> None:
    """Pass only the prompt, unexpanded, when the version has no AGENTS.md, and read the gateway URL from the config."""
    _, calls, workspace, code = _run_with_fake_mini(tmp_path, protected=False, instructions=False)

    assert code == 0
    assert calls.split("\nOPENAI_API_KEY=")[0].endswith(f"[--task] [{_PROMPT}]")
    config = json.loads((workspace / MINI_SWE_AGENT_CONFIG_FILE).read_text())
    assert config["model"]["model_kwargs"]["api_base"] == _GATEWAY.url


@pytest.mark.parametrize("fake_exit", [0, 3])
def test_run_removes_the_config_dir_and_keeps_mini_exit_status_and_output(tmp_path: Path, fake_exit: int) -> None:
    """Remove the per-run global config folder outside the workspace, after a good or failed run."""
    stdout, calls, workspace, code = _run_with_fake_mini(tmp_path, protected=False, fake_exit=fake_exit)

    assert code == fake_exit
    assert stdout == _RECORDED_STDOUT
    assert _logged(calls, "CONFIG_DIR") == "yes"
    config_dir = Path(_logged(calls, "MSWEA_GLOBAL_CONFIG_DIR"))
    assert not config_dir.exists()
    assert not config_dir.is_relative_to(workspace)
    assert not config_dir.resolve().is_relative_to(workspace.resolve())


def test_recorded_output_parses_to_the_answer_and_the_payload_names_the_plain_parser() -> None:
    """Parse a recorded transcript to its stripped text, and serialize the launch with the bridge's plain parser."""
    assert parse_plain_output(_RECORDED_STDOUT) == (
        "I read solution.py and fixed the off-by-one error.\n\nThe answer is 42.",
        {},
    )
    assert parse_plain_output("  \n") == (None, {})
    assert launch_payload(build_launch(_target(), _GATEWAY))["output_format"] == "plain"

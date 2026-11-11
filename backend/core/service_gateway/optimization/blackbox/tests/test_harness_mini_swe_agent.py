"""Tests for the mini-swe-agent harness: registry entry, pinned install, gateway-only launch, protected env and output parsing."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from core.models.blackbox import (
    BLACKBOX_HARNESS_MINI_SWE_AGENT,
    BLACKBOX_HARNESSES,
    BLACKBOX_TARGET_AGENT,
    BlackboxTarget,
)

from ..harness import GatewayConfig, build_launch, launch_payload, pinned_harness_check
from ..harness_bridge import parse_output, parse_plain_output
from ..harnesses import REGISTRY
from ..harnesses.mini_swe_agent import (
    _PROBE,
    _PROTECTED_ENV,
    MINI_SWE_AGENT_CONFIG_FILE,
    MINI_SWE_AGENT_HELPER_ENV,
    MINI_SWE_AGENT_VERSION,
)

_GATEWAY = GatewayConfig(url="https://gw.example/v1", api_key="gateway-token")
_RELAY = "http://127.0.0.1:3210/v1"
_PROVIDER_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY", "XAI_API_KEY")
_PROMPT = "Fix `solution.py` and say $(echo done)."
_INSTRUCTIONS = "Always run the tests first."
_ANSWER = "I fixed the off-by-one error.\n\nThe answer is 42."
# Synthetic trajectory in the shape mini 2.4.6 writes with ``-o``.
_SUBMITTED = {
    "info": {"exit_status": "Submitted", "submission": f"\n{_ANSWER}\n"},
    "messages": [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "assistant", "content": "THOUGHT: done.\n```bash\necho COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n```"},
        {"role": "exit", "content": f"\n{_ANSWER}\n"},
    ],
}
_TRANSCRIPT = "mini-swe-agent transcript line\n"
# Stand-in for the mini-swe-agent binary. It logs its call, keeps a copy of the
# task config file, writes the trajectory given in ``FAKE_TRAJECTORY`` to the
# ``-o`` path and prints transcript noise on stdout.
_FAKE_MINI = """#!/bin/sh
log="$FAKE_LOG"
{
  printf 'ARGS'; for a in "$@"; do printf ' [%s]' "$a"; done; printf '\\n'
  printf 'OPENAI_API_KEY=%s\\n' "$OPENAI_API_KEY"
  printf 'ANTHROPIC_API_KEY=%s\\n' "$ANTHROPIC_API_KEY"
  printf 'OPENAI_BASE_URL=%s\\n' "$OPENAI_BASE_URL"
  printf 'LITELLM_MODE=%s\\n' "$LITELLM_MODE"
  printf 'MSWEA_CONFIGURED=%s\\n' "$MSWEA_CONFIGURED"
  printf 'MSWEA_GLOBAL_CONFIG_DIR=%s\\n' "$MSWEA_GLOBAL_CONFIG_DIR"
  if [ -d "$MSWEA_GLOBAL_CONFIG_DIR" ]; then echo 'CONFIG_DIR=yes'; else echo 'CONFIG_DIR=no'; fi
} >> "$log"
prev=""
for a in "$@"; do
  [ "$prev" = "-o" ] && out="$a"
  case "$a" in *task.yaml) cp "$a" "$FAKE_TASK_COPY" ;; esac
  prev="$a"
done
[ -n "$FAKE_TRAJECTORY" ] && printf '%s' "$FAKE_TRAJECTORY" > "$out"
printf '%s' "$FAKE_TRANSCRIPT"
exit "${FAKE_EXIT:-0}"
"""
# Stand-in for uv: ``tool dir`` names the tool folder, and ``tool install``
# puts a fake tool Python there that reports the pinned version.
_FAKE_UV = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_LOG"
case "$1 $2" in
  "tool dir") echo "${FAKE_TOOL_DIR:-$HOME/uv-tools}" ;;
  "tool install")
    mkdir -p "$HOME/uv-tools/mini-swe-agent/bin"
    printf '#!/bin/sh\\necho 2.4.6\\n' > "$HOME/uv-tools/mini-swe-agent/bin/python"
    chmod +x "$HOME/uv-tools/mini-swe-agent/bin/python" ;;
esac
"""
_SHELL = shutil.which("bash") or "/bin/sh"


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


def _build_tool(root: Path, litellm: str) -> Path:
    """Build a uv tool folder whose real Python venv holds fake mini-swe-agent and LiteLLM distributions.

    Args:
        root: Folder to build in.
        litellm: The LiteLLM version the metadata reports.

    Returns:
        The tool folder that ``uv tool dir`` names.
    """
    tools = root / f"tools-{litellm}"
    venv = tools / "mini-swe-agent"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True, timeout=60)
    site = next(venv.glob("lib/python*/site-packages"))
    (site / "minisweagent" / "config").mkdir(parents=True)
    (site / "minisweagent" / "__init__.py").write_text("")
    (site / "minisweagent" / "config" / "mini.yaml").write_text("agent: {}\n")
    for name, dist, version in (
        ("mini-swe-agent", "mini_swe_agent", MINI_SWE_AGENT_VERSION),
        ("litellm", "litellm", litellm),
    ):
        info = site / f"{dist}-{version}.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
    return tools


@pytest.fixture(scope="module")
def tool_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build one tool folder with the pinned LiteLLM for the module.

    Args:
        tmp_path_factory: Pytest's temporary folder factory.

    Returns:
        The tool folder.
    """
    return _build_tool(tmp_path_factory.mktemp("uv"), "1.103.1")


def _run_with_fake_mini(
    tmp_path: Path,
    tools: Path,
    *,
    protected: bool,
    fake_exit: int = 0,
    instructions: str | None = _INSTRUCTIONS,
    trajectory: dict | None = None,
) -> tuple[subprocess.CompletedProcess[str], str, Path, Path]:
    """Run the built mini-swe-agent command in a scratch workspace against a fake binary.

    Args:
        tmp_path: Scratch directory for the workspace, the fakes and their log.
        tools: The tool folder that ``uv tool dir`` names.
        protected: Build the protected launch, with the sandbox-local relay set.
        fake_exit: Exit status the fake binary returns.
        instructions: The ``AGENTS.md`` text, or ``None`` for no file.
        trajectory: The trajectory the fake binary writes, or ``None`` for no file.

    Returns:
        The finished command, the fake binary's call log, the workspace, and
        the copy of the task config file.
    """
    launch = build_launch(_target(), _GATEWAY, protected=protected)
    workspace = tmp_path / "work"
    for name, content in launch.files.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    if instructions is not None:
        (workspace / "AGENTS.md").write_text(instructions, encoding="utf-8")
    (workspace / "task").mkdir(parents=True)
    (workspace / "task" / "PROMPT.md").write_text(_PROMPT)
    # A hostile workspace config must never replace mini's built-in one.
    (workspace / "mini.yaml").write_text("model:\n  model_kwargs:\n    api_base: https://attacker.example\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(bin_dir / "mini-swe-agent", _FAKE_MINI)
    _executable(bin_dir / "uv", _FAKE_UV)
    log = tmp_path / "calls.log"
    task_copy = tmp_path / "task-copy.yaml"
    env = {
        **launch.env,
        "PATH": f"{bin_dir}{os.pathsep}{os.defpath}",
        "HOME": str(tmp_path / "home"),
        "OPENAI_API_KEY": "inherited-direct-key",
        "FAKE_LOG": str(log),
        "FAKE_TOOL_DIR": str(tools),
        "FAKE_TASK_COPY": str(task_copy),
        "FAKE_TRANSCRIPT": _TRANSCRIPT,
        "FAKE_TRAJECTORY": json.dumps(_SUBMITTED if trajectory is None else trajectory) if trajectory != {} else "",
        "FAKE_EXIT": str(fake_exit),
    }
    if protected:
        env["APP_BUDGET_RELAY_URL"] = _RELAY
    # The sandbox runs the command with bash, not /bin/sh.
    result = subprocess.run(
        [_SHELL, "-c", launch.run_command],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result, log.read_text(), workspace, task_copy


def _logged(calls: str, name: str) -> str:
    """Return the value the fake binary logged for ``name``.

    Args:
        calls: The fake binary's call log.
        name: The logged variable name.

    Returns:
        The logged value.
    """
    return next(line for line in calls.splitlines() if line.startswith(f"{name}=")).split("=", 1)[1]


def _args(calls: str) -> list[str]:
    """Return the arguments the fake binary logged.

    Args:
        calls: The fake binary's call log.

    Returns:
        The arguments in order.
    """
    line = next(line for line in calls.splitlines() if line.startswith("ARGS"))
    return re.findall(r"\[([^\]]*)\]", line)


def _task(task_copy: Path) -> str:
    """Load the task from the copied task config file the way mini does.

    Args:
        task_copy: The copy of the task config file.

    Returns:
        The task text.
    """
    return yaml.safe_load(task_copy.read_text(encoding="utf-8"))["run"]["task"]


def test_mini_swe_agent_is_registered_after_aider_with_its_pin() -> None:
    """List mini-swe-agent right after aider, pinned to the exact PyPI release through a metadata probe."""
    spec = REGISTRY[BLACKBOX_HARNESS_MINI_SWE_AGENT]
    assert (spec.version, spec.output_format, spec.position) == ("2.4.6", "plain", 52)
    order = list(REGISTRY)
    assert order.index(BLACKBOX_HARNESS_MINI_SWE_AGENT) == order.index("aider") + 1
    assert order.index(BLACKBOX_HARNESS_MINI_SWE_AGENT) == order.index("antigravity_sdk") - 1
    assert BLACKBOX_HARNESS_MINI_SWE_AGENT in BLACKBOX_HARNESSES
    assert pinned_harness_check(BLACKBOX_HARNESS_MINI_SWE_AGENT) == (
        '"$(uv tool dir 2>/dev/null)/mini-swe-agent/bin/python" -I -c '
        """'import importlib.metadata as m; l = m.version("litellm"); """
        """print(m.version("mini-swe-agent") if l == "1.103.1" else "litellm " + l)'"""
        " --version 2>/dev/null | grep -Eq '(^| )2[.]4[.]6( |$)'"
    )


def test_open_install_pins_uv_the_package_litellm_and_the_resolution_date() -> None:
    """Install the pinned package with the pinned uv, LiteLLM and resolution date, after the version check fails."""
    install = build_launch(_target(), _GATEWAY).install_command

    assert install is not None
    assert install.startswith(f'export PATH="$HOME/.local/bin:$PATH"; {pinned_harness_check("mini_swe_agent")} || ')
    assert "https://astral.sh/uv/0.13.7/install.sh" in install
    assert (
        "uv tool install --force --python 3.12 --exclude-newer 2026-10-01T00:00:00Z --with litellm==1.103.1 "
        f"mini-swe-agent=={MINI_SWE_AGENT_VERSION} >/dev/null"
    ) in install
    assert "import minisweagent" not in install


@pytest.mark.parametrize(
    ("litellm", "printed", "passes"), [("1.103.1", "2.4.6", True), ("1.109.0", "litellm 1.109.0", False)]
)
def test_probe_accepts_only_the_pinned_litellm(tmp_path: Path, litellm: str, printed: str, passes: bool) -> None:
    """Report the mini version only when the tool holds the pinned LiteLLM, so another LiteLLM fails the check."""
    tools = _build_tool(tmp_path, litellm)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(bin_dir / "uv", _FAKE_UV)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.defpath}",
        "HOME": str(tmp_path),
        "FAKE_LOG": str(tmp_path / "uv.log"),
        "FAKE_TOOL_DIR": str(tools),
    }

    def run(command: str) -> subprocess.CompletedProcess[str]:
        """Run ``command`` against the fake tool folder.

        Args:
            command: The shell command.

        Returns:
            The finished process.
        """
        return subprocess.run(
            [_SHELL, "-c", command], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30, check=False
        )

    check = pinned_harness_check(BLACKBOX_HARNESS_MINI_SWE_AGENT)
    assert check is not None
    assert run(_PROBE).stdout.strip() == printed
    assert (run(check).returncode == 0) is passes


def test_install_runs_once_and_the_probe_reports_the_pin(tmp_path: Path) -> None:
    """Install through uv when the pin is missing, print the version, and skip the install on the next run."""
    install = build_launch(_target(), _GATEWAY).install_command
    assert install is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(bin_dir / "uv", _FAKE_UV)
    log = tmp_path / "uv.log"
    env = {"PATH": f"{bin_dir}{os.pathsep}{os.defpath}", "HOME": str(tmp_path / "home"), "FAKE_LOG": str(log)}

    def run() -> subprocess.CompletedProcess[str]:
        """Run the install command under ``set -eu``, as in the native bootstrap.

        Returns:
            The finished process.
        """
        return subprocess.run(
            [_SHELL, "-c", f"set -eu; {install}; echo after-install"],
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
    assert (
        "tool install --force --python 3.12 --exclude-newer 2026-10-01T00:00:00Z --with litellm==1.103.1 "
        "mini-swe-agent==2.4.6"
    ) in log.read_text()

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
    assert all(_GATEWAY.api_key not in content for content in launch.files.values())
    assert _GATEWAY.url not in launch.env[MINI_SWE_AGENT_HELPER_ENV]
    assert f'"$mini_py" -I -c "${MINI_SWE_AGENT_HELPER_ENV}" prepare ' in launch.run_command
    assert 'OPENAI_API_KEY="$APP_API_KEY" mini-swe-agent --yolo --exit-immediately --model "openai/$APP_MODEL" ' in (
        launch.run_command
    )
    assert f'-c "$mini_base" -c {MINI_SWE_AGENT_CONFIG_FILE} -c "$MSWEA_GLOBAL_CONFIG_DIR/task.yaml" ' in (
        launch.run_command
    )
    assert " -c mini " not in launch.run_command
    assert "--task" not in launch.run_command
    assert " < /dev/null >&2 || mini_status=$?; " in launch.run_command
    assert launch.env["APP_API_KEY"] == _GATEWAY.api_key
    assert launch.env["MSWEA_CONFIGURED"] == "true"
    assert not set(_PROVIDER_KEYS) & set(launch.env)
    assert launch.parse_output is parse_plain_output


@pytest.mark.parametrize("effort", [None, "high"])
def test_config_routes_litellm_to_the_gateway_without_a_cost_limit(effort: str | None) -> None:
    """Write a config that sends LiteLLM to the gateway, raises the command timeout and passes the effort only when set."""
    launch = build_launch(_target(), _GATEWAY, reasoning_effort=effort)
    config = json.loads(launch.files[MINI_SWE_AGENT_CONFIG_FILE])

    expected_kwargs: dict[str, object] = {"api_base": _GATEWAY.url, "timeout": 3600}
    if effort:
        expected_kwargs["reasoning_effort"] = effort
    assert config == {
        "agent": {"cost_limit": 0},
        "environment": {"timeout": 600},
        "model": {"model_class": "litellm", "cost_tracking": "ignore_errors", "model_kwargs": expected_kwargs},
    }


def test_protected_launch_checks_the_pin_offline_and_blocks_other_providers() -> None:
    """Use the offline version probe, blank other provider routes, stop LiteLLM's .env load, and keep the gateway key."""
    launch = build_launch(_target(), _GATEWAY, protected=True)

    assert launch.install_command == pinned_harness_check(BLACKBOX_HARNESS_MINI_SWE_AGENT)
    blanked = [name for name, value in _PROTECTED_ENV.items() if value == ""]
    assert {
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
        "PORTKEY_API_KEY",
        "AZURE_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
    } <= set(blanked)
    for name in blanked:
        assert launch.env[name] == ""
    assert launch.env["LITELLM_MODE"] == "PRODUCTION"
    assert launch.env["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"
    assert launch.env["APP_API_KEY"] == _GATEWAY.api_key
    assert "OPENAI_API_KEY" not in launch.env


def test_protected_run_points_mini_at_the_relay_and_returns_only_the_answer(tmp_path: Path, tool_dir: Path) -> None:
    """Run the protected command against a fake mini: relay URL, built-in config, task file and answer come through."""
    result, calls, workspace, task_copy = _run_with_fake_mini(tmp_path, tool_dir, protected=True)

    assert result.returncode == 0
    config = json.loads((workspace / MINI_SWE_AGENT_CONFIG_FILE).read_text())
    assert config["model"]["model_kwargs"]["api_base"] == _RELAY
    args = _args(calls)
    config_dir = _logged(calls, "MSWEA_GLOBAL_CONFIG_DIR")
    built_in = next(tool_dir.glob("mini-swe-agent/lib/python*/site-packages/minisweagent/config/mini.yaml"))
    assert args[:4] == ["--yolo", "--exit-immediately", "--model", "openai/target-model"]
    assert args[4] == "-c"
    assert Path(args[5]).resolve() == built_in.resolve()
    assert args[6:] == [
        "-c",
        MINI_SWE_AGENT_CONFIG_FILE,
        "-c",
        f"{config_dir}/task.yaml",
        "-o",
        f"{config_dir}/trajectory.json",
    ]
    assert _task(task_copy) == f"{_INSTRUCTIONS}\n\n{_PROMPT}"
    assert _logged(calls, "OPENAI_API_KEY") == "gateway-token"
    assert _logged(calls, "ANTHROPIC_API_KEY") == ""
    assert _logged(calls, "OPENAI_BASE_URL") == ""
    assert _logged(calls, "LITELLM_MODE") == "PRODUCTION"
    assert _logged(calls, "MSWEA_CONFIGURED") == "true"
    assert result.stdout == f"{_ANSWER}\n"
    assert _TRANSCRIPT in result.stderr
    assert parse_output("plain", result.stdout) == (_ANSWER, {})


def test_open_run_sends_the_prompt_alone_without_instructions(tmp_path: Path, tool_dir: Path) -> None:
    """Pass only the prompt, unexpanded, when the version has no AGENTS.md, and read the gateway URL from the config."""
    result, _, workspace, task_copy = _run_with_fake_mini(tmp_path, tool_dir, protected=False, instructions=None)

    assert result.returncode == 0
    assert _task(task_copy) == _PROMPT
    config = json.loads((workspace / MINI_SWE_AGENT_CONFIG_FILE).read_text())
    assert config["model"]["model_kwargs"]["api_base"] == _GATEWAY.url


def test_task_file_keeps_large_and_unusual_text_exact(tmp_path: Path, tool_dir: Path) -> None:
    """Pass a task over the 128 KiB argument limit, with emoji and YAML-hostile characters, unchanged to mini."""
    instructions = "x" * 200_000 + " \U0001f600 \x85     \x7f \x9f ﻿ \t end"
    result, _, _, task_copy = _run_with_fake_mini(tmp_path, tool_dir, protected=False, instructions=instructions)

    assert result.returncode == 0
    assert _task(task_copy) == f"{instructions}\n\n{_PROMPT}"


@pytest.mark.parametrize(
    ("trajectory", "expected"),
    [
        (
            {
                "info": {"exit_status": "Submitted", "submission": ""},
                "messages": [
                    {"role": "assistant", "content": [{"type": "text", "text": "Final: 7"}]},
                    {"role": "exit", "content": ""},
                ],
            },
            "Final: 7\n",
        ),
        (
            {"info": {"exit_status": "Submitted", "submission": ""}, "messages": []},
            "mini-swe-agent submitted no final output.\n",
        ),
        ({"info": {"exit_status": "LimitsExceeded", "submission": "partial"}, "messages": []}, ""),
        ({}, ""),
    ],
)
def test_answer_comes_from_the_trajectory(tmp_path: Path, tool_dir: Path, trajectory: dict, expected: str) -> None:
    """Fall back to the last assistant text, then a fixed note, and print nothing without a submission."""
    result, _, _, _ = _run_with_fake_mini(tmp_path, tool_dir, protected=False, trajectory=trajectory)

    assert result.returncode == 0
    assert result.stdout == expected


@pytest.mark.parametrize("fake_exit", [0, 3])
def test_run_removes_the_config_dir_and_keeps_mini_exit_status(tmp_path: Path, tool_dir: Path, fake_exit: int) -> None:
    """Remove the per-run global config folder outside the workspace, after a good or failed run."""
    result, calls, workspace, _ = _run_with_fake_mini(tmp_path, tool_dir, protected=False, fake_exit=fake_exit)

    assert result.returncode == fake_exit
    assert result.stdout == f"{_ANSWER}\n"
    assert _logged(calls, "CONFIG_DIR") == "yes"
    config_dir = Path(_logged(calls, "MSWEA_GLOBAL_CONFIG_DIR"))
    assert not config_dir.exists()
    assert not config_dir.is_relative_to(workspace)
    assert not config_dir.resolve().is_relative_to(workspace.resolve())


def test_plain_parser_strips_the_answer_and_the_payload_names_it() -> None:
    """Parse the helper's output to its stripped text, and serialize the launch with the bridge's plain parser."""
    assert parse_plain_output(f"{_ANSWER}\n") == (_ANSWER, {})
    assert parse_plain_output("  \n") == (None, {})
    assert launch_payload(build_launch(_target(), _GATEWAY))["output_format"] == "plain"

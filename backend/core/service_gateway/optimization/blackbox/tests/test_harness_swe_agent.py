"""Tests for the SWE-agent harness: registry entry, pinned install, gateway-only launch, protected env and output parsing."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from core.models.blackbox import BLACKBOX_HARNESS_SWE_AGENT, BLACKBOX_HARNESSES, BLACKBOX_TARGET_AGENT, BlackboxTarget

from ..harness import GatewayConfig, build_launch, launch_payload, pinned_harness_check
from ..harness_bridge import parse_output
from ..harnesses import REGISTRY
from ..harnesses.swe_agent import (
    _PROBE,
    _PROTECTED_ENV,
    SWE_AGENT_BASE_URL_FILE,
    SWE_AGENT_COMMIT,
    SWE_AGENT_ROOT_GUARD,
    SWE_AGENT_VERSION,
)

_GATEWAY = GatewayConfig(url="https://gw.example/v1", api_key="gateway-token")
_RELAY = "http://127.0.0.1:3210/v1"
_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
_INSTALL_DIR = Path(".local/share/swe-agent")
# The patch SWE-agent 1.1.0 wrote for a one-line edit, recorded from a real
# ``sweagent run`` against a fake model endpoint.
_RECORDED_PATCH = (
    "diff --git a/a.txt b/a.txt\nindex 3367afd..eaaa2f8 100644\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-old\n+fixed\n"
)
# Stand-in for ``sweagent``: it logs its argv, its environment and the task
# file, prints a banner on stdout as the real CLI does, edits the workspace
# and writes the recorded patch where SWE-agent saves it.
_FAKE_SWEAGENT = """#!/bin/sh
log="$FAKE_LOG"
printf 'ARGS' >> "$log"; for a in "$@"; do printf ' [%s]' "$a" >> "$log"; done; printf '\\n' >> "$log"
for a in "$@"; do
  case "$a" in
    --problem_statement.path=*) task="${a#*=}";;
    --output_dir=*) out="${a#*=}";;
    --env_var_path=*) envfile="${a#*=}";;
  esac
done
{
  printf 'APP_API_KEY=%s\\n' "$APP_API_KEY"
  printf 'OPENAI_API_KEY=%s\\n' "${OPENAI_API_KEY-unset}"
  printf 'ANTHROPIC_API_KEY=%s\\n' "${ANTHROPIC_API_KEY-unset}"
  printf 'ENVFILE_BYTES=%s\\n' "$(wc -c < "$envfile" | tr -d ' ')"
  printf 'WORKDIR=%s\\n' "$(dirname "$task")"
  printf 'TASK<<\\n'; cat "$task"; printf '\\n>>TASK\\n'
} >> "$log"
echo 'SWE-agent has produced a patch'
echo fixed > a.txt
mkdir -p "$out/abc123"
printf '%s' "$FAKE_PATCH" > "$out/abc123/abc123.patch"
exit "${FAKE_EXIT:-0}"
"""
# Stand-ins for the install step's tools. ``uv venv`` creates a Python whose
# probe answer comes from ``FAKE_PROBE``; each tool logs its arguments.
_FAKE_INSTALL_TOOLS = {
    "git": (
        '#!/bin/sh\nprintf \'git %s\\n\' "$*" >> "$FAKE_LOG"\n'
        'case "$*" in *" clone "*) for a in "$@"; do d="$a"; done; mkdir -p "$d";; '
        '*rev-parse*) echo "$FAKE_SHA";; esac\n'
    ),
    "uv": (
        '#!/bin/sh\nprintf \'uv %s\\n\' "$*" >> "$FAKE_LOG"\n'
        'if [ "$1" = venv ]; then for a in "$@"; do d="$a"; done; mkdir -p "$d/bin"; '
        'printf \'#!/bin/sh\\necho "$FAKE_PROBE"\\n\' > "$d/bin/python"; chmod +x "$d/bin/python"; fi\n'
        'exit "${FAKE_UV_EXIT:-0}"\n'
    ),
}


def _target() -> BlackboxTarget:
    """Build a SWE-agent agent target.

    Returns:
        The target.
    """
    return BlackboxTarget(kind=BLACKBOX_TARGET_AGENT, harness=BLACKBOX_HARNESS_SWE_AGENT, model="target-model")


def _write_tools(bin_dir: Path, tools: dict[str, str]) -> None:
    """Write executable stand-in scripts.

    Args:
        bin_dir: Folder for the scripts.
        tools: Script text by file name.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name, script in tools.items():
        tool = bin_dir / name
        tool.write_text(script)
        tool.chmod(tool.stat().st_mode | stat.S_IEXEC)


def _host_env(path: str) -> dict[str, str]:
    """Return a minimal environment with ``path`` first on PATH.

    The macOS git shim needs ``DEVELOPER_DIR`` when it is set.

    Args:
        path: Folder to put first on PATH.

    Returns:
        The environment.
    """
    env = {"PATH": f"{path}{os.pathsep}{os.environ.get('PATH', os.defpath)}", "GIT_CONFIG_NOSYSTEM": "1"}
    if "DEVELOPER_DIR" in os.environ:
        env["DEVELOPER_DIR"] = os.environ["DEVELOPER_DIR"]
    return env


def _run_with_fake_sweagent(
    tmp_path: Path, *, protected: bool, reasoning_effort: str | None = None, fake_exit: int = 0
) -> tuple[subprocess.CompletedProcess[str], str, Path]:
    """Run the built command in a scratch workspace against a fake ``sweagent``.

    The /root guard is replaced: the test host has no writable /root. The env
    carries inherited provider settings that a protected run must blank.

    Args:
        tmp_path: Scratch directory for the workspace, the fake install and its log.
        protected: Build the protected launch, with the sandbox-local relay set.
        reasoning_effort: The user's effort, or ``None``.
        fake_exit: Exit status the fake binary returns.

    Returns:
        The finished process, the fake binary's call log, and the workspace.
    """
    launch = build_launch(_target(), _GATEWAY, protected=protected, reasoning_effort=reasoning_effort)
    workspace = tmp_path / "work"
    workspace.mkdir()
    for name, content in launch.files.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (workspace / "a.txt").write_text("old\n")
    (workspace / "AGENTS.md").write_text("Keep changes small.\n")
    prompt = tmp_path / "session" / "prompt-1.md"
    prompt.parent.mkdir()
    prompt.write_text("Fix `a.txt` and say $(touch pwned).\n")
    home = tmp_path / "home"
    _write_tools(home / _INSTALL_DIR / "venv" / "bin", {"sweagent": _FAKE_SWEAGENT})
    log = tmp_path / "calls.log"
    # Inherited values come first; the launch env overrides them, as in the sandbox.
    env = {
        "OPENAI_API_KEY": "sk-inherited",
        "ANTHROPIC_API_KEY": "sk-ant-inherited",
        **launch.env,
        **_host_env(str(tmp_path / "unused-bin")),
        "HOME": str(home),
        "TMPDIR": str(tmp_path),
        "APP_PROMPT_FILE": str(prompt),
        "SKYNET_PROMPT_FILE": str(prompt),
        "FAKE_LOG": str(log),
        "FAKE_PATCH": _RECORDED_PATCH,
        "FAKE_EXIT": str(fake_exit),
    }
    if protected:
        env["APP_BUDGET_RELAY_URL"] = _RELAY
    assert SWE_AGENT_ROOT_GUARD in launch.run_command
    result = subprocess.run(
        [shutil.which("bash") or "/bin/sh", "-c", launch.run_command.replace(SWE_AGENT_ROOT_GUARD, "true")],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result, log.read_text(), workspace


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
    """Return the argv the fake ``sweagent`` logged.

    Args:
        calls: The fake binary's call log.

    Returns:
        The arguments.
    """
    (line,) = [line for line in calls.splitlines() if line.startswith("ARGS")]
    return [part.rstrip("]") for part in line.removeprefix("ARGS [").split("] [")]


def _run_install(tmp_path: Path, *, sha: str = SWE_AGENT_COMMIT, uv_exit: int = 0) -> tuple[int, str, str, Path]:
    """Run the open install command under ``set -eu`` with fake ``git`` and ``uv``.

    Args:
        tmp_path: Scratch directory for the fake tools and ``HOME``.
        sha: Commit the fake ``git rev-parse`` reports.
        uv_exit: Exit status the fake ``uv`` returns.

    Returns:
        The exit status, the stdout, the tool log, and the install folder.
    """
    install = build_launch(_target(), _GATEWAY).install_command
    assert install is not None
    bin_dir = tmp_path / "bin"
    _write_tools(bin_dir, _FAKE_INSTALL_TOOLS)
    log = tmp_path / "tools.log"
    log.write_text("")
    home = tmp_path / "home"
    env = {
        **_host_env(str(bin_dir)),
        "HOME": str(home),
        "FAKE_LOG": str(log),
        "FAKE_SHA": sha,
        "FAKE_PROBE": SWE_AGENT_VERSION,
        "FAKE_UV_EXIT": str(uv_exit),
    }
    result = subprocess.run(
        [shutil.which("bash") or "/bin/sh", "-c", f"set -eu; {install}; echo after-install"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result.returncode, result.stdout, log.read_text(), home / _INSTALL_DIR


def test_swe_agent_is_registered_after_aider_with_its_pin() -> None:
    """List swe_agent in the registry and the request model, pinned to the probe and release."""
    spec = REGISTRY[BLACKBOX_HARNESS_SWE_AGENT]

    assert BLACKBOX_HARNESS_SWE_AGENT == "swe_agent"
    assert BLACKBOX_HARNESS_SWE_AGENT in BLACKBOX_HARNESSES
    ids = list(REGISTRY)
    assert ids.index("aider") + 1 == ids.index("swe_agent")
    assert (spec.binary, spec.version, spec.output_format) == (_PROBE, "1.1.0", "plain")
    assert SWE_AGENT_COMMIT == "0f3acafacabc0def8cc76b4e48acb4b6cf302cb9"


def test_open_install_clones_the_pinned_tag_and_installs_pinned_dependencies(tmp_path: Path) -> None:
    """Clone v1.1.0, check its commit, install it editable with the pins, and log the probe version."""
    code, stdout, log, install_dir = _run_install(tmp_path)

    assert code == 0
    assert stdout.splitlines() == ["1.1.0", "after-install"]
    lines = log.splitlines()
    assert (
        f"git -c advice.detachedHead=false clone -q --depth 1 --branch v1.1.0 "
        f"https://github.com/SWE-agent/SWE-agent.git {install_dir}/src"
    ) in lines
    assert f"git -C {install_dir}/src rev-parse HEAD" in lines
    assert f"uv venv -q --python 3.12 {install_dir}/venv" in lines
    assert (
        f"uv pip install -q --python {install_dir}/venv/bin/python --exclude-newer 2026-10-01T00:00:00Z "
        f"-e {install_dir}/src swe-rex==1.4.0 litellm==1.103.1"
    ) in lines


def test_install_skips_when_the_pinned_version_is_present(tmp_path: Path) -> None:
    """Leave a venv whose probe already answers the pinned release alone."""
    _run_install(tmp_path)
    code, stdout, log, _ = _run_install(tmp_path)

    assert code == 0
    assert stdout.splitlines() == ["1.1.0", "after-install"]
    assert log == ""


@pytest.mark.parametrize(("sha", "uv_exit"), [("0" * 40, 0), (SWE_AGENT_COMMIT, 7)])
def test_install_with_a_wrong_commit_or_failed_install_removes_the_folder_and_fails(
    tmp_path: Path, sha: str, uv_exit: int
) -> None:
    """Fail the bootstrap and leave no partial install behind."""
    code, stdout, _, install_dir = _run_install(tmp_path, sha=sha, uv_exit=uv_exit)

    assert code != 0
    assert "after-install" not in stdout
    assert not install_dir.exists()


def test_probe_rejects_a_venv_with_another_litellm_or_swe_rex() -> None:
    """Print the dependency versions instead of the release when they drift from the pins."""
    assert "1.103.1" in _PROBE
    assert "1.4.0" in _PROBE
    assert '"litellm %s swe-rex %s" % d' in _PROBE
    assert "--version" not in _PROBE


def test_open_launch_sends_model_calls_only_to_the_gateway() -> None:
    """Write the gateway URL to a launch file and keep the key out of files and argv."""
    launch = build_launch(_target(), _GATEWAY)

    assert launch.files == {SWE_AGENT_BASE_URL_FILE: _GATEWAY.url}
    assert launch.env["APP_API_KEY"] == launch.env["SKYNET_API_KEY"] == "gateway-token"
    assert launch.env["APP_MODEL"] == launch.env["SKYNET_MODEL"] == "target-model"
    assert "gateway-token" not in launch.run_command
    assert "gateway-token" not in json.dumps(launch.files)
    assert _GATEWAY.url not in launch.run_command
    assert "--agent.model.api_key='$APP_API_KEY'" in launch.run_command
    assert launch.env["HF_HUB_DISABLE_TELEMETRY"] == "1"
    assert launch.env["DO_NOT_TRACK"] == "1"
    assert launch.env["LITELLM_MODE"] == "PRODUCTION"


def test_protected_launch_checks_the_pin_and_blanks_other_provider_keys() -> None:
    """Use the offline probe and blank every other provider key, while the gateway key stays."""
    launch = build_launch(_target(), _GATEWAY, protected=True)

    assert launch.install_command == pinned_harness_check(BLACKBOX_HARNESS_SWE_AGENT)
    blanked = {name for name, value in _PROTECTED_ENV.items() if value == ""}
    assert {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "OPENAI_BASE_URL"} <= blanked
    for name in blanked:
        assert launch.env[name] == ""
    assert launch.env["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"
    assert launch.env["APP_API_KEY"] == "gateway-token"


def test_protected_run_points_swe_agent_at_the_relay_and_prints_only_the_patch(tmp_path: Path) -> None:
    """Run the protected command against a fake sweagent: relay URL, env key and patch come through."""
    result, calls, workspace = _run_with_fake_sweagent(tmp_path, protected=True)

    assert result.returncode == 0
    assert result.stdout == _RECORDED_PATCH
    assert "SWE-agent has produced a patch" in result.stderr
    args = _args(calls)
    assert args[:4] == [
        "run",
        "--agent.model.name=openai/target-model",
        f"--agent.model.api_base={_RELAY}",
        "--agent.model.api_key=$APP_API_KEY",
    ]
    assert _GATEWAY.url not in calls
    assert "--agent.model.top_p=null" in args
    assert "--agent.model.per_instance_cost_limit=0" in args
    assert "--env.repo.type=preexisting" in args
    assert f"--env.repo.repo_name={workspace}" in args
    assert not any(arg.startswith("--agent.model.completion_kwargs") for arg in args)
    assert _logged(calls, "APP_API_KEY") == "gateway-token"
    assert _logged(calls, "OPENAI_API_KEY") == ""
    assert _logged(calls, "ANTHROPIC_API_KEY") == ""
    assert _logged(calls, "ENVFILE_BYTES") == "0"


def test_run_builds_the_task_from_agents_md_and_the_prompt_without_the_shell(tmp_path: Path) -> None:
    """Pass AGENTS.md plus the prompt verbatim, run nothing from them, and remove the run folder."""
    _, calls, workspace = _run_with_fake_sweagent(tmp_path, protected=False)

    task = calls.split("TASK<<\n", 1)[1].split("\n>>TASK", 1)[0]
    assert task == "Keep changes small.\n\nFix `a.txt` and say $(touch pwned).\n"
    assert not (workspace / "pwned").exists()
    workdir = Path(_logged(calls, "WORKDIR"))
    assert not workdir.exists()
    assert not workdir.is_relative_to(workspace)
    assert _logged(calls, "OPENAI_API_KEY") == "sk-inherited"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_run_snapshots_a_plain_workspace_so_the_repo_reset_keeps_its_files(tmp_path: Path) -> None:
    """Commit the task files first, so SWE-agent's ``git clean -fdq`` cannot delete them."""
    _, _, workspace = _run_with_fake_sweagent(tmp_path, protected=False)

    env = _host_env(str(tmp_path))
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=workspace, env=env, capture_output=True, text=True, check=True
    ).stdout.split()
    assert {"a.txt", "AGENTS.md", SWE_AGENT_BASE_URL_FILE} <= set(tracked)
    diff = subprocess.run(["git", "diff"], cwd=workspace, env=env, capture_output=True, text=True, check=True).stdout
    assert "+fixed" in diff


@pytest.mark.parametrize("effort", _EFFORTS)
def test_reasoning_effort_passes_unchanged(tmp_path: Path, effort: str) -> None:
    """Send every Skynet effort level to LiteLLM as is; an active one also sets temperature 1."""
    _, calls, _ = _run_with_fake_sweagent(tmp_path, protected=False, reasoning_effort=effort)

    args = _args(calls)
    assert f'--agent.model.completion_kwargs={{"reasoning_effort": "{effort}"}}' in args
    assert ("--agent.model.temperature=1" in args) is (effort != "none")


@pytest.mark.parametrize("fake_exit", [0, 3])
def test_run_keeps_the_exit_status_and_still_prints_the_patch(tmp_path: Path, fake_exit: int) -> None:
    """Return SWE-agent's exit status and the saved patch."""
    result, _, _ = _run_with_fake_sweagent(tmp_path, protected=False, fake_exit=fake_exit)

    assert result.returncode == fake_exit
    assert result.stdout == _RECORDED_PATCH


@pytest.mark.skipif(os.access("/root", os.W_OK), reason="/root is writable on this host")
def test_run_stops_with_a_clear_message_without_a_writable_root(tmp_path: Path) -> None:
    """Fail before SWE-agent starts when /root is not writable."""
    launch = build_launch(_target(), _GATEWAY)
    result = subprocess.run(
        [shutil.which("bash") or "/bin/sh", "-c", launch.run_command],
        cwd=tmp_path,
        env={"PATH": os.defpath, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert "needs a writable /root" in result.stderr


def test_recorded_patch_parses_to_the_answer_and_the_payload_names_the_plain_parser() -> None:
    """Parse the recorded patch with the plain parser the payload names."""
    payload = launch_payload(build_launch(_target(), _GATEWAY))

    assert payload["output_format"] == "plain"
    assert parse_output("plain", _RECORDED_PATCH) == (_RECORDED_PATCH.strip(), {})
    assert parse_output("plain", "") == (None, {})

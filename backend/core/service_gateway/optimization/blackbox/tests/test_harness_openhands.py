"""Tests for the OpenHands harness: registry entry, pinned install, gateway-only launch, protected env and output parsing."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from core.models.blackbox import BLACKBOX_HARNESS_OPENHANDS, BLACKBOX_HARNESSES, BLACKBOX_TARGET_AGENT, BlackboxTarget

from ..harness import GatewayConfig, build_launch, launch_payload, pinned_harness_check
from ..harness_bridge import parse_output, parse_plain_output
from ..harnesses import REGISTRY
from ..harnesses.openhands import _PROTECTED_ENV, OPENHANDS_BASE_URL_FILE, OPENHANDS_VERSION

_GATEWAY = GatewayConfig(url="https://gw.example/v1", api_key="gateway-token")
_RELAY = "http://127.0.0.1:3210/v1"
_PROVIDER_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "XAI_API_KEY")
# Synthetic tail of a headless ``openhands.core.main`` log with stderr merged.
_RECORDED_STDOUT = (
    "\n12:00:01 - openhands:INFO: main.py:120 - Agent Controller Initialized\n"
    "AgentFinishAction: I fixed the off-by-one error in solution.py. The answer is 42.\n\n"
)
# Stand-in for the ``openhands`` wrapper: it records each call, writes to both
# streams, and checks that its per-run root exists.
_FAKE_OPENHANDS = """#!/bin/sh
log="$FAKE_LOG"
{
  printf 'ARGS'; for a in "$@"; do printf ' [%s]' "$a"; done; printf '\\n'
  printf 'LLM_BASE_URL=%s\\n' "$LLM_BASE_URL"
  printf 'LLM_API_KEY=%s\\n' "$LLM_API_KEY"
  printf 'LLM_MODEL=%s\\n' "$LLM_MODEL"
  printf 'OPENAI_API_KEY=%s\\n' "${OPENAI_API_KEY-unset}"
  printf 'SANDBOX_VOLUMES=%s\\n' "$SANDBOX_VOLUMES"
  printf 'FILE_STORE_PATH=%s\\n' "$FILE_STORE_PATH"
  printf 'CACHE_DIR=%s\\n' "$CACHE_DIR"
  printf 'ROOT=%s\\n' "$OPENHANDS_ROOT"
  if [ -d "$OPENHANDS_ROOT" ]; then echo 'ROOT_DIR=yes'; else echo 'ROOT_DIR=no'; fi
} >> "$log"
printf '%s' "$FAKE_ANSWER" >&2
exit "${FAKE_EXIT:-0}"
"""
# Stand-ins for the install step's tools. ``uv venv`` makes a fake venv
# python that answers ``--version`` like the real entry point; ``uv pip``
# logs its arguments.
_FAKE_INSTALL_TOOLS = {
    "uv": (
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$FAKE_LOG"\n'
        'if [ "$1" = venv ]; then for v; do :; done; mkdir -p "$v/bin"; '
        'printf \'#!/bin/sh\\necho "OpenHands version: 1.6.0"\\n\' > "$v/bin/python"; '
        'chmod 755 "$v/bin/python"; fi\n'
        'exit "${FAKE_UV_EXIT:-0}"\n'
    ),
    "curl": '#!/bin/sh\necho curl >> "$FAKE_LOG"\n',
}


def _target() -> BlackboxTarget:
    """Build an OpenHands agent target.

    Returns:
        The target.
    """
    return BlackboxTarget(kind=BLACKBOX_TARGET_AGENT, harness=BLACKBOX_HARNESS_OPENHANDS, model="target-model")


def _run_with_fake_openhands(
    tmp_path: Path, *, protected: bool, fake_exit: int = 0, reasoning_effort: str | None = None
) -> tuple[str, str, Path, int]:
    """Run the built OpenHands command in a scratch workspace against a fake ``openhands`` binary.

    The env also carries an inherited ``LLM_BASE_URL`` and ``LLM_API_KEY``,
    which the run must replace so they cannot override the gateway.

    Args:
        tmp_path: Scratch directory for the workspace, the fake binary and its log.
        protected: Build the protected launch, with the sandbox-local relay set.
        fake_exit: Exit status the fake binary returns.
        reasoning_effort: The user's effort for the model, or ``None``.

    Returns:
        The command's stdout, the fake binary's call log, the workspace, and
        the command's exit status.
    """
    launch = build_launch(_target(), _GATEWAY, protected=protected, reasoning_effort=reasoning_effort)
    workspace = tmp_path / "work"
    for name, content in launch.files.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (workspace / "task").mkdir(parents=True)
    (workspace / "task" / "PROMPT.md").write_text("Fix `solution.py` and say $(echo done).")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "openhands"
    fake.write_text(_FAKE_OPENHANDS)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    env = {
        **launch.env,
        "PATH": f"{bin_dir}{os.pathsep}{os.defpath}",
        "HOME": str(tmp_path / "home"),
        "LLM_BASE_URL": "https://api.openai.com/v1",
        "LLM_API_KEY": "inherited-key",
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


def _run_install(tmp_path: Path, *, uv_exit: int = 0) -> tuple[int, str, str]:
    """Run the open install command under ``set -eu`` with fake ``uv`` and ``curl``.

    Args:
        tmp_path: Scratch directory for the fake tools and ``HOME``.
        uv_exit: Exit status the fake ``uv`` returns.

    Returns:
        The exit status, the stdout, and the fake tools' call log.
    """
    install = build_launch(_target(), _GATEWAY).install_command
    assert install is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, script in _FAKE_INSTALL_TOOLS.items():
        tool = bin_dir / name
        tool.write_text(script)
        tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "tools.log"
    log.write_text("")
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.defpath}",
        "HOME": str(tmp_path / "home"),
        "FAKE_LOG": str(log),
        "FAKE_UV_EXIT": str(uv_exit),
    }
    result = subprocess.run(
        [shutil.which("bash") or "/bin/sh", "-c", f"set -eu; {install}; echo after-install"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result.returncode, result.stdout, log.read_text()


def _logged(calls: str, name: str) -> str:
    """Return the value the fake binary logged for ``name``.

    Args:
        calls: The fake binary's call log.
        name: The logged variable name.

    Returns:
        The logged value.
    """
    return next(line for line in calls.splitlines() if line.startswith(f"{name}=")).split("=", 1)[1]


def test_openhands_is_registered_after_aider_with_its_pin() -> None:
    """List OpenHands in the registry and the request model, pinned to the exact PyPI release."""
    spec = REGISTRY[BLACKBOX_HARNESS_OPENHANDS]
    assert (spec.binary, spec.version, spec.output_format, spec.position) == ("openhands", "1.6.0", "plain", 54)
    assert list(REGISTRY).index(BLACKBOX_HARNESS_OPENHANDS) == list(REGISTRY).index("aider") + 1
    assert BLACKBOX_HARNESS_OPENHANDS in BLACKBOX_HARNESSES
    assert (
        pinned_harness_check(BLACKBOX_HARNESS_OPENHANDS)
        == "openhands --version 2>/dev/null | grep -Eq '(^| )1[.]6[.]0( |$)'"
    )


def test_open_install_pins_uv_and_the_package() -> None:
    """Install the pinned uv and ``openhands-ai`` release into a virtualenv, without the VS Code build."""
    install = build_launch(_target(), _GATEWAY).install_command

    assert install is not None
    assert install.startswith(f'export PATH="$HOME/.local/bin":"$PATH"; {pinned_harness_check("openhands")} || ')
    assert "https://astral.sh/uv/0.13.7/install.sh" in install
    assert f"openhands-ai=={OPENHANDS_VERSION}" in install
    assert "SKIP_VSCODE_BUILD=true uv pip install" in install
    assert install.endswith('|| exit "$?"; }; openhands --version')
    assert "sudo" not in install


def test_install_writes_a_wrapper_that_passes_the_pin_check(tmp_path: Path) -> None:
    """Create the venv, install the package into it, and write an ``openhands`` wrapper the pin check accepts."""
    code, stdout, calls = _run_install(tmp_path)

    assert code == 0
    assert stdout.splitlines() == ["OpenHands version: 1.6.0", "after-install"]
    venv = tmp_path / "home" / ".local" / "share" / "openhands-venv"
    assert f"venv --clear --python 3.12 {venv}" in calls
    assert f"pip install --python {venv}/bin/python openhands-ai==1.6.0" in calls
    assert "curl" not in calls
    wrapper = tmp_path / "home" / ".local" / "bin" / "openhands"
    assert wrapper.read_text() == f'#!/bin/sh\nexec "{venv}/bin/python" -m openhands.core.main "$@"\n'


def test_failed_package_install_fails_the_install(tmp_path: Path) -> None:
    """Stop the caller's script and write no wrapper when uv fails."""
    code, stdout, _ = _run_install(tmp_path, uv_exit=2)

    assert code == 2
    assert "after-install" not in stdout
    assert not (tmp_path / "home" / ".local" / "bin" / "openhands").exists()


def test_open_launch_sends_model_calls_only_to_the_gateway() -> None:
    """Read the gateway URL from a file, pass the gateway token, and keep provider keys out of the env."""
    launch = build_launch(_target(), _GATEWAY)

    assert launch.instructions_file == "AGENTS.md"
    assert launch.files == {OPENHANDS_BASE_URL_FILE: _GATEWAY.url}
    assert _GATEWAY.url not in launch.run_command
    assert _GATEWAY.api_key not in launch.run_command
    assert _GATEWAY.api_key not in "".join(launch.files.values())
    assert (
        f'LLM_BASE_URL="$(cat {OPENHANDS_BASE_URL_FILE})" LLM_API_KEY="$APP_API_KEY" LLM_MODEL="openai/$APP_MODEL" '
    ) in launch.run_command
    assert 'openhands --config-file "$OPENHANDS_ROOT/config.toml" -f "$APP_PROMPT_FILE" 2>&1 < /dev/null' in (
        launch.run_command
    )
    assert "--task" not in launch.run_command
    assert launch.env["APP_API_KEY"] == _GATEWAY.api_key
    assert launch.env["RUNTIME"] == "local"
    assert launch.env["AGENT_ENABLE_PROMPT_EXTENSIONS"] == "true"
    assert launch.env["AGENT_ENABLE_BROWSING"] == "false"
    assert launch.env["ENABLE_BROWSER"] == "false"
    assert "LLM_REASONING_EFFORT" not in launch.env
    assert not set(_PROVIDER_KEYS) & set(launch.env)
    assert launch.parse_output is parse_plain_output


@pytest.mark.parametrize(
    ("effort", "level"),
    [(None, None), ("minimal", "low"), ("medium", "medium"), ("xhigh", "high"), ("max", "high"), ("turbo", None)],
)
def test_reasoning_effort_maps_to_the_litellm_level(effort: str | None, level: str | None) -> None:
    """Pass the user's effort as ``LLM_REASONING_EFFORT``, or nothing for the model default."""
    launch = build_launch(_target(), _GATEWAY, reasoning_effort=effort)
    assert launch.env.get("LLM_REASONING_EFFORT") == level


def test_protected_launch_checks_the_pin_offline_and_blocks_other_providers() -> None:
    """Use the offline version probe, blank other provider keys and keep the gateway key."""
    launch = build_launch(_target(), _GATEWAY, protected=True)

    assert launch.install_command == pinned_harness_check(BLACKBOX_HARNESS_OPENHANDS)
    blanked = [name for name, value in _PROTECTED_ENV.items() if value == ""]
    assert set(_PROVIDER_KEYS) | {"SEARCH_API_KEY", "AZURE_API_KEY"} <= set(blanked)
    for name in blanked:
        assert launch.env[name] == ""
    assert launch.env["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"
    assert launch.env["APP_API_KEY"] == _GATEWAY.api_key


def test_protected_run_points_openhands_at_the_relay_and_returns_its_log(tmp_path: Path) -> None:
    """Run the protected command against a fake binary: the relay URL, gateway token, prompt file and log come through."""
    stdout, calls, workspace, code = _run_with_fake_openhands(tmp_path, protected=True)

    assert code == 0
    (run,) = [line for line in calls.splitlines() if line.startswith("ARGS")]
    root = _logged(calls, "ROOT")
    assert run == f"ARGS [--config-file] [{root}/config.toml] [-f] [task/PROMPT.md]"
    assert _logged(calls, "LLM_BASE_URL") == _RELAY
    assert _GATEWAY.url not in calls
    assert _logged(calls, "LLM_API_KEY") == "gateway-token"
    assert _logged(calls, "LLM_MODEL") == "openai/target-model"
    assert _logged(calls, "OPENAI_API_KEY") == ""
    assert _logged(calls, "SANDBOX_VOLUMES") == f"{workspace}:/workspace:rw"
    assert _logged(calls, "FILE_STORE_PATH") == f"{root}/store"
    assert _logged(calls, "CACHE_DIR") == f"{root}/cache"
    assert _logged(calls, "ROOT_DIR") == "yes"
    assert not Path(root).exists()
    assert not Path(root).is_relative_to(workspace.resolve())
    assert parse_output("plain", stdout) == (_RECORDED_STDOUT.strip(), {})


def test_open_run_reads_the_gateway_url_from_the_generated_file(tmp_path: Path) -> None:
    """Without a relay, point OpenHands at the gateway URL the launch wrote."""
    _, calls, _, _ = _run_with_fake_openhands(tmp_path, protected=False)

    assert _logged(calls, "LLM_BASE_URL") == _GATEWAY.url
    assert _logged(calls, "LLM_API_KEY") == "gateway-token"


@pytest.mark.parametrize("fake_exit", [0, 3])
def test_run_removes_the_root_and_keeps_the_exit_status_and_output(tmp_path: Path, fake_exit: int) -> None:
    """Remove the per-run root when the shell exits, after a good or failed run, without changing the result."""
    stdout, calls, _, code = _run_with_fake_openhands(tmp_path, protected=False, fake_exit=fake_exit)

    assert code == fake_exit
    assert stdout == _RECORDED_STDOUT
    assert _logged(calls, "ROOT_DIR") == "yes"
    assert not Path(_logged(calls, "ROOT")).exists()


def test_recorded_output_parses_to_the_log_and_the_payload_names_the_plain_parser() -> None:
    """Parse a recorded log to its stripped text, and serialize the launch with the bridge's plain parser."""
    assert parse_plain_output(_RECORDED_STDOUT) == (_RECORDED_STDOUT.strip(), {})
    assert parse_plain_output("  \n") == (None, {})
    assert launch_payload(build_launch(_target(), _GATEWAY))["output_format"] == "plain"

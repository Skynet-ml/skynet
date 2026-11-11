"""Tests for the Code Puppy harness: registry entry, gateway-only launch, protected env and output parsing."""

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

from core.models.blackbox import BLACKBOX_HARNESS_CODE_PUPPY, BLACKBOX_HARNESSES, BLACKBOX_TARGET_AGENT, BlackboxTarget

from .. import repo_tree
from ..harness import GatewayConfig, build_launch, launch_payload, pinned_harness_check
from ..harness_bridge import parse_output, parse_plain_output, workspace_files
from ..harnesses import REGISTRY
from ..harnesses.code_puppy import CODE_PUPPY_MODELS_FILE, CODE_PUPPY_VERSION

_GATEWAY = GatewayConfig(url="https://gw.example/v1", api_key="gateway-token")
_RELAY = "http://127.0.0.1:3210/v1"
_PROVIDER_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY")
# Synthetic one-shot transcript in the shape of ``code-puppy --prompt`` plain output.
_RECORDED_STDOUT = "\nI read solution.py and fixed the off-by-one error.\n\nThe answer is 42.\n\n"
# Stand-in for Code Puppy: it records its call and seeded config, writes a
# session and a cache under its XDG dirs like the real CLI, makes one code
# change when the workspace has ``main.py``, and writes the usage file unless
# FAKE_FAIL is set (like a swallowed model error).
_FAKE_CODE_PUPPY = """#!/bin/sh
{
  printf 'ARGS'; for a in "$@"; do printf ' [%s]' "$a"; done; printf '\\n'
  printf 'CODE_PUPPY_API_KEY=%s\\n' "$CODE_PUPPY_API_KEY"
  printf 'OPENAI_API_KEY=%s\\n' "$OPENAI_API_KEY"
  printf 'ANTHROPIC_API_KEY=%s\\n' "$ANTHROPIC_API_KEY"
  printf 'GEMINI_API_KEY=%s\\n' "$GEMINI_API_KEY"
  printf 'NO_VERSION_UPDATE=%s\\n' "$NO_VERSION_UPDATE"
  printf 'CFG=%s\\n' "$XDG_CONFIG_HOME/code_puppy/puppy.cfg"
  printf 'MODELS=%s\\n' "$XDG_DATA_HOME/code_puppy/extra_models.json"
  printf 'MODEL_URL=%s\\n' "$(grep -o '"url": "[^"]*"' "$XDG_DATA_HOME/code_puppy/extra_models.json")"
  printf 'CFG_BEGIN\\n'; cat "$XDG_CONFIG_HOME/code_puppy/puppy.cfg"; printf 'CFG_END\\n'
} >> "$FAKE_LOG"
[ -s "$XDG_CONFIG_HOME/code_puppy/puppy.cfg" ] || exit 3
[ -s "$XDG_DATA_HOME/code_puppy/extra_models.json" ] || exit 4
mkdir -p "$XDG_DATA_HOME/code_puppy/sessions" "$XDG_CACHE_HOME/code_puppy"
printf 'USER: the prompt\\n' > "$XDG_DATA_HOME/code_puppy/sessions/s1.json"
printf 'cache' > "$XDG_CACHE_HOME/code_puppy/c.bin"
if [ -f main.py ]; then echo 'print(2)' > main.py; fi
printf '%s' "$FAKE_ANSWER"
if [ -z "$FAKE_FAIL" ]; then
  while [ "$#" -gt 0 ]; do
    if [ "$1" = "--usage-file" ]; then printf '{"requests": 1}' > "$2"; fi
    shift
  done
fi
exit 0
"""


def _target() -> BlackboxTarget:
    """Build a Code Puppy agent target.

    Returns:
        The target.
    """
    return BlackboxTarget(kind=BLACKBOX_TARGET_AGENT, harness=BLACKBOX_HARNESS_CODE_PUPPY, model="target-model")


def _run_with_fake_code_puppy(
    tmp_path: Path,
    *,
    protected: bool,
    prompt: str = "Fix `solution.py` and say $(echo done).",
    fail: bool = False,
    reasoning_effort: str | None = None,
    workspace: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], str, Path]:
    """Run the built Code Puppy command in a scratch workspace against a fake ``code-puppy`` binary.

    Args:
        tmp_path: Scratch directory for the workspace, the fake binary and its log.
        protected: Build the protected launch, with the sandbox-local relay set.
        prompt: Task prompt written to the prompt file.
        fail: Make the fake skip the usage file, as Code Puppy does after a model error.
        reasoning_effort: The user's effort for the model.
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
    (workspace / "task" / "PROMPT.md").write_text(prompt)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "code-puppy"
    fake.write_text(_FAKE_CODE_PUPPY)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    env = {
        **launch.env,
        "PATH": f"{bin_dir}{os.pathsep}{os.defpath}",
        "HOME": str(tmp_path / "home"),
        "FAKE_LOG": str(log),
        "FAKE_ANSWER": _RECORDED_STDOUT,
    }
    if fail:
        env["FAKE_FAIL"] = "1"
    if protected:
        env["APP_BUDGET_RELAY_URL"] = _RELAY
    result = subprocess.run(
        launch.run_command, shell=True, cwd=workspace, env=env, capture_output=True, text=True, timeout=10, check=False
    )
    return result, log.read_text(), workspace


def test_code_puppy_is_registered_last_with_its_pin() -> None:
    """List Code Puppy in the registry and the request model, pinned to the exact PyPI release."""
    spec = REGISTRY[BLACKBOX_HARNESS_CODE_PUPPY]
    assert (spec.binary, spec.version, spec.output_format) == ("code-puppy", "0.0.967", "plain")
    assert list(REGISTRY)[-1] == BLACKBOX_HARNESS_CODE_PUPPY
    assert BLACKBOX_HARNESSES[-2:] == (BLACKBOX_HARNESS_CODE_PUPPY, "custom")
    assert (
        pinned_harness_check(BLACKBOX_HARNESS_CODE_PUPPY)
        == "code-puppy --version 2>/dev/null | grep -Eq '(^| )0[.]0[.]967( |$)'"
    )


def test_open_launch_installs_the_pin_and_sends_model_calls_only_to_the_gateway() -> None:
    """Install the pinned package, point the only model at the gateway, and blank every provider key."""
    launch = build_launch(_target(), _GATEWAY)

    assert launch.instructions_file == "AGENTS.md"
    assert launch.install_command is not None
    assert f"uv tool install --force --python 3.13 code-puppy=={CODE_PUPPY_VERSION}" in launch.install_command
    assert "astral.sh/uv/0.13.7/install.sh" in launch.install_command
    assert set(launch.files) == {CODE_PUPPY_MODELS_FILE}
    assert json.loads(launch.files[CODE_PUPPY_MODELS_FILE]) == {
        "app": {
            "type": "custom_openai",
            "name": "target-model",
            "custom_endpoint": {"url": _GATEWAY.url, "api_key": "$CODE_PUPPY_API_KEY"},
        }
    }
    for content in launch.files.values():
        assert _GATEWAY.api_key not in content
    assert _GATEWAY.url not in launch.run_command
    assert _GATEWAY.api_key not in launch.run_command
    assert (
        'CODE_PUPPY_API_KEY="$APP_API_KEY" code-puppy --disable-ask-user-question '
        '--usage-file "$_cp_dir/usage.json" --model app --prompt "$_cp_prompt" < /dev/null'
    ) in launch.run_command
    assert launch.env["APP_API_KEY"] == _GATEWAY.api_key
    assert {name: launch.env[name] for name in _PROVIDER_KEYS} == dict.fromkeys(_PROVIDER_KEYS, "")
    assert "CODE_PUPPY_API_KEY" not in launch.env
    assert launch.parse_output is parse_plain_output


@pytest.mark.parametrize(
    ("effort", "line"),
    [
        (None, None),
        ("none", None),
        ("high", "model_settings_app_reasoning_effort = high"),
        ("max", "model_settings_app_reasoning_effort = max"),
        ("minimal", "model_settings_app_reasoning_effort = minimal"),
    ],
)
def test_reasoning_effort_goes_to_the_config_only_when_code_puppy_knows_it(
    tmp_path: Path, effort: str | None, line: str | None
) -> None:
    """Write the user's effort to ``puppy.cfg`` and advertise it on the model for Code Puppy's effort names only."""
    result, calls, workspace = _run_with_fake_code_puppy(tmp_path, protected=False, reasoning_effort=effort)

    assert result.returncode == 0, result.stderr
    cfg = calls.split("CFG_BEGIN\n", 1)[1].split("CFG_END\n", 1)[0]
    assert cfg.startswith(
        "[puppy]\npuppy_name = Puppy\nowner_name = User\nauto_save_session = false\nyolo_mode = true\n"
    )
    model_entry = json.loads((workspace / CODE_PUPPY_MODELS_FILE).read_text())["app"]
    if line is None:
        assert "reasoning_effort" not in cfg
        assert "setting_choices" not in model_entry
    else:
        assert cfg.endswith(f"{line}\n")
        assert model_entry["setting_choices"] == {"reasoning_effort": [effort]}


def test_protected_launch_checks_the_pin_offline_and_blocks_other_providers() -> None:
    """Use the offline version probe, blank every other provider key, and stop the update check."""
    launch = build_launch(_target(), _GATEWAY, protected=True)

    assert launch.install_command == pinned_harness_check(BLACKBOX_HARNESS_CODE_PUPPY)
    for name in _PROVIDER_KEYS:
        assert launch.env[name] == ""
    assert launch.env["NO_VERSION_UPDATE"] == "1"
    assert launch.env["DO_NOT_TRACK"] == "1"
    assert launch.env["APP_API_KEY"] == _GATEWAY.api_key


def test_protected_run_points_code_puppy_at_the_relay_and_returns_its_answer(tmp_path: Path) -> None:
    """Run the protected command against a fake Code Puppy: the relay URL, gateway token and answer come through."""
    result, calls, workspace = _run_with_fake_code_puppy(tmp_path, protected=True)

    assert result.returncode == 0, result.stderr
    (run,) = [line for line in calls.splitlines() if line.startswith("ARGS")]
    (usage_arg,) = [arg for arg in run.split("] [") if arg.endswith("/usage.json")]
    assert run == (
        f"ARGS [--disable-ask-user-question] [--usage-file] [{usage_arg}] [--model] [app] "
        "[--prompt] [Fix `solution.py` and say $(echo done).]"
    )
    assert "CODE_PUPPY_API_KEY=gateway-token" in calls
    for name in _PROVIDER_KEYS:
        assert f"{name}=\n" in calls
    assert "NO_VERSION_UPDATE=1" in calls
    for prefix in ("CFG=", "MODELS="):
        (path,) = [line[len(prefix) :] for line in calls.splitlines() if line.startswith(prefix)]
        assert not Path(path).resolve().is_relative_to(workspace.resolve())
        assert not Path(path).exists(), "the run removes its temporary Code Puppy folder"
    assert f'MODEL_URL="url": "{_RELAY}"' in calls
    assert parse_output("plain", result.stdout) == (
        "I read solution.py and fixed the off-by-one error.\n\nThe answer is 42.",
        {},
    )


def test_open_run_keeps_the_gateway_url(tmp_path: Path) -> None:
    """Without a relay, leave the gateway URL the launch wrote in the model file."""
    result, calls, _ = _run_with_fake_code_puppy(tmp_path, protected=False)

    assert result.returncode == 0, result.stderr
    assert f'MODEL_URL="url": "{_GATEWAY.url}"' in calls


@pytest.mark.parametrize("prompt", ["/help me fix it", "  !rm -rf build"])
def test_command_like_prompts_are_framed_as_a_task(tmp_path: Path, prompt: str) -> None:
    """Prefix a prompt that Code Puppy would read as a slash or shell command, so it reaches the model."""
    _, calls, _ = _run_with_fake_code_puppy(tmp_path, protected=False, prompt=prompt)

    assert f"[--prompt] [Task request:\n{prompt}]" in calls


def test_run_fails_when_code_puppy_writes_no_usage_file(tmp_path: Path) -> None:
    """Turn Code Puppy's exit 0 after a swallowed model error into a failed run."""
    result, _, _ = _run_with_fake_code_puppy(tmp_path, protected=False, fail=True)

    assert result.returncode == 1
    assert "its usage file was not written" in result.stderr


def test_recorded_output_parses_to_the_answer_and_the_payload_names_the_plain_parser() -> None:
    """Parse a recorded transcript to its stripped text, and serialize the launch with the bridge's plain parser."""
    assert parse_plain_output(_RECORDED_STDOUT) == (
        "I read solution.py and fixed the off-by-one error.\n\nThe answer is 42.",
        {},
    )
    assert parse_plain_output("  \n") == (None, {})
    assert launch_payload(build_launch(_target(), _GATEWAY))["output_format"] == "plain"


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
@pytest.mark.parametrize("protected", [False, True])
def test_version_patch_holds_only_the_code_change(tmp_path: Path, protected: bool) -> None:
    """Code Puppy's config, sessions, caches, usage file and model file never join a repo-mode version patch."""
    checkout = _shipped_tree(tmp_path)

    result, _, _ = _run_with_fake_code_puppy(tmp_path, protected=protected, workspace=checkout)
    assert result.returncode == 0, result.stderr
    # The bridge writes and removes its own prompt file; only Code Puppy's files are under test.
    shutil.rmtree(checkout / "task")

    patch = repo_tree.version_patch(checkout)
    assert [line for line in patch.splitlines() if line.startswith("diff --git")] == ["diff --git a/main.py b/main.py"]
    assert "+print(2)" in patch
    assert "the prompt" not in patch

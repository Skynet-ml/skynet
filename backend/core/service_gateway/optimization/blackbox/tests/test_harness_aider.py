"""Tests for the Aider harness: registry entry, launch, protected build and output parsing."""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from core.models.blackbox import BLACKBOX_TARGET_AGENT, BlackboxTarget

from .. import repo_tree
from ..harness import GatewayConfig, build_launch, launch_payload, pinned_harness_check
from ..harness_bridge import parse_plain_output, workspace_files
from ..harnesses import REGISTRY
from ..harnesses.aider import (
    _PROTECTED_ENV,
    _SNAPSHOT_UNTRACKED_WORKSPACE,
    AIDER_CONFIG_FILE,
    AIDER_UV_VERSION,
    AIDER_VERSION,
    SPEC,
)

_GATEWAY = GatewayConfig(url="https://gw.example/v1", api_key="secret-key")
_RELAY_URL = "http://127.0.0.1:3210/v1"
# A synthetic recording of Aider 0.86 stdout in scripting mode with --no-pretty.
_RECORDED_STDOUT = """\
Aider v0.86.2
Model: openai/target-model with whole edit format
Git repo: .git with 3 files
Repo-map: disabled
Added AGENTS.md to the chat (read-only).

The answer is 42.

Tokens: 1.2k sent, 18 received.
"""


def _target(**overrides: object) -> BlackboxTarget:
    """Build a validated Aider agent target.

    Args:
        **overrides: Extra target fields.

    Returns:
        The target.
    """
    fields: dict[str, object] = {
        "kind": BLACKBOX_TARGET_AGENT,
        "harness": "aider",
        "model": "target-model",
    }
    fields.update(overrides)
    return BlackboxTarget(**fields)


def _config(content: str) -> dict[str, object]:
    """Parse the generated config, whose values are all JSON scalars or lists.

    Args:
        content: The YAML text.

    Returns:
        The settings by key.
    """
    settings: dict[str, object] = {}
    for line in content.splitlines():
        key, _, value = line.partition(": ")
        settings[key] = json.loads(value)
    return settings


# Aider's defaults for the files it writes at the repository root when the
# config names no other path.
_AIDER_DEFAULT_FILES = {
    "chat-history-file": ".aider.chat.history.md",
    "input-history-file": ".aider.input.history",
}


def _fake_aider_run(checkout: Path, settings: dict[str, object]) -> None:
    """Write the files Aider writes for these settings, then make one real code change.

    Args:
        checkout: The workspace Aider runs in.
        settings: The parsed Aider config.
    """
    files = {key: settings.get(key, default) for key, default in _AIDER_DEFAULT_FILES.items()}
    files["llm-history-file"] = settings.get("llm-history-file")
    for path in files.values():
        if path:
            with (checkout / str(path)).open("a", encoding="utf-8") as stream:
                stream.write("USER: the prompt\nASSISTANT: the reply\n")
    if settings.get("map-tokens") != 0:
        cache = checkout / ".aider.tags.cache.v4"
        cache.mkdir(exist_ok=True)
        (cache / "cache.db").write_bytes(b"tags")
    (checkout / "main.py").write_text("print(2)\n")


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


def test_registry_lists_aider_with_its_pin_and_plain_parser() -> None:
    """Aider registers under its own id with a pinned release and the plain parser."""
    assert REGISTRY["aider"] is SPEC
    assert SPEC.binary == "aider"
    assert SPEC.version == AIDER_VERSION == "0.86.2"
    assert SPEC.output_format == "plain"


def test_launch_points_aider_at_the_gateway_through_its_config_file() -> None:
    """The gateway URL is in the config file, the key comes from the env, and the run is non-interactive."""
    launch = build_launch(_target(), _GATEWAY)

    settings = _config(launch.files[AIDER_CONFIG_FILE])
    assert settings["openai-api-base"] == _GATEWAY.url
    assert settings["read"] == ["AGENTS.md"]
    assert settings["yes-always"] is True
    assert settings["auto-commits"] is False
    assert settings["analytics-disable"] is True
    assert settings["check-update"] is False
    assert "reasoning-effort" not in settings
    assert launch.instructions_file == "AGENTS.md"
    assert f"aider --config {AIDER_CONFIG_FILE} " in launch.run_command
    assert '--model "openai/$APP_MODEL"' in launch.run_command
    assert '--message-file "$APP_PROMPT_FILE"' in launch.run_command
    assert 'OPENAI_API_KEY="$APP_API_KEY" aider' in launch.run_command
    assert launch.parse_output is parse_plain_output


def test_launch_never_writes_the_gateway_key_or_a_provider_key() -> None:
    """Only the gateway token reaches the box, and only through APP_API_KEY."""
    launch = build_launch(_target(), _GATEWAY)

    assert _GATEWAY.api_key not in launch.run_command
    assert _GATEWAY.api_key not in launch.install_command
    assert all(_GATEWAY.api_key not in content for content in launch.files.values())
    assert launch.env["APP_API_KEY"] == _GATEWAY.api_key
    assert launch.env["APP_MODEL"] == "target-model"
    assert not {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"} & set(launch.env)


def test_install_pins_uv_and_aider() -> None:
    """The open install skips work when the pin is present and otherwise installs that exact release."""
    install = build_launch(_target(), _GATEWAY).install_command

    assert "aider --version" in install
    assert "0[.]86[.]2" in install
    assert f"https://astral.sh/uv/{AIDER_UV_VERSION}/install.sh" in install
    assert f"aider-chat=={AIDER_VERSION}" in install
    assert "aider.chat/install.sh" not in install


def test_reasoning_effort_reaches_the_config_and_skips_the_metadata_check() -> None:
    """A chosen effort is set for the model, and Aider does not drop it for an unknown gateway id."""
    launch = build_launch(_target(), _GATEWAY, reasoning_effort="high")

    settings = _config(launch.files[AIDER_CONFIG_FILE])
    assert settings["reasoning-effort"] == "high"
    assert settings["check-model-accepts-settings"] is False


def test_protected_build_uses_the_pinned_check_env_flags_and_relay(tmp_path: Path) -> None:
    """A protected launch checks the preinstalled pin, blocks network side paths, and rewrites the gateway URL."""
    launch = build_launch(_target(run_command="true"), _GATEWAY, protected=True)

    assert launch.install_command == pinned_harness_check("aider")
    assert "uv tool install" not in launch.install_command
    for key, value in _PROTECTED_ENV.items():
        assert launch.env[key] == value
    for name, content in launch.files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    subprocess.run(
        launch.run_command,
        shell=True,
        cwd=tmp_path,
        env={"PATH": os.defpath, "APP_BUDGET_RELAY_URL": _RELAY_URL},
        timeout=10,
        check=True,
    )
    settings = _config((tmp_path / AIDER_CONFIG_FILE).read_text())
    assert settings["openai-api-base"] == _RELAY_URL


def test_open_build_keeps_the_protected_env_out() -> None:
    """The default launch is unchanged by the protected-only flags."""
    launch = build_launch(_target(), _GATEWAY)

    assert not set(_PROTECTED_ENV) & set(launch.env)


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_snapshot_commits_a_plain_workspace_once(tmp_path: Path) -> None:
    """A workspace that is not a git repo is committed so Aider can see its files; a repo is left alone."""
    (tmp_path / "task.txt").write_text("files before the run\n")
    env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1"}

    for _ in range(2):
        subprocess.run(["sh", "-c", _SNAPSHOT_UNTRACKED_WORKSPACE + "true"], cwd=tmp_path, env=env, check=True)

    log = subprocess.run(
        ["git", "log", "--format=%an <%ae> %s"], cwd=tmp_path, env=env, check=True, capture_output=True, text=True
    )
    assert log.stdout.splitlines() == ["Agent <agent@localhost> Task files before agent run"]
    tracked = subprocess.run(["git", "ls-files"], cwd=tmp_path, env=env, check=True, capture_output=True, text=True)
    assert tracked.stdout.split() == ["task.txt"]


def test_plain_parser_keeps_the_recorded_aider_transcript() -> None:
    """The bridge keeps Aider's stdout as the answer and reports no usage."""
    launch = build_launch(_target(), _GATEWAY)

    answer, usage = launch.parse_output(_RECORDED_STDOUT)
    assert answer == _RECORDED_STDOUT.strip()
    assert "The answer is 42." in answer
    assert usage == {}
    assert launch.parse_output("") == (None, {})


def test_launch_payload_names_the_plain_parser() -> None:
    """The sandbox bridge receives the plain parser name for Aider."""
    payload = launch_payload(build_launch(_target(), _GATEWAY))

    assert payload["output_format"] == "plain"
    assert payload["files"][AIDER_CONFIG_FILE]


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_version_patch_holds_only_the_code_change(tmp_path: Path) -> None:
    """Aider's config, instructions, histories and repo-map cache never join a repo-mode version patch."""
    checkout = _shipped_tree(tmp_path)
    payload = launch_payload(build_launch(_target(), _GATEWAY))
    repo_tree.exclude_paths(checkout, workspace_files(payload))
    for name, content in payload["files"].items():
        path = checkout / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (checkout / payload["instructions_file"]).write_text("instructions\n")

    _fake_aider_run(checkout, _config(payload["files"][AIDER_CONFIG_FILE]))

    patch = repo_tree.version_patch(checkout)
    assert [line for line in patch.splitlines() if line.startswith("diff --git")] == ["diff --git a/main.py b/main.py"]
    assert "+print(2)" in patch
    assert "the prompt" not in patch

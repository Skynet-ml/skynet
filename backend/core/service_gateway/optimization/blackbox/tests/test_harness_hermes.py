"""Tests for the Hermes Agent harness: registry entry, launch, install pin, protected build and output parsing."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from core.models.blackbox import BLACKBOX_TARGET_AGENT, BlackboxTarget

from .. import repo_tree
from ..harness import GatewayConfig, build_launch, launch_payload, pinned_harness_check
from ..harness_bridge import parse_plain_output, workspace_files
from ..harnesses import REGISTRY
from ..harnesses.hermes import (
    _PROTECTED_ENV,
    _WHEEL_FETCH,
    HERMES_BASE_URL_FILE,
    HERMES_CONFIG_FILE,
    HERMES_VERSION,
    HERMES_WHEEL,
    HERMES_WHEEL_SHA256,
    HERMES_WHEEL_URL,
    SPEC,
)

_GATEWAY = GatewayConfig(url="https://gw.example/v1", api_key="secret-key")
_RELAY_URL = "http://127.0.0.1:3210/v1"
# A synthetic recording of Hermes 0.19 stdout for ``chat -q ... -Q``: quiet mode
# prints the final answer and the session line.
_RECORDED_STDOUT = """\
The answer is 42.

session_id: 20261108_120000_abcdef
"""
_DIRECT_PROVIDER_KEYS = {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "KIMI_API_KEY", "GLM_API_KEY"}


def _target(**overrides: object) -> BlackboxTarget:
    """Build a validated Hermes agent target.

    Args:
        **overrides: Extra target fields.

    Returns:
        The target.
    """
    fields: dict[str, object] = {
        "kind": BLACKBOX_TARGET_AGENT,
        "harness": "hermes",
        "model": "target-model",
    }
    fields.update(overrides)
    return BlackboxTarget(**fields)


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


def _fetch(tmp_path: Path, sha: str) -> subprocess.CompletedProcess[str]:
    """Run the install's wheel fetch on a local file URL.

    Args:
        tmp_path: Test folder.
        sha: Expected sha256 digest.

    Returns:
        The finished process.
    """
    source = tmp_path / "source.whl"
    source.write_bytes(b"wheel bytes")
    return subprocess.run(
        [sys.executable, "-I", "-c", _WHEEL_FETCH, source.as_uri(), sha, str(tmp_path / "out.whl")],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_registry_lists_hermes_with_its_pin_and_plain_parser() -> None:
    """Hermes registers at position 68 with the version token its ``--version`` prints."""
    assert REGISTRY["hermes"] is SPEC
    assert SPEC.binary == "hermes"
    assert HERMES_VERSION == "0.19.0"
    assert SPEC.version == "v0.19.0"
    assert SPEC.output_format == "plain"
    assert SPEC.position == 68


def test_pinned_check_matches_the_printed_version_only() -> None:
    """The probe's pattern accepts ``hermes --version`` output for the pin and rejects other releases."""
    check = pinned_harness_check("hermes")
    assert check is not None
    assert check.startswith("hermes --version ")
    pattern = re.search(r"grep -Eq '([^']+)'", check).group(1)
    assert re.search(pattern, "Hermes Agent v0.19.0 (2026.7.20)")
    assert not re.search(pattern, "Hermes Agent v0.19.01 (2026.7.21)")
    assert not re.search(pattern, "Hermes Agent v0.23.0 (2026.10.1)")


def test_launch_points_hermes_at_the_gateway_through_its_config_files() -> None:
    """The custom profile and OPENAI_BASE_URL carry the gateway URL; the run is a quiet one-shot chat."""
    launch = build_launch(_target(), _GATEWAY)

    config = json.loads(launch.files[HERMES_CONFIG_FILE])
    assert config["provider"] == "custom:runtime"
    assert config["custom_providers"] == [
        {
            "name": "runtime",
            "model": "target-model",
            "base_url": _GATEWAY.url,
            "key_env": "OPENAI_API_KEY",
            "api_mode": "chat_completions",
        }
    ]
    assert config["fallback_providers"] == []
    assert config["memory"] == {"memory_enabled": False, "user_profile_enabled": False}
    assert "agent" not in config
    assert launch.files[HERMES_BASE_URL_FILE] == _GATEWAY.url
    assert launch.instructions_file == "AGENTS.md"
    assert 'export HERMES_HOME="$(mktemp -d)"' in launch.run_command
    assert f'cp {HERMES_CONFIG_FILE} "$HERMES_HOME/config.yaml"' in launch.run_command
    assert f'OPENAI_BASE_URL="$(cat {HERMES_BASE_URL_FILE})"' in launch.run_command
    assert 'OPENAI_API_KEY="$APP_API_KEY"' in launch.run_command
    assert (
        'hermes --yolo chat -q "$(cat "$APP_PROMPT_FILE")" -Q --model "$APP_MODEL" --provider custom:runtime'
        in launch.run_command
    )
    assert "--ignore-rules" not in launch.run_command
    assert launch.parse_output is parse_plain_output


def test_run_seeds_a_fresh_update_check_cache(tmp_path: Path) -> None:
    """The run writes a current cache entry for the pinned version, so Hermes skips its PyPI check."""
    launch = build_launch(_target(), _GATEWAY)
    seed = launch.run_command.split(" && OPENAI_API_KEY=")[0] + ' && cp "$HERMES_HOME/.update_check" seeded.json'
    (tmp_path / HERMES_CONFIG_FILE).parent.mkdir(parents=True)
    (tmp_path / HERMES_CONFIG_FILE).write_text(launch.files[HERMES_CONFIG_FILE])

    subprocess.run(seed, shell=True, cwd=tmp_path, env={"PATH": os.defpath}, timeout=10, check=True)

    cache = json.loads((tmp_path / "seeded.json").read_text())
    assert cache["ver"] == HERMES_VERSION
    assert cache["rev"] is None
    assert cache["behind"] == 0
    assert isinstance(cache["ts"], int) and cache["ts"] > 0


def test_launch_never_writes_the_gateway_key_or_a_provider_key() -> None:
    """Only the gateway token reaches the box, and only through APP_API_KEY."""
    launch = build_launch(_target(), _GATEWAY)

    assert _GATEWAY.api_key not in launch.run_command
    assert _GATEWAY.api_key not in launch.install_command
    assert all(_GATEWAY.api_key not in content for content in launch.files.values())
    assert launch.env["APP_API_KEY"] == _GATEWAY.api_key
    assert launch.env["APP_MODEL"] == "target-model"
    assert not _DIRECT_PROVIDER_KEYS & set(launch.env)


def test_install_pins_the_checksummed_wheel_without_a_piped_script() -> None:
    """The open install skips work when the pin is present and otherwise installs the verified wheel."""
    install = build_launch(_target(), _GATEWAY).install_command

    assert "hermes --version" in install
    assert "v0[.]19[.]0" in install
    assert HERMES_WHEEL_URL in install
    assert HERMES_WHEEL_SHA256 in install
    assert HERMES_WHEEL_URL.endswith(HERMES_WHEEL)
    assert f'uv tool install --force --python 3.12 "$_hermes_wheel/{HERMES_WHEEL}"' in install
    assert "uv==0.9.13" in install
    assert "curl" not in install
    assert "| sh" not in install and "| bash" not in install


def test_wheel_fetch_keeps_a_matching_file_and_rejects_a_mismatch(tmp_path: Path) -> None:
    """The fetch writes the wheel only when its sha256 matches the pin."""
    good = _fetch(tmp_path, hashlib.sha256(b"wheel bytes").hexdigest())
    assert good.returncode == 0
    assert (tmp_path / "out.whl").read_bytes() == b"wheel bytes"
    (tmp_path / "out.whl").unlink()

    bad = _fetch(tmp_path, "0" * 64)
    assert bad.returncode != 0
    assert "sha256 mismatch" in bad.stderr
    assert not (tmp_path / "out.whl").exists()


@pytest.mark.parametrize(("effort", "expected"), [("high", "high"), ("max", "max"), ("turbo", None), (None, None)])
def test_reasoning_effort_reaches_the_config_only_for_known_levels(effort: str | None, expected: str | None) -> None:
    """A Hermes effort level is set in ``agent.reasoning_effort``; an unknown one keeps Hermes's default."""
    launch = build_launch(_target(), _GATEWAY, reasoning_effort=effort)

    config = json.loads(launch.files[HERMES_CONFIG_FILE])
    assert config.get("agent", {}).get("reasoning_effort") == expected


def test_protected_build_uses_the_pinned_check_env_flags_and_relay(tmp_path: Path) -> None:
    """A protected launch checks the preinstalled pin, blanks provider keys, and rewrites both gateway files."""
    launch = build_launch(_target(run_command="true"), _GATEWAY, protected=True)

    assert launch.install_command == pinned_harness_check("hermes")
    assert "uv tool install" not in launch.install_command
    for key, value in _PROTECTED_ENV.items():
        assert launch.env[key] == value
    assert launch.env["DO_NOT_TRACK"] == "1"
    assert "OPENAI_API_KEY" not in _PROTECTED_ENV
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
    config = json.loads((tmp_path / HERMES_CONFIG_FILE).read_text())
    assert config["custom_providers"][0]["base_url"] == _RELAY_URL
    assert (tmp_path / HERMES_BASE_URL_FILE).read_text() == _RELAY_URL


def test_open_build_keeps_the_protected_env_out() -> None:
    """The default launch is unchanged by the protected-only flags."""
    launch = build_launch(_target(), _GATEWAY)

    assert not set(_PROTECTED_ENV) & set(launch.env)


def test_plain_parser_keeps_the_recorded_hermes_answer() -> None:
    """The bridge keeps Hermes's quiet stdout as the answer and reports no usage."""
    launch = build_launch(_target(), _GATEWAY)

    answer, usage = launch.parse_output(_RECORDED_STDOUT)
    assert answer == _RECORDED_STDOUT.strip()
    assert answer.startswith("The answer is 42.")
    assert usage == {}
    assert launch.parse_output("") == (None, {})


def test_launch_payload_names_the_plain_parser() -> None:
    """The sandbox bridge receives the plain parser name and both generated files."""
    payload = launch_payload(build_launch(_target(), _GATEWAY))

    assert payload["output_format"] == "plain"
    assert set(payload["files"]) == {HERMES_CONFIG_FILE, HERMES_BASE_URL_FILE}


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_version_patch_holds_only_the_code_change(tmp_path: Path) -> None:
    """Hermes's generated files and instructions never join a repo-mode version patch."""
    checkout = _shipped_tree(tmp_path)
    payload = launch_payload(build_launch(_target(), _GATEWAY))
    repo_tree.exclude_paths(checkout, workspace_files(payload))
    for name, content in payload["files"].items():
        path = checkout / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (checkout / payload["instructions_file"]).write_text("instructions\n")
    (checkout / "main.py").write_text("print(2)\n")

    patch = repo_tree.version_patch(checkout)
    assert [line for line in patch.splitlines() if line.startswith("diff --git")] == ["diff --git a/main.py b/main.py"]
    assert "+print(2)" in patch
    assert "gw.example" not in patch

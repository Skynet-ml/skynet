"""Tests for the Hermes Agent harness: registry entry, launch, install pin, protected build and output parsing."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from core.exceptions import ServiceError
from core.models.blackbox import BLACKBOX_HARNESSES, BLACKBOX_TARGET_AGENT, BlackboxTarget

from .. import repo_tree
from ..harness import GatewayConfig, build_launch, launch_payload, pinned_harness_check
from ..harness_bridge import parse_plain_output, workspace_files
from ..harnesses import REGISTRY
from ..harnesses.hermes import (
    _PROTECTED_ENV,
    _WHEEL_FETCH,
    HERMES_BASE_URL_FILE,
    HERMES_CONFIG_FILE,
    HERMES_EFFORTS,
    HERMES_VERSION,
    HERMES_WHEEL,
    HERMES_WHEEL_SHA256,
    HERMES_WHEEL_URL,
    SPEC,
    UV_VERSION,
    UV_WHEEL_SHA256,
)

_GATEWAY = GatewayConfig(url="https://gw.example/v1", api_key="secret-key")
_RELAY_URL = "http://127.0.0.1:3210/v1"
# A synthetic recording of Hermes 0.19 stdout for ``chat -q ... -Q``: quiet mode
# prints only the final answer there and the ``session_id:`` line on stderr.
_RECORDED_STDOUT = "I read solution.py and fixed the off-by-one error.\n\nThe answer is 42.\n"
_RECORDED_STDERR = "\nsession_id: 20261108_120000_abcdef\n"
_DIRECT_PROVIDER_KEYS = {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "KIMI_API_KEY", "GLM_API_KEY"}
# Keys Hermes 0.19 reads for other model routes, speech tools and side tasks.
# ``AI_GATEWAY_API_KEY`` is not in 0.19.0; it is blanked for later releases.
_EXTRA_MODEL_KEYS = {
    "AI_GATEWAY_API_KEY",
    "NOUS_API_KEY",
    "GROQ_API_KEY",
    "DEEPINFRA_API_KEY",
    "NOVITA_API_KEY",
    "QWEN_API_KEY",
    "VOICE_TOOLS_OPENAI_KEY",
    "AUXILIARY_VISION_API_KEY",
    "AUXILIARY_WEB_EXTRACT_API_KEY",
    "AUXILIARY_APPROVAL_API_KEY",
}
# Keys that 0.19.0 can take from the models.dev catalog, the Azure Anthropic
# key, the custom provider's fallback key and the Hugging Face trace-upload
# tokens.
_CATALOG_AND_TOOL_KEYS = {
    "ACTUAL_API_KEY",
    "NEBIUS_API_KEY",
    "RAMP_ROUTER_API_KEY",
    "TOKENPLAN_API_KEY",
    "AZURE_ANTHROPIC_KEY",
    "ALIBABA_CODING_PLAN_API_KEY",
    "CUSTOM_API_KEY",
    "HUGGINGFACE_HUB_TOKEN",
    "HUGGING_FACE_HUB_TOKEN",
    "HUGGINGFACE_TOKEN",
}
# Every secret ``tool`` key in Hermes 0.19.0's ``OPTIONAL_ENV_VARS``: paid web,
# browser, image, speech, memory and tracing services.
_PAID_TOOL_KEYS = {
    "EXA_API_KEY",
    "PARALLEL_API_KEY",
    "FIRECRAWL_API_KEY",
    "TOOL_GATEWAY_USER_TOKEN",
    "TAVILY_API_KEY",
    "BRAVE_SEARCH_API_KEY",
    "BROWSERBASE_API_KEY",
    "BROWSER_USE_API_KEY",
    "CAMOFOX_API_KEY",
    "FAL_KEY",
    "KREA_API_KEY",
    "VOICE_TOOLS_OPENAI_KEY",
    "ELEVENLABS_API_KEY",
    "MISTRAL_API_KEY",
    "GITHUB_TOKEN",
    "HONCHO_API_KEY",
    "HINDSIGHT_API_KEY",
    "SUPERMEMORY_API_KEY",
    "MEM0_API_KEY",
    "RETAINDB_API_KEY",
    "BRV_API_KEY",
    "OPENVIKING_API_KEY",
    "HERMES_LANGFUSE_SECRET_KEY",
}
# Hermes reads these only for Bedrock, which the config never selects, and an
# empty ``AWS_PROFILE`` breaks boto3, so they stay inherited.
_INHERITED_AWS_KEYS = {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"}
_PROMPT = "Fix `solution.py` and say $(echo done)."
# Stand-in for the hermes binary: it records its call and prints a fixed answer.
# Python writes the argv as one JSON line, so any prompt text round-trips.
_FAKE_HERMES = """#!/bin/sh
{
  printf 'ARGS='; "$FAKE_PYTHON" -I -c 'import json, sys; print(json.dumps(sys.argv[1:]))' "$@"
  printf 'OPENAI_BASE_URL=%s\\n' "$OPENAI_BASE_URL"
  printf 'OPENAI_API_KEY=%s\\n' "$OPENAI_API_KEY"
  printf 'ANTHROPIC_API_KEY=%s\\n' "$ANTHROPIC_API_KEY"
  printf 'NOUS_API_KEY=%s\\n' "$NOUS_API_KEY"
  printf 'HERMES_LANGFUSE_PUBLIC_KEY=%s\\n' "$HERMES_LANGFUSE_PUBLIC_KEY"
  printf 'HERMES_LANGFUSE_SECRET_KEY=%s\\n' "$HERMES_LANGFUSE_SECRET_KEY"
  printf 'BRAVE_SEARCH_API_KEY=%s\\n' "$BRAVE_SEARCH_API_KEY"
  printf 'AWS_ACCESS_KEY_ID=%s\\n' "$AWS_ACCESS_KEY_ID"
  printf 'AWS_SECRET_ACCESS_KEY=%s\\n' "$AWS_SECRET_ACCESS_KEY"
  printf 'AWS_SESSION_TOKEN=%s\\n' "$AWS_SESSION_TOKEN"
  printf 'AWS_PROFILE=%s\\n' "$AWS_PROFILE"
  printf 'HERMES_HOME=%s\\n' "$HERMES_HOME"
  printf 'CONFIG=%s\\n' "$(tr -d '\\n' < "$HERMES_HOME/config.yaml")"
} >> "$FAKE_LOG"
printf '%s' "$FAKE_ANSWER"
printf '%s' "$FAKE_STDERR" >&2
exit "${FAKE_EXIT:-0}"
"""
# Stand-ins for the install step's tools. The fake ``python3`` plays pip and
# the wheel fetch, and logs both; the fake uv installs a fake ``hermes``. macOS
# ``mktemp -d`` ignores ``TMPDIR``, so a fake one keeps the folder in scratch.
_FAKE_INSTALL_TOOLS = {
    "mktemp": '#!/bin/sh\nd="$TMPDIR/download.$$"\nmkdir "$d" && echo "$d"\n',
    "python3": """#!/bin/sh
{ printf 'python3'; for a in "$@"; do printf ' [%s]' "$a"; done; printf '\\n'; } >> "$FAKE_LOG"
if [ "$1" = "-m" ]; then
  [ "${FAKE_PIP_EXIT:-0}" -eq 0 ] || exit "$FAKE_PIP_EXIT"
  for a in "$@"; do last="$a"; done
  printf 'REQ=%s\\n' "$(cat "$last")" >> "$FAKE_LOG"
  for a in "$@"; do [ "$prev" = "--target" ] && t="$a"; prev="$a"; done
  mkdir -p "$t/bin" && cp "$FAKE_UV" "$t/bin/uv" && chmod +x "$t/bin/uv"
  exit 0
fi
[ "${FAKE_FETCH_EXIT:-0}" -eq 0 ] || exit "$FAKE_FETCH_EXIT"
for a in "$@"; do last="$a"; done
printf wheel > "$last"
""",
}
_FAKE_UV = """#!/bin/sh
{ printf 'uv'; for a in "$@"; do printf ' [%s]' "$a"; done; printf '\\n'; } >> "$FAKE_LOG"
mkdir -p "$HOME/.local/bin"
printf '#!/bin/sh\\necho "Hermes Agent v0.19.0 (2026.7.20)"\\n' > "$HOME/.local/bin/hermes"
chmod +x "$HOME/.local/bin/hermes"
"""


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


def _run_install(
    tmp_path: Path, *, uv_on_path: bool = False, pip_exit: int = 0, fetch_exit: int = 0
) -> tuple[int, str, Path, str]:
    """Run the open install command under ``set -eu`` with fake install tools.

    The command runs inside a ``set -eu`` script, as in the native bootstrap,
    and ``TMPDIR`` points at a scratch folder, so the test can see every
    folder ``mktemp -d`` leaves behind.

    Args:
        tmp_path: Scratch directory for the fake tools, ``HOME`` and ``TMPDIR``.
        uv_on_path: Put the fake uv on ``PATH``, so the install must not run pip.
        pip_exit: Exit status the fake pip returns.
        fetch_exit: Exit status the fake wheel fetch returns.

    Returns:
        The exit status, the stdout, the ``TMPDIR`` folder and the tool log.
    """
    install = build_launch(_target(), _GATEWAY).install_command
    assert install is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tools = dict(_FAKE_INSTALL_TOOLS, **({"uv": _FAKE_UV} if uv_on_path else {}))
    for name, script in tools.items():
        tool = bin_dir / name
        tool.write_text(script)
        tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    fake_uv = tmp_path / "fake_uv"
    fake_uv.write_text(_FAKE_UV)
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    log = tmp_path / "tools.log"
    log.touch()
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.defpath}",
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(scratch),
        "FAKE_LOG": str(log),
        "FAKE_UV": str(fake_uv),
        "FAKE_PIP_EXIT": str(pip_exit),
        "FAKE_FETCH_EXIT": str(fetch_exit),
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
    return result.returncode, result.stdout, scratch, log.read_text()


def _run_with_fake_hermes(
    tmp_path: Path,
    *,
    protected: bool,
    relay_env: str = "APP_BUDGET_RELAY_URL",
    fake_exit: int = 0,
    prompt: str = _PROMPT,
) -> tuple[subprocess.CompletedProcess[str], str, Path]:
    """Run the built Hermes command in a scratch workspace against a fake ``hermes`` binary.

    The env also carries an inherited OpenAI URL, an Anthropic key, a Brave
    key and Hermes Langfuse keys, which a protected run must not pass on. It
    also carries AWS credentials, which every run passes on unchanged.

    Args:
        tmp_path: Scratch directory for the workspace, the fake binary and its log.
        protected: Build the protected launch, with the sandbox-local relay set.
        relay_env: The env name that carries the relay URL in a protected run.
        fake_exit: Exit status the fake binary returns.
        prompt: Text of the task's prompt file.

    Returns:
        The finished process, the fake binary's call log, and the workspace.
    """
    launch = build_launch(_target(), _GATEWAY, protected=protected)
    workspace = tmp_path / "work"
    for name, content in launch.files.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (workspace / "task").mkdir(parents=True)
    (workspace / "task" / "PROMPT.md").write_text(prompt)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "hermes"
    fake.write_text(_FAKE_HERMES)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    env = {
        "OPENAI_BASE_URL": "https://api.openai.com/v1",
        "ANTHROPIC_API_KEY": "inherited-key",
        "NOUS_API_KEY": "inherited-key",
        "HERMES_LANGFUSE_PUBLIC_KEY": "inherited-key",
        "HERMES_LANGFUSE_SECRET_KEY": "inherited-key",
        "BRAVE_SEARCH_API_KEY": "inherited-key",
        **{name: f"inherited-{name.lower()}" for name in _INHERITED_AWS_KEYS},
        **launch.env,
        "PATH": f"{bin_dir}{os.pathsep}{os.defpath}",
        "HOME": str(tmp_path / "home"),
        "FAKE_LOG": str(log),
        "FAKE_ANSWER": _RECORDED_STDOUT,
        "FAKE_STDERR": _RECORDED_STDERR,
        "FAKE_EXIT": str(fake_exit),
        "FAKE_PYTHON": sys.executable,
    }
    if protected:
        env[relay_env] = _RELAY_URL
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


def test_registry_lists_hermes_after_goose_with_its_pin_and_plain_parser() -> None:
    """Hermes registers after goose and before cline, with the version token its ``--version`` prints."""
    assert REGISTRY["hermes"] is SPEC
    assert SPEC.binary == "hermes"
    assert HERMES_VERSION == "0.19.0"
    assert SPEC.version == "v0.19.0"
    assert SPEC.output_format == "plain"
    assert SPEC.position == 65
    ids = list(REGISTRY)
    assert ids.index("goose") < ids.index("hermes") < ids.index("cline")
    assert BLACKBOX_HARNESSES.index("goose") < BLACKBOX_HARNESSES.index("hermes") < BLACKBOX_HARNESSES.index("cline")


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
        'hermes --yolo chat --query="$(cat "$APP_PROMPT_FILE")" -Q --model "$APP_MODEL" --provider custom:runtime'
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
    assert isinstance(cache["ts"], int)
    assert cache["ts"] > 0


def test_launch_never_writes_the_gateway_key_or_a_provider_key() -> None:
    """Only the gateway token reaches the box, and only through APP_API_KEY."""
    launch = build_launch(_target(), _GATEWAY)

    assert _GATEWAY.api_key not in launch.run_command
    assert _GATEWAY.api_key not in launch.install_command
    assert all(_GATEWAY.api_key not in content for content in launch.files.values())
    for prefix in ("APP", "SKYNET"):
        assert launch.env[f"{prefix}_API_KEY"] == _GATEWAY.api_key
        assert launch.env[f"{prefix}_MODEL"] == "target-model"
        assert launch.env[f"{prefix}_GATEWAY_URL"] == _GATEWAY.url
    assert not _DIRECT_PROVIDER_KEYS & set(launch.env)


def test_install_pins_the_checksummed_wheel_and_uv_without_a_piped_script() -> None:
    """The open install skips work when the pin is present and otherwise installs hash-checked uv and wheel."""
    install = build_launch(_target(), _GATEWAY).install_command

    assert "hermes --version" in install
    assert "v0[.]19[.]0" in install
    assert HERMES_WHEEL_URL in install
    assert HERMES_WHEEL_SHA256 in install
    assert HERMES_WHEEL_URL.endswith(HERMES_WHEEL)
    assert f'tool install --force --python 3.12 "$d/{HERMES_WHEEL}"' in install
    assert f"uv=={UV_VERSION}" in install
    assert "--require-hashes" in install
    assert all(f"--hash=sha256:{digest}" in install for digest in UV_WHEEL_SHA256)
    assert "curl" not in install
    assert "| sh" not in install
    assert "| bash" not in install
    assert "sudo" not in install


def test_install_without_uv_installs_hash_checked_uv_and_removes_the_download_folder(tmp_path: Path) -> None:
    """Pip installs the pinned uv with hashes into the download folder; the folder is gone after the install."""
    code, stdout, scratch, log = _run_install(tmp_path)

    assert code == 0
    assert stdout.splitlines() == ["Hermes Agent v0.19.0 (2026.7.20)", "after-install"]
    (pip,) = [line for line in log.splitlines() if line.startswith("python3 [-m]")]
    for flag in ("[--require-hashes]", "[--no-deps]", "[--only-binary]"):
        assert flag in pip
    assert _logged(log, "REQ").startswith(f"uv=={UV_VERSION} --hash=sha256:")
    (uv,) = [line for line in log.splitlines() if line.startswith("uv ")]
    assert uv.startswith("uv [tool] [install] [--force] [--python] [3.12] [")
    assert uv.endswith(f"{HERMES_WHEEL}]")
    assert list(scratch.iterdir()) == []


def test_install_with_uv_on_path_skips_pip(tmp_path: Path) -> None:
    """A uv already on PATH installs the wheel; pip never runs."""
    code, stdout, scratch, log = _run_install(tmp_path, uv_on_path=True)

    assert code == 0
    assert stdout.splitlines()[-1] == "after-install"
    assert "python3 [-m]" not in log
    assert "uv [tool] [install]" in log
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize(("pip_exit", "fetch_exit", "code"), [(1, 0, 1), (0, 1, 1)])
def test_failed_uv_install_or_wheel_check_removes_the_download_folder_and_fails(
    tmp_path: Path, pip_exit: int, fetch_exit: int, code: int
) -> None:
    """A failed pip step or wheel digest check stops the install, removes the folder, and installs nothing."""
    exit_code, stdout, scratch, log = _run_install(tmp_path, pip_exit=pip_exit, fetch_exit=fetch_exit)

    assert exit_code == code
    assert "after-install" not in stdout
    assert "uv [tool]" not in log
    assert not (tmp_path / "home" / ".local" / "bin" / "hermes").exists()
    assert list(scratch.iterdir()) == []


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


@pytest.mark.parametrize("effort", HERMES_EFFORTS)
def test_supported_reasoning_effort_reaches_the_config_unchanged(effort: str) -> None:
    """Every Hermes effort level goes into ``agent.reasoning_effort`` as given."""
    launch = build_launch(_target(), _GATEWAY, reasoning_effort=effort)

    assert json.loads(launch.files[HERMES_CONFIG_FILE])["agent"] == {"reasoning_effort": effort}


def test_no_reasoning_effort_keeps_the_hermes_default() -> None:
    """Without an effort the config has no ``agent`` block."""
    launch = build_launch(_target(), _GATEWAY, reasoning_effort=None)

    assert "agent" not in json.loads(launch.files[HERMES_CONFIG_FILE])


def test_spec_declares_only_the_skynet_effort_levels() -> None:
    """Submit sees the seven Skynet levels Hermes accepts; ``ultra`` is not one of them."""
    assert SPEC.supported_efforts == ("none", "minimal", "low", "medium", "high", "xhigh", "max")
    assert "ultra" not in SPEC.supported_efforts


@pytest.mark.parametrize("effort", ["ultra", "turbo", "High", "auto"])
def test_unsupported_reasoning_effort_is_refused(effort: str) -> None:
    """An effort outside the Skynet levels fails the launch instead of a silent drop or remap."""
    with pytest.raises(ServiceError, match="Hermes does not support the reasoning effort") as error:
        build_launch(_target(), _GATEWAY, reasoning_effort=effort)
    assert str(error.value).endswith("Use one of: none, minimal, low, medium, high, xhigh, max.")


def test_protected_build_uses_the_pinned_check_env_flags_and_relay(tmp_path: Path) -> None:
    """A protected launch checks the preinstalled pin, blanks provider keys, and rewrites both gateway files."""
    launch = build_launch(_target(run_command="true"), _GATEWAY, protected=True)

    assert launch.install_command == pinned_harness_check("hermes")
    assert "uv tool install" not in launch.install_command
    for key, value in _PROTECTED_ENV.items():
        assert launch.env[key] == value
    assert launch.env["DO_NOT_TRACK"] == "1"
    blanked = {name for name, value in _PROTECTED_ENV.items() if value == ""}
    assert blanked >= _EXTRA_MODEL_KEYS
    assert blanked >= _DIRECT_PROVIDER_KEYS
    assert blanked >= _CATALOG_AND_TOOL_KEYS
    assert blanked >= _PAID_TOOL_KEYS
    assert not _INHERITED_AWS_KEYS & set(_PROTECTED_ENV)
    assert {"CLAUDE_CODE_OAUTH_TOKEN", "COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"} <= set(_PROTECTED_ENV)
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


def test_protected_run_passes_aws_credentials_on_and_blanks_a_tool_key(tmp_path: Path) -> None:
    """Against a fake hermes, a protected run keeps inherited AWS credentials and blanks the Brave key."""
    result, calls, _ = _run_with_fake_hermes(tmp_path, protected=True)

    assert result.returncode == 0
    for name in _INHERITED_AWS_KEYS:
        assert _logged(calls, name) == f"inherited-{name.lower()}"
    assert _logged(calls, "BRAVE_SEARCH_API_KEY") == ""


@pytest.mark.parametrize("relay_env", ["APP_BUDGET_RELAY_URL", "SKYNET_BUDGET_RELAY_URL"])
def test_protected_run_sends_hermes_to_the_relay_with_the_gateway_token(tmp_path: Path, relay_env: str) -> None:
    """Against a fake hermes, the run passes the prompt as one unparsed argument and uses only the relay."""
    result, calls, workspace = _run_with_fake_hermes(tmp_path, protected=True, relay_env=relay_env)

    assert result.returncode == 0
    assert json.loads(_logged(calls, "ARGS")) == [
        "--yolo",
        "chat",
        f"--query={_PROMPT}",
        "-Q",
        "--model",
        "target-model",
        "--provider",
        "custom:runtime",
    ]
    assert _logged(calls, "OPENAI_BASE_URL") == _RELAY_URL
    assert _logged(calls, "OPENAI_API_KEY") == _GATEWAY.api_key
    assert _logged(calls, "ANTHROPIC_API_KEY") == ""
    assert _logged(calls, "NOUS_API_KEY") == ""
    assert _logged(calls, "HERMES_LANGFUSE_PUBLIC_KEY") == ""
    assert _logged(calls, "HERMES_LANGFUSE_SECRET_KEY") == ""
    assert _RELAY_URL in _logged(calls, "CONFIG")
    assert _GATEWAY.url not in calls
    home = Path(_logged(calls, "HERMES_HOME"))
    assert not home.is_relative_to(workspace)
    assert (
        parse_plain_output(result.stdout)[0]
        == "I read solution.py and fixed the off-by-one error.\n\nThe answer is 42."
    )
    assert "session_id" not in result.stdout


def test_prompt_that_starts_with_an_option_stays_the_query_value(tmp_path: Path) -> None:
    """A prompt that looks like Hermes options, with brackets and a newline, stays the one ``--query`` value."""
    prompt = "--model=other-model -Q ] [--provider=openrouter]\n[x] [y]"
    result, calls, _ = _run_with_fake_hermes(tmp_path, protected=False, prompt=prompt)

    assert result.returncode == 0
    argv = json.loads(_logged(calls, "ARGS"))
    # The options of Hermes 0.19.0's ``chat`` parser that this run uses.
    parser = argparse.ArgumentParser(prog="hermes chat")
    parser.add_argument("-q", "--query")
    parser.add_argument("-Q", "--quiet", action="store_true")
    parser.add_argument("-m", "--model")
    parser.add_argument("--provider")
    args = parser.parse_args(argv[2:])
    assert args.query == prompt
    assert args.model == "target-model"
    assert args.provider == "custom:runtime"


@pytest.mark.parametrize("fake_exit", [0, 3])
def test_run_removes_hermes_home_and_keeps_the_exit_status(tmp_path: Path, fake_exit: int) -> None:
    """The EXIT trap removes the per-run HERMES_HOME after a good or a failed run, and keeps Hermes's status."""
    result, calls, _ = _run_with_fake_hermes(tmp_path, protected=False, fake_exit=fake_exit)

    assert result.returncode == fake_exit
    assert result.stdout == _RECORDED_STDOUT
    assert _logged(calls, "OPENAI_BASE_URL") == _GATEWAY.url
    assert not Path(_logged(calls, "HERMES_HOME")).exists()


def test_plain_parser_keeps_the_recorded_hermes_answer() -> None:
    """The bridge keeps Hermes's quiet stdout as the answer and reports no usage."""
    launch = build_launch(_target(), _GATEWAY)

    answer, usage = launch.parse_output(_RECORDED_STDOUT)
    assert answer == "I read solution.py and fixed the off-by-one error.\n\nThe answer is 42."
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

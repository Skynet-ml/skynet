"""Tests for the OpenCode V2 harness: registry entry, pinned install, gateway-only launch, effort variants and output parsing."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from core.exceptions import ServiceError
from core.models.blackbox import (
    BLACKBOX_HARNESS_OPENCODE,
    BLACKBOX_HARNESS_OPENCODE_V2,
    BLACKBOX_HARNESSES,
    BLACKBOX_TARGET_AGENT,
    BlackboxTarget,
)

from ..harness import GatewayConfig, build_launch, launch_payload, pinned_harness_check
from ..harness_bridge import parse_opencode_output, parse_output
from ..harnesses import REGISTRY
from ..harnesses.opencode_v2 import _PROTECTED_ENV, OPENCODE_V2_EFFORTS, OPENCODE_V2_SHA256, OPENCODE_V2_VERSION

_GATEWAY = GatewayConfig(url="https://gw.example/v1", api_key="gateway-token")
_RELAY = "http://127.0.0.1:3210/v1"
_PROVIDER_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "OPENCODE_API_KEY")
# Synthetic ``opencode2 run --format json`` lines in the shape that
# packages/cli/src/run/noninteractive.ts writes in 2.2.7.
_RECORDED_STDOUT = "\n".join(
    json.dumps(line)
    for line in (
        {"type": "step_start", "timestamp": 1, "sessionID": "ses_1", "part": {"type": "step-start"}},
        {
            "type": "tool_use",
            "timestamp": 2,
            "sessionID": "ses_1",
            "part": {"type": "tool", "tool": "read", "state": {"status": "completed"}},
        },
        {"type": "text", "timestamp": 3, "sessionID": "ses_1", "part": {"type": "text", "text": "Reading."}},
        {
            "type": "step_finish",
            "timestamp": 4,
            "sessionID": "ses_1",
            "part": {"type": "step-finish", "tokens": {"input": 120, "output": 30, "reasoning": 5}},
        },
        {
            "type": "text",
            "timestamp": 5,
            "sessionID": "ses_1",
            "part": {"type": "text", "text": "  I fixed solution.py.\n\nThe answer is 42.\n"},
        },
        {
            "type": "step_finish",
            "timestamp": 6,
            "sessionID": "ses_1",
            "part": {"type": "step-finish", "tokens": {"input": 200, "output": 12, "reasoning": 0}},
        },
    )
)
# Stand-in for the opencode2 binary: it records each call and prints a fixed answer.
_FAKE_OPENCODE2 = """#!/bin/sh
{
  printf 'ARGS'; for a in "$@"; do printf ' [%s]' "$a"; done; printf '\\n'
  printf 'APP_API_KEY=%s\\n' "$APP_API_KEY"
  printf 'OPENAI_API_KEY=%s\\n' "$OPENAI_API_KEY"
  printf 'OPENCODE_CONFIG=%s\\n' "${OPENCODE_CONFIG-unset}"
  printf 'OPENCODE_CONFIG_CONTENT=%s\\n' "${OPENCODE_CONFIG_CONTENT-unset}"
  printf 'STDIN=%s\\n' "$(cat)"
} >> "$FAKE_LOG"
printf '%s' "$FAKE_ANSWER"
"""
# Stand-ins for the install step's tools. ``curl`` logs the file it writes,
# so a test can check that the download folder is gone afterwards. macOS
# ``mktemp -d`` ignores ``TMPDIR``, so a fake one keeps the folder in scratch.
_FAKE_INSTALL_TOOLS = {
    "uname": "#!/bin/sh\necho aarch64\n",
    "mktemp": '#!/bin/sh\nd="$TMPDIR/download.$$"\nmkdir "$d" && echo "$d"\n',
    "curl": (
        '#!/bin/sh\nprintf \'%s\\n\' "$3" >> "$FAKE_LOG"\n'
        '[ "${FAKE_CURL_EXIT:-0}" -eq 0 ] || exit "$FAKE_CURL_EXIT"\nprintf archive > "$3"\n'
    ),
    "sha256sum": '#!/bin/sh\ncat > /dev/null\nexit "${FAKE_SHA_EXIT:-0}"\n',
    "tar": "#!/bin/sh\nprintf '#!/bin/sh\\necho opencode v2.2.7\\n' > \"$4/opencode\"\n",
}


def _target() -> BlackboxTarget:
    """Build an OpenCode V2 agent target.

    Returns:
        The target.
    """
    return BlackboxTarget(kind=BLACKBOX_TARGET_AGENT, harness=BLACKBOX_HARNESS_OPENCODE_V2, model="acme/agent-1")


def _run_with_fake_opencode2(
    tmp_path: Path, *, protected: bool, reasoning_effort: str | None = None
) -> tuple[str, str, Path]:
    """Run the built command in a scratch workspace against a fake ``opencode2`` binary.

    The env also carries an inherited ``OPENCODE_CONFIG`` and
    ``OPENCODE_CONFIG_CONTENT``, which a protected run must replace so they
    cannot add a provider.

    Args:
        tmp_path: Scratch directory for the workspace, the fake binary and its log.
        protected: Build the protected launch, with the sandbox-local relay set.
        reasoning_effort: The user's effort for the model, or ``None``.

    Returns:
        The command's stdout, the fake binary's call log, and the workspace.
    """
    launch = build_launch(_target(), _GATEWAY, protected=protected, reasoning_effort=reasoning_effort)
    workspace = tmp_path / "work"
    for name, content in launch.files.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (workspace / "task").mkdir(parents=True)
    (workspace / "task" / "PROMPT.md").write_text("-Fix `solution.py` and say $(echo done).")
    bin_dir = tmp_path / "home" / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    fake = bin_dir / "opencode2"
    fake.write_text(_FAKE_OPENCODE2)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    # The sandbox layers the launch env over the inherited env.
    env = {
        "OPENCODE_CONFIG": "/elsewhere/opencode.json",
        "OPENCODE_CONFIG_CONTENT": '{"providers": {"openai": {}}}',
        **launch.env,
        "PATH": os.defpath,
        "HOME": str(tmp_path / "home"),
        "FAKE_LOG": str(log),
        "FAKE_ANSWER": _RECORDED_STDOUT,
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
        check=True,
    )
    return result.stdout, log.read_text(), workspace


def _run_install(tmp_path: Path, *, curl_exit: int = 0, sha_exit: int = 0) -> tuple[int, str, Path, Path]:
    """Run the open install command under ``set -eu`` with fake download tools.

    Args:
        tmp_path: Scratch directory for the fake tools, ``HOME`` and ``TMPDIR``.
        curl_exit: Exit status the fake ``curl`` returns.
        sha_exit: Exit status the fake ``sha256sum`` returns.

    Returns:
        The exit status, the stdout, the ``TMPDIR`` folder, and the
        downloaded file path the fake ``curl`` logged.
    """
    install = build_launch(_target(), _GATEWAY).install_command
    assert install is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, script in _FAKE_INSTALL_TOOLS.items():
        tool = bin_dir / name
        tool.write_text(script)
        tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    log = tmp_path / "curl.log"
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.defpath}",
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(scratch),
        "FAKE_LOG": str(log),
        "FAKE_CURL_EXIT": str(curl_exit),
        "FAKE_SHA_EXIT": str(sha_exit),
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
    return result.returncode, result.stdout, scratch, Path(log.read_text().strip())


def _logged(calls: str, name: str) -> str:
    """Return the value the fake binary logged for ``name``.

    Args:
        calls: The fake binary's call log.
        name: The logged variable name.

    Returns:
        The logged value.
    """
    return next(line for line in calls.splitlines() if line.startswith(f"{name}=")).split("=", 1)[1]


def test_opencode_v2_is_registered_after_v1_with_its_pin_and_efforts() -> None:
    """List opencode_v2 right after V1, pinned to the exact release, with V2's effort variants."""
    spec = REGISTRY[BLACKBOX_HARNESS_OPENCODE_V2]
    assert (spec.binary, spec.version, spec.output_format, spec.position) == ("opencode2", "v2.2.7", "opencode", 32)
    assert spec.supported_efforts == ("low", "medium", "high")
    harnesses = list(REGISTRY)
    assert harnesses.index(BLACKBOX_HARNESS_OPENCODE_V2) == harnesses.index(BLACKBOX_HARNESS_OPENCODE) + 1
    assert BLACKBOX_HARNESS_OPENCODE_V2 in BLACKBOX_HARNESSES
    assert pinned_harness_check(BLACKBOX_HARNESS_OPENCODE_V2) == (
        "opencode2 --version 2>/dev/null | grep -Eq '(^| )v2[.]2[.]7( |$)'"
    )


def test_pinned_check_matches_the_cli_version_line(tmp_path: Path) -> None:
    """Accept the ``opencode v2.2.7`` line that 2.2.7 prints, and refuse another release."""
    check = pinned_harness_check(BLACKBOX_HARNESS_OPENCODE_V2)
    assert check is not None
    fake = tmp_path / "opencode2"
    for line, code in (("opencode v2.2.7", 0), ("opencode v2.2.70", 1), ("opencode v2.3.0", 1)):
        fake.write_text(f"#!/bin/sh\necho '{line}'\n")
        fake.chmod(0o755)
        result = subprocess.run(
            ["/bin/sh", "-c", check], env={"PATH": f"{tmp_path}{os.pathsep}{os.defpath}"}, check=False
        )
        assert result.returncode == code, line


def test_open_install_downloads_the_pinned_release_and_checks_its_digest() -> None:
    """Fetch one pinned tarball, verify its sha256 before unpacking, and never pipe a script into a shell."""
    install = build_launch(_target(), _GATEWAY).install_command

    assert install is not None
    check = pinned_harness_check(BLACKBOX_HARNESS_OPENCODE_V2)
    assert install.startswith(f'export PATH="$HOME/.local/bin":"$PATH"; {check} || {{ ')
    assert f"https://github.com/anomalyco/opencode/releases/download/v{OPENCODE_V2_VERSION}/$f" in install
    assert 'f="opencode-linux-$a.tar.gz"' in install
    assert f"x86_64|amd64) a=x64-baseline; s={OPENCODE_V2_SHA256['x64-baseline']};;" in install
    for arch, digest in OPENCODE_V2_SHA256.items():
        assert f"a={arch}; s={digest};;" in install
        assert len(digest) == 64
    assert install.index("sha256sum -c -") < install.index("tar -xzf")
    assert "| bash" not in install
    assert "| sh " not in install
    assert "npm" not in install
    assert "sudo" not in install


def test_install_puts_opencode2_on_the_local_bin_and_removes_the_download_folder(tmp_path: Path) -> None:
    """Install the release binary as ``opencode2``, beside V1's ``opencode``, and let the caller's script continue."""
    code, stdout, scratch, download = _run_install(tmp_path)

    assert code == 0
    assert stdout.strip() == "after-install"
    assert download.name == "opencode-linux-arm64.tar.gz"
    assert (tmp_path / "home" / ".local" / "bin" / "opencode2").is_file()
    assert not (tmp_path / "home" / ".local" / "bin" / "opencode").exists()
    assert not download.parent.exists()
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize(("curl_exit", "sha_exit", "code"), [(22, 0, 22), (0, 1, 1)])
def test_failed_download_or_digest_check_removes_the_download_folder_and_fails(
    tmp_path: Path, curl_exit: int, sha_exit: int, code: int
) -> None:
    """Remove the download folder when the download or digest check fails, and exit with that step's status."""
    exit_code, stdout, scratch, download = _run_install(tmp_path, curl_exit=curl_exit, sha_exit=sha_exit)

    assert exit_code == code
    assert "after-install" not in stdout
    assert not (tmp_path / "home" / ".local" / "bin" / "opencode2").exists()
    assert not download.parent.exists()
    assert list(scratch.iterdir()) == []


def test_open_launch_sends_model_calls_only_to_the_gateway() -> None:
    """Point V2's only provider at the gateway, keep the key in the env, and turn updates and free models off."""
    launch = build_launch(_target(), _GATEWAY)

    assert launch.instructions_file == "AGENTS.md"
    assert set(launch.files) == {"opencode.json"}
    assert json.loads(launch.files["opencode.json"]) == {
        "update": "disable",
        "share": "disabled",
        "providers": {
            "app": {
                "name": "Model gateway",
                "package": "@opencode/ai/providers/openai-compatible",
                "settings": {"baseURL": _GATEWAY.url, "apiKey": "{env:APP_API_KEY}"},
                "models": {"acme/agent-1": {"name": "acme/agent-1"}},
            }
        },
        "model": "app/acme/agent-1",
    }
    assert _GATEWAY.api_key not in launch.files["opencode.json"]
    assert _GATEWAY.url not in launch.run_command
    assert launch.run_command == (
        'export PATH="$HOME/.local/bin":"$PATH" && '
        'opencode2 run --standalone --format json --auto --model "app/$APP_MODEL" '
        '-- "$(cat "$APP_PROMPT_FILE")" < /dev/null'
    )
    assert launch.env["APP_MODEL"] == "acme/agent-1"
    assert launch.env["APP_API_KEY"] == _GATEWAY.api_key
    assert launch.env["OPENCODE_DISABLE_AUTOUPDATE"] == "true"
    assert launch.env["OPENCODE_DISABLE_FREE_MODELS"] == "true"
    assert launch.env["OTEL_EXPORTER_OTLP_ENDPOINT"] == ""
    assert not set(_PROVIDER_KEYS) & set(launch.env)
    assert launch.parse_output is parse_opencode_output


@pytest.mark.parametrize("effort", OPENCODE_V2_EFFORTS)
def test_effort_reaches_the_model_variant_unchanged(effort: str) -> None:
    """Pass the user's level as the ``#variant`` suffix of the model, with no other change."""
    plain = build_launch(_target(), _GATEWAY)
    launch = build_launch(_target(), _GATEWAY, reasoning_effort=effort)

    assert f'--model "app/$APP_MODEL#{effort}" ' in launch.run_command
    assert launch.run_command == plain.run_command.replace('"app/$APP_MODEL"', f'"app/$APP_MODEL#{effort}"')
    assert launch.files == plain.files
    assert launch.env == plain.env


@pytest.mark.parametrize("effort", ["none", "minimal", "xhigh", "max", "turbo"])
def test_unsupported_effort_fails_before_the_run(effort: str) -> None:
    """Refuse a level that V2 has no variant for, rather than let the CLI fail after paid setup."""
    with pytest.raises(ServiceError, match=f"does not support the effort '{effort}'"):
        build_launch(_target(), _GATEWAY, reasoning_effort=effort)


def test_protected_launch_checks_the_pin_offline_and_blocks_other_providers() -> None:
    """Use the offline version probe, blank other provider keys, stop the models fetch, and keep the gateway key."""
    launch = build_launch(_target(), _GATEWAY, protected=True)

    assert launch.install_command == pinned_harness_check(BLACKBOX_HARNESS_OPENCODE_V2)
    blanked = [name for name, value in _PROTECTED_ENV.items() if value == ""]
    assert set(_PROVIDER_KEYS) <= set(blanked)
    for name in blanked:
        assert launch.env[name] == ""
    assert launch.env["OPENCODE_DISABLE_MODELS_FETCH"] == "true"
    assert launch.env["OPENCODE_CONFIG"] == ""
    assert launch.env["OPENCODE_CONFIG_CONTENT"] == "{}"
    assert launch.env["OPENCODE_DISABLE_FREE_MODELS"] == "true"
    assert launch.env["APP_API_KEY"] == _GATEWAY.api_key


def test_protected_run_points_opencode2_at_the_relay_and_returns_its_answer(tmp_path: Path) -> None:
    """Run the protected command against a fake opencode2: the relay URL, argv, key and answer come through."""
    stdout, calls, workspace = _run_with_fake_opencode2(tmp_path, protected=True, reasoning_effort="high")

    (run,) = [line for line in calls.splitlines() if line.startswith("ARGS")]
    assert run == (
        "ARGS [run] [--standalone] [--format] [json] [--auto] [--model] [app/acme/agent-1#high] "
        "[--] [-Fix `solution.py` and say $(echo done).]"
    )
    config = json.loads((workspace / "opencode.json").read_text())
    assert config["providers"]["app"]["settings"]["baseURL"] == _RELAY
    assert _GATEWAY.url not in (workspace / "opencode.json").read_text()
    assert _logged(calls, "APP_API_KEY") == "gateway-token"
    assert _logged(calls, "OPENAI_API_KEY") == ""
    assert _logged(calls, "OPENCODE_CONFIG") == ""
    assert _logged(calls, "OPENCODE_CONFIG_CONTENT") == "{}"
    assert _logged(calls, "STDIN") == ""
    assert parse_output("opencode", stdout) == (
        "I fixed solution.py.\n\nThe answer is 42.",
        {"input_tokens": 320, "output_tokens": 42},
    )


def test_recorded_output_parses_to_the_answer_and_usage() -> None:
    """Parse a recorded V2 JSON stream to its last text part and summed step tokens."""
    assert parse_opencode_output(_RECORDED_STDOUT) == (
        "I fixed solution.py.\n\nThe answer is 42.",
        {"input_tokens": 320, "output_tokens": 42},
    )
    assert parse_opencode_output("") == (None, {})
    assert launch_payload(build_launch(_target(), _GATEWAY))["output_format"] == "opencode"

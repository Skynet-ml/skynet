"""Tests for the fx harness: registry entry, checksummed install, gateway-only launch and JSON parsing."""

from __future__ import annotations

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

from core.models.blackbox import BLACKBOX_HARNESS_FX, BLACKBOX_HARNESSES, BLACKBOX_TARGET_AGENT, BlackboxTarget

from ..harness import GatewayConfig, build_launch, launch_payload, pinned_harness_check
from ..harness_bridge import parse_output, parse_pi_output
from ..harnesses import REGISTRY
from ..harnesses.fx import FX_SETTINGS_FILE, FX_SHA256, FX_TO_PI, FX_VERSION

_GATEWAY = GatewayConfig(url="https://gw.example/v1", api_key="gateway-token")
_RELAY = "http://127.0.0.1:3210/v1"
_DIRECT_KEYS = ("AI_GATEWAY_API_KEY", "VERCEL_AI_GATEWAY_API_KEY", "VERCEL_OIDC_TOKEN", "OPENAI_API_KEY")
_ANSWER = "I read solution.py and fixed the off-by-one error.\n\nThe answer is 42."
# Synthetic result in the shape of ``fx ask --json`` 0.0.16 (``renderFinalJsonResult``):
# one object on stdout, cache keys only because this provider reports them.
_RECORDED_STDOUT = (
    json.dumps(
        {
            "output": "Let me read the file.\n\n" + _ANSWER,
            "final_output": "\n" + _ANSWER + "\n",
            "exit_code": 0,
            "model": "target-model",
            "resolved_provider": "runtime",
            "session_id": "sess-synthetic-1",
            "steps": 3,
            "tool_calls": [{"name": "read", "status": "completed"}, {"name": "edit", "status": "completed"}],
            "usage": {"input_tokens": 1834, "output_tokens": 212, "cache_read_tokens": 1024, "cache_write_tokens": 64},
        }
    )
    + "\n"
)
# Stand-in for the fx binary: it records its call and the settings it reads, then prints a fixed answer.
_FAKE_FX = """#!/bin/sh
{
  printf 'ARGS'; for a in "$@"; do printf ' [%s]' "$a"; done; printf '\\n'
  printf 'FX_MODEL=%s\\n' "$FX_MODEL"
  printf 'FX_PROVIDER=%s\\n' "$FX_PROVIDER"
  printf 'APP_API_KEY=%s\\n' "$APP_API_KEY"
  printf 'AI_GATEWAY_API_KEY=%s\\n' "$AI_GATEWAY_API_KEY"
  printf 'SETTINGS='; cat "$HOME/.fx/settings.json" | tr -d '\\n'; printf '\\n'
} >> "$FAKE_LOG"
printf '%s' "$FAKE_ANSWER"
exit "${FAKE_STATUS:-0}"
"""
# Stand-in for curl: it logs the URL and writes ``$FAKE_TARBALL`` to the ``-o`` path.
_FAKE_CURL = """#!/bin/sh
out=""; url=""
while [ $# -gt 0 ]; do
  case "$1" in -o) out="$2"; shift;; -*) ;; *) url="$1";; esac
  shift
done
printf 'CURL %s\\n' "$url" >> "$FAKE_LOG"
cp "$FAKE_TARBALL" "$out"
"""


def _target() -> BlackboxTarget:
    """Build an fx agent target.

    Returns:
        The target.
    """
    return BlackboxTarget(kind=BLACKBOX_TARGET_AGENT, harness=BLACKBOX_HARNESS_FX, model="target-model")


def _settings(launch_files: dict[str, str]) -> dict[str, object]:
    """Read the generated fx settings from a launch's files.

    Args:
        launch_files: The launch's generated files.

    Returns:
        The parsed settings.
    """
    return json.loads(launch_files[FX_SETTINGS_FILE])


def _run_with_fake_fx(
    tmp_path: Path, *, protected: bool, answer: str = _RECORDED_STDOUT, status: int = 0
) -> tuple[str, str, int]:
    """Run the built fx command in a scratch workspace against a fake ``fx`` binary.

    Args:
        tmp_path: Scratch directory for the workspace, the fake binary and its log.
        protected: Build the protected launch, with the sandbox-local relay set.
        answer: What the fake fx prints on stdout.
        status: The fake fx's exit status.

    Returns:
        The command's stdout, the fake binary's call log and the command's exit status.
    """
    launch = build_launch(_target(), _GATEWAY, protected=protected)
    workspace = tmp_path / "work"
    for name, content in launch.files.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (workspace / "task").mkdir(parents=True)
    (workspace / "task" / "PROMPT.md").write_text("Fix `solution.py` and say $(echo done).")
    bin_dir = tmp_path / "home" / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    fake = bin_dir / "fx"
    fake.write_text(_FAKE_FX)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    env = {
        **launch.env,
        # The converter runs on ``python3``; put this interpreter first so the run needs no system Python.
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.defpath}",
        "HOME": str(tmp_path / "home"),
        "FAKE_LOG": str(log),
        "FAKE_ANSWER": answer,
        "FAKE_STATUS": str(status),
    }
    if protected:
        env["APP_BUDGET_RELAY_URL"] = _RELAY
    result = subprocess.run(
        launch.run_command, shell=True, cwd=workspace, env=env, capture_output=True, text=True, timeout=10, check=False
    )
    return result.stdout, log.read_text(), result.returncode


def _fx_tarball(path: Path) -> str:
    """Write a release-shaped tarball whose ``fx`` reports the pinned version.

    Args:
        path: Where to write the tarball.

    Returns:
        The tarball's SHA-256.
    """
    script = f"#!/bin/sh\necho 'fx {FX_VERSION}'\n".encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("fx")
        info.size = len(script)
        info.mode = 0o755
        archive.addfile(info, io.BytesIO(script))
    path.write_bytes(buffer.getvalue())
    return hashlib.sha256(buffer.getvalue()).hexdigest()


def _run_install(tmp_path: Path, command: str, *, arch: str = "x86_64") -> tuple[subprocess.CompletedProcess[str], str]:
    """Run an install command with fake ``curl`` and ``uname`` and no network.

    Args:
        tmp_path: Scratch directory for the home, the fakes and the tarball.
        command: The install command.
        arch: What the fake ``uname -m`` prints.

    Returns:
        The finished process and the fake curl's log.
    """
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    for name, body in (("curl", _FAKE_CURL), ("uname", f"#!/bin/sh\necho {arch}\n")):
        fake = fakes / name
        fake.write_text(body)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    path = os.pathsep.join([str(fakes), "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    if shutil.which("sha256sum", path=path) is None:
        pytest.skip("sha256sum is not installed")
    log = tmp_path / "curl.log"
    log.touch()
    env = {"PATH": path, "HOME": str(tmp_path / "home"), "FAKE_LOG": str(log), "FAKE_TARBALL": str(tmp_path / "fx.tgz")}
    result = subprocess.run(command, shell=True, env=env, capture_output=True, text=True, timeout=20, check=False)
    return result, log.read_text()


def test_fx_is_registered_before_cline_with_its_pin() -> None:
    """List fx in the registry and the request model, pinned to the exact release."""
    spec = REGISTRY[BLACKBOX_HARNESS_FX]
    assert (spec.binary, spec.version, spec.output_format, spec.position) == ("fx", "0.0.16", "pi", 62)
    ids = list(REGISTRY)
    assert ids.index("antigravity_sdk") < ids.index(BLACKBOX_HARNESS_FX) < ids.index("cline")
    assert BLACKBOX_HARNESS_FX in BLACKBOX_HARNESSES
    assert pinned_harness_check(BLACKBOX_HARNESS_FX) == "fx --version 2>/dev/null | grep -Eq '(^| )0[.]0[.]16( |$)'"


def test_open_launch_installs_the_pin_and_sends_model_calls_only_to_the_gateway() -> None:
    """Install the pinned release, seed the gateway provider from a file, and keep direct keys out of the env."""
    launch = build_launch(_target(), _GATEWAY)

    assert launch.instructions_file == "AGENTS.md"
    assert launch.install_command.startswith(
        "export PATH=\"$HOME/.local/bin:$PATH\"; fx --version 2>/dev/null | grep -Eq '(^| )0[.]0[.]16( |$)' || "
    )
    assert launch.install_command.endswith("} && fx --version")
    assert _settings(launch.files) == {
        "providers": {
            "runtime": {
                "protocol": "openai-chat-completions",
                "base_url": _GATEWAY.url,
                "auth": {"type": "bearer", "env": "APP_API_KEY"},
            }
        }
    }
    assert _GATEWAY.url not in launch.run_command
    assert _GATEWAY.api_key not in launch.run_command
    assert _GATEWAY.api_key not in launch.files[FX_SETTINGS_FILE]
    assert launch.run_command == (
        'export PATH="$HOME/.local/bin:$PATH"; mkdir -p "$HOME/.fx" && '
        f'cp {FX_SETTINGS_FILE} "$HOME/.fx/settings.json" && '
        'fx ask --yolo --json -- "$(cat "$APP_PROMPT_FILE")" < /dev/null > "$HOME/.fx/ask.json"; fx_status=$?; '
        f"python3 -I -c '{FX_TO_PI}' \"$HOME/.fx/ask.json\"; (exit $fx_status)"
    )
    assert "'" not in FX_TO_PI
    assert list(launch.files) == [FX_SETTINGS_FILE]
    assert {name: launch.env[name] for name in ("FX_MODEL", "FX_PROVIDER", "FX_AUTO_UPGRADE", "FX_SOUND")} == {
        "FX_MODEL": "target-model",
        "FX_PROVIDER": "runtime",
        "FX_AUTO_UPGRADE": "0",
        "FX_SOUND": "0",
    }
    assert launch.env["APP_API_KEY"] == _GATEWAY.api_key
    assert launch.env["APP_GATEWAY_URL"] == _GATEWAY.url
    assert not set(_DIRECT_KEYS) & set(launch.env)
    assert launch.parse_output is parse_pi_output


def test_install_downloads_the_pinned_asset_and_checks_its_sha256() -> None:
    """Fetch the v0.0.16 tarball for the box's arch and verify the pinned hash before anything is installed."""
    command = build_launch(_target(), _GATEWAY).install_command

    assert FX_SHA256 == {
        "x86_64": "f0fb822c7be546c80bcc89d50521bc1454435e0c6f586e8420c4e2b84fa80599",
        "aarch64": "ca9441b104357e2105039dbfd4c2eaf0d6ee65a84085f23f51e880c95cbf46af",
    }
    assert f"x86_64|amd64) a=x86_64 h={FX_SHA256['x86_64']};;" in command
    assert f"aarch64|arm64) a=aarch64 h={FX_SHA256['aarch64']};;" in command
    assert '"https://releases.fx.sh/v0.0.16/fx-linux-$a.tar.gz"' in command
    assert command.index("sha256sum -c -") < command.index("tar -xzf") < command.index('"$HOME/.local/bin/fx"')
    assert "setup.sh" not in command
    assert not re.search(r"\|\s*(ba|z)?sh\b", command)


def test_install_fails_closed_on_a_hash_mismatch(tmp_path: Path) -> None:
    """Stop with a nonzero status and install nothing when the downloaded tarball is not the pinned one."""
    _fx_tarball(tmp_path / "fx.tgz")
    result, curl_log = _run_install(tmp_path, build_launch(_target(), _GATEWAY).install_command)

    assert result.returncode != 0
    assert curl_log == "CURL https://releases.fx.sh/v0.0.16/fx-linux-x86_64.tar.gz\n"
    assert not (tmp_path / "home" / ".local" / "bin" / "fx").exists()


def test_install_fails_closed_on_an_unknown_arch(tmp_path: Path) -> None:
    """Stop before any download when the box's arch has no pinned hash."""
    _fx_tarball(tmp_path / "fx.tgz")
    result, curl_log = _run_install(tmp_path, build_launch(_target(), _GATEWAY).install_command, arch="riscv64")

    assert result.returncode != 0
    assert "unsupported architecture riscv64" in result.stderr
    assert curl_log == ""
    assert not (tmp_path / "home" / ".local" / "bin" / "fx").exists()


def test_install_puts_a_verified_tarball_on_the_path(tmp_path: Path) -> None:
    """Install and run the binary when the tarball matches the hash, here a synthetic one swapped into the command."""
    digest = _fx_tarball(tmp_path / "fx.tgz")
    command = build_launch(_target(), _GATEWAY).install_command.replace(FX_SHA256["aarch64"], digest)
    result, curl_log = _run_install(tmp_path, command, arch="aarch64")

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"fx {FX_VERSION}\n"
    assert curl_log == "CURL https://releases.fx.sh/v0.0.16/fx-linux-aarch64.tar.gz\n"
    assert (tmp_path / "home" / ".local" / "bin" / "fx").is_file()


@pytest.mark.parametrize(
    ("effort", "expected"),
    [(None, None), ("none", "none"), ("minimal", "minimal"), ("high", "high"), ("max", "max"), ("turbo", None)],
)
def test_reasoning_effort_goes_into_the_settings(effort: str | None, expected: str | None) -> None:
    """Write the user's effort as fx's ``effort`` setting, or leave fx's default for an unknown name."""
    settings = _settings(build_launch(_target(), _GATEWAY, reasoning_effort=effort).files)
    assert settings.get("effort") == expected


def test_protected_launch_checks_the_pin_offline_and_blocks_direct_routes() -> None:
    """Use the offline version probe, blank every direct Vercel route, and keep the gateway key."""
    launch = build_launch(_target(), _GATEWAY, protected=True)

    assert launch.install_command == pinned_harness_check(BLACKBOX_HARNESS_FX)
    for name in (
        "AI_GATEWAY_API_KEY",
        "VERCEL_AI_GATEWAY_API_KEY",
        "VERCEL_OIDC_TOKEN",
        "FX_GATEWAY_BASE_URL",
        "FX_GATEWAY_CHAT_URL",
        "FX_E2E_GATEWAY_CHAT_URL",
    ):
        assert launch.env[name] == ""
    assert launch.env["DO_NOT_TRACK"] == "1"
    assert launch.env["FX_AUTO_UPGRADE"] == "0"
    assert launch.env["APP_API_KEY"] == _GATEWAY.api_key
    assert "OPENAI_API_KEY" not in launch.env


def test_protected_run_points_fx_at_the_relay_and_returns_its_answer(tmp_path: Path) -> None:
    """Run the protected command against a fake fx: the relay URL, gateway token and answer come through."""
    stdout, calls, status = _run_with_fake_fx(tmp_path, protected=True)

    run = next(line for line in calls.splitlines() if line.startswith("ARGS"))
    assert run == "ARGS [ask] [--yolo] [--json] [--] [Fix `solution.py` and say $(echo done).]"
    assert "FX_MODEL=target-model\n" in calls
    assert "FX_PROVIDER=runtime\n" in calls
    assert "APP_API_KEY=gateway-token\n" in calls
    assert "AI_GATEWAY_API_KEY=\n" in calls
    settings = json.loads(next(line for line in calls.splitlines() if line.startswith("SETTINGS=")).split("=", 1)[1])
    assert settings["providers"]["runtime"]["base_url"] == _RELAY
    assert _GATEWAY.url not in calls
    assert status == 0
    assert parse_output("pi", stdout) == (_ANSWER, {"input_tokens": 1834, "output_tokens": 212})


def test_run_keeps_fx_exit_status_and_reports_its_error(tmp_path: Path) -> None:
    """Return fx's nonzero status, with its usage and error on stdout for the bridge."""
    failed = json.dumps(
        {
            "output": "",
            "final_output": "",
            "exit_code": 1,
            "error": "provider returned 401",
            "usage": {"input_tokens": 40},
        }
    )
    stdout, _, status = _run_with_fake_fx(tmp_path, protected=False, answer=failed + "\n", status=1)

    assert status == 1
    assert parse_pi_output(stdout) == (None, {"input_tokens": 40, "output_tokens": 0})
    assert '"type": "error"' in stdout
    assert "provider returned 401" in stdout


def test_run_prints_nothing_when_fx_writes_no_result(tmp_path: Path) -> None:
    """Leave stdout empty when fx dies before its JSON result, and keep the status."""
    stdout, _, status = _run_with_fake_fx(tmp_path, protected=False, answer="", status=2)

    assert (stdout, status) == ("", 2)


def test_open_run_reads_the_gateway_url_from_the_generated_file(tmp_path: Path) -> None:
    """Without a relay, give fx the gateway URL the launch wrote."""
    _, calls, _ = _run_with_fake_fx(tmp_path, protected=False)

    settings = json.loads(next(line for line in calls.splitlines() if line.startswith("SETTINGS=")).split("=", 1)[1])
    assert settings["providers"]["runtime"]["base_url"] == _GATEWAY.url


def _convert(tmp_path: Path, stdout: str) -> str:
    """Run :data:`FX_TO_PI` on a saved fx result, the way the run command does.

    Args:
        tmp_path: Scratch directory for the result file.
        stdout: What fx wrote to the result file.

    Returns:
        The converter's stdout.
    """
    result_file = tmp_path / "ask.json"
    result_file.write_text(stdout)
    return subprocess.run(
        [sys.executable, "-I", "-c", FX_TO_PI, str(result_file)], capture_output=True, text=True, timeout=10, check=True
    ).stdout


def test_recorded_json_parses_to_the_final_answer_and_usage(tmp_path: Path) -> None:
    """Read ``final_output`` (not the interim ``output``) and the token counts from a recorded result."""
    converted = _convert(tmp_path, "progress noise\n" + _RECORDED_STDOUT)

    assert converted.count("\n") == 1
    assert parse_pi_output(converted) == (_ANSWER, {"input_tokens": 1834, "output_tokens": 212})
    assert launch_payload(build_launch(_target(), _GATEWAY))["output_format"] == "pi"


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ("", (None, {})),
        ("  \n", (None, {})),
        ("fx: starting\nnot json at all\n", (None, {})),
        ('{"final_output": "cut off', (None, {})),
        ("[1, 2, 3]\n", (None, {})),
        ('{"type": "progress"}\n', (None, {})),
        (
            '{"final_output": "", "usage": {"input_tokens": 9, "output_tokens": 1}}',
            (None, {"input_tokens": 9, "output_tokens": 1}),
        ),
        ('{"final_output": "done", "usage": null}', ("done", {"input_tokens": 0, "output_tokens": 0})),
        (
            '{"final_output": "done", "usage": {"input_tokens": null, "output_tokens": "7"}}',
            ("done", {"input_tokens": 0, "output_tokens": 0}),
        ),
        (
            '{"final_output": 5, "usage": {"input_tokens": true, "output_tokens": -3}}',
            (None, {"input_tokens": 0, "output_tokens": 0}),
        ),
        (
            'warn\n{"final_output": "first", "usage": {"input_tokens": 1}}\n{"broken\n'
            '{"final_output": "second", "usage": {"input_tokens": 5, "output_tokens": 2}}\n',
            ("second", {"input_tokens": 5, "output_tokens": 2}),
        ),
    ],
)
def test_parser_skips_malformed_lines_and_bad_counts(
    tmp_path: Path, stdout: str, expected: tuple[str | None, dict[str, int]]
) -> None:
    """Skip non-JSON, truncated and non-object lines, count bad token values as 0, and keep the last result."""
    assert parse_pi_output(_convert(tmp_path, stdout)) == expected


def test_converter_prints_nothing_for_a_missing_result_file(tmp_path: Path) -> None:
    """Exit 0 with no output when fx wrote no result file, so the run keeps fx's own status."""
    missing = subprocess.run(
        [sys.executable, "-I", "-c", FX_TO_PI, str(tmp_path / "absent.json")],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert (missing.returncode, missing.stdout) == (0, "")

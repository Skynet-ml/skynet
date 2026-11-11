"""Tests for the Grok Build CLI harness: registry entry, pinned install, gateway-only launch, effort and output parsing."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tomllib
from pathlib import Path

import pytest

from core.exceptions import ServiceError
from core.models.blackbox import (
    BLACKBOX_HARNESS_GROK_BUILD,
    BLACKBOX_HARNESSES,
    BLACKBOX_TARGET_AGENT,
    BlackboxProposer,
    BlackboxTarget,
)

from ..harness import GatewayConfig, build_launch, launch_payload, pinned_harness_check
from ..harness_bridge import parse_output, parse_pi_output
from ..harnesses import REGISTRY
from ..harnesses.grok_build import _SESSION_ID, GROK_CONFIG_FILE, GROK_EFFORTS, GROK_SHA256, GROK_VERSION
from ..service import _check_proposer_effort

_GATEWAY = GatewayConfig(url="https://gw.example/v1", api_key="gateway-token")
_RELAY = "http://127.0.0.1:3210/v1"
_MODEL = "acme/grok-4"
_DIRECT_KEYS = ("XAI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GROK_DEPLOYMENT_KEY")
_PROMPT = "Fix `solution.py` and say $(echo done)."
# Synthetic ``grok --output-format streaming-json`` stdout: per-call usage, then
# the ``end`` event with the run total, cache and reasoning buckets.
_RECORDED_STREAM = "\n".join(
    json.dumps(event)
    for event in (
        {"type": "text", "data": "Working on it."},
        {"type": "usage", "usage": {"input_tokens": 60, "output_tokens": 10}},
        {"type": "usage", "usage": {"input_tokens": 40, "output_tokens": 30}},
        {
            "type": "end",
            "stopReason": "end_turn",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 40,
                "cache_read_input_tokens": 20,
                "cache_creation_input_tokens": 5,
                "reasoning_tokens": 30,
            },
        },
    )
)
# Synthetic ``chat_history.jsonl`` in the message shapes Harbor's Grok models read.
_RECORDED_HISTORY = "\n".join(
    json.dumps(message)
    for message in (
        {"type": "system", "content": "You are Grok."},
        {"type": "user", "content": _PROMPT},
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "Check solution.py."}]},
        {
            "type": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "name": "bash", "arguments": '{"command": "pytest"}'}],
        },
        {"type": "tool_result", "tool_call_id": "c1", "content": "1 passed"},
        {
            "type": "assistant",
            "content": [{"type": "text", "text": "I fixed the off-by-one error.\n\n"}, {"type": "text", "text": "42."}],
        },
    )
)
# Stand-in for the Grok binary: it records its call and config, writes the
# session history under its home, prints a fixed stream and exits as told.
_FAKE_GROK = """#!/bin/sh
{
  printf 'ARGS'; for a in "$@"; do printf ' [%s]' "$a"; done; printf '\\n'
  printf 'HOME=%s\\n' "$HOME"
  for k in XAI_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY GROK_DEPLOYMENT_KEY APP_API_KEY GROK_LOG_FILE; do
    eval "printf '%s=%s\\n' $k \\"\\$$k\\""
  done
} >> "$FAKE_LOG"
cp "$HOME/.grok/config.toml" "$FAKE_CONFIG_COPY"
s="$HOME/.grok/sessions/workspace/$FAKE_SESSION"
mkdir -p "$s" "$HOME/.grok/sessions/workspace/sub-agent"
printf '%s\\n' "$FAKE_HISTORY" > "$s/chat_history.jsonl"
printf '%s\\n' '{"type": "assistant", "content": "subagent text"}' > "$HOME/.grok/sessions/workspace/sub-agent/chat_history.jsonl"
printf '%s\\n' "$FAKE_STREAM"
exit "${FAKE_EXIT:-0}"
"""
# Stand-ins for the install step's tools. ``curl`` logs the URL and writes a
# fake ``grok`` that reports ``FAKE_VERSION``; ``zstd`` copies it out of the
# ``.zst`` file; ``sha256sum`` logs the line it checks and exits as told.
# macOS ``mktemp -d`` ignores ``TMPDIR``, so a fake one keeps the folder in scratch.
_FAKE_INSTALL_TOOLS = {
    "uname": "#!/bin/sh\necho x86_64\n",
    "mktemp": '#!/bin/sh\nd="$TMPDIR/download.$$"\nmkdir "$d" && echo "$d"\n',
    "curl": (
        '#!/bin/sh\nprintf \'curl %s\\n\' "$4" >> "$FAKE_LOG"\n'
        '[ "${FAKE_CURL_EXIT:-0}" -eq 0 ] || exit "$FAKE_CURL_EXIT"\n'
        'printf \'#!/bin/sh\\necho "grok %s (50ca579fa996)"\\n\' "$FAKE_VERSION" > "$3"\n'
    ),
    "sha256sum": '#!/bin/sh\nprintf \'sha256sum %s\\n\' "$(cat)" >> "$FAKE_LOG"\nexit "${FAKE_SHA_EXIT:-0}"\n',
}
_FAKE_ZSTD = '#!/bin/sh\nprintf \'zstd %s %s\\n\' "$4" "$5" >> "$FAKE_LOG"\ncp "$5" "$4"\n'
# Host tools the install needs. The run sees only these and the fakes, so a
# real ``zstd`` on the test machine never changes which download it takes.
_HOST_TOOLS = ("sh", "cat", "cp", "chmod", "grep", "mkdir", "mv", "rm")


def _target() -> BlackboxTarget:
    """Build a Grok Build agent target.

    Returns:
        The target.
    """
    return BlackboxTarget(kind=BLACKBOX_TARGET_AGENT, harness=BLACKBOX_HARNESS_GROK_BUILD, model=_MODEL)


def _run_with_fake_grok(
    tmp_path: Path,
    *,
    protected: bool,
    stream: str = _RECORDED_STREAM,
    fake_exit: int = 0,
    prompt: str = _PROMPT,
) -> tuple[subprocess.CompletedProcess[str], str, dict, Path]:
    """Run the built Grok command in a scratch workspace against a fake ``grok`` binary.

    Args:
        tmp_path: Scratch directory for the workspace, the fake binary and its log.
        protected: Build the protected launch, with the sandbox-local relay set.
        stream: The NDJSON the fake binary prints.
        fake_exit: Exit status the fake binary returns.
        prompt: The task prompt the run reads from its prompt file.

    Returns:
        The finished command, the fake binary's call log, the config Grok saw, and the workspace.
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
    fake = bin_dir / "grok"
    fake.write_text(_FAKE_GROK)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    config_copy = tmp_path / "seen.toml"
    env = {
        **launch.env,
        "PATH": f"{bin_dir}{os.pathsep}{os.defpath}",
        "HOME": str(tmp_path / "home"),
        "XAI_API_KEY": "leaked-host-key",
        "FAKE_LOG": str(log),
        "FAKE_CONFIG_COPY": str(config_copy),
        "FAKE_SESSION": _SESSION_ID,
        "FAKE_HISTORY": _RECORDED_HISTORY,
        "FAKE_STREAM": stream,
        "FAKE_EXIT": str(fake_exit),
    }
    env.update(launch.env)
    if protected:
        env["APP_BUDGET_RELAY_URL"] = _RELAY
    shell = shutil.which("bash") or "/bin/sh"
    result = subprocess.run(
        [shell, "-c", launch.run_command],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result, log.read_text(), tomllib.loads(config_copy.read_text()), workspace


def _run_install(
    tmp_path: Path, *, curl_exit: int = 0, sha_exit: int = 0, version: str = GROK_VERSION, zstd: bool = False
) -> tuple[int, str, Path, list[str]]:
    """Run the open install command under ``set -eu`` with fake download tools.

    Args:
        tmp_path: Scratch directory for the fake tools, ``HOME`` and ``TMPDIR``.
        curl_exit: Exit status the fake ``curl`` returns.
        sha_exit: Exit status the fake ``sha256sum`` returns.
        version: Version the downloaded fake ``grok`` reports.
        zstd: Put a fake ``zstd`` on ``PATH``.

    Returns:
        The exit status, the stdout, the ``TMPDIR`` folder, and the fake tools' log lines.
    """
    install = build_launch(_target(), _GATEWAY).install_command
    assert install is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fakes = {**_FAKE_INSTALL_TOOLS, **({"zstd": _FAKE_ZSTD} if zstd else {})}
    for name, script in fakes.items():
        tool = bin_dir / name
        tool.write_text(script)
        tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    for name in _HOST_TOOLS:
        found = shutil.which(name)
        assert found is not None, name
        (host_dir / name).symlink_to(found)
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    log = tmp_path / "tools.log"
    log.touch()
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{host_dir}",
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(scratch),
        "FAKE_LOG": str(log),
        "FAKE_CURL_EXIT": str(curl_exit),
        "FAKE_SHA_EXIT": str(sha_exit),
        "FAKE_VERSION": version,
    }
    shell = shutil.which("bash") or "/bin/sh"
    result = subprocess.run(
        [shell, "-c", f"set -eu; {install}; echo after-install"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result.returncode, result.stdout, scratch, log.read_text().splitlines()


def test_grok_build_is_registered_before_cline_with_its_pin() -> None:
    """List Grok Build in the registry and the request model, pinned to the exact CLI release."""
    spec = REGISTRY[BLACKBOX_HARNESS_GROK_BUILD]
    assert (spec.binary, spec.version, spec.output_format, spec.position) == ("grok", "1.0.77", "pi", 67)
    ids = list(REGISTRY)
    assert ids.index("goose") < ids.index(BLACKBOX_HARNESS_GROK_BUILD) < ids.index("cline")
    assert BLACKBOX_HARNESS_GROK_BUILD in BLACKBOX_HARNESSES
    assert (
        pinned_harness_check(BLACKBOX_HARNESS_GROK_BUILD)
        == "grok --version 2>/dev/null | grep -Eq '(^| )1[.]0[.]77( |$)'"
    )


def test_open_launch_sends_model_calls_only_to_the_gateway() -> None:
    """Route every model role to the gateway in the config, keep keys out of files and argv, and blank direct keys."""
    launch = build_launch(_target(), _GATEWAY)

    assert launch.instructions_file == "AGENTS.md"
    assert set(launch.files) == {GROK_CONFIG_FILE}
    config = tomllib.loads(launch.files[GROK_CONFIG_FILE])
    assert config["disable_web_search"] is True
    assert config["cli"] == {"auto_update": False}
    assert config["features"] == {"telemetry": False}
    assert config["harness"] == {"disable_codebase_upload": True}
    assert config["endpoints"] == {"xai_api_base_url": _GATEWAY.url}
    assert config["models"] == dict.fromkeys(("default", "session_summary", "image_description", "web_search"), _MODEL)
    assert config["model"] == {
        _MODEL: {
            "name": _MODEL,
            "model": _MODEL,
            "base_url": _GATEWAY.url,
            "env_key": "APP_API_KEY",
            "api_backend": "chat_completions",
        }
    }
    assert _GATEWAY.api_key not in "".join(launch.files.values())
    assert _GATEWAY.url not in launch.run_command
    assert _GATEWAY.api_key not in launch.run_command
    assert (
        'grok --no-auto-update --single="$(cat "$APP_PROMPT_FILE")" --always-approve '
        f'--output-format streaming-json --session-id {_SESSION_ID} --model "$APP_MODEL" < /dev/null'
    ) in launch.run_command
    assert all(launch.env[name] == "" for name in _DIRECT_KEYS)
    assert launch.env["GROK_DISABLE_AUTOUPDATER"] == "1"
    assert launch.parse_output is parse_pi_output


def test_launch_env_carries_the_app_and_skynet_names() -> None:
    """Give the gateway key, model and prompt file under both the ``APP_*`` and the legacy ``SKYNET_*`` names."""
    env = build_launch(_target(), _GATEWAY).env

    for name, value in (("API_KEY", _GATEWAY.api_key), ("MODEL", _MODEL), ("GATEWAY_URL", _GATEWAY.url)):
        assert env[f"APP_{name}"] == env[f"SKYNET_{name}"] == value


def test_open_install_downloads_the_pinned_binary_without_a_remote_script() -> None:
    """Fetch the pinned release binary and never pipe xAI's install script into a shell."""
    install = build_launch(_target(), _GATEWAY).install_command

    assert install is not None
    assert install.startswith(f'export PATH="$HOME/.grok/bin":"$PATH"; {pinned_harness_check("grok_build")} || {{ ')
    assert f'"https://x.ai/cli/grok-{GROK_VERSION}-linux-$a"' in install
    assert f'"https://x.ai/cli/grok-{GROK_VERSION}-linux-$a.zst"' in install
    assert install.index("sha256sum -c -") < install.index('"$d/grok" --version')
    for arch, digest in GROK_SHA256.items():
        assert f"a={arch}; s={digest};;" in install
    assert "install.sh" not in install
    assert "| bash" not in install
    assert "| sh " not in install
    assert not install.endswith("| sh")
    assert "sudo" not in install


def test_pinned_digests_are_the_release_binaries() -> None:
    """Pin the sha256 of the 1.0.77 Linux binaries, taken from xAI's downloads on 2026-11-10."""
    assert GROK_SHA256 == {
        "x86_64": "b702950aa5827d855e493278ab6215a4e0f5bceb5bba6c6243a9f55cdd143b0a",
        "aarch64": "b30eee14a656124ef3c9da8577b5e783ef2cffabeb2b679c5c49633e77eed574",
    }


@pytest.mark.parametrize(
    ("zstd", "expected"),
    [
        (False, [f"curl https://x.ai/cli/grok-{GROK_VERSION}-linux-x86_64"]),
        (True, [f"curl https://x.ai/cli/grok-{GROK_VERSION}-linux-x86_64.zst", "zstd"]),
    ],
)
def test_install_checks_the_digest_puts_grok_on_path_and_removes_the_download_folder(
    tmp_path: Path, zstd: bool, expected: list[str]
) -> None:
    """Fetch the ``.zst`` file when zstd exists, else the raw binary, check its digest and install it."""
    code, stdout, scratch, calls = _run_install(tmp_path, zstd=zstd)

    assert code == 0
    assert stdout.strip() == "after-install"
    assert (tmp_path / "home" / ".grok" / "bin" / "grok").is_file()
    assert [line.split(" ", 2)[0] if line.startswith("zstd") else line for line in calls[:-1]] == expected
    assert calls[-1].startswith(f"sha256sum {GROK_SHA256['x86_64']}  ")
    assert calls[-1].endswith("/grok")
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize(
    ("curl_exit", "sha_exit", "version", "code"),
    [(22, 0, GROK_VERSION, 22), (0, 1, GROK_VERSION, 1), (0, 0, "1.0.78", 1)],
)
def test_failed_download_digest_or_version_removes_the_download_folder_and_fails(
    tmp_path: Path, curl_exit: int, sha_exit: int, version: str, code: int
) -> None:
    """Remove the download folder when the download, the digest or the version check fails, and exit non-zero."""
    exit_code, stdout, scratch, _ = _run_install(tmp_path, curl_exit=curl_exit, sha_exit=sha_exit, version=version)

    assert exit_code == code
    assert "after-install" not in stdout
    assert not (tmp_path / "home" / ".grok" / "bin" / "grok").exists()
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize("effort", GROK_EFFORTS)
def test_every_grok_effort_passes_unchanged(effort: str) -> None:
    """Pass each level Grok Build knows as ``--reasoning-effort`` with the same spelling, and declare it on the model.

    Grok 1.0.77 drops the flag with only a warning when the custom model's menu
    lacks the level, so the config must list it.
    """
    launch = build_launch(_target(), _GATEWAY, reasoning_effort=effort)

    assert f'--model "$APP_MODEL" --reasoning-effort {effort} < /dev/null' in launch.run_command
    config = tomllib.loads(launch.files[GROK_CONFIG_FILE])
    assert config["model"][_MODEL]["reasoning_efforts"] == [{"value": effort}]


def test_no_effort_sends_no_flag() -> None:
    """Leave the effort to Grok's default when the user sets none."""
    launch = build_launch(_target(), _GATEWAY)

    assert "--reasoning-effort" not in launch.run_command
    assert "reasoning_efforts" not in launch.files[GROK_CONFIG_FILE]


@pytest.mark.parametrize("effort", ["turbo", "auto", "HIGH", ""])
def test_an_unsupported_effort_stops_the_launch(effort: str) -> None:
    """Refuse a level Grok Build does not know, rather than drop or remap it."""
    with pytest.raises(ServiceError, match="reasoning effort"):
        build_launch(_target(), _GATEWAY, reasoning_effort=effort)


def test_submit_accepts_only_the_declared_grok_efforts() -> None:
    """Declare Grok's levels on the spec, so submit refuses any other level before paid work."""
    proposer = BlackboxProposer(harness=BLACKBOX_HARNESS_GROK_BUILD)

    assert REGISTRY[BLACKBOX_HARNESS_GROK_BUILD].supported_efforts == GROK_EFFORTS
    for effort in (*GROK_EFFORTS, None):
        _check_proposer_effort(proposer, effort)
    with pytest.raises(ServiceError, match="grok_build proposer does not support the 'turbo' effort level"):
        _check_proposer_effort(proposer, "turbo")


def test_protected_launch_checks_the_pin_offline_and_blocks_other_providers() -> None:
    """Use the offline version probe, blank every direct provider key, keep telemetry off and keep the gateway key."""
    launch = build_launch(_target(), _GATEWAY, protected=True)

    assert launch.install_command == pinned_harness_check(BLACKBOX_HARNESS_GROK_BUILD)
    assert all(launch.env[name] == "" for name in _DIRECT_KEYS)
    assert launch.env["GROK_DISABLE_AUTOUPDATER"] == "1"
    assert launch.env["DO_NOT_TRACK"] == "1"
    assert launch.env["APP_API_KEY"] == _GATEWAY.api_key


@pytest.mark.parametrize("url", ["", "gw.example/v1", 'https://gw.example/"v1', "https://user:pw@gw.example/v1"])
def test_launch_refuses_a_gateway_url_that_cannot_sit_in_the_config(url: str) -> None:
    """Stop the launch for a missing, relative, quoted or credentialed gateway URL."""
    with pytest.raises(ServiceError):
        build_launch(_target(), GatewayConfig(url=url, api_key="gateway-token"))


def test_protected_run_points_grok_at_the_relay_and_returns_its_answer(tmp_path: Path) -> None:
    """Run the protected command against a fake Grok: relay URL, blank keys, literal prompt, answer and usage."""
    result, calls, config, workspace = _run_with_fake_grok(tmp_path, protected=True)

    assert result.returncode == 0, result.stderr
    run = next(line for line in calls.splitlines() if line.startswith("ARGS"))
    assert run == (
        f"ARGS [--no-auto-update] [--single={_PROMPT}] [--always-approve] "
        f"[--output-format] [streaming-json] [--session-id] [{_SESSION_ID}] [--model] [{_MODEL}]"
    )
    assert config["model"][_MODEL]["base_url"] == _RELAY
    assert config["endpoints"]["xai_api_base_url"] == _RELAY
    assert _GATEWAY.url not in calls
    assert "APP_API_KEY=gateway-token" in calls.splitlines()
    for name in _DIRECT_KEYS:
        assert f"{name}=" in calls.splitlines()
    grok_home = Path(next(line for line in calls.splitlines() if line.startswith("HOME=")).split("=", 1)[1])
    assert not grok_home.is_relative_to(workspace.resolve())
    assert not grok_home.is_relative_to(workspace)
    assert f"GROK_LOG_FILE={grok_home}/grok.log" in calls.splitlines()
    assert not grok_home.exists()
    assert parse_output("pi", result.stdout) == (
        "I fixed the off-by-one error.\n\n42.",
        {"input_tokens": 125, "output_tokens": 40},
    )


def test_a_prompt_that_starts_with_a_dash_stays_the_prompt_value(tmp_path: Path) -> None:
    """Keep a prompt such as ``- Fix X`` the value of ``--single``, so Grok does not read it as a flag."""
    result, calls, _, _ = _run_with_fake_grok(tmp_path, protected=False, prompt="- Fix X")

    assert result.returncode == 0, result.stderr
    run = next(line for line in calls.splitlines() if line.startswith("ARGS"))
    assert run.startswith("ARGS [--no-auto-update] [--single=- Fix X] [--always-approve] ")


def test_open_run_reads_the_gateway_url_from_the_generated_config(tmp_path: Path) -> None:
    """Without a relay, Grok gets the gateway URL the launch wrote."""
    _, _, config, _ = _run_with_fake_grok(tmp_path, protected=False)

    assert config["model"][_MODEL]["base_url"] == _GATEWAY.url
    assert config["endpoints"]["xai_api_base_url"] == _GATEWAY.url


def test_without_an_end_event_the_run_fails_and_the_per_call_usage_adds_up(tmp_path: Path) -> None:
    """Fail a run with no ``end`` event, even when Grok exits 0, drop its text, and sum the ``usage`` events."""
    stream = "\n".join(_RECORDED_STREAM.splitlines()[:3])
    result, _, _, _ = _run_with_fake_grok(tmp_path, protected=False, stream=stream)

    assert result.returncode == 1
    assert parse_output("pi", result.stdout) == (None, {"input_tokens": 100, "output_tokens": 40})


@pytest.mark.parametrize("stop_reason", ["end_turn", "cancelled"])
def test_a_failed_grok_run_keeps_its_exit_status_and_usage(tmp_path: Path, stop_reason: str) -> None:
    """Return Grok's own exit status with no answer, still report the tokens it used, and remove its home."""
    events = [json.loads(line) for line in _RECORDED_STREAM.splitlines()]
    events[-1]["stopReason"] = stop_reason
    stream = "\n".join(json.dumps(event) for event in events)
    result, calls, _, _ = _run_with_fake_grok(tmp_path, protected=False, stream=stream, fake_exit=3)

    assert result.returncode == 3
    assert parse_output("pi", result.stdout) == (None, {"input_tokens": 125, "output_tokens": 40})
    grok_home = Path(next(line for line in calls.splitlines() if line.startswith("HOME=")).split("=", 1)[1])
    assert not grok_home.exists()


@pytest.mark.parametrize("stop_reason", ["cancelled", "max_tokens", "refusal", None])
def test_a_run_that_does_not_end_its_turn_fails_without_an_answer(tmp_path: Path, stop_reason: str | None) -> None:
    """Fail a run whose ``end`` event has no ``end_turn`` stop, even when Grok exits 0, and drop earlier text."""
    events = [json.loads(line) for line in _RECORDED_STREAM.splitlines()]
    if stop_reason is None:
        del events[-1]["stopReason"]
    else:
        events[-1]["stopReason"] = stop_reason
    stream = "\n".join(json.dumps(event) for event in events)
    result, _, _, _ = _run_with_fake_grok(tmp_path, protected=False, stream=stream)

    assert result.returncode == 1
    assert parse_output("pi", result.stdout) == (None, {"input_tokens": 125, "output_tokens": 40})


def test_the_payload_names_the_pi_parser() -> None:
    """Serialize the launch with the bridge's Pi parser, which reads the converter's event."""
    assert launch_payload(build_launch(_target(), _GATEWAY))["output_format"] == "pi"

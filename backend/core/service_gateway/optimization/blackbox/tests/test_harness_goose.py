"""Tests for the Goose CLI harness: registry entry, pinned install, gateway-only launch, protected env and output parsing."""

from __future__ import annotations

import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from core.exceptions import ServiceError
from core.models.blackbox import (
    BLACKBOX_HARNESS_GOOSE,
    BLACKBOX_HARNESSES,
    BLACKBOX_TARGET_AGENT,
    BlackboxProposer,
    BlackboxTarget,
)

from ..harness import GatewayConfig, build_launch, launch_payload, pinned_harness_check
from ..harness_bridge import parse_output, parse_plain_output
from ..harnesses import REGISTRY
from ..harnesses.goose import _PROTECTED_ENV, GOOSE_BASE_URL_FILE, GOOSE_SHA256, GOOSE_VERSION
from ..service import _check_proposer_effort

_GATEWAY = GatewayConfig(url="https://gw.example/v1", api_key="gateway-token")
_RELAY = "http://127.0.0.1:3210/v1"
_PROVIDER_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY", "XAI_API_KEY")
# Synthetic one-shot transcript in the shape of ``goose run --quiet`` output.
_RECORDED_STDOUT = "\nI read solution.py and fixed the off-by-one error.\n\nThe answer is 42.\n\n"
# Stand-in for the goose binary: it records each call and prints a fixed answer.
_FAKE_GOOSE = """#!/bin/sh
log="$FAKE_LOG"
{
  printf 'ARGS'; for a in "$@"; do printf ' [%s]' "$a"; done; printf '\\n'
  printf 'OPENAI_BASE_URL=%s\\n' "$OPENAI_BASE_URL"
  printf 'OPENAI_API_KEY=%s\\n' "$OPENAI_API_KEY"
  printf 'OPENAI_HOST=%s\\n' "${OPENAI_HOST-unset}"
  printf 'OPENAI_BASE_PATH=%s\\n' "${OPENAI_BASE_PATH-unset}"
  printf 'ANTHROPIC_API_KEY=%s\\n' "$ANTHROPIC_API_KEY"
  printf 'GOOSE_PATH_ROOT=%s\\n' "$GOOSE_PATH_ROOT"
  if [ -d "$GOOSE_PATH_ROOT" ]; then echo 'ROOT_DIR=yes'; else echo 'ROOT_DIR=no'; fi
} >> "$log"
[ -z "$FAKE_SLEEP" ] || sleep "$FAKE_SLEEP"
printf '%s' "$FAKE_ANSWER"
exit "${FAKE_EXIT:-0}"
"""
# Stand-ins for the install step's tools. ``curl`` logs the file it writes,
# so a test can check that the download folder is gone afterwards. macOS
# ``mktemp -d`` ignores ``TMPDIR``, so a fake one keeps the folder in scratch.
_FAKE_INSTALL_TOOLS = {
    "uname": "#!/bin/sh\necho x86_64\n",
    "mktemp": '#!/bin/sh\nd="$TMPDIR/download.$$"\nmkdir "$d" && echo "$d"\n',
    "curl": (
        '#!/bin/sh\nprintf \'%s\\n\' "$3" >> "$FAKE_LOG"\n'
        '[ "${FAKE_CURL_EXIT:-0}" -eq 0 ] || exit "$FAKE_CURL_EXIT"\nprintf archive > "$3"\n'
    ),
    "sha256sum": '#!/bin/sh\ncat > /dev/null\nexit "${FAKE_SHA_EXIT:-0}"\n',
    "tar": "#!/bin/sh\nprintf '#!/bin/sh\\necho goose 1.57.0\\n' > \"$4/goose\"\n",
}
# The run command calls ``python3``; this puts the test's interpreter first, as
# a CI image may keep its python3 outside the default path.
_PATH = f"{Path(sys.executable).parent}{os.pathsep}{os.defpath}"


def _target() -> BlackboxTarget:
    """Build a goose agent target.

    Returns:
        The target.
    """
    return BlackboxTarget(kind=BLACKBOX_TARGET_AGENT, harness=BLACKBOX_HARNESS_GOOSE, model="target-model")


def _run_with_fake_goose(
    tmp_path: Path, *, protected: bool, gateway: GatewayConfig = _GATEWAY, fake_exit: int = 0
) -> tuple[str, str, Path, int]:
    """Run the built goose command in a scratch workspace against a fake ``goose`` binary.

    The env also carries an inherited ``OPENAI_HOST`` and ``OPENAI_BASE_PATH``,
    which the run must replace so they cannot override the gateway URL.

    Args:
        tmp_path: Scratch directory for the workspace, the fake binary and its log.
        protected: Build the protected launch, with the sandbox-local relay set.
        gateway: Gateway URL and key for the launch.
        fake_exit: Exit status the fake binary returns.

    Returns:
        The command's stdout, the fake binary's call log, the workspace, and
        the command's exit status.
    """
    launch = build_launch(_target(), gateway, protected=protected)
    workspace = tmp_path / "work"
    for name, content in launch.files.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (workspace / "task").mkdir(parents=True)
    (workspace / "task" / "PROMPT.md").write_text("Fix `solution.py` and say $(echo done).")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "goose"
    fake.write_text(_FAKE_GOOSE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    env = {
        **launch.env,
        "PATH": f"{bin_dir}{os.pathsep}{_PATH}",
        "HOME": str(tmp_path / "home"),
        "OPENAI_HOST": "https://api.openai.com",
        "OPENAI_BASE_PATH": "v1/other",
        "OPENAI_BASE_URL": "https://api.openai.com/v1",
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


def _run_install(tmp_path: Path, *, curl_exit: int = 0, sha_exit: int = 0) -> tuple[int, str, Path, Path]:
    """Run the open install command under ``set -eu`` with fake download tools.

    The command runs inside a ``set -eu`` script, as in the native bootstrap,
    and ``TMPDIR`` points at a scratch folder, so the test can see every
    folder ``mktemp -d`` leaves behind.

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


def test_goose_is_registered_before_cline_with_its_pin() -> None:
    """List goose in the registry and the request model, pinned to the exact GitHub release."""
    spec = REGISTRY[BLACKBOX_HARNESS_GOOSE]
    assert (spec.binary, spec.version, spec.output_format, spec.position) == ("goose", "1.57.0", "plain", 64)
    assert list(REGISTRY).index(BLACKBOX_HARNESS_GOOSE) < list(REGISTRY).index("cline")
    assert BLACKBOX_HARNESS_GOOSE in BLACKBOX_HARNESSES
    assert (
        pinned_harness_check(BLACKBOX_HARNESS_GOOSE) == "goose --version 2>/dev/null | grep -Eq '(^| )1[.]57[.]0( |$)'"
    )


def test_open_install_downloads_the_pinned_release_and_checks_its_digest() -> None:
    """Fetch one pinned tarball, verify its sha256 before unpacking, and never pipe a script into a shell."""
    install = build_launch(_target(), _GATEWAY).install_command

    assert install is not None
    assert install.startswith(f'export PATH="$HOME/.local/bin":"$PATH"; {pinned_harness_check("goose")} || {{ ')
    assert f"https://github.com/aaif-goose/goose/releases/download/v{GOOSE_VERSION}/$f" in install
    assert 'f="goose-$a-unknown-linux-musl.tar.gz"' in install
    for arch, digest in GOOSE_SHA256.items():
        assert f"a={arch}; s={digest};;" in install
        assert len(digest) == 64
    assert install.index("sha256sum -c -") < install.index("tar -xzf")
    assert "| bash" not in install
    assert "| sh " not in install
    assert "download_cli.sh" not in install
    assert "sudo" not in install


def test_install_removes_the_download_folder_after_a_good_install(tmp_path: Path) -> None:
    """Install the binary, remove the download folder, and let the caller's script continue."""
    code, stdout, scratch, download = _run_install(tmp_path)

    assert code == 0
    assert stdout.strip() == "after-install"
    assert (tmp_path / "home" / ".local" / "bin" / "goose").is_file()
    assert download.parent.parent == scratch
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
    assert not (tmp_path / "home" / ".local" / "bin" / "goose").exists()
    assert not download.parent.exists()
    assert list(scratch.iterdir()) == []


def test_open_launch_sends_model_calls_only_to_the_gateway() -> None:
    """Read the gateway URL from a file, pass the gateway token, and keep provider keys out of the env."""
    launch = build_launch(_target(), _GATEWAY)

    assert launch.instructions_file == "AGENTS.md"
    assert launch.files == {GOOSE_BASE_URL_FILE: _GATEWAY.url}
    assert _GATEWAY.url not in launch.run_command
    assert _GATEWAY.api_key not in launch.run_command
    assert (
        f'OPENAI_HOST="$(cat {GOOSE_BASE_URL_FILE})" OPENAI_BASE_PATH=chat/completions OPENAI_API_KEY="$APP_API_KEY" '
    ) in launch.run_command
    assert (
        "goose run --no-session --quiet --no-profile --with-builtin developer "
        '--provider openai --model "$APP_MODEL" -i "$APP_PROMPT_FILE" < /dev/null'
    ) in launch.run_command
    assert launch.env["APP_API_KEY"] == _GATEWAY.api_key
    assert launch.env["APP_GATEWAY_URL"] == _GATEWAY.url
    assert launch.env["GOOSE_DISABLE_KEYRING"] == "1"
    assert launch.env["GOOSE_TELEMETRY_OFF"] == "1"
    assert launch.env["GOOSE_MODE"] == "auto"
    assert "GOOSE_THINKING_EFFORT" not in launch.env
    assert not set(_PROVIDER_KEYS) & set(launch.env)
    assert launch.parse_output is parse_plain_output


@pytest.mark.parametrize("effort", ["none", "low", "medium", "high", "max"])
def test_supported_reasoning_effort_passes_unchanged(effort: str) -> None:
    """Pass each effort goose supports as ``GOOSE_THINKING_EFFORT`` with no change."""
    launch = build_launch(_target(), _GATEWAY, reasoning_effort=effort)
    assert launch.env["GOOSE_THINKING_EFFORT"] == effort


def test_goose_declares_its_effort_levels_for_the_submit_check() -> None:
    """Declare goose's levels on its registry entry, so submit refuses the rest before paid work."""
    assert REGISTRY[BLACKBOX_HARNESS_GOOSE].supported_efforts == ("none", "low", "medium", "high", "max")


@pytest.mark.parametrize(("effort", "accepted"), [("high", True), (None, True), ("xhigh", False), ("minimal", False)])
def test_submit_check_uses_the_goose_levels(effort: str | None, accepted: bool) -> None:
    """Refuse at submit an effort goose has no level for, and accept a supported one or none."""
    proposer = BlackboxProposer(harness=BLACKBOX_HARNESS_GOOSE)
    if accepted:
        _check_proposer_effort(proposer, effort)
    else:
        with pytest.raises(ServiceError, match=f"goose proposer does not support the '{effort}' effort level"):
            _check_proposer_effort(proposer, effort)


@pytest.mark.parametrize("effort", [None, ""])
def test_no_reasoning_effort_keeps_the_goose_default(effort: str | None) -> None:
    """Give goose no thinking setting when the user picks no effort."""
    launch = build_launch(_target(), _GATEWAY, reasoning_effort=effort)
    assert "GOOSE_THINKING_EFFORT" not in launch.env


@pytest.mark.parametrize("effort", ["minimal", "xhigh", "turbo"])
def test_unsupported_reasoning_effort_raises(effort: str) -> None:
    """Refuse an effort goose has no level for, instead of a remap or a silent drop."""
    with pytest.raises(ServiceError, match="Goose does not support effort"):
        build_launch(_target(), _GATEWAY, reasoning_effort=effort)


def test_protected_launch_checks_the_pin_offline_and_blocks_other_providers_and_telemetry() -> None:
    """Use the offline version probe, blank other provider keys, force telemetry and the toolshim off, and keep the gateway key."""
    launch = build_launch(_target(), _GATEWAY, protected=True)

    assert launch.install_command == pinned_harness_check(BLACKBOX_HARNESS_GOOSE)
    blanked = [name for name, value in _PROTECTED_ENV.items() if value == ""]
    assert {"MISTRAL_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "AZURE_OPENAI_API_KEY"} <= set(blanked)
    for name in blanked:
        assert launch.env[name] == ""
    assert launch.env["GOOSE_TELEMETRY_OFF"] == "1"
    assert launch.env["GOOSE_TELEMETRY_ENABLED"] == "false"
    assert launch.env["DO_NOT_TRACK"] == "1"
    assert launch.env["GOOSE_TOOLSHIM"] == "false"
    assert launch.env["APP_API_KEY"] == _GATEWAY.api_key
    assert "OPENAI_API_KEY" not in launch.env


def test_protected_run_points_goose_at_the_relay_and_returns_its_answer(tmp_path: Path) -> None:
    """Run the protected command against a fake goose: the relay URL, gateway token and answer come through."""
    stdout, calls, workspace, code = _run_with_fake_goose(tmp_path, protected=True)

    assert code == 0

    (run,) = [line for line in calls.splitlines() if line.startswith("ARGS")]
    assert run == (
        "ARGS [run] [--no-session] [--quiet] [--no-profile] [--with-builtin] [developer] "
        "[--provider] [openai] [--model] [target-model] [-i] [task/PROMPT.md]"
    )
    assert _logged(calls, "OPENAI_HOST") == _RELAY
    assert _logged(calls, "OPENAI_BASE_PATH") == "chat/completions"
    assert _GATEWAY.url not in calls
    assert _logged(calls, "OPENAI_API_KEY") == "gateway-token"
    assert _logged(calls, "ANTHROPIC_API_KEY") == ""
    root = Path(_logged(calls, "GOOSE_PATH_ROOT"))
    assert _logged(calls, "ROOT_DIR") == "yes"
    assert not root.exists()
    assert not root.is_relative_to(workspace.resolve())
    assert not root.is_relative_to(workspace)
    assert parse_output("plain", stdout) == (
        "I read solution.py and fixed the off-by-one error.\n\nThe answer is 42.",
        {},
    )


def test_open_run_reads_the_gateway_url_from_the_generated_file(tmp_path: Path) -> None:
    """Without a relay, point goose at the gateway URL the launch wrote."""
    _, calls, _, _ = _run_with_fake_goose(tmp_path, protected=False)

    assert _logged(calls, "OPENAI_HOST") == _GATEWAY.url


def test_run_forces_chat_completions_on_the_full_gateway_url(tmp_path: Path) -> None:
    """Pass the whole gateway URL as ``OPENAI_HOST`` with the custom ``chat/completions`` base path.

    Goose 1.57.0 sends gpt-5*, gpt-6* and o* models to /responses when the base
    path is its default ``v1/chat/completions``
    (``OpenAiProvider::should_use_responses_api``), and Skynet's local CLI and
    BYOK routes reject /responses. Only a custom chat path forces chat
    completions. ``ApiClient::build_url`` appends that relative path to
    ``OPENAI_HOST``, so the calls go to ``<gateway>/chat/completions`` with the
    gateway's path prefix kept. An inherited ``OPENAI_HOST``, ``OPENAI_BASE_PATH``
    or ``OPENAI_BASE_URL`` must not change either value.
    """
    gateway = GatewayConfig(url="https://gw.example/relay/abc/v1", api_key="gateway-token")
    _, calls, _, _ = _run_with_fake_goose(tmp_path, protected=False, gateway=gateway)

    assert _logged(calls, "OPENAI_HOST") == gateway.url
    assert _logged(calls, "OPENAI_BASE_PATH") == "chat/completions"


@pytest.mark.parametrize("fake_exit", [0, 3])
def test_run_removes_the_goose_root_and_keeps_goose_exit_status_and_output(tmp_path: Path, fake_exit: int) -> None:
    """Remove the per-run goose root when the shell exits, after a good or failed run, without changing the result."""
    stdout, calls, _, code = _run_with_fake_goose(tmp_path, protected=False, fake_exit=fake_exit)

    assert code == fake_exit
    assert stdout == _RECORDED_STDOUT
    assert _logged(calls, "ROOT_DIR") == "yes"
    assert not Path(_logged(calls, "GOOSE_PATH_ROOT")).exists()


def test_recorded_output_parses_to_the_answer_and_the_payload_names_the_plain_parser() -> None:
    """Parse a recorded transcript to its stripped text, and serialize the launch with the bridge's plain parser."""
    assert parse_plain_output(_RECORDED_STDOUT) == (
        "I read solution.py and fixed the off-by-one error.\n\nThe answer is 42.",
        {},
    )
    assert parse_plain_output("  \n") == (None, {})
    assert launch_payload(build_launch(_target(), _GATEWAY))["output_format"] == "plain"


def _spawn_goose(tmp_path: Path, tag: str, *, sleep: int = 0, path: str = _PATH) -> tuple[subprocess.Popen[str], Path]:
    """Start the built goose command against a fake goose in its own process group.

    Every run shares one fake binary and one temp directory, as runs in one box do.

    Args:
        tmp_path: Scratch directory for the workspaces, the fake binary and the temp directory.
        tag: Name of this run's workspace and call log.
        sleep: Seconds the fake goose waits before it answers.
        path: ``PATH`` after the fake binary's folder.

    Returns:
        The running shell and its call log.
    """
    launch = build_launch(_target(), _GATEWAY)
    workspace = tmp_path / f"work-{tag}"
    for name, content in launch.files.items():
        (workspace / name).parent.mkdir(parents=True, exist_ok=True)
        (workspace / name).write_text(content)
    (workspace / "task").mkdir()
    (workspace / "task" / "PROMPT.md").write_text("Fix it.")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "goose"
    fake.write_text(_FAKE_GOOSE)
    fake.chmod(0o755)
    (tmp_path / "tmp").mkdir(exist_ok=True)
    log = tmp_path / f"calls-{tag}.log"
    env = {
        **launch.env,
        "PATH": f"{bin_dir}{os.pathsep}{path}",
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(tmp_path / "tmp"),
        "FAKE_LOG": str(log),
        "FAKE_ANSWER": "done",
        "FAKE_SLEEP": str(sleep or ""),
    }
    process = subprocess.Popen(
        [shutil.which("bash") or "/bin/sh", "-c", launch.run_command],
        cwd=workspace,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    return process, log


def _goose_root(log: Path) -> Path:
    """Wait until the fake goose logs its root, and return that root.

    Args:
        log: The run's call log.

    Returns:
        The run's goose root.
    """
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if log.exists() and "ROOT_DIR=" in log.read_text():
            return Path(_logged(log.read_text(), "GOOSE_PATH_ROOT"))
        time.sleep(0.05)
    raise AssertionError("the fake goose never started")


def test_next_run_removes_the_root_of_a_run_killed_at_its_timeout(tmp_path: Path) -> None:
    """Remove the goose root that a SIGKILL left behind when the next run starts (SKY-594).

    The bridge kills a timed-out run's process group with SIGKILL, so the EXIT
    trap does not run and the root stays until the next run sweeps it.
    """
    killed, killed_log = _spawn_goose(tmp_path, "killed", sleep=30)
    killed_root = _goose_root(killed_log)
    os.killpg(killed.pid, signal.SIGKILL)
    killed.wait(timeout=10)
    assert killed_root.is_dir()

    after, after_log = _spawn_goose(tmp_path, "after")
    assert after.wait(timeout=10) == 0

    assert not killed_root.exists()
    assert not _goose_root(after_log).exists()


def test_a_run_keeps_the_root_of_a_concurrent_live_run(tmp_path: Path) -> None:
    """Keep the root of a run that still holds its lock while another run sweeps."""
    live, live_log = _spawn_goose(tmp_path, "live", sleep=30)
    try:
        live_root = _goose_root(live_log)
        deadline = time.monotonic() + 10
        while not (live_root / "owner.lock").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        other, _ = _spawn_goose(tmp_path, "other")
        assert other.wait(timeout=10) == 0
        assert (live_root / "owner.lock").is_file()
    finally:
        os.killpg(live.pid, signal.SIGKILL)
        live.wait(timeout=10)


def test_sweep_removes_an_old_lockless_root_and_keeps_a_new_one(tmp_path: Path) -> None:
    """Remove a lockless root older than the stale limit, but keep a fresh one that may still get its lock."""
    temp = tmp_path / "tmp"
    old = temp / "app-goose-old"
    new = temp / "app-goose-new"
    other = temp / "app-other-old"
    for folder in (old, new, other):
        folder.mkdir(parents=True)
    hour_ago = time.time() - 3600
    for folder in (old, other):
        os.utime(folder, (hour_ago, hour_ago))

    run, _ = _spawn_goose(tmp_path, "sweep")
    assert run.wait(timeout=10) == 0

    assert not old.exists()
    assert new.is_dir()
    assert other.is_dir()


def test_a_box_without_python3_still_runs_and_removes_its_root(tmp_path: Path) -> None:
    """Fall back to a plain ``mktemp`` root without the sweep when the box has no python3 (SKY-594)."""
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("cat", "mktemp", "rm", "sleep"):
        found = shutil.which(name)
        assert found is not None
        (tools / name).symlink_to(found)
    old = tmp_path / "tmp" / "app-goose-old"
    old.mkdir(parents=True)
    hour_ago = time.time() - 3600
    os.utime(old, (hour_ago, hour_ago))

    run, log = _spawn_goose(tmp_path, "nopython", path=str(tools))
    assert run.wait(timeout=10) == 0

    assert "ROOT_DIR=yes" in log.read_text()
    root = _goose_root(log)
    assert root.parent == tmp_path / "tmp"
    assert root.name.startswith("app-goose-")
    assert not root.exists()
    assert old.is_dir()

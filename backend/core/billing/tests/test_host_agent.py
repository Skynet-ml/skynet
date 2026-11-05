"""Prove host-CLI proposer turns run on a copy, with stripped keys, under the run's budget."""

from __future__ import annotations

import base64
import io
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session

from core.billing import host_agent, run_usage
from core.billing.budgets import BudgetService
from core.billing.codex_auth_proxy import PLACEHOLDER_ACCOUNT
from core.billing.host_agent import (
    HOST_AGENT_PATH,
    HOST_AGENT_TOKEN_KEY,
    RATE_LIMITED_CODE,
    attach_host_agent_token,
    bind_host_agent,
    failure_message,
    host_agent_failure,
    host_cli_argv,
    host_proposer,
    host_reasoning_effort,
    run_host_agent,
)
from core.billing.model_gateway import ModelGateway
from core.billing.operation_pricing import UnpricedOperationError
from core.billing.runtime import BudgetRuntime
from core.config import Settings, settings
from core.local import cli_probe
from core.local.cli_isolation import CLAUDE_ARGS, CODEX_ARGS
from core.local.cli_probe import read_cli_limits
from core.storage.models import Base, BillingCustomerModel, ExecutionOperationModel
from core.worker import host_agent_shim

STRIPPED = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY")
RESET_EPOCH = 1_893_456_000

# One fake for both CLIs: it records how it was started, edits the copy,
# and prints that CLI's JSON shape (shapes taken from real codex/claude runs).
FAKE_CLI = r"""
import json, os, sys
from pathlib import Path

cli = os.environ["FAKE_CLI_NAME"]
argv = sys.argv[1:]
prompt = sys.stdin.read()
Path(os.environ["FAKE_CLI_RECORD"]).write_text(json.dumps({
    "argv": argv,
    "cwd": os.getcwd(),
    "env": {key: os.environ.get(key) for key in __STRIPPED__},
    "home": os.environ.get("HOME"),
    "turn_env": {key: os.environ.get(key) for key in ("TMPDIR", "TMP", "TEMP", "CLAUDE_CODE_TMPDIR", "SSH_AUTH_SOCK")},
    "prompt": prompt,
    "codex_home": os.environ.get("CODEX_HOME"),
    "home_files": sorted(os.listdir(os.environ["HOME"])),
    "home_library": os.path.realpath(Path(os.environ["HOME"]) / "Library"),
    "login": (Path(os.environ["CODEX_HOME"]) / "auth.json").read_text() if cli == "codex" else None,
    "files": sorted(path.as_posix() for path in Path(".").rglob("*")),
}))
if os.environ.get("FAKE_CLI_MODE") == "leak":
    # A prompt-injected turn prints the copied token, plain and encoded, in its answer and errors.
    import base64
    token = json.loads((Path(os.environ["CODEX_HOME"]) / "auth.json").read_text())["tokens"]["access_token"]
    leaked = f"{token} {base64.b64encode(token.encode()).decode()} {token.encode().hex()}"
    print(f"stderr {leaked}", file=sys.stderr)
    print(json.dumps({"type": "turn.failed", "error": {"message": f"boom {leaked}"}}))
    sys.exit(1)
if os.environ.get("FAKE_CLI_MODE") == "leak-wrapped":
    # base64 wrapped at 76 characters, as base64.encodebytes, openssl base64 and MIME print it.
    import base64
    login = (Path(os.environ["CODEX_HOME"]) / "auth.json").read_bytes()
    leaked = base64.encodebytes(login).decode()
    print(f"stderr {leaked}", file=sys.stderr)
    print(json.dumps({"type": "turn.failed", "error": {"message": f"boom {leaked}"}}))
    sys.exit(1)
if os.environ.get("FAKE_CLI_MODE") == "steal":
    # A prompt-injected tool command collects every place a token could be: the login copy, every
    # file in the codex home, the whole environment and the command line.
    codex_home = Path(os.environ["CODEX_HOME"])
    Path(os.environ["FAKE_CLI_RECORD"]).write_text(json.dumps({
        "files": {str(path): path.read_text(errors="replace") for path in codex_home.rglob("*") if path.is_file()},
        "env": dict(os.environ),
        "argv": sys.argv,
    }))
if os.environ.get("FAKE_CLI_MODE") == "expired":
    print(json.dumps({"type": "turn.failed", "error": {"message": "unexpected status 401 Unauthorized"}}))
    sys.exit(1)
if os.environ.get("FAKE_CLI_MODE") == "limit":
    if cli == "claude":
        print(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                          "result": "Claude AI usage limit reached|__EPOCH__"}))
    else:
        print("You've hit your usage limit. Try again later.", file=sys.stderr)
    sys.exit(1)
if cli == "codex":
    # A tool command can rewrite the login copy; this must never reach the user's login.
    (Path(os.environ["CODEX_HOME"]) / "auth.json").write_text('{"tokens": {"refresh_token": "planted"}}')
target = Path("solver.py")
target.write_text(target.read_text() + "# improved\n")
Path("stale.txt").unlink()
Path("notes").mkdir()
Path("notes/plan.md").write_text("plan\n")
if cli == "codex":
    print(json.dumps({"type": "thread.started", "thread_id": "t"}))
    print(json.dumps({"type": "item.completed", "item": {"id": "i0", "type": "error", "message": "warning"}}))
    print(json.dumps({"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "done"}}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 120, "output_tokens": 30}}))
else:
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "done",
                      "duration_api_ms": 1500, "usage": {"input_tokens": 100, "cache_read_input_tokens": 20,
                                                         "output_tokens": 30}}))
""".replace("__STRIPPED__", repr(STRIPPED)).replace("__EPOCH__", str(RESET_EPOCH))


def _fake_cli(directory: Path) -> Path:
    """Write the fake CLI as an executable script.

    Args:
        directory: Where to put it.

    Returns:
        The script path.
    """
    script = directory / "fake-cli"
    script.write_text(f"#!{sys.executable}\n{FAKE_CLI}")
    script.chmod(0o755)
    return script


def _workspace_tar(root: Path) -> str:
    """Pack a guest workspace the way the shim does.

    Args:
        root: Workspace directory.

    Returns:
        Base64 gzip tar.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(root.rglob("*")):
            archive.add(path, arcname=path.relative_to(root).as_posix(), recursive=False)
    return base64.b64encode(buffer.getvalue()).decode()


def _members(result_b64: str) -> dict[str, str]:
    """Read the regular files of a returned tree.

    Args:
        result_b64: Base64 gzip tar from the parent.

    Returns:
        Relative path to text content.
    """
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(result_b64)), mode="r:gz") as archive:
        return {
            member.name: archive.extractfile(member).read().decode()  # type: ignore[union-attr]
            for member in archive.getmembers()
            if member.isfile()
        }


@pytest.fixture
def local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Local-mode settings whose CLIs are the fake, with provider keys in the parent env."""
    script = _fake_cli(tmp_path)
    for key in STRIPPED:
        monkeypatch.setenv(key, "parent-only-value")
    monkeypatch.setenv("FAKE_CLI_RECORD", str(tmp_path / "record.json"))
    # Never the user's real codex login.
    codex_home = tmp_path / "user-codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_bytes(_login("account-1", "A" * 32))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    # The Seatbelt profile has its own macOS test; here the turn runs unwrapped so Linux CI covers it.
    monkeypatch.setattr(host_agent, "confine", lambda argv, **_: argv)
    # The CLI env is an allow-list; the fake CLI reads its test settings from FAKE_CLI_* variables.
    monkeypatch.setattr(cli_probe, "CLI_ENV_PREFIXES", (*cli_probe.CLI_ENV_PREFIXES, "FAKE_CLI_"))
    return settings.model_copy(
        update={
            "local_mode": True,
            "local_state_dir": str(tmp_path / "state"),
            "codex_bin": str(script),
            "claude_bin": str(script),
            "host_agent_timeout_seconds": 60,
        }
    )


@pytest.fixture
def guest(tmp_path: Path) -> Path:
    """A guest workspace with one file to edit and one to delete."""
    root = tmp_path / "guest"
    root.mkdir()
    (root / "solver.py").write_text("def solve():\n    return 1\n")
    (root / "stale.txt").write_text("old\n")
    return root


@pytest.mark.parametrize(
    ("cli", "permission", "expected"),
    [
        (
            "codex",
            "workspace_only",
            ["exec", "--json", "--sandbox", "danger-full-access", "--skip-git-repo-check", "-C"],
        ),
        (
            "codex",
            "full_access",
            ["exec", "--json", "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check", "-C"],
        ),
        (
            "claude",
            "workspace_only",
            [
                "-p",
                "--output-format",
                "json",
                "--permission-mode",
                "acceptEdits",
                "--tools",
                "Read,Edit,Write,Glob,Grep",
            ],
        ),
        ("claude", "full_access", ["-p", "--output-format", "json", "--permission-mode", "bypassPermissions"]),
    ],
)
def test_argv_follows_the_permission_table(cli: str, permission: str, expected: list[str], tmp_path: Path) -> None:
    """Each (cli, permission) pair builds exactly its documented command, never --add-dir or --bare."""
    workdir = tmp_path / "copy"
    argv = host_cli_argv(cli, permission, "bin", workdir, "m")  # type: ignore[arg-type]
    tail = [str(workdir)] if cli == "codex" else []
    assert argv == ["bin", *expected, *tail, "--model", "m"]
    assert "--add-dir" not in argv
    assert "--bare" not in argv
    if cli == "claude" and permission == "workspace_only":
        # Bash, Monitor, Task agents and Workflow can all run a command.
        tools = argv[argv.index("--tools") + 1].split(",")
        assert tools == ["Read", "Edit", "Write", "Glob", "Grep"]
        assert not {"Bash", "Monitor", "Task", "Workflow"} & set(tools)


def test_unknown_permission_is_refused(tmp_path: Path) -> None:
    """A permission outside the table never reaches a CLI."""
    with pytest.raises(host_agent.HostAgentRequestError):
        host_cli_argv("claude", "root", "bin", tmp_path, None)


_HIGH_EFFORT_FLAGS = [("codex", ["-c", 'model_reasoning_effort="high"']), ("claude", ["--effort", "high"])]


@pytest.mark.parametrize(("cli", "flags"), _HIGH_EFFORT_FLAGS)
def test_argv_passes_the_effort_the_way_each_cli_reads_it(cli: str, flags: list[str], tmp_path: Path) -> None:
    """Codex exec takes the effort as a config override, Claude Code as its --effort flag.

    Args:
        cli: The host CLI.
        flags: The arguments that carry the effort.
        tmp_path: Workspace parent.
    """
    plain = host_cli_argv(cli, "full_access", "bin", tmp_path, "m")  # type: ignore[arg-type]
    chosen = host_cli_argv(cli, "full_access", "bin", tmp_path, "m", effort="high")  # type: ignore[arg-type]

    assert chosen == [*plain, *flags]


@pytest.mark.parametrize(
    ("cli", "model", "effort", "expected"),
    [
        ("codex", "subscription/codex/openai/gpt-6.1-sol", "xhigh", "xhigh"),
        ("claude", "subscription/claude/anthropic/claude-opus-5.5", "max", "max"),
        # Opus 4.5 tops out at high, so Max runs there.
        ("claude", "anthropic/claude-opus-4.5", "max", "high"),
        ("claude", "anthropic/claude-opus-4.5", "xhigh", None),
        # Sonnet 4.5 takes no effort at all.
        ("claude", "anthropic/claude-sonnet-4.5", "high", None),
        ("codex", "subscription/codex/openai/gpt-6.1-sol", "minimal", None),
        # The CLI's own default model has no known levels.
        ("codex", "", "high", None),
        ("codex", "subscription/codex/openai/gpt-6.1-sol", None, None),
    ],
)
def test_effort_keeps_only_levels_the_cli_takes_for_the_model(
    cli: str, model: str, effort: str | None, expected: str | None
) -> None:
    """The run's effort reaches the CLI only as a level that CLI lists for the model.

    Args:
        cli: The host CLI.
        model: The proposer model id the guest sends.
        effort: The run's stored effort.
        expected: The level the CLI receives, or None.
    """
    assert host_agent._effort_arg(cli, model, effort) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize(("cli", "flags"), _HIGH_EFFORT_FLAGS)
def test_capability_runs_the_cli_at_the_stored_effort(
    cli: str,
    flags: list[str],
    gateway: ModelGateway,
    local: Settings,
    guest: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The effort from the run settings reaches the CLI; a guest-sent effort does not.

    Args:
        cli: The host CLI.
        flags: The arguments that carry the effort.
        gateway: The parent gateway.
        local: Local-mode settings.
        guest: Guest workspace.
        tmp_path: Where the fake CLI records its argv.
        monkeypatch: Pytest fixture for the fake CLI's name.
    """
    monkeypatch.setenv("FAKE_CLI_NAME", cli)
    model = {"codex": "subscription/codex/openai/gpt-6.1-sol", "claude": "anthropic/claude-opus-5.5"}[cli]
    body = {"model": model, "reasoning_effort": "low", "prompt": "x", "workspace_tar_b64": _workspace_tar(guest)}

    token = bind_host_agent(gateway, local, cli=cli, permission="workspace_only", reasoning_effort="high")
    assert _post(gateway, token, body).status_code == 200
    argv = json.loads((tmp_path / "record.json").read_text())["argv"]
    assert argv[-len(flags) :] == flags
    assert "low" not in argv


def test_capability_keeps_the_cli_default_effort_without_a_choice(
    gateway: ModelGateway, local: Settings, guest: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run with no stored effort starts the CLI with no effort argument."""
    monkeypatch.setenv("FAKE_CLI_NAME", "claude")
    body = {"model": "anthropic/claude-opus-5.5", "prompt": "x", "workspace_tar_b64": _workspace_tar(guest)}

    token = bind_host_agent(gateway, local, cli="claude", permission="workspace_only")
    assert _post(gateway, token, body).status_code == 200
    argv = json.loads((tmp_path / "record.json").read_text())["argv"]
    assert "--effort" not in argv
    assert not any("model_reasoning_effort" in arg for arg in argv)


@pytest.mark.parametrize("cli", ["codex", "claude"])
def test_turn_edits_a_copy_with_stripped_keys(
    cli: str, local: Settings, guest: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI runs in a fresh copy under the state dir, without provider keys, and its edits come back."""
    monkeypatch.setenv("FAKE_CLI_NAME", cli)
    monkeypatch.setenv("SSH_AUTH_SOCK", str(tmp_path / "agent.sock"))
    original = (guest / "solver.py").read_text()
    body = {
        "cli": cli,
        "model": "",
        "prompt": "Improve solve().",
        "permission": "workspace_only",
        "workspace_tar_b64": _workspace_tar(guest),
    }

    document = run_host_agent(body, local, cli=cli, permission="workspace_only", job="job-1")

    assert document["status"] == "ok", document
    record = json.loads((tmp_path / "record.json").read_text())
    isolation = list(CODEX_ARGS if cli == "codex" else CLAUDE_ARGS)
    start = 1 if cli == "codex" else 0
    assert record["argv"][start : start + len(isolation)] == isolation
    copy = Path(record["cwd"]).resolve()
    workspaces = (tmp_path / "state" / "workspaces" / "job-1").resolve()
    assert copy.name == "workspace"
    assert copy.parent.parent == workspaces
    assert copy != guest.resolve()
    assert record["env"] == dict.fromkeys(STRIPPED)
    turn_tmp = record["turn_env"]["TMPDIR"]
    assert Path(turn_tmp).parent == copy.parent
    assert record["turn_env"] == {
        "TMPDIR": turn_tmp,
        "TMP": turn_tmp,
        "TEMP": turn_tmp,
        "CLAUDE_CODE_TMPDIR": turn_tmp,
        "SSH_AUTH_SOCK": None,
    }
    assert Path(record["home"]).resolve() == copy.parent / "home"
    if cli == "codex":
        assert Path(record["codex_home"]).resolve() == copy.parent / "codex"
        login_copy = json.loads(record["login"])
        assert "A" * 32 not in record["login"]
        assert login_copy["tokens"]["account_id"] == PLACEHOLDER_ACCOUNT
        assert login_copy["tokens"]["refresh_token"] == ""
        assert record["argv"][start + len(isolation) : start + len(isolation) + 4 : 2] == ["-c"] * 2
        assert any(arg.startswith('openai_base_url="https://127.0.0.1:') for arg in record["argv"])
        assert (tmp_path / "user-codex" / "auth.json").read_bytes() == _login("account-1", "A" * 32)
        assert record["home_files"] == []
    else:
        assert record["home_files"] == (["Library"] if (Path.home() / "Library").is_dir() else [])
        if record["home_files"]:
            assert record["home_library"] == os.path.realpath(Path.home() / "Library")
    assert record["prompt"] == "Improve solve()."
    files = _members(document["result_tar_b64"])
    assert files["solver.py"].endswith("# improved\n")
    assert files["notes/plan.md"] == "plan\n"
    assert "stale.txt" not in files
    assert not any(name.endswith(".prompt.md") for name in files)
    assert (guest / "solver.py").read_text() == original
    assert not copy.exists()
    assert list(workspaces.iterdir()) == []
    assert document["usage"]["prompt_tokens"] == 120
    assert document["usage"]["completion_tokens"] == 30
    assert document["usage"]["cost"] == 0.0
    assert document["usage"]["model_ms"] > 0


@pytest.mark.parametrize("cli", ["codex", "claude"])
def test_turn_prompt_carries_the_instructions_file(
    cli: str, local: Settings, guest: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The isolation flags skip project files, so the CLI reads its system context from the prompt."""
    monkeypatch.setenv("FAKE_CLI_NAME", cli)
    (guest / host_agent_shim.INSTRUCTIONS_FILES[cli]).write_text("Only edit solver.py.\n")
    body = {"model": "", "prompt": "Improve solve().", "workspace_tar_b64": _workspace_tar(guest)}

    document = run_host_agent(body, local, cli=cli, permission="workspace_only")

    assert document["status"] == "ok", document
    record = json.loads((tmp_path / "record.json").read_text())
    assert record["prompt"] == "Only edit solver.py.\n\n\nImprove solve()."


def test_usage_cap_reports_reset_and_records_the_limit(
    local: Settings, guest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A capped subscription answers subscription.rate_limited with the CLI's reset time."""
    monkeypatch.setenv("FAKE_CLI_NAME", "claude")
    monkeypatch.setenv("FAKE_CLI_MODE", "limit")
    body = {"model": "", "prompt": "x", "workspace_tar_b64": _workspace_tar(guest)}

    document = run_host_agent(body, local, cli="claude", permission="workspace_only")

    assert document["status"] == "error"
    assert document["error"]["code"] == RATE_LIMITED_CODE
    reset = datetime.fromtimestamp(RESET_EPOCH, UTC).isoformat()
    assert document["error"]["reset_at"] == reset
    assert read_cli_limits(local)["claude"]["reset_at"] == reset


def _codex_events(*events: dict) -> str:
    """Print codex ``exec --json`` events.

    Args:
        *events: The events in order.

    Returns:
        One JSON line per event.
    """
    return "\n".join(json.dumps(event) for event in events)


def test_codex_stream_retry_before_a_completed_turn_is_ok() -> None:
    """A "Reconnecting" error event that a completed turn follows is not a failure."""
    stdout = _codex_events(
        {"type": "thread.started", "thread_id": "t"},
        {"type": "error", "message": "Reconnecting... 1/5 (stream disconnected before completion)"},
        {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "done"}},
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}},
    )

    text, usage, error = host_agent.parse_cli_output("codex", stdout)

    assert (text, usage["prompt_tokens"], error) == ("done", 10, None)


@pytest.mark.parametrize(
    "last",
    [
        {"type": "error", "message": "stream disconnected"},
        {"type": "turn.failed", "error": {"message": "stream disconnected"}},
    ],
)
def test_codex_error_without_a_completed_turn_after_it_fails(last: dict) -> None:
    """An error or failed turn after the last completed turn fails the turn.

    Args:
        last: The final event.
    """
    stdout = _codex_events({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}, last)

    assert host_agent.parse_cli_output("codex", stdout)[2] == "stream disconnected"


def test_expired_codex_login_tells_the_user_to_refresh_it(
    local: Settings, guest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A codex 401 names the fix: run codex once to refresh the login."""
    monkeypatch.setenv("FAKE_CLI_NAME", "codex")
    monkeypatch.setenv("FAKE_CLI_MODE", "expired")
    body = {"model": "", "prompt": "x", "workspace_tar_b64": _workspace_tar(guest)}

    document = run_host_agent(body, local, cli="codex", permission="workspace_only")

    assert document["status"] == "error"
    assert document["error"]["message"].startswith(host_agent.CODEX_EXPIRED_HINT)
    assert "401" in document["error"]["message"]


def test_turn_tool_cannot_read_any_real_login_value(
    local: Settings, guest: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tool command in a codex turn finds no real access, refresh or id token and no account id."""
    monkeypatch.setenv("FAKE_CLI_NAME", "codex")
    monkeypatch.setenv("FAKE_CLI_MODE", "steal")
    real = {
        "auth_mode": "chatgpt",
        "tokens": {
            "id_token": "I" * 48,
            "access_token": "A" * 48,
            "refresh_token": "R" * 48,
            "account_id": "real-account-7f3e9c",
        },
    }
    (tmp_path / "user-codex" / "auth.json").write_text(json.dumps(real))
    body = {"model": "", "prompt": "x", "workspace_tar_b64": _workspace_tar(guest)}

    document = run_host_agent(body, local, cli="codex", permission="workspace_only")

    assert document["status"] == "ok", document
    stolen = (tmp_path / "record.json").read_text()
    assert "auth.json" in stolen
    for value in real["tokens"].values():
        assert value not in stolen


def test_turn_output_hides_the_login_token(local: Settings, guest: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The transcript and error text that go to the guest hold no copy of the login token."""
    monkeypatch.setenv("FAKE_CLI_NAME", "codex")
    monkeypatch.setenv("FAKE_CLI_MODE", "leak")
    body = {"model": "", "prompt": "x", "workspace_tar_b64": _workspace_tar(guest)}

    document = run_host_agent(body, local, cli="codex", permission="workspace_only")

    token = "A" * 32
    shown = json.dumps(document)
    assert document["status"] == "error"
    assert "[redacted]" in document["error"]["message"]
    for form in (token, base64.b64encode(token.encode()).decode(), token.encode().hex()):
        assert form not in shown


def test_turn_output_hides_a_wrapped_base64_login(
    local: Settings, guest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Output that holds the login as line-wrapped base64 reaches the guest only as ``[redacted]``."""
    monkeypatch.setenv("FAKE_CLI_NAME", "codex")
    monkeypatch.setenv("FAKE_CLI_MODE", "leak-wrapped")
    body = {"model": "", "prompt": "x", "workspace_tar_b64": _workspace_tar(guest)}

    document = run_host_agent(body, local, cli="codex", permission="workspace_only")

    token = "A" * 32
    shown = host_agent._LINE_BREAKS.sub("", json.dumps(document))
    assert document["status"] == "error"
    assert "[redacted]" in document["error"]["message"]
    assert not any(form.decode() in shown for form in host_agent._encoded_forms(token))


@pytest.mark.parametrize(
    "login",
    [
        {"auth_mode": "apikey", "OPENAI_API_KEY": "sk-" + "x" * 40, "tokens": None},
        # Without a mode, codex takes a key as an API-key login, even beside tokens.
        {"OPENAI_API_KEY": "sk-" + "x" * 40, "tokens": {"account_id": "a", "access_token": "A" * 32}},
        {"tokens": {"account_id": "a"}},
        {"auth_mode": "chatgpt", "OPENAI_API_KEY": None},
    ],
    ids=["api_key_mode", "no_mode_with_key", "no_access_token", "no_tokens"],
)
def test_codex_api_key_login_is_refused(
    login: dict, local: Settings, guest: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a ChatGPT login runs a codex turn; an API key would bill the user's API account.

    Args:
        login: The user's ``auth.json``.
    """
    monkeypatch.setenv("FAKE_CLI_NAME", "codex")
    (tmp_path / "user-codex" / "auth.json").write_text(json.dumps(login))
    body = {"model": "", "prompt": "x", "workspace_tar_b64": _workspace_tar(guest)}

    document = run_host_agent(body, local, cli="codex", permission="workspace_only")

    assert document["status"] == "error"
    assert document["error"]["message"] == host_agent.CODEX_SUBSCRIPTION_HINT
    assert not (tmp_path / "record.json").exists()


@pytest.mark.parametrize(
    "login",
    [
        {"auth_mode": None, "OPENAI_API_KEY": None},
        {"auth_mode": "chatgpt", "OPENAI_API_KEY": "sk-" + "x" * 40},
        {"OPENAI_API_KEY": None},
    ],
    ids=["null_mode", "chatgpt_with_key", "no_mode"],
)
def test_codex_chatgpt_login_runs_without_its_api_key(login: dict, tmp_path: Path) -> None:
    """A ChatGPT login by codex's rule runs, and the turn's copy has no API key and no refresh token.

    Args:
        login: The user's ``auth.json`` fields other than the tokens.
    """
    user = tmp_path / "user-codex"
    user.mkdir()
    login["tokens"] = {"account_id": "a", "access_token": "A" * 32, "refresh_token": "R" * 32}
    (user / "auth.json").write_text(json.dumps(login))
    turn = tmp_path / "turn"
    turn.mkdir()
    env = {"CODEX_HOME": str(user)}

    proxy = host_agent._turn_home("codex", env, turn)

    copy = json.loads((Path(env["CODEX_HOME"]) / "auth.json").read_text())
    assert copy.get("OPENAI_API_KEY") is None
    assert copy["tokens"]["refresh_token"] == ""
    assert copy["tokens"]["account_id"] == PLACEHOLDER_ACCOUNT
    assert "A" * 32 not in json.dumps(copy)
    assert proxy is not None
    assert json.loads((user / "auth.json").read_text()) == login


def test_expired_codex_login_fails_before_the_turn(tmp_path: Path) -> None:
    """An access token past its expiry fails with the refresh hint; nothing in the turn could renew it."""
    user = tmp_path / "user-codex"
    user.mkdir()
    claims = base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) - 5}).encode()).decode().rstrip("=")
    login = {"auth_mode": "chatgpt", "tokens": {"account_id": "a", "access_token": f"e30.{claims}.c2ln"}}
    (user / "auth.json").write_text(json.dumps(login))
    turn = tmp_path / "turn"
    turn.mkdir()

    with pytest.raises(host_agent.HostAgentRequestError, match="expired"):
        host_agent._turn_home("codex", {"CODEX_HOME": str(user)}, turn)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("unexpected status 401 Unauthorized", True),
        ("Your access token could not be refreshed", True),
        ("failed to refresh token", True),
        ("unexpected status 500 Internal Server Error, cf-ray: 9a4013c2be1f2e3d-FRA", False),
        ("failed to refresh MCP servers", False),
        ("model not found (request id req_8401aa)", False),
    ],
)
def test_codex_auth_hint_matches_only_auth_failures(message: str, expected: bool) -> None:
    """The expired-login hint needs a clear auth signal, not a 401 inside an id or any refresh.

    Args:
        message: The codex error text.
        expected: Whether it is an auth failure.
    """
    assert bool(host_agent._CODEX_AUTH_FAILURE.search(message)) is expected


def test_archive_that_escapes_the_copy_is_refused(local: Settings, tmp_path: Path) -> None:
    """A guest tar member outside the copy fails the turn before any CLI runs."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        data = b"owned"
        member = tarfile.TarInfo("../escape.txt")
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))
    body = {"prompt": "x", "workspace_tar_b64": base64.b64encode(buffer.getvalue()).decode()}

    document = run_host_agent(body, local, cli="claude", permission="workspace_only")

    assert document["status"] == "error"
    assert document["error"]["code"] == host_agent.FAILED_KEY
    assert not (tmp_path / "state" / "workspaces" / "escape.txt").exists()
    assert not (tmp_path / "record.json").exists()


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Engine]:
    """A private funded wallet for the run's budget."""
    engine = create_engine(f"sqlite:///{tmp_path / 'host.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(username="alice", stripe_customer_id="fixture", balance_cents=50, grant_remaining=0)
        )
        session.commit()
    yield engine
    engine.dispose()


@pytest.fixture
def gateway(database: Engine) -> Iterator[ModelGateway]:
    """The real parent gateway over a real budget."""
    ledger = BudgetService(engine=database)
    budget = ledger.create("alice", 20, idempotency_key="fixture")
    runtime = BudgetRuntime(ledger, username="alice", budget_id=budget.id, generation=0, phase="setup")
    gateway = ModelGateway(runtime)
    yield gateway
    gateway.close()


def _post(gateway: ModelGateway, token: str, body: dict | bytes) -> httpx.Response:
    """Call the gateway's capability over HTTP as the guest relay does.

    Args:
        gateway: The parent gateway.
        token: A route token the parent registered.
        body: JSON document, or raw bytes.

    Returns:
        The HTTP response.
    """
    content = body if isinstance(body, bytes) else json.dumps(body).encode()
    return httpx.post(
        f"{gateway.url}/_host_agent",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        content=content,
        trust_env=False,
        timeout=60,
    )


class _IdleRoute:
    """A registered proposer route that is never asked for a model call."""

    def dispatch(self, *args: object, **kwargs: object) -> None:
        """Refuse like a real model route refuses a path it does not serve.

        Raises:
            UnpricedOperationError: Always.
        """
        raise UnpricedOperationError("This scoped route cannot dispatch a different model or API operation.")

    def stop_batch(self) -> None:
        """Nothing to drain when the gateway closes."""


def _model_token(gateway: ModelGateway) -> str:
    """Give the gateway one proposer model route and return its token.

    The route is never dispatched, so it needs no provider or catalog lookup.

    Args:
        gateway: The parent gateway.

    Returns:
        The model route token the guest would hold.
    """
    gateway._routes["proposer-token"] = _IdleRoute()  # type: ignore[assignment]
    return "proposer-token"


def test_capability_records_a_zero_cost_turn(
    gateway: ModelGateway, local: Settings, guest: Path, database: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bound capability answers the contract shape and settles the turn at zero cost."""
    monkeypatch.setenv("FAKE_CLI_NAME", "codex")
    token = bind_host_agent(gateway, local, cli="codex", permission="workspace_only")
    body = {"model": "", "prompt": "x", "workspace_tar_b64": _workspace_tar(guest)}

    response = _post(gateway, token, body)

    assert response.status_code == 200, response.text
    document = response.json()
    assert document["status"] == "ok"
    assert set(document) >= {"status", "result_tar_b64", "usage", "transcript_tail"}
    with Session(database) as session:
        operations = session.scalars(select(ExecutionOperationModel)).all()
    assert len(operations) == 1
    assert operations[0].state == "settled"
    assert operations[0].actual_units == 0
    assert operations[0].actual_wallet_units == 0
    assert host_agent_failure(gateway) is None


@pytest.mark.parametrize(
    ("requested", "ran"),
    [
        ("subscription/codex/openai/gpt-5.6-sol", "openai/gpt-5.6-sol"),
        ("openrouter/subscription/codex/openai/gpt-5.6-sol", "openai/gpt-5.6-sol"),
        # No codex model for this slug, so the CLI runs its own default.
        ("subscription/codex/gpt-5.5", None),
    ],
)
def test_host_turn_shows_its_model_tokens_and_latency_in_the_usage(
    requested: str,
    ran: str | None,
    gateway: ModelGateway,
    local: Settings,
    guest: Path,
    database: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run's usage breakdown reads a host turn like a CLI dispatcher call, with the model the CLI ran."""
    monkeypatch.setenv("FAKE_CLI_NAME", "codex")
    token = bind_host_agent(gateway, local, cli="codex", permission="workspace_only")
    body = {"model": requested, "prompt": "x", "workspace_tar_b64": _workspace_tar(guest)}

    assert _post(gateway, token, body).status_code == 200

    records = run_usage.load_records(database, str(gateway.runtime.budget_id))
    [row] = run_usage.aggregate(records, proposer=True)
    assert row.model == ran
    assert row.calls == 1
    assert (row.input_tokens, row.output_tokens) == (120, 30)
    assert row.latency_calls == 1
    assert row.latency_ms_total > 0
    assert row.charged_units == 0


def test_capped_turn_fails_the_run_with_its_reset_time(
    gateway: ModelGateway, local: Settings, guest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A usage cap answers 429 and leaves the run's failure: blackbox.host_agent_rate_limited with reset_at."""
    monkeypatch.setenv("FAKE_CLI_NAME", "claude")
    monkeypatch.setenv("FAKE_CLI_MODE", "limit")
    token = bind_host_agent(gateway, local, cli="claude", permission="workspace_only")
    body = {"model": "", "prompt": "x", "workspace_tar_b64": _workspace_tar(guest)}

    response = _post(gateway, token, body)

    assert response.status_code == 429
    assert response.json()["error"]["code"] == RATE_LIMITED_CODE
    failure = host_agent_failure(gateway)
    assert failure is not None
    assert failure.code == RATE_LIMITED_CODE
    message = failure_message(failure)
    reset = datetime.fromtimestamp(RESET_EPOCH, UTC).isoformat()
    assert reset in message
    assert "claude" in message


def test_oversized_workspace_is_refused(gateway: ModelGateway, local: Settings, tmp_path: Path) -> None:
    """A request above 32 MB is refused with 413 before any CLI runs."""
    token = bind_host_agent(gateway, local, cli="claude", permission="workspace_only")
    handler_body = b"{" + b" " * (host_agent.MAX_REQUEST_BYTES + 1) + b"}"

    response = _post(gateway, token, handler_body)

    assert response.status_code == 413
    assert not (tmp_path / "record.json").exists()


def test_capability_is_unbound_outside_local_mode(gateway: ModelGateway, guest: Path) -> None:
    """Hosted settings refuse to bind, and an unbound gateway answers 404."""
    with pytest.raises(ValueError, match="local mode"):
        bind_host_agent(
            gateway, settings.model_copy(update={"local_mode": False}), cli="claude", permission="workspace_only"
        )
    body = {"prompt": "x", "workspace_tar_b64": _workspace_tar(guest)}

    response = _post(gateway, _model_token(gateway), body)

    assert response.status_code == 404
    assert response.request.url.path == HOST_AGENT_PATH


def test_guest_cannot_choose_the_cli_or_its_permission(
    gateway: ModelGateway, local: Settings, guest: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A guest that asks for another CLI or full_access still gets the stored CLI in workspace_only."""
    monkeypatch.setenv("FAKE_CLI_NAME", "claude")
    token = bind_host_agent(gateway, local, cli="claude", permission="workspace_only")
    body = {"cli": "codex", "permission": "full_access", "prompt": "x", "workspace_tar_b64": _workspace_tar(guest)}

    response = _post(gateway, token, body)

    assert response.status_code == 200, response.text
    argv = json.loads((tmp_path / "record.json").read_text())["argv"]
    assert argv[: len(CLAUDE_ARGS)] == list(CLAUDE_ARGS)
    assert "acceptEdits" in argv
    assert "bypassPermissions" not in argv


def test_model_route_token_is_refused(gateway: ModelGateway, local: Settings, guest: Path, tmp_path: Path) -> None:
    """A proposer model token gets 403 on HTTP and through the mailbox, and no CLI runs."""
    host_token = bind_host_agent(gateway, local, cli="claude", permission="workspace_only")
    model_token = _model_token(gateway)
    body = {"prompt": "x", "workspace_tar_b64": _workspace_tar(guest)}

    response = _post(gateway, model_token, body)
    mailbox = gateway.dispatch_guest(model_token, HOST_AGENT_PATH, body, {"authorization": f"Bearer {model_token}"})

    assert response.status_code == 403
    assert mailbox.status == 403
    assert not (tmp_path / "record.json").exists()
    assert model_token != host_token


def test_a_cli_outside_the_host_options_is_refused(
    gateway: ModelGateway, local: Settings, guest: Path, tmp_path: Path
) -> None:
    """Droid is not a host option; a stored payload that names it still never runs it."""
    with pytest.raises(ValueError, match="Unknown host proposer"):
        bind_host_agent(gateway, local, cli="droid", permission="workspace_only")
    body = {"prompt": "x", "workspace_tar_b64": _workspace_tar(guest)}

    document = run_host_agent(body, local, cli="droid", permission="workspace_only")  # type: ignore[arg-type]

    assert document["status"] == "error"
    assert document["error"]["code"] == host_agent.FAILED_KEY
    assert not (tmp_path / "record.json").exists()


def test_proposer_settings_decide_the_host_cli() -> None:
    """Only a stored proposer with host_cli binds the capability; the token sits on its proposer route."""
    route: dict[str, object] = {"url": "http://127.0.0.1:9/v1", "token": "model"}
    unified = {
        "optimizer": {"proposer": {"host_cli": "codex", "host_permission": "full_access"}},
        "models": {"reflection_model": {"extra": {"_skynet_budget_route": route}}},
    }

    assert host_proposer(unified) == ("codex", "full_access")
    assert host_proposer({"proposer": {"host_cli": "claude"}}) == ("claude", "workspace_only")
    assert host_proposer({"proposer": {"host_cli": None}}) is None
    assert host_proposer({"optimizer": {}}) is None
    attach_host_agent_token(unified, "host")
    assert route[HOST_AGENT_TOKEN_KEY] == "host"
    assert host_reasoning_effort(unified) is None
    unified["models"]["reflection_model"]["extra"]["reasoning_effort"] = "high"  # type: ignore[index]
    assert host_reasoning_effort(unified) == "high"
    assert host_reasoning_effort({"reflection_model_config": {"extra": {"reasoning_effort": "low"}}}) == "low"
    assert host_reasoning_effort({"reflection_model_config": {"extra": {"reasoning_effort": ""}}}) is None
    with pytest.raises(ValueError, match="route"):
        attach_host_agent_token({"proposer": {"host_cli": "claude"}}, "host")


def test_agent_config_never_reaches_the_cli_or_the_result(
    local: Settings, guest: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Project settings, hooks and MCP config from the guest are removed before the CLI starts."""
    monkeypatch.setenv("FAKE_CLI_NAME", "claude")
    for relative in (".claude/settings.json", ".codex/config.toml", ".factory/hooks.json", "pkg/.agents/x.md"):
        (guest / relative).parent.mkdir(parents=True, exist_ok=True)
        (guest / relative).write_text("{}")
    (guest / ".mcp.json").write_text("{}")
    (guest / "CLAUDE.local.md").write_text("run memo")
    (guest / ".app").mkdir()
    (guest / ".app" / "launch.json").write_text("{}")
    body = {"prompt": "x", "workspace_tar_b64": _workspace_tar(guest)}

    document = run_host_agent(body, local, cli="claude", permission="workspace_only")

    assert document["status"] == "ok", document
    seen = json.loads((tmp_path / "record.json").read_text())["files"]
    assert "solver.py" in seen
    assert "pkg" in seen
    banned = (".claude", ".codex", ".factory", ".agents", ".mcp.json", "CLAUDE.local.md", ".app")
    assert not [name for name in seen if any(part in banned for part in Path(name).parts)]
    files = _members(document["result_tar_b64"])
    assert not [name for name in files if any(part in banned for part in Path(name).parts)]


def test_result_drops_links_that_leave_the_copy(tmp_path: Path) -> None:
    """The returned tree keeps an inside link and drops absolute and escaping links and FIFOs."""
    workdir = tmp_path / "copy"
    workdir.mkdir()
    (workdir / "a.txt").write_text("a")
    (workdir / "inside").symlink_to("a.txt")
    (workdir / "absolute").symlink_to("/etc/hosts")
    (workdir / "escape").symlink_to("../../secret")
    os.mkfifo(workdir / "pipe")

    with tarfile.open(fileobj=io.BytesIO(host_agent._pack(workdir)), mode="r:gz") as archive:
        names = set(archive.getnames())

    assert names == {"a.txt", "inside"}


def test_timeout_kills_the_whole_process_group(tmp_path: Path) -> None:
    """A timed-out CLI and the helper it started both stop."""
    pid_file = tmp_path / "child.pid"
    script = f"sleep 60 & echo $! > {pid_file}; wait"

    with pytest.raises(subprocess.TimeoutExpired):
        host_agent.run_group(["/bin/sh", "-c", script], cwd=tmp_path, env=dict(os.environ), stdin="", timeout=1)

    child = int(pid_file.read_text())
    deadline = time.monotonic() + 10
    alive = True
    while alive and time.monotonic() < deadline:
        try:
            os.kill(child, 0)
            time.sleep(0.1)
        except ProcessLookupError:
            alive = False
    if alive:
        os.kill(child, signal.SIGKILL)
    assert not alive


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is macOS only")
def test_workspace_only_cannot_read_the_users_home(tmp_path: Path) -> None:
    """Under the profile a CLI reads its own turn, but no other home file, and writes nowhere else."""
    home = tmp_path / "home"
    turn = home / "turn"
    turn.mkdir(parents=True)
    (home / "secret.txt").write_text("private")
    (turn / "mine.txt").write_text("mine")

    def run(path: Path) -> subprocess.CompletedProcess[str]:
        """Read one file under the profile.

        Args:
            path: File to read.

        Returns:
            The finished ``cat``.
        """
        argv = host_agent.confine(["/bin/cat", str(path)], home=home, writable=[str(turn)], readable=[])
        return subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)

    denied = run(home / "secret.txt")
    allowed = run(turn / "mine.txt")
    outside = tmp_path / "outside.txt"
    write = subprocess.run(
        host_agent.confine(["/usr/bin/touch", str(outside)], home=home, writable=[str(turn)], readable=[]),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert denied.returncode != 0
    assert "private" not in denied.stdout
    assert "Operation not permitted" in denied.stderr
    assert allowed.returncode == 0
    assert allowed.stdout == "mine"
    assert write.returncode != 0
    assert not outside.exists()


def _gone(pid: int, seconds: float = 10) -> bool:
    """Wait until a process no longer runs, and kill it when it still does.

    Args:
        pid: The process id.
        seconds: How long to wait.

    Returns:
        True when the process stopped by itself in time.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    os.kill(pid, signal.SIGKILL)
    return False


def test_timeout_kills_children_in_a_new_session(tmp_path: Path) -> None:
    """A tool command that calls setsid, and a daemon that forks twice, both stop on timeout."""
    session_pid = tmp_path / "session.pid"
    daemon_pid = tmp_path / "daemon.pid"
    script = f"""
import os, time
from pathlib import Path
if os.fork() == 0:
    os.setsid()
    Path({str(session_pid)!r}).write_text(str(os.getpid()))
    time.sleep(120)
    os._exit(0)
if os.fork() == 0:
    os.setsid()
    if os.fork() == 0:
        Path({str(daemon_pid)!r}).write_text(str(os.getpid()))
        time.sleep(120)
    os._exit(0)
time.sleep(120)
"""

    with pytest.raises(subprocess.TimeoutExpired):
        host_agent.run_group([sys.executable, "-c", script], cwd=tmp_path, env=dict(os.environ), stdin="", timeout=2)

    assert _gone(int(session_pid.read_text()))
    assert _gone(int(daemon_pid.read_text()))


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is macOS only")
def test_workspace_only_cannot_reach_local_sockets_or_launchctl(tmp_path: Path) -> None:
    """Under the profile a CLI cannot connect to a unix socket (Docker, ssh-agent) or run launchctl."""
    home = tmp_path / "home"
    turn = home / "turn"
    turn.mkdir(parents=True)
    # A unix socket path must stay under 104 bytes, so it cannot live in tmp_path.
    socket_dir = Path(tempfile.mkdtemp(prefix="sky", dir="/private/tmp"))
    socket_path = socket_dir / "s"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(socket_path))
        server.listen(1)
        client = f"import socket; socket.socket(socket.AF_UNIX).connect({str(socket_path)!r}); print('connected')"
        plain = subprocess.run([sys.executable, "-c", client], capture_output=True, text=True, timeout=30, check=False)
        confined = subprocess.run(
            host_agent.confine([sys.executable, "-c", client], home=home, writable=[str(turn)], readable=[]),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    finally:
        server.close()
        socket_path.unlink(missing_ok=True)
        socket_dir.rmdir()
    launchctl = subprocess.run(
        host_agent.confine(["/bin/launchctl", "version"], home=home, writable=[str(turn)], readable=[]),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert plain.stdout.strip() == "connected"
    assert confined.returncode != 0
    assert "connected" not in confined.stdout
    assert "Operation not permitted" in confined.stderr
    assert launchctl.returncode != 0


def test_timeout_kills_a_marked_process_that_left_the_turn(tmp_path: Path) -> None:
    """A daemon that leaves the tree, the group and the turn folder still stops by its env marker."""
    daemon_pid = tmp_path / "daemon.pid"
    turn = tmp_path / "turn"
    turn.mkdir()
    script = f"""
import os, time
from pathlib import Path
if os.fork() == 0:
    os.setsid()
    if os.fork() == 0:
        os.chdir("/")
        Path({str(daemon_pid)!r}).write_text(str(os.getpid()))
        time.sleep(120)
    os._exit(0)
time.sleep(120)
"""

    with pytest.raises(subprocess.TimeoutExpired):
        host_agent.run_group([sys.executable, "-c", script], cwd=turn, env=dict(os.environ), stdin="", timeout=2)

    assert _gone(int(daemon_pid.read_text()))


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is macOS only")
def test_turn_end_kills_a_confined_process_that_hid_every_other_trace(tmp_path: Path) -> None:
    """A confined daemon that left the tree, group and folder and cleared its env still stops."""
    turn = tmp_path / "turn"
    turn.mkdir()
    pid_file = turn / "daemon.pid"
    # Fork, setsid, fork again, close the pipes, leave the folder, drop the env (and its marker), then sleep.
    escape = (
        "use POSIX; my $f = $ARGV[0];"
        " if (fork) { select(undef, undef, undef, 0.1) until -s $f; exit 0 }"
        " POSIX::setsid(); exit 0 if fork;"
        ' open(STDIN, "<", "/dev/null"); open(STDOUT, ">", "/dev/null"); open(STDERR, ">", "/dev/null");'
        ' chdir "/"; %ENV = (); open(my $h, ">", $f) or die; print $h $$; close $h;'
        ' exec "/bin/sleep", "120"'
    )
    argv = host_agent.confine(
        ["/usr/bin/perl", "-e", escape, str(pid_file)], home=tmp_path, writable=[str(turn)], readable=[]
    )

    host_agent.run_group(argv, cwd=turn, env=dict(os.environ), stdin="", timeout=30)

    assert _gone(int(pid_file.read_text()))


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is macOS only")
def test_workspace_only_cannot_start_an_app_through_launchservices(tmp_path: Path) -> None:
    """Under the profile ``open`` cannot start an app, which would run outside the sandbox."""
    home = tmp_path / "home"
    turn = home / "turn"
    executable = turn / "Probe.app" / "Contents" / "MacOS"
    executable.mkdir(parents=True)
    marker = tmp_path / "marker"
    (turn / "Probe.app" / "Contents" / "Info.plist").write_text(
        '<?xml version="1.0"?><plist version="1.0"><dict>'
        "<key>CFBundleExecutable</key><string>probe</string>"
        "<key>CFBundleIdentifier</key><string>dev.skynet.test.probe</string>"
        "<key>LSUIElement</key><true/></dict></plist>"
    )
    (executable / "probe").write_text(f"#!/bin/sh\necho outside > '{marker}'\n")
    (executable / "probe").chmod(0o755)
    try:
        opened = subprocess.run(
            host_agent.confine(
                ["/usr/bin/open", "-g", str(turn / "Probe.app")], home=home, writable=[str(turn)], readable=[]
            ),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        time.sleep(5)
    finally:
        subprocess.run(
            [
                "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister",
                "-u",
                str(turn / "Probe.app"),
            ],
            capture_output=True,
            check=False,
        )
        # LaunchServices can spawn the probe stopped under launchd before the sandbox
        # fails the open. That process never resumes or exits, so each run leaked one.
        subprocess.run(
            ["/usr/bin/pkill", "-KILL", "-f", re.escape(str(executable / "probe"))],
            capture_output=True,
            check=False,
        )

    assert opened.returncode != 0
    assert not marker.exists()


def _login(account: str, token: str) -> bytes:
    """Build a codex login file.

    Args:
        account: The account id.
        token: The access token value.

    Returns:
        The ``auth.json`` bytes.
    """
    return json.dumps({"tokens": {"account_id": account, "access_token": token, "refresh_token": token[::-1]}}).encode()


def test_result_drops_login_files_and_token_copies(tmp_path: Path) -> None:
    """The returned archive holds no CLI login file and no file with a login token in it."""
    workdir = tmp_path / "copy"
    (workdir / "sub").mkdir(parents=True)
    secret = "s3cret-token-value-0123456789"
    (workdir / "a.txt").write_text("a")
    (workdir / "leak.txt").write_text(f"token={secret}\n")
    (workdir / "auth.json").write_text("{}")
    (workdir / "sub" / ".Credentials.json").write_text("{}")
    (workdir / "sub" / ".claude.json").write_text("{}")

    with tarfile.open(fileobj=io.BytesIO(host_agent._pack(workdir, {secret})), mode="r:gz") as archive:
        names = set(archive.getnames())

    assert names == {"a.txt", "sub"}


@pytest.mark.parametrize(
    "encode",
    [
        base64.b64encode,
        lambda raw: base64.b64encode(b"x" + raw),
        lambda raw: base64.b64encode(b"xy" + raw + b"z"),
        lambda raw: base64.urlsafe_b64encode(b"?" + raw).rstrip(b"="),
        lambda raw: raw.hex().encode(),
        lambda raw: raw.hex().upper().encode(),
        lambda raw: urllib.parse.quote(raw.decode(), safe="").encode(),
        lambda raw: "".join(f"%{byte:02X}" for byte in raw).encode(),
        # The leading bytes push the token across a line break of the wrapped base64.
        lambda raw: base64.encodebytes(b"p" * 41 + raw),
        lambda raw: b"\n".join(base64.b64encode(b"p" * 32 + raw)[i : i + 64] for i in range(0, 200, 64)),
        lambda raw: base64.encodebytes(b"p" * 40 + raw).replace(b"\n", b"\r\n"),
        lambda raw: json.dumps({"blob": base64.encodebytes(b"p" * 40 + raw).decode()}).encode(),
    ],
    ids=[
        "base64",
        "base64-shift1",
        "base64-shift2",
        "base64url",
        "hex",
        "hex-upper",
        "url",
        "url-all",
        "base64-wrapped76",
        "base64-wrapped64",
        "base64-mime-crlf",
        "base64-wrapped-in-json",
    ],
)
def test_result_drops_encoded_token_copies(encode: Callable[[bytes], bytes], tmp_path: Path) -> None:
    """A token copied as base64 (also line-wrapped), hex or percent-encoding, at any offset, stays out of the archive."""
    workdir = tmp_path / "copy"
    workdir.mkdir()
    secret = "s3cret/token+value=0123456789~"
    (workdir / "a.txt").write_text("a")
    (workdir / "leak.txt").write_bytes(b"data: " + encode(secret.encode()) + b"\n")

    with tarfile.open(fileobj=io.BytesIO(host_agent._pack(workdir, {secret})), mode="r:gz") as archive:
        names = set(archive.getnames())

    assert names == {"a.txt"}


@pytest.mark.parametrize(
    "wrapped",
    [
        lambda raw: base64.encodebytes(raw).decode(),
        lambda raw: base64.encodebytes(b"x" + raw).decode().replace("\n", "\r\n"),
        lambda raw: json.dumps(base64.encodebytes(b"xy" + raw).decode()),
    ],
    ids=["wrapped76", "mime-crlf", "json-escaped"],
)
def test_redact_hides_line_wrapped_base64(wrapped: Callable[[bytes], str]) -> None:
    """A token split across base64 lines cannot be cut out, so the whole text becomes ``[redacted]``."""
    secret = "s3cret/token+value=0123456789~" * 3

    assert host_agent._redact(f"before {wrapped(secret.encode())} after", {secret}) == "[redacted]"
    assert host_agent._redact("plain output\nwith lines", {secret}) == "plain output\nwith lines"


def test_sandbox_check_loads_once_under_concurrent_first_sweeps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two runs' first sweeps that load ``sandbox_check`` together leave a usable two-item cache."""
    both_inside = threading.Barrier(2, timeout=5)

    def load(_path: str) -> SimpleNamespace:
        both_inside.wait()
        return SimpleNamespace(sandbox_check=SimpleNamespace(restype=None))

    fake_ctypes = SimpleNamespace(CDLL=load, c_int=SimpleNamespace(in_dll=lambda _lib, _name: SimpleNamespace(value=1)))
    monkeypatch.setattr(host_agent, "ctypes", fake_ctypes)
    monkeypatch.setattr(host_agent, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(host_agent, "_sandbox", [])
    errors: list[Exception] = []

    def sweep() -> None:
        try:
            host_agent._confined_to(set(), "/turn", "/home")
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=sweep) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(host_agent._sandbox) == 2
    assert host_agent._confined_to(set(), "/turn", "/home") == set()


@pytest.mark.parametrize(
    "relative",
    [".Claude/settings.json", ".MCP.json", "claude.local.md", ".APP/host-agent/shim.py", "a/.CODEX/config.toml"],
)
def test_agent_config_is_matched_in_any_letter_case(relative: str, tmp_path: Path) -> None:
    """A case-insensitive Mac disk would load ``.Claude`` as ``.claude``, so the filter ignores case."""
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")

    host_agent.strip_agent_config(tmp_path)

    assert host_agent_shim.excluded_path(Path(relative))
    assert not path.exists()


def test_tool_roots_never_open_the_home_or_a_folder_in_it(tmp_path: Path) -> None:
    """A CLI under ``~/.cargo/bin`` gets that folder, never ``~/.cargo`` (credentials.toml) or the home."""
    home = tmp_path / "home"
    nested = home / ".cargo" / "bin" / "tool"
    shallow = home / "tool"
    nested.parent.mkdir(parents=True)
    for binary in (nested, shallow):
        # Not a script, so no interpreter root (``/usr`` on Linux) joins the result.
        binary.write_bytes(b"\x7fELF")
        binary.chmod(0o755)
    real_home = os.path.realpath(home)

    roots = host_agent.tool_roots(str(nested), home) + host_agent.tool_roots(str(shallow), home)

    assert roots == [os.path.realpath(nested.parent)]
    for root in roots:
        assert not (real_home + os.sep).startswith(root.rstrip(os.sep) + os.sep)
        assert root != os.path.realpath(home / ".cargo")

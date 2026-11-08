"""Tests for the Vercel sandbox runtime: command wrapping, teardown and availability."""

from __future__ import annotations

import contextvars
import logging
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from pydantic import ValidationError

from core.billing.budgets import BudgetError
from core.billing.runtime import UsagePendingError
from core.config import VERCEL_SANDBOX_LIFETIME_CEILING_SECONDS, Settings
from core.exceptions import ServiceError
from core.sandbox import SandboxSpecError, sync_bridge
from core.sandbox import factory as sandbox_factory
from core.sandbox.environment import NetworkPolicy, SandboxEnvironment
from core.sandbox.errors import (
    SandboxFatalError,
    SandboxInfraError,
    SandboxRejectedError,
    SandboxTransientError,
)
from core.sandbox.factory import ProviderEntry, UnknownProviderError
from core.sandbox.providers.docker import DockerEnvironment
from core.sandbox.tests.fake_provider import InMemorySandbox

from .. import sandbox as sandbox_mod
from .. import sandbox_log
from ..sandbox import (
    _CODEX_REQUIREMENTS_CHECK,
    _CODEX_REQUIREMENTS_COMMAND,
    _CODEX_REQUIREMENTS_READBACK,
    _CREDENTIALS_MISSING,
    _PACKAGE_MISSING,
    KILL_GRACE_SECONDS,
    SANDBOX_TAG,
    AsyncBridgeRuntime,
    AsyncBridgeSession,
    CommandResult,
    LocalSubprocessRuntime,
    LocalSubprocessSession,
    SandboxSpec,
    VercelCredentials,
    VercelSandboxRuntime,
    VercelSandboxSession,
    check_codex_requirements,
    sandbox_runtime_from_settings,
    sandbox_unavailable_reason,
    unique_sandbox_name,
    write_codex_requirements,
)

# The Vercel SDK is a declared dependency; skip the settings-availability
# tests on a build that vendored an older requirements file without it.
_SDK_PRESENT = sandbox_mod.vercel_sync is not None and sandbox_mod.vercel_api is not None
_needs_sdk = pytest.mark.skipif(not _SDK_PRESENT, reason="the Vercel Sandbox SDK is not installed")

_VERCEL_ENV = (
    "SANDBOX_BACKEND",
    "VERCEL_TOKEN",
    "VERCEL_TEAM_ID",
    "VERCEL_PROJECT_ID",
    "VERCEL_SANDBOX_IMAGE",
    "VERCEL_SANDBOX_MAX_LIFETIME_SECONDS",
    "BLACKBOX_AGENT_GATEWAY_URL",
    "BLACKBOX_AGENT_GATEWAY_API_KEY",
    "LITELLM_PROXY_URL",
    "LITELLM_PROXY_API_KEY",
)


def _settings(monkeypatch: pytest.MonkeyPatch, **values: Any) -> Settings:
    """Build a Settings with the sandbox env cleared, then the given overrides.

    Ignores the developer's ``.env`` file too, so a locally configured
    Vercel token cannot leak into the assertions.

    Args:
        monkeypatch: Pytest fixture.
        **values: Field overrides.

    Returns:
        The settings.
    """
    for name in _VERCEL_ENV:
        monkeypatch.delenv(name, raising=False)
    return Settings(_env_file=None, **values)


class _FakeReader:
    """One-shot text reader over a captured stream."""

    def __init__(self, text: str) -> None:
        """Hold the stream's full text."""
        self._text = text

    def read(self) -> str:
        """Return the whole stream."""
        return self._text


class _FakeProcess:
    """Fake detached SDK process: only its output replay and kill switch matter."""

    def __init__(self, stdout: str = "", stderr: str = "") -> None:
        """Hold the captured output."""
        self.stdout = _FakeReader(stdout)
        self.stderr = _FakeReader(stderr)
        self.killed = False

    def kill(self) -> None:
        """Record the kill."""
        self.killed = True


class _FakeTime:
    """Deterministic stand-in for the sandbox module's ``time``."""

    def __init__(self, step: float = 0.0) -> None:
        """Start the clock at zero, advancing ``step`` per ``monotonic`` call."""
        self.now = 0.0
        self._step = step
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        """Return the current time, then advance it."""
        value = self.now
        self.now += self._step
        return value

    def sleep(self, seconds: float) -> None:
        """Record the requested delay without waiting."""
        self.sleeps.append(seconds)


def _exit_file(wrapped: str) -> str:
    """Return the exit-file path a wrapped command line records its code in."""
    match = re.search(r"> (\S+)$", wrapped)
    assert match is not None, wrapped
    return match.group(1)


def _output_files(wrapped: str) -> tuple[str, str]:
    """Return the stdout and stderr file paths a wrapped command line writes to."""
    match = re.search(r" > (\S+) 2> (\S+); echo", wrapped)
    assert match is not None, wrapped
    return match.group(1), match.group(2)


class _FakeFs:
    """In-memory filesystem for a fake sandbox."""

    def __init__(self) -> None:
        """Start empty."""
        self.files: dict[str, str] = {}
        self.made: list[str] = []
        self.polls = 0
        self._pending: tuple[str, str, int] | None = None

    def land_after(self, path: str, text: str, polls: int) -> None:
        """Make ``path`` hold ``text`` once it has been looked for ``polls`` times."""
        self._pending = (path, text, polls)

    def mkdir(self, path: str, cwd: str | None = None, recursive: bool = False) -> None:
        """Record a created directory."""
        self.made.append(path)

    def write_text(self, path: str, text: str, cwd: str | None = None) -> None:
        """Store a file."""
        self.files[path] = text

    def exists(self, path: str, cwd: str | None = None) -> bool:
        """Report whether a file was stored, landing a pending one when its poll is due."""
        if self._pending is not None and path == self._pending[0]:
            self.polls += 1
            if self.polls >= self._pending[2]:
                self.files[path] = self._pending[1]
        return path in self.files

    def read_text(self, path: str, cwd: str | None = None) -> str:
        """Return a stored file."""
        return self.files[path]


class _FakeBox:
    """Fake managed sandbox handle."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "", polls_to_finish: int = 0) -> None:
        """Configure the process result the box returns."""
        self.cwd = "/work"
        self.current_session = self
        self.fs = _FakeFs()
        self.name = "box"
        self.runs: list[dict[str, Any]] = []
        self.exited = False
        self.process: _FakeProcess | None = None
        self._returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._polls_to_finish = polls_to_finish

    def create_process(
        self,
        program: str,
        args: list[str],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        kill_after: float | None = None,
        sudo: bool = False,
    ) -> _FakeProcess:
        """Record the invocation, hand back a detached fake process and schedule its exit file."""
        self.runs.append({"program": program, "args": args, "env": env, "kill_after": kill_after, "sudo": sudo})
        self.fs.land_after(_exit_file(args[-1]), f"{self._returncode}\n", self._polls_to_finish)
        stdout_file, stderr_file = _output_files(args[-1])
        self.fs.files[stdout_file] = self._stdout
        self.fs.files[stderr_file] = self._stderr
        self.process = _FakeProcess(self._stdout, self._stderr)
        return self.process

    def __exit__(self, *args: Any) -> None:
        """Mark the box destroyed."""
        self.exited = True


class _FakeApiSession:
    """Fake ``vercel.api.session`` context."""

    def __init__(self, *, fail_on_exit: bool = False) -> None:
        """Track lifecycle."""
        self.entered = False
        self.exited = False
        self._fail_on_exit = fail_on_exit

    def __enter__(self) -> _FakeApiSession:
        """Enter the context."""
        self.entered = True
        return self

    def __exit__(self, *args: Any) -> bool:
        """Leave the context, optionally raising."""
        self.exited = True
        if self._fail_on_exit:
            raise RuntimeError("api session teardown blew up")
        return False


_ACTIVE = contextvars.ContextVar("fake_vercel_active_session", default=None)


class _TokenApiSession(_FakeApiSession):
    """Fake session that binds and resets a real context variable, as the SDK does."""

    def __enter__(self) -> _TokenApiSession:
        """Bind the variable and remember the token."""
        self._token = _ACTIVE.set(self)
        super().__enter__()
        return self

    def __exit__(self, *args: Any) -> bool:
        """Reset the variable, which only works in the context that set it."""
        _ACTIVE.reset(self._token)
        return super().__exit__(*args)


def test_run_wraps_in_timeout_and_flags_timeouts() -> None:
    """A timed command is wrapped in ``timeout --signal=KILL`` and 124 reads as a timeout."""
    box = _FakeBox(returncode=124)
    session = VercelSandboxSession(box, _FakeApiSession(), contextvars.copy_context())

    result = session.run("sleep 99", timeout_seconds=5)

    assert box.runs[0]["args"][:3] == ["--noprofile", "--norc", "-c"]
    wrapped = box.runs[0]["args"][3]
    assert wrapped.startswith("timeout --signal=KILL 5s bash --noprofile --norc -c 'sleep 99' > /tmp/.app-output-")
    assert "; echo $? > /tmp/.app-exit-" in wrapped
    assert box.runs[0]["kill_after"] == 5 + KILL_GRACE_SECONDS
    assert result.timed_out is True
    assert result.exit_code == 124


def test_run_caps_the_kill_timer_at_the_vercel_maximum() -> None:
    """A command given a full 24-hour box asks Vercel for no more than its five-hour kill timer."""
    box = _FakeBox(returncode=0)
    session = VercelSandboxSession(box, _FakeApiSession(), contextvars.copy_context())

    session.run("sleep 1", timeout_seconds=VERCEL_SANDBOX_LIFETIME_CEILING_SECONDS)

    wrapped = box.runs[0]["args"][3]
    assert wrapped.startswith(
        f"timeout --signal=KILL {VERCEL_SANDBOX_LIFETIME_CEILING_SECONDS}s bash --noprofile --norc -c"
    )
    assert box.runs[0]["kill_after"] == 18_000


def test_run_without_timeout_has_no_kill_timer() -> None:
    """A command with no timeout still records its exit code, and a 124 exit is not a timeout."""
    box = _FakeBox(returncode=124, stdout="hi")
    session = VercelSandboxSession(box, _FakeApiSession(), contextvars.copy_context())

    result = session.run("echo hi")

    assert box.runs[0]["args"][3].startswith("bash --noprofile --norc -c 'echo hi' > /tmp/.app-output-")
    assert box.runs[0]["kill_after"] is None
    assert result.timed_out is False
    assert result.exit_code == 124
    assert result.stdout == "hi"


@pytest.mark.parametrize("sudo", [False, True])
def test_a_vercel_run_passes_sudo_to_the_provider(sudo: bool) -> None:
    """A Vercel command runs as root only when the caller asks for it, through the SDK's own option."""
    box = _FakeBox(returncode=0)
    VercelSandboxSession(box, _FakeApiSession(), contextvars.copy_context()).run("id -u", sudo=sudo)

    assert box.runs[0]["sudo"] is sudo


def test_a_local_session_refuses_a_root_command() -> None:
    """A local session never runs a command as root on the user's machine."""
    session = LocalSubprocessSession(SandboxSpec(lifetime_seconds=60))
    try:
        with pytest.raises(ServiceError, match="cannot run commands as root"):
            session.run("id -u", sudo=True)
    finally:
        session.close()


class _RecordingSession:
    """Session that records each command and returns one fixed result."""

    def __init__(self, result: CommandResult) -> None:
        """Keep the result to return."""
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run(self, command: str, **kwargs: Any) -> CommandResult:
        """Record the command and return the fixed result."""
        self.calls.append((command, kwargs))
        return self.result


def test_the_codex_requirements_write_runs_the_fixed_command_as_root() -> None:
    """The write runs one fixed root command, and a matching read-back lets Codex start."""
    session = _RecordingSession(CommandResult(exit_code=0, stdout=_CODEX_REQUIREMENTS_READBACK))

    write_codex_requirements(session)

    assert session.calls == [(_CODEX_REQUIREMENTS_COMMAND, {"timeout_seconds": 60, "sudo": True})]
    assert _CODEX_REQUIREMENTS_COMMAND.endswith(_CODEX_REQUIREMENTS_CHECK)


def test_the_codex_requirements_check_runs_without_root() -> None:
    """A contained session only reads the file back and never asks for root."""
    session = _RecordingSession(CommandResult(exit_code=0, stdout=_CODEX_REQUIREMENTS_READBACK))

    check_codex_requirements(session)

    assert session.calls == [(_CODEX_REQUIREMENTS_CHECK, {"timeout_seconds": 60})]


@pytest.mark.parametrize(
    "result",
    [
        CommandResult(exit_code=1, stdout=_CODEX_REQUIREMENTS_READBACK),
        CommandResult(exit_code=0, stdout=_CODEX_REQUIREMENTS_READBACK, timed_out=True),
        CommandResult(exit_code=1, stdout="", stderr="No such file or directory"),
        CommandResult(exit_code=0, stdout=_CODEX_REQUIREMENTS_READBACK.replace("root:root:644", "user:user:644")),
        CommandResult(exit_code=0, stdout=_CODEX_REQUIREMENTS_READBACK.replace("false", "true")),
    ],
    ids=["failed", "timed-out", "missing", "user-owned", "wrong-content"],
)
@pytest.mark.parametrize("step", [write_codex_requirements, check_codex_requirements])
def test_a_bad_codex_requirements_read_back_stops_codex(result: CommandResult, step: Any) -> None:
    """A failed, missing, user-owned or changed file fails closed in the write and the check."""
    with pytest.raises(ServiceError, match="Codex cannot start"):
        step(_RecordingSession(result))


def test_a_planted_shell_profile_never_runs_in_a_box_command(tmp_path: Path) -> None:
    """Run the Vercel command in a real bash: profiles in ``$HOME`` stay unread (E38)."""
    box = _FakeBox(returncode=0)
    VercelSandboxSession(box, _FakeApiSession(), contextvars.copy_context()).run("printf ok")
    home = tmp_path / "home"
    home.mkdir()
    marker = tmp_path / "profile-ran"
    for name in (".bash_profile", ".bash_login", ".profile", ".bashrc"):
        (home / name).write_text(f"echo {name} >> {marker}\n")
    args = box.runs[0]["args"]
    paths = re.findall(r"/tmp/\.app-(?:output|exit)-[^ ;]+", args[3])

    try:
        subprocess.run(
            [box.runs[0]["program"], *args],
            env={"PATH": os.environ["PATH"], "HOME": str(home), "SSH_CLIENT": "10.0.0.1 1 22"},
            check=True,
        )
        assert Path(paths[0]).read_text().startswith("ok")
    finally:
        for path in paths:
            Path(path).unlink(missing_ok=True)

    assert len(paths) >= 2
    assert not marker.exists()


def test_a_planted_shell_profile_never_runs_in_a_local_command() -> None:
    """A ``.bashrc`` a version writes in the local box ``$HOME`` stays unread, even with ``SSH_CLIENT`` set (E38)."""
    session = LocalSubprocessSession(SandboxSpec(lifetime_seconds=60))
    try:
        for name in (".bash_profile", ".bash_login", ".profile", ".bashrc"):
            (session.path / name).write_text("echo ran >> profile-ran\n")

        result = session.run("printf ok", env={"SSH_CLIENT": "10.0.0.1 1 22"})

        assert result.stdout == "ok"
        assert not (session.path / "profile-ran").exists()
    finally:
        session.close()


def test_run_uses_a_fresh_exit_file_per_command() -> None:
    """Two commands in one box never share an exit file."""
    box = _FakeBox(returncode=0)
    session = VercelSandboxSession(box, _FakeApiSession(), contextvars.copy_context())

    session.run("true")
    session.run("true")

    assert _exit_file(box.runs[0]["args"][-1]) != _exit_file(box.runs[1]["args"][-1])


def test_run_polls_the_exit_file_with_growing_delays(monkeypatch: pytest.MonkeyPatch) -> None:
    """A slow, silent command is watched with doubling sleeps until its exit file lands."""
    box = _FakeBox(returncode=0, polls_to_finish=3)
    session = VercelSandboxSession(box, _FakeApiSession(), contextvars.copy_context())
    clock = _FakeTime()
    monkeypatch.setattr(sandbox_mod, "time", clock)

    result = session.run("make things")

    assert box.fs.polls == 3
    assert clock.sleeps == [0.5, 1.0, 2.0]
    assert result.exit_code == 0


def test_run_streams_output_as_the_files_grow(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each poll forwards whatever the box has written since the last one, in order."""
    box = _FakeBox(returncode=0, polls_to_finish=3)
    session = VercelSandboxSession(box, _FakeApiSession(), contextvars.copy_context())
    clock = _FakeTime()
    monkeypatch.setattr(sandbox_mod, "time", clock)
    chunks = ["one\n", "two\n", "three\n"]
    stored_read = box.fs.read_text

    def read_text(path: str, cwd: str | None = None) -> str:
        """Grow the stdout file by one chunk per poll."""
        if path.endswith(".stdout"):
            return "".join(chunks[: box.fs.polls])
        return stored_read(path, cwd)

    monkeypatch.setattr(box.fs, "read_text", read_text)
    seen: list[tuple[str, str]] = []

    result = session.run("chatty", on_output=lambda stream, text: seen.append((stream, text)))

    assert seen == [("stdout", "one\n"), ("stdout", "two\n"), ("stdout", "three\n")]
    # Output moved on every poll, so the backoff never left the floor: a chatty
    # command is usually waiting on the answer only the next poll can deliver.
    assert clock.sleeps == [0.5, 0.5, 0.5]
    assert result.stdout == "one\ntwo\nthree\n"
    assert result.exit_code == 0


def test_local_session_streams_lines_as_they_are_written() -> None:
    """The local runtime forwards each output line to the sink as it arrives, per stream."""
    session = LocalSubprocessRuntime().open(SandboxSpec(lifetime_seconds=30))
    seen: list[tuple[str, str]] = []
    try:
        result = session.run(
            "printf 'a\\nb\\n'; echo err >&2",
            timeout_seconds=10,
            on_output=lambda stream, text: seen.append((stream, text)),
        )
    finally:
        session.close()

    assert result.exit_code == 0
    assert [text for stream, text in seen if stream == "stdout"] == ["a\n", "b\n"]
    assert [text for stream, text in seen if stream == "stderr"] == ["err\n"]
    assert result.stdout == "a\nb\n"
    assert result.stderr == "err\n"


def test_run_waits_for_the_exit_code_to_be_written(monkeypatch: pytest.MonkeyPatch) -> None:
    """An exit file that exists but is still empty is polled again rather than read as a code."""
    box = _FakeBox(returncode=3)
    session = VercelSandboxSession(box, _FakeApiSession(), contextvars.copy_context())
    monkeypatch.setattr(sandbox_mod, "time", _FakeTime())
    reads: list[str] = ["", "3\n"]
    stored_read = box.fs.read_text

    def read_text(path: str, cwd: str | None = None) -> str:
        """Answer the exit file slowly; the output files read through."""
        if ".app-exit-" in path:
            return reads.pop(0)
        return stored_read(path, cwd)

    monkeypatch.setattr(box.fs, "read_text", read_text)

    result = session.run("exit 3")

    assert result.exit_code == 3
    assert reads == []


def test_run_kills_a_process_that_outlives_its_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """A command still running past kill_after plus slack is killed and the error names it."""
    box = _FakeBox(returncode=0, polls_to_finish=10**9)
    session = VercelSandboxSession(box, _FakeApiSession(), contextvars.copy_context())
    monkeypatch.setattr(sandbox_mod, "time", _FakeTime(step=30.0))

    with pytest.raises(ServiceError, match=r"still running \d+s past its 5s timeout: sleep forever"):
        session.run("sleep forever", timeout_seconds=5)

    assert box.process is not None
    assert box.process.killed is True


def test_write_files_creates_parents_and_read_file_round_trips() -> None:
    """Writing a nested path makes its parent; reading a missing path returns None."""
    box = _FakeBox()
    session = VercelSandboxSession(box, _FakeApiSession(), contextvars.copy_context())

    session.write_files({"a/b/c.txt": "content", "top.txt": "x"})

    assert box.fs.files["a/b/c.txt"] == "content"
    assert "a/b" in box.fs.made
    assert session.read_file("a/b/c.txt") == "content"
    assert session.read_file("missing.txt") is None


def test_close_destroys_the_box_and_never_raises() -> None:
    """Close exits the box and the API session even when teardown throws."""
    box = _FakeBox()
    api_session = _FakeApiSession(fail_on_exit=True)
    session = VercelSandboxSession(box, api_session, contextvars.copy_context())

    session.close()

    assert box.exited is True
    assert api_session.exited is True


class _FakeSync:
    """Fake ``vercel.sandbox.sync`` module."""

    def __init__(self, box: _FakeBox) -> None:
        """Hold the box to hand back and record calls."""
        self._box = box
        self.created: dict[str, Any] = {}
        self.credentials: dict[str, Any] = {}

    def SandboxServiceOptions(self, credentials_factory: Any) -> Any:  # noqa: N802 - SDK name
        """Capture the credentials factory and invoke it once."""
        self.credentials = credentials_factory()
        return "options"

    def SandboxCredentials(self, token: str, team_id: str, project_id: str) -> dict[str, str]:  # noqa: N802
        """Return the credentials as a dict."""
        return {"token": token, "team_id": team_id, "project_id": project_id}

    def SandboxResources(self, vcpus: int, memory: int) -> dict[str, int]:  # noqa: N802
        """Return explicit resource limits without contacting Vercel."""
        return {"vcpus": vcpus, "memory": memory}

    def create_sandbox(
        self,
        name: str | None,
        image: str | None,
        execution_time_limit: float,
        env: dict[str, str] | None,
        network_policy: Any,
        tags: dict[str, str],
        resources: Any,
        persistent: bool,
        ports: list[int],
    ) -> _FakeBox:
        """Record the sandbox request and return the box."""
        self.created = {
            "name": name,
            "image": image,
            "lifetime": execution_time_limit,
            "env": env,
            "network_policy": network_policy,
            "tags": tags,
            "resources": resources,
            "persistent": persistent,
            "ports": ports,
        }
        return self._box


class _FakeApi:
    """Fake ``vercel.api`` module."""

    def __init__(self, session: _FakeApiSession) -> None:
        """Hold the session to hand back."""
        self._session = session
        self.service_options: Any = None
        self.httpx_client_factory: Any = None

    def session(self, service_options: Any = None, httpx_client_factory: Any = None) -> _FakeApiSession:
        """Return the fake session, recording the options and client factory."""
        self.service_options = service_options
        self.httpx_client_factory = httpx_client_factory
        return self._session


def test_runtime_open_creates_a_tagged_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """``open`` enters the API session and creates a sandbox merging the base tag."""
    box = _FakeBox()
    api_session = _FakeApiSession()
    fake_sync = _FakeSync(box)
    fake_api = _FakeApi(api_session)
    monkeypatch.setattr(sandbox_mod, "vercel_sync", fake_sync)
    monkeypatch.setattr(sandbox_mod, "vercel_api", fake_api)
    runtime = VercelSandboxRuntime(VercelCredentials(token="t", team_id="team", project_id="proj"), image="img")

    session = runtime.open(SandboxSpec(lifetime_seconds=630, env={"K": "v"}, name="job-1", tags={"skynet_job": "1"}))

    client = fake_api.httpx_client_factory()
    assert isinstance(client, httpx.Client)
    assert client.timeout.read == 630
    assert client.timeout.connect == 60.0
    client.close()
    assert api_session.entered is True
    assert fake_sync.created["lifetime"] == 630
    assert fake_sync.created["env"] == {"K": "v"}
    assert fake_sync.created["name"] == "job-1"
    assert fake_sync.created["tags"] == {**SANDBOX_TAG, "skynet_job": "1"}
    assert fake_sync.created["image"] == "img"
    assert fake_sync.created["network_policy"] is None
    assert fake_sync.created["resources"] == {"vcpus": 2, "memory": 4096}
    assert fake_sync.created["persistent"] is False
    assert fake_sync.created["ports"] == []
    assert fake_sync.credentials == {"token": "t", "team_id": "team", "project_id": "proj"}
    assert isinstance(session, VercelSandboxSession)


def test_runtime_commands_use_the_original_session_without_implicit_recovery() -> None:
    """Keep command and filesystem calls on the funded session after a named handle changes."""
    original = _FakeBox(stdout="original")
    named = _FakeBox(stdout="replacement")
    named.current_session = original
    session = VercelSandboxSession(named, _FakeApiSession(), contextvars.copy_context())
    named.current_session = _FakeBox(stdout="resumed")

    session.write_files({"candidate.py": "print('ok')"})
    assert session.run("true").stdout == "original"
    assert session.read_file("candidate.py") == "print('ok')"
    assert named.runs == []
    assert named.fs.files == {}


@_needs_sdk
def test_runtime_explicitly_denies_network_for_offline_model_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use the provider firewall without a guest-visible exception for model traffic."""
    fake_sync = _FakeSync(_FakeBox())
    monkeypatch.setattr(sandbox_mod, "vercel_sync", fake_sync)
    monkeypatch.setattr(sandbox_mod, "vercel_api", _FakeApi(_FakeApiSession()))
    runtime = VercelSandboxRuntime(VercelCredentials(token="t", team_id="team", project_id="proj"), image="img")
    runtime.open(SandboxSpec(lifetime_seconds=60, network_disabled=True))
    assert fake_sync.created["network_policy"].mode == "deny-all"
    with pytest.raises(ServiceError, match="cannot inject"):
        runtime.open(SandboxSpec(lifetime_seconds=60, network_disabled=True, inject_headers={"host": {"key": "value"}}))


@_needs_sdk
def test_runtime_open_pins_the_spec_image_and_injects_headers_at_the_edge(monkeypatch: pytest.MonkeyPatch) -> None:
    """A spec's image wins over the runtime's, and header injection is a custom policy that still allows every host."""
    fake_sync = _FakeSync(_FakeBox())
    monkeypatch.setattr(sandbox_mod, "vercel_sync", fake_sync)
    monkeypatch.setattr(sandbox_mod, "vercel_api", _FakeApi(_FakeApiSession()))
    runtime = VercelSandboxRuntime(VercelCredentials(token="t", team_id="team", project_id="proj"), image="img")

    assert runtime.injects_headers is True
    runtime.open(
        SandboxSpec(lifetime_seconds=1, image="custom", inject_headers={"gw.example": {"Authorization": "Bearer k"}})
    )

    assert fake_sync.created["image"] == "custom"
    policy = fake_sync.created["network_policy"]
    assert policy.mode == "custom"
    assert list(policy.allow["*"]) == []
    [rule] = policy.allow["gw.example"]
    [transform] = rule.transform
    assert transform.headers == {"Authorization": "Bearer k"}


def test_runtime_open_closes_the_api_session_when_creation_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A create failure leaves the API session and re-raises."""
    api_session = _FakeApiSession()

    class _BrokenSync(_FakeSync):
        """Sync whose create fails."""

        def create_sandbox(self, *args: Any, **kwargs: Any) -> _FakeBox:
            """Fail like the SDK rejecting the request."""
            raise RuntimeError("no capacity")

    monkeypatch.setattr(sandbox_mod, "vercel_sync", _BrokenSync(_FakeBox()))
    monkeypatch.setattr(sandbox_mod, "vercel_api", _FakeApi(api_session))
    runtime = VercelSandboxRuntime(VercelCredentials(token="t", team_id="team", project_id="proj"), image="img")

    with pytest.raises(RuntimeError, match="no capacity"):
        runtime.open(SandboxSpec(lifetime_seconds=10))
    assert api_session.exited is True


def test_runtime_open_keeps_the_api_session_in_a_private_context(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A box opened on a worker thread closes cleanly from another thread, and the worker's context stays clean."""
    api_session = _TokenApiSession()
    monkeypatch.setattr(sandbox_mod, "vercel_sync", _FakeSync(_FakeBox()))
    monkeypatch.setattr(sandbox_mod, "vercel_api", _FakeApi(api_session))
    runtime = VercelSandboxRuntime(VercelCredentials(token="t", team_id="team", project_id="proj"), image="img")
    opened: dict[str, Any] = {}

    def open_box() -> None:
        """Open the box and note what the worker sees bound afterwards."""
        opened["session"] = runtime.open(SandboxSpec(lifetime_seconds=10))
        opened["active_after_open"] = _ACTIVE.get()

    worker = threading.Thread(target=open_box)
    worker.start()
    worker.join()

    assert api_session.entered is True
    assert opened["active_after_open"] is None
    with caplog.at_level(logging.ERROR, logger=sandbox_mod.logger.name):
        opened["session"].close()
    assert api_session.exited is True
    assert caplog.records == []


def test_unique_sandbox_name_sanitizes_the_stem_and_keeps_the_suffix_under_the_cap() -> None:
    """The stem is lower-cased and reduced to ``[a-z0-9-]``; a long stem is trimmed, the suffix never."""
    name = unique_sandbox_name("Skynet_Job.1")

    assert name.startswith("skynet-job-1-")
    assert len(name) == len("skynet-job-1-") + 8

    long = unique_sandbox_name("skynet-" + "x" * 80)

    assert len(long) == 60
    assert long[:51] == ("skynet-" + "x" * 80)[:51]
    assert long[51] == "-"
    assert len(long[52:]) == 8


def test_unique_sandbox_name_differs_per_call() -> None:
    """Two boxes for one job never share a name, so parallel opens cannot collide on Vercel."""
    assert unique_sandbox_name("skynet-job-1") != unique_sandbox_name("skynet-job-1")


def test_runtime_construction_needs_the_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the SDK, constructing the runtime is a typed error."""
    monkeypatch.setattr(sandbox_mod, "vercel_sync", None)
    monkeypatch.setattr(sandbox_mod, "vercel_api", None)

    with pytest.raises(ServiceError, match=_PACKAGE_MISSING):
        VercelSandboxRuntime(VercelCredentials(token="t", team_id="x", project_id="y"), image="img")


def test_unavailable_reason_reports_missing_package(monkeypatch: pytest.MonkeyPatch) -> None:
    """No SDK is reported before missing credentials."""
    monkeypatch.setattr(sandbox_mod, "vercel_sync", None)
    monkeypatch.setattr(sandbox_mod, "vercel_api", None)

    assert sandbox_unavailable_reason(_settings(monkeypatch, vercel_token="t")) == _PACKAGE_MISSING


@_needs_sdk
def test_unavailable_reason_reports_missing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the SDK present but no credentials, the reason says the sandbox is not configured."""
    assert sandbox_unavailable_reason(_settings(monkeypatch)) == _CREDENTIALS_MISSING


@pytest.mark.parametrize("sdk_present", [True, False])
def test_unavailable_reason_never_names_the_sandbox_vendor(monkeypatch: pytest.MonkeyPatch, sdk_present: bool) -> None:
    """Users see the unavailable reason in the engine catalog and run errors, so it never names the vendor.

    Args:
        monkeypatch: Fixture that removes the SDK or the credentials.
        sdk_present: Whether the sandbox SDK counts as installed.
    """
    if not sdk_present:
        monkeypatch.setattr(sandbox_mod, "vercel_sync", None)
        monkeypatch.setattr(sandbox_mod, "vercel_api", None)
    elif not _SDK_PRESENT:
        pytest.skip("the Vercel Sandbox SDK is not installed")

    reason = sandbox_unavailable_reason(_settings(monkeypatch))

    assert reason is not None
    assert "vercel" not in reason.lower()


@_needs_sdk
def test_configured_settings_yield_a_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Full credentials leave no reason and build a runtime; missing ones give None."""
    configured = _settings(monkeypatch, vercel_token="t", vercel_team_id="team", vercel_project_id="proj")

    assert sandbox_unavailable_reason(configured) is None
    assert isinstance(sandbox_runtime_from_settings(configured), VercelSandboxRuntime)
    assert sandbox_runtime_from_settings(_settings(monkeypatch)) is None


@_needs_sdk
def test_configured_settings_pick_the_sandbox_image(monkeypatch: pytest.MonkeyPatch) -> None:
    """Boxes boot from ``VERCEL_SANDBOX_IMAGE``, a current python image by default."""
    fake_sync = _FakeSync(_FakeBox())
    monkeypatch.setattr(sandbox_mod, "vercel_sync", fake_sync)
    monkeypatch.setattr(sandbox_mod, "vercel_api", _FakeApi(_FakeApiSession()))
    creds = {"vercel_token": "t", "vercel_team_id": "team", "vercel_project_id": "proj"}

    for image, expected in ((None, "vercel/sandbox/universal:latest"), ("my/img:1", "my/img:1")):
        overrides = {"vercel_sandbox_image": image} if image else {}
        runtime = sandbox_runtime_from_settings(_settings(monkeypatch, **creds, **overrides))
        assert runtime is not None
        runtime.open(SandboxSpec(lifetime_seconds=1))
        assert fake_sync.created["image"] == expected


def test_command_result_ok() -> None:
    """``ok`` is exit code zero."""
    assert CommandResult(exit_code=0).ok is True
    assert CommandResult(exit_code=1).ok is False


class _PolicyBox(_FakeBox):
    """Fake box whose session echoes a configured network policy after an update."""

    def __init__(self, echoed: Any) -> None:
        """Choose the policy Vercel reports back.

        Args:
            echoed: The policy the update returns, or an exception to raise.
        """
        super().__init__()
        self._echoed = echoed
        self.requested: list[Any] = []
        self.network_policy: Any = None

    def update_network_policy(self, policy: Any) -> _PolicyBox:
        """Record the request and report the configured outcome."""
        self.requested.append(policy)
        if isinstance(self._echoed, Exception):
            raise self._echoed
        self.network_policy = self._echoed
        return self


@_needs_sdk
@pytest.mark.parametrize(
    "echoed",
    [
        sandbox_mod.NetworkPolicy.deny_all() if _SDK_PRESENT else None,
        sandbox_mod.NetworkPolicy.custom(allow={}) if _SDK_PRESENT else None,
    ],
)
def test_disable_network_accepts_only_a_confirmed_deny_all(echoed: Any) -> None:
    """Ask Vercel for deny-all and accept either form it reports with no host allowed."""
    box = _PolicyBox(echoed)
    VercelSandboxSession(box, _FakeApiSession(), contextvars.copy_context()).disable_network()
    assert box.requested[0].mode == "deny-all"


@_needs_sdk
@pytest.mark.parametrize(
    "echoed",
    [
        None,
        sandbox_mod.NetworkPolicy.allow_all() if _SDK_PRESENT else None,
        sandbox_mod.NetworkPolicy.custom(allow={"pypi.org": ()}) if _SDK_PRESENT else None,
        ServiceError("policy update failed"),
    ],
)
def test_disable_network_fails_closed_without_confirmation(echoed: Any) -> None:
    """Raise when Vercel refuses the switch or reports any host still reachable."""
    session = VercelSandboxSession(_PolicyBox(echoed), _FakeApiSession(), contextvars.copy_context())
    with pytest.raises(ServiceError):
        session.disable_network()


_VERCEL_CREDS = {"vercel_token": "t", "vercel_team_id": "team", "vercel_project_id": "proj"}


@_needs_sdk
def test_legacy_backend_returns_the_sync_vercel_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """``legacy`` is the default and builds the same sync runtime as before SKY-419."""
    default = _settings(monkeypatch, **_VERCEL_CREDS)
    explicit = _settings(monkeypatch, **_VERCEL_CREDS, sandbox_backend="legacy")

    assert default.sandbox_backend == "legacy"
    for configured in (default, explicit):
        runtime = sandbox_runtime_from_settings(configured)
        assert type(runtime) is VercelSandboxRuntime
        assert runtime.injects_headers is True


@_needs_sdk
def test_async_backend_returns_the_bridge_for_the_configured_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """``async`` opens the configured provider through the sync bridge; a fake stands in for Vercel."""
    monkeypatch.setitem(
        sandbox_factory.PROVIDERS,
        "vercel",
        ProviderEntry("core.sandbox.tests.fake_provider", "InMemorySandbox", None, True),
    )
    runtime = sandbox_runtime_from_settings(_settings(monkeypatch, **_VERCEL_CREDS, sandbox_backend="async"))

    assert isinstance(runtime, AsyncBridgeRuntime)
    assert runtime.provider == "vercel"
    assert runtime.injects_headers is True
    session = runtime.open(SandboxSpec(lifetime_seconds=60, env={"GREETING": "hi"}, vcpus=4))
    assert isinstance(session, AsyncBridgeSession)
    environment = session._session.environment
    assert isinstance(environment, InMemorySandbox)
    assert environment.spec.resources.cpus == 4
    session.write_files({"a.txt": "alpha"})
    assert session.read_file("a.txt") == "alpha"
    assert session.read_file("missing.txt") is None
    result = session.run("printenv GREETING")
    assert isinstance(result, CommandResult)
    assert result.ok
    assert result.stdout.strip() == "hi"
    assert session.run("exit 3") == CommandResult(exit_code=3)
    session.close()
    assert environment.running is False


def test_unknown_sandbox_backend_fails_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """A value other than ``legacy`` or ``async`` stops the settings from loading."""
    monkeypatch.setenv("SANDBOX_BACKEND", "turbo")
    with pytest.raises(ValidationError, match=r"sandbox_backend|SANDBOX_BACKEND"):
        Settings(_env_file=None)


def test_async_runtime_refuses_an_unknown_provider() -> None:
    """The bridge runtime checks its provider when it is built, not on the first open."""
    with pytest.raises(UnknownProviderError):
        AsyncBridgeRuntime("nope")


def _fake_runtime(monkeypatch: pytest.MonkeyPatch, provider: str = "vercel") -> AsyncBridgeRuntime:
    """Build a bridge runtime whose provider is the in-memory fake.

    Args:
        monkeypatch: Pytest fixture.
        provider: The provider name the fake stands in for.

    Returns:
        The runtime.
    """
    monkeypatch.setitem(
        sandbox_factory.PROVIDERS,
        provider,
        ProviderEntry("core.sandbox.tests.fake_provider", "InMemorySandbox", None, True),
    )
    return AsyncBridgeRuntime(provider)


def _raising_exec(error: BaseException) -> Any:
    """Return an ``exec`` replacement that raises ``error``.

    Args:
        error: The error to raise.

    Returns:
        The async replacement.
    """

    async def exec_(*_args: Any, **_kwargs: Any) -> Any:
        """Raise the scripted error."""
        raise error

    return exec_


def _kill_deadline_error() -> SandboxInfraError:
    """Build the error both providers raise when a command outlives its kill deadline.

    Returns:
        A ``SandboxInfraError`` raised from a ``TimeoutError``.
    """
    try:
        try:
            raise TimeoutError
        except TimeoutError as timeout:
            raise SandboxInfraError("The sandbox command outlived its kill deadline.") from timeout
    except SandboxInfraError as error:
        return error


def test_async_spec_keeps_the_skynet_sandbox_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bridge adds ``SANDBOX_TAG`` as the legacy runtime does, and the spec's tags still win."""
    session = _fake_runtime(monkeypatch).open(SandboxSpec(lifetime_seconds=60, tags={"skynet_job": "job-1"}))
    try:
        assert session._session.environment.spec.tags == {**SANDBOX_TAG, "skynet_job": "job-1"}
    finally:
        session.close()


def test_async_runtime_stops_a_jobs_sandboxes_through_the_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker's cancel sweep reaches the async provider's tag sweep through the bridge."""
    runtime = _fake_runtime(monkeypatch)
    mine = [runtime.open(SandboxSpec(lifetime_seconds=60, tags={"skynet_job": "job-a"})) for _ in range(2)]
    other = runtime.open(SandboxSpec(lifetime_seconds=60, tags={"skynet_job": "job-b"}))
    try:
        assert runtime.stop_job_sandboxes("job-a") == 2
        assert [s._session.environment.running for s in mine] == [False, False]
        assert other._session.environment.running is True
    finally:
        other.close()


def test_async_runtime_without_a_job_sweep_raises_a_service_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A provider class that keeps the abstract sweep fails clearly, so the engine can log it."""

    no_sweep = type("NoSweep", (), {"stop_job_sandboxes": SandboxEnvironment.stop_job_sandboxes})
    runtime = _fake_runtime(monkeypatch)
    monkeypatch.setattr(sandbox_mod, "load_provider", lambda _name: no_sweep)
    with pytest.raises(ServiceError, match="cannot stop a job's sandboxes"):
        runtime.stop_job_sandboxes("job-a")


def test_async_session_disables_the_network_through_the_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """``disable_network`` applies a deny-all policy on the running async box."""
    session = _fake_runtime(monkeypatch).open(SandboxSpec(lifetime_seconds=60))
    try:
        session.disable_network()
        assert session._session.environment.applied_policies == [NetworkPolicy(disabled=True)]
    finally:
        session.close()


def test_async_session_run_logs_through_the_job_logger(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run`` goes through ``logged_command``: it owns the job's lines and redacts the box's env."""
    seen: list[tuple[Any, Any, list[str]]] = []
    real = sandbox_log.SandboxOutputLog

    def recording(command: str, secrets: Any, owner: Any) -> Any:
        """Record what the decorator passes, then build the real log."""
        seen.append((command, owner, list(secrets)))
        return real(command, secrets, owner)

    monkeypatch.setattr(sandbox_log, "SandboxOutputLog", recording)
    session = _fake_runtime(monkeypatch).open(
        SandboxSpec(lifetime_seconds=60, env={"TOKEN": "fake-secret-value"}, tags={"skynet_job": "job-7"})
    )
    try:
        assert session.log_owner == "job-7"
        assert session.run("true").ok
    finally:
        session.close()
    assert seen == [("true", "job-7", ["fake-secret-value"])]


@pytest.mark.parametrize(
    ("error", "timeout"),
    [
        (SandboxSpecError("no such image"), None),
        (SandboxRejectedError("quota refused"), None),
        (SandboxFatalError("credentials refused"), None),
        (_kill_deadline_error(), 30.0),
    ],
)
def test_async_session_maps_unretryable_errors_to_service_error(
    monkeypatch: pytest.MonkeyPatch, error: BaseException, timeout: float | None
) -> None:
    """Errors a fresh box cannot fix reach the scorer as ``ServiceError``, so it does not retry them."""
    session = _fake_runtime(monkeypatch).open(SandboxSpec(lifetime_seconds=60))
    monkeypatch.setattr(session._session.environment, "exec", _raising_exec(error))
    try:
        with pytest.raises(ServiceError) as raised:
            session.run("true", timeout_seconds=timeout)
        assert raised.value.__cause__ is error
    finally:
        session.close()


@pytest.mark.parametrize(
    ("error", "timeout"),
    [
        (SandboxInfraError("the box died"), 30.0),
        (_kill_deadline_error(), None),
        (SandboxTransientError("rate limited"), None),
    ],
)
def test_async_session_keeps_box_death_errors_retryable(
    monkeypatch: pytest.MonkeyPatch, error: BaseException, timeout: float | None
) -> None:
    """A dead box keeps its own error, so the scorer's one fresh box still applies."""
    session = _fake_runtime(monkeypatch).open(SandboxSpec(lifetime_seconds=60))
    monkeypatch.setattr(session._session.environment, "exec", _raising_exec(error))
    try:
        with pytest.raises(type(error)) as raised:
            session.run("true", timeout_seconds=timeout)
        assert raised.value is error
    finally:
        session.close()


def test_async_session_close_logs_a_stop_past_the_deadline(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A stop that outlives the bridge deadline is logged, so the caller keeps its finished result."""
    monkeypatch.setattr(sync_bridge, "_STOP_TIMEOUT_SECONDS", 0.2)
    session = _fake_runtime(monkeypatch).open(SandboxSpec(lifetime_seconds=60, tags={"skynet_job": "job-9"}))

    async def slow_stop() -> None:
        """Outlive the shortened stop deadline."""
        await anyio.sleep(1.0)

    monkeypatch.setattr(session._session.environment, "stop", slow_stop)

    def finish() -> str:
        """Return a result and close in ``finally``, like the native runtime does."""
        try:
            return "winner"
        finally:
            session.close()

    with caplog.at_level(logging.ERROR, logger=sandbox_mod.logger.name):
        assert finish() == "winner"
    [record] = caplog.records
    assert "job-9" in record.getMessage()
    assert record.exc_info is not None
    assert record.exc_info[0] is TimeoutError


def test_async_session_close_keeps_the_original_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failing stop does not replace the error that the caller raised before ``close``."""
    session = _fake_runtime(monkeypatch).open(SandboxSpec(lifetime_seconds=60))

    async def bad_stop() -> None:
        """Fail the stop."""
        raise RuntimeError("stop failed")

    monkeypatch.setattr(session._session.environment, "stop", bad_stop)
    with caplog.at_level(logging.ERROR, logger=sandbox_mod.logger.name), pytest.raises(ValueError, match="original"):
        try:
            raise ValueError("original")
        finally:
            session.close()
    [record] = caplog.records
    assert record.exc_info is not None
    assert record.exc_info[0] is RuntimeError


@pytest.mark.parametrize("error", [UsagePendingError("usage pending"), BudgetError("over budget")])
def test_async_session_close_raises_usage_errors(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    """Usage errors still reach the caller, as they do from the legacy Vercel close."""
    session = _fake_runtime(monkeypatch).open(SandboxSpec(lifetime_seconds=60))

    async def unsettled_stop() -> None:
        """Fail the stop with a usage error."""
        raise error

    monkeypatch.setattr(session._session.environment, "stop", unsettled_stop)
    with pytest.raises(type(error)) as raised:
        session.close()
    assert raised.value is error


def test_async_runtime_maps_a_bad_open_spec_to_service_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A spec the provider refuses (here three vCPUs on a fixed-size provider) is not box death."""
    with pytest.raises(ServiceError, match="cannot open this request"):
        _fake_runtime(monkeypatch).open(SandboxSpec(lifetime_seconds=60, vcpus=3))


def test_local_async_backend_passes_the_docker_image_and_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local mode gives the async Docker provider the local sandbox image and the Docker CLI path."""
    image = "sha256:" + "0" * 64
    # Docker need not run here: the test checks only the provider options.
    monkeypatch.setattr(sandbox_mod.docker_sandbox, "docker_sandbox_unavailable_reason", lambda _settings: None)
    runtime = sandbox_runtime_from_settings(
        _settings(
            monkeypatch,
            local_mode=True,
            api_host="127.0.0.1",
            sandbox_backend="async",
            local_sandbox_image=image,
            docker_bin="/fake/docker",
        )
    )
    assert isinstance(runtime, AsyncBridgeRuntime)
    assert runtime.provider == "docker"
    environment = sandbox_factory.create_environment(
        "docker",
        sandbox_mod._async_spec(SandboxSpec(lifetime_seconds=60, network_disabled=True)),
        **runtime._options,
    )
    assert isinstance(environment, DockerEnvironment)
    assert environment._image == image
    assert environment._docker_bin == "/fake/docker"


def test_async_docker_without_an_image_fails_with_a_service_error() -> None:
    """A Docker box with no image is a setup error, not a dead box."""
    with pytest.raises(ServiceError, match="needs an image"):
        AsyncBridgeRuntime("docker").open(SandboxSpec(lifetime_seconds=60, network_disabled=True))


def test_async_docker_refuses_open_egress_with_a_clear_error() -> None:
    """The async Docker provider has no open egress; the bridge says so before it starts a box."""
    runtime = AsyncBridgeRuntime("docker", image="ubuntu:24.04")
    with pytest.raises(ServiceError, match="SANDBOX_BACKEND=legacy"):
        runtime.open(SandboxSpec(lifetime_seconds=60))


_DOCKER_UP = (
    shutil.which("docker") is not None
    and subprocess.run(["docker", "image", "inspect", "ubuntu:24.04"], capture_output=True, check=False).returncode == 0
)


@pytest.mark.skipif(not _DOCKER_UP, reason="Docker or the local ubuntu:24.04 image is missing.")
def test_async_docker_runs_a_command_in_a_real_box() -> None:
    """The bridge opens a real local Docker box, runs a command in it and stops it."""
    runtime = AsyncBridgeRuntime("docker", image="ubuntu:24.04")
    session = runtime.open(
        SandboxSpec(lifetime_seconds=120, env={"GREETING": "hi"}, network_disabled=True, tags={"skynet_job": "sky419"})
    )
    try:
        session.write_files({"a.txt": "alpha"})
        assert session.read_file("a.txt") == "alpha"
        result = session.run("echo $GREETING", timeout_seconds=30)
        assert result.ok
        assert result.stdout.strip() == "hi"
    finally:
        session.close()

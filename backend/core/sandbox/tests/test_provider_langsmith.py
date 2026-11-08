"""Tests for the LangSmith sandbox provider, against a fake async SDK.

The tests never call LangSmith: every SDK name in the provider module is
replaced with an in-memory fake that records the calls.
"""

from __future__ import annotations

import asyncio
import shlex
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

import pytest

from core.sandbox import SandboxSpec, factory
from core.sandbox.errors import (
    RunTimeoutError,
    SandboxFatalError,
    SandboxRejectedError,
    SandboxTransientError,
    StartTimeoutError,
)
from core.sandbox.factory import PROVIDERS, ProductionNotAllowedError
from core.sandbox.providers import langsmith
from core.sandbox.providers.langsmith import LangSmithEnvironment
from core.sandbox.resources import ResourceMode, ResourceRequest
from core.sandbox.tests.test_environment import SandboxEnvironmentContract

_SPEC = SandboxSpec(lifetime_seconds=60, resources=ResourceRequest(cpus=1))


class FakeLangSmithError(Exception):
    """Stand in for ``LangSmithError``, the SDK base class."""


class FakeSDKError(FakeLangSmithError):
    """Stand in for ``SandboxClientError``."""

    def __init__(self, message: str = "", *, error_type: str | None = None) -> None:
        """Keep the machine-readable error type, like the SDK errors.

        Args:
            message: The error message.
            error_type: The API error type.
        """
        super().__init__(message)
        self.error_type = error_type


class FakeHTTPStatusError(Exception):
    """Stand in for ``httpx.HTTPStatusError``, which the SDK chains as the cause."""

    def __init__(self, status_code: int) -> None:
        """Keep a response with the status code.

        Args:
            status_code: The HTTP status.
        """
        super().__init__(status_code)
        self.response = type("FakeResponse", (), {"status_code": status_code})()


class FakeAuthError(FakeSDKError):
    """Stand in for ``SandboxAuthenticationError``."""


class FakeConnectionError(FakeSDKError):
    """Stand in for ``SandboxConnectionError``."""


class FakeQuotaError(FakeSDKError):
    """Stand in for ``QuotaExceededError``."""


class FakeNotFoundError(FakeSDKError):
    """Stand in for ``ResourceNotFoundError``."""


class FakeValidationError(FakeSDKError):
    """Stand in for ``ValidationError``."""


class FakeCreationError(FakeSDKError):
    """Stand in for ``ResourceCreationError``."""


class FakeResourceTimeoutError(FakeSDKError):
    """Stand in for ``ResourceTimeoutError``."""


class FakeAPIError(FakeSDKError):
    """Stand in for ``SandboxAPIError``."""


class FakeNotReadyError(FakeSDKError):
    """Stand in for ``SandboxNotReadyError``."""


class FakeDataplaneError(FakeSDKError):
    """Stand in for ``DataplaneNotConfiguredError``."""


class FakeOperationError(FakeSDKError):
    """Stand in for ``SandboxOperationError``."""


class FakeCommandTimeoutError(FakeOperationError):
    """Stand in for ``CommandTimeoutError``."""


def _caused_by_status(error: FakeSDKError, status_code: int) -> FakeSDKError:
    """Chain an HTTP status error as the cause, as the SDK does.

    Args:
        error: The SDK error.
        status_code: The HTTP status of the cause.

    Returns:
        The same error.
    """
    error.__cause__ = FakeHTTPStatusError(status_code)
    return error


@dataclass
class FakeChunk:
    """Stand in for ``OutputChunk``."""

    stream: str
    data: str


@dataclass
class FakeResult:
    """Stand in for ``ExecutionResult``."""

    stdout: str
    stderr: str
    exit_code: int


class FakeHandle:
    """Stand in for ``AsyncCommandHandle``: yield chunks, then give the result."""

    def __init__(self, result: FakeResult, *, block: bool = False, error: BaseException | None = None) -> None:
        """Keep the result that the command ends with.

        Args:
            result: The final result.
            block: True to hang in the stream until ``kill``.
            error: An error that the stream raises.
        """
        self._result = result
        self._block = block
        self._error = error
        self.killed = asyncio.Event()
        self.streaming = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[FakeChunk]:
        """Yield the stdout and stderr chunks, or hang until killed."""
        self.streaming.set()
        if self._error is not None:
            raise self._error
        if self._block:
            await asyncio.Event().wait()
        if self._result.stdout:
            yield FakeChunk("stdout", self._result.stdout)
        if self._result.stderr:
            yield FakeChunk("stderr", self._result.stderr)

    @property
    def result(self) -> Any:
        """Return an awaitable of the final result, like the SDK async property."""

        async def _value() -> FakeResult:
            """Return the result."""
            return self._result

        return _value()

    async def kill(self) -> None:
        """Record the kill."""
        self.killed.set()


class FakeBox:
    """Stand in for ``AsyncSandbox``, with a tiny shell interpreter."""

    def __init__(self, server: FakeServer) -> None:
        """Start with no files.

        Args:
            server: The fake server state.
        """
        self.server = server
        self.files: dict[str, bytes] = {}

    async def write(self, path: str, content: bytes, **kwargs: Any) -> None:
        """Store one file."""
        self.files[path] = content

    async def read(self, path: str, **kwargs: Any) -> bytes:
        """Return one file, or raise the not-found error."""
        if path not in self.files:
            raise FakeNotFoundError(path)
        return self.files[path]

    async def run(self, command: str, **kwargs: Any) -> FakeHandle:
        """Record the call and return a handle for the command."""
        self.server.runs.append({"command": command, **kwargs})
        if self.server.next_handle is not None:
            handle, self.server.next_handle = self.server.next_handle, None
        else:
            handle = FakeHandle(self._run(command, kwargs.get("env") or {}))
        self.server.handles.append(handle)
        return handle

    def _run(self, command: str, env: Mapping[str, str]) -> FakeResult:
        """Interpret one command from the fake command set.

        Args:
            command: The shell command.
            env: The environment that ``printenv`` reads.

        Returns:
            The result; exit code 127 for an unknown command.
        """
        argv = shlex.split(command)
        name, args = argv[0], argv[1:]
        if name == "true":
            return FakeResult("", "", 0)
        if name == "false":
            return FakeResult("", "", 1)
        if name == "exit":
            return FakeResult("", "", int(args[0]))
        if name == "echo":
            return FakeResult(" ".join(args) + "\n", "", 0)
        if name == "cat":
            missing = [path for path in args if path not in self.files]
            if missing:
                return FakeResult("", f"cat: {missing[0]}: No such file or directory\n", 1)
            return FakeResult("".join(self.files[path].decode("utf-8") for path in args), "", 0)
        if name == "printenv":
            value = env.get(args[0])
            return FakeResult("", "", 1) if value is None else FakeResult(value + "\n", "", 0)
        return FakeResult("", f"{name}: command not found\n", 127)


class FakeServer:
    """Hold what the fake LangSmith service saw."""

    def __init__(self) -> None:
        """Start empty."""
        self.clients: list[dict[str, Any]] = []
        self.creates: list[dict[str, Any]] = []
        self.boxes: dict[str, FakeBox] = {}
        self.deletes: list[str] = []
        self.closed = 0
        self.runs: list[dict[str, Any]] = []
        self.handles: list[FakeHandle] = []
        self.next_handle: FakeHandle | None = None
        self.create_error: BaseException | None = None
        self.create_hangs = False
        self.create_then_fail = False


class FakeClient:
    """Stand in for ``AsyncSandboxClient``."""

    server: FakeServer

    def __init__(self, **kwargs: Any) -> None:
        """Record the client options."""
        self.server.clients.append(kwargs)

    async def create_sandbox(self, **kwargs: Any) -> FakeBox:
        """Record the create call and return a new box."""
        self.server.creates.append(kwargs)
        if self.server.create_hangs:
            await asyncio.Event().wait()
        if self.server.create_error is not None:
            raise self.server.create_error
        box = FakeBox(self.server)
        self.server.boxes[kwargs["name"]] = box
        if self.server.create_then_fail:
            raise FakeConnectionError("lost after create")
        return box

    async def delete_sandbox(self, name: str) -> None:
        """Delete one box, or raise the not-found error."""
        self.server.deletes.append(name)
        if self.server.boxes.pop(name, None) is None:
            raise FakeNotFoundError(name)

    async def aclose(self) -> None:
        """Record the close."""
        self.server.closed += 1


@pytest.fixture(autouse=True)
def server(monkeypatch: pytest.MonkeyPatch) -> FakeServer:
    """Replace the SDK names in the provider module with fakes.

    Returns:
        The fake server state.
    """
    state = FakeServer()
    client_cls = type("BoundFakeClient", (FakeClient,), {"server": state})
    for name, value in {
        "AsyncSandboxClient": client_cls,
        "CommandTimeoutError": FakeCommandTimeoutError,
        "DataplaneNotConfiguredError": FakeDataplaneError,
        "LangSmithError": FakeLangSmithError,
        "QuotaExceededError": FakeQuotaError,
        "ResourceCreationError": FakeCreationError,
        "ResourceNotFoundError": FakeNotFoundError,
        "ResourceTimeoutError": FakeResourceTimeoutError,
        "SandboxAPIError": FakeAPIError,
        "SandboxAuthenticationError": FakeAuthError,
        "SandboxConnectionError": FakeConnectionError,
        "SandboxNotReadyError": FakeNotReadyError,
        "SandboxOperationError": FakeOperationError,
        "ValidationError": FakeValidationError,
    }.items():
        monkeypatch.setattr(langsmith, name, value)
    monkeypatch.delenv("LANGSMITH_SANDBOX_API_URL", raising=False)
    return state


class TestLangSmithEnvironmentContract(SandboxEnvironmentContract):
    """Run the provider contract against the LangSmith provider on the fake SDK."""

    provider_cls = LangSmithEnvironment


async def test_start_sends_resources_snapshot_and_ttls(server: FakeServer) -> None:
    """Map the spec sizes, the image and the TTLs to the create call."""
    spec = SandboxSpec(
        lifetime_seconds=60,
        image="python-3-12",
        resources=ResourceRequest(cpus=2, memory_mb=1024, disk_mb=2048),
    )
    async with LangSmithEnvironment(spec, idle_ttl_seconds=600, delete_after_stop_seconds=120) as env:
        create = server.creates[0]
        assert create["name"] == env.name
        assert create["snapshot"] == "python-3-12"
        assert create["vcpus"] == 2
        assert create["mem_bytes"] == 1024 * 1024 * 1024
        assert create["fs_capacity_bytes"] == 2048 * 1024 * 1024
        assert create["idle_ttl_seconds"] == 600
        assert create["delete_after_stop_seconds"] == 120
        assert "proxy_config" not in create


async def test_ignore_mode_sends_no_sizes(server: FakeServer) -> None:
    """Send no size fields and no snapshot when the spec asks for none."""
    spec = SandboxSpec(lifetime_seconds=60, resources=ResourceRequest(cpus=3, mode=ResourceMode.IGNORE))
    async with LangSmithEnvironment(spec):
        create = server.creates[0]
    assert not {"vcpus", "mem_bytes", "fs_capacity_bytes", "snapshot"} & create.keys()


async def test_snapshot_argument_wins_over_the_image(server: FakeServer) -> None:
    """Boot the named snapshot instead of the spec image."""
    spec = SandboxSpec(lifetime_seconds=60, image="ignored")
    async with LangSmithEnvironment(spec, snapshot="chosen"):
        assert server.creates[0]["snapshot"] == "chosen"


async def test_deny_all_and_allowlist_map_to_the_proxy_access_list(server: FakeServer) -> None:
    """Send a deny-all access list and an allow list from the spec network mode."""
    async with LangSmithEnvironment(SandboxSpec(lifetime_seconds=60, network_disabled=True)):
        pass
    async with LangSmithEnvironment(SandboxSpec(lifetime_seconds=60, allowed_hosts=("pypi.org",))):
        pass
    assert server.creates[0]["proxy_config"]["access_control"] == {"deny_list": ["*"]}
    assert server.creates[1]["proxy_config"]["access_control"] == {"allow_list": ["pypi.org"]}


async def test_client_turns_off_sdk_retries(server: FakeServer) -> None:
    """Build the client with SDK retries off and the given endpoint and timeout."""
    async with LangSmithEnvironment(_SPEC, api_url="https://sandbox.invalid", request_timeout_seconds=12):
        pass
    assert server.clients == [
        {"api_endpoint": "https://sandbox.invalid", "api_key": None, "timeout": 12, "max_retries": 0}
    ]


async def test_api_url_falls_back_to_the_environment(server: FakeServer, monkeypatch: pytest.MonkeyPatch) -> None:
    """Read the endpoint from LANGSMITH_SANDBOX_API_URL when no argument is given."""
    monkeypatch.setenv("LANGSMITH_SANDBOX_API_URL", "https://env.invalid")
    async with LangSmithEnvironment(_SPEC):
        pass
    assert server.clients[0]["api_endpoint"] == "https://env.invalid"


async def test_sandbox_names_are_unique_and_dns_safe(server: FakeServer) -> None:
    """Give each sandbox its own lowercase name of at most 63 characters."""
    spec = SandboxSpec(lifetime_seconds=60, name="My_Box " * 20)
    first, second = LangSmithEnvironment(spec), LangSmithEnvironment(spec)
    async with first, second:
        assert first.name != second.name
    for name in (first.name, second.name):
        assert name is not None
        assert len(name) <= 63
        assert name.startswith("skynet-")
        assert all(char.isdigit() or char.islower() or char == "-" for char in name)


async def test_stop_deletes_by_name_and_closes_the_client(server: FakeServer) -> None:
    """Delete the sandbox by name, close the client, and allow a second stop."""
    env = LangSmithEnvironment(_SPEC)
    await env.start()
    await env.stop()
    await env.stop()
    assert server.deletes == [env.name]
    assert server.closed == 1
    assert server.boxes == {}


async def test_stop_tolerates_a_sandbox_that_is_already_gone(server: FakeServer) -> None:
    """Accept a not-found error from the delete."""
    env = LangSmithEnvironment(_SPEC)
    await env.start()
    server.boxes.clear()
    await env.stop()
    assert server.closed == 1


async def test_failed_start_deletes_the_partial_sandbox(server: FakeServer) -> None:
    """Delete a sandbox that the server made before the create call failed."""
    server.create_then_fail = True
    env = LangSmithEnvironment(_SPEC)
    with pytest.raises(SandboxTransientError):
        await env.start()
    assert server.deletes == [env.name]
    assert server.boxes == {}
    assert server.closed == 1
    with pytest.raises(RuntimeError):
        await env.exec("true")


async def test_start_timeout_raises_and_cleans_up(server: FakeServer) -> None:
    """Raise StartTimeoutError when the sandbox is not ready in time, and delete it."""
    server.create_hangs = True
    env = LangSmithEnvironment(_SPEC, startup_timeout_seconds=0.05)
    with pytest.raises(StartTimeoutError):
        await env.start()
    assert server.deletes == [env.name]
    assert server.closed == 1


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (FakeAuthError("bad key"), SandboxFatalError),
        (FakeQuotaError("full"), SandboxTransientError),
        (FakeConnectionError("reset"), SandboxTransientError),
        (FakeValidationError("bad spec"), SandboxRejectedError),
        (_caused_by_status(FakeCreationError("no nodes", error_type="Unschedulable"), 503), SandboxTransientError),
        (_caused_by_status(FakeCreationError("pull failed", error_type="ImagePull"), 422), SandboxRejectedError),
        (_caused_by_status(FakeCreationError("crashed", error_type="CrashLoop"), 422), SandboxRejectedError),
        (_caused_by_status(FakeResourceTimeoutError("not ready"), 408), StartTimeoutError),
        (_caused_by_status(FakeAPIError("boom"), 500), SandboxTransientError),
        (FakeNotReadyError("not ready"), SandboxTransientError),
        (_caused_by_status(FakeNotFoundError("gone"), 404), SandboxFatalError),
        (FakeDataplaneError("no dataplane"), SandboxFatalError),
        (FakeOperationError("denied", error_type="PermissionDenied"), SandboxRejectedError),
        (_caused_by_status(FakeSDKError("conflict"), 409), SandboxRejectedError),
        (_caused_by_status(FakeSDKError("bad gateway"), 502), SandboxTransientError),
        (FakeLangSmithError("base"), SandboxRejectedError),
    ],
)
async def test_sdk_errors_map_to_error_groups(server: FakeServer, error: Exception, expected: type) -> None:
    """Map each SDK error class to its Skynet error group, with one create call, and delete the sandbox."""
    server.create_error = error
    env = LangSmithEnvironment(_SPEC)
    with pytest.raises(expected) as caught:
        await env.start()
    assert caught.value.__cause__ is error
    assert len(server.creates) == 1
    assert server.deletes == [env.name]


async def test_server_ready_timeout_raises_start_timeout(server: FakeServer) -> None:
    """Raise StartTimeoutError with the start limit when the server times out before the local deadline."""
    server.create_error = _caused_by_status(FakeResourceTimeoutError("not ready"), 408)
    env = LangSmithEnvironment(_SPEC, startup_timeout_seconds=30)
    with pytest.raises(StartTimeoutError) as caught:
        await env.start()
    assert caught.value.seconds == 30
    assert server.closed == 1


async def test_resource_timeout_after_start_is_transient(server: FakeServer) -> None:
    """Map a server timeout outside start to the transient group, not to a start timeout."""
    async with LangSmithEnvironment(_SPEC) as env:
        box = server.boxes[env.name]

        async def _timed_out(path: str, content: bytes, **kwargs: Any) -> None:
            """Raise the server timeout."""
            raise FakeResourceTimeoutError("slow")

        box.write = _timed_out  # type: ignore[method-assign]
        with pytest.raises(SandboxTransientError):
            await env.upload_files({"/tmp/a": "x"})


async def test_download_of_a_non_utf8_file_is_rejected(server: FakeServer) -> None:
    """Raise SandboxRejectedError, not UnicodeDecodeError, for a file that is not UTF-8 text."""
    async with LangSmithEnvironment(_SPEC) as env:
        server.boxes[env.name].files["/tmp/bin"] = b"\xff\xfe\x00bad"
        with pytest.raises(SandboxRejectedError, match="UTF-8"):
            await env.download_file("/tmp/bin")


async def test_exec_sends_server_limits_and_merged_env(server: FakeServer) -> None:
    """Send the timeout, the merged env, the cwd and the command TTL to run."""
    spec = SandboxSpec(lifetime_seconds=60, env={"A": "1", "B": "spec"})
    async with LangSmithEnvironment(spec) as env:
        await env.exec("true", cwd="/work", env={"B": "call"}, timeout_sec=12.2)
    run = server.runs[0]
    assert run["timeout"] == 13
    assert run["env"] == {"A": "1", "B": "call"}
    assert run["cwd"] == "/work"
    assert run["wait"] is False
    assert run["kill_on_disconnect"] is True
    assert run["ttl_seconds"] == 600


async def test_exec_default_timeout_comes_from_the_run_phase(server: FakeServer) -> None:
    """Use one hour when no timeout is given anywhere."""
    async with LangSmithEnvironment(_SPEC) as env:
        await env.exec("true")
    assert server.runs[0]["timeout"] == 3600
    assert server.runs[0]["ttl_seconds"] == 3660


async def test_exec_streams_stderr(server: FakeServer) -> None:
    """Send stderr chunks to the sink and return them in the result."""
    chunks: list[tuple[str, str]] = []
    async with LangSmithEnvironment(_SPEC) as env:
        result = await env.exec("nope", on_output=lambda stream, text: chunks.append((stream, text)))
    assert result.exit_code == 127
    assert chunks == [("stderr", "nope: command not found\n")]


async def test_exec_rejects_sudo(server: FakeServer) -> None:
    """Refuse a command as root before any call."""
    async with LangSmithEnvironment(_SPEC) as env:
        with pytest.raises(SandboxRejectedError):
            await env.exec("true", sudo=True)
    assert server.runs == []


async def test_server_command_timeout_raises_run_timeout(server: FakeServer) -> None:
    """Raise RunTimeoutError for the SDK command timeout and kill the command."""
    async with LangSmithEnvironment(_SPEC) as env:
        handle = FakeHandle(FakeResult("", "", 0), error=FakeCommandTimeoutError("late"))
        server.next_handle = handle
        with pytest.raises(RunTimeoutError):
            await env.exec("sleep 99", timeout_sec=5)
    assert handle.killed.is_set()


async def test_local_deadline_raises_run_timeout_and_kills(server: FakeServer, monkeypatch: pytest.MonkeyPatch) -> None:
    """Raise RunTimeoutError when the stream outlives the local deadline, and kill the command."""
    monkeypatch.setattr(langsmith, "_EXEC_TIMEOUT_GRACE_SECONDS", -0.95)
    async with LangSmithEnvironment(_SPEC) as env:
        handle = FakeHandle(FakeResult("", "", 0), block=True)
        server.next_handle = handle
        with pytest.raises(RunTimeoutError):
            await env.exec("sleep 99", timeout_sec=1)
    assert handle.killed.is_set()


async def test_cancel_kills_the_command(server: FakeServer) -> None:
    """Kill the remote command when the caller cancels exec."""
    async with LangSmithEnvironment(_SPEC) as env:
        handle = FakeHandle(FakeResult("", "", 0), block=True)
        server.next_handle = handle
        task = asyncio.create_task(env.exec("sleep 99"))
        await handle.streaming.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert handle.killed.is_set()


async def test_finished_command_is_not_killed(server: FakeServer) -> None:
    """Leave a command alone after it finished."""
    async with LangSmithEnvironment(_SPEC) as env:
        await env.exec("true")
    assert not server.handles[0].killed.is_set()


def test_missing_sdk_is_a_fatal_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse to build or preflight the provider without the langsmith extra."""
    monkeypatch.setattr(langsmith, "AsyncSandboxClient", None)
    with pytest.raises(SandboxFatalError, match="langsmith"):
        LangSmithEnvironment(_SPEC)
    with pytest.raises(SandboxFatalError, match="langsmith"):
        LangSmithEnvironment.preflight()


def test_preflight_needs_an_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail preflight without an API key and pass with one."""
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("LANGCHAIN_API_KEY", raising=False)
    with pytest.raises(SandboxFatalError, match="LANGSMITH_API_KEY"):
        LangSmithEnvironment.preflight()
    monkeypatch.setenv("LANGSMITH_API_KEY", "test-not-a-key")
    LangSmithEnvironment.preflight()


@pytest.mark.parametrize("ttl", [-60, 61, 1.5, True])
def test_rejects_a_ttl_that_is_not_a_multiple_of_60(ttl: Any) -> None:
    """Reject a TTL that the server would refuse."""
    with pytest.raises(ValueError, match="multiple of 60"):
        LangSmithEnvironment(_SPEC, idle_ttl_seconds=ttl)
    with pytest.raises(ValueError, match="multiple of 60"):
        LangSmithEnvironment(_SPEC, delete_after_stop_seconds=ttl)


def test_factory_registers_a_dev_only_provider() -> None:
    """Register the provider with its extra and refuse it in production."""
    entry = PROVIDERS["langsmith"]
    assert entry.module == "core.sandbox.providers.langsmith"
    assert entry.cls == "LangSmithEnvironment"
    assert entry.extra == "langsmith"
    assert entry.production_allowed is False


def test_factory_creates_the_provider_outside_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Build the provider through the factory outside production."""
    monkeypatch.setattr(factory.settings, "app_environment", "development")
    assert isinstance(factory.create_environment("langsmith", _SPEC), LangSmithEnvironment)


def test_factory_refuses_the_provider_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse the dev-only provider in production, for create and preflight."""
    monkeypatch.setattr(factory.settings, "app_environment", "production")
    with pytest.raises(ProductionNotAllowedError):
        factory.create_environment("langsmith", _SPEC)
    with pytest.raises(ProductionNotAllowedError):
        factory.preflight("langsmith")

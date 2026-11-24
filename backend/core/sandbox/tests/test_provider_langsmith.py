"""Tests for the LangSmith sandbox provider, against a fake async SDK.

The tests never call LangSmith: every SDK name in the provider module is
replaced with an in-memory fake that records the calls.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import threading
import time
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from core.billing.budgets import BudgetError, BudgetService
from core.billing.langsmith_usage import LangSmithUsageReservation
from core.billing.runtime import BudgetRuntime, UsagePendingError
from core.sandbox import SandboxSpec, factory
from core.sandbox.capabilities import SandboxSpecError
from core.sandbox.errors import (
    SandboxFatalError,
    SandboxInfraError,
    SandboxRejectedError,
    SandboxTransientError,
    StartTimeoutError,
)
from core.sandbox.factory import PROVIDERS, ProductionNotAllowedError
from core.sandbox.providers import langsmith
from core.sandbox.providers.langsmith import LangSmithEnvironment
from core.sandbox.resources import ResourceMode, ResourceRequest
from core.sandbox.tests.test_environment import SandboxEnvironmentContract
from core.storage.models import Base, BillingCustomerModel

try:
    from langsmith import sandbox as sdk
    from langsmith._httpx import httpx as sdk_httpx
    from langsmith.sandbox._transport import AsyncRetryTransport
    from langsmith.utils import LangSmithError as SDKLangSmithError
except ImportError:
    sdk = None
    sdk_httpx = None
    AsyncRetryTransport = None
    SDKLangSmithError = None

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


class FakeTransportError(Exception):
    """Stand in for ``httpx.TransportError``, which the SDK does not always wrap."""


class FakeTransportTimeoutError(FakeTransportError):
    """Stand in for ``httpx.TimeoutException``."""


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
        for field, value in server.box_fields.items():
            setattr(self, field, value)

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
        self.create_started = asyncio.Event()
        self.create_gate: asyncio.Event | None = None
        self.delete_started = asyncio.Event()
        self.delete_gate: asyncio.Event | None = None
        self.box_fields: dict[str, Any] = {}
        self.get_error: BaseException | None = None
        self.late_box_seconds: float | None = None
        # Each delete takes the next step: "keep" returns but keeps the box,
        # "fail" raises and keeps it, "lost" removes it and then raises.
        self.delete_steps: list[str] = []
        self.found_at: list[float] = []
        self.missing_at: list[float] = []


class FakeClient:
    """Stand in for ``AsyncSandboxClient``."""

    server: FakeServer

    def __init__(self, **kwargs: Any) -> None:
        """Record the client options."""
        self.server.clients.append(kwargs)

    async def create_sandbox(self, **kwargs: Any) -> FakeBox:
        """Record the create call and return a new box."""
        self.server.creates.append(kwargs)
        self.server.create_started.set()
        if self.server.create_gate is not None:
            await self.server.create_gate.wait()
        if self.server.create_hangs:
            await asyncio.Event().wait()
        if self.server.create_error is not None:
            if self.server.late_box_seconds is not None:
                name = kwargs["name"]
                asyncio.get_running_loop().call_later(
                    self.server.late_box_seconds, self.server.boxes.__setitem__, name, FakeBox(self.server)
                )
            raise self.server.create_error
        box = FakeBox(self.server)
        self.server.boxes[kwargs["name"]] = box
        if self.server.create_then_fail:
            raise FakeConnectionError("lost after create")
        return box

    async def delete_sandbox(self, name: str) -> None:
        """Delete one box, or raise the not-found error."""
        self.server.deletes.append(name)
        self.server.delete_started.set()
        if self.server.delete_gate is not None:
            await self.server.delete_gate.wait()
        step = self.server.delete_steps.pop(0) if self.server.delete_steps else None
        if step == "keep":
            return
        if step == "fail":
            raise FakeConnectionError("delete lost")
        if step == "lost":
            self.server.boxes.pop(name, None)
            raise FakeConnectionError("delete answer lost")
        if self.server.boxes.pop(name, None) is None:
            raise FakeNotFoundError(name)

    async def get_sandbox(self, name: str) -> FakeBox:
        """Return one box, or raise the lookup error or the not-found error."""
        if self.server.get_error is not None:
            raise self.server.get_error
        if name not in self.server.boxes:
            self.server.missing_at.append(time.monotonic())
            raise FakeNotFoundError(name)
        self.server.found_at.append(time.monotonic())
        return self.server.boxes[name]

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
    # The fakes replace the HTTPX errors too, so the tests run the same
    # without the langsmith extra, where the provider catches no HTTPX error.
    monkeypatch.setattr(langsmith, "_TRANSPORT_ERRORS", (FakeTransportError,))
    monkeypatch.setattr(langsmith, "_TIMEOUT_ERRORS", (FakeTransportTimeoutError,))
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


async def test_stop_during_create_stops_the_new_box(server: FakeServer) -> None:
    """End the start with an infra error and delete the box when a stop runs during the create."""
    server.create_gate = asyncio.Event()
    env = LangSmithEnvironment(_SPEC)
    task = asyncio.create_task(env.start())
    await asyncio.wait_for(server.create_started.wait(), 5)
    await asyncio.wait_for(env.stop(), 5)
    server.create_gate.set()
    with pytest.raises(SandboxInfraError, match="stopped during start"):
        await task
    assert server.boxes == {}
    assert server.deletes == [env.name, env.name]
    assert server.closed == 2
    assert env not in LangSmithEnvironment._running
    await env.stop()
    with pytest.raises(RuntimeError):
        await env.exec("true")


async def test_stop_during_a_failing_create_deletes_the_partial_box(server: FakeServer) -> None:
    """Delete a box that a failing create made after a stop ran, and chain the create error to the infra error."""
    server.create_gate = asyncio.Event()
    server.create_then_fail = True
    env = LangSmithEnvironment(_SPEC)
    task = asyncio.create_task(env.start())
    await asyncio.wait_for(server.create_started.wait(), 5)
    await asyncio.wait_for(env.stop(), 5)
    server.create_gate.set()
    with pytest.raises(SandboxInfraError, match="stopped during start") as caught:
        await task
    assert isinstance(caught.value.__cause__, SandboxTransientError)
    assert server.boxes == {}
    assert server.closed == 2


async def test_create_on_a_client_that_stop_closed_is_an_infra_error(server: FakeServer) -> None:
    """Raise SandboxInfraError, not the raw RuntimeError, when the create fails because a stop closed the client."""
    server.create_gate = asyncio.Event()
    env = LangSmithEnvironment(_SPEC)
    task = asyncio.create_task(env.start())
    await asyncio.wait_for(server.create_started.wait(), 5)
    await asyncio.wait_for(env.stop(), 5)
    closed = RuntimeError("Cannot send a request, as the client has been closed.")
    server.create_error = closed
    server.create_gate.set()
    with pytest.raises(SandboxInfraError, match="stopped during start") as caught:
        await task
    assert caught.value.__cause__ is closed
    assert server.deletes == [env.name, env.name]
    assert server.closed == 2
    assert env not in LangSmithEnvironment._running


async def test_repeated_cancel_during_stop_finishes_the_delete(server: FakeServer) -> None:
    """Finish the delete through two cancels, pass one cancel on, and keep later stops quiet."""
    env = LangSmithEnvironment(_SPEC)
    await env.start()
    server.delete_gate = asyncio.Event()
    task = asyncio.create_task(env.stop())
    await asyncio.wait_for(server.delete_started.wait(), 5)
    for _ in range(2):
        task.cancel()
        await asyncio.sleep(0)
    assert not task.done()
    server.delete_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert server.boxes == {}
    assert server.closed == 1
    await env.stop()


async def test_stop_ignores_a_cancelled_teardown(server: FakeServer) -> None:
    """Return from a later stop when the earlier teardown task was cancelled."""
    env = LangSmithEnvironment(_SPEC)
    await env.start()
    await env.stop()
    cancelled = asyncio.get_running_loop().create_future()
    cancelled.cancel()
    env._stop_task = cancelled
    await env.stop()


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
        (FakeTransportTimeoutError("read timed out"), StartTimeoutError),
        (FakeTransportError("protocol error"), SandboxTransientError),
        (json.JSONDecodeError("Expecting value", "not json", 0), SandboxTransientError),
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


async def test_server_command_timeout_returns_a_timed_out_result(server: FakeServer) -> None:
    """Return a timed-out result for the SDK command timeout and kill the command."""
    async with LangSmithEnvironment(_SPEC) as env:
        handle = FakeHandle(FakeResult("", "", 0), error=FakeCommandTimeoutError("late"))
        server.next_handle = handle
        result = await env.exec("sleep 99", timeout_sec=5)
        assert (result.exit_code, result.timed_out) == (124, True)
    assert handle.killed.is_set()


async def test_local_deadline_returns_a_timed_out_result_and_kills(
    server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Return a timed-out result when the stream outlives the local deadline, and kill the command."""
    monkeypatch.setattr(langsmith, "_EXEC_TIMEOUT_GRACE_SECONDS", -0.95)
    async with LangSmithEnvironment(_SPEC) as env:
        handle = FakeHandle(FakeResult("", "", 0), block=True)
        server.next_handle = handle
        result = await env.exec("sleep 99", timeout_sec=1)
        assert (result.exit_code, result.timed_out) == (124, True)
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


_BOX_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def transport_failures(server: FakeServer, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Run the provider on the real SDK over an HTTPX mock transport, with no network.

    Args:
        server: The fake SDK names, which this fixture replaces again.
        monkeypatch: Restores the provider module after the test.

    Returns:
        Maps ``"create"`` and ``"read"`` to the transport error that the call raises, or ``None``,
        and ``"create_body"`` to the raw body of a 200 create response, or ``None``.
    """
    if sdk is None:
        pytest.skip("The langsmith extra is not installed.")
    failures: dict[str, Any] = {"create": None, "read": None, "create_body": None}

    def handler(request: Any) -> Any:
        """Answer the sandbox API calls, or raise the configured transport error.

        Args:
            request: The HTTPX request.

        Returns:
            The HTTPX response.
        """
        if request.method == "POST" and request.url.path.endswith("/boxes"):
            if failures["create"] is not None:
                raise failures["create"]
            if failures["create_body"] is not None:
                return sdk_httpx.Response(200, content=failures["create_body"])
            body = json.loads(request.content)
            return sdk_httpx.Response(
                200,
                json={
                    "name": body["name"],
                    "id": _BOX_ID,
                    "status": "ready",
                    "dataplane_url": "https://dp.invalid/box",
                },
            )
        if request.method == "DELETE":
            return sdk_httpx.Response(204)
        if request.method == "GET" and request.url.path.endswith("/download"):
            if failures["read"] is not None:
                raise failures["read"]
            return sdk_httpx.Response(200, content=b"hi")
        return sdk_httpx.Response(200, json={})

    class MockClient(sdk.AsyncSandboxClient):
        """Use the real SDK client with a mock transport behind the SDK retry transport."""

        def __init__(self, **kwargs: Any) -> None:
            """Build the real client, then swap its HTTP client.

            Args:
                **kwargs: The client options.
            """
            super().__init__(**kwargs)
            self._http = sdk_httpx.AsyncClient(
                transport=AsyncRetryTransport(max_retries=0, transport=sdk_httpx.MockTransport(handler)),
                timeout=kwargs.get("timeout", 10),
            )

    monkeypatch.setattr(langsmith, "AsyncSandboxClient", MockClient)
    monkeypatch.setattr(langsmith, "LangSmithError", SDKLangSmithError)
    monkeypatch.setattr(langsmith, "_TRANSPORT_ERRORS", (sdk_httpx.TransportError,))
    monkeypatch.setattr(langsmith, "_TIMEOUT_ERRORS", (sdk_httpx.TimeoutException,))
    for name in (
        "CommandTimeoutError",
        "DataplaneNotConfiguredError",
        "QuotaExceededError",
        "ResourceCreationError",
        "ResourceNotFoundError",
        "ResourceTimeoutError",
        "SandboxAPIError",
        "SandboxAuthenticationError",
        "SandboxConnectionError",
        "SandboxNotReadyError",
        "SandboxOperationError",
        "ValidationError",
    ):
        monkeypatch.setattr(langsmith, name, getattr(sdk, name))
    return failures


def _make_sdk_env() -> LangSmithEnvironment:
    """Build a provider with a fake key and an endpoint that the mock transport serves.

    Returns:
        The stopped provider.
    """
    return LangSmithEnvironment(_SPEC, api_key="test-not-a-key", api_url="https://api.invalid/v2/sandboxes")


async def test_raw_read_timeout_during_start_is_a_start_timeout(
    transport_failures: dict[str, Any],
) -> None:
    """Map an HTTPX read timeout of the create call, which the SDK does not wrap, to StartTimeoutError."""
    request = sdk_httpx.Request("POST", "https://api.invalid/v2/sandboxes/boxes")
    transport_failures["create"] = sdk_httpx.ReadTimeout("read timed out", request=request)
    env = _make_sdk_env()
    with pytest.raises(StartTimeoutError) as caught:
        await env.start()
    assert isinstance(caught.value.__cause__, sdk_httpx.ReadTimeout)
    assert caught.value.seconds == env._startup_timeout


@pytest.mark.parametrize("error_cls", ["ReadTimeout", "RemoteProtocolError"])
async def test_raw_transport_error_after_start_is_transient(transport_failures: dict[str, Any], error_cls: str) -> None:
    """Map an HTTPX read timeout or protocol error of a file read, which the SDK does not wrap, to transient."""
    async with _make_sdk_env() as env:
        request = sdk_httpx.Request("GET", "https://dp.invalid/box/download")
        transport_failures["read"] = getattr(sdk_httpx, error_cls)("failed", request=request)
        with pytest.raises(SandboxTransientError) as caught:
            await env.download_file("/tmp/x")
        assert isinstance(caught.value.__cause__, getattr(sdk_httpx, error_cls))
        transport_failures["read"] = None
        assert await env.download_file("/tmp/x") == "hi"


async def test_broken_json_of_a_create_response_is_transient(transport_failures: dict[str, Any]) -> None:
    """Map a 200 create response that is not JSON, which the SDK parses without a check, to transient."""
    transport_failures["create_body"] = b"{not json"
    env = _make_sdk_env()
    with pytest.raises(SandboxTransientError) as caught:
        await env.start()
    assert isinstance(caught.value.__cause__, json.JSONDecodeError)


_PROTECTED_SPEC = SandboxSpec(
    lifetime_seconds=60,
    resources=ResourceRequest(cpus=2, memory_mb=1024, disk_mb=2048),
    operation_key="op-1",
)
# A short start limit keeps the window in which a create without an answer can land short.
_STARTUP_SECONDS = 0.2


@pytest.fixture(autouse=True)
def _fast_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    """Poll for the proof of a delete quickly, and end the create window at the start limit."""
    monkeypatch.setattr(langsmith, "_GONE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(langsmith, "_CREATE_LANDING_GRACE_SECONDS", 0)


class FakeUsage:
    """Stand in for ``LangSmithUsageReservation``: record each billing call in order."""

    events: list[Any]
    ends: list[float | None]
    error: BaseException | None = None
    entered: threading.Event | None = None
    gate: threading.Event | None = None

    def __init__(self, budget: Any, request: Mapping[str, Any], *, operation_key: str) -> None:
        """Record the reservation, or raise the refusal that the test set.

        Args:
            budget: The budget runtime.
            request: The priced create parameters.
            operation_key: The box identity.

        Raises:
            BaseException: The refusal that the test set.
        """
        if self.entered is not None:
            self.entered.set()
        if self.gate is not None:
            self.gate.wait(5)
        if self.error is not None:
            raise self.error
        self.request = dict(request)
        self.deleted = False
        self.events.append(("reserve", operation_key))

    def confirm_created(self, box: Any) -> None:
        """Record the confirm.

        Args:
            box: The created SDK box.
        """
        self.events.append(("confirm", box))

    def mark_deleted(self, at: float | None = None) -> None:
        """Record the confirmed delete and the end instant that the provider passed.

        Args:
            at: The end of the billed life; ``None`` means the proof time.
        """
        self.deleted = True
        self.ends.append(at)

    def release(self, reason: str) -> None:
        """Record the release of the hold.

        Args:
            reason: Why no box exists.
        """
        self.events.append(("release", reason))

    def settle(self) -> None:
        """Record the settle, and fail like the real reservation when the delete is not confirmed.

        Raises:
            UsagePendingError: The delete is not confirmed.
        """
        self.events.append(("settle", self.deleted))
        if not self.deleted:
            raise UsagePendingError("not deleted")


@pytest.fixture
def usage() -> type[FakeUsage]:
    """Give each test its own fake reservation class and event list.

    Returns:
        A ``FakeUsage`` subclass.
    """
    return type("BoundFakeUsage", (FakeUsage,), {"events": [], "ends": []})


def _protected(usage: Any, spec: SandboxSpec = _PROTECTED_SPEC, **kwargs: Any) -> LangSmithEnvironment:
    """Build a protected provider on the fake reservation.

    Args:
        usage: The reservation class.
        spec: The sandbox spec.
        **kwargs: More provider options.

    Returns:
        The provider.
    """
    kwargs.setdefault("startup_timeout_seconds", _STARTUP_SECONDS)
    kwargs.setdefault("budget", object())
    return LangSmithEnvironment(spec, usage_factory=usage, **kwargs)


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[BudgetRuntime]:
    """Bind a real budget runtime to a private funded SQLite ledger.

    Args:
        tmp_path: Per-test directory for the SQLite file.

    Yields:
        A runtime for a fresh 50-cent budget owned by ``alice``.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'langsmith.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(username="alice", stripe_customer_id="fixture", balance_cents=100, grant_remaining=0)
        )
        session.commit()
    service = BudgetService(engine=engine)
    budget = service.create("alice", 50, idempotency_key="draft")
    yield BudgetRuntime(service, username="alice", budget_id=budget.id, generation=0, phase="setup", wait_timeout=0)
    engine.dispose()


def _recording(made: list[LangSmithUsageReservation]) -> Any:
    """Build a factory that makes real reservations and keeps each one.

    Args:
        made: Receives each reservation.

    Returns:
        The usage factory.
    """

    def factory(*args: Any, **kwargs: Any) -> LangSmithUsageReservation:
        """Make one real reservation and keep it.

        Returns:
            The reservation.
        """
        reservation = LangSmithUsageReservation(*args, **kwargs)
        made.append(reservation)
        return reservation

    return factory


def _operation(runtime: BudgetRuntime, made: list[LangSmithUsageReservation]) -> Any:
    """Return the ledger state of the one reservation that the provider made.

    Args:
        runtime: The budget runtime.
        made: The reservations that the factory made.

    Returns:
        The operation snapshot.
    """
    (reservation,) = made
    return runtime.service.get_operation(reservation.operation.id, "alice")


def test_capabilities_are_billable_and_a_budget_makes_the_box_protected(usage: type[FakeUsage]) -> None:
    """Declare the provider billable, so the bridge passes a budget, and mark a funded box protected."""
    assert LangSmithEnvironment.capabilities.billable is True
    assert LangSmithEnvironment(_SPEC).capabilities.protected is False
    assert _protected(usage).capabilities.protected is True


@pytest.mark.parametrize(
    ("spec", "options"),
    [
        (SandboxSpec(lifetime_seconds=60, resources=ResourceRequest(cpus=2, memory_mb=1024, disk_mb=2048)), {}),
        (SandboxSpec(lifetime_seconds=60, resources=ResourceRequest(cpus=2), operation_key="op-1"), {}),
        (
            SandboxSpec(
                lifetime_seconds=60,
                resources=ResourceRequest(cpus=2, memory_mb=1024, disk_mb=2048, mode=ResourceMode.IGNORE),
                operation_key="op-1",
            ),
            {},
        ),
        (_PROTECTED_SPEC, {"idle_ttl_seconds": 0}),
        (_PROTECTED_SPEC, {"delete_after_stop_seconds": 0}),
    ],
)
def test_protected_box_refuses_a_spec_with_no_cost_bound(
    usage: type[FakeUsage], spec: SandboxSpec, options: dict[str, Any]
) -> None:
    """Refuse a protected box with no operation key, no fixed size, or a TTL that turns the server bound off."""
    with pytest.raises(SandboxSpecError):
        _protected(usage, spec, **options)


async def test_protected_box_reserves_before_create_and_settles_after_delete(
    server: FakeServer, usage: type[FakeUsage]
) -> None:
    """Reserve, create, confirm, delete, then settle at the confirmed delete, once."""
    env = _protected(usage)
    await env.start()
    reservation_request = env._usage.request
    box = server.boxes[env.name]
    assert usage.events == [("reserve", "op-1"), ("confirm", box)]
    assert reservation_request["vcpus"] == server.creates[0]["vcpus"] == 2
    assert reservation_request["mem_bytes"] == server.creates[0]["mem_bytes"] == 1024 * 1024 * 1024
    assert reservation_request["fs_capacity_bytes"] == server.creates[0]["fs_capacity_bytes"] == 2048 * 1024 * 1024
    assert reservation_request["lifetime_ms"] == 60_000
    assert reservation_request["idle_ttl_seconds"] == server.creates[0]["idle_ttl_seconds"]
    await env.stop()
    await env.stop()
    assert usage.events[2:] == [("settle", True)]
    assert server.deletes == [env.name]


async def test_failed_confirm_deletes_the_box_before_the_settle(server: FakeServer, usage: type[FakeUsage]) -> None:
    """Delete the created box, then settle, and raise the confirm error when the ledger refuses the confirm."""

    def refuse(self: FakeUsage, box: Any) -> None:
        """Fail like a refused ledger call.

        Args:
            self: The fake reservation.
            box: The created SDK box.

        Raises:
            BudgetError: Always.
        """
        self.events.append(("confirm", box))
        raise BudgetError("ledger refused")

    usage.confirm_created = refuse  # type: ignore[method-assign]
    env = _protected(usage)
    with pytest.raises(BudgetError, match="ledger refused"):
        await env.start()
    assert len(server.deletes) == 1
    assert usage.events[-1] == ("settle", True)
    assert [event[0] for event in usage.events] == ["reserve", "confirm", "settle"]


async def test_budget_refusal_creates_no_box(server: FakeServer, usage: type[FakeUsage]) -> None:
    """Raise the budget refusal before any create call."""
    usage.error = BudgetError("no headroom")
    env = _protected(usage)
    with pytest.raises(BudgetError):
        await env.start()
    assert server.creates == []
    assert usage.events == []


async def test_cancel_during_reserve_releases_the_hold(server: FakeServer, usage: type[FakeUsage]) -> None:
    """Release the hold that a cancelled reservation still made, because no create left."""
    usage.entered = threading.Event()
    usage.gate = threading.Event()
    env = _protected(usage)
    task = asyncio.create_task(env.start())
    await asyncio.to_thread(usage.entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    usage.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert usage.events == [("reserve", "op-1"), ("release", "create-not-sent")]
    assert server.creates == []


async def test_cancel_during_a_refused_reserve_keeps_the_cancel(server: FakeServer, usage: type[FakeUsage]) -> None:
    """Raise the cancel, not the budget refusal, when the reservation fails after a cancel."""
    usage.entered = threading.Event()
    usage.gate = threading.Event()
    usage.error = BudgetError("no headroom")
    env = _protected(usage)
    task = asyncio.create_task(env.start())
    await asyncio.to_thread(usage.entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    usage.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert usage.events == []
    assert server.creates == []


async def test_stop_during_reserve_releases_the_hold(server: FakeServer, usage: type[FakeUsage]) -> None:
    """Release the hold when a stop() ends the start before the create call."""
    usage.entered = threading.Event()
    usage.gate = threading.Event()
    env = _protected(usage)
    task = asyncio.create_task(env.start())
    await asyncio.to_thread(usage.entered.wait, 5)
    await asyncio.wait_for(env.stop(), 5)
    usage.gate.set()
    with pytest.raises(SandboxInfraError, match="stopped during start"):
        await task
    assert usage.events == [("reserve", "op-1"), ("release", "create-not-sent")]
    assert server.creates == []


async def test_refused_create_with_no_box_releases_the_hold(server: FakeServer, usage: type[FakeUsage]) -> None:
    """Release the hold when the server refused the create and has no box by its name."""
    server.create_error = _caused_by_status(FakeValidationError("too large"), 422)
    env = _protected(usage)
    with pytest.raises(SandboxRejectedError):
        await env.start()
    assert usage.events == [("reserve", "op-1"), ("release", "create-refused-422")]
    assert server.deletes == []


async def test_failed_create_settles_after_the_create_window(server: FakeServer, usage: type[FakeUsage]) -> None:
    """Delete the partial box of a create without an answer, and settle only after its create window."""
    server.create_then_fail = True
    env = _protected(usage)
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(SandboxTransientError):
        await env.start()
    assert loop.time() - started >= _STARTUP_SECONDS
    assert usage.events == [("reserve", "op-1"), ("settle", True)]
    assert server.boxes == {}


async def test_box_that_lands_late_is_deleted_before_the_settle(server: FakeServer, usage: type[FakeUsage]) -> None:
    """Treat an early 404 as no proof, and delete a box that a create without an answer makes later."""
    server.create_error = FakeConnectionError("lost")
    server.late_box_seconds = 0.05
    env = _protected(usage)
    with pytest.raises(SandboxTransientError):
        await env.start()
    assert server.boxes == {}
    assert server.deletes.count(env.name) >= 2
    assert usage.events == [("reserve", "op-1"), ("settle", True)]


async def test_gateway_error_on_create_waits_for_a_late_box(server: FakeServer, usage: type[FakeUsage]) -> None:
    """Treat a gateway 504 as no answer: keep polling, delete the late box, and end at the proof."""
    server.create_error = _caused_by_status(FakeAPIError("gateway timeout"), 504)
    server.late_box_seconds = 0.05
    env = _protected(usage)
    with pytest.raises(SandboxTransientError):
        await env.start()
    assert server.boxes == {}
    assert server.deletes.count(env.name) >= 2
    assert usage.events == [("reserve", "op-1"), ("settle", True)]
    assert usage.ends == [None]


async def test_answered_create_ends_at_the_delete_call(server: FakeServer, usage: type[FakeUsage]) -> None:
    """End the billed life of an answered create at the delete call, not at the later 404 proof."""
    env = _protected(usage)
    await env.start()
    server.get_error = FakeConnectionError("lookup lost")
    loop = asyncio.get_running_loop()
    loop.call_later(0.1, setattr, server, "get_error", None)
    before = time.monotonic()
    await env.stop()
    proved = time.monotonic()
    assert usage.events[-1] == ("settle", True)
    [ended_at] = usage.ends
    assert ended_at is not None
    assert before <= ended_at <= proved - 0.05


async def test_box_seen_after_the_delete_moves_the_end(server: FakeServer, usage: type[FakeUsage]) -> None:
    """End the billed life no earlier than a lookup that still found the box after a delete."""
    env = _protected(usage)
    await env.start()
    server.delete_steps = ["keep", "lost"]
    await env.stop()
    assert usage.events[-1] == ("settle", True)
    [seen_at] = server.found_at
    [ended_at] = usage.ends
    assert ended_at is not None
    assert ended_at >= seen_at
    assert ended_at >= server.missing_at[-1]


async def test_box_seen_after_a_good_delete_ends_at_the_404(server: FakeServer, usage: type[FakeUsage]) -> None:
    """End at the 404 proof, not at a later delete's return, once a lookup found the box after a delete."""
    env = _protected(usage)
    await env.start()
    server.delete_steps = ["keep"]
    await env.stop()
    [ended_at] = usage.ends
    assert ended_at is not None
    assert server.deletes == [env.name, env.name]
    assert ended_at >= server.missing_at[-1]


async def test_failed_first_delete_retries_until_proof(server: FakeServer, usage: type[FakeUsage]) -> None:
    """Retry a failed first delete in the bounded poll, then settle on the 404 proof."""
    env = _protected(usage)
    await env.start()
    server.delete_steps = ["fail"]
    await env.stop()
    assert server.boxes == {}
    assert server.deletes == [env.name, env.name]
    assert usage.events[-1] == ("settle", True)


async def test_real_reservation_checks_the_size_after_a_stop_during_create(
    server: FakeServer, ledger: BudgetRuntime
) -> None:
    """Record the reported size when a stop() comes during a create that still makes a larger box."""
    server.box_fields = {"id": "sandbox-uuid", "vcpus": 4}
    server.create_gate = asyncio.Event()
    made: list[LangSmithUsageReservation] = []
    env = _protected(_recording(made), budget=ledger, idle_ttl_seconds=60, delete_after_stop_seconds=60)
    task = asyncio.create_task(env.start())
    await asyncio.wait_for(server.create_started.wait(), 5)
    await asyncio.wait_for(env.stop(), 5)
    server.create_gate.set()
    with pytest.raises(SandboxInfraError, match="stopped during start"):
        await task
    assert made[0].reported == {"vcpus": 4}
    assert server.boxes == {}
    assert _operation(ledger, made).state == "settled"


async def test_no_proof_keeps_the_hold_pending_and_the_start_error_carries_it(
    server: FakeServer, usage: type[FakeUsage], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the hold pending when no lookup proves the box gone, and attach that to the start error."""
    monkeypatch.setattr(langsmith, "_DELETE_TIMEOUT_SECONDS", 0.1)
    server.create_error = FakeConnectionError("lost")
    server.get_error = FakeConnectionError("lookup lost")
    env = _protected(usage)
    with pytest.raises(SandboxTransientError) as caught:
        await env.start()
    assert usage.events == [("reserve", "op-1"), ("settle", False)]
    assert any("stays pending" in note for note in caught.value.__notes__)
    await env.stop()


async def test_cancelled_create_settles_after_the_create_window(server: FakeServer, usage: type[FakeUsage]) -> None:
    """Delete the box and settle its reservation after the create window when the start is cancelled."""
    server.create_gate = asyncio.Event()
    env = _protected(usage)
    task = asyncio.create_task(env.start())
    await asyncio.wait_for(server.create_started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert usage.events == [("reserve", "op-1"), ("settle", True)]


async def test_stop_during_create_settles_only_after_the_last_delete(
    server: FakeServer, usage: type[FakeUsage]
) -> None:
    """Hold the settle while a create runs, then settle once after the delete of the late box."""
    server.create_gate = asyncio.Event()
    env = _protected(usage)
    task = asyncio.create_task(env.start())
    await asyncio.wait_for(server.create_started.wait(), 5)
    await asyncio.wait_for(env.stop(), 5)
    assert usage.events == [("reserve", "op-1")]
    server.create_gate.set()
    with pytest.raises(SandboxInfraError, match="stopped during start"):
        await task
    assert usage.events == [("reserve", "op-1"), ("confirm", usage.events[1][1]), ("settle", True)]
    assert server.boxes == {}


async def test_failed_delete_keeps_the_reservation_pending(
    server: FakeServer, usage: type[FakeUsage], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Settle without a confirmed delete, raise the pending error once, and keep later stops quiet."""
    # The retry poll is bounded by the delete timeout; keep it short.
    monkeypatch.setattr(langsmith, "_DELETE_TIMEOUT_SECONDS", 0.1)
    env = _protected(usage)
    await env.start()

    async def broken_delete(name: str) -> None:
        """Fail the delete like a lost connection.

        Args:
            name: The sandbox name.

        Raises:
            FakeConnectionError: Always.
        """
        raise FakeConnectionError(name)

    env._client.delete_sandbox = broken_delete
    with pytest.raises(UsagePendingError):
        await env.stop()
    assert usage.events[-1] == ("settle", False)
    await env.stop()


async def test_protected_box_stops_when_its_funded_lifetime_ends(server: FakeServer, usage: type[FakeUsage]) -> None:
    """Delete and settle a protected box when its funded lifetime ends without a stop call."""
    spec = SandboxSpec(
        lifetime_seconds=0.05,
        resources=ResourceRequest(cpus=2, memory_mb=1024, disk_mb=2048),
        operation_key="op-1",
    )
    env = _protected(usage, spec)
    await env.start()
    await asyncio.wait_for(env._expiry, 5)
    assert server.boxes == {}
    assert usage.events[-1] == ("settle", True)
    with pytest.raises(RuntimeError):
        await env.exec("true")
    await env.stop()


async def test_real_reservation_settles_a_stopped_box(server: FakeServer, ledger: BudgetRuntime) -> None:
    """Reserve, confirm and settle a box on the real ledger through the provider."""
    server.box_fields = {"id": "sandbox-uuid", "vcpus": 2, "mem_bytes": 1024 * 1024 * 1024}
    made: list[LangSmithUsageReservation] = []
    env = _protected(_recording(made), budget=ledger, idle_ttl_seconds=60, delete_after_stop_seconds=60)
    await env.start()
    assert _operation(ledger, made).state == "dispatched"
    await env.stop()
    operation = _operation(ledger, made)
    assert operation.state == "settled"
    assert operation.provider_request_id == "sandbox-uuid"
    assert operation.actual_cents <= operation.max_cents
    assert operation.id not in ledger._pinned


async def test_real_reservation_prices_a_larger_box_at_its_size(server: FakeServer, ledger: BudgetRuntime) -> None:
    """Run a box larger than funded, then settle it at the reported size, inside the coverage."""
    server.box_fields = {"id": "sandbox-uuid", "vcpus": 4}
    made: list[LangSmithUsageReservation] = []
    env = _protected(_recording(made), budget=ledger, idle_ttl_seconds=60, delete_after_stop_seconds=60)
    await env.start()
    await env.stop()
    operation = _operation(ledger, made)
    assert operation.state == "settled"
    assert operation.actual_cents <= operation.max_cents
    settled = ledger.service.get_reconciliation(operation.id, "alice")
    (evidence,) = [item for item in settled.evidence if "priced_sizes" in item]
    assert evidence["priced_sizes"]["vcpus"] == 4


async def test_real_reservation_releases_a_refused_create(server: FakeServer, ledger: BudgetRuntime) -> None:
    """Give the whole hold back on the real ledger when the server refused the create."""
    server.create_error = _caused_by_status(FakeQuotaError("full"), 429)
    made: list[LangSmithUsageReservation] = []
    env = _protected(_recording(made), budget=ledger, idle_ttl_seconds=60, delete_after_stop_seconds=60)
    with pytest.raises(SandboxTransientError):
        await env.start()
    assert _operation(ledger, made).state == "released"
    assert ledger.service.get(ledger.budget_id, "alice").reserved_cents == 0

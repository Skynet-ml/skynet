"""Tests for the Kernel provider against a fake SDK, with no network and no cost."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import itertools
import json
import shlex
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from core.billing.budgets import BudgetService
from core.billing.runtime import BudgetRuntime, UsagePendingError
from core.config import settings
from core.sandbox import factory
from core.sandbox.capabilities import SandboxSpecError
from core.sandbox.environment import JOB_TAG
from core.sandbox.errors import SandboxFatalError, SandboxInfraError, StartTimeoutError
from core.sandbox.pacing import ProviderAtCapacityError
from core.sandbox.providers import kernel
from core.sandbox.resources import ResourceRequest, SandboxSpec
from core.sandbox.tests.test_environment import SandboxEnvironmentContract
from core.storage.models import Base, BillingCustomerModel, ExecutionOperationModel

pytest.importorskip("kernel", reason="The kernel extra is not installed.")

_REQUEST = httpx.Request("POST", "https://api.example.invalid/browsers")


def _status_error(cls: type[BaseException], status: int) -> BaseException:
    """Return an SDK status error with an HTTP status.

    Args:
        cls: The SDK error class.
        status: The status code.

    Returns:
        The error.
    """
    return cls("error", response=httpx.Response(status, request=_REQUEST), body=None)


class FakeClock:
    """Advance time only when the code under test sleeps."""

    def __init__(self) -> None:
        """Start the clock at zero."""
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        """Return the fake time.

        Returns:
            The current fake time in seconds.
        """
        return self.now

    async def sleep(self, seconds: float) -> None:
        """Move the fake time forward and yield once.

        Args:
            seconds: The requested delay.
        """
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


class FakeBrowser:
    """Hold the state of one fake Kernel browser VM."""

    def __init__(self, session_id: str, options: Mapping[str, Any], cdp_ws_url: str) -> None:
        """Keep the create options and start with no files.

        Args:
            session_id: The browser id.
            options: The create options.
            cdp_ws_url: The CDP URL that the create returns.
        """
        self.session_id = session_id
        self.name = options.get("name")
        self.tags = dict(options.get("tags") or {})
        self.cdp_ws_url = cdp_ws_url
        self.webdriver_ws_url = f"wss://wd.example.invalid/{session_id}"
        self.base_url = f"https://base.example.invalid/{session_id}"
        self.headless = options.get("headless")
        self.gpu = False
        self.memory = options.get("memory", "8GiB")
        self.timeout_seconds = options.get("timeout_seconds")
        self.files: dict[str, bytes] = {}


class FakeDetails:
    """Act as the SDK details of a deleted browser."""

    def __init__(self, data: dict[str, Any]) -> None:
        """Keep the details.

        Args:
            data: The JSON form of the details.
        """
        self.data = data
        self.deleted_at = data.get("deleted_at")

    def to_dict(self, *, mode: str) -> dict[str, Any]:
        """Return the details as JSON data.

        Args:
            mode: The SDK dump mode.

        Returns:
            The details.
        """
        assert mode == "json"
        return dict(self.data)


class FakeResponse:
    """Act as an SDK process exec response."""

    def __init__(self, exit_code: int | None, stdout: str, stderr: str) -> None:
        """Encode the output as base64, like the API.

        Args:
            exit_code: The exit code, or ``None``.
            stdout: The stdout text.
            stderr: The stderr text.
        """
        self.exit_code = exit_code
        self.stdout_b64 = base64.b64encode(stdout.encode()).decode()
        self.stderr_b64 = base64.b64encode(stderr.encode()).decode()


class FakeBinary:
    """Act as an SDK binary response."""

    def __init__(self, data: bytes) -> None:
        """Keep the bytes.

        Args:
            data: The file content.
        """
        self.data = data

    async def iter_bytes(self, chunk_size: int) -> AsyncIterator[bytes]:
        """Yield the bytes in chunks.

        Args:
            chunk_size: The chunk size.

        Yields:
            One chunk.
        """
        for start in range(0, len(self.data), chunk_size):
            yield self.data[start : start + chunk_size]


class FakeState:
    """Hold the shared fake Kernel account state."""

    browsers: ClassVar[dict[str, FakeBrowser]] = {}
    create_calls: ClassVar[list[dict[str, Any]]] = []
    create_errors: ClassVar[list[BaseException]] = []
    create_delay: ClassVar[float] = 0.0
    leak_on_error: ClassVar[bool] = False
    cdp_ws_url: ClassVar[str] = "wss://hypeman.example.invalid/cdp"
    clients: ClassVar[list[dict[str, Any]]] = []
    execs: ClassVar[list[dict[str, Any]]] = []
    exec_responses: ClassVar[list[Any]] = []
    spawns: ClassVar[list[dict[str, Any]]] = []
    deleted: ClassVar[list[str]] = []
    retrieves: ClassVar[list[str]] = []
    retrieve_errors: ClassVar[list[BaseException]] = []
    delete_errors: ClassVar[list[BaseException]] = []
    list_error: ClassVar[BaseException | None] = None
    # Act as a stale list that does not show the live browsers yet.
    list_hidden: ClassVar[bool] = False
    usage_statuses: ClassVar[list[str]] = []
    uptime_ms: ClassVar[int] = 60_000
    closed: ClassVar[int] = 0
    # The real list pages by offset; a small page shows any skip.
    page_size: ClassVar[int] = 3
    _ids: ClassVar[Any] = itertools.count(1)

    @classmethod
    def reset(cls) -> None:
        """Clear the state between tests."""
        cls.browsers = {}
        cls.create_calls = []
        cls.create_errors = []
        cls.create_delay = 0.0
        cls.leak_on_error = False
        cls.cdp_ws_url = "wss://hypeman.example.invalid/cdp"
        cls.clients = []
        cls.execs = []
        cls.exec_responses = []
        cls.spawns = []
        cls.deleted = []
        cls.retrieves = []
        cls.retrieve_errors = []
        cls.delete_errors = []
        cls.list_error = None
        cls.list_hidden = False
        cls.usage_statuses = []
        cls.uptime_ms = 60_000
        cls.closed = 0

    @classmethod
    def new_browser(cls, options: Mapping[str, Any]) -> FakeBrowser:
        """Create and register one browser.

        Args:
            options: The create options.

        Returns:
            The browser.
        """
        browser = FakeBrowser(f"ses-{next(cls._ids)}", options, cls.cdp_ws_url)
        cls.browsers[browser.session_id] = browser
        return browser


def _interpret(browser: FakeBrowser, command: str, env: Mapping[str, str]) -> tuple[int, str, str]:
    """Interpret one command from the fake command set.

    Args:
        browser: The browser that runs the command.
        command: The shell command.
        env: The command environment.

    Returns:
        The exit code, stdout and stderr; 127 for an unknown command.
    """
    if command.startswith(("set -eu", "for i in")):
        return 0, "", ""
    argv = shlex.split(command)
    if not argv:
        return 0, "", ""
    name, args = argv[0], argv[1:]
    if name == "true":
        return 0, "", ""
    if name == "false":
        return 1, "", ""
    if name == "exit":
        return (int(args[0]) if args else 0), "", ""
    if name == "echo":
        return 0, " ".join(args) + "\n", ""
    if name == "mkdir":
        return 0, "", ""
    if name == "cat":
        missing = [path for path in args if path not in browser.files]
        if missing:
            return 1, "", f"cat: {missing[0]}: No such file or directory\n"
        return 0, b"".join(browser.files[path] for path in args).decode(), ""
    if name == "printenv" and len(args) == 1:
        value = env.get(args[0])
        return (1, "", "") if value is None else (0, value + "\n", "")
    if name == "docker":
        return 0, "", ""
    return 127, "", f"{name}: command not found\n"


class FakeBrowsersProcess:
    """Act as ``client.browsers.process``."""

    async def exec(self, session_id: str, **options: Any) -> Any:
        """Run one command, or return the next scripted response.

        Args:
            session_id: The browser id.
            **options: The SDK options.

        Returns:
            The exec response.

        Raises:
            BaseException: The next scripted error.
        """
        FakeState.execs.append({"session_id": session_id, **options})
        if FakeState.exec_responses:
            scripted = FakeState.exec_responses.pop(0)
            if isinstance(scripted, BaseException):
                raise scripted
            if callable(scripted):
                return await scripted()
            return scripted
        command = options["args"][1]
        return FakeResponse(*_interpret(FakeState.browsers[session_id], command, options.get("env") or {}))

    async def spawn(self, session_id: str, **options: Any) -> Any:
        """Record one spawn.

        Args:
            session_id: The browser id.
            **options: The SDK options.

        Returns:
            An object with a process id.
        """
        FakeState.spawns.append({"session_id": session_id, **options})
        return type("Spawned", (), {"process_id": "proc-1"})()


class FakeBrowsersFs:
    """Act as ``client.browsers.fs``."""

    async def write_file(self, session_id: str, contents: bytes, *, path: str) -> None:
        """Store one file.

        Args:
            session_id: The browser id.
            contents: The bytes.
            path: The path.
        """
        FakeState.browsers[session_id].files[path] = contents

    def __init__(self) -> None:
        """Expose only the streamed read, like the provider uses it."""
        self.with_streaming_response = FakeStreamingFs()


class FakeStreamingFs:
    """Act as ``client.browsers.fs.with_streaming_response``."""

    @contextlib.asynccontextmanager
    async def read_file(self, session_id: str, *, path: str) -> AsyncIterator[FakeBinary]:
        """Open one file as a streamed response.

        Args:
            session_id: The browser id.
            path: The path.

        Yields:
            The binary response.

        Raises:
            NotFoundError: The file does not exist.
        """
        files = FakeState.browsers[session_id].files
        if path not in files:
            raise _status_error(kernel.NotFoundError, 404)
        yield FakeBinary(files[path])


class FakeBrowsers:
    """Act as ``client.browsers``."""

    def __init__(self) -> None:
        """Create the sub-namespaces."""
        self.process = FakeBrowsersProcess()
        self.fs = FakeBrowsersFs()

    async def create(self, **options: Any) -> FakeBrowser:
        """Create a browser, or raise the next scripted error.

        Args:
            **options: The SDK options.

        Returns:
            The browser.

        Raises:
            BaseException: The next scripted create error.
        """
        FakeState.create_calls.append(options)
        if FakeState.create_delay:
            await asyncio.sleep(FakeState.create_delay)
        if FakeState.create_errors:
            error = FakeState.create_errors.pop(0)
            if FakeState.leak_on_error:
                FakeState.new_browser(options)
            raise error
        return FakeState.new_browser(options)

    async def list(self, *, tags: Mapping[str, str]) -> AsyncIterator[FakeBrowser]:
        """List the browsers whose tags match, one offset page at a time.

        Each page reads the live state, like the real offset pagination.

        Args:
            tags: The tags to match.

        Yields:
            Each match.

        Raises:
            BaseException: The scripted list error.
        """
        if FakeState.list_error is not None:
            raise FakeState.list_error
        if FakeState.list_hidden:
            return
        offset = 0
        while True:
            matches = [
                browser
                for browser in FakeState.browsers.values()
                if all(browser.tags.get(key) == value for key, value in tags.items())
            ]
            page = matches[offset : offset + FakeState.page_size]
            for browser in page:
                yield browser
            if len(page) < FakeState.page_size:
                return
            offset += FakeState.page_size

    async def delete_by_id(self, session_id: str) -> None:
        """Delete one browser.

        Args:
            session_id: The browser id.

        Raises:
            Exception: The next scripted delete error.
            NotFoundError: The browser does not exist.
        """
        if FakeState.delete_errors:
            raise FakeState.delete_errors.pop(0)
        if FakeState.browsers.pop(session_id, None) is None:
            raise _status_error(kernel.NotFoundError, 404)
        FakeState.deleted.append(session_id)

    async def retrieve(self, session_id: str, *, include_deleted: bool) -> FakeDetails:
        """Return the details of a deleted browser, or raise the next scripted error.

        Args:
            session_id: The browser id.
            include_deleted: Must be True to read a deleted browser.

        Returns:
            The details with the usage report.

        Raises:
            BaseException: The next scripted read error.
        """
        assert include_deleted is True
        FakeState.retrieves.append(session_id)
        if FakeState.retrieve_errors:
            raise FakeState.retrieve_errors.pop(0)
        status = FakeState.usage_statuses.pop(0) if FakeState.usage_statuses else "ready"
        return FakeDetails(
            {
                "session_id": session_id,
                "deleted_at": None if session_id in FakeState.browsers else "2026-11-24T00:05:00Z",
                "usage_status": status,
                "usage": {"uptime_ms": FakeState.uptime_ms},
            }
        )


class FakeKernel:
    """Act as ``kernel.AsyncKernel``."""

    def __init__(self, **options: Any) -> None:
        """Record the client options.

        Args:
            **options: The client options.
        """
        FakeState.clients.append(options)
        self.browsers = FakeBrowsers()

    async def close(self) -> None:
        """Record a close."""
        FakeState.closed += 1


@pytest.fixture(autouse=True)
def fake_sdk(monkeypatch: pytest.MonkeyPatch) -> type[FakeState]:
    """Replace the SDK client with the fake and set a fake key for every test.

    Args:
        monkeypatch: The pytest monkeypatch fixture.

    Returns:
        The fake state.
    """
    FakeState.reset()
    monkeypatch.setattr(kernel.KernelEnvironment, "client_cls", FakeKernel)
    monkeypatch.setenv(kernel.API_KEY_ENV, "test-key")
    return FakeState


def _spec(**overrides: Any) -> SandboxSpec:
    """Return a small spec.

    Args:
        **overrides: Spec fields to replace.

    Returns:
        The spec.
    """
    return SandboxSpec(**{"lifetime_seconds": 300, "resources": ResourceRequest(cpus=1), **overrides})


class TestKernelContract(SandboxEnvironmentContract):
    """Run the provider contract against the fake SDK."""

    provider_cls = kernel.KernelEnvironment


def test_factory_registers_kernel_as_dev_only() -> None:
    """Register the provider with the kernel extra and refuse it in production."""
    entry = factory.PROVIDERS["kernel"]
    assert entry.cls == "KernelEnvironment"
    assert entry.production_allowed is False


def test_preflight_needs_the_sdk_and_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail fast without the key or without the SDK."""
    kernel.KernelEnvironment.preflight()
    monkeypatch.delenv(kernel.API_KEY_ENV)
    with pytest.raises(SandboxFatalError):
        kernel.KernelEnvironment.preflight()
    monkeypatch.setattr(kernel.KernelEnvironment, "client_cls", None)
    with pytest.raises(SandboxFatalError):
        kernel.KernelEnvironment.preflight()


def test_capabilities_reject_more_than_eight_vcpus() -> None:
    """Refuse a spec above Harbor's eight CPUs."""
    with pytest.raises(SandboxSpecError):
        kernel.KernelEnvironment(_spec(resources=ResourceRequest(cpus=9)))


async def test_create_turns_off_sdk_retries_and_sends_the_spec() -> None:
    """Send max_retries=0, a headful browser, the tags and the clamped timeout."""
    spec = _spec(name="box-a", tags={JOB_TAG: "job-1"}, env={"A": "1"})
    async with kernel.KernelEnvironment(spec) as env:
        pass
    assert FakeState.clients[0]["max_retries"] == 0
    (call,) = FakeState.create_calls
    assert call["headless"] is False
    assert call["name"] == "box-a"
    assert call["timeout_seconds"] == 300
    assert call["tags"] == {JOB_TAG: "job-1", kernel.BOX_TAG: env._box_tag}
    assert "memory" not in call


@pytest.mark.parametrize(
    ("lifetime", "expected"),
    [(1, kernel.MIN_SESSION_TIMEOUT), (10**7, kernel.MAX_SESSION_TIMEOUT), (90.2, 91)],
)
async def test_create_clamps_the_session_timeout(lifetime: float, expected: int) -> None:
    """Keep the inactivity timeout inside Kernel's range."""
    async with kernel.KernelEnvironment(_spec(lifetime_seconds=lifetime)):
        pass
    assert FakeState.create_calls[0]["timeout_seconds"] == expected


@pytest.mark.parametrize(
    ("memory_mb", "container", "expected"),
    [(None, False, None), (1500, False, "2GiB"), (4096, False, "4GiB"), (None, True, "8GiB"), (9000, True, "16GiB")],
)
def test_memory_request_picks_the_smallest_size(memory_mb: int | None, container: bool, expected: str | None) -> None:
    """Map the memory request to the smallest Kernel size that holds it."""
    assert kernel._memory_request(memory_mb, container=container) == expected


def test_memory_request_above_sixteen_gib_fails() -> None:
    """Refuse a memory request that no Kernel size holds."""
    with pytest.raises(SandboxFatalError):
        kernel._memory_request(16385, container=False)


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(lambda: _status_error(kernel.RateLimitError, 429), id="429"),
        pytest.param(lambda: _status_error(kernel.APIStatusError, 502), id="502"),
        pytest.param(lambda: _status_error(kernel.APIStatusError, 503), id="503"),
    ],
)
async def test_capacity_errors_wait_with_no_attempt_limit(error: Callable[[], BaseException]) -> None:
    """Wait and create again on every capacity answer, past any fixed attempt count."""
    FakeState.create_errors = [error() for _ in range(12)]
    clock = FakeClock()
    async with kernel.KernelEnvironment(_spec(), clock=clock) as env:
        assert (await env.exec("true")).ok
    assert len(FakeState.create_calls) == 13
    assert len(clock.sleeps) >= 12


async def test_capacity_error_maps_to_provider_at_capacity() -> None:
    """Raise the shared capacity error type for a 503."""
    env = kernel.KernelEnvironment(_spec())
    env._client = FakeKernel()
    FakeState.create_errors = [_status_error(kernel.APIStatusError, 503)]
    with pytest.raises(ProviderAtCapacityError):
        await env._create(None, env._create_options())


async def test_other_create_errors_are_not_retried() -> None:
    """Send the create once and raise a non-capacity error."""
    FakeState.create_errors = [_status_error(kernel.APIStatusError, 400)]
    with pytest.raises(kernel.APIStatusError):
        async with kernel.KernelEnvironment(_spec(), clock=FakeClock()):
            pass
    assert len(FakeState.create_calls) == 1


async def test_sdk_timeout_on_create_raises_start_timeout() -> None:
    """Map the SDK timeout of the create call to StartTimeoutError."""
    FakeState.create_errors = [kernel.APITimeoutError(_REQUEST)]
    with pytest.raises(StartTimeoutError):
        await kernel.KernelEnvironment(_spec()).start()
    assert len(FakeState.create_calls) == 1


async def test_create_timeout_sweeps_a_late_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    """Raise StartTimeoutError for a slow create and delete a browser that appeared late."""
    monkeypatch.setattr(kernel, "CREATE_TIMEOUT", 0.01)
    FakeState.create_errors = [TimeoutError()]
    FakeState.leak_on_error = True
    FakeState.create_delay = 0.05
    with pytest.raises(StartTimeoutError):
        await kernel.KernelEnvironment(_spec(name="slow")).start()
    assert FakeState.browsers == {}
    assert FakeState.closed == 1


async def test_failed_create_sweeps_a_leaked_browser() -> None:
    """Delete a browser that a failed create left behind."""
    FakeState.create_errors = [_status_error(kernel.APIStatusError, 500)]
    FakeState.leak_on_error = True
    with pytest.raises(kernel.APIStatusError):
        await kernel.KernelEnvironment(_spec(name="leaky")).start()
    assert FakeState.browsers == {}


async def test_stop_during_start_raises_infra_error() -> None:
    """End a slow start with a stop and leave no browser."""
    FakeState.create_delay = 0.05
    env = kernel.KernelEnvironment(_spec())
    start = asyncio.ensure_future(env.start())
    await asyncio.sleep(0.01)
    await env.stop()
    with pytest.raises(SandboxInfraError):
        await start
    assert FakeState.browsers == {}


async def test_exec_sends_the_connection_env_and_no_api_key() -> None:
    """Give the command the browser URLs and the spec env, never the API key."""
    async with kernel.KernelEnvironment(_spec(env={"A": "1"})) as env:
        await env.exec("true", cwd="/work", env={"B": "2"}, timeout_sec=5, sudo=True)
    call = FakeState.execs[-1]
    assert call["command"] == "bash"
    assert call["args"] == ["-lc", "true"]
    assert call["cwd"] == "/work"
    assert call["as_root"] is True
    assert call["timeout_sec"] == 5
    assert call["timeout"] == 5 + kernel.EXEC_HTTP_MARGIN
    assert call["env"]["A"] == "1"
    assert call["env"]["B"] == "2"
    assert call["env"]["KERNEL_SESSION_ID"] == call["session_id"]
    assert call["env"]["KERNEL_CDP_WS_URL"].startswith("wss://")
    assert kernel.API_KEY_ENV not in call["env"]
    assert "test-key" not in call["env"].values()


async def test_exec_without_timeout_sends_no_limit() -> None:
    """Leave out the command limit and turn off the SDK HTTP timeout when the caller sets none."""
    async with kernel.KernelEnvironment(_spec()) as env:
        await env.exec("true")
    call = FakeState.execs[-1]
    assert "timeout_sec" not in call
    assert call["timeout"] is None


async def test_exec_decodes_bad_utf8_with_replacement() -> None:
    """Replace bytes that are not UTF-8 instead of failing."""
    response = FakeResponse(0, "", "")
    response.stdout_b64 = base64.b64encode(b"ok\xff").decode()
    async with kernel.KernelEnvironment(_spec()) as env:
        FakeState.exec_responses = [response]
        result = await env.exec("x")
    assert result.stdout == "ok�"


async def test_missing_exit_code_with_timeout_is_a_timeout() -> None:
    """Treat a missing exit code after a limited command as a timeout."""
    async with kernel.KernelEnvironment(_spec()) as env:
        FakeState.exec_responses = [FakeResponse(None, "partial", "")]
        result = await env.exec("sleep 99", timeout_sec=1)
        FakeState.exec_responses = [FakeResponse(None, "", "")]
        unlimited = await env.exec("true")
    assert result.timed_out
    assert result.exit_code == kernel.TIMEOUT_EXIT_CODE
    assert result.stdout == "partial"
    assert unlimited.ok


async def test_sdk_timeout_on_exec_is_a_timeout() -> None:
    """Map the SDK timeout of a limited command to a timed-out result."""
    async with kernel.KernelEnvironment(_spec()) as env:
        FakeState.exec_responses = [kernel.APITimeoutError(_REQUEST)]
        result = await env.exec("sleep 99", timeout_sec=1)
    assert result.timed_out


async def test_client_side_timeout_on_exec_is_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop waiting after the command limit plus the margin."""
    monkeypatch.setattr(kernel, "EXEC_HTTP_MARGIN", 0.01)

    async def hang() -> None:
        """Never answer."""
        await asyncio.Event().wait()

    async with kernel.KernelEnvironment(_spec()) as env:
        FakeState.exec_responses = [hang]
        result = await env.exec("sleep 99", timeout_sec=0.01)
    assert result.timed_out
    assert result.exit_code == kernel.TIMEOUT_EXIT_CODE


async def test_exec_api_errors_are_infra_errors_and_not_retried() -> None:
    """Raise SandboxInfraError for an API error and send the exec once."""
    async with kernel.KernelEnvironment(_spec()) as env:
        before = len(FakeState.execs)
        FakeState.exec_responses = [_status_error(kernel.APIStatusError, 503)]
        with pytest.raises(SandboxInfraError):
            await env.exec("true")
        assert len(FakeState.execs) == before + 1


async def test_exec_after_stop_raises_infra_error() -> None:
    """Refuse commands after the stop."""
    env = kernel.KernelEnvironment(_spec())
    await env.start()
    await env.stop()
    with pytest.raises(SandboxInfraError):
        await env.exec("true")


async def test_upload_creates_the_parent_directory() -> None:
    """Run mkdir -p on the parent as root before the write."""
    async with kernel.KernelEnvironment(_spec()) as env:
        await env.upload_files({"/deep/dir/a.txt": "x"})
        mkdir = FakeState.execs[-1]
        assert mkdir["args"][1] == "mkdir -p /deep/dir"
        assert mkdir["as_root"] is True
        assert await env.download_file("/deep/dir/a.txt") == "x"


async def test_download_above_the_limit_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse a file above the download limit."""
    monkeypatch.setattr(kernel, "MAX_DOWNLOAD_BYTES", 4)
    async with kernel.KernelEnvironment(_spec()) as env:
        await env.upload_files({"/tmp/big.txt": "12345"})
        with pytest.raises(SandboxInfraError):
            await env.download_file("/tmp/big.txt")


async def test_stop_sweeps_other_browsers_with_the_box_tag() -> None:
    """Delete the known browser and any other browser with the same box tag."""
    env = kernel.KernelEnvironment(_spec(name="box-b"))
    await env.start()
    FakeState.new_browser({"name": "box-b", "tags": {kernel.BOX_TAG: env._box_tag}})
    FakeState.new_browser({"name": "box-b", "tags": {kernel.BOX_TAG: "another-box"}})
    await env.stop()
    assert len(FakeState.browsers) == 1
    assert len(FakeState.deleted) == 2
    assert FakeState.closed == 1


async def test_stop_job_sandboxes_without_sdk_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Return 0 when the SDK is not installed."""
    monkeypatch.setattr(kernel.KernelEnvironment, "client_cls", None)
    assert await kernel.KernelEnvironment.stop_job_sandboxes("job") == 0


async def test_container_mode_installs_docker_and_runs_the_image() -> None:
    """Install the pinned Docker, spawn dockerd and run the image with env names only."""
    spec = _spec(name="ctr", image="python:3.12", env={"SECRET_NAME": "value-1"}, resources=ResourceRequest(cpus=2))
    async with kernel.KernelEnvironment(spec) as env:
        assert FakeState.create_calls[0]["memory"] == "8GiB"
        install, wait, run = (call["args"][1] for call in FakeState.execs[:3])
        assert kernel.DOCKER_ARCHIVE_SHA256 in install
        assert "fuse-overlayfs" in wait
        (spawn,) = FakeState.spawns
        assert spawn["as_root"] is True
        assert "exec dockerd" in spawn["args"][1]
        assert shlex.split(run)[:4] == ["docker", "run", "-d", "--name"]
        assert "value-1" not in run
        assert FakeState.execs[2]["env"] == {"SECRET_NAME": "value-1"}
        await env.exec("id", cwd="/app", env={"B": "2"}, sudo=True)
        command = FakeState.execs[-1]["args"][1]
        assert shlex.split(command) == [
            "docker",
            "exec",
            "-u",
            "root",
            "-w",
            "/app",
            "-e",
            "B",
            "ctr",
            "sh",
            "-lc",
            "id",
        ]
        assert FakeState.execs[-1]["env"] == {"B": "2"}


async def test_container_mode_upload_and_download_use_docker_cp() -> None:
    """Move files through a VM temp file and docker cp."""
    async with kernel.KernelEnvironment(_spec(name="ctr", image="img")) as env:
        await env.upload_files({"/app/a.txt": "alpha"})
        assert "docker cp" in FakeState.execs[-1]["args"][1]
        FakeState.exec_responses = [FakeResponse(1, "", "")]
        assert await env.download_file("/app/missing.txt") is None


async def test_container_mode_needs_a_hypeman_browser() -> None:
    """Fail the start once and delete the browser when it is not Hypeman-backed."""
    FakeState.cdp_ws_url = "wss://other.example.invalid/cdp"
    with pytest.raises(SandboxInfraError, match="Hypeman"):
        await kernel.KernelEnvironment(_spec(image="img")).start()
    assert len(FakeState.create_calls) == 1
    assert FakeState.browsers == {}


async def test_container_mode_failed_install_stops_the_browser() -> None:
    """Raise SandboxInfraError and delete the browser when the Docker install fails."""
    FakeState.exec_responses = [FakeResponse(1, "", "no network")]
    with pytest.raises(SandboxInfraError, match="install"):
        await kernel.KernelEnvironment(_spec(image="img")).start()
    assert FakeState.browsers == {}


async def test_job_sweep_deletes_browsers_on_every_page() -> None:
    """Delete every tagged browser, also past the first list page."""
    for index in range(7):
        FakeState.new_browser({"name": f"job-{index}", "tags": {JOB_TAG: "job-7"}})
    FakeState.new_browser({"name": "other", "tags": {JOB_TAG: "job-8"}})
    assert await kernel.KernelEnvironment.stop_job_sandboxes("job-7") == 7
    assert [b.name for b in FakeState.browsers.values()] == ["other"]


async def test_stop_sweep_deletes_box_browsers_on_every_page() -> None:
    """Delete every leaked browser with the box tag, also past the first list page."""
    env = kernel.KernelEnvironment(_spec(name="box-p"))
    await env.start()
    for _ in range(5):
        FakeState.new_browser({"name": "box-p", "tags": {kernel.BOX_TAG: env._box_tag}})
    await env.stop()
    assert FakeState.browsers == {}
    assert len(FakeState.deleted) == 6


class _GateBucket:
    """Block each acquire until the gate opens."""

    def __init__(self) -> None:
        """Start with the gate open."""
        self.gate = asyncio.Event()
        self.gate.set()

    async def acquire(self) -> None:
        """Wait for the gate."""
        await self.gate.wait()


class _GatePacing:
    """Hold gated read and create buckets."""

    def __init__(self) -> None:
        """Create the buckets."""
        self.reads = _GateBucket()
        self.creates = _GateBucket()


@pytest.mark.parametrize(
    "operation",
    [
        lambda env: env.exec("true"),
        lambda env: env.upload_files({"/tmp/a.txt": "x"}),
        lambda env: env.download_file("/tmp/a.txt"),
    ],
    ids=["exec", "upload", "download"],
)
async def test_stop_during_read_wait_raises_infra_error(operation: Callable[[Any], Awaitable[Any]]) -> None:
    """Raise SandboxInfraError when a stop ends the browser while a call waits for the read bucket."""
    pacing = _GatePacing()
    env = kernel.KernelEnvironment(_spec(), pacing=pacing)  # type: ignore[arg-type]
    await env.start()
    pacing.reads.gate.clear()
    task = asyncio.ensure_future(operation(env))
    await asyncio.sleep(0)
    await env.stop()
    pacing.reads.gate.set()
    with pytest.raises(SandboxInfraError, match="not running"):
        await task


class _MockKernel:
    """Serve a small Kernel API over ``httpx.MockTransport`` for the real SDK."""

    def __init__(self, file_bytes: bytes = b"") -> None:
        """Start with no browsers.

        Args:
            file_bytes: The content of the file ``/big``.
        """
        self.file_bytes = file_bytes
        self.consumed = 0
        self.timeouts: dict[str, Any] = {}
        self.browsers: list[str] = []

    def client_cls(self, **options: Any) -> Any:
        """Create a real SDK client on the mock transport.

        Args:
            **options: The provider's client options.

        Returns:
            The SDK client.
        """
        return kernel.AsyncKernel(
            api_key="test",
            max_retries=options["max_retries"],
            base_url="https://kernel.example.invalid",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.handle)),
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Answer one SDK request.

        Args:
            request: The request.

        Returns:
            The response.
        """
        path = request.url.path
        self.timeouts[f"{request.method} {path}"] = request.extensions.get("timeout")
        browser = {
            "session_id": "ses-real",
            "cdp_ws_url": "wss://cdp.example.invalid",
            "created_at": "2026-01-01T00:00:00Z",
            "headless": False,
            "stealth": False,
            "timeout_seconds": 300,
        }
        if request.method == "POST" and path == "/browsers":
            self.browsers.append("ses-real")
            return httpx.Response(200, json=browser)
        if request.method == "GET" and path == "/browsers":
            return httpx.Response(200, json=[{**browser, "session_id": sid} for sid in self.browsers])
        if request.method == "DELETE" and path.startswith("/browsers/"):
            self.browsers.remove(path.rsplit("/", 1)[1])
            return httpx.Response(204)
        if path.endswith("/process/exec"):
            return httpx.Response(200, json={"exit_code": 0, "stdout_b64": "", "stderr_b64": ""})
        if path.endswith("/fs/read_file") and request.url.params.get("path") == "/big":
            return httpx.Response(
                200, stream=_CountingStream(self), headers={"content-type": "application/octet-stream"}
            )
        return httpx.Response(404, content=json.dumps({"code": "not_found", "message": "missing"}))


class _CountingStream(httpx.AsyncByteStream):
    """Stream the mock file in 64 KiB chunks and count the bytes sent."""

    def __init__(self, server: _MockKernel) -> None:
        """Keep the server.

        Args:
            server: The mock server.
        """
        self.server = server

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Yield the file in chunks.

        Yields:
            One chunk.
        """
        data = self.server.file_bytes
        for start in range(0, len(data), 65536):
            chunk = data[start : start + 65536]
            self.server.consumed += len(chunk)
            yield chunk


async def test_real_sdk_download_stops_reading_at_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop the streamed body at the limit, and return None for a missing file, with the real SDK."""
    server = _MockKernel(file_bytes=b"a" * (16 * 1024 * 1024))
    monkeypatch.setattr(kernel.KernelEnvironment, "client_cls", server.client_cls)
    monkeypatch.setattr(kernel, "MAX_DOWNLOAD_BYTES", 1024 * 1024)
    async with kernel.KernelEnvironment(_spec()) as env:
        with pytest.raises(SandboxInfraError, match="limit"):
            await env.download_file("/big")
        assert await env.download_file("/missing") is None
    assert server.consumed <= 4 * 1024 * 1024
    assert server.browsers == []


async def test_real_sdk_exec_without_timeout_has_no_read_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Send a no-limit exec with no HTTP read timeout, and a limited exec with the margin, with the real SDK."""
    server = _MockKernel()
    monkeypatch.setattr(kernel.KernelEnvironment, "client_cls", server.client_cls)
    async with kernel.KernelEnvironment(_spec()) as env:
        await env.exec("sleep 600")
        unlimited = server.timeouts["POST /browsers/ses-real/process/exec"]
        await env.exec("sleep 1", timeout_sec=5)
        limited = server.timeouts["POST /browsers/ses-real/process/exec"]
    assert unlimited["read"] is None
    assert limited["read"] == 5 + kernel.EXEC_HTTP_MARGIN


class FakeUsage:
    """Record the calls that a protected box makes on its usage reservation."""

    def __init__(self, request: dict[str, Any], operation_key: str, billing_error: Exception | None = None) -> None:
        """Keep the funded request and start with the reserve call.

        Args:
            request: The usage request that the box reserved.
            operation_key: The operation key of the box.
            billing_error: The error that ``settle`` and ``fail`` raise, or ``None``.
        """
        self.request = request
        self.operation_key = operation_key
        self.billing_error = billing_error
        self.calls: list[str] = ["reserve"]
        self.browser: Any = None
        self.receipt: dict[str, Any] | None = None
        self.measured_ms: int | None = None
        self.error: BaseException | None = None
        self.no_browser: bool | None = None

    def confirm(self, browser: Any) -> None:
        """Record the confirm call.

        Args:
            browser: The created browser.
        """
        self.calls.append("confirm")
        self.browser = browser

    def record(self, receipt: dict[str, Any], *, measured_ms: int | None) -> None:
        """Record the usage evidence.

        Args:
            receipt: Kernel's details of the deleted browser.
            measured_ms: The measured wall time.
        """
        self.calls.append("record")
        self.receipt = receipt
        self.measured_ms = measured_ms

    def settle(self) -> None:
        """Record the settle call, or raise the scripted error.

        Raises:
            Exception: The scripted settle error.
        """
        self.calls.append("settle")
        if self.billing_error is not None:
            raise self.billing_error

    def fail(self, error: BaseException, *, no_browser: bool) -> None:
        """Record the fail call, or raise the scripted error.

        Args:
            error: The create failure.
            no_browser: Whether the tag sweep found no browser.

        Raises:
            Exception: The scripted billing error.
        """
        self.calls.append("fail")
        self.error = error
        self.no_browser = no_browser
        if self.billing_error is not None:
            raise self.billing_error

    def pending(self) -> None:
        """Record the pending call."""
        self.calls.append("pending")

    def release(self, reason: str) -> None:
        """Record the release call.

        Args:
            reason: Why the create call did not go out.
        """
        self.calls.append("release")


def _protected(
    spec: SandboxSpec | None = None,
    *,
    billing_error: Exception | None = None,
    reserve_delay: float = 0.0,
    **options: Any,
) -> tuple[kernel.KernelEnvironment, list[FakeUsage]]:
    """Return a protected box and the list that collects its usage fakes.

    Args:
        spec: The spec, or ``None`` for a small spec with an operation key.
        billing_error: The error that the fake settle and fail raise.
        reserve_delay: Seconds that the reserve thread blocks.
        **options: More provider options.

    Returns:
        The box and the usage list.
    """
    usages: list[FakeUsage] = []

    def usage_factory(budget: Any, request: dict[str, Any], *, operation_key: str) -> FakeUsage:
        """Make one recording usage fake.

        Args:
            budget: The budget runtime.
            request: The usage request.
            operation_key: The operation key.

        Returns:
            The fake.
        """
        assert budget == "budget"
        time.sleep(reserve_delay)
        usages.append(FakeUsage(request, operation_key, billing_error))
        return usages[-1]

    env = kernel.KernelEnvironment(
        spec or _spec(operation_key="box-op"), budget="budget", usage_factory=usage_factory, **options
    )
    return env, usages


def test_protected_box_is_billable_and_needs_an_operation_key() -> None:
    """Mark the provider billable, and refuse a protected box with no operation key."""
    assert kernel.KERNEL_CAPABILITIES.billable is True
    assert kernel.KernelEnvironment.capabilities.protected is False
    env, _ = _protected()
    assert env.capabilities.protected is True
    with pytest.raises(SandboxSpecError):
        kernel.KernelEnvironment(_spec(), budget="budget")


async def test_protected_box_reserves_confirms_and_settles_on_stop() -> None:
    """Reserve before the create, confirm after it, and settle from Kernel's report after the delete."""
    env, usages = _protected(clock=FakeClock())
    async with env:
        (usage,) = usages
        assert usage.calls == ["reserve", "confirm"]
        assert FakeState.create_calls[0]["memory"] == "8GiB"
    assert usage.operation_key == "box-op"
    assert usage.request == {
        "lifetime_ms": 300_000,
        "timeout_seconds": 300,
        "memory_gib": 8,
        "headless": False,
        "gpu": False,
    }
    assert usage.calls == ["reserve", "confirm", "record", "settle"]
    assert usage.receipt is not None
    assert usage.receipt["usage"] == {"uptime_ms": 60_000}
    assert usage.measured_ms is not None
    assert FakeState.retrieves == FakeState.deleted == [usage.browser.session_id]


async def test_protected_box_settles_after_a_failed_start() -> None:
    """Delete the browser and settle when the container setup fails."""
    FakeState.exec_responses = [FakeResponse(1, "", "no network")]
    env, usages = _protected(_spec(image="img", operation_key="box-op"))
    with pytest.raises(SandboxInfraError, match="install"):
        await env.start()
    assert usages[0].calls == ["reserve", "confirm", "record", "settle"]
    assert FakeState.browsers == {}


async def test_protected_box_settles_after_a_cancelled_start() -> None:
    """Delete the browser and settle when the caller cancels the start after the create."""
    entered = asyncio.Event()

    async def hang() -> Any:
        """Never answer the exec call.

        Returns:
            Nothing; the call waits until it is cancelled.
        """
        entered.set()
        await asyncio.Event().wait()

    FakeState.exec_responses = [hang]
    env, usages = _protected(_spec(image="img", operation_key="box-op"))
    start = asyncio.ensure_future(env.start())
    await entered.wait()
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start
    assert usages[0].calls == ["reserve", "confirm", "record", "settle"]
    assert FakeState.browsers == {}


async def test_stop_during_a_protected_create_fails_the_reservation() -> None:
    """Pass the reservation to ``fail`` when the stop ends the start before a browser exists."""
    FakeState.create_delay = 0.05
    env, usages = _protected()
    start = asyncio.ensure_future(env.start())
    await asyncio.sleep(0.01)
    await env.stop()
    with pytest.raises(SandboxInfraError):
        await start
    assert usages[0].calls == ["reserve", "fail"]
    assert usages[0].no_browser is True


async def test_cancel_during_the_reserve_releases_the_hold() -> None:
    """Release the hold when the start is cancelled before the create call goes out."""
    env, usages = _protected(reserve_delay=0.05)
    start = asyncio.ensure_future(env.start())
    await asyncio.sleep(0.01)
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start
    assert FakeState.create_calls == []
    assert usages[0].calls == ["reserve", "release"]


async def test_failed_create_with_one_swept_browser_settles_it() -> None:
    """Bind and settle the one browser that a failed create left, at the measured time."""
    FakeState.create_errors = [_status_error(kernel.APIStatusError, 500)]
    FakeState.leak_on_error = True
    env, usages = _protected(_spec(name="leaky", operation_key="box-op"))
    with pytest.raises(kernel.APIStatusError):
        await env.start()
    usage = usages[0]
    assert usage.calls == ["reserve", "confirm", "record", "settle"]
    assert usage.browser.session_id == FakeState.deleted[0]
    assert usage.measured_ms is not None
    assert FakeState.browsers == {}


async def test_failed_create_with_two_swept_browsers_keeps_the_hold() -> None:
    """Keep the hold when the sweep finds more than one browser, because the funded one is unknown."""
    FakeState.create_errors = [_status_error(kernel.APIStatusError, 500)]
    FakeState.leak_on_error = True
    env, usages = _protected(_spec(name="twin", operation_key="box-op"))
    FakeState.new_browser({"name": "twin", "tags": {kernel.BOX_TAG: env._box_tag}})
    with pytest.raises(kernel.APIStatusError):
        await env.start()
    assert usages[0].calls == ["reserve", "fail"]
    assert usages[0].no_browser is False


def test_protected_box_refuses_more_than_the_priced_memory() -> None:
    """Refuse a protected spec above 8 GiB, because Kernel publishes no price for a larger browser."""
    with pytest.raises(SandboxSpecError, match="8 GiB"):
        _protected(_spec(operation_key="box-op", resources=ResourceRequest(cpus=1, memory_mb=8 * 1024 + 1)))


async def test_late_stop_of_an_ended_browser_bills_no_extra_time() -> None:
    """Cap the measured time at the funded lifetime plus one inactivity timeout when Kernel already ended the browser."""
    FakeState.retrieve_errors = [_status_error(kernel.APIStatusError, 500) for _ in range(40)]
    clock = FakeClock()
    env, usages = _protected(clock=clock)
    await env.start()
    FakeState.browsers.clear()
    clock.now += 10_000
    await env.stop()
    usage = usages[0]
    assert usage.calls == ["reserve", "confirm", "record", "settle"]
    assert usage.receipt == {}
    # 300 s funded lifetime + 300 s inactivity timeout + 120 s stop margin.
    assert usage.measured_ms == 720_000


async def test_failed_delete_that_went_through_is_settled() -> None:
    """Settle the known browser when its delete fails but the finished sweep no longer lists it."""
    clock = FakeClock()
    env, usages = _protected(clock=clock)
    await env.start()
    clock.now += 30
    FakeState.browsers.clear()
    FakeState.delete_errors = [_status_error(kernel.APIStatusError, 500)]
    await env.stop()
    usage = usages[0]
    assert usage.calls == ["reserve", "confirm", "record", "settle"]
    assert usage.measured_ms == 30_000


async def test_stale_list_after_a_failed_delete_keeps_the_hold() -> None:
    """Keep the hold when the list leaves out a browser that Kernel does not report as deleted."""
    env, usages = _protected(clock=FakeClock())
    await env.start()
    FakeState.list_hidden = True
    FakeState.delete_errors = [_status_error(kernel.APIStatusError, 500)]
    with pytest.raises(UsagePendingError):
        await env.stop()
    assert usages[0].calls == ["reserve", "confirm", "pending"]


async def test_not_found_retrieve_after_a_failed_delete_keeps_the_hold() -> None:
    """Keep the hold when a stale list leaves out the browser and its retrieve raises NotFound."""
    env, usages = _protected(clock=FakeClock())
    await env.start()
    FakeState.list_hidden = True
    FakeState.delete_errors = [_status_error(kernel.APIStatusError, 500)]
    FakeState.retrieve_errors = [_status_error(kernel.NotFoundError, 404)]
    with pytest.raises(UsagePendingError):
        await env.stop()
    assert usages[0].calls == ["reserve", "confirm", "pending"]
    assert usages[0].measured_ms is None


async def test_second_failed_delete_keeps_the_hold() -> None:
    """Keep the hold and raise from stop when the first delete and the sweep delete both fail."""
    env, usages = _protected(clock=FakeClock())
    await env.start()
    FakeState.delete_errors = [_status_error(kernel.APIStatusError, 500) for _ in range(2)]
    with pytest.raises(UsagePendingError):
        await env.stop()
    assert usages[0].calls == ["reserve", "confirm", "pending"]
    assert usages[0].measured_ms is None


async def test_failed_list_after_a_failed_delete_keeps_the_hold() -> None:
    """Keep the hold with no measured time when the sweep list fails after the delete failed."""
    env, usages = _protected(clock=FakeClock())
    await env.start()
    FakeState.delete_errors = [_status_error(kernel.APIStatusError, 500)]
    FakeState.list_error = _status_error(kernel.APIStatusError, 500)
    with pytest.raises(UsagePendingError):
        await env.stop()
    assert usages[0].calls == ["reserve", "confirm", "pending"]
    assert usages[0].measured_ms is None


async def test_failed_create_browser_gets_the_measured_time_cap() -> None:
    """Cap the measured time of a browser that a failed create left, as for a created one."""
    FakeState.create_errors = [_status_error(kernel.APIStatusError, 500)]
    clock = FakeClock()
    env, _ = _protected(clock=clock)
    with pytest.raises(kernel.APIStatusError):
        await env.start()
    clock.now += 10_000
    # 300 s funded lifetime + 300 s inactivity timeout + 120 s stop margin.
    assert env._measured_ms() == 720_000


async def test_measured_time_cap_ignores_float_clock_error() -> None:
    """Cap a late stop at the integer reservation bound when the clock base is not a whole number."""
    clock = FakeClock()
    # At this base, funded end minus create time is 300.0000000000009 s.
    clock.now = 8184.81
    env, usages = _protected(clock=clock)
    async with env:
        clock.now += 10_000
    # 300 s funded lifetime + 300 s inactivity timeout + 120 s stop margin.
    assert usages[0].measured_ms == 720_000


async def test_second_start_is_refused() -> None:
    """Refuse a second start, so no second browser runs on the first reservation."""
    env, usages = _protected()
    await env.start()
    with pytest.raises(SandboxInfraError, match="already started"):
        await env.start()
    assert len(FakeState.create_calls) == 1
    assert len(usages) == 1
    await env.stop()


async def test_job_sweep_counts_an_already_ended_browser_as_gone() -> None:
    """Count a job browser whose delete finds that Kernel already ended it."""
    FakeState.new_browser({"name": "job-0", "tags": {JOB_TAG: "job-9"}})
    FakeState.delete_errors = [_status_error(kernel.NotFoundError, 404)]
    assert await kernel.KernelEnvironment.stop_job_sandboxes("job-9") == 1


async def test_sweep_counts_an_already_ended_browser_as_gone() -> None:
    """Settle the one swept browser when its delete finds that Kernel already ended it."""
    FakeState.create_errors = [_status_error(kernel.APIStatusError, 500)]
    FakeState.leak_on_error = True
    env, usages = _protected()
    FakeState.delete_errors = [_status_error(kernel.NotFoundError, 404)]
    with pytest.raises(kernel.APIStatusError):
        await env.start()
    assert usages[0].calls == ["reserve", "confirm", "record", "settle"]
    assert usages[0].measured_ms is not None


async def test_boxes_with_one_name_do_not_sweep_each_other() -> None:
    """Keep and leave unbilled the browser of another box that shares ``spec.name``."""
    other, _ = _protected(_spec(name="same", operation_key="box-a"))
    await other.start()
    FakeState.create_errors = [_status_error(kernel.APIStatusError, 500)]
    env, usages = _protected(_spec(name="same", operation_key="box-b"))
    with pytest.raises(kernel.APIStatusError):
        await env.start()
    assert usages[0].calls == ["reserve", "fail"]
    assert usages[0].no_browser is True
    assert len(FakeState.browsers) == 1
    await other.stop()
    assert FakeState.browsers == {}


async def test_failed_fail_is_raised_from_stop_once() -> None:
    """Raise a pending-usage error from the stop after a failed create whose fail call also failed."""
    FakeState.create_errors = [_status_error(kernel.RateLimitError, 429)]
    env, _ = _protected(billing_error=RuntimeError("database detail"))
    with pytest.raises(kernel.RateLimitError):
        await env.start()
    with pytest.raises(UsagePendingError):
        await env.stop()
    await env.stop()


async def test_protected_box_does_not_wait_for_capacity() -> None:
    """Fail the start on a capacity answer, so no reservation waits for capacity."""
    FakeState.create_errors = [_status_error(kernel.RateLimitError, 429)]
    clock = FakeClock()
    env, usages = _protected(clock=clock)
    with pytest.raises(kernel.RateLimitError):
        await env.start()
    assert len(FakeState.create_calls) == 1
    assert clock.sleeps == []
    assert usages[0].calls == ["reserve", "fail"]
    assert isinstance(usages[0].error, kernel.RateLimitError)
    assert usages[0].no_browser is True


async def test_usage_read_failure_settles_from_the_measured_time() -> None:
    """Pass an empty report and the measured time when every usage read fails."""
    FakeState.retrieve_errors = [_status_error(kernel.APIStatusError, 500) for _ in range(40)]
    clock = FakeClock()
    env, usages = _protected(clock=clock)
    async with env:
        clock.now += 42
    usage = usages[0]
    assert usage.calls == ["reserve", "confirm", "record", "settle"]
    assert usage.receipt == {}
    assert usage.measured_ms == 42_000
    assert len(FakeState.retrieves) == int(kernel.USAGE_WAIT / kernel.USAGE_POLL)


def _clock_wait_for(monkeypatch: pytest.MonkeyPatch, clock: FakeClock) -> list[tuple[str, float | None]]:
    """Make ``asyncio.wait_for`` time out on the fake clock, and record each timeout.

    Args:
        monkeypatch: Replaces ``asyncio.wait_for`` for the test.
        clock: The fake clock of the box.

    Returns:
        The coroutine name and the timeout of each call, in order.
    """
    calls: list[tuple[str, float | None]] = []

    async def wait_for(awaitable: Any, seconds: float | None) -> Any:
        """Await the work, then time out when it used more fake time than allowed.

        Args:
            awaitable: The coroutine to await.
            seconds: The allowed fake seconds.

        Returns:
            The result of the work.

        Raises:
            TimeoutError: The work used up the timeout; the clock stops at the timeout.
        """
        calls.append((awaitable.cr_code.co_name, seconds))
        begin = clock.now
        result = await awaitable
        if seconds is not None and clock.now - begin >= seconds:
            clock.now = begin + seconds
            raise TimeoutError
        return result

    monkeypatch.setattr(kernel.asyncio, "wait_for", wait_for)
    return calls


def _reads(calls: list[tuple[str, float | None]], name: str) -> list[float | None]:
    """Return the timeouts of the recorded calls to one coroutine.

    Args:
        calls: The recorded calls.
        name: The coroutine name.

    Returns:
        The timeouts, in order.
    """
    return [timeout for called, timeout in calls if called == name]


async def test_slow_usage_reads_stop_at_the_usage_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give each slow usage read only the time left, and end all reads at USAGE_WAIT.

    Args:
        monkeypatch: Replaces the browser read and ``asyncio.wait_for``.
    """
    clock = FakeClock()
    calls = _clock_wait_for(monkeypatch, clock)

    async def slow_retrieve(self: FakeBrowsers, session_id: str, *, include_deleted: bool) -> FakeDetails:
        """Use up 20 fake seconds and report the usage as not ready.

        Args:
            self: The fake browsers resource.
            session_id: The browser id.
            include_deleted: Must be True to read a deleted browser.

        Returns:
            Details with a pending usage report.
        """
        clock.now += 20
        return FakeDetails({"session_id": session_id, "usage_status": "pending"})

    monkeypatch.setattr(FakeBrowsers, "retrieve", slow_retrieve)
    env, usages = _protected(clock=clock)
    await env.start()
    start = clock.now
    await env.stop()
    # Read 1 gets the full 30 s and uses 20; after one 2 s poll, read 2 gets the 8 s left.
    assert _reads(calls, "slow_retrieve") == [min(kernel.STOP_TIMEOUT, kernel.USAGE_WAIT), 8]
    assert clock.now - start <= kernel.USAGE_WAIT
    assert usages[0].calls == ["reserve", "confirm", "record", "settle"]


async def test_slow_bucket_wait_stays_inside_the_usage_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bound the read bucket wait by the same USAGE_WAIT deadline as the reads.

    Args:
        monkeypatch: Replaces ``asyncio.wait_for``.
    """
    FakeState.usage_statuses = ["pending"] * 5
    clock = FakeClock()
    calls = _clock_wait_for(monkeypatch, clock)
    pacing = _ClockPacing(clock)
    env, usages = _protected(clock=clock, pacing=pacing)
    await env.start()
    pacing.reads.step = 25
    start = clock.now
    await env.stop()
    # Bucket wait 1 uses 25 of 30 s, read 1 gets the 5 s left, and after one
    # 2 s poll bucket wait 2 hits the deadline, so no second read starts.
    assert _reads(calls, "_pace_read") == [kernel.USAGE_WAIT, 3]
    assert _reads(calls, "retrieve") == [5]
    assert clock.now - start <= kernel.USAGE_WAIT
    assert usages[0].calls == ["reserve", "confirm", "record", "settle"]


async def test_delete_check_stays_inside_the_stop_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bound the delete check's bucket wait and read by one STOP_TIMEOUT, and keep the hold when it times out.

    Args:
        monkeypatch: Replaces the browser read and ``asyncio.wait_for``.
    """
    clock = FakeClock()
    calls = _clock_wait_for(monkeypatch, clock)

    async def slow_retrieve(self: FakeBrowsers, session_id: str, *, include_deleted: bool) -> FakeDetails:
        """Use up 20 fake seconds and report the browser as deleted.

        Args:
            self: The fake browsers resource.
            session_id: The browser id.
            include_deleted: Must be True to read a deleted browser.

        Returns:
            Details with ``deleted_at``, which the timeout must discard.
        """
        clock.now += 20
        return FakeDetails({"session_id": session_id, "deleted_at": "2026-11-24T00:05:00Z"})

    monkeypatch.setattr(FakeBrowsers, "retrieve", slow_retrieve)
    pacing = _ClockPacing(clock)
    env, usages = _protected(clock=clock, pacing=pacing)
    await env.start()
    FakeState.list_hidden = True
    FakeState.delete_errors = [_status_error(kernel.APIStatusError, 500)]
    pacing.reads.step = 50
    start = clock.now
    with pytest.raises(UsagePendingError):
        await env.stop()
    # The bucket wait uses 50 of 60 s, so the read gets the 10 s left and times out.
    assert _reads(calls, "_pace_read") == [kernel.STOP_TIMEOUT]
    assert _reads(calls, "slow_retrieve") == [kernel.STOP_TIMEOUT - 50]
    assert clock.now - start <= kernel.STOP_TIMEOUT
    assert usages[0].calls == ["reserve", "confirm", "pending"]
    assert usages[0].measured_ms is None


async def test_usage_read_waits_for_a_ready_report() -> None:
    """Read the report again while Kernel still computes the uptime."""
    FakeState.usage_statuses = ["pending", "ready"]
    clock = FakeClock()
    env, usages = _protected(clock=clock)
    async with env:
        pass
    assert len(FakeState.retrieves) == 2
    assert kernel.USAGE_POLL in clock.sleeps
    assert usages[0].receipt is not None
    assert usages[0].receipt["usage_status"] == "ready"


async def test_settle_failure_is_raised_from_stop_once() -> None:
    """Raise a pending-usage error from the first stop and return quietly from the next."""
    env, _ = _protected(billing_error=RuntimeError("database detail"))
    await env.start()
    with pytest.raises(UsagePendingError):
        await env.stop()
    await env.stop()


async def test_protected_commands_stop_at_the_funded_end() -> None:
    """Cap a command at the funded time left and refuse one after the funded end."""
    clock = FakeClock()
    env, _ = _protected(clock=clock)
    async with env:
        clock.now += 100
        await env.exec("true", timeout_sec=1000)
        assert FakeState.execs[-1]["timeout_sec"] == 200
        clock.now += 200
        with pytest.raises(SandboxInfraError, match="funded"):
            await env.exec("true")


class _ClockReads:
    """Move a fake clock forward on each read wait once armed."""

    def __init__(self, clock: FakeClock) -> None:
        """Keep the clock and start disarmed.

        Args:
            clock: The fake clock of the box.
        """
        self.clock = clock
        self.step = 0.0

    async def acquire(self) -> None:
        """Advance the clock by the armed step."""
        self.clock.now += self.step


class _ClockPacing:
    """Hold a clock-moving read bucket and an open create bucket."""

    def __init__(self, clock: FakeClock) -> None:
        """Create the buckets.

        Args:
            clock: The fake clock of the box.
        """
        self.reads = _ClockReads(clock)
        self.creates = _GateBucket()


async def test_protected_command_cap_counts_the_read_wait() -> None:
    """Compute the funded cap after the read wait, so the wait does not extend a command."""
    clock = FakeClock()
    pacing = _ClockPacing(clock)
    env, _ = _protected(clock=clock, pacing=pacing)
    async with env:
        pacing.reads.step = 100
        await env.exec("true", timeout_sec=1000)
        pacing.reads.step = 0
        assert FakeState.execs[-1]["timeout_sec"] == 200


async def test_stopped_box_cannot_start_again() -> None:
    """Refuse a start after a stop, so no second browser runs on a settled reservation."""
    env, usages = _protected()
    await env.start()
    await env.stop()
    with pytest.raises(SandboxInfraError, match="cannot start again"):
        await env.start()
    assert len(FakeState.create_calls) == 1
    assert len(usages) == 1


async def test_failed_delete_gets_a_second_delete_from_the_sweep() -> None:
    """Delete the known browser again in the sweep after its first delete fails, then settle it."""
    clock = FakeClock()
    env, usages = _protected(clock=clock)
    await env.start()
    clock.now += 30
    FakeState.delete_errors = [_status_error(kernel.APIStatusError, 500)]
    await env.stop()
    usage = usages[0]
    assert FakeState.browsers == {}
    assert FakeState.deleted == [usage.browser.session_id]
    assert usage.calls == ["reserve", "confirm", "record", "settle"]
    assert usage.measured_ms == 30_000


async def test_protected_box_settles_on_a_real_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Settle a protected box at Kernel's reported uptime through the real adapter and ledger.

    Args:
        tmp_path: Per-test directory for the SQLite file.
        monkeypatch: The pytest monkeypatch fixture.
    """
    monkeypatch.setattr(settings, "usage_markup", 1.0)
    engine = create_engine(f"sqlite:///{tmp_path / 'kernel.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(username="alice", stripe_customer_id="fixture", balance_cents=100, grant_remaining=0)
        )
        session.commit()
    service = BudgetService(engine=engine)
    budget = service.create("alice", 50, idempotency_key="draft")
    runtime = BudgetRuntime(service, username="alice", budget_id=budget.id, generation=0, phase="setup", wait_timeout=0)
    async with kernel.KernelEnvironment(_spec(operation_key="box-op"), budget=runtime, clock=FakeClock()):
        pass
    with Session(engine) as session:
        operation = session.scalar(select(ExecutionOperationModel))
        assert operation.state == "settled"
    # 60 s x $0.0001333336/s = $0.0080000160.
    assert service.get_operation(operation.id, "alice").actual_cents == Decimal("0.80000160")
    assert runtime._pinned == {}
    engine.dispose()


@pytest.mark.parametrize(("elapsed", "expected_ms"), [(10.2, 10_200), (10.2004, 10_201)])
async def test_measured_time_ignores_float_noise_of_a_large_clock(elapsed: float, expected_ms: int) -> None:
    """Keep a long-running host's large monotonic clock from adding a millisecond to the bill.

    Args:
        elapsed: The seconds between the create and the stop readings.
        expected_ms: The measured milliseconds the usage must record.
    """
    clock = FakeClock()
    clock.now = 1e9
    env, usages = _protected(clock=clock)
    async with env:
        clock.now += elapsed
    assert usages[0].measured_ms == expected_ms

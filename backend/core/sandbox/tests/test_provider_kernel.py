"""Tests for the Kernel provider against a fake SDK, with no network and no cost."""

from __future__ import annotations

import asyncio
import base64
import itertools
import shlex
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any, ClassVar

import httpx
import pytest

from core.sandbox import factory
from core.sandbox.capabilities import SandboxSpecError
from core.sandbox.environment import JOB_TAG
from core.sandbox.errors import SandboxFatalError, SandboxInfraError, StartTimeoutError
from core.sandbox.pacing import ProviderAtCapacityError
from core.sandbox.providers import kernel
from core.sandbox.resources import ResourceRequest, SandboxSpec
from core.sandbox.tests.test_environment import SandboxEnvironmentContract

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
        self.files: dict[str, bytes] = {}


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
    closed: ClassVar[int] = 0
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

    async def read_file(self, session_id: str, *, path: str) -> FakeBinary:
        """Return one file.

        Args:
            session_id: The browser id.
            path: The path.

        Returns:
            The binary response.

        Raises:
            NotFoundError: The file does not exist.
        """
        files = FakeState.browsers[session_id].files
        if path not in files:
            raise _status_error(kernel.NotFoundError, 404)
        return FakeBinary(files[path])


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
        """List the browsers whose tags match.

        Args:
            tags: The tags to match.

        Yields:
            Each match.
        """
        for browser in list(FakeState.browsers.values()):
            if all(browser.tags.get(key) == value for key, value in tags.items()):
                yield browser

    async def delete_by_id(self, session_id: str) -> None:
        """Delete one browser.

        Args:
            session_id: The browser id.

        Raises:
            NotFoundError: The browser does not exist.
        """
        if FakeState.browsers.pop(session_id, None) is None:
            raise _status_error(kernel.NotFoundError, 404)
        FakeState.deleted.append(session_id)


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
    async with kernel.KernelEnvironment(spec):
        pass
    assert FakeState.clients[0]["max_retries"] == 0
    (call,) = FakeState.create_calls
    assert call["headless"] is False
    assert call["name"] == "box-a"
    assert call["timeout_seconds"] == 300
    assert call["tags"] == {JOB_TAG: "job-1", kernel.BOX_TAG: "box-a"}
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
    """Leave out the command and HTTP limits when the caller sets none."""
    async with kernel.KernelEnvironment(_spec()) as env:
        await env.exec("true")
    call = FakeState.execs[-1]
    assert "timeout_sec" not in call
    assert "timeout" not in call


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
    FakeState.new_browser({"name": "box-b", "tags": {kernel.BOX_TAG: "box-b"}})
    FakeState.new_browser({"name": "keep", "tags": {kernel.BOX_TAG: "keep"}})
    await env.stop()
    assert [b.name for b in FakeState.browsers.values()] == ["keep"]
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
